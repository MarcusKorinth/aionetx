import pytest

from aionetx.api.heartbeat import TcpHeartbeatSettings
from aionetx.api.event_delivery_settings import (
    EventBackpressurePolicy,
    EventDeliverySettings,
    EventDispatchMode,
    EventHandlerFailurePolicy,
)
from aionetx.api.errors import (
    InvalidNetworkConfigurationError,
    NetworkConfigurationError,
    NetworkRuntimeError,
)
from aionetx.api.multicast_receiver_settings import MulticastReceiverSettings
from aionetx.api.error_policy import ErrorPolicy
from aionetx.api.reconnect_jitter import ReconnectJitter
from aionetx.api.tcp_reconnect_settings import TcpReconnectSettings
from aionetx.api.tcp_client import TcpClientSettings
from aionetx.api.tcp_server import TcpServerSettings
from aionetx.api.udp import (
    UdpInvalidTargetError,
    UdpReceiverSettings,
    UdpSenderSettings,
    UdpSenderStoppedError,
)


def test_reconnect_settings_validate_success() -> None:
    TcpReconnectSettings(
        enabled=True,
        initial_delay_seconds=1.0,
        max_delay_seconds=5.0,
        backoff_factor=2.0,
    ).validate()


def test_reconnect_settings_validate_success_with_jitter() -> None:
    TcpReconnectSettings(
        enabled=True,
        initial_delay_seconds=1.0,
        max_delay_seconds=5.0,
        backoff_factor=2.0,
        jitter=ReconnectJitter.FULL,
    ).validate()


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: TcpReconnectSettings(initial_delay_seconds=0), "initial_delay_seconds"),
        (lambda: TcpReconnectSettings(max_delay_seconds=0), "max_delay_seconds"),
        (
            lambda: TcpReconnectSettings(initial_delay_seconds=2.0, max_delay_seconds=1.0),
            "max_delay_seconds",
        ),
        (lambda: TcpReconnectSettings(backoff_factor=0.5), "backoff_factor"),
    ],
)
def test_reconnect_settings_validate_failure(factory, message: str) -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match=message):
        factory()


def test_reconnect_settings_validate_failure_for_invalid_jitter() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="jitter"):
        TcpReconnectSettings(jitter="full")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"initial_delay_seconds": float("nan")},
        {"initial_delay_seconds": float("inf")},
        {"max_delay_seconds": float("nan")},
        {"max_delay_seconds": float("inf")},
        {"backoff_factor": float("nan")},
        {"backoff_factor": float("inf")},
        {"enabled": "yes"},
    ],
)
def test_reconnect_settings_reject_wrong_type_and_non_finite_values(kwargs) -> None:
    with pytest.raises(InvalidNetworkConfigurationError):
        TcpReconnectSettings(**kwargs)  # type: ignore[arg-type]


def test_heartbeat_settings_validate_failure() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="interval_seconds"):
        TcpHeartbeatSettings(enabled=True, interval_seconds=0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enabled": "yes"},
        {"enabled": True, "interval_seconds": float("nan")},
        {"enabled": True, "interval_seconds": float("inf")},
        {"enabled": True, "interval_seconds": "1"},
    ],
)
def test_heartbeat_settings_reject_wrong_type_and_non_finite_values(kwargs) -> None:
    with pytest.raises(InvalidNetworkConfigurationError):
        TcpHeartbeatSettings(**kwargs)  # type: ignore[arg-type]


def test_tcp_client_settings_validate_failure() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="port"):
        TcpClientSettings(host="127.0.0.1", port=0)


def test_tcp_client_settings_validate_failure_for_empty_host() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="host"):
        TcpClientSettings(host="", port=1234)


def test_tcp_client_settings_retry_policy_requires_reconnect_enabled() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="error_policy"):
        TcpClientSettings(
            host="127.0.0.1",
            port=1234,
            reconnect=TcpReconnectSettings(enabled=False),
            error_policy=ErrorPolicy.RETRY,
        )


def test_tcp_client_settings_validate_failure_for_zero_connect_timeout() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="connect_timeout_seconds"):
        TcpClientSettings(host="127.0.0.1", port=1234, connect_timeout_seconds=0)


def test_tcp_client_settings_validate_failure_for_negative_connect_timeout() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="connect_timeout_seconds"):
        TcpClientSettings(host="127.0.0.1", port=1234, connect_timeout_seconds=-1.0)


def test_tcp_client_settings_default_connection_send_timeout() -> None:
    settings = TcpClientSettings(host="127.0.0.1", port=1234)

    assert settings.connection_send_timeout_seconds == 30.0


def test_tcp_client_settings_allow_disabled_connection_send_timeout() -> None:
    settings = TcpClientSettings(
        host="127.0.0.1",
        port=1234,
        connection_send_timeout_seconds=None,
    )

    assert settings.connection_send_timeout_seconds is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": 123},
        {"port": 1234.5},
        {"port": True},
        {"receive_buffer_size": 1024.0},
        {"receive_buffer_size": True},
        {"connect_timeout_seconds": float("nan")},
        {"connect_timeout_seconds": float("inf")},
        {"connect_timeout_seconds": "1"},
        {"connection_send_timeout_seconds": 0},
        {"connection_send_timeout_seconds": -1},
        {"connection_send_timeout_seconds": float("nan")},
        {"connection_send_timeout_seconds": float("inf")},
        {"connection_send_timeout_seconds": True},
        {"connection_send_timeout_seconds": "1"},
        {"error_policy": "retry"},
    ],
)
def test_tcp_client_settings_reject_wrong_type_and_non_finite_values(kwargs) -> None:
    settings_kwargs = {"host": "127.0.0.1", "port": 1234}
    settings_kwargs.update(kwargs)
    with pytest.raises(InvalidNetworkConfigurationError):
        TcpClientSettings(**settings_kwargs)  # type: ignore[arg-type]


def test_tcp_server_settings_validate_failure() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="backlog"):
        TcpServerSettings(host="127.0.0.1", port=1, max_connections=64, backlog=0)


def test_tcp_server_settings_validate_failure_for_none_host() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="host"):
        TcpServerSettings(host=None, port=1, max_connections=64)  # type: ignore[arg-type]


def test_tcp_server_settings_validate_failure_for_empty_host() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="host"):
        TcpServerSettings(host="", port=1, max_connections=64)


def test_tcp_server_settings_validate_failure_for_whitespace_only_host() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="host"):
        TcpServerSettings(host="   ", port=1, max_connections=64)


def test_tcp_server_settings_validate_failure_for_non_positive_max_connections() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="max_connections"):
        TcpServerSettings(host="127.0.0.1", port=1, max_connections=0)


def test_tcp_server_settings_validate_failure_for_non_positive_idle_timeout() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="connection_idle_timeout_seconds"):
        TcpServerSettings(
            host="127.0.0.1",
            port=1,
            max_connections=64,
            connection_idle_timeout_seconds=0,
        )


def test_tcp_server_settings_validate_failure_for_non_positive_broadcast_concurrency_limit() -> (
    None
):
    with pytest.raises(InvalidNetworkConfigurationError, match="broadcast_concurrency_limit"):
        TcpServerSettings(
            host="127.0.0.1",
            port=1,
            max_connections=64,
            broadcast_concurrency_limit=0,
        )


def test_tcp_server_settings_validate_failure_for_non_positive_broadcast_send_timeout() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="broadcast_send_timeout_seconds"):
        TcpServerSettings(
            host="127.0.0.1",
            port=1,
            max_connections=64,
            broadcast_send_timeout_seconds=0,
        )


def test_tcp_server_settings_default_connection_send_timeout() -> None:
    settings = TcpServerSettings(host="127.0.0.1", port=1, max_connections=64)

    assert settings.connection_send_timeout_seconds == 30.0


def test_tcp_server_settings_allow_disabled_connection_send_timeout() -> None:
    settings = TcpServerSettings(
        host="127.0.0.1",
        port=1,
        max_connections=64,
        connection_send_timeout_seconds=None,
    )

    assert settings.connection_send_timeout_seconds is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": 123},
        {"port": 1234.5},
        {"max_connections": 64.0},
        {"connection_idle_timeout_seconds": float("nan")},
        {"connection_idle_timeout_seconds": float("inf")},
        {"broadcast_concurrency_limit": True},
        {"broadcast_send_timeout_seconds": float("nan")},
        {"connection_send_timeout_seconds": 0},
        {"connection_send_timeout_seconds": -1},
        {"connection_send_timeout_seconds": float("nan")},
        {"connection_send_timeout_seconds": float("inf")},
        {"connection_send_timeout_seconds": True},
        {"connection_send_timeout_seconds": "1"},
        {"receive_buffer_size": 4096.0},
        {"backlog": "100"},
    ],
)
def test_tcp_server_settings_reject_wrong_type_and_non_finite_values(kwargs) -> None:
    settings_kwargs = {"host": "127.0.0.1", "port": 1, "max_connections": 64}
    settings_kwargs.update(kwargs)
    with pytest.raises(InvalidNetworkConfigurationError):
        TcpServerSettings(**settings_kwargs)  # type: ignore[arg-type]


def test_multicast_receiver_settings_validate_failure() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="group_ip"):
        MulticastReceiverSettings(group_ip="", port=5000)


def test_multicast_receiver_settings_validate_failure_for_invalid_group_ip() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="group_ip"):
        MulticastReceiverSettings(group_ip="not-an-ip", port=5000)


def test_multicast_receiver_settings_validate_failure_for_non_multicast_group_ip() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="multicast"):
        MulticastReceiverSettings(group_ip="127.0.0.1", port=5000)


def test_multicast_receiver_settings_validate_failure_for_invalid_bind_ip() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="bind_ip"):
        MulticastReceiverSettings(group_ip="239.0.0.1", port=5000, bind_ip="not-an-ip")


def test_multicast_receiver_settings_validate_failure_for_non_positive_receive_buffer() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="receive_buffer_size"):
        MulticastReceiverSettings(group_ip="239.0.0.1", port=5000, receive_buffer_size=0)


def test_multicast_receiver_settings_validate_failure_for_invalid_interface_ip() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="interface_ip"):
        MulticastReceiverSettings(
            group_ip="239.0.0.1",
            port=5000,
            interface_ip="not-an-ip",
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"group_ip": 123},
        {"port": 5000.0},
        {"bind_ip": b"0.0.0.0"},
        {"interface_ip": object()},
        {"receive_buffer_size": True},
    ],
)
def test_multicast_receiver_settings_reject_wrong_type_values(kwargs) -> None:
    settings_kwargs = {"group_ip": "239.0.0.1", "port": 5000}
    settings_kwargs.update(kwargs)
    with pytest.raises(InvalidNetworkConfigurationError):
        MulticastReceiverSettings(**settings_kwargs)  # type: ignore[arg-type]


def test_udp_receiver_settings_validate_failure_for_empty_host() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="host"):
        UdpReceiverSettings(host="", port=5000)


def test_udp_receiver_settings_validate_failure_for_whitespace_only_host() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="host"):
        UdpReceiverSettings(host="   ", port=5000)


def test_udp_sender_settings_validate_failure_for_whitespace_only_default_host() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="default_host"):
        UdpSenderSettings(default_host="   ", default_port=5000)


def test_udp_sender_settings_validate_failure_for_whitespace_only_local_host() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="local_host"):
        UdpSenderSettings(local_host="   ")


def test_udp_sender_settings_validate_failure_for_invalid_default_port() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="default_port"):
        UdpSenderSettings(default_host="127.0.0.1", default_port=70000)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: UdpReceiverSettings(host=123, port=5000),
        lambda: UdpReceiverSettings(host="127.0.0.1", port=5000.0),
        lambda: UdpReceiverSettings(host="127.0.0.1", port=True),
        lambda: UdpReceiverSettings(host="127.0.0.1", port=5000, receive_buffer_size=65535.0),
        lambda: UdpReceiverSettings(host="127.0.0.1", port=5000, enable_broadcast="yes"),
        lambda: UdpSenderSettings(default_host=b"127.0.0.1", default_port=5000),
        lambda: UdpSenderSettings(default_host="127.0.0.1", default_port=5000.0),
        lambda: UdpSenderSettings(local_host=123),
        lambda: UdpSenderSettings(local_port=False),
        lambda: UdpSenderSettings(enable_broadcast=1),
    ],
)
def test_udp_settings_reject_wrong_type_values(factory) -> None:
    with pytest.raises(InvalidNetworkConfigurationError):
        factory()  # type: ignore[misc]


def test_event_delivery_settings_validate_failure_for_invalid_handler_failure_policy() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="handler_failure_policy"):
        EventDeliverySettings(handler_failure_policy="log_only")  # type: ignore[arg-type]


def test_event_delivery_settings_validate_failure_for_invalid_dispatch_mode() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="dispatch_mode"):
        EventDeliverySettings(dispatch_mode="inline")  # type: ignore[arg-type]


def test_event_delivery_settings_validate_failure_for_invalid_backpressure_policy() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="backpressure_policy"):
        EventDeliverySettings(backpressure_policy="block")  # type: ignore[arg-type]


def test_event_delivery_settings_validate_success_for_enum_values() -> None:
    EventDeliverySettings(
        dispatch_mode=EventDispatchMode.INLINE,
        backpressure_policy=EventBackpressurePolicy.DROP_NEWEST,
    ).validate()


def test_settings_construction_is_fail_fast() -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="handler_failure_policy"):
        EventDeliverySettings(handler_failure_policy="invalid")  # type: ignore[arg-type]


@pytest.mark.parametrize("max_pending_events", [0.5, True, "1"])
def test_event_delivery_settings_reject_wrong_type_max_pending_events(max_pending_events) -> None:
    with pytest.raises(InvalidNetworkConfigurationError, match="max_pending_events"):
        EventDeliverySettings(max_pending_events=max_pending_events)  # type: ignore[arg-type]


def test_error_taxonomy_splits_configuration_and_runtime_errors() -> None:
    assert issubclass(InvalidNetworkConfigurationError, NetworkConfigurationError)
    assert issubclass(UdpInvalidTargetError, NetworkConfigurationError)
    assert issubclass(UdpSenderStoppedError, NetworkRuntimeError)


def test_error_taxonomy_keeps_configuration_and_runtime_categories_disjoint() -> None:
    assert not issubclass(UdpSenderStoppedError, NetworkConfigurationError)
    assert not issubclass(UdpInvalidTargetError, NetworkRuntimeError)


def test_error_policy_enum_values_still_validate_under_fail_fast_model() -> None:
    EventDeliverySettings(handler_failure_policy=EventHandlerFailurePolicy.STOP_COMPONENT)
