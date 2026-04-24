"""
Shared lifecycle protocol for managed transport components.

This protocol captures the common start/stop/context-manager behavior that TCP
clients, TCP servers, and datagram receivers all expose.
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, TypeVar

from aionetx.api.component_lifecycle_state import ComponentLifecycleState

_TManagedTransport = TypeVar("_TManagedTransport", bound="ManagedTransportProtocol")


class ManagedTransportProtocol(Protocol):
    """
    Capability protocol for components with explicit lifecycle management.

    All managed transports use the same transition model:
    ``STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED``.
    Failed startup may transition ``STARTING -> STOPPED``.

    Guarantees only shared lifecycle participation via:
    - ``start()``
    - ``stop()``
    - ``lifecycle_state``
    - async context manager (``async with``)

    It intentionally does *not* guarantee connection shape, reconnect behavior,
    background-task strategy, or client/server-specific features.
    """

    @property
    def lifecycle_state(self) -> ComponentLifecycleState:
        """Return current component lifecycle state."""
        ...

    async def start(self) -> None:
        """
        Start the component lifecycle.

        Calling ``start()`` when the component is already starting or running
        must be a no-op. Successful return means the managed runtime owns its
        background resources and lifecycle publication has begun.
        """
        ...

    async def stop(self) -> None:
        """
        Stop the component lifecycle.

        Implementations must be safe to call repeatedly. Once ``stop()``
        returns, owned background tasks and transport resources must no longer
        continue running on behalf of the component.
        """
        ...

    async def __aenter__(self: _TManagedTransport) -> _TManagedTransport:
        """Start the component and return ``self`` for use as a context variable."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Stop the component unconditionally, regardless of any exception."""
        ...
