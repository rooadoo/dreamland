import importlib.util
import json
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.client import RemoteDisconnected
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator, FormatChecker

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_lifecycle_preview.py"
CONTRACT_TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_lifecycle_contract.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.2.0-rc.2"
SERVICE_BEARER = "private-preview-service-bearer"


def _load_module(path: Path, name: str):
    specification = importlib.util.spec_from_file_location(
        name,
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {name}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


PREVIEW = _load_module(TOOL_PATH, "dreamland_product_lifecycle_preview_tool")
OPENAPI = json.loads((CONTRACT_ROOT / "openapi.json").read_text(encoding="utf-8"))


def validate_schema(instance: object, schema_name: str) -> None:
    schema = dict(OPENAPI)
    schema["$ref"] = f"#/components/schemas/{schema_name}"
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)


def validate_safe_error(payload: dict) -> None:
    validate_schema(payload, "SafeErrorEnvelope")
    error = payload["error"]
    expected = PREVIEW.SAFE_ERROR_DETAILS[error["code"]]
    actual = (
        error["message"],
        error["retry"],
        error["retry_after_ms"],
    )
    if actual != expected:
        raise AssertionError(
            f"noncanonical safe error tuple for {error['code']}: {actual!r}"
        )


def validate_exchange(
    method: str,
    path: str,
    status: int,
    payload: object,
    body: object | None,
) -> None:
    if status >= 400:
        validate_safe_error(payload)
        return
    route = path.partition("?")[0]
    if route == PREVIEW.CAPABILITIES_PATH:
        response_schema = "ProductCapabilitiesResponse"
    elif route == PREVIEW.SESSIONS_PATH and method == "POST":
        if body is not None:
            validate_schema(body, "CreateLifecyclePreviewRequest")
        response_schema = "CreateLifecyclePreviewResponse"
    elif route == PREVIEW.CURRENT_PATH:
        response_schema = "CurrentLifecyclePreviewResponse"
    elif method == "POST" and route.endswith(PREVIEW.SESSION_CLOSE_SUFFIX):
        if body is not None:
            validate_schema(body, "CloseLifecyclePreviewRequest")
        response_schema = "CloseLifecyclePreviewResponse"
    else:
        response_schema = "LifecyclePreviewSnapshotResponse"
    validate_schema(payload, response_schema)


class RunningPreview:
    def __init__(self, scenario: str = "lifecycle-happy") -> None:
        self.server = PREVIEW.build_preview_server(
            host="127.0.0.1",
            port=0,
            bearer=SERVICE_BEARER,
            scenario=scenario,
            contract_root=CONTRACT_ROOT,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


def request_json(
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
    request = Request(
        running.base_url + path,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=2) as response:
            payload = json.load(response)
            validate_exchange(method, path, response.status, payload, body)
            return response.status, payload, response.headers
    except HTTPError as error:
        payload = json.load(error)
        validate_exchange(method, path, error.code, payload, body)
        return error.code, payload, error.headers


def raw_http_exchange(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.settimeout(2)
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        response = bytearray()
        while True:
            chunk = connection.recv(4_096)
            if not chunk:
                return bytes(response)
            response.extend(chunk)


def parse_raw_problem(response: bytes) -> tuple[int, dict[str, str], dict]:
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
    expected_length = int(headers["content-length"])
    payload = json.loads(body[:expected_length])
    validate_safe_error(payload)
    return status, headers, payload


def actor_headers(
    epoch: str,
    actor_context: str = PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
) -> dict[str, str]:
    return {
        PREVIEW.EXPECTED_EPOCH_HEADER: epoch,
        PREVIEW.ACTOR_CONTEXT_HEADER: actor_context,
    }


def mutation_headers(
    epoch: str,
    key: str,
    actor_context: str = PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
) -> dict[str, str]:
    return {
        **actor_headers(epoch, actor_context),
        PREVIEW.IDEMPOTENCY_HEADER: key,
    }


class ProductLifecyclePreviewTests(unittest.TestCase):
    def capabilities(self, running: RunningPreview) -> tuple[str, dict]:
        status, payload, headers = request_json(
            running,
            "GET",
            PREVIEW.CAPABILITIES_PATH,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        return payload["service_epoch"], payload

    def create(
        self,
        running: RunningPreview,
        epoch: str,
        *,
        key: str = "preview-create-key-0001",
        actor_context: str = PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
        grant: str = PREVIEW.PREVIEW_GRANT_A,
    ) -> tuple[int, dict, object]:
        return request_json(
            running,
            "POST",
            PREVIEW.SESSIONS_PATH,
            headers=mutation_headers(epoch, key, actor_context),
            body={"launch_grant": grant},
        )

    def test_capabilities_require_separate_service_bearer_and_stay_disabled(self):
        with RunningPreview() as running:
            request = Request(running.base_url + PREVIEW.CAPABILITIES_PATH)
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=2)
            unauthorized = json.load(caught.exception)
            validate_safe_error(unauthorized)
            epoch, payload = self.capabilities(running)

        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(caught.exception.headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(caught.exception.headers.get("Cache-Control"), "no-store")
        self.assertEqual(unauthorized["error"]["code"], "unauthorized")
        self.assertTrue(epoch.startswith("epoch:"))
        authority_fixture = json.loads(
            (
                CONTRACT_ROOT / "fixtures/canonical-valid/authority/actor-alpha.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            authority_fixture["expected_epoch_source"],
            "getProductCapabilities.service_epoch",
        )
        self.assertNotEqual(epoch, "epoch:phase1preview0001")
        self.assertEqual(set(payload["capabilities"]), set(PREVIEW.CAPABILITY_NAMES))
        self.assertEqual(payload["admission"]["state"], "closed")
        for capability in payload["capabilities"].values():
            self.assertFalse(capability["enabled"])
            self.assertIsNone(capability["enabled_revision"])

    def test_each_constructed_preview_has_a_fresh_service_epoch(self):
        first = PREVIEW.build_preview_server(
            host="127.0.0.1",
            port=0,
            bearer=SERVICE_BEARER,
            scenario="lifecycle-happy",
            contract_root=CONTRACT_ROOT,
        )
        second = PREVIEW.build_preview_server(
            host="127.0.0.1",
            port=0,
            bearer=SERVICE_BEARER,
            scenario="lifecycle-happy",
            contract_root=CONTRACT_ROOT,
        )
        try:
            self.assertNotEqual(
                first.preview.service.service_epoch,
                second.preview.service.service_epoch,
            )
        finally:
            first.server_close()
            second.server_close()

    def test_generated_client_normalizes_live_transport_and_domain_errors(self):
        contract_tool = _load_module(
            CONTRACT_TOOL_PATH,
            "dreamland_product_lifecycle_contract_tool_for_preview_test",
        )
        with tempfile.TemporaryDirectory() as directory:
            generated_path = Path(directory) / "product_lifecycle_client.py"
            contract_tool.client_smoke(CONTRACT_ROOT, output=generated_path)
            generated = _load_module(
                generated_path,
                "generated_product_lifecycle_client_for_live_preview_test",
            )
            with RunningPreview() as running:
                unauthorized_client = generated.ProductLifecycleClient(
                    running.base_url,
                    "wrong-preview-service-bearer",
                )
                with self.assertRaises(generated.ProductLifecycleError) as unauthorized:
                    unauthorized_client.get_product_capabilities()
                self.assertEqual(unauthorized.exception.status, 401)
                self.assertEqual(unauthorized.exception.error["code"], "unauthorized")

                client = generated.ProductLifecycleClient(
                    running.base_url,
                    SERVICE_BEARER,
                )
                capabilities = client.get_product_capabilities()
                epoch = capabilities["service_epoch"]
                with self.assertRaises(generated.ProductLifecycleError) as denied:
                    client.create_lifecycle_preview(
                        epoch,
                        PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                        "launch-grant:preview_missing_grant_00000001",
                        "preview-missing-grant-key-0001",
                    )
                self.assertEqual(denied.exception.status, 403)
                self.assertEqual(denied.exception.error["code"], "authority_denied")

                client.create_lifecycle_preview(
                    epoch,
                    PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                    PREVIEW.PREVIEW_GRANT_A,
                    "preview-client-create-key-0001",
                )
                with self.assertRaises(generated.ProductLifecycleError) as conflict:
                    client.create_lifecycle_preview(
                        epoch,
                        PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                        PREVIEW.PREVIEW_GRANT_A_SECONDARY,
                        "preview-client-create-key-0002",
                    )
                self.assertEqual(conflict.exception.status, 409)
                self.assertEqual(
                    conflict.exception.error["code"], "current_session_exists"
                )
                self.assertEqual(conflict.exception.error["retry"], "resume_snapshot")

    def test_http_idempotency_claim_boundary_replays_only_postclaim_outcomes(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)

            rejected_create_key = "preview-rejected-create-key-0001"
            rejected_create_headers = mutation_headers(
                epoch,
                rejected_create_key,
            )
            missing_grant = "launch-grant:preview_missing_grant_00000001"
            create_errors = []
            for _attempt in range(2):
                status, denied, _ = request_json(
                    running,
                    "POST",
                    PREVIEW.SESSIONS_PATH,
                    headers=rejected_create_headers,
                    body={"launch_grant": missing_grant},
                )
                self.assertEqual(status, 403)
                create_errors.append(
                    {
                        key: value
                        for key, value in denied["error"].items()
                        if key != "request_id"
                    }
                )
            self.assertEqual(create_errors[0], create_errors[1])

            status, changed_create, _ = request_json(
                running,
                "POST",
                PREVIEW.SESSIONS_PATH,
                headers=rejected_create_headers,
                body={"launch_grant": PREVIEW.PREVIEW_GRANT_A},
            )
            self.assertEqual(status, 409)
            self.assertEqual(
                changed_create["error"]["code"],
                "idempotency_conflict",
            )
            status, current, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers=actor_headers(epoch),
            )
            self.assertEqual(status, 200)
            self.assertEqual(current["result"], "none")

            reusable_create_key = "preview-preclaim-create-key-0001"
            status, malformed, _ = request_json(
                running,
                "POST",
                PREVIEW.SESSIONS_PATH,
                headers=mutation_headers(epoch, reusable_create_key),
                body={"launch_grant": "too-short"},
            )
            self.assertEqual(status, 400)
            self.assertEqual(malformed["error"]["code"], "invalid_request")
            status, created, _ = self.create(
                running,
                epoch,
                key=reusable_create_key,
            )
            self.assertEqual(status, 201)

            snapshot = created["snapshot"]
            reference = snapshot["reference"]
            close_path = (
                f"{PREVIEW.SESSION_PATH_PREFIX}{quote(reference, safe='')}"
                f"{PREVIEW.SESSION_CLOSE_SUFFIX}"
            )
            rejected_close_key = "preview-rejected-close-key-0001"
            rejected_close_headers = {
                **mutation_headers(epoch, rejected_close_key),
                PREVIEW.SESSION_CAPABILITY_HEADER: (
                    "session-cap:preview_missing_capability_00000001"
                ),
            }
            close_errors = []
            for _attempt in range(2):
                status, denied, _ = request_json(
                    running,
                    "POST",
                    close_path,
                    headers=rejected_close_headers,
                    body={"expected_control_revision": 1},
                )
                self.assertEqual(status, 403)
                close_errors.append(
                    {
                        key: value
                        for key, value in denied["error"].items()
                        if key != "request_id"
                    }
                )
            self.assertEqual(close_errors[0], close_errors[1])

            status, changed_close, _ = request_json(
                running,
                "POST",
                close_path,
                headers=rejected_close_headers,
                body={"expected_control_revision": 2},
            )
            self.assertEqual(status, 409)
            self.assertEqual(
                changed_close["error"]["code"],
                "idempotency_conflict",
            )

            reusable_close_key = "preview-preclaim-close-key-0001"
            close_headers = {
                **mutation_headers(epoch, reusable_close_key),
                PREVIEW.SESSION_CAPABILITY_HEADER: created["session_capability"],
            }
            status, malformed_close, _ = request_json(
                running,
                "POST",
                close_path,
                headers=close_headers,
                body={"expected_control_revision": 0},
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                malformed_close["error"]["code"],
                "invalid_request",
            )
            status, closed, _ = request_json(
                running,
                "POST",
                close_path,
                headers=close_headers,
                body={"expected_control_revision": 1},
            )
            self.assertEqual(status, 200)
            self.assertEqual(closed["result"], "closed")

    def test_happy_lifecycle_is_actor_scoped_idempotent_and_lifecycle_only(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            status, created, headers = self.create(running, epoch)
            self.assertEqual(status, 201)
            self.assertEqual(headers.get("Cache-Control"), "no-store")
            self.assertEqual(created["result"], "created")
            self.assertFalse(created["replayed"])
            snapshot = created["snapshot"]
            reference = snapshot["reference"]
            capability = created["session_capability"]

            self.assertEqual(snapshot["lifecycle"], {"state": "active"})
            self.assertEqual(snapshot["authority_class"], "attendee")
            self.assertEqual(snapshot["available_controls"], ["snapshot", "close"])
            self.assertEqual(
                snapshot["launch_context"],
                {
                    "policy_id": PREVIEW.PREVIEW_POLICY_ID,
                    "notice_version": PREVIEW.PREVIEW_NOTICE_VERSION,
                    "limits": {
                        "absolute_ttl_ms": 30 * PREVIEW.MILLISECONDS_PER_MINUTE,
                        "idle_ttl_ms": 10 * PREVIEW.MILLISECONDS_PER_MINUTE,
                    },
                },
            )
            self.assertEqual(
                snapshot["starting_world"]["immutable_revision"],
                PREVIEW.PREVIEW_REVISION_A,
            )
            for forbidden in (
                "accepted_state_revision",
                "command_slot_generation",
                "event_position",
                "transcript",
                "active_command",
            ):
                self.assertNotIn(forbidden, snapshot)

            status, replayed, _ = self.create(running, epoch)
            self.assertEqual(status, 201)
            self.assertTrue(replayed["replayed"])
            self.assertEqual(replayed["snapshot"]["session_id"], snapshot["session_id"])
            self.assertEqual(replayed["session_capability"], capability)

            status, key_conflict, _ = self.create(
                running,
                epoch,
                grant=PREVIEW.PREVIEW_GRANT_A_SECONDARY,
            )
            self.assertEqual(status, 409)
            self.assertEqual(key_conflict["error"]["code"], "idempotency_conflict")

            status, conflict, _ = self.create(
                running,
                epoch,
                key="preview-create-key-0002",
                grant=PREVIEW.PREVIEW_GRANT_A_SECONDARY,
            )
            self.assertEqual(status, 409)
            self.assertEqual(conflict["error"]["code"], "current_session_exists")
            self.assertEqual(conflict["error"]["retry"], "resume_snapshot")

            status, consumed, _ = self.create(
                running,
                epoch,
                key="preview-create-key-0003",
            )
            self.assertEqual(status, 403)
            self.assertEqual(consumed["error"]["code"], "authority_denied")

            status, current, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers=actor_headers(epoch),
            )
            self.assertEqual(status, 200)
            self.assertEqual(current["result"], "current_active")
            self.assertEqual(current["snapshot"]["reference"], reference)
            self.assertEqual(current["session_capability"], capability)

            status, read, _ = request_json(
                running,
                "GET",
                f"{PREVIEW.SESSION_PATH_PREFIX}{quote(reference, safe='')}",
                headers={
                    **actor_headers(epoch),
                    PREVIEW.SESSION_CAPABILITY_HEADER: capability,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(read["result"], "snapshot")
            self.assertEqual(read["snapshot"]["reference"], reference)

            status, denied, _ = request_json(
                running,
                "GET",
                f"{PREVIEW.SESSION_PATH_PREFIX}{quote(reference, safe='')}",
                headers={
                    **actor_headers(epoch, PREVIEW.PREVIEW_ACTOR_B_CONTEXT),
                    PREVIEW.SESSION_CAPABILITY_HEADER: capability,
                },
            )
            self.assertEqual(status, 403)
            self.assertEqual(denied["error"]["code"], "authority_denied")

            control_revision = snapshot["control_revision"]
            close_path = (
                f"{PREVIEW.SESSION_PATH_PREFIX}{quote(reference, safe='')}"
                f"{PREVIEW.SESSION_CLOSE_SUFFIX}"
            )
            close_headers = {
                **mutation_headers(epoch, "preview-close-key-0001"),
                PREVIEW.SESSION_CAPABILITY_HEADER: capability,
            }
            status, stale_close, _ = request_json(
                running,
                "POST",
                close_path,
                headers={
                    **mutation_headers(epoch, "preview-close-stale-0001"),
                    PREVIEW.SESSION_CAPABILITY_HEADER: capability,
                },
                body={"expected_control_revision": control_revision + 1},
            )
            self.assertEqual(status, 409)
            self.assertEqual(stale_close["error"]["code"], "control_revision_conflict")
            self.assertEqual(stale_close["error"]["retry"], "resume_snapshot")

            status, closed, _ = request_json(
                running,
                "POST",
                close_path,
                headers=close_headers,
                body={"expected_control_revision": control_revision},
            )
            self.assertEqual(status, 200)
            self.assertEqual(closed["result"], "closed")
            self.assertFalse(closed["replayed"])
            self.assertEqual(closed["snapshot"]["lifecycle"]["state"], "closed")
            self.assertEqual(closed["snapshot"]["available_controls"], [])

            status, close_replay, _ = request_json(
                running,
                "POST",
                close_path,
                headers=close_headers,
                body={"expected_control_revision": control_revision},
            )
            self.assertEqual(status, 200)
            self.assertTrue(close_replay["replayed"])

            status, terminal, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers=actor_headers(epoch),
            )
            self.assertEqual(status, 200)
            self.assertEqual(terminal["result"], "current_terminal")
            self.assertNotIn("session_capability", terminal)

    def test_two_seeded_actors_resolve_distinct_immutable_revisions(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            first_status, first, _ = self.create(running, epoch)
            second_status, second, _ = self.create(
                running,
                epoch,
                key="preview-bravo-create-0001",
                actor_context=PREVIEW.PREVIEW_ACTOR_B_CONTEXT,
                grant=PREVIEW.PREVIEW_GRANT_B,
            )

        self.assertEqual(first_status, 201)
        self.assertEqual(second_status, 201)
        self.assertNotEqual(
            first["snapshot"]["starting_world"]["immutable_revision"],
            second["snapshot"]["starting_world"]["immutable_revision"],
        )
        self.assertNotEqual(
            first["snapshot"]["session_id"], second["snapshot"]["session_id"]
        )
        self.assertEqual(second["snapshot"]["authority_class"], "presenter")

    def test_create_response_loss_reconciles_with_exact_same_request(self):
        with RunningPreview("create-response-dropped-after-commit") as running:
            epoch, _ = self.capabilities(running)
            with self.assertRaises(
                (RemoteDisconnected, URLError, ConnectionResetError)
            ):
                self.create(running, epoch)
            status, replayed, _ = self.create(running, epoch)

        self.assertEqual(status, 201)
        self.assertTrue(replayed["replayed"])

    def test_close_response_loss_reconciles_with_exact_same_request(self):
        with RunningPreview("close-response-dropped-after-commit") as running:
            epoch, _ = self.capabilities(running)
            _, created, _ = self.create(running, epoch)
            snapshot = created["snapshot"]
            reference = snapshot["reference"]
            close_path = (
                f"{PREVIEW.SESSION_PATH_PREFIX}{quote(reference, safe='')}"
                f"{PREVIEW.SESSION_CLOSE_SUFFIX}"
            )
            headers = {
                **mutation_headers(epoch, "preview-close-loss-0001"),
                PREVIEW.SESSION_CAPABILITY_HEADER: created["session_capability"],
            }
            body = {"expected_control_revision": snapshot["control_revision"]}
            with self.assertRaises(
                (RemoteDisconnected, URLError, ConnectionResetError)
            ):
                request_json(
                    running,
                    "POST",
                    close_path,
                    headers=headers,
                    body=body,
                )
            status, replayed, _ = request_json(
                running,
                "POST",
                close_path,
                headers=headers,
                body=body,
            )

        self.assertEqual(status, 200)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["snapshot"]["lifecycle"]["state"], "closed")

    def test_expiry_occurs_deterministically_on_next_passive_read(self):
        with RunningPreview("expires-on-next-read") as running:
            epoch, _ = self.capabilities(running)
            _, created, _ = self.create(running, epoch)
            self.assertEqual(created["snapshot"]["lifecycle"]["state"], "active")
            status, terminal, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers=actor_headers(epoch),
            )

        self.assertEqual(status, 200)
        self.assertEqual(terminal["result"], "current_terminal")
        self.assertEqual(terminal["snapshot"]["lifecycle"]["state"], "expired")
        self.assertEqual(
            terminal["snapshot"]["lifecycle"]["reason_code"], "idle_expired"
        )
        self.assertEqual(terminal["snapshot"]["available_controls"], [])

    def test_expiry_scenario_is_atomic_for_two_concurrent_first_readers(self):
        with RunningPreview("expires-on-next-read") as running:
            epoch, _ = self.capabilities(running)
            _, created, _ = self.create(running, epoch)
            self.assertEqual(created["snapshot"]["lifecycle"], {"state": "active"})

            harness = running.server.preview
            original_advance = harness.clock.advance
            original_expire = harness.expire_before_read_if_selected
            advance_started = threading.Event()
            release_advance = threading.Event()
            second_reader_entered = threading.Event()
            entry_lock = threading.Lock()
            entries = 0

            def paused_advance(milliseconds: int) -> None:
                advance_started.set()
                if not release_advance.wait(timeout=2):
                    raise AssertionError("concurrent expiry test did not release clock")
                original_advance(milliseconds)

            def observed_expire() -> None:
                nonlocal entries
                with entry_lock:
                    entries += 1
                    if entries == 2:
                        second_reader_entered.set()
                original_expire()

            harness.clock.advance = paused_advance
            harness.expire_before_read_if_selected = observed_expire
            results: list[tuple[int, dict, object]] = []
            failures: list[BaseException] = []

            def read_current() -> None:
                try:
                    results.append(
                        request_json(
                            running,
                            "GET",
                            PREVIEW.CURRENT_PATH,
                            headers=actor_headers(epoch),
                        )
                    )
                except BaseException as error:
                    failures.append(error)

            first = threading.Thread(target=read_current, daemon=True)
            second = threading.Thread(target=read_current, daemon=True)
            try:
                first.start()
                self.assertTrue(advance_started.wait(timeout=2))
                second.start()
                self.assertTrue(second_reader_entered.wait(timeout=2))
            finally:
                release_advance.set()
            first.join(timeout=2)
            second.join(timeout=2)
            harness.clock.advance = original_advance
            harness.expire_before_read_if_selected = original_expire

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(results), 2)
            for status, payload, _ in results:
                self.assertEqual(status, 200)
                self.assertEqual(payload["result"], "current_terminal")
                self.assertEqual(payload["snapshot"]["lifecycle"]["state"], "expired")

    def test_epoch_loss_distinguishes_reads_from_uncertain_mutations(self):
        with RunningPreview("epoch-loss") as running:
            current_epoch, _ = self.capabilities(running)
            old_epoch = running.server.preview.old_epoch
            self.assertIsNotNone(old_epoch)
            self.assertNotEqual(old_epoch, current_epoch)
            status, lost, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers=actor_headers(
                    old_epoch,
                    PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                ),
            )
            mutation_status, unknown, _ = self.create(
                running,
                old_epoch,
                actor_context=PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                grant=PREVIEW.PREVIEW_GRANT_A,
            )
            denied_status, denied, _ = self.create(
                running,
                current_epoch,
                key="preview-old-grant-rebind-0001",
                actor_context=PREVIEW.PREVIEW_ACTOR_A_CONTEXT,
                grant=PREVIEW.PREVIEW_GRANT_A,
            )
            none_status, none, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers=actor_headers(
                    current_epoch,
                    PREVIEW.PREVIEW_EPOCH_LOSS_ACTOR_CONTEXT,
                ),
            )
            fresh_status, fresh, _ = self.create(
                running,
                current_epoch,
                key="preview-fresh-create-0001",
                actor_context=PREVIEW.PREVIEW_EPOCH_LOSS_ACTOR_CONTEXT,
                grant=PREVIEW.PREVIEW_EPOCH_LOSS_GRANT,
            )

        self.assertEqual(status, 409)
        self.assertEqual(lost["error"]["code"], "session_lost")
        self.assertEqual(lost["error"]["retry"], "none")
        self.assertEqual(mutation_status, 409)
        self.assertEqual(unknown["error"]["code"], "outcome_unknown")
        self.assertEqual(unknown["error"]["retry"], "none")
        self.assertEqual(denied_status, 403)
        self.assertEqual(denied["error"]["code"], "authority_denied")
        self.assertEqual(none_status, 200)
        self.assertEqual(none, {"result": "none"})
        self.assertEqual(fresh_status, 201)
        self.assertEqual(fresh["result"], "created")

    def test_epoch_loss_cli_is_integratable_from_only_startup_and_fixtures(self):
        old_fixture = json.loads(
            (
                CONTRACT_ROOT / "fixtures/canonical-valid/authority/actor-alpha.json"
            ).read_text(encoding="utf-8")
        )
        current_fixture = json.loads(
            (
                CONTRACT_ROOT / "fixtures/canonical-valid/authority/"
                "simulator-epoch-loss-current-alpha.json"
            ).read_text(encoding="utf-8")
        )
        command = [
            str(REPOSITORY_ROOT / ".venv/bin/python"),
            str(TOOL_PATH),
            "--token",
            SERVICE_BEARER,
            "--contract-root",
            str(CONTRACT_ROOT),
            "--scenario",
            "epoch-loss",
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
                self.fail("epoch-loss preview stdout was unavailable")
            readable, _, _ = select.select([process.stdout], [], [], 15)
            self.assertTrue(readable, "epoch-loss preview did not report startup")
            startup_line = process.stdout.readline()
            startup = json.loads(startup_line)
            self.assertNotIn(SERVICE_BEARER, startup_line)
            self.assertNotIn(old_fixture["actor_context"], startup_line)
            self.assertNotIn(current_fixture["actor_context"], startup_line)
            self.assertEqual(startup["scenario"], "epoch-loss")
            previous_epoch = startup["previous_service_epoch"]

            class ExternalPreview:
                base_url = f"http://{startup['host']}:{startup['port']}"

            external = ExternalPreview()
            current_epoch, _ = self.capabilities(external)
            self.assertNotEqual(previous_epoch, current_epoch)
            stale_status, stale, _ = self.create(
                external,
                previous_epoch,
                key="preview-old-create-key-0001",
                actor_context=old_fixture["actor_context"],
                grant=old_fixture["launch_grant"],
            )
            denied_status, denied, _ = self.create(
                external,
                current_epoch,
                key="preview-old-current-epoch-key-0001",
                actor_context=old_fixture["actor_context"],
                grant=old_fixture["launch_grant"],
            )
            fresh_status, fresh, _ = self.create(
                external,
                current_epoch,
                key="preview-current-authority-key-0001",
                actor_context=current_fixture["actor_context"],
                grant=current_fixture["launch_grant"],
            )

            self.assertEqual(stale_status, 409)
            self.assertEqual(stale["error"]["code"], "outcome_unknown")
            self.assertEqual(denied_status, 403)
            self.assertEqual(denied["error"]["code"], "authority_denied")
            self.assertEqual(fresh_status, 201)
            self.assertEqual(fresh["result"], "created")
        finally:
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

    def test_once_waits_for_complete_http11_response_and_exits_cleanly(self):
        command = [
            str(REPOSITORY_ROOT / ".venv/bin/python"),
            str(TOOL_PATH),
            "--token",
            SERVICE_BEARER,
            "--contract-root",
            str(CONTRACT_ROOT),
            "--scenario",
            "lifecycle-happy",
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
                self.fail("lifecycle preview stdout was unavailable")
            readable, _, _ = select.select([process.stdout], [], [], 15)
            self.assertTrue(readable, "lifecycle preview did not report startup")
            startup = json.loads(process.stdout.readline())
            self.assertEqual(startup["scenario"], "lifecycle-happy")

            # Connect before sending request bytes so the threaded implementation
            # this replaces can dispatch its daemon handler and exit first.  The
            # synchronous path remains alive waiting for the selected request.
            with socket.create_connection(
                (startup["host"], startup["port"]),
                timeout=5,
            ) as connection:
                connection.settimeout(5)
                time.sleep(0.1)
                connection.sendall(
                    (
                        f"GET {PREVIEW.CAPABILITIES_PATH} HTTP/1.1\r\n"
                        "Host: lifecycle-preview-once\r\n"
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

            payload = json.loads(body.decode("utf-8"))
            validate_exchange(
                "GET",
                PREVIEW.CAPABILITIES_PATH,
                status,
                payload,
                None,
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("cache-control"), "no-store")
            self.assertEqual(headers.get("content-type"), "application/json")
            self.assertEqual(headers.get("content-length"), str(len(body)))
            self.assertEqual(headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(headers.get("connection"), "close")
            self.assertEqual(payload["contract"]["revision"], PREVIEW.CONTRACT_REVISION)

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

    def test_fault_scenario_cannot_be_selected_by_request(self):
        with RunningPreview("lifecycle-happy") as running:
            epoch, _ = self.capabilities(running)
            status, _, _ = self.create(running, epoch)
            query_status, query_problem, _ = request_json(
                running,
                "GET",
                f"{PREVIEW.CURRENT_PATH}?scenario=epoch-loss",
                headers={
                    **actor_headers(epoch),
                    "X-Dreamland-Preview-Scenario": "epoch-loss",
                },
            )

        self.assertEqual(status, 201)
        self.assertEqual(query_status, 404)
        self.assertEqual(query_problem["error"]["code"], "not_found")

    def test_wire_values_are_validated_before_they_reach_domain_state(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            status, malformed_key, _ = self.create(
                running,
                epoch,
                key="too-short",
            )
            self.assertEqual(status, 400)
            self.assertEqual(malformed_key["error"]["code"], "invalid_request")

            status, malformed_grant, _ = self.create(
                running,
                epoch,
                key="preview-malformed-grant-key-0001",
                grant="launch-grant:short",
            )
            self.assertEqual(status, 400)
            self.assertEqual(malformed_grant["error"]["code"], "invalid_request")

            status, malformed_actor, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers=actor_headers(epoch, "actor:short"),
            )
            self.assertEqual(status, 400)
            self.assertEqual(malformed_actor["error"]["code"], "invalid_request")

            status, malformed_epoch, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers={PREVIEW.EXPECTED_EPOCH_HEADER: "epoch:short"},
            )
            self.assertEqual(status, 400)
            self.assertEqual(malformed_epoch["error"]["code"], "invalid_request")

            status, none, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers=actor_headers(epoch),
            )
            self.assertEqual(status, 200)
            self.assertEqual(none, {"result": "none"})

            _, created, _ = self.create(
                running,
                epoch,
                key="preview-wire-valid-create-0001",
            )
            reference = created["snapshot"]["reference"]
            status, malformed_capability, _ = request_json(
                running,
                "GET",
                f"{PREVIEW.SESSION_PATH_PREFIX}{quote(reference, safe='')}",
                headers={
                    **actor_headers(epoch),
                    PREVIEW.SESSION_CAPABILITY_HEADER: "session-cap:short",
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(malformed_capability["error"]["code"], "invalid_request")

            close_path = (
                f"{PREVIEW.SESSION_PATH_PREFIX}{quote(reference, safe='')}"
                f"{PREVIEW.SESSION_CLOSE_SUFFIX}"
            )
            status, malformed_revision, _ = request_json(
                running,
                "POST",
                close_path,
                headers={
                    **mutation_headers(epoch, "preview-wire-close-key-0001"),
                    PREVIEW.SESSION_CAPABILITY_HEADER: created["session_capability"],
                },
                body={"expected_control_revision": 0},
            )
            self.assertEqual(status, 400)
            self.assertEqual(malformed_revision["error"]["code"], "invalid_request")

            status, oversized_revision, _ = request_json(
                running,
                "POST",
                close_path,
                headers={
                    **mutation_headers(
                        epoch,
                        "preview-wire-oversized-control-key-0001",
                    ),
                    PREVIEW.SESSION_CAPABILITY_HEADER: created["session_capability"],
                },
                body={"expected_control_revision": PREVIEW.MAX_CONTROL_REVISION + 1},
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                oversized_revision["error"]["code"],
                "invalid_request",
            )

            status, malformed_reference, _ = request_json(
                running,
                "GET",
                f"{PREVIEW.SESSION_PATH_PREFIX}not-a-session-reference",
                headers=actor_headers(epoch),
            )
            self.assertEqual(status, 404)
            self.assertEqual(malformed_reference["error"]["code"], "not_found")

            status, still_active, _ = request_json(
                running,
                "GET",
                f"{PREVIEW.SESSION_PATH_PREFIX}{quote(reference, safe='')}",
                headers={
                    **actor_headers(epoch),
                    PREVIEW.SESSION_CAPABILITY_HEADER: created["session_capability"],
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(still_active["snapshot"]["lifecycle"], {"state": "active"})

    def test_epoch_gate_precedes_all_other_evidence_and_body_parsing(self):
        with RunningPreview() as running:
            current_epoch, _ = self.capabilities(running)
            stale_epoch = "epoch:" + "x" * 24
            self.assertNotEqual(stale_epoch, current_epoch)

            status, stale_missing_evidence, _ = request_json(
                running,
                "POST",
                PREVIEW.SESSIONS_PATH,
                headers={PREVIEW.EXPECTED_EPOCH_HEADER: stale_epoch},
            )
            self.assertEqual(status, 409)
            self.assertEqual(stale_missing_evidence["error"]["code"], "outcome_unknown")

            status, stale_read, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers={PREVIEW.EXPECTED_EPOCH_HEADER: stale_epoch},
            )
            self.assertEqual(status, 409)
            self.assertEqual(stale_read["error"]["code"], "session_lost")

            raw_headers = {
                "Authorization": f"Bearer {SERVICE_BEARER}",
                PREVIEW.EXPECTED_EPOCH_HEADER: stale_epoch,
                "Content-Type": "application/json",
            }
            stale_malformed_body = Request(
                running.base_url + PREVIEW.SESSIONS_PATH,
                data=b"{",
                headers=raw_headers,
                method="POST",
            )
            with self.assertRaises(HTTPError) as stale_body_error:
                urlopen(stale_malformed_body, timeout=2)
            stale_body_problem = json.load(stale_body_error.exception)
            validate_safe_error(stale_body_problem)
            self.assertEqual(stale_body_error.exception.code, 409)
            self.assertEqual(stale_body_problem["error"]["code"], "outcome_unknown")

            missing_epoch_malformed_body = Request(
                running.base_url + PREVIEW.SESSIONS_PATH,
                data=b"{",
                headers={
                    "Authorization": f"Bearer {SERVICE_BEARER}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with self.assertRaises(HTTPError) as missing_epoch_error:
                urlopen(missing_epoch_malformed_body, timeout=2)
            missing_epoch_problem = json.load(missing_epoch_error.exception)
            validate_safe_error(missing_epoch_problem)
            self.assertEqual(missing_epoch_error.exception.code, 400)
            self.assertEqual(
                missing_epoch_problem["error"]["code"],
                "expected_epoch_required",
            )

            status, none, _ = request_json(
                running,
                "GET",
                PREVIEW.CURRENT_PATH,
                headers=actor_headers(current_epoch),
            )
            self.assertEqual(status, 200)
            self.assertEqual(none, {"result": "none"})

    def test_transport_rejects_unbounded_or_ambiguous_json(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            url = running.base_url + PREVIEW.SESSIONS_PATH
            headers = {
                "Authorization": f"Bearer {SERVICE_BEARER}",
                **mutation_headers(epoch, "preview-invalid-json-0001"),
                "Content-Type": "application/json",
            }
            duplicate = Request(
                url,
                data=b'{"launch_grant":"one","launch_grant":"two"}',
                headers=headers,
                method="POST",
            )
            with self.assertRaises(HTTPError) as duplicate_error:
                urlopen(duplicate, timeout=2)
            duplicate_problem = json.load(duplicate_error.exception)
            validate_safe_error(duplicate_problem)

            oversized = Request(
                url,
                data=b"{" + b" " * PREVIEW.MAX_REQUEST_BODY_BYTES + b"}",
                headers=headers,
                method="POST",
            )
            with self.assertRaises(HTTPError) as oversized_error:
                urlopen(oversized, timeout=2)
            oversized_problem = json.load(oversized_error.exception)
            validate_safe_error(oversized_problem)

        self.assertEqual(duplicate_error.exception.code, 400)
        self.assertEqual(duplicate_problem["error"]["code"], "invalid_json")
        self.assertEqual(oversized_error.exception.code, 413)
        self.assertEqual(oversized_problem["error"]["code"], "request_too_large")

    def test_transport_rejects_duplicate_body_framing_and_media_headers(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            port = running.server.server_address[1]
            body = json.dumps(
                {"launch_grant": PREVIEW.PREVIEW_GRANT_A},
                separators=(",", ":"),
            ).encode("utf-8")
            common = (
                f"POST {PREVIEW.SESSIONS_PATH} HTTP/1.1\r\n"
                "Host: lifecycle-preview-duplicate-headers\r\n"
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
                        + f"{PREVIEW.IDEMPOTENCY_HEADER}: duplicate-header-key-{index:04d}\r\n"
                        + body_headers
                        + "Connection: close\r\n\r\n"
                    ).encode("ascii") + body
                    response = raw_http_exchange(port, request)
                    status, headers, problem = parse_raw_problem(response)
                    self.assertEqual(status, 400)
                    self.assertEqual(problem["error"]["code"], "invalid_request")
                    self.assertEqual(
                        headers["content-type"], "application/problem+json"
                    )
                    self.assertEqual(
                        len(response.partition(b"\r\n\r\n")[2]),
                        int(headers["content-length"]),
                    )

    def test_unread_rejected_body_closes_before_a_pipelined_request(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            port = running.server.server_address[1]
            pipelined_request = (
                f"GET {PREVIEW.CAPABILITIES_PATH} HTTP/1.1\r\n"
                "Host: lifecycle-preview-pipelined\r\n"
                f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            rejected_request = (
                f"POST {PREVIEW.SESSIONS_PATH} HTTP/1.1\r\n"
                "Host: lifecycle-preview-pipelined\r\n"
                f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                f"{PREVIEW.EXPECTED_EPOCH_HEADER}: {epoch}\r\n"
                f"{PREVIEW.ACTOR_CONTEXT_HEADER}: "
                f"{PREVIEW.PREVIEW_ACTOR_A_CONTEXT}\r\n"
                f"{PREVIEW.IDEMPOTENCY_HEADER}: unread-body-key-0001\r\n"
                "Content-Type: text/plain\r\n"
                f"Content-Length: {len(pipelined_request)}\r\n\r\n"
            ).encode("ascii")

            response = raw_http_exchange(
                port,
                rejected_request + pipelined_request,
            )
            status, headers, problem = parse_raw_problem(response)

            self.assertEqual(status, 415)
            self.assertEqual(problem["error"]["code"], "unsupported_media_type")
            self.assertEqual(headers["connection"], "close")
            self.assertEqual(response.count(b"HTTP/1.1 "), 1)
            self.assertEqual(
                len(response.partition(b"\r\n\r\n")[2]),
                int(headers["content-length"]),
            )

    def test_pre_body_rejections_close_before_a_pipelined_request(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            port = running.server.server_address[1]
            pipelined_request = (
                f"GET {PREVIEW.CAPABILITIES_PATH} HTTP/1.1\r\n"
                "Host: lifecycle-preview-pipelined\r\n"
                f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            valid_epoch = f"{PREVIEW.EXPECTED_EPOCH_HEADER}: {epoch}\r\n"
            valid_actor = (
                f"{PREVIEW.ACTOR_CONTEXT_HEADER}: {PREVIEW.PREVIEW_ACTOR_A_CONTEXT}\r\n"
            )
            valid_key = f"{PREVIEW.IDEMPOTENCY_HEADER}: pre-body-key-0001\r\n"
            cases = (
                (
                    "unauthorized",
                    "Authorization: Bearer incorrect-private-bearer\r\n"
                    + valid_epoch
                    + valid_actor
                    + valid_key,
                    401,
                    "unauthorized",
                ),
                (
                    "missing-epoch",
                    f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                    + valid_actor
                    + valid_key,
                    400,
                    "expected_epoch_required",
                ),
                (
                    "missing-idempotency",
                    f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                    + valid_epoch
                    + valid_actor,
                    400,
                    "invalid_request",
                ),
            )

            for label, gate_headers, expected_status, expected_code in cases:
                with self.subTest(label=label):
                    rejected_request = (
                        f"POST {PREVIEW.SESSIONS_PATH} HTTP/1.1\r\n"
                        "Host: lifecycle-preview-pipelined\r\n"
                        + gate_headers
                        + "Content-Type: application/json\r\n"
                        + f"Content-Length: {len(pipelined_request)}\r\n\r\n"
                    ).encode("ascii")

                    response = raw_http_exchange(
                        port,
                        rejected_request + pipelined_request,
                    )
                    status, headers, problem = parse_raw_problem(response)

                    self.assertEqual(status, expected_status)
                    self.assertEqual(problem["error"]["code"], expected_code)
                    self.assertEqual(headers["connection"], "close")
                    self.assertEqual(response.count(b"HTTP/1.1 "), 1)
                    self.assertEqual(
                        len(response.partition(b"\r\n\r\n")[2]),
                        int(headers["content-length"]),
                    )

    def test_get_with_declared_body_closes_before_a_pipelined_request(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            port = running.server.server_address[1]
            pipelined_request = (
                f"GET {PREVIEW.CAPABILITIES_PATH} HTTP/1.1\r\n"
                "Host: lifecycle-preview-pipelined\r\n"
                f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            request_with_body = (
                f"GET {PREVIEW.CAPABILITIES_PATH} HTTP/1.1\r\n"
                "Host: lifecycle-preview-pipelined\r\n"
                f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                f"Content-Length: {len(pipelined_request)}\r\n\r\n"
            ).encode("ascii")

            response = raw_http_exchange(
                port,
                request_with_body + pipelined_request,
            )
            header_bytes, separator, body = response.partition(b"\r\n\r\n")
            self.assertTrue(separator)
            lines = header_bytes.decode("ascii").split("\r\n")
            status = int(lines[0].split(" ", 2)[1])
            headers = {
                name.strip().lower(): value.strip()
                for line in lines[1:]
                for name, value in (line.split(":", 1),)
            }
            payload = json.loads(body)

            self.assertEqual(status, 200)
            self.assertEqual(payload["service_epoch"], epoch)
            self.assertEqual(headers["connection"], "close")
            self.assertEqual(response.count(b"HTTP/1.1 "), 1)
            self.assertEqual(len(body), int(headers["content-length"]))

    def test_fully_read_invalid_json_can_precede_a_pipelined_request(self):
        with RunningPreview() as running:
            epoch, _ = self.capabilities(running)
            port = running.server.server_address[1]
            invalid_body = b"{"
            invalid_request = (
                f"POST {PREVIEW.SESSIONS_PATH} HTTP/1.1\r\n"
                "Host: lifecycle-preview-pipelined\r\n"
                f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                f"{PREVIEW.EXPECTED_EPOCH_HEADER}: {epoch}\r\n"
                f"{PREVIEW.ACTOR_CONTEXT_HEADER}: "
                f"{PREVIEW.PREVIEW_ACTOR_A_CONTEXT}\r\n"
                f"{PREVIEW.IDEMPOTENCY_HEADER}: invalid-json-key-0001\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(invalid_body)}\r\n\r\n"
            ).encode("ascii")
            pipelined_request = (
                f"GET {PREVIEW.CAPABILITIES_PATH} HTTP/1.1\r\n"
                "Host: lifecycle-preview-pipelined\r\n"
                f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")

            response = raw_http_exchange(
                port,
                invalid_request + invalid_body + pipelined_request,
            )
            status, headers, problem = parse_raw_problem(response)
            first_body = response.partition(b"\r\n\r\n")[2]
            second_response = first_body[int(headers["content-length"]) :]

            self.assertEqual(status, 400)
            self.assertEqual(problem["error"]["code"], "invalid_json")
            self.assertNotIn("connection", headers)
            self.assertTrue(second_response.startswith(b"HTTP/1.1 200 "))
            self.assertEqual(response.count(b"HTTP/1.1 "), 2)

    def test_parser_error_does_not_reuse_prior_keepalive_headers(self):
        with RunningPreview() as running:
            port = running.server.server_address[1]
            unauthorized_request = (
                f"GET {PREVIEW.CAPABILITIES_PATH} HTTP/1.1\r\n"
                "Host: lifecycle-preview-pipelined\r\n"
                "Authorization: Bearer incorrect-private-bearer\r\n\r\n"
            ).encode("ascii")
            malformed_requests = (
                (
                    "invalid-version",
                    (
                        f"GET {PREVIEW.CAPABILITIES_PATH} HTTP/not-a-version\r\n\r\n"
                    ).encode("ascii"),
                ),
                ("overlong-request-line", b"G" * 65_537 + b"\r\n"),
            )

            for label, malformed_request in malformed_requests:
                with self.subTest(label=label):
                    response = raw_http_exchange(
                        port,
                        unauthorized_request + malformed_request,
                    )
                    first_status, first_headers, first_problem = parse_raw_problem(
                        response
                    )
                    first_body = response.partition(b"\r\n\r\n")[2]
                    second_response = first_body[int(first_headers["content-length"]) :]
                    second_status, second_headers, second_problem = parse_raw_problem(
                        second_response
                    )

                    self.assertEqual(first_status, 401)
                    self.assertEqual(
                        first_problem["error"]["code"],
                        "unauthorized",
                    )
                    self.assertNotIn("connection", first_headers)
                    self.assertEqual(second_status, 400)
                    self.assertEqual(
                        second_problem["error"]["code"],
                        "invalid_request",
                    )
                    self.assertEqual(second_headers["connection"], "close")
                    self.assertEqual(response.count(b"HTTP/1.1 "), 2)

    def test_stdlib_parser_and_unknown_method_errors_are_safe_and_bearer_first(self):
        with RunningPreview() as running:
            port = running.server.server_address[1]
            malformed_response = raw_http_exchange(
                port,
                b"BROKEN private-parser-material HTTP/not-a-version\r\n\r\n",
            )
            malformed_status, malformed_headers, malformed = parse_raw_problem(
                malformed_response
            )
            self.assertEqual(malformed_status, 400)
            self.assertEqual(malformed["error"]["code"], "invalid_request")
            self.assertEqual(
                malformed_headers["content-type"], "application/problem+json"
            )
            self.assertEqual(malformed_headers["cache-control"], "no-store")
            self.assertNotIn(b"private-parser-material", malformed_response)
            self.assertNotIn(b"<html", malformed_response.lower())

            legacy_response = raw_http_exchange(
                port,
                b"GET /private-http09-material\r\n\r\n",
            )
            legacy_status, legacy_headers, legacy_problem = parse_raw_problem(
                legacy_response
            )
            self.assertEqual(legacy_status, 401)
            self.assertEqual(legacy_problem["error"]["code"], "unauthorized")
            self.assertEqual(legacy_headers["www-authenticate"], "Bearer")
            self.assertEqual(legacy_headers["cache-control"], "no-store")
            self.assertEqual(legacy_headers["x-content-type-options"], "nosniff")
            self.assertNotIn(b"private-http09-material", legacy_response)
            self.assertNotIn(b"<html", legacy_response.lower())

            unknown_without_bearer = raw_http_exchange(
                port,
                (
                    b"PROPFIND /product/v1/capabilities HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
                ),
            )
            unauthorized_status, _, unauthorized = parse_raw_problem(
                unknown_without_bearer
            )
            self.assertEqual(unauthorized_status, 401)
            self.assertEqual(unauthorized["error"]["code"], "unauthorized")

            unknown_with_bearer = raw_http_exchange(
                port,
                (
                    b"PROPFIND /product/v1/capabilities HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\nAuthorization: Bearer "
                    + SERVICE_BEARER.encode("ascii")
                    + b"\r\nConnection: close\r\n\r\n"
                ),
            )
            not_found_status, not_found_headers, not_found = parse_raw_problem(
                unknown_with_bearer
            )
            self.assertEqual(not_found_status, 404)
            self.assertEqual(not_found["error"]["code"], "not_found")
            self.assertEqual(
                not_found_headers["content-type"], "application/problem+json"
            )
            self.assertNotIn(b"Unsupported method", unknown_with_bearer)

            too_many_headers = (
                b"GET /product/v1/capabilities HTTP/1.1\r\n"
                + b"X-Filler: value\r\n" * 101
                + b"\r\n"
            )
            header_status, header_response_headers, header_problem = parse_raw_problem(
                raw_http_exchange(port, too_many_headers)
            )
            self.assertEqual(header_status, 400)
            self.assertEqual(header_problem["error"]["code"], "invalid_request")
            self.assertEqual(
                header_response_headers["content-type"],
                "application/problem+json",
            )

    def test_stalled_clients_are_deadlined_and_connection_admission_is_bounded(self):
        stalled: list[socket.socket] = []
        with RunningPreview() as running:
            port = running.server.server_address[1]
            try:
                for _ in range(PREVIEW.MAX_CONCURRENT_CONNECTIONS):
                    connection = socket.create_connection(
                        ("127.0.0.1", port), timeout=2
                    )
                    connection.sendall(
                        b"GET /product/v1/capabilities HTTP/1.1\r\nHost: stalled"
                    )
                    stalled.append(connection)

                admission_deadline = time.monotonic() + 2
                while (
                    running.server.active_connections
                    < PREVIEW.MAX_CONCURRENT_CONNECTIONS
                    and time.monotonic() < admission_deadline
                ):
                    time.sleep(0.01)
                self.assertEqual(
                    running.server.active_connections,
                    PREVIEW.MAX_CONCURRENT_CONNECTIONS,
                )

                result: list[tuple[str, object]] = []

                def get_capabilities_after_slot_releases() -> None:
                    try:
                        result.append(("result", self.capabilities(running)))
                    except BaseException as error:
                        result.append(("error", error))

                requester = threading.Thread(
                    target=get_capabilities_after_slot_releases,
                    daemon=True,
                )
                requester.start()
                requester.join(timeout=PREVIEW.CONNECTION_READ_TIMEOUT_SECONDS + 3)
                self.assertFalse(requester.is_alive())
                self.assertEqual(result[0][0], "result", result)
                self.assertLessEqual(
                    running.server.maximum_observed_connections,
                    PREVIEW.MAX_CONCURRENT_CONNECTIONS,
                )
            finally:
                for connection in stalled:
                    connection.close()

            trickle = socket.create_connection(("127.0.0.1", port), timeout=2)
            try:
                trickle.settimeout(1)
                trickle.sendall(
                    b"GET /product/v1/capabilities HTTP/1.1\r\nHost: trickle\r\n"
                    b"X-Slow: "
                )
                started = time.monotonic()
                closed_by_deadline = False
                while (
                    time.monotonic() - started < PREVIEW.CONNECTION_DEADLINE_SECONDS + 1
                ):
                    try:
                        trickle.sendall(b"x")
                    except OSError:
                        closed_by_deadline = True
                        break
                    time.sleep(PREVIEW.CONNECTION_READ_TIMEOUT_SECONDS / 4)
                    readable, _, _ = select.select([trickle], [], [], 0)
                    if readable:
                        try:
                            received = trickle.recv(1)
                        except ConnectionResetError:
                            received = b""
                        if not received:
                            closed_by_deadline = True
                            break
                self.assertTrue(closed_by_deadline)
            finally:
                trickle.close()

    def test_token_file_is_owner_only_bounded_and_used_without_argv_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_file = root / "preview.token"
            token_file.write_text(SERVICE_BEARER + "\n", encoding="ascii")
            token_file.chmod(0o600)
            arguments = [
                "--token-file",
                str(token_file),
                "--contract-root",
                str(CONTRACT_ROOT),
                "--once",
            ]
            self.assertNotIn(SERVICE_BEARER, arguments)
            with mock.patch.object(PREVIEW, "serve_preview") as serve:
                self.assertEqual(PREVIEW.main(arguments), 0)
            self.assertEqual(serve.call_args.kwargs["bearer"], SERVICE_BEARER)

            token_file.chmod(0o640)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                PREVIEW.load_preview_bearer_file(token_file)
            token_file.chmod(0o600)

            token_link = root / "preview-link.token"
            token_link.symlink_to(token_file)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                PREVIEW.load_preview_bearer_file(token_link)

            multiline = root / "multiline.token"
            multiline.write_text(f"{SERVICE_BEARER}\nsecond-line\n", encoding="ascii")
            multiline.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "visible ASCII"):
                PREVIEW.load_preview_bearer_file(multiline)

            oversized = root / "oversized.token"
            oversized.write_bytes(b"x" * (PREVIEW.MAX_HEADER_VALUE_CHARACTERS + 3))
            oversized.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "too large"):
                PREVIEW.load_preview_bearer_file(oversized)

    def test_preview_rejects_nonloopback_and_unsafe_service_bearers(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            PREVIEW.build_preview_server(
                host="0.0.0.0",
                port=0,
                bearer=SERVICE_BEARER,
                scenario="lifecycle-happy",
                contract_root=CONTRACT_ROOT,
            )
        for unsupported_loopback in ("127.0.0.2", "::1"):
            with self.subTest(host=unsupported_loopback):
                with self.assertRaisesRegex(ValueError, "127.0.0.1"):
                    PREVIEW.build_preview_server(
                        host=unsupported_loopback,
                        port=0,
                        bearer=SERVICE_BEARER,
                        scenario="lifecycle-happy",
                        contract_root=CONTRACT_ROOT,
                    )
        for bearer in ("", "line\nbreak", "has space", "non-ascii-\N{SNOWMAN}"):
            with self.subTest(bearer=bearer):
                with self.assertRaisesRegex(ValueError, "bearer"):
                    PREVIEW.build_preview_server(
                        host="127.0.0.1",
                        port=0,
                        bearer=bearer,
                        scenario="lifecycle-happy",
                        contract_root=CONTRACT_ROOT,
                    )

    def test_preview_refuses_a_contract_with_a_stale_artifact_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            copied_root = Path(directory) / "v0.2.0-rc.2"
            shutil.copytree(CONTRACT_ROOT, copied_root)
            manifest_path = copied_root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["openapi_sha256"] = "0" * 64
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "complete artifact validation"):
                PREVIEW.validate_contract_root(copied_root)

    def test_unavailable_methods_never_fall_back_to_default_html(self):
        with RunningPreview() as running:
            for method in ("OPTIONS", "TRACE", "CONNECT", "PUT", "PATCH", "DELETE"):
                with self.subTest(method=method):
                    status, problem, headers = request_json(
                        running,
                        method,
                        PREVIEW.CAPABILITIES_PATH,
                    )
                    self.assertEqual(status, 404)
                    self.assertEqual(problem["error"]["code"], "not_found")
                    self.assertEqual(headers.get("Cache-Control"), "no-store")
                    self.assertEqual(
                        headers.get("Content-Type"), "application/problem+json"
                    )

            head = Request(
                running.base_url + PREVIEW.CAPABILITIES_PATH,
                headers={"Authorization": f"Bearer {SERVICE_BEARER}"},
                method="HEAD",
            )
            with self.assertRaises(HTTPError) as head_error:
                urlopen(head, timeout=2)
            self.assertEqual(head_error.exception.code, 404)
            self.assertEqual(head_error.exception.read(), b"")
            self.assertEqual(
                head_error.exception.headers.get("Cache-Control"), "no-store"
            )
            self.assertEqual(
                head_error.exception.headers.get("Content-Type"),
                "application/problem+json",
            )


if __name__ == "__main__":
    unittest.main()
