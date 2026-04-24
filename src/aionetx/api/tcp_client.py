"""
Public TCP client settings and protocol definitions.

This module exposes the dataclass users configure and the managed protocol
implemented by concrete TCP client transports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from aionetx.api.connection_protocol import ConnectionProtocol
from aionetx.api.error_policy import ErrorPolicy
from aionetx.api.event_delivery_settings import EventDeliverySettings
from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.api.errors import InvalidNetworkConfigurationError
from aionetx.api.managed_transport_protocol import ManagedTransportProtocol
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api._validation import (
    require_enum_member,
    require_int_range,
    require_non_empty_str,
    require_optional_positive_finite_number,
    require_positive_int,
)


@dataclass(frozen=True, slots=True)
class TcpClientSettings:
    """
    Configuration for a TCP client connection.

    Attributes:
        host: Target host to connect to (non-empty, non-whitespace).
        port: Target TCP port in the range ``1..65535``.
        reconnect: Reconnect scheduling policy. When ``enabled=True`` the
            client retries connection attempts using the configured backoff.
            Defaults to a disabled policy (no automatic reconnect).
        heartbeat: TCP keep-alive scheduling policy. When ``enabled=True`` the
            configured ``HeartbeatProviderProtocol`` is asked every interval
            whether to send a heartbeat payload. Defaults to disabled.
        error_policy: Optional strategy for runtime send/receive failures.
            ``ErrorPolicy.RETRY`` requires ``reconnect.enabled=True``.
            ``None`` (default) lets the transport surface errors via events
            without an implicit retry policy.
        receive_buffer_size: Max bytes read per underlying receive call.
            Must be ``> 0``. Defaults to 4096.
        connect_timeout_seconds: Optional per-attempt connect timeout. Must
            be ``> 0`` when set. ``None`` (default) waits for the OS-level
            connect to resolve.
        event_delivery: Dispatch mode, buffering, and handler-failure policy
            controls for events emitted by this client.
    """

    host: str
    port: int
    reconnect: TcpReconnectSettings = field(default_factory=TcpReconnectSettings)
    heartbeat: TcpHeartbeatSettings = field(default_factory=TcpHeartbeatSettings)
    error_policy: ErrorPolicy | None = None
    receive_buffer_size: int = 4096
    connect_timeout_seconds: float | None = None
    event_delivery: EventDeliverySettings = field(default_factory=EventDeliverySettings)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """
        Validate TCP client configuration.

        Raises:
            InvalidNetworkConfigurationError: If host, port, reconnect, timeout,
                heartbeat, or event-delivery settings are invalid.
        """
        require_non_empty_str(field_name="TcpClientSettings.host", value=self.host)
        require_int_range(
            field_name="TcpClientSettings.port",
            value=self.port,
            min_value=1,
            max_value=65535,
        )
        require_positive_int(
            field_name="TcpClientSettings.receive_buffer_size",
            value=self.receive_buffer_size,
        )
        require_optional_positive_finite_number(
            field_name="TcpClientSettings.connect_timeout_seconds",
            value=self.connect_timeout_seconds,
        )
        if self.error_policy is not None:
            require_enum_member(
                field_name="TcpClientSettings.error_policy",
                value=self.error_policy,
                enum_type=ErrorPolicy,
            )
        if self.error_policy == ErrorPolicy.RETRY and not self.reconnect.enabled:
            raise InvalidNetworkConfigurationError(
                "TcpClientSettings.error_policy='retry' requires reconnect.enabled=True."
            )
        self.reconnect.validate()
        self.heartbeat.validate()
        self.event_delivery.validate()


class TcpClientProtocol(ManagedTransportProtocol, Protocol):
    """
    TCP client contract built on managed lifecycle capability.

    A client exposes at most one active connection at a time. Reconnect, when
    enabled, is still observable through lifecycle/error events rather than
    hidden behind a permanently "connected" abstraction.
    """

    @property
    def connection(self) -> ConnectionProtocol | None:
        """Return active connection if connected, otherwise ``None``."""
        raise NotImplementedError

    async def wait_until_connected(
        self,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> ConnectionProtocol:
        """
        Wait until an active connected connection is available.

        Args:
            timeout_seconds: Optional max wait time; waits forever when ``None``.
            poll_interval_seconds: Positive interval used to re-check failure
                conditions while waiting.

        Raises:
            ConnectionError: If the client has been started and then stopped
                before a connection becomes available, or when connection
                establishment fails without further reconnect attempts.
            asyncio.TimeoutError: When ``timeout_seconds`` is provided and the
                timeout expires first.

        Returns:
            The currently active connection object. The returned connection is
            still subject to later disconnect/close events.
        """
        raise NotImplementedError


__all__ = (
    "TcpClientProtocol",
    "TcpClientSettings",
)
