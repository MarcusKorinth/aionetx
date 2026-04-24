"""
aionetx — asyncio-first transport library for TCP and UDP networking.

This package exposes a curated, stable public API via :data:`PUBLIC_API`.
The recommended entry point for application code is
:class:`AsyncioNetworkFactory`, which produces managed TCP/UDP transports
driven by dataclass-based settings (``TcpClientSettings``, ``TcpServerSettings``,
``UdpReceiverSettings``, ``UdpSenderSettings``, ``MulticastReceiverSettings``).

Advanced event-handler composition helpers live alongside the factory
(``BaseNetworkEventHandler``, ``NetworkEvent``, ``BytesReceivedEvent``).
Transport-protocol interfaces, concrete event subclasses, and additional
errors are available from :mod:`aionetx.api` for users who need the full
curated surface. Anything under :mod:`aionetx.implementations` is internal.
"""

from importlib.metadata import version as _metadata_version

try:
    __version__: str = _metadata_version("aionetx")
except Exception:  # pragma: no cover — package not installed in editable mode rarely
    __version__ = "0.0.0.dev0"

from aionetx.api import (
    BaseNetworkEventHandler,
    BytesReceivedEvent,
    ComponentLifecycleState,
    EventDeliverySettings,
    HeartbeatConfigurationError,
    HeartbeatProviderProtocol,
    MulticastReceiverSettings,
    NetworkEvent,
    TcpHeartbeatSettings,
    TcpReconnectSettings,
    TcpClientSettings,
    TcpServerSettings,
    UdpReceiverSettings,
    UdpSenderSettings,
)
from aionetx.factories import AsyncioNetworkFactory

# Public API governance:
# - `__all__` is the canonical curated export list for this module.
# - `PUBLIC_API` is derived from `__all__` for downstream introspection/testing.
# - `aionetx.implementations.*` remains internal/advanced and may change.
__all__ = (
    "__version__",
    "AsyncioNetworkFactory",
    "BaseNetworkEventHandler",
    "BytesReceivedEvent",
    "ComponentLifecycleState",
    "EventDeliverySettings",
    "HeartbeatConfigurationError",
    "HeartbeatProviderProtocol",
    "TcpHeartbeatSettings",
    "MulticastReceiverSettings",
    "NetworkEvent",
    "TcpReconnectSettings",
    "TcpClientSettings",
    "TcpServerSettings",
    "UdpReceiverSettings",
    "UdpSenderSettings",
)

PUBLIC_API = tuple(__all__)
