from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

from dreamland.background_agents import (
    EventSource,
    SessionEvent,
    SessionEventType,
    TriggerCandidate,
    TriggerDecisionCode,
    TriggerDecisionReason,
    TriggerGatePolicy,
    TriggerGateState,
    TriggerMetric,
    evaluate_trigger_gate,
)


def _event(
    *,
    source: EventSource = EventSource.COMMAND_EMULATOR,
    source_agent_id: str | None = None,
) -> SessionEvent:
    return SessionEvent(
        event_id="evt-1",
        correlation_id="trace-1",
        session_id="session-1",
        event_type=SessionEventType.STATE_MATERIALIZED,
        source=source,
        source_agent_id=source_agent_id,
        occurred_at_ms=1_000,
        command_id="command-1",
        target_host="cache01",
        surface="service",
        anchors=("redis",),
        payload={"paths": ["/etc/redis/redis.conf"], "changed": True},
    )


def _candidate(
    *,
    agent_id: str = "enrichment",
    event: SessionEvent | None = None,
    deduplication_key: str = "session-1:cache01:service:redis",
) -> TriggerCandidate:
    return TriggerCandidate(
        candidate_id=f"candidate-{agent_id}",
        trigger_id="anchor_materialized",
        agent_id=agent_id,
        event=_event() if event is None else event,
        deduplication_key=deduplication_key,
        created_at_ms=1_010,
        target_host="cache01",
        surface="service",
        anchors=("redis",),
        details={"activity_epoch": 3},
    )


class SessionEventTests(unittest.TestCase):
    def test_event_types_cover_the_shared_session_signals(self):
        self.assertEqual(
            {event_type.value for event_type in SessionEventType},
            {
                "session.registered",
                "command.accepted",
                "command.completed",
                "active_host.changed",
                "state.materialized",
                "session.idle",
                "session.closing",
            },
        )

    def test_records_are_deeply_immutable_and_plain_json_is_serializable(self):
        event = _event()
        candidate = _candidate(event=event)

        with self.assertRaises(FrozenInstanceError):
            event.session_id = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            event.payload["changed"] = False  # type: ignore[index]
        with self.assertRaises(TypeError):
            event.payload["paths"][0] = "/tmp/nope"  # type: ignore[index]

        plain = candidate.to_dict()
        self.assertEqual(plain["event"]["source"], "command_emulator")
        self.assertEqual(plain["event"]["payload"]["paths"], ["/etc/redis/redis.conf"])
        json.dumps(plain)

    def test_background_agent_source_requires_a_named_agent(self):
        with self.assertRaisesRegex(ValueError, "source_agent_id"):
            _event(source=EventSource.BACKGROUND_AGENT)


class TriggerGateTests(unittest.TestCase):
    def test_source_suppression_is_independent_for_each_subscriber(self):
        event = _event(
            source=EventSource.BACKGROUND_AGENT,
            source_agent_id="enrichment",
        )
        enrichment = _candidate(agent_id="enrichment", event=event)
        correction = _candidate(agent_id="correction", event=event)
        state = TriggerGateState(now_ms=1_100)

        enrichment_decision = evaluate_trigger_gate(
            enrichment,
            TriggerGatePolicy(agent_id="enrichment"),
            state,
        )
        correction_decision = evaluate_trigger_gate(
            correction,
            TriggerGatePolicy(agent_id="correction"),
            state,
        )

        self.assertEqual(
            enrichment_decision.reason,
            TriggerDecisionReason.SOURCE_SELF_SUPPRESSED,
        )
        self.assertEqual(enrichment_decision.observed, "enrichment")
        self.assertEqual(correction_decision.code, TriggerDecisionCode.ACCEPTED)

    def test_pending_duplicate_is_coalesced_before_inflight_suppression(self):
        candidate = _candidate()
        decision = evaluate_trigger_gate(
            candidate,
            TriggerGatePolicy(agent_id="enrichment", max_inflight=1),
            TriggerGateState(
                now_ms=1_100,
                inflight=1,
                pending_by_deduplication_key={
                    candidate.deduplication_key: "candidate-running"
                },
            ),
        )

        self.assertEqual(decision.code, TriggerDecisionCode.COALESCED)
        self.assertEqual(
            decision.reason,
            TriggerDecisionReason.DUPLICATE_COALESCED,
        )
        self.assertEqual(
            decision.coalesced_into_candidate_id,
            "candidate-running",
        )

    def test_inflight_limit_explains_observed_and_threshold(self):
        decision = evaluate_trigger_gate(
            _candidate(),
            TriggerGatePolicy(agent_id="enrichment", max_inflight=2),
            TriggerGateState(now_ms=1_100, inflight=2),
        )

        self.assertEqual(
            decision.reason,
            TriggerDecisionReason.INFLIGHT_LIMIT_REACHED,
        )
        self.assertEqual(decision.metric, TriggerMetric.INFLIGHT_JOBS)
        self.assertEqual(decision.observed, 2)
        self.assertEqual(decision.threshold, 2)
        self.assertIsNone(decision.retry_after_ms)

    def test_cooldown_reports_retry_and_accepts_at_boundary(self):
        candidate = _candidate()
        policy = TriggerGatePolicy(agent_id="enrichment", cooldown_ms=500)

        active = evaluate_trigger_gate(
            candidate,
            policy,
            TriggerGateState(now_ms=1_300, last_accepted_at_ms=1_000),
        )
        boundary = evaluate_trigger_gate(
            candidate,
            policy,
            TriggerGateState(now_ms=1_500, last_accepted_at_ms=1_000),
        )

        self.assertEqual(active.reason, TriggerDecisionReason.COOLDOWN_ACTIVE)
        self.assertEqual(active.observed, 300)
        self.assertEqual(active.threshold, 500)
        self.assertEqual(active.retry_after_ms, 200)
        self.assertEqual(boundary.code, TriggerDecisionCode.ACCEPTED)

    def test_rate_limit_reports_window_and_expires_deterministically(self):
        candidate = _candidate()
        policy = TriggerGatePolicy(
            agent_id="enrichment",
            rate_limit_count=3,
            rate_limit_window_ms=10_000,
        )

        limited = evaluate_trigger_gate(
            candidate,
            policy,
            TriggerGateState(
                now_ms=8_000,
                rate_window_started_at_ms=1_000,
                accepted_in_window=3,
            ),
        )
        expired = evaluate_trigger_gate(
            candidate,
            policy,
            TriggerGateState(
                now_ms=11_000,
                rate_window_started_at_ms=1_000,
                accepted_in_window=3,
            ),
        )

        self.assertEqual(
            limited.reason,
            TriggerDecisionReason.RATE_LIMIT_REACHED,
        )
        self.assertEqual(limited.metric, TriggerMetric.ACCEPTED_IN_WINDOW)
        self.assertEqual(limited.observed, 3)
        self.assertEqual(limited.threshold, 3)
        self.assertEqual(limited.retry_after_ms, 3_000)
        self.assertEqual(limited.details["window_ms"], 10_000)
        self.assertEqual(expired.code, TriggerDecisionCode.ACCEPTED)

    def test_policy_must_belong_to_candidate_agent(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            evaluate_trigger_gate(
                _candidate(agent_id="enrichment"),
                TriggerGatePolicy(agent_id="correction"),
                TriggerGateState(now_ms=1_100),
            )

    def test_rate_count_requires_a_window_start(self):
        with self.assertRaisesRegex(ValueError, "requires rate_window_started"):
            TriggerGateState(now_ms=1_100, accepted_in_window=1)


if __name__ == "__main__":
    unittest.main()
