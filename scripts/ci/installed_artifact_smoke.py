from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from importlib.metadata import version as package_version

import aionetx
from aionetx import (
    AsyncioNetworkFactory,
    BytesReceivedEvent,
    TcpReconnectSettings,
    TcpClientSettings,
    TcpServerSettings,
    UdpReceiverSettings,
    UdpSenderSettings,
)
from aionetx.api import NetworkEvent, ReconnectAttemptFailedEvent

# Verify __version__ is present, non-empty, and matches expected pyproject.toml version.
assert hasattr(aionetx, "__version__"), "aionetx.__version__ is missing from installed package"
assert isinstance(aionetx.__version__, str) and aionetx.__version__, (
    f"aionetx.__version__ must be a non-empty string; got {aionetx.__version__!r}"
)
assert aionetx.__version__ == package_version("aionetx"), (
    "aionetx.__version__ must match installed package metadata; "
    f"got {aionetx.__version__!r} vs {package_version('aionetx')!r}"
)


class SmokeEventHandler:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self.events: list[NetworkEvent] = []
        self.received_payloads: list[bytes] = []

    async def on_event(self, event: NetworkEvent) -> None:
        async with self._condition:
            self.events.append(event)
            if isinstance(event, BytesReceivedEvent):
                self.received_payloads.append(event.data)
            self._condition.notify_all()


async def _wait_for_event(
    handler: SmokeEventHandler,
    predicate: Callable[[], bool],
    *,
    timeout: float,
    description: str,
) -> None:
    try:
        async with handler._condition:
            await asyncio.wait_for(handler._condition.wait_for(predicate), timeout=timeout)
    except TimeoutError as error:
        raise AssertionError(
            f"installed artifact smoke did not observe expected event: {description}"
        ) from error


def _get_unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_unused_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _run_tcp_roundtrip_smoke(
    factory: AsyncioNetworkFactory, handler: SmokeEventHandler
) -> None:
    port = _get_unused_tcp_port()
    server = factory.create_tcp_server(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=handler,
    )
    client = factory.create_tcp_client(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=handler,
    )

    await server.start()
    await client.start()
    try:
        connection = await client.wait_until_connected(timeout_seconds=3.0)
        payload = b"installed-artifact-smoke-tcp"
        await connection.send(payload)
        await _wait_for_event(
            handler,
            lambda: payload in handler.received_payloads,
            timeout=3.0,
            description="TCP payload loopback",
        )
    finally:
        await client.stop()
        await server.stop()


async def _run_udp_roundtrip_smoke(
    factory: AsyncioNetworkFactory, handler: SmokeEventHandler
) -> None:
    port = _get_unused_udp_port()
    receiver = factory.create_udp_receiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=port),
        event_handler=handler,
    )
    sender = factory.create_udp_sender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port),
    )

    await receiver.start()
    try:
        payload = b"installed-artifact-smoke-udp"
        await sender.send(payload)
        await _wait_for_event(
            handler,
            lambda: payload in handler.received_payloads,
            timeout=3.0,
            description="UDP payload receive",
        )
    finally:
        await sender.stop()
        await receiver.stop()


async def _run_reconnect_smoke(factory: AsyncioNetworkFactory, handler: SmokeEventHandler) -> None:
    port = _get_unused_tcp_port()
    reconnecting_client = factory.create_tcp_client(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(
                enabled=True,
                initial_delay_seconds=0.05,
                max_delay_seconds=0.1,
                backoff_factor=1.0,
            ),
        ),
        event_handler=handler,
    )

    await reconnecting_client.start()
    try:
        await _wait_for_event(
            handler,
            lambda: any(isinstance(event, ReconnectAttemptFailedEvent) for event in handler.events),
            timeout=3.0,
            description="reconnect failure event",
        )
    finally:
        await reconnecting_client.stop()


async def main() -> None:
    handler = SmokeEventHandler()
    factory = AsyncioNetworkFactory()

    await _run_tcp_roundtrip_smoke(factory, handler)
    await _run_udp_roundtrip_smoke(factory, handler)
    await _run_reconnect_smoke(factory, handler)


if __name__ == "__main__":
    asyncio.run(main())
