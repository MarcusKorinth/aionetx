from __future__ import annotations

import asyncio

from aionetx.api.component_lifecycle_state import ComponentLifecycleState
from aionetx.api.connection_lifecycle import ConnectionState
from aionetx.implementations.asyncio_impl._datagram_receiver_state import (
    DatagramRuntimeState,
    DatagramStopProvenance,
    DatagramStopSnapshot,
)


def test_datagram_runtime_state_defaults_to_stopped_created() -> None:
    state = DatagramRuntimeState()

    assert state.running is False
    assert state.sock is None
    assert state.task is None
    assert state.lifecycle_state is ComponentLifecycleState.STOPPED
    assert state.connection_state is ConnectionState.CREATED
    assert state.metadata is None


def test_datagram_stop_snapshot_identifies_noop_stop_plan() -> None:
    snapshot = DatagramStopSnapshot()

    assert snapshot.is_noop is True
    assert snapshot.waits_for_owner is False


def test_datagram_stop_snapshot_identifies_non_owner_wait() -> None:
    loop = asyncio.new_event_loop()
    try:
        waiter = loop.create_future()
        snapshot = DatagramStopSnapshot(stop_waiter=waiter, owns_stop=False)

        assert snapshot.waits_for_owner is True
        assert snapshot.is_noop is True
    finally:
        loop.close()


def test_datagram_stop_snapshot_owner_does_not_wait_for_itself() -> None:
    loop = asyncio.new_event_loop()
    try:
        waiter = loop.create_future()
        snapshot = DatagramStopSnapshot(stop_waiter=waiter, owns_stop=True)

        assert snapshot.waits_for_owner is False
    finally:
        loop.close()


def test_datagram_stop_provenance_reports_deferred_terminal_events() -> None:
    assert DatagramStopProvenance().defers_terminal_events is False
    assert DatagramStopProvenance(handler_originated=True).has_handler_provenance is True
    assert DatagramStopProvenance(handler_originated=True).defers_terminal_events is True
    assert DatagramStopProvenance(inherited_handler_origin=True).has_handler_provenance is True
    assert DatagramStopProvenance(inherited_handler_origin=True).defers_terminal_events is True
    assert DatagramStopProvenance(active_inline_handler=True).has_handler_provenance is False
    assert DatagramStopProvenance(active_inline_handler=True).defers_terminal_events is True
