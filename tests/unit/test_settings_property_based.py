"""
Property-based tests for settings validation.

Uses Hypothesis to generate random inputs and verify that the invariants
described in TcpClientSettings.validate() hold for arbitrary values — not
just the hand-picked examples in test_settings.py.

These tests are OPT-IN.  They are skipped automatically when the
``hypothesis`` package is not installed.  To run them:

    pip install -e ".[dev-hypothesis]"
    pytest -m hypothesis

They are intentionally excluded from the required CI gate.
"""

from __future__ import annotations

import sys

import pytest

hypothesis = pytest.importorskip(
    "hypothesis", reason="hypothesis not installed; skipping property-based tests"
)

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from aionetx.api.errors import InvalidNetworkConfigurationError  # noqa: E402
from aionetx.api.tcp_client import TcpClientSettings  # noqa: E402
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings  # noqa: E402

pytestmark = pytest.mark.hypothesis


# ---------------------------------------------------------------------------
# Host validation
# ---------------------------------------------------------------------------


@given(host=st.text(max_size=200).filter(lambda s: len(s.strip()) == 0))
@settings(max_examples=50)
def test_empty_or_whitespace_host_always_invalid(host: str) -> None:
    """Any empty or whitespace-only host string must be rejected."""
    with pytest.raises(InvalidNetworkConfigurationError, match="host"):
        TcpClientSettings(host=host, port=1234).validate()


# ---------------------------------------------------------------------------
# Port validation
# ---------------------------------------------------------------------------


@given(port=st.integers(max_value=0))
@settings(max_examples=50)
def test_non_positive_port_always_invalid(port: int) -> None:
    """Ports ≤ 0 are always invalid (unbounded below for full coverage)."""
    with pytest.raises(InvalidNetworkConfigurationError, match="port"):
        TcpClientSettings(host="127.0.0.1", port=port).validate()


@given(port=st.integers(min_value=65_536))
@settings(max_examples=50)
def test_port_above_65535_always_invalid(port: int) -> None:
    """Ports > 65535 are always invalid (unbounded above for full coverage)."""
    with pytest.raises(InvalidNetworkConfigurationError, match="port"):
        TcpClientSettings(host="127.0.0.1", port=port).validate()


@given(port=st.integers(min_value=1, max_value=65_535))
@settings(max_examples=50)
def test_valid_port_range_always_accepted(port: int) -> None:
    """Ports in [1, 65535] with a valid host must not raise."""
    TcpClientSettings(host="127.0.0.1", port=port).validate()


# ---------------------------------------------------------------------------
# receive_buffer_size validation
# ---------------------------------------------------------------------------


@given(size=st.integers(min_value=-1_000, max_value=0))
@settings(max_examples=50)
def test_non_positive_receive_buffer_size_always_invalid(size: int) -> None:
    """receive_buffer_size ≤ 0 must always be rejected."""
    with pytest.raises(InvalidNetworkConfigurationError, match="receive_buffer_size"):
        TcpClientSettings(host="127.0.0.1", port=1234, receive_buffer_size=size).validate()


# ---------------------------------------------------------------------------
# connect_timeout_seconds validation
# ---------------------------------------------------------------------------


@given(timeout=st.floats(max_value=0.0, allow_nan=False, allow_infinity=False))
@settings(max_examples=50)
def test_non_positive_connect_timeout_always_invalid(timeout: float) -> None:
    """connect_timeout_seconds ≤ 0 must always be rejected."""
    with pytest.raises(InvalidNetworkConfigurationError, match="connect_timeout_seconds"):
        TcpClientSettings(host="127.0.0.1", port=1234, connect_timeout_seconds=timeout).validate()


@given(
    timeout=st.floats(
        min_value=sys.float_info.min, max_value=3600.0, allow_nan=False, allow_infinity=False
    )
)
@settings(max_examples=50)
def test_positive_connect_timeout_always_accepted(timeout: float) -> None:
    """Any positive connect_timeout_seconds must be accepted (down to the smallest float > 0)."""
    TcpClientSettings(host="127.0.0.1", port=1234, connect_timeout_seconds=timeout).validate()


# ---------------------------------------------------------------------------
# Reconnect initial_delay_seconds validation
# ---------------------------------------------------------------------------


@given(delay=st.floats(max_value=0.0, allow_nan=False, allow_infinity=False))
@settings(max_examples=50)
def test_non_positive_reconnect_initial_delay_always_invalid(delay: float) -> None:
    """TcpReconnectSettings with initial_delay_seconds ≤ 0 must be rejected."""
    with pytest.raises(InvalidNetworkConfigurationError):
        TcpReconnectSettings(
            enabled=True,
            initial_delay_seconds=delay,
        ).validate()
