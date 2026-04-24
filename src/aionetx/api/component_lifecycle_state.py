"""
Lifecycle-state enum for managed network components.

These states apply to top-level managed transports such as TCP clients,
servers, and datagram receivers.
"""

from __future__ import annotations

from enum import Enum


class ComponentLifecycleState(str, Enum):
    """Lifecycle state for top-level network components."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
