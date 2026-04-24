"""
Dedicated unit test for the UDP sendto() fallback-warning limiter.

Mirrors ``test_udp_fallback_warning.py`` (receiver side). The sender uses the
same two-layer suppression mechanism:

  1. Module-level ``_send_fallback_warning_limiter`` (``WarningRateLimiter``),
     keyed by connection_id.
  2. Per-instance flag ``_sendto_fallback_warning_emitted``: permanently
     suppresses subsequent warnings from the *same sender* after the first.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from aionetx.api.udp import UdpSenderSettings
from aionetx.implementations.asyncio_impl import asyncio_udp_sender as sender_module
from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender


class _LoopWithoutSockSendTo:
    """Minimal event loop stub that does NOT expose sock_sendto."""


class _AlwaysSuccessSocket:
    """Socket stub whose sendto() always succeeds immediately."""

    def sendto(self, _payload: bytes, _target: tuple[str, int]) -> int:
        return len(_payload)

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 22000)


def _make_sender(port_offset: int) -> AsyncioUdpSender:
    return AsyncioUdpSender(
        settings=UdpSenderSettings(
            default_host="127.0.0.1",
            default_port=21000 + port_offset,
            local_host="127.0.0.1",
            local_port=22000 + port_offset,
        )
    )


def _reset_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sender_module,
        "_send_fallback_warning_limiter",
        sender_module.WarningRateLimiter(interval_seconds=300.0),
    )


@pytest.mark.asyncio
async def test_sender_fallback_warning_emitted_exactly_once_on_first_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logger.warning is called exactly once when sock_sendto is absent."""
    _reset_limiter(monkeypatch)

    sender = _make_sender(1)
    # Pin the owner loop so _assert_owner_loop does not fire. We only exercise
    # _send_nonblocking directly here.
    sock_stub = _AlwaysSuccessSocket()
    loop_stub = _LoopWithoutSockSendTo()

    with patch.object(sender_module.asyncio, "get_running_loop", return_value=loop_stub):
        with patch.object(sender._logger, "warning") as mock_warning:
            await sender._send_nonblocking(sock_stub, b"x", ("127.0.0.1", 9999))  # type: ignore[arg-type]

            assert mock_warning.call_count == 1
            call_args = str(mock_warning.call_args)
            assert "sendto() fallback polling" in call_args
            assert "sock_sendto" in call_args


@pytest.mark.asyncio
async def test_sender_fallback_warning_suppressed_on_second_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logger.warning is NOT called again on a second fallback from the same sender."""
    _reset_limiter(monkeypatch)

    sender = _make_sender(2)
    sock_stub = _AlwaysSuccessSocket()
    loop_stub = _LoopWithoutSockSendTo()

    with patch.object(sender_module.asyncio, "get_running_loop", return_value=loop_stub):
        with patch.object(sender._logger, "warning") as mock_warning:
            await sender._send_nonblocking(sock_stub, b"x", ("127.0.0.1", 9999))  # type: ignore[arg-type]
            assert mock_warning.call_count == 1

            await sender._send_nonblocking(sock_stub, b"y", ("127.0.0.1", 9999))  # type: ignore[arg-type]
            assert mock_warning.call_count == 1, (
                f"Second fallback must not emit another warning; got {mock_warning.call_count}"
            )


@pytest.mark.asyncio
async def test_sender_fallback_flag_is_set_after_first_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_limiter(monkeypatch)

    sender = _make_sender(3)
    sock_stub = _AlwaysSuccessSocket()
    loop_stub = _LoopWithoutSockSendTo()

    assert not sender._sendto_fallback_warning_emitted

    with patch.object(sender_module.asyncio, "get_running_loop", return_value=loop_stub):
        await sender._send_nonblocking(sock_stub, b"x", ("127.0.0.1", 9999))  # type: ignore[arg-type]

    assert sender._sendto_fallback_warning_emitted


@pytest.mark.asyncio
async def test_sender_fallback_warning_not_emitted_when_sock_sendto_present() -> None:
    class _LoopWithSockSendTo:
        async def sock_sendto(
            self, _sock: object, _payload: bytes, _target: tuple[str, int]
        ) -> None:
            return None

    sender = _make_sender(4)
    sock_stub = _AlwaysSuccessSocket()
    loop_stub = _LoopWithSockSendTo()

    with patch.object(sender_module.asyncio, "get_running_loop", return_value=loop_stub):
        with patch.object(sender._logger, "warning") as mock_warning:
            await sender._send_nonblocking(sock_stub, b"x", ("127.0.0.1", 9999))  # type: ignore[arg-type]
            assert mock_warning.call_count == 0

    assert not sender._sendto_fallback_warning_emitted


@pytest.mark.asyncio
async def test_sender_fallback_warning_uses_actual_bound_socket_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_limiter(monkeypatch)

    class _EphemeralSocket:
        def __init__(self, bound_port: int) -> None:
            self._bound_port = bound_port

        def sendto(self, _payload: bytes, _target: tuple[str, int]) -> int:
            return len(_payload)

        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", self._bound_port)

    loop_stub = _LoopWithoutSockSendTo()
    sender_a = AsyncioUdpSender(
        settings=UdpSenderSettings(
            default_host="127.0.0.1",
            default_port=21010,
            local_host="0.0.0.0",
            local_port=0,
        )
    )
    sender_b = AsyncioUdpSender(
        settings=UdpSenderSettings(
            default_host="127.0.0.1",
            default_port=21011,
            local_host="0.0.0.0",
            local_port=0,
        )
    )

    with patch.object(sender_module.asyncio, "get_running_loop", return_value=loop_stub):
        with patch.object(sender_a._logger, "warning") as warning_a:
            await sender_a._send_nonblocking(_EphemeralSocket(30101), b"x", ("127.0.0.1", 9999))  # type: ignore[arg-type]
            assert warning_a.call_count == 1

        with patch.object(sender_b._logger, "warning") as warning_b:
            await sender_b._send_nonblocking(_EphemeralSocket(30102), b"y", ("127.0.0.1", 9999))  # type: ignore[arg-type]
            assert warning_b.call_count == 1
