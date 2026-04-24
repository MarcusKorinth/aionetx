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
        await client.stop()
        await server.stop()


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

        assert client.lifecycle_state == ComponentLifecycleState.STOPPED
        assert any(isinstance(event, ConnectionClosedEvent) for event in client_handler.events)
    finally:
        await client.stop()
        await server.stop()
