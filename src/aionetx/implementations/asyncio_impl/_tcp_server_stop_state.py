"""
Internal TCP server stop-planning state.

The server owns lifecycle and resource teardown behavior; these value objects
hold the stop plan and execution flags shared across stop helper phases.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.implementations.asyncio_impl.asyncio_heartbeat_sender import AsyncioHeartbeatSender
from aionetx.implementations.asyncio_impl.asyncio_tcp_connection import AsyncioTcpConnection


@dataclass(slots=True)
class TcpServerStopPlan:
    """Detached stop-time decisions made while holding the server state lock."""

    stop_waiter: asyncio.Future[None] | None = None
    owns_stop: bool = False
    server: asyncio.AbstractServer | None = None
    stopping_event: ComponentLifecycleChangedEvent | None = None
    should_transition_to_stopped: bool = False
    heartbeat_senders: tuple[AsyncioHeartbeatSender, ...] = ()
    connections: tuple[AsyncioTcpConnection, ...] = ()

    @property
    def waits_for_owner(self) -> bool:
        """Whether this stop call should wait for an already-running stop path."""
        return self.stop_waiter is not None and not self.owns_stop


@dataclass(frozen=True, slots=True)
class TcpServerStopProvenance:
    """Where the current stop request originated relative to active handlers."""

    handler_originated: bool = False
    active_inline_handler: bool = False

    @property
    def defers_terminal_events(self) -> bool:
        """Whether terminal publication must wait for active handler work to unwind."""
        return self.handler_originated or self.active_inline_handler


@dataclass(slots=True)
class TcpServerStopExecutionState:
    """Mutable cross-step state for a single TCP server stop execution."""

    deferred_close_waiters: tuple[asyncio.Future[None], ...] = ()
    stop_waiter_completion_deferred: bool = False
    dispatcher_stopped_from_handler_origin: bool = False
    raise_cancel_after_stop_waiter: bool = False
