"""Immutable, JSON-compatible records shared by background-agent flows."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


class SessionEventType(StrEnum):
    """Stable signals emitted by a temporal DREAMLAND session."""

    SESSION_REGISTERED = "session.registered"
    COMMAND_ACCEPTED = "command.accepted"
    COMMAND_COMPLETED = "command.completed"
    ACTIVE_HOST_CHANGED = "active_host.changed"
    STATE_MATERIALIZED = "state.materialized"
    SESSION_IDLE = "session.idle"
    SESSION_CLOSING = "session.closing"


class EventSource(StrEnum):
    """Stable producer categories; ``source_agent_id`` identifies agent roles."""

    SESSION_RUNTIME = "session_runtime"
    USER_COMMAND = "user_command"
    COMMAND_EMULATOR = "command_emulator"
    SPIRITGUIDE = "spiritguide"
    BACKGROUND_AGENT = "background_agent"
    ADMIN = "admin"


class TriggerDecisionCode(StrEnum):
    """Whether a subscriber should start, suppress, or coalesce work."""

    ACCEPTED = "accepted"
    SUPPRESSED = "suppressed"
    COALESCED = "coalesced"


class TriggerDecisionReason(StrEnum):
    """Stable reason codes intended for logs, traces, and metrics."""

    ELIGIBLE = "eligible"
    SOURCE_SELF_SUPPRESSED = "source_self_suppressed"
    DUPLICATE_COALESCED = "duplicate_coalesced"
    INFLIGHT_LIMIT_REACHED = "inflight_limit_reached"
    COOLDOWN_ACTIVE = "cooldown_active"
    RATE_LIMIT_REACHED = "rate_limit_reached"


class TriggerMetric(StrEnum):
    """Names the quantity represented by ``observed`` and ``threshold``."""

    SOURCE_AGENT_ID = "source_agent_id"
    DEDUPLICATION_KEY = "deduplication_key"
    INFLIGHT_JOBS = "inflight_jobs"
    COOLDOWN_ELAPSED_MS = "cooldown_elapsed_ms"
    ACCEPTED_IN_WINDOW = "accepted_in_window"


def _empty_json_object() -> Mapping[str, JsonValue]:
    return MappingProxyType({})


def _freeze_json(value: Any, *, path: str = "payload") -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} keys must be strings")
            frozen[key] = _freeze_json(nested_value, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(nested_value, path=f"{path}[{index}]")
            for index, nested_value in enumerate(value)
        )
    raise TypeError(f"{path} contains unsupported value {type(value).__name__}")


def _plain_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(nested) for nested in value]
    return value


def _require_nonempty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _validate_optional_text(name: str, value: str | None) -> None:
    if value is not None:
        _require_nonempty(name, value)


def _validate_milliseconds(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """One neutral session signal that any background agent may inspect."""

    event_id: str
    correlation_id: str
    session_id: str
    event_type: SessionEventType
    source: EventSource
    occurred_at_ms: int
    caused_by_event_id: str | None = None
    source_agent_id: str | None = None
    command_id: str | None = None
    target_host: str | None = None
    surface: str | None = None
    anchors: tuple[str, ...] = ()
    payload: Mapping[str, JsonValue] = field(default_factory=_empty_json_object)

    def __post_init__(self) -> None:
        for name in ("event_id", "correlation_id", "session_id"):
            _require_nonempty(name, getattr(self, name))
        if not isinstance(self.event_type, SessionEventType):
            raise TypeError("event_type must be a SessionEventType")
        if not isinstance(self.source, EventSource):
            raise TypeError("source must be an EventSource")
        _validate_milliseconds("occurred_at_ms", self.occurred_at_ms)
        for name in (
            "caused_by_event_id",
            "source_agent_id",
            "command_id",
            "target_host",
            "surface",
        ):
            _validate_optional_text(name, getattr(self, name))
        if self.caused_by_event_id == self.event_id:
            raise ValueError("an event cannot be caused by itself")
        if self.source is EventSource.BACKGROUND_AGENT and self.source_agent_id is None:
            raise ValueError("background-agent events require source_agent_id")
        if any(
            not isinstance(anchor, str) or not anchor.strip() for anchor in self.anchors
        ):
            raise ValueError("anchors must contain non-empty strings")
        object.__setattr__(self, "anchors", tuple(self.anchors))
        frozen_payload = _freeze_json(self.payload)
        if not isinstance(frozen_payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "payload", frozen_payload)

    def to_dict(self) -> dict[str, Any]:
        """Return a mutable plain-JSON representation for logging or transport."""

        return {
            "event_id": self.event_id,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "event_type": self.event_type.value,
            "source": self.source.value,
            "occurred_at_ms": self.occurred_at_ms,
            "caused_by_event_id": self.caused_by_event_id,
            "source_agent_id": self.source_agent_id,
            "command_id": self.command_id,
            "target_host": self.target_host,
            "surface": self.surface,
            "anchors": list(self.anchors),
            "payload": _plain_json(self.payload),
        }


@dataclass(frozen=True, slots=True)
class TriggerCandidate:
    """An agent-specific interpretation of a reusable session event."""

    candidate_id: str
    trigger_id: str
    agent_id: str
    event: SessionEvent
    deduplication_key: str
    created_at_ms: int
    target_host: str | None = None
    surface: str | None = None
    anchors: tuple[str, ...] = ()
    details: Mapping[str, JsonValue] = field(default_factory=_empty_json_object)

    def __post_init__(self) -> None:
        for name in ("candidate_id", "trigger_id", "agent_id", "deduplication_key"):
            _require_nonempty(name, getattr(self, name))
        if not isinstance(self.event, SessionEvent):
            raise TypeError("event must be a SessionEvent")
        _validate_milliseconds("created_at_ms", self.created_at_ms)
        _validate_optional_text("target_host", self.target_host)
        _validate_optional_text("surface", self.surface)
        if any(
            not isinstance(anchor, str) or not anchor.strip() for anchor in self.anchors
        ):
            raise ValueError("anchors must contain non-empty strings")
        object.__setattr__(self, "anchors", tuple(self.anchors))
        frozen_details = _freeze_json(self.details, path="details")
        if not isinstance(frozen_details, Mapping):
            raise TypeError("details must be a mapping")
        object.__setattr__(self, "details", frozen_details)

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def correlation_id(self) -> str:
        return self.event.correlation_id

    @property
    def session_id(self) -> str:
        return self.event.session_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "trigger_id": self.trigger_id,
            "agent_id": self.agent_id,
            "event": self.event.to_dict(),
            "deduplication_key": self.deduplication_key,
            "created_at_ms": self.created_at_ms,
            "target_host": self.target_host,
            "surface": self.surface,
            "anchors": list(self.anchors),
            "details": _plain_json(self.details),
        }


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    """A deterministic gate result with enough context to explain it in logs."""

    candidate_id: str
    trigger_id: str
    agent_id: str
    event_id: str
    correlation_id: str
    session_id: str
    decided_at_ms: int
    code: TriggerDecisionCode
    reason: TriggerDecisionReason
    policy_name: str
    metric: TriggerMetric | None = None
    observed: JsonScalar = None
    threshold: JsonScalar = None
    retry_after_ms: int | None = None
    coalesced_into_candidate_id: str | None = None
    details: Mapping[str, JsonValue] = field(default_factory=_empty_json_object)

    def __post_init__(self) -> None:
        for name in (
            "candidate_id",
            "trigger_id",
            "agent_id",
            "event_id",
            "correlation_id",
            "session_id",
            "policy_name",
        ):
            _require_nonempty(name, getattr(self, name))
        _validate_milliseconds("decided_at_ms", self.decided_at_ms)
        if not isinstance(self.code, TriggerDecisionCode):
            raise TypeError("code must be a TriggerDecisionCode")
        if not isinstance(self.reason, TriggerDecisionReason):
            raise TypeError("reason must be a TriggerDecisionReason")
        if self.metric is not None and not isinstance(self.metric, TriggerMetric):
            raise TypeError("metric must be a TriggerMetric or None")
        for name in ("observed", "threshold"):
            frozen_value = _freeze_json(getattr(self, name), path=name)
            if isinstance(frozen_value, (Mapping, tuple)):
                raise TypeError(f"{name} must be a JSON scalar")
            object.__setattr__(self, name, frozen_value)
        if self.retry_after_ms is not None:
            _validate_milliseconds("retry_after_ms", self.retry_after_ms)
        _validate_optional_text(
            "coalesced_into_candidate_id",
            self.coalesced_into_candidate_id,
        )
        if self.code is TriggerDecisionCode.ACCEPTED:
            if self.reason is not TriggerDecisionReason.ELIGIBLE:
                raise ValueError("accepted decisions must use the eligible reason")
        elif self.code is TriggerDecisionCode.COALESCED:
            if self.reason is not TriggerDecisionReason.DUPLICATE_COALESCED:
                raise ValueError(
                    "coalesced decisions must use the duplicate_coalesced reason"
                )
            if self.coalesced_into_candidate_id is None:
                raise ValueError("coalesced decisions require a target candidate")
        elif self.reason in {
            TriggerDecisionReason.ELIGIBLE,
            TriggerDecisionReason.DUPLICATE_COALESCED,
        }:
            raise ValueError("suppressed decisions require a suppression reason")
        frozen_details = _freeze_json(self.details, path="details")
        if not isinstance(frozen_details, Mapping):
            raise TypeError("details must be a mapping")
        object.__setattr__(self, "details", frozen_details)

    @classmethod
    def for_candidate(
        cls,
        candidate: TriggerCandidate,
        *,
        decided_at_ms: int,
        code: TriggerDecisionCode,
        reason: TriggerDecisionReason,
        policy_name: str,
        metric: TriggerMetric | None = None,
        observed: JsonScalar = None,
        threshold: JsonScalar = None,
        retry_after_ms: int | None = None,
        coalesced_into_candidate_id: str | None = None,
        details: Mapping[str, JsonValue] | None = None,
    ) -> TriggerDecision:
        return cls(
            candidate_id=candidate.candidate_id,
            trigger_id=candidate.trigger_id,
            agent_id=candidate.agent_id,
            event_id=candidate.event_id,
            correlation_id=candidate.correlation_id,
            session_id=candidate.session_id,
            decided_at_ms=decided_at_ms,
            code=code,
            reason=reason,
            policy_name=policy_name,
            metric=metric,
            observed=observed,
            threshold=threshold,
            retry_after_ms=retry_after_ms,
            coalesced_into_candidate_id=coalesced_into_candidate_id,
            details={} if details is None else details,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "trigger_id": self.trigger_id,
            "agent_id": self.agent_id,
            "event_id": self.event_id,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "decided_at_ms": self.decided_at_ms,
            "code": self.code.value,
            "reason": self.reason.value,
            "policy_name": self.policy_name,
            "metric": None if self.metric is None else self.metric.value,
            "observed": self.observed,
            "threshold": self.threshold,
            "retry_after_ms": self.retry_after_ms,
            "coalesced_into_candidate_id": self.coalesced_into_candidate_id,
            "details": _plain_json(self.details),
        }
