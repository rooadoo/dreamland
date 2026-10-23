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
TOOL_PATH = REPOSITORY_ROOT / "tools/product_attendee_security_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts/product-api/v0.8.0-rc.1"
PRODUCT_RC4_ROOT = REPOSITORY_ROOT / "contracts/product-api/v0.7.0-rc.4"
WORLD_ACCESS_RC4_ROOT = REPOSITORY_ROOT / "contracts/world-access-api/v0.1.0-rc.4"
PRODUCT_RC4_TREE = (
    186,
    "49d5d342a3c7ec72a59c4c99128e5332edfaa3374226648a574f1cd1de6a094f",
)
WORLD_ACCESS_RC4_TREE = (
    76,
    "24b50d53fb2ec59b57a4560c42f8a11edf12f6e759ebb11044664c815ff72a0f",
)


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_contract_test_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("attendee security contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load_tool()


class ProductAttendeeSecurityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.generated_root = Path(cls._temporary.name) / "v0.8.0-rc.1"
        TOOL.refresh_contract(cls.generated_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_candidate_validates_and_generated_server_client_smokes(self) -> None:
        validated = TOOL.validate_contract(CONTRACT_ROOT)
        smoked = TOOL.client_smoke(CONTRACT_ROOT)

        self.assertEqual(validated["contract_revision"], "0.8.0-rc.1")
        self.assertEqual(validated["routes"], 25)
        self.assertEqual(validated["operations"], 25)
        self.assertEqual(validated["fixtures"], 188)
        self.assertEqual(validated["canonical_valid"], 134)
        self.assertEqual(validated["forward_compat"], 7)
        self.assertEqual(validated["invalid"], 47)
        self.assertEqual(validated["safe_error_codes"], 46)
        self.assertEqual(validated["capabilities_enabled"], 0)
        self.assertEqual(smoked["validated_client_fixtures"], 24)
        self.assertEqual(smoked["rejected_client_mutations"], 150)

    def test_rc4_routes_and_operation_shapes_are_preserved_additively(self) -> None:
        rc4 = _json(PRODUCT_RC4_ROOT / "openapi.json")
        candidate = _json(CONTRACT_ROOT / "openapi.json")
        self.assertEqual(
            set(candidate["paths"]),
            {
                *rc4["paths"],
                TOOL.ATTENDEE_CONTEXT_REVOCATION_PATH,
            },
        )

        for path, rc4_path in rc4["paths"].items():
            observed = deepcopy(candidate["paths"][path])
            expected = deepcopy(rc4_path)
            self.assertEqual(
                _without_operation_extensions(observed),
                _without_operation_extensions(expected),
                path,
            )

    def test_revocation_route_is_private_exact_and_identifier_safe(self) -> None:
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        operation = openapi["paths"][TOOL.ATTENDEE_CONTEXT_REVOCATION_PATH]["delete"]
        response = openapi["components"]["schemas"]["AttendeeContextRevocationResponse"]

        self.assertEqual(operation["operationId"], "revokeAttendeeContext")
        self.assertEqual(
            {
                parameter["$ref"].rsplit("/", 1)[-1]
                for parameter in operation["parameters"]
            },
            {"GatewayContextId", "ExpectedEpoch", "ActorContext", "IdempotencyKey"},
        )
        self.assertNotIn("requestBody", operation)
        self.assertEqual(operation["security"], [{"ProductServiceBearer": []}])
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/AttendeeContextRevocationResponse"},
        )
        self.assertEqual(
            set(response["required"]),
            {
                "kind",
                "service_epoch",
                "replayed",
                "outcome",
                "contract",
                "preview",
            },
        )
        self.assertEqual(
            response["properties"]["outcome"]["enum"],
            ["revoked", "already_revoked"],
        )
        for secret in ("actor_context", "gateway_context_id", "revoked_at"):
            self.assertNotIn(secret, response["properties"])

    def test_revocation_recovery_replays_after_fence_without_redispatch(self) -> None:
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        policy = openapi["paths"][TOOL.ATTENDEE_CONTEXT_REVOCATION_PATH]["delete"][
            "x-dreamland-idempotency"
        ]
        order = policy["validation_order"]
        self.assertLess(
            order.index("exact_replay_or_changed_request_conflict"),
            order.index("current_gateway_actor_binding"),
        )
        self.assertLess(
            order.index("current_gateway_actor_binding"), order.index("claim")
        )
        self.assertLess(order.index("claim"), order.index("revoke_binding"))
        self.assertNotIn("logical_request_admission", order)
        self.assertNotIn(
            "logical_rate_limited", policy.get("unclaimed_retryable_rejections", [])
        )
        self.assertEqual(policy["claimed_deterministic_rejections"], [])
        self.assertEqual(
            policy["unclaimed_prebinding_rejections"], ["authority_denied"]
        )
        self.assertEqual(
            policy["same_key_same_request"],
            "replay_original_success_even_after_context_revocation",
        )
        self.assertEqual(policy["fresh_key_after_revocation"], "authority_denied")
        self.assertEqual(
            policy["scope"],
            [
                "service_epoch",
                "private_service_principal",
                "operation_family",
                "idempotency_key",
            ],
        )
        self.assertEqual(
            policy["semantic_request_evidence"],
            {
                "canonical_body": False,
                "included_headers": ["X-Dreamland-Actor-Context"],
                "path_parameters": ["gateway_context_id"],
            },
        )

        recovery = _json(
            CONTRACT_ROOT
            / "fixtures/canonical-valid/recovery/attendee-context-revocation-response-lost.json"
        )
        self.assertTrue(recovery["same_key_replay"])
        self.assertEqual(recovery["replay_after_revocation"], "return_original_success")
        self.assertEqual(recovery["changed_request_outcome"], "idempotency_conflict")
        self.assertEqual(recovery["fresh_key_after_revocation"], "authority_denied")
        self.assertFalse(recovery["prebinding_authority_denial_claimed"])
        self.assertFalse(recovery["automatic_new_key_redispatch"])

    def test_logical_admission_is_replay_safe_and_not_public_throttling(self) -> None:
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        boundary = openapi["x-dreamland-private-attendee-security"]
        admission = boundary["logical_admission"]
        policy = openapi["x-dreamland-idempotency-policy"]

        self.assertEqual(boundary["transport"], "private_server_to_server_only")
        self.assertFalse(boundary["browser_direct_access"])
        self.assertFalse(boundary["browser_credentials_accepted"])
        self.assertFalse(boundary["production_capabilities_enabled"])
        self.assertTrue(boundary["revocation"]["revokes_existing_sessions"])
        self.assertTrue(
            boundary["revocation"]["fences_active_commands_output_and_accepted_state"]
        )
        self.assertTrue(
            boundary["revocation"]["expires_issued_grants_and_session_authority"]
        )
        self.assertFalse(admission["network_or_ip_identity_used"])
        self.assertEqual(
            admission["bootstrap_subject"], "opaque_gateway_context_sha256"
        )
        self.assertEqual(
            admission["authenticated_subject"], "bound_gateway_context_sha256"
        )
        self.assertEqual(admission["unit"], "first_seen_logical_request")
        self.assertEqual(
            admission["charged_operations"],
            [
                "bootstrapAttendeeContext",
                "exchangeAttendeeLaunchSelection",
                "createProductSession",
                "submitProductCommand",
                "resetProductSession",
                "switchProductSession",
            ],
        )
        self.assertEqual(
            set(admission["exempt_operations"]),
            {
                "revokeAttendeeContext",
                "getAttendeeLaunchOptions",
                "getProductCapabilities",
                "getCurrentProductSession",
                "getProductSessionSnapshot",
                "getProductSessionEvents",
                "getProductSessionTranscript",
                "getProductCommandResult",
                "cancelProductCommand",
                "closeProductSession",
            },
        )
        self.assertEqual(
            admission["exact_claimed_replay"], "bypass_without_consumption"
        )
        self.assertEqual(admission["rejection"], "logical_rate_limited")
        self.assertFalse(admission["rejection_claimed"])
        self.assertEqual(admission["public_source_throttling_owner"], "ui_bff")
        self.assertIn("logical_rate_limited", policy["unclaimed_rejections"])
        self.assertIn("without_new_admission_charge", policy["exact_retry"])

        operations = {
            operation["operationId"]: operation
            for path_item in openapi["paths"].values()
            for operation in path_item.values()
            if isinstance(operation, dict) and "operationId" in operation
        }
        for operation_id in admission["charged_operations"]:
            metadata = operations[operation_id]["x-dreamland-logical-admission"]
            self.assertEqual(metadata["charge"], "first_seen_logical_request_only")
            self.assertEqual(metadata["exact_replay_charge"], 0)
            self.assertEqual(metadata["changed_request_conflict_charge"], 0)
            self.assertFalse(metadata["retryable_denial_claimed"])
        for operation_id in admission["exempt_operations"]:
            self.assertNotIn("x-dreamland-logical-admission", operations[operation_id])

        logical_error = _json(
            CONTRACT_ROOT / "fixtures/canonical-valid/errors/logical-rate-limited.json"
        )
        self.assertEqual(logical_error["error"]["code"], "logical_rate_limited")
        self.assertEqual(logical_error["error"]["retry"], "after_delay")
        self.assertEqual(logical_error["error"]["retry_after_ms"], 1_000)
        self.assertEqual(logical_error["error"].get("context", {}), {})

    def test_provider_free_loopback_identity_and_capabilities_are_disabled(
        self,
    ) -> None:
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        manifest = _json(CONTRACT_ROOT / "manifest.json")
        preview = openapi["components"]["schemas"]["PreviewIdentity"]["properties"]

        self.assertEqual(preview["executor"]["const"], TOOL.SECURITY_EXECUTOR)
        self.assertFalse(preview["provider_calls"]["const"])
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
                "provider_calls": False,
                "usage_accounting": "provider_free_deterministic",
            },
        )
        self.assertIn(
            manifest["publication_status"],
            {"release_candidate", "published"},
        )
        if manifest["publication_status"] == "release_candidate":
            self.assertIsNone(manifest["backend_commit"])
            self.assertIsNone(manifest["published_at"])
        else:
            self.assertRegex(manifest["backend_commit"], r"^[0-9a-f]{40}$")
            self.assertRegex(
                manifest["published_at"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
            )
        self.assertTrue(
            all(
                not capability["enabled"]
                for capability in manifest["capabilities"].values()
            )
        )

    def test_generated_client_is_server_side_and_pins_security_identity(self) -> None:
        source = (CONTRACT_ROOT / "generated/product_session_client.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "Generated by tools/product_attendee_security_contract.py", source
        )
        self.assertIn("def revoke_attendee_context(", source)
        self.assertIn('"DELETE"', source)
        self.assertIn(TOOL.SECURITY_EXECUTOR, source)
        self.assertIn('"provider_calls": False', source)
        self.assertNotIn('"provider_calls": True', source)

    def test_generation_is_deterministic_and_published_predecessors_are_exact(
        self,
    ) -> None:
        self.assertEqual(
            _contract_identity_without_publication(CONTRACT_ROOT),
            _contract_identity_without_publication(self.generated_root),
        )
        self.assertEqual(_tree_identity(PRODUCT_RC4_ROOT), PRODUCT_RC4_TREE)
        self.assertEqual(_tree_identity(WORLD_ACCESS_RC4_ROOT), WORLD_ACCESS_RC4_TREE)

    def test_manifest_dispatch_and_published_refresh_guard_are_owned(self) -> None:
        self.assertIs(TOOL._CORE.publish_contract, TOOL.publish_contract)
        manifest = _json(CONTRACT_ROOT / "manifest.json")
        prefix = (
            ".venv/bin/python tools/product_attendee_security_contract.py"
            " {verb} contracts/product-api/v0.8.0-rc.1"
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
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "v0.8.0-rc.1"
            TOOL.refresh_contract(target)
            target_manifest = _json(target / "manifest.json")
            target_manifest["publication_status"] = "published"
            (target / "manifest.json").write_text(
                json.dumps(target_manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(Exception, "immutable"):
                TOOL.refresh_contract(target)

    def test_publish_binds_exact_phase9a_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
            )
            root = repository / "contracts" / "product-api" / "v0.8.0-rc.1"
            TOOL.refresh_contract(root)
            _copy_publication_implementation(repository)
            backend_commit = _commit_all(
                repository,
                "Record exact Phase 9A implementation and artifacts",
            )
            before = _json(root / "manifest.json")

            published = TOOL.publish_contract(
                root,
                backend_commit=backend_commit,
                published_at="2026-08-31T12:00:00Z",
            )
            after = _json(root / "manifest.json")

            self.assertEqual(published["contract_revision"], "0.8.0-rc.1")
            self.assertEqual(after["publication_status"], "published")
            self.assertEqual(after["backend_commit"], backend_commit)
            self.assertEqual(after["published_at"], "2026-08-31T12:00:00Z")
            for key in ("publication_status", "backend_commit", "published_at"):
                before[key] = after[key]
            self.assertEqual(before, after)

    def test_publish_rejects_missing_implementation_without_manifest_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
            )
            root = repository / "contracts" / "product-api" / "v0.8.0-rc.1"
            TOOL.refresh_contract(root)
            missing = "tools/product_attendee_security_preview.py"
            _copy_publication_implementation(repository, excluded={missing})
            backend_commit = _commit_all(
                repository,
                "Record artifacts without complete Phase 9A implementation",
            )
            manifest_path = root / "manifest.json"
            before = manifest_path.read_bytes()

            with self.assertRaisesRegex(
                TOOL._CORE.ContractError,
                "does not contain the exact Phase 9A implementation",
            ):
                TOOL.publish_contract(
                    root,
                    backend_commit=backend_commit,
                    published_at="2026-08-31T12:00:00Z",
                )

            self.assertEqual(manifest_path.read_bytes(), before)

    def test_tool_import_is_runtime_inert(self) -> None:
        code = f"""
import importlib.util
import pathlib
import threading
before = [(thread.ident, thread.name) for thread in threading.enumerate()]
path = pathlib.Path({str(TOOL_PATH)!r})
spec = importlib.util.spec_from_file_location("attendee_security_contract_inert", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
after = [(thread.ident, thread.name) for thread in threading.enumerate()]
assert after == before
assert module.CONTRACT_REVISION == "0.8.0-rc.1"
assert len(module.EXPECTED_OPERATIONS) == 25
"""
        subprocess.run([sys.executable, "-c", code], check=True)


def _without_operation_extensions(path_item: dict) -> dict:
    return {
        method: {
            key: value
            for key, value in operation.items()
            if not key.startswith("x-dreamland-")
        }
        for method, operation in path_item.items()
    }


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} is not an object")
    return value


def _copy_publication_implementation(
    repository: Path,
    *,
    excluded: set[str] | None = None,
) -> None:
    excluded = set() if excluded is None else excluded
    for relative in TOOL.PUBLICATION_IMPLEMENTATION_FILES - excluded:
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
            "user.email=contract-test@dreamland.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--quiet",
            "-m",
            message,
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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


def _contract_identity_without_publication(root: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    files = tuple(
        sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    )
    for path in files:
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        if relative == "manifest.json":
            manifest = _json(path)
            manifest["publication_status"] = "release_candidate"
            manifest["backend_commit"] = None
            manifest["published_at"] = None
            content = (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content)
    return len(files), digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
