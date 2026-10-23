#!/usr/bin/env python3
"""Serve the provider-free Phase 8A generated-world access preview.

This executable is an explicitly invoked, loopback-only private-BFF
composition.  Its selected Forge record and immutable artifact are fixed test
truth: it performs no provider call and is not evidence that live Forge or any
production capability is enabled.  Importing this module creates no listener,
worker, credential lookup, provider, or normal DREAMLAND runtime integration.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote, urlsplit

from dreamland.product.attendee import (
    AttendeeBffAuthoritySeed,
    AttendeeContextPolicy,
    AttendeeSessionCoreService,
    PublicAttendeeSessionService,
)
from dreamland.product.attendee_catalog import (
    GeneratedWorldCatalogPolicy,
    GeneratedWorldCatalogService,
)
from dreamland.product.core import ProductCorePolicy
from dreamland.product.forge import (
    DEFAULT_SELECTED_HANDOFF_POLICY_ID,
    ForgeSelectedRevisionRecord,
    ForgeServiceError,
)
from dreamland.product.forge_artifacts import (
    ImmutableForgeRevision,
    canonical_revision_document,
)
from dreamland.product.generated_access import (
    GeneratedAccessActorSeed,
    GeneratedAccessError,
    GeneratedAccessPolicy,
    GeneratedWorldAccessService,
)
from dreamland.product.launch_selection import (
    LaunchOfferingSeed,
    LaunchSelectionActorSeed,
    LaunchSelectionError,
    LaunchSelectionOrigin,
    LaunchSelectionPolicy,
    LaunchSelectionRegistry,
)
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LifecyclePolicy,
    ProductOperation,
    ProductRole,
)
from dreamland.state_profiles import StateProfile


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_world_access_preview",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_ATTENDEE: Any = _load_tool("product_attendee_preview")
_SELECTION: Any = _ATTENDEE._SELECTION
_CORE: Any = _ATTENDEE._CORE
_LIFECYCLE: Any = _ATTENDEE._LIFECYCLE

WORLD_ACCESS_CONTRACT_NAME = "dreamland-private-world-access-api"
WORLD_ACCESS_CONTRACT_REVISION = "0.1.0-rc.3"
WORLD_ACCESS_CONTRACT_COMPATIBILITY = "unfrozen-integration-preview"
PRODUCT_CONTRACT_REVISION = "0.7.0-rc.3"
WORLD_FORGE_CONTRACT_REVISION = "0.1.0-rc.4"
PREVIEW_KIND = "provider_free_generated_access_preview"
PREVIEW_EXECUTOR = _ATTENDEE.PREVIEW_EXECUTOR
SCENARIOS = ("world-access-happy",)

IDENTITY_PATH = "/world-access/v1/identity"
PROMOTIONS_PATH = "/world-access/v1/promotions"
PROMOTION_PATH_PREFIX = "/world-access/v1/promotions/"
CATALOG_PATH = "/world-access/v1/catalog"
LAUNCH_SELECTIONS_PATH = "/world-access/v1/launch-selections"

OPERATOR_CONTEXT = "actor:phase8a_preview_operator_00000001"
FORGE_JOB_REFERENCE = "forge-reference:phase8a-preview-selected-00000001"
NOTICE_VERSION = "notice:phase8a-world-access-v1"
PRIVATE_BFF_AUTHORITY = _ATTENDEE.PRIVATE_BFF_AUTHORITY
ATTENDEE_CONTEXT_CAPACITY = _ATTENDEE.ATTENDEE_CONTEXT_CAPACITY
MILLISECONDS_PER_MINUTE = 60_000

DEFAULT_WORLD_ACCESS_CONTRACT_ROOT = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "world-access-api"
    / f"v{WORLD_ACCESS_CONTRACT_REVISION}"
)

_PROMOTION_ID_PATTERN = re.compile(
    r"^generated-access-promotion:[A-Za-z0-9_-]{16,128}$"
)
_ACCESS_RECORD_ID_PATTERN = re.compile(
    r"^generated-access-record:[A-Za-z0-9_-]{16,128}$"
)
_OFFERING_ID_PATTERN = re.compile(r"^offering:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_OFFERING_REVISION_PATTERN = re.compile(
    r"^offering-revision:[A-Za-z0-9][A-Za-z0-9_.-]{15,127}$"
)
_NOTICE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_WORLD_ACTOR_CONTEXT_PATTERN = re.compile(r"^[\x21-\x7e]{16,512}$")
_WORLD_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,128}$")
_RESULT_IDENTITY_TEXT_PATTERN = re.compile(r"^[\x21-\x7e]{1,256}$")

_ELIGIBILITY_KEY = b"phase8a preview catalog eligibility proof key v1"
_LOCATOR_KEY = b"phase8a preview catalog locator derivation key v1"


class WorldAccessResultIdentity:
    """Immutable publication identity for one World Access server instance."""

    __slots__ = (
        "contract_name",
        "contract_revision",
        "contract_compatibility",
        "product_contract_revision",
        "world_forge_contract_revision",
        "preview_kind",
        "provider_calls",
        "live_forge_evidence",
        "live_capability_evidence",
        "live_attendee_execution",
    )

    def __init__(
        self,
        *,
        contract_name: str,
        contract_revision: str,
        contract_compatibility: str,
        product_contract_revision: str,
        world_forge_contract_revision: str,
        preview_kind: str,
        provider_calls: bool,
        live_forge_evidence: bool,
        live_capability_evidence: bool,
        live_attendee_execution: bool | None = None,
    ) -> None:
        values: dict[str, object] = {
            "contract_name": contract_name,
            "contract_revision": contract_revision,
            "contract_compatibility": contract_compatibility,
            "product_contract_revision": product_contract_revision,
            "world_forge_contract_revision": world_forge_contract_revision,
            "preview_kind": preview_kind,
            "provider_calls": provider_calls,
            "live_forge_evidence": live_forge_evidence,
            "live_capability_evidence": live_capability_evidence,
            "live_attendee_execution": live_attendee_execution,
        }
        for field_name in (
            "contract_name",
            "contract_revision",
            "contract_compatibility",
            "product_contract_revision",
            "world_forge_contract_revision",
            "preview_kind",
        ):
            value = values[field_name]
            if (
                not isinstance(value, str)
                or _RESULT_IDENTITY_TEXT_PATTERN.fullmatch(value) is None
            ):
                raise ValueError(
                    f"World Access result identity {field_name} is invalid"
                )
        for field_name in (
            "provider_calls",
            "live_forge_evidence",
            "live_capability_evidence",
        ):
            if type(values[field_name]) is not bool:
                raise TypeError(
                    f"World Access result identity {field_name} must be boolean"
                )
        if (
            live_attendee_execution is not None
            and type(live_attendee_execution) is not bool
        ):
            raise TypeError(
                "World Access result identity live_attendee_execution must be "
                "boolean or None"
            )
        for field_name, value in values.items():
            object.__setattr__(self, field_name, value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("World Access result identity is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("World Access result identity is immutable")

    def contract_projection(self) -> dict[str, str]:
        return {
            "name": self.contract_name,
            "revision": self.contract_revision,
            "compatibility": self.contract_compatibility,
        }

    def dependencies_projection(self) -> dict[str, str]:
        return {
            "product_api": self.product_contract_revision,
            "world_forge_api": self.world_forge_contract_revision,
        }

    def preview_projection(self) -> dict[str, object]:
        projection: dict[str, object] = {
            "kind": self.preview_kind,
            "provider_calls": self.provider_calls,
            "live_forge_evidence": self.live_forge_evidence,
            "live_capability_evidence": self.live_capability_evidence,
            "loopback_only": True,
            "process_local": True,
            "capabilities_enabled": [],
        }
        if self.live_attendee_execution is not None:
            projection["live_attendee_execution"] = self.live_attendee_execution
        return projection


DEFAULT_WORLD_ACCESS_RESULT_IDENTITY = WorldAccessResultIdentity(
    contract_name=WORLD_ACCESS_CONTRACT_NAME,
    contract_revision=WORLD_ACCESS_CONTRACT_REVISION,
    contract_compatibility=WORLD_ACCESS_CONTRACT_COMPATIBILITY,
    product_contract_revision=PRODUCT_CONTRACT_REVISION,
    world_forge_contract_revision=WORLD_FORGE_CONTRACT_REVISION,
    preview_kind=PREVIEW_KIND,
    provider_calls=False,
    live_forge_evidence=False,
    live_capability_evidence=False,
)

_WORLD_SAFE_ERRORS: dict[str, tuple[int, str, str, int | None]] = {
    "unauthorized": (
        401,
        "Missing or invalid private World Access authority.",
        "none",
        None,
    ),
    "not_found": (
        404,
        "The requested World Access resource is unavailable.",
        "none",
        None,
    ),
    "invalid_request": (400, "The World Access request is invalid.", "none", None),
    "invalid_json": (400, "The request body is not valid JSON.", "none", None),
    "unsupported_media_type": (
        415,
        "The request content type is unsupported.",
        "none",
        None,
    ),
    "content_length_required": (
        411,
        "A Content-Length header is required.",
        "none",
        None,
    ),
    "request_too_large": (
        413,
        "The request body exceeds the World Access preview limit.",
        "none",
        None,
    ),
    "expected_epoch_required": (
        400,
        "Expected World Access epoch evidence is required.",
        "none",
        None,
    ),
    "outcome_unknown": (
        410,
        "The prior mutation outcome cannot be reconciled.",
        "none",
        None,
    ),
    "service_epoch_changed": (
        410,
        "The World Access service epoch has changed.",
        "none",
        None,
    ),
    "authority_denied": (
        403,
        "The requested World Access resource is unavailable.",
        "none",
        None,
    ),
    "selection_unavailable": (
        404,
        "The selected World Forge revision is unavailable for promotion.",
        "none",
        None,
    ),
    "revision_unavailable": (
        409,
        "The selected World Forge artifact is unavailable for promotion.",
        "none",
        None,
    ),
    "source_temporarily_unavailable": (
        503,
        "The selected World Forge revision is temporarily unavailable.",
        "after_delay",
        250,
    ),
    "capacity_exhausted": (
        503,
        "Generated-world access capacity is temporarily exhausted.",
        "after_delay",
        250,
    ),
    "identity_capacity_exhausted": (
        503,
        "Generated-world identity capacity is temporarily exhausted.",
        "after_delay",
        250,
    ),
    "idempotency_capacity_exhausted": (
        503,
        "World Access replay capacity is temporarily exhausted.",
        "after_delay",
        250,
    ),
    "idempotency_conflict": (
        409,
        "The idempotency key is already bound to another request.",
        "none",
        None,
    ),
    "access_unavailable": (
        404,
        "The generated-world access record is unavailable.",
        "none",
        None,
    ),
    "access_record_changed": (
        409,
        "The generated-world access record has changed.",
        "none",
        None,
    ),
    "reveal_unavailable": (
        409,
        "The generated-world access record cannot be revealed.",
        "none",
        None,
    ),
    "publication_capacity_exhausted": (
        503,
        "Generated-world publication capacity is temporarily exhausted.",
        "after_delay",
        250,
    ),
    "publication_conflict": (
        409,
        "The generated-world publication conflicts with current state.",
        "none",
        None,
    ),
    "launch_selection_unavailable": (
        404,
        "The attendee launch selection is unavailable.",
        "none",
        None,
    ),
    "notice_version_mismatch": (
        409,
        "The presented privacy notice version does not match this offering.",
        "none",
        None,
    ),
    "launch_selection_capacity_exhausted": (
        503,
        "Attendee launch-selection capacity is temporarily exhausted.",
        "after_delay",
        1_000,
    ),
    "service_not_open": (
        503,
        "Attendee admission is not open yet.",
        "after_delay",
        1_000,
    ),
    "service_closed": (410, "Attendee admission is closed.", "none", None),
    "internal_error": (
        500,
        "The World Access preview could not complete the request.",
        "none",
        None,
    ),
}


class _RegistryTokens:
    def __init__(self, service_epoch: str) -> None:
        self._service_epoch = service_epoch
        self._sequence = 0

    def __call__(self, purpose: str) -> str:
        if purpose == "epoch":
            return self._service_epoch
        if purpose != "launch-grant":
            raise ValueError("unsupported World Access registry token purpose")
        self._sequence += 1
        return f"launch-grant:phase8a_preview_candidate_{self._sequence:08d}"


class _AccessTokens:
    def __init__(self) -> None:
        self._sequence = 0

    def __call__(self, purpose: str) -> str:
        self._sequence += 1
        return f"{purpose}:phase8a_preview_{self._sequence:08d}"


class StartupAnchoredPreviewClock:
    """Expose current wall time while measuring elapsed time monotonically."""

    def __init__(
        self,
        *,
        wall_time_ms: Callable[[], int] | None = None,
        monotonic_ms: Callable[[], int] | None = None,
    ) -> None:
        wall_clock = wall_time_ms or (lambda: time.time_ns() // 1_000_000)
        monotonic_clock = monotonic_ms or (lambda: time.monotonic_ns() // 1_000_000)
        self._monotonic_ms = monotonic_clock
        self._wall_anchor_ms = wall_clock()
        self._monotonic_anchor_ms = monotonic_clock()
        self._advance_ms = 0
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        elapsed_ms = max(0, self._monotonic_ms() - self._monotonic_anchor_ms)
        with self._lock:
            return self._wall_anchor_ms + elapsed_ms + self._advance_ms

    def advance(self, milliseconds: int) -> None:
        if milliseconds < 0:
            raise ValueError("preview clock cannot move backwards")
        with self._lock:
            self._advance_ms += milliseconds


class _DeterministicSelectedSource:
    """In-memory test truth; deliberately not a simulated live Forge service."""

    def __init__(self, *, clock_ms: Callable[[], int]) -> None:
        self._clock_ms = clock_ms
        profile = _generated_profile()
        document = canonical_revision_document(profile)
        canonical = (
            json.dumps(
                document,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        sha256 = hashlib.sha256(canonical).hexdigest()
        self.artifact = ImmutableForgeRevision(
            revision_id=f"world-revision:sha256:{sha256}",
            digest=f"sha256:{sha256}",
            path=Path("provider-free-in-memory-world-revision.json"),
            document=document,
            profile=profile,
        )
        self.resolve_calls = 0
        self.load_calls = 0

    def resolve_selected(self, job_reference: str) -> ForgeSelectedRevisionRecord:
        self.resolve_calls += 1
        if job_reference != FORGE_JOB_REFERENCE:
            raise ForgeServiceError(
                "selected_record_unavailable",
                "The selected World Forge record is unavailable.",
                status=404,
            )
        now_ms = self._clock_ms()
        selected_at = datetime.fromtimestamp(now_ms / 1_000, UTC).isoformat()
        expires_at = datetime.fromtimestamp(
            (now_ms + 30 * 60_000) / 1_000, UTC
        ).isoformat()
        return ForgeSelectedRevisionRecord(
            selected_record_id="forge-selected-record:phase8a-preview-00000001",
            status="selected",
            service_epoch="forge-epoch:phase8a-preview-00000001",
            job_id="forge-job:phase8a-preview-00000001",
            job_reference=FORGE_JOB_REFERENCE,
            actor_id="forge-presenter:phase8a-preview",
            selected_at=selected_at,
            expires_at=expires_at,
            policy_id=DEFAULT_SELECTED_HANDOFF_POLICY_ID,
            revision_id=self.artifact.revision_id,
            immutable_digest=self.artifact.digest,
            source_kind=PREVIEW_KIND,
            source_fingerprint="sha256:" + "1" * 64,
            receipt_correlation_id="forge-receipt:phase8a-preview-00000001",
            receipt_fingerprint="sha256:" + "2" * 64,
        )

    def load_revision(
        self, revision_id: str, immutable_digest: str
    ) -> ImmutableForgeRevision:
        self.load_calls += 1
        if (
            revision_id != self.artifact.revision_id
            or immutable_digest != self.artifact.digest
        ):
            raise ValueError("deterministic selected artifact is unavailable")
        return self.artifact


class WorldAccessPreviewHarness(_ATTENDEE.AttendeePreviewHarness):
    """Compose hidden access, catalog publication, and the attendee core."""

    def __init__(self, scenario: str, *, clock: Any | None = None) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown World Access preview scenario")
        resolved_clock = clock if clock is not None else StartupAnchoredPreviewClock()
        super().__init__(_ATTENDEE.SCENARIOS[0], clock=resolved_clock)
        self.scenario = scenario
        now_ms = self.clock.now_ms()
        self.selected_source = _DeterministicSelectedSource(clock_ms=self.clock.now_ms)
        self.access = GeneratedWorldAccessService(
            actors=(
                GeneratedAccessActorSeed(
                    authority=OPERATOR_CONTEXT,
                    actor_id="operator:phase8a-preview",
                    expires_at_ms=now_ms + 24 * 60 * MILLISECONDS_PER_MINUTE,
                ),
            ),
            source=self.selected_source,
            policy=GeneratedAccessPolicy(
                max_actor_authorities=1,
                max_active_promotions=4,
                max_access_records=8,
                max_idempotency_records=32,
                max_token_history=64,
                access_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
                idempotency_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
                failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                accepted_selected_policy_ids=frozenset(
                    {DEFAULT_SELECTED_HANDOFF_POLICY_ID}
                ),
                accepted_source_kinds=frozenset({PREVIEW_KIND}),
            ),
            clock_ms=self.clock.now_ms,
            monotonic_ms=self.clock.now_ms,
            token_factory=_AccessTokens(),
            service_epoch=self.attendee_service.service_epoch,
        )
        self.world_catalog = GeneratedWorldCatalogService(
            access=self.access,
            registry=self.attendee_registry,
            attendee=self.attendee_service,
            policy=GeneratedWorldCatalogPolicy(
                launch_limits=_launch_limits(),
                notice_version=NOTICE_VERSION,
                # Reuse the already-constructed attendee boundary exactly. A
                # live clock can advance between adjacent service constructors,
                # and the catalog must never outlive attendee admission.
                admission_closes_at_ms=(
                    self.attendee_service._policy.admission_closes_at_ms
                ),
                maximum_generated_offerings=4,
                maximum_catalog_items=8,
                launch_grant_ttl_ms=10 * MILLISECONDS_PER_MINUTE,
            ),
            locator_key=_LOCATOR_KEY,
            eligibility_key=_ELIGIBILITY_KEY,
            clock_ms=self.clock.now_ms,
        )

    def _build_attendee_service(self) -> PublicAttendeeSessionService:
        now_ms = self.clock.now_ms()
        actor_expiry_ms = now_ms + 24 * 60 * MILLISECONDS_PER_MINUTE
        document = _ATTENDEE._authored_document()
        revision = _ATTENDEE._revision_seed(document)
        actors = tuple(
            LaunchSelectionActorSeed(
                authority=f"actor:attendee_preview_context_{index:04d}",
                actor_id=f"attendee-preview-{index:04d}",
                role=ProductRole.ATTENDEE,
                expires_at_ms=actor_expiry_ms,
            )
            for index in range(1, ATTENDEE_CONTEXT_CAPACITY + 1)
        )
        clock_ms = self.clock.now_ms
        self.attendee_registry = LaunchSelectionRegistry(
            revisions=(revision,),
            offerings=(
                LaunchOfferingSeed(
                    offering_id="offering:attendee-authored-preview",
                    launch_selection_id=_ATTENDEE.ATTENDEE_SELECTION_ID,
                    revision_id=revision.revision_id,
                    origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                    title="Authored attendee world",
                    source_label="Authored",
                    revision_label=_ATTENDEE.ATTENDEE_REVISION_LABEL,
                    role=ProductRole.ATTENDEE,
                    allowed_operations=frozenset(
                        {ProductOperation.CREATE, ProductOperation.SWITCH}
                    ),
                    limits=_launch_limits(),
                    policy_id="policy:attendee-preview-v1",
                    notice_version=NOTICE_VERSION,
                    expires_at_ms=now_ms + 60 * MILLISECONDS_PER_MINUTE,
                    launch_grant_ttl_ms=20 * MILLISECONDS_PER_MINUTE,
                ),
            ),
            actors=actors,
            policy=LaunchSelectionPolicy(
                max_revisions=8,
                max_offerings=8,
                max_actor_authorities=ATTENDEE_CONTEXT_CAPACITY,
                max_active_launch_grants=16,
                max_idempotency_records=64,
                max_token_history=128,
                max_revision_bytes=4 * 1024 * 1024,
                max_launch_grant_ttl_ms=20 * MILLISECONDS_PER_MINUTE,
                idempotency_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
            ),
            allowed_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            clock_ms=clock_ms,
            monotonic_ms=clock_ms,
            token_factory=_RegistryTokens(self.stage_service.service_epoch),
        )
        self.attendee_core = AttendeeSessionCoreService(
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
            executor=_CORE.DeterministicPreviewExecutor(),
            submit_task=self.submit_task,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=20 * MILLISECONDS_PER_MINUTE,
                idle_ttl_ms=10 * MILLISECONDS_PER_MINUTE,
                tombstone_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                max_active_sessions=4,
                max_tombstones=8,
                max_idempotency_records=128,
                max_launch_grants=16,
                max_actor_authorities=ATTENDEE_CONTEXT_CAPACITY,
                max_revisions=8,
            ),
            core_policy=ProductCorePolicy(
                max_commands_per_session=8,
                command_deadline_ms=10_000,
                cancellation_deadline_ms=2_000,
                max_output_bytes_per_command=16 * 1_024,
                max_output_chunks_per_command=16,
                max_runtime_document_bytes=4 * 1024 * 1024,
                reserved_tokens_per_command=32,
                reserved_spend_units_per_command=32,
                max_attempts_per_command=1,
            ),
            admission=self.stage_service.admission,
            clock_ms=clock_ms,
            monotonic_ms=clock_ms,
            token_factory=_ATTENDEE._CoreTokens(self.stage_service.service_epoch),
            generated_world_access=True,
        )
        return PublicAttendeeSessionService(
            registry=self.attendee_registry,
            core=self.attendee_core,
            context_actors=actors,
            bff_authorities=(
                AttendeeBffAuthoritySeed(
                    authority=PRIVATE_BFF_AUTHORITY,
                    authority_id="bff:attendee-preview",
                    expires_at_ms=actor_expiry_ms,
                ),
            ),
            policy=AttendeeContextPolicy(
                max_contexts=ATTENDEE_CONTEXT_CAPACITY,
                context_ttl_ms=60 * MILLISECONDS_PER_MINUTE,
                max_bootstrap_idempotency_records=16,
                max_exchange_idempotency_records=64,
                admission_opens_at_ms=now_ms,
                admission_closes_at_ms=(now_ms + 12 * 60 * MILLISECONDS_PER_MINUTE),
            ),
            generated_world_access=True,
            generated_eligibility_key=_ELIGIBILITY_KEY,
        )


class WorldAccessPreviewServer(_ATTENDEE.AttendeePreviewServer):
    """The existing Product preview plus six additive World Access routes."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        scenario: str,
        product_contract_root: Path,
        world_access_contract_root: Path,
        harness_factory: Callable[[str], WorldAccessPreviewHarness] = (
            WorldAccessPreviewHarness
        ),
        result_identity: WorldAccessResultIdentity = (
            DEFAULT_WORLD_ACCESS_RESULT_IDENTITY
        ),
    ) -> None:
        if not isinstance(result_identity, WorldAccessResultIdentity):
            raise TypeError(
                "World Access server result_identity must be a "
                "WorldAccessResultIdentity"
            )
        self.world_access_contract_root = world_access_contract_root
        self.result_identity = result_identity
        super().__init__(
            address,
            bearer=bearer,
            scenario=scenario,
            contract_root=product_contract_root,
            harness_factory=harness_factory,
        )
        self.RequestHandlerClass = WorldAccessPreviewHandler


class WorldAccessPreviewHandler(_ATTENDEE.AttendeePreviewHandler):
    server: WorldAccessPreviewServer
    server_version = "DREAMLANDWorldAccessPreview/0.1"

    def _additional_get_operations(self) -> frozenset[str]:
        return super()._additional_get_operations() | frozenset(
            {"world-access-promotion", "world-access-catalog"}
        )

    def _additional_post_operations(self) -> frozenset[str]:
        return super()._additional_post_operations() | frozenset(
            {
                "world-access-promote",
                "world-access-reveal",
                "world-access-exchange",
            }
        )

    def _route(self) -> tuple[str, str | None, str | None, str]:
        parsed = urlsplit(self.path)
        if parsed.fragment:
            return "not-found", None, None, parsed.query
        if parsed.path == IDENTITY_PATH:
            # Reuse the parent's bearer-only capabilities dispatch category.
            return "capabilities", None, None, parsed.query
        if parsed.path == PROMOTIONS_PATH:
            return "world-access-promote", None, None, parsed.query
        if parsed.path == CATALOG_PATH:
            return "world-access-catalog", None, None, parsed.query
        if parsed.path == LAUNCH_SELECTIONS_PATH:
            return "world-access-exchange", None, None, parsed.query
        if parsed.path.startswith(PROMOTION_PATH_PREFIX):
            parts = parsed.path.removeprefix(PROMOTION_PATH_PREFIX).split("/")
            if not parts or len(parts[0]) > 180:
                return "not-found", None, None, parsed.query
            promotion_id = unquote(parts[0])
            if _PROMOTION_ID_PATTERN.fullmatch(promotion_id) is None:
                return "not-found", None, None, parsed.query
            if len(parts) == 1:
                return "world-access-promotion", promotion_id, None, parsed.query
            if len(parts) == 2 and parts[1] == "reveal":
                return "world-access-reveal", promotion_id, None, parsed.query
            return "not-found", None, None, parsed.query
        return super()._route()

    def _dispatch_get(
        self,
        operation: str,
        reference: str | None,
        command_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object] | None:
        if urlsplit(self.path).path == IDENTITY_PATH:
            self._require_empty_query(raw_query)
            return _world_success(
                {
                    "kind": "world_access_identity",
                    "service_epoch": self.server.preview.access.service_epoch,
                    "dependencies": (
                        self.server.result_identity.dependencies_projection()
                    ),
                },
                request_id,
                self.server.result_identity,
            )
        if operation == "world-access-promotion":
            self._require_empty_query(raw_query)
            if reference is None:
                raise _world_transport_error("not_found")
            expected_epoch, actor_context = self._read_world_context()
            try:
                domain = self.server.preview.access.status(
                    actor_authority=actor_context,
                    expected_epoch=expected_epoch,
                    promotion_id=reference,
                )
            except GeneratedAccessError as error:
                raise _world_domain_error(error) from None
            return _world_success(domain, request_id, self.server.result_identity)
        if operation == "world-access-catalog":
            self._require_empty_query(raw_query)
            expected_epoch, actor_context = self._read_world_context()
            try:
                domain = self.server.preview.world_catalog.catalog(
                    actor_authority=actor_context,
                    expected_epoch=expected_epoch,
                )
            except (GeneratedAccessError, LaunchSelectionError) as error:
                raise _world_domain_error(error) from None
            return _world_success(domain, request_id, self.server.result_identity)
        return super()._dispatch_get(
            operation,
            reference,
            command_id,
            raw_query,
            request_id,
        )

    def _dispatch_post(
        self,
        operation: str,
        reference: str | None,
        command_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object] | None:
        if operation == "world-access-promote":
            self._require_empty_query(raw_query)
            expected_epoch, actor_context = self._read_world_context()
            idempotency_key = self._read_world_idempotency_key()
            body = self._read_json_object(frozenset({"forge_job_reference"}))
            job_reference = body["forge_job_reference"]
            if not isinstance(job_reference, str):
                raise _world_transport_error("invalid_request")
            try:
                domain = self.server.preview.access.promote(
                    actor_authority=actor_context,
                    expected_epoch=expected_epoch,
                    idempotency_key=idempotency_key,
                    job_reference=job_reference,
                )
            except GeneratedAccessError as error:
                raise _world_domain_error(error) from None
            self._world_success_status = HTTPStatus.CREATED
            return _world_success(domain, request_id, self.server.result_identity)
        if operation == "world-access-reveal":
            self._require_empty_query(raw_query)
            if reference is None:
                raise _world_transport_error("not_found")
            expected_epoch, actor_context = self._read_world_context()
            idempotency_key = self._read_world_idempotency_key()
            body = self._read_json_object(frozenset({"expected_access_record_id"}))
            access_record_id = body["expected_access_record_id"]
            if (
                not isinstance(access_record_id, str)
                or _ACCESS_RECORD_ID_PATTERN.fullmatch(access_record_id) is None
            ):
                raise _world_transport_error("invalid_request")
            try:
                domain = self.server.preview.world_catalog.reveal(
                    actor_authority=actor_context,
                    expected_epoch=expected_epoch,
                    idempotency_key=idempotency_key,
                    promotion_id=reference,
                    expected_access_record_id=access_record_id,
                )
            except (GeneratedAccessError, LaunchSelectionError) as error:
                raise _world_domain_error(error) from None
            return _world_success(domain, request_id, self.server.result_identity)
        if operation == "world-access-exchange":
            self._require_empty_query(raw_query)
            expected_epoch, actor_context = self._read_world_context()
            idempotency_key = self._read_world_idempotency_key()
            body = self._read_json_object(
                frozenset(
                    {
                        "offering_id",
                        "offering_revision",
                        "notice_version_presented",
                    }
                )
            )
            offering_id = body["offering_id"]
            offering_revision = body["offering_revision"]
            notice_version = body["notice_version_presented"]
            if (
                not isinstance(offering_id, str)
                or _OFFERING_ID_PATTERN.fullmatch(offering_id) is None
                or not isinstance(offering_revision, str)
                or _OFFERING_REVISION_PATTERN.fullmatch(offering_revision) is None
                or not isinstance(notice_version, str)
                or _NOTICE_VERSION_PATTERN.fullmatch(notice_version) is None
            ):
                raise _world_transport_error("invalid_request")
            try:
                domain = self.server.preview.world_catalog.exchange(
                    actor_authority=actor_context,
                    expected_epoch=expected_epoch,
                    offering_id=offering_id,
                    offering_revision=offering_revision,
                    notice_version_presented=notice_version,
                    idempotency_key=idempotency_key,
                )
            except (GeneratedAccessError, LaunchSelectionError) as error:
                raise _world_domain_error(error) from None
            return _world_success(domain, request_id, self.server.result_identity)
        return super()._dispatch_post(
            operation,
            reference,
            command_id,
            raw_query,
            request_id,
        )

    def _send_json(
        self,
        status: HTTPStatus,
        value: Mapping[str, Any],
        *,
        content_type: str = "application/json",
    ) -> None:
        if not _is_world_path(self.path):
            super()._send_json(status, value, content_type=content_type)
            return
        override = getattr(self, "_world_success_status", None)
        if override is not None:
            status = override
            del self._world_success_status
        body = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "X-Dreamland-Contract-Revision",
            self.server.result_identity.contract_revision,
        )
        self.send_header("X-Dreamland-Integration-Preview", "true")
        if self.close_connection:
            self.send_header("Connection", "close")
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except OSError:
                return

    def _send_problem(
        self,
        status: HTTPStatus,
        code: str,
        request_id: str,
    ) -> None:
        if not _is_world_path(self.path):
            super()._send_problem(status, code, request_id)
            return
        translated = "service_epoch_changed" if code == "session_lost" else code
        detail = _WORLD_SAFE_ERRORS.get(
            translated, _WORLD_SAFE_ERRORS["internal_error"]
        )
        final_code = (
            translated if translated in _WORLD_SAFE_ERRORS else "internal_error"
        )
        self._send_json(
            HTTPStatus(detail[0]),
            {
                "request_id": request_id,
                "error": {
                    "code": final_code,
                    "message": detail[1],
                    "retry": detail[2],
                    "retry_after_ms": detail[3],
                },
            },
            content_type="application/problem+json",
        )

    def _require_empty_query(self, raw_query: str) -> None:
        if _CORE._query_pairs(raw_query):
            raise _world_transport_error("not_found")

    def _read_world_context(self) -> tuple[str, str]:
        """Read the broader opaque header grammar published by World Access."""
        return (
            self._required_header(
                _CORE.EXPECTED_EPOCH_HEADER,
                _CORE.SERVICE_EPOCH_PATTERN,
            ),
            self._required_header(
                _CORE.ACTOR_CONTEXT_HEADER,
                _WORLD_ACTOR_CONTEXT_PATTERN,
            ),
        )

    def _read_world_idempotency_key(self) -> str:
        return self._required_header(
            _CORE.IDEMPOTENCY_HEADER,
            _WORLD_IDEMPOTENCY_KEY_PATTERN,
        )


def _world_success(
    domain: Mapping[str, object],
    request_id: str,
    result_identity: WorldAccessResultIdentity,
) -> dict[str, object]:
    return {
        **dict(domain),
        "request_id": request_id,
        "contract": result_identity.contract_projection(),
        "preview": result_identity.preview_projection(),
    }


def _world_domain_error(
    error: GeneratedAccessError | LaunchSelectionError,
) -> Exception:
    code = error.code
    if isinstance(error, LaunchSelectionError) and code == "selection_unavailable":
        code = "launch_selection_unavailable"
    detail = _WORLD_SAFE_ERRORS.get(code)
    if detail is None:
        return _world_transport_error("internal_error")
    return _LIFECYCLE.PreviewTransportError(
        HTTPStatus(detail[0]),
        code,
        detail[1],
    )


def _world_transport_error(code: str) -> Exception:
    detail = _WORLD_SAFE_ERRORS[code]
    return _LIFECYCLE.PreviewTransportError(
        HTTPStatus(detail[0]),
        code,
        detail[1],
    )


def _is_world_path(path: str) -> bool:
    return urlsplit(path).path.startswith("/world-access/")


def _launch_limits() -> dict[str, int]:
    return {
        "absolute_ttl_ms": 20 * MILLISECONDS_PER_MINUTE,
        "idle_ttl_ms": 10 * MILLISECONDS_PER_MINUTE,
        "max_commands": 8,
        "max_command_bytes": 4_096,
        "command_reserved_tokens": 32,
        "command_reserved_spend_units": 32,
        "command_max_attempts": 1,
        "command_deadline_ms": 10_000,
    }


def _generated_profile() -> StateProfile:
    return StateProfile(
        name="phase8a-provider-free-preview",
        level="world",
        created_at="2026-08-30T00:00:00+00:00",
        state={
            "world_state": {
                "name": "phase8a provider-free generated world",
                "entry_host": "signal-console",
                "active_host": "signal-console",
                "purpose": {"theme": "Generated-world access preview"},
                "hosts_by_name": {
                    "signal-console": {
                        "hostname": "signal-console",
                        "login_banner": "Signal console ready.\n",
                        "purpose": {"theme": "Inspect generated access"},
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


def validate_world_access_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    tool_path = Path(__file__).with_name("product_world_access_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_world_access_preview_contract_validator",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("World Access contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("World Access contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != WORLD_ACCESS_CONTRACT_REVISION
        or result.get("valid") is not True
    ):
        raise ValueError("World Access contract is incompatible")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    source = manifest.get("source")
    if not isinstance(source, Mapping) or (
        source.get("provider_calls") is not False
        or source.get("live_forge_evidence") is not False
        or source.get("live_capability_evidence") is not False
        or source.get("production_capabilities_enabled") != []
    ):
        raise ValueError("World Access preview capabilities must remain disabled")
    return root


def build_preview_server(
    *,
    host: str,
    port: int,
    bearer: str,
    scenario: str,
    contract_root: Path,
    product_contract_root: Path,
) -> WorldAccessPreviewServer:
    if not _LIFECYCLE._loopback_host(host):
        raise ValueError("World Access preview supports only loopback hosts")
    if not 0 <= port <= 65_535:
        raise ValueError("preview port must be between 0 and 65535")
    _LIFECYCLE._validate_preview_bearer(bearer)
    if scenario not in SCENARIOS:
        raise ValueError("unknown World Access preview scenario")
    world_root = validate_world_access_contract_root(contract_root)
    product_root = _ATTENDEE.validate_contract_root(product_contract_root)
    _ATTENDEE._configure_transport()
    return WorldAccessPreviewServer(
        (host, port),
        bearer=bearer,
        scenario=scenario,
        product_contract_root=product_root,
        world_access_contract_root=world_root,
    )


def serve_preview(
    *,
    host: str,
    port: int,
    bearer: str,
    scenario: str,
    contract_root: Path,
    product_contract_root: Path,
    once: bool,
) -> None:
    server = build_preview_server(
        host=host,
        port=port,
        bearer=bearer,
        scenario=scenario,
        contract_root=contract_root,
        product_contract_root=product_contract_root,
    )
    address, bound_port = server.server_address[:2]
    print(
        json.dumps(
            {
                "capabilities_enabled": 0,
                "contract_revision": WORLD_ACCESS_CONTRACT_REVISION,
                "executor": _CORE.PREVIEW_EXECUTOR,
                "forge_job_reference": FORGE_JOB_REFERENCE,
                "host": address,
                "integration_only": True,
                "live_capability_evidence": False,
                "live_forge_evidence": False,
                "normal_runtime_composition": False,
                "operator_actor_context": OPERATOR_CONTEXT,
                "port": bound_port,
                "provider_calls": False,
                "scenario": scenario,
                "test_only": True,
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the provider-free generated-world access preview."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    bearer_source = parser.add_mutually_exclusive_group(required=True)
    bearer_source.add_argument("--token")
    bearer_source.add_argument("--token-file", type=Path)
    parser.add_argument(
        "--contract-root",
        default=DEFAULT_WORLD_ACCESS_CONTRACT_ROOT,
        type=Path,
    )
    parser.add_argument("--product-contract-root", required=True, type=Path)
    parser.add_argument("--scenario", choices=SCENARIOS, default=SCENARIOS[0])
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        bearer = (
            arguments.token
            if arguments.token is not None
            else _LIFECYCLE.load_preview_bearer_file(arguments.token_file)
        )
        serve_preview(
            host=arguments.host,
            port=arguments.port,
            bearer=bearer,
            scenario=arguments.scenario,
            contract_root=arguments.contract_root,
            product_contract_root=arguments.product_contract_root,
            once=arguments.once,
        )
    except (OSError, ValueError) as error:
        print(f"World Access preview error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
