"""
Integration tests for concurrency scenarios that are easy to miss.

The module covers concurrent send() on one live connection, UDP receiver
restartability, and end-to-end STOP_COMPONENT behavior with BACKGROUND
dispatch. These are coordination-heavy paths where failures usually appear
only under real scheduling pressure rather than in narrow unit tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.connection_events import ConnectionClosedEvent
from aionetx.api.connection_events import ConnectionOpenedEvent
from aionetx.api.event_delivery_settings import (
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
)
from aionetx.api.network_event import NetworkEvent
from aionetx.api.reconnect_events import ReconnectAttemptStartedEvent
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.api.udp import UdpReceiverSettings, UdpSenderSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver
from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender
from tests.helpers import wait_for_condition


def _unused_port(kind: socket.SocketKind = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _NoopHandler:
    async def on_event(self, event: NetworkEvent) -> None:
        return None


# ---------------------------------------------------------------------------
# Scenario 1 — Concurrent send() from multiple coroutines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_send_on_connected_connection() -> None:
    """
    Ten coroutines calling send() on the same connection concurrently must all succeed.

    Rationale: asyncio writers are not thread-safe, but they are coroutine-safe
    when drained properly.  This test exercises the send path under concurrent
    scheduling pressure to confirm no silent drops, exceptions, or ordering
    violations at the API level.
    """
    port = _unused_port()
    received_payloads: list[bytes] = []
    server_done = asyncio.Event()

    async def _server_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                received_payloads.append(chunk)
        finally:
            server_done.set()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    server_socket = await asyncio.start_server(_server_handler, "127.0.0.1", port)
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=_NoopHandler(),
    )
    try:
        await client.start()
        connection = await client.wait_until_connected(timeout_seconds=3.0)

        # Fire 10 concurrent sends without awaiting between them.
        payloads = [f"msg-{i:02d}".encode() for i in range(10)]
        await asyncio.gather(*[connection.send(p) for p in payloads])

        # Stop the client: this closes the TCP connection, which delivers an
        # EOF to the server reader loop — causing server_done to be set.
        await client.stop()

        # Wait for the server's reader loop to drain and exit (EOF path).
        # This guarantees all in-flight bytes have been appended to received_payloads
        # before we assert, avoiding a timing-dependent race.
        try:
            await asyncio.wait_for(server_done.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("Server did not finish reading within 5 s after client closed.")
    finally:
        server_socket.close()
        await server_socket.wait_closed()

    # All sends completed without exception — the key invariant.
    # We also verify the server received data (i.e., sends were not silently dropped).
    total_received = sum(len(chunk) for chunk in received_payloads)
    expected_total = sum(len(p) for p in payloads)
    assert total_received == expected_total, (
        f"Server received {total_received} bytes; expected {expected_total}. "
        "Some sends were silently dropped."
    )


# ---------------------------------------------------------------------------
# Scenario 2 — UDP receiver stop → restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_udp_receiver_restarts_cleanly_after_stop() -> None:
    """
    UDP receiver can be stopped and restarted without leaking resources.

    Validates that the second start() binds a fresh socket and delivers
    datagrams normally — confirming full state reset between lifecycle cycles.
    """
    port = _unused_port(socket.SOCK_DGRAM)
    received: list[bytes] = []

    class _Collector:
        async def on_event(self, event: NetworkEvent) -> None:
            from aionetx.api.bytes_received_event import BytesReceivedEvent

            if isinstance(event, BytesReceivedEvent):
                received.append(event.data)

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=port),
        event_handler=_Collector(),
    )
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port),
    )

    # First lifecycle — start, send, stop.
    await receiver.start()
    assert receiver.lifecycle_state == ComponentLifecycleState.RUNNING
    await sender.send(b"before-restart")
    await wait_for_condition(lambda: b"before-restart" in received, timeout_seconds=2.0)
    await receiver.stop()
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED

    # Second lifecycle — restart, send again, stop.
    await receiver.start()
    assert receiver.lifecycle_state == ComponentLifecycleState.RUNNING
    await sender.send(b"after-restart")
    await wait_for_condition(lambda: b"after-restart" in received, timeout_seconds=2.0)
    await receiver.stop()
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED

    await sender.stop()
    assert b"before-restart" in received
    assert b"after-restart" in received


# ---------------------------------------------------------------------------
# Scenario 3 — STOP_COMPONENT + BACKGROUND: component-level end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stop_component_policy_background_mode_stops_client_on_handler_failure() -> None:
    """
    STOP_COMPONENT + BACKGROUND: handler exception causes the TCP client to stop.

    This is an end-to-end scenario: the handler raises on the first
    ConnectionOpenedEvent, the BACKGROUND dispatcher calls stop_component_callback,
    and the component must fully transition to STOPPED.

    Existing unit tests verify the dispatcher mechanics in isolation.  This
    test confirms the wiring: stop_component_callback → client.stop() →
    lifecycle_state == STOPPED.
    """
    port = _unused_port()

    class _FailOnOpen:
        def __init__(self) -> None:
            self.called = False

        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, ConnectionOpenedEvent) and not self.called:
                self.called = True
                raise RuntimeError("Simulated handler failure to trigger STOP_COMPONENT.")

    handler = _FailOnOpen()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
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
    await server.start()
    baseline_tasks = {
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }
    try:
        await client.start()
        await wait_for_condition(
            lambda: client.lifecycle_state == ComponentLifecycleState.STOPPED,
            timeout_seconds=3.0,
        )
        await wait_for_condition(
            lambda: (
                not [
                    task
                    for task in asyncio.all_tasks()
                    if (
                        task is not asyncio.current_task()
                        and not task.done()
                        and task not in baseline_tasks
                        and (
                            task.get_name() == "tcp-client-supervisor"
                            or task.get_name() == "event-dispatcher"
                            or "tcp/client/" in task.get_name()
                        )
                    )
                ]
            ),
            timeout_seconds=3.0,
        )
        assert client.lifecycle_state == ComponentLifecycleState.STOPPED, (
            "Client must reach STOPPED after handler failure with STOP_COMPONENT policy."
        )
        assert handler.called, "Handler must have been called at least once."
    finally:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(client.stop(), timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(server.stop(), timeout=1.0)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "dispatch_mode",
    [EventDispatchMode.INLINE, EventDispatchMode.BACKGROUND],
)
async def test_opened_handler_can_stop_client_without_overlapping_connection_events(
    dispatch_mode: EventDispatchMode,
) -> None:
    port = _unused_port()

    class _StopClientOnOpen:
        def __init__(self) -> None:
            self.client: AsyncioTcpClient | None = None
            self.opened_seen = asyncio.Event()
            self.opened_finished = asyncio.Event()
            self.closed_seen = asyncio.Event()
            self.stop_returned = asyncio.Event()
            self.stop_error: BaseException | None = None
            self.active_connection_handlers = 0
            self.max_active_connection_handlers = 0

        async def on_event(self, event: NetworkEvent) -> None:
            is_connection_event = isinstance(event, (ConnectionOpenedEvent, ConnectionClosedEvent))
            if is_connection_event:
                self.active_connection_handlers += 1
                self.max_active_connection_handlers = max(
                    self.max_active_connection_handlers,
                    self.active_connection_handlers,
                )
            try:
                if isinstance(event, ConnectionClosedEvent):
                    assert self.opened_finished.is_set()
                    self.closed_seen.set()
                if not isinstance(event, ConnectionOpenedEvent):
                    return
                self.opened_seen.set()
                if self.client is None:
                    raise AssertionError("client reference was not attached")
                await self.client.stop()
                assert not self.closed_seen.is_set()
                assert self.max_active_connection_handlers == 1
                assert self.client.lifecycle_state == ComponentLifecycleState.STOPPED
                assert self.client.connection is None
                assert self.client._starting_connection is None
            except (Exception, asyncio.CancelledError) as error:
                self.stop_error = error
            finally:
                if is_connection_event:
                    self.active_connection_handlers -= 1
                if isinstance(event, ConnectionOpenedEvent):
                    self.opened_finished.set()
                    self.stop_returned.set()

    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    handler = _StopClientOnOpen()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(dispatch_mode=dispatch_mode),
        ),
        event_handler=handler,
    )
    handler.client = client

    try:
        await server.start()
        await asyncio.wait_for(client.start(), timeout=3.0)
        await asyncio.wait_for(handler.opened_seen.wait(), timeout=3.0)
        await asyncio.wait_for(handler.stop_returned.wait(), timeout=3.0)
        await asyncio.wait_for(handler.closed_seen.wait(), timeout=3.0)
        await wait_for_condition(
            lambda: (
                client.lifecycle_state == ComponentLifecycleState.STOPPED
                and client.connection is None
            ),
            timeout_seconds=3.0,
        )
    finally:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(client.stop(), timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(server.stop(), timeout=1.0)

    assert handler.stop_error is None
    assert handler.opened_finished.is_set()
    assert handler.closed_seen.is_set()
    assert handler.max_active_connection_handlers == 1
    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert client.connection is None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "dispatch_mode",
    [EventDispatchMode.INLINE, EventDispatchMode.BACKGROUND],
)
async def test_client_opened_handler_can_observe_active_connection(
    dispatch_mode: EventDispatchMode,
) -> None:
    port = _unused_port()

    class _ObserveConnectionOnOpen:
        def __init__(self) -> None:
            self.client: AsyncioTcpClient | None = None
            self.opened_checked = asyncio.Event()
            self.error: BaseException | None = None

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            if self.client is None:
                raise AssertionError("client reference was not attached")
            try:
                connection = self.client.connection
                assert connection is not None
                observed = await self.client.wait_until_connected(timeout_seconds=1.0)
                assert observed is connection
            except (Exception, asyncio.CancelledError) as error:
                self.error = error
            finally:
                self.opened_checked.set()

    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    handler = _ObserveConnectionOnOpen()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(dispatch_mode=dispatch_mode),
        ),
        event_handler=handler,
    )
    handler.client = client

    try:
        await server.start()
        await asyncio.wait_for(client.start(), timeout=3.0)
        await asyncio.wait_for(handler.opened_checked.wait(), timeout=3.0)
    finally:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(client.stop(), timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(server.stop(), timeout=1.0)

    assert handler.error is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_external_client_stop_waits_for_handler_owned_deferred_close() -> None:
    port = _unused_port()

    class _StopClientOnOpenWithBlockedStopping:
        def __init__(self) -> None:
            self.client: AsyncioTcpClient | None = None
            self.opened_seen = asyncio.Event()
            self.opened_finished = asyncio.Event()
            self.closed_seen = asyncio.Event()
            self.stopping_seen = asyncio.Event()
            self.allow_stopping_to_finish = asyncio.Event()
            self.allow_opened_to_finish = asyncio.Event()
            self.handler_stop_returned = asyncio.Event()
            self.error: BaseException | None = None

        async def on_event(self, event: NetworkEvent) -> None:
            try:
                if isinstance(event, ConnectionClosedEvent):
                    assert self.opened_finished.is_set()
                    self.closed_seen.set()
                    return
                if (
                    isinstance(event, ComponentLifecycleChangedEvent)
                    and event.current == ComponentLifecycleState.STOPPING
                ):
                    assert self.opened_finished.is_set()
                    self.stopping_seen.set()
                    await self.allow_stopping_to_finish.wait()
                    return
                if not isinstance(event, ConnectionOpenedEvent):
                    return
                self.opened_seen.set()
                if self.client is None:
                    raise AssertionError("client reference was not attached")
                await self.client.stop()
                self.handler_stop_returned.set()
                await self.allow_opened_to_finish.wait()
            except (Exception, asyncio.CancelledError) as error:
                self.error = error
            finally:
                if isinstance(event, ConnectionOpenedEvent):
                    self.opened_finished.set()

    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    handler = _StopClientOnOpenWithBlockedStopping()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        ),
        event_handler=handler,
    )
    handler.client = client
    external_stop_task: asyncio.Task[None] | None = None

    try:
        await server.start()
        await asyncio.wait_for(client.start(), timeout=3.0)
        await asyncio.wait_for(handler.opened_seen.wait(), timeout=3.0)

        external_stop_task = asyncio.create_task(client.stop())
        await asyncio.wait_for(handler.handler_stop_returned.wait(), timeout=3.0)
        await asyncio.sleep(0)
        assert not external_stop_task.done()
        assert not handler.stopping_seen.is_set()

        handler.allow_opened_to_finish.set()
        await asyncio.wait_for(handler.stopping_seen.wait(), timeout=3.0)
        await asyncio.sleep(0)
        assert not external_stop_task.done()

        handler.allow_stopping_to_finish.set()
        await asyncio.wait_for(handler.closed_seen.wait(), timeout=3.0)
        await asyncio.wait_for(external_stop_task, timeout=3.0)
    finally:
        handler.allow_stopping_to_finish.set()
        handler.allow_opened_to_finish.set()
        if external_stop_task is not None and not external_stop_task.done():
            external_stop_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(external_stop_task, timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(client.stop(), timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(server.stop(), timeout=1.0)

    assert handler.error is None
    assert handler.closed_seen.is_set()
    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert client.connection is None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "dispatch_mode",
    [EventDispatchMode.INLINE, EventDispatchMode.BACKGROUND],
)
async def test_opened_handler_spawned_client_stop_task_does_not_overlap_connection_events(
    dispatch_mode: EventDispatchMode,
) -> None:
    port = _unused_port()

    class _SpawnStopTaskOnOpen:
        def __init__(self) -> None:
            self.client: AsyncioTcpClient | None = None
            self.opened_seen = asyncio.Event()
            self.opened_finished = asyncio.Event()
            self.closed_seen = asyncio.Event()
            self.stop_returned = asyncio.Event()
            self.stop_error: BaseException | None = None
            self.active_connection_handlers = 0
            self.max_active_connection_handlers = 0

        async def on_event(self, event: NetworkEvent) -> None:
            is_connection_event = isinstance(event, (ConnectionOpenedEvent, ConnectionClosedEvent))
            if is_connection_event:
                self.active_connection_handlers += 1
                self.max_active_connection_handlers = max(
                    self.max_active_connection_handlers,
                    self.active_connection_handlers,
                )
            try:
                if isinstance(event, ConnectionClosedEvent):
                    assert self.opened_finished.is_set()
                    self.closed_seen.set()
                if not isinstance(event, ConnectionOpenedEvent):
                    return
                self.opened_seen.set()
                if self.client is None:
                    raise AssertionError("client reference was not attached")
                stop_task = asyncio.create_task(self.client.stop())
                await stop_task
                assert not self.closed_seen.is_set()
                assert self.max_active_connection_handlers == 1
                assert self.client.lifecycle_state == ComponentLifecycleState.STOPPED
                assert self.client.connection is None
            except (Exception, asyncio.CancelledError) as error:
                self.stop_error = error
            finally:
                if is_connection_event:
                    self.active_connection_handlers -= 1
                if isinstance(event, ConnectionOpenedEvent):
                    self.opened_finished.set()
                    self.stop_returned.set()

    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    handler = _SpawnStopTaskOnOpen()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(dispatch_mode=dispatch_mode),
        ),
        event_handler=handler,
    )
    handler.client = client

    try:
        await server.start()
        await asyncio.wait_for(client.start(), timeout=3.0)
        await asyncio.wait_for(handler.opened_seen.wait(), timeout=3.0)
        await asyncio.wait_for(handler.stop_returned.wait(), timeout=3.0)
        await asyncio.wait_for(handler.closed_seen.wait(), timeout=3.0)
    finally:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(client.stop(), timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(server.stop(), timeout=1.0)

    assert handler.stop_error is None
    assert handler.max_active_connection_handlers == 1
    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert client.connection is None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "dispatch_mode",
    [EventDispatchMode.INLINE, EventDispatchMode.BACKGROUND],
)
async def test_opened_handler_can_stop_server_without_overlapping_connection_events(
    dispatch_mode: EventDispatchMode,
) -> None:
    port = _unused_port()

    class _StopServerOnOpen:
        def __init__(self) -> None:
            self.server: AsyncioTcpServer | None = None
            self.opened_seen = asyncio.Event()
            self.opened_finished = asyncio.Event()
            self.closed_seen = asyncio.Event()
            self.stop_returned = asyncio.Event()
            self.stop_error: BaseException | None = None
            self.active_connection_handlers = 0
            self.max_active_connection_handlers = 0

        async def on_event(self, event: NetworkEvent) -> None:
            is_connection_event = isinstance(event, (ConnectionOpenedEvent, ConnectionClosedEvent))
            if is_connection_event:
                self.active_connection_handlers += 1
                self.max_active_connection_handlers = max(
                    self.max_active_connection_handlers,
                    self.active_connection_handlers,
                )
            try:
                if isinstance(event, ConnectionClosedEvent):
                    assert self.opened_finished.is_set()
                    self.closed_seen.set()
                if not isinstance(event, ConnectionOpenedEvent):
                    return
                self.opened_seen.set()
                if self.server is None:
                    raise AssertionError("server reference was not attached")
                await self.server.stop()
                assert not self.closed_seen.is_set()
                assert self.max_active_connection_handlers == 1
                assert self.server.lifecycle_state == ComponentLifecycleState.STOPPED
                assert self.server.connections == ()
            except (Exception, asyncio.CancelledError) as error:
                self.stop_error = error
            finally:
                if is_connection_event:
                    self.active_connection_handlers -= 1
                if isinstance(event, ConnectionOpenedEvent):
                    self.opened_finished.set()
                    self.stop_returned.set()

    handler = _StopServerOnOpen()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=64,
            event_delivery=EventDeliverySettings(dispatch_mode=dispatch_mode),
        ),
        event_handler=handler,
    )
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=_NoopHandler(),
    )
    handler.server = server

    try:
        await server.start()
        await asyncio.wait_for(client.start(), timeout=3.0)
        await asyncio.wait_for(handler.opened_seen.wait(), timeout=3.0)
        await asyncio.wait_for(handler.stop_returned.wait(), timeout=3.0)
        await asyncio.wait_for(handler.closed_seen.wait(), timeout=3.0)
    finally:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(client.stop(), timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(server.stop(), timeout=1.0)

    assert handler.stop_error is None
    assert handler.opened_finished.is_set()
    assert handler.closed_seen.is_set()
    assert handler.max_active_connection_handlers == 1
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED
    assert server.connections == ()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "dispatch_mode",
    [EventDispatchMode.INLINE, EventDispatchMode.BACKGROUND],
)
async def test_opened_handler_spawned_server_stop_task_does_not_overlap_connection_events(
    dispatch_mode: EventDispatchMode,
) -> None:
    port = _unused_port()

    class _SpawnServerStopTaskOnOpen:
        def __init__(self) -> None:
            self.server: AsyncioTcpServer | None = None
            self.opened_seen = asyncio.Event()
            self.opened_finished = asyncio.Event()
            self.closed_seen = asyncio.Event()
            self.stop_returned = asyncio.Event()
            self.stop_error: BaseException | None = None
            self.active_connection_handlers = 0
            self.max_active_connection_handlers = 0

        async def on_event(self, event: NetworkEvent) -> None:
            is_connection_event = isinstance(event, (ConnectionOpenedEvent, ConnectionClosedEvent))
            if is_connection_event:
                self.active_connection_handlers += 1
                self.max_active_connection_handlers = max(
                    self.max_active_connection_handlers,
                    self.active_connection_handlers,
                )
            try:
                if isinstance(event, ConnectionClosedEvent):
                    assert self.opened_finished.is_set()
                    self.closed_seen.set()
                if not isinstance(event, ConnectionOpenedEvent):
                    return
                self.opened_seen.set()
                if self.server is None:
                    raise AssertionError("server reference was not attached")
                stop_task = asyncio.create_task(self.server.stop())
                await stop_task
                assert not self.closed_seen.is_set()
                assert self.max_active_connection_handlers == 1
                assert self.server.lifecycle_state == ComponentLifecycleState.STOPPED
                assert self.server.connections == ()
            except (Exception, asyncio.CancelledError) as error:
                self.stop_error = error
            finally:
                if is_connection_event:
                    self.active_connection_handlers -= 1
                if isinstance(event, ConnectionOpenedEvent):
                    self.opened_finished.set()
                    self.stop_returned.set()

    handler = _SpawnServerStopTaskOnOpen()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=64,
            event_delivery=EventDeliverySettings(dispatch_mode=dispatch_mode),
        ),
        event_handler=handler,
    )
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=_NoopHandler(),
    )
    handler.server = server

    try:
        await server.start()
        await asyncio.wait_for(client.start(), timeout=3.0)
        await asyncio.wait_for(handler.opened_seen.wait(), timeout=3.0)
        await asyncio.wait_for(handler.stop_returned.wait(), timeout=3.0)
        await asyncio.wait_for(handler.closed_seen.wait(), timeout=3.0)
    finally:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(client.stop(), timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(server.stop(), timeout=1.0)

    assert handler.stop_error is None
    assert handler.opened_finished.is_set()
    assert handler.closed_seen.is_set()
    assert handler.max_active_connection_handlers == 1
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED
    assert server.connections == ()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_external_server_stop_waits_for_handler_owned_deferred_close() -> None:
    port = _unused_port()

    class _StopServerOnOpenWithBlockedStopping:
        def __init__(self) -> None:
            self.server: AsyncioTcpServer | None = None
            self.opened_seen = asyncio.Event()
            self.opened_finished = asyncio.Event()
            self.closed_seen = asyncio.Event()
            self.stopping_seen = asyncio.Event()
            self.allow_stopping_to_finish = asyncio.Event()
            self.allow_opened_to_finish = asyncio.Event()
            self.handler_stop_returned = asyncio.Event()
            self.error: BaseException | None = None

        async def on_event(self, event: NetworkEvent) -> None:
            try:
                if isinstance(event, ConnectionClosedEvent):
                    assert self.opened_finished.is_set()
                    self.closed_seen.set()
                    return
                if (
                    isinstance(event, ComponentLifecycleChangedEvent)
                    and event.current == ComponentLifecycleState.STOPPING
                ):
                    assert self.opened_finished.is_set()
                    self.stopping_seen.set()
                    await self.allow_stopping_to_finish.wait()
                    return
                if not isinstance(event, ConnectionOpenedEvent):
                    return
                self.opened_seen.set()
                if self.server is None:
                    raise AssertionError("server reference was not attached")
                await self.server.stop()
                self.handler_stop_returned.set()
                await self.allow_opened_to_finish.wait()
            except (Exception, asyncio.CancelledError) as error:
                self.error = error
            finally:
                if isinstance(event, ConnectionOpenedEvent):
                    self.opened_finished.set()

    handler = _StopServerOnOpenWithBlockedStopping()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=64,
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        ),
        event_handler=handler,
    )
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=_NoopHandler(),
    )
    handler.server = server
    external_stop_task: asyncio.Task[None] | None = None

    try:
        await server.start()
        await asyncio.wait_for(client.start(), timeout=3.0)
        await asyncio.wait_for(handler.opened_seen.wait(), timeout=3.0)

        external_stop_task = asyncio.create_task(server.stop())
        await asyncio.wait_for(handler.handler_stop_returned.wait(), timeout=3.0)
        await asyncio.sleep(0)
        assert not external_stop_task.done()
        assert not handler.stopping_seen.is_set()

        handler.allow_opened_to_finish.set()
        await asyncio.wait_for(handler.stopping_seen.wait(), timeout=3.0)
        await asyncio.sleep(0)
        assert not external_stop_task.done()

        handler.allow_stopping_to_finish.set()
        await asyncio.wait_for(handler.closed_seen.wait(), timeout=3.0)
        await asyncio.wait_for(external_stop_task, timeout=3.0)
    finally:
        handler.allow_stopping_to_finish.set()
        handler.allow_opened_to_finish.set()
        if external_stop_task is not None and not external_stop_task.done():
            external_stop_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(external_stop_task, timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(client.stop(), timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(server.stop(), timeout=1.0)

    assert handler.error is None
    assert handler.closed_seen.is_set()
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED
    assert server.connections == ()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_inline_attempt_started_handler_can_stop_client_before_socket_open() -> None:
    opener_called = asyncio.Event()

    async def _open_connection(**_kwargs: object):
        opener_called.set()
        raise AssertionError("connection opener should not run after attempt-start stop")

    class _StopClientOnAttemptStarted:
        def __init__(self) -> None:
            self.client: AsyncioTcpClient | None = None
            self.attempt_seen = asyncio.Event()
            self.stop_returned = asyncio.Event()
            self.stop_error: BaseException | None = None

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ReconnectAttemptStartedEvent):
                return
            self.attempt_seen.set()
            if self.client is None:
                raise AssertionError("client reference was not attached")
            try:
                await self.client.stop()
            except (Exception, asyncio.CancelledError) as error:
                self.stop_error = error
            finally:
                self.stop_returned.set()

    handler = _StopClientOnAttemptStarted()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=1,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=handler,
        connection_opener=_open_connection,
    )
    handler.client = client

    try:
        await asyncio.wait_for(client.start(), timeout=3.0)
        await asyncio.wait_for(handler.attempt_seen.wait(), timeout=3.0)
        await asyncio.wait_for(handler.stop_returned.wait(), timeout=3.0)
    finally:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(client.stop(), timeout=1.0)

    assert handler.stop_error is None
    assert not opener_called.is_set()
    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert client.connection is None
    assert client._supervisor_task is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_handler_initiated_client_stop_publishes_connection_closed_event() -> None:
    port = _unused_port()
    stop_returned = asyncio.Event()

    class _StopOnBytes:
        def __init__(self) -> None:
            self.client: AsyncioTcpClient | None = None
            self.events: list[NetworkEvent] = []
            self._stopped = False

        async def on_event(self, event: NetworkEvent) -> None:
            self.events.append(event)
            if isinstance(event, BytesReceivedEvent) and not self._stopped:
                self._stopped = True
                assert self.client is not None
                await self.client.stop()
                stop_returned.set()

    client_handler = _StopOnBytes()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        ),
        event_handler=client_handler,
    )
    client_handler.client = client

    await server.start()
    try:
        await client.start()
        await client.wait_until_connected(timeout_seconds=3.0)
        await server.broadcast(b"stop-from-handler")
        await asyncio.wait_for(stop_returned.wait(), timeout=3.0)

        await wait_for_condition(
            lambda: client.lifecycle_state == ComponentLifecycleState.STOPPED,
            timeout_seconds=3.0,
        )
        await wait_for_condition(
            lambda: any(
                isinstance(event, ConnectionClosedEvent) for event in client_handler.events
            ),
            timeout_seconds=3.0,
        )
    finally:
        await client.stop()
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_inline_client_stop_from_bytes_handler_defers_terminal_events() -> None:
    port = _unused_port()
    stop_returned = asyncio.Event()

    class _StopOnBytesAndBlock:
        def __init__(self) -> None:
            self.client: AsyncioTcpClient | None = None
            self.bytes_finished = asyncio.Event()
            self.release_bytes = asyncio.Event()
            self.terminal_event_seen = asyncio.Event()
            self.error: BaseException | None = None
            self._stopped = False

        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, BytesReceivedEvent) and not self._stopped:
                self._stopped = True
                assert self.client is not None
                await self.client.stop()
                stop_returned.set()
                if self.terminal_event_seen.is_set():
                    self.error = AssertionError("terminal event was published before stop returned")
                    self.release_bytes.set()
                await self.release_bytes.wait()
                self.bytes_finished.set()
                return
            if isinstance(event, ConnectionClosedEvent) or (
                isinstance(event, ComponentLifecycleChangedEvent)
                and event.current
                in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
            ):
                if not self.bytes_finished.is_set():
                    self.error = AssertionError("terminal event re-entered bytes handler")
                    self.release_bytes.set()
                self.terminal_event_seen.set()

    handler = _StopOnBytesAndBlock()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=handler,
    )
    handler.client = client

    await server.start()
    try:
        await client.start()
        await client.wait_until_connected(timeout_seconds=3.0)
        await server.broadcast(b"inline-stop-from-handler")
        await asyncio.wait_for(stop_returned.wait(), timeout=3.0)

        assert not handler.terminal_event_seen.is_set()
        assert not handler.bytes_finished.is_set()

        handler.release_bytes.set()
        await wait_for_condition(
            lambda: client.lifecycle_state == ComponentLifecycleState.STOPPED,
            timeout_seconds=3.0,
        )
        await asyncio.wait_for(handler.terminal_event_seen.wait(), timeout=3.0)
    finally:
        handler.release_bytes.set()
        await client.stop()
        await server.stop()

    assert handler.error is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_inline_server_stop_from_bytes_handler_defers_terminal_events() -> None:
    port = _unused_port()
    stop_returned = asyncio.Event()

    class _StopOnBytesAndBlock:
        def __init__(self) -> None:
            self.server: AsyncioTcpServer | None = None
            self.bytes_finished = asyncio.Event()
            self.release_bytes = asyncio.Event()
            self.terminal_event_seen = asyncio.Event()
            self.error: BaseException | None = None
            self.bytes_payloads: list[bytes] = []
            self._stopped = False

        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, BytesReceivedEvent):
                self.bytes_payloads.append(event.data)
                if self._stopped:
                    self.error = AssertionError("bytes event delivered after server stop")
                    self.release_bytes.set()
                    return
                self._stopped = True
                assert self.server is not None
                await self.server.stop()
                stop_returned.set()
                if self.terminal_event_seen.is_set():
                    self.error = AssertionError("terminal event was published before stop returned")
                    self.release_bytes.set()
                await self.release_bytes.wait()
                self.bytes_finished.set()
                return
            if isinstance(event, ConnectionClosedEvent) or (
                isinstance(event, ComponentLifecycleChangedEvent)
                and event.current
                in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
            ):
                if not self.bytes_finished.is_set():
                    self.error = AssertionError("terminal event re-entered bytes handler")
                    self.release_bytes.set()
                self.terminal_event_seen.set()

    handler = _StopOnBytesAndBlock()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=64,
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=handler,
    )
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=_NoopHandler(),
    )
    handler.server = server

    await server.start()
    try:
        await client.start()
        connection = await client.wait_until_connected(timeout_seconds=3.0)
        await connection.send(b"first")
        await asyncio.wait_for(stop_returned.wait(), timeout=3.0)
        with contextlib.suppress(Exception):
            await connection.send(b"second")

        assert not handler.terminal_event_seen.is_set()
        assert not handler.bytes_finished.is_set()

        handler.release_bytes.set()
        await wait_for_condition(
            lambda: server.lifecycle_state == ComponentLifecycleState.STOPPED,
            timeout_seconds=3.0,
        )
        await asyncio.wait_for(handler.terminal_event_seen.wait(), timeout=3.0)
    finally:
        handler.release_bytes.set()
        await client.stop()
        await server.stop()

    assert handler.error is None
    assert handler.bytes_payloads == [b"first"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_spawned_server_stop_preserves_close_events_for_two_real_clients() -> None:
    port = _unused_port()

    class _SpawnStopFromBytesAndBlock:
        def __init__(self) -> None:
            self.server: AsyncioTcpServer | None = None
            self.opened_ids: list[str] = []
            self.closed_ids: list[str] = []
            self.origin_connection_id: str | None = None
            self.bytes_seen = asyncio.Event()
            self.stop_returned = asyncio.Event()
            self.release_bytes = asyncio.Event()
            self.bytes_finished = asyncio.Event()
            self.stop_task: asyncio.Task[None] | None = None
            self.error: BaseException | None = None

        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, ConnectionOpenedEvent):
                self.opened_ids.append(event.resource_id)
                return
            if isinstance(event, ConnectionClosedEvent):
                if (
                    event.resource_id == self.origin_connection_id
                    and not self.bytes_finished.is_set()
                ):
                    self.error = AssertionError("origin close event re-entered bytes handler")
                    self.release_bytes.set()
                self.closed_ids.append(event.resource_id)
                return
            if not isinstance(event, BytesReceivedEvent):
                return

            async def child_stop() -> None:
                assert self.server is not None
                await self.server.stop()
                self.stop_returned.set()

            self.origin_connection_id = event.resource_id
            self.bytes_seen.set()
            try:
                self.stop_task = asyncio.create_task(child_stop())
                await self.stop_task
                if event.resource_id in self.closed_ids:
                    raise AssertionError("origin close event was published before handler returned")
                await self.release_bytes.wait()
            except (Exception, asyncio.CancelledError) as error:
                self.error = error
                self.release_bytes.set()
            finally:
                self.bytes_finished.set()

    handler = _SpawnStopFromBytesAndBlock()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=64,
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        ),
        event_handler=handler,
    )
    handler.server = server
    client_streams: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []

    await server.start()
    try:
        client_streams.append(await asyncio.open_connection("127.0.0.1", port))
        client_streams.append(await asyncio.open_connection("127.0.0.1", port))
        await wait_for_condition(lambda: len(handler.opened_ids) == 2, timeout_seconds=3.0)

        _, first_writer = client_streams[0]
        first_writer.write(b"spawned-server-stop")
        await first_writer.drain()

        await asyncio.wait_for(handler.bytes_seen.wait(), timeout=3.0)
        await asyncio.wait_for(handler.stop_returned.wait(), timeout=3.0)
        assert handler.origin_connection_id not in handler.closed_ids

        handler.release_bytes.set()
        await wait_for_condition(
            lambda: set(handler.closed_ids) == set(handler.opened_ids),
            timeout_seconds=3.0,
        )
    finally:
        handler.release_bytes.set()
        if handler.stop_task is not None and not handler.stop_task.done():
            handler.stop_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(handler.stop_task, timeout=1.0)
        for _, writer in client_streams:
            writer.close()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await writer.wait_closed()
        await server.stop()

    assert handler.error is None
    assert handler.origin_connection_id is not None
    assert len(handler.opened_ids) == 2
    assert set(handler.closed_ids) == set(handler.opened_ids)
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED


@pytest.mark.asyncio
@pytest.mark.integration
async def test_external_inline_client_stop_waits_for_active_bytes_handler() -> None:
    port = _unused_port()

    class _BlockingBytesHandler:
        def __init__(self) -> None:
            self.bytes_started = asyncio.Event()
            self.bytes_finished = asyncio.Event()
            self.release_bytes = asyncio.Event()
            self.terminal_event_seen = asyncio.Event()
            self.error: BaseException | None = None

        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, BytesReceivedEvent):
                self.bytes_started.set()
                await self.release_bytes.wait()
                self.bytes_finished.set()
                return
            if isinstance(event, ConnectionClosedEvent) or (
                isinstance(event, ComponentLifecycleChangedEvent)
                and event.current
                in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
            ):
                if not self.bytes_finished.is_set():
                    self.error = AssertionError("terminal event re-entered bytes handler")
                    self.release_bytes.set()
                self.terminal_event_seen.set()

    handler = _BlockingBytesHandler()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=handler,
    )
    stop_task: asyncio.Task[None] | None = None
    await server.start()
    try:
        await client.start()
        await client.wait_until_connected(timeout_seconds=3.0)
        await server.broadcast(b"external-client-stop")
        await asyncio.wait_for(handler.bytes_started.wait(), timeout=3.0)

        stop_task = asyncio.create_task(client.stop())
        await wait_for_condition(
            lambda: client.lifecycle_state == ComponentLifecycleState.STOPPING,
            timeout_seconds=3.0,
        )

        assert not handler.terminal_event_seen.is_set()
        assert not handler.bytes_finished.is_set()
        assert stop_task.done() is False

        handler.release_bytes.set()
        await asyncio.wait_for(stop_task, timeout=3.0)
        await asyncio.wait_for(handler.terminal_event_seen.wait(), timeout=3.0)
    finally:
        handler.release_bytes.set()
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(stop_task, timeout=1.0)
        await client.stop()
        await server.stop()

    assert handler.error is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_external_inline_server_stop_waits_for_active_bytes_handler() -> None:
    port = _unused_port()

    class _BlockingBytesHandler:
        def __init__(self) -> None:
            self.bytes_started = asyncio.Event()
            self.bytes_finished = asyncio.Event()
            self.release_bytes = asyncio.Event()
            self.terminal_event_seen = asyncio.Event()
            self.error: BaseException | None = None

        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, BytesReceivedEvent):
                self.bytes_started.set()
                await self.release_bytes.wait()
                self.bytes_finished.set()
                return
            if isinstance(event, ConnectionClosedEvent) or (
                isinstance(event, ComponentLifecycleChangedEvent)
                and event.current
                in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
            ):
                if not self.bytes_finished.is_set():
                    self.error = AssertionError("terminal event re-entered bytes handler")
                    self.release_bytes.set()
                self.terminal_event_seen.set()

    handler = _BlockingBytesHandler()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=64,
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=handler,
    )
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=_NoopHandler(),
    )
    stop_task: asyncio.Task[None] | None = None
    await server.start()
    try:
        await client.start()
        connection = await client.wait_until_connected(timeout_seconds=3.0)
        await connection.send(b"external-server-stop")
        await asyncio.wait_for(handler.bytes_started.wait(), timeout=3.0)

        stop_task = asyncio.create_task(server.stop())
        await wait_for_condition(
            lambda: server.lifecycle_state == ComponentLifecycleState.STOPPING,
            timeout_seconds=3.0,
        )

        assert not handler.terminal_event_seen.is_set()
        assert not handler.bytes_finished.is_set()
        assert stop_task.done() is False

        handler.release_bytes.set()
        await asyncio.wait_for(stop_task, timeout=3.0)
        await asyncio.wait_for(handler.terminal_event_seen.wait(), timeout=3.0)
    finally:
        handler.release_bytes.set()
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(stop_task, timeout=1.0)
        await client.stop()
        await server.stop()

    assert handler.error is None
