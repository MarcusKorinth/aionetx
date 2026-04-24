from __future__ import annotations

import pytest

from aionetx.api.errors import HeartbeatConfigurationError
from aionetx.api.heartbeat import TcpHeartbeatSettings
from tests.internal_asyncio_impl_refs import validate_heartbeat_provider


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
