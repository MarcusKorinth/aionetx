"""
Shared capability protocol for bytes-like send operations.

This module defines the narrowest send contract used across stream and
datagram transports. Role-specific protocols layer lifecycle, addressing, and
connection semantics on top of this capability.
"""

from __future__ import annotations

from typing import Protocol

from aionetx.api.bytes_like import BytesLike


class ByteSenderProtocol(Protocol):
    """
    Capability protocol for components that can send raw bytes-like payloads.

    This capability means only: "can send bytes-like payloads". It does not
    imply shared addressing semantics (connected stream vs datagram target),
    shared lifecycle model, or shared connection supervision behavior.
    Use role-specific protocols when you need transport-specific guarantees.
    """

    async def send(self, data: BytesLike) -> None:
        """
        Send raw bytes-like payload.

        Implementations may differ in lifecycle, addressing, and transport
        failure semantics. A successful return means the payload was accepted
        by the local transport path; it does not guarantee remote delivery.
        Role-specific protocols document their concrete exceptions.
        """
        raise NotImplementedError
