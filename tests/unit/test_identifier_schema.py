from __future__ import annotations

import asyncio
import socket

import pytest

from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.api.udp import UdpReceiverSettings
from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
from aionetx.implementations.asyncio_impl.asyncio_multicast_receiver import AsyncioMulticastReceiver
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver
from tests.internal_asyncio_impl_refs import (
    multicast_receiver_component_id,
    tcp_client_component_id,
    tcp_client_connection_id,
    tcp_server_component_id,
    tcp_server_connection_id,
    udp_receiver_component_id,
)
from tests.helpers import wait_for_condition


def _unused_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_component_id_helpers_follow_canonical_schema() -> None:
    assert tcp_client_component_id("127.0.0.1", 5000) == "tcp/client/127.0.0.1/5000"
    assert (
        tcp_client_connection_id("127.0.0.1", 5000, ("10.0.0.1", 6000))
        == "tcp/client/10.0.0.1/6000/connection"
    )
    assert (
        tcp_client_connection_id("127.0.0.1", 5000, None) == "tcp/client/127.0.0.1/5000/connection"
    )
    assert tcp_client_component_id("127.0.0.1", 5000) != tcp_client_connection_id(
        "127.0.0.1", 5000, None
    )
    assert tcp_server_component_id("127.0.0.1", 5001) == "tcp/server/127.0.0.1/5001"
    assert (
        tcp_server_connection_id(("10.0.0.2", 7000), 3) == "tcp/server/10.0.0.2/7000/connection/3"
    )
    assert tcp_server_connection_id(None, 4) == "tcp/server/unknown/unknown/connection/4"
    assert udp_receiver_component_id("0.0.0.0", 9000) == "udp/receiver/0.0.0.0/9000"
    assert multicast_receiver_component_id("239.0.0.1", 9999) == "udp/multicast/239.0.0.1/9999"


def test_connection_role_excludes_udp_sender() -> None:
    assert "UDP_SENDER" not in ConnectionRole.__members__


@pytest.mark.asyncio
async def test_tcp_client_lifecycle_events_use_component_ids(recording_event_handler) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=65001,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )
    await client.start()
    await client.stop()
    assert any(
        event.resource_id == "tcp/client/127.0.0.1/65001"
        for event in recording_event_handler.lifecycle_events
    )
    assert all(
        event.resource_id == "tcp/client/127.0.0.1/65001"
        for event in recording_event_handler.reconnect_attempt_started_events
    )
    assert all(
        event.resource_id == "tcp/client/127.0.0.1/65001"
        for event in recording_event_handler.reconnect_attempt_failed_events
    )


@pytest.mark.asyncio
async def test_tcp_client_connection_events_use_connection_ids(
    recording_event_handler, unused_tcp_port: int
) -> None:
    async def handle_client(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", unused_tcp_port)
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=unused_tcp_port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )
    try:
        await client.start()
        await wait_for_condition(
            lambda: bool(recording_event_handler.opened_events), timeout_seconds=1.0
        )
        assert recording_event_handler.opened_events
        assert all(
            event.resource_id.startswith(f"tcp/client/127.0.0.1/{unused_tcp_port}/connection")
            for event in recording_event_handler.opened_events
        )
        assert all(
            event.resource_id != f"tcp/client/127.0.0.1/{unused_tcp_port}"
            for event in recording_event_handler.opened_events
        )
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_tcp_server_lifecycle_events_use_component_ids(recording_event_handler) -> None:
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=_unused_tcp_port(), max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()
    await server.stop()
    assert any(
        event.resource_id.startswith("tcp/server/")
        for event in recording_event_handler.lifecycle_events
    )


@pytest.mark.asyncio
async def test_datagram_receivers_use_canonical_component_ids(recording_event_handler) -> None:
    udp_receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=_unused_udp_port()),
        event_handler=recording_event_handler,
    )
    await udp_receiver.start()
    await udp_receiver.stop()
    assert any(
        event.resource_id.startswith("udp/receiver/")
        for event in recording_event_handler.lifecycle_events
    )

    multicast_receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(group_ip="239.255.0.50", port=21055),
        event_handler=recording_event_handler,
    )
    assert multicast_receiver._connection_id.startswith("udp/multicast/")  # type: ignore[attr-defined]
