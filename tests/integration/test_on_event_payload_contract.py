from __future__ import annotations

import socket

import pytest

from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from tests.helpers import wait_for_condition


class BytesOnlyHandler:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    async def on_event(self, event) -> None:
        if isinstance(event, BytesReceivedEvent):
            self.payloads.append(event.data)


def get_unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.integration_semantic
async def test_on_event_bytes_received_event_contains_payload_bytes() -> None:
    port = get_unused_tcp_port()
    handler = BytesOnlyHandler()
    server = AsyncioTcpServer(
        TcpServerSettings(host="127.0.0.1", port=port, max_connections=64), handler
    )
    client = AsyncioTcpClient(
        TcpClientSettings(
            host="127.0.0.1", port=port, reconnect=TcpReconnectSettings(enabled=False)
        ),
        handler,
    )

    await server.start()
    await client.start()
    try:
        connection = await client.wait_until_connected(timeout_seconds=3.0)
        await connection.send(b"payload-check")
        await wait_for_condition(lambda: b"payload-check" in handler.payloads, timeout_seconds=2.0)
    finally:
        await client.stop()
        await server.stop()

    assert b"payload-check" in handler.payloads
    assert all(isinstance(payload, bytes) for payload in handler.payloads)
