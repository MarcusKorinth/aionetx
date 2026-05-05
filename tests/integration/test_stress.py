"""
Stress tests for moderate fan-out and many-connection scenarios.

These tests check that the server can manage dozens of simultaneous clients
without leaks, deadlocks, or silent delivery loss. The concurrency levels stay
modest enough for CI while still exercising code paths that matter for
multi-session workloads.

Marked ``slow`` because wall-clock time scales with connection count.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from collections import defaultdict
import socket

import pytest

from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_events import ConnectionClosedEvent
from aionetx.api.connection_events import ConnectionOpenedEvent
from aionetx.api.connection_protocol import ConnectionProtocol
from aionetx.api.errors import ConnectionClosedError
from aionetx.api.network_event import NetworkEvent
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from tests.helpers import wait_for_condition


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Noop:
    async def on_event(self, event: NetworkEvent) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_server_handles_100_concurrent_connections() -> None:
    """
    Server accepts and tracks 100 simultaneous client connections.

    All clients connect concurrently using asyncio.gather.  After all are
    confirmed connected on the client side, we wait for the server to register
    all connections, then stop everything cleanly.

    Invariants checked:
    - Server records exactly 100 connections (no silent drops or duplicates).
    - All clients report is_connected.
    - Clean shutdown leaves no dangling connections.
    """
    num_clients = 100
    port = _unused_port()

    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=num_clients,
            backlog=128,
        ),
        event_handler=_Noop(),
    )
    await server.start()

    clients = [
        AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
            ),
            event_handler=_Noop(),
        )
        for _ in range(num_clients)
    ]

    try:
        # Start all clients concurrently.
        await asyncio.gather(*[c.start() for c in clients])

        # Wait for all clients to report connected.
        connections = await asyncio.gather(
            *[c.wait_until_connected(timeout_seconds=10.0) for c in clients]
        )
        assert all(conn.is_connected for conn in connections), (
            "Not all client connections reached is_connected=True."
        )

        # Wait for server to register all connections (server-side _handle_client
        # tasks run concurrently and may not all have finished registering yet).
        await wait_for_condition(
            lambda: len(server.connections) == num_clients,
            timeout_seconds=5.0,
        )
        assert len(server.connections) == num_clients, (
            f"Server registered {len(server.connections)} connections; expected {num_clients}."
        )
    finally:
        # Stop all clients concurrently, then the server.
        # return_exceptions=True prevents a single stop() failure from skipping
        # the remaining client stops and the subsequent server.stop().
        await asyncio.gather(*[c.stop() for c in clients], return_exceptions=True)
        await server.stop()

    assert len(server.connections) == 0, "Server must have zero connections after stop()."


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_server_broadcast_reaches_all_connected_clients() -> None:
    """
    Server broadcast delivers data to all 50 concurrently connected clients.

    Each client has a recording handler.  After broadcast, every client must
    have received the exact payload.
    """
    num_clients = 50
    port = _unused_port()

    received_by: dict[int, list[bytes]] = {i: [] for i in range(num_clients)}

    class _Recorder:
        def __init__(self, idx: int) -> None:
            self._idx = idx

        async def on_event(self, event: NetworkEvent) -> None:
            from aionetx.api.bytes_received_event import BytesReceivedEvent

            if isinstance(event, BytesReceivedEvent):
                received_by[self._idx].append(event.data)

    server = AsyncioTcpServer(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64, backlog=64),
        event_handler=_Noop(),
    )
    clients = [
        AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
            ),
            event_handler=_Recorder(i),
        )
        for i in range(num_clients)
    ]

    await server.start()
    try:
        await asyncio.gather(*[c.start() for c in clients])
        await asyncio.gather(*[c.wait_until_connected(timeout_seconds=10.0) for c in clients])
        await wait_for_condition(
            lambda: len(server.connections) == num_clients,
            timeout_seconds=5.0,
        )

        payload = b"broadcast-stress-test"
        await server.broadcast(payload)

        # Wait for every client to receive the broadcast.
        await wait_for_condition(
            lambda: all(len(received_by[i]) >= 1 for i in range(num_clients)),
            timeout_seconds=5.0,
        )

        # Exactly-once delivery: each client must have received the payload
        # precisely once — duplicates would indicate a broadcast bug.
        for i in range(num_clients):
            count = received_by[i].count(payload)
            assert count == 1, f"Client {i} received payload {count} time(s); expected exactly 1."
    finally:
        # return_exceptions=True prevents a single stop() failure from skipping
        # the remaining client stops and the subsequent server.stop().
        await asyncio.gather(*[c.stop() for c in clients], return_exceptions=True)
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_bidirectional_payload_churn_survives_simultaneous_close_race() -> None:
    """
    Payload flow in both TCP directions remains orderly while both sides stop.

    This is not a throughput benchmark. It pins the lifecycle edge where active
    client/server send loops are still running when all managed TCP endpoints
    begin shutdown concurrently.
    """
    num_clients = 4
    port = _unused_port()
    client_prefix = b"client-churn:"
    server_prefix = b"server-churn:"

    class _TrafficRecorder:
        def __init__(self) -> None:
            self.opened_ids: list[str] = []
            self.closed_ids: list[str] = []
            self.bytes_by_connection: defaultdict[str, list[bytes]] = defaultdict(list)
            self.events_by_connection: defaultdict[str, list[str]] = defaultdict(list)

        async def on_event(self, event: NetworkEvent) -> None:
            if isinstance(event, ConnectionOpenedEvent):
                self.opened_ids.append(event.resource_id)
                self.events_by_connection[event.resource_id].append("opened")
                return
            if isinstance(event, ConnectionClosedEvent):
                self.closed_ids.append(event.resource_id)
                self.events_by_connection[event.resource_id].append("closed")
                return
            if isinstance(event, BytesReceivedEvent):
                self.bytes_by_connection[event.resource_id].append(event.data)
                self.events_by_connection[event.resource_id].append("bytes")

    server_recorder = _TrafficRecorder()
    client_recorders = [_TrafficRecorder() for _ in range(num_clients)]
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=num_clients,
            receive_buffer_size=64,
        ),
        event_handler=server_recorder,
    )
    clients = [
        AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=port,
                reconnect=TcpReconnectSettings(enabled=False),
                receive_buffer_size=64,
            ),
            event_handler=client_recorders[index],
        )
        for index in range(num_clients)
    ]
    close_started = asyncio.Event()
    send_counts: Counter[str] = Counter()
    send_tasks: list[asyncio.Task[None]] = []
    client_connection_ids: list[str] = []
    server_connection_ids: set[str] = set()
    baseline_tasks = {
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    previous_task_factory = loop.get_task_factory()
    background_exception_contexts: list[dict[str, object]] = []
    tracked_tasks: set[asyncio.Task[object]] = set()

    def tracking_task_factory(
        loop: asyncio.AbstractEventLoop,
        coro: object,
        **kwargs: object,
    ) -> asyncio.Task[object]:
        if previous_task_factory is not None:
            try:
                task = previous_task_factory(loop, coro, **kwargs)
            except TypeError:
                task = previous_task_factory(loop, coro)
        else:
            task = asyncio.Task(coro, loop=loop, **kwargs)
        tracked_tasks.add(task)
        return task

    def record_loop_exception(
        loop: asyncio.AbstractEventLoop,
        context: dict[str, object],
    ) -> None:
        background_exception_contexts.append(context)

    async def send_until_close(
        *, name: str, connection: ConnectionProtocol, payload_prefix: bytes
    ) -> None:
        index = 0
        while True:
            if close_started.is_set() and not connection.is_connected:
                return
            payload = payload_prefix + name.encode() + b":" + str(index).encode()
            try:
                await connection.send(payload)
            except (ConnectionClosedError, OSError, asyncio.TimeoutError):
                if close_started.is_set():
                    return
                raise
            send_counts[name] += 1
            index += 1
            await asyncio.sleep(0)

    def each_connection_observed_bidirectional_payloads(
        *,
        client_connections: tuple[ConnectionProtocol, ...],
        server_connection_ids: set[str],
    ) -> bool:
        return all(
            client_prefix in b"".join(server_recorder.bytes_by_connection[resource_id])
            for resource_id in server_connection_ids
        ) and all(
            server_prefix
            in b"".join(client_recorders[index].bytes_by_connection[connection.connection_id])
            for index, connection in enumerate(client_connections)
        )

    def is_aionetx_impl_task(task: asyncio.Task[object]) -> bool:
        coroutine = task.get_coro()
        code = getattr(coroutine, "cr_code", None)
        if code is None:
            code = getattr(coroutine, "gi_code", None)
        filename = getattr(code, "co_filename", "")
        return "aionetx/implementations/asyncio_impl" in filename.replace("\\", "/")

    def active_managed_tasks() -> list[asyncio.Task[object]]:
        current_task = asyncio.current_task()
        return [
            task
            for task in asyncio.all_tasks()
            if (
                task is not current_task
                and task not in baseline_tasks
                and not task.done()
                and (
                    task.get_name() == "event-dispatcher"
                    or task.get_name() == "tcp-client-supervisor"
                    or "tcp/client/" in task.get_name()
                    or "tcp/server/" in task.get_name()
                    or is_aionetx_impl_task(task)
                )
            )
        ]

    def completed_task_failures() -> list[BaseException]:
        failures: list[BaseException] = []
        for task in tracked_tasks:
            if not task.done() or task.cancelled():
                continue
            with contextlib.suppress(asyncio.CancelledError):
                failure = task.exception()
            if failure is not None:
                failures.append(failure)
        return failures

    def assert_ordered_terminal_sequence(events: list[str], connection_id: str) -> None:
        assert events.count("opened") == 1, (
            f"{connection_id} should publish exactly one opened event, got {events!r}."
        )
        assert events.count("closed") == 1, (
            f"{connection_id} should publish exactly one closed event, got {events!r}."
        )
        byte_indices = [index for index, name in enumerate(events) if name == "bytes"]
        assert byte_indices, f"{connection_id} should receive payload bytes before close."

        opened_index = events.index("opened")
        closed_index = events.index("closed")
        assert opened_index < min(byte_indices), (
            f"{connection_id} received payload before opened event: {events!r}."
        )
        assert max(byte_indices) < closed_index, (
            f"{connection_id} received payload after closed event: {events!r}."
        )

    loop.set_task_factory(tracking_task_factory)
    loop.set_exception_handler(record_loop_exception)
    await server.start()
    try:
        await asyncio.gather(*(client.start() for client in clients))
        client_connections = await asyncio.gather(
            *(client.wait_until_connected(timeout_seconds=5.0) for client in clients)
        )
        await wait_for_condition(
            lambda: len(server.connections) == num_clients,
            timeout_seconds=5.0,
        )
        server_connections = list(server.connections)
        client_connection_ids = [connection.connection_id for connection in client_connections]
        server_connection_ids = {connection.connection_id for connection in server_connections}

        for index, connection in enumerate(client_connections):
            send_tasks.append(
                asyncio.create_task(
                    send_until_close(
                        name=f"client-{index}",
                        connection=connection,
                        payload_prefix=client_prefix,
                    )
                )
            )
        for index, connection in enumerate(server_connections):
            send_tasks.append(
                asyncio.create_task(
                    send_until_close(
                        name=f"server-{index}",
                        connection=connection,
                        payload_prefix=server_prefix,
                    )
                )
            )

        await wait_for_condition(
            lambda: each_connection_observed_bidirectional_payloads(
                client_connections=tuple(client_connections),
                server_connection_ids=server_connection_ids,
            ),
            timeout_seconds=5.0,
        )
        assert all(not task.done() for task in send_tasks)

        close_started.set()
        stop_results = await asyncio.wait_for(
            asyncio.gather(
                server.stop(),
                *(client.stop() for client in clients),
                return_exceptions=True,
            ),
            timeout=5.0,
        )
        assert not [result for result in stop_results if isinstance(result, BaseException)]

        send_results = await asyncio.wait_for(
            asyncio.gather(*send_tasks, return_exceptions=True),
            timeout=5.0,
        )
        assert not [result for result in send_results if isinstance(result, BaseException)]

        await wait_for_condition(
            lambda: (
                len(server.connections) == 0
                and all(client.connection is None for client in clients)
            ),
            timeout_seconds=5.0,
        )
        await wait_for_condition(lambda: not active_managed_tasks(), timeout_seconds=5.0)
    finally:
        close_started.set()
        for task in send_tasks:
            if not task.done():
                task.cancel()
        if send_tasks:
            _ = await asyncio.gather(*send_tasks, return_exceptions=True)
        for client in clients:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await client.stop()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await server.stop()
        loop.set_task_factory(previous_task_factory)
        loop.set_exception_handler(previous_exception_handler)

    server_closed_counts = Counter(server_recorder.closed_ids)
    for connection_id in server_connection_ids:
        assert server_closed_counts[connection_id] == 1
        assert_ordered_terminal_sequence(
            server_recorder.events_by_connection[connection_id],
            connection_id,
        )
    for index, connection_id in enumerate(client_connection_ids):
        client_closed_counts = Counter(client_recorders[index].closed_ids)
        assert client_closed_counts[connection_id] == 1
        assert_ordered_terminal_sequence(
            client_recorders[index].events_by_connection[connection_id],
            connection_id,
        )

    assert server.lifecycle_state == ComponentLifecycleState.STOPPED
    assert all(client.lifecycle_state == ComponentLifecycleState.STOPPED for client in clients)
    assert len(server.connections) == 0
    assert all(client.connection is None for client in clients)
    assert all(count > 0 for count in send_counts.values())
    assert not completed_task_failures()
    assert background_exception_contexts == []
