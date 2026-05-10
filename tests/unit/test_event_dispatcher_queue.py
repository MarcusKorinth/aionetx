from __future__ import annotations

import asyncio

import pytest

from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.implementations.asyncio_impl._event_dispatcher_queue import (
    QueuedEvent,
    complete_queued_event,
    drop_queued_event,
    fail_queued_event,
)


def _event() -> NetworkErrorEvent:
    return NetworkErrorEvent(resource_id="dispatcher", error=RuntimeError("boom"))


@pytest.mark.asyncio
async def test_complete_queued_event_releases_waiter_successfully() -> None:
    handled = asyncio.get_running_loop().create_future()
    queued_event = QueuedEvent(event=_event(), handled=handled)

    complete_queued_event(queued_event)

    assert handled.done()
    assert await handled is None


@pytest.mark.asyncio
async def test_drop_queued_event_releases_waiter_with_reason() -> None:
    handled = asyncio.get_running_loop().create_future()
    queued_event = QueuedEvent(event=_event(), handled=handled)

    drop_queued_event(queued_event, reason="dropped by backpressure")

    with pytest.raises(RuntimeError, match="Queued event was dropped by backpressure\\."):
        handled.result()


@pytest.mark.asyncio
async def test_fail_queued_event_releases_waiter_with_original_error() -> None:
    handled = asyncio.get_running_loop().create_future()
    queued_event = QueuedEvent(event=_event(), handled=handled)
    error = RuntimeError("worker stopped")

    fail_queued_event(queued_event, error)

    with pytest.raises(RuntimeError, match="worker stopped") as exc_info:
        handled.result()
    assert exc_info.value is error
