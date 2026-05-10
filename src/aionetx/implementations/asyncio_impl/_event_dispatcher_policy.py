"""
Internal event-dispatcher stop policy.

The policy is intentionally small: it validates the callback contract used by
handler-failure shutdown without owning any dispatcher lifecycle behavior.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DispatcherStopPolicy:
    """
    Callback policy used by ``STOP_COMPONENT`` handler-failure behavior.

    Attributes:
        enabled: Whether handler failures may request component shutdown.
        callback: Awaitable shutdown callback invoked when the policy is active.
    """

    enabled: bool
    callback: Callable[[], Awaitable[None]] | None = None

    @classmethod
    def disabled(cls) -> DispatcherStopPolicy:
        """Build a policy that never requests component shutdown."""
        return cls(enabled=False, callback=None)

    @classmethod
    def stop_component(cls, callback: Callable[[], Awaitable[None]]) -> DispatcherStopPolicy:
        """
        Build a policy that stops the owning component on handler failure.

        Args:
            callback: Awaitable shutdown callback owned by the component.

        Returns:
            DispatcherStopPolicy: Enabled stop policy bound to ``callback``.
        """
        return cls(enabled=True, callback=callback)

    def validate(self) -> None:
        """
        Validate the internal consistency of the configured stop policy.

        Raises:
            ValueError: If ``enabled`` and ``callback`` disagree about whether
                shutdown is supported.
        """
        if self.enabled and self.callback is None:
            raise ValueError("DispatcherStopPolicy with enabled=True requires a callback.")
        if not self.enabled and self.callback is not None:
            raise ValueError("DispatcherStopPolicy with enabled=False must not provide a callback.")
