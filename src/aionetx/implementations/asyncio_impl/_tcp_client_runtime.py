"""
Internal runtime-state container for the asyncio TCP client.

This module groups the mutable client state that supervision and tests both
need to observe, while preserving the private attribute surface expected by the
client implementation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from aionetx.implementations.asyncio_impl.asyncio_heartbeat_sender import AsyncioHeartbeatSender

if TYPE_CHECKING:
    from aionetx.implementations.asyncio_impl.asyncio_tcp_connection import AsyncioTcpConnection


@dataclass
class _ClientRuntimeState:
    """Mutable client runtime state with explicit per-client ownership."""

    running: bool = False
    has_started: bool = False
    connection: AsyncioTcpConnection | None = None
    heartbeat_sender: AsyncioHeartbeatSender | None = None
    connection_closed_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_connect_error: Exception | None = None
    status_version: int = 0
    attempt_counter: int = 0


class _HasClientRuntime(Protocol):
    """Structural protocol for objects that expose ``_runtime`` state."""

    _runtime: _ClientRuntimeState


class _ClientRuntimeAccessors:
    """
    Compatibility accessors for the internal client runtime state.

    The client's tests and supervision helper refer to these private
    attributes directly. Keeping them as accessors preserves the current
    surface while allowing the state container to live in a focused module.
    """

    _runtime: _ClientRuntimeState

    @property
    def _running(self: _HasClientRuntime) -> bool:
        return self._runtime.running

    @_running.setter
    def _running(self: _HasClientRuntime, value: bool) -> None:
        self._runtime.running = value

    @property
    def _has_started(self: _HasClientRuntime) -> bool:
        return self._runtime.has_started

    @_has_started.setter
    def _has_started(self: _HasClientRuntime, value: bool) -> None:
        self._runtime.has_started = value

    @property
    def _connection(self: _HasClientRuntime) -> AsyncioTcpConnection | None:
        return self._runtime.connection

    @_connection.setter
    def _connection(self: _HasClientRuntime, value: AsyncioTcpConnection | None) -> None:
        self._runtime.connection = value

    @property
    def _heartbeat_sender(self: _HasClientRuntime) -> AsyncioHeartbeatSender | None:
        return self._runtime.heartbeat_sender

    @_heartbeat_sender.setter
    def _heartbeat_sender(self: _HasClientRuntime, value: AsyncioHeartbeatSender | None) -> None:
        self._runtime.heartbeat_sender = value

    @property
    def _last_connect_error(self: _HasClientRuntime) -> Exception | None:
        return self._runtime.last_connect_error

    @_last_connect_error.setter
    def _last_connect_error(self: _HasClientRuntime, value: Exception | None) -> None:
        self._runtime.last_connect_error = value

    @property
    def _status_version(self: _HasClientRuntime) -> int:
        return self._runtime.status_version

    @_status_version.setter
    def _status_version(self: _HasClientRuntime, value: int) -> None:
        self._runtime.status_version = value

    @property
    def _attempt_counter(self: _HasClientRuntime) -> int:
        return self._runtime.attempt_counter

    @_attempt_counter.setter
    def _attempt_counter(self: _HasClientRuntime, value: int) -> None:
        self._runtime.attempt_counter = value

    @property
    def _connection_closed_event(self: _HasClientRuntime) -> asyncio.Event:
        return self._runtime.connection_closed_event
