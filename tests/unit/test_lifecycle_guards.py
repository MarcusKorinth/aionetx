from __future__ import annotations

import pytest

from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from tests.internal_asyncio_impl_refs import (
    LifecycleRole,
    assert_legal_lifecycle_transition,
)


@pytest.mark.parametrize("role", list(LifecycleRole))
def test_lifecycle_policy_is_shared_across_managed_roles(role: LifecycleRole) -> None:
    assert_legal_lifecycle_transition(
        component_name=f"component/{role.value}",
        role=role,
        previous=ComponentLifecycleState.STOPPED,
        target=ComponentLifecycleState.STARTING,
    )
    assert_legal_lifecycle_transition(
        component_name=f"component/{role.value}",
        role=role,
        previous=ComponentLifecycleState.STARTING,
        target=ComponentLifecycleState.RUNNING,
    )
    assert_legal_lifecycle_transition(
        component_name=f"component/{role.value}",
        role=role,
        previous=ComponentLifecycleState.RUNNING,
        target=ComponentLifecycleState.STOPPING,
    )
    assert_legal_lifecycle_transition(
        component_name=f"component/{role.value}",
        role=role,
        previous=ComponentLifecycleState.STOPPING,
        target=ComponentLifecycleState.STOPPED,
    )


@pytest.mark.parametrize("role", list(LifecycleRole))
def test_lifecycle_policy_rejects_running_to_stopped_for_all_roles(role: LifecycleRole) -> None:
    with pytest.raises(RuntimeError, match="Illegal lifecycle transition"):
        assert_legal_lifecycle_transition(
            component_name=f"component/{role.value}",
            role=role,
            previous=ComponentLifecycleState.RUNNING,
            target=ComponentLifecycleState.STOPPED,
        )


@pytest.mark.parametrize("role", list(LifecycleRole))
@pytest.mark.parametrize(
    ("previous", "target"),
    [
        (ComponentLifecycleState.STOPPED, ComponentLifecycleState.RUNNING),
        (ComponentLifecycleState.STOPPED, ComponentLifecycleState.STOPPING),
        (ComponentLifecycleState.RUNNING, ComponentLifecycleState.STARTING),
        (ComponentLifecycleState.STOPPING, ComponentLifecycleState.RUNNING),
    ],
)
def test_lifecycle_policy_rejects_invalid_cross_state_transitions_for_all_roles(
    role: LifecycleRole,
    previous: ComponentLifecycleState,
    target: ComponentLifecycleState,
) -> None:
    with pytest.raises(RuntimeError, match="Illegal lifecycle transition"):
        assert_legal_lifecycle_transition(
            component_name=f"component/{role.value}",
            role=role,
            previous=previous,
            target=target,
        )
