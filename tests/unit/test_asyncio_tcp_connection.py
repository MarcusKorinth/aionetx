"""
Contract tests for AsyncioTcpConnection.

This module focuses on the per-connection behavior beneath the TCP client and
server transports: opened/received ordering, send and close semantics,
metadata exposure, and teardown idempotency under cancellation or concurrency.
Regressions here would surface in every TCP role, so the close-path edge cases
are covered in detail.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from aionetx.api.errors import ConnectionClosedError
from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.connection_events import ConnectionClosedEvent
from aionetx.api.connection_events import ConnectionOpenedEvent
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api.event_delivery_settings import (
    EventBackpressurePolicy,
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
)
from aionetx.implementations.asyncio_impl.asyncio_tcp_connection import AsyncioTcpConnection
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher
from tests.helpers import wait_for_condition


def make_dispatcher(handler) -> AsyncioEventDispatcher:
    """Construct the default dispatcher used by connection-focused tests."""

    return AsyncioEventDispatcher(
        event_handler=handler,
        delivery=EventDeliverySettings(),
        logger=logging.getLogger("test"),
    )


async def _no_op_server_handler(
    _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Accept a connection and close it immediately."""

    writer.close()
    await writer.wait_closed()


class _DummyWriter:
    def __init__(self) -> None:
        self.closed = False
        self.closed_event = asyncio.Event()

    def get_extra_info(self, key: str):
        if key == "sockname":
            return ("127.0.0.1", 10001)
        if key == "peername":
            return ("127.0.0.1", 10002)
        return None

    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        self.closed_event.set()

    async def wait_closed(self) -> None:
        return None


# Startup, receive ordering, and basic send behavior.
@pytest.mark.asyncio
async def test_tcp_connection_receives_and_closes(recording_event_handler) -> None:
    received_data: list[bytes] = []

    async def server_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"hello")
        await writer.drain()
        received_data.append(await reader.read(4096))
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        dispatcher = make_dispatcher(recording_event_handler)
        await dispatcher.start()
        connection = AsyncioTcpConnection(
            "client:test", ConnectionRole.CLIENT, reader, writer, dispatcher, 4096
        )
        await connection.start()
        await connection.send(b"ping")
        await wait_for_condition(
            lambda: bool(recording_event_handler.received_events and received_data),
            timeout_seconds=1.0,
        )
        await connection.close()
        await dispatcher.stop()

    assert received_data == [b"ping"]
    assert recording_event_handler.opened_events
    assert recording_event_handler.received_events
    assert connection.state == ConnectionState.CLOSED


@pytest.mark.asyncio
async def test_tcp_connection_invariant_connection_opened_precedes_bytes_received(
    recording_event_handler,
) -> None:
    async def server_handler(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"hello")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        dispatcher = make_dispatcher(recording_event_handler)
        await dispatcher.start()
        connection = AsyncioTcpConnection(
            "client:ordering", ConnectionRole.CLIENT, reader, writer, dispatcher, 4096
        )
        await connection.start()
        await wait_for_condition(
            lambda: bool(recording_event_handler.received_events), timeout_seconds=1.0
        )
        await connection.close()
        await dispatcher.stop()

    event_names = [
        type(event).__name__
        for event in recording_event_handler.events
        if isinstance(event, (ConnectionOpenedEvent, BytesReceivedEvent))
    ]
    assert event_names[:2] == ["ConnectionOpenedEvent", "BytesReceivedEvent"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dispatch_mode",
    [EventDispatchMode.INLINE, EventDispatchMode.BACKGROUND],
)
async def test_tcp_connection_dispatch_waits_for_opened_before_reading(
    dispatch_mode: EventDispatchMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingOpenedHandler:
        def __init__(self) -> None:
            self.opened_started = asyncio.Event()
            self.opened_finished = asyncio.Event()
            self.allow_opened_to_finish = asyncio.Event()
            self.bytes_started = asyncio.Event()
            self.active_handlers = 0
            self.max_active_handlers = 0

        async def on_event(self, event) -> None:
            is_opened_event = isinstance(event, ConnectionOpenedEvent)
            self.active_handlers += 1
            self.max_active_handlers = max(self.max_active_handlers, self.active_handlers)
            try:
                if is_opened_event:
                    self.opened_started.set()
                    await self.allow_opened_to_finish.wait()
                elif isinstance(event, BytesReceivedEvent):
                    self.bytes_started.set()
            finally:
                self.active_handlers -= 1
                if is_opened_event:
                    self.opened_finished.set()

    handler = BlockingOpenedHandler()
    read_task_created = asyncio.Event()
    original_create_task = asyncio.create_task

    def tracking_create_task(coro, *args, **kwargs):
        coro_name = getattr(getattr(coro, "cr_code", None), "co_name", "")
        task = original_create_task(coro, *args, **kwargs)
        if coro_name == "_read_loop":
            read_task_created.set()
        return task

    dispatcher = AsyncioEventDispatcher(
        event_handler=handler,
        delivery=EventDeliverySettings(dispatch_mode=dispatch_mode),
        logger=logging.getLogger("test"),
    )
    reader = asyncio.StreamReader()
    reader.feed_data(b"hello")
    connection = AsyncioTcpConnection(
        "client:inline-ordering",
        ConnectionRole.CLIENT,
        reader,
        _DummyWriter(),  # type: ignore[arg-type]
        dispatcher,
        4096,
    )

    await dispatcher.start()
    monkeypatch.setattr(asyncio, "create_task", tracking_create_task)
    start_task = asyncio.create_task(connection.start())
    try:
        await asyncio.wait_for(handler.opened_started.wait(), timeout=1.0)

        assert not read_task_created.is_set()
        assert not handler.bytes_started.is_set()
        assert handler.max_active_handlers == 1

        handler.allow_opened_to_finish.set()
        await asyncio.wait_for(handler.opened_finished.wait(), timeout=1.0)
        await asyncio.wait_for(start_task, timeout=1.0)
        await asyncio.wait_for(read_task_created.wait(), timeout=1.0)
        await asyncio.wait_for(handler.bytes_started.wait(), timeout=1.0)
        assert handler.max_active_handlers == 1
    finally:
        handler.allow_opened_to_finish.set()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(start_task, timeout=1.0)
        await connection.close()
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_tcp_connection_starts_read_loop_after_opened_event_publication(
    recording_event_handler, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_state_at_task_creation: list[ConnectionState] = []
    seen_opened_emit_completed_at_task_creation: list[bool] = []

    original_create_task = asyncio.create_task

    def tracking_create_task(coro, *args, **kwargs):
        coro_name = getattr(getattr(coro, "cr_code", None), "co_name", "")
        if coro_name == "_read_loop":
            seen_state_at_task_creation.append(connection.state)
            seen_opened_emit_completed_at_task_creation.append(opened_emit_completed)
        return original_create_task(coro, *args, **kwargs)

    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    original_emit_and_wait = dispatcher.emit_and_wait
    opened_emit_completed = False

    async def tracking_emit_and_wait(event, **kwargs):
        nonlocal opened_emit_completed
        await original_emit_and_wait(event, **kwargs)
        if isinstance(event, ConnectionOpenedEvent):
            opened_emit_completed = True

    connection = AsyncioTcpConnection(
        "client:test-connecting",
        ConnectionRole.CLIENT,
        asyncio.StreamReader(),
        _DummyWriter(),  # type: ignore[arg-type]
        dispatcher,
        4096,
    )

    monkeypatch.setattr(asyncio, "create_task", tracking_create_task)
    monkeypatch.setattr(dispatcher, "emit_and_wait", tracking_emit_and_wait)

    await connection.start()
    await connection.close()
    await dispatcher.stop()

    assert seen_state_at_task_creation
    assert seen_state_at_task_creation[0] == ConnectionState.CONNECTED
    assert seen_opened_emit_completed_at_task_creation == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [EventBackpressurePolicy.DROP_NEWEST, EventBackpressurePolicy.DROP_OLDEST],
)
async def test_tcp_connection_opened_barrier_is_not_dropped_by_background_backpressure(
    policy: EventBackpressurePolicy,
) -> None:
    blocker_started = asyncio.Event()
    allow_blocker_to_finish = asyncio.Event()
    opened_seen = asyncio.Event()

    class BlockingHandler:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def on_event(self, event) -> None:
            if isinstance(event, BytesReceivedEvent):
                self.events.append(event.resource_id)
                if event.resource_id == "blocker":
                    blocker_started.set()
                    await allow_blocker_to_finish.wait()
            elif isinstance(event, ConnectionOpenedEvent):
                self.events.append("opened")
                opened_seen.set()

    handler = BlockingHandler()
    dispatcher = AsyncioEventDispatcher(
        event_handler=handler,
        delivery=EventDeliverySettings(
            dispatch_mode=EventDispatchMode.BACKGROUND,
            max_pending_events=1,
            backpressure_policy=policy,
        ),
        logger=logging.getLogger("test"),
    )
    connection = AsyncioTcpConnection(
        "client:opened-barrier-not-dropped",
        ConnectionRole.CLIENT,
        asyncio.StreamReader(),
        _DummyWriter(),  # type: ignore[arg-type]
        dispatcher,
        4096,
    )

    await dispatcher.start()
    start_task: asyncio.Task[None] | None = None
    try:
        await dispatcher.emit(BytesReceivedEvent(resource_id="blocker", data=b""))
        await asyncio.wait_for(blocker_started.wait(), timeout=1.0)
        await dispatcher.emit(BytesReceivedEvent(resource_id="queued", data=b""))
        await wait_for_condition(
            lambda: dispatcher.runtime_stats.queue_depth == 1,
            timeout_seconds=1.0,
        )

        start_task = asyncio.create_task(connection.start())
        await asyncio.sleep(0)
        assert not start_task.done()
        assert dispatcher.runtime_stats.queue_depth == 1

        allow_blocker_to_finish.set()
        await asyncio.wait_for(start_task, timeout=1.0)
        await asyncio.wait_for(opened_seen.wait(), timeout=1.0)
    finally:
        allow_blocker_to_finish.set()
        if start_task is not None and not start_task.done():
            start_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(start_task, timeout=1.0)
        if connection.state != ConnectionState.CLOSED:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await connection.close()
        await dispatcher.stop()

    if policy == EventBackpressurePolicy.DROP_OLDEST:
        assert handler.events == ["blocker", "opened"]
    else:
        assert handler.events == ["blocker", "queued", "opened"]


@pytest.mark.asyncio
async def test_tcp_connection_close_during_ready_callback_does_not_emit_late_opened() -> None:
    ready_started = asyncio.Event()
    allow_ready_to_finish = asyncio.Event()

    class RecordingHandler:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def on_event(self, event) -> None:
            if isinstance(event, ConnectionOpenedEvent):
                self.events.append("opened")
            elif isinstance(event, ConnectionClosedEvent):
                self.events.append("closed")

    async def on_ready(_connection: AsyncioTcpConnection) -> None:
        ready_started.set()
        await allow_ready_to_finish.wait()

    handler = RecordingHandler()
    dispatcher = AsyncioEventDispatcher(
        event_handler=handler,
        delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        logger=logging.getLogger("test"),
    )
    writer = _DummyWriter()
    connection = AsyncioTcpConnection(
        "client:close-during-ready",
        ConnectionRole.CLIENT,
        asyncio.StreamReader(),
        writer,  # type: ignore[arg-type]
        dispatcher,
        4096,
        on_ready_callback=on_ready,
    )

    await dispatcher.start()
    start_task = asyncio.create_task(connection.start())
    try:
        await asyncio.wait_for(ready_started.wait(), timeout=1.0)
        await connection.close()
        allow_ready_to_finish.set()
        await asyncio.wait_for(start_task, timeout=1.0)
    finally:
        allow_ready_to_finish.set()
        if not start_task.done():
            start_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(start_task, timeout=1.0)
        await dispatcher.stop()

    assert handler.events == ["closed"]
    assert connection.state == ConnectionState.CLOSED
    assert writer.closed


@pytest.mark.asyncio
async def test_tcp_connection_send_on_closed_connection_raises(recording_event_handler) -> None:
    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        dispatcher = make_dispatcher(recording_event_handler)
        await dispatcher.start()
        connection = AsyncioTcpConnection(
            "client:test", ConnectionRole.CLIENT, reader, writer, dispatcher, 4096
        )
        await connection.start()
        await connection.close()
        await dispatcher.stop()
        with pytest.raises(ConnectionClosedError):
            await connection.send(b"test")


@pytest.mark.asyncio
async def test_tcp_connection_send_rejects_non_bytes_payload(recording_event_handler) -> None:
    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        dispatcher = make_dispatcher(recording_event_handler)
        await dispatcher.start()
        connection = AsyncioTcpConnection(
            "client:test", ConnectionRole.CLIENT, reader, writer, dispatcher, 4096
        )
        await connection.start()
        with pytest.raises(TypeError, match="bytes-like"):
            await connection.send("bad-payload")  # type: ignore[arg-type]
        await connection.close()
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_tcp_connection_send_surfaces_writer_drain_failure(recording_event_handler) -> None:
    class DummyWriter:
        def get_extra_info(self, key: str):
            if key == "sockname":
                return ("127.0.0.1", 10001)
            if key == "peername":
                return ("127.0.0.1", 10002)
            return None

        def write(self, data: bytes) -> None:
            return None

        async def drain(self) -> None:
            raise ConnectionResetError("peer reset")

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    connection = AsyncioTcpConnection(
        "client:drain-failure",
        ConnectionRole.CLIENT,
        asyncio.StreamReader(),
        DummyWriter(),  # type: ignore[arg-type]
        dispatcher,
        1024,
    )
    await connection.start()
    with pytest.raises(ConnectionResetError, match="peer reset"):
        await connection.send(b"payload")
    await connection.close()
    await dispatcher.stop()


@pytest.mark.asyncio
async def test_tcp_connection_send_times_out_when_writer_drain_stalls(
    recording_event_handler,
) -> None:
    drain_started = asyncio.Event()

    class BlockingDrainWriter:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def get_extra_info(self, key: str):
            if key == "sockname":
                return ("127.0.0.1", 10001)
            if key == "peername":
                return ("127.0.0.1", 10002)
            return None

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            drain_started.set()
            await asyncio.Event().wait()

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    writer = BlockingDrainWriter()
    connection = AsyncioTcpConnection(
        "client:send-timeout",
        ConnectionRole.CLIENT,
        asyncio.StreamReader(),
        writer,  # type: ignore[arg-type]
        dispatcher,
        1024,
        send_timeout_seconds=0.01,
    )
    await connection.start()

    with pytest.raises(asyncio.TimeoutError):
        await connection.send(b"payload")

    assert writer.writes == [b"payload"]
    assert drain_started.is_set()
    await connection.close()
    await dispatcher.stop()


@pytest.mark.asyncio
async def test_tcp_connection_send_timeout_none_waits_for_drain_completion(
    recording_event_handler,
) -> None:
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()

    class BlockingDrainWriter:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def get_extra_info(self, key: str):
            if key == "sockname":
                return ("127.0.0.1", 10001)
            if key == "peername":
                return ("127.0.0.1", 10002)
            return None

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            drain_started.set()
            await release_drain.wait()

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    writer = BlockingDrainWriter()
    connection = AsyncioTcpConnection(
        "client:send-timeout-disabled",
        ConnectionRole.CLIENT,
        asyncio.StreamReader(),
        writer,  # type: ignore[arg-type]
        dispatcher,
        1024,
        send_timeout_seconds=None,
    )
    await connection.start()
    send_task = asyncio.create_task(connection.send(b"payload"))

    try:
        await asyncio.wait_for(drain_started.wait(), timeout=1.0)
        await asyncio.sleep(0)

        assert send_task.done() is False

        release_drain.set()
        assert await asyncio.wait_for(send_task, timeout=1.0) is None
        assert writer.writes == [b"payload"]
    finally:
        release_drain.set()
        if not send_task.done():
            send_task.cancel()
        await connection.close()
        await dispatcher.stop()


@pytest.mark.parametrize(
    "send_timeout_seconds",
    [
        0,
        -1,
        float("nan"),
        float("inf"),
        True,
        "1.0",
        pytest.param(object(), id="object"),
    ],
)
@pytest.mark.asyncio
async def test_tcp_connection_rejects_invalid_send_timeout(
    recording_event_handler,
    send_timeout_seconds: object,
) -> None:
    with pytest.raises(ValueError, match="send_timeout_seconds"):
        AsyncioTcpConnection(
            "client:invalid-send-timeout",
            ConnectionRole.CLIENT,
            asyncio.StreamReader(),
            _FakeWriter(),  # type: ignore[arg-type]
            make_dispatcher(recording_event_handler),
            1024,
            send_timeout_seconds=send_timeout_seconds,
        )


@pytest.mark.asyncio
async def test_tcp_connection_keeps_positional_on_closed_callback_compatibility(
    recording_event_handler,
) -> None:
    callback_called = asyncio.Event()

    async def on_closed(_connection: AsyncioTcpConnection) -> None:
        callback_called.set()

    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    connection = AsyncioTcpConnection(
        "client:positional-close-callback",
        ConnectionRole.CLIENT,
        asyncio.StreamReader(),
        _FakeWriter(),  # type: ignore[arg-type]
        dispatcher,
        1024,
        None,
        on_closed,
    )

    try:
        await connection.start()
        await connection.close()
        await asyncio.wait_for(callback_called.wait(), timeout=1.0)
    finally:
        await connection.close()
        await dispatcher.stop()


# Metadata parsing and read-loop failure handling.
def test_tcp_connection_metadata_parsing_tolerates_non_integer_port_values(
    recording_event_handler,
) -> None:
    class DummyReader:
        async def read(self, _size: int) -> bytes:
            return b""

    class DummyWriter:
        def get_extra_info(self, key: str):
            if key == "sockname":
                return ("127.0.0.1", "not-an-int")
            if key == "peername":
                return ("127.0.0.1", object())
            return None

        def write(self, data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    dispatcher = make_dispatcher(recording_event_handler)
    connection = AsyncioTcpConnection(
        "client:metadata",
        ConnectionRole.CLIENT,
        DummyReader(),  # type: ignore[arg-type]
        DummyWriter(),  # type: ignore[arg-type]
        dispatcher,
        1024,
    )

    assert connection.metadata.local_port is None
    assert connection.metadata.remote_port is None


@pytest.mark.asyncio
async def test_tcp_connection_read_loop_error_emits_event_and_closes(
    recording_event_handler,
) -> None:
    class FaultyReader:
        def __init__(self) -> None:
            self._calls = 0

        async def read(self, size: int) -> bytes:
            self._calls += 1
            if self._calls == 1:
                return b"first-chunk"
            raise RuntimeError("reader-crashed")

    class DummyWriter:
        def __init__(self) -> None:
            self._closed = False

        def get_extra_info(self, key: str):
            if key == "sockname":
                return ("127.0.0.1", 10001)
            if key == "peername":
                return ("127.0.0.1", 10002)
            return None

        def write(self, data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self._closed = True

        async def wait_closed(self) -> None:
            return None

    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    connection = AsyncioTcpConnection(
        "client:faulty-reader",
        ConnectionRole.CLIENT,
        FaultyReader(),  # type: ignore[arg-type]
        DummyWriter(),  # type: ignore[arg-type]
        dispatcher,
        1024,
    )
    await connection.start()

    await wait_for_condition(
        lambda: bool(recording_event_handler.received_events), timeout_seconds=1.0
    )
    await wait_for_condition(
        lambda: bool(recording_event_handler.error_events), timeout_seconds=1.0
    )
    await wait_for_condition(
        lambda: connection.state == ConnectionState.CLOSED, timeout_seconds=1.0
    )
    await dispatcher.stop()

    assert recording_event_handler.received_events[0].data == b"first-chunk"
    assert isinstance(recording_event_handler.error_events[-1].error, RuntimeError)


# Close-path publication, callback behavior, and idempotency.
@pytest.mark.asyncio
async def test_tcp_connection_close_callback_failure_emits_error_and_closed_once(
    recording_event_handler,
) -> None:
    close_callback_calls = 0

    async def on_closed(_connection: AsyncioTcpConnection) -> None:
        nonlocal close_callback_calls
        close_callback_calls += 1
        raise RuntimeError("close-callback-failed")

    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        dispatcher = make_dispatcher(recording_event_handler)
        await dispatcher.start()
        connection = AsyncioTcpConnection(
            "client:test-close-callback",
            ConnectionRole.CLIENT,
            reader,
            writer,
            dispatcher,
            4096,
            on_closed_callback=on_closed,
        )
        await connection.start()
        await connection.close()
        await dispatcher.stop()

    assert close_callback_calls == 1
    assert (
        len(
            [
                event
                for event in recording_event_handler.events
                if isinstance(event, NetworkErrorEvent)
            ]
        )
        == 1
    )
    assert (
        len(
            [
                event
                for event in recording_event_handler.events
                if isinstance(event, ConnectionClosedEvent)
            ]
        )
        == 1
    )
    assert connection.state == ConnectionState.CLOSED


@pytest.mark.asyncio
async def test_tcp_connection_close_inline_stop_component_on_closed_event_does_not_deadlock() -> (
    None
):
    observed_events: list[object] = []
    stop_calls = 0

    class FailingOnClosedHandler:
        async def on_event(self, event) -> None:
            observed_events.append(event)
            if isinstance(event, ConnectionClosedEvent):
                raise RuntimeError("closed-handler-failed")

    settings = EventDeliverySettings(
        dispatch_mode=EventDispatchMode.INLINE,
        handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
    )
    dispatcher = AsyncioEventDispatcher(
        event_handler=FailingOnClosedHandler(),
        delivery=settings,
        logger=logging.getLogger("test"),
    )

    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        connection = AsyncioTcpConnection(
            "client:no-deadlock", ConnectionRole.CLIENT, reader, writer, dispatcher, 4096
        )

        async def stop_component() -> None:
            nonlocal stop_calls
            stop_calls += 1
            await connection.close()

        dispatcher._stop_component_callback = stop_component  # type: ignore[attr-defined]

        await connection.start()
        await asyncio.wait_for(connection.close(), timeout=1.0)

    assert connection.state == ConnectionState.CLOSED
    assert stop_calls == 1
    assert any(isinstance(event, ConnectionClosedEvent) for event in observed_events)


@pytest.mark.asyncio
async def test_tcp_connection_close_callback_runs_before_connection_closed_event() -> None:
    ordering: list[str] = []

    class OrderingHandler:
        async def on_event(self, event) -> None:
            if isinstance(event, ConnectionClosedEvent):
                ordering.append("event")

    async def on_closed(_connection: AsyncioTcpConnection) -> None:
        ordering.append("callback")

    dispatcher = AsyncioEventDispatcher(
        event_handler=OrderingHandler(),
        delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        logger=logging.getLogger("test"),
    )

    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        connection = AsyncioTcpConnection(
            "client:ordering",
            ConnectionRole.CLIENT,
            reader,
            writer,
            dispatcher,
            4096,
            on_closed_callback=on_closed,
        )
        await connection.start()
        await connection.close()

    assert ordering == ["callback", "event"]


@pytest.mark.asyncio
async def test_tcp_connection_close_retries_closed_event_publication_after_handler_exception() -> (
    None
):
    close_handler_calls = 0
    callback_calls = 0

    class FailFirstClosedEventHandler:
        async def on_event(self, event) -> None:
            nonlocal close_handler_calls
            if isinstance(event, ConnectionClosedEvent):
                close_handler_calls += 1
                if close_handler_calls == 1:
                    raise RuntimeError("first-close-event-fails")

    async def on_closed(_connection: AsyncioTcpConnection) -> None:
        nonlocal callback_calls
        callback_calls += 1

    dispatcher = AsyncioEventDispatcher(
        event_handler=FailFirstClosedEventHandler(),
        delivery=EventDeliverySettings(
            dispatch_mode=EventDispatchMode.INLINE,
            handler_failure_policy=EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE,
        ),
        logger=logging.getLogger("test"),
    )

    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        connection = AsyncioTcpConnection(
            "client:retry-close-publication",
            ConnectionRole.CLIENT,
            reader,
            writer,
            dispatcher,
            4096,
            on_closed_callback=on_closed,
        )
        await connection.start()

        with pytest.raises(RuntimeError, match="first-close-event-fails"):
            await connection.close()

        assert connection.state == ConnectionState.CLOSED
        assert connection._closed_event_published is False  # type: ignore[attr-defined]

        await connection.close()

    assert callback_calls == 1
    assert close_handler_calls == 2
    assert connection._closed_event_published is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_tcp_connection_close_handler_cancelled_error_does_not_mark_publication_success() -> (
    None
):
    closed_handler_calls = 0

    class CancelFirstClosedEventHandler:
        async def on_event(self, event) -> None:
            nonlocal closed_handler_calls
            if isinstance(event, ConnectionClosedEvent):
                closed_handler_calls += 1
                if closed_handler_calls == 1:
                    raise asyncio.CancelledError

    dispatcher = AsyncioEventDispatcher(
        event_handler=CancelFirstClosedEventHandler(),
        delivery=EventDeliverySettings(
            dispatch_mode=EventDispatchMode.INLINE,
            handler_failure_policy=EventHandlerFailurePolicy.RAISE_IN_INLINE_MODE,
        ),
        logger=logging.getLogger("test"),
    )

    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        connection = AsyncioTcpConnection(
            "client:cancelled-close-event-publication",
            ConnectionRole.CLIENT,
            reader,
            writer,
            dispatcher,
            4096,
        )
        await connection.start()

        with pytest.raises(
            RuntimeError, match="Network event handler raised asyncio.CancelledError"
        ):
            await connection.close()

        assert connection.state == ConnectionState.CLOSED
        assert connection._closed_event_published is False  # type: ignore[attr-defined]

        await connection.close()

    assert closed_handler_calls == 2
    assert connection._closed_event_published is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_tcp_connection_close_cancellation_during_closed_event_emit_still_emits_once() -> (
    None
):
    closed_event_started = asyncio.Event()
    allow_closed_event_handler = asyncio.Event()
    closed_event_count = 0

    class BlockingOnClosedEventHandler:
        async def on_event(self, event) -> None:
            nonlocal closed_event_count
            if isinstance(event, ConnectionClosedEvent):
                closed_event_count += 1
                closed_event_started.set()
                await allow_closed_event_handler.wait()

    dispatcher = AsyncioEventDispatcher(
        event_handler=BlockingOnClosedEventHandler(),
        delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        logger=logging.getLogger("test"),
    )

    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        connection = AsyncioTcpConnection(
            "client:cancel-during-emit",
            ConnectionRole.CLIENT,
            reader,
            writer,
            dispatcher,
            4096,
        )
        await connection.start()

        close_task = asyncio.create_task(connection.close())
        await asyncio.wait_for(closed_event_started.wait(), timeout=1.0)
        close_task.cancel()
        allow_closed_event_handler.set()

        with pytest.raises(asyncio.CancelledError):
            _ = await close_task

    assert connection.state == ConnectionState.CLOSED
    assert closed_event_count == 1


@pytest.mark.asyncio
async def test_tcp_connection_concurrent_close_waits_for_shared_publication_and_shares_result() -> (
    None
):
    closed_event_started = asyncio.Event()
    allow_closed_event_handler = asyncio.Event()
    closed_event_count = 0

    class BlockingClosedHandler:
        async def on_event(self, event) -> None:
            nonlocal closed_event_count
            if isinstance(event, ConnectionClosedEvent):
                closed_event_count += 1
                closed_event_started.set()
                await allow_closed_event_handler.wait()

    dispatcher = AsyncioEventDispatcher(
        event_handler=BlockingClosedHandler(),
        delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        logger=logging.getLogger("test"),
    )

    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        connection = AsyncioTcpConnection(
            "client:shared-close-publication",
            ConnectionRole.CLIENT,
            reader,
            writer,
            dispatcher,
            4096,
        )
        await connection.start()

        close_task_1 = asyncio.create_task(connection.close())
        await asyncio.wait_for(closed_event_started.wait(), timeout=1.0)
        close_task_2 = asyncio.create_task(connection.close())
        await asyncio.sleep(0)
        assert close_task_2.done() is False

        allow_closed_event_handler.set()
        await asyncio.gather(close_task_1, close_task_2)

    assert closed_event_count == 1
    assert connection._closed_event_published is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_tcp_connection_concurrent_close_during_callback_waits_for_same_close_task(
    recording_event_handler,
) -> None:
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    callback_calls = 0

    async def on_closed(_connection: AsyncioTcpConnection) -> None:
        nonlocal callback_calls
        callback_calls += 1
        callback_started.set()
        await release_callback.wait()

    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        dispatcher = make_dispatcher(recording_event_handler)
        await dispatcher.start()
        connection = AsyncioTcpConnection(
            "client:concurrent-close-callback-phase",
            ConnectionRole.CLIENT,
            reader,
            writer,
            dispatcher,
            4096,
            on_closed_callback=on_closed,
        )
        await connection.start()

        close_task_1 = asyncio.create_task(connection.close())
        await asyncio.wait_for(callback_started.wait(), timeout=1.0)
        close_task_2 = asyncio.create_task(connection.close())
        await asyncio.sleep(0)
        assert close_task_2.done() is False

        release_callback.set()
        await asyncio.gather(close_task_1, close_task_2)
        await dispatcher.stop()

    assert callback_calls == 1
    assert len(recording_event_handler.closed_events) == 1
    assert connection.state == ConnectionState.CLOSED


@pytest.mark.asyncio
async def test_tcp_connection_concurrent_close_during_wait_closed_waits_for_same_close_task() -> (
    None
):
    wait_closed_started = asyncio.Event()
    release_wait_closed = asyncio.Event()
    close_event_count = 0

    class CountingClosedHandler:
        async def on_event(self, event) -> None:
            nonlocal close_event_count
            if isinstance(event, ConnectionClosedEvent):
                close_event_count += 1

    dispatcher = AsyncioEventDispatcher(
        event_handler=CountingClosedHandler(),
        delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        logger=logging.getLogger("test"),
    )

    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        original_wait_closed = writer.wait_closed

        async def blocking_wait_closed() -> None:
            wait_closed_started.set()
            await release_wait_closed.wait()
            await original_wait_closed()

        writer.wait_closed = blocking_wait_closed  # type: ignore[assignment]

        connection = AsyncioTcpConnection(
            "client:concurrent-close-teardown-phase",
            ConnectionRole.CLIENT,
            reader,
            writer,
            dispatcher,
            4096,
        )
        await connection.start()

        close_task_1 = asyncio.create_task(connection.close())
        await asyncio.wait_for(wait_closed_started.wait(), timeout=1.0)
        close_task_2 = asyncio.create_task(connection.close())
        await asyncio.sleep(0)
        assert close_task_2.done() is False

        release_wait_closed.set()
        await asyncio.gather(close_task_1, close_task_2)

    assert close_event_count == 1
    assert connection.state == ConnectionState.CLOSED


@pytest.mark.asyncio
async def test_tcp_connection_close_cancellation_still_reaches_closed_and_emits_event(
    recording_event_handler,
) -> None:
    callback_started = asyncio.Event()
    allow_callback_return = asyncio.Event()
    callback_states: list[ConnectionState] = []

    async def on_closed(connection: AsyncioTcpConnection) -> None:
        callback_states.append(connection.state)
        callback_started.set()
        await allow_callback_return.wait()

    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        dispatcher = make_dispatcher(recording_event_handler)
        await dispatcher.start()
        connection = AsyncioTcpConnection(
            "client:cancel-close",
            ConnectionRole.CLIENT,
            reader,
            writer,
            dispatcher,
            4096,
            on_closed_callback=on_closed,
        )
        await connection.start()

        close_task = asyncio.create_task(connection.close())
        await asyncio.wait_for(callback_started.wait(), timeout=1.0)
        close_task.cancel()
        allow_callback_return.set()
        with pytest.raises(asyncio.CancelledError):
            _ = await close_task

        await wait_for_condition(
            lambda: connection.state == ConnectionState.CLOSED, timeout_seconds=1.0
        )
        await wait_for_condition(
            lambda: bool(recording_event_handler.closed_events), timeout_seconds=1.0
        )
        await dispatcher.stop()

    assert callback_states == [ConnectionState.CLOSING]
    assert len(recording_event_handler.closed_events) == 1


@pytest.mark.asyncio
async def test_tcp_connection_close_is_idempotent_under_concurrency(
    recording_event_handler,
) -> None:
    close_callback_calls = 0

    async def on_closed(_connection: AsyncioTcpConnection) -> None:
        nonlocal close_callback_calls
        close_callback_calls += 1

    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        dispatcher = make_dispatcher(recording_event_handler)
        await dispatcher.start()
        connection = AsyncioTcpConnection(
            "client:test-close-idempotent",
            ConnectionRole.CLIENT,
            reader,
            writer,
            dispatcher,
            4096,
            on_closed_callback=on_closed,
        )
        await connection.start()
        await asyncio.gather(connection.close(), connection.close())
        await dispatcher.stop()

    assert close_callback_calls == 1
    assert len(recording_event_handler.closed_events) == 1
    assert connection.state == ConnectionState.CLOSED


@pytest.mark.asyncio
async def test_tcp_connection_invariant_close_return_is_callback_silent(
    recording_event_handler,
) -> None:
    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        dispatcher = make_dispatcher(recording_event_handler)
        await dispatcher.start()
        connection = AsyncioTcpConnection(
            "client:post-close-silence",
            ConnectionRole.CLIENT,
            reader,
            writer,
            dispatcher,
            4096,
        )
        await connection.start()
        await connection.close()
        event_count_at_close_return = len(recording_event_handler.events)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await dispatcher.stop()

    assert len(recording_event_handler.events) == event_count_at_close_return


# Teardown logging for unexpected close-path failures.
@pytest.mark.asyncio
async def test_tcp_connection_close_logs_unexpected_read_task_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class NoOpHandler:
        async def on_event(self, event: object) -> None:
            return None

    class DummyWriter:
        def get_extra_info(self, key: str):
            if key == "sockname":
                return ("127.0.0.1", 10001)
            if key == "peername":
                return ("127.0.0.1", 10002)
            return None

        def write(self, data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def failing_read_task() -> None:
        raise RuntimeError("read-task-crashed")

    dispatcher = AsyncioEventDispatcher(
        event_handler=NoOpHandler(),
        delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        logger=logging.getLogger("test"),
    )
    connection = AsyncioTcpConnection(
        "client:read-task-log",
        ConnectionRole.CLIENT,
        asyncio.StreamReader(),
        DummyWriter(),  # type: ignore[arg-type]
        dispatcher,
        1024,
    )
    connection._state = ConnectionState.CONNECTED  # type: ignore[attr-defined]
    connection._read_task = asyncio.create_task(failing_read_task())  # type: ignore[attr-defined]
    await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR):
        await connection.close()

    assert connection.state == ConnectionState.CLOSED
    assert "Unexpected error while awaiting read task during close." in caplog.text


@pytest.mark.asyncio
async def test_tcp_connection_close_logs_unexpected_wait_closed_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class NoOpHandler:
        async def on_event(self, event: object) -> None:
            return None

    class DummyWriter:
        def get_extra_info(self, key: str):
            if key == "sockname":
                return ("127.0.0.1", 10001)
            if key == "peername":
                return ("127.0.0.1", 10002)
            return None

        def write(self, data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            raise RuntimeError("wait-closed-crashed")

    dispatcher = AsyncioEventDispatcher(
        event_handler=NoOpHandler(),
        delivery=EventDeliverySettings(dispatch_mode=EventDispatchMode.INLINE),
        logger=logging.getLogger("test"),
    )
    connection = AsyncioTcpConnection(
        "client:wait-closed-log",
        ConnectionRole.CLIENT,
        asyncio.StreamReader(),
        DummyWriter(),  # type: ignore[arg-type]
        dispatcher,
        1024,
    )
    connection._state = ConnectionState.CONNECTED  # type: ignore[attr-defined]

    with caplog.at_level(logging.ERROR):
        await connection.close()

    assert connection.state == ConnectionState.CLOSED
    assert "Unexpected error while awaiting writer.wait_closed() during close." in caplog.text


class _FakeWriter:
    """Writer stub that captures bytes-like payloads without a real socket."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def get_extra_info(self, key: str):
        if key == "sockname":
            return ("127.0.0.1", 20000)
        if key == "peername":
            return ("127.0.0.1", 20001)
        return None

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


# Bytes-like payload support and closed-event metadata coverage.
@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"raw", bytearray(b"mutable"), memoryview(b"view")])
async def test_tcp_connection_send_accepts_bytes_like_payloads(
    recording_event_handler, payload: bytes | bytearray | memoryview
) -> None:
    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    writer = _FakeWriter()
    connection = AsyncioTcpConnection(
        "client:test-send-bytes-like",
        ConnectionRole.CLIENT,
        asyncio.StreamReader(),
        writer,  # type: ignore[arg-type]
        dispatcher,
        1024,
    )
    await connection.start()
    await connection.send(payload)
    await connection.close()
    await dispatcher.stop()

    assert writer.writes == [bytes(payload)]


@pytest.mark.asyncio
async def test_tcp_connection_closed_event_previous_state_for_explicit_close(
    recording_event_handler,
) -> None:
    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        dispatcher = make_dispatcher(recording_event_handler)
        await dispatcher.start()
        connection = AsyncioTcpConnection(
            "client:test-prev-explicit", ConnectionRole.CLIENT, reader, writer, dispatcher, 1024
        )
        await connection.start()
        await connection.close()
        await dispatcher.stop()

    assert recording_event_handler.closed_events[-1].previous_state == ConnectionState.CONNECTED
    assert recording_event_handler.closed_events[-1].metadata == connection.metadata


@pytest.mark.asyncio
async def test_tcp_connection_closed_event_previous_state_for_eof_close(
    recording_event_handler,
) -> None:
    async def server_handler(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"bye")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        dispatcher = make_dispatcher(recording_event_handler)
        await dispatcher.start()
        connection = AsyncioTcpConnection(
            "client:test-prev-eof", ConnectionRole.CLIENT, reader, writer, dispatcher, 1024
        )
        await connection.start()
        await wait_for_condition(
            lambda: bool(recording_event_handler.closed_events), timeout_seconds=1.0
        )
        await dispatcher.stop()

    assert recording_event_handler.closed_events[-1].previous_state == ConnectionState.CONNECTED
    assert recording_event_handler.closed_events[-1].metadata == connection.metadata
