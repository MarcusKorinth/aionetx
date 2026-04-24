"""
Reconnect policy configuration for managed TCP clients.

The dataclass in this module defines whether reconnect is enabled and how
backoff timing behaves across repeated connection attempts.
"""

from __future__ import annotations

from dataclasses import dataclass

from aionetx.api.errors import InvalidNetworkConfigurationError
from aionetx.api.reconnect_jitter import ReconnectJitter
from aionetx.api._validation import (
    require_bool,
    require_enum_member,
    require_finite_number_at_least,
    require_positive_finite_number,
)


@dataclass(frozen=True, slots=True)
class TcpReconnectSettings:
    """
    Reconnect policy for TCP clients.

    Attributes:
        enabled: Whether reconnect scheduling is active after disconnects or
            handled failures.
        initial_delay_seconds: Delay before the first reconnect attempt.
        max_delay_seconds: Maximum backoff delay after repeated failures.
        backoff_factor: Multiplicative growth factor for delay progression.
        jitter: Randomization strategy applied to each reconnect delay.
    """

    enabled: bool = False
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    backoff_factor: float = 2.0
    jitter: ReconnectJitter = ReconnectJitter.NONE

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """
        Validate reconnect backoff bounds.

        Raises:
            InvalidNetworkConfigurationError: If reconnect timing values are invalid.
        """
        require_bool(field_name="TcpReconnectSettings.enabled", value=self.enabled)
        initial_delay_seconds = require_positive_finite_number(
            field_name="TcpReconnectSettings.initial_delay_seconds",
            value=self.initial_delay_seconds,
        )
        max_delay_seconds = require_positive_finite_number(
            field_name="TcpReconnectSettings.max_delay_seconds",
            value=self.max_delay_seconds,
        )
        if max_delay_seconds < initial_delay_seconds:
            raise InvalidNetworkConfigurationError(
                "TcpReconnectSettings.max_delay_seconds must be >= initial_delay_seconds."
            )
        require_finite_number_at_least(
            field_name="TcpReconnectSettings.backoff_factor",
            value=self.backoff_factor,
            minimum=1.0,
        )
        require_enum_member(
            field_name="TcpReconnectSettings.jitter",
            value=self.jitter,
            enum_type=ReconnectJitter,
        )
