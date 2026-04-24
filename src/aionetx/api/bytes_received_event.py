"""
Event type for raw transport payload delivery.

The dataclass in this module is emitted by stream, UDP, and multicast receivers
whenever bytes are read from the underlying transport.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BytesReceivedEvent:
    """
    Event emitted when raw bytes are read from a transport.

    Attributes:
        resource_id: Identifier of the transport/source stream.
        data: Raw transport payload bytes with no framing/parsing.
        remote_host: Optional remote sender host metadata (typically present for
            UDP/multicast datagrams, may be None for stream transports).
        remote_port: Optional remote sender port metadata (typically present for
            UDP/multicast datagrams, may be None for stream transports).
    """

    resource_id: str
    data: bytes
    remote_host: str | None = None
    remote_port: int | None = None
