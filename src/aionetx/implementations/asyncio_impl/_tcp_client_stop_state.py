"""
Internal TCP client stop-planning state.

The client owns lifecycle behavior; these value objects hold the detached stop
plan and execution flags built while coordinating concurrent stop callers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent


@dataclass(slots=True)
class TcpClientStopPlan:
    """Detached stop-time decisions made while holding the client state lock."""

    stop_waiter: asyncio.Future[None] | None = None
    owns_stop: bool = False
    stopping_event: ComponentLifecycleChangedEvent | None = None
    supervisor_task: asyncio.Task[None] | None = None
    should_stop_dispatcher: bool = False
    await_supervisor_completion_only: bool = False
    skip_await_supervisor: bool = False
    stop_called_from_supervisor: bool = False
    cancel_supervisor_after_local_cleanup: bool = False

    @property
    def waits_for_owner(self) -> bool:
        """Whether this stop call should wait for an already-running stop path."""
        return self.stop_waiter is not None and not self.owns_stop


@dataclass(frozen=True, slots=True)
class TcpClientStopProvenance:
    """Where the current stop request originated relative to active handlers."""

    handler_originated: bool = False
    active_inline_handler: bool = False

    @property
    def defers_terminal_events(self) -> bool:
        """Whether terminal publication must wait for active handler work to unwind."""
        return self.handler_originated or self.active_inline_handler


@dataclass(slots=True)
class TcpClientStopExecutionState:
    """Mutable cross-step state for a single TCP client stop execution."""

    deferred_close_waiters: tuple[asyncio.Future[None], ...] = ()
    stop_waiter_completion_deferred: bool = False
    raise_cancel_after_stop_waiter: bool = False
