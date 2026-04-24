"""
Asyncio implementation of the managed UDP receiver role.

This module adapts the shared datagram receiver base to plain UDP socket setup
and UDP-specific receive metadata.
"""

from __future__ import annotations

import asyncio  # noqa: F401 - test seam: monkeypatched in unit tests
import contextlib
import logging
import socket
from types import TracebackType

from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.network_event_handler_protocol import NetworkEventHandlerProtocol
from aionetx.api.udp import UdpReceiverProtocol, UdpReceiverSettings
from aionetx.implementations.asyncio_impl._asyncio_datagram_receiver_base import (
    _AsyncioDatagramReceiverBase,
)
from aionetx.implementations.asyncio_impl.identifier_utils import udp_receiver_component_id
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher
from aionetx.implementations.asyncio_impl.lifecycle_internal import LifecycleRole
from aionetx.implementations.asyncio_impl.runtime_utils import configure_listener_bind_socket


class AsyncioUdpReceiver(_AsyncioDatagramReceiverBase, UdpReceiverProtocol):
    """Managed asyncio UDP receiver built on the shared datagram base."""

    def __init__(
        self, settings: UdpReceiverSettings, event_handler: NetworkEventHandlerProtocol
    ) -> None:
        settings.validate()
        self._settings = settings
        connection_id = udp_receiver_component_id(settings.host, settings.port)
        logger = logging.LoggerAdapter(
            logging.getLogger(__name__),
            {"component": "udp_receiver", "connection_id": connection_id},
        )
        event_dispatcher = AsyncioEventDispatcher(
            event_handler=event_handler,
            delivery=settings.event_delivery,
            logger=logger,
            error_source=connection_id,
            stop_component_callback=self.stop,
        )
        super().__init__(
            receiver_name="UDP",
            connection_id=connection_id,
            logger=logger,
            event_dispatcher=event_dispatcher,
            lifecycle_role=LifecycleRole.UDP_RECEIVER,
        )

    @property
    def lifecycle_state(self) -> ComponentLifecycleState:
        """Return the current managed lifecycle state."""
        return self._lifecycle_state

    def __repr__(self) -> str:
        return (
            f"AsyncioUdpReceiver("
            f"host={self._settings.host!r}, "
            f"port={self._settings.port!r}, "
            f"state={self._lifecycle_state.value!r})"
        )

    async def start(self) -> None:
        """
        Bind the UDP socket, publish lifecycle transitions, and start receiving.

        Raises:
            OSError: If socket setup or bind fails.
        """
        self._assert_owner_loop()
        if not await self._begin_startup():
            return
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            configure_listener_bind_socket(sock, allow_address_reuse=False)
            if self._settings.enable_broadcast:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((self._settings.host, self._settings.port))
            sock.setblocking(False)
            local_host, local_port = sock.getsockname()[:2]
            metadata = ConnectionMetadata(
                connection_id=self._connection_id,
                role=ConnectionRole.UDP_RECEIVER,
                local_host=str(local_host),
                local_port=int(local_port),
            )
        except Exception:
            if sock is not None:
                with contextlib.suppress(Exception):
                    sock.close()
            await self._fail_startup()
            raise
        await self._complete_startup(
            sock=sock,
            metadata=metadata,
            receive_buffer_size=self._settings.receive_buffer_size,
        )

    async def stop(self) -> None:
        """Stop UDP reception and release owned socket resources."""
        self._assert_owner_loop()
        await super().stop()

    async def __aenter__(self) -> AsyncioUdpReceiver:
        """Start the receiver and return ``self``."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Stop the receiver unconditionally."""
        await self.stop()
