from __future__ import annotations

import pytest

from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from tests.internal_asyncio_impl_refs import (
    apply_stopped_transition_if_stopping,
    apply_stopping_transition_if_active,
)


def test_apply_stopping_transition_if_active_applies_for_starting_or_running() -> None:
    state = ComponentLifecycleState.STARTING
    applied: list[tuple[ComponentLifecycleState, ComponentLifecycleState]] = []

    def _get_state() -> ComponentLifecycleState:
        return state

    def _apply(target: ComponentLifecycleState):
        nonlocal state
        previous = state
        state = target
        applied.append((previous, target))
        return (previous, target)

    event = apply_stopping_transition_if_active(get_state=_get_state, apply_transition=_apply)

    assert event == (ComponentLifecycleState.STARTING, ComponentLifecycleState.STOPPING)
    assert applied == [(ComponentLifecycleState.STARTING, ComponentLifecycleState.STOPPING)]


def test_apply_stopping_transition_if_active_is_noop_when_already_stopped() -> None:
    state = ComponentLifecycleState.STOPPED

    def _get_state() -> ComponentLifecycleState:
        return state

    def _apply(_target: ComponentLifecycleState):
        raise AssertionError("apply_transition should not be called")

    event = apply_stopping_transition_if_active(get_state=_get_state, apply_transition=_apply)

    assert event is None


def test_apply_stopped_transition_if_stopping_applies_only_from_stopping() -> None:
    state = ComponentLifecycleState.STOPPING
    applied: list[tuple[ComponentLifecycleState, ComponentLifecycleState]] = []

    def _get_state() -> ComponentLifecycleState:
        return state

    def _apply(target: ComponentLifecycleState):
        nonlocal state
        previous = state
        state = target
        applied.append((previous, target))
        return (previous, target)

    event = apply_stopped_transition_if_stopping(get_state=_get_state, apply_transition=_apply)

    assert event == (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
    assert applied == [(ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)]


def test_apply_stopped_transition_if_stopping_is_noop_outside_stopping() -> None:
    state = ComponentLifecycleState.RUNNING

    def _get_state() -> ComponentLifecycleState:
        return state

    def _apply(_target: ComponentLifecycleState):
        raise AssertionError("apply_transition should not be called")

    event = apply_stopped_transition_if_stopping(get_state=_get_state, apply_transition=_apply)

    assert event is None


def test_lifecycle_invariant_stopping_transition_is_not_reentrant_once_stopped() -> None:
    state = ComponentLifecycleState.STOPPED

    def _get_state() -> ComponentLifecycleState:
        return state

    def _apply(_target: ComponentLifecycleState):
        raise AssertionError("apply_transition should not be called after STOPPED is published")

    event = apply_stopping_transition_if_active(get_state=_get_state, apply_transition=_apply)

    assert event is None


@pytest.mark.parametrize(
    ("state", "helper"),
    [
        (
            ComponentLifecycleState.STARTING,
            apply_stopping_transition_if_active,
        ),
        (
            ComponentLifecycleState.RUNNING,
            apply_stopping_transition_if_active,
        ),
        (
            ComponentLifecycleState.STOPPING,
            apply_stopped_transition_if_stopping,
        ),
    ],
)
def test_transition_helpers_fail_fast_if_transition_returns_none(
    state: ComponentLifecycleState,
    helper,
) -> None:
    with pytest.raises(RuntimeError, match="expected .* transition event"):
        helper(
            get_state=lambda: state,
            apply_transition=lambda _target: None,
        )
