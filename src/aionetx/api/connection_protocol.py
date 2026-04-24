"""
Shared public protocol for logical network connections.

This module defines the stable surface that concrete TCP connection
implementations expose to callers and to higher-level transport abstractions.
"""

from __future__ import annotations

from typing import Protocol

from aionetx.api.byte_sender_protocol import ByteSenderProtocol
from aionetx.api.bytes_like import BytesLike
from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.connection_lifecycle import ConnectionState


class ConnectionProtocol(ByteSenderProtocol, Protocol):
    """
    Role-specific connection contract built on shared send capability.

    A connection models one logical TCP session owned by a managed client or
    server. Identifiers and metadata are part of the observable contract and
    are used to correlate send/receive/lifecycle events.
    """

    @property
    def connection_id(self) -> str:
        """Return a stable identifier used for correlation in emitted events."""
        ...

    @property
    def role(self) -> ConnectionRole:
        """Return the logical role of the connection endpoint."""
        ...

    @property
    def state(self) -> ConnectionState:
        """Return the current lifecycle state."""
        ...

    @property
    def is_connected(self) -> bool:
        """Return ``True`` when ``send`` is expected to succeed."""
        ...

    @property
    def metadata(self) -> ConnectionMetadata:
        """Return immutable transport metadata for this connection."""
        ...

    async def send(self, data: BytesLike) -> None:
        """
        Send raw bytes-like payload over the transport.

        Args:
            data: Bytes-like payload to write to the peer.

        Raises:
            ConnectionClosedError: When the connection is already known to be closed.
            TypeError: If ``data`` is not bytes-like.
            OSError: When the underlying transport accepts the write but later
                reports a socket or stream failure while flushing.
        """
        ...

    async def close(self) -> None:
        """
        Close the connection.

        Implementations should be safe to call repeatedly. After close, future
        ``send()`` calls must fail rather than silently succeeding.
        """
        ...
