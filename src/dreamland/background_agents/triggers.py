"""Pure trigger gates shared by independently configured background agents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from dreamland.background_agents.events import (
    EventSource,
    TriggerCandidate,
    TriggerDecision,
    TriggerDecisionCode,
    TriggerDecisionReason,
    TriggerMetric,
)


def _empty_pending() -> Mapping[str, str]:
    return MappingProxyType({})


def _validate_nonnegative_integer(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class TriggerGatePolicy:
    """Limits for one subscriber; policies are never shared between agents."""

    agent_id: str
    suppress_self_generated: bool = True
    coalesce_pending: bool = True
    cooldown_ms: int = 0
    rate_limit_count: int | None = None
    rate_limit_window_ms: int | None = None
    max_inflight: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
        _validate_nonnegative_integer("cooldown_ms", self.cooldown_ms)
        _validate_nonnegative_integer("rate_limit_count", self.rate_limit_count)
        _validate_nonnegative_integer(
            "rate_limit_window_ms",
            self.rate_limit_window_ms,
        )
        _validate_nonnegative_integer("max_inflight", self.max_inflight)
        if (self.rate_limit_count is None) != (self.rate_limit_window_ms is None):
            raise ValueError(
                "rate_limit_count and rate_limit_window_ms must be set together"
            )
        if self.rate_limit_count == 0:
            raise ValueError("rate_limit_count must be positive when configured")
        if self.rate_limit_window_ms == 0:
            raise ValueError("rate_limit_window_ms must be positive when configured")


@dataclass(frozen=True, slots=True)
class TriggerGateState:
    """A caller-supplied gate snapshot using one monotonic millisecond clock."""

    now_ms: int
    last_accepted_at_ms: int | None = None
    rate_window_started_at_ms: int | None = None
    accepted_in_window: int = 0
    inflight: int = 0
    pending_by_deduplication_key: Mapping[str, str] = field(
        default_factory=_empty_pending
    )

    def __post_init__(self) -> None:
        for name in (
            "now_ms",
            "last_accepted_at_ms",
            "rate_window_started_at_ms",
            "accepted_in_window",
            "inflight",
        ):
            _validate_nonnegative_integer(name, getattr(self, name))
        if (
            self.last_accepted_at_ms is not None
            and self.last_accepted_at_ms > self.now_ms
        ):
            raise ValueError("last_accepted_at_ms cannot be later than now_ms")
        if (
            self.rate_window_started_at_ms is not None
            and self.rate_window_started_at_ms > self.now_ms
        ):
            raise ValueError("rate_window_started_at_ms cannot be later than now_ms")
        if self.accepted_in_window and self.rate_window_started_at_ms is None:
            raise ValueError("accepted_in_window requires rate_window_started_at_ms")
        pending: dict[str, str] = {}
        for key, candidate_id in self.pending_by_deduplication_key.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("pending deduplication keys must be non-empty strings")
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                raise ValueError("pending candidate IDs must be non-empty strings")
            pending[key] = candidate_id
        object.__setattr__(
            self,
            "pending_by_deduplication_key",
            MappingProxyType(pending),
        )


def _ensure_policy_matches_candidate(
    candidate: TriggerCandidate,
    policy: TriggerGatePolicy,
) -> None:
    if candidate.agent_id != policy.agent_id:
        raise ValueError("trigger policy agent_id does not match candidate agent_id")


def evaluate_self_source_gate(
    candidate: TriggerCandidate,
    policy: TriggerGatePolicy,
    state: TriggerGateState,
) -> TriggerDecision | None:
    """Suppress a feedback loop only for the agent that emitted the event."""

    _ensure_policy_matches_candidate(candidate, policy)
    source_agent_id = candidate.event.source_agent_id
    if (
        not policy.suppress_self_generated
        or candidate.event.source is not EventSource.BACKGROUND_AGENT
        or source_agent_id != candidate.agent_id
    ):
        return None
    return TriggerDecision.for_candidate(
        candidate,
        decided_at_ms=state.now_ms,
        code=TriggerDecisionCode.SUPPRESSED,
        reason=TriggerDecisionReason.SOURCE_SELF_SUPPRESSED,
        policy_name="source_self_suppression",
        metric=TriggerMetric.SOURCE_AGENT_ID,
        observed=source_agent_id,
        threshold=candidate.agent_id,
    )


def evaluate_coalescing_gate(
    candidate: TriggerCandidate,
    policy: TriggerGatePolicy,
    state: TriggerGateState,
) -> TriggerDecision | None:
    """Coalesce a duplicate into the already-pending candidate for this agent."""

    _ensure_policy_matches_candidate(candidate, policy)
    if not policy.coalesce_pending:
        return None
    pending_id = state.pending_by_deduplication_key.get(candidate.deduplication_key)
    if pending_id is None:
        return None
    return TriggerDecision.for_candidate(
        candidate,
        decided_at_ms=state.now_ms,
        code=TriggerDecisionCode.COALESCED,
        reason=TriggerDecisionReason.DUPLICATE_COALESCED,
        policy_name="pending_deduplication",
        metric=TriggerMetric.DEDUPLICATION_KEY,
        observed=candidate.deduplication_key,
        threshold="pending",
        coalesced_into_candidate_id=pending_id,
    )


def evaluate_inflight_gate(
    candidate: TriggerCandidate,
    policy: TriggerGatePolicy,
    state: TriggerGateState,
) -> TriggerDecision | None:
    _ensure_policy_matches_candidate(candidate, policy)
    if policy.max_inflight is None or state.inflight < policy.max_inflight:
        return None
    return TriggerDecision.for_candidate(
        candidate,
        decided_at_ms=state.now_ms,
        code=TriggerDecisionCode.SUPPRESSED,
        reason=TriggerDecisionReason.INFLIGHT_LIMIT_REACHED,
        policy_name="max_inflight",
        metric=TriggerMetric.INFLIGHT_JOBS,
        observed=state.inflight,
        threshold=policy.max_inflight,
    )


def evaluate_cooldown_gate(
    candidate: TriggerCandidate,
    policy: TriggerGatePolicy,
    state: TriggerGateState,
) -> TriggerDecision | None:
    _ensure_policy_matches_candidate(candidate, policy)
    if policy.cooldown_ms == 0 or state.last_accepted_at_ms is None:
        return None
    elapsed_ms = state.now_ms - state.last_accepted_at_ms
    if elapsed_ms >= policy.cooldown_ms:
        return None
    return TriggerDecision.for_candidate(
        candidate,
        decided_at_ms=state.now_ms,
        code=TriggerDecisionCode.SUPPRESSED,
        reason=TriggerDecisionReason.COOLDOWN_ACTIVE,
        policy_name="cooldown",
        metric=TriggerMetric.COOLDOWN_ELAPSED_MS,
        observed=elapsed_ms,
        threshold=policy.cooldown_ms,
        retry_after_ms=policy.cooldown_ms - elapsed_ms,
    )


def evaluate_rate_limit_gate(
    candidate: TriggerCandidate,
    policy: TriggerGatePolicy,
    state: TriggerGateState,
) -> TriggerDecision | None:
    _ensure_policy_matches_candidate(candidate, policy)
    if policy.rate_limit_count is None or policy.rate_limit_window_ms is None:
        return None
    if state.rate_window_started_at_ms is None:
        return None
    elapsed_ms = state.now_ms - state.rate_window_started_at_ms
    if elapsed_ms >= policy.rate_limit_window_ms:
        return None
    if state.accepted_in_window < policy.rate_limit_count:
        return None
    return TriggerDecision.for_candidate(
        candidate,
        decided_at_ms=state.now_ms,
        code=TriggerDecisionCode.SUPPRESSED,
        reason=TriggerDecisionReason.RATE_LIMIT_REACHED,
        policy_name="rate_limit",
        metric=TriggerMetric.ACCEPTED_IN_WINDOW,
        observed=state.accepted_in_window,
        threshold=policy.rate_limit_count,
        retry_after_ms=policy.rate_limit_window_ms - elapsed_ms,
        details={
            "window_ms": policy.rate_limit_window_ms,
            "window_elapsed_ms": elapsed_ms,
        },
    )


def evaluate_trigger_gate(
    candidate: TriggerCandidate,
    policy: TriggerGatePolicy,
    state: TriggerGateState,
) -> TriggerDecision:
    """Evaluate gates in a stable order without mutating or scheduling anything."""

    _ensure_policy_matches_candidate(candidate, policy)
    for gate in (
        evaluate_self_source_gate,
        evaluate_coalescing_gate,
        evaluate_inflight_gate,
        evaluate_cooldown_gate,
        evaluate_rate_limit_gate,
    ):
        decision = gate(candidate, policy, state)
        if decision is not None:
            return decision
    return TriggerDecision.for_candidate(
        candidate,
        decided_at_ms=state.now_ms,
        code=TriggerDecisionCode.ACCEPTED,
        reason=TriggerDecisionReason.ELIGIBLE,
        policy_name="eligible",
    )
