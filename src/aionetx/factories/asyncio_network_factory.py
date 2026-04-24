"""
Asyncio-backed factory implementation for all supported transport roles.

This module contains the concrete ``NetworkFactory`` implementation that
constructs TCP, UDP, and multicast transports for the asyncio backend.
"""

from __future__ import annotations

from aionetx.api.heartbeat_provider_protocol import HeartbeatProviderProtocol
from aionetx.api.multicast_receiver_protocol import MulticastReceiverProtocol
from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
from aionetx.api.network_factory import NetworkFactory
from aionetx.api.network_event_handler_protocol import NetworkEventHandlerProtocol
from aionetx.api.tcp_client import TcpClientProtocol, TcpClientSettings
from aionetx.api.tcp_server import TcpServerProtocol, TcpServerSettings
from aionetx.api.udp import (
    UdpReceiverProtocol,
    UdpReceiverSettings,
    UdpSenderProtocol,
    UdpSenderSettings,
)
from aionetx.implementations.asyncio_impl.asyncio_multicast_receiver import AsyncioMulticastReceiver
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver
from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender
from aionetx.implementations.asyncio_impl.runtime_utils import validate_heartbeat_provider


class AsyncioNetworkFactory(NetworkFactory):
    """
    Factory that creates asyncio-based network layer components.

    Construction symmetry:
    - Managed transports (TCP client/server + datagram receivers) accept an
      event handler because they emit runtime events.
    - UDP sender is intentionally lifecycle-light and stateless from an event
      stream perspective; it therefore requires only sender settings.
    - The concrete asyncio implementation satisfies the explicit
      ``aionetx.api.NetworkFactory`` contract.
    """

    def create_tcp_client(
        self,
        settings: TcpClientSettings,
        event_handler: NetworkEventHandlerProtocol,
        heartbeat_provider: HeartbeatProviderProtocol | None = None,
    ) -> TcpClientProtocol:
        """
        Create an asyncio TCP client transport.

        Args:
            settings: TCP client configuration.
            event_handler: Async event handler for lifecycle, data, and error events.
            heartbeat_provider: Optional heartbeat provider used when client
                heartbeat scheduling is enabled.

        Returns:
            A transport implementing ``TcpClientProtocol``.
        """
        validate_heartbeat_provider(
            heartbeat_settings=settings.heartbeat,
            heartbeat_provider=heartbeat_provider,
        )
        return AsyncioTcpClient(
            settings=settings,
            event_handler=event_handler,
            heartbeat_provider=heartbeat_provider,
        )

    def create_tcp_server(
        self,
        settings: TcpServerSettings,
        event_handler: NetworkEventHandlerProtocol,
        heartbeat_provider: HeartbeatProviderProtocol | None = None,
    ) -> TcpServerProtocol:
        """
        Create an asyncio TCP server transport.

        Args:
            settings: TCP server configuration.
            event_handler: Async event handler for lifecycle, data, and error events.
            heartbeat_provider: Optional heartbeat provider used when server
                heartbeat scheduling is enabled.

        Returns:
            A transport implementing ``TcpServerProtocol``.
        """
        validate_heartbeat_provider(
            heartbeat_settings=settings.heartbeat,
            heartbeat_provider=heartbeat_provider,
        )
        return AsyncioTcpServer(
            settings=settings,
            event_handler=event_handler,
            heartbeat_provider=heartbeat_provider,
        )

    def create_multicast_receiver(
        self,
        settings: MulticastReceiverSettings,
        event_handler: NetworkEventHandlerProtocol,
    ) -> MulticastReceiverProtocol:
        """
        Create an asyncio multicast receiver.

        Args:
            settings: Multicast receiver configuration.
            event_handler: Async event handler for lifecycle, data, and error events.

        Returns:
            A transport implementing ``MulticastReceiverProtocol``.
        """
        return AsyncioMulticastReceiver(settings=settings, event_handler=event_handler)

    def create_udp_receiver(
        self,
        settings: UdpReceiverSettings,
        event_handler: NetworkEventHandlerProtocol,
    ) -> UdpReceiverProtocol:
        """
        Create an asyncio UDP receiver.

        Args:
            settings: UDP receiver configuration.
            event_handler: Async event handler for lifecycle, data, and error events.

        Returns:
            A transport implementing ``UdpReceiverProtocol``.
        """
        return AsyncioUdpReceiver(settings=settings, event_handler=event_handler)

    def create_udp_sender(self, settings: UdpSenderSettings) -> UdpSenderProtocol:
        """
        Create a UDP sender.

        UDP sender intentionally does not implement managed lifecycle/event
        contracts: it is a lightweight bytes sender with lazy socket creation.
        """
        return AsyncioUdpSender(settings=settings)
