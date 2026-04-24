"""
Curated protocol re-exports for the public API.

This module groups shared capability and role-specific protocol types so users
can import the protocol layer from one stable location.
"""

from __future__ import annotations

from aionetx.api.byte_sender_protocol import ByteSenderProtocol
from aionetx.api.connection_protocol import ConnectionProtocol
from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.managed_transport_protocol import ManagedTransportProtocol
from aionetx.api.multicast_receiver_protocol import MulticastReceiverProtocol
from aionetx.api.network_event_handler_protocol import NetworkEventHandlerProtocol
from aionetx.api.network_factory import NetworkFactory
from aionetx.api.tcp_client import TcpClientProtocol
from aionetx.api.tcp_server import TcpServerProtocol
from aionetx.api.udp import UdpReceiverProtocol, UdpSenderProtocol

__all__ = (
    "ByteSenderProtocol",
    "ConnectionProtocol",
    "HeartbeatProviderProtocol",
    "ManagedTransportProtocol",
    "MulticastReceiverProtocol",
    "NetworkEventHandlerProtocol",
    "NetworkFactory",
    "TcpClientProtocol",
    "TcpServerProtocol",
    "UdpReceiverProtocol",
    "UdpSenderProtocol",
)
