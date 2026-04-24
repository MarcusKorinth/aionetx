"""
Curated settings re-exports for the public API.

This module groups the public dataclass-based transport settings so callers can
import configuration types from one stable location.
"""

from __future__ import annotations

from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.api.udp import UdpReceiverSettings, UdpSenderSettings

__all__ = (
    "TcpHeartbeatSettings",
    "MulticastReceiverSettings",
    "TcpClientSettings",
    "TcpReconnectSettings",
    "TcpServerSettings",
    "UdpReceiverSettings",
    "UdpSenderSettings",
)
