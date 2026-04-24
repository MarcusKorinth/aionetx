"""Tests for async context manager (__aenter__ / __aexit__) support on all transports."""

from __future__ import annotations

import socket

import pytest

from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.network_event import NetworkEvent
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.udp import UdpReceiverSettings, UdpSenderSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver
from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender


class _NoopHandler:
    async def on_event(self, event: NetworkEvent) -> None:
        return None


async def _raise_test_error() -> None:
    raise ValueError("test-error")


def _get_unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get_unused_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --- TCP Server ---


@pytest.mark.asyncio
async def test_tcp_server_async_context_manager_is_running_inside_block() -> None:
    """__aenter__ must call start() and the server is RUNNING inside the block."""
    port = _get_unused_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    async with server as ctx:
        assert ctx is server
        assert server.lifecycle_state == ComponentLifecycleState.RUNNING


@pytest.mark.asyncio
async def test_tcp_server_async_context_manager_is_stopped_after_block() -> None:
    """__aexit__ must call stop(); server is STOPPED after leaving the block."""
    port = _get_unused_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    async with server:
        pass
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED


@pytest.mark.asyncio
async def test_tcp_server_async_context_manager_stops_on_exception() -> None:
    """__aexit__ must call stop() even when the body raises."""
    port = _get_unused_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    with pytest.raises(ValueError, match="test-error"):
        async with server:
            await _raise_test_error()
    assert server.lifecycle_state == ComponentLifecycleState.STOPPED


# --- TCP Client ---


@pytest.mark.asyncio
async def test_tcp_client_async_context_manager_is_running_inside_block() -> None:
    port = _get_unused_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    await server.start()
    try:
        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
            ),
            event_handler=_NoopHandler(),
        )
        async with client as ctx:
            assert ctx is client
            assert client.lifecycle_state == ComponentLifecycleState.RUNNING
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_tcp_client_async_context_manager_is_stopped_after_block() -> None:
    port = _get_unused_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_NoopHandler(),
    )
    await server.start()
    try:
        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
            ),
            event_handler=_NoopHandler(),
        )
        async with client:
            pass
        assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    finally:
        await server.stop()


# --- UDP Receiver ---


@pytest.mark.asyncio
async def test_udp_receiver_async_context_manager_is_running_inside_block() -> None:
    port = _get_unused_udp_port()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=port),
        event_handler=_NoopHandler(),
    )
    async with receiver as ctx:
        assert ctx is receiver
        assert receiver.lifecycle_state == ComponentLifecycleState.RUNNING


@pytest.mark.asyncio
async def test_udp_receiver_async_context_manager_is_stopped_after_block() -> None:
    port = _get_unused_udp_port()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=port),
        event_handler=_NoopHandler(),
    )
    async with receiver:
        pass
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED


# --- UDP Sender ---


@pytest.mark.asyncio
async def test_udp_sender_async_context_manager_returns_self() -> None:
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=_get_unused_udp_port())
    )
    async with sender as ctx:
        assert ctx is sender
        assert not sender._closed


@pytest.mark.asyncio
async def test_udp_sender_async_context_manager_closes_after_block() -> None:
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=_get_unused_udp_port())
    )
    async with sender:
        pass
    assert sender._closed
