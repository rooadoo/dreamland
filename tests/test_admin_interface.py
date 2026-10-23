import logging
import tempfile
import unittest
from pathlib import Path

from dreamland.admin_interface import (
    AdminServiceError,
    DreamlandAdminService,
)
from dreamland.agent_structured import command_result_from_model_response
from dreamland.backend import AgentBackend
from dreamland.console_introspection import (
    CommandInspection,
    CommandSummary,
    ObservationSummary,
    SessionList,
    SessionSummary,
    StateDelta,
)
from dreamland.models import (
    CommandHistoryEntry,
    CommandStateUpdate,
    EnvironmentMetadata,
    InteractiveProgram,
    SessionState,
    ShellStackTransition,
)
from dreamland.profile import server_profile
from dreamland.runtime_logging import (
    EnrichmentEventLogBuffer,
    LogRecordBuffer,
    SessionTraceLogBuffer,
    configure_session_trace_logging,
)
from dreamland.session_registry import LiveSessionRegistry
from dreamland.session_state import apply_external_state_update
from dreamland.spiritguide import SpiritguideResult
from dreamland.state_profiles import (
    STATE_PROFILE_METADATA_LEVEL,
    STATE_PROFILE_METADATA_NAME,
    StateProfileRuntime,
    StateProfileStore,
    create_state_profile,
)
from dreamland.structured_failures import (
    StructuredFailureStore,
    create_structured_failure_capsule,
)


def _profile_world(*, services=None):
    return {
        "entry_host": "ubuntu",
        "active_host": "ubuntu",
        "hosts_by_name": {
            "ubuntu": {
                "hostname": "ubuntu",
                "login_banner": "Authored administration fixture.\n",
                "services_by_name": {} if services is None else services,
            }
        },
    }


class DreamlandAdminServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.logs = LogRecordBuffer(5)
        self.logs.setFormatter(logging.Formatter("%(message)s"))
        self.trace_logs = SessionTraceLogBuffer(5)
        self.trace_logs.setFormatter(logging.Formatter("%(message)s"))
        self.enrichment_event_logs = EnrichmentEventLogBuffer(5)
        self.enrichment_event_logs.setFormatter(logging.Formatter("%(message)s"))
        self.registry = LiveSessionRegistry()
        self.state_profile_runtime = StateProfileRuntime(
            store=StateProfileStore(Path(self.tmpdir.name))
        )
        self.structured_failure_store = StructuredFailureStore(
            Path(self.tmpdir.name) / "structured_failures"
        )
        self.introspection = FakeIntrospection()
        self.shutdown_count = 0
        self.spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Done.",
                state_update=CommandStateUpdate(cwd="/srv/app"),
                metadata={"structured_response_accepted": True},
            )
        )
        self.service = DreamlandAdminService(
            logs=self.logs,
            trace_logs=self.trace_logs,
            enrichment_event_logs=self.enrichment_event_logs,
            session_registry=self.registry,
            state_profile_runtime=self.state_profile_runtime,
            introspection=self.introspection,
            spiritguide=self.spiritguide,
            request_shutdown=self._request_shutdown,
            shutdown_requested=lambda: False,
            spiritguide_available=True,
            shutdown_available=True,
            effective_config={
                "openai": {"api_key_configured": True},
                "api": {"token_configured": True},
            },
            structured_failure_store=self.structured_failure_store,
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_status_and_capabilities_report_runtime_features(self):
        self.registry.register(SessionState(username="alice", langfuse_trace_id="s-a"))

        status = self.service.status()
        capabilities = self.service.capabilities()

        self.assertEqual(status.live_session_count, 1)
        self.assertTrue(status.introspection_available)
        self.assertFalse(status.shutdown_requested)
        self.assertTrue(capabilities.historical_introspection)
        self.assertTrue(capabilities.structured_failures)
        self.assertTrue(capabilities.spiritguide)
        self.assertTrue(capabilities.shutdown)
        self.assertFalse(capabilities.mcp_write_tools)

    def test_logs_and_traces_return_structured_entries_after_cursor(self):
        self.logs.handle(_record("first"))
        self.logs.handle(_record("second"))
        self.logs.handle(_record("third"))
        self.trace_logs.handle(_trace_record("s-a", "input"))
        self.trace_logs.handle(_trace_record("s-b", "other"))
        self.trace_logs.handle(_trace_record("s-a", "output"))

        logs = self.service.recent_logs(limit=10, after_sequence=1)
        trace = self.service.session_trace(
            session_id="s-a",
            limit=10,
            after_sequence=1,
        )

        self.assertEqual([entry.text for entry in logs], ["second", "third"])
        self.assertEqual([entry.text for entry in trace], ["output"])
        self.assertEqual(trace[0].session_id, "s-a")

    def test_enrichment_events_filter_exact_job_and_do_not_require_live_session(self):
        self.enrichment_event_logs.handle(
            _enrichment_record(
                "s-closed",
                "job-1",
                "trigger.accepted",
                fields={
                    "event_id": "event-1",
                    "correlation_id": "correlation-1",
                    "trigger_id": "manual",
                    "enrichment_mode": "ambient_presence",
                    "coordinator_role": "ambient_coordinator",
                    "target_host": "cache",
                    "requested_surface": "filesystem",
                    "surface": "filesystem",
                    "reason": "eligible",
                    "candidate_id": "candidate-1",
                    "replaced_candidate_id": "candidate-0",
                    "completed_job_id": "job-0",
                    "retained_enrichment_mode": "focused",
                    "presence_probe_kind": "session_presence",
                    "retained_presence_probe_kind": "network_presence",
                    "subject_key_sha256": "subject-digest",
                    "retained_subject_key_sha256": "retained-subject-digest",
                    "cooldown_scope_sha256": "cooldown-digest",
                    "match_policy": "interaction-focus-match-v1",
                    "match_classification": "surface_only",
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
                    "action": "reject_cluster",
                    "integrator_refresh_outcome": "applied",
                    "integrator_refresh_attempt": 1,
                    "integrator_refresh_latency_ms": 8123,
                    "authority_dependency_count": 2,
                    "provider_circuit_count": 1,
                    "priority": 4,
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
                    "registry_targets": (("filesystem", "artifacts_by_path"),),
                },
            )
        )
        self.enrichment_event_logs.handle(
            _enrichment_record("s-closed", "job-2", "job.queued")
        )
        self.enrichment_event_logs.handle(
            _enrichment_record("s-other", "job-1", "job.started")
        )
        self.enrichment_event_logs.handle(
            _enrichment_record("s-closed", "job-1", "job.completed")
        )
        for index in range(10):
            self.logs.handle(_record(f"unrelated-global-log-{index}"))

        result = self.service.session_enrichment_events(
            session_id="s-closed",
            job_id="job-1",
            limit=10,
            after_sequence=1,
        )

        self.assertEqual(result.session_id, "s-closed")
        self.assertEqual(result.job_id, "job-1")
        self.assertEqual(result.returned, 1)
        self.assertEqual(result.events[0].event, "job.completed")
        self.assertEqual(result.events[0].level, "INFO")
        self.assertEqual(result.retention.capacity, 5)
        self.assertEqual(result.retention.retained, 3)
        self.assertEqual(result.retention.evicted, 0)
        self.assertFalse(result.retention.restart_durable)
        self.assertEqual(
            self.service.session_enrichment_events(
                session_id="s-closed",
                job_id="job",
                limit=10,
            ).events,
            [],
        )

        accepted = self.service.session_enrichment_events(
            session_id="s-closed",
            job_id="job-1",
            limit=10,
        ).events[0]
        self.assertEqual(accepted.event_id, "event-1")
        self.assertEqual(accepted.correlation_id, "correlation-1")
        self.assertEqual(accepted.trigger_id, "manual")
        self.assertEqual(accepted.enrichment_mode, "ambient_presence")
        self.assertEqual(accepted.coordinator_role, "ambient_coordinator")
        self.assertEqual(accepted.target_host, "cache")
        self.assertEqual(accepted.requested_surface, "filesystem")
        self.assertEqual(accepted.surface, "filesystem")
        self.assertEqual(accepted.reason, "eligible")
        self.assertEqual(accepted.candidate_id, "candidate-1")
        self.assertEqual(accepted.replaced_candidate_id, "candidate-0")
        self.assertEqual(accepted.completed_job_id, "job-0")
        self.assertEqual(accepted.retained_enrichment_mode, "focused")
        self.assertEqual(accepted.presence_probe_kind, "session_presence")
        self.assertEqual(accepted.retained_presence_probe_kind, "network_presence")
        self.assertEqual(accepted.subject_key_sha256, "subject-digest")
        self.assertEqual(
            accepted.retained_subject_key_sha256,
            "retained-subject-digest",
        )
        self.assertEqual(accepted.cooldown_scope_sha256, "cooldown-digest")
        self.assertEqual(accepted.match_policy, "interaction-focus-match-v1")
        self.assertEqual(accepted.match_classification, "surface_only")
        self.assertEqual(accepted.command_focus_key_sha256, "command-focus-digest")
        self.assertEqual(accepted.command_focus_path_count, 1)
        self.assertEqual(accepted.command_focus_artifact_count, 2)
        self.assertEqual(accepted.retained_focus_key_sha256, "retained-focus-digest")
        self.assertEqual(accepted.retained_focus_path_count, 3)
        self.assertEqual(accepted.retained_focus_artifact_count, 4)
        self.assertEqual(accepted.failure_kind, "provider_quota")
        self.assertEqual(accepted.failure_disposition, "circuit_opened")
        self.assertEqual(accepted.provider, "openai")
        self.assertEqual(accepted.provider_code, "credit_balance_exhausted")
        self.assertEqual(accepted.provider_status_code, 402)
        self.assertEqual(accepted.role, "coordinator")
        self.assertEqual(accepted.model, "gpt-5.6-terra")
        self.assertEqual(accepted.circuit_origin, "coordinator")
        self.assertEqual(accepted.authority_domain, "shell_session")
        self.assertEqual(accepted.binding_reason, "latest_foreground")
        self.assertEqual(accepted.policy_version, "foreground-authority-v1")
        self.assertEqual(accepted.action, "reject_cluster")
        self.assertEqual(accepted.integrator_refresh_outcome, "applied")
        self.assertEqual(accepted.integrator_refresh_attempt, 1)
        self.assertEqual(accepted.integrator_refresh_latency_ms, 8123)
        self.assertEqual(accepted.authority_dependency_count, 2)
        self.assertEqual(accepted.provider_circuit_count, 1)
        self.assertEqual(accepted.priority, 4)
        self.assertEqual(accepted.pulse_count, 4)
        self.assertEqual(accepted.retained_pulse_count, 3)
        self.assertEqual(accepted.inflight, 3)
        self.assertEqual(accepted.mode_inflight, 2)
        self.assertEqual(accepted.inflight_before, 2)
        self.assertEqual(accepted.inflight_after, 3)
        self.assertEqual(accepted.mode_inflight_before, 1)
        self.assertEqual(accepted.mode_inflight_after, 2)
        self.assertEqual(accepted.max_inflight, 3)
        self.assertEqual(accepted.age_ms, 25)
        self.assertEqual(accepted.max_age_ms, 60000)
        self.assertTrue(accepted.accepted)
        self.assertTrue(accepted.eligible)
        self.assertTrue(accepted.changed)
        self.assertTrue(accepted.foreground_authority_present)
        self.assertFalse(accepted.foreground_authority_changed)
        self.assertEqual(accepted.relation_policy_version, "expert-relations-v2")
        self.assertEqual(accepted.relation_limit, 24)
        self.assertEqual(accepted.relation_received_count, 27)
        self.assertEqual(accepted.relation_accepted_count, 24)
        self.assertEqual(accepted.relation_invalid_count, 1)
        self.assertEqual(accepted.relation_duplicate_count, 1)
        self.assertEqual(accepted.relation_overflow_count, 1)
        self.assertEqual(accepted.relation_discarded_count, 3)
        self.assertEqual(accepted.presence_gap_policy_version, "presence-gap-v2")
        self.assertEqual(accepted.presence_gap_sha256, "presence-gap-digest")
        self.assertEqual(accepted.presence_gap_ambient_objective, "independent_life")
        self.assertEqual(accepted.presence_gap_preferred_surface, "network")
        self.assertEqual(accepted.presence_gap_anti_repetition_subject_count, 0)
        self.assertEqual(accepted.presence_gap_anti_repetition_system_count, 1)
        self.assertEqual(
            accepted.foreground_evidence_policy_version,
            "foreground-evidence-v1",
        )
        self.assertEqual(
            accepted.foreground_evidence_sha256,
            "foreground-evidence-digest",
        )
        self.assertTrue(accepted.host_time_observed)
        self.assertEqual(accepted.recent_exhaustive_evidence_count, 2)
        self.assertEqual(
            accepted.registry_targets,
            (("filesystem", "artifacts_by_path"),),
        )

    def test_live_sessions_include_metadata_state_profile_label(self):
        session = SessionState(
            username="alice",
            login_username="alice",
            langfuse_trace_id="s-a",
            cwd="/opt/app",
            environment_metadata=EnvironmentMetadata(
                metadata={
                    STATE_PROFILE_METADATA_NAME: "lab-system",
                    STATE_PROFILE_METADATA_LEVEL: "system",
                }
            ),
            history=[CommandHistoryEntry(cwd="/opt/app", command="whoami")],
        )
        self.registry.register(session)

        sessions = self.service.live_sessions()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "s-a")
        self.assertEqual(sessions[0].username, "alice")
        self.assertEqual(sessions[0].cwd, "/opt/app")
        self.assertEqual(sessions[0].history_count, 1)
        self.assertEqual(sessions[0].state_profile, "lab-system:system")

    def test_live_session_returns_single_summary(self):
        self.registry.register(
            SessionState(
                username="alice",
                login_username="alice",
                langfuse_trace_id="s-a",
                cwd="/opt/app",
            )
        )

        session = self.service.live_session("s-a")

        self.assertEqual(session.session_id, "s-a")
        self.assertEqual(session.username, "alice")
        self.assertEqual(session.cwd, "/opt/app")

    def test_sessions_combines_langfuse_and_live_attributes(self):
        self.registry.register(
            SessionState(
                username="alice",
                login_username="alice",
                langfuse_trace_id="s-a",
                cwd="/opt/app",
            )
        )
        self.registry.register(
            SessionState(
                username="bob",
                login_username="bob",
                langfuse_trace_id="live-only",
                cwd="/home/bob",
            )
        )

        sessions = self.service.sessions(limit=20)

        by_id = {session.id: session for session in sessions.sessions}
        self.assertTrue(sessions.langfuse_available)
        self.assertTrue(by_id["s-a"].live)
        self.assertTrue(by_id["s-a"].langfuse)
        self.assertEqual(by_id["s-a"].cwd, "/opt/app")
        self.assertFalse(by_id["live-only"].langfuse)
        self.assertTrue(by_id["live-only"].live)

    def test_live_sessions_filter_and_group_by_session_metadata(self):
        self.service.command_backend = AgentBackend(server_profile())
        first = self.service.create_mcp_session(
            username="alice",
            tags=["scenario:login", "batch:a"],
            label="batch-a",
        )
        second = self.service.create_mcp_session(
            username="bob",
            tags=["scenario:files", "batch:b"],
            label="batch-b",
        )

        login_sessions = self.service.live_sessions(tags=["scenario:login"])
        batch_sessions = self.service.sessions(
            limit=20,
            tags=["batch:a"],
            origin="mcp",
        )
        groups = self.service.live_session_tag_groups(origin="mcp")

        self.assertEqual(
            [session.session_id for session in login_sessions], [first.session_id]
        )
        self.assertEqual(
            [session.id for session in batch_sessions.sessions], [first.session_id]
        )
        by_tag = {group.tag: group for group in groups}
        self.assertEqual(by_tag["mcp"].count, 2)
        self.assertEqual(
            by_tag["scenario:login"].session_ids,
            [first.session_id],
        )
        self.assertEqual(
            by_tag["scenario:files"].session_ids,
            [second.session_id],
        )

    def test_session_inspection_combines_live_and_historical_access(self):
        self.registry.register(
            SessionState(
                username="alice",
                login_username="alice",
                langfuse_trace_id="s-a",
                cwd="/opt/app",
            )
        )

        detail = self.service.session("s-a")

        self.assertEqual(detail.id, "s-a")
        self.assertTrue(detail.live)
        self.assertTrue(detail.langfuse)
        self.assertTrue(detail.langfuse_available)
        self.assertEqual(detail.summary.cwd, "/opt/app")
        self.assertEqual(detail.live_session.cwd, "/opt/app")
        self.assertEqual(detail.historical_session.id, "s-a")
        self.assertTrue(detail.inspections["live_state"])
        self.assertTrue(detail.inspections["historical_commands"])

    def test_session_inspection_reports_live_only_session(self):
        self.registry.register(
            SessionState(
                username="bob",
                login_username="bob",
                langfuse_trace_id="live-only",
                cwd="/home/bob",
            )
        )

        detail = self.service.session("live-only")

        self.assertTrue(detail.live)
        self.assertFalse(detail.langfuse)
        self.assertEqual(detail.live_session.username, "bob")
        self.assertIsNone(detail.historical_session)
        self.assertTrue(detail.inspections["live_state"])
        self.assertFalse(detail.inspections["historical_commands"])

    def test_session_inspection_reports_historical_only_session(self):
        detail = self.service.session("s-a")

        self.assertFalse(detail.live)
        self.assertTrue(detail.langfuse)
        self.assertIsNone(detail.live_session)
        self.assertEqual(detail.historical_session.id, "s-a")
        self.assertFalse(detail.inspections["live_state"])
        self.assertTrue(detail.inspections["historical_state_delta"])

    def test_session_inspection_reports_missing_session(self):
        with self.assertRaises(AdminServiceError) as captured:
            self.service.session("missing", historical_limit=10)

        self.assertEqual(captured.exception.code, "not_found")
        self.assertEqual(captured.exception.details["session_id"], "missing")
        self.assertEqual(captured.exception.details["historical_lookup_limit"], 10)

    def test_live_session_state_returns_exact_session_snapshot(self):
        self.registry.register(
            SessionState(
                username="alice",
                login_username="alice",
                langfuse_trace_id="s-a",
                cwd="/opt/app",
                world_state=_profile_world(services={"redis": {}}),
                history=[CommandHistoryEntry(cwd="/opt/app", command="ls")],
            )
        )

        snapshot = self.service.live_session_state("s-a")

        self.assertEqual(snapshot.session_id, "s-a")
        self.assertEqual(snapshot.state["username"], "alice")
        self.assertEqual(snapshot.state["cwd"], "/opt/app")
        self.assertEqual(snapshot.state["history"][0]["command"], "ls")
        self.assertIn("world_state", snapshot.state)

    def test_live_session_state_reports_missing_session(self):
        with self.assertRaises(AdminServiceError) as captured:
            self.service.live_session_state("missing")

        self.assertEqual(captured.exception.code, "not_found")
        self.assertEqual(captured.exception.details["session_id"], "missing")

    def test_create_mcp_session_registers_tagged_live_session(self):
        self.service.command_backend = AgentBackend(server_profile())
        recorded_sessions = []
        self.service.record_session_start = recorded_sessions.append

        result = self.service.create_mcp_session(
            username="alice",
            tags=["scenario:login", "mcp"],
            label="agent-run-1",
            metadata={"agent": "codex"},
        )

        registration = self.registry.get(result.session_id)
        self.assertIsNotNone(registration)
        self.assertEqual(result.username, "alice")
        self.assertEqual(result.tags, ["mcp", "scenario:login"])
        self.assertEqual(result.label, "agent-run-1")
        self.assertEqual(result.metadata, {"agent": "codex"})
        self.assertEqual(recorded_sessions, [registration.session])
        self.assertEqual(
            registration.session.environment_metadata.connection.protocol, "mcp"
        )
        live = self.service.live_session(result.session_id)
        self.assertEqual(live.tags, ["mcp", "scenario:login"])
        self.assertEqual(live.origin, "mcp")

    def test_run_mcp_session_command_returns_user_visible_result(self):
        self.service.command_backend = AgentBackend(server_profile())
        self.service.trace_logs = configure_session_trace_logging(10)
        session = self.service.create_mcp_session(username="alice")

        result = self.service.run_mcp_session_command(
            session_id=session.session_id,
            command="pwd",
        )

        self.assertEqual(result.stdout, "/home/alice\n")
        self.assertEqual(result.stderr, "")
        self.assertIsNotNone(result.command_id)
        self.assertEqual(
            result.command_id,
            self.registry.get(session.session_id).session.history[0].id,
        )
        self.assertEqual(result.exit_status, 0)
        self.assertEqual(result.history_index, 0)
        self.assertEqual(result.history_position, 1)
        self.assertEqual(result.history_count, 1)
        self.assertEqual(result.inspection_target, "@-1")
        trace = self.service.session_trace(session_id=session.session_id, limit=10)
        trace_text = "\n".join(entry.text for entry in trace)
        self.assertIn("type=input text='pwd'", trace_text)
        self.assertIn("type=output stream=stdout text='/home/alice\\n'", trace_text)

    def test_run_mcp_session_command_rejects_shell_console_prefix(self):
        self.service.command_backend = AgentBackend(server_profile())
        session = self.service.create_mcp_session(username="alice")

        with self.assertRaises(AdminServiceError) as captured:
            self.service.run_mcp_session_command(
                session_id=session.session_id,
                command="!dreamland sessions",
            )

        self.assertEqual(captured.exception.code, "bad_request")

        self.assertIn("explicit MCP resources or tools", captured.exception.message)

    def test_mcp_empty_input_is_valid_only_when_the_program_needs_it(self):
        self.service.command_backend = AgentBackend(server_profile())
        created = self.service.create_mcp_session(username="alice")
        with self.assertRaises(AdminServiceError):
            self.service.run_mcp_session_command(
                session_id=created.session_id, command=""
            )
        session = self.registry.get(created.session_id).session
        apply_external_state_update(
            session,
            CommandStateUpdate(
                interactive_program=InteractiveProgram(
                    name="ssh-keygen",
                    command="/usr/bin/ssh-keygen",
                    argv=("ssh-keygen",),
                    metadata={"phase": "passphrase"},
                ),
                shell_stack_transition=ShellStackTransition(operation="push"),
            ),
        )
        for command in ("", "  "):
            result = self.service.run_mcp_session_command(
                session_id=created.session_id, command=command
            )
            self.assertEqual(result.command, command)
            self.assertIsNotNone(result.command_id)
            self.assertEqual(session.history[-1].command, command)

    def test_run_mcp_session_command_preserves_the_whole_original_line(self):
        self.service.command_backend = AgentBackend(server_profile())
        session = self.service.create_mcp_session(username="alice")
        command = "  cd /tmp; pwd ; touch testfile  "

        result = self.service.run_mcp_session_command(
            session_id=session.session_id, command=command
        )

        self.assertEqual(result.command, command)
        self.assertEqual(result.history_count, 1)
        runtime = self.registry.get(session.session_id).session
        self.assertEqual(runtime.history[0].command, command)
        self.assertEqual(runtime.cwd, "/home/alice")

    def test_close_live_sessions_closes_exact_mcp_session_with_origin_guard(self):
        self.service.command_backend = AgentBackend(server_profile())
        session = self.service.create_mcp_session(username="alice")
        self.registry.register(SessionState(username="bob", langfuse_trace_id="ssh-a"))

        closed = self.service.close_live_sessions(
            session_ids=[session.session_id],
            origin="mcp",
        )
        not_matched = self.service.close_live_sessions(
            session_ids=["ssh-a"],
            origin="mcp",
        )

        self.assertEqual(closed.closed, [session.session_id])
        self.assertEqual(closed.matched, 1)
        self.assertEqual(not_matched.closed, [])
        self.assertEqual(not_matched.matched, 0)
        self.assertIsNone(self.registry.get(session.session_id))
        self.assertIsNotNone(self.registry.get("ssh-a"))

    def test_close_live_sessions_logs_filtered_non_dry_run_close(self):
        self.service.command_backend = AgentBackend(server_profile())
        first = self.service.create_mcp_session(
            username="alice",
            tags=["batch:a"],
            label="run-a",
        )
        second = self.service.create_mcp_session(
            username="bob",
            tags=["batch:b"],
            label="run-b",
        )

        with self.assertLogs("dreamland.admin", level="WARNING") as captured:
            result = self.service.close_live_sessions(
                tags=["batch:a"],
                label="run-a",
                origin="mcp",
            )

        self.assertEqual(result.closed, [first.session_id])
        self.assertEqual(result.matched, 1)
        self.assertIn("Filtered live session close requested", captured.output[0])
        self.assertIn("batch:a", captured.output[0])
        self.assertIsNone(self.registry.get(first.session_id))
        self.assertIsNotNone(self.registry.get(second.session_id))

    def test_close_live_sessions_logs_non_mcp_close(self):
        self.registry.register(SessionState(username="bob", langfuse_trace_id="ssh-a"))

        with self.assertLogs("dreamland.admin", level="WARNING") as captured:
            result = self.service.close_live_sessions(session_ids=["ssh-a"])

        self.assertEqual(result.closed, ["ssh-a"])
        self.assertEqual(result.matched, 1)
        self.assertIn("Non-MCP live session close requested", captured.output[0])
        self.assertIn("ssh-a", captured.output[0])
        self.assertIsNone(self.registry.get("ssh-a"))

    def test_close_live_sessions_supports_dry_run_and_filters(self):
        self.service.command_backend = AgentBackend(server_profile())
        first = self.service.create_mcp_session(
            username="alice",
            tags=["batch:a"],
            label="run-a",
        )
        second = self.service.create_mcp_session(
            username="bob",
            tags=["batch:b"],
            label="run-b",
        )
        self.registry.register(
            SessionState(username="carol", langfuse_trace_id="ssh-a")
        )

        preview = self.service.close_live_sessions(
            tags=["batch:a"],
            origin="mcp",
            dry_run=True,
        )
        self.assertEqual(preview.matched, 1)
        self.assertEqual(preview.closed, [])
        self.assertEqual(preview.filters["tags"], ["batch:a"])
        self.assertEqual(preview.filters["origin"], "mcp")
        self.assertIsNotNone(self.registry.get(first.session_id))

        closed = self.service.close_live_sessions(
            tags=["batch:a"],
            origin="mcp",
        )

        self.assertEqual(closed.closed, [first.session_id])
        self.assertEqual(closed.matched, 1)
        self.assertIsNone(self.registry.get(first.session_id))
        self.assertIsNotNone(self.registry.get(second.session_id))
        self.assertIsNotNone(self.registry.get("ssh-a"))

    def test_close_live_sessions_requires_ids_or_filters(self):
        with self.assertRaises(AdminServiceError) as captured:
            self.service.close_live_sessions()

        self.assertEqual(captured.exception.code, "bad_request")

    def test_historical_sessions_are_enriched_from_state_profile_index(self):
        profile = create_state_profile(
            SessionState(
                username="alice",
                langfuse_trace_id="source",
                world_state=_profile_world(),
            ),
            name="indexed-system",
            level="system",
            created_at="2026-06-15T12:00:00+00:00",
        )
        self.state_profile_runtime.store.record_session_profile(
            session_id="s-a",
            profile=profile,
            source="apply",
            updated_at="2026-06-16T06:20:00+00:00",
        )

        sessions = self.service.list_historical_sessions(limit=20)

        self.assertEqual(sessions.sessions[0].id, "s-a")
        self.assertEqual(sessions.sessions[0].state_profile, "indexed-system:system")

    def test_historical_commands_and_inspection_delegate_to_introspection(self):
        commands = self.service.list_historical_commands(
            session_id="s-a",
            limit=5,
            filter_text="who",
            include_io_summary=False,
        )
        inspection = self.service.inspect_command(
            session_id="s-a",
            target=None,
            limit=5,
            detail="compact",
        )
        delta = self.service.state_delta(
            session_id="s-a",
            from_target=None,
            to_target="@-1",
            limit=5,
            sections=("cwd",),
        )

        self.assertEqual([command.command for command in commands], ["whoami"])
        self.assertEqual(self.introspection.include_io_summary_calls, [False, False])
        self.assertEqual(inspection.command.id, "cmd-1")
        self.assertEqual(delta.sections, ("cwd",))

    def test_state_profile_summaries_and_default_are_structured(self):
        profile = create_state_profile(
            SessionState(
                username="alice",
                langfuse_trace_id="source",
                world_state=_profile_world(),
            ),
            name="default-system",
            level="system",
            comment="checkpoint",
            created_at="2026-06-15T12:00:00+00:00",
        )
        self.state_profile_runtime.store.save(profile)
        self.state_profile_runtime.set_default("default-system")

        summaries = self.service.list_state_profiles()
        default = self.service.default_state_profile()
        status = self.service.status()

        self.assertEqual(summaries[0].name, "default-system")
        self.assertTrue(summaries[0].path.endswith("default-system.json"))
        self.assertEqual(default.name, "default-system")
        self.assertEqual(default.comment, "checkpoint")
        self.assertEqual(status.default_state_profile, "default-system:system")

    def test_state_profile_detail_loads_saved_profile(self):
        profile = create_state_profile(
            SessionState(
                username="alice",
                langfuse_trace_id="source",
                world_state=_profile_world(),
            ),
            name="default-system",
            level="system",
            comment="checkpoint",
            created_at="2026-06-15T12:00:00+00:00",
        )
        self.state_profile_runtime.store.save(profile)

        detail = self.service.state_profile("default-system")

        self.assertEqual(detail.name, "default-system")
        self.assertEqual(detail.comment, "checkpoint")
        self.assertIn("world_state", detail.state)

    def test_missing_state_profile_detail_reports_not_found(self):
        with self.assertRaises(AdminServiceError) as captured:
            self.service.state_profile("missing")

        self.assertEqual(captured.exception.code, "not_found")
        self.assertEqual(captured.exception.details["name"], "missing")

    def test_structured_failure_summaries_detail_and_replay_are_structured(self):
        capsule = _structured_failure_capsule()
        self.structured_failure_store.save(capsule)

        summaries = self.service.list_structured_failures()
        detail = self.service.structured_failure(capsule.id)
        replay = self.service.replay_structured_failure_parser(capsule.id)

        self.assertEqual(summaries[0].id, capsule.id)
        self.assertEqual(summaries[0].failure_class, "invalid_json")
        self.assertEqual(detail.raw_response, capsule.raw_response)
        self.assertEqual(detail.result["exit_status"], 0)
        self.assertFalse(replay.accepted)
        self.assertEqual(replay.failure_class, "invalid_json")
        self.assertIn("invalid JSON", replay.parser_error)

    def test_missing_structured_failure_reports_not_found(self):
        with self.assertRaises(AdminServiceError) as captured:
            self.service.structured_failure("missing")

        self.assertEqual(captured.exception.code, "not_found")
        self.assertEqual(captured.exception.details["id"], "missing")

    def test_flush_introspection_delegates_to_introspection(self):
        self.service.flush_introspection()

        self.assertEqual(self.introspection.flush_count, 1)

    def test_effective_config_returns_redacted_snapshot(self):
        config = self.service.config_effective()

        self.assertTrue(config["openai"]["api_key_configured"])
        self.assertTrue(config["api"]["token_configured"])

    def test_save_state_profile_from_live_session(self):
        self.registry.register(
            SessionState(
                username="alice",
                langfuse_trace_id="s-a",
                cwd="/opt/app",
                world_state=_profile_world(),
            )
        )

        result = self.service.save_state_profile(
            name="saved-system",
            level="system",
            session_id="s-a",
            comment="checkpoint",
        )

        self.assertEqual(result.name, "saved-system")
        self.assertEqual(result.session_id, "s-a")
        self.assertEqual(
            self.state_profile_runtime.store.load("saved-system").comment,
            "checkpoint",
        )

    def test_apply_state_profile_replaces_live_session_and_records_index(self):
        profile = create_state_profile(
            SessionState(
                username="alice",
                langfuse_trace_id="source",
                cwd="/srv/restore",
                world_state=_profile_world(),
            ),
            name="restore-point",
            level="exact",
            created_at="2026-06-15T12:00:00+00:00",
        )
        self.state_profile_runtime.store.save(profile)
        session = SessionState(
            username="bob",
            langfuse_trace_id="target-session",
            cwd="/home/bob",
        )
        self.registry.register(session)

        result = self.service.apply_state_profile(
            name="restore-point",
            session_id="target-session",
        )

        self.assertEqual(result.name, "restore-point")
        self.assertEqual(session.cwd, "/srv/restore")
        self.assertEqual(
            self.state_profile_runtime.store.session_profile("target-session").label,
            "restore-point:exact",
        )

    def test_default_state_profile_can_be_set_cleared_and_deleted(self):
        profile = create_state_profile(
            SessionState(
                username="alice",
                langfuse_trace_id="source",
                world_state=_profile_world(),
            ),
            name="runtime-default",
            level="system",
            created_at="2026-06-15T12:00:00+00:00",
        )
        self.state_profile_runtime.store.save(profile)

        default = self.service.set_default_state_profile("runtime-default")
        cleared = self.service.clear_default_state_profile()
        self.state_profile_runtime.set_default("runtime-default")
        deleted = self.service.delete_state_profile("runtime-default")

        self.assertEqual(default.name, "runtime-default")
        self.assertEqual(cleared.previous.name, "runtime-default")
        self.assertTrue(deleted.cleared_default)
        self.assertIsNone(self.state_profile_runtime.current_profile())

    def test_spiritguide_applies_update_to_live_session(self):
        session = SessionState(
            username="alice",
            langfuse_trace_id="s-a",
            cwd="/home/alice",
        )
        self.registry.register(session)

        result = self.service.run_spiritguide(
            session_id="s-a",
            instruction="move cwd",
        )

        self.assertEqual(result.message, "Done.")
        self.assertTrue(result.applied)
        self.assertEqual(result.patch_keys, ["cwd"])
        self.assertEqual(session.cwd, "/srv/app")
        self.assertEqual(self.spiritguide.instructions, ["move cwd"])

    def test_spiritguide_dry_run_does_not_apply_update(self):
        session = SessionState(
            username="alice",
            langfuse_trace_id="s-a",
            cwd="/home/alice",
        )
        self.registry.register(session)

        result = self.service.run_spiritguide(
            session_id="s-a",
            instruction="move cwd",
            dry_run=True,
        )

        self.assertFalse(result.applied)
        self.assertEqual(session.cwd, "/home/alice")

    def test_spiritguide_profile_create_saves_world_state_without_session_fields(self):
        spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Factory world created.",
                state_update=CommandStateUpdate(
                    cwd="/srv/factory",
                    world_state={
                        "entry_host": "ubuntu",
                        "hosts_by_name": {
                            "ubuntu": {
                                "login_banner": "Factory maintenance console\n",
                                "services_by_name": {"plc-gateway": {"port": 502}},
                                "filesystem": {
                                    "artifacts_by_path": {
                                        "/etc/factory/topology.json": {
                                            "kind": "network-topology"
                                        }
                                    }
                                },
                            }
                        },
                    },
                ),
                metadata={"structured_response_accepted": True},
            )
        )
        self.service.spiritguide = spiritguide

        result = self.service.create_spiritguide_profile(
            name="factory-lab",
            instruction="build a factory OT lab",
            comment="seed world",
        )

        profile = self.state_profile_runtime.store.load("factory-lab")
        world_state = profile.state["world_state"]
        host = world_state["hosts_by_name"]["ubuntu"]
        self.assertEqual(result.name, "factory-lab")
        self.assertTrue(result.saved)
        self.assertEqual(result.level, "world")
        self.assertIn("cwd", result.excluded_patch_keys)
        self.assertNotIn("cwd", profile.state)
        self.assertEqual(host["services_by_name"]["plc-gateway"]["port"], 502)
        self.assertIn(
            "/etc/factory/topology.json",
            host["filesystem"]["artifacts_by_path"],
        )
        self.assertEqual(spiritguide.instructions, ["build a factory OT lab"])
        seed_world_state = spiritguide.world_states[0]
        self.assertEqual(seed_world_state["entry_host"], "operator-defined")
        self.assertIn("operator-defined", seed_world_state["hosts_by_name"])
        self.assertNotIn("ubuntu", seed_world_state["hosts_by_name"])
        self.assertNotIn("roleplay_guidance", seed_world_state["purpose"])
        self.assertTrue(_valid_langfuse_trace_id(spiritguide.session_ids[0]))
        self.assertTrue(_valid_langfuse_trace_id(profile.source_session_id))

    def test_spiritguide_profile_create_finalizes_placeholder_entry_host(self):
        self.service.spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Colony ship created.",
                state_update=CommandStateUpdate(
                    world_state={
                        "name": "aethers-promise",
                        "entry_host": "aether-core",
                        "hosts_by_name": {
                            "aether-core": {
                                "hostname": "aether-core",
                                "login_banner": "Aether command and control\n",
                                "purpose": {
                                    "theme": "Primary ship command and control",
                                    "description": "Central hardened core.",
                                },
                                "shell": {
                                    "default": {
                                        "name": "starsh",
                                        "command": "/bin/starsh",
                                        "argv": ["starsh"],
                                    }
                                },
                                "filesystem": {"artifacts_by_path": {}},
                                "services_by_name": {},
                                "facts_by_id": {},
                                "command_history": [],
                            }
                        },
                    }
                ),
                metadata={"structured_response_accepted": True},
            )
        )

        self.service.create_spiritguide_profile(
            name="colonyship",
            instruction="build a colony ship",
        )

        profile = self.state_profile_runtime.store.load("colonyship")
        world_state = profile.state["world_state"]
        self.assertEqual(world_state["entry_host"], "aether-core")
        self.assertEqual(world_state["active_host"], "aether-core")
        self.assertIn("aether-core", world_state["hosts_by_name"])
        self.assertNotIn("operator-defined", world_state["hosts_by_name"])

    def test_spiritguide_profile_update_iterates_existing_profile(self):
        self.service.spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Seeded.",
                state_update=CommandStateUpdate(
                    world_state={
                        "entry_host": "jump01",
                        "hosts_by_name": {
                            "jump01": {
                                "role": "jump host",
                                "login_banner": "Jump host\n",
                            }
                        },
                    }
                ),
                metadata={"structured_response_accepted": True},
            )
        )
        self.service.create_spiritguide_profile(
            name="factory-lab",
            instruction="seed jump host",
        )
        spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Added engineering workstation.",
                state_update=CommandStateUpdate(
                    world_state={
                        "hosts_by_name": {
                            "eng-ws01": {"role": "engineering workstation"}
                        }
                    }
                ),
                metadata={"structured_response_accepted": True},
            )
        )
        self.service.spiritguide = spiritguide

        result = self.service.update_spiritguide_profile(
            name="factory-lab",
            instruction="add engineering workstation",
        )

        profile = self.state_profile_runtime.store.load("factory-lab")
        hosts = profile.state["world_state"]["hosts_by_name"]
        self.assertEqual(result.action, "update")
        self.assertTrue(result.saved)
        self.assertIn("jump01", hosts)
        self.assertIn("eng-ws01", hosts)
        self.assertEqual(spiritguide.instructions, ["add engineering workstation"])
        self.assertTrue(_valid_langfuse_trace_id(spiritguide.session_ids[0]))
        self.assertTrue(_valid_langfuse_trace_id(profile.source_session_id))

    def test_spiritguide_profile_fork_without_instruction_copies_profile(self):
        self.service.spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Seeded.",
                state_update=CommandStateUpdate(
                    world_state={
                        "entry_host": "ubuntu",
                        "hosts_by_name": {
                            "ubuntu": {
                                "login_banner": "Redis maintenance console\n",
                                "services_by_name": {"redis": {"port": 6379}},
                            }
                        },
                    }
                ),
                metadata={"structured_response_accepted": True},
            )
        )
        self.service.create_spiritguide_profile(
            name="source-world",
            instruction="seed redis",
        )

        result = self.service.fork_spiritguide_profile(
            source_name="source-world",
            name="forked-world",
        )

        profile = self.state_profile_runtime.store.load("forked-world")
        self.assertEqual(result.action, "fork")
        self.assertTrue(result.saved)
        self.assertEqual(result.source_name, "source-world")
        self.assertIn(
            "redis",
            profile.state["world_state"]["hosts_by_name"]["ubuntu"]["services_by_name"],
        )
        self.assertTrue(_valid_langfuse_trace_id(profile.source_session_id))

    def test_shutdown_invokes_callback(self):
        result = self.service.shutdown()

        self.assertEqual(result.status, "shutdown_requested")
        self.assertEqual(self.shutdown_count, 1)
        self.assertTrue(self.service.status().shutdown_requested)

    def _request_shutdown(self):
        self.shutdown_count += 1


class FakeIntrospection:
    def __init__(self):
        self.include_io_summary_calls = []
        self.flush_count = 0

    def available(self):
        return True

    def flush(self):
        self.flush_count += 1

    def list_sessions(self, *, limit):
        return SessionList(
            sessions=[SessionSummary(id="s-a", created_at="2026-06-07T20:00:00Z")],
            total=1,
        )

    def list_commands(
        self,
        *,
        session_id,
        limit,
        filter_text=None,
        sort="time",
        descending=True,
        include_io_summary=True,
    ):
        self.include_io_summary_calls.append(include_io_summary)
        commands = [
            CommandSummary(
                id="cmd-1",
                session_id=session_id,
                timestamp="2026-06-07T21:01:00Z",
                command="whoami",
            ),
            CommandSummary(
                id="cmd-2",
                session_id=session_id,
                timestamp="2026-06-07T21:02:00Z",
                command="id",
            ),
        ]
        if filter_text:
            return [command for command in commands if filter_text in command.command]
        return commands

    def inspect_command(self, *, session_id, target, limit, detail="full"):
        full_detail = detail == "full"
        command = self.list_commands(
            session_id=session_id,
            limit=limit,
            include_io_summary=full_detail,
        )[0]
        return CommandInspection(
            command=command,
            detail=detail,
            trace_input={"command": command.command} if full_detail else None,
            trace_output={"stdout": "alice\n"} if full_detail else None,
            observations=[
                ObservationSummary(
                    id="obs-1",
                    type="GENERATION",
                    name="model",
                )
            ],
        )

    def state_delta(
        self,
        *,
        session_id,
        from_target,
        to_target,
        limit,
        sections=(),
    ):
        return StateDelta(
            from_label="before cmd-1",
            to_label="after cmd-1",
            before_state={"cwd": "/home/alice"},
            after_state={"cwd": "/opt/app"},
            sections=tuple(sections),
        )


class FakeSpiritguide:
    def __init__(self, result):
        self.result = result
        self.instructions = []
        self.session_ids = []
        self.dry_runs = []
        self.world_states = []

    def respond(self, context):
        self.instructions.append(context.instruction)
        self.session_ids.append(context.session_id)
        self.dry_runs.append(context.dry_run)
        self.world_states.append(context.session.world_state)
        return self.result


def _record(message):
    return logging.LogRecord(
        "dreamland.lifecycle",
        logging.INFO,
        __file__,
        1,
        message,
        (),
        None,
    )


def _valid_langfuse_trace_id(value):
    if not isinstance(value, str) or len(value) != 32 or value != value.lower():
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _trace_record(session_id, message):
    record = logging.LogRecord(
        "dreamland.trace",
        logging.INFO,
        __file__,
        1,
        message,
        (),
        None,
    )
    record.dreamland_session_id = session_id
    return record


def _enrichment_record(session_id, job_id, event, *, fields=None):
    record = logging.LogRecord(
        "dreamland.enrichment",
        logging.INFO,
        __file__,
        1,
        f"event={event}",
        (),
        None,
    )
    record.dreamland_session_id = session_id
    record.dreamland_enrichment_job_id = job_id
    record.dreamland_enrichment_event = event
    if fields is not None:
        record.dreamland_enrichment_fields = fields
    return record


def _structured_failure_capsule():
    session = SessionState(username="alice", langfuse_trace_id="session-failure")
    result = command_result_from_model_response('{"stdout": "unterminated"')
    return create_structured_failure_capsule(
        session,
        command="cat broken.json",
        command_id="cmd-failure",
        starting_cwd="/home/alice",
        result=result,
    )


if __name__ == "__main__":
    unittest.main()
