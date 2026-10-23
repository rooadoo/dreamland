from __future__ import annotations

import http.client
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_preview.py"
CONTRACT_TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_contract.py"
SERVICE_BEARER = "private-attendee-preview-service-bearer"


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


PREVIEW = _load_module(TOOL_PATH, "dreamland_product_attendee_preview_test_tool")
CONTRACT = _load_module(
    CONTRACT_TOOL_PATH,
    "dreamland_product_attendee_contract_preview_test_tool",
)


def _load_generated_client(root: Path):
    return _load_module(
        root / "generated" / "product_session_client.py",
        "dreamland_product_attendee_preview_generated_client",
    )


class RunningPreview:
    def __init__(self, contract_root: Path) -> None:
        self.server = PREVIEW.build_preview_server(
            host="127.0.0.1",
            port=0,
            bearer=SERVICE_BEARER,
            scenario=PREVIEW.SCENARIOS[0],
            contract_root=contract_root,
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
    bearer: str = SERVICE_BEARER,
) -> tuple[int, dict, object]:
    request_headers = {"Authorization": f"Bearer {bearer}"}
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


def epoch(running: RunningPreview) -> str:
    status, payload, _headers = request(running, "GET", "/product/v1/capabilities")
    if status != 200:
        raise AssertionError(f"capabilities failed: {status} {payload}")
    return payload["service_epoch"]


def context_id(label: str) -> str:
    return f"gateway-context:{label * 32}"


def bootstrap(
    running: RunningPreview,
    service_epoch: str,
    label: str,
    key: str,
) -> tuple[int, dict, object]:
    return request(
        running,
        "POST",
        PREVIEW.ATTENDEE_CONTEXTS_PATH,
        headers={
            "X-Dreamland-Expected-Epoch": service_epoch,
            "Idempotency-Key": f"attendee-test:{key}",
        },
        body={"gateway_context_id": context_id(label)},
    )


def attendee_headers(
    service_epoch: str,
    actor_context: str,
    *,
    key: str | None = None,
    session_authority: str | None = None,
) -> dict[str, str]:
    headers = {
        "X-Dreamland-Expected-Epoch": service_epoch,
        "X-Dreamland-Actor-Context": actor_context,
    }
    if key is not None:
        headers["Idempotency-Key"] = f"attendee-test:{key}"
    if session_authority is not None:
        headers["X-Dreamland-Session-Capability"] = session_authority
    return headers


def options(
    running: RunningPreview,
    service_epoch: str,
    actor_context: str,
) -> tuple[int, dict, object]:
    return request(
        running,
        "GET",
        PREVIEW.ATTENDEE_LAUNCH_OPTIONS_PATH,
        headers=attendee_headers(service_epoch, actor_context),
    )


def exchange(
    running: RunningPreview,
    service_epoch: str,
    actor_context: str,
    selection_id: str,
    notice_version: str,
    key: str,
) -> tuple[int, dict, object]:
    return request(
        running,
        "POST",
        PREVIEW.ATTENDEE_LAUNCH_SELECTIONS_PATH,
        headers=attendee_headers(service_epoch, actor_context, key=key),
        body={
            "launch_selection_id": selection_id,
            "notice_version_presented": notice_version,
        },
    )


class ProductAttendeePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.contract_root = Path(cls._temporary.name) / "v0.7.0-rc.3"
        CONTRACT.refresh_contract(cls.contract_root)
        cls.generated = _load_generated_client(cls.contract_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_import_is_inert_provider_free_and_explicit(self) -> None:
        code = f"""
import importlib.util
import json
import pathlib
import threading
before = [(thread.ident, thread.name) for thread in threading.enumerate()]
path = pathlib.Path({str(TOOL_PATH)!r})
spec = importlib.util.spec_from_file_location("attendee_preview_inert", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
after = [(thread.ident, thread.name) for thread in threading.enumerate()]
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
        self.assertEqual(payload["revision"], "0.7.0-rc.3")

    def test_contract_capabilities_and_all_predecessor_routes_are_retained(
        self,
    ) -> None:
        validation = CONTRACT.validate_contract(self.contract_root)
        rc2 = json.loads(
            (
                REPOSITORY_ROOT / "contracts/product-api/v0.7.0-rc.2/openapi.json"
            ).read_text(encoding="utf-8")
        )
        rc3 = json.loads(
            (self.contract_root / "openapi.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validation["routes"], 24)
        self.assertEqual(
            set(rc2["paths"]),
            set(rc3["paths"])
            - {
                PREVIEW.ATTENDEE_CONTEXTS_PATH,
                PREVIEW.ATTENDEE_LAUNCH_OPTIONS_PATH,
                PREVIEW.ATTENDEE_LAUNCH_SELECTIONS_PATH,
            },
        )

        with RunningPreview(self.contract_root) as running:
            status, payload, headers = request(
                running, "GET", "/product/v1/capabilities"
            )
            service_epoch = payload["service_epoch"]
            stage_status, stage_options, _ = request(
                running,
                "GET",
                PREVIEW._SELECTION.LAUNCH_OPTIONS_PATH,
                headers={
                    "X-Dreamland-Expected-Epoch": service_epoch,
                    "X-Dreamland-Actor-Context": PREVIEW._SELECTION.PRESENTER_CONTEXT,
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Dreamland-Contract-Revision"], "0.7.0-rc.3")
        self.assertTrue(
            all(not value["enabled"] for value in payload["capabilities"].values())
        )
        self.assertEqual(stage_status, 200)
        self.assertEqual(stage_options["kind"], "presenter_stage_launch_options")

    def test_bootstrap_auth_strict_body_and_epoch_precedence(self) -> None:
        with RunningPreview(self.contract_root) as running:
            service_epoch = epoch(running)
            unauthorized_status, unauthorized, _ = request(
                running,
                "POST",
                PREVIEW.ATTENDEE_CONTEXTS_PATH,
                headers={
                    "X-Dreamland-Expected-Epoch": "epoch:stale_preview_value_0001",
                    "Idempotency-Key": "bootstrap-precedence",
                },
                body={"gateway_context_id": "invalid"},
                bearer="wrong-private-product-bearer",
            )
            stale_status, stale, _ = request(
                running,
                "POST",
                PREVIEW.ATTENDEE_CONTEXTS_PATH,
                headers={
                    "X-Dreamland-Expected-Epoch": "epoch:stale_preview_value_0001",
                    "Idempotency-Key": "bootstrap-precedence",
                },
                body={"gateway_context_id": "invalid", "extra": True},
            )
            strict_status, strict, _ = request(
                running,
                "POST",
                PREVIEW.ATTENDEE_CONTEXTS_PATH,
                headers={
                    "X-Dreamland-Expected-Epoch": service_epoch,
                    "Idempotency-Key": "bootstrap-strict",
                },
                body={
                    "gateway_context_id": context_id("a"),
                    "actor_context": "actor:caller_must_not_supply_0001",
                },
            )

        self.assertEqual(unauthorized_status, 401)
        self.assertEqual(unauthorized["error"]["code"], "unauthorized")
        self.assertEqual(stale_status, 410)
        self.assertEqual(stale["error"]["code"], "outcome_unknown")
        self.assertEqual(strict_status, 400)
        self.assertEqual(strict["error"]["code"], "invalid_request")

    def test_bootstrap_replay_and_changed_body_conflict(self) -> None:
        with RunningPreview(self.contract_root) as running:
            service_epoch = epoch(running)
            first_status, first, first_headers = bootstrap(
                running, service_epoch, "a", "bootstrap-key"
            )
            replay_status, replay, _ = bootstrap(
                running, service_epoch, "a", "bootstrap-key"
            )
            conflict_status, conflict, _ = bootstrap(
                running, service_epoch, "b", "bootstrap-key"
            )

        self.assertEqual(first_status, 201)
        self.assertEqual(replay_status, 201)
        self.assertEqual(first_headers["Cache-Control"], "no-store")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["actor_context"], replay["actor_context"])
        self.assertEqual(first["authority_class"], "attendee")
        self.assertIn("actor_expires_at", first)
        self.assertNotIn("context_reused", first)
        self.assertNotIn("gateway_context_id", json.dumps(first))
        self.assertIs(
            self.generated.validate_attendee_context_bootstrap(
                first,
                expected_epoch=first["service_epoch"],
            ),
            first,
        )
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")

    def test_capacity_rejection_replays_and_changed_body_conflicts(self) -> None:
        with RunningPreview(self.contract_root) as running:
            service_epoch = epoch(running)
            for index, label in enumerate(("a", "b", "c", "d"), 1):
                status, _payload, _ = bootstrap(
                    running, service_epoch, label, f"capacity-{index}"
                )
                self.assertEqual(status, 201)
            first_status, first, _ = bootstrap(
                running, service_epoch, "e", "capacity-terminal"
            )
            replay_status, replay, _ = bootstrap(
                running, service_epoch, "e", "capacity-terminal"
            )
            conflict_status, conflict, _ = bootstrap(
                running, service_epoch, "f", "capacity-terminal"
            )

        self.assertEqual(first_status, 503)
        self.assertEqual(replay_status, 503)
        self.assertEqual(first["error"]["code"], "attendee_context_capacity_exhausted")
        self.assertEqual(replay["error"]["retry"], "none")
        self.assertIsNone(replay["error"]["retry_after_ms"])
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")

    def test_authored_selection_session_and_cross_context_isolation(self) -> None:
        with RunningPreview(self.contract_root) as running:
            service_epoch = epoch(running)
            _, context_a, _ = bootstrap(running, service_epoch, "a", "boot-a")
            _, context_b, _ = bootstrap(running, service_epoch, "b", "boot-b")
            actor_a = context_a["actor_context"]
            actor_b = context_b["actor_context"]

            option_status, option_payload, _ = options(running, service_epoch, actor_a)
            option = option_payload["options"][0]
            self.assertEqual(option["origin"], {"kind": "authored_profile_snapshot"})
            serialized_option = json.dumps(option, sort_keys=True)
            self.assertNotIn("profile_path", serialized_option)
            self.assertNotIn("world_state", serialized_option)
            self.assertIs(
                self.generated.validate_attendee_launch_options(
                    option_payload,
                    expected_epoch=service_epoch,
                ),
                option_payload,
            )

            exchange_status, granted, _ = exchange(
                running,
                service_epoch,
                actor_a,
                option["launch_selection_id"],
                option["notice_version"],
                "exchange-a",
            )
            replay_status, replayed, _ = exchange(
                running,
                service_epoch,
                actor_a,
                option["launch_selection_id"],
                option["notice_version"],
                "exchange-a",
            )
            create_status, created, _ = request(
                running,
                "POST",
                "/product/v1/sessions",
                headers=attendee_headers(service_epoch, actor_a, key="create-a"),
                body={"launch_grant": granted["launch_grant"]},
            )
            self.assertEqual(create_status, 201, created)
            reference = created["snapshot"]["reference"]
            session_authority = created["session_authority"]
            denied_status, denied, _ = request(
                running,
                "GET",
                f"/product/v1/sessions/{reference}",
                headers=attendee_headers(
                    service_epoch,
                    actor_b,
                    session_authority=session_authority,
                ),
            )
            own_status, own, _ = request(
                running,
                "GET",
                f"/product/v1/sessions/{reference}",
                headers=attendee_headers(
                    service_epoch,
                    actor_a,
                    session_authority=session_authority,
                ),
            )
            command_status, accepted, _ = request(
                running,
                "POST",
                f"/product/v1/sessions/{quote(reference, safe='')}/commands",
                headers={
                    **attendee_headers(
                        service_epoch,
                        actor_a,
                        session_authority=session_authority,
                    ),
                    "Idempotency-Key": "attendee-command-0001",
                },
                body={"command": "hostname", "expected_slot_generation": 0},
            )
            deadline = time.monotonic() + 5
            while True:
                result_status, result, _ = request(
                    running,
                    "GET",
                    (
                        f"/product/v1/sessions/{quote(reference, safe='')}/commands/"
                        f"{quote(accepted['command_id'], safe='')}"
                    ),
                    headers=attendee_headers(
                        service_epoch,
                        actor_a,
                        session_authority=session_authority,
                    ),
                )
                if result["terminal"]:
                    break
                if time.monotonic() >= deadline:
                    self.fail("attendee preview command did not become terminal")

        self.assertEqual(option_status, 200)
        self.assertEqual(exchange_status, 200)
        self.assertEqual(replay_status, 200)
        self.assertFalse(granted["replayed"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(granted["launch_grant"], replayed["launch_grant"])
        self.assertIs(
            self.generated.validate_attendee_launch_selection(
                granted,
                expected_launch_selection_id=option["launch_selection_id"],
                expected_notice_version=option["notice_version"],
                expected_epoch=service_epoch,
            ),
            granted,
        )
        self.assertEqual(create_status, 201)
        self.assertEqual(denied_status, 403)
        self.assertEqual(denied["error"]["code"], "authority_denied")
        self.assertEqual(own_status, 200)
        self.assertEqual(own["snapshot"]["reference"], reference)
        self.assertEqual(command_status, 202)
        self.assertEqual(result_status, 200)
        self.assertEqual(result["command"]["result"]["outcome"], "completed")

    def test_notice_and_strict_exchange_body_are_bound_to_idempotency(self) -> None:
        with RunningPreview(self.contract_root) as running:
            service_epoch = epoch(running)
            _, context, _ = bootstrap(running, service_epoch, "a", "boot")
            actor = context["actor_context"]
            _, option_payload, _ = options(running, service_epoch, actor)
            option = option_payload["options"][0]
            wrong_status, wrong, _ = exchange(
                running,
                service_epoch,
                actor,
                option["launch_selection_id"],
                "notice:wrong-preview-v1",
                "notice-key",
            )
            conflict_status, conflict, _ = exchange(
                running,
                service_epoch,
                actor,
                option["launch_selection_id"],
                option["notice_version"],
                "notice-key",
            )
            strict_status, strict, _ = request(
                running,
                "POST",
                PREVIEW.ATTENDEE_LAUNCH_SELECTIONS_PATH,
                headers=attendee_headers(service_epoch, actor, key="strict-exchange"),
                body={
                    "launch_selection_id": option["launch_selection_id"],
                    "notice_version_presented": option["notice_version"],
                    "launch_grant": "caller-supplied",
                },
            )

        self.assertEqual(wrong_status, 409)
        self.assertEqual(wrong["error"]["code"], "notice_version_mismatch")
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
        self.assertEqual(strict_status, 400)
        self.assertEqual(strict["error"]["code"], "invalid_request")

    def test_presenter_stage_session_retains_generic_runtime_routes(self) -> None:
        with RunningPreview(self.contract_root) as running:
            service_epoch = epoch(running)
            presenter = PREVIEW._SELECTION.PRESENTER_CONTEXT
            stage_headers = attendee_headers(service_epoch, presenter)
            option_status, option_payload, _ = request(
                running,
                "GET",
                PREVIEW._SELECTION.LAUNCH_OPTIONS_PATH,
                headers=stage_headers,
            )
            option = option_payload["options"][0]
            exchange_status, granted, _ = request(
                running,
                "POST",
                PREVIEW._SELECTION.LAUNCH_SELECTIONS_PATH,
                headers=attendee_headers(
                    service_epoch,
                    presenter,
                    key="presenter-exchange",
                ),
                body={"launch_selection_id": option["launch_selection_id"]},
            )
            launch_status, launched, _ = request(
                running,
                "POST",
                PREVIEW._STAGE.STAGE_LAUNCH_PATH,
                headers=attendee_headers(
                    service_epoch,
                    presenter,
                    key="presenter-launch",
                ),
                body={"launch_grant": granted["launch_grant"]},
            )
            reference = launched["snapshot"]["reference"]
            encoded_reference = quote(reference, safe="")
            session_authority = launched["session_authority"]
            session_headers = attendee_headers(
                service_epoch,
                presenter,
                session_authority=session_authority,
            )
            snapshot_status, snapshot, _ = request(
                running,
                "GET",
                f"/product/v1/sessions/{encoded_reference}",
                headers=session_headers,
            )
            command_status, accepted, _ = request(
                running,
                "POST",
                f"/product/v1/sessions/{encoded_reference}/commands",
                headers={
                    **session_headers,
                    "Idempotency-Key": "presenter-command-0001",
                },
                body={"command": "whoami", "expected_slot_generation": 0},
            )
            deadline = time.monotonic() + 5
            while True:
                result_status, result, _ = request(
                    running,
                    "GET",
                    (
                        f"/product/v1/sessions/{encoded_reference}/commands/"
                        f"{quote(accepted['command_id'], safe='')}"
                    ),
                    headers=session_headers,
                )
                if result["terminal"]:
                    break
                if time.monotonic() >= deadline:
                    self.fail("Presenter preview command did not become terminal")
            transcript_status, transcript, _ = request(
                running,
                "GET",
                f"/product/v1/sessions/{encoded_reference}/transcript",
                headers=session_headers,
            )
            close_status, closed, _ = request(
                running,
                "POST",
                f"/product/v1/sessions/{encoded_reference}/close",
                headers={
                    **session_headers,
                    "Idempotency-Key": "presenter-close-0001",
                },
                body={
                    "expected_control_revision": snapshot["snapshot"]["coordinates"][
                        "control_revision"
                    ]
                },
            )

        self.assertEqual(option_status, 200)
        self.assertEqual(exchange_status, 200)
        self.assertEqual(launch_status, 201)
        self.assertEqual(snapshot_status, 200)
        self.assertEqual(command_status, 202)
        self.assertEqual(result_status, 200)
        self.assertEqual(result["command"]["result"]["outcome"], "completed")
        self.assertEqual(transcript_status, 200)
        self.assertTrue(transcript["entries"])
        self.assertEqual(close_status, 200)
        self.assertEqual(closed["snapshot"]["lifecycle"]["state"], "closed")

    def test_once_subprocess_finishes_complete_response_before_exit(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                str(TOOL_PATH),
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--token",
                SERVICE_BEARER,
                "--contract-root",
                str(self.contract_root),
                "--once",
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        announcement = json.loads(process.stdout.readline())
        connection = http.client.HTTPConnection(
            announcement["host"], announcement["port"], timeout=5
        )
        connection.request(
            "GET",
            "/product/v1/capabilities",
            headers={"Authorization": f"Bearer {SERVICE_BEARER}"},
        )
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        connection.close()
        stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(response.status, 200)
        self.assertEqual(headers["X-Dreamland-Contract-Revision"], "0.7.0-rc.3")
        self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assertEqual(json.loads(body)["kind"], "product_capabilities")
        self.assertEqual(process.returncode, 0, (stdout, stderr))
        self.assertEqual(stderr, "")

    def test_rejects_non_loopback_and_bad_contract_before_listening(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            PREVIEW.build_preview_server(
                host="0.0.0.0",
                port=0,
                bearer=SERVICE_BEARER,
                scenario=PREVIEW.SCENARIOS[0],
                contract_root=self.contract_root,
            )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "contract"):
                PREVIEW.build_preview_server(
                    host="127.0.0.1",
                    port=0,
                    bearer=SERVICE_BEARER,
                    scenario=PREVIEW.SCENARIOS[0],
                    contract_root=Path(directory),
                )


if __name__ == "__main__":
    unittest.main()
