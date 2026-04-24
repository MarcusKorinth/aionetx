"""
Integration tests for post-stop lifecycle invariants.

The key contract here is simple: once stop() has returned, no further handler
callbacks may be delivered. Making that invariant explicit keeps shutdown
regressions easy to diagnose instead of burying them as side effects in other
tests.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from aionetx.api.tcp_server import TcpServerSettings
from tests.helpers import wait_for_condition


def _get_unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.behavior_critical
async def test_invariant_no_callbacks_after_stopped(recording_event_handler) -> None:
    """
    No new events must be emitted after stop() returns.

    After ``stop()`` has fully awaited, the event handler must receive zero
    additional events regardless of how long the test waits. The invariant
    protects callers from use-after-free and double-dispatch bugs caused by
    in-flight async tasks that were not properly cancelled during shutdown.
    """
    port = _get_unused_tcp_port()

    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=recording_event_handler,
    )
    await server.start()

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )

    await client.start()
    connection = await client.wait_until_connected(timeout_seconds=3.0)
    assert connection.is_connected

    # Ensure the server has registered the connection before we stop.
    await wait_for_condition(lambda: len(server.connections) >= 1, timeout_seconds=2.0)

    # Send some data to confirm the pipeline is live before stopping.
    await connection.send(b"invariant-probe")
    await wait_for_condition(
        lambda: any(
            event.data == b"invariant-probe" for event in recording_event_handler.received_events
        ),
        timeout_seconds=2.0,
    )

    # Stop the client fully and await its completion.
    await client.stop()

    # Capture the event count at the exact point stop() returned.
    event_count_at_stop = len(recording_event_handler.events)

    # Wait a short quiet period — any stray in-flight callbacks would arrive here.
    await asyncio.sleep(0.1)

    # Assert the invariant: no new events must have been emitted after stop().
    assert len(recording_event_handler.events) == event_count_at_stop, (
        f"{len(recording_event_handler.events) - event_count_at_stop} event(s) were emitted "
        "after stop() returned. Events after stop indicate un-cancelled tasks "
        "or a broken shutdown sequence."
    )

    await server.stop()
