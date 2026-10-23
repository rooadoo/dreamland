"""Bounded foreground authority shared by asynchronous background agents.

The live session frame is more authoritative about the user's current terminal
context than asynchronously generated world detail.  This module captures that
small, typed observation without exposing arbitrary environment, metadata, or
command-history content.  Callers can give the public payload to a model and use
the stable session-semantics digest for deterministic race checks.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from typing import Final

from dreamland.models import SessionState
from dreamland.session_state import active_shell_frame
from dreamland.world_state import active_host_ref

FOREGROUND_AUTHORITY_SCHEMA_VERSION: Final[int] = 1
FOREGROUND_AUTHORITY_CLASS: Final[str] = "foreground_observation"
SESSION_SEMANTICS_AUTHORITY_DOMAIN: Final[str] = "active_session"

_SELECTED_SHELL_VARIABLES: Final[tuple[str, ...]] = (
    "SHELL",
    "USER",
    "LOGNAME",
    "HOME",
    "HOSTNAME",
)
_MAX_IDENTITY_CHARS: Final[int] = 256
_MAX_PATH_CHARS: Final[int] = 1024
_MAX_COMMAND_CHARS: Final[int] = 512
_MAX_PROMPT_CHARS: Final[int] = 512
_MAX_ARGUMENTS: Final[int] = 12
_MAX_ARGUMENT_CHARS: Final[int] = 256
_MAX_ENVIRONMENT_VALUE_CHARS: Final[int] = 1024
_REDACTED_ARGUMENT: Final[str] = "<redacted-credential-argument>"
_REDACTED_ASSIGNMENT: Final[str] = "<redacted-credential-assignment>"
_REDACTED_URI_SECRET: Final[str] = "<redacted-credential-secret>"
_SENSITIVE_ARGUMENT = re.compile(
    r"(?:^|[^a-z0-9])(?:api[-_]?key|credential|pass(?:word|wd)?|secret|token)\s*[=:]",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_NAME_PATTERN: Final[str] = (
    r"(?:[a-z0-9_]*(?:api_?key|access_?key(?:_id)?|auth_?token|"
    r"bearer_?token|client_?secret|credential|password|passwd|passphrase|"
    r"private_?key|secret|token)[a-z0-9_]*|mysql_pwd|pgpassword|"
    r"rediscli_auth)"
)
_SENSITIVE_ASSIGNMENT_NAME = re.compile(
    rf"{_SENSITIVE_ASSIGNMENT_NAME_PATTERN}\Z",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT = re.compile(
    rf"(?P<name>{_SENSITIVE_ASSIGNMENT_NAME_PATTERN})="
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s]*)",
    re.IGNORECASE,
)
_CREDENTIAL_URI = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)"
    r"(?P<username>[^:/@\s]*):(?P<password>[^/@\s]*)@",
    re.IGNORECASE,
)
_SENSITIVE_LONG_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--api-key",
        "--apikey",
        "--access-token",
        "--auth-token",
        "--bearer-token",
        "--client-secret",
        "--credential",
        "--pass",
        "--passwd",
        "--password",
        "--private-key",
        "--secret",
        "--secret-access-key",
        "--secret-key",
        "--token",
    }
)
_SENSITIVE_SHORT_FLAGS_BY_PROGRAM: Final[dict[str, frozenset[str]]] = {
    "curl": frozenset({"-u"}),
    "mariadb": frozenset({"-p"}),
    "mysql": frozenset({"-p"}),
    "redis-cli": frozenset({"-a"}),
    "sshpass": frozenset({"-p"}),
}
_SENSITIVE_LONG_FLAGS_BY_PROGRAM: Final[dict[str, frozenset[str]]] = {
    "curl": frozenset({"--user"}),
}


@dataclass(frozen=True, slots=True, repr=False)
class ForegroundAuthoritySnapshot:
    """Immutable, bounded evidence about the active session frame.

    ``cwd`` is useful prompt evidence but is deliberately absent from
    :meth:`session_semantics_payload`.  Ordinary directory movement therefore
    does not invalidate work whose safety depends only on the active login and
    program semantics.
    """

    target_host: str
    hostname: str
    active_username: str
    login_username: str
    cwd: str
    program_name: str
    program_command: str
    program_argv: tuple[str, ...]
    program_prompt_template: str | None
    shell_environment: tuple[tuple[str, str], ...]
    shell_stack_depth: int

    def public_payload(self) -> dict[str, object]:
        """Return detached, model-safe foreground evidence."""

        payload = self.session_semantics_payload()
        payload["cwd"] = _bounded_text(self.cwd, _MAX_PATH_CHARS)
        return payload

    def session_semantics_payload(self) -> dict[str, object]:
        """Return the stable fields that govern active session semantics."""

        return {
            "v": FOREGROUND_AUTHORITY_SCHEMA_VERSION,
            "class": FOREGROUND_AUTHORITY_CLASS,
            "target_host": _bounded_text(self.target_host, _MAX_IDENTITY_CHARS),
            "hostname": _bounded_text(self.hostname, _MAX_IDENTITY_CHARS),
            "active_username": _bounded_text(
                self.active_username,
                _MAX_IDENTITY_CHARS,
            ),
            "login_username": _bounded_text(
                self.login_username,
                _MAX_IDENTITY_CHARS,
            ),
            "interactive_program": {
                "name": _bounded_text(self.program_name, _MAX_IDENTITY_CHARS),
                "command": _sanitized_program_command(
                    self.program_name,
                    self.program_command,
                ),
                "argv": list(
                    _sanitized_program_argv(
                        self.program_name,
                        self.program_argv,
                    )
                ),
                "prompt_template": (
                    _bounded_text(
                        _redact_inline_credentials(self.program_prompt_template),
                        _MAX_PROMPT_CHARS,
                    )
                    if self.program_prompt_template is not None
                    else None
                ),
            },
            "shell_environment": {
                name: _bounded_text(value, _MAX_ENVIRONMENT_VALUE_CHARS)
                for name, value in self.shell_environment
                if name in _SELECTED_SHELL_VARIABLES
            },
            "shell_stack_depth": self.shell_stack_depth,
        }

    @property
    def session_semantics_sha256(self) -> str:
        """Fingerprint the active login/program contract, excluding ``cwd``."""

        encoded = json.dumps(
            self._private_session_semantics_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _private_session_semantics_payload(self) -> dict[str, object]:
        """Return complete sanitized semantics for the hidden race digest.

        Public count and length limits must not make a later program argument
        invisible to optimistic concurrency checks. Credential-bearing values
        remain replaced before hashing so the digest is not a credential
        verifier if it is accidentally exposed by future instrumentation.
        """

        return {
            "v": FOREGROUND_AUTHORITY_SCHEMA_VERSION,
            "class": FOREGROUND_AUTHORITY_CLASS,
            "target_host": self.target_host,
            "hostname": self.hostname,
            "active_username": self.active_username,
            "login_username": self.login_username,
            "interactive_program": {
                "name": self.program_name,
                "command": _sanitized_program_command(
                    self.program_name,
                    self.program_command,
                    maximum=None,
                ),
                "argv": list(
                    _sanitized_program_argv(
                        self.program_name,
                        self.program_argv,
                        maximum_arguments=None,
                        maximum_argument_chars=None,
                    )
                ),
                "prompt_template": (
                    _redact_inline_credentials(self.program_prompt_template)
                    if self.program_prompt_template is not None
                    else None
                ),
            },
            "shell_environment": {
                name: value
                for name, value in self.shell_environment
                if name in _SELECTED_SHELL_VARIABLES
            },
            "shell_stack_depth": self.shell_stack_depth,
        }


def capture_foreground_authority(
    session: SessionState,
    *,
    target_host: str,
) -> ForegroundAuthoritySnapshot | None:
    """Capture the active frame only when it belongs to ``target_host``.

    The caller owns synchronization around ``session``.  Enrichment and other
    background runtimes should invoke this while holding the same short session
    lock used to copy world state.
    """

    if not isinstance(target_host, str) or not target_host.strip():
        raise ValueError("target_host must be non-empty text")
    resolved_target = target_host.strip()
    if active_host_ref(session.world_state) != resolved_target:
        return None

    frame = active_shell_frame(session)
    program = frame.interactive_program
    variables = frame.shell_environment.variables
    selected_environment = tuple(
        (name, str(variables[name]))
        for name in _SELECTED_SHELL_VARIABLES
        if name in variables
    )
    return ForegroundAuthoritySnapshot(
        target_host=resolved_target,
        hostname=frame.host_identity.hostname,
        active_username=frame.username,
        login_username=session.login_username or session.username,
        cwd=frame.cwd,
        program_name=program.name,
        program_command=program.command,
        program_argv=tuple(program.argv),
        program_prompt_template=program.prompt_template,
        shell_environment=selected_environment,
        shell_stack_depth=max(1, len(session.shell_stack)),
    )


def _sanitized_program_command(
    program_name: str,
    value: str,
    *,
    maximum: int | None = _MAX_COMMAND_CHARS,
) -> str:
    try:
        tokens = tuple(shlex.split(value, posix=True))
    except ValueError:
        tokens = tuple(value.split())
    sanitized = " ".join(
        _sanitized_program_argv(
            program_name,
            tokens,
            maximum_arguments=None,
            maximum_argument_chars=None,
        )
    )
    if maximum is None:
        return sanitized
    return _bounded_text(sanitized, maximum)


def _sanitized_program_argv(
    program_name: str,
    values: tuple[str, ...],
    *,
    maximum_arguments: int | None = _MAX_ARGUMENTS,
    maximum_argument_chars: int | None = _MAX_ARGUMENT_CHARS,
) -> tuple[str, ...]:
    result: list[str] = []
    redact_next = False
    short_flags = _program_sensitive_short_flags(program_name)
    long_flags = _SENSITIVE_LONG_FLAGS | _program_sensitive_long_flags(program_name)
    selected_values = (
        values if maximum_arguments is None else values[:maximum_arguments]
    )
    for value in selected_values:
        if redact_next:
            result.append(_REDACTED_ARGUMENT)
            redact_next = False
            continue
        flag, separator, _secret = value.partition("=")
        normalized_flag = flag.casefold()
        if normalized_flag in long_flags:
            if separator:
                result.append(f"{flag}=<{_REDACTED_ARGUMENT[1:-1]}>")
            else:
                result.append(_bounded_optional(value, maximum_argument_chars))
                redact_next = True
            continue
        assignment_name, assignment_separator, _assignment_value = value.partition("=")
        if (
            assignment_separator
            and _SENSITIVE_ASSIGNMENT_NAME.fullmatch(assignment_name) is not None
        ):
            result.append(f"{assignment_name}={_REDACTED_ASSIGNMENT}")
            continue
        normalized_value = value.casefold()
        matched_short_flag = next(
            (
                short_flag
                for short_flag in short_flags
                if normalized_value == short_flag
                or (
                    not normalized_value.startswith("--")
                    and normalized_value.startswith(short_flag)
                )
            ),
            None,
        )
        if matched_short_flag is not None:
            if normalized_value == matched_short_flag:
                result.append(_bounded_optional(value, maximum_argument_chars))
                redact_next = True
            else:
                result.append(f"{value[: len(matched_short_flag)]}<redacted>")
            continue
        redacted_value = _redact_inline_credentials(value)
        if redacted_value != value:
            result.append(_bounded_optional(redacted_value, maximum_argument_chars))
            continue
        if _contains_credential_material(value):
            result.append(_REDACTED_ARGUMENT)
            continue
        result.append(_bounded_optional(value, maximum_argument_chars))
    return tuple(result)


def _program_sensitive_short_flags(program_name: str) -> frozenset[str]:
    normalized = program_name.rsplit("/", maxsplit=1)[-1].casefold()
    return _SENSITIVE_SHORT_FLAGS_BY_PROGRAM.get(normalized, frozenset())


def _program_sensitive_long_flags(program_name: str) -> frozenset[str]:
    normalized = program_name.rsplit("/", maxsplit=1)[-1].casefold()
    return _SENSITIVE_LONG_FLAGS_BY_PROGRAM.get(normalized, frozenset())


def _contains_credential_material(value: str) -> bool:
    return bool(_SENSITIVE_ARGUMENT.search(value) or _CREDENTIAL_URI.search(value))


def _redact_inline_credentials(value: str) -> str:
    redacted = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group('name')}={_REDACTED_ASSIGNMENT}",
        value,
    )
    return _CREDENTIAL_URI.sub(
        lambda match: (
            f"{match.group('scheme')}{match.group('username')}:{_REDACTED_URI_SECRET}@"
        ),
        redacted,
    )


def _bounded_optional(value: str, maximum: int | None) -> str:
    if maximum is None:
        return value
    return _bounded_text(value, maximum)


def _bounded_text(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return f"{value[: maximum - 1]}…"


__all__ = [
    "FOREGROUND_AUTHORITY_CLASS",
    "FOREGROUND_AUTHORITY_SCHEMA_VERSION",
    "SESSION_SEMANTICS_AUTHORITY_DOMAIN",
    "ForegroundAuthoritySnapshot",
    "capture_foreground_authority",
]
