"""
Unit tests for multicast receiver startup rollback and cleanup.

Multicast setup has several OS-dependent failure points, so this file focuses
on deterministic cleanup when bind, join, receive-loop, or stop paths fail.
The goal is to keep lifecycle events and socket teardown coherent even when
the environment refuses multicast operations.
"""

from __future__ import annotations

import socket
import asyncio

import pytest

from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.connection_events import ConnectionClosedEvent, ConnectionOpenedEvent
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.event_delivery_settings import (
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
)
from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
from aionetx.implementations.asyncio_impl import (
    _asyncio_datagram_receiver_base as datagram_base_module,
)
from aionetx.implementations.asyncio_impl import asyncio_multicast_receiver as multicast_module
from aionetx.implementations.asyncio_impl.asyncio_multicast_receiver import (
    AsyncioMulticastReceiver,
)


class NoopHandler:
    async def on_event(self, event) -> None:
        return None


class FailOnOpenedHandler:
    async def on_event(self, event) -> None:
        if isinstance(event, ConnectionOpenedEvent):
            raise RuntimeError("boom-on-opened")


@pytest.mark.asyncio
@pytest.mark.multicast
@pytest.mark.reliability_smoke
async def test_multicast_start_failure_cleans_dispatcher_and_state(
    awaitable_recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_bind(self: socket.socket, address: tuple[str, int]) -> None:
        raise OSError("bind-failed")

    monkeypatch.setattr(socket.socket, "bind", failing_bind)

    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip="239.255.0.1",
            port=20001,
            bind_ip="0.0.0.0",
            interface_ip="127.0.0.1",
        ),
        event_handler=awaitable_recording_event_handler,
    )

    with pytest.raises(OSError, match="bind-failed"):
        await receiver.start()

    lifecycle_states = [
        event.current
        for event in awaitable_recording_event_handler.lifecycle_events
        if event.resource_id == receiver._connection_id  # type: ignore[attr-defined]
    ]
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert lifecycle_states == [ComponentLifecycleState.STARTING, ComponentLifecycleState.STOPPED]
    assert awaitable_recording_event_handler.closed_events == []
    await receiver.stop()


@pytest.mark.asyncio
async def test_multicast_join_failure_cleans_up_state(
    awaitable_recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_setsockopt = socket.socket.setsockopt

    def failing_setsockopt(self: socket.socket, level: int, optname: int, value) -> None:
        if level == socket.IPPROTO_IP and optname == socket.IP_ADD_MEMBERSHIP:
            raise OSError("join-failed")
        return original_setsockopt(self, level, optname, value)

    monkeypatch.setattr(socket.socket, "setsockopt", failing_setsockopt)

    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip="239.255.0.1",
            port=20002,
            bind_ip="0.0.0.0",
            interface_ip="127.0.0.1",
        ),
        event_handler=awaitable_recording_event_handler,
    )

    with pytest.raises(OSError, match="join-failed"):
        await receiver.start()

    lifecycle_states = [
        event.current
        for event in awaitable_recording_event_handler.lifecycle_events
        if event.resource_id == receiver._connection_id  # type: ignore[attr-defined]
    ]
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert lifecycle_states == [ComponentLifecycleState.STARTING, ComponentLifecycleState.STOPPED]
    assert awaitable_recording_event_handler.closed_events == []


@pytest.mark.asyncio
async def test_multicast_join_failure_rolls_back_without_closed_event(
    awaitable_recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_setsockopt = socket.socket.setsockopt

    def failing_setsockopt(self: socket.socket, level: int, optname: int, value) -> None:
        if level == socket.IPPROTO_IP and optname == socket.IP_ADD_MEMBERSHIP:
            raise OSError("join-failed")
        return original_setsockopt(self, level, optname, value)

    monkeypatch.setattr(socket.socket, "setsockopt", failing_setsockopt)

    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip="239.255.0.10",
            port=20102,
            bind_ip="0.0.0.0",
            interface_ip="127.0.0.1",
        ),
        event_handler=awaitable_recording_event_handler,
    )

    with pytest.raises(OSError, match="join-failed"):
        await receiver.start()

    lifecycle_states = [
        event.current
        for event in awaitable_recording_event_handler.lifecycle_events
        if event.resource_id == receiver._connection_id  # type: ignore[attr-defined]
    ]
    assert lifecycle_states == [ComponentLifecycleState.STARTING, ComponentLifecycleState.STOPPED]
    assert awaitable_recording_event_handler.closed_events == []


@pytest.mark.asyncio
@pytest.mark.multicast
async def test_multicast_opened_handler_failure_rolls_back_runtime_resources() -> None:
    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip="239.255.0.11",
            port=20111,
            bind_ip="0.0.0.0",
            interface_ip="127.0.0.1",
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
async def test_multicast_drop_membership_failure_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original_setsockopt = socket.socket.setsockopt

    def failing_drop_membership(self: socket.socket, level: int, optname: int, value) -> None:
        if level == socket.IPPROTO_IP and optname == socket.IP_DROP_MEMBERSHIP:
            raise OSError("drop-failed")
        return original_setsockopt(self, level, optname, value)

    monkeypatch.setattr(socket.socket, "setsockopt", failing_drop_membership)
    caplog.set_level("WARNING")

    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip="239.255.0.1",
            port=20003,
            bind_ip="0.0.0.0",
            interface_ip="127.0.0.1",
        ),
        event_handler=NoopHandler(),
    )

    await receiver.start()
    await receiver.stop()
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert any("IP_DROP_MEMBERSHIP failure" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_multicast_start_failure_logs_socket_close_cleanup_error(
    awaitable_recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original_socket_cls = socket.socket

    class _FailingCloseSocket(original_socket_cls):  # type: ignore[misc]
        def bind(self, address: tuple[str, int]) -> None:
            raise OSError("bind-failed")

        def close(self) -> None:
            raise OSError("close-failed")

    monkeypatch.setattr(multicast_module.socket, "socket", _FailingCloseSocket)
    caplog.set_level("WARNING")

    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip="239.255.0.20",
            port=20120,
            bind_ip="0.0.0.0",
            interface_ip="127.0.0.1",
        ),
        event_handler=awaitable_recording_event_handler,
    )

    with pytest.raises(OSError, match="bind-failed"):
        await receiver.start()

    assert any(
        "startup cleanup could not close socket cleanly" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_multicast_receive_loop_runtime_failure_emits_error_and_stops(
    awaitable_recording_event_handler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingLoop:
        async def sock_recvfrom(self, _sock: socket.socket, _size: int):
            raise RuntimeError("recv-failed")

    monkeypatch.setattr(datagram_base_module.asyncio, "get_running_loop", lambda: _FailingLoop())

    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip="239.255.0.2",
            port=20004,
            bind_ip="0.0.0.0",
            interface_ip="127.0.0.1",
        ),
        event_handler=awaitable_recording_event_handler,
    )
    await receiver.start()
    await asyncio.wait_for(awaitable_recording_event_handler.error_observed.wait(), timeout=1.0)
    await asyncio.wait_for(awaitable_recording_event_handler.connection_closed.wait(), timeout=1.0)

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    assert receiver._socket is None  # type: ignore[attr-defined]
    assert len(awaitable_recording_event_handler.closed_events) == 1


@pytest.mark.asyncio
async def test_multicast_receiver_stop_from_starting_state_does_not_emit_closed_event(
    awaitable_recording_event_handler,
) -> None:
    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip="239.255.0.3",
            port=20005,
            bind_ip="0.0.0.0",
            interface_ip="127.0.0.1",
        ),
        event_handler=awaitable_recording_event_handler,
    )
    receiver._runtime.lifecycle_state = ComponentLifecycleState.STARTING  # type: ignore[attr-defined]
    receiver._runtime.connection_state = ConnectionState.CREATED  # type: ignore[attr-defined]

    await receiver.stop()

    assert awaitable_recording_event_handler.closed_events == []


@pytest.mark.asyncio
async def test_multicast_receiver_overlapping_stop_calls_publish_one_stop_sequence(
    awaitable_recording_event_handler,
) -> None:
    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip="239.255.0.4",
            port=20006,
            bind_ip="0.0.0.0",
            interface_ip="127.0.0.1",
        ),
        event_handler=awaitable_recording_event_handler,
    )
    await receiver.start()

    await asyncio.gather(receiver.stop(), receiver.stop())

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
