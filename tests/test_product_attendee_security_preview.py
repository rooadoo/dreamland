from __future__ import annotations

import http.client
import importlib.util
import json
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dreamland.product.attendee_security import (
    AttendeeRequestAdmissionLedger,
    AttendeeRequestAdmissionPolicy,
)
from dreamland.product.execution import ProductExecutionResult
from dreamland.product.launch_selection import LaunchSelectionError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_security_preview.py"
SERVICE_BEARER = "private-attendee-security-service-bearer"
BFF_AUTHORITY = "bff-authority:private_attendee_security_test_0001"


class ManualClock:
    def __init__(self, now_ms: int) -> None:
        self.now_ms_value = now_ms

    def now_ms(self) -> int:
        return self.now_ms_value


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
    TOOL_PATH,
    "dreamland_product_attendee_security_preview_test_tool",
)


class RunningPreview:
    def __init__(self, *, harness_factory=None) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        arguments = {
            "host": "127.0.0.1",
            "port": 0,
            "bearer": SERVICE_BEARER,
            "bff_authority": BFF_AUTHORITY,
            "scenario": PREVIEW.SCENARIOS[0],
            "contract_root": root,
            "contract_validator": lambda path: path.resolve(),
        }
        if harness_factory is not None:
            arguments["harness_factory"] = harness_factory
        self.server = PREVIEW.build_preview_server(**arguments)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self._temporary.cleanup()

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


def header_only_problem(
    running: RunningPreview,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    declared_content_length: int,
) -> tuple[int, dict, object]:
    """Read a pre-body rejection without queuing bytes that can force TCP RST."""

    connection = http.client.HTTPConnection(
        "127.0.0.1",
        running.server.server_address[1],
        timeout=5,
    )
    try:
        connection.putrequest(method, path)
        connection.putheader("Authorization", f"Bearer {SERVICE_BEARER}")
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(declared_content_length))
        connection.putheader("Connection", "close")
        connection.endheaders()
        response = connection.getresponse()
        return response.status, json.loads(response.read()), response.headers
    finally:
        connection.close()


def service_epoch(running: RunningPreview) -> str:
    status, payload, _headers = request(
        running,
        "GET",
        PREVIEW._CORE.CAPABILITIES_PATH,
    )
    if status != 200:
        raise AssertionError(f"capabilities failed: {status} {payload}")
    return payload["service_epoch"]


def context_id(label: str) -> str:
    return f"gateway-context:{label * 32}"


def key(label: str) -> str:
    return f"attendee-security-test:{label}"


def context_headers(
    epoch: str,
    actor_context: str | None = None,
    *,
    idempotency: str | None = None,
    session_authority: str | None = None,
) -> dict[str, str]:
    headers = {"X-Dreamland-Expected-Epoch": epoch}
    if actor_context is not None:
        headers["X-Dreamland-Actor-Context"] = actor_context
    if idempotency is not None:
        headers["Idempotency-Key"] = key(idempotency)
    if session_authority is not None:
        headers["X-Dreamland-Session-Capability"] = session_authority
    return headers


def bootstrap(
    running: RunningPreview,
    epoch: str,
    label: str,
    idempotency: str,
) -> tuple[int, dict, object]:
    return request(
        running,
        "POST",
        PREVIEW.ATTENDEE_CONTEXTS_PATH,
        headers=context_headers(epoch, idempotency=idempotency),
        body={"gateway_context_id": context_id(label)},
    )


class ProductAttendeeSecurityPreviewTests(unittest.TestCase):
    def test_import_is_inert_provider_free_and_does_not_compose_runtime(self) -> None:
        code = f"""
import importlib.util
import json
import pathlib
import sys
import threading
before = [(thread.ident, thread.name) for thread in threading.enumerate()]
path = pathlib.Path({str(TOOL_PATH)!r})
spec = importlib.util.spec_from_file_location("attendee_security_inert", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
after = [(thread.ident, thread.name) for thread in threading.enumerate()]
print(json.dumps({{
    "before": before,
    "after": after,
    "provider_calls": module._CORE.PREVIEW_PROVIDER_CALLS,
    "revision": module.CONTRACT_REVISION,
    "runtime_modules": [
        name for name in ("forge", "world_access", "xray")
        if hasattr(module, name)
    ],
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
        self.assertEqual(payload["revision"], "0.8.0-rc.1")
        self.assertEqual(payload["runtime_modules"], [])

    def test_harness_composes_only_attendee_core_and_standard_capacity(self) -> None:
        harness = PREVIEW.AttendeeSecurityHarness(bff_authority=BFF_AUTHORITY)
        try:
            self.assertIs(harness.service, harness.attendee_service)
            self.assertFalse(hasattr(harness, "stage_service"))
            self.assertFalse(hasattr(harness, "handoff"))
            self.assertFalse(hasattr(harness, "world_access"))
            self.assertFalse(hasattr(harness, "xray"))
            limits = harness.service._core.admission.limits
            self.assertEqual(limits.max_physical_workers, 2)
            self.assertEqual(limits.presenter_stage_reserved_sessions, 0)
            self.assertEqual(limits.presenter_stage_reserved_logical_jobs, 0)
            self.assertEqual(limits.presenter_stage_reserved_physical_workers, 0)
        finally:
            harness.close()

    def test_default_composition_exactly_preserves_phase9a_bounds(self) -> None:
        composition = PREVIEW.default_attendee_security_composition()

        self.assertIsInstance(
            composition.executor,
            PREVIEW._CORE.DeterministicPreviewExecutor,
        )
        self.assertEqual(composition.revision_document, PREVIEW._authored_document())
        self.assertEqual(composition.physical_worker_capacity, 2)
        self.assertEqual(composition.active_session_capacity, 4)
        self.assertEqual(composition.logical_job_capacity, 8)
        self.assertEqual(composition.context_capacity, 4)
        self.assertEqual(composition.connection_capacity, 8)
        self.assertEqual(composition.max_fresh_operations_per_context, 8)
        self.assertEqual(composition.max_fresh_operations_global, 16)
        self.assertEqual(composition.max_commands_per_session, 8)
        self.assertEqual(composition.command_deadline_ms, 10_000)
        self.assertEqual(composition.cancellation_deadline_ms, 2_000)
        self.assertEqual(composition.max_output_bytes_per_command, 16 * 1_024)
        self.assertEqual(composition.max_output_chunks_per_command, 16)
        self.assertEqual(composition.max_runtime_document_bytes, 4 * 1_024 * 1_024)
        self.assertEqual(composition.notice_version, PREVIEW.ATTENDEE_NOTICE_VERSION)
        self.assertEqual(composition.policy_id, PREVIEW.ATTENDEE_POLICY_ID)
        self.assertEqual(composition.reserved_tokens_per_command, 32)
        self.assertEqual(composition.reserved_spend_units_per_command, 32)
        self.assertEqual(composition.token_budget, 256)
        self.assertEqual(composition.spend_budget_units, 256)
        self.assertIsNone(composition.availability_window)
        self.assertFalse(composition.bootstrap_closes_at_admission)
        self.assertIsNone(composition.validate_runtime_document)
        with self.assertRaises(FrozenInstanceError):
            composition.logical_job_capacity = 9

    def test_availability_window_is_frozen_and_strictly_validated(self) -> None:
        window = PREVIEW.AttendeeSecurityAvailabilityWindow(
            admission_opens_at_ms=1_000,
            admission_closes_at_ms=2_001,
            recovery_closes_at_ms=3_000,
        )
        self.assertEqual(window.admission_opens_at_ms, 1_000)
        with self.assertRaises(FrozenInstanceError):
            window.admission_opens_at_ms = 0

        invalid_timestamps = (
            {"admission_opens_at_ms": True},
            {"admission_opens_at_ms": -1},
            {"admission_opens_at_ms": 1.5},
            {"admission_closes_at_ms": PREVIEW._MAX_TIMESTAMP_MS + 1},
            {"recovery_closes_at_ms": False},
        )
        for changes in invalid_timestamps:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "bounded timestamp"):
                    replace(window, **changes)

        ordering_cases = (
            (
                {"admission_closes_at_ms": 1_000},
                "close after it opens",
            ),
            (
                {"admission_closes_at_ms": 2_000},
                "longer than 1000ms",
            ),
            (
                {"recovery_closes_at_ms": 2_001},
                "recovery must close after admission",
            ),
        )
        for changes, message in ordering_cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, message):
                    replace(window, **changes)

        with self.assertRaisesRegex(TypeError, "must be a boolean"):
            replace(
                PREVIEW.default_attendee_security_composition(),
                bootstrap_closes_at_admission=1,
            )

        with self.assertRaisesRegex(TypeError, "availability_window"):
            replace(
                PREVIEW.default_attendee_security_composition(),
                availability_window={"admission_closes_at_ms": 2_000},
            )

    def test_default_harness_resolves_the_exact_phase9a_window(self) -> None:
        now_ms = 1_000_000
        clock = ManualClock(now_ms)
        harness = PREVIEW.AttendeeSecurityHarness(
            bff_authority=BFF_AUTHORITY,
            clock=clock,
        )
        try:
            expected = PREVIEW.AttendeeSecurityAvailabilityWindow(
                admission_opens_at_ms=now_ms,
                admission_closes_at_ms=(
                    now_ms + 12 * 60 * PREVIEW.MILLISECONDS_PER_MINUTE
                ),
                recovery_closes_at_ms=(
                    now_ms + 24 * 60 * PREVIEW.MILLISECONDS_PER_MINUTE
                ),
            )
            self.assertEqual(harness.resolved_availability_window, expected)
            self.assertEqual(harness.availability_window, expected)
            self.assertIsNone(harness.composition.availability_window)
            self.assertEqual(
                harness.service._policy.admission_opens_at_ms,
                expected.admission_opens_at_ms,
            )
            self.assertEqual(
                harness.service._policy.admission_closes_at_ms,
                expected.admission_closes_at_ms,
            )
            offering = next(iter(harness.service._registry._offerings.values()))
            self.assertEqual(
                offering.expires_at_ms,
                expected.admission_closes_at_ms - 1_000,
            )
            actor_expiries = {
                actor.expires_at_ms
                for actor in harness.service._registry._actors.values()
            }
            self.assertEqual(actor_expiries, {expected.recovery_closes_at_ms})
            bff_expiries = {
                record.expires_at_ms
                for record in harness.service._bff_authorities.values()
            }
            self.assertEqual(bff_expiries, {expected.recovery_closes_at_ms})
        finally:
            harness.close()

    def test_configured_window_propagates_and_enforces_open_and_cutoff(self) -> None:
        clock = ManualClock(1_000)
        window = PREVIEW.AttendeeSecurityAvailabilityWindow(
            admission_opens_at_ms=2_000,
            admission_closes_at_ms=10_000,
            recovery_closes_at_ms=15_000,
        )
        composition = replace(
            PREVIEW.default_attendee_security_composition(),
            availability_window=window,
        )
        harness = PREVIEW.AttendeeSecurityHarness(
            bff_authority=BFF_AUTHORITY,
            clock=clock,
            composition=composition,
        )
        try:
            service = harness.service
            epoch = service.service_epoch
            self.assertIs(harness.resolved_availability_window, window)
            self.assertIs(harness.availability_window, window)
            self.assertEqual(service._policy.admission_opens_at_ms, 2_000)
            self.assertEqual(service._policy.admission_closes_at_ms, 10_000)
            self.assertEqual(service._admission_opens_deadline_ms, 2_000)
            self.assertEqual(service._admission_closes_deadline_ms, 10_000)
            offering = next(iter(service._registry._offerings.values()))
            self.assertEqual(offering.expires_at_ms, 9_000)
            self.assertEqual(offering.expires_deadline_ms, 9_000)
            self.assertEqual(
                {actor.expires_at_ms for actor in service._registry._actors.values()},
                {15_000},
            )
            self.assertEqual(
                {actor.expires_at_ms for actor in service._core._actors.values()},
                {15_000},
            )
            self.assertEqual(
                {
                    authority.expires_at_ms
                    for authority in service._bff_authorities.values()
                },
                {15_000},
            )

            context = service.bootstrap_context(
                bff_authority=BFF_AUTHORITY,
                expected_epoch=epoch,
                gateway_context_id=context_id("a"),
                idempotency_key=key("availability-bootstrap"),
            )
            actor = context["actor_context"]
            option = service.options(
                actor_authority=actor,
                expected_epoch=epoch,
            )["options"][0]
            exchange_arguments = {
                "actor_authority": actor,
                "expected_epoch": epoch,
                "idempotency_key": key("availability-exchange"),
                "launch_selection_id": option["launch_selection_id"],
                "notice_version_presented": option["notice_version"],
            }
            with self.assertRaises(LaunchSelectionError) as not_open:
                service.exchange(**exchange_arguments)
            self.assertEqual(not_open.exception.code, "service_not_open")

            clock.now_ms_value = 2_000
            granted = service.exchange(**exchange_arguments)
            self.assertEqual(
                granted["kind"],
                "attendee_launch_selection_granted",
            )

            clock.now_ms_value = 10_000
            with self.assertRaises(LaunchSelectionError) as closed:
                service.exchange(
                    **dict(
                        exchange_arguments,
                        idempotency_key=key("availability-after-cutoff"),
                    )
                )
            self.assertEqual(closed.exception.code, "service_closed")
        finally:
            harness.close()

    def test_configured_window_rejects_construction_at_close_or_recovery(self) -> None:
        window = PREVIEW.AttendeeSecurityAvailabilityWindow(
            admission_opens_at_ms=1_000,
            admission_closes_at_ms=5_000,
            recovery_closes_at_ms=7_000,
        )
        composition = replace(
            PREVIEW.default_attendee_security_composition(),
            availability_window=window,
        )
        for now_ms, message in (
            (5_000, "admission window has already closed"),
            (7_000, "recovery window has already closed"),
            (8_000, "recovery window has already closed"),
        ):
            with self.subTest(now_ms=now_ms):
                with self.assertRaisesRegex(ValueError, message):
                    PREVIEW.AttendeeSecurityHarness(
                        bff_authority=BFF_AUTHORITY,
                        clock=ManualClock(now_ms),
                        composition=composition,
                    )

    def test_composition_snapshots_revision_before_harness_construction(self) -> None:
        source = PREVIEW._authored_document()
        expected = json.loads(json.dumps(source))
        composition = replace(
            PREVIEW.default_attendee_security_composition(),
            revision_document=source,
        )
        source["profile"]["state"]["world_state"]["name"] = "source-mutated"
        normalized = composition.revision_document
        self.assertIs(type(normalized), dict)
        normalized["profile"]["state"]["world_state"]["name"] = "field-mutated"

        harness = PREVIEW.AttendeeSecurityHarness(
            bff_authority=BFF_AUTHORITY,
            composition=composition,
        )
        try:
            revision = next(iter(harness.service._core._revisions.values())).allocate()
            self.assertEqual(revision, expected)
        finally:
            harness.close()

    def test_composition_injection_drives_only_attendee_worker_and_core_bounds(
        self,
    ) -> None:
        class InjectedExecutor:
            def execute(self, _request, _emit):
                raise AssertionError("injected executor was not expected to run")

        executor = InjectedExecutor()
        validated_documents = []

        def validate_runtime_document(document):
            validated_documents.append(document)

        document = PREVIEW._authored_document()
        document["profile"]["state"]["world_state"]["name"] = "injected-world"
        composition = PREVIEW.AttendeeSecurityComposition(
            executor=executor,
            revision_document=document,
            physical_worker_capacity=5,
            active_session_capacity=20,
            logical_job_capacity=40,
            max_commands_per_session=None,
            command_deadline_ms=7_000,
            cancellation_deadline_ms=1_000,
            max_output_bytes_per_command=8_192,
            max_output_chunks_per_command=8,
            max_runtime_document_bytes=256 * 1_024,
            reserved_tokens_per_command=10,
            reserved_spend_units_per_command=11,
            token_budget=400,
            spend_budget_units=440,
            context_capacity=20,
            connection_capacity=20,
            max_fresh_operations_per_context=8,
            max_fresh_operations_global=65,
            validate_runtime_document=validate_runtime_document,
        )

        def factory(**arguments):
            return PREVIEW.AttendeeSecurityHarness(
                **arguments,
                composition=composition,
            )

        harness = factory(
            bff_authority=BFF_AUTHORITY,
            scenario=PREVIEW.SCENARIOS[0],
        )
        try:
            core = harness.service._core
            offering = next(iter(harness.service._registry._offerings.values()))
            revision = next(iter(core._revisions.values())).allocate()

            class AnnouncementServer:
                server_address = ("127.0.0.1", 0)
                preview = harness

            announcement = PREVIEW.startup_announcement(AnnouncementServer())

            self.assertIs(harness.composition, composition)
            self.assertIs(core._executor, executor)
            self.assertIs(core._validate_state_effect, validate_runtime_document)
            self.assertEqual(validated_documents, [document])
            self.assertEqual(harness._workers._max_workers, 5)
            self.assertEqual(revision, document)
            self.assertEqual(core._policy.max_active_sessions, 20)
            self.assertIsNone(core._core_policy.max_commands_per_session)
            self.assertEqual(core._core_policy.command_deadline_ms, 7_000)
            self.assertEqual(core._core_policy.cancellation_deadline_ms, 1_000)
            self.assertEqual(core._core_policy.max_output_bytes_per_command, 8_192)
            self.assertEqual(core._core_policy.max_output_chunks_per_command, 8)
            self.assertEqual(core._core_policy.max_runtime_document_bytes, 256 * 1_024)
            self.assertEqual(core._core_policy.reserved_tokens_per_command, 10)
            self.assertEqual(core._core_policy.reserved_spend_units_per_command, 11)
            self.assertEqual(core._core_policy.max_attempts_per_command, 1)
            self.assertEqual(core.admission.limits.max_active_sessions, 20)
            self.assertEqual(core.admission.limits.max_logical_jobs, 40)
            self.assertEqual(core.admission.limits.max_physical_workers, 5)
            self.assertEqual(core.admission.limits.token_budget, 400)
            self.assertEqual(core.admission.limits.spend_budget_units, 440)
            self.assertNotIn("max_commands", offering.limits)
            self.assertEqual(offering.limits["max_command_bytes"], 4_096)
            self.assertEqual(offering.limits["command_max_attempts"], 1)
            self.assertEqual(offering.limits["command_deadline_ms"], 7_000)
            self.assertEqual(offering.limits["command_reserved_tokens"], 10)
            self.assertEqual(offering.limits["command_reserved_spend_units"], 11)
            self.assertEqual(announcement["resources"]["active_sessions"], 20)
            self.assertEqual(announcement["resources"]["context_capacity"], 20)
            self.assertEqual(announcement["resources"]["logical_jobs"], 40)
            self.assertEqual(announcement["resources"]["physical_workers"], 5)
            self.assertEqual(announcement["resources"]["spend_budget_units"], 440)
            self.assertEqual(announcement["resources"]["token_budget"], 400)
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
                announcement["resources"]["retention"],
                {
                    "bootstrap_idempotency_records": 21,
                    "exchange_idempotency_records": 64,
                    "launch_grants": 40,
                    "lifecycle_idempotency_records": 160,
                    "revoke_idempotency_records": 20,
                    "selection_idempotency_records": 64,
                    "selection_token_history": 128,
                    "tombstones": 40,
                },
            )
            self.assertEqual(announcement["security"]["connection_limit"], 20)
            self.assertEqual(announcement["security"]["logical_per_context_limit"], 8)
            self.assertEqual(announcement["security"]["logical_global_limit"], 65)
            self.assertEqual(announcement["security"]["logical_tracked_contexts"], 20)
            self.assertEqual(len(harness.service._registry._actors), 20)
            self.assertEqual(len(core._actors), 20)
            self.assertEqual(harness.service._policy.max_contexts, 20)
            self.assertEqual(
                harness.request_admission.policy.max_tracked_contexts,
                20,
            )
            self.assertGreaterEqual(
                harness.service._registry._policy.max_active_launch_grants,
                20,
            )
            self.assertGreaterEqual(core._policy.max_launch_grants, 20)
            self.assertGreater(
                harness.service._policy.max_bootstrap_idempotency_records,
                20,
            )
            self.assertGreaterEqual(
                harness.service._policy.max_revoke_idempotency_records,
                20,
            )
            self.assertFalse(hasattr(harness, "stage_service"))
            self.assertFalse(hasattr(harness, "forge_service"))
            self.assertFalse(hasattr(harness, "world_access"))
        finally:
            harness.close()

    def test_composition_rejects_invalid_or_overcommitted_bounds(self) -> None:
        valid = PREVIEW.default_attendee_security_composition()
        cases = (
            ({"notice_version": ""}, "bounded safe identity"),
            ({"policy_id": "policy with spaces"}, "bounded safe identity"),
            ({"physical_worker_capacity": 0}, "positive bounded integer"),
            ({"active_session_capacity": True}, "positive bounded integer"),
            ({"context_capacity": 0}, "context_capacity"),
            ({"connection_capacity": 0}, "connection_capacity"),
            (
                {"context_capacity": (PREVIEW.MAXIMUM_ATTENDEE_CONTEXT_CAPACITY + 1)},
                "context_capacity",
            ),
            (
                {
                    "active_session_capacity": (
                        PREVIEW.MAXIMUM_ATTENDEE_ACTIVE_SESSION_CAPACITY + 1
                    ),
                    "context_capacity": PREVIEW.MAXIMUM_ATTENDEE_CONTEXT_CAPACITY,
                },
                "active_session_capacity",
            ),
            (
                {
                    "connection_capacity": (
                        PREVIEW.MAXIMUM_ATTENDEE_CONNECTION_CAPACITY + 1
                    )
                },
                "connection_capacity",
            ),
            (
                {
                    "physical_worker_capacity": (
                        PREVIEW.MAXIMUM_ATTENDEE_WORKER_CAPACITY + 1
                    ),
                    "active_session_capacity": 9,
                    "context_capacity": 9,
                    "logical_job_capacity": 9,
                },
                "physical_worker_capacity",
            ),
            (
                {"max_fresh_operations_per_context": 0},
                "max_fresh_operations_per_context",
            ),
            (
                {"max_fresh_operations_global": 0},
                "max_fresh_operations_global",
            ),
            ({"max_commands_per_session": 0}, "positive bounded integer"),
            ({"max_output_chunks_per_command": 2**63}, "positive bounded integer"),
            ({"max_runtime_document_bytes": 0}, "positive bounded integer"),
            (
                {
                    "max_runtime_document_bytes": (
                        len(valid._revision_canonical_json) - 1
                    )
                },
                "bounded authored revision",
            ),
            (
                {"cancellation_deadline_ms": valid.command_deadline_ms + 1},
                "cannot exceed command_deadline_ms",
            ),
            (
                {"physical_worker_capacity": valid.logical_job_capacity + 1},
                "cannot exceed logical_job_capacity",
            ),
            (
                {"active_session_capacity": valid.context_capacity + 1},
                "cannot exceed attendee context capacity",
            ),
            (
                {"physical_worker_capacity": valid.active_session_capacity + 1},
                "cannot exceed active session capacity",
            ),
            (
                {"logical_job_capacity": valid.active_session_capacity - 1},
                "must cover active session capacity",
            ),
            (
                {
                    "max_fresh_operations_global": (
                        valid.max_fresh_operations_per_context - 1
                    )
                },
                "must cover the per-context limit",
            ),
            ({"token_budget": valid.token_budget - 1}, "token budget"),
            ({"spend_budget_units": valid.spend_budget_units - 1}, "spend budget"),
            (
                {"command_deadline_ms": PREVIEW._MAX_LAUNCH_LIMIT + 1},
                "launch-authority limit",
            ),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, message):
                    replace(valid, **changes)

        with self.assertRaisesRegex(TypeError, "executor"):
            replace(valid, executor=object())
        with self.assertRaisesRegex(TypeError, "validate_runtime_document"):
            replace(valid, validate_runtime_document=object())
        with self.assertRaisesRegex(TypeError, "revision_document"):
            replace(valid, revision_document=[])
        with self.assertRaisesRegex(ValueError, "finite JSON"):
            replace(
                valid,
                revision_document={
                    "kind": "dreamland.authored_world_revision",
                    "not_finite": float("nan"),
                },
            )
        for revision_document in (
            {
                "kind": "dreamland.authored_world_revision",
                "schema_revision": "2",
                "profile": {},
            },
            {
                "kind": "dreamland.authored_world_revision",
                "schema_revision": "1",
                "profile": [],
            },
        ):
            with self.subTest(revision_document=revision_document):
                with self.assertRaisesRegex(ValueError, "bounded authored revision"):
                    replace(valid, revision_document=revision_document)

        recursive = PREVIEW._authored_document()
        nested = recursive["profile"]
        for _index in range(10 * sys.getrecursionlimit() + 100):
            child = {}
            nested["nested"] = child
            nested = child
        with self.assertRaisesRegex(ValueError, "finite JSON"):
            replace(valid, revision_document=recursive)

        invalid_unicode = PREVIEW._authored_document()
        invalid_unicode["profile"]["invalid_unicode"] = "\ud800"
        with self.assertRaisesRegex(ValueError, "finite JSON"):
            replace(valid, revision_document=invalid_unicode)

    def test_twenty_context_session_envelope_reaches_capacity_and_cleans_up(
        self,
    ) -> None:
        clock = ManualClock(1_000_000)
        composition = replace(
            PREVIEW.default_attendee_security_composition(),
            physical_worker_capacity=5,
            active_session_capacity=20,
            logical_job_capacity=20,
            max_commands_per_session=1,
            token_budget=20 * 32,
            spend_budget_units=20 * 32,
            context_capacity=20,
            connection_capacity=20,
            max_fresh_operations_per_context=8,
            max_fresh_operations_global=65,
        )
        harness = PREVIEW.AttendeeSecurityHarness(
            bff_authority=BFF_AUTHORITY,
            clock=clock,
            composition=composition,
        )
        workflows: list[dict[str, object]] = []
        try:
            service = harness.service
            epoch = service.service_epoch
            for index in range(20):
                gateway_context_id = context_id(str(index))
                bootstrapped = service.bootstrap_context(
                    bff_authority=BFF_AUTHORITY,
                    expected_epoch=epoch,
                    gateway_context_id=gateway_context_id,
                    idempotency_key=key(f"capacity-bootstrap-{index}"),
                )
                actor = bootstrapped["actor_context"]
                option = service.options(
                    actor_authority=actor,
                    expected_epoch=epoch,
                )["options"][0]
                granted = service.exchange(
                    actor_authority=actor,
                    expected_epoch=epoch,
                    idempotency_key=key(f"capacity-exchange-{index}"),
                    launch_selection_id=option["launch_selection_id"],
                    notice_version_presented=option["notice_version"],
                )
                created = service.create(
                    actor_authority=actor,
                    expected_epoch=epoch,
                    idempotency_key=key(f"capacity-create-{index}"),
                    launch_grant=granted["launch_grant"],
                )
                workflows.append(
                    {
                        "actor": actor,
                        "gateway_context_id": gateway_context_id,
                        "reference": created["snapshot"]["reference"],
                        "session_authority": created["session_authority"],
                        "control_revision": created["snapshot"]["coordinates"][
                            "control_revision"
                        ],
                    }
                )

            self.assertEqual(service._core.admission.snapshot().active_sessions, 20)
            with self.assertRaises(LaunchSelectionError) as rate_limited:
                service.bootstrap_context(
                    bff_authority=BFF_AUTHORITY,
                    expected_epoch=epoch,
                    gateway_context_id=context_id("z"),
                    idempotency_key=key("capacity-bootstrap-overflow"),
                )
            self.assertEqual(rate_limited.exception.code, "logical_rate_limited")
            self.assertEqual(rate_limited.exception.status, 429)
            self.assertEqual(rate_limited.exception.retry, "after_delay")

            clock.now_ms_value += 1_001
            with self.assertRaises(LaunchSelectionError) as exhausted:
                service.bootstrap_context(
                    bff_authority=BFF_AUTHORITY,
                    expected_epoch=epoch,
                    gateway_context_id=context_id("z"),
                    idempotency_key=key("capacity-bootstrap-overflow"),
                )
            self.assertEqual(
                exhausted.exception.code,
                "attendee_context_capacity_exhausted",
            )
            self.assertEqual(exhausted.exception.status, 503)
            self.assertEqual(exhausted.exception.retry, "none")

            for index, workflow in enumerate(workflows):
                closed = service.close(
                    actor_authority=workflow["actor"],
                    session_authority=workflow["session_authority"],
                    reference=workflow["reference"],
                    expected_epoch=epoch,
                    idempotency_key=key(f"capacity-close-{index}"),
                    expected_control_revision=workflow["control_revision"],
                )
                self.assertEqual(closed["snapshot"]["lifecycle"]["state"], "closed")
                revoked = service.revoke_context(
                    bff_authority=BFF_AUTHORITY,
                    actor_authority=workflow["actor"],
                    expected_epoch=epoch,
                    gateway_context_id=workflow["gateway_context_id"],
                    idempotency_key=key(f"capacity-revoke-{index}"),
                )
                self.assertEqual(revoked["kind"], "attendee_context_revoked")
                self.assertEqual(revoked["outcome"], "revoked")

            self.assertEqual(service._core.admission.snapshot().active_sessions, 0)
            self.assertEqual(len(service._revoke_idempotency), 20)
        finally:
            harness.close()

    def test_five_workers_saturate_and_unclaimed_sixth_retry_recovers(self) -> None:
        class BlockingExecutor:
            def __init__(self) -> None:
                self._lock = threading.Lock()
                self._release = {
                    f"capacity-command-{index}": threading.Event() for index in range(6)
                }
                self.requests = []
                self.five_started = threading.Event()
                self.sixth_started = threading.Event()
                self.active = 0
                self.maximum_active = 0

            def execute(self, request, _emit):
                with self._lock:
                    self.requests.append(request)
                    self.active += 1
                    self.maximum_active = max(self.maximum_active, self.active)
                    if self.active == 5:
                        self.five_started.set()
                    if len(self.requests) == 6:
                        self.sixth_started.set()
                release = self._release[request.canonical_command]
                if not release.wait(10):
                    raise RuntimeError("test executor release timed out")
                with self._lock:
                    self.active -= 1
                return ProductExecutionResult.completed_exited(exit_status=0)

            def release(self, command: str) -> None:
                self._release[command].set()

            def release_all(self) -> None:
                for release in self._release.values():
                    release.set()

            def request_count(self) -> int:
                with self._lock:
                    return len(self.requests)

        executor = BlockingExecutor()
        clock = ManualClock(1_000)
        default = PREVIEW.default_attendee_security_composition()
        composition = replace(
            default,
            executor=executor,
            physical_worker_capacity=5,
            active_session_capacity=6,
            logical_job_capacity=6,
            max_commands_per_session=1,
            token_budget=6 * default.reserved_tokens_per_command,
            spend_budget_units=6 * default.reserved_spend_units_per_command,
            context_capacity=6,
            max_fresh_operations_global=PREVIEW.derived_fresh_operation_capacity(
                context_capacity=6,
                physical_worker_capacity=5,
            ),
        )
        harness = PREVIEW.AttendeeSecurityHarness(
            bff_authority=BFF_AUTHORITY,
            clock=clock,
            composition=composition,
        )
        workflows: list[dict[str, object]] = []
        submitted_futures = set()
        try:
            service = harness.service
            epoch = service.service_epoch
            for index in range(6):
                bootstrapped = service.bootstrap_context(
                    bff_authority=BFF_AUTHORITY,
                    expected_epoch=epoch,
                    gateway_context_id=context_id(str(index)),
                    idempotency_key=key(f"worker-bootstrap-{index}"),
                )
                actor = bootstrapped["actor_context"]
                option = service.options(
                    actor_authority=actor,
                    expected_epoch=epoch,
                )["options"][0]
                granted = service.exchange(
                    actor_authority=actor,
                    expected_epoch=epoch,
                    idempotency_key=key(f"worker-exchange-{index}"),
                    launch_selection_id=option["launch_selection_id"],
                    notice_version_presented=option["notice_version"],
                )
                created = service.create(
                    actor_authority=actor,
                    expected_epoch=epoch,
                    idempotency_key=key(f"worker-create-{index}"),
                    launch_grant=granted["launch_grant"],
                )
                workflows.append(
                    {
                        "actor_authority": actor,
                        "session_authority": created["session_authority"],
                        "reference": created["snapshot"]["reference"],
                        "expected_epoch": epoch,
                    }
                )

            # Put command admission in a fresh fixed window so this assertion
            # exercises worker capacity, not the preceding launch burst.
            clock.now_ms_value += 1_000

            def submit(index: int) -> dict[str, object]:
                return service.submit_command(
                    **workflows[index],
                    idempotency_key=key(f"worker-submit-{index}"),
                    expected_slot_generation=0,
                    command=f"capacity-command-{index}",
                )

            accepted = [submit(index) for index in range(5)]
            self.assertTrue(executor.five_started.wait(10))
            self.assertEqual(executor.maximum_active, 5)
            self.assertEqual(executor.request_count(), 5)
            self.assertTrue(
                all(item["kind"] == "command_accepted" for item in accepted)
            )
            with harness._future_lock:
                initial_futures = set(harness._futures)
            self.assertEqual(len(initial_futures), 5)
            submitted_futures.update(initial_futures)

            with self.assertRaises(PREVIEW.ProductLifecycleError) as saturated:
                submit(5)
            self.assertEqual(saturated.exception.code, "capacity_exhausted")
            self.assertEqual(saturated.exception.status, 503)
            self.assertEqual(saturated.exception.retry, "after_delay")
            self.assertGreater(saturated.exception.retry_after_ms, 0)
            self.assertEqual(executor.request_count(), 5)

            one_completed = threading.Event()
            for future in initial_futures:
                future.add_done_callback(lambda _future: one_completed.set())
            executor.release("capacity-command-0")
            self.assertTrue(one_completed.wait(10))
            first_terminal = service.command_result(
                **workflows[0],
                command_id=accepted[0]["command_id"],
            )
            self.assertEqual(
                first_terminal["command"]["result"]["outcome"],
                "completed",
                first_terminal,
            )
            recovered = service._core.admission.snapshot()
            self.assertEqual(recovered.physical_workers, 4)
            self.assertEqual(recovered.unsettled_jobs, 4)

            retried = submit(5)
            self.assertEqual(retried["kind"], "command_accepted")
            self.assertFalse(retried["replayed"])
            self.assertTrue(executor.sixth_started.wait(10))
            self.assertEqual(executor.request_count(), 6)
            self.assertEqual(executor.maximum_active, 5)
            with harness._future_lock:
                submitted_futures.update(harness._futures)

            executor.release_all()
            for future in submitted_futures:
                future.result(timeout=10)
            for index, item in enumerate(accepted[1:] + [retried], start=1):
                terminal = service.command_result(
                    **workflows[index],
                    command_id=item["command_id"],
                )
                self.assertEqual(terminal["command"]["result"]["outcome"], "completed")
            recovered = service._core.admission.snapshot()
            self.assertEqual(recovered.physical_workers, 0)
            self.assertEqual(recovered.unsettled_jobs, 0)
        finally:
            executor.release_all()
            harness.close()

    def test_attendee_core_allowlist_hides_stage_forge_world_and_admin_routes(
        self,
    ) -> None:
        hidden = (
            ("GET", "/product/v1/stage/launch-options"),
            ("POST", "/product/v1/stage/sessions"),
            ("GET", "/product/v1/stage/sessions/current"),
            ("GET", "/product/v1/stage/sessions/session-ref:hidden/xray"),
            ("POST", "/product/v1/stage/forge-handoffs"),
            ("GET", "/forge/v1/jobs"),
            ("GET", "/world-access/v1/catalog"),
            ("GET", "/admin/v1/sessions"),
        )
        with RunningPreview() as running:
            unauthorized_status, unauthorized, _ = request(
                running,
                "GET",
                hidden[0][1],
                bearer="wrong-private-attendee-bearer",
            )
            responses = [request(running, method, path) for method, path in hidden]
            status, capabilities, headers = request(
                running,
                "GET",
                PREVIEW._CORE.CAPABILITIES_PATH,
            )

        self.assertEqual(unauthorized_status, 401)
        self.assertEqual(unauthorized["error"]["code"], "unauthorized")
        for hidden_status, hidden_payload, _headers in responses:
            self.assertEqual(hidden_status, 404, hidden_payload)
            self.assertEqual(hidden_payload["error"]["code"], "not_found")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Dreamland-Contract-Revision"], "0.8.0-rc.1")
        self.assertTrue(
            all(not item["enabled"] for item in capabilities["capabilities"].values())
        )

    def test_attendee_selection_and_session_are_isolated_from_other_roles(self) -> None:
        with RunningPreview() as running:
            epoch = service_epoch(running)
            status, context, _ = bootstrap(running, epoch, "a", "bootstrap-a")
            self.assertEqual(status, 201, context)
            actor = context["actor_context"]
            option_status, options, _ = request(
                running,
                "GET",
                PREVIEW.ATTENDEE_LAUNCH_OPTIONS_PATH,
                headers=context_headers(epoch, actor),
            )
            option = options["options"][0]
            exchange_status, granted, _ = request(
                running,
                "POST",
                PREVIEW.ATTENDEE_LAUNCH_SELECTIONS_PATH,
                headers=context_headers(
                    epoch,
                    actor,
                    idempotency="exchange-a",
                ),
                body={
                    "launch_selection_id": option["launch_selection_id"],
                    "notice_version_presented": option["notice_version"],
                },
            )
            create_status, created, _ = request(
                running,
                "POST",
                PREVIEW._CORE.SESSIONS_PATH,
                headers=context_headers(
                    epoch,
                    actor,
                    idempotency="create-a",
                ),
                body={"launch_grant": granted["launch_grant"]},
            )
            self.assertEqual(create_status, 201, created)
            reference = quote(created["snapshot"]["reference"], safe="")
            denied_actor = "actor:presenter_shaped_but_unowned_0001"
            denied_options_status, denied_options, _ = request(
                running,
                "GET",
                PREVIEW.ATTENDEE_LAUNCH_OPTIONS_PATH,
                headers=context_headers(epoch, denied_actor),
            )
            denied_snapshot_status, denied_snapshot, _ = request(
                running,
                "GET",
                f"{PREVIEW._CORE.SESSION_PATH_PREFIX}{reference}",
                headers=context_headers(
                    epoch,
                    denied_actor,
                    session_authority=created["session_authority"],
                ),
            )

        self.assertEqual(option_status, 200)
        self.assertEqual(exchange_status, 200)
        self.assertEqual(create_status, 201)
        self.assertEqual(denied_options_status, 403)
        self.assertEqual(denied_options["error"]["code"], "authority_denied")
        self.assertEqual(denied_snapshot_status, 403)
        self.assertEqual(denied_snapshot["error"]["code"], "authority_denied")

    def test_context_revocation_binds_actor_replays_and_echoes_no_authority(
        self,
    ) -> None:
        with RunningPreview() as running:
            epoch = service_epoch(running)
            _, context_a, _ = bootstrap(running, epoch, "a", "bootstrap-a")
            _, context_b, _ = bootstrap(running, epoch, "b", "bootstrap-b")
            actor_a = context_a["actor_context"]
            actor_b = context_b["actor_context"]
            path = PREVIEW.ATTENDEE_CONTEXT_PREFIX + quote(context_id("a"), safe="")
            mismatch_status, mismatch, _ = request(
                running,
                "DELETE",
                path,
                headers=context_headers(
                    epoch,
                    actor_b,
                    idempotency="revoke-mismatch",
                ),
            )
            revoke_status, revoked, _ = request(
                running,
                "DELETE",
                path,
                headers=context_headers(
                    epoch,
                    actor_a,
                    idempotency="revoke-a",
                ),
            )
            replay_status, replayed, _ = request(
                running,
                "DELETE",
                path,
                headers=context_headers(
                    epoch,
                    actor_a,
                    idempotency="revoke-a",
                ),
            )
            after_status, after, _ = request(
                running,
                "GET",
                PREVIEW.ATTENDEE_LAUNCH_OPTIONS_PATH,
                headers=context_headers(epoch, actor_a),
            )
            body_status, body_error, _ = header_only_problem(
                running,
                "DELETE",
                PREVIEW.ATTENDEE_CONTEXT_PREFIX + quote(context_id("b"), safe=""),
                headers=context_headers(
                    epoch,
                    actor_b,
                    idempotency="revoke-with-body",
                ),
                declared_content_length=2,
            )

        self.assertEqual(mismatch_status, 403)
        self.assertEqual(mismatch["error"]["code"], "authority_denied")
        self.assertEqual(revoke_status, 200)
        self.assertEqual(revoked["kind"], "attendee_context_revoked")
        self.assertEqual(revoked["outcome"], "revoked")
        self.assertFalse(revoked["replayed"])
        self.assertNotIn("actor:", json.dumps(revoked))
        self.assertNotIn("gateway-context:", json.dumps(revoked))
        self.assertEqual(replay_status, 200)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["outcome"], "revoked")
        self.assertEqual(after_status, 403)
        self.assertEqual(after["error"]["code"], "authority_denied")
        self.assertEqual(body_status, 400)
        self.assertEqual(body_error["error"]["code"], "invalid_request")

    def test_logical_limit_is_unclaimed_and_exact_replay_bypasses_it(self) -> None:
        admission = AttendeeRequestAdmissionLedger(
            AttendeeRequestAdmissionPolicy(
                window_ms=10_000,
                max_fresh_operations_per_context=1,
                max_fresh_operations_global=1,
                max_tracked_contexts=PREVIEW.ATTENDEE_CONTEXT_CAPACITY,
            )
        )

        def factory(**arguments):
            return PREVIEW.AttendeeSecurityHarness(
                **arguments,
                request_admission=admission,
            )

        with RunningPreview(harness_factory=factory) as running:
            epoch = service_epoch(running)
            first_status, first, _ = bootstrap(
                running,
                epoch,
                "a",
                "limited-a",
            )
            replay_status, replay, _ = bootstrap(
                running,
                epoch,
                "a",
                "limited-a",
            )
            limited_status, limited, _ = bootstrap(
                running,
                epoch,
                "b",
                "limited-b",
            )
            limited_retry_status, limited_retry, _ = bootstrap(
                running,
                epoch,
                "b",
                "limited-b",
            )

        self.assertEqual(first_status, 201)
        self.assertFalse(first["replayed"])
        self.assertEqual(replay_status, 201)
        self.assertTrue(replay["replayed"])
        self.assertEqual(limited_status, 429)
        self.assertEqual(limited["error"]["code"], "logical_rate_limited")
        self.assertEqual(limited["error"]["retry"], "after_delay")
        self.assertGreater(limited["error"]["retry_after_ms"], 0)
        self.assertEqual(limited_retry_status, 429)
        self.assertEqual(limited_retry["error"]["code"], "logical_rate_limited")
        self.assertEqual(admission.snapshot()["global_accepted"], 1)

    def test_default_clock_advances_and_short_logical_window_reopens(self) -> None:
        admission = AttendeeRequestAdmissionLedger(
            AttendeeRequestAdmissionPolicy(
                window_ms=25,
                max_fresh_operations_per_context=1,
                max_fresh_operations_global=1,
                max_tracked_contexts=PREVIEW.ATTENDEE_CONTEXT_CAPACITY,
            )
        )
        harness = PREVIEW.AttendeeSecurityHarness(
            bff_authority=BFF_AUTHORITY,
            request_admission=admission,
        )
        try:
            epoch = harness.service.service_epoch
            first_now = harness.clock.now_ms()
            harness.service.bootstrap_context(
                bff_authority=BFF_AUTHORITY,
                expected_epoch=epoch,
                gateway_context_id=context_id("a"),
                idempotency_key=key("clock-a"),
            )
            with self.assertRaises(LaunchSelectionError) as denied:
                harness.service.bootstrap_context(
                    bff_authority=BFF_AUTHORITY,
                    expected_epoch=epoch,
                    gateway_context_id=context_id("b"),
                    idempotency_key=key("clock-b"),
                )
            self.assertEqual(denied.exception.code, "logical_rate_limited")
            time.sleep(0.04)
            accepted = harness.service.bootstrap_context(
                bff_authority=BFF_AUTHORITY,
                expected_epoch=epoch,
                gateway_context_id=context_id("b"),
                idempotency_key=key("clock-b"),
            )
            self.assertGreater(harness.clock.now_ms(), first_now)
            self.assertEqual(accepted["kind"], "attendee_context_bootstrapped")
        finally:
            harness.close()

    def test_close_and_revoke_bypass_saturated_logical_admission(self) -> None:
        admission = AttendeeRequestAdmissionLedger(
            AttendeeRequestAdmissionPolicy(
                window_ms=10_000,
                max_fresh_operations_per_context=3,
                max_fresh_operations_global=3,
                max_tracked_contexts=PREVIEW.ATTENDEE_CONTEXT_CAPACITY,
            )
        )

        def factory(**arguments):
            return PREVIEW.AttendeeSecurityHarness(
                **arguments,
                request_admission=admission,
            )

        with RunningPreview(harness_factory=factory) as running:
            epoch = service_epoch(running)
            _, context, _ = bootstrap(running, epoch, "a", "cleanup-bootstrap")
            actor = context["actor_context"]
            _, options, _ = request(
                running,
                "GET",
                PREVIEW.ATTENDEE_LAUNCH_OPTIONS_PATH,
                headers=context_headers(epoch, actor),
            )
            option = options["options"][0]
            _, granted, _ = request(
                running,
                "POST",
                PREVIEW.ATTENDEE_LAUNCH_SELECTIONS_PATH,
                headers=context_headers(
                    epoch,
                    actor,
                    idempotency="cleanup-exchange",
                ),
                body={
                    "launch_selection_id": option["launch_selection_id"],
                    "notice_version_presented": option["notice_version"],
                },
            )
            _, created, _ = request(
                running,
                "POST",
                PREVIEW._CORE.SESSIONS_PATH,
                headers=context_headers(
                    epoch,
                    actor,
                    idempotency="cleanup-create",
                ),
                body={"launch_grant": granted["launch_grant"]},
            )
            limited_status, limited, _ = request(
                running,
                "POST",
                PREVIEW.ATTENDEE_LAUNCH_SELECTIONS_PATH,
                headers=context_headers(
                    epoch,
                    actor,
                    idempotency="cleanup-limited",
                ),
                body={
                    "launch_selection_id": option["launch_selection_id"],
                    "notice_version_presented": option["notice_version"],
                },
            )
            encoded_reference = quote(created["snapshot"]["reference"], safe="")
            close_status, closed, _ = request(
                running,
                "POST",
                f"{PREVIEW._CORE.SESSION_PATH_PREFIX}{encoded_reference}/close",
                headers=context_headers(
                    epoch,
                    actor,
                    idempotency="cleanup-close",
                    session_authority=created["session_authority"],
                ),
                body={
                    "expected_control_revision": created["snapshot"]["coordinates"][
                        "control_revision"
                    ]
                },
            )
            revoke_status, revoked, _ = request(
                running,
                "DELETE",
                PREVIEW.ATTENDEE_CONTEXT_PREFIX + quote(context_id("a"), safe=""),
                headers=context_headers(
                    epoch,
                    actor,
                    idempotency="cleanup-revoke",
                ),
            )

        self.assertEqual(limited_status, 429)
        self.assertEqual(limited["error"]["code"], "logical_rate_limited")
        self.assertEqual(close_status, 200)
        self.assertEqual(closed["snapshot"]["lifecycle"]["state"], "closed")
        self.assertEqual(revoke_status, 200)
        self.assertEqual(revoked["outcome"], "revoked")

    def test_owner_files_and_startup_identity_are_safe_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "private-token"
            bootstrap_file = Path(directory) / "private-bootstrap.json"
            PREVIEW.initialize_private_files(
                token_file=token_file,
                bootstrap_file=bootstrap_file,
            )
            bearer = PREVIEW._LIFECYCLE.load_preview_bearer_file(token_file)
            bff_authority = PREVIEW.load_bootstrap(bootstrap_file)
            self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(bootstrap_file.stat().st_mode), 0o600)
            self.assertNotEqual(bearer, bff_authority)
            with self.assertRaises(FileExistsError):
                PREVIEW.initialize_private_files(
                    token_file=token_file,
                    bootstrap_file=Path(directory) / "another-bootstrap.json",
                )
            server = PREVIEW.build_preview_server(
                host="127.0.0.1",
                port=0,
                bearer=bearer,
                bff_authority=bff_authority,
                scenario=PREVIEW.SCENARIOS[0],
                contract_root=Path(directory),
                contract_validator=lambda path: path.resolve(),
            )
            try:
                announcement = PREVIEW.startup_announcement(server)
                self.assertNotIn("request_queue_size", server.__dict__)
                self.assertEqual(
                    server.request_queue_size,
                    PREVIEW._LIFECYCLE.LifecyclePreviewServer.request_queue_size,
                )
            finally:
                server.server_close()

        serialized = json.dumps(announcement, sort_keys=True)
        self.assertEqual(announcement["capabilities_enabled"], 0)
        self.assertEqual(announcement["kind"], PREVIEW.PREVIEW_KIND)
        self.assertFalse(announcement["provider_calls"])
        self.assertFalse(announcement["normal_runtime_composition"])
        self.assertEqual(announcement["deployment"]["exposure"], "loopback_only")
        self.assertFalse(announcement["deployment"]["browser_direct_supported"])
        self.assertNotIn(bearer, serialized)
        self.assertNotIn(bff_authority, serialized)
        self.assertNotIn(str(token_file), serialized)
        self.assertNotIn(str(bootstrap_file), serialized)
        self.assertNotIn("actor:", serialized)
        self.assertNotIn("bff-authority:", serialized)

    def test_http_body_connection_and_deadline_limits_remain_enabled(self) -> None:
        with RunningPreview() as running:
            epoch = service_epoch(running)
            oversized_status, oversized, _ = header_only_problem(
                running,
                "POST",
                PREVIEW.ATTENDEE_CONTEXTS_PATH,
                headers=context_headers(epoch, idempotency="oversized"),
                declared_content_length=PREVIEW._CORE.MAX_REQUEST_BODY_BYTES + 1,
            )
            announcement = PREVIEW.startup_announcement(running.server)

            started = time.monotonic()
            connection = socket.create_connection(
                ("127.0.0.1", running.server.server_address[1]),
                timeout=5,
            )
            connection.settimeout(5)
            partial = (
                f"POST {PREVIEW.ATTENDEE_CONTEXTS_PATH} HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                f"Authorization: Bearer {SERVICE_BEARER}\r\n"
                f"X-Dreamland-Expected-Epoch: {epoch}\r\n"
                f"Idempotency-Key: {key('partial-body')}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 100\r\n"
                "Connection: close\r\n\r\n"
                "{"
            ).encode("ascii")
            connection.sendall(partial)
            response = bytearray()
            while True:
                chunk = connection.recv(4_096)
                if not chunk:
                    break
                response.extend(chunk)
            elapsed = time.monotonic() - started
            connection.close()

        self.assertEqual(oversized_status, 413)
        self.assertEqual(oversized["error"]["code"], "request_too_large")
        self.assertIn(b"HTTP/1.1 400", response)
        self.assertLess(elapsed, 3.5)
        security = announcement["security"]
        self.assertEqual(security["connection_limit"], 8)
        self.assertEqual(security["read_timeout_seconds"], 1.0)
        self.assertEqual(security["connection_deadline_seconds"], 3.0)
        self.assertEqual(security["max_request_body_bytes"], 16_384)

    def test_non_loopback_and_malformed_private_authority_fail_before_listen(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "loopback"):
                PREVIEW.build_preview_server(
                    host="0.0.0.0",
                    port=0,
                    bearer=SERVICE_BEARER,
                    bff_authority=BFF_AUTHORITY,
                    scenario=PREVIEW.SCENARIOS[0],
                    contract_root=root,
                    contract_validator=lambda path: path.resolve(),
                )
            with self.assertRaisesRegex(ValueError, "BFF authority"):
                PREVIEW.build_preview_server(
                    host="127.0.0.1",
                    port=0,
                    bearer=SERVICE_BEARER,
                    bff_authority="actor:not-a-bff-authority-0001",
                    scenario=PREVIEW.SCENARIOS[0],
                    contract_root=root,
                    contract_validator=lambda path: path.resolve(),
                )

    def test_once_subprocess_completes_response_and_exits_cleanly(self) -> None:
        contract_root = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.8.0-rc.1"
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "attendee-token"
            bootstrap_file = Path(directory) / "attendee-bootstrap.json"
            initialized = subprocess.run(
                [
                    sys.executable,
                    str(TOOL_PATH),
                    "init",
                    "--token-file",
                    str(token_file),
                    "--bootstrap-file",
                    str(bootstrap_file),
                ],
                cwd=REPOSITORY_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(initialized.stdout), {"created": True})
            self.assertEqual(initialized.stderr, "")
            bearer = PREVIEW._LIFECYCLE.load_preview_bearer_file(token_file)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(TOOL_PATH),
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--token-file",
                    str(token_file),
                    "--bootstrap-file",
                    str(bootstrap_file),
                    "--contract-root",
                    str(contract_root),
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
                announcement["host"],
                announcement["port"],
                timeout=5,
            )
            connection.request(
                "GET",
                PREVIEW._CORE.CAPABILITIES_PATH,
                headers={"Authorization": f"Bearer {bearer}"},
            )
            response = connection.getresponse()
            body = response.read()
            headers = dict(response.getheaders())
            connection.close()
            stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(response.status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Dreamland-Contract-Revision"], "0.8.0-rc.1")
        self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assertEqual(json.loads(body)["kind"], "product_capabilities")
        self.assertEqual(process.returncode, 0, (stdout, stderr))
        self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
