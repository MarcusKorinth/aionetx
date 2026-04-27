"""
Shared asyncio event dispatcher for transport lifecycle/event delivery.

The dispatcher provides per-component event handling with configurable inline
or background execution and explicit backpressure/failure policies.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import cast

from aionetx.api.event_delivery_settings import (
    EventBackpressurePolicy,
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
)
from aionetx.api.handler_failure_policy_stop_event import HandlerFailurePolicyStopEvent
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api.network_event import NetworkEvent
from aionetx.api.network_event_handler_protocol import NetworkEventHandlerProtocol
from aionetx.implementations.asyncio_impl.runtime_utils import (
    WarningRateLimiter,
    validate_async_event_handler,
)

_stop_drop_warning_limiter = WarningRateLimiter(interval_seconds=5.0)
_backpressure_drop_warning_limiter = WarningRateLimiter(interval_seconds=5.0)
_inline_dispatcher_context: contextvars.ContextVar[frozenset[int]] = contextvars.ContextVar(
    "aionetx_inline_dispatcher_context", default=frozenset()
)


class _HandlerCancelledError(RuntimeError):
    """Exception wrapper used when a handler raises CancelledError itself."""


@dataclass(frozen=True, slots=True)
class DispatcherRuntimeStats:
    """
    Point-in-time operational counters for one dispatcher instance.

    Attributes:
        emit_calls_total: Total number of ``emit()`` calls observed.
        enqueued_total: Events accepted into the background queue.
        handler_dispatch_attempts_total: Attempts to invoke the event handler.
        handler_failures_total: Handler invocations that raised.
        inline_fallback_total: Background-mode emits delivered inline because no worker existed yet.
        dropped_backpressure_oldest_total: Events evicted by ``DROP_OLDEST`` backpressure.
        dropped_backpressure_newest_total: Events rejected by ``DROP_NEWEST`` backpressure.
        dropped_stop_phase_total: Background-mode emits ignored after shutdown started.
        queue_depth: Current queued event count.
        queue_peak: Highest queue depth observed so far.
    """

    emit_calls_total: int
    enqueued_total: int
    handler_dispatch_attempts_total: int
    handler_failures_total: int
    inline_fallback_total: int
    dropped_backpressure_oldest_total: int
    dropped_backpressure_newest_total: int
    dropped_stop_phase_total: int
    queue_depth: int
    queue_peak: int


@dataclass(frozen=True, slots=True)
class DispatcherStopPolicy:
    """
    Callback policy used by ``STOP_COMPONENT`` handler-failure behavior.

    Attributes:
        enabled: Whether handler failures may request component shutdown.
        callback: Awaitable shutdown callback invoked when the policy is active.
    """

    enabled: bool
    callback: Callable[[], Awaitable[None]] | None = None

    @classmethod
    def disabled(cls) -> DispatcherStopPolicy:
        """Build a policy that never requests component shutdown."""
        return cls(enabled=False, callback=None)

    @classmethod
    def stop_component(cls, callback: Callable[[], Awaitable[None]]) -> DispatcherStopPolicy:
        """
        Build a policy that stops the owning component on handler failure.

        Args:
            callback: Awaitable shutdown callback owned by the component.

        Returns:
            DispatcherStopPolicy: Enabled stop policy bound to ``callback``.
        """
        return cls(enabled=True, callback=callback)

    def validate(self) -> None:
        """
        Validate the internal consistency of the configured stop policy.

        Raises:
            ValueError: If ``enabled`` and ``callback`` disagree about whether
                shutdown is supported.
        """
        if self.enabled and self.callback is None:
            raise ValueError("DispatcherStopPolicy with enabled=True requires a callback.")
        if not self.enabled and self.callback is not None:
            raise ValueError("DispatcherStopPolicy with enabled=False must not provide a callback.")


class AsyncioEventDispatcher:
    """
    Async event dispatcher with optional background delivery.

    BACKGROUND-mode contract:
    - Before ``start()`` creates the worker task, ``emit()`` falls back to
      inline delivery in the caller task.
    - After ``start()``, ``emit()`` enqueues to the worker task.
    - Once ``stop()`` begins, new BACKGROUND-mode emits are intentionally
      ignored so blocked producers can exit promptly and shutdown can complete.

    Flow overview:
        emit()
          |
          +-- INLINE ------------------------------> _emit_now()
          |
          +-- BACKGROUND, worker missing ----------> inline fallback
          |
          +-- BACKGROUND, worker running ----------> _enqueue() -> _run() -> _emit_now()
          |
          +-- BACKGROUND, stop in progress --------> drop and log rate-limited warning
    """

    def __init__(
        self,
        event_handler: NetworkEventHandlerProtocol,
        delivery: EventDeliverySettings,
        logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
        *,
        error_source: str | None = None,
        stop_policy: DispatcherStopPolicy | None = None,
        stop_component_callback: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        validate_async_event_handler(event_handler)
        if stop_policy is not None and stop_component_callback is not None:
            raise ValueError("Provide either stop_policy or stop_component_callback, not both.")
        if stop_policy is None:
            if stop_component_callback is None:
                stop_policy = DispatcherStopPolicy.disabled()
            else:
                stop_policy = DispatcherStopPolicy.stop_component(stop_component_callback)
        stop_policy.validate()
        self._event_handler = event_handler
        self._delivery = delivery
        self._logger = logger
        self._error_source = error_source
        self._stop_policy_enabled = stop_policy.enabled
        self._stop_component_callback = stop_policy.callback
        self._worker_task: asyncio.Task[None] | None = None
        self._queue: deque[NetworkEvent] = deque()
        self._queue_not_empty = asyncio.Condition()
        self._queue_not_full = asyncio.Condition()
        self._stopping = False
        self._emit_calls_total = 0
        self._enqueued_total = 0
        self._handler_dispatch_attempts_total = 0
        self._handler_failures_total = 0
        self._inline_fallback_total = 0
        self._dropped_backpressure_oldest_total = 0
        self._dropped_backpressure_newest_total = 0
        self._dropped_stop_phase_total = 0
        self._queue_peak = 0

    def _has_background_worker(self) -> bool:
        return self._worker_task is not None

    def _accepting_background_events(self) -> bool:
        return not self._stopping

    @property
    def stop_policy(self) -> DispatcherStopPolicy:
        """Explicit stop policy configured for handler-failure shutdown."""
        callback = self._stop_component_callback
        enabled = self._stop_policy_enabled or callback is not None
        return DispatcherStopPolicy(enabled=enabled, callback=callback)

    @property
    def is_running(self) -> bool:
        """
        Whether the background worker task currently exists.

        Note: this does not imply the dispatcher is still accepting new events;
        once shutdown begins (``_stopping=True``), BACKGROUND emits are ignored
        even if the worker has not exited yet.
        """
        return self._has_background_worker()

    @property
    def is_stopping(self) -> bool:
        """
        Whether dispatcher shutdown has been initiated.

        Set to ``True`` at the start of ``stop()``.  Once ``True``, BACKGROUND
        emits are ignored and blocked producers are released.  The background
        worker may still be draining its queue when this flag becomes ``True``.
        """
        return self._stopping

    @property
    def runtime_stats(self) -> DispatcherRuntimeStats:
        """Point-in-time dispatcher operational diagnostics snapshot."""
        return DispatcherRuntimeStats(
            emit_calls_total=self._emit_calls_total,
            enqueued_total=self._enqueued_total,
            handler_dispatch_attempts_total=self._handler_dispatch_attempts_total,
            handler_failures_total=self._handler_failures_total,
            inline_fallback_total=self._inline_fallback_total,
            dropped_backpressure_oldest_total=self._dropped_backpressure_oldest_total,
            dropped_backpressure_newest_total=self._dropped_backpressure_newest_total,
            dropped_stop_phase_total=self._dropped_stop_phase_total,
            queue_depth=len(self._queue),
            queue_peak=self._queue_peak,
        )

    async def start(self) -> None:
        """Start background dispatch worker when configured for BACKGROUND mode."""
        if self._delivery.dispatch_mode != EventDispatchMode.BACKGROUND:
            return
        if self._has_background_worker():
            return
        self._stopping = False
        self._worker_task = asyncio.create_task(self._run(), name="event-dispatcher")

    async def stop(self) -> None:
        """
        Stop background dispatch and release blocked producers.

        Queued events are drained by the worker before it exits. Emits arriving
        after stop has started (or completed) are ignored in BACKGROUND mode.
        """
        self._stopping = True
        worker_task = self._worker_task
        if worker_task is None:
            return
        current_task = asyncio.current_task()
        async with self._queue_not_empty:
            self._queue_not_empty.notify_all()
        async with self._queue_not_full:
            self._queue_not_full.notify_all()
        # In background mode, handlers run on the worker task. If a handler
        # triggers component shutdown, stop() may execute on that same task, so
        # it must not await the worker it is currently unwinding.
        if current_task is worker_task:
            self._dropped_stop_phase_total += len(self._queue)
            self._queue.clear()
            return
        _ = await cast(Awaitable[object], worker_task)

    async def emit(self, event: NetworkEvent) -> None:
        """
        Emit an event according to configured dispatch mode.

        BACKGROUND-mode edge semantics are intentional and part of the
        dispatcher contract:
        - before worker start: deliver inline in the caller task;
        - steady-state (worker running): enqueue for worker delivery;
        - after stop begins: ignore new emits.
        - ``emit()`` in BACKGROUND does not await handler completion; it may
          return while handler work is still in flight on the worker task.

        Arguments:
            event (NetworkEvent): Event instance to deliver according to the
                configured dispatch mode.
        """
        self._emit_calls_total += 1
        if self._delivery.dispatch_mode == EventDispatchMode.INLINE:
            await self._emit_now(event)
            return
        if self._is_in_inline_dispatch_context():
            self._inline_fallback_total += 1
            await self._emit_now(event)
            return
        if not self._accepting_background_events():
            self._record_stop_phase_drop(event)
            return
        if self.current_task_is_worker():
            self._inline_fallback_total += 1
            await self._emit_now(event)
            return
        if not self._has_background_worker():
            self._inline_fallback_total += 1
            await self._emit_now(event)
            return
        await self._enqueue(event)

    async def _enqueue(self, event: NetworkEvent) -> None:
        """
        Queue one event for background delivery under backpressure rules.

        The method keeps shutdown predictable: once stop begins, blocked
        producers are released and no additional background work is accepted.
        """
        while True:
            async with self._queue_not_full:
                if not self._accepting_background_events():
                    self._record_stop_phase_drop(event)
                    return
                if len(self._queue) < self._delivery.max_pending_events:
                    self._queue.append(event)
                    self._enqueued_total += 1
                    self._queue_peak = max(self._queue_peak, len(self._queue))
                    break
                policy = self._delivery.backpressure_policy
                if policy == EventBackpressurePolicy.BLOCK:
                    await self._queue_not_full.wait()
                    continue
                if policy == EventBackpressurePolicy.DROP_OLDEST:
                    self._queue.popleft()
                    self._queue.append(event)
                    self._dropped_backpressure_oldest_total += 1
                    self._enqueued_total += 1
                    self._queue_peak = max(self._queue_peak, len(self._queue))
                    self._warn_backpressure_drop(policy=policy)
                    break
                self._dropped_backpressure_newest_total += 1
                self._warn_backpressure_drop(policy=policy)
                return

        async with self._queue_not_empty:
            self._queue_not_empty.notify()

    def _record_stop_phase_drop(self, event: NetworkEvent) -> None:
        """Track and rate-limit warnings for emits ignored during shutdown."""
        self._dropped_stop_phase_total += 1
        limiter_key = f"{self._error_source or 'event-dispatcher'}:stop-phase-drop"
        if _stop_drop_warning_limiter.should_log(limiter_key):
            self._logger.warning(
                "Dropping %s because dispatcher stop is in progress.",
                type(event).__name__,
            )

    def current_task_is_worker(self) -> bool:
        """
        Return whether the caller currently runs on the dispatcher worker task.

        This is an internal runtime hook for components that need self-await-safe
        shutdown behavior without reaching into dispatcher private state.
        """
        current_task = asyncio.current_task()
        return current_task is not None and current_task is self._worker_task

    def _is_in_inline_dispatch_context(self) -> bool:
        """
        Return whether this dispatcher is already invoking user handler code.

        Background handlers may initiate shutdown paths that create helper tasks
        before publishing terminal events. Context variables are inherited by
        those tasks, so this lets terminal internal events avoid re-entering the
        same background queue that the initiating handler is currently blocking.
        """
        return id(self) in _inline_dispatcher_context.get()

    @contextmanager
    def inline_delivery_context(self) -> Iterator[None]:
        """
        Temporarily deliver this dispatcher's background emits inline.

        This is an internal escape hatch for handler-originated shutdown paths
        that create helper tasks before publishing terminal events. It must not
        be enabled around arbitrary user-handler execution, because that would
        leak into user-created tasks through context variable inheritance and
        break normal BACKGROUND queue semantics.
        """
        active_dispatchers = _inline_dispatcher_context.get()
        reset_token = _inline_dispatcher_context.set(active_dispatchers | {id(self)})
        try:
            yield
        finally:
            _inline_dispatcher_context.reset(reset_token)

    def _warn_backpressure_drop(self, *, policy: EventBackpressurePolicy) -> None:
        """Emit a rate-limited warning for overload drops."""
        limiter_key = f"{self._error_source or 'event-dispatcher'}:{policy.value}"
        if not _backpressure_drop_warning_limiter.should_log(limiter_key):
            return
        drop_count = (
            self._dropped_backpressure_oldest_total
            if policy == EventBackpressurePolicy.DROP_OLDEST
            else self._dropped_backpressure_newest_total
        )
        self._logger.warning(
            "Dropped events due to backpressure (%s, total=%s).",
            policy.value,
            drop_count,
        )

    async def _run(self) -> None:
        """Drain the background queue until shutdown begins and queued work is exhausted."""
        current_task = asyncio.current_task()
        try:
            while True:
                event: NetworkEvent | None = None
                async with self._queue_not_empty:
                    while not self._queue and not self._stopping:
                        await self._queue_not_empty.wait()
                    if self._stopping and not self._queue:
                        return
                    if self._queue:
                        event = self._queue.popleft()
                async with self._queue_not_full:
                    self._queue_not_full.notify()
                if event is not None:
                    await self._emit_now(event)
        finally:
            if self._worker_task is current_task:
                self._worker_task = None

    async def _emit_now(self, event: NetworkEvent) -> None:
        """Deliver one event to the handler and route failures through policy handling."""
        self._handler_dispatch_attempts_total += 1
        try:
            await self._event_handler.on_event(event)
        except asyncio.CancelledError as error:
            current_task = asyncio.current_task()
            task_cancelling = getattr(current_task, "cancelling", None)
            if task_cancelling is not None and task_cancelling():
                raise
            wrapped_error = _HandlerCancelledError(
                "Network event handler raised asyncio.CancelledError without "
                "cancelling the dispatcher task."
            )
            wrapped_error.__cause__ = error
            await self._record_handler_failure(error=wrapped_error, triggering_event=event)
        except Exception as error:
            await self._record_handler_failure(error=error, triggering_event=event)

    async def _record_handler_failure(
        self, *, error: Exception, triggering_event: NetworkEvent
    ) -> None:
        """Record and route one user handler failure."""
        self._handler_failures_total += 1
        self._logger.exception(
            "Network event handler raised while processing %s.", type(triggering_event).__name__
        )
        await self._handle_handler_failure(error=error, triggering_event=triggering_event)

    async def _handle_handler_failure(
        self, *, error: Exception, triggering_event: NetworkEvent
    ) -> None:
        """
        Apply the configured failure policy after a handler exception.

        Args:
            error: Exception raised by the handler.
            triggering_event: Event whose delivery triggered the failure.

        Raises:
            Exception: Re-raises ``error`` when inline mode uses
                ``RAISE_IN_INLINE_MODE``.
        """
        policy = self._delivery.handler_failure_policy
        if policy == EventHandlerFailurePolicy.LOG_ONLY:
            return
        if policy == EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE:
            if self._delivery.dispatch_mode == EventDispatchMode.INLINE:
                raise error
            return
        if policy == EventHandlerFailurePolicy.EMIT_ERROR_EVENT:
            if isinstance(triggering_event, NetworkErrorEvent):
                return
            source = self._error_source or "event-dispatcher"
            await self._emit_handler_failure_control_event(
                NetworkErrorEvent(resource_id=source, error=error)
            )
            return
        if policy == EventHandlerFailurePolicy.STOP_COMPONENT:
            stop_policy = self.stop_policy
            stop_callback = stop_policy.callback
            if not stop_policy.enabled or stop_callback is None:
                self._logger.warning(
                    "handler_failure_policy=stop_component is configured but dispatcher stop_policy does not provide a callback."
                )
                return
            if isinstance(triggering_event, HandlerFailurePolicyStopEvent):
                return
            source = self._error_source or "event-dispatcher"
            await self._emit_handler_failure_control_event(
                HandlerFailurePolicyStopEvent(
                    resource_id=source,
                    triggering_event_name=type(triggering_event).__name__,
                    error=error,
                    policy=policy,
                    dispatch_mode=self._delivery.dispatch_mode,
                )
            )
            with self.inline_delivery_context():
                await stop_callback()
            return
        raise RuntimeError(f"Unhandled EventHandlerFailurePolicy: {policy!r}")

    async def _emit_handler_failure_control_event(self, event: NetworkEvent) -> None:
        """
        Deliver a failure-path control event without deadlocking the worker queue.

        When the background worker itself is handling a failing event, re-entering
        ``emit()`` under ``BLOCK`` backpressure can wait forever on queue space
        that only the same worker could free. Deliver control events inline on
        the worker to keep failure handling progress-safe.
        """
        if (
            self._delivery.dispatch_mode == EventDispatchMode.BACKGROUND
            and self.current_task_is_worker()
        ):
            await self._emit_now(event)
            return
        await self.emit(event)
