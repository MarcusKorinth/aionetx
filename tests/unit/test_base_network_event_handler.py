from __future__ import annotations

import pytest

from aionetx.api.base_network_event_handler import BaseNetworkEventHandler
from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_events import ConnectionClosedEvent
from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.connection_events import ConnectionOpenedEvent
from aionetx.api.connection_events import ConnectionRejectedEvent
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.event_delivery_settings import EventDispatchMode
from aionetx.api.event_delivery_settings import EventHandlerFailurePolicy
from aionetx.api.handler_failure_policy_stop_event import HandlerFailurePolicyStopEvent
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api.reconnect_events import ReconnectAttemptFailedEvent
from aionetx.api.reconnect_events import ReconnectAttemptStartedEvent
from aionetx.api.reconnect_events import ReconnectScheduledEvent


@pytest.mark.asyncio
async def test_base_network_event_handler_noop_on_event() -> None:
    handler = BaseNetworkEventHandler()
    await handler.on_event(
        ConnectionOpenedEvent(
            resource_id="x",
            metadata=ConnectionMetadata(connection_id="x", role=ConnectionRole.CLIENT),
        )
    )


@pytest.mark.asyncio
async def test_base_network_event_handler_default_typed_hooks_are_noops() -> None:
    handler = BaseNetworkEventHandler()
    metadata = ConnectionMetadata(connection_id="x", role=ConnectionRole.CLIENT)

    await handler.on_connection_opened(ConnectionOpenedEvent(resource_id="x", metadata=metadata))
    await handler.on_connection_closed(
        ConnectionClosedEvent(resource_id="x", previous_state=ConnectionState.CONNECTED)
    )
    await handler.on_connection_rejected(
        ConnectionRejectedEvent(
            resource_id="server",
            connection_id="server:rejected:1",
            reason="max_connections_reached",
        )
    )
    await handler.on_bytes_received(BytesReceivedEvent(resource_id="x", data=b"payload"))
    await handler.on_error(NetworkErrorEvent(resource_id="x", error=RuntimeError("boom")))
    await handler.on_handler_failure_policy_stop(
        HandlerFailurePolicyStopEvent(
            resource_id="component",
            triggering_event_name="BytesReceivedEvent",
            error=RuntimeError("handler-failed"),
            policy=EventHandlerFailurePolicy.STOP_COMPONENT,
            dispatch_mode=EventDispatchMode.BACKGROUND,
        )
    )
    await handler.on_component_lifecycle_changed(
        ComponentLifecycleChangedEvent(
            resource_id="component",
            previous=ComponentLifecycleState.STARTING,
            current=ComponentLifecycleState.RUNNING,
        )
    )
    await handler.on_reconnect_attempt_started(
        ReconnectAttemptStartedEvent(resource_id="client", attempt=1)
    )
    await handler.on_reconnect_attempt_failed(
        ReconnectAttemptFailedEvent(
            resource_id="client",
            attempt=1,
            error=ConnectionRefusedError("refused"),
            next_delay_seconds=0.5,
        )
    )
    await handler.on_reconnect_scheduled(
        ReconnectScheduledEvent(resource_id="client", attempt=2, delay_seconds=0.5)
    )


@pytest.mark.asyncio
async def test_base_network_event_handler_dispatches_to_typed_hooks() -> None:
    class Handler(BaseNetworkEventHandler):
        def __init__(self) -> None:
            self.opened = 0
            self.received = 0

        async def on_connection_opened(self, event: ConnectionOpenedEvent) -> None:
            self.opened += 1

        async def on_bytes_received(self, event: BytesReceivedEvent) -> None:
            self.received += 1

    handler = Handler()
    await handler.on_event(
        ConnectionOpenedEvent(
            resource_id="x",
            metadata=ConnectionMetadata(connection_id="x", role=ConnectionRole.CLIENT),
        )
    )
    await handler.on_event(BytesReceivedEvent(resource_id="x", data=b"abc"))

    assert handler.opened == 1
    assert handler.received == 1


@pytest.mark.asyncio
async def test_base_network_event_handler_dispatches_subclasses_to_matching_hook() -> None:
    class DerivedBytesReceivedEvent(BytesReceivedEvent):
        pass

    class Handler(BaseNetworkEventHandler):
        def __init__(self) -> None:
            self.received_types: list[type[BytesReceivedEvent]] = []

        async def on_bytes_received(self, event: BytesReceivedEvent) -> None:
            self.received_types.append(type(event))

    handler = Handler()
    await handler.on_event(DerivedBytesReceivedEvent(resource_id="x", data=b"abc"))

    assert handler.received_types == [DerivedBytesReceivedEvent]
