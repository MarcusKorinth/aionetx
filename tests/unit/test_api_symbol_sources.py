"""
Curated API symbol-source checks for the merge-blocking symbol gate.

These tests stay intentionally small. They verify that the curated root and
``aionetx.api`` surfaces are sourced from their dedicated grouping modules
instead of encouraging deep-module imports.
"""

from __future__ import annotations

import importlib

import aionetx
import aionetx.api as api


def test_api_symbol_root_public_api_matches_dunder_all() -> None:
    assert aionetx.PUBLIC_API == aionetx.__all__


def test_api_symbol_api_public_api_matches_dunder_all() -> None:
    assert api.PUBLIC_API == api.__all__


def test_api_symbol_root_curated_exports_come_from_grouping_modules() -> None:
    grouped_sources = {
        "AsyncioNetworkFactory": "aionetx.factories",
        "BaseNetworkEventHandler": "aionetx.api",
        "BytesReceivedEvent": "aionetx.api",
        "ComponentLifecycleState": "aionetx.api",
        "EventDeliverySettings": "aionetx.api",
        "HeartbeatConfigurationError": "aionetx.api",
        "HeartbeatProviderProtocol": "aionetx.api",
        "MulticastReceiverSettings": "aionetx.api",
        "NetworkEvent": "aionetx.api",
        "TcpHeartbeatSettings": "aionetx.api",
        "TcpReconnectSettings": "aionetx.api",
        "TcpClientSettings": "aionetx.api",
        "TcpServerSettings": "aionetx.api",
        "UdpReceiverSettings": "aionetx.api",
        "UdpSenderSettings": "aionetx.api",
    }

    for symbol_name, expected_module in grouped_sources.items():
        grouped_module = importlib.import_module(expected_module)
        assert getattr(aionetx, symbol_name) is getattr(grouped_module, symbol_name), symbol_name


def test_api_symbol_api_curated_exports_come_from_grouping_modules() -> None:
    grouped_sources = {
        "BaseNetworkEventHandler": "aionetx.api.events",
        "BytesReceivedEvent": "aionetx.api.events",
        "ComponentLifecycleChangedEvent": "aionetx.api.events",
        "ConnectionClosedError": "aionetx.api.errors",
        "ConnectionClosedEvent": "aionetx.api.events",
        "ConnectionMetadata": "aionetx.api.lifecycle",
        "ConnectionOpenedEvent": "aionetx.api.events",
        "ConnectionProtocol": "aionetx.api.protocols",
        "ConnectionRole": "aionetx.api.lifecycle",
        "ConnectionState": "aionetx.api.lifecycle",
        "ErrorPolicy": "aionetx.api.policies",
        "EventDeliverySettings": "aionetx.api.policies",
        "HandlerFailurePolicyStopEvent": "aionetx.api.events",
        "HeartbeatProviderProtocol": "aionetx.api.protocols",
        "HeartbeatResult": "aionetx.api.heartbeat",
        "NetworkConfigurationError": "aionetx.api.errors",
        "MulticastReceiverProtocol": "aionetx.api.protocols",
        "MulticastReceiverSettings": "aionetx.api.settings",
        "NetworkFactory": "aionetx.api.protocols",
        "NetworkErrorEvent": "aionetx.api.events",
        "NetworkEvent": "aionetx.api.events",
        "NetworkLayerError": "aionetx.api.errors",
        "NetworkRuntimeError": "aionetx.api.errors",
        "ReconnectScheduledEvent": "aionetx.api.events",
        "TcpClientProtocol": "aionetx.api.protocols",
        "TcpClientSettings": "aionetx.api.settings",
        "TcpServerProtocol": "aionetx.api.protocols",
        "TcpServerSettings": "aionetx.api.settings",
        "TypedEventRouter": "aionetx.api.typed_event_router",
        "UdpReceiverProtocol": "aionetx.api.protocols",
        "UdpReceiverSettings": "aionetx.api.settings",
        "UdpSenderProtocol": "aionetx.api.protocols",
        "UdpSenderSettings": "aionetx.api.settings",
    }

    for symbol_name, expected_module in grouped_sources.items():
        grouped_module = importlib.import_module(expected_module)
        assert getattr(api, symbol_name) is getattr(grouped_module, symbol_name), symbol_name
