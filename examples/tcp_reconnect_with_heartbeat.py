"""
TCP client reconnect cycle with observable lifecycle/reconnect events.

Run:
    python examples/tcp_reconnect_with_heartbeat.py

What this shows
---------------
1. A client starts with reconnect enabled while no server is listening. The
   first few connection attempts fail and the client schedules retries with
   exponential backoff.
2. After a short delay the server is started on the same port. The client's
   next reconnect attempt succeeds.
3. The example observes the reconnect events (`attempt_started`,
   `attempt_failed`, `scheduled`) and prints them so you can see the full
   cycle, then sends one message and exits cleanly.

Heartbeats
----------
The client is configured with `TcpHeartbeatSettings(enabled=True, ...)` plus a
minimal heartbeat provider. Once the connection is up, the client asks the
provider every interval whether to send a keep-alive byte. This is how you
would keep idle TCP connections alive or detect zombie peers.
"""

from __future__ import annotations

import asyncio
import socket
import sys

from aionetx import (
    AsyncioNetworkFactory,
    BaseNetworkEventHandler,
    BytesReceivedEvent,
    HeartbeatProviderProtocol,
    TcpHeartbeatSettings,
    TcpReconnectSettings,
    TcpClientSettings,
    TcpServerSettings,
)
from aionetx.api import (
    ConnectionOpenedEvent,
    HeartbeatRequest,
    HeartbeatResult,
    ReconnectAttemptFailedEvent,
    ReconnectAttemptStartedEvent,
    ReconnectScheduledEvent,
)


def pick_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ClientObserver(BaseNetworkEventHandler):
    """Prints reconnect lifecycle events and signals when the client connects."""

    def __init__(self) -> None:
        self.connected = asyncio.Event()
        self.first_failure_seen = asyncio.Event()
        self.failed_attempts = 0

    async def on_reconnect_attempt_started(self, event: ReconnectAttemptStartedEvent) -> None:
        print(f"[reconnect] attempt #{event.attempt} starting")

    async def on_reconnect_attempt_failed(self, event: ReconnectAttemptFailedEvent) -> None:
        self.failed_attempts += 1
        self.first_failure_seen.set()
        print(
            f"[reconnect] attempt #{event.attempt} failed "
            f"(next delay: {event.next_delay_seconds}s) — {type(event.error).__name__}"
        )

    async def on_reconnect_scheduled(self, event: ReconnectScheduledEvent) -> None:
        print(f"[reconnect] scheduled attempt #{event.attempt} in {event.delay_seconds}s")

    async def on_connection_opened(self, event: ConnectionOpenedEvent) -> None:
        print(f"[connection] opened {event.resource_id}")
        self.connected.set()


class ServerHandler(BaseNetworkEventHandler):
    """Records the first payload the client sends after reconnecting."""

    def __init__(self) -> None:
        self.received = asyncio.Event()
        self.payload: bytes = b""

    async def on_bytes_received(self, event: BytesReceivedEvent) -> None:
        # Ignore heartbeat bytes; only store the real application message.
        if event.data == b"hb":
            return
        self.payload = event.data
        self.received.set()


class ConstantHeartbeatProvider(HeartbeatProviderProtocol):
    """
    Sends a 2-byte keep-alive on every tick. Production providers typically
    gate `should_send` on traffic-idle conditions or wall-clock deadlines.
    """

    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        return HeartbeatResult(should_send=True, payload=b"hb")


async def main() -> int:
    port = pick_free_tcp_port()
    factory = AsyncioNetworkFactory()

    observer = ClientObserver()
    client = factory.create_tcp_client(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=port,
            reconnect=TcpReconnectSettings(
                enabled=True,
                initial_delay_seconds=0.2,
                max_delay_seconds=1.0,
                backoff_factor=2.0,
            ),
            heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=0.5),
        ),
        event_handler=observer,
        heartbeat_provider=ConstantHeartbeatProvider(),
    )

    # Start client first. No server is listening yet, so initial attempts fail
    # and the client schedules retries.
    await client.start()

    # Wait until at least one attempt has failed before bringing the server
    # up. That keeps the "reconnect cycle" part of the example deterministic
    # across platforms (connect refusal latency varies).
    await asyncio.wait_for(observer.first_failure_seen.wait(), timeout=5.0)

    # Now bring the server up. The next reconnect attempt will succeed.
    server_handler = ServerHandler()
    server = factory.create_tcp_server(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=server_handler,
    )
    await server.start()
    await server.wait_until_running(timeout_seconds=5.0)
    print("[server] up; waiting for client to reconnect")

    connection = await client.wait_until_connected(timeout_seconds=5.0)
    await asyncio.wait_for(observer.connected.wait(), timeout=5.0)

    # Send one message over the now-established connection.
    await connection.send(b"ready-after-reconnect")
    await asyncio.wait_for(server_handler.received.wait(), timeout=5.0)

    assert server_handler.payload == b"ready-after-reconnect", server_handler.payload
    assert observer.failed_attempts >= 1, "expected at least one failed attempt"
    print(
        f"reconnected after {observer.failed_attempts} failed attempt(s); "
        f"server received {server_handler.payload!r}"
    )

    await client.stop()
    await server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
