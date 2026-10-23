import json
import os
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dreamland.admin_interface import AdminServiceError, DreamlandAdminService
from dreamland.agent_core import _format_command_context
from dreamland.agent_tracing import session_start_trace_metadata
from dreamland.backend import AgentBackend
from dreamland.background_agents.event_bus import SessionEventBus
from dreamland.config import load_settings
from dreamland.evaluation_config import (
    EVALUATION_CONFIG_KEY,
    evaluation_snapshot,
    resolve_evaluation_config,
    resolve_mcp_session_config,
)
from dreamland.evaluation_runtime import EvaluationRuntimeFactory
from dreamland.models import (
    CommandResult,
    CommandStateUpdate,
    ConnectionMetadata,
    SessionState,
)
from dreamland.profile import server_profile
from dreamland.runtime_logging import LogRecordBuffer, SessionTraceLogBuffer
from dreamland.session_metadata import (
    SESSION_LABEL_METADATA_KEY,
    SESSION_ORIGIN_METADATA_KEY,
)
from dreamland.session_registry import LiveSessionRegistry
from dreamland.spiritguide import (
    SpiritguideContext,
    SpiritguideResult,
    _format_spiritguide_context,
)
from dreamland.state_profiles import (
    StateProfile,
    StateProfileRuntime,
    StateProfileStore,
    apply_state_profile_to_session,
    create_state_profile,
)
from dreamland.structured_failures import StructuredFailureStore


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.dict(os.environ, {}, clear=True):
            self.settings = load_settings(Path(self.tmp.name) / "absent.ini")
        self.settings = replace(
            self.settings, enrichment=replace(self.settings.enrichment, enabled=False)
        )
        self.bus = SessionEventBus()
        self.default_events = []
        self.bus.subscribe("test", self.default_events.append)
        self.registry = LiveSessionRegistry(event_bus=self.bus)
        self.profiles = StateProfileRuntime(
            store=StateProfileStore(Path(self.tmp.name))
        )
        self.failures = StructuredFailureStore(Path(self.tmp.name) / "failures")
        self.backend = AgentBackend(
            server_profile(),
            state_profile_runtime=self.profiles,
            event_bus=self.bus,
        )
        self.factory = EvaluationRuntimeFactory(
            self.settings,
            profile=self.backend.profile,
            state_profile_runtime=self.profiles,
            session_registry=self.registry,
            structured_failure_store=self.failures,
            api_key_resolver=lambda provider: f"private-{provider}-key",
        )
        self.service = DreamlandAdminService(
            logs=LogRecordBuffer(20),
            trace_logs=SessionTraceLogBuffer(20),
            session_registry=self.registry,
            state_profile_runtime=self.profiles,
            command_backend=self.backend,
            evaluation_factory=self.factory,
        )
        self.command_constructor = self.enterContext(
            patch(
                "dreamland.evaluation_runtime.LangGraphCommandAgent",
                side_effect=RecordingAgent,
            )
        )
        self.guide_constructor = self.enterContext(
            patch(
                "dreamland.evaluation_runtime.ModelSpiritguide",
                side_effect=RecordingGuide,
            )
        )
        self.enrichment_constructor = self.enterContext(
            patch("dreamland.evaluation_runtime.EnrichmentAgent.from_settings")
        )
        self.addCleanup(self.close_sessions)

    def close_sessions(self):
        for registration in self.registry.list_sessions():
            self.registry.unregister(registration.session_id)

    def save_profile(self, name, host, level="world"):
        profile = StateProfile(
            name=name,
            level=level,
            created_at="2026-09-05T00:00:00Z",
            state={
                "world_state": {
                    "entry_host": host,
                    "active_host": host,
                    "hosts_by_name": {
                        host: {
                            "hostname": host,
                            "login_banner": "",
                            "facts_by_id": {"owner": "{username}"},
                        }
                    },
                }
            },
        )
        self.profiles.store.save(profile)
        return profile

    def test_provider_neutral_resolution_all_roles_and_null_clear(self):
        overrides = {
            "agent": {"model": "anthropic:claude-test", "reasoning_effort": "max"},
            "spiritguide": {"model": "xai:grok-test", "reasoning_summary": None},
            "enrichment": {
                role: {"model": f"openai:{role}-test", "max_output_tokens": 100 + index}
                for index, role in enumerate(
                    ("coordinator", "ambient_coordinator", "expert", "integrator")
                )
            },
        }
        resolved = resolve_evaluation_config(self.settings, overrides)
        self.assertEqual(resolved.agent.model_provider, "anthropic")
        self.assertIsNone(resolved.spiritguide.reasoning_summary)
        self.assertEqual(resolved.enrichment.integrator.max_output_tokens, 103)
        self.assertEqual(self.settings.agent.model_provider, "openai")
        self.assertEqual(overrides["agent"]["model"], "anthropic:claude-test")
        self.assertEqual(
            resolved.enrichment.expert.system_prompt_name,
            self.settings.enrichment.expert.system_prompt_name,
        )
        self.assertEqual(
            evaluation_snapshot(resolved),
            evaluation_snapshot(
                resolve_evaluation_config(self.settings, deepcopy(overrides))
            ),
        )

    def test_invalid_overrides_fail_before_models_or_sessions(self):
        cases = [
            [],
            {"api_key": "private-value"},
            {"agent": {"api_key": "private-value"}},
            {"agent": {"model": None}},
            {"agent": {"model": "unknown:test"}},
            {"agent": {"max_output_tokens": True}},
            {"agent": {"max_output_tokens": -1}},
            {"agent": {"reasoning_effort": "invented"}},
            {"agent": {"openai_api": "chat_completions"}},
            {"enrichment": {"enabled": "false"}},
            {"enrichment": {"apply_mode": "apply_all"}},
            {"enrichment": {"triggers": ["unknown"]}},
            {"enrichment": {"max_inflight_per_session": 0}},
            {"enrichment": {"integrator": {"max_output_tokens": 1.5}}},
        ]
        for overrides in cases:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises(AdminServiceError) as caught,
            ):
                self.service.create_mcp_session(config_overrides=overrides)
            self.assertEqual(caught.exception.code, "bad_request")
            self.assertNotIn("private-value", str(caught.exception))
        self.assertFalse(self.registry.list_sessions())
        self.command_constructor.assert_not_called()

    def test_mcp_command_defaults_are_isolated_from_process_fast_tier(self):
        base = replace(
            self.settings,
            agent=replace(self.settings.agent, openai_service_tier="fast"),
            spiritguide=replace(self.settings.spiritguide, openai_service_tier="fast"),
        )
        self.factory.settings = base
        before = evaluation_snapshot(base)
        self.backend.agent = RecordingAgent(
            model_name="ssh-model", openai_service_tier="fast"
        )
        ssh_session = self.backend.create_session(
            "ssh-user", ConnectionMetadata(protocol="ssh")
        )
        self.registry.register(ssh_session)
        self.default_events.clear()
        for arguments in (
            {},
            {"config_overrides": {}},
            {"model": "openai:direct-model"},
            {"config_overrides": {"agent": {"model": "openai:nested-model"}}},
        ):
            with self.subTest(arguments=arguments):
                created = self.service.create_mcp_session(**arguments)
                registration = self.registry.get(created.session_id)
                runtime = registration.runtime
                self.assertIsNotNone(runtime)
                self.assertIsNot(runtime.backend, self.backend)
                self.assertEqual(
                    runtime.backend.agent.kwargs["openai_service_tier"], "default"
                )
                settings = created.evaluation_config["settings"]
                self.assertEqual(settings["agent"]["openai_service_tier"], "default")
                self.assertEqual(
                    settings["spiritguide"], before["settings"]["spiritguide"]
                )
                self.assertEqual(
                    settings["enrichment"], before["settings"]["enrichment"]
                )
                self.assertEqual(
                    registration.session.environment_metadata.metadata[
                        EVALUATION_CONFIG_KEY
                    ],
                    created.evaluation_config,
                )
                observed = []
                runtime.event_bus.subscribe("observe", observed.append)
                command = self.service.run_mcp_session_command(
                    session_id=created.session_id, command="probe"
                )
                expected_model = settings["agent"]["model"].split(":", 1)[1]
                self.assertEqual(command.stdout, expected_model + "\n")
                self.assertTrue(observed)
                self.assertFalse(self.default_events)
                with patch.object(
                    runtime.enrichment, "stop", wraps=runtime.enrichment.stop
                ) as stop:
                    self.service.close_live_sessions(
                        session_ids=[created.session_id], origin="mcp"
                    )
                stop.assert_called_once_with(wait=False)
                self.assertIsNone(self.registry.get(created.session_id))
        self.assertEqual(evaluation_snapshot(self.factory.settings), before)
        self.assertIs(self.service.command_backend, self.backend)
        self.assertEqual(self.backend.agent.kwargs["openai_service_tier"], "fast")
        self.assertEqual(
            self.service.run_mcp_session_command(
                session_id=ssh_session.langfuse_trace_id, command="probe"
            ).stdout,
            "ssh-model\n",
        )

    def test_mcp_direct_options_merge_without_mutating_nested_overrides(self):
        overrides = {
            "agent": {
                "model": "openai:direct-model",
                "openai_service_tier": "flex",
                "reasoning_effort": "low",
            },
            "spiritguide": {"model": "xai:guide-model"},
            "enrichment": {"expert": {"model": "anthropic:expert-model"}},
        }
        original = deepcopy(overrides)
        created = self.service.create_mcp_session(
            model="openai:direct-model",
            openai_service_tier="flex",
            config_overrides=overrides,
        )
        settings = created.evaluation_config["settings"]
        self.assertEqual(settings["agent"]["model"], "openai:direct-model")
        self.assertEqual(settings["agent"]["openai_service_tier"], "flex")
        self.assertEqual(settings["agent"]["reasoning_effort"], "low")
        self.assertEqual(settings["spiritguide"]["model"], "xai:guide-model")
        self.assertEqual(
            settings["enrichment"]["expert"]["model"], "anthropic:expert-model"
        )
        self.assertEqual(overrides, original)

    def test_mcp_explicit_tiers_and_nested_null_retain_their_meaning(self):
        for tier in ("auto", "default", "flex", "fast", "priority", "ultrafast", None):
            with self.subTest(tier=tier):
                nested = resolve_mcp_session_config(
                    self.settings, {"agent": {"openai_service_tier": tier}}
                )
                self.assertEqual(nested.agent.openai_service_tier, tier)
                if tier is not None:
                    direct = resolve_mcp_session_config(
                        self.settings, openai_service_tier=tier
                    )
                    self.assertEqual(direct.agent, nested.agent)
        for inherited_tier in (None, "fast"):
            base = replace(
                self.settings,
                agent=replace(self.settings.agent, openai_service_tier=inherited_tier),
            )
            for provider in ("anthropic", "xai"):
                with self.subTest(provider=provider, inherited_tier=inherited_tier):
                    resolved = resolve_mcp_session_config(
                        base, model=f"{provider}:test-model"
                    )
                    self.assertEqual(resolved.agent.openai_service_tier, inherited_tier)
                    self.assertEqual(resolved.agent.model_provider, provider)

    def test_mcp_invalid_or_conflicting_direct_options_fail_before_construction(self):
        cases = (
            {"model": "private-invalid-reference"},
            {"model": ""},
            {"model": True},
            {"openai_service_tier": "private-invalid-tier"},
            {"openai_service_tier": True},
            {
                "model": "openai:direct",
                "config_overrides": {"agent": {"model": "openai:nested"}},
            },
            {
                "openai_service_tier": "default",
                "config_overrides": {"agent": {"openai_service_tier": "fast"}},
            },
            {
                "openai_service_tier": "default",
                "config_overrides": {"agent": {"openai_service_tier": None}},
            },
            {"model": "openai:direct", "config_overrides": {"agent": []}},
            {"model": "openai:direct", "config_overrides": []},
        )
        for arguments in cases:
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(AdminServiceError) as caught,
            ):
                self.service.create_mcp_session(**arguments)
            self.assertEqual(caught.exception.code, "bad_request")
            self.assertNotIn("private-", str(caught.exception))
        self.command_constructor.assert_not_called()
        self.assertFalse(self.registry.list_sessions())

    def test_profile_is_selected_before_registration_without_changing_default(self):
        self.save_profile("a", "alpha")
        self.save_profile("b", "beta")
        self.profiles.set_default("a")
        seen = []
        create_runtime = self.factory.create

        def observe_registration(settings):
            runtime = create_runtime(settings)
            runtime.event_bus.subscribe(
                "inspect",
                lambda event: seen.append(
                    self.registry.get(event.session_id).session.host_identity.hostname
                ),
            )
            return runtime

        with patch.object(self.factory, "create", side_effect=observe_registration):
            selected = self.service.create_mcp_session(
                username="alice", state_profile="b"
            )
            default = self.service.create_mcp_session(username="bob")
        self.assertEqual((selected.hostname, default.hostname), ("beta", "alpha"))
        self.assertEqual(seen, ["beta", "alpha"])
        self.assertEqual(selected.username, "alice")
        self.assertEqual(self.profiles.current_profile().name, "a")
        self.assertEqual(
            self.registry.get(selected.session_id).session.world_state["hosts_by_name"][
                "beta"
            ]["facts_by_id"]["owner"],
            "alice",
        )

    def test_missing_profile_and_unavailable_overrides_have_no_side_effects(self):
        with self.assertRaises(AdminServiceError) as caught:
            self.service.create_mcp_session(
                state_profile="missing", config_overrides={}
            )
        self.assertEqual(caught.exception.code, "not_found")
        self.command_constructor.assert_not_called()
        self.service.evaluation_factory = None
        for arguments in (
            {"config_overrides": {}},
            {"model": "openai:test"},
            {"openai_service_tier": "default"},
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(AdminServiceError) as caught,
            ):
                self.service.create_mcp_session(**arguments)
            self.assertEqual(caught.exception.code, "unavailable")
        self.assertFalse(self.registry.list_sessions())

    def test_two_sessions_route_independent_models_and_enrichment_events(self):
        a = self.service.create_mcp_session(
            username="alice",
            config_overrides={
                "agent": {"model": "anthropic:variant-a"},
                "spiritguide": {"model": "xai:guide-a"},
                "enrichment": {
                    "expert": {"model": "anthropic:expert-a"},
                    "apply_mode": "shadow",
                },
            },
        )
        b = self.service.create_mcp_session(
            username="bob",
            config_overrides={
                "agent": {"model": "openai:variant-b"},
                "spiritguide": {"model": "openai:guide-b"},
            },
        )
        ra, rb = (self.registry.get(item.session_id).runtime for item in (a, b))
        events_a, events_b = [], []
        ra.event_bus.subscribe("observe", events_a.append)
        rb.event_bus.subscribe("observe", events_b.append)
        for created, model in ((a, "variant-a"), (b, "variant-b")):
            output = self.service.run_mcp_session_command(
                session_id=created.session_id, command="probe"
            )
            self.assertEqual(output.stdout, model + "\n")
            self.assertEqual(output.exit_status, 0)
        self.assertTrue(events_a)
        self.assertTrue(events_b)
        self.assertEqual({event.session_id for event in events_a}, {a.session_id})
        self.assertEqual({event.session_id for event in events_b}, {b.session_id})
        self.assertFalse(self.default_events)
        self.assertEqual(ra.enrichment.settings.expert.model, "anthropic:expert-a")
        self.assertEqual(ra.enrichment.settings.apply_mode, "shadow")
        self.assertEqual(rb.enrichment.settings.apply_mode, "safe")
        self.assertIsNot(ra.backend.agent, rb.backend.agent)
        self.assertEqual(ra.backend.agent.kwargs["api_key"], "private-anthropic-key")
        guide = self.service.run_spiritguide(
            session_id=a.session_id, instruction="test", dry_run=True
        )
        self.assertEqual(guide.message, "xai:guide-a")
        self.assertNotEqual(
            a.evaluation_config["sha256"], b.evaluation_config["sha256"]
        )
        self.assertNotIn("private-", json.dumps(a.evaluation_config))
        a.evaluation_config["settings"]["agent"]["model"] = "tampered"
        self.assertEqual(
            ra.snapshot["settings"]["agent"]["model"], "anthropic:variant-a"
        )
        self.service.close_live_sessions(
            session_ids=[a.session_id], origin="mcp", dry_run=True
        )
        self.assertTrue(ra.enrichment.status().started)
        closed = self.service.close_live_sessions(
            session_ids=[a.session_id], origin="mcp", dry_run=False
        )
        self.assertEqual(closed.closed, [a.session_id])
        self.assertFalse(ra.enrichment.status().started)
        self.assertTrue(rb.enrichment.status().started)
        self.assertIsNotNone(self.registry.get(b.session_id))

    def test_apply_preserves_identity_provenance_config_and_cleanup(self):
        self.save_profile("fresh", "beta")
        created = self.service.create_mcp_session(
            username="alice",
            label="a",
            tags=["test"],
            metadata={"variant": "a"},
            config_overrides={},
        )
        registration = self.registry.get(created.session_id)
        self.service.apply_state_profile(name="fresh", session_id=created.session_id)
        self.assertEqual(registration.session.username, "alice")
        self.assertEqual(registration.session.login_username, "alice")
        self.assertEqual(
            registration.session.environment_metadata.connection.protocol, "mcp"
        )
        self.assertEqual(
            registration.session.environment_metadata.metadata[EVALUATION_CONFIG_KEY],
            created.evaluation_config,
        )
        self.assertEqual(
            self.service.live_sessions(label="a", tags=["test"], origin="mcp")[
                0
            ].session_id,
            created.session_id,
        )
        self.assertEqual(
            self.service.run_mcp_session_command(
                session_id=created.session_id, command="whoami"
            ).stdout,
            "alice\n",
        )
        self.assertEqual(
            self.service.close_live_sessions(
                session_ids=[created.session_id], origin="mcp", dry_run=False
            ).closed,
            [created.session_id],
        )

    def test_exact_restore_keeps_connection_but_restores_effective_user(self):
        source = SessionState(
            username="root",
            login_username="source",
            langfuse_trace_id="source",
            world_state=server_profile().world_state,
        )
        source.environment_metadata.metadata.update(
            {SESSION_ORIGIN_METADATA_KEY: "ssh", SESSION_LABEL_METADATA_KEY: "source"}
        )
        profile = create_state_profile(source, name="snapshot", level="exact")
        target = SessionState(
            username="alice", login_username="alice", langfuse_trace_id="target"
        )
        target.environment_metadata = replace(
            target.environment_metadata, connection=ConnectionMetadata(protocol="mcp")
        )
        target.environment_metadata.metadata[SESSION_ORIGIN_METADATA_KEY] = "mcp"
        apply_state_profile_to_session(target, profile)
        self.assertEqual((target.username, target.login_username), ("root", "alice"))
        self.assertEqual(target.environment_metadata.connection.protocol, "mcp")
        self.assertEqual(
            target.environment_metadata.metadata[SESSION_ORIGIN_METADATA_KEY], "mcp"
        )
        self.assertNotIn(
            SESSION_LABEL_METADATA_KEY, target.environment_metadata.metadata
        )

    def test_configuration_is_in_traces_but_not_model_prompts(self):
        created = self.service.create_mcp_session(
            config_overrides={"agent": {"model": "openai:configuration-marker"}}
        )
        session = self.registry.get(created.session_id).session
        trace = session_start_trace_metadata(session)
        self.assertEqual(trace[EVALUATION_CONFIG_KEY], created.evaluation_config)
        context = self.backend._context_from_session(session, "probe")
        self.assertNotIn("configuration-marker", _format_command_context(context))
        guide_context = SpiritguideContext(
            instruction="test", session_id=created.session_id, session=session
        )
        self.assertNotIn(
            "configuration-marker", _format_spiritguide_context(guide_context)
        )

    def test_detached_spiritguide_overrides_create_update_fork_and_dry_run(self):
        overrides = {
            "spiritguide": {"model": "xai:detached-a", "max_output_tokens": 2048}
        }
        preview = self.service.create_spiritguide_profile(
            name="preview",
            instruction="build",
            dry_run=True,
            config_overrides=overrides,
        )
        self.assertFalse(preview.saved)
        self.assertFalse(self.profiles.store.path_for("preview").exists())
        created = self.service.create_spiritguide_profile(
            name="world-a", instruction="build", config_overrides=overrides
        )
        self.assertTrue(created.saved)
        self.assertEqual(
            created.metadata["evaluation_config"]["settings"]["spiritguide"]["model"],
            "xai:detached-a",
        )
        self.assertEqual(len(created.metadata["session_id"]), 32)
        updated = self.service.update_spiritguide_profile(
            name="world-a",
            instruction="extend",
            config_overrides={"spiritguide": {"model": "anthropic:detached-b"}},
        )
        self.assertEqual(updated.message, "anthropic:detached-b")
        forked = self.service.fork_spiritguide_profile(
            source_name="world-a",
            name="world-b",
            instruction="fork",
            config_overrides=overrides,
        )
        self.assertTrue(forked.saved)
        self.assertIsNone(self.profiles.current_profile())
        self.assertFalse(self.registry.list_sessions())
        self.command_constructor.assert_not_called()
        with self.assertRaises(AdminServiceError):
            self.service.create_spiritguide_profile(
                name="bad",
                instruction="build",
                config_overrides={"agent": {"model": "openai:unused"}},
            )

    def test_construction_failure_does_not_register_or_start_enrichment(self):
        self.guide_constructor.side_effect = RuntimeError("private-construction-error")
        with self.assertRaises(AdminServiceError) as caught:
            self.service.create_mcp_session(config_overrides={})
        self.assertNotIn("private-construction-error", str(caught.exception))
        self.assertFalse(self.registry.list_sessions())
        self.enrichment_constructor.assert_not_called()

    def test_real_agent_constructors_receive_resolved_provider_controls(self):
        from dreamland.agent import LangGraphCommandAgent
        from dreamland.spiritguide import ModelSpiritguide
        from tests.test_backend import RecordingModel

        command_model = RecordingModel(
            '{"stdout":"configured\\n","stderr":"","exit_status":0,"state_update":{}}'
        )
        guide_model = RecordingModel('{"message":"configured","state_update":{}}')
        with (
            patch(
                "dreamland.evaluation_runtime.LangGraphCommandAgent",
                LangGraphCommandAgent,
            ),
            patch("dreamland.evaluation_runtime.ModelSpiritguide", ModelSpiritguide),
            patch(
                "dreamland.agent_core.build_chat_model", return_value=command_model
            ) as command_builder,
            patch(
                "dreamland.spiritguide.build_chat_model", return_value=guide_model
            ) as guide_builder,
        ):
            created = self.service.create_mcp_session(
                model="openai:gpt-6-astra",
                openai_service_tier="flex",
                config_overrides={
                    "agent": {
                        "reasoning_effort": "max",
                        "openai_api": "responses",
                        "max_output_tokens": 8192,
                    },
                    "spiritguide": {
                        "model": "xai:grok-test",
                        "max_output_tokens": 2048,
                    },
                },
            )
            output = self.service.run_mcp_session_command(
                session_id=created.session_id, command="probe"
            )
            self.assertEqual(output.stdout, "configured\n")
            config = command_builder.call_args.args[0]
            self.assertEqual(
                (
                    config.provider,
                    config.model,
                    config.reasoning_effort,
                    config.openai_service_tier,
                    config.max_output_tokens,
                ),
                ("openai", "gpt-6-astra", "max", "flex", 8192),
            )
            self.assertEqual(guide_builder.call_args.args[0].provider, "xai")
            self.assertEqual(guide_builder.call_args.args[0].max_output_tokens, 2048)
            self.assertNotIn(
                EVALUATION_CONFIG_KEY, command_model.messages[0][-1].content
            )

    def test_enrichment_start_failure_cleans_up_runtime(self):
        with (
            patch(
                "dreamland.evaluation_runtime.EnrichmentRuntimeService.start",
                side_effect=RuntimeError("startup failed"),
            ),
            patch("dreamland.evaluation_runtime.EnrichmentRuntimeService.stop") as stop,
        ):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                self.service.create_mcp_session(config_overrides={})
            stop.assert_called_once_with(wait=False)
        self.assertFalse(self.registry.list_sessions())


class RecordingAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def respond(self, context):
        return CommandResult(stdout=self.kwargs["model_name"] + "\n")


class RecordingGuide:
    def __init__(self, settings, **kwargs):
        self.settings = settings

    def respond(self, context):
        return SpiritguideResult(
            message=self.settings.model,
            state_update=CommandStateUpdate(
                world_state={
                    "entry_host": "lab",
                    "active_host": "lab",
                    "hosts_by_name": {"lab": {"hostname": "lab", "login_banner": ""}},
                }
            ),
            metadata={"structured_response_accepted": True},
        )
