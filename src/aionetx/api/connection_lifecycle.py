"""
Enums describing connection roles and connection-level lifecycle states.

These enums are shared by TCP connections and datagram receiver abstractions
when they report logical connection identity and state.
"""

from __future__ import annotations

from enum import Enum


class ConnectionRole(str, Enum):
    """Logical role assigned to a connection for lifecycle/event reporting."""

    CLIENT = "client"
    SERVER = "server"
    MULTICAST_RECEIVER = "multicast_receiver"
    UDP_RECEIVER = "udp_receiver"


class ConnectionState(str, Enum):
    """Lifecycle state used by connection implementations."""

    CREATED = "created"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CLOSING = "closing"
    CLOSED = "closed"
