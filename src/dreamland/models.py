from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypedDict
from uuid import uuid4

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InteractiveProgram:
    name: str
    command: str
    argv: tuple[str, ...] = ()
    prompt_template: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def default_interactive_program() -> InteractiveProgram:
    return InteractiveProgram(name="bash", command="/bin/bash", argv=("bash",))


@dataclass(frozen=True, slots=True)
class ShellEnvironment:
    variables: dict[str, str] = field(default_factory=dict)
    exported: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class HostIdentity:
    hostname: str = "ubuntu"


def default_shell_environment(
    username: str = "guest",
    hostname: str = "ubuntu",
    cwd: str | None = None,
    shell: str = "/bin/bash",
) -> ShellEnvironment:
    home = "/root" if username == "root" else f"/home/{username or 'guest'}"
    current_directory = cwd or home
    variables = {
        "HOME": home,
        "HOSTNAME": hostname,
        "LANG": "C.UTF-8",
        "LOGNAME": username or "guest",
        "OLDPWD": "",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PWD": current_directory,
        "SHELL": shell,
        "SHLVL": "1",
        "TERM": "xterm-256color",
        "USER": username or "guest",
    }
    return ShellEnvironment(variables=variables, exported=frozenset(variables))


@dataclass(frozen=True, slots=True)
class ShellFrame:
    username: str
    cwd: str
    interactive_program: InteractiveProgram = field(
        default_factory=default_interactive_program
    )
    shell_environment: ShellEnvironment = field(
        default_factory=default_shell_environment
    )
    host_identity: HostIdentity = field(default_factory=HostIdentity)
    metadata: dict[str, object] = field(default_factory=dict)


def default_shell_frame(
    username: str = "guest",
    hostname: str = "ubuntu",
    cwd: str | None = None,
    interactive_program: InteractiveProgram | None = None,
    shell_environment: ShellEnvironment | None = None,
) -> ShellFrame:
    program = interactive_program or default_interactive_program()
    home = "/root" if username == "root" else f"/home/{username or 'guest'}"
    current_directory = cwd or home
    return ShellFrame(
        username=username or "guest",
        cwd=current_directory,
        interactive_program=program,
        shell_environment=(
            shell_environment
            or default_shell_environment(
                username=username,
                hostname=hostname,
                cwd=current_directory,
                shell=program.command,
            )
        ),
        host_identity=HostIdentity(hostname=hostname),
    )


@dataclass(frozen=True, slots=True)
class ShellStackTransition:
    operation: Literal["push", "pop", "replace_active"]
    username: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommandStateUpdate:
    host_identity: HostIdentity | None = None
    cwd: str | None = None
    interactive_program: InteractiveProgram | None = None
    shell_environment: ShellEnvironment | None = None
    shell_stack: tuple[ShellFrame, ...] | None = None
    shell_stack_transition: ShellStackTransition | None = None
    world_state: dict[str, object] | None = None
    effects: tuple[CommandStateEffect, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandStateEffect:
    # Attribution for an accepted state change, never a command to execute.
    command: str
    state_update: CommandStateUpdate


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_status: int = 0
    interrupted: bool = False
    signal: str | None = None
    state_update: CommandStateUpdate = field(default_factory=CommandStateUpdate)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommandOutputChunk:
    text: str
    stream: Literal["stdout", "stderr"] = "stdout"


@dataclass(frozen=True, slots=True)
class CommandStreamResult:
    result: CommandResult


CommandStreamEvent = CommandOutputChunk | CommandStreamResult


class CommandExecutionControl:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._rendered = {"stdout": "", "stderr": ""}
        self._cancel_callbacks: list[Callable[[], None]] = []
        self._recording_claimed = False
        self._graph_recording_claimed = False

    def cancel(self) -> None:
        self._cancelled.set()
        callbacks: list[Callable[[], None]]
        with self._lock:
            callbacks = list(self._cancel_callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                LOGGER.debug("Command cancellation callback failed", exc_info=True)

    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def add_cancel_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._cancel_callbacks.append(callback)
        if self.cancelled():
            try:
                callback()
            except Exception:
                LOGGER.debug("Late command cancellation callback failed", exc_info=True)

    def add_rendered(self, stream: Literal["stdout", "stderr"], text: str) -> None:
        if not text:
            return
        with self._lock:
            self._rendered[stream] += text

    def claim_recording(self) -> bool:
        with self._lock:
            if self._recording_claimed:
                return False
            self._recording_claimed = True
            return True

    def claim_graph_recording(self) -> bool:
        with self._lock:
            if self._graph_recording_claimed:
                return False
            self._graph_recording_claimed = True
            return True

    def rendered_result(self, *, exit_status: int = 130) -> CommandResult:
        with self._lock:
            stdout = self._rendered["stdout"]
            stderr = self._rendered["stderr"]
        return CommandResult(
            stdout=stdout,
            stderr=stderr,
            exit_status=exit_status,
            interrupted=True,
            signal="SIGINT",
        )


@dataclass(frozen=True, slots=True)
class CommandHistoryEntry:
    cwd: str
    command: str
    id: str = field(default_factory=lambda: uuid4().hex[:16])
    stdout: str = ""
    stderr: str = ""
    exit_status: int = 0
    interrupted: bool = False
    signal: str | None = None


def normalize_command_history_streams(
    stdout: str,
    stderr: str,
) -> tuple[str, str]:
    """Collapse an accidentally saved structured result envelope for history."""
    if stderr:
        return stdout, stderr
    text = stdout.strip()
    if not text.startswith("{") or not text.endswith("}"):
        return stdout, stderr
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return stdout, stderr
    if not isinstance(payload, dict):
        return stdout, stderr
    if not {"stdout", "stderr", "exit_status", "state_update"}.issubset(payload):
        return stdout, stderr
    if not isinstance(payload.get("state_update"), dict):
        return stdout, stderr
    return str(payload.get("stdout", "")), str(payload.get("stderr", ""))


@dataclass(frozen=True, slots=True)
class ConnectionEndpoint:
    ip: str | None = None
    port: int | None = None


@dataclass(frozen=True, slots=True)
class ConnectionMetadata:
    protocol: str | None = None
    source: ConnectionEndpoint = field(default_factory=ConnectionEndpoint)
    destination: ConnectionEndpoint = field(default_factory=ConnectionEndpoint)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EnvironmentMetadata:
    connection: ConnectionMetadata | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SimulatedConnection:
    id: str
    source_host: str | None
    destination_host: str
    source: ConnectionEndpoint
    destination: ConnectionEndpoint
    phase: Literal["authenticating", "authenticated"] = "authenticated"
    entry: bool = False
    source_peer: ConnectionEndpoint | None = None
    destination_peer: ConnectionEndpoint | None = None


@dataclass(frozen=True, slots=True)
class FrameConnectionBinding:
    login_connection_id: str | None = None
    owned_connection_id: str | None = None
    retained_connection_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class SessionConnections:
    # Bindings correspond to the authoritative shell stack, never model metadata.
    connections: dict[str, SimulatedConnection] = field(default_factory=dict)
    frames: list[FrameConnectionBinding] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RuntimeProvenance:
    public_id: str
    session_id: str
    profile_name: str
    parent_connection_id: str
    notice_version: str
    accepted_at: str


@dataclass(slots=True)
class SessionState:
    username: str
    login_username: str | None = None
    host_identity: HostIdentity = field(default_factory=HostIdentity)
    cwd: str = "/home/guest"
    interactive_program: InteractiveProgram = field(
        default_factory=default_interactive_program
    )
    shell_environment: ShellEnvironment = field(
        default_factory=default_shell_environment
    )
    shell_stack: list[ShellFrame] = field(default_factory=list)
    langfuse_trace_id: str = field(default_factory=lambda: uuid4().hex)
    history: list[CommandHistoryEntry] = field(default_factory=list)
    environment_metadata: EnvironmentMetadata = field(
        default_factory=EnvironmentMetadata
    )
    world_state: dict[str, object] = field(default_factory=dict)
    force_session_state_next_command: bool = False
    network_connections: SessionConnections = field(default_factory=SessionConnections)
    # Application-owned provenance is deliberately absent from state codecs,
    # model patches, checkpoints, and saved profiles.
    runtime_provenance: RuntimeProvenance | None = None
    # Runtime-only foreground SSH mutation authority. Never projected into
    # model state, checkpoints, or saved profiles; contains paths, not secrets.
    foreground_ssh_directories_by_host: dict[str, set[str]] = field(
        default_factory=dict, repr=False, compare=False
    )
    foreground_ssh_key_paths_by_host: dict[str, set[str]] = field(
        default_factory=dict, repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class CommandContext:
    username: str
    cwd: str
    hostname: str
    command: str
    langfuse_trace_id: str
    command_id: str = field(default_factory=lambda: uuid4().hex[:16])
    login_username: str | None = None
    host_identity: HostIdentity = field(default_factory=HostIdentity)
    interactive_program: InteractiveProgram = field(
        default_factory=default_interactive_program
    )
    shell_environment: ShellEnvironment = field(
        default_factory=default_shell_environment
    )
    shell_stack: tuple[ShellFrame, ...] = ()
    history: tuple[CommandHistoryEntry, ...] = ()
    environment_metadata: EnvironmentMetadata = field(
        default_factory=EnvironmentMetadata
    )
    world_state: dict[str, object] = field(default_factory=dict)
    active_host: str = ""
    active_host_metadata: dict[str, object] = field(default_factory=dict)
    force_session_state: bool = False
    runtime_provenance: RuntimeProvenance | None = None
    # Operator-only ingress provenance; never serialized into emulator state.
    transport_connection: ConnectionMetadata | None = None


class DreamlandCommandState(TypedDict, total=False):
    username: str
    login_username: str
    cwd: str
    hostname: str
    host_identity: dict[str, object]
    current_command: str
    interactive_program: dict[str, object]
    shell_environment: dict[str, object]
    shell_stack: list[dict[str, object]]
    environment_metadata: dict[str, object]
    world_state: dict[str, object]
    active_host: str
    active_host_metadata: dict[str, object]
    langfuse_trace_id: str
    result: dict[str, object]
    response: str
