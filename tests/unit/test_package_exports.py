"""
Public import-boundary tests for the curated aionetx API.

These checks stay intentionally small. They cover the import paths that user
code is encouraged to rely on, while avoiding assertions that pin internal
module layout, implementation namespaces, or complete export lists.
"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest

from aionetx.api.base_network_event_handler import BaseNetworkEventHandler
from aionetx.api.byte_sender_protocol import ByteSenderProtocol
from aionetx.api.bytes_like import BytesLike
from aionetx.api.errors import NetworkConfigurationError, NetworkLayerError, NetworkRuntimeError
from aionetx.api.managed_transport_protocol import ManagedTransportProtocol
from aionetx.api.multicast_receiver_protocol import MulticastReceiverProtocol
from aionetx.api.network_event import NetworkEvent
from aionetx.api.tcp_client import TcpClientProtocol, TcpClientSettings
from aionetx.api.tcp_server import TcpServerProtocol, TcpServerSettings
from aionetx.api.typed_event_router import TypedEventRouter
from aionetx.api.udp import (
    UdpInvalidTargetError,
    UdpReceiverProtocol,
    UdpSenderProtocol,
    UdpSenderStoppedError,
)
from aionetx.factories import AsyncioNetworkFactory


def _api_module() -> ModuleType:
    return importlib.import_module("aionetx.api")


def _root_module() -> ModuleType:
    return importlib.import_module("aionetx")


def test_package_root_exports_recommended_entry_points() -> None:
    aionetx = _root_module()

    assert aionetx.AsyncioNetworkFactory is AsyncioNetworkFactory
    assert aionetx.TcpClientSettings is TcpClientSettings
    assert aionetx.TcpServerSettings is TcpServerSettings
    assert aionetx.BaseNetworkEventHandler is BaseNetworkEventHandler
    assert aionetx.NetworkEvent is NetworkEvent


def test_aionetx_api_exports_transport_protocols_for_advanced_usage() -> None:
    api = _api_module()

    assert api.TcpClientProtocol is TcpClientProtocol
    assert api.TcpServerProtocol is TcpServerProtocol
    assert api.UdpReceiverProtocol is UdpReceiverProtocol
    assert api.UdpSenderProtocol is UdpSenderProtocol
    assert api.MulticastReceiverProtocol is MulticastReceiverProtocol


def test_aionetx_api_exports_bytes_capability_types() -> None:
    api = _api_module()

    assert api.BytesLike is BytesLike
    assert api.ByteSenderProtocol is ByteSenderProtocol
    assert api.ManagedTransportProtocol is ManagedTransportProtocol


def test_aionetx_api_exports_udp_send_exceptions() -> None:
    api = _api_module()

    assert api.UdpSenderStoppedError is UdpSenderStoppedError
    assert api.UdpInvalidTargetError is UdpInvalidTargetError


def test_aionetx_api_exports_exception_bases_and_typed_router() -> None:
    api = _api_module()

    assert api.NetworkLayerError is NetworkLayerError
    assert api.NetworkConfigurationError is NetworkConfigurationError
    assert api.NetworkRuntimeError is NetworkRuntimeError
    assert api.TypedEventRouter is TypedEventRouter


def test_recording_event_handler_is_available_only_from_testing_namespace() -> None:
    aionetx = _root_module()

    from aionetx.testing import RecordingEventHandler

    assert RecordingEventHandler.__name__ == "RecordingEventHandler"
    assert "RecordingEventHandler" not in aionetx.PUBLIC_API

    with pytest.raises(ImportError):
        exec("from aionetx import RecordingEventHandler", {})


@pytest.mark.parametrize(
    "unsupported_root_symbol",
    [
        "TcpClientProtocol",
        "TcpServerProtocol",
        "UdpReceiverProtocol",
        "UdpSenderProtocol",
        "MulticastReceiverProtocol",
        "ConnectionClosedEvent",
    ],
)
def test_root_import_does_not_expose_advanced_or_internal_symbols(
    unsupported_root_symbol: str,
) -> None:
    with pytest.raises(ImportError):
        exec(f"from aionetx import {unsupported_root_symbol}", {})
