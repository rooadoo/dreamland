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
from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_world_access_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "world-access-api" / "v0.1.0-rc.3"
PRODUCT_RC3_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.7.0-rc.3"
PRODUCT_RC4_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.7.0-rc.4"
FORGE_RC4_ROOT = REPOSITORY_ROOT / "contracts" / "world-forge-api" / "v0.1.0-rc.4"
PRODUCT_RC4_TREE = (
    186,
    "49d5d342a3c7ec72a59c4c99128e5332edfaa3374226648a574f1cd1de6a094f",
)
NODE_EXECUTABLE = shutil.which("node")

OPERATION_MATRIX = {
    "/world-access/v1/identity": {
        "method": "get",
        "operation_id": "getWorldAccessIdentity",
        "actor_class": "none",
        "parameter_refs": ("RequestId",),
        "request_schema": None,
        "response_status": "200",
        "response_schema": "IdentityResponse",
        "path_parameter": False,
    },
    "/world-access/v1/promotions": {
        "method": "post",
        "operation_id": "promoteSelectedForgeWorld",
        "actor_class": "operator",
        "parameter_refs": (
            "RequestId",
            "ExpectedEpoch",
            "ActorContext",
            "IdempotencyKey",
        ),
        "request_schema": "PromotionRequest",
        "response_status": "201",
        "response_schema": "PromotionResponse",
        "path_parameter": False,
    },
    "/world-access/v1/promotions/{promotion_id}": {
        "method": "get",
        "operation_id": "getWorldAccessPromotion",
        "actor_class": "operator",
        "parameter_refs": ("RequestId", "ExpectedEpoch", "ActorContext"),
        "request_schema": None,
        "response_status": "200",
        "response_schema": "PromotionResponse",
        "path_parameter": True,
    },
    "/world-access/v1/promotions/{promotion_id}/reveal": {
        "method": "post",
        "operation_id": "revealWorldAccessPromotion",
        "actor_class": "operator",
        "parameter_refs": (
            "RequestId",
            "ExpectedEpoch",
            "ActorContext",
            "IdempotencyKey",
        ),
        "request_schema": "RevealRequest",
        "response_status": "200",
        "response_schema": "PromotionResponse",
        "path_parameter": True,
    },
    "/world-access/v1/catalog": {
        "method": "get",
        "operation_id": "getWorldAccessCatalog",
        "actor_class": "attendee",
        "parameter_refs": ("RequestId", "ExpectedEpoch", "ActorContext"),
        "request_schema": None,
        "response_status": "200",
        "response_schema": "CatalogResponse",
        "path_parameter": False,
    },
    "/world-access/v1/launch-selections": {
        "method": "post",
        "operation_id": "exchangeWorldAccessOffering",
        "actor_class": "attendee",
        "parameter_refs": (
            "RequestId",
            "ExpectedEpoch",
            "ActorContext",
            "IdempotencyKey",
        ),
        "request_schema": "LaunchSelectionRequest",
        "response_status": "200",
        "response_schema": "LaunchSelectionResponse",
        "path_parameter": False,
    },
}


def _load_tool() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "test_product_world_access_contract_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("World Access contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


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


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


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


def _copy_publication_implementation(
    repository: Path, relative_paths: set[str]
) -> None:
    for relative in relative_paths:
        source = REPOSITORY_ROOT / relative
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


class ProductWorldAccessContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _load_tool()

    def test_checked_contract_validates_and_client_smokes(self) -> None:
        validated = self.tool.validate_contract(CONTRACT_ROOT)
        if NODE_EXECUTABLE is None:
            self.skipTest("Node.js is not installed for the generated-client smoke")
        smoked = self.tool.client_smoke(CONTRACT_ROOT)
        index = json.loads((CONTRACT_ROOT / "fixtures/index.json").read_text())

        self.assertTrue(validated["valid"])
        self.assertEqual(validated["contract_revision"], "0.1.0-rc.3")
        self.assertEqual(validated["fixture_count"], len(index["fixtures"]))
        self.assertGreater(validated["fixture_count"], 58)
        self.assertTrue(smoked["smoke"])
        self.assertEqual(smoked["operations"], 6)
        self.assertTrue(smoked["compiled"])
        self.assertTrue(smoked["content_type_mismatch_rejected"])
        self.assertTrue(smoked["launch_binding_sha256_consumed"])
        self.assertTrue(smoked["launch_selection_fields_consumed"])
        self.assertTrue(smoked["missing_content_type_rejected"])
        self.assertEqual(smoked["transport"], "bounded_fake_fetch")
        self.assertEqual(smoked["typescript"], "6.0.3")
        self.assertGreaterEqual(smoked["node_major"], 20)

    def test_refresh_is_deterministic_and_matches_checked_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as first_directory:
            with tempfile.TemporaryDirectory() as second_directory:
                first = Path(first_directory) / "contract"
                second = Path(second_directory) / "contract"
                self.tool.refresh_contract(first)
                self.tool.refresh_contract(second)
                self.assertEqual(_files(first), _files(second))
                self.assertEqual(
                    _draft_normalized_files(first),
                    _draft_normalized_files(CONTRACT_ROOT),
                )

    def test_validation_rejects_self_rehashed_noncanonical_artifacts(self) -> None:
        cases = ("client", "fixture", "manifest-schema", "manifest")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "contract"
                shutil.copytree(CONTRACT_ROOT, root)
                manifest_path = root / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                if case == "client":
                    path = root / "generated/world_access_client.ts"
                    path.write_text(path.read_text() + "// noncanonical\n")
                    manifest["artifacts"]["generated_client_sha256"] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    _write_json(manifest_path, manifest)
                elif case == "fixture":
                    index_path = root / "fixtures/index.json"
                    index = json.loads(index_path.read_text())
                    entry = next(
                        item for item in index["fixtures"] if item["case"] == "identity"
                    )
                    path = root / entry["path"]
                    payload = json.loads(path.read_text())
                    payload["future_safe_field"] = True
                    _write_json(path, payload)
                    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                    _write_json(index_path, index)
                    manifest["fixture_set"]["index_sha256"] = hashlib.sha256(
                        index_path.read_bytes()
                    ).hexdigest()
                    _write_json(manifest_path, manifest)
                elif case == "manifest-schema":
                    path = root / "manifest.schema.json"
                    schema = json.loads(path.read_text())
                    schema["$comment"] = "noncanonical"
                    _write_json(path, schema)
                    manifest["artifacts"]["manifest_schema_sha256"] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    _write_json(manifest_path, manifest)
                else:
                    manifest["generation"]["validate_command"] = "changed"
                    _write_json(manifest_path, manifest)

                with self.assertRaisesRegex(
                    self.tool.WorldAccessContractError,
                    "not canonical|evidence changed",
                ):
                    self.tool.validate_contract(root)

    def test_exact_operation_security_actor_parameter_and_schema_matrix(self) -> None:
        openapi = json.loads((CONTRACT_ROOT / "openapi.json").read_text())
        self.assertEqual(set(openapi["paths"]), set(OPERATION_MATRIX))
        self.assertEqual(openapi["security"], [{"PrivateBearer": []}])
        parameters = openapi["components"]["parameters"]
        self.assertFalse(parameters["RequestId"]["required"])
        for name in ("ExpectedEpoch", "ActorContext", "IdempotencyKey"):
            self.assertTrue(parameters[name]["required"])

        path_parameter = {
            "name": "promotion_id",
            "in": "path",
            "required": True,
            "schema": {"$ref": "#/components/schemas/PromotionId"},
        }
        for path, expected in OPERATION_MATRIX.items():
            method = expected["method"]
            path_item = openapi["paths"][path]
            expected_path_keys = {method}
            if expected["path_parameter"]:
                expected_path_keys.add("parameters")
                self.assertEqual(path_item["parameters"], [path_parameter])
            self.assertEqual(set(path_item), expected_path_keys)

            operation = path_item[method]
            self.assertEqual(operation["operationId"], expected["operation_id"])
            self.assertEqual(operation["security"], [{"PrivateBearer": []}])
            self.assertEqual(
                operation["x-dreamland-actor-class"], expected["actor_class"]
            )
            self.assertEqual(
                operation["parameters"],
                [
                    {"$ref": f"#/components/parameters/{name}"}
                    for name in expected["parameter_refs"]
                ],
            )

            request_schema = expected["request_schema"]
            if request_schema is None:
                self.assertNotIn("requestBody", operation)
            else:
                self.assertEqual(
                    operation["requestBody"],
                    {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": f"#/components/schemas/{request_schema}"
                                }
                            }
                        },
                    },
                )

            response_status = expected["response_status"]
            self.assertEqual(set(operation["responses"]), {response_status, "default"})
            self.assertEqual(
                operation["responses"][response_status]["content"],
                {
                    "application/json": {
                        "schema": {
                            "$ref": (
                                "#/components/schemas/" + expected["response_schema"]
                            )
                        }
                    }
                },
            )
            self.assertEqual(
                operation["responses"]["default"]["content"],
                {
                    "application/problem+json": {
                        "schema": {"$ref": "#/components/schemas/SafeErrorEnvelope"}
                    }
                },
            )

    def test_route_and_authority_boundary_is_private_and_non_authoritative(
        self,
    ) -> None:
        openapi = json.loads((CONTRACT_ROOT / "openapi.json").read_text())
        self.assertFalse(openapi["x-dreamland-boundary"]["direct_browser_access"])
        self.assertFalse(openapi["x-dreamland-boundary"]["offering_id_is_authority"])
        self.assertFalse(openapi["x-dreamland-boundary"]["launch_selection_id_exposed"])
        self.assertEqual(
            openapi["x-dreamland-boundary"]["production_capabilities_enabled"],
            [],
        )
        self.assertNotIn("events.schema.json", _files(CONTRACT_ROOT))
        self.assertTrue(
            openapi["x-dreamland-boundary"]["launch_binding_sha256_server_only"]
        )
        self.assertFalse(
            openapi["x-dreamland-boundary"]["launch_binding_sha256_browser_exposed"]
        )

        encoded_catalog = json.dumps(
            openapi["components"]["schemas"]["CatalogOffering"]
        )
        for forbidden in (
            "launch_selection_id",
            "launch_grant",
            "artifact_path",
            "revision_digest",
            "raw_state",
        ):
            self.assertNotIn(forbidden, encoded_catalog)

    def test_launch_selection_correlation_is_required_output_only_lowercase_sha256(
        self,
    ) -> None:
        import jsonschema

        openapi = json.loads((CONTRACT_ROOT / "openapi.json").read_text())
        schemas = openapi["components"]["schemas"]
        request_schema = schemas["LaunchSelectionRequest"]
        response_schema = schemas["LaunchSelectionResponse"]
        correlation = response_schema["properties"]["launch_binding_sha256"]
        self.assertFalse(request_schema["additionalProperties"])
        self.assertNotIn("launch_binding_sha256", request_schema["properties"])
        self.assertFalse(response_schema["additionalProperties"])
        self.assertIn("launch_binding_sha256", response_schema["required"])
        self.assertEqual(correlation["type"], "string")
        self.assertEqual(correlation["pattern"], r"^[a-f0-9]{64}$")
        self.assertTrue(correlation["readOnly"])
        self.assertIn("Product", correlation["description"])
        self.assertIn("server-only", correlation["description"].lower())
        self.assertIn("must not be accepted from requests", correlation["description"])

        index = json.loads((CONTRACT_ROOT / "fixtures/index.json").read_text())
        entries = {entry["case"]: entry for entry in index["fixtures"]}
        valid_cases = {
            "authored-grant": "1" * 64,
            "authored-grant-replay": "1" * 64,
            "generated-grant": "2" * 64,
            "generated-grant-replay": "2" * 64,
        }
        for case, expected in valid_cases.items():
            payload = json.loads((CONTRACT_ROOT / entries[case]["path"]).read_text())
            self.assertEqual(payload["launch_binding_sha256"], expected)

        invalid_cases = {
            "exchange-launch-binding-injection",
            "grant-launch-binding-missing",
            "grant-launch-binding-nonhex",
            "grant-launch-binding-uppercase",
            "grant-launch-binding-wrong-length",
            "grant-launch-binding-object",
        }
        self.assertTrue(invalid_cases.issubset(entries))
        for case in invalid_cases:
            entry = entries[case]
            self.assertEqual(entry["classification"], "invalid")
            payload = json.loads((CONTRACT_ROOT / entry["path"]).read_text())
            target = entry["target"].removeprefix("openapi.json")
            schema = {**openapi, "$ref": target}
            errors = tuple(jsonschema.Draft202012Validator(schema).iter_errors(payload))
            self.assertTrue(errors, f"{case} unexpectedly satisfies its wire schema")

    def test_dependencies_are_exact_published_provider_free_inputs(self) -> None:
        manifest = json.loads((CONTRACT_ROOT / "manifest.json").read_text())
        dependencies = manifest["dependencies"]
        self.assertEqual(
            dependencies["product_api"],
            {
                "contract": "dreamland-private-product-api",
                "revision": "0.7.0-rc.3",
                "manifest_sha256": hashlib.sha256(
                    (PRODUCT_RC3_ROOT / "manifest.json").read_bytes()
                ).hexdigest(),
            },
        )
        self.assertEqual(
            dependencies["world_forge_api"],
            {
                "contract": "dreamland-private-world-forge-api",
                "revision": "0.1.0-rc.4",
                "manifest_sha256": hashlib.sha256(
                    (FORGE_RC4_ROOT / "manifest.json").read_bytes()
                ).hexdigest(),
            },
        )
        self.assertIn(manifest["publication_status"], {"draft", "published"})
        if manifest["publication_status"] == "draft":
            self.assertIsNone(manifest["backend_commit"])
            self.assertIsNone(manifest["published_at"])
        else:
            self.assertRegex(manifest["backend_commit"], r"^[0-9a-f]{40}$")
            self.assertIsInstance(manifest["published_at"], str)
        self.assertFalse(manifest["source"]["provider_calls"])
        self.assertFalse(manifest["source"]["normal_runtime_integration"])
        self.assertEqual(manifest["source"]["production_capabilities_enabled"], [])
        toolchain = manifest["generation"]["client_toolchain"]
        self.assertEqual(toolchain["typescript"], "6.0.3")
        for field, relative in (
            ("package_sha256", "tools/world_access_client_smoke/package.json"),
            (
                "package_lock_sha256",
                "tools/world_access_client_smoke/package-lock.json",
            ),
            ("harness_sha256", "tools/world_access_client_smoke/run-smoke.mjs"),
        ):
            self.assertEqual(
                toolchain[field],
                hashlib.sha256((REPOSITORY_ROOT / relative).read_bytes()).hexdigest(),
            )
        self.assertTrue(
            {
                "tools/product_stage_preview.py",
                "tools/product_launch_selection_preview.py",
                "tools/product_attendee_preview.py",
            }.issubset(self.tool.PUBLICATION_IMPLEMENTATION_FILES)
        )

    def test_every_safe_error_has_http_reachability_and_status(self) -> None:
        index = json.loads((CONTRACT_ROOT / "fixtures/index.json").read_text())
        safe_errors = {
            entry["case"].removeprefix("safe-error-"): entry
            for entry in index["fixtures"]
            if entry["classification"] == "safe-error"
        }
        self.assertEqual(set(safe_errors), set(self.tool.SAFE_ERRORS))
        for code, (status, _message, _retry, _delay) in self.tool.SAFE_ERRORS.items():
            self.assertEqual(safe_errors[code]["reachability"], "http")
            self.assertEqual(safe_errors[code]["http_status"], status)

    def test_structural_invariant_fixtures_are_rejected_by_their_wire_schemas(
        self,
    ) -> None:
        import jsonschema

        openapi = json.loads((CONTRACT_ROOT / "openapi.json").read_text())
        index = json.loads((CONTRACT_ROOT / "fixtures/index.json").read_text())
        entries = {entry["case"]: entry for entry in index["fixtures"]}
        structural_cases = {
            "hidden-promotion-catalog-eligible": (
                "openapi.json#/components/schemas/PromotionResponse",
                {"state": "hidden", "catalog_eligible": True},
            ),
            "revealed-promotion-catalog-ineligible": (
                "openapi.json#/components/schemas/PromotionResponse",
                {"state": "revealed", "catalog_eligible": False},
            ),
            "safe-error-after-delay-without-delay": (
                "openapi.json#/components/schemas/SafeErrorEnvelope",
                {"retry": "after_delay", "retry_after_ms": None},
            ),
            "safe-error-none-with-delay": (
                "openapi.json#/components/schemas/SafeErrorEnvelope",
                {"retry": "none", "retry_after_ms": 250},
            ),
            "safe-error-same-request-with-delay": (
                "openapi.json#/components/schemas/SafeErrorEnvelope",
                {"retry": "same_request", "retry_after_ms": 250},
            ),
        }
        self.assertTrue(set(structural_cases).issubset(entries))
        for case, (target, contradiction) in structural_cases.items():
            entry = entries[case]
            self.assertEqual(entry["classification"], "invalid")
            self.assertEqual(entry["target"], target)
            payload = json.loads((CONTRACT_ROOT / entry["path"]).read_text())
            observed = payload.get("error", payload)
            for key, expected in contradiction.items():
                self.assertEqual(observed[key], expected)
            schema = {**openapi, "$ref": target.removeprefix("openapi.json")}
            errors = tuple(jsonschema.Draft202012Validator(schema).iter_errors(payload))
            self.assertTrue(errors, f"{case} unexpectedly satisfies its wire schema")

    def test_fixtures_cover_lifecycle_generic_catalog_and_recovery(self) -> None:
        index = json.loads((CONTRACT_ROOT / "fixtures/index.json").read_text())
        entries = {entry["case"]: entry for entry in index["fixtures"]}
        self.assertTrue(
            {
                "hidden-promotion",
                "hidden-promotion-replay",
                "hidden-promotion-status",
                "revealed-promotion",
                "revealed-promotion-replay",
                "hidden-generated-offering-omitted",
                "mixed-catalog",
                "authored-exchange-request",
                "generated-exchange-request",
                "generated-grant",
                "generated-grant-replay",
                "empty-catalog",
                "generated-only-catalog",
                "authored-grant",
                "authored-grant-replay",
                "additive-identity",
                "promotion-unknown-state",
                "exchange-launch-binding-injection",
                "grant-launch-binding-missing",
                "grant-launch-binding-nonhex",
                "grant-launch-binding-uppercase",
                "grant-launch-binding-wrong-length",
                "grant-launch-binding-object",
            }.issubset(entries)
        )

        expected_scenarios = {
            "wrong-attendee-actor-for-promotion": {
                "operation_id": "promoteSelectedForgeWorld",
                "request_actor_class": "attendee",
                "required_actor_class": "operator",
                "http_status": 403,
            },
            "wrong-operator-actor-for-catalog": {
                "operation_id": "getWorldAccessCatalog",
                "request_actor_class": "operator",
                "required_actor_class": "attendee",
                "http_status": 403,
            },
            "offering-revision-changed": {
                "operation_id": "exchangeWorldAccessOffering",
                "request_actor_class": "attendee",
                "required_actor_class": "attendee",
                "http_status": 404,
            },
            "offering-expired": {
                "operation_id": "exchangeWorldAccessOffering",
                "request_actor_class": "attendee",
                "required_actor_class": "attendee",
                "http_status": 404,
            },
        }
        self.assertTrue(set(expected_scenarios).issubset(entries))
        for case, expected in expected_scenarios.items():
            entry = entries[case]
            self.assertEqual(entry["classification"], "http-scenario")
            self.assertEqual(entry["reachability"], "http")
            for key, value in expected.items():
                self.assertEqual(entry[key], value)
            payload = json.loads((CONTRACT_ROOT / entry["path"]).read_text())
            self.assertFalse(payload["side_effects_committed"])
            self.assertIn(payload["expected_error_code"], self.tool.SAFE_ERRORS)

    def test_generated_client_is_private_and_requires_epoch_before_actor_calls(
        self,
    ) -> None:
        source = (CONTRACT_ROOT / "generated/world_access_client.ts").read_text()
        self.assertIn("server-only private BFF client", source)
        self.assertIn("WorldAccessLaunchSelectionResponse", source)
        self.assertIn("readonly launch_binding_sha256: string", source)
        for field in (
            "readonly kind:",
            "readonly service_epoch: string",
            "readonly launch_grant: string",
            "readonly grant_expires_at: string",
            "readonly selection: WorldAccessCatalogOffering",
            "readonly replayed: boolean",
            "readonly request_id: string",
            "readonly contract: WorldAccessContractIdentity",
            "readonly preview: WorldAccessPreviewIdentity",
        ):
            self.assertIn(field, source)
        self.assertIn("World Access identity must be resolved first", source)
        self.assertIn('headers["X-Dreamland-Expected-Epoch"]', source)
        self.assertIn('headers["X-Dreamland-Actor-Context"]', source)
        self.assertIn('headers["Idempotency-Key"]', source)
        self.assertIn('Accept: "application/json, application/problem+json"', source)
        self.assertIn("response Content-Type must be", source)
        self.assertIn("requirePreviewLoopbackBaseUrl", source)
        self.assertIn('parsed.protocol !== "http:"', source)
        self.assertIn('parsed.hostname === "127.0.0.1"', source)
        self.assertIn('parsed.hostname === "localhost"', source)
        self.assertNotIn("[::1]", source)
        self.assertIn('parsed.username !== ""', source)
        self.assertIn('parsed.password !== ""', source)
        self.assertIn('parsed.pathname !== "/"', source)
        self.assertIn('parsed.search !== ""', source)
        self.assertIn('parsed.hash !== ""', source)
        self.assertIn("this.baseUrl = requirePreviewLoopbackBaseUrl(baseUrl)", source)
        self.assertIn("this.auth = auth", source)
        self.assertIn("return parsed.origin", source)
        self.assertNotIn("constructor(private readonly baseUrl", source)
        self.assertNotIn("localStorage", source)
        self.assertNotIn("document.cookie", source)

    def test_publish_changes_only_release_evidence(self) -> None:
        if NODE_EXECUTABLE is None:
            self.skipTest("Node.js is not installed for the publication smoke")
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
            )
            root = repository / "contracts" / "world-access-api" / "v0.1.0-rc.3"
            self.tool.refresh_contract(root)
            _copy_publication_implementation(
                repository, self.tool.PUBLICATION_IMPLEMENTATION_FILES
            )
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
                    "Record RC3 implementation artifact",
                ],
                check=True,
            )
            backend_commit = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            before = _files(root)
            published = self.tool.publish_contract(
                root,
                backend_commit=backend_commit,
                published_at="2026-08-30T12:00:00Z",
            )
            after = _files(root)
            before_manifest = json.loads(before.pop("manifest.json"))
            after_manifest = json.loads(after.pop("manifest.json"))

            self.assertEqual(
                published,
                {"contract_revision": "0.1.0-rc.3", "published": True},
            )
            self.assertEqual(before, after)
            for name in ("publication_status", "backend_commit", "published_at"):
                before_manifest[name] = after_manifest[name]
            self.assertEqual(before_manifest, after_manifest)
            self.assertEqual(after_manifest["publication_status"], "published")
            self.assertEqual(after_manifest["backend_commit"], backend_commit)
            self.assertEqual(after_manifest["published_at"], "2026-08-30T12:00:00Z")
            self.assertTrue(self.tool.validate_contract(root)["valid"])
            with self.assertRaises(self.tool.WorldAccessContractError):
                self.tool.publish_contract(
                    root,
                    backend_commit=backend_commit,
                    published_at="2026-08-30T12:01:00Z",
                )

    def test_publish_rejects_reachable_commit_without_exact_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
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
                    "--allow-empty",
                    "-m",
                    "Commit without RC3 artifacts",
                ],
                check=True,
            )
            backend_commit = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            root = repository / "contracts" / "world-access-api" / "v0.1.0-rc.3"
            self.tool.refresh_contract(root)
            before = (root / "manifest.json").read_bytes()

            with self.assertRaisesRegex(
                self.tool.WorldAccessContractError,
                "does not contain the exact World Access artifact",
            ):
                self.tool.publish_contract(
                    root,
                    backend_commit=backend_commit,
                    published_at="2026-08-30T12:00:00Z",
                )

            self.assertEqual((root / "manifest.json").read_bytes(), before)

    def test_publish_rejects_commit_without_exact_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
            root = repository / "contracts" / "world-access-api" / "v0.1.0-rc.3"
            self.tool.refresh_contract(root)
            missing = "tools/world_access_client_smoke/run-smoke.mjs"
            _copy_publication_implementation(
                repository,
                self.tool.PUBLICATION_IMPLEMENTATION_FILES - {missing},
            )
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
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
                    "Record artifact without complete implementation",
                ],
                check=True,
            )
            backend_commit = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with self.assertRaisesRegex(
                self.tool.WorldAccessContractError,
                "does not contain the exact World Access implementation",
            ):
                self.tool.publish_contract(
                    root,
                    backend_commit=backend_commit,
                    published_at="2026-08-30T12:00:00Z",
                )

    def test_validation_rejects_invalid_published_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "contract"
            shutil.copytree(CONTRACT_ROOT, root)
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["publication_status"] = "published"
            manifest["backend_commit"] = "a" * 40
            manifest["published_at"] = "2026-99-99T99:99:99Z"
            _write_json(manifest_path, manifest)

            with self.assertRaisesRegex(
                self.tool.WorldAccessContractError, "published-at must be ISO-8601"
            ):
                self.tool.validate_contract(root)

    def test_cli_validate_and_client_smoke(self) -> None:
        commands = ["validate"]
        if NODE_EXECUTABLE is not None:
            commands.append("client-smoke")
        for command in commands:
            result = subprocess.run(
                [sys.executable, str(TOOL_PATH), command, str(CONTRACT_ROOT)],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["contract_revision"], "0.1.0-rc.3")
        if NODE_EXECUTABLE is None:
            self.skipTest("Node.js is not installed for the generated-client smoke")

    def test_published_product_rc4_tree_is_unchanged(self) -> None:
        self.assertEqual(_tree_identity(PRODUCT_RC4_ROOT), PRODUCT_RC4_TREE)


if __name__ == "__main__":
    unittest.main()
