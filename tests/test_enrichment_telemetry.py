import logging
import unittest

from dreamland.background_agents.enrichment.telemetry import (
    EnrichmentLogContext,
    log_enrichment_event,
)
from dreamland.config import LoggingSettings
from dreamland.runtime_logging import (
    configure_console_logging,
    configure_enrichment_event_logging,
)


class EnrichmentTelemetryTests(unittest.TestCase):
    def test_context_and_decision_fields_are_grep_friendly(self):
        context = EnrichmentLogContext(
            session_id="session-1",
            job_id="job-1",
            event_id="event-1",
            correlation_id="command-1",
            trigger_id="surface_focus",
            target_host="cache-01",
            requested_surface="network",
            selected_surface="process",
            surface="process",
        )

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            log_enrichment_event(
                "trigger.suppressed",
                context=context,
                reason="rate_limit_reached",
                candidate_id="candidate-1",
                replaced_candidate_id="candidate-0",
                completed_job_id="job-0",
                priority=4,
                age_ms=25,
                max_age_ms=60000,
                focus_path_count=2,
                match_policy="interaction-focus-match-v1",
                match_classification="artifact_exact",
                command_focus_key_sha256="command-focus-digest",
                command_focus_path_count=1,
                command_focus_artifact_count=2,
                retained_focus_key_sha256="retained-focus-digest",
                retained_focus_path_count=3,
                retained_focus_artifact_count=4,
                failure_kind="provider_quota",
                failure_disposition="circuit_opened",
                provider="openai",
                provider_code="credit_balance_exhausted",
                provider_status_code=402,
                role="coordinator",
                model="gpt-5.6-terra",
                circuit_origin="coordinator",
                authority_domain="shell_session",
                binding_reason="latest_foreground",
                policy_version="foreground-authority-v1",
                integrator_refresh_outcome="applied",
                relation_policy_version="expert-relations-v2",
                action="reject_cluster",
                integrator_refresh_attempt=1,
                integrator_refresh_latency_ms=8123,
                authority_dependency_count=2,
                provider_circuit_count=1,
                relation_limit=24,
                relation_received_count=27,
                relation_accepted_count=24,
                relation_invalid_count=0,
                relation_duplicate_count=1,
                relation_overflow_count=2,
                relation_discarded_count=3,
                presence_gap_policy_version="presence-gap-v2",
                presence_gap_sha256="presence-gap-digest",
                presence_gap_ambient_objective="independent_life",
                presence_gap_preferred_surface="network",
                presence_gap_anti_repetition_subject_count=0,
                presence_gap_anti_repetition_system_count=1,
                presence_gap_preferred_observable="users",
                presence_gap_underrepresented_count=4,
                presence_gap_repeated_subject_count=1,
                presence_gap_repeated_system_count=1,
                presence_gap_users_count=2,
                presence_gap_groups_count=3,
                presence_gap_services_count=4,
                presence_gap_filesystem_artifacts_count=5,
                foreground_evidence_policy_version="foreground-evidence-v1",
                foreground_evidence_sha256="foreground-evidence-digest",
                host_time_observed=True,
                recent_exhaustive_evidence_count=2,
                accepted=False,
                eligible=True,
                changed=True,
                foreground_authority_present=True,
                foreground_authority_changed=False,
                observed=6,
                threshold=6,
                retry_after_ms=1200,
                registry_targets=[["services_by_name"]],
                paths=[["services_by_name", "redis"]],
                presence_gap_anti_repetition_subjects=["user:alice"],
                recent_exhaustive_evidence_kinds=["home_listing", "login_history"],
            )

        line = captured.output[0]
        self.assertIn("event=trigger.suppressed", line)
        self.assertIn("session_id=session-1", line)
        self.assertIn("reason=rate_limit_reached", line)
        self.assertIn("requested_surface=network", line)
        self.assertIn("selected_surface=process", line)
        self.assertIn("observed=6 threshold=6 retry_after_ms=1200", line)
        self.assertIn('paths=[["services_by_name","redis"]]', line)
        record = captured.records[0]
        self.assertEqual(record.dreamland_enrichment_event, "trigger.suppressed")
        self.assertEqual(record.dreamland_session_id, "session-1")
        self.assertEqual(record.dreamland_enrichment_job_id, "job-1")
        self.assertEqual(
            record.dreamland_enrichment_fields,
            {
                "candidate_id": "candidate-1",
                "replaced_candidate_id": "candidate-0",
                "completed_job_id": "job-0",
                "correlation_id": "command-1",
                "event_id": "event-1",
                "priority": 4,
                "age_ms": 25,
                "max_age_ms": 60000,
                "focus_path_count": 2,
                "match_policy": "interaction-focus-match-v1",
                "match_classification": "artifact_exact",
                "command_focus_key_sha256": "command-focus-digest",
                "command_focus_path_count": 1,
                "command_focus_artifact_count": 2,
                "retained_focus_key_sha256": "retained-focus-digest",
                "retained_focus_path_count": 3,
                "retained_focus_artifact_count": 4,
                "failure_kind": "provider_quota",
                "failure_disposition": "circuit_opened",
                "provider": "openai",
                "provider_code": "credit_balance_exhausted",
                "provider_status_code": 402,
                "role": "coordinator",
                "model": "gpt-5.6-terra",
                "circuit_origin": "coordinator",
                "authority_domain": "shell_session",
                "binding_reason": "latest_foreground",
                "policy_version": "foreground-authority-v1",
                "integrator_refresh_outcome": "applied",
                "relation_policy_version": "expert-relations-v2",
                "action": "reject_cluster",
                "integrator_refresh_attempt": 1,
                "integrator_refresh_latency_ms": 8123,
                "authority_dependency_count": 2,
                "provider_circuit_count": 1,
                "relation_limit": 24,
                "relation_received_count": 27,
                "relation_accepted_count": 24,
                "relation_invalid_count": 0,
                "relation_duplicate_count": 1,
                "relation_overflow_count": 2,
                "relation_discarded_count": 3,
                "presence_gap_policy_version": "presence-gap-v2",
                "presence_gap_sha256": "presence-gap-digest",
                "presence_gap_ambient_objective": "independent_life",
                "presence_gap_preferred_surface": "network",
                "presence_gap_anti_repetition_subject_count": 0,
                "presence_gap_anti_repetition_system_count": 1,
                "foreground_evidence_policy_version": "foreground-evidence-v1",
                "foreground_evidence_sha256": "foreground-evidence-digest",
                "host_time_observed": True,
                "recent_exhaustive_evidence_count": 2,
                "accepted": False,
                "eligible": True,
                "changed": True,
                "foreground_authority_present": True,
                "foreground_authority_changed": False,
                "reason": "rate_limit_reached",
                "registry_targets": (("services_by_name",),),
                "requested_surface": "network",
                "selected_surface": "process",
                "surface": "process",
                "target_host": "cache-01",
                "trigger_id": "surface_focus",
            },
        )
        self.assertNotIn(
            "presence_gap_anti_repetition_subjects",
            record.dreamland_enrichment_fields,
        )
        for legacy_field in (
            "presence_gap_preferred_observable",
            "presence_gap_underrepresented_count",
            "presence_gap_repeated_subject_count",
            "presence_gap_repeated_system_count",
            "presence_gap_users_count",
            "presence_gap_groups_count",
            "presence_gap_services_count",
            "presence_gap_filesystem_artifacts_count",
        ):
            self.assertNotIn(legacy_field, record.dreamland_enrichment_fields)
        self.assertNotIn(
            "recent_exhaustive_evidence_kinds",
            record.dreamland_enrichment_fields,
        )

    def test_presence_gap_fields_reject_wrong_types(self):
        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            log_enrichment_event(
                "coordinator.context_prepared",
                context=EnrichmentLogContext(session_id="session-1"),
                presence_gap_policy_version=7,
                presence_gap_sha256=False,
                presence_gap_ambient_objective=7,
                presence_gap_preferred_surface=False,
                presence_gap_anti_repetition_subject_count=True,
                presence_gap_anti_repetition_system_count=False,
            )

        fields = captured.records[0].dreamland_enrichment_fields
        self.assertFalse(any(name.startswith("presence_gap_") for name in fields))

    def test_foreground_evidence_typed_fields_reject_wrong_types(self):
        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            log_enrichment_event(
                "coordinator.context_prepared",
                context=EnrichmentLogContext(session_id="session-1"),
                foreground_evidence_policy_version=7,
                foreground_evidence_sha256=False,
                host_time_observed=1,
                recent_exhaustive_evidence_count=True,
            )

        fields = captured.records[0].dreamland_enrichment_fields
        self.assertFalse(
            any(
                name.startswith("foreground_evidence_")
                or name in {"host_time_observed", "recent_exhaustive_evidence_count"}
                for name in fields
            )
        )

    def test_integrator_refresh_fields_reject_wrong_types(self):
        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            log_enrichment_event(
                "integrator.refresh.completed",
                context=EnrichmentLogContext(session_id="session-1"),
                integrator_refresh_outcome=False,
                integrator_refresh_attempt=True,
                integrator_refresh_latency_ms=False,
                eligible=1,
                changed=0,
            )

        fields = captured.records[0].dreamland_enrichment_fields
        for name in (
            "integrator_refresh_outcome",
            "integrator_refresh_attempt",
            "integrator_refresh_latency_ms",
            "eligible",
            "changed",
        ):
            self.assertNotIn(name, fields)

    def test_dedicated_buffer_survives_global_log_ring_churn(self):
        settings = LoggingSettings(
            level="INFO",
            events=frozenset({"enrichment", "lifecycle"}),
        )
        global_logs = configure_console_logging(
            settings,
            stream=False,
            log_capacity=1,
        )
        enrichment_events = configure_enrichment_event_logging(
            settings,
            log_capacity=3,
        )
        log_enrichment_event(
            "job.completed",
            context=EnrichmentLogContext(session_id="session-1", job_id="job-1"),
            status="applied",
        )
        logging.getLogger("dreamland.lifecycle").info("unrelated one")
        logging.getLogger("dreamland.lifecycle").info("unrelated two")

        self.assertNotIn("job.completed", global_logs.latest(1)[0].text)
        records = enrichment_events.latest("session-1", 10, job_id="job-1")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].event, "job.completed")
        self.assertEqual(records[0].level, "INFO")


if __name__ == "__main__":
    unittest.main()
