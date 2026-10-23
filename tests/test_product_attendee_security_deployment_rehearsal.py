from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dreamland.product.attendee_live import (
    _read_provider_credential,
    validate_attendee_live_credential_file,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = (
    REPOSITORY_ROOT / "tools" / "product_attendee_security_deployment_rehearsal.py"
)
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.8.0-rc.4"


def _load_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_attendee_deployment_rehearsal_test_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("attendee deployment rehearsal tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    prior = sys.modules.get(specification.name)
    sys.modules[specification.name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        if prior is None:
            sys.modules.pop(specification.name, None)
        else:
            sys.modules[specification.name] = prior
    return module


TOOL = _load_tool()


def _direct_child_pids(pid: int) -> list[int]:
    path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        return [int(value) for value in path.read_text(encoding="ascii").split()]
    except FileNotFoundError:
        return []


def _kill_process_groups(group_ids: list[int]) -> None:
    for group_id in group_ids:
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _valid_public_report() -> dict[str, object]:
    checks: list[dict[str, object]] = []
    for identifier in TOOL._FIXED_CASES:
        expected_metrics, codes = TOOL._EXPECTED_CHECK_EVIDENCE[identifier]
        metrics = {
            name: (1 if expected is None else expected)
            for name, expected in expected_metrics.items()
        }
        checks.append(TOOL._Check(identifier, metrics=metrics, codes=codes).public())
    return {
        "kind": TOOL.REPORT_KIND,
        "schema": TOOL.SCHEMA,
        "scope": TOOL.SCOPE,
        "production_capability_evidence": False,
        "contract": {
            "name": "dreamland-private-product-api",
            "revision": "0.8.0-rc.4",
            "compatibility": "unfrozen-release-candidate",
            "backend_commit": "0" * 40,
            "manifest_sha256": "0" * 64,
            "openapi_sha256": "0" * 64,
            "generated_client_sha256": "0" * 64,
        },
        "limits": {
            "contexts": TOOL.CONTEXT_CAPACITY,
            "sessions": TOOL.SESSION_CAPACITY,
            "workers": TOOL.WORKER_CAPACITY,
            "connections": TOOL.CONNECTION_CAPACITY,
        },
        "checks": checks,
        "summary": {
            "passed": True,
            "checks_passed": len(checks),
            "checks_total": len(checks),
            "elapsed_ms": 1,
        },
    }


class ProductAttendeeSecurityDeploymentRehearsalTests(unittest.TestCase):
    def test_import_is_inert(self) -> None:
        script = f"""
import importlib.util, json, pathlib, sys, threading
path = pathlib.Path({str(TOOL_PATH)!r})
before = [(thread.ident, thread.name) for thread in threading.enumerate()]
spec = importlib.util.spec_from_file_location("attendee_rehearsal_inert", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
after = [(thread.ident, thread.name) for thread in threading.enumerate()]
print(json.dumps({{
    "threads_unchanged": before == after,
    "schema": module.SCHEMA,
    "scope": module.SCOPE,
}}))
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(result.stdout),
            {
                "threads_unchanged": True,
                "schema": "dreamland.attendee-deployment-rehearsal/v1",
                "scope": "loopback_provider_free",
            },
        )
        self.assertEqual(result.stderr, "")

    def test_provider_placeholder_is_metadata_valid_but_content_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "provider.key"
            TOOL._write_owner_file(credential, "\n")

            self.assertEqual(
                validate_attendee_live_credential_file(credential),
                credential.resolve(),
            )
            with self.assertRaisesRegex(ValueError, "one API key"):
                _read_provider_credential(credential)

    def test_complete_provider_free_rehearsal(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(TOOL_PATH),
                "run",
                "--contract-root",
                str(CONTRACT_ROOT),
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(
                timeout=TOOL.PUBLIC_RUN_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            fixture_groups = _direct_child_pids(process.pid)
            process.send_signal(signal.SIGTERM)
            try:
                process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
            _kill_process_groups(fixture_groups)
            raise
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(len(stdout.splitlines()), 1)
        report = json.loads(stdout)
        TOOL._validate_report(report, sentinels=())
        self.assertEqual(report["kind"], TOOL.REPORT_KIND)
        self.assertEqual(report["scope"], "loopback_provider_free")
        self.assertFalse(report["production_capability_evidence"])
        self.assertEqual(report["contract"]["revision"], "0.8.0-rc.4")
        self.assertEqual(
            report["limits"],
            {"contexts": 4, "sessions": 4, "workers": 2, "connections": 8},
        )
        self.assertTrue(report["summary"]["passed"])
        self.assertEqual(report["summary"]["checks_total"], 11)
        self.assertEqual(
            {check["id"] for check in report["checks"]},
            set(TOOL._FIXED_CASES),
        )

        sentinel = "a" * 40
        sentinel_report = json.loads(json.dumps(report))
        sentinel_report["contract"]["backend_commit"] = sentinel
        with self.assertRaisesRegex(AssertionError, "private rehearsal value"):
            TOOL._validate_report(sentinel_report, sentinels=(sentinel,))

        incomplete_report = json.loads(json.dumps(report))
        contract_check = next(
            check
            for check in incomplete_report["checks"]
            if check["id"] == "contract_identity"
        )
        contract_check["metrics"] = {}
        with self.assertRaisesRegex(ValueError, "evidence"):
            TOOL._validate_report(incomplete_report, sentinels=())

    def test_public_sigterm_cleans_the_isolated_fixture_group(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(TOOL_PATH),
                "run",
                "--contract-root",
                str(CONTRACT_ROOT),
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        fixture_pid: int | None = None
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                self.assertIsNone(
                    process.poll(), "rehearsal exited before fixture start"
                )
                children = _direct_child_pids(process.pid)
                if children:
                    fixture_pid = children[0]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(fixture_pid, "rehearsal did not start a fixture")
            time.sleep(0.1)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 143, (stdout, stderr))
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "")
            assert fixture_pid is not None
            group_deadline = time.monotonic() + 5
            while time.monotonic() < group_deadline:
                try:
                    os.killpg(fixture_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("fixture process group survived public SIGTERM")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
            if fixture_pid is not None:
                _kill_process_groups([fixture_pid])

    def test_force_stop_kills_a_group_after_its_leader_exits(self) -> None:
        class ExitedLeader:
            def communicate(self, timeout: float):
                return "", ""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = TOOL._FixtureProcess(
                contract_root=CONTRACT_ROOT,
                root=root,
                token_file=root / "token",
                bootstrap_file=root / "bootstrap",
                profile_path=root / "profile",
                provider_file=root / "provider",
                artifact_root=root / "artifacts",
                close_after_ms=2_000,
                recovery_after_ms=3_000,
                sentinels=[],
            )
            fixture.process = ExitedLeader()
            fixture._process_group_id = 424_242
            with mock.patch.object(TOOL.os, "killpg") as kill_group:
                fixture._force_stop()
            kill_group.assert_called_once_with(424_242, signal.SIGKILL)

    def test_signal_during_process_creation_cleans_registered_group(self) -> None:
        class CreatedProcess:
            pid = 424_242

            def communicate(self, timeout: float):
                return "", ""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = TOOL._FixtureProcess(
                contract_root=CONTRACT_ROOT,
                root=root,
                token_file=root / "token",
                bootstrap_file=root / "bootstrap",
                profile_path=root / "profile",
                provider_file=root / "provider",
                artifact_root=root / "artifacts",
                close_after_ms=2_000,
                recovery_after_ms=3_000,
                sentinels=[],
            )

            def interrupted_creation(*_args, **_kwargs):
                os.kill(os.getpid(), signal.SIGTERM)
                return CreatedProcess()

            with (
                mock.patch.object(TOOL.subprocess, "Popen", interrupted_creation),
                mock.patch.object(TOOL.os, "killpg") as kill_group,
                mock.patch.object(
                    TOOL, "_run_rehearsal", lambda _root: fixture.start()
                ),
                self.assertRaises(TOOL._RehearsalSignal) as raised,
            ):
                TOOL._run_rehearsal_with_signal_cleanup(CONTRACT_ROOT)

            self.assertEqual(raised.exception.signum, signal.SIGTERM)
            self.assertIsInstance(fixture.process, CreatedProcess)
            kill_group.assert_called_once_with(424_242, signal.SIGKILL)
            self.assertTrue(fixture._forced_cleanup_complete)
            self.assertEqual(TOOL._SIGNAL_DEFERRAL_DEPTH, 0)
            self.assertIsNone(TOOL._DEFERRED_REHEARSAL_SIGNAL)

    def test_public_failure_is_typed_and_does_not_emit_a_traceback(self) -> None:
        missing_contract = REPOSITORY_ROOT / "contracts" / "missing-rehearsal-rc"
        self.assertFalse(missing_contract.exists())

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(TOOL_PATH),
                "run",
                "--contract-root",
                str(missing_contract),
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            json.loads(result.stderr),
            {"error": {"code": "rehearsal_failed", "retry": "after_review"}},
        )
        self.assertNotIn("Traceback", result.stderr)

        private_argument = "private-rehearsal-path-do-not-echo"
        invalid_arguments = subprocess.run(
            [
                sys.executable,
                "-B",
                str(TOOL_PATH),
                "run",
                "--unknown-private-option",
                private_argument,
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(invalid_arguments.returncode, 1)
        self.assertEqual(invalid_arguments.stdout, "")
        self.assertEqual(
            json.loads(invalid_arguments.stderr),
            {"error": {"code": "rehearsal_failed", "retry": "after_review"}},
        )
        self.assertNotIn(private_argument, invalid_arguments.stderr)
        self.assertNotIn("usage:", invalid_arguments.stderr)

    def test_report_guard_rejects_private_keys_and_production_claims(self) -> None:
        report = _valid_public_report()
        TOOL._validate_report(report, sentinels=())

        production_claim = json.loads(json.dumps(report))
        production_claim["production_capability_evidence"] = True
        with self.assertRaisesRegex(ValueError, "identity"):
            TOOL._validate_report(production_claim, sentinels=())

        private_key = json.loads(json.dumps(report))
        private_key["secret"] = "must-not-cross"
        with self.assertRaisesRegex(ValueError, "private key"):
            TOOL._validate_report(private_key, sentinels=())


if __name__ == "__main__":
    unittest.main()
