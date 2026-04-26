"""
TCP client connection and heartbeat helpers.

These free functions keep connection establishment, heartbeat sender ownership,
and wait logic focused and reusable outside the main client class.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.connection_protocol import ConnectionProtocol
from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.implementations.asyncio_impl.asyncio_heartbeat_sender import AsyncioHeartbeatSender
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher
from aionetx.implementations.asyncio_impl.identifier_utils import tcp_client_connection_id
from aionetx.implementations.asyncio_impl.runtime_utils import (
    WarningRateLimiter,
    validate_heartbeat_provider,
)

if TYPE_CHECKING:
    from aionetx.implementations.asyncio_impl.asyncio_tcp_connection import AsyncioTcpConnection

_warning_limiter = WarningRateLimiter(interval_seconds=30.0)


async def connect_once(
    *,
    settings: TcpClientSettings,
    connection_opener: Callable[..., Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]],
    event_dispatcher: AsyncioEventDispatcher,
    on_closed_callback: Callable[["AsyncioTcpConnection"], Awaitable[None] | None],
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
    component_id: str,
) -> "AsyncioTcpConnection":
    """
    Open one TCP session and return an initialized connection object.

    Applies keepalive, builds the connection ID, and starts the connection.
    The caller is responsible for heartbeat start after this returns.
    """
    from aionetx.implementations.asyncio_impl.asyncio_tcp_connection import AsyncioTcpConnection

    connect_coro = connection_opener(host=settings.host, port=settings.port)
    timeout = settings.connect_timeout_seconds
    if timeout is not None:
        try:
            reader, writer = await asyncio.wait_for(connect_coro, timeout=timeout)
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"TCP connect to {settings.host}:{settings.port} timed out after {timeout:.3g}s."
            ) from None
    else:
        reader, writer = await connect_coro
    logger.debug("Established socket to %s:%s.", settings.host, settings.port)
    _apply_tcp_keepalive(writer=writer, component_id=component_id, logger=logger)
    peer_info = writer.get_extra_info("peername")
    connection_id = tcp_client_connection_id(settings.host, settings.port, peer_info)
    connection = AsyncioTcpConnection(
        connection_id=connection_id,
        role=ConnectionRole.CLIENT,
        reader=reader,
        writer=writer,
        event_dispatcher=event_dispatcher,
        receive_buffer_size=settings.receive_buffer_size,
        on_closed_callback=on_closed_callback,
        # Public client settings do not expose send timeouts yet; keep the
        # managed-client behavior unchanged until that contract exists.
        send_timeout_seconds=None,
    )
    try:
        await connection.start()
    except (Exception, asyncio.CancelledError):
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await connection.close()
        raise
    return connection


def _apply_tcp_keepalive(
    *,
    writer: asyncio.StreamWriter,
    component_id: str,
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
) -> None:
    raw_socket = writer.get_extra_info("socket")
    if raw_socket is None:
        return
    try:
        raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        if _warning_limiter.should_log(f"{component_id}/keepalive"):
            logger.warning("Failed to enable TCP keepalive on client socket.")


async def start_heartbeat_sender(
    *,
    connection: "AsyncioTcpConnection",
    settings: TcpClientSettings,
    heartbeat_provider: HeartbeatProviderProtocol | None,
    event_dispatcher: AsyncioEventDispatcher,
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
) -> "AsyncioHeartbeatSender | None":
    """Start a heartbeat sender for the connection and return it, or ``None`` if disabled."""
    validate_heartbeat_provider(
        heartbeat_settings=settings.heartbeat,
        heartbeat_provider=heartbeat_provider,
    )
    if not settings.heartbeat.enabled:
        return None
    # Keep the invariant explicit at this boundary so a missing provider fails
    # immediately even if the validation call above changes in the future.
    if heartbeat_provider is None:
        raise RuntimeError(
            "Heartbeat is enabled but no heartbeat_provider was supplied. "
            "This is an internal invariant violation."
        )
    sender = AsyncioHeartbeatSender(
        connection=connection,
        settings=settings.heartbeat,
        heartbeat_provider=heartbeat_provider,
        event_dispatcher=event_dispatcher,
    )
    await sender.start()
    logger.debug("Started TCP client heartbeat sender.")
    return sender


async def stop_heartbeat_sender(
    *,
    sender: "AsyncioHeartbeatSender | None",
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
) -> None:
    """Stop a heartbeat sender if one is active."""
    if sender is not None:
        await sender.stop()
        logger.debug("Stopped TCP client heartbeat sender.")


async def wait_until_client_connected(
    *,
    get_connection: Callable[[], "ConnectionProtocol | None"],
    get_lifecycle_state: Callable[[], ComponentLifecycleState],
    get_has_started: Callable[[], bool],
    get_last_connect_error: Callable[[], Exception | None],
    get_reconnect_enabled: Callable[[], bool],
    get_status_version: Callable[[], int],
    status_changed: asyncio.Event,
    host: str,
    port: int,
    poll_interval_seconds: float,
) -> "ConnectionProtocol":
    """Core poll loop for :meth:`AsyncioTcpClient.wait_until_connected`."""
    while True:
        connection = get_connection()
        if connection is not None and connection.is_connected:
            return connection
        if get_lifecycle_state() == ComponentLifecycleState.STOPPED and get_has_started():
            raise ConnectionError(f"TCP client for {host}:{port} is stopped.")
        last_error = get_last_connect_error()
        if last_error is not None and (
            not get_reconnect_enabled() or get_lifecycle_state() == ComponentLifecycleState.STOPPED
        ):
            raise ConnectionError(f"TCP client failed to connect to {host}:{port}.") from last_error
        observed_version = get_status_version()
        status_changed.clear()
        if get_status_version() != observed_version:
            continue
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(status_changed.wait(), timeout=poll_interval_seconds)
