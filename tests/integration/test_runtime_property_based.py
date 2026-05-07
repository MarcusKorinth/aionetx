"""
Bounded property-style runtime tests for malformed-peer transport invariants.

These tests are opt-in and require the ``dev-hypothesis`` extra.  They drive
real TCP and UDP receive paths with generated raw byte payloads while keeping
example counts and payload sizes small enough for the non-blocking CI lane.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

pytestmark = [pytest.mark.hypothesis, pytest.mark.integration]

hypothesis = pytest.importorskip(
    "hypothesis", reason="hypothesis not installed; skipping runtime property tests"
)

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from aionetx.api.bytes_received_event import BytesReceivedEvent  # noqa: E402
from aionetx.api.component_lifecycle_state import ComponentLifecycleState  # noqa: E402
from aionetx.api.tcp_server import TcpServerSettings  # noqa: E402
from aionetx.api.udp import UdpReceiverSettings  # noqa: E402
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import (  # noqa: E402
    AsyncioTcpServer,
)
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import (  # noqa: E402
    AsyncioUdpReceiver,
)
from aionetx.testing import RecordingEventHandler, wait_for_condition  # noqa: E402

TCP_MAX_EXAMPLES = 12
UDP_MAX_EXAMPLES = 12
UDP_RECEIVE_BUFFER_SIZE = 128


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _unused_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _run_with_exception_capture(test_body: Callable[[], Awaitable[None]]) -> None:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unexpected_contexts: list[dict[str, Any]] = []

    def capture_exception(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        unexpected_contexts.append(context)

    loop.set_exception_handler(capture_exception)
    try:
        await test_body()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert unexpected_contexts == []


def _assert_component_lifecycle_sequence(
    handler: RecordingEventHandler,
    resource_id: str,
) -> None:
    observed = [
        event.current for event in handler.lifecycle_events if event.resource_id == resource_id
    ]
    assert observed == [
        ComponentLifecycleState.STARTING,
        ComponentLifecycleState.RUNNING,
        ComponentLifecycleState.STOPPING,
        ComponentLifecycleState.STOPPED,
    ]


def _assert_no_bytes_after_component_stopped(
    handler: RecordingEventHandler,
    component_resource_id: str,
) -> None:
    stopped_index = next(
        index
        for index, event in enumerate(handler.events)
        if (
            getattr(event, "resource_id", None) == component_resource_id
            and getattr(event, "current", None) == ComponentLifecycleState.STOPPED
        )
    )
    assert not any(
        isinstance(event, BytesReceivedEvent) for event in handler.events[stopped_index + 1 :]
    )


def test_tcp_server_preserves_invariants_for_generated_stream_chunks() -> None:
    @given(
        chunks=st.lists(st.binary(min_size=1, max_size=512), min_size=1, max_size=6),
        abort_after_drain=st.booleans(),
    )
    @settings(max_examples=TCP_MAX_EXAMPLES, deadline=None)
    def run_example(chunks: list[bytes], abort_after_drain: bool) -> None:
        asyncio.run(_exercise_tcp_server(chunks, abort_after_drain=abort_after_drain))

    run_example()


async def _exercise_tcp_server(chunks: list[bytes], *, abort_after_drain: bool) -> None:
    async def body() -> None:
        port = _unused_tcp_port()
        handler = RecordingEventHandler()
        server = AsyncioTcpServer(
            settings=TcpServerSettings(
                host="127.0.0.1",
                port=port,
                max_connections=4,
                receive_buffer_size=64,
            ),
            event_handler=handler,
        )
        component_id = f"tcp/server/127.0.0.1/{port}"
        expected_stream = b"".join(chunks)
        tasks_before = set(asyncio.all_tasks())

        await server.start()
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await wait_for_condition(lambda: len(server.connections) == 1, timeout_seconds=2.0)
            server_connection_id = server.connections[0].connection_id

            for chunk in chunks:
                writer.write(chunk)
                await writer.drain()

            if abort_after_drain:
                writer.transport.abort()
            else:
                writer.close()
                await writer.wait_closed()

            await wait_for_condition(
                lambda: (
                    len(b"".join(handler.received_by_connection[server_connection_id]))
                    >= len(expected_stream)
                    if not abort_after_drain
                    else bool(handler.closed_events)
                ),
                timeout_seconds=2.0,
            )

            received_stream = b"".join(handler.received_by_connection[server_connection_id])
            if abort_after_drain:
                assert expected_stream.startswith(received_stream)
            else:
                assert received_stream == expected_stream
        finally:
            await server.stop()

        assert server.lifecycle_state == ComponentLifecycleState.STOPPED
        _assert_component_lifecycle_sequence(handler, component_id)
        _assert_no_bytes_after_component_stopped(handler, component_id)
        current_task = asyncio.current_task()
        leaked = asyncio.all_tasks() - tasks_before - ({current_task} if current_task else set())
        assert not leaked

    await _run_with_exception_capture(body)


def test_udp_receiver_preserves_invariants_for_generated_datagrams() -> None:
    @given(datagram_bodies=st.lists(st.binary(max_size=512), min_size=1, max_size=8))
    @settings(max_examples=UDP_MAX_EXAMPLES, deadline=None)
    def run_example(datagram_bodies: list[bytes]) -> None:
        asyncio.run(_exercise_udp_receiver(datagram_bodies))

    run_example()


async def _exercise_udp_receiver(datagram_bodies: list[bytes]) -> None:
    async def body() -> None:
        port = _unused_udp_port()
        handler = RecordingEventHandler()
        receiver = AsyncioUdpReceiver(
            settings=UdpReceiverSettings(
                host="127.0.0.1",
                port=port,
                receive_buffer_size=UDP_RECEIVE_BUFFER_SIZE,
            ),
            event_handler=handler,
        )
        component_id = f"udp/receiver/127.0.0.1/{port}"
        datagrams = [index.to_bytes(2, "big") + body for index, body in enumerate(datagram_bodies)]
        tasks_before = set(asyncio.all_tasks())

        raw_peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        raw_peer.setblocking(False)
        try:
            await receiver.start()
            loop = asyncio.get_running_loop()
            for datagram in datagrams:
                await loop.sock_sendto(raw_peer, datagram, ("127.0.0.1", port))

            await wait_for_condition(
                lambda: len(handler.received_by_connection[component_id]) >= len(datagrams),
                timeout_seconds=2.0,
            )
        finally:
            raw_peer.close()
            await receiver.stop()

        received = handler.received_by_connection[component_id]
        assert len(received) == len(datagrams)
        for emitted, original in zip(received, datagrams, strict=True):
            assert original.startswith(emitted)
            assert 0 < len(emitted) <= UDP_RECEIVE_BUFFER_SIZE

        assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
        assert handler.error_events == []
        _assert_component_lifecycle_sequence(handler, component_id)
        _assert_no_bytes_after_component_stopped(handler, component_id)
        current_task = asyncio.current_task()
        leaked = asyncio.all_tasks() - tasks_before - ({current_task} if current_task else set())
        assert not leaked

    await _run_with_exception_capture(body)
