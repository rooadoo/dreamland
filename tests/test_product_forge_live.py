from __future__ import annotations

import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from dreamland.models import CommandStateUpdate
from dreamland.product.forge_artifacts import compile_world_seed, live_forge_receipt
from dreamland.product.forge_live import SpiritguideForgeProfileGenerator
from dreamland.spiritguide import SpiritguideResult


def _load_tool() -> ModuleType:
    path = Path("tools/product_forge_live.py")
    specification = importlib.util.spec_from_file_location(
        "test_world_forge_live_tool", path
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("live World Forge tool is unavailable")
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


def _receipt() -> dict[str, object]:
    digest = "sha256:" + "a" * 64
    return live_forge_receipt(
        compiler_revision="1",
        validator_revision="1",
        revision_id="world-revision:" + digest,
        digest=digest,
    )


class Runner:
    def __init__(self, *, failure: bool = False, gate: threading.Event | None = None):
        self.failure = failure
        self.gate = gate
        self.calls = 0

    def run(self, _seed):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(3)
        if self.failure:
            raise ValueError("unsafe provider detail")
        return SimpleNamespace(receipt=_receipt())


class ProductForgeLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _load_tool()

    def _submit(self, harness):
        service = harness.service
        prepared = service.prepare(
            actor_authority=harness.actor_context,
            expected_epoch=service.service_epoch,
            recipe_id="world-recipe:bsides-room:v1",
            recipe_revision="1",
            answers=_answers(),
        )
        accepted = service.confirm_and_submit(
            actor_authority=harness.actor_context,
            expected_epoch=service.service_epoch,
            idempotency_key="live-submit",
            preparation_reference=str(prepared["preparation_reference"]),
        )
        harness.schedule(str(accepted["reference"]))
        return str(accepted["reference"])

    def _wait_terminal(self, harness, reference, timeout=3):
        deadline = time.monotonic() + timeout
        while True:
            value = harness.service.status(
                actor_authority=harness.actor_context,
                expected_epoch=harness.service.service_epoch,
                reference=reference,
            )
            if value["terminal"]:
                return value
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    def test_live_harness_selects_worker_receipt(self) -> None:
        runner = Runner()
        harness = self.tool.LiveForgeHarness(
            actor_context="actor:live-test-presenter", runner=runner, deadline_seconds=2
        )
        try:
            projection = self._wait_terminal(harness, self._submit(harness))
            self.assertEqual(projection["lifecycle"]["state"], "selected")
            self.assertEqual(
                projection["source"]["kind"], "single_attempt_live_vertical_slice"
            )
            self.assertEqual(projection["receipt"], _receipt())
            self.assertEqual(runner.calls, 1)
        finally:
            harness.close()

    def test_selected_observer_failure_is_isolated_from_forge_truth(self) -> None:
        observed: list[str] = []

        def reject_registration(reference: str) -> None:
            observed.append(reference)
            raise RuntimeError("private registration failure detail")

        harness = self.tool.LiveForgeHarness(
            actor_context="actor:live-test-presenter",
            runner=Runner(),
            deadline_seconds=2,
            on_selected=reject_registration,
        )
        try:
            with self.assertLogs("dreamland.failures", level="ERROR") as captured:
                reference = self._submit(harness)
                projection = self._wait_terminal(harness, reference)
            self.assertEqual(projection["lifecycle"]["state"], "selected")
            self.assertEqual(projection["receipt"], _receipt())
            self.assertEqual(observed, [reference])
            logged = "\n".join(captured.output)
            self.assertIn("world_forge_launch_option_registration_failed", logged)
            self.assertIn("failure_type=RuntimeError", logged)
            self.assertNotIn("private registration failure detail", logged)
        finally:
            harness.close()

    def test_selected_observer_is_not_called_for_failed_generation(self) -> None:
        observed: list[str] = []
        harness = self.tool.LiveForgeHarness(
            actor_context="actor:live-test-presenter",
            runner=Runner(failure=True),
            deadline_seconds=2,
            on_selected=observed.append,
        )
        try:
            projection = self._wait_terminal(harness, self._submit(harness))
            self.assertEqual(projection["lifecycle"]["state"], "failed")
            self.assertEqual(observed, [])
        finally:
            harness.close()

    def test_combined_policy_overrides_are_opt_in(self) -> None:
        ordinary = self.tool.LiveForgeHarness(
            actor_context="actor:live-test-presenter",
            runner=Runner(),
            deadline_seconds=2,
        )
        combined = self.tool.LiveForgeHarness(
            actor_context="actor:live-test-presenter",
            runner=Runner(),
            deadline_seconds=2,
            maximum_jobs=16,
            selected_handoff_ttl_seconds=3_600,
        )
        try:
            self.assertEqual(ordinary.service._policy.maximum_jobs, 1)
            self.assertEqual(
                ordinary.service._policy.selected_handoff_ttl_seconds,
                300,
            )
            self.assertEqual(combined.service._policy.maximum_jobs, 16)
            self.assertEqual(
                combined.service._policy.selected_handoff_ttl_seconds,
                3_600,
            )
        finally:
            ordinary.close()
            combined.close()

    def test_runner_reports_canonical_resolved_model(self) -> None:
        settings = SimpleNamespace(
            spiritguide=SimpleNamespace(
                model_reference=SimpleNamespace(provider="openai", model="gpt-test"),
                max_output_tokens=4_096,
            ),
            openai=SimpleNamespace(api_key="private-test-key"),
            langfuse=SimpleNamespace(
                enabled=False,
                public_key=None,
                secret_key=None,
                host=None,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(self.tool, "ModelSpiritguide", return_value=object()),
                patch.object(
                    self.tool,
                    "SpiritguideForgeProfileGenerator",
                    return_value=object(),
                ),
                patch.object(
                    self.tool,
                    "SingleAttemptForge",
                    return_value=SimpleNamespace(store=object()),
                ),
            ):
                _runner, model, max_output_tokens = self.tool.build_runner(
                    settings,
                    Path(directory),
                )
        self.assertEqual(model, "openai:gpt-test")
        self.assertEqual(max_output_tokens, 4_096)

    def test_service_rejects_worker_receipt_from_another_source(self) -> None:
        harness = self.tool.LiveForgeHarness(
            actor_context="actor:live-test-presenter",
            runner=Runner(),
            deadline_seconds=2,
        )
        try:
            service = harness.service
            prepared = service.prepare(
                actor_authority=harness.actor_context,
                expected_epoch=service.service_epoch,
                recipe_id="world-recipe:bsides-room:v1",
                recipe_revision="1",
                answers=_answers(),
            )
            accepted = service.confirm_and_submit(
                actor_authority=harness.actor_context,
                expected_epoch=service.service_epoch,
                idempotency_key="foreign-receipt",
                preparation_reference=str(prepared["preparation_reference"]),
            )
            reference = str(accepted["reference"])
            for state in (
                self.tool.ForgeJobState.QUEUED,
                self.tool.ForgeJobState.BUILDING,
                self.tool.ForgeJobState.GENERATED,
                self.tool.ForgeJobState.VALIDATING,
            ):
                service.preview_transition(reference=reference, state=state)
            receipt = _receipt()
            receipt["provenance"] = {"kind": "provider_free_integration_preview"}
            with self.assertRaises(self.tool.ForgeServiceError) as captured:
                service.complete_selected(reference=reference, receipt=receipt)
            self.assertEqual(captured.exception.code, "transition_invalid")
            projection = service.status(
                actor_authority=harness.actor_context,
                expected_epoch=service.service_epoch,
                reference=reference,
            )
            self.assertEqual(projection["lifecycle"]["state"], "validating")
            self.assertIsNone(projection["receipt"])
        finally:
            harness.close()

    def test_failure_is_safe_and_deadline_fences_late_result(self) -> None:
        failure = self.tool.LiveForgeHarness(
            actor_context="actor:live-test-presenter",
            runner=Runner(failure=True),
            deadline_seconds=2,
        )
        try:
            projection = self._wait_terminal(failure, self._submit(failure))
            self.assertEqual(projection["lifecycle"]["state"], "failed")
            self.assertNotIn(
                "unsafe provider detail", projection["lifecycle"]["safe_message"]
            )
        finally:
            failure.close()

        gate = threading.Event()
        late = self.tool.LiveForgeHarness(
            actor_context="actor:live-test-presenter",
            runner=Runner(gate=gate),
            deadline_seconds=1,
        )
        try:
            reference = self._submit(late)
            projection = self._wait_terminal(late, reference)
            self.assertEqual(projection["lifecycle"]["state"], "deadline_exceeded")
            gate.set()
            time.sleep(0.05)
            projection = self._wait_terminal(late, reference)
            self.assertEqual(projection["lifecycle"]["state"], "deadline_exceeded")
            self.assertIsNone(projection["receipt"])
        finally:
            gate.set()
            late.close()

    def test_cancellation_fences_a_late_live_result(self) -> None:
        gate = threading.Event()
        harness = self.tool.LiveForgeHarness(
            actor_context="actor:live-test-presenter",
            runner=Runner(gate=gate),
            deadline_seconds=2,
        )
        try:
            reference = self._submit(harness)
            deadline = time.monotonic() + 2
            while True:
                snapshot = harness.service.status(
                    actor_authority=harness.actor_context,
                    expected_epoch=harness.service.service_epoch,
                    reference=reference,
                )
                if snapshot["lifecycle"]["state"] == "building":
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            cancelled = harness.service.cancel(
                actor_authority=harness.actor_context,
                expected_epoch=harness.service.service_epoch,
                reference=reference,
                idempotency_key="cancel-live",
            )
            self.assertEqual(cancelled["source"], harness.service.source)
            gate.set()
            projection = self._wait_terminal(harness, reference)
            time.sleep(0.05)
            self.assertEqual(projection["lifecycle"]["state"], "cancelled")
            self.assertIsNone(projection["receipt"])
        finally:
            gate.set()
            harness.close()

    def test_spiritguide_generator_uses_exact_instruction_and_private_candidate(
        self,
    ) -> None:
        world = {
            "name": "comet-observatory",
            "entry_host": "console",
            "active_host": "console",
            "purpose": {"theme": "Weathered observatory"},
            "hosts_by_name": {
                "console": {
                    "hostname": "console",
                    "login_banner": "",
                    "purpose": {"theme": "Track a comet"},
                    "shell": {
                        "default": {
                            "name": "bash",
                            "command": "/bin/bash",
                            "argv": ["bash"],
                        }
                    },
                }
            },
        }

        class Spiritguide:
            def __init__(self):
                self.contexts = []

            def respond(self, context):
                self.contexts.append(context)
                return SpiritguideResult(
                    state_update=CommandStateUpdate(world_state=world),
                    metadata={"structured_response_accepted": True},
                )

        with tempfile.TemporaryDirectory() as directory:
            spiritguide = Spiritguide()
            generator = SpiritguideForgeProfileGenerator(
                spiritguide=spiritguide,
                candidate_directory=Path(directory) / "candidates",
            )
            from tests.test_product_forge_artifacts import _seed

            instruction = compile_world_seed(_seed())
            with self.assertLogs("dreamland.admin", level="INFO") as captured:
                profile = generator.generate(instruction)
            self.assertEqual(spiritguide.contexts[0].instruction, instruction.text)
            logged = "\n".join(captured.output)
            self.assertIn("Detached Spiritguide profile completed", logged)
            self.assertNotIn(instruction.text, logged)
            self.assertEqual(profile.state["world_state"]["entry_host"], "console")
            self.assertEqual(profile.path if hasattr(profile, "path") else None, None)
            candidate_directory = Path(directory) / "candidates"
            self.assertEqual(os.stat(candidate_directory).st_mode & 0o777, 0o700)
            candidate_files = tuple(candidate_directory.glob("*.json"))
            self.assertEqual(len(candidate_files), 1)
            self.assertEqual(os.stat(candidate_files[0]).st_mode & 0o777, 0o600)

    def test_invalid_listener_and_deadline_values_fail_before_provider_setup(
        self,
    ) -> None:
        common = [
            "serve",
            "--token-file",
            "/does/not/exist/token",
            "--bootstrap-file",
            "/does/not/exist/bootstrap",
            "--contract-root",
            "/does/not/exist/contract",
            "--artifact-root",
            "/does/not/exist/artifacts",
        ]
        for extra, expected in (
            (["--deadline-seconds", "0"], "deadline seconds must be positive"),
            (["--host", "0.0.0.0"], "only to explicit loopback"),
            (["--port", "65536"], "between 0 and 65535"),
        ):
            with self.subTest(extra=extra):
                errors = StringIO()
                with redirect_stderr(errors):
                    result = self.tool.main([*common, *extra])
                self.assertEqual(result, 2)
                self.assertIn(expected, errors.getvalue())

    def test_live_harness_uses_rc4_with_invariant_source_and_runtime_receipt(
        self,
    ) -> None:
        harness = self.tool.LiveForgeHarness(
            actor_context="actor:live-http-presenter",
            runner=Runner(),
            deadline_seconds=2,
        )
        server = self.tool._TRANSPORT.ForgePreviewServer(
            ("127.0.0.1", 0),
            bearer="forge-live-test-bearer",
            harness=harness,
            contract_revision=self.tool.CONTRACT_REVISION,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def request(method, path, body=None, key=None):
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=3
            )
            headers = {
                "Authorization": "Bearer forge-live-test-bearer",
                "Accept": "application/json",
            }
            if path != "/forge/v1/identity":
                headers["X-Dreamland-Actor-Context"] = harness.actor_context
                headers["X-Dreamland-Expected-Epoch"] = harness.service.service_epoch
            if body is not None:
                headers["Content-Type"] = "application/json"
            if key is not None:
                headers["Idempotency-Key"] = key
            connection.request(
                method,
                path,
                body=None if body is None else json.dumps(body),
                headers=headers,
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            return response.status, payload

        try:
            status, identity = request("GET", "/forge/v1/identity")
            self.assertEqual(status, 200)
            self.assertEqual(identity["contract_revision"], "0.1.0-rc.4")
            self.assertEqual(
                identity["source"]["kind"], "single_attempt_live_vertical_slice"
            )
            source = identity["source"]
            status, recipes = request("GET", "/forge/v1/recipes")
            self.assertEqual(status, 200)
            self.assertEqual(recipes["source"], source)
            status, prepared = request(
                "POST",
                "/forge/v1/preparations",
                {
                    "recipe_id": "world-recipe:bsides-room:v1",
                    "recipe_revision": "1",
                    "answers": _answers(),
                },
            )
            self.assertEqual(status, 201)
            self.assertEqual(prepared["source"], source)
            status, accepted = request(
                "POST",
                "/forge/v1/builds",
                {"preparation_reference": prepared["preparation_reference"]},
                "live-http-submit",
            )
            self.assertEqual(status, 201)
            self.assertEqual(accepted["source"], source)
            reference = quote(accepted["reference"], safe="")
            deadline = time.monotonic() + 3
            while True:
                status, projection = request("GET", f"/forge/v1/builds/{reference}")
                self.assertEqual(status, 200)
                if projection["terminal"]:
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            self.assertEqual(projection["lifecycle"]["state"], "selected")
            self.assertEqual(projection["source"], source)
            self.assertEqual(projection["receipt"]["provenance"], source)
            status, events = request(
                "GET",
                f"/forge/v1/builds/{reference}/events?after="
                + quote(str(accepted["position"]["cursor"]), safe=""),
            )
            self.assertEqual(status, 200)
            self.assertEqual(events["source"], source)
            canonical = json.loads(
                Path(
                    "contracts/world-forge-api/v0.1.0-rc.4/fixtures/"
                    "canonical-valid/lifecycle/selected-live-vertical-slice.json"
                ).read_text()
            )
            self.assertEqual(projection["receipt"], canonical["receipt"])

            harness.service.preview_rotate_epoch()
            gate = threading.Event()
            harness.runner = Runner(gate=gate)
            status, rotated_identity = request("GET", "/forge/v1/identity")
            self.assertEqual(status, 200)
            self.assertEqual(rotated_identity["source"], source)
            status, cancel_prepared = request(
                "POST",
                "/forge/v1/preparations",
                {
                    "recipe_id": "world-recipe:bsides-room:v1",
                    "recipe_revision": "1",
                    "answers": _answers(),
                },
            )
            self.assertEqual(status, 201)
            status, cancel_accepted = request(
                "POST",
                "/forge/v1/builds",
                {"preparation_reference": cancel_prepared["preparation_reference"]},
                "live-http-cancel-submit",
            )
            self.assertEqual(status, 201)
            cancel_reference = quote(cancel_accepted["reference"], safe="")
            status, cancelled = request(
                "POST",
                f"/forge/v1/builds/{cancel_reference}/cancel",
                {},
                "live-http-cancel",
            )
            self.assertEqual(status, 200)
            self.assertEqual(cancelled["source"], source)
            gate.set()
        finally:
            if "gate" in locals():
                gate.set()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
