"""
Public exception hierarchy for configuration and runtime failures.

User code can import these exceptions to validate configuration failures and to
catch transport operations that fail after construction succeeds.
"""

from __future__ import annotations


class NetworkLayerError(Exception):
    """Base exception for the network layer."""


class NetworkConfigurationError(NetworkLayerError):
    """Base exception for invalid public configuration or validation failures."""


class NetworkRuntimeError(NetworkLayerError):
    """Base exception for runtime transport failures after configuration succeeds."""


class InvalidNetworkConfigurationError(NetworkConfigurationError):
    """Raised when invalid network settings are supplied."""


class HeartbeatConfigurationError(NetworkConfigurationError):
    """Raised when heartbeat is enabled but configured incorrectly."""


class ConnectionClosedError(NetworkRuntimeError):
    """Raised when an operation requires an open connection."""
