from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dreamland.product.execution import (
    ProductExecutionResult,
    ProductOutputChunk,
    ProductOutputStream,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_stage_live.py"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.4.0-rc.2"
CLIENT_PATH = CONTRACT_ROOT / "generated" / "product_session_client.py"


def _load(path: Path, name: str):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {path.name}")
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


TOOL = _load(TOOL_PATH, "dreamland_product_stage_live_unit_tool")
CLIENT = _load(CLIENT_PATH, "dreamland_product_stage_live_unit_client")


class ProductStageLiveTests(unittest.TestCase):
    def test_command_reservation_covers_provider_maximum_and_budget(self) -> None:
        self.assertEqual(
            TOOL.resolve_live_command_reservation(
                12_800,
                max_logical_jobs=TOOL.MAX_LOGICAL_JOBS,
            ),
            12_800,
        )
        self.assertEqual(
            TOOL.resolve_live_command_reservation(
                TOOL.RESERVED_TOKENS_PER_COMMAND,
                max_logical_jobs=TOOL.MAX_LOGICAL_JOBS,
            ),
            TOOL.RESERVED_TOKENS_PER_COMMAND,
        )
        self.assertEqual(
            TOOL.resolve_live_command_reservation(
                2_048,
                max_logical_jobs=TOOL.MAX_LOGICAL_JOBS,
            ),
            TOOL.RESERVED_TOKENS_PER_COMMAND,
        )
        self.assertEqual(
            TOOL.live_token_budget(12_800, max_logical_jobs=1_024),
            13_107_200,
        )
        with self.assertRaisesRegex(ValueError, "token budget"):
            TOOL.resolve_live_command_reservation(
                2**62,
                max_logical_jobs=4,
            )
        for value in (None, 0, False):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ValueError,
                    "finite positive integer",
                ),
            ):
                TOOL.resolve_live_command_reservation(
                    value,
                    max_logical_jobs=TOOL.MAX_LOGICAL_JOBS,
                )

    def test_unbounded_provider_maximum_fails_before_agent_construction(self) -> None:
        settings = TOOL.load_settings(Path("/definitely-missing-stage-live.ini"))
        settings = replace(
            settings,
            agent=replace(settings.agent, max_output_tokens=None),
        )
        with (
            patch.object(TOOL, "LangGraphCommandAgent") as command_agent,
            self.assertRaisesRegex(ValueError, "finite positive integer"),
        ):
            TOOL.build_live_executor(
                settings=settings,
                state_codec=TOOL.DreamlandStateProfileCodec(),
            )
        command_agent.assert_not_called()

    def test_configured_maximum_reaches_provider_with_matching_reservation(
        self,
    ) -> None:
        settings = TOOL.load_settings(Path("/definitely-missing-stage-live.ini"))
        settings = replace(
            settings,
            openai=replace(settings.openai, api_key="provider-free-test-key"),
            agent=replace(
                settings.agent,
                max_output_tokens=12_800,
                openai_api="responses",
                openai_service_tier="default",
            ),
        )
        with patch.object(TOOL, "LangGraphCommandAgent") as command_agent:
            _executor, _model, provider_maximum = TOOL.build_live_executor(
                settings=settings,
                state_codec=TOOL.DreamlandStateProfileCodec(),
            )

        self.assertEqual(provider_maximum, 12_800)
        self.assertEqual(command_agent.call_args.kwargs["openai_api"], "responses")
        self.assertEqual(
            command_agent.call_args.kwargs["openai_service_tier"], "default"
        )
        self.assertEqual(
            command_agent.call_args.kwargs["max_output_tokens"],
            provider_maximum,
        )
        self.assertEqual(
            TOOL.resolve_live_command_reservation(
                provider_maximum,
                max_logical_jobs=TOOL.MAX_LOGICAL_JOBS,
            ),
            provider_maximum,
        )

    def test_import_and_construction_are_inert_until_explicitly_served(self) -> None:
        before = {thread.name for thread in threading.enumerate()}
        composition = _fake_composition()

        self.assertEqual({thread.name for thread in threading.enumerate()}, before)
        self.assertEqual(composition.profile_name, "default")

    def test_configured_12800_is_used_by_policy_launch_and_admission(self) -> None:
        composition = _fake_composition(max_output_tokens=12_800)
        harness = TOOL.LiveStageHarness(composition)
        try:
            self.assertEqual(
                harness.service._core_policy.reserved_tokens_per_command,
                12_800,
            )
            self.assertEqual(
                harness.service.admission.limits.token_budget,
                TOOL.MAX_LOGICAL_JOBS * 12_800,
            )
            launched = harness.service.launch(
                actor_authority=composition.actor_context,
                launch_grant=composition.launch_grant,
                expected_epoch=harness.service.service_epoch,
                idempotency_key="idempotency:accounting_launch_0001",
            )
            self.assertEqual(
                launched["snapshot"]["launch_binding"]["limits"][
                    "command_reserved_tokens"
                ],
                12_800,
            )
        finally:
            harness.close()

        with self.assertRaisesRegex(ValueError, "does not match provider maximum"):
            TOOL.LiveStageHarness(
                replace(
                    composition,
                    command_reserved_tokens=TOOL.RESERVED_TOKENS_PER_COMMAND,
                )
            )

    def test_init_creates_owner_only_unprinted_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_file = root / "private.token"
            bootstrap_file = root / "private-bootstrap.json"

            TOOL.initialize_private_files(
                token_file=token_file,
                bootstrap_file=bootstrap_file,
            )
            bootstrap = TOOL.load_live_bootstrap(bootstrap_file)

            self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(bootstrap_file.stat().st_mode), 0o600)
            self.assertTrue(bootstrap.actor_context.startswith("actor:"))
            self.assertTrue(bootstrap.launch_grant.startswith("launch-grant:"))
            with self.assertRaises(FileExistsError):
                TOOL.initialize_private_files(
                    token_file=token_file,
                    bootstrap_file=root / "another.json",
                )

    def test_bootstrap_rejects_symlink_and_overbroad_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "bootstrap.json"
            target.write_text(
                json.dumps(
                    {
                        "actor_context": "actor:stage_live_presenter_0001",
                        "launch_grant": "launch-grant:stage_live_launch_grant_0001",
                    }
                )
            )
            target.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "group or world"):
                TOOL.load_live_bootstrap(target)
            target.chmod(0o600)
            link = root / "bootstrap-link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                TOOL.load_live_bootstrap(link)

    def test_generated_client_drives_live_context_stage_and_command(self) -> None:
        with _running_server(_fake_composition()) as running:
            client, composition = running
            capabilities = client.capabilities()
            epoch = capabilities["service_epoch"]
            launched = client.launch_stage(
                epoch=epoch,
                actor=composition.actor_context,
                key="idempotency:live_launch_0001",
                launch_grant=composition.launch_grant,
            )
            snapshot = launched["snapshot"]
            accepted = client.submit(
                epoch=epoch,
                actor=composition.actor_context,
                capability=launched["session_authority"],
                reference=snapshot["reference"],
                key="idempotency:live_command_0001",
                command="pwd",
                expected_slot_generation=snapshot["command_slot"]["generation"],
            )
            terminal = _wait_for_terminal(
                client,
                epoch=epoch,
                actor=composition.actor_context,
                capability=launched["session_authority"],
                reference=snapshot["reference"],
                command_id=accepted["command_id"],
            )
            self.assertEqual(terminal["status"], "completed")
            self.assertEqual(
                capabilities["preview"]["executor"],
                TOOL.LIVE_EXECUTOR,
            )
            self.assertTrue(capabilities["preview"]["provider_calls"])
            self.assertTrue(
                all(
                    not value["enabled"]
                    for value in capabilities["capabilities"].values()
                )
            )

    def test_cli_accepts_positive_and_unlimited_limits_and_rejects_invalid(
        self,
    ) -> None:
        parser = TOOL._parser()
        for value, expected in (("7", 7), ("-1", None)):
            arguments = parser.parse_args(_serve_arguments(value))
            self.assertEqual(arguments.max_commands, expected)
        for value in ("0", "-2"):
            with self.assertRaises(SystemExit):
                parser.parse_args(_serve_arguments(value))
        self.assertEqual(
            parser.parse_args(_serve_arguments()).max_commands,
            TOOL.MAX_COMMANDS,
        )

    def test_positive_limit_rejects_only_command_after_configured_count(self) -> None:
        with _running_server(_fake_composition(max_commands=2)) as running:
            client, composition = running
            session = _launch(client, composition, "positive_limit")
            for index in range(2):
                _submit_and_wait(client, session, index)
            with self.assertRaises(CLIENT.ProductSessionPreviewError) as caught:
                _submit_and_wait(client, session, 2)

            self.assertEqual(
                caught.exception.payload["error"]["code"],
                "command_limit_reached",
            )

    def test_unlimited_mode_accepts_command_thirteen_and_keeps_safeguards(self) -> None:
        composition = _fake_composition(max_commands=None)
        harness = TOOL.LiveStageHarness(composition)
        try:
            policy = harness.service._core_policy
            limits = harness.service.admission.limits
            self.assertIsNone(policy.max_commands_per_session)
            self.assertEqual(policy.command_deadline_ms, TOOL.COMMAND_DEADLINE_MS)
            self.assertEqual(
                policy.max_output_bytes_per_command,
                TOOL.MAX_OUTPUT_BYTES_PER_COMMAND,
            )
            self.assertEqual(
                policy.max_output_chunks_per_command,
                TOOL.MAX_OUTPUT_CHUNKS_PER_COMMAND,
            )
            self.assertEqual(policy.max_attempts_per_command, 1)
            self.assertEqual(
                policy.reserved_tokens_per_command,
                composition.command_reserved_tokens,
            )
            self.assertEqual(limits.max_physical_workers, 2)
            self.assertEqual(limits.max_logical_jobs, TOOL.MAX_LOGICAL_JOBS)
            self.assertEqual(limits.token_budget, TOOL.TOKEN_BUDGET)
            self.assertEqual(limits.spend_budget_units, TOOL.SPEND_BUDGET_UNITS)
        finally:
            harness.close()

        with _running_server(composition) as running:
            client, running_composition = running
            session = _launch(client, running_composition, "unlimited_limit")
            self.assertNotIn(
                "max_commands", session["snapshot"]["launch_binding"]["limits"]
            )
            for index in range(13):
                terminal = _submit_and_wait(client, session, index)
                self.assertEqual(terminal["status"], "completed")
            with self.assertRaises(CLIENT.ProductSessionPreviewError) as caught:
                _submit_and_wait(client, session, 13, command="x" * 4_097)
            self.assertEqual(
                caught.exception.payload["error"]["code"], "command_too_large"
            )

    def test_more_than_128_small_publications_remain_complete(self) -> None:
        class FragmentedExecutor:
            def execute(self, request, emit):
                for index in range(300):
                    emit(
                        ProductOutputChunk(
                            ProductOutputStream.STDOUT,
                            f"line-{index:03d}\n",
                        )
                    )
                return ProductExecutionResult.completed_exited(
                    exit_status=0,
                    usage_tokens=min(request.max_tokens, 1),
                    usage_spend_units=min(request.max_spend_units, 1),
                )

        composition = _fake_composition(executor=FragmentedExecutor())
        with _running_server(composition) as running:
            client, running_composition = running
            session = _launch(client, running_composition, "fragmented_output")
            terminal = _submit_and_wait(client, session, 0)
            transcript = client.transcript(
                epoch=session["epoch"],
                actor=session["actor"],
                capability=session["capability"],
                reference=session["reference"],
            )

        expected = "".join(f"line-{index:03d}\n" for index in range(300))
        published = "".join(
            entry["text"]
            for entry in transcript["entries"]
            if entry["kind"] == "stdout" and not entry["final"]
        )
        self.assertEqual(published, expected)
        self.assertEqual(terminal["output"]["chunks"], 300)
        self.assertFalse(terminal["output"]["clipped"])

    def test_startup_reports_nonsecret_resolved_configuration(self) -> None:
        composition = _fake_composition(max_commands=None)

        class FakeServer:
            server_address = ("127.0.0.1", 43123)

            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def handle_request_synchronously(self) -> None:
                pass

            def server_close(self) -> None:
                pass

        output = io.StringIO()
        with (
            patch.object(
                TOOL, "validate_live_contract_root", return_value=CONTRACT_ROOT
            ),
            patch.object(TOOL._STAGE, "ProductStagePreviewServer", FakeServer),
            redirect_stdout(output),
        ):
            TOOL.serve_live(
                host="127.0.0.1",
                port=0,
                bearer="preview-bearer:stage_live_test_00000001",
                contract_root=CONTRACT_ROOT,
                composition=composition,
                once=True,
            )

        announcement = json.loads(output.getvalue())
        self.assertIsNone(announcement["max_commands"])
        self.assertEqual(announcement["model"], "openai:test-model")
        self.assertEqual(announcement["max_output_tokens"], 2_048)
        self.assertEqual(
            announcement["command_reserved_tokens"],
            TOOL.RESERVED_TOKENS_PER_COMMAND,
        )
        self.assertEqual(announcement["token_budget"], TOOL.TOKEN_BUDGET)
        self.assertEqual(
            announcement["output_publication"],
            {
                "flush_bytes": TOOL.DEFAULT_PUBLICATION_CHUNK_BYTES,
                "flush_on_newline": True,
                "max_bytes_per_command": TOOL.MAX_OUTPUT_BYTES_PER_COMMAND,
            },
        )
        self.assertNotIn("actor_context", announcement)
        self.assertNotIn("launch_grant", announcement)

    @unittest.skipUnless(
        os.environ.get("DREAMLAND_LIVE_PROVIDER_TESTS") == "1",
        "Set DREAMLAND_LIVE_PROVIDER_TESTS=1 to run the live Stage provider test",
    )
    def test_opt_in_real_agent_preserves_state_across_commands(self) -> None:
        settings = TOOL.load_settings()
        bootstrap = TOOL.LiveBootstrap(
            actor_context="actor:stage_live_provider_test_0001",
            launch_grant="launch-grant:stage_live_provider_grant_test_0001",
        )
        composition = TOOL.build_live_composition(
            settings=settings,
            profile_name=settings.state_profile.profile,
            bootstrap=bootstrap,
        )
        with _running_server(composition) as running:
            client, _composition = running
            capabilities = client.capabilities()
            epoch = capabilities["service_epoch"]
            launched = client.launch_stage(
                epoch=epoch,
                actor=composition.actor_context,
                key="idempotency:provider_launch_0001",
                launch_grant=composition.launch_grant,
            )
            snapshot = launched["snapshot"]
            capability = launched["session_authority"]
            reference = snapshot["reference"]
            generation = snapshot["command_slot"]["generation"]
            for index, command in enumerate(
                ("ls -la", "touch phase3d-live-marker", "ls -la"),
                start=1,
            ):
                accepted = client.submit(
                    epoch=epoch,
                    actor=composition.actor_context,
                    capability=capability,
                    reference=reference,
                    key=f"idempotency:provider_command_{index:04d}",
                    command=command,
                    expected_slot_generation=generation,
                )
                terminal = _wait_for_terminal(
                    client,
                    epoch=epoch,
                    actor=composition.actor_context,
                    capability=capability,
                    reference=reference,
                    command_id=accepted["command_id"],
                    timeout_seconds=150,
                )
                self.assertEqual(terminal["status"], "completed")
                generation = terminal["slot_generation"]
            transcript = client.transcript(
                epoch=epoch,
                actor=composition.actor_context,
                capability=capability,
                reference=reference,
            )
            self.assertIn("phase3d-live-marker", json.dumps(transcript))


class _RunningServer:
    def __init__(self, composition) -> None:
        TOOL._configure_live_transport()
        self.composition = composition
        self.server = TOOL._STAGE.ProductStagePreviewServer(
            ("127.0.0.1", 0),
            bearer="preview-bearer:stage_live_test_00000001",
            scenario=TOOL.LIVE_SCENARIO,
            contract_root=TOOL.validate_live_contract_root(CONTRACT_ROOT),
            harness_factory=lambda _scenario: TOOL.LiveStageHarness(composition),
        )
        self.thread = threading.Thread(target=self.server.serve_forever)

    def __enter__(self):
        self.thread.start()
        port = self.server.server_address[1]
        client = CLIENT.ProductSessionPreviewClient(
            f"http://127.0.0.1:{port}",
            "preview-bearer:stage_live_test_00000001",
        )
        return client, self.composition

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def _running_server(composition) -> _RunningServer:
    return _RunningServer(composition)


def _fake_composition(
    *,
    max_commands=TOOL.MAX_COMMANDS,
    executor=None,
    max_output_tokens=2_048,
):
    profile = TOOL.StateProfileStore().load("default")
    command_reserved_tokens = TOOL.resolve_live_command_reservation(
        max_output_tokens,
        max_logical_jobs=TOOL.MAX_LOGICAL_JOBS,
    )
    return TOOL.LiveStageComposition(
        profile_document=profile.to_document(),
        profile_name=profile.name,
        actor_context="actor:stage_live_presenter_0001",
        launch_grant="launch-grant:stage_live_launch_grant_0001",
        executor=executor or TOOL._CORE.DeterministicPreviewExecutor(),
        model="openai:test-model",
        max_output_tokens=max_output_tokens,
        command_reserved_tokens=command_reserved_tokens,
        max_commands=max_commands,
    )


def _serve_arguments(max_commands: str | None = None) -> list[str]:
    arguments = [
        "serve",
        "--token-file",
        "/tmp/live.token",
        "--bootstrap-file",
        "/tmp/live-bootstrap.json",
        "--contract-root",
        str(CONTRACT_ROOT),
    ]
    if max_commands is not None:
        arguments.extend(("--max-commands", max_commands))
    return arguments


def _launch(client, composition, key_suffix: str):
    capabilities = client.capabilities()
    launched = client.launch_stage(
        epoch=capabilities["service_epoch"],
        actor=composition.actor_context,
        key=f"idempotency:{key_suffix}_launch_0001",
        launch_grant=composition.launch_grant,
    )
    return {
        "actor": composition.actor_context,
        "capability": launched["session_authority"],
        "client": client,
        "epoch": capabilities["service_epoch"],
        "generation": launched["snapshot"]["command_slot"]["generation"],
        "key_suffix": key_suffix,
        "reference": launched["snapshot"]["reference"],
        "snapshot": launched["snapshot"],
    }


def _submit_and_wait(client, session, index: int, *, command: str = "pwd"):
    accepted = client.submit(
        epoch=session["epoch"],
        actor=session["actor"],
        capability=session["capability"],
        reference=session["reference"],
        key=f"idempotency:{session['key_suffix']}_command_{index:04d}",
        command=command,
        expected_slot_generation=session["generation"],
    )
    terminal = _wait_for_terminal(
        client,
        epoch=session["epoch"],
        actor=session["actor"],
        capability=session["capability"],
        reference=session["reference"],
        command_id=accepted["command_id"],
    )
    session["generation"] = terminal["slot_generation"]
    return terminal


def _wait_for_terminal(
    client,
    *,
    epoch: str,
    actor: str,
    capability: str,
    reference: str,
    command_id: str,
    timeout_seconds: float = 5,
):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.command_result(
            epoch=epoch,
            actor=actor,
            capability=capability,
            reference=reference,
            command_id=command_id,
        )
        command = response["command"]
        if command["status"] in {"completed", "interrupted", "failed"}:
            return command
        time.sleep(0.01)
    raise AssertionError("live Stage command did not become terminal")


if __name__ == "__main__":
    unittest.main()
