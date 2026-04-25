"""
Shared runtime helpers for asyncio implementations.

This module groups small utilities that are reused across transports, such as
warning rate limiting, reconnect backoff, event-handler validation, and
single-owner-loop enforcement.
"""

from __future__ import annotations

import asyncio
import inspect
import random
import socket
import sys
import time

from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.api.errors import HeartbeatConfigurationError
from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.network_event_handler_protocol import NetworkEventHandlerProtocol
from aionetx.api.reconnect_jitter import ReconnectJitter
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings

_HEARTBEAT_PROVIDER_REQUIRED_MESSAGE = (
    "Heartbeat is enabled but no heartbeat_provider was supplied."
)


class WarningRateLimiter:
    """
    Simple in-memory warning limiter keyed by event name.

    Caps memory use via eviction: once the number of tracked keys reaches
    ``max_keys``, the oldest entry (by insertion/last-update order) is removed
    *before* the new key is inserted, so the dict never exceeds ``max_keys``
    entries.  Suitable for long-running servers with many transient connection
    IDs used as keys.

    Not thread-safe; safe for single-threaded asyncio use (no ``await`` inside
    :meth:`should_log`, so coroutines cannot interleave within it).
    """

    def __init__(self, interval_seconds: float, max_keys: int = 1000) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0.")
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1.")
        self._interval_seconds = interval_seconds
        self._max_keys = max_keys
        self._last_logged_at: dict[str, float] = {}

    def should_log(self, key: str) -> bool:
        """
        Return whether a warning for ``key`` should be emitted right now.

        Args:
            key: Logical warning bucket to rate-limit.

        Returns:
            ``True`` when enough time has elapsed since the last emission for
            ``key``; otherwise ``False``.
        """
        now = time.monotonic()
        previous = self._last_logged_at.get(key)
        if previous is None or (now - previous) >= self._interval_seconds:
            if key not in self._last_logged_at and len(self._last_logged_at) >= self._max_keys:
                # Evict the least-recently-inserted-or-updated entry (Python 3.7+ dict order).
                oldest_key = next(iter(self._last_logged_at))
                del self._last_logged_at[oldest_key]
            self._last_logged_at[key] = now
            return True
        return False


class ReconnectBackoff:
    """Stateful exponential backoff helper for reconnect attempts."""

    def __init__(self, settings: TcpReconnectSettings) -> None:
        settings.validate()
        self._settings = settings
        self._current_delay = settings.initial_delay_seconds

    def reset(self) -> None:
        """Reset delay progression to the configured initial value."""
        self._current_delay = self._settings.initial_delay_seconds

    def next_delay(self) -> float:
        """Return current delay and advance to next backoff step."""
        delay = self._apply_jitter(self._current_delay)
        self._current_delay = min(
            self._current_delay * self._settings.backoff_factor,
            self._settings.max_delay_seconds,
        )
        return delay

    def _apply_jitter(self, delay: float) -> float:
        jitter = self._settings.jitter
        if jitter == ReconnectJitter.NONE:
            return delay
        if jitter == ReconnectJitter.FULL:
            return random.uniform(0.0, delay)
        if jitter == ReconnectJitter.EQUAL:
            return (delay / 2.0) + random.uniform(0.0, delay / 2.0)
        return delay


def assert_running_on_owner_loop(
    *,
    class_name: str,
    owner_loop: asyncio.AbstractEventLoop | None,
) -> asyncio.AbstractEventLoop:
    """
    Raise ``RuntimeError`` if the caller is not on the component's owner loop.

    This guard enforces the asyncio single-event-loop contract for all
    managed transport methods.  A component's owner loop is the event loop
    that called ``start()`` first.  Calling any public method from a
    different loop (or from a thread with no running loop) is a programming
    error and raises immediately with a descriptive message.

    Returns the current running loop so callers can pin it on first use:
    ``self._owner_loop = assert_running_on_owner_loop(...)``.  Storing the
    loop via the return value (rather than a separate ``get_running_loop()``
    call in the transport module) ensures the identity is always drawn from
    this module's ``asyncio`` import — not from a potentially test-patched
    module-level import in the transport.

    Performance: ``asyncio.get_running_loop()`` is a C-level O(1) call; the
    guard adds no measurable overhead on the hot path.
    """
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        raise RuntimeError(
            f"{class_name} methods must be called from within the asyncio "
            "event loop. No running loop detected in the calling thread."
        ) from None
    if owner_loop is not None and current_loop is not owner_loop:
        raise RuntimeError(
            f"{class_name} was started on a different event loop than the "
            "calling thread's current loop. Cross-loop use is not supported. "
            "Use loop.call_soon_threadsafe() to schedule from another thread."
        )
    return current_loop


async def await_task_completion_preserving_cancellation(task: asyncio.Task[object]) -> None:
    """
    Await an internal task without swallowing caller cancellation.

    This helper is for shutdown paths that may cancel and await a child task.
    ``CancelledError`` from the child task is expected during shutdown and is
    suppressed.  Cancellation of the caller, however, is delayed only long
    enough for the shielded child task to settle and is then re-raised.
    """
    caller_cancelled = False
    while True:
        try:
            _ = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            cancelling = getattr(current_task, "cancelling", None)
            if (
                current_task is not None and callable(cancelling) and cancelling()
            ) or not task.done():
                caller_cancelled = True
                # Keep awaiting the shielded child task so repeated caller
                # cancellations cannot detach the internal cleanup task.  The
                # ``not task.done()`` fallback preserves this contract on
                # Python 3.10, where Task.cancelling() is unavailable.
                if task.done():
                    break
                continue
            if task.done() and task.cancelled():
                break
            break
    if caller_cancelled:
        raise asyncio.CancelledError


def validate_async_event_handler(event_handler: NetworkEventHandlerProtocol) -> None:
    """
    Validate the runtime contract for ``NetworkEventHandlerProtocol``.

    We enforce this at construction boundaries so factory and direct concrete
    constructors fail the same way for invalid handlers.
    """
    on_event = getattr(event_handler, "on_event", None)
    if on_event is None or not callable(on_event):
        raise TypeError("event_handler must define a callable async on_event(event) method.")
    on_event_call = getattr(on_event, "__call__", None)
    if not (inspect.iscoroutinefunction(on_event) or inspect.iscoroutinefunction(on_event_call)):
        raise TypeError("event_handler.on_event must be an async function.")


def validate_heartbeat_provider(
    *,
    heartbeat_settings: TcpHeartbeatSettings,
    heartbeat_provider: HeartbeatProviderProtocol | None,
) -> None:
    """
    Validate that heartbeat configuration and provider presence agree.

    Raises:
        HeartbeatConfigurationError: If heartbeat is enabled but no provider
            was supplied.
    """
    if heartbeat_settings.enabled and heartbeat_provider is None:
        raise HeartbeatConfigurationError(_HEARTBEAT_PROVIDER_REQUIRED_MESSAGE)
    if heartbeat_provider is None:
        return

    create_heartbeat = getattr(heartbeat_provider, "create_heartbeat", None)
    if create_heartbeat is None or not callable(create_heartbeat):
        raise TypeError(
            "heartbeat_provider must define a callable async create_heartbeat(request) method."
        )
    create_heartbeat_call = getattr(create_heartbeat, "__call__", None)
    if not (
        inspect.iscoroutinefunction(create_heartbeat)
        or inspect.iscoroutinefunction(create_heartbeat_call)
    ):
        raise TypeError("heartbeat_provider.create_heartbeat must be an async function.")


def configure_listener_bind_socket(
    sock: socket.socket,
    *,
    allow_address_reuse: bool,
) -> None:
    """
    Configure bind semantics for server/receiver sockets with safer Windows defaults.

    On Windows we prefer ``SO_EXCLUSIVEADDRUSE`` for ordinary listeners so a
    second process cannot bind the same local address. Multicast receivers can
    opt into ``allow_address_reuse=True`` because group membership requires the
    traditional reuse semantics there.
    """
    if sys.platform == "win32" and not allow_address_reuse:
        exclusive_addr_use = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive_addr_use is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive_addr_use, 1)
            return
    if allow_address_reuse:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
