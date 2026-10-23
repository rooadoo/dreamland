import importlib.util
import json
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_core_preview.py"
CONTRACT_TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_session_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.3.0-rc.1"
GENERATED_CLIENT_PATH = CONTRACT_ROOT / "generated" / "product_session_client.py"
SERVICE_BEARER = "private-core-preview-service-bearer"


def _load_module(path: Path, name: str):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {name}")
    module = importlib.util.module_from_spec(specification)
    prior_module = sys.modules.get(name)
    prior_dont_write_bytecode = sys.dont_write_bytecode
    sys.modules[name] = module
    sys.dont_write_bytecode = True
    try:
        specification.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prior_dont_write_bytecode
        if prior_module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prior_module
    return module


PREVIEW = _load_module(TOOL_PATH, "dreamland_product_core_preview_tool")
CONTRACT = _load_module(
    CONTRACT_TOOL_PATH,
    "dreamland_product_session_contract_tool_for_preview_test",
)
GENERATED_CLIENT = _load_module(
    GENERATED_CLIENT_PATH,
    "dreamland_generated_product_session_client_for_preview_test",
)
OPENAPI = json.loads((CONTRACT_ROOT / "openapi.json").read_text(encoding="utf-8"))
EVENTS_SCHEMA = json.loads(
    (CONTRACT_ROOT / "events.schema.json").read_text(encoding="utf-8")
)
SCHEMA_DOCUMENTS = {
    "openapi.json": OPENAPI,
    "events.schema.json": EVENTS_SCHEMA,
}


def validate_schema(value: object, target: str) -> None:
    schema, _label = CONTRACT._schema_target(target, SCHEMA_DOCUMENTS)
    if schema is None:
        raise AssertionError(f"schema target is not JSON Schema: {target}")
    errors = CONTRACT._validation_errors(schema, value)
    if errors:
        raise AssertionError(f"{target} rejected live preview value: {errors[0]}")


def response_schema(method: str, path: str, status: int) -> str:
    if status >= 400:
        return "SafeErrorEnvelope"
    route = path.partition("?")[0]
    if route == PREVIEW.CAPABILITIES_PATH:
        return "ProductCapabilitiesResponse"
    if route == PREVIEW.SESSIONS_PATH and method == "POST":
        return "CreateSessionResponse"
    if route == PREVIEW.CURRENT_PATH:
        return "CurrentSessionResponse"
    if route.endswith("/cancel"):
        return "CancelCommandResponse"
    if route.endswith("/commands") and method == "POST":
        return "SubmitCommandResponse"
    if "/commands/" in route and method == "GET":
        return "CommandResultResponse"
    if route.endswith("/events"):
        return "EventPageResponse"
    if route.endswith("/transcript"):
        return "TranscriptPageResponse"
    if route.endswith(("/reset", "/switch")):
        return "ReplacementResponse"
    if route.endswith("/close"):
        return "CloseSessionResponse"
    return "SnapshotResponse"


def request_schema(path: str) -> str | None:
    route = path.partition("?")[0]
    if route == PREVIEW.SESSIONS_PATH:
        return "CreateSessionRequest"
    if route.endswith("/cancel"):
        return "CancelCommandRequest"
    if route.endswith("/commands"):
        return "SubmitCommandRequest"
    if route.endswith("/reset"):
        return "ResetSessionRequest"
    if route.endswith("/switch"):
        return "SwitchSessionRequest"
    if route.endswith("/close"):
        return "CloseSessionRequest"
    return None


class RunningPreview:
    def __init__(self, scenario: str = "core-happy") -> None:
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


class RunningPreviewProcess:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.process: subprocess.Popen[str] | None = None
        self.startup: dict[str, object] = {}
        self.base_url = ""

    def __enter__(self):
        command = [
            str(REPOSITORY_ROOT / ".venv/bin/python"),
            str(TOOL_PATH),
            "--token",
            SERVICE_BEARER,
            "--contract-root",
            str(CONTRACT_ROOT),
            "--scenario",
            self.scenario,
        ]
        self.process = subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if self.process.stdout is None:
            self._stop()
            raise AssertionError("product-core preview stdout was unavailable")
        readable, _, _ = select.select([self.process.stdout], [], [], 15)
        if not readable:
            self._stop()
            raise AssertionError("product-core preview did not report startup")
        startup_line = self.process.stdout.readline()
        if not startup_line:
            stderr = self.process.stderr.read() if self.process.stderr else ""
            self._stop()
            raise AssertionError(
                f"product-core preview failed before startup: {stderr}"
            )
        self.startup = json.loads(startup_line)
        if self.startup.get("scenario") != self.scenario:
            self._stop()
            raise AssertionError("product-core preview started another scenario")
        self.base_url = f"http://{self.startup['host']}:{self.startup['port']}"
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop()

    def _stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        self.process = None


def request_bytes(
    running: RunningPreview,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: object | None = None,
) -> tuple[int, bytes, object]:
    request_headers = {"Authorization": f"Bearer {SERVICE_BEARER}"}
    request_headers.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(
        running.base_url + path,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, response.read(), response.headers
    except HTTPError as error:
        return error.code, error.read(), error.headers


def raw_http_exchange(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.settimeout(5)
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        response = bytearray()
        while True:
            chunk = connection.recv(4_096)
            if not chunk:
                return bytes(response)
            response.extend(chunk)


def parse_raw_json_response(response: bytes) -> tuple[int, dict[str, str], dict]:
    header_bytes, separator, body = response.partition(b"\r\n\r\n")
    if not separator:
        raise AssertionError(f"raw response had no header terminator: {response!r}")
    lines = header_bytes.decode("ascii").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    headers = {
        name.strip().lower(): value.strip()
        for line in lines[1:]
        for name, value in (line.split(":", 1),)
    }
    if len(body) != int(headers["content-length"]):
        raise AssertionError("raw response body was incomplete")
    payload = json.loads(body)
    return status, headers, payload


def request_json(
    running: RunningPreview,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: object | None = None,
) -> tuple[int, dict, object]:
    status, raw, response_headers = request_bytes(
        running,
        method,
        path,
        headers=headers,
        body=body,
    )
    payload = json.loads(raw)
    validate_schema(
        payload,
        f"openapi.json#/components/schemas/{response_schema(method, path, status)}",
    )
    body_schema = request_schema(path)
    if body is not None and body_schema is not None:
        validate_schema(
            body,
            f"openapi.json#/components/schemas/{body_schema}",
        )
    if payload.get("contract", {}).get("revision") != PREVIEW.CONTRACT_REVISION:
        raise AssertionError(
            "JSON response did not identify the exact preview contract"
        )
    preview = payload.get("preview", {})
    if (
        preview.get("test_only") is not True
        or preview.get("integration_only") is not True
        or preview.get("live_capability_evidence") is not False
    ):
        raise AssertionError("JSON response overstated preview availability")
    if status < 400:
        if not str(payload.get("transport_request_id", "")).startswith("request:"):
            raise AssertionError("JSON success omitted its transport request identity")
    else:
        error = payload.get("error", {})
        if not str(error.get("request_id", "")).startswith("request:"):
            raise AssertionError("JSON problem omitted its transport request identity")
        expected = PREVIEW.SAFE_ERROR_DETAILS[error["code"]]
        actual = (
            status,
            error["message"],
            error["retry"],
            error["retry_after_ms"],
        )
        if actual != expected:
            raise AssertionError(f"noncanonical safe error tuple: {actual!r}")
    return status, payload, response_headers


def actor_headers(
    epoch: str,
    actor: str = PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
) -> dict[str, str]:
    return {
        PREVIEW.EXPECTED_EPOCH_HEADER: epoch,
        PREVIEW.ACTOR_CONTEXT_HEADER: actor,
    }


def mutation_headers(
    epoch: str,
    key: str,
    *,
    actor: str = PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
    session_capability: str | None = None,
) -> dict[str, str]:
    headers = {
        **actor_headers(epoch, actor),
        PREVIEW.IDEMPOTENCY_HEADER: key,
    }
    if session_capability is not None:
        headers[PREVIEW.SESSION_CAPABILITY_HEADER] = session_capability
    return headers


def session_headers(epoch: str, session_capability: str) -> dict[str, str]:
    return {
        **actor_headers(epoch),
        PREVIEW.SESSION_CAPABILITY_HEADER: session_capability,
    }


def session_path(reference: str, suffix: str = "") -> str:
    return f"{PREVIEW.SESSION_PATH_PREFIX}{quote(reference, safe='')}{suffix}"


def parse_sse(raw: bytes) -> list[dict[str, object]]:
    frames = []
    text = raw.decode("utf-8")
    for frame in text.split("\n\n"):
        if not frame:
            continue
        fields = {}
        for line in frame.splitlines():
            name, separator, value = line.partition(": ")
            if not separator:
                raise AssertionError(f"invalid SSE line: {line!r}")
            fields[name] = value
        event = json.loads(fields["data"])
        validate_schema(event, "events.schema.json#/$defs/ProductEvent")
        if fields["id"] != event["event_id"]:
            raise AssertionError("SSE ID differs from event payload")
        if fields["event"] != event["type"]:
            raise AssertionError("SSE type differs from event payload")
        frames.append(event)
    return frames


class ProductCorePreviewTests(unittest.TestCase):
    def capabilities(self, running: RunningPreview) -> tuple[str, dict]:
        status, payload, headers = request_json(
            running,
            "GET",
            PREVIEW.CAPABILITIES_PATH,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(
            headers.get("X-Dreamland-Contract-Revision"),
            PREVIEW.CONTRACT_REVISION,
        )
        self.assertEqual(headers.get("X-Dreamland-Integration-Preview"), "true")
        self.assertEqual(payload["contract"]["revision"], PREVIEW.CONTRACT_REVISION)
        self.assertEqual(payload["preview"]["executor"], PREVIEW.PREVIEW_EXECUTOR)
        self.assertFalse(payload["preview"]["provider_calls"])
        self.assertFalse(payload["preview"]["live_capability_evidence"])
        return payload["service_epoch"], payload

    def create(
        self,
        running: RunningPreview,
        epoch: str,
        *,
        key: str = "preview-core-create-key-0001",
        actor: str = PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
        grant: str = PREVIEW.PREVIEW_CREATE_GRANT_A,
    ) -> tuple[int, dict, object]:
        return request_json(
            running,
            "POST",
            PREVIEW.SESSIONS_PATH,
            headers=mutation_headers(epoch, key, actor=actor),
            body={"launch_grant": grant},
        )

    def submit(
        self,
        running: RunningPreview,
        epoch: str,
        reference: str,
        capability: str,
        *,
        key: str,
        command: str,
        generation: int,
    ) -> tuple[int, dict, object]:
        return request_json(
            running,
            "POST",
            session_path(reference, "/commands"),
            headers=mutation_headers(
                epoch,
                key,
                session_capability=capability,
            ),
            body={
                "command": command,
                "expected_slot_generation": generation,
            },
        )

    def wait_for_terminal(
        self,
        running: RunningPreview,
        epoch: str,
        reference: str,
        capability: str,
        command_id: str,
    ) -> dict:
        path = session_path(reference, f"/commands/{quote(command_id, safe='')}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, result, _headers = request_json(
                running,
                "GET",
                path,
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(status, 200)
            if result["terminal"]:
                return result
            time.sleep(0.01)
        self.fail("preview command did not become terminal")

    def test_contract_root_and_capability_projection_remain_unfrozen_and_disabled(self):
        self.assertEqual(PREVIEW.validate_contract_root(CONTRACT_ROOT), CONTRACT_ROOT)
        with RunningPreview() as running:
            _epoch, payload = self.capabilities(running)

        self.assertEqual(payload["admission"]["state"], "closed")
        self.assertEqual(payload["admission"]["reason"], "integration_preview_only")
        self.assertEqual(set(payload["capabilities"]), set(PREVIEW.CAPABILITY_NAMES))
        for capability in payload["capabilities"].values():
            self.assertFalse(capability["enabled"])
            self.assertIsNone(capability["enabled_revision"])
            self.assertEqual(capability["reason"], "integration_preview_only")

        with tempfile.TemporaryDirectory() as directory:
            altered_root = Path(directory) / "contract"
            shutil.copytree(CONTRACT_ROOT, altered_root)
            manifest_path = altered_root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["capabilities"]["product_session_core"]["enabled"] = True
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                PREVIEW.validate_contract_root(altered_root)

    def test_service_bearer_is_separate_and_errors_are_safe_preview_envelopes(self):
        with RunningPreview() as running:
            request = Request(running.base_url + PREVIEW.CAPABILITIES_PATH)
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=5)
            payload = json.load(caught.exception)

        validate_schema(
            payload,
            "openapi.json#/components/schemas/SafeErrorEnvelope",
        )
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(caught.exception.headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(payload["error"]["code"], "unauthorized")
        self.assertEqual(
            (
                caught.exception.code,
                payload["error"]["message"],
                payload["error"]["retry"],
                payload["error"]["retry_after_ms"],
            ),
            PREVIEW.SAFE_ERROR_DETAILS["unauthorized"],
        )
        self.assertEqual(payload["contract"]["revision"], PREVIEW.CONTRACT_REVISION)
        self.assertTrue(payload["preview"]["test_only"])
        self.assertNotIn(SERVICE_BEARER, json.dumps(payload))

    def test_transport_rejects_duplicate_body_framing_and_media_headers(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            port = running.server.server_address[1]
            body = json.dumps(
                {"launch_grant": PREVIEW.PREVIEW_CREATE_GRANT_A},
                separators=(",", ":"),
            ).encode("utf-8")
            common = (
                f"POST {PREVIEW.SESSIONS_PATH} HTTP/1.1\r\n"
                "Host: core-preview-duplicate-headers\r\n"
                f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                f"{PREVIEW.EXPECTED_EPOCH_HEADER}: {epoch}\r\n"
                f"{PREVIEW.ACTOR_CONTEXT_HEADER}: {PREVIEW.PREVIEW_ACTOR_A_CONTEXT}\r\n"
            )
            cases = (
                (
                    "duplicate-content-type",
                    "Content-Type: application/json\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n",
                ),
                (
                    "conflicting-content-type",
                    "Content-Type: application/json\r\n"
                    "Content-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\n",
                ),
                (
                    "duplicate-content-length",
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Content-Length: {len(body)}\r\n",
                ),
                (
                    "conflicting-content-length",
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Content-Length: 0\r\n",
                ),
            )
            for index, (label, body_headers) in enumerate(cases, start=1):
                with self.subTest(label=label):
                    request = (
                        common
                        + f"{PREVIEW.IDEMPOTENCY_HEADER}: core-duplicate-key-{index:04d}\r\n"
                        + body_headers
                        + "Connection: close\r\n\r\n"
                    ).encode("ascii") + body
                    response = raw_http_exchange(port, request)
                    status, headers, problem = parse_raw_json_response(response)
                    validate_schema(
                        problem,
                        "openapi.json#/components/schemas/SafeErrorEnvelope",
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(problem["error"]["code"], "invalid_request")
                    self.assertEqual(
                        headers["content-type"], "application/problem+json"
                    )

    def test_json_command_result_events_and_transcript_are_integratable(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            status, created, _ = self.create(running, epoch)
            self.assertEqual(status, 201)
            self.assertEqual(created["kind"], "session_created")
            reference = created["snapshot"]["reference"]
            capability = created["session_authority"]

            status, current, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers=actor_headers(epoch),
            )
            self.assertEqual(status, 200)
            self.assertEqual(current["kind"], "current_session")
            self.assertEqual(current["snapshot"]["reference"], reference)

            status, snapshot, _ = request_json(
                running,
                "GET",
                session_path(reference),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(status, 200)
            self.assertEqual(snapshot["kind"], "session_snapshot")
            self.assertEqual(
                snapshot["snapshot"]["coordinates"]["command_slot_generation"],
                0,
            )

            status, accepted, _ = self.submit(
                running,
                epoch,
                reference,
                capability,
                key="preview-core-submit-key-0001",
                command="  look around  ",
                generation=0,
            )
            self.assertEqual(status, 202)
            self.assertEqual(accepted["kind"], "command_accepted")
            self.assertEqual(accepted["canonical_command"], "look around")
            terminal = self.wait_for_terminal(
                running,
                epoch,
                reference,
                capability,
                accepted["command_id"],
            )
            self.assertEqual(terminal["command"]["status"], "completed")
            self.assertEqual(
                terminal["command"]["result"]["guest_termination"],
                {"kind": "exited", "exit_status": 0},
            )
            self.assertEqual(
                terminal["command"]["result"]["state_effect"]["kind"],
                "committed",
            )
            self.assertEqual(
                terminal["command"]["result"]["usage"]["state"],
                "settled",
            )

            status, events, _ = request_json(
                running,
                "GET",
                session_path(reference, "/events"),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(status, 200)
            self.assertEqual(events["kind"], "event_page")
            event_types = [event["type"] for event in events["events"]]
            self.assertIn("command.accepted", event_types)
            self.assertIn("command.completed", event_types)
            state_events = [
                event for event in events["events"] if event["type"] == "state.accepted"
            ]
            self.assertEqual(state_events[0]["data"]["source"], "foreground_command")

            status, transcript, _ = request_json(
                running,
                "GET",
                session_path(reference, "/transcript"),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(status, 200)
            self.assertEqual(transcript["kind"], "transcript_page")
            self.assertEqual(transcript["entries"][0]["kind"], "command")
            self.assertEqual(transcript["entries"][0]["text"], "look around")
            self.assertTrue(
                any(
                    entry["kind"] == "stdout"
                    and "integration preview accepted" in entry["text"]
                    for entry in transcript["entries"]
                )
            )

    def test_generated_server_side_client_drives_the_live_preview(self):
        with RunningPreview() as running:
            unauthorized = GENERATED_CLIENT.ProductSessionPreviewClient(
                running.base_url,
                "wrong-private-core-preview-service-bearer",
            )
            with self.assertRaises(
                GENERATED_CLIENT.ProductSessionPreviewError
            ) as denied:
                unauthorized.capabilities()
            self.assertEqual(denied.exception.status, 401)
            self.assertEqual(denied.exception.payload["error"]["code"], "unauthorized")

            client = GENERATED_CLIENT.ProductSessionPreviewClient(
                running.base_url,
                SERVICE_BEARER,
            )
            capabilities = client.capabilities()
            epoch = capabilities["service_epoch"]
            created = client.create(
                epoch=epoch,
                actor=PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                key="preview-generated-create-key-0001",
                launch_grant=PREVIEW.PREVIEW_CREATE_GRANT_A,
            )
            reference = created["snapshot"]["reference"]
            capability = created["session_authority"]
            snapshot = client.snapshot(
                epoch=epoch,
                actor=PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                capability=capability,
                reference=reference,
            )
            initial_transcript = client.transcript(
                epoch=epoch,
                actor=PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                capability=capability,
                reference=reference,
            )
            self.assertEqual(initial_transcript["position"]["position"], 0)
            self.assertIsNone(initial_transcript["position"]["entry_id"])
            normalized_initial_transcript = client.transcript(
                epoch=epoch,
                actor=PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                capability=capability,
                reference=reference,
                after=initial_transcript["position"],
            )
            self.assertEqual(
                normalized_initial_transcript["position"],
                initial_transcript["position"],
            )
            accepted = client.submit(
                epoch=epoch,
                actor=PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                capability=capability,
                reference=reference,
                key="preview-generated-submit-key-0001",
                command="generated client command",
                expected_slot_generation=snapshot["snapshot"]["coordinates"][
                    "command_slot_generation"
                ],
            )
            deadline = time.monotonic() + 5
            while True:
                result = client.command_result(
                    epoch=epoch,
                    actor=PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                    capability=capability,
                    reference=reference,
                    command_id=accepted["command_id"],
                )
                if result["terminal"]:
                    break
                if time.monotonic() >= deadline:
                    self.fail("generated client command did not become terminal")
                time.sleep(0.01)
            self.assertEqual(result["command"]["status"], "completed")
            events = client.events(
                epoch=epoch,
                actor=PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                capability=capability,
                reference=reference,
                after=accepted["event_cursor"],
            )
            self.assertTrue(events["events"])
            transcript = client.transcript(
                epoch=epoch,
                actor=PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                capability=capability,
                reference=reference,
            )
            self.assertEqual(
                transcript["entries"][0]["text"], "generated client command"
            )

    def test_exact_event_and_transcript_cursors_support_json_and_sse_replay(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            _, created, _ = self.create(running, epoch)
            reference = created["snapshot"]["reference"]
            capability = created["session_authority"]
            _, accepted, _ = self.submit(
                running,
                epoch,
                reference,
                capability,
                key="preview-core-cursor-submit-0001",
                command="observe",
                generation=0,
            )
            self.wait_for_terminal(
                running,
                epoch,
                reference,
                capability,
                accepted["command_id"],
            )

            cursor = accepted["event_cursor"]
            query = urlencode(
                {
                    "after_stream_id": cursor["stream_id"],
                    "after_sequence": cursor["sequence"],
                    "after_event_id": cursor["event_id"],
                }
            )
            status, replay, _ = request_json(
                running,
                "GET",
                session_path(reference, f"/events?{query}"),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(status, 200)
            self.assertTrue(replay["events"])
            self.assertTrue(
                all(
                    event["sequence"] > cursor["sequence"] for event in replay["events"]
                )
            )

            status, raw_sse, headers = request_bytes(
                running,
                "GET",
                session_path(reference, "/events"),
                headers={
                    **session_headers(epoch, capability),
                    "Accept": "text/event-stream",
                    PREVIEW.LAST_EVENT_ID_HEADER: cursor["event_id"],
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                headers.get_content_type(),
                "text/event-stream",
            )
            sse_events = parse_sse(raw_sse)
            self.assertEqual(
                [event["event_id"] for event in sse_events],
                [event["event_id"] for event in replay["events"]],
            )

            status, negotiated_json, negotiated_json_headers = request_json(
                running,
                "GET",
                session_path(reference, "/events"),
                headers={
                    **session_headers(epoch, capability),
                    "Accept": "*/*;q=1, text/event-stream;q=0",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                negotiated_json_headers.get_content_type(),
                "application/json",
            )
            self.assertEqual(negotiated_json["kind"], "event_page")

            status, weighted_sse, weighted_sse_headers = request_bytes(
                running,
                "GET",
                session_path(reference, "/events"),
                headers={
                    **session_headers(epoch, capability),
                    "Accept": "application/json;q=0.2, text/event-stream;q=0.8",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                weighted_sse_headers.get_content_type(),
                "text/event-stream",
            )
            self.assertTrue(parse_sse(weighted_sse))

            status, excluded, _ = request_json(
                running,
                "GET",
                session_path(reference, "/events"),
                headers={
                    **session_headers(epoch, capability),
                    "Accept": "text/event-stream;q=0",
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(excluded["error"]["code"], "invalid_request")

            status, json_last_event, _ = request_json(
                running,
                "GET",
                session_path(reference, "/events"),
                headers={
                    **session_headers(epoch, capability),
                    "Accept": "application/json",
                    PREVIEW.LAST_EVENT_ID_HEADER: cursor["event_id"],
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                json_last_event["error"]["code"],
                "invalid_request",
            )

            explicit_initial_event_query = urlencode(
                {
                    "after_stream_id": cursor["stream_id"],
                    "after_sequence": 0,
                    "after_event_id": "",
                }
            )
            status, explicit_initial_event, _ = request_json(
                running,
                "GET",
                session_path(
                    reference,
                    f"/events?{explicit_initial_event_query}",
                ),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                explicit_initial_event["error"]["code"],
                "invalid_request",
            )

            mismatched_event_query = urlencode(
                {
                    "after_stream_id": cursor["stream_id"],
                    "after_sequence": cursor["sequence"],
                    "after_event_id": (
                        f"event:{cursor['stream_id'].removeprefix('stream:')}:"
                        f"{cursor['sequence'] + 1}"
                    ),
                }
            )
            status, mismatched_event, _ = request_json(
                running,
                "GET",
                session_path(reference, f"/events?{mismatched_event_query}"),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                mismatched_event["error"]["code"],
                "invalid_request",
            )

            status, transcript, _ = request_json(
                running,
                "GET",
                session_path(reference, "/transcript"),
                headers=session_headers(epoch, capability),
            )
            first = transcript["entries"][0]
            transcript_query = urlencode(
                {
                    "after_transcript_id": first["transcript_id"],
                    "after_position": first["position"],
                    "after_entry_id": first["entry_id"],
                }
            )
            status, transcript_replay, _ = request_json(
                running,
                "GET",
                session_path(reference, f"/transcript?{transcript_query}"),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                transcript_replay["entries"],
                transcript["entries"][1:],
            )

            explicit_initial_transcript_query = urlencode(
                {
                    "after_transcript_id": first["transcript_id"],
                    "after_position": 0,
                    "after_entry_id": "",
                }
            )
            status, explicit_initial_transcript, _ = request_json(
                running,
                "GET",
                session_path(
                    reference,
                    f"/transcript?{explicit_initial_transcript_query}",
                ),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                explicit_initial_transcript["error"]["code"],
                "invalid_request",
            )

            mismatched_transcript_query = urlencode(
                {
                    "after_transcript_id": first["transcript_id"],
                    "after_position": first["position"],
                    "after_entry_id": (
                        f"entry:{first['transcript_id'].removeprefix('transcript:')}:"
                        f"{first['position'] + 1}"
                    ),
                }
            )
            status, mismatched_transcript, _ = request_json(
                running,
                "GET",
                session_path(
                    reference,
                    f"/transcript?{mismatched_transcript_query}",
                ),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                mismatched_transcript["error"]["code"],
                "invalid_request",
            )

            status, ambiguous, _ = request_json(
                running,
                "GET",
                session_path(reference, f"/events?{query}"),
                headers={
                    **session_headers(epoch, capability),
                    PREVIEW.LAST_EVENT_ID_HEADER: cursor["event_id"],
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(ambiguous["error"]["code"], "invalid_request")

            oversized_sequence = PREVIEW.MAX_CONTROL_REVISION + 1
            oversized_query = urlencode(
                {
                    "after_stream_id": cursor["stream_id"],
                    "after_sequence": oversized_sequence,
                    "after_event_id": (
                        f"event:{cursor['stream_id'].removeprefix('stream:')}:"
                        f"{oversized_sequence}"
                    ),
                }
            )
            status, oversized, _ = request_json(
                running,
                "GET",
                session_path(reference, f"/events?{oversized_query}"),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(status, 400)
            self.assertEqual(oversized["error"]["code"], "invalid_request")

            status, oversized_header, _ = request_json(
                running,
                "GET",
                session_path(reference, "/events"),
                headers={
                    **session_headers(epoch, capability),
                    PREVIEW.LAST_EVENT_ID_HEADER: (
                        f"event:{cursor['stream_id'].removeprefix('stream:')}:"
                        f"{oversized_sequence}"
                    ),
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(oversized_header["error"]["code"], "invalid_request")

    def test_cancel_acknowledgement_is_replayable_and_terminal_truth_is_separate(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            _, created, _ = self.create(running, epoch)
            reference = created["snapshot"]["reference"]
            capability = created["session_authority"]
            _, accepted, _ = self.submit(
                running,
                epoch,
                reference,
                capability,
                key="preview-core-hold-submit-0001",
                command="preview-hold",
                generation=0,
            )
            cancel_path = session_path(
                reference,
                f"/commands/{quote(accepted['command_id'], safe='')}/cancel",
            )
            headers = mutation_headers(
                epoch,
                "preview-core-cancel-key-0001",
                session_capability=capability,
            )
            body = {"slot_generation": accepted["slot_generation"]}
            status, cancelled, _ = request_json(
                running,
                "POST",
                cancel_path,
                headers=headers,
                body=body,
            )
            self.assertEqual(status, 200)
            self.assertEqual(cancelled["outcome"], "cancel_requested")
            self.assertFalse(cancelled["replayed"])
            terminal = self.wait_for_terminal(
                running,
                epoch,
                reference,
                capability,
                accepted["command_id"],
            )
            self.assertEqual(terminal["command"]["status"], "interrupted")

            status, replay, _ = request_json(
                running,
                "POST",
                cancel_path,
                headers=headers,
                body=body,
            )
            self.assertEqual(status, 200)
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["outcome"], "cancel_requested")

    def test_idempotency_and_compound_rejection_are_authoritative_at_http_boundary(
        self,
    ):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            _, created, _ = self.create(running, epoch)
            reference = created["snapshot"]["reference"]
            capability = created["session_authority"]
            key = "preview-core-submit-replay-0001"
            _, accepted, _ = self.submit(
                running,
                epoch,
                reference,
                capability,
                key=key,
                command="whoami",
                generation=0,
            )
            status, replay, _ = self.submit(
                running,
                epoch,
                reference,
                capability,
                key=key,
                command="whoami",
                generation=0,
            )
            self.assertEqual(status, 202)
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["command_id"], accepted["command_id"])
            status, conflict, _ = self.submit(
                running,
                epoch,
                reference,
                capability,
                key=key,
                command="hostname",
                generation=0,
            )
            self.assertEqual(status, 409)
            self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
            self.wait_for_terminal(
                running,
                epoch,
                reference,
                capability,
                accepted["command_id"],
            )

            status, snapshot, _ = request_json(
                running,
                "GET",
                session_path(reference),
                headers=session_headers(epoch, capability),
            )
            next_generation = snapshot["snapshot"]["coordinates"][
                "command_slot_generation"
            ]
            status, rejected, _ = self.submit(
                running,
                epoch,
                reference,
                capability,
                key="preview-core-compound-key-0001",
                command="one | two",
                generation=next_generation,
            )
            self.assertEqual(status, 422)
            self.assertEqual(
                rejected["error"]["code"],
                "compound_syntax_unsupported",
            )
            _, unchanged, _ = request_json(
                running,
                "GET",
                session_path(reference),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(
                unchanged["snapshot"]["coordinates"]["command_slot_generation"],
                next_generation,
            )

    def test_reset_switch_and_close_rotate_authority_after_successful_cutover(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            _, created, _ = self.create(running, epoch)
            reference = created["snapshot"]["reference"]
            capability = created["session_authority"]

            status, reset, _ = request_json(
                running,
                "POST",
                session_path(reference, "/reset"),
                headers=mutation_headers(
                    epoch,
                    "preview-core-reset-key-0001",
                    session_capability=capability,
                ),
                body={"expected_control_revision": 1},
            )
            self.assertEqual(status, 200)
            self.assertEqual(reset["kind"], "session_reset")
            self.assertEqual(
                reset["previous_snapshot"]["lifecycle"]["state"], "replaced"
            )
            reset_reference = reset["snapshot"]["reference"]
            reset_capability = reset["session_authority"]
            self.assertNotEqual(reset_reference, reference)
            self.assertNotEqual(reset_capability, capability)

            status, switched, _ = request_json(
                running,
                "POST",
                session_path(reset_reference, "/switch"),
                headers=mutation_headers(
                    epoch,
                    "preview-core-switch-key-0001",
                    session_capability=reset_capability,
                ),
                body={
                    "expected_control_revision": 1,
                    "launch_grant": PREVIEW.PREVIEW_SWITCH_GRANT_A,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(switched["kind"], "session_switched")
            self.assertEqual(
                switched["snapshot"]["starting_revision"]["revision_id"],
                PREVIEW.PREVIEW_REVISION_B,
            )
            switched_reference = switched["snapshot"]["reference"]
            switched_capability = switched["session_authority"]

            close_headers = mutation_headers(
                epoch,
                "preview-core-close-key-0001",
                session_capability=switched_capability,
            )
            close_body = {"expected_control_revision": 1}
            close_path = session_path(switched_reference, "/close")
            status, closed, _ = request_json(
                running,
                "POST",
                close_path,
                headers=close_headers,
                body=close_body,
            )
            self.assertEqual(status, 200)
            self.assertEqual(closed["kind"], "session_closed")
            self.assertEqual(closed["snapshot"]["lifecycle"]["state"], "closed")
            status, replay, _ = request_json(
                running,
                "POST",
                close_path,
                headers=close_headers,
                body=close_body,
            )
            self.assertEqual(status, 200)
            self.assertTrue(replay["replayed"])

    def test_cross_actor_authority_and_stale_epoch_fail_closed(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            _, created, _ = self.create(running, epoch)
            reference = created["snapshot"]["reference"]
            capability = created["session_authority"]

            status, denied, _ = request_json(
                running,
                "GET",
                session_path(reference),
                headers={
                    **actor_headers(epoch, PREVIEW.PREVIEW_ACTOR_B_CONTEXT),
                    PREVIEW.SESSION_CAPABILITY_HEADER: capability,
                },
            )
            self.assertEqual(status, 403)
            self.assertEqual(denied["error"]["code"], "authority_denied")

            stale_epoch = "epoch:" + "z" * 24
            status, lost, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers={PREVIEW.EXPECTED_EPOCH_HEADER: stale_epoch},
            )
            self.assertEqual(status, 410)
            self.assertEqual(lost["error"]["code"], "session_lost")
            status, unknown, _ = request_json(
                running,
                "POST",
                PREVIEW.SESSIONS_PATH,
                headers={PREVIEW.EXPECTED_EPOCH_HEADER: stale_epoch},
            )
            self.assertEqual(status, 410)
            self.assertEqual(unknown["error"]["code"], "outcome_unknown")

    def test_lost_command_acceptance_response_recovers_without_redispatch(self):
        with RunningPreviewProcess("command-accept-response-lost") as running:
            epoch, _ = self.capabilities(running)
            _, created, _ = self.create(running, epoch)
            reference = created["snapshot"]["reference"]
            capability = created["session_authority"]
            path = session_path(reference, "/commands")
            headers = mutation_headers(
                epoch,
                "preview-core-lost-accept-key-0001",
                session_capability=capability,
            )
            headers["Content-Type"] = "application/json"
            body = {
                "command": "recover acceptance",
                "expected_slot_generation": 0,
            }
            first_request = Request(
                running.base_url + path,
                data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {SERVICE_BEARER}",
                    **headers,
                },
                method="POST",
            )
            with self.assertRaises((RemoteDisconnected, URLError)):
                urlopen(first_request, timeout=5)

            status, replay, _ = request_json(
                running,
                "POST",
                path,
                headers=headers,
                body=body,
            )
            self.assertEqual(status, 202)
            self.assertTrue(replay["replayed"])
            terminal = self.wait_for_terminal(
                running,
                epoch,
                reference,
                capability,
                replay["command_id"],
            )
            self.assertTrue(terminal["terminal"])
            _, snapshot, _ = request_json(
                running,
                "GET",
                session_path(reference),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(
                snapshot["snapshot"]["latest_command"]["command_id"],
                replay["command_id"],
            )
            _, transcript, _ = request_json(
                running,
                "GET",
                session_path(reference, "/transcript"),
                headers=session_headers(epoch, capability),
            )
            command_entries = [
                entry for entry in transcript["entries"] if entry["kind"] == "command"
            ]
            self.assertEqual(len(command_entries), 1)

    def test_withheld_terminal_feed_response_recovers_by_same_key_query_and_snapshot(
        self,
    ):
        with RunningPreviewProcess("promised-terminal-event-withheld") as running:
            epoch, _ = self.capabilities(running)
            _, created, _ = self.create(running, epoch)
            reference = created["snapshot"]["reference"]
            capability = created["session_authority"]
            key = "preview-core-withheld-event-key-0001"
            _, accepted, _ = self.submit(
                running,
                epoch,
                reference,
                capability,
                key=key,
                command="recover terminal event",
                generation=0,
            )
            terminal = self.wait_for_terminal(
                running,
                epoch,
                reference,
                capability,
                accepted["command_id"],
            )
            self.assertTrue(terminal["terminal"])

            event_request = Request(
                running.base_url + session_path(reference, "/events"),
                headers={
                    "Authorization": f"Bearer {SERVICE_BEARER}",
                    **session_headers(epoch, capability),
                    "Accept": "text/event-stream",
                },
                method="GET",
            )
            with self.assertRaises((RemoteDisconnected, URLError)):
                urlopen(event_request, timeout=5)

            status, same_key, _ = self.submit(
                running,
                epoch,
                reference,
                capability,
                key=key,
                command="recover terminal event",
                generation=0,
            )
            self.assertEqual(status, 202)
            self.assertTrue(same_key["replayed"])
            self.assertEqual(same_key["command_id"], accepted["command_id"])
            queried = self.wait_for_terminal(
                running,
                epoch,
                reference,
                capability,
                accepted["command_id"],
            )
            self.assertEqual(queried["command"]["status"], "completed")
            _, snapshot, _ = request_json(
                running,
                "GET",
                session_path(reference),
                headers=session_headers(epoch, capability),
            )
            self.assertEqual(
                snapshot["snapshot"]["latest_command"]["command_id"],
                accepted["command_id"],
            )

            status, raw_sse, _ = request_bytes(
                running,
                "GET",
                session_path(reference, "/events"),
                headers={
                    **session_headers(epoch, capability),
                    "Accept": "text/event-stream",
                },
            )
            self.assertEqual(status, 200)
            events = parse_sse(raw_sse)
            terminal_events = [
                event
                for event in events
                if event["type"]
                in {"command.completed", "command.interrupted", "command.failed"}
            ]
            self.assertEqual(len(terminal_events), 1)
            self.assertEqual(terminal_events[0]["command_id"], accepted["command_id"])

    def test_fault_scenario_cannot_be_selected_by_request(self):
        with RunningPreview("core-happy") as running:
            epoch, _ = self.capabilities(running)
            _, created, _ = self.create(running, epoch)
            reference = created["snapshot"]["reference"]
            capability = created["session_authority"]
            status, accepted, _ = request_json(
                running,
                "POST",
                session_path(reference, "/commands"),
                headers={
                    **mutation_headers(
                        epoch,
                        "preview-core-header-fault-key-0001",
                        session_capability=capability,
                    ),
                    "X-Dreamland-Preview-Scenario": "command-accept-response-lost",
                },
                body={"command": "still responds", "expected_slot_generation": 0},
            )
            self.assertEqual(status, 202)
            self.assertFalse(accepted["replayed"])
            status, problem, _ = request_json(
                running,
                "GET",
                f"{PREVIEW.CURRENT_PATH}?scenario=promised-terminal-event-withheld",
                headers=actor_headers(epoch),
            )
            self.assertEqual(status, 404)
            self.assertEqual(problem["error"]["code"], "not_found")

    def test_preview_is_loopback_only_and_rejects_noncanonical_contract(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            PREVIEW.build_preview_server(
                host="0.0.0.0",
                port=0,
                bearer=SERVICE_BEARER,
                scenario="core-happy",
                contract_root=CONTRACT_ROOT,
            )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                PREVIEW.validate_contract_root(Path(directory))

    def test_once_waits_for_complete_http11_response_and_exits_cleanly(self):
        command = [
            str(REPOSITORY_ROOT / ".venv/bin/python"),
            str(TOOL_PATH),
            "--token",
            SERVICE_BEARER,
            "--contract-root",
            str(CONTRACT_ROOT),
            "--scenario",
            "core-happy",
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
            if process.stdout is None:
                self.fail("product-core preview stdout was unavailable")
            readable, _, _ = select.select([process.stdout], [], [], 15)
            self.assertTrue(readable, "product-core preview did not report startup")
            startup_line = process.stdout.readline()
            if not startup_line:
                stderr = process.stderr.read() if process.stderr is not None else ""
                self.fail(f"product-core preview failed before startup: {stderr}")
            startup = json.loads(startup_line)
            self.assertEqual(startup["contract_revision"], PREVIEW.CONTRACT_REVISION)
            self.assertTrue(startup["integration_only"])
            self.assertFalse(startup["live_capability_evidence"])
            self.assertNotIn(SERVICE_BEARER, startup_line)
            self.assertNotIn(PREVIEW.PREVIEW_ACTOR_A_CONTEXT, startup_line)

            with socket.create_connection(
                (startup["host"], startup["port"]),
                timeout=5,
            ) as connection:
                connection.settimeout(5)
                # The old threaded --once race could exit after accepting this
                # connection but before its daemon handler wrote the response.
                time.sleep(0.1)
                connection.sendall(
                    (
                        f"GET {PREVIEW.CAPABILITIES_PATH} HTTP/1.1\r\n"
                        "Host: product-core-preview-once\r\n"
                        f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                        "Connection: keep-alive\r\n\r\n"
                    ).encode("ascii")
                )
                response_bytes = bytearray()
                while True:
                    chunk = connection.recv(4_096)
                    if not chunk:
                        break
                    response_bytes.extend(chunk)

            header_bytes, separator, body = bytes(response_bytes).partition(b"\r\n\r\n")
            self.assertTrue(separator, response_bytes)
            header_lines = header_bytes.decode("ascii").split("\r\n")
            status = int(header_lines[0].split(" ", 2)[1])
            headers = {
                name.strip().lower(): value.strip()
                for line in header_lines[1:]
                for name, value in (line.split(":", 1),)
            }
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("cache-control"), "no-store")
            self.assertEqual(headers.get("content-type"), "application/json")
            self.assertEqual(headers.get("content-length"), str(len(body)))
            self.assertEqual(headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(
                headers.get("x-dreamland-contract-revision"),
                PREVIEW.CONTRACT_REVISION,
            )
            self.assertEqual(headers.get("x-dreamland-integration-preview"), "true")
            self.assertEqual(headers.get("connection"), "close")
            payload = json.loads(body)
            validate_schema(
                payload,
                "openapi.json#/components/schemas/ProductCapabilitiesResponse",
            )
            self.assertEqual(payload["contract"]["revision"], PREVIEW.CONTRACT_REVISION)
            self.assertFalse(payload["preview"]["live_capability_evidence"])

            remaining_stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(remaining_stdout, "")
            self.assertEqual(stderr, "")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
