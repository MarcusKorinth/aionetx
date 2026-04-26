"""
Public TCP server settings and protocol definitions.

This module exposes the dataclass users configure and the managed protocol
implemented by concrete TCP server transports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from aionetx.api.bytes_like import BytesLike
from aionetx.api.connection_protocol import ConnectionProtocol
from aionetx.api.event_delivery_settings import EventDeliverySettings
from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.api.managed_transport_protocol import ManagedTransportProtocol
from aionetx.api._validation import (
    require_int_range,
    require_non_empty_str,
    require_optional_positive_finite_number,
    require_positive_int,
)


@dataclass(frozen=True, slots=True)
class TcpServerSettings:
    """
    Configuration for a TCP server listener.

    Attributes:
        host: Bind address (non-empty, non-whitespace). Use ``"0.0.0.0"`` or
            ``"::"`` to listen on all interfaces.
        port: Listen port in the range ``1..65535``.
        max_connections: Maximum simultaneously tracked live client connections.
            Must be ``> 0`` and is intentionally explicit because the library is
            transport-level and should not silently accept unbounded server fan-in.
        connection_idle_timeout_seconds: Optional per-connection receive idle timeout.
            ``None`` disables idle-timeout enforcement.
        broadcast_concurrency_limit: Maximum number of concurrent per-connection
            broadcast sends. Must be ``> 0``.
        broadcast_send_timeout_seconds: Optional outer timeout applied to each
            individual per-connection broadcast send. ``None`` disables only
            this broadcast-specific wrapper; ``connection_send_timeout_seconds``
            may still bound the underlying connection ``send()``.
        connection_send_timeout_seconds: Optional timeout applied to accepted
            connection ``send()`` flushes, including direct connection sends,
            heartbeat sends, and broadcast delivery. Must be ``> 0`` when set.
            ``None`` disables send-timeout enforcement. Defaults to 30.0.
        heartbeat: Server-side TCP keep-alive scheduling policy. When
            ``enabled=True`` the configured ``HeartbeatProviderProtocol`` is
            asked every interval whether to send a heartbeat payload to each
            connected client. Defaults to disabled.
        receive_buffer_size: Max bytes read per underlying receive call for
            each accepted connection. Must be ``> 0``. Defaults to 4096.
        backlog: Maximum queued pending connections passed to ``listen(2)``.
            Must be ``> 0``. Defaults to 100.
        event_delivery: Dispatch mode, buffering, and handler-failure policy
            controls for events emitted by this server.
    """

    host: str
    port: int
    max_connections: int
    connection_idle_timeout_seconds: float | None = None
    broadcast_concurrency_limit: int = 64
    broadcast_send_timeout_seconds: float | None = None
    connection_send_timeout_seconds: float | None = 30.0
    heartbeat: TcpHeartbeatSettings = field(default_factory=TcpHeartbeatSettings)
    receive_buffer_size: int = 4096
    backlog: int = 100
    event_delivery: EventDeliverySettings = field(default_factory=EventDeliverySettings)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """
        Validate TCP server listener settings.

        Raises:
            InvalidNetworkConfigurationError: If host/port/backlog/buffer inputs
                are invalid.
        """
        require_non_empty_str(field_name="TcpServerSettings.host", value=self.host)
        require_int_range(
            field_name="TcpServerSettings.port",
            value=self.port,
            min_value=1,
            max_value=65535,
        )
        require_positive_int(
            field_name="TcpServerSettings.max_connections", value=self.max_connections
        )
        require_optional_positive_finite_number(
            field_name="TcpServerSettings.connection_idle_timeout_seconds",
            value=self.connection_idle_timeout_seconds,
        )
        require_positive_int(
            field_name="TcpServerSettings.broadcast_concurrency_limit",
            value=self.broadcast_concurrency_limit,
        )
        require_optional_positive_finite_number(
            field_name="TcpServerSettings.broadcast_send_timeout_seconds",
            value=self.broadcast_send_timeout_seconds,
        )
        require_optional_positive_finite_number(
            field_name="TcpServerSettings.connection_send_timeout_seconds",
            value=self.connection_send_timeout_seconds,
        )
        require_positive_int(
            field_name="TcpServerSettings.receive_buffer_size",
            value=self.receive_buffer_size,
        )
        require_positive_int(field_name="TcpServerSettings.backlog", value=self.backlog)
        self.heartbeat.validate()
        self.event_delivery.validate()


class TcpServerProtocol(ManagedTransportProtocol, Protocol):
    """
    TCP server contract built on managed lifecycle capability.

    The server owns listener lifecycle plus a tracked set of accepted
    connections. Connection admission, timeout, and broadcast behavior are
    explicit settings concerns rather than hidden implementation details.
    """

    @property
    def connections(self) -> tuple[ConnectionProtocol, ...]:
        """
        Return a snapshot of currently tracked client connections.

        Rejected or already-closed connections are never part of this tuple.
        """
        raise NotImplementedError

    async def wait_until_running(
        self,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        """
        Wait until server lifecycle reaches RUNNING.

        Raises:
            ConnectionError: If the server is started and then returns to
                STOPPED before reaching RUNNING.
            asyncio.TimeoutError: When ``timeout_seconds`` is provided and
                timeout expires.
        """
        raise NotImplementedError

    async def broadcast(self, data: BytesLike) -> None:
        """
        Send raw bytes-like payload to the current connection snapshot.

        Implementations may fan out concurrently, but a slow client must not
        serialize all peers behind it. Per-connection failures remain
        observable through events and may still cause this call to raise after
        best-effort delivery/cleanup.
        """
        raise NotImplementedError


__all__ = (
    "TcpServerProtocol",
    "TcpServerSettings",
)
