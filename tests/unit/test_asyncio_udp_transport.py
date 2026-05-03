"""
Contract tests for the asyncio UDP receiver and sender implementations.

These tests focus on lifecycle rollback, lazy socket creation, fallback
polling, payload and target validation, and deterministic shutdown. Receiver
and sender coverage live together because the public UDP API is asymmetric but
the two components still share socket and lifecycle invariants.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

import pytest

from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.connection_events import ConnectionClosedEvent
from aionetx.api.connection_events import ConnectionOpenedEvent
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.errors import NetworkConfigurationError, NetworkRuntimeError
from aionetx.api.event_delivery_settings import (
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
)
from aionetx.api.handler_failure_policy_stop_event import HandlerFailurePolicyStopEvent
from aionetx.api.udp import UdpInvalidTargetError
from aionetx.api.udp import UdpReceiverSettings
from aionetx.api.udp import UdpSenderStoppedError
from aionetx.api.udp import UdpSenderSettings
from aionetx.implementations.asyncio_impl import (
    _asyncio_datagram_receiver_base as datagram_base_module,
)
from aionetx.implementations.asyncio_impl import asyncio_udp_receiver as udp_receiver_module
from aionetx.implementations.asyncio_impl import asyncio_udp_sender as udp_sender_module
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver
from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender
from tests.helpers import assert_awaitable_cancelled
from tests.helpers import drain_awaitable_ignoring_cancelled
from tests.helpers import wait_for_condition


class NoopHandler:
    async def on_event(self, event) -> None:
        return None


class FailOnOpenedHandler:
    async def on_event(self, event) -> None:
        if isinstance(event, ConnectionOpenedEvent):
            raise RuntimeError("boom-on-opened")


class FailOnLifecycleStateHandler:
    def __init__(self, state: ComponentLifecycleState) -> None:
        self._state = state

    async def on_event(self, event) -> None:
        if isinstance(event, ComponentLifecycleChangedEvent) and event.current == self._state:
            raise RuntimeError(f"udp-{self._state.value}-publication-failed")


class RecordAndFailOnStoppingHandler:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def on_event(self, event) -> None:
        self.events.append(event)
        if (
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current == ComponentLifecycleState.STOPPING
        ):
            raise RuntimeError("udp-stopping-publication-failed")


class StopOnStartingHandler:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.receiver: AsyncioUdpReceiver | None = None

    async def on_event(self, event) -> None:
        self.events.append(event)
        if (
            self.receiver is not None
            and isinstance(event, ComponentLifecycleChangedEvent)
            and event.current == ComponentLifecycleState.STARTING
        ):
            await self.receiver.stop()


class BlockingStoppingHandler:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.stopping_seen = asyncio.Event()
        self.release_stopping = asyncio.Event()

    async def on_event(self, event) -> None:
        self.events.append(event)
        if (
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current == ComponentLifecycleState.STOPPING
        ):
            self.stopping_seen.set()
            await self.release_stopping.wait()


class StopAgainOnStoppingHandler:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.receiver: AsyncioUdpReceiver | None = None
        self.reentered_stop = asyncio.Event()

    async def on_event(self, event) -> None:
        self.events.append(event)
        if (
            self.receiver is not None
            and isinstance(event, ComponentLifecycleChangedEvent)
            and event.current == ComponentLifecycleState.STOPPING
        ):
            await self.receiver.stop()
            self.reentered_stop.set()


class SpawnStopOnOpenedHandler:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.receiver: AsyncioUdpReceiver | None = None
        self.opened_seen = asyncio.Event()
        self.opened_finished = asyncio.Event()
        self.closed_seen = asyncio.Event()
        self.stop_returned = asyncio.Event()
        self.release_opened = asyncio.Event()
        self.stop_task: asyncio.Task[None] | None = None
        self.error: BaseException | None = None

    async def on_event(self, event) -> None:
        self.events.append(event)
        if isinstance(event, ConnectionClosedEvent):
            if not self.opened_finished.is_set():
                self.error = AssertionError("closed event re-entered opened handler")
                self.release_opened.set()
                return
            self.closed_seen.set()
            return
        if (
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
            and not self.opened_finished.is_set()
        ):
            self.error = AssertionError("lifecycle event re-entered opened handler")
            self.release_opened.set()
            return
        if self.receiver is None or not isinstance(event, ConnectionOpenedEvent):
            return
        self.opened_seen.set()
        try:
            self.stop_task = asyncio.create_task(self.receiver.stop())
            await self.stop_task
            self.stop_returned.set()
            if self.closed_seen.is_set():
                raise AssertionError("closed event was published before opened handler returned")
            await self.release_opened.wait()
        except (Exception, asyncio.CancelledError) as error:
            self.error = error
            self.release_opened.set()
        finally:
            self.opened_finished.set()


class HoldingOpenEventLockHandler:
    def __init__(self) -> None:
        self.receiver: AsyncioUdpReceiver | None = None
        self.opened_and_locked = asyncio.Event()

    async def on_event(self, event) -> None:
        if self.receiver is None or not isinstance(event, ConnectionOpenedEvent):
            return
        acquired = False
        try:
            await self.receiver._state_lock.acquire()  # type: ignore[attr-defined]
            acquired = True
            self.opened_and_locked.set()
            await asyncio.Event().wait()
        finally:
            if acquired:
                self.receiver._state_lock.release()  # type: ignore[attr-defined]


def _get_unused_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
async def test_udp_receiver_start_failure_cleans_dispatcher_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_bind(self: socket.socket, address: tuple[str, int]) -> None:
        raise OSError("bind-failed")

    monkeypatch.setattr(socket.socket, "bind", failing_bind)

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=21001),
        event_handler=NoopHandler(),
    )

    with pytest.raises(OSError, match="bind-failed"):
        await receiver.start()

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    # Verify that the dispatcher background worker was cleaned up via the public contract.
    assert not receiver._event_dispatcher.is_running


@pytest.mark.asyncio
async def test_udp_receiver_start_failure_rolls_back_without_closed_event(
    awaitable_recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_bind(self: socket.socket, address: tuple[str, int]) -> None:
        raise OSError("bind-failed")

    monkeypatch.setattr(socket.socket, "bind", failing_bind)

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=21011),
        event_handler=awaitable_recording_event_handler,
    )

    with pytest.raises(OSError, match="bind-failed"):
        await receiver.start()

    lifecycle_states = [
        event.current
        for event in awaitable_recording_event_handler.lifecycle_events
        if event.resource_id == receiver._connection_id  # type: ignore[attr-defined]
    ]
    assert lifecycle_states == [ComponentLifecycleState.STARTING, ComponentLifecycleState.STOPPED]
    assert awaitable_recording_event_handler.closed_events == []


@pytest.mark.asyncio
async def test_udp_receiver_opened_handler_failure_rolls_back_runtime_resources() -> None:
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=_get_unused_udp_port(),
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.INLINE,
                handler_failure_policy=EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE,
            ),
        ),
        event_handler=FailOnOpenedHandler(),
    )

    with pytest.raises(RuntimeError, match="boom-on-opened"):
        await receiver.start()

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._socket is None  # type: ignore[attr-defined]
    assert receiver._task is None  # type: ignore[attr-defined]
    assert not receiver._event_dispatcher.is_running  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_startup_cancellation_after_opened_event_rolls_back_before_task_creation() -> (
    None
):
    handler = HoldingOpenEventLockHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=_get_unused_udp_port(),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=handler,
    )
    handler.receiver = receiver

    start_task = asyncio.create_task(receiver.start())
    await asyncio.wait_for(handler.opened_and_locked.wait(), timeout=1.0)
    start_task.cancel()
    await assert_awaitable_cancelled(start_task)

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._socket is None  # type: ignore[attr-defined]
    assert receiver._task is None  # type: ignore[attr-defined]
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_background_startup_cancellation_after_opened_event_rolls_back_before_task_creation() -> (
    None
):
    opened_emit_seen = asyncio.Event()
    allow_opened_emit_to_return = asyncio.Event()

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=_get_unused_udp_port(),
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.BACKGROUND,
            ),
        ),
        event_handler=NoopHandler(),
    )

    original_emit_and_wait = receiver._event_dispatcher.emit_and_wait  # type: ignore[attr-defined]

    async def _emit_and_wait_blocking_opened_event(
        event, *, drop_on_backpressure: bool = True
    ) -> None:
        if isinstance(event, ConnectionOpenedEvent):
            opened_emit_seen.set()
            await allow_opened_emit_to_return.wait()
        await original_emit_and_wait(event, drop_on_backpressure=drop_on_backpressure)

    receiver._event_dispatcher.emit_and_wait = _emit_and_wait_blocking_opened_event  # type: ignore[method-assign]

    start_task = asyncio.create_task(receiver.start())
    await asyncio.wait_for(opened_emit_seen.wait(), timeout=1.0)

    start_task.cancel()
    await assert_awaitable_cancelled(start_task)

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._socket is None  # type: ignore[attr-defined]
    assert receiver._task is None  # type: ignore[attr-defined]
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_cancelled_external_stop_during_opened_barrier_publishes_terminal_events() -> (
    None
):
    class BlockingOpenedHandler:
        def __init__(self) -> None:
            self.events: list[object] = []
            self.opened_seen = asyncio.Event()
            self.release_opened = asyncio.Event()

        async def on_event(self, event) -> None:
            self.events.append(event)
            if isinstance(event, ConnectionOpenedEvent):
                self.opened_seen.set()
                await self.release_opened.wait()

    handler = BlockingOpenedHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=_get_unused_udp_port(),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        ),
        event_handler=handler,
    )
    start_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None
    joiner_stop_task: asyncio.Task[None] | None = None
    try:
        start_task = asyncio.create_task(receiver.start())
        await asyncio.wait_for(handler.opened_seen.wait(), timeout=1.0)

        stop_task = asyncio.create_task(receiver.stop())
        await wait_for_condition(
            lambda: receiver.lifecycle_state == ComponentLifecycleState.STOPPING,
            timeout_seconds=1.0,
        )
        stop_task.cancel()
        await assert_awaitable_cancelled(stop_task)

        joiner_stop_task = asyncio.create_task(receiver.stop())
        await asyncio.sleep(0)
        assert not joiner_stop_task.done()

        handler.release_opened.set()
        await asyncio.wait_for(joiner_stop_task, timeout=1.0)
        await asyncio.wait_for(start_task, timeout=1.0)
    finally:
        handler.release_opened.set()
        for task in (stop_task, joiner_stop_task, start_task):
            if task is not None and not task.done():
                task.cancel()
                await drain_awaitable_ignoring_cancelled(task)
        await receiver.stop()

    lifecycle_states = [
        event.current
        for event in handler.events
        if isinstance(event, ComponentLifecycleChangedEvent)
    ]
    assert ComponentLifecycleState.STOPPING in lifecycle_states
    assert ComponentLifecycleState.STOPPED in lifecycle_states
    assert any(isinstance(event, ConnectionClosedEvent) for event in handler.events)
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_starting_lifecycle_failure_rolls_back_dispatcher() -> None:
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=_get_unused_udp_port(),
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.INLINE,
                handler_failure_policy=EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE,
            ),
        ),
        event_handler=FailOnLifecycleStateHandler(ComponentLifecycleState.STARTING),
    )

    with pytest.raises(RuntimeError, match="udp-starting-publication-failed"):
        await receiver.start()

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._socket is None  # type: ignore[attr-defined]
    assert receiver._task is None  # type: ignore[attr-defined]
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dispatch_mode",
    [EventDispatchMode.INLINE, EventDispatchMode.BACKGROUND],
)
async def test_udp_receiver_starting_handler_stop_aborts_startup_without_resources(
    dispatch_mode: EventDispatchMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _get_unused_udp_port()

    class SocketSetupShouldNotRun:
        AF_INET = socket.AF_INET
        SOCK_DGRAM = socket.SOCK_DGRAM
        IPPROTO_UDP = socket.IPPROTO_UDP

        @staticmethod
        def socket(*args, **kwargs):
            del args, kwargs
            raise AssertionError("socket setup should not continue after STARTING stop")

    monkeypatch.setattr(udp_receiver_module, "socket", SocketSetupShouldNotRun)

    handler = StopOnStartingHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=port,
            event_delivery=EventDeliverySettings(dispatch_mode=dispatch_mode),
        ),
        event_handler=handler,
    )
    handler.receiver = receiver

    await receiver.start()
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED

    lifecycle_states = [
        event.current
        for event in handler.events
        if isinstance(event, ComponentLifecycleChangedEvent)
    ]
    assert lifecycle_states == [
        ComponentLifecycleState.STARTING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    ]
    assert not any(isinstance(event, ConnectionOpenedEvent) for event in handler.events)
    assert not any(isinstance(event, ConnectionClosedEvent) for event in handler.events)
    assert not receiver._running  # type: ignore[attr-defined]
    assert receiver._socket is None  # type: ignore[attr-defined]
    assert receiver._task is None  # type: ignore[attr-defined]
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_receive_loop_runtime_failure_emits_error_and_stops(
    awaitable_recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingLoop:
        async def sock_recvfrom(self, _sock: socket.socket, _size: int):
            raise RuntimeError("recv-failed")

    monkeypatch.setattr(datagram_base_module.asyncio, "get_running_loop", lambda: _FailingLoop())

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=21002),
        event_handler=awaitable_recording_event_handler,
    )
    await receiver.start()
    await asyncio.wait_for(awaitable_recording_event_handler.error_observed.wait(), timeout=1.0)
    await asyncio.wait_for(awaitable_recording_event_handler.connection_closed.wait(), timeout=1.0)

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    # lifecycle_state == STOPPED is the public invariant for socket release;
    # no need to inspect the private _socket attribute.
    assert (
        awaitable_recording_event_handler.closed_events[-1].previous_state
        == ConnectionState.CONNECTED
    )
    assert awaitable_recording_event_handler.closed_events[-1].metadata is not None


@pytest.mark.asyncio
async def test_udp_receiver_stop_lifecycle_failure_still_closes_socket_and_stops_dispatcher() -> (
    None
):
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=_get_unused_udp_port(),
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.INLINE,
                handler_failure_policy=EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE,
            ),
        ),
        event_handler=FailOnLifecycleStateHandler(ComponentLifecycleState.STOPPING),
    )

    await receiver.start()
    with pytest.raises(RuntimeError, match="udp-stopping-publication-failed"):
        await receiver.stop()

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._socket is None  # type: ignore[attr-defined]
    assert receiver._task is None  # type: ignore[attr-defined]
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_stop_component_failure_during_stopping_publishes_single_terminal_sequence() -> (
    None
):
    handler = RecordAndFailOnStoppingHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=_get_unused_udp_port(),
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.BACKGROUND,
                handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
            ),
        ),
        event_handler=handler,
    )

    await receiver.start()
    await asyncio.wait_for(receiver.stop(), timeout=1.0)

    stop_relevant_events = [
        event
        for event in handler.events
        if isinstance(event, (ConnectionClosedEvent, HandlerFailurePolicyStopEvent))
        or (
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
        )
    ]

    assert [
        (
            event.current
            if isinstance(event, ComponentLifecycleChangedEvent)
            else type(event).__name__
        )
        for event in stop_relevant_events
    ] == [
        ComponentLifecycleState.STOPPING,
        "HandlerFailurePolicyStopEvent",
        "ConnectionClosedEvent",
        ComponentLifecycleState.STOPPED,
    ]
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._socket is None  # type: ignore[attr-defined]
    assert receiver._task is None  # type: ignore[attr-defined]
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_stop_cancellation_still_detaches_receive_task_and_socket() -> None:
    stopping_seen = asyncio.Event()

    class BlockingStoppingHandler:
        async def on_event(self, event) -> None:
            if (
                isinstance(event, ComponentLifecycleChangedEvent)
                and event.current == ComponentLifecycleState.STOPPING
            ):
                stopping_seen.set()
                await asyncio.Event().wait()

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=_get_unused_udp_port(),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=BlockingStoppingHandler(),
    )

    await receiver.start()
    original_task = receiver._task  # type: ignore[attr-defined]
    stop_task = asyncio.create_task(receiver.stop())
    await asyncio.wait_for(stopping_seen.wait(), timeout=1.0)
    stop_task.cancel()

    # Supported Python releases differ in whether this caller cancellation
    # remains visible after inline handler-failure handling. Cleanup is stable.
    await drain_awaitable_ignoring_cancelled(stop_task)

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._socket is None  # type: ignore[attr-defined]
    assert receiver._task is None  # type: ignore[attr-defined]
    assert original_task is not None and original_task.done()
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_stop_cancellation_waits_for_receive_task_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_cancel_seen = asyncio.Event()
    release_task = asyncio.Event()

    async def blocking_receive_datagrams(self, receive_buffer_size: int) -> None:
        del self, receive_buffer_size
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            task_cancel_seen.set()
            await release_task.wait()
            raise

    monkeypatch.setattr(
        datagram_base_module._AsyncioDatagramReceiverBase,
        "_receive_datagrams",
        blocking_receive_datagrams,
    )

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=_get_unused_udp_port(),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=NoopHandler(),
    )

    await receiver.start()
    original_task = receiver._task  # type: ignore[attr-defined]
    assert original_task is not None

    stop_task = asyncio.create_task(receiver.stop())
    try:
        await asyncio.wait_for(task_cancel_seen.wait(), timeout=1.0)
        stop_task.cancel()
        await asyncio.sleep(0)

        assert stop_task.done() is False

        release_task.set()
        await assert_awaitable_cancelled(stop_task)
    finally:
        release_task.set()
        if not stop_task.done():
            stop_task.cancel()
            await drain_awaitable_ignoring_cancelled(stop_task)

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._socket is None  # type: ignore[attr-defined]
    assert receiver._task is None  # type: ignore[attr-defined]
    assert original_task.done()
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_inline_handler_can_stop_from_bytes_received_without_self_await() -> (
    None
):
    stopped = asyncio.Event()

    class StopFromBytesHandler:
        def __init__(self) -> None:
            self.receiver: AsyncioUdpReceiver | None = None

        async def on_event(self, event) -> None:
            if self.receiver is not None and type(event).__name__ == "BytesReceivedEvent":
                await self.receiver.stop()
                stopped.set()

    port = _get_unused_udp_port()
    handler = StopFromBytesHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=port,
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=handler,
    )
    handler.receiver = receiver
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port)
    )
    try:
        await receiver.start()
        await sender.send(b"stop-from-handler")
        await asyncio.wait_for(stopped.wait(), timeout=1.0)
        assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
        assert receiver._task is None  # type: ignore[attr-defined]
    finally:
        await sender.stop()
        await receiver.stop()


@pytest.mark.asyncio
async def test_udp_sender_nonblocking_fallback_uses_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LoopWithoutSockSendTo:
        pass

    class _TransientBlockingSocket:
        def __init__(self) -> None:
            self.calls = 0

        def sendto(self, payload: bytes, target: tuple[str, int]) -> None:
            self.calls += 1
            if self.calls < 3:
                raise BlockingIOError
            return None

        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 0)

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(
        udp_sender_module.asyncio, "get_running_loop", lambda: _LoopWithoutSockSendTo()
    )
    monkeypatch.setattr(udp_sender_module.asyncio, "sleep", fake_sleep)

    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=12345)
    )
    sock = _TransientBlockingSocket()
    await sender._send_nonblocking(sock, b"payload", ("127.0.0.1", 12345))  # type: ignore[arg-type,attr-defined]

    assert sock.calls == 3
    assert sleep_delays == [
        sender._WOULD_BLOCK_INITIAL_DELAY_SECONDS,
        sender._WOULD_BLOCK_INITIAL_DELAY_SECONDS * 2,
    ]


@pytest.mark.asyncio
async def test_udp_receiver_nonblocking_fallback_uses_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LoopWithoutSockRecvFrom:
        pass

    class _TransientBlockingSocket:
        def __init__(self) -> None:
            self.calls = 0

        def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
            self.calls += 1
            if self.calls < 3:
                raise BlockingIOError
            return (b"payload", ("127.0.0.1", 1111))

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(datagram_base_module.asyncio, "sleep", fake_sleep)

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=21009),
        event_handler=NoopHandler(),
    )
    sock = _TransientBlockingSocket()
    data, sender = await receiver._recv_nonblocking(_LoopWithoutSockRecvFrom(), sock, 1024)  # type: ignore[arg-type,attr-defined]

    assert data == b"payload"
    assert sender == ("127.0.0.1", 1111)
    assert sock.calls == 3
    assert sleep_delays == [
        receiver._WOULD_BLOCK_INITIAL_DELAY_SECONDS,
        receiver._WOULD_BLOCK_INITIAL_DELAY_SECONDS * 2,
    ]


@pytest.mark.asyncio
async def test_udp_receiver_nonblocking_fallback_logs_one_warning_per_receiver(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _LoopWithoutSockRecvFrom:
        pass

    class _TransientBlockingSocket:
        def __init__(self) -> None:
            self.calls = 0

        def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
            self.calls += 1
            if self.calls in (1, 3):
                raise BlockingIOError
            return (b"payload", ("127.0.0.1", 1111))

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(datagram_base_module.asyncio, "sleep", fake_sleep)

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=21010),
        event_handler=NoopHandler(),
    )
    sock = _TransientBlockingSocket()

    with caplog.at_level(logging.WARNING):
        await receiver._recv_nonblocking(_LoopWithoutSockRecvFrom(), sock, 1024)  # type: ignore[arg-type,attr-defined]
        await receiver._recv_nonblocking(_LoopWithoutSockRecvFrom(), sock, 1024)  # type: ignore[arg-type,attr-defined]

    warning_messages = [
        message
        for message in caplog.messages
        if "fallback polling because the event loop does not expose sock_recvfrom" in message
    ]
    assert warning_messages == [
        "UDP is using recvfrom() fallback polling because the event loop does not expose sock_recvfrom()."
    ]


@pytest.mark.asyncio
async def test_udp_sender_constructor_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    socket_calls: list[tuple[int, int, int]] = []
    original_socket = socket.socket

    def tracking_socket(family: int, stype: int, proto: int = 0):
        socket_calls.append((family, stype, proto))
        return original_socket(family, stype, proto)

    monkeypatch.setattr(udp_sender_module.socket, "socket", tracking_socket)

    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=12345)
    )
    await sender.stop()

    assert socket_calls == []


@pytest.mark.asyncio
async def test_udp_sender_first_send_initializes_socket() -> None:
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=_get_unused_udp_port())
    )
    try:
        # Verify lazy socket initialization: no socket before first send.
        assert not sender.is_socket_initialized
        await sender.send(b"first")
        assert sender.is_socket_initialized
    finally:
        await sender.stop()


@pytest.mark.asyncio
async def test_udp_sender_accepts_bytes_like_payloads() -> None:
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=_get_unused_udp_port())
    )
    try:
        await sender.send(b"bytes")
        await sender.send(bytearray(b"bytearray"))
        await sender.send(memoryview(b"memoryview"))
    finally:
        await sender.stop()


@pytest.mark.asyncio
async def test_udp_sender_stop_before_first_send_is_safe_and_send_after_stop_fails() -> None:
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=12345)
    )
    await sender.stop()
    await sender.stop()

    with pytest.raises(UdpSenderStoppedError, match="stopped") as exc_info:
        await sender.send(b"after-stop")
    assert isinstance(exc_info.value, NetworkRuntimeError)


@pytest.mark.asyncio
async def test_udp_sender_validates_payload_and_target() -> None:
    sender = AsyncioUdpSender(settings=UdpSenderSettings())
    try:
        with pytest.raises(TypeError, match="bytes-like"):
            await sender.send("not-bytes")  # type: ignore[arg-type]
        with pytest.raises(UdpInvalidTargetError, match="target host") as exc_info:
            await sender.send(b"ok")
        assert isinstance(exc_info.value, NetworkConfigurationError)

        with pytest.raises(UdpInvalidTargetError, match="target host"):
            await sender.send(b"ok", host="   ", port=9999)

        with pytest.raises(UdpInvalidTargetError, match="target host"):
            await sender.send(b"ok", host=object(), port=9999)  # type: ignore[arg-type]

        with pytest.raises(UdpInvalidTargetError, match="between 1 and 65535") as invalid_port_exc:
            await sender.send(b"ok", host="127.0.0.1", port=70000)
        assert isinstance(invalid_port_exc.value, NetworkConfigurationError)

        with pytest.raises(UdpInvalidTargetError, match="target port"):
            await sender.send(b"ok", host="127.0.0.1", port=9999.0)  # type: ignore[arg-type]
    finally:
        await sender.stop()


@pytest.mark.asyncio
async def test_udp_sender_sets_broadcast_socket_option(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[int, int, object]] = []
    original_setsockopt = socket.socket.setsockopt

    def tracking_setsockopt(self: socket.socket, level: int, optname: int, value) -> None:
        observed.append((level, optname, value))
        return original_setsockopt(self, level, optname, value)

    monkeypatch.setattr(socket.socket, "setsockopt", tracking_setsockopt)

    receiver_port = _get_unused_udp_port()
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(
            default_host="255.255.255.255",
            default_port=receiver_port,
            enable_broadcast=True,
        )
    )
    try:
        with contextlib.suppress(OSError):
            await sender.send(b"broadcast")
    finally:
        await sender.stop()

    assert any(
        level == socket.SOL_SOCKET and optname == socket.SO_BROADCAST
        for level, optname, _ in observed
    )


@pytest.mark.asyncio
async def test_udp_sender_target_resolution_prefers_send_arguments_over_defaults() -> None:
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=11000),
    )
    seen_targets: list[tuple[str, int]] = []

    async def fake_send_nonblocking(sock, payload: bytes, target: tuple[str, int]) -> None:  # type: ignore[no-untyped-def]
        seen_targets.append(target)

    sender._send_nonblocking = fake_send_nonblocking  # type: ignore[method-assign,assignment]
    try:
        await sender.send(b"default")
        await sender.send(b"host-only", host="127.0.0.2")
        await sender.send(b"port-only", port=12000)
        await sender.send(b"both", host="127.0.0.3", port=13000)
    finally:
        await sender.stop()

    assert seen_targets == [
        ("127.0.0.1", 11000),
        ("127.0.0.2", 11000),
        ("127.0.0.1", 12000),
        ("127.0.0.3", 13000),
    ]


@pytest.mark.asyncio
async def test_udp_receiver_stop_does_not_await_dispatcher_while_holding_state_lock() -> None:
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=_get_unused_udp_port()),
        event_handler=NoopHandler(),
    )
    await receiver.start()

    async def dispatcher_stop_requiring_state_lock() -> None:
        async with receiver._state_lock:  # type: ignore[attr-defined]
            return None

    receiver._event_dispatcher.stop = dispatcher_stop_requiring_state_lock  # type: ignore[method-assign]
    await asyncio.wait_for(receiver.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_udp_receiver_stop_from_starting_state_does_not_emit_closed_event(
    awaitable_recording_event_handler,
) -> None:
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=_get_unused_udp_port()),
        event_handler=awaitable_recording_event_handler,
    )

    receiver._runtime.lifecycle_state = ComponentLifecycleState.STARTING  # type: ignore[attr-defined]
    receiver._runtime.connection_state = ConnectionState.CREATED  # type: ignore[attr-defined]

    await receiver.stop()

    assert awaitable_recording_event_handler.closed_events == []


@pytest.mark.asyncio
async def test_udp_receiver_stop_emits_stopping_closed_stopped_in_order(
    awaitable_recording_event_handler,
) -> None:
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=_get_unused_udp_port()),
        event_handler=awaitable_recording_event_handler,
    )
    await receiver.start()
    await receiver.stop()

    stop_relevant_events = [
        event
        for event in awaitable_recording_event_handler.events
        if (
            isinstance(event, ConnectionClosedEvent)
            or (
                isinstance(event, ComponentLifecycleChangedEvent)
                and event.current
                in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
            )
        )
    ]
    assert [type(event).__name__ for event in stop_relevant_events] == [
        "ComponentLifecycleChangedEvent",
        "ConnectionClosedEvent",
        "ComponentLifecycleChangedEvent",
    ]
    assert isinstance(stop_relevant_events[0], ComponentLifecycleChangedEvent)
    assert stop_relevant_events[0].current == ComponentLifecycleState.STOPPING
    assert isinstance(stop_relevant_events[2], ComponentLifecycleChangedEvent)
    assert stop_relevant_events[2].current == ComponentLifecycleState.STOPPED


@pytest.mark.asyncio
async def test_udp_receiver_overlapping_stop_calls_publish_one_stop_sequence() -> None:
    handler = BlockingStoppingHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=_get_unused_udp_port()),
        event_handler=handler,
    )
    first_stop_task: asyncio.Task[None] | None = None
    second_stop_task: asyncio.Task[None] | None = None
    try:
        await receiver.start()
        original_task = receiver._task  # type: ignore[attr-defined]

        first_stop_task = asyncio.create_task(receiver.stop())
        await asyncio.wait_for(handler.stopping_seen.wait(), timeout=1.0)

        second_stop_task = asyncio.create_task(receiver.stop())
        await asyncio.sleep(0)
        assert not second_stop_task.done()

        handler.release_stopping.set()
        await asyncio.wait_for(
            asyncio.gather(first_stop_task, second_stop_task),
            timeout=1.0,
        )

        stop_lifecycle_events = [
            event
            for event in handler.events
            if (
                isinstance(event, ComponentLifecycleChangedEvent)
                and event.current
                in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
            )
        ]
        closed_events = [
            event for event in handler.events if isinstance(event, ConnectionClosedEvent)
        ]
        assert [event.current for event in stop_lifecycle_events] == [
            ComponentLifecycleState.STOPPING,
            ComponentLifecycleState.STOPPED,
        ]
        assert len(closed_events) == 1
        assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
        assert receiver._socket is None  # type: ignore[attr-defined]
        assert receiver._task is None  # type: ignore[attr-defined]
        assert original_task is not None and original_task.done()
        assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]
    finally:
        handler.release_stopping.set()
        for task in (first_stop_task, second_stop_task):
            if task is not None and not task.done():
                task.cancel()
                await drain_awaitable_ignoring_cancelled(task)
        await receiver.stop()


@pytest.mark.asyncio
async def test_udp_receiver_cancelled_waiting_stop_does_not_cancel_owner_waiter() -> None:
    handler = BlockingStoppingHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=_get_unused_udp_port()),
        event_handler=handler,
    )
    first_stop_task: asyncio.Task[None] | None = None
    second_stop_task: asyncio.Task[None] | None = None
    third_stop_task: asyncio.Task[None] | None = None
    try:
        await receiver.start()

        first_stop_task = asyncio.create_task(receiver.stop())
        await asyncio.wait_for(handler.stopping_seen.wait(), timeout=1.0)

        second_stop_task = asyncio.create_task(receiver.stop())
        await asyncio.sleep(0)
        second_stop_task.cancel()
        await assert_awaitable_cancelled(second_stop_task)

        third_stop_task = asyncio.create_task(receiver.stop())
        await asyncio.sleep(0)
        assert not third_stop_task.done()

        handler.release_stopping.set()
        await asyncio.wait_for(
            asyncio.gather(first_stop_task, third_stop_task),
            timeout=1.0,
        )

        stop_lifecycle_events = [
            event
            for event in handler.events
            if (
                isinstance(event, ComponentLifecycleChangedEvent)
                and event.current
                in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
            )
        ]
        closed_events = [
            event for event in handler.events if isinstance(event, ConnectionClosedEvent)
        ]
        assert [event.current for event in stop_lifecycle_events] == [
            ComponentLifecycleState.STOPPING,
            ComponentLifecycleState.STOPPED,
        ]
        assert len(closed_events) == 1
        assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
        assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]
    finally:
        handler.release_stopping.set()
        for task in (first_stop_task, second_stop_task, third_stop_task):
            if task is not None and not task.done():
                task.cancel()
                await drain_awaitable_ignoring_cancelled(task)
        await receiver.stop()


@pytest.mark.asyncio
async def test_udp_receiver_inline_stop_reentry_does_not_self_await() -> None:
    handler = StopAgainOnStoppingHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=_get_unused_udp_port(),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=handler,
    )
    handler.receiver = receiver

    await receiver.start()
    await asyncio.wait_for(receiver.stop(), timeout=1.0)

    stop_lifecycle_events = [
        event
        for event in handler.events
        if (
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
        )
    ]
    closed_events = [event for event in handler.events if isinstance(event, ConnectionClosedEvent)]
    assert handler.reentered_stop.is_set()
    assert [event.current for event in stop_lifecycle_events] == [
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    ]
    assert len(closed_events) == 1
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_spawned_stop_from_opened_handler_delivers_terminal_events() -> None:
    handler = SpawnStopOnOpenedHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=_get_unused_udp_port(),
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        ),
        event_handler=handler,
    )
    handler.receiver = receiver
    start_task: asyncio.Task[None] | None = None

    try:
        start_task = asyncio.create_task(receiver.start())
        await asyncio.wait_for(handler.opened_seen.wait(), timeout=1.0)
        await asyncio.wait_for(handler.stop_returned.wait(), timeout=1.0)
        assert not handler.closed_seen.is_set()
        assert not start_task.done()
        assert not any(
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
            for event in handler.events
        )

        handler.release_opened.set()
        assert handler.stop_task is not None
        await asyncio.wait_for(handler.closed_seen.wait(), timeout=1.0)
        await asyncio.wait_for(start_task, timeout=1.0)
    finally:
        handler.release_opened.set()
        if start_task is not None and not start_task.done():
            start_task.cancel()
            await drain_awaitable_ignoring_cancelled(start_task)
        if handler.stop_task is not None and not handler.stop_task.done():
            handler.stop_task.cancel()
            await drain_awaitable_ignoring_cancelled(handler.stop_task)
        await receiver.stop()

    assert handler.error is None
    stop_relevant_events = [
        event
        for event in handler.events
        if isinstance(event, ConnectionClosedEvent)
        or (
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
        )
    ]
    assert [type(event).__name__ for event in stop_relevant_events] == [
        "ComponentLifecycleChangedEvent",
        "ConnectionClosedEvent",
        "ComponentLifecycleChangedEvent",
    ]
    assert isinstance(stop_relevant_events[0], ComponentLifecycleChangedEvent)
    assert stop_relevant_events[0].current == ComponentLifecycleState.STOPPING
    assert isinstance(stop_relevant_events[1], ConnectionClosedEvent)
    assert isinstance(stop_relevant_events[2], ComponentLifecycleChangedEvent)
    assert stop_relevant_events[2].current == ComponentLifecycleState.STOPPED
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_spawned_stop_from_bytes_handler_defers_close_until_handler_returns() -> (
    None
):
    class SpawnStopFromBytesHandler:
        def __init__(self) -> None:
            self.receiver: AsyncioUdpReceiver | None = None
            self.bytes_seen = asyncio.Event()
            self.bytes_finished = asyncio.Event()
            self.closed_seen = asyncio.Event()
            self.stop_returned = asyncio.Event()
            self.release_bytes = asyncio.Event()
            self.stop_task: asyncio.Task[None] | None = None
            self.error: BaseException | None = None

        async def on_event(self, event) -> None:
            if isinstance(event, ConnectionClosedEvent):
                if not self.bytes_finished.is_set():
                    self.error = AssertionError("closed event re-entered bytes handler")
                    self.release_bytes.set()
                self.closed_seen.set()
                return
            if (
                isinstance(event, ComponentLifecycleChangedEvent)
                and event.current
                in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
                and not self.bytes_finished.is_set()
            ):
                self.error = AssertionError("lifecycle event re-entered bytes handler")
                self.release_bytes.set()
                return
            if self.receiver is None or not isinstance(event, BytesReceivedEvent):
                return
            self.bytes_seen.set()
            try:
                self.stop_task = asyncio.create_task(self.receiver.stop())
                await self.stop_task
                self.stop_returned.set()
                if self.closed_seen.is_set():
                    raise AssertionError("closed event was published before bytes handler returned")
                await self.release_bytes.wait()
            except (Exception, asyncio.CancelledError) as error:
                self.error = error
                self.release_bytes.set()
            finally:
                self.bytes_finished.set()

    port = _get_unused_udp_port()
    handler = SpawnStopFromBytesHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=port,
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        ),
        event_handler=handler,
    )
    handler.receiver = receiver
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port)
    )
    try:
        await receiver.start()
        await sender.send(b"stop-from-bytes-handler")
        await asyncio.wait_for(handler.bytes_seen.wait(), timeout=1.0)
        await asyncio.wait_for(handler.stop_returned.wait(), timeout=1.0)
        assert not handler.closed_seen.is_set()

        handler.release_bytes.set()
        await asyncio.wait_for(handler.closed_seen.wait(), timeout=1.0)
    finally:
        handler.release_bytes.set()
        if handler.stop_task is not None and not handler.stop_task.done():
            handler.stop_task.cancel()
            await drain_awaitable_ignoring_cancelled(handler.stop_task)
        await sender.stop()
        await receiver.stop()

    assert handler.error is None
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_inherited_handler_stop_does_not_wait_for_owner() -> None:
    class AwaitChildStopFromBytesHandler:
        def __init__(self) -> None:
            self.receiver: AsyncioUdpReceiver | None = None
            self.bytes_seen = asyncio.Event()
            self.child_stop_returned = asyncio.Event()
            self.bytes_finished = asyncio.Event()
            self.closed_seen = asyncio.Event()
            self.release_bytes = asyncio.Event()
            self.child_stop_task: asyncio.Task[None] | None = None
            self.second_child_stop_returned = asyncio.Event()
            self.second_child_stop_task: asyncio.Task[None] | None = None
            self.error: BaseException | None = None

        async def on_event(self, event) -> None:
            if isinstance(event, ConnectionClosedEvent):
                if not self.bytes_finished.is_set():
                    self.error = AssertionError("closed event re-entered bytes handler")
                    self.release_bytes.set()
                self.closed_seen.set()
                return
            if self.receiver is None or not isinstance(event, BytesReceivedEvent):
                return

            async def child_stop(returned: asyncio.Event) -> None:
                await self.receiver.stop()
                returned.set()

            self.bytes_seen.set()
            try:
                self.child_stop_task = asyncio.create_task(child_stop(self.child_stop_returned))
                await self.child_stop_task
                self.second_child_stop_task = asyncio.create_task(
                    child_stop(self.second_child_stop_returned)
                )
                await self.second_child_stop_task
                await self.release_bytes.wait()
            except (Exception, asyncio.CancelledError) as error:
                self.error = error
                self.release_bytes.set()
            finally:
                self.bytes_finished.set()

    port = _get_unused_udp_port()
    handler = AwaitChildStopFromBytesHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=port,
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        ),
        event_handler=handler,
    )
    handler.receiver = receiver
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port)
    )
    try:
        await receiver.start()
        await sender.send(b"child-stop-from-handler")
        await asyncio.wait_for(handler.bytes_seen.wait(), timeout=1.0)
        await asyncio.wait_for(handler.child_stop_returned.wait(), timeout=1.0)
        await asyncio.wait_for(handler.second_child_stop_returned.wait(), timeout=1.0)
        assert not handler.closed_seen.is_set()

        handler.release_bytes.set()
        await asyncio.wait_for(handler.closed_seen.wait(), timeout=1.0)
    finally:
        handler.release_bytes.set()
        if handler.child_stop_task is not None and not handler.child_stop_task.done():
            handler.child_stop_task.cancel()
            await drain_awaitable_ignoring_cancelled(handler.child_stop_task)
        if handler.second_child_stop_task is not None and not handler.second_child_stop_task.done():
            handler.second_child_stop_task.cancel()
            await drain_awaitable_ignoring_cancelled(handler.second_child_stop_task)
        await sender.stop()
        await receiver.stop()

    assert handler.error is None
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED


@pytest.mark.asyncio
async def test_udp_receiver_handler_origin_stop_drops_queued_background_bytes_before_terminal_events() -> (
    None
):
    class StopFromFirstBytesHandler:
        def __init__(self) -> None:
            self.receiver: AsyncioUdpReceiver | None = None
            self.first_bytes_seen = asyncio.Event()
            self.first_bytes_finished = asyncio.Event()
            self.allow_stop_to_start = asyncio.Event()
            self.allow_first_bytes_to_finish = asyncio.Event()
            self.closed_seen = asyncio.Event()
            self.stop_returned = asyncio.Event()
            self.events: list[object] = []
            self.stop_task: asyncio.Task[None] | None = None
            self.error: BaseException | None = None

        async def on_event(self, event) -> None:
            self.events.append(event)
            if isinstance(event, ConnectionClosedEvent):
                if not self.first_bytes_finished.is_set():
                    self.error = AssertionError("closed event re-entered bytes handler")
                    self.allow_first_bytes_to_finish.set()
                self.closed_seen.set()
                return
            if not isinstance(event, BytesReceivedEvent):
                return
            if event.data == b"second":
                self.error = AssertionError("queued bytes event was delivered after receiver stop")
                self.allow_first_bytes_to_finish.set()
                return
            self.first_bytes_seen.set()
            try:
                await self.allow_stop_to_start.wait()
                if self.receiver is None:
                    raise AssertionError("receiver reference was not attached")
                self.stop_task = asyncio.create_task(self.receiver.stop())
                await self.stop_task
                self.stop_returned.set()
                await self.allow_first_bytes_to_finish.wait()
            except (Exception, asyncio.CancelledError) as error:
                self.error = error
                self.allow_first_bytes_to_finish.set()
            finally:
                self.first_bytes_finished.set()

    port = _get_unused_udp_port()
    handler = StopFromFirstBytesHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=port,
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.BACKGROUND),
        ),
        event_handler=handler,
    )
    handler.receiver = receiver
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port)
    )
    try:
        await receiver.start()
        await sender.send(b"first")
        await asyncio.wait_for(handler.first_bytes_seen.wait(), timeout=1.0)
        await sender.send(b"second")
        await wait_for_condition(
            lambda: receiver._event_dispatcher.runtime_stats.queue_depth == 1,  # type: ignore[attr-defined]
            timeout_seconds=1.0,
        )

        handler.allow_stop_to_start.set()
        await asyncio.wait_for(handler.stop_returned.wait(), timeout=1.0)
        handler.allow_first_bytes_to_finish.set()
        await asyncio.wait_for(handler.closed_seen.wait(), timeout=1.0)
        await asyncio.sleep(0)
    finally:
        handler.allow_stop_to_start.set()
        handler.allow_first_bytes_to_finish.set()
        if handler.stop_task is not None and not handler.stop_task.done():
            handler.stop_task.cancel()
            await drain_awaitable_ignoring_cancelled(handler.stop_task)
        await sender.stop()
        await receiver.stop()

    assert handler.error is None
    assert [event.data for event in handler.events if isinstance(event, BytesReceivedEvent)] == [
        b"first"
    ]
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_external_inline_stop_waits_for_active_bytes_handler() -> None:
    class BlockingBytesHandler:
        def __init__(self) -> None:
            self.bytes_started = asyncio.Event()
            self.bytes_finished = asyncio.Event()
            self.terminal_event_seen = asyncio.Event()
            self.release_bytes = asyncio.Event()
            self.events: list[object] = []
            self.error: BaseException | None = None

        async def on_event(self, event) -> None:
            self.events.append(event)
            if isinstance(event, BytesReceivedEvent):
                self.bytes_started.set()
                try:
                    await self.release_bytes.wait()
                finally:
                    self.bytes_finished.set()
                return
            if isinstance(event, ConnectionClosedEvent) or (
                isinstance(event, ComponentLifecycleChangedEvent)
                and event.current
                in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
            ):
                if not self.bytes_finished.is_set():
                    self.error = AssertionError("terminal event re-entered bytes handler")
                    self.release_bytes.set()
                self.terminal_event_seen.set()

    port = _get_unused_udp_port()
    handler = BlockingBytesHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=port,
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=handler,
    )
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port)
    )
    stop_task: asyncio.Task[None] | None = None
    try:
        await receiver.start()
        await sender.send(b"external-inline-stop")
        await asyncio.wait_for(handler.bytes_started.wait(), timeout=1.0)

        stop_task = asyncio.create_task(receiver.stop())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not handler.terminal_event_seen.is_set()
        assert not handler.bytes_finished.is_set()
        assert not stop_task.done()

        handler.release_bytes.set()
        await asyncio.wait_for(stop_task, timeout=1.0)
        await asyncio.wait_for(handler.terminal_event_seen.wait(), timeout=1.0)
    finally:
        handler.release_bytes.set()
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
            await drain_awaitable_ignoring_cancelled(stop_task)
        await sender.stop()
        await receiver.stop()

    assert handler.error is None
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_cancelled_external_inline_stop_keeps_shared_waiter() -> None:
    class BlockingBytesHandler:
        def __init__(self) -> None:
            self.bytes_started = asyncio.Event()
            self.bytes_finished = asyncio.Event()
            self.terminal_event_seen = asyncio.Event()
            self.release_bytes = asyncio.Event()
            self.error: BaseException | None = None

        async def on_event(self, event) -> None:
            if isinstance(event, BytesReceivedEvent):
                self.bytes_started.set()
                try:
                    await self.release_bytes.wait()
                finally:
                    self.bytes_finished.set()
                return
            if isinstance(event, ConnectionClosedEvent) or (
                isinstance(event, ComponentLifecycleChangedEvent)
                and event.current
                in (ComponentLifecycleState.STOPPING, ComponentLifecycleState.STOPPED)
            ):
                if not self.bytes_finished.is_set():
                    self.error = AssertionError("terminal event re-entered bytes handler")
                    self.release_bytes.set()
                self.terminal_event_seen.set()

    port = _get_unused_udp_port()
    handler = BlockingBytesHandler()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=port,
            event_delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        ),
        event_handler=handler,
    )
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port)
    )
    stop_task: asyncio.Task[None] | None = None
    joiner_stop_task: asyncio.Task[None] | None = None
    try:
        await receiver.start()
        await sender.send(b"cancel-external-inline-stop")
        await asyncio.wait_for(handler.bytes_started.wait(), timeout=1.0)

        stop_task = asyncio.create_task(receiver.stop())
        await wait_for_condition(
            lambda: receiver.lifecycle_state == ComponentLifecycleState.STOPPING,
            timeout_seconds=1.0,
        )
        assert not handler.terminal_event_seen.is_set()

        stop_task.cancel()
        await assert_awaitable_cancelled(stop_task)

        joiner_stop_task = asyncio.create_task(receiver.stop())
        await asyncio.sleep(0)
        assert not joiner_stop_task.done()

        handler.release_bytes.set()
        await asyncio.wait_for(joiner_stop_task, timeout=1.0)
        await asyncio.wait_for(handler.terminal_event_seen.wait(), timeout=1.0)
    finally:
        handler.release_bytes.set()
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
            await drain_awaitable_ignoring_cancelled(stop_task)
        if joiner_stop_task is not None and not joiner_stop_task.done():
            joiner_stop_task.cancel()
            await drain_awaitable_ignoring_cancelled(joiner_stop_task)
        await sender.stop()
        await receiver.stop()

    assert handler.error is None
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._event_dispatcher.is_running is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_receiver_rejects_illegal_lifecycle_transition() -> None:
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=_get_unused_udp_port()),
        event_handler=NoopHandler(),
    )

    with pytest.raises(RuntimeError, match="Illegal lifecycle transition"):
        await receiver._set_lifecycle_state(ComponentLifecycleState.RUNNING)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_udp_sender_closes_socket_when_bind_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Socket created before bind() must be closed if bind() raises OSError."""
    closed_sockets: list[socket.socket] = []
    original_socket_cls = socket.socket

    class TrackingSocket(original_socket_cls):  # type: ignore[misc]
        def bind(self, address: tuple[str, int]) -> None:
            raise OSError("bind-failed")

        def close(self) -> None:
            closed_sockets.append(self)
            super().close()

    monkeypatch.setattr(udp_sender_module.socket, "socket", TrackingSocket)

    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=12345)
    )
    with pytest.raises(OSError, match="bind-failed"):
        await sender.send(b"trigger-socket-creation")

    assert len(closed_sockets) == 1, "Socket must be closed when bind() fails"
    # After a failed initialization the sender must not hold a socket reference.
    assert not sender.is_socket_initialized


@pytest.mark.asyncio
async def test_udp_sender_refreshes_connection_id_after_ephemeral_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_socket_cls = socket.socket

    class TrackingSocket(original_socket_cls):  # type: ignore[misc]
        def bind(self, address: tuple[str, int]) -> None:
            self._bound_address = ("127.0.0.1", 30123)

        def getsockname(self) -> tuple[str, int]:
            return self._bound_address

    monkeypatch.setattr(udp_sender_module.socket, "socket", TrackingSocket)

    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(
            default_host="127.0.0.1",
            default_port=12345,
            local_host="0.0.0.0",
            local_port=0,
        )
    )

    sock = sender._ensure_socket()  # type: ignore[attr-defined]
    try:
        assert sender._connection_id == "udp/sender/127.0.0.1/30123"  # type: ignore[attr-defined]
        assert sender._logger.extra["connection_id"] == "udp/sender/127.0.0.1/30123"  # type: ignore[attr-defined]
        assert sock is not None
    finally:
        await sender.stop()


@pytest.mark.asyncio
async def test_udp_sender_closes_socket_when_setsockopt_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Socket created before setsockopt() must be closed if setsockopt() raises OSError."""
    closed_sockets: list[socket.socket] = []
    original_socket_cls = socket.socket

    class TrackingSocket(original_socket_cls):  # type: ignore[misc]
        def setsockopt(self, level: int, optname: int, value: int) -> None:
            raise OSError("setsockopt-failed")

        def close(self) -> None:
            closed_sockets.append(self)
            super().close()

    monkeypatch.setattr(udp_sender_module.socket, "socket", TrackingSocket)

    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(
            default_host="127.0.0.1", default_port=12345, enable_broadcast=True
        )
    )
    with pytest.raises(OSError, match="setsockopt-failed"):
        await sender.send(b"trigger-socket-creation")

    assert len(closed_sockets) == 1, "Socket must be closed when setsockopt() fails"
    # After a failed initialization the sender must not hold a socket reference.
    assert not sender.is_socket_initialized
