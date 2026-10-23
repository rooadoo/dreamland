from __future__ import annotations

import importlib.util
import json
import select
import socket
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_stage_preview.py"
CONTRACT_TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_stage_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.4.0-rc.1"
GENERATED_CLIENT_PATH = CONTRACT_ROOT / "generated" / "product_session_client.py"
SERVICE_BEARER = "private-stage-preview-service-bearer"


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


PREVIEW = _load_module(TOOL_PATH, "dreamland_product_stage_preview_test_tool")
CONTRACT = _load_module(
    CONTRACT_TOOL_PATH, "dreamland_product_stage_contract_test_tool"
)
GENERATED_CLIENT = _load_module(
    GENERATED_CLIENT_PATH,
    "dreamland_generated_stage_client_test_tool",
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
        raise AssertionError(f"{target} rejected Stage preview value: {errors[0]}")


class RunningPreview:
    def __init__(self, scenario: str = "stage-happy") -> None:
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


class ProductStagePreviewTests(unittest.TestCase):
    def test_contract_and_capabilities_remain_unfrozen_disabled_preview(self) -> None:
        validation = CONTRACT.validate_contract(CONTRACT_ROOT)
        self.assertEqual(validation["contract_revision"], "0.4.0-rc.1")
        self.assertEqual(validation["routes"], 15)
        self.assertEqual(validation["capabilities_enabled"], 0)

        with RunningPreview() as running:
            status, payload, headers = request(
                running,
                "GET",
                "/product/v1/capabilities",
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Dreamland-Contract-Revision"], "0.4.0-rc.1")
        self.assertTrue(
            all(not value["enabled"] for value in payload["capabilities"].values())
        )
        validate_schema(
            payload, "openapi.json#/components/schemas/ProductCapabilitiesResponse"
        )

    def test_stage_launch_fresh_resume_and_selection_conflict(self) -> None:
        with RunningPreview() as running:
            _, capabilities, _ = request(running, "GET", "/product/v1/capabilities")
            epoch = capabilities["service_epoch"]
            status, created, _ = request(
                running,
                "POST",
                PREVIEW.STAGE_LAUNCH_PATH,
                headers=stage_headers(epoch, key="stage-launch-key-0001"),
                body={"launch_grant": PREVIEW.STAGE_CREATE_GRANT},
            )
            replay_status, replay, _ = request(
                running,
                "POST",
                PREVIEW.STAGE_LAUNCH_PATH,
                headers=stage_headers(epoch, key="stage-launch-key-0001"),
                body={"launch_grant": PREVIEW.STAGE_CREATE_GRANT},
            )
            resume_status, resumed, _ = request(
                running,
                "POST",
                PREVIEW.STAGE_LAUNCH_PATH,
                headers=stage_headers(epoch, key="stage-resume-key-0001"),
                body={"launch_grant": PREVIEW.STAGE_CREATE_GRANT},
            )
            conflict_status, conflict, _ = request(
                running,
                "POST",
                PREVIEW.STAGE_LAUNCH_PATH,
                headers=stage_headers(epoch, key="stage-conflict-key-0001"),
                body={"launch_grant": PREVIEW.STAGE_ALTERNATE_GRANT},
            )

        self.assertEqual((status, replay_status, resume_status), (201, 201, 201))
        self.assertEqual(created["kind"], "stage_session_created")
        self.assertTrue(replay["replayed"])
        self.assertEqual(resumed["kind"], "stage_session_resumed")
        self.assertEqual(
            created["snapshot"]["session_id"], resumed["snapshot"]["session_id"]
        )
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error"]["code"], "current_session_exists")
        self.assertEqual(
            conflict["error"]["context"]["requested_selected_record_id"],
            PREVIEW.STAGE_SELECTION_ALTERNATE,
        )
        validate_schema(created, "openapi.json#/components/schemas/StageLaunchResponse")
        validate_schema(conflict, "openapi.json#/components/schemas/SafeErrorEnvelope")

    def test_stage_uses_shared_command_and_explicit_replace_routes(self) -> None:
        with RunningPreview() as running:
            _, capabilities, _ = request(running, "GET", "/product/v1/capabilities")
            epoch = capabilities["service_epoch"]
            _, created, _ = request(
                running,
                "POST",
                PREVIEW.STAGE_LAUNCH_PATH,
                headers=stage_headers(epoch, key="stage-launch-key-0002"),
                body={"launch_grant": PREVIEW.STAGE_CREATE_GRANT},
            )
            snapshot = created["snapshot"]
            reference = snapshot["reference"]
            session_headers = {
                **stage_headers(epoch, key="stage-command-key-0001"),
                "X-Dreamland-Session-Capability": created["session_authority"],
            }
            command_status, accepted, _ = request(
                running,
                "POST",
                f"/product/v1/sessions/{quote(reference, safe='')}/commands",
                headers=session_headers,
                body={"command": "whoami", "expected_slot_generation": 0},
            )
            deadline = __import__("time").monotonic() + 5
            while True:
                result_status, result, _ = request(
                    running,
                    "GET",
                    f"/product/v1/sessions/{quote(reference, safe='')}/commands/{quote(accepted['command_id'], safe='')}",
                    headers={
                        **stage_headers(epoch),
                        "X-Dreamland-Session-Capability": created["session_authority"],
                    },
                )
                if result["terminal"]:
                    break
                if __import__("time").monotonic() >= deadline:
                    self.fail("Stage command did not become terminal")
            current_status, current, _ = request(
                running,
                "GET",
                PREVIEW.STAGE_CURRENT_PATH,
                headers=stage_headers(epoch),
            )
            replace_status, replaced, _ = request(
                running,
                "POST",
                f"{PREVIEW.STAGE_SESSION_PREFIX}{quote(reference, safe='')}/replace",
                headers={
                    **stage_headers(epoch, key="stage-replace-key-0001"),
                    "X-Dreamland-Session-Capability": created["session_authority"],
                },
                body={
                    "expected_control_revision": current["snapshot"]["coordinates"][
                        "control_revision"
                    ],
                    "launch_grant": PREVIEW.STAGE_REPLACE_GRANT,
                },
            )

        self.assertEqual(command_status, 202)
        self.assertEqual(result_status, 200)
        self.assertEqual(result["command"]["result"]["outcome"], "completed")
        self.assertEqual(current_status, 200)
        self.assertEqual(replace_status, 200)
        self.assertEqual(replaced["kind"], "stage_session_replaced")
        self.assertEqual(
            replaced["snapshot"]["starting_revision"]["revision_id"],
            PREVIEW.STAGE_REVISION_B,
        )
        validate_schema(
            replaced,
            "openapi.json#/components/schemas/StageReplacementResponse",
        )

    def test_generated_server_side_client_drives_stage_launch(self) -> None:
        with RunningPreview() as running:
            client = GENERATED_CLIENT.ProductSessionPreviewClient(
                running.base_url,
                SERVICE_BEARER,
            )
            capabilities = client.capabilities()
            created = client.launch_stage(
                epoch=capabilities["service_epoch"],
                actor=PREVIEW.PRESENTER_CONTEXT,
                key="generated-stage-launch-0001",
                launch_grant=PREVIEW.STAGE_CREATE_GRANT,
            )
            current = client.current_stage(
                epoch=capabilities["service_epoch"],
                actor=PREVIEW.PRESENTER_CONTEXT,
            )

        self.assertEqual(created["kind"], "stage_session_created")
        self.assertEqual(
            current["snapshot"]["session_id"], created["snapshot"]["session_id"]
        )

    def test_lost_stage_launch_response_replays_without_second_allocation(self) -> None:
        with RunningPreview("stage-launch-response-lost") as running:
            _, capabilities, _ = request(running, "GET", "/product/v1/capabilities")
            epoch = capabilities["service_epoch"]
            with self.assertRaises((URLError, ConnectionError, OSError)):
                request(
                    running,
                    "POST",
                    PREVIEW.STAGE_LAUNCH_PATH,
                    headers=stage_headers(epoch, key="lost-stage-launch-0001"),
                    body={"launch_grant": PREVIEW.STAGE_CREATE_GRANT},
                )
            status, replay, _ = request(
                running,
                "POST",
                PREVIEW.STAGE_LAUNCH_PATH,
                headers=stage_headers(epoch, key="lost-stage-launch-0001"),
                body={"launch_grant": PREVIEW.STAGE_CREATE_GRANT},
            )

        self.assertEqual(status, 201)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["kind"], "stage_session_created")

    def test_preview_is_loopback_only(self) -> None:
        with self.assertRaises(ValueError):
            PREVIEW.build_preview_server(
                host="0.0.0.0",
                port=0,
                bearer=SERVICE_BEARER,
                scenario="stage-happy",
                contract_root=CONTRACT_ROOT,
            )

    def test_once_writes_complete_response_and_exits_cleanly(self) -> None:
        command = [
            str(REPOSITORY_ROOT / ".venv/bin/python"),
            str(TOOL_PATH),
            "--token",
            SERVICE_BEARER,
            "--contract-root",
            str(CONTRACT_ROOT),
            "--once",
        ]
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            readable, _, _ = select.select([process.stdout], [], [], 15)
            self.assertTrue(readable)
            startup = json.loads(process.stdout.readline())
            with socket.create_connection(
                (startup["host"], startup["port"]), timeout=5
            ) as connection:
                connection.sendall(
                    (
                        "GET /product/v1/capabilities HTTP/1.1\r\n"
                        f"Host: {startup['host']}:{startup['port']}\r\n"
                        f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                        "Accept: application/json\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                )
                response = bytearray()
                while chunk := connection.recv(4_096):
                    response.extend(chunk)
            self.assertIn(b"HTTP/1.1 200 OK\r\n", response)
            header, separator, body = bytes(response).partition(b"\r\n\r\n")
            self.assertTrue(separator)
            content_length = next(
                int(line.partition(b":")[2].strip())
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            self.assertEqual(len(body), content_length)
            payload = json.loads(body)
            self.assertEqual(payload["contract"]["revision"], "0.4.0-rc.1")
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
