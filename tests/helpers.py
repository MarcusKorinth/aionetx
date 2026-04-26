from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from aionetx.testing import wait_for_condition

__all__ = [
    "assert_awaitable_cancelled",
    "drain_awaitable_ignoring_cancelled",
    "wait_for_condition",
]


async def assert_awaitable_cancelled(awaitable: Awaitable[object]) -> None:
    """Assert that an awaitable completes by propagating caller cancellation."""
    try:
        result = await awaitable
    except asyncio.CancelledError:
        return
    raise AssertionError(
        f"Expected awaitable to raise asyncio.CancelledError, but it returned {result!r}."
    )


async def drain_awaitable_ignoring_cancelled(awaitable: Awaitable[object]) -> None:
    """Drain cleanup awaitables where cancellation is the expected terminal state."""
    try:
        result = await awaitable
    except asyncio.CancelledError:
        return
    if result is not None:
        return
