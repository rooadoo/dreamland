"""Shared runtime building blocks for DREAMLAND background agents."""

from dreamland.background_agents.event_bus import (
    EventSubscription,
    SessionEventBus,
    new_session_event,
)
from dreamland.background_agents.events import (
    EventSource,
    SessionEvent,
    SessionEventType,
    TriggerCandidate,
    TriggerDecision,
    TriggerDecisionCode,
    TriggerDecisionReason,
    TriggerMetric,
)
from dreamland.background_agents.surfaces import infer_command_surface
from dreamland.background_agents.triggers import (
    TriggerGatePolicy,
    TriggerGateState,
    evaluate_coalescing_gate,
    evaluate_cooldown_gate,
    evaluate_inflight_gate,
    evaluate_rate_limit_gate,
    evaluate_self_source_gate,
    evaluate_trigger_gate,
)

__all__ = [
    "EventSource",
    "EventSubscription",
    "SessionEventBus",
    "SessionEvent",
    "SessionEventType",
    "TriggerCandidate",
    "TriggerDecision",
    "TriggerDecisionCode",
    "TriggerDecisionReason",
    "TriggerGatePolicy",
    "TriggerGateState",
    "TriggerMetric",
    "evaluate_coalescing_gate",
    "evaluate_cooldown_gate",
    "evaluate_inflight_gate",
    "evaluate_rate_limit_gate",
    "evaluate_self_source_gate",
    "evaluate_trigger_gate",
    "infer_command_surface",
    "new_session_event",
]
