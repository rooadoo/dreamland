#!/usr/bin/env python3
"""Run the development-only Presenter Stage against a real command agent.

The target is explicit, loopback-only, capability-disabled, and separate from
normal DREAMLAND composition.  It is intended for local UI evaluation, not as
production or conference-readiness evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from dreamland.agent import LangGraphCommandAgent
from dreamland.config import DreamlandSettings, load_settings
from dreamland.product.admission import ProductAdmissionLedger, ProductAdmissionLimits
from dreamland.product.core import ProductCorePolicy
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    LifecyclePolicy,
    ProductOperation,
    ProductRole,
)
from dreamland.product.live_agent import (
    DEFAULT_PUBLICATION_CHUNK_BYTES,
    ExactThreadCheckpointReaper,
    ProductLiveAgentAdapter,
    ReservedMaximumUsageMeter,
)
from dreamland.product.runtime_executor import (
    DreamlandStateProfileCodec,
    ProductRuntimeCommandExecutor,
    ProductStateProfileCodec,
)
from dreamland.product.stage import PresenterStageService, StageDisplayMetadataSeed
from dreamland.state_profiles import StateProfileStore

CONTRACT_REVISION = "0.4.0-rc.2"
LIVE_EXECUTOR = "development-live-langgraph-v1"
LIVE_SCENARIO = "stage-live-development"
MILLISECONDS_PER_MINUTE = 60_000
MAX_COMMANDS = 12
MAX_LOGICAL_JOBS = 1_024
RESERVED_TOKENS_PER_COMMAND = 8_192
RESERVED_SPEND_UNITS_PER_COMMAND = 1_000
COMMAND_DEADLINE_MS = 120_000
MAX_OUTPUT_BYTES_PER_COMMAND = 64 * 1_024
# Every accepted nonempty output chunk accounts for at least one UTF-8 byte.
# This makes the byte boundary, not provider segmentation, the effective limit.
MAX_OUTPUT_CHUNKS_PER_COMMAND = MAX_OUTPUT_BYTES_PER_COMMAND
TOKEN_BUDGET = MAX_LOGICAL_JOBS * RESERVED_TOKENS_PER_COMMAND
SPEND_BUDGET_UNITS = MAX_LOGICAL_JOBS * RESERVED_SPEND_UNITS_PER_COMMAND


def resolve_live_command_reservation(
    max_output_tokens: int | None,
    *,
    max_logical_jobs: int,
) -> int:
    """Return a finite reservation that covers one provider request maximum."""
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens <= 0
        or max_output_tokens >= 2**63
    ):
        raise ValueError(
            "live product max_output_tokens must be a finite positive integer"
        )
    reservation = max(RESERVED_TOKENS_PER_COMMAND, max_output_tokens)
    live_token_budget(reservation, max_logical_jobs=max_logical_jobs)
    return reservation


def live_token_budget(
    command_reserved_tokens: int,
    *,
    max_logical_jobs: int,
) -> int:
    if (
        isinstance(command_reserved_tokens, bool)
        or not isinstance(command_reserved_tokens, int)
        or command_reserved_tokens <= 0
        or isinstance(max_logical_jobs, bool)
        or not isinstance(max_logical_jobs, int)
        or max_logical_jobs <= 0
    ):
        raise ValueError("live product token accounting must use positive integers")
    token_budget = command_reserved_tokens * max_logical_jobs
    if token_budget >= 2**63:
        raise ValueError("live product token budget exceeds the bounded limit")
    return token_budget


def _load_stage_transport() -> ModuleType:
    path = Path(__file__).with_name("product_stage_preview.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_stage_live_transport",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("Presenter Stage transport is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_STAGE: Any = _load_stage_transport()
_CORE: Any = _STAGE._CORE
_LIFECYCLE: Any = _STAGE._LIFECYCLE


@dataclass(frozen=True, slots=True)
class LiveBootstrap:
    actor_context: str
    launch_grant: str


@dataclass(frozen=True, slots=True)
class LiveStageComposition:
    profile_document: Mapping[str, object]
    profile_name: str
    actor_context: str
    launch_grant: str
    executor: ProductRuntimeCommandExecutor
    model: str
    max_output_tokens: int | None
    command_reserved_tokens: int
    max_commands: int | None


class LiveStageHarness(_STAGE.StagePreviewHarness):
    """Use the reviewed Stage transport with a real, explicitly injected agent."""

    def __init__(self, composition: LiveStageComposition) -> None:
        expected_reservation = resolve_live_command_reservation(
            composition.max_output_tokens,
            max_logical_jobs=MAX_LOGICAL_JOBS,
        )
        if composition.command_reserved_tokens != expected_reservation:
            raise ValueError(
                "live Stage command reservation does not match provider maximum"
            )
        self._live_composition = composition
        super().__init__(_STAGE.SCENARIOS[0])
        self.scenario = LIVE_SCENARIO

    def _build_service(self) -> PresenterStageService:
        composition = self._live_composition
        now_ms = _utc_ms()
        seed_expiry = now_ms + 8 * 60 * MILLISECONDS_PER_MINUTE
        canonical = json.dumps(
            composition.profile_document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        revision_digest = hashlib.sha256(canonical).hexdigest()
        revision_id = f"world-revision:{revision_digest[:32]}"
        launch_limits: dict[str, int] = {
            "absolute_ttl_ms": 2 * 60 * MILLISECONDS_PER_MINUTE,
            "idle_ttl_ms": 30 * MILLISECONDS_PER_MINUTE,
            "max_command_bytes": 4_096,
            "command_reserved_tokens": composition.command_reserved_tokens,
            "command_reserved_spend_units": RESERVED_SPEND_UNITS_PER_COMMAND,
            "command_max_attempts": 1,
            "command_deadline_ms": COMMAND_DEADLINE_MS,
        }
        if composition.max_commands is not None:
            launch_limits["max_commands"] = composition.max_commands
        return PresenterStageService(
            revisions=(
                ImmutableRevisionSeed(
                    revision_id=revision_id,
                    document=composition.profile_document,
                ),
            ),
            actors=(
                ActorAuthoritySeed(
                    token=composition.actor_context,
                    actor_id="stage-live-presenter",
                    role=ProductRole.PRESENTER,
                    expires_at_ms=seed_expiry,
                ),
            ),
            launch_grants=(
                LaunchGrantSeed(
                    token=composition.launch_grant,
                    selected_record_id=f"selected-record:{revision_digest[:32]}",
                    actor_id="stage-live-presenter",
                    role=ProductRole.PRESENTER,
                    revision_id=revision_id,
                    expires_at_ms=seed_expiry,
                    allowed_operations=frozenset(
                        {ProductOperation.CREATE, ProductOperation.SWITCH}
                    ),
                    limits=launch_limits,
                    policy_id="policy:stage-live-development-v1",
                    notice_version="notice:stage-live-development-v1",
                ),
            ),
            display_metadata=(
                StageDisplayMetadataSeed(
                    revision_id,
                    "DREAMLAND Live Development Stage",
                    "Local state profile",
                    composition.profile_name,
                ),
            ),
            executor=composition.executor,
            submit_task=self.submit_task,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=2 * 60 * MILLISECONDS_PER_MINUTE,
                idle_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
                tombstone_ttl_ms=10 * MILLISECONDS_PER_MINUTE,
                failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                max_active_sessions=2,
                max_tombstones=4,
                max_idempotency_records=128,
                max_launch_grants=4,
            ),
            core_policy=ProductCorePolicy(
                max_commands_per_session=composition.max_commands,
                command_deadline_ms=COMMAND_DEADLINE_MS,
                cancellation_deadline_ms=5_000,
                max_output_bytes_per_command=MAX_OUTPUT_BYTES_PER_COMMAND,
                max_output_chunks_per_command=MAX_OUTPUT_CHUNKS_PER_COMMAND,
                reserved_tokens_per_command=composition.command_reserved_tokens,
                reserved_spend_units_per_command=RESERVED_SPEND_UNITS_PER_COMMAND,
                max_attempts_per_command=1,
            ),
            admission=ProductAdmissionLedger(
                ProductAdmissionLimits(
                    max_active_sessions=2,
                    max_logical_jobs=MAX_LOGICAL_JOBS,
                    max_physical_workers=2,
                    token_budget=live_token_budget(
                        composition.command_reserved_tokens,
                        max_logical_jobs=MAX_LOGICAL_JOBS,
                    ),
                    spend_budget_units=SPEND_BUDGET_UNITS,
                    presenter_stage_reserved_sessions=1,
                    presenter_stage_reserved_logical_jobs=MAX_LOGICAL_JOBS,
                    presenter_stage_reserved_physical_workers=2,
                    presenter_stage_reserved_token_budget=live_token_budget(
                        composition.command_reserved_tokens,
                        max_logical_jobs=MAX_LOGICAL_JOBS,
                    ),
                    presenter_stage_reserved_spend_budget_units=SPEND_BUDGET_UNITS,
                )
            ),
            clock_ms=_utc_ms,
            monotonic_ms=_monotonic_ms,
            token_factory=_live_token_factory,
        )


def initialize_private_files(*, token_file: Path, bootstrap_file: Path) -> None:
    """Create owner-only BFF and Stage credentials without printing secrets."""
    if token_file.resolve() == bootstrap_file.resolve():
        raise ValueError("token and bootstrap paths must differ")
    bearer = f"preview-bearer:{secrets.token_urlsafe(32)}"
    bootstrap = {
        "actor_context": f"actor:{secrets.token_urlsafe(32)}",
        "launch_grant": f"launch-grant:{secrets.token_urlsafe(32)}",
    }
    _exclusive_owner_file(token_file, f"{bearer}\n")
    _exclusive_owner_file(
        bootstrap_file,
        json.dumps(bootstrap, indent=2, sort_keys=True) + "\n",
    )


def load_live_bootstrap(path: Path) -> LiveBootstrap:
    text = _read_owner_file(path, maximum_bytes=4_096)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("live Stage bootstrap is not valid JSON") from error
    if not isinstance(value, dict) or set(value) != {"actor_context", "launch_grant"}:
        raise ValueError("live Stage bootstrap has invalid fields")
    actor_context = value["actor_context"]
    launch_grant = value["launch_grant"]
    if not isinstance(actor_context, str) or not _CORE.ACTOR_CONTEXT_PATTERN.fullmatch(
        actor_context
    ):
        raise ValueError("live Stage actor context is malformed")
    if not isinstance(launch_grant, str) or not _CORE.LAUNCH_GRANT_PATTERN.fullmatch(
        launch_grant
    ):
        raise ValueError("live Stage launch grant is malformed")
    return LiveBootstrap(actor_context=actor_context, launch_grant=launch_grant)


def build_live_composition(
    *,
    settings: DreamlandSettings,
    profile_name: str,
    bootstrap: LiveBootstrap,
    max_commands: int | None = MAX_COMMANDS,
) -> LiveStageComposition:
    _validate_max_commands(max_commands)
    profile = StateProfileStore().load(profile_name)
    executor, model, max_output_tokens = build_live_executor(
        settings=settings,
        state_codec=DreamlandStateProfileCodec(),
    )
    command_reserved_tokens = resolve_live_command_reservation(
        max_output_tokens,
        max_logical_jobs=MAX_LOGICAL_JOBS,
    )
    return LiveStageComposition(
        profile_document=profile.to_document(),
        profile_name=profile.name,
        actor_context=bootstrap.actor_context,
        launch_grant=bootstrap.launch_grant,
        executor=executor,
        model=model,
        max_output_tokens=max_output_tokens,
        command_reserved_tokens=command_reserved_tokens,
        max_commands=max_commands,
    )


def build_live_executor(
    *,
    settings: DreamlandSettings,
    state_codec: ProductStateProfileCodec,
) -> tuple[ProductRuntimeCommandExecutor, str, int | None]:
    """Build the isolated live command executor with an explicit state codec.

    The ordinary live Stage supplies ``DreamlandStateProfileCodec``.  The
    explicit Forge-to-Stage development target can inject its Forge-aware codec
    without adding any composition to normal DREAMLAND startup.
    """
    # Admission must have a finite worst case before provider construction.
    resolve_live_command_reservation(
        settings.agent.max_output_tokens,
        max_logical_jobs=MAX_LOGICAL_JOBS,
    )
    model_reference = settings.agent.model_reference
    api_key = _provider_api_key(settings, model_reference.provider)
    if not api_key:
        raise ValueError(
            f"{model_reference.provider} API key is required for live Stage development"
        )
    try:
        from langgraph.checkpoint.memory import InMemorySaver
    except ImportError as error:
        raise ValueError("LangGraph checkpoint support is unavailable") from error
    checkpointer = InMemorySaver()
    command_agent = LangGraphCommandAgent(
        provider=model_reference.provider,
        model_name=model_reference.model,
        system_prompt_name=settings.agent.system_prompt_name,
        reasoning_effort=settings.agent.reasoning_effort,
        reasoning_summary=settings.agent.reasoning_summary,
        verbosity=settings.agent.verbosity,
        speed=settings.agent.speed,
        prompt_cache=settings.agent.prompt_cache,
        openai_api=settings.agent.openai_api,
        openai_service_tier=settings.agent.openai_service_tier,
        openai_reasoning_mode=settings.agent.openai_reasoning_mode,
        max_output_tokens=settings.agent.max_output_tokens,
        api_key=api_key,
        langfuse_enabled=settings.langfuse.enabled,
        langfuse_public_key=settings.langfuse.public_key,
        langfuse_secret_key=settings.langfuse.secret_key,
        langfuse_host=settings.langfuse.host,
        checkpointer=checkpointer,
    )
    executor = ProductRuntimeCommandExecutor(
        agent=ProductLiveAgentAdapter(
            command_agent,
            publication_chunk_bytes=DEFAULT_PUBLICATION_CHUNK_BYTES,
        ),
        state_codec=state_codec,
        checkpoint_reaper=ExactThreadCheckpointReaper(checkpointer.delete_thread),
        usage_meter=ReservedMaximumUsageMeter(),
        clock_ms=_monotonic_ms,
        cancellation_poll_interval_ms=25,
    )
    return (
        executor,
        f"{model_reference.provider}:{model_reference.model}",
        settings.agent.max_output_tokens,
    )


def serve_live(
    *,
    host: str,
    port: int,
    bearer: str,
    contract_root: Path,
    composition: LiveStageComposition,
    once: bool,
) -> None:
    if not _LIFECYCLE._loopback_host(host):
        raise ValueError("live Stage development supports only loopback hosts")
    if not 0 <= port <= 65_535:
        raise ValueError("live Stage port must be between 0 and 65535")
    _LIFECYCLE._validate_preview_bearer(bearer)
    root = validate_live_contract_root(contract_root)
    _configure_live_transport()
    server = _STAGE.ProductStagePreviewServer(
        (host, port),
        bearer=bearer,
        scenario=LIVE_SCENARIO,
        contract_root=root,
        harness_factory=lambda _scenario: LiveStageHarness(composition),
    )
    address, bound_port = server.server_address[:2]
    print(
        json.dumps(
            {
                "capabilities_enabled": 0,
                "contract_revision": CONTRACT_REVISION,
                "development_only": True,
                "executor": LIVE_EXECUTOR,
                "host": address,
                "live_capability_evidence": False,
                "max_commands": composition.max_commands,
                "max_output_tokens": composition.max_output_tokens,
                "command_reserved_tokens": composition.command_reserved_tokens,
                "model": composition.model,
                "output_publication": {
                    "flush_bytes": DEFAULT_PUBLICATION_CHUNK_BYTES,
                    "flush_on_newline": True,
                    "max_bytes_per_command": MAX_OUTPUT_BYTES_PER_COMMAND,
                },
                "port": bound_port,
                "profile": composition.profile_name,
                "provider_calls": True,
                "token_budget": live_token_budget(
                    composition.command_reserved_tokens,
                    max_logical_jobs=MAX_LOGICAL_JOBS,
                ),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        if once:
            server.handle_request_synchronously()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        return
    finally:
        server.server_close()


def validate_live_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    tool_path = Path(__file__).with_name("product_stage_live_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_stage_live_contract_validator",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("live Stage contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("live Stage contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != CONTRACT_REVISION
        or result.get("capabilities_enabled") != 0
        or result.get("routes") != 15
    ):
        raise ValueError("live Stage contract is incompatible")
    return root


def _configure_live_transport() -> None:
    _STAGE.CONTRACT_REVISION = CONTRACT_REVISION
    _STAGE.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    _CORE.CONTRACT_REVISION = CONTRACT_REVISION
    _CORE.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    _CORE.PREVIEW_PROVIDER_CALLS = True
    _CORE.PREVIEW_TEST_ONLY = True


def _provider_api_key(settings: DreamlandSettings, provider: str) -> str | None:
    if provider == "openai":
        return settings.openai.api_key
    if provider == "anthropic":
        return settings.anthropic.api_key
    if provider == "xai":
        return settings.xai.api_key
    return None


def _live_token_factory(purpose: str) -> str:
    prefixes = {
        "service_epoch": "epoch:",
        "product_session_id": "session:",
        "product_reference": "session-ref:",
        "session_authority": "session-cap:",
    }
    return f"{prefixes.get(purpose, 'product-opaque:')}{secrets.token_urlsafe(32)}"


def _exclusive_owner_file(path: Path, text: str) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _read_owner_file(path: Path, *, maximum_bytes: int) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(
            "private live Stage file must be a regular non-symlink"
        ) from error
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("private live Stage file must be a regular non-symlink")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError(
                "private live Stage file must be owned by the current user"
            )
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError(
                "private live Stage file must not grant group or world access"
            )
        data = stream.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        raise ValueError("private live Stage file is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("private live Stage file must be UTF-8") from error


def _utc_ms() -> int:
    return time.time_ns() // 1_000_000


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _parse_max_commands(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "max commands must be -1 or a positive integer"
        ) from error
    if parsed == -1:
        return None
    try:
        _validate_max_commands(parsed)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return parsed


def _validate_max_commands(value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("max commands must be -1 or a positive integer")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the development-only live DREAMLAND Presenter Stage."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="create owner-only local credentials")
    initialize.add_argument("--token-file", required=True, type=Path)
    initialize.add_argument("--bootstrap-file", required=True, type=Path)
    serve = commands.add_parser("serve", help="start the explicit loopback target")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--token-file", required=True, type=Path)
    serve.add_argument("--bootstrap-file", required=True, type=Path)
    serve.add_argument("--contract-root", required=True, type=Path)
    serve.add_argument("--profile")
    serve.add_argument(
        "--max-commands",
        default=MAX_COMMANDS,
        type=_parse_max_commands,
    )
    serve.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "init":
            initialize_private_files(
                token_file=arguments.token_file,
                bootstrap_file=arguments.bootstrap_file,
            )
            print(
                json.dumps(
                    {
                        "bootstrap_file": str(arguments.bootstrap_file),
                        "created": True,
                        "token_file": str(arguments.token_file),
                    },
                    sort_keys=True,
                )
            )
            return 0
        settings = load_settings()
        bearer = _LIFECYCLE.load_preview_bearer_file(arguments.token_file)
        bootstrap = load_live_bootstrap(arguments.bootstrap_file)
        composition = build_live_composition(
            settings=settings,
            profile_name=arguments.profile or settings.state_profile.profile,
            bootstrap=bootstrap,
            max_commands=arguments.max_commands,
        )
        serve_live(
            host=arguments.host,
            port=arguments.port,
            bearer=bearer,
            contract_root=arguments.contract_root,
            composition=composition,
            once=arguments.once,
        )
    except (OSError, ValueError) as error:
        print(f"Live Presenter Stage error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
