"""
Resource-leak detection tests.

Invariant: every asyncio Task created by start() must be cancelled and
awaited by stop().  A leaked task causes memory growth and can execute
stale callbacks on the next start() cycle.

Strategy: snapshot asyncio.all_tasks() before start() and after stop(),
subtract the current task and any tasks that were already present, and
assert the set is empty.

Coverage:
- AsyncioTcpClient (with real server so the supervisor connects)
- AsyncioTcpServer
- AsyncioUdpReceiver
- AsyncioUdpSender (stateless; start/send/stop cycle)
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from aionetx.api.network_event import NetworkEvent
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.api.udp import UdpReceiverSettings, UdpSenderSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver
from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _unused_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Noop:
    async def on_event(self, event: NetworkEvent) -> None:
        return None


def _leaked_tasks(tasks_before: set[asyncio.Task[object]]) -> set[asyncio.Task[object]]:
    """Return tasks present after stop() that were not present before start()."""
    current = asyncio.current_task()
    return asyncio.all_tasks() - tasks_before - ({current} if current is not None else set())


# ---------------------------------------------------------------------------
# TCP Client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tcp_client_start_stop_leaves_no_dangling_tasks() -> None:
    """
    TCP client leaves no asyncio tasks running after stop().

    Both server and client are started/stopped inside the measurement window
    so that server-side per-connection tasks (created after the client
    connects) are also accounted for in the final snapshot.
    """
    port = _unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_Noop(),
    )
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=_Noop(),
    )

    tasks_before = set(asyncio.all_tasks())
    await server.start()
    await client.start()
    # Wait for supervisor to connect so the receive-loop task is also started.
    connection = await client.wait_until_connected(timeout_seconds=3.0)
    assert connection.is_connected
    await client.stop()
    await server.stop()

    leaked = _leaked_tasks(tasks_before)
    assert not leaked, f"TCP client leaked {len(leaked)} task(s): {leaked}"


@pytest.mark.asyncio
async def test_tcp_client_failed_start_stop_leaves_no_dangling_tasks() -> None:
    """Failed TCP client startup still tears down supervision tasks cleanly."""
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=_unused_tcp_port(),
            reconnect=TcpReconnectSettings(enabled=False),
            connect_timeout_seconds=0.2,
        ),
        event_handler=_Noop(),
    )

    tasks_before = set(asyncio.all_tasks())
    await client.start()
    with pytest.raises(ConnectionError):
        await client.wait_until_connected(timeout_seconds=0.5, poll_interval_seconds=0.02)
    await client.stop()

    leaked = _leaked_tasks(tasks_before)
    assert not leaked, f"TCP client failed start leaked {len(leaked)} task(s): {leaked}"


# ---------------------------------------------------------------------------
# TCP Server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tcp_server_start_stop_leaves_no_dangling_tasks() -> None:
    """TCP server leaves no asyncio tasks running after stop()."""
    port = _unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_Noop(),
    )

    tasks_before = set(asyncio.all_tasks())
    await server.start()
    await server.stop()

    leaked = _leaked_tasks(tasks_before)
    assert not leaked, f"TCP server leaked {len(leaked)} task(s): {leaked}"


@pytest.mark.asyncio
async def test_tcp_server_with_connected_client_stop_leaves_no_dangling_tasks() -> None:
    """TCP server cleans up all per-connection tasks when stopped."""
    port = _unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=_Noop(),
    )
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=_Noop(),
    )

    tasks_before = set(asyncio.all_tasks())
    await server.start()
    await client.start()
    connection = await client.wait_until_connected(timeout_seconds=3.0)
    assert connection.is_connected

    await client.stop()
    await server.stop()

    leaked = _leaked_tasks(tasks_before)
    assert not leaked, f"TCP server + client leaked {len(leaked)} task(s): {leaked}"


# ---------------------------------------------------------------------------
# UDP Receiver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_udp_receiver_start_stop_leaves_no_dangling_tasks() -> None:
    """UDP receiver leaves no asyncio tasks running after stop()."""
    port = _unused_udp_port()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=port),
        event_handler=_Noop(),
    )

    tasks_before = set(asyncio.all_tasks())
    await receiver.start()
    await receiver.stop()

    leaked = _leaked_tasks(tasks_before)
    assert not leaked, f"UDP receiver leaked {len(leaked)} task(s): {leaked}"


# ---------------------------------------------------------------------------
# UDP Sender
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_udp_sender_send_stop_leaves_no_dangling_tasks() -> None:
    """UDP sender leaves no asyncio tasks running after stop()."""
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=_unused_udp_port()),
    )

    tasks_before = set(asyncio.all_tasks())
    await sender.send(b"probe")
    await sender.stop()

    leaked = _leaked_tasks(tasks_before)
    assert not leaked, f"UDP sender leaked {len(leaked)} task(s): {leaked}"
