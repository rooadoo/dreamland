from __future__ import annotations

from copy import deepcopy

from dreamland.models import (
    CommandHistoryEntry,
    CommandStateEffect,
    CommandStateUpdate,
    HostIdentity,
    InteractiveProgram,
    SessionState,
    ShellEnvironment,
    ShellFrame,
    ShellStackTransition,
    default_shell_environment,
    default_shell_frame,
    normalize_command_history_streams,
)
from dreamland.network_connections import reconcile_session_connections
from dreamland.world_state import active_host_ref, sync_active_host

WORLD_STATE_DELETE_DIRECTIVE = "__dreamland_delete__"
WORLD_STATE_REPLACE_DIRECTIVE = "__dreamland_replace__"
WORLD_STATE_DIRECTIVES = frozenset(
    {
        WORLD_STATE_DELETE_DIRECTIVE,
        WORLD_STATE_REPLACE_DIRECTIVE,
    }
)
SHELL_STACK_EXIT_FIX_MARKER = "shell-stack-exit-v1"
MAX_COMMAND_STATE_EFFECTS = 64


def copy_interactive_program(program: InteractiveProgram) -> InteractiveProgram:
    return InteractiveProgram(
        name=program.name,
        command=program.command,
        argv=tuple(program.argv),
        prompt_template=program.prompt_template,
        metadata=dict(program.metadata),
    )


def copy_shell_environment(environment: ShellEnvironment) -> ShellEnvironment:
    return ShellEnvironment(
        variables=dict(environment.variables),
        exported=frozenset(environment.exported),
    )


def copy_host_identity(identity: HostIdentity) -> HostIdentity:
    return HostIdentity(hostname=identity.hostname)


def shell_environment_with_hostname(
    environment: ShellEnvironment,
    hostname: str,
) -> ShellEnvironment:
    variables = dict(environment.variables)
    variables["HOSTNAME"] = hostname
    exported = set(environment.exported)
    exported.add("HOSTNAME")
    return ShellEnvironment(variables=variables, exported=frozenset(exported))


def copy_shell_frame(frame: ShellFrame) -> ShellFrame:
    return ShellFrame(
        username=frame.username,
        cwd=frame.cwd,
        interactive_program=copy_interactive_program(frame.interactive_program),
        shell_environment=copy_shell_environment(frame.shell_environment),
        host_identity=_copy_frame_host_identity(frame),
        metadata=dict(frame.metadata),
    )


def _copy_frame_host_identity(frame: ShellFrame) -> HostIdentity:
    environment_hostname = frame.shell_environment.variables.get("HOSTNAME")
    if frame.host_identity == HostIdentity() and environment_hostname:
        return HostIdentity(hostname=environment_hostname)
    return copy_host_identity(frame.host_identity)


def active_shell_frame(session: SessionState) -> ShellFrame:
    if session.shell_stack:
        return session.shell_stack[-1]
    return default_shell_frame(
        username=session.username,
        hostname=session.host_identity.hostname,
        cwd=session.cwd,
        interactive_program=session.interactive_program,
        shell_environment=session.shell_environment,
    )


def sync_active_shell_frame(session: SessionState) -> None:
    frame = ShellFrame(
        username=session.username,
        cwd=session.cwd,
        interactive_program=copy_interactive_program(session.interactive_program),
        shell_environment=copy_shell_environment(session.shell_environment),
        host_identity=copy_host_identity(session.host_identity),
        metadata=dict(active_shell_frame(session).metadata),
    )
    if session.shell_stack:
        session.shell_stack[-1] = frame
    else:
        session.shell_stack.append(frame)


def sync_session_from_active_shell_frame(session: SessionState) -> None:
    frame = active_shell_frame(session)
    session.username = frame.username
    session.cwd = frame.cwd
    session.interactive_program = copy_interactive_program(frame.interactive_program)
    session.shell_environment = copy_shell_environment(frame.shell_environment)
    session.host_identity = copy_host_identity(frame.host_identity)
    session.world_state = sync_active_host(
        session.world_state,
        session.host_identity.hostname,
    )


def replace_shell_stack(
    session: SessionState,
    shell_stack: list[ShellFrame] | tuple[ShellFrame, ...],
    *,
    force_session_state_next_command: bool = False,
    reconcile_network: bool = True,
) -> None:
    previous_stack = tuple(session.shell_stack)
    session.shell_stack = [copy_shell_frame(frame) for frame in shell_stack]
    if session.shell_stack:
        sync_session_from_active_shell_frame(session)
    if force_session_state_next_command:
        session.force_session_state_next_command = True
    if reconcile_network:
        reconcile_session_connections(session, previous_stack)


def pop_active_shell_frame(
    session: SessionState,
    *,
    force_session_state_next_command: bool = False,
) -> bool:
    if len(session.shell_stack) <= 1:
        return False
    replace_shell_stack(
        session,
        session.shell_stack[:-1],
        force_session_state_next_command=force_session_state_next_command,
    )
    return True


def apply_command_state_update(
    session: SessionState,
    update: CommandStateUpdate,
    *,
    command: str | None = None,
    reconcile_network: bool = True,
) -> None:
    validate_command_state_effects(update)
    if update.effects:
        prepared = deepcopy(session)
        for index, effect in enumerate(update.effects):
            transition = effect.state_update.shell_stack_transition
            if (
                transition is not None
                and transition.operation == "pop"
                and len(prepared.shell_stack) <= 1
                and index != len(update.effects) - 1
            ):
                raise ValueError("state_update effects cannot follow an outermost pop")
            _apply_command_state_update(prepared, effect.state_update)
        for effect in update.effects:
            apply_command_state_update(
                session,
                effect.state_update,
                command=effect.command,
                reconcile_network=reconcile_network,
            )
        return
    previous_stack = tuple(session.shell_stack) or (active_shell_frame(session),)
    _apply_command_state_update(session, update)
    if reconcile_network:
        reconcile_session_connections(
            session,
            previous_stack,
            command=command,
            transition=update.shell_stack_transition,
        )


def validate_command_state_effects(update: CommandStateUpdate) -> None:
    """Reject malformed effect envelopes before applying any of their changes."""
    if not isinstance(update.effects, tuple):
        raise ValueError("state_update.effects must be a tuple")
    if not update.effects:
        return
    if len(update.effects) > MAX_COMMAND_STATE_EFFECTS:
        raise ValueError("state_update.effects exceeds 64 effects")
    if any(
        value is not None
        for value in (
            update.host_identity,
            update.cwd,
            update.interactive_program,
            update.shell_environment,
            update.shell_stack,
            update.shell_stack_transition,
            update.world_state,
        )
    ):
        raise ValueError("state_update.effects cannot accompany ordinary state fields")
    for effect in update.effects:
        if (
            not isinstance(effect, CommandStateEffect)
            or not isinstance(effect.command, str)
            or not effect.command.strip()
            or not isinstance(effect.state_update, CommandStateUpdate)
        ):
            raise ValueError("invalid state_update effect")
        if not isinstance(effect.state_update.effects, tuple):
            raise ValueError("state_update.effects must be a tuple")
        if effect.state_update.effects:
            raise ValueError("state_update.effects cannot be nested")
        if effect.state_update.shell_stack is not None:
            raise ValueError("state_update effects must use shell_stack_transition")
        _validate_effect_state_update(effect.state_update)


def _validate_effect_state_update(update: CommandStateUpdate) -> None:
    """Check typed custom-agent effects as strictly as model effect payloads."""
    if update.cwd is not None and (not isinstance(update.cwd, str) or not update.cwd):
        raise ValueError("effect cwd must be a nonempty string")
    identity = update.host_identity
    if identity is not None and (
        not isinstance(identity, HostIdentity)
        or not isinstance(identity.hostname, str)
        or not identity.hostname
    ):
        raise ValueError("invalid effect host_identity")
    program = update.interactive_program
    if program is not None and (
        not isinstance(program, InteractiveProgram)
        or not isinstance(program.name, str)
        or not program.name
        or not isinstance(program.command, str)
        or not program.command
        or not isinstance(program.argv, tuple)
        or any(not isinstance(arg, str) for arg in program.argv)
        or (
            program.prompt_template is not None
            and not isinstance(program.prompt_template, str)
        )
        or not isinstance(program.metadata, dict)
    ):
        raise ValueError("invalid effect interactive_program")
    environment = update.shell_environment
    if environment is not None and (
        not isinstance(environment, ShellEnvironment)
        or not isinstance(environment.variables, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.variables.items()
        )
        or not isinstance(environment.exported, frozenset)
        or any(not isinstance(name, str) for name in environment.exported)
    ):
        raise ValueError("invalid effect shell_environment")
    transition = update.shell_stack_transition
    if transition is not None and (
        not isinstance(transition, ShellStackTransition)
        or transition.operation not in ("push", "pop", "replace_active")
        or (
            transition.username is not None
            and (not isinstance(transition.username, str) or not transition.username)
        )
        or not isinstance(transition.metadata, dict)
    ):
        raise ValueError("invalid effect shell_stack_transition")
    if update.world_state is not None and not isinstance(update.world_state, dict):
        raise ValueError("effect world_state must be an object")


def _apply_command_state_update(
    session: SessionState,
    update: CommandStateUpdate,
) -> None:
    if update.host_identity is not None:
        session.host_identity = copy_host_identity(update.host_identity)

    if (
        update.host_identity is not None
        and update.shell_environment is None
        and update.shell_stack is None
    ):
        update = CommandStateUpdate(
            host_identity=update.host_identity,
            cwd=update.cwd,
            interactive_program=update.interactive_program,
            shell_environment=shell_environment_with_hostname(
                active_shell_frame(session).shell_environment,
                update.host_identity.hostname,
            ),
            shell_stack=update.shell_stack,
            shell_stack_transition=update.shell_stack_transition,
            world_state=update.world_state,
        )

    if update.world_state is not None:
        session.world_state = merge_world_state(session.world_state, update.world_state)

    current_stack = tuple(session.shell_stack)
    if update.shell_stack_transition is not None and not current_stack:
        current_stack = (active_shell_frame(session),)
    next_stack = shell_stack_after_update(current_stack, update)
    if next_stack is not None:
        replace_shell_stack(session, next_stack, reconcile_network=False)
        session.world_state = sync_active_host(
            session.world_state,
            session.host_identity.hostname,
        )
        return

    active_frame = active_shell_frame(session)
    entering_program = (
        update.interactive_program is not None
        and update.interactive_program != active_frame.interactive_program
    )
    next_frame = ShellFrame(
        username=active_frame.username,
        cwd=update.cwd if update.cwd is not None else active_frame.cwd,
        interactive_program=(
            copy_interactive_program(update.interactive_program)
            if update.interactive_program is not None
            else copy_interactive_program(active_frame.interactive_program)
        ),
        shell_environment=(
            copy_shell_environment(update.shell_environment)
            if update.shell_environment is not None
            else copy_shell_environment(active_frame.shell_environment)
        ),
        host_identity=(
            copy_host_identity(update.host_identity)
            if update.host_identity is not None
            else copy_host_identity(active_frame.host_identity)
        ),
        metadata=dict(active_frame.metadata),
    )
    if entering_program:
        session.shell_stack.append(next_frame)
    elif session.shell_stack:
        session.shell_stack[-1] = next_frame
    else:
        session.shell_stack.append(next_frame)
    sync_session_from_active_shell_frame(session)


def apply_external_state_update(
    session: SessionState,
    update: CommandStateUpdate,
) -> None:
    apply_command_state_update(session, update)
    session.force_session_state_next_command = True


def append_host_command_history(
    world_state: dict[str, object],
    hostname: str,
    entry: CommandHistoryEntry,
) -> dict[str, object]:
    synced = sync_active_host(world_state, hostname)
    host_ref = active_host_ref(synced)
    hosts = synced.setdefault("hosts_by_name", {})
    if not isinstance(hosts, dict):
        synced["hosts_by_name"] = {}
        hosts = synced["hosts_by_name"]
    host = hosts.get(host_ref)
    if not isinstance(host, dict):
        host = {"hostname": hostname}
        hosts[host_ref] = host
    history = host.setdefault("command_history", [])
    if not isinstance(history, list):
        history = []
        host["command_history"] = history
    history.append(command_history_entry_to_state(entry))
    return synced


def host_command_history(
    world_state: dict[str, object],
    hostname: str,
) -> tuple[CommandHistoryEntry, ...]:
    synced = sync_active_host(world_state, hostname)
    host_ref = active_host_ref(synced)
    hosts = synced.get("hosts_by_name")
    if not isinstance(hosts, dict):
        return ()
    host = hosts.get(host_ref)
    if not isinstance(host, dict):
        return ()
    history = host.get("command_history")
    if not isinstance(history, list):
        return ()
    return tuple(command_history_entry_from_state(entry) for entry in history)


def command_history_entry_to_state(entry: CommandHistoryEntry) -> dict[str, object]:
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


def command_history_entry_from_state(value: object) -> CommandHistoryEntry:
    if not isinstance(value, dict):
        return CommandHistoryEntry(cwd="", command="")
    return CommandHistoryEntry(
        id=str(value["id"]) if value.get("id") is not None else "",
        cwd=str(value.get("cwd", "")),
        command=str(value.get("command", "")),
        stdout=str(value.get("stdout", "")),
        stderr=str(value.get("stderr", "")),
        exit_status=_coerce_int(value.get("exit_status")),
        interrupted=bool(value.get("interrupted", False)),
        signal=str(value["signal"]) if value.get("signal") is not None else None,
    )


def merge_world_state(
    current: dict[str, object],
    patch: dict[str, object],
) -> dict[str, object]:
    directive = _world_state_directive(patch)
    if directive == WORLD_STATE_DELETE_DIRECTIVE:
        return {}
    if directive == WORLD_STATE_REPLACE_DIRECTIVE:
        replacement = patch.get(WORLD_STATE_REPLACE_DIRECTIVE)
        return deepcopy(replacement) if isinstance(replacement, dict) else {}

    merged = deepcopy(current)
    for key, value in patch.items():
        if key in WORLD_STATE_DIRECTIVES:
            continue
        if isinstance(value, dict):
            directive = _world_state_directive(value)
            if directive == WORLD_STATE_DELETE_DIRECTIVE:
                merged.pop(key, None)
                continue
            if directive == WORLD_STATE_REPLACE_DIRECTIVE:
                merged[key] = deepcopy(value.get(WORLD_STATE_REPLACE_DIRECTIVE))
                continue
        current_value = merged.get(key)
        if isinstance(current_value, dict) and isinstance(value, dict):
            merged[key] = merge_world_state(current_value, value)
            continue
        merged[key] = deepcopy(value)
    return merged


def _world_state_directive(value: dict[str, object]) -> str | None:
    if value.get(WORLD_STATE_DELETE_DIRECTIVE) is True:
        return WORLD_STATE_DELETE_DIRECTIVE
    if WORLD_STATE_REPLACE_DIRECTIVE in value:
        return WORLD_STATE_REPLACE_DIRECTIVE
    return None


def _coerce_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (str, int, float)):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def shell_stack_after_update(
    shell_stack: tuple[ShellFrame, ...],
    update: CommandStateUpdate,
) -> tuple[ShellFrame, ...] | None:
    if update.shell_stack is not None:
        return tuple(copy_shell_frame(frame) for frame in update.shell_stack)
    transition = update.shell_stack_transition
    if transition is None:
        return None
    if not shell_stack:
        return None
    if transition.operation == "pop":
        return shell_stack[:-1] if len(shell_stack) > 1 else shell_stack

    active_frame = shell_stack[-1]
    next_frame = ShellFrame(
        username=transition.username or active_frame.username,
        cwd=update.cwd if update.cwd is not None else active_frame.cwd,
        interactive_program=(
            copy_interactive_program(update.interactive_program)
            if update.interactive_program is not None
            else copy_interactive_program(active_frame.interactive_program)
        ),
        shell_environment=(
            copy_shell_environment(update.shell_environment)
            if update.shell_environment is not None
            else copy_shell_environment(active_frame.shell_environment)
        ),
        host_identity=(
            copy_host_identity(update.host_identity)
            if update.host_identity is not None
            else copy_host_identity(active_frame.host_identity)
        ),
        metadata={**active_frame.metadata, **transition.metadata},
    )
    if transition.operation == "push":
        return (*shell_stack, next_frame)
    return (*shell_stack[:-1], next_frame)


def initial_shell_environment(
    username: str,
    hostname: str,
    cwd: str,
    shell: str,
    overrides: dict[str, str],
) -> ShellEnvironment:
    environment = default_shell_environment(
        username=username,
        hostname=hostname,
        cwd=cwd,
        shell=shell,
    )
    variables = dict(environment.variables)
    variables.update(overrides)
    exported = set(environment.exported)
    exported.update(overrides)
    return ShellEnvironment(variables=variables, exported=frozenset(exported))
