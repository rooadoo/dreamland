from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Literal

from dreamland.models import CommandResult, CommandStateUpdate, ShellEnvironment


@dataclass(frozen=True, slots=True)
class ShellCommandSegment:
    command: str
    operator_before: str | None = None


@dataclass(frozen=True, slots=True)
class ShellCommandAnalysis:
    kind: Literal["empty", "simple", "list", "unsupported"]
    segments: tuple[ShellCommandSegment, ...] = ()


@dataclass(frozen=True, slots=True)
class FilesystemHint:
    path_kind: str | None = None
    exists: bool | None = None
    accessible: bool | None = None


def change_directory(
    username: str,
    cwd: str,
    argv: list[str],
    environment: ShellEnvironment,
    filesystem_hint: FilesystemHint | None = None,
) -> CommandResult:
    if len(argv) > 2:
        return CommandResult(stderr="bash: cd: too many arguments\n", exit_status=1)
    next_cwd = resolve_cd_target(username, cwd, argv, environment)
    if next_cwd is None:
        return CommandResult(stderr="bash: cd: OLDPWD not set\n", exit_status=1)
    if filesystem_hint is not None:
        if filesystem_hint.exists is False:
            return CommandResult(
                stderr=f"bash: cd: {next_cwd}: No such file or directory\n",
                exit_status=1,
            )
        if filesystem_hint.path_kind == "file":
            return CommandResult(
                stderr=f"bash: cd: {next_cwd}: Not a directory\n",
                exit_status=1,
            )
        if filesystem_hint.accessible is False:
            return CommandResult(
                stderr=f"bash: cd: {next_cwd}: Permission denied\n",
                exit_status=1,
            )
    return CommandResult(
        stdout=f"{next_cwd}\n" if len(argv) > 1 and argv[1] == "-" else "",
        state_update=CommandStateUpdate(
            cwd=next_cwd,
            shell_environment=with_directory_environment(
                environment,
                cwd,
                next_cwd,
            ),
        ),
    )


def resolve_cd_target(
    username: str,
    cwd: str,
    argv: list[str],
    environment: ShellEnvironment,
) -> str | None:
    home = environment.variables.get("HOME", f"/home/{username}")
    if len(argv) == 1 or argv[1] in {"~", ""}:
        return home
    target = expand_shell_variables(argv[1], environment)
    if target == "-":
        oldpwd = environment.variables.get("OLDPWD", "")
        return oldpwd or None
    if target == "~" or target.startswith("~/"):
        return normalize_path(home if target == "~" else f"{home}/{target[2:]}")
    if target.startswith("/"):
        return normalize_path(target)
    return normalize_path(f"{cwd}/{target}")


def print_environment(environment: ShellEnvironment) -> CommandResult:
    lines = [
        f"{key}={environment.variables[key]}"
        for key in sorted(environment.exported)
        if key in environment.variables
    ]
    return CommandResult(stdout="\n".join(lines) + ("\n" if lines else ""))


def env(args: list[str], environment: ShellEnvironment) -> CommandResult:
    assignments, remaining = split_leading_assignments(args)
    if assignments:
        environment = environment_with_assignments(
            environment,
            assignments,
            export=True,
        )
    if not remaining:
        return print_environment(environment)
    executable = remaining[0]
    if executable == "env":
        return env(remaining[1:], environment)
    if executable == "printenv":
        return printenv(remaining[1:], environment)
    return CommandResult(
        stderr=f"env: `{executable}': No such file or directory\n",
        exit_status=127,
    )


def printenv(names: list[str], environment: ShellEnvironment) -> CommandResult:
    if not names:
        return print_environment(environment)
    lines: list[str] = []
    missing = False
    for name in names:
        expanded_name = expand_shell_variables(name, environment)
        if (
            expanded_name in environment.exported
            and expanded_name in environment.variables
        ):
            lines.append(environment.variables[expanded_name])
        else:
            missing = True
    return CommandResult(
        stdout="\n".join(lines) + ("\n" if lines else ""),
        exit_status=1 if missing else 0,
    )


def set_variables(args: list[str], environment: ShellEnvironment) -> CommandResult:
    if args:
        return CommandResult()
    lines = [
        f"{name}={format_set_value(value)}"
        for name, value in sorted(environment.variables.items())
    ]
    return CommandResult(stdout="\n".join(lines) + ("\n" if lines else ""))


def declare_variables(
    args: list[str],
    environment: ShellEnvironment,
) -> CommandResult:
    if not args:
        return CommandResult(stdout=format_declare_variables(environment, None))
    if args[0] == "-p":
        names = args[1:]
        if not names:
            return CommandResult(stdout=format_declare_variables(environment, None))
        return declare_named_variables(names, environment)
    if args[0] == "-x":
        return export_variables(args[1:], environment)
    return CommandResult(stdout=format_declare_variables(environment, args))


def declare_named_variables(
    names: list[str],
    environment: ShellEnvironment,
) -> CommandResult:
    lines: list[str] = []
    missing: list[str] = []
    for name in names:
        if name in environment.variables:
            lines.append(format_declare_variable(name, environment))
        else:
            missing.append(name)
    stderr = "".join(f"bash: declare: {name}: not found\n" for name in missing)
    return CommandResult(
        stdout="\n".join(lines) + ("\n" if lines else ""),
        stderr=stderr,
        exit_status=1 if missing else 0,
    )


def format_declare_variables(
    environment: ShellEnvironment,
    names: list[str] | None,
) -> str:
    selected_names = sorted(environment.variables) if names is None else names
    lines = [
        format_declare_variable(name, environment)
        for name in selected_names
        if name in environment.variables
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def format_declare_variable(name: str, environment: ShellEnvironment) -> str:
    flag = "-x" if name in environment.exported else "--"
    value = escape_export_value(environment.variables.get(name, ""))
    return f'declare {flag} {name}="{value}"'


def format_set_value(value: str) -> str:
    if value and not re.search(r"[\s'\"\\$`]", value):
        return value
    escaped = value.replace("'", "'\\''")
    return f"'{escaped}'"


def export_variables(
    args: list[str],
    environment: ShellEnvironment,
) -> CommandResult:
    if not is_bash_environment(environment):
        return CommandResult(
            stderr="export: not available in this interactive program\n",
            exit_status=1,
        )
    if not args:
        return CommandResult(stdout=format_export_declarations(environment))
    variables = dict(environment.variables)
    exported = set(environment.exported)
    for arg in args:
        if arg == "-p":
            continue
        if "=" in arg:
            name, value = arg.split("=", maxsplit=1)
            if not is_shell_variable_name(name):
                return CommandResult(
                    stderr=f"bash: export: `{arg}': not a valid identifier\n",
                    exit_status=1,
                )
            variables[name] = expand_shell_variables(value, environment)
            exported.add(name)
            continue
        if not is_shell_variable_name(arg):
            return CommandResult(
                stderr=f"bash: export: `{arg}': not a valid identifier\n",
                exit_status=1,
            )
        exported.add(arg)
        variables.setdefault(arg, "")
    return CommandResult(
        state_update=CommandStateUpdate(
            shell_environment=ShellEnvironment(
                variables=variables,
                exported=frozenset(exported),
            )
        )
    )


def unset_variables(
    args: list[str],
    environment: ShellEnvironment,
) -> CommandResult:
    if not is_bash_environment(environment):
        return CommandResult(
            stderr="unset: not available in this interactive program\n",
            exit_status=1,
        )
    variables = dict(environment.variables)
    exported = set(environment.exported)
    for name in args:
        if name.startswith("-"):
            continue
        variables.pop(name, None)
        exported.discard(name)
    return CommandResult(
        state_update=CommandStateUpdate(
            shell_environment=ShellEnvironment(
                variables=variables,
                exported=frozenset(exported),
            )
        )
    )


def assign_variables(
    argv: list[str],
    environment: ShellEnvironment,
) -> CommandResult:
    if not is_bash_environment(environment):
        return CommandResult(
            stderr="variable assignment is not available in this interactive program\n",
            exit_status=1,
        )
    variables = dict(environment.variables)
    for arg in argv:
        name, value = arg.split("=", maxsplit=1)
        variables[name] = expand_shell_variables(value, environment)
    return CommandResult(
        state_update=CommandStateUpdate(
            shell_environment=ShellEnvironment(
                variables=variables,
                exported=frozenset(environment.exported),
            )
        )
    )


def environment_with_assignments(
    environment: ShellEnvironment,
    assignments: dict[str, str],
    *,
    export: bool,
) -> ShellEnvironment:
    variables = dict(environment.variables)
    for name, value in assignments.items():
        variables[name] = expand_shell_variables(value, environment)
    exported = set(environment.exported)
    if export:
        exported.update(assignments)
    return ShellEnvironment(variables=variables, exported=frozenset(exported))


def with_directory_environment(
    environment: ShellEnvironment,
    old_cwd: str,
    new_cwd: str,
) -> ShellEnvironment:
    variables = dict(environment.variables)
    variables["OLDPWD"] = old_cwd
    variables["PWD"] = new_cwd
    exported = set(environment.exported)
    exported.update({"OLDPWD", "PWD"})
    return ShellEnvironment(variables=variables, exported=frozenset(exported))


def format_export_declarations(environment: ShellEnvironment) -> str:
    lines = [
        f'declare -x {name}="{escape_export_value(environment.variables.get(name, ""))}"'
        for name in sorted(environment.exported)
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def escape_export_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def split_command(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def analyze_command(command: str) -> ShellCommandAnalysis:
    """Recognize simple shell lists without rewriting their source text.

    This is an eligibility check, not a shell interpreter. Anything requiring
    richer shell parsing stays on the model path. Keeping quoting here avoids
    mistaking a quoted separator for an operator after tokenization.
    """

    segments: list[ShellCommandSegment] = []
    start = 0
    index = 0
    quote: str | None = None
    operator_before: str | None = None
    saw_separator = False
    unsupported = ShellCommandAnalysis("unsupported")

    while index < len(command):
        character = command[index]
        if quote == "'":
            if character == "'":
                quote = None
            index += 1
            continue
        if character == "\\":
            if index + 1 == len(command) or command[index + 1] in "\r\n":
                return unsupported
            if quote == '"' and command[index + 1] not in '$`"\\':
                index += 1
            else:
                index += 2
            continue
        if character == quote:
            quote = None
            index += 1
            continue
        if character == "`" or command.startswith("$(", index):
            return unsupported
        if command.startswith("${", index):
            parameter = re.match(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", command[index:])
            if parameter is None:
                return unsupported
            index += len(parameter.group())
            continue
        if quote is not None:
            index += 1
            continue
        if character in "'\"":
            quote = character
            index += 1
            continue
        if character in "\r\n<>(){}":
            return unsupported
        if character == "#" and (index == start or command[index - 1].isspace()):
            return unsupported
        separator = None
        if character == ";":
            separator = ";"
        elif command[index : index + 2] in {"&&", "||"}:
            separator = command[index : index + 2]
        elif character in "&|":
            return unsupported
        if separator is not None:
            saw_separator = True
            source = command[start:index].strip(" \t")
            if not source:
                return unsupported
            segments.append(
                ShellCommandSegment(source, operator_before=operator_before)
            )
            operator_before = separator
            index += len(separator)
            start = index
            continue
        index += 1

    if quote is not None:
        return unsupported
    source = command[start:].strip(" \t")
    if source:
        segments.append(ShellCommandSegment(source, operator_before=operator_before))
    elif operator_before in {"&&", "||"}:
        return unsupported
    if not segments:
        return ShellCommandAnalysis("empty")

    reserved = {
        "!",
        "[[",
        "]]",
        "case",
        "do",
        "done",
        "elif",
        "else",
        "esac",
        "fi",
        "for",
        "function",
        "if",
        "in",
        "select",
        "then",
        "time",
        "until",
        "while",
    }
    for segment in segments:
        argv = split_command(segment.command)
        if not argv or argv[0] in reserved:
            return unsupported
    return ShellCommandAnalysis("list" if saw_separator else "simple", tuple(segments))


def split_command_list(command: str) -> list[ShellCommandSegment] | None:
    """Return source-preserving segments only for a supported shell list."""

    analysis = analyze_command(command)
    return list(analysis.segments) if analysis.kind == "list" else None


def is_variable_assignment_command(argv: list[str]) -> bool:
    return bool(argv) and all(is_assignment_word(arg) for arg in argv)


def split_leading_assignments(
    argv: list[str],
) -> tuple[dict[str, str], list[str]]:
    assignments: dict[str, str] = {}
    for index, value in enumerate(argv):
        if not is_assignment_word(value):
            return assignments, argv[index:]
        name, assigned_value = value.split("=", maxsplit=1)
        assignments[name] = assigned_value
    return assignments, []


def is_assignment_word(value: str) -> bool:
    if "=" not in value:
        return False
    name = value.split("=", maxsplit=1)[0]
    return is_shell_variable_name(name)


def is_shell_variable_name(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None


def expand_shell_variables(
    value: str,
    environment: ShellEnvironment,
) -> str:
    def replace_braced(match: re.Match[str]) -> str:
        return environment.variables.get(match.group(1), "")

    def replace_plain(match: re.Match[str]) -> str:
        return environment.variables.get(match.group(1), "")

    expanded = re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
        replace_braced,
        value,
    )
    return re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", replace_plain, expanded)


def is_bash_environment(environment: ShellEnvironment) -> bool:
    shell = environment.variables.get("SHELL", "")
    return shell.endswith("/bash") or shell == "bash"


def normalize_path(path: str) -> str:
    parts: list[str] = []
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)
