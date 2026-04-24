from __future__ import annotations

from inspect import iscoroutinefunction
import socket

import pytest

from aionetx.testing import AwaitableRecordingEventHandler, RecordingEventHandler


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
