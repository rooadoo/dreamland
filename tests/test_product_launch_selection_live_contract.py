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
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_launch_selection_live_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.7.0-rc.2"
RC1_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.7.0-rc.1"
RC1_TREE = (
    160,
    "ed39163c55940f4db19756809f9ea5099562347dc6e6aacff59470e2b7938a93",
)
WORLD_FORGE_DEPENDENCY = {
    "compatibility": "exact",
    "contract_name": "dreamland-private-world-forge-api",
    "contract_revision": "0.1.0-rc.4",
    "manifest_path": "contracts/world-forge-api/v0.1.0-rc.4/manifest.json",
    "manifest_sha256": (
        "0824a9d7f8ea1b1ed264a5f6cd76dd6831054b44932973278b60db1c384baa09"
    ),
}


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_launch_selection_live_contract_test_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("live launch-selection contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load_tool()


class ProductLaunchSelectionLiveContractTests(unittest.TestCase):
    def test_release_candidate_validates_and_generated_client_smokes(self) -> None:
        validated = TOOL.validate_contract(CONTRACT_ROOT)
        smoked = TOOL.client_smoke(CONTRACT_ROOT)

        self.assertEqual(validated["contract_revision"], "0.7.0-rc.2")
        self.assertEqual(validated["routes"], 21)
        self.assertEqual(validated["fixtures"], 155)
        self.assertEqual(validated["capabilities_enabled"], 0)
        self.assertEqual(smoked["validated_client_fixtures"], 16)
        self.assertEqual(smoked["rejected_client_mutations"], 118)

    def test_identity_is_live_but_selection_exchange_remains_provider_free(
        self,
    ) -> None:
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        manifest = _json(CONTRACT_ROOT / "manifest.json")
        preview = openapi["components"]["schemas"]["PreviewIdentity"]["properties"]
        development = openapi["x-dreamland-development-live"]
        selection = openapi["x-dreamland-stage-launch-selection"]

        self.assertEqual(preview["executor"]["const"], TOOL.LIVE_EXECUTOR)
        self.assertTrue(preview["provider_calls"]["const"])
        self.assertTrue(preview["test_only"]["const"])
        self.assertFalse(preview["live_capability_evidence"]["const"])
        self.assertEqual(
            development,
            {
                "capability_evidence": False,
                "explicit_start_only": True,
                "loopback_only": True,
                "normal_runtime_composition": False,
                "provider_calls": True,
                "usage_accounting": "full_reservation_conservative",
            },
        )
        self.assertFalse(selection["provider_dispatch"])
        self.assertFalse(selection["normal_runtime_composition"])
        self.assertFalse(selection["capability_evidence"])
        self.assertEqual(
            manifest["dependencies"]["world_forge"], WORLD_FORGE_DEPENDENCY
        )
        self.assertTrue(
            all(
                not capability["enabled"]
                for capability in manifest["capabilities"].values()
            )
        )

    def test_rc2_changes_only_revision_and_live_identity_metadata(self) -> None:
        rc1 = _json(RC1_ROOT / "openapi.json")
        rc2 = _json(CONTRACT_ROOT / "openapi.json")
        normalized = _replace_string(deepcopy(rc1), "0.7.0-rc.1", "0.7.0-rc.2")
        normalized["info"] = deepcopy(rc2["info"])
        preview = normalized["components"]["schemas"]["PreviewIdentity"]["properties"]
        preview["executor"]["const"] = TOOL.LIVE_EXECUTOR
        preview["provider_calls"]["const"] = True
        normalized["x-dreamland-development-live"] = deepcopy(
            rc2["x-dreamland-development-live"]
        )

        self.assertEqual(rc2, normalized)
        self.assertEqual(rc2["paths"], normalized["paths"])
        self.assertEqual(len(rc2["paths"]), 21)

    def test_generated_client_pins_only_the_live_context(self) -> None:
        source = (CONTRACT_ROOT / "generated" / "product_session_client.py").read_text(
            encoding="utf-8"
        )
        generated = _load_generated_client()
        live_options = _json(
            CONTRACT_ROOT
            / "fixtures/canonical-valid/launch-selection/options-mixed.json"
        )
        provider_free_options = _json(
            RC1_ROOT / "fixtures/canonical-valid/launch-selection/options-mixed.json"
        )

        self.assertIn(TOOL.LIVE_EXECUTOR, source)
        self.assertIn('"provider_calls": True', source)
        self.assertNotIn("deterministic-launch-selection-no-provider-v1", source)
        self.assertIs(
            generated.validate_presenter_stage_launch_options(live_options),
            live_options,
        )
        with self.assertRaises(ValueError):
            generated.validate_presenter_stage_launch_options(provider_free_options)

    def test_refresh_is_deterministic_and_preserves_published_rc1(self) -> None:
        self.assertEqual(_tree_identity(RC1_ROOT), RC1_TREE)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "v0.7.0-rc.2"
            TOOL.refresh_contract(target)
            self.assertEqual(
                _tree_digest(target, normalize_publication=True),
                _tree_digest(CONTRACT_ROOT, normalize_publication=True),
            )
        self.assertEqual(_tree_identity(RC1_ROOT), RC1_TREE)

    def test_tool_import_is_runtime_inert(self) -> None:
        code = f"""
import importlib.util
import pathlib
import threading
before = [(thread.ident, thread.name) for thread in threading.enumerate()]
path = pathlib.Path({str(TOOL_PATH)!r})
spec = importlib.util.spec_from_file_location("launch_selection_live_contract_inert", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
after = [(thread.ident, thread.name) for thread in threading.enumerate()]
assert after == before
assert module.CONTRACT_REVISION == "0.7.0-rc.2"
"""
        subprocess.run([sys.executable, "-c", code], check=True)


def _load_generated_client():
    path = CONTRACT_ROOT / "generated" / "product_session_client.py"
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_launch_selection_live_generated_test_client",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("generated live launch-selection client is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not an object")
    return value


def _replace_string(value, old: str, new: str):
    if isinstance(value, dict):
        return {key: _replace_string(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_string(item, old, new) for item in value]
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def _tree_identity(root: Path) -> tuple[int, str]:
    files = tuple(
        sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    )
    return len(files), _tree_digest(root)


def _tree_digest(root: Path, *, normalize_publication: bool = False) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        content = path.read_bytes()
        if normalize_publication and path.name == "manifest.json":
            manifest = json.loads(content)
            manifest["backend_commit"] = None
            manifest["publication_status"] = "release_candidate"
            manifest["published_at"] = None
            content = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
        digest.update(content)
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
