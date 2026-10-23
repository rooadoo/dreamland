from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_contract.py"
RC2_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.7.0-rc.2"
RC2_TREE = (
    160,
    "14b2c531c611c4212c37d7cb30948dff2db6dd69769e48fe9985e18a6da6d406",
)


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_contract_test_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("attendee contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load_tool()


class ProductAttendeeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.contract_root = Path(cls._temporary.name) / "v0.7.0-rc.3"
        TOOL.refresh_contract(cls.contract_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_release_candidate_validates_and_generated_client_smokes(self) -> None:
        validated = TOOL.validate_contract(self.contract_root)
        smoked = TOOL.client_smoke(self.contract_root)

        self.assertEqual(validated["contract_revision"], "0.7.0-rc.3")
        self.assertEqual(validated["routes"], 24)
        self.assertEqual(validated["fixtures"], 181)
        self.assertEqual(validated["canonical_valid"], 130)
        self.assertEqual(validated["forward_compat"], 7)
        self.assertEqual(validated["invalid"], 44)
        self.assertEqual(validated["safe_error_codes"], 45)
        self.assertEqual(validated["capabilities_enabled"], 0)
        self.assertEqual(smoked["validated_client_fixtures"], 22)
        self.assertEqual(smoked["rejected_client_mutations"], 144)

    def test_rc3_preserves_every_published_rc2_path_exactly(self) -> None:
        rc2 = _json(RC2_ROOT / "openapi.json")
        rc3 = _json(self.contract_root / "openapi.json")

        self.assertEqual(len(rc2["paths"]), 21)
        self.assertEqual(len(rc3["paths"]), 24)
        for path, path_item in rc2["paths"].items():
            self.assertIn(path, rc3["paths"])
            self.assertEqual(rc3["paths"][path], path_item, path)
        self.assertEqual(
            set(rc3["paths"]) - set(rc2["paths"]),
            {
                "/product/v1/attendee/contexts",
                "/product/v1/attendee/launch-options",
                "/product/v1/attendee/launch-selections",
            },
        )

    def test_bootstrap_mints_actor_context_without_accepting_one(self) -> None:
        openapi = _json(self.contract_root / "openapi.json")
        operation = openapi["paths"]["/product/v1/attendee/contexts"]["post"]
        parameters = {
            value["$ref"].rsplit("/", 1)[-1] for value in operation["parameters"]
        }

        self.assertEqual(operation["operationId"], "bootstrapAttendeeContext")
        self.assertEqual(parameters, {"ExpectedEpoch", "IdempotencyKey"})
        self.assertNotIn("ActorContext", parameters)
        self.assertIn("201", operation["responses"])
        self.assertNotIn("200", operation["responses"])
        self.assertEqual(
            operation["requestBody"]["content"]["application/json"]["schema"],
            {"$ref": ("#/components/schemas/AttendeeContextBootstrapRequest")},
        )

        schemas = openapi["components"]["schemas"]
        request = schemas["AttendeeContextBootstrapRequest"]
        response = schemas["AttendeeContextBootstrapResponse"]
        self.assertFalse(request["additionalProperties"])
        self.assertEqual(request["required"], ["gateway_context_id"])
        self.assertEqual(
            request["properties"],
            {"gateway_context_id": {"$ref": "#/components/schemas/GatewayContextId"}},
        )
        self.assertEqual(
            schemas["GatewayContextId"]["pattern"],
            r"^gateway-context:[A-Za-z0-9_-]{24,128}$",
        )
        self.assertIn("actor_context", response["required"])
        self.assertNotIn("gateway_context_id", response["properties"])
        self.assertEqual(
            response["properties"]["actor_context"],
            {"$ref": "#/components/schemas/ActorContext"},
        )
        self.assertEqual(
            response["properties"]["authority_class"],
            {"const": "attendee", "type": "string"},
        )

    def test_bootstrap_claim_precedes_context_lookup_and_capacity(
        self,
    ) -> None:
        openapi = _json(self.contract_root / "openapi.json")
        policy = openapi["paths"]["/product/v1/attendee/contexts"]["post"][
            "x-dreamland-idempotency"
        ]

        self.assertEqual(policy["operation_family"], "attendee.context.bootstrap")
        self.assertEqual(
            policy["scope"],
            [
                "service_epoch",
                "private_service_principal",
                "operation_family",
                "idempotency_key",
            ],
        )
        self.assertEqual(policy["same_key_same_body"], "replay_original_result")
        self.assertEqual(policy["same_key_changed_body"], "idempotency_conflict")
        self.assertFalse(policy["automatic_new_key_redispatch"])
        self.assertEqual(
            policy["validation_order"],
            [
                "current_service_epoch",
                "private_bff_authority",
                "idempotency_key_and_canonical_body",
                "exact_replay_or_claim",
                "gateway_context_lookup",
                "context_capacity",
            ],
        )
        self.assertIn(
            "attendee_context_capacity_exhausted",
            policy["claimed_deterministic_rejections"],
        )
        self.assertIn(
            "attendee_context_idempotency_capacity_exhausted",
            policy["unclaimed_rejections"],
        )

        capacity = TOOL.SAFE_ERROR_CASES["attendee_context_capacity_exhausted"]
        self.assertEqual(
            capacity,
            (
                503,
                "Attendee context capacity is exhausted for this service epoch.",
                "none",
                None,
            ),
        )
        fixture = _json(
            self.contract_root
            / "fixtures/canonical-valid/errors/attendee-context-capacity-exhausted.json"
        )
        self.assertEqual(fixture["error"]["retry"], "none")
        self.assertIsNone(fixture["error"]["retry_after_ms"])
        replay_capacity = _json(
            self.contract_root
            / "fixtures/canonical-valid/errors/attendee-context-idempotency-capacity-exhausted.json"
        )
        self.assertEqual(replay_capacity["error"]["retry"], "after_delay")
        self.assertEqual(replay_capacity["error"]["retry_after_ms"], 1_000)

    def test_attendee_options_are_authored_only_and_non_authorizing(self) -> None:
        openapi = _json(self.contract_root / "openapi.json")
        operation = openapi["paths"]["/product/v1/attendee/launch-options"]["get"]
        schemas = openapi["components"]["schemas"]
        option = schemas["AttendeeLaunchOption"]

        self.assertEqual(operation["operationId"], "getAttendeeLaunchOptions")
        self.assertEqual(
            {value["$ref"].rsplit("/", 1)[-1] for value in operation["parameters"]},
            {"ExpectedEpoch", "ActorContext"},
        )
        self.assertEqual(
            schemas["AttendeeLaunchOrigin"]["properties"]["kind"]["const"],
            "authored_profile_snapshot",
        )
        self.assertFalse(option["additionalProperties"])
        self.assertIn("notice_version", option["required"])
        for forbidden in (
            "actor_context",
            "gateway_context_id",
            "launch_grant",
            "profile_name",
            "profile_path",
            "runtime_document",
            "session_authority",
            "world_state",
        ):
            self.assertNotIn(forbidden, option["properties"])

        authored = _json(
            self.contract_root
            / "fixtures/canonical-valid/attendee/options-authored.json"
        )
        self.assertEqual(len(authored["options"]), 1)
        self.assertEqual(
            authored["options"][0]["origin"],
            {"kind": "authored_profile_snapshot"},
        )

    def test_exchange_binds_selection_and_exact_notice_in_the_body(self) -> None:
        openapi = _json(self.contract_root / "openapi.json")
        operation = openapi["paths"]["/product/v1/attendee/launch-selections"]["post"]
        request = openapi["components"]["schemas"]["AttendeeLaunchSelectionRequest"]
        response = openapi["components"]["schemas"]["AttendeeLaunchSelectionResponse"]
        policy = operation["x-dreamland-idempotency"]

        self.assertEqual(operation["operationId"], "exchangeAttendeeLaunchSelection")
        self.assertEqual(
            {value["$ref"].rsplit("/", 1)[-1] for value in operation["parameters"]},
            {"ExpectedEpoch", "ActorContext", "IdempotencyKey"},
        )
        self.assertFalse(request["additionalProperties"])
        self.assertEqual(
            set(request["required"]),
            {"launch_selection_id", "notice_version_presented"},
        )
        self.assertNotIn("profile_name", request["properties"])
        self.assertNotIn("launch_grant", request["properties"])
        self.assertEqual(
            response["properties"]["launch_grant"],
            {"$ref": "#/components/schemas/LaunchSelectionGrant"},
        )
        self.assertEqual(
            policy["semantic_request_evidence"]["body_fields"],
            ["launch_selection_id", "notice_version_presented"],
        )
        self.assertEqual(policy["same_key_same_body"], "replay_original_result")
        self.assertEqual(policy["same_key_changed_body"], "idempotency_conflict")
        self.assertEqual(
            set(policy["claimed_deterministic_rejections"]),
            {
                "launch_selection_unavailable",
                "notice_version_mismatch",
                "privacy_notice_unavailable",
                "service_closed",
            },
        )
        self.assertEqual(
            set(policy["unclaimed_retryable_rejections"]),
            {"service_not_open", "launch_selection_capacity_exhausted"},
        )
        self.assertEqual(
            policy["internal_registry_key"],
            {
                "derivation": (
                    "outer_epoch_actor_operation_key_and_canonical_body_sha256"
                ),
                "notice_changes_cannot_alias": True,
                "outer_retention_boundary_preserved": True,
                "selection_only_key_reused": False,
            },
        )

    def test_existing_session_routes_remain_the_only_attendee_runtime_routes(
        self,
    ) -> None:
        openapi = _json(self.contract_root / "openapi.json")
        attendee = openapi["x-dreamland-attendee-integration-preview"]

        self.assertEqual(attendee["session_routes"], "existing_product_v1_sessions")
        self.assertTrue(attendee["authored_only"])
        self.assertFalse(attendee["browser_credentials_accepted"])
        self.assertFalse(attendee["caller_supplied_actor_or_grant_accepted"])
        self.assertFalse(attendee["generated_world_attendee_eligibility"])
        self.assertFalse(attendee["normal_runtime_composition"])
        self.assertFalse(attendee["provider_dispatch"])
        self.assertEqual(
            sorted(
                path
                for path in openapi["paths"]
                if path.startswith("/product/v1/sessions")
            ),
            sorted(
                path
                for path in _json(RC2_ROOT / "openapi.json")["paths"]
                if path.startswith("/product/v1/sessions")
            ),
        )

    def test_identity_is_provider_free_and_all_capabilities_stay_disabled(
        self,
    ) -> None:
        openapi = _json(self.contract_root / "openapi.json")
        manifest = _json(self.contract_root / "manifest.json")
        preview = openapi["components"]["schemas"]["PreviewIdentity"]["properties"]

        self.assertEqual(preview["executor"]["const"], TOOL.PREVIEW_EXECUTOR)
        self.assertFalse(preview["provider_calls"]["const"])
        self.assertTrue(preview["test_only"]["const"])
        self.assertFalse(preview["live_capability_evidence"]["const"])
        self.assertEqual(
            openapi["x-dreamland-development-live"],
            {
                "capability_evidence": False,
                "explicit_start_only": True,
                "loopback_only": True,
                "normal_runtime_composition": False,
                "provider_calls": False,
                "usage_accounting": "provider_free_deterministic",
            },
        )
        self.assertEqual(manifest["contract_revision"], "0.7.0-rc.3")
        self.assertEqual(manifest["publication_status"], "release_candidate")
        self.assertIsNone(manifest["backend_commit"])
        self.assertTrue(
            all(
                state["enabled"] is False and state["enabled_revision"] is None
                for state in manifest["capabilities"].values()
            )
        )
        self.assertEqual(
            manifest["generation"]["refresh_command"],
            (
                ".venv/bin/python tools/product_attendee_contract.py refresh "
                "contracts/product-api/v0.7.0-rc.3"
            ),
        )

    def test_generated_client_exposes_private_attendee_methods(self) -> None:
        source = (
            self.contract_root / "generated" / "product_session_client.py"
        ).read_text(encoding="utf-8")
        generated = _load_generated_client(self.contract_root)
        context = _json(
            self.contract_root / "fixtures/canonical-valid/attendee/context-fresh.json"
        )
        options = _json(
            self.contract_root
            / "fixtures/canonical-valid/attendee/options-authored.json"
        )
        selection = _json(
            self.contract_root
            / "fixtures/canonical-valid/attendee/selection-fresh.json"
        )

        self.assertIn("Generated by tools/product_attendee_contract.py", source)
        self.assertIn("def bootstrap_attendee_context", source)
        self.assertIn("def attendee_launch_options", source)
        self.assertIn("def exchange_attendee_launch_selection", source)
        self.assertIn('"provider_calls": False', source)
        self.assertNotIn('"provider_calls": True', source)
        self.assertIs(
            generated.validate_attendee_context_bootstrap(context),
            context,
        )
        self.assertIs(generated.validate_attendee_launch_options(options), options)
        self.assertIs(
            generated.validate_attendee_launch_selection(
                selection,
                expected_launch_selection_id=(
                    selection["selection"]["launch_selection_id"]
                ),
                expected_notice_version=selection["selection"]["notice_version"],
            ),
            selection,
        )

    def test_fixture_inventory_covers_new_operations_and_failures(self) -> None:
        index = _json(self.contract_root / "fixtures" / "index.json")
        operations = set(index["coverage"]["operations"])
        errors = set(index["coverage"]["safe_error_codes"])
        cases = {value["case"] for value in index["fixtures"]}

        self.assertTrue(
            {
                "bootstrapAttendeeContext",
                "getAttendeeLaunchOptions",
                "exchangeAttendeeLaunchSelection",
            }.issubset(operations)
        )
        self.assertTrue(set(TOOL.ATTENDEE_SAFE_ERROR_CASES).issubset(errors))
        self.assertTrue(
            {
                "attendee_context_bootstrap_response_lost",
                "attendee_launch_selection_response_lost",
                "attendee_forge_origin",
                "attendee_option_profile_path",
                "attendee_selection_consent_claim",
            }.issubset(cases)
        )

    def test_refresh_is_deterministic_and_rc2_remains_immutable(self) -> None:
        self.assertEqual(_tree_identity(RC2_ROOT), RC2_TREE)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "v0.7.0-rc.3"
            TOOL.refresh_contract(target)
            self.assertEqual(
                _tree_identity(target),
                _tree_identity(self.contract_root),
            )
        self.assertEqual(_tree_identity(RC2_ROOT), RC2_TREE)

    def test_tool_import_is_runtime_inert(self) -> None:
        code = f"""
import importlib.util
import pathlib
import threading
before = [(thread.ident, thread.name) for thread in threading.enumerate()]
path = pathlib.Path({str(TOOL_PATH)!r})
spec = importlib.util.spec_from_file_location("attendee_contract_inert", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
after = [(thread.ident, thread.name) for thread in threading.enumerate()]
assert after == before
assert module.CONTRACT_REVISION == "0.7.0-rc.3"
assert len(module.EXPECTED_OPERATIONS) == 24
"""
        subprocess.run([sys.executable, "-c", code], check=True)


def _load_generated_client(root: Path):
    path = root / "generated" / "product_session_client.py"
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_generated_test_client",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("generated attendee client is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not an object")
    return value


def _tree_identity(root: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    files = tuple(
        sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return len(files), digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
