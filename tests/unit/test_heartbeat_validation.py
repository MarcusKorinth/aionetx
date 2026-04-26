from __future__ import annotations

import pytest

from aionetx.api.errors import HeartbeatConfigurationError
from aionetx.api.heartbeat import HeartbeatResult
from aionetx.api.heartbeat import TcpHeartbeatSettings
from tests.internal_asyncio_impl_refs import validate_heartbeat_provider


@pytest.mark.parametrize("payload", [b"hb", bytearray(b"hb"), memoryview(b"hb")])
def test_heartbeat_result_accepts_bytes_like_payload(
    payload: bytes | bytearray | memoryview,
) -> None:
    result = HeartbeatResult(should_send=True, payload=payload)

    assert result.payload is payload


@pytest.mark.parametrize("should_send", [1, "yes", None])
def test_heartbeat_result_requires_exact_bool_should_send(should_send: object) -> None:
    with pytest.raises(TypeError, match="HeartbeatResult.should_send"):
        HeartbeatResult(should_send=should_send, payload=b"hb")  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", ["hb", object(), None])
def test_heartbeat_result_requires_bytes_like_payload(payload: object) -> None:
    with pytest.raises(TypeError, match="HeartbeatResult.payload"):
        HeartbeatResult(should_send=True, payload=payload)  # type: ignore[arg-type]


def test_heartbeat_result_requires_bytes_like_payload_even_when_not_sending() -> None:
    with pytest.raises(TypeError, match="HeartbeatResult.payload"):
        HeartbeatResult(should_send=False, payload=None)  # type: ignore[arg-type]


def test_heartbeat_validation_requires_provider_when_enabled() -> None:
    with pytest.raises(HeartbeatConfigurationError, match="heartbeat_provider"):
        validate_heartbeat_provider(
            heartbeat_settings=TcpHeartbeatSettings(enabled=True, interval_seconds=1.0),
            heartbeat_provider=None,
        )


def test_heartbeat_validation_allows_missing_provider_when_disabled() -> None:
    validate_heartbeat_provider(
        heartbeat_settings=TcpHeartbeatSettings(enabled=False, interval_seconds=1.0),
        heartbeat_provider=None,
    )
