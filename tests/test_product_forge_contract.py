from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

CONTRACT_ROOT = Path("contracts/world-forge-api/v0.1.0-rc.4")
LEGACY_RC2_ROOT = Path("contracts/world-forge-api/v0.1.0-rc.2")


def _load_tool() -> ModuleType:
    path = Path("tools/product_forge_contract.py")
    specification = importlib.util.spec_from_file_location(
        "test_world_forge_contract_tool", path
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("World Forge contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


class ProductForgeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _load_tool()

    def test_published_tree_validates_and_client_smokes(self) -> None:
        validated = self.tool.validate_contract(CONTRACT_ROOT)
        smoked = self.tool.client_smoke(CONTRACT_ROOT)
        self.assertTrue(validated["valid"])
        self.assertEqual(validated["fixture_count"], 41)
        self.assertTrue(smoked["smoke"])
        self.assertEqual(smoked["operations"], 7)

    def test_refresh_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "contract"
            self.tool.refresh_contract(root)
            expected = {
                path.relative_to(CONTRACT_ROOT).as_posix(): path.read_bytes()
                for path in CONTRACT_ROOT.rglob("*")
                if path.is_file()
            }
            actual = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            expected_manifest = json.loads(expected["manifest.json"])
            actual_manifest = json.loads(actual["manifest.json"])
            for name in ("publication_status", "backend_commit", "published_at"):
                expected_manifest[name] = actual_manifest[name]
            expected["manifest.json"] = (
                json.dumps(expected_manifest, indent=2, sort_keys=True) + "\n"
            ).encode()
            self.assertEqual(actual, expected)

    def test_contract_is_separate_and_contains_no_downstream_authority(self) -> None:
        openapi = json.loads((CONTRACT_ROOT / "openapi.json").read_text())
        self.assertEqual(
            openapi["x-dreamland-world-forge"]["contract_name"],
            "dreamland-private-world-forge-api",
        )
        self.assertFalse(openapi["x-dreamland-world-forge"]["stage_authority"])
        self.assertFalse(openapi["x-dreamland-world-forge"]["publication_authority"])
        encoded = json.dumps(openapi)
        for forbidden in (
            "launch_grant",
            "stage_session",
            "participant",
            "published_world",
            "canary_passed",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_published_rc2_remains_valid_and_distinct(self) -> None:
        validated = self.tool.validate_contract(LEGACY_RC2_ROOT)
        self.assertEqual(validated["contract_revision"], "0.1.0-rc.2")
        self.assertEqual(validated["fixture_count"], 39)
        self.assertEqual(
            json.loads((LEGACY_RC2_ROOT / "manifest.json").read_text())[
                "backend_commit"
            ],
            "640075890c638e4c2c3dc99c906f0bc72300b472",
        )

    def test_every_lifecycle_and_safe_error_has_a_fixture(self) -> None:
        index = json.loads((CONTRACT_ROOT / "fixtures/index.json").read_text())
        cases = {entry["case"] for entry in index["fixtures"]}
        for state in self.tool.LIFECYCLE_STATES:
            self.assertIn(f"projection_{state}", cases)
        for code in self.tool.ERROR_CODES:
            self.assertIn(f"error_{code}", cases)
        self.assertIn("projection_unknown_additive", cases)
        self.assertIn("projection_unknown_enum", cases)
        self.assertIn("projection_selected_live_vertical_slice", cases)
        self.assertIn("cancel_result", cases)

    def test_safe_error_reachability_is_explicit(self) -> None:
        index = json.loads((CONTRACT_ROOT / "fixtures/index.json").read_text())
        entries = {
            entry["case"].removeprefix("error_"): entry
            for entry in index["fixtures"]
            if entry["classification"] == "safe-error"
        }
        self.assertEqual(set(entries), set(self.tool.ERROR_CODES))
        for code, reachability in self.tool.ERROR_REACHABILITY.items():
            self.assertEqual(entries[code]["reachability"], reachability)
            self.assertEqual(
                entries[code]["reachability_detail"],
                self.tool.ERROR_REACHABILITY_DETAILS[code],
            )

    def test_every_runtime_service_error_is_in_the_contract_or_internal(self) -> None:
        tree = ast.parse(Path("src/dreamland/product/forge.py").read_text())
        runtime_codes = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ForgeServiceError"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        self.assertEqual(
            runtime_codes - set(self.tool.ERROR_CODES), {"transition_invalid"}
        )

    def test_projection_and_recipe_semantic_relationships_are_enforced(self) -> None:
        import jsonschema

        openapi = json.loads((CONTRACT_ROOT / "openapi.json").read_text())
        projection_schema = {**openapi, "$ref": "#/components/schemas/BuildProjection"}
        recipe_field_schema = {**openapi, "$ref": "#/components/schemas/RecipeField"}
        accepted = json.loads(
            (
                CONTRACT_ROOT / "fixtures/canonical-valid/lifecycle/accepted.json"
            ).read_text()
        )
        selected = json.loads(
            (
                CONTRACT_ROOT / "fixtures/canonical-valid/lifecycle/selected.json"
            ).read_text()
        )
        recipes = json.loads(
            (CONTRACT_ROOT / "fixtures/canonical-valid/recipes.json").read_text()
        )

        invalid_values = []
        accepted_with_receipt = json.loads(json.dumps(accepted))
        accepted_with_receipt["receipt"] = selected["receipt"]
        invalid_values.append(accepted_with_receipt)
        selected_without_receipt = json.loads(json.dumps(selected))
        selected_without_receipt["receipt"] = None
        invalid_values.append(selected_without_receipt)
        accepted_terminal = json.loads(json.dumps(accepted))
        accepted_terminal["terminal"] = True
        invalid_values.append(accepted_terminal)
        accepted_uncancellable = json.loads(json.dumps(accepted))
        accepted_uncancellable["cancellation"] = {
            "state": "unavailable",
            "safe_reason": "wrong",
        }
        invalid_values.append(accepted_uncancellable)
        failed_without_message = json.loads(
            (
                CONTRACT_ROOT / "fixtures/canonical-valid/lifecycle/failed.json"
            ).read_text()
        )
        del failed_without_message["lifecycle"]["safe_message"]
        invalid_values.append(failed_without_message)
        wrong_provenance = json.loads(json.dumps(selected))
        wrong_provenance["receipt"]["provenance"]["label"] = "different"
        invalid_values.append(wrong_provenance)
        unknown_provenance = json.loads(json.dumps(selected))
        unknown_provenance["source"]["kind"] = "future_source"
        invalid_values.append(unknown_provenance)
        for value in invalid_values:
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(projection_schema).validate(value)

        additive = json.loads(
            (
                CONTRACT_ROOT
                / "fixtures/forward-compatibility/projection-unknown-additive.json"
            ).read_text()
        )
        jsonschema.Draft202012Validator(projection_schema).validate(additive)
        unknown_enum = json.loads(
            (
                CONTRACT_ROOT / "fixtures/invalid/projection-unknown-enum.json"
            ).read_text()
        )
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(projection_schema).validate(unknown_enum)

        short_text = recipes["recipes"][0]["fields"][0]
        integer = recipes["recipes"][0]["fields"][1]
        jsonschema.Draft202012Validator(recipe_field_schema).validate(short_text)
        jsonschema.Draft202012Validator(recipe_field_schema).validate(integer)
        invalid_short_text = {**short_text, "minimum": 1}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(recipe_field_schema).validate(
                invalid_short_text
            )

    def test_cli_validate_and_client_smoke(self) -> None:
        for command in ("validate", "client-smoke"):
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/product_forge_contract.py",
                    command,
                    str(CONTRACT_ROOT),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["contract_revision"], "0.1.0-rc.4")


if __name__ == "__main__":
    unittest.main()
