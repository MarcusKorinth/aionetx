"""
State-machine tests for AsyncioTcpClient lifecycle transitions.

This module isolates the managed client lifecycle contract: legal transition
order, start/stop idempotency, waiter unblocking, and handler-failure
interactions during startup. Reconnect semantics are covered elsewhere; this
file focuses on the lifecycle state machine itself.
"""

from __future__ import annotations

import asyncio

import pytest

from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.event_delivery_settings import (
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
)
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from tests.helpers import wait_for_condition

pytestmark = pytest.mark.behavior_critical


async def _no_op_server_handler(
    _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_client_lifecycle_start_stop_is_deterministic(recording_event_handler) -> None:
    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        client = AsyncioTcpClient(
            TcpClientSettings(host="127.0.0.1", port=port), recording_event_handler
        )
        assert client.lifecycle_state == ComponentLifecycleState.STOPPED
        await client.start()
        assert client.lifecycle_state == ComponentLifecycleState.RUNNING
        await client.wait_until_connected(timeout_seconds=2.0)
        await client.stop()
        assert client.lifecycle_state == ComponentLifecycleState.STOPPED


@pytest.mark.asyncio
async def test_client_lifecycle_events_preserve_start_stop_order(recording_event_handler) -> None:
    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        client = AsyncioTcpClient(
            TcpClientSettings(host="127.0.0.1", port=port), recording_event_handler
        )
        await client.start()
        await client.stop()

    component_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == f"tcp/client/127.0.0.1/{port}"
    ]
    assert [event.current for event in component_events] == [
        ComponentLifecycleState.STARTING,
        ComponentLifecycleState.RUNNING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    ]


@pytest.mark.asyncio
async def test_client_concurrent_start_and_stop_are_safe(recording_event_handler) -> None:
    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        client = AsyncioTcpClient(
            TcpClientSettings(host="127.0.0.1", port=port), recording_event_handler
        )

        await asyncio.gather(client.start(), client.start())
        assert client.lifecycle_state == ComponentLifecycleState.RUNNING

        await asyncio.gather(client.stop(), client.stop())
        assert client.lifecycle_state == ComponentLifecycleState.STOPPED


class _FailOnClientStartingLifecycle:
    async def on_event(self, event) -> None:
        if (
            isinstance(event, ComponentLifecycleChangedEvent)
            and event.current == ComponentLifecycleState.STARTING
        ):
            raise RuntimeError("client-lifecycle-handler-failed")


class _StopOnClientStartingLifecycle:
    def __init__(self) -> None:
        self.client: AsyncioTcpClient | None = None
        self.lifecycle_events: list[ComponentLifecycleChangedEvent] = []
        self._stop_requested = False

    async def on_event(self, event) -> None:
        if not isinstance(event, ComponentLifecycleChangedEvent):
            return
        self.lifecycle_events.append(event)
        if event.current == ComponentLifecycleState.STARTING and not self._stop_requested:
            self._stop_requested = True
            assert self.client is not None
            await self.client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dispatch_mode",
    (EventDispatchMode.INLINE, EventDispatchMode.BACKGROUND),
)
async def test_client_starting_handler_stop_suppresses_stale_running_event(
    dispatch_mode: EventDispatchMode,
) -> None:
    handler = _StopOnClientStartingLifecycle()
    client = AsyncioTcpClient(
        TcpClientSettings(
            host="127.0.0.1",
            port=12345,
            event_delivery=EventDeliverySettings(dispatch_mode=dispatch_mode),
        ),
        handler,
    )
    handler.client = client

    await asyncio.wait_for(client.start(), timeout=1.0)
    await wait_for_condition(lambda: len(handler.lifecycle_events) >= 3)

    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert client.connection is None
    assert client.dispatcher_runtime_stats.queue_depth == 0
    assert [event.current for event in handler.lifecycle_events] == [
        ComponentLifecycleState.STARTING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    ]


@pytest.mark.asyncio
async def test_client_start_inline_stop_component_handler_failure_does_not_deadlock() -> None:
    client = AsyncioTcpClient(
        TcpClientSettings(
            host="127.0.0.1",
            port=12345,
            event_delivery=EventDeliverySettings(
                dispatch_mode=EventDispatchMode.INLINE,
                handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT,
            ),
        ),
        _FailOnClientStartingLifecycle(),
    )

    await asyncio.wait_for(client.start(), timeout=1.0)
    assert client.lifecycle_state == ComponentLifecycleState.STOPPED


@pytest.mark.asyncio
async def test_client_rejects_illegal_lifecycle_transition(recording_event_handler) -> None:
    client = AsyncioTcpClient(
        TcpClientSettings(
            host="127.0.0.1",
            port=12345,
        ),
        recording_event_handler,
    )

    with pytest.raises(RuntimeError, match="Illegal lifecycle transition"):
        await client._transition_lifecycle_state(ComponentLifecycleState.RUNNING)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_client_stop_during_start_is_safe(recording_event_handler) -> None:
    gate = asyncio.Event()

    async def delayed_open_connection(*, host: str, port: int):
        del host, port
        await gate.wait()
        raise OSError("connect-cancelled")

    client = AsyncioTcpClient(
        TcpClientSettings(host="127.0.0.1", port=12345),
        recording_event_handler,
        connection_opener=delayed_open_connection,
    )
    start_task = asyncio.create_task(client.start())
    await asyncio.sleep(0)
    stop_task = asyncio.create_task(client.stop())
    gate.set()
    await asyncio.gather(start_task, stop_task)
    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    component_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == "tcp/client/127.0.0.1/12345"
    ]
    assert [event.current for event in component_events][-2:] == [
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    ]
    assert (
        sum(1 for event in component_events if event.current == ComponentLifecycleState.STOPPING)
        == 1
    )
    assert (
        sum(1 for event in component_events if event.current == ComponentLifecycleState.STOPPED)
        == 1
    )


@pytest.mark.asyncio
async def test_client_repeated_restart_cycles_keep_lifecycle_transitions_coherent(
    recording_event_handler,
) -> None:
    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        client = AsyncioTcpClient(
            TcpClientSettings(host="127.0.0.1", port=port),
            recording_event_handler,
        )

        for _ in range(3):
            await client.start()
            await client.wait_until_connected(timeout_seconds=2.0)
            await client.stop()
            assert client.lifecycle_state == ComponentLifecycleState.STOPPED

    component_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == f"tcp/client/127.0.0.1/{port}"
    ]
    states = [event.current for event in component_events]

    assert states == [
        ComponentLifecycleState.STARTING,
        ComponentLifecycleState.RUNNING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
        ComponentLifecycleState.STARTING,
        ComponentLifecycleState.RUNNING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
        ComponentLifecycleState.STARTING,
        ComponentLifecycleState.RUNNING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    ]


@pytest.mark.asyncio
async def test_client_stop_during_start_unblocks_pending_connect_waiters(
    recording_event_handler,
) -> None:
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()

    async def delayed_open_connection(*, host: str, port: int):
        del host, port
        connect_started.set()
        await release_connect.wait()
        raise OSError("connect-cancelled")

    client = AsyncioTcpClient(
        TcpClientSettings(host="127.0.0.1", port=12345),
        recording_event_handler,
        connection_opener=delayed_open_connection,
    )
    await client.start()
    await asyncio.wait_for(connect_started.wait(), timeout=1.0)

    waiters = [
        asyncio.create_task(client.wait_until_connected(timeout_seconds=2.0)) for _ in range(3)
    ]

    async def _all_waiters_pending() -> None:
        while not all(waiter.done() is False for waiter in waiters):
            await asyncio.sleep(0)

    await asyncio.wait_for(_all_waiters_pending(), timeout=0.5)

    stop_tasks = [asyncio.create_task(client.stop()), asyncio.create_task(client.stop())]
    release_connect.set()
    await asyncio.gather(*stop_tasks)
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert all(isinstance(result, ConnectionError) for result in results)


@pytest.mark.asyncio
async def test_client_repeated_start_is_idempotent_and_does_not_duplicate_start_events(
    recording_event_handler,
) -> None:
    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        client = AsyncioTcpClient(
            TcpClientSettings(host="127.0.0.1", port=port), recording_event_handler
        )
        await client.start()
        await client.start()
        await client.wait_until_connected(timeout_seconds=2.0)
        await client.stop()

    component_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == f"tcp/client/127.0.0.1/{port}"
    ]
    assert [event.current for event in component_events] == [
        ComponentLifecycleState.STARTING,
        ComponentLifecycleState.RUNNING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    ]


@pytest.mark.asyncio
async def test_client_repeated_stop_is_idempotent_and_does_not_duplicate_terminal_events(
    recording_event_handler,
) -> None:
    server = await asyncio.start_server(_no_op_server_handler, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        client = AsyncioTcpClient(
            TcpClientSettings(host="127.0.0.1", port=port), recording_event_handler
        )
        await client.start()
        await client.wait_until_connected(timeout_seconds=2.0)
        await client.stop()
        await client.stop()

    component_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == f"tcp/client/127.0.0.1/{port}"
    ]

    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert [event.current for event in component_events] == [
        ComponentLifecycleState.STARTING,
        ComponentLifecycleState.RUNNING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    ]


@pytest.mark.asyncio
async def test_client_fail_fast_connect_failure_unblocks_pending_connect_waiters(
    recording_event_handler,
) -> None:
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()

    async def failing_open_connection(*, host: str, port: int):
        del host, port
        connect_started.set()
        await release_connect.wait()
        raise OSError("connect-failed")

    client = AsyncioTcpClient(
        TcpClientSettings(host="127.0.0.1", port=12345),
        recording_event_handler,
        connection_opener=failing_open_connection,
    )
    await client.start()
    await asyncio.wait_for(connect_started.wait(), timeout=1.0)

    waiters = [
        asyncio.create_task(client.wait_until_connected(timeout_seconds=2.0)) for _ in range(3)
    ]
    await asyncio.sleep(0)
    assert all(waiter.done() is False for waiter in waiters)
    release_connect.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert all(isinstance(result, ConnectionError) for result in results)
