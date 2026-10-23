import copy
import importlib.util
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.2.0-rc.2"
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_lifecycle_contract.py"


def _load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


CONTRACT_TOOL = _load_module("dreamland_product_lifecycle_contract", TOOL_PATH)


class ProductLifecycleContractTests(unittest.TestCase):
    def _fixture(self, relative: str):
        return CONTRACT_TOOL.load_json(CONTRACT_ROOT / "fixtures" / relative)

    def _generated_client(self, destination: Path):
        result = CONTRACT_TOOL.client_smoke(CONTRACT_ROOT, output=destination)
        return _load_module("generated_product_lifecycle_client", destination), result

    def test_contract_validates_exact_unfrozen_preview(self):
        result = CONTRACT_TOOL.validate_contract(CONTRACT_ROOT)

        self.assertEqual(result["contract_revision"], "0.2.0-rc.2")
        self.assertEqual(result["implementation_status"], "integration_preview_only")
        self.assertIn(
            result["publication_status"],
            {"release_candidate", "published"},
        )
        self.assertEqual(result["routes"], 5)
        self.assertEqual(result["capabilities"], 5)
        self.assertEqual(result["fixtures"], len(CONTRACT_TOOL.FIXTURE_SPECS))

    def test_openapi_surface_security_and_lifecycle_only_shape_are_exact(self):
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")

        self.assertEqual(openapi["servers"], [])
        self.assertEqual(set(openapi["paths"]), set(CONTRACT_TOOL.EXPECTED_OPERATIONS))
        for path, (method, operation_id) in CONTRACT_TOOL.EXPECTED_OPERATIONS.items():
            operation = openapi["paths"][path][method]
            self.assertEqual(operation["operationId"], operation_id)
            self.assertEqual(
                operation["x-dreamland-availability"], "integration-preview-only"
            )
            self.assertEqual(operation["security"], [{"ProductServiceBearer": []}])

        schemas = openapi["components"]["schemas"]
        snapshot = schemas["LifecycleSnapshot"]
        self.assertIn("launch_context", snapshot["required"])
        self.assertIn("available_controls", snapshot["required"])
        self.assertFalse(
            CONTRACT_TOOL.FORBIDDEN_SNAPSHOT_FIELDS.intersection(snapshot["properties"])
        )
        self.assertNotIn("session_capability", snapshot["properties"])
        self.assertEqual(
            schemas["LifecyclePreviewSnapshotResponse"]["properties"]["snapshot"],
            {"$ref": "#/components/schemas/ActiveLifecycleSnapshot"},
        )
        self.assertTrue(schemas["CurrentTerminalResponse"]["additionalProperties"])
        self.assertNotIn(
            "session_capability",
            schemas["CurrentTerminalResponse"]["properties"],
        )
        expected_control_schema = {
            "maximum": CONTRACT_TOOL.MAX_CONTROL_REVISION,
            "minimum": 1,
            "type": "integer",
        }
        self.assertEqual(
            snapshot["properties"]["control_revision"], expected_control_schema
        )
        self.assertEqual(
            schemas["CloseLifecyclePreviewRequest"]["properties"][
                "expected_control_revision"
            ],
            expected_control_schema,
        )
        date_time_schemas = [
            item
            for item in CONTRACT_TOOL._walk(openapi)
            if isinstance(item, dict) and item.get("format") == "date-time"
        ]
        self.assertEqual(len(date_time_schemas), 6)
        self.assertTrue(
            all(
                item.get("type") == "string" and item.get("maxLength") == 64
                for item in date_time_schemas
            )
        )

        manifest_schema = CONTRACT_TOOL.load_json(
            CONTRACT_ROOT / "manifest.schema.json"
        )
        manifest_date_times = [
            item
            for item in CONTRACT_TOOL._walk(manifest_schema)
            if isinstance(item, dict) and item.get("format") == "date-time"
        ]
        self.assertEqual(len(manifest_date_times), 2)
        self.assertTrue(
            all(item.get("maxLength") == 64 for item in manifest_date_times)
        )
        unbounded_manifest_schema = copy.deepcopy(manifest_schema)
        del unbounded_manifest_schema["allOf"][1]["then"]["properties"]["published_at"][
            "maxLength"
        ]
        with self.assertRaises(CONTRACT_TOOL.ContractError):
            CONTRACT_TOOL._validate_manifest_schema_bounds(unbounded_manifest_schema)

    def test_idempotency_claim_policy_is_exact_and_enforced(self):
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")
        policy = openapi["x-dreamland-idempotency-policy"]

        self.assertEqual(policy, CONTRACT_TOOL.EXPECTED_IDEMPOTENCY_POLICY)
        for (
            operation_id,
            expected,
        ) in CONTRACT_TOOL.EXPECTED_OPERATION_IDEMPOTENCY.items():
            route, method = next(
                (route, method)
                for route, (method, candidate) in (
                    CONTRACT_TOOL.EXPECTED_OPERATIONS.items()
                )
                if candidate == operation_id
            )
            self.assertEqual(
                openapi["paths"][route][method]["x-dreamland-idempotency"],
                expected,
            )

        deterministic = {
            item["category"]: item["wire_codes"]
            for item in policy["claim_boundary"]["claimed"]["deterministic_rejections"]
        }
        self.assertEqual(
            deterministic["launch_grant_rejected"],
            ["authority_denied"],
        )
        self.assertEqual(
            deterministic["current_session_already_exists"],
            ["current_session_exists"],
        )
        self.assertEqual(
            deterministic["session_reference_or_capability_rejected"],
            ["authority_denied"],
        )
        self.assertEqual(
            deterministic["control_revision_changed"],
            ["control_revision_conflict"],
        )
        self.assertEqual(
            deterministic["key_already_bound_to_another_body"],
            ["idempotency_conflict"],
        )
        self.assertEqual(
            policy["claim_boundary"]["transient_unclaimed"][0]["wire_codes"],
            ["capacity_exhausted"],
        )
        self.assertEqual(
            policy["retention"]["unrelated_claimed_failures"]["ttl_configuration"],
            "failed_operation_ttl_ms",
        )
        self.assertFalse(
            policy["epoch_namespace"]["stale_mutation_claims_current_epoch_key"]
        )
        self.assertEqual(
            policy["claim_boundary"]["claimed"]["same_body"],
            "replay_recorded_logical_outcome_and_status",
        )
        self.assertEqual(
            policy["claim_boundary"]["claimed"]["fresh_metadata_may_differ"],
            ["observed_at", "replayed", "request_id"],
        )
        schemas = openapi["components"]["schemas"]
        self.assertEqual(
            policy["claim_boundary"]["claimed"]["successful_results"],
            sorted(
                (
                    schemas["CloseLifecyclePreviewResponse"]["properties"]["result"][
                        "const"
                    ],
                    schemas["CreateLifecyclePreviewResponse"]["properties"]["result"][
                        "const"
                    ],
                )
            ),
        )
        self.assertEqual(
            policy["scope"]["request_authority_storage"]["comparison_material"],
            "sha256_digest",
        )
        self.assertEqual(
            policy["wire_code_resolution"]["grant_unavailable"],
            "declared_safe_error_code_not_emitted_by_rc2_mutations",
        )

        mutated = copy.deepcopy(openapi)
        mutated["x-dreamland-idempotency-policy"]["claim_boundary"][
            "transient_unclaimed"
        ][0]["key_binding"] = "claimed"
        with self.assertRaisesRegex(
            CONTRACT_TOOL.ContractError,
            "idempotency claim policy is not exact",
        ):
            CONTRACT_TOOL._validate_openapi(mutated, manifest)

    def test_all_capabilities_remain_disabled_without_enabled_revision(self):
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")
        response = self._fixture("canonical-valid/capabilities/all-disabled.json")

        for projection in (manifest["capabilities"], response["capabilities"]):
            self.assertEqual(set(projection), set(CONTRACT_TOOL.CAPABILITY_NAMES))
            for state in projection.values():
                self.assertFalse(state["enabled"])
                self.assertIsNone(state["enabled_revision"])
                self.assertEqual(state["declared_revision"], "0.2.0-rc.2")

    def test_openapi_rejects_weakened_or_expanded_surface(self):
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")
        mutations = []

        live_server = copy.deepcopy(openapi)
        live_server["servers"] = [{"url": "https://product.example"}]
        mutations.append(live_server)
        extra_route = copy.deepcopy(openapi)
        extra_route["paths"]["/product/v1/sessions/{reference}/commands"] = {}
        mutations.append(extra_route)
        missing_bearer = copy.deepcopy(openapi)
        missing_bearer["paths"]["/product/v1/sessions"]["post"]["security"] = []
        mutations.append(missing_bearer)
        missing_epoch = copy.deepcopy(openapi)
        parameters = missing_epoch["paths"]["/product/v1/sessions"]["post"][
            "parameters"
        ]
        parameters.pop(0)
        mutations.append(missing_epoch)
        terminal_authority = copy.deepcopy(openapi)
        terminal_authority["components"]["schemas"]["CurrentTerminalResponse"][
            "properties"
        ]["session_capability"] = {"$ref": "#/components/schemas/SessionCapability"}
        mutations.append(terminal_authority)
        broad_snapshot = copy.deepcopy(openapi)
        broad_snapshot["components"]["schemas"]["LifecyclePreviewSnapshotResponse"][
            "properties"
        ]["snapshot"] = {"$ref": "#/components/schemas/LifecycleSnapshot"}
        mutations.append(broad_snapshot)
        unbounded_timestamp = copy.deepcopy(openapi)
        del unbounded_timestamp["components"]["schemas"]["LifecycleSnapshot"][
            "properties"
        ]["observed_at"]["maxLength"]
        mutations.append(unbounded_timestamp)
        unbounded_control_revision = copy.deepcopy(openapi)
        del unbounded_control_revision["components"]["schemas"][
            "CloseLifecyclePreviewRequest"
        ]["properties"]["expected_control_revision"]["maximum"]
        mutations.append(unbounded_control_revision)

        for mutated in mutations:
            with self.subTest(paths=len(mutated["paths"])):
                with self.assertRaises(CONTRACT_TOOL.ContractError):
                    CONTRACT_TOOL._validate_openapi(mutated, manifest)

    def test_openapi_rejects_identity_and_authority_schema_drift(self):
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")

        for schema_name in CONTRACT_TOOL.EXPECTED_IDENTITY_AUTHORITY_SCHEMAS:
            for field, replacement in (("pattern", r"^.*$"), ("maxLength", 4096)):
                with self.subTest(schema=schema_name, field=field):
                    mutated = copy.deepcopy(openapi)
                    mutated["components"]["schemas"][schema_name][field] = replacement
                    with self.assertRaisesRegex(
                        CONTRACT_TOOL.ContractError,
                        "identity/authority schema is not canonical",
                    ):
                        CONTRACT_TOOL._validate_openapi(mutated, manifest)

    def test_openapi_rejects_request_and_response_envelope_drift(self):
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")

        for schema_name in CONTRACT_TOOL.EXPECTED_REQUEST_SCHEMAS:
            for mutation in ("additionalProperties", "required"):
                with self.subTest(schema=schema_name, mutation=mutation):
                    mutated = copy.deepcopy(openapi)
                    schema = mutated["components"]["schemas"][schema_name]
                    if mutation == "additionalProperties":
                        schema["additionalProperties"] = True
                    else:
                        schema["required"].pop()
                    with self.assertRaisesRegex(
                        CONTRACT_TOOL.ContractError,
                        "request schema is not canonical",
                    ):
                        CONTRACT_TOOL._validate_openapi(mutated, manifest)

        for schema_name in CONTRACT_TOOL.EXPECTED_RESPONSE_OBJECT_SHAPES:
            for mutation in ("additionalProperties", "required"):
                with self.subTest(schema=schema_name, mutation=mutation):
                    mutated = copy.deepcopy(openapi)
                    schema = mutated["components"]["schemas"][schema_name]
                    if mutation == "additionalProperties":
                        schema["additionalProperties"] = not schema[
                            "additionalProperties"
                        ]
                    else:
                        schema["required"].pop()
                    with self.assertRaisesRegex(
                        CONTRACT_TOOL.ContractError,
                        "response required/additionalProperties shape is not exact",
                    ):
                        CONTRACT_TOOL._validate_openapi(mutated, manifest)

    def test_openapi_rejects_duplicate_parameters_and_alternate_media_types(self):
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")

        duplicate_parameter = copy.deepcopy(openapi)
        create_parameters = duplicate_parameter["paths"]["/product/v1/sessions"][
            "post"
        ]["parameters"]
        create_parameters.insert(2, copy.deepcopy(create_parameters[1]))

        inline_parameter = copy.deepcopy(openapi)
        inline_parameter["paths"]["/product/v1/sessions"]["post"]["parameters"][1] = (
            copy.deepcopy(inline_parameter["components"]["parameters"]["ActorContext"])
        )

        alternate_request_media = copy.deepcopy(openapi)
        request_content = alternate_request_media["paths"]["/product/v1/sessions"][
            "post"
        ]["requestBody"]["content"]
        request_content["text/html"] = copy.deepcopy(
            request_content["application/json"]
        )

        alternate_success_media = copy.deepcopy(openapi)
        success_content = alternate_success_media["paths"]["/product/v1/sessions"][
            "post"
        ]["responses"]["201"]["content"]
        success_content["text/html"] = copy.deepcopy(
            success_content["application/json"]
        )

        alternate_error_media = copy.deepcopy(openapi)
        error_content = alternate_error_media["components"]["responses"]["SafeError"][
            "content"
        ]
        error_content["application/json"] = copy.deepcopy(
            error_content["application/problem+json"]
        )

        mutations = {
            "duplicate_parameter": duplicate_parameter,
            "inline_parameter": inline_parameter,
            "alternate_request_media": alternate_request_media,
            "alternate_success_media": alternate_success_media,
            "alternate_error_media": alternate_error_media,
        }
        for case, mutated in mutations.items():
            with self.subTest(case=case):
                with self.assertRaises(CONTRACT_TOOL.ContractError):
                    CONTRACT_TOOL._validate_openapi(mutated, manifest)

    def test_openapi_semantic_pin_rejects_remaining_client_adapter_drift(self):
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")
        mutations = {}

        for schema_name in ("SessionId", "SelectedRecordId", "ImmutableRevision"):
            mutated = copy.deepcopy(openapi)
            mutated["components"]["schemas"][schema_name]["pattern"] = r"^.*$"
            mutations[f"weakened_{schema_name}"] = mutated

        snapshot_session_id = copy.deepcopy(openapi)
        snapshot_session_id["components"]["schemas"]["LifecycleSnapshot"]["properties"][
            "session_id"
        ] = {"type": "string"}
        mutations["generic_snapshot_session_id"] = snapshot_session_id

        create_capability = copy.deepcopy(openapi)
        create_capability["components"]["schemas"]["CreateLifecyclePreviewResponse"][
            "properties"
        ]["session_capability"] = {"type": "string"}
        mutations["generic_create_session_capability"] = create_capability

        for schema_name in ("SafeError", "ProductCapabilitiesResponse"):
            mutated = copy.deepcopy(openapi)
            mutated["components"]["schemas"][schema_name]["properties"][
                "request_id"
            ] = {"type": "string"}
            mutations[f"weakened_{schema_name}_request_id"] = mutated

        starting_world = copy.deepcopy(openapi)
        starting_world["components"]["schemas"]["StartingWorld"][
            "additionalProperties"
        ] = not starting_world["components"]["schemas"]["StartingWorld"][
            "additionalProperties"
        ]
        mutations["changed_starting_world_additive_policy"] = starting_world

        missing_epoch = copy.deepcopy(openapi)
        missing_epoch["components"]["schemas"]["LifecycleSnapshot"]["required"].remove(
            "service_epoch"
        )
        mutations["snapshot_without_service_epoch"] = missing_epoch

        generic_active_lifecycle = copy.deepcopy(openapi)
        generic_active_lifecycle["components"]["schemas"]["ActiveLifecycleSnapshot"][
            "allOf"
        ][1]["properties"]["lifecycle"] = {"type": "object"}
        mutations["generic_active_lifecycle"] = generic_active_lifecycle

        for case, mutated in mutations.items():
            with self.subTest(case=case):
                with self.assertRaisesRegex(
                    CONTRACT_TOOL.ContractError,
                    "semantic shape differs",
                ):
                    CONTRACT_TOOL._validate_openapi(mutated, manifest)

    def test_refresh_cannot_bless_noncanonical_openapi(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            copied = Path(tmpdir) / "contract"
            shutil.copytree(CONTRACT_ROOT, copied)
            openapi_path = copied / "openapi.json"
            openapi = CONTRACT_TOOL.load_json(openapi_path)
            openapi["components"]["schemas"]["CreateLifecyclePreviewRequest"][
                "additionalProperties"
            ] = True
            CONTRACT_TOOL.write_json(openapi_path, openapi)

            with self.assertRaisesRegex(
                CONTRACT_TOOL.ContractError,
                "request schema is not canonical",
            ):
                CONTRACT_TOOL.refresh_contract(copied)

        with tempfile.TemporaryDirectory() as tmpdir:
            copied = Path(tmpdir) / "contract"
            shutil.copytree(CONTRACT_ROOT, copied)
            openapi_path = copied / "openapi.json"
            openapi = CONTRACT_TOOL.load_json(openapi_path)
            openapi["components"]["schemas"]["SessionId"]["pattern"] = r"^.*$"
            CONTRACT_TOOL.write_json(openapi_path, openapi)

            with self.assertRaisesRegex(
                CONTRACT_TOOL.ContractError,
                "semantic shape differs",
            ):
                CONTRACT_TOOL.refresh_contract(copied)

    def test_fixture_classes_cover_lifecycle_handoff_cases(self):
        index = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "fixtures" / "index.json")
        entries = index["fixtures"]
        classes = {entry["class"] for entry in entries}
        cases = {entry["case"] for entry in entries}

        self.assertEqual(classes, {"canonical-valid", "forward-compat", "invalid"})
        required_cases = {
            "authority_actor_alpha",
            "authority_actor_bravo",
            "simulator_secondary_alpha_grant",
            "simulator_epoch_loss_current_alpha",
            "cross_actor_grant_denied",
            "cross_actor_session_capability_denied",
            "create_success",
            "create_replay",
            "current_none",
            "current_active",
            "current_authority_denied",
            "current_terminal_closed",
            "current_terminal_expired",
            "close_success",
            "close_replay",
            "close_excessive_control_revision",
            "safe_error_current_session_exists",
            "safe_error_idempotency_conflict",
            "safe_error_control_revision_conflict",
            "safe_error_session_lost",
            "safe_error_outcome_unknown",
            "snapshot_additive_fields",
            "unknown_safe_error_code",
            "offering_id_is_not_grant",
            "selection_id_is_not_grant",
            "unknown_lifecycle_state",
            "snapshot_must_not_contain_authority",
            "snapshot_excessive_control_revision",
        }
        self.assertTrue(required_cases.issubset(cases))

        simulator_grant = self._fixture(
            "canonical-valid/authority/simulator-secondary-alpha-grant.json"
        )
        self.assertEqual(
            simulator_grant["fixture_scope"],
            "integration-preview-simulator-only",
        )
        self.assertNotIn("expected_epoch", simulator_grant)
        self.assertEqual(
            simulator_grant["expected_epoch_source"],
            "getProductCapabilities.service_epoch",
        )
        self.assertEqual(
            simulator_grant["launch_grant"],
            "launch-grant:preview_alpha_grant_00000002",
        )
        create_request = self._fixture("canonical-valid/sessions/create-request.json")
        self.assertNotEqual(
            create_request["launch_grant"], simulator_grant["launch_grant"]
        )
        epoch_loss_current = self._fixture(
            "canonical-valid/authority/simulator-epoch-loss-current-alpha.json"
        )
        self.assertEqual(
            epoch_loss_current["purpose"],
            "exercise-epoch-loss-current-authority",
        )
        self.assertEqual(
            epoch_loss_current["actor_context"],
            "actor:preview_alpha_rotated_0001",
        )
        self.assertEqual(
            epoch_loss_current["launch_grant"],
            "launch-grant:preview_alpha_rotated_00000001",
        )
        self.assertEqual(
            epoch_loss_current["expected_epoch_source"],
            "getProductCapabilities.service_epoch",
        )

    def test_snapshot_policy_rejects_phase2_authority_and_bad_limits(self):
        snapshot_response = self._fixture("canonical-valid/snapshots/active.json")
        snapshot = snapshot_response["snapshot"]
        CONTRACT_TOOL._validate_snapshot_policy(snapshot)

        mutations = []
        with_phase2 = copy.deepcopy(snapshot)
        with_phase2["event_position"] = {"sequence": 1}
        mutations.append(with_phase2)
        with_authority = copy.deepcopy(snapshot)
        with_authority["session_capability"] = "session-cap:private"
        mutations.append(with_authority)
        bad_limits = copy.deepcopy(snapshot)
        bad_limits["launch_context"]["limits"]["idle_ttl_ms"] = 1800001
        mutations.append(bad_limits)
        bad_controls = copy.deepcopy(snapshot)
        bad_controls["available_controls"] = []
        mutations.append(bad_controls)

        for mutated in mutations:
            with self.subTest(fields=sorted(mutated)):
                with self.assertRaises(CONTRACT_TOOL.ContractError):
                    CONTRACT_TOOL._validate_snapshot_policy(mutated)

    def test_safe_errors_are_allowlisted_but_unknown_consumer_code_fails_closed(self):
        canonical = self._fixture("canonical-valid/errors/session-lost.json")
        future = self._fixture("forward-compat/errors/unknown-code.json")

        self.assertEqual(CONTRACT_TOOL._validate_safe_error(canonical)["retry"], "none")
        normalized = CONTRACT_TOOL._validate_safe_error(future, allow_unknown=True)
        self.assertEqual(normalized["code"], "unknown_safe_error")
        self.assertEqual(normalized["retry"], "none")

    def test_refresh_is_deterministic_and_invalidates_publication(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            copied = Path(tmpdir) / "contract"
            shutil.copytree(CONTRACT_ROOT, copied)
            manifest_path = copied / "manifest.json"
            manifest = CONTRACT_TOOL.load_json(manifest_path)
            manifest["publication_status"] = "published"
            manifest["backend_commit"] = "a" * 40
            manifest["published_at"] = "2026-08-22T12:00:00Z"
            CONTRACT_TOOL.write_json(manifest_path, manifest)

            CONTRACT_TOOL.refresh_contract(copied)
            first = {
                path.relative_to(copied).as_posix(): path.read_bytes()
                for path in copied.rglob("*.json")
            }
            CONTRACT_TOOL.refresh_contract(copied)
            second = {
                path.relative_to(copied).as_posix(): path.read_bytes()
                for path in copied.rglob("*.json")
            }
            refreshed = CONTRACT_TOOL.load_json(manifest_path)

        self.assertEqual(first, second)
        self.assertEqual(refreshed["publication_status"], "release_candidate")
        self.assertIsNone(refreshed["backend_commit"])
        self.assertIsNone(refreshed["published_at"])

    def test_publish_requires_and_records_exact_committed_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = Path(tmpdir) / "repository"
            repository.mkdir()
            copied = repository / "contract"
            shutil.copytree(CONTRACT_ROOT, copied)
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=Lifecycle Contract Test",
                    "-c",
                    "user.email=lifecycle-contract@dreamland.invalid",
                    "commit",
                    "-qm",
                    "lifecycle artifact",
                ],
                check=True,
            )
            commit = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()

            result = CONTRACT_TOOL.publish_contract(
                copied,
                backend_commit=commit,
                published_at="2026-08-22T12:00:00Z",
            )
            published = CONTRACT_TOOL.load_json(copied / "manifest.json")

        self.assertEqual(result["publication_status"], "published")
        self.assertEqual(published["backend_commit"], commit)
        self.assertEqual(published["published_at"], "2026-08-22T12:00:00Z")

    def test_publish_rejects_artifacts_changed_after_the_named_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = Path(tmpdir) / "repository"
            repository.mkdir()
            copied = repository / "contract"
            shutil.copytree(CONTRACT_ROOT, copied)
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=Lifecycle Contract Test",
                    "-c",
                    "user.email=lifecycle-contract@dreamland.invalid",
                    "commit",
                    "-qm",
                    "lifecycle artifact",
                ],
                check=True,
            )
            commit = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            fixture_path = (
                copied / "fixtures" / "canonical-valid" / "current" / "none.json"
            )
            fixture = CONTRACT_TOOL.load_json(fixture_path)
            fixture["safe_additive_label"] = "changed-after-commit"
            CONTRACT_TOOL.write_json(fixture_path, fixture)
            CONTRACT_TOOL.refresh_contract(copied)

            with self.assertRaisesRegex(
                CONTRACT_TOOL.ContractError,
                "does not contain the exact lifecycle artifact",
            ):
                CONTRACT_TOOL.publish_contract(
                    copied,
                    backend_commit=commit,
                    published_at="2026-08-22T12:00:00Z",
                )

    def test_generated_client_compiles_validates_and_accepts_additive_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generated, result = self._generated_client(
                Path(tmpdir) / "product_lifecycle_client.py"
            )
            capabilities = self._fixture(
                "forward-compat/capabilities/additive-fields.json"
            )
            snapshot = self._fixture("forward-compat/snapshots/additive-fields.json")

            self.assertIs(
                generated.validate_product_capabilities(capabilities), capabilities
            )
            self.assertIs(
                generated.validate_lifecycle_snapshot(
                    snapshot["snapshot"], require_active=True
                ),
                snapshot["snapshot"],
            )
            normalized = generated.normalize_safe_error(
                self._fixture("forward-compat/errors/unknown-code.json")
            )

        self.assertEqual(result["contract_revision"], "0.2.0-rc.2")
        self.assertEqual(result["operations"], 5)
        self.assertTrue(result["server_side_only"])
        self.assertEqual(generated.SAFE_ERRORS, CONTRACT_TOOL.SAFE_ERRORS)
        self.assertEqual(normalized["code"], "unknown_safe_error")
        self.assertEqual(normalized["retry"], "none")

    def test_generated_client_rejects_malformed_snapshot_coordinates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generated, _ = self._generated_client(
                Path(tmpdir) / "product_lifecycle_client.py"
            )
            active = self._fixture("canonical-valid/snapshots/active.json")["snapshot"]
            terminal = self._fixture("canonical-valid/current/terminal-closed.json")[
                "snapshot"
            ]
            mutations = []
            for field, value in (
                ("session_id", "session:short"),
                ("reference", "session-ref:short"),
                ("authority_class", "administrator"),
                ("created_at", "not-a-timestamp"),
                ("created_at", "2026-08-22T12:00:00." + "0" * 50 + "Z"),
                ("control_revision", True),
                (
                    "control_revision",
                    CONTRACT_TOOL.MAX_CONTROL_REVISION + 1,
                ),
            ):
                mutated = copy.deepcopy(active)
                mutated[field] = value
                mutations.append(mutated)
            bad_selected_record = copy.deepcopy(active)
            bad_selected_record["starting_world"]["selected_record_id"] = "selected:bad"
            mutations.append(bad_selected_record)
            bad_revision = copy.deepcopy(active)
            bad_revision["starting_world"]["immutable_revision"] = "mutable:bad"
            mutations.append(bad_revision)
            bad_terminal_time = copy.deepcopy(terminal)
            bad_terminal_time["lifecycle"]["changed_at"] = "2026-99-99T00:00:00Z"
            mutations.append(bad_terminal_time)
            bad_terminal_reason = copy.deepcopy(terminal)
            bad_terminal_reason["lifecycle"]["reason_code"] = "provider_failed"
            mutations.append(bad_terminal_reason)
            hostile_terminal_message = copy.deepcopy(terminal)
            hostile_terminal_message["lifecycle"]["safe_message"] = (
                "Session closed.\n<script>"
            )
            mutations.append(hostile_terminal_message)

            for mutated in mutations:
                with self.subTest(
                    session_id=mutated.get("session_id"),
                    lifecycle=mutated.get("lifecycle"),
                ):
                    with self.assertRaises(ValueError):
                        generated.validate_lifecycle_snapshot(mutated)

            client = generated.ProductLifecycleClient(
                "http://127.0.0.1:9", "private-server-side-bearer"
            )
            for revision in (True, CONTRACT_TOOL.MAX_CONTROL_REVISION + 1):
                with self.subTest(close_revision=revision):
                    with self.assertRaises(ValueError):
                        client.close_lifecycle_preview(
                            "epoch:phase1preview0001",
                            "actor:preview_alpha_0001",
                            "session-ref:preview_alpha_0001",
                            "session-cap:preview_alpha_session_cap_000001",
                            "idem:close:preview-alpha:0001",
                            revision,
                        )

    def test_generated_client_rejects_malformed_safe_error_coordinates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generated, _ = self._generated_client(
                Path(tmpdir) / "product_lifecycle_client.py"
            )
            safe_error = self._fixture("canonical-valid/errors/internal-error.json")
            mutations = []
            for field, value in (
                ("code", "Unsafe-Code"),
                ("message", "x" * 161),
                ("message", "Please retry.\u001b[31m"),
                ("request_id", "request:short"),
                ("retry", "eventually"),
                ("retry_after_ms", 1),
            ):
                mutated = copy.deepcopy(safe_error)
                mutated["error"][field] = value
                mutations.append(mutated)
            invalid_delayed = copy.deepcopy(safe_error)
            invalid_delayed["error"]["retry"] = "after_delay"
            invalid_delayed["error"]["retry_after_ms"] = True
            mutations.append(invalid_delayed)

            for mutated in mutations:
                with self.subTest(error=mutated["error"]):
                    with self.assertRaises(ValueError):
                        generated.normalize_safe_error(mutated)

    def test_generated_client_accepts_every_canonical_live_error_tuple(self):
        error_status = {
            "unauthorized": 401,
            "not_found": 404,
            "grant_unavailable": 410,
            "current_session_exists": 409,
            "idempotency_conflict": 409,
            "control_revision_conflict": 409,
            "session_lost": 409,
            "outcome_unknown": 409,
            "invalid_request": 400,
            "invalid_json": 400,
            "request_too_large": 413,
            "unsupported_media_type": 415,
            "expected_epoch_required": 400,
            "authority_denied": 403,
            "content_length_required": 411,
            "capacity_exhausted": 503,
            "internal_error": 500,
        }
        response_state: dict[str, object] = {}
        error_accept_headers: list[str | None] = []

        class ErrorHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                error_accept_headers.append(self.headers.get("Accept"))
                encoded = json.dumps(
                    response_state["payload"], separators=(",", ":")
                ).encode()
                self.send_response(int(response_state["status"]))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "application/problem+json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ErrorHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                generated, _ = self._generated_client(
                    Path(tmpdir) / "product_lifecycle_client.py"
                )
                client = generated.ProductLifecycleClient(
                    f"http://127.0.0.1:{server.server_address[1]}",
                    "private-server-side-bearer",
                )
                for code, status in error_status.items():
                    response_state["payload"] = self._fixture(
                        f"canonical-valid/errors/{code.replace('_', '-')}.json"
                    )
                    response_state["status"] = status
                    with self.subTest(code=code, status=status):
                        with self.assertRaises(
                            generated.ProductLifecycleError
                        ) as caught:
                            client._request_json(
                                "GET",
                                "/product/v1/capabilities",
                                success_status=200,
                            )
                        self.assertEqual(caught.exception.status, status)
                        self.assertEqual(caught.exception.error["code"], code)

                response_state["payload"] = self._fixture(
                    "forward-compat/errors/unknown-code.json"
                )
                response_state["status"] = 500
                with self.assertRaises(generated.ProductLifecycleError) as caught:
                    client._request_json(
                        "GET",
                        "/product/v1/capabilities",
                        success_status=200,
                    )
                self.assertEqual(caught.exception.error["code"], "unknown_safe_error")
                self.assertEqual(caught.exception.error["retry"], "none")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertTrue(error_accept_headers)
        self.assertTrue(
            all(
                value == "application/json, application/problem+json"
                for value in error_accept_headers
            )
        )

    def test_generated_client_calls_all_operations_with_private_evidence(self):
        responses = {
            "capabilities": self._fixture(
                "canonical-valid/capabilities/all-disabled.json"
            ),
            "create": self._fixture("canonical-valid/sessions/create-success.json"),
            "current": self._fixture("canonical-valid/current/active.json"),
            "snapshot": self._fixture("canonical-valid/snapshots/active.json"),
            "close": self._fixture("canonical-valid/close/success.json"),
        }
        captured: list[dict[str, object]] = []

        class FixtureHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, status: int, body: dict[str, object]) -> None:
                encoded = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(encoded)

            def _capture(self, body: object = None) -> None:
                captured.append(
                    {
                        "path": self.path,
                        "accept": self.headers.get("Accept"),
                        "authorization": self.headers.get("Authorization"),
                        "epoch": self.headers.get("X-Dreamland-Expected-Epoch"),
                        "actor": self.headers.get("X-Dreamland-Actor-Context"),
                        "session_capability": self.headers.get(
                            "X-Dreamland-Session-Capability"
                        ),
                        "idempotency": self.headers.get("Idempotency-Key"),
                        "body": body,
                    }
                )

            def do_GET(self) -> None:
                self._capture()
                if self.path == "/product/v1/capabilities":
                    self._send(200, responses["capabilities"])
                elif self.path == "/product/v1/sessions/current":
                    self._send(200, responses["current"])
                else:
                    self._send(200, responses["snapshot"])

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                body = json.loads(self.rfile.read(length))
                self._capture(body)
                if self.path == "/product/v1/sessions":
                    self._send(201, responses["create"])
                else:
                    self._send(200, responses["close"])

        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                generated, _ = self._generated_client(
                    Path(tmpdir) / "product_lifecycle_client.py"
                )
                client = generated.ProductLifecycleClient(
                    f"http://127.0.0.1:{server.server_address[1]}",
                    "private-server-side-bearer",
                )
                epoch = "epoch:phase1preview0001"
                actor = "actor:preview_alpha_0001"
                reference = "session-ref:preview_alpha_0001"
                capability = "session-cap:preview_alpha_session_cap_000001"
                client.get_product_capabilities()
                client.create_lifecycle_preview(
                    epoch,
                    actor,
                    "launch-grant:preview_alpha_grant_00000001",
                    "idem:create:preview-alpha:0001",
                )
                client.get_current_lifecycle_preview(epoch, actor)
                client.get_lifecycle_preview_snapshot(
                    epoch, actor, reference, capability
                )
                client.close_lifecycle_preview(
                    epoch,
                    actor,
                    reference,
                    capability,
                    "idem:close:preview-alpha:0001",
                    1,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(len(captured), 5)
        self.assertTrue(
            all(
                item["accept"] == "application/json, application/problem+json"
                for item in captured
            )
        )
        self.assertTrue(
            all(
                item["authorization"] == "Bearer private-server-side-bearer"
                for item in captured
            )
        )
        for item in captured[1:]:
            self.assertEqual(item["epoch"], "epoch:phase1preview0001")
            self.assertEqual(item["actor"], "actor:preview_alpha_0001")
        self.assertIsNone(captured[1]["session_capability"])
        self.assertIsNone(captured[2]["session_capability"])
        self.assertEqual(
            captured[3]["session_capability"],
            "session-cap:preview_alpha_session_cap_000001",
        )
        self.assertEqual(captured[4]["body"], {"expected_control_revision": 1})

    def test_generated_client_disables_redirects_proxies_and_bounds_response(self):
        captured_authorization: list[str | None] = []

        class TargetHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                captured_authorization.append(self.headers.get("Authorization"))
                self.send_response(204)
                self.end_headers()

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{target.server_address[1]}/capture",
                )
                self.end_headers()

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                generated, _ = self._generated_client(
                    Path(tmpdir) / "product_lifecycle_client.py"
                )
                client = generated.ProductLifecycleClient(
                    f"http://127.0.0.1:{redirect.server_address[1]}",
                    "private-server-side-bearer",
                )
                self.assertEqual(client._proxy_handler.proxies, {})
                with self.assertRaises(HTTPError) as caught:
                    client.get_product_capabilities()
        finally:
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join(timeout=2)
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)

        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(captured_authorization, [])

    def test_duplicate_keys_symlinks_and_runtime_imports_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            duplicate = Path(tmpdir) / "duplicate.json"
            duplicate.write_text('{"value": 1, "value": 2}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                CONTRACT_TOOL.ContractError, "duplicate JSON object key"
            ):
                CONTRACT_TOOL.load_json(duplicate)

            root = Path(tmpdir) / "contract"
            root.mkdir()
            outside = Path(tmpdir) / "outside.json"
            outside.write_text("{}\n", encoding="utf-8")
            (root / "linked.json").symlink_to(outside)
            with self.assertRaises(CONTRACT_TOOL.ContractError):
                CONTRACT_TOOL._contract_path(root, "linked.json")

        source = TOOL_PATH.read_text(encoding="utf-8")
        self.assertNotIn("from dreamland", source)
        self.assertNotIn("import dreamland", source)
        self.assertNotIn("product_contract.py", source)

    def test_phase0_artifacts_and_tool_are_not_part_of_the_preview_kit(self):
        self.assertFalse((CONTRACT_ROOT / "events.schema.json").exists())
        self.assertNotIn(
            "events.schema.json",
            CONTRACT_TOOL._expected_contract_files(),
        )
        self.assertEqual(CONTRACT_TOOL.CONTRACT_REVISION, "0.2.0-rc.2")


if __name__ == "__main__":
    unittest.main()
