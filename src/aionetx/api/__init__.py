"""
Curated advanced API surface for ``aionetx``.

This package groups public event types, lifecycle models, protocols, settings,
and selected exceptions for users who want more control than the package-root
imports provide.
"""

from aionetx.api.events import (
    BaseNetworkEventHandler,
    BytesReceivedEvent,
    ComponentLifecycleChangedEvent,
    ConnectionClosedEvent,
    ConnectionOpenedEvent,
    ConnectionRejectedEvent,
    HandlerFailurePolicyStopEvent,
    NetworkErrorEvent,
    NetworkEvent,
    ReconnectAttemptFailedEvent,
    ReconnectAttemptStartedEvent,
    ReconnectScheduledEvent,
)
from aionetx.api.lifecycle import (
    ComponentLifecycleState,
    ConnectionMetadata,
    ConnectionRole,
    ConnectionState,
)
from aionetx.api.errors import (
    ConnectionClosedError,
    HeartbeatConfigurationError,
    InvalidNetworkConfigurationError,
    NetworkConfigurationError,
    NetworkLayerError,
    NetworkRuntimeError,
)
from aionetx.api.heartbeat import HeartbeatRequest, HeartbeatResult, TcpHeartbeatSettings
from aionetx.api.network_event_handler_protocol import NetworkEventHandlerProtocol
from aionetx.api.policies import (
    ErrorPolicy,
    EventBackpressurePolicy,
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
    ReconnectJitter,
)
from aionetx.api.protocols import (
    ByteSenderProtocol,
    BytesLike,
    ConnectionProtocol,
    HeartbeatProviderProtocol,
    ManagedTransportProtocol,
    MulticastReceiverProtocol,
    NetworkFactory,
    TcpClientProtocol,
    TcpServerProtocol,
    UdpReceiverProtocol,
    UdpSenderProtocol,
)
from aionetx.api.settings import (
    MulticastReceiverSettings,
    TcpClientSettings,
    TcpReconnectSettings,
    TcpServerSettings,
    UdpReceiverSettings,
    UdpSenderSettings,
)
from aionetx.api.udp import (
    UdpInvalidTargetError,
    UdpSenderStoppedError,
)
from aionetx.api.typed_event_router import TypedEventRouter

# Public API governance:
# - ``__all__`` is the canonical curated export list for this module.
# - ``PUBLIC_API`` mirrors that list for tests and introspection helpers.
# - Underscore-prefixed modules remain internal unless explicitly promoted here.
# - Any type reachable from a public signature should be importable from this
#   curated API surface so callers do not need to reach into internal modules.
__all__ = (
    "BaseNetworkEventHandler",
    "ByteSenderProtocol",
    "BytesLike",
    "BytesReceivedEvent",
    "ComponentLifecycleChangedEvent",
    "ComponentLifecycleState",
    "ConnectionClosedError",
    "ConnectionClosedEvent",
    "ConnectionMetadata",
    "ConnectionOpenedEvent",
    "ConnectionRejectedEvent",
    "ConnectionProtocol",
    "ConnectionRole",
    "ConnectionState",
    "ErrorPolicy",
    "EventBackpressurePolicy",
    "EventDeliverySettings",
    "EventDispatchMode",
    "EventHandlerFailurePolicy",
    "HandlerFailurePolicyStopEvent",
    "HeartbeatConfigurationError",
    "HeartbeatProviderProtocol",
    "HeartbeatRequest",
    "HeartbeatResult",
    "TcpHeartbeatSettings",
    "InvalidNetworkConfigurationError",
    "ManagedTransportProtocol",
    "MulticastReceiverProtocol",
    "MulticastReceiverSettings",
    "NetworkConfigurationError",
    "NetworkFactory",
    "NetworkErrorEvent",
    "NetworkEvent",
    "NetworkEventHandlerProtocol",
    "NetworkLayerError",
    "NetworkRuntimeError",
    "ReconnectAttemptFailedEvent",
    "ReconnectAttemptStartedEvent",
    "ReconnectJitter",
    "ReconnectScheduledEvent",
    "TcpReconnectSettings",
    "TcpClientProtocol",
    "TcpClientSettings",
    "TcpServerProtocol",
    "TcpServerSettings",
    "TypedEventRouter",
    "UdpInvalidTargetError",
    "UdpReceiverProtocol",
    "UdpReceiverSettings",
    "UdpSenderProtocol",
    "UdpSenderSettings",
    "UdpSenderStoppedError",
)

PUBLIC_API = tuple(__all__)
