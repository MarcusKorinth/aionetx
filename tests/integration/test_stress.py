"""
Stress tests for moderate fan-out and many-connection scenarios.

These tests check that the server can manage dozens of simultaneous clients
without leaks, deadlocks, or silent delivery loss. The concurrency levels stay
modest enough for CI while still exercising code paths that matter for
multi-session workloads.

Marked ``slow`` because wall-clock time scales with connection count.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from aionetx.api.network_event import NetworkEvent
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from tests.helpers import wait_for_condition


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Noop:
    async def on_event(self, event: NetworkEvent) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_server_handles_100_concurrent_connections() -> None:
    """
    Server accepts and tracks 100 simultaneous client connections.

    All clients connect concurrently using asyncio.gather.  After all are
    confirmed connected on the client side, we wait for the server to register
    all connections, then stop everything cleanly.

    Invariants checked:
    - Server records exactly 100 connections (no silent drops or duplicates).
    - All clients report is_connected.
    - Clean shutdown leaves no dangling connections.
    """
    num_clients = 100
    port = _unused_port()

    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=num_clients,
            backlog=128,
        ),
        event_handler=_Noop(),
    )
    await server.start()

    clients = [
        AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
            ),
            event_handler=_Noop(),
        )
        for _ in range(num_clients)
    ]

    try:
        # Start all clients concurrently.
        await asyncio.gather(*[c.start() for c in clients])

        # Wait for all clients to report connected.
        connections = await asyncio.gather(
            *[c.wait_until_connected(timeout_seconds=10.0) for c in clients]
        )
        assert all(conn.is_connected for conn in connections), (
            "Not all client connections reached is_connected=True."
        )

        # Wait for server to register all connections (server-side _handle_client
        # tasks run concurrently and may not all have finished registering yet).
        await wait_for_condition(
            lambda: len(server.connections) == num_clients,
            timeout_seconds=5.0,
        )
        assert len(server.connections) == num_clients, (
            f"Server registered {len(server.connections)} connections; expected {num_clients}."
        )
    finally:
        # Stop all clients concurrently, then the server.
        # return_exceptions=True prevents a single stop() failure from skipping
        # the remaining client stops and the subsequent server.stop().
        await asyncio.gather(*[c.stop() for c in clients], return_exceptions=True)
        await server.stop()

    assert len(server.connections) == 0, "Server must have zero connections after stop()."


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_server_broadcast_reaches_all_connected_clients() -> None:
    """
    Server broadcast delivers data to all 50 concurrently connected clients.

    Each client has a recording handler.  After broadcast, every client must
    have received the exact payload.
    """
    num_clients = 50
    port = _unused_port()

    received_by: dict[int, list[bytes]] = {i: [] for i in range(num_clients)}

    class _Recorder:
        def __init__(self, idx: int) -> None:
            self._idx = idx

        async def on_event(self, event: NetworkEvent) -> None:
            from aionetx.api.bytes_received_event import BytesReceivedEvent

            if isinstance(event, BytesReceivedEvent):
                received_by[self._idx].append(event.data)

    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64, backlog=64),
        event_handler=_Noop(),
    )
    clients = [
        AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
            ),
            event_handler=_Recorder(i),
        )
        for i in range(num_clients)
    ]

    await server.start()
    try:
        await asyncio.gather(*[c.start() for c in clients])
        await asyncio.gather(*[c.wait_until_connected(timeout_seconds=10.0) for c in clients])
        await wait_for_condition(
            lambda: len(server.connections) == num_clients,
            timeout_seconds=5.0,
        )

        payload = b"broadcast-stress-test"
        await server.broadcast(payload)

        # Wait for every client to receive the broadcast.
        await wait_for_condition(
            lambda: all(len(received_by[i]) >= 1 for i in range(num_clients)),
            timeout_seconds=5.0,
        )

        # Exactly-once delivery: each client must have received the payload
        # precisely once — duplicates would indicate a broadcast bug.
        for i in range(num_clients):
            count = received_by[i].count(payload)
            assert count == 1, f"Client {i} received payload {count} time(s); expected exactly 1."
    finally:
        # return_exceptions=True prevents a single stop() failure from skipping
        # the remaining client stops and the subsequent server.stop().
        await asyncio.gather(*[c.stop() for c in clients], return_exceptions=True)
        await server.stop()
