"""Strict, bounded foreground observations for asynchronous enrichment.

Command history is a record of what the foreground user saw.  Most command
output remains useful but non-authoritative context because arbitrary shell
syntax and model-rendered text cannot be interpreted safely.  This module
recognizes only a deliberately small set of standalone commands whose output
has a useful temporal or inventory meaning.  Ambiguous commands simply produce
no typed evidence.

The public payload is bounded for model input.  The private digest is computed
from the latest recognized command record for every exhaustive evidence scope
across the complete history, before output is trimmed.  Public evidence can
therefore age out without changing authority, while a new observation of any
recognized scope still invalidates stale work.  Later code can use the digest
as an optimistic-concurrency dependency without exposing raw values in logs.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

FOREGROUND_EVIDENCE_SCHEMA_VERSION: Final[int] = 1
FOREGROUND_EVIDENCE_POLICY_VERSION: Final[str] = "foreground-evidence-v2"
FOREGROUND_EVIDENCE_AUTHORITY_DOMAIN: Final[Literal["foreground_evidence"]] = (
    "foreground_evidence"
)
DEFAULT_RECENT_EXHAUSTIVE_HISTORY_LIMIT: Final[int] = 4
HOST_TIME_OUTPUT_CHAR_LIMIT: Final[int] = 1_024
EXHAUSTIVE_OUTPUT_CHAR_LIMIT: Final[int] = 1_500
MAX_RECENT_EXHAUSTIVE_OBSERVATIONS: Final[int] = 4

_TRIM_MARKER: Final[str] = "\n…output trimmed…\n"
_SHELL_CONTROL = re.compile(r"[|;&<>`]|\$\(|[\r\n]")
_WTMP_BEGINS = re.compile(r"^wtmp\s+begins\s+.+$", re.IGNORECASE)
_SAFE_DATE_LONG_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "--iso-8601",
        "--rfc-email",
        "--universal",
        "--utc",
    }
)
_SAFE_DATE_LONG_PREFIXES: Final[tuple[str, ...]] = (
    "--iso-8601=",
    "--rfc-3339=",
)
_SAFE_DATE_SHORT_OPTIONS: Final[frozenset[str]] = frozenset({"-I", "-R", "-u"})
_CHRONOLOGY_ANCHOR_DATE_CONVERSIONS: Final[frozenset[str]] = frozenset(
    {
        "c",
        "C",
        "D",
        "F",
        "g",
        "G",
        "s",
        "x",
        "y",
        "Y",
        "+",
    }
)
_SAFE_LS_LONG_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "--all",
        "--almost-all",
        "--author",
        "--escape",
        "--file-type",
        "--group-directories-first",
        "--human-readable",
        "--inode",
        "--literal",
        "--numeric-uid-gid",
        "--quote-name",
        "--reverse",
        "--size",
    }
)
_SAFE_LS_LONG_PREFIXES: Final[tuple[str, ...]] = (
    "--sort=",
    "--time=",
    "--time-style=",
)
_SAFE_LS_SHORT_FLAGS: Final[frozenset[str]] = frozenset("1AaFghilNnoQqrSsTtuU")
_SS_DISPLAY_FLAGS: Final[frozenset[str]] = frozenset("HnpoemirZz")
_SS_SCOPE_FLAGS: Final[frozenset[str]] = frozenset("altuxwdS46")
_SS_LONG_DISPLAY_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "--bpf-maps",
        "--context",
        "--contexts",
        "--extended",
        "--info",
        "--memory",
        "--no-header",
        "--numeric",
        "--options",
        "--processes",
        "--resolve",
        "--tos",
    }
)
_SS_LONG_SCOPE_OPTIONS: Final[dict[str, str]] = {
    "--all": "a",
    "--dccp": "d",
    "--family=inet": "4",
    "--family=inet6": "6",
    "--ipv4": "4",
    "--ipv6": "6",
    "--listening": "l",
    "--raw": "w",
    "--sctp": "S",
    "--tcp": "t",
    "--udp": "u",
    "--unix": "x",
}
_PS_EXHAUSTIVE_ARGUMENTS: Final[frozenset[tuple[str, ...]]] = frozenset(
    {
        ("-A",),
        ("-e",),
        ("-e", "-f"),
        ("-eF",),
        ("-ef",),
        ("ax",),
        ("aux",),
    }
)


@dataclass(frozen=True, slots=True)
class ForegroundEvidenceObservation:
    """One bounded, strictly recognized foreground observation."""

    kind: str
    command_id: str
    command: str
    coverage: tuple[tuple[str, str], ...]
    output_excerpt: str
    output_omitted_chars: int = 0

    def public_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "source": "recorded_command_result",
            "command_id": self.command_id,
            "command": self.command,
            "coverage": dict(self.coverage),
            "output_excerpt": self.output_excerpt,
        }
        if self.output_omitted_chars:
            payload["output_omitted_chars"] = self.output_omitted_chars
        return payload


@dataclass(frozen=True, slots=True)
class ForegroundEvidenceSnapshot:
    """Compact public evidence plus a digest of complete recognized records."""

    host_time: ForegroundEvidenceObservation | None
    recent_exhaustive: tuple[ForegroundEvidenceObservation, ...]
    private_sha256: str = field(repr=False)
    schema_version: int = FOREGROUND_EVIDENCE_SCHEMA_VERSION

    @property
    def host_time_observed(self) -> bool:
        return self.host_time is not None

    @property
    def recent_exhaustive_count(self) -> int:
        return len(self.recent_exhaustive)

    def host_time_payload(self) -> dict[str, object]:
        if self.host_time is None:
            return {
                "v": self.schema_version,
                "status": "unknown",
            }
        return {
            "v": self.schema_version,
            "status": "observed",
            **self.host_time.public_payload(),
        }

    def recent_exhaustive_payload(self) -> list[dict[str, object]]:
        return [item.public_payload() for item in self.recent_exhaustive]


def capture_foreground_evidence(
    host: Mapping[str, object],
    *,
    recent_history_limit: int = DEFAULT_RECENT_EXHAUSTIVE_HISTORY_LIMIT,
) -> ForegroundEvidenceSnapshot:
    """Capture a strict evidence view from one trusted target-host record.

    Host time uses the newest recognized observation in the complete history.
    Public exhaustive observations use only the recent suffix because a listing
    or snapshot is bounded negative evidence rather than a permanent absence
    fact.  Private authority separately retains the latest recognized record
    for each exhaustive scope across the complete history.  This prevents the
    passage of unrelated commands from looking like an authority change while
    ensuring a replacement or newly observed scope invalidates stale work.
    """

    if recent_history_limit < 0:
        raise ValueError("recent_history_limit must be zero or greater")
    raw_history = host.get("command_history")
    history = (
        [item for item in raw_history if isinstance(item, Mapping)]
        if isinstance(raw_history, list)
        else []
    )

    host_time_pair: tuple[ForegroundEvidenceObservation, dict[str, object]] | None = (
        None
    )
    for entry in reversed(history):
        pair = _host_time_observation(entry)
        if pair is not None:
            host_time_pair = pair
            break

    authority_pairs_by_scope: dict[
        tuple[str, tuple[tuple[str, str], ...]],
        tuple[ForegroundEvidenceObservation, dict[str, object]],
    ] = {}
    for entry in reversed(history):
        pair = _exhaustive_observation(entry)
        if pair is None:
            continue
        observation, _private_payload = pair
        scope = (observation.kind, observation.coverage)
        if scope not in authority_pairs_by_scope:
            authority_pairs_by_scope[scope] = pair

    recent_pairs: list[tuple[ForegroundEvidenceObservation, dict[str, object]]] = []
    seen_scopes: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    if recent_history_limit:
        for entry in reversed(history[-recent_history_limit:]):
            pair = _exhaustive_observation(entry)
            if pair is None:
                continue
            observation, private_payload = pair
            scope = (observation.kind, observation.coverage)
            if scope in seen_scopes:
                continue
            seen_scopes.add(scope)
            recent_pairs.append(pair)
            if len(recent_pairs) == MAX_RECENT_EXHAUSTIVE_OBSERVATIONS:
                break

    private_items: list[dict[str, object]] = []
    if host_time_pair is not None:
        private_items.append({"role": "host_time", **host_time_pair[1]})
    for scope in sorted(authority_pairs_by_scope):
        _observation, private_payload = authority_pairs_by_scope[scope]
        private_items.append({"role": "latest_exhaustive_scope", **private_payload})
    # The empty view is still an authoritative observation: a first recognized
    # foreground command arriving after the Integrator snapshot must invalidate
    # a patch just like a change between two non-empty views.
    digest = _sha256_payload(private_items)
    return ForegroundEvidenceSnapshot(
        host_time=(host_time_pair[0] if host_time_pair is not None else None),
        recent_exhaustive=tuple(pair[0] for pair in recent_pairs),
        private_sha256=digest,
    )


def _host_time_observation(
    entry: Mapping[str, object],
) -> tuple[ForegroundEvidenceObservation, dict[str, object]] | None:
    common = _successful_entry(entry)
    if common is None:
        return None
    command_id, command, stdout = common
    parts = _standalone_parts(command)
    if parts is None:
        return None
    executable, arguments = parts
    coverage: tuple[tuple[str, str], ...]
    if executable == "date":
        if not _safe_date_arguments(arguments):
            return None
        coverage = (("clock", "host_reported"),)
    elif executable == "timedatectl":
        if arguments not in ((), ("status",)):
            return None
        coverage = (("clock", "host_reported"), ("timezone", "included_if_shown"))
    else:
        return None
    if not stdout.strip():
        return None
    return _observation_pair(
        kind="recorded_host_time",
        command_id=command_id,
        command=command,
        coverage=coverage,
        stdout=stdout,
        output_limit=HOST_TIME_OUTPUT_CHAR_LIMIT,
    )


def _exhaustive_observation(
    entry: Mapping[str, object],
) -> tuple[ForegroundEvidenceObservation, dict[str, object]] | None:
    common = _successful_entry(entry)
    if common is None:
        return None
    command_id, command, stdout = common
    if len(stdout) > EXHAUSTIVE_OUTPUT_CHAR_LIMIT:
        # A partial inventory cannot establish exhaustive negative evidence.
        return None
    parts = _standalone_parts(command)
    if parts is None:
        return None
    executable, arguments = parts

    kind: str
    coverage: tuple[tuple[str, str], ...]
    if executable == "ls":
        ls_coverage = _home_ls_coverage(arguments)
        if ls_coverage is None:
            return None
        kind = "home_directory_inventory"
        coverage = ls_coverage
    elif executable == "last":
        if arguments or not _complete_last_output(stdout):
            return None
        kind = "login_history"
        coverage = (("database", "wtmp"), ("horizon", "reported_by_command"))
    elif executable in {"w", "who", "users"}:
        if arguments:
            return None
        kind = "session_presence"
        coverage = (("sessions", "current"),)
    elif executable == "ss":
        ss_coverage = _ss_coverage(arguments)
        if ss_coverage is None:
            return None
        kind = "socket_snapshot"
        coverage = ss_coverage
    elif executable == "ps":
        if arguments not in _PS_EXHAUSTIVE_ARGUMENTS:
            return None
        kind = "process_snapshot"
        coverage = (("processes", "all"),)
    else:
        return None

    return _observation_pair(
        kind=kind,
        command_id=command_id,
        command=command,
        coverage=coverage,
        stdout=stdout,
        output_limit=EXHAUSTIVE_OUTPUT_CHAR_LIMIT,
    )


def _successful_entry(
    entry: Mapping[str, object],
) -> tuple[str, str, str] | None:
    command_id = entry.get("id")
    command = entry.get("command")
    stdout = entry.get("stdout")
    stderr = entry.get("stderr", "")
    exit_status = entry.get("exit_status")
    if (
        not isinstance(command_id, str)
        or not command_id.strip()
        or not isinstance(command, str)
        or not command.strip()
        or not isinstance(stdout, str)
        or not isinstance(stderr, str)
        or stderr.strip()
        or type(exit_status) is not int
        or exit_status != 0
        or bool(entry.get("interrupted", False))
        or entry.get("signal") is not None
        or _looks_like_structured_response_envelope(stdout)
    ):
        return None
    return command_id, command.strip(), stdout


def _looks_like_structured_response_envelope(value: str) -> bool:
    stripped = value.lstrip()
    if not stripped.startswith("{"):
        return False
    prefix = stripped[:1_024].casefold()
    return any(
        marker in prefix
        for marker in ('"stdout"', '"stderr"', '"exit_status"', '"state_update"')
    )


def _standalone_parts(command: str) -> tuple[str, tuple[str, ...]] | None:
    if _SHELL_CONTROL.search(command):
        return None
    try:
        values = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not values:
        return None
    executable = posixpath.basename(values[0]).casefold()
    return executable, tuple(values[1:])


def _safe_date_arguments(arguments: Sequence[str]) -> bool:
    for argument in arguments:
        if argument.startswith("+") and _date_format_has_chronology_anchor(
            argument[1:]
        ):
            continue
        if argument.startswith("-I") and argument[2:] in {
            "",
            "date",
            "hours",
            "minutes",
            "seconds",
            "ns",
        }:
            continue
        if argument in _SAFE_DATE_SHORT_OPTIONS or argument in _SAFE_DATE_LONG_OPTIONS:
            continue
        if any(argument.startswith(prefix) for prefix in _SAFE_DATE_LONG_PREFIXES):
            continue
        return False
    return True


def _date_format_has_chronology_anchor(value: str) -> bool:
    """Accept a format only when it establishes calendar or epoch position.

    GNU ``date`` permits arbitrary literal formats such as ``+hello``. Those
    commands are safe to execute, but their output is not a chronology anchor.
    Time-of-day, month/day, timezone, and layout conversions alone are also
    insufficient for the absolute chronology semantics of ``host_time``. This
    scanner requires a year-, calendar-date-, locale-date-, or epoch-bearing
    conversion while supporting flags, width, and ``E``/``O`` modifiers.
    """

    index = 0
    while index < len(value):
        marker = value.find("%", index)
        if marker < 0 or marker + 1 >= len(value):
            return False
        index = marker + 1
        if value[index] == "%":
            index += 1
            continue
        while index < len(value) and value[index] in "-_0^#:":
            index += 1
        while index < len(value) and value[index].isdigit():
            index += 1
        if index < len(value) and value[index] in "EO":
            index += 1
        if index < len(value) and value[index] in _CHRONOLOGY_ANCHOR_DATE_CONVERSIONS:
            return True
        index += 1
    return False


def _home_ls_coverage(
    arguments: Sequence[str],
) -> tuple[tuple[str, str], ...] | None:
    operands: list[str] = []
    include_hidden = False
    options_ended = False
    for argument in arguments:
        if not options_ended and argument == "--":
            options_ended = True
            continue
        if not options_ended and argument.startswith("--"):
            if argument in {"--all", "--almost-all"}:
                include_hidden = True
            elif argument not in _SAFE_LS_LONG_OPTIONS and not any(
                argument.startswith(prefix) for prefix in _SAFE_LS_LONG_PREFIXES
            ):
                return None
            continue
        if not options_ended and argument.startswith("-") and argument != "-":
            flags = argument[1:]
            if not flags or any(flag not in _SAFE_LS_SHORT_FLAGS for flag in flags):
                return None
            if "a" in flags or "A" in flags:
                include_hidden = True
            continue
        operands.append(argument)
    if len(operands) != 1 or operands[0].rstrip("/") != "/home":
        return None
    return (
        ("path", "/home"),
        ("entries", "including_hidden" if include_hidden else "non_hidden"),
        ("depth", "immediate_children"),
    )


def _complete_last_output(stdout: str) -> bool:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return bool(lines and _WTMP_BEGINS.fullmatch(lines[-1]))


def _ss_coverage(
    arguments: Sequence[str],
) -> tuple[tuple[str, str], ...] | None:
    scope_flags: set[str] = set()
    for argument in arguments:
        if argument.startswith("--"):
            scope = _SS_LONG_SCOPE_OPTIONS.get(argument)
            if scope is not None:
                scope_flags.add(scope)
                continue
            if argument not in _SS_LONG_DISPLAY_OPTIONS:
                return None
            continue
        if not argument.startswith("-") or argument == "-":
            return None
        flags = argument[1:]
        if not flags or any(
            flag not in _SS_DISPLAY_FLAGS and flag not in _SS_SCOPE_FLAGS
            for flag in flags
        ):
            return None
        scope_flags.update(flag for flag in flags if flag in _SS_SCOPE_FLAGS)

    if "a" in scope_flags:
        states = "all"
    elif "l" in scope_flags:
        states = "listening"
    else:
        states = "connected"
    protocol_names = {
        "t": "tcp",
        "u": "udp",
        "x": "unix",
        "w": "raw",
        "d": "dccp",
        "S": "sctp",
    }
    protocols = [name for flag, name in protocol_names.items() if flag in scope_flags]
    if "4" in scope_flags and "6" in scope_flags:
        family = "ipv4_ipv6"
    elif "4" in scope_flags:
        family = "ipv4"
    elif "6" in scope_flags:
        family = "ipv6"
    else:
        family = "all"
    return (
        ("states", states),
        ("protocols", ",".join(protocols) if protocols else "default"),
        ("family", family),
    )


def _observation_pair(
    *,
    kind: str,
    command_id: str,
    command: str,
    coverage: tuple[tuple[str, str], ...],
    stdout: str,
    output_limit: int,
) -> tuple[ForegroundEvidenceObservation, dict[str, object]]:
    excerpt, omitted = _bounded_excerpt(stdout, output_limit)
    observation = ForegroundEvidenceObservation(
        kind=kind,
        command_id=command_id,
        command=command,
        coverage=coverage,
        output_excerpt=excerpt,
        output_omitted_chars=omitted,
    )
    private_payload = {
        "v": FOREGROUND_EVIDENCE_SCHEMA_VERSION,
        "kind": kind,
        "command_id": command_id,
        "command": command,
        "coverage": dict(coverage),
        "stdout": stdout,
    }
    return observation, private_payload


def _bounded_excerpt(value: str, limit: int) -> tuple[str, int]:
    if len(value) <= limit:
        return value, 0
    content_budget = limit - len(_TRIM_MARKER)
    if content_budget <= 0:
        return value[:limit], len(value) - limit
    head_chars = (content_budget + 1) // 2
    tail_chars = content_budget - head_chars
    excerpt = value[:head_chars] + _TRIM_MARKER
    if tail_chars:
        excerpt += value[-tail_chars:]
    return excerpt, len(value) - head_chars - tail_chars


def _sha256_payload(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DEFAULT_RECENT_EXHAUSTIVE_HISTORY_LIMIT",
    "EXHAUSTIVE_OUTPUT_CHAR_LIMIT",
    "FOREGROUND_EVIDENCE_AUTHORITY_DOMAIN",
    "FOREGROUND_EVIDENCE_POLICY_VERSION",
    "FOREGROUND_EVIDENCE_SCHEMA_VERSION",
    "HOST_TIME_OUTPUT_CHAR_LIMIT",
    "MAX_RECENT_EXHAUSTIVE_OBSERVATIONS",
    "ForegroundEvidenceObservation",
    "ForegroundEvidenceSnapshot",
    "capture_foreground_evidence",
]
