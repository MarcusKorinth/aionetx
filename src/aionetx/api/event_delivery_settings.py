"""
Configuration types controlling event dispatch behavior.

The enums and dataclass in this module define dispatch mode, queue behavior,
and handler-failure policy for emitted transport events.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from aionetx.api._validation import require_enum_member, require_positive_int


class EventDispatchMode(str, Enum):
    """Execution strategy used to deliver emitted events to the handler."""

    INLINE = "inline"
    BACKGROUND = "background"


class EventBackpressurePolicy(str, Enum):
    """Policy used when the background dispatcher queue reaches capacity."""

    BLOCK = "block"
    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"


class EventHandlerFailurePolicy(str, Enum):
    """Policy describing what the dispatcher does when the handler raises."""

    LOG_ONLY = "log_only"
    EMIT_ERROR_EVENT = "emit_error_event"
    STOP_COMPONENT = "stop_component"
    RAISE_IN_INLINE_MODE = "raise_in_inline_mode"


@dataclass(frozen=True, slots=True)
class EventDeliverySettings:
    """
    Event delivery configuration for managed components.

    Attributes:
        dispatch_mode: Whether events are delivered inline in the caller task
            or via a background worker task.
        backpressure_policy: Queue behavior once pending-event capacity is
            reached in background mode.
        max_pending_events: Maximum number of queued background events before
            backpressure handling applies.
        handler_failure_policy: Response when the application event handler
            raises while processing an event.
    """

    dispatch_mode: EventDispatchMode = EventDispatchMode.BACKGROUND
    backpressure_policy: EventBackpressurePolicy = EventBackpressurePolicy.BLOCK
    max_pending_events: int = 1024
    handler_failure_policy: EventHandlerFailurePolicy = EventHandlerFailurePolicy.LOG_ONLY

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """
        Validate event delivery settings.

        Raises:
            InvalidNetworkConfigurationError: If queue bounds or enum values
                are invalid.
        """
        require_positive_int(
            field_name="EventDeliverySettings.max_pending_events",
            value=self.max_pending_events,
        )
        require_enum_member(
            field_name="EventDeliverySettings.dispatch_mode",
            value=self.dispatch_mode,
            enum_type=EventDispatchMode,
        )
        require_enum_member(
            field_name="EventDeliverySettings.backpressure_policy",
            value=self.backpressure_policy,
            enum_type=EventBackpressurePolicy,
        )
        require_enum_member(
            field_name="EventDeliverySettings.handler_failure_policy",
            value=self.handler_failure_policy,
            enum_type=EventHandlerFailurePolicy,
        )
