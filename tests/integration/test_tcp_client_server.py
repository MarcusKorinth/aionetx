"""
Integration tests for the core TCP client/server contract.

These tests cover the main end-to-end behaviors a user expects from the public
API: connection establishment, round trips, broadcast, large-payload transfer,
idempotent stop(), and connection cleanup during churn. They use real sockets
so ordering and lifecycle assertions reflect observable runtime behavior.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from tests.helpers import wait_for_condition


def get_unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tcp_client_server_roundtrip(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED
    await server.start()
    assert server.lifecycle_state == ComponentLifecycleState.RUNNING

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )
    await client.start()
    connection = await client.wait_until_connected(timeout_seconds=3.0)
    await connection.send(b"hello-server")

    await wait_for_condition(
        lambda: any(
            event.data == b"hello-server" for event in recording_event_handler.received_events
        )
    )

    server_connection = server.connections[0]
    matching_events = [
        event
        for event in recording_event_handler.events
        if isinstance(event, BytesReceivedEvent)
        and event.resource_id == server_connection.connection_id
        and event.data == b"hello-server"
    ]
    assert len(matching_events) == 1

    await client.stop()
    await server.stop()
    # The client emits at minimum: lifecycle transitions (STARTING, RUNNING,
    # STOPPING, STOPPED) + ConnectionOpened + ConnectionClosed = 6 events.
    assert client.dispatcher_runtime_stats.emit_calls_total >= 6
    # The server emits at minimum: lifecycle transitions + ConnectionOpened +
    # BytesReceived ("hello-server") + ConnectionClosed = 7 events.
    assert server.dispatcher_runtime_stats.emit_calls_total >= 7
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED

    server_component_id = f"tcp/server/127.0.0.1/{port}"
    server_running_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == server_component_id
        and event.current == ComponentLifecycleState.RUNNING
    ]
    assert len(server_running_events) == 1

    server_stopped_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == server_component_id
        and event.current == ComponentLifecycleState.STOPPED
    ]
    assert len(server_stopped_events) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tcp_client_remains_connected_without_server_traffic(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )
    await client.start()
    connection = await client.wait_until_connected(timeout_seconds=3.0)
    # Assert the connection survives 1 second of total idleness: wait_for_condition
    # will time out (TimeoutError) if the connection never drops — which is the
    # expected outcome; a premature disconnect would make the condition true and
    # the test would fail on the assertions below.
    with pytest.raises(TimeoutError):
        await wait_for_condition(lambda: not connection.is_connected, timeout_seconds=1.0)

    assert connection.is_connected
    assert client.connection is not None
    assert client.connection.is_connected

    await client.stop()
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_stop_closes_active_clients(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()

    client_one = AsyncioTcpClient(
        settings=TcpClientSettings(host="127.0.0.1", port=port),
        event_handler=recording_event_handler,
    )
    client_two = AsyncioTcpClient(
        settings=TcpClientSettings(host="127.0.0.1", port=port),
        event_handler=recording_event_handler,
    )

    await client_one.start()
    await client_two.start()
    connection_one = await client_one.wait_until_connected(timeout_seconds=3.0)
    connection_two = await client_two.wait_until_connected(timeout_seconds=3.0)
    assert connection_one.is_connected
    assert connection_two.is_connected
    # wait_until_connected returns when the *client* side is connected; the
    # server's _handle_client task may still be pending.  Wait for the server
    # to register both connections before proceeding.
    # Exactly 2 clients connected; wait until both are registered server-side.
    await wait_for_condition(lambda: len(server.connections) == 2, timeout_seconds=3.0)
    assert len(server.connections) == 2

    await server.stop()

    await wait_for_condition(
        lambda: (
            client_one.connection is None
            and client_two.connection is None
            and len(server.connections) == 0
        )
    )

    assert client_one.connection is None
    assert client_two.connection is None
    assert len(server.connections) == 0
    # Exactly 2 server-side connections were opened; both must emit a closed event.
    closed_server_events = [
        event
        for event in recording_event_handler.closed_events
        if event.resource_id.startswith("tcp/server/")
    ]
    assert len(closed_server_events) == 2
    assert all(event.previous_state.name == "CONNECTED" for event in closed_server_events)

    await client_one.stop()
    await client_two.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_stop_is_idempotent(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )

    await server.start()
    await server.stop()
    await server.stop()

    assert len(server.connections) == 0
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_handles_rapid_connect_disconnect_bursts(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()

    try:
        for _ in range(10):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            await reader.read(1)

        await wait_for_condition(
            lambda: len(recording_event_handler.closed_events) >= 10,
            timeout_seconds=2.0,
        )
        assert len(server.connections) == 0
    finally:
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_connected_client_stop_is_idempotent_and_quiet(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )

    await client.start()
    connection = await client.wait_until_connected(timeout_seconds=3.0)
    assert connection.is_connected
    await wait_for_condition(lambda: len(server.connections) == 1, timeout_seconds=1.0)

    await client.stop()
    error_count_after_first_stop = len(recording_event_handler.error_events)
    await client.stop()

    assert client.connection is None
    assert len(recording_event_handler.error_events) == error_count_after_first_stop
    await wait_for_condition(lambda: len(server.connections) == 0, timeout_seconds=1.0)

    await server.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_broadcast_reaches_multiple_connected_clients(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()

    client_one = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )
    client_two = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )

    await client_one.start()
    await client_two.start()
    connection_one = await client_one.wait_until_connected(timeout_seconds=3.0)
    connection_two = await client_two.wait_until_connected(timeout_seconds=3.0)
    await wait_for_condition(lambda: len(server.connections) >= 2, timeout_seconds=2.0)

    await server.broadcast(b"broadcast-data")
    await wait_for_condition(
        lambda: (
            b"broadcast-data"
            in recording_event_handler.received_by_connection[connection_one.connection_id]
            and b"broadcast-data"
            in recording_event_handler.received_by_connection[connection_two.connection_id]
        ),
        timeout_seconds=1.0,
    )

    assert (
        b"broadcast-data"
        in recording_event_handler.received_by_connection[connection_one.connection_id]
    )
    assert (
        b"broadcast-data"
        in recording_event_handler.received_by_connection[connection_two.connection_id]
    )

    await client_one.stop()
    await client_two.stop()
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_broadcast_handles_concurrent_client_disconnect(
    recording_event_handler, unused_tcp_port: int
) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=unused_tcp_port, max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()
    stable_client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=unused_tcp_port, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )
    disconnecting_client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=unused_tcp_port, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=recording_event_handler,
    )
    await stable_client.start()
    await disconnecting_client.start()
    stable_connection = await stable_client.wait_until_connected(timeout_seconds=2.0)
    await disconnecting_client.wait_until_connected(timeout_seconds=2.0)
    await wait_for_condition(lambda: len(server.connections) >= 2, timeout_seconds=2.0)

    stop_task = asyncio.create_task(disconnecting_client.stop())
    await server.broadcast(b"race-broadcast")
    await stop_task

    await wait_for_condition(
        lambda: (
            b"race-broadcast"
            in recording_event_handler.received_by_connection[stable_connection.connection_id]
        ),
        timeout_seconds=1.0,
    )
    assert (
        b"race-broadcast"
        in recording_event_handler.received_by_connection[stable_connection.connection_id]
    )
    assert len(server.connections) <= 1
    await stable_client.stop()
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_client_server_bi_directional_exchange_over_five_seconds(
    recording_event_handler,
) -> None:
    port = get_unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )
    await client.start()
    client_connection = await client.wait_until_connected(timeout_seconds=3.0)
    await wait_for_condition(lambda: len(server.connections) == 1, timeout_seconds=2.0)
    server_connection = server.connections[0]

    client_prefix = b"client-msg-"
    server_prefix = b"server-msg-"
    exchange_duration_seconds = 5.0
    tick_seconds = 0.5
    tick_count = int(exchange_duration_seconds / tick_seconds)

    async def client_sender() -> None:
        for index in range(tick_count):
            await client_connection.send(client_prefix + str(index).encode())
            await asyncio.sleep(tick_seconds)

    async def server_sender() -> None:
        for index in range(tick_count):
            await server_connection.send(server_prefix + str(index).encode())
            await asyncio.sleep(tick_seconds)

    await asyncio.gather(client_sender(), server_sender())

    await wait_for_condition(
        lambda: (
            sum(
                1
                for payload in recording_event_handler.received_by_connection[
                    server_connection.connection_id
                ]
                if payload.startswith(client_prefix)
            )
            >= tick_count
            and sum(
                1
                for payload in recording_event_handler.received_by_connection[
                    client_connection.connection_id
                ]
                if payload.startswith(server_prefix)
            )
            >= tick_count
        ),
        timeout_seconds=2.0,
    )

    # Each message is sent 0.5 s apart so TCP does not coalesce; every send
    # arrives as its own chunk and the count must exactly match tick_count.
    assert (
        sum(
            1
            for payload in recording_event_handler.received_by_connection[
                server_connection.connection_id
            ]
            if payload.startswith(client_prefix)
        )
        == tick_count
    )
    assert (
        sum(
            1
            for payload in recording_event_handler.received_by_connection[
                client_connection.connection_id
            ]
            if payload.startswith(server_prefix)
        )
        == tick_count
    )

    await client.stop()
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tcp_client_server_large_payload_is_received_across_chunks(
    recording_event_handler,
) -> None:
    port = get_unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1", port=port, max_connections=64, receive_buffer_size=1024
        ),
        event_handler=recording_event_handler,
    )
    await server.start()

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
            receive_buffer_size=1024,
        ),
        event_handler=recording_event_handler,
    )
    await client.start()
    connection = await client.wait_until_connected(timeout_seconds=3.0)

    payload = b"x" * (64 * 1024)
    await connection.send(payload)

    await wait_for_condition(
        lambda: (
            server.connections
            and sum(
                len(chunk)
                for chunk in recording_event_handler.received_by_connection[
                    server.connections[0].connection_id
                ]
            )
            >= len(payload)
        ),
        timeout_seconds=2.0,
    )

    server_connection_id = server.connections[0].connection_id
    received = b"".join(recording_event_handler.received_by_connection[server_connection_id])
    assert received.startswith(payload[:1024])
    assert len(received) >= len(payload)

    await client.stop()
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_connection_ids_are_unique_across_reconnects(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()
    try:
        ids: set[str] = set()
        for _ in range(3):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await wait_for_condition(lambda: len(server.connections) == 1, timeout_seconds=1.0)
            ids.add(server.connections[0].connection_id)
            writer.close()
            await writer.wait_closed()
            await reader.read(1)
            await wait_for_condition(lambda: len(server.connections) == 0, timeout_seconds=1.0)

        assert len(ids) == 3
        assert all(connection_id.startswith("tcp/server/127.0.0.1/") for connection_id in ids)
    finally:
        await server.stop()
