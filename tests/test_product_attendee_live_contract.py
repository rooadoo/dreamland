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

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_live_contract.py"
RC3_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.7.0-rc.3"
RC3_TREE = (
    186,
    "d00465b400584b86e55263fe8aebe519f475c181bbb683725c7b13568ce860c9",
)


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_live_contract_test_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("live attendee contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load_tool()


class ProductAttendeeLiveContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.contract_root = Path(cls._temporary.name) / "v0.7.0-rc.4"
        TOOL.refresh_contract(cls.contract_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_release_candidate_validates_and_generated_client_smokes(self) -> None:
        validated = TOOL.validate_contract(self.contract_root)
        smoked = TOOL.client_smoke(self.contract_root)

        self.assertEqual(validated["contract_revision"], "0.7.0-rc.4")
        self.assertEqual(validated["routes"], 24)
        self.assertEqual(validated["fixtures"], 181)
        self.assertEqual(validated["canonical_valid"], 130)
        self.assertEqual(validated["forward_compat"], 7)
        self.assertEqual(validated["invalid"], 44)
        self.assertEqual(validated["safe_error_codes"], 45)
        self.assertEqual(validated["capabilities_enabled"], 0)
        self.assertEqual(smoked["validated_client_fixtures"], 22)
        self.assertEqual(smoked["rejected_client_mutations"], 144)

    def test_rc4_changes_only_revision_and_live_identity_metadata(self) -> None:
        rc3 = _json(RC3_ROOT / "openapi.json")
        rc4 = _json(self.contract_root / "openapi.json")
        normalized = _replace_string(deepcopy(rc3), "0.7.0-rc.3", "0.7.0-rc.4")
        normalized["info"] = deepcopy(rc4["info"])
        preview = normalized["components"]["schemas"]["PreviewIdentity"]["properties"]
        preview["executor"]["const"] = TOOL.LIVE_EXECUTOR
        preview["provider_calls"]["const"] = True
        normalized["x-dreamland-development-live"] = deepcopy(
            rc4["x-dreamland-development-live"]
        )

        self.assertEqual(rc4, normalized)
        self.assertEqual(rc4["paths"], normalized["paths"])
        self.assertEqual(rc4["paths"], rc3["paths"])
        self.assertEqual(len(rc4["paths"]), 24)

    def test_identity_is_live_but_attendee_facade_remains_provider_free(
        self,
    ) -> None:
        openapi = _json(self.contract_root / "openapi.json")
        manifest = _json(self.contract_root / "manifest.json")
        preview = openapi["components"]["schemas"]["PreviewIdentity"]["properties"]

        self.assertEqual(preview["executor"]["const"], TOOL.LIVE_EXECUTOR)
        self.assertTrue(preview["provider_calls"]["const"])
        self.assertTrue(preview["test_only"]["const"])
        self.assertTrue(preview["integration_only"]["const"])
        self.assertFalse(preview["live_capability_evidence"]["const"])
        self.assertEqual(
            openapi["x-dreamland-development-live"],
            {
                "capability_evidence": False,
                "explicit_start_only": True,
                "loopback_only": True,
                "normal_runtime_composition": False,
                "provider_calls": True,
                "usage_accounting": "full_reservation_conservative",
            },
        )
        attendee = openapi["x-dreamland-attendee-integration-preview"]
        self.assertFalse(attendee["provider_dispatch"])
        self.assertTrue(attendee["authored_only"])
        self.assertFalse(attendee["generated_world_attendee_eligibility"])
        self.assertFalse(attendee["normal_runtime_composition"])
        self.assertFalse(attendee["capability_evidence"])
        self.assertTrue(
            all(
                not capability["enabled"]
                for capability in manifest["capabilities"].values()
            )
        )

    def test_manifest_dispatch_and_commands_are_owned_by_live_generator(self) -> None:
        manifest = _json(self.contract_root / "manifest.json")
        prefix = (
            ".venv/bin/python tools/product_attendee_live_contract.py"
            " {verb} contracts/product-api/v0.7.0-rc.4"
        )
        self.assertEqual(
            manifest["generation"],
            {
                "client_smoke_command": prefix.format(verb="client-smoke"),
                "publish_command": (
                    prefix.format(verb="publish")
                    + " --backend-commit COMMIT --published-at TIMESTAMP"
                ),
                "refresh_command": prefix.format(verb="refresh"),
                "validate_command": prefix.format(verb="validate"),
            },
        )
        for module in TOOL._MODULES:
            self.assertIs(module._base_manifest, TOOL._base_manifest)
        # The captured attendee builder must retain its predecessor dispatch:
        # pointing it back at the live wrapper would recurse through
        # _attendee_base_manifest() -> _base_manifest().
        self.assertIsNot(TOOL._ATTENDEE._base_manifest, TOOL._base_manifest)

    def test_every_fixture_changes_only_contract_and_preview_identity(self) -> None:
        rc3_index = _json(RC3_ROOT / "fixtures" / "index.json")
        rc4_index = _json(self.contract_root / "fixtures" / "index.json")
        rc3_by_case = {fixture["case"]: fixture for fixture in rc3_index["fixtures"]}
        rc4_by_case = {fixture["case"]: fixture for fixture in rc4_index["fixtures"]}
        self.assertEqual(set(rc3_by_case), set(rc4_by_case))

        for case, rc3_fixture in rc3_by_case.items():
            rc4_fixture = rc4_by_case[case]
            self.assertEqual(rc3_fixture["path"], rc4_fixture["path"])
            rc3_payload = _json_value(RC3_ROOT / rc3_fixture["path"])
            rc4_payload = _json_value(self.contract_root / rc4_fixture["path"])
            self.assertEqual(rc4_payload, _live_identity(rc3_payload), case)

    def test_generated_client_pins_only_the_live_context(self) -> None:
        source = (
            self.contract_root / "generated" / "product_session_client.py"
        ).read_text(encoding="utf-8")
        generated = _load_generated_client(self.contract_root)
        live_options = _json(
            self.contract_root
            / "fixtures/canonical-valid/attendee/options-authored.json"
        )
        deterministic_options = _json(
            RC3_ROOT / "fixtures/canonical-valid/attendee/options-authored.json"
        )

        self.assertIn("Generated by tools/product_attendee_live_contract.py", source)
        self.assertIn(TOOL.LIVE_EXECUTOR, source)
        self.assertIn('"provider_calls": True', source)
        self.assertNotIn("deterministic-attendee-no-provider-v1", source)
        self.assertIs(
            generated.validate_attendee_launch_options(live_options),
            live_options,
        )
        with self.assertRaises(ValueError):
            generated.validate_attendee_launch_options(deterministic_options)

    def test_refresh_is_deterministic_and_preserves_published_rc3(self) -> None:
        self.assertEqual(_tree_identity(RC3_ROOT), RC3_TREE)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "v0.7.0-rc.4"
            TOOL.refresh_contract(target)
            self.assertEqual(
                _tree_identity(target),
                _tree_identity(self.contract_root),
            )
        self.assertEqual(_tree_identity(RC3_ROOT), RC3_TREE)

    def test_tool_import_is_runtime_inert(self) -> None:
        code = f"""
import importlib.util
import pathlib
import threading
before = [(thread.ident, thread.name) for thread in threading.enumerate()]
path = pathlib.Path({str(TOOL_PATH)!r})
spec = importlib.util.spec_from_file_location("attendee_live_contract_inert", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
after = [(thread.ident, thread.name) for thread in threading.enumerate()]
assert after == before
assert module.CONTRACT_REVISION == "0.7.0-rc.4"
assert len(module.EXPECTED_OPERATIONS) == 24
"""
        subprocess.run([sys.executable, "-c", code], check=True)


def _load_generated_client(root: Path):
    path = root / "generated" / "product_session_client.py"
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_live_generated_test_client",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("generated live attendee client is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _json(path: Path) -> dict:
    value = _json_value(path)
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not an object")
    return value


def _json_value(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _replace_string(value, old: str, new: str):
    if isinstance(value, dict):
        return {key: _replace_string(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_string(item, old, new) for item in value]
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def _live_identity(value):
    copied = _replace_string(deepcopy(value), "0.7.0-rc.3", "0.7.0-rc.4")

    def replace(item) -> None:
        if isinstance(item, dict):
            preview = item.get("preview")
            if isinstance(preview, dict) and {
                "test_only",
                "integration_only",
                "executor",
                "provider_calls",
                "live_capability_evidence",
            }.issubset(preview):
                preview["executor"] = TOOL.LIVE_EXECUTOR
                preview["provider_calls"] = True
            for nested in item.values():
                replace(nested)
        elif isinstance(item, list):
            for nested in item:
                replace(nested)

    replace(copied)
    return copied


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
