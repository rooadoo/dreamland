from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dreamland.product.admission import ProductJobCategory
from dreamland.product.attendee_live import AttendeeLiveProviderSettings
from dreamland.product.attendee_provider import (
    AttendeeProviderLifecycle,
    AttendeeProviderPoolSnapshot,
    AttendeeProviderRejectionReason,
    UpstreamTerminationEvidence,
)
from dreamland.product.authored_artifacts import (
    authored_revision_digest,
    load_configured_authored_profile,
)
from dreamland.product.execution import ProductExecutionResult
from dreamland.product.launch_selection import LaunchSelectionError
from dreamland.state_profiles import StateProfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_security_live.py"
SERVICE_BEARER = "private-attendee-security-live-service-bearer"
BFF_AUTHORITY = "bff-authority:private_attendee_security_live_test_0001"


def _load_tool(name: str):
    specification = importlib.util.spec_from_file_location(name, TOOL_PATH)
    if specification is None or specification.loader is None:
        raise RuntimeError("attendee security live tool is unavailable")
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


TOOL = _load_tool("dreamland_product_attendee_security_live_test_tool")


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
    def __init__(self, **values) -> None:
        self.values = values


class _FakePool:
    instances: list[_FakePool] = []
    events: list[str] = []
    fail_start = False

    def __init__(self, worker_factory, *, limits) -> None:
        self.worker_factory = worker_factory
        self.limits = limits
        self.closed = False
        self.__class__.instances.append(self)

    def start(self):
        self.__class__.events.append("pool.start")
        if self.__class__.fail_start:
            raise ValueError("safe startup failure")
        return _snapshot(capacity=self.limits.worker_capacity)

    def snapshot(self):
        lifecycle = (
            AttendeeProviderLifecycle.CLOSED
            if self.closed
            else AttendeeProviderLifecycle.READY
        )
        return _snapshot(lifecycle, capacity=self.limits.worker_capacity)

    def begin_drain(self, *, timeout_ms=None):
        self.__class__.events.append("pool.begin_drain")
        return _snapshot(
            AttendeeProviderLifecycle.DRAINING,
            capacity=self.limits.worker_capacity,
        )

    def shutdown(self, *, timeout_ms=None):
        self.__class__.events.append("pool.shutdown")
        self.closed = True
        return _snapshot(
            AttendeeProviderLifecycle.CLOSED,
            capacity=self.limits.worker_capacity,
        )

    def execute(self, _request, _emit) -> ProductExecutionResult:
        raise AssertionError("provider-free tool tests must not dispatch")


class ProductAttendeeSecurityLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakePool.instances.clear()
        _FakePool.events.clear()
        _FakePool.fail_start = False
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile_path = self.root / "authored.json"
        _write_profile(self.profile_path)
        profile = load_configured_authored_profile(self.profile_path)
        self.profile_digest = authored_revision_digest(profile)
        self.artifact_root = self.root / "private-artifacts"
        self.credential = self.root / "provider.key"
        self.credential.write_text("provider-secret-never-print\n", encoding="utf-8")
        self.credential.chmod(0o600)
        self.settings = AttendeeLiveProviderSettings(
            model="openai:gpt-attendee-test",
            system_prompt_name="system.md",
            max_output_tokens=12_800,
        )

    def test_import_is_inert_and_does_not_activate_ordinary_runtime(self) -> None:
        script = r"""
import importlib.util, json, pathlib, sys, tempfile, threading
from unittest.mock import patch
import dreamland.model_providers as providers
path = pathlib.Path(sys.argv[1])
before_threads = [(thread.ident, thread.name) for thread in threading.enumerate()]
with tempfile.TemporaryDirectory() as directory:
    before_files = list(pathlib.Path(directory).iterdir())
    with patch.object(providers, "build_chat_model") as build:
        spec = importlib.util.spec_from_file_location("attendee_security_live_inert", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    after_files = list(pathlib.Path(directory).iterdir())
after_threads = [(thread.ident, thread.name) for thread in threading.enumerate()]
print(json.dumps({
    "provider_calls": build.call_count,
    "threads_unchanged": before_threads == after_threads,
    "files_unchanged": before_files == after_files,
    "provider_clients": [name for name in (
        "langchain_openai", "langchain_anthropic", "langchain_xai"
    ) if name in sys.modules],
    "revision": module.CONTRACT_REVISION,
    "routes": len(module.PRIVATE_ROUTE_ALLOWLIST),
}))
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(TOOL_PATH)],
            check=True,
            capture_output=True,
            cwd=REPOSITORY_ROOT,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["provider_calls"], 0)
        self.assertTrue(payload["threads_unchanged"])
        self.assertTrue(payload["files_unchanged"])
        self.assertEqual(payload["provider_clients"], [])
        self.assertEqual(payload["revision"], "0.8.0-rc.3")
        self.assertEqual(payload["routes"], 16)

    def test_runtime_limits_align_pool_core_and_conservative_budgets(self) -> None:
        runtime = self._runtime(
            context_capacity=20,
            active_session_capacity=20,
            connection_capacity=32,
            worker_capacity=5,
            max_commands=77,
        )
        pool = _FakePool.instances[-1]
        composition = runtime.composition

        self.assertEqual(runtime.pool_limits.worker_capacity, 5)
        self.assertEqual(composition.context_capacity, 20)
        self.assertEqual(composition.active_session_capacity, 20)
        self.assertEqual(composition.connection_capacity, 32)
        self.assertEqual(composition.physical_worker_capacity, 5)
        self.assertEqual(composition.max_fresh_operations_per_context, 8)
        self.assertEqual(composition.max_fresh_operations_global, 65)
        self.assertEqual(runtime.ready_snapshot.ready_workers, 5)
        self.assertGreaterEqual(
            runtime.pool_limits.dispatch_timeout_ms,
            runtime.pool_limits.startup_timeout_ms
            + 2 * runtime.pool_limits.terminate_grace_ms,
        )
        self.assertEqual(runtime.pool_limits.max_command_bytes, 4_096)
        self.assertEqual(runtime.pool_limits.max_state_bytes, 4 * 1_024 * 1_024)
        self.assertGreater(
            runtime.pool_limits.max_ipc_frame_bytes,
            runtime.pool_limits.max_state_bytes,
        )
        self.assertEqual(runtime.pool_limits.max_output_chunk_bytes, 4_096)
        self.assertEqual(runtime.pool_limits.max_output_bytes, 65_536)
        self.assertEqual(runtime.pool_limits.max_output_chunks, 65_536)
        self.assertEqual(composition.max_commands_per_session, 77)
        self.assertEqual(
            composition.logical_job_capacity,
            composition.active_session_capacity * 77,
        )
        self.assertEqual(composition.command_deadline_ms, 120_000)
        self.assertEqual(composition.cancellation_deadline_ms, 2_000)
        self.assertEqual(composition.max_output_bytes_per_command, 65_536)
        self.assertEqual(composition.max_output_chunks_per_command, 65_536)
        self.assertEqual(composition.reserved_tokens_per_command, 12_800)
        self.assertEqual(
            composition.token_budget,
            12_800 * composition.logical_job_capacity,
        )
        self.assertEqual(
            composition.spend_budget_units,
            1_000 * composition.logical_job_capacity,
        )
        self.assertIs(composition.executor, pool)
        self.assertEqual(
            composition.max_runtime_document_bytes,
            runtime.pool_limits.max_state_bytes,
        )
        self.assertEqual(
            composition.notice_version,
            TOOL.LIVE_ATTENDEE_NOTICE_VERSION,
        )
        self.assertEqual(composition.policy_id, TOOL.LIVE_ATTENDEE_POLICY_ID)
        harness = TOOL.AttendeeSecurityLiveHarness(
            bff_authority=BFF_AUTHORITY,
            scenario=TOOL.SCENARIOS[0],
            runtime=runtime,
        )
        server = SimpleNamespace(
            server_address=("127.0.0.1", 40123),
            preview=harness,
        )
        try:
            announcement = TOOL.startup_announcement(server)
            self.assertEqual(
                announcement["resources"],
                {
                    **announcement["resources"],
                    "active_sessions": 20,
                    "context_capacity": 20,
                    "logical_jobs": 20 * 77,
                    "physical_workers": 5,
                },
            )
            self.assertEqual(announcement["security"]["connection_limit"], 32)
            self.assertEqual(
                announcement["security"]["logical_per_context_limit"],
                8,
            )
            self.assertEqual(announcement["security"]["logical_global_limit"], 65)
            self.assertEqual(announcement["security"]["logical_tracked_contexts"], 20)
            self.assertEqual(
                announcement["resources"]["hard_maximums"],
                {
                    "active_sessions": 64,
                    "connections": 32,
                    "contexts": 64,
                    "physical_workers": 8,
                },
            )
            self.assertEqual(
                announcement["resources"]["retention"]["lifecycle_idempotency_records"],
                2 * (20 * 77) + 4 * 20,
            )
            self.assertEqual(
                announcement["resources"]["retention"]["launch_grants"],
                40,
            )
            self.assertEqual(len(harness.service._registry._actors), 20)
            self.assertGreaterEqual(
                harness.service._registry._policy.max_active_launch_grants,
                20,
            )
            self.assertGreaterEqual(harness.service._core._policy.max_launch_grants, 20)
            self.assertGreater(
                harness.service._policy.max_bootstrap_idempotency_records,
                20,
            )
            self.assertGreaterEqual(
                harness.service._policy.max_revoke_idempotency_records,
                20,
            )
        finally:
            harness.close()

    def test_custom_output_chunk_limit_aligns_pool_core_and_announcement(
        self,
    ) -> None:
        runtime = self._runtime(max_output_chunks=240)
        harness = TOOL.AttendeeSecurityLiveHarness(
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

    def test_programmatic_output_chunk_limit_rejects_invalid_values(self) -> None:
        for invalid in (0, TOOL.MAX_OUTPUT_CHUNKS_PER_COMMAND + 1, True):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(
                    ValueError,
                    "max output chunks per command must be between",
                ),
            ):
                self._runtime(max_output_chunks=invalid)
        self.assertEqual(_FakePool.instances, [])

    def test_live_privacy_identity_survives_option_selection_and_create(
        self,
    ) -> None:
        runtime = self._runtime()
        harness = TOOL.AttendeeSecurityLiveHarness(
            bff_authority=BFF_AUTHORITY,
            scenario=TOOL.SCENARIOS[0],
            runtime=runtime,
        )
        try:
            epoch = harness.service.service_epoch
            context = harness.service.bootstrap_context(
                bff_authority=BFF_AUTHORITY,
                expected_epoch=epoch,
                gateway_context_id="gateway-context:live_privacy_identity_0001",
                idempotency_key="attendee-live-privacy:bootstrap",
            )
            actor = context["actor_context"]
            options = harness.service.options(
                actor_authority=actor,
                expected_epoch=epoch,
            )
            option = options["options"][0]
            self.assertEqual(
                option["notice_version"],
                TOOL.LIVE_ATTENDEE_NOTICE_VERSION,
            )
            self.assertNotEqual(
                option["notice_version"],
                TOOL._SECURITY.ATTENDEE_NOTICE_VERSION,
            )

            with self.assertRaises(LaunchSelectionError) as rejected:
                harness.service.exchange(
                    actor_authority=actor,
                    expected_epoch=epoch,
                    launch_selection_id=option["launch_selection_id"],
                    notice_version_presented=(TOOL._SECURITY.ATTENDEE_NOTICE_VERSION),
                    idempotency_key="attendee-live-privacy:old-notice",
                )
            self.assertEqual(rejected.exception.code, "notice_version_mismatch")

            granted = harness.service.exchange(
                actor_authority=actor,
                expected_epoch=epoch,
                launch_selection_id=option["launch_selection_id"],
                notice_version_presented=option["notice_version"],
                idempotency_key="attendee-live-privacy:exchange",
            )
            created = harness.service.create(
                actor_authority=actor,
                expected_epoch=epoch,
                launch_grant=granted["launch_grant"],
                idempotency_key="attendee-live-privacy:create",
            )
            binding = created["snapshot"]["launch_binding"]
            self.assertEqual(
                binding["notice_version"],
                TOOL.LIVE_ATTENDEE_NOTICE_VERSION,
            )
            self.assertEqual(binding["policy_id"], TOOL.LIVE_ATTENDEE_POLICY_ID)
        finally:
            harness.close()

    def test_admission_settles_more_than_eight_commands_without_hidden_quota(
        self,
    ) -> None:
        runtime = self._runtime()
        harness = TOOL.AttendeeSecurityLiveHarness(
            bff_authority=BFF_AUTHORITY,
            scenario=TOOL.SCENARIOS[0],
            runtime=runtime,
        )
        try:
            admission = harness.service._core.admission
            admission.admit_session("session:live-admission-regression")
            for index in range(13):
                job_id = f"command-job:live-admission-{index:02d}"
                reservation, claim = admission.reserve_and_claim_worker(
                    logical_job_id=job_id,
                    session_id="session:live-admission-regression",
                    category=ProductJobCategory.FOREGROUND_PRODUCT,
                    reserved_tokens=runtime.composition.reserved_tokens_per_command,
                    reserved_spend_units=(
                        runtime.composition.reserved_spend_units_per_command
                    ),
                )
                self.assertFalse(reservation.replayed)
                self.assertFalse(claim.replayed)
                settlement, reap = admission.settle_and_reap(
                    job_id,
                    actual_tokens=0,
                    actual_spend_units=0,
                )
                self.assertFalse(settlement.replayed)
                self.assertTrue(reap.reaped)

            snapshot = admission.snapshot()
            self.assertEqual(snapshot.logical_jobs, 13)
            self.assertEqual(snapshot.settled_jobs, 13)
            self.assertEqual(snapshot.physical_workers, 0)
            self.assertEqual(admission.limits.max_logical_jobs, 4 * 64)
        finally:
            harness.close()

    def test_runtime_state_policy_preflights_initial_and_committed_documents(
        self,
    ) -> None:
        runtime = self._runtime()
        harness = TOOL.AttendeeSecurityLiveHarness(
            bff_authority=BFF_AUTHORITY,
            scenario=TOOL.SCENARIOS[0],
            runtime=runtime,
        )
        try:
            validator = runtime.composition.validate_runtime_document
            self.assertIsNotNone(validator)
            validator(runtime.composition.revision_document)
            harness.service._core._validate_state_effect(
                runtime.composition.revision_document
            )
            with self.assertRaisesRegex(ValueError, "complexity limit"):
                harness.service._core._validate_state_effect(_deep_document())
        finally:
            harness.close()

        _write_profile(self.profile_path, nested_purpose_depth=40)
        self.profile_digest = authored_revision_digest(
            load_configured_authored_profile(self.profile_path)
        )
        _FakePool.instances.clear()
        _FakePool.events.clear()
        with self.assertRaisesRegex(ValueError, "runtime boundary"):
            self._runtime()
        self.assertEqual(_FakePool.events, [])

    def test_runtime_refuses_a_pool_with_a_different_state_boundary(self) -> None:
        class MismatchedPool(_FakePool):
            def __init__(self, worker_factory, *, limits) -> None:
                super().__init__(worker_factory, limits=limits)
                self.limits = replace(
                    limits,
                    max_state_bytes=limits.max_state_bytes // 2,
                )

        with self.assertRaisesRegex(RuntimeError, "runtime-state limits must match"):
            TOOL.start_live_runtime(
                artifact_root=self.artifact_root,
                authored_profile_path=self.profile_path,
                authored_profile_digest=self.profile_digest,
                provider_credential_file=self.credential,
                provider_settings=self.settings,
                provider_worker_factory=_FakeWorkerFactory,
                provider_pool_factory=MismatchedPool,
            )
        self.assertNotIn("pool.start", MismatchedPool.events)

    def test_pins_profile_into_owner_private_store_before_pool_start(self) -> None:
        self._runtime()
        store = self.artifact_root / "authored-revisions"
        artifacts = list(store.iterdir())
        self.assertEqual(stat.S_IMODE(self.artifact_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o700)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(stat.S_IMODE(artifacts[0].stat().st_mode), 0o600)
        factory = _FakePool.instances[-1].worker_factory
        self.assertEqual(factory.values["settings"], self.settings)
        self.assertEqual(factory.values["credential_file"], self.credential)
        self.assertEqual(
            factory.values["authored_revision"].digest,
            self.profile_digest,
        )
        self.assertEqual(_FakePool.events[0], "pool.start")

    def test_listener_is_not_constructed_until_entire_pool_is_ready(self) -> None:
        events: list[str] = []

        class OrderedPool(_FakePool):
            def start(self):
                events.append("all-workers-ready")
                return super().start()

        sentinel = SimpleNamespace()

        def server_factory(*_args, **_kwargs):
            events.append("listener-bind")
            return sentinel

        server = self._build_server(
            provider_pool_factory=OrderedPool,
            server_factory=server_factory,
        )
        self.assertIs(server, sentinel)
        self.assertEqual(events, ["all-workers-ready", "listener-bind"])
        OrderedPool.instances[-1].shutdown()

    def test_startup_failure_rolls_back_without_constructing_listener(self) -> None:
        _FakePool.fail_start = True
        bound = False

        def server_factory(*_args, **_kwargs):
            nonlocal bound
            bound = True
            raise AssertionError("listener must not be constructed")

        with self.assertRaisesRegex(ValueError, "safe startup failure"):
            self._build_server(server_factory=server_factory)
        self.assertFalse(bound)
        self.assertEqual(_FakePool.events, ["pool.start", "pool.shutdown"])
        self.assertTrue(_FakePool.instances[-1].closed)

    def test_announcement_failure_after_bind_always_closes_server(self) -> None:
        events: list[str] = []

        class Server:
            def server_close(self):
                events.append("server.close")

        with (
            patch.object(TOOL, "build_live_server", return_value=Server()),
            patch.object(
                TOOL,
                "startup_announcement",
                side_effect=ValueError("safe announcement failure"),
            ),
            self.assertRaisesRegex(ValueError, "safe announcement failure"),
        ):
            TOOL.serve_live(
                host="127.0.0.1",
                port=0,
                bearer=SERVICE_BEARER,
                bff_authority=BFF_AUTHORITY,
                scenario=TOOL.SCENARIOS[0],
                contract_root=self.root,
                artifact_root=self.artifact_root,
                authored_profile_path=self.profile_path,
                authored_profile_digest=self.profile_digest,
                provider_credential_file=self.credential,
                provider_settings=self.settings,
                worker_capacity=2,
                max_commands_per_session=64,
                once=False,
            )
        self.assertEqual(events, ["server.close"])

    def test_live_transport_identity_capabilities_and_route_allowlist(self) -> None:
        runtime = self._runtime()
        TOOL._configure_live_transport()
        harness = TOOL.AttendeeSecurityLiveHarness(
            bff_authority=BFF_AUTHORITY,
            scenario=TOOL.SCENARIOS[0],
            runtime=runtime,
        )
        try:
            handler = object.__new__(TOOL.AttendeeSecurityLiveHandler)
            handler.server = SimpleNamespace(preview=harness)
            payload = handler._capabilities("request:attendee-security-live-00000001")
            self.assertEqual(
                payload["contract"],
                {
                    "name": "dreamland-private-product-api",
                    "revision": "0.8.0-rc.3",
                    "compatibility": "unfrozen-release-candidate",
                },
            )
            self.assertEqual(payload["preview"]["executor"], TOOL.LIVE_EXECUTOR)
            self.assertTrue(payload["preview"]["provider_calls"])
            self.assertTrue(payload["preview"]["test_only"])
            self.assertFalse(payload["preview"]["live_capability_evidence"])
            self.assertTrue(
                all(
                    value["enabled"] is False
                    for value in payload["capabilities"].values()
                )
            )

            self.assertEqual(
                TOOL.PRIVATE_ROUTE_ALLOWLIST,
                TOOL._SECURITY.PRIVATE_ROUTE_ALLOWLIST,
            )
            self.assertEqual(len(TOOL.PRIVATE_ROUTE_ALLOWLIST), 16)
            handler.path = "/product/v1/stage/sessions"
            self.assertEqual(handler._route()[0], "not-found")
            handler.path = "/ready"
            self.assertEqual(handler._route()[0], "not-found")
            self.assertTrue(
                issubclass(
                    TOOL.AttendeeSecurityLiveHandler,
                    TOOL._SECURITY.AttendeeSecurityPreviewHandler,
                )
            )
        finally:
            harness.close()

    def test_provider_free_loopback_server_smoke_and_clean_close(self) -> None:
        server = self._build_server()
        pool = _FakePool.instances[-1]
        self.assertNotIn("request_queue_size", server.__dict__)
        self.assertEqual(
            server.request_queue_size,
            TOOL._LIFECYCLE.LifecyclePreviewServer.request_queue_size,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            status, payload = _request(
                server,
                "GET",
                TOOL._CORE.CAPABILITIES_PATH,
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["contract"]["revision"], "0.8.0-rc.3")
            self.assertEqual(payload["preview"]["executor"], TOOL.LIVE_EXECUTOR)
            self.assertTrue(payload["preview"]["provider_calls"])
            self.assertTrue(
                all(
                    capability["enabled"] is False
                    for capability in payload["capabilities"].values()
                )
            )
            for hidden in ("/product/v1/stage/sessions", "/ready"):
                status, problem = _request(server, "GET", hidden)
                self.assertEqual(status, 404)
                self.assertEqual(problem["error"]["code"], "not_found")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(pool.closed)

    def test_close_order_fences_service_then_pool_then_task_threads(self) -> None:
        runtime = self._runtime()
        harness = TOOL.AttendeeSecurityLiveHarness(
            bff_authority=BFF_AUTHORITY,
            scenario=TOOL.SCENARIOS[0],
            runtime=runtime,
        )
        events: list[str] = []
        original_threads = harness._workers
        original_threads.shutdown(wait=True, cancel_futures=True)

        # Bind the test case explicitly; nested helpers do not inherit it.
        test_case = self

        class Threads:
            def shutdown(self, *, wait, cancel_futures):
                test_case.assertEqual((wait, cancel_futures), (True, True))
                events.append("threads.shutdown")

        harness._workers = Threads()
        service_begin = harness.service.begin_drain
        service_shutdown = harness.service.shutdown
        pool_begin = runtime.provider_pool.begin_drain
        pool_shutdown = runtime.provider_pool.shutdown

        def record_service_begin():
            events.append("service.begin_drain")
            return service_begin()

        def record_service_shutdown():
            events.append("service.shutdown")
            return service_shutdown()

        def record_pool_begin(*, timeout_ms=None):
            events.append("pool.begin_drain")
            return pool_begin(timeout_ms=timeout_ms)

        def record_pool_shutdown(*, timeout_ms=None):
            events.append("pool.shutdown")
            return pool_shutdown(timeout_ms=timeout_ms)

        harness.service.begin_drain = record_service_begin
        harness.service.shutdown = record_service_shutdown
        runtime.provider_pool.begin_drain = record_pool_begin
        runtime.provider_pool.shutdown = record_pool_shutdown
        harness.close()
        harness.close()
        self.assertEqual(
            events,
            [
                "service.begin_drain",
                "pool.begin_drain",
                "service.shutdown",
                "pool.shutdown",
                "threads.shutdown",
            ],
        )

    def test_explicit_drain_preserves_recovery_until_shutdown(self) -> None:
        runtime = self._runtime()
        harness = TOOL.AttendeeSecurityLiveHarness(
            bff_authority=BFF_AUTHORITY,
            scenario=TOOL.SCENARIOS[0],
            runtime=runtime,
        )
        try:
            self.assertEqual(harness.service.lifecycle_state, "ready")
            self.assertTrue(harness.begin_drain())
            self.assertEqual(harness.service.lifecycle_state, "draining")
            self.assertFalse(harness.begin_drain())
            self.assertFalse(runtime.provider_pool.closed)
        finally:
            harness.close()
        self.assertEqual(harness.service.lifecycle_state, "closed")
        self.assertTrue(runtime.provider_pool.closed)

    def test_startup_announcement_is_ready_bounded_and_secret_free(self) -> None:
        runtime = self._runtime()
        harness = TOOL.AttendeeSecurityLiveHarness(
            bff_authority=BFF_AUTHORITY,
            scenario=TOOL.SCENARIOS[0],
            runtime=runtime,
        )
        server = SimpleNamespace(
            server_address=("127.0.0.1", 40123),
            preview=harness,
        )
        try:
            announcement = TOOL.startup_announcement(server)
            encoded = json.dumps(announcement, sort_keys=True)
            self.assertEqual(announcement["capabilities_enabled"], 0)
            self.assertEqual(announcement["route_count"], 16)
            self.assertTrue(announcement["provider_calls"])
            self.assertEqual(
                announcement["privacy"],
                {
                    "notice_version": TOOL.LIVE_ATTENDEE_NOTICE_VERSION,
                    "policy_id": TOOL.LIVE_ATTENDEE_POLICY_ID,
                },
            )
            self.assertEqual(announcement["provider"]["lifecycle"], "ready")
            self.assertEqual(announcement["provider"]["ready_workers"], 2)
            self.assertEqual(
                announcement["provider"]["model"],
                "openai:gpt-attendee-test",
            )
            self.assertEqual(announcement["provider"]["max_output_tokens"], 12_800)
            self.assertEqual(
                announcement["provider"]["upstream_termination_evidence"],
                "unconfirmed",
            )
            self.assertEqual(
                announcement["provider"]["readiness_scope"],
                "all_local_worker_processes_and_provider_clients_constructed",
            )
            self.assertEqual(
                announcement["output_publication"]["max_chunks_per_command"],
                TOOL.MAX_OUTPUT_CHUNKS_PER_COMMAND,
            )
            self.assertFalse(announcement["provider"]["upstream_reachability_checked"])
            self.assertNotIn("upstream_ready", encoded)
            self.assertEqual(
                announcement["resources"]["effective_command_limit"],
                64,
            )
            self.assertEqual(announcement["resources"]["context_capacity"], 4)
            self.assertEqual(announcement["resources"]["active_sessions"], 4)
            self.assertEqual(announcement["resources"]["logical_jobs"], 4 * 64)
            self.assertEqual(announcement["resources"]["physical_workers"], 2)
            self.assertEqual(announcement["security"]["connection_limit"], 8)
            self.assertEqual(announcement["security"]["logical_global_limit"], 16)
            self.assertEqual(announcement["security"]["logical_tracked_contexts"], 4)
            self.assertEqual(
                announcement["resources"]["retention"]["lifecycle_idempotency_records"],
                528,
            )
            self.assertEqual(
                announcement["security"]["logical_per_context_limit"],
                8,
            )
            self.assertGreater(
                announcement["resources"]["shutdown_upper_bound_ms"],
                announcement["resources"]["command_deadline_ms"] * 0,
            )
            for private in (
                str(self.credential),
                str(self.profile_path),
                str(self.artifact_root),
                self.profile_digest,
                "provider-secret-never-print",
                SERVICE_BEARER,
                BFF_AUTHORITY,
            ):
                self.assertNotIn(private, encoded)
        finally:
            harness.close()

    def test_cli_has_explicit_model_output_and_rejects_invalid_limits(self) -> None:
        parser = TOOL._parser()
        valid = parser.parse_args(self._serve_arguments())
        self.assertEqual(valid.command, "serve")
        self.assertEqual(valid.model, "openai:gpt-attendee-test")
        self.assertEqual(valid.max_output_tokens, 12_800)
        self.assertEqual(valid.context_capacity, 4)
        self.assertEqual(valid.active_session_capacity, 4)
        self.assertEqual(valid.connection_capacity, 8)
        self.assertEqual(valid.worker_capacity, 2)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(self._serve_arguments(max_output_tokens="0"))
        for option, invalid in (
            (
                "--context-capacity",
                str(TOOL.MAXIMUM_CONTEXT_CAPACITY + 1),
            ),
            (
                "--active-session-capacity",
                str(TOOL.MAXIMUM_ACTIVE_SESSION_CAPACITY + 1),
            ),
            (
                "--connection-capacity",
                str(TOOL.MAXIMUM_CONNECTION_CAPACITY + 1),
            ),
            ("--worker-capacity", str(TOOL.MAXIMUM_WORKER_CAPACITY + 1)),
        ):
            with (
                self.subTest(option=option),
                redirect_stderr(StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args(self._serve_arguments() + [option, invalid])

        _FakePool.instances.clear()
        for capacities in (
            {
                "context_capacity": 4,
                "active_session_capacity": 5,
                "worker_capacity": 2,
            },
            {
                "context_capacity": 5,
                "active_session_capacity": 4,
                "worker_capacity": 5,
            },
        ):
            with self.subTest(capacities=capacities), self.assertRaises(ValueError):
                self._runtime(**capacities)
        self.assertEqual(_FakePool.instances, [])

        envelope = parser.parse_args(
            self._serve_arguments(
                context_capacity="20",
                active_session_capacity="20",
                connection_capacity="32",
                worker_capacity="5",
            )
        )
        self.assertEqual(envelope.context_capacity, 20)
        self.assertEqual(envelope.active_session_capacity, 20)
        self.assertEqual(envelope.connection_capacity, 32)
        self.assertEqual(envelope.worker_capacity, 5)

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
            patch.object(TOOL, "serve_live") as serve,
        ):
            status = TOOL.main(
                self._serve_arguments(
                    context_capacity="20",
                    active_session_capacity="20",
                    connection_capacity="32",
                    worker_capacity="5",
                )
            )
        self.assertEqual(status, 0)
        self.assertEqual(serve.call_args.kwargs["context_capacity"], 20)
        self.assertEqual(serve.call_args.kwargs["active_session_capacity"], 20)
        self.assertEqual(serve.call_args.kwargs["connection_capacity"], 32)
        self.assertEqual(serve.call_args.kwargs["worker_capacity"], 5)

        stderr = StringIO()
        with (
            patch.object(
                TOOL._LIFECYCLE, "load_preview_bearer_file", return_value=SERVICE_BEARER
            ),
            patch.object(TOOL._SECURITY, "load_bootstrap", return_value=BFF_AUTHORITY),
            patch.object(TOOL, "serve_live") as serve,
            redirect_stderr(stderr),
        ):
            status = TOOL.main(self._serve_arguments(model="not-canonical"))
        self.assertEqual(status, 2)
        serve.assert_not_called()
        self.assertNotIn("provider-secret-never-print", stderr.getvalue())

    def test_cli_reports_typed_pool_start_failure_without_secret_or_traceback(
        self,
    ) -> None:
        stderr = StringIO()
        failure = TOOL.AttendeeProviderPoolError(
            AttendeeProviderRejectionReason.PROVIDER_FAILED
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
            patch.object(TOOL, "serve_live", side_effect=failure),
            redirect_stderr(stderr),
        ):
            status = TOOL.main(self._serve_arguments())
        message = stderr.getvalue()
        self.assertEqual(status, 2)
        self.assertIn("provider_failed", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("provider-secret-never-print", message)
        self.assertNotIn(str(self.credential), message)

    def test_contract_validation_requires_all_five_capabilities_disabled(self) -> None:
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
        openapi = {
            "x-dreamland-attendee-provider-boundary": {
                "privacy_identity": {
                    "notice_version": TOOL.LIVE_ATTENDEE_NOTICE_VERSION,
                    "policy_id": TOOL.LIVE_ATTENDEE_POLICY_ID,
                }
            }
        }
        (contract / "openapi.json").write_text(json.dumps(openapi), encoding="utf-8")

        validator = SimpleNamespace(
            validate_contract=lambda _root: {
                "contract_revision": "0.8.0-rc.3",
                "implementation_status": "integration_preview_only",
                "capabilities_enabled": 0,
                "routes": 25,
            }
        )

        class Loader:
            def exec_module(self, _module):
                return None

        specification = SimpleNamespace(
            loader=Loader(),
            name="test_attendee_security_live_contract",
        )
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
            with self.assertRaisesRegex(
                ValueError, "capabilities must remain disabled"
            ):
                TOOL.validate_contract_root(contract)
            manifest["capabilities"]["public_attendee_sessions"]["enabled"] = False
            (contract / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            openapi["x-dreamland-attendee-provider-boundary"]["privacy_identity"][
                "notice_version"
            ] = TOOL._SECURITY.ATTENDEE_NOTICE_VERSION
            (contract / "openapi.json").write_text(
                json.dumps(openapi), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "privacy identity is incompatible"):
                TOOL.validate_contract_root(contract)

    def test_artifact_root_and_model_settings_fail_before_pool_start(self) -> None:
        self.artifact_root.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, "owner-private"):
            self._runtime()
        self.assertEqual(_FakePool.instances, [])

        with self.assertRaises((TypeError, ValueError)):
            AttendeeLiveProviderSettings(
                model="openai:gpt-attendee-test",
                system_prompt_name="system.md",
                max_output_tokens=0,
            )

    def _runtime(
        self,
        *,
        context_capacity=4,
        active_session_capacity=4,
        connection_capacity=8,
        worker_capacity=2,
        max_commands=64,
        max_output_chunks=TOOL.MAX_OUTPUT_CHUNKS_PER_COMMAND,
    ):
        return TOOL.start_live_runtime(
            artifact_root=self.artifact_root,
            authored_profile_path=self.profile_path,
            authored_profile_digest=self.profile_digest,
            provider_credential_file=self.credential,
            provider_settings=self.settings,
            context_capacity=context_capacity,
            active_session_capacity=active_session_capacity,
            connection_capacity=connection_capacity,
            worker_capacity=worker_capacity,
            max_commands_per_session=max_commands,
            max_output_chunks_per_command=max_output_chunks,
            provider_worker_factory=_FakeWorkerFactory,
            provider_pool_factory=_FakePool,
        )

    def _build_server(self, **overrides):
        values = {
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
            "contract_validator": lambda path: path.resolve(),
            "provider_worker_factory": _FakeWorkerFactory,
            "provider_pool_factory": _FakePool,
        }
        values.update(overrides)
        return TOOL.build_live_server(**values)

    def _serve_arguments(
        self,
        *,
        model="openai:gpt-attendee-test",
        max_output_tokens="12800",
        context_capacity=None,
        active_session_capacity=None,
        connection_capacity=None,
        worker_capacity="2",
    ) -> list[str]:
        arguments = [
            "serve",
            "--token-file",
            str(self.root / "product.token"),
            "--bootstrap-file",
            str(self.root / "bootstrap.json"),
            "--contract-root",
            str(self.root / "contract"),
            "--artifact-root",
            str(self.artifact_root),
            "--authored-profile-path",
            str(self.profile_path),
            "--authored-profile-digest",
            self.profile_digest,
            "--provider-credential-file",
            str(self.credential),
            "--model",
            model,
            "--max-output-tokens",
            max_output_tokens,
            "--worker-capacity",
            worker_capacity,
        ]
        for option, value in (
            ("--context-capacity", context_capacity),
            ("--active-session-capacity", active_session_capacity),
            ("--connection-capacity", connection_capacity),
        ):
            if value is not None:
                arguments.extend((option, value))
        return arguments


def _request(server, method: str, path: str) -> tuple[int, dict[str, object]]:
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    request = Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {SERVICE_BEARER}"},
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def _deep_document(depth: int = 40) -> dict[str, object]:
    document: dict[str, object] = {}
    nested = document
    for _index in range(depth):
        child: dict[str, object] = {}
        nested["nested"] = child
        nested = child
    return document


def _write_profile(path: Path, *, nested_purpose_depth: int = 0) -> None:
    purpose: dict[str, object] = {"theme": "Provider-free tool test"}
    nested = purpose
    for _index in range(nested_purpose_depth):
        child: dict[str, object] = {}
        nested["nested"] = child
        nested = child
    profile = StateProfile(
        name="private-attendee-live-test",
        level="world",
        created_at="2026-09-01T00:00:00+00:00",
        state={
            "world_state": {
                "name": "private-attendee-world",
                "entry_host": "console",
                "active_host": "console",
                "hosts_by_name": {
                    "console": {
                        "login_banner": "Test console\n",
                        "hostname": "console",
                        "purpose": purpose,
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
