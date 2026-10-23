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
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_stage_live_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.4.0-rc.2"
PHASE3_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.4.0-rc.1"


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_stage_live_contract_unit_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load live Stage contract tool")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


TOOL = _load_tool()


class ProductStageLiveContractTests(unittest.TestCase):
    def test_contract_is_live_development_only_and_capability_disabled(self) -> None:
        validation = TOOL.validate_contract(CONTRACT_ROOT)
        manifest = json.loads((CONTRACT_ROOT / "manifest.json").read_text())
        openapi = json.loads((CONTRACT_ROOT / "openapi.json").read_text())
        preview = openapi["components"]["schemas"]["PreviewIdentity"]["properties"]

        self.assertEqual(validation["contract_revision"], "0.4.0-rc.2")
        self.assertEqual(validation["routes"], 15)
        self.assertEqual(manifest["publication_status"], "published")
        self.assertEqual(
            manifest["backend_commit"],
            "37083b1e2d3ecc2faad6bf473d6e5e3eb6fce6d8",
        )
        self.assertEqual(manifest["published_at"], "2026-08-24T14:01:24Z")
        self.assertEqual(preview["executor"]["const"], TOOL.LIVE_EXECUTOR)
        self.assertTrue(preview["provider_calls"]["const"])
        self.assertFalse(openapi["x-dreamland-development-live"]["capability_evidence"])
        self.assertTrue(
            all(not value["enabled"] for value in manifest["capabilities"].values())
        )

    def test_generated_client_accepts_only_live_development_context(self) -> None:
        source = (CONTRACT_ROOT / "generated" / "product_session_client.py").read_text()

        self.assertIn(TOOL.LIVE_EXECUTOR, source)
        self.assertIn('"provider_calls": True', source)
        self.assertNotIn("deterministic-no-provider-v1", source)

    def test_refresh_is_deterministic_and_leaves_rc1_unchanged(self) -> None:
        before = _tree_digest(PHASE3_ROOT)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "v0.4.0-rc.2"
            TOOL.refresh_contract(target)
            self.assertEqual(
                _tree_digest(target, normalize_publication=True),
                _tree_digest(CONTRACT_ROOT, normalize_publication=True),
            )
        self.assertEqual(_tree_digest(PHASE3_ROOT), before)

    def test_client_smoke_and_tool_import_are_runtime_inert(self) -> None:
        result = TOOL.client_smoke(CONTRACT_ROOT)
        self.assertEqual(result["validated_client_fixtures"], 6)
        script = (
            "import importlib.util, pathlib, threading; "
            f"p=pathlib.Path({str(TOOL_PATH)!r}); "
            "b={t.name for t in threading.enumerate()}; "
            "s=importlib.util.spec_from_file_location('live_contract_inert',p); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "a={t.name for t in threading.enumerate()}; "
            "assert a == b"
        )
        subprocess.run([sys.executable, "-c", script], check=True)


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
            content = (
                json.dumps(
                    manifest,
                    indent=2,
                    sort_keys=True,
                ).encode()
                + b"\n"
            )
        digest.update(content)
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
