"""
Public protocol for managed UDP multicast receivers.

The protocol adds no new methods beyond ``ManagedTransportProtocol``; its
purpose is to give multicast receivers an explicit role-specific API type.
"""

from __future__ import annotations

from typing import Protocol

from aionetx.api.managed_transport_protocol import ManagedTransportProtocol


class MulticastReceiverProtocol(ManagedTransportProtocol, Protocol):
    """Multicast receiver contract built on managed lifecycle capability."""
