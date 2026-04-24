"""
Jitter strategies for reconnect backoff calculation.

These enum values control how reconnect delays are randomized between attempts.
"""

from __future__ import annotations

from enum import Enum


class ReconnectJitter(str, Enum):
    """Jitter strategy used for reconnect delay randomization."""

    NONE = "none"
    FULL = "full"
    EQUAL = "equal"
