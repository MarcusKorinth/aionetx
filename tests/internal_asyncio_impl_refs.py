"""
Test-local indirection for unstable asyncio implementation symbols.

This keeps brittle internal import paths centralized in one test utility module.
"""

from aionetx.implementations.asyncio_impl.identifier_utils import (
    multicast_receiver_component_id,
    tcp_client_component_id,
    tcp_client_connection_id,
    tcp_server_component_id,
    tcp_server_connection_id,
    udp_receiver_component_id,
)
from aionetx.implementations.asyncio_impl.lifecycle_internal import (
    LifecycleRole,
    apply_stopped_transition_if_stopping,
    apply_stopping_transition_if_active,
    assert_legal_lifecycle_transition,
)
from aionetx.implementations.asyncio_impl.runtime_utils import (
    ReconnectBackoff,
    WarningRateLimiter,
    validate_heartbeat_provider,
)

__all__ = (
    "LifecycleRole",
    "ReconnectBackoff",
    "WarningRateLimiter",
    "apply_stopped_transition_if_stopping",
    "apply_stopping_transition_if_active",
    "assert_legal_lifecycle_transition",
    "multicast_receiver_component_id",
    "tcp_client_component_id",
    "tcp_client_connection_id",
    "tcp_server_component_id",
    "tcp_server_connection_id",
    "udp_receiver_component_id",
    "validate_heartbeat_provider",
)
