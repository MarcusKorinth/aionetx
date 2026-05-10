"""
Public runtime diagnostic snapshot types.

Diagnostics expose operational counters for managed transports without making
the underlying dispatcher implementation part of the public API.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DispatcherRuntimeStats:
    """
    Point-in-time dispatcher diagnostics for one managed transport.

    The snapshot is cumulative since the owning transport was constructed,
    except queue depth values, which describe the queue at snapshot time.

    Attributes:
        emit_calls_total: Total number of event emission attempts observed.
        enqueued_total: Events accepted into the background dispatcher queue.
        handler_dispatch_attempts_total: Attempts to invoke the event handler.
        handler_failures_total: Handler invocations that raised.
        inline_fallback_total: Background-mode events delivered inline because
            no worker could be used for that emission.
        dropped_backpressure_oldest_total: Events evicted by ``DROP_OLDEST``.
        dropped_backpressure_newest_total: Events rejected by ``DROP_NEWEST``.
        dropped_stop_phase_total: Background-mode events ignored after
            dispatcher shutdown started, plus queued payload events cleared
            when their resource closes during terminal cleanup.
        queue_depth: Current queued event count.
        queue_peak: Highest queued event count observed so far.
    """

    emit_calls_total: int
    enqueued_total: int
    handler_dispatch_attempts_total: int
    handler_failures_total: int
    inline_fallback_total: int
    dropped_backpressure_oldest_total: int
    dropped_backpressure_newest_total: int
    dropped_stop_phase_total: int
    queue_depth: int
    queue_peak: int


__all__ = ("DispatcherRuntimeStats",)
