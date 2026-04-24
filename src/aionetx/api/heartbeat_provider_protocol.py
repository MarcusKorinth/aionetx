"""
Protocol for user-defined heartbeat payload generation.

Implementations receive a lightweight request describing the active connection
and return a decision about whether bytes should be sent for that tick.
"""

from __future__ import annotations

from typing import Protocol

from aionetx.api.heartbeat import HeartbeatRequest, HeartbeatResult


class HeartbeatProviderProtocol(Protocol):
    """Contract for creating heartbeat payloads on demand."""

    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        """
        Build the next heartbeat decision for a connection.

        Args:
            request: Context identifying which connection is requesting a heartbeat.

        Returns:
            A ``HeartbeatResult`` indicating whether bytes should be sent and which payload.
        """
        ...
