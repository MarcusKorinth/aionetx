"""
Regression tests for the TOCTOU race in start().

Root cause (pre-fix):
  Both AsyncioTcpClient.start() and AsyncioTcpServer.start() used a
  two-lock pattern:

      Lock 1: check state, set flags (state stays STOPPED)
      # --- window: dispatcher.start() outside the lock ---
      Lock 2: transition STOPPED → STARTING → RUNNING

  A concurrent stop() that ran during the window saw state=STOPPED and
  treated the call as a no-op.  start() then completed to RUNNING with
  the stop() silently lost.

Fix:
  dispatcher.start() and the STARTING transition now happen inside Lock 1,
  so no concurrent stop() can interleave between "state check" and "state
  update".  stop() either completes before start() acquires the lock (and
  is itself a no-op because state is STOPPED and start() has not begun) or
  it runs after start() releases the lock (sees RUNNING, transitions
  correctly to STOPPED).

Test strategy:
  Monkeypatch AsyncioEventDispatcher.start() to insert an asyncio.sleep(0)
  yield point.  With the old two-lock pattern this yield gave stop() an
  opening to run while the lock was not held.  With the fix the lock covers
  the entire startup sequence; stop() blocks on it and only runs after
  start() is fully committed to RUNNING — guaranteeing a proper teardown.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
from aionetx.api.network_event import NetworkEvent
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.api.udp import UdpReceiverSettings
from aionetx.implementations.asyncio_impl.asyncio_multicast_receiver import (
    AsyncioMulticastReceiver,
)
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher


class _NoopHandler:
    async def on_event(self, event: NetworkEvent) -> None:
        return None


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _unused_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# Helper: patched dispatcher that yields inside start() to expose the window
# ---------------------------------------------------------------------------


def _patch_dispatcher_start_with_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Patch AsyncioEventDispatcher.start() to yield once before proceeding.

    This creates a suspension point that — with the *old* two-lock pattern —
    would have allowed a concurrent stop() to run and observe state=STOPPED
    (treating the stop() as a no-op).  After the fix the lock is held during
    this yield, so the window no longer exists.
    """
    original_start = AsyncioEventDispatcher.start

    async def start_with_yield(self: AsyncioEventDispatcher) -> None:
        await asyncio.sleep(0)  # yield to allow other coroutines to run
        await original_start(self)

    monkeypatch.setattr(AsyncioEventDispatcher, "start", start_with_yield)


# ---------------------------------------------------------------------------
# TCP Client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tcp_client_concurrent_stop_not_lost_during_dispatcher_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    stop() called concurrently with start() must not be silently dropped.

    Pre-fix: with a yield in dispatcher.start() outside the lock, stop()
    would see state=STOPPED, be a no-op, and start() would complete to
    RUNNING — stop() effectively lost.

    Post-fix: dispatcher.start() runs inside the lock.  stop() blocks until
    start() releases the lock (with state already RUNNING), then correctly
    transitions RUNNING → STOPPED.
    """
    _patch_dispatcher_start_with_yield(monkeypatch)

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=_unused_tcp_port(),
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=_NoopHandler(),
    )

    # Schedule start() first; the single sleep(0) in start() lets stop() be
    # created *while* start() still holds _state_lock (the yield is now inside
    # the lock).  stop() therefore blocks on the lock and only runs after
    # start() has fully committed to RUNNING and released the lock.
    start_task = asyncio.create_task(client.start())
    await asyncio.sleep(0)  # advance start() to the sleep(0) inside the lock
    stop_task = asyncio.create_task(client.stop())
    await asyncio.gather(start_task, stop_task, return_exceptions=True)

    # After the fix: stop() serialised after start() → component is STOPPED.
    assert client.lifecycle_state == ComponentLifecycleState.STOPPED, (
        "stop() must not be silently lost during start() — component must end up STOPPED"
    )
    assert not client._event_dispatcher.is_running, (
        "dispatcher must be stopped when component lifecycle is STOPPED"
    )


@pytest.mark.asyncio
async def test_tcp_client_state_and_dispatcher_always_consistent_under_rapid_oscillation() -> None:
    """
    Rapid start/stop oscillation must never leave state and dispatcher inconsistent.

    Runs 10 start/stop cycles and asserts the invariant after each cycle:
      - RUNNING  → dispatcher.is_running is True
      - STOPPED  → dispatcher.is_running is False
    """
    port = _unused_tcp_port()
    # Use a real server so the client can actually connect (reconnect=False
    # means the client won't retry; we just need start() to succeed at least once).
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
        for _ in range(10):
            await client.start()
            await client.stop()
            state = client.lifecycle_state
            dispatcher_running = client._event_dispatcher.is_running
            if state == ComponentLifecycleState.RUNNING:
                assert dispatcher_running, "dispatcher must be running when state is RUNNING"
            else:
                assert not dispatcher_running, (
                    "dispatcher must not be running when state is STOPPED"
                )
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# TCP Server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tcp_server_concurrent_stop_not_lost_during_dispatcher_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    stop() called concurrently with server start() must not be silently dropped.

    Same invariant as the client test: after concurrent start()+stop() the
    component must not remain in RUNNING with an orphaned dispatcher.
    """
    _patch_dispatcher_start_with_yield(monkeypatch)

    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=_unused_tcp_port(), max_connections=64),
        event_handler=_NoopHandler(),
    )

    # Same sequencing rationale as the client test: stop() is created while
    # start() holds _state_lock (the patched sleep(0) is inside the lock), so
    # stop() blocks on the lock and only runs after start() reaches RUNNING.
    start_task = asyncio.create_task(server.start())
    await asyncio.sleep(0)  # advance start() to the sleep(0) inside the lock
    stop_task = asyncio.create_task(server.stop())
    await asyncio.gather(start_task, stop_task, return_exceptions=True)

    assert server.lifecycle_state == ComponentLifecycleState.STOPPED, (
        "stop() must not be silently lost during server start() — server must end up STOPPED"
    )
    assert not server._event_dispatcher.is_running, (
        "dispatcher must be stopped when server lifecycle is STOPPED"
    )


@pytest.mark.asyncio
async def test_tcp_server_state_and_dispatcher_always_consistent_under_rapid_oscillation() -> None:
    """Rapid server start/stop oscillation must never leave state and dispatcher inconsistent."""
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=_unused_tcp_port(), max_connections=64),
        event_handler=_NoopHandler(),
    )
    for _ in range(10):
        await server.start()
        await server.stop()
        state = server.lifecycle_state
        dispatcher_running = server._event_dispatcher.is_running
        if state == ComponentLifecycleState.RUNNING:
            assert dispatcher_running, "dispatcher must be running when state is RUNNING"
        else:
            assert not dispatcher_running, "dispatcher must not be running when state is STOPPED"


# ---------------------------------------------------------------------------
# UDP Receiver / Multicast Receiver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_udp_receiver_concurrent_stop_not_lost_during_dispatcher_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    stop() racing with UDP receiver start() must still leave the receiver stopped.

    This pins the datagram-base startup fix so UDP receivers cannot regress back
    to the same lost-stop window that previously affected TCP transports.
    """
    _patch_dispatcher_start_with_yield(monkeypatch)

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=_unused_udp_port()),
        event_handler=_NoopHandler(),
    )

    start_task = asyncio.create_task(receiver.start())
    await asyncio.sleep(0)
    stop_task = asyncio.create_task(receiver.stop())
    await asyncio.gather(start_task, stop_task, return_exceptions=True)

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED, (
        "UDP receiver stop() must not be silently lost during startup."
    )
    assert not receiver._event_dispatcher.is_running, (
        "UDP receiver dispatcher must be stopped when lifecycle is STOPPED."
    )


@pytest.mark.asyncio
async def test_multicast_receiver_concurrent_stop_not_lost_during_dispatcher_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    stop() racing with multicast receiver start() must still leave the receiver stopped.

    The shared datagram-base fix must protect both generic UDP and multicast
    receivers from the same startup TOCTOU.
    """
    _patch_dispatcher_start_with_yield(monkeypatch)

    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip="239.255.0.1",
            port=_unused_udp_port(),
            bind_ip="0.0.0.0",
            interface_ip="127.0.0.1",
        ),
        event_handler=_NoopHandler(),
    )

    start_task = asyncio.create_task(receiver.start())
    await asyncio.sleep(0)
    stop_task = asyncio.create_task(receiver.stop())
    await asyncio.gather(start_task, stop_task, return_exceptions=True)

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED, (
        "Multicast receiver stop() must not be silently lost during startup."
    )
    assert not receiver._event_dispatcher.is_running, (
        "Multicast receiver dispatcher must be stopped when lifecycle is STOPPED."
    )
