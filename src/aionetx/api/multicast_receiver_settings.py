"""
Configuration model for UDP multicast receivers.

The dataclass in this module describes group membership, bind/interface
addresses, receive-buffer size, and event-delivery behavior for multicast
receivers.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from aionetx.api.event_delivery_settings import EventDeliverySettings
from aionetx.api.errors import InvalidNetworkConfigurationError
from aionetx.api._validation import (
    require_int_range,
    require_non_empty_str,
    require_positive_int,
)


def _validate_ipv4_address(*, field_name: str, value: str) -> ipaddress.IPv4Address:
    """Return a parsed IPv4 address or raise the public configuration error."""
    try:
        return ipaddress.IPv4Address(value)
    except ValueError as error:
        raise InvalidNetworkConfigurationError(
            f"MulticastReceiverSettings.{field_name} must be a valid IPv4 address."
        ) from error


@dataclass(frozen=True, slots=True)
class MulticastReceiverSettings:
    """
    Configuration for receiving UDP multicast datagrams.

    Attributes:
        group_ip: Multicast group address to join.
        port: UDP port used for bind and group membership.
        bind_ip: Local bind address for the socket.
        interface_ip: Interface address used for multicast membership.
        receive_buffer_size: Maximum bytes read from the socket per receive.
        event_delivery: Event dispatch configuration for emitted receive and
            lifecycle events.
    """

    group_ip: str
    port: int
    bind_ip: str = "0.0.0.0"
    interface_ip: str = "0.0.0.0"
    receive_buffer_size: int = 65535
    event_delivery: EventDeliverySettings = field(default_factory=EventDeliverySettings)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """
        Validate multicast socket settings.

        Raises:
            InvalidNetworkConfigurationError: If any required value is missing
                or outside valid UDP/socket bounds.
        """
        require_non_empty_str(field_name="MulticastReceiverSettings.group_ip", value=self.group_ip)
        require_int_range(
            field_name="MulticastReceiverSettings.port",
            value=self.port,
            min_value=1,
            max_value=65535,
        )
        require_non_empty_str(field_name="MulticastReceiverSettings.bind_ip", value=self.bind_ip)
        require_non_empty_str(
            field_name="MulticastReceiverSettings.interface_ip", value=self.interface_ip
        )
        group_address = _validate_ipv4_address(field_name="group_ip", value=self.group_ip)
        if not group_address.is_multicast:
            raise InvalidNetworkConfigurationError(
                "MulticastReceiverSettings.group_ip must be an IPv4 multicast address."
            )
        _validate_ipv4_address(field_name="bind_ip", value=self.bind_ip)
        _validate_ipv4_address(field_name="interface_ip", value=self.interface_ip)
        require_positive_int(
            field_name="MulticastReceiverSettings.receive_buffer_size",
            value=self.receive_buffer_size,
        )
        self.event_delivery.validate()
