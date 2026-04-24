"""
Length-prefixed TCP framing with a typed message dispatcher.

Run:
    python examples/tcp_framing_length_prefix.py

Scope of this example
---------------------
aionetx is a raw-byte transport: `on_bytes_received` gives you whatever
chunk the OS decided to hand over. TCP has no message boundaries, so
realistic applications add a framing layer on top. This example shows the
most common one: a fixed-size header that carries the payload length, with
a central dispatcher that turns complete frames into typed messages.

Wire format
-----------
Every frame starts with an 8-byte `CommonMessageHeader`, followed by a
payload of exactly `payload_length` bytes:

    +----------------+----------------+-------------------------+
    | type_id (u32)  | payload_length | payload (JSON-encoded)  |
    |  big endian    |    (u32 be)    |    <payload_length>B    |
    +----------------+----------------+-------------------------+

`struct.pack(">II", type_id, payload_length)` produces the header. Network
byte order (big endian) is used because that is the standard on the wire.

Payloads are JSON here to keep the example self-contained. In production,
you would typically use a binary schema (protobuf, MessagePack, CBOR,
custom struct layout, ...); the framing+dispatcher pattern is the same.

Shape of the example
--------------------
- Five message types with 3-7 fields each (`Heartbeat`,
  `TelemetryUpdate`, `CommandRequest`, `CommandAck`, `EventNotification`).
- A central `MessageDispatcher` maps type_id to typed handlers.
- `FramingHandler` owns a per-connection buffer, pulls out complete
  frames, and routes them to the dispatcher. This is the piece you would
  lift into production code: aionetx gives you bytes, the framing handler
  turns them into messages, the dispatcher turns messages into work.
"""

from __future__ import annotations

import asyncio
import json
import random
import socket
import struct
import sys
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from aionetx import (
    AsyncioNetworkFactory,
    BaseNetworkEventHandler,
    BytesReceivedEvent,
    TcpClientSettings,
    TcpServerSettings,
)

# ---------------------------------------------------------------------------
# Wire framing
# ---------------------------------------------------------------------------

HEADER_STRUCT = struct.Struct(">II")  # type_id, payload_length (both u32 BE)
HEADER_SIZE = HEADER_STRUCT.size  # 8 bytes
MAX_PAYLOAD_BYTES = 1 << 20  # defensive cap: reject 1+ MiB frames


@dataclass(frozen=True, slots=True)
class CommonMessageHeader:
    """Fixed 8-byte header that precedes every message on the wire."""

    type_id: int
    payload_length: int

    def pack(self) -> bytes:
        return HEADER_STRUCT.pack(self.type_id, self.payload_length)

    @staticmethod
    def unpack(buffer: bytes) -> "CommonMessageHeader":
        type_id, payload_length = HEADER_STRUCT.unpack(buffer)
        return CommonMessageHeader(type_id=type_id, payload_length=payload_length)


# ---------------------------------------------------------------------------
# Message catalog
# ---------------------------------------------------------------------------


class MessageType:
    HEARTBEAT = 1
    TELEMETRY_UPDATE = 2
    COMMAND_REQUEST = 3
    COMMAND_ACK = 4
    EVENT_NOTIFICATION = 5


@dataclass(frozen=True, slots=True)
class Heartbeat:
    sequence: int
    sender_id: str
    monotonic_ms: int


@dataclass(frozen=True, slots=True)
class TelemetryUpdate:
    sensor_id: str
    timestamp_ms: int
    temperature_celsius: float
    pressure_kpa: float
    battery_percent: int


@dataclass(frozen=True, slots=True)
class CommandRequest:
    command_id: str
    operator: str
    target: str
    arguments: list[str]
    priority: int


@dataclass(frozen=True, slots=True)
class CommandAck:
    command_id: str
    accepted: bool
    reason: str
    executor: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class EventNotification:
    event_id: str
    severity: str
    category: str
    source: str
    message: str
    occurred_at_ms: int
    correlation_id: str


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

# Each message type maps to its dataclass so the framing layer can reconstruct
# typed objects from the payload. A production codebase would use a proper
# schema/codec library for this; JSON + dataclasses keeps the example clear.
MESSAGE_TYPE_TO_CLASS: dict[int, type] = {
    MessageType.HEARTBEAT: Heartbeat,
    MessageType.TELEMETRY_UPDATE: TelemetryUpdate,
    MessageType.COMMAND_REQUEST: CommandRequest,
    MessageType.COMMAND_ACK: CommandAck,
    MessageType.EVENT_NOTIFICATION: EventNotification,
}


def encode_message(message: Any) -> bytes:
    """Encode `message` into header + JSON payload."""
    type_id = next(tid for tid, cls in MESSAGE_TYPE_TO_CLASS.items() if isinstance(message, cls))
    payload = json.dumps(asdict(message)).encode("utf-8")
    header = CommonMessageHeader(type_id=type_id, payload_length=len(payload))
    return header.pack() + payload


def decode_message(type_id: int, payload: bytes) -> Any:
    """Decode `payload` back into the dataclass registered for `type_id`."""
    message_class = MESSAGE_TYPE_TO_CLASS[type_id]
    fields = json.loads(payload.decode("utf-8"))
    return message_class(**fields)


# ---------------------------------------------------------------------------
# Central dispatcher
# ---------------------------------------------------------------------------


MessageHandler = Callable[[Any], Awaitable[None]]


class MessageDispatcher:
    """
    Routes typed messages to per-type async handlers.

    This is deliberately small: a dict of `type_id -> handler`. Even so, the
    shape scales: add a new message type, register its handler, done. Keeping
    dispatch central means the rest of the codebase never isinstance-chains
    over message variants.
    """

    def __init__(self) -> None:
        self._handlers: dict[int, MessageHandler] = {}

    def register(self, type_id: int, handler: MessageHandler) -> None:
        self._handlers[type_id] = handler

    async def dispatch(self, type_id: int, message: Any) -> None:
        handler = self._handlers.get(type_id)
        if handler is None:
            print(f"[dispatcher] no handler registered for type_id={type_id}")
            return
        await handler(message)


# ---------------------------------------------------------------------------
# Framing handler: bytes -> frames -> dispatcher
# ---------------------------------------------------------------------------


class FramingHandler(BaseNetworkEventHandler):
    """
    Converts a raw byte stream into complete frames, one connection at a time.

    Responsibilities:
    1. Maintain a per-connection `bytearray` buffer (aionetx does not frame
       anything for you).
    2. Pull out every complete frame the buffer currently contains.
    3. Decode each frame into a typed message and hand it to the dispatcher.
    """

    def __init__(self, dispatcher: MessageDispatcher) -> None:
        self._dispatcher = dispatcher
        self._buffers: dict[str, bytearray] = {}

    async def on_bytes_received(self, event: BytesReceivedEvent) -> None:
        buffer = self._buffers.setdefault(event.resource_id, bytearray())
        buffer.extend(event.data)

        while True:
            if len(buffer) < HEADER_SIZE:
                return  # not enough bytes for a header yet

            header = CommonMessageHeader.unpack(bytes(buffer[:HEADER_SIZE]))
            if header.payload_length > MAX_PAYLOAD_BYTES:
                # Defensive: a malformed or hostile peer could claim a huge
                # length. Production code should close the connection and log.
                print(
                    f"[framing] rejecting oversize frame "
                    f"({header.payload_length} > {MAX_PAYLOAD_BYTES})"
                )
                buffer.clear()
                return

            frame_end = HEADER_SIZE + header.payload_length
            if len(buffer) < frame_end:
                return  # header parsed, payload still incomplete

            payload = bytes(buffer[HEADER_SIZE:frame_end])
            del buffer[:frame_end]

            try:
                message = decode_message(header.type_id, payload)
            except Exception as exc:  # pragma: no cover - example-only path
                print(f"[framing] decode failed for type_id={header.type_id}: {exc}")
                continue
            await self._dispatcher.dispatch(header.type_id, message)


# ---------------------------------------------------------------------------
# Per-type handlers
# ---------------------------------------------------------------------------


async def handle_heartbeat(message: Heartbeat) -> None:
    print(f"[heartbeat] seq={message.sequence} sender={message.sender_id!r}")
    # here dispatch to your liveness/watchdog subsystem


async def handle_telemetry(message: TelemetryUpdate) -> None:
    print(
        f"[telemetry] sensor={message.sensor_id!r} "
        f"temp={message.temperature_celsius}C pressure={message.pressure_kpa}kPa"
    )
    # here dispatch to your metrics/telemetry pipeline


async def handle_command_request(message: CommandRequest) -> None:
    print(
        f"[command.request] id={message.command_id!r} "
        f"operator={message.operator!r} target={message.target!r}"
    )
    # here dispatch to your command executor with the typed request


async def handle_command_ack(message: CommandAck) -> None:
    print(
        f"[command.ack] id={message.command_id!r} accepted={message.accepted} "
        f"latency_ms={message.latency_ms}"
    )
    # here dispatch the ack back to the originating command futures map


async def handle_event_notification(message: EventNotification) -> None:
    print(
        f"[event] id={message.event_id!r} severity={message.severity!r} "
        f"source={message.source!r} message={message.message!r}"
    )
    # here dispatch to your alerting / event bus


# ---------------------------------------------------------------------------
# Random message factory (one of each type)
# ---------------------------------------------------------------------------


def build_random_message_sequence() -> list[Any]:
    """Return one randomized instance of every message type, order-shuffled."""
    rng = random.Random(0xA10E)  # deterministic so smoke output is stable
    messages: list[Any] = [
        Heartbeat(
            sequence=rng.randint(1, 10_000),
            sender_id=f"node-{rng.randint(1, 32)}",
            monotonic_ms=rng.randint(0, 10_000_000),
        ),
        TelemetryUpdate(
            sensor_id=f"sensor-{rng.randint(1, 16)}",
            timestamp_ms=rng.randint(1_700_000_000_000, 1_800_000_000_000),
            temperature_celsius=round(rng.uniform(-20.0, 120.0), 2),
            pressure_kpa=round(rng.uniform(80.0, 120.0), 2),
            battery_percent=rng.randint(0, 100),
        ),
        CommandRequest(
            command_id=f"cmd-{rng.randint(1000, 9999)}",
            operator=f"op-{rng.randint(1, 8)}",
            target=f"actuator-{rng.randint(1, 4)}",
            arguments=[f"arg{i}" for i in range(rng.randint(1, 3))],
            priority=rng.randint(0, 9),
        ),
        CommandAck(
            command_id=f"cmd-{rng.randint(1000, 9999)}",
            accepted=bool(rng.getrandbits(1)),
            reason=rng.choice(["ok", "rejected", "queued"]),
            executor=f"executor-{rng.randint(1, 4)}",
            latency_ms=rng.randint(0, 500),
        ),
        EventNotification(
            event_id=f"evt-{rng.randint(10_000, 99_999)}",
            severity=rng.choice(["info", "warn", "error"]),
            category=rng.choice(["system", "network", "application"]),
            source=f"component-{rng.randint(1, 6)}",
            message="synthetic example event",
            occurred_at_ms=rng.randint(1_700_000_000_000, 1_800_000_000_000),
            correlation_id=f"corr-{rng.randint(10_000, 99_999)}",
        ),
    ]
    rng.shuffle(messages)
    return messages


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def pick_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def main() -> int:
    port = pick_free_tcp_port()
    factory = AsyncioNetworkFactory()

    # Server side: build the dispatcher and framing handler that will turn
    # the incoming byte stream into typed messages.
    dispatcher = MessageDispatcher()
    dispatcher.register(MessageType.HEARTBEAT, handle_heartbeat)
    dispatcher.register(MessageType.TELEMETRY_UPDATE, handle_telemetry)
    dispatcher.register(MessageType.COMMAND_REQUEST, handle_command_request)
    dispatcher.register(MessageType.COMMAND_ACK, handle_command_ack)
    dispatcher.register(MessageType.EVENT_NOTIFICATION, handle_event_notification)

    messages_to_send = build_random_message_sequence()
    total_expected = len(messages_to_send)
    delivered = 0
    all_delivered = asyncio.Event()

    # Wrap every handler so we can count deliveries and stop once done.
    async def counting_wrapper(inner: MessageHandler) -> MessageHandler:
        async def wrapper(message: Any) -> None:
            nonlocal delivered
            await inner(message)
            delivered += 1
            if delivered >= total_expected:
                all_delivered.set()

        return wrapper

    for type_id, handler in list(dispatcher._handlers.items()):  # noqa: SLF001
        dispatcher.register(type_id, await counting_wrapper(handler))

    server = factory.create_tcp_server(
        settings=TcpServerSettings(host="127.0.0.1", port=port, max_connections=64),
        event_handler=FramingHandler(dispatcher),
    )
    client = factory.create_tcp_client(
        settings=TcpClientSettings(host="127.0.0.1", port=port),
        event_handler=BaseNetworkEventHandler(),  # client does not consume here
    )

    await server.start()
    await server.wait_until_running(timeout_seconds=5.0)
    await client.start()
    connection = await client.wait_until_connected(timeout_seconds=5.0)

    # Send all five messages back-to-back. The framing handler on the server
    # side correctly pulls them apart even when multiple frames arrive in the
    # same on_bytes_received call.
    for message in messages_to_send:
        await connection.send(encode_message(message))

    await asyncio.wait_for(all_delivered.wait(), timeout=5.0)
    print(f"dispatched {delivered} typed messages")

    await client.stop()
    await server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
