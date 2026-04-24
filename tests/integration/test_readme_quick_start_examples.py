"""
Smoke tests for the README quick-start examples.

These tests execute the main README code paths against real transports so the
documented onboarding examples remain runnable: beginner TCP, advanced
heartbeat/reconnect TCP, fixed-frame handling, and UDP sender/receiver usage.
They are intentionally smoke coverage, not exhaustive protocol tests.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from aionetx.api.connection_events import ConnectionClosedEvent
from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.heartbeat import HeartbeatRequest
from aionetx.api.heartbeat import HeartbeatResult
from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.api.network_event import NetworkEvent
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx import (
    BaseNetworkEventHandler,
    BytesReceivedEvent,
    UdpReceiverSettings,
    UdpSenderSettings,
)
from aionetx.factories.asyncio_network_factory import AsyncioNetworkFactory
from tests.helpers import wait_for_condition


def get_unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DynamicServerHeartbeatProvider(HeartbeatProviderProtocol):
    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        payload = f"HB:server:{request.connection_id}".encode()
        return HeartbeatResult(should_send=True, payload=payload)


class DynamicClientHeartbeatProvider(HeartbeatProviderProtocol):
    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        payload = f"HB:client:{request.connection_id}".encode()
        return HeartbeatResult(should_send=True, payload=payload)


FRAME_SIZE = 1359


class FixedFrameHandler:
    def __init__(self) -> None:
        self._buffers: dict[str, bytearray] = {}
        self.frames: list[tuple[str, bytes]] = []
        self.closed_resource_ids: list[str] = []

    async def on_event(self, event: NetworkEvent) -> None:
        if isinstance(event, BytesReceivedEvent):
            buf = self._buffers.setdefault(event.resource_id, bytearray())
            buf.extend(event.data)

            while len(buf) >= FRAME_SIZE:
                frame = bytes(buf[:FRAME_SIZE])
                del buf[:FRAME_SIZE]
                await self.process_frame(event.resource_id, frame)

        elif isinstance(event, ConnectionClosedEvent):
            self.closed_resource_ids.append(event.resource_id)
            self._buffers.pop(event.resource_id, None)

    async def process_frame(self, resource_id: str, frame: bytes) -> None:
        assert len(frame) == FRAME_SIZE
        self.frames.append((resource_id, frame))


class UdpHandler(BaseNetworkEventHandler):
    def __init__(self) -> None:
        self.received = asyncio.Event()
        self.payload: bytes | None = None
        self.remote_host: str | None = None
        self.remote_port: int | None = None

    async def on_bytes_received(self, event: BytesReceivedEvent) -> None:
        self.payload = event.data
        self.remote_host = event.remote_host
        self.remote_port = event.remote_port
        self.received.set()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.integration_smoke
async def test_readme_beginner_tcp_examples_are_runnable(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    factory = AsyncioNetworkFactory()

    server = factory.create_tcp_server(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )
    client = factory.create_tcp_client(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )

    await server.start()
    await client.start()
    try:
        connection = await client.wait_until_connected(timeout_seconds=3.0)
        await connection.send(b"hello from client")

        await wait_for_condition(
            lambda: any(
                event.data == b"hello from client"
                for event in recording_event_handler.received_events
            )
        )
        client_hello_events = [
            event
            for event in recording_event_handler.received_events
            if event.data == b"hello from client"
        ]
        assert len(client_hello_events) == 1
    finally:
        await client.stop()
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_readme_advanced_tcp_examples_are_runnable(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    factory = AsyncioNetworkFactory()

    server = factory.create_tcp_server(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=64,
            heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.2),
        ),
        event_handler=recording_event_handler,
        heartbeat_provider=DynamicServerHeartbeatProvider(),
    )
    client = factory.create_tcp_client(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=True),
            heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.2),
        ),
        event_handler=recording_event_handler,
        heartbeat_provider=DynamicClientHeartbeatProvider(),
    )

    await server.start()
    await client.start()
    try:
        connection = await client.wait_until_connected(timeout_seconds=3.0)
        await connection.send(b"hello from advanced client")

        await wait_for_condition(
            lambda: any(
                event.data.startswith(b"HB:") for event in recording_event_handler.received_events
            ),
            timeout_seconds=2.0,
        )
        advanced_client_events = [
            event
            for event in recording_event_handler.received_events
            if event.data == b"hello from advanced client"
        ]
        assert len(advanced_client_events) == 1

        hb_events = [
            event
            for event in recording_event_handler.received_events
            if event.data.startswith(b"HB:")
        ]
        assert len(hb_events) >= 1
    finally:
        await client.stop()
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_readme_fixed_frame_example_is_runnable() -> None:
    port = get_unused_tcp_port()
    factory = AsyncioNetworkFactory()
    handler = FixedFrameHandler()

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
        event_handler=BaseNetworkEventHandler(),
    )

    await server.start()
    await client.start()
    try:
        connection = await client.wait_until_connected(timeout_seconds=3.0)
        frame = b"x" * FRAME_SIZE
        await connection.send(frame[:400])
        await connection.send(frame[400:])
        await wait_for_condition(lambda: len(handler.frames) == 1, timeout_seconds=2.0)

        assert len(handler.frames) == 1
        assert handler.frames[0][1] == frame
    finally:
        await client.stop()
        await server.stop()

    # The server-side connection for the one client must emit a close event.
    assert len(handler.closed_resource_ids) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_readme_udp_sender_receiver_example_is_runnable() -> None:
    port = get_unused_tcp_port()
    factory = AsyncioNetworkFactory()
    handler = UdpHandler()

    receiver = factory.create_udp_receiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=port),
        event_handler=handler,
    )
    sender = factory.create_udp_sender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port),
    )

    await receiver.start()
    try:
        await sender.send(b"hello over udp")
        await asyncio.wait_for(handler.received.wait(), timeout=2.0)
    finally:
        await sender.stop()
        await receiver.stop()

    assert handler.payload == b"hello over udp"
    assert handler.remote_host == "127.0.0.1"
    assert handler.remote_port is not None
