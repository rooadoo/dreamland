from __future__ import annotations

import ipaddress
from typing import Literal, cast

from dreamland.models import (
    ConnectionEndpoint,
    FrameConnectionBinding,
    SessionConnections,
    SimulatedConnection,
)


def network_connections_to_state(value: SessionConnections) -> dict[str, object]:
    """Serialize fictional connections separately from authored world inventory."""
    return {
        "connections": {
            connection_id: {
                "id": connection.id,
                "source_host": connection.source_host,
                "destination_host": connection.destination_host,
                "source": {
                    "ip": connection.source.ip,
                    "port": connection.source.port,
                },
                "destination": {
                    "ip": connection.destination.ip,
                    "port": connection.destination.port,
                },
                "phase": connection.phase,
                "entry": connection.entry,
                **(
                    {
                        "source_peer": {
                            "ip": connection.source_peer.ip,
                            "port": connection.source_peer.port,
                        }
                    }
                    if connection.source_peer is not None
                    else {}
                ),
                **(
                    {
                        "destination_peer": {
                            "ip": connection.destination_peer.ip,
                            "port": connection.destination_peer.port,
                        }
                    }
                    if connection.destination_peer is not None
                    else {}
                ),
            }
            for connection_id, connection in value.connections.items()
        },
        "frames": [
            {
                "login_connection_id": frame.login_connection_id,
                "owned_connection_id": frame.owned_connection_id,
                "retained_connection_ids": list(frame.retained_connection_ids),
            }
            for frame in value.frames
        ],
    }


def network_connections_from_state(
    value: object, *, frame_count: int
) -> SessionConnections:
    """Load compatible snapshots without accepting malformed edges or references."""
    if frame_count < 0:
        raise ValueError("frame_count must not be negative")
    result = SessionConnections(
        frames=[FrameConnectionBinding() for _ in range(frame_count)]
    )
    if not isinstance(value, dict):
        return result
    connections = value.get("connections")
    if isinstance(connections, dict):
        for connection_id, item in connections.items():
            connection = _connection_from_state(connection_id, item)
            if connection is not None:
                result.connections[connection.id] = connection
    frames = value.get("frames")
    owners: set[str] = set()
    if isinstance(frames, list):
        for index, item in enumerate(frames[:frame_count]):
            if not isinstance(item, dict):
                continue
            login = _reference(item.get("login_connection_id"), result.connections)
            if login and result.connections[login].phase != "authenticated":
                login = None
            owned = _reference(item.get("owned_connection_id"), result.connections)
            if owned in owners:
                owned = None
            if owned is not None:
                owners.add(owned)
            retained: list[str] = []
            retained_value = item.get("retained_connection_ids")
            if isinstance(retained_value, list):
                for value in retained_value:
                    connection_id = _reference(value, result.connections)
                    if connection_id is not None and connection_id not in owners:
                        retained.append(connection_id)
                        owners.add(connection_id)
            result.frames[index] = FrameConnectionBinding(login, owned, tuple(retained))
    result.connections = {
        key: connection
        for key, connection in result.connections.items()
        if key in owners
    }
    result.frames = [
        FrameConnectionBinding(
            login_connection_id=_reference(
                frame.login_connection_id, result.connections
            ),
            owned_connection_id=frame.owned_connection_id,
            retained_connection_ids=frame.retained_connection_ids,
        )
        for frame in result.frames
    ]
    return result


def _connection_from_state(
    connection_id: object, value: object
) -> SimulatedConnection | None:
    if not _nonempty_string(connection_id) or not isinstance(value, dict):
        return None
    if value.get("id") != connection_id:
        return None
    source_host = value.get("source_host")
    destination_host = value.get("destination_host")
    if source_host is not None and not _nonempty_string(source_host):
        return None
    if not _nonempty_string(destination_host):
        return None
    phase = value.get("phase", "authenticated")
    if not isinstance(phase, str) or phase not in {"authenticating", "authenticated"}:
        return None
    entry = value.get("entry", False)
    if not isinstance(entry, bool):
        return None
    source = _endpoint_from_state(value.get("source"))
    destination = _endpoint_from_state(value.get("destination"))
    if source is None or destination is None:
        return None
    source_peer_value = value.get("source_peer")
    destination_peer_value = value.get("destination_peer")
    source_peer = (
        _endpoint_from_state(source_peer_value)
        if source_peer_value is not None
        else None
    )
    destination_peer = (
        _endpoint_from_state(destination_peer_value)
        if destination_peer_value is not None
        else None
    )
    if (source_peer_value is not None and source_peer is None) or (
        destination_peer_value is not None and destination_peer is None
    ):
        return None
    # Values have been validated above; explicit narrowing keeps malformed JSON
    # out of the typed runtime state without coercing IDs, hosts, or ports.
    assert isinstance(connection_id, str)
    assert source_host is None or isinstance(source_host, str)
    assert isinstance(destination_host, str)
    return SimulatedConnection(
        id=connection_id,
        source_host=source_host,
        destination_host=destination_host,
        source=source,
        destination=destination,
        phase=cast(Literal["authenticating", "authenticated"], phase),
        entry=entry,
        source_peer=source_peer,
        destination_peer=destination_peer,
    )


def _endpoint_from_state(value: object) -> ConnectionEndpoint | None:
    if not isinstance(value, dict):
        return None
    ip = value.get("ip")
    if ip is not None:
        if not isinstance(ip, str):
            return None
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return None
    port = value.get("port")
    if port is not None and (
        not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
    ):
        return None
    return ConnectionEndpoint(ip=ip, port=port)


def _reference(
    value: object, connections: dict[str, SimulatedConnection]
) -> str | None:
    return value if isinstance(value, str) and value in connections else None


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
