from __future__ import annotations

import ipaddress
import socket

from dreamland.models import ConnectionEndpoint, ConnectionMetadata
from dreamland.profile import ServerProfile
from dreamland.world_state import host_metadata_for_ref

_LOOPBACK_TEST_SOURCE_IP = "198.51.100.23"


def initial_ssh_connection_metadata(
    client_socket: socket.socket,
    profile: ServerProfile,
) -> ConnectionMetadata:
    observed_source = _endpoint_from_socket_address(client_socket.getpeername())
    observed_destination = _endpoint_from_socket_address(client_socket.getsockname())
    return ConnectionMetadata(
        protocol="ssh",
        source=_initial_ssh_source_endpoint(client_socket),
        destination=_initial_ssh_destination_endpoint(profile, client_socket),
        metadata={
            "transport": {
                "source": {"ip": observed_source.ip, "port": observed_source.port},
                "destination": {
                    "ip": observed_destination.ip,
                    "port": observed_destination.port,
                },
            }
        },
    )


def _initial_ssh_source_endpoint(
    client_socket: socket.socket,
) -> ConnectionEndpoint:
    endpoint = _endpoint_from_socket_address(client_socket.getpeername())
    if endpoint.ip is not None and _is_loopback_ip(endpoint.ip):
        return ConnectionEndpoint(ip=_LOOPBACK_TEST_SOURCE_IP, port=endpoint.port)
    return endpoint


def _initial_ssh_destination_endpoint(
    profile: ServerProfile,
    client_socket: socket.socket,
) -> ConnectionEndpoint:
    profile_endpoint = _ssh_endpoint_from_world_state(profile)
    if profile_endpoint.ip is not None or profile_endpoint.port is not None:
        return profile_endpoint
    return _endpoint_from_socket_address(client_socket.getsockname())


def _ssh_endpoint_from_world_state(
    profile: ServerProfile,
) -> ConnectionEndpoint:
    entry_host = profile.world_state.get("entry_host")
    metadata = host_metadata_for_ref(
        profile.world_state,
        entry_host if isinstance(entry_host, str) and entry_host.strip() else None,
    )
    network = metadata.get("network")
    if not isinstance(network, dict):
        return ConnectionEndpoint()
    return ConnectionEndpoint(
        ip=_primary_profile_ip(network.get("interfaces")),
        port=_profile_ssh_port(network.get("listening_ports")),
    )


def _primary_profile_ip(interfaces: object) -> str | None:
    if not isinstance(interfaces, list):
        return None
    fallback: str | None = None
    for item in interfaces:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        if not isinstance(address, str) or not address:
            continue
        ip = address.split("/", maxsplit=1)[0]
        if fallback is None:
            fallback = ip
        if not ip.startswith("127."):
            return ip
    return fallback


def _profile_ssh_port(listening_ports: object) -> int | None:
    if not isinstance(listening_ports, list):
        return None
    for item in listening_ports:
        if not isinstance(item, dict):
            continue
        service = item.get("service") or item.get("name")
        protocol = item.get("protocol")
        if service != "ssh" or protocol not in {None, "tcp"}:
            continue
        port = item.get("port")
        if port is None:
            return None
        try:
            return int(port)
        except (TypeError, ValueError):
            return None
    return None


def _is_loopback_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _endpoint_from_socket_address(address: object) -> ConnectionEndpoint:
    if not isinstance(address, tuple) or len(address) < 2:
        return ConnectionEndpoint()
    host, port = address[:2]
    return ConnectionEndpoint(ip=str(host), port=int(port))
