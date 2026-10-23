import io
import json
import logging
import os
import select
import tempfile
import threading
import unittest
from pathlib import Path

from prompt_toolkit.document import Document

from dreamland.agent_structured import command_result_from_model_response
from dreamland.background_agents.enrichment.control import (
    EnrichmentJobSnapshot,
    EnrichmentRuntimeStatus,
    EnrichmentSubmission,
)
from dreamland.config import ConsoleSettings, LangfuseSettings
from dreamland.console import (
    CONSOLE_REFRESH_ENTER_SCREEN,
    CONSOLE_REFRESH_EXIT_SCREEN,
    ConsoleContext,
    ConsoleRefresh,
    DreamlandConsole,
    redraw_console_frame,
)
from dreamland.console_introspection import (
    CommandInspection,
    CommandSummary,
    IntrospectionUnavailable,
    LangfuseConsoleIntrospection,
    ObservationSummary,
    SessionList,
    SessionSummary,
    StateDelta,
    _reasoning_summary_from_observations,
    _resolve_command,
)
from dreamland.console_runtime import DreamlandCompleter, LocalConsole
from dreamland.lobby_catalog import LobbyCatalog
from dreamland.models import (
    CommandHistoryEntry,
    CommandStateUpdate,
    EnvironmentMetadata,
    HostIdentity,
    InteractiveProgram,
    SessionState,
    ShellEnvironment,
    ShellStackTransition,
)
from dreamland.runtime_logging import (
    TRACE_LOGGER,
    LogRecordBuffer,
    SessionTraceLogBuffer,
)
from dreamland.session_metadata import (
    SESSION_LABEL_METADATA_KEY,
    SESSION_ORIGIN_METADATA_KEY,
    SESSION_TAGS_METADATA_KEY,
)
from dreamland.spiritguide import SpiritguideResult
from dreamland.state_profiles import (
    STATE_PROFILE_METADATA_LEVEL,
    STATE_PROFILE_METADATA_NAME,
    StateProfile,
    StateProfileRuntime,
    StateProfileStore,
    create_state_profile,
)
from dreamland.structured_failures import (
    StructuredFailureStore,
    create_structured_failure_capsule,
)
from dreamland.terminal import CompletionLineEditor


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


class DreamlandConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.logs = LogRecordBuffer(3)
        self.logs.setFormatter(logging.Formatter("%(message)s"))
        self.trace_logs = SessionTraceLogBuffer(3)
        self.trace_logs.setFormatter(logging.Formatter("%(message)s"))
        self.shutdown_requested = False
        self.console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            trace_logs=self.trace_logs,
            introspection=FakeIntrospection(),
            structured_failure_store=StructuredFailureStore(Path(self.tmpdir.name)),
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_lobby_curation_persists_updates_and_lists_explicit_entry_username(self):
        root = Path(self.tmpdir.name)
        store = StateProfileStore(root / "profiles")
        store.save(
            StateProfile(
                "alpha",
                "world",
                {
                    "world_state": {
                        "entry_host": "ubuntu",
                        "active_host": "ubuntu",
                        "hosts_by_name": {
                            "ubuntu": {
                                "hostname": "ubuntu",
                                "login_banner": "Test console\n",
                            }
                        },
                    }
                },
                "2026-09-11",
            )
        )
        catalog = LobbyCatalog(store, root / "catalog.json")
        self.console.lobby_catalog = catalog

        missing = self.console.execute(
            'lobby worlds add alpha --title Alpha --description "An authored world"',
            allow_exit=False,
        )
        self.assertIn("--entry-username", missing.output)
        self.assertIn("missing", missing.output)
        self.assertEqual(catalog.snapshot().entries, ())

        added = self.console.execute(
            'lobby worlds add alpha --title Alpha --description "An authored world" '
            "--entry-username operator",
            allow_exit=False,
        )
        self.assertIn("entry_username", added.output)
        self.assertIn("operator", added.output)
        updated = self.console.execute(
            "lobby worlds update alpha --entry-username reader", allow_exit=False
        )
        self.assertIn("reader", updated.output)
        self.console.execute(
            'lobby worlds update alpha --title "New title"', allow_exit=False
        )
        listed = self.console.execute("lobby worlds list", allow_exit=False)
        self.assertIn("reader", listed.output)
        self.assertIn("New title", listed.output)
        self.assertEqual(
            LobbyCatalog(store, root / "catalog.json")
            .snapshot()
            .entries[0]
            .entry_username,
            "reader",
        )

        invalid = self.console.execute(
            'lobby worlds update alpha --entry-username "invalid name"',
            allow_exit=False,
        )
        self.assertIn("error:", invalid.output)
        self.assertEqual(catalog.snapshot().entries[0].entry_username, "reader")

    def test_lobby_add_without_entry_username_uses_saved_starting_account(self):
        root = Path(self.tmpdir.name)
        store = StateProfileStore(root / "profiles")
        store.save(
            StateProfile(
                "alpha",
                "world",
                {
                    "world_state": {
                        "entry_host": "terminal",
                        "active_host": "terminal",
                        "hosts_by_name": {
                            "terminal": {
                                "login_banner": "Test console\n",
                                "default_user": "operator",
                            }
                        },
                    }
                },
                "2026-10-07",
            )
        )
        self.console.lobby_catalog = LobbyCatalog(store, root / "catalog.json")

        added = self.console.execute(
            'lobby worlds add alpha --title Alpha --description "An authored world"',
            allow_exit=False,
        )

        self.assertNotIn("error:", added.output)
        self.assertIn("operator", added.output)
        self.assertEqual(
            LobbyCatalog(store, root / "catalog.json")
            .snapshot()
            .entries[0]
            .entry_username,
            "operator",
        )
        self.console.execute("lobby worlds remove alpha", allow_exit=False)
        override = self.console.execute(
            'lobby worlds add alpha --title Alpha --description "An authored world" '
            "--entry-username reader",
            allow_exit=False,
        )
        self.assertNotIn("error:", override.output)
        self.assertEqual(
            self.console.lobby_catalog.snapshot().entries[0].entry_username, "reader"
        )

    def test_logging_defaults_to_twenty_and_respects_ring_capacity(self):
        for message in ("one", "two", "three", "four"):
            self.logs.handle(_record(message))

        result = self.console.execute("logging", allow_exit=False)

        self.assertEqual(result.output, "two\nthree\nfour\n")

    def test_logging_count_and_follow_options(self):
        for message in ("one", "two", "three"):
            self.logs.handle(_record(message))

        result = self.console.execute("logging -n 1 -f", allow_exit=False)

        self.assertEqual(result.output, "three\n")
        self.assertTrue(result.follow)

    def test_trace_defaults_to_active_session(self):
        self.trace_logs.handle(_trace_record("session-a", "session-a input"))
        self.trace_logs.handle(_trace_record("session-b", "session-b input"))

        result = self.console.execute(
            "trace",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertEqual(result.output, "session-a input\n")
        self.assertFalse(result.follow)

    def test_trace_accepts_session_id_argument(self):
        self.trace_logs.handle(_trace_record("session-a", "session-a input"))
        self.trace_logs.handle(_trace_record("session-b", "session-b input"))

        result = self.console.execute(
            "trace session-b",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertEqual(result.output, "session-b input\n")

    def test_trace_count_and_follow_options(self):
        for message in ("one", "two", "three"):
            self.trace_logs.handle(_trace_record("session-a", message))

        result = self.console.execute(
            "trace -n 1 -f",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertEqual(result.output, "three\n")
        self.assertTrue(result.follow)
        self.assertEqual(result.follow_trace_session_id, "session-a")

    def test_trace_requires_active_session_or_argument(self):
        result = self.console.execute("trace", allow_exit=False)

        self.assertIn("no active session", result.output)

    def test_logging_does_not_show_session_trace_records(self):
        self.logs.handle(_record("system-log"))
        self.trace_logs.handle(_trace_record("session-a", "trace-log"))

        result = self.console.execute("logging", allow_exit=False)

        self.assertIn("system-log", result.output)
        self.assertNotIn("trace-log", result.output)

    def test_execute_accepts_optional_dreamland_prefix(self):
        self.logs.handle(_record("latest"))

        result = self.console.execute(
            "!dreamland logging -n 1",
            allow_exit=False,
        )

        self.assertEqual(result.output, "latest\n")

    def test_help_is_available_globally_and_per_command(self):
        self.assertIn(
            "Introspect and control",
            self.console.execute("--help", allow_exit=False).output,
        )
        self.assertIn(
            "--follow",
            self.console.execute("logging --help", allow_exit=False).output,
        )

    def test_shutdown_requests_process_stop(self):
        result = self.console.execute("shutdown", allow_exit=False)

        self.assertTrue(result.shutdown)
        self.assertTrue(self.shutdown_requested)
        self.assertTrue(self.console.shutdown_requested)

    def test_failures_list_show_and_replay_parser(self):
        capsule = _structured_failure_capsule()
        self.console.structured_failures.save(capsule)

        listing = self.console.execute("failures list", allow_exit=False)
        shown = self.console.execute(f"failures show {capsule.id}", allow_exit=False)
        replay = self.console.execute(
            f"failures replay-parser {capsule.id}",
            allow_exit=False,
        )

        self.assertIn(capsule.id, listing.output)
        self.assertIn("invalid_json", listing.output)
        self.assertIn("parser_error: invalid JSON", shown.output)
        self.assertIn("raw_response_chars", shown.output)
        self.assertIn("accepted: no", replay.output)
        self.assertIn("failure_class: invalid_json", replay.output)

    def test_failures_show_json_outputs_capsule_document(self):
        capsule = _structured_failure_capsule()
        self.console.structured_failures.save(capsule)

        result = self.console.execute(
            f"failures show {capsule.id} --json",
            allow_exit=False,
        )

        payload = json.loads(result.output)
        self.assertEqual(payload["id"], capsule.id)
        self.assertEqual(payload["kind"], "dreamland.structured_failure_capsule")

    def test_sessions_lists_and_sets_active_session(self):
        context = ConsoleContext()

        listing = self.console.execute("sessions", allow_exit=False, context=context)
        selected = self.console.execute(
            "sessions --set session-b",
            allow_exit=False,
            context=context,
        )
        relisting = self.console.execute("sessions", allow_exit=False, context=context)

        self.assertIn("2 total", listing.output)
        self.assertIn("session-a", listing.output)
        self.assertIn("Active session set to session-b", selected.output)
        self.assertEqual(context.active_session_id, "session-b")
        self.assertIn("*", relisting.output)
        self.assertIn("profile", listing.output)

    def test_sessions_list_shows_live_state_profile(self):
        session = SessionState(
            username="alice",
            langfuse_trace_id="session-a",
            environment_metadata=EnvironmentMetadata(
                metadata={
                    STATE_PROFILE_METADATA_NAME: "manual-system",
                    STATE_PROFILE_METADATA_LEVEL: "system",
                }
            ),
        )
        self.console.session_registry.register(session)

        result = self.console.execute("sessions", allow_exit=False)

        self.assertIn("manual-system:system", result.output)

    def test_sessions_live_lists_tags_labels_and_origin(self):
        session = SessionState(
            username="alice",
            langfuse_trace_id="session-a",
            cwd="/srv/app",
            environment_metadata=EnvironmentMetadata(
                metadata={
                    SESSION_TAGS_METADATA_KEY: ["mcp", "scenario:login"],
                    SESSION_LABEL_METADATA_KEY: "run-a",
                    SESSION_ORIGIN_METADATA_KEY: "mcp",
                }
            ),
            history=[CommandHistoryEntry(cwd="/srv/app", command="pwd")],
        )
        self.console.session_registry.register(session)

        result = self.console.execute(
            "sessions --live --tag scenario:login", allow_exit=False
        )

        self.assertIn("session-a", result.output)
        self.assertIn("run-a", result.output)
        self.assertIn("mcp,scenario:login", result.output)
        self.assertIn("/srv/app", result.output)

    def test_sessions_close_matching_supports_dry_run_and_filters(self):
        first = SessionState(
            username="alice",
            langfuse_trace_id="session-a",
            environment_metadata=EnvironmentMetadata(
                metadata={
                    SESSION_TAGS_METADATA_KEY: ["mcp", "batch:a"],
                    SESSION_LABEL_METADATA_KEY: "run-a",
                    SESSION_ORIGIN_METADATA_KEY: "mcp",
                }
            ),
        )
        second = SessionState(
            username="bob",
            langfuse_trace_id="session-b",
            environment_metadata=EnvironmentMetadata(
                metadata={
                    SESSION_TAGS_METADATA_KEY: ["mcp", "batch:b"],
                    SESSION_LABEL_METADATA_KEY: "run-b",
                    SESSION_ORIGIN_METADATA_KEY: "mcp",
                }
            ),
        )
        self.console.session_registry.register(first)
        self.console.session_registry.register(second)

        preview = self.console.execute(
            "sessions --close-matching --tag batch:a --origin mcp --dry-run",
            allow_exit=False,
        )
        self.assertIn("Would close 1 live session", preview.output)
        self.assertIsNotNone(self.console.session_registry.get("session-a"))

        closed = self.console.execute(
            "sessions --close-matching --tag batch:a --origin mcp",
            allow_exit=False,
        )

        self.assertIn("Closed 1 live session", closed.output)
        self.assertIsNone(self.console.session_registry.get("session-a"))
        self.assertIsNotNone(self.console.session_registry.get("session-b"))

    def test_sessions_list_shows_indexed_state_profile_for_inactive_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            profile = create_state_profile(
                SessionState(
                    username="alice",
                    langfuse_trace_id="source-session",
                    world_state=_profile_world(),
                ),
                name="manual-system",
                level="system",
                created_at="2026-06-15T12:00:00+00:00",
            )
            store.record_session_profile(
                session_id="session-a",
                profile=profile,
                source="apply",
                updated_at="2026-06-16T06:20:00+00:00",
            )
            console = DreamlandConsole(
                ConsoleSettings(),
                self.logs,
                self._request_shutdown,
                introspection=FakeIntrospection(session_ids=["session-a"]),
                state_profiles=store,
            )

            result = console.execute("sessions", allow_exit=False)

        self.assertIn("manual-system:system", result.output)

    def test_session_alias_matches_sessions_command(self):
        context = ConsoleContext()

        result = self.console.execute(
            "session --set session-a",
            allow_exit=False,
            context=context,
        )

        self.assertIn("Active session set to session-a", result.output)
        self.assertEqual(context.active_session_id, "session-a")

    def test_short_session_prefix_resolves_from_recent_sessions(self):
        full_session_id = "b203ff05cdea4acda41f88e750b06fc9"
        introspection = FakeIntrospection(session_ids=[full_session_id])
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            introspection=introspection,
        )
        context = ConsoleContext()

        console.execute("sessions", allow_exit=False, context=context)
        selected = console.execute(
            "session --set b203ff05cdea",
            allow_exit=False,
            context=context,
        )
        console.execute("commands", allow_exit=False, context=context)

        self.assertIn(full_session_id, selected.output)
        self.assertEqual(context.active_session_id, full_session_id)
        self.assertEqual(introspection.command_sessions, [full_session_id])

    def test_sessions_table_renders_full_session_id(self):
        full_session_id = "b203ff05cdea4acda41f88e750b06fc9"
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            introspection=FakeIntrospection(session_ids=[full_session_id]),
        )

        result = console.execute("sessions", allow_exit=False)

        self.assertIn(full_session_id, result.output)

    def test_commands_requires_active_session(self):
        result = self.console.execute("commands", allow_exit=False)

        self.assertIn("no active session", result.output)

    def test_langfuse_commands_are_disabled_without_configuration(self):
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
        )
        context = ConsoleContext(active_session_id="session-a")

        result = console.execute("commands", allow_exit=False, context=context)

        self.assertIn("Langfuse introspection is disabled", result.output)

    def test_commands_uses_active_session_and_supports_flush(self):
        introspection = FakeIntrospection()
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            introspection=introspection,
        )
        context = ConsoleContext(active_session_id="session-a")

        result = console.execute(
            "commands --flush --columns pos,id,command",
            allow_exit=False,
            context=context,
        )

        self.assertEqual(introspection.flush_count, 1)
        self.assertEqual(introspection.command_sessions, ["session-a"])
        self.assertIn("id-command-1", result.output)
        self.assertIn("whoami", result.output)

    def test_commands_default_columns_show_reasoning_and_summary_flags(self):
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            introspection=FakeIntrospection(),
        )
        context = ConsoleContext(active_session_id="session-a")

        result = console.execute("commands", allow_exit=False, context=context)

        self.assertIn("reasoning", result.output)
        self.assertIn("summary", result.output)

    def test_commands_handles_empty_session(self):
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            introspection=EmptyIntrospection(),
        )
        context = ConsoleContext(active_session_id="session-empty")

        result = console.execute("commands", allow_exit=False, context=context)

        self.assertIn("pos", result.output)
        self.assertIn("command", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_inspection_defaults_to_previous_command_and_prints_multiline_strings(self):
        context = ConsoleContext(active_session_id="session-a")

        result = self.console.execute("inspection", allow_exit=False, context=context)

        self.assertIn("=== MODEL INPUT ===", result.output)
        self.assertIn("=== MODEL OUTPUT ===", result.output)
        self.assertIn("line one", result.output)
        self.assertIn("line two", result.output)
        self.assertNotIn("\\nline two", result.output)
        self.assertNotIn("=== COST ===", result.output)
        self.assertNotIn("=== TOKENS ===", result.output)
        self.assertNotIn("=== TIMING ===", result.output)

    def test_inspection_can_show_cost_tokens_timing_and_metadata(self):
        context = ConsoleContext(active_session_id="session-a")

        result = self.console.execute(
            "inspection --cost --tokens --timing --metadata",
            allow_exit=False,
            context=context,
        )

        self.assertIn("=== METADATA ===", result.output)
        self.assertIn("=== COST ===", result.output)
        self.assertIn("$0.001000", result.output)
        self.assertIn("=== TOKENS ===", result.output)
        self.assertIn("total_tokens", result.output)
        self.assertIn("42", result.output)
        self.assertIn("=== TIMING ===", result.output)
        self.assertIn("0.500s", result.output)

    def test_inspect_alias_routes_to_inspection_command(self):
        context = ConsoleContext(active_session_id="session-a")

        default_result = self.console.execute(
            "inspect",
            allow_exit=False,
            context=context,
        )
        relative_result = self.console.execute(
            "inspect @-1",
            allow_exit=False,
            context=context,
        )

        self.assertIn("=== MODEL INPUT ===", default_result.output)
        self.assertIn("=== MODEL INPUT ===", relative_result.output)

    def test_state_delta_routes_to_active_session(self):
        context = ConsoleContext(active_session_id="session-a")

        result = self.console.execute(
            "state-delta @-2 @-1 --section cwd",
            allow_exit=False,
            context=context,
        )

        self.assertIn("--- after id-command-2", result.output)
        self.assertIn("+++ after id-command-1", result.output)
        self.assertIn('-  "cwd": "/home/user"', result.output)
        self.assertIn('+  "cwd": "/tmp"', result.output)

    def test_state_delta_single_target_shows_before_and_after(self):
        context = ConsoleContext(active_session_id="session-a")

        result = self.console.execute(
            "delta @-1",
            allow_exit=False,
            context=context,
        )

        self.assertIn("--- before id-command-1", result.output)
        self.assertIn("+++ after id-command-1", result.output)

    def test_state_delta_rejects_too_many_targets(self):
        context = ConsoleContext(active_session_id="session-a")

        result = self.console.execute(
            "state-delta @-3 @-2 @-1",
            allow_exit=False,
            context=context,
        )

        self.assertIn("state-delta expects one target or two targets", result.output)

    def test_inspection_reasoning_prints_only_summary(self):
        context = ConsoleContext(active_session_id="session-a")

        result = self.console.execute(
            "inspect --reasoning",
            allow_exit=False,
            context=context,
        )

        self.assertIn("=== REASONING SUMMARY ===", result.output)
        self.assertIn("Checked the command context.", result.output)
        self.assertNotIn("=== MODEL INPUT ===", result.output)
        self.assertNotIn("=== MODEL OUTPUT ===", result.output)

    def test_inspection_target_resolver_uses_at_relative_positions(self):
        commands = [
            CommandSummary(
                id="command-newest",
                session_id="session-a",
                timestamp="2026-06-08T01:00:00Z",
                command="id",
            ),
            CommandSummary(
                id="command-previous",
                session_id="session-a",
                timestamp="2026-06-08T00:59:00Z",
                command="whoami",
            ),
        ]

        self.assertEqual(_resolve_command(commands, None).id, "command-newest")
        self.assertEqual(_resolve_command(commands, "@-2").id, "command-previous")
        self.assertEqual(
            _resolve_command(commands, "command-previous").id,
            "command-previous",
        )
        with self.assertRaises(IntrospectionUnavailable):
            _resolve_command(commands, "-1")

    def test_inspect_rejects_session_option(self):
        result = self.console.execute(
            "inspect --session session-a",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertIn("unrecognized arguments: --session", result.output)

    def test_langfuse_flush_command_flushes_without_querying(self):
        introspection = FakeIntrospection()
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            introspection=introspection,
        )

        result = console.execute("langfuse flush", allow_exit=False)

        self.assertEqual(introspection.flush_count, 1)
        self.assertIn("Langfuse flush complete", result.output)

    def test_state_profile_saves_active_live_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            console = DreamlandConsole(
                ConsoleSettings(),
                self.logs,
                self._request_shutdown,
                introspection=FakeIntrospection(),
                state_profiles=StateProfileStore(Path(tmpdir)),
            )
            session = SessionState(
                username="alice",
                langfuse_trace_id="session-a",
                world_state=_profile_world(
                    services={"redis": {"description": "Local cache service."}}
                ),
                history=[
                    CommandHistoryEntry(
                        cwd="/home/alice",
                        command="ls /opt",
                        stdout="telemetry\n",
                    )
                ],
            )
            console.session_registry.register(session)

            result = console.execute(
                "state-profile save lab-a --level exact --comment checkpoint",
                allow_exit=False,
                context=ConsoleContext(active_session_id="session-a"),
            )
            listing = console.execute("state-profile list", allow_exit=False)
            shown = console.execute("state-profile show lab-a", allow_exit=False)
            shown_json = console.execute(
                "state-profile show lab-a --json",
                allow_exit=False,
            )

        self.assertIn("Saved state profile 'lab-a' (exact)", result.output)
        self.assertIn("lab-a  level=exact", listing.output)
        self.assertIn("checkpoint", listing.output)
        self.assertIn("state_sections:", shown.output)
        self.assertIn("history", shown.output)
        self.assertIn('"name": "lab-a"', shown_json.output)
        self.assertIn('"command": "ls /opt"', shown_json.output)

    def test_state_profile_save_can_use_only_live_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            console = DreamlandConsole(
                ConsoleSettings(),
                self.logs,
                self._request_shutdown,
                state_profiles=StateProfileStore(Path(tmpdir)),
            )
            session = SessionState(
                username="alice",
                langfuse_trace_id="session-a",
                world_state=_profile_world(),
            )
            console.session_registry.register(session)

            result = console.execute(
                "state-profile save only-session --level system",
                allow_exit=False,
            )

        self.assertIn("Saved state profile 'only-session' (system)", result.output)

    def test_state_profile_save_requires_live_session(self):
        no_active = self.console.execute(
            "state-profile save missing",
            allow_exit=False,
        )
        not_live = self.console.execute(
            "state-profile save missing",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertIn("no active session", no_active.output)
        self.assertIn("is not live", not_live.output)

    def test_state_profile_show_reports_missing_profile(self):
        result = self.console.execute(
            "state-profile show missing",
            allow_exit=False,
        )

        self.assertIn("does not exist", result.output)

    def test_state_profile_apply_replaces_active_live_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            source = SessionState(
                username="alice",
                login_username="alice",
                langfuse_trace_id="source-session",
                world_state=_profile_world(
                    services={"redis": {"description": "Local cache service."}}
                ),
                history=[
                    CommandHistoryEntry(
                        cwd="/home/alice",
                        command="ls /opt",
                        stdout="telemetry\n",
                    )
                ],
            )
            store.save(
                create_state_profile(
                    source,
                    name="restore-point",
                    level="exact",
                    created_at="2026-06-15T12:00:00+00:00",
                )
            )
            console = DreamlandConsole(
                ConsoleSettings(),
                self.logs,
                self._request_shutdown,
                state_profiles=store,
            )
            target = SessionState(
                username="bob",
                login_username="bob",
                langfuse_trace_id="target-session",
                world_state={"hosts_by_name": {"ubuntu": {"services_by_name": {}}}},
            )
            console.session_registry.register(target)

            result = console.execute(
                "state-profile apply restore-point",
                allow_exit=False,
                context=ConsoleContext(active_session_id="target-session"),
            )
            indexed = store.session_profile("target-session")

        self.assertIn(
            "Applied state profile 'restore-point' (exact) to session target-session",
            result.output,
        )
        self.assertEqual(target.langfuse_trace_id, "target-session")
        self.assertEqual(target.username, "alice")
        self.assertEqual(target.login_username, "bob")
        self.assertEqual(target.history[0].command, "ls /opt")
        self.assertEqual(
            target.world_state["hosts_by_name"]["ubuntu"]["services_by_name"]["redis"],
            {"description": "Local cache service."},
        )
        self.assertEqual(indexed.label, "restore-point:exact")
        self.assertEqual(indexed.source, "apply")
        self.assertTrue(target.force_session_state_next_command)

    def test_state_profile_apply_records_session_trace_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            source = SessionState(
                username="alice",
                langfuse_trace_id="source-session",
                world_state=_profile_world(),
            )
            store.save(
                create_state_profile(
                    source,
                    name="restore-point",
                    level="system",
                    created_at="2026-06-15T12:00:00+00:00",
                )
            )
            console = DreamlandConsole(
                ConsoleSettings(),
                self.logs,
                self._request_shutdown,
                trace_logs=self.trace_logs,
                state_profiles=store,
            )
            target = SessionState(username="bob", langfuse_trace_id="target-session")
            console.session_registry.register(target)
            context = ConsoleContext(active_session_id="target-session")

            old_level = TRACE_LOGGER.level
            TRACE_LOGGER.addHandler(self.trace_logs)
            TRACE_LOGGER.setLevel(logging.INFO)
            try:
                console.execute(
                    "state-profile apply restore-point",
                    allow_exit=False,
                    context=context,
                )
            finally:
                TRACE_LOGGER.removeHandler(self.trace_logs)
                TRACE_LOGGER.setLevel(old_level)
            result = console.execute("trace", allow_exit=False, context=context)

        self.assertIn("type=state_profile_apply", result.output)
        self.assertIn("name='restore-point'", result.output)
        self.assertIn("level=system", result.output)
        self.assertIn("source_session=source-session", result.output)

    def test_state_profile_apply_accepts_explicit_live_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            store.save(
                create_state_profile(
                    SessionState(
                        username="alice",
                        langfuse_trace_id="source-session",
                        world_state=_profile_world(),
                    ),
                    name="restore-point",
                    level="system",
                    created_at="2026-06-15T12:00:00+00:00",
                )
            )
            console = DreamlandConsole(
                ConsoleSettings(),
                self.logs,
                self._request_shutdown,
                state_profiles=store,
            )
            target = SessionState(username="bob", langfuse_trace_id="target-session")
            console.session_registry.register(target)

            result = console.execute(
                "state-profile apply restore-point --session target-session",
                allow_exit=False,
            )

        self.assertIn("target-session", result.output)
        self.assertEqual(target.username, "bob")

    def test_state_profile_apply_requires_live_session(self):
        no_active = self.console.execute(
            "state-profile apply missing",
            allow_exit=False,
        )
        not_live = self.console.execute(
            "state-profile apply missing",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertIn("no active session", no_active.output)
        self.assertIn("is not live", not_live.output)

    def test_state_profile_default_can_be_set_shown_and_cleared(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            store.save(
                create_state_profile(
                    SessionState(
                        username="alice",
                        langfuse_trace_id="source-session",
                        world_state=_profile_world(),
                    ),
                    name="runtime-default",
                    level="system",
                    created_at="2026-06-15T12:00:00+00:00",
                )
            )
            runtime = StateProfileRuntime(store=store)
            console = DreamlandConsole(
                ConsoleSettings(),
                self.logs,
                self._request_shutdown,
                state_profile_runtime=runtime,
            )

            missing = console.execute("state-profile default", allow_exit=False)
            selected = console.execute(
                "state-profile default set runtime-default",
                allow_exit=False,
            )
            shown = console.execute("state-profile default", allow_exit=False)
            cleared = console.execute(
                "state-profile default clear",
                allow_exit=False,
            )

        self.assertIn("No default state profile set", missing.output)
        self.assertIn("Default state profile set to 'runtime-default'", selected.output)
        self.assertIn("Default state profile: runtime-default (system)", shown.output)
        self.assertIn("Default state profile cleared", cleared.output)
        self.assertIsNone(runtime.current_profile())

    def test_state_profile_default_set_can_persist_to_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir) / "profiles")
            store.save(
                create_state_profile(
                    SessionState(
                        username="alice",
                        langfuse_trace_id="source-session",
                        world_state=_profile_world(),
                    ),
                    name="runtime-default",
                    level="system",
                    created_at="2026-06-15T12:00:00+00:00",
                )
            )
            config_path = Path(tmpdir) / "dreamland.ini"
            config_path.write_text(
                "[agent]\nmodel = openai:gpt-test\n",
                encoding="utf-8",
            )
            console = DreamlandConsole(
                ConsoleSettings(),
                self.logs,
                self._request_shutdown,
                state_profile_runtime=StateProfileRuntime(store=store),
                config_path=str(config_path),
            )

            selected = console.execute(
                "state-profile default set runtime-default --persist",
                allow_exit=False,
            )
            cleared = console.execute(
                "state-profile default clear --persist",
                allow_exit=False,
            )
            persisted_text = config_path.read_text(encoding="utf-8")

        self.assertIn("and persisted", selected.output)
        self.assertIn("and persisted", cleared.output)
        self.assertIn("[state_profile]", persisted_text)
        self.assertIn("profile = ", persisted_text)

    def test_state_profile_default_set_requires_name(self):
        result = self.console.execute(
            "state-profile default set",
            allow_exit=False,
        )

        self.assertIn("requires NAME", result.output)

    def test_state_profile_delete_removes_saved_profile_and_clears_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            store.save(
                create_state_profile(
                    SessionState(
                        username="alice",
                        langfuse_trace_id="source-session",
                        world_state=_profile_world(),
                    ),
                    name="runtime-default",
                    level="system",
                    created_at="2026-06-15T12:00:00+00:00",
                )
            )
            runtime = StateProfileRuntime(store=store)
            runtime.set_default("runtime-default")
            console = DreamlandConsole(
                ConsoleSettings(),
                self.logs,
                self._request_shutdown,
                state_profile_runtime=runtime,
            )

            result = console.execute(
                "state-profile delete runtime-default",
                allow_exit=False,
            )

        self.assertIn("Deleted state profile 'runtime-default'", result.output)
        self.assertIn("Default state profile cleared", result.output)
        self.assertIsNone(runtime.current_profile())
        self.assertEqual(store.list(), [])

    def test_state_profile_delete_reports_missing_profile(self):
        result = self.console.execute(
            "state-profile delete missing",
            allow_exit=False,
        )

        self.assertIn("does not exist", result.output)

    def test_spiritguide_applies_state_update_to_active_live_session(self):
        spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Redis service added.",
                state_update=CommandStateUpdate(
                    world_state={
                        "hosts_by_name": {
                            "ubuntu": {
                                "services_by_name": {
                                    "redis": {
                                        "state": "running",
                                        "listen": "127.0.0.1:6379",
                                    }
                                }
                            }
                        }
                    }
                ),
                metadata={"structured_response_accepted": True},
            )
        )
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            introspection=FakeIntrospection(),
            spiritguide=spiritguide,
        )
        session = SessionState(username="alice", langfuse_trace_id="session-a")
        console.session_registry.register(session)

        result = console.execute(
            "spiritguide add redis",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertIn("Redis service added.", result.output)
        self.assertIn("state_update: world_state", result.output)
        self.assertIn("applied: yes", result.output)
        self.assertIn("structured_response: accepted", result.output)
        self.assertEqual(spiritguide.instructions, ["add redis"])
        self.assertEqual(
            session.world_state["hosts_by_name"]["ubuntu"]["services_by_name"]["redis"],
            {"state": "running", "listen": "127.0.0.1:6379"},
        )

    def test_spiritguide_applies_shell_state_update_to_active_live_session(self):
        shell_environment = ShellEnvironment(
            variables={
                "HOME": "/root",
                "HOSTNAME": "jump-01",
                "LOGNAME": "root",
                "PATH": "/usr/bin:/bin",
                "PWD": "/root",
                "SHELL": "/bin/bash",
                "USER": "root",
            },
            exported=frozenset(
                {"HOME", "HOSTNAME", "LOGNAME", "PATH", "PWD", "SHELL", "USER"}
            ),
        )
        spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Moved into the root maintenance shell.",
                state_update=CommandStateUpdate(
                    cwd="/root",
                    interactive_program=InteractiveProgram(
                        name="bash",
                        command="/bin/bash",
                        argv=("bash",),
                    ),
                    shell_environment=shell_environment,
                    shell_stack_transition=ShellStackTransition(
                        operation="push",
                        username="root",
                        metadata={"source": "spiritguide"},
                    ),
                ),
                metadata={"structured_response_accepted": True},
            )
        )
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            introspection=FakeIntrospection(),
            spiritguide=spiritguide,
        )
        session = SessionState(username="alice", langfuse_trace_id="session-a")
        console.session_registry.register(session)

        result = console.execute(
            "spiritguide enter maintenance shell",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertIn("Moved into the root maintenance shell.", result.output)
        self.assertIn(
            "state_update: cwd, interactive_program, shell_environment, "
            "shell_stack_transition",
            result.output,
        )
        self.assertIn("applied: yes", result.output)
        self.assertEqual(session.username, "root")
        self.assertEqual(session.cwd, "/root")
        self.assertEqual(session.interactive_program.name, "bash")
        self.assertEqual(session.shell_environment.variables["USER"], "root")
        self.assertEqual(session.shell_environment.variables["PWD"], "/root")
        self.assertEqual(len(session.shell_stack), 2)
        self.assertEqual(session.shell_stack[-1].username, "root")
        self.assertEqual(session.shell_stack[-1].cwd, "/root")
        self.assertEqual(
            session.shell_stack[-1].shell_environment.variables["USER"],
            "root",
        )
        self.assertEqual(session.shell_stack[-1].metadata["source"], "spiritguide")
        self.assertTrue(session.force_session_state_next_command)

    def test_spiritguide_dry_run_does_not_apply_state_update(self):
        spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Would add redis.",
                state_update=CommandStateUpdate(
                    world_state={
                        "hosts_by_name": {"ubuntu": {"services_by_name": {"redis": {}}}}
                    }
                ),
                metadata={"structured_response_accepted": True},
            )
        )
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            spiritguide=spiritguide,
        )
        session = SessionState(username="alice", langfuse_trace_id="session-a")
        console.session_registry.register(session)

        result = console.execute(
            "spiritguide --dry-run add redis",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertIn("Would add redis.", result.output)
        self.assertIn("state_update: world_state", result.output)
        self.assertIn("applied: no (dry run)", result.output)
        self.assertIn("dry_run: yes", result.output)
        self.assertIn("Spiritguide patch:", result.output)
        self.assertIn('"services_by_name"', result.output)
        self.assertIn("Dry run", result.output)
        self.assertEqual(spiritguide.dry_runs, [True])
        self.assertEqual(session.world_state, {})

    def test_spiritguide_show_patch_prints_applied_state_update(self):
        spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Redis service added.",
                state_update=CommandStateUpdate(
                    world_state={
                        "hosts_by_name": {"ubuntu": {"services_by_name": {"redis": {}}}}
                    }
                ),
                metadata={"structured_response_accepted": True},
            )
        )
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            spiritguide=spiritguide,
        )
        session = SessionState(username="alice", langfuse_trace_id="session-a")
        console.session_registry.register(session)

        result = console.execute(
            "spiritguide --show-patch add redis",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertIn("Redis service added.", result.output)
        self.assertIn("applied: yes", result.output)
        self.assertIn("Spiritguide patch:", result.output)
        self.assertIn('"world_state"', result.output)
        self.assertIn('"redis"', result.output)

    def test_spiritguide_profile_create_saves_detached_world_state(self):
        spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Factory world created.",
                state_update=CommandStateUpdate(
                    cwd="/srv/factory",
                    world_state={
                        "entry_host": "ubuntu",
                        "active_host": "ubuntu",
                        "hosts_by_name": {
                            "ubuntu": {
                                "hostname": "ubuntu",
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
        state_profiles = StateProfileStore(Path(self.tmpdir.name) / "profiles")
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            spiritguide=spiritguide,
            state_profiles=state_profiles,
        )

        result = console.execute(
            "spiritguide profile create factory-lab --level world build a factory OT lab",
            allow_exit=False,
        )

        profile = state_profiles.load("factory-lab")
        host = profile.state["world_state"]["hosts_by_name"]["ubuntu"]
        self.assertIn("Factory world created.", result.output)
        self.assertIn("Spiritguide profile result:", result.output)
        self.assertIn("saved: yes", result.output)
        self.assertIn("excluded_from_profile: cwd", result.output)
        self.assertNotIn("cwd", profile.state)
        self.assertIn("plc-gateway", host["services_by_name"])
        self.assertIn(
            "/etc/factory/topology.json",
            host["filesystem"]["artifacts_by_path"],
        )
        self.assertEqual(spiritguide.instructions, ["build a factory OT lab"])

    def test_spiritguide_show_patch_prints_host_identity_update(self):
        spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Host renamed.",
                state_update=CommandStateUpdate(
                    host_identity=HostIdentity(hostname="jump01")
                ),
                metadata={"structured_response_accepted": True},
            )
        )
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            spiritguide=spiritguide,
        )
        session = SessionState(username="alice", langfuse_trace_id="session-a")
        console.session_registry.register(session)

        result = console.execute(
            "spiritguide --show-patch rename host",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertIn("state_update: host_identity", result.output)
        self.assertIn('"host_identity"', result.output)
        self.assertIn('"hostname": "jump01"', result.output)
        self.assertEqual(session.host_identity.hostname, "jump01")
        self.assertEqual(
            session.shell_environment.variables["HOSTNAME"],
            "jump01",
        )

    def test_spiritguide_records_session_trace_summary(self):
        spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="Redis service added.",
                state_update=CommandStateUpdate(
                    world_state={
                        "hosts_by_name": {"ubuntu": {"services_by_name": {"redis": {}}}}
                    }
                ),
                metadata={"structured_response_accepted": True},
            )
        )
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            trace_logs=self.trace_logs,
            spiritguide=spiritguide,
        )
        session = SessionState(username="alice", langfuse_trace_id="session-a")
        console.session_registry.register(session)
        context = ConsoleContext(active_session_id="session-a")

        old_level = TRACE_LOGGER.level
        TRACE_LOGGER.addHandler(self.trace_logs)
        TRACE_LOGGER.setLevel(logging.INFO)
        try:
            console.execute(
                "spiritguide add redis",
                allow_exit=False,
                context=context,
            )
        finally:
            TRACE_LOGGER.removeHandler(self.trace_logs)
            TRACE_LOGGER.setLevel(old_level)
        result = console.execute("trace", allow_exit=False, context=context)

        self.assertIn("type=spiritguide", result.output)
        self.assertIn("instruction='add redis'", result.output)
        self.assertIn("dry_run=false", result.output)
        self.assertIn("structured_response=accepted", result.output)
        self.assertIn("state_update=world_state", result.output)
        self.assertIn("applied=true", result.output)

    def test_spiritguide_rejected_structured_response_does_not_apply_state_update(self):
        spiritguide = FakeSpiritguide(
            SpiritguideResult(
                message="partial json",
                state_update=CommandStateUpdate(
                    world_state={
                        "hosts_by_name": {"ubuntu": {"services_by_name": {"redis": {}}}}
                    }
                ),
                metadata={"structured_response_accepted": False},
            )
        )
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            spiritguide=spiritguide,
        )
        session = SessionState(username="alice", langfuse_trace_id="session-a")
        console.session_registry.register(session)

        result = console.execute(
            "spiritguide add redis",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertIn("partial json", result.output)
        self.assertIn("state_update: world_state", result.output)
        self.assertIn("applied: no (structured response rejected)", result.output)
        self.assertIn("structured_response: rejected", result.output)
        self.assertIn("not valid structured state", result.output)
        self.assertEqual(session.world_state, {})

    def test_spiritguide_accepts_explicit_live_session(self):
        spiritguide = FakeSpiritguide(SpiritguideResult(message="Done."))
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            spiritguide=spiritguide,
        )
        session = SessionState(username="alice", langfuse_trace_id="session-a")
        console.session_registry.register(session)

        result = console.execute(
            "spiritguide --session session-a tune world",
            allow_exit=False,
        )

        self.assertIn("Done.", result.output)
        self.assertEqual(spiritguide.session_ids, ["session-a"])
        self.assertEqual(spiritguide.instructions, ["tune world"])
        self.assertEqual(spiritguide.dry_runs, [False])

    def test_spiritguide_requires_active_live_session(self):
        no_active = self.console.execute("spiritguide tune world", allow_exit=False)
        not_live = self.console.execute(
            "spiritguide tune world",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertIn("no active session", no_active.output)
        self.assertIn("is not live", not_live.output)

    def test_spiritguide_help_does_not_require_active_session(self):
        result = self.console.execute("spiritguide help", allow_exit=False)

        self.assertIn("Detached profile workflow:", result.output)
        self.assertIn("spiritguide profile create NAME", result.output)
        self.assertNotIn("no active session", result.output)

    def test_spiritguide_profile_help_explains_profile_workflow(self):
        result = self.console.execute("spiritguide profile help", allow_exit=False)

        self.assertIn("Build detached DREAMLAND state profiles", result.output)
        self.assertIn("spiritguide profile update NAME", result.output)
        self.assertIn("The default level is world", result.output)

    def test_spiritguide_provider_failure_returns_console_error(self):
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            spiritguide=FailingSpiritguide(),
        )
        session = SessionState(username="alice", langfuse_trace_id="session-a")
        console.session_registry.register(session)

        result = console.execute(
            "spiritguide tune world",
            allow_exit=False,
            context=ConsoleContext(active_session_id="session-a"),
        )

        self.assertIn("spiritguide provider error", result.output)
        self.assertEqual(session.world_state, {})

    def test_langfuse_adapter_uses_observations_for_commands(self):
        api = FakeLangfuseApi()
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=api,
            flush_client=FakeFlushClient(),
        )

        commands = introspection.list_commands(
            session_id="session-a",
            limit=5,
            include_io_summary=False,
        )

        self.assertEqual(api.observations.get_many_calls, 2)
        self.assertEqual(
            api.observations.last_fields,
            "core,basic,metadata,usage,model,metrics",
        )
        self.assertIn('"sessionId"', api.observations.last_filter)
        self.assertEqual(commands[0].id, "dreamland-command-1")
        self.assertEqual(commands[0].observation_id, "command-observation-1")
        self.assertEqual(commands[0].command, "whoami")
        self.assertEqual(commands[0].model, "gpt-5.5")
        self.assertEqual(commands[0].total_tokens, 2966)
        self.assertEqual(commands[0].total_cost, 0.000123)
        self.assertTrue(commands[0].reasoning)
        self.assertIsNone(commands[0].summary)

    def test_langfuse_adapter_marks_commands_without_llm_children_local(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=FakeLangfuseApi(include_child=False),
            flush_client=FakeFlushClient(),
        )

        commands = introspection.list_commands(session_id="session-a", limit=5)

        self.assertEqual(commands[0].model, "local")

    def test_langfuse_adapter_accepts_model_field_as_llm_model(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=FakeLangfuseApi(model_field="model"),
            flush_client=FakeFlushClient(),
        )

        commands = introspection.list_commands(session_id="session-a", limit=5)

        self.assertEqual(commands[0].model, "gpt-5.5")

    def test_langfuse_adapter_prefers_display_model_over_internal_model_id(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=FakeLangfuseApi(
                extra_model_fields={
                    "modelId": "4bf01a9f-663f-4302-a05c-b2b42c5348e3",
                    "internalModelId": "internal-123",
                }
            ),
            flush_client=FakeFlushClient(),
        )

        commands = introspection.list_commands(session_id="session-a", limit=5)

        self.assertEqual(commands[0].model, "gpt-5.5")

    def test_langfuse_adapter_extracts_reasoning_summary_from_observation_output(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=FakeLangfuseApi(include_reasoning=True),
            flush_client=FakeFlushClient(),
        )

        inspection = introspection.inspect_command(
            session_id="session-a",
            target=None,
            limit=5,
        )

        self.assertEqual(
            inspection.reasoning_summary,
            "Reviewed the shell state before answering.",
        )

    def test_langfuse_adapter_extracts_reasoning_summary_from_result_metadata(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=FakeLangfuseApi(include_result_reasoning=True),
            flush_client=FakeFlushClient(),
        )

        inspection = introspection.inspect_command(
            session_id="session-a",
            target=None,
            limit=5,
        )

        self.assertEqual(
            inspection.reasoning_summary,
            "Captured before Langfuse callback serialization.",
        )

    def test_langfuse_adapter_extracts_xai_reasoning_content(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=FakeLangfuseApi(include_xai_reasoning=True),
            flush_client=FakeFlushClient(),
        )

        inspection = introspection.inspect_command(
            session_id="session-a",
            target=None,
            limit=5,
        )

        self.assertEqual(
            inspection.reasoning_summary,
            "Checked xAI command context.",
        )

    def test_langfuse_adapter_extracts_anthropic_thinking_content(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=FakeLangfuseApi(include_anthropic_reasoning=True),
            flush_client=FakeFlushClient(),
        )

        inspection = introspection.inspect_command(
            session_id="session-a",
            target=None,
            limit=5,
        )

        self.assertEqual(
            inspection.reasoning_summary,
            "Checked Claude command context.",
        )

    def test_langfuse_adapter_compact_inspection_omits_raw_payloads(self):
        api = FakeLangfuseApi(include_reasoning=True)
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=api,
            flush_client=FakeFlushClient(),
        )

        inspection = introspection.inspect_command(
            session_id="session-a",
            target=None,
            limit=5,
            detail="compact",
        )

        self.assertEqual(inspection.detail, "compact")
        self.assertIsNone(inspection.trace_input)
        self.assertIsNone(inspection.trace_output)
        self.assertIsNone(inspection.trace_metadata)
        self.assertEqual(
            api.observations.last_fields,
            "core,basic,usage,model,metrics",
        )
        self.assertTrue(inspection.observations)
        self.assertIsNone(inspection.observations[0].input)
        self.assertIsNone(inspection.observations[0].output)
        self.assertIsNone(inspection.observations[0].metadata)

    def test_langfuse_adapter_deduplicates_repeated_summary_sections(self):
        summary = "\n\n".join(
            [
                "**Crafting process output**",
                "Generate plausible process output.",
                "**Setting up process details**",
                "Keep Ubuntu ps formatting consistent.",
                "**Crafting process output**",
                "Generate plausible process output.",
                "**Setting up process details**",
                "Keep Ubuntu ps formatting consistent.",
            ]
        )

        rendered = _reasoning_summary_from_observations(
            [{"metadata": {"dreamland.reasoning_summary": summary}}]
        )

        self.assertEqual(
            rendered,
            "\n\n".join(
                [
                    "**Crafting process output**",
                    "Generate plausible process output.",
                    "**Setting up process details**",
                    "Keep Ubuntu ps formatting consistent.",
                ]
            ),
        )

    def test_langfuse_adapter_marks_summary_available_on_commands(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=FakeLangfuseApi(include_result_reasoning=True),
            flush_client=FakeFlushClient(),
        )

        commands = introspection.list_commands(session_id="session-a", limit=5)

        self.assertTrue(commands[0].summary)

    def test_langfuse_adapter_scans_io_for_historical_summary_flags_by_default(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=FakeLangfuseApi(
                include_result_reasoning=True,
                promote_result_reasoning=False,
            ),
            flush_client=FakeFlushClient(),
        )

        default_commands = introspection.list_commands(session_id="session-a", limit=5)
        fast_commands = introspection.list_commands(
            session_id="session-a",
            limit=5,
            include_io_summary=False,
        )

        self.assertTrue(default_commands[0].summary)
        self.assertIsNone(fast_commands[0].summary)

    def test_langfuse_adapter_state_delta_compares_two_command_outputs(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=StateDeltaLangfuseApi(),
            flush_client=FakeFlushClient(),
        )

        delta = introspection.state_delta(
            session_id="session-a",
            from_target="@-2",
            to_target="@-1",
            limit=5,
            sections=("cwd",),
        )

        self.assertEqual(delta.from_label, "after command-old 2026-06-07T21:00:00Z pwd")
        self.assertEqual(
            delta.to_label, "after command-new 2026-06-07T21:01:00Z cd /tmp"
        )
        self.assertEqual(delta.before_state, {"cwd": "/home/user"})
        self.assertEqual(delta.after_state, {"cwd": "/tmp"})

    def test_langfuse_adapter_state_delta_single_target_uses_previous_command(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=StateDeltaLangfuseApi(),
            flush_client=FakeFlushClient(),
        )

        delta = introspection.state_delta(
            session_id="session-a",
            from_target=None,
            to_target="@-1",
            limit=5,
        )

        self.assertEqual(
            delta.from_label, "before command-new 2026-06-07T21:01:00Z cd /tmp"
        )
        self.assertEqual(
            delta.to_label, "after command-new 2026-06-07T21:01:00Z cd /tmp"
        )
        self.assertEqual(delta.before_state["cwd"], "/home/user")
        self.assertEqual(delta.after_state["cwd"], "/tmp")

    def test_commands_shows_unknown_when_summary_requires_io_scan(self):
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            introspection=StaticIntrospection(
                [
                    CommandSummary(
                        id="id-command-1",
                        session_id="session-a",
                        timestamp="2026-06-07T21:00:00Z",
                        command="whoami",
                        reasoning=True,
                        summary=None,
                    )
                ]
            ),
        )
        context = ConsoleContext(active_session_id="session-a")

        result = console.execute(
            "commands --columns command,reasoning,summary",
            allow_exit=False,
            context=context,
        )

        self.assertIn("whoami", result.output)
        self.assertIn("yes", result.output)
        self.assertIn("unknown", result.output)

    def test_commands_quick_skips_summary_io_scan(self):
        introspection = FakeIntrospection()
        console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self._request_shutdown,
            introspection=introspection,
        )
        context = ConsoleContext(active_session_id="session-a")

        console.execute("commands --quick", allow_exit=False, context=context)

        self.assertEqual(introspection.include_io_summary_calls, [False])

    def test_langfuse_adapter_wraps_api_failures(self):
        introspection = LangfuseConsoleIntrospection(
            LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
            api_client=FailingLangfuseApi(),
            flush_client=FakeFlushClient(),
        )

        with self.assertRaises(IntrospectionUnavailable):
            introspection.list_commands(session_id="session-a", limit=5)

    def test_exit_is_not_a_console_command(self):
        local = self.console.execute("exit", allow_exit=False)
        remote = self.console.execute("exit", allow_exit=True)

        self.assertIn("not a Dreamland console command", local.output)
        self.assertIn("not a Dreamland console command", remote.output)

    def test_tab_completion_uses_isolated_editor_history(self):
        output = []
        editor = CompletionLineEditor(
            output.append,
            "dreamland> ",
            lambda line: self.console.complete(line, allow_exit=True),
        )
        for byte in b"log\t":
            editor.handle_byte(byte)

        self.assertIn("logging ", "".join(output))

    def test_tab_completion_accepts_dreamland_prefix(self):
        output = []
        editor = CompletionLineEditor(
            output.append,
            "dreamland> ",
            lambda line: self.console.complete(line, allow_exit=True),
        )
        for byte in b"!dreamland log\t":
            editor.handle_byte(byte)

        self.assertIn("!dreamland logging ", "".join(output))

    def test_tab_completion_supports_spiritguide_options(self):
        completed = self.console.complete("spiritguide --dr", allow_exit=False)

        self.assertEqual(completed, "spiritguide --dry-run ")

        completed = self.console.complete("spiritguide --sh", allow_exit=False)

        self.assertEqual(completed, "spiritguide --show-patch ")

    def test_enrichment_status_is_concise_and_presentation_friendly(self):
        control = FakeEnrichmentControl()
        self.console.enrichment_control = control

        result = self.console.execute("enrichment status", allow_exit=False)

        self.assertIn("EnrichmentAgent: started", result.output)
        self.assertIn("automatic=enabled", result.output)
        self.assertIn("apply_mode=safe", result.output)
        self.assertIn("inflight=1 queued=2 completed=7 failed=1", result.output)
        self.assertIsNone(result.refresh)

    def test_enrichment_status_follow_returns_one_second_refresh(self):
        control = FakeEnrichmentControl()
        self.console.enrichment_control = control

        result = self.console.execute("enrichment status -f", allow_exit=False)

        self.assertIn("refreshing every 1s", result.output)
        self.assertIn("inflight=1 queued=2 completed=7 failed=1", result.output)
        self.assertIsNotNone(result.refresh)
        assert result.refresh is not None
        self.assertEqual(result.refresh.name, "enrichment_status")
        self.assertEqual(result.refresh.interval_seconds, 1.0)
        self.assertEqual(result.refresh.render(), result.output)
        control.inflight = 0
        control.completed = 8
        refreshed = result.refresh.render()
        self.assertIn("inflight=0", refreshed)
        self.assertIn("completed=8", refreshed)

    def test_enrichment_status_follow_long_option_and_help(self):
        self.console.enrichment_control = FakeEnrichmentControl()

        result = self.console.execute(
            "enrichment status --follow",
            allow_exit=False,
        )
        help_result = self.console.execute(
            "enrichment status --help",
            allow_exit=False,
        )

        self.assertIsNotNone(result.refresh)
        self.assertIn("-f", help_result.output)
        self.assertIn("--follow", help_result.output)

    def test_enrichment_run_resolves_live_active_session_and_scope(self):
        control = FakeEnrichmentControl()
        self.console.enrichment_control = control
        session_id = "session-enrichment-long"
        self.console.session_registry.register(
            SessionState(username="alice", langfuse_trace_id=session_id)
        )
        context = ConsoleContext(
            active_session_id="session-enr",
            recent_session_ids=(session_id,),
        )

        result = self.console.execute(
            "enrichment run --host cache-01 --surface processes",
            allow_exit=False,
            context=context,
        )

        self.assertEqual(
            control.submissions,
            [(session_id, "cache-01", "processes")],
        )
        self.assertIn("enrichment-job-1", result.output)
        self.assertIn(session_id, result.output)

    def test_enrichment_run_rejects_non_live_session_before_submission(self):
        control = FakeEnrichmentControl()
        self.console.enrichment_control = control

        result = self.console.execute(
            "enrichment run --session departed-session",
            allow_exit=False,
        )

        self.assertIn("is not live", result.output)
        self.assertEqual(control.submissions, [])

    def test_enrichment_jobs_renders_scope_timing_and_reason(self):
        control = FakeEnrichmentControl()
        self.console.enrichment_control = control

        result = self.console.execute("enrichment jobs -n 3", allow_exit=False)

        self.assertEqual(control.job_limits, [3])
        self.assertIn("Recent enrichment jobs (1)", result.output)
        self.assertIn("completed enrichment-job-1", result.output)
        self.assertIn("mode=ambient_presence", result.output)
        self.assertIn("target=cache-01", result.output)
        self.assertIn("surface=processes", result.output)
        self.assertIn("timing=84ms", result.output)
        self.assertIn("reason=patch_applied", result.output)

    def test_enrichment_graph_prints_mermaid_topology(self):
        self.console.enrichment_control = FakeEnrichmentControl()

        result = self.console.execute("enrichment graph", allow_exit=False)

        self.assertIn("EnrichmentAgent graphs (Mermaid)", result.output)
        self.assertIn("coordinator --> expert", result.output)
        self.assertIn("Bounded Integrator refresh graph", result.output)
        self.assertIn("snapshot_refresh --> integrator_refresh", result.output)

    def test_enrichment_reports_unavailable_control_clearly(self):
        result = self.console.execute("enrichment status", allow_exit=False)

        self.assertEqual(
            result.output,
            "error: EnrichmentAgent control is unavailable in this runtime\n",
        )

    def test_tab_completion_supports_enrichment_commands_and_options(self):
        self.assertEqual(
            self.console.complete("enrichment st", allow_exit=False),
            "enrichment status ",
        )
        self.assertEqual(
            self.console.complete("enrichment run --ho", allow_exit=False),
            "enrichment run --host ",
        )
        self.assertEqual(
            self.console.complete("enrichment status --f", allow_exit=False),
            "enrichment status --follow ",
        )

    def test_redraw_console_frame_replaces_previous_status_block(self):
        current = "heading\nnew status\nnew jobs\n"

        self.assertEqual(
            redraw_console_frame(current),
            "\x1b[H\x1b[2Jheading\nnew status\nnew jobs\n",
        )

    def test_local_console_supports_completion_and_shutdown(self):
        output = io.StringIO()
        LocalConsole(
            self.console,
            output_stream=output,
            prompt_session=FakePromptSession(["shutdown"]),
        ).run()

        self.assertTrue(self.shutdown_requested)
        self.assertIn("Dreamland shutdown requested.", output.getvalue())

    def test_local_console_uses_prompt_toolkit_completion(self):
        completions = list(
            DreamlandCompleter(self.console).get_completions(
                Document("shut"),
                object(),
            )
        )

        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0].text, "shutdown ")

    def test_local_console_follow_logs_uses_carriage_return_line_endings(self):
        read_fd, write_fd = os.pipe()
        input_stream = os.fdopen(read_fd, "rb", buffering=0)
        output = io.StringIO()
        local_console = LocalConsole(
            self.console,
            input_stream=input_stream,
            output_stream=output,
            prompt_session=FakePromptSession([]),
        )
        thread = threading.Thread(target=local_console._follow_logs)
        thread.start()
        try:
            self._wait_until(lambda: thread.is_alive())
            threading.Event().wait(0.05)
            self.logs.handle(_record("followed-log"))
            self._wait_until(lambda: "followed-log\r\n" in output.getvalue())
            os.write(write_fd, b"x")
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertTrue(output.getvalue().startswith("followed-log\r\n"))
            self.assertTrue(output.getvalue().endswith("\r\n"))
        finally:
            os.close(write_fd)
            input_stream.close()

    def test_local_console_follow_trace_uses_carriage_return_line_endings(self):
        read_fd, write_fd = os.pipe()
        input_stream = os.fdopen(read_fd, "rb", buffering=0)
        output = io.StringIO()
        local_console = LocalConsole(
            self.console,
            input_stream=input_stream,
            output_stream=output,
            prompt_session=FakePromptSession([]),
        )
        thread = threading.Thread(
            target=local_console._follow_trace,
            args=("session-a",),
        )
        thread.start()
        try:
            self._wait_until(lambda: thread.is_alive())
            threading.Event().wait(0.05)
            self.trace_logs.handle(_trace_record("session-a", "followed-trace"))
            self._wait_until(lambda: "followed-trace\r\n" in output.getvalue())
            os.write(write_fd, b"x")
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertTrue(output.getvalue().startswith("followed-trace\r\n"))
            self.assertTrue(output.getvalue().endswith("\r\n"))
        finally:
            os.close(write_fd)
            input_stream.close()

    def test_local_console_refresh_redraws_until_keypress(self):
        read_fd, write_fd = os.pipe()
        input_stream = os.fdopen(read_fd, "rb", buffering=0)
        output = io.StringIO()
        render_count = 0

        def render():
            nonlocal render_count
            render_count += 1
            return f"heading\nstatus={render_count}\njobs\n"

        refresh = ConsoleRefresh(
            name="enrichment_status",
            render=render,
            interval_seconds=0.01,
        )
        local_console = LocalConsole(
            self.console,
            input_stream=input_stream,
            output_stream=output,
            prompt_session=FakePromptSession([]),
            terminal_capable=True,
        )
        initial_frame = "heading\nstatus=0\njobs\n"
        output.write(CONSOLE_REFRESH_ENTER_SCREEN + initial_frame)
        thread = threading.Thread(
            target=local_console._follow_refresh,
            kwargs={"refresh": refresh, "initial_frame": initial_frame},
        )
        thread.start()
        try:
            self._wait_until(lambda: "status=1" in output.getvalue())
            os.write(write_fd, b"\x1b")
            threading.Event().wait(0.04)
            os.write(write_fd, b"[A")
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertTrue(output.getvalue().startswith(CONSOLE_REFRESH_ENTER_SCREEN))
            self.assertIn("\x1b[H\x1b[2J", output.getvalue())
            self.assertIn("status=1\r\njobs\r\n", output.getvalue())
            self.assertIn(CONSOLE_REFRESH_EXIT_SCREEN, output.getvalue())
            final_frame = output.getvalue().split(CONSOLE_REFRESH_EXIT_SCREEN, 1)[1]
            self.assertEqual(final_frame, f"heading\nstatus={render_count}\njobs\n")
            ready, _, _ = select.select([input_stream], [], [], 0)
            self.assertEqual(ready, [])
        finally:
            os.close(write_fd)
            input_stream.close()

    def test_local_console_rejects_refresh_without_terminal(self):
        self.console.enrichment_control = FakeEnrichmentControl()
        output = io.StringIO()

        LocalConsole(
            self.console,
            output_stream=output,
            prompt_session=FakePromptSession(["enrichment status -f"]),
            terminal_capable=False,
        ).run()

        self.assertEqual(
            output.getvalue(),
            "error: enrichment status --follow requires an interactive terminal\n",
        )

    def test_local_console_refresh_reports_status_read_failure(self):
        read_fd, write_fd = os.pipe()
        input_stream = os.fdopen(read_fd, "rb", buffering=0)
        output = io.StringIO()

        def fail():
            raise RuntimeError("provider prose must not be rendered")

        local_console = LocalConsole(
            self.console,
            input_stream=input_stream,
            output_stream=output,
            prompt_session=FakePromptSession([]),
            terminal_capable=True,
        )
        refresh = ConsoleRefresh(
            name="enrichment_status",
            render=fail,
            interval_seconds=0.01,
        )

        try:
            with self.assertLogs("dreamland.console", level="INFO") as captured:
                local_console._follow_refresh(
                    refresh,
                    initial_frame="initial status\n",
                )

            self.assertIn(
                "\x1b[H\x1b[2Jerror: live console refresh failed [RuntimeError]\r\n",
                output.getvalue(),
            )
            self.assertIn(CONSOLE_REFRESH_EXIT_SCREEN, output.getvalue())
            self.assertTrue(
                output.getvalue().endswith(
                    "error: live console refresh failed [RuntimeError]\n"
                )
            )
            self.assertNotIn("provider prose", output.getvalue())
            logs = "\n".join(captured.output)
            self.assertNotIn("provider prose", logs)
            self.assertIn("Console refresh started", logs)
            self.assertIn("Console refresh read failed", logs)
            self.assertIn("reason=status_error", logs)
        finally:
            os.close(write_fd)
            input_stream.close()

    def test_local_console_exits_when_shutdown_is_requested_elsewhere(self):
        output = io.StringIO()
        prompt_session = BlockingPromptSession()
        local_console = LocalConsole(
            self.console,
            output_stream=output,
            prompt_session=prompt_session,
        )
        thread = threading.Thread(target=local_console.run)
        thread.start()
        try:
            prompt_session.wait_until_prompting()
            result = self.console.execute("shutdown", allow_exit=False)
            thread.join(timeout=2)

            self.assertTrue(result.shutdown)
            self.assertFalse(thread.is_alive())
        finally:
            prompt_session.release()

    def _request_shutdown(self):
        self.shutdown_requested = True

    def _wait_until(self, condition):
        for _ in range(200):
            if condition():
                return
            threading.Event().wait(0.01)
        self.fail("Timed out waiting for condition.")


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


class FakePromptSession:
    def __init__(self, commands):
        self._commands = list(commands)

    def prompt(self, prompt):
        if not self._commands:
            raise EOFError
        return self._commands.pop(0)


class BlockingPromptSession:
    def __init__(self):
        self._prompting = threading.Event()
        self._released = threading.Event()
        self.app = BlockingPromptApplication(self._released)

    def prompt(self, prompt):
        self._prompting.set()
        self._released.wait(timeout=5)
        raise EOFError

    def wait_until_prompting(self):
        self._prompting.wait(timeout=2)

    def release(self):
        self._released.set()


class BlockingPromptApplication:
    def __init__(self, released):
        self._released = released

    def exit(self, exception=None):
        self._released.set()


class FakeEnrichmentControl:
    def __init__(self):
        self.submissions = []
        self.job_limits = []
        self.inflight = 1
        self.completed = 7

    def status(self):
        return EnrichmentRuntimeStatus(
            configured=True,
            started=True,
            automatic_enabled=True,
            apply_mode="safe",
            inflight=self.inflight,
            queued=2,
            completed=self.completed,
            failed=1,
        )

    def submit_manual(self, session_id, target_host=None, surface=None):
        self.submissions.append((session_id, target_host, surface))
        return EnrichmentSubmission(
            accepted=True,
            code="accepted",
            reason="manual_trigger",
            job_id="enrichment-job-1",
        )

    def list_jobs(self, limit):
        self.job_limits.append(limit)
        return (
            EnrichmentJobSnapshot(
                job_id="enrichment-job-1",
                session_id="session-enrichment-long",
                trigger="manual",
                target_host="cache-01",
                surface="processes",
                status="completed",
                mode="ambient_presence",
                reason="patch_applied",
                queued_at="2026-07-21T10:00:00Z",
                started_at="2026-07-21T10:00:00Z",
                completed_at="2026-07-21T10:00:00Z",
                duration_ms=84,
            ),
        )

    def graph_mermaid(self):
        return (
            "Main enrichment proposal graph\n\n"
            "```mermaid\n"
            "flowchart LR\n  coordinator --> expert\n  expert --> integrator\n"
            "```\n\n"
            "Bounded Integrator refresh graph (attempt 1)\n\n"
            "```mermaid\n"
            "flowchart LR\n  snapshot_refresh --> integrator_refresh\n"
            "```"
        )


class FakeSpiritguide:
    def __init__(self, result):
        self.result = result
        self.instructions = []
        self.session_ids = []
        self.dry_runs = []

    def respond(self, context):
        self.instructions.append(context.instruction)
        self.session_ids.append(context.session_id)
        self.dry_runs.append(context.dry_run)
        return self.result


class FailingSpiritguide:
    def respond(self, context):
        raise RuntimeError("provider failed")


class FakeIntrospection:
    def __init__(self, session_ids=None):
        self.flush_count = 0
        self.command_sessions = []
        self.include_io_summary_calls = []
        self.session_ids = session_ids or ["session-a", "session-b"]

    def available(self):
        return True

    def flush(self):
        self.flush_count += 1

    def list_sessions(self, *, limit):
        return SessionList(
            sessions=[
                SessionSummary(id=session_id, created_at="2026-06-07T20:00:00Z")
                for session_id in self.session_ids
            ],
            total=len(self.session_ids),
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
        self.command_sessions.append(session_id)
        self.include_io_summary_calls.append(include_io_summary)
        commands = [
            CommandSummary(
                id="id-command-1",
                session_id=session_id,
                timestamp="2026-06-07T21:01:00Z",
                command="whoami",
                total_tokens=42,
                total_cost=0.001,
                latency=0.5,
                reasoning=True,
                summary=True,
            ),
            CommandSummary(
                id="id-command-2",
                session_id=session_id,
                timestamp="2026-06-07T21:02:00Z",
                command="id",
                total_tokens=84,
                total_cost=0.002,
                latency=0.7,
            ),
        ]
        if filter_text:
            return [command for command in commands if filter_text in command.command]
        return commands

    def inspect_command(self, *, session_id, target, limit, detail="full"):
        command = self.list_commands(session_id=session_id, limit=limit)[0]
        full_detail = detail == "full"
        return CommandInspection(
            command=command,
            detail=detail,
            trace_input=[
                {
                    "role": "user",
                    "content": "line one\nline two",
                }
            ]
            if full_detail
            else None,
            trace_output={"stdout": "uid=1000(user)\n"} if full_detail else None,
            trace_metadata={"dreamland.command": command.command}
            if full_detail
            else None,
            reasoning_summary="Checked the command context.",
            observations=[
                ObservationSummary(
                    id="observation-1",
                    type="GENERATION",
                    name="model",
                    input={"prompt": "line one\nline two"} if full_detail else None,
                    output={"text": "done\n"} if full_detail else None,
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
        commands = self.list_commands(session_id=session_id, limit=limit)
        from_command = commands[1] if from_target is not None else commands[0]
        to_command = commands[0]
        before_state = {
            "cwd": "/home/user",
            "history": [{"command": "id"}],
        }
        after_state = {
            "cwd": "/tmp",
            "history": [{"command": "id"}, {"command": "whoami"}],
        }
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
            from_label=(
                f"after {from_command.id}"
                if from_target is not None
                else f"before {to_command.id}"
            ),
            to_label=f"after {to_command.id}",
            before_state=before_state,
            after_state=after_state,
            sections=tuple(sections),
        )


class StaticIntrospection(FakeIntrospection):
    def __init__(self, commands):
        super().__init__()
        self._commands = list(commands)

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
        self.command_sessions.append(session_id)
        self.include_io_summary_calls.append(include_io_summary)
        return self._commands


class EmptyIntrospection(FakeIntrospection):
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
        self.command_sessions.append(session_id)
        self.include_io_summary_calls.append(include_io_summary)
        return []


class FakeLangfuseApi:
    def __init__(
        self,
        *,
        include_child=True,
        model_field="providedModelName",
        extra_model_fields=None,
        include_reasoning=False,
        include_xai_reasoning=False,
        include_anthropic_reasoning=False,
        include_result_reasoning=False,
        promote_result_reasoning=True,
    ):
        self.observations = FakeLangfuseObservationsApi(
            include_child=include_child,
            model_field=model_field,
            extra_model_fields=extra_model_fields or {},
            include_reasoning=include_reasoning,
            include_xai_reasoning=include_xai_reasoning,
            include_anthropic_reasoning=include_anthropic_reasoning,
            include_result_reasoning=include_result_reasoning,
            promote_result_reasoning=promote_result_reasoning,
        )
        self.sessions = object()


class FakeLangfuseObservationsApi:
    def __init__(
        self,
        *,
        include_child,
        model_field,
        extra_model_fields,
        include_reasoning,
        include_xai_reasoning,
        include_anthropic_reasoning,
        include_result_reasoning,
        promote_result_reasoning,
    ):
        self.get_many_calls = 0
        self.last_fields = None
        self.last_filter = None
        self.include_child = include_child
        self.model_field = model_field
        self.extra_model_fields = extra_model_fields
        self.include_reasoning = include_reasoning
        self.include_xai_reasoning = include_xai_reasoning
        self.include_anthropic_reasoning = include_anthropic_reasoning
        self.include_result_reasoning = include_result_reasoning
        self.promote_result_reasoning = promote_result_reasoning

    def get_many(self, **kwargs):
        self.get_many_calls += 1
        self.last_fields = kwargs["fields"]
        self.last_filter = kwargs["filter"]
        requested_fields = kwargs["fields"].split(",")
        include_io = "io" in requested_fields
        include_metadata = "metadata" in requested_fields
        if kwargs.get("name") == "dreamland-command-agent":
            return {
                "data": [
                    self._command_observation(
                        include_io=include_io,
                        include_metadata=include_metadata,
                    )
                ]
            }
        observations = [
            self._command_observation(
                include_io=include_io,
                include_metadata=include_metadata,
            ),
        ]
        if self.include_child:
            model_payload = {
                self.model_field: "gpt-5.5",
                **self.extra_model_fields,
            }
            observations.extend(
                [
                    {
                        "id": "chain-observation-1",
                        "parentObservationId": "command-observation-1",
                        "sessionId": "session-a",
                        "startTime": "2026-06-07T21:00:01Z",
                        "name": "agent-node",
                    },
                    {
                        "id": "generation-observation-1",
                        "parentObservationId": "chain-observation-1",
                        "sessionId": "session-a",
                        "startTime": "2026-06-07T21:00:01Z",
                        "name": "ChatOpenAI",
                        **model_payload,
                        "usageDetails": {
                            "input": 2921,
                            "output": 45,
                            "total": 2966,
                            "output_reasoning": 91,
                        },
                        "totalCost": 0.000123,
                        **({"output": self._generation_output()} if include_io else {}),
                    },
                ]
            )
        return {"data": observations}

    def _command_observation(self, *, include_io, include_metadata):
        observation = {
            "id": "command-observation-1",
            "sessionId": "session-a",
            "startTime": "2026-06-07T21:00:00Z",
            "name": "dreamland-command-agent",
        }
        if include_metadata:
            observation["metadata"] = self._command_metadata()
        if include_io:
            observation["output"] = self._command_output()
        return observation

    def _command_metadata(self):
        metadata = {
            "dreamland.command": "whoami",
            "dreamland.command_id": "dreamland-command-1",
        }
        if self.include_result_reasoning and self.promote_result_reasoning:
            metadata["dreamland.reasoning_summary"] = (
                "Captured before Langfuse callback serialization."
            )
        return metadata

    def _command_output(self):
        metadata = {}
        if self.include_result_reasoning:
            metadata["dreamland.reasoning_summary"] = (
                "Captured before Langfuse callback serialization."
            )
        return {"result": {"metadata": metadata}}

    def _generation_output(self):
        if self.include_xai_reasoning:
            return {
                "role": "assistant",
                "content": "uid=1000(user)\n",
                "additional_kwargs": {
                    "reasoning_content": "Checked xAI command context."
                },
            }
        if self.include_anthropic_reasoning:
            return {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Checked Claude command context.",
                    },
                    {"type": "text", "text": "uid=1000(user)\n"},
                ],
            }
        if not self.include_reasoning:
            return {"content": [{"type": "text", "text": "uid=1000(user)\n"}]}
        return {
            "content": [
                {
                    "type": "reasoning",
                    "summary": [
                        {
                            "type": "summary_text",
                            "text": "Reviewed the shell state before answering.",
                        }
                    ],
                },
                {"type": "text", "text": "uid=1000(user)\n"},
            ]
        }


class StateDeltaLangfuseApi:
    def __init__(self):
        self.observations = StateDeltaLangfuseObservationsApi()
        self.sessions = object()


class StateDeltaLangfuseObservationsApi:
    def __init__(self):
        self.get_many_calls = 0

    def get_many(self, **kwargs):
        self.get_many_calls += 1
        include_io = "io" in kwargs["fields"].split(",")
        observations = [
            self._command_observation(
                observation_id="command-new",
                command="cd /tmp",
                timestamp="2026-06-07T21:01:00Z",
                state={
                    "cwd": "/tmp",
                    "history": [
                        {"command": "pwd", "stdout": "/home/user\n"},
                        {"command": "cd /tmp", "stdout": ""},
                    ],
                },
                include_io=include_io,
            ),
            self._command_observation(
                observation_id="command-old",
                command="pwd",
                timestamp="2026-06-07T21:00:00Z",
                state={
                    "cwd": "/home/user",
                    "history": [{"command": "pwd", "stdout": "/home/user\n"}],
                },
                include_io=include_io,
            ),
        ]
        if kwargs.get("name") == "dreamland-command-agent":
            return {"data": observations}
        return {"data": observations}

    def _command_observation(
        self,
        *,
        observation_id,
        command,
        timestamp,
        state,
        include_io,
    ):
        observation = {
            "id": observation_id,
            "sessionId": "session-a",
            "startTime": timestamp,
            "name": "dreamland-command-agent",
            "metadata": {"dreamland.command": command},
        }
        if include_io:
            observation["input"] = {"current_command": command}
            observation["output"] = state
        return observation


class FailingLangfuseApi:
    def __init__(self):
        self.observations = FailingLangfuseObservationsApi()


class FailingLangfuseObservationsApi:
    def get_many(self, **kwargs):
        raise AttributeError("no observations")


class FakeFlushClient:
    def flush(self):
        pass


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
