#!/usr/bin/env python3
"""
Small local baseline harness for TCP event-delivery measurements.

The numbers emitted by this script are local observations only. They are not
API guarantees, CI thresholds, or cross-platform performance claims.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import socket
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from aionetx.api.bytes_received_event import BytesReceivedEvent  # noqa: E402
from aionetx.api.event_delivery_settings import (  # noqa: E402
    EventBackpressurePolicy,
    EventDeliverySettings,
    EventDispatchMode,
)
from aionetx.api.tcp_server import TcpServerSettings  # noqa: E402
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import (  # noqa: E402
    AsyncioTcpServer,
)


BENCHMARK_NAME = "tcp-event-delivery-baseline"


@dataclass(frozen=True, slots=True)
class BenchmarkSettings:
    dispatch_mode: str
    backpressure_policy: str
    payload_count: int
    payload_size_bytes: int
    max_pending_events: int


class CountingBytesHandler:
    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.bytes_received = 0
        self.bytes_events = 0
        self._bytes_changed = asyncio.Event()

    async def on_event(self, event: Any) -> None:
        if isinstance(event, BytesReceivedEvent):
            self.bytes_received += len(event.data)
            self.bytes_events += 1
            self._bytes_changed.set()
            if self.delay_seconds > 0.0:
                await asyncio.sleep(self.delay_seconds)

    async def wait_for_quiescence(
        self, *, idle_window_seconds: float, timeout_seconds: float
    ) -> None:
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


def _get_unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    event_delivery = EventDeliverySettings(
        dispatch_mode=EventDispatchMode(args.dispatch_mode),
        backpressure_policy=EventBackpressurePolicy(args.backpressure_policy),
        max_pending_events=args.max_pending_events,
    )
    settings = BenchmarkSettings(
        dispatch_mode=args.dispatch_mode,
        backpressure_policy=args.backpressure_policy,
        payload_count=args.payload_count,
        payload_size_bytes=args.payload_size,
        max_pending_events=args.max_pending_events,
    )
    handler = CountingBytesHandler(delay_seconds=args.handler_delay_seconds)
    port = _get_unused_tcp_port()
    server = AsyncioTcpServer(
        settings=TcpServerSettings(
            host="127.0.0.1",
            port=port,
            max_connections=1,
            receive_buffer_size=args.receive_buffer_size,
            event_delivery=event_delivery,
        ),
        event_handler=handler,
    )

    await server.start()
    started_at = perf_counter()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        payload = b"x" * args.payload_size
        for _ in range(args.payload_count):
            writer.write(payload)
            await writer.drain()

        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(reader.read(), timeout=args.connection_close_timeout_seconds)
        await handler.wait_for_quiescence(
            idle_window_seconds=args.idle_window_seconds,
            timeout_seconds=args.quiescence_timeout_seconds,
        )
    finally:
        await server.stop()

    duration_seconds = perf_counter() - started_at
    bytes_sent = args.payload_count * args.payload_size
    stats = server.dispatcher_runtime_stats

    return {
        "benchmark": BENCHMARK_NAME,
        "context": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "event_loop": type(asyncio.get_running_loop()).__name__,
        },
        "settings": asdict(settings),
        "metrics": {
            "duration_seconds": duration_seconds,
            "payload_events_received": handler.bytes_events,
            "bytes_sent": bytes_sent,
            "bytes_received": handler.bytes_received,
            "throughput_bytes_per_second": bytes_sent / duration_seconds,
            "emit_calls_total": stats.emit_calls_total,
            "enqueued_total": stats.enqueued_total,
            "handler_dispatch_attempts_total": stats.handler_dispatch_attempts_total,
            "handler_failures_total": stats.handler_failures_total,
            "inline_fallback_total": stats.inline_fallback_total,
            "dropped_backpressure_oldest_total": stats.dropped_backpressure_oldest_total,
            "dropped_backpressure_newest_total": stats.dropped_backpressure_newest_total,
            "dropped_stop_phase_total": stats.dropped_stop_phase_total,
            "queue_depth": stats.queue_depth,
            "queue_peak": stats.queue_peak,
        },
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0.0:
        raise argparse.ArgumentTypeError("must be greater than or equal to zero")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a small local TCP event-delivery baseline. Output is "
            "non-contractual and intended for before/after comparisons."
        )
    )
    parser.add_argument("--payload-count", type=_positive_int, default=1000)
    parser.add_argument("--payload-size", type=_positive_int, default=256)
    parser.add_argument("--receive-buffer-size", type=_positive_int, default=4096)
    parser.add_argument("--max-pending-events", type=_positive_int, default=1024)
    parser.add_argument(
        "--dispatch-mode",
        choices=[mode.value for mode in EventDispatchMode],
        default=EventDispatchMode.BACKGROUND.value,
    )
    parser.add_argument(
        "--backpressure-policy",
        choices=[policy.value for policy in EventBackpressurePolicy],
        default=EventBackpressurePolicy.BLOCK.value,
    )
    parser.add_argument(
        "--handler-delay-seconds",
        type=_non_negative_float,
        default=0.0,
        help="Optional per-bytes-event handler delay for backpressure experiments.",
    )
    parser.add_argument("--idle-window-seconds", type=_non_negative_float, default=0.05)
    parser.add_argument("--quiescence-timeout-seconds", type=_non_negative_float, default=5.0)
    parser.add_argument(
        "--connection-close-timeout-seconds",
        type=_non_negative_float,
        default=5.0,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object instead of a human-readable summary.",
    )
    return parser


def _format_text(result: dict[str, Any]) -> str:
    context = result["context"]
    settings = result["settings"]
    metrics = result["metrics"]
    lines = [
        f"Benchmark: {result['benchmark']}",
        "Context:",
        f"  python_version: {context['python_version']}",
        f"  python_implementation: {context['python_implementation']}",
        f"  platform: {context['platform']}",
        f"  event_loop: {context['event_loop']}",
        "Settings:",
        f"  dispatch_mode: {settings['dispatch_mode']}",
        f"  backpressure_policy: {settings['backpressure_policy']}",
        f"  payload_count: {settings['payload_count']}",
        f"  payload_size_bytes: {settings['payload_size_bytes']}",
        f"  max_pending_events: {settings['max_pending_events']}",
        "Metrics:",
    ]
    for key in sorted(metrics):
        value = metrics[key]
        if isinstance(value, float):
            lines.append(f"  {key}: {value:.6f}")
        else:
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    result = asyncio.run(_run_benchmark(args))
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(_format_text(result))


if __name__ == "__main__":
    main()
