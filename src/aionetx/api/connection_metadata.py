"""
Connection metadata model shared by emitted lifecycle events.

The dataclass in this module carries stable connection identity plus optional
local and remote addressing details when implementations can provide them.
"""

from __future__ import annotations

from dataclasses import dataclass

from aionetx.api.connection_lifecycle import ConnectionRole


@dataclass(frozen=True, slots=True)
class ConnectionMetadata:
    """
    Transport-level metadata describing one logical connection.

    Attributes:
        connection_id (str): Stable ID used in all events for this connection.
        role (ConnectionRole): Role of this endpoint (client/server/multicast receiver).
        local_host (str | None): Local interface address when available.
        local_port (int | None): Local interface port when available.
        remote_host (str | None): Peer address when available.
        remote_port (int | None): Peer port when available.
    """

    connection_id: str
    role: ConnectionRole
    local_host: str | None = None
    local_port: int | None = None
    remote_host: str | None = None
    remote_port: int | None = None
