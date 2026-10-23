from __future__ import annotations

from dreamland import agent_shell
from dreamland.models import (
    CommandHistoryEntry,
    CommandResult,
    CommandStateUpdate,
    DreamlandCommandState,
    InteractiveProgram,
    ShellEnvironment,
)
from dreamland.program_input import has_pending_shell_input

_SHELL_EXIT_PROGRAMS = frozenset({"bash", "dash", "ksh", "sh", "zsh"})


def supports_builtin_exit(program: InteractiveProgram) -> bool:
    name = program.name.strip().lower()
    executable = program.command.rsplit("/", 1)[-1].strip().lower()
    return (
        name in _SHELL_EXIT_PROGRAMS or executable in _SHELL_EXIT_PROGRAMS
    ) and not has_pending_shell_input(program)


def is_builtin_command(
    command: str,
    interactive_program: InteractiveProgram | None = None,
) -> bool:
    if interactive_program is not None and not supports_builtin_exit(
        interactive_program
    ):
        return False
    if agent_shell.analyze_command(command).kind != "simple":
        return False
    if _requires_shell_expansion(command):
        return False
    argv = agent_shell.split_command(command)
    if not argv:
        return False
    assignments, remaining = agent_shell.split_leading_assignments(argv)
    if assignments and remaining:
        if remaining[0] not in {"env", "printenv", "set", "declare"}:
            return False
        argv = remaining
    executable = argv[0]
    args = argv[1:]
    if executable in {"exit", "logout"}:
        return (
            not args
            and interactive_program is not None
            and supports_builtin_exit(interactive_program)
        )
    if executable in {"pwd", "whoami", "set"}:
        return not args
    if executable == "cd":
        return not args or not args[0].startswith("-") or args[0] == "-"
    if executable == "env":
        return _supports_env_arguments(args)
    if executable == "printenv":
        return not any(arg.startswith("-") for arg in args)
    if executable == "export":
        return not any(arg.startswith("-") for arg in args)
    if executable == "unset":
        return all(agent_shell.is_shell_variable_name(arg) for arg in args)
    if executable == "declare":
        if not args:
            return True
        if args[0] == "-p":
            return all(agent_shell.is_shell_variable_name(arg) for arg in args[1:])
        if args[0] == "-x":
            return not any(arg.startswith("-") for arg in args[1:])
        return False
    return agent_shell.is_variable_assignment_command(argv)


def _supports_env_arguments(args: list[str]) -> bool:
    """The local env implementation only wraps env and printenv."""

    _, remaining = agent_shell.split_leading_assignments(args)
    while remaining and remaining[0] == "env":
        _, remaining = agent_shell.split_leading_assignments(remaining[1:])
    return not remaining or (
        remaining[0] == "printenv"
        and not any(arg.startswith("-") for arg in remaining[1:])
    )


def _requires_shell_expansion(command: str) -> bool:
    """Defer syntax whose quote/expansion semantics local handlers lose.

    Local handlers expand simple variable names after shlex removes quotes.
    Literal dollar signs must therefore stay with the model, as must pathname
    expansion and shell-specific parameter expansions.
    """

    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if quote == "'":
            if character in "$~":
                return True
            if character == "'":
                quote = None
            index += 1
            continue
        if character == "\\":
            if index + 1 < len(command) and command[index + 1] in "$~":
                return True
            index += 2
            continue
        if character == quote:
            quote = None
        elif character in "'\"" and quote is None:
            quote = character
        elif character == "~" and quote is not None:
            return True
        elif character == "$":
            suffix = command[index + 1 :]
            if suffix.startswith("{"):
                end = command.find("}", index + 2)
                if end == -1 or not agent_shell.is_shell_variable_name(
                    command[index + 2 : end]
                ):
                    return True
            elif suffix and not (suffix[0].isalpha() or suffix[0] == "_"):
                return True
        elif quote is None and character in "*?[":
            return True
        index += 1
    return False


def handle_builtin_command(state: DreamlandCommandState) -> CommandResult:
    return _handle_builtin_command(state, defer_ambiguous_cd=False)


def handle_builtin_command_if_deterministic(
    state: DreamlandCommandState,
) -> CommandResult | None:
    command = state.get("current_command", "")
    interactive_program = agent_core_program_from_state(state)
    if not is_builtin_command(command, interactive_program):
        return None
    return _handle_builtin_command(state, defer_ambiguous_cd=True)


def _handle_builtin_command(
    state: DreamlandCommandState,
    *,
    defer_ambiguous_cd: bool,
) -> CommandResult | None:
    from dreamland import agent_core

    command = state.get("current_command", "")
    username = state.get("username", "guest")
    cwd = state.get("cwd", f"/home/{username}")
    environment = agent_core._shell_environment_from_state(
        state.get("shell_environment")
    )
    argv = agent_shell.split_command(command)
    if not argv:
        return CommandResult()
    local_assignments, remaining = agent_shell.split_leading_assignments(argv)
    if local_assignments and remaining:
        environment = agent_shell.environment_with_assignments(
            environment,
            local_assignments,
            export=True,
        )
        argv = remaining
    executable = argv[0]
    if executable == "pwd":
        return CommandResult(stdout=f"{cwd}\n")
    if executable == "whoami":
        return CommandResult(stdout=f"{username}\n")
    if executable in {"exit", "logout"}:
        shell_stack = agent_core._shell_stack_from_state(
            state.get("shell_stack"),
            username=username,
            hostname=str(state.get("hostname", "ubuntu")),
            cwd=cwd,
            interactive_program=agent_core._interactive_program_from_state(
                state.get("interactive_program")
            ),
            shell_environment=environment,
        )
        if len(shell_stack) > 1:
            return CommandResult(
                exit_status=0,
                state_update=CommandStateUpdate(shell_stack=shell_stack[:-1]),
            )
        return CommandResult(exit_status=0)
    if executable == "cd":
        if defer_ambiguous_cd:
            hint = _cd_filesystem_hint(state, username, cwd, argv, environment)
            if hint is None:
                return None
            return agent_shell.change_directory(
                username,
                cwd,
                argv,
                environment,
                filesystem_hint=hint,
            )
        return agent_shell.change_directory(username, cwd, argv, environment)
    if executable == "env":
        return agent_shell.env(argv[1:], environment)
    if executable == "printenv":
        return agent_shell.printenv(argv[1:], environment)
    if executable == "export":
        return agent_shell.export_variables(argv[1:], environment)
    if executable == "unset":
        return agent_shell.unset_variables(argv[1:], environment)
    if executable == "set":
        return agent_shell.set_variables(argv[1:], environment)
    if executable == "declare":
        return agent_shell.declare_variables(argv[1:], environment)
    if agent_shell.is_variable_assignment_command(argv):
        return agent_shell.assign_variables(argv, environment)
    return CommandResult()


def agent_core_program_from_state(state: DreamlandCommandState) -> InteractiveProgram:
    from dreamland import agent_core

    return agent_core._interactive_program_from_state(state.get("interactive_program"))


def _cd_filesystem_hint(
    state: DreamlandCommandState,
    username: str,
    cwd: str,
    argv: list[str],
    environment: ShellEnvironment,
) -> agent_shell.FilesystemHint | None:
    """Return a conservative cd hint, or None when the model should decide.

    This is a temporary bridge until DREAMLAND has a first-class filesystem
    state model. Keep the evidence sources narrow and explicit so the helper can
    be removed without untangling command execution.
    """

    if len(argv) > 2:
        return agent_shell.FilesystemHint(path_kind="directory", exists=True)
    target = agent_shell.resolve_cd_target(username, cwd, argv, environment)
    if target is None:
        return agent_shell.FilesystemHint(path_kind="directory", exists=True)

    known_dirs = _baseline_directories(username, cwd, environment)
    known_files: set[str] = set()
    missing_paths: set[str] = set()
    denied_paths: set[str] = set()

    for entry in _history_from_state(state):
        _collect_history_path_hints(
            entry,
            username,
            environment,
            known_dirs,
            known_files,
            missing_paths,
            denied_paths,
        )

    if _is_root_only_path(target, username):
        return agent_shell.FilesystemHint(
            path_kind="directory",
            exists=True,
            accessible=False,
        )
    if target in known_files:
        return agent_shell.FilesystemHint(path_kind="file", exists=True)
    if target in missing_paths:
        return agent_shell.FilesystemHint(exists=False)
    if target in denied_paths:
        return agent_shell.FilesystemHint(
            path_kind="directory",
            exists=True,
            accessible=False,
        )
    if target in known_dirs:
        return agent_shell.FilesystemHint(path_kind="directory", exists=True)
    return None


def _is_root_only_path(path: str, username: str) -> bool:
    return username != "root" and (path == "/root" or path.startswith("/root/"))


def _baseline_directories(
    username: str,
    cwd: str,
    environment: ShellEnvironment,
) -> set[str]:
    home = environment.variables.get("HOME", f"/home/{username}")
    directories = {
        "/",
        "/bin",
        "/etc",
        "/home",
        "/opt",
        "/opt/app",
        "/root",
        "/tmp",
        "/usr",
        "/usr/bin",
        "/usr/local",
        "/var",
        "/var/log",
        agent_shell.normalize_path(home),
        agent_shell.normalize_path(cwd),
    }
    for path in (home, cwd):
        normalized = agent_shell.normalize_path(path)
        while normalized != "/":
            normalized = normalized.rsplit("/", 1)[0] or "/"
            directories.add(normalized)
    return directories


def _history_from_state(state: DreamlandCommandState) -> list[CommandHistoryEntry]:
    from dreamland import agent_core

    world_state = state.get("world_state")
    active_host = state.get("active_host")
    if not active_host and isinstance(world_state, dict):
        active_host = world_state.get("active_host") or world_state.get("entry_host")
    host = {}
    if isinstance(world_state, dict):
        hosts = world_state.get("hosts_by_name")
        if isinstance(hosts, dict) and isinstance(active_host, str):
            candidate = hosts.get(active_host)
            if isinstance(candidate, dict):
                host = candidate
    raw_history = host.get("command_history") or []
    history: list[CommandHistoryEntry] = []
    for entry in raw_history:
        if isinstance(entry, CommandHistoryEntry):
            history.append(entry)
        elif isinstance(entry, dict):
            history.append(agent_core._history_entry_from_state(entry))
    return history


def _collect_history_path_hints(
    entry: CommandHistoryEntry,
    username: str,
    environment: ShellEnvironment,
    known_dirs: set[str],
    known_files: set[str],
    missing_paths: set[str],
    denied_paths: set[str],
) -> None:
    if agent_shell.analyze_command(entry.command).kind != "simple":
        return
    argv = agent_shell.split_command(entry.command)
    if not argv:
        return
    if entry.exit_status == 0 and argv[0] == "pwd":
        path = entry.stdout.strip()
        if path.startswith("/"):
            known_dirs.add(agent_shell.normalize_path(path))
    if entry.exit_status == 0 and argv[0] == "cd":
        target = agent_shell.resolve_cd_target(username, entry.cwd, argv, environment)
        if target is not None:
            known_dirs.add(target)
    if argv[0] == "ls":
        _collect_ls_hints(
            entry,
            username,
            environment,
            known_dirs,
            known_files,
            missing_paths,
            denied_paths,
        )


def _collect_ls_hints(
    entry: CommandHistoryEntry,
    username: str,
    environment: ShellEnvironment,
    known_dirs: set[str],
    known_files: set[str],
    missing_paths: set[str],
    denied_paths: set[str],
) -> None:
    argv = agent_shell.split_command(entry.command)
    operands = [arg for arg in argv[1:] if not arg.startswith("-")]
    listed_dir = _resolve_ls_operand(
        username,
        entry.cwd,
        operands[0] if operands else ".",
        environment,
    )
    if entry.exit_status != 0:
        _collect_failed_ls_hint(entry, listed_dir, missing_paths, denied_paths)
        return
    known_dirs.add(listed_dir)
    for line in entry.stdout.splitlines():
        if not line or line.startswith("total "):
            continue
        file_type = line[0]
        if file_type not in {"d", "-"}:
            continue
        parts = line.split()
        if len(parts) < 9:
            continue
        name = parts[8]
        if " -> " in name:
            name = name.split(" -> ", 1)[0]
        if name in {".", ".."}:
            continue
        path = agent_shell.normalize_path(f"{listed_dir}/{name}")
        if file_type == "d":
            known_dirs.add(path)
            if not _directory_entry_allows_cd(parts, username):
                denied_paths.add(path)
        else:
            known_files.add(path)


def _directory_entry_allows_cd(parts: list[str], username: str) -> bool:
    mode = parts[0]
    owner = parts[2] if len(parts) > 2 else ""
    if username == "root":
        return True
    if len(mode) < 10:
        return True
    if owner == username:
        return mode[3] in {"x", "s", "t"}
    return mode[9] in {"x", "t"}


def _collect_failed_ls_hint(
    entry: CommandHistoryEntry,
    listed_dir: str,
    missing_paths: set[str],
    denied_paths: set[str],
) -> None:
    text = entry.stderr.lower()
    if "no such file or directory" in text:
        missing_paths.add(listed_dir)
    if "permission denied" in text:
        denied_paths.add(listed_dir)


def _resolve_ls_operand(
    username: str,
    cwd: str,
    operand: str,
    environment: ShellEnvironment,
) -> str:
    argv = ["ls", operand]
    return agent_shell.resolve_cd_target(
        username, cwd, argv, environment
    ) or agent_shell.normalize_path(cwd)
