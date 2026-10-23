from __future__ import annotations

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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from dreamland.product.forge_artifacts import ImmutableForgeStore, SingleAttemptForge
from dreamland.product.forge_runtime import ForgeStateProfileCodec
from dreamland.state_profiles import StateProfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_forge_stage_live.py"
FORGE_CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "world-forge-api" / "v0.1.0-rc.4"
STAGE_CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.6.0-rc.3"
CLIENT_PATH = STAGE_CONTRACT_ROOT / "generated" / "product_session_client.py"


def _load_tool(name: str):
    return _load_path(TOOL_PATH, name)


def _load_path(path: Path, name: str):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("combined live tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


TOOL = _load_tool("dreamland_product_forge_stage_live_unit_tool")
CLIENT = _load_path(CLIENT_PATH, "dreamland_product_forge_stage_live_unit_client")


class _Generator:
    def generate(self, _instruction):
        return _profile()


class ProductForgeStageLiveTests(unittest.TestCase):
    def test_presenter_accounting_uses_effective_12800_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            composition = replace(
                _composition(Path(directory)),
                stage_max_output_tokens=12_800,
                stage_command_reserved_tokens=12_800,
            )
            harness = TOOL.LiveForgeStageHarness(composition)
            try:
                self.assertEqual(
                    harness.service._core_policy.reserved_tokens_per_command,
                    12_800,
                )
                limits = harness.service.admission.limits
                self.assertEqual(
                    limits.token_budget,
                    TOOL._STAGE_LIVE.MAX_LOGICAL_JOBS * 12_800,
                )
                self.assertEqual(
                    limits.presenter_stage_reserved_token_budget,
                    limits.token_budget,
                )
            finally:
                harness.close()

            with _running_pair(composition) as running:
                self.assertEqual(
                    running.pair.stage.handoff._policy.launch_limits[
                        "command_reserved_tokens"
                    ],
                    12_800,
                )

            with self.assertRaisesRegex(
                ValueError,
                "does not match provider maximum",
            ):
                TOOL.LiveForgeStageHarness(
                    replace(
                        composition,
                        stage_command_reserved_tokens=(
                            TOOL._STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND
                        ),
                    )
                )

    def test_unbounded_stage_maximum_fails_before_provider_construction(self) -> None:
        settings = SimpleNamespace(agent=SimpleNamespace(max_output_tokens=None))
        with (
            patch.object(TOOL._FORGE_LIVE, "build_runner") as forge_builder,
            patch.object(
                TOOL._STAGE_LIVE,
                "build_live_executor",
            ) as stage_builder,
            self.assertRaisesRegex(ValueError, "finite positive integer"),
        ):
            TOOL.build_live_composition(
                settings=settings,
                actor_context="actor:combined_presenter_0001",
                artifact_root=Path("/tmp/provider-free-not-used"),
                deadline_seconds=30,
                max_commands=None,
            )
        forge_builder.assert_not_called()
        stage_builder.assert_not_called()

    def test_fresh_import_constructs_no_runtime_resources_or_files(self) -> None:
        script = r"""
import concurrent.futures
import http.server
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
import dreamland.agent_core as agent_core
import dreamland.model_providers as model_providers
import dreamland.spiritguide as spiritguide

tool_path = Path(sys.argv[1])
scratch = Path(sys.argv[2])
sys.dont_write_bytecode = True
os.chdir(scratch)

filesystem_mutations = []
mutating_events = {
    "os.chmod",
    "os.chown",
    "os.link",
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.symlink",
    "os.truncate",
}


def audit(event, arguments):
    if event == "open":
        path, mode, flags = arguments
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
        if (
            isinstance(mode, str)
            and any(character in mode for character in "wax+")
        ) or (isinstance(flags, int) and flags & write_flags):
            filesystem_mutations.append((event, os.fspath(path)))
    elif event in mutating_events:
        filesystem_mutations.append((event, repr(arguments[0])))


sys.addaudithook(audit)
threads_before = tuple(
    sorted((thread.ident, thread.name) for thread in threading.enumerate())
)

with (
    patch.object(agent, "LangGraphCommandAgent") as command_agent,
    patch.object(agent_core, "build_chat_model") as command_model_factory,
    patch.object(spiritguide, "ModelSpiritguide") as spiritguide_constructor,
    patch.object(spiritguide, "build_chat_model") as spiritguide_model_factory,
    patch.object(model_providers, "build_chat_model") as provider_factory,
    patch.object(socket, "socket") as socket_constructor,
    patch.object(socketserver.TCPServer, "__init__") as listener_constructor,
    patch.object(threading.Thread, "__init__") as thread_constructor,
    patch.object(threading.Thread, "start") as thread_start,
    patch.object(
        concurrent.futures.ThreadPoolExecutor,
        "__init__",
    ) as worker_constructor,
):
    specification = importlib.util.spec_from_file_location(
        "dreamland_forge_stage_live_fresh_import_isolation",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise AssertionError("combined live tool is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        sys.modules.pop(specification.name, None)

    calls = {
        "command_agent": command_agent.call_count,
        "command_model_factory": command_model_factory.call_count,
        "spiritguide": spiritguide_constructor.call_count,
        "spiritguide_model_factory": spiritguide_model_factory.call_count,
        "provider_factory": provider_factory.call_count,
        "socket": socket_constructor.call_count,
        "listener": listener_constructor.call_count,
        "thread": thread_constructor.call_count,
        "thread_start": thread_start.call_count,
        "worker": worker_constructor.call_count,
    }

threads_after = tuple(
    sorted((thread.ident, thread.name) for thread in threading.enumerate())
)
print(
    json.dumps(
        {
            "calls": calls,
            "filesystem_mutations": filesystem_mutations,
            "scratch_entries": sorted(path.name for path in scratch.iterdir()),
            "threads_unchanged": threads_after == threads_before,
        },
        sort_keys=True,
    )
)
"""
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, "-c", script, str(TOOL_PATH), directory],
                check=True,
                capture_output=True,
                cwd=REPOSITORY_ROOT,
                env=environment,
                text=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["calls"], {name: 0 for name in payload["calls"]})
        self.assertEqual(payload["filesystem_mutations"], [])
        self.assertEqual(payload["scratch_entries"], [])
        self.assertTrue(payload["threads_unchanged"])

    def test_import_is_inert_and_credentials_are_owner_only(self) -> None:
        before = {thread.ident for thread in threading.enumerate()}
        loaded = _load_tool("dreamland_product_forge_stage_live_inert_tool")
        self.assertEqual({thread.ident for thread in threading.enumerate()}, before)
        self.assertEqual(loaded.CONTRACT_REVISION, "0.6.0-rc.3")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forge_token = root / "forge.token"
            stage_token = root / "stage.token"
            bootstrap = root / "bootstrap.json"
            loaded.initialize_private_files(
                forge_token_file=forge_token,
                stage_token_file=stage_token,
                bootstrap_file=bootstrap,
            )
            value = loaded.load_bootstrap(bootstrap)
            self.assertTrue(value.actor_context.startswith("actor:"))
            for path in (forge_token, stage_token, bootstrap):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotEqual(forge_token.read_text(), stage_token.read_text())

    def test_parser_preserves_positive_and_unlimited_command_modes(self) -> None:
        parser = TOOL._parser()
        common = [
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
            str(STAGE_CONTRACT_ROOT),
            "--artifact-root",
            "/tmp/forge-stage-artifacts",
        ]
        self.assertEqual(
            parser.parse_args([*common, "--max-commands", "7"]).max_commands,
            7,
        )
        self.assertIsNone(
            parser.parse_args([*common, "--max-commands", "-1"]).max_commands
        )
        for value in ("0", "-2"):
            with self.assertRaises(SystemExit):
                parser.parse_args([*common, "--max-commands", value])

    def test_live_composition_injects_forge_codec_into_real_stage_builder(self) -> None:
        runner = SimpleNamespace(store=object())
        captured = {}

        def build_executor(*, settings, state_codec):
            captured["settings"] = settings
            captured["codec"] = state_codec
            return object(), "stage:test", 2_048

        settings = SimpleNamespace(agent=SimpleNamespace(max_output_tokens=2_048))
        with (
            patch.object(
                TOOL._FORGE_LIVE,
                "build_runner",
                return_value=(runner, "forge:test", 4_096),
            ),
            patch.object(
                TOOL._STAGE_LIVE,
                "build_live_executor",
                side_effect=build_executor,
            ),
        ):
            composition = TOOL.build_live_composition(
                settings=settings,
                actor_context="actor:combined_presenter_0001",
                artifact_root=Path("/tmp/provider-free-not-used"),
                deadline_seconds=30,
                max_commands=None,
            )

        self.assertIs(captured["settings"], settings)
        self.assertIsInstance(captured["codec"], ForgeStateProfileCodec)
        self.assertIsNone(composition.max_commands)
        self.assertEqual(composition.forge_model, "forge:test")
        self.assertEqual(composition.stage_model, "stage:test")
        self.assertEqual(
            composition.stage_command_reserved_tokens,
            TOOL._STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND,
        )

    def test_provider_free_http_handoff_preserves_exact_selected_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            composition = _composition(root)
            with _running_pair(composition) as running:
                forge_epoch = running.forge_identity()["service_epoch"]
                stage_capabilities = running.stage_request(
                    "GET", "/product/v1/capabilities"
                )[1]
                stage_epoch = stage_capabilities["service_epoch"]
                self.assertTrue(
                    all(
                        not item["enabled"]
                        for item in stage_capabilities["capabilities"].values()
                    )
                )

                short_suffix = "123456789012345"
                status, invalid_epoch = running.handoff(
                    f"forge-epoch:{short_suffix}",
                    stage_epoch,
                    "forge-reference:1234567890123456",
                    actor=composition.actor_context,
                    key="idempotency:short_forge_epoch_0001",
                )
                self.assertEqual(status, 400)
                self.assertEqual(invalid_epoch["error"]["code"], "invalid_request")
                status, invalid_reference = running.handoff(
                    forge_epoch,
                    stage_epoch,
                    f"forge-reference:{short_suffix}",
                    actor=composition.actor_context,
                    key="idempotency:short_forge_reference_0001",
                )
                self.assertEqual(status, 400)
                self.assertEqual(invalid_reference["error"]["code"], "invalid_request")

                forge_reference, selected = running.build_world(
                    forge_epoch,
                    key="idempotency:forge_build_0001",
                )
                stage_harness = running.pair.stage.preview
                self.assertEqual(stage_harness.service._revisions, {})
                self.assertEqual(
                    running.pair.forge.preview_harness.service._policy.maximum_jobs,
                    TOOL.MAXIMUM_FORGE_JOBS,
                )

                status, denied = running.handoff(
                    forge_epoch,
                    stage_epoch,
                    forge_reference,
                    actor="actor:another_presenter_0001",
                    key="idempotency:denied_handoff_0001",
                )
                self.assertEqual(status, 403)
                self.assertEqual(denied["error"]["code"], "authority_denied")
                self.assertEqual(stage_harness.service._revisions, {})

                status, launched = running.handoff(
                    forge_epoch,
                    stage_epoch,
                    forge_reference,
                    actor=composition.actor_context,
                    key="idempotency:valid_handoff_0001",
                )
                self.assertEqual(status, 201)
                CLIENT.validate_forge_stage_handoff(launched)
                self.assertEqual(launched["kind"], "forge_stage_session_created")
                snapshot = launched["snapshot"]
                source = snapshot["stage"]["source"]
                revision = selected["receipt"]["revision"]
                self.assertEqual(
                    source["immutable_digest"], revision["immutable_digest"]
                )
                self.assertEqual(
                    launched["forge_selection"]["revision"],
                    {
                        "revision_id": revision["revision_id"],
                        "immutable_digest": revision["immutable_digest"],
                    },
                )
                self.assertTrue(snapshot["stage"]["xray_available"])
                self.assertNotIn("max_commands", snapshot["launch_binding"]["limits"])

                stored = composition.forge_runner.store.load(
                    revision["revision_id"],
                    revision["immutable_digest"],
                )
                allocated = stage_harness.service._revisions[
                    revision["revision_id"]
                ].allocate()
                self.assertEqual(allocated, stored.document)

                encoded_reference = quote(snapshot["reference"], safe="")
                status, refreshed = running.stage_request(
                    "GET",
                    f"/product/v1/sessions/{encoded_reference}",
                    headers={
                        "X-Dreamland-Expected-Epoch": stage_epoch,
                        "X-Dreamland-Actor-Context": composition.actor_context,
                        "X-Dreamland-Session-Capability": launched["session_authority"],
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(refreshed["snapshot"]["stage"]["source"], source)

                generated_client = CLIENT.ProductSessionPreviewClient(
                    f"http://127.0.0.1:{running.pair.stage.server_address[1]}",
                    running.stage_bearer,
                )
                xray_snapshot = generated_client.stage_xray_snapshot(
                    epoch=stage_epoch,
                    actor=composition.actor_context,
                    capability=launched["session_authority"],
                    reference=snapshot["reference"],
                )
                CLIENT.validate_stage_xray_snapshot(xray_snapshot)
                self.assertEqual(
                    xray_snapshot["xray"]["overview"]["world_name"]["text"],
                    "harbor-control",
                )
                self.assertEqual(len(xray_snapshot["xray"]["hosts"]), 1)
                host_id = xray_snapshot["xray"]["hosts"][0]["entity_id"]
                position = xray_snapshot["xray"]["projection_position"]
                xray_detail = generated_client.stage_xray_detail(
                    epoch=stage_epoch,
                    actor=composition.actor_context,
                    capability=launched["session_authority"],
                    reference=snapshot["reference"],
                    entity_id=host_id,
                    accepted_state_revision=xray_snapshot["xray"][
                        "accepted_state_revision"
                    ],
                    xray_stream_id=position["stream_id"],
                    xray_sequence=position["sequence"],
                )
                CLIENT.validate_stage_xray_detail(
                    xray_detail,
                    expected_entity_id=host_id,
                    expected_accepted_state_revision=xray_snapshot["xray"][
                        "accepted_state_revision"
                    ],
                    expected_xray_stream_id=position["stream_id"],
                    expected_xray_sequence=position["sequence"],
                )

                stale_coordinates = (
                    {
                        "accepted_state_revision": xray_snapshot["xray"][
                            "accepted_state_revision"
                        ]
                        + 1,
                        "xray_stream_id": position["stream_id"],
                        "xray_sequence": position["sequence"],
                    },
                    {
                        "accepted_state_revision": xray_snapshot["xray"][
                            "accepted_state_revision"
                        ],
                        "xray_stream_id": "xray-stream:" + "f" * 32,
                        "xray_sequence": position["sequence"],
                    },
                    {
                        "accepted_state_revision": xray_snapshot["xray"][
                            "accepted_state_revision"
                        ],
                        "xray_stream_id": position["stream_id"],
                        "xray_sequence": position["sequence"] + 1,
                    },
                )
                for stale_coordinate in stale_coordinates:
                    with self.assertRaises(CLIENT.ProductSessionPreviewError) as raised:
                        generated_client.stage_xray_detail(
                            epoch=stage_epoch,
                            actor=composition.actor_context,
                            capability=launched["session_authority"],
                            reference=snapshot["reference"],
                            entity_id=host_id,
                            **stale_coordinate,
                        )
                    self.assertEqual(raised.exception.status, 409)
                    self.assertEqual(
                        raised.exception.payload["error"]["code"],
                        "xray_resync_required",
                    )
                    self.assertEqual(
                        raised.exception.payload["error"]["retry"],
                        "resume_xray_snapshot",
                    )

                with self.assertRaises(CLIENT.ProductSessionPreviewError) as raised:
                    generated_client.stage_xray_detail(
                        epoch=stage_epoch,
                        actor=composition.actor_context,
                        capability=launched["session_authority"],
                        reference=snapshot["reference"],
                        entity_id="host:" + "f" * 32,
                        accepted_state_revision=xray_snapshot["xray"][
                            "accepted_state_revision"
                        ],
                        xray_stream_id=position["stream_id"],
                        xray_sequence=position["sequence"],
                    )
                self.assertEqual(raised.exception.status, 404)
                self.assertEqual(
                    raised.exception.payload["error"]["code"],
                    "xray_entity_unavailable",
                )

                status, replayed = running.handoff(
                    forge_epoch,
                    stage_epoch,
                    forge_reference,
                    actor=composition.actor_context,
                    key="idempotency:valid_handoff_0001",
                )
                self.assertEqual(status, 201)
                self.assertTrue(replayed["replayed"])
                self.assertEqual(
                    replayed["snapshot"]["reference"], snapshot["reference"]
                )

                command_generation = snapshot["command_slot"]["generation"]
                thirteenth = None
                for command_index in range(13):
                    thirteenth = running.submit_and_wait(
                        stage_epoch=stage_epoch,
                        reference=snapshot["reference"],
                        capability=launched["session_authority"],
                        generation=command_generation,
                        command_index=command_index,
                    )
                    self.assertEqual(thirteenth["status"], "completed")
                    command_generation = thirteenth["slot_generation"]
                self.assertIsNotNone(thirteenth)
                self.assertEqual(thirteenth["slot_generation"], 13)

                replacement_reference, replacement_selected = running.build_world(
                    forge_epoch,
                    key="idempotency:forge_build_0002",
                )
                status, replacement_requires_confirmation = running.handoff(
                    forge_epoch,
                    stage_epoch,
                    replacement_reference,
                    actor=composition.actor_context,
                    key="idempotency:ordinary_handoff_must_not_replace_0001",
                )
                self.assertEqual(status, 409)
                self.assertEqual(
                    replacement_requires_confirmation["error"]["code"],
                    "current_session_exists",
                )
                status, before_replacement = running.stage_request(
                    "GET",
                    f"/product/v1/sessions/{encoded_reference}",
                    headers={
                        "X-Dreamland-Expected-Epoch": stage_epoch,
                        "X-Dreamland-Actor-Context": composition.actor_context,
                        "X-Dreamland-Session-Capability": launched["session_authority"],
                    },
                )
                self.assertEqual(status, 200)
                active_before_replacement = before_replacement["snapshot"]
                self.assertEqual(
                    active_before_replacement["session_id"], snapshot["session_id"]
                )
                self.assertEqual(active_before_replacement["stage"]["source"], source)
                self.assertEqual(
                    active_before_replacement["lifecycle"]["state"], "active"
                )
                self.assertEqual(stage_harness.service._tombstones_by_reference, {})

                replacement_path = (
                    "/product/v1/stage/sessions/"
                    + encoded_reference
                    + TOOL.FORGE_REPLACEMENT_SUFFIX
                )
                replacement_body = {
                    "forge_reference": replacement_reference,
                    "expected_control_revision": active_before_replacement[
                        "coordinates"
                    ]["control_revision"],
                }
                replacement_headers = {
                    "X-Dreamland-Expected-Epoch": stage_epoch,
                    "X-Dreamland-Forge-Expected-Epoch": forge_epoch,
                    "X-Dreamland-Actor-Context": composition.actor_context,
                    "X-Dreamland-Session-Capability": launched["session_authority"],
                    "Idempotency-Key": "idempotency:forge_replace_0001",
                }
                status, replaced = running.stage_request(
                    "POST",
                    replacement_path,
                    replacement_body,
                    headers=replacement_headers,
                )
                self.assertEqual(status, 200)
                CLIENT.validate_forge_stage_handoff(
                    replaced,
                    expected_control_revision=replacement_body[
                        "expected_control_revision"
                    ],
                )
                self.assertEqual(replaced["kind"], "forge_stage_session_replaced")
                self.assertFalse(replaced["replayed"])
                previous = replaced["previous_snapshot"]
                self.assertEqual(
                    previous["session_id"], active_before_replacement["session_id"]
                )
                self.assertEqual(
                    previous["reference"], active_before_replacement["reference"]
                )
                self.assertEqual(
                    previous["starting_revision"],
                    active_before_replacement["starting_revision"],
                )
                self.assertEqual(
                    previous["launch_binding"],
                    active_before_replacement["launch_binding"],
                )
                previous_stage = {**previous["stage"]}
                active_stage = {**active_before_replacement["stage"]}
                self.assertFalse(previous_stage.pop("xray_available"))
                self.assertTrue(active_stage.pop("xray_available"))
                self.assertEqual(previous_stage, active_stage)
                self.assertEqual(previous["stage"]["source"], source)
                self.assertEqual(previous["available_controls"], [])
                self.assertEqual(
                    previous["command_slot"],
                    {
                        "state": "terminal",
                        "generation": active_before_replacement["command_slot"][
                            "generation"
                        ],
                        "active_command_id": None,
                    },
                )
                self.assertEqual(
                    previous["latest_command"],
                    active_before_replacement["latest_command"],
                )
                self.assertEqual(
                    previous["transcript"], active_before_replacement["transcript"]
                )
                self.assertEqual(previous["lifecycle"]["state"], "replaced")
                self.assertEqual(previous["lifecycle"]["terminal_reason"], "replaced")
                self.assertTrue(previous["recovery"]["tombstone"])
                self.assertIsNotNone(previous["recovery"]["retain_until_ms"])
                self.assertGreater(
                    previous["recovery"]["retain_until_ms"],
                    previous["lifecycle"]["terminal_at_ms"],
                )
                self.assertEqual(
                    previous["coordinates"]["control_revision"],
                    replacement_body["expected_control_revision"] + 1,
                )
                self.assertEqual(
                    replaced["snapshot"]["coordinates"]["control_revision"], 1
                )
                self.assertEqual(
                    previous["coordinates"]["command_slot_generation"],
                    active_before_replacement["coordinates"]["command_slot_generation"],
                )
                self.assertEqual(
                    previous["coordinates"]["accepted_state_revision"],
                    active_before_replacement["coordinates"]["accepted_state_revision"],
                )
                self.assertEqual(
                    previous["coordinates"]["transcript"],
                    active_before_replacement["coordinates"]["transcript"],
                )
                self.assertEqual(
                    previous["coordinates"]["event"]["stream_id"],
                    active_before_replacement["coordinates"]["event"]["stream_id"],
                )
                self.assertEqual(
                    previous["coordinates"]["event"]["sequence"],
                    active_before_replacement["coordinates"]["event"]["sequence"] + 1,
                )
                replacement_source = replaced["snapshot"]["stage"]["source"]
                self.assertNotEqual(
                    replacement_source["selected_record_id"],
                    source["selected_record_id"],
                )
                self.assertEqual(
                    replacement_source["immutable_digest"],
                    replacement_selected["receipt"]["revision"]["immutable_digest"],
                )
                replacement_selection = replaced["forge_selection"]
                self.assertEqual(
                    replacement_source,
                    {
                        "kind": "world_forge_selection",
                        "selected_record_id": replacement_selection[
                            "selected_record_id"
                        ],
                        "immutable_digest": replacement_selection["revision"][
                            "immutable_digest"
                        ],
                        "forge_job_id": replacement_selection["job_id"],
                        "forge_reference": replacement_selection["reference"],
                    },
                )
                self.assertNotIn(
                    "max_commands",
                    replaced["snapshot"]["launch_binding"]["limits"],
                )

                status, revoked = running.stage_request(
                    "GET",
                    f"/product/v1/sessions/{encoded_reference}",
                    headers={
                        "X-Dreamland-Expected-Epoch": stage_epoch,
                        "X-Dreamland-Actor-Context": composition.actor_context,
                        "X-Dreamland-Session-Capability": launched["session_authority"],
                    },
                )
                self.assertEqual(status, 403)
                self.assertEqual(revoked["error"]["code"], "authority_denied")

                active_session_ids = set(stage_harness.service._sessions_by_id)
                tombstone_references = set(
                    stage_harness.service._tombstones_by_reference
                )
                status, replacement_replay = running.stage_request(
                    "POST",
                    replacement_path,
                    replacement_body,
                    headers=replacement_headers,
                )
                self.assertEqual(status, 200)
                CLIENT.validate_forge_stage_handoff(
                    replacement_replay,
                    expected_control_revision=replacement_body[
                        "expected_control_revision"
                    ],
                )
                self.assertTrue(replacement_replay["replayed"])
                self.assertEqual(
                    replacement_replay["kind"], "forge_stage_session_replaced"
                )
                self.assertEqual(replacement_replay["previous_snapshot"], previous)
                self.assertEqual(replacement_replay["snapshot"], replaced["snapshot"])
                self.assertEqual(
                    replacement_replay["session_authority"],
                    replaced["session_authority"],
                )
                self.assertEqual(
                    replacement_replay["forge_selection"], replacement_selection
                )
                self.assertEqual(
                    set(stage_harness.service._sessions_by_id), active_session_ids
                )
                self.assertEqual(
                    set(stage_harness.service._tombstones_by_reference),
                    tombstone_references,
                )
                self.assertEqual(len(active_session_ids), 1)
                self.assertEqual(len(tombstone_references), 1)
                self.assertEqual(stage_harness.service._replacement_claims, {})

    def test_startup_announcement_is_explicit_and_contains_no_authority(self) -> None:
        composition = SimpleNamespace(
            actor_context="actor:must_not_be_announced_0001",
            forge_deadline_seconds=120,
            forge_max_output_tokens=4_096,
            forge_model="openai:forge-model",
            max_commands=None,
            stage_max_output_tokens=2_048,
            stage_command_reserved_tokens=(
                TOOL._STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND
            ),
            stage_model="openai:stage-model",
        )
        pair = TOOL.LiveServerPair(
            SimpleNamespace(server_address=("127.0.0.1", 41001)),
            SimpleNamespace(server_address=("127.0.0.1", 41002)),
        )
        announcement = TOOL.startup_announcement(pair, composition)
        self.assertEqual(announcement["capabilities_enabled"], 0)
        self.assertFalse(announcement["normal_runtime_composition"])
        self.assertEqual(
            announcement["presenter_stage"]["effective_command_limit"],
            "unlimited",
        )
        self.assertEqual(announcement["world_forge"]["model"], "openai:forge-model")
        self.assertEqual(announcement["presenter_stage"]["model"], "openai:stage-model")
        self.assertEqual(
            announcement["presenter_stage"]["command_reserved_tokens"],
            TOOL._STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND,
        )
        self.assertEqual(
            announcement["presenter_stage"]["token_budget"],
            TOOL._STAGE_LIVE.TOKEN_BUDGET,
        )
        self.assertEqual(
            announcement["presenter_stage"]["xray"],
            {
                "available": True,
                "schema_revision": "stage-xray-v1",
                "authority": "presenter_session_only",
                "maximum_hosts": 64,
                "maximum_facts": 128,
            },
        )
        rendered = json.dumps(announcement)
        self.assertNotIn(composition.actor_context, rendered)
        self.assertNotIn("bearer", rendered)
        self.assertNotIn("api_key", rendered)

    def test_dependency_digest_mismatch_fails_before_harness_or_listener(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tampered_forge = root / "world-forge"
            tampered_forge.mkdir()
            tampered_manifest = (
                FORGE_CONTRACT_ROOT / "manifest.json"
            ).read_bytes() + b"\n"
            (tampered_forge / "manifest.json").write_bytes(tampered_manifest)
            composition = _composition(root / "artifacts")
            with (
                patch.object(
                    TOOL,
                    "validate_forge_contract_root",
                    return_value=tampered_forge,
                ),
                patch.object(
                    TOOL,
                    "validate_contract_root",
                    return_value=STAGE_CONTRACT_ROOT,
                ),
                patch.object(TOOL._FORGE_LIVE, "LiveForgeHarness") as forge_harness,
                patch.object(TOOL, "LiveForgeStageHarness") as stage_harness,
                patch.object(
                    TOOL._FORGE_TRANSPORT,
                    "ForgePreviewServer",
                ) as forge_server,
            ):
                with self.assertRaisesRegex(ValueError, "manifest digest"):
                    TOOL.build_server_pair(
                        host="127.0.0.1",
                        forge_port=0,
                        stage_port=0,
                        forge_bearer=(
                            "forge-preview-bearer:provider_free_test_00000001"
                        ),
                        stage_bearer="preview-bearer:provider_free_test_00000001",
                        forge_contract_root=tampered_forge,
                        stage_contract_root=STAGE_CONTRACT_ROOT,
                        composition=composition,
                    )
            forge_harness.assert_not_called()
            stage_harness.assert_not_called()
            forge_server.assert_not_called()

    def test_main_validates_dependency_before_provider_construction(self) -> None:
        arguments = [
            "serve",
            "--forge-token-file",
            "/tmp/not-read-forge.token",
            "--stage-token-file",
            "/tmp/not-read-stage.token",
            "--bootstrap-file",
            "/tmp/not-read-bootstrap.json",
            "--forge-contract-root",
            str(FORGE_CONTRACT_ROOT),
            "--contract-root",
            str(STAGE_CONTRACT_ROOT),
            "--artifact-root",
            "/tmp/not-created-forge-stage-artifacts",
        ]
        with (
            patch.object(
                TOOL,
                "load_bootstrap",
                return_value=TOOL.ForgeStageBootstrap("actor:combined_presenter_0001"),
            ),
            patch.object(
                TOOL._FORGE_TRANSPORT,
                "load_bearer",
                return_value="forge-preview-bearer:provider_free_test_00000001",
            ),
            patch.object(
                TOOL._LIFECYCLE,
                "load_preview_bearer_file",
                return_value="preview-bearer:provider_free_test_00000001",
            ),
            patch.object(
                TOOL,
                "validate_contract_dependency",
                side_effect=ValueError("dependency mismatch"),
            ),
            patch.object(TOOL, "load_settings") as load_settings,
        ):
            self.assertEqual(TOOL.main(arguments), 2)
        load_settings.assert_not_called()


class _RunningPair:
    def __init__(self, composition) -> None:
        self.composition = composition
        self.forge_bearer = "forge-preview-bearer:provider_free_test_00000001"
        self.stage_bearer = "preview-bearer:provider_free_test_00000001"
        self.pair = TOOL.build_server_pair(
            host="127.0.0.1",
            forge_port=0,
            stage_port=0,
            forge_bearer=self.forge_bearer,
            stage_bearer=self.stage_bearer,
            forge_contract_root=FORGE_CONTRACT_ROOT,
            stage_contract_root=STAGE_CONTRACT_ROOT,
            composition=composition,
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

    def forge_identity(self):
        return self.forge_request("GET", "/forge/v1/identity")[1]

    def forge_request(self, method, path, body=None, *, headers=None):
        values = {
            "Authorization": f"Bearer {self.forge_bearer}",
            "Accept": "application/json",
            **(headers or {}),
        }
        if body is not None:
            values["Content-Type"] = "application/json"
        return _request(self.pair.forge.server_address[1], method, path, body, values)

    def stage_request(self, method, path, body=None, *, headers=None):
        values = {
            "Authorization": f"Bearer {self.stage_bearer}",
            "Accept": "application/json",
            **(headers or {}),
        }
        if body is not None:
            values["Content-Type"] = "application/json"
        return _request(self.pair.stage.server_address[1], method, path, body, values)

    def build_world(self, epoch, *, key):
        authority = {
            "X-Dreamland-Actor-Context": self.composition.actor_context,
            "X-Dreamland-Expected-Epoch": epoch,
        }
        status, recipes = self.forge_request(
            "GET", "/forge/v1/recipes", headers=authority
        )
        if status != 200:
            raise AssertionError(recipes)
        status, prepared = self.forge_request(
            "POST",
            "/forge/v1/preparations",
            {
                "recipe_id": recipes["recipes"][0]["recipe_id"],
                "recipe_revision": recipes["recipes"][0]["recipe_revision"],
                "answers": _answers(),
            },
            headers=authority,
        )
        if status != 201:
            raise AssertionError(prepared)
        status, accepted = self.forge_request(
            "POST",
            "/forge/v1/builds",
            {"preparation_reference": prepared["preparation_reference"]},
            headers={
                **authority,
                "Idempotency-Key": key,
            },
        )
        if status != 201:
            raise AssertionError(accepted)
        reference = accepted["reference"]
        deadline = time.monotonic() + 5
        while True:
            status, selected = self.forge_request(
                "GET",
                "/forge/v1/builds/" + quote(reference, safe=""),
                headers=authority,
            )
            if status != 200:
                raise AssertionError(selected)
            if selected["terminal"]:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("provider-free Forge job did not finish")
            time.sleep(0.01)
        if selected["lifecycle"]["state"] != "selected":
            raise AssertionError(selected)
        return reference, selected

    def handoff(self, forge_epoch, stage_epoch, reference, *, actor, key):
        return self.stage_request(
            "POST",
            TOOL.FORGE_HANDOFF_PATH,
            {"forge_reference": reference},
            headers={
                "X-Dreamland-Expected-Epoch": stage_epoch,
                "X-Dreamland-Forge-Expected-Epoch": forge_epoch,
                "X-Dreamland-Actor-Context": actor,
                "Idempotency-Key": key,
            },
        )

    def submit_and_wait(
        self,
        *,
        stage_epoch,
        reference,
        capability,
        generation,
        command_index,
    ):
        encoded_reference = quote(reference, safe="")
        common_headers = {
            "X-Dreamland-Expected-Epoch": stage_epoch,
            "X-Dreamland-Actor-Context": self.composition.actor_context,
            "X-Dreamland-Session-Capability": capability,
        }
        status, accepted = self.stage_request(
            "POST",
            f"/product/v1/sessions/{encoded_reference}/commands",
            {
                "command": f"echo combined-{command_index + 1}",
                "expected_slot_generation": generation,
            },
            headers={
                **common_headers,
                "Idempotency-Key": (
                    f"idempotency:combined_command_{command_index:04d}"
                ),
            },
        )
        if status != 202:
            raise AssertionError(accepted)
        command_id = quote(accepted["command_id"], safe="")
        deadline = time.monotonic() + 5
        while True:
            status, result = self.stage_request(
                "GET",
                (f"/product/v1/sessions/{encoded_reference}/commands/" + command_id),
                headers=common_headers,
            )
            if status != 200:
                raise AssertionError(result)
            command = result["command"]
            if command["status"] in {"completed", "interrupted", "failed"}:
                return command
            if time.monotonic() >= deadline:
                raise AssertionError("provider-free Stage command did not finish")
            time.sleep(0.005)


def _running_pair(composition):
    return _RunningPair(composition)


def _request(port, method, path, body, headers):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(
        method,
        path,
        body=None if body is None else json.dumps(body),
        headers=headers,
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
    status = response.status
    connection.close()
    return status, payload


def _composition(root: Path):
    store = ImmutableForgeStore(root / "revisions")
    runner = SingleAttemptForge(_Generator(), store)
    return TOOL.LiveForgeStageComposition(
        actor_context="actor:combined_presenter_0001",
        forge_runner=runner,
        forge_model="provider-free:forge-test",
        forge_max_output_tokens=1,
        forge_deadline_seconds=3,
        stage_executor=TOOL._CORE.DeterministicPreviewExecutor(),
        stage_model="provider-free:stage-test",
        stage_max_output_tokens=1,
        stage_command_reserved_tokens=TOOL._STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND,
        max_commands=None,
    )


def _profile() -> StateProfile:
    return StateProfile(
        name="combined-live-candidate",
        level="world",
        created_at="2026-08-26T00:00:00+00:00",
        state={
            "world_state": {
                "name": "harbor-control",
                "entry_host": "console",
                "active_host": "console",
                "purpose": {"theme": "Harbor control during a radio outage"},
                "hosts_by_name": {
                    "console": {
                        "login_banner": "Test console\n",
                        "hostname": "console",
                        "purpose": {"theme": "Coordinate harbor traffic"},
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


def _answers():
    return [
        {"field_id": "day-or-date", "kind": "short-text", "value": "Saturday"},
        {"field_id": "year", "kind": "integer", "value": "2026"},
        {"field_id": "adjective", "kind": "short-text", "value": "stormy"},
        {
            "field_id": "industry-or-setting",
            "kind": "short-text",
            "value": "harbor control room",
        },
        {"field_id": "technology-one", "kind": "short-text", "value": "radar"},
        {
            "field_id": "technology-two",
            "kind": "short-text",
            "value": "radio consoles",
        },
        {
            "field_id": "important-job",
            "kind": "short-text",
            "value": "coordinate vessel traffic",
        },
        {
            "field_id": "wildcard-event",
            "kind": "short-text",
            "value": "the radios lose their uplink",
        },
    ]


if __name__ == "__main__":
    unittest.main()
