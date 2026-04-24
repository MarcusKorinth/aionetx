"""
Event type describing managed component lifecycle transitions.

Managed transports emit this dataclass whenever their component lifecycle state
changes, for example during startup, steady-state operation, or shutdown.
"""

from __future__ import annotations

from dataclasses import dataclass

from aionetx.api.component_lifecycle_state import ComponentLifecycleState


@dataclass(frozen=True, slots=True)
class ComponentLifecycleChangedEvent:
    """
    Event emitted when a managed component lifecycle state changes.

    Attributes:
        resource_id: Identifier of the component whose lifecycle changed.
        previous: Lifecycle state before the transition.
        current: Lifecycle state after the transition.
        reason: Optional implementation-supplied explanation for the transition.
    """

    resource_id: str
    previous: ComponentLifecycleState
    current: ComponentLifecycleState
    reason: str | None = None
