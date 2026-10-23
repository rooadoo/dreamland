from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_world_access_live_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts/world-access-api/v0.1.0-rc.4"
RC3_ROOT = REPOSITORY_ROOT / "contracts/world-access-api/v0.1.0-rc.3"
PRODUCT_RC4_ROOT = REPOSITORY_ROOT / "contracts/product-api/v0.7.0-rc.4"
FORGE_RC4_ROOT = REPOSITORY_ROOT / "contracts/world-forge-api/v0.1.0-rc.4"
RC3_TREE = (
    76,
    "364dfefc5a2054779b35389f476abeaf03d9bebb730cd9746f2bed7ae7577859",
)
RC3_MANIFEST_SHA256 = "b1d27b59a8701933c37dbe28481fee777d1abf63523ac60ba25276dfed025a23"


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_world_access_live_contract_test_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("live World Access contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load_tool()


class ProductWorldAccessLiveContractTests(unittest.TestCase):
    def test_checked_rc4_validates_and_client_smokes(self) -> None:
        validated = TOOL.validate_contract(CONTRACT_ROOT)
        self.assertEqual(validated["contract_revision"], "0.1.0-rc.4")
        self.assertEqual(validated["fixture_count"], 71)
        self.assertTrue(validated["valid"])
        if shutil.which("node") is None:
            self.skipTest("Node.js is not installed for the generated-client smoke")
        smoked = TOOL.client_smoke(CONTRACT_ROOT)
        self.assertTrue(smoked["smoke"])
        self.assertTrue(smoked["compiled"])
        self.assertTrue(smoked["live_identity_consumed"])
        self.assertTrue(smoked["launch_binding_sha256_consumed"])
        self.assertEqual(smoked["operations"], 6)
        self.assertEqual(smoked["typescript"], "6.0.3")

    def test_rc4_changes_only_revision_dependency_and_live_identity(self) -> None:
        rc3 = _json(RC3_ROOT / "openapi.json")
        rc4 = _json(CONTRACT_ROOT / "openapi.json")
        expected = _replace_strings(
            deepcopy(rc3),
            {
                "0.1.0-rc.3": "0.1.0-rc.4",
                "0.7.0-rc.3": "0.7.0-rc.4",
            },
        )
        expected["info"] = deepcopy(rc4["info"])
        _replace_preview_schemas(expected)
        boundary = expected["x-dreamland-boundary"]
        boundary["provider_calls"] = True
        boundary["live_forge_evidence"] = True
        boundary["live_attendee_execution"] = True
        expected["x-dreamland-development-live"] = deepcopy(
            rc4["x-dreamland-development-live"]
        )

        self.assertEqual(rc4, expected)
        self.assertEqual(rc4["paths"], rc3["paths"])
        self.assertEqual(set(rc4["paths"]), TOOL.EXPECTED_PATHS)
        self.assertEqual(len(TOOL.EXPECTED_OPERATIONS), 6)

    def test_live_identity_is_truthful_but_capabilities_stay_disabled(self) -> None:
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        preview = openapi["components"]["schemas"]["PreviewIdentity"]
        properties = preview["properties"]
        self.assertEqual(properties["kind"]["const"], TOOL.LIVE_KIND)
        self.assertTrue(properties["provider_calls"]["const"])
        self.assertTrue(properties["live_forge_evidence"]["const"])
        self.assertTrue(properties["live_attendee_execution"]["const"])
        self.assertFalse(properties["live_capability_evidence"]["const"])
        self.assertTrue(properties["loopback_only"]["const"])
        self.assertTrue(properties["process_local"]["const"])
        self.assertEqual(properties["capabilities_enabled"]["maxItems"], 0)
        self.assertEqual(
            openapi["x-dreamland-development-live"],
            {
                "capability_evidence": False,
                "explicit_start_only": True,
                "live_attendee_execution": True,
                "live_forge_evidence": True,
                "loopback_only": True,
                "normal_runtime_composition": False,
                "production_capabilities_enabled": [],
                "provider_calls": True,
            },
        )
        self.assertFalse(openapi["x-dreamland-boundary"]["direct_browser_access"])
        self.assertTrue(
            openapi["x-dreamland-boundary"]["launch_binding_sha256_server_only"]
        )
        self.assertFalse(
            openapi["x-dreamland-boundary"]["launch_binding_sha256_browser_exposed"]
        )

    def test_dependencies_pin_published_live_siblings_exactly(self) -> None:
        manifest = _json(CONTRACT_ROOT / "manifest.json")
        self.assertEqual(
            manifest["dependencies"],
            {
                "product_api": {
                    "contract": "dreamland-private-product-api",
                    "revision": "0.7.0-rc.4",
                    "manifest_sha256": _sha256(PRODUCT_RC4_ROOT / "manifest.json"),
                },
                "world_forge_api": {
                    "contract": "dreamland-private-world-forge-api",
                    "revision": "0.1.0-rc.4",
                    "manifest_sha256": _sha256(FORGE_RC4_ROOT / "manifest.json"),
                },
            },
        )
        self.assertEqual(
            manifest["implementation_status"],
            "provider_backed_live_development_only",
        )
        self.assertEqual(
            manifest["source"],
            {
                "kind": TOOL.LIVE_KIND,
                "provider_calls": True,
                "live_forge_evidence": True,
                "live_attendee_execution": True,
                "live_capability_evidence": False,
                "normal_runtime_integration": False,
                "production_capabilities_enabled": [],
            },
        )
        self.assertIn(manifest["publication_status"], {"draft", "published"})
        if manifest["publication_status"] == "draft":
            self.assertIsNone(manifest["backend_commit"])
            self.assertIsNone(manifest["published_at"])
        else:
            self.assertRegex(manifest["backend_commit"], r"^[0-9a-f]{40}$")
            self.assertIsInstance(manifest["published_at"], str)

    def test_manifest_and_publication_dispatch_are_live_owned(self) -> None:
        manifest = _json(CONTRACT_ROOT / "manifest.json")
        generation = manifest["generation"]
        for command in (
            "refresh_command",
            "validate_command",
            "client_smoke_command",
            "publish_command",
        ):
            self.assertIn(
                "tools/product_world_access_live_contract.py", generation[command]
            )
            self.assertIn("v0.1.0-rc.4", generation[command])
        toolchain = generation["client_toolchain"]
        self.assertEqual(
            toolchain["harness"],
            "tools/world_access_live_client_smoke/run-smoke.mjs",
        )
        self.assertEqual(
            toolchain["harness_sha256"],
            _sha256(REPOSITORY_ROOT / toolchain["harness"]),
        )
        self.assertTrue(
            {
                "tools/product_world_access_live.py",
                "tools/product_world_access_live_contract.py",
                "tools/product_attendee_live.py",
                "tools/world_access_live_client_smoke/run-smoke.mjs",
            }.issubset(TOOL.PUBLICATION_IMPLEMENTATION_FILES)
        )

    def test_every_fixture_changes_only_revision_dependency_and_identity(self) -> None:
        rc3_index = _json(RC3_ROOT / "fixtures/index.json")
        rc4_index = _json(CONTRACT_ROOT / "fixtures/index.json")
        rc3_by_case = {item["case"]: item for item in rc3_index["fixtures"]}
        rc4_by_case = {item["case"]: item for item in rc4_index["fixtures"]}
        self.assertEqual(set(rc3_by_case), set(rc4_by_case))
        for case, rc3_entry in rc3_by_case.items():
            rc4_entry = rc4_by_case[case]
            self.assertEqual(rc3_entry["path"], rc4_entry["path"])
            rc3_payload = _json_value(RC3_ROOT / rc3_entry["path"])
            rc4_payload = _json_value(CONTRACT_ROOT / rc4_entry["path"])
            expected = _replace_strings(
                deepcopy(rc3_payload),
                {
                    "0.1.0-rc.3": "0.1.0-rc.4",
                    "0.7.0-rc.3": "0.7.0-rc.4",
                },
            )
            _replace_fixture_identity(expected)
            self.assertEqual(rc4_payload, expected, case)

    def test_generated_client_pins_live_identity_and_preserves_private_boundary(
        self,
    ) -> None:
        source = (CONTRACT_ROOT / "generated/world_access_client.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "Generated by tools/product_world_access_live_contract.py", source
        )
        self.assertIn('WORLD_ACCESS_CONTRACT_REVISION = "0.1.0-rc.4"', source)
        self.assertIn(f'readonly kind: "{TOOL.LIVE_KIND}"', source)
        self.assertIn("readonly provider_calls: true", source)
        self.assertIn("readonly live_forge_evidence: true", source)
        self.assertIn("readonly live_attendee_execution: true", source)
        self.assertIn("readonly launch_binding_sha256: string", source)
        self.assertIn("server-only private BFF client", source)
        self.assertIn("requirePreviewLoopbackBaseUrl", source)
        self.assertIn('parsed.protocol !== "http:"', source)
        self.assertNotIn("localStorage", source)
        self.assertNotIn("document.cookie", source)

    def test_refresh_is_deterministic_and_preserves_provider_free_contract(
        self,
    ) -> None:
        before = {
            RC3_ROOT: _tree_identity(RC3_ROOT),
        }
        self.assertEqual(before[RC3_ROOT], RC3_TREE)
        self.assertEqual(_sha256(RC3_ROOT / "manifest.json"), RC3_MANIFEST_SHA256)
        with tempfile.TemporaryDirectory() as first_directory:
            with tempfile.TemporaryDirectory() as second_directory:
                first = Path(first_directory) / "v0.1.0-rc.4"
                second = Path(second_directory) / "v0.1.0-rc.4"
                TOOL.refresh_contract(first)
                TOOL.refresh_contract(second)
                self.assertEqual(_files(first), _files(second))
                self.assertEqual(
                    _draft_normalized_files(first),
                    _draft_normalized_files(CONTRACT_ROOT),
                )
        self.assertEqual(_tree_identity(RC3_ROOT), before[RC3_ROOT])

    def test_validation_rejects_false_live_truth_or_changed_dependency(self) -> None:
        cases = ("source", "product-dependency")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "contract"
                shutil.copytree(CONTRACT_ROOT, root)
                manifest_path = root / "manifest.json"
                manifest = _json(manifest_path)
                if case == "source":
                    manifest["source"]["provider_calls"] = False
                else:
                    manifest["dependencies"]["product_api"]["revision"] = "0.7.0-rc.3"
                _write_json(manifest_path, manifest)
                with self.assertRaises(TOOL.WorldAccessContractError):
                    TOOL.validate_contract(root)

    def test_tool_import_is_runtime_inert(self) -> None:
        code = f"""
import importlib.util
import pathlib
import threading
before = [(thread.ident, thread.name) for thread in threading.enumerate()]
path = pathlib.Path({str(TOOL_PATH)!r})
spec = importlib.util.spec_from_file_location("world_access_live_contract_inert", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
after = [(thread.ident, thread.name) for thread in threading.enumerate()]
assert after == before
assert module.CONTRACT_REVISION == "0.1.0-rc.4"
assert len(module.EXPECTED_OPERATIONS) == 6
"""
        subprocess.run([sys.executable, "-c", code], check=True)


def _replace_fixture_identity(value) -> None:
    if isinstance(value, dict):
        preview = value.get("preview")
        if isinstance(preview, dict):
            preview.update(
                {
                    "kind": TOOL.LIVE_KIND,
                    "provider_calls": True,
                    "live_forge_evidence": True,
                    "live_attendee_execution": True,
                    "live_capability_evidence": False,
                    "loopback_only": True,
                    "process_local": True,
                    "capabilities_enabled": [],
                }
            )
        for item in value.values():
            _replace_fixture_identity(item)
    elif isinstance(value, list):
        for item in value:
            _replace_fixture_identity(item)


def _replace_preview_schemas(value) -> None:
    if isinstance(value, dict):
        properties = value.get("properties")
        required = value.get("required")
        if (
            isinstance(properties, dict)
            and isinstance(required, list)
            and isinstance(properties.get("kind"), dict)
            and properties["kind"].get("const")
            == "provider_free_generated_access_preview"
        ):
            properties["kind"]["const"] = TOOL.LIVE_KIND
            properties["provider_calls"]["const"] = True
            properties["live_forge_evidence"]["const"] = True
            properties["live_attendee_execution"] = {
                "const": True,
                "type": "boolean",
            }
            required.insert(
                required.index("live_capability_evidence"),
                "live_attendee_execution",
            )
        for item in value.values():
            _replace_preview_schemas(item)
    elif isinstance(value, list):
        for item in value:
            _replace_preview_schemas(item)


def _replace_strings(value, replacements: dict[str, str]):
    if isinstance(value, dict):
        return {
            key: _replace_strings(item, replacements) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_strings(item, replacements) for item in value]
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
    return value


def _json(path: Path) -> dict:
    value = _json_value(path)
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not an object")
    return value


def _json_value(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def _draft_normalized_files(root: Path) -> dict[str, bytes]:
    files = _files(root)
    manifest = json.loads(files["manifest.json"])
    manifest["publication_status"] = "draft"
    manifest["backend_commit"] = None
    manifest["published_at"] = None
    files["manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()
    return files


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
