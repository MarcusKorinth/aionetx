from __future__ import annotations

import asyncio
import logging

import pytest

from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.event_delivery_settings import EventDeliverySettings
from aionetx.api.heartbeat import HeartbeatRequest
from aionetx.api.heartbeat import HeartbeatResult
from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api._event_registry import NETWORK_EVENT_TYPES
from aionetx.implementations.asyncio_impl.asyncio_heartbeat_sender import AsyncioHeartbeatSender
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher
from tests.helpers import wait_for_condition


class FakeConnection:
    def __init__(self) -> None:
        self.connection_id = "fake"
        self.role = ConnectionRole.CLIENT
        self.state = ConnectionState.CONNECTED
        self.metadata = ConnectionMetadata(connection_id="fake", role=ConnectionRole.CLIENT)
        self.sent: list[bytes] = []

    @property
    def is_connected(self) -> bool:
        return self.state == ConnectionState.CONNECTED

    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def close(self) -> None:
        self.state = ConnectionState.CLOSED


class ToggleHeartbeatProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        self.calls += 1
        if self.calls % 2 == 0:
            return HeartbeatResult(should_send=False)
        return HeartbeatResult(
            should_send=True, payload=f"hb:{request.connection_id}:{self.calls}".encode()
        )


class AlwaysSendHeartbeatProvider:
    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        return HeartbeatResult(should_send=True, payload=f"hb:{request.connection_id}".encode())


class FailingHeartbeatProvider:
    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        raise RuntimeError("failed")


class InvalidResultHeartbeatProvider:
    async def create_heartbeat(self, request: HeartbeatRequest) -> str:
        return "not-a-heartbeat-result"


class InvalidPayloadHeartbeatProvider:
    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        return HeartbeatResult(should_send=True, payload="bad-payload")  # type: ignore[arg-type]


class InvalidShouldSendHeartbeatProvider:
    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        return HeartbeatResult(should_send="yes", payload=b"hb")  # type: ignore[arg-type]


class FailingSendConnection(FakeConnection):
    def __init__(self) -> None:
        super().__init__()
        self.send_calls = 0

    async def send(self, data: bytes) -> None:
        self.send_calls += 1
        raise RuntimeError("send-failed")


def make_dispatcher(handler) -> AsyncioEventDispatcher:
    return AsyncioEventDispatcher(handler, EventDeliverySettings(), logging.getLogger("test"))


@pytest.mark.asyncio
async def test_heartbeat_sender_sends_only_when_requested(recording_event_handler) -> None:
    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    sender = AsyncioHeartbeatSender(
        FakeConnection(),
        TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
        ToggleHeartbeatProvider(),
        dispatcher,
    )
    await sender.start()
    await wait_for_condition(lambda: len(sender._connection.sent) >= 2, timeout_seconds=1.0)
    await sender.stop()
    await dispatcher.stop()
    assert sender._connection.sent
    assert recording_event_handler.events == []


@pytest.mark.asyncio
async def test_heartbeat_sender_start_and_stop_are_idempotent(recording_event_handler) -> None:
    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    sender = AsyncioHeartbeatSender(
        FakeConnection(),
        TcpHeartbeatSettings(enabled=True, interval_seconds=1.0),
        ToggleHeartbeatProvider(),
        dispatcher,
    )

    await sender.start()
    first_task = sender._task  # type: ignore[attr-defined]
    await sender.start()

    assert sender.is_running is True
    assert sender._task is first_task  # type: ignore[attr-defined]

    await sender.stop()
    await sender.stop()
    await dispatcher.stop()

    assert sender.is_running is False
    assert sender._task is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_heartbeat_sender_stop_preserves_caller_cancellation_after_task_cleanup(
    recording_event_handler,
) -> None:
    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    sender = AsyncioHeartbeatSender(
        FakeConnection(),
        TcpHeartbeatSettings(enabled=True, interval_seconds=1.0),
        ToggleHeartbeatProvider(),
        dispatcher,
    )
    task_cancel_seen = asyncio.Event()
    release_task = asyncio.Event()

    async def blocking_heartbeat_task() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            task_cancel_seen.set()
            await release_task.wait()
            raise

    heartbeat_task = asyncio.create_task(blocking_heartbeat_task())
    sender._running = True  # type: ignore[attr-defined]
    sender._task = heartbeat_task  # type: ignore[attr-defined]
    stop_task = asyncio.create_task(sender.stop())

    try:
        await asyncio.wait_for(task_cancel_seen.wait(), timeout=1.0)
        stop_task.cancel()
        await asyncio.sleep(0)

        assert not stop_task.done()

        release_task.set()
        with pytest.raises(asyncio.CancelledError):
            await stop_task

        assert sender.is_running is False
        assert sender._task is None  # type: ignore[attr-defined]
        assert heartbeat_task.done()
    finally:
        release_task.set()
        if not stop_task.done():
            stop_task.cancel()
            try:
                await stop_task
            except asyncio.CancelledError:
                pass
        if not heartbeat_task.done():
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_heartbeat_sender_exits_when_connection_is_no_longer_connected(
    recording_event_handler,
) -> None:
    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    connection = FakeConnection()
    connection.state = ConnectionState.CLOSED
    sender = AsyncioHeartbeatSender(
        connection,
        TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
        ToggleHeartbeatProvider(),
        dispatcher,
    )

    await sender.start()
    await wait_for_condition(lambda: sender.is_running is False, timeout_seconds=1.0)
    await dispatcher.stop()

    assert connection.sent == []
    assert recording_event_handler.events == []


def test_network_event_taxonomy_has_no_heartbeat_activity_events() -> None:
    heartbeat_named_events = [
        event_type.__name__
        for event_type in NETWORK_EVENT_TYPES
        if "heartbeat" in event_type.__name__.lower()
    ]
    assert heartbeat_named_events == []


@pytest.mark.asyncio
async def test_heartbeat_sender_emits_error_when_provider_fails(recording_event_handler) -> None:
    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    sender = AsyncioHeartbeatSender(
        FakeConnection(),
        TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
        FailingHeartbeatProvider(),
        dispatcher,
    )
    await sender.start()
    await wait_for_condition(lambda: sender.is_running is False, timeout_seconds=1.0)
    await dispatcher.stop()
    assert recording_event_handler.error_events


@pytest.mark.asyncio
async def test_heartbeat_sender_emits_error_for_invalid_result_type(
    recording_event_handler,
) -> None:
    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    sender = AsyncioHeartbeatSender(
        FakeConnection(),
        TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
        InvalidResultHeartbeatProvider(),  # type: ignore[arg-type]
        dispatcher,
    )
    await sender.start()
    await wait_for_condition(lambda: sender.is_running is False, timeout_seconds=1.0)
    await dispatcher.stop()
    assert recording_event_handler.error_events
    assert isinstance(recording_event_handler.error_events[-1], NetworkErrorEvent)
    assert isinstance(recording_event_handler.error_events[-1].error, TypeError)


@pytest.mark.asyncio
async def test_heartbeat_sender_emits_error_for_invalid_payload_type(
    recording_event_handler,
) -> None:
    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    sender = AsyncioHeartbeatSender(
        FakeConnection(),
        TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
        InvalidPayloadHeartbeatProvider(),  # type: ignore[arg-type]
        dispatcher,
    )
    await sender.start()
    await wait_for_condition(lambda: sender.is_running is False, timeout_seconds=1.0)
    await dispatcher.stop()
    assert recording_event_handler.error_events
    assert isinstance(recording_event_handler.error_events[-1].error, TypeError)


@pytest.mark.asyncio
async def test_heartbeat_sender_emits_error_for_invalid_should_send_type(
    recording_event_handler,
) -> None:
    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    sender = AsyncioHeartbeatSender(
        FakeConnection(),
        TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
        InvalidShouldSendHeartbeatProvider(),  # type: ignore[arg-type]
        dispatcher,
    )
    await sender.start()
    await wait_for_condition(lambda: sender.is_running is False, timeout_seconds=1.0)
    await dispatcher.stop()
    assert recording_event_handler.error_events
    assert isinstance(recording_event_handler.error_events[-1].error, TypeError)


def test_heartbeat_sender_requires_enabled_settings(recording_event_handler) -> None:
    dispatcher = make_dispatcher(recording_event_handler)
    with pytest.raises(ValueError, match="requires TcpHeartbeatSettings.enabled=True"):
        AsyncioHeartbeatSender(
            FakeConnection(),
            TcpHeartbeatSettings(enabled=False, interval_seconds=1.0),
            ToggleHeartbeatProvider(),
            dispatcher,
        )


@pytest.mark.asyncio
async def test_heartbeat_sender_emits_error_and_stops_when_connection_send_fails(
    recording_event_handler,
) -> None:
    dispatcher = make_dispatcher(recording_event_handler)
    await dispatcher.start()
    connection = FailingSendConnection()
    sender = AsyncioHeartbeatSender(
        connection,
        TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
        AlwaysSendHeartbeatProvider(),
        dispatcher,
    )
    await sender.start()
    await wait_for_condition(lambda: sender.is_running is False, timeout_seconds=1.0)
    await dispatcher.stop()

    assert connection.send_calls == 1
    assert recording_event_handler.error_events
    assert isinstance(recording_event_handler.error_events[-1].error, RuntimeError)
