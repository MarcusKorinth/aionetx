from __future__ import annotations

import asyncio
import socket
from collections import defaultdict
from collections.abc import Callable

from aionetx import (
    AsyncioNetworkFactory,
    BytesReceivedEvent,
    TcpHeartbeatSettings,
    TcpReconnectSettings,
    TcpClientSettings,
    TcpServerSettings,
)
from aionetx.api import (
    ComponentLifecycleChangedEvent,
    ComponentLifecycleState,
    ConnectionOpenedEvent,
    HeartbeatRequest,
    HeartbeatResult,
    NetworkEvent,
    ReconnectAttemptFailedEvent,
)


class SemanticEventHandler:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self.events: list[NetworkEvent] = []
        self.received_payloads: list[bytes] = []

    async def on_event(self, event: NetworkEvent) -> None:
        async with self._condition:
            self.events.append(event)
            if isinstance(event, BytesReceivedEvent):
                self.received_payloads.append(event.data)
            self._condition.notify_all()


class _StaticHeartbeatProvider:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        del request
        return HeartbeatResult(should_send=True, payload=self._payload)


async def _wait_for_condition(
    handler: SemanticEventHandler,
    predicate: Callable[[], bool],
    *,
    timeout: float,
    description: str,
) -> None:
    try:
        async with handler._condition:
            await asyncio.wait_for(handler._condition.wait_for(predicate), timeout=timeout)
    except TimeoutError as error:
        raise AssertionError(
            f"installed artifact semantic mini-suite missing expected condition: {description}"
        ) from error


def _get_unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _lifecycle_sequences(handler: SemanticEventHandler) -> dict[str, list[ComponentLifecycleState]]:
    by_resource: dict[str, list[ComponentLifecycleState]] = defaultdict(list)
    for event in handler.events:
        if isinstance(event, ComponentLifecycleChangedEvent):
            by_resource[event.resource_id].append(event.current)
    return dict(by_resource)


def _has_ordered_subsequence(
    sequence: list[ComponentLifecycleState], expected: list[ComponentLifecycleState]
) -> bool:
    index = 0
    for state in sequence:
        if state == expected[index]:
            index += 1
            if index == len(expected):
                return True
    return False


async def _assert_lifecycle_and_order_semantics(factory: AsyncioNetworkFactory) -> None:
    handler = SemanticEventHandler()
    port = _get_unused_tcp_port()

    server = factory.create_tcp_server(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=handler,
    )
    client = factory.create_tcp_client(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=handler,
    )

    await server.start()
    await client.start()
    try:
        connection = await client.wait_until_connected(timeout_seconds=3.0)
        payloads = [b"mini-suite-order-1", b"mini-suite-order-2", b"mini-suite-order-3"]
        for payload in payloads:
            await connection.send(payload)
            await _wait_for_condition(
                handler,
                lambda payload=payload: payload in handler.received_payloads,
                timeout=3.0,
                description=f"ordered byte receive for {payload!r}",
            )

        received_subset = [p for p in handler.received_payloads if p in payloads]
        assert received_subset[: len(payloads)] == payloads, (
            "expected in-order sequential bytes delivery for single connection, "
            f"got {received_subset[: len(payloads)]!r}"
        )
    finally:
        await client.stop()
        await server.stop()

    expected_lifecycle = [
        ComponentLifecycleState.STARTING,
        ComponentLifecycleState.RUNNING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    ]
    lifecycle_by_resource = _lifecycle_sequences(handler)
    assert any(
        _has_ordered_subsequence(states, expected_lifecycle)
        for states in lifecycle_by_resource.values()
    ), (
        "expected at least one managed transport lifecycle STARTING→RUNNING→STOPPING→STOPPED sequence"
    )


async def _assert_reconnect_failure_and_recovery(factory: AsyncioNetworkFactory) -> None:
    handler = SemanticEventHandler()
    port = _get_unused_tcp_port()

    client = factory.create_tcp_client(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(
                enabled=True,
                initial_delay_seconds=0.05,
                max_delay_seconds=0.1,
                backoff_factor=1.0,
            ),
        ),
        event_handler=handler,
    )

    await client.start()
    server = factory.create_tcp_server(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=handler,
    )
    try:
        await _wait_for_condition(
            handler,
            lambda: any(isinstance(event, ReconnectAttemptFailedEvent) for event in handler.events),
            timeout=3.0,
            description="reconnect failure before server starts",
        )

        await server.start()
        connection = await client.wait_until_connected(timeout_seconds=3.0)
        payload = b"mini-suite-reconnect-recovered"
        await connection.send(payload)

        await _wait_for_condition(
            handler,
            lambda: payload in handler.received_payloads,
            timeout=3.0,
            description="reconnect recovery with successful send",
        )

        assert any(isinstance(event, ConnectionOpenedEvent) for event in handler.events), (
            "expected a connection-opened event after reconnect recovery"
        )
    finally:
        await client.stop()
        await server.stop()


async def _assert_heartbeat_semantics(factory: AsyncioNetworkFactory) -> None:
    handler = SemanticEventHandler()
    port = _get_unused_tcp_port()
    heartbeat_payload = b"mini-suite-heartbeat"

    server = factory.create_tcp_server(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=64,
            heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.05),
        ),
        heartbeat_provider=_StaticHeartbeatProvider(heartbeat_payload),
        event_handler=handler,
    )
    client = factory.create_tcp_client(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=handler,
    )

    await server.start()
    await client.start()
    try:
        await client.wait_until_connected(timeout_seconds=3.0)
        await _wait_for_condition(
            handler,
            lambda: heartbeat_payload in handler.received_payloads,
            timeout=3.0,
            description="heartbeat payload emission",
        )
    finally:
        await client.stop()
        await server.stop()


async def main() -> None:
    factory = AsyncioNetworkFactory()
    await _assert_lifecycle_and_order_semantics(factory)
    await _assert_reconnect_failure_and_recovery(factory)
    await _assert_heartbeat_semantics(factory)


if __name__ == "__main__":
    asyncio.run(main())
