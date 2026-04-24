"""
Typed hook dispatch helpers for unified network event handling.

This module exposes :class:`BaseNetworkEventHandler`, a convenience base class
that keeps the single ``on_event(event)`` callback contract while letting
applications override per-event async hooks instead of writing their own
``isinstance`` dispatch ladder.
"""

from __future__ import annotations

from typing import cast

from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.component_lifecycle_changed_event import ComponentLifecycleChangedEvent
from aionetx.api.connection_events import (
    ConnectionClosedEvent,
    ConnectionOpenedEvent,
    ConnectionRejectedEvent,
)
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api.handler_failure_policy_stop_event import HandlerFailurePolicyStopEvent
from aionetx.api._event_registry import EventHandlerMethod, NETWORK_EVENT_HANDLER_METHODS
from aionetx.api.network_event import NetworkEvent
from aionetx.api.network_event_handler_protocol import NetworkEventHandlerProtocol
from aionetx.api.reconnect_events import (
    ReconnectAttemptFailedEvent,
    ReconnectAttemptStartedEvent,
    ReconnectScheduledEvent,
)


class BaseNetworkEventHandler(NetworkEventHandlerProtocol):
    """
    Convenience base class with typed no-op hooks.

    `NetworkEventHandlerProtocol` stays unified as `on_event(event)`.
    This class keeps that contract but provides optional typed hooks so users can
    implement readable per-event handlers without writing isinstance chains.
    """

    async def on_event(self, event: NetworkEvent) -> None:
        """
        Route one network event to the matching typed hook.

        Args:
            event: Event instance emitted by a transport or component.
        """
        for event_type, handler_name in NETWORK_EVENT_HANDLER_METHODS.items():
            if isinstance(event, event_type):
                handler = cast(EventHandlerMethod, getattr(self, handler_name))
                await handler(event)
                break
        return None

    async def on_connection_opened(self, event: ConnectionOpenedEvent) -> None:
        """Handle ``ConnectionOpenedEvent`` with a default no-op implementation."""
        del event
        return None

    async def on_connection_closed(self, event: ConnectionClosedEvent) -> None:
        """Handle ``ConnectionClosedEvent`` with a default no-op implementation."""
        del event
        return None

    async def on_connection_rejected(self, event: ConnectionRejectedEvent) -> None:
        """Handle ``ConnectionRejectedEvent`` with a default no-op implementation."""
        del event
        return None

    async def on_bytes_received(self, event: BytesReceivedEvent) -> None:
        """Handle ``BytesReceivedEvent`` with a default no-op implementation."""
        del event
        return None

    async def on_error(self, event: NetworkErrorEvent) -> None:
        """Handle ``NetworkErrorEvent`` with a default no-op implementation."""
        del event
        return None

    async def on_handler_failure_policy_stop(self, event: HandlerFailurePolicyStopEvent) -> None:
        """Handle ``HandlerFailurePolicyStopEvent`` with a default no-op implementation."""
        del event
        return None

    async def on_component_lifecycle_changed(
        self,
        event: ComponentLifecycleChangedEvent,
    ) -> None:
        """Handle ``ComponentLifecycleChangedEvent`` with a default no-op implementation."""
        del event
        return None

    async def on_reconnect_attempt_started(
        self,
        event: ReconnectAttemptStartedEvent,
    ) -> None:
        """Handle ``ReconnectAttemptStartedEvent`` with a default no-op implementation."""
        del event
        return None

    async def on_reconnect_attempt_failed(
        self,
        event: ReconnectAttemptFailedEvent,
    ) -> None:
        """Handle ``ReconnectAttemptFailedEvent`` with a default no-op implementation."""
        del event
        return None

    async def on_reconnect_scheduled(self, event: ReconnectScheduledEvent) -> None:
        """Handle ``ReconnectScheduledEvent`` with a default no-op implementation."""
        del event
        return None
