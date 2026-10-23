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
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from dreamland.config import LangfuseSettings
from dreamland.product.execution import (
    ProductExecutionResult,
    ProductOutputChunk,
    ProductOutputStream,
)
from dreamland.product.launch_selection_runtime import LaunchSelectionStateProfileCodec

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_live.py"
PHASE7B_TEST_PATH = REPOSITORY_ROOT / "tests" / "test_product_launch_selection_live.py"
FORGE_CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "world-forge-api" / "v0.1.0-rc.4"
CONTRACT_TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_live_contract.py"
FORGE_BEARER = "forge-preview-bearer:attendee_live_test_00000001"
PRODUCT_BEARER = "preview-bearer:attendee_live_test_00000001"


def _load(path: Path, name: str):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {name}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


TOOL = _load(TOOL_PATH, "dreamland_product_attendee_live_test_tool")
PHASE7B = _load(PHASE7B_TEST_PATH, "dreamland_phase7b_test_helpers_for_attendee")
CONTRACT = _load(
    CONTRACT_TOOL_PATH, "dreamland_product_attendee_live_contract_test_helper"
)


class _CountingExecutor:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def execute(self, request, emit):
        self.calls += 1
        return self.delegate.execute(request, emit)


class _BlockingExecutor:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def execute(self, request, _emit):
        self.started.set()
        while not request.cancelled():
            time.sleep(0.01)
        self.cancelled.set()
        return ProductExecutionResult.interrupted("cancelled")


class _CancellationRaceExecutor:
    """Expose cancellation and publication fences at the live HTTP boundary."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.returned = threading.Event()
        self.calls = 0
        self.initial_emit_accepted: bool | None = None
        self.late_emit_accepted: bool | None = None

    def execute(self, request, emit):
        self.calls += 1
        if self.calls > 1:
            emit(ProductOutputChunk(ProductOutputStream.STDOUT, "service-still-live\n"))
            return ProductExecutionResult.completed_exited(
                exit_status=0,
                usage_tokens=1,
                usage_spend_units=1,
            )

        self.initial_emit_accepted = emit(
            ProductOutputChunk(ProductOutputStream.STDOUT, "before-cancel\n")
        )
        self.started.set()
        while not request.cancelled():
            time.sleep(0.005)
        self.cancelled.set()
        if isinstance(request.provisional_document, dict):
            request.provisional_document["late_cancel_mutation"] = True
        self.late_emit_accepted = emit(
            ProductOutputChunk(ProductOutputStream.STDOUT, "after-cancel\n")
        )
        self.returned.set()
        return ProductExecutionResult.interrupted(
            "cancelled",
            usage_tokens=1,
            usage_spend_units=1,
        )


class _RunningProduct:
    def __init__(
        self, composition: TOOL.LiveAttendeeComposition, contract_root: Path
    ) -> None:
        self.pair = TOOL._build_validated_server_pair(
            host="127.0.0.1",
            forge_port=0,
            stage_port=0,
            forge_bearer=FORGE_BEARER,
            stage_bearer=PRODUCT_BEARER,
            validated_forge_root=FORGE_CONTRACT_ROOT,
            validated_stage_root=contract_root,
            composition=composition,
        )
        self.server = self.pair.stage
        self.harness = self.server.preview
        self.thread = threading.Thread(target=self.server.serve_forever)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.pair.forge.server_close()
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, body=None, headers=None):
        values = {
            "Authorization": f"Bearer {PRODUCT_BEARER}",
            "Accept": "application/json",
            **(headers or {}),
        }
        encoded = None
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":")).encode()
            values["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        connection.request(method, path, encoded, values)
        response = connection.getresponse()
        payload = json.loads(response.read())
        status = response.status
        connection.close()
        return status, payload


class ProductAttendeeLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._contract_temp = tempfile.TemporaryDirectory()
        cls.contract_root = Path(cls._contract_temp.name) / "v0.7.0-rc.4"
        CONTRACT.refresh_contract(cls.contract_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._contract_temp.cleanup()

    def test_import_is_inert_and_provider_free(self) -> None:
        script = r"""
import importlib.util, json, os, sys, threading
from pathlib import Path
from unittest.mock import patch
import dreamland.agent as agent
import dreamland.spiritguide as spiritguide
scratch = Path(sys.argv[2]); os.chdir(scratch)
before = [(t.ident, t.name) for t in threading.enumerate()]
with patch.object(agent, "LangGraphCommandAgent") as command, patch.object(spiritguide, "ModelSpiritguide") as forge:
    spec = importlib.util.spec_from_file_location("attendee_live_inert", Path(sys.argv[1]))
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module
    spec.loader.exec_module(module)
after = [(t.ident, t.name) for t in threading.enumerate()]
print(json.dumps({"calls": [command.call_count, forge.call_count], "threads": before == after, "files": list(scratch.iterdir())}))
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", script, str(TOOL_PATH), directory],
                check=True,
                capture_output=True,
                cwd=REPOSITORY_ROOT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
            )
        self.assertEqual(
            json.loads(result.stdout), {"calls": [0, 0], "threads": True, "files": []}
        )

    def test_parser_and_limits(self) -> None:
        parser = TOOL._parser()
        common = _serve_arguments()
        self.assertEqual(
            parser.parse_args([*common, "--max-commands", "7"]).max_commands, 7
        )
        self.assertIsNone(
            parser.parse_args([*common, "--max-commands", "-1"]).max_commands
        )
        for value in ("0", "-2"):
            with self.assertRaises(SystemExit):
                parser.parse_args([*common, "--max-commands", value])
        for values in ((0, 1, 1), (1, 0, 1), (1, 2, 1), (1, 1, 721)):
            with self.assertRaises(ValueError):
                TOOL._validate_attendee_limits(
                    max_contexts=values[0],
                    context_ttl_minutes=values[1],
                    admission_duration_minutes=values[2],
                )

    def test_generated_world_configuration_is_explicit_and_bounded(self) -> None:
        key = b"g" * 32
        TOOL._validate_generated_world_configuration(
            generated_world_access=False,
            generated_eligibility_key=None,
            attendee_notice_version=TOOL.ATTENDEE_NOTICE_VERSION,
            max_revisions=1,
            max_offerings=1,
            generated_publication_capacity=0,
        )
        TOOL._validate_generated_world_configuration(
            generated_world_access=True,
            generated_eligibility_key=key,
            attendee_notice_version="notice:shared-world-access-v1",
            max_revisions=17,
            max_offerings=17,
            generated_publication_capacity=16,
        )
        invalid = (
            {"generated_world_access": "yes"},
            {
                "generated_world_access": True,
                "generated_eligibility_key": b"short",
                "max_revisions": 5,
                "max_offerings": 5,
                "generated_publication_capacity": 4,
            },
            {
                "generated_world_access": True,
                "generated_eligibility_key": key,
                "max_revisions": 16,
                "max_offerings": 17,
                "generated_publication_capacity": 16,
            },
            {
                "generated_world_access": True,
                "generated_eligibility_key": key,
                "max_revisions": 17,
                "max_offerings": 17,
                "generated_publication_capacity": 17,
            },
            {"generated_eligibility_key": key},
            {"max_revisions": 2},
            {"attendee_notice_version": "unsafe notice"},
        )
        defaults = {
            "generated_world_access": False,
            "generated_eligibility_key": None,
            "attendee_notice_version": TOOL.ATTENDEE_NOTICE_VERSION,
            "max_revisions": 1,
            "max_offerings": 1,
            "generated_publication_capacity": 0,
        }
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(ValueError):
                TOOL._validate_generated_world_configuration(**{**defaults, **override})

    def test_shared_admission_math_preserves_presenter_reservations(self) -> None:
        presenter_reservation = 12_800
        attendee_reservation = 12_800
        limits = TOOL._shared_admission(
            presenter_command_reserved_tokens=presenter_reservation,
            attendee_command_reserved_tokens=attendee_reservation,
        ).limits
        self.assertEqual(limits.max_active_sessions, 5)
        self.assertEqual(
            limits.max_active_sessions - limits.presenter_stage_reserved_sessions, 4
        )
        self.assertEqual(limits.presenter_stage_reserved_sessions, 1)
        self.assertEqual(
            limits.presenter_stage_reserved_logical_jobs,
            TOOL._STAGE_LIVE.MAX_LOGICAL_JOBS,
        )
        self.assertEqual(limits.presenter_stage_reserved_physical_workers, 2)
        self.assertEqual(
            limits.presenter_stage_reserved_token_budget,
            TOOL._STAGE_LIVE.MAX_LOGICAL_JOBS * presenter_reservation,
        )
        self.assertEqual(
            limits.presenter_stage_reserved_spend_budget_units,
            TOOL._STAGE_LIVE.SPEND_BUDGET_UNITS,
        )
        self.assertEqual(
            limits.max_logical_jobs - limits.presenter_stage_reserved_logical_jobs,
            TOOL.ATTENDEE_LOGICAL_JOBS,
        )
        self.assertEqual(
            limits.max_physical_workers
            - limits.presenter_stage_reserved_physical_workers,
            TOOL.ATTENDEE_PHYSICAL_WORKERS,
        )
        self.assertEqual(
            limits.token_budget - limits.presenter_stage_reserved_token_budget,
            TOOL.ATTENDEE_LOGICAL_JOBS * attendee_reservation,
        )

    def test_build_uses_separate_executor_and_disables_attendee_langfuse(self) -> None:
        attendee_executor = object()
        defaults = TOOL.load_settings(
            Path("/definitely-missing-attendee-live-test.ini")
        )
        settings = replace(
            defaults,
            agent=replace(defaults.agent, max_output_tokens=12_800),
            langfuse=LangfuseSettings(
                enabled=True,
                public_key="public",
                secret_key="secret",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            presenter = replace(
                PHASE7B._composition(Path(directory)),
                stage_command_reserved_tokens=settings.agent.max_output_tokens,
            )
            with (
                patch.object(
                    TOOL._LIVE, "build_live_composition", return_value=presenter
                ) as presenter_builder,
                patch.object(
                    TOOL._STAGE_LIVE,
                    "build_live_executor",
                    return_value=(
                        attendee_executor,
                        "provider:model",
                        settings.agent.max_output_tokens,
                    ),
                ) as attendee_builder,
            ):
                composition = TOOL.build_live_composition(
                    settings=settings,
                    actor_context="actor:presenter_test_0001",
                    artifact_root=Path("/unused"),
                    authored_profile_path=Path("/unused.json"),
                    authored_profile_digest="sha256:" + "a" * 64,
                    authored_title="Title",
                    authored_revision_label="Revision",
                    deadline_seconds=1,
                    max_commands=None,
                    max_contexts=2,
                    context_ttl_minutes=1,
                    admission_duration_minutes=2,
                )
        presenter_builder.assert_called_once()
        call = attendee_builder.call_args.kwargs
        self.assertIsInstance(call["state_codec"], LaunchSelectionStateProfileCodec)
        self.assertFalse(call["settings"].langfuse.enabled)
        self.assertEqual(call["settings"].agent.max_output_tokens, 12_800)
        self.assertIsNone(call["settings"].langfuse.public_key)
        self.assertIsNone(call["settings"].langfuse.secret_key)
        self.assertIs(composition.attendee_executor, attendee_executor)
        self.assertIsNotNone(composition.presenter.admission)
        self.assertEqual(
            composition.attendee_command_reserved_tokens,
            settings.agent.max_output_tokens,
        )
        self.assertFalse(composition.generated_world_access)
        self.assertIsNone(composition.generated_eligibility_key)
        self.assertEqual(
            composition.attendee_notice_version,
            TOOL.ATTENDEE_NOTICE_VERSION,
        )
        self.assertEqual(composition.max_revisions, 1)
        self.assertEqual(composition.max_offerings, 1)
        self.assertEqual(composition.generated_publication_capacity, 0)

    def test_invalid_generated_configuration_fails_before_provider_builders(
        self,
    ) -> None:
        defaults = TOOL.load_settings(
            Path("/definitely-missing-attendee-live-test.ini")
        )
        settings = replace(
            defaults,
            agent=replace(defaults.agent, max_output_tokens=12_800),
        )
        with (
            patch.object(TOOL._LIVE, "build_live_composition") as presenter_builder,
            patch.object(TOOL._STAGE_LIVE, "build_live_executor") as attendee_builder,
            self.assertRaisesRegex(ValueError, "eligibility key"),
        ):
            TOOL.build_live_composition(
                settings=settings,
                actor_context="actor:presenter_test_0001",
                artifact_root=Path("/unused"),
                authored_profile_path=Path("/unused.json"),
                authored_profile_digest="sha256:" + "a" * 64,
                authored_title="Title",
                authored_revision_label="Revision",
                deadline_seconds=1,
                max_commands=None,
                max_contexts=2,
                context_ttl_minutes=1,
                admission_duration_minutes=2,
                generated_world_access=True,
                generated_eligibility_key=b"short",
                attendee_notice_version="notice:shared-world-access-v1",
                max_revisions=17,
                max_offerings=17,
                generated_publication_capacity=16,
            )
        presenter_builder.assert_not_called()
        attendee_builder.assert_not_called()

    def test_unbounded_output_fails_before_live_provider_builders(self) -> None:
        defaults = TOOL.load_settings(
            Path("/definitely-missing-attendee-live-test.ini")
        )
        settings = replace(
            defaults,
            agent=replace(defaults.agent, max_output_tokens=None),
        )
        with (
            patch.object(TOOL._LIVE, "build_live_composition") as presenter_builder,
            patch.object(TOOL._STAGE_LIVE, "build_live_executor") as attendee_builder,
            self.assertRaisesRegex(ValueError, "finite positive integer"),
        ):
            TOOL.build_live_composition(
                settings=settings,
                actor_context="actor:presenter_test_0001",
                artifact_root=Path("/unused"),
                authored_profile_path=Path("/unused.json"),
                authored_profile_digest="sha256:" + "a" * 64,
                authored_title="Title",
                authored_revision_label="Revision",
                deadline_seconds=1,
                max_commands=None,
                max_contexts=2,
                context_ttl_minutes=1,
                admission_duration_minutes=2,
            )
        presenter_builder.assert_not_called()
        attendee_builder.assert_not_called()

    def test_combined_token_budget_overflow_fails_before_provider_builders(
        self,
    ) -> None:
        defaults = TOOL.load_settings(
            Path("/definitely-missing-attendee-live-test.ini")
        )
        combined_jobs = TOOL._STAGE_LIVE.MAX_LOGICAL_JOBS + TOOL.ATTENDEE_LOGICAL_JOBS
        overflowing_maximum = ((2**63 - 1) // combined_jobs) + 1
        settings = replace(
            defaults,
            agent=replace(
                defaults.agent,
                max_output_tokens=overflowing_maximum,
            ),
        )
        with (
            patch.object(TOOL._LIVE, "build_live_composition") as presenter_builder,
            patch.object(TOOL._STAGE_LIVE, "build_live_executor") as attendee_builder,
            self.assertRaisesRegex(ValueError, "bounded integer"),
        ):
            TOOL.build_live_composition(
                settings=settings,
                actor_context="actor:presenter_test_0001",
                artifact_root=Path("/unused"),
                authored_profile_path=Path("/unused.json"),
                authored_profile_digest="sha256:" + "a" * 64,
                authored_title="Title",
                authored_revision_label="Revision",
                deadline_seconds=1,
                max_commands=None,
                max_contexts=2,
                context_ttl_minutes=1,
                admission_duration_minutes=2,
            )
        presenter_builder.assert_not_called()
        attendee_builder.assert_not_called()

    def test_attendee_executor_cannot_report_a_smaller_provider_maximum(self) -> None:
        defaults = TOOL.load_settings(
            Path("/definitely-missing-attendee-live-test.ini")
        )
        settings = replace(
            defaults,
            agent=replace(defaults.agent, max_output_tokens=12_800),
        )
        with tempfile.TemporaryDirectory() as directory:
            presenter = replace(
                PHASE7B._composition(Path(directory)),
                stage_command_reserved_tokens=12_800,
            )
            with (
                patch.object(
                    TOOL._LIVE,
                    "build_live_composition",
                    return_value=presenter,
                ),
                patch.object(
                    TOOL._STAGE_LIVE,
                    "build_live_executor",
                    return_value=(object(), "provider:model", 8_192),
                ),
                self.assertRaisesRegex(ValueError, "does not match"),
            ):
                TOOL.build_live_composition(
                    settings=settings,
                    actor_context="actor:presenter_test_0001",
                    artifact_root=Path("/unused"),
                    authored_profile_path=Path("/unused.json"),
                    authored_profile_digest="sha256:" + "a" * 64,
                    authored_title="Title",
                    authored_revision_label="Revision",
                    deadline_seconds=1,
                    max_commands=None,
                    max_contexts=2,
                    context_ttl_minutes=1,
                    admission_duration_minutes=2,
                )

    def test_injected_attendee_under_reservation_fails_before_harness_build(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            composition = replace(
                _composition(
                    PHASE7B._composition(Path(directory)),
                    PHASE7B._live_style_executor(),
                ),
                attendee_command_reserved_tokens=1,
            )
            with self.assertRaisesRegex(ValueError, "does not match"):
                TOOL.LiveAttendeeHarness(composition)

    def test_combined_routing_facade_is_provider_free_until_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            presenter = PHASE7B._composition(Path(directory))
            attendee = _CountingExecutor(PHASE7B._live_style_executor())
            composition = _composition(presenter, attendee)
            with _RunningProduct(composition, self.contract_root) as running:
                status, capabilities = running.request(
                    "GET", "/product/v1/capabilities"
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    capabilities["preview"]["executor"], TOOL.LIVE_EXECUTOR
                )
                self.assertTrue(capabilities["preview"]["provider_calls"])
                self.assertTrue(
                    all(
                        not item["enabled"]
                        for item in capabilities["capabilities"].values()
                    )
                )

                epoch = capabilities["service_epoch"]
                status, context = running.request(
                    "POST",
                    TOOL._ATTENDEE.ATTENDEE_CONTEXTS_PATH,
                    {"gateway_context_id": "gateway-context:" + "a" * 32},
                    {
                        "X-Dreamland-Expected-Epoch": epoch,
                        "Idempotency-Key": "live-bootstrap-0001",
                    },
                )
                self.assertEqual(status, 201, context)
                actor = context["actor_context"]
                authority = {
                    "X-Dreamland-Expected-Epoch": epoch,
                    "X-Dreamland-Actor-Context": actor,
                }
                status, options = running.request(
                    "GET",
                    TOOL._ATTENDEE.ATTENDEE_LAUNCH_OPTIONS_PATH,
                    headers=authority,
                )
                self.assertEqual(status, 200, options)
                option = options["options"][0]
                status, grant = running.request(
                    "POST",
                    TOOL._ATTENDEE.ATTENDEE_LAUNCH_SELECTIONS_PATH,
                    {
                        "launch_selection_id": option["launch_selection_id"],
                        "notice_version_presented": option["notice_version"],
                    },
                    {**authority, "Idempotency-Key": "live-exchange-0001"},
                )
                self.assertEqual(status, 200, grant)
                status, created = running.request(
                    "POST",
                    "/product/v1/sessions",
                    {"launch_grant": grant["launch_grant"]},
                    {**authority, "Idempotency-Key": "live-create-0001"},
                )
                self.assertEqual(status, 201, created)
                self.assertEqual(attendee.calls, 0)
                snapshot = created["snapshot"]
                session_headers = {
                    **authority,
                    "X-Dreamland-Session-Capability": created["session_authority"],
                    "Idempotency-Key": "live-command-0001",
                }
                status, accepted = running.request(
                    "POST",
                    f"/product/v1/sessions/{snapshot['reference']}/commands",
                    {"command": "pwd", "expected_slot_generation": 0},
                    session_headers,
                )
                self.assertEqual(status, 202, accepted)
                deadline = time.monotonic() + 5
                while attendee.calls < 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(attendee.calls, 1)
                status, denied = running.request(
                    "GET",
                    "/product/v1/sessions/current",
                    headers={
                        "X-Dreamland-Expected-Epoch": epoch,
                        "X-Dreamland-Actor-Context": "actor:unknown_authority_0001",
                    },
                )
                self.assertEqual(status, 403, denied)
                self.assertEqual(denied["error"]["code"], "authority_denied")

    def test_http_cancellation_replays_and_fences_late_attendee_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = _CancellationRaceExecutor()
            composition = _composition(
                PHASE7B._composition(Path(directory)),
                executor,
            )
            with _RunningProduct(composition, self.contract_root) as running:
                status, capabilities = running.request(
                    "GET", "/product/v1/capabilities"
                )
                self.assertEqual(status, 200, capabilities)
                epoch = capabilities["service_epoch"]
                status, context = running.request(
                    "POST",
                    TOOL._ATTENDEE.ATTENDEE_CONTEXTS_PATH,
                    {"gateway_context_id": "gateway-context:" + "c" * 32},
                    {
                        "X-Dreamland-Expected-Epoch": epoch,
                        "Idempotency-Key": "cancel-bootstrap-0001",
                    },
                )
                self.assertEqual(status, 201, context)
                actor = context["actor_context"]
                actor_headers = {
                    "X-Dreamland-Expected-Epoch": epoch,
                    "X-Dreamland-Actor-Context": actor,
                }
                status, options = running.request(
                    "GET",
                    TOOL._ATTENDEE.ATTENDEE_LAUNCH_OPTIONS_PATH,
                    headers=actor_headers,
                )
                self.assertEqual(status, 200, options)
                option = options["options"][0]
                status, grant = running.request(
                    "POST",
                    TOOL._ATTENDEE.ATTENDEE_LAUNCH_SELECTIONS_PATH,
                    {
                        "launch_selection_id": option["launch_selection_id"],
                        "notice_version_presented": option["notice_version"],
                    },
                    {
                        **actor_headers,
                        "Idempotency-Key": "cancel-exchange-0001",
                    },
                )
                self.assertEqual(status, 200, grant)
                status, created = running.request(
                    "POST",
                    "/product/v1/sessions",
                    {"launch_grant": grant["launch_grant"]},
                    {
                        **actor_headers,
                        "Idempotency-Key": "cancel-create-0001",
                    },
                )
                self.assertEqual(status, 201, created)
                snapshot = created["snapshot"]
                reference = snapshot["reference"]
                encoded_reference = quote(reference, safe="")
                session_headers = {
                    **actor_headers,
                    "X-Dreamland-Session-Capability": created["session_authority"],
                }
                status, accepted = running.request(
                    "POST",
                    f"/product/v1/sessions/{encoded_reference}/commands",
                    {"command": "pwd", "expected_slot_generation": 0},
                    {
                        **session_headers,
                        "Idempotency-Key": "cancel-command-0001",
                    },
                )
                self.assertEqual(status, 202, accepted)
                self.assertTrue(executor.started.wait(2))

                command_id = accepted["command_id"]
                slot_generation = accepted["slot_generation"]
                cancel_path = (
                    f"/product/v1/sessions/{encoded_reference}/commands/"
                    f"{quote(command_id, safe='')}/cancel"
                )
                cancel_body = {"slot_generation": slot_generation}
                cancel_headers = {
                    **session_headers,
                    "Idempotency-Key": "cancel-request-0001",
                }
                status, cancellation = running.request(
                    "POST",
                    cancel_path,
                    cancel_body,
                    cancel_headers,
                )
                self.assertEqual(status, 200, cancellation)
                self.assertEqual(cancellation["outcome"], "cancel_requested")
                self.assertFalse(cancellation["replayed"])
                self.assertEqual(cancellation["command_id"], command_id)
                self.assertEqual(cancellation["slot_generation"], slot_generation)

                status, replay = running.request(
                    "POST",
                    cancel_path,
                    cancel_body,
                    cancel_headers,
                )
                self.assertEqual(status, 200, replay)
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["outcome"], "cancel_requested")
                self.assertEqual(replay["command_id"], command_id)
                self.assertEqual(replay["slot_generation"], slot_generation)
                self.assertTrue(executor.cancelled.wait(2))
                self.assertTrue(executor.returned.wait(2))

                command_path = (
                    f"/product/v1/sessions/{encoded_reference}/commands/"
                    f"{quote(command_id, safe='')}"
                )
                deadline = time.monotonic() + 5
                terminal = None
                while time.monotonic() < deadline:
                    status, candidate = running.request(
                        "GET",
                        command_path,
                        headers=session_headers,
                    )
                    self.assertEqual(status, 200, candidate)
                    if candidate["terminal"]:
                        terminal = candidate
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(terminal)
                assert terminal is not None
                self.assertEqual(terminal["command"]["status"], "interrupted")
                self.assertEqual(
                    terminal["command"]["result"]["reason"],
                    "cancelled",
                )
                self.assertTrue(executor.initial_emit_accepted)
                self.assertFalse(executor.late_emit_accepted)

                session_path = f"/product/v1/sessions/{encoded_reference}"
                status, projected = running.request(
                    "GET",
                    session_path,
                    headers=session_headers,
                )
                self.assertEqual(status, 200, projected)
                self.assertEqual(
                    projected["snapshot"]["coordinates"]["accepted_state_revision"],
                    0,
                )
                self.assertEqual(
                    projected["snapshot"]["command_slot"]["state"],
                    "available",
                )
                status, transcript = running.request(
                    "GET",
                    f"{session_path}/transcript",
                    headers=session_headers,
                )
                self.assertEqual(status, 200, transcript)
                transcript_text = "".join(
                    entry["text"] for entry in transcript["entries"]
                )
                self.assertIn("before-cancel\n", transcript_text)
                self.assertNotIn("after-cancel\n", transcript_text)
                status, events = running.request(
                    "GET",
                    f"{session_path}/events",
                    headers=session_headers,
                )
                self.assertEqual(status, 200, events)
                command_events = [
                    event
                    for event in events["events"]
                    if event.get("command_id") == command_id
                ]
                self.assertEqual(
                    [
                        event["type"]
                        for event in command_events
                        if event["type"] == "command.interrupted"
                    ],
                    ["command.interrupted"],
                )
                self.assertFalse(
                    any(event["type"] == "state.accepted" for event in command_events)
                )

                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    admission = running.harness.stage_service.admission.snapshot()
                    if not admission.physical_workers and not admission.unsettled_jobs:
                        break
                    time.sleep(0.01)
                self.assertEqual(admission.physical_workers, 0)
                self.assertEqual(admission.unsettled_jobs, 0)

                status, next_accepted = running.request(
                    "POST",
                    f"{session_path}/commands",
                    {
                        "command": "whoami",
                        "expected_slot_generation": slot_generation,
                    },
                    {
                        **session_headers,
                        "Idempotency-Key": "after-cancel-command-0001",
                    },
                )
                self.assertEqual(status, 202, next_accepted)
                next_path = (
                    f"{session_path}/commands/"
                    f"{quote(next_accepted['command_id'], safe='')}"
                )
                deadline = time.monotonic() + 5
                next_terminal = None
                while time.monotonic() < deadline:
                    status, candidate = running.request(
                        "GET",
                        next_path,
                        headers=session_headers,
                    )
                    self.assertEqual(status, 200, candidate)
                    if candidate["terminal"]:
                        next_terminal = candidate
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(next_terminal)
                assert next_terminal is not None
                self.assertEqual(next_terminal["command"]["status"], "completed")
                self.assertEqual(executor.calls, 2)
                admission = running.harness.stage_service.admission.snapshot()
                self.assertEqual(admission.physical_workers, 0)
                self.assertEqual(admission.unsettled_jobs, 0)

    def test_attendee_reservation_reaches_core_launch_and_shared_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            presenter = PHASE7B._composition(Path(directory))
            composition = _composition(
                presenter,
                _CountingExecutor(PHASE7B._live_style_executor()),
            )
            harness = TOOL.LiveAttendeeHarness(composition)
            try:
                reservation = composition.attendee_command_reserved_tokens
                self.assertEqual(
                    harness.attendee_service._core._core_policy.reserved_tokens_per_command,
                    reservation,
                )
                offering = next(
                    iter(harness.attendee_service._registry._offerings.values())
                )
                self.assertEqual(
                    offering.limits["command_reserved_tokens"], reservation
                )
                admission = harness.stage_service.admission.limits
                self.assertEqual(
                    admission.token_budget
                    - admission.presenter_stage_reserved_token_budget,
                    TOOL.ATTENDEE_LOGICAL_JOBS * reservation,
                )
                self.assertEqual(
                    admission.presenter_stage_reserved_token_budget,
                    TOOL._STAGE_LIVE.MAX_LOGICAL_JOBS
                    * presenter.stage_command_reserved_tokens,
                )
            finally:
                harness.close()

    def test_generated_world_opt_in_exposes_aligned_bounded_composition(self) -> None:
        eligibility_key = b"private-generated-eligibility-key" + b"x" * 8
        notice_version = "notice:shared-world-access-v1"
        with tempfile.TemporaryDirectory() as directory:
            composition = replace(
                _composition(
                    PHASE7B._composition(Path(directory)),
                    PHASE7B._live_style_executor(),
                ),
                generated_world_access=True,
                generated_eligibility_key=eligibility_key,
                attendee_notice_version=notice_version,
                max_revisions=17,
                max_offerings=17,
                generated_publication_capacity=16,
                max_contexts=1,
            )
            self.assertNotIn(eligibility_key.decode(), repr(composition))
            harness = TOOL.LiveAttendeeHarness(composition)
            try:
                self.assertIs(
                    harness.attendee_registry,
                    harness.attendee_service._registry,
                )
                self.assertIs(harness.attendee_core, harness.attendee_service._core)
                self.assertTrue(harness.attendee_service._generated_world_access)
                self.assertEqual(
                    harness.attendee_service._generated_eligibility_key,
                    eligibility_key,
                )
                self.assertTrue(harness.attendee_core._generated_world_access)
                self.assertEqual(harness.attendee_registry._policy.max_revisions, 17)
                self.assertEqual(harness.attendee_registry._policy.max_offerings, 17)
                self.assertEqual(
                    harness.attendee_registry._policy.max_token_history,
                    19,
                )
                self.assertEqual(harness.attendee_core._policy.max_revisions, 17)
                self.assertEqual(harness.generated_publication_capacity, 16)
                authored = next(iter(harness.attendee_registry._offerings.values()))
                self.assertEqual(authored.notice_version, notice_version)
            finally:
                harness.close()

    def test_shutdown_revokes_attendee_authority_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = TOOL.LiveAttendeeHarness(
                _composition(
                    PHASE7B._composition(Path(directory)),
                    PHASE7B._live_style_executor(),
                )
            )
            epoch = harness.attendee_service.service_epoch
            context = harness.attendee_service.bootstrap_context(
                bff_authority=harness.bff_authority,
                expected_epoch=epoch,
                gateway_context_id="gateway-context:" + "z" * 32,
                idempotency_key="shutdown-bootstrap-0001",
            )
            self.assertTrue(
                harness.attendee_service.owns_actor_authority(context["actor_context"])
            )
            harness.close()
            harness.close()
            with self.assertRaises(Exception) as caught:
                harness.attendee_service.options(
                    actor_authority=context["actor_context"], expected_epoch=epoch
                )
            self.assertEqual(caught.exception.code, "authority_denied")

    def test_cutoff_watcher_fences_blocked_work_without_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = _BlockingExecutor()
            presenter = PHASE7B._composition(Path(directory))
            now = TOOL._STAGE_LIVE._utc_ms()
            composition = replace(
                _composition(presenter, executor),
                admission_opens_at_ms=now - 100,
                admission_closes_at_ms=now + 1_000,
            )
            harness = TOOL.LiveAttendeeHarness(composition)
            try:
                service = harness.attendee_service
                self.assertEqual(
                    harness._cutoff_deadline_ms,
                    service._admission_closes_deadline_ms,
                )
                epoch = service.service_epoch
                context = service.bootstrap_context(
                    bff_authority=harness.bff_authority,
                    expected_epoch=epoch,
                    gateway_context_id="gateway-context:" + "w" * 32,
                    idempotency_key="watcher-bootstrap-0001",
                )
                actor = context["actor_context"]
                option = service.options(
                    actor_authority=actor,
                    expected_epoch=epoch,
                )["options"][0]
                grant = service.exchange(
                    actor_authority=actor,
                    expected_epoch=epoch,
                    idempotency_key="watcher-exchange-0001",
                    launch_selection_id=option["launch_selection_id"],
                    notice_version_presented=option["notice_version"],
                )
                created = service.create(
                    actor_authority=actor,
                    expected_epoch=epoch,
                    idempotency_key="watcher-create-0001",
                    launch_grant=grant["launch_grant"],
                )
                snapshot = created["snapshot"]
                service.submit_command(
                    actor_authority=actor,
                    session_authority=created["session_authority"],
                    reference=snapshot["reference"],
                    expected_epoch=epoch,
                    idempotency_key="watcher-command-0001",
                    expected_slot_generation=0,
                    command="pwd",
                )
                self.assertTrue(executor.started.wait(2))
                # No facade/HTTP call and no manual expire_due follows. The
                # explicit live watcher alone must cross the admission cutoff.
                self.assertTrue(executor.cancelled.wait(3))
                deadline = time.monotonic() + 2
                while (
                    harness.stage_service.admission.snapshot().active_sessions
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                admission = harness.stage_service.admission.snapshot()
                self.assertEqual(admission.active_sessions, 0)
                self.assertEqual(admission.physical_workers, 0)
                self.assertEqual(admission.unsettled_jobs, 0)
            finally:
                harness.close()
            self.assertFalse(harness._cutoff_thread.is_alive())

    def test_startup_identity_capacity_and_redaction(self) -> None:
        presenter = SimpleNamespace(
            actor_context="actor:secret_presenter_0001",
            actor_expires_at_ms=1,
            authored_revision=SimpleNamespace(
                digest="sha256:" + "d" * 64,
                revision_id="world-revision:sha256:" + "d" * 64,
            ),
            authored_title="secret title",
            authored_revision_label="secret path label",
            forge_deadline_seconds=2,
            forge_max_output_tokens=3,
            forge_model="provider:forge",
            max_commands=None,
            stage_max_output_tokens=4,
            stage_command_reserved_tokens=8_192,
            stage_model="provider:stage",
        )
        composition = TOOL.LiveAttendeeComposition(
            presenter=presenter,
            attendee_executor=object(),
            attendee_model="provider:attendee",
            attendee_max_output_tokens=5,
            attendee_command_reserved_tokens=8_192,
            max_contexts=2,
            context_ttl_ms=60_000,
            admission_opens_at_ms=1,
            admission_closes_at_ms=61_000,
        )
        pair = TOOL.LiveServerPair(
            SimpleNamespace(server_address=("127.0.0.1", 1)),
            SimpleNamespace(server_address=("127.0.0.1", 2)),
        )
        announcement = TOOL.startup_announcement(pair, composition)
        attendee = announcement["attendee_sessions"]
        self.assertEqual(announcement["contract_revision"], "0.7.0-rc.4")
        self.assertEqual(attendee["executor"], "development-live-attendee-langgraph-v1")
        self.assertEqual(attendee["standard_active_session_capacity"], 4)
        self.assertEqual(attendee["command_reserved_tokens"], 8_192)
        self.assertFalse(attendee["generated_world_eligibility"])
        self.assertEqual(attendee["token_budget"], TOOL.ATTENDEE_LOGICAL_JOBS * 8_192)
        self.assertEqual(
            announcement["presenter_stage"]["command_reserved_tokens"], 8_192
        )
        self.assertEqual(
            announcement["presenter_stage"]["token_budget"],
            TOOL._STAGE_LIVE.MAX_LOGICAL_JOBS * 8_192,
        )
        self.assertFalse(attendee["external_tracing"])
        rendered = json.dumps(announcement, sort_keys=True)
        for secret in (
            presenter.actor_context,
            presenter.authored_revision.digest,
            presenter.authored_title,
            presenter.authored_revision_label,
            "api_key",
            "bff-authority",
        ):
            self.assertNotIn(secret, rendered)

    def test_contract_dependency_rejects_before_server_construction(self) -> None:
        forge, product = TOOL.validate_contract_dependency(
            forge_contract_root=FORGE_CONTRACT_ROOT,
            stage_contract_root=self.contract_root,
        )
        self.assertEqual(forge, FORGE_CONTRACT_ROOT.resolve())
        self.assertEqual(product, self.contract_root.resolve())
        with patch.object(
            TOOL, "validate_contract_root", side_effect=ValueError("bad rc4")
        ):
            with self.assertRaisesRegex(ValueError, "bad rc4"):
                TOOL.validate_contract_dependency(
                    forge_contract_root=FORGE_CONTRACT_ROOT,
                    stage_contract_root=self.contract_root,
                )


def _composition(presenter, attendee_executor):
    now = TOOL._STAGE_LIVE._utc_ms()
    presenter_reservation = presenter.stage_command_reserved_tokens
    attendee_reservation = TOOL._STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND
    return TOOL.LiveAttendeeComposition(
        presenter=replace(
            presenter,
            admission=TOOL._shared_admission(
                presenter_command_reserved_tokens=presenter_reservation,
                attendee_command_reserved_tokens=attendee_reservation,
            ),
        ),
        attendee_executor=attendee_executor,
        attendee_model="provider-free:attendee-test",
        attendee_max_output_tokens=1,
        attendee_command_reserved_tokens=attendee_reservation,
        max_contexts=2,
        context_ttl_ms=60 * 60_000,
        admission_opens_at_ms=now - 1_000,
        admission_closes_at_ms=now + 60 * 60_000,
    )


def _serve_arguments() -> list[str]:
    return [
        "serve",
        "--forge-token-file",
        "/tmp/forge.token",
        "--stage-token-file",
        "/tmp/stage.token",
        "--bootstrap-file",
        "/tmp/bootstrap.json",
        "--forge-contract-root",
        str(FORGE_CONTRACT_ROOT),
        "--contract-root",
        "/tmp/v0.7.0-rc.4",
        "--artifact-root",
        "/tmp/artifacts",
        "--authored-profile-path",
        "/tmp/authored.json",
        "--authored-profile-digest",
        "sha256:" + "a" * 64,
    ]


if __name__ == "__main__":
    unittest.main()
