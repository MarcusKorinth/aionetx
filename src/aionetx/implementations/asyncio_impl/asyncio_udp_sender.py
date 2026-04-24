"""
Asyncio implementation of the lifecycle-light UDP sender role.

This module provides lazy socket creation, optional target overrides, and
best-effort nonblocking send behavior for UDP datagrams.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from types import TracebackType

from aionetx.api.bytes_like import BytesLike
from aionetx.api.udp import (
    UdpInvalidTargetError,
    UdpSenderProtocol,
    UdpSenderSettings,
    UdpSenderStoppedError,
)
from aionetx.api._validation import require_int_range, require_non_empty_str
from aionetx.implementations.asyncio_impl.identifier_utils import udp_sender_component_id
from aionetx.implementations.asyncio_impl.runtime_utils import (
    WarningRateLimiter,
    assert_running_on_owner_loop,
    configure_listener_bind_socket,
)

_send_fallback_warning_limiter = WarningRateLimiter(interval_seconds=300.0)


class AsyncioUdpSender(UdpSenderProtocol):
    """
    Stateless UDP datagram sender.

    Unlike receiver/server/client components this sender has no running/stopped
    lifecycle stream. It lazily owns a socket and only exposes send() and
    idempotent stop().
    """

    _WOULD_BLOCK_INITIAL_DELAY_SECONDS = 0.0005
    _WOULD_BLOCK_MAX_DELAY_SECONDS = 0.02

    def __init__(self, settings: UdpSenderSettings) -> None:
        settings.validate()
        self._settings = settings
        self._socket: socket.socket | None = None
        self._closed = False
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._connection_id = udp_sender_component_id(settings.local_host, settings.local_port)
        self._logger: logging.LoggerAdapter[logging.Logger] = logging.LoggerAdapter(
            logging.getLogger(__name__),
            {"component": "udp_sender", "connection_id": self._connection_id},
        )
        self._sendto_fallback_warning_emitted = False

    @property
    def is_socket_initialized(self) -> bool:
        """
        Whether the underlying UDP socket has been created.

        The socket is created lazily on the first ``send()`` call and released
        by ``stop()``.  This property allows tests and callers to verify
        socket-lifecycle invariants via the public contract rather than private
        attributes.
        """
        return self._socket is not None

    def __repr__(self) -> str:
        return (
            f"AsyncioUdpSender("
            f"default_host={self._settings.default_host!r}, "
            f"default_port={self._settings.default_port!r}, "
            f"closed={self._closed!r})"
        )

    def _assert_owner_loop(self) -> None:
        """
        Raise RuntimeError if called outside the owner event loop.

        Also pins ``_owner_loop`` on first call so all subsequent guard checks
        use the same loop identity drawn from ``runtime_utils.asyncio`` — not
        from a potentially test-patched module-level asyncio import.
        """
        self._owner_loop = assert_running_on_owner_loop(
            class_name=type(self).__name__, owner_loop=self._owner_loop
        )

    async def send(self, data: BytesLike, host: str | None = None, port: int | None = None) -> None:
        """
        Send one datagram to the configured default target or a per-call override.

        Args:
            data: Datagram payload to send.
            host: Optional per-call target host override.
            port: Optional per-call target port override.

        Raises:
            UdpSenderStoppedError: If the sender has already been stopped.
            UdpInvalidTargetError: If target host or port cannot be resolved.
            TypeError: If ``data`` is not bytes-like.
        """
        self._assert_owner_loop()
        if self._closed:
            raise UdpSenderStoppedError("UDP sender is stopped.")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("send expects bytes-like data.")

        target_host = host if host is not None else self._settings.default_host
        target_port = port if port is not None else self._settings.default_port
        target_host = require_non_empty_str(
            field_name="UDP target host",
            value=target_host,
            error_type=UdpInvalidTargetError,
        )
        if target_port is None:
            raise UdpInvalidTargetError("UDP target port must be between 1 and 65535.")
        require_int_range(
            field_name="UDP target port",
            value=target_port,
            min_value=1,
            max_value=65535,
            error_type=UdpInvalidTargetError,
        )

        sock = self._ensure_socket()
        await self._send_nonblocking(sock, bytes(data), (target_host, target_port))

    async def stop(self) -> None:
        """Close the sender socket and make future ``send()`` calls fail."""
        if self._closed:
            return
        self._assert_owner_loop()
        self._closed = True
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    async def __aenter__(self) -> AsyncioUdpSender:
        """Return ``self``; UDP sender is lazy and needs no explicit start."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the sender's socket unconditionally."""
        await self.stop()

    def _ensure_socket(self) -> socket.socket:
        """Create, configure, and cache the UDP socket on first use."""
        if self._socket is not None:
            return self._socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setblocking(False)
            configure_listener_bind_socket(sock, allow_address_reuse=False)
            if self._settings.enable_broadcast:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((self._settings.local_host, self._settings.local_port))
        except OSError:
            with contextlib.suppress(OSError):
                sock.close()
            raise
        self._refresh_connection_identity_from_socket(sock)
        self._socket = sock
        return sock

    def _refresh_connection_identity_from_socket(self, sock: socket.socket) -> None:
        """Refresh sender identity from the actual bound socket address when available."""
        try:
            sockname = sock.getsockname()
        except OSError:
            return

        if not (isinstance(sockname, tuple) and len(sockname) >= 2):
            return

        self._connection_id = udp_sender_component_id(str(sockname[0]), int(sockname[1]))
        self._logger.extra = {
            "component": "udp_sender",
            "connection_id": self._connection_id,
        }

    def _sendto_fallback_limiter_key(self, sock: socket.socket) -> str:
        """Build a limiter key from the actual bound socket identity when available."""
        try:
            sockname = sock.getsockname()
        except OSError:
            return f"{self._connection_id}:sendto-fallback"

        if isinstance(sockname, tuple) and len(sockname) >= 2:
            return f"{udp_sender_component_id(str(sockname[0]), int(sockname[1]))}:sendto-fallback"

        return f"{self._connection_id}:sendto-fallback"

    async def _send_nonblocking(
        self, sock: socket.socket, payload: bytes, target: tuple[str, int]
    ) -> None:
        """
        Send one datagram using loop-native send support or a polling fallback.

        Args:
            sock: Bound UDP socket used for the send.
            payload: Concrete payload bytes to transmit.
            target: Resolved target host and port.
        """
        loop = asyncio.get_running_loop()
        runtime_sock_sendto = getattr(loop, "sock_sendto", None)
        if callable(runtime_sock_sendto):
            await runtime_sock_sendto(sock, payload, target)
            return

        if not self._sendto_fallback_warning_emitted:
            limiter_key = self._sendto_fallback_limiter_key(sock)
            if _send_fallback_warning_limiter.should_log(limiter_key):
                self._logger.warning(
                    "AsyncioUdpSender is using sendto() fallback polling because "
                    "the event loop does not expose sock_sendto(). On Windows this "
                    "typically means the ProactorEventLoop is active; see "
                    "docs/platform_notes.md for latency implications and "
                    "mitigation via SelectorEventLoop.",
                )
            self._sendto_fallback_warning_emitted = True

        backoff_delay_seconds = self._WOULD_BLOCK_INITIAL_DELAY_SECONDS
        while True:
            try:
                sock.sendto(payload, target)
                return
            except (BlockingIOError, InterruptedError):
                await asyncio.sleep(backoff_delay_seconds)
                backoff_delay_seconds = min(
                    self._WOULD_BLOCK_MAX_DELAY_SECONDS,
                    backoff_delay_seconds * 2,
                )
