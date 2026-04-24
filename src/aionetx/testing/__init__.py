"""
Testing helpers for capturing and waiting on emitted network events.

This module exposes in-memory recording handlers plus a small async polling
helper that tests and examples can reuse without reimplementing bookkeeping.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable

from aionetx.api._event_registry import NETWORK_EVENT_RECORDING_BUCKETS
from aionetx.api.events import (
    BytesReceivedEvent,
    ComponentLifecycleChangedEvent,
    ConnectionClosedEvent,
    ConnectionOpenedEvent,
    ConnectionRejectedEvent,
    HandlerFailurePolicyStopEvent,
    NetworkErrorEvent,
    NetworkEvent,
)
from aionetx.api.reconnect_events import (
    ReconnectAttemptFailedEvent,
    ReconnectAttemptStartedEvent,
    ReconnectScheduledEvent,
)


class RecordingEventHandler:
    """In-memory event collector for tests and examples."""

    def __init__(self) -> None:
        self.events: list[NetworkEvent] = []
        self.opened_events: list[ConnectionOpenedEvent] = []
        self.rejected_events: list[ConnectionRejectedEvent] = []
        self.closed_events: list[ConnectionClosedEvent] = []
        self.received_events: list[BytesReceivedEvent] = []
        self.error_events: list[NetworkErrorEvent] = []
        self.lifecycle_events: list[ComponentLifecycleChangedEvent] = []
        self.handler_failure_policy_stop_events: list[HandlerFailurePolicyStopEvent] = []
        self.reconnect_attempt_started_events: list[ReconnectAttemptStartedEvent] = []
        self.reconnect_attempt_failed_events: list[ReconnectAttemptFailedEvent] = []
        self.reconnect_scheduled_events: list[ReconnectScheduledEvent] = []
        self.received_by_connection: dict[str, list[bytes]] = defaultdict(list)

    async def on_event(self, event: NetworkEvent) -> None:
        """
        Record an emitted event into the aggregate and typed event buckets.

        Args:
            event: Event instance emitted by a component under test.
        """
        self.events.append(event)
        for event_type, bucket_name in NETWORK_EVENT_RECORDING_BUCKETS.items():
            if isinstance(event, event_type):
                bucket = getattr(self, bucket_name)
                bucket.append(event)
                break

        if isinstance(event, BytesReceivedEvent):
            self.received_by_connection[event.resource_id].append(event.data)


class AwaitableRecordingEventHandler(RecordingEventHandler):
    """Recording handler with event milestones for deterministic async tests."""

    def __init__(self) -> None:
        super().__init__()
        self.connection_opened = asyncio.Event()
        self.bytes_received = asyncio.Event()
        self.error_observed = asyncio.Event()
        self.connection_closed = asyncio.Event()
        self.lifecycle_changed = asyncio.Event()

    async def on_event(self, event: NetworkEvent) -> None:
        """
        Record an event and update milestone flags for common event categories.

        Args:
            event: Event instance emitted by a component under test.
        """
        await super().on_event(event)
        if isinstance(event, ConnectionOpenedEvent):
            self.connection_opened.set()
        elif isinstance(event, BytesReceivedEvent):
            self.bytes_received.set()
        elif isinstance(event, NetworkErrorEvent):
            self.error_observed.set()
        elif isinstance(event, ConnectionClosedEvent):
            self.connection_closed.set()
        elif isinstance(event, ComponentLifecycleChangedEvent):
            self.lifecycle_changed.set()


async def wait_for_condition(
    condition: Callable[[], bool],
    timeout_seconds: float = 2.0,
    interval_seconds: float = 0.01,
) -> None:
    """Poll ``condition`` until it is true or timeout is exceeded."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not condition():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Timed out waiting for test condition.")
        await asyncio.sleep(interval_seconds)


__all__ = (
    "AwaitableRecordingEventHandler",
    "RecordingEventHandler",
    "wait_for_condition",
)
