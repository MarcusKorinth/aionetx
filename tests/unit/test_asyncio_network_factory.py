"""
Unit tests for AsyncioNetworkFactory validation and return types.

These tests pin the factory-first onboarding contract: valid settings produce
the expected transport implementations, invalid settings fail fast, and
handler or heartbeat-provider requirements are enforced consistently between
the factory and the direct constructors.
"""

from __future__ import annotations

import pytest

from aionetx.api.event_delivery_settings import EventDeliverySettings
from aionetx.api.errors import HeartbeatConfigurationError, InvalidNetworkConfigurationError
from aionetx.api.heartbeat import HeartbeatRequest
from aionetx.api.heartbeat import HeartbeatResult
from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
from aionetx.api.network_factory import NetworkFactory
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.api.udp import UdpReceiverSettings
from aionetx.api.udp import UdpSenderSettings
from aionetx.factories.asyncio_network_factory import AsyncioNetworkFactory
from aionetx.implementations.asyncio_impl.asyncio_multicast_receiver import AsyncioMulticastReceiver
from aionetx.implementations.asyncio_impl.asyncio_tcp_client import AsyncioTcpClient
from aionetx.implementations.asyncio_impl.asyncio_tcp_server import AsyncioTcpServer
from aionetx.implementations.asyncio_impl.asyncio_udp_receiver import AsyncioUdpReceiver
from aionetx.implementations.asyncio_impl.asyncio_udp_sender import AsyncioUdpSender


class _AsyncHandler:
    async def on_event(self, event) -> None:
        return None


class _SyncHandler:
    def on_event(self, event) -> None:
        return None


class _AsyncCallableOnEvent:
    async def __call__(self, event) -> None:
        return None


class _HandlerWithAsyncCallableAttribute:
    def __init__(self) -> None:
        self.on_event = _AsyncCallableOnEvent()


class _HeartbeatProvider:
    async def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        return HeartbeatResult(should_send=True, payload=b"hb")


class _SyncHeartbeatProvider:
    def create_heartbeat(self, request: HeartbeatRequest) -> HeartbeatResult:
        return HeartbeatResult(should_send=True, payload=b"hb")


def test_factory_returns_expected_concrete_types() -> None:
    factory = AsyncioNetworkFactory()
    handler = _AsyncHandler()

    client = factory.create_tcp_client(
        TcpClientSettings(
            host="127.0.0.1", port=12345, reconnect=TcpReconnectSettings(enabled=False)
        ),
        handler,
    )
    server = factory.create_tcp_server(
        TcpServerSettings(host="127.0.0.1", port=12346, max_connections=64), handler
    )
    receiver = factory.create_multicast_receiver(
        MulticastReceiverSettings(group_ip="239.1.1.1", port=12347),
        handler,
    )
    udp_receiver = factory.create_udp_receiver(
        UdpReceiverSettings(host="127.0.0.1", port=12348),
        handler,
    )
    udp_sender = factory.create_udp_sender(UdpSenderSettings())

    assert isinstance(client, AsyncioTcpClient)
    assert isinstance(server, AsyncioTcpServer)
    assert isinstance(receiver, AsyncioMulticastReceiver)
    assert isinstance(udp_receiver, AsyncioUdpReceiver)
    assert isinstance(udp_sender, AsyncioUdpSender)


def test_factory_conforms_to_explicit_network_factory_protocol() -> None:
    factory = AsyncioNetworkFactory()

    assert isinstance(factory, NetworkFactory)


def test_factory_requires_heartbeat_provider_when_client_heartbeat_enabled() -> None:
    factory = AsyncioNetworkFactory()
    settings = TcpClientSettings(
        host="127.0.0.1",
        port=12345,
        reconnect=TcpReconnectSettings(enabled=False),
        heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=1.0),
        event_delivery=EventDeliverySettings(),
    )

    with pytest.raises(HeartbeatConfigurationError, match="heartbeat_provider"):
        factory.create_tcp_client(settings=settings, event_handler=_AsyncHandler())


def test_factory_requires_heartbeat_provider_when_server_heartbeat_enabled() -> None:
    factory = AsyncioNetworkFactory()
    settings = TcpServerSettings(
        host="127.0.0.1",
        port=12345,
        max_connections=64,
        heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=1.0),
    )

    with pytest.raises(HeartbeatConfigurationError, match="heartbeat_provider"):
        factory.create_tcp_server(settings=settings, event_handler=_AsyncHandler())


def test_direct_server_constructor_requires_heartbeat_provider_when_server_heartbeat_enabled() -> (
    None
):
    with pytest.raises(HeartbeatConfigurationError, match="heartbeat_provider"):
        AsyncioTcpServer(
            settings=TcpServerSettings(
                host="127.0.0.1",
                port=12345,
                max_connections=64,
                heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=1.0),
            ),
            event_handler=_AsyncHandler(),
        )


def test_factory_rejects_non_async_heartbeat_provider() -> None:
    factory = AsyncioNetworkFactory()
    with pytest.raises(TypeError, match="create_heartbeat"):
        factory.create_tcp_server(
            settings=TcpServerSettings(
                host="127.0.0.1",
                port=12345,
                max_connections=64,
                heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=1.0),
            ),
            event_handler=_AsyncHandler(),
            heartbeat_provider=_SyncHeartbeatProvider(),  # type: ignore[arg-type]
        )


def test_direct_client_constructor_rejects_non_async_heartbeat_provider() -> None:
    with pytest.raises(TypeError, match="create_heartbeat"):
        AsyncioTcpClient(
            settings=TcpClientSettings(
                host="127.0.0.1",
                port=12345,
                reconnect=TcpReconnectSettings(enabled=False),
                heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=1.0),
            ),
            event_handler=_AsyncHandler(),
            heartbeat_provider=_SyncHeartbeatProvider(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "factory_method,kwargs",
    [
        (
            "create_tcp_client",
            {
                "settings": TcpClientSettings(
                    host="127.0.0.1", port=12345, reconnect=TcpReconnectSettings(enabled=False)
                ),
                "heartbeat_provider": None,
            },
        ),
        (
            "create_tcp_server",
            {
                "settings": TcpServerSettings(host="127.0.0.1", port=12345, max_connections=64),
                "heartbeat_provider": None,
            },
        ),
        (
            "create_multicast_receiver",
            {"settings": MulticastReceiverSettings(group_ip="239.1.1.1", port=12345)},
        ),
        (
            "create_udp_receiver",
            {"settings": UdpReceiverSettings(host="127.0.0.1", port=12345)},
        ),
    ],
)
def test_factory_rejects_non_async_event_handler(
    factory_method: str, kwargs: dict[str, object]
) -> None:
    factory = AsyncioNetworkFactory()
    method = getattr(factory, factory_method)

    with pytest.raises(TypeError, match="on_event"):
        method(event_handler=_SyncHandler(), **kwargs)


def test_factory_accepts_heartbeat_provider_when_required() -> None:
    factory = AsyncioNetworkFactory()
    client = factory.create_tcp_client(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=12345,
            reconnect=TcpReconnectSettings(enabled=False),
            heartbeat=TcpHeartbeatSettings(enabled=True, interval_seconds=1.0),
        ),
        event_handler=_AsyncHandler(),
        heartbeat_provider=_HeartbeatProvider(),
    )
    assert isinstance(client, AsyncioTcpClient)


def test_factory_accepts_async_callable_object_for_on_event() -> None:
    factory = AsyncioNetworkFactory()
    client = factory.create_tcp_client(
        settings=TcpClientSettings(
            host="127.0.0.1",
            port=12345,
            reconnect=TcpReconnectSettings(enabled=False),
        ),
        event_handler=_HandlerWithAsyncCallableAttribute(),  # type: ignore[arg-type]
    )
    assert isinstance(client, AsyncioTcpClient)


@pytest.mark.parametrize(
    "constructor,kwargs",
    [
        (
            AsyncioTcpClient,
            {
                "settings": TcpClientSettings(
                    host="127.0.0.1", port=12345, reconnect=TcpReconnectSettings(enabled=False)
                ),
            },
        ),
        (
            AsyncioTcpServer,
            {
                "settings": TcpServerSettings(host="127.0.0.1", port=12345, max_connections=64),
            },
        ),
        (
            AsyncioMulticastReceiver,
            {
                "settings": MulticastReceiverSettings(group_ip="239.1.1.1", port=12345),
            },
        ),
        (
            AsyncioUdpReceiver,
            {
                "settings": UdpReceiverSettings(host="127.0.0.1", port=12345),
            },
        ),
    ],
)
def test_direct_constructor_matches_factory_event_handler_validation(
    constructor: object,
    kwargs: dict[str, object],
) -> None:
    factory = AsyncioNetworkFactory()

    with pytest.raises(TypeError, match="on_event"):
        constructor(event_handler=_SyncHandler(), **kwargs)  # type: ignore[misc]

    factory_method_by_constructor = {
        AsyncioTcpClient: factory.create_tcp_client,
        AsyncioTcpServer: factory.create_tcp_server,
        AsyncioMulticastReceiver: factory.create_multicast_receiver,
        AsyncioUdpReceiver: factory.create_udp_receiver,
    }
    method = factory_method_by_constructor[constructor]  # type: ignore[index]
    with pytest.raises(TypeError, match="on_event"):
        method(event_handler=_SyncHandler(), **kwargs)


@pytest.mark.parametrize(
    ("factory_method", "kwargs_factory", "match"),
    [
        (
            "create_tcp_client",
            lambda: {
                "settings": TcpClientSettings(host="", port=1234),
                "event_handler": _AsyncHandler(),
            },
            "host",
        ),
        (
            "create_tcp_server",
            lambda: {
                "settings": TcpServerSettings(host="127.0.0.1", port=0, max_connections=64),
                "event_handler": _AsyncHandler(),
            },
            "port",
        ),
        (
            "create_multicast_receiver",
            lambda: {
                "settings": MulticastReceiverSettings(group_ip="", port=5000),
                "event_handler": _AsyncHandler(),
            },
            "group_ip",
        ),
        (
            "create_udp_receiver",
            lambda: {
                "settings": UdpReceiverSettings(host="", port=5000),
                "event_handler": _AsyncHandler(),
            },
            "host",
        ),
        (
            "create_udp_sender",
            lambda: {
                "settings": UdpSenderSettings(default_port=70000),
            },
            "default_port",
        ),
    ],
)
def test_factory_validates_settings_at_creation_time(
    factory_method: str, kwargs_factory, match: str
) -> None:
    factory = AsyncioNetworkFactory()
    method = getattr(factory, factory_method)
    with pytest.raises(InvalidNetworkConfigurationError, match=match):
        method(**kwargs_factory())
