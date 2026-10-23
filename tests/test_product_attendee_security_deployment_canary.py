from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dreamland.state_profiles import StateProfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_security_deployment_canary.py"
FIXTURE_PATH = (
    REPOSITORY_ROOT / "tests" / "product_attendee_security_deployment_canary_fixture.py"
)
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.8.0-rc.4"


def _load_tool(path: Path, name: str):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {name}")
    module = importlib.util.module_from_spec(specification)
    prior = sys.modules.get(name)
    sys.modules[name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        if prior is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prior
    return module


CANARY = _load_tool(
    TOOL_PATH,
    "dreamland_product_attendee_security_deployment_canary_test_tool",
)


def _write_profile(path: Path) -> None:
    profile = StateProfile(
        name="attendee-canary-fixture",
        level="world",
        created_at="2026-09-02T00:00:00+00:00",
        state={
            "world_state": {
                "name": "attendee-canary-world",
                "entry_host": "console",
                "active_host": "console",
                "hosts_by_name": {
                    "console": {
                        "login_banner": "Test console\n",
                        "hostname": "console",
                        "purpose": {"theme": "Provider-free canary test"},
                        "shell": {
                            "default": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["bash"],
                            }
                        },
                    }
                },
            }
        },
    )
    path.write_text(
        json.dumps(profile.to_document(), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _safe_environment() -> dict[str, str]:
    prefixes = ("ANTHROPIC_", "LANGCHAIN_", "LANGSMITH_", "OPENAI_", "XAI_")
    return {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(prefixes)
        and name.upper() != "LANGFUSE"
        and not name.upper().startswith("LANGFUSE_")
    }


class _TerminalProjectionClient:
    def __init__(self) -> None:
        transcript_id = "transcript:testfixture"
        result = {
            "outcome": "completed",
            "guest_termination": {"kind": "exited", "exit_status": 0},
            "state_effect": {"kind": "none"},
            "output_clipped": False,
            "usage": {"state": "settled", "tokens": 8_192, "spend_units": 1_000},
        }
        self.command = {
            "command_id": "command:test",
            "slot_generation": 1,
            "status": "completed",
            "accepted_at_ms": 1,
            "deadline_at_ms": 2,
            "terminal_at_ms": 3,
            "output": {"bytes": 2, "chunks": 1, "clipped": False},
            "result": result,
        }
        self.snapshot_payload = {
            "latest_command": copy.deepcopy(self.command),
            "coordinates": {
                "control_revision": 2,
                "command_slot_generation": 1,
                "accepted_state_revision": 0,
                "event": {
                    "stream_id": "stream:test",
                    "sequence": 6,
                    "event_id": "event:test:6",
                },
                "transcript": {
                    "transcript_id": transcript_id,
                    "position": 4,
                    "entry_id": "entry:testfixture:4",
                },
            },
        }
        self.event_position = copy.deepcopy(
            self.snapshot_payload["coordinates"]["event"]
        )
        output_base = {
            "encoding": "utf-8",
            "projection": "inert-text-v1",
            "transformed": False,
            "clipped": False,
            "omitted_before_bytes": 0,
            "omitted_after_bytes": 0,
        }
        self.event_items = [
            {
                "stream_id": "stream:test",
                "sequence": 2,
                "event_id": "event:test:2",
                "type": "command.running",
                "criticality": "informational",
                "command_id": "command:test",
                "data": {"slot_generation": 1},
            },
            {
                "stream_id": "stream:test",
                "sequence": 3,
                "event_id": "event:test:3",
                "type": "command.output",
                "criticality": "informational",
                "command_id": "command:test",
                "data": {
                    **output_base,
                    "stream": "stdout",
                    "text": "x\n",
                    "final": False,
                    "chunk_index": 0,
                    "transcript_position": 2,
                },
            },
            {
                "stream_id": "stream:test",
                "sequence": 4,
                "event_id": "event:test:4",
                "type": "command.output",
                "criticality": "informational",
                "command_id": "command:test",
                "data": {
                    **output_base,
                    "stream": "stdout",
                    "text": "",
                    "final": True,
                    "chunk_index": 1,
                    "transcript_position": 3,
                },
            },
            {
                "stream_id": "stream:test",
                "sequence": 5,
                "event_id": "event:test:5",
                "type": "command.output",
                "criticality": "informational",
                "command_id": "command:test",
                "data": {
                    **output_base,
                    "stream": "stderr",
                    "text": "",
                    "final": True,
                    "chunk_index": 0,
                    "transcript_position": 4,
                },
            },
            {
                "stream_id": "stream:test",
                "sequence": 6,
                "event_id": "event:test:6",
                "type": "command.completed",
                "criticality": "authoritative",
                "command_id": "command:test",
                "data": {
                    "slot_generation": 1,
                    "accepted_state_revision": 0,
                    "outcome": "completed",
                    "output_clipped": False,
                },
            },
        ]
        transcript_base = {
            "encoding": "utf-8",
            "projection": "inert-text-v1",
            "transformed": False,
            "clipped": False,
            "omitted_before_bytes": 0,
            "omitted_after_bytes": 0,
        }
        self.transcript_items = [
            {
                **transcript_base,
                "kind": "command",
                "command_id": "command:test",
                "text": CANARY.CANARY_COMMAND,
                "position": 1,
                "chunk_index": None,
                "final": None,
            },
            {
                **transcript_base,
                "kind": "stdout",
                "command_id": "command:test",
                "text": "x\n",
                "final": False,
                "chunk_index": 0,
                "position": 2,
            },
            {
                **transcript_base,
                "kind": "stdout",
                "command_id": "command:test",
                "text": "",
                "final": True,
                "chunk_index": 1,
                "position": 3,
            },
            {
                **transcript_base,
                "kind": "stderr",
                "command_id": "command:test",
                "text": "",
                "final": True,
                "chunk_index": 0,
                "position": 4,
            },
        ]
        for entry in self.transcript_items:
            entry["transcript_id"] = transcript_id
            entry["entry_id"] = f"entry:testfixture:{entry['position']}"
        self.transcript_position = copy.deepcopy(
            self.snapshot_payload["coordinates"]["transcript"]
        )
        self.snapshot_payload["transcript"] = {
            "entries": copy.deepcopy(self.transcript_items),
            "position": copy.deepcopy(self.transcript_position),
            "omitted_before_entries": 0,
            "omitted_after_entries": 0,
        }

    def command_result(self, **_arguments):
        return {"terminal": True, "command": copy.deepcopy(self.command)}

    def snapshot(self, **_arguments):
        return {"snapshot": copy.deepcopy(self.snapshot_payload)}

    def events(self, **arguments):
        if arguments["after"] == self.event_position:
            items = []
        else:
            items = copy.deepcopy(self.event_items)
        return {"events": items, "position": dict(self.event_position)}

    def transcript(self, **arguments):
        if arguments["after"] == self.transcript_position:
            items = []
        else:
            items = copy.deepcopy(self.transcript_items)
        return {
            "entries": items,
            "position": dict(self.transcript_position),
            "omitted_before_entries": 0,
            "omitted_after_entries": 0,
        }


def _terminal_workflow() -> object:
    return CANARY._Workflow(
        gateway_context_id="gateway-context:test",
        actor="actor:test",
        session_authority="session:test",
        reference="session-reference:test",
        control_revision=1,
        slot_generation=1,
        command_id="command:test",
    )


class ProductAttendeeSecurityDeploymentCanaryTests(unittest.TestCase):
    def _arguments(self, root: Path):
        profile = root / "profile.json"
        credential = root / "provider.key"
        _write_profile(profile)
        credential.write_text("\n", encoding="utf-8")
        credential.chmod(0o600)
        deployment = CANARY._load_tool("product_attendee_security_deployment")
        digest = deployment._LIVE.authored_digest(profile)
        arguments = CANARY._parser().parse_args(
            [
                "run",
                "--acknowledge-provider-spend",
                "--contract-root",
                str(CONTRACT_ROOT),
                "--profile-path",
                str(profile),
                "--profile-digest",
                digest,
                "--provider-credential-file",
                str(credential),
                "--model",
                "openai:gpt-provider-free-canary",
                "--max-output-tokens",
                "128",
            ]
        )
        return arguments, profile, credential, digest

    def test_import_is_inert_and_provider_free(self) -> None:
        script = """
import importlib.util, json, pathlib, sys, threading
scratch = pathlib.Path(sys.argv[2]); before = [(t.ident, t.name) for t in threading.enumerate()]
spec = importlib.util.spec_from_file_location("attendee_canary_inert", pathlib.Path(sys.argv[1]))
module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module
spec.loader.exec_module(module)
after = [(t.ident, t.name) for t in threading.enumerate()]
print(json.dumps({"threads": before == after, "files": list(scratch.iterdir()), "scope": module.LIVE_SCOPE}))
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-c", script, str(TOOL_PATH), directory],
                cwd=directory,
                check=True,
                capture_output=True,
                text=True,
                env={**_safe_environment(), "PYTHONDONTWRITEBYTECODE": "1"},
            )
        self.assertEqual(
            json.loads(result.stdout),
            {
                "threads": True,
                "files": [],
                "scope": "loopback_real_provider_single_command",
            },
        )
        self.assertEqual(result.stderr, "")

    def test_exact_rc4_pins_and_tree_are_enforced(self) -> None:
        deployment = CANARY._load_tool("product_attendee_security_deployment")
        manifest = CANARY._validate_exact_contract(CONTRACT_ROOT, deployment)
        self.assertEqual(manifest["contract_revision"], "0.8.0-rc.4")
        expected = {
            "manifest.json": CANARY.MANIFEST_SHA256,
            "openapi.json": CANARY.OPENAPI_SHA256,
            "generated/product_session_client.py": CANARY.GENERATED_CLIENT_SHA256,
        }
        for relative, digest in expected.items():
            with self.subTest(relative=relative):
                self.assertEqual(
                    hashlib.sha256((CONTRACT_ROOT / relative).read_bytes()).hexdigest(),
                    digest,
                )
        self.assertEqual(
            CANARY._tree_digest(CONTRACT_ROOT),
            (CANARY.CONTRACT_FILES, CANARY.CONTRACT_TREE_SHA256),
        )

        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "rc4"
            shutil.copytree(CONTRACT_ROOT, copied)
            client = copied / "generated" / "product_session_client.py"
            client.write_bytes(client.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "exact published RC4"):
                CANARY._validate_exact_contract(copied, deployment)

    def test_live_command_is_fixed_loopback_one_worker_one_command(self) -> None:
        configuration = CANARY._CanaryConfiguration(
            contract_root=Path("/contract"),
            profile_path=Path("/profile"),
            profile_digest="sha256:" + "a" * 64,
            credential_file=Path("/credential"),
            model="openai:gpt-canary",
            max_output_tokens=128,
            system_prompt="system.md",
            reasoning_effort=None,
            reasoning_summary=None,
            verbosity=None,
            speed=None,
            prompt_cache=None,
        )
        private = CANARY._PrivateFiles(
            root=Path("/private"),
            token_file=Path("/private/token"),
            bootstrap_file=Path("/private/bootstrap"),
            artifact_root=Path("/private/artifacts"),
            bearer="private-bearer",
            bff_authority="private-bff",
        )
        command = CANARY._live_deployment_command(
            configuration,
            private,
            ("2026-09-02T00:00:00Z", "2026-09-02T00:01:00Z", "2026-09-02T00:02:00Z"),
        )
        self.assertEqual(command[2], str(CANARY.DEPLOYMENT_TOOL))
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertEqual(command[command.index("--port") + 1], "0")
        self.assertEqual(command[command.index("--worker-capacity") + 1], "1")
        self.assertEqual(command[command.index("--max-commands") + 1], "1")
        self.assertEqual(
            command[command.index("--max-output-chunks-per-command") + 1],
            str(CANARY.CANARY_MAX_OUTPUT_CHUNKS_PER_COMMAND),
        )
        self.assertEqual(CANARY.PROVIDER_ATTEMPT_LIMIT, 1)

    def test_preflight_rejects_profile_mismatch_and_insecure_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments, _profile, credential, digest = self._arguments(Path(directory))
            with (
                mock.patch.object(CANARY, "_validate_exact_contract", return_value={}),
                mock.patch.object(
                    CANARY,
                    "_load_rc4_client",
                    return_value=SimpleNamespace(),
                ),
                mock.patch.dict(os.environ, _safe_environment(), clear=True),
            ):
                arguments.profile_digest = "sha256:" + "0" * 64
                with self.assertRaisesRegex(ValueError, "profile digest diverged"):
                    CANARY._preflight(arguments)

                arguments.profile_digest = digest
                with mock.patch(
                    "dreamland.product.attendee_live._read_provider_credential",
                    side_effect=AssertionError("credential content was read"),
                ):
                    configuration, *_unused = CANARY._preflight(arguments)
                self.assertEqual(configuration.credential_file, credential)

                credential.chmod(0o644)
                with self.assertRaisesRegex(ValueError, "credential file"):
                    CANARY._preflight(arguments)

    def test_full_provider_free_injected_canary_uses_rc4_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments, _profile, _credential, _digest = self._arguments(Path(directory))

            def fixture_command(configuration, private, availability):
                command = CANARY._live_deployment_command(
                    configuration,
                    private,
                    availability,
                )
                self.assertEqual(command[2], str(CANARY.DEPLOYMENT_TOOL))
                command[2] = str(FIXTURE_PATH)
                return command

            with mock.patch.dict(os.environ, _safe_environment(), clear=True):
                report = CANARY._execute_canary(
                    arguments,
                    scope=CANARY.FIXTURE_SCOPE,
                    command_factory=fixture_command,
                    expect_fixture_diagnostic=True,
                )

        CANARY._validate_report(report, sentinels=())
        self.assertEqual(report["scope"], CANARY.FIXTURE_SCOPE)
        self.assertFalse(report["live_provider_execution_evidence"])
        self.assertEqual(report["limits"]["physical_workers"], 1)
        self.assertEqual(report["limits"]["commands_per_session"], 1)
        self.assertEqual(
            report["limits"]["output_bytes_per_command"],
            CANARY.CANARY_MAX_OUTPUT_BYTES_PER_COMMAND,
        )
        self.assertEqual(
            report["limits"]["output_chunks_per_command"],
            CANARY.CANARY_MAX_OUTPUT_CHUNKS_PER_COMMAND,
        )
        self.assertEqual(report["evidence"]["commands_submitted"], 1)
        self.assertEqual(report["evidence"]["terminal_outcome"], "completed")
        self.assertEqual(
            report["evidence"]["published_bytes"],
            CANARY.CANARY_MAX_OUTPUT_BYTES_PER_COMMAND,
        )
        self.assertEqual(
            report["evidence"]["published_chunks"],
            CANARY.CANARY_MAX_OUTPUT_CHUNKS_PER_COMMAND,
        )
        self.assertTrue(report["evidence"]["session_closed"])
        self.assertTrue(report["evidence"]["context_revoked"])

    def test_parser_and_public_failures_are_typed_and_redacted(self) -> None:
        private_argument = "private-canary-argument-must-not-echo"
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(TOOL_PATH),
                "run",
                "--unknown-private-option",
                private_argument,
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            env={**_safe_environment(), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            json.loads(result.stderr),
            {
                "error": {
                    "code": "attendee_live_canary_failed",
                    "retry": "after_review",
                }
            },
        )
        self.assertNotIn(private_argument, result.stderr)
        self.assertNotIn("usage:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        parser = CANARY._parser()
        with self.assertRaises(ValueError):
            parser.parse_args([])
        common = [
            "run",
            "--acknowledge-provider-spend",
            "--profile-path",
            "/private/profile",
            "--profile-digest",
            "sha256:" + "a" * 64,
            "--provider-credential-file",
            "/private/credential",
            "--model",
            "openai:gpt-canary",
        ]
        for invalid in ("0", str(CANARY.CANARY_MAX_OUTPUT_TOKENS + 1)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parser.parse_args(common + ["--max-output-tokens", invalid])
        for valid in ("1", str(CANARY.CANARY_MAX_OUTPUT_TOKENS)):
            with self.subTest(valid=valid):
                parsed = parser.parse_args(common + ["--max-output-tokens", valid])
                self.assertEqual(parsed.max_output_tokens, int(valid))

    def test_execute_requires_explicit_spend_acknowledgement(self) -> None:
        arguments = mock.Mock(acknowledge_provider_spend=False)
        with self.assertRaisesRegex(ValueError, "spend was not acknowledged"):
            CANARY._execute_canary(arguments)

    def test_output_projection_requires_contiguous_data_then_one_final(self) -> None:
        valid = [
            ("stdout", "one\n", False, 0),
            ("stderr", "warning\n", False, 0),
            ("stdout", "two\n", False, 1),
            ("stdout", "", True, 2),
            ("stderr", "", True, 1),
        ]
        CANARY._validate_output_projection(valid)
        invalid = {
            "duplicate": [
                ("stdout", "one", False, 0),
                ("stdout", "two", False, 0),
                ("stdout", "", True, 1),
                ("stderr", "", True, 0),
            ],
            "gap": [
                ("stdout", "one", False, 0),
                ("stdout", "two", False, 2),
                ("stdout", "", True, 3),
                ("stderr", "", True, 0),
            ],
            "final_before_data": [
                ("stdout", "", True, 0),
                ("stdout", "late", False, 1),
                ("stderr", "", True, 0),
            ],
            "nonempty_final": [
                ("stdout", "final", True, 0),
                ("stderr", "", True, 0),
            ],
            "empty_data": [
                ("stdout", "", False, 0),
                ("stdout", "", True, 1),
                ("stderr", "", True, 0),
            ],
            "missing_stream_final": [
                ("stdout", "one", False, 0),
                ("stdout", "", True, 1),
            ],
        }
        for name, projection in invalid.items():
            with self.subTest(name=name), self.assertRaises(AssertionError):
                CANARY._validate_output_projection(projection)

    def test_terminal_rejects_usage_that_differs_from_announced_reservation(
        self,
    ) -> None:
        reservation = CANARY._UsageReservation(tokens=8_192, spend_units=1_000)
        workflow = CANARY._Workflow(
            gateway_context_id="gateway-context:test",
            actor="actor:test",
            session_authority="session:test",
            reference="session-reference:test",
            control_revision=1,
            slot_generation=1,
            command_id="command:test",
        )

        class Client:
            def __init__(self, usage):
                self.usage = usage

            def command_result(self, **_arguments):
                return {
                    "terminal": True,
                    "command": {
                        "command_id": workflow.command_id,
                        "status": "completed",
                        "result": {
                            "outcome": "completed",
                            "guest_termination": {
                                "kind": "exited",
                                "exit_status": 0,
                            },
                            "state_effect": {"kind": "none"},
                            "output_clipped": False,
                            "usage": self.usage,
                        },
                    },
                }

        for name, usage in {
            "tokens": {"state": "settled", "tokens": 1, "spend_units": 1_000},
            "spend": {"state": "settled", "tokens": 8_192, "spend_units": 1},
            "pending": {"state": "pending", "tokens": None, "spend_units": None},
        }.items():
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(AssertionError, "did not complete"),
            ):
                CANARY._validate_terminal_publication(
                    SimpleNamespace(),
                    Client(usage),
                    workflow,
                    epoch="epoch:test",
                    accepted_cursor={},
                    reservation=reservation,
                    sentinels=[],
                )

    def test_terminal_correlates_snapshot_lifecycle_event_and_transcript(
        self,
    ) -> None:
        def validate(client):
            return CANARY._validate_terminal_publication(
                SimpleNamespace(
                    validate_snapshot=lambda value: value,
                    TRANSCRIPT_ID_RE=re.compile(r"^transcript:[A-Za-z0-9_-]{8,96}$"),
                    ENTRY_ID_RE=re.compile(
                        r"^entry:[A-Za-z0-9_-]{8,96}:[1-9][0-9]{0,15}$"
                    ),
                ),
                client,
                _terminal_workflow(),
                epoch="epoch:test",
                accepted_cursor={
                    "stream_id": "stream:test",
                    "sequence": 1,
                    "event_id": "event:test:1",
                },
                reservation=CANARY._UsageReservation(
                    tokens=8_192,
                    spend_units=1_000,
                ),
                sentinels=[],
            )

        self.assertEqual(
            validate(_TerminalProjectionClient()), ("completed", "none", 2, 1)
        )

        snapshot_mismatch = _TerminalProjectionClient()
        snapshot_mismatch.snapshot_payload["latest_command"]["terminal_at_ms"] = 4
        with self.assertRaisesRegex(AssertionError, "snapshot"):
            validate(snapshot_mismatch)

        accepted_slot_mismatch = _TerminalProjectionClient()
        accepted_slot_mismatch.command["slot_generation"] = 2
        with self.assertRaisesRegex(AssertionError, "did not complete"):
            validate(accepted_slot_mismatch)

        snapshot_slot_mismatch = _TerminalProjectionClient()
        snapshot_slot_mismatch.snapshot_payload["coordinates"][
            "command_slot_generation"
        ] = 2
        with self.assertRaisesRegex(AssertionError, "coordinates"):
            validate(snapshot_slot_mismatch)

        event_order_mismatch = _TerminalProjectionClient()
        first_event = event_order_mismatch.event_items[0]
        second_event = event_order_mismatch.event_items[1]
        for field in ("type", "criticality", "data"):
            first_event[field], second_event[field] = (
                second_event[field],
                first_event[field],
            )
        with self.assertRaisesRegex(AssertionError, "lifecycle events"):
            validate(event_order_mismatch)

        terminal_event_mismatch = _TerminalProjectionClient()
        terminal_event_mismatch.event_items[-1]["data"]["output_clipped"] = True
        with self.assertRaisesRegex(AssertionError, "lifecycle data"):
            validate(terminal_event_mismatch)

        transcript_correlation_mismatch = _TerminalProjectionClient()
        transcript_correlation_mismatch.event_items[1]["data"][
            "transcript_position"
        ] = 99
        with self.assertRaisesRegex(AssertionError, "correlation"):
            validate(transcript_correlation_mismatch)

        event_tail_mismatch = _TerminalProjectionClient()
        event_tail_mismatch.event_position["event_id"] = "event:test:999"
        event_tail_mismatch.snapshot_payload["coordinates"]["event"]["event_id"] = (
            "event:test:999"
        )
        with self.assertRaisesRegex(AssertionError, "event replay sequence"):
            validate(event_tail_mismatch)

        transcript_tail_mismatch = _TerminalProjectionClient()
        bad_transcript_position = {
            "transcript_id": "transcript:testfixture",
            "position": 4,
            "entry_id": "entry:testfixture:99",
        }
        transcript_tail_mismatch.transcript_position = copy.deepcopy(
            bad_transcript_position
        )
        transcript_tail_mismatch.snapshot_payload["coordinates"]["transcript"] = (
            copy.deepcopy(bad_transcript_position)
        )
        transcript_tail_mismatch.snapshot_payload["transcript"]["position"] = (
            copy.deepcopy(bad_transcript_position)
        )
        with self.assertRaisesRegex(AssertionError, "transcript replay sequence"):
            validate(transcript_tail_mismatch)

        transcript_identity_mismatch = _TerminalProjectionClient()
        transcript_identity_mismatch.transcript_items[1]["entry_id"] = (
            "entry:testfixture:99"
        )
        transcript_identity_mismatch.snapshot_payload["transcript"]["entries"][1][
            "entry_id"
        ] = "entry:testfixture:99"
        with self.assertRaisesRegex(AssertionError, "transcript identity"):
            validate(transcript_identity_mismatch)

    def test_report_guard_rejects_private_keys_sentinels_and_live_claim(self) -> None:
        manifest = json.loads((CONTRACT_ROOT / "manifest.json").read_text("utf-8"))
        settings = CANARY._load_tool(
            "product_attendee_security_deployment"
        )._LIVE.AttendeeLiveProviderSettings(
            model="openai:gpt-provider-free-canary",
            system_prompt_name="system.md",
            max_output_tokens=128,
        )
        report = CANARY._build_report(
            scope=CANARY.FIXTURE_SCOPE,
            manifest=manifest,
            provider_settings=settings,
            evidence=CANARY._CanaryEvidence(
                terminal_outcome="completed",
                revision_effect="none",
                published_bytes=1,
                published_chunks=1,
                supervisor_exit_code=128 + signal.SIGTERM,
                elapsed_ms=1,
            ),
        )
        CANARY._validate_report(report, sentinels=())

        private_key = json.loads(json.dumps(report))
        private_key["secret"] = "must-not-cross"
        with self.assertRaisesRegex(ValueError, "private key"):
            CANARY._validate_report(private_key, sentinels=())

        sentinel = "gpt-provider-free-canary"
        with self.assertRaisesRegex(AssertionError, "private canary value"):
            CANARY._validate_report(report, sentinels=(sentinel,))

        false_live_claim = json.loads(json.dumps(report))
        false_live_claim["live_provider_execution_evidence"] = True
        with self.assertRaisesRegex(ValueError, "identity"):
            CANARY._validate_report(false_live_claim, sentinels=())

        unbounded_tokens = json.loads(json.dumps(report))
        unbounded_tokens["configuration"]["max_output_tokens"] = (
            CANARY.CANARY_MAX_OUTPUT_TOKENS + 1
        )
        with self.assertRaisesRegex(ValueError, "configuration"):
            CANARY._validate_report(unbounded_tokens, sentinels=())

        excess_chunks = json.loads(json.dumps(report))
        excess_chunks["evidence"]["published_chunks"] = (
            CANARY.CANARY_MAX_OUTPUT_CHUNKS_PER_COMMAND + 1
        )
        with self.assertRaisesRegex(ValueError, "publication"):
            CANARY._validate_report(excess_chunks, sentinels=())

    def test_signal_during_process_creation_cleans_registered_group(self) -> None:
        class CreatedProcess:
            pid = 424_242

            def __init__(self) -> None:
                self.returncode = None
                self.stdout = None

            def poll(self):
                return None if self.returncode is None else self.returncode

            def send_signal(self, _signum: int) -> None:
                return None

            def communicate(self, timeout: float):
                del timeout
                self.returncode = 143
                return "", ""

        with tempfile.TemporaryDirectory() as directory:
            process = CANARY._DeploymentProcess(
                command=["fixture"],
                private_root=Path(directory),
            )

            def interrupted_creation(*_args, **_kwargs):
                os.kill(os.getpid(), signal.SIGTERM)
                return CreatedProcess()

            with (
                mock.patch.object(CANARY.subprocess, "Popen", interrupted_creation),
                mock.patch.object(
                    CANARY,
                    "_process_group_alive",
                    side_effect=(True, False),
                ),
                mock.patch.object(CANARY.os, "killpg") as kill_group,
                self.assertRaises(CANARY._CanarySignal) as raised,
            ):
                CANARY._run_with_signal_cleanup(process.start)

            self.assertEqual(raised.exception.signum, signal.SIGTERM)
            self.assertEqual(process.process_group_id, 424_242)
            kill_group.assert_called_once_with(424_242, signal.SIGKILL)
            self.assertTrue(process._forced_cleanup_complete)
            self.assertEqual(CANARY._SIGNAL_DEFERRAL_DEPTH, 0)
            self.assertIsNone(CANARY._DEFERRED_CANARY_SIGNAL)

    def test_force_stop_kills_orphan_group_after_leader_exit(self) -> None:
        class ExitedLeader:
            returncode = 143

            def poll(self):
                return self.returncode

            def communicate(self, timeout: float):
                del timeout
                return "", ""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = CANARY._DeploymentProcess(command=["fixture"], private_root=root)
            process.process = ExitedLeader()
            process.process_group_id = 424_242
            process._stderr_path.write_text("", encoding="utf-8")
            with (
                mock.patch.object(
                    CANARY,
                    "_process_group_alive",
                    side_effect=(True, False),
                ),
                mock.patch.object(CANARY.os, "killpg") as kill_group,
            ):
                process.force_stop()
            kill_group.assert_called_once_with(424_242, signal.SIGKILL)
            self.assertTrue(process._forced_cleanup_complete)


if __name__ == "__main__":
    unittest.main()
