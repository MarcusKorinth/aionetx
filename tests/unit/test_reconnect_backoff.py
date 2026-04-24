from unittest.mock import patch

from aionetx.api.reconnect_jitter import ReconnectJitter
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from tests.internal_asyncio_impl_refs import ReconnectBackoff


def test_reconnect_backoff_progression_and_reset() -> None:
    backoff = ReconnectBackoff(
        TcpReconnectSettings(
            enabled=True,
            initial_delay_seconds=1.0,
            max_delay_seconds=5.0,
            backoff_factor=2.0,
        )
    )
    assert backoff.next_delay() == 1.0
    assert backoff.next_delay() == 2.0
    assert backoff.next_delay() == 4.0
    assert backoff.next_delay() == 5.0
    assert backoff.next_delay() == 5.0

    backoff.reset()
    assert backoff.next_delay() == 1.0


def test_reconnect_backoff_full_jitter() -> None:
    backoff = ReconnectBackoff(
        TcpReconnectSettings(
            enabled=True,
            initial_delay_seconds=4.0,
            max_delay_seconds=10.0,
            backoff_factor=2.0,
            jitter=ReconnectJitter.FULL,
        )
    )
    with patch("random.uniform", return_value=1.25) as mocked_uniform:
        assert backoff.next_delay() == 1.25
        mocked_uniform.assert_called_once_with(0.0, 4.0)


def test_reconnect_backoff_equal_jitter() -> None:
    backoff = ReconnectBackoff(
        TcpReconnectSettings(
            enabled=True,
            initial_delay_seconds=8.0,
            max_delay_seconds=10.0,
            backoff_factor=2.0,
            jitter=ReconnectJitter.EQUAL,
        )
    )
    with patch("random.uniform", return_value=1.5) as mocked_uniform:
        assert backoff.next_delay() == 5.5
        mocked_uniform.assert_called_once_with(0.0, 4.0)
