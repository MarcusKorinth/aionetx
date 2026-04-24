"""
Event type emitted when a handler failure requests component shutdown.

This event lets applications observe why a dispatcher chose the
``STOP_COMPONENT`` failure path and which triggering event caused it.
"""

from __future__ import annotations

from dataclasses import dataclass

from aionetx.api.event_delivery_settings import EventDispatchMode, EventHandlerFailurePolicy


@dataclass(slots=True, frozen=True)
class HandlerFailurePolicyStopEvent:
    """
    Event emitted when handler failure policy requests component shutdown.

    Attributes:
        resource_id: Identifier of the component whose dispatcher raised the
            stop request.
        triggering_event_name: Name of the event type that caused the handler
            failure.
        error: Original exception raised by the event handler.
        policy: Handler-failure policy that selected component shutdown.
        dispatch_mode: Dispatcher mode in effect when the failure happened.
    """

    resource_id: str
    triggering_event_name: str
    error: Exception
    policy: EventHandlerFailurePolicy
    dispatch_mode: EventDispatchMode
