"""
Connection lifecycle event dataclasses.

These events describe when a logical connection becomes available and when it
reaches its terminal closed state.
"""

from __future__ import annotations

from dataclasses import dataclass

from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.connection_lifecycle import ConnectionState


@dataclass(frozen=True, slots=True)
class ConnectionOpenedEvent:
    """
    Event emitted when a connection becomes available for data transfer.

    Attributes:
        resource_id (str): Identifier of the opened connection.
        metadata (ConnectionMetadata): Metadata snapshot captured at open time.
    """

    resource_id: str
    metadata: ConnectionMetadata

    def __post_init__(self) -> None:
        if self.resource_id != self.metadata.connection_id:
            raise ValueError("resource_id must match metadata.connection_id.")


@dataclass(frozen=True, slots=True)
class ConnectionClosedEvent:
    """
    Event emitted exactly once when a connection transitions to closed.

    Attributes:
        resource_id (str): Identifier of the connection that was closed.
        previous_state (ConnectionState): Lifecycle state immediately before closure.
        metadata (ConnectionMetadata | None): Transport metadata snapshot when
            available from the implementation. This is populated for normal TCP
            connection closes and for datagram receivers after a successful
            start/open sequence. It may be ``None`` when close occurs before
            metadata could be established.
    """

    resource_id: str
    previous_state: ConnectionState
    metadata: ConnectionMetadata | None = None


@dataclass(frozen=True, slots=True)
class ConnectionRejectedEvent:
    """
    Event emitted when a TCP server rejects an incoming connection attempt.

    Attributes:
        resource_id: Identifier of the rejecting server component.
        connection_id: Stable identifier assigned to the rejected attempt.
        reason: Machine-readable rejection reason.
        remote_host: Best-effort remote peer host when available.
        remote_port: Best-effort remote peer port when available.
    """

    resource_id: str
    connection_id: str
    reason: str
    remote_host: str | None = None
    remote_port: int | None = None
