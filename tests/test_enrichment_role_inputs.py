from __future__ import annotations

import json
import unittest

from dreamland.background_agents.enrichment.modes import EnrichmentMode
from dreamland.background_agents.enrichment.role_inputs import (
    AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_POLICY_VERSION,
    RoleInputValidationError,
    build_coordinator_prompt_input,
    build_expert_brief,
    build_presence_gap,
    expert_task_policy_log_fields,
    resolve_current_reference_bindings,
)


def snapshot(*, liberty: object = "moderate") -> dict[str, object]:
    return {
        "v": 1,
        "world": {
            "active_host": "cache",
            "generation_policy": {"liberty": liberty},
            "simulation": {"missing_detail_policy": "infer"},
            "expansion_directives_by_id": {"d1": {"summary": "keep ordinary"}},
            "hosts_by_name": {
                "cache": {
                    "hostname": "redis-01",
                    "purpose": {
                        "description": "A DREAMLAND state-profile default",
                        "role": "community-network cache",
                        "theme": "weathered relay host in a volunteer radio shack",
                        "roleplay_guidance": [
                            "Treat sparse fields as creative space",
                        ],
                    },
                    "operating_system": {"family": "linux"},
                    "services_by_name": {
                        "redis": {
                            "state": "running",
                            "port": 6379,
                            "dreamland": {"schema": "visible"},
                            "coordinator_note": "workflow-only detail",
                        }
                    },
                    "processes_by_id": {
                        "101": {"name": "redis-server"},
                    },
                    "command_history": [
                        {
                            "id": "c1",
                            "command": "ps aux",
                            "exit_status": 0,
                        }
                    ],
                }
            },
        },
    }


def foreground_authority() -> dict[str, object]:
    return {
        "v": 1,
        "class": "foreground_observation",
        "target_host": "cache",
        "hostname": "redis-01",
        "active_username": "alice",
        "login_username": "alice",
        "interactive_program": {
            "name": "bash",
            "command": "/bin/bash",
            "argv": ["/bin/bash"],
            "prompt_template": None,
        },
        "shell_environment": {"SHELL": "/bin/bash", "USER": "alice"},
        "shell_stack_depth": 1,
        "cwd": "/srv",
    }


class EnrichmentRoleInputTests(unittest.TestCase):
    def test_recorded_time_and_exhaustive_evidence_reach_roles_without_new_keys(
        self,
    ) -> None:
        current = snapshot()
        current["host_time"] = {
            "v": 1,
            "status": "observed",
            "kind": "recorded_host_time",
            "source": "recorded_command_result",
            "command_id": "time-1",
            "command": "date",
            "coverage": {"clock": "host_reported"},
            "output_excerpt": "Thu Nov 12 16:38:27 GST 2318\n",
        }
        current["recent_exhaustive_evidence"] = [
            {
                "kind": "session_presence",
                "source": "recorded_command_result",
                "command_id": "who-1",
                "command": "who",
                "coverage": {"sessions": "current"},
                "output_excerpt": "alice pts/0 2318-11-12 16:30\n",
            }
        ]
        prepared = build_coordinator_prompt_input(
            current,
            target_host="cache",
            trigger="interaction_pulse",
            surface_hint="identity",
            focus_hints=(),
            mode=EnrichmentMode.AMBIENT_PRESENCE,
        )

        brief, _selected, _latitude = build_expert_brief(
            current,
            target_host="cache",
            surface="process",
            expertise={
                "domain": "host process ecology",
                "perspective": "systems generalist",
            },
            question="Which ordinary processes accompany this service?",
            selected_reference_ids=(),
            reference_catalog=prepared.references,
        )

        self.assertEqual(prepared.payload["host_time"], current["host_time"])
        self.assertEqual(
            prepared.payload["recent_exhaustive_evidence"],
            current["recent_exhaustive_evidence"],
        )
        self.assertEqual(
            [item["kind"] for item in brief["observations"][:3]],
            ["host_time", "recent_exhaustive_observation", "process"],
        )
        self.assertEqual(
            set(brief),
            {"expertise", "question", "latitude", "host", "observations", "references"},
        )

    def test_foreground_authority_reaches_coordinator_and_leads_expert_evidence(
        self,
    ) -> None:
        current = snapshot()
        current["foreground_authority"] = foreground_authority()
        prepared = build_coordinator_prompt_input(
            current,
            target_host="cache",
            trigger="surface_focus",
            surface_hint="process",
            focus_hints=(),
        )

        brief, _selected, _latitude = build_expert_brief(
            current,
            target_host="cache",
            surface="process",
            expertise={
                "domain": "host process ecology",
                "perspective": "systems generalist",
            },
            question="Which ordinary processes accompany this service?",
            selected_reference_ids=("r5",),
            reference_catalog=prepared.references,
        )

        self.assertEqual(
            prepared.payload["foreground_authority"],
            foreground_authority(),
        )
        self.assertEqual(brief["observations"][0]["kind"], "active_session")
        self.assertEqual(
            brief["observations"][0]["details"],
            foreground_authority(),
        )

    def test_coordinator_receives_organized_planning_context_and_catalog(self) -> None:
        prepared = build_coordinator_prompt_input(
            snapshot(),
            target_host="cache",
            trigger="surface_focus",
            surface_hint="process",
            focus_hints=("redis",),
            interaction_focus={
                "cwd": "/home/alice",
                "paths": ["/home/alice/reports"],
                "primary": {"kind": "path", "id": "/home/alice/reports"},
                "key": "path:/home/alice/reports",
            },
        )

        self.assertEqual(
            prepared.payload["operator_bounds"]["generation_policy"]["liberty"],
            "moderate",
        )
        self.assertEqual(prepared.payload["enrichment_mode"], "focused")
        self.assertEqual(
            prepared.payload["coverage_summary"],
            {
                "facts": 0,
                "filesystem_artifacts": 0,
                "groups": 0,
                "services": 1,
                "users": 0,
            },
        )
        self.assertNotIn(
            "generation_policy",
            prepared.payload["materialized_state"],
        )
        self.assertEqual(
            prepared.payload["wake_reason"]["interaction_focus"]["paths"],
            ["/home/alice/reports"],
        )
        self.assertEqual(
            [item.id for item in prepared.references],
            ["r1", "r2", "r3", "r4", "r5"],
        )
        self.assertEqual(
            prepared.references[0].observation,
            {"name": "cache", "hostname": "redis-01"},
        )
        service = prepared.references[-1]
        self.assertEqual((service.kind, service.label), ("service", "redis"))

    def test_ambient_coordinator_input_is_explicit_and_coverage_is_structural(
        self,
    ) -> None:
        current = snapshot()
        host = current["world"]["hosts_by_name"]["cache"]
        host["users_by_username"] = {"alice": {}, "operator": {}}
        host["groups_by_name"] = {"operators": {}}
        host["facts_by_id"] = {"login-history": {}, "peer-activity": {}}
        host["filesystem"] = {
            "artifacts_by_path": {"/var/log/auth.log": {}, "/srv/queue": {}}
        }

        prepared = build_coordinator_prompt_input(
            current,
            target_host="cache",
            trigger="interaction_pulse",
            surface_hint=None,
            focus_hints=(),
            mode=EnrichmentMode.AMBIENT_PRESENCE,
        )

        self.assertEqual(prepared.payload["enrichment_mode"], "ambient_presence")
        self.assertEqual(
            prepared.payload["coverage_summary"],
            {
                "facts": 2,
                "filesystem_artifacts": 2,
                "groups": 1,
                "services": 1,
                "users": 2,
            },
        )
        self.assertEqual(
            prepared.payload["presence_gap"],
            {
                "policy_version": "presence-gap-v2",
                "ambient_objective": "independent_life",
                "preferred_surface": "service",
                "subject_preference": (
                    "named_human_or_person_equivalent_excluding_foreground_user_"
                    "and_service_accounts"
                ),
                "anti_repetition": {
                    "subjects": [
                        "user:alice",
                        "user:operator",
                        "group:operators",
                    ],
                    "systems": ["service:redis"],
                },
            },
        )

    def test_presence_gap_prefers_supplied_supported_non_shell_surface(self) -> None:
        current = snapshot()
        host = current["world"]["hosts_by_name"]["cache"]
        host["users_by_username"] = {
            "operator": {},
            "radio": {},
            "sensor": {},
            "visitor": {},
        }
        host["groups_by_name"] = {"operators": {}}

        gap = build_presence_gap(
            current,
            target_host="cache",
            surface_hint="network",
        )

        self.assertEqual(gap["preferred_surface"], "network")
        self.assertEqual(
            set(gap),
            {
                "policy_version",
                "ambient_objective",
                "preferred_surface",
                "subject_preference",
                "anti_repetition",
            },
        )

    def test_presence_gap_shell_fallback_excludes_foreground_login_and_ties_stably(
        self,
    ) -> None:
        current = snapshot()
        current["foreground_authority"] = foreground_authority()
        host = current["world"]["hosts_by_name"]["cache"]
        host["users_by_username"] = {"alice": {}}

        gap = build_presence_gap(
            current,
            target_host="cache",
            surface_hint="shell",
        )

        self.assertEqual(gap["preferred_surface"], "identity")
        self.assertEqual(gap["policy_version"], "presence-gap-v2")
        self.assertEqual(gap["ambient_objective"], "independent_life")
        self.assertEqual(
            gap["subject_preference"],
            "named_human_or_person_equivalent_excluding_foreground_user_"
            "and_service_accounts",
        )
        self.assertEqual(
            gap["anti_repetition"],
            {"subjects": [], "systems": ["service:redis"]},
        )

    def test_presence_gap_fallback_uses_composite_surface_coverage(self) -> None:
        current = snapshot()
        host = current["world"]["hosts_by_name"]["cache"]
        host["users_by_username"] = {"operator": {}}
        host["groups_by_name"] = {"operators": {}}
        host["services_by_name"].clear()

        service_tie = build_presence_gap(
            current,
            target_host="cache",
            surface_hint="unsupported",
        )
        self.assertEqual(service_tie["preferred_surface"], "service")

        host["services_by_name"] = {"redis": {}}
        filesystem_least = build_presence_gap(
            current,
            target_host="cache",
            surface_hint=None,
        )
        self.assertEqual(filesystem_least["preferred_surface"], "filesystem")

    def test_presence_gap_anti_repetition_is_state_derived_and_bounded(self) -> None:
        current = snapshot()
        current["foreground_authority"] = foreground_authority()
        host = current["world"]["hosts_by_name"]["cache"]
        host["users_by_username"] = {
            "alice": {},
            "operator": {},
            "radio": {},
            "sensor": {},
            "visitor": {},
        }
        host["groups_by_name"] = {"operators": {}, "telemetry": {}}
        host["services_by_name"].update(
            {
                "fleet-osquery": {},
                "telemetry": {},
                "watchdog": {},
                "worker": {},
            }
        )

        gap = build_presence_gap(
            current,
            target_host="cache",
            surface_hint=None,
        )
        hints = gap["anti_repetition"]

        self.assertNotIn("user:alice", hints["subjects"])
        self.assertEqual(len(hints["subjects"]), 4)
        self.assertEqual(len(hints["systems"]), 4)
        self.assertEqual(hints["systems"][0], "service:fleet-osquery")

    def test_focused_coordinator_does_not_receive_ambient_presence_gap(self) -> None:
        prepared = build_coordinator_prompt_input(
            snapshot(),
            target_host="cache",
            trigger="surface_focus",
            surface_hint="process",
            focus_hints=(),
        )

        self.assertNotIn("presence_gap", prepared.payload)

    def test_expert_brief_is_neutral_allowlisted_and_reference_bound(self) -> None:
        prepared = build_coordinator_prompt_input(
            snapshot(),
            target_host="cache",
            trigger="surface_focus",
            surface_hint="process",
            focus_hints=(),
        )
        brief, selected, latitude = build_expert_brief(
            snapshot(),
            target_host="cache",
            surface="process",
            expertise={
                "domain": "small-network service processes",
                "perspective": "systems generalist",
            },
            question="Which native processes normally accompany this Redis service?",
            selected_reference_ids=("r4", "r5"),
            reference_catalog=prepared.references,
        )

        self.assertEqual(
            set(brief),
            {
                "expertise",
                "question",
                "latitude",
                "host",
                "observations",
                "references",
            },
        )
        self.assertEqual(latitude.level, "moderate")
        self.assertFalse(latitude.defaulted)
        self.assertEqual(
            brief["latitude"],
            {
                "instruction": (
                    "State affirmative, concrete, mutually consistent details from "
                    "one realization; do not return inspection advice, possibilities, "
                    "or alternative menus. Construct one coherent representative "
                    "realization. When "
                    "several ordinary answers fit, select plausible concrete "
                    "details instead of leaving every choice unresolved. Develop "
                    "supporting observable context around the focal detail when it "
                    "fits."
                ),
                "candidate_limit": 8,
                "relation_limit": 8,
            },
        )
        self.assertEqual(
            brief["expertise"],
            {
                "domain": "small-network service processes",
                "perspective": "systems generalist",
            },
        )
        self.assertEqual(
            brief["host"]["purpose"],
            {
                "role": "community-network cache",
                "theme": "weathered relay host in a volunteer radio shack",
            },
        )
        self.assertEqual(
            brief["references"][0],
            {
                "id": "r4",
                "kind": "purpose",
                "label": "theme",
                "observation": "weathered relay host in a volunteer radio shack",
            },
        )
        self.assertEqual(brief["references"][1]["label"], "redis")
        self.assertEqual(brief["observations"][0]["kind"], "host_time")

        self.assertEqual(
            brief["observations"][0]["details"],
            {"v": 1, "status": "unknown"},
        )
        self.assertEqual(brief["observations"][1]["label"], "processes")
        serialized = json.dumps(brief).lower()
        for forbidden in (
            "dreamland",
            "generation_policy",
            "liberty",
            "simulation",
            "expansion_directives_by_id",
            "hosts_by_name",
            "roleplay_guidance",
            "creative space",
            "coordinator",
            "processes_by_id",
        ):
            self.assertNotIn(forbidden, serialized)

        bindings = resolve_current_reference_bindings(
            snapshot()["world"]["hosts_by_name"]["cache"],
            selected,
        )
        self.assertEqual(
            bindings,
            [
                {
                    "id": "r4",
                    "kind": "purpose",
                    "path": ["purpose", "theme"],
                    "expected": "weathered relay host in a volunteer radio shack",
                },
                {
                    "id": "r5",
                    "kind": "service",
                    "path": ["services_by_name", "redis"],
                    "expected": {
                        "state": "running",
                        "port": 6379,
                        "dreamland": {"schema": "visible"},
                        "coordinator_note": "workflow-only detail",
                    },
                },
            ],
        )

    def test_ambient_expert_task_adds_versioned_human_presence_requirement_only(
        self,
    ) -> None:
        current = snapshot()
        prepared = build_coordinator_prompt_input(
            current,
            target_host="cache",
            trigger="interaction_pulse",
            surface_hint="identity",
            focus_hints=(),
            mode=EnrichmentMode.AMBIENT_PRESENCE,
        )
        question = (
            "Attribute the TIME-WAIT row 10.42.20.12:22 -> "
            "10.42.30.15:51844 to Elena's earlier session.  "
        )
        common = {
            "snapshot": current,
            "target_host": "cache",
            "surface": "identity",
            "expertise": {
                "domain": "local identity activity",
                "perspective": "systems observer",
            },
            "question": question,
            "selected_reference_ids": (),
            "reference_catalog": prepared.references,
        }

        focused, _selected, _latitude = build_expert_brief(**common)
        explicit_focused, _selected, _latitude = build_expert_brief(
            **common,
            mode=EnrichmentMode.FOCUSED,
        )
        ambient, _selected, _latitude = build_expert_brief(
            **common,
            mode=EnrichmentMode.AMBIENT_PRESENCE,
        )

        self.assertEqual(
            AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_POLICY_VERSION,
            "ambient-human-presence-v3",
        )
        self.assertEqual(focused["question"], question)
        self.assertEqual(
            json.dumps(focused, ensure_ascii=False, separators=(",", ":")),
            json.dumps(
                explicit_focused,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        self.assertEqual(
            expert_task_policy_log_fields(mode=EnrichmentMode.FOCUSED),
            {},
        )
        ambient_policy_log = expert_task_policy_log_fields(
            mode=EnrichmentMode.AMBIENT_PRESENCE
        )
        self.assertEqual(
            ambient_policy_log["policy_version"],
            "ambient-human-presence-v3",
        )
        self.assertEqual(
            ambient_policy_log["action"],
            "ambient_human_expert_constraint_appended",
        )
        self.assertEqual(
            ambient_policy_log["policy_sha256"],
            "f5b0d486038867af79806f43a61239a86c670e8863dc2b95b73c82e1102c7efe",
        )
        self.assertEqual(ambient_policy_log["policy_chars"], 1606)
        self.assertEqual(
            set(ambient),
            {
                "expertise",
                "question",
                "latitude",
                "host",
                "observations",
                "references",
            },
        )
        ambient_question = ambient["question"]
        self.assertIsInstance(ambient_question, str)
        self.assertTrue(
            ambient_question.startswith(
                "Attribute the TIME-WAIT row 10.42.20.12:22 -> "
                "10.42.30.15:51844 to Elena's earlier session. "
                "Additional requirement:"
            )
        )
        self.assertIn("one named human individual", ambient_question)
        self.assertIn("other than the active interactive user", ambient_question)
        self.assertIn("cannot satisfy the requested individual", ambient_question)
        self.assertIn("do not substitute a service identity", ambient_question)
        self.assertIn(
            "account transition does not create a new person", ambient_question
        )
        self.assertIn(
            "directly observed login, session, process, or socket", ambient_question
        )
        self.assertIn("reserved to its observed or unknown owner", ambient_question)
        self.assertIn(
            "unless the row itself identifies the individual", ambient_question
        )
        self.assertIn("distinct historical or later event", ambient_question)
        self.assertIn(
            "independently grounded by supplied evidence outside the directly "
            "observed row",
            ambient_question,
        )
        self.assertIn(
            "Do not copy or reuse the row's username, PID, TTY, endpoint, port, "
            "source, or observation time",
            ambient_question,
        )
        self.assertIn("merely by shifting chronology", ambient_question)
        self.assertIn(
            "Time qualification alone does not authorize reuse", ambient_question
        )
        self.assertIn(
            "must not be presented as explaining the observed foreground row",
            ambient_question,
        )
        self.assertNotIn(
            AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_POLICY_VERSION,
            ambient_question,
        )

    def test_internal_vocabulary_cannot_cross_in_expert_question(self) -> None:
        prepared = build_coordinator_prompt_input(
            snapshot(),
            target_host="cache",
            trigger="manual",
            surface_hint=None,
            focus_hints=(),
        )

        with self.assertRaises(RoleInputValidationError) as raised:
            build_expert_brief(
                snapshot(),
                target_host="cache",
                surface="process",
                expertise={
                    "domain": "host processes",
                    "perspective": "systems generalist",
                },
                question=(
                    "What should the emulator Integrator patch into world state?"
                ),
                selected_reference_ids=("r5",),
                reference_catalog=prepared.references,
            )

        self.assertEqual(raised.exception.reason_code, "internal_vocabulary")

    def test_internal_vocabulary_cannot_cross_in_expertise_scope(self) -> None:
        prepared = build_coordinator_prompt_input(
            snapshot(),
            target_host="cache",
            trigger="manual",
            surface_hint=None,
            focus_hints=(),
        )

        with self.assertRaises(RoleInputValidationError) as raised:
            build_expert_brief(
                snapshot(),
                target_host="cache",
                surface="process",
                expertise={
                    "domain": "DREAMLAND generation policy",
                    "perspective": "systems generalist",
                },
                question="Which processes normally support the observed service?",
                selected_reference_ids=("r5",),
                reference_catalog=prepared.references,
            )

        self.assertEqual(raised.exception.reason_code, "internal_vocabulary")
        self.assertEqual(raised.exception.location, "$.expertise.domain")

    def test_real_world_role_titles_remain_valid_expertise_perspectives(self) -> None:
        prepared = build_coordinator_prompt_input(
            snapshot(),
            target_host="cache",
            trigger="manual",
            surface_hint=None,
            focus_hints=(),
        )
        perspectives = (
            "power-station operations coordinator",
            "OT operations coordinator",
            "industrial-control maintenance coordinator",
            "industrial systems integrator",
            "embedded emulator specialist",
        )

        for perspective in perspectives:
            with self.subTest(perspective=perspective):
                brief, _selected, _latitude = build_expert_brief(
                    snapshot(),
                    target_host="cache",
                    surface="process",
                    expertise={
                        "domain": "host processes",
                        "perspective": perspective,
                    },
                    question="Which processes normally support the observed service?",
                    selected_reference_ids=("r5",),
                    reference_catalog=prepared.references,
                )

                self.assertEqual(brief["expertise"]["perspective"], perspective)

    def test_internal_workflow_role_cannot_cross_in_expert_question(self) -> None:
        prepared = build_coordinator_prompt_input(
            snapshot(),
            target_host="cache",
            trigger="manual",
            surface_hint=None,
            focus_hints=(),
        )

        with self.assertRaises(RoleInputValidationError) as raised:
            build_expert_brief(
                snapshot(),
                target_host="cache",
                surface="process",
                expertise={
                    "domain": "host processes",
                    "perspective": "systems generalist",
                },
                question="Ask the Coordinator which ordinary processes to include.",
                selected_reference_ids=("r5",),
                reference_catalog=prepared.references,
            )

        self.assertEqual(raised.exception.reason_code, "internal_vocabulary")
        self.assertEqual(raised.exception.location, "$.question")

    def test_generic_domain_role_terms_remain_valid_scenario_evidence(self) -> None:
        current = snapshot()
        host = current["world"]["hosts_by_name"]["cache"]
        host["purpose"]["theme"] = (
            "retro arcade emulator managed by a distributed coordinator"
        )
        prepared = build_coordinator_prompt_input(
            current,
            target_host="cache",
            trigger="manual",
            surface_hint="process",
            focus_hints=(),
        )

        brief, _selected, _latitude = build_expert_brief(
            current,
            target_host="cache",
            surface="process",
            expertise={
                "domain": "host processes",
                "perspective": "systems generalist",
            },
            question="Which processes normally support the observed service?",
            selected_reference_ids=("r4", "r5"),
            reference_catalog=prepared.references,
        )

        self.assertEqual(
            brief["host"]["purpose"]["theme"],
            "retro arcade emulator managed by a distributed coordinator",
        )
        self.assertEqual(
            brief["references"][0]["observation"],
            "retro arcade emulator managed by a distributed coordinator",
        )

    def test_expert_brief_injects_deterministic_task_neutral_latitude(self) -> None:
        affirmative_instruction = (
            "State affirmative, concrete, mutually consistent details from one "
            "realization; do not return inspection advice, possibilities, or "
            "alternative menus. "
        )
        expected = {
            "strict": (
                affirmative_instruction
                + "Return only net-new details that are directly supported or "
                "logically unavoidable from the evidence.",
                2,
            ),
            "conservative": (
                affirmative_instruction
                + "Return a small number of net-new routine details that are "
                "strongly implied by the evidence.",
                4,
            ),
            "moderate": (
                affirmative_instruction
                + "Construct one coherent representative realization. When several "
                "ordinary answers fit, select plausible concrete details instead "
                "of leaving every choice unresolved. Develop supporting observable "
                "context around the focal detail when it fits.",
                8,
            ),
            "expansive": (
                affirmative_instruction
                + "Produce a substantial connected layer of mutually supporting "
                "operational details. Select plausible concrete specifics where the "
                "evidence leaves ordinary creative space. Use most available capacity "
                "when distinct coherent material exists, developing structural, "
                "lifecycle, causal, and residual depth instead of a flat inventory.",
                12,
            ),
            "abundant": (
                affirmative_instruction
                + "Build a dense, coherent causal layer with candid operational "
                "texture and residue. Use the full available capacity when distinct "
                "coherent net-new material supports it. Develop applicable structural "
                "nesting, leaf material, lifecycle variation, causal relationships, "
                "and signs of present or past activity rather than a flat inventory. "
                "Prefer believable concrete choices over unspecified ordinary details "
                "unless a choice would conflict with the evidence.",
                24,
            ),
        }
        expected_integration = {
            "strict": (
                "Faithfully materialize every directly and unambiguously mappable "
                "eligible candidate; do not add interpretive substance."
            ),
            "conservative": (
                "Faithfully materialize every clearly mappable eligible candidate "
                "while avoiding marginal or lossy encodings."
            ),
            "moderate": (
                "Materialize a coherent representative cluster and preserve "
                "distinct safely mappable artifacts when each adds net-new "
                "observable substance."
            ),
            "expansive": (
                "Materialize most safely mappable eligible material. Preserve "
                "distinct observable kinds, nested paths, leaf artifacts, lifecycle "
                "distinctions, and relationships rather than compressing them into a "
                "minimal or flat summary."
            ),
            "abundant": (
                "Materialize the fullest safely mappable version of the eligible "
                "cluster up to the patch limit. Preserve distinct nested paths, leaf "
                "artifacts, identities, lifecycle distinctions, candid cross-surface "
                "operational residue, and relationships rather than flattening "
                "hierarchy or treating time-varying activity as a permanent "
                "simultaneous inventory."
            ),
        }
        for level, (instruction, candidate_limit) in expected.items():
            with self.subTest(level=level):
                current = snapshot(liberty=level)
                prepared = build_coordinator_prompt_input(
                    current,
                    target_host="cache",
                    trigger="manual",
                    surface_hint="process",
                    focus_hints=(),
                )
                first, _selected, latitude = build_expert_brief(
                    current,
                    target_host="cache",
                    surface="process",
                    expertise={
                        "domain": "host process ecology",
                        "perspective": "systems generalist",
                    },
                    question="Which ordinary processes accompany this service?",
                    selected_reference_ids=("r5",),
                    reference_catalog=prepared.references,
                )
                second, _selected_again, _latitude_again = build_expert_brief(
                    current,
                    target_host="cache",
                    surface="process",
                    expertise={
                        "domain": "host process ecology",
                        "perspective": "systems generalist",
                    },
                    question="Which ordinary processes accompany this service?",
                    selected_reference_ids=("r5",),
                    reference_catalog=prepared.references,
                )

                self.assertEqual(first, second)
                self.assertEqual(
                    first["latitude"],
                    {
                        "instruction": instruction,
                        "candidate_limit": candidate_limit,
                        "relation_limit": candidate_limit,
                    },
                )
                self.assertEqual(latitude.configured_value, level)
                self.assertFalse(latitude.defaulted)
                self.assertEqual(
                    latitude.integration_payload(),
                    {"instruction": expected_integration[level]},
                )
                self.assertEqual(latitude.template_version, "creative-latitude-v5")

    def test_missing_invalid_or_non_string_liberty_defaults_to_moderate(self) -> None:
        cases: list[dict[str, object]] = [
            snapshot(liberty="unknown"),
            snapshot(liberty=7),
            snapshot(),
        ]
        del cases[-1]["world"]["generation_policy"]
        for current in cases:
            with self.subTest(policy=current["world"].get("generation_policy")):
                prepared = build_coordinator_prompt_input(
                    current,
                    target_host="cache",
                    trigger="manual",
                    surface_hint="process",
                    focus_hints=(),
                )
                brief, _selected, latitude = build_expert_brief(
                    current,
                    target_host="cache",
                    surface="process",
                    expertise={
                        "domain": "host processes",
                        "perspective": "systems generalist",
                    },
                    question="Which ordinary processes accompany this service?",
                    selected_reference_ids=("r5",),
                    reference_catalog=prepared.references,
                )

                self.assertEqual(
                    set(brief["latitude"]),
                    {"instruction", "candidate_limit", "relation_limit"},
                )
                self.assertEqual(latitude.level, "moderate")
                self.assertTrue(latitude.defaulted)

    def test_recorded_command_results_prioritize_selected_surface_then_recency(
        self,
    ) -> None:
        current = snapshot()
        current["world"]["hosts_by_name"]["cache"]["command_history"] = [
            {
                "id": "process-old",
                "command": "ps -ef",
                "surface": "process",
                "evidence_kind": "recorded_command_result",
                "stdout_excerpt": "redis-server and helper process",
                "exit_status": 0,
            },
            {
                "id": "network-new",
                "command": "ss -lntp",
                "surface": "network",
                "evidence_kind": "recorded_command_result",
                "stdout_excerpt": "tcp listener listing",
                "exit_status": 0,
            },
            {
                "id": "process-new",
                "command": "pgrep -a redis",
                "surface": "process",
                "evidence_kind": "recorded_command_result",
                "stdout_excerpt": "redis-server --port 6379",
                "stdout_omitted_chars": 12,
                "exit_status": 0,
            },
        ]
        prepared = build_coordinator_prompt_input(
            current,
            target_host="cache",
            trigger="manual",
            surface_hint="process",
            focus_hints=(),
        )

        brief, _selected, _latitude = build_expert_brief(
            current,
            target_host="cache",
            surface="process",
            expertise={
                "domain": "host process ecology",
                "perspective": "systems generalist",
            },
            question="Which process details complete the observed runtime?",
            selected_reference_ids=("r5",),
            reference_catalog=prepared.references,
        )

        command_results = [
            observation
            for observation in brief["observations"]
            if observation["kind"] == "recent_command_result"
        ]
        self.assertEqual(
            [item["details"]["id"] for item in command_results],
            ["process-new", "process-old", "network-new"],
        )
        self.assertEqual(
            command_results[0]["details"]["stdout_excerpt"],
            "redis-server --port 6379",
        )


if __name__ == "__main__":
    unittest.main()
