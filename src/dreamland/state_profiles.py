from __future__ import annotations

import json
import re
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Literal

from dreamland.agent_state import (
    environment_metadata_from_state,
    environment_metadata_to_state,
    host_identity_from_state,
    host_identity_to_state,
    interactive_program_from_state,
    interactive_program_to_state,
    shell_environment_from_state,
    shell_environment_to_state,
    shell_frame_from_state,
    shell_frame_to_state,
)
from dreamland.connection_codec import (
    network_connections_from_state,
    network_connections_to_state,
)
from dreamland.lobby_identity import PUBLIC_ID_METADATA_KEY
from dreamland.login_banner import validate_profile_login_banners
from dreamland.models import (
    CommandHistoryEntry,
    ConnectionMetadata,
    EnvironmentMetadata,
    SessionConnections,
    SessionState,
    ShellEnvironment,
    default_shell_environment,
    default_shell_frame,
    normalize_command_history_streams,
)
from dreamland.network_connections import initialize_session_connections
from dreamland.world_state import (
    active_host_ref,
    default_interactive_program_from_world_state,
    host_metadata_for_ref,
    hostname_for_host_ref,
    sync_active_host,
)

STATE_PROFILE_KIND = "dreamland.state_profile"
STATE_PROFILE_SCHEMA_VERSION = 2
STATE_PROFILE_SESSION_INDEX_KIND = "dreamland.state_profile_session_index"
STATE_PROFILE_SESSION_INDEX_SCHEMA_VERSION = 1
DEFAULT_STATE_PROFILE_DIR = Path("state_profiles")
DEFAULT_SESSION_INDEX_NAME = "session_index.json"
DEFAULT_STATE_PROFILE_NAME = "default"
STATE_PROFILE_LEVELS = ("exact", "system", "world")
STATE_PROFILE_METADATA_NAME = "state_profile.name"
STATE_PROFILE_METADATA_LEVEL = "state_profile.level"
StateProfileLevel = Literal["exact", "system", "world"]
_PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class StateProfile:
    name: str
    level: StateProfileLevel
    state: dict[str, object]
    created_at: str
    source_session_id: str | None = None
    comment: str | None = None

    def to_document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "kind": STATE_PROFILE_KIND,
            "schema_version": STATE_PROFILE_SCHEMA_VERSION,
            "name": self.name,
            "level": self.level,
            "created_at": self.created_at,
            "state": deepcopy(self.state),
        }
        if self.source_session_id is not None:
            document["source_session_id"] = self.source_session_id
        if self.comment:
            document["comment"] = self.comment
        return document


@dataclass(frozen=True, slots=True)
class StateProfileSummary:
    name: str
    level: StateProfileLevel
    created_at: str
    source_session_id: str | None
    comment: str | None
    path: Path


@dataclass(frozen=True, slots=True)
class StateProfileSessionRecord:
    session_id: str
    state_profile: str
    level: str
    source: str
    updated_at: str

    @property
    def label(self) -> str:
        return f"{self.state_profile}:{self.level}"


class StateProfileError(ValueError):
    pass


class StateProfileStore:
    def __init__(self, directory: Path | str = DEFAULT_STATE_PROFILE_DIR) -> None:
        self.directory = Path(directory)
        self._session_index_lock = threading.RLock()

    def save(self, profile: StateProfile) -> Path:
        validate_state_profile_name(profile.name)
        _validate_profile_state(profile.state)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(profile.name)
        temporary_path = path.with_suffix(".json.tmp")
        text = json.dumps(profile.to_document(), indent=2, sort_keys=True)
        temporary_path.write_text(f"{text}\n", encoding="utf-8")
        temporary_path.replace(path)
        return path

    def load(self, name: str) -> StateProfile:
        validate_state_profile_name(name)
        path = self.path_for(name)
        try:
            document = json.loads(self._profile_text(name, path))
        except json.JSONDecodeError as exc:
            raise StateProfileError(
                f"state profile {name!r} is not valid JSON: {exc.msg}"
            ) from exc
        profile = state_profile_from_document(document)
        if profile.name != name:
            raise StateProfileError(
                f"state profile file {path} contains name {profile.name!r}"
            )
        return profile

    def _profile_text(self, name: str, path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        resource = files("dreamland.default_state_profiles").joinpath(f"{name}.json")
        if resource.is_file():
            return resource.read_text(encoding="utf-8")
        raise StateProfileError(f"state profile {name!r} does not exist")

    def delete(self, name: str) -> Path:
        validate_state_profile_name(name)
        path = self.path_for(name)
        if not path.exists():
            raise StateProfileError(f"state profile {name!r} does not exist")
        path.unlink()
        return path

    def list(self) -> list[StateProfileSummary]:
        if not self.directory.exists():
            return []
        summaries: list[StateProfileSummary] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                profile = state_profile_from_document(document)
            except (OSError, StateProfileError, json.JSONDecodeError):
                continue
            summaries.append(
                StateProfileSummary(
                    name=profile.name,
                    level=profile.level,
                    created_at=profile.created_at,
                    source_session_id=profile.source_session_id,
                    comment=profile.comment,
                    path=path,
                )
            )
        return sorted(summaries, key=lambda summary: summary.created_at)

    def path_for(self, name: str) -> Path:
        validate_state_profile_name(name)
        return self.directory / f"{name}.json"

    def record_session_profile(
        self,
        *,
        session_id: str,
        profile: StateProfile,
        source: str,
        updated_at: str | None = None,
    ) -> StateProfileSessionRecord:
        if not session_id:
            raise StateProfileError("session id is required")
        record = StateProfileSessionRecord(
            session_id=session_id,
            state_profile=profile.name,
            level=profile.level,
            source=source,
            updated_at=updated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        )
        with self._session_index_lock:
            records = self._load_session_index_records()
            records[session_id] = record
            self._write_session_index_records(records)
        return record

    def session_profile(self, session_id: str) -> StateProfileSessionRecord | None:
        with self._session_index_lock:
            return self._load_session_index_records().get(session_id)

    @property
    def _session_index_path(self) -> Path:
        return self.directory / DEFAULT_SESSION_INDEX_NAME

    def _load_session_index_records(self) -> dict[str, StateProfileSessionRecord]:
        path = self._session_index_path
        if not path.exists():
            return {}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateProfileError(
                f"state profile session index is not valid JSON: {exc.msg}"
            ) from exc
        if not isinstance(document, dict):
            raise StateProfileError("state profile session index must be an object")
        if document.get("kind") != STATE_PROFILE_SESSION_INDEX_KIND:
            raise StateProfileError("state profile session index kind is invalid")
        if document.get("schema_version") != STATE_PROFILE_SESSION_INDEX_SCHEMA_VERSION:
            raise StateProfileError(
                "state profile session index schema version is unsupported"
            )
        sessions = document.get("sessions")
        if not isinstance(sessions, dict):
            return {}
        records: dict[str, StateProfileSessionRecord] = {}
        for session_id, value in sessions.items():
            if not isinstance(session_id, str) or not isinstance(value, dict):
                continue
            state_profile = value.get("state_profile")
            level = value.get("level")
            source = value.get("source")
            updated_at = value.get("updated_at")
            if not isinstance(state_profile, str) or not state_profile:
                continue
            if not isinstance(level, str) or not level:
                continue
            if not isinstance(source, str) or not source:
                continue
            if not isinstance(updated_at, str) or not updated_at:
                continue
            records[session_id] = StateProfileSessionRecord(
                session_id=session_id,
                state_profile=state_profile,
                level=level,
                source=source,
                updated_at=updated_at,
            )
        return records

    def _write_session_index_records(
        self,
        records: dict[str, StateProfileSessionRecord],
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        document = {
            "kind": STATE_PROFILE_SESSION_INDEX_KIND,
            "schema_version": STATE_PROFILE_SESSION_INDEX_SCHEMA_VERSION,
            "sessions": {
                session_id: {
                    "state_profile": record.state_profile,
                    "level": record.level,
                    "source": record.source,
                    "updated_at": record.updated_at,
                }
                for session_id, record in sorted(records.items())
            },
        }
        text = json.dumps(document, indent=2, sort_keys=True)
        path = self._session_index_path
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(f"{text}\n", encoding="utf-8")
        temporary_path.replace(path)


class StateProfileRuntime:
    def __init__(
        self,
        *,
        store: StateProfileStore | None = None,
        current_profile: StateProfile | None = None,
    ) -> None:
        self.store = store or StateProfileStore()
        self._current_profile = current_profile
        self._lock = threading.RLock()

    def current_profile(self) -> StateProfile | None:
        with self._lock:
            return self._current_profile

    def required_current_profile(self) -> StateProfile:
        profile = self.current_profile()
        if profile is not None:
            return profile
        return self.store.load(DEFAULT_STATE_PROFILE_NAME)

    def set_default(self, name: str) -> StateProfile:
        profile = self.store.load(name)
        with self._lock:
            self._current_profile = profile
        return profile

    def clear_default(self) -> StateProfile | None:
        with self._lock:
            previous = self._current_profile
            self._current_profile = None
        return previous

    def delete_profile(self, name: str) -> tuple[Path, bool]:
        path = self.store.delete(name)
        cleared_default = False
        with self._lock:
            if self._current_profile is not None and self._current_profile.name == name:
                self._current_profile = None
                cleared_default = True
        return path, cleared_default


def create_state_profile(
    session: SessionState,
    *,
    name: str,
    level: StateProfileLevel,
    comment: str | None = None,
    created_at: str | None = None,
) -> StateProfile:
    validate_state_profile_name(name)
    if level not in STATE_PROFILE_LEVELS:
        raise StateProfileError(f"unsupported state profile level: {level}")
    state = session_state_for_level(session, level)
    _validate_profile_state(state)
    return StateProfile(
        name=name,
        level=level,
        state=state,
        created_at=created_at or datetime.now(UTC).isoformat(timespec="seconds"),
        source_session_id=session.langfuse_trace_id,
        comment=comment,
    )


def session_state_for_level(
    session: SessionState,
    level: StateProfileLevel,
) -> dict[str, object]:
    if level == "exact":
        return _exact_state(session)
    if level == "system":
        return _system_state(session)
    if level == "world":
        return _world_state(session)
    raise StateProfileError(f"unsupported state profile level: {level}")


def state_profile_from_document(document: object) -> StateProfile:
    if not isinstance(document, dict):
        raise StateProfileError("state profile must be a JSON object")
    if document.get("kind") != STATE_PROFILE_KIND:
        raise StateProfileError("state profile kind is invalid")
    if document.get("schema_version") != STATE_PROFILE_SCHEMA_VERSION:
        raise StateProfileError("state profile schema version is unsupported")
    name = document.get("name")
    level = document.get("level")
    created_at = document.get("created_at")
    state = document.get("state")
    if not isinstance(name, str):
        raise StateProfileError("state profile name is required")
    validate_state_profile_name(name)
    if level not in STATE_PROFILE_LEVELS:
        raise StateProfileError("state profile level is invalid")
    if not isinstance(created_at, str) or not created_at:
        raise StateProfileError("state profile created_at is required")
    if not isinstance(state, dict):
        raise StateProfileError("state profile state must be an object")
    _validate_profile_state(state)
    source_session_id = document.get("source_session_id")
    comment = document.get("comment")
    return StateProfile(
        name=name,
        level=level,
        state=deepcopy(state),
        created_at=created_at,
        source_session_id=(
            str(source_session_id) if source_session_id is not None else None
        ),
        comment=str(comment) if comment else None,
    )


def validate_state_profile_name(name: str) -> None:
    if not _PROFILE_NAME_PATTERN.match(name):
        raise StateProfileError(
            "state profile names must start with a letter or number and contain "
            "only letters, numbers, dots, dashes, and underscores"
        )


def _validate_profile_state(state: dict[str, object]) -> None:
    try:
        validate_profile_login_banners(state)
    except ValueError as exc:
        raise StateProfileError(str(exc)) from exc


def state_profile_to_session(
    profile: StateProfile,
    *,
    current_session_id: str,
    fallback_username: str = "guest",
    connection_metadata: ConnectionMetadata | None = None,
    authenticated_username: str | None = None,
) -> SessionState:
    _validate_profile_state(profile.state)
    state = deepcopy(profile.state)
    connection_username = str(authenticated_username or "").strip()
    use_connection_username = bool(
        connection_username and profile.level in {"system", "world"}
    )
    if use_connection_username:
        username = connection_username
        login_username = connection_username
    else:
        username = str(
            state.get("username") or state.get("login_username") or fallback_username
        )
        login_username = (
            str(state["login_username"])
            if state.get("login_username") is not None
            else username
        )
    if use_connection_username:
        state = _expand_profile_templates(state, {"username": username})
    saved_world_state = state.get("world_state")
    world_state = (
        deepcopy(saved_world_state) if isinstance(saved_world_state, dict) else {}
    )
    world_state = _normalize_world_state_command_history(world_state)
    default_hostname = hostname_for_host_ref(world_state, active_host_ref(world_state))
    host_identity = host_identity_from_state(
        state.get("host_identity"),
        default_hostname=default_hostname,
    )
    cwd = str(state.get("cwd") or _home_for(username))
    interactive_program = (
        interactive_program_from_state(state.get("interactive_program"))
        if isinstance(state.get("interactive_program"), dict)
        else default_interactive_program_from_world_state(world_state)
    )
    shell_environment = (
        shell_environment_from_state(state.get("shell_environment"))
        if isinstance(state.get("shell_environment"), dict)
        else _default_shell_environment_from_world_state(
            username=username,
            hostname=host_identity.hostname,
            cwd=cwd,
            shell=interactive_program.command,
            world_state=world_state,
        )
    )
    shell_stack_value = state.get("shell_stack")
    shell_stack = (
        [shell_frame_from_state(item) for item in shell_stack_value]
        if isinstance(shell_stack_value, list) and shell_stack_value
        else []
    )
    if shell_stack:
        active_frame = shell_stack[-1]
        username = active_frame.username
        host_identity = active_frame.host_identity
        cwd = active_frame.cwd
        interactive_program = active_frame.interactive_program
        shell_environment = active_frame.shell_environment
    else:
        shell_stack = [
            default_shell_frame(
                username=username,
                hostname=host_identity.hostname,
                cwd=cwd,
                interactive_program=interactive_program,
                shell_environment=shell_environment,
            )
        ]
    history_value = state.get("history")
    history = (
        [_history_entry_from_state(item) for item in history_value]
        if isinstance(history_value, list)
        else []
    )
    environment_metadata = environment_metadata_from_state(
        state.get("environment_metadata")
    )
    metadata = _without_visit_metadata(environment_metadata.metadata)
    metadata[STATE_PROFILE_METADATA_NAME] = profile.name
    metadata[STATE_PROFILE_METADATA_LEVEL] = profile.level
    if connection_metadata is not None:
        environment_metadata = EnvironmentMetadata(
            connection=connection_metadata,
            metadata=metadata,
        )
    else:
        environment_metadata = EnvironmentMetadata(
            connection=environment_metadata.connection,
            metadata=metadata,
        )
    session = SessionState(
        username=username,
        login_username=login_username,
        host_identity=host_identity,
        cwd=cwd,
        interactive_program=interactive_program,
        shell_environment=shell_environment,
        shell_stack=shell_stack,
        langfuse_trace_id=current_session_id,
        history=history,
        environment_metadata=environment_metadata,
        world_state=sync_active_host(world_state, host_identity.hostname),
        force_session_state_next_command=True,
        network_connections=(
            network_connections_from_state(
                state.get("network_connections"), frame_count=len(shell_stack)
            )
            if profile.level == "exact" and "network_connections" in state
            else SessionConnections()
        ),
    )
    initialize_session_connections(
        session,
        ingress=connection_metadata,
        restore=profile.level == "exact",
    )
    # Exact shell frames or system host_identity may select a different host.
    # Validate the actual initial shell as well as the saved world references.
    _validate_profile_state({"world_state": session.world_state})
    return session


def apply_state_profile_to_session(
    session: SessionState,
    profile: StateProfile,
) -> None:
    replacement = state_profile_to_session(
        profile,
        current_session_id=session.langfuse_trace_id,
        authenticated_username=session.login_username or session.username,
        connection_metadata=(
            session.environment_metadata.connection
            if any(
                edge.entry for edge in session.network_connections.connections.values()
            )
            else None
        ),
    )
    # A restore owns simulated state, never the live connection's provenance.
    replacement.login_username = session.login_username or session.username
    for key in tuple(replacement.environment_metadata.metadata):
        if key.startswith("dreamland.session."):
            del replacement.environment_metadata.metadata[key]
    replacement.environment_metadata.metadata.update(
        deepcopy(
            {
                key: value
                for key, value in session.environment_metadata.metadata.items()
                if key.startswith("dreamland.session.")
            }
        )
    )
    session.username = replacement.username
    session.login_username = replacement.login_username
    session.host_identity = replacement.host_identity
    session.cwd = replacement.cwd
    session.interactive_program = replacement.interactive_program
    session.shell_environment = replacement.shell_environment
    session.shell_stack = replacement.shell_stack
    session.history = replacement.history
    session.environment_metadata = EnvironmentMetadata(
        connection=session.environment_metadata.connection,
        metadata=replacement.environment_metadata.metadata,
    )
    session.world_state = replacement.world_state
    session.network_connections = replacement.network_connections
    session.force_session_state_next_command = True


def _exact_state(session: SessionState) -> dict[str, object]:
    state = _system_state(session)
    state.update(
        {
            "username": session.username,
            "cwd": session.cwd,
            "interactive_program": interactive_program_to_state(
                session.interactive_program
            ),
            "shell_environment": shell_environment_to_state(session.shell_environment),
            "shell_stack": [
                shell_frame_to_state(frame) for frame in session.shell_stack
            ],
            "history": [_history_entry_to_state(entry) for entry in session.history],
            "network_connections": network_connections_to_state(
                session.network_connections
            ),
        }
    )
    return state


def _system_state(session: SessionState) -> dict[str, object]:
    state: dict[str, object] = {
        "world_state": _normalize_world_state_command_history(session.world_state)
    }
    login_username = session.login_username or session.username
    state.update(
        {
            "username": login_username,
            "login_username": login_username,
            "host_identity": host_identity_to_state(session.host_identity),
            "environment_metadata": environment_metadata_to_state(
                EnvironmentMetadata(
                    connection=session.environment_metadata.connection,
                    metadata=_without_visit_metadata(
                        session.environment_metadata.metadata
                    ),
                )
            ),
        }
    )
    return state


def _without_visit_metadata(metadata: dict[str, object]) -> dict[str, object]:
    # These names are runtime-only projections, never authoritative profile data.
    return {
        key: value
        for key, value in metadata.items()
        if key != PUBLIC_ID_METADATA_KEY and not key.startswith("dreamland.lobby.")
    }


def _world_state(session: SessionState) -> dict[str, object]:
    return {"world_state": _project_world_state(session.world_state)}


def _project_world_state(world_state: dict[str, object]) -> dict[str, object]:
    world_state = _normalize_world_state_command_history(world_state)
    projected: dict[str, object] = {}
    for key in (
        "name",
        "entry_host",
        "active_host",
        "purpose",
        "simulation",
        "generation_policy",
        "expansion_directives_by_id",
        "world_facts_by_id",
        "hosts_by_name",
    ):
        if key in world_state:
            projected[key] = deepcopy(world_state[key])
    facts = world_state.get("world_facts_by_id")
    if isinstance(facts, dict):
        projected["world_facts_by_id"] = _project_world_facts(facts)
    return projected


def _normalize_world_state_command_history(
    world_state: dict[str, object],
) -> dict[str, object]:
    normalized = deepcopy(world_state)
    hosts = normalized.get("hosts_by_name")
    if not isinstance(hosts, dict):
        return normalized
    for host in hosts.values():
        if not isinstance(host, dict):
            continue
        history = host.get("command_history")
        if not isinstance(history, list):
            continue
        for entry in history:
            if not isinstance(entry, dict):
                continue
            stdout, stderr = normalize_command_history_streams(
                str(entry.get("stdout", "")),
                str(entry.get("stderr", "")),
            )
            entry["stdout"] = stdout
            entry["stderr"] = stderr
    return normalized


def _project_world_facts(facts: dict[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {}
    high_level_words = (
        "description",
        "environment",
        "host",
        "network",
        "service",
        "system",
        "topology",
        "world",
    )
    for key, value in facts.items():
        if any(word in key.lower() for word in high_level_words):
            projected[key] = deepcopy(value)
            continue
        if isinstance(value, dict):
            category = str(
                value.get("category") or value.get("scope") or value.get("type") or ""
            ).lower()
            if category in high_level_words:
                projected[key] = deepcopy(value)
    return projected


def _history_entry_to_state(entry: CommandHistoryEntry) -> dict[str, object]:
    stdout, stderr = normalize_command_history_streams(entry.stdout, entry.stderr)
    return {
        "id": entry.id,
        "cwd": entry.cwd,
        "command": entry.command,
        "stdout": stdout,
        "stderr": stderr,
        "exit_status": entry.exit_status,
        "interrupted": entry.interrupted,
        "signal": entry.signal,
    }


def _history_entry_from_state(value: object) -> CommandHistoryEntry:
    if not isinstance(value, dict):
        return CommandHistoryEntry(cwd="", command="")
    stdout, stderr = normalize_command_history_streams(
        str(value.get("stdout", "")),
        str(value.get("stderr", "")),
    )
    return CommandHistoryEntry(
        id=str(value["id"]) if value.get("id") is not None else "",
        cwd=str(value.get("cwd", "")),
        command=str(value.get("command", "")),
        stdout=stdout,
        stderr=stderr,
        exit_status=_coerce_int(value.get("exit_status")),
        interrupted=bool(value.get("interrupted", False)),
        signal=str(value["signal"]) if value.get("signal") is not None else None,
    )


def _coerce_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (str, int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _home_for(username: str) -> str:
    return "/root" if username == "root" else f"/home/{username or 'guest'}"


def _default_shell_environment_from_world_state(
    *,
    username: str,
    hostname: str,
    cwd: str,
    shell: str,
    world_state: dict[str, object],
) -> ShellEnvironment:
    environment = default_shell_environment(
        username=username,
        hostname=hostname,
        cwd=cwd,
        shell=shell,
    )
    overrides = _host_environment_overrides(world_state)
    if not overrides:
        return environment
    variables = dict(environment.variables)
    variables.update(overrides)
    exported = set(environment.exported)
    exported.update(overrides)
    return ShellEnvironment(variables=variables, exported=frozenset(exported))


def _host_environment_overrides(world_state: dict[str, object]) -> dict[str, str]:
    host = host_metadata_for_ref(world_state, active_host_ref(world_state))
    environment = host.get("environment")
    if not isinstance(environment, dict):
        return {}
    return {str(key): str(value) for key, value in environment.items()}


def _expand_profile_templates(
    value: object,
    variables: dict[str, str],
) -> dict[str, object]:
    expanded = _expand_template_value(value, variables)
    return expanded if isinstance(expanded, dict) else {}


def _expand_template_value(value: object, variables: dict[str, str]) -> object:
    if isinstance(value, dict):
        return {
            key: _expand_template_value(item, variables) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_expand_template_value(item, variables) for item in value]
    if isinstance(value, str):
        try:
            return value.format_map(_TemplateVariables(variables))
        except ValueError:
            return value
    return value


class _TemplateVariables(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
