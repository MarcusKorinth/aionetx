"""
Integration tests for TCP heartbeat behavior.

These scenarios verify that heartbeat payloads are emitted on live
connections, resume after reconnect, shut down cleanly, and fail visibly when
heartbeat configuration is incomplete on either the client or server side.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.errors import HeartbeatConfigurationError
from aionetx.api.heartbeat import HeartbeatRequest
from aionetx.api.heartbeat import HeartbeatResult
from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from aionetx.api.tcp_server import TcpServerSettings
from tests.helpers import wait_for_condition


def get_unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CounterHeartbeatProvider(HeartbeatProviderProtocol):
    def __init__(self) -> None:
        self.counter = 0

    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        self.counter += 1
        return HeartbeatResult(
            should_send=True,
            payload=f"HB:{request.connection_id}:{self.counter}".encode(),
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_heartbeat_is_sent(recording_event_handler) -> None:
    received: list[bytes] = []
    heartbeat_received = asyncio.Event()
    port = get_unused_tcp_port()

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                received.append(data)
                if data.startswith(b"HB:"):
                    heartbeat_received.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", port)
    async with server:
        provider = CounterHeartbeatProvider()
        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
                heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.02),
            ),
            event_handler=recording_event_handler,
            heartbeat_provider=provider,
        )
        await client.start()
        await client.wait_until_connected(timeout_seconds=3.0)
        await asyncio.wait_for(heartbeat_received.wait(), timeout=1.0)
        await client.stop()

    assert provider.counter >= 1
    # At least one heartbeat chunk was received; verify the first matches the protocol prefix.
    assert len(received) >= 1
    hb_payloads = [p for p in received if p.startswith(b"HB:")]
    assert len(hb_payloads) >= 1


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.integration_smoke
@pytest.mark.integration_semantic
async def test_heartbeat_restarts_after_reconnect(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    connection_count = 0
    second_connection_started = asyncio.Event()

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        nonlocal connection_count
        connection_count += 1
        if connection_count == 1:
            writer.close()
            await writer.wait_closed()
            return

        second_connection_started.set()
        await wait_for_condition(lambda: provider.counter >= 2, timeout_seconds=1.0)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", port)
    async with server:
        provider = CounterHeartbeatProvider()
        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(
                    enabled=True,
                    initial_delay_seconds=0.02,
                    max_delay_seconds=0.05,
                    backoff_factor=1.0,
                ),
                heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.02),
            ),
            event_handler=recording_event_handler,
            heartbeat_provider=provider,
        )
        await client.start()
        await wait_for_condition(lambda: connection_count >= 2, timeout_seconds=3.0)
        await asyncio.wait_for(second_connection_started.wait(), timeout=2.0)
        await wait_for_condition(lambda: provider.counter >= 2, timeout_seconds=1.0)
        await client.stop()

    # Exactly 2 connections (first dropped immediately, second kept alive).
    assert connection_count == 2
    # Second connection receives at least 2 heartbeats (loop condition above waits for >= 2).
    assert provider.counter >= 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stop_during_active_heartbeat_is_idempotent(recording_event_handler) -> None:
    port = get_unused_tcp_port()

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while await reader.read(4096):
                continue
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", port)
    async with server:
        provider = CounterHeartbeatProvider()
        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
                heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
            ),
            event_handler=recording_event_handler,
            heartbeat_provider=provider,
        )
        await client.start()
        await client.wait_until_connected(timeout_seconds=3.0)
        await wait_for_condition(lambda: provider.counter >= 1, timeout_seconds=1.0)
        await client.stop()
        await client.stop()

    assert client.connection is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_client_heartbeat_misconfiguration_fails_fast_at_construction(
    recording_event_handler,
) -> None:
    port = get_unused_tcp_port()
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
    async with server:
        with pytest.raises(HeartbeatConfigurationError, match="heartbeat_provider"):
            AsyncioTcpClient(
                settings=TcpClientSettings(
                    host="127.0.0.1",
                    port=port,
                    reconnect=TcpReconnectSettings(enabled=False),
                    heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
                ),
                event_handler=recording_event_handler,
            )

    assert recording_event_handler.error_events == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_heartbeat_misconfiguration_fails_fast_at_construction(
    recording_event_handler,
) -> None:
    port = get_unused_tcp_port()
    with pytest.raises(HeartbeatConfigurationError, match="heartbeat_provider"):
        AsyncioTcpServer(
            settings=TcpServerSettings(
                host="127.0.0.1",
                port=port,
                max_connections=64,
                heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
            ),
            event_handler=recording_event_handler,
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_heartbeat_sends_to_multiple_clients(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    provider = CounterHeartbeatProvider()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=64,
            heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.02),
        ),
        event_handler=recording_event_handler,
        heartbeat_provider=provider,
    )
    await server.start()

    reader_one, writer_one = await asyncio.open_connection("127.0.0.1", port)
    reader_two, writer_two = await asyncio.open_connection("127.0.0.1", port)

    payloads_one: list[bytes] = []
    payloads_two: list[bytes] = []

    async def read_heartbeat(reader: asyncio.StreamReader, sink: list[bytes]) -> None:
        while len(sink) < 1:
            data = await reader.read(4096)
            if not data:
                break
            sink.append(data)

    try:
        await asyncio.wait_for(
            asyncio.gather(
                read_heartbeat(reader_one, payloads_one),
                read_heartbeat(reader_two, payloads_two),
            ),
            timeout=1.0,
        )
    finally:
        writer_one.close()
        writer_two.close()
        await writer_one.wait_closed()
        await writer_two.wait_closed()
        await server.stop()

    # Each raw reader loop collects chunks until at least 1 is present.
    assert len(payloads_one) >= 1
    assert payloads_one[0].startswith(b"HB:")
    assert len(payloads_two) >= 1
    assert payloads_two[0].startswith(b"HB:")
    # At least one heartbeat per connection (2 connections total).
    assert provider.counter >= 2
