from __future__ import annotations

import asyncio

import pytest

from aionetx.implementations.asyncio_impl import runtime_utils as runtime_utils_module
from tests.internal_asyncio_impl_refs import await_task_completion_preserving_cancellation


@pytest.mark.asyncio
async def test_task_await_helper_preserves_caller_cancellation_without_task_cancelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

        class _CurrentTaskWithoutCancelling:
            pass

        monkeypatch.setattr(
            runtime_utils_module.asyncio,
            "current_task",
            lambda: _CurrentTaskWithoutCancelling(),
        )
        awaiter_task.cancel()
        await asyncio.sleep(0)

        assert not awaiter_task.done()

        release_child.set()
        with pytest.raises(asyncio.CancelledError):
            await awaiter_task
    finally:
        release_child.set()
        if not awaiter_task.done():
            awaiter_task.cancel()
            try:
                await awaiter_task
            except asyncio.CancelledError:
                pass
        if not child_task.done():
            child_task.cancel()
            try:
                await child_task
            except asyncio.CancelledError:
                pass
