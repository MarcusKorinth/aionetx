from __future__ import annotations
import logging
import socket

import pytest

from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer


class NoopHandler:
    async def on_event(self, event) -> None:  # pragma: no cover - behavior is logger-focused
        return None


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
async def test_client_and_server_emit_lifecycle_debug_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = NoopHandler()
    port = _unused_port()

    server = AsyncioTcpServer(
        TcpServerSettings(host="127.0.0.1", port=port, max_connections=64), handler
    )
    await server.start()

    client = AsyncioTcpClient(
        TcpClientSettings(
            host="127.0.0.1", port=port, reconnect=TcpReconnectSettings(enabled=False)
        ),
        handler,
    )
    await client.start()
    await client.wait_until_connected(timeout_seconds=1.0)
    await client.stop()
    await server.stop()

    messages = [record.getMessage() for record in caplog.records]
    assert any("Starting TCP client" in message for message in messages)
    assert any("TCP client stopped" in message for message in messages)
    assert any("Starting TCP server" in message for message in messages)
    assert any("TCP server stopped" in message for message in messages)
