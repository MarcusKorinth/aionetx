"""
Minimal TCP echo: server echoes bytes back, client verifies the roundtrip.

Run:
    python examples/tcp_echo_server_and_client.py

This example picks an ephemeral port on 127.0.0.1, starts a server that echoes
any bytes it receives, runs a client that sends one message, waits for the
echoed bytes to come back, then stops cleanly. It exits 0 on success so CI can
smoke-test it as a subprocess.
"""

from __future__ import annotations

import asyncio
import socket
import sys

from typing import Callable

from aionetx import (
    AsyncioNetworkFactory,
    BaseNetworkEventHandler,
    BytesReceivedEvent,
    TcpClientSettings,
    TcpServerSettings,
)
from aionetx.api import ConnectionProtocol, TcpServerProtocol


def pick_free_tcp_port() -> int:
    """
    Ask the OS for a free TCP port on the loopback interface.

    NOTE: there is a small TOCTOU window between closing this socket and the
    server binding. For examples that's acceptable; production code should
    prefer passing a fixed, pre-allocated port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class EchoServerHandler(BaseNetworkEventHandler):
    """Echoes every chunk of bytes back over the same connection."""

    def __init__(
        self,
        server_connections_provider: Callable[[], tuple[ConnectionProtocol, ...]],
    ) -> None:
        self._server_connections_provider = server_connections_provider

    async def on_bytes_received(self, event: BytesReceivedEvent) -> None:
        for connection in self._server_connections_provider():
            if connection.connection_id == event.resource_id:
                await connection.send(event.data)
                return


class EchoClientHandler(BaseNetworkEventHandler):
    """Records the first echoed payload and sets an asyncio.Event."""

    def __init__(self) -> None:
        self.received = asyncio.Event()
        self.payload: bytes = b""

    async def on_bytes_received(self, event: BytesReceivedEvent) -> None:
        self.payload = event.data
        self.received.set()


async def main() -> int:
    port = pick_free_tcp_port()
    factory = AsyncioNetworkFactory()

    server: TcpServerProtocol
    server = factory.create_tcp_server(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=EchoServerHandler(lambda: server.connections),
    )
    client_handler = EchoClientHandler()
    client = factory.create_tcp_client(
        settings=TcpClientSettings(host="127.0.0.1", port=port),
        event_handler=client_handler,
    )

    await server.start()
    await server.wait_until_running(timeout_seconds=5.0)
    await client.start()
    connection = await client.wait_until_connected(timeout_seconds=5.0)

    message = b"hello, aionetx"
    await connection.send(message)
    await asyncio.wait_for(client_handler.received.wait(), timeout=5.0)

    assert client_handler.payload == message, (
        f"expected echoed {message!r}, got {client_handler.payload!r}"
    )
    print(f"echoed payload: {client_handler.payload!r}")

    await client.stop()
    await server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
