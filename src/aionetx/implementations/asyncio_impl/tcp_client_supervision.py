"""
Connect/reconnect supervision for the managed asyncio TCP client.

This module keeps retry policy, failure publication, and terminal lifecycle
cleanup in one place so the transport-facing client object can stay focused on
public API state and resource ownership.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.error_policy import ErrorPolicy
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api.reconnect_events import (
    ReconnectAttemptFailedEvent,
    ReconnectAttemptStartedEvent,
    ReconnectScheduledEvent,
)
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_connection import AsyncioTcpConnection
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher
from aionetx.implementations.asyncio_impl.lifecycle_internal import (
    apply_stopped_transition_if_stopping,
    apply_stopping_transition_if_active,
)
from aionetx.implementations.asyncio_impl.runtime_utils import ReconnectBackoff


class _TcpClientForSupervision(Protocol):
    """Structural view of the private client hooks used by supervision."""

    _attempt_counter: int
    _backoff: ReconnectBackoff
    _component_id: str
    _connection: AsyncioTcpConnection | None
    _event_dispatcher: AsyncioEventDispatcher
    _last_connect_error: Exception | None
    _lifecycle_state: ComponentLifecycleState
    _logger: logging.LoggerAdapter[logging.Logger]
    _running: bool
    _settings: TcpClientSettings
    _state_lock: asyncio.Lock
    _stop_waiter: asyncio.Future[None] | None
    _status_changed: asyncio.Event
    _status_version: int

    @property
    def _connection_closed_event(self) -> asyncio.Event:
        raise NotImplementedError

    def _apply_lifecycle_state(
        self, target: ComponentLifecycleState
    ) -> ComponentLifecycleChangedEvent | None:
        raise NotImplementedError

    async def _close_current_connection(self) -> tuple[asyncio.Future[None], ...]:
        raise NotImplementedError

    async def _connect_once(self) -> None:
        raise NotImplementedError

    async def _emit_lifecycle_event(self, event: ComponentLifecycleChangedEvent | None) -> None:
        raise NotImplementedError

    def _notify_status_changed(self) -> None:
        raise NotImplementedError

    def _resolve_error_policy(self) -> ErrorPolicy:
        raise NotImplementedError

    async def _stop_heartbeat_sender(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class _ReconnectPlan:
    """
    Internal reconnect decision produced after a disconnect or failure.

    Attributes:
        should_reconnect: Whether supervision should attempt another connect cycle.
        delay_seconds: Delay before the next attempt when reconnect is enabled.
    """

    should_reconnect: bool
    delay_seconds: float | None

    def __post_init__(self) -> None:
        if self.should_reconnect and self.delay_seconds is None:
            raise ValueError("Reconnect plan delay must be set when reconnect is scheduled.")

    @classmethod
    def disabled(cls) -> "_ReconnectPlan":
        """Return a plan that terminates supervision without another retry."""
        return cls(should_reconnect=False, delay_seconds=None)

    @classmethod
    def scheduled(cls, delay_seconds: float) -> "_ReconnectPlan":
        """
        Return a plan that schedules another connect attempt after ``delay_seconds``.

        Args:
            delay_seconds: Backoff delay before the next attempt.

        Returns:
            _ReconnectPlan: Retry plan carrying the requested delay.
        """
        return cls(should_reconnect=True, delay_seconds=delay_seconds)


@dataclass(frozen=True)
class _FailureOutcome:
    """Failure-handling decision pairing error policy with reconnect behavior."""

    policy: ErrorPolicy
    reconnect: _ReconnectPlan


class TcpClientConnectionSupervisor:
    """
    Coordinates connect/reconnect supervision in explicit, ordered phases.

    Flow overview:
        connect attempt
            |
            +-- success ------------------> wait for connection termination
            |                                  |
            |                                  +-- running + reconnect --> sleep -> next attempt
            |                                  |
            |                                  +-- stop / reconnect off --> finalize
            |
            +-- failure ------------------> publish failure events
                                               |
                                               +-- fail-fast / stopped ----> finalize
                                               |
                                               +-- retry enabled -----------> sleep -> next attempt
    """

    def __init__(self, client: _TcpClientForSupervision) -> None:
        self._client = client

    async def run(self) -> None:
        """Run connect/reconnect supervision until stop or terminal failure."""
        client = self._client
        try:
            while client._running:
                try:
                    should_continue = await self._run_connect_cycle()
                    if not should_continue:
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    should_continue = await self._handle_connect_cycle_failure(error)
                    if not should_continue:
                        break
        finally:
            await self._finalize_supervision()

    async def _run_connect_cycle(self) -> bool:
        """Run one connect-to-disconnect cycle and decide whether supervision continues."""
        if not await self._start_connect_attempt():
            return False
        await self._await_connection_termination()
        return await self._schedule_reconnect_after_disconnect()

    async def _handle_connect_cycle_failure(self, error: Exception) -> bool:
        """
        Publish failure state, tear down resources, and decide whether to retry.

        Args:
            error: Exception raised while creating or supervising a connection.

        Returns:
            bool: ``True`` when supervision should keep retrying.
        """
        outcome = self._determine_failure_outcome(error)
        await self._publish_connect_failure(
            error=error,
            policy=outcome.policy,
            next_delay_seconds=outcome.reconnect.delay_seconds,
        )
        await self._teardown_after_failure()
        if self._should_stop_after_failure(policy=outcome.policy):
            return False
        return await self._sleep_until_next_reconnect(reconnect=outcome.reconnect)

    def _determine_failure_outcome(self, error: Exception) -> _FailureOutcome:
        """Resolve error policy and reconnect timing for one failed connect cycle."""
        policy = self._capture_failure_state(error)
        return _FailureOutcome(policy=policy, reconnect=self._plan_reconnect())

    async def _start_connect_attempt(self) -> bool:
        """Advance attempt counters, publish start notification, and open one connection."""
        client = self._client
        client._last_connect_error = None
        client._attempt_counter += 1
        client._notify_status_changed()
        await client._event_dispatcher.emit(
            ReconnectAttemptStartedEvent(
                resource_id=client._component_id,
                attempt=client._attempt_counter,
            )
        )
        if not client._running or client._lifecycle_state != ComponentLifecycleState.RUNNING:
            return False
        await client._connect_once()
        if not client._running:
            return False
        client._backoff.reset()
        client._logger.debug("TCP client connected.")
        return True

    async def _await_connection_termination(self) -> None:
        """Wait until the current connection disappears or reaches ``CLOSED``."""
        client = self._client
        while client._running:
            await client._connection_closed_event.wait()
            client._connection_closed_event.clear()
            if client._connection is None or client._connection.state == ConnectionState.CLOSED:
                return

    async def _schedule_reconnect_after_disconnect(self) -> bool:
        """Plan and wait for the next reconnect after a clean disconnect."""
        return await self._sleep_until_next_reconnect(reconnect=self._plan_reconnect())

    def _capture_failure_state(self, error: Exception) -> ErrorPolicy:
        """Persist failure state on the client and return the active error policy."""
        client = self._client
        client._logger.warning("TCP client supervision error: %s", error)
        client._last_connect_error = error
        client._notify_status_changed()
        return client._resolve_error_policy()

    def _plan_reconnect(self) -> _ReconnectPlan:
        """Return the reconnect decision implied by client state and settings."""
        client = self._client
        if not client._running or not client._settings.reconnect.enabled:
            return _ReconnectPlan.disabled()
        return _ReconnectPlan.scheduled(delay_seconds=client._backoff.next_delay())

    async def _emit_failure_events(
        self,
        error: Exception,
        policy: ErrorPolicy,
        next_delay_seconds: float | None,
    ) -> None:
        """Emit error and reconnect-failure events for one failed attempt."""
        client = self._client
        if policy != ErrorPolicy.IGNORE:
            await client._event_dispatcher.emit(
                NetworkErrorEvent(resource_id=client._component_id, error=error)
            )
        await client._event_dispatcher.emit(
            ReconnectAttemptFailedEvent(
                resource_id=client._component_id,
                attempt=client._attempt_counter,
                error=error,
                next_delay_seconds=next_delay_seconds,
            )
        )

    async def _publish_connect_failure(
        self,
        *,
        error: Exception,
        policy: ErrorPolicy,
        next_delay_seconds: float | None,
    ) -> None:
        """Publish the externally visible events for a failed connect cycle."""
        await self._emit_failure_events(
            error=error,
            policy=policy,
            next_delay_seconds=next_delay_seconds,
        )

    async def _teardown_after_failure(self) -> None:
        """Stop heartbeats and close any partially active connection after failure."""
        client = self._client
        await client._stop_heartbeat_sender()
        await client._close_current_connection()

    def _should_stop_after_failure(self, *, policy: ErrorPolicy) -> bool:
        """Return whether supervision should stop instead of sleeping for a retry."""
        client = self._client
        return (
            policy == ErrorPolicy.FAIL_FAST
            or not client._running
            or not client._settings.reconnect.enabled
        )

    async def _sleep_until_next_reconnect(self, *, reconnect: _ReconnectPlan) -> bool:
        """
        Wait for the next reconnect delay unless stop state changes first.

        Returns:
            bool: ``True`` when another connect cycle should start immediately after the wait.
        """
        if not reconnect.should_reconnect:
            return False
        client = self._client
        if not client._running or not client._settings.reconnect.enabled:
            return False
        delay_seconds = reconnect.delay_seconds
        if delay_seconds is None:
            raise RuntimeError(
                "Reconnect plan is marked as scheduled but does not provide delay_seconds."
            )
        await self._emit_reconnect_scheduled(next_delay_seconds=delay_seconds)
        if not client._running or not client._settings.reconnect.enabled:
            return False
        client._logger.debug("TCP client reconnect scheduled in %.3f seconds.", delay_seconds)
        return await self._wait_for_reconnect_delay_or_stop(delay_seconds=delay_seconds)

    async def _wait_for_reconnect_delay_or_stop(self, *, delay_seconds: float) -> bool:
        """Sleep for the reconnect delay, but wake early if status changes or stop begins."""
        client = self._client
        observed_version = client._status_version
        client._status_changed.clear()
        if client._status_version != observed_version:
            return client._running and client._settings.reconnect.enabled
        try:
            await asyncio.wait_for(client._status_changed.wait(), timeout=delay_seconds)
        except asyncio.TimeoutError:
            return client._running and client._settings.reconnect.enabled
        return client._running and client._settings.reconnect.enabled

    async def _emit_reconnect_scheduled(self, next_delay_seconds: float) -> None:
        """Emit the next scheduled reconnect delay to observers."""
        client = self._client
        await client._event_dispatcher.emit(
            ReconnectScheduledEvent(
                resource_id=client._component_id,
                attempt=client._attempt_counter + 1,
                delay_seconds=next_delay_seconds,
            )
        )

    async def _finalize_supervision(self) -> None:
        """Leave the client in a fully stopped state after supervision exits."""
        try:
            await self._shutdown_active_resources()
            async with self._client._state_lock:
                explicit_stop_in_progress = self._client._stop_waiter is not None
            if not explicit_stop_in_progress:
                await self._publish_terminal_lifecycle_transitions()
        finally:
            await self._stop_dispatcher()

    async def _shutdown_active_resources(self) -> None:
        """Stop heartbeat before connection close to preserve teardown order."""
        client = self._client
        await client._stop_heartbeat_sender()
        await client._close_current_connection()

    async def _publish_terminal_lifecycle_transitions(self) -> None:
        """Emit any remaining STOPPING/STOPPED lifecycle events in order."""
        transition_events = await self._collect_terminal_lifecycle_events()
        for event in transition_events:
            await self._client._emit_lifecycle_event(event)

    async def _stop_dispatcher(self) -> None:
        # Supervision can reach a terminal state on its own, for example after a
        # non-retrying connect failure. The dispatcher is still stopped here so
        # background delivery never outlives the final STOPPED transition.
        async with self._client._state_lock:
            if self._client._stop_waiter is not None:
                return
        await self._client._event_dispatcher.stop()

    async def _collect_terminal_lifecycle_events(self) -> list[ComponentLifecycleChangedEvent]:
        """Apply final lifecycle transitions under lock and return them for emission."""
        client = self._client
        transition_events: list[ComponentLifecycleChangedEvent] = []
        async with client._state_lock:
            stopping_event = apply_stopping_transition_if_active(
                get_state=lambda: client._lifecycle_state,
                apply_transition=client._apply_lifecycle_state,
            )
            if stopping_event is not None:
                transition_events.append(stopping_event)
            stopped_event = apply_stopped_transition_if_stopping(
                get_state=lambda: client._lifecycle_state,
                apply_transition=client._apply_lifecycle_state,
            )
            if stopped_event is not None:
                transition_events.append(stopped_event)
        return transition_events
