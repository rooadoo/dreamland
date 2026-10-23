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
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from dreamland.models import CommandResult, CommandStateUpdate
from dreamland.product.authored_artifacts import (
    ImmutableAuthoredStore,
    authored_revision_digest,
    import_configured_authored_profile,
    load_configured_authored_profile,
)
from dreamland.product.forge_artifacts import ImmutableForgeStore, SingleAttemptForge
from dreamland.product.launch_selection_runtime import (
    LaunchSelectionStateProfileCodec,
)
from dreamland.product.runtime_executor import (
    ProductRuntimeCommandExecutor,
    ProductRuntimeUsage,
)
from dreamland.state_profiles import StateProfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_launch_selection_live.py"
FORGE_CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "world-forge-api" / "v0.1.0-rc.4"
STAGE_CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.7.0-rc.2"
FORGE_BEARER = "forge-preview-bearer:provider_free_test_00000001"
STAGE_BEARER = "preview-bearer:provider_free_test_00000001"
ACTOR_CONTEXT = "actor:combined_presenter_0001"


def _load_tool(name: str):
    specification = importlib.util.spec_from_file_location(name, TOOL_PATH)
    if specification is None or specification.loader is None:
        raise RuntimeError("live launch-selection tool is unavailable")
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


TOOL = _load_tool("dreamland_product_launch_selection_live_test_tool")


class _Generator:
    def generate(self, _instruction):
        return _forge_profile()


class ProductLaunchSelectionLiveTests(unittest.TestCase):
    def test_unbounded_stage_maximum_fails_before_provider_construction(self) -> None:
        settings = TOOL.load_settings(Path("/definitely-missing-launch-live.ini"))
        settings = replace(
            settings,
            agent=replace(settings.agent, max_output_tokens=None),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "authored.json"
            _write_authored(source)
            digest = authored_revision_digest(load_configured_authored_profile(source))
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
                    actor_context=ACTOR_CONTEXT,
                    artifact_root=root,
                    authored_profile_path=source,
                    authored_profile_digest=digest,
                    authored_title="Authored",
                    authored_revision_label="Pinned",
                    deadline_seconds=1,
                    max_commands=1,
                )
            forge_builder.assert_not_called()
            stage_builder.assert_not_called()

    def test_presenter_accounting_uses_effective_12800_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            composition = replace(
                _composition(Path(directory)),
                stage_max_output_tokens=12_800,
                stage_command_reserved_tokens=12_800,
            )
            harness = TOOL.LiveLaunchSelectionHarness(composition)
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
                self.assertEqual(
                    harness.forge_stage_policy.launch_limits["command_reserved_tokens"],
                    12_800,
                )
            finally:
                harness.close()

            with self.assertRaisesRegex(
                ValueError,
                "does not match provider maximum",
            ):
                TOOL.LiveLaunchSelectionHarness(
                    replace(
                        composition,
                        stage_command_reserved_tokens=(
                            TOOL._STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND
                        ),
                    )
                )

    def test_fresh_import_is_inert(self) -> None:
        script = r"""
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import dreamland.agent as agent
import dreamland.spiritguide as spiritguide

tool_path = Path(sys.argv[1])
scratch = Path(sys.argv[2])
os.chdir(scratch)
before = sorted(thread.name for thread in threading.enumerate())
with (
    patch.object(agent, "LangGraphCommandAgent") as command_agent,
    patch.object(spiritguide, "ModelSpiritguide") as forge_agent,
):
    specification = importlib.util.spec_from_file_location(
        "dreamland_launch_selection_live_inert_check", tool_path
    )
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    calls = [command_agent.call_count, forge_agent.call_count]
after = sorted(thread.name for thread in threading.enumerate())
print(json.dumps({
    "calls": calls,
    "entries": sorted(path.name for path in scratch.iterdir()),
    "threads_unchanged": before == after,
}))
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
        self.assertEqual(payload["calls"], [0, 0])
        self.assertEqual(payload["entries"], [])
        self.assertTrue(payload["threads_unchanged"])

    def test_digest_authored_is_exact_and_prints_no_path_or_profile_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "private-friendly-name.json"
            _write_authored(source)
            expected = authored_revision_digest(
                load_configured_authored_profile(source)
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOL_PATH),
                    "digest-authored",
                    "--profile-path",
                    str(source),
                ],
                check=True,
                capture_output=True,
                cwd=REPOSITORY_ROOT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
            )
        payload = json.loads(result.stdout)
        self.assertEqual(payload, {"authored_revision_digest": expected})
        self.assertNotIn(str(source), result.stdout)
        self.assertNotIn("private-authored-name", result.stdout)

    def test_parser_accepts_positive_and_unlimited_commands(self) -> None:
        parser = TOOL._parser()
        common = _serve_arguments()
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

    def test_bad_authored_pin_fails_before_provider_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "authored.json"
            _write_authored(source)
            with (
                patch.object(TOOL._FORGE_LIVE, "build_runner") as forge_builder,
                patch.object(TOOL._STAGE_LIVE, "build_live_executor") as stage_builder,
            ):
                with self.assertRaisesRegex(ValueError, "digest does not match"):
                    TOOL.build_live_composition(
                        settings=SimpleNamespace(
                            agent=SimpleNamespace(max_output_tokens=2_048)
                        ),
                        actor_context=ACTOR_CONTEXT,
                        artifact_root=root / "artifacts",
                        authored_profile_path=source,
                        authored_profile_digest="sha256:" + "0" * 64,
                        authored_title="Authored world",
                        authored_revision_label="Pinned revision",
                        deadline_seconds=30,
                        max_commands=None,
                    )
            forge_builder.assert_not_called()
            stage_builder.assert_not_called()
            self.assertFalse((root / "artifacts" / "authored-revisions").exists())

    def test_composition_imports_before_providers_and_injects_dispatch_codec(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "authored.json"
            _write_authored(source)
            pin = TOOL.authored_digest(source)
            events: list[str] = []

            def build_runner(_settings, artifact_root):
                self.assertEqual(artifact_root, root / "artifacts")
                self.assertTrue((artifact_root / "authored-revisions").is_dir())
                events.append("forge")
                return SimpleNamespace(store=object()), "forge:test", 4_096

            def build_executor(*, settings, state_codec):
                self.assertIsInstance(state_codec, LaunchSelectionStateProfileCodec)
                events.append("stage")
                return object(), "stage:test", 2_048

            with (
                patch.object(
                    TOOL._FORGE_LIVE,
                    "build_runner",
                    side_effect=build_runner,
                ),
                patch.object(
                    TOOL._STAGE_LIVE,
                    "build_live_executor",
                    side_effect=build_executor,
                ),
            ):
                composition = TOOL.build_live_composition(
                    settings=SimpleNamespace(
                        agent=SimpleNamespace(max_output_tokens=2_048)
                    ),
                    actor_context=ACTOR_CONTEXT,
                    artifact_root=root / "artifacts",
                    authored_profile_path=source,
                    authored_profile_digest=pin,
                    authored_title="Authored world",
                    authored_revision_label="Pinned revision",
                    deadline_seconds=30,
                    max_commands=None,
                )

        self.assertEqual(events, ["forge", "stage"])
        self.assertEqual(composition.authored_revision.digest, pin)
        self.assertIsNone(composition.max_commands)

    def test_harness_shares_epoch_actor_expiry_and_preserves_safeguards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            composition = _composition(Path(directory))
            harness = TOOL.LiveLaunchSelectionHarness(composition)
            try:
                self.assertEqual(
                    harness.selection.service_epoch,
                    harness.service.service_epoch,
                )
                stage_actor = next(iter(harness.service._actors.values()))
                registry_actor = next(iter(harness.selection_registry._actors.values()))
                self.assertEqual(
                    stage_actor.expires_at_ms, composition.actor_expires_at_ms
                )
                self.assertEqual(
                    registry_actor.expires_at_ms,
                    composition.actor_expires_at_ms,
                )
                self.assertEqual(stage_actor.actor_id, TOOL.PRESENTER_ACTOR_ID)
                self.assertEqual(registry_actor.actor_id, TOOL.PRESENTER_ACTOR_ID)
                self.assertEqual(harness.service._policy.max_revisions, 32)
                self.assertEqual(harness.selection_registry._policy.max_revisions, 32)
                self.assertEqual(harness.selection_registry._policy.max_offerings, 32)
                self.assertEqual(harness.service._policy.max_launch_grants, 64)
                self.assertIsNone(harness.service._core_policy.max_commands_per_session)
                self.assertEqual(
                    harness.service._core_policy.command_deadline_ms,
                    TOOL._STAGE_LIVE.COMMAND_DEADLINE_MS,
                )
                self.assertEqual(
                    harness.service._core_policy.max_output_bytes_per_command,
                    TOOL._STAGE_LIVE.MAX_OUTPUT_BYTES_PER_COMMAND,
                )
                authored = harness.selection.options(
                    actor_authority=composition.actor_context,
                    expected_epoch=harness.service.service_epoch,
                )["options"][0]
                self.assertEqual(
                    authored["origin"]["kind"], "authored_profile_snapshot"
                )
                self.assertNotIn(
                    "max_commands", harness.forge_stage_policy.launch_limits
                )
            finally:
                harness.close()

    def test_forge_harness_uses_the_exact_shared_actor_expiry(self) -> None:
        expiry_ms = TOOL._STAGE_LIVE._utc_ms() + 60_000
        harness = TOOL._FORGE_LIVE.LiveForgeHarness(
            actor_context=ACTOR_CONTEXT,
            runner=SimpleNamespace(),
            deadline_seconds=3,
            actor_expires_at=TOOL._actor_expiry(expiry_ms),
        )
        try:
            actor = harness.service._actors[ACTOR_CONTEXT]
            observed = datetime.fromisoformat(actor.expires_at)
            self.assertEqual(int(observed.timestamp() * 1_000), expiry_ms)
            self.assertEqual(actor.actor_id, TOOL.PRESENTER_ACTOR_ID)
        finally:
            harness.close()

    def test_contract_dependency_and_capabilities_are_exact(self) -> None:
        forge, stage = TOOL.validate_contract_dependency(
            forge_contract_root=FORGE_CONTRACT_ROOT,
            stage_contract_root=STAGE_CONTRACT_ROOT,
        )
        self.assertEqual(forge, FORGE_CONTRACT_ROOT.resolve())
        self.assertEqual(stage, STAGE_CONTRACT_ROOT.resolve())
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["contract_revision"], "0.7.0-rc.2")
        self.assertTrue(
            all(not value["enabled"] for value in manifest["capabilities"].values())
        )

    def test_startup_announcement_contains_no_authority_or_authored_source(
        self,
    ) -> None:
        composition = SimpleNamespace(
            actor_context="actor:must_not_appear_0001",
            actor_expires_at_ms=123,
            authored_revision=SimpleNamespace(
                digest="sha256:" + "a" * 64,
                revision_id="world-revision:sha256:" + "a" * 64,
            ),
            authored_title="Secret internal profile label",
            authored_revision_label="Secret filename label",
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
        self.assertTrue(announcement["provider_calls"])
        self.assertFalse(announcement["normal_runtime_composition"])
        self.assertEqual(
            announcement["presenter_stage"]["effective_command_limit"],
            "unlimited",
        )
        self.assertEqual(
            announcement["presenter_stage"]["command_reserved_tokens"],
            TOOL._STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND,
        )
        self.assertEqual(
            announcement["presenter_stage"]["token_budget"],
            TOOL._STAGE_LIVE.TOKEN_BUDGET,
        )
        rendered = json.dumps(announcement, sort_keys=True)
        for forbidden in (
            composition.actor_context,
            composition.authored_title,
            composition.authored_revision_label,
            composition.authored_revision.digest,
            "profile_path",
            "bearer",
            "api_key",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_provider_free_generic_and_legacy_http_paths_share_live_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            composition = _composition(Path(directory))
            with _RunningPair(composition) as running:
                capabilities = running.stage_request("GET", "/product/v1/capabilities")[
                    1
                ]
                stage_epoch = capabilities["service_epoch"]
                self.assertEqual(
                    capabilities["preview"]["executor"], TOOL.LIVE_EXECUTOR
                )
                self.assertTrue(capabilities["preview"]["provider_calls"])
                self.assertTrue(
                    all(
                        not value["enabled"]
                        for value in capabilities["capabilities"].values()
                    )
                )

                authored = running.options(stage_epoch)["options"][0]
                authored_grant = running.exchange(
                    stage_epoch,
                    authored["launch_selection_id"],
                    "idempotency:authored_exchange_0001",
                )[1]
                status, launched = running.stage_request(
                    "POST",
                    "/product/v1/stage/sessions",
                    {"launch_grant": authored_grant["launch_grant"]},
                    headers=running.stage_headers(
                        stage_epoch,
                        key="idempotency:authored_launch_0001",
                    ),
                )
                self.assertEqual(status, 201, launched)
                self.assertEqual(
                    launched["snapshot"]["stage"]["display"]["title"],
                    composition.authored_title,
                )
                self.assertNotIn(
                    "max_commands", launched["snapshot"]["launch_binding"]["limits"]
                )
                authored_terminal = running.submit_and_wait(
                    stage_epoch=stage_epoch,
                    snapshot=launched["snapshot"],
                    session_authority=launched["session_authority"],
                    command="pwd",
                    key="idempotency:authored_command_0001",
                )
                self.assertEqual(authored_terminal["status"], "completed")
                authored_xray = running.xray(
                    stage_epoch=stage_epoch,
                    reference=launched["snapshot"]["reference"],
                    session_authority=launched["session_authority"],
                )
                self.assertEqual(
                    authored_xray["xray"]["overview"]["world_name"]["text"],
                    "colonyship-a1",
                )

                forge_epoch = running.forge_request("GET", "/forge/v1/identity")[1][
                    "service_epoch"
                ]
                forge_reference, _selected = running.build_world(forge_epoch)
                options = running.wait_for_options(stage_epoch, count=2)
                forged = next(
                    option
                    for option in options["options"]
                    if option["origin"]["kind"] == "world_forge_selection"
                )
                forged_grant = running.exchange(
                    stage_epoch,
                    forged["launch_selection_id"],
                    "idempotency:forged_exchange_0001",
                )[1]
                snapshot = launched["snapshot"]
                reference = snapshot["reference"]
                status, replaced = running.stage_request(
                    "POST",
                    f"/product/v1/stage/sessions/{quote(reference, safe='')}/replace",
                    {
                        "expected_control_revision": snapshot["coordinates"][
                            "control_revision"
                        ],
                        "launch_grant": forged_grant["launch_grant"],
                    },
                    headers={
                        **running.stage_headers(
                            stage_epoch,
                            key="idempotency:forged_replace_0001",
                        ),
                        "X-Dreamland-Session-Capability": launched["session_authority"],
                    },
                )
                self.assertEqual(status, 200, replaced)
                self.assertEqual(
                    replaced["snapshot"]["stage"]["display"]["source_label"],
                    "Live World Forge",
                )
                forged_terminal = running.submit_and_wait(
                    stage_epoch=stage_epoch,
                    snapshot=replaced["snapshot"],
                    session_authority=replaced["session_authority"],
                    command="hostname",
                    key="idempotency:forged_command_0001",
                )
                self.assertEqual(forged_terminal["status"], "completed")
                forged_xray = running.xray(
                    stage_epoch=stage_epoch,
                    reference=replaced["snapshot"]["reference"],
                    session_authority=replaced["session_authority"],
                )
                self.assertEqual(
                    forged_xray["xray"]["overview"]["world_name"]["text"],
                    "harbor-control",
                )
                status, legacy = running.stage_request(
                    "POST",
                    (
                        "/product/v1/stage/sessions/"
                        f"{quote(replaced['snapshot']['reference'], safe='')}"
                        f"{TOOL._COMBINED.FORGE_REPLACEMENT_SUFFIX}"
                    ),
                    {
                        "forge_reference": forge_reference,
                        "expected_control_revision": replaced["snapshot"][
                            "coordinates"
                        ]["control_revision"],
                    },
                    headers={
                        **running.stage_headers(
                            stage_epoch,
                            key="idempotency:legacy_replacement_0001",
                        ),
                        "X-Dreamland-Forge-Expected-Epoch": forge_epoch,
                        "X-Dreamland-Session-Capability": replaced["session_authority"],
                    },
                )
                self.assertEqual(status, 200, legacy)
                self.assertEqual(legacy["kind"], "forge_stage_session_replaced")
                self.assertEqual(
                    legacy["snapshot"]["stage"]["display"],
                    replaced["snapshot"]["stage"]["display"],
                )
                self.assertEqual(
                    legacy["snapshot"]["stage"]["source"]["forge_reference"],
                    forge_reference,
                )

    def test_main_rejects_contract_before_settings_or_provider_construction(
        self,
    ) -> None:
        arguments = _serve_arguments()
        error = StringIO()
        with (
            patch.object(
                TOOL._COMBINED,
                "load_bootstrap",
                return_value=SimpleNamespace(actor_context=ACTOR_CONTEXT),
            ),
            patch.object(
                TOOL._FORGE_TRANSPORT,
                "load_bearer",
                return_value=FORGE_BEARER,
            ),
            patch.object(
                TOOL._LIFECYCLE,
                "load_preview_bearer_file",
                return_value=STAGE_BEARER,
            ),
            patch.object(
                TOOL,
                "validate_contract_dependency",
                side_effect=ValueError("dependency mismatch"),
            ),
            patch.object(TOOL, "load_settings") as settings,
            redirect_stderr(error),
        ):
            self.assertEqual(TOOL.main(arguments), 2)
        settings.assert_not_called()
        self.assertIn("dependency mismatch", error.getvalue())


class _RunningPair:
    def __init__(self, composition: TOOL.LiveLaunchSelectionComposition) -> None:
        self.composition = composition
        self.pair = TOOL._build_validated_server_pair(
            host="127.0.0.1",
            forge_port=0,
            stage_port=0,
            forge_bearer=FORGE_BEARER,
            stage_bearer=STAGE_BEARER,
            validated_forge_root=FORGE_CONTRACT_ROOT,
            validated_stage_root=STAGE_CONTRACT_ROOT,
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

    def stage_headers(self, epoch: str, *, key: str | None = None) -> dict[str, str]:
        headers = {
            "X-Dreamland-Expected-Epoch": epoch,
            "X-Dreamland-Actor-Context": self.composition.actor_context,
        }
        if key is not None:
            headers["Idempotency-Key"] = key
        return headers

    def forge_request(self, method, path, body=None, *, headers=None):
        values = {
            "Authorization": f"Bearer {FORGE_BEARER}",
            "Accept": "application/json",
            **(headers or {}),
        }
        if body is not None:
            values["Content-Type"] = "application/json"
        return _request(self.pair.forge.server_address[1], method, path, body, values)

    def stage_request(self, method, path, body=None, *, headers=None):
        values = {
            "Authorization": f"Bearer {STAGE_BEARER}",
            "Accept": "application/json",
            **(headers or {}),
        }
        if body is not None:
            values["Content-Type"] = "application/json"
        return _request(self.pair.stage.server_address[1], method, path, body, values)

    def options(self, epoch: str) -> dict[str, object]:
        status, payload = self.stage_request(
            "GET",
            TOOL._SELECTION.LAUNCH_OPTIONS_PATH,
            headers=self.stage_headers(epoch),
        )
        if status != 200:
            raise AssertionError(payload)
        return payload

    def wait_for_options(self, epoch: str, *, count: int) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while True:
            result = self.options(epoch)
            if len(result["options"]) == count:
                return result
            if time.monotonic() >= deadline:
                raise AssertionError("Forge launch option was not registered")
            time.sleep(0.01)

    def exchange(self, epoch: str, selection_id: str, key: str):
        status, payload = self.stage_request(
            "POST",
            TOOL._SELECTION.LAUNCH_SELECTIONS_PATH,
            {"launch_selection_id": selection_id},
            headers=self.stage_headers(epoch, key=key),
        )
        if status != 200:
            raise AssertionError(payload)
        return status, payload

    def build_world(self, epoch: str):
        authority = self.stage_headers(epoch)
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
                "Idempotency-Key": "idempotency:live_generic_forge_0001",
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

    def submit_and_wait(
        self,
        *,
        stage_epoch: str,
        snapshot: dict[str, object],
        session_authority: str,
        command: str,
        key: str,
    ) -> dict[str, object]:
        reference = str(snapshot["reference"])
        command_slot = snapshot["command_slot"]
        if not isinstance(command_slot, dict):
            raise AssertionError("Stage command slot is invalid")
        headers = {
            **self.stage_headers(stage_epoch, key=key),
            "X-Dreamland-Session-Capability": session_authority,
        }
        status, accepted = self.stage_request(
            "POST",
            f"/product/v1/sessions/{quote(reference, safe='')}/commands",
            {
                "command": command,
                "expected_slot_generation": command_slot["generation"],
            },
            headers=headers,
        )
        if status != 202:
            raise AssertionError(accepted)
        command_id = quote(str(accepted["command_id"]), safe="")
        read_headers = {
            **self.stage_headers(stage_epoch),
            "X-Dreamland-Session-Capability": session_authority,
        }
        deadline = time.monotonic() + 5
        while True:
            status, result = self.stage_request(
                "GET",
                f"/product/v1/sessions/{quote(reference, safe='')}/commands/{command_id}",
                headers=read_headers,
            )
            if status != 200:
                raise AssertionError(result)
            projected = result["command"]
            if projected["status"] in {"completed", "interrupted", "failed"}:
                return projected
            if time.monotonic() >= deadline:
                raise AssertionError("provider-free Stage command did not finish")
            time.sleep(0.005)

    def xray(
        self,
        *,
        stage_epoch: str,
        reference: str,
        session_authority: str,
    ) -> dict[str, object]:
        status, result = self.stage_request(
            "GET",
            f"/product/v1/stage/sessions/{quote(reference, safe='')}/xray",
            headers={
                **self.stage_headers(stage_epoch),
                "X-Dreamland-Session-Capability": session_authority,
            },
        )
        if status != 200:
            raise AssertionError(result)
        return result


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


def _composition(root: Path) -> TOOL.LiveLaunchSelectionComposition:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "authored.json"
    _write_authored(source)
    profile = load_configured_authored_profile(source)
    authored = import_configured_authored_profile(
        source,
        expected_digest=authored_revision_digest(profile),
        store=ImmutableAuthoredStore(root / "authored-revisions"),
    )
    runner = SingleAttemptForge(
        _Generator(),
        ImmutableForgeStore(root / "forge-revisions"),
    )
    return TOOL.LiveLaunchSelectionComposition(
        actor_context=ACTOR_CONTEXT,
        actor_expires_at_ms=TOOL._STAGE_LIVE._utc_ms() + TOOL.ACTOR_TTL_MS,
        authored_revision=authored,
        authored_title="Colonyship A1",
        authored_revision_label="Pinned operator revision",
        forge_runner=runner,
        forge_model="provider-free:forge-test",
        forge_max_output_tokens=1,
        forge_deadline_seconds=3,
        stage_executor=_live_style_executor(),
        stage_model="provider-free:stage-test",
        stage_max_output_tokens=1,
        stage_command_reserved_tokens=TOOL._STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND,
        max_commands=None,
    )


def _write_authored(path: Path) -> None:
    profile = StateProfile(
        name="private-authored-name",
        level="world",
        created_at="2026-08-27T00:00:00+00:00",
        source_session_id="private-source-session",
        comment="private operator note",
        state={
            "world_state": {
                "name": "colonyship-a1",
                "entry_host": "navigation-console",
                "active_host": "navigation-console",
                "hosts_by_name": {
                    "navigation-console": {
                        "login_banner": "Test console\n",
                        "hostname": "navigation-console",
                        "purpose": {"theme": "Navigate a colony ship"},
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


def _forge_profile() -> StateProfile:
    return StateProfile(
        name="live-forge-candidate",
        level="world",
        created_at="2026-08-27T00:00:00+00:00",
        state={
            "world_state": {
                "name": "harbor-control",
                "entry_host": "console",
                "active_host": "console",
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


def _answers() -> list[dict[str, str]]:
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
            "value": "the radios fail",
        },
    ]


class _RuntimeAgent:
    def __init__(self) -> None:
        self._commands = 0

    def execute(self, context, _control, _emit):
        self._commands += 1
        return CommandResult(
            stdout=f"provider-free command: {context.command}\n",
            exit_status=0,
            state_update=CommandStateUpdate(cwd=f"/tmp/command-{self._commands}"),
        )


class _UsageMeter:
    def begin(self, **_arguments):
        return object()

    def finish(self, _measurement):
        return ProductRuntimeUsage(tokens=1, spend_units=1)


class _CheckpointReaper:
    def reap(self, _checkpoint_namespace: str) -> None:
        return


def _live_style_executor() -> ProductRuntimeCommandExecutor:
    return ProductRuntimeCommandExecutor(
        agent=_RuntimeAgent(),
        state_codec=LaunchSelectionStateProfileCodec(),
        checkpoint_reaper=_CheckpointReaper(),
        usage_meter=_UsageMeter(),
        clock_ms=TOOL._STAGE_LIVE._monotonic_ms,
        cancellation_poll_interval_ms=1,
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
        str(STAGE_CONTRACT_ROOT),
        "--artifact-root",
        "/tmp/launch-selection-artifacts",
        "--authored-profile-path",
        "/tmp/authored.json",
        "--authored-profile-digest",
        "sha256:" + "a" * 64,
    ]


if __name__ == "__main__":
    unittest.main()
