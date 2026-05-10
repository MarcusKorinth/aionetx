"""
Public protocol for managed UDP multicast receivers.

The protocol adds role-specific typing for multicast receivers plus dispatcher
diagnostics exposed by managed receive transports.
"""

from __future__ import annotations

from typing import Protocol

from aionetx.api.diagnostics import DispatcherRuntimeStats
from aionetx.api.managed_transport_protocol import ManagedTransportProtocol


class MulticastReceiverProtocol(ManagedTransportProtocol, Protocol):
    """Multicast receiver contract built on lifecycle and diagnostics capability."""

    @property
    def dispatcher_runtime_stats(self) -> DispatcherRuntimeStats:
        """Return a point-in-time dispatcher diagnostics snapshot."""
        raise NotImplementedError
