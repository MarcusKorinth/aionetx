"""
Composable typed router for ``NetworkEvent`` handlers.

This module offers a composition-first alternative to subclassing
``BaseNetworkEventHandler`` when callers want one unified ``on_event`` entry
point backed by per-event handlers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from aionetx.api.network_event import NetworkEvent

TEvent = TypeVar("TEvent", bound=NetworkEvent)
EventHandler = Callable[[NetworkEvent], Awaitable[None]]


class TypedEventRouter:
    """
    Composable typed event router for unified `on_event` handlers.

    Use this when you prefer composition over subclassing `BaseNetworkEventHandler`.

    Dispatch is intentionally single-handler (no fan-out):
    1) closest event type in MRO wins,
    2) ties are resolved by earliest registration order.
    """

    def __init__(self) -> None:
        self._handlers: list[tuple[type[object], EventHandler]] = []

    def register(
        self, event_type: type[TEvent], handler: Callable[[TEvent], Awaitable[None]]
    ) -> None:
        """
        Register one async handler for an event type.

        Args:
            event_type: Concrete event class that should be matched.
            handler: Async callback invoked when the best-match dispatch
                algorithm selects ``event_type`` for an incoming event.
        """

        async def _wrapped(event: NetworkEvent) -> None:
            await handler(event)  # type: ignore[arg-type]

        self._handlers.append((event_type, _wrapped))

    async def dispatch(self, event: NetworkEvent) -> bool:
        """
        Dispatch to one best-matching handler.

        Returns ``True`` when a handler is selected and awaited, otherwise
        returns ``False``.
        """
        event_mro = type(event).mro()
        selected_handler: EventHandler | None = None
        best_distance: int | None = None
        best_registration_index: int | None = None

        for registration_index, (event_type, handler) in enumerate(self._handlers):
            if not isinstance(event, event_type):
                continue
            try:
                distance = event_mro.index(event_type)
            except ValueError:
                distance = len(event_mro)

            if (
                best_distance is None
                or distance < best_distance
                or (
                    distance == best_distance
                    and best_registration_index is not None
                    and registration_index < best_registration_index
                )
            ):
                best_distance = distance
                best_registration_index = registration_index
                selected_handler = handler

        if selected_handler is not None:
            await selected_handler(event)
            return True
        return False
