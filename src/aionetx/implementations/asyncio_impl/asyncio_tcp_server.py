"""
Asyncio TCP server lifecycle and multi-connection coordination.

This module coordinates accept-loop lifecycle, per-connection registration,
and optional heartbeat sender ownership for server-side sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Awaitable
from types import TracebackType
from typing import cast

from aionetx.api.bytes_like import BytesLike
from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_protocol import ConnectionProtocol
from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.network_event_handler_protocol import NetworkEventHandlerProtocol
from aionetx.api.tcp_server import TcpServerProtocol, TcpServerSettings
from aionetx.implementations.asyncio_impl._tcp_server_helpers import (
    broadcast_to_connections,
    handle_accepted_client,
    report_teardown_errors,
    start_server_heartbeat_if_needed,
    stop_server_heartbeat_senders,
    wait_until_server_running,
)
from aionetx.implementations.asyncio_impl.asyncio_heartbeat_sender import AsyncioHeartbeatSender
from aionetx.implementations.asyncio_impl.asyncio_tcp_connection import AsyncioTcpConnection
from aionetx.implementations.asyncio_impl.identifier_utils import (
    tcp_server_component_id,
    tcp_server_connection_id,
)
from aionetx.implementations.asyncio_impl.event_dispatcher import (
    AsyncioEventDispatcher,
    DispatcherRuntimeStats,
)
from aionetx.implementations.asyncio_impl.lifecycle_internal import LifecycleRole
from aionetx.implementations.asyncio_impl.lifecycle_internal import (
    LifecycleTransitionPublisher,
    apply_stopped_transition_if_stopping,
    apply_stopping_transition_if_active,
    emit_lifecycle_event,
)
from aionetx.implementations.asyncio_impl.runtime_utils import (
    assert_running_on_owner_loop,
    configure_listener_bind_socket,
    validate_heartbeat_provider,
)

logger = logging.getLogger(__name__)


class AsyncioTcpServer(TcpServerProtocol):
    """Managed asyncio TCP server with explicit connection tracking."""

    def __init__(
        self,
        settings: TcpServerSettings,
        event_handler: NetworkEventHandlerProtocol,
        heartbeat_provider: HeartbeatProviderProtocol | None = None,
    ) -> None:
        settings.validate()
        validate_heartbeat_provider(
            heartbeat_settings=settings.heartbeat,
            heartbeat_provider=heartbeat_provider,
        )
        self._settings = settings
        self._heartbeat_provider = heartbeat_provider
        self._server: asyncio.AbstractServer | None = None
        self._has_started = False
        self._status_changed = asyncio.Event()
        self._status_version = 0
        self._lifecycle_state = ComponentLifecycleState.STOPPED
        self._connections: dict[str, AsyncioTcpConnection] = {}
        self._heartbeat_senders: dict[str, AsyncioHeartbeatSender] = {}
        self._connection_sequence = 0
        self._state_lock = asyncio.Lock()
        self._stop_waiter: asyncio.Future[None] | None = None
        self._component_id = tcp_server_component_id(settings.host, settings.port)
        self._logger = logging.LoggerAdapter(
            logger, {"component": "tcp_server", "host": settings.host, "port": settings.port}
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
            role=LifecycleRole.TCP_SERVER,
            get_state=lambda: self._lifecycle_state,
            set_state=lambda state: setattr(self, "_lifecycle_state", state),
            on_state_applied=lambda _state: self._notify_status_changed(),
        )
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    @property
    def connections(self) -> tuple[ConnectionProtocol, ...]:
        """Return an immutable snapshot of connections known to the server."""
        return tuple(self._connections.values())

    @property
    def lifecycle_state(self) -> ComponentLifecycleState:
        """Current component lifecycle state exposed by the managed server."""
        return self._lifecycle_state

    def __repr__(self) -> str:
        return (
            f"AsyncioTcpServer("
            f"host={self._settings.host!r}, "
            f"port={self._settings.port!r}, "
            f"state={self._lifecycle_state.value!r})"
        )

    @property
    def dispatcher_runtime_stats(self) -> DispatcherRuntimeStats:
        """Dispatcher operational snapshot for runtime diagnostics."""
        return self._event_dispatcher.runtime_stats

    def _assert_owner_loop(self) -> None:
        """Raise RuntimeError if called outside the owner event loop."""
        self._owner_loop = assert_running_on_owner_loop(
            class_name=type(self).__name__, owner_loop=self._owner_loop
        )

    async def start(self) -> None:
        """Start the accept loop and publish lifecycle transitions."""
        self._assert_owner_loop()
        # Keep dispatcher startup and the STARTING transition in one locked
        # section so stop() cannot observe a half-started server.
        lifecycle_event: ComponentLifecycleChangedEvent | None = None
        async with self._state_lock:
            if self._lifecycle_state in (
                ComponentLifecycleState.STARTING,
                ComponentLifecycleState.RUNNING,
            ):
                self._logger.debug("TCP server start called while already running.")
                return
            self._logger.debug("Starting TCP server.")
            self._has_started = True
            await self._event_dispatcher.start()
            lifecycle_event = self._apply_lifecycle_state(ComponentLifecycleState.STARTING)
            self._notify_status_changed()
        try:
            await self._emit_lifecycle_event(lifecycle_event)
        except (Exception, asyncio.CancelledError):
            await self._rollback_failed_startup()
            raise
        server: asyncio.AbstractServer | None = None
        listening_socket: socket.socket | None = None
        try:
            listening_socket = self._create_listening_socket()
            server = await asyncio.start_server(
                self._handle_client,
                sock=listening_socket,
            )
        except (Exception, asyncio.CancelledError):
            # CancelledError is not an Exception on supported Python versions,
            # but startup cancellation still owns rollback before propagating.
            lifecycle_event = None
            if listening_socket is not None:
                with contextlib.suppress(Exception):
                    listening_socket.close()
            async with self._state_lock:
                self._server = None
                lifecycle_event = self._apply_lifecycle_state(ComponentLifecycleState.STOPPED)
            try:
                await self._emit_lifecycle_event(lifecycle_event)
            finally:
                await self._stop_dispatcher_during_startup_rollback()
                self._notify_status_changed()
            raise

        lifecycle_event = None
        async with self._state_lock:
            if self._lifecycle_state != ComponentLifecycleState.STARTING:
                # stop() may have completed while ``asyncio.start_server()``
                # was still in flight. Close this just-created server locally
                # because the stop path could not yet see it.
                server.close()
                await server.wait_closed()
                return
            self._server = server
            lifecycle_event = self._apply_lifecycle_state(ComponentLifecycleState.RUNNING)
            self._notify_status_changed()
        try:
            await self._emit_lifecycle_event(lifecycle_event)
        except (Exception, asyncio.CancelledError):
            await self._rollback_failed_startup(server=server)
            raise

    async def stop(self) -> None:
        """Stop accepting clients and best-effort close active resources."""
        self._assert_owner_loop()
        stopping_event: ComponentLifecycleChangedEvent | None = None
        stopped_event: ComponentLifecycleChangedEvent | None = None
        should_transition_to_stopped = False
        stop_waiter: asyncio.Future[None] | None = None
        deferred_close_waiters: tuple[asyncio.Future[None], ...] = ()
        stop_waiter_completion_deferred = False
        dispatcher_stopped_from_handler_origin = False
        active_inline_handler_stop = False
        defer_stop_events = False
        owns_stop = False
        heartbeat_senders: tuple[AsyncioHeartbeatSender, ...] = ()
        connections: tuple[AsyncioTcpConnection, ...] = ()
        async with self._state_lock:
            self._logger.debug("Stopping TCP server.")
            if (
                self._lifecycle_state == ComponentLifecycleState.STOPPING
                and self._stop_waiter is not None
            ):
                stop_waiter = self._stop_waiter
            else:
                stop_waiter = asyncio.get_running_loop().create_future()
                self._stop_waiter = stop_waiter
                owns_stop = True
            server = self._server
            self._server = None
            if owns_stop:
                stopping_event = apply_stopping_transition_if_active(
                    get_state=lambda: self._lifecycle_state,
                    apply_transition=self._apply_lifecycle_state,
                )
                if stopping_event is not None:
                    should_transition_to_stopped = True
                heartbeat_senders = tuple(self._heartbeat_senders.values())
                self._heartbeat_senders.clear()
                connections = tuple(self._connections.values())
                self._connections.clear()
        if not owns_stop:
            if stop_waiter is not None:
                if (
                    self._event_dispatcher.current_task_is_dispatching_handler()
                    or self._event_dispatcher.current_task_has_handler_origin_context()
                    or self._event_dispatcher.current_task_inherits_handler_origin_context()
                ):
                    return
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
            else:
                try:
                    await self._event_dispatcher.stop_from_handler_origin()
                    dispatcher_stopped_from_handler_origin = True
                except (Exception, asyncio.CancelledError) as error:
                    first_error = error
            try:
                if server is not None:
                    server.close()
                await stop_server_heartbeat_senders(
                    senders=heartbeat_senders,
                    event_dispatcher=self._event_dispatcher,
                    logger=self._logger,
                    component_id=self._component_id,
                )
                if connections:
                    close_context = (
                        self._event_dispatcher.inline_delivery_context()
                        if defer_stop_events
                        else contextlib.nullcontext()
                    )
                    with close_context:
                        close_results = await asyncio.gather(
                            *(connection.close() for connection in connections),
                            return_exceptions=True,
                        )
                    deferred_close_waiters = await self._pending_deferred_close_waiters(connections)
                    await report_teardown_errors(
                        event_dispatcher=self._event_dispatcher,
                        logger=self._logger,
                        component_id=self._component_id,
                        operation="TCP connection close",
                        targets=(connection.connection_id for connection in connections),
                        results=close_results,
                    )
                if server is not None:
                    await server.wait_closed()
            except (Exception, asyncio.CancelledError) as error:
                if first_error is None:
                    first_error = error
            if should_transition_to_stopped:
                async with self._state_lock:
                    stopped_event = apply_stopped_transition_if_stopping(
                        get_state=lambda: self._lifecycle_state,
                        apply_transition=self._apply_lifecycle_state,
                    )
                if defer_stop_events:
                    if first_error is None:
                        self._complete_stop_waiter_after_deferred_stop_events(
                            stop_waiter=stop_waiter,
                            stopping_event=stopping_event,
                            stopped_event=stopped_event,
                            deferred_close_waiters=deferred_close_waiters,
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
            elif (
                defer_stop_events
                and first_error is None
                and not dispatcher_stopped_from_handler_origin
            ):
                try:
                    await self._event_dispatcher.stop_from_handler_origin()
                except (Exception, asyncio.CancelledError) as error:
                    first_error = error
            if not should_transition_to_stopped and defer_stop_events and first_error is None:
                self._complete_stop_waiter_after_deferred_stop_events(
                    stop_waiter=stop_waiter,
                    stopping_event=stopping_event,
                    stopped_event=None,
                    deferred_close_waiters=deferred_close_waiters,
                )
                stop_waiter_completion_deferred = True
            if (
                active_inline_handler_stop
                and first_error is None
                and stop_waiter is not None
                and not stop_waiter.done()
            ):
                await asyncio.shield(stop_waiter)
            self._notify_status_changed()
            self._logger.debug("TCP server stopped.")
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

    def _complete_stop_waiter_after_deferred_closes(
        self,
        stop_waiter: asyncio.Future[None],
        deferred_close_waiters: tuple[asyncio.Future[None], ...],
    ) -> None:
        """Release external stop waiters after handler-originated deferred close publication."""

        async def _complete() -> None:
            try:
                await asyncio.gather(*(asyncio.shield(waiter) for waiter in deferred_close_waiters))
            except (Exception, asyncio.CancelledError) as error:
                if not stop_waiter.done():
                    stop_waiter.set_exception(error)
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        stop_waiter.exception()
            else:
                if not stop_waiter.done():
                    stop_waiter.set_result(None)
            finally:
                async with self._state_lock:
                    if self._stop_waiter is stop_waiter:
                        self._stop_waiter = None

        _ = asyncio.create_task(_complete())

    def _complete_stop_waiter_after_deferred_stop_events(
        self,
        *,
        stop_waiter: asyncio.Future[None] | None,
        stopping_event: ComponentLifecycleChangedEvent | None,
        stopped_event: ComponentLifecycleChangedEvent | None,
        deferred_close_waiters: tuple[asyncio.Future[None], ...],
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
                    await self._emit_lifecycle_event(stopped_event)
                await self._event_dispatcher.stop()
            except (Exception, asyncio.CancelledError) as error:
                if stop_waiter is not None and not stop_waiter.done():
                    stop_waiter.set_exception(error)
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        stop_waiter.exception()
            else:
                if stop_waiter is not None and not stop_waiter.done():
                    stop_waiter.set_result(None)
            finally:
                async with self._state_lock:
                    if self._stop_waiter is stop_waiter:
                        self._stop_waiter = None

        _ = asyncio.create_task(_complete())

    async def _pending_deferred_close_waiters(
        self, connections: tuple[AsyncioTcpConnection, ...]
    ) -> tuple[asyncio.Future[None], ...]:
        """Collect or create deferred close waiters from managed TCP connections."""
        waiters: list[asyncio.Future[None]] = []
        for connection in connections:
            get_waiter = getattr(connection, "_pending_deferred_close_waiter", None)
            if get_waiter is None:
                continue
            if waiter := get_waiter():
                waiters.append(waiter)
                continue
            if (
                not connection._closed_event_published
                and (
                    self._event_dispatcher.has_active_handler_context(connection.connection_id)
                    or self._event_dispatcher.current_task_inherits_handler_origin_context(
                        connection.connection_id
                    )
                )
                and (waiter := await connection._ensure_deferred_close_publication_waiter())
            ):
                waiters.append(waiter)
        return tuple(waiters)

    async def __aenter__(self) -> AsyncioTcpServer:
        """Start the server and return ``self``."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Stop the server unconditionally."""
        await self.stop()

    async def wait_until_running(
        self, timeout_seconds: float | None = None, poll_interval_seconds: float = 0.1
    ) -> None:
        """
        Wait until lifecycle reaches ``RUNNING``.

        Raises:
            ValueError: ``poll_interval_seconds`` <= 0.
            ConnectionError: Server stopped before reaching RUNNING.
            asyncio.TimeoutError: ``timeout_seconds`` elapsed first.
        """
        self._assert_owner_loop()
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0.")
        coro = wait_until_server_running(
            get_lifecycle_state=lambda: self._lifecycle_state,
            get_has_started=lambda: self._has_started,
            get_status_version=lambda: self._status_version,
            status_changed=self._status_changed,
            host=self._settings.host,
            port=self._settings.port,
            poll_interval_seconds=poll_interval_seconds,
        )
        if timeout_seconds is None:
            _ = await cast(Awaitable[object], coro)
            return
        await asyncio.wait_for(coro, timeout=timeout_seconds)

    async def broadcast(self, data: BytesLike) -> None:
        """Send data to all live clients; per-connection failures are best-effort."""
        self._assert_owner_loop()
        await broadcast_to_connections(
            connections=tuple(self._connections.values()),
            data=data,
            event_dispatcher=self._event_dispatcher,
            component_id=self._component_id,
            logger=self._logger,
            concurrency_limit=self._settings.broadcast_concurrency_limit,
            send_timeout_seconds=self._settings.broadcast_send_timeout_seconds,
        )

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Initialize one accepted socket into managed connection state."""

        def _next_connection_id() -> str:
            self._connection_sequence += 1
            return self._build_connection_id(
                writer.get_extra_info("peername"), self._connection_sequence
            )

        await handle_accepted_client(
            reader=reader,
            writer=writer,
            state_lock=self._state_lock,
            get_lifecycle_state=lambda: self._lifecycle_state,
            get_connection_id=_next_connection_id,
            connections=self._connections,
            event_dispatcher=self._event_dispatcher,
            max_connections=self._settings.max_connections,
            receive_buffer_size=self._settings.receive_buffer_size,
            idle_timeout_seconds=self._settings.connection_idle_timeout_seconds,
            connection_send_timeout_seconds=self._settings.connection_send_timeout_seconds,
            on_closed_callback=self._on_connection_closed,
            heartbeat_settings=self._settings.heartbeat,
            heartbeat_provider=self._heartbeat_provider,
            heartbeat_senders=self._heartbeat_senders,
            component_id=self._component_id,
            logger=self._logger,
        )

    async def _start_heartbeat_if_needed(self, connection: AsyncioTcpConnection) -> None:
        """Create and register a heartbeat sender for ``connection`` when configured."""
        await start_server_heartbeat_if_needed(
            connection=connection,
            heartbeat_settings=self._settings.heartbeat,
            heartbeat_provider=self._heartbeat_provider,
            event_dispatcher=self._event_dispatcher,
            state_lock=self._state_lock,
            get_lifecycle_state=lambda: self._lifecycle_state,
            connections=self._connections,
            heartbeat_senders=self._heartbeat_senders,
            logger=self._logger,
        )

    async def _on_connection_closed(self, connection: AsyncioTcpConnection) -> None:
        """Remove closed connection state and stop its heartbeat sender, if any."""
        async with self._state_lock:
            sender = self._heartbeat_senders.pop(connection.connection_id, None)
            self._connections.pop(connection.connection_id, None)
        if sender is not None:
            await sender.stop()

    def _apply_lifecycle_state(
        self, next_state: ComponentLifecycleState
    ) -> ComponentLifecycleChangedEvent | None:
        """Apply one lifecycle transition and return the corresponding event, if any."""
        return self._lifecycle_publisher.apply(next_state)

    async def _emit_lifecycle_event(self, event: ComponentLifecycleChangedEvent | None) -> None:
        """Emit a lifecycle event when the transition publisher produced one."""
        await emit_lifecycle_event(dispatcher=self._event_dispatcher, event=event)

    async def _set_lifecycle_state(self, next_state: ComponentLifecycleState) -> None:
        """Convenience helper that applies and emits a lifecycle transition."""
        event = self._apply_lifecycle_state(next_state)
        await self._emit_lifecycle_event(event)

    async def _rollback_failed_startup(self, server: asyncio.AbstractServer | None = None) -> None:
        """Return startup-owned resources to STOPPED after lifecycle publication fails."""
        async with self._state_lock:
            owned_server = self._server
            self._server = None
            if self._lifecycle_state == ComponentLifecycleState.RUNNING:
                self._apply_lifecycle_state(ComponentLifecycleState.STOPPING)
                self._apply_lifecycle_state(ComponentLifecycleState.STOPPED)
            elif self._lifecycle_state == ComponentLifecycleState.STARTING:
                self._apply_lifecycle_state(ComponentLifecycleState.STOPPED)
            self._notify_status_changed()
        for server_to_close in (server, owned_server):
            if server_to_close is not None:
                server_to_close.close()
                with contextlib.suppress(Exception):
                    await server_to_close.wait_closed()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await self._event_dispatcher.stop()

    async def _stop_dispatcher_during_startup_rollback(self) -> None:
        """
        Stop dispatcher cleanup before propagating startup cancellation.

        The startup task owns dispatcher cleanup once it has published STARTING.
        If the caller cancels again while rollback is already stopping the
        dispatcher, cleanup must still converge before cancellation is re-raised.
        """
        stop_task = asyncio.create_task(
            self._event_dispatcher.stop(),
            name=f"{self._component_id}-startup-rollback-dispatcher-stop",
        )
        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(stop_task)
            raise

    def _build_connection_id(self, peer_info: object, sequence: int) -> str:
        """Build the stable per-connection identifier used by server-side events."""
        return tcp_server_connection_id(peer_info, sequence)

    def _create_listening_socket(self) -> socket.socket:
        """Create and bind the server listening socket with the configured bind policy."""
        last_error: OSError | None = None
        addrinfo_list = socket.getaddrinfo(
            self._settings.host,
            self._settings.port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
            flags=socket.AI_PASSIVE,
        )
        for family, socktype, proto, _canonname, sockaddr in addrinfo_list:
            sock = socket.socket(family, socktype, proto)
            try:
                configure_listener_bind_socket(sock, allow_address_reuse=False)
                if family == socket.AF_INET6:
                    with contextlib.suppress(OSError):
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind(sockaddr)
                sock.listen(self._settings.backlog)
                sock.setblocking(False)
                return sock
            except OSError as error:
                last_error = error
                with contextlib.suppress(OSError):
                    sock.close()
        if last_error is not None:
            raise last_error
        raise OSError(
            f"Could not create listening socket for {self._settings.host}:{self._settings.port}."
        )

    def _notify_status_changed(self) -> None:
        """Bump the status version and wake polling/waiting helpers."""
        self._status_version += 1
        self._status_changed.set()
