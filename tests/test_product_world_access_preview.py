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
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import jsonschema

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_world_access_preview.py"
WORLD_CONTRACT_TOOL_PATH = (
    REPOSITORY_ROOT / "tools" / "product_world_access_contract.py"
)
PRODUCT_CONTRACT_TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_contract.py"
SERVICE_BEARER = "private-world-access-preview-service-bearer"


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


PREVIEW = _load_module(TOOL_PATH, "dreamland_product_world_access_preview_test_tool")
WORLD_CONTRACT = _load_module(
    WORLD_CONTRACT_TOOL_PATH,
    "dreamland_product_world_access_contract_preview_test_tool",
)
PRODUCT_CONTRACT = _load_module(
    PRODUCT_CONTRACT_TOOL_PATH,
    "dreamland_product_attendee_contract_world_access_preview_test_tool",
)


class RunningPreview:
    def __init__(
        self,
        world_root: Path,
        product_root: Path,
        *,
        result_identity: object | None = None,
    ) -> None:
        if result_identity is None:
            self.server = PREVIEW.build_preview_server(
                host="127.0.0.1",
                port=0,
                bearer=SERVICE_BEARER,
                scenario=PREVIEW.SCENARIOS[0],
                contract_root=world_root,
                product_contract_root=product_root,
            )
        else:
            PREVIEW._ATTENDEE._configure_transport()
            self.server = PREVIEW.WorldAccessPreviewServer(
                ("127.0.0.1", 0),
                bearer=SERVICE_BEARER,
                scenario=PREVIEW.SCENARIOS[0],
                product_contract_root=product_root,
                world_access_contract_root=world_root,
                result_identity=result_identity,
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


def actor_headers(
    epoch: str,
    actor: str,
    *,
    key: str | None = None,
    session_authority: str | None = None,
) -> dict[str, str]:
    headers = {
        "X-Dreamland-Expected-Epoch": epoch,
        "X-Dreamland-Actor-Context": actor,
    }
    if key is not None:
        headers["Idempotency-Key"] = f"world-access:{key}"
    if session_authority is not None:
        headers["X-Dreamland-Session-Capability"] = session_authority
    return headers


def bootstrap(running: RunningPreview, epoch: str, label: str = "a") -> dict:
    status, payload, _headers = request(
        running,
        "POST",
        PREVIEW._ATTENDEE.ATTENDEE_CONTEXTS_PATH,
        headers={
            "X-Dreamland-Expected-Epoch": epoch,
            "Idempotency-Key": f"phase8a-bootstrap-{label}",
        },
        body={"gateway_context_id": "gateway-context:" + label * 32},
    )
    if status != 201:
        raise AssertionError(f"attendee bootstrap failed: {status} {payload}")
    return payload


def promote(running: RunningPreview, epoch: str, key: str = "phase8a-promote"):
    return request(
        running,
        "POST",
        PREVIEW.PROMOTIONS_PATH,
        headers=actor_headers(epoch, PREVIEW.OPERATOR_CONTEXT, key=key),
        body={"forge_job_reference": PREVIEW.FORGE_JOB_REFERENCE},
    )


def reveal(
    running: RunningPreview,
    epoch: str,
    promotion: dict,
    key: str = "phase8a-reveal",
):
    return request(
        running,
        "POST",
        f"{PREVIEW.PROMOTIONS_PATH}/{quote(promotion['promotion_id'], safe='')}/reveal",
        headers=actor_headers(epoch, PREVIEW.OPERATOR_CONTEXT, key=key),
        body={"expected_access_record_id": promotion["access_record_id"]},
    )


def validate_response(openapi: dict, name: str, value: object) -> None:
    schema = {**openapi, "$ref": f"#/components/schemas/{name}"}
    jsonschema.Draft202012Validator(schema).validate(value)


def world_access_side_effects(running: RunningPreview) -> dict[str, int]:
    """Snapshot authority-bearing state while ignoring request-local metadata."""
    harness = running.server.preview
    return {
        "access_records": len(harness.access._records_by_id),
        "access_promotions": len(harness.access._current_by_promotion),
        "access_idempotency": len(harness.access._idempotency),
        "access_token_history": len(harness.access._used_token_sha256s),
        "source_resolutions": harness.selected_source.resolve_calls,
        "pending_publications": len(harness.world_catalog._pending_by_access_record),
        "active_publications": len(harness.world_catalog._generated_by_access_record),
        "catalog_sequence": harness.world_catalog._catalog_sequence,
        "registry_revisions": len(harness.attendee_registry._revisions),
        "registry_offerings": len(harness.attendee_registry._offerings),
        "registry_grants": len(harness.attendee_registry._grants),
        "registry_token_history": len(harness.attendee_registry._used_token_sha256s),
        "registry_grant_serial": harness.attendee_registry._launch_grant_serial,
        "registry_exchange_idempotency": len(
            harness.attendee_registry._offering_idempotency
        ),
        "attendee_exchange_idempotency": len(
            harness.attendee_service._catalog_exchange_idempotency
        ),
        "core_dynamic_grants": len(harness.attendee_core._attendee_dynamic_grants),
        "core_revisions": len(harness.attendee_core._revisions),
    }


class ProductWorldAccessPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        temporary = Path(cls._temporary.name)
        cls.world_root = temporary / "world" / "v0.1.0-rc.3"
        cls.product_root = temporary / "product" / "v0.7.0-rc.3"
        WORLD_CONTRACT.refresh_contract(cls.world_root)
        PRODUCT_CONTRACT.refresh_contract(cls.product_root)
        cls.openapi = json.loads(
            (cls.world_root / "openapi.json").read_text(encoding="utf-8")
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_import_is_inert_and_all_capability_evidence_is_false(self) -> None:
        code = f"""
import importlib.util
import json
import pathlib
import threading
before = [(thread.ident, thread.name) for thread in threading.enumerate()]
path = pathlib.Path({str(TOOL_PATH)!r})
spec = importlib.util.spec_from_file_location("world_access_preview_inert", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
after = [(thread.ident, thread.name) for thread in threading.enumerate()]
print(json.dumps({{
    "before": before,
    "after": after,
    "revision": module.WORLD_ACCESS_CONTRACT_REVISION,
    "provider_calls": module._CORE.PREVIEW_PROVIDER_CALLS,
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
        self.assertEqual(payload["revision"], "0.1.0-rc.3")
        self.assertFalse(payload["provider_calls"])

    def test_default_result_identity_is_immutable_and_preserves_rc3_wire(
        self,
    ) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            identity_status, identity, identity_headers = request(
                running,
                "GET",
                PREVIEW.IDENTITY_PATH,
            )
            server_identity = running.server.result_identity

        self.assertEqual(identity_status, 200)
        self.assertIs(
            server_identity,
            PREVIEW.DEFAULT_WORLD_ACCESS_RESULT_IDENTITY,
        )
        with self.assertRaises(AttributeError):
            server_identity.provider_calls = True
        self.assertEqual(
            identity["contract"],
            {
                "name": PREVIEW.WORLD_ACCESS_CONTRACT_NAME,
                "revision": "0.1.0-rc.3",
                "compatibility": PREVIEW.WORLD_ACCESS_CONTRACT_COMPATIBILITY,
            },
        )
        self.assertEqual(
            identity["dependencies"],
            {
                "product_api": "0.7.0-rc.3",
                "world_forge_api": "0.1.0-rc.4",
            },
        )
        self.assertEqual(
            identity["preview"],
            {
                "kind": PREVIEW.PREVIEW_KIND,
                "provider_calls": False,
                "live_forge_evidence": False,
                "live_capability_evidence": False,
                "loopback_only": True,
                "process_local": True,
                "capabilities_enabled": [],
            },
        )
        self.assertEqual(
            identity_headers["X-Dreamland-Contract-Revision"],
            "0.1.0-rc.3",
        )

    def test_injected_result_identity_is_local_to_world_access_routes(self) -> None:
        live_identity = PREVIEW.WorldAccessResultIdentity(
            contract_name=PREVIEW.WORLD_ACCESS_CONTRACT_NAME,
            contract_revision="0.1.0-rc.4",
            contract_compatibility=PREVIEW.WORLD_ACCESS_CONTRACT_COMPATIBILITY,
            product_contract_revision="0.7.0-rc.4",
            world_forge_contract_revision="0.1.0-rc.4",
            preview_kind="live_generated_world_access_development",
            provider_calls=True,
            live_forge_evidence=True,
            live_capability_evidence=False,
            live_attendee_execution=True,
        )
        with (
            RunningPreview(
                self.world_root,
                self.product_root,
                result_identity=live_identity,
            ) as live_running,
            RunningPreview(self.world_root, self.product_root) as preview_running,
        ):
            live_status, live_result, live_headers = request(
                live_running,
                "GET",
                PREVIEW.IDENTITY_PATH,
            )
            epoch = live_result["service_epoch"]
            promotion_status, promotion, promotion_headers = promote(
                live_running,
                epoch,
                "injected-result-identity",
            )
            product_status, product_result, product_headers = request(
                live_running,
                "GET",
                "/product/v1/capabilities",
            )
            preview_status, preview_result, preview_headers = request(
                preview_running,
                "GET",
                PREVIEW.IDENTITY_PATH,
            )

        self.assertEqual(live_status, 200)
        self.assertEqual(live_result["contract"]["revision"], "0.1.0-rc.4")
        self.assertEqual(
            live_result["dependencies"],
            {
                "product_api": "0.7.0-rc.4",
                "world_forge_api": "0.1.0-rc.4",
            },
        )
        self.assertEqual(
            live_result["preview"],
            {
                "kind": "live_generated_world_access_development",
                "provider_calls": True,
                "live_forge_evidence": True,
                "live_attendee_execution": True,
                "live_capability_evidence": False,
                "loopback_only": True,
                "process_local": True,
                "capabilities_enabled": [],
            },
        )
        self.assertEqual(
            live_headers["X-Dreamland-Contract-Revision"],
            "0.1.0-rc.4",
        )
        self.assertEqual(promotion_status, 201)
        self.assertEqual(promotion["contract"]["revision"], "0.1.0-rc.4")
        self.assertEqual(promotion["preview"], live_result["preview"])
        self.assertEqual(
            promotion_headers["X-Dreamland-Contract-Revision"],
            "0.1.0-rc.4",
        )

        self.assertEqual(product_status, 200)
        self.assertEqual(product_result["contract"]["revision"], "0.7.0-rc.3")
        self.assertFalse(product_result["preview"]["provider_calls"])
        self.assertEqual(
            product_headers["X-Dreamland-Contract-Revision"],
            "0.7.0-rc.3",
        )

        self.assertEqual(preview_status, 200)
        self.assertEqual(preview_result["contract"]["revision"], "0.1.0-rc.3")
        self.assertNotIn("live_attendee_execution", preview_result["preview"])
        self.assertEqual(
            preview_headers["X-Dreamland-Contract-Revision"],
            "0.1.0-rc.3",
        )

    def test_world_error_vocabulary_exactly_matches_contract(self) -> None:
        self.assertEqual(PREVIEW._WORLD_SAFE_ERRORS, WORLD_CONTRACT.SAFE_ERRORS)

    def test_cli_defaults_to_checked_rc3_world_contract(self) -> None:
        arguments = PREVIEW._parser().parse_args(
            [
                "--token",
                SERVICE_BEARER,
                "--product-contract-root",
                str(self.product_root),
            ]
        )

        self.assertEqual(
            arguments.contract_root,
            REPOSITORY_ROOT / "contracts/world-access-api/v0.1.0-rc.3",
        )

    def test_domain_harness_is_explicit_provider_free_and_hidden_by_default(
        self,
    ) -> None:
        harness = PREVIEW.WorldAccessPreviewHarness(PREVIEW.SCENARIOS[0])
        self.addCleanup(harness.close)
        epoch = harness.access.service_epoch
        context = harness.attendee_service.bootstrap_context(
            bff_authority=harness.bff_authority,
            expected_epoch=epoch,
            gateway_context_id="gateway-context:" + "d" * 32,
            idempotency_key="domain-bootstrap",
        )
        catalog = harness.world_catalog.catalog(
            actor_authority=context["actor_context"],
            expected_epoch=epoch,
        )

        self.assertEqual(harness.selected_source.resolve_calls, 0)
        self.assertEqual(
            [item["origin"]["kind"] for item in catalog["offerings"]],
            ["authored_profile_snapshot"],
        )
        self.assertEqual(
            {item["notice_version"] for item in catalog["offerings"]},
            {PREVIEW.NOTICE_VERSION},
        )
        self.assertFalse(PREVIEW._CORE.PREVIEW_PROVIDER_CALLS)

    def test_cohosted_catalog_uses_one_notice_and_both_origins_exchange_it(
        self,
    ) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            _, identity, _ = request(running, "GET", PREVIEW.IDENTITY_PATH)
            epoch = identity["service_epoch"]
            attendee = bootstrap(running, epoch, "n")["actor_context"]
            _, hidden_catalog, _ = request(
                running,
                "GET",
                PREVIEW.CATALOG_PATH,
                headers=actor_headers(epoch, attendee),
            )
            _, promotion, _ = promote(running, epoch, "notice-promote")
            reveal(running, epoch, promotion, "notice-reveal")
            _, mixed_catalog, _ = request(
                running,
                "GET",
                PREVIEW.CATALOG_PATH,
                headers=actor_headers(epoch, attendee),
            )

            stale_results = []
            exact_results = []
            for index, offering in enumerate(mixed_catalog["offerings"]):
                retained_before = len(running.server.preview.attendee_registry._grants)
                stale = request(
                    running,
                    "POST",
                    PREVIEW.LAUNCH_SELECTIONS_PATH,
                    headers=actor_headers(
                        epoch,
                        attendee,
                        key=f"stale-notice-{index}",
                    ),
                    body={
                        "offering_id": offering["offering_id"],
                        "offering_revision": offering["offering_revision"],
                        "notice_version_presented": "notice:stale-world-access-v0",
                    },
                )
                retained_after = len(running.server.preview.attendee_registry._grants)
                stale_results.append((stale, retained_before, retained_after))
                exact_results.append(
                    request(
                        running,
                        "POST",
                        PREVIEW.LAUNCH_SELECTIONS_PATH,
                        headers=actor_headers(
                            epoch,
                            attendee,
                            key=f"exact-notice-{index}",
                        ),
                        body={
                            "offering_id": offering["offering_id"],
                            "offering_revision": offering["offering_revision"],
                            "notice_version_presented": PREVIEW.NOTICE_VERSION,
                        },
                    )
                )

        self.assertEqual(len(hidden_catalog["offerings"]), 1)
        self.assertEqual(
            {item["notice_version"] for item in hidden_catalog["offerings"]},
            {PREVIEW.NOTICE_VERSION},
        )
        self.assertEqual(
            {item["origin"]["kind"] for item in mixed_catalog["offerings"]},
            {"authored_profile_snapshot", "world_forge_selection"},
        )
        self.assertEqual(
            {item["notice_version"] for item in mixed_catalog["offerings"]},
            {PREVIEW.NOTICE_VERSION},
        )
        for (
            (status, problem, _headers),
            retained_before,
            retained_after,
        ) in stale_results:
            self.assertEqual(status, 409)
            self.assertEqual(problem["error"]["code"], "notice_version_mismatch")
            self.assertEqual(retained_after, retained_before)
            validate_response(self.openapi, "SafeErrorEnvelope", problem)
        for offering, (status, selection, _headers) in zip(
            mixed_catalog["offerings"], exact_results, strict=True
        ):
            self.assertEqual(status, 200)
            self.assertEqual(selection["selection"], offering)
            self.assertEqual(
                selection["selection"]["notice_version"], PREVIEW.NOTICE_VERSION
            )
            validate_response(self.openapi, "LaunchSelectionResponse", selection)

    def test_executable_clock_is_current_and_orders_access_timestamps(self) -> None:
        evaluation_started_ms = time.time_ns() // 1_000_000
        with RunningPreview(self.world_root, self.product_root) as running:
            _, identity, _ = request(running, "GET", PREVIEW.IDENTITY_PATH)
            epoch = identity["service_epoch"]
            _, promotion, _ = promote(running, epoch, "wall-clock-promote")
            running.server.preview.clock.advance(1)
            _, revealed, _ = reveal(running, epoch, promotion, "wall-clock-reveal")
        evaluation_finished_ms = time.time_ns() // 1_000_000

        prepared_at = datetime.fromisoformat(promotion["created_at"])
        revealed_at = datetime.fromisoformat(revealed["changed_at"])
        expires_at = datetime.fromisoformat(revealed["expires_at"])
        self.assertGreaterEqual(
            int(prepared_at.timestamp() * 1_000), evaluation_started_ms
        )
        self.assertLessEqual(
            int(prepared_at.timestamp() * 1_000), evaluation_finished_ms
        )
        self.assertLessEqual(prepared_at, revealed_at)
        self.assertLess(revealed_at, expires_at)
        self.assertGreater(int(expires_at.timestamp() * 1_000), evaluation_finished_ms)

    def test_world_clock_is_deterministically_injected_before_inherited_services(
        self,
    ) -> None:
        initial_ms = 1_900_000_000_000
        clock = PREVIEW._LIFECYCLE.MutablePreviewClock(initial_ms)
        harness = PREVIEW.WorldAccessPreviewHarness(
            PREVIEW.SCENARIOS[0],
            clock=clock,
        )
        self.addCleanup(harness.close)
        epoch = harness.access.service_epoch
        promotion = harness.access.promote(
            actor_authority=PREVIEW.OPERATOR_CONTEXT,
            expected_epoch=epoch,
            idempotency_key="deterministic-clock-promote",
            job_reference=PREVIEW.FORGE_JOB_REFERENCE,
        )
        clock.advance(17)
        revealed = harness.world_catalog.reveal(
            actor_authority=PREVIEW.OPERATOR_CONTEXT,
            expected_epoch=epoch,
            idempotency_key="deterministic-clock-reveal",
            promotion_id=promotion["promotion_id"],
            expected_access_record_id=promotion["access_record_id"],
        )

        self.assertIs(harness.clock, clock)
        self.assertEqual(
            promotion["created_at"],
            datetime.fromtimestamp(initial_ms / 1_000, UTC).isoformat(
                timespec="milliseconds"
            ),
        )
        self.assertEqual(
            revealed["changed_at"],
            datetime.fromtimestamp((initial_ms + 17) / 1_000, UTC).isoformat(
                timespec="milliseconds"
            ),
        )

    def test_startup_clock_uses_monotonic_elapsed_time_and_supports_advance(
        self,
    ) -> None:
        monotonic_ms = [7_000]
        clock = PREVIEW.StartupAnchoredPreviewClock(
            wall_time_ms=lambda: 10_000,
            monotonic_ms=lambda: monotonic_ms[0],
        )

        self.assertEqual(clock.now_ms(), 10_000)
        monotonic_ms[0] += 23
        self.assertEqual(clock.now_ms(), 10_023)
        clock.advance(11)
        self.assertEqual(clock.now_ms(), 10_034)
        with self.assertRaisesRegex(ValueError, "backwards"):
            clock.advance(-1)

    def test_standalone_attendee_preview_keeps_its_original_notice(self) -> None:
        harness = PREVIEW._ATTENDEE.AttendeePreviewHarness(
            PREVIEW._ATTENDEE.SCENARIOS[0]
        )
        self.addCleanup(harness.close)
        epoch = harness.attendee_service.service_epoch
        context = harness.attendee_service.bootstrap_context(
            bff_authority=harness.bff_authority,
            expected_epoch=epoch,
            gateway_context_id="gateway-context:" + "p" * 32,
            idempotency_key="standalone-notice-bootstrap",
        )
        options = harness.attendee_service.options(
            actor_authority=context["actor_context"],
            expected_epoch=epoch,
        )

        self.assertEqual(len(options["options"]), 1)
        self.assertEqual(
            options["options"][0]["notice_version"],
            PREVIEW._ATTENDEE.ATTENDEE_NOTICE_VERSION,
        )
        self.assertNotEqual(
            PREVIEW._ATTENDEE.ATTENDEE_NOTICE_VERSION,
            PREVIEW.NOTICE_VERSION,
        )

    def test_complete_hidden_reveal_catalog_grant_and_product_session_flow(
        self,
    ) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            identity_status, identity, identity_headers = request(
                running, "GET", PREVIEW.IDENTITY_PATH
            )
            epoch = identity["service_epoch"]
            context = bootstrap(running, epoch)
            attendee = context["actor_context"]
            before_status, before, _ = request(
                running,
                "GET",
                PREVIEW.CATALOG_PATH,
                headers=actor_headers(epoch, attendee),
            )
            promotion_status, promotion, promotion_headers = promote(running, epoch)
            hidden_status, hidden, _ = request(
                running,
                "GET",
                f"{PREVIEW.PROMOTIONS_PATH}/{quote(promotion['promotion_id'], safe='')}",
                headers=actor_headers(epoch, PREVIEW.OPERATOR_CONTEXT),
            )
            still_hidden_status, still_hidden, _ = request(
                running,
                "GET",
                PREVIEW.CATALOG_PATH,
                headers=actor_headers(epoch, attendee),
            )
            reveal_status, revealed, _ = reveal(running, epoch, promotion)
            catalog_status, catalog, _ = request(
                running,
                "GET",
                PREVIEW.CATALOG_PATH,
                headers=actor_headers(epoch, attendee),
            )
            generated = next(
                item
                for item in catalog["offerings"]
                if item["origin"]["kind"] == "world_forge_selection"
            )
            exchange_body = {
                "offering_id": generated["offering_id"],
                "offering_revision": generated["offering_revision"],
                "notice_version_presented": generated["notice_version"],
            }
            exchange_headers = actor_headers(
                epoch,
                attendee,
                key="phase8a-exchange",
            )
            exchange_status, grant, _ = request(
                running,
                "POST",
                PREVIEW.LAUNCH_SELECTIONS_PATH,
                headers=exchange_headers,
                body=exchange_body,
            )
            exchange_replay_status, grant_replay, _ = request(
                running,
                "POST",
                PREVIEW.LAUNCH_SELECTIONS_PATH,
                headers=exchange_headers,
                body=exchange_body,
            )
            create_body = {"launch_grant": grant["launch_grant"]}
            create_headers = actor_headers(
                epoch,
                attendee,
                key="phase8a-create",
            )
            create_status, created, _ = request(
                running,
                "POST",
                "/product/v1/sessions",
                headers=create_headers,
                body=create_body,
            )
            create_replay_status, created_replay, _ = request(
                running,
                "POST",
                "/product/v1/sessions",
                headers=create_headers,
                body=create_body,
            )
            current_status, current, _ = request(
                running,
                "GET",
                "/product/v1/sessions/current",
                headers=actor_headers(epoch, attendee),
            )
            reference = created["snapshot"]["reference"]
            session_authority = created["session_authority"]
            command_status, accepted, _ = request(
                running,
                "POST",
                f"/product/v1/sessions/{quote(reference, safe='')}/commands",
                headers=actor_headers(
                    epoch,
                    attendee,
                    key="phase8a-command",
                    session_authority=session_authority,
                ),
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
                    headers=actor_headers(
                        epoch,
                        attendee,
                        session_authority=session_authority,
                    ),
                )
                if result["terminal"]:
                    break
                if time.monotonic() >= deadline:
                    self.fail("generated-world preview command did not become terminal")

            dynamic_revisions = running.server.preview.attendee_core._revisions
            exact_revision = promotion["revision"]["revision_id"]

        self.assertEqual(identity_status, 200)
        self.assertEqual(
            identity_headers["X-Dreamland-Contract-Revision"], "0.1.0-rc.3"
        )
        self.assertEqual(identity["preview"]["capabilities_enabled"], [])
        self.assertFalse(identity["preview"]["provider_calls"])
        self.assertFalse(identity["preview"]["live_forge_evidence"])
        self.assertEqual(before_status, 200)
        self.assertEqual(len(before["offerings"]), 1)
        self.assertEqual(promotion_status, 201)
        self.assertEqual(
            promotion_headers["X-Dreamland-Contract-Revision"], "0.1.0-rc.3"
        )
        self.assertEqual(hidden_status, 200)
        self.assertEqual(hidden["state"], "hidden")
        self.assertEqual(still_hidden_status, 200)
        self.assertEqual(len(still_hidden["offerings"]), 1)
        self.assertEqual(reveal_status, 200)
        self.assertEqual(revealed["state"], "revealed")
        self.assertEqual(catalog_status, 200)
        self.assertEqual(exchange_status, 200)
        self.assertEqual(exchange_replay_status, 200)
        self.assertEqual(create_status, 201)
        self.assertEqual(create_replay_status, 201)
        self.assertEqual(current_status, 200)
        self.assertEqual(command_status, 202)
        self.assertEqual(result_status, 200)
        self.assertEqual(result["command"]["result"]["outcome"], "completed")
        self.assertFalse(grant["replayed"])
        self.assertTrue(grant_replay["replayed"])
        self.assertEqual(grant["launch_grant"], grant_replay["launch_grant"])
        self.assertEqual(grant["grant_expires_at"], grant_replay["grant_expires_at"])
        self.assertEqual(grant["selection"], grant_replay["selection"])
        self.assertFalse(created["replayed"])
        self.assertTrue(created_replay["replayed"])
        self.assertEqual(
            created["session_authority"], created_replay["session_authority"]
        )
        self.assertEqual(created["snapshot"], created_replay["snapshot"])
        self.assertEqual(current["kind"], "current_session")
        self.assertEqual(current["session_authority"], created["session_authority"])
        self.assertEqual(current["snapshot"]["reference"], reference)
        recovery_correlations = {
            "exchange": (
                grant["service_epoch"],
                grant["launch_binding_sha256"],
            ),
            "exchange_replay": (
                grant_replay["service_epoch"],
                grant_replay["launch_binding_sha256"],
            ),
            "create": (
                created["service_epoch"],
                created["snapshot"]["launch_binding"]["sha256"],
            ),
            "create_replay": (
                created_replay["service_epoch"],
                created_replay["snapshot"]["launch_binding"]["sha256"],
            ),
            "current": (
                current["service_epoch"],
                current["snapshot"]["launch_binding"]["sha256"],
            ),
        }
        self.assertEqual(
            set(recovery_correlations.values()),
            {(epoch, grant["launch_binding_sha256"])},
        )
        self.assertIn(exact_revision, dynamic_revisions)
        validate_response(self.openapi, "IdentityResponse", identity)
        validate_response(self.openapi, "PromotionResponse", promotion)
        validate_response(self.openapi, "PromotionResponse", revealed)
        validate_response(self.openapi, "CatalogResponse", catalog)
        validate_response(self.openapi, "LaunchSelectionResponse", grant)
        validate_response(self.openapi, "LaunchSelectionResponse", grant_replay)

    def test_world_catalog_has_no_private_revision_or_authority_leakage(self) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            _, identity, _ = request(running, "GET", PREVIEW.IDENTITY_PATH)
            epoch = identity["service_epoch"]
            attendee = bootstrap(running, epoch, "b")["actor_context"]
            _, promotion, _ = promote(running, epoch, "leak-promote")
            reveal(running, epoch, promotion, "leak-reveal")
            status, catalog, _ = request(
                running,
                "GET",
                PREVIEW.CATALOG_PATH,
                headers=actor_headers(epoch, attendee),
            )

        serialized = json.dumps(catalog, sort_keys=True)
        self.assertEqual(status, 200)
        for forbidden in (
            "launch_selection_id",
            "launch_grant",
            "access_record_id",
            "promotion_id",
            "revision_id",
            "sha256",
            "profile_path",
            "world_state",
            PREVIEW.OPERATOR_CONTEXT,
            PREVIEW.FORGE_JOB_REFERENCE,
        ):
            self.assertNotIn(forbidden, serialized)

    def test_published_attendee_options_remain_authored_only_after_reveal(self) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            product_status, product_identity, product_headers = request(
                running, "GET", "/product/v1/capabilities"
            )
            epoch = product_identity["service_epoch"]
            attendee = bootstrap(running, epoch, "c")["actor_context"]
            _, promotion, _ = promote(running, epoch, "legacy-promote")
            reveal(running, epoch, promotion, "legacy-reveal")
            options_status, options, _ = request(
                running,
                "GET",
                PREVIEW._ATTENDEE.ATTENDEE_LAUNCH_OPTIONS_PATH,
                headers=actor_headers(epoch, attendee),
            )

        self.assertEqual(product_status, 200)
        self.assertEqual(product_headers["X-Dreamland-Contract-Revision"], "0.7.0-rc.3")
        self.assertEqual(options_status, 200)
        self.assertEqual(len(options["options"]), 1)
        self.assertEqual(
            options["options"][0]["origin"],
            {"kind": "authored_profile_snapshot"},
        )

    def test_actor_classes_are_denied_cross_role_without_side_effects(self) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            _, identity, _ = request(running, "GET", PREVIEW.IDENTITY_PATH)
            epoch = identity["service_epoch"]
            attendee = bootstrap(running, epoch, "r")["actor_context"]

            before_promote = world_access_side_effects(running)
            denied_promote = request(
                running,
                "POST",
                PREVIEW.PROMOTIONS_PATH,
                headers=actor_headers(
                    epoch,
                    attendee,
                    key="wrong-role-attendee-promote",
                ),
                body={"forge_job_reference": PREVIEW.FORGE_JOB_REFERENCE},
            )
            self.assertEqual(world_access_side_effects(running), before_promote)

            promotion_status, promotion, _ = promote(
                running,
                epoch,
                "wrong-role-control-promote",
            )
            self.assertEqual(promotion_status, 201)
            hidden_state = world_access_side_effects(running)
            promotion_path = (
                f"{PREVIEW.PROMOTIONS_PATH}/{quote(promotion['promotion_id'], safe='')}"
            )
            denied_status = request(
                running,
                "GET",
                promotion_path,
                headers=actor_headers(epoch, attendee),
            )
            denied_reveal = request(
                running,
                "POST",
                promotion_path + "/reveal",
                headers=actor_headers(
                    epoch,
                    attendee,
                    key="wrong-role-attendee-reveal",
                ),
                body={"expected_access_record_id": promotion["access_record_id"]},
            )
            self.assertEqual(world_access_side_effects(running), hidden_state)

            catalog_status, catalog, _ = request(
                running,
                "GET",
                PREVIEW.CATALOG_PATH,
                headers=actor_headers(epoch, attendee),
            )
            self.assertEqual(catalog_status, 200)
            authored = next(
                value
                for value in catalog["offerings"]
                if value["origin"]["kind"] == "authored_profile_snapshot"
            )
            before_attendee_routes = world_access_side_effects(running)
            denied_catalog = request(
                running,
                "GET",
                PREVIEW.CATALOG_PATH,
                headers=actor_headers(epoch, PREVIEW.OPERATOR_CONTEXT),
            )
            denied_exchange = request(
                running,
                "POST",
                PREVIEW.LAUNCH_SELECTIONS_PATH,
                headers=actor_headers(
                    epoch,
                    PREVIEW.OPERATOR_CONTEXT,
                    key="wrong-role-operator-exchange",
                ),
                body={
                    "offering_id": authored["offering_id"],
                    "offering_revision": authored["offering_revision"],
                    "notice_version_presented": authored["notice_version"],
                },
            )
            self.assertEqual(
                world_access_side_effects(running),
                before_attendee_routes,
            )

        for status, problem, _headers in (
            denied_promote,
            denied_status,
            denied_reveal,
            denied_catalog,
            denied_exchange,
        ):
            self.assertEqual(status, 403)
            self.assertEqual(problem["error"]["code"], "authority_denied")
            validate_response(self.openapi, "SafeErrorEnvelope", problem)

    def test_authored_catalog_exchange_succeeds_and_replays_exactly(self) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            _, identity, _ = request(running, "GET", PREVIEW.IDENTITY_PATH)
            epoch = identity["service_epoch"]
            attendee = bootstrap(running, epoch, "s")["actor_context"]
            catalog_status, catalog, _ = request(
                running,
                "GET",
                PREVIEW.CATALOG_PATH,
                headers=actor_headers(epoch, attendee),
            )
            authored = next(
                value
                for value in catalog["offerings"]
                if value["origin"]["kind"] == "authored_profile_snapshot"
            )
            body = {
                "offering_id": authored["offering_id"],
                "offering_revision": authored["offering_revision"],
                "notice_version_presented": authored["notice_version"],
            }
            exchange_headers = actor_headers(
                epoch,
                attendee,
                key="authored-success-replay",
            )
            first_status, first, _ = request(
                running,
                "POST",
                PREVIEW.LAUNCH_SELECTIONS_PATH,
                headers=exchange_headers,
                body=body,
            )
            replay_status, replay, _ = request(
                running,
                "POST",
                PREVIEW.LAUNCH_SELECTIONS_PATH,
                headers=exchange_headers,
                body=body,
            )

        self.assertEqual(catalog_status, 200)
        self.assertEqual(first_status, 200)
        self.assertEqual(replay_status, 200)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["launch_grant"], replay["launch_grant"])
        self.assertEqual(first["grant_expires_at"], replay["grant_expires_at"])
        self.assertEqual(first["selection"], replay["selection"])
        self.assertEqual(
            first["selection"]["origin"],
            {"kind": "authored_profile_snapshot"},
        )
        validate_response(self.openapi, "LaunchSelectionResponse", first)
        validate_response(self.openapi, "LaunchSelectionResponse", replay)

    def test_stale_catalog_revision_fails_closed_and_reserves_rejection(self) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            _, identity, _ = request(running, "GET", PREVIEW.IDENTITY_PATH)
            epoch = identity["service_epoch"]
            attendee = bootstrap(running, epoch, "t")["actor_context"]
            _, catalog, _ = request(
                running,
                "GET",
                PREVIEW.CATALOG_PATH,
                headers=actor_headers(epoch, attendee),
            )
            authored = next(
                value
                for value in catalog["offerings"]
                if value["origin"]["kind"] == "authored_profile_snapshot"
            )
            revision = authored["offering_revision"]
            stale_revision = revision[:-1] + ("0" if revision[-1] != "0" else "1")
            exchange_headers = actor_headers(
                epoch,
                attendee,
                key="stale-authored-revision",
            )
            stale_body = {
                "offering_id": authored["offering_id"],
                "offering_revision": stale_revision,
                "notice_version_presented": authored["notice_version"],
            }
            first_status, first, _ = request(
                running,
                "POST",
                PREVIEW.LAUNCH_SELECTIONS_PATH,
                headers=exchange_headers,
                body=stale_body,
            )
            replay_status, replay, _ = request(
                running,
                "POST",
                PREVIEW.LAUNCH_SELECTIONS_PATH,
                headers=exchange_headers,
                body=stale_body,
            )
            conflict_status, conflict, _ = request(
                running,
                "POST",
                PREVIEW.LAUNCH_SELECTIONS_PATH,
                headers=exchange_headers,
                body={
                    **stale_body,
                    "offering_revision": authored["offering_revision"],
                },
            )
            retained_grants = len(running.server.preview.attendee_registry._grants)
            core_grants = len(
                running.server.preview.attendee_core._attendee_dynamic_grants
            )

        self.assertEqual(first_status, 404)
        self.assertEqual(replay_status, 404)
        self.assertEqual(first["error"]["code"], "launch_selection_unavailable")
        self.assertEqual(replay["error"], first["error"])
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
        self.assertEqual(retained_grants, 0)
        self.assertEqual(core_grants, 0)
        for problem in (first, replay, conflict):
            validate_response(self.openapi, "SafeErrorEnvelope", problem)

    def test_expired_catalog_offering_fails_closed_over_http(self) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            _, identity, _ = request(running, "GET", PREVIEW.IDENTITY_PATH)
            epoch = identity["service_epoch"]
            attendee = bootstrap(running, epoch, "u")["actor_context"]
            harness = running.server.preview
            authored_record = next(
                offering
                for offering in harness.attendee_registry._offerings.values()
                if offering.origin
                is PREVIEW.LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
            )
            short_id = "offering:phase8a-short-authored"
            harness.attendee_registry.register_offering(
                PREVIEW.LaunchOfferingSeed(
                    offering_id=short_id,
                    launch_selection_id=(
                        "launch-selection:phase8a_short_authored_http_0001"
                    ),
                    revision_id=authored_record.revision_id,
                    origin=authored_record.origin,
                    title=authored_record.title,
                    source_label=authored_record.source_label,
                    revision_label=authored_record.revision_label,
                    role=authored_record.role,
                    allowed_operations=authored_record.allowed_operations,
                    limits=authored_record.limits,
                    policy_id=authored_record.policy_id,
                    notice_version=authored_record.notice_version,
                    expires_at_ms=harness.clock.now_ms() + 60_000,
                    launch_grant_ttl_ms=50,
                )
            )
            catalog_status, catalog, _ = request(
                running,
                "GET",
                PREVIEW.CATALOG_PATH,
                headers=actor_headers(epoch, attendee),
            )
            short = next(
                value
                for value in catalog["offerings"]
                if value["offering_id"] == short_id
            )
            harness.clock.advance(60_001)
            exchange_headers = actor_headers(
                epoch,
                attendee,
                key="expired-authored-offering",
            )
            body = {
                "offering_id": short["offering_id"],
                "offering_revision": short["offering_revision"],
                "notice_version_presented": short["notice_version"],
            }
            first_status, first, _ = request(
                running,
                "POST",
                PREVIEW.LAUNCH_SELECTIONS_PATH,
                headers=exchange_headers,
                body=body,
            )
            replay_status, replay, _ = request(
                running,
                "POST",
                PREVIEW.LAUNCH_SELECTIONS_PATH,
                headers=exchange_headers,
                body=body,
            )
            retained_grants = len(harness.attendee_registry._grants)

        self.assertEqual(catalog_status, 200)
        self.assertEqual(first_status, 404)
        self.assertEqual(replay_status, 404)
        self.assertEqual(first["error"]["code"], "launch_selection_unavailable")
        self.assertEqual(replay["error"], first["error"])
        self.assertEqual(retained_grants, 0)
        validate_response(self.openapi, "SafeErrorEnvelope", first)
        validate_response(self.openapi, "SafeErrorEnvelope", replay)

    def test_rejected_promotion_is_replayed_and_changed_body_conflicts(self) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            _, identity, _ = request(running, "GET", PREVIEW.IDENTITY_PATH)
            epoch = identity["service_epoch"]
            headers = actor_headers(
                epoch,
                PREVIEW.OPERATOR_CONTEXT,
                key="rejected-promotion-key",
            )
            first_status, first, first_headers = request(
                running,
                "POST",
                PREVIEW.PROMOTIONS_PATH,
                headers=headers,
                body={"forge_job_reference": "forge-reference:missing-preview"},
            )
            replay_status, replay, _ = request(
                running,
                "POST",
                PREVIEW.PROMOTIONS_PATH,
                headers=headers,
                body={"forge_job_reference": "forge-reference:missing-preview"},
            )
            conflict_status, conflict, _ = request(
                running,
                "POST",
                PREVIEW.PROMOTIONS_PATH,
                headers=headers,
                body={"forge_job_reference": PREVIEW.FORGE_JOB_REFERENCE},
            )

        self.assertEqual(first_status, 404)
        self.assertEqual(replay_status, 404)
        self.assertEqual(first["error"]["code"], "selection_unavailable")
        self.assertEqual(replay["error"], first["error"])
        self.assertNotEqual(replay["request_id"], first["request_id"])
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
        self.assertEqual(first_headers["Cache-Control"], "no-store")
        validate_response(self.openapi, "SafeErrorEnvelope", first)

    def test_one_character_contract_key_replays_promotion(self) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            _, identity, _ = request(running, "GET", PREVIEW.IDENTITY_PATH)
            epoch = identity["service_epoch"]
            headers = {
                "X-Dreamland-Expected-Epoch": epoch,
                "X-Dreamland-Actor-Context": PREVIEW.OPERATOR_CONTEXT,
                "Idempotency-Key": "x",
            }
            first_status, first, _ = request(
                running,
                "POST",
                PREVIEW.PROMOTIONS_PATH,
                headers=headers,
                body={"forge_job_reference": PREVIEW.FORGE_JOB_REFERENCE},
            )
            replay_status, replay, _ = request(
                running,
                "POST",
                PREVIEW.PROMOTIONS_PATH,
                headers=headers,
                body={"forge_job_reference": PREVIEW.FORGE_JOB_REFERENCE},
            )

        self.assertEqual(first_status, 201)
        self.assertEqual(replay_status, 201)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["promotion_id"], replay["promotion_id"])
        self.assertEqual(first["access_record_id"], replay["access_record_id"])
        validate_response(self.openapi, "PromotionResponse", first)
        validate_response(self.openapi, "PromotionResponse", replay)

    def test_contract_valid_non_actor_authority_reaches_uniform_denial(self) -> None:
        with RunningPreview(self.world_root, self.product_root) as running:
            _, identity, _ = request(running, "GET", PREVIEW.IDENTITY_PATH)
            epoch = identity["service_epoch"]
            status, problem, _ = request(
                running,
                "POST",
                PREVIEW.PROMOTIONS_PATH,
                headers={
                    "X-Dreamland-Expected-Epoch": epoch,
                    "X-Dreamland-Actor-Context": (
                        "opaque-authority:contract-valid-0001"
                    ),
                    "Idempotency-Key": "y",
                },
                body={"forge_job_reference": PREVIEW.FORGE_JOB_REFERENCE},
            )

        self.assertEqual(status, 403)
        self.assertEqual(problem["error"]["code"], "authority_denied")
        validate_response(self.openapi, "SafeErrorEnvelope", problem)

    def test_once_subprocess_writes_complete_world_identity_before_exit(self) -> None:
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
                str(self.world_root),
                "--product-contract-root",
                str(self.product_root),
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
            PREVIEW.IDENTITY_PATH,
            headers={"Authorization": f"Bearer {SERVICE_BEARER}"},
        )
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        connection.close()
        stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(response.status, 200)
        self.assertEqual(headers["X-Dreamland-Contract-Revision"], "0.1.0-rc.3")
        self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assertEqual(json.loads(body)["kind"], "world_access_identity")
        self.assertFalse(announcement["provider_calls"])
        self.assertFalse(announcement["live_forge_evidence"])
        self.assertEqual(announcement["capabilities_enabled"], 0)
        self.assertEqual(announcement["executor"], PREVIEW.PREVIEW_EXECUTOR)
        self.assertNotIn(SERVICE_BEARER, json.dumps(announcement, sort_keys=True))
        self.assertEqual(process.returncode, 0, (stdout, stderr))
        self.assertEqual(stderr, "")

    def test_rejects_non_loopback_and_invalid_world_contract(self) -> None:
        for host in ("0.0.0.0", "::1"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "loopback"):
                    PREVIEW.build_preview_server(
                        host=host,
                        port=0,
                        bearer=SERVICE_BEARER,
                        scenario=PREVIEW.SCENARIOS[0],
                        contract_root=self.world_root,
                        product_contract_root=self.product_root,
                    )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "contract"):
                PREVIEW.build_preview_server(
                    host="127.0.0.1",
                    port=0,
                    bearer=SERVICE_BEARER,
                    scenario=PREVIEW.SCENARIOS[0],
                    contract_root=Path(directory),
                    product_contract_root=self.product_root,
                )


if __name__ == "__main__":
    unittest.main()
