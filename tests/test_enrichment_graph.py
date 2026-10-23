from __future__ import annotations

import json
import unittest
from copy import deepcopy

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.callbacks.manager import CallbackManager
from langfuse._utils.serializer import EventSerializer

from dreamland.background_agents.authority import ForegroundAuthoritySnapshot
from dreamland.background_agents.enrichment.context import (
    project_world_state_for_enrichment,
)
from dreamland.background_agents.enrichment.graph import (
    EnrichmentAgent,
    EnrichmentGraphReason,
    EnrichmentGraphStatus,
    EnrichmentJobContext,
    RoleOutputValidationError,
    parse_coordinator_decision,
    parse_expert_report,
    parse_expert_report_with_relation_salvage,
)
from dreamland.background_agents.enrichment.modes import EnrichmentMode
from dreamland.background_agents.enrichment.patching import apply_enrichment_patch
from dreamland.config import EnrichmentRoleSettings, EnrichmentSettings
from dreamland.prompt_loader import load_prompt


class RecordingModel:
    def __init__(self, response: str, role: str, order: list[str]) -> None:
        self.response = response
        self.role = role
        self.order = order
        self.calls: list[tuple[object, object]] = []

    def invoke(self, messages, config=None):
        self.order.append(self.role)
        self.calls.append((messages, config))
        return ModelResponse(self.response)


class FailingModel:
    def invoke(self, messages, config=None):
        raise RuntimeError("provider unavailable")


class RateLimitError(RuntimeError):
    pass


class RateLimitedModel:
    def invoke(self, messages, config=None):
        raise RateLimitError("provider budget exhausted")


class CreditBalanceError(RuntimeError):
    status_code = 429
    body = {"error": {"code": "credit_balance_exhausted"}}


class CreditExhaustedModel:
    def invoke(self, messages, config=None):
        raise CreditBalanceError("provider account detail")


class ModelResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class ChainRunRecorder(BaseCallbackHandler):
    def __init__(self) -> None:
        self.starts = []
        self.ends = []

    def on_chain_start(self, serialized, inputs, **kwargs):
        self.starts.append((serialized, inputs, kwargs))

    def on_chain_end(self, outputs, **kwargs):
        self.ends.append((outputs, kwargs))


def coordinator_work() -> str:
    return json.dumps(
        {
            "v": 2,
            "status": "work",
            "surface": "process",
            "reason": "Process activity is likely next",
            "expertise": {
                "domain": "host process ecology",
                "perspective": "operating-system runtime analyst",
            },
            "expert_task": "Identify one plausible process supporting this service",
            "anchors": ["r2"],
        },
        separators=(",", ":"),
    )


def coordinator_no_op() -> str:
    return json.dumps(
        {
            "v": 2,
            "status": "no_op",
            "surface": None,
            "reason": "Surface already grounded",
            "expertise": None,
            "expert_task": None,
            "anchors": [],
        },
        separators=(",", ":"),
    )


def expert_ok() -> str:
    return json.dumps(
        {
            "v": 2,
            "status": "ok",
            "reason": None,
            "conditions": [],
            "items": [
                {
                    "id": "p1",
                    "kind": "process",
                    "name": "redis-server",
                    "detail": "Native server runtime behind the observed service",
                    "origin": "selected",
                    "basis": ["r2"],
                }
            ],
            "gates": [],
            "relations": [["p1", "implements", "@r2"]],
        },
        separators=(",", ":"),
    )


def expert_insufficient() -> str:
    return json.dumps(
        {
            "v": 2,
            "status": "insufficient",
            "reason": "No bounded candidate is supported",
            "conditions": [],
            "items": [],
            "gates": [],
            "relations": [],
        },
        separators=(",", ":"),
    )


def expert_conditional_only() -> str:
    payload = json.loads(expert_ok())
    payload["conditions"] = [["native", "The service uses a native package"]]
    payload["gates"] = [["p1", ["native"]]]
    return json.dumps(payload, separators=(",", ":"))


def expert_mixed() -> str:
    payload = json.loads(expert_ok())
    payload["conditions"] = [["native", "The service uses a native package"]]
    payload["items"].append(
        {
            "id": "p2",
            "kind": "process",
            "name": "package-specific-helper",
            "detail": "Helper present only for one installation method",
            "origin": "derived",
            "basis": ["r2"],
        }
    )
    payload["gates"] = [["p2", ["native"]]]
    payload["relations"].append(["p2", "supports", "p1"])
    return json.dumps(payload, separators=(",", ":"))


def integrator_patch(
    *,
    target_host: str = "cache",
    clusters: int = 1,
    source_item_ids: tuple[str, ...] = ("p1",),
) -> str:
    cluster = {
        "id": "native-redis-runtime",
        "dependencies": [
            {
                "path": ["services_by_name", "redis"],
                "expected": {"protocol": "tcp", "state": "running"},
            }
        ],
        "contributions": [
            {
                "id": "redis-process-fact",
                "registry_path": ["facts_by_id"],
                "key": "process.redis-server",
                "operation": "put_if_absent",
                "value": {
                    "kind": "process",
                    "name": "redis-server",
                    "description": "Native Redis server runtime",
                },
                "source_item_ids": list(source_item_ids),
            }
        ],
    }
    return json.dumps(
        {
            "v": 2,
            "status": "patch",
            "target_host": target_host,
            "clusters": [cluster for _index in range(clusters)],
        },
        separators=(",", ":"),
    )


def integrator_no_op() -> str:
    return json.dumps(
        {
            "v": 2,
            "status": "no_op",
            "reason_code": "no_safe_mapping",
        },
        separators=(",", ":"),
    )


def snapshot(*, generation: str, liberty: object = "moderate") -> dict[str, object]:
    return {
        "v": 1,
        "scope": {"target_host": "cache"},
        "world": {
            "active_host": "cache",
            "generation_policy": {
                "liberty": liberty,
            },
            "simulation": {"missing_detail_policy": "infer_plausible_details"},
            "expansion_directives_by_id": {
                "test": {"description": "TEST-DIRECTIVE-CANARY"},
            },
            "hosts_by_name": {
                "cache": {
                    "hostname": "redis-01",
                    "services_by_name": {
                        "redis": {"state": "running", "protocol": "tcp"}
                    },
                    "facts_by_id": {"snapshot-generation": {"value": generation}},
                }
            },
        },
    }


def job_context(**overrides: object) -> EnrichmentJobContext:
    values: dict[str, object] = {
        "session_id": "session-1",
        "target_host": "cache",
        "trigger_id": "trigger-1",
        "event_id": "event-1",
        "job_id": "job-1",
        "correlation_id": "correlation-1",
        "langfuse_session_id": "trace-session-1",
        "username": "alice",
        "profile_name": "mixed-network-lab",
    }
    values.update(overrides)
    return EnrichmentJobContext(**values)


def foreground_authority(
    *,
    program_name: str = "bash",
) -> ForegroundAuthoritySnapshot:
    command = f"/bin/{program_name}"
    return ForegroundAuthoritySnapshot(
        target_host="cache",
        hostname="redis-01",
        active_username="alice",
        login_username="alice",
        cwd="/srv",
        program_name=program_name,
        program_command=command,
        program_argv=(command,),
        program_prompt_template=None,
        shell_environment=(("SHELL", command), ("USER", "alice")),
        shell_stack_depth=1,
    )


class EnrichmentGraphTests(unittest.TestCase):
    def test_shell_patch_binds_current_foreground_authority_outside_model_schema(
        self,
    ) -> None:
        order: list[str] = []
        coordinator_payload = json.loads(coordinator_work())
        coordinator_payload["surface"] = "shell"
        coordinator = RecordingModel(
            json.dumps(coordinator_payload, separators=(",", ":")),
            "coordinator",
            order,
        )
        expert = RecordingModel(expert_ok(), "expert", order)
        integrator = RecordingModel(integrator_patch(), "integrator", order)
        agent = EnrichmentAgent(
            coordinator_model=coordinator,
            expert_model=expert,
            integrator_model=integrator,
        )
        world = snapshot(generation="initial")["world"]
        initial_authority = foreground_authority(program_name="bash")
        current_authority = foreground_authority(program_name="sh")
        initial = project_world_state_for_enrichment(
            world,
            target_host="cache",
            foreground_authority=initial_authority,
        )
        current = project_world_state_for_enrichment(
            world,
            target_host="cache",
            foreground_authority=current_authority,
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=initial,
            capture_current_snapshot=lambda _context: current,
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        assert result.patch is not None
        dependency = result.patch.clusters[0].authority_dependencies[0]
        self.assertEqual(dependency.domain, "active_session")
        self.assertEqual(
            dependency.expected_sha256,
            current_authority.session_semantics_sha256,
        )
        self.assertNotIn("authority_dependencies", result.patch.to_payload())
        integrator_payload = json.loads(integrator.calls[0][0][-1].content)
        self.assertEqual(
            integrator.calls[0][0][0].content,
            load_prompt("enrichment_integrator.md"),
        )
        self.assertEqual(
            integrator_payload["foreground_authority"]["interactive_program"]["name"],
            "sh",
        )

    def test_focused_process_patch_binds_empty_foreground_evidence_only(
        self,
    ) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )
        world = snapshot(generation="initial")["world"]
        projected = project_world_state_for_enrichment(
            world,
            target_host="cache",
            foreground_authority=foreground_authority(),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=projected,
            capture_current_snapshot=lambda _context: projected,
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        assert result.patch is not None
        dependencies = result.patch.clusters[0].authority_dependencies
        self.assertEqual(
            [item.domain for item in dependencies], ["foreground_evidence"]
        )
        self.assertEqual(
            dependencies[0].expected_sha256,
            projected.foreground_evidence_sha256,
        )

    def test_ambient_patch_binds_current_foreground_evidence_outside_model_schema(
        self,
    ) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )
        world = snapshot(generation="initial")["world"]
        world["hosts_by_name"]["cache"]["command_history"] = [
            {
                "id": "time-1",
                "cwd": "/srv",
                "command": "date",
                "stdout": "Thu Nov 12 16:38:27 GST 2318\n",
                "stderr": "",
                "exit_status": 0,
                "interrupted": False,
                "signal": None,
            }
        ]
        projected = project_world_state_for_enrichment(world, target_host="cache")
        recorder = ChainRunRecorder()

        result = agent.invoke(
            job_context(mode=EnrichmentMode.AMBIENT_PRESENCE),
            initial_snapshot=projected,
            capture_current_snapshot=lambda _context: projected,
            callbacks=[recorder],
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        assert result.patch is not None
        dependencies = result.patch.clusters[0].authority_dependencies
        self.assertEqual(
            [item.domain for item in dependencies], ["foreground_evidence"]
        )
        self.assertEqual(
            dependencies[0].expected_sha256,
            projected.foreground_evidence_sha256,
        )
        self.assertNotIn("authority_dependencies", result.patch.to_payload())
        self.assertNotIn(
            projected.foreground_evidence_sha256,
            json.dumps(
                {"starts": recorder.starts, "ends": recorder.ends},
                cls=EventSerializer,
            ),
        )

    def test_service_ssh_shape_binds_current_foreground_authority(self) -> None:
        order: list[str] = []
        coordinator_payload = json.loads(coordinator_work())
        coordinator_payload["surface"] = "process"
        patch_payload = json.loads(integrator_patch())
        contribution = patch_payload["clusters"][0]["contributions"][0]
        contribution["key"] = "sshd_match_rule"
        contribution["value"] = {
            "kind": "sshd_match_rule",
            "force_command": "/usr/local/bin/catalogue-session",
        }
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(
                json.dumps(coordinator_payload, separators=(",", ":")),
                "coordinator",
                order,
            ),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(
                json.dumps(patch_payload, separators=(",", ":")),
                "integrator",
                order,
            ),
        )
        world = snapshot(generation="initial")["world"]
        authority = foreground_authority()
        projected = project_world_state_for_enrichment(
            world,
            target_host="cache",
            foreground_authority=authority,
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=projected,
            capture_current_snapshot=lambda _context: projected,
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        assert result.patch is not None
        dependency = result.patch.clusters[0].authority_dependencies[0]
        self.assertEqual(
            dependency.expected_sha256,
            authority.session_semantics_sha256,
        )

    def test_private_authority_digest_is_absent_from_traced_graph_state(self) -> None:
        order: list[str] = []
        coordinator_payload = json.loads(coordinator_work())
        coordinator_payload["surface"] = "shell"
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(
                json.dumps(coordinator_payload, separators=(",", ":")),
                "coordinator",
                order,
            ),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(
                integrator_patch(),
                "integrator",
                order,
            ),
        )
        authority = foreground_authority(program_name="bash")
        projected = project_world_state_for_enrichment(
            snapshot(generation="initial")["world"],
            target_host="cache",
            foreground_authority=authority,
        )
        recorder = ChainRunRecorder()

        result = agent.invoke(
            job_context(),
            initial_snapshot=projected,
            capture_current_snapshot=lambda _context: projected,
            callbacks=[recorder],
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        assert result.patch is not None
        self.assertTrue(result.patch.clusters[0].authority_dependencies)
        serialized_trace_values = json.dumps(
            {"starts": recorder.starts, "ends": recorder.ends},
            cls=EventSerializer,
        )
        self.assertNotIn(
            authority.session_semantics_sha256,
            serialized_trace_values,
        )

    def test_full_flow_refreshes_snapshot_between_expert_and_integrator(self) -> None:
        order: list[str] = []
        coordinator = RecordingModel(coordinator_work(), "coordinator", order)
        expert = RecordingModel(expert_ok(), "expert", order)
        integrator = RecordingModel(integrator_patch(), "integrator", order)
        agent = EnrichmentAgent(
            coordinator_model=coordinator,
            expert_model=expert,
            integrator_model=integrator,
            coordinator_model_name="coordinator-fast",
            expert_model_name="expert-fast",
            integrator_model_name="integrator-fast",
        )

        def capture(context: EnrichmentJobContext):
            self.assertEqual(context.job_id, "job-1")
            order.append("snapshot_current")
            return snapshot(generation="current")

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            result = agent.invoke(
                job_context(
                    surface_hint="network",
                    interaction_focus={
                        "cwd": "/home/alice",
                        "paths": ["/home/alice/reports"],
                        "primary": {
                            "kind": "path",
                            "id": "/home/alice/reports",
                        },
                        "key": "path:/home/alice/reports",
                    },
                ),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=capture,
            )

        self.assertEqual(
            order,
            ["coordinator", "expert", "snapshot_current", "integrator"],
        )
        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertEqual(result.reason_code, EnrichmentGraphReason.PATCH_PROPOSED)
        self.assertIsNotNone(result.patch)
        assert result.patch is not None
        self.assertEqual(result.patch.target_host, "cache")
        self.assertEqual(
            result.patch.basis_host["facts_by_id"]["snapshot-generation"]["value"],
            "current",
        )
        self.assertEqual(
            result.patch.trusted_dependency_hashes[0][0],
            ("services_by_name", "redis"),
        )
        self.assertEqual(len(result.patch.trusted_dependency_hashes[0][1]), 64)
        self.assertNotIn("trusted_dependency_hashes", result.patch.to_payload())
        self.assertNotEqual(
            result.initial_snapshot_sha256,
            result.current_snapshot_sha256,
        )
        integrator_payload = json.loads(integrator.calls[0][0][-1].content)
        self.assertEqual(
            integrator.calls[0][0][0].content,
            load_prompt("enrichment_integrator.md"),
        )
        self.assertEqual(
            integrator_payload["current_snapshot"]["world"]["hosts_by_name"]["cache"][
                "facts_by_id"
            ]["snapshot-generation"]["value"],
            "current",
        )
        coordinator_payload = json.loads(coordinator.calls[0][0][-1].content)
        self.assertEqual(
            set(coordinator_payload),
            {
                "enrichment_mode",
                "coverage_summary",
                "host_time",
                "recent_exhaustive_evidence",
                "target_host",
                "wake_reason",
                "operator_bounds",
                "available_references",
                "materialized_state",
            },
        )
        self.assertEqual(
            coordinator_payload["enrichment_mode"],
            "focused",
        )
        self.assertEqual(
            coordinator_payload["host_time"],
            {"v": 1, "status": "unknown"},
        )
        self.assertEqual(coordinator_payload["recent_exhaustive_evidence"], [])
        self.assertEqual(
            coordinator_payload["coverage_summary"],
            {
                "facts": 1,
                "filesystem_artifacts": 0,
                "groups": 0,
                "services": 1,
                "users": 0,
            },
        )
        self.assertEqual(
            coordinator_payload["operator_bounds"]["generation_policy"]["liberty"],
            "moderate",
        )
        self.assertEqual(
            coordinator_payload["wake_reason"]["interaction_focus"]["paths"],
            ["/home/alice/reports"],
        )
        self.assertNotIn("job_id", json.dumps(coordinator_payload))

        expert_payload = json.loads(expert.calls[0][0][-1].content)
        self.assertEqual(
            set(expert_payload),
            {
                "expertise",
                "question",
                "latitude",
                "host",
                "observations",
                "references",
            },
        )
        self.assertEqual(
            set(expert_payload["latitude"]),
            {"instruction", "candidate_limit", "relation_limit"},
        )
        self.assertEqual(
            expert_payload["latitude"]["instruction"],
            (
                "State affirmative, concrete, mutually consistent details from one "
                "realization; do not return inspection advice, possibilities, or "
                "alternative menus. Construct one coherent representative "
                "realization. When several ordinary answers fit, select plausible "
                "concrete details instead of leaving every choice unresolved. "
                "Develop supporting observable context around the focal detail when "
                "it fits."
            ),
        )
        self.assertEqual(expert_payload["latitude"]["candidate_limit"], 8)
        self.assertEqual(expert_payload["latitude"]["relation_limit"], 8)
        self.assertEqual(
            expert_payload["expertise"],
            {
                "domain": "host process ecology",
                "perspective": "operating-system runtime analyst",
            },
        )
        self.assertEqual(expert_payload["host"]["hostname"], "redis-01")
        self.assertEqual(
            expert_payload["references"],
            [
                {
                    "id": "r2",
                    "kind": "service",
                    "label": "redis",
                    "observation": {"protocol": "tcp", "state": "running"},
                }
            ],
        )
        serialized_expert = json.dumps(expert_payload).lower()
        for forbidden in (
            "dreamland",
            "generation_policy",
            "simulation",
            "expansion_directives_by_id",
            "hosts_by_name",
            "job_id",
            "trigger_id",
            "event_id",
            "coordinator",
            "integrator",
        ):
            self.assertNotIn(forbidden, serialized_expert)

        self.assertEqual(
            integrator_payload["integration_scope"],
            {"target_host": "cache", "surface": "process"},
        )
        self.assertEqual(
            integrator_payload["integration_guidance"],
            {
                "instruction": (
                    "Materialize a coherent representative cluster and preserve "
                    "distinct safely mappable artifacts when each adds net-new "
                    "observable substance."
                )
            },
        )
        self.assertNotIn("coordinator", integrator_payload)
        self.assertNotIn("presence_gap", integrator_payload)
        self.assertEqual(
            integrator_payload["eligibility"],
            {
                "eligible_item_ids": ["p1"],
                "blocked_item_count": 0,
                "unresolved_condition_count": 0,
            },
        )
        self.assertNotIn("job", integrator_payload)
        self.assertEqual(
            integrator_payload["reference_bindings"],
            [
                {
                    "id": "r2",
                    "kind": "service",
                    "path": ["services_by_name", "redis"],
                    "expected": {"protocol": "tcp", "state": "running"},
                }
            ],
        )
        model_config = coordinator.calls[0][1]
        self.assertEqual(
            model_config["metadata"]["dreamland.state_profile.name"],
            "mixed-network-lab",
        )
        self.assertNotIn("dreamland.profile", model_config["metadata"])
        self.assertEqual(
            model_config["metadata"]["dreamland.enrichment.job_id"], "job-1"
        )
        self.assertEqual(
            model_config["metadata"]["dreamland.enrichment.role"], "coordinator"
        )
        self.assertEqual(
            model_config["metadata"]["dreamland.enrichment.mode"], "focused"
        )
        self.assertEqual(
            model_config["metadata"]["dreamland.enrichment.coverage_summary"],
            coordinator_payload["coverage_summary"],
        )
        self.assertEqual(
            model_config["metadata"]["langfuse_session_id"], "trace-session-1"
        )
        log_output = "\n".join(captured.output)
        self.assertIn("reference.catalog_prepared", log_output)
        self.assertIn("coordinator.context_prepared", log_output)
        self.assertIn("expert_brief.prepared", log_output)
        self.assertIn("reference.bindings_resolved", log_output)
        self.assertIn("event=graph.started", log_output)
        self.assertIn("graph_version=enrichment-v15", log_output)
        self.assertIn("enrichment_mode=focused", log_output)
        self.assertIn("event=graph.completed", log_output)
        self.assertNotIn("event=job.started", log_output)
        self.assertIn("event=snapshot.accepted", log_output)
        self.assertNotIn("event=snapshot.captured", log_output)
        self.assertIn("event=reference.selection_evaluated", log_output)
        self.assertIn("event=reference.usage_evaluated", log_output)
        self.assertNotIn("selected_ids=", log_output)
        self.assertNotIn("used_ids=", log_output)
        self.assertNotIn("unused_ids=", log_output)
        self.assertNotIn("anchors=", log_output)
        self.assertIn("event=condition.resolution_evaluated", log_output)
        self.assertIn("event=expert.items_evaluated", log_output)
        self.assertIn("gate_count=0", log_output)
        self.assertIn("event=patch.provenance_validated", log_output)
        self.assertIn(
            "event=patch.reference_dependencies_validated",
            log_output,
        )
        self.assertIn("trusted_fingerprint_count=1", log_output)
        self.assertIn("latitude_effective=moderate", log_output)
        self.assertIn("latitude_template_version=creative-latitude-v5", log_output)
        self.assertIn("latitude_instruction_sha256=", log_output)
        self.assertIn("candidate_limit=8", log_output)
        self.assertIn("unused_candidate_capacity=7", log_output)
        self.assertIn("candidate_limit_reached=false", log_output)
        self.assertIn("event=integrator.guidance_prepared", log_output)
        self.assertIn("patch_contribution_limit=24", log_output)
        self.assertIn("integration_instruction_sha256=", log_output)
        self.assertIn("contribution_limit=24", log_output)
        self.assertIn("unused_contribution_capacity=23", log_output)
        self.assertIn("unsourced_eligible_item_count=0", log_output)
        self.assertIn("requested_surface=network", log_output)
        self.assertIn("selected_surface=process", log_output)

    def test_integrator_refresh_reuses_coordinator_and_expert(self) -> None:
        order: list[str] = []
        coordinator = RecordingModel(coordinator_work(), "coordinator", order)
        expert = RecordingModel(expert_ok(), "expert", order)
        integrator = RecordingModel(integrator_patch(), "integrator", order)
        agent = EnrichmentAgent(
            coordinator_model=coordinator,
            expert_model=expert,
            integrator_model=integrator,
        )
        context = job_context()

        def capture_current(_context: EnrichmentJobContext):
            order.append("snapshot_current")
            return snapshot(generation="current")

        previous = agent.invoke(
            context,
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=capture_current,
        )
        self.assertIsNotNone(previous.integrator_refresh_basis)

        def capture_refresh(_context: EnrichmentJobContext):
            order.append("snapshot_refresh")
            return snapshot(generation="refresh")

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            refreshed = agent.refresh_integrator(
                context,
                previous_result=previous,
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=capture_refresh,
                attempt=1,
            )

        self.assertEqual(refreshed.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertEqual(len(coordinator.calls), 1)
        self.assertEqual(len(expert.calls), 1)
        self.assertEqual(len(integrator.calls), 2)
        self.assertEqual(
            order,
            [
                "coordinator",
                "expert",
                "snapshot_current",
                "integrator",
                "snapshot_refresh",
                "integrator",
            ],
        )
        assert refreshed.patch is not None
        self.assertEqual(
            refreshed.patch.basis_host["facts_by_id"]["snapshot-generation"]["value"],
            "refresh",
        )
        refresh_payload = json.loads(integrator.calls[1][0][-1].content)
        self.assertEqual(
            refresh_payload["current_snapshot"]["world"]["hosts_by_name"]["cache"][
                "facts_by_id"
            ]["snapshot-generation"]["value"],
            "refresh",
        )
        refresh_thread_id = f"{context.thread_id}:integrator-refresh:1"
        refresh_config = integrator.calls[1][1]
        self.assertEqual(
            refresh_config["metadata"][
                "dreamland.enrichment.integrator_refresh_thread_id"
            ],
            refresh_thread_id,
        )
        output = "\n".join(captured.output)
        self.assertIn("event=integrator.refresh.graph_started", output)
        self.assertIn(f"thread_id={refresh_thread_id}", output)

    def test_integrator_refresh_topology_is_explicit(self) -> None:
        order: list[str] = []
        model = RecordingModel(coordinator_no_op(), "unused", order)
        agent = EnrichmentAgent(
            coordinator_model=model,
            expert_model=model,
            integrator_model=model,
        )

        mermaid = agent.integrator_refresh_topology_mermaid(expanded=True)

        self.assertIn("snapshot_integrator_refresh", mermaid)
        self.assertIn("resolve_integrator_refresh_references", mermaid)
        self.assertIn("subgraph integrator_refresh_agent", mermaid)
        self.assertIn("call_integrator_refresh_model", mermaid)
        self.assertIn("parse_integrator_refresh_output", mermaid)
        self.assertIn("normalize_integrator_refresh_output", mermaid)
        self.assertIn("validate_integrator_refresh_output", mermaid)

    def test_ambient_integrator_refresh_keeps_human_integration_suffix(self) -> None:
        order: list[str] = []
        coordinator = RecordingModel(coordinator_work(), "ambient", order)
        expert = RecordingModel(expert_ok(), "expert", order)
        integrator = RecordingModel(integrator_patch(), "integrator", order)
        agent = EnrichmentAgent(
            coordinator_model=coordinator,
            expert_model=expert,
            integrator_model=integrator,
        )
        context = job_context(mode=EnrichmentMode.AMBIENT_PRESENCE)
        initial = snapshot(generation="initial")
        previous = agent.invoke(
            context,
            initial_snapshot=initial,
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        refreshed = agent.refresh_integrator(
            context,
            previous_result=previous,
            initial_snapshot=initial,
            capture_current_snapshot=lambda _context: snapshot(generation="refresh"),
        )

        self.assertEqual(refreshed.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertEqual(len(coordinator.calls), 1)
        self.assertEqual(len(expert.calls), 1)
        self.assertEqual(len(integrator.calls), 2)
        expected_system_prompt = (
            f"{load_prompt('enrichment_integrator.md')}\n\n"
            f"{load_prompt('enrichment_ambient_integrator_suffix.md')}"
        )
        self.assertTrue(
            all(
                call[0][0].content == expected_system_prompt
                for call in integrator.calls
            )
        )

    def test_full_projection_basis_fingerprints_omitted_dependency_fields(
        self,
    ) -> None:
        order: list[str] = []
        raw_world = deepcopy(snapshot(generation="current")["world"])
        service = raw_world["hosts_by_name"]["cache"]["services_by_name"]["redis"]
        service["content"] = "omitted operational body"
        initial = project_world_state_for_enrichment(raw_world, target_host="cache")
        current = project_world_state_for_enrichment(raw_world, target_host="cache")
        integrator = RecordingModel(integrator_patch(), "integrator", order)
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=integrator,
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=initial,
            capture_current_snapshot=lambda _context: current,
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        assert result.patch is not None
        integrator_payload = json.loads(integrator.calls[0][0][-1].content)
        projected_service = integrator_payload["current_snapshot"]["world"][
            "hosts_by_name"
        ]["cache"]["services_by_name"]["redis"]
        self.assertNotIn("content", projected_service)
        self.assertNotIn("omitted operational body", repr(result.patch))

        evidence_authority = {
            "foreground_evidence": current.foreground_evidence_sha256,
        }
        applied = apply_enrichment_patch(
            raw_world,
            result.patch,
            current_authority_sha256_by_domain=evidence_authority,
        )
        self.assertEqual(applied.outcome.status, "applied")

        changed = deepcopy(raw_world)
        changed["hosts_by_name"]["cache"]["services_by_name"]["redis"]["late_field"] = (
            "added after Integrator snapshot"
        )
        rejected = apply_enrichment_patch(
            changed,
            result.patch,
            current_authority_sha256_by_domain=evidence_authority,
        )
        self.assertEqual(
            rejected.outcome.clusters[0].reason_code,
            "dependency_changed",
        )

    def test_proposal_graph_inherits_service_parent_callback_run(self) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )
        recorder = ChainRunRecorder()
        callback_manager = CallbackManager.configure(inheritable_callbacks=[recorder])
        parent = callback_manager.on_chain_start(
            {"name": "dreamland-enrichment-agent"},
            {"job_id": "job-1"},
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
            callbacks=parent.get_child(),
        )
        parent.on_chain_end({"graph_status": result.status.value})

        proposal_runs = [
            kwargs
            for _serialized, _inputs, kwargs in recorder.starts
            if kwargs.get("name") == "dreamland-enrichment-proposal-graph"
        ]
        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertEqual(len(proposal_runs), 1)
        self.assertEqual(proposal_runs[0]["parent_run_id"], parent.run_id)

    def test_invalid_auxiliary_relation_is_discarded_before_integrator(self) -> None:
        order: list[str] = []
        expert_payload = json.loads(expert_ok())
        expert_payload["relations"].insert(0, ["@r2", "launches", "@r2"])
        integrator = RecordingModel(integrator_patch(), "integrator", order)
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(
                json.dumps(expert_payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=integrator,
        )

        with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current"
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertEqual(order, ["coordinator", "expert", "integrator"])
        integrator_payload = json.loads(integrator.calls[0][0][-1].content)
        self.assertEqual(
            integrator_payload["expert"]["relations"],
            [["p1", "implements", "@r2"]],
        )
        output = "\n".join(captured.output)
        self.assertIn("event=expert.relation_discarded", output)
        self.assertIn("relation_index=0", output)
        self.assertIn("rule=item_endpoint_required", output)
        self.assertIn("relation_discarded_count=1", output)
        self.assertNotIn("event=role.rejected", output)

    def test_relation_salvage_retains_first_twenty_four_valid_unique_entries(
        self,
    ) -> None:
        order: list[str] = []
        expert_payload = json.loads(expert_ok())
        accepted_relations = [
            ["p1", f"relationship-{index}", "@r2"] for index in range(1, 26)
        ]
        expert_payload["relations"] = [
            ["@r2", "launches", "@r2"],
            accepted_relations[0],
            accepted_relations[0],
            *accepted_relations[1:],
        ]
        integrator = RecordingModel(integrator_patch(), "integrator", order)
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(
                json.dumps(expert_payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=integrator,
        )

        with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(
                    generation="initial",
                    liberty="abundant",
                ),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current",
                    liberty="abundant",
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertEqual(order, ["coordinator", "expert", "integrator"])
        integrator_payload = json.loads(integrator.calls[0][0][-1].content)
        self.assertEqual(
            integrator_payload["expert"]["relations"],
            accepted_relations[:24],
        )
        output = "\n".join(captured.output)
        self.assertIn("event=expert.relations_evaluated", output)
        self.assertIn("relation_policy_version=expert-relations-v2", output)
        self.assertIn("relation_limit=24", output)
        self.assertIn("relation_received_count=27", output)
        self.assertIn("relation_accepted_count=24", output)
        self.assertIn("relation_invalid_count=1", output)
        self.assertIn("relation_duplicate_count=1", output)
        self.assertIn("relation_overflow_count=1", output)
        self.assertIn("relation_discarded_count=3", output)
        self.assertIn("rule=item_endpoint_required", output)
        self.assertIn("rule=duplicate_relation", output)
        self.assertIn("rule=relation_limit_exceeded", output)
        aggregate_events = [
            line
            for line in captured.output
            if "event=expert.relations_evaluated" in line
        ]
        discarded_events = [
            line
            for line in captured.output
            if "event=expert.relation_discarded" in line
        ]
        self.assertEqual(len(aggregate_events), 1)
        self.assertTrue(aggregate_events[0].startswith("WARNING:"))
        self.assertEqual(len(discarded_events), 3)
        self.assertTrue(all(line.startswith("DEBUG:") for line in discarded_events))

    def test_observed_valid_luna_report_shapes_reach_integrator(self) -> None:
        observed_shapes = (
            (23, 16),
            (11, 11),
            (15, 12),
            (17, 10),
            (18, 14),
        )
        for item_count, relation_count in observed_shapes:
            with self.subTest(
                item_count=item_count,
                relation_count=relation_count,
            ):
                order: list[str] = []
                expert_payload = json.loads(expert_ok())
                expert_payload["items"] = [
                    {
                        "id": f"p{index}",
                        "kind": "process",
                        "name": f"reliquary-worker-{index}",
                        "detail": (f"Distinct worker {index} in one connected runtime"),
                        "origin": "selected",
                        "basis": ["r2"],
                    }
                    for index in range(1, item_count + 1)
                ]
                expected_relations = [
                    ["p1", "implements", "@r2"],
                    *[
                        [f"p{index}", "supports", f"p{index - 1}"]
                        for index in range(2, relation_count + 1)
                    ],
                ]
                expert_payload["relations"] = expected_relations
                integrator = RecordingModel(
                    integrator_patch(),
                    "integrator",
                    order,
                )
                agent = EnrichmentAgent(
                    coordinator_model=RecordingModel(
                        coordinator_work(),
                        "coordinator",
                        order,
                    ),
                    expert_model=RecordingModel(
                        json.dumps(expert_payload, separators=(",", ":")),
                        "expert",
                        order,
                    ),
                    integrator_model=integrator,
                )

                result = agent.invoke(
                    job_context(job_id=f"luna-{item_count}-{relation_count}"),
                    initial_snapshot=snapshot(
                        generation="initial",
                        liberty="abundant",
                    ),
                    capture_current_snapshot=lambda _context: snapshot(
                        generation="current",
                        liberty="abundant",
                    ),
                )

                self.assertEqual(
                    result.status,
                    EnrichmentGraphStatus.PATCH_PROPOSED,
                )
                self.assertEqual(order, ["coordinator", "expert", "integrator"])
                integrator_payload = json.loads(integrator.calls[0][0][-1].content)
                self.assertEqual(
                    len(integrator_payload["expert"]["items"]),
                    item_count,
                )
                self.assertEqual(
                    integrator_payload["expert"]["relations"],
                    expected_relations,
                )

    def test_graph_enforces_each_low_latitude_relation_limit(self) -> None:
        limits = {
            "strict": 2,
            "conservative": 4,
            "moderate": 8,
            "expansive": 12,
        }
        for liberty, relation_limit in limits.items():
            with self.subTest(liberty=liberty, relation_limit=relation_limit):
                order: list[str] = []
                expert_payload = json.loads(expert_ok())
                expert_payload["items"][0]["origin"] = "derived"
                authored_relations = [
                    ["p1", f"relationship-{index}", "@r2"]
                    for index in range(1, relation_limit + 2)
                ]
                expert_payload["relations"] = authored_relations
                integrator = RecordingModel(
                    integrator_patch(),
                    "integrator",
                    order,
                )
                agent = EnrichmentAgent(
                    coordinator_model=RecordingModel(
                        coordinator_work(),
                        "coordinator",
                        order,
                    ),
                    expert_model=RecordingModel(
                        json.dumps(expert_payload, separators=(",", ":")),
                        "expert",
                        order,
                    ),
                    integrator_model=integrator,
                )

                with self.assertLogs(
                    "dreamland.enrichment",
                    level="DEBUG",
                ) as captured:
                    result = agent.invoke(
                        job_context(job_id=f"relation-limit-{liberty}"),
                        initial_snapshot=snapshot(
                            generation="initial",
                            liberty=liberty,
                        ),
                        capture_current_snapshot=lambda _context, value=liberty: (
                            snapshot(
                                generation="current",
                                liberty=value,
                            )
                        ),
                    )

                self.assertEqual(
                    result.status,
                    EnrichmentGraphStatus.PATCH_PROPOSED,
                )
                self.assertEqual(order, ["coordinator", "expert", "integrator"])
                integrator_payload = json.loads(integrator.calls[0][0][-1].content)
                self.assertEqual(
                    integrator_payload["expert"]["relations"],
                    authored_relations[:relation_limit],
                )
                aggregate_events = [
                    line
                    for line in captured.output
                    if "event=expert.relations_evaluated" in line
                ]
                self.assertEqual(len(aggregate_events), 1)
                aggregate = aggregate_events[0]
                self.assertTrue(aggregate.startswith("WARNING:"))
                self.assertIn(f"relation_limit={relation_limit}", aggregate)
                self.assertIn(
                    f"relation_received_count={relation_limit + 1}",
                    aggregate,
                )
                self.assertIn(
                    f"relation_accepted_count={relation_limit}",
                    aggregate,
                )
                self.assertIn("relation_invalid_count=0", aggregate)
                self.assertIn("relation_duplicate_count=0", aggregate)
                self.assertIn("relation_overflow_count=1", aggregate)
                self.assertIn("relation_discarded_count=1", aggregate)

    def test_clean_relation_evaluation_is_debug_without_discard_events(self) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )

        with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
            result = agent.invoke(
                job_context(job_id="clean-relations"),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current"
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        aggregate_events = [
            line
            for line in captured.output
            if "event=expert.relations_evaluated" in line
        ]
        report_events = [
            line for line in captured.output if "event=expert.report" in line
        ]
        discarded_events = [
            line
            for line in captured.output
            if "event=expert.relation_discarded" in line
        ]
        self.assertEqual(len(aggregate_events), 1)
        aggregate = aggregate_events[0]
        self.assertTrue(aggregate.startswith("DEBUG:"))
        self.assertIn("relation_policy_version=expert-relations-v2", aggregate)
        self.assertIn("relation_limit=8", aggregate)
        self.assertIn("relation_received_count=1", aggregate)
        self.assertIn("relation_accepted_count=1", aggregate)
        self.assertIn("relation_invalid_count=0", aggregate)
        self.assertIn("relation_duplicate_count=0", aggregate)
        self.assertIn("relation_overflow_count=0", aggregate)
        self.assertIn("relation_discarded_count=0", aggregate)
        self.assertEqual(discarded_events, [])
        self.assertEqual(len(report_events), 1)
        self.assertTrue(report_events[0].startswith("INFO:"))

    def test_coordinator_no_op_skips_expert_snapshot_and_integrator(self) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_no_op(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )
        captures: list[str] = []

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: (
                captures.append("capture") or snapshot(generation="current")
            ),
        )

        self.assertEqual(order, ["coordinator"])
        self.assertEqual(captures, [])
        self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
        self.assertEqual(result.reason_code, EnrichmentGraphReason.COORDINATOR_NO_OP)
        self.assertIsNone(result.patch)

    def test_expert_insufficient_skips_current_snapshot_and_integrator(self) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_insufficient(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: self.fail(
                "current snapshot should not be captured"
            ),
        )

        self.assertEqual(order, ["coordinator", "expert"])
        self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
        self.assertEqual(result.reason_code, EnrichmentGraphReason.EXPERT_INSUFFICIENT)

    def test_conditional_only_report_stops_before_integrator(self) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_conditional_only(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: (
                order.append("snapshot_current") or snapshot(generation="current")
            ),
        )

        self.assertEqual(order, ["coordinator", "expert"])
        self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
        self.assertEqual(
            result.reason_code,
            EnrichmentGraphReason.EXPERT_NO_ELIGIBLE_ITEMS,
        )
        self.assertIsNone(result.patch)

    def test_candidate_origin_eligibility_follows_creative_latitude(self) -> None:
        cases = (
            ("strict", "derived", True),
            ("strict", "selected", False),
            ("conservative", "derived", True),
            ("conservative", "selected", False),
            ("moderate", "selected", True),
            ("expansive", "selected", True),
            ("abundant", "selected", True),
        )
        for liberty, origin, accepted in cases:
            with self.subTest(liberty=liberty, origin=origin):
                order: list[str] = []

                def capture_current(
                    _context: EnrichmentJobContext,
                    *,
                    current_order: list[str] = order,
                    current_liberty: str = liberty,
                ) -> dict[str, object]:
                    current_order.append("snapshot_current")
                    return snapshot(
                        generation="current",
                        liberty=current_liberty,
                    )

                expert_payload = json.loads(expert_ok())
                expert_payload["items"][0]["origin"] = origin
                agent = EnrichmentAgent(
                    coordinator_model=RecordingModel(
                        coordinator_work(), "coordinator", order
                    ),
                    expert_model=RecordingModel(
                        json.dumps(expert_payload, separators=(",", ":")),
                        "expert",
                        order,
                    ),
                    integrator_model=RecordingModel(
                        integrator_patch(), "integrator", order
                    ),
                )

                result = agent.invoke(
                    job_context(),
                    initial_snapshot=snapshot(
                        generation="initial",
                        liberty=liberty,
                    ),
                    capture_current_snapshot=capture_current,
                )

                if accepted:
                    self.assertEqual(
                        result.status,
                        EnrichmentGraphStatus.PATCH_PROPOSED,
                    )
                    self.assertIn("integrator", order)
                else:
                    self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
                    self.assertEqual(
                        result.reason_code,
                        EnrichmentGraphReason.EXPERT_NO_ELIGIBLE_ITEMS,
                    )
                    self.assertEqual(order, ["coordinator", "expert"])

    def test_each_creative_latitude_rejects_limit_plus_one_candidates(self) -> None:
        limits = {
            "strict": 2,
            "conservative": 4,
            "moderate": 8,
            "expansive": 12,
            "abundant": 24,
        }
        for liberty, candidate_limit in limits.items():
            with self.subTest(liberty=liberty, candidate_limit=candidate_limit):
                order: list[str] = []
                expert_payload = json.loads(expert_ok())
                expert_payload["items"] = [
                    {
                        "id": f"p{index}",
                        "kind": "process",
                        "name": f"support-worker-{index}",
                        "detail": f"Distinct supporting runtime candidate {index}",
                        "origin": "derived",
                        "basis": ["r2"],
                    }
                    for index in range(1, candidate_limit + 2)
                ]
                expert_payload["relations"] = []
                agent = EnrichmentAgent(
                    coordinator_model=RecordingModel(
                        coordinator_work(), "coordinator", order
                    ),
                    expert_model=RecordingModel(
                        json.dumps(expert_payload, separators=(",", ":")),
                        "expert",
                        order,
                    ),
                    integrator_model=RecordingModel(
                        integrator_patch(), "integrator", order
                    ),
                )

                result = agent.invoke(
                    job_context(),
                    initial_snapshot=snapshot(
                        generation="initial",
                        liberty=liberty,
                    ),
                    capture_current_snapshot=lambda _context: self.fail(
                        "an over-limit Expert report should stop before capture"
                    ),
                )

                self.assertEqual(order, ["coordinator", "expert"])
                self.assertEqual(result.status, EnrichmentGraphStatus.REJECTED)
                self.assertEqual(
                    result.reason_code,
                    EnrichmentGraphReason.EXPERT_OUTPUT_INVALID,
                )
                self.assertEqual(result.rejection_detail, "limit_exceeded@$.items")

    def test_abundant_flow_accepts_twenty_four_multi_depth_artifacts(self) -> None:
        order: list[str] = []
        coordinator_payload = json.loads(coordinator_work())
        coordinator_payload["surface"] = "filesystem"
        coordinator_payload["expert_task"] = (
            "Describe one connected layer of material below the observed workspace"
        )
        expert_payload = json.loads(expert_ok())
        artifacts = [
            ("/home/operator", "directory"),
            ("/home/operator/.config", "directory"),
            ("/home/operator/.config/field-console", "directory"),
            ("/home/operator/.config/field-console/settings.toml", "file"),
            ("/home/operator/workspace", "directory"),
            ("/home/operator/workspace/relay", "directory"),
            ("/home/operator/workspace/relay/src", "directory"),
            ("/home/operator/workspace/relay/src/main.py", "file"),
            ("/home/operator/workspace/relay/tests", "directory"),
            ("/home/operator/workspace/relay/tests/test_main.py", "file"),
            ("/home/operator/workspace/relay/.cache", "directory"),
            ("/home/operator/workspace/relay/.cache/last-run.json", "file"),
            ("/home/operator/notes", "directory"),
            ("/home/operator/notes/archive", "directory"),
            ("/home/operator/notes/archive/2026", "directory"),
            ("/home/operator/notes/archive/2026/field.txt", "file"),
            ("/home/operator/workspace/relay/docs", "directory"),
            ("/home/operator/workspace/relay/docs/operations", "directory"),
            (
                "/home/operator/workspace/relay/docs/operations/recovery.md",
                "file",
            ),
            ("/home/operator/workspace/relay/tmp", "directory"),
            ("/home/operator/workspace/relay/tmp/worker.pid", "file"),
            ("/home/operator/notes/inbox", "directory"),
            ("/home/operator/notes/inbox/handoff.md", "file"),
            ("/home/operator/.local", "directory"),
        ]
        expert_payload["items"] = [
            {
                "id": f"a{index}",
                "kind": kind,
                "name": path,
                "detail": f"Distinct {kind} in one connected workspace hierarchy",
                "origin": "selected",
                "basis": ["r2"],
            }
            for index, (path, kind) in enumerate(artifacts, start=1)
        ]
        expert_payload["relations"] = []
        patch = json.loads(integrator_patch())
        patch["clusters"][0]["contributions"] = [
            {
                "id": f"artifact-{index}",
                "registry_path": ["filesystem", "artifacts_by_path"],
                "key": path,
                "operation": "put_if_absent",
                "value": {
                    "kind": kind,
                    "description": f"Distinct {kind} in the relay workspace",
                },
                "source_item_ids": [f"a{index}"],
            }
            for index, (path, kind) in enumerate(artifacts, start=1)
        ]
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(
                json.dumps(coordinator_payload, separators=(",", ":")),
                "coordinator",
                order,
            ),
            expert_model=RecordingModel(
                json.dumps(expert_payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=RecordingModel(
                json.dumps(patch, separators=(",", ":")),
                "integrator",
                order,
            ),
        )

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(
                    generation="initial",
                    liberty="abundant",
                ),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current",
                    liberty="abundant",
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertIsNotNone(result.patch)
        self.assertEqual(len(result.patch.clusters[0].contributions), 24)
        log_output = "\n".join(captured.output)
        self.assertIn("candidate_limit=24", log_output)
        self.assertIn("candidate_limit_reached=true", log_output)
        self.assertIn("contribution_limit_reached=true", log_output)
        self.assertIn("source_item_count=24", log_output)
        self.assertIn("filesystem_contribution_count=24", log_output)
        self.assertIn("filesystem_path_segment_depth_min=2", log_output)
        self.assertIn("filesystem_path_segment_depth_max=7", log_output)
        self.assertIn("filesystem_ancestor_linked_count=23", log_output)
        self.assertIn("filesystem_max_proposed_ancestor_chain=6", log_output)

    def test_graph_accepts_disjoint_same_target_merge_with_split_provenance(
        self,
    ) -> None:
        order: list[str] = []
        expert_payload = json.loads(expert_ok())
        expert_payload["items"] = [
            {
                "id": f"p{index}",
                "kind": "fact",
                "name": f"report-field-{index}",
                "detail": f"Distinct supported report field {index}",
                "origin": "selected",
                "basis": ["r2"],
            }
            for index in range(1, 17)
        ]
        expert_payload["relations"] = []
        patch = json.loads(integrator_patch())
        patch["clusters"][0]["contributions"] = [
            {
                "id": "report-fields-first",
                "registry_path": ["facts_by_id"],
                "key": "inspection-report",
                "operation": "merge_missing",
                "value": {
                    "metadata": {
                        f"field-{index}": f"value-{index}" for index in range(1, 9)
                    }
                },
                "source_item_ids": [f"p{index}" for index in range(1, 9)],
            },
            {
                "id": "report-fields-second",
                "registry_path": ["facts_by_id"],
                "key": "inspection-report",
                "operation": "merge_missing",
                "value": {
                    "metadata": {
                        f"field-{index}": f"value-{index}" for index in range(9, 17)
                    }
                },
                "source_item_ids": [f"p{index}" for index in range(9, 17)],
            },
        ]
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(
                json.dumps(expert_payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=RecordingModel(
                json.dumps(patch, separators=(",", ":")),
                "integrator",
                order,
            ),
        )

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(
                    generation="initial",
                    liberty="abundant",
                ),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current",
                    liberty="abundant",
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertIsNotNone(result.patch)
        assert result.patch is not None
        contributions = result.patch.clusters[0].contributions
        self.assertEqual(len(contributions), 2)
        self.assertEqual(len(contributions[0].source_item_ids), 8)
        self.assertEqual(len(contributions[1].source_item_ids), 8)
        self.assertIn(
            "repeated_target_group_count=1",
            "\n".join(captured.output),
        )

    def test_integrator_rejects_twenty_five_contributions(self) -> None:
        order: list[str] = []
        expert_payload = json.loads(expert_ok())
        expert_payload["items"] = [
            {
                "id": f"p{index}",
                "kind": "process",
                "name": f"worker-{index}",
                "detail": f"Distinct worker {index}",
                "origin": "selected",
                "basis": ["r2"],
            }
            for index in range(1, 25)
        ]
        expert_payload["relations"] = []
        patch = json.loads(integrator_patch())
        patch["clusters"][0]["contributions"] = [
            {
                "id": f"worker-{index}",
                "registry_path": ["facts_by_id"],
                "key": f"process.worker-{index}",
                "operation": "put_if_absent",
                "value": {"kind": "process", "name": f"worker-{index}"},
                "source_item_ids": [f"p{min(index, 24)}"],
            }
            for index in range(1, 26)
        ]
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(
                json.dumps(expert_payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=RecordingModel(
                json.dumps(patch, separators=(",", ":")),
                "integrator",
                order,
            ),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial", liberty="abundant"),
            capture_current_snapshot=lambda _context: snapshot(
                generation="current",
                liberty="abundant",
            ),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.REJECTED)
        self.assertEqual(
            result.reason_code,
            EnrichmentGraphReason.INTEGRATOR_LIMIT_EXCEEDED,
        )
        self.assertEqual(result.rejection_detail, "contributions")

    def test_invalid_latitude_fallback_is_logged_without_raw_value(self) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )
        raw_value = "TEST-GENERATION-POLICY-CANARY"

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(
                    generation="initial",
                    liberty=raw_value,
                ),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current",
                    liberty=raw_value,
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        output = "\n".join(captured.output)
        self.assertIn("latitude_configured=invalid", output)
        self.assertIn("latitude_effective=moderate", output)
        self.assertIn("latitude_defaulted=true", output)
        self.assertNotIn(raw_value, output)

    def test_gated_candidate_is_blocked_even_at_abundant_latitude(self) -> None:
        for origin in ("derived", "selected"):
            with self.subTest(origin=origin):
                order: list[str] = []
                payload = json.loads(expert_conditional_only())
                payload["items"][0]["origin"] = origin
                agent = EnrichmentAgent(
                    coordinator_model=RecordingModel(
                        coordinator_work(), "coordinator", order
                    ),
                    expert_model=RecordingModel(
                        json.dumps(payload, separators=(",", ":")),
                        "expert",
                        order,
                    ),
                    integrator_model=RecordingModel(
                        integrator_patch(), "integrator", order
                    ),
                )

                result = agent.invoke(
                    job_context(),
                    initial_snapshot=snapshot(
                        generation="initial",
                        liberty="abundant",
                    ),
                    capture_current_snapshot=lambda _context: self.fail(
                        "a fully gated report should not capture current state"
                    ),
                )

                self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
                self.assertEqual(order, ["coordinator", "expert"])

    def test_same_kind_anchor_restatement_is_blocked(self) -> None:
        order: list[str] = []
        payload = json.loads(expert_ok())
        payload["items"][0].update(
            {
                "kind": "service",
                "name": "redis",
                "detail": "redis",
                "origin": "derived",
            }
        )
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(
                json.dumps(payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )

        with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: self.fail(
                    "an exact restatement should stop before the second snapshot"
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
        self.assertIn("anchor_restatement", "\n".join(captured.output))

    def test_same_kind_anchor_name_with_novel_detail_remains_eligible(self) -> None:
        order: list[str] = []
        payload = json.loads(expert_ok())
        payload["items"][0].update(
            {
                "kind": "service",
                "name": "redis",
                "detail": "Keeps append-only persistence snapshots on local storage",
                "origin": "derived",
            }
        )
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(
                json.dumps(payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)

    def test_same_name_cross_kind_candidate_remains_eligible(self) -> None:
        order: list[str] = []
        payload = json.loads(expert_ok())
        payload["items"][0]["name"] = "redis"
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(
                json.dumps(payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)

    def test_sparse_world_candidate_can_use_host_observations_without_anchor(
        self,
    ) -> None:
        order: list[str] = []
        coordinator_payload = json.loads(coordinator_work())
        coordinator_payload["anchors"] = []
        expert_payload = json.loads(expert_ok())
        expert_payload["items"][0]["basis"] = []
        expert_payload["relations"] = []
        patch = json.loads(integrator_patch())
        patch["clusters"][0]["dependencies"] = []
        integrator = RecordingModel(
            json.dumps(patch, separators=(",", ":")),
            "integrator",
            order,
        )
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(
                json.dumps(coordinator_payload, separators=(",", ":")),
                "coordinator",
                order,
            ),
            expert_model=RecordingModel(
                json.dumps(expert_payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=integrator,
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        integrator_payload = json.loads(integrator.calls[0][0][-1].content)
        self.assertEqual(integrator_payload["reference_bindings"], [])

    def test_integrator_no_op_is_a_valid_graph_no_op(self) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(integrator_no_op(), "integrator", order),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: (
                order.append("snapshot_current") or snapshot(generation="current")
            ),
        )

        self.assertEqual(
            order,
            ["coordinator", "expert", "snapshot_current", "integrator"],
        )
        self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
        self.assertEqual(result.reason_code, EnrichmentGraphReason.INTEGRATOR_NO_OP)
        self.assertIsNone(result.patch)

    def test_unavailable_coordinator_reference_is_rejected_before_expert(self) -> None:
        order: list[str] = []
        decision = json.loads(coordinator_work())
        decision["anchors"] = ["r999"]
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(
                json.dumps(decision, separators=(",", ":")),
                "coordinator",
                order,
            ),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: self.fail(
                "current snapshot should not be captured"
            ),
        )

        self.assertEqual(order, ["coordinator"])
        self.assertEqual(result.status, EnrichmentGraphStatus.REJECTED)
        self.assertEqual(
            result.reason_code,
            EnrichmentGraphReason.EXPERT_BRIEF_INVALID,
        )
        self.assertEqual(result.rejection_detail, "unknown_reference@$.anchors[0]")

    def test_internal_workflow_question_is_rejected_before_expert(self) -> None:
        order: list[str] = []
        decision = json.loads(coordinator_work())
        decision["expert_task"] = (
            "What should the emulator Integrator patch into world state?"
        )
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(
                json.dumps(decision, separators=(",", ":")),
                "coordinator",
                order,
            ),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: self.fail(
                "current snapshot should not be captured"
            ),
        )

        self.assertEqual(order, ["coordinator"])
        self.assertEqual(
            result.reason_code,
            EnrichmentGraphReason.EXPERT_BRIEF_INVALID,
        )
        self.assertEqual(
            result.rejection_detail,
            "internal_vocabulary@$.expert_task",
        )

    def test_reference_removed_during_expert_call_stops_before_integrator(
        self,
    ) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )
        changed = snapshot(generation="current")
        del changed["world"]["hosts_by_name"]["cache"]["services_by_name"]["redis"]

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: (
                    order.append("snapshot_current") or changed
                ),
            )

        self.assertEqual(order, ["coordinator", "expert", "snapshot_current"])
        self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
        self.assertEqual(
            result.reason_code,
            EnrichmentGraphReason.EXPERT_NO_ELIGIBLE_ITEMS,
        )
        output = "\n".join(captured.output)
        self.assertIn("event=reference.currentness_evaluated", output)
        self.assertIn("missing_count=1", output)
        self.assertIn("stage=current_reference_filter", output)

    def test_reference_changed_during_expert_call_blocks_dependent_candidate(
        self,
    ) -> None:
        order: list[str] = []
        changed = snapshot(generation="current")
        changed["world"]["hosts_by_name"]["cache"]["services_by_name"]["redis"][
            "state"
        ] = "stopped"
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: (
                    order.append("snapshot_current") or changed
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
        self.assertEqual(order, ["coordinator", "expert", "snapshot_current"])
        self.assertIn("changed_count=1", "\n".join(captured.output))

    def test_stale_candidate_is_removed_while_current_candidate_continues(self) -> None:
        order: list[str] = []
        coordinator_payload = json.loads(coordinator_work())
        coordinator_payload["anchors"] = ["r1", "r2"]
        expert_payload = json.loads(expert_ok())
        expert_payload["items"].append(
            {
                "id": "p2",
                "kind": "process",
                "name": "host-monitor",
                "detail": "Host-local monitoring activity",
                "origin": "derived",
                "basis": ["r1"],
            }
        )
        expert_payload["relations"].append(["p2", "runs_on", "@r1"])
        current = snapshot(generation="current")
        del current["world"]["hosts_by_name"]["cache"]["hostname"]
        integrator = RecordingModel(integrator_patch(), "integrator", order)
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(
                json.dumps(coordinator_payload, separators=(",", ":")),
                "coordinator",
                order,
            ),
            expert_model=RecordingModel(
                json.dumps(expert_payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=integrator,
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: current,
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        payload = json.loads(integrator.calls[0][0][-1].content)
        self.assertEqual(payload["eligibility"]["eligible_item_ids"], ["p1"])
        self.assertEqual(payload["eligibility"]["blocked_item_count"], 1)
        self.assertNotIn("p2", json.dumps(payload["expert"]))

    def test_strict_invalid_expert_json_is_structurally_rejected(self) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(
                f"```json\n{expert_ok()}\n```", "expert", order
            ),
            integrator_model=RecordingModel(integrator_patch(), "integrator", order),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: self.fail(
                "current snapshot should not be captured"
            ),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.REJECTED)
        self.assertEqual(
            result.reason_code, EnrichmentGraphReason.EXPERT_OUTPUT_INVALID
        )
        self.assertEqual(result.rejection_detail, "invalid_json@line 1 column 1")
        self.assertEqual(order, ["coordinator", "expert"])

    def test_integrator_target_change_is_rejected(self) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(
                integrator_patch(target_host="other"), "integrator", order
            ),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.REJECTED)
        self.assertEqual(
            result.reason_code,
            EnrichmentGraphReason.INTEGRATOR_TARGET_MISMATCH,
        )

    def test_integrator_unknown_source_item_is_rejected(self) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(
                integrator_patch(source_item_ids=("missing",)),
                "integrator",
                order,
            ),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.REJECTED)
        self.assertEqual(
            result.reason_code,
            EnrichmentGraphReason.INTEGRATOR_PROVENANCE_INVALID,
        )
        self.assertIn("source_item_unknown@", result.rejection_detail or "")

    def test_integrator_blocked_source_item_is_rejected(self) -> None:
        order: list[str] = []
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_mixed(), "expert", order),
            integrator_model=RecordingModel(
                integrator_patch(source_item_ids=("p2",)),
                "integrator",
                order,
            ),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.REJECTED)
        self.assertEqual(
            result.reason_code,
            EnrichmentGraphReason.INTEGRATOR_PROVENANCE_INVALID,
        )
        self.assertIn("source_item_blocked@", result.rejection_detail or "")

    def test_mixed_report_can_patch_through_eligible_source_only(self) -> None:
        order: list[str] = []
        integrator = RecordingModel(
            integrator_patch(source_item_ids=("p1",)),
            "integrator",
            order,
        )
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_mixed(), "expert", order),
            integrator_model=integrator,
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertEqual(result.reason_code, EnrichmentGraphReason.PATCH_PROPOSED)
        payload = json.loads(integrator.calls[0][0][-1].content)
        self.assertEqual(
            payload["eligibility"],
            {
                "eligible_item_ids": ["p1"],
                "blocked_item_count": 1,
                "unresolved_condition_count": 1,
            },
        )
        self.assertNotIn("coordinator", payload)
        self.assertEqual(
            payload["expert"]["items"],
            [
                {
                    "id": "p1",
                    "kind": "process",
                    "name": "redis-server",
                    "detail": "Native server runtime behind the observed service",
                    "origin": "selected",
                    "basis": ["r2"],
                }
            ],
        )
        self.assertEqual(
            payload["expert"]["relations"],
            [["p1", "implements", "@r2"]],
        )
        self.assertIsNone(payload["expert"]["reason"])
        for field in ("conditions", "gates"):
            self.assertEqual(payload["expert"][field], [])
        serialized = json.dumps(payload)
        for blocked_content in (
            "p2",
            "package-specific-helper",
            "Present only for one installation method",
            "native",
        ):
            self.assertNotIn(blocked_content, serialized)

    def test_direct_reference_requires_exact_dependency(self) -> None:
        dependency_cases = (
            ([], "required_reference_dependency_missing"),
            (
                [
                    {
                        "path": ["services_by_name", "redis"],
                        "expected": {"protocol": "tcp", "state": "stopped"},
                    }
                ],
                "required_reference_dependency_mismatch",
            ),
        )
        for dependencies, expected_reason in dependency_cases:
            with self.subTest(dependencies=dependencies):
                order: list[str] = []
                patch = json.loads(integrator_patch())
                patch["clusters"][0]["dependencies"] = dependencies
                expert_payload = json.loads(expert_ok())
                expert_payload["relations"] = []
                agent = EnrichmentAgent(
                    coordinator_model=RecordingModel(
                        coordinator_work(), "coordinator", order
                    ),
                    expert_model=RecordingModel(
                        json.dumps(expert_payload, separators=(",", ":")),
                        "expert",
                        order,
                    ),
                    integrator_model=RecordingModel(
                        json.dumps(patch, separators=(",", ":")),
                        "integrator",
                        order,
                    ),
                )

                with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                    result = agent.invoke(
                        job_context(),
                        initial_snapshot=snapshot(generation="initial"),
                        capture_current_snapshot=lambda _context: snapshot(
                            generation="current"
                        ),
                    )

                self.assertEqual(result.status, EnrichmentGraphStatus.REJECTED)
                self.assertEqual(
                    result.reason_code,
                    EnrichmentGraphReason.INTEGRATOR_REFERENCE_DEPENDENCY_INVALID,
                )
                self.assertEqual(
                    result.rejection_detail,
                    f"{expected_reason}@$.clusters[0].dependencies",
                )
                self.assertIn(
                    "event=patch.reference_dependency_rejected",
                    "\n".join(captured.output),
                )

    def test_unused_candidate_basis_is_not_required_by_patch(
        self,
    ) -> None:
        order: list[str] = []
        coordinator_output = json.loads(coordinator_work())
        coordinator_output["anchors"] = ["r1", "r2"]
        expert_output = json.loads(expert_ok())
        expert_output["items"].append(
            {
                "id": "p2",
                "kind": "process",
                "name": "host-monitor",
                "detail": "Host-local monitoring activity",
                "origin": "derived",
                "basis": ["r1"],
            }
        )
        expert_output["relations"].append(["p2", "runs_on", "@r1"])
        integrator = RecordingModel(
            integrator_patch(source_item_ids=("p1",)),
            "integrator",
            order,
        )
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(
                json.dumps(coordinator_output, separators=(",", ":")),
                "coordinator",
                order,
            ),
            expert_model=RecordingModel(
                json.dumps(expert_output, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=integrator,
        )

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current"
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertEqual(result.reason_code, EnrichmentGraphReason.PATCH_PROPOSED)
        payload = json.loads(integrator.calls[0][0][-1].content)
        self.assertEqual(
            [binding["id"] for binding in payload["reference_bindings"]],
            ["r1", "r2"],
        )
        log_output = "\n".join(captured.output)
        self.assertIn("event=patch.reference_dependencies_validated", log_output)
        self.assertIn("required_count=1", log_output)

    def test_candidate_basis_requires_every_dependency_without_relations(
        self,
    ) -> None:
        coordinator_payload = json.loads(coordinator_work())
        coordinator_payload["anchors"] = ["r1", "r2"]
        expert_payload = json.loads(expert_ok())
        expert_payload["items"][0]["basis"] = ["r1", "r2"]
        expert_payload["relations"] = []
        base_patch = json.loads(integrator_patch())
        complete_dependencies = [
            {"path": ["hostname"], "expected": "redis-01"},
            {
                "path": ["services_by_name", "redis"],
                "expected": {"protocol": "tcp", "state": "running"},
            },
        ]
        for dependencies, accepted in (
            (complete_dependencies[1:], False),
            (complete_dependencies, True),
        ):
            with self.subTest(accepted=accepted):
                order: list[str] = []
                patch = json.loads(json.dumps(base_patch))
                patch["clusters"][0]["dependencies"] = dependencies
                agent = EnrichmentAgent(
                    coordinator_model=RecordingModel(
                        json.dumps(coordinator_payload, separators=(",", ":")),
                        "coordinator",
                        order,
                    ),
                    expert_model=RecordingModel(
                        json.dumps(expert_payload, separators=(",", ":")),
                        "expert",
                        order,
                    ),
                    integrator_model=RecordingModel(
                        json.dumps(patch, separators=(",", ":")),
                        "integrator",
                        order,
                    ),
                )

                result = agent.invoke(
                    job_context(),
                    initial_snapshot=snapshot(generation="initial"),
                    capture_current_snapshot=lambda _context: snapshot(
                        generation="current"
                    ),
                )

                self.assertEqual(
                    result.status,
                    EnrichmentGraphStatus.PATCH_PROPOSED
                    if accepted
                    else EnrichmentGraphStatus.REJECTED,
                )
                if not accepted:
                    self.assertEqual(
                        result.reason_code,
                        EnrichmentGraphReason.INTEGRATOR_REFERENCE_DEPENDENCY_INVALID,
                    )

    def test_multiple_source_candidates_require_basis_union(self) -> None:
        order: list[str] = []
        coordinator_payload = json.loads(coordinator_work())
        coordinator_payload["anchors"] = ["r1", "r2"]
        expert_payload = json.loads(expert_ok())
        expert_payload["items"].append(
            {
                "id": "p2",
                "kind": "process",
                "name": "host-monitor",
                "detail": "Host-local monitor supporting service availability",
                "origin": "derived",
                "basis": ["r1"],
            }
        )
        expert_payload["relations"].append(["p2", "runs_on", "@r1"])
        patch = json.loads(integrator_patch(source_item_ids=("p1", "p2")))
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(
                json.dumps(coordinator_payload, separators=(",", ":")),
                "coordinator",
                order,
            ),
            expert_model=RecordingModel(
                json.dumps(expert_payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=RecordingModel(
                json.dumps(patch, separators=(",", ":")),
                "integrator",
                order,
            ),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.REJECTED)
        self.assertEqual(
            result.reason_code,
            EnrichmentGraphReason.INTEGRATOR_REFERENCE_DEPENDENCY_INVALID,
        )
        self.assertIn(
            "required_reference_dependency_missing", result.rejection_detail or ""
        )

    def test_multiple_source_candidates_accept_complete_basis_union(self) -> None:
        order: list[str] = []
        coordinator_payload = json.loads(coordinator_work())
        coordinator_payload["anchors"] = ["r1", "r2"]
        expert_payload = json.loads(expert_ok())
        expert_payload["items"].append(
            {
                "id": "p2",
                "kind": "process",
                "name": "host-monitor",
                "detail": "Host-local monitor supporting service availability",
                "origin": "derived",
                "basis": ["r1"],
            }
        )
        expert_payload["relations"].append(["p2", "runs_on", "@r1"])
        patch = json.loads(integrator_patch(source_item_ids=("p1",)))
        patch["clusters"][0]["dependencies"].append(
            {"path": ["hostname"], "expected": "redis-01"}
        )
        patch["clusters"][0]["contributions"].append(
            {
                "id": "host-monitor-fact",
                "registry_path": ["facts_by_id"],
                "key": "process.host-monitor",
                "operation": "put_if_absent",
                "value": {
                    "kind": "process",
                    "name": "host-monitor",
                    "description": "Host-local monitoring runtime",
                },
                "source_item_ids": ["p2"],
            }
        )
        integrator = RecordingModel(
            json.dumps(patch, separators=(",", ":")),
            "integrator",
            order,
        )
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(
                json.dumps(coordinator_payload, separators=(",", ":")),
                "coordinator",
                order,
            ),
            expert_model=RecordingModel(
                json.dumps(expert_payload, separators=(",", ":")),
                "expert",
                order,
            ),
            integrator_model=integrator,
        )

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current"
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertEqual(result.reason_code, EnrichmentGraphReason.PATCH_PROPOSED)
        integrator_payload = json.loads(integrator.calls[0][0][-1].content)
        self.assertEqual(
            [binding["id"] for binding in integrator_payload["reference_bindings"]],
            ["r1", "r2"],
        )
        output = "\n".join(captured.output)
        self.assertIn("event=patch.reference_dependencies_validated", output)
        self.assertIn("required_count=2", output)
        self.assertIn("sourced_candidate_count=2", output)

    def test_already_satisfied_validation_copy_becomes_no_op(self) -> None:
        order: list[str] = []
        patch = json.loads(integrator_patch())
        contribution = patch["clusters"][0]["contributions"][0]
        contribution["key"] = "snapshot-generation"
        contribution["value"] = {"value": "current"}
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(
                json.dumps(patch, separators=(",", ":")),
                "integrator",
                order,
            ),
        )

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current"
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
        self.assertEqual(result.reason_code, EnrichmentGraphReason.INTEGRATOR_NO_OP)
        output = "\n".join(captured.output)
        self.assertIn("event=patch.validated", output)
        self.assertIn("reason_code=already_materialized", output)
        self.assertIn("stage=patch_validation", output)
        self.assertIn("validation_copy_outcome=already_satisfied", output)

    def test_integrator_normalization_prunes_array_and_preserves_safe_siblings(
        self,
    ) -> None:
        order: list[str] = []
        forbidden = json.loads(integrator_patch())
        forbidden["clusters"][0]["contributions"][0]["value"]["ports"] = [6379]
        agent = EnrichmentAgent(
            coordinator_model=RecordingModel(coordinator_work(), "coordinator", order),
            expert_model=RecordingModel(expert_ok(), "expert", order),
            integrator_model=RecordingModel(
                json.dumps(forbidden, separators=(",", ":")),
                "integrator",
                order,
            ),
        )

        with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current"
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertEqual(result.reason_code, EnrichmentGraphReason.PATCH_PROPOSED)
        self.assertIsNotNone(result.patch)
        assert result.patch is not None
        value = result.patch.clusters[0].contributions[0].value
        self.assertNotIn("ports", value)
        self.assertEqual(value["name"], "redis-server")
        output = "\n".join(captured.output)
        self.assertIn("event=integrator.output_normalized", output)
        self.assertIn("pruned_array_field_count=1", output)
        self.assertIn("event=integrator.contribution_normalized", output)
        self.assertEqual(order, ["coordinator", "expert", "integrator"])

    def test_integrator_normalization_all_pruned_becomes_no_supported_no_op(
        self,
    ) -> None:
        order: list[str] = []
        unsupported = json.loads(integrator_patch())
        unsupported["clusters"][0]["contributions"][0]["value"] = {"ports": [6379]}
        coordinator = RecordingModel(coordinator_work(), "coordinator", order)
        expert = RecordingModel(expert_ok(), "expert", order)
        integrator = RecordingModel(
            json.dumps(unsupported, separators=(",", ":")),
            "integrator",
            order,
        )
        agent = EnrichmentAgent(
            coordinator_model=coordinator,
            expert_model=expert,
            integrator_model=integrator,
        )

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current"
                ),
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
        self.assertEqual(result.reason_code, EnrichmentGraphReason.INTEGRATOR_NO_OP)
        self.assertIsNone(result.patch)
        self.assertEqual(len(coordinator.calls), 1)
        self.assertEqual(len(expert.calls), 1)
        self.assertEqual(len(integrator.calls), 1)
        output = "\n".join(captured.output)
        self.assertIn("all_contributions_pruned=true", output)
        self.assertIn("reason_code=no_supported_contribution", output)

    def test_current_snapshot_capture_and_validation_have_distinct_reasons(
        self,
    ) -> None:
        def build_agent() -> EnrichmentAgent:
            order: list[str] = []
            return EnrichmentAgent(
                coordinator_model=RecordingModel(
                    coordinator_work(), "coordinator", order
                ),
                expert_model=RecordingModel(expert_ok(), "expert", order),
                integrator_model=RecordingModel(
                    integrator_patch(), "integrator", order
                ),
            )

        def capture_failure(_context: EnrichmentJobContext):
            raise RuntimeError("lock unavailable")

        capture_failed = build_agent().invoke(
            job_context(job_id="capture-failed"),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=capture_failure,
        )
        invalid_snapshot = build_agent().invoke(
            job_context(job_id="snapshot-invalid"),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: {"world": {}},
        )

        self.assertEqual(
            capture_failed.reason_code,
            EnrichmentGraphReason.CURRENT_SNAPSHOT_CAPTURE_FAILED,
        )
        self.assertEqual(
            invalid_snapshot.reason_code,
            EnrichmentGraphReason.CURRENT_SNAPSHOT_INVALID,
        )

    def test_model_failure_is_rejected_with_stable_role_reason(self) -> None:
        agent = EnrichmentAgent(
            coordinator_model=FailingModel(),
            expert_model=FailingModel(),
            integrator_model=FailingModel(),
        )

        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.REJECTED)
        self.assertEqual(
            result.reason_code,
            EnrichmentGraphReason.COORDINATOR_MODEL_FAILED,
        )
        self.assertEqual(result.rejection_detail, "RuntimeError")
        self.assertIsNotNone(result.model_failure)
        assert result.model_failure is not None
        self.assertEqual(result.model_failure.role, "coordinator")
        self.assertEqual(result.model_failure.provider, "injected")

    def test_provider_rate_limit_is_explicit_in_role_failure_log(self) -> None:
        agent = EnrichmentAgent(
            coordinator_model=RateLimitedModel(),
            expert_model=FailingModel(),
            integrator_model=FailingModel(),
        )

        with self.assertLogs("dreamland.enrichment", level="WARNING") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current"
                ),
            )

        self.assertEqual(
            result.reason_code,
            EnrichmentGraphReason.COORDINATOR_MODEL_FAILED,
        )
        output = "\n".join(captured.output)
        self.assertIn("failure_kind=provider_rate_limit", output)
        self.assertIn("failure_disposition=transient", output)
        self.assertIn("error_type=RateLimitError", output)
        assert result.model_failure is not None
        self.assertTrue(result.model_failure.classification.is_transient)

    def test_terminal_provider_failure_is_propagated_without_raw_detail(self) -> None:
        agent = EnrichmentAgent(
            coordinator_model=CreditExhaustedModel(),
            expert_model=FailingModel(),
            integrator_model=FailingModel(),
            coordinator_provider="openai",
            coordinator_model_name="gpt-5.6-terra",
        )

        with self.assertLogs("dreamland.enrichment", level="WARNING") as captured:
            result = agent.invoke(
                job_context(),
                initial_snapshot=snapshot(generation="initial"),
                capture_current_snapshot=lambda _context: snapshot(
                    generation="current"
                ),
            )

        assert result.model_failure is not None
        self.assertTrue(result.model_failure.classification.is_terminal)
        self.assertEqual(
            result.model_failure.classification.kind.value,
            "provider_credit_exhausted",
        )
        self.assertIn("openai", agent.provider_names)
        output = "\n".join(captured.output)
        self.assertIn("failure_disposition=terminal", output)
        self.assertNotIn("provider account detail", output)

    def test_expanded_topology_keeps_roles_and_temporal_boundary_visible(self) -> None:
        order: list[str] = []
        model = RecordingModel(coordinator_no_op(), "unused", order)
        agent = EnrichmentAgent(
            coordinator_model=model,
            expert_model=model,
            integrator_model=model,
        )

        mermaid = agent.topology_mermaid(expanded=True)

        self.assertIn("subgraph focus_coordinator_agent", mermaid)
        self.assertIn("subgraph ambient_presence_coordinator_agent", mermaid)
        self.assertIn("call_focus_coordinator_model", mermaid)
        self.assertIn("call_ambient_presence_coordinator_model", mermaid)
        self.assertIn("subgraph expert_agent", mermaid)
        self.assertIn("subgraph integrator_agent", mermaid)
        self.assertIn("snapshot_initial", mermaid)
        self.assertIn("prepare_coordinator_context", mermaid)
        self.assertIn("prepare_expert_brief", mermaid)
        self.assertIn("snapshot_current", mermaid)
        self.assertIn("resolve_current_references", mermaid)
        self.assertIn("classify_expert_items", mermaid)
        self.assertIn("parse_integrator_output", mermaid)
        self.assertIn("normalize_integrator_output", mermaid)
        self.assertIn("validate_integrator_output", mermaid)

    def test_ambient_mode_runs_only_ambient_coordinator_then_shared_roles(
        self,
    ) -> None:
        order: list[str] = []
        focus = RecordingModel(coordinator_no_op(), "focus", order)
        ambient_decision = json.loads(coordinator_work())
        ambient_decision["surface"] = "network"
        ambient_decision["expert_task"] = (
            "Attribute the TIME-WAIT row 10.42.20.12:22 -> "
            "10.42.30.15:51844 to Elena's earlier session."
        )
        ambient = RecordingModel(
            json.dumps(ambient_decision, separators=(",", ":")),
            "ambient",
            order,
        )
        expert = RecordingModel(expert_ok(), "expert", order)
        integrator = RecordingModel(integrator_patch(), "integrator", order)
        agent = EnrichmentAgent(
            coordinator_model=focus,
            ambient_coordinator_model=ambient,
            expert_model=expert,
            integrator_model=integrator,
        )
        time_wait_evidence = {
            "kind": "socket_snapshot",
            "source": "recorded_command_result",
            "command_id": "socket-1",
            "command": "ss -nat",
            "coverage": {"family": "all", "protocols": "tcp", "states": "all"},
            "output_excerpt": ("TIME-WAIT 0 0 10.42.20.12:22 10.42.30.15:51844\n"),
        }
        initial_snapshot = snapshot(generation="initial")
        initial_snapshot["recent_exhaustive_evidence"] = [time_wait_evidence]

        def capture(_context: EnrichmentJobContext):
            order.append("snapshot_current")
            current_snapshot = snapshot(generation="current")
            current_snapshot["recent_exhaustive_evidence"] = [time_wait_evidence]
            return current_snapshot

        with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
            result = agent.invoke(
                job_context(mode=EnrichmentMode.AMBIENT_PRESENCE),
                initial_snapshot=initial_snapshot,
                capture_current_snapshot=capture,
            )

        self.assertEqual(result.status, EnrichmentGraphStatus.PATCH_PROPOSED)
        self.assertEqual(
            order,
            ["ambient", "expert", "snapshot_current", "integrator"],
        )
        self.assertEqual(focus.calls, [])
        ambient_payload = json.loads(ambient.calls[0][0][-1].content)
        self.assertEqual(ambient_payload["enrichment_mode"], "ambient_presence")
        self.assertEqual(
            ambient_payload["presence_gap"],
            {
                "policy_version": "presence-gap-v2",
                "ambient_objective": "independent_life",
                "preferred_surface": "identity",
                "subject_preference": (
                    "named_human_or_person_equivalent_excluding_foreground_user_"
                    "and_service_accounts"
                ),
                "anti_repetition": {
                    "subjects": [],
                    "systems": ["service:redis"],
                },
            },
        )
        expert_payload = json.loads(expert.calls[0][0][-1].content)
        self.assertNotIn("presence_gap", expert_payload)
        self.assertIn(
            "TIME-WAIT row 10.42.20.12:22 -> 10.42.30.15:51844",
            expert_payload["question"],
        )
        socket_observation = next(
            item
            for item in expert_payload["observations"]
            if item["kind"] == "recent_exhaustive_observation"
        )
        self.assertEqual(
            socket_observation["details"]["output_excerpt"],
            "TIME-WAIT 0 0 10.42.20.12:22 10.42.30.15:51844\n",
        )
        self.assertIn(
            "independently grounded by supplied evidence outside the directly "
            "observed row",
            expert_payload["question"],
        )
        self.assertIn(
            "Do not copy or reuse the row's username, PID, TTY, endpoint, port, "
            "source, or observation time",
            expert_payload["question"],
        )
        self.assertIn(
            "Time qualification alone does not authorize reuse",
            expert_payload["question"],
        )
        integrator_payload = json.loads(integrator.calls[0][0][-1].content)
        self.assertEqual(
            integrator_payload["presence_gap"],
            ambient_payload["presence_gap"],
        )
        self.assertEqual(
            integrator.calls[0][0][0].content,
            (
                f"{load_prompt('enrichment_integrator.md')}\n\n"
                f"{load_prompt('enrichment_ambient_integrator_suffix.md')}"
            ),
        )
        self.assertIn(
            "Ambient Presence Coordinator",
            ambient.calls[0][0][0].content,
        )
        ambient_system_prompt = " ".join(ambient.calls[0][0][0].content.split())
        integrator_system_prompt = " ".join(integrator.calls[0][0][0].content.split())
        self.assertIn(
            "Do not ask the Expert for any of those values unless selected "
            "non-row references independently establish them",
            ambient_system_prompt,
        )
        self.assertIn(
            "copies or reuses the row's username, PID, TTY, endpoint, port, "
            "source, or observation time",
            integrator_system_prompt,
        )
        self.assertIn(
            "Time qualification alone does not authorize reuse",
            integrator_system_prompt,
        )
        self.assertEqual(
            ambient.calls[0][1]["metadata"]["dreamland.enrichment.role"],
            "ambient_coordinator",
        )
        self.assertEqual(
            ambient.calls[0][1]["metadata"]["dreamland.enrichment.mode"],
            "ambient_presence",
        )
        accepted_events = [
            line for line in captured.output if "event=role.accepted" in line
        ]
        self.assertEqual(len(accepted_events), 2)
        self.assertTrue(
            all("enrichment_mode=ambient_presence" in line for line in accepted_events)
        )
        context_events = [
            line
            for line in captured.output
            if "event=coordinator.context_prepared" in line
        ]
        self.assertEqual(len(context_events), 1)
        self.assertIn("presence_gap_policy_version=presence-gap-v2", context_events[0])
        self.assertIn("presence_gap_sha256=", context_events[0])
        self.assertIn(
            "presence_gap_ambient_objective=independent_life", context_events[0]
        )
        self.assertIn("presence_gap_preferred_surface=identity", context_events[0])
        self.assertIn(
            "presence_gap_anti_repetition_subject_count=0",
            context_events[0],
        )
        self.assertIn(
            "presence_gap_anti_repetition_system_count=1",
            context_events[0],
        )
        log_output = "\n".join(captured.output)
        self.assertIn(
            "ambient_integration_policy_version=ambient-human-integration-v3",
            log_output,
        )
        self.assertIn("ambient_integration_policy_sha256=", log_output)
        self.assertIn("ambient_integration_policy_chars=", log_output)

    def test_ambient_constructor_defaults_model_not_prompt_to_focus_coordinator(
        self,
    ) -> None:
        order: list[str] = []
        shared = RecordingModel(coordinator_no_op(), "shared", order)
        agent = EnrichmentAgent(
            coordinator_model=shared,
            expert_model=FailingModel(),
            integrator_model=FailingModel(),
            coordinator_prompt="FOCUS-PROMPT-CANARY",
        )

        result = agent.invoke(
            job_context(mode="ambient_presence"),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
        self.assertIs(agent._roles["ambient_coordinator"].model, shared)
        self.assertNotEqual(
            agent._roles["ambient_coordinator"].system_prompt,
            agent._roles["coordinator"].system_prompt,
        )
        self.assertIn(
            "Ambient Presence Coordinator",
            shared.calls[0][0][0].content,
        )

    def test_from_settings_builds_each_role_with_resolved_api_key(self) -> None:
        configs = []
        models = []

        def model_factory(config):
            configs.append(config)
            model = RecordingModel(coordinator_no_op(), config.model, [])
            models.append(model)
            return model

        settings = EnrichmentSettings(
            coordinator=EnrichmentRoleSettings(
                model="openai:coord-model",
                system_prompt_name="enrichment_coordinator.md",
                max_output_tokens=101,
                openai_api="responses",
                openai_service_tier="default",
                openai_reasoning_mode="standard",
            ),
            ambient_coordinator=EnrichmentRoleSettings(
                model="openai:ambient-model",
                system_prompt_name="enrichment_ambient_coordinator.md",
                max_output_tokens=151,
                openai_api="responses",
                openai_service_tier="fast",
                openai_reasoning_mode="pro",
            ),
            expert=EnrichmentRoleSettings(
                model="openai:expert-model",
                system_prompt_name="enrichment_expert.md",
                max_output_tokens=202,
                openai_api="chat_completions",
                openai_service_tier="flex",
            ),
            integrator=EnrichmentRoleSettings(
                model="openai:integrator-model",
                system_prompt_name="enrichment_integrator.md",
                max_output_tokens=303,
                openai_api="responses",
                openai_service_tier="priority",
                openai_reasoning_mode="pro",
            ),
        )

        agent = EnrichmentAgent.from_settings(
            settings,
            api_key_resolver=lambda provider: f"key-for-{provider}",
            model_factory=model_factory,
        )

        self.assertEqual(
            [config.model for config in configs],
            ["coord-model", "ambient-model", "expert-model", "integrator-model"],
        )
        self.assertEqual(
            [config.api_key for config in configs],
            [
                "key-for-openai",
                "key-for-openai",
                "key-for-openai",
                "key-for-openai",
            ],
        )
        self.assertEqual(
            [config.max_output_tokens for config in configs], [101, 151, 202, 303]
        )
        self.assertIs(agent._roles["coordinator"].model, models[0])
        self.assertIs(agent._roles["ambient_coordinator"].model, models[1])
        self.assertEqual(
            [c.openai_api for c in configs],
            ["responses", "responses", "chat_completions", "responses"],
        )
        self.assertEqual(
            [c.openai_service_tier for c in configs],
            ["default", "fast", "flex", "priority"],
        )
        self.assertEqual(
            [c.openai_reasoning_mode for c in configs], ["standard", "pro", None, "pro"]
        )
        self.assertEqual(
            agent._roles["ambient_coordinator"].openai_reasoning_mode, "pro"
        )

    def test_from_settings_can_delay_provider_construction_until_role_use(self) -> None:
        configs = []

        def model_factory(config):
            configs.append(config)
            return RecordingModel(coordinator_no_op(), config.model, [])

        settings = EnrichmentSettings(
            coordinator=EnrichmentRoleSettings(
                model="openai:coord-model",
                system_prompt_name="enrichment_coordinator.md",
            ),
            expert=EnrichmentRoleSettings(
                model="openai:expert-model",
                system_prompt_name="enrichment_expert.md",
            ),
            integrator=EnrichmentRoleSettings(
                model="openai:integrator-model",
                system_prompt_name="enrichment_integrator.md",
            ),
        )
        agent = EnrichmentAgent.from_settings(
            settings,
            model_factory=model_factory,
            lazy_models=True,
        )

        self.assertEqual(configs, [])
        result = agent.invoke(
            job_context(),
            initial_snapshot=snapshot(generation="initial"),
            capture_current_snapshot=lambda _context: snapshot(generation="current"),
        )

        self.assertEqual(result.status, EnrichmentGraphStatus.NO_OP)
        self.assertEqual([config.model for config in configs], ["coord-model"])


class EnrichmentParserTests(unittest.TestCase):
    def test_coordinator_contract_rejects_extra_keys(self) -> None:
        payload = json.loads(coordinator_work())
        payload["second_surface"] = "filesystem"

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_coordinator_decision(json.dumps(payload))

        self.assertEqual(raised.exception.reason_code, "unexpected_keys")

    def test_expert_contract_rejects_unknown_gate_reference(self) -> None:
        payload = json.loads(expert_ok())
        payload["conditions"] = [["native", "The service uses a native package"]]
        payload["gates"] = [["missing-item", ["native"]]]

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report(json.dumps(payload))

        self.assertEqual(raised.exception.reason_code, "invalid_reference")

    def test_expert_contract_accepts_gate_for_selected_candidate(self) -> None:
        payload = json.loads(expert_ok())
        payload["conditions"] = [["native", "The service uses a native package"]]
        payload["gates"] = [["p1", ["native"]]]

        report = parse_expert_report(json.dumps(payload), reference_ids=("r2",))

        self.assertEqual(report.gates[0].item_id, "p1")

    def test_expert_contract_rejects_empty_gate(self) -> None:
        payload = json.loads(expert_ok())
        payload["conditions"] = [["native", "The service uses a native package"]]
        payload["gates"] = [["p1", []]]

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report(json.dumps(payload))

        self.assertEqual(raised.exception.reason_code, "empty_gate")

    def test_coordinator_contract_requires_canonical_surface(self) -> None:
        payload = json.loads(coordinator_work())
        payload["surface"] = "processes"

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_coordinator_decision(json.dumps(payload))

        self.assertEqual(raised.exception.reason_code, "invalid_choice")

    def test_coordinator_can_scope_work_without_optional_references(self) -> None:
        payload = json.loads(coordinator_work())
        payload["anchors"] = []

        decision = parse_coordinator_decision(json.dumps(payload))

        self.assertEqual(decision.status, "work")
        self.assertEqual(decision.anchors, ())

    def test_coordinator_requires_bounded_exact_expertise_scope(self) -> None:
        cases = (
            (
                {"domain": "host processes"},
                "unexpected_keys",
                "$",
            ),
            (
                {
                    "domain": "x" * 81,
                    "perspective": "systems generalist",
                },
                "limit_exceeded",
                "$.expertise.domain",
            ),
        )

        for expertise, reason_code, location in cases:
            with self.subTest(expertise=expertise):
                payload = json.loads(coordinator_work())
                payload["expertise"] = expertise

                with self.assertRaises(RoleOutputValidationError) as raised:
                    parse_coordinator_decision(json.dumps(payload))

                self.assertEqual(raised.exception.reason_code, reason_code)
                self.assertEqual(raised.exception.location, location)

    def test_coordinator_no_op_requires_null_expertise(self) -> None:
        payload = json.loads(coordinator_no_op())
        payload["expertise"] = {
            "domain": "host processes",
            "perspective": "systems generalist",
        }

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_coordinator_decision(json.dumps(payload))

        self.assertEqual(raised.exception.reason_code, "invalid_no_op")

    def test_expert_contract_rejects_unknown_external_reference(self) -> None:
        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report(expert_ok(), reference_ids=("r1",))

        self.assertEqual(raised.exception.reason_code, "invalid_reference")
        self.assertEqual(raised.exception.location, "$.items[0].basis[0]")

    def test_expert_item_ids_cannot_use_external_reference_prefix(self) -> None:
        payload = json.loads(expert_ok())
        payload["items"][0]["id"] = "@r2"
        payload["relations"][0][0] = "@r2"

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report(json.dumps(payload), reference_ids=("r2",))

        self.assertEqual(raised.exception.reason_code, "invalid_id")
        self.assertEqual(raised.exception.location, "$.items[0].id")

    def test_expert_contract_rejects_old_support_shape(self) -> None:
        payload = json.loads(expert_ok())
        item = payload["items"][0]
        item["support"] = "likely"

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report(json.dumps(payload), reference_ids=("r2",))

        self.assertEqual(raised.exception.reason_code, "unexpected_keys")

    def test_expert_contract_rejects_invalid_candidate_origins(self) -> None:
        for origin in ("entailed", "likely", "observed", "conditional"):
            with self.subTest(origin=origin):
                payload = json.loads(expert_ok())
                payload["items"][0]["origin"] = origin

                with self.assertRaises(RoleOutputValidationError) as raised:
                    parse_expert_report(json.dumps(payload), reference_ids=("r2",))

                self.assertEqual(raised.exception.reason_code, "invalid_choice")

    def test_expert_contract_rejects_prefixed_duplicate_and_excess_basis(self) -> None:
        cases = (
            (["@r2"], "invalid_reference"),
            (["r2", "r2"], "duplicate_id"),
            ([f"r{index}" for index in range(1, 10)], "limit_exceeded"),
        )
        for basis, reason_code in cases:
            with self.subTest(basis=basis):
                payload = json.loads(expert_ok())
                payload["items"][0]["basis"] = basis

                with self.assertRaises(RoleOutputValidationError) as raised:
                    parse_expert_report(
                        json.dumps(payload),
                        reference_ids=tuple(f"r{index}" for index in range(1, 10)),
                    )

                self.assertEqual(raised.exception.reason_code, reason_code)

    def test_expert_relation_reference_must_be_in_candidate_basis(self) -> None:
        payload = json.loads(expert_ok())
        payload["items"][0]["basis"] = []

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report(json.dumps(payload), reference_ids=("r2",))

        self.assertEqual(raised.exception.reason_code, "invalid_reference")
        self.assertEqual(raised.exception.location, "$.relations[0][2]")

    def test_expert_reverse_relation_accepts_reference_in_candidate_basis(
        self,
    ) -> None:
        payload = json.loads(expert_ok())
        payload["relations"] = [["@r2", "is_implemented_by", "p1"]]

        report = parse_expert_report(json.dumps(payload), reference_ids=("r2",))

        self.assertEqual(report.relations[0].subject, "@r2")
        self.assertEqual(report.relations[0].object, "p1")
        self.assertEqual(report.items[0].basis, ("r2",))

    def test_expert_reverse_relation_rejects_reference_outside_candidate_basis(
        self,
    ) -> None:
        payload = json.loads(expert_ok())
        payload["items"][0]["basis"] = []
        payload["relations"] = [["@r2", "is_implemented_by", "p1"]]

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report(json.dumps(payload), reference_ids=("r2",))

        self.assertEqual(raised.exception.reason_code, "invalid_reference")
        self.assertEqual(raised.exception.location, "$.relations[0][0]")

    def test_expert_relation_salvage_preserves_valid_items_and_relation_order(
        self,
    ) -> None:
        payload = json.loads(expert_ok())
        payload["items"].append(
            {
                "id": "p2",
                "kind": "process",
                "name": "redis-helper",
                "detail": "Companion runtime helper",
                "origin": "derived",
                "basis": [],
            }
        )
        payload["relations"] = [
            ["p1", "implements", "@r2"],
            ["@r2", "launches", "@r3"],
            ["p1", "uses", "@r3"],
            ["p2", "supports", "p1"],
        ]

        parsed = parse_expert_report_with_relation_salvage(
            json.dumps(payload),
            reference_ids=("r2", "r3"),
        )

        self.assertEqual(
            [relation.to_payload() for relation in parsed.report.relations],
            [
                ["p1", "implements", "@r2"],
                ["p2", "supports", "p1"],
            ],
        )
        self.assertEqual(
            [
                (item.index, item.rule, item.reason_code, item.location)
                for item in parsed.rejected_relations
            ],
            [
                (1, "item_endpoint_required", "invalid_reference", "$.relations[1]"),
                (
                    2,
                    "reference_not_in_item_basis",
                    "invalid_reference",
                    "$.relations[2][2]",
                ),
            ],
        )
        self.assertEqual(len(parsed.report.items), 2)

    def test_expert_relation_salvage_discards_malformed_entries_only(self) -> None:
        payload = json.loads(expert_ok())
        payload["relations"] = [
            ["p1", "implements"],
            ["p1", "", "@r2"],
            ["missing", "supports", "p1"],
            ["p1", "implements", "@r2"],
        ]

        parsed = parse_expert_report_with_relation_salvage(
            json.dumps(payload),
            reference_ids=("r2",),
        )

        self.assertEqual(
            [relation.to_payload() for relation in parsed.report.relations],
            [["p1", "implements", "@r2"]],
        )
        self.assertEqual(
            [(item.index, item.rule) for item in parsed.rejected_relations],
            [
                (0, "invalid_shape"),
                (1, "invalid_text"),
                (2, "unknown_endpoint"),
            ],
        )

    def test_expert_relation_salvage_keeps_top_level_contract_strict(self) -> None:
        payload = json.loads(expert_ok())
        payload["relations"] = "not-an-array"

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report_with_relation_salvage(
                json.dumps(payload),
                reference_ids=("r2",),
            )

        self.assertEqual(raised.exception.reason_code, "invalid_type")

    def test_insufficient_report_cannot_be_legalized_by_relation_salvage(self) -> None:
        payload = json.loads(expert_insufficient())
        payload["relations"] = [["@r2", "launches", "@r2"]]

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report_with_relation_salvage(
                json.dumps(payload),
                reference_ids=("r2",),
            )

        self.assertEqual(raised.exception.reason_code, "invalid_reference")

    def test_expert_candidate_limit_is_enforced(self) -> None:
        payload = json.loads(expert_ok())
        payload["items"].append(
            {
                "id": "p2",
                "kind": "process",
                "name": "second-worker",
                "detail": "Second coherent runtime worker",
                "origin": "derived",
                "basis": ["r2"],
            }
        )

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report(
                json.dumps(payload),
                reference_ids=("r2",),
                max_items=1,
            )

        self.assertEqual(raised.exception.reason_code, "limit_exceeded")

    def test_expert_contract_accepts_twenty_four_unique_relations(self) -> None:
        payload = json.loads(expert_ok())
        expected = [["p1", f"relationship-{index}", "@r2"] for index in range(1, 25)]
        payload["relations"] = expected

        report = parse_expert_report(
            json.dumps(payload),
            reference_ids=("r2",),
        )

        self.assertEqual(
            [relation.to_payload() for relation in report.relations],
            expected,
        )

    def test_strict_expert_contract_rejects_relation_overflow(self) -> None:
        payload = json.loads(expert_ok())
        payload["relations"] = [
            ["p1", f"relationship-{index}", "@r2"] for index in range(1, 26)
        ]

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report(
                json.dumps(payload),
                reference_ids=("r2",),
            )

        self.assertEqual(raised.exception.reason_code, "limit_exceeded")
        self.assertEqual(raised.exception.location, "$.relations")

    def test_strict_expert_contract_rejects_duplicate_relation(self) -> None:
        payload = json.loads(expert_ok())
        payload["relations"].append(payload["relations"][0])

        with self.assertRaises(RoleOutputValidationError) as raised:
            parse_expert_report(
                json.dumps(payload),
                reference_ids=("r2",),
            )

        self.assertEqual(raised.exception.reason_code, "duplicate_relation")
        self.assertEqual(raised.exception.location, "$.relations[1]")

    def test_expert_support_arrays_retain_eight_entry_limits(self) -> None:
        base = json.loads(expert_ok())
        cases: list[tuple[str, dict[str, object], str]] = []

        conditions = deepcopy(base)
        conditions["conditions"] = [
            [f"c{index}", f"Prerequisite {index}"] for index in range(1, 10)
        ]
        cases.append(("conditions", conditions, "$.conditions"))

        gates = deepcopy(base)
        gates["conditions"] = [["c1", "One prerequisite"]]
        gates["items"] = [
            {
                "id": f"p{index}",
                "kind": "process",
                "name": f"worker-{index}",
                "detail": f"Distinct worker {index}",
                "origin": "derived",
                "basis": ["r2"],
            }
            for index in range(1, 10)
        ]
        gates["gates"] = [[f"p{index}", ["c1"]] for index in range(1, 10)]
        gates["relations"] = []
        cases.append(("gates", gates, "$.gates"))

        basis = deepcopy(base)
        basis["items"][0]["basis"] = [f"r{index}" for index in range(1, 10)]
        basis["relations"] = []
        cases.append(("basis", basis, "$.items[0].basis"))

        for name, payload, location in cases:
            with self.subTest(array=name):
                with self.assertRaises(RoleOutputValidationError) as raised:
                    parse_expert_report(
                        json.dumps(payload),
                        reference_ids=("r2",),
                    )

                self.assertEqual(raised.exception.reason_code, "limit_exceeded")
                self.assertEqual(raised.exception.location, location)

    def test_job_ids_default_to_distinct_checkpoint_threads(self) -> None:
        common = {
            "session_id": "session-1",
            "target_host": "cache",
            "trigger_id": "trigger-1",
            "event_id": "event-1",
        }

        first = EnrichmentJobContext(**common)
        second = EnrichmentJobContext(**common)

        self.assertIs(first.mode, EnrichmentMode.FOCUSED)
        self.assertNotEqual(first.job_id, second.job_id)
        self.assertNotEqual(first.thread_id, second.thread_id)

    def test_job_context_preserves_legacy_positional_job_id(self) -> None:
        context = EnrichmentJobContext(
            "session-1",
            "cache",
            "trigger-1",
            "event-1",
            "legacy-job-id",
        )

        self.assertEqual(context.job_id, "legacy-job-id")
        self.assertIs(context.mode, EnrichmentMode.FOCUSED)

    def test_job_context_rejects_unknown_enrichment_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "supported enrichment mode"):
            job_context(mode="unbounded_clutter")


if __name__ == "__main__":
    unittest.main()
