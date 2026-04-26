"""
Shared helpers for TCP server coordination and teardown reporting.

These free functions keep accept handling, heartbeat sender ownership,
best-effort broadcast, and wait/poll logic focused and reusable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable

from aionetx.api.bytes_like import BytesLike
from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_events import ConnectionRejectedEvent
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.network_error_event import NetworkErrorEvent
from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.implementations.asyncio_impl.asyncio_heartbeat_sender import AsyncioHeartbeatSender
from aionetx.implementations.asyncio_impl.asyncio_tcp_connection import (
    AsyncioTcpConnection,
    ConnectionClosedCallback,
)
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher
from aionetx.implementations.asyncio_impl.runtime_utils import validate_heartbeat_provider


async def report_teardown_errors(
    *,
    event_dispatcher: AsyncioEventDispatcher,
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
    component_id: str,
    operation: str,
    targets: Iterable[str],
    results: Iterable[object],
) -> None:
    """
    Emit warnings and best-effort error events for teardown failures.

    Args:
        event_dispatcher: Dispatcher used for ``NetworkErrorEvent`` emission.
        logger: Logger used for human-readable teardown warnings.
        component_id: Canonical component identifier for emitted errors.
        operation: Short operation label such as ``TCP connection close``.
        targets: Logical targets paired with ``results`` for diagnostics.
        results: Gathered teardown results that may include exceptions.
    """
    for target, result in zip(targets, results, strict=False):
        if result is None:
            continue
        if isinstance(result, BaseException):
            logger.warning(
                "Best-effort teardown %s failed for %s: %s",
                operation,
                target,
                result,
            )
            if isinstance(result, Exception):
                try:
                    await event_dispatcher.emit(
                        NetworkErrorEvent(resource_id=component_id, error=result),
                    )
                except Exception as reporting_error:
                    logger.warning(
                        "Failed to emit teardown NetworkErrorEvent for %s on %s: %s",
                        operation,
                        target,
                        reporting_error,
                    )


async def wait_until_server_running(
    *,
    get_lifecycle_state: Callable[[], ComponentLifecycleState],
    get_has_started: Callable[[], bool],
    get_status_version: Callable[[], int],
    status_changed: asyncio.Event,
    host: str,
    port: int,
    poll_interval_seconds: float,
) -> None:
    """Poll loop core for AsyncioTcpServer.wait_until_running."""
    while True:
        if get_lifecycle_state() == ComponentLifecycleState.RUNNING:
            return
        if get_lifecycle_state() == ComponentLifecycleState.STOPPED and get_has_started():
            raise ConnectionError(f"TCP server for {host}:{port} is stopped.")
        observed_version = get_status_version()
        status_changed.clear()
        if get_status_version() != observed_version:
            continue
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(status_changed.wait(), timeout=poll_interval_seconds)


async def start_server_heartbeat_if_needed(
    *,
    connection: AsyncioTcpConnection,
    heartbeat_settings: TcpHeartbeatSettings,
    heartbeat_provider: HeartbeatProviderProtocol | None,
    event_dispatcher: AsyncioEventDispatcher,
    state_lock: asyncio.Lock,
    get_lifecycle_state: Callable[[], ComponentLifecycleState],
    connections: dict[str, AsyncioTcpConnection],
    heartbeat_senders: dict[str, AsyncioHeartbeatSender],
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
) -> None:
    """Start a heartbeat sender for a newly accepted connection if heartbeat is enabled."""
    validate_heartbeat_provider(
        heartbeat_settings=heartbeat_settings,
        heartbeat_provider=heartbeat_provider,
    )
    if not heartbeat_settings.enabled:
        return
    # Keep the invariant explicit at this boundary so a missing provider fails
    # immediately even if the validation call above changes in the future.
    if heartbeat_provider is None:
        raise RuntimeError(
            "Heartbeat is enabled but no heartbeat_provider was supplied. "
            "This is an internal invariant violation."
        )
    sender = AsyncioHeartbeatSender(
        connection=connection,
        settings=heartbeat_settings,
        heartbeat_provider=heartbeat_provider,
        event_dispatcher=event_dispatcher,
    )
    should_start = False
    async with state_lock:
        if (
            get_lifecycle_state() == ComponentLifecycleState.RUNNING
            and connection.connection_id in connections
        ):
            heartbeat_senders[connection.connection_id] = sender
            should_start = True
    if not should_start:
        return
    logger.debug("Starting heartbeat sender for %s.", connection.connection_id)
    try:
        await sender.start()
    except Exception:
        async with state_lock:
            heartbeat_senders.pop(connection.connection_id, None)
        raise


async def stop_server_heartbeat_senders(
    *,
    senders: Iterable[AsyncioHeartbeatSender],
    event_dispatcher: AsyncioEventDispatcher,
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
    component_id: str,
) -> None:
    """Stop a collection of heartbeat senders and report any teardown errors."""
    sender_list = tuple(senders)
    if sender_list:
        stop_results = await asyncio.gather(
            *(sender.stop() for sender in sender_list),
            return_exceptions=True,
        )
        await report_teardown_errors(
            event_dispatcher=event_dispatcher,
            logger=logger,
            component_id=component_id,
            operation="heartbeat sender stop",
            targets=(f"sender[{index}]" for index, _sender in enumerate(sender_list, start=1)),
            results=stop_results,
        )


async def broadcast_to_connections(
    *,
    connections: Iterable[AsyncioTcpConnection],
    data: BytesLike,
    event_dispatcher: AsyncioEventDispatcher,
    component_id: str,
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
    concurrency_limit: int,
    send_timeout_seconds: float | None,
) -> None:
    """Send data to a snapshot of live connections with bounded fan-out concurrency."""
    live_connections = tuple(connection for connection in connections if connection.is_connected)
    if not live_connections:
        return

    semaphore = asyncio.Semaphore(concurrency_limit)

    async def _send_one(connection: AsyncioTcpConnection) -> Exception | None:
        async with semaphore:
            try:
                if send_timeout_seconds is None:
                    await connection.send(data)
                else:
                    await asyncio.wait_for(
                        connection.send(data),
                        timeout=send_timeout_seconds,
                    )
                return None
            except Exception as error:
                return error

    results = await asyncio.gather(*(_send_one(connection) for connection in live_connections))

    failed_connections: list[AsyncioTcpConnection] = []
    for connection, result in zip(live_connections, results, strict=True):
        if result is None:
            continue
        logger.warning(
            "TCP server broadcast send failed for connection %s: %s",
            connection.connection_id,
            result,
        )
        failed_connections.append(connection)
        with contextlib.suppress(Exception):
            await event_dispatcher.emit(NetworkErrorEvent(resource_id=component_id, error=result))

    if not failed_connections:
        return

    close_results = await asyncio.gather(
        *(connection.close() for connection in failed_connections),
        return_exceptions=True,
    )
    await report_teardown_errors(
        event_dispatcher=event_dispatcher,
        logger=logger,
        component_id=component_id,
        operation="TCP broadcast failed connection close",
        targets=(connection.connection_id for connection in failed_connections),
        results=close_results,
    )


def _extract_remote_peer_address(peer_info: object) -> tuple[str | None, int | None]:
    """Return best-effort remote host/port extracted from ``peername``-style metadata."""
    if not (isinstance(peer_info, tuple) and len(peer_info) >= 2):
        return None, None
    remote_host = str(peer_info[0])
    try:
        remote_port = int(peer_info[1])
    except (TypeError, ValueError):
        remote_port = None
    return remote_host, remote_port


async def handle_accepted_client(
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    state_lock: asyncio.Lock,
    get_lifecycle_state: Callable[[], ComponentLifecycleState],
    get_connection_id: Callable[[], str],
    connections: dict[str, AsyncioTcpConnection],
    event_dispatcher: AsyncioEventDispatcher,
    max_connections: int,
    receive_buffer_size: int,
    idle_timeout_seconds: float | None,
    connection_send_timeout_seconds: float | None,
    on_closed_callback: ConnectionClosedCallback,
    heartbeat_settings: TcpHeartbeatSettings,
    heartbeat_provider: HeartbeatProviderProtocol | None,
    heartbeat_senders: dict[str, AsyncioHeartbeatSender],
    component_id: str,
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
) -> None:
    """Initialize one accepted socket into managed connection state."""
    peer_info = writer.get_extra_info("peername")
    remote_host, remote_port = _extract_remote_peer_address(peer_info)
    connection: AsyncioTcpConnection | None = None
    rejection_event: ConnectionRejectedEvent | None = None
    async with state_lock:
        connection_id = get_connection_id()
        if get_lifecycle_state() != ComponentLifecycleState.RUNNING:
            logger.debug(
                "Rejecting accepted TCP client connection %s because lifecycle=%s.",
                connection_id,
                get_lifecycle_state().value,
            )
            should_reject = True
        elif len(connections) >= max_connections:
            logger.warning(
                "Rejecting accepted TCP client connection %s because max_connections=%s is reached.",
                connection_id,
                max_connections,
            )
            should_reject = True
            rejection_event = ConnectionRejectedEvent(
                resource_id=component_id,
                connection_id=connection_id,
                reason="max_connections_reached",
                remote_host=remote_host,
                remote_port=remote_port,
            )
        else:
            logger.debug("Accepted TCP client connection %s.", connection_id)
            should_reject = False
            connection = AsyncioTcpConnection(
                connection_id=connection_id,
                role=ConnectionRole.SERVER,
                reader=reader,
                writer=writer,
                event_dispatcher=event_dispatcher,
                receive_buffer_size=receive_buffer_size,
                idle_timeout_seconds=idle_timeout_seconds,
                on_closed_callback=on_closed_callback,
                send_timeout_seconds=connection_send_timeout_seconds,
            )
            connections[connection_id] = connection
    if should_reject:
        if rejection_event is not None:
            try:
                await event_dispatcher.emit(rejection_event)
            except Exception as error:
                logger.warning(
                    "Failed to emit ConnectionRejectedEvent for %s: %s",
                    connection_id,
                    error,
                )
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return
    if connection is None:
        raise RuntimeError(
            "Connection is None after non-rejected accept. This is an internal invariant violation."
        )
    try:
        await connection.start()
        await start_server_heartbeat_if_needed(
            connection=connection,
            heartbeat_settings=heartbeat_settings,
            heartbeat_provider=heartbeat_provider,
            event_dispatcher=event_dispatcher,
            state_lock=state_lock,
            get_lifecycle_state=get_lifecycle_state,
            connections=connections,
            heartbeat_senders=heartbeat_senders,
            logger=logger,
        )
    except (Exception, asyncio.CancelledError) as error:
        logger.warning("Failed to initialize connection %s: %s", connection_id, error)
        if isinstance(error, Exception):
            try:
                await event_dispatcher.emit(
                    NetworkErrorEvent(resource_id=component_id, error=error)
                )
            except Exception as reporting_error:
                logger.warning(
                    "Failed to emit NetworkErrorEvent for accepted connection %s: %s",
                    connection_id,
                    reporting_error,
                )
        try:
            await connection.close()
        except Exception as close_error:
            logger.warning(
                "Failed to close partially accepted connection %s: %s",
                connection_id,
                close_error,
            )
        if isinstance(error, asyncio.CancelledError):
            raise
