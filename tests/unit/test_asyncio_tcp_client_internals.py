"""
Internal-contract tests for AsyncioTcpClient supervision.

These tests cover the coordination code that turns low-level asyncio failures
into stable client behavior: runtime-state ownership, reconnect planning,
keepalive warnings, ordered teardown, and emitted-event sequencing. They reach
into private helpers on purpose because regressions in this layer quickly turn
into user-visible reconnect bugs.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.event_delivery_settings import (
    EventBackpressurePolicy,
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
)
from aionetx.api.error_policy import ErrorPolicy
from aionetx.api.handler_failure_policy_stop_event import HandlerFailurePolicyStopEvent
from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_events import ConnectionClosedEvent, ConnectionOpenedEvent
from aionetx.api.connection_lifecycle import ConnectionRole, ConnectionState
from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.reconnect_events import ReconnectScheduledEvent
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.implementations.asyncio_impl import asyncio_tcp_client as tcp_client_module
from aionetx.implementations.asyncio_impl import (
    tcp_client_supervision as tcp_client_supervision_module,
)
from aionetx.implementations.asyncio_impl import _tcp_client_connect as tcp_client_connect_module
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_connection import AsyncioTcpConnection
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher
from tests.helpers import assert_awaitable_cancelled
from tests.helpers import drain_awaitable_ignoring_cancelled
from tests.helpers import wait_for_condition
from tests.internal_asyncio_impl_refs import WarningRateLimiter


class _SocketThatFailsKeepalive:
    """Socket double that always fails keepalive configuration."""

    def setsockopt(self, level: int, optname: int, value: int) -> None:
        raise OSError("keepalive-not-supported")


class _WriterWithPeerInfo:
    """Writer stub that exposes peer info and a failing socket object."""

    def __init__(self, peer_info: object) -> None:
        self._peer_info = peer_info

    def get_extra_info(self, key: str):
        if key == "peername":
            return self._peer_info
        if key == "socket":
            return _SocketThatFailsKeepalive()
        return None


class _BlockingDrainWriter:
    """Writer double whose drain can either block indefinitely or be released."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.drain_started = asyncio.Event()
        self.release_drain = asyncio.Event()
        self.closed = False

    def get_extra_info(self, key: str, default=None):
        if key == "peername":
            return ("127.0.0.1", 45678)
        if key == "sockname":
            return ("127.0.0.1", 12345)
        if key == "socket":
            return None
        return default

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        self.drain_started.set()
        await self.release_drain.wait()

    def close(self) -> None:
        self.closed = True
        self.release_drain.set()

    async def wait_closed(self) -> None:
        self.closed = True


class _WriterProxy:
    """Wrap a real writer while overriding metadata used by client internals."""

    def __init__(
        self, writer: asyncio.StreamWriter, *, peer_info: object, socket_obj: object
    ) -> None:
        self._writer = writer
        self._peer_info = peer_info
        self._socket_obj = socket_obj

    def get_extra_info(self, key: str, default=None):
        if key == "peername":
            return self._peer_info
        if key == "socket":
            return self._socket_obj
        return self._writer.get_extra_info(key, default)

    def __getattr__(self, name: str):
        return getattr(self._writer, name)


def _track_emitted_event_order(recording_event_handler, call_order: list[str]) -> None:
    """Record emitted event types while preserving the original handler behavior."""

    original_on_event = recording_event_handler.on_event

    async def _on_event(event) -> None:
        call_order.append(type(event).__name__)
        await original_on_event(event)

    recording_event_handler.on_event = _on_event  # type: ignore[method-assign]


class _FailOnClientLifecycleState:
    def __init__(self, state: ComponentLifecycleState) -> None:
        self._state = state

    async def on_event(self, event) -> None:
        if isinstance(event, ComponentLifecycleChangedEvent) and event.current == self._state:
            raise RuntimeError(f"client-{self._state.value}-publication-failed")


# Runtime-state ownership and basic supervision wiring.
@pytest.mark.asyncio
async def test_client_runtime_state_groups_mutable_supervision_fields(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=45678, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )

    assert client._runtime.running is False  # type: ignore[attr-defined]
    assert client._runtime.connection is None  # type: ignore[attr-defined]
    assert client._runtime.attempt_counter == 0  # type: ignore[attr-defined]

    client._running = True  # type: ignore[attr-defined]
    client._attempt_counter = 4  # type: ignore[attr-defined]
    client._notify_status_changed()  # type: ignore[attr-defined]

    assert client._runtime.running is True  # type: ignore[attr-defined]
    assert client._runtime.attempt_counter == 4  # type: ignore[attr-defined]
    assert client._runtime.status_version == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_client_connection_closed_event_is_owned_by_session_runtime_state(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=45688, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )

    assert client._connection_closed_event is client._runtime.connection_closed_event  # type: ignore[attr-defined]
    assert client._connection_closed_event.is_set() is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_client_connection_uses_fallback_id_when_peer_info_invalid_and_missing_socket(
    recording_event_handler,
) -> None:
    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]

        async def _open_connection(*, host: str, port: int):
            reader, writer = await asyncio.open_connection(host, port)
            return reader, _WriterProxy(writer, peer_info="invalid-peer-info", socket_obj=None)

        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1", port=port, reconnect=TcpReconnectSettings(enabled=False)
            ),
            event_handler=recording_event_handler,
            connection_opener=_open_connection,
        )
        await client.start()
        try:
            connection = await client.wait_until_connected(timeout_seconds=2.0)
            assert connection.connection_id == f"tcp/client/127.0.0.1/{port}/connection"
        finally:
            await client.stop()


async def _connect_once_with_blocking_writer(
    *,
    recording_event_handler,
    connection_send_timeout_seconds: float | None,
) -> tuple[AsyncioTcpConnection, _BlockingDrainWriter, AsyncioEventDispatcher]:
    writer = _BlockingDrainWriter()

    async def _open_connection(**_kwargs: object):
        return asyncio.StreamReader(), writer

    dispatcher = AsyncioEventDispatcher(
        recording_event_handler,
        EventDeliverySettings(),
        logging.getLogger("test"),
    )
    await dispatcher.start()
    connection = await tcp_client_connect_module.connect_once(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=12345,
            reconnect=TcpReconnectSettings(enabled=False),
            connection_send_timeout_seconds=connection_send_timeout_seconds,
        ),
        connection_opener=_open_connection,
        event_dispatcher=dispatcher,
        on_closed_callback=lambda _connection: None,
        logger=logging.getLogger("test"),
        component_id="tcp/client/127.0.0.1/12345",
    )
    return connection, writer, dispatcher


@pytest.mark.asyncio
async def test_connect_once_applies_connection_send_timeout_to_managed_connection(
    recording_event_handler,
) -> None:
    connection, writer, dispatcher = await _connect_once_with_blocking_writer(
        recording_event_handler=recording_event_handler,
        connection_send_timeout_seconds=0.01,
    )

    try:
        with pytest.raises(asyncio.TimeoutError):
            await connection.send(b"payload")

        assert writer.writes == [b"payload"]
        assert writer.drain_started.is_set()
    finally:
        await connection.close()
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_connect_once_disabled_connection_send_timeout_waits_for_drain_completion(
    recording_event_handler,
) -> None:
    connection, writer, dispatcher = await _connect_once_with_blocking_writer(
        recording_event_handler=recording_event_handler,
        connection_send_timeout_seconds=None,
    )
    send_task = asyncio.create_task(connection.send(b"payload"))

    try:
        await asyncio.wait_for(writer.drain_started.wait(), timeout=1.0)
        await asyncio.sleep(0)

        assert send_task.done() is False

        writer.release_drain.set()
        assert await asyncio.wait_for(send_task, timeout=1.0) is None
        assert writer.writes == [b"payload"]
    finally:
        writer.release_drain.set()
        if not send_task.done():
            send_task.cancel()
        await connection.close()
        await dispatcher.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_state", [ComponentLifecycleState.STARTING, ComponentLifecycleState.RUNNING]
)
async def test_client_start_lifecycle_publication_failure_rolls_back_supervision(
    failing_state: ComponentLifecycleState,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45693,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.INLINE,
                handler_failure_policy=EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE,
            ),
        ),
        event_handler=_FailOnClientLifecycleState(failing_state),
    )

    with pytest.raises(RuntimeError, match=f"client-{failing_state.value}-publication-failed"):
        await client.start()

    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert client._supervisor_task is None  # type: ignore[attr-defined]
    assert client._event_dispatcher.is_running is False  # type: ignore[attr-defined]
    await client.stop()


# Keepalive warnings and active-resource teardown helpers.
@pytest.mark.asyncio
async def test_client_keepalive_warning_is_rate_limited(
    recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Exercise the keepalive helper directly so warning-rate behavior stays
    # pinned even if the surrounding supervisor wiring changes.
    writer = _WriterWithPeerInfo(("127.0.0.1", 1111))

    monkeypatch.setattr(
        tcp_client_connect_module, "_warning_limiter", WarningRateLimiter(interval_seconds=60.0)
    )

    import logging as _logging

    _logger = _logging.getLogger("test_keepalive")
    with caplog.at_level(logging.WARNING):
        tcp_client_connect_module._apply_tcp_keepalive(
            writer=writer,  # type: ignore[arg-type]
            component_id="tcp/client/127.0.0.1/34567",
            logger=_logger,
        )
        tcp_client_connect_module._apply_tcp_keepalive(
            writer=writer,  # type: ignore[arg-type]
            component_id="tcp/client/127.0.0.1/34567",
            logger=_logger,
        )

    warning_messages = [
        message
        for message in caplog.messages
        if "Failed to enable TCP keepalive on client socket." in message
    ]
    assert len(warning_messages) == 1


@pytest.mark.asyncio
async def test_client_stop_heartbeat_is_idempotent_for_same_sender(recording_event_handler) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=45679, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )

    class _Sender:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1

    sender = _Sender()
    client._heartbeat_sender = sender  # type: ignore[attr-defined]

    await client._stop_heartbeat_sender()  # type: ignore[attr-defined]
    await client._stop_heartbeat_sender()  # type: ignore[attr-defined]

    assert sender.stop_calls == 1


@pytest.mark.asyncio
async def test_client_supervisor_shutdown_resources_stops_heartbeat_before_connection_close(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=45680, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )
    call_order: list[str] = []

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")

    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]

    await client._connection_supervisor._shutdown_active_resources()  # type: ignore[attr-defined]

    assert call_order == ["heartbeat-stop", "connection-close"]


@pytest.mark.asyncio
async def test_client_stop_preserves_caller_cancellation_after_supervisor_cleanup(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=45681, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )
    supervisor_cancel_seen = asyncio.Event()
    release_supervisor = asyncio.Event()
    call_order: list[str] = []

    async def blocking_supervisor() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            supervisor_cancel_seen.set()
            await release_supervisor.wait()
            raise

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")

    supervisor_task = asyncio.create_task(blocking_supervisor())
    client._running = True  # type: ignore[attr-defined]
    client._lifecycle_state = ComponentLifecycleState.RUNNING  # type: ignore[attr-defined]
    client._supervisor_task = supervisor_task  # type: ignore[attr-defined]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]
    await client._event_dispatcher.start()  # type: ignore[attr-defined]
    stop_task = asyncio.create_task(client.stop())

    try:
        await asyncio.wait_for(supervisor_cancel_seen.wait(), timeout=1.0)
        stop_task.cancel()
        await asyncio.sleep(0)

        assert not stop_task.done()

        release_supervisor.set()
        await assert_awaitable_cancelled(stop_task)

        assert call_order == ["heartbeat-stop", "connection-close"]
        assert client.lifecycle_state == ComponentLifecycleState.STOPPED
        assert client._supervisor_task is None  # type: ignore[attr-defined]
        assert client._event_dispatcher.is_running is False  # type: ignore[attr-defined]
    finally:
        release_supervisor.set()
        if not stop_task.done():
            stop_task.cancel()
            await drain_awaitable_ignoring_cancelled(stop_task)
        if not supervisor_task.done():
            supervisor_task.cancel()
            await drain_awaitable_ignoring_cancelled(supervisor_task)
        if client._event_dispatcher.is_running:  # type: ignore[attr-defined]
            await client._event_dispatcher.stop()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_client_concurrent_stop_waits_for_owner_without_duplicate_cleanup(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=45682, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )
    stopping_seen = asyncio.Event()
    release_stopping = asyncio.Event()
    call_order: list[str] = []
    lifecycle_emits: list[str] = []

    async def fake_emit_lifecycle_event(event) -> None:
        if event is None:
            return
        lifecycle_emits.append(event.current.value)
        if event.current == ComponentLifecycleState.STOPPING:
            stopping_seen.set()
            await release_stopping.wait()

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")

    client._running = True  # type: ignore[attr-defined]
    client._lifecycle_state = ComponentLifecycleState.RUNNING  # type: ignore[attr-defined]
    client._emit_lifecycle_event = fake_emit_lifecycle_event  # type: ignore[attr-defined,method-assign]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]
    await client._event_dispatcher.start()  # type: ignore[attr-defined]

    owner_stop_task = asyncio.create_task(client.stop())
    try:
        await asyncio.wait_for(stopping_seen.wait(), timeout=1.0)

        waiter_stop_task = asyncio.create_task(client.stop())
        await asyncio.sleep(0)

        assert not waiter_stop_task.done()
        assert call_order == []

        release_stopping.set()
        await asyncio.wait_for(asyncio.gather(owner_stop_task, waiter_stop_task), timeout=1.0)

        assert call_order == ["heartbeat-stop", "connection-close"]
        assert lifecycle_emits == ["stopping", "stopped"]
        assert client.lifecycle_state == ComponentLifecycleState.STOPPED
        assert client._event_dispatcher.is_running is False  # type: ignore[attr-defined]
    finally:
        release_stopping.set()
        for task in (owner_stop_task, locals().get("waiter_stop_task")):
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
                await drain_awaitable_ignoring_cancelled(task)
        if client._event_dispatcher.is_running:  # type: ignore[attr-defined]
            await client._event_dispatcher.stop()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_client_stop_reentry_from_owner_task_is_noop(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=45683, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )
    call_order: list[str] = []
    lifecycle_emits: list[str] = []

    async def fake_emit_lifecycle_event(event) -> None:
        if event is None:
            return
        lifecycle_emits.append(event.current.value)
        if event.current == ComponentLifecycleState.STOPPING:
            await client.stop()

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")

    client._running = True  # type: ignore[attr-defined]
    client._lifecycle_state = ComponentLifecycleState.RUNNING  # type: ignore[attr-defined]
    client._emit_lifecycle_event = fake_emit_lifecycle_event  # type: ignore[attr-defined,method-assign]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]
    await client._event_dispatcher.start()  # type: ignore[attr-defined]

    try:
        await client.stop()

        assert call_order == ["heartbeat-stop", "connection-close"]
        assert lifecycle_emits == ["stopping", "stopped"]
        assert client.lifecycle_state == ComponentLifecycleState.STOPPED
        assert client._event_dispatcher.is_running is False  # type: ignore[attr-defined]
    finally:
        if client._event_dispatcher.is_running:  # type: ignore[attr-defined]
            await client._event_dispatcher.stop()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_client_connect_attempt_clears_cached_last_connect_error_before_new_attempt(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45691,
            reconnect=TcpReconnectSettings(enabled=True, initial_delay_seconds=0.05),
        ),
        event_handler=recording_event_handler,
    )
    observed_cached_error: list[object] = []
    client._last_connect_error = RuntimeError("stale-connect-error")  # type: ignore[attr-defined]
    client._apply_lifecycle_state(ComponentLifecycleState.STARTING)  # type: ignore[attr-defined]
    client._apply_lifecycle_state(ComponentLifecycleState.RUNNING)  # type: ignore[attr-defined]

    async def fake_connect_once() -> None:
        observed_cached_error.append(client._last_connect_error)  # type: ignore[attr-defined]

    client._connect_once = fake_connect_once  # type: ignore[attr-defined,method-assign]

    await client._connection_supervisor._start_connect_attempt()  # type: ignore[attr-defined]

    assert observed_cached_error == [None]
    assert client._last_connect_error is None  # type: ignore[attr-defined]


# Supervisor finalization and lifecycle publication ordering.
@pytest.mark.asyncio
async def test_client_supervisor_finalize_emits_terminal_lifecycle_transitions_in_order(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=45683, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )
    call_order: list[str] = []
    lifecycle_emits: list[str] = []

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")

    async def fake_emit_lifecycle_event(event) -> None:
        if event is not None:
            lifecycle_emits.append(event.current.value)

    client._lifecycle_state = tcp_client_module.ComponentLifecycleState.RUNNING  # type: ignore[attr-defined]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]
    client._emit_lifecycle_event = fake_emit_lifecycle_event  # type: ignore[attr-defined,method-assign]

    await client._connection_supervisor._finalize_supervision()  # type: ignore[attr-defined]

    assert call_order == ["heartbeat-stop", "connection-close"]
    assert lifecycle_emits == ["stopping", "stopped"]


@pytest.mark.asyncio
async def test_client_supervisor_finalize_stops_dispatcher_when_terminal_publish_fails(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45682,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        ),
        event_handler=recording_event_handler,
    )

    async def fake_stop_heartbeat() -> None:
        return None

    async def fake_close_connection() -> None:
        return None

    async def fail_terminal_publication(event) -> None:
        raise RuntimeError("terminal-publish-failed")

    client._running = True  # type: ignore[attr-defined]
    client._lifecycle_state = tcp_client_module.ComponentLifecycleState.RUNNING  # type: ignore[attr-defined]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]
    client._emit_lifecycle_event = fail_terminal_publication  # type: ignore[attr-defined,method-assign]

    await client._event_dispatcher.start()  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="terminal-publish-failed"):
        await client._connection_supervisor._finalize_supervision()  # type: ignore[attr-defined]

    assert client._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_client_supervisor_collect_terminal_events_is_idempotent_under_concurrency(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=45694, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )
    client._lifecycle_state = tcp_client_module.ComponentLifecycleState.RUNNING  # type: ignore[attr-defined]

    results = await asyncio.gather(
        client._connection_supervisor._collect_terminal_lifecycle_events(),  # type: ignore[attr-defined]
        client._connection_supervisor._collect_terminal_lifecycle_events(),  # type: ignore[attr-defined]
    )
    emitted_states = [event.current for batch in results for event in batch]

    assert client.lifecycle_state == tcp_client_module.ComponentLifecycleState.STOPPED
    assert emitted_states.count(tcp_client_module.ComponentLifecycleState.STOPPING) == 1
    assert emitted_states.count(tcp_client_module.ComponentLifecycleState.STOPPED) == 1


@pytest.mark.asyncio
async def test_client_supervisor_run_cancellation_finalizes_teardown_and_lifecycle_order(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=45684, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )
    call_order: list[str] = []
    lifecycle_emits: list[str] = []

    async def fake_run_connect_cycle() -> bool:
        raise asyncio.CancelledError

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")

    async def fake_emit_lifecycle_event(event) -> None:
        if event is not None:
            lifecycle_emits.append(event.current.value)

    client._running = True  # type: ignore[attr-defined]
    client._lifecycle_state = tcp_client_module.ComponentLifecycleState.RUNNING  # type: ignore[attr-defined]
    client._connection_supervisor._run_connect_cycle = fake_run_connect_cycle  # type: ignore[attr-defined,method-assign]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]
    client._emit_lifecycle_event = fake_emit_lifecycle_event  # type: ignore[attr-defined,method-assign]

    with pytest.raises(asyncio.CancelledError):
        await client._connection_supervisor.run()  # type: ignore[attr-defined]

    assert call_order == ["heartbeat-stop", "connection-close"]
    assert lifecycle_emits == ["stopping", "stopped"]


# Failure-path ordering, teardown, and reconnect signaling.
@pytest.mark.asyncio
async def test_client_supervision_failure_orders_events_before_teardown_and_reschedule(
    recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry failures should publish teardown and reschedule events in a stable order."""
    call_order: list[str] = []
    _track_emitted_event_order(recording_event_handler, call_order)

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45681,
            reconnect=TcpReconnectSettings(
                enabled=True, initial_delay_seconds=0.05, max_delay_seconds=0.05
            ),
            error_policy=ErrorPolicy.RETRY,
        ),
        event_handler=recording_event_handler,
    )

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")

    async def fake_wait_for_reconnect_delay_or_stop(*, delay_seconds: float) -> bool:
        call_order.append(f"wait:{delay_seconds}")
        return True

    client._attempt_counter = 3  # type: ignore[attr-defined]
    client._running = True  # type: ignore[attr-defined]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]
    client._connection_supervisor._wait_for_reconnect_delay_or_stop = (  # type: ignore[attr-defined,method-assign]
        fake_wait_for_reconnect_delay_or_stop
    )

    should_continue = await client._connection_supervisor._handle_connect_cycle_failure(  # type: ignore[attr-defined]
        RuntimeError("connect-failed")
    )

    assert should_continue is True
    assert call_order == [
        "NetworkErrorEvent",
        "ReconnectAttemptFailedEvent",
        "heartbeat-stop",
        "connection-close",
        "ReconnectScheduledEvent",
        "wait:0.05",
    ]
    assert call_order.index("ReconnectScheduledEvent") < call_order.index("wait:0.05")


@pytest.mark.asyncio
async def test_client_supervision_failure_cancellation_during_sleep_keeps_prior_ordering(
    recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during backoff must not reorder already-emitted retry signals."""
    call_order: list[str] = []
    _track_emitted_event_order(recording_event_handler, call_order)

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45682,
            reconnect=TcpReconnectSettings(
                enabled=True, initial_delay_seconds=0.05, max_delay_seconds=0.05
            ),
            error_policy=ErrorPolicy.RETRY,
        ),
        event_handler=recording_event_handler,
    )

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")

    async def fake_wait_for_reconnect_delay_or_stop(*, delay_seconds: float) -> bool:
        call_order.append(f"wait-cancel:{delay_seconds}")
        raise asyncio.CancelledError

    client._attempt_counter = 7  # type: ignore[attr-defined]
    client._running = True  # type: ignore[attr-defined]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]
    client._connection_supervisor._wait_for_reconnect_delay_or_stop = (  # type: ignore[attr-defined,method-assign]
        fake_wait_for_reconnect_delay_or_stop
    )

    with pytest.raises(asyncio.CancelledError):
        await client._connection_supervisor._handle_connect_cycle_failure(
            RuntimeError("connect-failed")
        )  # type: ignore[attr-defined]

    assert call_order == [
        "NetworkErrorEvent",
        "ReconnectAttemptFailedEvent",
        "heartbeat-stop",
        "connection-close",
        "ReconnectScheduledEvent",
        "wait-cancel:0.05",
    ]
    assert call_order.index("ReconnectScheduledEvent") < call_order.index("wait-cancel:0.05")


@pytest.mark.asyncio
async def test_client_supervision_failure_with_reconnect_disabled_does_not_reschedule(
    recording_event_handler,
) -> None:
    call_order: list[str] = []
    _track_emitted_event_order(recording_event_handler, call_order)

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45685,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")

    client._attempt_counter = 5  # type: ignore[attr-defined]
    client._running = True  # type: ignore[attr-defined]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]

    should_continue = await client._connection_supervisor._handle_connect_cycle_failure(
        RuntimeError("connect-failed")
    )  # type: ignore[attr-defined]

    assert should_continue is False
    assert call_order == [
        "NetworkErrorEvent",
        "ReconnectAttemptFailedEvent",
        "heartbeat-stop",
        "connection-close",
    ]


@pytest.mark.asyncio
async def test_client_supervision_failure_ignore_policy_skips_network_error_event(
    recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IGNORE suppresses NetworkErrorEvent but still publishes retry lifecycle signals."""
    call_order: list[str] = []
    _track_emitted_event_order(recording_event_handler, call_order)

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45686,
            reconnect=TcpReconnectSettings(
                enabled=True, initial_delay_seconds=0.05, max_delay_seconds=0.05
            ),
            error_policy=ErrorPolicy.IGNORE,
        ),
        event_handler=recording_event_handler,
    )

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")

    async def fake_wait_for_reconnect_delay_or_stop(*, delay_seconds: float) -> bool:
        call_order.append(f"wait:{delay_seconds}")
        return True

    client._attempt_counter = 9  # type: ignore[attr-defined]
    client._running = True  # type: ignore[attr-defined]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]
    client._connection_supervisor._wait_for_reconnect_delay_or_stop = (  # type: ignore[attr-defined,method-assign]
        fake_wait_for_reconnect_delay_or_stop
    )

    should_continue = await client._connection_supervisor._handle_connect_cycle_failure(
        RuntimeError("connect-failed")
    )  # type: ignore[attr-defined]

    assert should_continue is True
    assert call_order == [
        "ReconnectAttemptFailedEvent",
        "heartbeat-stop",
        "connection-close",
        "ReconnectScheduledEvent",
        "wait:0.05",
    ]
    assert call_order.index("ReconnectScheduledEvent") < call_order.index("wait:0.05")


# Reconnect-plan validation and wait coordination.
@pytest.mark.asyncio
async def test_reconnect_plan_requires_delay_when_reconnect_enabled() -> None:
    with pytest.raises(ValueError, match="Reconnect plan delay must be set"):
        tcp_client_supervision_module._ReconnectPlan(should_reconnect=True, delay_seconds=None)


@pytest.mark.asyncio
async def test_client_disconnect_without_reconnect_does_not_schedule_sleep(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45687,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )
    client._running = True  # type: ignore[attr-defined]

    should_continue = await client._connection_supervisor._schedule_reconnect_after_disconnect()  # type: ignore[attr-defined]

    assert should_continue is False


@pytest.mark.asyncio
async def test_client_supervision_failure_stop_during_reconnect_scheduled_skips_sleep(
    recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order: list[str] = []
    _track_emitted_event_order(recording_event_handler, call_order)

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45688,
            reconnect=TcpReconnectSettings(
                enabled=True, initial_delay_seconds=0.05, max_delay_seconds=0.05
            ),
            error_policy=ErrorPolicy.RETRY,
        ),
        event_handler=recording_event_handler,
    )

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")

    original_on_event = recording_event_handler.on_event

    async def stop_on_reconnect_scheduled(event) -> None:
        await original_on_event(event)
        if isinstance(event, ReconnectScheduledEvent):
            client._running = False  # type: ignore[attr-defined]

    client._attempt_counter = 11  # type: ignore[attr-defined]
    client._running = True  # type: ignore[attr-defined]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]
    recording_event_handler.on_event = stop_on_reconnect_scheduled  # type: ignore[method-assign]

    should_continue = await client._connection_supervisor._handle_connect_cycle_failure(  # type: ignore[attr-defined]
        RuntimeError("connect-failed")
    )

    assert should_continue is False
    assert call_order == [
        "NetworkErrorEvent",
        "ReconnectAttemptFailedEvent",
        "heartbeat-stop",
        "connection-close",
        "ReconnectScheduledEvent",
    ]


@pytest.mark.asyncio
async def test_client_reconnect_wait_returns_early_when_stop_changes_status(
    recording_event_handler,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45689,
            reconnect=TcpReconnectSettings(
                enabled=True, initial_delay_seconds=0.05, max_delay_seconds=0.05
            ),
        ),
        event_handler=recording_event_handler,
    )
    client._running = True  # type: ignore[attr-defined]

    async def stop_soon() -> None:
        await asyncio.sleep(0)
        client._running = False  # type: ignore[attr-defined]
        client._notify_status_changed()  # type: ignore[attr-defined]

    stop_task = asyncio.create_task(stop_soon())
    should_continue = await client._connection_supervisor._wait_for_reconnect_delay_or_stop(  # type: ignore[attr-defined]
        delay_seconds=10.0
    )
    _ = await stop_task

    assert should_continue is False


# STOP_COMPONENT ordering when handler failures trigger client shutdown.
class _RecordAndFailOnClientStopping:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def on_event(self, event) -> None:
        self.events.append(event)
        if (
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current == ComponentLifecycleState.STOPPING
        ):
            raise RuntimeError("client-stopping-publication-failed")


@pytest.mark.asyncio
async def test_stop_component_policy_stops_heartbeat_before_connection_close() -> None:
    call_order: list[str] = []
    observed_events: list[object] = []

    class _RecordingHandler:
        async def on_event(self, event) -> None:
            observed_events.append(event)

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45690,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.INLINE,
                handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
            ),
        ),
        event_handler=_RecordingHandler(),
    )

    async def fake_stop_heartbeat() -> None:
        call_order.append("heartbeat-stop")

    async def fake_close_connection() -> None:
        call_order.append("connection-close")
        client._connection = None  # type: ignore[attr-defined]
        client._connection_closed_event.set()  # type: ignore[attr-defined]
        client._notify_status_changed()  # type: ignore[attr-defined]

    client._lifecycle_state = tcp_client_module.ComponentLifecycleState.RUNNING  # type: ignore[attr-defined]
    client._running = True  # type: ignore[attr-defined]
    client._stop_heartbeat_sender = fake_stop_heartbeat  # type: ignore[attr-defined,method-assign]
    client._close_current_connection = fake_close_connection  # type: ignore[attr-defined,method-assign]
    await client._event_dispatcher.start()  # type: ignore[attr-defined]
    await client._event_dispatcher._handle_handler_failure(  # type: ignore[attr-defined]
        error=RuntimeError("stop-component-failure"),
        triggering_event=ConnectionOpenedEvent(
            resource_id="tcp/client/127.0.0.1/45690/connection",
            metadata=ConnectionMetadata(
                connection_id="tcp/client/127.0.0.1/45690/connection",
                role=ConnectionRole.CLIENT,
            ),
        ),
    )

    assert call_order[:2] == ["heartbeat-stop", "connection-close"]
    assert client.lifecycle_state == tcp_client_module.ComponentLifecycleState.STOPPED
    assert any(type(event).__name__ == "HandlerFailurePolicyStopEvent" for event in observed_events)


@pytest.mark.asyncio
async def test_stop_component_policy_from_worker_with_full_queue_stops_client() -> None:
    first_started = asyncio.Event()
    release_failure = asyncio.Event()
    stopped = asyncio.Event()
    observed_events: list[object] = []

    class _FailFirstThenRecord:
        async def on_event(self, event) -> None:
            observed_events.append(event)
            if isinstance(event, ConnectionOpenedEvent):
                if event.metadata.connection_id.endswith("/first"):
                    first_started.set()
                    await release_failure.wait()
                    raise RuntimeError("boom")
                if event.metadata.connection_id.endswith("/queued"):
                    pytest.fail("queued user event was delivered after worker-originated stop")

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=45691,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.BACKGROUND,
                backpressure_policy=EventBackpressurePolicy.BLOCK,
                max_pending_events=1,
                handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
            ),
        ),
        event_handler=_FailFirstThenRecord(),
    )
    client._lifecycle_state = tcp_client_module.ComponentLifecycleState.RUNNING  # type: ignore[attr-defined]
    client._running = True  # type: ignore[attr-defined]
    await client._event_dispatcher.start()  # type: ignore[attr-defined]

    await client._event_dispatcher.emit(  # type: ignore[attr-defined]
        ConnectionOpenedEvent(
            resource_id="tcp/client/127.0.0.1/45691/first",
            metadata=ConnectionMetadata(
                connection_id="tcp/client/127.0.0.1/45691/first",
                role=ConnectionRole.CLIENT,
            ),
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await client._event_dispatcher.emit(  # type: ignore[attr-defined]
        ConnectionOpenedEvent(
            resource_id="tcp/client/127.0.0.1/45691/queued",
            metadata=ConnectionMetadata(
                connection_id="tcp/client/127.0.0.1/45691/queued",
                role=ConnectionRole.CLIENT,
            ),
        )
    )

    release_failure.set()

    async def _wait_until_stopped() -> None:
        while client.lifecycle_state != tcp_client_module.ComponentLifecycleState.STOPPED:
            await asyncio.sleep(0)
        stopped.set()

    await asyncio.wait_for(_wait_until_stopped(), timeout=1.0)
    await asyncio.wait_for(client._event_dispatcher.stop(), timeout=1.0)  # type: ignore[attr-defined]
    await wait_for_condition(
        lambda: any(
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current == tcp_client_module.ComponentLifecycleState.STOPPING
            for event in observed_events
        ),
        timeout_seconds=1.0,
    )

    lifecycle_states = [
        event.current
        for event in observed_events
        if isinstance(event, ComponentLifecycleChangedEvent)
    ]
    assert tcp_client_module.ComponentLifecycleState.STOPPING in lifecycle_states
    assert tcp_client_module.ComponentLifecycleState.STOPPED in lifecycle_states
    assert any(type(event).__name__ == "HandlerFailurePolicyStopEvent" for event in observed_events)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_tcp_client_stop_component_failure_during_stopping_publishes_single_terminal_sequence() -> (
    None
):
    release_server_connection = asyncio.Event()

    async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await release_server_connection.wait()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        handler = _RecordAndFailOnClientStopping()
        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
                event_delivery=EventDeliverySettings(
                    dispatch_mode=EventDispatchMode.BACKGROUND,
                    handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
                ),
            ),
            event_handler=handler,
        )

        try:
            await client.start()
            await client.wait_until_connected(timeout_seconds=1.0)
            await asyncio.wait_for(client.stop(), timeout=1.0)
        finally:
            release_server_connection.set()
            await client.stop()

    stop_relevant_events = [
        event
        for event in handler.events
        if isinstance(event, (ConnectionClosedEvent, HandlerFailurePolicyStopEvent))
        or (
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
        )
    ]

    assert [
        (
            event.current
            if isinstance(event, ComponentLifecycleChangedEvent)
            else type(event).__name__
        )
        for event in stop_relevant_events
    ] == [
        ComponentLifecycleState.STOPPING,
        "HandlerFailurePolicyStopEvent",
        "ConnectionClosedEvent",
        ComponentLifecycleState.STOPPED,
    ]
    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert client._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_client_open_event_failure_closes_partially_started_connection_without_read_loop_leak() -> (
    None
):
    server_connection_opened = asyncio.Event()
    release_server_connection = asyncio.Event()

    async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        server_connection_opened.set()
        try:
            await release_server_connection.wait()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(server.start_serving(), timeout=1.0)

        class _FailOnOpenedEvent:
            async def on_event(self, event) -> None:
                if isinstance(event, ConnectionOpenedEvent):
                    raise RuntimeError("boom-on-opened")

        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
                event_delivery=EventDeliverySettings(
                    dispatch_mode=EventDispatchMode.INLINE,
                    handler_failure_policy=EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE,
                ),
            ),
            event_handler=_FailOnOpenedEvent(),
        )

        await client.start()
        await asyncio.wait_for(server_connection_opened.wait(), timeout=1.0)

        async def _wait_until_stopped() -> None:
            while client.lifecycle_state != tcp_client_module.ComponentLifecycleState.STOPPED:
                await asyncio.sleep(0)

        await asyncio.wait_for(_wait_until_stopped(), timeout=1.0)
        await client.stop()
        await asyncio.sleep(0)

        leaked_read_loops = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().endswith("-read-loop")
            and task.get_name().startswith(f"tcp/client/127.0.0.1/{port}/connection")
            and not task.done()
        ]
        assert leaked_read_loops == []

        release_server_connection.set()


@pytest.mark.asyncio
async def test_client_stop_during_opened_publication_closes_startup_connection() -> None:
    server_connection_opened = asyncio.Event()
    server_payload_sent = asyncio.Event()
    server_saw_eof = asyncio.Event()
    opened_started = asyncio.Event()
    opened_cancelled = asyncio.Event()
    release_opened = asyncio.Event()
    bytes_seen = asyncio.Event()
    closed_seen = asyncio.Event()

    async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        server_connection_opened.set()
        writer.write(b"payload-before-opened-completes")
        await writer.drain()
        server_payload_sent.set()
        try:
            if await reader.read() == b"":
                server_saw_eof.set()
        finally:
            writer.close()
            await writer.wait_closed()

    class _BlockOpenedHandler:
        async def on_event(self, event) -> None:
            if isinstance(event, ConnectionOpenedEvent):
                opened_started.set()
                try:
                    await release_opened.wait()
                except asyncio.CancelledError:
                    opened_cancelled.set()
                    raise
            elif isinstance(event, BytesReceivedEvent):
                bytes_seen.set()
            elif isinstance(event, ConnectionClosedEvent):
                closed_seen.set()

    server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(server.start_serving(), timeout=1.0)
        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
                event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
            ),
            event_handler=_BlockOpenedHandler(),
        )

        await client.start()
        try:
            await asyncio.wait_for(server_connection_opened.wait(), timeout=1.0)
            await asyncio.wait_for(server_payload_sent.wait(), timeout=1.0)
            await asyncio.wait_for(opened_started.wait(), timeout=1.0)

            tracked_connection = client._connection or client._starting_connection  # type: ignore[attr-defined]
            assert tracked_connection is not None
            assert tracked_connection.state == ConnectionState.CONNECTED
            assert bytes_seen.is_set() is False

            await asyncio.wait_for(client.stop(), timeout=1.0)
            await asyncio.wait_for(server_saw_eof.wait(), timeout=1.0)

            assert opened_cancelled.is_set()
            assert closed_seen.is_set()
            assert bytes_seen.is_set() is False
            assert client.connection is None
            assert client._connection is None  # type: ignore[attr-defined]
            assert client._starting_connection is None  # type: ignore[attr-defined]
            assert client.lifecycle_state == ComponentLifecycleState.STOPPED
        finally:
            release_opened.set()
            await client.stop()


@pytest.mark.asyncio
async def test_client_supervisor_cancellation_during_connection_start_closes_partial_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_connection_opened = asyncio.Event()
    server_saw_eof = asyncio.Event()
    connection_start_called = asyncio.Event()

    async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        server_connection_opened.set()
        try:
            if await reader.read() == b"":
                server_saw_eof.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(server.start_serving(), timeout=1.0)

        async def _cancel_connection_start(self: AsyncioTcpConnection) -> None:
            connection_start_called.set()
            raise asyncio.CancelledError

        monkeypatch.setattr(AsyncioTcpConnection, "start", _cancel_connection_start)

        class _NoopHandler:
            async def on_event(self, event) -> None:
                return None

        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
                event_delivery=EventDeliverySettings(
                    dispatch_mode=EventDispatchMode.INLINE,
                    handler_failure_policy=EventHandlerFailurePolicy.LOG_ONLY,
                ),
            ),
            event_handler=_NoopHandler(),
        )

        await client.start()
        await asyncio.wait_for(connection_start_called.wait(), timeout=1.0)
        await asyncio.wait_for(server_connection_opened.wait(), timeout=1.0)
        await asyncio.wait_for(server_saw_eof.wait(), timeout=1.0)

        async def _wait_until_stopped() -> None:
            while client.lifecycle_state != tcp_client_module.ComponentLifecycleState.STOPPED:
                await asyncio.sleep(0)

        await asyncio.wait_for(_wait_until_stopped(), timeout=1.0)
        await client.stop()
        await asyncio.sleep(0)

        leaked_read_loops = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().endswith("-read-loop")
            and task.get_name().startswith(f"tcp/client/127.0.0.1/{port}/connection")
            and not task.done()
        ]
        assert leaked_read_loops == []
