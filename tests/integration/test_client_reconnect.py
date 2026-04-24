"""
Integration tests for TCP client reconnect behavior.

These scenarios verify the user-visible reconnect contract: retries begin only
after start(), waiters unblock deterministically, stop() interrupts in-flight
connect or heartbeat work promptly, and error-policy choices change emitted
events without corrupting lifecycle state.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.heartbeat import HeartbeatRequest
from aionetx.api.heartbeat import HeartbeatResult
from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.error_policy import ErrorPolicy
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_events import ConnectionOpenedEvent
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from tests.helpers import wait_for_condition

pytestmark = pytest.mark.integration_confidence


def get_unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _HeartbeatProvider(HeartbeatProviderProtocol):
    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        return HeartbeatResult(should_send=True, payload=f"HB:{request.connection_id}".encode())


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.integration_smoke
@pytest.mark.integration_semantic
async def test_tcp_client_reconnects_after_server_appears(recording_event_handler) -> None:
    server: asyncio.AbstractServer | None = None
    port = get_unused_tcp_port()

    async def start_server() -> asyncio.AbstractServer:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while await reader.read(4096):
                    continue
            finally:
                writer.close()
                await writer.wait_closed()

        return await asyncio.start_server(handle, "127.0.0.1", port)

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(
                enabled=True,
                initial_delay_seconds=0.05,
                max_delay_seconds=0.1,
                backoff_factor=2.0,
            ),
            connect_timeout_seconds=0.5,
        ),
        event_handler=recording_event_handler,
    )
    delayed_server_task: asyncio.Task[None] | None = None
    try:
        await client.start()

        async def delayed_server_start() -> None:
            nonlocal server
            await wait_for_condition(
                lambda: bool(recording_event_handler.reconnect_attempt_started_events),
                timeout_seconds=1.5,
            )
            server = await start_server()

        delayed_server_task = asyncio.create_task(delayed_server_start())
        await wait_for_condition(
            lambda: client.connection is not None and client.connection.is_connected,
            timeout_seconds=3.0,
        )
        connection = client.connection
        assert connection is not None
        assert connection.is_connected
        # The wait_for_condition above confirmed at least one attempt started and
        # at least one was scheduled; verify the lists are non-empty with an
        # explicit count lower-bound instead of bare truthiness.
        assert len(recording_event_handler.reconnect_attempt_started_events) >= 1
        assert len(recording_event_handler.reconnect_scheduled_events) >= 1
        assert all(
            event.resource_id == f"tcp/client/127.0.0.1/{port}"
            for event in recording_event_handler.reconnect_attempt_started_events
        )
        assert all(
            event.resource_id == f"tcp/client/127.0.0.1/{port}"
            for event in recording_event_handler.reconnect_scheduled_events
        )
        event_types = [type(event).__name__ for event in recording_event_handler.events]
        assert event_types.index("ReconnectAttemptStartedEvent") < event_types.index(
            "ReconnectAttemptFailedEvent"
        )
        assert event_types.index("ReconnectAttemptFailedEvent") < event_types.index(
            "ReconnectScheduledEvent"
        )
        assert event_types.index("ReconnectScheduledEvent") < event_types.index(
            "ConnectionOpenedEvent"
        )
        assert recording_event_handler.reconnect_attempt_started_events[0].attempt == 1
        assert recording_event_handler.reconnect_scheduled_events[0].attempt == 2
    finally:
        if delayed_server_task is not None:
            delayed_server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await delayed_server_task
        await client.stop()
        if server is not None:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.integration_smoke
@pytest.mark.behavior_critical
async def test_client_stop_during_reconnect_sleep_is_prompt(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(
                enabled=True,
                initial_delay_seconds=5.0,
                max_delay_seconds=5.0,
                backoff_factor=1.0,
            ),
            connect_timeout_seconds=0.5,
        ),
        event_handler=recording_event_handler,
    )
    await client.start()

    await wait_for_condition(
        lambda: len(recording_event_handler.reconnect_scheduled_events) >= 1,
        timeout_seconds=1.5,
    )
    scheduled_event = recording_event_handler.reconnect_scheduled_events[0]
    assert scheduled_event.delay_seconds == pytest.approx(5.0)
    assert scheduled_event.attempt == 2

    await asyncio.wait_for(client.stop(), timeout=1.0)

    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert client.connection is None
    assert len(recording_event_handler.reconnect_attempt_started_events) == 1
    client_component_id = f"tcp/client/127.0.0.1/{port}"
    lifecycle_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == client_component_id
    ]
    lifecycle_states = [event.current for event in lifecycle_events]
    assert lifecycle_states.count(ComponentLifecycleState.STOPPING) == 1
    assert lifecycle_states.count(ComponentLifecycleState.STOPPED) == 1
    assert lifecycle_states.index(ComponentLifecycleState.STOPPING) < lifecycle_states.index(
        ComponentLifecycleState.STOPPED
    )
    event_types = [type(event).__name__ for event in recording_event_handler.events]
    assert event_types.count("ReconnectScheduledEvent") == 1
    reconnect_index = event_types.index("ReconnectScheduledEvent")
    stopping_index = next(
        index
        for index, event in enumerate(recording_event_handler.events)
        if (
            type(event).__name__ == "ComponentLifecycleChangedEvent"
            and getattr(event, "current", None) == ComponentLifecycleState.STOPPING
        )
    )
    assert reconnect_index < stopping_index


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.behavior_critical
async def test_client_stop_during_inflight_heartbeat_send_is_prompt(
    recording_event_handler,
) -> None:
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    port = get_unused_tcp_port()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        client = AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
                heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.01),
            ),
            event_handler=recording_event_handler,
            heartbeat_provider=_HeartbeatProvider(),
        )
        await client.start()
        connection = await client.wait_until_connected(timeout_seconds=2.0)
        original_send = connection.send

        async def blocked_send(payload: bytes | bytearray | memoryview) -> None:
            if bytes(payload).startswith(b"HB:"):
                send_started.set()
                await release_send.wait()
            await original_send(payload)

        connection.send = blocked_send  # type: ignore[method-assign]
        await asyncio.wait_for(send_started.wait(), timeout=1.0)
        await asyncio.wait_for(client.stop(), timeout=1.0)
        release_send.set()

        assert client.lifecycle_state == ComponentLifecycleState.STOPPED
        assert client.connection is None
        opened_events = [
            event
            for event in recording_event_handler.events
            if isinstance(event, ConnectionOpenedEvent)
        ]
        assert len(opened_events) == 1
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_wait_until_connected_rejects_non_positive_poll_interval(
    recording_event_handler,
) -> None:
    port = get_unused_tcp_port()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(host="127.0.0.1", port=port),
        event_handler=recording_event_handler,
    )

    with pytest.raises(ValueError, match="poll_interval_seconds"):
        await client.wait_until_connected(poll_interval_seconds=0)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_wait_until_connected_fails_when_server_never_available(
    recording_event_handler,
) -> None:
    port = get_unused_tcp_port()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )

    await client.start()
    try:
        with pytest.raises((asyncio.TimeoutError, ConnectionError)):
            await client.wait_until_connected(
                timeout_seconds=0.15,
                poll_interval_seconds=0.02,
            )
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_wait_until_connected_does_not_fail_immediately_before_start(
    recording_event_handler,
    unused_tcp_port: int,
) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while await reader.read(4096):
                continue
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", unused_tcp_port)
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=unused_tcp_port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=recording_event_handler,
    )
    try:
        waiter_task = asyncio.create_task(
            client.wait_until_connected(timeout_seconds=2.0, poll_interval_seconds=0.02)
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiter_task), timeout=0.05)
        assert waiter_task.done() is False
        await client.start()
        connection = await waiter_task
        assert connection.is_connected
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_wait_until_connected_waiters_unblock_when_client_stops(
    recording_event_handler,
    unused_tcp_port: int,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=unused_tcp_port,
            reconnect=TcpReconnectSettings(
                enabled=True, initial_delay_seconds=0.05, max_delay_seconds=0.1
            ),
        ),
        event_handler=recording_event_handler,
    )
    await client.start()

    waiters = [
        asyncio.create_task(client.wait_until_connected(timeout_seconds=2.0)) for _ in range(4)
    ]
    await wait_for_condition(
        lambda: all(waiter.done() is False for waiter in waiters),
        timeout_seconds=0.5,
    )
    await client.stop()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert all(isinstance(result, ConnectionError) for result in results)
    assert all(waiter.done() for waiter in waiters)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.behavior_critical
async def test_client_stop_while_connect_attempt_is_pending(
    recording_event_handler,
    unused_tcp_port: int,
) -> None:
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()

    async def slow_opener(
        *, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        assert host == "127.0.0.1"
        assert port == unused_tcp_port
        connect_started.set()
        await release_connect.wait()
        raise OSError("connect-cancelled-for-test")

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=unused_tcp_port,
            reconnect=TcpReconnectSettings(
                enabled=True, initial_delay_seconds=0.1, max_delay_seconds=0.1
            ),
        ),
        event_handler=recording_event_handler,
        connection_opener=slow_opener,
    )
    await client.start()
    await asyncio.wait_for(connect_started.wait(), timeout=1.0)

    stop_task = asyncio.create_task(client.stop())
    await asyncio.wait_for(stop_task, timeout=1.0)
    release_connect.set()

    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert client._supervisor_task is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_client_restart_clears_cached_connect_error(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
            # Must be < 0.5s so the connect fails and the supervisor transitions
            # to STOPPED before wait_until_connected(timeout_seconds=0.5) fires.
            connect_timeout_seconds=0.3,
        ),
        event_handler=recording_event_handler,
    )

    # First run with no server: should fail and cache a connect error.
    await client.start()
    try:
        with pytest.raises(ConnectionError):
            await client.wait_until_connected(timeout_seconds=0.5, poll_interval_seconds=0.02)
    finally:
        await client.stop()

    # Second run with server available must not raise stale first-run error.
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while await reader.read(4096):
                continue
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        await client.start()
        connection = await client.wait_until_connected(timeout_seconds=2.0)
        assert connection.is_connected
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_client_fail_fast_policy_stops_supervision(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=True, initial_delay_seconds=0.05),
            error_policy=ErrorPolicy.FAIL_FAST,
            connect_timeout_seconds=0.5,
        ),
        event_handler=recording_event_handler,
    )

    await client.start()
    # Wait for both STOPPED state and event delivery (BACKGROUND dispatcher may
    # queue events while the event loop is busy with other tasks).
    await wait_for_condition(
        lambda: (
            client.lifecycle_state == ComponentLifecycleState.STOPPED
            and bool(recording_event_handler.reconnect_attempt_failed_events)
        ),
        timeout_seconds=1.0,
    )
    # FAIL_FAST policy: exactly 1 attempt is made, then the client stops.
    assert len(recording_event_handler.reconnect_attempt_failed_events) == 1
    assert recording_event_handler.reconnect_scheduled_events == []
    assert recording_event_handler.reconnect_attempt_failed_events[
        0
    ].next_delay_seconds == pytest.approx(0.05)

    with pytest.raises(ConnectionError):
        await client.wait_until_connected(timeout_seconds=0.2)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_client_ignore_policy_suppresses_error_events(recording_event_handler) -> None:
    port = get_unused_tcp_port()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(
                enabled=True,
                initial_delay_seconds=0.05,
                max_delay_seconds=0.1,
                backoff_factor=1.0,
            ),
            error_policy=ErrorPolicy.IGNORE,
            connect_timeout_seconds=0.5,
        ),
        event_handler=recording_event_handler,
    )
    await client.start()

    await wait_for_condition(
        lambda: bool(recording_event_handler.reconnect_attempt_failed_events),
        timeout_seconds=1.5,
    )
    assert recording_event_handler.error_events == []
    assert len(recording_event_handler.reconnect_attempt_failed_events) >= 1
    assert client.lifecycle_state in (
        ComponentLifecycleState.RUNNING,
        ComponentLifecycleState.STOPPING,
    )

    await client.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_client_default_retry_policy_emits_error_then_recovers(
    recording_event_handler,
) -> None:
    server: asyncio.AbstractServer | None = None
    port = get_unused_tcp_port()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while await reader.read(4096):
                continue
        finally:
            writer.close()
            await writer.wait_closed()

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(
                enabled=True,
                initial_delay_seconds=0.05,
                max_delay_seconds=0.1,
                backoff_factor=1.0,
            ),
            connect_timeout_seconds=0.5,
        ),
        event_handler=recording_event_handler,
    )

    try:
        await client.start()
        await wait_for_condition(
            lambda: bool(recording_event_handler.error_events), timeout_seconds=1.5
        )
        await wait_for_condition(
            lambda: bool(recording_event_handler.reconnect_attempt_failed_events),
            timeout_seconds=1.5,
        )

        server = await asyncio.start_server(handle, "127.0.0.1", port)
        await wait_for_condition(
            lambda: client.connection is not None and client.connection.is_connected,
            timeout_seconds=3.0,
        )

        assert len(recording_event_handler.error_events) >= 1
        assert len(recording_event_handler.reconnect_scheduled_events) >= 1
        assert client.connection is not None
        assert client.connection.is_connected
    finally:
        await client.stop()
        if server is not None:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_client_reconnect_event_sequence_contains_expected_order(
    awaitable_recording_event_handler,
    unused_tcp_port: int,
) -> None:
    server: asyncio.AbstractServer | None = None
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=unused_tcp_port,
            reconnect=TcpReconnectSettings(
                enabled=True, initial_delay_seconds=0.05, max_delay_seconds=0.1
            ),
            connect_timeout_seconds=0.5,
        ),
        event_handler=awaitable_recording_event_handler,
    )
    await client.start()
    await wait_for_condition(
        lambda: bool(awaitable_recording_event_handler.reconnect_scheduled_events),
        timeout_seconds=1.5,
    )
    server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", unused_tcp_port)
    try:
        await asyncio.wait_for(
            awaitable_recording_event_handler.connection_opened.wait(), timeout=2.0
        )
        event_types = [type(event).__name__ for event in awaitable_recording_event_handler.events]
        started_index = event_types.index("ReconnectAttemptStartedEvent")
        failed_index = event_types.index("ReconnectAttemptFailedEvent")
        scheduled_index = event_types.index("ReconnectScheduledEvent")
        opened_index = event_types.index("ConnectionOpenedEvent")
        assert started_index < failed_index < scheduled_index < opened_index
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancelling_connect_waiter_does_not_break_subsequent_waiters(
    recording_event_handler,
    unused_tcp_port: int,
) -> None:
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=unused_tcp_port,
            reconnect=TcpReconnectSettings(
                enabled=True, initial_delay_seconds=0.05, max_delay_seconds=0.1
            ),
        ),
        event_handler=recording_event_handler,
    )
    await client.start()
    try:
        cancelled_waiter = asyncio.create_task(client.wait_until_connected(timeout_seconds=2.0))
        await wait_for_condition(lambda: cancelled_waiter.done() is False, timeout_seconds=0.5)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter

        surviving_waiter = asyncio.create_task(client.wait_until_connected(timeout_seconds=2.0))
        await wait_for_condition(lambda: surviving_waiter.done() is False, timeout_seconds=0.5)
        await client.stop()
        result = await asyncio.gather(surviving_waiter, return_exceptions=True)
        assert len(result) == 1
        assert isinstance(result[0], ConnectionError)
        assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.behavior_critical
async def test_supervisor_cancellation_during_connect_attempt_finalizes_without_leaking_tasks(
    recording_event_handler,
    unused_tcp_port: int,
) -> None:
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()

    async def slow_opener(
        *, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        assert host == "127.0.0.1"
        assert port == unused_tcp_port
        connect_started.set()
        await release_connect.wait()
        raise OSError("connect-cancelled-for-test")

    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=unused_tcp_port,
            reconnect=TcpReconnectSettings(
                enabled=True, initial_delay_seconds=0.1, max_delay_seconds=0.1
            ),
        ),
        event_handler=recording_event_handler,
        connection_opener=slow_opener,
    )
    await client.start()
    await asyncio.wait_for(connect_started.wait(), timeout=1.0)

    supervisor = client._supervisor_task  # type: ignore[attr-defined]
    assert supervisor is not None
    supervisor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await supervisor

    release_connect.set()
    await asyncio.wait_for(client.stop(), timeout=1.0)
    assert client.lifecycle_state == ComponentLifecycleState.STOPPED
    assert client._supervisor_task is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reconnect_client_repeated_start_stop_cycles_remain_operationally_stable(
    recording_event_handler,
) -> None:
    port = get_unused_tcp_port()
    client = AsyncioTcpClient(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(
                enabled=True,
                initial_delay_seconds=0.05,
                max_delay_seconds=0.05,
                backoff_factor=1.0,
            ),
            connect_timeout_seconds=0.5,
        ),
        event_handler=recording_event_handler,
    )

    reconnect_counts: list[int] = []
    for _ in range(3):
        await client.start()
        await wait_for_condition(
            lambda: (
                len(recording_event_handler.reconnect_scheduled_events)
                >= (len(reconnect_counts) + 1)
            ),
            timeout_seconds=1.5,
        )
        reconnect_counts.append(len(recording_event_handler.reconnect_scheduled_events))
        await asyncio.wait_for(client.stop(), timeout=1.0)
        assert client.lifecycle_state == ComponentLifecycleState.STOPPED
        assert client.connection is None

    assert reconnect_counts[0] < reconnect_counts[1] < reconnect_counts[2]
    assert client.dispatcher_runtime_stats.emit_calls_total > 0
