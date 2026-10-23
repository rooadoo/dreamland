"""Protocol-neutral, integration-only product session lifecycle.

This module deliberately has no dependency on DREAMLAND's SSH, admin, profile,
provider, tracing, event, or enrichment runtimes.  It owns only the bounded,
in-memory Phase 1 lifecycle preview.  Normal DREAMLAND startup does not import
this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice
from types import MappingProxyType
from typing import TypeVar, cast

_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LIMIT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUEST_ID_PATTERN = re.compile(r"^request:[A-Za-z0-9_-]{8,128}$")
MAX_IMMUTABLE_REVISION_BYTES = 4 * 1024 * 1024
_MAX_REVISION_BYTES = MAX_IMMUTABLE_REVISION_BYTES
_MAX_LIMIT_ENTRIES = 32
_MAX_LIMIT_VALUE = 1_000_000_000
_MAX_IDEMPOTENCY_KEY_CHARS = 128
_MIN_OPAQUE_VALUE_CHARS = 16
_MAX_OPAQUE_VALUE_CHARS = 512
_MAX_TOKEN_GENERATION_ATTEMPTS = 16
_SeedT = TypeVar("_SeedT")


class ProductRole(StrEnum):
    """Authority roles supported by the shared lifecycle substrate."""

    ATTENDEE = "attendee"
    PRESENTER = "presenter"


class ProductOperation(StrEnum):
    """Product operation families used by grants and idempotency scopes."""

    CREATE = "session.create"
    CURRENT = "session.current"
    SNAPSHOT = "session.snapshot"
    SUBMIT = "command.submit"
    CANCEL = "command.cancel"
    RESET = "session.reset"
    SWITCH = "session.switch"
    CLOSE = "session.close"


@dataclass(frozen=True, slots=True)
class ImmutableRevisionSeed:
    """One explicit immutable revision supplied by trusted startup composition."""

    revision_id: str
    document: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ActorAuthoritySeed:
    """One opaque actor-context authority supplied at service startup."""

    token: str = field(repr=False)
    actor_id: str = ""
    role: ProductRole = ProductRole.ATTENDEE
    expires_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class LaunchGrantSeed:
    """Narrow launch authority bound to one actor and immutable revision."""

    token: str = field(repr=False)
    selected_record_id: str = ""
    actor_id: str = ""
    role: ProductRole = ProductRole.ATTENDEE
    revision_id: str = ""
    expires_at_ms: int = 0
    allowed_operations: frozenset[ProductOperation] = field(
        default_factory=lambda: frozenset({ProductOperation.CREATE})
    )
    limits: Mapping[str, int] = field(default_factory=dict)
    policy_id: str = "preview-policy-v1"
    notice_version: str = "preview-notice-v1"


@dataclass(frozen=True, slots=True)
class LifecyclePolicy:
    """Bounded process-local retention and expiry settings."""

    absolute_ttl_ms: int = 60 * 60 * 1000
    idle_ttl_ms: int = 20 * 60 * 1000
    tombstone_ttl_ms: int = 10 * 60 * 1000
    failed_operation_ttl_ms: int = 5 * 60 * 1000
    max_active_sessions: int = 64
    max_tombstones: int = 128
    max_idempotency_records: int = 512
    max_launch_grants: int = 256
    max_actor_authorities: int = 256
    max_revisions: int = 64

    def __post_init__(self) -> None:
        for name in (
            "absolute_ttl_ms",
            "idle_ttl_ms",
            "tombstone_ttl_ms",
            "failed_operation_ttl_ms",
            "max_active_sessions",
            "max_tombstones",
            "max_idempotency_records",
            "max_launch_grants",
            "max_actor_authorities",
            "max_revisions",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_tombstones < self.max_active_sessions:
            raise ValueError("max_tombstones must reserve one slot per active session")
        if self.max_idempotency_records < 2 * self.max_active_sessions:
            raise ValueError(
                "max_idempotency_records must reserve create and close outcomes"
            )


class ProductLifecycleError(RuntimeError):
    """Stable product error containing only transport-safe fields."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status: int,
        retry: str = "none",
        retry_after_ms: int | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        if not _ERROR_CODE_PATTERN.fullmatch(code):
            raise ValueError("product error code is invalid")
        if (
            not isinstance(message, str)
            or not message
            or len(message) > 160
            or any(
                ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
                for character in message
            )
        ):
            raise ValueError("product error message is invalid")
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 400 <= status <= 599
        ):
            raise ValueError("product error status is invalid")
        if retry not in {
            "none",
            "same_request",
            "resume_snapshot",
            "resume_xray_snapshot",
            "after_delay",
        }:
            raise ValueError("product error retry category is invalid")
        if retry == "after_delay":
            if (
                isinstance(retry_after_ms, bool)
                or not isinstance(retry_after_ms, int)
                or not 1 <= retry_after_ms <= 60_000
            ):
                raise ValueError("after_delay errors require retry_after_ms")
        elif retry_after_ms is not None:
            raise ValueError("retry_after_ms is valid only for after_delay errors")
        safe_context = dict(context or {})
        if len(safe_context) > 8 or any(
            not _ERROR_CODE_PATTERN.fullmatch(name)
            or not _is_safe_error_context_value(value)
            for name, value in safe_context.items()
        ):
            raise ValueError("product error context is invalid")
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry = retry
        self.retry_after_ms = retry_after_ms
        self.context = safe_context

    def to_problem(
        self, *, request_id: str = "request:product-lifecycle"
    ) -> dict[str, object]:
        """Return the bounded private safe-error envelope."""
        safe_request_id = (
            request_id
            if isinstance(request_id, str) and _REQUEST_ID_PATTERN.fullmatch(request_id)
            else "request:product-lifecycle"
        )
        problem: dict[str, object] = {
            "error": {
                "code": self.code,
                "message": self.message,
                "retry": self.retry,
                "retry_after_ms": self.retry_after_ms,
                "request_id": safe_request_id,
            }
        }
        if self.context:
            cast(dict[str, object], problem["error"])["context"] = dict(self.context)
        return problem


@dataclass(frozen=True, slots=True)
class _RevisionRecord:
    revision_id: str
    sha256: str
    canonical_json: bytes = field(repr=False)

    def allocate(self) -> dict[str, object]:
        document = json.loads(self.canonical_json)
        if not isinstance(document, dict):  # Defensive: construction enforces this.
            raise RuntimeError("immutable revision is not a JSON object")
        return document


@dataclass(frozen=True, slots=True)
class _ActorAuthorityRecord:
    actor_id: str
    role: ProductRole
    expires_at_ms: int
    expires_deadline_ms: int
    service_epoch: str

    @property
    def key(self) -> tuple[str, ProductRole]:
        return self.actor_id, self.role


@dataclass(slots=True)
class _LaunchGrantRecord:
    selected_record_id: str
    actor_id: str
    role: ProductRole
    revision_id: str
    expires_at_ms: int
    expires_deadline_ms: int
    allowed_operations: frozenset[ProductOperation]
    limits: Mapping[str, int]
    policy_id: str
    notice_version: str
    binding_sha256: str
    service_epoch: str
    consumed_session_id: str | None = None

    @property
    def actor_key(self) -> tuple[str, ProductRole]:
        return self.actor_id, self.role


@dataclass(slots=True)
class _SessionAuthorityRecord:
    actor_key: tuple[str, ProductRole]
    session_id: str
    reference: str
    expires_deadline_ms: int
    service_epoch: str
    revoked: bool = False


@dataclass(slots=True)
class _SessionRecord:
    session_id: str
    reference: str
    actor_key: tuple[str, ProductRole]
    revision: _RevisionRecord
    runtime_document: dict[str, object] = field(repr=False)
    session_authority: str = field(repr=False)
    session_authority_sha256: str = field(repr=False)
    selected_record_id: str
    launch_grant_sha256: str = field(repr=False)
    grant_binding_sha256: str
    limits: Mapping[str, int]
    policy_id: str
    notice_version: str
    created_at_ms: int
    last_activity_at_ms: int
    absolute_expires_at_ms: int
    idle_expires_at_ms: int
    absolute_deadline_ms: int
    idle_deadline_ms: int
    control_revision: int = 1


@dataclass(frozen=True, slots=True)
class _SessionTombstone:
    session_id: str
    reference: str
    actor_key: tuple[str, ProductRole]
    revision_id: str
    revision_sha256: str
    selected_record_id: str
    launch_grant_sha256: str = field(repr=False)
    grant_binding_sha256: str
    limits: Mapping[str, int]
    policy_id: str
    notice_version: str
    created_at_ms: int
    last_activity_at_ms: int
    absolute_expires_at_ms: int
    idle_expires_at_ms: int
    terminal_at_ms: int
    terminal_reason: str
    control_revision: int
    retain_until_ms: int
    retain_until_deadline_ms: int
    session_authority_sha256: str = field(repr=False)
    generated_value_sha256s: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _RecordedError:
    code: str
    message: str
    status: int
    retry: str
    retry_after_ms: int | None
    context: Mapping[str, str]

    @classmethod
    def capture(cls, error: ProductLifecycleError) -> _RecordedError:
        return cls(
            code=error.code,
            message=error.message,
            status=error.status,
            retry=error.retry,
            retry_after_ms=error.retry_after_ms,
            context=dict(error.context),
        )

    def restore(self) -> ProductLifecycleError:
        return ProductLifecycleError(
            code=self.code,
            message=self.message,
            status=self.status,
            retry=self.retry,
            retry_after_ms=self.retry_after_ms,
            context=self.context,
        )


@dataclass(slots=True)
class _IdempotencyRecord:
    body_sha256: str = field(repr=False)
    result: dict[str, object] | None = field(repr=False)
    error: _RecordedError | None
    retain_until_deadline_ms: int
    session_id: str | None = None


class ProductLifecycleService:
    """Thread-safe, in-memory Phase 1 product lifecycle preview."""

    def __init__(
        self,
        *,
        revisions: Iterable[ImmutableRevisionSeed],
        actors: Iterable[ActorAuthoritySeed],
        launch_grants: Iterable[LaunchGrantSeed],
        policy: LifecyclePolicy | None = None,
        clock_ms: Callable[[], int] | None = None,
        monotonic_ms: Callable[[], int] | None = None,
        token_factory: Callable[[str], str] | None = None,
        launch_operation_families: frozenset[ProductOperation] | None = None,
        allow_empty_revisions: bool = False,
    ) -> None:
        if not isinstance(allow_empty_revisions, bool):
            raise ValueError("allow_empty_revisions must be boolean")
        self._policy = policy or LifecyclePolicy()
        self._utc_clock_ms = clock_ms or _system_clock_ms
        self._monotonic_clock_ms = monotonic_ms or (
            clock_ms if clock_ms is not None else _system_monotonic_ms
        )
        self._token_factory = token_factory or _secure_token
        self._launch_operation_families = (
            launch_operation_families
            if launch_operation_families is not None
            else frozenset({ProductOperation.CREATE})
        )
        if (
            not self._launch_operation_families
            or ProductOperation.CREATE not in self._launch_operation_families
            or not all(
                isinstance(operation, ProductOperation)
                for operation in self._launch_operation_families
            )
        ):
            raise ValueError("launch operation families must include session.create")
        self._lock = threading.RLock()
        self._used_value_digests: set[str] = set()
        revision_seeds = _bounded_seed_tuple(
            revisions,
            maximum=self._policy.max_revisions,
            capacity_error="immutable revision seeds exceed the configured capacity",
        )
        self._revisions = _build_revisions(
            revision_seeds,
            allow_empty=allow_empty_revisions,
        )
        actor_seeds = _bounded_seed_tuple(
            actors,
            maximum=self._policy.max_actor_authorities,
            capacity_error="actor authority seeds exceed the configured capacity",
        )
        launch_seeds = _bounded_seed_tuple(
            launch_grants,
            maximum=self._policy.max_launch_grants,
            capacity_error="launch grant seeds exceed the configured capacity",
        )
        self._reserve_seed_tokens(actor_seeds, launch_seeds)
        self._service_epoch = self._new_opaque_value("service_epoch")
        self._started_at_ms, self._started_deadline_ms = self._now()
        self._last_deadline_ms = self._started_deadline_ms
        self._actors = self._build_actors(actor_seeds)
        self._launch_grants = self._build_launch_grants(launch_seeds)
        self._session_authorities: dict[str, _SessionAuthorityRecord] = {}
        self._sessions_by_reference: dict[str, _SessionRecord] = {}
        self._sessions_by_id: dict[str, _SessionRecord] = {}
        self._tombstones_by_reference: dict[str, _SessionTombstone] = {}
        self._current_by_actor: dict[tuple[str, ProductRole], str] = {}
        self._idempotency: dict[
            tuple[str, tuple[str, ProductRole], ProductOperation, str],
            _IdempotencyRecord,
        ] = {}

    @property
    def service_epoch(self) -> str:
        return self._service_epoch

    def create(
        self,
        *,
        actor_authority: str,
        launch_grant: str,
        expected_epoch: str | None,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Create one exact immutable-revision session, or replay its result."""
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            now_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms)
            self._expire_and_prune_locked(deadline_ms)
            key = _require_idempotency_key(idempotency_key)
            if not _is_bounded_visible_ascii(launch_grant):
                raise _authority_denied()
            launch_sha256 = _credential_sha256(launch_grant)
            body_sha256 = _canonical_sha256(
                {
                    "launch_grant_sha256": launch_sha256,
                }
            )
            scope = self._idempotency_scope(actor.key, ProductOperation.CREATE, key)
            replay = self._idempotency_replay(scope, body_sha256)
            if replay is not None:
                return replay

            # The lock and this capacity reservation are the claim linearization
            # point.  Every deterministic outcome below is recorded before the
            # lock is released.  A capacity rejection occurs before the claim
            # and therefore deliberately leaves the key reusable.
            self._require_idempotency_capacity(additional_records=1)
            try:
                grant = self._require_launch_grant(
                    launch_sha256=launch_sha256,
                    actor=actor,
                    deadline_ms=deadline_ms,
                )
            except ProductLifecycleError as authority_error:
                session_id = self._related_launch_session_id(launch_sha256)
                retain_until_deadline_ms, retained_session_id = self._failure_retention(
                    deadline_ms=deadline_ms,
                    session_id=session_id,
                )
                self._record_idempotent_error(
                    scope,
                    body_sha256,
                    authority_error,
                    retain_until_deadline_ms=retain_until_deadline_ms,
                    session_id=retained_session_id,
                )
                raise
            current_reference = self._current_by_actor.get(actor.key)
            current_session = (
                self._sessions_by_reference.get(current_reference)
                if current_reference is not None
                else None
            )
            if current_session is not None:
                error = _error(
                    "current_session_exists",
                    "A current product session already exists.",
                    status=409,
                    retry="resume_snapshot",
                )
                self._record_idempotent_error(
                    scope,
                    body_sha256,
                    error,
                    retain_until_deadline_ms=(
                        current_session.absolute_deadline_ms
                        + self._policy.tombstone_ttl_ms
                    ),
                    session_id=current_session.session_id,
                )
                raise error

            self._require_new_session_capacity()

            revision = self._revisions[grant.revision_id]
            runtime_document = revision.allocate()
            session_id, reference, session_authority = self._new_session_values()
            session_authority_sha256 = _credential_sha256(session_authority)
            absolute_ttl_ms = _effective_lifecycle_ttl(
                grant.limits,
                name="absolute_ttl_ms",
                policy_ttl_ms=self._policy.absolute_ttl_ms,
            )
            idle_ttl_ms = _effective_lifecycle_ttl(
                grant.limits,
                name="idle_ttl_ms",
                policy_ttl_ms=self._policy.idle_ttl_ms,
            )
            absolute_deadline_ms = min(
                deadline_ms + absolute_ttl_ms,
                grant.expires_deadline_ms,
            )
            idle_deadline_ms = min(
                deadline_ms + idle_ttl_ms,
                absolute_deadline_ms,
            )
            absolute_expires_at_ms = now_ms + (absolute_deadline_ms - deadline_ms)
            idle_expires_at_ms = now_ms + (idle_deadline_ms - deadline_ms)
            session = _SessionRecord(
                session_id=session_id,
                reference=reference,
                actor_key=actor.key,
                revision=revision,
                runtime_document=runtime_document,
                session_authority=session_authority,
                session_authority_sha256=session_authority_sha256,
                selected_record_id=grant.selected_record_id,
                launch_grant_sha256=launch_sha256,
                grant_binding_sha256=grant.binding_sha256,
                limits=grant.limits,
                policy_id=grant.policy_id,
                notice_version=grant.notice_version,
                created_at_ms=now_ms,
                last_activity_at_ms=now_ms,
                absolute_expires_at_ms=absolute_expires_at_ms,
                idle_expires_at_ms=idle_expires_at_ms,
                absolute_deadline_ms=absolute_deadline_ms,
                idle_deadline_ms=idle_deadline_ms,
            )
            authority_record = _SessionAuthorityRecord(
                actor_key=actor.key,
                session_id=session_id,
                reference=reference,
                expires_deadline_ms=absolute_deadline_ms,
                service_epoch=self._service_epoch,
            )
            self._sessions_by_reference[reference] = session
            self._sessions_by_id[session_id] = session
            self._session_authorities[session_authority_sha256] = authority_record
            self._current_by_actor[actor.key] = reference
            grant.consumed_session_id = session_id
            result = {
                "kind": "session_created",
                "service_epoch": self._service_epoch,
                "replayed": False,
                "session_authority": session_authority,
                "snapshot": self._active_snapshot(session),
            }
            self._idempotency[scope] = _IdempotencyRecord(
                body_sha256=body_sha256,
                result=deepcopy(result),
                error=None,
                retain_until_deadline_ms=(
                    absolute_deadline_ms + self._policy.tombstone_ttl_ms
                ),
                session_id=session_id,
            )
            return deepcopy(result)

    def current(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
    ) -> dict[str, object]:
        """Return the actor-role current binding without ever allocating one."""
        with self._lock:
            self._require_epoch(expected_epoch, mutation=False)
            _now_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms)
            self._expire_and_prune_locked(deadline_ms)
            reference = self._current_by_actor.get(actor.key)
            if reference is None:
                return {
                    "kind": "no_current_session",
                    "service_epoch": self._service_epoch,
                }
            session = self._sessions_by_reference.get(reference)
            if session is not None:
                return {
                    "kind": "current_session",
                    "service_epoch": self._service_epoch,
                    "session_authority": session.session_authority,
                    "snapshot": self._active_snapshot(session),
                }
            tombstone = self._tombstones_by_reference.get(reference)
            if tombstone is not None:
                return {
                    "kind": "current_session_terminal",
                    "service_epoch": self._service_epoch,
                    "snapshot": self._terminal_snapshot(tombstone),
                }
            self._current_by_actor.pop(actor.key, None)
            return {
                "kind": "no_current_session",
                "service_epoch": self._service_epoch,
            }

    def snapshot(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
    ) -> dict[str, object]:
        """Return the bounded lifecycle-only projection for one active session."""
        with self._lock:
            self._require_epoch(expected_epoch, mutation=False)
            _now_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms)
            self._expire_and_prune_locked(deadline_ms)
            session = self._require_live_session(
                actor=actor,
                session_authority=session_authority,
                reference=reference,
                deadline_ms=deadline_ms,
            )
            return {
                "kind": "session_snapshot",
                "service_epoch": self._service_epoch,
                "snapshot": self._active_snapshot(session),
            }

    def close(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        idempotency_key: str,
        expected_control_revision: int,
    ) -> dict[str, object]:
        """Atomically close a session and retain only safe recovery state."""
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            now_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms)
            self._expire_and_prune_locked(deadline_ms)
            key = _require_idempotency_key(idempotency_key)
            if (
                isinstance(expected_control_revision, bool)
                or not isinstance(expected_control_revision, int)
                or expected_control_revision <= 0
            ):
                raise _error(
                    "invalid_request",
                    "The product lifecycle request is invalid.",
                    status=400,
                )
            if not _is_bounded_visible_ascii(reference):
                raise _authority_denied()
            if not _is_bounded_visible_ascii(session_authority):
                raise _authority_denied()
            session_authority_sha256 = _credential_sha256(session_authority)
            body_sha256 = _canonical_sha256(
                {
                    "expected_control_revision": expected_control_revision,
                    "reference": reference,
                    "session_authority_sha256": session_authority_sha256,
                }
            )
            scope = self._idempotency_scope(actor.key, ProductOperation.CLOSE, key)
            replay = self._idempotency_replay(scope, body_sha256)
            if replay is not None:
                return replay

            # A close may consume the slot reserved for the active session's
            # terminal outcome.  Deterministic failures may consume only spare
            # capacity, so a full ledger remains retryable instead of stranding
            # the session behind a recorded rejection.
            self._require_close_capacity()
            try:
                session = self._require_live_session(
                    actor=actor,
                    session_authority=session_authority,
                    reference=reference,
                    deadline_ms=deadline_ms,
                )
            except ProductLifecycleError as authority_error:
                self._require_idempotency_capacity(additional_records=1)
                session_id = self._related_close_session_id(
                    session_authority_sha256=session_authority_sha256,
                )
                retain_until_deadline_ms, retained_session_id = self._failure_retention(
                    deadline_ms=deadline_ms,
                    session_id=session_id,
                )
                self._record_idempotent_error(
                    scope,
                    body_sha256,
                    authority_error,
                    retain_until_deadline_ms=retain_until_deadline_ms,
                    session_id=retained_session_id,
                )
                raise
            if session.control_revision != expected_control_revision:
                self._require_idempotency_capacity(additional_records=1)
                error = _error(
                    "control_revision_conflict",
                    "The product session control revision changed.",
                    status=409,
                    retry="resume_snapshot",
                )
                self._record_idempotent_error(
                    scope,
                    body_sha256,
                    error,
                    retain_until_deadline_ms=(
                        session.absolute_deadline_ms + self._policy.tombstone_ttl_ms
                    ),
                    session_id=session.session_id,
                )
                raise error
            tombstone = self._terminalize_locked(
                session,
                terminal_at_ms=now_ms,
                terminal_deadline_ms=deadline_ms,
                terminal_reason="closed",
            )
            result = {
                "kind": "session_closed",
                "service_epoch": self._service_epoch,
                "replayed": False,
                "snapshot": self._terminal_snapshot(tombstone),
            }
            self._idempotency[scope] = _IdempotencyRecord(
                body_sha256=body_sha256,
                result=deepcopy(result),
                error=None,
                retain_until_deadline_ms=tombstone.retain_until_deadline_ms,
                session_id=session.session_id,
            )
            return deepcopy(result)

    def expire_due(self) -> int:
        """Lazily expire due sessions; no timer or worker calls this implicitly."""
        with self._lock:
            _now_ms, deadline_ms = self._now()
            return self._expire_and_prune_locked(deadline_ms)

    def _reserve_seed_tokens(
        self,
        actors: tuple[ActorAuthoritySeed, ...],
        launch_grants: tuple[LaunchGrantSeed, ...],
    ) -> None:
        for token in (
            *(seed.token for seed in actors),
            *(seed.token for seed in launch_grants),
        ):
            _require_seed_token(token)
            digest = _credential_sha256(token)
            if digest in self._used_value_digests:
                raise ValueError("product authority tokens must be unique")
            self._used_value_digests.add(digest)

    def _build_actors(
        self,
        seeds: tuple[ActorAuthoritySeed, ...],
    ) -> dict[str, _ActorAuthorityRecord]:
        actors: dict[str, _ActorAuthorityRecord] = {}
        actor_keys: set[tuple[str, ProductRole]] = set()
        for seed in seeds:
            _require_identity("actor_id", seed.actor_id)
            _require_role(seed.role)
            _require_timestamp("actor expires_at_ms", seed.expires_at_ms)
            key = (seed.actor_id, seed.role)
            if key in actor_keys:
                raise ValueError("actor-role authority seeds must be unique")
            actor_keys.add(key)
            actors[_credential_sha256(seed.token)] = _ActorAuthorityRecord(
                actor_id=seed.actor_id,
                role=seed.role,
                expires_at_ms=seed.expires_at_ms,
                expires_deadline_ms=self._deadline_from_projection(seed.expires_at_ms),
                service_epoch=self._service_epoch,
            )
        return actors

    def _build_launch_grants(
        self,
        seeds: tuple[LaunchGrantSeed, ...],
    ) -> dict[str, _LaunchGrantRecord]:
        grants: dict[str, _LaunchGrantRecord] = {}
        actor_keys = {actor.key for actor in self._actors.values()}
        for seed in seeds:
            # Preserve validation ordering before converting the projected
            # wall-clock expiry into the service's monotonic deadline.
            _require_timestamp("launch grant expires_at_ms", seed.expires_at_ms)
            grants[_credential_sha256(seed.token)] = _build_launch_grant_record(
                seed,
                actor_keys=actor_keys,
                revision_ids=frozenset(self._revisions),
                launch_operation_families=self._launch_operation_families,
                service_epoch=self._service_epoch,
                expires_deadline_ms=self._deadline_from_projection(seed.expires_at_ms),
            )
        return grants

    def _require_epoch(self, expected_epoch: str | None, *, mutation: bool) -> None:
        if not isinstance(expected_epoch, str) or not expected_epoch:
            raise _error(
                "expected_epoch_required",
                "Expected product service epoch evidence is required.",
                status=400,
                retry="none",
            )
        epoch_matches = _is_bounded_visible_ascii(
            expected_epoch
        ) and secrets.compare_digest(
            expected_epoch,
            self._service_epoch,
        )
        if not epoch_matches:
            if mutation:
                raise _error(
                    "outcome_unknown",
                    "The prior product mutation outcome is unavailable after restart.",
                    status=409,
                    retry="none",
                )
            raise _error(
                "session_lost",
                "The product session was lost after service restart.",
                status=409,
                retry="none",
            )

    def _require_actor(
        self,
        actor_authority: str,
        deadline_ms: int,
    ) -> _ActorAuthorityRecord:
        digest = _credential_sha256(actor_authority)
        actor = self._actors.get(digest)
        if (
            actor is None
            or actor.service_epoch != self._service_epoch
            or deadline_ms >= actor.expires_deadline_ms
        ):
            raise _authority_denied()
        return actor

    def _require_launch_grant(
        self,
        *,
        launch_sha256: str,
        actor: _ActorAuthorityRecord,
        deadline_ms: int,
        operation: ProductOperation = ProductOperation.CREATE,
    ) -> _LaunchGrantRecord:
        grant = self._launch_grants.get(launch_sha256)
        if (
            grant is None
            or grant.service_epoch != self._service_epoch
            or grant.actor_key != actor.key
            or operation not in grant.allowed_operations
            or deadline_ms >= grant.expires_deadline_ms
            or grant.consumed_session_id is not None
        ):
            raise _authority_denied()
        return grant

    def _require_live_session(
        self,
        *,
        actor: _ActorAuthorityRecord,
        session_authority: str,
        reference: str,
        deadline_ms: int,
    ) -> _SessionRecord:
        if not _is_bounded_visible_ascii(reference):
            raise _authority_denied()
        authority = self._session_authorities.get(_credential_sha256(session_authority))
        if (
            authority is None
            or authority.revoked
            or authority.service_epoch != self._service_epoch
            or authority.actor_key != actor.key
            or authority.reference != reference
            or deadline_ms >= authority.expires_deadline_ms
        ):
            raise _authority_denied()
        session = self._sessions_by_reference.get(reference)
        if session is None or session.session_id != authority.session_id:
            raise _authority_denied()
        return session

    def _idempotency_scope(
        self,
        actor_key: tuple[str, ProductRole],
        operation: ProductOperation,
        idempotency_key: str,
    ) -> tuple[str, tuple[str, ProductRole], ProductOperation, str]:
        return self._service_epoch, actor_key, operation, idempotency_key

    def _idempotency_replay(
        self,
        scope: tuple[str, tuple[str, ProductRole], ProductOperation, str],
        body_sha256: str,
    ) -> dict[str, object] | None:
        record = self._idempotency.get(scope)
        if record is None:
            return None
        if record.body_sha256 != body_sha256:
            raise _error(
                "idempotency_conflict",
                "The idempotency key was already used for another request.",
                status=409,
                retry="none",
            )
        if record.error is not None:
            raise record.error.restore()
        if record.result is None:  # Defensive: records always have one outcome.
            raise RuntimeError("idempotency record has no outcome")
        result = deepcopy(record.result)
        result["replayed"] = True
        return result

    def _record_idempotent_error(
        self,
        scope: tuple[str, tuple[str, ProductRole], ProductOperation, str],
        body_sha256: str,
        error: ProductLifecycleError,
        *,
        retain_until_deadline_ms: int,
        session_id: str | None = None,
    ) -> None:
        self._idempotency[scope] = _IdempotencyRecord(
            body_sha256=body_sha256,
            result=None,
            error=_RecordedError.capture(error),
            retain_until_deadline_ms=retain_until_deadline_ms,
            session_id=session_id,
        )

    def _related_launch_session_id(self, launch_sha256: str) -> str | None:
        grant = self._launch_grants.get(launch_sha256)
        if grant is None or grant.service_epoch != self._service_epoch:
            return None
        return grant.consumed_session_id

    def _related_close_session_id(
        self,
        *,
        session_authority_sha256: str,
    ) -> str | None:
        # Only an unguessable authority can establish the internal retention
        # relationship.  A caller-supplied reference alone must not become an
        # existence or lifecycle oracle.
        authority = self._session_authorities.get(session_authority_sha256)
        if authority is not None and authority.service_epoch == self._service_epoch:
            return authority.session_id
        return None

    def _failure_retention(
        self,
        *,
        deadline_ms: int,
        session_id: str | None,
    ) -> tuple[int, str | None]:
        if session_id is not None:
            session = self._sessions_by_id.get(session_id)
            if session is not None:
                return (
                    session.absolute_deadline_ms + self._policy.tombstone_ttl_ms,
                    session_id,
                )
            for tombstone in self._tombstones_by_reference.values():
                if tombstone.session_id == session_id:
                    return tombstone.retain_until_deadline_ms, session_id
        return deadline_ms + self._policy.failed_operation_ttl_ms, None

    def _require_idempotency_capacity(self, *, additional_records: int) -> None:
        if len(self._idempotency) + additional_records > (
            self._policy.max_idempotency_records - len(self._sessions_by_reference)
        ):
            raise _capacity_exhausted()

    def _require_new_session_capacity(self) -> None:
        active_count = len(self._sessions_by_reference)
        if active_count >= self._policy.max_active_sessions:
            raise _capacity_exhausted()
        if (
            len(self._tombstones_by_reference) + active_count + 1
            > self._policy.max_tombstones
        ):
            raise _capacity_exhausted()
        # One result is written by create and one slot remains reserved for the
        # session's eventual close/expiry reconciliation record.
        if (
            len(self._idempotency) + active_count + 2
            > self._policy.max_idempotency_records
        ):
            raise _capacity_exhausted()

    def _require_close_capacity(self) -> None:
        if len(self._tombstones_by_reference) >= self._policy.max_tombstones:
            raise _capacity_exhausted()
        if len(self._idempotency) + 1 > self._policy.max_idempotency_records:
            raise _capacity_exhausted()

    def _expire_and_prune_locked(self, deadline_ms: int) -> int:
        expired = 0
        for session in tuple(self._sessions_by_reference.values()):
            if session.absolute_deadline_ms <= session.idle_deadline_ms:
                terminal_reason = "absolute_expired"
                terminal_at_ms = session.absolute_expires_at_ms
                terminal_deadline_ms = session.absolute_deadline_ms
            else:
                terminal_reason = "idle_expired"
                terminal_at_ms = session.idle_expires_at_ms
                terminal_deadline_ms = session.idle_deadline_ms
            if deadline_ms < terminal_deadline_ms:
                continue
            self._terminalize_locked(
                session,
                terminal_at_ms=terminal_at_ms,
                terminal_deadline_ms=terminal_deadline_ms,
                terminal_reason=terminal_reason,
            )
            expired += 1
        for reference, tombstone in tuple(self._tombstones_by_reference.items()):
            if deadline_ms < tombstone.retain_until_deadline_ms:
                continue
            self._tombstones_by_reference.pop(reference, None)
            if self._current_by_actor.get(tombstone.actor_key) == reference:
                self._current_by_actor.pop(tombstone.actor_key, None)
            self._session_authorities.pop(tombstone.session_authority_sha256, None)
            for digest in tombstone.generated_value_sha256s:
                self._used_value_digests.discard(digest)
        for scope, record in tuple(self._idempotency.items()):
            if deadline_ms >= record.retain_until_deadline_ms:
                self._idempotency.pop(scope, None)
        return expired

    def _terminalize_locked(
        self,
        session: _SessionRecord,
        *,
        terminal_at_ms: int,
        terminal_deadline_ms: int,
        terminal_reason: str,
    ) -> _SessionTombstone:
        existing = self._tombstones_by_reference.get(session.reference)
        if existing is not None:
            return existing
        session.control_revision += 1
        retain_until_ms = terminal_at_ms + self._policy.tombstone_ttl_ms
        retain_until_deadline_ms = terminal_deadline_ms + self._policy.tombstone_ttl_ms
        tombstone = _SessionTombstone(
            session_id=session.session_id,
            reference=session.reference,
            actor_key=session.actor_key,
            revision_id=session.revision.revision_id,
            revision_sha256=session.revision.sha256,
            selected_record_id=session.selected_record_id,
            launch_grant_sha256=session.launch_grant_sha256,
            grant_binding_sha256=session.grant_binding_sha256,
            limits=session.limits,
            policy_id=session.policy_id,
            notice_version=session.notice_version,
            created_at_ms=session.created_at_ms,
            last_activity_at_ms=session.last_activity_at_ms,
            absolute_expires_at_ms=session.absolute_expires_at_ms,
            idle_expires_at_ms=session.idle_expires_at_ms,
            terminal_at_ms=terminal_at_ms,
            terminal_reason=terminal_reason,
            control_revision=session.control_revision,
            retain_until_ms=retain_until_ms,
            retain_until_deadline_ms=retain_until_deadline_ms,
            session_authority_sha256=session.session_authority_sha256,
            generated_value_sha256s=(
                _credential_sha256(session.session_id),
                _credential_sha256(session.reference),
                session.session_authority_sha256,
            ),
        )
        self._sessions_by_reference.pop(session.reference, None)
        self._sessions_by_id.pop(session.session_id, None)
        authority = self._session_authorities.get(session.session_authority_sha256)
        if authority is not None:
            authority.revoked = True
        self._tombstones_by_reference[session.reference] = tombstone
        if self._current_by_actor.get(session.actor_key) == session.reference:
            self._current_by_actor[session.actor_key] = session.reference
        for record in self._idempotency.values():
            if record.session_id == session.session_id:
                record.retain_until_deadline_ms = retain_until_deadline_ms
        return tombstone

    def _active_snapshot(self, session: _SessionRecord) -> dict[str, object]:
        return {
            "session_id": session.session_id,
            "reference": session.reference,
            "authority_class": session.actor_key[1].value,
            "starting_revision": {
                "selected_record_id": session.selected_record_id,
                "revision_id": session.revision.revision_id,
                "sha256": session.revision.sha256,
            },
            "launch_binding": {
                "sha256": session.grant_binding_sha256,
                "limits": dict(session.limits),
                "policy_id": session.policy_id,
                "notice_version": session.notice_version,
            },
            "coordinates": {
                "control_revision": session.control_revision,
            },
            "available_controls": ["snapshot", "close"],
            "lifecycle": {
                "state": "active",
                "created_at_ms": session.created_at_ms,
                "last_activity_at_ms": session.last_activity_at_ms,
                "absolute_expires_at_ms": session.absolute_expires_at_ms,
                "idle_expires_at_ms": session.idle_expires_at_ms,
            },
            "recovery": {
                "tombstone": False,
                "retain_until_ms": None,
            },
        }

    def _terminal_snapshot(
        self,
        tombstone: _SessionTombstone,
    ) -> dict[str, object]:
        state = (
            tombstone.terminal_reason
            if tombstone.terminal_reason in {"closed", "replaced"}
            else "expired"
        )
        return {
            "session_id": tombstone.session_id,
            "reference": tombstone.reference,
            "authority_class": tombstone.actor_key[1].value,
            "starting_revision": {
                "selected_record_id": tombstone.selected_record_id,
                "revision_id": tombstone.revision_id,
                "sha256": tombstone.revision_sha256,
            },
            "launch_binding": {
                "sha256": tombstone.grant_binding_sha256,
                "limits": dict(tombstone.limits),
                "policy_id": tombstone.policy_id,
                "notice_version": tombstone.notice_version,
            },
            "coordinates": {
                "control_revision": tombstone.control_revision,
            },
            "available_controls": [],
            "lifecycle": {
                "state": state,
                "created_at_ms": tombstone.created_at_ms,
                "last_activity_at_ms": tombstone.last_activity_at_ms,
                "absolute_expires_at_ms": tombstone.absolute_expires_at_ms,
                "idle_expires_at_ms": tombstone.idle_expires_at_ms,
                "terminal_at_ms": tombstone.terminal_at_ms,
                "terminal_reason": tombstone.terminal_reason,
            },
            "recovery": {
                "tombstone": True,
                "retain_until_ms": tombstone.retain_until_ms,
            },
        }

    def _new_opaque_value(self, purpose: str) -> str:
        for _attempt in range(_MAX_TOKEN_GENERATION_ATTEMPTS):
            value = self._token_factory(purpose)
            if not _is_bounded_visible_ascii(value):
                raise ValueError(
                    "token_factory must return a bounded visible-ASCII string"
                )
            digest = _credential_sha256(value)
            if digest in self._used_value_digests:
                continue
            self._used_value_digests.add(digest)
            return value
        raise RuntimeError("token_factory did not produce a unique opaque value")

    def _new_session_values(self) -> tuple[str, str, str]:
        values: list[str] = []
        try:
            for purpose in (
                "product_session_id",
                "product_reference",
                "session_authority",
            ):
                values.append(self._new_opaque_value(purpose))
        except Exception:
            for value in values:
                self._used_value_digests.discard(_credential_sha256(value))
            raise
        return values[0], values[1], values[2]

    def _deadline_from_projection(self, expires_at_ms: int) -> int:
        remaining_ms = max(0, expires_at_ms - self._started_at_ms)
        return self._started_deadline_ms + remaining_ms

    def _now(self) -> tuple[int, int]:
        raw_utc_value = self._utc_clock_ms()
        deadline_value = self._monotonic_clock_ms()
        if (
            isinstance(raw_utc_value, bool)
            or not isinstance(raw_utc_value, int)
            or raw_utc_value < 0
        ):
            raise RuntimeError("clock_ms must return a non-negative integer")
        if (
            isinstance(deadline_value, bool)
            or not isinstance(deadline_value, int)
            or deadline_value < 0
        ):
            raise RuntimeError("monotonic_ms must return a non-negative integer")
        self._last_wall_ms = raw_utc_value
        if not hasattr(self, "_started_deadline_ms"):
            return raw_utc_value, deadline_value
        if deadline_value < self._last_deadline_ms:
            raise RuntimeError("monotonic_ms must not move backward")
        self._last_deadline_ms = deadline_value
        projected_utc_value = self._started_at_ms + (
            deadline_value - self._started_deadline_ms
        )
        return projected_utc_value, deadline_value


def _bounded_seed_tuple(
    values: Iterable[_SeedT],
    *,
    maximum: int,
    capacity_error: str,
) -> tuple[_SeedT, ...]:
    bounded = tuple(islice(values, maximum + 1))
    if len(bounded) > maximum:
        raise ValueError(capacity_error)
    return bounded


def _build_revisions(
    seeds: Iterable[ImmutableRevisionSeed],
    *,
    allow_empty: bool = False,
) -> dict[str, _RevisionRecord]:
    revisions: dict[str, _RevisionRecord] = {}
    for seed in seeds:
        _require_identity("revision_id", seed.revision_id)
        if seed.revision_id in revisions:
            raise ValueError("immutable revision identifiers must be unique")
        if not isinstance(seed.document, Mapping):
            raise ValueError("immutable revision document must be a JSON object")
        _require_string_json_keys(seed.document)
        try:
            canonical = json.dumps(
                seed.document,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (RecursionError, TypeError, ValueError) as exc:
            raise ValueError(
                "immutable revision document must contain JSON values"
            ) from exc
        if len(canonical) > _MAX_REVISION_BYTES:
            raise ValueError("immutable revision document exceeds the size limit")
        parsed = json.loads(canonical)
        if not isinstance(parsed, dict):
            raise ValueError("immutable revision document must be a JSON object")
        revisions[seed.revision_id] = _RevisionRecord(
            revision_id=seed.revision_id,
            sha256=hashlib.sha256(canonical).hexdigest(),
            canonical_json=canonical,
        )
    if not revisions and not allow_empty:
        raise ValueError("at least one immutable revision seed is required")
    return revisions


def _build_launch_grant_record(
    seed: LaunchGrantSeed,
    *,
    actor_keys: set[tuple[str, ProductRole]],
    revision_ids: frozenset[str],
    launch_operation_families: frozenset[ProductOperation],
    service_epoch: str,
    expires_deadline_ms: int,
) -> _LaunchGrantRecord:
    """Validate and detach one grant without mutating a service registry."""
    _require_identity("selected_record_id", seed.selected_record_id)
    _require_identity("actor_id", seed.actor_id)
    _require_role(seed.role)
    _require_identity("revision_id", seed.revision_id)
    _require_timestamp("launch grant expires_at_ms", seed.expires_at_ms)
    _require_safe_label("policy_id", seed.policy_id)
    _require_safe_label("notice_version", seed.notice_version)
    if seed.revision_id not in revision_ids:
        raise ValueError("launch grant names an unknown revision")
    if (seed.actor_id, seed.role) not in actor_keys:
        raise ValueError("launch grant names an unknown actor-role authority")
    if not seed.allowed_operations or not seed.allowed_operations.issubset(
        launch_operation_families
    ):
        if launch_operation_families == frozenset({ProductOperation.CREATE}):
            raise ValueError("Phase 1 launch grants must allow only session.create")
        raise ValueError("launch grant operations exceed the configured families")
    limits = _safe_limits(seed.limits)
    _validate_lifecycle_limits(limits)
    binding = {
        "actor_id": seed.actor_id,
        "allowed_operations": sorted(
            operation.value for operation in seed.allowed_operations
        ),
        "expires_at_ms": seed.expires_at_ms,
        "limits": dict(limits),
        "notice_version": seed.notice_version,
        "policy_id": seed.policy_id,
        "revision_id": seed.revision_id,
        "role": seed.role.value,
        "selected_record_id": seed.selected_record_id,
    }
    return _LaunchGrantRecord(
        selected_record_id=seed.selected_record_id,
        actor_id=seed.actor_id,
        role=seed.role,
        revision_id=seed.revision_id,
        expires_at_ms=seed.expires_at_ms,
        expires_deadline_ms=expires_deadline_ms,
        allowed_operations=seed.allowed_operations,
        limits=limits,
        policy_id=seed.policy_id,
        notice_version=seed.notice_version,
        binding_sha256=_canonical_sha256(binding),
        service_epoch=service_epoch,
    )


def _require_string_json_keys(document: Mapping[str, object]) -> None:
    pending: list[object] = [document]
    visited_containers: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in visited_containers:
                continue
            visited_containers.add(identity)
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ValueError(
                        "immutable revision JSON object keys must be strings"
                    )
                pending.append(nested)
        elif isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in visited_containers:
                continue
            visited_containers.add(identity)
            pending.extend(value)


def _safe_limits(value: Mapping[str, int]) -> Mapping[str, int]:
    if not isinstance(value, Mapping) or len(value) > _MAX_LIMIT_ENTRIES:
        raise ValueError("launch grant limits are invalid")
    limits: dict[str, int] = {}
    for name, limit in value.items():
        if not isinstance(name, str) or not _LIMIT_NAME_PATTERN.fullmatch(name):
            raise ValueError("launch grant limit name is invalid")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 0
            or limit > _MAX_LIMIT_VALUE
        ):
            raise ValueError("launch grant limit value is invalid")
        limits[name] = limit
    return MappingProxyType(dict(sorted(limits.items())))


def _validate_lifecycle_limits(limits: Mapping[str, int]) -> None:
    absolute_ttl_ms = limits.get("absolute_ttl_ms")
    idle_ttl_ms = limits.get("idle_ttl_ms")
    for name, value in (
        ("absolute_ttl_ms", absolute_ttl_ms),
        ("idle_ttl_ms", idle_ttl_ms),
    ):
        if value is not None and value <= 0:
            raise ValueError(f"launch grant {name} must be positive")
    if (
        absolute_ttl_ms is not None
        and idle_ttl_ms is not None
        and idle_ttl_ms > absolute_ttl_ms
    ):
        raise ValueError("launch grant idle_ttl_ms cannot exceed absolute_ttl_ms")


def _effective_lifecycle_ttl(
    limits: Mapping[str, int],
    *,
    name: str,
    policy_ttl_ms: int,
) -> int:
    configured = limits.get(name)
    return min(policy_ttl_ms, configured) if configured is not None else policy_ttl_ms


def _canonical_sha256(value: object) -> str:
    canonical = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _credential_sha256(value: object) -> str:
    text = value if isinstance(value, str) and _is_bounded_visible_ascii(value) else ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_bounded_visible_ascii(value: object) -> bool:
    return (
        isinstance(value, str)
        and _MIN_OPAQUE_VALUE_CHARS <= len(value) <= _MAX_OPAQUE_VALUE_CHARS
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _is_safe_error_context_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 512
        and all(
            ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F
            for character in value
        )
    )


def _require_seed_token(value: str) -> None:
    if not isinstance(value, str) or not (
        _MIN_OPAQUE_VALUE_CHARS <= len(value) <= _MAX_OPAQUE_VALUE_CHARS
    ):
        raise ValueError("seed authority tokens must contain 16 to 512 characters")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise ValueError("seed authority tokens must contain visible ASCII")


def _require_identity(name: str, value: str) -> None:
    if not isinstance(value, str) or not _IDENTITY_PATTERN.fullmatch(value):
        raise ValueError(f"{name} is invalid")


def _require_safe_label(name: str, value: str) -> None:
    if not isinstance(value, str) or not _IDENTITY_PATTERN.fullmatch(value):
        raise ValueError(f"{name} is invalid")


def _require_role(role: ProductRole) -> None:
    if not isinstance(role, ProductRole):
        raise ValueError("product role is invalid")


def _require_timestamp(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_idempotency_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_IDEMPOTENCY_KEY_CHARS
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise _error(
            "invalid_request",
            "The product lifecycle request is invalid.",
            status=400,
        )
    return value


def _authority_denied() -> ProductLifecycleError:
    return _error(
        "authority_denied",
        "The product authority does not permit this operation.",
        status=403,
        retry="none",
    )


def _capacity_exhausted() -> ProductLifecycleError:
    return ProductLifecycleError(
        code="capacity_exhausted",
        message="Product lifecycle preview capacity is temporarily exhausted.",
        status=503,
        retry="after_delay",
        retry_after_ms=1_000,
    )


def _error(
    code: str,
    message: str,
    *,
    status: int,
    retry: str = "none",
) -> ProductLifecycleError:
    return ProductLifecycleError(
        code=code,
        message=message,
        status=status,
        retry=retry,
    )


def _system_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _system_monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _secure_token(purpose: str) -> str:
    return f"{purpose}_{secrets.token_urlsafe(32)}"


__all__ = [
    "ActorAuthoritySeed",
    "ImmutableRevisionSeed",
    "LaunchGrantSeed",
    "LifecyclePolicy",
    "MAX_IMMUTABLE_REVISION_BYTES",
    "ProductLifecycleError",
    "ProductLifecycleService",
    "ProductOperation",
    "ProductRole",
]
