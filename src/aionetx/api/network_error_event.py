"""
Event type used to surface caught transport-side exceptions.

Implementations emit this event when they catch an exception and choose to
report it through the event stream instead of raising it to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NetworkErrorEvent:
    """
    Event emitted when transport-side logic catches an exception.

    Attributes:
        resource_id (str): Identifier of the component that detected the error.
        error (Exception): Original exception instance.
    """

    resource_id: str
    error: Exception
