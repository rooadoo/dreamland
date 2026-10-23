from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import ModuleType

from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_forge_stage_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.5.0-rc.2"
STAGE_RC2_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.4.0-rc.2"
FORGE_RC4_ROOT = REPOSITORY_ROOT / "contracts" / "world-forge-api" / "v0.1.0-rc.4"


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {path.name}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load_module("dreamland_forge_stage_contract_test_tool", TOOL_PATH)


class ProductForgeStageContractTests(unittest.TestCase):
    def test_generated_release_candidate_validates_and_client_smokes(self) -> None:
        validated = TOOL.validate_contract(CONTRACT_ROOT)
        smoked = TOOL.client_smoke(CONTRACT_ROOT)

        self.assertEqual(validated["contract_revision"], "0.5.0-rc.2")
        self.assertEqual(validated["routes"], 17)
        self.assertEqual(validated["capabilities_enabled"], 0)
        self.assertEqual(smoked["validated_client_fixtures"], 8)
        self.assertEqual(smoked["rejected_client_mutations"], 11)

    def test_refresh_refuses_any_identifiable_published_root(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "published Forge-to-Stage contract artifacts are immutable",
        ):
            TOOL.refresh_contract(CONTRACT_ROOT)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "v0.5.0-rc.2"
            root.mkdir()
            (root / "manifest.json").write_text(
                json.dumps({"publication_status": "published"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "published Forge-to-Stage contract artifacts are immutable",
            ):
                TOOL.refresh_contract(root)

    def test_rc2_routes_remain_and_handoff_routes_are_strictly_additive(self) -> None:
        rc2 = json.loads((STAGE_RC2_ROOT / "openapi.json").read_text())
        current = TOOL.build_openapi()

        self.assertEqual(
            set(current["paths"]),
            set(rc2["paths"])
            | {
                "/product/v1/stage/forge-handoffs",
                "/product/v1/stage/sessions/{reference}/forge-replacement",
            },
        )
        for path, item in rc2["paths"].items():
            self.assertEqual(current["paths"][path], item)

        launch = current["paths"]["/product/v1/stage/forge-handoffs"]["post"]
        parameter_names = {
            parameter["$ref"].rsplit("/", 1)[-1] for parameter in launch["parameters"]
        }
        self.assertEqual(
            parameter_names,
            {
                "ExpectedEpoch",
                "ForgeExpectedEpoch",
                "ActorContext",
                "IdempotencyKey",
            },
        )
        self.assertEqual(
            current["components"]["parameters"]["ForgeExpectedEpoch"]["name"],
            "X-Dreamland-Forge-Expected-Epoch",
        )
        for path in (
            "/product/v1/stage/forge-handoffs",
            "/product/v1/stage/sessions/{reference}/forge-replacement",
        ):
            binding = current["paths"][path]["post"]["x-dreamland-idempotency"]
            self.assertEqual(
                binding["semantic_request_evidence"],
                {
                    "canonical_body": True,
                    "included_headers": ["X-Dreamland-Forge-Expected-Epoch"],
                },
            )
            self.assertEqual(
                binding["changed_semantic_evidence"],
                "idempotency_conflict_requires_fresh_key_after_reconciliation",
            )
        legacy_binding = current["paths"]["/product/v1/stage/sessions"]["post"][
            "x-dreamland-idempotency"
        ]
        self.assertNotIn("semantic_request_evidence", legacy_binding)

    def test_requests_accept_only_non_authorizing_forge_reference(self) -> None:
        openapi = TOOL.build_openapi()
        launch_schema = openapi["components"]["schemas"]["ForgeStageHandoffRequest"]
        replacement_schema = openapi["components"]["schemas"][
            "ForgeStageReplacementRequest"
        ]
        resolver = {**openapi, "$ref": "#/components/schemas/ForgeStageHandoffRequest"}
        validator = Draft202012Validator(resolver)

        self.assertEqual(set(launch_schema["properties"]), {"forge_reference"})
        self.assertEqual(
            set(replacement_schema["properties"]),
            {"forge_reference", "expected_control_revision"},
        )
        self.assertFalse(launch_schema["additionalProperties"])
        self.assertFalse(replacement_schema["additionalProperties"])
        self.assertFalse(
            validator.is_valid(
                {
                    "forge_reference": "forge-reference:phase5_job_a_00000001",
                    "artifact_path": "/tmp/untrusted.json",
                }
            )
        )
        self.assertNotIn("launch_grant", json.dumps(launch_schema))
        schemas = openapi["components"]["schemas"]
        for name in ("ForgeServiceEpoch", "ForgeReference", "ForgeJobId"):
            self.assertIn("{16,128}", schemas[name]["pattern"])
            self.assertNotIn("{8,128}", schemas[name]["pattern"])
        selection = schemas["ForgeSelectionProjection"]["properties"]
        source = schemas["ForgeStageSource"]["properties"]
        self.assertEqual(
            selection["selected_record_id"]["$ref"],
            "#/components/schemas/ForgeSelectedRecordId",
        )
        self.assertEqual(
            source["selected_record_id"]["$ref"],
            "#/components/schemas/ForgeSelectedRecordId",
        )
        self.assertEqual(
            selection["policy_id"]["pattern"],
            r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
        )
        self.assertEqual(
            schemas["ForgeSelectedRevision"]["properties"]["revision_id"]["$ref"],
            "#/components/schemas/ForgeImmutableRevisionId",
        )

    def test_schema_rejects_incoherent_terminal_replacement_projections(self) -> None:
        openapi = TOOL.build_openapi()
        validator = Draft202012Validator(
            {
                **openapi,
                "$ref": "#/components/schemas/ForgeStageReplacementResponse",
            }
        )
        replacement = deepcopy(
            next(
                fixture.payload
                for fixture in TOOL.fixture_definitions()
                if fixture.case == "forge_stage_replacement_success"
            )
        )
        self.assertTrue(validator.is_valid(replacement))

        mutations = []
        changed = deepcopy(replacement)
        changed["previous_snapshot"]["available_controls"] = ["submit"]
        mutations.append(changed)
        changed = deepcopy(replacement)
        changed["previous_snapshot"]["command_slot"]["state"] = "available"
        mutations.append(changed)
        changed = deepcopy(replacement)
        changed["previous_snapshot"]["command_slot"].update(
            {
                "state": "occupied",
                "generation": 1,
                "active_command_id": "command:phase5_terminal_invalid_0001",
            }
        )
        mutations.append(changed)
        changed = deepcopy(replacement)
        changed["previous_snapshot"]["recovery"]["tombstone"] = False
        mutations.append(changed)
        changed = deepcopy(replacement)
        changed["previous_snapshot"]["recovery"]["retain_until_ms"] = None
        mutations.append(changed)
        changed = deepcopy(replacement)
        changed["previous_snapshot"]["lifecycle"]["terminal_reason"] = "closed"
        mutations.append(changed)

        for changed in mutations:
            self.assertFalse(validator.is_valid(changed))

        # Draft 2020-12 cannot compare values held in two sibling properties.
        semantic_only = deepcopy(replacement)
        semantic_only["previous_snapshot"]["recovery"]["retain_until_ms"] = (
            semantic_only["previous_snapshot"]["lifecycle"]["terminal_at_ms"]
        )
        self.assertTrue(validator.is_valid(semantic_only))

    def test_fixtures_preserve_exact_selected_artifact_identity(self) -> None:
        fixtures = {
            fixture.case: fixture.payload for fixture in TOOL.fixture_definitions()
        }
        for case in (
            "forge_stage_handoff_created",
            "forge_stage_handoff_resumed",
            "forge_stage_handoff_exact_retry",
            "forge_stage_replacement_success",
        ):
            payload = fixtures[case]
            snapshot = payload["snapshot"]
            selection = payload["forge_selection"]
            source = snapshot["stage"]["source"]
            revision = selection["revision"]
            self.assertEqual(
                (
                    source["selected_record_id"],
                    snapshot["starting_revision"]["revision_id"],
                    source["immutable_digest"],
                    source["forge_job_id"],
                    source["forge_reference"],
                ),
                (
                    selection["selected_record_id"],
                    revision["revision_id"],
                    revision["immutable_digest"],
                    selection["job_id"],
                    selection["reference"],
                ),
            )

        self.assertTrue(fixtures["forge_stage_handoff_exact_retry"]["replayed"])
        self.assertTrue(fixtures["forge_stage_launch_response_lost"]["same_key_replay"])
        self.assertEqual(
            fixtures["forge_stage_launch_response_lost"]["changed_body_outcome"],
            "idempotency_conflict",
        )
        replacement = fixtures["forge_stage_replacement_success"]
        previous = replacement["previous_snapshot"]
        self.assertEqual(previous["lifecycle"]["state"], "replaced")
        self.assertEqual(previous["lifecycle"]["terminal_reason"], "replaced")
        self.assertEqual(previous["available_controls"], [])
        self.assertEqual(
            previous["command_slot"],
            {"state": "terminal", "generation": 0, "active_command_id": None},
        )
        self.assertTrue(previous["recovery"]["tombstone"])
        self.assertGreater(
            previous["recovery"]["retain_until_ms"],
            previous["lifecycle"]["terminal_at_ms"],
        )
        self.assertEqual(previous["coordinates"]["control_revision"], 2)
        self.assertEqual(
            previous["coordinates"]["event"],
            {
                "event_id": "event:phase2alphaevent:2",
                "sequence": 2,
                "stream_id": "stream:phase2alphaevents",
            },
        )
        self.assertEqual(replacement["snapshot"]["coordinates"]["control_revision"], 1)
        self.assertEqual(
            previous["stage"]["source"]["selected_record_id"],
            "forge-selected-record:phase5_selection_a_00000001",
        )
        self.assertEqual(
            replacement["snapshot"]["stage"]["source"]["selected_record_id"],
            replacement["forge_selection"]["selected_record_id"],
        )
        expected_errors = {
            "safe_error_selected_record_unavailable": (
                "selected_record_unavailable",
                "The selected World Forge revision is unavailable.",
            ),
            "safe_error_selected_artifact_unavailable": (
                "selected_artifact_unavailable",
                "The selected World Forge artifact is unavailable.",
            ),
            "safe_error_handoff_capacity_exhausted": (
                "handoff_capacity_exhausted",
                "Forge-to-Stage selection capacity is exhausted.",
            ),
            "safe_error_idempotency_capacity_exhausted": (
                "idempotency_capacity_exhausted",
                "Forge-to-Stage replay capacity is exhausted.",
            ),
        }
        for case, (code, message) in expected_errors.items():
            self.assertEqual(fixtures[case]["error"]["code"], code)
            self.assertEqual(fixtures[case]["error"]["message"], message)
            self.assertEqual(fixtures[case]["error"]["retry"], "none")

    def test_stage_source_is_optional_and_static_rc2_fixture_still_validates(
        self,
    ) -> None:
        openapi = TOOL.build_openapi()
        stage_schema = openapi["components"]["schemas"]["StageProjection"]
        self.assertIn("source", stage_schema["properties"])
        self.assertNotIn("source", stage_schema["required"])

        payload = json.loads(
            (
                STAGE_RC2_ROOT / "fixtures/canonical-valid/stage/launch-created.json"
            ).read_text()
        )
        # Only contract identity changes between additive release candidates.
        payload["contract"]["revision"] = TOOL.CONTRACT_REVISION
        payload["preview"]["executor"] = TOOL.LIVE_EXECUTOR
        handoff_schema = {
            **openapi,
            "$ref": "#/components/schemas/ForgeStageCreatedResponse",
        }
        forge_payload = deepcopy(
            next(
                fixture.payload
                for fixture in TOOL.fixture_definitions()
                if fixture.case == "forge_stage_handoff_created"
            )
        )
        del forge_payload["snapshot"]["stage"]["source"]
        # Optional source keeps legacy Stage projections valid as Stage values;
        # the new handoff response itself still requires selected identity.
        stage_value_schema = {
            **openapi,
            "$ref": "#/components/schemas/PresenterStageSnapshot",
        }
        Draft202012Validator(stage_value_schema).validate(payload["snapshot"])
        Draft202012Validator(stage_value_schema).validate(forge_payload["snapshot"])
        self.assertFalse(Draft202012Validator(handoff_schema).is_valid(forge_payload))
        fixtures = {
            fixture.case: fixture.payload for fixture in TOOL.fixture_definitions()
        }
        replacement_without_previous_source = deepcopy(
            fixtures["forge_stage_replacement_success"]
        )
        del replacement_without_previous_source["previous_snapshot"]["stage"]["source"]
        replacement_schema = {
            **openapi,
            "$ref": "#/components/schemas/ForgeStageReplacementResponse",
        }
        self.assertFalse(
            Draft202012Validator(replacement_schema).is_valid(
                replacement_without_previous_source
            )
        )
        self.assertTrue(
            Draft202012Validator(handoff_schema).is_valid(
                fixtures["forge_stage_handoff_created"]
            )
        )

    def test_manifest_pins_exact_published_world_forge_dependency(self) -> None:
        manifest = json.loads((CONTRACT_ROOT / "manifest.json").read_text())
        forge_manifest = json.loads((FORGE_RC4_ROOT / "manifest.json").read_text())
        dependency = manifest["dependencies"]["world_forge"]

        self.assertEqual(dependency["compatibility"], "exact")
        self.assertEqual(dependency["contract_name"], forge_manifest["contract_name"])
        self.assertEqual(
            dependency["contract_revision"], forge_manifest["contract_revision"]
        )
        self.assertEqual(
            dependency["manifest_path"],
            "contracts/world-forge-api/v0.1.0-rc.4/manifest.json",
        )
        expected_digest = hashlib.sha256(
            (REPOSITORY_ROOT / dependency["manifest_path"]).read_bytes()
        ).hexdigest()
        self.assertEqual(
            dependency["manifest_sha256"],
            "0824a9d7f8ea1b1ed264a5f6cd76dd6831054b44932973278b60db1c384baa09",
        )
        self.assertEqual(dependency["manifest_sha256"], expected_digest)
        self.assertTrue(
            all(not state["enabled"] for state in manifest["capabilities"].values())
        )
        self.assertIn(
            manifest["publication_status"], {"release_candidate", "published"}
        )

    def test_generated_client_has_handoff_methods_and_checks_identity(self) -> None:
        client = _load_module(
            "dreamland_forge_stage_generated_client_test",
            CONTRACT_ROOT / "generated" / "product_session_client.py",
        )
        source = (CONTRACT_ROOT / "generated" / "product_session_client.py").read_text()
        fixture = json.loads(
            (
                CONTRACT_ROOT
                / "fixtures/canonical-valid/forge-stage/handoff-created.json"
            ).read_text()
        )

        self.assertIn("def launch_selected_forge_revision(", source)
        self.assertIn("def replace_stage_from_selected_forge_revision(", source)
        self.assertIs(client.validate_forge_stage_handoff(fixture), fixture)
        changed = deepcopy(fixture)
        changed["snapshot"]["stage"]["source"]["immutable_digest"] = (
            "sha256:" + "f" * 64
        )
        with self.assertRaisesRegex(ValueError, "diverged"):
            client.validate_forge_stage_handoff(changed)

        incomplete = deepcopy(fixture)
        for name in ("selected_record_id", "job_id", "reference"):
            incomplete["forge_selection"][name] = None
        source = incomplete["snapshot"]["stage"]["source"]
        source["selected_record_id"] = None
        source["forge_job_id"] = None
        source["forge_reference"] = None
        incomplete["snapshot"]["starting_revision"]["selected_record_id"] = None
        with self.assertRaises(ValueError):
            client.validate_forge_stage_handoff(incomplete)

        wrong_context = deepcopy(fixture)
        wrong_context["contract"]["revision"] = "0.5.0-rc.0"
        with self.assertRaisesRegex(
            ValueError, "incompatible private product contract"
        ):
            client.validate_forge_stage_handoff(wrong_context)

        conflated_hash_domains = deepcopy(fixture)
        artifact_digest = conflated_hash_domains["snapshot"]["stage"]["source"][
            "immutable_digest"
        ]
        conflated_hash_domains["snapshot"]["starting_revision"]["sha256"] = (
            artifact_digest.removeprefix("sha256:")
        )
        with self.assertRaisesRegex(ValueError, "hash domains are conflated"):
            client.validate_forge_stage_handoff(conflated_hash_domains)

        fixtures = {item.case: item.payload for item in TOOL.fixture_definitions()}
        replacement = deepcopy(fixtures["forge_stage_replacement_success"])
        with self.assertRaisesRegex(ValueError, "requires request CAS"):
            client.validate_forge_stage_handoff(replacement)
        self.assertIs(
            client.validate_forge_stage_handoff(
                replacement,
                expected_control_revision=1,
            ),
            replacement,
        )
        del replacement["previous_snapshot"]["stage"]["source"]
        with self.assertRaisesRegex(ValueError, "source identity"):
            client.validate_forge_stage_handoff(
                replacement,
                expected_control_revision=1,
            )

    def test_tool_import_is_runtime_inert(self) -> None:
        code = f"""
import importlib.util
import json
import sys
import threading
from pathlib import Path
before = tuple(thread.name for thread in threading.enumerate())
spec = importlib.util.spec_from_file_location("forge_stage_contract_inert", Path({str(TOOL_PATH)!r}))
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
after = tuple(thread.name for thread in threading.enumerate())
runtime_modules = sorted(name for name in sys.modules if name.startswith("dreamland.product"))
print(json.dumps({{"before": before, "after": after, "runtime_modules": runtime_modules}}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["before"], payload["after"])
        self.assertEqual(payload["runtime_modules"], [])


if __name__ == "__main__":
    unittest.main()
