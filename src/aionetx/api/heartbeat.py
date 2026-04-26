"""
Heartbeat request, response, and configuration models.

These dataclasses define the public handshake between transports that schedule
heartbeats and user-supplied heartbeat providers that decide what to send.
"""

from __future__ import annotations

from dataclasses import dataclass

from aionetx.api.bytes_like import BytesLike
from aionetx.api._validation import require_bool, require_positive_finite_number


@dataclass(frozen=True, slots=True)
class HeartbeatRequest:
    """
    Input provided to ``HeartbeatProviderProtocol`` for each heartbeat tick.

    Attributes:
        connection_id (str): Identifier of the currently connected transport.
    """

    connection_id: str


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    """
    Heartbeat provider output consumed by the sender loop.

    Attributes:
        should_send (bool): When ``True``, ``payload`` is sent over the connection.
        payload (BytesLike): Raw heartbeat payload to send when ``should_send`` is true.
            Ignored when ``should_send`` is false.
    """

    should_send: bool
    payload: BytesLike = b""

    def __post_init__(self) -> None:
        # Validate the provider decision at the boundary so the sender never
        # relies on truthiness. Payload type matters only when it will be sent.
        require_bool(
            field_name="HeartbeatResult.should_send", value=self.should_send, error_type=TypeError
        )
        if self.should_send and not isinstance(self.payload, (bytes, bytearray, memoryview)):
            raise TypeError("HeartbeatResult.payload must be bytes-like.")


@dataclass(frozen=True, slots=True)
class TcpHeartbeatSettings:
    """
    TCP heartbeat scheduling configuration.

    Attributes:
        enabled: Whether heartbeat scheduling is active for the transport.
        interval_seconds: Seconds between heartbeat polls while enabled.
    """

    enabled: bool = False
    interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """
        Validate heartbeat configuration.

        Raises:
            InvalidNetworkConfigurationError: If ``enabled`` is not a bool or
                ``interval_seconds`` is not a positive finite number.
        """
        require_bool(field_name="TcpHeartbeatSettings.enabled", value=self.enabled)
        require_positive_finite_number(
            field_name="TcpHeartbeatSettings.interval_seconds",
            value=self.interval_seconds,
        )
