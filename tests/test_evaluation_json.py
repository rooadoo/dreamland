import hashlib
import json
import unittest
from dataclasses import asdict
from pathlib import Path

from dreamland.agent_structured import command_result_from_model_response
from dreamland.evaluation_json import (
    PARSER_SOURCE_PATH,
    POLICY_VERSION,
    SCORE_NAME,
    STRUCTURAL_RULE_IDS,
    SUPPORTED_PARSER_SHA256,
    SUPPORTED_RUNTIME_SOURCE_SHA256,
    assess_json_response,
)


def envelope(**overrides):
    return json.dumps(
        {
            "stdout": "ok\n",
            "stderr": "",
            "exit_status": 0,
            "state_update": {},
            **overrides,
        }
    )


def command_result(**overrides):
    return {
        "stdout": "ok\n",
        "stderr": "",
        "exit_status": 0,
        "metadata": {},
        **overrides,
    }


def rejection(raw=None, **metadata_overrides):
    metadata = {
        "structured_response_accepted": False,
        "dreamland.structured.failure_class": "invalid_json",
        "structured_parse_error": "invalid JSON: Expecting ',' delimiter",
        **metadata_overrides,
    }
    if raw is None:
        return {"metadata": metadata}
    metadata["dreamland.structured.raw_response"] = raw
    return command_result(
        stdout=raw if not raw or raw.endswith("\n") else raw + "\n", metadata=metadata
    )


class JsonAssessmentTests(unittest.TestCase):
    def test_policy_names_and_supported_parser_baseline_are_explicit(self):
        self.assertEqual(POLICY_VERSION, "emulator-json-v1")
        self.assertEqual(SCORE_NAME, "dreamland.json_status")
        self.assertEqual(PARSER_SOURCE_PATH, "src/dreamland/agent_structured.py")
        self.assertIn(
            "9d8144f0a48bd7b002ae4fa41c0428029b44b448f623726ae2f5e3e0e5fb7b69",
            SUPPORTED_PARSER_SHA256,
        )
        self.assertEqual(len(STRUCTURAL_RULE_IDS), len(set(STRUCTURAL_RULE_IDS)))

    def test_current_runtime_sources_match_the_audited_baseline(self):
        root = Path(__file__).resolve().parents[1]
        for relative_path, supported_digests in SUPPORTED_RUNTIME_SOURCE_SHA256.items():
            with self.subTest(source=relative_path):
                digest = hashlib.sha256((root / relative_path).read_bytes()).hexdigest()
                self.assertIn(
                    digest,
                    supported_digests,
                    "Runtime source changed; audit reconstruction before updating its baseline",
                )

    def test_whole_supported_fences_are_reconstructed_repairs(self):
        for language in ("json", "JSON", ""):
            with self.subTest(language=language):
                assessment = assess_json_response(
                    f" \n```{language}\r\n{envelope()}\r\n```\n",
                    command_result(),
                    parser_verified=True,
                )
                self.assertEqual(assessment.status, "repaired")
                self.assertEqual(
                    assessment.events,
                    (
                        {
                            "status": "repaired",
                            "rule": "markdown_fence_removed",
                            "evidence": "reconstructed",
                        },
                    ),
                )

    def test_trailing_envelope_after_commentary_is_reconstructed_repair(self):
        assessment = assess_json_response(
            "Thinking about {incomplete text\n" + envelope(),
            command_result(),
            parser_verified=True,
        )
        self.assertEqual(assessment.status, "repaired")
        self.assertEqual(assessment.events[0]["rule"], "leading_text_removed")

    def test_trailing_extraction_matches_the_last_complete_envelope(self):
        raw = envelope(stdout="ignored") + "\n" + envelope()
        self.assertEqual(
            assess_json_response(raw, command_result(), parser_verified=True).status,
            "repaired",
        )

    def test_ordinary_json_and_whitespace_are_not_repairs(self):
        for raw in (envelope(), " \n" + envelope() + "\n "):
            with self.subTest(raw=raw):
                assessment = assess_json_response(
                    raw, command_result(), parser_verified=True
                )
                self.assertEqual(assessment.status, "none_observed")
                self.assertEqual(assessment.events, ())

    def test_json_inside_stdout_does_not_trigger_repair(self):
        stdout = envelope()
        assessment = assess_json_response(
            envelope(stdout=stdout), command_result(stdout=stdout), parser_verified=True
        )
        self.assertEqual(assessment.status, "none_observed")

    def test_absent_raw_is_unknown_without_runtime_marker(self):
        for verified in (False, True):
            with self.subTest(verified=verified):
                self.assertEqual(
                    assess_json_response(
                        None, command_result(), parser_verified=verified
                    ).status,
                    "unknown",
                )

    def test_repairs_and_none_observed_require_verified_runtime_parser(self):
        for raw in (
            envelope(),
            "```json\n" + envelope() + "\n```",
            "Prose\n" + envelope(),
        ):
            with self.subTest(raw=raw):
                assessment = assess_json_response(
                    raw, command_result(), parser_verified=False
                )
                self.assertEqual(assessment.status, "unknown")
                self.assertEqual(assessment.events, ())

    def test_explicit_runtime_marker_needs_no_reconstruction(self):
        assessment = assess_json_response(None, rejection(), parser_verified=False)
        self.assertEqual(assessment.status, "rejected")
        self.assertEqual(
            assessment.events,
            (
                {
                    "status": "rejected",
                    "rule": "invalid_json",
                    "evidence": "runtime_marker",
                },
            ),
        )

    def test_broken_delimiters_are_rejected_without_attempting_repairs(self):
        for raw in (
            envelope()[:-1],
            '{"stdout": "unterminated',
            envelope().replace('", "stderr"', '" "stderr"'),
            envelope().replace('"ok\\n"', '"invalid\\q"'),
        ):
            with self.subTest(raw=raw):
                assessment = assess_json_response(
                    raw, rejection(raw), parser_verified=True
                )
                self.assertEqual(assessment.status, "rejected")
                self.assertEqual(len(assessment.events), 1)
                self.assertEqual(assessment.events[0]["rule"], "invalid_json")

    def test_failed_fence_recovery_is_rejected_without_repaired_event(self):
        raw = '```json\n{"stdout": "unfinished"\n```'
        assessment = assess_json_response(raw, rejection(raw), parser_verified=True)
        self.assertEqual(assessment.status, "rejected")
        self.assertEqual(
            [event["rule"] for event in assessment.events], ["invalid_json"]
        )

    def test_raw_metadata_can_supply_rejection_evidence(self):
        assessment = assess_json_response(
            None, rejection(envelope()[:-1]), parser_verified=True
        )
        self.assertEqual(assessment.status, "rejected")

    def test_malformed_text_without_runtime_rejection_is_unknown(self):
        raw = envelope()[:-1]
        assessment = assess_json_response(
            raw, command_result(stdout=raw + "\n"), parser_verified=True
        )
        self.assertEqual(assessment.status, "unknown")
        self.assertEqual(assessment.events, ())

    def test_plain_text_nonobjects_and_missing_opening_brace_are_unknown(self):
        for raw in ("terminal output", "null", "[]", "17", envelope()[1:]):
            with self.subTest(raw=raw):
                assessment = assess_json_response(
                    raw, command_result(stdout=raw + "\n"), parser_verified=True
                )
                self.assertEqual(assessment.status, "unknown")

    def test_valid_raw_or_unrecognized_raw_contradicts_verified_invalid_json_marker(
        self,
    ):
        for raw in (envelope(), "[]", envelope()[1:], "Prose\n" + envelope()):
            with self.subTest(raw=raw):
                assessment = assess_json_response(
                    raw, rejection(raw), parser_verified=True
                )
                self.assertEqual(assessment.status, "unknown")
                self.assertEqual(assessment.events, ())

    def test_direct_valid_json_contradicts_rejection_even_without_verified_parser(self):
        self.assertEqual(
            assess_json_response(
                envelope(), rejection(envelope()), parser_verified=False
            ).status,
            "unknown",
        )

    def test_invalid_json_class_requires_explicit_false_acceptance(self):
        for accepted in (None, True, 0, "false"):
            with self.subTest(accepted=accepted):
                result = rejection(structured_response_accepted=accepted)
                self.assertEqual(
                    assess_json_response(None, result, parser_verified=True).status,
                    "unknown",
                )

    def test_conflicting_error_and_failure_class_are_unknown(self):
        result = rejection(structured_parse_error="state_update must be an object")
        self.assertEqual(
            assess_json_response(None, result, parser_verified=True).status, "unknown"
        )

    def test_unclassified_rejection_is_unknown(self):
        result = command_result(metadata={"structured_response_accepted": False})
        self.assertEqual(
            assess_json_response(envelope(), result, parser_verified=True).status,
            "unknown",
        )

    def test_conflicting_raw_sources_are_unknown(self):
        assessment = assess_json_response(
            envelope()[:-2], rejection(envelope()[:-1]), parser_verified=True
        )
        self.assertEqual(assessment.status, "unknown")
        self.assertEqual(assessment.reason, "contradictory_raw_response")

    def test_processed_output_must_match_before_reconstructing_repair(self):
        raw = "```json\n" + envelope() + "\n```"
        for result in (
            command_result(stdout="different"),
            command_result(stderr="different"),
            command_result(exit_status=1),
            {"stdout": "ok\n", "metadata": {}},
        ):
            with self.subTest(result=result):
                self.assertEqual(
                    assess_json_response(raw, result, parser_verified=True).status,
                    "unknown",
                )

    def test_processed_output_cannot_contradict_verified_rejection(self):
        raw = envelope()[:-1]
        result = rejection(raw)
        result["stdout"] = "ok\n"
        self.assertEqual(
            assess_json_response(raw, result, parser_verified=True).status, "unknown"
        )

    def test_runtime_output_coercions_are_respected(self):
        for payload, result in (
            (
                {"stdout": None, "stderr": 7, "exit_status": "3"},
                command_result(stdout="", stderr="7", exit_status=3),
            ),
            (
                {"stdout": {"value": 1}, "exit_status": "bad"},
                command_result(stdout="{'value': 1}"),
            ),
            ({"exit_status": 2.9}, command_result(exit_status=2)),
            ({"exit_status": None}, command_result()),
            ({"exit_status": float("nan")}, command_result()),
        ):
            with self.subTest(payload=payload):
                raw = "Prose\n" + envelope(**payload)
                self.assertEqual(
                    assess_json_response(raw, result, parser_verified=True).status,
                    "repaired",
                )

    def test_overflowing_exit_status_cannot_prove_acceptance(self):
        raw = "Prose\n" + envelope(exit_status=float("inf"))
        self.assertEqual(
            assess_json_response(raw, command_result(), parser_verified=True).status,
            "unknown",
        )

    def test_non_json_validation_is_out_of_scope(self):
        for raw in ('{"stdout": "partial"}', envelope(state_update=[])):
            with self.subTest(raw=raw):
                self.assertEqual(
                    assess_json_response(
                        raw, command_result(), parser_verified=True
                    ).status,
                    "out_of_scope",
                )
        for failure_class in (
            "missing_required_keys",
            "invalid_state_update",
            "wrong_top_level_type",
        ):
            with self.subTest(failure_class=failure_class):
                result = command_result(
                    metadata={
                        "structured_response_accepted": False,
                        "dreamland.structured.failure_class": failure_class,
                    }
                )
                assessment = assess_json_response(None, result, parser_verified=False)
                self.assertEqual(assessment.status, "out_of_scope")
                self.assertEqual(assessment.events, ())

    def test_non_json_transition_interventions_are_not_json_flags(self):
        for metadata in (
            {"dreamland.transition.accepted": False},
            {"dreamland.transition.normalized": "implicit_program_push"},
        ):
            with self.subTest(metadata=metadata):
                assessment = assess_json_response(
                    envelope(), command_result(metadata=metadata), parser_verified=True
                )
                self.assertEqual(assessment.status, "out_of_scope")
                self.assertEqual(assessment.events, ())

    def test_runtime_ordered_effects_preserve_json_extraction_and_output_coercion(self):
        for prefix, suffix, expected in (
            ("", "", "none_observed"),
            ("```json\n", "\n```", "repaired"),
            ("Terminal result follows:\n", "", "repaired"),
        ):
            assessments = []
            for update in (
                {"cwd": "/tmp"},
                {"effects": [{"command": "cd /tmp", "state_update": {"cwd": "/tmp"}}]},
            ):
                with self.subTest(prefix=prefix, update=update):
                    raw = (
                        prefix
                        + envelope(
                            stdout=None,
                            stderr=7,
                            exit_status="3",
                            state_update=update,
                        )
                        + suffix
                    )
                    result = asdict(command_result_from_model_response(raw))
                    self.assertEqual(
                        (result["stdout"], result["stderr"], result["exit_status"]),
                        ("", "7", 3),
                    )
                    assessment = assess_json_response(raw, result, parser_verified=True)
                    self.assertEqual(assessment.status, expected)
                    assessments.append(assessment)
            self.assertEqual(assessments[0], assessments[1])

    def test_runtime_ordered_normalization_preserves_non_json_intervention_evidence(
        self,
    ):
        program_update = {
            "interactive_program": {"name": "python3", "command": "python3"}
        }
        for prefix, suffix, expected in (
            ("", "", "out_of_scope"),
            ("```json\n", "\n```", "repaired"),
        ):
            assessments = []
            for update in (
                program_update,
                {"effects": [{"command": "python3", "state_update": program_update}]},
            ):
                with self.subTest(prefix=prefix, update=update):
                    raw = prefix + envelope(state_update=update) + suffix
                    result = asdict(command_result_from_model_response(raw))
                    self.assertIn(
                        "implicit_program_push",
                        result["metadata"]["dreamland.transition.normalized"],
                    )
                    assessment = assess_json_response(raw, result, parser_verified=True)
                    self.assertEqual(assessment.status, expected)
                    assessments.append(assessment)
            self.assertEqual(assessments[0], assessments[1])

    def test_runtime_rejected_ordered_effects_remain_non_json_validation_failures(self):
        for update in (
            {"effects": []},
            {"effects": [{"command": "cd /tmp", "state_update": {"cwd": 7}}]},
            {
                "effects": [
                    {
                        "command": "exit",
                        "state_update": {
                            "shell_stack_transition": {"operation": "pop"}
                        },
                    },
                    {"command": "cd /tmp", "state_update": {"cwd": "/tmp"}},
                ]
            },
        ):
            for prefix, suffix in (("", ""), ("```json\n", "\n```")):
                with self.subTest(prefix=prefix, update=update):
                    raw = prefix + envelope(state_update=update) + suffix
                    result = asdict(command_result_from_model_response(raw))
                    self.assertFalse(result["metadata"]["structured_response_accepted"])
                    self.assertEqual(
                        result["metadata"]["dreamland.structured.failure_class"],
                        "validation_error",
                    )
                    assessment = assess_json_response(raw, result, parser_verified=True)
                    self.assertEqual(assessment.status, "out_of_scope")
                    self.assertEqual(assessment.reason, "non_json_structured_rejection")
                    self.assertEqual(assessment.events, ())

    def test_runtime_dialogue_normalization_preserves_json_assessment(self):
        for name, updated_name, transition in (
            ("ssh-keygen", "ssh-keygen", None),
            ("sudo", "bash", {"operation": "push"}),
        ):
            state = {
                "interactive_program": {
                    "name": name,
                    "command": "/usr/bin/" + name,
                    "argv": [name],
                    "metadata": {"phase": "password_auth"},
                }
            }
            update = {
                "interactive_program": {
                    "name": updated_name,
                    "command": "/usr/bin/" + updated_name,
                    "argv": [updated_name],
                    "metadata": {"phase": "next"},
                }
            }
            if transition is not None:
                update["shell_stack_transition"] = transition
            for prefix, suffix, expected in (
                ("", "", "out_of_scope"),
                ("```json\n", "\n```", "repaired"),
                ("Terminal result follows:\n", "", "repaired"),
            ):
                with self.subTest(program=name, prefix=prefix):
                    raw = prefix + envelope(state_update=update) + suffix
                    result = command_result_from_model_response(raw, state)
                    self.assertEqual(
                        result.state_update.shell_stack_transition.operation,
                        "replace_active",
                    )
                    self.assertIn("dreamland.transition.normalized", result.metadata)
                    assessment = assess_json_response(
                        raw, asdict(result), parser_verified=True
                    )
                    self.assertEqual(assessment.status, expected)

    def test_runtime_malformed_json_with_ordered_effects_remains_a_json_rejection(self):
        for update in (
            {"cwd": "/tmp"},
            {"effects": [{"command": "cd /tmp", "state_update": {"cwd": "/tmp"}}]},
        ):
            raw = envelope(state_update=update)[:-1]
            result = asdict(command_result_from_model_response(raw))
            assessment = assess_json_response(raw, result, parser_verified=True)
            self.assertEqual(assessment.status, "rejected")
            self.assertEqual(assessment.events[0]["rule"], "invalid_json")

    def test_structural_repair_remains_visible_alongside_non_json_intervention(self):
        assessment = assess_json_response(
            "Prose\n" + envelope(),
            command_result(metadata={"dreamland.transition.accepted": False}),
            parser_verified=True,
        )
        self.assertEqual(assessment.status, "repaired")
        self.assertEqual(len(assessment.events), 1)

    def test_interruption_prevents_all_positive_assessments(self):
        for raw, result in (
            (envelope(), command_result()),
            ("Prose\n" + envelope(), command_result()),
            (envelope()[:-1], rejection(envelope()[:-1])),
        ):
            with self.subTest(raw=raw):
                result["interrupted"] = True
                self.assertEqual(
                    assess_json_response(raw, result, parser_verified=True).status,
                    "unknown",
                )

    def test_unsupported_fence_and_trailing_commentary_are_not_repaired(self):
        for raw in (
            "```javascript\n" + envelope() + "\n```",
            envelope() + "\nCommentary",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    assess_json_response(
                        raw, command_result(), parser_verified=True
                    ).status,
                    "unknown",
                )

    def test_invalid_metadata_is_unknown_instead_of_crashing(self):
        for metadata in (
            None,
            [],
            {"dreamland.structured.raw_response": 17},
            {
                "dreamland.structured.failure_class": [],
                "structured_response_accepted": False,
            },
        ):
            with self.subTest(metadata=metadata):
                self.assertEqual(
                    assess_json_response(
                        envelope(),
                        command_result(metadata=metadata),
                        parser_verified=True,
                    ).status,
                    "unknown",
                )

    def test_assessment_does_not_include_response_content(self):
        raw = "sensitive example text\n" + envelope()
        assessment = assess_json_response(raw, command_result(), parser_verified=True)
        self.assertNotIn("sensitive", json.dumps(assessment.events))
        self.assertNotIn("sensitive", assessment.reason)


if __name__ == "__main__":
    unittest.main()
