from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def reserved_tcp_failure_port() -> Iterator[int]:
    """Hold a bound non-listening TCP port for predictable connection failures."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        yield int(sock.getsockname()[1])


def released_tcp_listener_port() -> int:
    """Return an intentionally released TCP port for managed server checks.

    This released-port helper is intentional: TcpServerSettings requires a
    concrete nonzero port and does not accept a pre-bound socket. Keep this
    helper scoped to installed-artifact checks that must exercise the managed
    TCP server itself.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def released_udp_receiver_port() -> int:
    """Return an intentionally released UDP port for managed receiver checks.

    This released-port helper is intentional: UdpReceiverSettings requires a
    concrete nonzero port and does not accept a pre-bound socket. Keep this
    helper scoped to installed-artifact checks that must exercise the managed
    UDP receiver itself.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
