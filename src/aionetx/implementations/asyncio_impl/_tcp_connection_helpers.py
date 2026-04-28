"""
TCP connection teardown and metadata helpers.

These free functions keep shutdown waiting, writer teardown, and metadata
construction logic shared and testable outside the connection class itself.
"""

from __future__ import annotations

import asyncio
import logging

from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.implementations.asyncio_impl.runtime_utils import WarningRateLimiter


async def await_read_task_shutdown(
    *,
    connection_id: str,
    read_task: asyncio.Task[None],
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
    warning_limiter: WarningRateLimiter,
    timeout_seconds: float,
) -> None:
    """
    Wait for the read task to finish after cancellation, logging anomalies.

    Args:
        connection_id: Canonical connection identifier used in logs.
        read_task: Task running the connection read loop.
        logger: Logger used for warnings and unexpected shutdown errors.
        warning_limiter: Rate limiter used for repeated timeout warnings.
        timeout_seconds: Maximum time to wait for task shutdown.
    """
    try:
        await asyncio.wait_for(read_task, timeout=timeout_seconds)
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            raise
    except TimeoutError:
        if warning_limiter.should_log(f"tcp-connection-read-task-timeout:{connection_id}"):
            logger.warning(
                "Timed out waiting for read task shutdown during close after %.2fs.",
                timeout_seconds,
            )
    except Exception:
        logger.exception("Unexpected error while awaiting read task during close.")


async def await_writer_shutdown(
    *,
    connection_id: str,
    writer: asyncio.StreamWriter,
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
    warning_limiter: WarningRateLimiter,
    timeout_seconds: float,
) -> None:
    """
    Wait for ``writer.wait_closed()`` to finish, logging anomalies.

    Args:
        connection_id: Canonical connection identifier used in logs.
        writer: Stream writer being shut down.
        logger: Logger used for warnings and unexpected shutdown errors.
        warning_limiter: Rate limiter used for repeated timeout warnings.
        timeout_seconds: Maximum time to wait for writer shutdown.
    """
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=timeout_seconds)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        if warning_limiter.should_log(f"tcp-connection-wait-closed-timeout:{connection_id}"):
            logger.warning(
                "Timed out waiting for writer.wait_closed() during close after %.2fs.",
                timeout_seconds,
            )
    except Exception:
        logger.exception("Unexpected error while awaiting writer.wait_closed() during close.")


def build_connection_metadata(
    *,
    connection_id: str,
    role: ConnectionRole,
    writer: asyncio.StreamWriter,
) -> ConnectionMetadata:
    """
    Build connection metadata from the stream writer's addressing information.

    Args:
        connection_id: Canonical connection identifier.
        role: Logical role of the connection endpoint.
        writer: Stream writer exposing socket addressing details.

    Returns:
        A ``ConnectionMetadata`` snapshot with best-effort local and remote
        host/port information.
    """
    local_info = writer.get_extra_info("sockname")
    remote_info = writer.get_extra_info("peername")
    local_host = (
        str(local_info[0]) if isinstance(local_info, tuple) and len(local_info) >= 2 else None
    )
    local_port: int | None = None
    if isinstance(local_info, tuple) and len(local_info) >= 2:
        try:
            local_port = int(local_info[1])
        except (TypeError, ValueError):
            local_port = None
    remote_host = (
        str(remote_info[0]) if isinstance(remote_info, tuple) and len(remote_info) >= 2 else None
    )
    remote_port: int | None = None
    if isinstance(remote_info, tuple) and len(remote_info) >= 2:
        try:
            remote_port = int(remote_info[1])
        except (TypeError, ValueError):
            remote_port = None
    return ConnectionMetadata(
        connection_id=connection_id,
        role=role,
        local_host=local_host,
        local_port=local_port,
        remote_host=remote_host,
        remote_port=remote_port,
    )
