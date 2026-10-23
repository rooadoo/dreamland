#!/usr/bin/env python3
"""Run the explicit provider-backed authored-attendee development target.

This is the Phase 7B live Forge/Presenter composition with the Phase 7C
authored-attendee facade added to the same private product listener. Importing
the tool creates no provider, artifact, listener, worker, credential, or
ordinary DREAMLAND runtime dependency. All construction remains behind the
explicit ``serve`` command and every product capability remains disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import re
import secrets
import sys
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Any

from dreamland.config import DreamlandSettings, LangfuseSettings, load_settings
from dreamland.product.admission import ProductAdmissionLedger, ProductAdmissionLimits
from dreamland.product.attendee import (
    AttendeeBffAuthoritySeed,
    AttendeeContextPolicy,
    AttendeeSessionCoreService,
    PublicAttendeeSessionService,
)
from dreamland.product.core import ProductCorePolicy
from dreamland.product.execution import ProductCommandExecutor
from dreamland.product.launch_selection import (
    LaunchOfferingSeed,
    LaunchRevisionSeed,
    LaunchSelectionActorSeed,
    LaunchSelectionOrigin,
    LaunchSelectionPolicy,
    LaunchSelectionRegistry,
)
from dreamland.product.launch_selection_forge import ForgeLaunchSelectionRegistrar
from dreamland.product.launch_selection_runtime import LaunchSelectionStateProfileCodec
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LifecyclePolicy,
    ProductOperation,
    ProductRole,
)
from dreamland.runtime_logging import configure_console_logging


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_attendee_live",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_ATTENDEE: Any = _load_tool("product_attendee_preview")
_LIVE: Any = _load_tool("product_launch_selection_live")
_STAGE_LIVE: Any = _LIVE._STAGE_LIVE
_FORGE_LIVE: Any = _LIVE._FORGE_LIVE
_FORGE_TRANSPORT: Any = _LIVE._FORGE_TRANSPORT
_LIFECYCLE: Any = _ATTENDEE._LIFECYCLE
_CORE: Any = _ATTENDEE._CORE
_STAGE: Any = _ATTENDEE._STAGE

CONTRACT_REVISION = "0.7.0-rc.4"
FORGE_CONTRACT_REVISION = _LIVE.FORGE_CONTRACT_REVISION
LIVE_EXECUTOR = "development-live-attendee-langgraph-v1"
LIVE_SCENARIO = "authored-attendee-live-development"
ATTENDEE_NOTICE_VERSION = "notice:attendee-live-development-v1"
ATTENDEE_POLICY_ID = "policy:attendee-live-authored-v1"

MILLISECONDS_PER_MINUTE = 60_000
DEFAULT_MAX_CONTEXTS = 64
MAXIMUM_CONTEXTS = 1_024
DEFAULT_CONTEXT_TTL_MINUTES = 60
DEFAULT_ADMISSION_DURATION_MINUTES = 12 * 60
MAXIMUM_ADMISSION_DURATION_MINUTES = 12 * 60
ATTENDEE_CLEANUP_GRACE_MINUTES = 30
ATTENDEE_ACTIVE_SESSIONS = 4
ATTENDEE_LOGICAL_JOBS = 64
ATTENDEE_PHYSICAL_WORKERS = 2
ATTENDEE_SPEND_BUDGET = (
    ATTENDEE_LOGICAL_JOBS * _STAGE_LIVE.RESERVED_SPEND_UNITS_PER_COMMAND
)
ATTENDEE_MAX_GRANTS = 2 * MAXIMUM_CONTEXTS
ATTENDEE_MAX_IDEMPOTENCY = 8 * MAXIMUM_CONTEXTS
MAXIMUM_ATTENDEE_REVISIONS = 17
MAXIMUM_ATTENDEE_OFFERINGS = 17
MAXIMUM_GENERATED_PUBLICATIONS = 16
_NOTICE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class LiveAttendeeComposition:
    """Explicit Phase 7B collaborators plus isolated attendee provider state."""

    presenter: Any
    attendee_executor: ProductCommandExecutor
    attendee_model: str
    attendee_max_output_tokens: int | None
    attendee_command_reserved_tokens: int
    max_contexts: int
    context_ttl_ms: int
    admission_opens_at_ms: int
    admission_closes_at_ms: int
    generated_world_access: bool = False
    generated_eligibility_key: bytes | None = field(default=None, repr=False)
    attendee_notice_version: str = ATTENDEE_NOTICE_VERSION
    max_revisions: int = 1
    max_offerings: int = 1
    generated_publication_capacity: int = 0


class _RegistryTokens:
    def __init__(self, service_epoch: str) -> None:
        self._service_epoch = service_epoch

    def __call__(self, purpose: str) -> str:
        if purpose == "epoch":
            return self._service_epoch
        if purpose == "launch-grant":
            return f"launch-grant:{secrets.token_urlsafe(32)}"
        raise ValueError("unsupported attendee launch-selection token purpose")


class _CoreTokens:
    def __init__(self, service_epoch: str) -> None:
        self._service_epoch = service_epoch

    def __call__(self, purpose: str) -> str:
        if purpose == "service_epoch":
            return self._service_epoch
        return _STAGE_LIVE._live_token_factory(purpose)


class LiveAttendeeHarness(_LIVE.LiveLaunchSelectionHarness):
    """Keep live Forge/Presenter behavior and add authored attendee sessions."""

    def __init__(self, composition: LiveAttendeeComposition) -> None:
        _validate_generated_world_configuration(
            generated_world_access=composition.generated_world_access,
            generated_eligibility_key=composition.generated_eligibility_key,
            attendee_notice_version=composition.attendee_notice_version,
            max_revisions=composition.max_revisions,
            max_offerings=composition.max_offerings,
            generated_publication_capacity=(composition.generated_publication_capacity),
        )
        resolved_attendee_reservation = _STAGE_LIVE.resolve_live_command_reservation(
            composition.attendee_max_output_tokens,
            max_logical_jobs=ATTENDEE_LOGICAL_JOBS,
        )
        if (
            composition.attendee_command_reserved_tokens
            != resolved_attendee_reservation
        ):
            raise ValueError(
                "attendee provider output maximum does not match its token reservation"
            )
        self._attendee_composition = composition
        super().__init__(composition.presenter)
        self.scenario = LIVE_SCENARIO
        self.stage_service = self.service
        self.presenter_actor_context = composition.presenter.actor_context
        self.bff_authority = f"bff-authority:{secrets.token_urlsafe(32)}"

        # StagePreviewHarness owns the one explicit worker pool. No request can
        # arrive yet, so it is safe to replace the empty Phase 7B pool with the
        # exact shared physical capacity before constructing attendee state.
        old_workers = self._workers
        old_workers.shutdown(wait=True, cancel_futures=True)
        self._workers = ThreadPoolExecutor(
            max_workers=2 + ATTENDEE_PHYSICAL_WORKERS,
            thread_name_prefix="dreamland-attendee-live",
        )

        self._attendee_closed = False
        try:
            self.attendee_service = self._build_attendee_service()
        except Exception:
            super().close()
            raise
        self.service = _ATTENDEE._SessionRoutingService(
            stage_service=self.stage_service,
            attendee_service=self.attendee_service,
            presenter_actor_context=self.presenter_actor_context,
        )
        self._cutoff_stop = threading.Event()
        # Observe the service's exact projected monotonic boundary. Recomputing
        # it here would create a second cutoff truth if wall time moved between
        # service construction and watcher construction.
        self._cutoff_deadline_ms = self.attendee_service._admission_closes_deadline_ms
        self._cutoff_thread = threading.Thread(
            target=self._watch_attendee_cutoff,
            name="dreamland-attendee-live-cutoff",
        )
        self._cutoff_thread.start()

    def close(self) -> None:
        """Fence attendee authority before stopping the shared worker pool."""
        if hasattr(self, "_cutoff_stop"):
            self._cutoff_stop.set()
        if (
            hasattr(self, "_cutoff_thread")
            and self._cutoff_thread is not threading.current_thread()
        ):
            self._cutoff_thread.join()
        if not self._attendee_closed and hasattr(self, "attendee_service"):
            self._attendee_closed = True
            self.attendee_service.shutdown()
        super().close()

    def _watch_attendee_cutoff(self) -> None:
        """Fence due attendee work even when no request arrives at cutoff."""
        closes_at_ms = self._attendee_composition.admission_closes_at_ms
        while not self._cutoff_stop.is_set():
            wall_remaining = closes_at_ms - _STAGE_LIVE._utc_ms()
            monotonic_remaining = self._cutoff_deadline_ms - _STAGE_LIVE._monotonic_ms()
            remaining_ms = min(wall_remaining, monotonic_remaining)
            if remaining_ms <= 0:
                self.attendee_service.expire_due()
                return
            self._cutoff_stop.wait(min(remaining_ms / 1_000, 0.25))

    def _build_attendee_service(self) -> PublicAttendeeSessionService:
        composition = self._attendee_composition
        presenter = composition.presenter
        revision = presenter.authored_revision
        document = revision.allocate_document()
        authority_expires_at_ms = composition.admission_closes_at_ms + (
            ATTENDEE_CLEANUP_GRACE_MINUTES * MILLISECONDS_PER_MINUTE
        )
        actors = tuple(
            LaunchSelectionActorSeed(
                authority=f"actor:{secrets.token_urlsafe(32)}",
                actor_id=f"attendee-live-{index:04d}",
                role=ProductRole.ATTENDEE,
                expires_at_ms=authority_expires_at_ms,
            )
            for index in range(1, composition.max_contexts + 1)
        )
        digest_hex = revision.digest.removeprefix("sha256:")
        locator = hmac.new(
            secrets.token_bytes(32),
            (
                "dreamland.attendee-authored-selection.v1\0" + revision.revision_id
            ).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        limits = _attendee_launch_limits(
            presenter.max_commands,
            composition.attendee_command_reserved_tokens,
        )
        registry = LaunchSelectionRegistry(
            revisions=(
                LaunchRevisionSeed(
                    revision_id=revision.revision_id,
                    document=document,
                ),
            ),
            offerings=(
                LaunchOfferingSeed(
                    offering_id=f"offering:attendee-authored-{digest_hex[:32]}",
                    launch_selection_id=f"launch-selection:attendee_{locator}",
                    revision_id=revision.revision_id,
                    origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                    title=presenter.authored_title,
                    source_label="Authored profile",
                    revision_label=presenter.authored_revision_label,
                    role=ProductRole.ATTENDEE,
                    allowed_operations=frozenset(
                        {ProductOperation.CREATE, ProductOperation.SWITCH}
                    ),
                    limits=limits,
                    policy_id=ATTENDEE_POLICY_ID,
                    notice_version=composition.attendee_notice_version,
                    # Registry and facade project wall cutoffs independently;
                    # leave a one-second monotonic rounding margin while still
                    # fencing grants before the advertised admission cutoff.
                    expires_at_ms=(
                        composition.admission_closes_at_ms
                        - min(
                            1_000,
                            max(
                                1,
                                (
                                    composition.admission_closes_at_ms
                                    - composition.admission_opens_at_ms
                                )
                                // 10,
                            ),
                        )
                    ),
                    launch_grant_ttl_ms=min(
                        _LIVE.LAUNCH_GRANT_TTL_MS,
                        composition.context_ttl_ms,
                    ),
                ),
            ),
            actors=actors,
            policy=LaunchSelectionPolicy(
                max_revisions=composition.max_revisions,
                max_offerings=composition.max_offerings,
                max_actor_authorities=composition.max_contexts,
                max_active_launch_grants=min(
                    ATTENDEE_MAX_GRANTS,
                    2 * composition.max_contexts,
                ),
                max_idempotency_records=min(
                    ATTENDEE_MAX_IDEMPOTENCY,
                    8 * composition.max_contexts,
                ),
                max_token_history=min(
                    ATTENDEE_MAX_IDEMPOTENCY + ATTENDEE_MAX_GRANTS,
                    max(
                        10 * composition.max_contexts,
                        composition.max_contexts + composition.max_offerings + 1,
                    ),
                ),
                max_revision_bytes=4 * 1024 * 1024,
                max_launch_grant_ttl_ms=min(
                    _LIVE.LAUNCH_GRANT_TTL_MS,
                    composition.context_ttl_ms,
                ),
                idempotency_ttl_ms=min(
                    composition.context_ttl_ms,
                    40 * MILLISECONDS_PER_MINUTE,
                ),
            ),
            allowed_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            clock_ms=_STAGE_LIVE._utc_ms,
            monotonic_ms=_STAGE_LIVE._monotonic_ms,
            token_factory=_RegistryTokens(self.stage_service.service_epoch),
        )
        core = AttendeeSessionCoreService(
            revisions=(ImmutableRevisionSeed(revision.revision_id, document),),
            actors=tuple(
                ActorAuthoritySeed(
                    token=actor.authority,
                    actor_id=actor.actor_id,
                    role=actor.role,
                    expires_at_ms=actor.expires_at_ms,
                )
                for actor in actors
            ),
            executor=composition.attendee_executor,
            submit_task=self.submit_task,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=min(
                    2 * 60 * MILLISECONDS_PER_MINUTE,
                    composition.context_ttl_ms,
                ),
                idle_ttl_ms=min(
                    30 * MILLISECONDS_PER_MINUTE,
                    composition.context_ttl_ms,
                ),
                tombstone_ttl_ms=10 * MILLISECONDS_PER_MINUTE,
                failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                max_active_sessions=ATTENDEE_ACTIVE_SESSIONS,
                max_tombstones=2 * ATTENDEE_ACTIVE_SESSIONS,
                max_idempotency_records=min(
                    ATTENDEE_MAX_IDEMPOTENCY,
                    8 * composition.max_contexts,
                ),
                max_launch_grants=min(
                    ATTENDEE_MAX_GRANTS,
                    2 * composition.max_contexts,
                ),
                max_actor_authorities=composition.max_contexts,
                max_revisions=composition.max_revisions,
            ),
            core_policy=ProductCorePolicy(
                max_commands_per_session=presenter.max_commands,
                command_deadline_ms=_STAGE_LIVE.COMMAND_DEADLINE_MS,
                cancellation_deadline_ms=5_000,
                max_output_bytes_per_command=_STAGE_LIVE.MAX_OUTPUT_BYTES_PER_COMMAND,
                max_output_chunks_per_command=(
                    _STAGE_LIVE.MAX_OUTPUT_CHUNKS_PER_COMMAND
                ),
                reserved_tokens_per_command=(
                    composition.attendee_command_reserved_tokens
                ),
                reserved_spend_units_per_command=(
                    _STAGE_LIVE.RESERVED_SPEND_UNITS_PER_COMMAND
                ),
                max_attempts_per_command=1,
            ),
            admission=self.stage_service.admission,
            clock_ms=_STAGE_LIVE._utc_ms,
            monotonic_ms=_STAGE_LIVE._monotonic_ms,
            token_factory=_CoreTokens(self.stage_service.service_epoch),
            generated_world_access=composition.generated_world_access,
        )
        service = PublicAttendeeSessionService(
            registry=registry,
            core=core,
            context_actors=actors,
            bff_authorities=(
                AttendeeBffAuthoritySeed(
                    authority=self.bff_authority,
                    authority_id="bff:attendee-live",
                    expires_at_ms=authority_expires_at_ms,
                ),
            ),
            policy=AttendeeContextPolicy(
                max_contexts=composition.max_contexts,
                context_ttl_ms=composition.context_ttl_ms,
                max_bootstrap_idempotency_records=max(
                    composition.max_contexts,
                    2 * composition.max_contexts,
                ),
                max_exchange_idempotency_records=min(
                    ATTENDEE_MAX_IDEMPOTENCY,
                    8 * composition.max_contexts,
                ),
                max_revoke_idempotency_records=composition.max_contexts,
                admission_opens_at_ms=composition.admission_opens_at_ms,
                admission_closes_at_ms=composition.admission_closes_at_ms,
            ),
            generated_world_access=composition.generated_world_access,
            generated_eligibility_key=composition.generated_eligibility_key,
        )
        # These are stable composition seams for the adjacent World Access
        # catalog. They are exposed only after the facade has proved that the
        # registry, core, clocks, epoch, capacities, and private key agree.
        self.attendee_registry = registry
        self.attendee_core = core
        self.generated_publication_capacity = composition.generated_publication_capacity
        return service


class LiveAttendeeHandler(_ATTENDEE.AttendeePreviewHandler):
    server_version = "DREAMLANDAttendeeLive/0.7"


class LiveAttendeeServer(_ATTENDEE.AttendeePreviewServer):
    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        contract_root: Path,
        harness: LiveAttendeeHarness,
        handoff: Any,
    ) -> None:
        super().__init__(
            address,
            bearer=bearer,
            scenario=_ATTENDEE.SCENARIOS[0],
            contract_root=contract_root,
            harness_factory=lambda _scenario: harness,
        )
        self.handoff = handoff
        self.RequestHandlerClass = LiveAttendeeHandler


@dataclass(frozen=True, slots=True)
class LiveServerPair:
    forge: Any
    stage: LiveAttendeeServer


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
    max_contexts: int,
    context_ttl_minutes: int,
    admission_duration_minutes: int,
    generated_world_access: bool = False,
    generated_eligibility_key: bytes | None = None,
    attendee_notice_version: str = ATTENDEE_NOTICE_VERSION,
    max_revisions: int = 1,
    max_offerings: int = 1,
    generated_publication_capacity: int = 0,
) -> LiveAttendeeComposition:
    _validate_attendee_limits(
        max_contexts=max_contexts,
        context_ttl_minutes=context_ttl_minutes,
        admission_duration_minutes=admission_duration_minutes,
    )
    _validate_generated_world_configuration(
        generated_world_access=generated_world_access,
        generated_eligibility_key=generated_eligibility_key,
        attendee_notice_version=attendee_notice_version,
        max_revisions=max_revisions,
        max_offerings=max_offerings,
        generated_publication_capacity=generated_publication_capacity,
    )
    _STAGE_LIVE._validate_max_commands(max_commands)
    presenter_command_reserved_tokens = _STAGE_LIVE.resolve_live_command_reservation(
        settings.agent.max_output_tokens,
        max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
    )
    attendee_command_reserved_tokens = _STAGE_LIVE.resolve_live_command_reservation(
        settings.agent.max_output_tokens,
        max_logical_jobs=ATTENDEE_LOGICAL_JOBS,
    )
    admission = _shared_admission(
        presenter_command_reserved_tokens=presenter_command_reserved_tokens,
        attendee_command_reserved_tokens=attendee_command_reserved_tokens,
    )
    presenter = _LIVE.build_live_composition(
        settings=settings,
        actor_context=actor_context,
        artifact_root=artifact_root,
        authored_profile_path=authored_profile_path,
        authored_profile_digest=authored_profile_digest,
        authored_title=authored_title,
        authored_revision_label=authored_revision_label,
        deadline_seconds=deadline_seconds,
        max_commands=max_commands,
    )
    if presenter.stage_command_reserved_tokens != presenter_command_reserved_tokens:
        raise ValueError(
            "Presenter provider output maximum does not match its token reservation"
        )
    presenter = replace(presenter, admission=admission)
    attendee_settings = replace(settings, langfuse=LangfuseSettings())
    attendee_executor, attendee_model, attendee_max_output_tokens = (
        _STAGE_LIVE.build_live_executor(
            settings=attendee_settings,
            state_codec=LaunchSelectionStateProfileCodec(),
        )
    )
    if (
        _STAGE_LIVE.resolve_live_command_reservation(
            attendee_max_output_tokens,
            max_logical_jobs=ATTENDEE_LOGICAL_JOBS,
        )
        != attendee_command_reserved_tokens
    ):
        raise ValueError(
            "attendee provider output maximum does not match its token reservation"
        )
    opens_at_ms = _STAGE_LIVE._utc_ms()
    closes_at_ms = opens_at_ms + admission_duration_minutes * MILLISECONDS_PER_MINUTE
    return LiveAttendeeComposition(
        presenter=presenter,
        attendee_executor=attendee_executor,
        attendee_model=attendee_model,
        attendee_max_output_tokens=attendee_max_output_tokens,
        attendee_command_reserved_tokens=attendee_command_reserved_tokens,
        max_contexts=max_contexts,
        context_ttl_ms=context_ttl_minutes * MILLISECONDS_PER_MINUTE,
        admission_opens_at_ms=opens_at_ms,
        admission_closes_at_ms=closes_at_ms,
        generated_world_access=generated_world_access,
        generated_eligibility_key=generated_eligibility_key,
        attendee_notice_version=attendee_notice_version,
        max_revisions=max_revisions,
        max_offerings=max_offerings,
        generated_publication_capacity=generated_publication_capacity,
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
    composition: LiveAttendeeComposition,
) -> LiveServerPair:
    _LIVE._COMBINED._validate_listener(host, forge_port, "Forge")
    _LIVE._COMBINED._validate_listener(host, stage_port, "Product")
    if forge_port and forge_port == stage_port:
        raise ValueError("Forge and product ports must differ")
    _LIFECYCLE._validate_preview_bearer(stage_bearer)
    _LIVE._COMBINED._validate_forge_bearer(forge_bearer)
    forge_root, stage_root = validate_contract_dependency(
        forge_contract_root=forge_contract_root,
        stage_contract_root=stage_contract_root,
    )
    return _build_validated_server_pair(
        host=host,
        forge_port=forge_port,
        stage_port=stage_port,
        forge_bearer=forge_bearer,
        stage_bearer=stage_bearer,
        validated_forge_root=forge_root,
        validated_stage_root=stage_root,
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
    composition: LiveAttendeeComposition,
) -> LiveServerPair:
    if not (validated_forge_root.is_dir() and validated_stage_root.is_dir()):
        raise ValueError("validated attendee-live contract roots are unavailable")
    _configure_live_transport()
    stage_harness: LiveAttendeeHarness | None = None
    forge_harness: Any | None = None
    forge_server: Any | None = None
    try:
        stage_harness = LiveAttendeeHarness(composition)
        registrar_slot: dict[str, ForgeLaunchSelectionRegistrar] = {}

        def register_selected(reference: str) -> object:
            registrar = registrar_slot.get("registrar")
            if registrar is None:
                raise RuntimeError("Forge launch-selection registrar is unavailable")
            return registrar.register_selected(reference)

        presenter = composition.presenter
        forge_harness = _FORGE_LIVE.LiveForgeHarness(
            actor_context=presenter.actor_context,
            runner=presenter.forge_runner,
            deadline_seconds=presenter.forge_deadline_seconds,
            maximum_jobs=_LIVE.MAXIMUM_FORGE_JOBS,
            selected_handoff_ttl_seconds=_LIVE.SELECTED_HANDOFF_TTL_SECONDS,
            actor_expires_at=_LIVE._actor_expiry(presenter.actor_expires_at_ms),
            on_selected=register_selected,
        )
        registrar_slot["registrar"] = ForgeLaunchSelectionRegistrar(
            forge=forge_harness.service,
            artifacts=presenter.forge_runner.store,
            registry=stage_harness.selection_registry,
            actor_authority=presenter.actor_context,
            actor_id=_LIVE.PRESENTER_ACTOR_ID,
            stage_policy=stage_harness.forge_stage_policy,
            maximum_launch_grant_ttl_ms=_LIVE.LAUNCH_GRANT_TTL_MS,
        )
        handoff = _LIVE.ForgeStageHandoffService(
            forge=forge_harness.service,
            artifacts=presenter.forge_runner.store,
            stage=stage_harness.stage_service,
            policy=stage_harness.forge_stage_policy,
            token_factory=_LIVE._COMBINED._handoff_token_factory,
        )
        forge_server = _FORGE_TRANSPORT.ForgePreviewServer(
            (host, forge_port),
            bearer=forge_bearer,
            harness=forge_harness,
            contract_revision=FORGE_CONTRACT_REVISION,
        )
        product_server = LiveAttendeeServer(
            (host, stage_port),
            bearer=stage_bearer,
            contract_root=validated_stage_root,
            harness=stage_harness,
            handoff=handoff,
        )
        return LiveServerPair(forge_server, product_server)
    except Exception:
        if forge_server is not None:
            forge_server.server_close()
        elif forge_harness is not None:
            forge_harness.close()
        if stage_harness is not None:
            stage_harness.close()
        raise


def validate_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    tool_path = Path(__file__).with_name("product_attendee_live_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_live_contract_validator",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("attendee live contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("attendee live contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != CONTRACT_REVISION
        or result.get("implementation_status") != "integration_preview_only"
        or result.get("capabilities_enabled") != 0
        or result.get("routes") != 24
    ):
        raise ValueError("attendee live contract is incompatible")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if any(
        not isinstance(value, Mapping) or value.get("enabled") is not False
        for value in manifest.get("capabilities", {}).values()
    ):
        raise ValueError("attendee live capabilities must remain disabled")
    return root


def validate_contract_dependency(
    *,
    forge_contract_root: Path,
    stage_contract_root: Path,
) -> tuple[Path, Path]:
    forge_root = _LIVE.validate_forge_contract_root(forge_contract_root)
    stage_root = validate_contract_root(stage_contract_root)
    try:
        manifest = json.loads((stage_root / "manifest.json").read_text("utf-8"))
        dependency = manifest["dependencies"]["world_forge"]
        forge_manifest = (forge_root / "manifest.json").read_bytes()
    except (KeyError, OSError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(
            "attendee live World Forge dependency is unavailable"
        ) from error
    if dependency != _LIVE.WORLD_FORGE_DEPENDENCY or (
        hashlib.sha256(forge_manifest).hexdigest() != dependency["manifest_sha256"]
    ):
        raise ValueError("attendee live World Forge dependency is incompatible")
    return forge_root, stage_root


def startup_announcement(
    pair: LiveServerPair,
    composition: LiveAttendeeComposition,
) -> dict[str, object]:
    presenter = composition.presenter
    base = _LIVE.startup_announcement(pair, presenter)
    base.update(
        {
            "contract_revision": CONTRACT_REVISION,
            "external_attendee_tracing": False,
            "provider_calls": True,
        }
    )
    presenter_stage = dict(base["presenter_stage"])
    presenter_stage["contract_revision"] = CONTRACT_REVISION
    presenter_stage["command_reserved_tokens"] = presenter.stage_command_reserved_tokens
    presenter_stage["token_budget"] = _STAGE_LIVE.live_token_budget(
        presenter.stage_command_reserved_tokens,
        max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
    )
    base["presenter_stage"] = presenter_stage
    base["attendee_sessions"] = {
        "admission_closes_at_ms": composition.admission_closes_at_ms,
        "authored_options": 1,
        "context_capacity": composition.max_contexts,
        "cleanup_grace_ms": (ATTENDEE_CLEANUP_GRACE_MINUTES * MILLISECONDS_PER_MINUTE),
        "context_ttl_ms": composition.context_ttl_ms,
        "effective_command_limit": (
            presenter.max_commands
            if presenter.max_commands is not None
            else "unlimited"
        ),
        "executor": LIVE_EXECUTOR,
        "external_tracing": False,
        "generated_world_eligibility": composition.generated_world_access,
        "max_commands": presenter.max_commands,
        "max_output_tokens": composition.attendee_max_output_tokens,
        "model": composition.attendee_model,
        "command_reserved_tokens": composition.attendee_command_reserved_tokens,
        "output_publication": {
            "flush_bytes": _STAGE_LIVE.DEFAULT_PUBLICATION_CHUNK_BYTES,
            "flush_on_newline": True,
            "max_bytes_per_command": _STAGE_LIVE.MAX_OUTPUT_BYTES_PER_COMMAND,
        },
        "standard_active_session_capacity": ATTENDEE_ACTIVE_SESSIONS,
        "standard_logical_job_capacity": ATTENDEE_LOGICAL_JOBS,
        "standard_physical_worker_capacity": ATTENDEE_PHYSICAL_WORKERS,
        "token_budget": _STAGE_LIVE.live_token_budget(
            composition.attendee_command_reserved_tokens,
            max_logical_jobs=ATTENDEE_LOGICAL_JOBS,
        ),
    }
    return base


def serve_pair(pair: LiveServerPair) -> None:
    _LIVE.serve_pair(pair)


def _configure_live_transport() -> None:
    _ATTENDEE._configure_transport()
    for module in (
        _ATTENDEE,
        _ATTENDEE._SELECTION,
        _ATTENDEE._COMBINED,
        _STAGE,
        _CORE,
    ):
        module.CONTRACT_REVISION = CONTRACT_REVISION
    _STAGE.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    _CORE.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    _CORE.PREVIEW_PROVIDER_CALLS = True
    _CORE.PREVIEW_TEST_ONLY = True


def _shared_admission(
    *,
    presenter_command_reserved_tokens: int,
    attendee_command_reserved_tokens: int,
) -> ProductAdmissionLedger:
    presenter_token_budget = _STAGE_LIVE.live_token_budget(
        presenter_command_reserved_tokens,
        max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
    )
    attendee_token_budget = _STAGE_LIVE.live_token_budget(
        attendee_command_reserved_tokens,
        max_logical_jobs=ATTENDEE_LOGICAL_JOBS,
    )
    return ProductAdmissionLedger(
        ProductAdmissionLimits(
            max_active_sessions=1 + ATTENDEE_ACTIVE_SESSIONS,
            max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS + ATTENDEE_LOGICAL_JOBS,
            max_physical_workers=2 + ATTENDEE_PHYSICAL_WORKERS,
            token_budget=presenter_token_budget + attendee_token_budget,
            spend_budget_units=(_STAGE_LIVE.SPEND_BUDGET_UNITS + ATTENDEE_SPEND_BUDGET),
            presenter_stage_reserved_sessions=1,
            presenter_stage_reserved_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
            presenter_stage_reserved_physical_workers=2,
            presenter_stage_reserved_token_budget=presenter_token_budget,
            presenter_stage_reserved_spend_budget_units=(
                _STAGE_LIVE.SPEND_BUDGET_UNITS
            ),
        )
    )


def _attendee_launch_limits(
    max_commands: int | None,
    command_reserved_tokens: int,
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


def _validate_attendee_limits(
    *,
    max_contexts: int,
    context_ttl_minutes: int,
    admission_duration_minutes: int,
) -> None:
    if (
        isinstance(max_contexts, bool)
        or not isinstance(max_contexts, int)
        or not 1 <= max_contexts <= MAXIMUM_CONTEXTS
    ):
        raise ValueError(f"max contexts must be between 1 and {MAXIMUM_CONTEXTS}")
    if (
        isinstance(context_ttl_minutes, bool)
        or not isinstance(context_ttl_minutes, int)
        or context_ttl_minutes <= 0
    ):
        raise ValueError("context TTL minutes must be positive")
    if (
        isinstance(admission_duration_minutes, bool)
        or not isinstance(admission_duration_minutes, int)
        or not 1 <= admission_duration_minutes <= MAXIMUM_ADMISSION_DURATION_MINUTES
    ):
        raise ValueError(
            "admission duration minutes must be between 1 and "
            f"{MAXIMUM_ADMISSION_DURATION_MINUTES}"
        )
    if context_ttl_minutes > admission_duration_minutes:
        raise ValueError("context TTL cannot exceed the admission duration")


def _validate_generated_world_configuration(
    *,
    generated_world_access: bool,
    generated_eligibility_key: bytes | None,
    attendee_notice_version: str,
    max_revisions: int,
    max_offerings: int,
    generated_publication_capacity: int,
) -> None:
    """Validate the bounded, explicitly opted-in World Access composition."""
    if not isinstance(generated_world_access, bool):
        raise ValueError("generated_world_access must be boolean")
    if (
        not isinstance(attendee_notice_version, str)
        or _NOTICE_VERSION_PATTERN.fullmatch(attendee_notice_version) is None
    ):
        raise ValueError("attendee notice version is invalid")
    for value, name, maximum in (
        (max_revisions, "max revisions", MAXIMUM_ATTENDEE_REVISIONS),
        (max_offerings, "max offerings", MAXIMUM_ATTENDEE_OFFERINGS),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise ValueError(f"{name} must be between 1 and {maximum}")

    if not generated_world_access:
        if generated_eligibility_key is not None:
            raise ValueError("generated-world eligibility key is unexpected")
        if (
            max_revisions != 1
            or max_offerings != 1
            or generated_publication_capacity != 0
        ):
            raise ValueError(
                "authored-only attendee composition requires its default capacities"
            )
        return

    if (
        not isinstance(generated_eligibility_key, bytes)
        or len(generated_eligibility_key) < 32
    ):
        raise ValueError("generated-world eligibility key is invalid")
    if (
        isinstance(generated_publication_capacity, bool)
        or not isinstance(generated_publication_capacity, int)
        or not 1 <= generated_publication_capacity <= MAXIMUM_GENERATED_PUBLICATIONS
    ):
        raise ValueError(
            "generated publication capacity must be between 1 and "
            f"{MAXIMUM_GENERATED_PUBLICATIONS}"
        )
    minimum_capacity = 1 + generated_publication_capacity
    if max_revisions < minimum_capacity or max_offerings < minimum_capacity:
        raise ValueError(
            "attendee revision and offering capacities must cover the authored "
            "offering and lifetime generated publication budget"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the explicit live authored-attendee development target."
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
    serve.add_argument("--max-contexts", type=int, default=DEFAULT_MAX_CONTEXTS)
    serve.add_argument(
        "--context-ttl-minutes",
        type=int,
        default=DEFAULT_CONTEXT_TTL_MINUTES,
    )
    serve.add_argument(
        "--admission-duration-minutes",
        type=int,
        default=DEFAULT_ADMISSION_DURATION_MINUTES,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "init":
            _LIVE.initialize_private_files(
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
                        "authored_revision_digest": _LIVE.authored_digest(
                            arguments.profile_path
                        )
                    },
                    sort_keys=True,
                )
            )
            return 0

        _LIVE._COMBINED._validate_listener(
            arguments.host, arguments.forge_port, "Forge"
        )
        _LIVE._COMBINED._validate_listener(
            arguments.host, arguments.stage_port, "Product"
        )
        _validate_attendee_limits(
            max_contexts=arguments.max_contexts,
            context_ttl_minutes=arguments.context_ttl_minutes,
            admission_duration_minutes=arguments.admission_duration_minutes,
        )
        if arguments.deadline_seconds <= 0:
            raise ValueError("deadline seconds must be positive")
        bootstrap = _LIVE._COMBINED.load_bootstrap(arguments.bootstrap_file)
        forge_bearer = _FORGE_TRANSPORT.load_bearer(arguments.forge_token_file)
        stage_bearer = _LIFECYCLE.load_preview_bearer_file(arguments.stage_token_file)
        forge_root, stage_root = validate_contract_dependency(
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
            max_contexts=arguments.max_contexts,
            context_ttl_minutes=arguments.context_ttl_minutes,
            admission_duration_minutes=arguments.admission_duration_minutes,
        )
        pair = _build_validated_server_pair(
            host=arguments.host,
            forge_port=arguments.forge_port,
            stage_port=arguments.stage_port,
            forge_bearer=forge_bearer,
            stage_bearer=stage_bearer,
            validated_forge_root=forge_root,
            validated_stage_root=stage_root,
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
        print(f"Live attendee error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
