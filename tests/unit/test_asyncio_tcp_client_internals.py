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

from aionetx.api.event_delivery_settings import (
    EventBackpressurePolicy,
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
)
from aionetx.api.error_policy import ErrorPolicy
from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_events import ConnectionOpenedEvent
from aionetx.api.connection_lifecycle import ConnectionRole
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
    await stop_task

    assert should_continue is False


# STOP_COMPONENT ordering when handler failures trigger client shutdown.
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
