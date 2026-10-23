#!/usr/bin/env python3
"""Serve the provider-free Phase 9A private attendee security boundary.

This executable is a deliberately separate, loopback-only BFF integration
target.  Importing it creates no credential, listener, worker, timer, provider,
profile lookup, runtime registration, or enabled product capability.  Explicit
construction owns only attendee launch selection and session-core state; Stage,
Forge, World Access, X-ray, and administrative services are not composed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote, urlsplit

from dreamland.product.admission import ProductAdmissionLedger, ProductAdmissionLimits
from dreamland.product.attendee import (
    AttendeeBffAuthoritySeed,
    AttendeeContextPolicy,
    AttendeeSessionCoreService,
    PublicAttendeeSessionService,
)
from dreamland.product.attendee_security import (
    AttendeeRequestAdmissionLedger,
    AttendeeRequestAdmissionPolicy,
)
from dreamland.product.core import ProductCorePolicy
from dreamland.product.execution import ProductCommandExecutor
from dreamland.product.launch_selection import (
    LaunchOfferingSeed,
    LaunchRevisionSeed,
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
    ProductLifecycleError,
    ProductOperation,
    ProductRole,
)


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_attendee_security_preview",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_ATTENDEE: Any = _load_tool("product_attendee_preview")
_CORE: Any = _ATTENDEE._CORE
_LIFECYCLE: Any = _ATTENDEE._LIFECYCLE

CONTRACT_NAME = "dreamland-private-product-api"
CONTRACT_REVISION = "0.8.0-rc.1"
CONTRACT_COMPATIBILITY = "unfrozen-release-candidate"
PREVIEW_EXECUTOR = "private-attendee-security-no-provider-v1"
PREVIEW_KIND = "public_attendee_private_security_integration"
SCENARIOS = ("attendee-security-happy",)

ATTENDEE_CONTEXTS_PATH = "/product/v1/attendee/contexts"
ATTENDEE_CONTEXT_PREFIX = ATTENDEE_CONTEXTS_PATH + "/"
ATTENDEE_LAUNCH_OPTIONS_PATH = "/product/v1/attendee/launch-options"
ATTENDEE_LAUNCH_SELECTIONS_PATH = "/product/v1/attendee/launch-selections"

MILLISECONDS_PER_MINUTE = 60_000
ATTENDEE_CONTEXT_CAPACITY = 4
ATTENDEE_WORKER_CAPACITY = 2
ATTENDEE_ACTIVE_SESSION_CAPACITY = 4
ATTENDEE_LOGICAL_JOB_CAPACITY = 8
ATTENDEE_CONNECTION_CAPACITY = _CORE.MAX_CONCURRENT_CONNECTIONS
ATTENDEE_FRESH_OPERATIONS_PER_CONTEXT = 8
ATTENDEE_FRESH_OPERATIONS_GLOBAL = 16
MAXIMUM_ATTENDEE_CONTEXT_CAPACITY = 64
MAXIMUM_ATTENDEE_ACTIVE_SESSION_CAPACITY = 64
MAXIMUM_ATTENDEE_CONNECTION_CAPACITY = 32
MAXIMUM_ATTENDEE_WORKER_CAPACITY = 8
MAXIMUM_ATTENDEE_FRESH_OPERATIONS_PER_CONTEXT = 64
MAXIMUM_ATTENDEE_FRESH_OPERATIONS_GLOBAL = 4_096
ATTENDEE_TOKEN_BUDGET = 256
ATTENDEE_SPEND_BUDGET = 256
ATTENDEE_NOTICE_VERSION = "notice:attendee-security-preview-v1"
ATTENDEE_POLICY_ID = "policy:attendee-security-preview-v1"
ATTENDEE_SELECTION_ID = "launch-selection:attendee_security_preview_0001"
_GATEWAY_CONTEXT_PATTERN = re.compile(r"^gateway-context:[A-Za-z0-9_-]{24,128}$")
_BFF_AUTHORITY_PATTERN = re.compile(r"^bff-authority:[A-Za-z0-9_-]{24,256}$")
_COMPOSITION_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_COMPOSITION_LIMIT = (1 << 63) - 1
_MAX_LAUNCH_LIMIT = 1_000_000_000
_MAX_TIMESTAMP_MS = 253_402_300_799_999

# This target exposes the Product contract's attendee/core subset only.  The
# full release candidate remains additive for existing consumers, but merely
# possessing this target's bearer cannot reach its Stage or extension routes.
PRIVATE_ROUTE_ALLOWLIST = frozenset(
    {
        ("GET", _CORE.CAPABILITIES_PATH),
        ("POST", ATTENDEE_CONTEXTS_PATH),
        ("DELETE", ATTENDEE_CONTEXT_PREFIX + "{gateway_context_id}"),
        ("GET", ATTENDEE_LAUNCH_OPTIONS_PATH),
        ("POST", ATTENDEE_LAUNCH_SELECTIONS_PATH),
        ("POST", _CORE.SESSIONS_PATH),
        ("GET", _CORE.CURRENT_PATH),
        ("GET", _CORE.SESSION_PATH_PREFIX + "{reference}"),
        ("GET", _CORE.SESSION_PATH_PREFIX + "{reference}/transcript"),
        ("GET", _CORE.SESSION_PATH_PREFIX + "{reference}/events"),
        ("POST", _CORE.SESSION_PATH_PREFIX + "{reference}/commands"),
        ("GET", _CORE.SESSION_PATH_PREFIX + "{reference}/commands/{command_id}"),
        (
            "POST",
            _CORE.SESSION_PATH_PREFIX + "{reference}/commands/{command_id}/cancel",
        ),
        ("POST", _CORE.SESSION_PATH_PREFIX + "{reference}/reset"),
        ("POST", _CORE.SESSION_PATH_PREFIX + "{reference}/switch"),
        ("POST", _CORE.SESSION_PATH_PREFIX + "{reference}/close"),
    }
)


@dataclass(frozen=True, slots=True)
class AttendeeSecurityAvailabilityWindow:
    """Exact attendee admission and bounded post-cutoff recovery timestamps."""

    admission_opens_at_ms: int
    admission_closes_at_ms: int
    recovery_closes_at_ms: int

    def __post_init__(self) -> None:
        for name in (
            "admission_opens_at_ms",
            "admission_closes_at_ms",
            "recovery_closes_at_ms",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_TIMESTAMP_MS
            ):
                raise ValueError(f"{name} must be a bounded timestamp")
        if self.admission_closes_at_ms <= self.admission_opens_at_ms:
            raise ValueError("attendee admission must close after it opens")
        if self.admission_closes_at_ms - self.admission_opens_at_ms <= 1_000:
            raise ValueError("attendee admission window must be longer than 1000ms")
        if self.recovery_closes_at_ms <= self.admission_closes_at_ms:
            raise ValueError("attendee recovery must close after admission")


@dataclass(frozen=True, slots=True)
class AttendeeSecurityComposition:
    """Explicit bounded dependencies for one private attendee composition."""

    executor: ProductCommandExecutor = field(repr=False)
    revision_document: Mapping[str, object] = field(repr=False)
    physical_worker_capacity: int
    active_session_capacity: int
    logical_job_capacity: int
    max_commands_per_session: int | None
    command_deadline_ms: int
    cancellation_deadline_ms: int
    max_output_bytes_per_command: int
    max_output_chunks_per_command: int
    max_runtime_document_bytes: int
    reserved_tokens_per_command: int
    reserved_spend_units_per_command: int
    token_budget: int
    spend_budget_units: int
    notice_version: str = ATTENDEE_NOTICE_VERSION
    policy_id: str = ATTENDEE_POLICY_ID
    availability_window: AttendeeSecurityAvailabilityWindow | None = None
    bootstrap_closes_at_admission: bool = False
    context_capacity: int = ATTENDEE_CONTEXT_CAPACITY
    connection_capacity: int = ATTENDEE_CONNECTION_CAPACITY
    max_fresh_operations_per_context: int = ATTENDEE_FRESH_OPERATIONS_PER_CONTEXT
    max_fresh_operations_global: int = ATTENDEE_FRESH_OPERATIONS_GLOBAL
    validate_runtime_document: Callable[[Mapping[str, object]], object] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _revision_canonical_json: bytes = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not callable(getattr(self.executor, "execute", None)):
            raise TypeError("attendee executor must implement execute")
        if self.availability_window is not None and not isinstance(
            self.availability_window,
            AttendeeSecurityAvailabilityWindow,
        ):
            raise TypeError(
                "availability_window must be AttendeeSecurityAvailabilityWindow"
            )
        if not isinstance(self.bootstrap_closes_at_admission, bool):
            raise TypeError("bootstrap_closes_at_admission must be a boolean")
        for value, name in (
            (self.notice_version, "notice_version"),
            (self.policy_id, "policy_id"),
        ):
            if (
                not isinstance(value, str)
                or _COMPOSITION_IDENTITY_PATTERN.fullmatch(value) is None
            ):
                raise ValueError(f"{name} must be a bounded safe identity")
        if not isinstance(self.revision_document, Mapping):
            raise TypeError("attendee revision_document must be a mapping")
        _require_positive_composition_limit(
            self.max_runtime_document_bytes,
            "max_runtime_document_bytes",
        )
        try:
            canonical_document = json.dumps(
                self.revision_document,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (RecursionError, TypeError, UnicodeError, ValueError) as error:
            raise ValueError(
                "attendee revision_document must be finite JSON"
            ) from error
        try:
            normalized_document = json.loads(canonical_document)
        except (RecursionError, TypeError, UnicodeError, ValueError) as error:
            raise ValueError(
                "attendee revision_document must be finite JSON"
            ) from error
        if (
            not canonical_document
            or len(canonical_document) > self.max_runtime_document_bytes
            or not isinstance(normalized_document, dict)
            or normalized_document.get("kind") != "dreamland.authored_world_revision"
            or normalized_document.get("schema_revision") != "1"
            or not isinstance(normalized_document.get("profile"), Mapping)
        ):
            raise ValueError(
                "attendee revision_document must be one bounded authored revision"
            )
        validator = self.validate_runtime_document
        if validator is not None:
            if not callable(validator):
                raise TypeError("validate_runtime_document must be callable")
            try:
                validator(json.loads(canonical_document))
            except Exception as error:
                raise ValueError(
                    "attendee revision_document violates its runtime boundary"
                ) from error
        object.__setattr__(self, "revision_document", normalized_document)
        object.__setattr__(self, "_revision_canonical_json", canonical_document)

        for name in (
            "physical_worker_capacity",
            "active_session_capacity",
            "logical_job_capacity",
            "context_capacity",
            "connection_capacity",
            "max_fresh_operations_per_context",
            "max_fresh_operations_global",
            "command_deadline_ms",
            "cancellation_deadline_ms",
            "max_output_bytes_per_command",
            "max_output_chunks_per_command",
            "reserved_tokens_per_command",
            "reserved_spend_units_per_command",
            "token_budget",
            "spend_budget_units",
        ):
            _require_positive_composition_limit(getattr(self, name), name)
        if self.max_commands_per_session is not None:
            _require_positive_composition_limit(
                self.max_commands_per_session,
                "max_commands_per_session",
            )
        for name in (
            "command_deadline_ms",
            "reserved_tokens_per_command",
            "reserved_spend_units_per_command",
        ):
            if getattr(self, name) > _MAX_LAUNCH_LIMIT:
                raise ValueError(f"{name} exceeds the launch-authority limit")
        if (
            self.max_commands_per_session is not None
            and self.max_commands_per_session > _MAX_LAUNCH_LIMIT
        ):
            raise ValueError(
                "max_commands_per_session exceeds the launch-authority limit"
            )
        if self.cancellation_deadline_ms > self.command_deadline_ms:
            raise ValueError(
                "cancellation_deadline_ms cannot exceed command_deadline_ms"
            )
        if self.physical_worker_capacity > self.logical_job_capacity:
            raise ValueError(
                "physical_worker_capacity cannot exceed logical_job_capacity"
            )
        maximums = (
            (
                "context_capacity",
                self.context_capacity,
                MAXIMUM_ATTENDEE_CONTEXT_CAPACITY,
            ),
            (
                "active_session_capacity",
                self.active_session_capacity,
                MAXIMUM_ATTENDEE_ACTIVE_SESSION_CAPACITY,
            ),
            (
                "connection_capacity",
                self.connection_capacity,
                MAXIMUM_ATTENDEE_CONNECTION_CAPACITY,
            ),
            (
                "physical_worker_capacity",
                self.physical_worker_capacity,
                MAXIMUM_ATTENDEE_WORKER_CAPACITY,
            ),
            (
                "max_fresh_operations_per_context",
                self.max_fresh_operations_per_context,
                MAXIMUM_ATTENDEE_FRESH_OPERATIONS_PER_CONTEXT,
            ),
            (
                "max_fresh_operations_global",
                self.max_fresh_operations_global,
                MAXIMUM_ATTENDEE_FRESH_OPERATIONS_GLOBAL,
            ),
        )
        for name, value, maximum in maximums:
            if value > maximum:
                raise ValueError(f"{name} cannot exceed {maximum}")
        if self.active_session_capacity > self.context_capacity:
            raise ValueError(
                "active_session_capacity cannot exceed attendee context capacity"
            )
        if self.physical_worker_capacity > self.active_session_capacity:
            raise ValueError(
                "physical_worker_capacity cannot exceed active session capacity"
            )
        if self.logical_job_capacity < self.active_session_capacity:
            raise ValueError("logical_job_capacity must cover active session capacity")
        if self.max_fresh_operations_global < self.max_fresh_operations_per_context:
            raise ValueError(
                "global fresh-operation capacity must cover the per-context limit"
            )
        if (
            self.logical_job_capacity
            > (_MAX_COMPOSITION_LIMIT - 4 * self.active_session_capacity) // 2
        ):
            raise ValueError("logical_job_capacity exceeds derived retention capacity")
        if (
            self.reserved_tokens_per_command
            > self.token_budget // self.logical_job_capacity
        ):
            raise ValueError("token budget cannot cover every logical job reservation")
        if (
            self.reserved_spend_units_per_command
            > self.spend_budget_units // self.logical_job_capacity
        ):
            raise ValueError("spend budget cannot cover every logical job reservation")


def _require_positive_composition_limit(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_COMPOSITION_LIMIT
    ):
        raise ValueError(f"{name} must be a positive bounded integer")


def derived_fresh_operation_capacity(
    *,
    context_capacity: int,
    physical_worker_capacity: int,
) -> int:
    """Cover one launch burst and one command per available worker."""

    _require_positive_composition_limit(context_capacity, "context_capacity")
    _require_positive_composition_limit(
        physical_worker_capacity,
        "physical_worker_capacity",
    )
    if context_capacity > MAXIMUM_ATTENDEE_CONTEXT_CAPACITY:
        raise ValueError(
            f"context_capacity cannot exceed {MAXIMUM_ATTENDEE_CONTEXT_CAPACITY}"
        )
    if physical_worker_capacity > MAXIMUM_ATTENDEE_WORKER_CAPACITY:
        raise ValueError(
            f"physical_worker_capacity cannot exceed {MAXIMUM_ATTENDEE_WORKER_CAPACITY}"
        )
    if physical_worker_capacity > context_capacity:
        raise ValueError("physical_worker_capacity cannot exceed context_capacity")
    capacity = max(
        ATTENDEE_FRESH_OPERATIONS_GLOBAL,
        3 * context_capacity + physical_worker_capacity,
    )
    if capacity > MAXIMUM_ATTENDEE_FRESH_OPERATIONS_GLOBAL:
        raise ValueError("derived fresh-operation capacity exceeds its hard limit")
    return capacity


def _derived_retention_capacities(
    composition: AttendeeSecurityComposition,
) -> dict[str, int]:
    """Return finite store ceilings aligned with the configured work envelope."""

    contexts = composition.context_capacity
    sessions = composition.active_session_capacity
    logical_jobs = composition.logical_job_capacity
    launch_grants = max(16, contexts + sessions)
    selection_idempotency = max(64, launch_grants)
    return {
        "bootstrap_idempotency_records": max(16, contexts + 1),
        "exchange_idempotency_records": max(64, launch_grants),
        "launch_grants": launch_grants,
        "lifecycle_idempotency_records": max(
            128,
            2 * logical_jobs + 4 * sessions,
        ),
        "revoke_idempotency_records": max(16, contexts),
        "selection_idempotency_records": selection_idempotency,
        "selection_token_history": max(128, contexts + 2),
        "tombstones": max(8, 2 * sessions),
    }


class _RegistryTokens:
    def __init__(self, service_epoch: str) -> None:
        self._service_epoch = service_epoch

    def __call__(self, purpose: str) -> str:
        if purpose == "epoch":
            return self._service_epoch
        if purpose == "launch-grant":
            return f"launch-grant:{secrets.token_urlsafe(32)}"
        raise ValueError("unsupported attendee security token purpose")


class _CoreTokens:
    def __init__(self, service_epoch: str) -> None:
        self._service_epoch = service_epoch

    def __call__(self, purpose: str) -> str:
        if purpose == "service_epoch":
            return self._service_epoch
        prefixes = {
            "product_session_id": "session:",
            "product_reference": "session-ref:",
            "session_authority": "session-cap:",
        }
        return f"{prefixes.get(purpose, 'product-opaque:')}{secrets.token_urlsafe(32)}"


class _StartupAnchoredClock:
    """Wall-shaped milliseconds advanced only by a monotonic source."""

    def __init__(self) -> None:
        self._wall_anchor_ms = time.time_ns() // 1_000_000
        self._monotonic_anchor_ns = time.monotonic_ns()

    def now_ms(self) -> int:
        elapsed_ms = (time.monotonic_ns() - self._monotonic_anchor_ns) // 1_000_000
        return self._wall_anchor_ms + elapsed_ms


class AttendeeSecurityHarness:
    """Own only bounded attendee services and deterministic workers."""

    def __init__(
        self,
        *,
        bff_authority: str,
        scenario: str = SCENARIOS[0],
        clock: Any | None = None,
        request_admission: AttendeeRequestAdmissionLedger | None = None,
        composition: AttendeeSecurityComposition | None = None,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown attendee security preview scenario")
        if not isinstance(bff_authority, str) or not _BFF_AUTHORITY_PATTERN.fullmatch(
            bff_authority
        ):
            raise ValueError("private attendee BFF authority is malformed")
        self.scenario = scenario
        self.clock = clock if clock is not None else _StartupAnchoredClock()
        self.bff_authority = bff_authority
        self.composition = (
            default_attendee_security_composition()
            if composition is None
            else composition
        )
        if not isinstance(self.composition, AttendeeSecurityComposition):
            raise TypeError("composition must be AttendeeSecurityComposition")
        self._workers = ThreadPoolExecutor(
            max_workers=self.composition.physical_worker_capacity,
            thread_name_prefix="dreamland-attendee-security-preview",
        )
        self._future_lock = threading.Lock()
        self._futures: set[Future[object]] = set()
        self._closed = False
        try:
            self.service = self._build_service(request_admission=request_admission)
            self.attendee_service = self.service
        except Exception:
            self._workers.shutdown(wait=True, cancel_futures=True)
            raise

    @property
    def resolved_availability_window(self) -> AttendeeSecurityAvailabilityWindow:
        """Return the non-secret exact window resolved during construction."""

        return self._resolved_availability_window

    @property
    def availability_window(self) -> AttendeeSecurityAvailabilityWindow:
        """Alias the resolved availability window for concise local inspection."""

        return self.resolved_availability_window

    def _build_service(
        self,
        *,
        request_admission: AttendeeRequestAdmissionLedger | None,
    ) -> PublicAttendeeSessionService:
        now_ms = self.clock.now_ms()
        configured_window = self.composition.availability_window
        if configured_window is None:
            availability_window = AttendeeSecurityAvailabilityWindow(
                admission_opens_at_ms=now_ms,
                admission_closes_at_ms=(now_ms + 12 * 60 * MILLISECONDS_PER_MINUTE),
                recovery_closes_at_ms=(now_ms + 24 * 60 * MILLISECONDS_PER_MINUTE),
            )
        else:
            if now_ms >= configured_window.recovery_closes_at_ms:
                raise ValueError("attendee recovery window has already closed")
            if now_ms >= configured_window.admission_closes_at_ms:
                raise ValueError("attendee admission window has already closed")
            availability_window = configured_window
        self._resolved_availability_window = availability_window
        service_epoch = f"epoch:{secrets.token_urlsafe(32)}"
        authority_expires_at_ms = availability_window.recovery_closes_at_ms
        admission_opens_at_ms = availability_window.admission_opens_at_ms
        admission_closes_at_ms = availability_window.admission_closes_at_ms
        document = json.loads(self.composition._revision_canonical_json)
        if not isinstance(document, dict):
            raise RuntimeError("validated attendee revision is not an object")
        revision = _revision_seed(document)
        retention = _derived_retention_capacities(self.composition)
        actors = tuple(
            LaunchSelectionActorSeed(
                authority=f"actor:{secrets.token_urlsafe(32)}",
                actor_id=f"attendee-security-{index:04d}",
                role=ProductRole.ATTENDEE,
                expires_at_ms=authority_expires_at_ms,
            )
            for index in range(1, self.composition.context_capacity + 1)
        )
        launch_limits = {
            "absolute_ttl_ms": 20 * MILLISECONDS_PER_MINUTE,
            "idle_ttl_ms": 10 * MILLISECONDS_PER_MINUTE,
            "max_command_bytes": 4_096,
            "command_reserved_tokens": (self.composition.reserved_tokens_per_command),
            "command_reserved_spend_units": (
                self.composition.reserved_spend_units_per_command
            ),
            "command_max_attempts": 1,
            "command_deadline_ms": self.composition.command_deadline_ms,
        }
        if self.composition.max_commands_per_session is not None:
            launch_limits["max_commands"] = self.composition.max_commands_per_session
        registry = LaunchSelectionRegistry(
            revisions=(revision,),
            offerings=(
                LaunchOfferingSeed(
                    offering_id="offering:attendee-security-preview",
                    launch_selection_id=ATTENDEE_SELECTION_ID,
                    revision_id=revision.revision_id,
                    origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                    title="Private attendee security preview",
                    source_label="Authored",
                    revision_label="Pinned security-boundary revision",
                    role=ProductRole.ATTENDEE,
                    allowed_operations=frozenset(
                        {ProductOperation.CREATE, ProductOperation.SWITCH}
                    ),
                    limits=launch_limits,
                    policy_id=self.composition.policy_id,
                    notice_version=self.composition.notice_version,
                    expires_at_ms=admission_closes_at_ms - 1_000,
                    launch_grant_ttl_ms=20 * MILLISECONDS_PER_MINUTE,
                ),
            ),
            actors=actors,
            policy=LaunchSelectionPolicy(
                max_revisions=1,
                max_offerings=1,
                max_actor_authorities=self.composition.context_capacity,
                max_active_launch_grants=retention["launch_grants"],
                max_idempotency_records=(retention["selection_idempotency_records"]),
                max_token_history=retention["selection_token_history"],
                max_revision_bytes=4 * 1024 * 1024,
                max_launch_grant_ttl_ms=20 * MILLISECONDS_PER_MINUTE,
                idempotency_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
            ),
            allowed_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            clock_ms=self.clock.now_ms,
            monotonic_ms=self.clock.now_ms,
            token_factory=_RegistryTokens(service_epoch),
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
            executor=self.composition.executor,
            submit_task=self.submit_task,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=20 * MILLISECONDS_PER_MINUTE,
                idle_ttl_ms=10 * MILLISECONDS_PER_MINUTE,
                tombstone_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                max_active_sessions=self.composition.active_session_capacity,
                max_tombstones=retention["tombstones"],
                max_idempotency_records=(retention["lifecycle_idempotency_records"]),
                max_launch_grants=retention["launch_grants"],
                max_actor_authorities=self.composition.context_capacity,
                max_revisions=1,
            ),
            core_policy=ProductCorePolicy(
                max_commands_per_session=(self.composition.max_commands_per_session),
                command_deadline_ms=self.composition.command_deadline_ms,
                cancellation_deadline_ms=(self.composition.cancellation_deadline_ms),
                max_output_bytes_per_command=(
                    self.composition.max_output_bytes_per_command
                ),
                max_output_chunks_per_command=(
                    self.composition.max_output_chunks_per_command
                ),
                max_runtime_document_bytes=(
                    self.composition.max_runtime_document_bytes
                ),
                reserved_tokens_per_command=(
                    self.composition.reserved_tokens_per_command
                ),
                reserved_spend_units_per_command=(
                    self.composition.reserved_spend_units_per_command
                ),
                max_attempts_per_command=1,
            ),
            admission=ProductAdmissionLedger(
                ProductAdmissionLimits(
                    max_active_sessions=(self.composition.active_session_capacity),
                    max_logical_jobs=self.composition.logical_job_capacity,
                    max_physical_workers=(self.composition.physical_worker_capacity),
                    token_budget=self.composition.token_budget,
                    spend_budget_units=self.composition.spend_budget_units,
                )
            ),
            clock_ms=self.clock.now_ms,
            monotonic_ms=self.clock.now_ms,
            token_factory=_CoreTokens(service_epoch),
            validate_state_effect=self.composition.validate_runtime_document,
        )
        logical_admission = request_admission or AttendeeRequestAdmissionLedger(
            AttendeeRequestAdmissionPolicy(
                window_ms=1_000,
                max_fresh_operations_per_context=(
                    self.composition.max_fresh_operations_per_context
                ),
                max_fresh_operations_global=(
                    self.composition.max_fresh_operations_global
                ),
                max_tracked_contexts=self.composition.context_capacity,
            )
        )
        self.request_admission = logical_admission
        return PublicAttendeeSessionService(
            registry=registry,
            core=core,
            context_actors=actors,
            bff_authorities=(
                AttendeeBffAuthoritySeed(
                    authority=self.bff_authority,
                    authority_id="bff:attendee-security-preview",
                    expires_at_ms=authority_expires_at_ms,
                ),
            ),
            policy=AttendeeContextPolicy(
                max_contexts=self.composition.context_capacity,
                context_ttl_ms=60 * MILLISECONDS_PER_MINUTE,
                max_bootstrap_idempotency_records=(
                    retention["bootstrap_idempotency_records"]
                ),
                max_exchange_idempotency_records=(
                    retention["exchange_idempotency_records"]
                ),
                max_revoke_idempotency_records=(
                    retention["revoke_idempotency_records"]
                ),
                admission_opens_at_ms=admission_opens_at_ms,
                admission_closes_at_ms=admission_closes_at_ms,
                bootstrap_closes_at_admission=(
                    self.composition.bootstrap_closes_at_admission
                ),
            ),
            request_admission=logical_admission,
        )

    def submit_task(self, task: Any) -> None:
        if not callable(task):
            raise TypeError("attendee security preview task must be callable")
        with self._future_lock:
            if self._closed:
                raise RuntimeError("attendee security preview is closed")
            future = self._workers.submit(task)
            self._futures.add(future)
        future.add_done_callback(self._forget_future)

    def _forget_future(self, future: Future[object]) -> None:
        with self._future_lock:
            self._futures.discard(future)

    def should_drop_command_accept_response(self) -> bool:
        return False

    def should_withhold_terminal_event_response(
        self, _page: Mapping[str, object]
    ) -> bool:
        return False

    def close(self) -> None:
        with self._future_lock:
            if self._closed:
                return
            self._closed = True
        self.service.shutdown()
        self._workers.shutdown(wait=True, cancel_futures=True)


class AttendeeSecurityPreviewServer(_CORE.ProductCorePreviewServer):
    """Hardened private HTTP server with attendee-only composition."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        bff_authority: str,
        scenario: str,
        contract_root: Path,
        harness_factory: Callable[..., AttendeeSecurityHarness] = (
            AttendeeSecurityHarness
        ),
    ) -> None:
        self.preview_bearer = bearer
        self.contract_root = contract_root
        self.preview = harness_factory(
            bff_authority=bff_authority,
            scenario=scenario,
        )
        self._request_sequence = 0
        self._request_lock = threading.Lock()
        self.connection_capacity = self.preview.composition.connection_capacity
        if self.connection_capacity != ATTENDEE_CONNECTION_CAPACITY:
            self.request_queue_size = self.connection_capacity
        self._connection_slots = threading.BoundedSemaphore(self.connection_capacity)
        self._connection_count_lock = threading.Lock()
        self._active_connections = 0
        self._maximum_observed_connections = 0
        self._synchronous_request = False
        try:
            ThreadingHTTPServer.__init__(self, address, AttendeeSecurityPreviewHandler)
        except Exception:
            self.preview.close()
            raise

    def next_request_id(self) -> str:
        with self._request_lock:
            self._request_sequence += 1
            sequence = self._request_sequence
        return f"request:attendee-security-preview-{sequence:08d}"

    def server_close(self) -> None:
        try:
            HTTPServer.server_close(self)
        finally:
            self.preview.close()


class AttendeeSecurityPreviewHandler(_ATTENDEE.AttendeePreviewHandler):
    """Strict attendee/core route allowlist over the proven transport."""

    server: AttendeeSecurityPreviewServer
    server_version = "DREAMLANDAttendeeSecurityPreview/0.8"

    def _additional_get_operations(self) -> frozenset[str]:
        return frozenset({"attendee-launch-options"})

    def _additional_post_operations(self) -> frozenset[str]:
        return frozenset({"attendee-launch-selection"})

    def _route(self) -> tuple[str, str | None, str | None, str]:
        parsed = urlsplit(self.path)
        if parsed.fragment:
            return "not-found", None, None, parsed.query
        if parsed.path == ATTENDEE_CONTEXTS_PATH:
            return "create", None, None, parsed.query
        if parsed.path.startswith(ATTENDEE_CONTEXT_PREFIX):
            encoded = parsed.path.removeprefix(ATTENDEE_CONTEXT_PREFIX)
            if not encoded or "/" in encoded or len(encoded) > 420:
                return "not-found", None, None, parsed.query
            gateway_context_id = unquote(encoded)
            if _GATEWAY_CONTEXT_PATTERN.fullmatch(gateway_context_id) is None:
                return "not-found", None, None, parsed.query
            return "revoke-attendee-context", gateway_context_id, None, parsed.query
        if parsed.path == ATTENDEE_LAUNCH_OPTIONS_PATH:
            return "attendee-launch-options", None, None, parsed.query
        if parsed.path == ATTENDEE_LAUNCH_SELECTIONS_PATH:
            return "attendee-launch-selection", None, None, parsed.query
        if parsed.path in {
            _CORE.CAPABILITIES_PATH,
            _CORE.SESSIONS_PATH,
            _CORE.CURRENT_PATH,
        } or (parsed.path.startswith(_CORE.SESSION_PATH_PREFIX)):
            return _CORE.ProductCorePreviewHandler._route(self)
        return "not-found", None, None, parsed.query

    def _dispatch_get(
        self,
        operation: str,
        reference: str | None,
        command_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object] | None:
        if operation == "attendee-launch-options":
            return _ATTENDEE.AttendeePreviewHandler._dispatch_get(
                self,
                operation,
                reference,
                command_id,
                raw_query,
                request_id,
            )
        return _CORE.ProductCorePreviewHandler._dispatch_get(
            self,
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
        if (
            urlsplit(self.path).path == ATTENDEE_CONTEXTS_PATH
            or operation == "attendee-launch-selection"
        ):
            return _ATTENDEE.AttendeePreviewHandler._dispatch_post(
                self,
                operation,
                reference,
                command_id,
                raw_query,
                request_id,
            )
        return _CORE.ProductCorePreviewHandler._dispatch_post(
            self,
            operation,
            reference,
            command_id,
            raw_query,
            request_id,
        )

    def _handle(self, method: str) -> None:
        if method != "DELETE":
            super()._handle(method)
            return
        request_id = self.server.next_request_id()
        try:
            self._require_service_bearer(request_id)
            operation, gateway_context_id, _command_id, raw_query = self._route()
            if operation != "revoke-attendee-context" or gateway_context_id is None:
                raise _not_found()
            self._gate_expected_epoch(mutation=True)
            value = self._dispatch_revoke(
                gateway_context_id=gateway_context_id,
                raw_query=raw_query,
                request_id=request_id,
            )
            self._send_json(HTTPStatus.OK, value)
        except _LIFECYCLE.PreviewTransportError as error:
            self._send_problem(error.status, error.code, request_id)
        except ProductLifecycleError as error:
            self._send_json(
                _CORE._domain_status(error),
                _CORE._domain_problem(error, request_id),
                content_type="application/problem+json",
            )
        except Exception:
            self._send_problem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                request_id,
            )

    def _dispatch_revoke(
        self,
        *,
        gateway_context_id: str,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object]:
        if _CORE._query_pairs(raw_query):
            raise _not_found()
        self._require_empty_body()
        expected_epoch, actor_context = self._read_context()
        idempotency_key = self._required_header(
            _CORE.IDEMPOTENCY_HEADER,
            _CORE.IDEMPOTENCY_KEY_PATTERN,
        )
        try:
            domain = self.server.preview.service.revoke_context(
                bff_authority=self.server.preview.bff_authority,
                actor_authority=actor_context,
                expected_epoch=expected_epoch,
                gateway_context_id=gateway_context_id,
                idempotency_key=idempotency_key,
            )
        except LaunchSelectionError as error:
            raise _ATTENDEE._selection_lifecycle_error(error) from None
        return _ATTENDEE._success(domain, request_id)

    def _require_empty_body(self) -> None:
        if self.headers.get("Transfer-Encoding") is not None:
            self.close_connection = True
            raise _invalid_request("Context revocation does not accept a body.")
        values = self.headers.get_all("Content-Length", [])
        if len(values) > 1:
            self.close_connection = True
            raise _invalid_request("Context revocation does not accept a body.")
        if values:
            try:
                length = int(values[0])
            except ValueError as error:
                self.close_connection = True
                raise _invalid_request(
                    "Context revocation does not accept a body."
                ) from error
            if length != 0:
                self.close_connection = True
                raise _invalid_request("Context revocation does not accept a body.")


def _not_found() -> Exception:
    return _LIFECYCLE.PreviewTransportError(
        HTTPStatus.NOT_FOUND,
        "not_found",
        "Unavailable route.",
    )


def _invalid_request(message: str) -> Exception:
    return _LIFECYCLE.PreviewTransportError(
        HTTPStatus.BAD_REQUEST,
        "invalid_request",
        message,
    )


def _revision_seed(document: Mapping[str, object]) -> LaunchRevisionSeed:
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
    digest = hashlib.sha256(canonical).hexdigest()
    return LaunchRevisionSeed(
        revision_id=f"world-revision:sha256:{digest}",
        document=document,
    )


def _authored_document() -> dict[str, object]:
    return {
        "kind": "dreamland.authored_world_revision",
        "schema_revision": "1",
        "profile": {
            "level": "world",
            "state": {
                "world_state": {
                    "name": "attendee-security-preview",
                    "entry_host": "public-console",
                    "active_host": "public-console",
                    "hosts_by_name": {
                        "public-console": {
                            "hostname": "public-console",
                            "login_banner": "Private attendee console ready.\n",
                            "purpose": {"theme": "Private attendee security preview"},
                        }
                    },
                }
            },
        },
    }


def default_attendee_security_composition() -> AttendeeSecurityComposition:
    """Return the exact provider-free Phase 9A composition."""

    return AttendeeSecurityComposition(
        executor=_CORE.DeterministicPreviewExecutor(),
        revision_document=_authored_document(),
        physical_worker_capacity=ATTENDEE_WORKER_CAPACITY,
        active_session_capacity=ATTENDEE_ACTIVE_SESSION_CAPACITY,
        logical_job_capacity=ATTENDEE_LOGICAL_JOB_CAPACITY,
        max_commands_per_session=8,
        command_deadline_ms=10_000,
        cancellation_deadline_ms=2_000,
        max_output_bytes_per_command=16 * 1_024,
        max_output_chunks_per_command=16,
        max_runtime_document_bytes=4 * 1_024 * 1_024,
        reserved_tokens_per_command=32,
        reserved_spend_units_per_command=32,
        token_budget=ATTENDEE_TOKEN_BUDGET,
        spend_budget_units=ATTENDEE_SPEND_BUDGET,
    )


def _configure_transport() -> None:
    _ATTENDEE._configure_transport()
    for module in (
        _ATTENDEE,
        _ATTENDEE._SELECTION,
        _ATTENDEE._COMBINED,
        _ATTENDEE._STAGE,
        _CORE,
    ):
        module.CONTRACT_NAME = CONTRACT_NAME
        module.CONTRACT_REVISION = CONTRACT_REVISION
        module.CONTRACT_COMPATIBILITY = CONTRACT_COMPATIBILITY
    _ATTENDEE._STAGE.PREVIEW_EXECUTOR = PREVIEW_EXECUTOR
    _CORE.PREVIEW_EXECUTOR = PREVIEW_EXECUTOR
    _CORE.PREVIEW_PROVIDER_CALLS = False
    _CORE.PREVIEW_TEST_ONLY = True
    additions = {
        "logical_rate_limited": (
            429,
            "Attendee request admission is temporarily limited.",
            "after_delay",
            1_000,
        ),
    }
    for code, detail in additions.items():
        existing = _CORE.SAFE_ERROR_DETAILS.get(code)
        if existing is not None and existing != detail:
            raise RuntimeError("attendee security error configuration conflicts")
        _CORE.SAFE_ERROR_DETAILS[code] = detail


def validate_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    tool_path = Path(__file__).with_name("product_attendee_security_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_preview_contract_validator",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("attendee security contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("attendee security contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != CONTRACT_REVISION
        or result.get("implementation_status") != "integration_preview_only"
        or result.get("capabilities_enabled") != 0
    ):
        raise ValueError("attendee security contract is incompatible")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if any(
        not isinstance(value, Mapping)
        or value.get("enabled") is not False
        or value.get("enabled_revision") is not None
        for value in manifest.get("capabilities", {}).values()
    ):
        raise ValueError("attendee security capabilities must remain disabled")
    openapi = json.loads((root / "openapi.json").read_text(encoding="utf-8"))
    revoke_path = openapi.get("paths", {}).get(
        ATTENDEE_CONTEXT_PREFIX + "{gateway_context_id}"
    )
    if not isinstance(revoke_path, Mapping) or set(revoke_path) != {"delete"}:
        raise ValueError("attendee context revocation route is incompatible")
    return root


def build_preview_server(
    *,
    host: str,
    port: int,
    bearer: str,
    bff_authority: str,
    scenario: str,
    contract_root: Path,
    contract_validator: Callable[[Path], Path] = validate_contract_root,
    harness_factory: Callable[..., AttendeeSecurityHarness] = AttendeeSecurityHarness,
) -> AttendeeSecurityPreviewServer:
    if not _LIFECYCLE._loopback_host(host):
        raise ValueError("attendee security preview supports only loopback hosts")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
        raise ValueError("attendee security preview port must be between 0 and 65535")
    _LIFECYCLE._validate_preview_bearer(bearer)
    if not isinstance(bff_authority, str) or not _BFF_AUTHORITY_PATTERN.fullmatch(
        bff_authority
    ):
        raise ValueError("private attendee BFF authority is malformed")
    if scenario not in SCENARIOS:
        raise ValueError("unknown attendee security preview scenario")
    root = contract_validator(contract_root)
    _configure_transport()
    return AttendeeSecurityPreviewServer(
        (host, port),
        bearer=bearer,
        bff_authority=bff_authority,
        scenario=scenario,
        contract_root=root,
        harness_factory=harness_factory,
    )


def initialize_private_files(*, token_file: Path, bootstrap_file: Path) -> None:
    """Create distinct owner-only bearer and BFF bootstrap authority files."""
    if token_file.expanduser().absolute() == bootstrap_file.expanduser().absolute():
        raise ValueError("attendee token and bootstrap paths must differ")
    _exclusive_owner_file(
        token_file,
        f"attendee-security-bearer:{secrets.token_urlsafe(32)}\n",
    )
    _exclusive_owner_file(
        bootstrap_file,
        json.dumps(
            {"bff_authority": f"bff-authority:{secrets.token_urlsafe(32)}"},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def load_bootstrap(path: Path) -> str:
    try:
        value = json.loads(
            _read_owner_file(path, maximum_bytes=2_048),
            object_pairs_hook=_LIFECYCLE._reject_duplicate_keys,
            parse_constant=_LIFECYCLE._reject_json_constant,
        )
    except (ValueError, json.JSONDecodeError) as error:
        raise ValueError("attendee security bootstrap is not valid JSON") from error
    if not isinstance(value, dict) or set(value) != {"bff_authority"}:
        raise ValueError("attendee security bootstrap has invalid fields")
    authority = value["bff_authority"]
    if not isinstance(authority, str) or not _BFF_AUTHORITY_PATTERN.fullmatch(
        authority
    ):
        raise ValueError("attendee security BFF authority is malformed")
    return authority


def _exclusive_owner_file(path: Path, text: str) -> None:
    resolved = path.expanduser().absolute()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("attendee security files require no-follow support")
    flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _read_owner_file(path: Path, *, maximum_bytes: int) -> str:
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("attendee security files require no-follow support")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError as error:
        raise ValueError("attendee security bootstrap is unavailable") from error
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("attendee security bootstrap must be owner-only")
        data = stream.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        raise ValueError("attendee security bootstrap is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("attendee security bootstrap must be UTF-8") from error


def startup_announcement(server: AttendeeSecurityPreviewServer) -> dict[str, object]:
    address, port = server.server_address[:2]
    policy = server.preview.request_admission.policy
    composition = server.preview.composition
    registry_policy = server.preview.service._registry._policy
    lifecycle_policy = server.preview.service._core._policy
    context_policy = server.preview.service._policy
    connection_capacity = getattr(
        server,
        "connection_capacity",
        composition.connection_capacity,
    )
    return {
        "capabilities_enabled": 0,
        "contract": {
            "name": CONTRACT_NAME,
            "revision": CONTRACT_REVISION,
            "compatibility": CONTRACT_COMPATIBILITY,
        },
        "deployment": {
            "authority": "owner_only_private_bff",
            "browser_direct_supported": False,
            "exposure": "loopback_only",
            "production_capabilities": False,
        },
        "executor": PREVIEW_EXECUTOR,
        "host": address,
        "integration_only": True,
        "kind": PREVIEW_KIND,
        "normal_runtime_composition": False,
        "port": port,
        "provider_calls": False,
        "resources": {
            "active_sessions": composition.active_session_capacity,
            "context_capacity": composition.context_capacity,
            "hard_maximums": {
                "active_sessions": MAXIMUM_ATTENDEE_ACTIVE_SESSION_CAPACITY,
                "connections": MAXIMUM_ATTENDEE_CONNECTION_CAPACITY,
                "contexts": MAXIMUM_ATTENDEE_CONTEXT_CAPACITY,
                "physical_workers": MAXIMUM_ATTENDEE_WORKER_CAPACITY,
            },
            "logical_jobs": composition.logical_job_capacity,
            "physical_workers": composition.physical_worker_capacity,
            "retention": {
                "bootstrap_idempotency_records": (
                    context_policy.max_bootstrap_idempotency_records
                ),
                "exchange_idempotency_records": (
                    context_policy.max_exchange_idempotency_records
                ),
                "launch_grants": registry_policy.max_active_launch_grants,
                "lifecycle_idempotency_records": (
                    lifecycle_policy.max_idempotency_records
                ),
                "revoke_idempotency_records": (
                    context_policy.max_revoke_idempotency_records
                ),
                "selection_idempotency_records": (
                    registry_policy.max_idempotency_records
                ),
                "selection_token_history": registry_policy.max_token_history,
                "tombstones": lifecycle_policy.max_tombstones,
            },
            "spend_budget_units": composition.spend_budget_units,
            "token_budget": composition.token_budget,
        },
        "route_count": len(PRIVATE_ROUTE_ALLOWLIST),
        "scenario": server.preview.scenario,
        "security": {
            "connection_deadline_seconds": _LIFECYCLE.CONNECTION_DEADLINE_SECONDS,
            "connection_limit": connection_capacity,
            "logical_global_limit": policy.max_fresh_operations_global,
            "logical_per_context_limit": policy.max_fresh_operations_per_context,
            "logical_tracked_contexts": policy.max_tracked_contexts,
            "logical_window_ms": policy.window_ms,
            "max_request_body_bytes": _CORE.MAX_REQUEST_BODY_BYTES,
            "read_timeout_seconds": _LIFECYCLE.CONNECTION_READ_TIMEOUT_SECONDS,
        },
        "test_only": True,
    }


def serve_preview(
    *,
    host: str,
    port: int,
    bearer: str,
    bff_authority: str,
    scenario: str,
    contract_root: Path,
    once: bool,
) -> None:
    server = build_preview_server(
        host=host,
        port=port,
        bearer=bearer,
        bff_authority=bff_authority,
        scenario=scenario,
        contract_root=contract_root,
    )
    print(json.dumps(startup_announcement(server), sort_keys=True), flush=True)
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
        description="Serve the provider-free private attendee security boundary."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="create owner-only credentials")
    initialize.add_argument("--token-file", required=True, type=Path)
    initialize.add_argument("--bootstrap-file", required=True, type=Path)
    serve = commands.add_parser("serve", help="start the loopback listener")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--token-file", required=True, type=Path)
    serve.add_argument("--bootstrap-file", required=True, type=Path)
    serve.add_argument("--contract-root", required=True, type=Path)
    serve.add_argument("--scenario", choices=SCENARIOS, default=SCENARIOS[0])
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
            print(json.dumps({"created": True}, sort_keys=True))
            return 0
        bearer = _LIFECYCLE.load_preview_bearer_file(arguments.token_file)
        bff_authority = load_bootstrap(arguments.bootstrap_file)
        serve_preview(
            host=arguments.host,
            port=arguments.port,
            bearer=bearer,
            bff_authority=bff_authority,
            scenario=arguments.scenario,
            contract_root=arguments.contract_root,
            once=arguments.once,
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        print(f"Attendee security preview error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
