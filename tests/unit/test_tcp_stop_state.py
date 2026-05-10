from __future__ import annotations

import asyncio

from aionetx.implementations.asyncio_impl._tcp_client_stop_state import (
    TcpClientStopExecutionState,
    TcpClientStopPlan,
    TcpClientStopProvenance,
)
from aionetx.implementations.asyncio_impl._tcp_server_stop_state import (
    TcpServerStopExecutionState,
    TcpServerStopPlan,
    TcpServerStopProvenance,
)


def test_tcp_client_stop_plan_waits_only_for_non_owner_waiter() -> None:
    loop = asyncio.new_event_loop()
    try:
        waiter = loop.create_future()

        assert TcpClientStopPlan().waits_for_owner is False
        assert TcpClientStopPlan(stop_waiter=waiter, owns_stop=False).waits_for_owner is True
        assert TcpClientStopPlan(stop_waiter=waiter, owns_stop=True).waits_for_owner is False
    finally:
        loop.close()


def test_tcp_client_stop_provenance_defers_for_handler_or_inline_origin() -> None:
    assert TcpClientStopProvenance().defers_terminal_events is False
    assert TcpClientStopProvenance(handler_originated=True).defers_terminal_events is True
    assert TcpClientStopProvenance(active_inline_handler=True).defers_terminal_events is True


def test_tcp_client_stop_execution_state_defaults_to_inline_completion() -> None:
    state = TcpClientStopExecutionState()

    assert state.deferred_close_waiters == ()
    assert state.stop_waiter_completion_deferred is False
    assert state.raise_cancel_after_stop_waiter is False


def test_tcp_server_stop_plan_waits_only_for_non_owner_waiter() -> None:
    loop = asyncio.new_event_loop()
    try:
        waiter = loop.create_future()

        assert TcpServerStopPlan().waits_for_owner is False
        assert TcpServerStopPlan(stop_waiter=waiter, owns_stop=False).waits_for_owner is True
        assert TcpServerStopPlan(stop_waiter=waiter, owns_stop=True).waits_for_owner is False
    finally:
        loop.close()


def test_tcp_server_stop_provenance_defers_for_handler_or_inline_origin() -> None:
    assert TcpServerStopProvenance().defers_terminal_events is False
    assert TcpServerStopProvenance(handler_originated=True).defers_terminal_events is True
    assert TcpServerStopProvenance(active_inline_handler=True).defers_terminal_events is True


def test_tcp_server_stop_execution_state_defaults_to_inline_completion() -> None:
    state = TcpServerStopExecutionState()

    assert state.deferred_close_waiters == ()
    assert state.stop_waiter_completion_deferred is False
    assert state.dispatcher_stopped_from_handler_origin is False
    assert state.raise_cancel_after_stop_waiter is False
