from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools/product_attendee_security_live_contract.py"
SECURITY_RC1_ROOT = REPOSITORY_ROOT / "contracts/product-api/v0.8.0-rc.1"
SECURITY_RC1_TREE = (
    193,
    "0462d3e1c318c8c26600ae08a09e2b14777d0bb0ece6bb3ef8b99af40c5c24d9",
)


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_live_contract_test_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("attendee live security contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load_tool()


class ProductAttendeeSecurityLiveContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name) / "v0.8.0-rc.2"
        TOOL.refresh_contract(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_live_candidate_validates_and_smokes_without_capabilities(self) -> None:
        validated = TOOL.validate_contract(self.root)
        smoked = TOOL.client_smoke(self.root)

        self.assertEqual(validated["contract_revision"], "0.8.0-rc.2")
        self.assertEqual(validated["routes"], 25)
        self.assertEqual(validated["operations"], 25)
        self.assertEqual(validated["fixtures"], 188)
        self.assertEqual(validated["capabilities_enabled"], 0)
        self.assertEqual(smoked["validated_client_fixtures"], 24)
        self.assertEqual(smoked["rejected_client_mutations"], 150)

    def test_rc1_wire_revocation_and_restart_recovery_remain_exact(self) -> None:
        rc1 = _json(SECURITY_RC1_ROOT / "openapi.json")
        live = _json(self.root / "openapi.json")

        self.assertEqual(live["paths"], rc1["paths"])
        self.assertEqual(
            live["components"]["schemas"]["AttendeeContextRevocationResponse"],
            rc1["components"]["schemas"]["AttendeeContextRevocationResponse"],
        )
        revoke_path = "/product/v1/attendee/contexts/{gateway_context_id}"
        self.assertEqual(
            live["paths"][revoke_path]["delete"]["x-dreamland-idempotency"],
            rc1["paths"][revoke_path]["delete"]["x-dreamland-idempotency"],
        )
        serialized = json.dumps(live, sort_keys=True)
        self.assertNotIn("access_ended", serialized)
        self.assertNotIn("access-ended", serialized)
        recovery = _json(
            self.root
            / "fixtures/canonical-valid/recovery/attendee-context-revocation-response-lost.json"
        )
        self.assertEqual(recovery["old_epoch_outcome"], "outcome_unknown")
        self.assertFalse(recovery["automatic_new_key_redispatch"])
        self.assertEqual(recovery["fresh_key_after_revocation"], "authority_denied")

    def test_identity_is_live_attendee_only_and_explicitly_non_production(
        self,
    ) -> None:
        openapi = _json(self.root / "openapi.json")
        manifest = _json(self.root / "manifest.json")
        preview = openapi["components"]["schemas"]["PreviewIdentity"]["properties"]
        boundary = openapi["x-dreamland-attendee-provider-boundary"]

        self.assertEqual(preview["executor"]["const"], TOOL.LIVE_SECURITY_EXECUTOR)
        self.assertTrue(preview["provider_calls"]["const"])
        self.assertTrue(preview["test_only"]["const"])
        self.assertFalse(preview["live_capability_evidence"]["const"])
        self.assertTrue(boundary["readiness_before_listen"])
        self.assertEqual(boundary["sdk_retries"], 0)
        self.assertFalse(boundary["automatic_redispatch"])
        self.assertFalse(boundary["stage_or_forge_authority_composed"])
        self.assertFalse(boundary["local_kill_proves_upstream_termination"])
        self.assertEqual(
            boundary["restart_recovery"],
            "explicit_client_context_restart_only",
        )
        self.assertTrue(
            all(
                capability["enabled"] is False
                and capability["enabled_revision"] is None
                for capability in manifest["capabilities"].values()
            )
        )

    def test_generation_does_not_mutate_published_rc1(self) -> None:
        self.assertEqual(_tree_digest(SECURITY_RC1_ROOT), SECURITY_RC1_TREE)
        generated = _tree_digest(self.root)
        TOOL.refresh_contract(self.root)
        self.assertEqual(_tree_digest(self.root), generated)
        self.assertEqual(_tree_digest(SECURITY_RC1_ROOT), SECURITY_RC1_TREE)

    def test_publish_rejects_dirty_live_execution_dependency(self) -> None:
        required_dependencies = {
            "src/dreamland/product/authored_artifacts.py",
            "src/dreamland/product/launch_selection_runtime.py",
            "src/dreamland/product/live_agent.py",
            "src/dreamland/product/runtime_executor.py",
        }
        self.assertLessEqual(
            required_dependencies,
            TOOL.PUBLICATION_IMPLEMENTATION_FILES,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
            )
            root = repository / "contracts" / "product-api" / "v0.8.0-rc.2"
            TOOL.refresh_contract(root)
            _copy_publication_implementation(repository)
            backend_commit = _commit_all(
                repository,
                "Record exact Phase 9B implementation and artifacts",
            )
            manifest_path = root / "manifest.json"
            before = manifest_path.read_bytes()
            drifted = repository / "src/dreamland/product/runtime_executor.py"
            drifted.write_bytes(drifted.read_bytes() + b"\n# uncommitted drift\n")

            with self.assertRaisesRegex(
                TOOL._CORE.ContractError,
                "does not contain the exact Phase 9B implementation",
            ):
                TOOL.publish_contract(
                    root,
                    backend_commit=backend_commit,
                    published_at="2026-09-01T12:00:00Z",
                )

            self.assertEqual(manifest_path.read_bytes(), before)


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not an object")
    return value


def _tree_digest(root: Path) -> tuple[int, str]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return len(files), digest.hexdigest()


def _copy_publication_implementation(repository: Path) -> None:
    for relative in TOOL.PUBLICATION_IMPLEMENTATION_FILES:
        source = REPOSITORY_ROOT / relative
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _commit_all(repository: Path, message: str) -> str:
    subprocess.run(
        ["git", "-C", str(repository), "add", "."],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=DREAMLAND Contract Test",
            "-c",
            "user.email=dreamland-contract@example.invalid",
            "commit",
            "--quiet",
            "-m",
            message,
        ],
        check=True,
    )
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
