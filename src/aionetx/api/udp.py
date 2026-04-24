"""
Public UDP sender and receiver settings, protocols, and exceptions.

This module defines the lifecycle-light UDP sender surface together with the
managed UDP receiver protocol and their configuration dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol

from aionetx.api.byte_sender_protocol import ByteSenderProtocol
from aionetx.api.bytes_like import BytesLike
from aionetx.api.event_delivery_settings import EventDeliverySettings
from aionetx.api.managed_transport_protocol import ManagedTransportProtocol
from aionetx.api.errors import NetworkConfigurationError, NetworkRuntimeError
from aionetx.api._validation import (
    require_bool,
    require_int_range,
    require_non_empty_str,
    require_optional_non_empty_str,
    require_positive_int,
)


class UdpSenderProtocol(ByteSenderProtocol, Protocol):
    """
    UDP sender contract built on shared byte-sender capability.

    This role is intentionally stateless/fire-and-forget at API level:
    no lifecycle state/events and no ``ManagedTransportProtocol``.
    Addressing semantics intentionally differ from connected senders:
    ``host``/``port`` can override target selection per send call.
    """

    async def send(
        self,
        data: BytesLike,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        """
        Send raw bytes-like datagram data to a target host and port.

        Args:
            data: Datagram payload to send.
            host: Optional per-call target host override.
            port: Optional per-call target port override.

        Raises:
            TypeError: If ``data`` is not bytes-like.
            UdpInvalidTargetError: If the resolved target host/port is missing
                or invalid.
            UdpSenderStoppedError: If the sender has already been stopped.
            OSError: If the underlying UDP socket reports a send failure.
        """
        ...

    async def stop(self) -> None:
        """
        Close sender socket resources.

        This is resource finalization, not a managed component lifecycle
        transition. Implementations must be idempotent. After stop, send()
        must raise.
        """
        ...

    async def __aenter__(self) -> UdpSenderProtocol:
        """Return ``self``; UDP sender is lazy and needs no explicit start."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the sender's socket unconditionally."""
        ...


@dataclass(frozen=True, slots=True)
class UdpSenderSettings:
    """
    Configuration for sending generic UDP datagrams.

    Target resolution precedence at send-time is:
    1) explicit ``send(..., host=..., port=...)`` arguments
    2) ``default_host`` / ``default_port`` from these settings

    Partial explicit targets are supported. For example ``send(host="x")``
    uses ``port=default_port`` and vice versa.
    """

    default_host: str | None = None
    default_port: int | None = None
    local_host: str = "0.0.0.0"
    local_port: int = 0
    enable_broadcast: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """
        Validate UDP sender configuration.

        Raises:
            InvalidNetworkConfigurationError: If bind or default target values
                are empty or outside valid UDP bounds. Missing default target
                values are allowed when every send call provides a target.
        """
        require_optional_non_empty_str(
            field_name="UdpSenderSettings.default_host", value=self.default_host
        )
        if self.default_port is not None:
            require_int_range(
                field_name="UdpSenderSettings.default_port",
                value=self.default_port,
                min_value=1,
                max_value=65535,
            )
        require_non_empty_str(field_name="UdpSenderSettings.local_host", value=self.local_host)
        require_int_range(
            field_name="UdpSenderSettings.local_port",
            value=self.local_port,
            min_value=0,
            max_value=65535,
        )
        require_bool(
            field_name="UdpSenderSettings.enable_broadcast",
            value=self.enable_broadcast,
        )


class UdpSenderStoppedError(NetworkRuntimeError):
    """Raised when send() is attempted after a UDP sender has been stopped."""


class UdpInvalidTargetError(NetworkConfigurationError):
    """Raised when a UDP send target is missing or invalid."""


class UdpReceiverProtocol(ManagedTransportProtocol, Protocol):
    """UDP datagram receiver contract built on managed lifecycle capability."""


@dataclass(frozen=True, slots=True)
class UdpReceiverSettings:
    """Configuration for receiving generic UDP datagrams."""

    host: str
    port: int
    receive_buffer_size: int = 65535
    enable_broadcast: bool = False
    event_delivery: EventDeliverySettings = field(default_factory=EventDeliverySettings)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """
        Validate UDP receiver configuration.

        Raises:
            InvalidNetworkConfigurationError: If bind or receive-buffer values
                are missing or outside valid UDP bounds.
        """
        require_non_empty_str(field_name="UdpReceiverSettings.host", value=self.host)
        require_int_range(
            field_name="UdpReceiverSettings.port",
            value=self.port,
            min_value=1,
            max_value=65535,
        )
        require_positive_int(
            field_name="UdpReceiverSettings.receive_buffer_size",
            value=self.receive_buffer_size,
        )
        require_bool(
            field_name="UdpReceiverSettings.enable_broadcast",
            value=self.enable_broadcast,
        )
        self.event_delivery.validate()
