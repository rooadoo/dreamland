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
TOOL_PATH = (
    REPOSITORY_ROOT / "tools" / "product_attendee_security_deployment_contract.py"
)
LIVE_RC3_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.8.0-rc.3"
PUBLISHED_RC4_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.8.0-rc.4"
LIVE_RC3_MANIFEST_SHA256 = (
    "48f1c2f1a9c3d6421ebd54aeb029c99614192f0476338dde83d7762e6f9512fc"
)
LIVE_RC3_TREE = (
    193,
    "98831efadce8d4042b5eba6d29621f165e984bbab3e8736b0c8a6b67525ec38d",
)
PUBLISHED_RC4_MANIFEST_SHA256 = (
    "64454ebf7da717fdfd78ccc25b99681f07ee2225ce52b15836c171bc1d571567"
)
PUBLISHED_RC4_OPENAPI_SHA256 = (
    "e2d0e920cec59abda59b735b16b38627896eef405123e866ed1030702fafba80"
)
PUBLISHED_RC4_CLIENT_SHA256 = (
    "bd450ee334d0ec9adb8da1048607d421a580ae698b983a88073ebf16b97a1176"
)
PUBLISHED_RC4_TREE = (
    193,
    "4cc7f99a7afbd13261a2d81a50e40da1aa22e7627b7664d539bc743ff55177f3",
)


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_deployment_contract_test_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("attendee deployment contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load_tool()


class ProductAttendeeSecurityDeploymentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name) / "v0.8.0-rc.4"
        TOOL.refresh_contract(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_deployment_candidate_validates_and_smokes_without_capabilities(
        self,
    ) -> None:
        validated = TOOL.validate_contract(self.root)
        smoked = TOOL.client_smoke(self.root)
        manifest = _json(self.root / "manifest.json")

        self.assertEqual(validated["contract_revision"], "0.8.0-rc.4")
        self.assertEqual(validated["routes"], 25)
        self.assertEqual(validated["operations"], 25)
        self.assertEqual(validated["fixtures"], 188)
        self.assertEqual(validated["capabilities_enabled"], 0)
        self.assertEqual(smoked["validated_client_fixtures"], 24)
        self.assertEqual(smoked["rejected_client_mutations"], 150)
        self.assertEqual(manifest["implementation_status"], "integration_preview_only")
        self.assertEqual(
            manifest["transport_status"],
            TOOL.DEPLOYMENT_TRANSPORT_STATUS,
        )
        self.assertEqual(manifest["publication_status"], "release_candidate")
        self.assertIsNone(manifest["backend_commit"])
        self.assertIsNone(manifest["published_at"])
        self.assertTrue(
            all(
                capability["enabled"] is False
                and capability["enabled_revision"] is None
                for capability in manifest["capabilities"].values()
            )
        )

    def test_phase9c_metadata_declares_only_the_supervised_private_boundary(
        self,
    ) -> None:
        openapi = _json(self.root / "openapi.json")
        boundary = openapi["x-dreamland-attendee-deployment-boundary"]

        self.assertEqual(boundary, TOOL.DEPLOYMENT_BOUNDARY)
        self.assertEqual(boundary["invocation"], "explicit_only")
        self.assertEqual(boundary["listener_exposure"], "loopback_only")
        self.assertEqual(
            boundary["availability_window"],
            {
                "configuration": ("exact_utc_open_admission_close_and_recovery_close"),
                "deadline_projection": "startup_utc_anchor_plus_monotonic_elapsed",
                "admission_cutoff_trigger": "time_driven_without_request",
                "fresh_request_admission_at_or_after_cutoff": False,
            },
        )
        self.assertEqual(
            boundary["cutoff_recovery"],
            {
                "interval": "bounded",
                "listener": "loopback_recovery_only",
                "fresh_work_admission": False,
            },
        )
        self.assertEqual(
            boundary["supervised_convergence"]["signals"],
            ["SIGINT", "SIGTERM"],
        )
        self.assertTrue(boundary["supervised_convergence"]["bounded"])
        self.assertTrue(boundary["supervised_convergence"]["request_threads_joined"])
        self.assertEqual(
            boundary["rotation"]["credentials"],
            "process_restart_only",
        )
        self.assertEqual(
            boundary["rotation"]["service_epoch"],
            "process_restart_only",
        )
        self.assertFalse(boundary["rotation"]["hot_reload"])
        self.assertFalse(boundary["automatic_redispatch"])
        self.assertEqual(boundary["deliberate_access_end_owner"], "ui_bff")
        self.assertFalse(boundary["dreamland_access_ended_wire_error"])
        self.assertFalse(boundary["production_capability_evidence"])

        provider = openapi["x-dreamland-attendee-provider-boundary"]
        self.assertEqual(
            provider["privacy_identity"],
            {
                "notice_version": "notice:attendee-live-development-v1",
                "policy_id": "policy:attendee-live-authored-v1",
            },
        )
        preview = openapi["components"]["schemas"]["PreviewIdentity"]["properties"]
        self.assertEqual(preview["executor"]["const"], TOOL.LIVE_SECURITY_EXECUTOR)
        self.assertTrue(preview["provider_calls"]["const"])
        self.assertFalse(preview["live_capability_evidence"]["const"])

    def test_rc4_preserves_exact_rc3_paths_components_and_fixture_payloads(
        self,
    ) -> None:
        rc3 = _json(LIVE_RC3_ROOT / "openapi.json")
        rc4 = _json(self.root / "openapi.json")

        self.assertEqual(rc4["paths"], rc3["paths"])
        normalized_components = _replace_identity(
            deepcopy(rc4["components"]),
            "0.8.0-rc.4",
            "0.8.0-rc.3",
        )
        self.assertEqual(normalized_components, rc3["components"])

        rc3_fixtures = {
            path.relative_to(LIVE_RC3_ROOT).as_posix()
            for path in (LIVE_RC3_ROOT / "fixtures").rglob("*.json")
            if path.name != "index.json"
        }
        rc4_fixtures = {
            path.relative_to(self.root).as_posix()
            for path in (self.root / "fixtures").rglob("*.json")
            if path.name != "index.json"
        }
        self.assertEqual(rc4_fixtures, rc3_fixtures)
        for relative in sorted(rc3_fixtures):
            expected = _json_value(LIVE_RC3_ROOT / relative)
            actual = _replace_identity(
                _json_value(self.root / relative),
                "0.8.0-rc.4",
                "0.8.0-rc.3",
            )
            self.assertEqual(actual, expected, relative)

        rc3_index = _json(LIVE_RC3_ROOT / "fixtures/index.json")
        rc4_index = _json(self.root / "fixtures/index.json")
        self.assertEqual(rc4_index["coverage"], rc3_index["coverage"])
        self.assertEqual(
            [_fixture_identity(item) for item in rc4_index["fixtures"]],
            [_fixture_identity(item) for item in rc3_index["fixtures"]],
        )

    def test_revocation_restart_and_access_end_ownership_remain_exact(self) -> None:
        rc3 = _json(LIVE_RC3_ROOT / "openapi.json")
        rc4 = _json(self.root / "openapi.json")
        schema_name = "AttendeeContextRevocationResponse"
        self.assertEqual(
            rc4["components"]["schemas"][schema_name],
            rc3["components"]["schemas"][schema_name],
        )
        recovery = _json(
            self.root
            / "fixtures/canonical-valid/recovery/attendee-context-revocation-response-lost.json"
        )
        self.assertEqual(recovery["old_epoch_outcome"], "outcome_unknown")
        self.assertFalse(recovery["automatic_new_key_redispatch"])
        self.assertEqual(recovery["fresh_key_after_revocation"], "authority_denied")
        serialized = json.dumps(rc4, sort_keys=True)
        self.assertNotIn('"access_ended"', serialized)
        self.assertNotIn('"access-ended"', serialized)

    def test_generation_preserves_rc3_and_names_only_the_rc4_generator(self) -> None:
        self.assertEqual(
            _sha256(LIVE_RC3_ROOT / "manifest.json"),
            LIVE_RC3_MANIFEST_SHA256,
        )
        self.assertEqual(_tree_digest(LIVE_RC3_ROOT), LIVE_RC3_TREE)
        generated = _tree_digest(self.root)
        TOOL.refresh_contract(self.root)
        self.assertEqual(_tree_digest(self.root), generated)
        self.assertEqual(_tree_digest(LIVE_RC3_ROOT), LIVE_RC3_TREE)

        manifest = _json(self.root / "manifest.json")
        for command in manifest["generation"].values():
            self.assertIn(
                "tools/product_attendee_security_deployment_contract.py",
                command,
            )
            self.assertIn("contracts/product-api/v0.8.0-rc.4", command)
            self.assertNotIn(
                "tools/product_attendee_security_live_identity_contract.py",
                command,
            )
        schema_text = (self.root / "manifest.schema.json").read_text(encoding="utf-8")
        self.assertIn(
            "tools/product_attendee_security_deployment_contract.py",
            schema_text,
        )
        self.assertNotIn(
            "tools/product_attendee_security_live_identity_contract.py",
            schema_text,
        )

    def test_published_rc4_bytes_remain_exact(self) -> None:
        self.assertEqual(
            _sha256(PUBLISHED_RC4_ROOT / "manifest.json"),
            PUBLISHED_RC4_MANIFEST_SHA256,
        )
        self.assertEqual(
            _sha256(PUBLISHED_RC4_ROOT / "openapi.json"),
            PUBLISHED_RC4_OPENAPI_SHA256,
        )
        self.assertEqual(
            _sha256(PUBLISHED_RC4_ROOT / "generated" / "product_session_client.py"),
            PUBLISHED_RC4_CLIENT_SHA256,
        )
        self.assertEqual(_tree_digest(PUBLISHED_RC4_ROOT), PUBLISHED_RC4_TREE)

    def test_refresh_refuses_a_published_rc4_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "v0.8.0-rc.4"
            TOOL.refresh_contract(root)
            manifest_path = root / "manifest.json"
            manifest = _json(manifest_path)
            manifest["publication_status"] = "published"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                TOOL._CORE.ContractError,
                "published deployment artifacts are immutable",
            ):
                TOOL.refresh_contract(root)

    def test_publication_pins_supervisor_seam_and_rejects_drift(self) -> None:
        required = {
            "tools/product_attendee_contract.py",
            "tools/product_attendee_live_contract.py",
            "tools/product_attendee_security_preview.py",
            "tools/product_attendee_security_deployment.py",
            "tools/product_attendee_security_deployment_contract.py",
            "tools/product_forge_stage_contract.py",
            "tools/product_launch_selection_contract.py",
            "tools/product_launch_selection_live_contract.py",
            "tools/product_session_contract.py",
            "tools/product_stage_contract.py",
            "tools/product_stage_live_contract.py",
            "tools/product_xray_contract.py",
        }
        self.assertLessEqual(required, TOOL.PUBLICATION_IMPLEMENTATION_FILES)
        loaded_generators = {
            Path(module.__file__).resolve().relative_to(REPOSITORY_ROOT).as_posix()
            for module in TOOL._MODULES
        }
        self.assertLessEqual(
            loaded_generators,
            TOOL.PUBLICATION_IMPLEMENTATION_FILES,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
            root = repository / "contracts" / "product-api" / "v0.8.0-rc.4"
            TOOL.refresh_contract(root)
            _copy_publication_implementation(repository)
            backend_commit = _commit_all(
                repository,
                "Record exact Phase 9C implementation and artifacts",
            )
            manifest_path = root / "manifest.json"
            before = manifest_path.read_bytes()
            drifted = repository / "tools/product_session_contract.py"
            drifted.write_bytes(drifted.read_bytes() + b"\n# uncommitted drift\n")

            with self.assertRaisesRegex(
                TOOL._CORE.ContractError,
                "does not contain the exact Phase 9C implementation",
            ):
                TOOL.publish_contract(
                    root,
                    backend_commit=backend_commit,
                    published_at="2026-09-02T12:00:00Z",
                )
            self.assertEqual(manifest_path.read_bytes(), before)


def _json(path: Path) -> dict:
    value = _json_value(path)
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not an object")
    return value


def _json_value(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _replace_identity(value: object, old: str, new: str):
    if isinstance(value, str):
        return new if value == old else value
    if isinstance(value, dict):
        return {key: _replace_identity(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_identity(item, old, new) for item in value]
    return value


def _fixture_identity(value: object) -> tuple[object, ...]:
    if not isinstance(value, dict):
        raise AssertionError("fixture index entry is not an object")
    return tuple(
        value[name]
        for name in (
            "case",
            "class",
            "expected",
            "path",
            "schema_target",
            "client_action",
        )
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
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
