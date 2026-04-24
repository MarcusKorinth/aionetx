"""
Canonical component/connection ID builders for asyncio implementations.

Keep all ID format strings here to avoid drift across components while
enforcing one normalized identifier schema.
"""

from __future__ import annotations


def tcp_client_connection_id(default_host: str, default_port: int, peer_info: object) -> str:
    """Build the canonical connection ID for a TCP client session."""
    host, port = _host_port_or_default(peer_info, default_host, default_port)
    return f"tcp/client/{host}/{port}/connection"


def tcp_client_component_id(host: str, port: int) -> str:
    """Build the canonical component ID for a TCP client transport."""
    return f"tcp/client/{host}/{port}"


def tcp_server_connection_id(peer_info: object, sequence: int) -> str:
    """Build the canonical connection ID for one accepted TCP server session."""
    if isinstance(peer_info, tuple) and len(peer_info) >= 2:
        return f"tcp/server/{peer_info[0]}/{peer_info[1]}/connection/{sequence}"
    return f"tcp/server/unknown/unknown/connection/{sequence}"


def tcp_server_component_id(host: str, port: int) -> str:
    """Build the canonical component ID for a TCP server transport."""
    return f"tcp/server/{host}/{port}"


def udp_receiver_component_id(host: str, port: int) -> str:
    """Build the canonical component ID for a UDP receiver transport."""
    return f"udp/receiver/{host}/{port}"


def udp_sender_component_id(local_host: str, local_port: int) -> str:
    """Build the canonical component ID for a UDP sender transport."""
    return f"udp/sender/{local_host}/{local_port}"


def multicast_receiver_component_id(group_ip: str, port: int) -> str:
    """Build the canonical component ID for a multicast receiver transport."""
    return f"udp/multicast/{group_ip}/{port}"


def _host_port_or_default(
    peer_info: object,
    default_host: str,
    default_port: int,
) -> tuple[object, object]:
    """Return ``peer_info`` host/port when present, otherwise the supplied defaults."""
    if isinstance(peer_info, tuple) and len(peer_info) >= 2:
        return peer_info[0], peer_info[1]
    return default_host, default_port
