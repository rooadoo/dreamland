#!/usr/bin/env python3
"""Run the explicit live source-neutral Forge/Stage development target.

This tool is the provider-backed counterpart to the Phase 7 launch-selection
preview.  It is loopback-only, capability-disabled, and constructed only by
the ``serve`` command.  Importing it creates no provider, worker, listener,
credential, artifact, registry, or ordinary DREAMLAND runtime dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import secrets
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from dreamland.config import DreamlandSettings, load_settings
from dreamland.product.admission import ProductAdmissionLedger, ProductAdmissionLimits
from dreamland.product.authored_artifacts import (
    ImmutableAuthoredRevision,
    ImmutableAuthoredStore,
    authored_revision_digest,
    import_configured_authored_profile,
    load_configured_authored_profile,
)
from dreamland.product.core import ProductCorePolicy
from dreamland.product.execution import ProductCommandExecutor
from dreamland.product.forge_artifacts import SingleAttemptForge
from dreamland.product.forge_stage import (
    ForgeStageHandoffPolicy,
    ForgeStageHandoffService,
)
from dreamland.product.launch_selection import (
    LaunchOfferingSeed,
    LaunchRevisionSeed,
    LaunchSelectionActorSeed,
    LaunchSelectionOrigin,
    LaunchSelectionPolicy,
    LaunchSelectionRegistry,
)
from dreamland.product.launch_selection_forge import ForgeLaunchSelectionRegistrar
from dreamland.product.launch_selection_runtime import (
    LaunchSelectionStateProfileCodec,
)
from dreamland.product.launch_selection_stage import (
    PresenterStageLaunchSelectionService,
)
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    LifecyclePolicy,
    ProductOperation,
    ProductRole,
)
from dreamland.product.stage import PresenterStageService
from dreamland.product.xray import StageXrayPolicy
from dreamland.runtime_logging import configure_console_logging


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_launch_selection_live",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_SELECTION: Any = _load_tool("product_launch_selection_preview")
_COMBINED: Any = _SELECTION._COMBINED
_FORGE_LIVE: Any = _COMBINED._FORGE_LIVE
_FORGE_TRANSPORT: Any = _COMBINED._FORGE_TRANSPORT
_STAGE_LIVE: Any = _COMBINED._STAGE_LIVE
_STAGE: Any = _COMBINED._STAGE
_CORE: Any = _COMBINED._CORE
_LIFECYCLE: Any = _COMBINED._LIFECYCLE

CONTRACT_REVISION = "0.7.0-rc.2"
FORGE_CONTRACT_REVISION = _COMBINED.FORGE_CONTRACT_REVISION
LIVE_EXECUTOR = "development-live-launch-selection-langgraph-v1"
LIVE_SCENARIO = "launch-selection-live-development"
PRESENTER_ACTOR_ID = _COMBINED.PRESENTER_ACTOR_ID
NOTICE_VERSION = "notice:launch-selection-live-development-v1"
AUTHORED_POLICY_ID = "policy:launch-selection-live-authored-v1"
MILLISECONDS_PER_MINUTE = 60_000
MAXIMUM_FORGE_JOBS = 16
MAXIMUM_REVISIONS = 32
MAXIMUM_OFFERINGS = 32
MAXIMUM_ACTIVE_GENERIC_GRANTS = 32
MAXIMUM_STAGE_LAUNCH_GRANTS = 64
MAXIMUM_IDEMPOTENCY_RECORDS = 256
MAXIMUM_TOKEN_HISTORY = 256
LAUNCH_GRANT_TTL_MS = 30 * MILLISECONDS_PER_MINUTE
ACTOR_TTL_MS = 12 * 60 * MILLISECONDS_PER_MINUTE
SELECTED_HANDOFF_TTL_SECONDS = _COMBINED.SELECTED_HANDOFF_TTL_SECONDS
WORLD_FORGE_DEPENDENCY = _COMBINED.WORLD_FORGE_DEPENDENCY


@dataclass(frozen=True, slots=True)
class LiveLaunchSelectionComposition:
    """Explicit provider collaborators and pinned authored startup state."""

    actor_context: str
    actor_expires_at_ms: int
    authored_revision: ImmutableAuthoredRevision
    authored_title: str
    authored_revision_label: str
    forge_runner: SingleAttemptForge
    forge_model: str
    forge_max_output_tokens: int | None
    forge_deadline_seconds: int
    stage_executor: ProductCommandExecutor
    stage_model: str
    stage_max_output_tokens: int | None
    stage_command_reserved_tokens: int
    max_commands: int | None
    admission: ProductAdmissionLedger | None = None


class _LaunchSelectionTokens:
    """Private process-local token source sharing the Stage epoch exactly."""

    def __init__(self, service_epoch: str) -> None:
        self._service_epoch = service_epoch

    def __call__(self, purpose: str) -> str:
        if purpose == "epoch":
            return self._service_epoch
        if purpose == "launch-grant":
            return f"launch-grant:{secrets.token_urlsafe(32)}"
        raise ValueError("unsupported launch-selection token purpose")


class LiveLaunchSelectionHarness(_STAGE.StagePreviewHarness):
    """Own one live dynamic Stage and its source-neutral registry."""

    def __init__(self, composition: LiveLaunchSelectionComposition) -> None:
        expected_reservation = _STAGE_LIVE.resolve_live_command_reservation(
            composition.stage_max_output_tokens,
            max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
        )
        if composition.stage_command_reserved_tokens != expected_reservation:
            raise ValueError(
                "live Presenter command reservation does not match provider maximum"
            )
        self._composition = composition
        super().__init__(_STAGE.SCENARIOS[0])
        self.scenario = LIVE_SCENARIO
        self.forge_stage_policy = ForgeStageHandoffPolicy(
            launch_limits=_live_launch_limits(
                composition.max_commands,
                command_reserved_tokens=composition.stage_command_reserved_tokens,
            ),
            notice_version=NOTICE_VERSION,
            maximum_bindings=MAXIMUM_REVISIONS,
            maximum_idempotency_records=MAXIMUM_IDEMPOTENCY_RECORDS,
        )
        self.selection_registry = self._build_selection_registry()
        self.selection = PresenterStageLaunchSelectionService(
            registry=self.selection_registry,
            stage=self.service,
        )

    def _build_service(self) -> PresenterStageService:
        composition = self._composition
        if composition.actor_expires_at_ms <= _STAGE_LIVE._utc_ms():
            raise ValueError("live launch-selection actor authority is expired")
        return PresenterStageService(
            revisions=(),
            actors=(
                ActorAuthoritySeed(
                    token=composition.actor_context,
                    actor_id=PRESENTER_ACTOR_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=composition.actor_expires_at_ms,
                ),
            ),
            launch_grants=(),
            display_metadata=(),
            executor=composition.stage_executor,
            submit_task=self.submit_task,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=2 * 60 * MILLISECONDS_PER_MINUTE,
                idle_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
                tombstone_ttl_ms=10 * MILLISECONDS_PER_MINUTE,
                failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                max_active_sessions=2,
                max_tombstones=4,
                max_idempotency_records=MAXIMUM_IDEMPOTENCY_RECORDS,
                max_launch_grants=MAXIMUM_STAGE_LAUNCH_GRANTS,
                max_revisions=MAXIMUM_REVISIONS,
            ),
            core_policy=ProductCorePolicy(
                max_commands_per_session=composition.max_commands,
                command_deadline_ms=_STAGE_LIVE.COMMAND_DEADLINE_MS,
                cancellation_deadline_ms=5_000,
                max_output_bytes_per_command=(_STAGE_LIVE.MAX_OUTPUT_BYTES_PER_COMMAND),
                max_output_chunks_per_command=(
                    _STAGE_LIVE.MAX_OUTPUT_CHUNKS_PER_COMMAND
                ),
                reserved_tokens_per_command=composition.stage_command_reserved_tokens,
                reserved_spend_units_per_command=(
                    _STAGE_LIVE.RESERVED_SPEND_UNITS_PER_COMMAND
                ),
                max_attempts_per_command=1,
            ),
            admission=composition.admission
            or _presenter_admission(composition.stage_command_reserved_tokens),
            clock_ms=_STAGE_LIVE._utc_ms,
            monotonic_ms=_STAGE_LIVE._monotonic_ms,
            token_factory=_STAGE_LIVE._live_token_factory,
            dynamic_selection_only=True,
            xray_policy=StageXrayPolicy(),
        )

    def _build_selection_registry(self) -> LaunchSelectionRegistry:
        composition = self._composition
        revision = composition.authored_revision
        digest_hex = revision.digest.removeprefix("sha256:")
        locator_key = secrets.token_bytes(32)
        locator = hmac.new(
            locator_key,
            ("dreamland.authored-launch-selection.v1\0" + revision.revision_id).encode(
                "utf-8"
            ),
            hashlib.sha256,
        ).hexdigest()
        limits = _live_launch_limits(
            composition.max_commands,
            command_reserved_tokens=composition.stage_command_reserved_tokens,
        )
        registry = LaunchSelectionRegistry(
            revisions=(
                LaunchRevisionSeed(
                    revision_id=revision.revision_id,
                    document=revision.allocate_document(),
                ),
            ),
            offerings=(
                LaunchOfferingSeed(
                    offering_id=f"offering:authored-{digest_hex[:32]}",
                    launch_selection_id=f"launch-selection:authored_{locator}",
                    revision_id=revision.revision_id,
                    origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                    title=composition.authored_title,
                    source_label="Authored profile",
                    revision_label=composition.authored_revision_label,
                    role=ProductRole.PRESENTER,
                    allowed_operations=frozenset(
                        {ProductOperation.CREATE, ProductOperation.SWITCH}
                    ),
                    limits=limits,
                    policy_id=AUTHORED_POLICY_ID,
                    notice_version=NOTICE_VERSION,
                    expires_at_ms=composition.actor_expires_at_ms,
                    launch_grant_ttl_ms=LAUNCH_GRANT_TTL_MS,
                ),
            ),
            actors=(
                LaunchSelectionActorSeed(
                    authority=composition.actor_context,
                    actor_id=PRESENTER_ACTOR_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=composition.actor_expires_at_ms,
                ),
            ),
            policy=LaunchSelectionPolicy(
                max_revisions=MAXIMUM_REVISIONS,
                max_offerings=MAXIMUM_OFFERINGS,
                max_actor_authorities=4,
                max_active_launch_grants=MAXIMUM_ACTIVE_GENERIC_GRANTS,
                max_idempotency_records=MAXIMUM_IDEMPOTENCY_RECORDS,
                max_token_history=MAXIMUM_TOKEN_HISTORY,
                max_revision_bytes=4 * 1024 * 1024,
                max_launch_grant_ttl_ms=LAUNCH_GRANT_TTL_MS,
                idempotency_ttl_ms=40 * MILLISECONDS_PER_MINUTE,
            ),
            allowed_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            clock_ms=_STAGE_LIVE._utc_ms,
            monotonic_ms=_STAGE_LIVE._monotonic_ms,
            token_factory=_LaunchSelectionTokens(self.service.service_epoch),
        )
        return registry

    def should_drop_launch_selection_exchange_response(self) -> bool:
        return False


class LiveLaunchSelectionPreviewHandler(_SELECTION.LaunchSelectionPreviewHandler):
    server_version = "DREAMLANDLaunchSelectionLive/0.7"


class LiveLaunchSelectionPreviewServer(_STAGE.ProductStagePreviewServer):
    """Live Stage transport with generic and legacy Forge launch routes."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        contract_root: Path,
        harness: LiveLaunchSelectionHarness,
        handoff: ForgeStageHandoffService,
    ) -> None:
        self.handoff = handoff
        super().__init__(
            address,
            bearer=bearer,
            scenario=LIVE_SCENARIO,
            contract_root=contract_root,
            harness_factory=lambda _scenario: harness,
            handler_class=LiveLaunchSelectionPreviewHandler,
        )


@dataclass(frozen=True, slots=True)
class LiveServerPair:
    forge: Any
    stage: LiveLaunchSelectionPreviewServer


def initialize_private_files(
    *,
    forge_token_file: Path,
    stage_token_file: Path,
    bootstrap_file: Path,
) -> None:
    """Create distinct owner-only bearers and one shared actor context."""
    _COMBINED.initialize_private_files(
        forge_token_file=forge_token_file,
        stage_token_file=stage_token_file,
        bootstrap_file=bootstrap_file,
    )


def _presenter_admission(
    command_reserved_tokens: int = _STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND,
) -> ProductAdmissionLedger:
    """Return the Phase 7B Presenter-only ledger with derived token accounting."""
    token_budget = _STAGE_LIVE.live_token_budget(
        command_reserved_tokens,
        max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
    )
    return ProductAdmissionLedger(
        ProductAdmissionLimits(
            max_active_sessions=2,
            max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
            max_physical_workers=2,
            token_budget=token_budget,
            spend_budget_units=_STAGE_LIVE.SPEND_BUDGET_UNITS,
            presenter_stage_reserved_sessions=1,
            presenter_stage_reserved_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
            presenter_stage_reserved_physical_workers=2,
            presenter_stage_reserved_token_budget=token_budget,
            presenter_stage_reserved_spend_budget_units=(
                _STAGE_LIVE.SPEND_BUDGET_UNITS
            ),
        )
    )


def authored_digest(source_path: Path) -> str:
    """Calculate the exact metadata-free digest used for a startup pin."""
    return authored_revision_digest(load_configured_authored_profile(source_path))


def build_live_composition(
    *,
    settings: DreamlandSettings,
    actor_context: str,
    artifact_root: Path,
    authored_profile_path: Path,
    authored_profile_digest: str,
    authored_title: str,
    authored_revision_label: str,
    deadline_seconds: int,
    max_commands: int | None,
) -> LiveLaunchSelectionComposition:
    """Pin authored state first, then construct the explicit live providers."""
    if deadline_seconds <= 0:
        raise ValueError("deadline seconds must be positive")
    _STAGE_LIVE._validate_max_commands(max_commands)
    _require_display_label(authored_title, "authored title", maximum=120)
    _require_display_label(
        authored_revision_label,
        "authored revision label",
        maximum=96,
    )

    stage_command_reserved_tokens = _STAGE_LIVE.resolve_live_command_reservation(
        settings.agent.max_output_tokens,
        max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
    )
    authored_revision = import_configured_authored_profile(
        authored_profile_path,
        expected_digest=authored_profile_digest,
        store=ImmutableAuthoredStore(artifact_root / "authored-revisions"),
    )
    actor_expires_at_ms = _STAGE_LIVE._utc_ms() + ACTOR_TTL_MS
    forge_runner, forge_model, forge_max_output_tokens = _FORGE_LIVE.build_runner(
        settings,
        artifact_root,
    )
    stage_executor, stage_model, stage_max_output_tokens = (
        _STAGE_LIVE.build_live_executor(
            settings=settings,
            state_codec=LaunchSelectionStateProfileCodec(),
        )
    )
    return LiveLaunchSelectionComposition(
        actor_context=actor_context,
        actor_expires_at_ms=actor_expires_at_ms,
        authored_revision=authored_revision,
        authored_title=authored_title,
        authored_revision_label=authored_revision_label,
        forge_runner=forge_runner,
        forge_model=forge_model,
        forge_max_output_tokens=forge_max_output_tokens,
        forge_deadline_seconds=deadline_seconds,
        stage_executor=stage_executor,
        stage_model=stage_model,
        stage_max_output_tokens=stage_max_output_tokens,
        stage_command_reserved_tokens=stage_command_reserved_tokens,
        max_commands=max_commands,
    )


def build_server_pair(
    *,
    host: str,
    forge_port: int,
    stage_port: int,
    forge_bearer: str,
    stage_bearer: str,
    forge_contract_root: Path,
    stage_contract_root: Path,
    composition: LiveLaunchSelectionComposition,
) -> LiveServerPair:
    _COMBINED._validate_listener(host, forge_port, "Forge")
    _COMBINED._validate_listener(host, stage_port, "Stage")
    if forge_port and forge_port == stage_port:
        raise ValueError("Forge and Stage ports must differ")
    _LIFECYCLE._validate_preview_bearer(stage_bearer)
    _COMBINED._validate_forge_bearer(forge_bearer)
    validated_forge_root, validated_stage_root = validate_contract_dependency(
        forge_contract_root=forge_contract_root,
        stage_contract_root=stage_contract_root,
    )
    return _build_validated_server_pair(
        host=host,
        forge_port=forge_port,
        stage_port=stage_port,
        forge_bearer=forge_bearer,
        stage_bearer=stage_bearer,
        validated_forge_root=validated_forge_root,
        validated_stage_root=validated_stage_root,
        composition=composition,
    )


def _build_validated_server_pair(
    *,
    host: str,
    forge_port: int,
    stage_port: int,
    forge_bearer: str,
    stage_bearer: str,
    validated_forge_root: Path,
    validated_stage_root: Path,
    composition: LiveLaunchSelectionComposition,
) -> LiveServerPair:
    if not (validated_forge_root.is_dir() and validated_stage_root.is_dir()):
        raise ValueError("validated launch-selection contract roots are unavailable")
    _configure_live_transport()

    stage_harness: LiveLaunchSelectionHarness | None = None
    forge_harness: Any | None = None
    forge_server: Any | None = None
    try:
        stage_harness = LiveLaunchSelectionHarness(composition)
        registrar_slot: dict[str, ForgeLaunchSelectionRegistrar] = {}

        def register_selected(reference: str) -> object:
            registrar = registrar_slot.get("registrar")
            if registrar is None:
                raise RuntimeError("Forge launch-selection registrar is unavailable")
            return registrar.register_selected(reference)

        forge_harness = _FORGE_LIVE.LiveForgeHarness(
            actor_context=composition.actor_context,
            runner=composition.forge_runner,
            deadline_seconds=composition.forge_deadline_seconds,
            maximum_jobs=MAXIMUM_FORGE_JOBS,
            selected_handoff_ttl_seconds=SELECTED_HANDOFF_TTL_SECONDS,
            actor_expires_at=_actor_expiry(composition.actor_expires_at_ms),
            on_selected=register_selected,
        )
        registrar_slot["registrar"] = ForgeLaunchSelectionRegistrar(
            forge=forge_harness.service,
            artifacts=composition.forge_runner.store,
            registry=stage_harness.selection_registry,
            actor_authority=composition.actor_context,
            actor_id=PRESENTER_ACTOR_ID,
            stage_policy=stage_harness.forge_stage_policy,
            maximum_launch_grant_ttl_ms=LAUNCH_GRANT_TTL_MS,
        )
        handoff = ForgeStageHandoffService(
            forge=forge_harness.service,
            artifacts=composition.forge_runner.store,
            stage=stage_harness.service,
            policy=stage_harness.forge_stage_policy,
            token_factory=_COMBINED._handoff_token_factory,
        )
        forge_server = _FORGE_TRANSPORT.ForgePreviewServer(
            (host, forge_port),
            bearer=forge_bearer,
            harness=forge_harness,
            contract_revision=FORGE_CONTRACT_REVISION,
        )
        stage_server = LiveLaunchSelectionPreviewServer(
            (host, stage_port),
            bearer=stage_bearer,
            contract_root=validated_stage_root,
            harness=stage_harness,
            handoff=handoff,
        )
        return LiveServerPair(forge_server, stage_server)
    except Exception:
        if forge_server is not None:
            forge_server.server_close()
        elif forge_harness is not None:
            forge_harness.close()
        if stage_harness is not None:
            stage_harness.close()
        raise


def validate_forge_contract_root(contract_root: Path) -> Path:
    return _COMBINED.validate_forge_contract_root(contract_root)


def validate_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    tool_path = Path(__file__).with_name("product_launch_selection_live_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_launch_selection_live_contract_validator",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("launch-selection contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("launch-selection live contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != CONTRACT_REVISION
        or result.get("implementation_status") != "integration_preview_only"
        or result.get("capabilities_enabled") != 0
        or result.get("routes") != 21
    ):
        raise ValueError("launch-selection live contract is incompatible")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if any(
        not isinstance(value, Mapping) or value.get("enabled") is not False
        for value in manifest.get("capabilities", {}).values()
    ):
        raise ValueError("launch-selection live capabilities must remain disabled")
    return root


def validate_contract_dependency(
    *,
    forge_contract_root: Path,
    stage_contract_root: Path,
) -> tuple[Path, Path]:
    forge_root = validate_forge_contract_root(forge_contract_root)
    stage_root = validate_contract_root(stage_contract_root)
    try:
        stage_manifest = json.loads(
            (stage_root / "manifest.json").read_text(encoding="utf-8")
        )
        dependency = stage_manifest["dependencies"]["world_forge"]
        forge_manifest_bytes = (forge_root / "manifest.json").read_bytes()
    except (
        KeyError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise ValueError(
            "launch-selection contract dependency is unavailable"
        ) from error
    if dependency != WORLD_FORGE_DEPENDENCY:
        raise ValueError("launch-selection World Forge dependency is incompatible")
    if (
        hashlib.sha256(forge_manifest_bytes).hexdigest()
        != dependency["manifest_sha256"]
    ):
        raise ValueError("launch-selection World Forge manifest digest is incompatible")
    return forge_root, stage_root


def startup_announcement(
    pair: LiveServerPair,
    composition: LiveLaunchSelectionComposition,
) -> dict[str, object]:
    return {
        "capabilities_enabled": 0,
        "contract_revision": CONTRACT_REVISION,
        "development_only": True,
        "integration_only": True,
        "live_capability_evidence": False,
        "normal_runtime_composition": False,
        "provider_calls": True,
        "launch_selection": {
            "authored_options": 1,
            "forge_registration": "authoritative_selected_only",
            "generic_grant_capacity": MAXIMUM_ACTIVE_GENERIC_GRANTS,
            "legacy_forge_handoff": True,
            "maximum_options": MAXIMUM_OFFERINGS,
            "maximum_revisions": MAXIMUM_REVISIONS,
        },
        "world_forge": {
            "contract_revision": FORGE_CONTRACT_REVISION,
            "deadline_seconds": composition.forge_deadline_seconds,
            "host": pair.forge.server_address[0],
            "maximum_jobs": MAXIMUM_FORGE_JOBS,
            "max_output_tokens": composition.forge_max_output_tokens,
            "model": composition.forge_model,
            "port": pair.forge.server_address[1],
            "selected_handoff_ttl_seconds": SELECTED_HANDOFF_TTL_SECONDS,
        },
        "presenter_stage": {
            "contract_revision": CONTRACT_REVISION,
            "effective_command_limit": (
                composition.max_commands
                if composition.max_commands is not None
                else "unlimited"
            ),
            "executor": LIVE_EXECUTOR,
            "host": pair.stage.server_address[0],
            "max_commands": composition.max_commands,
            "max_output_tokens": composition.stage_max_output_tokens,
            "command_reserved_tokens": composition.stage_command_reserved_tokens,
            "model": composition.stage_model,
            "output_publication": {
                "flush_bytes": _STAGE_LIVE.DEFAULT_PUBLICATION_CHUNK_BYTES,
                "flush_on_newline": True,
                "max_bytes_per_command": (_STAGE_LIVE.MAX_OUTPUT_BYTES_PER_COMMAND),
            },
            "port": pair.stage.server_address[1],
            "stage_launch_grant_capacity": MAXIMUM_STAGE_LAUNCH_GRANTS,
            "token_budget": _STAGE_LIVE.live_token_budget(
                composition.stage_command_reserved_tokens,
                max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
            ),
            "xray": {
                "available": True,
                "schema_revision": "stage-xray-v1",
                "authority": "presenter_session_only",
                "maximum_hosts": 64,
                "maximum_facts": 128,
            },
        },
    }


def serve_pair(pair: LiveServerPair) -> None:
    forge_thread = threading.Thread(
        target=pair.forge.serve_forever,
        name="dreamland-launch-selection-live-forge-http",
    )
    forge_thread.start()
    try:
        pair.stage.serve_forever()
    finally:
        pair.forge.shutdown()
        forge_thread.join(timeout=10)
        pair.stage.server_close()
        pair.forge.server_close()


def _configure_live_transport() -> None:
    _SELECTION._configure_transport()
    for module in (_SELECTION, _COMBINED, _STAGE, _CORE):
        module.CONTRACT_REVISION = CONTRACT_REVISION
    _STAGE.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    _CORE.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    _CORE.PREVIEW_PROVIDER_CALLS = True
    _CORE.PREVIEW_TEST_ONLY = True


def _live_launch_limits(
    max_commands: int | None,
    *,
    command_reserved_tokens: int = _STAGE_LIVE.RESERVED_TOKENS_PER_COMMAND,
) -> dict[str, int]:
    limits = {
        "absolute_ttl_ms": 2 * 60 * MILLISECONDS_PER_MINUTE,
        "idle_ttl_ms": 30 * MILLISECONDS_PER_MINUTE,
        "max_command_bytes": 4_096,
        "command_reserved_tokens": command_reserved_tokens,
        "command_reserved_spend_units": (_STAGE_LIVE.RESERVED_SPEND_UNITS_PER_COMMAND),
        "command_max_attempts": 1,
        "command_deadline_ms": _STAGE_LIVE.COMMAND_DEADLINE_MS,
    }
    if max_commands is not None:
        limits["max_commands"] = max_commands
    return limits


def _actor_expiry(value_ms: int) -> datetime:
    if isinstance(value_ms, bool) or not isinstance(value_ms, int) or value_ms <= 0:
        raise ValueError("live launch-selection actor expiry is invalid")
    return datetime.fromtimestamp(value_ms // 1_000, UTC).replace(
        microsecond=(value_ms % 1_000) * 1_000
    )


def _require_display_label(value: str, name: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(
            ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
            for character in value
        )
    ):
        raise ValueError(f"{name} must be a bounded display-safe value")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the explicit live source-neutral launch-selection target."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="create owner-only credentials")
    initialize.add_argument("--forge-token-file", required=True, type=Path)
    initialize.add_argument("--stage-token-file", required=True, type=Path)
    initialize.add_argument("--bootstrap-file", required=True, type=Path)
    digest = commands.add_parser(
        "digest-authored",
        help="print the immutable digest pin for one trusted authored profile",
    )
    digest.add_argument("--profile-path", required=True, type=Path)
    serve = commands.add_parser("serve", help="start both loopback listeners")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--forge-port", type=int, default=0)
    serve.add_argument("--stage-port", type=int, default=0)
    serve.add_argument("--forge-token-file", required=True, type=Path)
    serve.add_argument("--stage-token-file", required=True, type=Path)
    serve.add_argument("--bootstrap-file", required=True, type=Path)
    serve.add_argument("--forge-contract-root", required=True, type=Path)
    serve.add_argument("--contract-root", required=True, type=Path)
    serve.add_argument("--artifact-root", required=True, type=Path)
    serve.add_argument("--authored-profile-path", required=True, type=Path)
    serve.add_argument("--authored-profile-digest", required=True)
    serve.add_argument(
        "--authored-title",
        default="Configured authored DREAMLAND world",
    )
    serve.add_argument(
        "--authored-revision-label",
        default="Pinned authored revision",
    )
    serve.add_argument(
        "--deadline-seconds",
        type=int,
        default=_FORGE_LIVE.DEFAULT_DEADLINE_SECONDS,
    )
    serve.add_argument(
        "--max-commands",
        default=_STAGE_LIVE.MAX_COMMANDS,
        type=_STAGE_LIVE._parse_max_commands,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "init":
            initialize_private_files(
                forge_token_file=arguments.forge_token_file,
                stage_token_file=arguments.stage_token_file,
                bootstrap_file=arguments.bootstrap_file,
            )
            print(
                json.dumps(
                    {
                        "bootstrap_file": str(arguments.bootstrap_file),
                        "created": True,
                        "forge_token_file": str(arguments.forge_token_file),
                        "stage_token_file": str(arguments.stage_token_file),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "digest-authored":
            print(
                json.dumps(
                    {
                        "authored_revision_digest": authored_digest(
                            arguments.profile_path
                        )
                    },
                    sort_keys=True,
                )
            )
            return 0

        _COMBINED._validate_listener(arguments.host, arguments.forge_port, "Forge")
        _COMBINED._validate_listener(arguments.host, arguments.stage_port, "Stage")
        if arguments.deadline_seconds <= 0:
            raise ValueError("deadline seconds must be positive")
        bootstrap = _COMBINED.load_bootstrap(arguments.bootstrap_file)
        forge_bearer = _FORGE_TRANSPORT.load_bearer(arguments.forge_token_file)
        stage_bearer = _LIFECYCLE.load_preview_bearer_file(arguments.stage_token_file)
        validated_forge_root, validated_stage_root = validate_contract_dependency(
            forge_contract_root=arguments.forge_contract_root,
            stage_contract_root=arguments.contract_root,
        )
        settings = load_settings()
        configure_console_logging(settings.logging, log_capacity=0)
        composition = build_live_composition(
            settings=settings,
            actor_context=bootstrap.actor_context,
            artifact_root=arguments.artifact_root,
            authored_profile_path=arguments.authored_profile_path,
            authored_profile_digest=arguments.authored_profile_digest,
            authored_title=arguments.authored_title,
            authored_revision_label=arguments.authored_revision_label,
            deadline_seconds=arguments.deadline_seconds,
            max_commands=arguments.max_commands,
        )
        pair = _build_validated_server_pair(
            host=arguments.host,
            forge_port=arguments.forge_port,
            stage_port=arguments.stage_port,
            forge_bearer=forge_bearer,
            stage_bearer=stage_bearer,
            validated_forge_root=validated_forge_root,
            validated_stage_root=validated_stage_root,
            composition=composition,
        )
        print(
            json.dumps(startup_announcement(pair, composition), sort_keys=True),
            flush=True,
        )
        serve_pair(pair)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        print(f"Live launch-selection error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
