"""Bounded deterministic interaction focus shared by background agents."""

from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Final

from dreamland.agent_shell import analyze_command
from dreamland.background_agents.events import JsonValue

MAX_FOCUS_PATHS: Final[int] = 4
MAX_FOCUS_ARTIFACTS: Final[int] = 4
MAX_FOCUS_TEXT: Final[int] = 256


class InteractionFocusMatch(StrEnum):
    """Stable deterministic alignment between two interaction-focus objects."""

    ARTIFACT_EXACT = "artifact_exact"
    PATH_RELATED = "path_related"
    SURFACE_ONLY = "surface_only"


class PresenceProbeKind(StrEnum):
    """Stable broad command classes that inspect signs of independent activity."""

    SESSION_PRESENCE = "session_presence"
    LOGIN_HISTORY = "login_history"
    PROCESS_ACTIVITY = "process_activity"
    NETWORK_ACTIVITY = "network_activity"
    SERVICE_ACTIVITY = "service_activity"
    SCHEDULED_ACTIVITY = "scheduled_activity"
    EVENT_ACTIVITY = "event_activity"


_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_GLOB_MARKERS: Final[frozenset[str]] = frozenset("*?[")
_FILESYSTEM_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "cat",
        "cd",
        "cp",
        "df",
        "du",
        "file",
        "find",
        "head",
        "less",
        "ls",
        "mkdir",
        "mount",
        "mv",
        "pwd",
        "rm",
        "stat",
        "tail",
        "touch",
        "tree",
    }
)
_PROCESS_COMMANDS: Final[frozenset[str]] = frozenset(
    {"kill", "pgrep", "pidof", "pkill", "ps", "pstree"}
)
_NETWORK_COMMANDS: Final[frozenset[str]] = frozenset(
    {"dig", "host", "nmap", "nslookup", "ping", "ssh", "traceroute"}
)
_PACKAGE_COMMANDS: Final[frozenset[str]] = frozenset(
    {"apk", "apt", "apt-get", "dnf", "dpkg", "pip", "pip3", "rpm", "yum"}
)
_DATABASE_COMMANDS: Final[frozenset[str]] = frozenset(
    {"mongosh", "mysql", "psql", "redis-cli", "sqlite3"}
)
_SYSTEMCTL_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "cat",
        "daemon-reload",
        "disable",
        "enable",
        "is-active",
        "is-enabled",
        "list-dependencies",
        "list-sockets",
        "list-timers",
        "list-unit-files",
        "list-units",
        "mask",
        "reload",
        "restart",
        "show",
        "start",
        "status",
        "stop",
        "unmask",
    }
)
_PACKAGE_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "add",
        "autoremove",
        "download",
        "info",
        "install",
        "list",
        "purge",
        "remove",
        "search",
        "show",
        "uninstall",
        "update",
        "upgrade",
    }
)
_OPTIONS_WITH_VALUES: Final[frozenset[str]] = frozenset(
    {
        "-B",
        "-c",
        "-d",
        "-f",
        "-i",
        "-L",
        "-m",
        "-n",
        "-o",
        "-p",
        "-S",
        "-t",
        "-u",
        "-w",
        "--block-size",
        "--bytes",
        "--directory",
        "--exclude",
        "--format",
        "--lines",
        "--max-depth",
        "--output",
        "--port",
        "--sort",
        "--time-style",
        "--unit",
        "--user",
    }
)


def infer_command_focus(
    command: str,
    *,
    starting_cwd: str,
    result_cwd: str,
) -> dict[str, JsonValue]:
    """Return bounded attention hints from one command without model work."""

    start = _bounded_text(starting_cwd)
    result = _bounded_text(result_cwd)
    executable, arguments = _command_parts(command)
    paths: list[str] = []
    artifacts: list[dict[str, JsonValue]] = []

    if executable in _FILESYSTEM_COMMANDS:
        path_values, path_patterns = _filesystem_focus(
            executable,
            arguments,
            starting_cwd=start,
            result_cwd=result,
        )
        paths.extend(path_values)
        artifacts.extend(
            {"kind": "path_pattern", "id": pattern} for pattern in path_patterns
        )
    elif executable in {"systemctl", "service", "journalctl"}:
        artifacts.extend(
            {"kind": "service", "id": value}
            for value in _service_focus(executable, arguments)
        )
    elif executable in _PROCESS_COMMANDS:
        artifacts.extend(
            {"kind": "process", "id": value} for value in _ordinary_operands(arguments)
        )
    elif executable in _NETWORK_COMMANDS:
        artifacts.extend(
            {"kind": "network_endpoint", "id": value}
            for value in _network_focus(executable, arguments)
        )
    elif executable in _PACKAGE_COMMANDS:
        artifacts.extend(
            {"kind": "package", "id": value}
            for value in _ordinary_operands(
                arguments,
                ignored=_PACKAGE_ACTIONS,
            )
        )
    elif executable in _DATABASE_COMMANDS:
        artifacts.extend(
            {"kind": "database", "id": value} for value in _ordinary_operands(arguments)
        )

    paths = _bounded_unique(paths, maximum=MAX_FOCUS_PATHS)
    artifacts = _bounded_artifacts(artifacts)
    primary: dict[str, JsonValue] | None = None
    if paths:
        primary = {"kind": "path", "id": paths[0]}
    elif artifacts:
        primary = dict(artifacts[0])
    elif result:
        primary = {"kind": "working_directory", "id": result}
    elif start:
        primary = {"kind": "working_directory", "id": start}

    focus: dict[str, JsonValue] = {}
    if start:
        focus["cwd"] = start
    if result:
        focus["result_cwd"] = result
    if paths:
        focus["paths"] = tuple(paths)
    if artifacts:
        focus["artifacts"] = tuple(artifacts)
    if primary is not None:
        focus["primary"] = primary
        focus["key"] = f"{primary['kind']}:{primary['id']}"
    return focus


def interaction_focus_key(value: object) -> str | None:
    """Return the stable bounded identity carried by an interaction focus."""

    if not isinstance(value, Mapping):
        return None
    key = value.get("key")
    if not isinstance(key, str) or not key.strip():
        return None
    return _bounded_text(key)


def interaction_focus_subject_key(value: object) -> str | None:
    """Return a concrete first-touch subject, excluding directory-only context."""

    if not isinstance(value, Mapping):
        return None
    primary = value.get("primary")
    if not isinstance(primary, Mapping):
        return None
    kind = _bounded_text(primary.get("kind"))
    identifier = _bounded_text(primary.get("id"))
    if not kind or not identifier or kind == "working_directory":
        return None
    return f"{kind}:{identifier}"


def classify_presence_probe(command: str) -> PresenceProbeKind | None:
    """Classify successful broad inspection commands without model work.

    Narrow selectors are intentionally excluded where they can be recognized:
    this signal describes a user looking for the surrounding population, not
    merely querying one already-known process or login.
    """

    executable, arguments = _command_parts(command)
    lowered = tuple(argument.casefold() for argument in arguments)

    if executable in {"who", "w", "users"}:
        if not _ordinary_operands(arguments):
            return PresenceProbeKind.SESSION_PRESENCE
        return None
    if executable in {"last", "lastlog"}:
        if not _ordinary_operands(arguments):
            return PresenceProbeKind.LOGIN_HISTORY
        return None
    if executable == "loginctl":
        actions = set(_ordinary_operands(arguments))
        if not actions or actions & {"list-sessions", "list-users"}:
            return PresenceProbeKind.SESSION_PRESENCE
        return None

    if executable == "ps":
        narrow = {
            "-p",
            "--pid",
            "--ppid",
            "-q",
            "--quick-pid",
            "-c",
            "--select",
        }
        if not any(
            argument in narrow
            or any(argument.startswith(f"{option}=") for option in narrow)
            for argument in lowered
        ):
            return PresenceProbeKind.PROCESS_ACTIVITY
        return None
    if executable == "pstree":
        if not _ordinary_operands(arguments):
            return PresenceProbeKind.PROCESS_ACTIVITY
        return None
    if executable in {"top", "htop"}:
        if not any(argument in {"-p", "--pid"} for argument in lowered):
            return PresenceProbeKind.PROCESS_ACTIVITY
        return None

    if executable in {"ss", "netstat"}:
        return PresenceProbeKind.NETWORK_ACTIVITY
    if executable == "lsof" and any(
        argument == "-i" or argument.startswith("-i") for argument in lowered
    ):
        return PresenceProbeKind.NETWORK_ACTIVITY

    if executable == "systemctl":
        actions = set(_ordinary_operands(arguments))
        if "list-timers" in lowered:
            return PresenceProbeKind.SCHEDULED_ACTIVITY
        if not actions or set(lowered) & {
            "list-units",
            "list-unit-files",
            "list-sockets",
        }:
            return PresenceProbeKind.SERVICE_ACTIVITY
        return None
    if executable == "service" and "--status-all" in lowered:
        return PresenceProbeKind.SERVICE_ACTIVITY
    if executable in {"atq", "batch"}:
        return PresenceProbeKind.SCHEDULED_ACTIVITY
    if executable == "crontab" and "-l" in lowered:
        return PresenceProbeKind.SCHEDULED_ACTIVITY
    if executable == "journalctl" and not any(
        argument in {"-u", "--unit", "_pid", "_comm"}
        or argument.startswith(("--unit=", "_pid=", "_comm="))
        for argument in lowered
    ):
        return PresenceProbeKind.EVENT_ACTIVITY
    return None


def interaction_focus_counts(value: object) -> tuple[int, int]:
    """Return deterministic path and artifact counts for low-cost logging."""

    if not isinstance(value, Mapping):
        return 0, 0
    paths = value.get("paths")
    artifacts = value.get("artifacts")
    return (
        len(paths) if isinstance(paths, (list, tuple)) else 0,
        len(artifacts) if isinstance(artifacts, (list, tuple)) else 0,
    )


def classify_interaction_focus_match(
    expected: object,
    observed: object,
) -> InteractionFocusMatch:
    """Classify bounded focus alignment after host/surface eligibility.

    Explicit concrete paths and typed artifact identifiers are subjects.
    Working-directory context is intentionally excluded: adjacent commands
    commonly share it without inspecting the same artifact. If neither an
    exact subject nor a meaningful path relationship exists, callers retain
    the coarse surface-only classification.
    """

    expected_paths, expected_patterns, expected_artifacts = _focus_subjects(expected)
    observed_paths, observed_patterns, observed_artifacts = _focus_subjects(observed)

    if _artifact_sets_intersect(expected_artifacts, observed_artifacts):
        return InteractionFocusMatch.ARTIFACT_EXACT
    if any(
        _paths_equal(expected_path, observed_path)
        for expected_path in expected_paths
        for observed_path in observed_paths
    ):
        return InteractionFocusMatch.ARTIFACT_EXACT
    if _artifact_sets_intersect(expected_patterns, observed_patterns):
        return InteractionFocusMatch.ARTIFACT_EXACT

    if any(
        _paths_are_related(expected_path, observed_path)
        for expected_path in expected_paths
        for observed_path in observed_paths
    ):
        return InteractionFocusMatch.PATH_RELATED
    if _pattern_matches_any_path(expected_patterns, observed_paths):
        return InteractionFocusMatch.PATH_RELATED
    if _pattern_matches_any_path(observed_patterns, expected_paths):
        return InteractionFocusMatch.PATH_RELATED
    return InteractionFocusMatch.SURFACE_ONLY


def _focus_subjects(
    value: object,
) -> tuple[list[str], set[tuple[str, str]], set[tuple[str, str]]]:
    """Return bounded concrete paths, patterns, and non-path artifacts."""

    if not isinstance(value, Mapping):
        return [], set(), set()

    paths: list[str] = []
    raw_paths = value.get("paths")
    if isinstance(raw_paths, (list, tuple)):
        paths = _bounded_unique(
            [path for path in raw_paths if isinstance(path, str)],
            maximum=MAX_FOCUS_PATHS,
        )

    patterns: set[tuple[str, str]] = set()
    artifacts: set[tuple[str, str]] = set()
    raw_artifacts = value.get("artifacts")
    if isinstance(raw_artifacts, (list, tuple)):
        for artifact in raw_artifacts[:MAX_FOCUS_ARTIFACTS]:
            _add_focus_artifact(artifact, patterns=patterns, artifacts=artifacts)

    primary = value.get("primary")
    if isinstance(primary, Mapping):
        kind = _bounded_text(primary.get("kind"))
        identifier = _bounded_text(primary.get("id"))
        if kind == "path" and identifier and identifier not in paths:
            paths = _bounded_unique(
                [*paths, identifier],
                maximum=MAX_FOCUS_PATHS,
            )
        elif kind != "working_directory":
            _add_focus_artifact(
                primary,
                patterns=patterns,
                artifacts=artifacts,
            )
    return paths, patterns, artifacts


def _add_focus_artifact(
    value: object,
    *,
    patterns: set[tuple[str, str]],
    artifacts: set[tuple[str, str]],
) -> None:
    if not isinstance(value, Mapping):
        return
    kind = _bounded_text(value.get("kind"))
    identifier = _bounded_text(value.get("id"))
    if not kind or not identifier or kind in {"path", "working_directory"}:
        return
    target = patterns if kind == "path_pattern" else artifacts
    subject = (kind, identifier)
    if subject in target or len(patterns) + len(artifacts) < MAX_FOCUS_ARTIFACTS:
        target.add(subject)


def _artifact_sets_intersect(
    expected: set[tuple[str, str]],
    observed: set[tuple[str, str]],
) -> bool:
    for expected_kind, expected_identifier in expected:
        for observed_kind, observed_identifier in observed:
            if expected_kind != observed_kind:
                continue
            if expected_kind == "path_pattern":
                if _paths_equal(expected_identifier, observed_identifier):
                    return True
            elif expected_identifier == observed_identifier:
                return True
    return False


def _paths_equal(expected: str, observed: str) -> bool:
    expected_path = _absolute_path(expected)
    observed_path = _absolute_path(observed)
    if expected_path is None or observed_path is None:
        return expected == observed
    if type(expected_path) is not type(observed_path):
        return False
    return expected_path == observed_path


def _paths_are_related(expected: str, observed: str) -> bool:
    expected_path = _absolute_path(expected)
    observed_path = _absolute_path(observed)
    if (
        expected_path is None
        or observed_path is None
        or type(expected_path) is not type(observed_path)
        or expected_path == observed_path
    ):
        return False
    if expected_path in observed_path.parents:
        return expected_path.parent != expected_path
    if observed_path in expected_path.parents:
        return observed_path.parent != observed_path
    return False


def _pattern_matches_any_path(
    patterns: set[tuple[str, str]],
    paths: Sequence[str],
) -> bool:
    return any(
        _path_matches_pattern(path, pattern)
        for kind, pattern in patterns
        if kind == "path_pattern"
        for path in paths
    )


def _path_matches_pattern(path: str, pattern: str) -> bool:
    concrete = _absolute_path(path)
    pattern_path = _absolute_path(pattern)
    if (
        concrete is None
        or pattern_path is None
        or type(concrete) is not type(pattern_path)
    ):
        return False
    try:
        return concrete.match(str(pattern_path))
    except ValueError:
        return False


def _absolute_path(value: str) -> PurePosixPath | PureWindowsPath | None:
    windows = PureWindowsPath(value)
    if windows.is_absolute():
        return windows
    posix = PurePosixPath(value)
    if posix.is_absolute():
        return posix
    return None


def _command_parts(command: str) -> tuple[str, tuple[str, ...]]:
    # A complete line has one aggregate status and may change directory/host
    # between segments; no segment can own its result-derived attention hints.
    if analyze_command(command).kind != "simple":
        return "", ()
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "", ()
    executable_index: int | None = None
    for index, token in enumerate(tokens):
        if _ASSIGNMENT.match(token):
            continue
        executable_index = index
        break
    if executable_index is None:
        return "", ()
    executable = tokens[executable_index].rsplit("/", maxsplit=1)[-1].lower()
    return executable, tuple(tokens[executable_index + 1 :])


def _filesystem_focus(
    executable: str,
    arguments: Sequence[str],
    *,
    starting_cwd: str,
    result_cwd: str,
) -> tuple[list[str], list[str]]:
    if executable == "pwd":
        return ([result_cwd] if result_cwd else []), []
    operands = _ordinary_operands(arguments)
    if executable == "cd" and result_cwd:
        return [result_cwd], []
    paths: list[str] = []
    patterns: list[str] = []
    for operand in operands:
        normalized = _normalize_path(operand, starting_cwd)
        if normalized is None:
            continue
        if any(marker in normalized for marker in _GLOB_MARKERS):
            patterns.append(normalized)
        else:
            paths.append(normalized)
    return paths, patterns


def _service_focus(executable: str, arguments: Sequence[str]) -> list[str]:
    if executable == "service":
        operands = _ordinary_operands(arguments)
        return operands[:1]
    if executable == "journalctl":
        units: list[str] = []
        for index, token in enumerate(arguments[:-1]):
            if token in {"-u", "--unit"}:
                units.append(arguments[index + 1])
        return _bounded_unique(units, maximum=MAX_FOCUS_ARTIFACTS)
    return _ordinary_operands(arguments, ignored=_SYSTEMCTL_ACTIONS)


def _network_focus(executable: str, arguments: Sequence[str]) -> list[str]:
    operands = _ordinary_operands(arguments)
    if executable == "ssh":
        return operands[:1]
    if executable == "dig":
        ignored = {
            "A",
            "AAAA",
            "ANY",
            "CNAME",
            "MX",
            "NS",
            "PTR",
            "SOA",
            "SRV",
            "TXT",
        }
        return [value for value in operands if value.upper() not in ignored]
    return operands


def _ordinary_operands(
    arguments: Sequence[str],
    *,
    ignored: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    values: list[str] = []
    skip_next = False
    options_ended = False
    for token in arguments:
        if skip_next:
            skip_next = False
            continue
        if not options_ended and token == "--":
            options_ended = True
            continue
        if not options_ended and token.startswith("-"):
            if token in _OPTIONS_WITH_VALUES and "=" not in token:
                skip_next = True
            continue
        if token in ignored or not token.strip():
            continue
        values.append(token)
    return _bounded_unique(values, maximum=MAX_FOCUS_ARTIFACTS)


def _normalize_path(value: str, cwd: str) -> str | None:
    text = _bounded_text(value)
    if not text or text in {"-", "~"} or text.startswith(("~/", "~\\")):
        return None
    windows = PureWindowsPath(text).is_absolute() or (
        bool(cwd) and PureWindowsPath(cwd).is_absolute()
    )
    if windows:
        normalized = ntpath.normpath(
            text if ntpath.isabs(text) else ntpath.join(cwd, text)
        )
    else:
        normalized = posixpath.normpath(
            text if PurePosixPath(text).is_absolute() else posixpath.join(cwd, text)
        )
    return _bounded_text(normalized)


def _bounded_artifacts(
    artifacts: Sequence[dict[str, JsonValue]],
) -> list[dict[str, JsonValue]]:
    retained: list[dict[str, JsonValue]] = []
    seen: set[tuple[str, str]] = set()
    for artifact in artifacts:
        kind = artifact.get("kind")
        identifier = artifact.get("id")
        if not isinstance(kind, str) or not isinstance(identifier, str):
            continue
        bounded_kind = _bounded_text(kind)
        bounded_identifier = _bounded_text(identifier)
        if not bounded_kind or not bounded_identifier:
            continue
        key = (bounded_kind, bounded_identifier)
        if key in seen:
            continue
        seen.add(key)
        retained.append({"kind": bounded_kind, "id": bounded_identifier})
        if len(retained) >= MAX_FOCUS_ARTIFACTS:
            break
    return retained


def _bounded_unique(values: Sequence[str], *, maximum: int) -> list[str]:
    retained: list[str] = []
    seen: set[str] = set()
    for value in values:
        bounded = _bounded_text(value)
        if not bounded or bounded in seen:
            continue
        seen.add(bounded)
        retained.append(bounded)
        if len(retained) >= maximum:
            break
    return retained


def _bounded_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_FOCUS_TEXT]


__all__ = [
    "InteractionFocusMatch",
    "MAX_FOCUS_ARTIFACTS",
    "MAX_FOCUS_PATHS",
    "PresenceProbeKind",
    "classify_presence_probe",
    "classify_interaction_focus_match",
    "infer_command_focus",
    "interaction_focus_counts",
    "interaction_focus_key",
    "interaction_focus_subject_key",
]
