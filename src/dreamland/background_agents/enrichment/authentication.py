"""Runtime-owned SSH client paths reserved to the foreground interaction.

This is a mutation boundary, not a credential scanner or a shell interpreter.
Only authoritative frame homes and explicit key destinations are retained; no
passwords, passphrases, arbitrary command arguments, or file contents are kept.
The small per-host sets live with SessionState, outside world/model codecs.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass

from dreamland.models import SessionState, ShellFrame
from dreamland.program_input import has_pending_shell_input, is_command_shell
from dreamland.session_state import active_shell_frame
from dreamland.world_state import active_host_ref

_SHELL_CONTROL = re.compile(r"[|;&<>`]|\$\(|[\r\n]")
_KEY_METADATA_FIELDS = ("key_path", "key_file", "filename", "file_path", "output_file")


@dataclass(frozen=True, slots=True)
class ForegroundAuthenticationPaths:
    ssh_directories: frozenset[str] = frozenset()
    key_paths: frozenset[str] = frozenset()

    def protects(self, path: str) -> bool:
        normalized = _absolute_path(path)
        if normalized is None:
            return False
        if normalized in self.key_paths:
            return True
        for directory in self.ssh_directories:
            if normalized in {directory, directory + "/config.d"}:
                return True
            if not normalized.startswith(directory + "/"):
                continue
            relative = normalized[len(directory) + 1 :]
            if (
                relative == "config"
                or relative.startswith("config.d/")
                or relative.startswith("known_hosts")
                or relative.startswith("id_")
            ):
                return True
        return False


def observe_foreground_authentication_paths(
    session: SessionState,
    command: str | None = None,
) -> None:
    """Accumulate homes and chosen key paths before/after foreground commands.

    The caller holds the session lock where one is in use. Frame host identity
    scopes each path; cwd never establishes ownership of another user's home.
    Sets survive program exit, account/host changes, and history compaction.
    """

    active = active_shell_frame(session)
    frames = session.shell_stack or [active]
    for frame in frames:
        host = _frame_host(session, frame, active=frame is active)
        if host is None:
            continue
        directories = session.foreground_ssh_directories_by_host.setdefault(host, set())
        keys = session.foreground_ssh_key_paths_by_host.setdefault(host, set())
        home = _absolute_path(frame.shell_environment.variables.get("HOME"))
        if home is not None:
            directories.add(posixpath.join(home, ".ssh"))
        program = frame.interactive_program
        program_name = posixpath.basename(program.name)
        # Both command and argv appear in model results. Parse only the
        # specific filename flags; never retain the remaining arguments.
        for invocation in (program.command, shlex.join(program.argv)):
            _observe_key_option(keys, invocation, frame, home)
        if program_name == "ssh-keygen":
            for field in _KEY_METADATA_FIELDS:
                path = program.metadata.get(field)
                if isinstance(path, str):
                    _reserve_key(keys, path, frame.cwd, home)
        if frame is active and command is not None:
            if program_name == "ssh-keygen" and _filename_prompt(frame):
                # Filename answers are not shell syntax; preserve spaces.
                if command:
                    _reserve_key(keys, command, frame.cwd, home)
            elif is_command_shell(program) and not has_pending_shell_input(program):
                _observe_key_option(keys, command, frame, home)


def foreground_authentication_paths(
    session: SessionState, *, target_host: str
) -> ForegroundAuthenticationPaths:
    """Refresh and detach the current host's mutation boundary for final apply."""

    observe_foreground_authentication_paths(session)
    return ForegroundAuthenticationPaths(
        ssh_directories=frozenset(
            session.foreground_ssh_directories_by_host.get(target_host, ())
        ),
        key_paths=frozenset(
            session.foreground_ssh_key_paths_by_host.get(target_host, ())
        ),
    )


def _frame_host(
    session: SessionState, frame: ShellFrame, *, active: bool
) -> str | None:
    if active:
        return active_host_ref(session.world_state)
    hosts = session.world_state.get("hosts_by_name")
    if not isinstance(hosts, dict):
        return None
    hostname = frame.host_identity.hostname
    if hostname in hosts:
        return hostname
    matches = [
        key
        for key, host in hosts.items()
        if isinstance(host, dict)
        and (
            host.get("hostname") == hostname
            or (
                isinstance(host.get("aliases"), (list, tuple))
                and hostname in host["aliases"]
            )
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _observe_key_option(
    paths: set[str], command: str, frame: ShellFrame, home: str | None
) -> None:
    if _SHELL_CONTROL.search(command):
        return
    try:
        tokens = shlex.split(command)
    except ValueError:
        return
    if not tokens:
        return
    program = posixpath.basename(tokens[0])
    flag = {"ssh-keygen": "-f", "ssh": "-i"}.get(program)
    if flag is None:
        return
    options_with_value = (
        set("abCDEfFIJjMmNnOPRrstTVwZz")
        if program == "ssh-keygen"
        else set("BbcDEeFIiJLlmOopQRSWw")
    )
    options_without_value = (
        set("AcegHhiKkLlopQquUvy")
        if program == "ssh-keygen"
        else set("1246AaCfGgKkMNnqsTtVvXxYy")
    )
    selected_paths: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--" or not token.startswith("-"):
            break
        for offset, option in enumerate(token[1:], 2):
            if option in options_without_value:
                continue
            if option not in options_with_value:
                # Unknown options can change the interpretation of following
                # words. Do not guess which of them is a key or a secret.
                return
            value = token[offset:]
            if not value:
                index += 1
                if index >= len(tokens):
                    return
                value = tokens[index]
            if option == flag[1]:
                selected_paths.append(value)
            break
        index += 1
    for value in selected_paths:
        _reserve_key(paths, value, frame.cwd, home)


def _filename_prompt(frame: ShellFrame) -> bool:
    program = frame.interactive_program
    phase = str(program.metadata.get("phase", "")).casefold()
    prompt = (program.prompt_template or "").casefold()
    if any(marker in phase for marker in ("password", "passphrase", "secret")):
        return False
    if "enter file in which to save" in prompt:
        return True
    if any(
        marker in phase or marker in prompt for marker in ("password", "passphrase")
    ):
        return False
    return phase in {"filename", "file_name", "filename_prompt", "awaiting_filename"}


def _reserve_key(paths: set[str], value: str, cwd: str, home: str | None) -> None:
    if not value:
        return
    if value.startswith("~/") and home is not None:
        value = posixpath.join(home, value[2:])
    elif not value.startswith("/") and not value.startswith("~"):
        absolute_cwd = _absolute_path(cwd)
        if absolute_cwd is None:
            return
        value = posixpath.join(absolute_cwd, value)
    path = _absolute_path(value)
    if path is not None:
        paths.add(path)
        paths.add(path + ".pub")


def _absolute_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return None
    # Variable expansion is intentionally not evaluated. These paths must
    # identify concrete artifacts rather than guesses about shell state.
    if "$" in value or "`" in value:
        return None
    return "/" + posixpath.normpath(value).lstrip("/")
