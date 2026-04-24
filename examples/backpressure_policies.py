"""
Side-by-side demo of the three event backpressure policies.

Run:
    python examples/backpressure_policies.py

What this shows
---------------
aionetx delivers events to your handlers through a bounded queue. When
handlers are slower than the producer, you have to choose what to do with
the overflow. `EventBackpressurePolicy` picks the trade-off:

- BLOCK         - producer waits for queue space. Nothing is lost; latency
                  on the producer rises under sustained overload.
- DROP_OLDEST   - queue evicts the oldest pending event to make room.
                  Newest data survives; old data is lost.
- DROP_NEWEST   - the incoming event is dropped. Oldest data survives;
                  newest data is lost.

This example uses a UDP receiver per policy (datagram boundaries are
preserved, so each `send()` produces exactly one `on_bytes_received`
event) with a tiny event queue and a handler that sleeps per event. The
producer sends a burst faster than the handler can drain. The printed
output shows how the three policies diverge.
"""

from __future__ import annotations

import asyncio
import socket
import sys

from aionetx import (
    AsyncioNetworkFactory,
    BaseNetworkEventHandler,
    BytesReceivedEvent,
    EventDeliverySettings,
    UdpReceiverSettings,
    UdpSenderSettings,
)
from aionetx.api import EventBackpressurePolicy

QUEUE_CAPACITY = 2


def pick_free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SlowRecorder(BaseNetworkEventHandler):
    """Records each event after a small delay, slow enough to force overflow."""

    HANDLER_DELAY_SECONDS = 0.05

    def __init__(self) -> None:
        self.observed: list[bytes] = []

    async def on_bytes_received(self, event: BytesReceivedEvent) -> None:
        await asyncio.sleep(self.HANDLER_DELAY_SECONDS)
        self.observed.append(event.data)


async def run_policy(policy: EventBackpressurePolicy, burst: list[bytes]) -> list[bytes]:
    """
    Start a UDP receiver + sender under `policy`, send `burst`, return what
    the receiver handler actually observed.
    """
    port = pick_free_udp_port()
    factory = AsyncioNetworkFactory()

    recorder = SlowRecorder()
    receiver = factory.create_udp_receiver(
        settings=UdpReceiverSettings(
            host="127.0.0.1",
            port=port,
            event_delivery=EventDeliverySettings(
                backpressure_policy=policy,
                max_pending_events=QUEUE_CAPACITY,
            ),
        ),
        event_handler=recorder,
    )
    sender = factory.create_udp_sender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port),
    )

    await receiver.start()

    # Fire the burst concurrently so overflow is determined by the receiver's
    # queue policy rather than by an arbitrary "sleep long enough" delay.
    try:
        await asyncio.gather(*(sender.send(payload) for payload in burst))
        while True:
            stats = receiver.dispatcher_runtime_stats
            delivered_or_dropped = (
                len(recorder.observed)
                + stats.dropped_backpressure_oldest_total
                + stats.dropped_backpressure_newest_total
            )
            if delivered_or_dropped >= len(burst) and stats.queue_depth == 0:
                break
            await asyncio.sleep(0)
    finally:
        await sender.stop()
        await receiver.stop()

    return list(recorder.observed)


async def main() -> int:
    burst = [f"msg-{i:02d}".encode("ascii") for i in range(10)]
    print(f"producer burst: {[p.decode() for p in burst]}")
    print(f"(receiver queue size = {QUEUE_CAPACITY}, handler sleeps 50ms per event)\n")

    for policy in (
        EventBackpressurePolicy.BLOCK,
        EventBackpressurePolicy.DROP_OLDEST,
        EventBackpressurePolicy.DROP_NEWEST,
    ):
        observed = await run_policy(policy, burst)
        decoded = [b.decode() for b in observed]
        print(f"[{policy.value:>12}] handler observed {len(decoded)}/{len(burst)}: {decoded}")

    print(
        "\nNotes:"
        "\n  BLOCK keeps every datagram (producer waits for queue space)."
        "\n  DROP_OLDEST keeps the tail of the burst (freshest data wins)."
        "\n  DROP_NEWEST keeps the head of the burst (earliest data wins)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
