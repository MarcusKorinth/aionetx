from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import gc
from inspect import iscoroutinefunction
import socket
from typing import Any

import pytest
import pytest_asyncio

from aionetx.testing import AwaitableRecordingEventHandler, RecordingEventHandler

pytest_plugins = ("pytester",)

_UNRETRIEVED_FUTURE_EXCEPTION_MESSAGE = "Future exception was never retrieved"


@pytest.fixture
def recording_event_handler() -> RecordingEventHandler:
    return RecordingEventHandler()


@pytest.fixture
def awaitable_recording_event_handler() -> AwaitableRecordingEventHandler:
    return AwaitableRecordingEventHandler()


@pytest.fixture
def unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_async_test(item: pytest.Item) -> bool:
    test_function = getattr(item, "obj", None)
    return item.get_closest_marker("asyncio") is not None or (
        test_function is not None and iscoroutinefunction(test_function)
    )


def _is_unretrieved_future_exception_context(context: dict[str, Any]) -> bool:
    return context.get("message") == _UNRETRIEVED_FUTURE_EXCEPTION_MESSAGE and isinstance(
        context.get("future"), asyncio.Future
    )


def _format_unretrieved_future_exception_context(context: dict[str, Any]) -> str:
    future = context.get("future")
    exception = context.get("exception")
    return f"{_UNRETRIEVED_FUTURE_EXCEPTION_MESSAGE}: {exception!r} from {future!r}"


@pytest_asyncio.fixture(autouse=True)
async def _fail_on_unretrieved_asyncio_future_diagnostics(
    request: pytest.FixtureRequest,
) -> AsyncIterator[None]:
    if not _is_async_test(request.node):
        yield
        return

    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    unretrieved_future_contexts: list[dict[str, Any]] = []

    def record_unretrieved_future_diagnostics(
        current_loop: asyncio.AbstractEventLoop,
        context: dict[str, Any],
    ) -> None:
        if _is_unretrieved_future_exception_context(context):
            unretrieved_future_contexts.append(dict(context))
            return
        if previous_exception_handler is not None:
            previous_exception_handler(current_loop, context)
            return
        current_loop.default_exception_handler(context)

    loop.set_exception_handler(record_unretrieved_future_diagnostics)
    try:
        yield
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_exception_handler)

    if unretrieved_future_contexts:
        details = "\n".join(
            f"- {_format_unretrieved_future_exception_context(context)}"
            for context in unretrieved_future_contexts
        )
        pytest.fail(
            f"asyncio reported unretrieved Future exception diagnostics:\n{details}",
            pytrace=False,
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.pluginmanager.has_plugin("asyncio"):
        return

    async_tests = [item for item in items if _is_async_test(item)]
    if not async_tests:
        return

    raise pytest.UsageError(
        "Cannot run a complete test suite: pytest-asyncio is not installed, "
        f"and {len(async_tests)} async tests were collected. "
        "Pytest stops here to avoid a false green run with async coverage missing. "
        "Fix: install dev dependencies (`pip install -e .[dev]`) and re-run `pytest`."
    )
