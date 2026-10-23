from __future__ import annotations

import copy
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.3.0-rc.1"
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_session_contract.py"
GENERATED_CLIENT_PATH = CONTRACT_ROOT / "generated" / "product_session_client.py"
SPEC = importlib.util.spec_from_file_location("product_session_contract", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
CONTRACT_TOOL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONTRACT_TOOL
SPEC.loader.exec_module(CONTRACT_TOOL)


class ProductSessionContractTests(unittest.TestCase):
    def test_contract_validates_with_complete_disabled_unfrozen_inventory(self) -> None:
        result = CONTRACT_TOOL.validate_contract(CONTRACT_ROOT)

        self.assertEqual(result["contract_revision"], "0.3.0-rc.1")
        self.assertEqual(result["compatibility"], "unfrozen-release-candidate")
        self.assertEqual(result["routes"], 12)
        self.assertEqual(result["fixtures"], 99)
        self.assertEqual(result["known_event_types"], 10)
        self.assertEqual(result["safe_error_codes"], 30)
        self.assertEqual(result["recovery_scenarios"], 7)
        self.assertEqual(result["capabilities_enabled"], 0)
        self.assertEqual(
            result["freeze_status"],
            "pending_ui_tracked_doc_acknowledgement",
        )

    def test_openapi_surface_is_exact_private_and_has_no_server(self) -> None:
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")

        self.assertEqual(openapi["servers"], [])
        self.assertEqual(openapi["security"], [{"ProductServiceBearer": []}])
        self.assertEqual(set(openapi["paths"]), set(CONTRACT_TOOL.EXPECTED_OPERATIONS))
        for path, (method, operation_id) in CONTRACT_TOOL.EXPECTED_OPERATIONS.items():
            self.assertEqual(set(openapi["paths"][path]), {method})
            operation = openapi["paths"][path][method]
            self.assertEqual(operation["operationId"], operation_id)
            self.assertEqual(operation["security"], [{"ProductServiceBearer": []}])
            self.assertEqual(
                operation["x-dreamland-availability"],
                "integration-preview-only",
            )
        self.assertEqual(
            set(openapi["components"]["responses"]["SafeError"]["content"]),
            {"application/problem+json"},
        )

    def test_async_submit_is_202_and_event_route_negotiates_json_and_sse(self) -> None:
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")
        submit = openapi["paths"]["/product/v1/sessions/{reference}/commands"]["post"]
        events = openapi["paths"]["/product/v1/sessions/{reference}/events"]["get"]

        self.assertIn("202", submit["responses"])
        self.assertNotIn("200", submit["responses"])
        self.assertEqual(
            set(events["responses"]["200"]["content"]),
            {"application/json", "text/event-stream"},
        )
        self.assertEqual(events["x-dreamland-cursor-query"]["presence"], "all-or-none")
        self.assertEqual(
            events["x-dreamland-cursor-query"]["last_event_id"],
            "sse-only-and-mutually-exclusive-with-query-cursor",
        )

    def test_every_mutation_declares_epoch_bound_idempotency(self) -> None:
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")
        found: set[str] = set()
        for path_item in openapi["paths"].values():
            for operation in path_item.values():
                operation_id = operation["operationId"]
                if operation_id not in CONTRACT_TOOL.MUTATION_OPERATIONS:
                    continue
                found.add(operation_id)
                self.assertEqual(
                    operation["x-dreamland-idempotency"]["policy"],
                    "#/x-dreamland-idempotency-policy",
                )
                parameter_names = {
                    parameter["$ref"].rsplit("/", 1)[-1]
                    for parameter in operation["parameters"]
                }
                self.assertIn("ExpectedEpoch", parameter_names)
                self.assertIn("ActorContext", parameter_names)
                self.assertIn("IdempotencyKey", parameter_names)
        self.assertEqual(found, CONTRACT_TOOL.MUTATION_OPERATIONS)

    def test_manifest_is_unfrozen_and_all_capabilities_disabled(
        self,
    ) -> None:
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")

        if manifest["publication_status"] == "release_candidate":
            self.assertIsNone(manifest["backend_commit"])
            self.assertIsNone(manifest["published_at"])
        else:
            self.assertEqual(manifest["publication_status"], "published")
            self.assertRegex(manifest["backend_commit"], r"^[0-9a-f]{40}$")
            self.assertRegex(
                manifest["published_at"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
            )
        self.assertEqual(manifest["implementation_status"], "integration_preview_only")
        self.assertEqual(
            manifest["transport_status"],
            "manually_invoked_loopback_preview_only",
        )
        for state in manifest["capabilities"].values():
            self.assertFalse(state["enabled"])
            self.assertIsNone(state["enabled_revision"])
            self.assertEqual(state["reason"], "integration_preview_only")

    def test_response_context_explicitly_disclaims_provider_and_live_evidence(
        self,
    ) -> None:
        payload = CONTRACT_TOOL.load_json(
            CONTRACT_ROOT
            / "fixtures"
            / "canonical-valid"
            / "capabilities"
            / "all-disabled.json"
        )

        self.assertEqual(
            payload["contract"]["compatibility"],
            "unfrozen-release-candidate",
        )
        self.assertEqual(
            payload["preview"],
            {
                "test_only": True,
                "integration_only": True,
                "executor": "deterministic-no-provider-v1",
                "provider_calls": False,
                "live_capability_evidence": False,
            },
        )

    def test_terminal_result_schemas_pin_tagged_unions_and_late_usage_truth(
        self,
    ) -> None:
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")
        events = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "events.schema.json")
        documents = {"openapi.json": openapi, "events.schema.json": events}
        schema, _target = CONTRACT_TOOL._schema_target(
            "openapi.json#/components/schemas/TerminalCommandResult",
            documents,
        )
        assert schema is not None
        valid_paths = (
            "completed-exited-none.json",
            "completed-exited-committed.json",
            "completed-signaled-rejected.json",
            "failed-pending-usage.json",
            "failed-settled-usage.json",
            "interrupted.json",
        )
        for filename in valid_paths:
            response = CONTRACT_TOOL.load_json(
                CONTRACT_ROOT / "fixtures" / "canonical-valid" / "commands" / filename
            )
            self.assertEqual(
                CONTRACT_TOOL._validation_errors(schema, response["command"]["result"]),
                [],
                filename,
            )

    def test_every_current_event_type_has_a_canonical_fixture(self) -> None:
        index = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "fixtures" / "index.json")

        self.assertEqual(
            set(index["coverage"]["event_types"]),
            set(CONTRACT_TOOL.KNOWN_EVENT_TYPES),
        )
        canonical_types = set()
        for item in index["fixtures"]:
            if (
                item["class"] == "canonical-valid"
                and item["schema_target"] == "events.schema.json#/$defs/ProductEvent"
            ):
                payload = CONTRACT_TOOL.load_json(CONTRACT_ROOT / item["path"])
                canonical_types.add(payload["type"])
        self.assertEqual(canonical_types, set(CONTRACT_TOOL.KNOWN_EVENT_TYPES))

    def test_nested_event_positions_receive_semantic_validation(self) -> None:
        with self.assertRaisesRegex(
            CONTRACT_TOOL.ContractError,
            "event fixture position mismatch",
        ):
            CONTRACT_TOOL._validate_event_semantics(
                {"events": [{"event_id": "event:stream:2", "sequence": 3}]},
                case="nested-event",
            )

    def test_unknown_event_policy_is_criticality_sensitive(self) -> None:
        schema = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "events.schema.json")

        self.assertEqual(
            schema["x-dreamland-unknown-event-policy"],
            {
                "informational": "ignore_payload_and_advance_cursor",
                "authoritative": "stop_incremental_apply_and_resume_snapshot",
            },
        )
        index = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "fixtures" / "index.json")
        actions = {item["case"]: item["client_action"] for item in index["fixtures"]}
        self.assertEqual(
            actions["forward_unknown_informational_event"],
            "ignore_payload_and_advance_cursor",
        )
        self.assertEqual(
            actions["forward_unknown_authoritative_event"],
            "stop_incremental_apply_and_resume_snapshot",
        )

    def test_recovery_fixtures_cover_response_loss_event_withholding_and_epoch_loss(
        self,
    ) -> None:
        index = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "fixtures" / "index.json")
        expected = {
            "create_response_lost_after_commit",
            "command_accept_response_lost",
            "promised_terminal_event_withheld",
            "cancel_ack_response_lost",
            "event_cursor_evicted",
            "epoch_rotated_after_uncertain_mutation",
            "forced_terminal_usage_settles_late",
        }

        self.assertEqual(set(index["coverage"]["recovery_scenarios"]), expected)
        for item in index["fixtures"]:
            if item["schema_target"].endswith("IntegrationRecoveryFixture"):
                payload = CONTRACT_TOOL.load_json(CONTRACT_ROOT / item["path"])
                self.assertFalse(payload["automatic_new_key_redispatch"])
        withheld = CONTRACT_TOOL.load_json(
            CONTRACT_ROOT
            / "fixtures"
            / "canonical-valid"
            / "recovery"
            / "promised-terminal-event-withheld.json"
        )
        self.assertEqual(withheld["reconcile_with"], "command_result")

    def test_safe_error_fixtures_cover_canonical_transport_projection(self) -> None:
        index = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "fixtures" / "index.json")

        self.assertEqual(
            set(index["coverage"]["safe_error_codes"]),
            set(CONTRACT_TOOL.SAFE_ERROR_CASES),
        )
        self.assertEqual(CONTRACT_TOOL.SAFE_ERROR_CASES["session_lost"][0], 410)
        self.assertEqual(CONTRACT_TOOL.SAFE_ERROR_CASES["outcome_unknown"][0], 410)
        self.assertEqual(
            CONTRACT_TOOL.SAFE_ERROR_CASES["compound_syntax_unsupported"][0],
            422,
        )

    def test_generated_server_side_client_compiles_and_checks_recovery_shapes(
        self,
    ) -> None:
        result = CONTRACT_TOOL.client_smoke(CONTRACT_ROOT)

        self.assertEqual(result["validated_client_fixtures"], 6)
        self.assertEqual(result["generated_path"], "temporary")
        self.assertEqual(result["capabilities_enabled"], 0)

    def test_generated_client_normalizes_canonical_initial_cursors(self) -> None:
        specification = importlib.util.spec_from_file_location(
            "dreamland_product_session_generated_cursor_test",
            GENERATED_CLIENT_PATH,
        )
        self.assertIsNotNone(specification)
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        prior_bytecode_setting = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            specification.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = prior_bytecode_setting

        self.assertIsNone(
            module._event_cursor_query(
                {
                    "stream_id": "stream:initial_cursor_0001",
                    "sequence": 0,
                    "event_id": None,
                }
            )
        )
        self.assertIsNone(
            module._transcript_cursor_query(
                {
                    "transcript_id": "transcript:initial_cursor_0001",
                    "position": 0,
                    "entry_id": None,
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "null item ID"):
            module._event_cursor_query(
                {
                    "stream_id": "stream:initial_cursor_0001",
                    "sequence": 0,
                    "event_id": "event:initial_cursor_0001:1",
                }
            )

    def test_refresh_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "v0.3.0-rc.1"
            shutil.copytree(CONTRACT_ROOT, copied)
            first = CONTRACT_TOOL.refresh_contract(copied)
            first_bytes = {
                path.relative_to(copied).as_posix(): path.read_bytes()
                for path in copied.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            }
            second = CONTRACT_TOOL.refresh_contract(copied)
            second_bytes = {
                path.relative_to(copied).as_posix(): path.read_bytes()
                for path in copied.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            }

        self.assertEqual(first, second)
        self.assertEqual(first_bytes, second_bytes)

    def test_semantic_openapi_drift_cannot_be_blessed_by_manifest_digest(self) -> None:
        with self._copied_contract() as copied:
            openapi_path = copied / "openapi.json"
            openapi = CONTRACT_TOOL.load_json(openapi_path)
            openapi["servers"] = [{"url": "https://product.example"}]
            CONTRACT_TOOL.write_json(openapi_path, openapi)
            manifest_path = copied / "manifest.json"
            manifest = CONTRACT_TOOL.load_json(manifest_path)
            manifest["artifacts"]["openapi_sha256"] = CONTRACT_TOOL.sha256_file(
                openapi_path
            )
            CONTRACT_TOOL.write_json(manifest_path, manifest)

            with self.assertRaisesRegex(
                CONTRACT_TOOL.ContractError, "reviewed Phase 2 trust anchor"
            ):
                CONTRACT_TOOL.validate_contract(copied)

    def test_manifest_digest_tamper_is_rejected(self) -> None:
        with self._copied_contract() as copied:
            manifest_path = copied / "manifest.json"
            manifest = CONTRACT_TOOL.load_json(manifest_path)
            manifest["artifacts"]["events_schema_sha256"] = "0" * 64
            CONTRACT_TOOL.write_json(manifest_path, manifest)

            with self.assertRaisesRegex(
                CONTRACT_TOOL.ContractError, "artifact digest mismatch"
            ):
                CONTRACT_TOOL.validate_contract(copied)

    def test_fixture_tamper_is_rejected_even_when_json_remains_valid(self) -> None:
        with self._copied_contract() as copied:
            fixture_path = (
                copied
                / "fixtures"
                / "canonical-valid"
                / "recovery"
                / "command-accept-response-lost.json"
            )
            fixture = CONTRACT_TOOL.load_json(fixture_path)
            fixture["same_key_replay"] = False
            CONTRACT_TOOL.write_json(fixture_path, fixture)

            with self.assertRaisesRegex(
                CONTRACT_TOOL.ContractError,
                "fixture digest mismatch|fixtures/index.json",
            ):
                CONTRACT_TOOL.validate_contract(copied)

    def test_generated_client_tamper_is_rejected(self) -> None:
        with self._copied_contract() as copied:
            client_path = copied / "generated" / "product_session_client.py"
            client_path.write_text(
                client_path.read_text(encoding="utf-8") + "# drift\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                CONTRACT_TOOL.ContractError,
                "generated server-side client|artifact digest mismatch",
            ):
                CONTRACT_TOOL.validate_contract(copied)

    def test_publish_requires_exact_reachable_artifact_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.email", "contract@example.invalid")
            self._git(repository, "config", "user.name", "Contract Test")
            marker = repository / "README.md"
            marker.write_text("test\n", encoding="utf-8")
            self._git(repository, "add", "README.md")
            self._git(repository, "commit", "--quiet", "-m", "initial")
            commit = self._git(repository, "rev-parse", "HEAD").stdout.strip()
            copied = repository / "contracts" / "product-api" / "v0.3.0-rc.1"
            shutil.copytree(CONTRACT_ROOT, copied)
            self._make_unpublished(copied)

            with self.assertRaisesRegex(
                CONTRACT_TOOL.ContractError,
                "does not contain the exact product-session artifact",
            ):
                CONTRACT_TOOL.publish_contract(
                    copied,
                    backend_commit=commit,
                    published_at="2026-08-23T12:00:00Z",
                )

    def test_publish_records_metadata_after_exact_artifact_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            copied = repository / "contracts" / "product-api" / "v0.3.0-rc.1"
            copied.parent.mkdir(parents=True)
            shutil.copytree(CONTRACT_ROOT, copied)
            self._make_unpublished(copied)
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.email", "contract@example.invalid")
            self._git(repository, "config", "user.name", "Contract Test")
            self._git(repository, "add", "contracts")
            self._git(repository, "commit", "--quiet", "-m", "contract")
            commit = self._git(repository, "rev-parse", "HEAD").stdout.strip()

            result = CONTRACT_TOOL.publish_contract(
                copied,
                backend_commit=commit,
                published_at="2026-08-23T12:00:00Z",
            )
            manifest = CONTRACT_TOOL.load_json(copied / "manifest.json")

        self.assertEqual(result["capabilities_enabled"], 0)
        self.assertEqual(manifest["publication_status"], "published")
        self.assertEqual(manifest["backend_commit"], commit)
        self.assertEqual(manifest["published_at"], "2026-08-23T12:00:00Z")
        self.assertEqual(
            manifest["freeze_status"],
            "pending_ui_tracked_doc_acknowledgement",
        )

    def test_publish_rejects_malformed_commit_and_timestamp(self) -> None:
        with self.assertRaisesRegex(CONTRACT_TOOL.ContractError, "40 lowercase hex"):
            CONTRACT_TOOL.publish_contract(
                CONTRACT_ROOT,
                backend_commit="ABC",
                published_at="2026-08-23T12:00:00Z",
            )
        with self.assertRaisesRegex(CONTRACT_TOOL.ContractError, "RFC 3339"):
            CONTRACT_TOOL.publish_contract(
                CONTRACT_ROOT,
                backend_commit="0" * 40,
                published_at="not-a-date",
            )

    def test_manifest_schema_rejects_capability_enablement(self) -> None:
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")
        schema = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.schema.json")
        enabled = copy.deepcopy(manifest)
        enabled["capabilities"]["product_session_core"]["enabled"] = True

        errors = CONTRACT_TOOL._validation_errors(schema, enabled)
        self.assertTrue(errors)

    def _copied_contract(self):
        class CopiedContract:
            def __init__(self) -> None:
                self.directory: tempfile.TemporaryDirectory[str] | None = None
                self.path: Path | None = None

            def __enter__(inner_self) -> Path:
                inner_self.directory = tempfile.TemporaryDirectory()
                inner_self.path = Path(inner_self.directory.name) / "v0.3.0-rc.1"
                shutil.copytree(CONTRACT_ROOT, inner_self.path)
                return inner_self.path

            def __exit__(inner_self, *_args: object) -> None:
                assert inner_self.directory is not None
                inner_self.directory.cleanup()

        return CopiedContract()

    def _make_unpublished(self, contract_root: Path) -> None:
        manifest_path = contract_root / "manifest.json"
        manifest = CONTRACT_TOOL.load_json(manifest_path)
        manifest["backend_commit"] = None
        manifest["publication_status"] = "release_candidate"
        manifest["published_at"] = None
        CONTRACT_TOOL.write_json(manifest_path, manifest)

    def _git(
        self, repository: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
