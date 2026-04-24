from __future__ import annotations

import socket

import pytest

from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.udp import UdpReceiverSettings
from aionetx.api.udp import UdpSenderSettings
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver
from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender
from tests.helpers import wait_for_condition


def get_unused_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
@pytest.mark.integration
async def test_udp_sender_receiver_roundtrip(recording_event_handler) -> None:
    port = get_unused_udp_port()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=port),
        event_handler=recording_event_handler,
    )
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(default_host="127.0.0.1", default_port=port)
    )

    await receiver.start()
    try:
        await sender.send(b"udp-test")
        await wait_for_condition(
            lambda: any(
                event.data == b"udp-test" for event in recording_event_handler.received_events
            ),
            timeout_seconds=2.0,
        )
        event = next(
            event for event in recording_event_handler.received_events if event.data == b"udp-test"
        )
        assert event.remote_host in ("127.0.0.1", "::ffff:127.0.0.1")
        assert isinstance(event.remote_port, int)
    finally:
        await sender.stop()
        await receiver.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_udp_receiver_repeated_sends_and_stop_idempotence(recording_event_handler) -> None:
    port = get_unused_udp_port()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=port),
        event_handler=recording_event_handler,
    )
    sender = AsyncioUdpSender(settings=UdpSenderSettings())

    await receiver.start()
    try:
        for payload in (b"one", b"two", b"three"):
            await sender.send(payload, host="127.0.0.1", port=port)

        await wait_for_condition(
            lambda: (
                sum(
                    1
                    for e in recording_event_handler.received_events
                    if e.data in {b"one", b"two", b"three"}
                )
                >= 3
            ),
            timeout_seconds=2.0,
        )
    finally:
        await sender.stop()
        await receiver.stop()

    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    closed_count = len(recording_event_handler.closed_events)
    assert recording_event_handler.closed_events[-1].previous_state == ConnectionState.CONNECTED
    assert recording_event_handler.closed_events[-1].metadata is not None
    await receiver.stop()
    assert len(recording_event_handler.closed_events) == closed_count


@pytest.mark.asyncio
@pytest.mark.integration
async def test_udp_broadcast_send_and_receive(recording_event_handler) -> None:
    port = get_unused_udp_port()
    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="0.0.0.0", port=port, enable_broadcast=True),
        event_handler=recording_event_handler,
    )
    sender = AsyncioUdpSender(
        settings=UdpSenderSettings(
            default_host="255.255.255.255", default_port=port, enable_broadcast=True
        )
    )

    await receiver.start()
    try:
        try:
            await sender.send(b"broadcast-test")
        except OSError as error:
            if error.errno in (101, 13):
                pytest.skip(f"Broadcast send is unavailable in this environment: {error}.")
            raise
        try:
            await wait_for_condition(
                lambda: any(
                    event.data == b"broadcast-test"
                    for event in recording_event_handler.received_events
                ),
                timeout_seconds=1.5,
            )
        except TimeoutError:
            pytest.skip("Broadcast loopback delivery is unavailable in this environment.")
    finally:
        await sender.stop()
        await receiver.stop()
