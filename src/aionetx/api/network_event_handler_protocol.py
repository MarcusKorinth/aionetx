"""
Public callback protocol for the emitted network event stream.

Every managed component delivers lifecycle, data, and error reporting through a
single async ``on_event(event)`` method defined here.
"""

from __future__ import annotations

from typing import Protocol

from aionetx.api.network_event import NetworkEvent


class NetworkEventHandlerProtocol(Protocol):
    """Application callback interface for network lifecycle and transport events."""

    async def on_event(self, event: NetworkEvent) -> None:
        """Handle one emitted transport event."""
        raise NotImplementedError
