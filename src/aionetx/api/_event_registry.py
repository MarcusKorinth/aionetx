"""
Internal taxonomy registry for all emitted network event types.

The registry in this module is the single source of truth for the open
``NetworkEvent`` union, typed hook names, and recording bucket names used by
testing helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias, get_args

from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.connection_events import (
    ConnectionClosedEvent,
    ConnectionOpenedEvent,
    ConnectionRejectedEvent,
)
from aionetx.api.handler_failure_policy_stop_event import HandlerFailurePolicyStopEvent
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api.reconnect_events import (
    ReconnectAttemptFailedEvent,
    ReconnectAttemptStartedEvent,
    ReconnectScheduledEvent,
)

NetworkEvent: TypeAlias = (
    ConnectionOpenedEvent
    | ConnectionRejectedEvent
    | ConnectionClosedEvent
    | BytesReceivedEvent
    | NetworkErrorEvent
    | HandlerFailurePolicyStopEvent
    | ComponentLifecycleChangedEvent
    | ReconnectAttemptStartedEvent
    | ReconnectAttemptFailedEvent
    | ReconnectScheduledEvent
)
EventType: TypeAlias = type[NetworkEvent]
EventHandlerMethod: TypeAlias = Callable[[Any], Awaitable[None]]


@dataclass(frozen=True)
class EventRegistryEntry:
    """Canonical taxonomy entry for one network event type."""

    event_type: EventType
    handler_method_name: str
    recording_bucket_name: str


NETWORK_EVENT_REGISTRY: tuple[EventRegistryEntry, ...] = (
    EventRegistryEntry(
        event_type=ConnectionOpenedEvent,
        handler_method_name="on_connection_opened",
        recording_bucket_name="opened_events",
    ),
    EventRegistryEntry(
        event_type=ConnectionRejectedEvent,
        handler_method_name="on_connection_rejected",
        recording_bucket_name="rejected_events",
    ),
    EventRegistryEntry(
        event_type=ConnectionClosedEvent,
        handler_method_name="on_connection_closed",
        recording_bucket_name="closed_events",
    ),
    EventRegistryEntry(
        event_type=BytesReceivedEvent,
        handler_method_name="on_bytes_received",
        recording_bucket_name="received_events",
    ),
    EventRegistryEntry(
        event_type=NetworkErrorEvent,
        handler_method_name="on_error",
        recording_bucket_name="error_events",
    ),
    EventRegistryEntry(
        event_type=HandlerFailurePolicyStopEvent,
        handler_method_name="on_handler_failure_policy_stop",
        recording_bucket_name="handler_failure_policy_stop_events",
    ),
    EventRegistryEntry(
        event_type=ComponentLifecycleChangedEvent,
        handler_method_name="on_component_lifecycle_changed",
        recording_bucket_name="lifecycle_events",
    ),
    EventRegistryEntry(
        event_type=ReconnectAttemptStartedEvent,
        handler_method_name="on_reconnect_attempt_started",
        recording_bucket_name="reconnect_attempt_started_events",
    ),
    EventRegistryEntry(
        event_type=ReconnectAttemptFailedEvent,
        handler_method_name="on_reconnect_attempt_failed",
        recording_bucket_name="reconnect_attempt_failed_events",
    ),
    EventRegistryEntry(
        event_type=ReconnectScheduledEvent,
        handler_method_name="on_reconnect_scheduled",
        recording_bucket_name="reconnect_scheduled_events",
    ),
)

# Keep ``NETWORK_EVENT_REGISTRY`` as the taxonomy source of truth. Conformance
# checks below ensure that the open ``NetworkEvent`` union, typed event hooks,
# and testing collectors stay aligned with the same event set.

NETWORK_EVENT_TYPES: tuple[EventType, ...] = tuple(
    entry.event_type for entry in NETWORK_EVENT_REGISTRY
)
NETWORK_EVENT_HANDLER_METHODS: dict[EventType, str] = {
    entry.event_type: entry.handler_method_name for entry in NETWORK_EVENT_REGISTRY
}
NETWORK_EVENT_RECORDING_BUCKETS: dict[EventType, str] = {
    entry.event_type: entry.recording_bucket_name for entry in NETWORK_EVENT_REGISTRY
}


def validate_network_event_registry_conformance(
    network_event_union: object,
    registry: tuple[EventRegistryEntry, ...],
) -> None:
    """
    Validate that the event union and taxonomy registry describe the same types.

    Args:
        network_event_union: Union object representing the public event taxonomy.
        registry: Registry entries that define hook names and recording buckets.

    Raises:
        RuntimeError: If the union and registry have drifted apart.
    """
    union_event_types = tuple(get_args(network_event_union))
    registry_event_types = tuple(entry.event_type for entry in registry)

    missing_from_registry = [
        event_type.__name__
        for event_type in union_event_types
        if event_type not in registry_event_types
    ]
    extra_in_registry = [
        event_type.__name__
        for event_type in registry_event_types
        if event_type not in union_event_types
    ]

    if not missing_from_registry and not extra_in_registry:
        return

    message_parts: list[str] = [
        "Network event registry drift detected.",
        "Update `NetworkEvent` and `NETWORK_EVENT_REGISTRY` together.",
    ]
    if missing_from_registry:
        message_parts.append(
            f"Missing registry entries for: {', '.join(sorted(missing_from_registry))}."
        )
    if extra_in_registry:
        message_parts.append(
            f"Registry contains events missing from the union: {', '.join(sorted(extra_in_registry))}."
        )

    raise RuntimeError(" ".join(message_parts))


validate_network_event_registry_conformance(NetworkEvent, NETWORK_EVENT_REGISTRY)
