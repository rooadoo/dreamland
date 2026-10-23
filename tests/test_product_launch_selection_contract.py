from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
from copy import deepcopy
from email.message import Message
from pathlib import Path

from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_launch_selection_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.7.0-rc.1"
RC3_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.6.0-rc.3"
RC3_TREE = (
    140,
    "42809e2b80f3454d77d463253280f0b0b5baeb9fcc1f929aec2ecb415ca6da2b",
)


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_launch_selection_contract_test_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("Presenter launch-selection contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load_tool()


class ProductLaunchSelectionContractTests(unittest.TestCase):
    def test_release_candidate_validates_and_generated_client_smokes(self) -> None:
        validated = TOOL.validate_contract(CONTRACT_ROOT)
        smoked = TOOL.client_smoke(CONTRACT_ROOT)

        self.assertEqual(validated["contract_revision"], "0.7.0-rc.1")
        self.assertEqual(validated["routes"], 21)
        self.assertEqual(validated["capabilities_enabled"], 0)
        self.assertEqual(smoked["validated_client_fixtures"], 16)
        self.assertEqual(smoked["rejected_client_mutations"], 118)

    def test_routes_are_private_presenter_bff_operations(self) -> None:
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        expected = {
            "/product/v1/stage/launch-options": (
                "get",
                "getPresenterStageLaunchOptions",
                {
                    "X-Dreamland-Expected-Epoch",
                    "X-Dreamland-Actor-Context",
                },
            ),
            "/product/v1/stage/launch-selections": (
                "post",
                "exchangePresenterStageLaunchSelection",
                {
                    "X-Dreamland-Expected-Epoch",
                    "X-Dreamland-Actor-Context",
                    "Idempotency-Key",
                },
            ),
        }
        for path, (method, operation_id, headers) in expected.items():
            operation = openapi["paths"][path][method]
            observed_headers = {
                openapi["components"]["parameters"][
                    parameter["$ref"].rsplit("/", 1)[1]
                ]["name"]
                for parameter in operation["parameters"]
            }
            with self.subTest(path=path):
                self.assertEqual(operation["operationId"], operation_id)
                self.assertEqual(operation["security"], [{"ProductServiceBearer": []}])
                self.assertEqual(observed_headers, headers)
                self.assertNotIn("X-Dreamland-Session-Capability", observed_headers)
                self.assertNotIn("X-Dreamland-Forge-Expected-Epoch", observed_headers)

        exchange = openapi["paths"]["/product/v1/stage/launch-selections"]["post"]
        idempotency = exchange["x-dreamland-idempotency"]
        self.assertEqual(
            idempotency["operation_family"],
            "stage.launch_selection.exchange",
        )
        self.assertEqual(
            idempotency["claimed_deterministic_rejections"],
            ["launch_selection_unavailable"],
        )
        self.assertEqual(
            idempotency["unclaimed_retryable_rejections"],
            ["launch_selection_capacity_exhausted"],
        )
        self.assertEqual(idempotency["old_epoch"], "outcome_unknown_without_remint")
        self.assertEqual(
            idempotency["expired_grant_replay"],
            "return_original_expired_grant",
        )

    def test_inherited_rc3_routes_remain_exact(self) -> None:
        previous = _json(RC3_ROOT / "openapi.json")
        current = _json(CONTRACT_ROOT / "openapi.json")
        self.assertEqual(
            set(current["paths"]),
            {
                *previous["paths"],
                "/product/v1/stage/launch-options",
                "/product/v1/stage/launch-selections",
            },
        )
        for path, operation in previous["paths"].items():
            with self.subTest(path=path):
                self.assertEqual(current["paths"][path], operation)

    def test_option_and_request_schemas_are_closed_and_bounded(self) -> None:
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        request_validator = _validator(openapi, "StageLaunchSelectionRequest")
        options_validator = _validator(
            openapi,
            "PresenterStageLaunchOptionsResponse",
        )
        response_validator = _validator(openapi, "StageLaunchSelectionResponse")
        options = _fixture("canonical-valid/launch-selection/options-mixed.json")
        response = _fixture("canonical-valid/launch-selection/authored-fresh.json")

        self.assertTrue(options_validator.is_valid(options))
        self.assertTrue(response_validator.is_valid(response))
        self.assertTrue(
            request_validator.is_valid(
                {"launch_selection_id": options["options"][0]["launch_selection_id"]}
            )
        )
        invalid_requests = (
            {"launch_selection_id": options["options"][0]["offering_id"]},
            {"launch_selection_id": "launch-selection:short"},
            {"launch_selection_id": options["options"][0]["revision"]["sha256"]},
            {
                "launch_selection_id": options["options"][0]["launch_selection_id"],
                "profile_name": "colonyship_a1",
            },
            {
                "launch_selection_id": options["options"][0]["launch_selection_id"],
                "profile_path": "/private/profile.json",
            },
            {
                "launch_selection_id": options["options"][0]["launch_selection_id"],
                "launch_grant": "launch-grant:caller_authority_0000001",
            },
        )
        for request in invalid_requests:
            with self.subTest(request=request):
                self.assertFalse(request_validator.is_valid(request))

        unknown_origin = deepcopy(options)
        unknown_origin["options"][0]["origin"]["kind"] = "future_origin"
        self.assertFalse(options_validator.is_valid(unknown_origin))
        nested_addition = deepcopy(options)
        nested_addition["options"][0]["future"] = "not-safe-by-default"
        self.assertFalse(options_validator.is_valid(nested_addition))
        legacy_revision = deepcopy(options)
        legacy_revision["options"][0]["revision"]["revision_id"] = (
            "world-revision:legacy_revision_identifier_0001"
        )
        self.assertFalse(options_validator.is_valid(legacy_revision))
        legacy_grant = deepcopy(response)
        legacy_grant["launch_grant"] = "launch-grant:legacy_opaque_grant_0000001"
        self.assertFalse(response_validator.is_valid(legacy_grant))
        zero_serial_grant = deepcopy(response)
        zero_serial_grant["launch_grant"] = "launch-grant:0000000000000000_" + "a" * 64
        self.assertFalse(response_validator.is_valid(zero_serial_grant))
        additive_response = deepcopy(options)
        additive_response["future_safe_top_level"] = {"revision": 1}
        self.assertTrue(options_validator.is_valid(additive_response))

    def test_generated_validators_enforce_cross_field_and_keyed_uniqueness(
        self,
    ) -> None:
        generated = _load_generated_client()
        options = _fixture("canonical-valid/launch-selection/options-mixed.json")
        self.assertIs(
            generated.validate_presenter_stage_launch_options(options),
            options,
        )

        mutations = []
        changed = deepcopy(options)
        changed["options"][1]["launch_selection_id"] = changed["options"][0][
            "launch_selection_id"
        ]
        mutations.append(changed)
        changed = deepcopy(options)
        changed["options"][1]["offering_id"] = changed["options"][0]["offering_id"]
        mutations.append(changed)
        changed = deepcopy(options)
        changed["options"][1]["revision"] = deepcopy(changed["options"][0]["revision"])
        changed["options"][1]["origin"] = deepcopy(changed["options"][0]["origin"])
        mutations.append(changed)
        changed = deepcopy(options)
        changed["options"][1]["revision"] = deepcopy(changed["options"][0]["revision"])
        changed["options"][1]["display"] = deepcopy(changed["options"][0]["display"])
        mutations.append(changed)
        changed = deepcopy(options)
        changed["options"][0]["revision"]["sha256"] = "f" * 64
        mutations.append(changed)
        changed = deepcopy(options)
        changed["options"][0]["origin"]["kind"] = "future_origin"
        mutations.append(changed)
        changed = deepcopy(options)
        changed["options"][0]["profile_path"] = "/private/profile.json"
        mutations.append(changed)
        changed = deepcopy(options)
        changed["options"][0]["launch_grant"] = (
            "launch-grant:nested_authority_must_not_cross_0001"
        )
        mutations.append(changed)

        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    generated.validate_presenter_stage_launch_options(mutation)

        additive = deepcopy(options)
        additive["future_safe_top_level"] = True
        self.assertIs(
            generated.validate_presenter_stage_launch_options(additive),
            additive,
        )
        with self.assertRaises(ValueError):
            generated.validate_presenter_stage_launch_options(
                options,
                expected_epoch="epoch:phase7_different_epoch_0001",
            )

        selected = _fixture("canonical-valid/launch-selection/authored-fresh.json")
        with self.assertRaises(ValueError):
            generated.validate_stage_launch_selection(
                selected,
                expected_epoch="epoch:phase7_different_epoch_0001",
            )
        zero_serial = deepcopy(selected)
        zero_serial["launch_grant"] = "launch-grant:0000000000000000_" + "a" * 64
        with self.assertRaises(ValueError):
            generated.validate_stage_launch_selection(zero_serial)
        forged = _fixture("canonical-valid/launch-selection/forge-fresh.json")
        forged["grant_expires_at"] = "2026-08-27T15:00:00Z"
        with self.assertRaises(ValueError):
            generated.validate_stage_launch_selection(forged)

    def test_generated_methods_emit_exact_headers_and_body(self) -> None:
        generated = _load_generated_client()
        options = _fixture("canonical-valid/launch-selection/options-mixed.json")
        selected = options["options"][0]
        response = _fixture("canonical-valid/launch-selection/authored-fresh.json")
        opener = _RecordingOpener(response)
        client = generated.ProductSessionPreviewClient(
            "http://127.0.0.1:1",
            "private-product-bff-bearer",
        )
        client._opener = opener

        result = client.exchange_presenter_stage_launch_selection(
            epoch=options["service_epoch"],
            actor="actor:phase7_launch_selection_0001",
            key="phase7-launch-selection-key-0001",
            launch_selection_id=selected["launch_selection_id"],
        )

        self.assertEqual(result, response)
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.full_url, "http://127.0.0.1:1/product/v1/stage/launch-selections"
        )
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"launch_selection_id": selected["launch_selection_id"]},
        )
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(
            headers["x-dreamland-expected-epoch"],
            options["service_epoch"],
        )
        self.assertEqual(
            headers["x-dreamland-actor-context"],
            "actor:phase7_launch_selection_0001",
        )
        self.assertEqual(
            headers["idempotency-key"],
            "phase7-launch-selection-key-0001",
        )
        self.assertNotIn("x-dreamland-forge-expected-epoch", headers)
        self.assertNotIn("x-dreamland-session-capability", headers)

    def test_generated_methods_reject_response_epoch_divergence(self) -> None:
        generated = _load_generated_client()
        options = _fixture("canonical-valid/launch-selection/options-mixed.json")
        expected_epoch = options["service_epoch"]
        changed_options = deepcopy(options)
        changed_options["service_epoch"] = "epoch:phase7_different_epoch_0001"
        client = generated.ProductSessionPreviewClient(
            "http://127.0.0.1:1",
            "private-product-bff-bearer",
        )
        client._opener = _RecordingOpener(changed_options)
        with self.assertRaises(ValueError):
            client.presenter_stage_launch_options(
                epoch=expected_epoch,
                actor="actor:phase7_launch_selection_0001",
            )

        selected = options["options"][0]
        changed_selection = _fixture(
            "canonical-valid/launch-selection/authored-fresh.json"
        )
        changed_selection["service_epoch"] = "epoch:phase7_different_epoch_0001"
        client._opener = _RecordingOpener(changed_selection)
        with self.assertRaises(ValueError):
            client.exchange_presenter_stage_launch_selection(
                epoch=expected_epoch,
                actor="actor:phase7_launch_selection_0001",
                key="phase7-launch-selection-key-epoch-mismatch",
                launch_selection_id=selected["launch_selection_id"],
            )

    def test_generated_exchange_rejects_invalid_input_before_transport(self) -> None:
        generated = _load_generated_client()
        client = generated.ProductSessionPreviewClient(
            "http://127.0.0.1:1",
            "private-product-bff-bearer",
        )
        opener = _RejectingOpener()
        client._opener = opener
        for invalid in (
            "offering:authored-colonyship-a1",
            "launch-selection:short",
            "launch-selection:" + "a" * 129,
            "a" * 64,
            7,
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    client.exchange_presenter_stage_launch_selection(
                        epoch="epoch:phase7_launch_epoch_0001",
                        actor="actor:phase7_launch_actor_0001",
                        key="phase7-launch-selection-key-0001",
                        launch_selection_id=invalid,
                    )
        self.assertEqual(opener.calls, 0)

    def test_safe_errors_are_canonical_and_do_not_leak_context(self) -> None:
        expected = {
            "launch-selection-unavailable.json": (
                404,
                "none",
                None,
            ),
            "launch-selection-capacity-exhausted.json": (
                503,
                "after_delay",
                1000,
            ),
        }
        for name, (status, retry, delay) in expected.items():
            payload = _fixture(f"canonical-valid/errors/{name}")
            with self.subTest(name=name):
                code = payload["error"]["code"]
                self.assertEqual(TOOL.SAFE_ERROR_CASES[code][0], status)
                self.assertEqual(payload["error"]["retry"], retry)
                self.assertEqual(payload["error"]["retry_after_ms"], delay)
                self.assertEqual(payload["error"].get("context", {}), {})

    def test_provider_free_preview_keeps_all_capabilities_disabled(self) -> None:
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        manifest = _json(CONTRACT_ROOT / "manifest.json")
        preview = openapi["components"]["schemas"]["PreviewIdentity"]["properties"]
        boundary = openapi["x-dreamland-stage-launch-selection"]

        self.assertEqual(
            preview["executor"]["const"],
            "deterministic-launch-selection-no-provider-v1",
        )
        self.assertFalse(preview["provider_calls"]["const"])
        self.assertFalse(boundary["selection_reference_authorizes_launch"])
        self.assertFalse(boundary["offering_id_authorizes_launch"])
        self.assertFalse(boundary["profile_names_or_paths_accepted"])
        self.assertFalse(boundary["provider_dispatch"])
        self.assertFalse(boundary["normal_runtime_composition"])
        self.assertFalse(boundary["capability_evidence"])
        self.assertEqual(manifest["publication_status"], "published")
        self.assertEqual(
            manifest["backend_commit"],
            "9679470cfb830bc45f4de128f8233f8c625d4ee5",
        )
        self.assertEqual(manifest["published_at"], "2026-08-27T23:33:00Z")
        self.assertTrue(manifest["capabilities"])
        self.assertTrue(
            all(not item["enabled"] for item in manifest["capabilities"].values())
        )

        for fixture in TOOL.fixture_definitions():
            if fixture.classification == "invalid":
                continue
            for value in _walk(fixture.payload):
                if isinstance(value, dict) and "preview" in value:
                    self.assertFalse(value["preview"]["provider_calls"])

    def test_safe_option_fixtures_expose_no_paths_profiles_or_authority(self) -> None:
        options = _fixture("canonical-valid/launch-selection/options-mixed.json")
        responses = (
            _fixture("canonical-valid/launch-selection/authored-fresh.json"),
            _fixture("canonical-valid/launch-selection/forge-fresh.json"),
        )
        forbidden = {
            "artifact_path",
            "authority",
            "forge_reference",
            "launch_grant",
            "profile",
            "profile_name",
            "profile_path",
            "raw_state",
            "selected_record_id",
            "source_path",
            "state_profile",
            "world_state",
        }
        for option in [*options["options"], *(item["selection"] for item in responses)]:
            observed = {
                key
                for value in _walk(option)
                if isinstance(value, dict)
                for key in value
            }
            self.assertEqual(forbidden.intersection(observed), set())

    def test_published_rc3_tree_remains_byte_exact(self) -> None:
        self.assertEqual(_tree_digest(RC3_ROOT), RC3_TREE)


def _load_generated_client():
    path = CONTRACT_ROOT / "generated" / "product_session_client.py"
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_launch_selection_generated_contract_test",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("generated launch-selection client is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _validator(openapi: dict[str, object], schema_name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {
            **openapi,
            "$ref": f"#/components/schemas/{schema_name}",
        }
    )


def _fixture(relative: str):
    return _json(CONTRACT_ROOT / "fixtures" / relative)


class _JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["Cache-Control"] = "no-store"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


class _RecordingOpener:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests = []

    def open(self, request, *, timeout: int):
        self.requests.append(request)
        return _JsonResponse(self.payload)


class _RejectingOpener:
    def __init__(self) -> None:
        self.calls = 0

    def open(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("invalid request reached transport")


def _tree_digest(root: Path) -> tuple[int, str]:
    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return len(paths), digest.hexdigest()


def _walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
