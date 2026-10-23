from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools/product_world_access_live.py"
PHASE7B_HELPERS_PATH = REPOSITORY_ROOT / "tests/test_product_launch_selection_live.py"
FORGE_CONTRACT_ROOT = REPOSITORY_ROOT / "contracts/world-forge-api/v0.1.0-rc.4"
PRODUCT_CONTRACT_ROOT = REPOSITORY_ROOT / "contracts/product-api/v0.7.0-rc.4"
WORLD_ACCESS_CONTRACT_ROOT = REPOSITORY_ROOT / "contracts/world-access-api/v0.1.0-rc.4"
FORGE_BEARER = "forge-preview-bearer:phase8b_test_00000001"
PRODUCT_BEARER = "preview-bearer:phase8b_test_00000001"
PRESENTER_CONTEXT = "actor:phase8b_presenter_test_00000001"
OPERATOR_CONTEXT = "actor:phase8b_operator_test_00000001"
ELIGIBILITY_KEY = b"phase8b-test-eligibility-key-0001"


def _load(path: Path, name: str):
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


TOOL = _load(TOOL_PATH, "dreamland_product_world_access_live_test_tool")
PHASE7B = _load(
    PHASE7B_HELPERS_PATH,
    "dreamland_phase7b_helpers_for_world_access_live",
)


class _CountingExecutor:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def execute(self, request, emit):
        self.calls += 1
        return self.delegate.execute(request, emit)


class _CountingGenerator:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def generate(self, instruction):
        self.calls += 1
        return self.delegate.generate(instruction)


class _RunningLiveWorldAccess:
    def __init__(self, root: Path, *, max_commands: int | None = None) -> None:
        self.composition, self.forge_generator, self.attendee_executor = (
            _fake_live_composition(root, max_commands=max_commands)
        )
        self.pair = TOOL._build_validated_server_pair(
            host="127.0.0.1",
            forge_port=0,
            stage_port=0,
            forge_bearer=FORGE_BEARER,
            stage_bearer=PRODUCT_BEARER,
            validated_forge_root=FORGE_CONTRACT_ROOT,
            validated_product_root=PRODUCT_CONTRACT_ROOT,
            validated_world_access_root=WORLD_ACCESS_CONTRACT_ROOT,
            composition=self.composition,
            operator_actor_context=OPERATOR_CONTEXT,
        )
        self.threads = (
            threading.Thread(target=self.pair.forge.serve_forever),
            threading.Thread(target=self.pair.stage.serve_forever),
        )

    def __enter__(self):
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.pair.forge.shutdown()
        self.pair.stage.shutdown()
        for thread in self.threads:
            thread.join(timeout=5)
        self.pair.stage.server_close()
        self.pair.forge.server_close()

    def forge_request(self, method, path, body=None, *, headers=None):
        values = {
            "Authorization": f"Bearer {FORGE_BEARER}",
            "Accept": "application/json",
            **(headers or {}),
        }
        return _request(
            self.pair.forge.server_address[1],
            method,
            path,
            body,
            values,
        )

    def stage_request(self, method, path, body=None, *, headers=None):
        values = {
            "Authorization": f"Bearer {PRODUCT_BEARER}",
            "Accept": "application/json",
            **(headers or {}),
        }
        return _request(
            self.pair.stage.server_address[1],
            method,
            path,
            body,
            values,
        )

    def build_world(self, *, key: str) -> tuple[str, dict]:
        status, identity, _ = self.forge_request("GET", "/forge/v1/identity")
        if status != 200:
            raise AssertionError(identity)
        epoch = identity["service_epoch"]
        authority = {
            "X-Dreamland-Expected-Epoch": epoch,
            "X-Dreamland-Actor-Context": PRESENTER_CONTEXT,
        }
        status, recipes, _ = self.forge_request(
            "GET",
            "/forge/v1/recipes",
            headers=authority,
        )
        if status != 200:
            raise AssertionError(recipes)
        recipe = recipes["recipes"][0]
        status, prepared, _ = self.forge_request(
            "POST",
            "/forge/v1/preparations",
            {
                "recipe_id": recipe["recipe_id"],
                "recipe_revision": recipe["recipe_revision"],
                "answers": PHASE7B._answers(),
            },
            headers=authority,
        )
        if status != 201:
            raise AssertionError(prepared)
        status, accepted, _ = self.forge_request(
            "POST",
            "/forge/v1/builds",
            {"preparation_reference": prepared["preparation_reference"]},
            headers={**authority, "Idempotency-Key": key},
        )
        if status != 201:
            raise AssertionError(accepted)
        reference = accepted["reference"]
        deadline = time.monotonic() + 5
        while True:
            status, selected, _ = self.forge_request(
                "GET",
                "/forge/v1/builds/" + quote(reference, safe=""),
                headers=authority,
            )
            if status != 200:
                raise AssertionError(selected)
            if selected["terminal"]:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("fake-live Forge build did not finish")
            time.sleep(0.01)
        if selected["lifecycle"]["state"] != "selected":
            raise AssertionError(selected)
        return reference, selected


class ProductWorldAccessLiveTests(unittest.TestCase):
    def test_fresh_import_is_inert_and_constructs_no_provider_or_runtime(self) -> None:
        script = r"""
import importlib.util
import json
import os
import socket
import socketserver
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import dreamland.agent as agent
import dreamland.spiritguide as spiritguide

tool_path = Path(sys.argv[1])
scratch = Path(sys.argv[2])
os.chdir(scratch)
before = sorted((thread.ident, thread.name) for thread in threading.enumerate())
with (
    patch.object(agent, "LangGraphCommandAgent") as command_agent,
    patch.object(spiritguide, "ModelSpiritguide") as forge_agent,
    patch.object(socket, "socket") as socket_constructor,
    patch.object(socketserver.TCPServer, "__init__") as listener_constructor,
    patch.object(threading.Thread, "start") as thread_start,
):
    specification = importlib.util.spec_from_file_location(
        "dreamland_world_access_live_inert_check", tool_path
    )
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    calls = {
        "command": command_agent.call_count,
        "forge": forge_agent.call_count,
        "listener": listener_constructor.call_count,
        "socket": socket_constructor.call_count,
        "thread_start": thread_start.call_count,
    }
after = sorted((thread.ident, thread.name) for thread in threading.enumerate())
print(json.dumps({
    "calls": calls,
    "entries": sorted(path.name for path in scratch.iterdir()),
    "threads_unchanged": before == after,
}))
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
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["calls"],
            {
                "command": 0,
                "forge": 0,
                "listener": 0,
                "socket": 0,
                "thread_start": 0,
            },
        )
        self.assertEqual(payload["entries"], [])
        self.assertTrue(payload["threads_unchanged"])

    def test_bootstrap_files_are_owner_only_distinct_and_repr_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forge_path = root / "forge.token"
            stage_path = root / "product.token"
            bootstrap_path = root / "bootstrap.json"
            TOOL.initialize_private_files(
                forge_token_file=forge_path,
                stage_token_file=stage_path,
                bootstrap_file=bootstrap_path,
            )
            bootstrap = TOOL.load_bootstrap(bootstrap_path)
            forge_bearer = TOOL._FORGE_TRANSPORT.load_bearer(forge_path)
            stage_bearer = TOOL._LIFECYCLE.load_preview_bearer_file(stage_path)
            modes = {
                stat.S_IMODE(path.stat().st_mode)
                for path in (forge_path, stage_path, bootstrap_path)
            }

        self.assertEqual(modes, {0o600})
        self.assertNotEqual(forge_bearer, stage_bearer)
        self.assertNotEqual(
            bootstrap.presenter_actor_context,
            bootstrap.operator_actor_context,
        )
        rendered = repr(bootstrap)
        self.assertNotIn(bootstrap.presenter_actor_context, rendered)
        self.assertNotIn(bootstrap.operator_actor_context, rendered)

    def test_contract_dependencies_are_exactly_pinned(self) -> None:
        roots = TOOL.validate_contract_dependencies(
            forge_contract_root=FORGE_CONTRACT_ROOT,
            product_contract_root=PRODUCT_CONTRACT_ROOT,
            world_access_contract_root=WORLD_ACCESS_CONTRACT_ROOT,
        )
        manifest = json.loads(
            (WORLD_ACCESS_CONTRACT_ROOT / "manifest.json").read_text("utf-8")
        )

        self.assertEqual(
            roots,
            (
                FORGE_CONTRACT_ROOT.resolve(),
                PRODUCT_CONTRACT_ROOT.resolve(),
                WORLD_ACCESS_CONTRACT_ROOT.resolve(),
            ),
        )
        self.assertEqual(
            manifest["dependencies"],
            {
                "product_api": {
                    "contract": "dreamland-private-product-api",
                    "revision": "0.7.0-rc.4",
                    "manifest_sha256": TOOL.PRODUCT_MANIFEST_SHA256,
                },
                "world_forge_api": {
                    "contract": "dreamland-private-world-forge-api",
                    "revision": "0.1.0-rc.4",
                    "manifest_sha256": TOOL.FORGE_MANIFEST_SHA256,
                },
            },
        )
        self.assertEqual(
            hashlib.sha256(
                (PRODUCT_CONTRACT_ROOT / "manifest.json").read_bytes()
            ).hexdigest(),
            TOOL.PRODUCT_MANIFEST_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(
                (FORGE_CONTRACT_ROOT / "manifest.json").read_bytes()
            ).hexdigest(),
            TOOL.FORGE_MANIFEST_SHA256,
        )

    def test_main_rejects_equal_ports_before_credentials_or_provider_build(
        self,
    ) -> None:
        arguments = _serve_arguments(
            extra=[
                "--forge-port",
                "43210",
                "--stage-port",
                "43210",
            ]
        )
        with (
            patch.object(TOOL, "load_bootstrap") as load_bootstrap,
            patch.object(TOOL, "validate_contract_dependencies") as validate,
            patch.object(TOOL, "load_settings") as load_settings,
            patch.object(TOOL, "build_live_composition") as build_composition,
            patch("sys.stderr", new_callable=StringIO) as error_output,
        ):
            status = TOOL.main(arguments)

        self.assertEqual(status, 2)
        self.assertIn("ports must differ", error_output.getvalue())
        load_bootstrap.assert_not_called()
        validate.assert_not_called()
        load_settings.assert_not_called()
        build_composition.assert_not_called()

    def test_main_rejects_contract_before_settings_or_provider_build(self) -> None:
        with (
            patch.object(
                TOOL,
                "load_bootstrap",
                return_value=TOOL.WorldAccessLiveBootstrap(
                    PRESENTER_CONTEXT,
                    OPERATOR_CONTEXT,
                ),
            ),
            patch.object(
                TOOL._FORGE_TRANSPORT,
                "load_bearer",
                return_value=FORGE_BEARER,
            ),
            patch.object(
                TOOL._LIFECYCLE,
                "load_preview_bearer_file",
                return_value=PRODUCT_BEARER,
            ),
            patch.object(
                TOOL,
                "validate_contract_dependencies",
                side_effect=ValueError("dependency mismatch"),
            ),
            patch.object(TOOL, "load_settings") as load_settings,
            patch.object(TOOL, "build_live_composition") as build_composition,
            patch("sys.stderr", new_callable=StringIO) as error_output,
        ):
            status = TOOL.main(_serve_arguments())

        self.assertEqual(status, 2)
        self.assertIn("dependency mismatch", error_output.getvalue())
        load_settings.assert_not_called()
        build_composition.assert_not_called()

    def test_fake_live_servers_share_product_epoch_and_isolate_identities(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with _RunningLiveWorldAccess(Path(directory)) as running:
                harness = running.pair.stage.preview
                world_status, world, world_headers = running.stage_request(
                    "GET",
                    TOOL._WORLD.IDENTITY_PATH,
                )
                product_status, product, product_headers = running.stage_request(
                    "GET",
                    "/product/v1/capabilities",
                )
                forge_status, forge, _forge_headers = running.forge_request(
                    "GET",
                    "/forge/v1/identity",
                )

                self.assertEqual(running.forge_generator.calls, 0)
                self.assertEqual(running.attendee_executor.calls, 0)
                self.assertEqual(
                    {
                        harness.stage_service.service_epoch,
                        harness.attendee_service.service_epoch,
                        harness.access.service_epoch,
                        harness.world_catalog.service_epoch,
                        world["service_epoch"],
                        product["service_epoch"],
                    },
                    {product["service_epoch"]},
                )

        self.assertEqual((world_status, product_status, forge_status), (200, 200, 200))
        self.assertEqual(
            world["contract"],
            {
                "name": "dreamland-private-world-access-api",
                "revision": "0.1.0-rc.4",
                "compatibility": "unfrozen-integration-preview",
            },
        )
        self.assertEqual(
            world["dependencies"],
            {"product_api": "0.7.0-rc.4", "world_forge_api": "0.1.0-rc.4"},
        )
        self.assertEqual(
            world["preview"],
            {
                "kind": TOOL.LIVE_KIND,
                "provider_calls": True,
                "live_forge_evidence": True,
                "live_attendee_execution": True,
                "live_capability_evidence": False,
                "loopback_only": True,
                "process_local": True,
                "capabilities_enabled": [],
            },
        )
        self.assertEqual(product["contract"]["revision"], "0.7.0-rc.4")
        self.assertTrue(product["preview"]["provider_calls"])
        self.assertNotIn("live_attendee_execution", product["preview"])
        self.assertEqual(forge["contract_revision"], "0.1.0-rc.4")
        self.assertNotEqual(forge["service_epoch"], product["service_epoch"])
        self.assertEqual(
            world_headers["X-Dreamland-Contract-Revision"],
            "0.1.0-rc.4",
        )
        self.assertEqual(
            product_headers["X-Dreamland-Contract-Revision"],
            "0.7.0-rc.4",
        )

    def test_fake_live_forge_to_generated_attendee_command_flow_and_boundaries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with _RunningLiveWorldAccess(Path(directory)) as running:
                _, world, _ = running.stage_request(
                    "GET",
                    TOOL._WORLD.IDENTITY_PATH,
                )
                epoch = world["service_epoch"]
                status, context, _ = running.stage_request(
                    "POST",
                    TOOL._WORLD._ATTENDEE.ATTENDEE_CONTEXTS_PATH,
                    {"gateway_context_id": "gateway-context:" + "a" * 32},
                    headers={
                        "X-Dreamland-Expected-Epoch": epoch,
                        "Idempotency-Key": "phase8b-bootstrap-0001",
                    },
                )
                self.assertEqual(status, 201, context)
                attendee = context["actor_context"]
                attendee_headers = _actor_headers(epoch, attendee)
                operator_headers = _actor_headers(epoch, OPERATOR_CONTEXT)
                presenter_headers = _actor_headers(epoch, PRESENTER_CONTEXT)

                status, hidden_catalog, _ = running.stage_request(
                    "GET",
                    TOOL._WORLD.CATALOG_PATH,
                    headers=attendee_headers,
                )
                self.assertEqual(status, 200, hidden_catalog)
                self.assertEqual(
                    [item["origin"]["kind"] for item in hidden_catalog["offerings"]],
                    ["authored_profile_snapshot"],
                )
                self.assertEqual(running.forge_generator.calls, 0)
                self.assertEqual(running.attendee_executor.calls, 0)

                forge_status, forge_identity, _ = running.forge_request(
                    "GET",
                    "/forge/v1/identity",
                )
                self.assertEqual(forge_status, 200, forge_identity)
                denied_forge_status, denied_forge, _ = running.forge_request(
                    "GET",
                    "/forge/v1/recipes",
                    headers={
                        "X-Dreamland-Expected-Epoch": forge_identity["service_epoch"],
                        "X-Dreamland-Actor-Context": OPERATOR_CONTEXT,
                    },
                )
                self.assertEqual(denied_forge_status, 403, denied_forge)
                self.assertEqual(
                    denied_forge["error"]["code"],
                    "authority_denied",
                )
                self.assertEqual(running.forge_generator.calls, 0)

                reference, selected = running.build_world(key="phase8b-build-0001")
                self.assertEqual(running.forge_generator.calls, 1)
                self.assertEqual(running.attendee_executor.calls, 0)

                for actor_name, headers in (
                    ("presenter", presenter_headers),
                    ("attendee", attendee_headers),
                ):
                    denied_status, denied, _ = running.stage_request(
                        "POST",
                        TOOL._WORLD.PROMOTIONS_PATH,
                        {"forge_job_reference": reference},
                        headers={
                            **headers,
                            "Idempotency-Key": f"phase8b-denied-{actor_name}",
                        },
                    )
                    self.assertEqual(denied_status, 403, denied)
                    self.assertEqual(denied["error"]["code"], "authority_denied")

                operator_catalog_status, operator_catalog, _ = running.stage_request(
                    "GET",
                    TOOL._WORLD.CATALOG_PATH,
                    headers=operator_headers,
                )
                self.assertEqual(operator_catalog_status, 403, operator_catalog)
                self.assertEqual(
                    operator_catalog["error"]["code"],
                    "authority_denied",
                )

                status, promotion, _ = running.stage_request(
                    "POST",
                    TOOL._WORLD.PROMOTIONS_PATH,
                    {"forge_job_reference": reference},
                    headers={
                        **operator_headers,
                        "Idempotency-Key": "phase8b-promote-0001",
                    },
                )
                self.assertEqual(status, 201, promotion)
                self.assertEqual(promotion["state"], "hidden")
                self.assertEqual(
                    "sha256:" + promotion["revision"]["sha256"],
                    selected["receipt"]["revision"]["immutable_digest"],
                )

                status, still_hidden, _ = running.stage_request(
                    "GET",
                    TOOL._WORLD.CATALOG_PATH,
                    headers=attendee_headers,
                )
                self.assertEqual(status, 200, still_hidden)
                self.assertEqual(len(still_hidden["offerings"]), 1)

                status, revealed, _ = running.stage_request(
                    "POST",
                    (
                        f"{TOOL._WORLD.PROMOTIONS_PATH}/"
                        f"{quote(promotion['promotion_id'], safe='')}/reveal"
                    ),
                    {"expected_access_record_id": promotion["access_record_id"]},
                    headers={
                        **operator_headers,
                        "Idempotency-Key": "phase8b-reveal-0001",
                    },
                )
                self.assertEqual(status, 200, revealed)
                self.assertEqual(revealed["state"], "revealed")

                options_status, options, _ = running.stage_request(
                    "GET",
                    TOOL._WORLD._ATTENDEE.ATTENDEE_LAUNCH_OPTIONS_PATH,
                    headers=attendee_headers,
                )
                self.assertEqual(options_status, 200, options)
                self.assertEqual(len(options["options"]), 1)
                self.assertEqual(
                    options["options"][0]["origin"],
                    {"kind": "authored_profile_snapshot"},
                )

                status, mixed_catalog, _ = running.stage_request(
                    "GET",
                    TOOL._WORLD.CATALOG_PATH,
                    headers=attendee_headers,
                )
                self.assertEqual(status, 200, mixed_catalog)
                self.assertEqual(
                    {item["origin"]["kind"] for item in mixed_catalog["offerings"]},
                    {"authored_profile_snapshot", "world_forge_selection"},
                )
                generated = next(
                    item
                    for item in mixed_catalog["offerings"]
                    if item["origin"]["kind"] == "world_forge_selection"
                )
                serialized_catalog = json.dumps(mixed_catalog, sort_keys=True)
                self.assertNotIn("revision_id", serialized_catalog)
                self.assertNotIn("launch_grant", serialized_catalog)

                exchange_body = {
                    "offering_id": generated["offering_id"],
                    "offering_revision": generated["offering_revision"],
                    "notice_version_presented": generated["notice_version"],
                }
                exchange_headers = {
                    **attendee_headers,
                    "Idempotency-Key": "phase8b-exchange-0001",
                }
                status, grant, _ = running.stage_request(
                    "POST",
                    TOOL._WORLD.LAUNCH_SELECTIONS_PATH,
                    exchange_body,
                    headers=exchange_headers,
                )
                self.assertEqual(status, 200, grant)
                self.assertEqual(grant["selection"], generated)
                replay_status, grant_replay, _ = running.stage_request(
                    "POST",
                    TOOL._WORLD.LAUNCH_SELECTIONS_PATH,
                    exchange_body,
                    headers=exchange_headers,
                )
                self.assertEqual(replay_status, 200, grant_replay)
                self.assertTrue(grant_replay["replayed"])
                self.assertEqual(grant_replay["launch_grant"], grant["launch_grant"])
                self.assertEqual(
                    grant_replay["launch_binding_sha256"],
                    grant["launch_binding_sha256"],
                )
                self.assertEqual(running.attendee_executor.calls, 0)

                create_body = {"launch_grant": grant["launch_grant"]}
                create_headers = {
                    **attendee_headers,
                    "Idempotency-Key": "phase8b-create-0001",
                }
                status, created, _ = running.stage_request(
                    "POST",
                    "/product/v1/sessions",
                    create_body,
                    headers=create_headers,
                )
                self.assertEqual(status, 201, created)
                self.assertEqual(
                    created["snapshot"]["launch_binding"]["sha256"],
                    grant["launch_binding_sha256"],
                )
                self.assertNotIn(
                    "max_commands",
                    created["snapshot"]["launch_binding"]["limits"],
                )
                replay_status, created_replay, _ = running.stage_request(
                    "POST",
                    "/product/v1/sessions",
                    create_body,
                    headers=create_headers,
                )
                self.assertEqual(replay_status, 201, created_replay)
                self.assertTrue(created_replay["replayed"])
                self.assertEqual(
                    created_replay["session_authority"],
                    created["session_authority"],
                )
                self.assertEqual(created_replay["snapshot"], created["snapshot"])
                current_status, current, _ = running.stage_request(
                    "GET",
                    "/product/v1/sessions/current",
                    headers=attendee_headers,
                )
                self.assertEqual(current_status, 200, current)
                self.assertEqual(
                    current["snapshot"]["reference"],
                    created["snapshot"]["reference"],
                )
                correlations = {
                    (grant["service_epoch"], grant["launch_binding_sha256"]),
                    (
                        grant_replay["service_epoch"],
                        grant_replay["launch_binding_sha256"],
                    ),
                    (
                        created["service_epoch"],
                        created["snapshot"]["launch_binding"]["sha256"],
                    ),
                    (
                        created_replay["service_epoch"],
                        created_replay["snapshot"]["launch_binding"]["sha256"],
                    ),
                    (
                        current["service_epoch"],
                        current["snapshot"]["launch_binding"]["sha256"],
                    ),
                }
                self.assertEqual(
                    correlations,
                    {(epoch, grant["launch_binding_sha256"])},
                )
                self.assertEqual(running.attendee_executor.calls, 0)

                session_reference = created["snapshot"]["reference"]
                session_headers = {
                    **attendee_headers,
                    "X-Dreamland-Session-Capability": created["session_authority"],
                }
                command_path = (
                    "/product/v1/sessions/"
                    + quote(session_reference, safe="")
                    + "/commands"
                )
                attendee_core = running.pair.stage.preview.attendee_core
                policy = attendee_core._core_policy
                lifecycle = attendee_core._policy
                admission = running.pair.stage.preview.stage_service.admission.limits
                self.assertIsNone(policy.max_commands_per_session)
                self.assertEqual(
                    policy.command_deadline_ms,
                    TOOL._STAGE_LIVE.COMMAND_DEADLINE_MS,
                )
                self.assertEqual(
                    policy.max_output_bytes_per_command,
                    TOOL._STAGE_LIVE.MAX_OUTPUT_BYTES_PER_COMMAND,
                )
                self.assertEqual(
                    policy.max_output_chunks_per_command,
                    TOOL._STAGE_LIVE.MAX_OUTPUT_CHUNKS_PER_COMMAND,
                )
                self.assertEqual(policy.max_attempts_per_command, 1)
                self.assertEqual(
                    policy.reserved_tokens_per_command,
                    running.composition.attendee_command_reserved_tokens,
                )
                self.assertEqual(
                    policy.reserved_spend_units_per_command,
                    TOOL._STAGE_LIVE.RESERVED_SPEND_UNITS_PER_COMMAND,
                )
                self.assertEqual(
                    lifecycle.absolute_ttl_ms,
                    min(2 * 60 * 60_000, running.composition.context_ttl_ms),
                )
                self.assertEqual(
                    lifecycle.idle_ttl_ms,
                    min(30 * 60_000, running.composition.context_ttl_ms),
                )
                self.assertEqual(
                    admission.max_physical_workers,
                    2 + TOOL._ATTENDEE_LIVE.ATTENDEE_PHYSICAL_WORKERS,
                )
                self.assertEqual(
                    admission.max_logical_jobs,
                    (
                        TOOL._STAGE_LIVE.MAX_LOGICAL_JOBS
                        + TOOL._ATTENDEE_LIVE.ATTENDEE_LOGICAL_JOBS
                    ),
                )
                self.assertEqual(
                    admission.token_budget,
                    (
                        TOOL._STAGE_LIVE.MAX_LOGICAL_JOBS
                        * running.composition.presenter.stage_command_reserved_tokens
                        + TOOL._ATTENDEE_LIVE.ATTENDEE_LOGICAL_JOBS
                        * running.composition.attendee_command_reserved_tokens
                    ),
                )
                self.assertEqual(
                    admission.spend_budget_units,
                    (
                        TOOL._STAGE_LIVE.SPEND_BUDGET_UNITS
                        + TOOL._ATTENDEE_LIVE.ATTENDEE_SPEND_BUDGET
                    ),
                )

                generation = 0
                result = None
                for index in range(13):
                    status, accepted, _ = running.stage_request(
                        "POST",
                        command_path,
                        {
                            "command": f"echo generated-{index + 1}",
                            "expected_slot_generation": generation,
                        },
                        headers={
                            **session_headers,
                            "Idempotency-Key": f"phase8b-command-{index + 1:04d}",
                        },
                    )
                    self.assertEqual(status, 202, accepted)
                    result = _wait_for_command(
                        running,
                        command_path + "/" + quote(accepted["command_id"], safe=""),
                        session_headers,
                    )
                    self.assertEqual(result["command"]["status"], "completed")
                    generation = result["command"]["slot_generation"]
                oversized_status, oversized, _ = running.stage_request(
                    "POST",
                    command_path,
                    {
                        "command": "x" * 4_097,
                        "expected_slot_generation": generation,
                    },
                    headers={
                        **session_headers,
                        "Idempotency-Key": "phase8b-command-oversized",
                    },
                )
                accounting = (
                    running.pair.stage.preview.world_catalog.publication_accounting()
                )

        self.assertIsNotNone(result)
        self.assertEqual(result["command"]["status"], "completed")
        self.assertEqual(result["command"]["result"]["outcome"], "completed")
        self.assertEqual(result["command"]["slot_generation"], 13)
        self.assertEqual(oversized_status, 413)
        self.assertEqual(oversized["error"]["code"], "command_too_large")
        self.assertEqual(running.forge_generator.calls, 1)
        self.assertEqual(running.attendee_executor.calls, 13)
        self.assertEqual(
            accounting,
            {
                "retention": "service_epoch_lifetime",
                "capacity": TOOL.GENERATED_PUBLICATION_CAPACITY,
                "used": 1,
                "remaining": TOOL.GENERATED_PUBLICATION_CAPACITY - 1,
            },
        )

    def test_positive_generated_command_limit_rejects_successor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with _RunningLiveWorldAccess(
                Path(directory),
                max_commands=12,
            ) as running:
                command_path, session_headers = _launch_generated_session(
                    running,
                    suffix="positive-limit",
                )
                policy = running.pair.stage.preview.attendee_core._core_policy
                self.assertEqual(policy.max_commands_per_session, 12)
                self.assertEqual(
                    running.pair.stage.preview.world_catalog._policy.launch_limits[
                        "max_commands"
                    ],
                    12,
                )
                generation = 0
                for index in range(12):
                    status, accepted, _ = running.stage_request(
                        "POST",
                        command_path,
                        {
                            "command": f"echo bounded-{index + 1}",
                            "expected_slot_generation": generation,
                        },
                        headers={
                            **session_headers,
                            "Idempotency-Key": f"positive-command-{index + 1:04d}",
                        },
                    )
                    self.assertEqual(status, 202, accepted)
                    terminal = _wait_for_command(
                        running,
                        command_path + "/" + quote(accepted["command_id"], safe=""),
                        session_headers,
                    )
                    self.assertEqual(terminal["command"]["status"], "completed")
                    generation = terminal["command"]["slot_generation"]
                rejected_status, rejected, _ = running.stage_request(
                    "POST",
                    command_path,
                    {
                        "command": "echo bounded-13",
                        "expected_slot_generation": generation,
                    },
                    headers={
                        **session_headers,
                        "Idempotency-Key": "positive-command-0013",
                    },
                )

        self.assertEqual(generation, 12)
        self.assertEqual(rejected_status, 409)
        self.assertEqual(rejected["error"]["code"], "command_limit_reached")
        self.assertEqual(running.forge_generator.calls, 1)
        self.assertEqual(running.attendee_executor.calls, 12)

    def test_startup_announcement_redacts_authority_and_reports_accounting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _RunningLiveWorldAccess(root) as running:
                announcement = TOOL.startup_announcement(
                    running.pair,
                    running.composition,
                )
                presenter = running.composition.presenter

        self.assertEqual(announcement["contract_revision"], "0.7.0-rc.4")
        self.assertEqual(
            announcement["world_access_contract_revision"],
            "0.1.0-rc.4",
        )
        self.assertTrue(announcement["live_forge_evidence"])
        self.assertTrue(announcement["live_attendee_execution"])
        self.assertEqual(
            announcement["world_access"]["publication_accounting"],
            {
                "retention": "service_epoch_lifetime",
                "capacity": TOOL.GENERATED_PUBLICATION_CAPACITY,
                "used": 0,
                "remaining": TOOL.GENERATED_PUBLICATION_CAPACITY,
            },
        )
        self.assertEqual(
            announcement["attendee_sessions"]["generated_publication_capacity"],
            TOOL.GENERATED_PUBLICATION_CAPACITY,
        )
        rendered = json.dumps(announcement, sort_keys=True)
        for secret in (
            PRESENTER_CONTEXT,
            OPERATOR_CONTEXT,
            FORGE_BEARER,
            PRODUCT_BEARER,
            ELIGIBILITY_KEY.hex(),
            str(root),
            presenter.authored_revision.digest,
            presenter.authored_title,
            presenter.authored_revision_label,
            "launch-grant:",
            "bff-authority:",
        ):
            self.assertNotIn(secret, rendered)


def _fake_live_composition(root: Path, *, max_commands: int | None = None):
    presenter = PHASE7B._composition(root)
    presenter = replace(
        presenter,
        actor_context=PRESENTER_CONTEXT,
        max_commands=max_commands,
    )
    forge_generator = _CountingGenerator(presenter.forge_runner.generator)
    presenter.forge_runner.generator = forge_generator
    attendee_executor = _CountingExecutor(PHASE7B._live_style_executor())
    attendee_reservation = TOOL._STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND
    admission = TOOL._ATTENDEE_LIVE._shared_admission(
        presenter_command_reserved_tokens=presenter.stage_command_reserved_tokens,
        attendee_command_reserved_tokens=attendee_reservation,
    )
    presenter = replace(presenter, admission=admission)
    now_ms = TOOL._STAGE_LIVE._utc_ms()
    composition = TOOL._ATTENDEE_LIVE.LiveAttendeeComposition(
        presenter=presenter,
        attendee_executor=attendee_executor,
        attendee_model="provider-free:phase8b-attendee-test",
        attendee_max_output_tokens=1,
        attendee_command_reserved_tokens=attendee_reservation,
        max_contexts=2,
        context_ttl_ms=60 * 60_000,
        admission_opens_at_ms=now_ms - 1_000,
        admission_closes_at_ms=now_ms + 60 * 60_000,
        generated_world_access=True,
        generated_eligibility_key=ELIGIBILITY_KEY,
        attendee_notice_version=TOOL.NOTICE_VERSION,
        max_revisions=TOOL.ATTENDEE_REVISION_CAPACITY,
        max_offerings=TOOL.ATTENDEE_OFFERING_CAPACITY,
        generated_publication_capacity=TOOL.GENERATED_PUBLICATION_CAPACITY,
    )
    return composition, forge_generator, attendee_executor


def _launch_generated_session(
    running: _RunningLiveWorldAccess,
    *,
    suffix: str,
) -> tuple[str, dict[str, str]]:
    status, identity, _ = running.stage_request("GET", TOOL._WORLD.IDENTITY_PATH)
    if status != 200:
        raise AssertionError(identity)
    epoch = identity["service_epoch"]
    status, context, _ = running.stage_request(
        "POST",
        TOOL._WORLD._ATTENDEE.ATTENDEE_CONTEXTS_PATH,
        {"gateway_context_id": "gateway-context:" + "b" * 32},
        headers={
            "X-Dreamland-Expected-Epoch": epoch,
            "Idempotency-Key": f"{suffix}-bootstrap",
        },
    )
    if status != 201:
        raise AssertionError(context)
    attendee_headers = _actor_headers(epoch, context["actor_context"])
    operator_headers = _actor_headers(epoch, OPERATOR_CONTEXT)
    reference, _selected = running.build_world(key=f"{suffix}-build")
    status, promotion, _ = running.stage_request(
        "POST",
        TOOL._WORLD.PROMOTIONS_PATH,
        {"forge_job_reference": reference},
        headers={**operator_headers, "Idempotency-Key": f"{suffix}-promote"},
    )
    if status != 201:
        raise AssertionError(promotion)
    status, revealed, _ = running.stage_request(
        "POST",
        (
            f"{TOOL._WORLD.PROMOTIONS_PATH}/"
            f"{quote(promotion['promotion_id'], safe='')}/reveal"
        ),
        {"expected_access_record_id": promotion["access_record_id"]},
        headers={**operator_headers, "Idempotency-Key": f"{suffix}-reveal"},
    )
    if status != 200:
        raise AssertionError(revealed)
    status, catalog, _ = running.stage_request(
        "GET",
        TOOL._WORLD.CATALOG_PATH,
        headers=attendee_headers,
    )
    if status != 200:
        raise AssertionError(catalog)
    generated = next(
        item
        for item in catalog["offerings"]
        if item["origin"]["kind"] == "world_forge_selection"
    )
    status, grant, _ = running.stage_request(
        "POST",
        TOOL._WORLD.LAUNCH_SELECTIONS_PATH,
        {
            "offering_id": generated["offering_id"],
            "offering_revision": generated["offering_revision"],
            "notice_version_presented": generated["notice_version"],
        },
        headers={**attendee_headers, "Idempotency-Key": f"{suffix}-exchange"},
    )
    if status != 200:
        raise AssertionError(grant)
    status, created, _ = running.stage_request(
        "POST",
        "/product/v1/sessions",
        {"launch_grant": grant["launch_grant"]},
        headers={**attendee_headers, "Idempotency-Key": f"{suffix}-create"},
    )
    if status != 201:
        raise AssertionError(created)
    command_path = (
        "/product/v1/sessions/"
        + quote(created["snapshot"]["reference"], safe="")
        + "/commands"
    )
    session_headers = {
        **attendee_headers,
        "X-Dreamland-Session-Capability": created["session_authority"],
    }
    return command_path, session_headers


def _actor_headers(epoch: str, actor: str) -> dict[str, str]:
    return {
        "X-Dreamland-Expected-Epoch": epoch,
        "X-Dreamland-Actor-Context": actor,
    }


def _serve_arguments(*, extra: list[str] | None = None) -> list[str]:
    return [
        "serve",
        "--forge-token-file",
        "/not-read/forge.token",
        "--stage-token-file",
        "/not-read/product.token",
        "--bootstrap-file",
        "/not-read/bootstrap.json",
        "--forge-contract-root",
        str(FORGE_CONTRACT_ROOT),
        "--product-contract-root",
        str(PRODUCT_CONTRACT_ROOT),
        "--world-access-contract-root",
        str(WORLD_ACCESS_CONTRACT_ROOT),
        "--artifact-root",
        "/not-created/artifacts",
        "--authored-profile-path",
        "/not-read/authored.json",
        "--authored-profile-digest",
        "sha256:" + "a" * 64,
        *(extra or []),
    ]


def _request(port, method, path, body, headers):
    values = dict(headers)
    encoded = None
    if body is not None:
        encoded = json.dumps(body, separators=(",", ":")).encode()
        values["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, encoded, values)
    response = connection.getresponse()
    payload = json.loads(response.read())
    status = response.status
    response_headers = response.headers
    connection.close()
    return status, payload, response_headers


def _wait_for_command(
    running: _RunningLiveWorldAccess,
    path: str,
    headers: dict[str, str],
) -> dict:
    deadline = time.monotonic() + 5
    while True:
        status, result, _ = running.stage_request("GET", path, headers=headers)
        if status != 200:
            raise AssertionError(result)
        if result["command"]["status"] in {"completed", "failed", "interrupted"}:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError("fake-live attendee command did not finish")
        time.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
