"""Asyncio TCP connection primitive with explicit close coordination."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable, cast

from aionetx.api._validation import require_optional_positive_finite_number
from aionetx.api.bytes_like import BytesLike
from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.errors import ConnectionClosedError
from aionetx.api.connection_events import ConnectionClosedEvent, ConnectionOpenedEvent
from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.connection_protocol import ConnectionProtocol
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.implementations.asyncio_impl._tcp_connection_helpers import (
    await_read_task_shutdown,
    await_writer_shutdown,
    build_connection_metadata,
)
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher
from aionetx.implementations.asyncio_impl.runtime_utils import WarningRateLimiter

ConnectionClosedCallback = Callable[["AsyncioTcpConnection"], Awaitable[None] | None]
ConnectionReadyCallback = Callable[["AsyncioTcpConnection"], Awaitable[None] | None]

logger = logging.getLogger(__name__)
_warning_limiter = WarningRateLimiter(interval_seconds=30.0)
_SHUTDOWN_AWAIT_TIMEOUT_SECONDS = 1.0


class AsyncioTcpConnection(ConnectionProtocol):
    """
    Concrete TCP connection used by client and server transports.

    The class owns socket read/write loops and emits connection lifecycle/data
    events through :class:`AsyncioEventDispatcher`.
    """

    def __init__(
        self,
        connection_id: str,
        role: ConnectionRole,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        event_dispatcher: AsyncioEventDispatcher,
        receive_buffer_size: int,
        idle_timeout_seconds: float | None = None,
        on_closed_callback: ConnectionClosedCallback | None = None,
        *,
        send_timeout_seconds: float | None = 30.0,
        on_ready_callback: ConnectionReadyCallback | None = None,
    ) -> None:
        if not connection_id:
            raise ValueError("connection_id must not be empty.")
        if receive_buffer_size <= 0:
            raise ValueError("receive_buffer_size must be > 0.")
        if idle_timeout_seconds is not None and idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be > 0 when provided.")
        send_timeout_seconds = require_optional_positive_finite_number(
            field_name="send_timeout_seconds",
            value=send_timeout_seconds,
            error_type=ValueError,
        )
        self._connection_id = connection_id
        self._role = role
        self._reader = reader
        self._writer = writer
        self._event_dispatcher = event_dispatcher
        self._receive_buffer_size = receive_buffer_size
        self._idle_timeout_seconds = idle_timeout_seconds
        self._send_timeout_seconds = send_timeout_seconds
        self._on_closed_callback = on_closed_callback
        self._on_ready_callback = on_ready_callback
        self._state = ConnectionState.CREATED
        self._metadata = self._build_metadata()
        self._read_task: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()
        self._logger = logging.LoggerAdapter(
            logger, {"connection_id": connection_id, "role": role.value}
        )
        self._closed_event_published = False
        self._close_event_previous_state: ConnectionState | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._close_event_task: asyncio.Task[None] | None = None
        self._opening_event_task: asyncio.Task[object] | None = None
        self._close_event_deferred_until_opened_event_completes = False
        self._deferred_close_event_waiter: asyncio.Future[None] | None = None
        self._deferred_close_publish_task: asyncio.Task[None] | None = None

    @property
    def connection_id(self) -> str:
        """Stable identifier used for lifecycle, data, and error events."""
        return self._connection_id

    @property
    def role(self) -> ConnectionRole:
        """Whether this connection belongs to a client or server transport."""
        return self._role

    @property
    def state(self) -> ConnectionState:
        """Current transport-facing lifecycle state of the connection."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Whether the connection is currently able to send and receive payloads."""
        return self._state == ConnectionState.CONNECTED

    @property
    def metadata(self) -> ConnectionMetadata:
        """Static connection metadata derived from the underlying stream writer."""
        return self._metadata

    async def start(self) -> None:
        """
        Transition to connected state and start read-loop publication.

        Raises:
            RuntimeError: If called from an invalid non-startable state.
        """
        if self._state == ConnectionState.CONNECTED:
            return
        if self._state not in (ConnectionState.CREATED, ConnectionState.CONNECTING):
            raise RuntimeError(
                f"Connection '{self._connection_id}' cannot be started from state '{self._state.value}'."
            )
        self._state = ConnectionState.CONNECTING
        self._state = ConnectionState.CONNECTED
        try:
            await self._notify_ready_callback()
        except (Exception, asyncio.CancelledError):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self.close()
            raise
        if self._state != ConnectionState.CONNECTED:
            return
        opening_task = asyncio.current_task()
        self._opening_event_task = cast(asyncio.Task[object] | None, opening_task)
        try:
            await self._event_dispatcher.emit_and_wait(
                ConnectionOpenedEvent(
                    resource_id=self._metadata.connection_id, metadata=self._metadata
                ),
                drop_on_backpressure=False,
            )
        except (Exception, asyncio.CancelledError):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._publish_deferred_close_after_opened_event_preserving_cancellation()
            if self._opening_event_task is opening_task:
                self._opening_event_task = None
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self.close()
            raise
        if self._opening_event_task is opening_task:
            self._opening_event_task = None
        try:
            if await self._publish_deferred_close_after_opened_event_preserving_cancellation():
                return
        except (Exception, asyncio.CancelledError):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self.close()
            raise
        if self._state == ConnectionState.CONNECTED:
            self._read_task = asyncio.create_task(
                self._read_loop(), name=f"{self._connection_id}-read-loop"
            )

    async def send(self, data: BytesLike) -> None:
        """
        Write bytes-like payload to the socket stream.

        Arguments:
            data (BytesLike): Raw payload to write to the peer.

        Raises:
            ConnectionClosedError: If the connection is not in CONNECTED state.
            TypeError: If ``data`` is not bytes-like.
            OSError: If the underlying stream reports a socket failure while
                flushing the write buffer.
            asyncio.TimeoutError: If the write buffer does not drain within
                the configured send timeout.
        """
        if self._state != ConnectionState.CONNECTED:
            raise ConnectionClosedError(f"Connection '{self._connection_id}' is not connected.")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("send expects bytes-like data.")
        self._writer.write(bytes(data))
        if self._send_timeout_seconds is None:
            await self._writer.drain()
            return
        await asyncio.wait_for(self._writer.drain(), timeout=self._send_timeout_seconds)

    async def close(self) -> None:
        """
        Close the connection with shared full-lifecycle coordination.

        Close flow:
            close()
              -> mark ``CLOSING`` and detach read task
              -> cancel read loop / close writer
              -> run ``on_closed_callback`` once
              -> transition to ``CLOSED``
              -> emit ``ConnectionClosedEvent`` once

        Concurrency contract:
        - Only one full close operation runs at a time.
        - Concurrent callers await the same in-flight close task and therefore
          observe the same terminal success/failure outcome.
        - Teardown, callback, and close-event publication are each executed once.

        Cancellation contract:
        - Caller cancellation is shielded from the shared close task.
        - If caller cancellation happens, close waits for shared finalization and
          then re-raises ``CancelledError``.
        - If caller cancellation and a real close failure race, the real close
          failure (task exception) takes precedence for all waiters; cancellation
          is re-raised only when the shared close task completes successfully.
        - Publication failures still propagate and leave reconciliation coherent.
        """
        close_task: asyncio.Task[None]
        async with self._close_lock:
            current_task = asyncio.current_task()
            if (
                self._state == ConnectionState.CLOSED
                and self._close_event_deferred_until_opened_event_completes
                and self._opening_event_task is current_task
            ):
                return
            if self._state == ConnectionState.CLOSED and self._closed_event_published:
                return
            if self._close_task is None or self._close_task.done():
                needs_teardown = False
                read_task: asyncio.Task[None] | None = None
                if self._state not in (ConnectionState.CLOSING, ConnectionState.CLOSED):
                    needs_teardown = True
                    read_task = self._read_task
                    self._read_task = None
                if self._state != ConnectionState.CLOSED:
                    # preserve first pre-close state across retries/reconciliation
                    self._close_event_previous_state = (
                        self._close_event_previous_state or self._state
                    )
                    self._state = ConnectionState.CLOSING
                callback = self._on_closed_callback
                close_caller_task = current_task
                close_caller_uses_inline_delivery = (
                    self._event_dispatcher.current_task_is_worker()
                    or self._event_dispatcher.current_task_is_dispatching_handler()
                    or self._event_dispatcher.current_task_has_handler_origin_context()
                    or self._event_dispatcher.current_task_has_inline_delivery_context()
                )
                inline_delivery_context = (
                    self._event_dispatcher.inline_delivery_context()
                    if close_caller_uses_inline_delivery
                    else contextlib.nullcontext()
                )
                with inline_delivery_context:
                    self._close_task = asyncio.create_task(
                        self._run_close_lifecycle(
                            needs_teardown=needs_teardown,
                            read_task=read_task,
                            callback=callback,
                            close_caller_task=close_caller_task,
                            close_caller_uses_inline_delivery=close_caller_uses_inline_delivery,
                        )
                    )
            close_task = self._close_task

        if close_task is asyncio.current_task():
            return
        if self._close_event_task is asyncio.current_task():
            return
        if self._close_event_deferred_until_opened_event_completes and (
            self._event_dispatcher.current_task_is_dispatching_handler(self._connection_id)
            or self._event_dispatcher.current_task_inherits_handler_origin_context(
                self._connection_id
            )
        ):
            return

        handler_origin_caller = self._event_dispatcher.current_task_is_dispatching_handler(
            self._connection_id
        ) or self._event_dispatcher.current_task_inherits_handler_origin_context(
            self._connection_id
        )
        cancellation_requested = False
        try:
            await asyncio.shield(close_task)
            deferred_waiter = self._pending_deferred_close_waiter()
            if (
                deferred_waiter is not None
                and not handler_origin_caller
                and self._event_dispatcher.has_active_handler_context(self._connection_id)
            ):
                await asyncio.shield(deferred_waiter)
        except asyncio.CancelledError:
            if close_task.done() and close_task.cancelled():
                raise
            cancellation_requested = True
            _ = await cast(Awaitable[object], close_task)
        if cancellation_requested:
            raise asyncio.CancelledError

    async def _run_close_lifecycle(
        self,
        *,
        needs_teardown: bool,
        read_task: asyncio.Task[None] | None,
        callback: ConnectionClosedCallback | None,
        close_caller_task: asyncio.Task[None] | None,
        close_caller_uses_inline_delivery: bool,
    ) -> None:
        """
        Execute the shared teardown portion of ``close()`` exactly once.

        Args:
            needs_teardown: Whether this caller owns the socket teardown path.
            read_task: Detached read-loop task to cancel and await.
            callback: Optional close callback invoked before terminal publication.
            close_caller_task: Task that created the shared close lifecycle task.
            close_caller_uses_inline_delivery: Whether close() was initiated by a handler
                path that must not queue terminal events behind the current handler.
        """
        cancellation_requested = False
        try:
            if needs_teardown:
                try:
                    current_task = asyncio.current_task()
                    preserve_active_inline_read_task = (
                        self._event_dispatcher.has_active_handler_context(self._connection_id)
                        and self._event_dispatcher.current_task_would_deliver_inline()
                    )
                    if self._event_dispatcher.has_active_handler_context(self._connection_id):
                        await self._event_dispatcher.drop_queued_events_for_resource(
                            self._connection_id
                        )
                    if (
                        read_task is not None
                        and read_task is not current_task
                        and read_task is not close_caller_task
                        and not preserve_active_inline_read_task
                    ):
                        read_task.cancel()
                        await self._await_read_task_shutdown(read_task)

                    self._writer.close()
                    await self._await_writer_shutdown()

                    if callback is not None:
                        try:
                            result = callback(self)
                            if asyncio.iscoroutine(result):
                                _ = await result
                        except Exception as error:
                            if _warning_limiter.should_log(
                                f"tcp-connection-close-callback:{self._connection_id}"
                            ):
                                self._logger.warning("Connection close callback raised: %s", error)
                            await self._safe_emit_error(error)
                except asyncio.CancelledError:
                    cancellation_requested = True
            deferred, deferred_waiter = await self._defer_close_event_until_opened_event_completes(
                close_caller_task,
                close_caller_uses_inline_delivery=close_caller_uses_inline_delivery,
            )
            if deferred:
                await self._event_dispatcher.drop_queued_events_for_resource(self._connection_id)
            if deferred_waiter is not None:
                await asyncio.shield(deferred_waiter)
            if not deferred:
                await self._event_dispatcher.drop_queued_events_for_resource(self._connection_id)
                await self._finalize_close()
        finally:
            async with self._close_lock:
                current_task = asyncio.current_task()
                if self._close_task is current_task:
                    self._close_task = None
            if cancellation_requested:
                raise asyncio.CancelledError

    async def _await_read_task_shutdown(self, read_task: asyncio.Task[None]) -> None:
        """Await read-loop cancellation with bounded shutdown warnings."""
        await await_read_task_shutdown(
            connection_id=self._connection_id,
            read_task=read_task,
            logger=self._logger,
            warning_limiter=_warning_limiter,
            timeout_seconds=_SHUTDOWN_AWAIT_TIMEOUT_SECONDS,
        )

    async def _await_writer_shutdown(self) -> None:
        """Close the stream writer and await drain/transport shutdown safely."""
        await await_writer_shutdown(
            connection_id=self._connection_id,
            writer=self._writer,
            logger=self._logger,
            warning_limiter=_warning_limiter,
            timeout_seconds=_SHUTDOWN_AWAIT_TIMEOUT_SECONDS,
        )

    async def _notify_ready_callback(self) -> None:
        """Run the internal post-connect hook before ConnectionOpenedEvent publication."""
        if self._state != ConnectionState.CONNECTED or self._on_ready_callback is None:
            return
        result = self._on_ready_callback(self)
        if asyncio.iscoroutine(result):
            _ = await result

    def _pending_deferred_close_waiter(self) -> asyncio.Future[None] | None:
        """Return the in-flight deferred close publication waiter, if any."""
        waiter = self._deferred_close_event_waiter
        if waiter is None or waiter.done():
            return None
        return waiter

    async def _finalize_close(self) -> None:
        """
        Publish terminal close state/event with shared in-flight finalization.

        Publication semantics:
        - Only one close-event publication task is created at a time.
        - Concurrent close() callers await that same task so they observe the
          same publication success/failure outcome.
        - ``_closed_event_published`` is set only after successful emission.

        Cancellation semantics:
        - Caller cancellation is isolated with ``asyncio.shield`` so the shared
          publication task continues to completion.
        - If caller cancellation happens, this method waits for publication and
          then re-raises cancellation.
        - If the publication task itself fails/cancels, that failure is surfaced
          to callers and publication is not marked successful.
        """
        publication_task: asyncio.Task[None]
        async with self._close_lock:
            if self._closed_event_published:
                return
            if self._close_event_task is None or self._close_event_task.done():
                previous_state = self._close_event_previous_state or ConnectionState.CLOSING
                self._state = ConnectionState.CLOSED
                self._close_event_task = asyncio.create_task(
                    self._run_close_event_publication(previous_state)
                )
            publication_task = self._close_event_task

        # Re-entrant close() from within close-event handlers can execute on the
        # same task that is currently publishing ``ConnectionClosedEvent``.
        # That caller must not await itself.
        if publication_task is asyncio.current_task():
            return

        cancelled_during_emit = False
        try:
            await asyncio.shield(publication_task)
        except asyncio.CancelledError:
            # Distinguish caller cancellation from publication task cancellation:
            # if the shared task was cancelled, propagate that directly.
            if publication_task.done() and publication_task.cancelled():
                raise
            cancelled_during_emit = True
            _ = await cast(Awaitable[object], publication_task)
        if cancelled_during_emit:
            raise asyncio.CancelledError

    async def _defer_close_event_until_opened_event_completes(
        self,
        close_caller_task: asyncio.Task[None] | None,
        *,
        close_caller_uses_inline_delivery: bool,
    ) -> tuple[bool, asyncio.Future[None] | None]:
        """Defer close publication while a connection handler is in flight."""
        async with self._close_lock:
            opening_event_task = self._opening_event_task
            inherited_handler_origin = (
                self._event_dispatcher.current_task_inherits_handler_origin_context(
                    self._connection_id
                )
            )
            handler_origin_in_flight = (
                close_caller_task is opening_event_task
                or self._event_dispatcher.current_task_is_dispatching_handler(self._connection_id)
                or self._event_dispatcher.current_task_has_handler_origin_context(
                    self._connection_id
                )
            )
            active_inline_handler_in_flight = (
                self._event_dispatcher.has_active_handler_context(self._connection_id)
                and self._event_dispatcher.current_task_would_deliver_inline()
            )
            if (
                opening_event_task is None
                and not handler_origin_in_flight
                and not close_caller_uses_inline_delivery
                and not inherited_handler_origin
                and not active_inline_handler_in_flight
                and not self._close_event_deferred_until_opened_event_completes
            ):
                return False, None
            self._state = ConnectionState.CLOSED
            self._close_event_deferred_until_opened_event_completes = True
            if (
                self._deferred_close_event_waiter is None
                or self._deferred_close_event_waiter.done()
            ):
                current_task = asyncio.current_task()
                loop = (
                    current_task.get_loop()
                    if current_task is not None
                    else asyncio.get_running_loop()
                )
                self._deferred_close_event_waiter = loop.create_future()
            if opening_event_task is None and (
                handler_origin_in_flight
                or close_caller_uses_inline_delivery
                or inherited_handler_origin
                or active_inline_handler_in_flight
            ):
                publish_task = self._deferred_close_publish_task
                if publish_task is None or publish_task.done():
                    self._deferred_close_publish_task = asyncio.create_task(
                        self._publish_deferred_close_after_handler_context_expires(),
                        name=f"{self._connection_id}-deferred-close-publisher",
                    )
            # Handler-originated close paths cannot await this waiter: the
            # active handler must return before the deferred close event may run.
            # External close callers do get the waiter so their close() call
            # still observes terminal publication.
            if (
                handler_origin_in_flight
                or close_caller_uses_inline_delivery
                or inherited_handler_origin
                or self._event_dispatcher.current_task_has_handler_origin_context(
                    self._connection_id
                )
            ):
                return True, None
            return True, self._deferred_close_event_waiter

    async def _ensure_deferred_close_publication_waiter(self) -> asyncio.Future[None] | None:
        """Ensure unpublished terminal close has a deferred publication waiter."""
        async with self._close_lock:
            if self._closed_event_published:
                return None
            self._close_event_deferred_until_opened_event_completes = True
            if (
                self._deferred_close_event_waiter is None
                or self._deferred_close_event_waiter.done()
            ):
                current_task = asyncio.current_task()
                loop = (
                    current_task.get_loop()
                    if current_task is not None
                    else asyncio.get_running_loop()
                )
                self._deferred_close_event_waiter = loop.create_future()
            publish_task = self._deferred_close_publish_task
            if publish_task is None or publish_task.done():
                self._deferred_close_publish_task = asyncio.create_task(
                    self._publish_deferred_close_after_handler_context_expires(),
                    name=f"{self._connection_id}-deferred-close-publisher",
                )
            return self._deferred_close_event_waiter

    async def _publish_deferred_close_after_handler_context_expires(self) -> None:
        """Publish a deferred close once active connection handlers have unwound."""
        current_task = asyncio.current_task()
        try:
            while self._event_dispatcher.has_active_handler_context(self._connection_id):
                await asyncio.sleep(0)
            await self._publish_deferred_close_after_opened_event()
        except (Exception, asyncio.CancelledError) as error:
            async with self._close_lock:
                deferred_waiter = self._deferred_close_event_waiter
            if deferred_waiter is not None and not deferred_waiter.done():
                deferred_waiter.set_exception(error)
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    deferred_waiter.exception()
            if not isinstance(error, asyncio.CancelledError):
                self._logger.warning("Deferred close publication failed: %s", error)
        finally:
            async with self._close_lock:
                if self._deferred_close_publish_task is current_task:
                    self._deferred_close_publish_task = None

    async def _publish_deferred_close_after_opened_event_preserving_cancellation(self) -> bool:
        """Publish deferred close even if the opening task is cancelled at the barrier."""
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
        async with self._close_lock:
            if not self._close_event_deferred_until_opened_event_completes:
                return False
            self._close_event_deferred_until_opened_event_completes = False
            deferred_waiter = self._deferred_close_event_waiter
        try:
            with self._event_dispatcher.inline_delivery_context():
                await self._finalize_close()
        except (Exception, asyncio.CancelledError) as error:
            if deferred_waiter is not None and not deferred_waiter.done():
                deferred_waiter.set_exception(error)
            raise
        else:
            if deferred_waiter is not None and not deferred_waiter.done():
                deferred_waiter.set_result(None)
        finally:
            async with self._close_lock:
                if self._deferred_close_event_waiter is deferred_waiter:
                    self._deferred_close_event_waiter = None
        return True

    async def _run_close_event_publication(self, previous_state: ConnectionState) -> None:
        """Emit the terminal close event and mark publication success atomically."""
        closed_event = ConnectionClosedEvent(
            resource_id=self._connection_id,
            previous_state=previous_state,
            metadata=self._metadata,
        )
        published_successfully = False
        try:
            await self._event_dispatcher.emit(closed_event)
            published_successfully = True
        finally:
            async with self._close_lock:
                if published_successfully:
                    self._closed_event_published = True
                    self._close_event_previous_state = None
                current_task = asyncio.current_task()
                if self._close_event_task is current_task:
                    self._close_event_task = None

    async def _read_loop(self) -> None:
        """Read payloads until EOF or failure, then trigger shared close reconciliation."""
        try:
            while self._state == ConnectionState.CONNECTED:
                if self._idle_timeout_seconds is None:
                    data = await self._reader.read(self._receive_buffer_size)
                else:
                    data = await asyncio.wait_for(
                        self._reader.read(self._receive_buffer_size),
                        timeout=self._idle_timeout_seconds,
                    )
                if not data:
                    break
                await self._event_dispatcher.emit(
                    BytesReceivedEvent(resource_id=self._connection_id, data=data)
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if _warning_limiter.should_log(f"tcp-connection-read-loop:{self._connection_id}"):
                self._logger.warning("Connection read loop error: %s", error)
            await self._safe_emit_error(error)
        finally:
            if self._state not in (ConnectionState.CLOSING, ConnectionState.CLOSED):
                await self.close()

    async def _safe_emit_error(self, error: Exception) -> None:
        """Emit a connection-scoped error event without adding extra policy logic here."""
        await self._event_dispatcher.emit(
            NetworkErrorEvent(resource_id=self._connection_id, error=error)
        )

    def _build_metadata(self) -> ConnectionMetadata:
        """Build the immutable metadata snapshot exposed by ``metadata`` and events."""
        return build_connection_metadata(
            connection_id=self._connection_id,
            role=self._role,
            writer=self._writer,
        )
