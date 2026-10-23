from __future__ import annotations

import http.client
import importlib.util
import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_launch_selection_preview.py"
CONTRACT_TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_launch_selection_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.7.0-rc.1"
SERVICE_BEARER = "private-launch-selection-preview-bearer"


def _load_module(path: Path, name: str):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {name}")
    module = importlib.util.module_from_spec(specification)
    prior = sys.modules.get(name)
    sys.modules[name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        if prior is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prior
    return module


PREVIEW = _load_module(
    TOOL_PATH, "dreamland_product_launch_selection_preview_test_tool"
)
CONTRACT = _load_module(
    CONTRACT_TOOL_PATH, "dreamland_product_launch_selection_contract_test_tool"
)
OPENAPI = json.loads((CONTRACT_ROOT / "openapi.json").read_text(encoding="utf-8"))
SCHEMAS = {
    "openapi.json": OPENAPI,
    "events.schema.json": json.loads(
        (CONTRACT_ROOT / "events.schema.json").read_text(encoding="utf-8")
    ),
}


def validate_schema(value: object, target: str) -> None:
    schema, _label = CONTRACT._CORE._schema_target(target, SCHEMAS)
    if schema is None:
        raise AssertionError(f"schema target is unavailable: {target}")
    errors = CONTRACT._CORE._validation_errors(schema, value)
    if errors:
        raise AssertionError(f"{target} rejected preview value: {errors[0]}")


class RunningPreview:
    def __init__(self, scenario: str = PREVIEW.SCENARIOS[0]) -> None:
        self.server = PREVIEW.build_preview_server(
            host="127.0.0.1",
            port=0,
            bearer=SERVICE_BEARER,
            scenario=scenario,
            contract_root=CONTRACT_ROOT,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


def request(
    running: RunningPreview,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: object | None = None,
) -> tuple[int, dict, object]:
    request_headers = {"Authorization": f"Bearer {SERVICE_BEARER}"}
    request_headers.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    http_request = Request(
        running.base_url + path,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(http_request, timeout=5) as response:
            return response.status, json.loads(response.read()), response.headers
    except HTTPError as error:
        return error.code, json.loads(error.read()), error.headers


def stage_headers(epoch: str, *, key: str | None = None) -> dict[str, str]:
    headers = {
        "X-Dreamland-Expected-Epoch": epoch,
        "X-Dreamland-Actor-Context": PREVIEW.PRESENTER_CONTEXT,
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def options(running: RunningPreview, epoch: str) -> dict:
    status, payload, _headers = request(
        running,
        "GET",
        PREVIEW.LAUNCH_OPTIONS_PATH,
        headers=stage_headers(epoch),
    )
    if status != 200:
        raise AssertionError(f"launch options failed: {status} {payload}")
    return payload


def exchange(
    running: RunningPreview,
    epoch: str,
    selection_id: str,
    key: str,
) -> tuple[int, dict, object]:
    return request(
        running,
        "POST",
        PREVIEW.LAUNCH_SELECTIONS_PATH,
        headers=stage_headers(epoch, key=key),
        body={"launch_selection_id": selection_id},
    )


class ProductLaunchSelectionPreviewTests(unittest.TestCase):
    def test_import_is_inert_and_does_not_configure_live_provider_mode(self) -> None:
        code = f"""
import importlib.util
import json
import pathlib
import threading
before = [thread.name for thread in threading.enumerate()]
path = pathlib.Path({str(TOOL_PATH)!r})
spec = importlib.util.spec_from_file_location("launch_selection_inert_check", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
after = [thread.name for thread in threading.enumerate()]
print(json.dumps({{
    "before": before,
    "after": after,
    "provider_calls": module._CORE.PREVIEW_PROVIDER_CALLS,
    "revision": module.CONTRACT_REVISION,
}}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["before"], payload["after"])
        self.assertFalse(payload["provider_calls"])
        self.assertEqual(payload["revision"], "0.7.0-rc.1")

    def test_contract_capabilities_and_options_are_exact_safe_and_bounded(self) -> None:
        validation = CONTRACT.validate_contract(CONTRACT_ROOT)
        self.assertEqual(validation["contract_revision"], "0.7.0-rc.1")
        self.assertEqual(validation["routes"], 21)
        self.assertEqual(validation["capabilities_enabled"], 0)

        with RunningPreview() as running:
            status, capabilities, response_headers = request(
                running, "GET", "/product/v1/capabilities"
            )
            epoch = capabilities["service_epoch"]
            option_payload = options(running, epoch)
            query_status, query_problem, _ = request(
                running,
                "GET",
                PREVIEW.LAUNCH_OPTIONS_PATH + "?unexpected=true",
                headers=stage_headers(epoch),
            )
            wrong_status, wrong_problem, _ = request(
                running,
                "GET",
                PREVIEW.LAUNCH_OPTIONS_PATH,
                headers={
                    "X-Dreamland-Expected-Epoch": epoch,
                    "X-Dreamland-Actor-Context": (
                        "actor:wrong_launch_selection_actor_0001"
                    ),
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            response_headers["X-Dreamland-Contract-Revision"], "0.7.0-rc.1"
        )
        self.assertTrue(
            all(not item["enabled"] for item in capabilities["capabilities"].values())
        )
        self.assertEqual(len(option_payload["options"]), 2)
        self.assertEqual(
            {item["origin"]["kind"] for item in option_payload["options"]},
            {"authored_profile_snapshot", "world_forge_selection"},
        )
        serialized = json.dumps(option_payload, sort_keys=True)
        for forbidden in (
            "profile_path",
            "source_path",
            "world_state",
            "session_authority",
            "launch_grant",
            PREVIEW.PRESENTER_CONTEXT,
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(query_status, 404)
        self.assertEqual(query_problem["error"]["code"], "not_found")
        self.assertEqual(wrong_status, 403)
        self.assertEqual(wrong_problem["error"]["code"], "authority_denied")
        validate_schema(
            option_payload,
            "openapi.json#/components/schemas/PresenterStageLaunchOptionsResponse",
        )

        with RunningPreview("launch-selection-empty") as running:
            _, capabilities, _ = request(running, "GET", "/product/v1/capabilities")
            empty = options(running, capabilities["service_epoch"])
        self.assertEqual(empty["options"], [])
        validate_schema(
            empty,
            "openapi.json#/components/schemas/PresenterStageLaunchOptionsResponse",
        )

    def test_exchange_replay_conflict_and_unavailable_are_contract_safe(self) -> None:
        unknown = "launch-selection:unknown_preview_selection_000000009"
        with RunningPreview() as running:
            _, capabilities, _ = request(running, "GET", "/product/v1/capabilities")
            epoch = capabilities["service_epoch"]
            invalid_status, invalid, _ = exchange(
                running,
                epoch,
                "offering:authored-preview",
                "body-validation-before-claim-0001",
            )
            first_status, first, _ = exchange(
                running,
                epoch,
                PREVIEW.AUTHORED_SELECTION_ID,
                "body-validation-before-claim-0001",
            )
            replay_status, replay, _ = exchange(
                running,
                epoch,
                PREVIEW.AUTHORED_SELECTION_ID,
                "body-validation-before-claim-0001",
            )
            unavailable_status, unavailable, _ = exchange(
                running, epoch, unknown, "deterministic-unavailable-0001"
            )
            unavailable_replay_status, unavailable_replay, _ = exchange(
                running, epoch, unknown, "deterministic-unavailable-0001"
            )
            conflict_status, conflict, _ = exchange(
                running,
                epoch,
                PREVIEW.FORGED_SELECTION_ID,
                "deterministic-unavailable-0001",
            )

        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_request")
        self.assertEqual((first_status, replay_status), (200, 200))
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["launch_grant"], replay["launch_grant"])
        self.assertEqual((unavailable_status, unavailable_replay_status), (404, 404))
        self.assertEqual(unavailable["error"]["code"], "launch_selection_unavailable")
        # Idempotency replays the recorded logical rejection.  The transport
        # still assigns a fresh correlation ID to each HTTP request.
        first_error = dict(unavailable["error"])
        replay_error = dict(unavailable_replay["error"])
        first_request_id = first_error.pop("request_id")
        replay_request_id = replay_error.pop("request_id")
        self.assertEqual(first_error, replay_error)
        self.assertNotEqual(first_request_id, replay_request_id)
        self.assertEqual(unavailable["contract"], unavailable_replay["contract"])
        self.assertEqual(unavailable["preview"], unavailable_replay["preview"])
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
        validate_schema(
            first,
            "openapi.json#/components/schemas/StageLaunchSelectionResponse",
        )
        validate_schema(
            unavailable,
            "openapi.json#/components/schemas/SafeErrorEnvelope",
        )

    def test_exchanged_authored_launch_and_forge_replace_use_stage_and_xray(
        self,
    ) -> None:
        with RunningPreview() as running:
            _, capabilities, _ = request(running, "GET", "/product/v1/capabilities")
            epoch = capabilities["service_epoch"]
            authored_option = next(
                item
                for item in options(running, epoch)["options"]
                if item["origin"]["kind"] == "authored_profile_snapshot"
            )
            _, authored, _ = exchange(
                running,
                epoch,
                authored_option["launch_selection_id"],
                "authored-exchange-0001",
            )
            launch_status, launched, _ = request(
                running,
                "POST",
                PREVIEW._STAGE.STAGE_LAUNCH_PATH,
                headers=stage_headers(epoch, key="authored-launch-0001"),
                body={"launch_grant": authored["launch_grant"]},
            )
            reference = launched["snapshot"]["reference"]
            session_headers = {
                **stage_headers(epoch),
                "X-Dreamland-Session-Capability": launched["session_authority"],
            }
            xray_status, xray, _ = request(
                running,
                "GET",
                f"{PREVIEW._STAGE.STAGE_SESSION_PREFIX}{quote(reference, safe='')}/xray",
                headers=session_headers,
            )
            command_status, accepted, _ = request(
                running,
                "POST",
                f"/product/v1/sessions/{quote(reference, safe='')}/commands",
                headers={
                    **session_headers,
                    "Idempotency-Key": "authored-command-0001",
                },
                body={"command": "whoami", "expected_slot_generation": 0},
            )
            deadline = time.monotonic() + 5
            while True:
                command_result_status, command_result, _ = request(
                    running,
                    "GET",
                    (
                        f"/product/v1/sessions/{quote(reference, safe='')}/commands/"
                        f"{quote(accepted['command_id'], safe='')}"
                    ),
                    headers=session_headers,
                )
                if command_result["terminal"]:
                    break
                if time.monotonic() >= deadline:
                    self.fail("selected authored Stage command did not become terminal")
            forged_option = next(
                item
                for item in options(running, epoch)["options"]
                if item["origin"]["kind"] == "world_forge_selection"
            )
            _, forged, _ = exchange(
                running,
                epoch,
                forged_option["launch_selection_id"],
                "forged-exchange-0001",
            )
            current_status, current, _ = request(
                running,
                "GET",
                PREVIEW._STAGE.STAGE_CURRENT_PATH,
                headers=stage_headers(epoch),
            )
            replace_status, replaced, _ = request(
                running,
                "POST",
                f"{PREVIEW._STAGE.STAGE_SESSION_PREFIX}{quote(reference, safe='')}/replace",
                headers={
                    **stage_headers(epoch, key="forged-replace-0001"),
                    "X-Dreamland-Session-Capability": launched["session_authority"],
                },
                body={
                    "expected_control_revision": current["snapshot"]["coordinates"][
                        "control_revision"
                    ],
                    "launch_grant": forged["launch_grant"],
                },
            )

        self.assertEqual(
            (
                launch_status,
                xray_status,
                command_status,
                command_result_status,
                current_status,
            ),
            (201, 200, 202, 200, 200),
        )
        self.assertEqual(command_result["command"]["result"]["outcome"], "completed")
        self.assertEqual(replace_status, 200)
        self.assertEqual(
            launched["snapshot"]["starting_revision"]["revision_id"],
            authored_option["revision"]["revision_id"],
        )
        self.assertEqual(
            replaced["snapshot"]["starting_revision"]["revision_id"],
            forged_option["revision"]["revision_id"],
        )
        self.assertNotIn("source", launched["snapshot"]["stage"])
        self.assertNotIn("source", replaced["snapshot"]["stage"])
        self.assertTrue(replaced["snapshot"]["stage"]["xray_available"])
        validate_schema(
            xray, "openapi.json#/components/schemas/StageXraySnapshotResponse"
        )
        validate_schema(
            accepted, "openapi.json#/components/schemas/SubmitCommandResponse"
        )
        validate_schema(
            command_result,
            "openapi.json#/components/schemas/CommandResultResponse",
        )
        validate_schema(
            replaced,
            "openapi.json#/components/schemas/StageReplacementResponse",
        )

    def test_lost_exchange_response_retries_without_reprovisioning(self) -> None:
        with RunningPreview("launch-selection-exchange-response-lost") as running:
            _, capabilities, _ = request(running, "GET", "/product/v1/capabilities")
            epoch = capabilities["service_epoch"]
            body = json.dumps(
                {"launch_selection_id": PREVIEW.AUTHORED_SELECTION_ID},
                separators=(",", ":"),
            ).encode("utf-8")
            connection = http.client.HTTPConnection(
                "127.0.0.1", running.server.server_address[1], timeout=5
            )
            connection.request(
                "POST",
                PREVIEW.LAUNCH_SELECTIONS_PATH,
                body=body,
                headers={
                    "Authorization": f"Bearer {SERVICE_BEARER}",
                    "Content-Type": "application/json",
                    **stage_headers(epoch, key="lost-exchange-response-0001"),
                },
            )
            with self.assertRaises(http.client.RemoteDisconnected):
                connection.getresponse()
            connection.close()
            retry_status, retry, _ = exchange(
                running,
                epoch,
                PREVIEW.AUTHORED_SELECTION_ID,
                "lost-exchange-response-0001",
            )
            launch_status, launched, _ = request(
                running,
                "POST",
                PREVIEW._STAGE.STAGE_LAUNCH_PATH,
                headers=stage_headers(epoch, key="lost-exchange-launch-0001"),
                body={"launch_grant": retry["launch_grant"]},
            )

        self.assertEqual((retry_status, launch_status), (200, 201))
        self.assertTrue(retry["replayed"])
        self.assertEqual(launched["kind"], "stage_session_created")

    def test_provider_free_preview_does_not_fake_legacy_forge_handoff(self) -> None:
        with RunningPreview() as running:
            _, capabilities, _ = request(running, "GET", "/product/v1/capabilities")
            epoch = capabilities["service_epoch"]
            status, problem, _ = request(
                running,
                "POST",
                PREVIEW._COMBINED.FORGE_HANDOFF_PATH,
                headers={
                    **stage_headers(epoch, key="legacy-forge-unavailable-0001"),
                    PREVIEW._COMBINED.FORGE_EXPECTED_EPOCH_HEADER: (
                        "forge-epoch:provider_free_preview_0001"
                    ),
                },
                body={"forge_reference": "forge-reference:provider-free-0001"},
            )

        self.assertEqual(status, 404)
        self.assertEqual(problem["error"]["code"], "selected_record_unavailable")
        self.assertFalse(problem["preview"]["provider_calls"])

    def test_once_subprocess_writes_complete_response_and_exits(self) -> None:
        with subprocess.Popen(
            [
                sys.executable,
                str(TOOL_PATH),
                "--token",
                SERVICE_BEARER,
                "--contract-root",
                str(CONTRACT_ROOT),
                "--once",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            assert process.stdout is not None
            announcement = json.loads(process.stdout.readline())
            try:
                http_request = Request(
                    f"http://127.0.0.1:{announcement['port']}/product/v1/capabilities",
                    headers={"Authorization": f"Bearer {SERVICE_BEARER}"},
                    method="GET",
                )
                with urlopen(http_request, timeout=10) as response:
                    status = response.status
                    headers = response.headers
                    payload = json.loads(response.read())
                return_code = process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            stderr = process.stderr.read() if process.stderr is not None else ""

        self.assertEqual(return_code, 0, stderr)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["X-Dreamland-Contract-Revision"], "0.7.0-rc.1")
        self.assertEqual(payload["kind"], "product_capabilities")
        self.assertFalse(announcement["provider_calls"])
        self.assertFalse(announcement["normal_runtime_composition"])


if __name__ == "__main__":
    unittest.main()
