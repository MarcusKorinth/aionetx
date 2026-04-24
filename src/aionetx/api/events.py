"""
Curated event re-export module for the public API.

This module groups the event dataclasses and handler helper that advanced users
may want to import from one place without reaching into the fine-grained event
modules directly.
"""

from __future__ import annotations

from aionetx.api.base_network_event_handler import BaseNetworkEventHandler
from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.connection_events import (
    ConnectionClosedEvent,
    ConnectionOpenedEvent,
    ConnectionRejectedEvent,
)
from aionetx.api.handler_failure_policy_stop_event import HandlerFailurePolicyStopEvent
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api.network_event import NetworkEvent
from aionetx.api.reconnect_events import (
    ReconnectAttemptFailedEvent,
    ReconnectAttemptStartedEvent,
    ReconnectScheduledEvent,
)

__all__ = (
    "BaseNetworkEventHandler",
    "BytesReceivedEvent",
    "ComponentLifecycleChangedEvent",
    "ConnectionClosedEvent",
    "ConnectionOpenedEvent",
    "ConnectionRejectedEvent",
    "HandlerFailurePolicyStopEvent",
    "NetworkErrorEvent",
    "NetworkEvent",
    "ReconnectAttemptFailedEvent",
    "ReconnectAttemptStartedEvent",
    "ReconnectScheduledEvent",
)
