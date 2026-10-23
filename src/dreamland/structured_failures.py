from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dreamland.agent_structured import (
    STRUCTURED_FAILURE_CLASS_METADATA,
    STRUCTURED_RAW_RESPONSE_METADATA,
    command_result_from_model_response,
)
from dreamland.models import CommandResult, SessionState
from dreamland.session_metadata import state_profile_name_from_metadata
from dreamland.state_profiles import StateProfile, session_state_for_level

STRUCTURED_FAILURE_CAPSULE_KIND = "dreamland.structured_failure_capsule"
STRUCTURED_FAILURE_CAPSULE_SCHEMA_VERSION = 1
DEFAULT_STRUCTURED_FAILURE_DIR = Path("state_profiles") / "structured_failures"
STRUCTURED_FAILURE_CAPSULE_ID_METADATA = "dreamland.structured.failure_capsule_id"
STRUCTURED_FAILURE_CAPSULE_PATH_METADATA = "dreamland.structured.failure_capsule_path"


@dataclass(frozen=True, slots=True)
class StructuredFailureCapsule:
    id: str
    created_at: str
    session_id: str
    command_id: str
    command: str
    starting_cwd: str
    username: str
    hostname: str
    profile_name: str
    parser_error: str
    failure_class: str
    raw_response: str
    result_stdout: str
    result_stderr: str
    result_exit_status: int
    result_metadata: dict[str, object]
    pre_command_state_profile: dict[str, object]

    def to_document(self) -> dict[str, object]:
        return {
            "kind": STRUCTURED_FAILURE_CAPSULE_KIND,
            "schema_version": STRUCTURED_FAILURE_CAPSULE_SCHEMA_VERSION,
            "id": self.id,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "command_id": self.command_id,
            "command": self.command,
            "starting_cwd": self.starting_cwd,
            "username": self.username,
            "hostname": self.hostname,
            "profile_name": self.profile_name,
            "parser_error": self.parser_error,
            "failure_class": self.failure_class,
            "raw_response": self.raw_response,
            "result": {
                "stdout": self.result_stdout,
                "stderr": self.result_stderr,
                "exit_status": self.result_exit_status,
                "metadata": deepcopy(self.result_metadata),
            },
            "pre_command_state_profile": deepcopy(self.pre_command_state_profile),
        }


@dataclass(frozen=True, slots=True)
class StructuredFailureSummary:
    id: str
    created_at: str
    session_id: str
    command_id: str
    command: str
    failure_class: str
    parser_error: str
    path: Path


class StructuredFailureStore:
    def __init__(self, directory: Path | str = DEFAULT_STRUCTURED_FAILURE_DIR) -> None:
        self.directory = Path(directory)

    def save(self, capsule: StructuredFailureCapsule) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(capsule.id)
        temporary_path = path.with_suffix(".json.tmp")
        text = json.dumps(capsule.to_document(), indent=2, sort_keys=True)
        temporary_path.write_text(f"{text}\n", encoding="utf-8")
        temporary_path.replace(path)
        return path

    def load(self, capsule_id: str) -> StructuredFailureCapsule:
        path = self.path_for(capsule_id)
        if not path.exists():
            raise StructuredFailureError(
                f"structured failure capsule {capsule_id!r} does not exist"
            )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StructuredFailureError(
                f"structured failure capsule {capsule_id!r} is not valid JSON: "
                f"{exc.msg}"
            ) from exc
        capsule = structured_failure_capsule_from_document(document)
        if capsule.id != capsule_id:
            raise StructuredFailureError(
                f"structured failure file {path} contains id {capsule.id!r}"
            )
        return capsule

    def list(self) -> list[StructuredFailureSummary]:
        if not self.directory.exists():
            return []
        summaries: list[StructuredFailureSummary] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                capsule = structured_failure_capsule_from_document(document)
            except (OSError, StructuredFailureError, json.JSONDecodeError):
                continue
            summaries.append(
                StructuredFailureSummary(
                    id=capsule.id,
                    created_at=capsule.created_at,
                    session_id=capsule.session_id,
                    command_id=capsule.command_id,
                    command=capsule.command,
                    failure_class=capsule.failure_class,
                    parser_error=capsule.parser_error,
                    path=path,
                )
            )
        return sorted(summaries, key=lambda summary: summary.created_at)

    def path_for(self, capsule_id: str) -> Path:
        _validate_capsule_id(capsule_id)
        return self.directory / f"{capsule_id}.json"


class StructuredFailureError(ValueError):
    pass


def should_capture_structured_failure(result: CommandResult) -> bool:
    return (
        result.metadata.get("structured_response_accepted") is False
        and isinstance(result.metadata.get("structured_parse_error"), str)
        and isinstance(result.metadata.get(STRUCTURED_RAW_RESPONSE_METADATA), str)
    )


def create_structured_failure_capsule(
    session: SessionState,
    *,
    command: str,
    command_id: str,
    starting_cwd: str,
    result: CommandResult,
    created_at: datetime | None = None,
) -> StructuredFailureCapsule:
    timestamp = created_at or datetime.now(UTC)
    created_at_text = timestamp.isoformat(timespec="seconds")
    capsule_id = _capsule_id(
        timestamp=timestamp,
        session_id=session.langfuse_trace_id,
        command_id=command_id,
    )
    parser_error = str(result.metadata.get("structured_parse_error", ""))
    failure_class = str(
        result.metadata.get(STRUCTURED_FAILURE_CLASS_METADATA) or "validation_error"
    )
    raw_response = str(result.metadata.get(STRUCTURED_RAW_RESPONSE_METADATA, ""))
    pre_command_profile = StateProfile(
        name=capsule_id,
        level="exact",
        state=session_state_for_level(session, "exact"),
        created_at=created_at_text,
        source_session_id=session.langfuse_trace_id,
        comment=(
            "Automatic pre-command state captured before a rejected structured "
            f"model response for command: {command}"
        ),
    )
    return StructuredFailureCapsule(
        id=capsule_id,
        created_at=created_at_text,
        session_id=session.langfuse_trace_id,
        command_id=command_id,
        command=command,
        starting_cwd=starting_cwd,
        username=session.username,
        hostname=session.host_identity.hostname,
        profile_name=state_profile_name_from_metadata(
            session.environment_metadata.metadata
        )
        or "unsaved",
        parser_error=parser_error,
        failure_class=failure_class,
        raw_response=raw_response,
        result_stdout=result.stdout,
        result_stderr=result.stderr,
        result_exit_status=result.exit_status,
        result_metadata=dict(result.metadata),
        pre_command_state_profile=pre_command_profile.to_document(),
    )


def replay_structured_failure_parser(
    capsule: StructuredFailureCapsule,
) -> CommandResult:
    return command_result_from_model_response(capsule.raw_response)


def structured_failure_capsule_from_document(
    document: object,
) -> StructuredFailureCapsule:
    if not isinstance(document, dict):
        raise StructuredFailureError("structured failure capsule must be an object")
    if document.get("kind") != STRUCTURED_FAILURE_CAPSULE_KIND:
        raise StructuredFailureError("structured failure capsule kind is invalid")
    if document.get("schema_version") != STRUCTURED_FAILURE_CAPSULE_SCHEMA_VERSION:
        raise StructuredFailureError(
            "structured failure capsule schema version is unsupported"
        )
    result = document.get("result")
    profile = document.get("pre_command_state_profile")
    if not isinstance(result, dict):
        raise StructuredFailureError("structured failure result must be an object")
    if not isinstance(profile, dict):
        raise StructuredFailureError(
            "structured failure pre-command state profile must be an object"
        )
    capsule_id = _required_string(document, "id")
    _validate_capsule_id(capsule_id)
    metadata = result.get("metadata")
    return StructuredFailureCapsule(
        id=capsule_id,
        created_at=_required_string(document, "created_at"),
        session_id=_required_string(document, "session_id"),
        command_id=_required_string(document, "command_id"),
        command=_required_string(document, "command"),
        starting_cwd=_required_string(document, "starting_cwd"),
        username=_required_string(document, "username"),
        hostname=_required_string(document, "hostname"),
        profile_name=_required_string(document, "profile_name"),
        parser_error=_required_string(document, "parser_error"),
        failure_class=_required_string(document, "failure_class"),
        raw_response=_required_string(document, "raw_response"),
        result_stdout=str(result.get("stdout", "")),
        result_stderr=str(result.get("stderr", "")),
        result_exit_status=_coerce_int(result.get("exit_status", 0)),
        result_metadata=deepcopy(metadata) if isinstance(metadata, dict) else {},
        pre_command_state_profile=deepcopy(profile),
    )


def _required_string(document: dict[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise StructuredFailureError(f"structured failure {key} is required")
    return value


def _coerce_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _capsule_id(
    *,
    timestamp: datetime,
    session_id: str,
    command_id: str,
) -> str:
    stamp = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{_safe_id_part(session_id)[:12]}-{_safe_id_part(command_id)}"


def _safe_id_part(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value) or "unknown"


def _validate_capsule_id(capsule_id: str) -> None:
    if not capsule_id or any(char in capsule_id for char in "/\\"):
        raise StructuredFailureError("structured failure capsule id is invalid")
