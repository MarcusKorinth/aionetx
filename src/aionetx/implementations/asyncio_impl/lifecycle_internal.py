"""Shared helpers for managed component lifecycle transition publication."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher


class LifecycleRole(str, Enum):
    """Managed component role used to resolve transition policy."""

    TCP_CLIENT = "tcp_client"
    TCP_SERVER = "tcp_server"
    UDP_RECEIVER = "udp_receiver"
    MULTICAST_RECEIVER = "multicast_receiver"


_BASE_TRANSITIONS = {
    ComponentLifecycleState.STOPPED: {ComponentLifecycleState.STARTING},
    ComponentLifecycleState.STARTING: {
        ComponentLifecycleState.RUNNING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    },
    ComponentLifecycleState.RUNNING: {ComponentLifecycleState.STOPPING},
    ComponentLifecycleState.STOPPING: {ComponentLifecycleState.STOPPED},
}

_ROLES_USING_BASE_POLICY = frozenset(LifecycleRole)


def assert_legal_lifecycle_transition(
    *,
    component_name: str,
    role: LifecycleRole,
    previous: ComponentLifecycleState,
    target: ComponentLifecycleState,
) -> None:
    """Validate lifecycle transition against the policy for ``role``."""
    if previous == target:
        return
    if role not in _ROLES_USING_BASE_POLICY:
        raise RuntimeError(f"Unsupported lifecycle role for {component_name}: {role!r}.")
    allowed = _BASE_TRANSITIONS.get(previous, set())
    if target not in allowed:
        raise RuntimeError(
            f"Illegal lifecycle transition for {component_name} ({role.value}): "
            f"{previous.value} -> {target.value}."
        )


class LifecycleTransitionPublisher:
    """
    Internal helper for managed lifecycle transition publication.

    This centralizes shared ``apply transition -> emit lifecycle event`` plumbing
    while keeping transition timing at call sites explicit and role-specific.
    """

    def __init__(
        self,
        *,
        component_name: str,
        resource_id: str,
        role: LifecycleRole,
        get_state: Callable[[], ComponentLifecycleState],
        set_state: Callable[[ComponentLifecycleState], None],
        on_state_applied: Callable[[ComponentLifecycleState], None] | None = None,
    ) -> None:
        self._component_name = component_name
        self._resource_id = resource_id
        self._role = role
        self._get_state = get_state
        self._set_state = set_state
        self._on_state_applied = on_state_applied

    def apply(self, target: ComponentLifecycleState) -> ComponentLifecycleChangedEvent | None:
        """
        Apply a validated lifecycle transition and build publishable event.

        Arguments:
            target (ComponentLifecycleState): Requested lifecycle state.

        Returns:
            ComponentLifecycleChangedEvent | None: Transition event ready for
            emission, or ``None`` when state is unchanged.

        Raises:
            RuntimeError: If the role-specific transition is illegal.
        """
        previous = self._get_state()
        if previous == target:
            return None
        assert_legal_lifecycle_transition(
            component_name=self._component_name,
            role=self._role,
            previous=previous,
            target=target,
        )
        self._set_state(target)
        if self._on_state_applied is not None:
            self._on_state_applied(target)
        return ComponentLifecycleChangedEvent(
            resource_id=self._resource_id,
            previous=previous,
            current=target,
        )


async def emit_lifecycle_event(
    *,
    dispatcher: AsyncioEventDispatcher,
    event: ComponentLifecycleChangedEvent | None,
) -> None:
    """Emit lifecycle transition event when one is available."""
    if event is None:
        return
    await dispatcher.emit(event)


def apply_stopping_transition_if_active(
    *,
    get_state: Callable[[], ComponentLifecycleState],
    apply_transition: Callable[[ComponentLifecycleState], ComponentLifecycleChangedEvent | None],
) -> ComponentLifecycleChangedEvent | None:
    """Apply ``STOPPING`` when component is currently active."""
    state = get_state()
    if state not in (ComponentLifecycleState.RUNNING, ComponentLifecycleState.STARTING):
        return None
    event = apply_transition(ComponentLifecycleState.STOPPING)
    if event is None:
        raise RuntimeError(
            "Lifecycle transition helper expected STOPPING transition event, "
            f"but got None while current state was {state.value}."
        )
    return event


def apply_stopped_transition_if_stopping(
    *,
    get_state: Callable[[], ComponentLifecycleState],
    apply_transition: Callable[[ComponentLifecycleState], ComponentLifecycleChangedEvent | None],
) -> ComponentLifecycleChangedEvent | None:
    """Apply ``STOPPED`` only from ``STOPPING``."""
    if get_state() != ComponentLifecycleState.STOPPING:
        return None
    event = apply_transition(ComponentLifecycleState.STOPPED)
    if event is None:
        raise RuntimeError(
            "Lifecycle transition helper expected STOPPED transition event, "
            "but got None while current state was stopping."
        )
    return event
