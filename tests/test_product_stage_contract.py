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
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_stage_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.4.0-rc.1"
PHASE2_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.3.0-rc.1"


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_stage_contract_unit_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load Presenter Stage contract tool")
    module = importlib.util.module_from_spec(specification)
    prior = sys.modules.get(specification.name)
    sys.modules[specification.name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        if prior is None:
            sys.modules.pop(specification.name, None)
        else:
            sys.modules[specification.name] = prior
    return module


TOOL = _load_tool()


class ProductStageContractTests(unittest.TestCase):
    def test_contract_is_additive_unfrozen_and_fail_closed(self) -> None:
        validation = TOOL.validate_contract(CONTRACT_ROOT)
        manifest = json.loads((CONTRACT_ROOT / "manifest.json").read_text())
        openapi = json.loads((CONTRACT_ROOT / "openapi.json").read_text())

        self.assertEqual(validation["contract_revision"], "0.4.0-rc.1")
        self.assertEqual(validation["routes"], 15)
        self.assertEqual(
            manifest["freeze_status"], "pending_ui_tracked_doc_acknowledgement"
        )
        self.assertEqual(manifest["publication_status"], "published")
        self.assertEqual(
            manifest["backend_commit"],
            "712dd271cf39ecb1f9cddbec3c438fd68036a6be",
        )
        self.assertEqual(manifest["published_at"], "2026-08-23T23:56:55Z")
        self.assertTrue(
            all(not value["enabled"] for value in manifest["capabilities"].values())
        )
        self.assertEqual(openapi["servers"], [])
        self.assertTrue(set(TOOL._CORE.EXPECTED_OPERATIONS).issubset(openapi["paths"]))

    def test_stage_operations_encode_exact_resume_and_explicit_replacement(
        self,
    ) -> None:
        openapi = TOOL.build_openapi()
        stage_policy = openapi["x-dreamland-presenter-stage"]

        self.assertEqual(
            stage_policy["ordinary_resume"],
            "identical_launch_grant_and_compatible_effective_policy",
        )
        self.assertEqual(
            stage_policy["same_revision_different_selection"], "current_session_exists"
        )
        self.assertEqual(stage_policy["admission_class"], "presenter_stage_reserved")
        self.assertFalse(stage_policy["xray_included"])
        self.assertEqual(
            set(TOOL.STAGE_OPERATIONS),
            {
                "/product/v1/stage/sessions",
                "/product/v1/stage/sessions/current",
                "/product/v1/stage/sessions/{reference}/replace",
            },
        )

    def test_fixtures_cover_stage_create_resume_replace_recovery_and_privacy(
        self,
    ) -> None:
        fixtures = {fixture.case: fixture for fixture in TOOL.fixture_definitions()}
        required = {
            "stage_launch_request",
            "stage_launch_created",
            "stage_launch_resumed",
            "stage_replace_request",
            "stage_replace_success",
            "stage_launch_response_lost",
            "stage_snapshot_forbidden_raw_state",
        }

        self.assertTrue(required.issubset(fixtures))
        self.assertEqual(
            fixtures["stage_launch_response_lost"].payload["reconcile_with"],
            "same_request",
        )
        self.assertEqual(
            fixtures["stage_snapshot_forbidden_raw_state"].expected, "invalid"
        )

    def test_refresh_is_deterministic_and_does_not_rewrite_phase2(self) -> None:
        phase2_before = _tree_digest(PHASE2_ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "v0.4.0-rc.1"
            validation = TOOL.refresh_contract(root)
            first = _tree_digest(root)
            TOOL.refresh_contract(root)
            second = _tree_digest(root)

        self.assertEqual(validation["contract_revision"], "0.4.0-rc.1")
        self.assertEqual(first, second)
        self.assertEqual(_tree_digest(PHASE2_ROOT), phase2_before)

    def test_generated_client_is_server_side_and_contains_stage_methods(self) -> None:
        source = (CONTRACT_ROOT / "generated" / "product_session_client.py").read_text()

        self.assertIn("def launch_stage(", source)
        self.assertIn("def current_stage(", source)
        self.assertIn("def replace_stage(", source)
        self.assertNotIn("document.cookie", source)
        self.assertNotIn("localStorage", source)
        smoke = TOOL.client_smoke(CONTRACT_ROOT)
        self.assertEqual(smoke["validated_client_fixtures"], 6)

    def test_tool_import_is_runtime_inert(self) -> None:
        code = f"""
import importlib.util
import json
import sys
import threading
from pathlib import Path
before = tuple(thread.name for thread in threading.enumerate())
spec = importlib.util.spec_from_file_location("stage_contract_inert", Path({str(TOOL_PATH)!r}))
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
after = tuple(thread.name for thread in threading.enumerate())
print(json.dumps({{"before": before, "after": after, "runtime": "dreamland.backend" in sys.modules}}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["before"], payload["after"])
        self.assertFalse(payload["runtime"])


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and "__pycache__" not in item.parts
    ):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
