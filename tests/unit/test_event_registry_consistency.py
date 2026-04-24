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
from aionetx.api._event_registry import (
    NETWORK_EVENT_HANDLER_METHODS,
    NETWORK_EVENT_RECORDING_BUCKETS,
    NETWORK_EVENT_REGISTRY,
    NETWORK_EVENT_TYPES,
    EventRegistryEntry,
    validate_network_event_registry_conformance,
)
from aionetx.api.reconnect_events import (
    ReconnectAttemptFailedEvent,
    ReconnectAttemptStartedEvent,
    ReconnectScheduledEvent,
)
from aionetx.api.base_network_event_handler import BaseNetworkEventHandler
from aionetx.api.network_event import NetworkEvent
from aionetx.testing import RecordingEventHandler


def _example_events() -> list[object]:
    metadata = ConnectionMetadata(connection_id="c-1", role=ConnectionRole.CLIENT)
    return [
        ConnectionOpenedEvent(resource_id=metadata.connection_id, metadata=metadata),
        ConnectionRejectedEvent(
            resource_id="tcp/server/127.0.0.1/1234",
            connection_id="tcp/server/127.0.0.1/1234/connection/rejected-1",
            reason="max_connections_reached",
            remote_host="127.0.0.1",
            remote_port=4567,
        ),
        ConnectionClosedEvent(resource_id="c-1", previous_state=ConnectionState.CONNECTED),
        BytesReceivedEvent(resource_id="c-1", data=b"payload"),
        NetworkErrorEvent(resource_id="tcp/client/127.0.0.1/1234", error=RuntimeError("boom")),
        ComponentLifecycleChangedEvent(
            resource_id="tcp/client/127.0.0.1/1234",
            previous=ComponentLifecycleState.STARTING,
            current=ComponentLifecycleState.RUNNING,
        ),
        HandlerFailurePolicyStopEvent(
            resource_id="tcp/client/127.0.0.1/1234",
            triggering_event_name="NetworkErrorEvent",
            error=RuntimeError("handler"),
            policy=EventHandlerFailurePolicy.STOP_COMPONENT,
            dispatch_mode=EventDispatchMode.BACKGROUND,
        ),
        ReconnectAttemptStartedEvent(resource_id="tcp/client/127.0.0.1/1234", attempt=1),
        ReconnectAttemptFailedEvent(
            resource_id="tcp/client/127.0.0.1/1234",
            attempt=1,
            error=RuntimeError("dial"),
            next_delay_seconds=0.5,
        ),
        ReconnectScheduledEvent(
            resource_id="tcp/client/127.0.0.1/1234", attempt=2, delay_seconds=1.0
        ),
    ]


def test_registry_contains_unique_event_types() -> None:
    event_types = [entry.event_type for entry in NETWORK_EVENT_REGISTRY]
    assert len(event_types) == len(set(event_types))
    assert tuple(event_types) == NETWORK_EVENT_TYPES


def test_registry_projection_maps_stay_aligned_with_registry_entries() -> None:
    expected_handler_methods = {
        entry.event_type: entry.handler_method_name for entry in NETWORK_EVENT_REGISTRY
    }
    expected_recording_buckets = {
        entry.event_type: entry.recording_bucket_name for entry in NETWORK_EVENT_REGISTRY
    }

    assert NETWORK_EVENT_HANDLER_METHODS == expected_handler_methods
    assert NETWORK_EVENT_RECORDING_BUCKETS == expected_recording_buckets


def test_registry_conformance_accepts_current_union_and_registry() -> None:
    validate_network_event_registry_conformance(NetworkEvent, NETWORK_EVENT_REGISTRY)


def test_registry_conformance_raises_for_missing_registry_entries() -> None:
    with pytest.raises(RuntimeError, match="Missing registry entries for: BytesReceivedEvent"):
        validate_network_event_registry_conformance(
            ConnectionOpenedEvent | BytesReceivedEvent,
            (
                EventRegistryEntry(
                    event_type=ConnectionOpenedEvent,
                    handler_method_name="on_connection_opened",
                    recording_bucket_name="opened_events",
                ),
            ),
        )


def test_registry_conformance_raises_for_extra_registry_entries() -> None:
    with pytest.raises(
        RuntimeError,
        match="Registry contains events missing from the union: BytesReceivedEvent",
    ):
        validate_network_event_registry_conformance(
            ConnectionOpenedEvent,
            (
                EventRegistryEntry(
                    event_type=ConnectionOpenedEvent,
                    handler_method_name="on_connection_opened",
                    recording_bucket_name="opened_events",
                ),
                EventRegistryEntry(
                    event_type=BytesReceivedEvent,
                    handler_method_name="on_bytes_received",
                    recording_bucket_name="received_events",
                ),
            ),
        )


def test_reconnect_registry_event_types_match_reconnect_event_model() -> None:
    reconnect_types = {
        entry.event_type
        for entry in NETWORK_EVENT_REGISTRY
        if entry.handler_method_name.startswith("on_reconnect_")
    }

    assert reconnect_types == {
        ReconnectAttemptStartedEvent,
        ReconnectAttemptFailedEvent,
        ReconnectScheduledEvent,
    }


def test_base_network_event_handler_declares_all_registry_typed_hooks() -> None:
    for hook_name in NETWORK_EVENT_HANDLER_METHODS.values():
        assert hasattr(BaseNetworkEventHandler, hook_name), (
            "BaseNetworkEventHandler must expose a typed hook for every registry event."
        )


def test_recording_event_handler_declares_all_registry_recording_buckets() -> None:
    handler = RecordingEventHandler()
    for bucket_name in NETWORK_EVENT_RECORDING_BUCKETS.values():
        assert hasattr(handler, bucket_name), (
            "RecordingEventHandler must expose a bucket for every registry event."
        )


@pytest.mark.asyncio
async def test_recording_event_handler_uses_registry_for_all_event_types() -> None:
    handler = RecordingEventHandler()
    for event in _example_events():
        await handler.on_event(event)

    assert len(handler.events) == len(NETWORK_EVENT_TYPES)
    for bucket_name in NETWORK_EVENT_RECORDING_BUCKETS.values():
        assert len(getattr(handler, bucket_name)) == 1
    assert handler.received_by_connection["c-1"] == [b"payload"]
