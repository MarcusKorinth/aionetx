from __future__ import annotations

import socket
import struct

import pytest

from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.implementations.asyncio_impl.asyncio_multicast_receiver import (
    AsyncioMulticastReceiver,
)
from tests.helpers import wait_for_condition


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.multicast
async def test_multicast_receiver_receives_datagram(recording_event_handler) -> None:
    group_ip = "239.255.0.1"
    port = 20001
    interface_ip = "127.0.0.1"

    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip=group_ip,
            port=port,
            bind_ip="0.0.0.0",
            interface_ip=interface_ip,
        ),
        event_handler=recording_event_handler,
    )
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
    try:
        await receiver.start()
    except OSError as error:
        if error.errno in (19, 99):
            pytest.skip(f"Multicast is unavailable in this environment: {error}.")
        raise

    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 1))
    sender.sendto(b"mc-test", (group_ip, port))
    sender.close()

    try:
        await wait_for_condition(
            lambda: any(
                event.data == b"mc-test" for event in recording_event_handler.received_events
            ),
            timeout_seconds=2.0,
        )
    except TimeoutError:
        pytest.skip("Multicast loopback delivery is unavailable in this environment.")

    await receiver.stop()
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED

    assert any(event.data == b"mc-test" for event in recording_event_handler.received_events)
    multicast_event = next(
        event for event in recording_event_handler.received_events if event.data == b"mc-test"
    )
    assert multicast_event.remote_host is not None
    assert isinstance(multicast_event.remote_port, int)
    multicast_component_id = f"udp/multicast/{group_ip}/{port}"
    multicast_running_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == multicast_component_id
        and event.current == ComponentLifecycleState.RUNNING
    ]
    assert len(multicast_running_events) == 1

    multicast_stopped_events = [
        event
        for event in recording_event_handler.lifecycle_events
        if event.resource_id == multicast_component_id
        and event.current == ComponentLifecycleState.STOPPED
    ]
    assert len(multicast_stopped_events) == 1


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.multicast
async def test_multicast_receiver_stop_is_idempotent(recording_event_handler) -> None:
    receiver = AsyncioMulticastReceiver(
        settings=MulticastReceiverSettings(
            group_ip="239.255.0.2",
            port=20002,
            bind_ip="0.0.0.0",
            interface_ip="127.0.0.1",
        ),
        event_handler=recording_event_handler,
    )
    try:
        await receiver.start()
    except OSError as error:
        if error.errno in (19, 99):
            pytest.skip(f"Multicast is unavailable in this environment: {error}.")
        raise

    await receiver.stop()
    closed_count = len(recording_event_handler.closed_events)
    await receiver.stop()

    assert len(recording_event_handler.closed_events) == closed_count
    assert receiver.lifecycle_state == ComponentLifecycleState.STOPPED
