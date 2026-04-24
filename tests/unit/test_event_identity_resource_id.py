from __future__ import annotations

import pytest

from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_events import ConnectionClosedEvent
from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.connection_events import ConnectionOpenedEvent, ConnectionRejectedEvent
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.event_delivery_settings import EventDispatchMode, EventHandlerFailurePolicy
from aionetx.api.handler_failure_policy_stop_event import HandlerFailurePolicyStopEvent
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api.reconnect_events import (
    ReconnectAttemptFailedEvent,
    ReconnectAttemptStartedEvent,
    ReconnectScheduledEvent,
)


def test_resource_id_is_canonical_identity_across_events() -> None:
    metadata = ConnectionMetadata(connection_id="conn-1", role=ConnectionRole.CLIENT)

    events = [
        BytesReceivedEvent(resource_id="conn-1", data=b"x"),
        ConnectionClosedEvent(resource_id="conn-1", previous_state=ConnectionState.CONNECTED),
        ConnectionOpenedEvent(resource_id="conn-1", metadata=metadata),
        ConnectionRejectedEvent(
            resource_id="component-1",
            connection_id="component-1/rejected-1",
            reason="max_connections_reached",
            remote_host="127.0.0.1",
            remote_port=9876,
        ),
        NetworkErrorEvent(resource_id="component-1", error=RuntimeError("boom")),
        HandlerFailurePolicyStopEvent(
            resource_id="component-1",
            triggering_event_name="BytesReceivedEvent",
            error=RuntimeError("stop"),
            policy=EventHandlerFailurePolicy.STOP_COMPONENT,
            dispatch_mode=EventDispatchMode.INLINE,
        ),
        ComponentLifecycleChangedEvent(
            resource_id="component-1",
            previous=ComponentLifecycleState.STARTING,
            current=ComponentLifecycleState.RUNNING,
        ),
        ReconnectAttemptStartedEvent(resource_id="component-1", attempt=1),
        ReconnectAttemptFailedEvent(
            resource_id="component-1", attempt=1, error=RuntimeError("fail")
        ),
        ReconnectScheduledEvent(resource_id="component-1", attempt=2, delay_seconds=0.25),
    ]

    assert [event.resource_id for event in events] == [
        "conn-1",
        "conn-1",
        "conn-1",
        "component-1",
        "component-1",
        "component-1",
        "component-1",
        "component-1",
        "component-1",
        "component-1",
    ]


def test_connection_opened_event_rejects_mismatched_resource_id() -> None:
    metadata = ConnectionMetadata(connection_id="conn-actual", role=ConnectionRole.CLIENT)

    with pytest.raises(ValueError, match="resource_id must match metadata.connection_id"):
        ConnectionOpenedEvent(resource_id="conn-other", metadata=metadata)


def test_generic_consumer_groups_by_resource_id_without_branching() -> None:
    events = [
        BytesReceivedEvent(resource_id="conn-a", data=b"a"),
        ConnectionClosedEvent(resource_id="conn-a", previous_state=ConnectionState.CONNECTED),
        ConnectionRejectedEvent(
            resource_id="component-b",
            connection_id="component-b/rejected-2",
            reason="max_connections_reached",
        ),
        NetworkErrorEvent(resource_id="component-b", error=RuntimeError("err")),
        ReconnectScheduledEvent(resource_id="component-b", attempt=2, delay_seconds=1.5),
    ]

    grouped: dict[str, int] = {}
    for event in events:
        grouped[event.resource_id] = grouped.get(event.resource_id, 0) + 1

    assert grouped == {"conn-a": 2, "component-b": 3}
