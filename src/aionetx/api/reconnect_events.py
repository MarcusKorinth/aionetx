"""
Reconnect-related event dataclasses emitted by supervised TCP clients.

These events describe when a reconnect attempt starts, fails, and when the
next reconnect delay has been scheduled.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReconnectAttemptStartedEvent:
    """
    Event emitted when the client starts a connection or reconnection attempt.

    Attributes:
        resource_id: Canonical TCP client component identifier.
        attempt: One-based attempt counter for the current connection cycle.
    """

    resource_id: str
    attempt: int


@dataclass(frozen=True, slots=True)
class ReconnectAttemptFailedEvent:
    """
    Event emitted when a connection or reconnection attempt fails.

    Attributes:
        resource_id: Canonical TCP client component identifier.
        attempt: Attempt counter that failed.
        error: Original exception raised by the failed attempt.
        next_delay_seconds: Delay before the next attempt when reconnect is
            still scheduled, otherwise ``None``.
    """

    resource_id: str
    attempt: int
    error: Exception
    next_delay_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ReconnectScheduledEvent:
    """
    Event emitted when the next reconnect delay has been scheduled.

    Attributes:
        resource_id: Canonical TCP client component identifier.
        attempt: One-based number of the upcoming attempt.
        delay_seconds: Delay before the next reconnect attempt is started.
    """

    resource_id: str
    attempt: int
    delay_seconds: float
