"""
Public factory protocol for constructing supported transport roles.

Applications can depend on this protocol when they want to abstract over the
concrete backend that creates TCP, UDP, and multicast transports.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.multicast_receiver_protocol import MulticastReceiverProtocol
from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
from aionetx.api.network_event_handler_protocol import NetworkEventHandlerProtocol
from aionetx.api.tcp_client import TcpClientProtocol, TcpClientSettings
from aionetx.api.tcp_server import TcpServerProtocol, TcpServerSettings
from aionetx.api.udp import (
    UdpReceiverProtocol,
    UdpReceiverSettings,
    UdpSenderProtocol,
    UdpSenderSettings,
)


@runtime_checkable
class NetworkFactory(Protocol):
    """
    Explicit transport-construction contract for supported backends.

    The factory boundary is intentionally transport-only: it constructs TCP,
    UDP, and multicast transport roles, but it does not add framing, parsing,
    or application-level protocol behavior.
    """

    def create_tcp_client(
        self,
        settings: TcpClientSettings,
        event_handler: NetworkEventHandlerProtocol,
        heartbeat_provider: HeartbeatProviderProtocol | None = None,
    ) -> TcpClientProtocol:
        """
        Create a managed TCP client transport.

        Args:
            settings: TCP client configuration.
            event_handler: Async event handler for lifecycle, data, and error events.
            heartbeat_provider: Optional heartbeat provider used when client
                heartbeat scheduling is enabled.

        Returns:
            A transport implementing ``TcpClientProtocol``.

        Raises:
            HeartbeatConfigurationError: If heartbeat is enabled but the
                provider is missing or does not satisfy the async provider
                contract.
        """
        raise NotImplementedError

    def create_tcp_server(
        self,
        settings: TcpServerSettings,
        event_handler: NetworkEventHandlerProtocol,
        heartbeat_provider: HeartbeatProviderProtocol | None = None,
    ) -> TcpServerProtocol:
        """
        Create a managed TCP server transport.

        Args:
            settings: TCP server listener configuration.
            event_handler: Async event handler for lifecycle, data, and error events.
            heartbeat_provider: Optional heartbeat provider used when
                server-side heartbeat scheduling is enabled.

        Returns:
            A transport implementing ``TcpServerProtocol``.

        Raises:
            HeartbeatConfigurationError: If heartbeat is enabled but the
                provider is missing or does not satisfy the async provider
                contract.
        """
        raise NotImplementedError

    def create_multicast_receiver(
        self,
        settings: MulticastReceiverSettings,
        event_handler: NetworkEventHandlerProtocol,
    ) -> MulticastReceiverProtocol:
        """
        Create a managed UDP multicast receiver.

        Args:
            settings: Multicast receiver configuration.
            event_handler: Async event handler for lifecycle, data, and error events.

        Returns:
            A transport implementing ``MulticastReceiverProtocol``.
        """
        raise NotImplementedError

    def create_udp_receiver(
        self,
        settings: UdpReceiverSettings,
        event_handler: NetworkEventHandlerProtocol,
    ) -> UdpReceiverProtocol:
        """
        Create a managed UDP receiver.

        Args:
            settings: UDP receiver configuration.
            event_handler: Async event handler for lifecycle, data, and error events.

        Returns:
            A transport implementing ``UdpReceiverProtocol``.
        """
        raise NotImplementedError

    def create_udp_sender(self, settings: UdpSenderSettings) -> UdpSenderProtocol:
        """
        Create a UDP sender.

        Args:
            settings: UDP sender configuration.

        Returns:
            A transport implementing ``UdpSenderProtocol``.
        """
        raise NotImplementedError
