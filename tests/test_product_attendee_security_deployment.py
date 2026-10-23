from __future__ import annotations

import importlib.util
import json
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

from dreamland.product.attendee_live import AttendeeLiveProviderSettings
from dreamland.product.attendee_provider import (
    AttendeeProviderLifecycle,
    AttendeeProviderPoolSnapshot,
    UpstreamTerminationEvidence,
)
from dreamland.product.authored_artifacts import (
    authored_revision_digest,
    load_configured_authored_profile,
)
from dreamland.product.execution import (
    ProductExecutionResult,
    ProductOutputChunk,
    ProductOutputStream,
)
from dreamland.state_profiles import StateProfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_security_deployment.py"
SERVICE_BEARER = "private-attendee-deployment-service-bearer"
BFF_AUTHORITY = "bff-authority:private_attendee_deployment_test_0001"


def _load_tool(name: str):
    specification = importlib.util.spec_from_file_location(name, TOOL_PATH)
    if specification is None or specification.loader is None:
        raise RuntimeError("attendee deployment tool is unavailable")
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


TOOL = _load_tool("dreamland_product_attendee_security_deployment_test_tool")


def _snapshot(
    lifecycle: AttendeeProviderLifecycle = AttendeeProviderLifecycle.READY,
    *,
    capacity: int = 2,
) -> AttendeeProviderPoolSnapshot:
    ready = capacity if lifecycle is AttendeeProviderLifecycle.READY else 0
    return AttendeeProviderPoolSnapshot(
        lifecycle=lifecycle,
        worker_capacity=capacity,
        ready_workers=ready,
        active_workers=0,
        available_workers=ready,
        accepted_dispatches=0,
        completed_dispatches=0,
        ambiguous_dispatches=0,
        locally_forced_workers=0,
        startup_failures=0,
        forced_upstream_termination_evidence=(UpstreamTerminationEvidence.UNCONFIRMED),
    )


class _FakeWorkerFactory:
    def __init__(self, **values: object) -> None:
        self.values = values


class _FakePool:
    instances: list[_FakePool] = []
    events: list[str] = []

    def __init__(self, worker_factory: object, *, limits: object) -> None:
        self.worker_factory = worker_factory
        self.limits = limits
        self.lifecycle = AttendeeProviderLifecycle.STARTING
        self.__class__.instances.append(self)

    def start(self) -> AttendeeProviderPoolSnapshot:
        self.__class__.events.append("pool.start")
        self.lifecycle = AttendeeProviderLifecycle.READY
        return _snapshot(self.lifecycle, capacity=self.limits.worker_capacity)

    def snapshot(self) -> AttendeeProviderPoolSnapshot:
        return _snapshot(self.lifecycle, capacity=self.limits.worker_capacity)

    def begin_drain(
        self, *, timeout_ms: int | None = None
    ) -> AttendeeProviderPoolSnapshot:
        del timeout_ms
        self.__class__.events.append("pool.begin_drain")
        if self.lifecycle is not AttendeeProviderLifecycle.CLOSED:
            self.lifecycle = AttendeeProviderLifecycle.DRAINING
        return _snapshot(self.lifecycle, capacity=self.limits.worker_capacity)

    def shutdown(
        self, *, timeout_ms: int | None = None
    ) -> AttendeeProviderPoolSnapshot:
        del timeout_ms
        self.__class__.events.append("pool.shutdown")
        self.lifecycle = AttendeeProviderLifecycle.CLOSED
        return _snapshot(self.lifecycle, capacity=self.limits.worker_capacity)

    def execute(self, _request: object, _emit: object) -> ProductExecutionResult:
        raise AssertionError("deployment tests must not dispatch a provider")


class _BlockingPool(_FakePool):
    """Provider-free collaborator exposing cutoff cancellation/publication."""

    def __init__(self, worker_factory: object, *, limits: object) -> None:
        super().__init__(worker_factory, limits=limits)
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.returned = threading.Event()
        self.initial_emit_accepted: bool | None = None
        self.late_emit_accepted: bool | None = None

    def execute(self, request: object, emit: object) -> ProductExecutionResult:
        self.initial_emit_accepted = emit(
            ProductOutputChunk(ProductOutputStream.STDOUT, "before-cutoff\n")
        )
        self.started.set()
        while not request.cancelled():
            time.sleep(0.005)
        self.cancelled.set()
        if isinstance(request.provisional_document, dict):
            request.provisional_document["late_cutoff_mutation"] = True
        self.late_emit_accepted = emit(
            ProductOutputChunk(ProductOutputStream.STDOUT, "after-cutoff\n")
        )
        self.returned.set()
        return ProductExecutionResult.interrupted("cancelled")


class ProductAttendeeSecurityDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakePool.instances.clear()
        _FakePool.events.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile_path = self.root / "authored.json"
        _write_profile(self.profile_path)
        profile = load_configured_authored_profile(self.profile_path)
        self.profile_digest = authored_revision_digest(profile)
        self.artifact_root = self.root / "private-artifacts"
        self.credential = self.root / "provider.key"
        self.credential.write_text(
            "deployment-provider-secret-never-print\n",
            encoding="utf-8",
        )
        self.credential.chmod(0o600)
        self.settings = AttendeeLiveProviderSettings(
            model="openai:gpt-attendee-deployment-test",
            system_prompt_name="system.md",
            max_output_tokens=12_800,
        )

    def test_import_is_inert_and_does_not_bind_or_spawn(self) -> None:
        script = r"""
import importlib.util, json, multiprocessing.process, pathlib, socket, sys, threading
from unittest.mock import patch
path = pathlib.Path(sys.argv[1])
before = sorted(thread.name for thread in threading.enumerate())
with patch.object(socket.socket, "bind", side_effect=AssertionError("bound")), \
     patch.object(multiprocessing.process.BaseProcess, "start", side_effect=AssertionError("spawned")):
    spec = importlib.util.spec_from_file_location("deployment_inert_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
after = sorted(thread.name for thread in threading.enumerate())
print(json.dumps({"before": before, "after": after, "revision": module.CONTRACT_REVISION}))
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(TOOL_PATH)],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["before"], result["after"])
        self.assertEqual(result["revision"], "0.8.0-rc.4")

    def test_canonical_utc_parser_is_exact(self) -> None:
        self.assertEqual(
            TOOL._canonical_utc_milliseconds("2026-10-03T15:00:00Z"),
            1_791_039_600_000,
        )
        self.assertEqual(
            TOOL._canonical_utc_milliseconds("2026-10-03T15:00:00.123Z"),
            1_791_039_600_123,
        )
        for invalid in (
            "2026-10-03T15:00Z",
            "2026-10-03T15:00:00+00:00",
            "2026-10-03T15:00:00.000Z",
            "2026-10-03T15:00:00.1234Z",
            "2026-10-03 15:00:00Z",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(TOOL.argparse.ArgumentTypeError),
            ):
                TOOL._canonical_utc_milliseconds(invalid)

    def test_parser_requires_three_exact_availability_instants(self) -> None:
        arguments = self._serve_arguments()
        parsed = TOOL._parser().parse_args(arguments)
        self.assertLess(parsed.availability_opens_at, parsed.availability_closes_at)
        self.assertLess(parsed.availability_closes_at, parsed.recovery_closes_at)
        self.assertEqual(
            parsed.max_output_chunks_per_command,
            TOOL._LIVE.MAX_OUTPUT_CHUNKS_PER_COMMAND,
        )
        self.assertNotIn("sighup", TOOL._parser().format_help().lower())

    def test_custom_output_chunk_limit_flows_through_cli_and_runtime(self) -> None:
        arguments = self._serve_arguments() + [
            "--max-output-chunks-per-command",
            "240",
        ]
        parsed = TOOL._parser().parse_args(arguments)
        self.assertEqual(parsed.max_output_chunks_per_command, 240)
        for invalid in ("0", str(TOOL._LIVE.MAX_OUTPUT_CHUNKS_PER_COMMAND + 1)):
            with (
                self.subTest(invalid=invalid),
                redirect_stderr(StringIO()),
                self.assertRaises(SystemExit),
            ):
                TOOL._parser().parse_args(
                    self._serve_arguments()
                    + ["--max-output-chunks-per-command", invalid]
                )

        with (
            patch.object(
                TOOL._LIFECYCLE,
                "load_preview_bearer_file",
                return_value=SERVICE_BEARER,
            ),
            patch.object(
                TOOL._SECURITY,
                "load_bootstrap",
                return_value=BFF_AUTHORITY,
            ),
            patch.object(TOOL, "serve_deployment", return_value=0) as serve,
        ):
            self.assertEqual(TOOL.main(arguments), 0)
        self.assertEqual(
            serve.call_args.kwargs["max_output_chunks_per_command"],
            240,
        )

        runtime, _window = self._runtime_with_window(
            close_after_ms=1_500,
            recovery_after_ms=2_500,
            max_output_chunks=240,
        )
        harness = TOOL.AttendeeSecurityDeploymentHarness(
            bff_authority=BFF_AUTHORITY,
            scenario=TOOL.SCENARIOS[0],
            runtime=runtime,
        )
        server = SimpleNamespace(
            server_address=("127.0.0.1", 40123),
            preview=harness,
        )
        try:
            self.assertEqual(runtime.pool_limits.max_output_chunks, 240)
            self.assertEqual(
                runtime.composition.max_output_chunks_per_command,
                240,
            )
            self.assertEqual(
                TOOL.startup_announcement(server)["output_publication"][
                    "max_chunks_per_command"
                ],
                240,
            )
        finally:
            harness.close()

    def test_capacity_envelope_flows_through_cli_runtime_and_announcement(
        self,
    ) -> None:
        arguments = self._serve_arguments(
            context_capacity="20",
            active_session_capacity="20",
            connection_capacity="32",
            worker_capacity="5",
        )
        parsed = TOOL._parser().parse_args(arguments)
        self.assertEqual(parsed.context_capacity, 20)
        self.assertEqual(parsed.active_session_capacity, 20)
        self.assertEqual(parsed.connection_capacity, 32)
        self.assertEqual(parsed.worker_capacity, 5)

        for option, invalid in (
            (
                "--context-capacity",
                str(TOOL._LIVE.MAXIMUM_CONTEXT_CAPACITY + 1),
            ),
            (
                "--active-session-capacity",
                str(TOOL._LIVE.MAXIMUM_ACTIVE_SESSION_CAPACITY + 1),
            ),
            (
                "--connection-capacity",
                str(TOOL._LIVE.MAXIMUM_CONNECTION_CAPACITY + 1),
            ),
            (
                "--worker-capacity",
                str(TOOL._LIVE.MAXIMUM_WORKER_CAPACITY + 1),
            ),
        ):
            with (
                self.subTest(option=option),
                redirect_stderr(StringIO()),
                self.assertRaises(SystemExit),
            ):
                TOOL._parser().parse_args(self._serve_arguments() + [option, invalid])

        with (
            patch.object(
                TOOL._LIFECYCLE,
                "load_preview_bearer_file",
                return_value=SERVICE_BEARER,
            ),
            patch.object(
                TOOL._SECURITY,
                "load_bootstrap",
                return_value=BFF_AUTHORITY,
            ),
            patch.object(TOOL, "serve_deployment", return_value=0) as serve,
        ):
            self.assertEqual(TOOL.main(arguments), 0)
        self.assertEqual(serve.call_args.kwargs["context_capacity"], 20)
        self.assertEqual(serve.call_args.kwargs["active_session_capacity"], 20)
        self.assertEqual(serve.call_args.kwargs["connection_capacity"], 32)
        self.assertEqual(serve.call_args.kwargs["worker_capacity"], 5)

        server = self._build_server(
            availability_window=self._window(
                close_after_ms=5_000,
                recovery_after_ms=7_000,
            ),
            context_capacity=20,
            active_session_capacity=20,
            connection_capacity=32,
            worker_capacity=5,
        )
        try:
            announcement = TOOL.startup_announcement(server)
            self.assertEqual(announcement["resources"]["context_capacity"], 20)
            self.assertEqual(announcement["resources"]["active_sessions"], 20)
            self.assertEqual(announcement["resources"]["physical_workers"], 5)
            self.assertEqual(announcement["resources"]["logical_jobs"], 20 * 64)
            self.assertEqual(announcement["security"]["connection_limit"], 32)
            self.assertEqual(server.connection_capacity, 32)
            self.assertEqual(server.request_queue_size, 32)
            self.assertEqual(announcement["security"]["logical_global_limit"], 65)
            self.assertEqual(
                announcement["security"]["logical_per_context_limit"],
                8,
            )
            self.assertEqual(server.preview.service._policy.max_contexts, 20)
            self.assertGreater(
                server.preview.service._policy.max_bootstrap_idempotency_records,
                20,
            )
            self.assertGreaterEqual(
                server.preview.service._policy.max_revoke_idempotency_records,
                20,
            )
        finally:
            server.server_close()

    def test_contract_validation_requires_rc4_routes_and_disabled_capabilities(
        self,
    ) -> None:
        contract = self.root / "contract"
        contract.mkdir()
        manifest = {
            "capabilities": {
                name: {"enabled": False, "enabled_revision": None}
                for name in (
                    "forge_to_stage_handoff",
                    "presenter_stage_terminal",
                    "product_session_core",
                    "public_attendee_sessions",
                    "stage_xray",
                )
            }
        }
        (contract / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        validator = SimpleNamespace(
            validate_contract=lambda _root: {
                "contract_revision": "0.8.0-rc.4",
                "implementation_status": "integration_preview_only",
                "capabilities_enabled": 0,
                "routes": 25,
            }
        )

        class Loader:
            def exec_module(self, _module: object) -> None:
                return None

        specification = SimpleNamespace(loader=Loader(), name="deployment_contract")
        with (
            patch.object(
                TOOL.importlib.util,
                "spec_from_file_location",
                return_value=specification,
            ),
            patch.object(
                TOOL.importlib.util,
                "module_from_spec",
                return_value=validator,
            ),
        ):
            self.assertEqual(TOOL.validate_contract_root(contract), contract.resolve())
            manifest["capabilities"]["public_attendee_sessions"]["enabled"] = True
            (contract / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "must remain disabled"):
                TOOL.validate_contract_root(contract)

    def test_build_is_loopback_only_and_cutoff_preflight_starts_nothing(self) -> None:
        window = self._window(close_after_ms=1_000, recovery_after_ms=2_000)
        with self.assertRaisesRegex(ValueError, "loopback"):
            self._build_server(host="0.0.0.0", availability_window=window)
        self.assertEqual(_FakePool.instances, [])

        with self.assertRaisesRegex(ValueError, "already closed"):
            self._build_server(
                availability_window=window,
                utc_ms=lambda: window.admission_closes_at_ms,
            )
        self.assertEqual(_FakePool.instances, [])

    def test_runtime_window_is_replaced_before_harness_construction(self) -> None:
        window = self._window(close_after_ms=1_500, recovery_after_ms=2_500)
        server = self._build_server(availability_window=window)
        try:
            self.assertEqual(server.preview.resolved_availability_window, window)
            self.assertEqual(
                server.preview.runtime.composition.availability_window, window
            )
            self.assertTrue(
                server.preview.runtime.composition.bootstrap_closes_at_admission
            )
            self.assertEqual(server.preview.scenario, TOOL.SCENARIOS[0])
            self.assertEqual(
                TOOL.PRIVATE_ROUTE_ALLOWLIST, TOOL._LIVE.PRIVATE_ROUTE_ALLOWLIST
            )
            self.assertEqual(len(TOOL.PRIVATE_ROUTE_ALLOWLIST), 16)
            self.assertFalse(server.daemon_threads)
            self.assertTrue(server.block_on_close)
        finally:
            server.server_close()

    def test_no_request_watcher_drains_then_closes_recovery(self) -> None:
        runtime, window = self._runtime_with_window(
            close_after_ms=120,
            recovery_after_ms=300,
        )
        harness = TOOL.AttendeeSecurityDeploymentHarness(
            bff_authority=BFF_AUTHORITY,
            scenario=TOOL.SCENARIOS[0],
            runtime=runtime,
        )
        try:
            self.assertTrue(harness.admission_closed.wait(2))
            self.assertEqual(harness.service.lifecycle_state, "draining")
            self.assertIn("pool.begin_drain", _FakePool.events)
            self.assertTrue(harness.recovery_closed.wait(2))
            self.assertEqual(harness.resolved_availability_window, window)
        finally:
            harness.close()
        self.assertEqual(harness.service.lifecycle_state, "closed")
        self.assertEqual(_FakePool.events.count("pool.shutdown"), 1)

    def test_exact_cutoff_blocks_fresh_bootstrap_before_watcher_drain(self) -> None:
        window = self._window(close_after_ms=10_000, recovery_after_ms=12_000)
        server = self._build_server(availability_window=window)
        harness = server.preview
        service = harness.service
        epoch = service.service_epoch
        context_id = "gateway-context:" + "x" * 32
        key = "deployment-exact-cutoff-bootstrap-0001"
        first = service.bootstrap_context(
            bff_authority=BFF_AUTHORITY,
            expected_epoch=epoch,
            gateway_context_id=context_id,
            idempotency_key=key,
        )
        try:
            # Stop the asynchronous watcher to prove the service policy itself
            # closes fresh bootstrap atomically at the advertised boundary.
            harness._availability_stop.set()
            harness._availability_thread.join()
            harness.clock._wall_anchor_ms += (
                window.admission_closes_at_ms - harness.clock.now_ms()
            )
            self.assertEqual(service.lifecycle_state, "ready")

            replay = service.bootstrap_context(
                bff_authority=BFF_AUTHORITY,
                expected_epoch=epoch,
                gateway_context_id=context_id,
                idempotency_key=key,
            )
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["actor_context"], first["actor_context"])
            with self.assertRaises(TOOL._SECURITY.LaunchSelectionError) as conflict:
                service.bootstrap_context(
                    bff_authority=BFF_AUTHORITY,
                    expected_epoch=epoch,
                    gateway_context_id="gateway-context:" + "y" * 32,
                    idempotency_key=key,
                )
            self.assertEqual(conflict.exception.code, "idempotency_conflict")
            with self.assertRaises(TOOL._SECURITY.LaunchSelectionError) as closed:
                service.bootstrap_context(
                    bff_authority=BFF_AUTHORITY,
                    expected_epoch=epoch,
                    gateway_context_id="gateway-context:" + "y" * 32,
                    idempotency_key="deployment-exact-cutoff-bootstrap-0002",
                )
            self.assertEqual(closed.exception.code, "service_closed")
        finally:
            server.server_close()

    def test_no_request_cutoff_fences_blocked_output_and_state(self) -> None:
        runtime, _window = self._runtime_with_window(
            close_after_ms=2_000,
            recovery_after_ms=3_500,
            provider_pool_factory=_BlockingPool,
        )
        pool = runtime.provider_pool
        harness = TOOL.AttendeeSecurityDeploymentHarness(
            bff_authority=BFF_AUTHORITY,
            scenario=TOOL.SCENARIOS[0],
            runtime=runtime,
        )
        try:
            service = harness.service
            epoch = service.service_epoch
            context = service.bootstrap_context(
                bff_authority=BFF_AUTHORITY,
                expected_epoch=epoch,
                gateway_context_id="gateway-context:" + "w" * 32,
                idempotency_key="deployment-cutoff-bootstrap-0001",
            )
            actor = context["actor_context"]
            option = service.options(
                actor_authority=actor,
                expected_epoch=epoch,
            )["options"][0]
            grant = service.exchange(
                actor_authority=actor,
                expected_epoch=epoch,
                idempotency_key="deployment-cutoff-exchange-0001",
                launch_selection_id=option["launch_selection_id"],
                notice_version_presented=option["notice_version"],
            )
            created = service.create(
                actor_authority=actor,
                expected_epoch=epoch,
                idempotency_key="deployment-cutoff-create-0001",
                launch_grant=grant["launch_grant"],
            )
            snapshot = created["snapshot"]
            session_arguments = {
                "actor_authority": actor,
                "session_authority": created["session_authority"],
                "reference": snapshot["reference"],
                "expected_epoch": epoch,
            }
            service.submit_command(
                **session_arguments,
                idempotency_key="deployment-cutoff-command-0001",
                expected_slot_generation=0,
                command="pwd",
            )
            self.assertTrue(pool.started.wait(2))

            # No request or manual expiry follows. The deployment watcher alone
            # must cancel and fence work when its exact cutoff arrives.
            self.assertTrue(pool.cancelled.wait(3))
            self.assertTrue(pool.returned.wait(2))
            self.assertTrue(pool.initial_emit_accepted)
            self.assertFalse(pool.late_emit_accepted)
            self.assertTrue(harness.admission_closed.wait(2))

            deadline = time.monotonic() + 2
            admission = service._core.admission.snapshot()
            while (
                admission.active_sessions
                or admission.physical_workers
                or admission.unsettled_jobs
            ) and time.monotonic() < deadline:
                time.sleep(0.01)
                admission = service._core.admission.snapshot()
            self.assertEqual(admission.active_sessions, 0)
            self.assertEqual(admission.physical_workers, 0)
            self.assertEqual(admission.unsettled_jobs, 0)

            projected = service.current(
                actor_authority=actor,
                expected_epoch=epoch,
            )
            self.assertEqual(projected["kind"], "current_session_terminal")
            self.assertEqual(
                projected["snapshot"]["coordinates"]["accepted_state_revision"],
                0,
            )
        finally:
            harness.close()

    def test_listener_remains_available_for_recovery_until_recovery_close(self) -> None:
        window = self._window(close_after_ms=250, recovery_after_ms=900)
        server = self._build_server(availability_window=window)
        output = StringIO()
        result: list[int] = []

        def serve() -> None:
            with redirect_stdout(output):
                result.append(
                    TOOL.serve_built_server(
                        server,
                        install_signal_handlers=False,
                    )
                )

        thread = threading.Thread(target=serve)
        thread.start()
        self.assertTrue(server.preview.admission_closed.wait(3))
        status, payload = _request(server, TOOL._CORE.CAPABILITIES_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(payload["contract"]["revision"], "0.8.0-rc.4")
        self.assertTrue(payload["preview"]["provider_calls"])
        thread.join(timeout=4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])
        self.assertEqual(_FakePool.events.count("pool.shutdown"), 1)

    def test_startup_announcement_is_windowed_supervised_and_redacted(self) -> None:
        window = self._window(close_after_ms=1_500, recovery_after_ms=2_500)
        server = self._build_server(availability_window=window)
        try:
            announcement = TOOL.startup_announcement(server)
            encoded = json.dumps(announcement, sort_keys=True)
            self.assertEqual(announcement["contract"]["revision"], "0.8.0-rc.4")
            self.assertEqual(
                announcement["availability"],
                {
                    "opens_at": TOOL._format_utc_milliseconds(
                        window.admission_opens_at_ms
                    ),
                    "admission_closes_at": TOOL._format_utc_milliseconds(
                        window.admission_closes_at_ms
                    ),
                    "recovery_closes_at": TOOL._format_utc_milliseconds(
                        window.recovery_closes_at_ms
                    ),
                    "clock_policy": "startup_utc_anchor_plus_monotonic_elapsed",
                },
            )
            self.assertEqual(
                announcement["supervisor"]["accepted_signals"],
                ["SIGINT", "SIGTERM"],
            )
            self.assertEqual(announcement["supervisor"]["rotation"], "restart_required")
            self.assertFalse(announcement["supervisor"]["hot_reload"])
            self.assertEqual(
                announcement["supervisor"]["shutdown_upper_bound_ms"],
                (
                    server.preview.runtime.pool_limits.shutdown_upper_bound_ms()
                    + int(2 * TOOL._LIFECYCLE.CONNECTION_DEADLINE_SECONDS * 1_000)
                    + int(TOOL.HANDLE_REQUEST_POLL_SECONDS * 1_000)
                ),
            )
            for private in (
                str(self.credential),
                str(self.profile_path),
                str(self.artifact_root),
                self.profile_digest,
                "deployment-provider-secret-never-print",
                SERVICE_BEARER,
                BFF_AUTHORITY,
            ):
                self.assertNotIn(private, encoded)
        finally:
            server.server_close()

    def test_signal_handlers_set_reason_and_close_exactly_once(self) -> None:
        def exercise(signum: int) -> tuple[int, list[str]]:
            handlers: dict[int, object] = {}
            events: list[str] = []

            class FakeServer:
                timeout = None
                preview = SimpleNamespace(recovery_closed=threading.Event())

                def handle_request(self) -> None:
                    handler = handlers[signum]
                    if callable(handler):
                        handler(signum, None)

                def begin_drain(self) -> bool:
                    events.append("drain")
                    return True

                def server_close(self) -> None:
                    events.append("close")

            def register(number: int, handler: object) -> None:
                if callable(handler):
                    handlers[number] = handler

            with (
                patch.object(TOOL.signal, "getsignal", return_value="prior"),
                patch.object(TOOL.signal, "signal", side_effect=register),
                redirect_stdout(StringIO()),
            ):
                status = TOOL.serve_built_server(
                    FakeServer(),
                    announcement_factory=lambda _server: {"ready": True},
                )
            return status, events

        for signum, expected in (
            (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(signal=signum):
                status, events = exercise(signum)
                self.assertEqual(status, expected)
                self.assertEqual(events, ["drain", "close"])

    def test_announcement_failure_still_orders_drain_and_close(self) -> None:
        events: list[str] = []

        class FakeServer:
            preview = SimpleNamespace(recovery_closed=threading.Event())

            def begin_drain(self) -> bool:
                events.append("drain")
                return True

            def server_close(self) -> None:
                events.append("close")

        with self.assertRaisesRegex(ValueError, "announcement failed"):
            TOOL.serve_built_server(
                FakeServer(),
                install_signal_handlers=False,
                announcement_factory=lambda _server: (_ for _ in ()).throw(
                    ValueError("announcement failed")
                ),
            )
        self.assertEqual(events, ["drain", "close"])

    def test_real_subprocess_sigint_and_sigterm_exit_cleanly(self) -> None:
        script = r"""
import importlib.util, pathlib, sys, threading, time
path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("deployment_signal_check", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
class Server:
    timeout = None
    preview = type("Preview", (), {"recovery_closed": threading.Event()})()
    def handle_request(self):
        time.sleep(0.02)
    def begin_drain(self):
        print("drain", file=sys.stderr, flush=True)
        return True
    def server_close(self):
        print("close", file=sys.stderr, flush=True)
server = Server()
status = module.serve_built_server(
    server,
    announcement_factory=lambda _server: {"ready": True},
)
raise SystemExit(status)
"""
        for signum, expected in (
            (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(signal=signum):
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", script, str(TOOL_PATH)],
                    cwd=REPOSITORY_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                assert process.stdout is not None
                ready = process.stdout.readline()
                self.assertEqual(json.loads(ready), {"ready": True})
                process.send_signal(signum)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, expected, (stdout, stderr))
                self.assertEqual(stderr.splitlines(), ["drain", "close"])
                self.assertNotIn("Traceback", stderr)

    def test_server_close_waits_for_admitted_handler_before_pool_shutdown(self) -> None:
        window = self._window(close_after_ms=2_000, recovery_after_ms=3_000)
        server = self._build_server(availability_window=window)
        accept = threading.Thread(target=server.handle_request)
        accept.start()
        client = socket.create_connection(server.server_address, timeout=2)
        client.sendall(b"GET /product/v1/capabilities HTTP/1.1\r\n")
        deadline = time.monotonic() + 2
        while server.active_connections != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(server.active_connections, 1)

        closed = threading.Event()

        def close() -> None:
            server.server_close()
            closed.set()

        closer = threading.Thread(target=close)
        closer.start()
        self.assertFalse(closed.wait(0.1))
        self.assertNotIn("pool.shutdown", _FakePool.events)
        client.close()
        closer.join(timeout=3)
        accept.join(timeout=3)
        self.assertFalse(closer.is_alive())
        self.assertFalse(accept.is_alive())
        self.assertTrue(closed.is_set())
        self.assertEqual(_FakePool.events.count("pool.shutdown"), 1)

    def test_restart_constructs_a_new_epoch_and_never_hot_reloads(self) -> None:
        first = self._build_server(
            availability_window=self._window(
                close_after_ms=1_500,
                recovery_after_ms=2_500,
            )
        )
        first_epoch = first.preview.service.service_epoch
        first.server_close()
        second = self._build_server(
            availability_window=self._window(
                close_after_ms=1_500,
                recovery_after_ms=2_500,
            )
        )
        try:
            self.assertNotEqual(first_epoch, second.preview.service.service_epoch)
            announcement = TOOL.startup_announcement(second)
            self.assertEqual(announcement["supervisor"]["rotation"], "restart_required")
            self.assertFalse(announcement["supervisor"]["automatic_authority_remint"])
        finally:
            second.server_close()

    def _window(
        self,
        *,
        close_after_ms: int,
        recovery_after_ms: int,
    ):
        now = TOOL._utc_ms()
        return TOOL._SECURITY.AttendeeSecurityAvailabilityWindow(
            admission_opens_at_ms=now - 1_000,
            admission_closes_at_ms=now + close_after_ms,
            recovery_closes_at_ms=now + recovery_after_ms,
        )

    def _runtime_with_window(
        self,
        *,
        close_after_ms: int,
        recovery_after_ms: int,
        provider_pool_factory: object = _FakePool,
        max_output_chunks: int = TOOL._LIVE.MAX_OUTPUT_CHUNKS_PER_COMMAND,
    ):
        window = self._window(
            close_after_ms=close_after_ms,
            recovery_after_ms=recovery_after_ms,
        )
        runtime = TOOL._LIVE.start_live_runtime(
            artifact_root=self.artifact_root,
            authored_profile_path=self.profile_path,
            authored_profile_digest=self.profile_digest,
            provider_credential_file=self.credential,
            provider_settings=self.settings,
            max_output_chunks_per_command=max_output_chunks,
            provider_worker_factory=_FakeWorkerFactory,
            provider_pool_factory=provider_pool_factory,
        )
        runtime = replace(
            runtime,
            composition=replace(
                runtime.composition,
                availability_window=window,
                bootstrap_closes_at_admission=True,
            ),
        )
        return runtime, window

    def _build_server(self, **overrides: object):
        values: dict[str, object] = {
            "host": "127.0.0.1",
            "port": 0,
            "bearer": SERVICE_BEARER,
            "bff_authority": BFF_AUTHORITY,
            "scenario": TOOL.SCENARIOS[0],
            "contract_root": self.root,
            "artifact_root": self.artifact_root,
            "authored_profile_path": self.profile_path,
            "authored_profile_digest": self.profile_digest,
            "provider_credential_file": self.credential,
            "provider_settings": self.settings,
            "availability_window": self._window(
                close_after_ms=1_500,
                recovery_after_ms=2_500,
            ),
            "contract_validator": lambda path: path.resolve(),
            "provider_worker_factory": _FakeWorkerFactory,
            "provider_pool_factory": _FakePool,
        }
        values.update(overrides)
        return TOOL.build_deployment_server(**values)

    def _serve_arguments(
        self,
        *,
        context_capacity: str | None = None,
        active_session_capacity: str | None = None,
        connection_capacity: str | None = None,
        worker_capacity: str | None = None,
    ) -> list[str]:
        arguments = [
            "serve",
            "--token-file",
            str(self.root / "token"),
            "--bootstrap-file",
            str(self.root / "bootstrap"),
            "--contract-root",
            str(self.root),
            "--artifact-root",
            str(self.artifact_root),
            "--authored-profile-path",
            str(self.profile_path),
            "--authored-profile-digest",
            self.profile_digest,
            "--provider-credential-file",
            str(self.credential),
            "--model",
            "openai:gpt-attendee-deployment-test",
            "--max-output-tokens",
            "12800",
            "--availability-opens-at",
            "2026-10-03T15:00:00Z",
            "--availability-closes-at",
            "2026-10-03T16:00:00Z",
            "--recovery-closes-at",
            "2026-10-03T16:05:00Z",
        ]
        for option, value in (
            ("--context-capacity", context_capacity),
            ("--active-session-capacity", active_session_capacity),
            ("--connection-capacity", connection_capacity),
            ("--worker-capacity", worker_capacity),
        ):
            if value is not None:
                arguments.extend((option, value))
        return arguments


def _request(server: object, path: str) -> tuple[int, dict[str, object]]:
    request = Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        headers={"Authorization": f"Bearer {SERVICE_BEARER}"},
    )
    with urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read())


def _write_profile(path: Path) -> None:
    profile = StateProfile(
        name="private-attendee-deployment-test",
        level="world",
        created_at="2026-09-02T00:00:00+00:00",
        state={
            "world_state": {
                "name": "private-attendee-deployment-world",
                "entry_host": "console",
                "active_host": "console",
                "hosts_by_name": {
                    "console": {
                        "login_banner": "Test console\n",
                        "hostname": "console",
                        "purpose": {"theme": "Provider-free deployment test"},
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
        },
    )
    path.write_text(json.dumps(profile.to_document()), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
