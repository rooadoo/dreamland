"""Runtime ownership of the SSH sockets participating in a simulated session.

The transport's ingress metadata remains operator provenance. This module owns
the fictional endpoints and stack lifetimes, independently of model patches.
"""

from __future__ import annotations

import ipaddress
import logging
import shlex
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from uuid import uuid4

from dreamland.agent_shell import analyze_command
from dreamland.connection_translation import translated_ssh_target
from dreamland.models import (
    ConnectionEndpoint,
    ConnectionMetadata,
    EnvironmentMetadata,
    FrameConnectionBinding,
    SessionConnections,
    SessionState,
    ShellEnvironment,
    ShellFrame,
    ShellStackTransition,
    SimulatedConnection,
)
from dreamland.network_identity import (
    ensure_host_address,
    resolve_host_ref,
    resolve_target,
    source_address_for_target,
)
from dreamland.world_state import active_host_ref, hostname_for_host_ref

LOGGER = logging.getLogger("dreamland.lifecycle")
_PROVISIONAL_EVENTS: ContextVar[bool] = ContextVar(
    "network_events_provisional", default=False
)


@contextmanager
def provisional_connection_events():
    """Label detached product effects until its core accepts the proposal."""
    token = _PROVISIONAL_EVENTS.set(True)
    try:
        yield
    finally:
        _PROVISIONAL_EVENTS.reset(token)


@dataclass(frozen=True, slots=True)
class SSHRequest:
    target: str
    port: int | None = None


def initialize_session_connections(
    session: SessionState,
    *,
    ingress: ConnectionMetadata | None = None,
    restore: bool = False,
) -> None:
    """Bind a real admission once; ordinary checkpoint hydration is inert."""
    if not session.shell_stack:
        return
    state = session.network_connections
    if not restore:
        state = SessionConnections()
        session.network_connections = state
    legacy = not state.frames
    if legacy:
        state.frames = [FrameConnectionBinding() for _ in session.shell_stack]
    _prune(session)
    if ingress is not None and ingress.protocol == "ssh":
        root = session.shell_stack[0]
        previous = next(
            (edge for edge in state.connections.values() if edge.entry), None
        )
        host = resolve_host_ref(session.world_state, root.host_identity.hostname)
        host = (
            previous.destination_host
            if previous
            else host or root.host_identity.hostname
        )
        address = ensure_host_address(session.world_state, host)
        source_ip = ingress.source.ip or "198.51.100.23"
        try:
            ipaddress.ip_address(source_ip)
        except ValueError:
            source_ip = "198.51.100.23"
        if (
            ipaddress.ip_address(source_ip).version
            != ipaddress.ip_address(address).version
        ):
            source_ip = "2001:db8::23" if ":" in address else "198.51.100.23"
        edge = SimulatedConnection(
            id=previous.id if previous else uuid4().hex,
            source_host=None,
            destination_host=host,
            source=ConnectionEndpoint(
                ip=source_ip,
                port=_port(ingress.source.port) or _ephemeral_port(state, source_ip),
            ),
            destination=ConnectionEndpoint(ip=address, port=22),
            entry=True,
        )
        state.connections[edge.id] = edge
        if previous is None:
            root_binding = state.frames[0]
            state.frames[0] = FrameConnectionBinding(
                root_binding.login_connection_id or edge.id,
                root_binding.owned_connection_id or edge.id,
                (*root_binding.retained_connection_ids, edge.id)
                if root_binding.owned_connection_id
                else root_binding.retained_connection_ids,
            )
            if root_binding.login_connection_id is None:
                _seed_login_environment(session, 0, edge)
        for index in range(len(state.frames)):
            binding = state.frames[index]
            if previous and binding.login_connection_id == previous.id:
                # Preserve deliberate unset/override and sudo environment clearing.
                variables = session.shell_stack[index].shell_environment.variables
                if (
                    variables.get("SSH_CONNECTION")
                    == _ssh_variables(previous)["SSH_CONNECTION"]
                ):
                    _seed_login_environment(session, index, edge)
        _event(session, "entry_bound", edge)
    if legacy and restore:
        _restore_legacy_frames(session)
    _prune(session)
    _sync_active_environment(session)


def reconcile_session_connections(
    session: SessionState,
    previous_stack: tuple[ShellFrame, ...],
    *,
    command: str | None = None,
    transition: ShellStackTransition | None = None,
) -> None:
    """Apply socket effects only after an accepted shell-state transition."""
    state = session.network_connections
    before = deepcopy(state)
    previous_depth = len(previous_stack)
    next_depth = len(session.shell_stack)
    while len(state.frames) < previous_depth:
        inherited = state.frames[-1].login_connection_id if state.frames else None
        state.frames.append(FrameConnectionBinding(inherited))
    if not previous_stack or not session.shell_stack:
        state.frames = state.frames[:next_depth]
        _prune(session)
        return
    old_frame = previous_stack[-1]
    frame = session.shell_stack[-1]
    old_binding = state.frames[previous_depth - 1]
    state.frames = state.frames[:next_depth]
    while len(state.frames) < next_depth:
        state.frames.append(FrameConnectionBinding(old_binding.login_connection_id))

    # A pop cannot open a connection. Parent ownership survives local subshells.
    if next_depth < previous_depth:
        _prune(session)
    else:
        pending = state.connections.get(old_binding.owned_connection_id or "")
        was_client = _is_ssh_client(old_frame)
        is_client = _is_ssh_client(frame)
        request = _transition_request(transition)
        if not was_client:
            parsed_request = _request_from_command(command, old_frame)
            if parsed_request is not None:
                request = SSHRequest(
                    parsed_request.target,
                    parsed_request.port or (request.port if request else None),
                )
        if request is None and is_client:
            request = _request_from_client(frame)
        if request is None and was_client:
            request = _request_from_client(old_frame)

        if pending is not None and pending.phase == "authenticating" and was_client:
            if next_depth == previous_depth and not is_client:
                # Authentication completes in the same owning frame.
                destination, address = resolve_target(
                    session.world_state,
                    pending.destination.ip or pending.destination_host,
                    hostname=frame.host_identity.hostname,
                )
                edge = replace(
                    pending,
                    destination_host=destination,
                    destination=replace(pending.destination, ip=address),
                    phase="authenticated",
                )
                state.connections[edge.id] = edge
                state.frames[-1] = FrameConnectionBinding(
                    edge.id, edge.id, old_binding.retained_connection_ids
                )
                _align_remote_frame(session, edge.destination_host)
                _seed_login_environment(session, next_depth - 1, edge)
                _event(session, "authenticated", edge)
        elif request is not None and (
            next_depth > previous_depth
            or (was_client and next_depth == previous_depth)
            or (transition is not None and transition.operation == "replace_active")
            or (next_depth == previous_depth and frame != old_frame)
        ):
            # Passphrase prompts can precede any network connection. Create only
            # after the client reaches the peer, or a remote shell is accepted.
            if not is_client or not _local_authentication_prompt(frame):
                source_ref = (
                    resolve_host_ref(
                        session.world_state, old_frame.host_identity.hostname
                    )
                    or old_frame.host_identity.hostname
                )
                edge = _new_hop(
                    session,
                    source_ref,
                    request,
                    hostname=None if is_client else frame.host_identity.hostname,
                    phase="authenticating" if is_client else "authenticated",
                )
                retained: tuple[str, ...] = ()
                if next_depth == previous_depth:
                    retained = old_binding.retained_connection_ids
                    if old_binding.owned_connection_id:
                        retained = (*retained, old_binding.owned_connection_id)
                state.frames[-1] = FrameConnectionBinding(
                    old_binding.login_connection_id if is_client else edge.id,
                    edge.id,
                    retained,
                )
                if not is_client:
                    _align_remote_frame(session, edge.destination_host)
                    _seed_login_environment(session, next_depth - 1, edge)
        elif not was_client and not is_client:
            _preserve_host_rename(session, old_frame, frame, command, transition)
        _prune(session)
    if state != before:
        session.force_session_state_next_command = True
    _sync_active_environment(session)


def projected_environment_metadata(session: SessionState) -> EnvironmentMetadata:
    """Give the emulator only the active host's fictional socket perspective."""
    state = session.network_connections
    host = resolve_host_ref(session.world_state, session.host_identity.hostname)
    host = host or active_host_ref(session.world_state)
    binding = state.frames[-1] if state.frames else FrameConnectionBinding()
    login = state.connections.get(binding.login_connection_id or "")
    if login is not None and login.destination_host != host:
        login = None
    rows: list[dict[str, object]] = []
    listeners: list[dict[str, object]] = []
    seen_listeners: set[tuple[str | None, int | None]] = set()
    for edge in state.connections.values():
        if edge.source_host == host:
            rows.append(
                _socket_row(
                    edge, "outbound", edge.source, edge.source_peer or edge.destination
                )
            )
        if edge.destination_host == host:
            rows.append(
                _socket_row(
                    edge,
                    "inbound",
                    edge.destination,
                    edge.destination_peer or edge.source,
                )
            )
            endpoint = (edge.destination.ip, edge.destination.port)
            if endpoint not in seen_listeners:
                seen_listeners.add(endpoint)
                listeners.append(
                    {
                        "protocol": "tcp",
                        "service": "ssh",
                        "local": _endpoint(edge.destination),
                    }
                )
    metadata = deepcopy(session.environment_metadata.metadata)
    metadata["active_network"] = {
        "host": host,
        "authoritative_session_connections": True,
        "connections": rows,
        "listeners": listeners,
    }
    return EnvironmentMetadata(
        connection=(
            ConnectionMetadata(
                protocol="ssh",
                source=login.destination_peer or login.source,
                destination=login.destination,
                metadata={"id": login.id, "host": host},
            )
            if login is not None
            else None
        ),
        metadata=metadata,
    )


def close_session_connections(session: SessionState) -> None:
    for edge in session.network_connections.connections.values():
        _event(session, "closed", edge)
    session.network_connections = SessionConnections()
    session.force_session_state_next_command = True


def _new_hop(
    session: SessionState,
    source_host: str,
    request: SSHRequest,
    *,
    hostname: str | None,
    phase: str,
) -> SimulatedConnection:
    translation = translated_ssh_target(
        session.world_state, source_host, request.target, request.port
    )
    target = request.target
    destination_name = hostname
    if translation is not None:
        translated_destination = translation.get("destination", {})
        destination_name = str(translation["destination_host"])
        target = str(translated_destination.get("ip") or destination_name)
    destination_host, destination_ip = resolve_target(
        session.world_state, target, hostname=destination_name
    )
    destination_port = request.port or _host_ssh_port(session, destination_host)
    source_peer = None
    destination_peer = None
    route_target = destination_ip
    if translation is not None:
        route_target = str(translation.get("target_address") or destination_ip)
        source_peer = ConnectionEndpoint(
            ip=route_target,
            port=request.port
            or _port(translation.get("target_port"))
            or destination_port,
        )
        destination_port = (
            _port(translation.get("destination", {}).get("port")) or destination_port
        )
    source_ip = source_address_for_target(
        session.world_state, source_host, route_target
    )
    source_port = _ephemeral_port(session.network_connections, source_ip)
    if translation is not None and isinstance(translation.get("source"), dict):
        translated_source = translation["source"]
        destination_peer = ConnectionEndpoint(
            ip=str(translated_source["ip"]),
            port=_port(translated_source.get("port")) or source_port,
        )
    edge = SimulatedConnection(
        id=uuid4().hex,
        source_host=source_host,
        destination_host=destination_host,
        source=ConnectionEndpoint(
            ip=source_ip,
            port=source_port,
        ),
        destination=ConnectionEndpoint(
            ip=destination_ip,
            port=destination_port,
        ),
        phase="authenticating" if phase == "authenticating" else "authenticated",
        source_peer=source_peer,
        destination_peer=destination_peer,
    )
    session.network_connections.connections[edge.id] = edge
    _event(session, "opened", edge)
    return edge


def _restore_legacy_frames(session: SessionState) -> None:
    state = session.network_connections
    for index in range(1, len(session.shell_stack)):
        parent, frame = session.shell_stack[index - 1 : index + 1]
        parent_binding = state.frames[index - 1]
        source = resolve_host_ref(session.world_state, parent.host_identity.hostname)
        destination = resolve_host_ref(
            session.world_state, frame.host_identity.hostname
        )
        state.frames[index] = FrameConnectionBinding(parent_binding.login_connection_id)
        request = _request_from_client(frame) if _is_ssh_client(frame) else None
        remote = source != destination and parent.host_identity != frame.host_identity
        if request is None and remote:
            request = SSHRequest(frame.host_identity.hostname)
        if request is None or (
            _is_ssh_client(frame) and _local_authentication_prompt(frame)
        ):
            continue
        edge = _new_hop(
            session,
            source or parent.host_identity.hostname,
            request,
            hostname=None if _is_ssh_client(frame) else frame.host_identity.hostname,
            phase="authenticating" if _is_ssh_client(frame) else "authenticated",
        )
        state.frames[index] = FrameConnectionBinding(
            parent_binding.login_connection_id if _is_ssh_client(frame) else edge.id,
            edge.id,
        )
        if not _is_ssh_client(frame):
            _seed_login_environment(session, index, edge)


def _prune(session: SessionState) -> None:
    state = session.network_connections
    state.frames = state.frames[: len(session.shell_stack)]
    owners = {binding.owned_connection_id for binding in state.frames}
    owners.update(
        connection_id
        for binding in state.frames
        for connection_id in binding.retained_connection_ids
    )
    for connection_id, edge in tuple(state.connections.items()):
        if connection_id not in owners:
            _event(session, "closed", edge)
            del state.connections[connection_id]
    state.frames = [
        FrameConnectionBinding(
            binding.login_connection_id
            if binding.login_connection_id in state.connections
            else None,
            binding.owned_connection_id
            if binding.owned_connection_id in state.connections
            else None,
            tuple(
                connection_id
                for connection_id in binding.retained_connection_ids
                if connection_id in state.connections
            ),
        )
        for binding in state.frames
    ]


def _seed_login_environment(
    session: SessionState, index: int, edge: SimulatedConnection
) -> None:
    frame = session.shell_stack[index]
    values = _ssh_variables(edge)
    session.shell_stack[index] = replace(
        frame,
        shell_environment=ShellEnvironment(
            variables={**frame.shell_environment.variables, **values},
            exported=frame.shell_environment.exported | frozenset(values),
        ),
    )


def _sync_active_environment(session: SessionState) -> None:
    if session.shell_stack:
        session.shell_environment = session.shell_stack[-1].shell_environment


def _align_remote_frame(session: SessionState, host_ref: str) -> None:
    session.world_state["active_host"] = host_ref
    frame = session.shell_stack[-1]
    if resolve_host_ref(session.world_state, frame.host_identity.hostname) == host_ref:
        return
    # A model-provided hostname cannot redirect an established target to another
    # populated host. Use that target's canonical identity when they disagree.
    hostname = hostname_for_host_ref(session.world_state, host_ref)
    identity = replace(frame.host_identity, hostname=hostname)
    environment = replace(
        frame.shell_environment,
        variables={**frame.shell_environment.variables, "HOSTNAME": hostname},
    )
    session.shell_stack[-1] = replace(
        frame, host_identity=identity, shell_environment=environment
    )
    session.host_identity = identity


def _ssh_variables(edge: SimulatedConnection) -> dict[str, str]:
    peer = edge.destination_peer or edge.source
    return {
        "SSH_CLIENT": f"{peer.ip} {peer.port} {edge.destination.port}",
        "SSH_CONNECTION": f"{peer.ip} {peer.port} {edge.destination.ip} {edge.destination.port}",
    }


def _preserve_host_rename(
    session: SessionState,
    old_frame: ShellFrame,
    frame: ShellFrame,
    command: str | None,
    transition: ShellStackTransition | None,
) -> None:
    if old_frame.host_identity == frame.host_identity or (
        transition and transition.operation == "push"
    ):
        return
    # Only explicit hostname commands rename a network identity. Operator host
    # switches and arbitrary host changes must not attach another host's ingress.
    if analyze_command(command or "").kind != "simple":
        return
    try:
        argv = shlex.split(command or "")
    except ValueError:
        return
    if not argv or PurePosixPath(argv[0]).name not in {"hostname", "hostnamectl"}:
        return
    old_ref = resolve_host_ref(session.world_state, old_frame.host_identity.hostname)
    hosts = session.world_state.get("hosts_by_name", {})
    if (
        old_ref is None
        or not isinstance(hosts, dict)
        or not isinstance(hosts.get(old_ref), dict)
    ):
        return
    old_host = hosts[old_ref]
    aliases = old_host.setdefault("aliases", [])
    if isinstance(aliases, list) and old_frame.host_identity.hostname not in aliases:
        aliases.append(old_frame.host_identity.hostname)
    new_name = frame.host_identity.hostname
    created = hosts.get(new_name)
    if new_name != old_ref and created == {"hostname": new_name}:
        del hosts[new_name]
    old_host["hostname"] = new_name
    session.world_state["active_host"] = old_ref
    session.force_session_state_next_command = True


def _is_ssh_client(frame: ShellFrame) -> bool:
    return PurePosixPath(frame.interactive_program.name).name == "ssh"


def _local_authentication_prompt(frame: ShellFrame) -> bool:
    phase = str(frame.interactive_program.metadata.get("phase", "")).lower()
    return "passphrase" in phase or phase in {"local_key_unlock", "key_unlock"}


def _request_from_client(frame: ShellFrame) -> SSHRequest | None:
    metadata = frame.interactive_program.metadata
    target = metadata.get("target_host") or metadata.get("target")
    parsed = _parse_ssh_argv(list(frame.interactive_program.argv))
    if isinstance(target, str) and target.strip():
        return SSHRequest(
            _target_name(target),
            _port(metadata.get("target_port") or metadata.get("port"))
            or (parsed.port if parsed else None),
        )
    return parsed


def _transition_request(transition: ShellStackTransition | None) -> SSHRequest | None:
    if transition is None:
        return None
    value = transition.metadata.get("ssh")
    if not isinstance(value, dict):
        return None
    target = value.get("target")
    if not isinstance(target, str) or not target.strip():
        return None
    return SSHRequest(_target_name(target), _port(value.get("port")))


def _request_from_command(command: str | None, frame: ShellFrame) -> SSHRequest | None:
    # Inputs to Python, mysql, etc. are never interpreted as shell SSH commands.
    if PurePosixPath(frame.interactive_program.name).name not in {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "csh",
        "tcsh",
    }:
        return None
    # An aggregate result may describe a later SSH command or a skipped branch.
    # Its transition metadata (or an ordered effect's source) owns attribution.
    if analyze_command(command or "").kind != "simple":
        return None
    try:
        argv = shlex.split(command or "")
    except ValueError:
        return None
    return _parse_ssh_argv(argv)


def _parse_ssh_argv(argv: list[str]) -> SSHRequest | None:
    if argv and argv[0] in {"exec", "command"}:
        argv = argv[1:]
    if not argv or PurePosixPath(argv[0]).name != "ssh":
        return None
    port = None
    index = 1
    takes_value = set("BbcDEeFIiJLlmOopQRSWw")
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-") or token == "-":
            break
        for offset, option in enumerate(token[1:], start=1):
            if option not in takes_value:
                continue
            argument = token[offset + 1 :]
            if not argument:
                index += 1
                if index >= len(argv):
                    return None
                argument = argv[index]
            if option == "p":
                port = _port(argument)
            elif option == "o":
                key, _, value = argument.replace("=", " ", 1).partition(" ")
                if key.lower() == "port":
                    port = _port(value.strip())
            break
        index += 1
    if index >= len(argv):
        return None
    target = argv[index]
    if target.startswith("ssh://"):
        from urllib.parse import urlsplit

        try:
            parsed = urlsplit(target)
            return SSHRequest(parsed.hostname or "", port or parsed.port)
        except ValueError:
            return None
    return SSHRequest(_target_name(target), port)


def _target_name(value: str) -> str:
    return value.rsplit("@", 1)[-1].strip().strip("[]")


def _host_ssh_port(session: SessionState, host_ref: str) -> int:
    hosts = session.world_state.get("hosts_by_name", {})
    host = hosts.get(host_ref, {}) if isinstance(hosts, dict) else {}
    if not isinstance(host, dict):
        return 22
    network = host.get("network", {})
    listeners = network.get("listening_ports", []) if isinstance(network, dict) else []
    services = host.get("services_by_name", {})
    candidates = (
        list(listeners.values())
        if isinstance(listeners, dict)
        else list(listeners)
        if isinstance(listeners, list)
        else []
    )
    if isinstance(services, dict):
        for name, value in services.items():
            if isinstance(value, dict):
                candidates.append({"name": name, **value})
    for item in candidates:
        if isinstance(item, dict) and (item.get("service") or item.get("name")) in {
            "ssh",
            "sshd",
            "openssh",
        }:
            port = _port(item.get("port"))
            if port:
                return port
    return 22


def _port(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        port = int(value)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _ephemeral_port(state: SessionConnections, address: str) -> int:
    used = {
        edge.source.port
        for edge in state.connections.values()
        if edge.source.ip == address
    }
    initial = 49152 + int(uuid4().hex[:4], 16) % 16384
    for offset in range(16384):
        candidate = 49152 + (initial - 49152 + offset) % 16384
        if candidate not in used:
            return candidate
    raise ValueError("simulated SSH source port capacity exhausted")


def _endpoint(endpoint: ConnectionEndpoint) -> dict[str, object]:
    return {"ip": endpoint.ip, "port": endpoint.port}


def _socket_row(
    edge: SimulatedConnection,
    direction: str,
    local: ConnectionEndpoint,
    peer: ConnectionEndpoint,
) -> dict[str, object]:
    return {
        "id": edge.id,
        "protocol": "tcp",
        "service": "ssh",
        "state": "ESTAB",
        "direction": direction,
        "local": _endpoint(local),
        "peer": _endpoint(peer),
        "phase": edge.phase,
    }


def _event(session: SessionState, event: str, edge: SimulatedConnection) -> None:
    from dreamland.runtime_logging import trace_network_connection_event

    if _PROVISIONAL_EVENTS.get():
        event = f"proposed_{event}"
    LOGGER.info(
        "Simulated connection event=%s session=%s connection=%s source_host=%s destination_host=%s phase=%s",
        event,
        session.langfuse_trace_id,
        edge.id,
        edge.source_host,
        edge.destination_host,
        edge.phase,
    )
    trace_network_connection_event(
        session.langfuse_trace_id,
        event=event,
        connection_id=edge.id,
        source_host=edge.source_host,
        destination_host=edge.destination_host,
        source=_endpoint(edge.source),
        destination=_endpoint(edge.destination),
        phase=edge.phase,
        peer_overrides={
            "source_peer": _endpoint(edge.source_peer) if edge.source_peer else None,
            "destination_peer": _endpoint(edge.destination_peer)
            if edge.destination_peer
            else None,
        },
    )
