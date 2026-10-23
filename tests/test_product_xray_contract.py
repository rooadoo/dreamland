from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
from copy import deepcopy
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from jsonschema import Draft202012Validator

from dreamland.product.forge_artifacts import validate_forge_profile
from dreamland.product.xray import StageXrayPolicy, build_stage_xray_projection
from dreamland.state_profiles import StateProfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_xray_contract.py"
MAX_SAFE_INTEGER = 9_007_199_254_740_991
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.6.0-rc.3"


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_xray_contract_test_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("Stage X-ray contract tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load_tool()


class ProductXrayContractTests(unittest.TestCase):
    def test_release_candidate_validates_and_generated_client_smokes(self) -> None:
        validated = TOOL.validate_contract(CONTRACT_ROOT)
        smoked = TOOL.client_smoke(CONTRACT_ROOT)

        self.assertEqual(validated["contract_revision"], "0.6.0-rc.3")
        self.assertEqual(validated["routes"], 19)
        self.assertEqual(validated["capabilities_enabled"], 0)
        self.assertEqual(smoked["validated_client_fixtures"], 11)
        self.assertEqual(smoked["rejected_client_mutations"], 93)

    def test_snapshot_and_detail_routes_bind_two_coordinates(self) -> None:
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        snapshot = openapi["paths"]["/product/v1/stage/sessions/{reference}/xray"][
            "get"
        ]
        detail = openapi["paths"][
            "/product/v1/stage/sessions/{reference}/xray/details/{xray_entity_id}"
        ]["get"]

        self.assertEqual(snapshot["operationId"], "getStageXraySnapshot")
        self.assertEqual(detail["operationId"], "getStageXrayDetail")
        query_names = {
            openapi["components"]["parameters"][parameter["$ref"].rsplit("/", 1)[1]][
                "name"
            ]
            for parameter in detail["parameters"]
            if "$ref" in parameter
            and openapi["components"]["parameters"][
                parameter["$ref"].rsplit("/", 1)[1]
            ]["in"]
            == "query"
        }
        self.assertEqual(
            query_names,
            {"accepted_state_revision", "xray_stream_id", "xray_sequence"},
        )
        xray = openapi["x-dreamland-stage-xray"]
        self.assertEqual(xray["stage_event_position"], "correlation_watermark_only")
        self.assertFalse(xray["raw_runtime_state"])

    def test_generated_coordinate_validators_enforce_safe_integer_boundaries(
        self,
    ) -> None:
        client = _load_generated_client()
        snapshot = _xray_fixture("snapshot.json")
        detail = _xray_fixture("host-detail.json")
        entity_id = detail["xray"]["detail"]["entity_id"]
        stream_id = detail["xray"]["projection_position"]["stream_id"]

        for accepted_revision, sequence in (
            (0, 1),
            (MAX_SAFE_INTEGER, MAX_SAFE_INTEGER),
        ):
            with self.subTest(
                boundary=(accepted_revision, sequence),
                response="snapshot",
            ):
                changed = _with_xray_coordinates(
                    snapshot,
                    accepted_revision=accepted_revision,
                    sequence=sequence,
                )
                self.assertIs(client.validate_stage_xray_snapshot(changed), changed)
            with self.subTest(
                boundary=(accepted_revision, sequence),
                response="detail",
            ):
                changed = _with_xray_coordinates(
                    detail,
                    accepted_revision=accepted_revision,
                    sequence=sequence,
                )
                self.assertIs(
                    client.validate_stage_xray_detail(
                        changed,
                        expected_entity_id=entity_id,
                        expected_accepted_state_revision=accepted_revision,
                        expected_xray_stream_id=stream_id,
                        expected_xray_sequence=sequence,
                    ),
                    changed,
                )

        invalid_accepted_revisions = (
            MAX_SAFE_INTEGER + 1,
            -1,
            True,
            False,
            "3",
            3.0,
            3.5,
        )
        for invalid in invalid_accepted_revisions:
            changed_snapshot = _with_xray_coordinates(
                snapshot,
                accepted_revision=invalid,
            )
            changed_detail = _with_xray_coordinates(
                detail,
                accepted_revision=invalid,
            )
            with self.subTest(observed_accepted_revision=invalid, response="snapshot"):
                with self.assertRaises(ValueError):
                    client.validate_stage_xray_snapshot(changed_snapshot)
            with self.subTest(observed_accepted_revision=invalid, response="detail"):
                with self.assertRaises(ValueError):
                    client.validate_stage_xray_detail(
                        changed_detail,
                        expected_entity_id=entity_id,
                        expected_accepted_state_revision=3,
                        expected_xray_stream_id=stream_id,
                        expected_xray_sequence=7,
                    )

        invalid_sequences = (
            MAX_SAFE_INTEGER + 1,
            0,
            -1,
            True,
            False,
            "7",
            7.0,
            7.5,
        )
        for invalid in invalid_sequences:
            changed_snapshot = _with_xray_coordinates(snapshot, sequence=invalid)
            changed_detail = _with_xray_coordinates(detail, sequence=invalid)
            with self.subTest(observed_sequence=invalid, response="snapshot"):
                with self.assertRaises(ValueError):
                    client.validate_stage_xray_snapshot(changed_snapshot)
            with self.subTest(observed_sequence=invalid, response="detail"):
                with self.assertRaises(ValueError):
                    client.validate_stage_xray_detail(
                        changed_detail,
                        expected_entity_id=entity_id,
                        expected_accepted_state_revision=3,
                        expected_xray_stream_id=stream_id,
                        expected_xray_sequence=7,
                    )

    def test_detail_validator_strictly_validates_expected_coordinates(self) -> None:
        client = _load_generated_client()
        detail = _xray_fixture("host-detail.json")
        entity_id = detail["xray"]["detail"]["entity_id"]
        stream_id = detail["xray"]["projection_position"]["stream_id"]

        for invalid in (
            MAX_SAFE_INTEGER + 1,
            -1,
            True,
            False,
            "3",
            3.0,
            3.5,
        ):
            with self.subTest(expected_accepted_revision=invalid):
                with self.assertRaises(ValueError):
                    client.validate_stage_xray_detail(
                        detail,
                        expected_entity_id=entity_id,
                        expected_accepted_state_revision=invalid,
                        expected_xray_stream_id=stream_id,
                        expected_xray_sequence=7,
                    )

        for invalid in (
            MAX_SAFE_INTEGER + 1,
            0,
            -1,
            True,
            False,
            "7",
            7.0,
            7.5,
        ):
            with self.subTest(expected_sequence=invalid):
                with self.assertRaises(ValueError):
                    client.validate_stage_xray_detail(
                        detail,
                        expected_entity_id=entity_id,
                        expected_accepted_state_revision=3,
                        expected_xray_stream_id=stream_id,
                        expected_xray_sequence=invalid,
                    )

        for invalid in (
            None,
            True,
            "",
            "xray-stream:short",
            "xray-stream:" + "g" * 32,
            "xray-stream:" + "a" * 33,
        ):
            with self.subTest(expected_stream_id=invalid):
                with self.assertRaises(ValueError):
                    client.validate_stage_xray_detail(
                        detail,
                        expected_entity_id=entity_id,
                        expected_accepted_state_revision=3,
                        expected_xray_stream_id=invalid,
                        expected_xray_sequence=7,
                    )

        equality_traps = (
            (1, 7, True, 7),
            (0, 7, False, 7),
            (3, 1, 3, True),
            (3, 7, 3.0, 7),
            (3, 7, 3, 7.0),
        )
        for (
            observed_revision,
            observed_sequence,
            expected_revision,
            expected_sequence,
        ) in equality_traps:
            changed = _with_xray_coordinates(
                detail,
                accepted_revision=observed_revision,
                sequence=observed_sequence,
            )
            with self.subTest(
                python_equality_trap=(
                    observed_revision,
                    observed_sequence,
                    expected_revision,
                    expected_sequence,
                )
            ):
                with self.assertRaises(ValueError):
                    client.validate_stage_xray_detail(
                        changed,
                        expected_entity_id=entity_id,
                        expected_accepted_state_revision=expected_revision,
                        expected_xray_stream_id=stream_id,
                        expected_xray_sequence=expected_sequence,
                    )

    def test_generated_detail_method_validates_before_transport(self) -> None:
        generated = _load_generated_client()
        arguments = _valid_detail_arguments()
        invalid_preconditions = []
        for value in (
            MAX_SAFE_INTEGER + 1,
            -1,
            True,
            False,
            "3",
            3.0,
            3.5,
        ):
            invalid_preconditions.append(("accepted_state_revision", value))
        for value in (
            MAX_SAFE_INTEGER + 1,
            0,
            -1,
            True,
            False,
            "7",
            7.0,
            7.5,
        ):
            invalid_preconditions.append(("xray_sequence", value))
        for value in (
            None,
            True,
            "",
            "xray-stream:short",
            "xray-stream:" + "g" * 32,
        ):
            invalid_preconditions.append(("xray_stream_id", value))

        for name, value in invalid_preconditions:
            opener = _RecordingOpener(_xray_fixture("host-detail.json"))
            client = generated.ProductSessionPreviewClient(
                "http://127.0.0.1:1",
                "test-stage-bearer",
            )
            client._opener = opener
            changed = {**arguments, name: value}
            with self.subTest(precondition=name, value=value):
                with self.assertRaises(ValueError):
                    client.stage_xray_detail(**changed)
                self.assertEqual(opener.requests, [])

    def test_generated_detail_method_emits_canonical_decimal_queries(self) -> None:
        generated = _load_generated_client()
        stream_id = "xray-stream:" + "a" * 32
        for accepted_revision, sequence in (
            (0, 1),
            (MAX_SAFE_INTEGER, MAX_SAFE_INTEGER),
        ):
            response = _with_xray_coordinates(
                _xray_fixture("host-detail.json"),
                accepted_revision=accepted_revision,
                stream_id=stream_id,
                sequence=sequence,
            )
            opener = _RecordingOpener(response)
            client = generated.ProductSessionPreviewClient(
                "http://127.0.0.1:1",
                "test-stage-bearer",
            )
            client._opener = opener
            arguments = {
                **_valid_detail_arguments(),
                "accepted_state_revision": accepted_revision,
                "xray_stream_id": stream_id,
                "xray_sequence": sequence,
            }

            result = client.stage_xray_detail(**arguments)

            self.assertEqual(result, response)
            self.assertEqual(len(opener.requests), 1)
            query = parse_qsl(urlsplit(opener.requests[0].full_url).query)
            self.assertEqual(
                query,
                [
                    ("accepted_state_revision", str(accepted_revision)),
                    ("xray_stream_id", stream_id),
                    ("xray_sequence", str(sequence)),
                ],
            )
            for _, value in (query[0], query[2]):
                self.assertRegex(value, r"^(?:0|[1-9][0-9]*)$")
                self.assertEqual(value, str(int(value)))

    def test_generated_response_context_matches_openapi(self) -> None:
        generated = _load_generated_client()
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        snapshot = _xray_fixture("snapshot.json")
        validator = Draft202012Validator(
            {
                **openapi,
                "$ref": "#/components/schemas/StageXraySnapshotResponse",
            }
        )
        invalid_payloads = []
        changed = deepcopy(snapshot)
        del changed["service_epoch"]
        invalid_payloads.append(("missing service epoch", changed))
        for value in (
            "",
            "epoch:short",
            7,
            True,
            "epoch:" + "a" * 129,
        ):
            changed = deepcopy(snapshot)
            changed["service_epoch"] = value
            invalid_payloads.append((f"invalid service epoch {value!r}", changed))
        for field in ("name", "revision", "compatibility"):
            changed = deepcopy(snapshot)
            del changed["contract"][field]
            invalid_payloads.append((f"missing contract {field}", changed))
        for field, value in (
            ("name", "another-contract"),
            ("revision", "0.6.0-rc.2"),
            ("compatibility", "stable"),
        ):
            changed = deepcopy(snapshot)
            changed["contract"][field] = value
            invalid_payloads.append((f"incorrect contract {field}", changed))
        changed = deepcopy(snapshot)
        changed["contract"]["future"] = "not-allowed"
        invalid_payloads.append(("additional contract field", changed))

        for label, payload in invalid_payloads:
            with self.subTest(context_mutation=label):
                self.assertFalse(validator.is_valid(payload))
                with self.assertRaises(ValueError):
                    generated.validate_stage_xray_snapshot(payload)

        additive = deepcopy(snapshot)
        additive["future_top_level_response_field"] = {"safe": True}
        self.assertTrue(validator.is_valid(additive))
        self.assertIs(generated.validate_stage_xray_snapshot(additive), additive)

    def test_canonical_projection_is_strict_bounded_and_privacy_safe(self) -> None:
        openapi = _json(CONTRACT_ROOT / "openapi.json")
        payload = _json(
            CONTRACT_ROOT / "fixtures" / "canonical-valid" / "xray" / "snapshot.json"
        )
        validator = Draft202012Validator(
            {
                **openapi,
                "$ref": "#/components/schemas/StageXraySnapshotResponse",
            }
        )
        self.assertTrue(validator.is_valid(payload))
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "world_state",
            "SessionState",
            "prompt",
            "reasoning",
            "credential",
            "environment",
            "backend_path",
        ):
            self.assertNotIn(forbidden, serialized)

        changed = deepcopy(payload)
        changed["xray"]["hosts"][0]["raw_state"] = {"secret": True}
        self.assertFalse(validator.is_valid(changed))

    def test_forge_valid_empty_and_hostile_display_values_remain_schema_valid(
        self,
    ) -> None:
        world_state = {
            "name": "",
            "entry_host": "h",
            "active_host": "h",
            "purpose": {"theme": "", "description": "\u202e"},
            "hosts_by_name": {
                "h": {
                    "hostname": "",
                    "login_banner": "Test console\n",
                    "purpose": {"theme": "", "description": "\x00"},
                    "operating_system": {
                        "family": "",
                        "distribution": "\u202eLinux",
                        "version": "v" * 513,
                    },
                    "shell": {
                        "default": {
                            "name": "bash",
                            "command": "/bin/bash",
                            "argv": ["bash"],
                        }
                    },
                }
            },
            "world_facts_by_id": {
                "empty": "",
                "bounded": {"statement": "", "description": "f" * 512},
                "marked": "radio\x00link",
            },
        }
        profile = StateProfile(
            name="xray-edge-case",
            level="world",
            created_at="2026-08-27T00:00:00+00:00",
            state={"world_state": world_state},
        )
        validation = validate_forge_profile(profile)
        self.assertTrue(validation.valid, validation.issues)

        projection = build_stage_xray_projection(
            {"world_state": world_state},
            policy=StageXrayPolicy(),
        )
        projected_snapshot = json.loads(json.dumps(dict(projection.snapshot)))
        self.assertNotIn("world_name", projected_snapshot["overview"])
        self.assertEqual(projected_snapshot["hosts"][0]["hostname"]["text"], "h")
        self.assertEqual(
            projected_snapshot["completeness"]["facts"]["omitted_count"], 1
        )
        self.assertNotIn(
            "",
            [
                value["text"]
                for host in projected_snapshot["hosts"]
                for value in (host["hostname"],)
            ],
        )

        openapi = _json(CONTRACT_ROOT / "openapi.json")
        canonical_snapshot = _json(
            CONTRACT_ROOT / "fixtures/canonical-valid/xray/snapshot.json"
        )
        coordinates = {
            name: deepcopy(canonical_snapshot["xray"][name])
            for name in (
                "accepted_state_revision",
                "projection_position",
                "stage_event_watermark",
            )
        }
        response = deepcopy(canonical_snapshot)
        response["xray"] = {**projected_snapshot, **coordinates}
        snapshot_validator = Draft202012Validator(
            {
                **openapi,
                "$ref": "#/components/schemas/StageXraySnapshotResponse",
            }
        )
        self.assertEqual(list(snapshot_validator.iter_errors(response)), [])

        canonical_detail = _json(
            CONTRACT_ROOT / "fixtures/canonical-valid/xray/host-detail.json"
        )
        detail_validator = Draft202012Validator(
            {
                **openapi,
                "$ref": "#/components/schemas/StageXrayDetailResponse",
            }
        )
        for detail in projection.details.values():
            detail_response = deepcopy(canonical_detail)
            detail_response["xray"] = {
                "schema_revision": projected_snapshot["schema_revision"],
                **coordinates,
                "detail": json.loads(json.dumps(dict(detail))),
            }
            self.assertEqual(list(detail_validator.iter_errors(detail_response)), [])

    def test_manifest_keeps_production_capability_disabled(self) -> None:
        manifest = _json(CONTRACT_ROOT / "manifest.json")
        self.assertEqual(manifest["publication_status"], "published")
        self.assertEqual(
            manifest["backend_commit"],
            "a6674bd3c43eda6e1c72403c136a5a69160ca7f6",
        )
        self.assertFalse(manifest["capabilities"]["stage_xray"]["enabled"])
        self.assertEqual(
            manifest["capabilities"]["stage_xray"]["reason"],
            "integration_preview_only",
        )
        self.assertEqual(
            manifest["dependencies"]["world_forge"]["contract_revision"],
            "0.1.0-rc.4",
        )
        for key, relative in {
            "events_schema_sha256": "events.schema.json",
            "generated_client_sha256": "generated/product_session_client.py",
            "manifest_schema_sha256": "manifest.schema.json",
            "openapi_sha256": "openapi.json",
        }.items():
            self.assertEqual(
                hashlib.sha256((CONTRACT_ROOT / relative).read_bytes()).hexdigest(),
                manifest["artifacts"][key],
            )

    def test_published_rc3_refuses_refresh(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "published Stage X-ray"):
            TOOL.refresh_contract(CONTRACT_ROOT)


def _load_generated_client():
    path = CONTRACT_ROOT / "generated" / "product_session_client.py"
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_xray_generated_contract_test",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("generated Stage X-ray client is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _xray_fixture(name: str) -> dict[str, object]:
    return _json(CONTRACT_ROOT / "fixtures" / "canonical-valid" / "xray" / name)


def _with_xray_coordinates(
    payload: dict[str, object],
    *,
    accepted_revision: object = 3,
    stream_id: str = "xray-stream:" + "a" * 32,
    sequence: object = 7,
) -> dict[str, object]:
    changed = deepcopy(payload)
    xray = changed["xray"]
    position = xray["projection_position"]
    xray["accepted_state_revision"] = accepted_revision
    position["stream_id"] = stream_id
    position["sequence"] = sequence
    position["cursor"] = (
        "xray-cursor:" + stream_id.removeprefix("xray-stream:") + f":{sequence}"
    )
    return changed


def _valid_detail_arguments() -> dict[str, object]:
    return {
        "epoch": "epoch:" + "e" * 16,
        "actor": "actor:" + "a" * 16,
        "capability": "session-cap:" + "c" * 24,
        "reference": "session-ref:" + "r" * 16,
        "entity_id": "host:" + "1" * 32,
        "accepted_state_revision": 3,
        "xray_stream_id": "xray-stream:" + "a" * 32,
        "xray_sequence": 7,
    }


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
        if limit < 0:
            return self._body
        return self._body[:limit]


class _RecordingOpener:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests = []

    def open(self, request, *, timeout: int):
        self.requests.append(request)
        return _JsonResponse(self.payload)


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
