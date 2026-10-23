from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = (
    REPOSITORY_ROOT / "tools" / "product_attendee_security_live_identity_contract.py"
)
SECURITY_PREVIEW_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.8.0-rc.1"


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_live_identity_contract_test_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("attendee live identity contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load_tool()


class ProductAttendeeSecurityLiveIdentityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name) / "v0.8.0-rc.3"
        TOOL.refresh_contract(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_corrective_candidate_validates_and_smokes_without_capabilities(
        self,
    ) -> None:
        validated = TOOL.validate_contract(self.root)
        smoked = TOOL.client_smoke(self.root)

        self.assertEqual(validated["contract_revision"], "0.8.0-rc.3")
        self.assertEqual(validated["routes"], 25)
        self.assertEqual(validated["operations"], 25)
        self.assertEqual(validated["fixtures"], 188)
        self.assertEqual(validated["capabilities_enabled"], 0)
        self.assertEqual(smoked["validated_client_fixtures"], 24)
        self.assertEqual(smoked["rejected_client_mutations"], 150)

    def test_provider_boundary_binds_exact_live_privacy_identity(self) -> None:
        openapi = _json(self.root / "openapi.json")
        manifest = _json(self.root / "manifest.json")
        boundary = openapi["x-dreamland-attendee-provider-boundary"]

        self.assertEqual(
            boundary["privacy_identity"],
            {
                "notice_version": "notice:attendee-live-development-v1",
                "policy_id": "policy:attendee-live-authored-v1",
            },
        )
        self.assertEqual(
            boundary["privacy_identity"]["notice_version"],
            TOOL.ATTENDEE_NOTICE_VERSION,
        )
        self.assertEqual(
            boundary["privacy_identity"]["policy_id"],
            TOOL.ATTENDEE_POLICY_ID,
        )
        preview = openapi["components"]["schemas"]["PreviewIdentity"]["properties"]
        self.assertEqual(preview["executor"]["const"], TOOL.LIVE_SECURITY_EXECUTOR)
        self.assertTrue(preview["provider_calls"]["const"])
        self.assertFalse(preview["live_capability_evidence"]["const"])
        self.assertTrue(
            all(
                capability["enabled"] is False
                and capability["enabled_revision"] is None
                for capability in manifest["capabilities"].values()
            )
        )

    def test_only_attendee_option_and_selection_notices_are_corrected(self) -> None:
        live_notice = TOOL.ATTENDEE_NOTICE_VERSION
        options = _json(
            self.root / "fixtures/canonical-valid/attendee/options-authored.json"
        )
        selection_request = _json(
            self.root / "fixtures/canonical-valid/attendee/selection-request.json"
        )
        selection = _json(
            self.root / "fixtures/canonical-valid/attendee/selection-fresh.json"
        )

        self.assertEqual(options["options"][0]["notice_version"], live_notice)
        self.assertEqual(
            selection_request["notice_version_presented"],
            live_notice,
        )
        self.assertEqual(selection["selection"]["notice_version"], live_notice)

        attendee_notice_values: set[str] = set()
        for classification in ("canonical-valid", "forward-compat", "invalid"):
            attendee_root = self.root / "fixtures" / classification / "attendee"
            for path in attendee_root.glob("*.json"):
                attendee_notice_values.update(
                    value
                    for value in _strings(json.loads(path.read_text(encoding="utf-8")))
                    if value.startswith("notice:")
                )
        self.assertEqual(attendee_notice_values, {live_notice})

        stage = _json(self.root / "fixtures/canonical-valid/stage/launch-created.json")
        self.assertEqual(
            stage["snapshot"]["launch_binding"]["notice_version"],
            "preview-notice-v1",
        )
        self.assertEqual(
            stage["snapshot"]["launch_binding"]["policy_id"],
            "preview-policy-v1",
        )

    def test_live_wire_revocation_and_restart_recovery_matches_preview(self) -> None:
        preview = _json(SECURITY_PREVIEW_ROOT / "openapi.json")
        rc3 = _json(self.root / "openapi.json")

        self.assertEqual(rc3["paths"], preview["paths"])
        self.assertEqual(
            rc3["components"]["schemas"]["AttendeeContextRevocationResponse"],
            preview["components"]["schemas"]["AttendeeContextRevocationResponse"],
        )
        recovery = _json(
            self.root
            / "fixtures/canonical-valid/recovery/attendee-context-revocation-response-lost.json"
        )
        self.assertEqual(recovery["old_epoch_outcome"], "outcome_unknown")
        self.assertFalse(recovery["automatic_new_key_redispatch"])
        self.assertEqual(recovery["fresh_key_after_revocation"], "authority_denied")
        serialized = json.dumps(rc3, sort_keys=True)
        self.assertNotIn("access_ended", serialized)
        self.assertNotIn("access-ended", serialized)

    def test_generation_is_deterministic_and_names_the_current_generator(
        self,
    ) -> None:
        generated = _tree_digest(self.root)
        TOOL.refresh_contract(self.root)
        self.assertEqual(_tree_digest(self.root), generated)

        manifest = _json(self.root / "manifest.json")
        for command in manifest["generation"].values():
            self.assertIn(
                "tools/product_attendee_security_live_identity_contract.py",
                command,
            )
            self.assertIn("contracts/product-api/v0.8.0-rc.3", command)
        schema_text = (self.root / "manifest.schema.json").read_text(encoding="utf-8")
        self.assertIn(
            "tools/product_attendee_security_live_identity_contract.py",
            schema_text,
        )


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not an object")
    return value


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _tree_digest(root: Path) -> tuple[int, str]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return len(files), digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
