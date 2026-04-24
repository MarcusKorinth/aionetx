"""
Asyncio heartbeat loop bound to one logical connection.

This module contains the periodic sender used by TCP client and server
heartbeats. It polls a heartbeat provider, optionally sends payload bytes, and
reports failures through the shared event dispatcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aionetx.api.connection_protocol import ConnectionProtocol
from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.heartbeat import HeartbeatRequest, HeartbeatResult, TcpHeartbeatSettings
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher
from aionetx.implementations.asyncio_impl.runtime_utils import WarningRateLimiter


logger = logging.getLogger(__name__)
_warning_limiter = WarningRateLimiter(interval_seconds=30.0)


class AsyncioHeartbeatSender:
    """
    Periodic heartbeat sender bound to a single connection.

    The sender exits automatically when the connection is no longer connected,
    which prevents stale heartbeat loops after teardown/reconnect.
    """

    def __init__(
        self,
        connection: ConnectionProtocol,
        settings: TcpHeartbeatSettings,
        heartbeat_provider: HeartbeatProviderProtocol,
        event_dispatcher: AsyncioEventDispatcher,
    ) -> None:
        """Create a heartbeat scheduler for one connection."""
        settings.validate()
        if not settings.enabled:
            raise ValueError("AsyncioHeartbeatSender requires TcpHeartbeatSettings.enabled=True.")

        self._connection = connection
        self._settings = settings
        self._heartbeat_provider = heartbeat_provider
        self._event_dispatcher = event_dispatcher

        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._state_lock = asyncio.Lock()
        self._logger = logging.LoggerAdapter(
            logger,
            {
                "component": "heartbeat_sender",
                "connection_id": self._connection.connection_id,
            },
        )

    @property
    def is_running(self) -> bool:
        """Return ``True`` while the heartbeat loop is scheduled to keep running."""
        return self._running

    async def start(self) -> None:
        """Start periodic heartbeat scheduling."""
        async with self._state_lock:
            if self._running:
                self._logger.debug(
                    "Heartbeat sender start called while already running for %s.",
                    self._connection.connection_id,
                )
                return
            self._running = True
            self._logger.debug(
                "Starting heartbeat sender for %s (interval=%ss).",
                self._connection.connection_id,
                self._settings.interval_seconds,
                extra={"heartbeat_interval_seconds": self._settings.interval_seconds},
            )
            self._task = asyncio.create_task(
                self._run(),
                name=f"{self._connection.connection_id}-heartbeat-sender",
            )

    async def stop(self) -> None:
        """
        Stop heartbeat scheduling.

        This method is idempotent.
        """
        async with self._state_lock:
            if not self._running:
                self._logger.debug(
                    "Heartbeat sender stop called while already stopped for %s.",
                    self._connection.connection_id,
                )
                return
            self._running = False
            task = self._task
            self._task = None

        if task is not None:
            self._logger.debug("Stopping heartbeat sender for %s.", self._connection.connection_id)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        """
        Drive heartbeat ticks until stopped or connection closes.

        Any exception from provider/send is emitted as an error event and stops
        the loop to avoid repeatedly failing at high frequency.

        TOCTOU note: ``is_connected`` is checked before calling ``send()``, but
        the connection may close in the interval between the check and the actual
        send call.  If that happens, ``send()`` raises ``ConnectionClosedError``
        (or a subclass), which falls through to the ``except Exception`` handler
        and is emitted as a ``NetworkErrorEvent`` — the same path used for any
        other send failure.  The loop then exits so no further heartbeats are
        attempted on the dead connection.
        """
        try:
            while self._running:
                await asyncio.sleep(self._settings.interval_seconds)
                if not self._running or not self._connection.is_connected:
                    self._logger.debug(
                        "Heartbeat sender exiting for %s because running=%s connected=%s.",
                        self._connection.connection_id,
                        self._running,
                        self._connection.is_connected,
                        extra={
                            "running": self._running,
                            "connected": self._connection.is_connected,
                        },
                    )
                    break

                result = await self._heartbeat_provider.create_heartbeat(
                    HeartbeatRequest(connection_id=self._connection.connection_id)
                )
                if not isinstance(result, HeartbeatResult):
                    raise TypeError(
                        "heartbeat_provider.create_heartbeat must return HeartbeatResult."
                    )
                if result.should_send:
                    if not isinstance(result.payload, (bytes, bytearray, memoryview)):
                        raise TypeError(
                            "HeartbeatResult.payload must be bytes-like when should_send=True."
                        )
                    await self._connection.send(result.payload)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if _warning_limiter.should_log(f"heartbeat-sender:{self._connection.connection_id}"):
                self._logger.warning(
                    "Heartbeat sender failed for %s: %s",
                    self._connection.connection_id,
                    error,
                    extra={"error_type": type(error).__name__},
                )
            await self._safe_emit_error(error)
        finally:
            async with self._state_lock:
                self._running = False
                self._task = None
            self._logger.debug("Heartbeat sender finalized for %s.", self._connection.connection_id)

    async def _safe_emit_error(self, error: Exception) -> None:
        """Best-effort error emission for heartbeat loop failures."""
        try:
            await self._event_dispatcher.emit(
                NetworkErrorEvent(
                    resource_id=f"{self._connection.connection_id}:heartbeat",
                    error=error,
                )
            )
        except Exception:
            self._logger.exception(
                "Failed to emit heartbeat error event for %s.", self._connection.connection_id
            )
