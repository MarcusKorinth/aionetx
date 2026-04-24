"""
UDP datagram round trip: receiver consumes one datagram sent by a sender.

Run:
    python examples/udp_round_trip.py

This example binds a UDP receiver to an ephemeral loopback port, creates a
sender that targets that port, sends a single datagram, waits until the
receiver has observed it, then shuts everything down. Exits 0 on success.
"""

from __future__ import annotations

import asyncio
import socket
import sys

from aionetx import (
    AsyncioNetworkFactory,
    BaseNetworkEventHandler,
    BytesReceivedEvent,
    UdpReceiverSettings,
    UdpSenderSettings,
)


def pick_free_udp_port() -> int:
    """Ask the OS for a free UDP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RecordingHandler(BaseNetworkEventHandler):
    def __init__(self) -> None:
        self.received = asyncio.Event()
        self.payload: bytes = b""
        self.remote: tuple[str | None, int | None] = (None, None)

    async def on_bytes_received(self, event: BytesReceivedEvent) -> None:
        self.payload = event.data
        self.remote = (event.remote_host, event.remote_port)
        self.received.set()


async def main() -> int:
    port = pick_free_udp_port()
    factory = AsyncioNetworkFactory()

    handler = RecordingHandler()
    receiver = factory.create_udp_receiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=port),
        event_handler=handler,
    )
    sender = factory.create_udp_sender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port),
    )

    await receiver.start()
    try:
        await sender.send(b"datagram-payload")
        await asyncio.wait_for(handler.received.wait(), timeout=5.0)
    finally:
        await sender.stop()
        await receiver.stop()

    assert handler.payload == b"datagram-payload", handler.payload
    print(f"received {handler.payload!r} from {handler.remote}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
