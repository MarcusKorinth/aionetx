"""
Internal datagram receiver runtime and stop-planning state.

The receiver base owns behavior; this module owns the mutable state containers
and stop-plan value objects shared by UDP and multicast receivers.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from dataclasses import dataclass

from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_events import ConnectionClosedEvent
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.connection_metadata import ConnectionMetadata

SocketCleanup = Callable[[socket.socket], None]


@dataclass(slots=True)
class DatagramRuntimeState:
    """Mutable runtime fields shared by datagram receiver implementations."""

    running: bool = False
    sock: socket.socket | None = None
    task: asyncio.Task[None] | None = None
    lifecycle_state: ComponentLifecycleState = ComponentLifecycleState.STOPPED
    connection_state: ConnectionState = ConnectionState.CREATED
    metadata: ConnectionMetadata | None = None
    recv_fallback_warning_emitted: bool = False
    stop_waiter: asyncio.Future[None] | None = None
    stop_owner_task: asyncio.Task[object] | None = None
    opening_event_task: asyncio.Task[object] | None = None
    deferred_close_event: ConnectionClosedEvent | None = None
    deferred_close_event_waiter: asyncio.Future[None] | None = None
    deferred_close_publish_task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class DatagramStopSnapshot:
    """
    Detached stop plan consumed after leaving the state lock.

    Attributes:
        stop_dispatcher: Whether dispatcher shutdown must run after stop-path events.
        should_transition_to_stopped: Whether STOPPED should be published after teardown.
        should_emit_closed_event: Whether the stop path should publish ``ConnectionClosedEvent``.
        previous_connection_state: Connection state to report in the close event.
        task: Receive task detached from runtime state.
        sock: Socket detached from runtime state.
        stopping_event: Precomputed STOPPING lifecycle event, if any.
        stop_waiter: Shared waiter for overlapping stop callers.
        owns_stop: Whether this snapshot owns teardown and terminal publication.
    """

    stop_dispatcher: bool = False
    should_transition_to_stopped: bool = False
    should_emit_closed_event: bool = False
    previous_connection_state: ConnectionState = ConnectionState.CREATED
    task: asyncio.Task[None] | None = None
    sock: socket.socket | None = None
    cancel_task: bool = True
    stopping_event: ComponentLifecycleChangedEvent | None = None
    stop_waiter: asyncio.Future[None] | None = None
    owns_stop: bool = False

    @property
    def waits_for_owner(self) -> bool:
        """Whether this stop call should wait for an already-running stop path."""
        return self.stop_waiter is not None and not self.owns_stop

    @property
    def is_noop(self) -> bool:
        """Whether stop planning found the receiver already fully stopped."""
        return (
            self.stopping_event is None
            and not self.should_transition_to_stopped
            and not self.should_emit_closed_event
            and self.task is None
            and self.sock is None
        )


@dataclass(frozen=True, slots=True)
class DatagramStopProvenance:
    """Where the current stop request originated relative to active handlers."""

    handler_originated: bool = False
    inherited_handler_origin: bool = False
    active_inline_handler: bool = False

    @property
    def has_handler_provenance(self) -> bool:
        """Whether the caller is the active handler or inherited handler-origin context."""
        return self.handler_originated or self.inherited_handler_origin

    @property
    def defers_terminal_events(self) -> bool:
        """Whether terminal publication must wait for active handler work to unwind."""
        return self.has_handler_provenance or self.active_inline_handler


@dataclass(slots=True)
class DatagramStopExecutionState:
    """Mutable cross-step state for a single stop execution."""

    deferred_close_waiter: asyncio.Future[None] | None = None
    stop_waiter_completion_deferred: bool = False
