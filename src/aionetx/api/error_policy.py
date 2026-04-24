"""
Runtime error-policy enum for supervised TCP clients.

These policies tell client supervision whether failures should stop
immediately, be retried, or be surfaced only through emitted events.
"""

from __future__ import annotations

from enum import Enum


class ErrorPolicy(str, Enum):
    """How client supervision handles runtime connection errors."""

    RETRY = "retry"
    FAIL_FAST = "fail_fast"
    IGNORE = "ignore"
