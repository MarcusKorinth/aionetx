"""
Unit tests for __version__ and __repr__.

Ensures the canonical user-facing introspection surfaces are correct for all
four transport implementations.
"""

from __future__ import annotations

import aionetx
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.api.udp import UdpReceiverSettings, UdpSenderSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver
from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender


# ---------------------------------------------------------------------------
# __version__
# ---------------------------------------------------------------------------


def test_version_attribute_exists() -> None:
    """aionetx.__version__ must exist and be a non-empty string."""
    assert hasattr(aionetx, "__version__")
    assert isinstance(aionetx.__version__, str)
    assert aionetx.__version__, "aionetx.__version__ must not be empty"


def test_version_in_all() -> None:
    """__version__ must be in aionetx.__all__ so it is part of the public API."""
    assert "__version__" in aionetx.__all__


def test_version_format_is_dotted_numeric() -> None:
    """Version string must look like PEP 440 (at least MAJOR.MINOR.PATCH)."""
    parts = aionetx.__version__.split(".")
    assert len(parts) >= 3, (
        f"__version__ {aionetx.__version__!r} does not contain at least 3 dot-separated components"
    )
    # Each leading component must be a digit string.
    for part in parts[:3]:
        assert part.isdigit() or part.split("a")[0].isdigit() or part.split("b")[0].isdigit(), (
            f"Version component {part!r} is not numeric"
        )


# ---------------------------------------------------------------------------
# __repr__ on all four transport implementations
# ---------------------------------------------------------------------------


class _Noop:
    async def on_event(self, event: object) -> None:
        return None


def test_tcp_client_repr_contains_expected_fields() -> None:
    """AsyncioTcpClient.__repr__ must include host, port, and lifecycle state."""
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=5000,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=_Noop(),
    )
    r = repr(client)
    assert "AsyncioTcpClient" in r
    assert "127.0.0.1" in r
    assert "5000" in r
    assert "stopped" in r.lower()


def test_tcp_server_repr_contains_expected_fields() -> None:
    """AsyncioTcpServer.__repr__ must include host, port, and lifecycle state."""
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="0.0.0.0", port=8080, max_connections=64),
        event_handler=_Noop(),
    )
    r = repr(server)
    assert "AsyncioTcpServer" in r
    assert "0.0.0.0" in r
    assert "8080" in r
    assert "stopped" in r.lower()


def test_udp_receiver_repr_contains_expected_fields() -> None:
    """AsyncioUdpReceiver.__repr__ must include host, port, and lifecycle state."""
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=6000),
        event_handler=_Noop(),
    )
    r = repr(receiver)
    assert "AsyncioUdpReceiver" in r
    assert "127.0.0.1" in r
    assert "6000" in r
    assert "stopped" in r.lower()


def test_udp_sender_repr_contains_expected_fields() -> None:
    """AsyncioUdpSender.__repr__ must include default_host, default_port, and closed status."""
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="192.168.1.1", default_port=9000),
    )
    r = repr(sender)
    assert "AsyncioUdpSender" in r
    assert "192.168.1.1" in r
    assert "9000" in r
    assert "False" in r  # closed=False before stop()


def test_repr_is_str_type() -> None:
    """repr() on all four implementations must return a plain str."""
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1", port=1234, reconnect=TcpReconnectSettings(enabled=False)
        ),
        event_handler=_Noop(),
    )
    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=1235, max_connections=64),
        event_handler=_Noop(),
    )
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=1236),
        event_handler=_Noop(),
    )
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=1237),
    )
    for obj in (client, server, receiver, sender):
        assert isinstance(repr(obj), str), f"repr({type(obj).__name__}) did not return str"
