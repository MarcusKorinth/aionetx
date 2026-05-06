from __future__ import annotations

import asyncio

import pytest

from tests.helpers import assert_awaitable_cancelled
from tests.helpers import drain_awaitable_ignoring_cancelled
from tests.internal_asyncio_impl_refs import await_future_completion_preserving_cancellation
from tests.internal_asyncio_impl_refs import await_task_completion_preserving_cancellation


@pytest.mark.asyncio
async def test_task_await_helper_preserves_caller_cancellation_until_child_settles() -> None:
    child_cancel_seen = asyncio.Event()
    release_child = asyncio.Event()

    async def child_task_body() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            child_cancel_seen.set()
            await release_child.wait()
            raise

    async def await_child_shutdown(task: asyncio.Task[object]) -> None:
        task.cancel()
        await await_task_completion_preserving_cancellation(task)

    child_task = asyncio.create_task(child_task_body())
    awaiter_task = asyncio.create_task(await_child_shutdown(child_task))

    try:
        await asyncio.wait_for(child_cancel_seen.wait(), timeout=1.0)

        awaiter_task.cancel()
        await asyncio.sleep(0)

        assert not awaiter_task.done()

        release_child.set()
        await assert_awaitable_cancelled(awaiter_task)
    finally:
        release_child.set()
        if not awaiter_task.done():
            awaiter_task.cancel()
            await drain_awaitable_ignoring_cancelled(awaiter_task)
        if not child_task.done():
            child_task.cancel()
            await drain_awaitable_ignoring_cancelled(child_task)


@pytest.mark.asyncio
async def test_future_await_helper_preserves_caller_cancellation_until_future_settles() -> None:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def await_shared_future() -> None:
        await await_future_completion_preserving_cancellation(future)

    cancelled_waiter = asyncio.create_task(await_shared_future())
    surviving_waiter = asyncio.create_task(await_shared_future())

    try:
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        await asyncio.sleep(0)

        assert not cancelled_waiter.done()
        assert not surviving_waiter.done()
        assert not future.done()

        future.set_result(None)

        await assert_awaitable_cancelled(cancelled_waiter)
        await asyncio.wait_for(surviving_waiter, timeout=1.0)
    finally:
        if not future.done():
            future.set_result(None)
        for waiter in (cancelled_waiter, surviving_waiter):
            if not waiter.done():
                waiter.cancel()
                await drain_awaitable_ignoring_cancelled(waiter)


@pytest.mark.asyncio
async def test_future_await_helper_preserves_future_exception_over_caller_cancellation() -> None:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def await_shared_future() -> None:
        await await_future_completion_preserving_cancellation(future)

    waiter = asyncio.create_task(await_shared_future())

    try:
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)

        assert not waiter.done()

        future.set_exception(RuntimeError("shared-future-failed"))

        with pytest.raises(RuntimeError, match="shared-future-failed"):
            await asyncio.wait_for(waiter, timeout=1.0)
    finally:
        if not future.done():
            future.set_result(None)
        if not waiter.done():
            waiter.cancel()
            await drain_awaitable_ignoring_cancelled(waiter)
