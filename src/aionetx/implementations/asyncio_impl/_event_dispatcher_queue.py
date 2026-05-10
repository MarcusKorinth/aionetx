"""
Internal event-dispatcher queue item helpers.

This module owns queue item shape and waiter completion semantics so the
dispatcher can keep queue policy decisions separate from waiter bookkeeping.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aionetx.api.network_event import NetworkEvent


@dataclass(slots=True)
class QueuedEvent:
    """One pending event plus optional emit-and-wait completion barrier."""

    event: NetworkEvent
    handled: asyncio.Future[None] | None = None
    drop_on_backpressure: bool = True


def complete_queued_event(queued_event: QueuedEvent) -> None:
    """Wake an emit-and-wait caller after successful handling or intentional drop."""
    handled = queued_event.handled
    if handled is not None and not handled.done():
        handled.set_result(None)


def drop_queued_event(queued_event: QueuedEvent, *, reason: str) -> None:
    """Wake an emit-and-wait caller when its barrier event was dropped."""
    handled = queued_event.handled
    if handled is not None and not handled.done():
        handled.set_exception(RuntimeError(f"Queued event was {reason}."))


def fail_queued_event(queued_event: QueuedEvent, error: BaseException) -> None:
    """Wake an emit-and-wait caller when worker delivery fails unexpectedly."""
    handled = queued_event.handled
    if handled is not None and not handled.done():
        handled.set_exception(error)
