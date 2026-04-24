"""
Dedicated unit test for the UDP recvfrom() fallback-warning limiter.

The _AsyncioDatagramReceiverBase class has a two-layer warning-suppression
mechanism:

  1. Module-level WarningRateLimiter (_recv_fallback_warning_limiter): rate-
     limits by a key derived from connection_id, with a 300-second interval.
  2. Per-instance flag (recv_fallback_warning_emitted on _DatagramRuntimeState):
     permanently suppresses subsequent warnings from the *same receiver* once
     the first warning has been emitted.

These tests verify that logger.warning is called exactly once (first fallback)
and NOT called on subsequent fallbacks from the same receiver instance.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aionetx.api.udp import UdpReceiverSettings
from aionetx.implementations.asyncio_impl import _asyncio_datagram_receiver_base as base_module
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver


class _NoopHandler:
    async def on_event(self, event) -> None:
        return None


class _LoopWithoutSockRecvFrom:
    """Minimal event loop stub that does NOT expose sock_recvfrom."""

    pass


class _AlwaysSuccessSocket:
    """Socket stub whose recvfrom() always succeeds immediately."""

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        return (b"data", ("127.0.0.1", 9999))


@pytest.mark.asyncio
async def test_fallback_warning_emitted_exactly_once_on_first_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logger.warning is called exactly once when sock_recvfrom is absent."""
    monkeypatch.setattr(base_module.asyncio, "sleep", AsyncMock())

    # Reset the module-level rate limiter so prior test runs don't suppress
    # the warning for this connection_id key.
    monkeypatch.setattr(
        base_module,
        "_recv_fallback_warning_limiter",
        base_module.WarningRateLimiter(interval_seconds=300.0),
    )

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=21900),
        event_handler=_NoopHandler(),
    )

    loop_stub = _LoopWithoutSockRecvFrom()
    sock_stub = _AlwaysSuccessSocket()

    with patch.object(receiver._logger, "warning") as mock_warning:
        # First fallback invocation — should trigger the warning.
        await receiver._recv_nonblocking(loop_stub, sock_stub, 1024)  # type: ignore[arg-type]

        assert mock_warning.call_count == 1, (
            "Expected exactly one logger.warning call on first fallback, "
            f"got {mock_warning.call_count}"
        )
        call_args = mock_warning.call_args
        assert "recvfrom() fallback polling" in str(call_args), (
            f"Warning message does not mention recvfrom() fallback: {call_args}"
        )
        assert "sock_recvfrom" in str(call_args), (
            f"Warning message does not mention sock_recvfrom: {call_args}"
        )


@pytest.mark.asyncio
async def test_fallback_warning_suppressed_on_second_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logger.warning is NOT called again on a second fallback from the same receiver."""
    monkeypatch.setattr(base_module.asyncio, "sleep", AsyncMock())

    # Reset the module-level rate limiter so prior test runs don't suppress
    # the warning for this connection_id key.
    monkeypatch.setattr(
        base_module,
        "_recv_fallback_warning_limiter",
        base_module.WarningRateLimiter(interval_seconds=300.0),
    )

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=21901),
        event_handler=_NoopHandler(),
    )

    loop_stub = _LoopWithoutSockRecvFrom()
    sock_stub = _AlwaysSuccessSocket()

    with patch.object(receiver._logger, "warning") as mock_warning:
        # First fallback — warning should be emitted.
        await receiver._recv_nonblocking(loop_stub, sock_stub, 1024)  # type: ignore[arg-type]
        assert mock_warning.call_count == 1, "First fallback must emit one warning"

        # Second fallback from the same receiver instance — the per-instance
        # flag recv_fallback_warning_emitted is now True, so the warning must
        # NOT be repeated regardless of the rate limiter state.
        await receiver._recv_nonblocking(loop_stub, sock_stub, 1024)  # type: ignore[arg-type]
        assert mock_warning.call_count == 1, (
            "Second fallback must NOT emit another warning; "
            f"logger.warning was called {mock_warning.call_count} times total"
        )


@pytest.mark.asyncio
async def test_fallback_limiter_flag_is_set_after_first_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recv_fallback_warning_emitted is True after the first fallback call."""
    monkeypatch.setattr(base_module.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        base_module,
        "_recv_fallback_warning_limiter",
        base_module.WarningRateLimiter(interval_seconds=300.0),
    )

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=21902),
        event_handler=_NoopHandler(),
    )

    loop_stub = _LoopWithoutSockRecvFrom()
    sock_stub = _AlwaysSuccessSocket()

    assert not receiver._runtime.recv_fallback_warning_emitted  # type: ignore[attr-defined]

    await receiver._recv_nonblocking(loop_stub, sock_stub, 1024)  # type: ignore[arg-type]

    assert receiver._runtime.recv_fallback_warning_emitted, (  # type: ignore[attr-defined]
        "recv_fallback_warning_emitted must be True after the first fallback"
    )


@pytest.mark.asyncio
async def test_fallback_warning_not_emitted_when_sock_recvfrom_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No warning is emitted when the loop supports sock_recvfrom()."""

    class _LoopWithSockRecvFrom:
        async def sock_recvfrom(self, sock: object, size: int) -> tuple[bytes, tuple[str, int]]:
            return (b"data", ("127.0.0.1", 9999))

    receiver = AsyncioUdpReceiver(
        settings=UdpReceiverSettings(host="127.0.0.1", port=21903),
        event_handler=_NoopHandler(),
    )

    loop_stub = _LoopWithSockRecvFrom()
    sock_stub = _AlwaysSuccessSocket()

    with patch.object(receiver._logger, "warning") as mock_warning:
        await receiver._recv_nonblocking(loop_stub, sock_stub, 1024)  # type: ignore[arg-type]

        assert mock_warning.call_count == 0, (
            "No warning should be emitted when sock_recvfrom() is present; "
            f"logger.warning was called {mock_warning.call_count} times"
        )
    assert not receiver._runtime.recv_fallback_warning_emitted  # type: ignore[attr-defined]
