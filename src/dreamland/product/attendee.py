"""Inert public-attendee facade over the shared product-session core.

This module owns no browser cookie, public HTTP route, provider, worker, timer,
profile lookup, or filesystem access.  Trusted composition explicitly builds a
bounded pool of attendee actors in both the generic launch registry and the
shared core.  A private BFF bootstrap then leases each actor at most once for a
service epoch.  Never reusing a lease is intentional: old current-session,
idempotency, and tombstone coordinates can therefore never attach to a later
browser context.

The published attendee option and selection methods remain authored-only.  A
separate, explicitly enabled in-process seam can accept an already promoted
Forge offering from the generated-world access boundary without widening the
published Product contract.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import islice
from typing import Literal

from dreamland.product.admission import (
    ProductAdmissionLedger,
    ProductCapacityClass,
)
from dreamland.product.attendee_security import (
    AttendeeRequestAdmissionDenied,
    AttendeeRequestAdmissionLedger,
)
from dreamland.product.core import (
    ProductCorePolicy,
    ProductSessionCoreService,
    TaskSubmitter,
)
from dreamland.product.execution import ProductCommandExecutor
from dreamland.product.journal import JournalLimits
from dreamland.product.launch_selection import (
    LaunchGrantIssue,
    LaunchGrantProvisioningCapacityError,
    LaunchGrantProvisioningReceipt,
    LaunchSelectionActorSeed,
    LaunchSelectionError,
    LaunchSelectionOrigin,
    LaunchSelectionRegistry,
    ResolvedLaunchSelection,
)
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductOperation,
    ProductRole,
    _ActorAuthorityRecord,
    _build_launch_grant_record,
    _build_revisions,
    _credential_sha256,
    _error,
    _LaunchGrantRecord,
    _require_seed_token,
    _require_timestamp,
    _SessionRecord,
)

_GATEWAY_CONTEXT_PATTERN = re.compile(r"^gateway-context:[A-Za-z0-9_-]{24,128}$")
_BFF_AUTHORITY_PATTERN = re.compile(r"^bff-authority:[A-Za-z0-9_-]{24,256}$")
_LAUNCH_SELECTION_GRANT_PATTERN = re.compile(
    r"^launch-grant:(?P<serial>[0-9a-f]{16})_[0-9a-f]{64}$"
)
_LAUNCH_SELECTION_ID_PATTERN = re.compile(r"^launch-selection:[A-Za-z0-9_-]{24,128}$")
_OFFERING_ID_PATTERN = re.compile(r"^offering:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_OFFERING_REVISION_PATTERN = re.compile(
    r"^offering-revision:[A-Za-z0-9][A-Za-z0-9_.-]{15,127}$"
)
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,128}$")
_SAFE_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_TIMESTAMP_MS = 253_402_300_799_999
_LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")


@dataclass(frozen=True, slots=True)
class AttendeeBffAuthoritySeed:
    """One private registrar authority supplied by trusted composition."""

    authority: str = field(repr=False)
    authority_id: str = ""
    expires_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class AttendeeContextPolicy:
    """Hard bounds for private gateway-context registration and admission."""

    max_contexts: int = 256
    context_ttl_ms: int = 60 * 60 * 1000
    max_bootstrap_idempotency_records: int = 512
    max_exchange_idempotency_records: int = 1_024
    admission_opens_at_ms: int = 0
    admission_closes_at_ms: int = _MAX_TIMESTAMP_MS
    max_revoke_idempotency_records: int = _MAX_SAFE_INTEGER
    bootstrap_closes_at_admission: bool = False

    def __post_init__(self) -> None:
        for name in (
            "max_contexts",
            "context_ttl_ms",
            "max_bootstrap_idempotency_records",
            "max_exchange_idempotency_records",
            "max_revoke_idempotency_records",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= _MAX_SAFE_INTEGER
            ):
                raise ValueError(f"{name} must be a positive safe integer")
        for name in ("admission_opens_at_ms", "admission_closes_at_ms"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_TIMESTAMP_MS
            ):
                raise ValueError(f"{name} must be a valid timestamp")
        if self.admission_closes_at_ms <= self.admission_opens_at_ms:
            raise ValueError("attendee admission must close after it opens")
        if not isinstance(self.bootstrap_closes_at_admission, bool):
            raise ValueError("bootstrap_closes_at_admission must be a boolean")
        if self.max_bootstrap_idempotency_records <= self.max_contexts:
            raise ValueError(
                "bootstrap idempotency capacity must cover every attendee context "
                "and one terminal capacity outcome"
            )
        if self.max_revoke_idempotency_records < self.max_contexts:
            raise ValueError(
                "revoke idempotency capacity must cover every attendee context"
            )


class AttendeeSelectionCapacityError(RuntimeError):
    """Trusted signal that the attendee core cannot provision another grant."""


@dataclass(frozen=True, slots=True)
class AttendeeCatalogEligibilityProof:
    """Signed in-process proof of one current catalog binding."""

    service_epoch: str
    offering_id: str
    offering_revision: str
    access_record_id: str
    origin: LaunchSelectionOrigin
    revision_id: str
    immutable_digest: str
    expires_at_ms: int
    verification_tag: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _BffAuthorityRecord:
    authority_id: str
    expires_at_ms: int
    expires_deadline_ms: int


@dataclass(frozen=True, slots=True)
class _ContextSlot:
    actor_authority: str = field(repr=False)
    actor_authority_sha256: str = field(repr=False)
    actor_id: str = ""
    actor_expires_at_ms: int = 0
    actor_expires_deadline_ms: int = 0


@dataclass(frozen=True, slots=True)
class _ContextLease:
    context_sha256: str = field(repr=False)
    slot_index: int
    expires_at_ms: int
    expires_deadline_ms: int


@dataclass(frozen=True, slots=True)
class _RecordedSelectionError:
    code: str
    message: str
    status: int
    retry: Literal["none", "same_request", "after_delay"]
    retry_after_ms: int | None

    @classmethod
    def capture(cls, error: LaunchSelectionError) -> _RecordedSelectionError:
        return cls(
            code=error.code,
            message=error.message,
            status=error.status,
            retry=error.retry,
            retry_after_ms=error.retry_after_ms,
        )

    def restore(self) -> LaunchSelectionError:
        return LaunchSelectionError(
            code=self.code,
            message=self.message,
            status=self.status,
            retry=self.retry,
            retry_after_ms=self.retry_after_ms,
        )


@dataclass(slots=True)
class _PrivateIdempotencyRecord:
    body_sha256: str = field(repr=False)
    retain_until_deadline_ms: int
    result: dict[str, object] | None = field(default=None, repr=False)
    error: _RecordedSelectionError | None = field(default=None, repr=False)


@dataclass(slots=True)
class _ProductOperationGate:
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    users: int = 0


class AttendeeSessionCoreService(ProductSessionCoreService):
    """Attendee-only shared core with one narrow dynamic-grant seam.

    Immutable authored revisions and attendee actors remain startup-seeded.
    Only grants issued by the adjacent :class:`LaunchSelectionRegistry` are
    installed dynamically.  Trusted composition must expose the enclosing
    :class:`PublicAttendeeSessionService`, never this object, to a transport.
    """

    def __init__(
        self,
        *,
        revisions: Iterable[ImmutableRevisionSeed],
        actors: Iterable[ActorAuthoritySeed],
        executor: ProductCommandExecutor,
        submit_task: TaskSubmitter,
        lifecycle_policy: LifecyclePolicy | None = None,
        core_policy: ProductCorePolicy | None = None,
        journal_limits: JournalLimits | None = None,
        admission: ProductAdmissionLedger | None = None,
        clock_ms: Callable[[], int] | None = None,
        monotonic_ms: Callable[[], int] | None = None,
        token_factory: Callable[[str], str] | None = None,
        validate_replacement: Callable[[Mapping[str, object]], None] | None = None,
        validate_state_effect: Callable[[Mapping[str, object]], None] | None = None,
        generated_world_access: bool = False,
    ) -> None:
        if not isinstance(generated_world_access, bool):
            raise ValueError("generated_world_access must be boolean")
        revision_values = tuple(revisions)
        actor_values = tuple(actors)
        if not revision_values or any(
            not isinstance(seed.document, Mapping)
            or seed.document.get("kind") != "dreamland.authored_world_revision"
            for seed in revision_values
        ):
            raise ValueError("attendee core accepts only authored immutable revisions")
        if not actor_values or any(
            actor.role is not ProductRole.ATTENDEE for actor in actor_values
        ):
            raise ValueError("attendee core actors must all use attendee authority")
        self._attendee_dynamic_grants: set[str] = set()
        self._attendee_launch_by_selection: dict[str, str] = {}
        self._attendee_launch_serial_high_water = 0
        self._generated_world_access = generated_world_access
        super().__init__(
            revisions=revision_values,
            actors=actor_values,
            launch_grants=(),
            executor=executor,
            submit_task=submit_task,
            lifecycle_policy=lifecycle_policy,
            core_policy=core_policy,
            journal_limits=journal_limits,
            admission=admission,
            clock_ms=clock_ms,
            monotonic_ms=monotonic_ms,
            token_factory=token_factory,
            validate_replacement=validate_replacement,
            validate_state_effect=validate_state_effect,
        )
        self._attendee_revision_origins = {
            revision_id: LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
            for revision_id in self._revisions
        }

    def provision_launch_selection(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        issue: LaunchGrantIssue,
        resolved: ResolvedLaunchSelection,
    ) -> LaunchGrantProvisioningReceipt:
        """Atomically install one exact authored attendee grant."""
        return self._provision_launch_selection(
            actor_authority=actor_authority,
            expected_epoch=expected_epoch,
            issue=issue,
            resolved=resolved,
            expected_origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
            allow_dynamic_revision=False,
        )

    def provision_generated_launch_selection(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        issue: LaunchGrantIssue,
        resolved: ResolvedLaunchSelection,
    ) -> LaunchGrantProvisioningReceipt:
        """Atomically install one exact promoted Forge revision and grant."""
        if not self._generated_world_access:
            raise ValueError("generated-world attendee access is disabled")
        return self._provision_launch_selection(
            actor_authority=actor_authority,
            expected_epoch=expected_epoch,
            issue=issue,
            resolved=resolved,
            expected_origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
            allow_dynamic_revision=True,
        )

    def _provision_launch_selection(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        issue: LaunchGrantIssue,
        resolved: ResolvedLaunchSelection,
        expected_origin: LaunchSelectionOrigin,
        allow_dynamic_revision: bool,
    ) -> LaunchGrantProvisioningReceipt:
        if not isinstance(issue, LaunchGrantIssue):
            raise TypeError("issue must be LaunchGrantIssue")
        if not isinstance(resolved, ResolvedLaunchSelection):
            raise TypeError("resolved must be ResolvedLaunchSelection")
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            _now_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms)
            self._expire_and_prune_locked(deadline_ms)
            if (
                actor.role is not ProductRole.ATTENDEE
                or issue.role is not ProductRole.ATTENDEE
                or resolved.role is not ProductRole.ATTENDEE
                or actor.key != (resolved.actor_id, resolved.role)
                or issue.actor_id != resolved.actor_id
                or issue.service_epoch != self._service_epoch
                or resolved.service_epoch != self._service_epoch
                or issue.origin is not expected_origin
                or resolved.origin is not expected_origin
                or issue.offering_id != resolved.offering_id
                or issue.allowed_operations != resolved.allowed_operations
                or issue.limits != resolved.limits
                or issue.policy_id != resolved.policy_id
                or issue.notice_version != resolved.notice_version
                or issue.expires_at_ms != resolved.expires_at_ms
            ):
                raise ValueError("attendee launch selection is inconsistent")
            grant_match = _LAUNCH_SELECTION_GRANT_PATTERN.fullmatch(issue.launch_grant)
            if grant_match is None:
                raise ValueError("attendee launch grant format is invalid")
            grant_serial = int(grant_match.group("serial"), 16)
            selected_record_id = _selected_record_id(issue.launch_grant)
            resolved_document = resolved.revision.allocate()
            expected_kind = {
                LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT: (
                    "dreamland.authored_world_revision"
                ),
                LaunchSelectionOrigin.WORLD_FORGE_SELECTION: (
                    "dreamland.world_forge_revision"
                ),
            }[expected_origin]
            if resolved_document.get("kind") != expected_kind:
                raise ValueError("attendee launch origin is inconsistent")
            revision = self._revisions.get(resolved.revision.revision_id)
            expected_canonical = resolved.revision.canonical_json.removesuffix(b"\n")
            revision_candidate = None
            if revision is None:
                if not allow_dynamic_revision:
                    raise ValueError("attendee immutable revision is unavailable")
                revision_candidate = _build_revisions(
                    (
                        ImmutableRevisionSeed(
                            resolved.revision.revision_id,
                            resolved_document,
                        ),
                    )
                )[resolved.revision.revision_id]
                if revision_candidate.canonical_json != expected_canonical:
                    raise ValueError("attendee immutable revision identity changed")
                canonical = revision_candidate.canonical_json
            else:
                if revision.canonical_json != expected_canonical:
                    raise ValueError("attendee immutable revision is unavailable")
                canonical = revision.canonical_json
            existing_origin = self._attendee_revision_origins.get(
                resolved.revision.revision_id
            )
            if existing_origin is not None and existing_origin is not expected_origin:
                raise ValueError("attendee immutable revision origin changed")
            if len(canonical) > self._core_policy.max_runtime_document_bytes:
                raise ValueError("attendee immutable revision exceeds runtime limits")
            if ProductOperation.CREATE not in resolved.allowed_operations:
                raise ValueError("attendee launch grant must allow session.create")

            grant_seed = LaunchGrantSeed(
                token=issue.launch_grant,
                selected_record_id=selected_record_id,
                actor_id=resolved.actor_id,
                role=resolved.role,
                revision_id=resolved.revision.revision_id,
                expires_at_ms=resolved.expires_at_ms,
                allowed_operations=resolved.allowed_operations,
                limits=resolved.limits,
                policy_id=resolved.policy_id,
                notice_version=resolved.notice_version,
            )
            _require_seed_token(grant_seed.token)
            local_deadline_ms = min(
                deadline_ms + max(0, grant_seed.expires_at_ms - self._last_wall_ms),
                resolved.expires_deadline_ms,
            )
            candidate = _build_launch_grant_record(
                grant_seed,
                actor_keys={actor.key},
                revision_ids=frozenset(
                    {*self._revisions, resolved.revision.revision_id}
                ),
                launch_operation_families=self._launch_operation_families,
                service_epoch=self._service_epoch,
                expires_deadline_ms=local_deadline_ms,
            )
            if (
                deadline_ms >= candidate.expires_deadline_ms
                or self._last_wall_ms >= candidate.expires_at_ms
            ):
                raise ValueError("attendee launch grant is expired")
            # Validate Product's correlation evidence before the first state
            # mutation. Marking it committed remains the final mutation action,
            # so the registry can distinguish a prepared failure from Product
            # state that must also be published in the launch registry.
            receipt = LaunchGrantProvisioningReceipt(candidate.binding_sha256)
            issue._prepare_provisioning_receipt(receipt)

            launch_sha256 = _credential_sha256(issue.launch_grant)
            existing = self._launch_grants.get(launch_sha256)
            selected_launch = self._attendee_launch_by_selection.get(selected_record_id)
            if launch_sha256 in self._attendee_dynamic_grants:
                if not (
                    existing is not None
                    and existing.binding_sha256 == candidate.binding_sha256
                    and existing.expires_at_ms == candidate.expires_at_ms
                    and existing.expires_deadline_ms == candidate.expires_deadline_ms
                    and selected_launch == launch_sha256
                ):
                    raise ValueError(
                        "attendee launch selection conflicts with prior provision"
                    )
                issue._mark_provisioning_committed()
                return receipt
            if grant_serial <= self._attendee_launch_serial_high_water:
                raise ValueError("attendee launch grant serial is not fresh")
            if launch_sha256 in self._used_value_digests or existing is not None:
                raise ValueError("attendee launch grant token is not unique")
            if selected_launch is not None:
                raise ValueError("attendee selected record is already provisioned")
            if (
                revision_candidate is not None
                and len(self._revisions) >= self._policy.max_revisions
            ):
                raise AttendeeSelectionCapacityError(
                    "attendee immutable revision capacity exhausted"
                )
            if len(self._launch_grants) >= self._policy.max_launch_grants:
                raise AttendeeSelectionCapacityError(
                    "attendee launch grant capacity exhausted"
                )
            if revision_candidate is not None:
                self._revisions[revision_candidate.revision_id] = revision_candidate
                self._attendee_revision_origins[revision_candidate.revision_id] = (
                    expected_origin
                )
            self._launch_grants[launch_sha256] = candidate
            self._attendee_launch_by_selection[selected_record_id] = launch_sha256
            self._attendee_dynamic_grants.add(launch_sha256)
            self._used_value_digests.add(launch_sha256)
            self._attendee_launch_serial_high_water = grant_serial
            issue._mark_provisioning_committed()
            return receipt

    def _require_actor(
        self,
        actor_authority: str,
        deadline_ms: int,
    ) -> _ActorAuthorityRecord:
        actor = super()._require_actor(actor_authority, deadline_ms)
        if self._last_wall_ms >= actor.expires_at_ms:
            raise _error(
                "authority_denied",
                "The requested product resource is unavailable.",
                status=403,
            )
        return actor

    def _require_launch_grant(
        self,
        *,
        launch_sha256: str,
        actor: _ActorAuthorityRecord,
        deadline_ms: int,
        operation: ProductOperation = ProductOperation.CREATE,
    ) -> _LaunchGrantRecord:
        grant = super()._require_launch_grant(
            launch_sha256=launch_sha256,
            actor=actor,
            deadline_ms=deadline_ms,
            operation=operation,
        )
        if self._last_wall_ms >= grant.expires_at_ms:
            raise _error(
                "authority_denied",
                "The requested product resource is unavailable.",
                status=403,
            )
        return grant

    def _capacity_class_for_session(
        self,
        session: _SessionRecord,
    ) -> ProductCapacityClass:
        if session.actor_key[1] is not ProductRole.ATTENDEE:
            raise RuntimeError("non-attendee session reached attendee core")
        return ProductCapacityClass.STANDARD

    def _fence_all_attendee_work(self) -> int:
        """Synchronously revoke this attendee core without starting a worker."""
        with self._lock:
            now_ms, deadline_ms = self._now()
            for digest, actor in tuple(self._actors.items()):
                self._actors[digest] = replace(
                    actor,
                    expires_at_ms=min(actor.expires_at_ms, now_ms),
                    expires_deadline_ms=min(
                        actor.expires_deadline_ms,
                        deadline_ms,
                    ),
                )
            for digest, grant in tuple(self._launch_grants.items()):
                self._launch_grants[digest] = replace(
                    grant,
                    expires_at_ms=min(grant.expires_at_ms, now_ms),
                    expires_deadline_ms=min(
                        grant.expires_deadline_ms,
                        deadline_ms,
                    ),
                )
            for session in tuple(self._sessions_by_reference.values()):
                session.absolute_expires_at_ms = min(
                    session.absolute_expires_at_ms,
                    now_ms,
                )
                session.idle_expires_at_ms = min(
                    session.idle_expires_at_ms,
                    now_ms,
                )
                session.absolute_deadline_ms = min(
                    session.absolute_deadline_ms,
                    deadline_ms,
                )
                session.idle_deadline_ms = min(
                    session.idle_deadline_ms,
                    deadline_ms,
                )
            return self._expire_and_prune_locked(deadline_ms)

    def _fence_attendee_actor(
        self,
        actor_authority_sha256: str,
        *,
        wall_ms: int,
        deadline_ms: int,
    ) -> int:
        """Revoke one actor using the facade's already validated clocks.

        Revocation has already fenced the facade lease and launch registry by
        the time this no-I/O commit step runs. Sampling either clock again here
        could turn a transient clock failure into split authority truth. Clamp
        to any later core observation instead; fencing at the later coordinate
        is at least as restrictive and this path cannot fail on clock input.
        """
        with self._lock:
            wall_ms = max(wall_ms, self._last_wall_ms)
            deadline_ms = max(deadline_ms, self._last_deadline_ms)
            self._last_wall_ms = wall_ms
            self._last_deadline_ms = deadline_ms
            actor = self._actors.get(actor_authority_sha256)
            if actor is None:
                return 0
            actor_key = actor.key
            self._actors[actor_authority_sha256] = replace(
                actor,
                expires_at_ms=min(actor.expires_at_ms, wall_ms),
                expires_deadline_ms=min(actor.expires_deadline_ms, deadline_ms),
            )
            for digest, grant in tuple(self._launch_grants.items()):
                if grant.actor_key != actor_key:
                    continue
                self._launch_grants[digest] = replace(
                    grant,
                    expires_at_ms=min(grant.expires_at_ms, wall_ms),
                    expires_deadline_ms=min(grant.expires_deadline_ms, deadline_ms),
                )
            fenced = 0
            for session in tuple(self._sessions_by_reference.values()):
                if session.actor_key != actor_key:
                    continue
                self._terminalize_locked(
                    session,
                    terminal_at_ms=wall_ms,
                    terminal_deadline_ms=deadline_ms,
                    terminal_reason="context_revoked",
                )
                fenced += 1
            self._expire_and_prune_locked(deadline_ms)
            return fenced

    def _owns_mutation_claim(
        self,
        *,
        actor_authority: object,
        operation: ProductOperation,
        idempotency_key: object,
    ) -> bool:
        """Check claim ownership without authorizing or validating a request."""
        if not isinstance(
            idempotency_key, str
        ) or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
            return False
        actor_digest = _credential_sha256(actor_authority)
        with self._lock:
            actor = self._actors.get(actor_digest)
            if actor is None:
                return False
            scope = self._idempotency_scope(actor.key, operation, idempotency_key)
            if scope in self._replacement_claims:
                return True
            record = self._idempotency.get(scope)
            if record is None:
                return False
            deadline_ms = _clock_value(
                self._monotonic_clock_ms(),
                "monotonic_ms",
            )
            if deadline_ms < self._last_deadline_ms:
                raise RuntimeError("monotonic_ms must not move backward")
            return deadline_ms < record.retain_until_deadline_ms

    def _expire_and_prune_locked(self, deadline_ms: int) -> int:
        expired = super()._expire_and_prune_locked(deadline_ms)
        for session in tuple(self._sessions_by_reference.values()):
            if session.absolute_expires_at_ms <= session.idle_expires_at_ms:
                terminal_reason = "absolute_expired"
                terminal_at_ms = session.absolute_expires_at_ms
            else:
                terminal_reason = "idle_expired"
                terminal_at_ms = session.idle_expires_at_ms
            if self._last_wall_ms < terminal_at_ms:
                continue
            self._terminalize_locked(
                session,
                terminal_at_ms=terminal_at_ms,
                terminal_deadline_ms=deadline_ms,
                terminal_reason=terminal_reason,
            )
            expired += 1
        for reference, tombstone in tuple(self._tombstones_by_reference.items()):
            if self._last_wall_ms < tombstone.retain_until_ms:
                continue
            self._tombstones_by_reference.pop(reference, None)
            if self._current_by_actor.get(tombstone.actor_key) == reference:
                self._current_by_actor.pop(tombstone.actor_key, None)
            self._session_authorities.pop(tombstone.session_authority_sha256, None)
            for digest in tombstone.generated_value_sha256s:
                self._used_value_digests.discard(digest)
        for launch_sha256 in tuple(self._attendee_dynamic_grants):
            grant = self._launch_grants.get(launch_sha256)
            if grant is None:
                self._attendee_dynamic_grants.discard(launch_sha256)
                self._used_value_digests.discard(launch_sha256)
                continue
            if (
                deadline_ms < grant.expires_deadline_ms
                and self._last_wall_ms < grant.expires_at_ms
            ):
                continue
            self._launch_grants.pop(launch_sha256, None)
            self._attendee_dynamic_grants.discard(launch_sha256)
            self._used_value_digests.discard(launch_sha256)
            if (
                self._attendee_launch_by_selection.get(grant.selected_record_id)
                == launch_sha256
            ):
                self._attendee_launch_by_selection.pop(grant.selected_record_id, None)
        return expired


class PublicAttendeeSessionService:
    """Private-BFF attendee context, selection, and shared-core facade."""

    def __init__(
        self,
        *,
        registry: LaunchSelectionRegistry,
        core: AttendeeSessionCoreService,
        context_actors: Iterable[LaunchSelectionActorSeed],
        bff_authorities: Iterable[AttendeeBffAuthoritySeed],
        policy: AttendeeContextPolicy | None = None,
        request_admission: AttendeeRequestAdmissionLedger | None = None,
        generated_world_access: bool = False,
        generated_eligibility_key: bytes | None = None,
    ) -> None:
        if not isinstance(registry, LaunchSelectionRegistry):
            raise TypeError("registry must be LaunchSelectionRegistry")
        if not isinstance(core, AttendeeSessionCoreService):
            raise TypeError("core must be AttendeeSessionCoreService")
        if not isinstance(generated_world_access, bool):
            raise ValueError("generated_world_access must be boolean")
        if generated_world_access != core._generated_world_access:
            raise ValueError("attendee generated-world policy is incompatible")
        if generated_world_access and (
            not isinstance(generated_eligibility_key, bytes)
            or len(generated_eligibility_key) < 32
        ):
            raise ValueError("generated-world eligibility key is invalid")
        if not generated_world_access and generated_eligibility_key is not None:
            raise ValueError("generated-world eligibility key is unexpected")
        if generated_world_access and (
            registry._policy.max_revisions > core._policy.max_revisions
            or registry._policy.max_revision_bytes - 1
            > core._core_policy.max_runtime_document_bytes
        ):
            raise ValueError("attendee generated revision capacity is incompatible")
        self._generated_world_access = generated_world_access
        self._generated_eligibility_key = generated_eligibility_key
        self._policy = policy or AttendeeContextPolicy()
        if request_admission is not None and not isinstance(
            request_admission, AttendeeRequestAdmissionLedger
        ):
            raise TypeError("request_admission must be AttendeeRequestAdmissionLedger")
        if (
            request_admission is not None
            and request_admission.policy.max_tracked_contexts
            < self._policy.max_contexts
        ):
            raise ValueError(
                "request admission must track every configured attendee context"
            )
        self._request_admission = request_admission
        actor_values = tuple(islice(context_actors, self._policy.max_contexts + 1))
        if len(actor_values) != self._policy.max_contexts:
            raise ValueError(
                "context actor pool must exactly match configured capacity"
            )
        bff_values = tuple(islice(bff_authorities, 65))
        if not bff_values or len(bff_values) > 64:
            raise ValueError("one to 64 private BFF authorities are required")
        if registry.service_epoch != core.service_epoch:
            raise ValueError("attendee registry and core epochs must match")
        if (
            registry._clock_ms != core._utc_clock_ms
            or registry._monotonic_ms != core._monotonic_clock_ms
        ):
            raise ValueError("attendee registry and core must share both clocks")
        if not registry._allowed_operation_families.issubset(
            core._launch_operation_families
        ):
            raise ValueError("attendee selection operations exceed core operations")
        if core._launch_grants:
            raise ValueError("attendee core must begin without launch grants")
        _require_authored_composition(registry, core)

        self._registry = registry
        self._core = core
        self._lock = threading.RLock()
        started_at_ms = _clock_value(registry._clock_ms(), "clock_ms")
        started_deadline_ms = _clock_value(registry._monotonic_ms(), "monotonic_ms")
        self._started_at_ms = started_at_ms
        self._started_deadline_ms = started_deadline_ms
        self._last_deadline_ms = started_deadline_ms
        self._admission_opens_deadline_ms = _project_deadline(
            timestamp_ms=self._policy.admission_opens_at_ms,
            started_at_ms=started_at_ms,
            started_deadline_ms=started_deadline_ms,
        )
        self._admission_closes_deadline_ms = _project_deadline(
            timestamp_ms=self._policy.admission_closes_at_ms,
            started_at_ms=started_at_ms,
            started_deadline_ms=started_deadline_ms,
        )
        _require_offerings_within_admission_cutoff(
            registry,
            closes_at_ms=self._policy.admission_closes_at_ms,
            closes_deadline_ms=self._admission_closes_deadline_ms,
        )

        slots: list[_ContextSlot] = []
        actor_digests: set[str] = set()
        for seed in actor_values:
            if not isinstance(seed, LaunchSelectionActorSeed):
                raise TypeError("context actor must be LaunchSelectionActorSeed")
            if seed.role is not ProductRole.ATTENDEE:
                raise ValueError("context actors must use attendee authority")
            digest = _credential_sha256(seed.authority)
            registry_actor = registry._actors.get(digest)
            core_actor = core._actors.get(digest)
            if (
                digest in actor_digests
                or registry_actor is None
                or core_actor is None
                or registry_actor.key != core_actor.key
                or registry_actor.key != (seed.actor_id, ProductRole.ATTENDEE)
                or registry_actor.expires_at_ms != seed.expires_at_ms
                or core_actor.expires_at_ms != seed.expires_at_ms
                or started_at_ms >= seed.expires_at_ms
                or started_deadline_ms >= registry_actor.expires_deadline_ms
                or started_deadline_ms >= core_actor.expires_deadline_ms
            ):
                raise ValueError("context actor pool is incompatible")
            actor_digests.add(digest)
            slots.append(
                _ContextSlot(
                    actor_authority=seed.authority,
                    actor_authority_sha256=digest,
                    actor_id=seed.actor_id,
                    actor_expires_at_ms=seed.expires_at_ms,
                    actor_expires_deadline_ms=min(
                        registry_actor.expires_deadline_ms,
                        core_actor.expires_deadline_ms,
                    ),
                )
            )
        if actor_digests != set(registry._actors) or actor_digests != set(core._actors):
            raise ValueError("attendee components contain undeclared actors")
        self._slots = tuple(slots)
        self._next_slot = 0

        bff_records: dict[str, _BffAuthorityRecord] = {}
        for seed in bff_values:
            if not isinstance(seed, AttendeeBffAuthoritySeed):
                raise TypeError("BFF authority must be AttendeeBffAuthoritySeed")
            if not isinstance(
                seed.authority, str
            ) or not _BFF_AUTHORITY_PATTERN.fullmatch(seed.authority):
                raise ValueError("private BFF authority is invalid")
            if not _SAFE_IDENTITY_PATTERN.fullmatch(seed.authority_id):
                raise ValueError("private BFF authority ID is invalid")
            _require_timestamp("BFF authority expires_at_ms", seed.expires_at_ms)
            digest = _credential_sha256(seed.authority)
            if digest in bff_records or digest in actor_digests:
                raise ValueError("private authority tokens must be unique")
            bff_records[digest] = _BffAuthorityRecord(
                authority_id=seed.authority_id,
                expires_at_ms=seed.expires_at_ms,
                expires_deadline_ms=_project_deadline(
                    timestamp_ms=seed.expires_at_ms,
                    started_at_ms=started_at_ms,
                    started_deadline_ms=started_deadline_ms,
                ),
            )
        self._bff_authorities = bff_records
        self._contexts_by_digest: dict[str, _ContextLease] = {}
        self._contexts_by_actor: dict[str, _ContextLease] = {}
        self._bootstrap_idempotency: dict[
            tuple[str, str], _PrivateIdempotencyRecord
        ] = {}
        self._exchange_idempotency: dict[
            tuple[str, str], _PrivateIdempotencyRecord
        ] = {}
        self._catalog_exchange_idempotency: dict[
            tuple[str, str], _PrivateIdempotencyRecord
        ] = {}
        self._revoke_idempotency: dict[tuple[str, str], _PrivateIdempotencyRecord] = {}
        self._product_operation_gates: dict[
            tuple[str, ProductOperation, str], _ProductOperationGate
        ] = {}
        self._draining = False
        self._shutdown = False

    @property
    def service_epoch(self) -> str:
        return self._registry.service_epoch

    @property
    def lifecycle_state(self) -> str:
        """Return the explicit attendee service admission lifecycle."""
        with self._lock:
            if self._shutdown:
                return "closed"
            if self._draining:
                return "draining"
            return "ready"

    def begin_drain(self) -> bool:
        """Stop fresh attendee work while preserving replay and recovery."""
        with self._lock:
            if self._shutdown or self._draining:
                return False
            self._draining = True
            _LIFECYCLE_LOGGER.info("attendee_service_draining")
            return True

    def owns_actor_authority(self, value: object) -> bool:
        """Return routing ownership without granting or projecting authority."""
        digest = _credential_sha256(value)
        with self._lock:
            return digest in self._contexts_by_actor

    def bootstrap_context(
        self,
        *,
        bff_authority: str,
        expected_epoch: str | None,
        gateway_context_id: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Bind one high-entropy private BFF context to one burned actor slot."""
        with self._lock:
            self._require_selection_epoch(expected_epoch, mutation=True)
            wall_ms, deadline_ms = self._now_locked()
            bff_digest, bff = self._require_bff(
                bff_authority, wall_ms=wall_ms, deadline_ms=deadline_ms
            )
            context_sha256 = _gateway_context_digest(gateway_context_id)
            key = _idempotency_key(idempotency_key)
            self._prune_private_idempotency_locked(deadline_ms)
            body_sha256 = _canonical_sha256({"gateway_context_sha256": context_sha256})
            scope = (bff_digest, key)
            replay = _private_replay(
                self._bootstrap_idempotency,
                scope=scope,
                body_sha256=body_sha256,
            )
            if replay is not None:
                _LIFECYCLE_LOGGER.info("attendee_context_bootstrap_replayed")
                return replay
            self._require_bootstrap_available_locked(
                wall_ms=wall_ms,
                deadline_ms=deadline_ms,
            )
            self._admit_fresh_locked(
                context_sha256=context_sha256,
                deadline_ms=deadline_ms,
                selection_error=True,
            )
            if (
                len(self._bootstrap_idempotency)
                >= self._policy.max_bootstrap_idempotency_records
            ):
                raise _selection_error(
                    "attendee_context_idempotency_capacity_exhausted",
                    "Attendee context replay capacity is temporarily exhausted.",
                    status=503,
                    retry="after_delay",
                    retry_after_ms=1_000,
                )
            retain_until = bff.expires_deadline_ms
            claim = _PrivateIdempotencyRecord(
                body_sha256=body_sha256,
                retain_until_deadline_ms=retain_until,
            )
            self._bootstrap_idempotency[scope] = claim
            try:
                existing = self._contexts_by_digest.get(context_sha256)
                if existing is None and self._next_slot >= len(self._slots):
                    # A burned context slot is never safe to recycle within an
                    # epoch. The terminal rejection is nevertheless claimed so
                    # exact recovery is stable and a changed body conflicts.
                    raise _selection_error(
                        "attendee_context_capacity_exhausted",
                        "Attendee context capacity is exhausted for this service epoch.",
                        status=503,
                    )
                if existing is not None:
                    if _lease_expired(
                        existing, wall_ms=wall_ms, deadline_ms=deadline_ms
                    ):
                        raise _selection_error(
                            "context_expired",
                            "The attendee browser context has expired.",
                            status=403,
                        )
                    lease = existing
                    context_reused = True
                else:
                    slot_index = self._next_slot
                    slot = self._slots[slot_index]
                    expires_at_ms = min(
                        wall_ms + self._policy.context_ttl_ms,
                        slot.actor_expires_at_ms,
                        bff.expires_at_ms,
                    )
                    expires_deadline_ms = min(
                        deadline_ms + self._policy.context_ttl_ms,
                        slot.actor_expires_deadline_ms,
                        bff.expires_deadline_ms,
                    )
                    if wall_ms >= expires_at_ms or deadline_ms >= expires_deadline_ms:
                        raise _selection_error(
                            "authority_denied",
                            "The requested attendee resource is unavailable.",
                            status=403,
                        )
                    lease = _ContextLease(
                        context_sha256=context_sha256,
                        slot_index=slot_index,
                        expires_at_ms=expires_at_ms,
                        expires_deadline_ms=expires_deadline_ms,
                    )
                    self._tighten_actor_to_lease(slot, lease)
                    self._contexts_by_digest[context_sha256] = lease
                    self._contexts_by_actor[slot.actor_authority_sha256] = lease
                    # A slot is burned only after every validation succeeds.
                    self._next_slot += 1
                    context_reused = False
                slot = self._slots[lease.slot_index]
                result = {
                    "kind": "attendee_context_bootstrapped",
                    "service_epoch": self.service_epoch,
                    "replayed": False,
                    "context_reused": context_reused,
                    "actor_context": slot.actor_authority,
                    "expires_at": _format_timestamp(lease.expires_at_ms),
                }
            except LaunchSelectionError as error:
                claim.error = _RecordedSelectionError.capture(error)
                raise
            except BaseException:
                self._bootstrap_idempotency.pop(scope, None)
                raise
            claim.result = deepcopy(result)
            _LIFECYCLE_LOGGER.info(
                "attendee_context_bootstrapped reused=%s", context_reused
            )
            return result

    def options(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
    ) -> dict[str, object]:
        self._require_context_selection(
            actor_authority=actor_authority,
            expected_epoch=expected_epoch,
            mutation=False,
        )
        with self._registry._lock:
            raw_options = self._registry.offerings(
                actor_authority=actor_authority,
                expected_epoch=expected_epoch,
                required_role=ProductRole.ATTENDEE,
            )
            options = []
            for option in raw_options:
                offering_id = option.get("offering_id")
                if not isinstance(offering_id, str):
                    raise RuntimeError("attendee launch offering identity is invalid")
                offering = self._registry._offerings.get(offering_id)
                if offering is None:
                    raise RuntimeError("attendee launch offering disappeared")
                if (
                    offering.role is not ProductRole.ATTENDEE
                    or offering.origin
                    is not LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
                    or not _offering_within_admission_cutoff(
                        offering,
                        closes_at_ms=self._policy.admission_closes_at_ms,
                        closes_deadline_ms=self._admission_closes_deadline_ms,
                    )
                ):
                    continue
                options.append(_attendee_option(option, offering.notice_version))
        return {
            "kind": "attendee_launch_options",
            "service_epoch": self.service_epoch,
            "options": options,
        }

    def revoke_context(
        self,
        *,
        bff_authority: str,
        actor_authority: str,
        expected_epoch: str | None,
        gateway_context_id: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Idempotently revoke one exact BFF context and fence its work."""
        with self._lock:
            self._require_selection_epoch(expected_epoch, mutation=True)
            wall_ms, deadline_ms = self._now_locked()
            bff_digest, bff = self._require_bff(
                bff_authority,
                wall_ms=wall_ms,
                deadline_ms=deadline_ms,
            )
            context_sha256 = _gateway_context_digest(gateway_context_id)
            actor_sha256 = _credential_sha256(actor_authority)
            key = _idempotency_key(idempotency_key)
            self._prune_private_idempotency_locked(deadline_ms)
            body_sha256 = _canonical_sha256(
                {
                    "actor_authority_sha256": actor_sha256,
                    "gateway_context_sha256": context_sha256,
                }
            )
            scope = (bff_digest, key)
            replay = _private_replay(
                self._revoke_idempotency,
                scope=scope,
                body_sha256=body_sha256,
            )
            if replay is not None:
                _LIFECYCLE_LOGGER.info("attendee_context_revoke_replayed")
                return replay
            # Invalid binding attempts are deliberately unclaimed. Reserving
            # every deterministic denial would let unique mismatch keys fill
            # this bounded safety ledger and prevent a legitimate context
            # teardown. A successful context can consume at most one record,
            # so policy guarantees one replay slot for every burnable context.
            lease = self._contexts_by_digest.get(context_sha256)
            actor_lease = self._contexts_by_actor.get(actor_sha256)
            if lease is None or actor_lease is not lease:
                raise _selection_error(
                    "authority_denied",
                    "The requested attendee resource is unavailable.",
                    status=403,
                )
            slot = self._slots[lease.slot_index]
            if slot.actor_authority_sha256 != actor_sha256:
                raise _selection_error(
                    "authority_denied",
                    "The requested attendee resource is unavailable.",
                    status=403,
                )
            if _lease_expired(
                lease,
                wall_ms=wall_ms,
                deadline_ms=deadline_ms,
            ):
                raise _selection_error(
                    "authority_denied",
                    "The requested attendee resource is unavailable.",
                    status=403,
                )
            if len(self._revoke_idempotency) >= (
                self._policy.max_revoke_idempotency_records
            ):
                raise RuntimeError(
                    "attendee revoke replay capacity invariant was violated"
                )
            claim = _PrivateIdempotencyRecord(
                body_sha256=body_sha256,
                retain_until_deadline_ms=bff.expires_deadline_ms,
            )
            self._revoke_idempotency[scope] = claim
            try:
                fenced = replace(
                    lease,
                    expires_at_ms=min(lease.expires_at_ms, wall_ms),
                    expires_deadline_ms=min(
                        lease.expires_deadline_ms,
                        deadline_ms,
                    ),
                )
                self._contexts_by_digest[context_sha256] = fenced
                self._contexts_by_actor[actor_sha256] = fenced
                self._fence_registry_actor_locked(
                    slot,
                    wall_ms=wall_ms,
                    deadline_ms=deadline_ms,
                )
                self._core._fence_attendee_actor(
                    actor_sha256,
                    wall_ms=wall_ms,
                    deadline_ms=deadline_ms,
                )
                result = {
                    "kind": "attendee_context_revoked",
                    "service_epoch": self.service_epoch,
                    "replayed": False,
                    "outcome": "revoked",
                }
            except BaseException:
                self._revoke_idempotency.pop(scope, None)
                raise
            claim.result = deepcopy(result)
            _LIFECYCLE_LOGGER.info(
                "attendee_context_revoked outcome=%s", result["outcome"]
            )
            return result

    def exchange(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        launch_selection_id: str,
        notice_version_presented: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        actor_digest = self._require_context_selection(
            actor_authority=actor_authority,
            expected_epoch=expected_epoch,
            mutation=True,
        )
        key = _idempotency_key(idempotency_key)
        if not isinstance(notice_version_presented, str) or not (
            _SAFE_IDENTITY_PATTERN.fullmatch(notice_version_presented)
        ):
            raise _selection_error(
                "invalid_request",
                "The attendee launch request is invalid.",
                status=400,
            )
        if not isinstance(launch_selection_id, str) or not (
            _LAUNCH_SELECTION_ID_PATTERN.fullmatch(launch_selection_id)
        ):
            raise _selection_error(
                "invalid_request",
                "The attendee launch request is invalid.",
                status=400,
            )
        body_sha256 = _canonical_sha256(
            {
                "launch_selection_sha256": _credential_sha256(launch_selection_id),
                "notice_version_presented": notice_version_presented,
            }
        )
        registry_idempotency_key = _registry_idempotency_key(
            key=key,
            body_sha256=body_sha256,
        )
        scope = (actor_digest, key)
        with self._lock:
            wall_ms, deadline_ms = self._now_locked()
            self._require_context_locked(
                actor_authority,
                wall_ms=wall_ms,
                deadline_ms=deadline_ms,
                selection_error=True,
            )
            self._prune_private_idempotency_locked(deadline_ms)
            replay = _private_replay(
                self._exchange_idempotency,
                scope=scope,
                body_sha256=body_sha256,
            )
            if replay is not None:
                return replay
            self._require_not_draining_selection_locked()
            self._admit_fresh_locked(
                context_sha256=self._context_request_key_locked(actor_digest),
                deadline_ms=deadline_ms,
                selection_error=True,
            )
            if (
                len(self._exchange_idempotency)
                + len(self._catalog_exchange_idempotency)
                >= self._policy.max_exchange_idempotency_records
            ):
                raise _selection_capacity()
            claim = _PrivateIdempotencyRecord(
                body_sha256=body_sha256,
                retain_until_deadline_ms=(
                    deadline_ms + self._registry._policy.idempotency_ttl_ms
                ),
            )
            self._exchange_idempotency[scope] = claim
            try:
                self._require_admission_window_selection(
                    wall_ms=wall_ms, deadline_ms=deadline_ms
                )

                def provision(
                    issue: LaunchGrantIssue,
                    resolved: ResolvedLaunchSelection,
                ) -> LaunchGrantProvisioningReceipt:
                    if (
                        issue.origin
                        is not LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
                        or resolved.origin
                        is not LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
                        or resolved.expires_at_ms > self._policy.admission_closes_at_ms
                        or resolved.expires_deadline_ms
                        > self._admission_closes_deadline_ms
                    ):
                        raise _selection_error(
                            "launch_selection_unavailable",
                            "The Presenter Stage launch selection is unavailable.",
                            status=404,
                        )
                    if issue.notice_version != notice_version_presented:
                        raise _selection_error(
                            "notice_version_mismatch",
                            (
                                "The presented privacy notice version does not "
                                "match this offering."
                            ),
                            status=409,
                        )
                    try:
                        return self._core.provision_launch_selection(
                            actor_authority=actor_authority,
                            expected_epoch=issue.service_epoch,
                            issue=issue,
                            resolved=resolved,
                        )
                    except AttendeeSelectionCapacityError:
                        raise LaunchGrantProvisioningCapacityError() from None

                issue = self._registry.exchange_selection(
                    actor_authority=actor_authority,
                    expected_epoch=expected_epoch,
                    launch_selection_id=launch_selection_id,
                    idempotency_key=registry_idempotency_key,
                    provision=provision,
                    required_role=ProductRole.ATTENDEE,
                )
                result = {
                    "kind": "attendee_launch_selection_granted",
                    "service_epoch": issue.service_epoch,
                    "replayed": issue.replayed,
                    "launch_grant": issue.launch_grant,
                    "grant_expires_at": _format_timestamp(issue.expires_at_ms),
                    "selection": _attendee_option(
                        issue.selection_projection(), issue.notice_version
                    ),
                }
            except LaunchSelectionError as error:
                if error.retry == "after_delay":
                    self._exchange_idempotency.pop(scope, None)
                else:
                    claim.error = _RecordedSelectionError.capture(error)
                raise
            except BaseException:
                self._exchange_idempotency.pop(scope, None)
                raise
            claim.result = deepcopy(result)
            return result

    def exchange_catalog_offering(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        offering_id: str,
        offering_revision: str,
        notice_version_presented: str,
        idempotency_key: str,
        catalog_eligibility_proof: AttendeeCatalogEligibilityProof | None = None,
    ) -> dict[str, object]:
        """Exchange one catalog locator through the separate access boundary."""
        actor_digest = self._require_context_selection(
            actor_authority=actor_authority,
            expected_epoch=expected_epoch,
            mutation=True,
        )
        if not self._generated_world_access:
            raise _selection_error(
                "launch_selection_unavailable",
                "The attendee launch selection is unavailable.",
                status=404,
            )
        key = _idempotency_key(idempotency_key)
        if not isinstance(notice_version_presented, str) or not (
            _SAFE_IDENTITY_PATTERN.fullmatch(notice_version_presented)
        ):
            raise _selection_error(
                "invalid_request",
                "The attendee launch request is invalid.",
                status=400,
            )
        if not isinstance(offering_id, str) or not (
            _OFFERING_ID_PATTERN.fullmatch(offering_id)
        ):
            raise _selection_error(
                "invalid_request",
                "The attendee launch request is invalid.",
                status=400,
            )
        if not isinstance(offering_revision, str) or not (
            _OFFERING_REVISION_PATTERN.fullmatch(offering_revision)
        ):
            raise _selection_error(
                "invalid_request",
                "The attendee launch request is invalid.",
                status=400,
            )
        body_sha256 = _canonical_sha256(
            {
                "offering_id": offering_id,
                "offering_revision": offering_revision,
                "notice_version_presented": notice_version_presented,
            }
        )
        registry_idempotency_key = _registry_idempotency_key(
            key=key,
            body_sha256=body_sha256,
        )
        scope = (actor_digest, key)
        with self._lock:
            wall_ms, deadline_ms = self._now_locked()
            self._require_context_locked(
                actor_authority,
                wall_ms=wall_ms,
                deadline_ms=deadline_ms,
                selection_error=True,
            )
            self._prune_private_idempotency_locked(deadline_ms)
            replay = _private_replay(
                self._catalog_exchange_idempotency,
                scope=scope,
                body_sha256=body_sha256,
            )
            if replay is not None:
                return replay
            self._require_not_draining_selection_locked()
            self._admit_fresh_locked(
                context_sha256=self._context_request_key_locked(actor_digest),
                deadline_ms=deadline_ms,
                selection_error=True,
            )
            if (
                len(self._exchange_idempotency)
                + len(self._catalog_exchange_idempotency)
                >= self._policy.max_exchange_idempotency_records
            ):
                raise _catalog_selection_capacity()
            claim = _PrivateIdempotencyRecord(
                body_sha256=body_sha256,
                retain_until_deadline_ms=(
                    deadline_ms + self._registry._policy.idempotency_ttl_ms
                ),
            )
            self._catalog_exchange_idempotency[scope] = claim
            try:
                self._require_admission_window_selection(
                    wall_ms=wall_ms, deadline_ms=deadline_ms
                )

                def provision(
                    issue: LaunchGrantIssue,
                    resolved: ResolvedLaunchSelection,
                ) -> LaunchGrantProvisioningReceipt:
                    generated = (
                        issue.origin is LaunchSelectionOrigin.WORLD_FORGE_SELECTION
                        and resolved.origin
                        is LaunchSelectionOrigin.WORLD_FORGE_SELECTION
                    )
                    authored = (
                        issue.origin is LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
                        and resolved.origin
                        is LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
                    )
                    if (
                        not (authored or (generated and self._generated_world_access))
                        or resolved.expires_at_ms > self._policy.admission_closes_at_ms
                        or resolved.expires_deadline_ms
                        > self._admission_closes_deadline_ms
                    ):
                        raise _selection_error(
                            "launch_selection_unavailable",
                            "The attendee launch selection is unavailable.",
                            status=404,
                        )
                    if issue.notice_version != notice_version_presented:
                        raise _selection_error(
                            "notice_version_mismatch",
                            (
                                "The presented privacy notice version does not "
                                "match this offering."
                            ),
                            status=409,
                        )
                    eligibility_key = self._generated_eligibility_key
                    if eligibility_key is None:  # Constructor enforces this.
                        raise RuntimeError("catalog eligibility key disappeared")
                    _verify_attendee_catalog_eligibility(
                        catalog_eligibility_proof,
                        key=eligibility_key,
                        issue=issue,
                        resolved=resolved,
                        wall_ms=wall_ms,
                        offering_revision=offering_revision,
                    )
                    try:
                        if generated:
                            receipt = self._core.provision_generated_launch_selection(
                                actor_authority=actor_authority,
                                expected_epoch=issue.service_epoch,
                                issue=issue,
                                resolved=resolved,
                            )
                        else:
                            receipt = self._core.provision_launch_selection(
                                actor_authority=actor_authority,
                                expected_epoch=issue.service_epoch,
                                issue=issue,
                                resolved=resolved,
                            )
                        return receipt
                    except AttendeeSelectionCapacityError:
                        raise LaunchGrantProvisioningCapacityError() from None

                issue = self._registry.exchange_offering(
                    actor_authority=actor_authority,
                    expected_epoch=expected_epoch,
                    offering_id=offering_id,
                    idempotency_key=registry_idempotency_key,
                    provision=provision,
                    required_role=ProductRole.ATTENDEE,
                    require_launch_binding_receipt=True,
                )
                launch_binding_sha256 = issue.launch_binding_sha256
                if (
                    not isinstance(launch_binding_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", launch_binding_sha256) is None
                ):
                    raise RuntimeError("attendee launch binding receipt is invalid")
                result = {
                    "kind": "world_access_launch_selection_granted",
                    "service_epoch": issue.service_epoch,
                    "replayed": issue.replayed,
                    "launch_grant": issue.launch_grant,
                    "launch_binding_sha256": launch_binding_sha256,
                    "grant_expires_at": _format_timestamp(issue.expires_at_ms),
                    "selection": _catalog_attendee_option(
                        issue.selection_projection(), issue.notice_version
                    ),
                }
            except LaunchSelectionError as error:
                if error.retry == "after_delay":
                    self._catalog_exchange_idempotency.pop(scope, None)
                else:
                    claim.error = _RecordedSelectionError.capture(error)
                raise
            except BaseException:
                self._catalog_exchange_idempotency.pop(scope, None)
                raise
            claim.result = deepcopy(result)
            return result

    def create(self, **kwargs: object) -> dict[str, object]:
        return self._delegate_product("create", kwargs, mutation=True, new_work=True)

    def current(self, **kwargs: object) -> dict[str, object]:
        return self._delegate_product("current", kwargs, mutation=False)

    def snapshot(self, **kwargs: object) -> dict[str, object]:
        return self._delegate_product("snapshot", kwargs, mutation=False)

    def submit_command(self, **kwargs: object) -> dict[str, object]:
        return self._delegate_product(
            "submit_command", kwargs, mutation=True, new_work=True
        )

    def cancel_command(self, **kwargs: object) -> dict[str, object]:
        return self._delegate_product("cancel_command", kwargs, mutation=True)

    def command_result(self, **kwargs: object) -> dict[str, object]:
        return self._delegate_product("command_result", kwargs, mutation=False)

    def events(self, **kwargs: object) -> dict[str, object]:
        return self._delegate_product("events", kwargs, mutation=False)

    def transcript(self, **kwargs: object) -> dict[str, object]:
        return self._delegate_product("transcript", kwargs, mutation=False)

    def reset(self, **kwargs: object) -> dict[str, object]:
        return self._delegate_product("reset", kwargs, mutation=True, new_work=True)

    def switch(self, **kwargs: object) -> dict[str, object]:
        return self._delegate_product("switch", kwargs, mutation=True, new_work=True)

    def close(self, **kwargs: object) -> dict[str, object]:
        return self._delegate_product("close", kwargs, mutation=True)

    def reconcile_due(self) -> int:
        """Run explicit lazy command reconciliation; starts no worker."""
        return self._core.reconcile_due()

    def expire_due(self) -> int:
        """Run coordinated lazy expiry across attendee-owned bounded stores."""
        with self._lock:
            wall_ms, deadline_ms = self._now_locked()
            self._prune_private_idempotency_locked(deadline_ms)
            with self._registry._lock:
                self._registry._expire_and_prune(deadline_ms, wall_ms)
            return self._core.expire_due()

    def shutdown(self) -> int:
        """Idempotently revoke attendee authority and fence active work."""
        with self._lock:
            wall_ms, deadline_ms = self._now_locked()
            self._shutdown = True
            for digest, record in tuple(self._bff_authorities.items()):
                self._bff_authorities[digest] = replace(
                    record,
                    expires_at_ms=min(record.expires_at_ms, wall_ms),
                    expires_deadline_ms=min(
                        record.expires_deadline_ms,
                        deadline_ms,
                    ),
                )
            for context_sha256, lease in tuple(self._contexts_by_digest.items()):
                fenced = replace(
                    lease,
                    expires_at_ms=min(lease.expires_at_ms, wall_ms),
                    expires_deadline_ms=min(
                        lease.expires_deadline_ms,
                        deadline_ms,
                    ),
                )
                self._contexts_by_digest[context_sha256] = fenced
                slot = self._slots[fenced.slot_index]
                self._contexts_by_actor[slot.actor_authority_sha256] = fenced
            with self._registry._lock:
                for slot in self._slots:
                    actor = self._registry._actors.get(slot.actor_authority_sha256)
                    if actor is None:
                        continue
                    self._registry._actors[slot.actor_authority_sha256] = replace(
                        actor,
                        expires_at_ms=min(actor.expires_at_ms, wall_ms),
                        expires_deadline_ms=min(
                            actor.expires_deadline_ms,
                            deadline_ms,
                        ),
                    )
                for digest, grant in tuple(self._registry._grants.items()):
                    self._registry._grants[digest] = replace(
                        grant,
                        expires_at_ms=min(grant.expires_at_ms, wall_ms),
                        expires_deadline_ms=min(
                            grant.expires_deadline_ms,
                            deadline_ms,
                        ),
                    )
                self._registry._expire_and_prune(deadline_ms, wall_ms)
            self._prune_private_idempotency_locked(deadline_ms)
            return self._core._fence_all_attendee_work()

    def _delegate_product(
        self,
        operation_name: str,
        kwargs: Mapping[str, object],
        *,
        mutation: bool,
        new_work: bool = False,
    ) -> dict[str, object]:
        actor_authority = kwargs.get("actor_authority")
        expected_epoch = kwargs.get("expected_epoch")
        if not isinstance(actor_authority, str):
            raise _product_authority_denied()
        actor_digest = self._require_context_product(
            actor_authority=actor_authority,
            expected_epoch=(
                expected_epoch if isinstance(expected_epoch, str) else None
            ),
            mutation=mutation,
            require_admission=False,
        )
        operation = getattr(self._core, operation_name)
        if not new_work:
            return operation(**dict(kwargs))
        product_operation = {
            "create": ProductOperation.CREATE,
            "submit_command": ProductOperation.SUBMIT,
            "reset": ProductOperation.RESET,
            "switch": ProductOperation.SWITCH,
        }[operation_name]
        key = kwargs.get("idempotency_key")
        if self._request_admission is None:
            with self._lock:
                claimed_replay = self._core._owns_mutation_claim(
                    actor_authority=actor_authority,
                    operation=product_operation,
                    idempotency_key=key,
                )
                if not claimed_replay:
                    wall_ms, deadline_ms = self._now_locked()
                    self._require_admission_window_product(
                        wall_ms=wall_ms,
                        deadline_ms=deadline_ms,
                    )
                return operation(**dict(kwargs))
        gate_scope = (
            (actor_digest, product_operation, key)
            if isinstance(key, str) and _IDEMPOTENCY_KEY_PATTERN.fullmatch(key)
            else None
        )
        gate = (
            self._product_operation_gate(gate_scope)
            if gate_scope is not None
            else nullcontext()
        )
        with gate:
            # A concurrent identical key waits here until the first request has
            # either installed a core claim or returned an unclaimed transient
            # failure. This makes replay/conflict classification precede rate
            # charging without serializing unrelated or safety operations.
            actor_digest = self._require_context_product(
                actor_authority=actor_authority,
                expected_epoch=(
                    expected_epoch if isinstance(expected_epoch, str) else None
                ),
                mutation=mutation,
                require_admission=False,
            )
            claimed_replay = self._core._owns_mutation_claim(
                actor_authority=actor_authority,
                operation=product_operation,
                idempotency_key=key,
            )
            if claimed_replay:
                return operation(**dict(kwargs))
            with self._lock:
                wall_ms, deadline_ms = self._now_locked()
                current_actor_digest = self._require_context_locked(
                    actor_authority,
                    wall_ms=wall_ms,
                    deadline_ms=deadline_ms,
                    selection_error=False,
                )
                if current_actor_digest != actor_digest:
                    raise RuntimeError("attendee context identity changed")
                self._require_admission_window_product(
                    wall_ms=wall_ms,
                    deadline_ms=deadline_ms,
                )
                self._admit_fresh_locked(
                    context_sha256=self._context_request_key_locked(actor_digest),
                    deadline_ms=deadline_ms,
                    selection_error=False,
                )
                return operation(**dict(kwargs))

    def _require_context_selection(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        mutation: bool,
    ) -> str:
        with self._lock:
            self._require_selection_epoch(expected_epoch, mutation=mutation)
            wall_ms, deadline_ms = self._now_locked()
            return self._require_context_locked(
                actor_authority,
                wall_ms=wall_ms,
                deadline_ms=deadline_ms,
                selection_error=True,
            )

    def _require_context_product(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        mutation: bool,
        require_admission: bool,
    ) -> str:
        with self._lock:
            self._require_product_epoch(expected_epoch, mutation=mutation)
            wall_ms, deadline_ms = self._now_locked()
            digest = self._require_context_locked(
                actor_authority,
                wall_ms=wall_ms,
                deadline_ms=deadline_ms,
                selection_error=False,
            )
            if require_admission:
                self._require_admission_window_product(
                    wall_ms=wall_ms, deadline_ms=deadline_ms
                )
            return digest

    def _require_context_locked(
        self,
        actor_authority: str,
        *,
        wall_ms: int,
        deadline_ms: int,
        selection_error: bool,
    ) -> str:
        # Wall-aware core expiry runs before an expired context is denied, so
        # abandoned sessions release admission and fence active work.
        self._core.expire_due()
        digest = _credential_sha256(actor_authority)
        lease = self._contexts_by_actor.get(digest)
        if lease is None or _lease_expired(
            lease, wall_ms=wall_ms, deadline_ms=deadline_ms
        ):
            if selection_error:
                raise _selection_error(
                    "authority_denied",
                    "The requested attendee resource is unavailable.",
                    status=403,
                )
            raise _product_authority_denied()
        return digest

    def _service_is_closed(self, *, wall_ms: int, deadline_ms: int) -> bool:
        return (
            self._shutdown
            or self._draining
            or (
                wall_ms >= self._policy.admission_closes_at_ms
                or deadline_ms >= self._admission_closes_deadline_ms
            )
        )

    def _require_not_draining_selection_locked(self) -> None:
        if self._draining:
            raise _selection_error(
                "service_closed",
                "Attendee admission is closed.",
                status=410,
            )

    def _require_bootstrap_available_locked(
        self,
        *,
        wall_ms: int,
        deadline_ms: int,
    ) -> None:
        self._require_not_draining_selection_locked()
        if self._policy.bootstrap_closes_at_admission and (
            wall_ms >= self._policy.admission_closes_at_ms
            or deadline_ms >= self._admission_closes_deadline_ms
        ):
            raise _selection_error(
                "service_closed",
                "Attendee admission is closed.",
                status=410,
            )

    def _require_bff(
        self,
        authority: str,
        *,
        wall_ms: int,
        deadline_ms: int,
    ) -> tuple[str, _BffAuthorityRecord]:
        digest = _credential_sha256(authority)
        record = self._bff_authorities.get(digest)
        if (
            record is None
            or wall_ms >= record.expires_at_ms
            or deadline_ms >= record.expires_deadline_ms
        ):
            raise _selection_error(
                "authority_denied",
                "The requested attendee resource is unavailable.",
                status=403,
            )
        return digest, record

    def _require_selection_epoch(
        self, expected_epoch: str | None, *, mutation: bool
    ) -> None:
        if not isinstance(expected_epoch, str) or not expected_epoch:
            raise _selection_error(
                "expected_epoch_required",
                "Expected attendee service epoch evidence is required.",
                status=400,
            )
        if not secrets.compare_digest(expected_epoch, self.service_epoch):
            raise _selection_error(
                "outcome_unknown" if mutation else "session_lost",
                (
                    "The prior attendee mutation outcome is unavailable after restart."
                    if mutation
                    else "The attendee session was lost after service restart."
                ),
                status=409,
            )

    def _require_product_epoch(
        self, expected_epoch: str | None, *, mutation: bool
    ) -> None:
        if not isinstance(expected_epoch, str) or not expected_epoch:
            raise _error(
                "expected_epoch_required",
                "Expected product service epoch evidence is required.",
                status=400,
            )
        if not secrets.compare_digest(expected_epoch, self.service_epoch):
            raise _error(
                "outcome_unknown" if mutation else "session_lost",
                (
                    "The prior product mutation outcome is unavailable after restart."
                    if mutation
                    else "The product session was lost after service restart."
                ),
                status=409,
            )

    def _require_admission_window_selection(
        self, *, wall_ms: int, deadline_ms: int
    ) -> None:
        if (
            wall_ms < self._policy.admission_opens_at_ms
            or deadline_ms < self._admission_opens_deadline_ms
        ):
            raise _selection_error(
                "service_not_open",
                "Attendee admission is not open yet.",
                status=503,
                retry="after_delay",
                retry_after_ms=1_000,
            )
        if self._service_is_closed(wall_ms=wall_ms, deadline_ms=deadline_ms):
            raise _selection_error(
                "service_closed",
                "Attendee admission is closed.",
                status=410,
            )

    def _require_admission_window_product(
        self, *, wall_ms: int, deadline_ms: int
    ) -> None:
        try:
            self._require_admission_window_selection(
                wall_ms=wall_ms, deadline_ms=deadline_ms
            )
        except LaunchSelectionError as error:
            raise ProductLifecycleError(
                code=error.code,
                message=error.message,
                status=error.status,
                retry=error.retry,
                retry_after_ms=error.retry_after_ms,
            ) from None

    def _now_locked(self) -> tuple[int, int]:
        wall_ms = _clock_value(self._registry._clock_ms(), "clock_ms")
        deadline_ms = _clock_value(self._registry._monotonic_ms(), "monotonic_ms")
        if deadline_ms < self._last_deadline_ms:
            raise RuntimeError("monotonic_ms must not move backward")
        self._last_deadline_ms = deadline_ms
        return wall_ms, deadline_ms

    def _prune_private_idempotency_locked(self, deadline_ms: int) -> None:
        for records in (
            self._bootstrap_idempotency,
            self._exchange_idempotency,
            self._catalog_exchange_idempotency,
            self._revoke_idempotency,
        ):
            expired = [
                scope
                for scope, record in records.items()
                if deadline_ms >= record.retain_until_deadline_ms
            ]
            for scope in expired:
                records.pop(scope, None)

    def _context_request_key_locked(self, actor_sha256: str) -> str:
        lease = self._contexts_by_actor.get(actor_sha256)
        if lease is None:
            raise RuntimeError("attendee context disappeared during admission")
        return lease.context_sha256

    @contextmanager
    def _product_operation_gate(
        self,
        scope: tuple[str, ProductOperation, str],
    ) -> Iterator[None]:
        with self._lock:
            gate = self._product_operation_gates.get(scope)
            if gate is None:
                gate = _ProductOperationGate()
                self._product_operation_gates[scope] = gate
            gate.users += 1
        acquired = False
        try:
            gate.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                gate.lock.release()
            with self._lock:
                gate.users -= 1
                if gate.users == 0 and self._product_operation_gates.get(scope) is gate:
                    self._product_operation_gates.pop(scope, None)

    def _admit_fresh_locked(
        self,
        *,
        context_sha256: str,
        deadline_ms: int,
        selection_error: bool,
    ) -> None:
        admission = self._request_admission
        if admission is None:
            return
        try:
            admission.admit_fresh(
                context_sha256=context_sha256,
                deadline_ms=deadline_ms,
            )
        except AttendeeRequestAdmissionDenied as denial:
            if selection_error:
                raise _selection_error(
                    "logical_rate_limited",
                    "Attendee request admission is temporarily limited.",
                    status=429,
                    retry="after_delay",
                    retry_after_ms=denial.retry_after_ms,
                ) from None
            raise ProductLifecycleError(
                code="logical_rate_limited",
                message="Attendee request admission is temporarily limited.",
                status=429,
                retry="after_delay",
                retry_after_ms=denial.retry_after_ms,
            ) from None

    def _fence_registry_actor_locked(
        self,
        slot: _ContextSlot,
        *,
        wall_ms: int,
        deadline_ms: int,
    ) -> None:
        digest = slot.actor_authority_sha256
        with self._registry._lock:
            actor = self._registry._actors.get(digest)
            if actor is None or actor.key != (slot.actor_id, ProductRole.ATTENDEE):
                raise RuntimeError("attendee registry actor disappeared during revoke")
            self._registry._actors[digest] = replace(
                actor,
                expires_at_ms=min(actor.expires_at_ms, wall_ms),
                expires_deadline_ms=min(actor.expires_deadline_ms, deadline_ms),
            )
            for grant_digest, grant in tuple(self._registry._grants.items()):
                if (grant.actor_id, grant.role) != actor.key:
                    continue
                self._registry._grants[grant_digest] = replace(
                    grant,
                    expires_at_ms=min(grant.expires_at_ms, wall_ms),
                    expires_deadline_ms=min(
                        grant.expires_deadline_ms,
                        deadline_ms,
                    ),
                )
            self._registry._expire_and_prune(deadline_ms, wall_ms)

    def _tighten_actor_to_lease(
        self,
        slot: _ContextSlot,
        lease: _ContextLease,
    ) -> None:
        """Make the lease the real actor boundary in both owning services."""
        digest = slot.actor_authority_sha256
        with self._registry._lock, self._core._lock:
            registry_actor = self._registry._actors.get(digest)
            core_actor = self._core._actors.get(digest)
            if registry_actor is None or core_actor is None:
                raise RuntimeError("attendee actor disappeared during bootstrap")
            self._registry._actors[digest] = replace(
                registry_actor,
                expires_at_ms=lease.expires_at_ms,
                expires_deadline_ms=lease.expires_deadline_ms,
            )
            self._core._actors[digest] = replace(
                core_actor,
                expires_at_ms=lease.expires_at_ms,
                expires_deadline_ms=lease.expires_deadline_ms,
            )


def _require_authored_composition(
    registry: LaunchSelectionRegistry,
    core: AttendeeSessionCoreService,
) -> None:
    if not registry._offerings:
        raise ValueError("attendee registry requires an authored offering")
    if any(
        offering.role is not ProductRole.ATTENDEE
        or offering.origin is not LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
        for offering in registry._offerings.values()
    ):
        raise ValueError("attendee registry accepts only authored attendee offerings")
    if set(registry._revisions) != set(core._revisions):
        raise ValueError("attendee registry and core revisions differ")
    for revision_id, selected in registry._revisions.items():
        core_revision = core._revisions[revision_id]
        if (
            selected.allocate().get("kind") != "dreamland.authored_world_revision"
            or selected.canonical_json.removesuffix(b"\n")
            != core_revision.canonical_json
        ):
            raise ValueError("attendee immutable revisions are incompatible")


def _require_offerings_within_admission_cutoff(
    registry: LaunchSelectionRegistry,
    *,
    closes_at_ms: int,
    closes_deadline_ms: int,
) -> None:
    if any(
        not _offering_within_admission_cutoff(
            offering,
            closes_at_ms=closes_at_ms,
            closes_deadline_ms=closes_deadline_ms,
        )
        for offering in registry._offerings.values()
    ):
        raise ValueError("attendee offerings must expire by admission close")


def _offering_within_admission_cutoff(
    offering: object,
    *,
    closes_at_ms: int,
    closes_deadline_ms: int,
) -> bool:
    expires_at_ms = getattr(offering, "expires_at_ms", None)
    expires_deadline_ms = getattr(offering, "expires_deadline_ms", None)
    return (
        isinstance(expires_at_ms, int)
        and expires_at_ms <= closes_at_ms
        and isinstance(expires_deadline_ms, int)
        and expires_deadline_ms <= closes_deadline_ms
    )


def _attendee_option(
    projection: Mapping[str, object],
    notice_version: str,
) -> dict[str, object]:
    """Add only the required safe notice coordinate to a generic option."""
    expected = {
        "launch_selection_id",
        "offering_id",
        "display",
        "origin",
        "revision",
        "expires_at",
    }
    if set(projection) != expected or not _SAFE_IDENTITY_PATTERN.fullmatch(
        notice_version
    ):
        raise RuntimeError("attendee registry returned an invalid option")
    origin = projection.get("origin")
    if not isinstance(origin, Mapping) or origin != {
        "kind": LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT.value
    }:
        raise RuntimeError("attendee registry exposed a non-authored option")
    result = deepcopy(dict(projection))
    result["notice_version"] = notice_version
    return result


def _catalog_attendee_option(
    projection: Mapping[str, object],
    notice_version: str,
) -> dict[str, object]:
    """Project a catalog selection without its private registry locator."""
    expected = {
        "launch_selection_id",
        "offering_id",
        "display",
        "origin",
        "revision",
        "expires_at",
    }
    if set(projection) != expected or not _SAFE_IDENTITY_PATTERN.fullmatch(
        notice_version
    ):
        raise RuntimeError("attendee registry returned an invalid catalog offering")
    origin = projection.get("origin")
    if not isinstance(origin, Mapping) or origin.get("kind") not in {
        LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT.value,
        LaunchSelectionOrigin.WORLD_FORGE_SELECTION.value,
    }:
        raise RuntimeError("attendee registry exposed an invalid catalog origin")
    result = deepcopy(dict(projection))
    result.pop("launch_selection_id")
    result["notice_version"] = notice_version
    return result


def mint_attendee_catalog_eligibility_proof(
    *,
    key: bytes,
    service_epoch: str,
    offering_id: str,
    offering_revision: str,
    access_record_id: str,
    origin: LaunchSelectionOrigin,
    revision_id: str,
    immutable_digest: str,
    expires_at_ms: int,
) -> AttendeeCatalogEligibilityProof:
    """Mint a repr-hidden proof for trusted catalog composition only."""
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("generated-world eligibility key is invalid")
    if not isinstance(service_epoch, str) or not service_epoch:
        raise ValueError("generated-world eligibility epoch is invalid")
    if _OFFERING_ID_PATTERN.fullmatch(offering_id) is None:
        raise ValueError("generated-world eligibility offering is invalid")
    if _OFFERING_REVISION_PATTERN.fullmatch(offering_revision) is None:
        raise ValueError("generated-world offering revision is invalid")
    if _SAFE_IDENTITY_PATTERN.fullmatch(access_record_id) is None:
        raise ValueError("generated-world access record is invalid")
    if not isinstance(origin, LaunchSelectionOrigin):
        raise ValueError("catalog eligibility origin is invalid")
    if _SAFE_IDENTITY_PATTERN.fullmatch(revision_id) is None:
        raise ValueError("generated-world revision is invalid")
    if _SHA256_PATTERN.fullmatch(immutable_digest) is None:
        raise ValueError("generated-world digest is invalid")
    _require_timestamp("generated-world eligibility expires_at_ms", expires_at_ms)
    values = {
        "access_record_id": access_record_id,
        "expires_at_ms": expires_at_ms,
        "immutable_digest": immutable_digest,
        "offering_id": offering_id,
        "offering_revision": offering_revision,
        "origin": origin.value,
        "revision_id": revision_id,
        "service_epoch": service_epoch,
    }
    return AttendeeCatalogEligibilityProof(
        service_epoch=service_epoch,
        offering_id=offering_id,
        offering_revision=offering_revision,
        access_record_id=access_record_id,
        origin=origin,
        revision_id=revision_id,
        immutable_digest=immutable_digest,
        expires_at_ms=expires_at_ms,
        verification_tag=_generated_eligibility_tag(key, values),
    )


def _verify_attendee_catalog_eligibility(
    proof: AttendeeCatalogEligibilityProof | None,
    *,
    key: bytes,
    issue: LaunchGrantIssue,
    resolved: ResolvedLaunchSelection,
    wall_ms: int,
    offering_revision: str,
) -> None:
    def unavailable() -> LaunchSelectionError:
        return _selection_error(
            "launch_selection_unavailable",
            "The attendee launch selection is unavailable.",
            status=404,
        )

    if not isinstance(proof, AttendeeCatalogEligibilityProof):
        raise unavailable()
    values = {
        "access_record_id": proof.access_record_id,
        "expires_at_ms": proof.expires_at_ms,
        "immutable_digest": proof.immutable_digest,
        "offering_id": proof.offering_id,
        "offering_revision": proof.offering_revision,
        "origin": proof.origin.value,
        "revision_id": proof.revision_id,
        "service_epoch": proof.service_epoch,
    }
    expected_tag = _generated_eligibility_tag(key, values)
    if (
        not hmac.compare_digest(proof.verification_tag, expected_tag)
        or proof.service_epoch != issue.service_epoch
        or proof.service_epoch != resolved.service_epoch
        or proof.offering_id != issue.offering_id
        or proof.offering_id != resolved.offering_id
        or proof.offering_revision != offering_revision
        or proof.origin is not issue.origin
        or proof.origin is not resolved.origin
        or proof.revision_id != resolved.revision.revision_id
        or proof.immutable_digest != resolved.revision.immutable_digest
        or wall_ms >= proof.expires_at_ms
        or resolved.expires_at_ms > proof.expires_at_ms
    ):
        raise unavailable()


def _generated_eligibility_tag(key: bytes, values: Mapping[str, object]) -> str:
    material = json.dumps(
        dict(values),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hmac.new(key, material, hashlib.sha256).hexdigest()


def _private_replay(
    records: Mapping[tuple[str, str], _PrivateIdempotencyRecord],
    *,
    scope: tuple[str, str],
    body_sha256: str,
) -> dict[str, object] | None:
    record = records.get(scope)
    if record is None:
        return None
    if record.body_sha256 != body_sha256:
        raise _selection_error(
            "idempotency_conflict",
            "The idempotency key is already bound to another request.",
            status=409,
        )
    if record.error is not None:
        raise record.error.restore()
    if record.result is None:
        raise RuntimeError("private attendee idempotency replay is incomplete")
    result = deepcopy(record.result)
    result["replayed"] = True
    return result


def _gateway_context_digest(value: object) -> str:
    if not isinstance(value, str) or not _GATEWAY_CONTEXT_PATTERN.fullmatch(value):
        raise _selection_error(
            "invalid_request",
            "The attendee gateway context is invalid.",
            status=400,
        )
    return _credential_sha256(value)


def _idempotency_key(value: object) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise _selection_error(
            "invalid_request",
            "The attendee idempotency key is invalid.",
            status=400,
        )
    return value


def _canonical_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _registry_idempotency_key(*, key: str, body_sha256: str) -> str:
    """Bind the inner selection claim to the facade's complete semantics."""
    digest = hashlib.sha256(
        ("dreamland.attendee.exchange.v1\0" + key + "\0" + body_sha256).encode("utf-8")
    ).hexdigest()
    return "attendee-inner:" + digest


def _selected_record_id(launch_grant: str) -> str:
    return "selected-record:" + hashlib.sha256(launch_grant.encode("utf-8")).hexdigest()


def _lease_expired(lease: _ContextLease, *, wall_ms: int, deadline_ms: int) -> bool:
    return wall_ms >= lease.expires_at_ms or deadline_ms >= lease.expires_deadline_ms


def _project_deadline(
    *, timestamp_ms: int, started_at_ms: int, started_deadline_ms: int
) -> int:
    return started_deadline_ms + max(0, timestamp_ms - started_at_ms)


def _clock_value(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{name} must return a non-negative integer")
    return value


def _format_timestamp(value: int) -> str:
    timespec = "seconds" if value % 1_000 == 0 else "milliseconds"
    return (
        datetime.fromtimestamp(value / 1_000, UTC)
        .isoformat(timespec=timespec)
        .replace("+00:00", "Z")
    )


def _selection_capacity() -> LaunchSelectionError:
    return _selection_error(
        "launch_selection_capacity_exhausted",
        "Presenter Stage launch-selection capacity is exhausted.",
        status=503,
        retry="after_delay",
        retry_after_ms=1_000,
    )


def _catalog_selection_capacity() -> LaunchSelectionError:
    return _selection_error(
        "launch_selection_capacity_exhausted",
        "Attendee launch-selection capacity is temporarily exhausted.",
        status=503,
        retry="after_delay",
        retry_after_ms=1_000,
    )


def _selection_error(
    code: str,
    message: str,
    *,
    status: int,
    retry: Literal["none", "same_request", "after_delay"] = "none",
    retry_after_ms: int | None = None,
) -> LaunchSelectionError:
    return LaunchSelectionError(
        code=code,
        message=message,
        status=status,
        retry=retry,
        retry_after_ms=retry_after_ms,
    )


def _product_authority_denied() -> ProductLifecycleError:
    return _error(
        "authority_denied",
        "The requested product resource is unavailable.",
        status=403,
    )


__all__ = [
    "AttendeeBffAuthoritySeed",
    "AttendeeCatalogEligibilityProof",
    "AttendeeContextPolicy",
    "AttendeeSelectionCapacityError",
    "AttendeeSessionCoreService",
    "PublicAttendeeSessionService",
    "mint_attendee_catalog_eligibility_proof",
]
