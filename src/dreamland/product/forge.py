"""Protocol-neutral World Forge seed and recipe primitives.

This module is inert until explicitly called.  It owns the canonical BSides
WorldSeed representation and deterministic validation boundary, but it does
not construct a provider, start a worker, alter the configured state profile,
or compose itself into the SSH runtime.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

WORLD_SEED_KIND = "dreamland.world_seed"
WORLD_SEED_SCHEMA_REVISION = "1"
WORLD_SEED_PREPARATION_KIND = "dreamland.world_seed_preparation"
WORLD_SEED_PREPARATION_SCHEMA_REVISION = "1"
BSIDES_RECIPE_ID = "world-recipe:bsides-room:v1"
BSIDES_RECIPE_REVISION = "1"
DEFAULT_SELECTED_HANDOFF_POLICY_ID = "forge-to-stage-handoff:v1"
MAXIMUM_SELECTED_HANDOFF_TTL_SECONDS = 3_600

WorldSeedFieldKind = Literal["short-text", "integer"]


@dataclass(frozen=True, slots=True)
class WorldSeedField:
    field_id: str
    kind: WorldSeedFieldKind
    minimum: int
    maximum: int


BSIDES_WORLD_SEED_FIELDS = (
    WorldSeedField("day-or-date", "short-text", 1, 40),
    WorldSeedField("year", "integer", 1, 9999),
    WorldSeedField("adjective", "short-text", 1, 40),
    WorldSeedField("industry-or-setting", "short-text", 1, 80),
    WorldSeedField("technology-one", "short-text", 1, 80),
    WorldSeedField("technology-two", "short-text", 1, 80),
    WorldSeedField("important-job", "short-text", 1, 120),
    WorldSeedField("wildcard-event", "short-text", 1, 160),
)

_INTEGER_PATTERN = re.compile(r"^[+-]?[0-9]+$")
_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
_URL_PATTERN = re.compile(r"(?:https?|ssh)://", re.IGNORECASE)
_SECRET_PATTERN = re.compile(
    r"(?:\b(?:api[_-]?key|password|passwd|secret|token)\s*[:=]\s*\S+|\b[A-Fa-f0-9]{32,}\b)",
    re.IGNORECASE,
)
_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class WorldSeedAnswer:
    field_id: str
    kind: WorldSeedFieldKind
    value: str

    def to_document(self) -> dict[str, str]:
        return {"field_id": self.field_id, "kind": self.kind, "value": self.value}


@dataclass(frozen=True, slots=True)
class WorldSeed:
    recipe_id: str
    recipe_revision: str
    answers: tuple[WorldSeedAnswer, ...]
    rendered_sentence: str
    confirmation_state: Literal["confirmed"]
    confirmed_at: str
    schema_revision: str = WORLD_SEED_SCHEMA_REVISION

    @property
    def seed_hash(self) -> str:
        digest = hashlib.sha256(_canonical_json(self._hash_document())).hexdigest()
        return f"sha256:{digest}"

    def _hash_document(self) -> dict[str, object]:
        return {
            "kind": WORLD_SEED_KIND,
            "schema_revision": self.schema_revision,
            "recipe_id": self.recipe_id,
            "recipe_revision": self.recipe_revision,
            "answers": [answer.to_document() for answer in self.answers],
            "rendered_sentence": self.rendered_sentence,
            "confirmation_state": self.confirmation_state,
            "confirmed_at": self.confirmed_at,
        }

    def to_document(self) -> dict[str, object]:
        document = self._hash_document()
        document["seed_hash"] = self.seed_hash
        return document


@dataclass(frozen=True, slots=True)
class WorldSeedPreparation:
    """Exact validated review that has not yet been presenter-confirmed."""

    recipe_id: str
    recipe_revision: str
    answers: tuple[WorldSeedAnswer, ...]
    rendered_sentence: str
    confirmation_state: Literal["unconfirmed"] = "unconfirmed"
    schema_revision: str = WORLD_SEED_PREPARATION_SCHEMA_REVISION

    @property
    def preparation_digest(self) -> str:
        digest = hashlib.sha256(_canonical_json(self._hash_document())).hexdigest()
        return f"sha256:{digest}"

    def _hash_document(self) -> dict[str, object]:
        return {
            "kind": WORLD_SEED_PREPARATION_KIND,
            "schema_revision": self.schema_revision,
            "recipe_id": self.recipe_id,
            "recipe_revision": self.recipe_revision,
            "answers": [answer.to_document() for answer in self.answers],
            "rendered_sentence": self.rendered_sentence,
            "confirmation_state": self.confirmation_state,
        }

    def to_document(self) -> dict[str, object]:
        document = self._hash_document()
        document["preparation_digest"] = self.preparation_digest
        return document


@dataclass(frozen=True, slots=True)
class WorldSeedIssue:
    code: str
    message: str
    field_id: str | None = None

    def to_document(self) -> dict[str, str]:
        value = {"code": self.code, "message": self.message}
        if self.field_id is not None:
            value["field_id"] = self.field_id
        return value


class WorldSeedValidationError(ValueError):
    def __init__(self, issues: Iterable[WorldSeedIssue]) -> None:
        self.issues = tuple(issues)
        if not self.issues:
            raise ValueError("WorldSeedValidationError requires at least one issue")
        super().__init__("; ".join(issue.message for issue in self.issues))


def prepare_world_seed(
    *,
    recipe_id: str,
    recipe_revision: str,
    answers: Iterable[WorldSeedAnswer | Mapping[str, object]],
) -> WorldSeedPreparation:
    """Validate exact answers and produce an explicitly unconfirmed review."""
    answer_values, parse_issues = _parse_answers(answers)
    issues = [*parse_issues]
    if recipe_id != BSIDES_RECIPE_ID:
        issues.append(
            WorldSeedIssue("recipe_unknown", "The world recipe is unavailable.")
        )
    if recipe_revision != BSIDES_RECIPE_REVISION:
        issues.append(
            WorldSeedIssue(
                "recipe_revision_mismatch",
                "The world recipe revision is no longer current.",
            )
        )
    issues.extend(_validate_answers(answer_values))
    rendered_sentence = render_bsides_world_seed(answer_values)
    if len(rendered_sentence) > 768:
        issues.append(
            WorldSeedIssue(
                "rendered_sentence_too_long",
                "The rendered sentence exceeds the configured limit.",
            )
        )
    if issues:
        raise WorldSeedValidationError(issues)
    return WorldSeedPreparation(
        recipe_id=recipe_id,
        recipe_revision=recipe_revision,
        answers=answer_values,
        rendered_sentence=rendered_sentence,
    )


def confirm_world_seed(
    preparation: WorldSeedPreparation, *, confirmed_at: str
) -> WorldSeed:
    """Create the canonical confirmed seed from the exact reviewed preparation."""
    issues = _validate_confirmation(confirmed_at)
    if issues:
        raise WorldSeedValidationError(issues)
    return WorldSeed(
        recipe_id=preparation.recipe_id,
        recipe_revision=preparation.recipe_revision,
        answers=preparation.answers,
        rendered_sentence=preparation.rendered_sentence,
        confirmation_state="confirmed",
        confirmed_at=confirmed_at,
    )


def world_seed_from_document(document: Mapping[str, object]) -> WorldSeed:
    allowed = {
        "kind",
        "schema_revision",
        "recipe_id",
        "recipe_revision",
        "answers",
        "rendered_sentence",
        "confirmation_state",
        "confirmed_at",
        "seed_hash",
    }
    if set(document) != allowed:
        raise WorldSeedValidationError(
            (
                WorldSeedIssue(
                    "document_shape_invalid", "The WorldSeed document shape is invalid."
                ),
            )
        )
    if document.get("kind") != WORLD_SEED_KIND:
        raise WorldSeedValidationError(
            (WorldSeedIssue("kind_invalid", "The WorldSeed kind is invalid."),)
        )
    if document.get("schema_revision") != WORLD_SEED_SCHEMA_REVISION:
        raise WorldSeedValidationError(
            (
                WorldSeedIssue(
                    "schema_revision_unsupported",
                    "The WorldSeed schema revision is unsupported.",
                ),
            )
        )
    raw_answers = document.get("answers")
    if not isinstance(raw_answers, list):
        raise WorldSeedValidationError(
            (
                WorldSeedIssue(
                    "answers_invalid", "The WorldSeed answers must be an array."
                ),
            )
        )
    required_strings = (
        "recipe_id",
        "recipe_revision",
        "rendered_sentence",
        "confirmed_at",
        "seed_hash",
    )
    if any(not isinstance(document.get(name), str) for name in required_strings):
        raise WorldSeedValidationError(
            (
                WorldSeedIssue(
                    "document_value_invalid",
                    "The WorldSeed document contains an invalid value.",
                ),
            )
        )
    if document.get("confirmation_state") != "confirmed":
        raise WorldSeedValidationError(
            (
                WorldSeedIssue(
                    "confirmation_required", "The WorldSeed is not confirmed."
                ),
            )
        )
    preparation = prepare_world_seed(
        recipe_id=str(document["recipe_id"]),
        recipe_revision=str(document["recipe_revision"]),
        answers=raw_answers,
    )
    if document["rendered_sentence"] != preparation.rendered_sentence:
        raise WorldSeedValidationError(
            (
                WorldSeedIssue(
                    "rendered_sentence_mismatch",
                    "The rendered sentence does not match the exact ordered answers.",
                ),
            )
        )
    seed = confirm_world_seed(preparation, confirmed_at=str(document["confirmed_at"]))
    if document["seed_hash"] != seed.seed_hash:
        raise WorldSeedValidationError(
            (
                WorldSeedIssue(
                    "seed_hash_mismatch",
                    "The WorldSeed hash does not match its exact contents.",
                ),
            )
        )
    return seed


def render_bsides_world_seed(
    answers: Iterable[WorldSeedAnswer | Mapping[str, object]],
) -> str:
    parsed, issues = _parse_answers(answers)
    if issues or len(parsed) != len(BSIDES_WORLD_SEED_FIELDS):
        return ""
    values = {answer.field_id: answer.value for answer in parsed}
    return (
        f"On {values.get('day-or-date', '')} in {values.get('year', '')}, "
        f"inside a {values.get('adjective', '')} "
        f"{values.get('industry-or-setting', '')}, people rely on "
        f"{values.get('technology-one', '')} and "
        f"{values.get('technology-two', '')} to "
        f"{values.get('important-job', '')}. Today, "
        f"{values.get('wildcard-event', '')}."
    )


def _parse_answers(
    answers: Iterable[WorldSeedAnswer | Mapping[str, object]],
) -> tuple[tuple[WorldSeedAnswer, ...], tuple[WorldSeedIssue, ...]]:
    parsed: list[WorldSeedAnswer] = []
    issues: list[WorldSeedIssue] = []
    try:
        values = tuple(answers)
    except TypeError:
        return (), (
            WorldSeedIssue(
                "answers_invalid", "The WorldSeed answers must be iterable."
            ),
        )
    for index, raw in enumerate(values):
        if isinstance(raw, WorldSeedAnswer):
            parsed.append(raw)
            continue
        if not isinstance(raw, Mapping) or set(raw) != {"field_id", "kind", "value"}:
            issues.append(
                WorldSeedIssue(
                    "answer_shape_invalid",
                    f"WorldSeed answer {index + 1} has an invalid shape.",
                )
            )
            continue
        field_id, kind, value = raw["field_id"], raw["kind"], raw["value"]
        if (
            not isinstance(field_id, str)
            or kind not in {"short-text", "integer"}
            or not isinstance(value, str)
        ):
            issues.append(
                WorldSeedIssue(
                    "answer_value_invalid",
                    f"WorldSeed answer {index + 1} has an invalid value.",
                )
            )
            continue
        typed_kind: WorldSeedFieldKind = (
            "short-text" if kind == "short-text" else "integer"
        )
        parsed.append(WorldSeedAnswer(field_id, typed_kind, value))
    return tuple(parsed), tuple(issues)


def _validate_answers(answers: tuple[WorldSeedAnswer, ...]) -> list[WorldSeedIssue]:
    if len(answers) != len(BSIDES_WORLD_SEED_FIELDS):
        return [
            WorldSeedIssue(
                "answer_count_invalid",
                "The seed must contain one answer for every recipe field.",
            )
        ]
    issues: list[WorldSeedIssue] = []
    for answer, field in zip(answers, BSIDES_WORLD_SEED_FIELDS, strict=True):
        if answer.field_id != field.field_id or answer.kind != field.kind:
            issues.append(
                WorldSeedIssue(
                    "answer_order_invalid",
                    "The seed field IDs, kinds, and order do not match the recipe revision.",
                    answer.field_id,
                )
            )
            continue
        issues.extend(_validate_answer(answer, field))
    return issues


def _validate_answer(
    answer: WorldSeedAnswer, field: WorldSeedField
) -> list[WorldSeedIssue]:
    if not answer.value.strip():
        return [WorldSeedIssue("blank", "The answer cannot be blank.", field.field_id)]
    unsafe = _unsafe_input_code(answer.value)
    if unsafe is not None:
        return [
            WorldSeedIssue(
                unsafe, "The answer is outside the safe input envelope.", field.field_id
            )
        ]
    if field.kind == "integer":
        if len(answer.value) > 16 or _INTEGER_PATTERN.fullmatch(answer.value) is None:
            return [
                WorldSeedIssue(
                    "integer_invalid",
                    "The answer must be a whole number without spaces.",
                    field.field_id,
                )
            ]
        value = int(answer.value)
        if not field.minimum <= value <= field.maximum:
            return [
                WorldSeedIssue(
                    "integer_out_of_range",
                    "The answer is outside the configured numeric range.",
                    field.field_id,
                )
            ]
        return []
    length = len(answer.value)
    if not field.minimum <= length <= field.maximum:
        return [
            WorldSeedIssue(
                "text_length_invalid",
                "The answer is outside the configured character limits.",
                field.field_id,
            )
        ]
    return []


def _unsafe_input_code(value: str) -> str | None:
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            return "unsafe_character"
    if (
        _EMAIL_PATTERN.search(value)
        or _URL_PATTERN.search(value)
        or _SECRET_PATTERN.search(value)
    ):
        return "sensitive_identifier"
    for token in re.findall(
        r"(?<![0-9A-Fa-f:.])[0-9A-Fa-f:.]{7,}(?![0-9A-Fa-f:.])", value
    ):
        try:
            ipaddress.ip_address(token)
        except ValueError:
            continue
        return "sensitive_identifier"
    return None


def _validate_confirmation(value: str) -> list[WorldSeedIssue]:
    if _TIMESTAMP_PATTERN.fullmatch(value) is None:
        return [
            WorldSeedIssue(
                "confirmation_time_invalid",
                "The confirmation time must be an ISO-8601 timestamp with a timezone.",
            )
        ]
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return [
            WorldSeedIssue(
                "confirmation_time_invalid", "The confirmation time is invalid."
            )
        ]
    if parsed.tzinfo is None:
        return [
            WorldSeedIssue(
                "confirmation_time_invalid",
                "The confirmation time requires a timezone.",
            )
        ]
    return []


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


class ForgeJobState(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    BUILDING = "building"
    GENERATED = "generated"
    VALIDATING = "validating"
    SELECTED = "selected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"


_TERMINAL_FORGE_STATES = frozenset(
    {
        ForgeJobState.SELECTED,
        ForgeJobState.FAILED,
        ForgeJobState.CANCELLED,
        ForgeJobState.DEADLINE_EXCEEDED,
    }
)
_FORGE_TRANSITIONS = {
    ForgeJobState.ACCEPTED: frozenset(
        {ForgeJobState.QUEUED, ForgeJobState.CANCELLED, ForgeJobState.FAILED}
    ),
    ForgeJobState.QUEUED: frozenset(
        {ForgeJobState.BUILDING, ForgeJobState.CANCELLED, ForgeJobState.FAILED}
    ),
    ForgeJobState.BUILDING: frozenset(
        {
            ForgeJobState.GENERATED,
            ForgeJobState.CANCELLED,
            ForgeJobState.FAILED,
            ForgeJobState.DEADLINE_EXCEEDED,
        }
    ),
    ForgeJobState.GENERATED: frozenset(
        {ForgeJobState.VALIDATING, ForgeJobState.CANCELLED, ForgeJobState.FAILED}
    ),
    ForgeJobState.VALIDATING: frozenset(
        {
            ForgeJobState.SELECTED,
            ForgeJobState.CANCELLED,
            ForgeJobState.FAILED,
            ForgeJobState.DEADLINE_EXCEEDED,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class ForgeActorSeed:
    authority: str
    actor_id: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class ForgeSelectedRevisionRecord:
    """Immutable private authority for one live Forge-to-Stage handoff."""

    selected_record_id: str
    status: Literal["selected"]
    service_epoch: str
    job_id: str
    job_reference: str
    actor_id: str
    selected_at: str
    expires_at: str
    policy_id: str
    revision_id: str
    immutable_digest: str
    source_kind: str
    source_fingerprint: str
    receipt_correlation_id: str
    receipt_fingerprint: str


@dataclass(frozen=True, slots=True)
class ForgePolicy:
    maximum_jobs: int = 16
    deadline_seconds: int = 120
    maximum_idempotency_records: int = 128
    maximum_preparations: int = 32
    preparation_ttl_seconds: int = 300
    selected_handoff_policy_id: str = DEFAULT_SELECTED_HANDOFF_POLICY_ID
    selected_handoff_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if self.maximum_jobs <= 0:
            raise ValueError("maximum_jobs must be positive")
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        if self.maximum_idempotency_records < self.maximum_jobs:
            raise ValueError("maximum_idempotency_records must cover maximum_jobs")
        if self.maximum_preparations <= 0:
            raise ValueError("maximum_preparations must be positive")
        if self.preparation_ttl_seconds <= 0:
            raise ValueError("preparation_ttl_seconds must be positive")
        if (
            not isinstance(self.selected_handoff_policy_id, str)
            or _SAFE_IDENTIFIER_PATTERN.fullmatch(self.selected_handoff_policy_id)
            is None
        ):
            raise ValueError("selected_handoff_policy_id must be a safe identifier")
        if (
            isinstance(self.selected_handoff_ttl_seconds, bool)
            or not isinstance(self.selected_handoff_ttl_seconds, int)
            or not (
                1
                <= self.selected_handoff_ttl_seconds
                <= MAXIMUM_SELECTED_HANDOFF_TTL_SECONDS
            )
        ):
            raise ValueError(
                "selected_handoff_ttl_seconds must be between 1 and "
                f"{MAXIMUM_SELECTED_HANDOFF_TTL_SECONDS}"
            )


class ForgeServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        retry: Literal["none", "resume", "same_request"] = "none",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry = retry


@dataclass(slots=True)
class _ForgeJob:
    job_id: str
    reference: str
    actor_id: str
    seed: WorldSeed
    preparation_reference: str
    stream_id: str
    state: ForgeJobState
    created_at: str
    changed_at: str
    deadline_at: str
    events: list[dict[str, object]]
    cursors: dict[str, int]
    safe_message: str | None = None
    receipt: dict[str, object] | None = None


@dataclass(slots=True)
class _ForgeIdempotency:
    body_hash: str
    result: dict[str, object] | None = None
    error: ForgeServiceError | None = None


@dataclass(slots=True)
class _ForgePreparation:
    reference: str
    actor_id: str
    preparation: WorldSeedPreparation
    prepared_at: str
    expires_at: str
    consumed_job_id: str | None = None


class WorldForgeService:
    """Bounded in-memory lifecycle foundation for an explicitly built Forge.

    Provider execution and durable restart recovery are deliberately absent in
    this first checkpoint.  The service exists so the transport and UI can
    integrate against authoritative lifecycle, replay, cancellation, and
    projection semantics before live workers are introduced.
    """

    def __init__(
        self,
        *,
        actors: Iterable[ForgeActorSeed],
        policy: ForgePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[str], str] | None = None,
        selected_record_token_factory: Callable[[str], str] | None = None,
        source: Mapping[str, object] | None = None,
    ) -> None:
        actor_values = tuple(actors)
        if not actor_values:
            raise ValueError("World Forge requires at least one presenter actor")
        self._actors: dict[str, ForgeActorSeed] = {}
        for actor in actor_values:
            if not actor.authority or not actor.actor_id:
                raise ValueError("World Forge actor authority and ID are required")
            if actor.authority in self._actors:
                raise ValueError("World Forge actor authorities must be unique")
            _parse_zoned_datetime(actor.expires_at, "actor expiry")
            self._actors[actor.authority] = actor
        self._policy = policy or ForgePolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_factory = token_factory or (
            lambda purpose: f"{purpose}:{secrets.token_urlsafe(24)}"
        )
        self._selected_record_token_factory = selected_record_token_factory or (
            lambda purpose: f"{purpose}:{secrets.token_urlsafe(24)}"
        )
        self._project_operation_source = source is not None
        self._source = (
            deepcopy(dict(source)) if source is not None else _preview_source()
        )
        self._lock = threading.RLock()
        self._jobs_by_id: dict[str, _ForgeJob] = {}
        self._jobs_by_reference: dict[str, _ForgeJob] = {}
        self._preparations: dict[str, _ForgePreparation] = {}
        self._idempotency: dict[tuple[str, str, str], _ForgeIdempotency] = {}
        self._selected_records_by_job_reference: dict[
            str, ForgeSelectedRevisionRecord
        ] = {}
        self._service_epoch = self._token_factory("forge-epoch")

    @property
    def service_epoch(self) -> str:
        return self._service_epoch

    @property
    def source(self) -> dict[str, object]:
        return deepcopy(self._source)

    def authorize(self, *, actor_authority: str, expected_epoch: str) -> None:
        """Validate private presenter and epoch evidence for a read operation."""
        with self._lock:
            self._require_epoch(expected_epoch)
            self._require_actor(actor_authority)

    def recipe(self) -> dict[str, object]:
        """Return the bounded presentation-safe recipe projection."""
        fields: list[dict[str, object]] = []
        labels = {
            "day-or-date": (
                "Day or date",
                "On what day or date does this world begin?",
            ),
            "year": ("Year", "What year is it?"),
            "adjective": ("Adjective", "What adjective describes the setting?"),
            "industry-or-setting": (
                "Industry or setting",
                "What industry or setting are we inside?",
            ),
            "technology-one": (
                "First technology",
                "What is the first technology people rely on?",
            ),
            "technology-two": (
                "Second technology",
                "What is the second technology people rely on?",
            ),
            "important-job": (
                "Important job",
                "What important job do those technologies help people do?",
            ),
            "wildcard-event": (
                "Wildcard event or constraint",
                "What unexpected event or constraint changes today?",
            ),
        }
        for field in BSIDES_WORLD_SEED_FIELDS:
            label, prompt = labels[field.field_id]
            value: dict[str, object] = {
                "field_id": field.field_id,
                "kind": field.kind,
                "label": _safe_text(label),
                "prompt": _safe_text(prompt),
            }
            if field.kind == "integer":
                value.update(
                    {
                        "minimum": field.minimum,
                        "maximum": field.maximum,
                        "maximum_input_characters": 16,
                        "accepted_pattern": "^[+-]?[0-9]+$",
                    }
                )
            else:
                value.update(
                    {
                        "minimum_characters": field.minimum,
                        "maximum_characters": field.maximum,
                    }
                )
            fields.append(value)
        return {
            "kind": "forge_recipe",
            "recipe_id": BSIDES_RECIPE_ID,
            "recipe_revision": BSIDES_RECIPE_REVISION,
            "title": _safe_text("Build a world with the room"),
            "description": _safe_text(
                "Collect one compact audience seed, then let DREAMLAND construct and validate a candidate revision."
            ),
            "fields": fields,
            "preparation": {"state": "available"},
        }

    def prepare(
        self,
        *,
        actor_authority: str,
        expected_epoch: str,
        recipe_id: str,
        recipe_revision: str,
        answers: Iterable[WorldSeedAnswer | Mapping[str, object]],
    ) -> dict[str, object]:
        """Create an expiring unconfirmed review bound to one presenter."""
        with self._lock:
            self._require_epoch(expected_epoch)
            actor = self._require_actor(actor_authority)
            now = self._now()
            self._prune_preparations(now)
            if len(self._preparations) >= self._policy.maximum_preparations:
                raise ForgeServiceError(
                    "preparation_capacity_exhausted",
                    "World Forge preparation capacity is temporarily exhausted.",
                    status=503,
                    retry="none",
                )
            preparation = prepare_world_seed(
                recipe_id=recipe_id,
                recipe_revision=recipe_revision,
                answers=answers,
            )
            reference = self._token_factory("forge-preparation")
            expires = now + timedelta(seconds=self._policy.preparation_ttl_seconds)
            record = _ForgePreparation(
                reference=reference,
                actor_id=actor.actor_id,
                preparation=preparation,
                prepared_at=_format_time(now),
                expires_at=_format_time(expires),
            )
            self._preparations[reference] = record
            return {
                "kind": "forge_seed_prepared",
                "service_epoch": self._service_epoch,
                "preparation_reference": reference,
                "prepared_at": record.prepared_at,
                "expires_at": record.expires_at,
                "preparation": preparation.to_document(),
                "display": _preparation_display(preparation),
                "source": deepcopy(self._source),
            }

    def confirm_and_submit(
        self,
        *,
        actor_authority: str,
        expected_epoch: str,
        idempotency_key: str,
        preparation_reference: str,
    ) -> dict[str, object]:
        with self._lock:
            self._require_epoch(expected_epoch)
            actor = self._require_actor(actor_authority)
            key = _require_forge_key(idempotency_key)
            body_hash = _sha256_document(
                {"preparation_reference": preparation_reference}
            )
            scope = (actor.actor_id, "submit", key)
            replay = self._replay(scope, body_hash)
            if replay is not None:
                return replay
            self._require_idempotency_capacity()
            preparation = self._require_preparation(
                preparation_reference, actor.actor_id
            )
            if preparation.consumed_job_id is not None:
                error = ForgeServiceError(
                    "preparation_consumed",
                    "The World Forge preparation has already been submitted.",
                    status=409,
                    retry="none",
                )
                self._idempotency[scope] = _ForgeIdempotency(body_hash, error=error)
                raise error
            if len(self._jobs_by_id) >= self._policy.maximum_jobs:
                error = ForgeServiceError(
                    "capacity_exhausted",
                    "World Forge capacity is temporarily exhausted.",
                    status=503,
                    retry="same_request",
                )
                self._idempotency[scope] = _ForgeIdempotency(body_hash, error=error)
                raise error
            now = self._now()
            if (
                _parse_zoned_datetime(preparation.expires_at, "preparation expiry")
                <= now
            ):
                error = ForgeServiceError(
                    "preparation_expired",
                    "The World Forge preparation has expired.",
                    status=409,
                    retry="none",
                )
                self._idempotency[scope] = _ForgeIdempotency(body_hash, error=error)
                raise error
            seed = confirm_world_seed(
                preparation.preparation, confirmed_at=_format_time(now)
            )
            job = _ForgeJob(
                job_id=self._token_factory("forge-job"),
                reference=self._token_factory("forge-reference"),
                actor_id=actor.actor_id,
                seed=seed,
                preparation_reference=preparation.reference,
                stream_id=self._token_factory("forge-stream"),
                state=ForgeJobState.ACCEPTED,
                created_at=_format_time(now),
                changed_at=_format_time(now),
                deadline_at=_format_time(
                    now + timedelta(seconds=self._policy.deadline_seconds)
                ),
                events=[],
                cursors={},
            )
            self._append_event(job)
            self._jobs_by_id[job.job_id] = job
            self._jobs_by_reference[job.reference] = job
            preparation.consumed_job_id = job.job_id
            result = {
                "kind": "forge_job_accepted",
                "service_epoch": self._service_epoch,
                "job_id": job.job_id,
                "reference": job.reference,
                "position": self._position(job),
                "deadline_at": job.deadline_at,
                "replayed": False,
                "source": deepcopy(self._source),
            }
            self._idempotency[scope] = _ForgeIdempotency(
                body_hash, result=deepcopy(result)
            )
            return result

    def status(
        self,
        *,
        actor_authority: str,
        expected_epoch: str,
        reference: str,
    ) -> dict[str, object]:
        with self._lock:
            self._require_epoch(expected_epoch)
            actor = self._require_actor(actor_authority)
            job = self._require_job(reference, actor.actor_id)
            return self._projection(job)

    def events(
        self,
        *,
        actor_authority: str,
        expected_epoch: str,
        reference: str,
        after_cursor: str,
    ) -> dict[str, object]:
        with self._lock:
            self._require_epoch(expected_epoch)
            actor = self._require_actor(actor_authority)
            job = self._require_job(reference, actor.actor_id)
            after_sequence = job.cursors.get(after_cursor)
            if after_sequence is None:
                raise ForgeServiceError(
                    "resync_required",
                    "World Forge event history must be resynchronized from the job snapshot.",
                    status=409,
                    retry="resume",
                )
            result: dict[str, object] = {
                "kind": "forge_events",
                "service_epoch": self._service_epoch,
                "stream_id": job.stream_id,
                "events": deepcopy(job.events[after_sequence:]),
                "position": self._position(job),
                "terminal": job.state in _TERMINAL_FORGE_STATES,
                "page_complete": True,
                "requires_snapshot_reconciliation": True,
            }
            if self._project_operation_source:
                result["source"] = deepcopy(self._source)
            return result

    def cancel(
        self,
        *,
        actor_authority: str,
        expected_epoch: str,
        reference: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        with self._lock:
            self._require_epoch(expected_epoch)
            actor = self._require_actor(actor_authority)
            key = _require_forge_key(idempotency_key)
            body_hash = _sha256_document({"reference": reference})
            scope = (actor.actor_id, "cancel", key)
            replay = self._replay(scope, body_hash)
            if replay is not None:
                return replay
            self._require_idempotency_capacity()
            try:
                job = self._require_job(reference, actor.actor_id)
                if job.state in _TERMINAL_FORGE_STATES:
                    raise ForgeServiceError(
                        "job_terminal",
                        "The World Forge job is already terminal.",
                        status=409,
                        retry="none",
                    )
                self._transition_locked(job, ForgeJobState.CANCELLED)
            except ForgeServiceError as error:
                self._idempotency[scope] = _ForgeIdempotency(body_hash, error=error)
                raise
            result = {
                "kind": "forge_job_cancelled",
                "service_epoch": self._service_epoch,
                "job_id": job.job_id,
                "position": self._position(job),
                "replayed": False,
            }
            if self._project_operation_source:
                result["source"] = deepcopy(self._source)
            self._idempotency[scope] = _ForgeIdempotency(
                body_hash, result=deepcopy(result)
            )
            return result

    def preview_transition(
        self,
        *,
        reference: str,
        state: ForgeJobState,
        safe_message: str | None = None,
    ) -> dict[str, object]:
        """Advance a provider-free fixture job; never exposed as product authority."""
        with self._lock:
            job = self._jobs_by_reference.get(reference)
            if job is None:
                raise ForgeServiceError(
                    "job_not_found", "The World Forge job is unavailable.", status=404
                )
            self._transition_locked(job, state, safe_message=safe_message)
            return self._projection(job)

    def preview_rotate_epoch(self) -> str:
        """Simulate provider-free process loss without retaining uncertain work."""
        with self._lock:
            self._service_epoch = self._token_factory("forge-epoch")
            self._jobs_by_id.clear()
            self._jobs_by_reference.clear()
            self._preparations.clear()
            self._idempotency.clear()
            self._selected_records_by_job_reference.clear()
            return self._service_epoch

    def complete_selected(
        self, *, reference: str, receipt: Mapping[str, object]
    ) -> dict[str, object]:
        """Install one trusted worker receipt atomically with selection."""
        with self._lock:
            job = self._jobs_by_reference.get(reference)
            if job is None:
                raise ForgeServiceError(
                    "job_not_found", "The World Forge job is unavailable.", status=404
                )
            self._transition_locked(
                job, ForgeJobState.SELECTED, selected_receipt=receipt
            )
            return self._projection(job)

    def resolve_selected_record(
        self,
        *,
        actor_authority: str,
        expected_epoch: str,
        job_reference: str,
    ) -> ForgeSelectedRevisionRecord:
        """Resolve private handoff authority for one presenter-owned live result."""
        with self._lock:
            self._require_epoch(expected_epoch)
            actor = self._require_actor(actor_authority)
            job = self._require_job(job_reference, actor.actor_id)
            record = self._selected_records_by_job_reference.get(job.reference)
            if job.state is not ForgeJobState.SELECTED or record is None:
                raise ForgeServiceError(
                    "job_not_found",
                    "The selected World Forge revision is unavailable for handoff.",
                    status=404,
                    retry="none",
                )
            if (
                _parse_zoned_datetime(record.expires_at, "selected record expiry")
                <= self._now()
            ):
                raise ForgeServiceError(
                    "job_not_found",
                    "The selected World Forge revision is no longer available for handoff.",
                    status=404,
                    retry="none",
                )
            return record

    def _transition_locked(
        self,
        job: _ForgeJob,
        state: ForgeJobState,
        *,
        safe_message: str | None = None,
        selected_receipt: Mapping[str, object] | None = None,
    ) -> None:
        if state not in _FORGE_TRANSITIONS.get(job.state, frozenset()):
            raise ForgeServiceError(
                "transition_invalid",
                "The requested World Forge lifecycle transition is invalid.",
                status=409,
                retry="resume",
            )
        now = self._now()
        deadline = _parse_zoned_datetime(job.deadline_at, "job deadline")
        if state is ForgeJobState.SELECTED and now >= deadline:
            state = ForgeJobState.DEADLINE_EXCEEDED
            selected_receipt = None
            safe_message = "The World Forge build exceeded its deadline."
        if state is ForgeJobState.DEADLINE_EXCEEDED and now < deadline:
            raise ForgeServiceError(
                "transition_invalid",
                "The World Forge job deadline has not elapsed.",
                status=409,
                retry="resume",
            )
        if (
            state is ForgeJobState.SELECTED
            and selected_receipt is not None
            and selected_receipt.get("provenance") != self._source
        ):
            raise ForgeServiceError(
                "transition_invalid",
                "The World Forge result provenance is invalid.",
                status=409,
                retry="none",
            )
        selected_record = None
        if (
            state is ForgeJobState.SELECTED
            and selected_receipt is not None
            and self._is_authoritative_live_source()
        ):
            selected_record = self._build_selected_record(
                job=job, receipt=selected_receipt, selected_at=now
            )
        job.state = state
        job.changed_at = _format_time(now)
        job.safe_message = safe_message
        if state is ForgeJobState.SELECTED:
            if selected_receipt is not None:
                job.receipt = deepcopy(dict(selected_receipt))
            else:
                revision = _preview_revision(job)
                digest = _sha256_document(revision)
                job.receipt = {
                    "validation": {
                        "state": "passed",
                        "safe_summary": "The deterministic preview candidate passed its configured checks.",
                    },
                    "fixed_selection_reason": "Attempt A was the first candidate in the fixed valid-attempt order.",
                    "revision": {
                        "revision_id": f"world-revision:{digest[:24]}",
                        "revision_label": f"Audience build {job.job_id[-12:]}",
                        "immutable_digest": f"sha256:{digest}",
                    },
                    "human_selection": "none",
                    "provenance": deepcopy(self._source),
                }
        elif state is ForgeJobState.CANCELLED:
            job.safe_message = safe_message or "The World Forge build was cancelled."
        elif state is ForgeJobState.FAILED:
            job.safe_message = safe_message or "The World Forge preview build failed."
        elif state is ForgeJobState.DEADLINE_EXCEEDED:
            job.safe_message = (
                safe_message or "The World Forge build exceeded its deadline."
            )
        self._append_event(job)
        if selected_record is not None:
            self._selected_records_by_job_reference[job.reference] = selected_record

    def _is_authoritative_live_source(self) -> bool:
        return (
            self._project_operation_source
            and self._source.get("provider_calls") is True
            and self._source.get("live_forge_evidence") is True
        )

    def _build_selected_record(
        self,
        *,
        job: _ForgeJob,
        receipt: Mapping[str, object],
        selected_at: datetime,
    ) -> ForgeSelectedRevisionRecord:
        validation = receipt.get("validation")
        revision = receipt.get("revision")
        source_kind = self._source.get("kind")
        if (
            not isinstance(validation, Mapping)
            or validation.get("state") != "passed"
            or not isinstance(revision, Mapping)
            or not isinstance(source_kind, str)
            or not source_kind
        ):
            raise ForgeServiceError(
                "transition_invalid",
                "The World Forge selected receipt is invalid.",
                status=409,
                retry="none",
            )
        revision_id = revision.get("revision_id")
        immutable_digest = revision.get("immutable_digest")
        if (
            not isinstance(revision_id, str)
            or not revision_id
            or not isinstance(immutable_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", immutable_digest) is None
        ):
            raise ForgeServiceError(
                "transition_invalid",
                "The World Forge selected revision identity is invalid.",
                status=409,
                retry="none",
            )
        try:
            source_fingerprint = f"sha256:{_sha256_document(self._source)}"
            receipt_fingerprint = f"sha256:{_sha256_document(dict(receipt))}"
        except (TypeError, ValueError):
            raise ForgeServiceError(
                "transition_invalid",
                "The World Forge selected receipt is invalid.",
                status=409,
                retry="none",
            ) from None
        return ForgeSelectedRevisionRecord(
            selected_record_id=self._selected_record_token_factory(
                "forge-selected-record"
            ),
            status="selected",
            service_epoch=self._service_epoch,
            job_id=job.job_id,
            job_reference=job.reference,
            actor_id=job.actor_id,
            selected_at=_format_time(selected_at),
            expires_at=_format_time(
                selected_at
                + timedelta(seconds=self._policy.selected_handoff_ttl_seconds)
            ),
            policy_id=self._policy.selected_handoff_policy_id,
            revision_id=revision_id,
            immutable_digest=immutable_digest,
            source_kind=source_kind,
            source_fingerprint=source_fingerprint,
            receipt_correlation_id=self._selected_record_token_factory("forge-receipt"),
            receipt_fingerprint=receipt_fingerprint,
        )

    def _projection(self, job: _ForgeJob) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": "forge_job_projection",
            "service_epoch": self._service_epoch,
            "job_id": job.job_id,
            "reference": job.reference,
            "seed": job.seed.to_document(),
            "lifecycle": {"state": job.state.value, "changed_at": job.changed_at},
            "deadline_at": job.deadline_at,
            "position": self._position(job),
            "terminal": job.state in _TERMINAL_FORGE_STATES,
            "receipt": deepcopy(job.receipt),
            "cancellation": (
                {
                    "state": "unavailable",
                    "safe_reason": "The World Forge build is terminal.",
                }
                if job.state in _TERMINAL_FORGE_STATES
                else {"state": "available"}
            ),
            "source": deepcopy(self._source),
        }
        if job.safe_message is not None:
            result["lifecycle"]["safe_message"] = job.safe_message  # type: ignore[index]
        return result

    def _append_event(self, job: _ForgeJob) -> None:
        sequence = len(job.events) + 1
        cursor = self._token_factory("forge-cursor")
        event_id = self._token_factory("forge-event")
        job.events.append(
            {
                "stream_id": job.stream_id,
                "event_id": event_id,
                "sequence": sequence,
                "cursor": cursor,
                "job_id": job.job_id,
                "state": job.state.value,
                "changed_at": job.changed_at,
                "terminal": job.state in _TERMINAL_FORGE_STATES,
                "service_epoch": self._service_epoch,
                "requires_snapshot_reconciliation": True,
            }
        )
        job.cursors[cursor] = sequence

    def _position(self, job: _ForgeJob) -> dict[str, object]:
        if not job.events:
            raise RuntimeError("World Forge job has no accepted event")
        event = job.events[-1]
        return {
            "service_epoch": self._service_epoch,
            "stream_id": job.stream_id,
            "last_event_id": event["event_id"],
            "sequence": event["sequence"],
            "cursor": event["cursor"],
        }

    def _require_epoch(self, expected: str) -> None:
        if expected != self._service_epoch:
            raise ForgeServiceError(
                "service_epoch_changed",
                "This development World Forge build was lost when the service restarted.",
                status=409,
                retry="none",
            )

    def _require_actor(self, authority: str) -> ForgeActorSeed:
        actor = self._actors.get(authority)
        if (
            actor is None
            or _parse_zoned_datetime(actor.expires_at, "actor expiry") <= self._now()
        ):
            raise ForgeServiceError(
                "authority_denied",
                "The requested World Forge resource is unavailable.",
                status=403,
            )
        return actor

    def _require_job(self, reference: str, actor_id: str) -> _ForgeJob:
        job = self._jobs_by_reference.get(reference)
        if job is None or job.actor_id != actor_id:
            raise ForgeServiceError(
                "job_not_found", "The World Forge job is unavailable.", status=404
            )
        return job

    def _require_preparation(self, reference: str, actor_id: str) -> _ForgePreparation:
        record = self._preparations.get(reference)
        if record is None or record.actor_id != actor_id:
            raise ForgeServiceError(
                "preparation_not_found",
                "The World Forge preparation is unavailable.",
                status=404,
                retry="none",
            )
        return record

    def _prune_preparations(self, now: datetime) -> None:
        expired = tuple(
            reference
            for reference, record in self._preparations.items()
            if record.consumed_job_id is None
            and _parse_zoned_datetime(record.expires_at, "preparation expiry") <= now
        )
        for reference in expired:
            del self._preparations[reference]

    def _replay(
        self, scope: tuple[str, str, str], body_hash: str
    ) -> dict[str, object] | None:
        record = self._idempotency.get(scope)
        if record is None:
            return None
        if record.body_hash != body_hash:
            raise ForgeServiceError(
                "idempotency_conflict",
                "The idempotency key is already bound to a different request.",
                status=409,
            )
        if record.error is not None:
            raise record.error
        result = deepcopy(record.result)
        if result is None:
            raise RuntimeError("World Forge idempotency record is incomplete")
        result["replayed"] = True
        return result

    def _require_idempotency_capacity(self) -> None:
        if len(self._idempotency) >= self._policy.maximum_idempotency_records:
            raise ForgeServiceError(
                "idempotency_capacity_exhausted",
                "World Forge request replay capacity is exhausted.",
                status=503,
                retry="none",
            )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("World Forge clock must return a zoned datetime")
        return value


def _safe_text(value: str) -> dict[str, object]:
    return {"text": value, "clipped": False, "controls_marked": False}


def _preparation_display(preparation: WorldSeedPreparation) -> dict[str, object]:
    return {
        "answers": [
            {
                "field_id": answer.field_id,
                "kind": answer.kind,
                "value": _safe_text(answer.value),
            }
            for answer in preparation.answers
        ],
        "rendered_sentence": _safe_text(preparation.rendered_sentence),
    }


def _preview_source() -> dict[str, object]:
    return {
        "kind": "provider_free_integration_preview",
        "label": "Deterministic no-provider World Forge backend integration preview",
        "provider_calls": False,
        "live_forge_evidence": False,
    }


def _preview_revision(job: _ForgeJob) -> dict[str, object]:
    """Return a deterministic fixture profile artifact, never launch authority."""
    seed_values = {answer.field_id: answer.value for answer in job.seed.answers}
    return {
        "kind": "dreamland.world_forge_preview_revision",
        "schema_revision": "1",
        "seed_hash": job.seed.seed_hash,
        "profile": {
            "level": "world",
            "world_state": {
                "name": "provider-free-integration-preview",
                "entry_host": "preview-entry",
                "active_host": "preview-entry",
                "purpose": {
                    "theme": f"{seed_values['adjective']} {seed_values['industry-or-setting']}",
                    "description": job.seed.rendered_sentence,
                },
                "hosts_by_name": {
                    "preview-entry": {
                        "hostname": "preview-entry",
                        "purpose": {"theme": seed_values["important-job"]},
                        "shell": {
                            "default": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["bash"],
                            }
                        },
                    }
                },
            },
        },
        "authority": "none",
    }


def _require_forge_key(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ForgeServiceError(
            "idempotency_key_invalid",
            "A valid idempotency key is required.",
            status=400,
        )
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ForgeServiceError(
            "idempotency_key_invalid",
            "A valid idempotency key is required.",
            status=400,
        )
    return value


def _sha256_document(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _parse_zoned_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{label} must be a valid timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _format_time(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
