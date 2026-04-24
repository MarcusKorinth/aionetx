"""
Backpressure behavior sanity scenario under a deliberately slow handler.

This is a policy-shape check, not a benchmark:
- BLOCK should preserve more payload bytes under pressure.
- DROP_NEWEST should preserve ingest progress by dropping.
"""

from __future__ import annotations

import asyncio
import logging
import socket

import pytest

from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.event_delivery_settings import (
    EventBackpressurePolicy,
    EventDeliverySettings,
)
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer


class SlowBytesHandler:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.bytes_received = 0
        self._bytes_changed = asyncio.Event()

    async def on_event(self, event) -> None:
        if isinstance(event, BytesReceivedEvent):
            self.bytes_received += len(event.data)
            self._bytes_changed.set()
            await asyncio.sleep(self.delay_seconds)

    async def wait_for_quiescence(
        self, *, idle_window_seconds: float, timeout_seconds: float
    ) -> None:
        """Wait until byte totals stop changing for one idle window."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            self._bytes_changed.clear()
            try:
                await asyncio.wait_for(
                    self._bytes_changed.wait(),
                    timeout=min(idle_window_seconds, remaining),
                )
            except asyncio.TimeoutError:
                return


def get_unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _run_stream_load(backpressure_policy: EventBackpressurePolicy) -> int:
    port = get_unused_tcp_port()
    handler = SlowBytesHandler(delay_seconds=0.01)
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=64,
            receive_buffer_size=256,
            event_delivery=EventDeliverySettings(
                max_pending_events=2,
                backpressure_policy=backpressure_policy,
            ),
        ),
        event_handler=handler,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        payload = b"x" * 256
        for _ in range(300):
            writer.write(payload)
            await writer.drain()
        writer.close()
        await writer.wait_closed()
        await reader.read()
        await handler.wait_for_quiescence(
            idle_window_seconds=0.05,
            timeout_seconds=2.0,
        )
    finally:
        await server.stop()
    stats = server.dispatcher_runtime_stats
    if backpressure_policy == EventBackpressurePolicy.BLOCK:
        assert stats.dropped_backpressure_newest_total == 0
        assert stats.dropped_backpressure_oldest_total == 0
    if backpressure_policy == EventBackpressurePolicy.DROP_NEWEST:
        assert stats.dropped_backpressure_newest_total > 0
    return handler.bytes_received


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.reliability_smoke
async def test_block_policy_prioritizes_delivery_over_ingest_latency() -> None:
    # Relative-envelope check only:
    # - BLOCK should sacrifice ingest latency for delivery completeness.
    # - DROP_NEWEST should preserve ingest progress by dropping under pressure.
    block_bytes = await _run_stream_load(EventBackpressurePolicy.BLOCK)
    drop_bytes = await _run_stream_load(EventBackpressurePolicy.DROP_NEWEST)

    assert block_bytes > drop_bytes


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.reliability_smoke
async def test_drop_newest_policy_emits_drop_signals_while_block_policy_does_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    split_index = len(caplog.records)

    await _run_stream_load(EventBackpressurePolicy.BLOCK)
    block_run_records = caplog.records[split_index:]
    block_drop_newest_logs = [
        record
        for record in block_run_records
        if "Dropped newest event due to backpressure." in record.getMessage()
    ]
    assert block_drop_newest_logs == []

    split_index = len(caplog.records)
    await _run_stream_load(EventBackpressurePolicy.DROP_NEWEST)
    drop_run_records = caplog.records[split_index:]
    drop_newest_logs = [
        record
        for record in drop_run_records
        if "Dropped events due to backpressure (drop_newest" in record.getMessage()
    ]
    assert len(drop_newest_logs) >= 1
