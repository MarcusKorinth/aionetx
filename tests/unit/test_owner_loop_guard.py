"""
Tests for assert_running_on_owner_loop and the cross-loop guards on transports.

These checks pin the fail-fast behavior around event-loop ownership.

Coverage strategy:
- Unit-test the helper directly for the three cases: no running loop, same
  loop (no-op), and a different loop (error).
- Smoke-test that each public transport method is wired to the guard by
  supplying a manually created event loop as the owner; calling the method
  from the *test* loop (which is a different loop) must raise RuntimeError.
"""

from __future__ import annotations

import asyncio

import pytest

from aionetx.implementations.asyncio_impl.runtime_utils import assert_running_on_owner_loop


# ---------------------------------------------------------------------------
# Direct unit tests for the helper function
# ---------------------------------------------------------------------------


def test_assert_running_on_owner_loop_raises_when_no_running_loop() -> None:
    """RuntimeError must be raised synchronously when there is no running loop."""
    with pytest.raises(RuntimeError, match="No running loop detected"):
        assert_running_on_owner_loop(class_name="MyClass", owner_loop=None)


@pytest.mark.asyncio
async def test_assert_running_on_owner_loop_passes_for_same_loop() -> None:
    """Guard must be a no-op when called from the owner loop."""
    loop = asyncio.get_running_loop()
    assert_running_on_owner_loop(class_name="MyClass", owner_loop=loop)


@pytest.mark.asyncio
async def test_assert_running_on_owner_loop_returns_current_loop() -> None:
    """
    Return value must be the running loop so callers can pin it on first use.

    Also verifies the guard passes (no raise) when owner_loop is None — the
    "not yet bound" case.
    """
    loop = asyncio.get_running_loop()
    returned = assert_running_on_owner_loop(class_name="MyClass", owner_loop=None)
    assert returned is loop


@pytest.mark.asyncio
async def test_assert_running_on_owner_loop_raises_for_different_loop() -> None:
    """RuntimeError must be raised when the owner loop differs from the caller's loop."""
    foreign_loop = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError, match="different event loop"):
            assert_running_on_owner_loop(class_name="MyClass", owner_loop=foreign_loop)
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_assert_running_on_owner_loop_message_contains_class_name() -> None:
    """Error message must include the class_name so callers can identify the component."""
    foreign_loop = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError, match="SpecialTransport"):
            assert_running_on_owner_loop(class_name="SpecialTransport", owner_loop=foreign_loop)
    finally:
        foreign_loop.close()


# ---------------------------------------------------------------------------
# Wiring smoke tests: each transport method must forward to the guard
# ---------------------------------------------------------------------------
# These tests inject a *foreign* event loop as the owner so that any method
# call from the test loop (a different loop) triggers the cross-loop guard.
# This confirms the guard is actually called — not just that the helper works.


@pytest.mark.asyncio
async def test_tcp_client_start_raises_on_wrong_loop() -> None:
    from aionetx.api.tcp_client import TcpClientSettings
    from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
    from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient

    class _Noop:
        async def on_event(self, event: object) -> None:
            return None

    foreign_loop = asyncio.new_event_loop()
    try:
        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=9999,
                reconnect=TcpReconnectSettings(enabled=False),
            ),
            event_handler=_Noop(),
        )
        client._owner_loop = foreign_loop  # simulate: owned by a different loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await client.start()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_tcp_client_stop_raises_on_wrong_loop() -> None:
    from aionetx.api.tcp_client import TcpClientSettings
    from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
    from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient

    class _Noop:
        async def on_event(self, event: object) -> None:
            return None

    foreign_loop = asyncio.new_event_loop()
    try:
        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=9999,
                reconnect=TcpReconnectSettings(enabled=False),
            ),
            event_handler=_Noop(),
        )
        client._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await client.stop()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_tcp_client_wait_until_connected_raises_on_wrong_loop() -> None:
    from aionetx.api.tcp_client import TcpClientSettings
    from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
    from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient

    class _Noop:
        async def on_event(self, event: object) -> None:
            return None

    foreign_loop = asyncio.new_event_loop()
    try:
        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=9999,
                reconnect=TcpReconnectSettings(enabled=False),
            ),
            event_handler=_Noop(),
        )
        client._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await client.wait_until_connected()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_tcp_server_start_raises_on_wrong_loop() -> None:
    from aionetx.api.tcp_server import TcpServerSettings
    from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer

    class _Noop:
        async def on_event(self, event: object) -> None:
            return None

    foreign_loop = asyncio.new_event_loop()
    try:
        server = AsyncioTcpServer(
            settings=TcpServerSettings(host="127.0.0.1", port=9999, max_connections=64),
            event_handler=_Noop(),
        )
        server._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await server.start()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_tcp_server_stop_raises_on_wrong_loop() -> None:
    from aionetx.api.tcp_server import TcpServerSettings
    from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer

    class _Noop:
        async def on_event(self, event: object) -> None:
            return None

    foreign_loop = asyncio.new_event_loop()
    try:
        server = AsyncioTcpServer(
            settings=TcpServerSettings(host="127.0.0.1", port=9999, max_connections=64),
            event_handler=_Noop(),
        )
        server._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await server.stop()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_tcp_server_wait_until_running_raises_on_wrong_loop() -> None:
    from aionetx.api.tcp_server import TcpServerSettings
    from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer

    class _Noop:
        async def on_event(self, event: object) -> None:
            return None

    foreign_loop = asyncio.new_event_loop()
    try:
        server = AsyncioTcpServer(
            settings=TcpServerSettings(host="127.0.0.1", port=9999, max_connections=64),
            event_handler=_Noop(),
        )
        server._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await server.wait_until_running()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_tcp_server_broadcast_raises_on_wrong_loop() -> None:
    from aionetx.api.tcp_server import TcpServerSettings
    from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer

    class _Noop:
        async def on_event(self, event: object) -> None:
            return None

    foreign_loop = asyncio.new_event_loop()
    try:
        server = AsyncioTcpServer(
            settings=TcpServerSettings(host="127.0.0.1", port=9999, max_connections=64),
            event_handler=_Noop(),
        )
        server._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await server.broadcast(b"hello")
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_udp_receiver_start_raises_on_wrong_loop() -> None:
    from aionetx.api.udp import UdpReceiverSettings
    from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver

    class _Noop:
        async def on_event(self, event: object) -> None:
            return None

    foreign_loop = asyncio.new_event_loop()
    try:
        receiver = AsyncioUdpReceiver(
            settings=UdpReceiverSettings(host="127.0.0.1", port=9999),
            event_handler=_Noop(),
        )
        receiver._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await receiver.start()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_udp_receiver_stop_raises_on_wrong_loop() -> None:
    from aionetx.api.udp import UdpReceiverSettings
    from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver

    class _Noop:
        async def on_event(self, event: object) -> None:
            return None

    foreign_loop = asyncio.new_event_loop()
    try:
        receiver = AsyncioUdpReceiver(
            settings=UdpReceiverSettings(host="127.0.0.1", port=9999),
            event_handler=_Noop(),
        )
        receiver._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await receiver.stop()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_multicast_receiver_start_raises_on_wrong_loop() -> None:
    from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
    from aionetx.implementations.asyncio_impl.asyncio_multicast_receiver import (
        AsyncioMulticastReceiver,
    )

    class _Noop:
        async def on_event(self, event: object) -> None:
            return None

    foreign_loop = asyncio.new_event_loop()
    try:
        receiver = AsyncioMulticastReceiver(
            settings=MulticastReceiverSettings(group_ip="239.0.0.1", port=9999),
            event_handler=_Noop(),
        )
        receiver._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await receiver.start()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_multicast_receiver_stop_raises_on_wrong_loop() -> None:
    from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
    from aionetx.implementations.asyncio_impl.asyncio_multicast_receiver import (
        AsyncioMulticastReceiver,
    )

    class _Noop:
        async def on_event(self, event: object) -> None:
            return None

    foreign_loop = asyncio.new_event_loop()
    try:
        receiver = AsyncioMulticastReceiver(
            settings=MulticastReceiverSettings(group_ip="239.0.0.1", port=9999),
            event_handler=_Noop(),
        )
        receiver._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await receiver.stop()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_udp_sender_send_raises_on_wrong_loop() -> None:
    from aionetx.api.udp import UdpSenderSettings
    from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender

    foreign_loop = asyncio.new_event_loop()
    try:
        sender = AsyncioUdpSender(
            settings=UdpSenderSettings(default_host="127.0.0.1", default_port=9999),
        )
        sender._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await sender.send(b"hello")
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_udp_sender_stop_raises_on_wrong_loop() -> None:
    from aionetx.api.udp import UdpSenderSettings
    from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender

    foreign_loop = asyncio.new_event_loop()
    try:
        sender = AsyncioUdpSender(
            settings=UdpSenderSettings(default_host="127.0.0.1", default_port=9999),
        )
        sender._owner_loop = foreign_loop
        with pytest.raises(RuntimeError, match="different event loop"):
            await sender.stop()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_udp_sender_stop_is_idempotent_from_any_loop_when_already_closed() -> None:
    """
    stop() must not fire the guard when the sender is already closed.

    The closed-check short-circuit (``if self._closed: return``) must fire
    *before* the owner-loop guard so that defensive ``stop()`` calls on an
    already-stopped sender never raise even when called from a context that
    would otherwise trigger the cross-loop check.
    """
    from aionetx.api.udp import UdpSenderSettings
    from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender

    foreign_loop = asyncio.new_event_loop()
    try:
        sender = AsyncioUdpSender(
            settings=UdpSenderSettings(default_host="127.0.0.1", default_port=9999),
        )
        sender._closed = True  # simulate: already stopped
        sender._owner_loop = foreign_loop  # would trigger guard if reached
        # Should not raise — closed guard fires first
        await sender.stop()
    finally:
        foreign_loop.close()
