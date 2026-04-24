from __future__ import annotations

import pytest

from aionetx.api.bytes_received_event import BytesReceivedEvent
from aionetx.api.connection_events import ConnectionClosedEvent
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.api.typed_event_router import TypedEventRouter


@pytest.mark.asyncio
async def test_typed_event_router_dispatches_registered_handler() -> None:
    router = TypedEventRouter()
    seen: list[bytes] = []

    async def handle_bytes(event: BytesReceivedEvent) -> None:
        seen.append(event.data)

    router.register(BytesReceivedEvent, handle_bytes)

    handled = await router.dispatch(BytesReceivedEvent(resource_id="x", data=b"abc"))

    assert handled is True
    assert seen == [b"abc"]


@pytest.mark.asyncio
async def test_typed_event_router_returns_false_when_unhandled() -> None:
    router = TypedEventRouter()

    handled = await router.dispatch(
        ConnectionClosedEvent(
            resource_id="x",
            previous_state=ConnectionState.CONNECTED,
        )
    )

    assert handled is False


@pytest.mark.asyncio
async def test_typed_event_router_prefers_most_specific_subclass_handler() -> None:
    class DerivedBytesReceivedEvent(BytesReceivedEvent):
        pass

    router = TypedEventRouter()
    calls: list[str] = []

    async def handle_base(event: BytesReceivedEvent) -> None:
        calls.append("base")

    async def handle_derived(event: DerivedBytesReceivedEvent) -> None:
        calls.append("derived")

    router.register(BytesReceivedEvent, handle_base)
    router.register(DerivedBytesReceivedEvent, handle_derived)

    handled = await router.dispatch(DerivedBytesReceivedEvent(resource_id="x", data=b"payload"))

    assert handled is True
    assert calls == ["derived"]


@pytest.mark.asyncio
async def test_typed_event_router_prefers_first_registered_on_equal_specificity() -> None:
    router = TypedEventRouter()
    calls: list[str] = []

    async def first(event: BytesReceivedEvent) -> None:
        calls.append("first")

    async def second(event: BytesReceivedEvent) -> None:
        calls.append("second")

    router.register(BytesReceivedEvent, first)
    router.register(BytesReceivedEvent, second)

    handled = await router.dispatch(BytesReceivedEvent(resource_id="x", data=b"abc"))

    assert handled is True
    assert calls == ["first"]


@pytest.mark.asyncio
async def test_typed_event_router_propagates_handler_exceptions() -> None:
    router = TypedEventRouter()

    async def failing_handler(event: BytesReceivedEvent) -> None:
        raise RuntimeError("router-handler-failed")

    router.register(BytesReceivedEvent, failing_handler)

    with pytest.raises(RuntimeError, match="router-handler-failed"):
        await router.dispatch(BytesReceivedEvent(resource_id="x", data=b"data"))
