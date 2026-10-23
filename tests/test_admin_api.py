import json
import logging
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dreamland.admin_api import DreamlandAdminAPIServer
from dreamland.admin_interface import DreamlandAdminService
from dreamland.agent_structured import command_result_from_model_response
from dreamland.backend import AgentBackend
from dreamland.config import ApiSettings
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
    SessionState,
)
from dreamland.profile import server_profile
from dreamland.runtime_logging import (
    EnrichmentEventLogBuffer,
    LogRecordBuffer,
    SessionTraceLogBuffer,
)
from dreamland.session_registry import LiveSessionRegistry
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
from dreamland.world_state import default_world_state


class DreamlandAdminAPIServerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.logs = LogRecordBuffer(10)
        self.logs.setFormatter(logging.Formatter("%(message)s"))
        self.trace_logs = SessionTraceLogBuffer(10)
        self.trace_logs.setFormatter(logging.Formatter("%(message)s"))
        self.enrichment_event_logs = EnrichmentEventLogBuffer(10)
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
                message="Moved.",
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
            mcp_write_tools=False,
            effective_config={
                "openai": {"api_key_configured": True},
                "api": {"token_configured": True},
            },
            command_backend=AgentBackend(server_profile()),
            structured_failure_store=self.structured_failure_store,
        )
        self.mcp_session_id = None
        self.client_mcp_session_id = None
        self.server = DreamlandAdminAPIServer(
            settings=ApiSettings(
                enabled=True,
                host="127.0.0.1",
                port=0,
                token="test-token",
            ),
            service=self.service,
        )
        self.server.start_in_thread()
        self.assertTrue(self.server.wait_until_ready())

    def tearDown(self):
        self.server.stop()
        self.tmpdir.cleanup()

    def test_health_requires_bearer_token(self):
        with self.assertRaises(HTTPError) as captured:
            self._get("/api/v0/health", token=None)

        self.assertEqual(captured.exception.code, 401)
        self.assertEqual(
            _error_payload(captured.exception)["error"]["code"], "unauthorized"
        )

    def test_health_returns_ok_with_bearer_token(self):
        payload = self._get("/api/v0/health")

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["api_version"], "v0")

    def test_start_in_thread_reports_missing_optional_dependencies(self):
        self.server.stop()
        server = self._new_server()

        with patch(
            "dreamland.admin_api._load_admin_dependencies",
            side_effect=RuntimeError("missing optional admin dependencies"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "missing optional admin dependencies",
            ):
                server.start_in_thread()

    def test_wait_until_ready_reports_thread_startup_failure(self):
        self.server.stop()
        server = self._new_server()

        try:
            with patch(
                "dreamland.admin_api._bind_socket",
                side_effect=OSError("test bind failure"),
            ):
                server.start_in_thread()
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Dreamland Admin API server failed to start: test bind failure",
                ):
                    server.wait_until_ready(timeout=1)
        finally:
            server.stop()

    def test_status_and_capabilities_return_service_data(self):
        status = self._get("/api/v0/status")
        capabilities = self._get("/api/v0/capabilities")
        config = self._get("/api/v0/config/effective")

        self.assertEqual(status["live_session_count"], 0)
        self.assertTrue(status["introspection_available"])
        self.assertTrue(capabilities["logs"])
        self.assertTrue(capabilities["live_sessions"])
        self.assertFalse(capabilities["mcp_write_tools"])
        self.assertTrue(config["openai"]["api_key_configured"])
        self.assertTrue(config["api"]["token_configured"])

    def test_unknown_route_uses_error_envelope(self):
        with self.assertRaises(HTTPError) as captured:
            self._get("/api/v0/missing")

        self.assertEqual(captured.exception.code, 404)
        payload = _error_payload(captured.exception)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_unsupported_method_still_requires_bearer_token(self):
        request = Request(
            self._url("/api/v0/health"),
            method="BREW",
        )

        with self.assertRaises(HTTPError) as captured:
            urlopen(request, timeout=5)

        self.assertEqual(captured.exception.code, 401)

    def test_unsupported_method_with_token_returns_json_405(self):
        request = Request(
            self._url("/api/v0/health"),
            headers={"Authorization": "Bearer test-token"},
            method="BREW",
        )

        with self.assertRaises(HTTPError) as captured:
            urlopen(request, timeout=5)

        self.assertEqual(captured.exception.code, 405)
        self.assertEqual(
            _error_payload(captured.exception)["error"]["code"],
            "method_not_allowed",
        )

    def test_logs_endpoint_supports_limit_and_after_cursor(self):
        self.logs.handle(_record("first"))
        self.logs.handle(_record("second"))
        self.logs.handle(_record("third"))

        payload = self._get("/api/v0/logs?after=1&limit=10")

        self.assertEqual([entry["text"] for entry in payload], ["second", "third"])
        self.assertEqual(payload[0]["sequence"], 2)

    def test_enrichment_events_endpoint_supports_exact_job_and_cursor(self):
        self.enrichment_event_logs.handle(
            _enrichment_record("session-closed", "job-1", "trigger.accepted")
        )
        self.enrichment_event_logs.handle(
            _enrichment_record("session-closed", "job-2", "job.queued")
        )
        self.enrichment_event_logs.handle(
            _enrichment_record(
                "session-closed",
                "job-1",
                "job.completed",
                fields={
                    "event_id": "event-1",
                    "correlation_id": "correlation-1",
                    "enrichment_mode": "ambient_presence",
                    "coordinator_role": "ambient_coordinator",
                    "selected_surface": "filesystem",
                    "surface": "filesystem",
                    "status": "applied",
                    "reason": "applied",
                    "retained_enrichment_mode": "focused",
                    "presence_probe_kind": "session_presence",
                    "retained_presence_probe_kind": "network_presence",
                    "subject_key_sha256": "subject-digest",
                    "retained_subject_key_sha256": "retained-subject-digest",
                    "cooldown_scope_sha256": "cooldown-digest",
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
                    "action": "reject_cluster",
                    "integrator_refresh_outcome": "applied",
                    "integrator_refresh_attempt": 1,
                    "integrator_refresh_latency_ms": 8123,
                    "pulse_count": 4,
                    "retained_pulse_count": 3,
                    "inflight": 3,
                    "mode_inflight": 2,
                    "inflight_before": 2,
                    "inflight_after": 3,
                    "mode_inflight_before": 1,
                    "mode_inflight_after": 2,
                    "max_inflight": 3,
                    "authority_dependency_count": 2,
                    "provider_circuit_count": 1,
                    "foreground_authority_present": True,
                    "foreground_authority_changed": False,
                    "eligible": True,
                    "changed": True,
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

        payload = self._get(
            "/api/v0/sessions/session-closed/enrichment-events"
            "?job_id=job-1&after=1&limit=10"
        )

        self.assertEqual(payload["session_id"], "session-closed")
        self.assertEqual(payload["job_id"], "job-1")
        self.assertEqual(payload["returned"], 1)
        event = payload["events"][0]
        self.assertEqual(event["event"], "job.completed")
        self.assertEqual(event["event_id"], "event-1")
        self.assertEqual(event["correlation_id"], "correlation-1")
        self.assertEqual(event["enrichment_mode"], "ambient_presence")
        self.assertEqual(event["coordinator_role"], "ambient_coordinator")
        self.assertEqual(event["selected_surface"], "filesystem")
        self.assertEqual(event["surface"], "filesystem")
        self.assertEqual(event["status"], "applied")
        self.assertEqual(event["reason"], "applied")
        self.assertEqual(event["retained_enrichment_mode"], "focused")
        self.assertEqual(event["presence_probe_kind"], "session_presence")
        self.assertEqual(event["retained_presence_probe_kind"], "network_presence")
        self.assertEqual(event["subject_key_sha256"], "subject-digest")
        self.assertEqual(
            event["retained_subject_key_sha256"],
            "retained-subject-digest",
        )
        self.assertEqual(event["cooldown_scope_sha256"], "cooldown-digest")
        self.assertEqual(event["match_policy"], "interaction-focus-match-v1")
        self.assertEqual(event["match_classification"], "artifact_exact")
        self.assertEqual(event["command_focus_key_sha256"], "command-focus-digest")
        self.assertEqual(event["command_focus_path_count"], 1)
        self.assertEqual(event["command_focus_artifact_count"], 2)
        self.assertEqual(event["retained_focus_key_sha256"], "retained-focus-digest")
        self.assertEqual(event["retained_focus_path_count"], 3)
        self.assertEqual(event["retained_focus_artifact_count"], 4)
        self.assertEqual(event["failure_kind"], "provider_quota")
        self.assertEqual(event["failure_disposition"], "circuit_opened")
        self.assertEqual(event["provider"], "openai")
        self.assertEqual(event["provider_code"], "credit_balance_exhausted")
        self.assertEqual(event["provider_status_code"], 402)
        self.assertEqual(event["role"], "coordinator")
        self.assertEqual(event["model"], "gpt-5.6-terra")
        self.assertEqual(event["circuit_origin"], "coordinator")
        self.assertEqual(event["authority_domain"], "shell_session")
        self.assertEqual(event["binding_reason"], "latest_foreground")
        self.assertEqual(event["policy_version"], "foreground-authority-v1")
        self.assertEqual(event["action"], "reject_cluster")
        self.assertEqual(event["integrator_refresh_outcome"], "applied")
        self.assertEqual(event["integrator_refresh_attempt"], 1)
        self.assertEqual(event["integrator_refresh_latency_ms"], 8123)
        self.assertEqual(event["pulse_count"], 4)
        self.assertEqual(event["retained_pulse_count"], 3)
        self.assertEqual(event["inflight"], 3)
        self.assertEqual(event["mode_inflight"], 2)
        self.assertEqual(event["inflight_before"], 2)
        self.assertEqual(event["inflight_after"], 3)
        self.assertEqual(event["mode_inflight_before"], 1)
        self.assertEqual(event["mode_inflight_after"], 2)
        self.assertEqual(event["max_inflight"], 3)
        self.assertEqual(event["authority_dependency_count"], 2)
        self.assertEqual(event["provider_circuit_count"], 1)
        self.assertTrue(event["foreground_authority_present"])
        self.assertFalse(event["foreground_authority_changed"])
        self.assertTrue(event["eligible"])
        self.assertTrue(event["changed"])
        self.assertEqual(event["relation_policy_version"], "expert-relations-v2")
        self.assertEqual(event["relation_limit"], 24)
        self.assertEqual(event["relation_received_count"], 27)
        self.assertEqual(event["relation_accepted_count"], 24)
        self.assertEqual(event["relation_invalid_count"], 1)
        self.assertEqual(event["relation_duplicate_count"], 1)
        self.assertEqual(event["relation_overflow_count"], 1)
        self.assertEqual(event["relation_discarded_count"], 3)
        self.assertEqual(event["presence_gap_policy_version"], "presence-gap-v2")
        self.assertEqual(event["presence_gap_sha256"], "presence-gap-digest")
        self.assertEqual(event["presence_gap_ambient_objective"], "independent_life")
        self.assertEqual(event["presence_gap_preferred_surface"], "network")
        self.assertEqual(event["presence_gap_anti_repetition_subject_count"], 0)
        self.assertEqual(event["presence_gap_anti_repetition_system_count"], 1)
        self.assertEqual(
            event["foreground_evidence_policy_version"],
            "foreground-evidence-v1",
        )
        self.assertEqual(
            event["foreground_evidence_sha256"],
            "foreground-evidence-digest",
        )
        self.assertTrue(event["host_time_observed"])
        self.assertEqual(event["recent_exhaustive_evidence_count"], 2)
        self.assertIsNone(event["candidate_id"])
        self.assertEqual(
            event["registry_targets"],
            [["filesystem", "artifacts_by_path"]],
        )
        self.assertEqual(payload["retention"]["retained"], 3)
        self.assertEqual(payload["retention"]["storage"], "process_memory")
        self.assertFalse(payload["retention"]["restart_durable"])

    def test_events_endpoint_streams_log_sse(self):
        self.logs.handle(_record("first"))
        self.logs.handle(_record("second"))

        events = _sse_events(
            self._get_text("/api/v0/events?after=1&limit=10&once=true")
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "log")
        self.assertEqual(events[0]["id"], "2")
        self.assertEqual(events[0]["data"]["sequence"], 2)
        self.assertEqual(events[0]["data"]["data"]["text"], "second")

    def test_bad_query_parameter_returns_json_400(self):
        with self.assertRaises(HTTPError) as captured:
            self._get("/api/v0/logs?limit=bad")

        self.assertEqual(captured.exception.code, 400)
        payload = _error_payload(captured.exception)
        self.assertEqual(payload["error"]["code"], "bad_request")

    def test_sessions_endpoint_returns_historical_session_list(self):
        payload = self._get("/api/v0/sessions?limit=5")

        self.assertEqual(payload["total"], 1)
        self.assertTrue(payload["langfuse_available"])
        self.assertEqual(payload["sessions"][0]["id"], "historical-session")
        self.assertFalse(payload["sessions"][0]["live"])
        self.assertTrue(payload["sessions"][0]["langfuse"])

    def test_sessions_endpoint_marks_session_found_live_and_in_langfuse(self):
        self.registry.register(
            SessionState(
                username="alice",
                login_username="alice",
                langfuse_trace_id="historical-session",
                cwd="/opt/app",
            )
        )

        payload = self._get("/api/v0/sessions")

        session = payload["sessions"][0]
        self.assertEqual(session["id"], "historical-session")
        self.assertTrue(session["live"])
        self.assertTrue(session["langfuse"])
        self.assertEqual(session["username"], "alice")
        self.assertEqual(session["cwd"], "/opt/app")

    def test_session_endpoint_returns_unified_live_and_historical_detail(self):
        self.registry.register(
            SessionState(
                username="alice",
                login_username="alice",
                langfuse_trace_id="historical-session",
                cwd="/opt/app",
            )
        )

        payload = self._get("/api/v0/sessions/historical-session")

        self.assertEqual(payload["id"], "historical-session")
        self.assertTrue(payload["live"])
        self.assertTrue(payload["langfuse"])
        self.assertTrue(payload["langfuse_available"])
        self.assertEqual(payload["summary"]["cwd"], "/opt/app")
        self.assertEqual(payload["live_session"]["cwd"], "/opt/app")
        self.assertEqual(payload["historical_session"]["id"], "historical-session")
        self.assertTrue(payload["inspections"]["live_state"])
        self.assertTrue(payload["inspections"]["historical_commands"])

    def test_session_endpoint_returns_historical_only_detail(self):
        payload = self._get("/api/v0/sessions/historical-session")

        self.assertEqual(payload["id"], "historical-session")
        self.assertFalse(payload["live"])
        self.assertTrue(payload["langfuse"])
        self.assertIsNone(payload["live_session"])
        self.assertEqual(payload["historical_session"]["id"], "historical-session")
        self.assertFalse(payload["inspections"]["live_state"])
        self.assertTrue(payload["inspections"]["historical_state_delta"])

    def test_live_sessions_state_and_trace_endpoints_return_live_data(self):
        session = SessionState(
            username="alice",
            login_username="alice",
            langfuse_trace_id="session-a",
            cwd="/opt/app",
            environment_metadata=EnvironmentMetadata(
                metadata={
                    STATE_PROFILE_METADATA_NAME: "lab",
                    STATE_PROFILE_METADATA_LEVEL: "exact",
                }
            ),
            history=[CommandHistoryEntry(cwd="/opt/app", command="whoami")],
        )
        self.registry.register(session)
        self.trace_logs.handle(_trace_record("session-a", "input"))
        self.trace_logs.handle(_trace_record("session-a", "output"))

        sessions = self._get("/api/v0/live-sessions")
        summary = self._get("/api/v0/live-sessions/session-a")
        state = self._get("/api/v0/sessions/session-a/state")
        trace = self._get("/api/v0/sessions/session-a/trace?limit=1")

        self.assertEqual(sessions[0]["session_id"], "session-a")
        self.assertEqual(summary["cwd"], "/opt/app")
        self.assertEqual(summary["state_profile"], "lab:exact")
        self.assertEqual(state["state"]["history"][0]["command"], "whoami")
        self.assertEqual([entry["text"] for entry in trace], ["output"])

    def test_session_events_endpoint_streams_trace_sse(self):
        self.registry.register(
            SessionState(username="alice", langfuse_trace_id="session-a")
        )
        self.trace_logs.handle(_trace_record("session-a", "input"))
        self.trace_logs.handle(_trace_record("session-a", "output"))

        events = _sse_events(
            self._get_text(
                "/api/v0/sessions/session-a/events?after=1&limit=10&once=true"
            )
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "trace")
        self.assertEqual(events[0]["id"], "2")
        self.assertEqual(events[0]["data"]["sequence"], 2)
        self.assertEqual(events[0]["data"]["data"]["session_id"], "session-a")
        self.assertEqual(events[0]["data"]["data"]["text"], "output")

    def test_session_events_endpoint_requires_live_session(self):
        with self.assertRaises(HTTPError) as captured:
            self._get_text("/api/v0/sessions/missing/events?once=true")

        self.assertEqual(captured.exception.code, 404)
        payload = _error_payload(captured.exception)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_missing_live_session_returns_json_404(self):
        with self.assertRaises(HTTPError) as captured:
            self._get("/api/v0/sessions/missing/state")

        self.assertEqual(captured.exception.code, 404)
        payload = _error_payload(captured.exception)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(payload["error"]["details"]["session_id"], "missing")

    def test_commands_endpoint_returns_historical_commands(self):
        payload = self._get(
            "/api/v0/sessions/historical-session/commands"
            "?filter=who&include_io_summary=false"
        )

        self.assertEqual([command["command"] for command in payload], ["whoami"])
        self.assertEqual(payload[0]["id"], "cmd-1")

    def test_command_inspection_endpoint_returns_structured_data(self):
        payload = self._get("/api/v0/sessions/historical-session/commands/cmd-1")

        self.assertEqual(payload["command"]["id"], "cmd-1")
        self.assertEqual(payload["detail"], "full")
        self.assertEqual(payload["trace_output"]["stdout"], "alice\n")
        self.assertEqual(payload["observations"][0]["id"], "obs-1")

    def test_command_inspection_endpoint_supports_compact_detail(self):
        payload = self._get(
            "/api/v0/sessions/historical-session/commands/cmd-1?detail=compact"
        )

        self.assertEqual(payload["command"]["id"], "cmd-1")
        self.assertEqual(payload["detail"], "compact")
        self.assertIsNone(payload["trace_input"])
        self.assertIsNone(payload["trace_output"])
        self.assertIsNone(payload["trace_metadata"])

    def test_state_delta_endpoint_returns_structured_data(self):
        payload = self._get(
            "/api/v0/sessions/historical-session/state-delta?to=@-1&section=cwd"
        )

        self.assertEqual(payload["from_label"], "before cmd-1")
        self.assertEqual(payload["to_label"], "after cmd-1")
        self.assertEqual(payload["after_state"], {"cwd": "/opt/app"})
        self.assertEqual(payload["sections"], ["cwd"])

    def test_invalid_boolean_query_returns_json_400(self):
        with self.assertRaises(HTTPError) as captured:
            self._get(
                "/api/v0/sessions/historical-session/commands?include_io_summary=maybe"
            )

        self.assertEqual(captured.exception.code, 400)
        payload = _error_payload(captured.exception)
        self.assertEqual(payload["error"]["code"], "bad_request")

    def test_state_profiles_and_default_endpoints_return_structured_data(self):
        profile = create_state_profile(
            SessionState(
                username="alice",
                langfuse_trace_id="source",
                world_state=default_world_state(),
            ),
            name="api-default",
            level="system",
            comment="checkpoint",
            created_at="2026-06-15T12:00:00+00:00",
        )
        self.state_profile_runtime.store.save(profile)
        self.state_profile_runtime.set_default("api-default")

        profiles = self._get("/api/v0/state-profiles")
        default = self._get("/api/v0/state-profiles/default")
        detail = self._get("/api/v0/state-profiles/api-default")

        self.assertEqual(profiles[0]["name"], "api-default")
        self.assertEqual(profiles[0]["comment"], "checkpoint")
        self.assertEqual(default["name"], "api-default")
        self.assertEqual(default["level"], "system")
        self.assertEqual(detail["name"], "api-default")
        self.assertIn("world_state", detail["state"])

    def test_missing_state_profile_returns_json_404(self):
        with self.assertRaises(HTTPError) as captured:
            self._get("/api/v0/state-profiles/missing")

        self.assertEqual(captured.exception.code, 404)
        payload = _error_payload(captured.exception)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(payload["error"]["details"]["name"], "missing")

    def test_introspection_flush_requires_bearer_token(self):
        with self.assertRaises(HTTPError) as captured:
            self._post("/api/v0/introspection/flush", token=None)

        self.assertEqual(captured.exception.code, 401)
        self.assertEqual(self.introspection.flush_count, 0)

    def test_introspection_flush_calls_introspection(self):
        payload = self._post("/api/v0/introspection/flush")

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(self.introspection.flush_count, 1)

    def test_state_profile_control_endpoints_mutate_profiles(self):
        self.registry.register(
            SessionState(
                username="alice",
                login_username="alice",
                langfuse_trace_id="session-a",
                cwd="/opt/app",
                world_state=default_world_state(),
            )
        )

        saved = self._post_json(
            "/api/v0/state-profiles",
            {
                "name": "api-save",
                "level": "exact",
                "session_id": "session-a",
                "comment": "checkpoint",
            },
        )
        default = self._request_json(
            "PUT",
            "/api/v0/state-profiles/default",
            {"name": "api-save"},
        )
        applied = self._post_json(
            "/api/v0/sessions/session-a/state-profile",
            {"name": "api-save"},
        )
        cleared = self._request_json("DELETE", "/api/v0/state-profiles/default")
        deleted = self._request_json("DELETE", "/api/v0/state-profiles/api-save")

        self.assertEqual(saved["name"], "api-save")
        self.assertEqual(default["name"], "api-save")
        self.assertEqual(applied["session_id"], "session-a")
        self.assertEqual(cleared["previous"]["name"], "api-save")
        self.assertTrue(deleted["cleared_default"] is False)

    def test_session_spiritguide_endpoint_applies_update(self):
        session = SessionState(
            username="alice",
            login_username="alice",
            langfuse_trace_id="session-a",
            cwd="/home/alice",
        )
        self.registry.register(session)

        payload = self._post_json(
            "/api/v0/sessions/session-a/spiritguide",
            {"instruction": "move cwd"},
        )

        self.assertEqual(payload["message"], "Moved.")
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["patch_keys"], ["cwd"])
        self.assertEqual(session.cwd, "/srv/app")

    def test_spiritguide_profile_create_endpoint_saves_world_state(self):
        self.service.spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Factory world created.",
                state_update=CommandStateUpdate(
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
                    }
                ),
                metadata={"structured_response_accepted": True},
            )
        )

        payload = self._post_json(
            "/api/v0/spiritguide/profiles",
            {
                "name": "factory-lab",
                "instruction": "build a factory OT lab",
                "comment": "api seed",
            },
        )

        profile = self.state_profile_runtime.store.load("factory-lab")
        host = profile.state["world_state"]["hosts_by_name"]["ubuntu"]
        self.assertEqual(payload["name"], "factory-lab")
        self.assertEqual(payload["level"], "world")
        self.assertTrue(payload["saved"])
        self.assertEqual(payload["patch_keys"], ["world_state"])
        self.assertIn("plc-gateway", host["services_by_name"])
        self.assertIn(
            "/etc/factory/topology.json",
            host["filesystem"]["artifacts_by_path"],
        )

    def test_shutdown_endpoint_invokes_callback(self):
        payload = self._post("/api/v0/shutdown")

        self.assertEqual(payload["status"], "shutdown_requested")
        self.assertEqual(self.shutdown_count, 1)

    def test_mcp_requires_bearer_token(self):
        with self.assertRaises(HTTPError) as captured:
            self._mcp(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {},
                },
                token=None,
            )

        self.assertEqual(captured.exception.code, 401)

    def test_mcp_rejects_non_loopback_origin(self):
        with self.assertRaises(HTTPError) as captured:
            self._mcp(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {},
                },
                headers={"Origin": "http://evil.example"},
            )

        self.assertEqual(captured.exception.code, 403)

    def test_mcp_initialize_exposes_read_only_capabilities_by_default(self):
        payload = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            }
        )

        result = payload["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("resources", result["capabilities"])
        self.assertIn("tools", result["capabilities"])

    def test_mcp_lists_and_reads_resources(self):
        resources = self._mcp({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
        status = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "dreamland://status"},
            }
        )

        self.assertIn(
            "dreamland://status",
            [resource["uri"] for resource in resources["result"]["resources"]],
        )
        self.assertIn(
            "dreamland://agent-guide",
            [resource["uri"] for resource in resources["result"]["resources"]],
        )
        self.assertIn(
            "dreamland://config/effective",
            [resource["uri"] for resource in resources["result"]["resources"]],
        )
        self.assertIn(
            "dreamland://events",
            [resource["uri"] for resource in resources["result"]["resources"]],
        )
        content = status["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "text/plain")
        self.assertEqual(json.loads(content["text"])["live_session_count"], 0)

    def test_mcp_agent_guide_resource_documents_external_agent_workflow(self):
        payload = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/read",
                "params": {"uri": "dreamland://agent-guide"},
            }
        )

        text = payload["result"]["contents"][0]["text"]
        self.assertIn("external coding agents", text)
        self.assertIn("not part", text)
        self.assertIn("simulated command-agent prompt", text)
        self.assertIn("Never run !dreamland", text)
        self.assertIn("dreamland_session_run_command", text)
        self.assertIn("command_id", text)
        self.assertIn("dreamland_session_enrichment_events_read", text)
        self.assertIn("process-memory", text)
        self.assertIn("Langfuse MCP", text)

    def test_client_mcp_lists_only_generic_resources(self):
        resources = self._client_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
        )
        guide = self._client_mcp(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "client://guide"},
            }
        )

        resource_uris = {
            resource["uri"] for resource in resources["result"]["resources"]
        }
        text = guide["result"]["contents"][0]["text"]
        self.assertEqual(resource_uris, {"client://guide"})
        self.assertIn("client_session_create", text)
        self.assertIn("client_run_command", text)
        self.assertIn("client_session_exit", text)
        self.assertNotIn("DREAMLAND", text)
        self.assertNotIn("simulation", text.lower())
        self.assertNotIn("Langfuse", text)
        self.assertNotIn("telemetry", text.lower())

    def test_mcp_lists_and_reads_agent_workflow_prompts(self):
        payload = self._mcp({"jsonrpc": "2.0", "id": 1, "method": "prompts/list"})

        prompt_names = {prompt["name"] for prompt in payload["result"]["prompts"]}
        self.assertIn("dreamland_session_testing_workflow", prompt_names)
        self.assertIn("dreamland_session_lifecycle_workflow", prompt_names)
        self.assertIn("dreamland_command_diagnostics_workflow", prompt_names)

        testing = _mcp_prompt_text(
            self._mcp(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "prompts/get",
                    "params": {"name": "dreamland_session_testing_workflow"},
                }
            )
        )
        lifecycle = _mcp_prompt_text(
            self._mcp(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "prompts/get",
                    "params": {"name": "dreamland_session_lifecycle_workflow"},
                }
            )
        )
        diagnostics = _mcp_prompt_text(
            self._mcp(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "prompts/get",
                    "params": {"name": "dreamland_command_diagnostics_workflow"},
                }
            )
        )

        self.assertIn("dreamland://agent-guide", testing)
        self.assertIn("stdout, stderr, exit_status", testing)
        self.assertIn("Do not run !dreamland", testing)
        self.assertIn("dry_run=true", lifecycle)
        self.assertIn("direct command_id", diagnostics)
        self.assertIn("user-visible output", diagnostics)

    def test_mcp_reads_dynamic_session_resource(self):
        self.registry.register(
            SessionState(
                username="alice",
                login_username="alice",
                langfuse_trace_id="session-a",
                cwd="/opt/app",
            )
        )
        self.enrichment_event_logs.handle(
            _enrichment_record("session-a", "job-1", "enrichment.ready")
        )

        detail_payload = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/read",
                "params": {"uri": "dreamland://sessions/session-a"},
            }
        )
        payload = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "dreamland://sessions/session-a/state"},
            }
        )
        event_payload = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {"uri": "dreamland://sessions/session-a/events"},
            }
        )
        live_payload = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": "dreamland://live-sessions/session-a"},
            }
        )
        enrichment_payload = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "resources/read",
                "params": {"uri": "dreamland://sessions/session-a/enrichment-events"},
            }
        )

        detail = json.loads(detail_payload["result"]["contents"][0]["text"])
        state = json.loads(payload["result"]["contents"][0]["text"])
        event = json.loads(event_payload["result"]["contents"][0]["text"])
        live = json.loads(live_payload["result"]["contents"][0]["text"])
        enrichment = json.loads(enrichment_payload["result"]["contents"][0]["text"])
        self.assertEqual(detail["id"], "session-a")
        self.assertTrue(detail["live"])
        self.assertFalse(detail["langfuse"])
        self.assertTrue(detail["inspections"]["live_state"])
        self.assertEqual(state["session_id"], "session-a")
        self.assertEqual(state["state"]["cwd"], "/opt/app")
        self.assertEqual(event["endpoint"], "/api/v0/sessions/session-a/events")
        self.assertEqual(event["event_type"], "trace")
        self.assertEqual(live["session_id"], "session-a")
        self.assertEqual(enrichment["events"][0]["event"], "enrichment.ready")

    def test_mcp_reads_structured_failure_resources(self):
        capsule = _structured_failure_capsule()
        self.structured_failure_store.save(capsule)

        summaries_payload = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/read",
                "params": {"uri": "dreamland://structured-failures"},
            }
        )
        detail_payload = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": f"dreamland://structured-failures/{capsule.id}"},
            }
        )
        replay_payload = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {
                    "uri": (
                        f"dreamland://structured-failures/{capsule.id}/replay-parser"
                    )
                },
            }
        )

        summaries = json.loads(summaries_payload["result"]["contents"][0]["text"])
        detail = json.loads(detail_payload["result"]["contents"][0]["text"])
        replay = json.loads(replay_payload["result"]["contents"][0]["text"])
        self.assertEqual(summaries[0]["id"], capsule.id)
        self.assertEqual(detail["raw_response"], capsule.raw_response)
        self.assertFalse(replay["accepted"])
        self.assertEqual(replay["failure_class"], "invalid_json")

    def test_mcp_missing_resource_uses_json_rpc_error(self):
        payload = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/read",
                "params": {"uri": "dreamland://state-profiles/missing"},
            }
        )

        self.assertEqual(payload["error"]["code"], 0)
        self.assertIn("state profile not found", payload["error"]["message"])

    def test_mcp_write_tools_are_hidden_and_blocked_by_default(self):
        tools = self._mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        client_tools = self._client_mcp(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        call = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "dreamland_introspection_flush"},
            }
        )
        client_call = self._client_mcp(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "client_session_create"},
            }
        )

        self.assertEqual(tools["result"]["tools"], [])
        self.assertEqual(client_tools["result"]["tools"], [])
        self.assertTrue(call["result"]["isError"])
        self.assertIn(
            "Unknown tool: dreamland_introspection_flush",
            call["result"]["content"][0]["text"],
        )
        self.assertTrue(client_call["result"]["isError"])
        self.assertIn(
            "Unknown tool: client_session_create",
            client_call["result"]["content"][0]["text"],
        )
        self.assertEqual(self.introspection.flush_count, 0)

    def test_mcp_write_tools_can_be_enabled_explicitly(self):
        self._restart_api_with_mcp_write_tools()

        tools = self._mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        call = self._mcp(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "dreamland_introspection_flush"},
            }
        )

        self.assertEqual(
            tools["result"]["tools"][0]["name"], "dreamland_introspection_flush"
        )
        self.assertEqual(call["result"]["structuredContent"]["status"], "ok")
        self.assertEqual(self.introspection.flush_count, 1)

    def test_mcp_tool_descriptions_guide_agent_workflows(self):
        self._restart_api_with_mcp_write_tools()

        payload = self._mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        descriptions = {
            tool["name"]: tool.get("description", "")
            for tool in payload["result"]["tools"]
        }
        self.assertIn(
            "stdout, stderr, status, and command_id",
            descriptions["dreamland_session_run_command"],
        )
        self.assertIn(
            "direct command ids",
            descriptions["dreamland_session_commands_list"],
        )
        self.assertIn(
            "direct command_id",
            descriptions["dreamland_session_command_inspect"],
        )
        self.assertIn(
            "@ targets are for humans",
            descriptions["dreamland_session_command_inspect"],
        )
        self.assertIn(
            "exact ids with origin=mcp", descriptions["dreamland_sessions_close"]
        )
        self.assertIn("dry-run", descriptions["dreamland_sessions_close"])

    def test_mcp_state_profile_and_spiritguide_tools_can_be_enabled(self):
        self.registry.register(
            SessionState(
                username="alice",
                login_username="alice",
                langfuse_trace_id="session-a",
                cwd="/home/alice",
                world_state=default_world_state(),
            )
        )
        self._restart_api_with_mcp_write_tools()

        tools = self._mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        save = self._mcp_tool(
            2,
            "dreamland_state_profile_save",
            {
                "name": "mcp-save",
                "level": "exact",
                "session_id": "session-a",
                "comment": "checkpoint",
            },
        )
        apply = self._mcp_tool(
            3,
            "dreamland_state_profile_apply",
            {"name": "mcp-save", "session_id": "session-a"},
        )
        spiritguide = self._mcp_tool(
            4,
            "dreamland_spiritguide",
            {"session_id": "session-a", "instruction": "move cwd"},
        )

        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("dreamland_state_profile_save", tool_names)
        self.assertIn("dreamland_spiritguide", tool_names)
        self.assertEqual(_mcp_json_text(save)["name"], "mcp-save")
        self.assertEqual(_mcp_json_text(apply)["session_id"], "session-a")
        self.assertEqual(_mcp_json_text(spiritguide)["patch_keys"], ["cwd"])

    def test_mcp_evaluation_profile_config_and_detached_spiritguide(self):
        from dreamland.config import load_settings
        from dreamland.evaluation_runtime import EvaluationRuntimeFactory
        from tests.test_evaluation import RecordingAgent, RecordingGuide

        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(Path(self.tmpdir.name) / "absent.ini")
        self.service.evaluation_factory = EvaluationRuntimeFactory(
            settings,
            profile=self.service.command_backend.profile,
            state_profile_runtime=self.state_profile_runtime,
            session_registry=self.registry,
            structured_failure_store=self.structured_failure_store,
            api_key_resolver=lambda provider: None,
        )
        self.state_profile_runtime.store.save(
            create_state_profile(
                SessionState(
                    username="source",
                    langfuse_trace_id="source",
                    world_state=default_world_state(),
                ),
                name="mcp-evaluation",
                level="world",
            )
        )
        self._restart_api_with_mcp_write_tools()
        tools = self._mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        schemas = {
            tool["name"]: tool["inputSchema"]["properties"]
            for tool in tools["result"]["tools"]
        }
        self.assertIn("state_profile", schemas["dreamland_session_create"])
        self.assertIn("config_overrides", schemas["dreamland_session_create"])
        for operation in ("create", "update", "fork"):
            self.assertIn(
                "config_overrides",
                schemas[f"dreamland_spiritguide_profile_{operation}"],
            )
        with (
            patch(
                "dreamland.evaluation_runtime.LangGraphCommandAgent",
                side_effect=RecordingAgent,
            ),
            patch(
                "dreamland.evaluation_runtime.ModelSpiritguide",
                side_effect=RecordingGuide,
            ),
            patch("dreamland.evaluation_runtime.EnrichmentAgent.from_settings"),
        ):
            created = _mcp_json_text(
                self._mcp_tool(
                    2,
                    "dreamland_session_create",
                    {
                        "username": "alice",
                        "state_profile": "mcp-evaluation",
                        "tags": ["ab:test"],
                        "config_overrides": {
                            "agent": {"model": "anthropic:test-command"},
                            "enrichment": {"enabled": False},
                        },
                    },
                )
            )
            session_id = created["session_id"]
            self.addCleanup(self.registry.unregister, session_id)
            self.assertEqual(created["username"], "alice")
            self.assertEqual(created["state_profile"], "mcp-evaluation:world")
            self.assertEqual(
                created["evaluation_config"]["settings"]["agent"]["model"],
                "anthropic:test-command",
            )
            command = _mcp_json_text(
                self._mcp_tool(
                    3,
                    "dreamland_session_run_command",
                    {"session_id": session_id, "command": "probe"},
                )
            )
            self.assertEqual(command["stdout"], "test-command\n")
            self.assertEqual(command["exit_status"], 0)
            self._mcp_tool(
                4,
                "dreamland_state_profile_apply",
                {"name": "mcp-evaluation", "session_id": session_id},
            )
            closed = _mcp_json_text(
                self._mcp_tool(
                    5,
                    "dreamland_sessions_close",
                    {"session_ids": [session_id], "origin": "mcp", "dry_run": False},
                )
            )
            self.assertEqual(closed["closed"], [session_id])
            preview = _mcp_json_text(
                self._mcp_tool(
                    6,
                    "dreamland_spiritguide_profile_create",
                    {
                        "name": "preview",
                        "instruction": "build",
                        "dry_run": True,
                        "config_overrides": {
                            "spiritguide": {"model": "xai:test-guide"}
                        },
                    },
                )
            )
            self.assertFalse(preview["saved"])
            self.assertEqual(preview["message"], "xai:test-guide")
            rest_preview = self._post_json(
                "/api/v0/spiritguide/profiles",
                {
                    "name": "rest-preview",
                    "instruction": "build",
                    "dry_run": True,
                    "config_overrides": {"spiritguide": {"model": "openai:rest-guide"}},
                },
            )
            self.assertEqual(rest_preview["message"], "openai:rest-guide")

    def test_mcp_direct_command_options_and_standard_default_over_transport(self):
        from dreamland.config import load_settings
        from dreamland.evaluation_runtime import EvaluationRuntimeFactory
        from tests.test_evaluation import RecordingAgent, RecordingGuide

        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(Path(self.tmpdir.name) / "absent.ini")
        settings = replace(
            settings,
            agent=replace(settings.agent, openai_service_tier="fast"),
            enrichment=replace(settings.enrichment, enabled=False),
        )
        self.service.evaluation_factory = EvaluationRuntimeFactory(
            settings,
            profile=self.service.command_backend.profile,
            state_profile_runtime=self.state_profile_runtime,
            session_registry=self.registry,
            structured_failure_store=self.structured_failure_store,
            api_key_resolver=lambda provider: None,
        )
        self.enterContext(
            patch(
                "dreamland.evaluation_runtime.LangGraphCommandAgent",
                side_effect=RecordingAgent,
            )
        )
        self.enterContext(
            patch(
                "dreamland.evaluation_runtime.ModelSpiritguide",
                side_effect=RecordingGuide,
            )
        )
        self.enterContext(
            patch("dreamland.evaluation_runtime.EnrichmentAgent.from_settings")
        )
        self._restart_api_with_mcp_write_tools()
        tools = self._mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        schema = next(
            tool["inputSchema"]
            for tool in tools["result"]["tools"]
            if tool["name"] == "dreamland_session_create"
        )
        self.assertIn("model", schema["properties"])
        self.assertIn("openai_service_tier", schema["properties"])
        self.assertNotIn("model", schema.get("required", []))
        self.assertNotIn("openai_service_tier", schema.get("required", []))

        for arguments, expected_model, expected_tier in (
            ({}, settings.agent.model, "default"),
            ({"model": "openai:direct-model"}, "openai:direct-model", "default"),
            ({"openai_service_tier": "flex"}, settings.agent.model, "flex"),
            (
                {
                    "model": "openai:direct-model",
                    "openai_service_tier": "default",
                    "config_overrides": {"agent": {"reasoning_effort": "low"}},
                },
                "openai:direct-model",
                "default",
            ),
            (
                {
                    "config_overrides": {
                        "agent": {
                            "model": "openai:legacy-model",
                            "openai_service_tier": "fast",
                        }
                    }
                },
                "openai:legacy-model",
                "fast",
            ),
        ):
            with self.subTest(arguments=arguments):
                created = _mcp_json_text(
                    self._mcp_tool(2, "dreamland_session_create", arguments)
                )
                session_id = created["session_id"]
                self.addCleanup(self.registry.unregister, session_id)
                agent = created["evaluation_config"]["settings"]["agent"]
                self.assertEqual(agent["model"], expected_model)
                self.assertEqual(agent["openai_service_tier"], expected_tier)
                command = _mcp_json_text(
                    self._mcp_tool(
                        3,
                        "dreamland_session_run_command",
                        {"session_id": session_id, "command": "probe"},
                    )
                )
                self.assertEqual(
                    command["stdout"], expected_model.split(":", 1)[1] + "\n"
                )
                closed = _mcp_json_text(
                    self._mcp_tool(
                        4,
                        "dreamland_sessions_close",
                        {
                            "session_ids": [session_id],
                            "origin": "mcp",
                            "dry_run": False,
                        },
                    )
                )
                self.assertEqual(closed["closed"], [session_id])

        for arguments, message in (
            (
                {
                    "model": "openai:direct",
                    "config_overrides": {"agent": {"model": "openai:nested"}},
                },
                "model conflicts",
            ),
            (
                {
                    "openai_service_tier": "default",
                    "config_overrides": {"agent": {"openai_service_tier": "fast"}},
                },
                "openai_service_tier conflicts",
            ),
            ({"openai_service_tier": "invalid"}, "agent.openai_service_tier"),
        ):
            with self.subTest(arguments=arguments):
                result = self._mcp_tool(5, "dreamland_session_create", arguments)
                self.assertTrue(result["result"]["isError"])
                self.assertIn(message, result["result"]["content"][0]["text"])
                self.assertFalse(self.registry.list_sessions())

        client_tools = self._client_mcp(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/list"}
        )
        client_schema = next(
            tool["inputSchema"]
            for tool in client_tools["result"]["tools"]
            if tool["name"] == "client_session_create"
        )
        self.assertEqual(set(client_schema["properties"]), {"username"})
        created = self._client_mcp_tool(
            7, "client_session_create", {"username": "alice"}
        )["result"]["structuredContent"]
        self.addCleanup(self.registry.unregister, created["session_id"])
        runtime = self.registry.get(created["session_id"]).runtime
        self.assertEqual(runtime.backend.agent.kwargs["openai_service_tier"], "default")
        self.assertNotIn("evaluation_config", created)
        self.assertNotIn("model", created)
        closed = self._client_mcp_tool(
            8, "client_session_exit", {"session_id": created["session_id"]}
        )["result"]["structuredContent"]
        self.assertTrue(closed["closed"])
        self.assertIs(self.service.evaluation_factory.settings, settings)
        self.assertEqual(settings.agent.openai_service_tier, "fast")

    def test_mcp_parameterized_inspection_tools_can_be_enabled(self):
        self.logs.handle(_record("first"))
        self.logs.handle(_record("second"))
        capsule = _structured_failure_capsule()
        self.structured_failure_store.save(capsule)
        self._restart_api_with_mcp_write_tools()

        tools = self._mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        create = _mcp_json_text(
            self._mcp_tool(
                2,
                "dreamland_session_create",
                {
                    "username": "alice",
                    "tags": ["batch:a"],
                    "label": "run-a",
                },
            )
        )
        self.trace_logs.handle(_trace_record(create["session_id"], "input"))
        self.enrichment_event_logs.handle(
            _enrichment_record(create["session_id"], "job-1", "job.completed")
        )

        logs = _mcp_json_text(self._mcp_tool(3, "dreamland_logs_read", {"limit": 1}))
        live = _mcp_json_text(
            self._mcp_tool(
                4,
                "dreamland_live_sessions_list",
                {"tags": ["batch:a"], "origin": "mcp"},
            )
        )
        sessions = _mcp_json_text(
            self._mcp_tool(
                5,
                "dreamland_sessions_list",
                {"tags": ["batch:a"], "origin": "mcp"},
            )
        )
        groups = _mcp_json_text(
            self._mcp_tool(6, "dreamland_session_tag_groups", {"origin": "mcp"})
        )
        trace = _mcp_json_text(
            self._mcp_tool(
                7,
                "dreamland_session_trace_read",
                {"session_id": create["session_id"], "limit": 1},
            )
        )
        commands = _mcp_json_text(
            self._mcp_tool(
                8,
                "dreamland_session_commands_list",
                {
                    "session_id": "historical-session",
                    "filter_text": "who",
                    "include_io_summary": False,
                },
            )
        )
        inspection = _mcp_json_text(
            self._mcp_tool(
                9,
                "dreamland_session_command_inspect",
                {"session_id": "historical-session", "target": "cmd-1"},
            )
        )
        delta = _mcp_json_text(
            self._mcp_tool(
                10,
                "dreamland_session_state_delta",
                {
                    "session_id": "historical-session",
                    "to_target": "@-1",
                    "sections": ["cwd"],
                },
            )
        )
        failures = _mcp_json_text(
            self._mcp_tool(11, "dreamland_structured_failures_list", {})
        )
        failure = _mcp_json_text(
            self._mcp_tool(
                12,
                "dreamland_structured_failure_show",
                {"failure_id": capsule.id},
            )
        )
        failure_replay = _mcp_json_text(
            self._mcp_tool(
                13,
                "dreamland_structured_failure_replay_parser",
                {"failure_id": capsule.id},
            )
        )
        close_preview = _mcp_json_text(
            self._mcp_tool(
                14,
                "dreamland_sessions_close",
                {
                    "tags": ["batch:a"],
                    "label": "run-a",
                    "origin": "mcp",
                    "dry_run": True,
                },
            )
        )
        closed = _mcp_json_text(
            self._mcp_tool(
                15,
                "dreamland_sessions_close",
                {
                    "session_ids": [create["session_id"]],
                    "origin": "mcp",
                    "dry_run": False,
                },
            )
        )
        enrichment = _mcp_json_text(
            self._mcp_tool(
                16,
                "dreamland_session_enrichment_events_read",
                {
                    "session_id": create["session_id"],
                    "job_id": "job-1",
                    "limit": 10,
                },
            )
        )

        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("dreamland_logs_read", tool_names)
        self.assertIn("dreamland_session_enrichment_events_read", tool_names)
        self.assertIn("dreamland_session_command_inspect", tool_names)
        self.assertIn("dreamland_structured_failures_list", tool_names)
        self.assertIn("dreamland_sessions_close", tool_names)
        self.assertNotIn("dreamland_session_close", tool_names)
        self.assertNotIn("dreamland_mcp_sessions_close", tool_names)
        self.assertEqual([entry["text"] for entry in logs], ["second"])
        self.assertEqual(
            [session["session_id"] for session in live], [create["session_id"]]
        )
        self.assertEqual(
            [session["id"] for session in sessions["sessions"]], [create["session_id"]]
        )
        self.assertEqual({group["tag"] for group in groups}, {"batch:a", "mcp"})
        self.assertEqual([entry["text"] for entry in trace], ["input"])
        self.assertEqual(enrichment["events"][0]["event"], "job.completed")
        self.assertEqual(enrichment["events"][0]["job_id"], "job-1")
        self.assertEqual([command["command"] for command in commands], ["whoami"])
        self.assertEqual(inspection["command"]["id"], "cmd-1")
        self.assertEqual(inspection["detail"], "compact")
        self.assertIsNone(inspection["trace_output"])
        self.assertEqual(delta["after_state"], {"cwd": "/opt/app"})
        self.assertEqual(failures[0]["id"], capsule.id)
        self.assertEqual(failure["raw_response"], capsule.raw_response)
        self.assertFalse(failure_replay["accepted"])
        self.assertEqual(close_preview["matched"], 1)
        self.assertEqual(close_preview["closed"], [])
        self.assertTrue(close_preview["dry_run"])
        self.assertEqual(closed["closed"], [create["session_id"]])
        self.assertIsNone(self.registry.get(create["session_id"]))

    def test_mcp_session_tools_create_run_and_close_headless_session(self):
        self._restart_api_with_mcp_write_tools()

        create = self._mcp_tool(
            2,
            "dreamland_session_create",
            {
                "username": "alice",
                "tags": ["scenario:login"],
                "label": "agent-run-1",
                "metadata": {"agent": "codex"},
            },
        )
        created = _mcp_json_text(create)
        run = self._mcp_tool(
            3,
            "dreamland_session_run_command",
            {"session_id": created["session_id"], "command": "pwd"},
        )
        close = self._mcp_tool(
            4,
            "dreamland_sessions_close",
            {
                "session_ids": [created["session_id"]],
                "origin": "mcp",
                "dry_run": False,
            },
        )

        command = _mcp_json_text(run)
        closed = _mcp_json_text(close)
        self.assertEqual(created["tags"], ["mcp", "scenario:login"])
        self.assertEqual(created["label"], "agent-run-1")
        self.assertEqual(command["stdout"], "/home/alice\n")
        self.assertEqual(command["stderr"], "")
        self.assertIsNotNone(command["command_id"])
        self.assertEqual(command["exit_status"], 0)
        self.assertEqual(command["history_index"], 0)
        self.assertEqual(command["history_position"], 1)
        self.assertEqual(command["inspection_target"], "@-1")
        self.assertEqual(command["context"]["username"], "alice")
        self.assertEqual(command["context"]["hostname"], "ubuntu")
        self.assertEqual(command["context"]["cwd"], "/home/alice")
        self.assertEqual(command["context"]["active_program"], "bash")
        self.assertEqual(command["context"]["prompt"], "alice@ubuntu:~$ ")
        self.assertEqual(
            command["context"]["label"],
            "shell on alice@ubuntu:/home/alice",
        )
        self.assertEqual(closed["closed"], [created["session_id"]])
        self.assertEqual(closed["matched"], 1)
        self.assertIsNone(self.registry.get(created["session_id"]))

    def test_client_mcp_tools_create_run_and_exit_generic_session(self):
        self._restart_api_with_mcp_write_tools()

        tools = self._client_mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        create = self._client_mcp_tool(
            2,
            "client_session_create",
            {"username": "alice"},
        )
        created = create["result"]["structuredContent"]
        run = self._client_mcp_tool(
            3,
            "client_run_command",
            {"session_id": created["session_id"], "command": "pwd"},
        )
        command = run["result"]["structuredContent"]
        exit_result = self._client_mcp_tool(
            4,
            "client_session_exit",
            {"session_id": created["session_id"]},
        )["result"]["structuredContent"]

        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertEqual(
            tool_names,
            {
                "client_session_create",
                "client_run_command",
                "client_session_exit",
            },
        )
        self.assertEqual(created["username"], "alice")
        self.assertEqual(command["stdout"], "/home/alice\n")
        self.assertEqual(command["stderr"], "")
        self.assertEqual(command["exit_status"], 0)
        self.assertEqual(command["username"], "alice")
        self.assertEqual(command["hostname"], "ubuntu")
        self.assertEqual(command["cwd"], "/home/alice")
        self.assertEqual(command["active_program"], "bash")
        self.assertEqual(command["prompt"], "alice@ubuntu:~$ ")
        self.assertEqual(
            command["context"]["label"],
            "shell on alice@ubuntu:/home/alice",
        )
        self.assertNotIn("command_id", command)
        self.assertNotIn("history_index", command)
        self.assertNotIn("inspection_target", command)
        self.assertTrue(exit_result["closed"])
        self.assertIsNone(self.registry.get(created["session_id"]))

    def test_client_mcp_does_not_expose_admin_tools_or_close_admin_session(self):
        self._restart_api_with_mcp_write_tools()
        admin_created = _mcp_json_text(
            self._mcp_tool(
                2,
                "dreamland_session_create",
                {"username": "alice", "tags": ["admin-run"]},
            )
        )

        tools = self._client_mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        admin_tool_call = self._client_mcp_tool(
            4,
            "dreamland_sessions_close",
            {
                "session_ids": [admin_created["session_id"]],
                "origin": "mcp",
                "dry_run": False,
            },
        )
        client_exit = self._client_mcp_tool(
            5,
            "client_session_exit",
            {"session_id": admin_created["session_id"]},
        )["result"]["structuredContent"]

        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertNotIn("dreamland_sessions_close", tool_names)
        self.assertTrue(admin_tool_call["result"]["isError"])
        self.assertIn(
            "Unknown tool: dreamland_sessions_close",
            admin_tool_call["result"]["content"][0]["text"],
        )
        self.assertFalse(client_exit["closed"])
        self.assertIsNotNone(self.registry.get(admin_created["session_id"]))

    def test_client_mcp_sanitizes_forbidden_command_error(self):
        self._restart_api_with_mcp_write_tools()
        created = self._client_mcp_tool(
            2,
            "client_session_create",
            {"username": "alice"},
        )["result"]["structuredContent"]

        call = self._client_mcp_tool(
            3,
            "client_run_command",
            {
                "session_id": created["session_id"],
                "command": "!dreamland sessions",
            },
        )

        self.assertTrue(call["result"]["isError"])
        text = call["result"]["content"][0]["text"]
        self.assertIn("command is not available", text)
        self.assertNotIn("DREAMLAND", text)
        self.assertNotIn("dreamland", text)
        self.assertNotIn("MCP", text)

    def test_mcp_session_run_command_rejects_shell_console_prefix(self):
        self._restart_api_with_mcp_write_tools()
        created = _mcp_json_text(
            self._mcp_tool(2, "dreamland_session_create", {"username": "alice"})
        )

        call = self._mcp_tool(
            3,
            "dreamland_session_run_command",
            {
                "session_id": created["session_id"],
                "command": "!dreamland sessions",
            },
        )

        self.assertTrue(call["result"]["isError"])
        self.assertIn(
            "explicit MCP resources or tools",
            call["result"]["content"][0]["text"],
        )

    def test_mcp_does_not_expose_raw_console_command_tool(self):
        self._restart_api_with_mcp_write_tools()

        tools = self._mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        call = self._mcp_tool(
            2,
            "dreamland_console_command",
            {"command": "trace --count 1", "session_id": "session-a"},
        )
        old_close = self._mcp_tool(
            3,
            "dreamland_session_close",
            {"session_id": "session-a"},
        )

        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertNotIn("dreamland_console_command", tool_names)
        self.assertNotIn("dreamland_session_close", tool_names)
        self.assertNotIn("dreamland_mcp_sessions_close", tool_names)
        self.assertTrue(call["result"]["isError"])
        self.assertIn(
            "Unknown tool: dreamland_console_command",
            call["result"]["content"][0]["text"],
        )
        self.assertTrue(old_close["result"]["isError"])
        self.assertIn(
            "Unknown tool: dreamland_session_close",
            old_close["result"]["content"][0]["text"],
        )

    def test_server_rejects_non_loopback_host(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            DreamlandAdminAPIServer(
                settings=ApiSettings(
                    enabled=True,
                    host="0.0.0.0",
                    port=0,
                    token="test-token",
                ),
                service=self.server.service,
            )

    def _get(self, path, *, token="test-token"):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self._url(path), headers=headers)
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get_text(self, path, *, token="test-token"):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self._url(path), headers=headers)
        with urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")

    def _post(self, path, *, token="test-token"):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self._url(path), data=b"", headers=headers, method="POST")
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_json(self, path, payload, *, token="test-token"):
        return self._request_json("POST", path, payload, token=token)

    def _request_json(self, method, path, payload=None, *, token="test-token"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(self._url(path), data=data, headers=headers, method=method)
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _mcp(self, payload, *, token="test-token", headers=None):
        if (
            token is not None
            and self.mcp_session_id is None
            and payload.get("method") not in {"initialize", "notifications/initialized"}
        ):
            self._initialize_mcp_session(token=token)
        request_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if headers is not None:
            request_headers.update(headers)
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        if self.mcp_session_id is not None:
            request_headers["Mcp-Session-Id"] = self.mcp_session_id
            request_headers["MCP-Protocol-Version"] = "2025-06-18"
        request = Request(
            self._url("/mcp"),
            data=json.dumps(payload).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self.mcp_session_id = session_id
            body = response.read()
            if not body:
                return None
            return json.loads(body.decode("utf-8"))

    def _client_mcp(self, payload, *, token="test-token", headers=None):
        if (
            token is not None
            and self.client_mcp_session_id is None
            and payload.get("method") not in {"initialize", "notifications/initialized"}
        ):
            self._initialize_client_mcp_session(token=token)
        request_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if headers is not None:
            request_headers.update(headers)
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        if self.client_mcp_session_id is not None:
            request_headers["Mcp-Session-Id"] = self.client_mcp_session_id
            request_headers["MCP-Protocol-Version"] = "2025-06-18"
        request = Request(
            self._url("/client-mcp"),
            data=json.dumps(payload).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self.client_mcp_session_id = session_id
            body = response.read()
            if not body:
                return None
            return json.loads(body.decode("utf-8"))

    def _initialize_mcp_session(self, *, token="test-token"):
        self._mcp(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            token=token,
        )
        self._mcp(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
            token=token,
        )

    def _initialize_client_mcp_session(self, *, token="test-token"):
        self._client_mcp(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            token=token,
        )
        self._client_mcp(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
            token=token,
        )

    def _mcp_tool(self, request_id, name, arguments):
        return self._mcp(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    def _client_mcp_tool(self, request_id, name, arguments):
        return self._client_mcp(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    def _restart_api_with_mcp_write_tools(self):
        self.server.stop()
        self.service.mcp_write_tools = True
        self.mcp_session_id = None
        self.client_mcp_session_id = None
        self.server = self._new_server()
        self.server.start_in_thread()
        self.assertTrue(self.server.wait_until_ready())

    def _request_shutdown(self):
        self.shutdown_count += 1

    def _url(self, path):
        return f"http://127.0.0.1:{self.server.bound_port}{path}"

    def _new_server(self):
        return DreamlandAdminAPIServer(
            settings=ApiSettings(
                enabled=True,
                host="127.0.0.1",
                port=0,
                token="test-token",
            ),
            service=self.service,
        )


def _error_payload(error):
    return json.loads(error.read().decode("utf-8"))


def _mcp_json_text(payload):
    return json.loads(payload["result"]["content"][0]["text"])


def _mcp_prompt_text(payload):
    return payload["result"]["messages"][0]["content"]["text"]


def _sse_events(text):
    events = []
    for block in text.strip().split("\n\n"):
        if not block or block.startswith(":"):
            continue
        event = {}
        for line in block.splitlines():
            field, _, value = line.partition(": ")
            if field == "data":
                event[field] = json.loads(value)
            else:
                event[field] = value
        events.append(event)
    return events


class FakeIntrospection:
    def __init__(self):
        self.flush_count = 0

    def available(self):
        return True

    def flush(self):
        self.flush_count += 1

    def list_sessions(self, *, limit):
        return SessionList(
            sessions=[
                SessionSummary(
                    id="historical-session",
                    created_at=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
                )
            ],
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
        commands = [
            CommandSummary(
                id="cmd-1",
                session_id=session_id,
                timestamp=datetime(2026, 6, 19, 12, 1, tzinfo=UTC),
                command="whoami",
                total_tokens=42,
            ),
            CommandSummary(
                id="cmd-2",
                session_id=session_id,
                timestamp=datetime(2026, 6, 19, 12, 2, tzinfo=UTC),
                command="id",
            ),
        ]
        if filter_text:
            return [command for command in commands if filter_text in command.command]
        return commands

    def inspect_command(self, *, session_id, target, limit, detail="full"):
        command = self.list_commands(session_id=session_id, limit=limit)[0]
        trace_input = {"command": command.command} if detail == "full" else None
        trace_output = {"stdout": "alice\n"} if detail == "full" else None
        return CommandInspection(
            command=command,
            detail=detail,
            trace_input=trace_input,
            trace_output=trace_output,
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
        before_state = {"cwd": "/home/alice", "history": []}
        after_state = {"cwd": "/opt/app", "history": [{"command": "whoami"}]}
        if sections:
            before_state = {
                section: before_state[section]
                for section in sections
                if section in before_state
            }
            after_state = {
                section: after_state[section]
                for section in sections
                if section in after_state
            }
        return StateDelta(
            from_label="before cmd-1",
            to_label="after cmd-1",
            before_state=before_state,
            after_state=after_state,
            sections=tuple(sections),
        )


class FakeSpiritguide:
    def __init__(self, result):
        self.result = result

    def respond(self, context):
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
