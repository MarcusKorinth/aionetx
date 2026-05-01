"""
Shared asyncio datagram receiver lifecycle and teardown helpers.

This module centralizes the startup, receive-loop, and ordered stop behavior
used by UDP and multicast receivers so both transports publish the same
lifecycle and connection events.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Callable
from dataclasses import dataclass
from logging import Logger, LoggerAdapter
from typing import cast

from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.connection_events import ConnectionClosedEvent, ConnectionOpenedEvent
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.implementations.asyncio_impl.event_dispatcher import (
    AsyncioEventDispatcher,
    DispatcherRuntimeStats,
)
from aionetx.implementations.asyncio_impl.lifecycle_internal import LifecycleRole
from aionetx.implementations.asyncio_impl.lifecycle_internal import (
    LifecycleTransitionPublisher,
    emit_lifecycle_event,
)
from aionetx.implementations.asyncio_impl.runtime_utils import (
    WarningRateLimiter,
    assert_running_on_owner_loop,
    await_task_completion_preserving_cancellation,
)

SocketCleanup = Callable[[socket.socket], None]
_recv_fallback_warning_limiter = WarningRateLimiter(interval_seconds=300.0)


@dataclass(slots=True)
class _DatagramRuntimeState:
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
class _DatagramStopSnapshot:
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
class _DatagramStopProvenance:
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
class _DatagramStopExecutionState:
    """Mutable cross-step state for a single stop execution."""

    deferred_close_waiter: asyncio.Future[None] | None = None
    stop_waiter_completion_deferred: bool = False


class _AsyncioDatagramReceiverBase:
    """
    Shared internals for asyncio datagram receiver implementations.

    The base class keeps startup and shutdown sequencing identical across
    datagram transports so users observe the same lifecycle transitions,
    connection events, and error reporting regardless of socket type.
    """

    _WOULD_BLOCK_INITIAL_DELAY_SECONDS = 0.0005
    _WOULD_BLOCK_MAX_DELAY_SECONDS = 0.02

    def __init__(
        self,
        *,
        receiver_name: str,
        connection_id: str,
        logger: Logger | LoggerAdapter[Logger],
        event_dispatcher: AsyncioEventDispatcher,
        lifecycle_role: LifecycleRole,
    ) -> None:
        self._receiver_name = receiver_name
        self._connection_id = connection_id
        self._logger = logger
        self._event_dispatcher = event_dispatcher

        self._runtime = _DatagramRuntimeState()
        self._lifecycle_publisher = LifecycleTransitionPublisher(
            component_name=connection_id,
            resource_id=connection_id,
            role=lifecycle_role,
            get_state=lambda: self._runtime.lifecycle_state,
            set_state=lambda state: setattr(self._runtime, "lifecycle_state", state),
        )
        self._state_lock = asyncio.Lock()
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    def _assert_owner_loop(self) -> None:
        """
        Raise RuntimeError if called outside the owner event loop.

        Also pins ``_owner_loop`` on first call so all subsequent guard checks
        use the same loop identity drawn from ``runtime_utils.asyncio`` - not
        from a potentially test-patched module-level asyncio import.
        """
        self._owner_loop = assert_running_on_owner_loop(
            class_name=type(self).__name__, owner_loop=self._owner_loop
        )

    async def stop(self) -> None:
        """
        Stop the receiver, tear down detached resources, and publish final events.

        The public stop path delegates to the ordered internal shutdown helper
        so subclasses inherit the same teardown semantics.
        """
        await self._stop_datagram_receiver(socket_cleanup=self._cleanup_socket)

    def _cleanup_socket(self, _sock: socket.socket) -> None:
        """Subclass extension point for socket-specific cleanup before close."""

    @property
    def _running(self) -> bool:
        return self._runtime.running

    @_running.setter
    def _running(self, value: bool) -> None:
        self._runtime.running = value

    @property
    def _socket(self) -> socket.socket | None:
        return self._runtime.sock

    @_socket.setter
    def _socket(self, value: socket.socket | None) -> None:
        self._runtime.sock = value

    @property
    def _task(self) -> asyncio.Task[None] | None:
        return self._runtime.task

    @_task.setter
    def _task(self, value: asyncio.Task[None] | None) -> None:
        self._runtime.task = value

    @property
    def _lifecycle_state(self) -> ComponentLifecycleState:
        return self._runtime.lifecycle_state

    @property
    def _connection_state(self) -> ConnectionState:
        return self._runtime.connection_state

    @_connection_state.setter
    def _connection_state(self, value: ConnectionState) -> None:
        self._runtime.connection_state = value

    @property
    def _connection_metadata(self) -> ConnectionMetadata | None:
        return self._runtime.metadata

    @_connection_metadata.setter
    def _connection_metadata(self, value: ConnectionMetadata | None) -> None:
        self._runtime.metadata = value

    def _apply_lifecycle_state(
        self, next_state: ComponentLifecycleState
    ) -> ComponentLifecycleChangedEvent | None:
        return self._lifecycle_publisher.apply(next_state)

    @property
    def dispatcher_runtime_stats(self) -> DispatcherRuntimeStats:
        """Dispatcher operational snapshot for runtime diagnostics."""
        return self._event_dispatcher.runtime_stats

    async def _emit_lifecycle_event(self, event: ComponentLifecycleChangedEvent | None) -> None:
        await emit_lifecycle_event(dispatcher=self._event_dispatcher, event=event)

    async def _set_lifecycle_state(self, next_state: ComponentLifecycleState) -> None:
        event = self._apply_lifecycle_state(next_state)
        await self._emit_lifecycle_event(event)

    async def _receive_datagrams(self, receive_buffer_size: int) -> None:
        """Read datagrams until stop, emitting payload and error events as needed."""
        if self._socket is None:
            return
        loop = asyncio.get_running_loop()
        try:
            while self._running:
                data, sender = await self._recv_nonblocking(loop, self._socket, receive_buffer_size)
                remote_host = (
                    str(sender[0]) if isinstance(sender, tuple) and len(sender) >= 2 else None
                )
                remote_port = (
                    int(sender[1]) if isinstance(sender, tuple) and len(sender) >= 2 else None
                )
                await self._event_dispatcher.emit(
                    BytesReceivedEvent(
                        resource_id=self._connection_id,
                        data=data,
                        remote_host=remote_host,
                        remote_port=remote_port,
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._logger.warning("%s receive loop error: %s", self._receiver_name, error)
            await self._event_dispatcher.emit(
                NetworkErrorEvent(resource_id=self._connection_id, error=error)
            )
        finally:
            if self._running:
                # The task is already on the owner loop, so it shuts down
                # internally instead of re-entering the public guard that exists
                # to catch cross-loop user calls.
                await self._stop_datagram_receiver(socket_cleanup=self._cleanup_socket)

    async def _stop_datagram_receiver(self, socket_cleanup: SocketCleanup | None = None) -> None:
        """
        Stop the receiver in ordered phases.

        The method first snapshots state under lock, then performs slow teardown
        and event publication outside the lock so concurrent callers see a
        stable stop plan without blocking on socket cleanup.

        Flow overview:
            lock      : plan snapshot, detach task/socket, compute STOPPING
            ordinary  : emit STOPPING, tear down resources, then publish
                        ConnectionClosedEvent/STOPPED and stop dispatcher
            deferred  : tear down resources first, stop handler-origin dispatch,
                        then publish terminal events after active handlers unwind
        """
        snapshot = await self._plan_stop_snapshot()
        provenance = self._capture_stop_provenance()
        if snapshot.waits_for_owner:
            if provenance.has_handler_provenance:
                return
            # Non-owner stop callers observe the active owner stop path instead
            # of planning another teardown. shield() prevents caller
            # cancellation from cancelling the shared owner waiter.
            await asyncio.shield(cast("asyncio.Future[None]", snapshot.stop_waiter))
            return

        stop_waiter = snapshot.stop_waiter if snapshot.owns_stop else None
        stop_state = _DatagramStopExecutionState()
        try:
            if provenance.defers_terminal_events:
                stop_state.stop_waiter_completion_deferred = (
                    await self._prepare_deferred_stop_events(
                        snapshot=snapshot,
                        provenance=provenance,
                        stop_waiter=stop_waiter,
                        socket_cleanup=socket_cleanup,
                    )
                )
                if not provenance.has_handler_provenance and stop_waiter is not None:
                    await asyncio.shield(stop_waiter)
            else:
                await self._run_ordinary_stop_path(
                    snapshot=snapshot,
                    stop_state=stop_state,
                    stop_waiter=stop_waiter,
                    socket_cleanup=socket_cleanup,
                )
        except (Exception, asyncio.CancelledError) as error:
            if (
                stop_waiter is not None
                and not stop_waiter.done()
                and not stop_state.stop_waiter_completion_deferred
            ):
                stop_waiter.set_exception(error)
                # Mark the exception as retrieved so failed owner stops do not
                # leave an unhandled-Future warning behind.
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    stop_waiter.exception()
            raise
        else:
            if stop_waiter is not None and not stop_waiter.done():
                if stop_state.stop_waiter_completion_deferred:
                    pass
                elif provenance.handler_originated and stop_state.deferred_close_waiter is not None:
                    self._complete_stop_waiter_after_deferred_close(
                        stop_waiter, stop_state.deferred_close_waiter
                    )
                    stop_state.stop_waiter_completion_deferred = True
                else:
                    stop_waiter.set_result(None)
        finally:
            if stop_waiter is not None and not stop_state.stop_waiter_completion_deferred:
                await self._clear_stop_waiter_if_current(stop_waiter)

    def _capture_stop_provenance(self) -> _DatagramStopProvenance:
        """Capture handler-origin facts for the current stop caller."""
        handler_originated = (
            self._event_dispatcher.current_task_is_dispatching_handler()
            or self._event_dispatcher.current_task_has_handler_origin_context()
        )
        inherited_handler_origin = (
            self._event_dispatcher.current_task_inherits_handler_origin_context()
        )
        active_inline_handler = (
            not handler_originated
            and not inherited_handler_origin
            and self._event_dispatcher.has_active_handler_context()
            and self._event_dispatcher.current_task_would_deliver_inline()
        )
        return _DatagramStopProvenance(
            handler_originated=handler_originated,
            inherited_handler_origin=inherited_handler_origin,
            active_inline_handler=active_inline_handler,
        )

    async def _prepare_deferred_stop_events(
        self,
        *,
        snapshot: _DatagramStopSnapshot,
        provenance: _DatagramStopProvenance,
        stop_waiter: asyncio.Future[None] | None,
        socket_cleanup: SocketCleanup | None,
    ) -> bool:
        """Tear down resources and schedule terminal publication after handlers unwind."""
        first_error: BaseException | None = None
        try:
            if provenance.active_inline_handler:
                snapshot.cancel_task = False
            await self._teardown_stop_resources(snapshot=snapshot, socket_cleanup=socket_cleanup)
        except (Exception, asyncio.CancelledError) as error:
            first_error = error
        if first_error is None:
            try:
                await self._event_dispatcher.stop_from_handler_origin()
            except (Exception, asyncio.CancelledError) as error:
                first_error = error
        if first_error is None:
            self._complete_stop_waiter_after_deferred_stop_events(stop_waiter, snapshot)
            return True
        raise first_error

    async def _run_ordinary_stop_path(
        self,
        *,
        snapshot: _DatagramStopSnapshot,
        stop_state: _DatagramStopExecutionState,
        stop_waiter: asyncio.Future[None] | None,
        socket_cleanup: SocketCleanup | None,
    ) -> None:
        """Run the ordinary stop path that publishes terminal events inline."""
        first_error: BaseException | None = None
        try:
            await self._publish_stopping_transition(snapshot)
        except (Exception, asyncio.CancelledError) as error:
            first_error = error
        try:
            await self._teardown_stop_resources(snapshot=snapshot, socket_cleanup=socket_cleanup)
        except (Exception, asyncio.CancelledError) as error:
            if first_error is None:
                first_error = error
        if first_error is None:
            try:
                _, stop_state.deferred_close_waiter = await self._publish_closed_event_if_needed(
                    snapshot
                )
            except (Exception, asyncio.CancelledError) as error:
                first_error = error
            else:
                if stop_state.deferred_close_waiter is not None:
                    try:
                        await asyncio.shield(stop_state.deferred_close_waiter)
                    except asyncio.CancelledError:
                        self._complete_stop_waiter_after_deferred_close_and_stop(
                            stop_waiter=stop_waiter,
                            snapshot=snapshot,
                            deferred_close_waiter=stop_state.deferred_close_waiter,
                        )
                        stop_state.stop_waiter_completion_deferred = True
                        raise
                    except Exception as error:
                        first_error = error
        await self._publish_stopped_transition_if_needed(snapshot, emit_event=first_error is None)
        if snapshot.stop_dispatcher:
            try:
                await self._event_dispatcher.stop()
            except (Exception, asyncio.CancelledError) as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    async def _clear_stop_waiter_if_current(self, stop_waiter: asyncio.Future[None] | None) -> None:
        """Clear the shared stop waiter when the caller still owns it."""
        async with self._state_lock:
            if self._runtime.stop_waiter is stop_waiter:
                self._runtime.stop_waiter = None
                self._runtime.stop_owner_task = None

    def _complete_stop_waiter_after_deferred_close(
        self,
        stop_waiter: asyncio.Future[None],
        deferred_close_waiter: asyncio.Future[None],
    ) -> None:
        """Release external stop waiters after handler-originated deferred close publication."""

        async def _complete() -> None:
            try:
                await asyncio.shield(deferred_close_waiter)
            except (Exception, asyncio.CancelledError) as error:
                if not stop_waiter.done():
                    stop_waiter.set_exception(error)
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        stop_waiter.exception()
            else:
                if not stop_waiter.done():
                    stop_waiter.set_result(None)
            finally:
                await self._clear_stop_waiter_if_current(stop_waiter)

        _ = asyncio.create_task(_complete())

    def _complete_stop_waiter_after_deferred_close_and_stop(
        self,
        *,
        stop_waiter: asyncio.Future[None] | None,
        snapshot: _DatagramStopSnapshot,
        deferred_close_waiter: asyncio.Future[None],
    ) -> None:
        """Complete terminal stop publication after close was deferred elsewhere."""

        async def _complete() -> None:
            first_error: BaseException | None = None
            try:
                try:
                    await asyncio.shield(deferred_close_waiter)
                except (Exception, asyncio.CancelledError) as error:
                    first_error = error
                try:
                    await self._publish_stopped_transition_if_needed(
                        snapshot, emit_event=first_error is None
                    )
                except (Exception, asyncio.CancelledError) as error:
                    if first_error is None:
                        first_error = error
                if snapshot.stop_dispatcher:
                    try:
                        await self._event_dispatcher.stop()
                    except (Exception, asyncio.CancelledError) as error:
                        if first_error is None:
                            first_error = error
                if first_error is not None:
                    raise first_error
            except (Exception, asyncio.CancelledError) as error:
                if stop_waiter is not None and not stop_waiter.done():
                    stop_waiter.set_exception(error)
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        stop_waiter.exception()
            else:
                if stop_waiter is not None and not stop_waiter.done():
                    stop_waiter.set_result(None)
            finally:
                await self._clear_stop_waiter_if_current(stop_waiter)

        _ = asyncio.create_task(_complete())

    def _complete_stop_waiter_after_deferred_stop_events(
        self,
        stop_waiter: asyncio.Future[None] | None,
        snapshot: _DatagramStopSnapshot,
    ) -> None:
        """Publish terminal stop events after a handler-originated stop unwinds."""

        async def _complete() -> None:
            first_error: BaseException | None = None
            try:
                while self._event_dispatcher.has_active_handler_context():
                    await asyncio.sleep(0)
                with self._event_dispatcher.inline_delivery_context():
                    try:
                        await self._publish_stopping_transition(snapshot)
                    except (Exception, asyncio.CancelledError) as error:
                        first_error = error
                    if first_error is None:
                        try:
                            _, deferred_close_waiter = await self._publish_closed_event_if_needed(
                                snapshot
                            )
                            if deferred_close_waiter is not None:
                                await asyncio.shield(deferred_close_waiter)
                        except (Exception, asyncio.CancelledError) as error:
                            first_error = error
                    try:
                        await self._publish_stopped_transition_if_needed(
                            snapshot, emit_event=first_error is None
                        )
                    except (Exception, asyncio.CancelledError) as error:
                        if first_error is None:
                            first_error = error
                if snapshot.stop_dispatcher:
                    try:
                        await self._event_dispatcher.stop()
                    except (Exception, asyncio.CancelledError) as error:
                        if first_error is None:
                            first_error = error
                if first_error is not None:
                    raise first_error
            except (Exception, asyncio.CancelledError) as error:
                if stop_waiter is not None and not stop_waiter.done():
                    stop_waiter.set_exception(error)
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        stop_waiter.exception()
            else:
                if stop_waiter is not None and not stop_waiter.done():
                    stop_waiter.set_result(None)
            finally:
                await self._clear_stop_waiter_if_current(stop_waiter)

        _ = asyncio.create_task(_complete())

    async def _plan_stop_snapshot(self) -> _DatagramStopSnapshot:
        """Detach stop-time resources under lock and return the resulting stop snapshot."""
        snapshot = _DatagramStopSnapshot()
        async with self._state_lock:
            snapshot.previous_connection_state = self._connection_state
            pre_transition_lifecycle_state = self._lifecycle_state
            current_task = asyncio.current_task()
            if self._runtime.stop_waiter is not None:
                if current_task is self._runtime.stop_owner_task:
                    # Inline handlers may re-enter stop() from the owner task
                    # while STOPPING is being published. Waiting on the owner
                    # waiter here would self-deadlock.
                    return snapshot
                snapshot.stop_waiter = self._runtime.stop_waiter
                return snapshot
            if self._is_fully_stopped_locked():
                snapshot.stop_dispatcher = True
                return snapshot

            loop = current_task.get_loop() if current_task is not None else asyncio.get_event_loop()
            snapshot.stop_waiter = loop.create_future()
            snapshot.owns_stop = True
            self._runtime.stop_waiter = snapshot.stop_waiter
            self._runtime.stop_owner_task = current_task

            if self._lifecycle_state != ComponentLifecycleState.STOPPED:
                snapshot.stopping_event = self._apply_lifecycle_state(
                    ComponentLifecycleState.STOPPING
                )
                snapshot.should_transition_to_stopped = True

            self._running = False
            snapshot.task = self._task
            self._task = None
            snapshot.sock = self._socket
            self._socket = None
            self._connection_state = ConnectionState.CLOSED
            if pre_transition_lifecycle_state != ComponentLifecycleState.STARTING:
                snapshot.should_emit_closed_event = True
            snapshot.stop_dispatcher = True
        return snapshot

    async def _publish_stopping_transition(self, snapshot: _DatagramStopSnapshot) -> None:
        """Emit the precomputed STOPPING event, if this stop path created one."""
        if snapshot.is_noop:
            return
        await self._emit_lifecycle_event(snapshot.stopping_event)

    async def _publish_closed_event_if_needed(
        self, snapshot: _DatagramStopSnapshot
    ) -> tuple[bool, asyncio.Future[None] | None]:
        """Emit the connection-closed event unless stop is rolling back startup."""
        if not snapshot.should_emit_closed_event:
            return False, None
        closed_event = ConnectionClosedEvent(
            resource_id=self._connection_id,
            previous_state=snapshot.previous_connection_state,
            metadata=self._connection_metadata,
        )
        deferred, deferred_waiter = await self._defer_close_event_until_current_handler_unwinds(
            closed_event
        )
        if deferred:
            return True, deferred_waiter
        await self._event_dispatcher.emit(closed_event)
        return False, None

    async def _defer_close_event_until_current_handler_unwinds(
        self, closed_event: ConnectionClosedEvent
    ) -> tuple[bool, asyncio.Future[None] | None]:
        """Defer close publication while the current connection handler is in flight."""
        async with self._state_lock:
            opening_event_in_flight = self._runtime.opening_event_task is not None
            inherited_handler_origin = (
                self._event_dispatcher.current_task_inherits_handler_origin_context()
            )
            handler_origin_in_flight = (
                self._event_dispatcher.current_task_is_dispatching_handler()
                or self._event_dispatcher.current_task_has_handler_origin_context()
            )
            active_inline_handler_in_flight = (
                self._event_dispatcher.has_active_handler_context()
                and self._event_dispatcher.current_task_would_deliver_inline()
            )
            if (
                not opening_event_in_flight
                and not handler_origin_in_flight
                and not inherited_handler_origin
                and not active_inline_handler_in_flight
            ):
                return False, None
            self._runtime.deferred_close_event = closed_event
            if (
                self._runtime.deferred_close_event_waiter is None
                or self._runtime.deferred_close_event_waiter.done()
            ):
                current_task = asyncio.current_task()
                loop = (
                    current_task.get_loop()
                    if current_task is not None
                    else asyncio.get_running_loop()
                )
                self._runtime.deferred_close_event_waiter = loop.create_future()
            if not opening_event_in_flight and (
                handler_origin_in_flight
                or inherited_handler_origin
                or active_inline_handler_in_flight
            ):
                publish_task = self._runtime.deferred_close_publish_task
                if publish_task is None or publish_task.done():
                    self._runtime.deferred_close_publish_task = asyncio.create_task(
                        self._publish_deferred_close_after_handler_origin_expires(),
                        name=f"{self._connection_id}-deferred-close-publisher",
                    )
            if handler_origin_in_flight or inherited_handler_origin:
                return True, None
            return True, self._runtime.deferred_close_event_waiter

    async def _publish_deferred_close_after_handler_origin_expires(self) -> None:
        """Publish a deferred close once the handler-origin context has unwound."""
        current_task = asyncio.current_task()
        try:
            while self._event_dispatcher.has_active_handler_context():
                await asyncio.sleep(0)
            await self._publish_deferred_close_after_opened_event()
        except (Exception, asyncio.CancelledError) as error:
            async with self._state_lock:
                deferred_waiter = self._runtime.deferred_close_event_waiter
            if deferred_waiter is not None and not deferred_waiter.done():
                deferred_waiter.set_exception(error)
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    deferred_waiter.exception()
            if not isinstance(error, asyncio.CancelledError):
                self._logger.warning(
                    "%s deferred close publication failed: %s",
                    self._receiver_name,
                    error,
                )
        finally:
            async with self._state_lock:
                if self._runtime.deferred_close_publish_task is current_task:
                    self._runtime.deferred_close_publish_task = None

    async def _publish_deferred_close_after_opened_event_preserving_cancellation(self) -> bool:
        """Publish deferred close even if receiver startup is cancelled at the barrier."""
        publish_task = asyncio.create_task(self._publish_deferred_close_after_opened_event())
        caller_cancelled = False
        try:
            while True:
                try:
                    result = await asyncio.shield(publish_task)
                    break
                except asyncio.CancelledError:
                    caller_cancelled = True
                    if publish_task.done():
                        publish_task.result()
                        break
                    continue
        finally:
            if caller_cancelled and not publish_task.done():
                _ = await asyncio.shield(publish_task)
        if caller_cancelled:
            raise asyncio.CancelledError
        return result

    async def _publish_deferred_close_after_opened_event(self) -> bool:
        """Publish a close event deferred until ConnectionOpenedEvent handling completed."""
        async with self._state_lock:
            closed_event = self._runtime.deferred_close_event
            if closed_event is None:
                return False
            self._runtime.deferred_close_event = None
            deferred_waiter = self._runtime.deferred_close_event_waiter
        try:
            with self._event_dispatcher.inline_delivery_context():
                await self._event_dispatcher.emit(closed_event)
        except (Exception, asyncio.CancelledError) as error:
            if deferred_waiter is not None and not deferred_waiter.done():
                deferred_waiter.set_exception(error)
            raise
        else:
            if deferred_waiter is not None and not deferred_waiter.done():
                deferred_waiter.set_result(None)
        finally:
            async with self._state_lock:
                if self._runtime.deferred_close_event_waiter is deferred_waiter:
                    self._runtime.deferred_close_event_waiter = None
        return True

    def _is_fully_stopped_locked(self) -> bool:
        """Return whether runtime state already represents a fully stopped receiver."""
        return (
            not self._running
            and self._socket is None
            and self._lifecycle_state == ComponentLifecycleState.STOPPED
        )

    async def _teardown_stop_resources(
        self,
        *,
        snapshot: _DatagramStopSnapshot,
        socket_cleanup: SocketCleanup | None,
    ) -> None:
        """Cancel the detached receive task and close the detached socket, if present."""
        task_error: BaseException | None = None
        if snapshot.task is not None:
            if snapshot.cancel_task and snapshot.task is not asyncio.current_task():
                snapshot.task.cancel()
                try:
                    await await_task_completion_preserving_cancellation(
                        cast("asyncio.Task[object]", snapshot.task)
                    )
                except asyncio.CancelledError as error:
                    task_error = error

        if snapshot.sock is not None:
            if socket_cleanup is not None:
                socket_cleanup(snapshot.sock)
            snapshot.sock.close()
        if (
            snapshot.cancel_task
            and snapshot.task is not None
            and snapshot.task is not asyncio.current_task()
        ):
            if task_error is not None:
                raise task_error

    async def _publish_stopped_transition_if_needed(
        self, snapshot: _DatagramStopSnapshot, *, emit_event: bool = True
    ) -> None:
        """Compute and emit the final STOPPED lifecycle event when required."""
        if not snapshot.should_transition_to_stopped:
            return
        async with self._state_lock:
            stopped_event = self._apply_lifecycle_state(ComponentLifecycleState.STOPPED)
        if emit_event:
            await self._emit_lifecycle_event(stopped_event)

    async def _recv_nonblocking(
        self,
        loop: asyncio.AbstractEventLoop,
        sock: socket.socket,
        receive_buffer_size: int,
    ) -> tuple[bytes, object]:
        """
        Receive one datagram using the event loop helper or a polling fallback.

        Returns:
            tuple[bytes, object]: Payload bytes and the raw sender object returned by the socket.
        """
        runtime_sock_recvfrom = getattr(loop, "sock_recvfrom", None)
        if callable(runtime_sock_recvfrom):
            return cast(
                tuple[bytes, object], await runtime_sock_recvfrom(sock, receive_buffer_size)
            )

        if not self._runtime.recv_fallback_warning_emitted:
            limiter_key = f"{self._connection_id}:recvfrom-fallback"
            if _recv_fallback_warning_limiter.should_log(limiter_key):
                self._logger.warning(
                    "%s is using recvfrom() fallback polling because the event loop "
                    "does not expose sock_recvfrom().",
                    self._receiver_name,
                )
            self._runtime.recv_fallback_warning_emitted = True

        backoff_delay_seconds = self._WOULD_BLOCK_INITIAL_DELAY_SECONDS
        while True:
            try:
                return sock.recvfrom(receive_buffer_size)
            except (BlockingIOError, InterruptedError):
                await asyncio.sleep(backoff_delay_seconds)
                backoff_delay_seconds = min(
                    self._WOULD_BLOCK_MAX_DELAY_SECONDS, backoff_delay_seconds * 2
                )

    async def _begin_startup(self) -> bool:
        """
        Begin receiver startup and publish STARTING when startup should proceed.

        Returns:
            bool: ``True`` when the caller should continue with socket setup.
        """
        starting_event: ComponentLifecycleChangedEvent | None = None
        async with self._state_lock:
            if self._lifecycle_state in (
                ComponentLifecycleState.STARTING,
                ComponentLifecycleState.RUNNING,
            ):
                self._logger.debug(
                    "%s receiver start called while already running.", self._receiver_name
                )
                return False
            # Keep dispatcher startup and the STARTING transition in one locked
            # section so stop() cannot observe a half-started receiver.
            await self._event_dispatcher.start()
            starting_event = self._apply_lifecycle_state(ComponentLifecycleState.STARTING)
        try:
            await self._emit_lifecycle_event(starting_event)
        except (Exception, asyncio.CancelledError):
            await self._rollback_failed_starting_publication()
            raise
        return True

    async def _complete_startup(
        self,
        *,
        sock: socket.socket,
        metadata: ConnectionMetadata,
        receive_buffer_size: int,
    ) -> None:
        """Attach socket state, publish open events, then start the receive task."""
        running_event: ComponentLifecycleChangedEvent | None = None
        async with self._state_lock:
            self._socket = sock
            self._running = True
            self._connection_metadata = metadata
            self._connection_state = ConnectionState.CONNECTED
            running_event = self._apply_lifecycle_state(ComponentLifecycleState.RUNNING)
        try:
            await self._emit_lifecycle_event(running_event)
            async with self._state_lock:
                if (
                    self._socket is not sock
                    or not self._running
                    or self._lifecycle_state != ComponentLifecycleState.RUNNING
                ):
                    return
            opening_task = asyncio.current_task()
            self._runtime.opening_event_task = cast(asyncio.Task[object] | None, opening_task)
            try:
                await self._event_dispatcher.emit_and_wait(
                    ConnectionOpenedEvent(resource_id=metadata.connection_id, metadata=metadata),
                    drop_on_backpressure=False,
                )
            except (Exception, asyncio.CancelledError):
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await self._publish_deferred_close_after_opened_event_preserving_cancellation()
                if self._runtime.opening_event_task is opening_task:
                    self._runtime.opening_event_task = None
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await self._stop_datagram_receiver(socket_cleanup=self._cleanup_socket)
                raise
            if self._runtime.opening_event_task is opening_task:
                self._runtime.opening_event_task = None
            if await self._publish_deferred_close_after_opened_event_preserving_cancellation():
                return
            async with self._state_lock:
                if (
                    self._socket is not sock
                    or not self._running
                    or self._lifecycle_state != ComponentLifecycleState.RUNNING
                ):
                    return
                self._task = asyncio.create_task(
                    self._receive_datagrams(receive_buffer_size),
                    name=f"{self._connection_id}-receiver",
                )
        except (Exception, asyncio.CancelledError):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._stop_datagram_receiver(socket_cleanup=self._cleanup_socket)
            raise

    async def _fail_startup(self) -> None:
        """Roll startup back to a clean stopped state and quiesce the dispatcher."""
        stopped_event: ComponentLifecycleChangedEvent | None = None
        async with self._state_lock:
            self._socket = None
            self._running = False
            self._task = None
            self._connection_metadata = None
            self._connection_state = ConnectionState.CREATED
            stopped_event = self._apply_lifecycle_state(ComponentLifecycleState.STOPPED)
        try:
            await self._emit_lifecycle_event(stopped_event)
        finally:
            await self._event_dispatcher.stop()

    async def _rollback_failed_starting_publication(self) -> None:
        """Return STARTING publication failure to STOPPED without relying on handlers."""
        async with self._state_lock:
            if self._lifecycle_state == ComponentLifecycleState.STARTING:
                self._socket = None
                self._running = False
                self._task = None
                self._connection_metadata = None
                self._connection_state = ConnectionState.CREATED
                self._apply_lifecycle_state(ComponentLifecycleState.STOPPED)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await self._event_dispatcher.stop()
