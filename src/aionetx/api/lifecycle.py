"""
Curated lifecycle-model re-exports for the public API.

This module groups component and connection lifecycle types so advanced users
can import the shared state model from one stable place.
"""

from __future__ import annotations

from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_lifecycle import ConnectionRole, ConnectionState
from aionetx.api.connection_metadata import ConnectionMetadata

__all__ = (
    "ComponentLifecycleState",
    "ConnectionMetadata",
    "ConnectionRole",
    "ConnectionState",
)
