import logging
import unittest
from types import SimpleNamespace

from dreamland.agent_tracing import TokenUsageLoggingCallback
from dreamland.config import LoggingSettings
from dreamland.runtime_logging import (
    EnrichmentEventLogBuffer,
    EventTypeFilter,
    LogRecordBuffer,
    SessionTraceLogBuffer,
    configure_console_logging,
    configure_mcp_logging,
    configure_session_trace_logging,
    reply_excerpt,
    trace_input,
)


class RuntimeLoggingTests(unittest.TestCase):
    def test_event_filter_selects_configured_log_types(self):
        event_filter = EventTypeFilter(frozenset({"commands", "tokens", "failures"}))

        self.assertTrue(event_filter.filter(_record("dreamland.commands")))
        self.assertTrue(event_filter.filter(_record("dreamland.tokens")))
        self.assertTrue(event_filter.filter(_record("dreamland.failures")))
        self.assertFalse(event_filter.filter(_record("dreamland.replies")))
        self.assertTrue(event_filter.filter(_record("dreamland.ssh_server")))

        replies_filter = EventTypeFilter(frozenset({"replies"}))
        self.assertFalse(replies_filter.filter(_record("dreamland.failures")))

        mcp_filter = EventTypeFilter(frozenset({"mcp"}))
        self.assertTrue(mcp_filter.filter(_record("dreamland.mcp")))
        self.assertFalse(mcp_filter.filter(_record("dreamland.lifecycle")))

        enrichment_filter = EventTypeFilter(frozenset({"enrichment"}))
        self.assertTrue(
            enrichment_filter.filter(_record("dreamland.enrichment.trigger"))
        )
        self.assertFalse(enrichment_filter.filter(_record("dreamland.commands")))

    def test_mcp_logs_route_to_runtime_buffer(self):
        root_logger = logging.getLogger()
        root_handlers = list(root_logger.handlers)
        root_level = root_logger.level
        try:
            logs = configure_console_logging(
                LoggingSettings(level="INFO", events=frozenset({"mcp"})),
                stream=False,
                log_capacity=10,
            )
            configure_mcp_logging()

            logging.getLogger("mcp.server.lowlevel.server").info(
                "Processing request of type CallToolRequest"
            )

            records = [record.text for record in logs.latest(10)]
            self.assertEqual(len(records), 1)
            self.assertIn(
                "INFO dreamland.mcp: Processing request of type CallToolRequest",
                records[0],
            )
        finally:
            _restore_root_logger(root_logger, root_handlers, root_level)

    def test_mcp_logs_respect_runtime_event_filter(self):
        root_logger = logging.getLogger()
        root_handlers = list(root_logger.handlers)
        root_level = root_logger.level
        try:
            logs = configure_console_logging(
                LoggingSettings(level="INFO", events=frozenset({"lifecycle"})),
                stream=False,
                log_capacity=10,
            )
            configure_mcp_logging()

            logging.getLogger("mcp.server.lowlevel.server").info(
                "Processing request of type CallToolRequest"
            )

            self.assertEqual(logs.latest(10), ())
        finally:
            _restore_root_logger(root_logger, root_handlers, root_level)

    def test_reply_excerpt_uses_only_first_line(self):
        self.assertEqual(reply_excerpt("first line\nsecond line\n", ""), "first line")

    def test_log_record_buffer_retains_bounded_tail(self):
        buffer = LogRecordBuffer(2)
        buffer.setFormatter(logging.Formatter("%(message)s"))
        buffer.handle(_record("dreamland.lifecycle", "first"))
        buffer.handle(_record("dreamland.lifecycle", "second"))
        buffer.handle(_record("dreamland.lifecycle", "third"))

        self.assertEqual(
            [record.text for record in buffer.latest(20)],
            ["second", "third"],
        )

    def test_session_trace_buffer_segregates_sessions(self):
        buffer = SessionTraceLogBuffer(2)
        buffer.setFormatter(logging.Formatter("%(message)s"))
        buffer.handle(_trace_record("session-a", "first-a"))
        buffer.handle(_trace_record("session-b", "first-b"))
        buffer.handle(_trace_record("session-a", "second-a"))
        buffer.handle(_trace_record("session-a", "third-a"))

        self.assertEqual(
            [record.text for record in buffer.latest("session-a", 20)],
            ["second-a", "third-a"],
        )
        self.assertEqual(
            [record.text for record in buffer.latest("session-b", 20)],
            ["first-b"],
        )

    def test_enrichment_event_buffer_retains_per_session_tail_and_exact_job(self):
        buffer = EnrichmentEventLogBuffer(2)
        buffer.setFormatter(logging.Formatter("%(message)s"))
        buffer.handle(_enrichment_record("session-a", "job-1", "job.queued"))
        buffer.handle(_enrichment_record("session-b", "job-9", "job.queued"))
        buffer.handle(_enrichment_record("session-a", "job-1", "job.started"))
        buffer.handle(_enrichment_record("session-a", "job-2", "job.completed"))

        retained = buffer.latest("session-a", 20)
        self.assertEqual(
            [(record.job_id, record.event) for record in retained],
            [("job-1", "job.started"), ("job-2", "job.completed")],
        )
        self.assertEqual(
            [record.event for record in buffer.latest("session-a", 20, job_id="job-1")],
            ["job.started"],
        )
        self.assertEqual(
            [record.event for record in buffer.after("session-a", 2, timeout=0)],
            ["job.started", "job.completed"],
        )
        self.assertEqual(
            [record.event for record in buffer.latest("session-b", 20)],
            ["job.queued"],
        )
        retention = buffer.retention("session-a")
        self.assertEqual(retention.capacity, 2)
        self.assertEqual(retention.retained, 2)
        self.assertEqual(retention.evicted, 1)
        self.assertEqual(retention.oldest_sequence, 3)
        self.assertEqual(retention.latest_sequence, 4)

    def test_enrichment_event_buffer_retains_bounded_structured_fields(self):
        buffer = EnrichmentEventLogBuffer(2)
        buffer.setFormatter(logging.Formatter("%(message)s"))
        registry_targets = [["services_by_name"], ["filesystem", "artifacts_by_path"]]
        record = _enrichment_record("session-a", "job-1", "patch.proposed")
        record.dreamland_enrichment_fields = {
            "event_id": "event-1",
            "correlation_id": "correlation-1",
            "trigger_id": "manual",
            "enrichment_mode": "ambient_presence",
            "coordinator_role": "ambient_coordinator",
            "target_host": "cache",
            "requested_surface": "service",
            "selected_surface": "filesystem",
            "surface": "filesystem",
            "status": "patch",
            "reason": "eligible",
            "reason_code": "patch_proposed",
            "candidate_id": "candidate-1",
            "coalesced_into_candidate_id": "candidate-0",
            "replaced_candidate_id": "candidate-old",
            "retained_candidate_id": "candidate-1",
            "completed_job_id": "job-0",
            "higher_priority_trigger": "active_host_changed",
            "retained_trigger": "host_discovered",
            "retained_enrichment_mode": "focused",
            "presence_probe_kind": "session_presence",
            "retained_presence_probe_kind": "network_presence",
            "subject_key_sha256": "subject-digest",
            "retained_subject_key_sha256": "retained-subject-digest",
            "cooldown_scope_sha256": "cooldown-digest",
            "focus_key_sha256": "0123456789abcdef",
            "match_policy": "interaction-focus-match-v1",
            "match_classification": "path_related",
            "command_focus_key_sha256": "command-focus-digest",
            "retained_focus_key_sha256": "retained-focus-digest",
            "failure_kind": "provider_quota",
            "failure_disposition": "circuit_opened",
            "provider": "anthropic",
            "provider_code": "credit_balance_exhausted",
            "role": "expert",
            "model": "claude-sonnet-4-5",
            "circuit_origin": "expert",
            "authority_domain": "shell_session",
            "binding_reason": "latest_foreground",
            "policy_version": "foreground-authority-v1",
            "action": "reject_cluster",
            "integrator_refresh_outcome": "applied",
            "integrator_refresh_attempt": 1,
            "integrator_refresh_latency_ms": 8123,
            "priority": 4,
            "retained_priority": 4,
            "pulse_count": 4,
            "retained_pulse_count": 3,
            "inflight": 3,
            "mode_inflight": 2,
            "inflight_before": 2,
            "inflight_after": 3,
            "mode_inflight_before": 1,
            "mode_inflight_after": 2,
            "max_inflight": 3,
            "age_ms": 25,
            "max_age_ms": 60000,
            "focus_path_count": 2,
            "focus_artifact_count": 1,
            "command_focus_path_count": 1,
            "command_focus_artifact_count": 2,
            "retained_focus_path_count": 3,
            "retained_focus_artifact_count": 4,
            "provider_status_code": 402,
            "authority_dependency_count": 2,
            "provider_circuit_count": 1,
            "accepted": True,
            "eligible": True,
            "changed": True,
            "foreground_authority_present": True,
            "foreground_authority_changed": False,
            "relation_policy_version": "expert-relations-v2",
            "relation_limit": 24,
            "relation_received_count": 27,
            "relation_accepted_count": 24,
            "relation_invalid_count": 1,
            "relation_duplicate_count": 1,
            "relation_overflow_count": 1,
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
            "registry_targets": registry_targets,
        }

        buffer.handle(record)
        registry_targets[0].append("mutated-after-handle")

        retained = buffer.latest("session-a", 1)[0]
        self.assertEqual(retained.event_id, "event-1")
        self.assertEqual(retained.correlation_id, "correlation-1")
        self.assertEqual(retained.trigger_id, "manual")
        self.assertEqual(retained.enrichment_mode, "ambient_presence")
        self.assertEqual(retained.coordinator_role, "ambient_coordinator")
        self.assertEqual(retained.target_host, "cache")
        self.assertEqual(retained.requested_surface, "service")
        self.assertEqual(retained.selected_surface, "filesystem")
        self.assertEqual(retained.surface, "filesystem")
        self.assertEqual(retained.status, "patch")
        self.assertEqual(retained.reason, "eligible")
        self.assertEqual(retained.reason_code, "patch_proposed")
        self.assertEqual(retained.candidate_id, "candidate-1")
        self.assertEqual(retained.coalesced_into_candidate_id, "candidate-0")
        self.assertEqual(retained.replaced_candidate_id, "candidate-old")
        self.assertEqual(retained.retained_candidate_id, "candidate-1")
        self.assertEqual(retained.completed_job_id, "job-0")
        self.assertEqual(retained.higher_priority_trigger, "active_host_changed")
        self.assertEqual(retained.retained_trigger, "host_discovered")
        self.assertEqual(retained.retained_enrichment_mode, "focused")
        self.assertEqual(retained.presence_probe_kind, "session_presence")
        self.assertEqual(retained.retained_presence_probe_kind, "network_presence")
        self.assertEqual(retained.subject_key_sha256, "subject-digest")
        self.assertEqual(
            retained.retained_subject_key_sha256,
            "retained-subject-digest",
        )
        self.assertEqual(retained.cooldown_scope_sha256, "cooldown-digest")
        self.assertEqual(retained.focus_key_sha256, "0123456789abcdef")
        self.assertEqual(retained.match_policy, "interaction-focus-match-v1")
        self.assertEqual(retained.match_classification, "path_related")
        self.assertEqual(retained.command_focus_key_sha256, "command-focus-digest")
        self.assertEqual(retained.retained_focus_key_sha256, "retained-focus-digest")
        self.assertEqual(retained.failure_kind, "provider_quota")
        self.assertEqual(retained.failure_disposition, "circuit_opened")
        self.assertEqual(retained.provider, "anthropic")
        self.assertEqual(retained.provider_code, "credit_balance_exhausted")
        self.assertEqual(retained.role, "expert")
        self.assertEqual(retained.model, "claude-sonnet-4-5")
        self.assertEqual(retained.circuit_origin, "expert")
        self.assertEqual(retained.authority_domain, "shell_session")
        self.assertEqual(retained.binding_reason, "latest_foreground")
        self.assertEqual(retained.policy_version, "foreground-authority-v1")
        self.assertEqual(retained.action, "reject_cluster")
        self.assertEqual(retained.integrator_refresh_outcome, "applied")
        self.assertEqual(retained.integrator_refresh_attempt, 1)
        self.assertEqual(retained.integrator_refresh_latency_ms, 8123)
        self.assertEqual(retained.priority, 4)
        self.assertEqual(retained.retained_priority, 4)
        self.assertEqual(retained.pulse_count, 4)
        self.assertEqual(retained.retained_pulse_count, 3)
        self.assertEqual(retained.inflight, 3)
        self.assertEqual(retained.mode_inflight, 2)
        self.assertEqual(retained.inflight_before, 2)
        self.assertEqual(retained.inflight_after, 3)
        self.assertEqual(retained.mode_inflight_before, 1)
        self.assertEqual(retained.mode_inflight_after, 2)
        self.assertEqual(retained.max_inflight, 3)
        self.assertEqual(retained.age_ms, 25)
        self.assertEqual(retained.max_age_ms, 60000)
        self.assertEqual(retained.focus_path_count, 2)
        self.assertEqual(retained.focus_artifact_count, 1)
        self.assertEqual(retained.command_focus_path_count, 1)
        self.assertEqual(retained.command_focus_artifact_count, 2)
        self.assertEqual(retained.retained_focus_path_count, 3)
        self.assertEqual(retained.retained_focus_artifact_count, 4)
        self.assertEqual(retained.provider_status_code, 402)
        self.assertEqual(retained.authority_dependency_count, 2)
        self.assertEqual(retained.provider_circuit_count, 1)
        self.assertTrue(retained.accepted)
        self.assertTrue(retained.eligible)
        self.assertTrue(retained.changed)
        self.assertTrue(retained.foreground_authority_present)
        self.assertFalse(retained.foreground_authority_changed)
        self.assertEqual(retained.relation_policy_version, "expert-relations-v2")
        self.assertEqual(retained.relation_limit, 24)
        self.assertEqual(retained.relation_received_count, 27)
        self.assertEqual(retained.relation_accepted_count, 24)
        self.assertEqual(retained.relation_invalid_count, 1)
        self.assertEqual(retained.relation_duplicate_count, 1)
        self.assertEqual(retained.relation_overflow_count, 1)
        self.assertEqual(retained.relation_discarded_count, 3)
        self.assertEqual(retained.presence_gap_policy_version, "presence-gap-v2")
        self.assertEqual(retained.presence_gap_sha256, "presence-gap-digest")
        self.assertEqual(retained.presence_gap_ambient_objective, "independent_life")
        self.assertEqual(retained.presence_gap_preferred_surface, "network")
        self.assertEqual(retained.presence_gap_anti_repetition_subject_count, 0)
        self.assertEqual(retained.presence_gap_anti_repetition_system_count, 1)
        self.assertEqual(
            retained.foreground_evidence_policy_version,
            "foreground-evidence-v1",
        )
        self.assertEqual(
            retained.foreground_evidence_sha256,
            "foreground-evidence-digest",
        )
        self.assertTrue(retained.host_time_observed)
        self.assertEqual(retained.recent_exhaustive_evidence_count, 2)
        self.assertEqual(
            retained.registry_targets,
            (
                ("services_by_name",),
                ("filesystem", "artifacts_by_path"),
            ),
        )

    def test_relation_event_fields_are_optional_and_type_bounded(self):
        buffer = EnrichmentEventLogBuffer(2)
        buffer.setFormatter(logging.Formatter("%(message)s"))
        buffer.handle(_enrichment_record("session-a", "job-1", "job.completed"))
        invalid = _enrichment_record(
            "session-a",
            "job-2",
            "expert.relations_evaluated",
        )
        invalid.dreamland_enrichment_fields = {
            "relation_policy_version": "",
            "relation_limit": True,
            "relation_received_count": -1,
            "relation_accepted_count": "24",
            "relation_invalid_count": None,
            "relation_duplicate_count": False,
            "relation_overflow_count": -1,
            "relation_discarded_count": "3",
            "presence_gap_policy_version": "",
            "presence_gap_sha256": None,
            "presence_gap_ambient_objective": 7,
            "presence_gap_preferred_surface": False,
            "presence_gap_anti_repetition_subject_count": True,
            "presence_gap_anti_repetition_system_count": -1,
            "presence_gap_anti_repetition_subjects": ["user:alice"],
            "foreground_evidence_policy_version": 7,
            "foreground_evidence_sha256": False,
            "host_time_observed": 1,
            "recent_exhaustive_evidence_count": True,
            "recent_exhaustive_evidence_kinds": ["home_listing"],
            "pulse_count": True,
            "retained_pulse_count": False,
            "inflight": True,
            "mode_inflight": False,
            "inflight_before": True,
            "inflight_after": False,
            "mode_inflight_before": True,
            "mode_inflight_after": False,
            "max_inflight": True,
        }
        buffer.handle(invalid)

        for retained in buffer.latest("session-a", 2):
            self.assertIsNone(retained.relation_policy_version)
            self.assertIsNone(retained.relation_limit)
            self.assertIsNone(retained.relation_received_count)
            self.assertIsNone(retained.relation_accepted_count)
            self.assertIsNone(retained.relation_invalid_count)
            self.assertIsNone(retained.relation_duplicate_count)
            self.assertIsNone(retained.relation_overflow_count)
            self.assertIsNone(retained.relation_discarded_count)
            self.assertIsNone(retained.presence_gap_policy_version)
            self.assertIsNone(retained.presence_gap_sha256)
            self.assertIsNone(retained.presence_gap_ambient_objective)
            self.assertIsNone(retained.presence_gap_preferred_surface)
            self.assertIsNone(retained.presence_gap_anti_repetition_subject_count)
            self.assertIsNone(retained.presence_gap_anti_repetition_system_count)
            self.assertIsNone(retained.foreground_evidence_policy_version)
            self.assertIsNone(retained.foreground_evidence_sha256)
            self.assertIsNone(retained.host_time_observed)
            self.assertIsNone(retained.recent_exhaustive_evidence_count)
            self.assertIsNone(retained.pulse_count)
            self.assertIsNone(retained.retained_pulse_count)
            self.assertIsNone(retained.inflight)
            self.assertIsNone(retained.mode_inflight)
            self.assertIsNone(retained.inflight_before)
            self.assertIsNone(retained.inflight_after)
            self.assertIsNone(retained.mode_inflight_before)
            self.assertIsNone(retained.mode_inflight_after)
            self.assertIsNone(retained.max_inflight)

    def test_trace_helper_records_to_configured_session_trace_buffer(self):
        buffer = configure_session_trace_logging(3)
        buffer.setFormatter(logging.Formatter("%(message)s"))

        trace_input("session-a", "whoami")

        records = buffer.latest("session-a", 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].text, "type=input text='whoami'")

    def test_token_callback_logs_provider_usage(self):
        callback = TokenUsageLoggingCallback(
            "gpt-test",
            command="find /",
            max_output_tokens=200,
        )
        response = SimpleNamespace(
            llm_output={
                "token_usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                }
            },
            generations=[],
        )

        with self.assertLogs("dreamland.tokens", level="INFO") as captured:
            callback.on_llm_end(response)

        self.assertIn("input_tokens=120", captured.output[0])
        self.assertIn("output_tokens=30", captured.output[0])
        self.assertIn("total_tokens=150", captured.output[0])
        self.assertIn("interrupted=false", captured.output[0])
        self.assertIn("service_tier=unknown", captured.output[0])

    def test_token_callback_logs_only_provider_reported_service_tier(self):
        for location in ("llm_output", "generation_info", "response_metadata"):
            with self.subTest(location=location):
                message = SimpleNamespace(
                    usage_metadata={
                        "input_tokens": 5,
                        "output_tokens": 2,
                        "total_tokens": 7,
                    },
                    response_metadata={"service_tier": "default"}
                    if location == "response_metadata"
                    else {},
                )
                response = SimpleNamespace(
                    llm_output={"service_tier": "default"}
                    if location == "llm_output"
                    else None,
                    generations=[
                        [
                            SimpleNamespace(
                                message=message,
                                generation_info={"service_tier": "default"}
                                if location == "generation_info"
                                else None,
                            )
                        ]
                    ],
                )
                with self.assertLogs("dreamland.tokens", level="INFO") as captured:
                    TokenUsageLoggingCallback(
                        "gpt-6-astra",
                        command="whoami",
                        max_output_tokens=100,
                    ).on_llm_end(response)
                self.assertIn("service_tier=default", captured.output[0])

    def test_token_callback_supports_message_usage_metadata(self):
        message = SimpleNamespace(
            usage_metadata={
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
            },
            response_metadata={},
        )
        response = SimpleNamespace(
            llm_output=None,
            generations=[[SimpleNamespace(message=message)]],
        )

        with self.assertLogs("dreamland.tokens", level="INFO") as captured:
            TokenUsageLoggingCallback(
                "gpt-test",
                command="find /",
                max_output_tokens=200,
            ).on_llm_end(response)

        self.assertIn("input_tokens=50", captured.output[0])
        self.assertIn("output_tokens=10", captured.output[0])

    def test_token_callback_logs_provider_cache_usage(self):
        message = SimpleNamespace(
            usage_metadata={
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
                "input_token_details": {
                    "cache_creation": 40,
                    "cache_read": 20,
                },
            },
            response_metadata={},
        )
        response = SimpleNamespace(
            llm_output=None,
            generations=[[SimpleNamespace(message=message)]],
        )

        with self.assertLogs("dreamland.tokens", level="INFO") as captured:
            TokenUsageLoggingCallback(
                "claude-test",
                command="find /",
                max_output_tokens=200,
            ).on_llm_end(response)

        self.assertIn("input_cache_creation=40", captured.output[0])
        self.assertIn("input_cache_read=20", captured.output[0])

    def test_token_callback_logs_raw_openai_responses_cache_usage(self):
        response = SimpleNamespace(
            llm_output={
                "token_usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "total_tokens": 110,
                    "input_tokens_details": {
                        "cache_write_tokens": 80,
                        "cached_tokens": 20,
                    },
                }
            },
            generations=[],
        )

        with self.assertLogs("dreamland.tokens", level="INFO") as captured:
            TokenUsageLoggingCallback(
                "gpt-5.6",
                command="find /",
                max_output_tokens=200,
            ).on_llm_end(response)

        self.assertIn("input_cache_creation=80", captured.output[0])
        self.assertIn("input_cache_read=20", captured.output[0])

    def test_token_callback_logs_raw_openai_chat_cache_usage(self):
        response = SimpleNamespace(
            llm_output={
                "token_usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 10,
                    "total_tokens": 130,
                    "prompt_tokens_details": {
                        "cache_write_tokens": 90,
                        "cached_tokens": 30,
                    },
                }
            },
            generations=[],
        )

        with self.assertLogs("dreamland.tokens", level="INFO") as captured:
            TokenUsageLoggingCallback(
                "gpt-5.6",
                command="find /",
                max_output_tokens=200,
            ).on_llm_end(response)

        self.assertIn("input_cache_creation=90", captured.output[0])
        self.assertIn("input_cache_read=30", captured.output[0])

    def test_token_callback_logs_tier_prefixed_openai_cache_usage(self):
        for tier in ("priority", "flex"):
            with self.subTest(tier=tier):
                message = SimpleNamespace(
                    usage_metadata={
                        "input_tokens": 50,
                        "output_tokens": 10,
                        "total_tokens": 60,
                        "input_token_details": {
                            f"{tier}_cache_creation": 40,
                            f"{tier}_cache_read": 20,
                        },
                    },
                    response_metadata={},
                )
                response = SimpleNamespace(
                    llm_output=None,
                    generations=[[SimpleNamespace(message=message)]],
                )

                with self.assertLogs("dreamland.tokens", level="INFO") as captured:
                    TokenUsageLoggingCallback(
                        "gpt-5.6",
                        command="find /",
                        max_output_tokens=200,
                    ).on_llm_end(response)

                self.assertIn("input_cache_creation=40", captured.output[0])
                self.assertIn("input_cache_read=20", captured.output[0])

    def test_token_callback_preserves_explicit_zero_cache_usage(self):
        response = SimpleNamespace(
            llm_output={
                "token_usage": {
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "total_tokens": 60,
                    "input_cache_creation": 0,
                    "input_cache_read": 0,
                    "input_tokens_details": {
                        "cache_write_tokens": 40,
                        "cached_tokens": 20,
                    },
                }
            },
            generations=[],
        )

        with self.assertLogs("dreamland.tokens", level="INFO") as captured:
            TokenUsageLoggingCallback(
                "gpt-5.6",
                command="find /",
                max_output_tokens=200,
            ).on_llm_end(response)

        self.assertIn("input_cache_creation=0", captured.output[0])
        self.assertIn("input_cache_read=0", captured.output[0])

    def test_token_callback_logs_when_output_cap_is_hit(self):
        callback = TokenUsageLoggingCallback(
            "gpt-test",
            command="find /",
            max_output_tokens=100,
        )
        message = SimpleNamespace(
            usage_metadata={
                "input_tokens": 40,
                "output_tokens": 99,
                "total_tokens": 139,
            },
            response_metadata={"finish_reason": "length"},
        )
        response = SimpleNamespace(
            llm_output=None,
            generations=[
                [
                    SimpleNamespace(
                        message=message,
                        generation_info={"finish_reason": "length"},
                    )
                ]
            ],
        )

        with self.assertLogs("dreamland.tokens", level="INFO") as captured:
            callback.on_llm_end(response)

        self.assertEqual(len(captured.output), 2)
        self.assertIn("output_token_cap_hit=true", captured.output[1])
        self.assertIn("max_output_tokens=100", captured.output[1])
        self.assertIn("finish_reason=length", captured.output[1])

    def test_cap_hit_logs_without_usage_metadata(self):
        callback = TokenUsageLoggingCallback(
            "gpt-test",
            command="find /",
            max_output_tokens=100,
        )
        message = SimpleNamespace(
            usage_metadata=None,
            response_metadata={"finish_reason": "length"},
        )
        response = SimpleNamespace(
            llm_output=None,
            generations=[
                [
                    SimpleNamespace(
                        message=message,
                        generation_info={"finish_reason": "length"},
                    )
                ]
            ],
        )

        with self.assertLogs("dreamland.tokens", level="WARNING") as captured:
            callback.on_llm_end(response)

        self.assertEqual(len(captured.output), 2)
        self.assertIn("token_usage=unavailable", captured.output[0])
        self.assertIn("output_token_cap_hit=true", captured.output[1])
        self.assertIn("output_tokens=unknown", captured.output[1])

    def test_interrupted_callback_logs_usage_if_provider_returns_it(self):
        callback = TokenUsageLoggingCallback(
            "gpt-test",
            command="find /",
            max_output_tokens=200,
        )
        response = SimpleNamespace(
            llm_output={
                "token_usage": {
                    "prompt_tokens": 80,
                    "completion_tokens": 20,
                    "total_tokens": 100,
                }
            },
            generations=[],
        )

        with self.assertLogs("dreamland.tokens", level="INFO") as captured:
            callback.mark_dreamland_interrupted()
            callback.on_llm_end(response)

        self.assertIn("token_usage=unavailable", captured.output[0])
        self.assertIn("interrupted=true", captured.output[1])
        self.assertIn("input_tokens=80", captured.output[1])
        self.assertIn("output_tokens=20", captured.output[1])


def _record(name: str, message: str = "message") -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, message, (), None)


def _restore_root_logger(
    root_logger: logging.Logger,
    handlers: list[logging.Handler],
    level: int,
) -> None:
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
    for handler in handlers:
        root_logger.addHandler(handler)
    root_logger.setLevel(level)


def _trace_record(session_id: str, message: str) -> logging.LogRecord:
    record = _record("dreamland.trace", message)
    record.dreamland_session_id = session_id
    return record


def _enrichment_record(
    session_id: str,
    job_id: str | None,
    event: str,
) -> logging.LogRecord:
    record = _record("dreamland.enrichment", f"event={event}")
    record.dreamland_session_id = session_id
    record.dreamland_enrichment_job_id = job_id
    record.dreamland_enrichment_event = event
    return record


if __name__ == "__main__":
    unittest.main()
