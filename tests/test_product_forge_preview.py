from __future__ import annotations

import http.client
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

CONTRACT_ROOT = Path("contracts/world-forge-api/v0.1.0-rc.2")


def _load_tool() -> ModuleType:
    path = Path("tools/product_forge_preview.py")
    specification = importlib.util.spec_from_file_location(
        "test_world_forge_preview_tool", path
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("World Forge preview tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _answers() -> list[dict[str, str]]:
    return [
        {"field_id": "day-or-date", "kind": "short-text", "value": "Saturday"},
        {"field_id": "year", "kind": "integer", "value": "+02026"},
        {"field_id": "adjective", "kind": "short-text", "value": "weathered"},
        {
            "field_id": "industry-or-setting",
            "kind": "short-text",
            "value": "observatory",
        },
        {"field_id": "technology-one", "kind": "short-text", "value": "VAX terminals"},
        {"field_id": "technology-two", "kind": "short-text", "value": "radio clocks"},
        {"field_id": "important-job", "kind": "short-text", "value": "track a comet"},
        {
            "field_id": "wildcard-event",
            "kind": "short-text",
            "value": "the clocks disagree",
        },
    ]


class RunningServer:
    def __init__(
        self, tool: ModuleType, *, scenario: str = "happy", delay: int = 20
    ) -> None:
        self.tool = tool
        self.actor = "actor:preview-test-presenter-00000001"
        self.bearer = "forge-preview-bearer:test-value-with-enough-entropy-00000001"
        self.server = tool.build_server(
            host="127.0.0.1",
            port=0,
            bearer=self.bearer,
            bootstrap=tool.ForgePreviewBootstrap(self.actor),
            scenario=scenario,
            step_delay_ms=delay,
            contract_root=CONTRACT_ROOT,
        )
        self.thread = threading.Thread(target=self.server.serve_forever)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def epoch(self) -> str:
        return str(self.server.preview_harness.service.service_epoch)

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        key: str | None = None,
        bearer: str | None = None,
        actor: str | None = None,
        epoch: str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {
            "Authorization": "Bearer " + (self.bearer if bearer is None else bearer),
            "Accept": "application/json",
        }
        if path != "/forge/v1/identity":
            headers["X-Dreamland-Actor-Context"] = (
                self.actor if actor is None else actor
            )
            headers["X-Dreamland-Expected-Epoch"] = (
                self.epoch if epoch is None else epoch
            )
        if body is not None:
            headers["Content-Type"] = content_type
        if key is not None:
            headers["Idempotency-Key"] = key
        payload = None if body is None else json.dumps(body)
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = response.read()
        response_headers = {
            name.lower(): value for name, value in response.getheaders()
        }
        connection.close()
        return response.status, json.loads(data), response_headers

    def prepare(self, answers=None) -> dict:
        status, payload, _headers = self.request(
            "POST",
            "/forge/v1/preparations",
            {
                "recipe_id": "world-recipe:bsides-room:v1",
                "recipe_revision": "1",
                "answers": answers or _answers(),
            },
        )
        if status != 201:
            raise AssertionError(payload)
        return payload

    def submit(self, preparation: dict, *, key: str = "submit-1") -> tuple[int, dict]:
        status, payload, _headers = self.request(
            "POST",
            "/forge/v1/builds",
            {"preparation_reference": preparation["preparation_reference"]},
            key=key,
        )
        return status, payload


class ProductForgePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _load_tool()

    def assert_contract_error(self, payload: dict, code: str) -> None:
        import jsonschema

        openapi = json.loads((CONTRACT_ROOT / "openapi.json").read_text())
        schema = {**openapi, "$ref": "#/components/schemas/SafeErrorEnvelope"}
        jsonschema.Draft202012Validator(schema).validate(payload)
        self.assertEqual(payload["error"]["code"], code)

    def test_identity_recipe_prepare_submit_refresh_and_selected(self) -> None:
        with RunningServer(self.tool) as running:
            status, identity, headers = running.request("GET", "/forge/v1/identity")
            self.assertEqual(status, 200)
            self.assertEqual(identity["service_epoch"], running.epoch)
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertEqual(headers["x-content-type-options"], "nosniff")

            status, recipes, _headers = running.request("GET", "/forge/v1/recipes")
            self.assertEqual(status, 200)
            year = recipes["recipes"][0]["fields"][1]
            self.assertEqual(year["maximum_input_characters"], 16)

            prepared = running.prepare()
            self.assertEqual(
                prepared["preparation"]["confirmation_state"], "unconfirmed"
            )
            self.assertNotIn("confirmed_at", prepared["preparation"])
            status, accepted = running.submit(prepared)
            self.assertEqual(status, 201)
            self.assertFalse(accepted["replayed"])

            reference = quote(accepted["reference"], safe="")
            deadline = time.monotonic() + 3
            while True:
                status, projection, _headers = running.request(
                    "GET", f"/forge/v1/builds/{reference}"
                )
                self.assertEqual(status, 200)
                if projection["terminal"]:
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            self.assertEqual(projection["lifecycle"]["state"], "selected")
            self.assertEqual(
                projection["receipt"]["provenance"]["kind"],
                "provider_free_integration_preview",
            )
            self.assertNotIn("publication", projection)
            self.assertNotIn("stage", projection)

    def test_submit_replay_and_changed_preparation_conflict(self) -> None:
        with RunningServer(self.tool, delay=100) as running:
            first = running.prepare()
            second_answers = _answers()
            second_answers[2] = {
                "field_id": "adjective",
                "kind": "short-text",
                "value": "dusty",
            }
            second = running.prepare(second_answers)
            status, accepted = running.submit(first, key="same-key")
            self.assertEqual(status, 201)
            status, replayed = running.submit(first, key="same-key")
            self.assertEqual(status, 201)
            self.assertTrue(replayed["replayed"])
            self.assertEqual(replayed["job_id"], accepted["job_id"])
            status, conflict = running.submit(second, key="same-key")
            self.assertEqual(status, 409)
            self.assertEqual(conflict["error"]["code"], "idempotency_conflict")

    def test_validation_wrong_authority_epoch_media_and_cursor_fail_safely(
        self,
    ) -> None:
        with RunningServer(self.tool, delay=100) as running:
            hostile = _answers()
            hostile[3] = {
                "field_id": "industry-or-setting",
                "kind": "short-text",
                "value": "admin@example.com",
            }
            status, failed, _headers = running.request(
                "POST",
                "/forge/v1/preparations",
                {
                    "recipe_id": "world-recipe:bsides-room:v1",
                    "recipe_revision": "1",
                    "answers": hostile,
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(failed["error"]["code"], "validation_failed")
            self.assertEqual(
                failed["error"]["issues"][0]["field_id"], "industry-or-setting"
            )

            status, denied, _headers = running.request(
                "GET", "/forge/v1/recipes", bearer="wrong-bearer"
            )
            self.assertEqual(status, 401)
            self.assertEqual(denied["error"]["code"], "authority_denied")
            status, lost, _headers = running.request(
                "GET", "/forge/v1/recipes", epoch="stale-epoch"
            )
            self.assertEqual(status, 409)
            self.assertEqual(lost["error"]["code"], "service_epoch_changed")
            self.assertEqual(lost["error"]["retry"], "none")

            prepared = running.prepare()
            status, accepted = running.submit(prepared)
            self.assertEqual(status, 201)
            reference = quote(accepted["reference"], safe="")
            status, resync, _headers = running.request(
                "GET", f"/forge/v1/builds/{reference}/events?after=1"
            )
            self.assertEqual(status, 409)
            self.assertEqual(resync["error"]["code"], "resync_required")
            self.assertEqual(resync["error"]["retry"], "resume")

            status, media, _headers = running.request(
                "POST",
                "/forge/v1/preparations",
                {"recipe_id": "x", "recipe_revision": "1", "answers": []},
                content_type="text/plain",
            )
            self.assertEqual(status, 415)
            self.assertEqual(media["error"]["code"], "unsupported_media_type")

    def test_recipe_identity_errors_keep_their_top_level_contract_codes(self) -> None:
        with RunningServer(self.tool, delay=100) as running:
            for recipe_id, revision, expected_status, expected_code in (
                ("world-recipe:unknown", "1", 400, "recipe_unknown"),
                (
                    "world-recipe:bsides-room:v1",
                    "stale",
                    409,
                    "recipe_revision_mismatch",
                ),
            ):
                with self.subTest(code=expected_code):
                    status, payload, _headers = running.request(
                        "POST",
                        "/forge/v1/preparations",
                        {
                            "recipe_id": recipe_id,
                            "recipe_revision": revision,
                            "answers": _answers(),
                        },
                    )
                    self.assertEqual(status, expected_status)
                    self.assertEqual(payload["error"]["code"], expected_code)
                    self.assertEqual(payload["error"]["retry"], "none")
                    self.assertNotIn("issues", payload["error"])

    def test_remaining_mutation_errors_are_reachable_and_contract_valid(self) -> None:
        with RunningServer(self.tool, delay=100) as running:
            cases: list[tuple[dict, str]] = []
            status, invalid, _headers = running.request(
                "POST", "/forge/v1/preparations", {"answers": []}
            )
            self.assertEqual(status, 400)
            cases.append((invalid, "invalid_request"))

            status, missing_preparation = running.submit(
                {"preparation_reference": "forge-preparation:missing"},
                key="missing-preparation",
            )
            self.assertEqual(status, 404)
            cases.append((missing_preparation, "preparation_not_found"))

            prepared = running.prepare()
            status, invalid_key = running.submit(prepared, key="x" * 129)
            self.assertEqual(status, 400)
            cases.append((invalid_key, "idempotency_key_invalid"))

            status, _accepted = running.submit(prepared, key="consume-first")
            self.assertEqual(status, 201)
            status, consumed = running.submit(prepared, key="consume-second")
            self.assertEqual(status, 409)
            cases.append((consumed, "preparation_consumed"))

            status, missing_job, _headers = running.request(
                "GET", "/forge/v1/builds/forge-reference%3Amissing"
            )
            self.assertEqual(status, 404)
            cases.append((missing_job, "job_not_found"))

            for payload, code in cases:
                self.assert_contract_error(payload, code)

    def test_contract_marks_only_defensive_unserved_errors_schema_only(self) -> None:
        index = json.loads((CONTRACT_ROOT / "fixtures/index.json").read_text())
        reachability = {
            entry["case"].removeprefix("error_"): entry["reachability"]
            for entry in index["fixtures"]
            if entry["classification"] == "safe-error"
        }
        self.assertEqual(
            {code for code, value in reachability.items() if value == "schema-only"},
            {"response_too_large", "temporarily_unavailable"},
        )
        self.assertEqual(
            {code for code, value in reachability.items() if value == "http"},
            set(self.tool._load_contract_tool().ERROR_CODES)
            - {"response_too_large", "temporarily_unavailable"},
        )

    def test_cancel_is_fenced_and_replayable(self) -> None:
        with RunningServer(self.tool, delay=500) as running:
            prepared = running.prepare()
            status, accepted = running.submit(prepared)
            self.assertEqual(status, 201)
            reference = quote(accepted["reference"], safe="")
            for replayed in (False, True):
                status, cancelled, _headers = running.request(
                    "POST",
                    f"/forge/v1/builds/{reference}/cancel",
                    {},
                    key="cancel-key",
                )
                self.assertEqual(status, 200)
                self.assertEqual(cancelled["replayed"], replayed)
            time.sleep(0.6)
            status, projection, _headers = running.request(
                "GET", f"/forge/v1/builds/{reference}"
            )
            self.assertEqual(status, 200)
            self.assertEqual(projection["lifecycle"]["state"], "cancelled")
            self.assertTrue(projection["terminal"])

    def test_terminal_cancel_is_non_retryable_and_replays_earlier_success(self) -> None:
        with RunningServer(self.tool, delay=20) as running:
            prepared = running.prepare()
            _status, accepted = running.submit(prepared)
            reference = quote(accepted["reference"], safe="")
            deadline = time.monotonic() + 3
            while True:
                status, projection, _headers = running.request(
                    "GET", f"/forge/v1/builds/{reference}"
                )
                self.assertEqual(status, 200)
                if projection["terminal"]:
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            for _index in range(2):
                status, payload, _headers = running.request(
                    "POST",
                    f"/forge/v1/builds/{reference}/cancel",
                    {},
                    key="terminal-cancel",
                )
                self.assertEqual(status, 409)
                self.assertEqual(payload["error"]["code"], "job_terminal")
                self.assertEqual(payload["error"]["retry"], "none")

    def test_lost_submit_response_replays_without_duplicate_dispatch(self) -> None:
        with RunningServer(
            self.tool, scenario="submit-response-lost", delay=100
        ) as running:
            prepared = running.prepare()
            body = json.dumps(
                {"preparation_reference": prepared["preparation_reference"]}
            )
            connection = http.client.HTTPConnection(
                "127.0.0.1", running.port, timeout=3
            )
            connection.request(
                "POST",
                "/forge/v1/builds",
                body=body,
                headers={
                    "Authorization": "Bearer " + running.bearer,
                    "X-Dreamland-Actor-Context": running.actor,
                    "X-Dreamland-Expected-Epoch": running.epoch,
                    "Idempotency-Key": "lost-submit",
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(http.client.RemoteDisconnected):
                connection.getresponse()
            connection.close()

            status, replayed = running.submit(prepared, key="lost-submit")
            self.assertEqual(status, 201)
            self.assertTrue(replayed["replayed"])
            self.assertEqual(replayed["position"]["sequence"], 1)

    def test_capacity_rejection_and_withheld_event_reconcile(self) -> None:
        with RunningServer(self.tool, scenario="capacity", delay=100) as running:
            first = running.prepare()
            second = running.prepare()
            self.assertEqual(running.submit(first, key="first")[0], 201)
            for _index in range(2):
                status, payload = running.submit(second, key="capacity")
                self.assertEqual(status, 503)
                self.assertEqual(payload["error"]["code"], "capacity_exhausted")
                self.assertEqual(payload["error"]["retry"], "same_request")

        with RunningServer(self.tool, scenario="withheld-event", delay=20) as running:
            prepared = running.prepare()
            _status, accepted = running.submit(prepared)
            time.sleep(0.08)
            reference = quote(accepted["reference"], safe="")
            cursor = quote(accepted["position"]["cursor"], safe="")
            status, withheld, _headers = running.request(
                "GET", f"/forge/v1/builds/{reference}/events?after={cursor}"
            )
            self.assertEqual(status, 409)
            self.assertEqual(withheld["error"]["code"], "resync_required")
            status, snapshot, _headers = running.request(
                "GET", f"/forge/v1/builds/{reference}"
            )
            self.assertEqual(status, 200)
            self.assertGreater(snapshot["position"]["sequence"], 1)

    def test_preparation_and_idempotency_capacity_and_expiry_are_explicit(self) -> None:
        with RunningServer(
            self.tool, scenario="preparation-capacity", delay=100
        ) as running:
            running.prepare()
            status, payload, _headers = running.request(
                "POST",
                "/forge/v1/preparations",
                {
                    "recipe_id": "world-recipe:bsides-room:v1",
                    "recipe_revision": "1",
                    "answers": _answers(),
                },
            )
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"]["code"], "preparation_capacity_exhausted")

        with RunningServer(
            self.tool, scenario="idempotency-capacity", delay=100
        ) as running:
            first = running.prepare()
            second = running.prepare()
            self.assertEqual(running.submit(first, key="fills-ledger")[0], 201)
            for _index in range(2):
                status, payload = running.submit(second, key="ledger-full")
                self.assertEqual(status, 503)
                self.assertEqual(
                    payload["error"]["code"], "idempotency_capacity_exhausted"
                )
                self.assertEqual(payload["error"]["retry"], "none")

        with RunningServer(
            self.tool, scenario="preparation-expiry", delay=100
        ) as running:
            prepared = running.prepare()
            time.sleep(1.05)
            status, payload = running.submit(prepared, key="expired")
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"]["code"], "preparation_expired")

    def test_cross_actor_oversized_and_terminal_failures_are_safe(self) -> None:
        for scenario, expected_state in (
            ("failure", "failed"),
            ("deadline", "deadline_exceeded"),
        ):
            with self.subTest(scenario=scenario):
                with RunningServer(self.tool, scenario=scenario, delay=20) as running:
                    prepared = running.prepare()
                    _status, accepted = running.submit(prepared)
                    reference = quote(accepted["reference"], safe="")
                    status, denied, _headers = running.request(
                        "GET",
                        f"/forge/v1/builds/{reference}",
                        actor="actor:different-presenter-00000001",
                    )
                    self.assertEqual(status, 403)
                    self.assertEqual(denied["error"]["code"], "authority_denied")
                    deadline = time.monotonic() + 3
                    while True:
                        status, projection, _headers = running.request(
                            "GET", f"/forge/v1/builds/{reference}"
                        )
                        if projection.get("terminal"):
                            break
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.02)
                    self.assertEqual(status, 200)
                    self.assertEqual(projection["lifecycle"]["state"], expected_state)
                    self.assertTrue(projection["lifecycle"]["safe_message"])
                    if scenario == "deadline":
                        changed_at = datetime.fromisoformat(
                            projection["lifecycle"]["changed_at"]
                        )
                        deadline_at = datetime.fromisoformat(projection["deadline_at"])
                        self.assertGreaterEqual(changed_at, deadline_at)

        with RunningServer(self.tool, delay=100) as running:
            connection = http.client.HTTPConnection(
                "127.0.0.1", running.port, timeout=3
            )
            connection.request(
                "POST",
                "/forge/v1/preparations",
                body=b"x" * (self.tool.MAX_REQUEST_BYTES + 1),
                headers={
                    "Authorization": "Bearer " + running.bearer,
                    "X-Dreamland-Actor-Context": running.actor,
                    "X-Dreamland-Expected-Epoch": running.epoch,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 413)
            self.assertEqual(payload["error"]["code"], "request_too_large")

    def test_cursor_loss_and_epoch_loss_scenarios_are_honest(self) -> None:
        with RunningServer(self.tool, scenario="cursor-loss") as running:
            prepared = running.prepare()
            _status, accepted = running.submit(prepared)
            reference = quote(accepted["reference"], safe="")
            cursor = quote(accepted["position"]["cursor"], safe="")
            status, payload, _headers = running.request(
                "GET", f"/forge/v1/builds/{reference}/events?after={cursor}"
            )
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"]["code"], "resync_required")

        with RunningServer(self.tool, scenario="epoch-loss", delay=20) as running:
            old_epoch = running.epoch
            prepared = running.prepare()
            _status, accepted = running.submit(prepared)
            reference = quote(accepted["reference"], safe="")
            time.sleep(0.08)
            status, payload, _headers = running.request(
                "GET", f"/forge/v1/builds/{reference}", epoch=old_epoch
            )
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"]["code"], "service_epoch_changed")
            self.assertEqual(payload["error"]["retry"], "none")

    def test_private_file_init_is_owner_only_and_no_follow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "token"
            bootstrap = root / "bootstrap.json"
            self.tool.initialize_private_files(
                token_file=token, bootstrap_file=bootstrap
            )
            self.assertEqual(os.stat(token).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(bootstrap).st_mode & 0o777, 0o600)
            self.assertTrue(
                self.tool.load_bearer(token).startswith("forge-preview-bearer:")
            )
            self.assertTrue(
                self.tool.load_bootstrap(bootstrap).actor_context.startswith("actor:")
            )
            with self.assertRaises(FileExistsError):
                self.tool.initialize_private_files(
                    token_file=token, bootstrap_file=root / "another.json"
                )

    def test_once_subprocess_finishes_complete_identity_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "token"
            bootstrap = root / "bootstrap.json"
            init = subprocess.run(
                [
                    sys.executable,
                    "tools/product_forge_preview.py",
                    "init",
                    "--token-file",
                    str(token),
                    "--bootstrap-file",
                    str(bootstrap),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(json.loads(init.stdout)["created"])
            process = subprocess.Popen(
                [
                    sys.executable,
                    "tools/product_forge_preview.py",
                    "serve",
                    "--token-file",
                    str(token),
                    "--bootstrap-file",
                    str(bootstrap),
                    "--contract-root",
                    str(CONTRACT_ROOT),
                    "--port",
                    "0",
                    "--once",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert process.stdout is not None
            announcement = json.loads(process.stdout.readline())
            bearer = token.read_text().strip()
            connection = http.client.HTTPConnection(
                announcement["host"], announcement["port"], timeout=3
            )
            connection.request(
                "GET",
                "/forge/v1/identity",
                headers={"Authorization": "Bearer " + bearer},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            headers = {name.lower(): value for name, value in response.getheaders()}
            connection.close()
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(response.status, 200)
            self.assertEqual(headers["content-type"], "application/json")
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertEqual(payload["contract_revision"], "0.1.0-rc.2")
            self.assertEqual(stdout, "")
            self.assertEqual(process.returncode, 0, stderr)


if __name__ == "__main__":
    unittest.main()
