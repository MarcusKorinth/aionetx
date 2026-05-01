"""
Asyncio TCP client with explicit supervision and lifecycle publication.

This module owns client-side connect/reconnect orchestration and keeps
connection-level behavior in :class:`AsyncioTcpConnection`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import cast

from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_protocol import ConnectionProtocol
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.error_policy import ErrorPolicy
from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.network_event_handler_protocol import NetworkEventHandlerProtocol
from aionetx.api.tcp_client import TcpClientProtocol, TcpClientSettings
from aionetx.implementations.asyncio_impl._tcp_client_connect import (
    connect_once,
    start_heartbeat_sender,
    stop_heartbeat_sender,
    wait_until_client_connected,
)
from aionetx.implementations.asyncio_impl.asyncio_tcp_connection import AsyncioTcpConnection
from aionetx.implementations.asyncio_impl._tcp_client_runtime import (
    _ClientRuntimeAccessors,
    _ClientRuntimeState,
)
from aionetx.implementations.asyncio_impl.identifier_utils import (
    tcp_client_component_id,
)
from aionetx.implementations.asyncio_impl.event_dispatcher import (
    AsyncioEventDispatcher,
    DispatcherRuntimeStats,
)
from aionetx.implementations.asyncio_impl.lifecycle_internal import (
    LifecycleRole,
    LifecycleTransitionPublisher,
    emit_lifecycle_event,
)
from aionetx.implementations.asyncio_impl.runtime_utils import (
    ReconnectBackoff,
    assert_running_on_owner_loop,
    await_task_completion_preserving_cancellation,
    validate_heartbeat_provider,
)
from aionetx.implementations.asyncio_impl.tcp_client_supervision import (
    TcpClientConnectionSupervisor,
)

logger = logging.getLogger(__name__)


class AsyncioTcpClient(_ClientRuntimeAccessors, TcpClientProtocol):
    """
    Managed asyncio TCP client with optional reconnect and heartbeats.

    The client owns component lifecycle state and supervision. Individual
    socket read/write behavior remains in :class:`AsyncioTcpConnection`.
    """

    def __init__(
        self,
        settings: TcpClientSettings,
        event_handler: NetworkEventHandlerProtocol,
        heartbeat_provider: HeartbeatProviderProtocol | None = None,
        connection_opener: Callable[
            ..., Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
        ]
        | None = None,
    ) -> None:
        settings.validate()
        validate_heartbeat_provider(
            heartbeat_settings=settings.heartbeat,
            heartbeat_provider=heartbeat_provider,
        )
        self._settings = settings
        self._heartbeat_provider = heartbeat_provider
        # Seam for deterministic tests and explicit connect-attempt control.
        self._connection_opener = connection_opener or asyncio.open_connection
        self._runtime = _ClientRuntimeState()
        self._supervisor_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._stop_waiter: asyncio.Future[None] | None = None
        self._stop_owner_task: asyncio.Task[object] | None = None
        self._lifecycle_state = ComponentLifecycleState.STOPPED
        self._backoff = ReconnectBackoff(settings.reconnect)
        self._status_changed = asyncio.Event()
        self._runtime.connection_closed_event.set()
        self._connection_supervisor = TcpClientConnectionSupervisor(self)
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._component_id = tcp_client_component_id(settings.host, settings.port)
        self._logger = logging.LoggerAdapter(
            logger, {"component": "tcp_client", "host": settings.host, "port": settings.port}
        )
        self._event_dispatcher = AsyncioEventDispatcher(
            event_handler=event_handler,
            delivery=settings.event_delivery,
            logger=self._logger,
            error_source=self._component_id,
            stop_component_callback=self.stop,
        )
        self._lifecycle_publisher = LifecycleTransitionPublisher(
            component_name=self._component_id,
            resource_id=self._component_id,
            role=LifecycleRole.TCP_CLIENT,
            get_state=lambda: self._lifecycle_state,
            set_state=lambda state: setattr(self, "_lifecycle_state", state),
            on_state_applied=self._on_lifecycle_state_applied,
        )

    @property
    def connection(self) -> ConnectionProtocol | None:
        """
        Return the current live connection when connected.

        Returns:
            ConnectionProtocol | None: Active connection object, or ``None``
            when no connected session is currently available.
        """
        if self._connection is None or self._connection.state == ConnectionState.CLOSED:
            return None
        return self._connection

    @property
    def lifecycle_state(self) -> ComponentLifecycleState:
        """Current component lifecycle state exposed by the managed client."""
        return self._lifecycle_state

    def __repr__(self) -> str:
        return (
            f"AsyncioTcpClient("
            f"host={self._settings.host!r}, "
            f"port={self._settings.port!r}, "
            f"state={self._lifecycle_state.value!r})"
        )

    @property
    def dispatcher_runtime_stats(self) -> DispatcherRuntimeStats:
        """Dispatcher operational snapshot for runtime diagnostics."""
        return self._event_dispatcher.runtime_stats

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

    async def start(self) -> None:
        """Start lifecycle supervision and begin connect attempts."""
        self._assert_owner_loop()
        self._logger.debug("Starting TCP client.")
        starting_event: ComponentLifecycleChangedEvent | None = None
        running_event: ComponentLifecycleChangedEvent | None = None
        async with self._state_lock:
            if self._lifecycle_state in (
                ComponentLifecycleState.STARTING,
                ComponentLifecycleState.RUNNING,
            ):
                self._logger.debug("TCP client start called while already running.")
                return
            self._last_connect_error = None
            self._has_started = True
            # Keep dispatcher startup and the initial lifecycle transitions in one
            # locked section so stop() cannot observe a half-started client.
            await self._event_dispatcher.start()
            starting_event = self._apply_lifecycle_state(ComponentLifecycleState.STARTING)
            self._supervisor_task = asyncio.create_task(
                self._supervise(), name="tcp-client-supervisor"
            )
            running_event = self._apply_lifecycle_state(ComponentLifecycleState.RUNNING)
        try:
            await self._emit_lifecycle_event(starting_event)
            await self._emit_lifecycle_event(running_event)
        except (Exception, asyncio.CancelledError):
            await self._rollback_failed_startup()
            raise

    async def stop(self) -> None:
        """Stop supervision, close active resources, and publish STOPPED."""
        self._assert_owner_loop()
        self._logger.debug("Stopping TCP client.")
        stopping_event: ComponentLifecycleChangedEvent | None = None
        stopped_event: ComponentLifecycleChangedEvent | None = None
        should_stop_dispatcher = False
        await_supervisor_completion_only = False
        skip_await_supervisor = False
        stop_called_from_supervisor = False
        cancel_supervisor_after_local_cleanup = False
        stop_waiter: asyncio.Future[None] | None = None
        deferred_close_waiters: tuple[asyncio.Future[None], ...] = ()
        stop_waiter_completion_deferred = False
        active_inline_handler_stop = False
        defer_stop_events = False
        owns_stop = False
        current_task = asyncio.current_task()
        async with self._state_lock:
            if self._stop_waiter is not None:
                if current_task is self._stop_owner_task:
                    # Inline STOPPING handlers can re-enter stop() from the
                    # owner task; waiting on the owner waiter would deadlock.
                    return
                stop_waiter = self._stop_waiter
            elif (
                self._lifecycle_state == ComponentLifecycleState.STOPPED
                and self._supervisor_task is None
                and self._heartbeat_sender is None
                and self._connection is None
                and not self._event_dispatcher.is_running
            ):
                return
            else:
                # Publish the shared waiter before slow teardown so overlapping
                # stop callers join this stop path instead of planning another.
                loop = (
                    current_task.get_loop()
                    if current_task is not None
                    else asyncio.get_event_loop()
                )
                stop_waiter = loop.create_future()
                self._stop_waiter = stop_waiter
                self._stop_owner_task = current_task
                owns_stop = True
            if owns_stop and self._lifecycle_state == ComponentLifecycleState.STOPPING:
                supervisor_task = self._supervisor_task
                if supervisor_task is not None and supervisor_task.done():
                    self._supervisor_task = None
                    supervisor_task = None
                else:
                    await_supervisor_completion_only = (
                        supervisor_task is not None and supervisor_task is not current_task
                    )
                should_stop_dispatcher = self._event_dispatcher.is_running
            elif owns_stop and self._lifecycle_state == ComponentLifecycleState.STOPPED:
                supervisor_task = self._supervisor_task
                if supervisor_task is not None and supervisor_task.done():
                    self._supervisor_task = None
                    supervisor_task = None
                else:
                    await_supervisor_completion_only = (
                        supervisor_task is not None and supervisor_task is not current_task
                    )
                should_stop_dispatcher = self._event_dispatcher.is_running
            elif owns_stop:
                stopping_event = self._apply_lifecycle_state(ComponentLifecycleState.STOPPING)
                supervisor_task = self._supervisor_task
                self._supervisor_task = None
                connection = self._connection
                connection_startup_in_progress = self._starting_connection is not None or (
                    connection is not None and connection._opening_event_task is not None
                )
                stop_called_from_supervisor = (
                    supervisor_task is not None and current_task is supervisor_task
                )
                handler_originated_stop = (
                    self._event_dispatcher.current_task_is_dispatching_handler()
                    or self._event_dispatcher.current_task_has_handler_origin_context()
                    or self._event_dispatcher.current_task_inherits_handler_origin_context()
                )
                active_inline_handler_stop = (
                    not handler_originated_stop
                    and self._event_dispatcher.has_active_handler_context()
                    and self._event_dispatcher.current_task_would_deliver_inline()
                )
                defer_stop_events = handler_originated_stop or active_inline_handler_stop
                should_stop_dispatcher = True
                skip_await_supervisor = supervisor_task is not None and (
                    stop_called_from_supervisor
                    or (
                        current_task is not None and self._event_dispatcher.current_task_is_worker()
                    )
                    or (current_task is not None and handler_originated_stop)
                )
                cancel_supervisor_after_local_cleanup = (
                    supervisor_task is not None
                    and skip_await_supervisor
                    and not stop_called_from_supervisor
                    and not connection_startup_in_progress
                )
        if not owns_stop:
            if stop_waiter is not None and not (
                self._event_dispatcher.current_task_is_dispatching_handler()
                or self._event_dispatcher.current_task_has_handler_origin_context()
                or self._event_dispatcher.current_task_inherits_handler_origin_context()
            ):
                await asyncio.shield(stop_waiter)
            return
        handler_originated_stop = (
            self._event_dispatcher.current_task_is_dispatching_handler()
            or self._event_dispatcher.current_task_has_handler_origin_context()
            or self._event_dispatcher.current_task_inherits_handler_origin_context()
        )
        active_inline_handler_stop = (
            not handler_originated_stop
            and self._event_dispatcher.has_active_handler_context()
            and self._event_dispatcher.current_task_would_deliver_inline()
        )
        defer_stop_events = handler_originated_stop or active_inline_handler_stop
        try:
            first_error: BaseException | None = None
            if not defer_stop_events:
                try:
                    await self._emit_lifecycle_event(stopping_event)
                except (Exception, asyncio.CancelledError) as error:
                    first_error = error
            try:
                if supervisor_task is not None and not skip_await_supervisor:
                    try:
                        if not await_supervisor_completion_only:
                            supervisor_task.cancel()
                        await await_task_completion_preserving_cancellation(
                            cast(asyncio.Task[object], supervisor_task)
                        )
                    finally:
                        if self._supervisor_task is supervisor_task:
                            self._supervisor_task = None
            except (Exception, asyncio.CancelledError) as error:
                if first_error is None:
                    first_error = error
            try:
                # A cancelled supervisor wait must not skip local resource cleanup.
                # Preserve the first error, finish teardown, then re-raise below.
                await self._stop_heartbeat_sender()
                deferred_close_waiters = await self._close_current_connection()
                if cancel_supervisor_after_local_cleanup and supervisor_task is not None:
                    supervisor_task.cancel()
            except (Exception, asyncio.CancelledError) as error:
                if first_error is None:
                    first_error = error
            if should_stop_dispatcher:
                async with self._state_lock:
                    if self._lifecycle_state == ComponentLifecycleState.STOPPING:
                        stopped_event = self._apply_lifecycle_state(ComponentLifecycleState.STOPPED)
                if defer_stop_events:
                    if first_error is None:
                        try:
                            await self._event_dispatcher.stop_from_handler_origin()
                        except (Exception, asyncio.CancelledError) as error:
                            first_error = error
                    if first_error is None:
                        self._complete_stop_waiter_after_deferred_stop_events(
                            stop_waiter=stop_waiter,
                            stopping_event=stopping_event,
                            stopped_event=stopped_event,
                            deferred_close_waiters=deferred_close_waiters,
                            supervisor_task=(
                                cast(asyncio.Task[object], supervisor_task)
                                if supervisor_task is not None
                                and supervisor_task is not current_task
                                else None
                            ),
                            stop_dispatcher=True,
                        )
                        stop_waiter_completion_deferred = True
                else:
                    if first_error is None:
                        try:
                            await self._emit_lifecycle_event(stopped_event)
                        except (Exception, asyncio.CancelledError) as error:
                            first_error = error
                if not defer_stop_events:
                    try:
                        await self._event_dispatcher.stop()
                    except (Exception, asyncio.CancelledError) as error:
                        if first_error is None:
                            first_error = error
            elif defer_stop_events and first_error is None:
                self._complete_stop_waiter_after_deferred_stop_events(
                    stop_waiter=stop_waiter,
                    stopping_event=stopping_event,
                    stopped_event=None,
                    deferred_close_waiters=deferred_close_waiters,
                    supervisor_task=(
                        cast(asyncio.Task[object], supervisor_task)
                        if supervisor_task is not None and supervisor_task is not current_task
                        else None
                    ),
                    stop_dispatcher=False,
                )
                stop_waiter_completion_deferred = True
            if (
                active_inline_handler_stop
                and first_error is None
                and stop_waiter is not None
                and not stop_waiter.done()
            ):
                await asyncio.shield(stop_waiter)
            if first_error is not None:
                raise first_error
        except (Exception, asyncio.CancelledError) as error:
            if stop_waiter is not None and not stop_waiter.done():
                stop_waiter.set_exception(error)
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    stop_waiter.exception()
            raise
        else:
            if stop_waiter is not None and not stop_waiter.done():
                if stop_waiter_completion_deferred:
                    pass
                elif defer_stop_events and deferred_close_waiters:
                    self._complete_stop_waiter_after_deferred_closes(
                        stop_waiter, deferred_close_waiters
                    )
                    stop_waiter_completion_deferred = True
                else:
                    stop_waiter.set_result(None)
        finally:
            if not stop_waiter_completion_deferred:
                async with self._state_lock:
                    if self._stop_waiter is stop_waiter:
                        self._stop_waiter = None
                        self._stop_owner_task = None
        self._logger.debug("TCP client stopped.")

    async def __aenter__(self) -> AsyncioTcpClient:
        """Start the client and return ``self``."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Stop the client unconditionally."""
        await self.stop()

    async def wait_until_connected(
        self, timeout_seconds: float | None = None, poll_interval_seconds: float = 0.1
    ) -> ConnectionProtocol:
        """
        Wait for an active connected connection.

        Raises:
            ValueError: If ``poll_interval_seconds`` is less than or equal to zero.
            ConnectionError: If the client stops before a connection is available.
            asyncio.TimeoutError: If ``timeout_seconds`` elapses first.
        """
        self._assert_owner_loop()
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0.")
        coro = wait_until_client_connected(
            get_connection=lambda: self.connection,
            get_lifecycle_state=lambda: self._lifecycle_state,
            get_has_started=lambda: self._has_started,
            get_last_connect_error=lambda: self._last_connect_error,
            get_reconnect_enabled=lambda: self._settings.reconnect.enabled,
            get_status_version=lambda: self._status_version,
            status_changed=self._status_changed,
            host=self._settings.host,
            port=self._settings.port,
            poll_interval_seconds=poll_interval_seconds,
        )
        if timeout_seconds is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout_seconds)

    async def _supervise(self) -> None:
        """Delegate the connect/reconnect loop to the supervision helper."""
        await self._connection_supervisor.run()

    async def _rollback_failed_startup(self) -> None:
        """Clean up startup-owned resources after lifecycle publication fails."""
        async with self._state_lock:
            supervisor_task = self._supervisor_task
            self._supervisor_task = None
            if self._lifecycle_state == ComponentLifecycleState.RUNNING:
                self._apply_lifecycle_state(ComponentLifecycleState.STOPPING)
                self._apply_lifecycle_state(ComponentLifecycleState.STOPPED)
            elif self._lifecycle_state == ComponentLifecycleState.STARTING:
                self._apply_lifecycle_state(ComponentLifecycleState.STOPPED)
        if supervisor_task is not None:
            supervisor_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                _ = await cast(Awaitable[object], supervisor_task)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await self._stop_heartbeat_sender()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await self._close_current_connection()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await self._event_dispatcher.stop()

    async def _connect_once(self) -> None:
        """Open one TCP session and attach connection-level event handling."""
        connection = await connect_once(
            settings=self._settings,
            connection_opener=self._connection_opener,
            event_dispatcher=self._event_dispatcher,
            on_closed_callback=self._on_connection_closed,
            logger=self._logger,
            component_id=self._component_id,
            on_connection_created=self._track_starting_connection,
            on_connection_ready=self._attach_starting_connection,
        )
        if self._starting_connection is connection:
            self._starting_connection = None
        if self._connection is connection:
            if (
                self._lifecycle_state == ComponentLifecycleState.RUNNING
                and connection.state == ConnectionState.CONNECTED
            ):
                await self._start_heartbeat_sender(connection)
            return
        if (
            self._lifecycle_state != ComponentLifecycleState.RUNNING
            or connection.state == ConnectionState.CLOSED
        ):
            if self._connection is connection:
                self._connection = None
            if connection.state != ConnectionState.CLOSED:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await connection.close()
            self._connection_closed_event.set()
            self._notify_status_changed()
            return
        self._connection = connection
        self._connection_closed_event.clear()
        self._notify_status_changed()
        await self._start_heartbeat_sender(connection)

    def _track_starting_connection(self, connection: AsyncioTcpConnection) -> None:
        """Retain a just-created connection so stop() can close it during opened publication."""
        self._starting_connection = connection

    async def _attach_starting_connection(self, connection: AsyncioTcpConnection) -> None:
        """Attach a connected socket before opened-event publication."""
        if (
            self._lifecycle_state != ComponentLifecycleState.RUNNING
            or connection.state != ConnectionState.CONNECTED
        ):
            if self._starting_connection is connection:
                self._starting_connection = None
            return
        self._connection = connection
        if self._starting_connection is connection:
            self._starting_connection = None
        self._connection_closed_event.clear()
        self._notify_status_changed()

    async def _start_heartbeat_sender(self, connection: AsyncioTcpConnection) -> None:
        """Create and retain the optional heartbeat sender bound to ``connection``."""
        self._heartbeat_sender = await start_heartbeat_sender(
            connection=connection,
            settings=self._settings,
            heartbeat_provider=self._heartbeat_provider,
            event_dispatcher=self._event_dispatcher,
            logger=self._logger,
        )

    async def _stop_heartbeat_sender(self) -> None:
        """Stop and detach the current heartbeat sender, if one exists."""
        sender = self._heartbeat_sender
        self._heartbeat_sender = None
        await stop_heartbeat_sender(sender=sender, logger=self._logger)

    async def _close_current_connection(self) -> tuple[asyncio.Future[None], ...]:
        """Detach and close the current or startup-pending connection, if one is tracked."""
        connection = self._connection
        starting_connection = self._starting_connection
        self._starting_connection = None
        close_targets: list[AsyncioTcpConnection] = []
        deferred_close_waiters: list[asyncio.Future[None]] = []
        for candidate in (starting_connection, connection):
            if (
                candidate is not None
                and candidate.state != ConnectionState.CLOSED
                and candidate not in close_targets
            ):
                close_targets.append(candidate)
        try:
            for target in close_targets:
                await target.close()
                if waiter := target._pending_deferred_close_waiter():
                    deferred_close_waiters.append(waiter)
                elif (
                    not target._closed_event_published
                    and (
                        self._event_dispatcher.has_active_handler_context(target.connection_id)
                        or self._event_dispatcher.current_task_inherits_handler_origin_context(
                            target.connection_id
                        )
                    )
                    and (waiter := await target._ensure_deferred_close_publication_waiter())
                ):
                    deferred_close_waiters.append(waiter)
        finally:
            if self._connection is connection or self._connection is starting_connection:
                self._connection = None
            self._connection_closed_event.set()
            self._notify_status_changed()
        return tuple(deferred_close_waiters)

    def _complete_stop_waiter_after_deferred_stop_events(
        self,
        *,
        stop_waiter: asyncio.Future[None] | None,
        stopping_event: ComponentLifecycleChangedEvent | None,
        stopped_event: ComponentLifecycleChangedEvent | None,
        deferred_close_waiters: tuple[asyncio.Future[None], ...],
        supervisor_task: asyncio.Task[object] | None,
        stop_dispatcher: bool,
    ) -> None:
        """Publish terminal stop events after a handler-originated stop unwinds."""

        async def _complete() -> None:
            try:
                while self._event_dispatcher.has_active_handler_context():
                    await asyncio.sleep(0)
                with self._event_dispatcher.inline_delivery_context():
                    await self._emit_lifecycle_event(stopping_event)
                    if deferred_close_waiters:
                        await asyncio.gather(
                            *(asyncio.shield(waiter) for waiter in deferred_close_waiters)
                        )
                    if supervisor_task is not None:
                        await await_task_completion_preserving_cancellation(supervisor_task)
                    await self._emit_lifecycle_event(stopped_event)
                if stop_dispatcher:
                    await self._event_dispatcher.stop()
            except BaseException as error:
                if stop_waiter is not None and not stop_waiter.done():
                    stop_waiter.set_exception(error)
                    with contextlib.suppress(BaseException):
                        stop_waiter.exception()
            else:
                if stop_waiter is not None and not stop_waiter.done():
                    stop_waiter.set_result(None)
            finally:
                async with self._state_lock:
                    if self._stop_waiter is stop_waiter:
                        self._stop_waiter = None
                        self._stop_owner_task = None

        _ = asyncio.create_task(_complete())

    def _complete_stop_waiter_after_deferred_closes(
        self,
        stop_waiter: asyncio.Future[None],
        deferred_close_waiters: tuple[asyncio.Future[None], ...],
    ) -> None:
        """Release external stop waiters after handler-originated deferred close publication."""

        async def _complete() -> None:
            try:
                await asyncio.gather(*(asyncio.shield(waiter) for waiter in deferred_close_waiters))
            except BaseException as error:
                if not stop_waiter.done():
                    stop_waiter.set_exception(error)
                    with contextlib.suppress(BaseException):
                        stop_waiter.exception()
            else:
                if not stop_waiter.done():
                    stop_waiter.set_result(None)
            finally:
                async with self._state_lock:
                    if self._stop_waiter is stop_waiter:
                        self._stop_waiter = None
                        self._stop_owner_task = None

        _ = asyncio.create_task(_complete())

    async def _on_connection_closed(self, connection: AsyncioTcpConnection) -> None:
        """Detach the closed connection, notify waiters, and stop heartbeats."""
        if self._connection is connection:
            self._connection = None
            if self._lifecycle_state != ComponentLifecycleState.STOPPING:
                self._connection_closed_event.set()
                self._notify_status_changed()
        await self._stop_heartbeat_sender()

    def _on_lifecycle_state_applied(self, target: ComponentLifecycleState) -> None:
        """Mirror published lifecycle state into runtime flags and wait conditions."""
        self._running = target in (
            ComponentLifecycleState.STARTING,
            ComponentLifecycleState.RUNNING,
        )
        self._notify_status_changed()

    def _apply_lifecycle_state(
        self, target: ComponentLifecycleState
    ) -> ComponentLifecycleChangedEvent | None:
        """Apply one lifecycle transition and return the corresponding event, if any."""
        return self._lifecycle_publisher.apply(target)

    async def _emit_lifecycle_event(self, event: ComponentLifecycleChangedEvent | None) -> None:
        """Emit a lifecycle event when the transition publisher produced one."""
        await emit_lifecycle_event(dispatcher=self._event_dispatcher, event=event)

    async def _transition_lifecycle_state(self, target: ComponentLifecycleState) -> None:
        """Convenience helper that applies and emits a lifecycle transition."""
        event = self._apply_lifecycle_state(target)
        await self._emit_lifecycle_event(event)

    def _resolve_error_policy(self) -> ErrorPolicy:
        """Resolve the effective error policy after considering reconnect settings."""
        if self._settings.error_policy is not None:
            return self._settings.error_policy
        return ErrorPolicy.RETRY if self._settings.reconnect.enabled else ErrorPolicy.FAIL_FAST

    def _notify_status_changed(self) -> None:
        """Bump the status version and wake polling/waiting helpers."""
        self._status_version += 1
        self._status_changed.set()
