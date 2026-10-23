#!/usr/bin/env python3
"""Run the explicit single-attempt live World Forge vertical slice."""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from dreamland.config import DreamlandSettings, load_settings
from dreamland.product.forge import (
    ForgeActorSeed,
    ForgeJobState,
    ForgePolicy,
    ForgeServiceError,
    WorldForgeService,
    world_seed_from_document,
)
from dreamland.product.forge_artifacts import (
    ImmutableForgeStore,
    SingleAttemptForge,
    live_forge_source,
)
from dreamland.product.forge_live import SpiritguideForgeProfileGenerator
from dreamland.runtime_logging import configure_console_logging
from dreamland.spiritguide import ModelSpiritguide

CONTRACT_REVISION = "0.1.0-rc.4"
LIVE_SCENARIO = "single-attempt-live-vertical-slice"
DEFAULT_DEADLINE_SECONDS = 120
LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")
FAILURE_LOGGER = logging.getLogger("dreamland.failures")


def _load_preview_transport() -> ModuleType:
    path = Path(__file__).with_name("product_forge_preview.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_world_forge_live_transport", path
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("World Forge preview transport is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_TRANSPORT: Any = _load_preview_transport()


def _live_source() -> dict[str, object]:
    return live_forge_source()


class LiveForgeHarness:
    def __init__(
        self,
        *,
        actor_context: str,
        runner: SingleAttemptForge,
        deadline_seconds: int,
        maximum_jobs: int = 1,
        selected_handoff_ttl_seconds: int = 300,
        actor_expires_at: datetime | None = None,
        on_selected: Callable[[str], object] | None = None,
    ) -> None:
        self.scenario = LIVE_SCENARIO
        self.actor_context = actor_context
        self.runner = runner
        self._on_selected = on_selected
        now = datetime.now(UTC)
        expiry = actor_expires_at or (now + timedelta(hours=12))
        if not isinstance(expiry, datetime) or expiry.tzinfo is None or expiry <= now:
            raise ValueError("live World Forge actor expiry must be future and zoned")
        expiry = expiry.astimezone(UTC)
        self.service = WorldForgeService(
            actors=(
                ForgeActorSeed(
                    authority=actor_context,
                    actor_id="forge-live-presenter",
                    expires_at=expiry.isoformat(),
                ),
            ),
            policy=ForgePolicy(
                maximum_jobs=maximum_jobs,
                maximum_idempotency_records=128,
                deadline_seconds=deadline_seconds,
                selected_handoff_ttl_seconds=selected_handoff_ttl_seconds,
            ),
            source=_live_source(),
        )
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="dreamland-forge-live"
        )
        self._futures: set[Future[object]] = set()
        self._closed = threading.Event()
        self._lock = threading.Lock()

    def schedule(self, reference: str) -> None:
        LIFECYCLE_LOGGER.info("world_forge_job_scheduled reference=%s", reference)
        for callback in (self._run, self._watch_deadline):
            future = self._executor.submit(callback, reference)
            with self._lock:
                self._futures.add(future)
            future.add_done_callback(self._forget)

    def consume_submit_response_loss(self) -> bool:
        return False

    def close(self) -> None:
        self._closed.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _forget(self, future: Future[object]) -> None:
        with self._lock:
            self._futures.discard(future)

    def _run(self, reference: str) -> None:
        try:
            LIFECYCLE_LOGGER.info("world_forge_job_started reference=%s", reference)
            self.service.preview_transition(
                reference=reference, state=ForgeJobState.QUEUED
            )
            self.service.preview_transition(
                reference=reference, state=ForgeJobState.BUILDING
            )
            snapshot = self._snapshot(reference)
            seed = world_seed_from_document(snapshot["seed"])
            result = self.runner.run(seed)
            self.service.preview_transition(
                reference=reference, state=ForgeJobState.GENERATED
            )
            self.service.preview_transition(
                reference=reference, state=ForgeJobState.VALIDATING
            )
            projection = self.service.complete_selected(
                reference=reference, receipt=result.receipt
            )
            if projection["lifecycle"]["state"] == ForgeJobState.SELECTED.value:
                revision = result.receipt["revision"]
                revision_id = (
                    revision.get("revision_id")
                    if isinstance(revision, dict)
                    else "unavailable"
                )
                LIFECYCLE_LOGGER.info(
                    "world_forge_job_selected reference=%s revision_id=%s",
                    reference,
                    revision_id,
                )
                if self._on_selected is not None:
                    try:
                        self._on_selected(reference)
                    except Exception as error:
                        FAILURE_LOGGER.error(
                            "world_forge_launch_option_registration_failed "
                            "reference=%s failure_type=%s",
                            reference,
                            type(error).__name__,
                        )
            else:
                LIFECYCLE_LOGGER.info(
                    "world_forge_job_result_fenced reference=%s terminal_state=%s",
                    reference,
                    projection["lifecycle"]["state"],
                )
        except ForgeServiceError:
            LIFECYCLE_LOGGER.info(
                "world_forge_job_result_fenced reference=%s", reference
            )
            return
        except Exception as error:
            FAILURE_LOGGER.error(
                "world_forge_job_failed reference=%s failure_type=%s",
                reference,
                type(error).__name__,
            )
            try:
                self.service.preview_transition(
                    reference=reference,
                    state=ForgeJobState.FAILED,
                    safe_message="The live World Forge candidate could not be validated.",
                )
            except ForgeServiceError:
                return

    def _watch_deadline(self, reference: str) -> None:
        try:
            snapshot = self._snapshot(reference)
            deadline = datetime.fromisoformat(
                str(snapshot["deadline_at"]).replace("Z", "+00:00")
            )
            while True:
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    break
                if self._closed.wait(min(remaining, 0.1)):
                    return
                if bool(self._snapshot(reference)["terminal"]):
                    return
            self.service.preview_transition(
                reference=reference,
                state=ForgeJobState.DEADLINE_EXCEEDED,
                safe_message="The live World Forge attempt exceeded its deadline.",
            )
            FAILURE_LOGGER.warning(
                "world_forge_job_deadline_exceeded reference=%s", reference
            )
        except ForgeServiceError:
            return

    def _snapshot(self, reference: str) -> dict[str, object]:
        return self.service.status(
            actor_authority=self.actor_context,
            expected_epoch=self.service.service_epoch,
            reference=reference,
        )


def _provider_key(settings: DreamlandSettings, provider: str) -> str | None:
    if provider == "openai":
        return settings.openai.api_key
    if provider == "xai":
        return settings.xai.api_key
    if provider == "anthropic":
        return settings.anthropic.api_key
    return None


def build_runner(
    settings: DreamlandSettings, artifact_root: Path
) -> tuple[SingleAttemptForge, str, int | None]:
    model = settings.spiritguide.model_reference
    api_key = _provider_key(settings, model.provider)
    if api_key is None:
        raise ValueError(f"{model.provider} API key is required for live World Forge")
    spiritguide = ModelSpiritguide(
        settings.spiritguide,
        api_key=api_key,
        langfuse_enabled=settings.langfuse.enabled,
        langfuse_public_key=settings.langfuse.public_key,
        langfuse_secret_key=settings.langfuse.secret_key,
        langfuse_host=settings.langfuse.host,
    )
    generator = SpiritguideForgeProfileGenerator(
        spiritguide=spiritguide,
        candidate_directory=artifact_root / "candidates",
    )
    return (
        SingleAttemptForge(generator, ImmutableForgeStore(artifact_root / "revisions")),
        f"{model.provider}:{model.model}",
        settings.spiritguide.max_output_tokens,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the live World Forge vertical slice."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--token-file", required=True, type=Path)
    initialize.add_argument("--bootstrap-file", required=True, type=Path)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--token-file", required=True, type=Path)
    serve.add_argument("--bootstrap-file", required=True, type=Path)
    serve.add_argument("--contract-root", required=True, type=Path)
    serve.add_argument("--artifact-root", required=True, type=Path)
    serve.add_argument("--deadline-seconds", type=int, default=DEFAULT_DEADLINE_SECONDS)
    serve.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "init":
            _TRANSPORT.initialize_private_files(
                token_file=arguments.token_file,
                bootstrap_file=arguments.bootstrap_file,
            )
            print(json.dumps({"created": True}, sort_keys=True))
            return 0
        if arguments.deadline_seconds <= 0:
            raise ValueError("deadline seconds must be positive")
        if arguments.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("live World Forge may bind only to explicit loopback")
        if not 0 <= arguments.port <= 65_535:
            raise ValueError("live World Forge port must be between 0 and 65535")
        settings = load_settings()
        configure_console_logging(settings.logging, log_capacity=0)
        bootstrap = _TRANSPORT.load_bootstrap(arguments.bootstrap_file)
        bearer = _TRANSPORT.load_bearer(arguments.token_file)
        runner, model, max_output_tokens = build_runner(
            settings, arguments.artifact_root
        )
        validated = _TRANSPORT._load_contract_tool().validate_contract(
            arguments.contract_root
        )
        if validated["contract_revision"] != CONTRACT_REVISION:
            raise ValueError("live World Forge requires the exact current contract RC")
        harness = LiveForgeHarness(
            actor_context=bootstrap.actor_context,
            runner=runner,
            deadline_seconds=arguments.deadline_seconds,
        )
        server = _TRANSPORT.ForgePreviewServer(
            (arguments.host, arguments.port),
            bearer=bearer,
            harness=harness,
            contract_revision=CONTRACT_REVISION,
        )
        print(
            json.dumps(
                {
                    "contract_revision": CONTRACT_REVISION,
                    "host": server.server_address[0],
                    "port": server.server_address[1],
                    "scenario": LIVE_SCENARIO,
                    "model": model,
                    "max_output_tokens": max_output_tokens,
                    "deadline_seconds": arguments.deadline_seconds,
                    "source": _live_source(),
                    "normal_runtime_composition": False,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        try:
            if arguments.once:
                server.handle_request_synchronously()
            else:
                server.serve_forever()
        finally:
            server.server_close()
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        print(f"Live World Forge error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
