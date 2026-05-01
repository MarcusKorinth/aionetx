"""
Contract tests for AsyncioEventDispatcher.

These tests pin the observable delivery semantics that higher-level transports
rely on: ordering, backpressure, stop boundaries, failure-policy behavior, and
runtime statistics. They intentionally exercise both inline and background
dispatch because those semantics are part of the library contract, not just an
implementation detail.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from aionetx.api.connection_events import ConnectionClosedEvent
from aionetx.api.connection_events import ConnectionOpenedEvent
from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.event_delivery_settings import (
    EventBackpressurePolicy,
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
)
from aionetx.api.handler_failure_policy_stop_event import HandlerFailurePolicyStopEvent
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api.network_event import NetworkEvent
from aionetx.implementations.asyncio_impl.event_dispatcher import (
    AsyncioEventDispatcher,
    DispatcherStopPolicy,
)
from tests.helpers import assert_awaitable_cancelled, wait_for_condition

pytestmark = pytest.mark.behavior_critical


class Recorder:
    """Minimal async handler that records delivered events for assertions."""

    def __init__(self, delay: float = 0.0, fail: bool = False) -> None:
        self.events: list[NetworkEvent] = []
        self.delay = delay
        self.fail = fail
        self._events_changed = asyncio.Condition()

    async def on_event(self, event: NetworkEvent) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        async with self._events_changed:
            self.events.append(event)
            self._events_changed.notify_all()
        if self.fail:
            raise RuntimeError("boom")

    async def wait_for_event_count(self, expected_count: int, *, timeout: float = 1.0) -> None:
        """Block until at least the requested number of events has been observed."""

        async def _wait_until_count() -> None:
            async with self._events_changed:
                while len(self.events) < expected_count:
                    await self._events_changed.wait()

        await asyncio.wait_for(_wait_until_count(), timeout=timeout)


def _opened(idx: int | str) -> ConnectionOpenedEvent:
    """Create a predictable opened event for ordering and queueing assertions."""

    connection_id = f"c{idx}" if isinstance(idx, int) else idx
    metadata = ConnectionMetadata(connection_id=connection_id, role=ConnectionRole.CLIENT)
    return ConnectionOpenedEvent(resource_id=metadata.connection_id, metadata=metadata)


async def _wait_until_dispatcher_stopping(dispatcher: AsyncioEventDispatcher) -> None:
    """Yield until stop() has entered the dispatcher stopping state."""

    while not dispatcher.is_stopping:
        await asyncio.sleep(0)


# Basic delivery and handler-isolation guarantees.
@pytest.mark.asyncio
async def test_event_ordering_background_mode() -> None:
    handler = Recorder()
    dispatcher = AsyncioEventDispatcher(handler, EventDeliverySettings(), logging.getLogger("test"))
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await dispatcher.emit(_opened(2))
    await handler.wait_for_event_count(2)
    await dispatcher.stop()
    assert [
        e.metadata.connection_id for e in handler.events if isinstance(e, ConnectionOpenedEvent)
    ] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_handler_failure_does_not_break_dispatcher() -> None:
    handler = Recorder(fail=True)
    dispatcher = AsyncioEventDispatcher(handler, EventDeliverySettings(), logging.getLogger("test"))
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await dispatcher.emit(
        ConnectionClosedEvent(resource_id="c1", previous_state=ConnectionState.CONNECTED)
    )
    await handler.wait_for_event_count(2)
    await dispatcher.stop()
    assert len(handler.events) == 2


@pytest.mark.asyncio
async def test_stop_cancels_background_worker_cleanly() -> None:
    handler = Recorder(delay=0.01)
    dispatcher = AsyncioEventDispatcher(handler, EventDeliverySettings(), logging.getLogger("test"))
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await dispatcher.stop()


@pytest.mark.asyncio
async def test_cancelled_stop_does_not_strand_emit_and_wait_waiters() -> None:
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()
    second_started = asyncio.Event()
    allow_second_to_finish = asyncio.Event()
    second_handled = asyncio.Event()

    class BlockingHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            if event.metadata.connection_id == "c1":
                first_started.set()
                await allow_first_to_finish.wait()
            elif event.metadata.connection_id == "c2":
                second_started.set()
                await allow_second_to_finish.wait()
                second_handled.set()

    dispatcher = AsyncioEventDispatcher(
        BlockingHandler(),
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
    )
    await dispatcher.start()
    emit_wait_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None
    try:
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        emit_wait_task = asyncio.create_task(dispatcher.emit_and_wait(_opened(2)))
        await wait_for_condition(
            lambda: dispatcher.runtime_stats.queue_depth == 1,
            timeout_seconds=1.0,
        )

        stop_task = asyncio.create_task(dispatcher.stop())
        await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)
        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop_task, timeout=1.0)

        allow_first_to_finish.set()
        await asyncio.wait_for(second_started.wait(), timeout=1.0)
        assert not emit_wait_task.done()
        allow_second_to_finish.set()
        await asyncio.wait_for(emit_wait_task, timeout=1.0)
        await asyncio.wait_for(second_handled.wait(), timeout=1.0)
    finally:
        allow_first_to_finish.set()
        allow_second_to_finish.set()
        if emit_wait_task is not None and not emit_wait_task.done():
            emit_wait_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(emit_wait_task, timeout=1.0)
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(stop_task, timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(dispatcher.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_stop_called_from_event_callback_is_safe(caplog: pytest.LogCaptureFixture) -> None:
    class StopFromCallbackHandler:
        def __init__(self) -> None:
            self.dispatcher: AsyncioEventDispatcher | None = None
            self.count = 0
            self.handled = asyncio.Event()

        async def on_event(self, event: NetworkEvent) -> None:
            del event
            self.count += 1
            if self.dispatcher is not None:
                await self.dispatcher.stop()
            self.handled.set()

    handler = StopFromCallbackHandler()
    dispatcher = AsyncioEventDispatcher(handler, EventDeliverySettings(), logging.getLogger("test"))
    handler.dispatcher = dispatcher

    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(handler.handled.wait(), timeout=1.0)

    assert handler.count == 1
    assert dispatcher.is_running is False
    assert "Task cannot await on itself" not in caplog.text


@pytest.mark.asyncio
async def test_handler_origin_context_expires_when_handler_returns() -> None:
    handler_finished = asyncio.Event()
    observed_origin_context: list[bool] = []

    class SpawnChildFromHandler:
        def __init__(self) -> None:
            self.dispatcher: AsyncioEventDispatcher | None = None
            self.child_task: asyncio.Task[None] | None = None

        async def on_event(self, event: NetworkEvent) -> None:
            del event
            if self.dispatcher is None:
                raise AssertionError("dispatcher reference was not attached")

            async def observe_after_handler_returns() -> None:
                await handler_finished.wait()
                observed_origin_context.append(
                    self.dispatcher.current_task_has_handler_origin_context()
                )

            self.child_task = asyncio.create_task(observe_after_handler_returns())

    handler = SpawnChildFromHandler()
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
    )
    handler.dispatcher = dispatcher
    await dispatcher.start()
    try:
        await dispatcher.emit_and_wait(_opened("origin"))
        handler_finished.set()
        assert handler.child_task is not None
        await asyncio.wait_for(handler.child_task, timeout=1.0)
    finally:
        handler_finished.set()
        if handler.child_task is not None and not handler.child_task.done():
            handler.child_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(handler.child_task, timeout=1.0)
        await dispatcher.stop()

    assert observed_origin_context == [False]


@pytest.mark.asyncio
async def test_handler_origin_context_is_not_inherited_by_child_tasks() -> None:
    child_ready = asyncio.Event()
    allow_child_to_check = asyncio.Event()
    child_checked = asyncio.Event()
    allow_handler_to_finish = asyncio.Event()
    observed_handler_origin_context: list[bool] = []
    observed_child_origin_context: list[bool] = []
    observed_child_inherited_context: list[bool] = []

    class SpawnChildFromActiveHandler:
        def __init__(self) -> None:
            self.dispatcher: AsyncioEventDispatcher | None = None
            self.child_task: asyncio.Task[None] | None = None

        async def on_event(self, event: NetworkEvent) -> None:
            del event
            if self.dispatcher is None:
                raise AssertionError("dispatcher reference was not attached")
            observed_handler_origin_context.append(
                self.dispatcher.current_task_has_handler_origin_context()
            )

            async def observe_while_handler_is_suspended() -> None:
                child_ready.set()
                await allow_child_to_check.wait()
                observed_child_origin_context.append(
                    self.dispatcher.current_task_has_handler_origin_context()
                )
                observed_child_inherited_context.append(
                    self.dispatcher.current_task_inherits_handler_origin_context()
                )
                child_checked.set()

            self.child_task = asyncio.create_task(observe_while_handler_is_suspended())
            await child_ready.wait()
            await child_checked.wait()
            await allow_handler_to_finish.wait()

    handler = SpawnChildFromActiveHandler()
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
    )
    handler.dispatcher = dispatcher
    await dispatcher.start()
    emit_task: asyncio.Task[None] | None = None
    try:
        emit_task = asyncio.create_task(dispatcher.emit_and_wait(_opened("origin")))
        await asyncio.wait_for(child_ready.wait(), timeout=1.0)
        allow_child_to_check.set()
        await asyncio.wait_for(child_checked.wait(), timeout=1.0)
        assert observed_handler_origin_context == [True]
        assert observed_child_origin_context == [False]
        assert observed_child_inherited_context == [True]

        allow_handler_to_finish.set()
        await asyncio.wait_for(emit_task, timeout=1.0)
        assert handler.child_task is not None
        await asyncio.wait_for(handler.child_task, timeout=1.0)
    finally:
        allow_child_to_check.set()
        allow_handler_to_finish.set()
        if emit_task is not None and not emit_task.done():
            emit_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(emit_task, timeout=1.0)
        if handler.child_task is not None and not handler.child_task.done():
            handler.child_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(handler.child_task, timeout=1.0)
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_inline_delivery_context_expires_for_tasks_spawned_inside_context() -> None:
    context_exited = asyncio.Event()
    observed_inline_context: list[bool] = []

    dispatcher = AsyncioEventDispatcher(
        Recorder(),
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
    )

    async def observe_after_context_exits() -> None:
        await context_exited.wait()
        observed_inline_context.append(dispatcher.current_task_has_inline_delivery_context())

    with dispatcher.inline_delivery_context():
        assert dispatcher.current_task_has_inline_delivery_context() is True
        child_task = asyncio.create_task(observe_after_context_exits())

    try:
        context_exited.set()
        await asyncio.wait_for(child_task, timeout=1.0)
    finally:
        context_exited.set()
        if not child_task.done():
            child_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(child_task, timeout=1.0)

    assert observed_inline_context == [False]


# Backpressure semantics under queue saturation.
@pytest.mark.asyncio
async def test_drop_newest_backpressure_policy() -> None:
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()
    second_handled = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                first_started.set()
                await allow_first_to_finish.wait()
            elif connection_id == "c2":
                second_handled.set()

    handler = BlockingHandler()
    settings = EventDeliverySettings(
        max_pending_events=1, backpressure_policy=EventBackpressurePolicy.DROP_NEWEST
    )
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))
    await dispatcher.emit(_opened(3))
    allow_first_to_finish.set()
    await asyncio.wait_for(second_handled.wait(), timeout=1.0)
    await dispatcher.stop()
    assert handler.ids == ["c1", "c2"]


@pytest.mark.asyncio
async def test_drop_oldest_backpressure_policy_keeps_newer_events() -> None:
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()
    third_handled = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                first_started.set()
                await allow_first_to_finish.wait()
            elif connection_id == "c3":
                third_handled.set()

    handler = BlockingHandler()
    settings = EventDeliverySettings(
        max_pending_events=1, backpressure_policy=EventBackpressurePolicy.DROP_OLDEST
    )
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))
    await dispatcher.emit(_opened(3))
    allow_first_to_finish.set()
    await asyncio.wait_for(third_handled.wait(), timeout=1.0)
    await dispatcher.stop()
    assert handler.ids == ["c1", "c3"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [EventBackpressurePolicy.DROP_NEWEST, EventBackpressurePolicy.DROP_OLDEST],
)
async def test_emit_and_wait_raises_when_barrier_event_is_dropped(
    policy: EventBackpressurePolicy,
) -> None:
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                first_started.set()
                await allow_first_to_finish.wait()

    handler = BlockingHandler()
    settings = EventDeliverySettings(
        dispatch_mode=EventDispatchMode.BACKGROUND,
        max_pending_events=1,
        backpressure_policy=policy,
    )
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))
    await dispatcher.start()
    emit_wait_task: asyncio.Task[None] | None = None
    try:
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        if policy == EventBackpressurePolicy.DROP_NEWEST:
            await dispatcher.emit(_opened(2))
            with pytest.raises(RuntimeError, match="dropped"):
                await dispatcher.emit_and_wait(_opened(3))
        else:
            emit_wait_task = asyncio.create_task(dispatcher.emit_and_wait(_opened(2)))
            await wait_for_condition(
                lambda: dispatcher.runtime_stats.queue_depth == 1,
                timeout_seconds=1.0,
            )
            await dispatcher.emit(_opened(3))
            with pytest.raises(RuntimeError, match="dropped"):
                await asyncio.wait_for(emit_wait_task, timeout=1.0)
    finally:
        allow_first_to_finish.set()
        if emit_wait_task is not None and not emit_wait_task.done():
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(emit_wait_task, timeout=1.0)
        await dispatcher.stop()
    if policy == EventBackpressurePolicy.DROP_NEWEST:
        assert handler.ids == ["c1", "c2"]
    else:
        assert handler.ids == ["c1", "c3"]


@pytest.mark.asyncio
async def test_drop_oldest_skips_protected_barrier_events() -> None:
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                first_started.set()
                await allow_first_to_finish.wait()

    handler = BlockingHandler()
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            max_pending_events=2,
            backpressure_policy=EventBackpressurePolicy.DROP_OLDEST,
        ),
        logging.getLogger("test"),
    )
    await dispatcher.start()
    protected_emit_task: asyncio.Task[None] | None = None
    try:
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)

        protected_emit_task = asyncio.create_task(
            dispatcher.emit_and_wait(_opened(2), drop_on_backpressure=False)
        )
        await wait_for_condition(
            lambda: dispatcher.runtime_stats.queue_depth == 1,
            timeout_seconds=1.0,
        )
        await dispatcher.emit(_opened(3))
        await wait_for_condition(
            lambda: dispatcher.runtime_stats.queue_depth == 2,
            timeout_seconds=1.0,
        )

        await dispatcher.emit(_opened(4))
        allow_first_to_finish.set()
        await asyncio.wait_for(protected_emit_task, timeout=1.0)
        await wait_for_condition(lambda: handler.ids == ["c1", "c2", "c4"])
    finally:
        allow_first_to_finish.set()
        if protected_emit_task is not None and not protected_emit_task.done():
            protected_emit_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(protected_emit_task, timeout=1.0)
        await dispatcher.stop()

    assert handler.ids == ["c1", "c2", "c4"]


@pytest.mark.asyncio
async def test_drop_oldest_protected_incoming_event_evicts_oldest_droppable_event() -> None:
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                first_started.set()
                await allow_first_to_finish.wait()

    handler = BlockingHandler()
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            max_pending_events=2,
            backpressure_policy=EventBackpressurePolicy.DROP_OLDEST,
        ),
        logging.getLogger("test"),
    )
    await dispatcher.start()
    protected_emit_task: asyncio.Task[None] | None = None
    try:
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)

        await dispatcher.emit(_opened(2))
        await dispatcher.emit(_opened(3))
        await wait_for_condition(lambda: dispatcher.runtime_stats.queue_depth == 2)

        protected_emit_task = asyncio.create_task(
            dispatcher.emit_and_wait(_opened(4), drop_on_backpressure=False)
        )
        await wait_for_condition(
            lambda: (
                dispatcher.runtime_stats.dropped_backpressure_oldest_total == 1
                and dispatcher.runtime_stats.queue_depth == 2
            )
        )
        assert protected_emit_task.done() is False

        allow_first_to_finish.set()
        await asyncio.wait_for(protected_emit_task, timeout=1.0)
        await wait_for_condition(lambda: handler.ids == ["c1", "c3", "c4"])
    finally:
        allow_first_to_finish.set()
        if protected_emit_task is not None and not protected_emit_task.done():
            protected_emit_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(protected_emit_task, timeout=1.0)
        await dispatcher.stop()

    assert handler.ids == ["c1", "c3", "c4"]


@pytest.mark.asyncio
async def test_emit_and_wait_raises_when_stop_drops_queued_barrier_event() -> None:
    first_started = asyncio.Event()
    allow_stop = asyncio.Event()

    class StopFromHandler:
        def __init__(self) -> None:
            self.dispatcher: AsyncioEventDispatcher | None = None
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                first_started.set()
                await allow_stop.wait()
                if self.dispatcher is not None:
                    await self.dispatcher.stop()

    handler = StopFromHandler()
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
    )
    handler.dispatcher = dispatcher
    await dispatcher.start()
    emit_wait_task: asyncio.Task[None] | None = None
    try:
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        emit_wait_task = asyncio.create_task(dispatcher.emit_and_wait(_opened(2)))
        await wait_for_condition(
            lambda: dispatcher.runtime_stats.queue_depth == 1,
            timeout_seconds=1.0,
        )

        allow_stop.set()
        with pytest.raises(RuntimeError, match="dropped"):
            await asyncio.wait_for(emit_wait_task, timeout=1.0)
    finally:
        allow_stop.set()
        if emit_wait_task is not None and not emit_wait_task.done():
            emit_wait_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(emit_wait_task, timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(dispatcher.stop(), timeout=1.0)
    assert handler.ids == ["c1"]


@pytest.mark.asyncio
async def test_handler_origin_stop_without_inline_context_drains_queued_events() -> None:
    first_started = asyncio.Event()
    allow_stop_to_spawn = asyncio.Event()
    allow_first_to_finish = asyncio.Event()

    class SpawnStopFromHandler:
        def __init__(self) -> None:
            self.dispatcher: AsyncioEventDispatcher | None = None
            self.stop_task: asyncio.Task[None] | None = None
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                first_started.set()
                await allow_stop_to_spawn.wait()
                if self.dispatcher is not None:
                    self.stop_task = asyncio.create_task(self.dispatcher.stop())
                await allow_first_to_finish.wait()

    handler = SpawnStopFromHandler()
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
    )
    handler.dispatcher = dispatcher
    await dispatcher.start()
    try:
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await dispatcher.emit(_opened(2))
        await wait_for_condition(
            lambda: dispatcher.runtime_stats.queue_depth == 1,
            timeout_seconds=1.0,
        )

        allow_stop_to_spawn.set()
        await wait_for_condition(lambda: handler.stop_task is not None, timeout_seconds=1.0)
        await asyncio.sleep(0)
        assert dispatcher.runtime_stats.queue_depth == 1

        allow_first_to_finish.set()
        await wait_for_condition(lambda: handler.ids == ["c1", "c2"], timeout_seconds=1.0)
        assert handler.stop_task is not None
        await asyncio.wait_for(handler.stop_task, timeout=1.0)
    finally:
        allow_stop_to_spawn.set()
        allow_first_to_finish.set()
        if handler.stop_task is not None and not handler.stop_task.done():
            handler.stop_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(handler.stop_task, timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(dispatcher.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_block_backpressure_stop_fails_emit_and_wait_blocked_for_queue_space() -> None:
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                first_started.set()
                await allow_first_to_finish.wait()

    handler = BlockingHandler()
    settings = EventDeliverySettings(
        dispatch_mode=EventDispatchMode.BACKGROUND,
        max_pending_events=1,
        backpressure_policy=EventBackpressurePolicy.BLOCK,
    )
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))
    await dispatcher.start()
    emit_wait_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None
    try:
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await dispatcher.emit(_opened(2))
        emit_wait_task = asyncio.create_task(dispatcher.emit_and_wait(_opened(3)))
        await wait_for_condition(
            lambda: bool(getattr(dispatcher._queue_not_full, "_waiters", None)),
            timeout_seconds=1.0,
        )

        stop_task = asyncio.create_task(dispatcher.stop())
        await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)
        with pytest.raises(RuntimeError, match="dropped"):
            await asyncio.wait_for(emit_wait_task, timeout=1.0)
    finally:
        allow_first_to_finish.set()
        if emit_wait_task is not None and not emit_wait_task.done():
            emit_wait_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(emit_wait_task, timeout=1.0)
        if stop_task is not None and not stop_task.done():
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(stop_task, timeout=1.0)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(dispatcher.stop(), timeout=1.0)
    assert handler.ids == ["c1", "c2"]


@pytest.mark.asyncio
async def test_block_backpressure_policy_waits_for_queue_space() -> None:
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()
    second_started = asyncio.Event()
    allow_second_to_finish = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []
            self.third_handled = asyncio.Event()

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                first_started.set()
                await allow_first_to_finish.wait()
            elif connection_id == "c2":
                second_started.set()
                await allow_second_to_finish.wait()
            elif connection_id == "c3":
                self.third_handled.set()

    handler = BlockingHandler()
    settings = EventDeliverySettings(
        max_pending_events=1, backpressure_policy=EventBackpressurePolicy.BLOCK
    )
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))
    await dispatcher.start()

    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))
    producer = asyncio.create_task(dispatcher.emit(_opened(3)))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(producer), timeout=0.05)

    allow_first_to_finish.set()
    await asyncio.wait_for(second_started.wait(), timeout=1.0)
    await asyncio.wait_for(producer, timeout=1.0)
    allow_second_to_finish.set()
    await asyncio.wait_for(handler.third_handled.wait(), timeout=1.0)
    await dispatcher.stop()
    assert handler.ids == ["c1", "c2", "c3"]


# Dispatch topology and execution-mode behavior.
@pytest.mark.asyncio
async def test_inline_mode_dispatches_without_worker_task() -> None:
    handler = Recorder()
    settings = EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE)
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await dispatcher.stop()
    assert dispatcher.is_running is False
    assert [
        e.metadata.connection_id for e in handler.events if isinstance(e, ConnectionOpenedEvent)
    ] == ["c1"]


@pytest.mark.asyncio
async def test_background_mode_without_started_worker_falls_back_to_inline() -> None:
    caller_task = asyncio.current_task()
    assert caller_task is not None

    seen_tasks: list[asyncio.Task[object] | None] = []

    class TaskRecordingHandler:
        async def on_event(self, _event: NetworkEvent) -> None:
            seen_tasks.append(asyncio.current_task())

    settings = EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND)
    dispatcher = AsyncioEventDispatcher(TaskRecordingHandler(), settings, logging.getLogger("test"))

    await dispatcher.emit(_opened(1))

    assert seen_tasks == [caller_task]


@pytest.mark.asyncio
async def test_background_mode_with_started_worker_dispatches_on_worker_task() -> None:
    caller_task = asyncio.current_task()
    assert caller_task is not None

    handled = asyncio.Event()
    seen_tasks: list[asyncio.Task[object] | None] = []

    class TaskRecordingHandler:
        async def on_event(self, _event: NetworkEvent) -> None:
            seen_tasks.append(asyncio.current_task())
            handled.set()

    settings = EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND)
    dispatcher = AsyncioEventDispatcher(TaskRecordingHandler(), settings, logging.getLogger("test"))

    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(handled.wait(), timeout=1.0)
    await dispatcher.stop()

    assert len(seen_tasks) == 1
    assert seen_tasks[0] is not None
    assert seen_tasks[0] is not caller_task


@pytest.mark.asyncio
async def test_background_mode_emit_returns_before_handler_completes() -> None:
    """
    BACKGROUND-mode emit must return after queueing, even while handler
    execution is still running.
    """
    handler_started = asyncio.Event()
    handler_release = asyncio.Event()
    handler_done = asyncio.Event()

    class SlowBlockingHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, ConnectionOpenedEvent):
                handler_started.set()
                await handler_release.wait()
                handler_done.set()

    dispatcher = AsyncioEventDispatcher(
        SlowBlockingHandler(),
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
    )
    await dispatcher.start()

    # In BACKGROUND mode, queueing and handler execution are intentionally decoupled:
    # if emit() were to await handler completion here, shutdown timing and
    # cancellation behavior become caller-coupled and no longer match the contract.
    emit_task = asyncio.create_task(dispatcher.emit(_opened(1)))
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    await asyncio.sleep(0)
    # Once queueing succeeds, emit() should be done even though the handler
    # has not finished its work path yet.
    assert emit_task.done()
    assert not handler_done.is_set()

    handler_release.set()
    await asyncio.wait_for(emit_task, timeout=1.0)
    await asyncio.wait_for(handler_done.wait(), timeout=1.0)
    await dispatcher.stop()


@pytest.mark.asyncio
async def test_background_mode_handler_spawned_task_does_not_inherit_inline_delivery() -> None:
    handled_tasks: list[asyncio.Task[None] | None] = []
    spawned_emit_finished = asyncio.Event()
    second_seen = asyncio.Event()
    dispatcher: AsyncioEventDispatcher | None = None

    async def emit_from_spawned_task() -> None:
        assert dispatcher is not None
        await dispatcher.emit(_opened(2))
        spawned_emit_finished.set()

    class SpawningHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            handled_tasks.append(asyncio.current_task())
            if isinstance(event, ConnectionOpenedEvent) and event.metadata.connection_id == "c1":
                asyncio.create_task(emit_from_spawned_task())
                return
            second_seen.set()

    dispatcher = AsyncioEventDispatcher(
        SpawningHandler(),
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
    )

    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(spawned_emit_finished.wait(), timeout=1.0)
    await asyncio.wait_for(second_seen.wait(), timeout=1.0)
    await dispatcher.stop()

    assert len(handled_tasks) == 2
    assert handled_tasks[0] is handled_tasks[1]


@pytest.mark.asyncio
async def test_background_mode_shared_dispatcher_serializes_cross_connection_handler_execution() -> (
    None
):
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()
    second_started = asyncio.Event()

    class BlockingPerConnectionHandler:
        def __init__(self) -> None:
            self.active_handlers = 0
            self.max_active_handlers = 0

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            self.active_handlers += 1
            self.max_active_handlers = max(self.max_active_handlers, self.active_handlers)
            try:
                if event.metadata.connection_id == "c1":
                    first_started.set()
                    await allow_first_to_finish.wait()
                elif event.metadata.connection_id == "c2":
                    second_started.set()
            finally:
                self.active_handlers -= 1

    handler = BlockingPerConnectionHandler()
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
    )

    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    second_emit_task = asyncio.create_task(dispatcher.emit(_opened(2)))
    await asyncio.wait_for(second_emit_task, timeout=1.0)
    assert second_started.is_set() is False

    allow_first_to_finish.set()
    await asyncio.wait_for(second_started.wait(), timeout=1.0)
    await dispatcher.stop()

    assert handler.max_active_handlers == 1


@pytest.mark.asyncio
async def test_background_mode_isolated_dispatchers_allow_parallel_handler_execution() -> None:
    release_handlers = asyncio.Event()
    both_started = asyncio.Event()
    shared_lock = asyncio.Lock()

    class ParallelTracker:
        def __init__(self) -> None:
            self.active_handlers = 0
            self.max_active_handlers = 0

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            async with shared_lock:
                self.active_handlers += 1
                self.max_active_handlers = max(self.max_active_handlers, self.active_handlers)
                if self.active_handlers == 2:
                    both_started.set()
            await release_handlers.wait()
            async with shared_lock:
                self.active_handlers -= 1

    tracker = ParallelTracker()
    dispatcher_one = AsyncioEventDispatcher(
        tracker,
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
    )
    dispatcher_two = AsyncioEventDispatcher(
        tracker,
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
    )

    await dispatcher_one.start()
    await dispatcher_two.start()

    emit_tasks = [
        asyncio.create_task(dispatcher_one.emit(_opened("iso-1"))),
        asyncio.create_task(dispatcher_two.emit(_opened("iso-2"))),
    ]
    await asyncio.wait_for(both_started.wait(), timeout=1.0)
    release_handlers.set()
    await asyncio.wait_for(asyncio.gather(*emit_tasks), timeout=1.0)

    await dispatcher_one.stop()
    await dispatcher_two.stop()

    assert tracker.max_active_handlers == 2


@pytest.mark.asyncio
async def test_shared_dispatcher_block_policy_cold_source_event_is_eventually_delivered() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    cold_handled = asyncio.Event()

    class AsymmetricLoadHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "hot-0":
                first_started.set()
                await release_first.wait()
            if connection_id == "cold-1":
                cold_handled.set()

    handler = AsymmetricLoadHandler()
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            max_pending_events=1,
            backpressure_policy=EventBackpressurePolicy.BLOCK,
        ),
        logging.getLogger("test"),
    )
    await dispatcher.start()
    await dispatcher.emit(_opened("hot-0"))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    async def _emit_hot_burst() -> None:
        for index in range(1, 4):
            await dispatcher.emit(_opened(f"hot-{index}"))

    hot_task = asyncio.create_task(_emit_hot_burst())
    cold_task = asyncio.create_task(dispatcher.emit(_opened("cold-1")))
    release_first.set()

    await asyncio.wait_for(asyncio.gather(hot_task, cold_task), timeout=2.0)
    await asyncio.wait_for(cold_handled.wait(), timeout=1.0)
    await dispatcher.stop()

    assert "cold-1" in handler.ids


@pytest.mark.asyncio
async def test_shared_dispatcher_drop_newest_can_drop_cold_source_under_hot_pressure() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    hot1_handled = asyncio.Event()

    class AsymmetricLoadHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "hot-0":
                first_started.set()
                await release_first.wait()
            if connection_id == "hot-1":
                hot1_handled.set()

    handler = AsymmetricLoadHandler()
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            max_pending_events=1,
            backpressure_policy=EventBackpressurePolicy.DROP_NEWEST,
        ),
        logging.getLogger("test"),
    )
    await dispatcher.start()
    await dispatcher.emit(_opened("hot-0"))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    await dispatcher.emit(_opened("hot-1"))
    await dispatcher.emit(_opened("cold-1"))
    release_first.set()
    await asyncio.wait_for(hot1_handled.wait(), timeout=1.0)
    await dispatcher.stop()

    assert handler.ids == ["hot-0", "hot-1"]


# Stop-boundary behavior and in-flight draining rules.
@pytest.mark.asyncio
async def test_background_mode_drops_events_emitted_after_stop_completed() -> None:
    handler = Recorder()
    settings = EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND)
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))

    await dispatcher.start()
    await dispatcher.stop()
    await dispatcher.emit(_opened(99))

    assert handler.events == []


@pytest.mark.asyncio
async def test_background_mode_drops_events_emitted_after_stop_begins() -> None:
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()
    stop_started = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.events: list[NetworkEvent] = []

        async def on_event(self, event: NetworkEvent) -> None:
            self.events.append(event)
            if isinstance(event, ConnectionOpenedEvent) and event.metadata.connection_id == "c1":
                first_started.set()
                await allow_first_to_finish.wait()

    handler = BlockingHandler()
    settings = EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND)
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))

    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    async def stop_dispatcher() -> None:
        stop_started.set()
        await dispatcher.stop()

    stop_task = asyncio.create_task(stop_dispatcher())
    await asyncio.wait_for(stop_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))
    allow_first_to_finish.set()
    await asyncio.wait_for(stop_task, timeout=1.0)

    ids = [e.metadata.connection_id for e in handler.events if isinstance(e, ConnectionOpenedEvent)]
    assert ids == ["c1"]


@pytest.mark.asyncio
async def test_background_mode_emit_stop_boundary_is_deterministic_across_repeated_runs() -> None:
    settings = EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND)

    for _ in range(50):
        first_started = asyncio.Event()
        allow_first_to_finish = asyncio.Event()

        class BlockingHandler:
            def __init__(self) -> None:
                self.events: list[NetworkEvent] = []

            async def on_event(self, event: NetworkEvent) -> None:
                self.events.append(event)
                if (
                    isinstance(event, ConnectionOpenedEvent)
                    and event.metadata.connection_id == "c1"
                ):
                    first_started.set()
                    await allow_first_to_finish.wait()

        handler = BlockingHandler()
        dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))

        await dispatcher.start()
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)

        stop_task = asyncio.create_task(dispatcher.stop())
        await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)

        late_emit_task = asyncio.create_task(dispatcher.emit(_opened(2)))
        await asyncio.wait_for(late_emit_task, timeout=1.0)

        allow_first_to_finish.set()
        await asyncio.wait_for(stop_task, timeout=1.0)

        ids = [
            e.metadata.connection_id for e in handler.events if isinstance(e, ConnectionOpenedEvent)
        ]
        assert ids == ["c1"]


@pytest.mark.asyncio
async def test_background_mode_stop_boundary_drops_concurrent_emit_burst() -> None:
    settings = EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND)

    for _ in range(25):
        first_started = asyncio.Event()
        allow_first_to_finish = asyncio.Event()

        class BlockingHandler:
            def __init__(self) -> None:
                self.events: list[NetworkEvent] = []

            async def on_event(self, event: NetworkEvent) -> None:
                self.events.append(event)
                if (
                    isinstance(event, ConnectionOpenedEvent)
                    and event.metadata.connection_id == "c1"
                ):
                    first_started.set()
                    await allow_first_to_finish.wait()

        handler = BlockingHandler()
        dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))

        await dispatcher.start()
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)

        stop_task = asyncio.create_task(dispatcher.stop())
        await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)

        late_emit_tasks = [
            asyncio.create_task(dispatcher.emit(_opened(idx))) for idx in range(2, 12)
        ]
        await asyncio.wait_for(asyncio.gather(*late_emit_tasks), timeout=1.0)

        allow_first_to_finish.set()
        await asyncio.wait_for(stop_task, timeout=1.0)

        ids = [
            event.metadata.connection_id
            for event in handler.events
            if isinstance(event, ConnectionOpenedEvent)
        ]
        assert ids == ["c1"]


@pytest.mark.asyncio
async def test_background_mode_post_stop_emit_burst_is_unsignaled_noop() -> None:
    settings = EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND)

    for _ in range(25):
        handler = Recorder()
        dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))
        start_gate = asyncio.Event()

        async def _emit_when_released(idx: int) -> None:
            await start_gate.wait()
            await dispatcher.emit(_opened(idx))

        await dispatcher.start()
        await dispatcher.stop()

        post_stop_emit_tasks = [
            asyncio.create_task(_emit_when_released(idx)) for idx in range(20, 35)
        ]
        start_gate.set()
        await asyncio.wait_for(asyncio.gather(*post_stop_emit_tasks), timeout=1.0)

        assert handler.events == []


@pytest.mark.asyncio
async def test_blocking_producer_is_released_when_dispatcher_stops() -> None:
    handler_started = asyncio.Event()
    unblock_handler = asyncio.Event()

    class BlockingHandler:
        async def on_event(self, _event: NetworkEvent) -> None:
            handler_started.set()
            await unblock_handler.wait()

    settings = EventDeliverySettings(
        max_pending_events=1, backpressure_policy=EventBackpressurePolicy.BLOCK
    )
    dispatcher = AsyncioEventDispatcher(BlockingHandler(), settings, logging.getLogger("test"))
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))
    blocked_producer = asyncio.create_task(dispatcher.emit(_opened(3)))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(blocked_producer), timeout=0.05)

    stop_task = asyncio.create_task(dispatcher.stop())
    unblock_handler.set()
    await asyncio.wait_for(stop_task, timeout=1.0)
    await asyncio.wait_for(blocked_producer, timeout=1.0)


@pytest.mark.asyncio
async def test_block_backpressure_stop_releases_multiple_blocked_producers_without_new_delivery() -> (
    None
):
    handler_started = asyncio.Event()
    unblock_handler = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []
            self.second_seen = asyncio.Event()

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                handler_started.set()
                await unblock_handler.wait()
            elif connection_id == "c2":
                self.second_seen.set()

    handler = BlockingHandler()
    settings = EventDeliverySettings(
        max_pending_events=1, backpressure_policy=EventBackpressurePolicy.BLOCK
    )
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))
    await dispatcher.start()

    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))
    producer_attempted = [asyncio.Event() for _ in range(3, 9)]

    async def _produce(idx: int, attempted: asyncio.Event) -> None:
        attempted.set()
        await dispatcher.emit(_opened(idx))

    blocked_producers = [
        asyncio.create_task(_produce(idx, attempted))
        for idx, attempted in zip(range(3, 9), producer_attempted, strict=True)
    ]
    await asyncio.wait_for(
        asyncio.gather(*(event.wait() for event in producer_attempted)), timeout=1.0
    )
    assert all(task.done() is False for task in blocked_producers)

    stop_task = asyncio.create_task(dispatcher.stop())
    await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)
    unblock_handler.set()

    await asyncio.wait_for(stop_task, timeout=1.0)
    await asyncio.wait_for(asyncio.gather(*blocked_producers), timeout=1.0)
    await asyncio.wait_for(handler.second_seen.wait(), timeout=1.0)

    assert handler.ids == ["c1", "c2"]


@pytest.mark.asyncio
async def test_block_backpressure_stop_counts_blocked_producers_as_stop_phase_drops() -> None:
    handler_started = asyncio.Event()
    unblock_handler = asyncio.Event()

    class BlockingHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, ConnectionOpenedEvent) and event.metadata.connection_id == "c1":
                handler_started.set()
                await unblock_handler.wait()

    dispatcher = AsyncioEventDispatcher(
        BlockingHandler(),
        EventDeliverySettings(
            max_pending_events=1, backpressure_policy=EventBackpressurePolicy.BLOCK
        ),
        logging.getLogger("test"),
        error_source="tcp/server/127.0.0.1/9010",
    )
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))

    producer_tasks = [asyncio.create_task(dispatcher.emit(_opened(idx))) for idx in range(3, 6)]
    await asyncio.sleep(0)

    stop_task = asyncio.create_task(dispatcher.stop())
    await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)
    unblock_handler.set()
    await asyncio.wait_for(stop_task, timeout=1.0)
    await asyncio.wait_for(asyncio.gather(*producer_tasks), timeout=1.0)

    stats = dispatcher.runtime_stats
    # c3-c5 were blocked behind BLOCK backpressure and released on stop.
    assert stats.dropped_stop_phase_total >= 3


@pytest.mark.asyncio
async def test_block_backpressure_stop_boundary_releases_blocked_producers_and_drops_late_emit() -> (
    None
):
    handler_started = asyncio.Event()
    unblock_handler = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []
            self.second_seen = asyncio.Event()

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                handler_started.set()
                await unblock_handler.wait()
            elif connection_id == "c2":
                self.second_seen.set()

    handler = BlockingHandler()
    settings = EventDeliverySettings(
        max_pending_events=1, backpressure_policy=EventBackpressurePolicy.BLOCK
    )
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))
    await dispatcher.start()

    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))
    blocked = [asyncio.create_task(dispatcher.emit(_opened(idx))) for idx in range(3, 7)]

    stop_task = asyncio.create_task(dispatcher.stop())
    await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)
    await dispatcher.emit(_opened(99))
    unblock_handler.set()

    await asyncio.wait_for(stop_task, timeout=1.0)
    await asyncio.wait_for(asyncio.gather(*blocked), timeout=1.0)
    await asyncio.wait_for(handler.second_seen.wait(), timeout=1.0)

    assert handler.ids == ["c1", "c2"]


@pytest.mark.asyncio
async def test_drop_oldest_saturated_queue_preserves_pre_stop_order_during_shutdown() -> None:
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                first_started.set()
                await allow_first_to_finish.wait()

    handler = BlockingHandler()
    settings = EventDeliverySettings(
        max_pending_events=1, backpressure_policy=EventBackpressurePolicy.DROP_OLDEST
    )
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))
    await dispatcher.start()

    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))
    await dispatcher.emit(_opened(3))

    stop_task = asyncio.create_task(dispatcher.stop())
    await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)
    await asyncio.gather(*(dispatcher.emit(_opened(idx)) for idx in range(4, 8)))

    allow_first_to_finish.set()
    await asyncio.wait_for(stop_task, timeout=1.0)

    assert handler.ids == ["c1", "c3"]


@pytest.mark.asyncio
async def test_drop_newest_saturated_queue_drains_pre_stop_event_and_drops_post_stop_emits() -> (
    None
):
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.ids: list[str] = []

        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            connection_id = event.metadata.connection_id
            self.ids.append(connection_id)
            if connection_id == "c1":
                first_started.set()
                await allow_first_to_finish.wait()

    handler = BlockingHandler()
    settings = EventDeliverySettings(
        max_pending_events=1, backpressure_policy=EventBackpressurePolicy.DROP_NEWEST
    )
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))
    await dispatcher.start()

    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))
    await dispatcher.emit(_opened(3))

    stop_task = asyncio.create_task(dispatcher.stop())
    await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)
    await asyncio.gather(*(dispatcher.emit(_opened(idx)) for idx in range(4, 8)))

    allow_first_to_finish.set()
    await asyncio.wait_for(stop_task, timeout=1.0)

    assert handler.ids == ["c1", "c2"]


# Explicit queue-accounting invariants for saturated backpressure cases.
def _simulate_saturated_queue_suffix(
    *,
    policy: EventBackpressurePolicy,
    capacity: int,
    emitted_ids: list[str],
) -> list[str]:
    """Model the suffix a saturated queue should retain for a drop policy."""

    queue: list[str] = []
    for connection_id in emitted_ids:
        if len(queue) < capacity:
            queue.append(connection_id)
            continue
        if policy == EventBackpressurePolicy.DROP_OLDEST:
            queue.pop(0)
            queue.append(connection_id)
            continue
        if policy == EventBackpressurePolicy.DROP_NEWEST:
            continue
        raise AssertionError(f"Unsupported policy for simulation: {policy}")
    return queue


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "capacity"),
    [
        (EventBackpressurePolicy.DROP_NEWEST, 1),
        (EventBackpressurePolicy.DROP_NEWEST, 3),
        (EventBackpressurePolicy.DROP_OLDEST, 1),
        (EventBackpressurePolicy.DROP_OLDEST, 3),
    ],
)
async def test_backpressure_drop_policies_match_queue_accounting_invariant_under_saturation(
    policy: EventBackpressurePolicy,
    capacity: int,
) -> None:
    """Invariant: delivered suffix must equal explicit saturated-queue accounting model."""
    for _ in range(20):
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        class BlockingThenRecordHandler:
            def __init__(self) -> None:
                self.ids: list[str] = []

            async def on_event(self, event: NetworkEvent) -> None:
                if not isinstance(event, ConnectionOpenedEvent):
                    return
                connection_id = event.metadata.connection_id
                self.ids.append(connection_id)
                if connection_id == "c1":
                    first_started.set()
                    await release_first.wait()

        handler = BlockingThenRecordHandler()
        dispatcher = AsyncioEventDispatcher(
            handler,
            EventDeliverySettings(max_pending_events=capacity, backpressure_policy=policy),
            logging.getLogger("test"),
        )
        await dispatcher.start()
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)

        burst_ids = [f"c{idx}" for idx in range(2, 14)]
        for connection_id in burst_ids:
            await dispatcher.emit(_opened(connection_id))

        expected_suffix = _simulate_saturated_queue_suffix(
            policy=policy, capacity=capacity, emitted_ids=burst_ids
        )

        release_first.set()
        await dispatcher.stop()

        assert handler.ids == ["c1", *expected_suffix]


@pytest.mark.asyncio
async def test_block_policy_repeated_concurrent_emit_accounting_is_lossless_and_bounded() -> None:
    """Invariant: BLOCK never drops accepted emits and queue occupancy never exceeds configured capacity."""
    capacity = 2

    async def _wait_until_queue_is_full(dispatcher: AsyncioEventDispatcher) -> None:
        while len(dispatcher._queue) < capacity:  # type: ignore[attr-defined]
            await asyncio.sleep(0)

    for _ in range(15):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        handler_seen: list[str] = []
        producer_attempted = [asyncio.Event() for _ in range(2, 12)]

        class BlockingThenRecordHandler:
            async def on_event(self, event: NetworkEvent) -> None:
                if not isinstance(event, ConnectionOpenedEvent):
                    return
                connection_id = event.metadata.connection_id
                handler_seen.append(connection_id)
                if connection_id == "c1":
                    first_started.set()
                    await release_first.wait()

        dispatcher = AsyncioEventDispatcher(
            BlockingThenRecordHandler(),
            EventDeliverySettings(
                max_pending_events=capacity, backpressure_policy=EventBackpressurePolicy.BLOCK
            ),
            logging.getLogger("test"),
        )
        await dispatcher.start()
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)

        async def _produce(idx: int, attempted: asyncio.Event) -> None:
            attempted.set()
            await dispatcher.emit(_opened(idx))

        producers = [
            asyncio.create_task(_produce(idx, attempted))
            for idx, attempted in zip(range(2, 12), producer_attempted, strict=True)
        ]
        await asyncio.wait_for(
            asyncio.gather(*(attempted.wait() for attempted in producer_attempted)), timeout=1.0
        )
        await asyncio.wait_for(_wait_until_queue_is_full(dispatcher), timeout=1.0)

        # Queue occupancy invariant while handler is blocked.
        assert len(dispatcher._queue) == capacity  # type: ignore[attr-defined]
        assert any(task.done() is False for task in producers)

        release_first.set()
        await asyncio.wait_for(asyncio.gather(*producers), timeout=2.0)
        await dispatcher.stop()

        delivered_payload = handler_seen[1:]
        expected_payload = [f"c{idx}" for idx in range(2, 12)]
        assert sorted(delivered_payload) == sorted(expected_payload)
        assert len(delivered_payload) == len(expected_payload)
        assert len(set(delivered_payload)) == len(expected_payload)


@pytest.mark.asyncio
async def test_dispatcher_supports_start_stop_start_reuse() -> None:
    handler = Recorder()
    dispatcher = AsyncioEventDispatcher(handler, EventDeliverySettings(), logging.getLogger("test"))

    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await handler.wait_for_event_count(1)
    await dispatcher.stop()

    await dispatcher.start()
    await dispatcher.emit(_opened(2))
    await handler.wait_for_event_count(2)
    await dispatcher.stop()

    ids = [e.metadata.connection_id for e in handler.events if isinstance(e, ConnectionOpenedEvent)]
    assert ids == ["c1", "c2"]


# Runtime statistics and observability counters.
@pytest.mark.asyncio
async def test_runtime_stats_capture_inline_fallback_and_dispatch_attempts() -> None:
    handler = Recorder()
    settings = EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND)
    dispatcher = AsyncioEventDispatcher(handler, settings, logging.getLogger("test"))

    await dispatcher.emit(_opened(1))

    stats = dispatcher.runtime_stats
    assert stats.emit_calls_total == 1
    assert stats.inline_fallback_total == 1
    assert stats.handler_dispatch_attempts_total == 1
    assert stats.enqueued_total == 0
    assert stats.queue_peak == 0


@pytest.mark.asyncio
async def test_runtime_stats_capture_backpressure_drop_counts() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            if event.metadata.connection_id == "c1":
                first_started.set()
                await release_first.wait()

    dispatcher = AsyncioEventDispatcher(
        BlockingHandler(),
        EventDeliverySettings(
            max_pending_events=1, backpressure_policy=EventBackpressurePolicy.DROP_NEWEST
        ),
        logging.getLogger("test"),
    )
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))
    await dispatcher.emit(_opened(3))

    release_first.set()
    await dispatcher.stop()

    stats = dispatcher.runtime_stats
    assert stats.emit_calls_total == 3
    assert stats.enqueued_total == 2
    assert stats.dropped_backpressure_newest_total == 1
    assert stats.dropped_backpressure_oldest_total == 0
    assert stats.queue_peak == 1


@pytest.mark.asyncio
async def test_runtime_stats_capture_stop_phase_drop_counts() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            if event.metadata.connection_id == "c1":
                first_started.set()
                await release_first.wait()

    settings = EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND)
    dispatcher = AsyncioEventDispatcher(BlockingHandler(), settings, logging.getLogger("test"))
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    stop_task = asyncio.create_task(dispatcher.stop())
    await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)
    await dispatcher.emit(_opened(2))
    await dispatcher.emit(_opened(3))
    release_first.set()
    await asyncio.wait_for(stop_task, timeout=1.0)

    stats = dispatcher.runtime_stats
    assert stats.emit_calls_total == 3
    assert stats.dropped_stop_phase_total == 2
    assert stats.enqueued_total == 1


@pytest.mark.asyncio
async def test_runtime_stats_characterize_stop_with_pending_queue_work() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            if event.metadata.connection_id == "c1":
                first_started.set()
                await release_first.wait()

    dispatcher = AsyncioEventDispatcher(
        BlockingHandler(),
        EventDeliverySettings(
            max_pending_events=2, backpressure_policy=EventBackpressurePolicy.BLOCK
        ),
        logging.getLogger("test"),
    )
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    await dispatcher.emit(_opened(2))
    await dispatcher.emit(_opened(3))

    stop_task = asyncio.create_task(dispatcher.stop())
    await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)
    await dispatcher.emit(_opened(99))
    release_first.set()
    await asyncio.wait_for(stop_task, timeout=1.0)

    stats = dispatcher.runtime_stats
    assert stats.enqueued_total == 3
    assert stats.dropped_stop_phase_total == 1
    assert stats.queue_depth == 0
    assert stats.queue_peak == 2


@pytest.mark.asyncio
async def test_stop_phase_drops_emit_distinct_warning_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, ConnectionOpenedEvent) and event.metadata.connection_id == "c1":
                first_started.set()
                await release_first.wait()

    caplog.set_level(logging.WARNING)
    dispatcher = AsyncioEventDispatcher(
        BlockingHandler(),
        EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        logging.getLogger("test"),
        error_source="tcp/server/127.0.0.1/9000",
    )
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    stop_task = asyncio.create_task(dispatcher.stop())
    await asyncio.wait_for(_wait_until_dispatcher_stopping(dispatcher), timeout=1.0)
    await dispatcher.emit(_opened(2))
    release_first.set()
    await asyncio.wait_for(stop_task, timeout=1.0)

    messages = [record.getMessage() for record in caplog.records]
    assert any("dispatcher stop is in progress" in message for message in messages)
    assert all("Dropped newest event due to backpressure." not in message for message in messages)


# Handler-failure policies exposed through the dispatcher contract.
@pytest.mark.asyncio
async def test_handler_failure_policy_emit_error_event_emits_network_error_event() -> None:
    handler = Recorder(fail=True)
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.INLINE,
            handler_failure_policy=EventHandlerFailurePolicy.EMIT_ERROR_EVENT,
        ),
        logging.getLogger("test"),
        error_source="tcp/client/127.0.0.1/12345",
    )

    await dispatcher.emit(_opened(1))

    error_events = [event for event in handler.events if isinstance(event, NetworkErrorEvent)]
    assert len(handler.events) == 2
    assert len(error_events) == 1


@pytest.mark.asyncio
async def test_handler_failure_policy_emit_error_event_does_not_recurse_on_network_error_event() -> (
    None
):
    handler = Recorder(fail=True)
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.INLINE,
            handler_failure_policy=EventHandlerFailurePolicy.EMIT_ERROR_EVENT,
        ),
        logging.getLogger("test"),
        error_source="tcp/client/127.0.0.1/12345",
    )

    await dispatcher.emit(
        NetworkErrorEvent(resource_id="tcp/client/127.0.0.1/12345", error=RuntimeError("origin"))
    )

    assert len(handler.events) == 1
    assert isinstance(handler.events[0], NetworkErrorEvent)


@pytest.mark.asyncio
async def test_handler_failure_policy_stop_component_invokes_stop_callback() -> None:
    handler = Recorder(fail=True)
    stop_calls = 0

    async def stop_component() -> None:
        nonlocal stop_calls
        stop_calls += 1

    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.INLINE,
            handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
        ),
        logging.getLogger("test"),
        stop_policy=DispatcherStopPolicy.stop_component(stop_component),
    )

    await dispatcher.emit(_opened(1))

    stop_events = [
        event for event in handler.events if isinstance(event, HandlerFailurePolicyStopEvent)
    ]
    assert len(stop_events) == 1
    assert stop_events[0].triggering_event_name == "ConnectionOpenedEvent"
    assert stop_calls == 1


@pytest.mark.asyncio
async def test_handler_failure_policy_stop_component_is_safe_in_background_worker() -> None:
    stop_calls = 0
    stopped = asyncio.Event()

    class StopInBackgroundHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            del event
            raise RuntimeError("boom")

    async def stop_component() -> None:
        nonlocal stop_calls
        stop_calls += 1
        stopped.set()

    dispatcher = AsyncioEventDispatcher(
        StopInBackgroundHandler(),
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
        ),
        logging.getLogger("test"),
        stop_policy=DispatcherStopPolicy.stop_component(stop_component),
    )
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(stopped.wait(), timeout=1.0)
    await dispatcher.stop()
    assert stop_calls == 1


@pytest.mark.asyncio
async def test_stop_component_callback_does_not_expose_inline_context_to_user_tasks() -> None:
    class FailThenStop:
        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, ConnectionOpenedEvent):
                raise RuntimeError("boom")

    child_task_started = asyncio.Event()
    allow_child_to_emit = asyncio.Event()
    child_emit_finished = asyncio.Event()
    observed_inline_context: list[bool] = []
    observed_origin_context: list[bool] = []

    async def stop_component() -> None:
        observed_origin_context.append(dispatcher.current_task_has_handler_origin_context())

        async def child_emit() -> None:
            child_task_started.set()
            await allow_child_to_emit.wait()
            observed_inline_context.append(dispatcher.current_task_has_inline_delivery_context())
            await dispatcher.emit(_opened("child"))
            child_emit_finished.set()

        child_task = asyncio.create_task(child_emit())
        await asyncio.wait_for(child_task_started.wait(), timeout=1.0)
        await asyncio.sleep(0)
        assert child_emit_finished.is_set() is False
        await dispatcher.stop()
        allow_child_to_emit.set()
        await asyncio.wait_for(child_task, timeout=1.0)

    dispatcher = AsyncioEventDispatcher(
        FailThenStop(),
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            max_pending_events=1,
            handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
        ),
        logging.getLogger("test"),
        stop_policy=DispatcherStopPolicy.stop_component(stop_component),
    )

    await dispatcher.start()
    await dispatcher.emit(_opened("failure"))
    await wait_for_condition(lambda: dispatcher.is_running is False, timeout_seconds=1.0)

    assert observed_inline_context == [False]
    assert observed_origin_context == [True]
    assert child_emit_finished.is_set()


@pytest.mark.asyncio
async def test_stop_component_callback_awaited_child_does_not_inherit_stop_authority() -> None:
    first_started = asyncio.Event()
    release_failure = asyncio.Event()
    child_ready = asyncio.Event()
    allow_child_to_stop = asyncio.Event()
    child_stop_blocked = asyncio.Event()
    child_stop_finished = asyncio.Event()
    second_seen = asyncio.Event()
    observed_child_origin_context: list[bool] = []
    observed_child_inherited_context: list[bool] = []

    class FailFirstThenRecordSecond:
        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            if event.metadata.connection_id == "c1":
                first_started.set()
                await release_failure.wait()
                raise RuntimeError("boom")
            if event.metadata.connection_id == "c2":
                second_seen.set()

    async def stop_component() -> None:
        async def child_stop() -> None:
            child_ready.set()
            await allow_child_to_stop.wait()
            observed_child_origin_context.append(
                dispatcher.current_task_has_handler_origin_context()
            )
            observed_child_inherited_context.append(
                dispatcher.current_task_inherits_handler_origin_context()
            )
            stop_task = asyncio.create_task(dispatcher.stop())
            await asyncio.sleep(0)
            if stop_task.done():
                await asyncio.wait_for(stop_task, timeout=1.0)
                child_stop_finished.set()
                return
            child_stop_blocked.set()
            stop_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(stop_task, timeout=1.0)
            child_stop_finished.set()

        await asyncio.create_task(child_stop())

    dispatcher = AsyncioEventDispatcher(
        FailFirstThenRecordSecond(),
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
        ),
        logging.getLogger("test"),
        stop_policy=DispatcherStopPolicy.stop_component(stop_component),
    )
    await dispatcher.start()
    try:
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await dispatcher.emit(_opened(2))
        await wait_for_condition(
            lambda: dispatcher.runtime_stats.queue_depth == 1,
            timeout_seconds=1.0,
        )

        release_failure.set()
        await asyncio.wait_for(child_ready.wait(), timeout=1.0)
        allow_child_to_stop.set()
        await asyncio.wait_for(child_stop_blocked.wait(), timeout=1.0)
        await asyncio.wait_for(second_seen.wait(), timeout=1.0)
        await asyncio.wait_for(child_stop_finished.wait(), timeout=1.0)
        assert observed_child_origin_context == [False]
        assert observed_child_inherited_context == [False]
    finally:
        release_failure.set()
        allow_child_to_stop.set()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(dispatcher.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_stop_component_callback_child_stop_waits_for_queued_events() -> None:
    first_started = asyncio.Event()
    release_failure = asyncio.Event()
    child_ready = asyncio.Event()
    allow_child_to_stop = asyncio.Event()
    child_stop_finished = asyncio.Event()
    second_seen = asyncio.Event()
    ordering_errors: list[str] = []

    class FailFirstThenRecordSecond:
        async def on_event(self, event: NetworkEvent) -> None:
            if not isinstance(event, ConnectionOpenedEvent):
                return
            if event.metadata.connection_id == "c1":
                first_started.set()
                await release_failure.wait()
                raise RuntimeError("boom")
            if event.metadata.connection_id == "c2":
                if child_stop_finished.is_set():
                    ordering_errors.append("child stop returned before queued c2 event")
                second_seen.set()

    async def stop_component() -> None:
        async def child_stop() -> None:
            child_ready.set()
            await allow_child_to_stop.wait()
            await dispatcher.stop()
            child_stop_finished.set()

        _ = asyncio.create_task(child_stop())

    dispatcher = AsyncioEventDispatcher(
        FailFirstThenRecordSecond(),
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
        ),
        logging.getLogger("test"),
        stop_policy=DispatcherStopPolicy.stop_component(stop_component),
    )
    await dispatcher.start()
    try:
        await dispatcher.emit(_opened(1))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await dispatcher.emit(_opened(2))
        await wait_for_condition(
            lambda: dispatcher.runtime_stats.queue_depth == 1,
            timeout_seconds=1.0,
        )

        release_failure.set()
        await asyncio.wait_for(child_ready.wait(), timeout=1.0)
        allow_child_to_stop.set()
        await asyncio.wait_for(second_seen.wait(), timeout=1.0)
        await asyncio.wait_for(child_stop_finished.wait(), timeout=1.0)
        assert ordering_errors == []
    finally:
        release_failure.set()
        allow_child_to_stop.set()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(dispatcher.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_handler_failure_policy_emit_error_event_does_not_deadlock_when_block_queue_is_full() -> (
    None
):
    first_started = asyncio.Event()
    release_failure = asyncio.Event()
    second_delivered = asyncio.Event()
    error_event_delivered = asyncio.Event()

    class FailFirstThenRecord:
        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, ConnectionOpenedEvent):
                if event.metadata.connection_id == "c1":
                    first_started.set()
                    await release_failure.wait()
                    raise RuntimeError("boom")
                if event.metadata.connection_id == "c2":
                    second_delivered.set()
                    return
            if isinstance(event, NetworkErrorEvent):
                error_event_delivered.set()

    dispatcher = AsyncioEventDispatcher(
        FailFirstThenRecord(),
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            max_pending_events=1,
            backpressure_policy=EventBackpressurePolicy.BLOCK,
            handler_failure_policy=EventHandlerFailurePolicy.EMIT_ERROR_EVENT,
        ),
        logging.getLogger("test"),
        error_source="tcp/client/127.0.0.1/12345",
    )
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))

    release_failure.set()
    await asyncio.wait_for(error_event_delivered.wait(), timeout=1.0)
    await asyncio.wait_for(second_delivered.wait(), timeout=1.0)
    await dispatcher.stop()


@pytest.mark.asyncio
async def test_handler_failure_policy_stop_component_does_not_deadlock_when_block_queue_is_full() -> (
    None
):
    first_started = asyncio.Event()
    release_failure = asyncio.Event()
    second_delivered = asyncio.Event()
    stop_event_delivered = asyncio.Event()
    stop_callback_called = asyncio.Event()

    class FailFirstThenRecord:
        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, ConnectionOpenedEvent):
                if event.metadata.connection_id == "c1":
                    first_started.set()
                    await release_failure.wait()
                    raise RuntimeError("boom")
                if event.metadata.connection_id == "c2":
                    second_delivered.set()
                    return
            if isinstance(event, HandlerFailurePolicyStopEvent):
                stop_event_delivered.set()

    async def stop_component() -> None:
        stop_callback_called.set()

    dispatcher = AsyncioEventDispatcher(
        FailFirstThenRecord(),
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            max_pending_events=1,
            backpressure_policy=EventBackpressurePolicy.BLOCK,
            handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
        ),
        logging.getLogger("test"),
        stop_policy=DispatcherStopPolicy.stop_component(stop_component),
    )
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await dispatcher.emit(_opened(2))

    release_failure.set()
    await asyncio.wait_for(stop_event_delivered.wait(), timeout=1.0)
    await asyncio.wait_for(stop_callback_called.wait(), timeout=1.0)
    await asyncio.wait_for(second_delivered.wait(), timeout=1.0)
    await dispatcher.stop()
    assert dispatcher.runtime_stats.queue_depth == 0


@pytest.mark.asyncio
async def test_stop_component_policy_with_concurrent_external_stop_is_non_recursive_and_non_deadlocking() -> (
    None
):
    stop_calls = 0
    callback_started = asyncio.Event()
    callback_finished = asyncio.Event()
    observed: list[NetworkEvent] = []
    dispatcher: AsyncioEventDispatcher

    class FailingThenRecordHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            observed.append(event)
            if isinstance(event, ConnectionOpenedEvent):
                raise RuntimeError("boom")

    async def stop_component() -> None:
        nonlocal stop_calls
        stop_calls += 1
        callback_started.set()
        await dispatcher.stop()
        callback_finished.set()

    dispatcher = AsyncioEventDispatcher(
        FailingThenRecordHandler(),
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
        ),
        logging.getLogger("test"),
        stop_policy=DispatcherStopPolicy.stop_component(stop_component),
    )
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(callback_started.wait(), timeout=1.0)
    external_stop_task = asyncio.create_task(dispatcher.stop())
    await asyncio.wait_for(callback_finished.wait(), timeout=1.0)
    await asyncio.wait_for(external_stop_task, timeout=1.0)

    assert stop_calls == 1
    assert dispatcher.is_running is False
    assert sum(isinstance(event, HandlerFailurePolicyStopEvent) for event in observed) == 1


@pytest.mark.asyncio
async def test_stop_component_policy_emits_observable_stop_event_before_callback() -> None:
    observed: list[NetworkEvent] = []

    class FailingThenRecordHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            observed.append(event)
            if isinstance(event, ConnectionOpenedEvent):
                raise RuntimeError("boom")

    stop_calls = 0

    async def stop_component() -> None:
        nonlocal stop_calls
        stop_calls += 1

    dispatcher = AsyncioEventDispatcher(
        FailingThenRecordHandler(),
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.INLINE,
            handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
        ),
        logging.getLogger("test"),
        error_source="tcp/client/127.0.0.1/12345",
        stop_policy=DispatcherStopPolicy.stop_component(stop_component),
    )

    await dispatcher.emit(_opened(7))

    assert isinstance(observed[1], HandlerFailurePolicyStopEvent)
    assert stop_calls == 1


@pytest.mark.asyncio
async def test_handler_failure_policy_raise_in_inline_mode_raises() -> None:
    handler = Recorder(fail=True)
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.INLINE,
            handler_failure_policy=EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE,
        ),
        logging.getLogger("test"),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await dispatcher.emit(_opened(1))


# Stop-policy validation and compatibility rules.
def test_dispatcher_stop_policy_disabled_is_explicit_default() -> None:
    dispatcher = AsyncioEventDispatcher(
        Recorder(),
        EventDeliverySettings(),
        logging.getLogger("test"),
    )

    assert dispatcher.stop_policy == DispatcherStopPolicy.disabled()


def test_dispatcher_stop_policy_rejects_enabled_without_callback() -> None:
    with pytest.raises(ValueError, match="requires a callback"):
        AsyncioEventDispatcher(
            Recorder(),
            EventDeliverySettings(),
            logging.getLogger("test"),
            stop_policy=DispatcherStopPolicy(enabled=True),
        )


def test_dispatcher_rejects_legacy_callback_and_explicit_policy_together() -> None:
    async def stop_component() -> None:
        return None

    with pytest.raises(ValueError, match="either stop_policy or stop_component_callback"):
        AsyncioEventDispatcher(
            Recorder(),
            EventDeliverySettings(),
            logging.getLogger("test"),
            stop_policy=DispatcherStopPolicy.stop_component(stop_component),
            stop_component_callback=stop_component,
        )


@pytest.mark.asyncio
async def test_handler_failure_policy_raise_in_inline_mode_background_still_logs_only() -> None:
    handler = Recorder(fail=True)
    dispatcher = AsyncioEventDispatcher(
        handler,
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            handler_failure_policy=EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE,
        ),
        logging.getLogger("test"),
    )
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await handler.wait_for_event_count(1)
    await dispatcher.stop()
    assert len(handler.events) == 1


@pytest.mark.asyncio
async def test_handler_cancelled_error_is_treated_as_handler_failure() -> None:
    first_seen = asyncio.Event()
    second_seen = asyncio.Event()
    events: list[NetworkEvent] = []

    class CancelFirstThenRecord:
        async def on_event(self, event: NetworkEvent) -> None:
            events.append(event)
            if len(events) == 1:
                first_seen.set()
                raise asyncio.CancelledError
            second_seen.set()

    dispatcher = AsyncioEventDispatcher(
        CancelFirstThenRecord(),
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            handler_failure_policy=EventHandlerFailurePolicy.LOG_ONLY,
        ),
        logging.getLogger("test"),
    )
    await dispatcher.start()
    await dispatcher.emit(_opened(1))
    await asyncio.wait_for(first_seen.wait(), timeout=1.0)

    assert dispatcher.is_running is True
    assert dispatcher.runtime_stats.handler_failures_total == 1

    await dispatcher.emit(_opened(2))
    await asyncio.wait_for(second_seen.wait(), timeout=1.0)
    await dispatcher.stop()

    assert [type(event).__name__ for event in events] == [
        "ConnectionOpenedEvent",
        "ConnectionOpenedEvent",
    ]


@pytest.mark.asyncio
async def test_inline_dispatcher_caller_cancellation_propagates() -> None:
    start_blocked = asyncio.Event()
    handler_reached = asyncio.Event()

    class BlockingInlineHandler:
        async def on_event(self, event: NetworkEvent) -> None:
            handler_reached.set()
            start_blocked.set()
            await asyncio.Event().wait()

    dispatcher = AsyncioEventDispatcher(
        BlockingInlineHandler(),
        EventDeliverySettings(
            dispatch_mode=EventDispatchMode.INLINE,
            handler_failure_policy=EventHandlerFailurePolicy.LOG_ONLY,
        ),
        logging.getLogger("test"),
    )
    emit_task = asyncio.create_task(dispatcher.emit(_opened(1)))
    await asyncio.wait_for(start_blocked.wait(), timeout=1.0)
    assert handler_reached.is_set()

    emit_task.cancel()
    await assert_awaitable_cancelled(emit_task)
