"""
Contract tests for AsyncioTcpServer.

This module covers server-level behavior that user code depends on: broadcast
semantics, lifecycle ordering, startup rollback, wait-until-running behavior,
shutdown races, per-connection teardown, and heartbeat-sender management.
Several tests patch internal state to isolate rare but critical failure paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

import pytest

from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_events import ConnectionRejectedEvent
from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.api.errors import HeartbeatConfigurationError
from aionetx.api.heartbeat import HeartbeatRequest
from aionetx.api.heartbeat import HeartbeatResult
from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api.event_delivery_settings import (
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
)
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.implementations.asyncio_impl.asyncio_heartbeat_sender import AsyncioHeartbeatSender
from aionetx.implementations.asyncio_impl._tcp_server_helpers import handle_accepted_client
from aionetx.implementations.asyncio_impl.asyncio_tcp_connection import AsyncioTcpConnection
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher
from tests.helpers import wait_for_condition


def _unused_tcp_port() -> int:
    """Reserve an ephemeral localhost port for a test that needs a unique bind target."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_timeout_error(error: BaseException) -> bool:
    """Accept timeout classes used by supported Python asyncio versions."""
    return isinstance(error, (TimeoutError, asyncio.TimeoutError))


class _GoodConnection:
    """Connection double that records sent payloads and supports normal close."""

    def __init__(self, connection_id: str) -> None:
        self.connection_id = connection_id
        self.role = ConnectionRole.SERVER
        self.state = ConnectionState.CONNECTED
        self.metadata = ConnectionMetadata(connection_id=connection_id, role=ConnectionRole.SERVER)
        self.sent: list[bytes] = []

    @property
    def is_connected(self) -> bool:
        return self.state == ConnectionState.CONNECTED

    async def send(self, data: bytes | bytearray | memoryview) -> None:
        self.sent.append(bytes(data))

    async def close(self) -> None:
        self.state = ConnectionState.CLOSED


class _FailingConnection(_GoodConnection):
    """Connection double whose send path always fails."""

    async def send(self, data: bytes) -> None:
        raise RuntimeError(f"send-failed:{self.connection_id}")


class _FailingTrackedConnection(_FailingConnection):
    """Failing sender that records whether broadcast cleanup closed it."""

    def __init__(self, connection_id: str) -> None:
        super().__init__(connection_id=connection_id)
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        await super().close()


class _FakeWriter:
    """StreamWriter-shaped test double for accepted-connection helper tests."""

    def __init__(self) -> None:
        self.closed = False

    def get_extra_info(self, name: str, default=None):
        if name == "peername":
            return ("127.0.0.1", 54321)
        if name == "sockname":
            return ("127.0.0.1", 12345)
        return default

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.closed = True


class _BlockingConnection(_GoodConnection):
    """Connection double whose send path can be paused to model a slow client."""

    def __init__(self, connection_id: str) -> None:
        super().__init__(connection_id=connection_id)
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()
        self.close_calls = 0

    async def send(self, data: bytes | bytearray | memoryview) -> None:
        del data
        self.send_started.set()
        await self.release_send.wait()

    async def close(self) -> None:
        self.close_calls += 1
        self.state = ConnectionState.CLOSED
        self.release_send.set()


class _NoopHeartbeatProvider:
    """Heartbeat provider stub that never requests a heartbeat send."""

    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        del request
        return HeartbeatResult(should_send=False)


class _TeardownConnection(_GoodConnection):
    """Connection double that can fail during close while tracking attempts."""

    def __init__(self, connection_id: str, *, fail_on_close: bool) -> None:
        super().__init__(connection_id=connection_id)
        self.fail_on_close = fail_on_close
        self.close_attempts = 0

    async def close(self) -> None:
        self.close_attempts += 1
        if self.fail_on_close:
            raise RuntimeError(f"close-failed:{self.connection_id}")
        await super().close()


class _BlockingCloseConnection(_GoodConnection):
    """Connection double whose close can be held to test stop convergence."""

    def __init__(self, connection_id: str) -> None:
        super().__init__(connection_id=connection_id)
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_attempts = 0

    async def close(self) -> None:
        self.close_attempts += 1
        self.close_started.set()
        await self.release_close.wait()
        await super().close()


class _TeardownSender:
    """Heartbeat-sender double that can fail during stop for teardown tests."""

    def __init__(self, connection_id: str, *, fail_on_stop: bool) -> None:
        self.connection_id = connection_id
        self.fail_on_stop = fail_on_stop
        self.stop_attempts = 0

    async def stop(self) -> None:
        self.stop_attempts += 1
        if self.fail_on_stop:
            raise RuntimeError(f"stop-failed:{self.connection_id}")


# Broadcast semantics and heartbeat preconditions.
@pytest.mark.asyncio
async def test_server_broadcast_continues_when_one_connection_send_fails(
    recording_event_handler,
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=12345, max_connections=64),
        event_handler=recording_event_handler,
    )
    good = _GoodConnection(connection_id="server:good:1")
    bad = _FailingConnection(connection_id="server:bad:1")
    server._connections = {  # type: ignore[attr-defined]
        bad.connection_id: bad,  # type: ignore[dict-item]
        good.connection_id: good,  # type: ignore[dict-item]
    }

    await server.broadcast(bytearray(b"broadcast-test"))

    assert good.sent == [b"broadcast-test"]
    assert recording_event_handler.error_events
    assert isinstance(recording_event_handler.error_events[-1].error, RuntimeError)
    assert recording_event_handler.error_events[-1].resource_id == "tcp/server/127.0.0.1/12345"


@pytest.mark.asyncio
async def test_server_broadcast_cleanup_survives_error_event_handler_failure() -> None:
    class RaiseOnNetworkError:
        async def on_event(self, event) -> None:
            if isinstance(event, NetworkErrorEvent):
                raise RuntimeError("error-event-handler-failed")

    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=12345,
            max_connections=64,
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.INLINE,
                handler_failure_policy=EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE,
            ),
        ),
        event_handler=RaiseOnNetworkError(),
    )
    failing = _FailingTrackedConnection(connection_id="server:bad:1")
    server._connections = {failing.connection_id: failing}  # type: ignore[attr-defined,dict-item]

    await server.broadcast(b"fanout")

    assert failing.close_calls == 1
    assert failing.state == ConnectionState.CLOSED


@pytest.mark.asyncio
async def test_server_broadcast_no_connections_is_noop(recording_event_handler) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=12345, max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.broadcast(b"noop")
    assert recording_event_handler.error_events == []


@pytest.mark.asyncio
async def test_server_broadcast_uses_bounded_concurrency_so_slow_client_does_not_block_fast_client(
    recording_event_handler,
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=12345,
            max_connections=64,
            broadcast_concurrency_limit=2,
        ),
        event_handler=recording_event_handler,
    )
    slow = _BlockingConnection(connection_id="server:slow:1")
    fast = _GoodConnection(connection_id="server:fast:1")
    server._connections = {  # type: ignore[attr-defined]
        slow.connection_id: slow,  # type: ignore[dict-item]
        fast.connection_id: fast,  # type: ignore[dict-item]
    }

    broadcast_task = asyncio.create_task(server.broadcast(b"fanout"))
    await asyncio.wait_for(slow.send_started.wait(), timeout=1.0)
    await wait_for_condition(
        lambda: fast.sent == [b"fanout"],
        timeout_seconds=1.0,
        interval_seconds=0.01,
    )

    slow.release_send.set()
    await asyncio.wait_for(broadcast_task, timeout=1.0)
    assert fast.sent == [b"fanout"]


@pytest.mark.asyncio
async def test_server_broadcast_timeout_closes_stalled_connection_and_emits_error(
    recording_event_handler,
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=12345,
            max_connections=64,
            broadcast_concurrency_limit=2,
            broadcast_send_timeout_seconds=0.01,
        ),
        event_handler=recording_event_handler,
    )
    stalled = _BlockingConnection(connection_id="server:stalled:1")
    server._connections = {  # type: ignore[attr-defined]
        stalled.connection_id: stalled,  # type: ignore[dict-item]
    }

    await server.broadcast(b"fanout")

    assert stalled.state == ConnectionState.CLOSED
    assert stalled.close_calls == 1
    assert recording_event_handler.error_events
    assert _is_timeout_error(recording_event_handler.error_events[-1].error)


@pytest.mark.asyncio
async def test_server_start_raises_when_heartbeat_enabled_without_provider(
    recording_event_handler,
) -> None:
    with pytest.raises(HeartbeatConfigurationError, match="heartbeat_provider"):
        AsyncioTcpServer(
            settings=TcpServerSettings(
                host="127.0.0.1",
                port=12345,
                max_connections=64,
                heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
            ),
            event_handler=recording_event_handler,
        )


# Lifecycle coordination for start/stop idempotency.
@pytest.mark.asyncio
async def test_server_concurrent_start_and_stop_are_safe(recording_event_handler) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=12348, max_connections=64),
        event_handler=recording_event_handler,
    )

    await asyncio.gather(server.start(), server.start())
    assert server.lifecycle_state == ComponentLifecycleState.RUNNING

    await asyncio.gather(server.stop(), server.stop())
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED


@pytest.mark.asyncio
async def test_server_concurrent_stop_emits_terminal_transitions_once(
    recording_event_handler,
) -> None:
    port = _unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )

    await server.start()
    await asyncio.gather(server.stop(), server.stop())

    component_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == f"tcp/server/127.0.0.1/{port}"
    ]
    assert (
        sum(1 for event in component_events if event.current == ComponentLifecycleState.STOPPING)
        == 1
    )
    assert (
        sum(1 for event in component_events if event.current == ComponentLifecycleState.STOPPED)
        == 1
    )


@pytest.mark.asyncio
async def test_server_concurrent_stop_waits_for_active_teardown(
    recording_event_handler,
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=12348, max_connections=64),
        event_handler=recording_event_handler,
    )
    blocking = _BlockingCloseConnection("server:blocking-close:1")
    server._lifecycle_state = ComponentLifecycleState.RUNNING  # type: ignore[attr-defined]
    server._connections = {blocking.connection_id: blocking}  # type: ignore[attr-defined,dict-item]

    first_stop = asyncio.create_task(server.stop())
    await asyncio.wait_for(blocking.close_started.wait(), timeout=1.0)
    second_stop = asyncio.create_task(server.stop())
    await asyncio.sleep(0)

    assert second_stop.done() is False

    blocking.release_close.set()
    await asyncio.gather(first_stop, second_stop)

    assert blocking.close_attempts == 1
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED


@pytest.mark.asyncio
async def test_server_lifecycle_events_preserve_start_stop_order(recording_event_handler) -> None:
    port = _unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )

    await server.start()
    await server.stop()

    component_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == f"tcp/server/127.0.0.1/{port}"
    ]
    assert [event.current for event in component_events] == [
        ComponentLifecycleState.STARTING,
        ComponentLifecycleState.RUNNING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    ]


@pytest.mark.asyncio
async def test_server_start_rolls_back_state_and_allows_retry(
    recording_event_handler, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=22345, max_connections=64),
        event_handler=recording_event_handler,
    )
    should_fail = True
    original_start_server = asyncio.start_server

    async def flaky_start_server(*args, **kwargs):
        nonlocal should_fail
        if should_fail:
            should_fail = False
            raise OSError("start-failed")
        return await original_start_server(*args, **kwargs)

    monkeypatch.setattr(asyncio, "start_server", flaky_start_server)

    with pytest.raises(OSError, match="start-failed"):
        await server.start()
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED
    assert server._event_dispatcher.is_running is False  # type: ignore[attr-defined]

    await server.start()
    assert server.lifecycle_state == ComponentLifecycleState.RUNNING
    await server.stop()


# Wait helpers and bootstrap state reporting.
@pytest.mark.asyncio
async def test_server_wait_until_running_succeeds(recording_event_handler) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=_unused_tcp_port(), max_connections=64),
        event_handler=recording_event_handler,
    )
    start_task = asyncio.create_task(server.start())
    await asyncio.wait_for(server.wait_until_running(timeout_seconds=1.0), timeout=1.0)
    await start_task
    assert server.lifecycle_state == ComponentLifecycleState.RUNNING
    await server.stop()


@pytest.mark.asyncio
async def test_server_wait_until_running_times_out(recording_event_handler) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=_unused_tcp_port(), max_connections=64),
        event_handler=recording_event_handler,
    )

    with pytest.raises(asyncio.TimeoutError):
        await server.wait_until_running(timeout_seconds=0.01, poll_interval_seconds=0.005)


@pytest.mark.asyncio
async def test_server_wait_until_running_reports_stopped_after_failed_start(
    recording_event_handler, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=_unused_tcp_port(), max_connections=64),
        event_handler=recording_event_handler,
    )

    async def failing_start_server(*args, **kwargs):
        del args, kwargs
        raise OSError("bind-failed")

    monkeypatch.setattr(asyncio, "start_server", failing_start_server)

    with pytest.raises(OSError):
        await server.start()

    with pytest.raises(ConnectionError, match="is stopped"):
        await server.wait_until_running(timeout_seconds=0.1, poll_interval_seconds=0.01)


class _FailOnStartingLifecycle:
    """Handler that fails on STARTING to probe inline deadlock safety."""

    async def on_event(self, event) -> None:
        if (
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current == ComponentLifecycleState.STARTING
        ):
            raise RuntimeError("lifecycle-handler-failed")


class _FailOnStoppingLifecycle:
    """Handler that fails on STOPPING to probe cleanup-before-propagation."""

    async def on_event(self, event) -> None:
        if (
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current == ComponentLifecycleState.STOPPING
        ):
            raise RuntimeError("stop-publication-failed")


# Start/stop races and inline STOP_COMPONENT failure handling.
@pytest.mark.asyncio
async def test_server_start_inline_stop_component_handler_failure_does_not_deadlock() -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=_unused_tcp_port(),
            max_connections=64,
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.INLINE,
                handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
            ),
        ),
        event_handler=_FailOnStartingLifecycle(),
    )

    await asyncio.wait_for(server.start(), timeout=1.0)
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED


@pytest.mark.asyncio
async def test_server_stop_lifecycle_publication_failure_still_releases_listener() -> None:
    port = _unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=64,
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.INLINE,
                handler_failure_policy=EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE,
            ),
        ),
        event_handler=_FailOnStoppingLifecycle(),
    )

    await server.start()
    with pytest.raises(RuntimeError, match="stop-publication-failed"):
        await server.stop()

    assert server.lifecycle_state == ComponentLifecycleState.STOPPED
    assert server._event_dispatcher.is_running is False  # type: ignore[attr-defined]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


@pytest.mark.asyncio
async def test_server_stop_component_reentrant_stop_during_stopping_does_not_deadlock() -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=_unused_tcp_port(),
            max_connections=64,
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.BACKGROUND,
                handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
            ),
        ),
        event_handler=_FailOnStoppingLifecycle(),
    )

    await server.start()
    await asyncio.wait_for(server.stop(), timeout=1.0)

    assert server.lifecycle_state == ComponentLifecycleState.STOPPED
    assert server._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_server_rejects_illegal_lifecycle_transition(recording_event_handler) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=_unused_tcp_port(), max_connections=64),
        event_handler=recording_event_handler,
    )

    with pytest.raises(RuntimeError, match="Illegal lifecycle transition"):
        await server._set_lifecycle_state(ComponentLifecycleState.RUNNING)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_server_stop_during_start_is_safe(
    recording_event_handler, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=_unused_tcp_port(), max_connections=64),
        event_handler=recording_event_handler,
    )

    original_start_server = asyncio.start_server
    gate = asyncio.Event()
    start_server_entered = asyncio.Event()

    async def delayed_start_server(*args, **kwargs):
        start_server_entered.set()
        await gate.wait()
        return await original_start_server(*args, **kwargs)

    monkeypatch.setattr(asyncio, "start_server", delayed_start_server)

    start_task = asyncio.create_task(server.start())
    await asyncio.wait_for(start_server_entered.wait(), timeout=1.0)
    stop_task = asyncio.create_task(server.stop())
    gate.set()
    await asyncio.gather(start_task, stop_task)
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED


@pytest.mark.asyncio
async def test_server_stop_race_does_not_leave_post_stop_connections(
    recording_event_handler,
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=_unused_tcp_port(), max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()
    connect_started = asyncio.Event()

    async def _open_client() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        connect_started.set()
        return await asyncio.open_connection("127.0.0.1", server._settings.port)  # type: ignore[attr-defined]

    client_tasks = [asyncio.create_task(_open_client()) for _ in range(40)]
    await asyncio.wait_for(connect_started.wait(), timeout=1.0)
    await server.stop()
    clients = await asyncio.gather(*client_tasks, return_exceptions=True)

    for client in clients:
        if isinstance(client, Exception):
            continue
        _reader, writer = client
        writer.write(b"ping")
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    assert server.connections == ()
    assert server._connections == {}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_server_stop_race_does_not_leave_orphaned_heartbeat_senders(
    recording_event_handler,
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=_unused_tcp_port(),
            max_connections=64,
            heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
        ),
        event_handler=recording_event_handler,
        heartbeat_provider=_NoopHeartbeatProvider(),
    )
    await server.start()

    clients = [await asyncio.open_connection("127.0.0.1", server._settings.port) for _ in range(8)]  # type: ignore[attr-defined]
    await wait_for_condition(
        lambda: len(server._heartbeat_senders) == len(clients),  # type: ignore[attr-defined]
        timeout_seconds=1.0,
        interval_seconds=0.01,
    )
    await asyncio.wait_for(server.stop(), timeout=5.0)
    await wait_for_condition(
        lambda: not server._heartbeat_senders and not server._connections,  # type: ignore[attr-defined]
        timeout_seconds=1.0,
        interval_seconds=0.01,
    )

    assert server._heartbeat_senders == {}  # type: ignore[attr-defined]
    assert server._connections == {}  # type: ignore[attr-defined]
    heartbeat_tasks = [
        task
        for task in asyncio.all_tasks()
        if "heartbeat-sender" in task.get_name() and not task.done()
    ]
    assert heartbeat_tasks == []

    for _reader, writer in clients:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_server_rejects_connections_above_max_connections_and_emits_event(
    recording_event_handler,
) -> None:
    port = _unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=1),
        event_handler=recording_event_handler,
    )
    await server.start()

    first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
    second_reader, second_writer = await asyncio.open_connection("127.0.0.1", port)
    await wait_for_condition(
        lambda: len(server.connections) == 1 and len(recording_event_handler.rejected_events) == 1,
        timeout_seconds=1.0,
        interval_seconds=0.01,
    )

    rejected_event = recording_event_handler.rejected_events[0]
    assert isinstance(rejected_event, ConnectionRejectedEvent)
    assert rejected_event.resource_id == f"tcp/server/127.0.0.1/{port}"
    assert rejected_event.reason == "max_connections_reached"
    assert len(server.connections) == 1

    first_writer.close()
    second_writer.close()
    with contextlib.suppress(Exception):
        await first_writer.wait_closed()
    with contextlib.suppress(Exception):
        await second_writer.wait_closed()
    del first_reader, second_reader
    await server.stop()


@pytest.mark.asyncio
async def test_server_idle_timeout_closes_inactive_connection_and_emits_timeout_error(
    recording_event_handler,
) -> None:
    port = _unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=1,
            connection_idle_timeout_seconds=0.05,
        ),
        event_handler=recording_event_handler,
    )
    await server.start()

    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await wait_for_condition(
        lambda: bool(server.connections),
        timeout_seconds=1.0,
        interval_seconds=0.01,
    )
    await wait_for_condition(
        lambda: not server.connections and bool(recording_event_handler.error_events),
        timeout_seconds=1.0,
        interval_seconds=0.01,
    )

    assert _is_timeout_error(recording_event_handler.error_events[-1].error)

    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_server_connection_start_failure_does_not_leave_registered_connection(
    recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=_unused_tcp_port(), max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()

    async def failing_start(self) -> None:
        raise RuntimeError(f"start-failed:{self.connection_id}")

    monkeypatch.setattr(AsyncioTcpConnection, "start", failing_start)

    _reader, writer = await asyncio.open_connection("127.0.0.1", server._settings.port)  # type: ignore[attr-defined]
    await wait_for_condition(
        lambda: not server._connections and bool(recording_event_handler.error_events),  # type: ignore[attr-defined]
        timeout_seconds=1.0,
        interval_seconds=0.01,
    )

    assert server._connections == {}  # type: ignore[attr-defined]
    assert recording_event_handler.error_events
    assert "start-failed:" in str(recording_event_handler.error_events[-1].error)

    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_accepted_connection_start_cancelled_error_cleans_registered_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: dict[str, AsyncioTcpConnection] = {}
    writer = _FakeWriter()
    dispatcher = AsyncioEventDispatcher(
        _FailOnStartingLifecycle(),
        EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        logging.getLogger("test"),
    )

    async def cancelled_start(self) -> None:
        raise asyncio.CancelledError

    def on_closed(connection: AsyncioTcpConnection) -> None:
        connections.pop(connection.connection_id, None)

    monkeypatch.setattr(AsyncioTcpConnection, "start", cancelled_start)

    with pytest.raises(asyncio.CancelledError):
        await handle_accepted_client(
            reader=asyncio.StreamReader(),
            writer=writer,  # type: ignore[arg-type]
            state_lock=asyncio.Lock(),
            get_lifecycle_state=lambda: ComponentLifecycleState.RUNNING,
            get_connection_id=lambda: "server:accepted:1",
            connections=connections,
            event_dispatcher=dispatcher,
            max_connections=64,
            receive_buffer_size=4096,
            idle_timeout_seconds=None,
            on_closed_callback=on_closed,
            heartbeat_settings=TcpHeartbeatSettings(enabled=False),
            heartbeat_provider=None,
            heartbeat_senders={},
            component_id="tcp/server/127.0.0.1/12345",
            logger=logging.getLogger("test"),
        )

    assert connections == {}
    assert writer.closed is True


@pytest.mark.asyncio
async def test_accepted_connection_error_event_failure_still_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: dict[str, AsyncioTcpConnection] = {}
    writer = _FakeWriter()
    dispatcher = AsyncioEventDispatcher(
        _FailOnStartingLifecycle(),
        EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        logging.getLogger("test"),
    )

    async def failing_start(self) -> None:
        raise RuntimeError("start-failed")

    async def failing_emit(_event) -> None:
        raise RuntimeError("error-event-failed")

    def on_closed(connection: AsyncioTcpConnection) -> None:
        connections.pop(connection.connection_id, None)

    monkeypatch.setattr(AsyncioTcpConnection, "start", failing_start)
    monkeypatch.setattr(dispatcher, "emit", failing_emit)

    await handle_accepted_client(
        reader=asyncio.StreamReader(),
        writer=writer,  # type: ignore[arg-type]
        state_lock=asyncio.Lock(),
        get_lifecycle_state=lambda: ComponentLifecycleState.RUNNING,
        get_connection_id=lambda: "server:accepted:2",
        connections=connections,
        event_dispatcher=dispatcher,
        max_connections=64,
        receive_buffer_size=4096,
        idle_timeout_seconds=None,
        on_closed_callback=on_closed,
        heartbeat_settings=TcpHeartbeatSettings(enabled=False),
        heartbeat_provider=None,
        heartbeat_senders={},
        component_id="tcp/server/127.0.0.1/12345",
        logger=logging.getLogger("test"),
    )

    assert connections == {}
    assert writer.closed is True


# Best-effort teardown and heartbeat cleanup on shutdown.
@pytest.mark.asyncio
async def test_server_stop_teardown_is_best_effort_and_emits_error_events(
    recording_event_handler,
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=12349, max_connections=64),
        event_handler=recording_event_handler,
    )
    ok_connection = _TeardownConnection("server:ok-close:1", fail_on_close=False)
    failing_connection = _TeardownConnection("server:bad-close:1", fail_on_close=True)
    ok_sender = _TeardownSender("server:ok-sender:1", fail_on_stop=False)
    failing_sender = _TeardownSender("server:bad-sender:1", fail_on_stop=True)
    server._connections = {  # type: ignore[attr-defined]
        ok_connection.connection_id: ok_connection,  # type: ignore[dict-item]
        failing_connection.connection_id: failing_connection,  # type: ignore[dict-item]
    }
    server._heartbeat_senders = {  # type: ignore[attr-defined]
        ok_sender.connection_id: ok_sender,  # type: ignore[dict-item]
        failing_sender.connection_id: failing_sender,  # type: ignore[dict-item]
    }

    await server.stop()

    assert ok_connection.close_attempts == 1
    assert failing_connection.close_attempts == 1
    assert ok_sender.stop_attempts == 1
    assert failing_sender.stop_attempts == 1
    observed_errors = [str(event.error) for event in recording_event_handler.error_events]
    assert "close-failed:server:bad-close:1" in observed_errors
    assert "stop-failed:server:bad-sender:1" in observed_errors


@pytest.mark.asyncio
async def test_server_stop_teardown_error_reporting_is_best_effort(
    recording_event_handler, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=12350, max_connections=64),
        event_handler=recording_event_handler,
    )
    ok_connection = _TeardownConnection("server:ok-close:2", fail_on_close=False)
    failing_connection = _TeardownConnection("server:bad-close:2", fail_on_close=True)
    server._connections = {  # type: ignore[attr-defined]
        ok_connection.connection_id: ok_connection,  # type: ignore[dict-item]
        failing_connection.connection_id: failing_connection,  # type: ignore[dict-item]
    }

    async def failing_emit(_event) -> None:
        raise RuntimeError("emit-failed-during-report")

    monkeypatch.setattr(server._event_dispatcher, "emit", failing_emit)  # type: ignore[attr-defined]

    await server.stop()

    assert ok_connection.close_attempts == 1
    assert failing_connection.close_attempts == 1


@pytest.mark.asyncio
async def test_server_heartbeat_start_failure_rolls_back_sender_registration(
    recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=_unused_tcp_port(),
            max_connections=64,
            heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
        ),
        event_handler=recording_event_handler,
        heartbeat_provider=_NoopHeartbeatProvider(),
    )
    await server.start()

    async def failing_heartbeat_start(self) -> None:
        raise RuntimeError("heartbeat-start-failed")

    monkeypatch.setattr(AsyncioHeartbeatSender, "start", failing_heartbeat_start)

    _reader, writer = await asyncio.open_connection("127.0.0.1", server._settings.port)  # type: ignore[attr-defined]
    await wait_for_condition(
        lambda: (
            not server._heartbeat_senders  # type: ignore[attr-defined]
            and not server._connections  # type: ignore[attr-defined]
            and bool(recording_event_handler.error_events)
        ),
        timeout_seconds=1.0,
        interval_seconds=0.01,
    )

    assert server._heartbeat_senders == {}  # type: ignore[attr-defined]
    assert server._connections == {}  # type: ignore[attr-defined]
    heartbeat_tasks = [
        task
        for task in asyncio.all_tasks()
        if "heartbeat-sender" in task.get_name() and not task.done()
    ]
    assert heartbeat_tasks == []
    assert recording_event_handler.error_events
    assert "heartbeat-start-failed" in str(recording_event_handler.error_events[-1].error)

    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    await server.stop()
