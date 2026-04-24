"""
Asyncio implementation of the managed multicast receiver role.

This module adapts the shared datagram receiver base to multicast socket setup,
group membership management, and multicast-specific cleanup behavior.
"""

from __future__ import annotations

import asyncio  # noqa: F401 - test seam: monkeypatched in unit tests
import contextlib
import logging
import socket
import struct
from types import TracebackType

from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_metadata import ConnectionMetadata
from aionetx.api.connection_lifecycle import ConnectionRole
from aionetx.api.multicast_receiver_protocol import MulticastReceiverProtocol
from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
from aionetx.api.network_event_handler_protocol import NetworkEventHandlerProtocol
from aionetx.implementations.asyncio_impl._asyncio_datagram_receiver_base import (
    _AsyncioDatagramReceiverBase,
)
from aionetx.implementations.asyncio_impl.identifier_utils import multicast_receiver_component_id
from aionetx.implementations.asyncio_impl.event_dispatcher import AsyncioEventDispatcher
from aionetx.implementations.asyncio_impl.lifecycle_internal import LifecycleRole
from aionetx.implementations.asyncio_impl.runtime_utils import configure_listener_bind_socket


class AsyncioMulticastReceiver(_AsyncioDatagramReceiverBase, MulticastReceiverProtocol):
    """Managed asyncio multicast receiver built on the shared datagram base."""

    def __init__(
        self, settings: MulticastReceiverSettings, event_handler: NetworkEventHandlerProtocol
    ) -> None:
        settings.validate()
        self._settings = settings
        connection_id = multicast_receiver_component_id(settings.group_ip, settings.port)
        logger = logging.LoggerAdapter(
            logging.getLogger(__name__),
            {"component": "multicast_receiver", "connection_id": connection_id},
        )
        event_dispatcher = AsyncioEventDispatcher(
            event_handler=event_handler,
            delivery=settings.event_delivery,
            logger=logger,
            error_source=connection_id,
            stop_component_callback=self.stop,
        )
        super().__init__(
            receiver_name="Multicast",
            connection_id=connection_id,
            logger=logger,
            event_dispatcher=event_dispatcher,
            lifecycle_role=LifecycleRole.MULTICAST_RECEIVER,
        )

    @property
    def lifecycle_state(self) -> ComponentLifecycleState:
        """Return the current managed lifecycle state."""
        return self._lifecycle_state

    async def start(self) -> None:
        """
        Join the multicast group, publish lifecycle transitions, and start receiving.

        Raises:
            OSError: If socket setup, bind, or group membership setup fails.
        """
        self._assert_owner_loop()
        # Follow the shared datagram lifecycle startup pattern documented on the
        # base class for consistency with UDP receiver startup semantics.
        if not await self._begin_startup():
            return
        self._logger.debug("Starting multicast receiver.")
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            configure_listener_bind_socket(sock, allow_address_reuse=True)
            # SO_REUSEPORT is not available on Windows; getattr avoids the
            # mypy attr-defined error while OSError suppression handles kernels
            # that define the constant but do not support the option (e.g. some
            # container environments).
            _so_reuseport: int | None = getattr(socket, "SO_REUSEPORT", None)
            if _so_reuseport is not None:
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, _so_reuseport, 1)
            sock.bind((self._settings.bind_ip, self._settings.port))
            membership = struct.pack(
                "4s4s",
                socket.inet_aton(self._settings.group_ip),
                socket.inet_aton(self._settings.interface_ip),
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.setblocking(False)
            metadata = ConnectionMetadata(
                connection_id=self._connection_id,
                role=ConnectionRole.MULTICAST_RECEIVER,
                local_host=self._settings.bind_ip,
                local_port=self._settings.port,
                remote_host=self._settings.group_ip,
                remote_port=self._settings.port,
            )
        except Exception:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    self._logger.warning(
                        "Multicast receiver startup cleanup could not close socket cleanly.",
                        exc_info=True,
                    )
            await self._fail_startup()
            raise

        await self._complete_startup(
            sock=sock,
            metadata=metadata,
            receive_buffer_size=self._settings.receive_buffer_size,
        )
        self._logger.debug("Multicast receiver joined group and is running.")

    async def stop(self) -> None:
        """Leave the multicast group and stop the receiver lifecycle."""
        self._assert_owner_loop()
        self._logger.debug("Stopping multicast receiver.")
        await super().stop()
        self._logger.debug("Multicast receiver stopped.")

    async def __aenter__(self) -> AsyncioMulticastReceiver:
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

    def _cleanup_socket(self, sock: socket.socket) -> None:
        """Attempt to drop multicast membership before the socket is closed."""
        try:
            membership = struct.pack(
                "4s4s",
                socket.inet_aton(self._settings.group_ip),
                socket.inet_aton(self._settings.interface_ip),
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, membership)
        except OSError:
            self._logger.warning(
                "Multicast receiver cleanup suppressed IP_DROP_MEMBERSHIP failure.",
                exc_info=True,
            )
