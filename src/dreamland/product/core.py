"""Protocol-neutral Phase 2 product-session command aggregate.

This module extends the Phase 1 lifecycle only when explicitly constructed. It
owns product command acceptance, fencing, recovery projections, journals, and
product-only admission. Normal DREAMLAND startup does not import it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias, cast

from dreamland.product.admission import (
    AdmissionDenialReason,
    ProductAdmissionDenied,
    ProductAdmissionLedger,
    ProductCapacityClass,
    ProductJobCategory,
)
from dreamland.product.commands import (
    MAX_CANONICAL_COMMAND_UTF8_BYTES,
    ProductCommandError,
    classify_product_command,
)
from dreamland.product.enrichment import (
    JsonValue,
    ProductEnrichmentAppliedEvent,
    ProductEnrichmentAttempt,
    ProductEnrichmentLifecycle,
    ProductEnrichmentSessionAccess,
    ProductEnrichmentSessionSnapshot,
    ProductEnrichmentSessionUnavailable,
)
from dreamland.product.execution import (
    ProductCommandExecutor,
    ProductExecutionOutcome,
    ProductExecutionRequest,
    ProductExecutionResult,
    ProductOutputChunk,
    ProductStateEffect,
    ProductStateEffectKind,
)
from dreamland.product.journal import (
    EventCursor,
    EventJournalSnapshot,
    JournalLimits,
    JournalSnapshotRequired,
    ProductEventCriticality,
    ProductSessionJournal,
    TranscriptCursor,
    TranscriptJournalSnapshot,
    TranscriptKind,
)
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductLifecycleService,
    ProductOperation,
    ProductRole,
    _canonical_sha256,
    _capacity_exhausted,
    _credential_sha256,
    _effective_lifecycle_ttl,
    _error,
    _IdempotencyRecord,
    _LaunchGrantRecord,
    _RecordedError,
    _require_idempotency_key,
    _SessionAuthorityRecord,
    _SessionRecord,
    _SessionTombstone,
)

Task: TypeAlias = Callable[[], None]
TaskSubmitter: TypeAlias = Callable[[Task], None]

_MIN_CORE_EVENT_PAYLOAD_BYTES = 512
_MIN_CORE_PAYLOAD_DEPTH = 2
_MIN_CORE_PAYLOAD_ITEMS = 16


class _DispatchState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETURNED = "returned"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class ProductCorePolicy:
    """Bounded command and recovery policy for an explicitly built core."""

    max_commands_per_session: int | None = 128
    command_deadline_ms: int = 30_000
    cancellation_deadline_ms: int = 5_000
    max_output_bytes_per_command: int = 256 * 1024
    max_output_chunks_per_command: int = 256
    max_runtime_document_bytes: int = 4 * 1024 * 1024
    reserved_tokens_per_command: int = 8_192
    reserved_spend_units_per_command: int = 10_000
    max_attempts_per_command: int = 1

    def __post_init__(self) -> None:
        if self.max_commands_per_session is not None:
            _require_positive_bounded_policy_limit(
                self.max_commands_per_session,
                "max_commands_per_session",
            )
        for name in (
            "command_deadline_ms",
            "cancellation_deadline_ms",
            "max_output_bytes_per_command",
            "max_output_chunks_per_command",
            "max_runtime_document_bytes",
            "reserved_tokens_per_command",
            "reserved_spend_units_per_command",
            "max_attempts_per_command",
        ):
            _require_positive_bounded_policy_limit(getattr(self, name), name)
        if self.cancellation_deadline_ms > self.command_deadline_ms:
            raise ValueError(
                "cancellation_deadline_ms cannot exceed command_deadline_ms"
            )


@dataclass(slots=True)
class _CommandRecord:
    session_id: str
    command_id: str
    canonical_command: str = field(repr=False)
    slot_generation: int
    request_id: str
    accepted_at_ms: int
    accepted_deadline_ms: int
    deadline_at_ms: int
    deadline_deadline_ms: int
    checkpoint_namespace: str
    cancel_event: threading.Event = field(repr=False)
    reserved_tokens: int = 0
    reserved_spend_units: int = 0
    max_attempts: int = 1
    base_accepted_state_revision: int = 0
    status: str = "accepted"
    fence_open: bool = True
    cancel_requested_at_ms: int | None = None
    cancel_deadline_ms: int | None = None
    result: dict[str, object] | None = None
    output_bytes: int = 0
    output_chunks: int = 0
    output_clipped: bool = False
    stream_chunk_indices: dict[str, int] = field(
        default_factory=lambda: {"stdout": 0, "stderr": 0}
    )
    terminal_at_ms: int | None = None
    dispatch_state: _DispatchState = _DispatchState.QUEUED
    worker_reconciled: bool = False
    actual_tokens: int | None = None
    actual_spend_units: int | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "interrupted", "failed"}


@dataclass(slots=True)
class _CoreSessionState:
    journal: ProductSessionJournal
    slot_generation: int = 0
    accepted_state_revision: int = 0
    command_count: int = 0
    active_command_id: str | None = None
    latest_command_id: str | None = None
    commands: dict[str, _CommandRecord] = field(default_factory=dict)
    admission_released: bool = False


@dataclass(slots=True)
class _ReplacementClaim:
    body_sha256: str = field(repr=False)
    owner_thread_id: int
    completed: threading.Event = field(default_factory=threading.Event, repr=False)
    transient_error: _RecordedError | None = field(default=None, repr=False)


@dataclass(slots=True)
class _CoreEnrichmentAccess:
    service: ProductSessionCoreService
    session: _SessionRecord
    core: _CoreSessionState
    owner_thread_id: int
    active: bool = True

    @property
    def enrichment_snapshot(self) -> ProductEnrichmentSessionSnapshot:
        self._require_usable()
        return self.service._enrichment_snapshot_locked(self.session, self.core)

    def install_enrichment_document(
        self,
        replacement_document: Mapping[str, object],
        applied_event: ProductEnrichmentAppliedEvent,
    ) -> int:
        self._require_usable()
        return self.service._install_enrichment_document_locked(
            self.session,
            self.core,
            replacement_document,
            applied_event,
        )

    def invalidate(self) -> None:
        self.active = False

    def _require_usable(self) -> None:
        if not self.active or threading.get_ident() != self.owner_thread_id:
            raise RuntimeError(
                "product enrichment access is valid only inside its transaction"
            )


class ProductSessionCoreService(ProductLifecycleService):
    """In-memory shared-core aggregate behind an explicit task/executor seam."""

    def __init__(
        self,
        *,
        revisions: Iterable[ImmutableRevisionSeed],
        actors: Iterable[ActorAuthoritySeed],
        launch_grants: Iterable[LaunchGrantSeed],
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
        allow_empty_revisions: bool = False,
    ) -> None:
        if not callable(getattr(executor, "execute", None)):
            raise TypeError("executor must implement execute")
        if not callable(submit_task):
            raise TypeError("submit_task must be callable")
        self._executor = executor
        self._submit_task = submit_task
        self._core_policy = core_policy or ProductCorePolicy()
        self._journal_limits = journal_limits or JournalLimits()
        if (
            self._journal_limits.max_event_payload_bytes < _MIN_CORE_EVENT_PAYLOAD_BYTES
            or self._journal_limits.max_payload_depth < _MIN_CORE_PAYLOAD_DEPTH
            or self._journal_limits.max_payload_items < _MIN_CORE_PAYLOAD_ITEMS
        ):
            raise ValueError(
                "journal limits cannot represent mandatory product-core events"
            )
        self._admission = admission or ProductAdmissionLedger()
        admission_snapshot = self._admission.snapshot()
        if admission_snapshot.active_sessions or admission_snapshot.logical_jobs:
            raise ValueError("product core requires a dedicated empty admission ledger")
        self._core_sessions: dict[str, _CoreSessionState] = {}
        self._replacement_claims: dict[
            tuple[str, tuple[str, ProductRole], ProductOperation, str],
            _ReplacementClaim,
        ] = {}
        self._validate_replacement = validate_replacement or (lambda _document: None)
        self._validate_state_effect = validate_state_effect or (lambda _document: None)
        policy = lifecycle_policy or LifecyclePolicy()
        if self._admission.limits.max_active_sessions < policy.max_active_sessions:
            raise ValueError(
                "product admission must cover lifecycle active-session capacity"
            )
        super().__init__(
            revisions=revisions,
            actors=actors,
            launch_grants=launch_grants,
            policy=policy,
            clock_ms=clock_ms,
            monotonic_ms=monotonic_ms,
            token_factory=token_factory,
            launch_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            allow_empty_revisions=allow_empty_revisions,
        )
        if any(
            len(revision.canonical_json) > self._core_policy.max_runtime_document_bytes
            for revision in self._revisions.values()
        ):
            raise ValueError(
                "immutable revisions must fit the product runtime document limit"
            )

    @property
    def admission(self) -> ProductAdmissionLedger:
        return self._admission

    def capture_enrichment_snapshot(
        self,
        session_id: str,
    ) -> ProductEnrichmentSessionSnapshot:
        """Capture detached product state for explicitly requested enrichment."""
        with self._lock:
            now_ms, deadline_ms = self._now()
            self._expire_and_prune_locked(deadline_ms)
            self._reconcile_due_commands_locked(
                now_ms=now_ms,
                deadline_ms=deadline_ms,
            )
            session = self._sessions_by_id.get(session_id)
            if session is None:
                raise ProductEnrichmentSessionUnavailable()
            core = self._require_core_session_locked(session.session_id)
            return self._enrichment_snapshot_locked(session, core)

    def transact_enrichment(
        self,
        session_id: str,
        operation: Callable[
            [ProductEnrichmentSessionAccess],
            ProductEnrichmentAttempt,
        ],
    ) -> ProductEnrichmentAttempt:
        """Run one enrichment compare-and-apply under the product-core lock."""
        if not callable(operation):
            raise TypeError("enrichment operation must be callable")
        with self._lock:
            now_ms, deadline_ms = self._now()
            self._expire_and_prune_locked(deadline_ms)
            self._reconcile_due_commands_locked(
                now_ms=now_ms,
                deadline_ms=deadline_ms,
            )
            session = self._sessions_by_id.get(session_id)
            if session is None:
                raise ProductEnrichmentSessionUnavailable()
            core = self._require_core_session_locked(session.session_id)
            access = _CoreEnrichmentAccess(
                service=self,
                session=session,
                core=core,
                owner_thread_id=threading.get_ident(),
            )
            try:
                result = operation(access)
            finally:
                access.invalidate()
            if not isinstance(result, ProductEnrichmentAttempt):
                raise TypeError("enrichment operation returned an invalid result")
            return result

    def create(
        self,
        *,
        actor_authority: str,
        launch_grant: str,
        expected_epoch: str | None,
        idempotency_key: str,
    ) -> dict[str, object]:
        with self._lock:
            result = super().create(
                actor_authority=actor_authority,
                launch_grant=launch_grant,
                expected_epoch=expected_epoch,
                idempotency_key=idempotency_key,
            )
            if bool(result["replayed"]):
                return result
            snapshot = _required_mapping(result["snapshot"], "create snapshot")
            session_id = _required_string(snapshot["session_id"], "session_id")
            reference = _required_string(snapshot["reference"], "reference")
            session = self._sessions_by_reference[reference]
            actor = self._require_actor(actor_authority, self._last_deadline_ms)
            scope = self._idempotency_scope(
                actor.key,
                ProductOperation.CREATE,
                _require_idempotency_key(idempotency_key),
            )
            try:
                core = self._initialize_core_session_locked(session)
                enriched = deepcopy(result)
                enriched["snapshot"] = self._core_active_snapshot_locked(session, core)
                record = self._idempotency[scope]
                record.result = deepcopy(enriched)
                record.session_id = session_id
            except BaseException as initialization_error:
                self._rollback_created_session_locked(
                    session,
                    launch_grant_sha256=_credential_sha256(launch_grant),
                    idempotency_scope=scope,
                )
                if isinstance(initialization_error, ProductAdmissionDenied):
                    raise _admission_error(initialization_error) from None
                raise
            return enriched

    def current(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
    ) -> dict[str, object]:
        with self._lock:
            result = super().current(
                actor_authority=actor_authority,
                expected_epoch=expected_epoch,
            )
            self._reconcile_due_commands_locked()
            if result["kind"] == "current_session":
                snapshot = _required_mapping(result["snapshot"], "current snapshot")
                reference = _required_string(snapshot["reference"], "reference")
                session = self._sessions_by_reference.get(reference)
                if session is not None:
                    core = self._require_core_session_locked(session.session_id)
                    result["snapshot"] = self._core_active_snapshot_locked(
                        session, core
                    )
            elif result["kind"] == "current_session_terminal":
                snapshot = _required_mapping(result["snapshot"], "terminal snapshot")
                reference = _required_string(snapshot["reference"], "reference")
                tombstone = self._tombstones_by_reference.get(reference)
                if tombstone is not None:
                    result["snapshot"] = self._core_terminal_snapshot_locked(tombstone)
            return result

    def snapshot(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
    ) -> dict[str, object]:
        with self._lock:
            result = super().snapshot(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
            )
            self._reconcile_due_commands_locked()
            session = self._sessions_by_reference.get(reference)
            if session is None:
                raise _error(
                    "session_closed",
                    "The product session is no longer active.",
                    status=409,
                    retry="resume_snapshot",
                )
            core = self._require_core_session_locked(session.session_id)
            result["snapshot"] = self._core_active_snapshot_locked(session, core)
            return result

    def submit_command(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        idempotency_key: str,
        expected_slot_generation: int,
        command: str | bytes,
    ) -> dict[str, object]:
        """Atomically accept one command, then dispatch outside the lock."""
        dispatch_command_id: str | None = None
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            now_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms)
            key = _require_idempotency_key(idempotency_key)
            _require_positive_or_zero_generation(expected_slot_generation)
            try:
                canonical = classify_product_command(command)
            except ProductCommandError as command_error:
                raise ProductLifecycleError(
                    code=command_error.code.value,
                    message=command_error.message,
                    status=command_error.status,
                    retry=command_error.retry,
                ) from None
            self._expire_and_prune_locked(deadline_ms)
            self._reconcile_due_commands_locked(
                now_ms=now_ms,
                deadline_ms=deadline_ms,
            )
            body_sha256 = _canonical_sha256(
                {
                    "canonical_command": canonical.canonical_text,
                    "expected_slot_generation": expected_slot_generation,
                    "reference": reference,
                    "session_authority_sha256": _credential_sha256(session_authority),
                }
            )
            scope = self._idempotency_scope(
                actor.key,
                ProductOperation.SUBMIT,
                key,
            )
            replay = self._idempotency_replay(scope, body_sha256)
            if replay is not None:
                return replay
            self._require_idempotency_capacity(additional_records=1)
            try:
                session = self._require_live_session(
                    actor=actor,
                    session_authority=session_authority,
                    reference=reference,
                    deadline_ms=deadline_ms,
                )
            except ProductLifecycleError as authority_error:
                self._record_core_error_locked(
                    scope,
                    body_sha256,
                    authority_error,
                    deadline_ms=deadline_ms,
                    session_id=self._related_close_session_id(
                        session_authority_sha256=_credential_sha256(session_authority)
                    ),
                )
                raise
            core = self._require_core_session_locked(session.session_id)
            command_limit = _effective_optional_positive_limit(
                session.limits,
                "max_commands",
                self._core_policy.max_commands_per_session,
            )
            if command_limit is not None and core.command_count >= command_limit:
                error = _error(
                    "command_limit_reached",
                    "The product session command limit was reached.",
                    status=409,
                    retry="none",
                )
                self._record_core_error_locked(
                    scope,
                    body_sha256,
                    error,
                    deadline_ms=deadline_ms,
                    session_id=session.session_id,
                )
                raise error
            command_bytes_limit = _effective_positive_limit(
                session.limits,
                "max_command_bytes",
                MAX_CANONICAL_COMMAND_UTF8_BYTES,
            )
            if len(canonical.canonical_utf8) > command_bytes_limit:
                error = _error(
                    "command_too_large",
                    "The product command exceeds this session's limit.",
                    status=413,
                    retry="none",
                )
                self._record_core_error_locked(
                    scope,
                    body_sha256,
                    error,
                    deadline_ms=deadline_ms,
                    session_id=session.session_id,
                )
                raise error
            if core.active_command_id is not None:
                error = _error(
                    "command_in_progress",
                    "A product command is already in progress.",
                    status=409,
                    retry="resume_snapshot",
                )
                self._record_core_error_locked(
                    scope,
                    body_sha256,
                    error,
                    deadline_ms=deadline_ms,
                    session_id=session.session_id,
                )
                raise error
            if core.slot_generation != expected_slot_generation:
                error = _error(
                    "command_slot_conflict",
                    "The product command slot generation changed.",
                    status=409,
                    retry="resume_snapshot",
                )
                self._record_core_error_locked(
                    scope,
                    body_sha256,
                    error,
                    deadline_ms=deadline_ms,
                    session_id=session.session_id,
                )
                raise error

            command_id, command_value_digest = self._new_core_identity_locked(
                "command",
                "command",
            )
            next_generation = core.slot_generation + 1
            reserved_tokens = _effective_positive_limit(
                session.limits,
                "command_reserved_tokens",
                self._core_policy.reserved_tokens_per_command,
            )
            reserved_spend = _effective_positive_limit(
                session.limits,
                "command_reserved_spend_units",
                self._core_policy.reserved_spend_units_per_command,
            )
            max_attempts = _effective_positive_limit(
                session.limits,
                "command_max_attempts",
                self._core_policy.max_attempts_per_command,
            )
            try:
                self._admission.reserve_and_claim_worker(
                    logical_job_id=command_id,
                    session_id=session.session_id,
                    category=ProductJobCategory.FOREGROUND_PRODUCT,
                    reserved_tokens=reserved_tokens,
                    reserved_spend_units=reserved_spend,
                )
            except ProductAdmissionDenied as denial:
                self._used_value_digests.discard(command_value_digest)
                admission_error = _admission_error(denial)
                if not denial.retryable:
                    self._record_core_error_locked(
                        scope,
                        body_sha256,
                        admission_error,
                        deadline_ms=deadline_ms,
                        session_id=session.session_id,
                    )
                raise admission_error from None

            command_deadline_ms = _effective_positive_limit(
                session.limits,
                "command_deadline_ms",
                self._core_policy.command_deadline_ms,
            )
            idle_ttl = _effective_positive_limit(
                session.limits,
                "idle_ttl_ms",
                self._policy.idle_ttl_ms,
            )
            next_idle_deadline_ms = min(
                deadline_ms + idle_ttl,
                session.absolute_deadline_ms,
            )
            command_deadline_deadline_ms = min(
                deadline_ms + command_deadline_ms,
                session.absolute_deadline_ms,
                next_idle_deadline_ms,
            )
            request_id = _request_id(command_id, key)
            record = _CommandRecord(
                session_id=session.session_id,
                command_id=command_id,
                canonical_command=canonical.canonical_text,
                slot_generation=next_generation,
                request_id=request_id,
                accepted_at_ms=now_ms,
                accepted_deadline_ms=deadline_ms,
                deadline_at_ms=(now_ms + (command_deadline_deadline_ms - deadline_ms)),
                deadline_deadline_ms=command_deadline_deadline_ms,
                checkpoint_namespace=(
                    f"product:{session.session_id}:{next_generation}:{command_id}"
                ),
                cancel_event=threading.Event(),
                reserved_tokens=reserved_tokens,
                reserved_spend_units=reserved_spend,
                max_attempts=max_attempts,
                base_accepted_state_revision=core.accepted_state_revision,
            )
            try:
                _transcript_entry, accepted_event = (
                    core.journal.append_command_accepted(
                        canonical.canonical_text,
                        command_id=command_id,
                        slot_generation=next_generation,
                        accepted_state_revision=core.accepted_state_revision,
                    )
                )
            except Exception:
                self._admission.discard_undispatched_job(command_id)
                self._used_value_digests.discard(command_value_digest)
                raise
            core.slot_generation = next_generation
            core.command_count += 1
            core.active_command_id = command_id
            core.latest_command_id = command_id
            core.commands[command_id] = record
            session.last_activity_at_ms = now_ms
            session.idle_deadline_ms = next_idle_deadline_ms
            session.idle_expires_at_ms = now_ms + (
                session.idle_deadline_ms - deadline_ms
            )
            result = {
                "kind": "command_accepted",
                "service_epoch": self._service_epoch,
                "replayed": False,
                "command_id": command_id,
                "canonical_command": canonical.canonical_text,
                "slot_generation": next_generation,
                "event_cursor": accepted_event.cursor.to_dict(),
                "request_id": request_id,
            }
            self._idempotency[scope] = _IdempotencyRecord(
                body_sha256=body_sha256,
                result=deepcopy(result),
                error=None,
                retain_until_deadline_ms=(
                    session.absolute_deadline_ms + self._policy.tombstone_ttl_ms
                ),
                session_id=session.session_id,
            )
            dispatch_command_id = command_id

        assert dispatch_command_id is not None
        command_to_dispatch = dispatch_command_id

        def dispatch() -> None:
            self._execute_command(command_to_dispatch)

        try:
            self._submit_task(dispatch)
        except Exception:
            self._fail_dispatch(dispatch_command_id)
        except BaseException:
            # Acceptance crossed the product admission boundary. Preserve
            # Python control-flow exceptions, but first converge the queued
            # command, slot, reservation, and exact-replay truth.
            self._fail_dispatch(dispatch_command_id)
            raise
        return result

    def cancel_command(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        idempotency_key: str,
        command_id: str,
        slot_generation: int,
    ) -> dict[str, object]:
        """Close the exact command publication fence or report terminal truth."""
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            now_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms)
            self._expire_and_prune_locked(deadline_ms)
            self._reconcile_due_commands_locked(
                now_ms=now_ms,
                deadline_ms=deadline_ms,
            )
            key = _require_idempotency_key(idempotency_key)
            _require_positive_generation(slot_generation)
            body_sha256 = _canonical_sha256(
                {
                    "command_id": command_id,
                    "reference": reference,
                    "session_authority_sha256": _credential_sha256(session_authority),
                    "slot_generation": slot_generation,
                }
            )
            scope = self._idempotency_scope(
                actor.key,
                ProductOperation.CANCEL,
                key,
            )
            replay = self._idempotency_replay(scope, body_sha256)
            if replay is not None:
                return replay
            self._require_idempotency_capacity(additional_records=1)
            try:
                session = self._require_live_session(
                    actor=actor,
                    session_authority=session_authority,
                    reference=reference,
                    deadline_ms=deadline_ms,
                )
            except ProductLifecycleError as authority_error:
                self._record_core_error_locked(
                    scope,
                    body_sha256,
                    authority_error,
                    deadline_ms=deadline_ms,
                    session_id=self._related_close_session_id(
                        session_authority_sha256=_credential_sha256(session_authority)
                    ),
                )
                raise
            core = self._require_core_session_locked(session.session_id)
            record = core.commands.get(command_id)
            if record is None or record.slot_generation != slot_generation:
                error = _error(
                    "command_not_found",
                    "The product command is unavailable.",
                    status=404,
                    retry="resume_snapshot",
                )
                self._record_core_error_locked(
                    scope,
                    body_sha256,
                    error,
                    deadline_ms=deadline_ms,
                    session_id=session.session_id,
                )
                raise error
            if record.terminal:
                result = {
                    "kind": "command_cancel",
                    "service_epoch": self._service_epoch,
                    "replayed": False,
                    "outcome": "already_terminal",
                    "command_id": record.command_id,
                    "slot_generation": record.slot_generation,
                    "command_result": deepcopy(record.result),
                    "event_cursor": core.journal.event_position.to_dict(),
                }
            elif core.active_command_id != command_id:
                error = _error(
                    "command_not_found",
                    "The product command is unavailable.",
                    status=404,
                    retry="resume_snapshot",
                )
                self._record_core_error_locked(
                    scope,
                    body_sha256,
                    error,
                    deadline_ms=deadline_ms,
                    session_id=session.session_id,
                )
                raise error
            else:
                record.fence_open = False
                record.status = "cancel_requested"
                record.cancel_requested_at_ms = now_ms
                record.cancel_deadline_ms = min(
                    deadline_ms + self._core_policy.cancellation_deadline_ms,
                    record.deadline_deadline_ms,
                    session.absolute_deadline_ms,
                    session.idle_deadline_ms,
                )
                record.cancel_event.set()
                self._admission.fence_job(command_id)
                event = core.journal.append_event(
                    "command.cancel_requested",
                    criticality=ProductEventCriticality.AUTHORITATIVE,
                    command_id=command_id,
                    data={"slot_generation": slot_generation},
                )
                result = {
                    "kind": "command_cancel",
                    "service_epoch": self._service_epoch,
                    "replayed": False,
                    "outcome": "cancel_requested",
                    "command_id": command_id,
                    "slot_generation": slot_generation,
                    "event_cursor": event.cursor.to_dict(),
                }
            self._idempotency[scope] = _IdempotencyRecord(
                body_sha256=body_sha256,
                result=deepcopy(result),
                error=None,
                retain_until_deadline_ms=(
                    session.absolute_deadline_ms + self._policy.tombstone_ttl_ms
                ),
                session_id=session.session_id,
            )
            return result

    def command_result(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        command_id: str,
    ) -> dict[str, object]:
        with self._lock:
            session, core = self._authorized_core_read_locked(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
            )
            record = core.commands.get(command_id)
            if record is None:
                raise _error(
                    "command_not_found",
                    "The product command is unavailable.",
                    status=404,
                    retry="resume_snapshot",
                )
            return {
                "kind": "command_result",
                "service_epoch": self._service_epoch,
                "session_id": session.session_id,
                "terminal": record.terminal,
                "command": self._command_projection(record),
                "event_cursor": core.journal.event_position.to_dict(),
            }

    def events(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        after: EventCursor | None = None,
    ) -> dict[str, object]:
        with self._lock:
            _session, core = self._authorized_core_read_locked(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
            )
            try:
                page = (
                    core.journal.event_snapshot()
                    if after is None
                    else core.journal.replay_events(after)
                )
            except JournalSnapshotRequired as error:
                raise _error(
                    "snapshot_required",
                    "The product event cursor requires an authoritative snapshot.",
                    status=409,
                    retry="resume_snapshot",
                ) from error
            return _event_page(page)

    def transcript(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        after: TranscriptCursor | None = None,
    ) -> dict[str, object]:
        with self._lock:
            _session, core = self._authorized_core_read_locked(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
            )
            try:
                page = (
                    core.journal.transcript_snapshot()
                    if after is None
                    else core.journal.replay_transcript(after)
                )
            except JournalSnapshotRequired as error:
                raise _error(
                    "snapshot_required",
                    "The product transcript cursor requires an authoritative snapshot.",
                    status=409,
                    retry="resume_snapshot",
                ) from error
            return _transcript_page(page)

    def reset(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        idempotency_key: str,
        expected_control_revision: int,
    ) -> dict[str, object]:
        """Provision a clean copy of the same immutable revision, then cut over."""
        return self._replace_session(
            actor_authority=actor_authority,
            session_authority=session_authority,
            reference=reference,
            expected_epoch=expected_epoch,
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            operation=ProductOperation.RESET,
            switch_grant=None,
        )

    def switch(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        idempotency_key: str,
        expected_control_revision: int,
        launch_grant: str,
    ) -> dict[str, object]:
        """Provision an authorized different revision, then atomically cut over."""
        return self._replace_session(
            actor_authority=actor_authority,
            session_authority=session_authority,
            reference=reference,
            expected_epoch=expected_epoch,
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            operation=ProductOperation.SWITCH,
            switch_grant=launch_grant,
        )

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
        with self._lock:
            result = super().close(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
                idempotency_key=idempotency_key,
                expected_control_revision=expected_control_revision,
            )
            if bool(result["replayed"]):
                return result
            tombstone = self._tombstones_by_reference[reference]
            enriched = deepcopy(result)
            enriched["snapshot"] = self._core_terminal_snapshot_locked(tombstone)
            actor = self._require_actor(actor_authority, self._last_deadline_ms)
            scope = self._idempotency_scope(
                actor.key,
                ProductOperation.CLOSE,
                _require_idempotency_key(idempotency_key),
            )
            self._idempotency[scope].result = deepcopy(enriched)
            return enriched

    def reconcile_due(self) -> int:
        """Lazily fence overdue command/cancel work; starts no timer."""
        with self._lock:
            _now_ms, deadline_ms = self._now()
            self._expire_and_prune_locked(deadline_ms)
            return self._reconcile_due_commands_locked(deadline_ms=deadline_ms)

    def _replace_session(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        idempotency_key: str,
        expected_control_revision: int,
        operation: ProductOperation,
        switch_grant: str | None,
    ) -> dict[str, object]:
        if operation not in {ProductOperation.RESET, ProductOperation.SWITCH}:
            raise ValueError("replacement operation is invalid")
        claim: _ReplacementClaim
        old_session: _SessionRecord
        old_core: _CoreSessionState
        grant: _LaunchGrantRecord | None
        candidate_document: dict[str, object]

        while True:
            wait_for: _ReplacementClaim | None = None
            with self._lock:
                self._require_epoch(expected_epoch, mutation=True)
                now_ms, deadline_ms = self._now()
                actor = self._require_actor(actor_authority, deadline_ms)
                self._expire_and_prune_locked(deadline_ms)
                self._reconcile_due_commands_locked(
                    now_ms=now_ms,
                    deadline_ms=deadline_ms,
                )
                key = _require_idempotency_key(idempotency_key)
                _require_positive_generation(expected_control_revision)
                grant_sha256 = (
                    _credential_sha256(switch_grant)
                    if switch_grant is not None
                    else None
                )
                body_sha256 = _canonical_sha256(
                    {
                        "expected_control_revision": expected_control_revision,
                        "launch_grant_sha256": grant_sha256,
                        "reference": reference,
                        "session_authority_sha256": _credential_sha256(
                            session_authority
                        ),
                    }
                )
                scope = self._idempotency_scope(actor.key, operation, key)
                replay = self._idempotency_replay(scope, body_sha256)
                if replay is not None:
                    return replay

                existing_claim = self._replacement_claims.get(scope)
                if existing_claim is not None:
                    if existing_claim.body_sha256 != body_sha256:
                        raise _error(
                            "idempotency_conflict",
                            "The idempotency key was already used for another request.",
                            status=409,
                            retry="none",
                        )
                    if existing_claim.owner_thread_id == threading.get_ident():
                        raise RuntimeError(
                            "replacement validation cannot re-enter its own "
                            "idempotency scope"
                        )
                    wait_for = existing_claim
                else:
                    # An in-flight claim reserves both the eventual idempotency
                    # record and the replacement tombstone. Distinct validators
                    # therefore cannot grow beyond either configured ledger.
                    self._require_replacement_capacity_locked(additional_claims=1)
                    try:
                        old_session = self._require_live_session(
                            actor=actor,
                            session_authority=session_authority,
                            reference=reference,
                            deadline_ms=deadline_ms,
                        )
                    except ProductLifecycleError as authority_error:
                        self._record_core_error_locked(
                            scope,
                            body_sha256,
                            authority_error,
                            deadline_ms=deadline_ms,
                            session_id=self._related_close_session_id(
                                session_authority_sha256=_credential_sha256(
                                    session_authority
                                )
                            ),
                        )
                        raise
                    old_core = self._require_core_session_locked(old_session.session_id)
                    if old_session.control_revision != expected_control_revision:
                        error = _error(
                            "control_revision_conflict",
                            "The product session control revision changed.",
                            status=409,
                            retry="resume_snapshot",
                        )
                        self._record_core_error_locked(
                            scope,
                            body_sha256,
                            error,
                            deadline_ms=deadline_ms,
                            session_id=old_session.session_id,
                        )
                        raise error
                    if old_core.active_command_id is not None:
                        error = _error(
                            "command_in_progress",
                            "The product session has a command in progress.",
                            status=409,
                            retry="resume_snapshot",
                        )
                        self._record_core_error_locked(
                            scope,
                            body_sha256,
                            error,
                            deadline_ms=deadline_ms,
                            session_id=old_session.session_id,
                        )
                        raise error

                    grant = None
                    if operation is ProductOperation.SWITCH:
                        assert grant_sha256 is not None
                        try:
                            grant = self._require_launch_grant(
                                launch_sha256=grant_sha256,
                                actor=actor,
                                deadline_ms=deadline_ms,
                                operation=ProductOperation.SWITCH,
                            )
                        except ProductLifecycleError as authority_error:
                            self._record_core_error_locked(
                                scope,
                                body_sha256,
                                authority_error,
                                deadline_ms=deadline_ms,
                                session_id=self._related_launch_session_id(
                                    grant_sha256
                                ),
                            )
                            raise

                    revision = (
                        old_session.revision
                        if grant is None
                        else self._revisions[grant.revision_id]
                    )
                    try:
                        candidate_document = revision.allocate()
                    except Exception:
                        safe_error = _error(
                            "replacement_unavailable",
                            "The replacement product session could not be provisioned.",
                            status=503,
                            retry="same_request",
                        )
                        self._record_core_error_locked(
                            scope,
                            body_sha256,
                            safe_error,
                            deadline_ms=deadline_ms,
                            session_id=old_session.session_id,
                        )
                        raise safe_error from None

                    claim = _ReplacementClaim(
                        body_sha256=body_sha256,
                        owner_thread_id=threading.get_ident(),
                    )
                    self._replacement_claims[scope] = claim

            if wait_for is None:
                break
            wait_for.completed.wait()
            if wait_for.transient_error is not None:
                raise wait_for.transient_error.restore()

        validation_failed = False
        try:
            try:
                self._validate_replacement(deepcopy(candidate_document))
            except Exception:
                validation_failed = True

            with self._lock:
                try:
                    current_claim = self._replacement_claims.get(scope)
                    if current_claim is not claim:
                        raise RuntimeError(
                            "replacement idempotency claim changed unexpectedly"
                        )
                    if claim.owner_thread_id != threading.get_ident():
                        raise RuntimeError(
                            "replacement idempotency claim owner changed unexpectedly"
                        )

                    # Validation is detached from the aggregate lock. Read both
                    # clocks again, expire stale state, and recheck every source
                    # identity and CAS before installing anything.
                    self._require_epoch(expected_epoch, mutation=True)
                    now_ms, deadline_ms = self._now()
                    try:
                        actor = self._require_actor(actor_authority, deadline_ms)
                    except ProductLifecycleError as authority_error:
                        self._record_core_error_locked(
                            scope,
                            body_sha256,
                            authority_error,
                            deadline_ms=deadline_ms,
                            session_id=self._related_close_session_id(
                                session_authority_sha256=_credential_sha256(
                                    session_authority
                                )
                            ),
                        )
                        raise
                    self._expire_and_prune_locked(deadline_ms)
                    self._reconcile_due_commands_locked(
                        now_ms=now_ms,
                        deadline_ms=deadline_ms,
                    )
                    replay = self._idempotency_replay(scope, body_sha256)
                    if replay is not None:
                        return replay
                    try:
                        self._require_replacement_capacity_locked(additional_claims=0)
                    except ProductLifecycleError as capacity_error:
                        if capacity_error.code != "capacity_exhausted":
                            raise
                        claim.transient_error = _RecordedError.capture(capacity_error)
                        raise
                    try:
                        current_old_session = self._require_live_session(
                            actor=actor,
                            session_authority=session_authority,
                            reference=reference,
                            deadline_ms=deadline_ms,
                        )
                    except ProductLifecycleError as authority_error:
                        self._record_core_error_locked(
                            scope,
                            body_sha256,
                            authority_error,
                            deadline_ms=deadline_ms,
                            session_id=self._related_close_session_id(
                                session_authority_sha256=_credential_sha256(
                                    session_authority
                                )
                            ),
                        )
                        raise
                    if current_old_session is not old_session:
                        raise RuntimeError(
                            "replacement source identity changed unexpectedly"
                        )
                    current_old_core = self._require_core_session_locked(
                        old_session.session_id
                    )
                    if current_old_core is not old_core:
                        raise RuntimeError(
                            "replacement core identity changed unexpectedly"
                        )
                    if old_session.control_revision != expected_control_revision:
                        safe_error = _error(
                            "control_revision_conflict",
                            "The product session control revision changed.",
                            status=409,
                            retry="resume_snapshot",
                        )
                        self._record_core_error_locked(
                            scope,
                            body_sha256,
                            safe_error,
                            deadline_ms=deadline_ms,
                            session_id=old_session.session_id,
                        )
                        raise safe_error
                    if old_core.active_command_id is not None:
                        safe_error = _error(
                            "command_in_progress",
                            "The product session has a command in progress.",
                            status=409,
                            retry="resume_snapshot",
                        )
                        self._record_core_error_locked(
                            scope,
                            body_sha256,
                            safe_error,
                            deadline_ms=deadline_ms,
                            session_id=old_session.session_id,
                        )
                        raise safe_error
                    if grant is not None:
                        assert grant_sha256 is not None
                        try:
                            current_grant = self._require_launch_grant(
                                launch_sha256=grant_sha256,
                                actor=actor,
                                deadline_ms=deadline_ms,
                                operation=ProductOperation.SWITCH,
                            )
                        except ProductLifecycleError as authority_error:
                            self._record_core_error_locked(
                                scope,
                                body_sha256,
                                authority_error,
                                deadline_ms=deadline_ms,
                                session_id=self._related_launch_session_id(
                                    grant_sha256
                                ),
                            )
                            raise
                        if current_grant is not grant:
                            raise RuntimeError(
                                "replacement grant identity changed unexpectedly"
                            )
                    if validation_failed:
                        safe_error = _error(
                            "replacement_unavailable",
                            "The replacement product session could not be provisioned.",
                            status=503,
                            retry="same_request",
                        )
                        self._record_core_error_locked(
                            scope,
                            body_sha256,
                            safe_error,
                            deadline_ms=deadline_ms,
                            session_id=old_session.session_id,
                        )
                        raise safe_error from None

                    replacement: _SessionRecord | None = None
                    try:
                        replacement = self._provision_replacement_locked(
                            old_session=old_session,
                            grant=grant,
                            now_ms=now_ms,
                            deadline_ms=deadline_ms,
                            runtime_document=candidate_document,
                        )
                        replacement_core = self._build_core_state_locked(replacement)
                        # Reset/switch replaces runtime state, not the session's
                        # already-consumed command allowance.
                        replacement_core.command_count = old_core.command_count
                    except Exception as provisioning_error:
                        if replacement is not None:
                            self._discard_uninstalled_session_values_locked(replacement)
                        if isinstance(provisioning_error, ProductLifecycleError):
                            safe_error = provisioning_error
                        else:
                            safe_error = _error(
                                "replacement_unavailable",
                                "The replacement product session could not be "
                                "provisioned.",
                                status=503,
                                retry="same_request",
                            )
                        self._record_core_error_locked(
                            scope,
                            body_sha256,
                            safe_error,
                            deadline_ms=deadline_ms,
                            session_id=old_session.session_id,
                        )
                        raise safe_error from None

                    try:
                        self._admission.replace_session(
                            old_session.session_id,
                            replacement.session_id,
                        )
                    except ProductAdmissionDenied as denial:
                        self._discard_uninstalled_session_values_locked(replacement)
                        safe_error = _admission_error(denial)
                        self._record_core_error_locked(
                            scope,
                            body_sha256,
                            safe_error,
                            deadline_ms=deadline_ms,
                            session_id=old_session.session_id,
                        )
                        raise safe_error from None

                    old_tombstone = self._terminalize_locked(
                        old_session,
                        terminal_at_ms=now_ms,
                        terminal_deadline_ms=deadline_ms,
                        terminal_reason="replaced",
                    )
                    authority = _SessionAuthorityRecord(
                        actor_key=replacement.actor_key,
                        session_id=replacement.session_id,
                        reference=replacement.reference,
                        expires_deadline_ms=replacement.absolute_deadline_ms,
                        service_epoch=self._service_epoch,
                    )
                    self._sessions_by_reference[replacement.reference] = replacement
                    self._sessions_by_id[replacement.session_id] = replacement
                    self._session_authorities[replacement.session_authority_sha256] = (
                        authority
                    )
                    self._current_by_actor[replacement.actor_key] = (
                        replacement.reference
                    )
                    self._core_sessions[replacement.session_id] = replacement_core
                    if grant is not None:
                        grant.consumed_session_id = replacement.session_id
                    result: dict[str, object] = {
                        "kind": (
                            "session_reset"
                            if operation is ProductOperation.RESET
                            else "session_switched"
                        ),
                        "service_epoch": self._service_epoch,
                        "replayed": False,
                        "session_authority": replacement.session_authority,
                        "previous_snapshot": self._core_terminal_snapshot_locked(
                            old_tombstone
                        ),
                        "snapshot": self._core_active_snapshot_locked(
                            replacement,
                            replacement_core,
                        ),
                    }
                    self._idempotency[scope] = _IdempotencyRecord(
                        body_sha256=body_sha256,
                        result=deepcopy(result),
                        error=None,
                        retain_until_deadline_ms=(
                            replacement.absolute_deadline_ms
                            + self._policy.tombstone_ttl_ms
                        ),
                        session_id=replacement.session_id,
                    )
                    return result
                finally:
                    self._release_replacement_claim_locked(scope, claim)
        finally:
            # This path owns BaseException and lock-acquisition interruption as
            # well as ordinary validation/finalization exits. No abandoned
            # claim may strand exact retries forever.
            if not claim.completed.is_set():
                with self._lock:
                    self._release_replacement_claim_locked(scope, claim)

    def _require_replacement_capacity_locked(
        self,
        *,
        additional_claims: int,
    ) -> None:
        if additional_claims not in {0, 1}:
            raise ValueError("additional replacement claims must be zero or one")
        reserved_claims = len(self._replacement_claims) + additional_claims
        # The shared subclass capacity check already includes installed
        # replacement claims. Only the prospective claim is additional here.
        self._require_idempotency_capacity(additional_records=additional_claims)
        # Replacement creates one tombstone while leaving the number of active
        # sessions unchanged. Preserve one eventual terminal slot for every
        # active session, including all replacements currently in validation.
        if (
            len(self._tombstones_by_reference)
            + len(self._sessions_by_reference)
            + reserved_claims
            > self._policy.max_tombstones
        ):
            raise _capacity_exhausted()

    def _require_idempotency_capacity(self, *, additional_records: int) -> None:
        """Preserve every detached replacement's eventual result record."""
        super()._require_idempotency_capacity(
            additional_records=(additional_records + len(self._replacement_claims)),
        )

    def _require_new_session_capacity(self) -> None:
        """Preserve detached replacement claims across inherited create."""
        active_count = len(self._sessions_by_reference)
        if active_count >= self._policy.max_active_sessions:
            raise _capacity_exhausted()
        if (
            len(self._tombstones_by_reference)
            + active_count
            + len(self._replacement_claims)
            + 1
            > self._policy.max_tombstones
        ):
            raise _capacity_exhausted()
        # A create needs its result record plus one idempotency slot reserved
        # for the new active session's eventual terminal reconciliation.
        self._require_idempotency_capacity(additional_records=2)

    def _require_close_capacity(self) -> None:
        """Allow a valid close to consume its active session reservations."""
        claim_count = len(self._replacement_claims)
        if (
            len(self._tombstones_by_reference) + claim_count
            >= self._policy.max_tombstones
        ):
            raise _capacity_exhausted()
        # The valid target's active reservation becomes the close result slot;
        # pre-authority failures separately use the stricter shared check.
        if (
            len(self._idempotency) + claim_count + 1
            > self._policy.max_idempotency_records
        ):
            raise _capacity_exhausted()

    def _release_replacement_claim_locked(
        self,
        scope: tuple[str, tuple[str, ProductRole], ProductOperation, str],
        claim: _ReplacementClaim,
    ) -> None:
        current = self._replacement_claims.get(scope)
        if current is None and claim.completed.is_set():
            return
        if current is not claim:
            raise RuntimeError("replacement idempotency claim changed unexpectedly")
        if claim.owner_thread_id != threading.get_ident():
            raise RuntimeError("only the replacement claim owner may release it")
        self._replacement_claims.pop(scope)
        claim.completed.set()

    def _provision_replacement_locked(
        self,
        *,
        old_session: _SessionRecord,
        grant: _LaunchGrantRecord | None,
        now_ms: int,
        deadline_ms: int,
        runtime_document: dict[str, object],
    ) -> _SessionRecord:
        if grant is None:
            revision = old_session.revision
            selected_record_id = old_session.selected_record_id
            launch_grant_sha256 = old_session.launch_grant_sha256
            grant_binding_sha256 = old_session.grant_binding_sha256
            limits = old_session.limits
            policy_id = old_session.policy_id
            notice_version = old_session.notice_version
            authority_deadline_ms = old_session.absolute_deadline_ms
        else:
            revision = self._revisions[grant.revision_id]
            selected_record_id = grant.selected_record_id
            launch_grant_sha256 = next(
                digest
                for digest, candidate in self._launch_grants.items()
                if candidate is grant
            )
            grant_binding_sha256 = grant.binding_sha256
            limits = grant.limits
            policy_id = grant.policy_id
            notice_version = grant.notice_version
            authority_deadline_ms = grant.expires_deadline_ms
        session_id, reference, session_authority = self._new_session_values()
        absolute_ttl_ms = _effective_lifecycle_ttl(
            limits,
            name="absolute_ttl_ms",
            policy_ttl_ms=self._policy.absolute_ttl_ms,
        )
        idle_ttl_ms = _effective_lifecycle_ttl(
            limits,
            name="idle_ttl_ms",
            policy_ttl_ms=self._policy.idle_ttl_ms,
        )
        absolute_deadline_ms = min(
            deadline_ms + absolute_ttl_ms,
            authority_deadline_ms,
        )
        idle_deadline_ms = min(
            deadline_ms + idle_ttl_ms,
            absolute_deadline_ms,
        )
        return _SessionRecord(
            session_id=session_id,
            reference=reference,
            actor_key=old_session.actor_key,
            revision=revision,
            runtime_document=runtime_document,
            session_authority=session_authority,
            session_authority_sha256=_credential_sha256(session_authority),
            selected_record_id=selected_record_id,
            launch_grant_sha256=launch_grant_sha256,
            grant_binding_sha256=grant_binding_sha256,
            limits=limits,
            policy_id=policy_id,
            notice_version=notice_version,
            created_at_ms=now_ms,
            last_activity_at_ms=now_ms,
            absolute_expires_at_ms=(now_ms + (absolute_deadline_ms - deadline_ms)),
            idle_expires_at_ms=now_ms + (idle_deadline_ms - deadline_ms),
            absolute_deadline_ms=absolute_deadline_ms,
            idle_deadline_ms=idle_deadline_ms,
        )

    def _initialize_core_session_locked(
        self,
        session: _SessionRecord,
    ) -> _CoreSessionState:
        existing = self._core_sessions.get(session.session_id)
        if existing is not None:
            return existing
        capacity_class = self._capacity_class_for_session(session)
        if capacity_class is ProductCapacityClass.STANDARD:
            self._admission.admit_session(session.session_id)
        else:
            self._admission.admit_session(
                session.session_id,
                capacity_class=capacity_class,
            )
        core = self._build_core_state_locked(session)
        self._core_sessions[session.session_id] = core
        return core

    def _capacity_class_for_session(
        self,
        _session: _SessionRecord,
    ) -> ProductCapacityClass:
        """Select the admission partition for an explicitly constructed core."""
        return ProductCapacityClass.STANDARD

    def _build_core_state_locked(
        self,
        session: _SessionRecord,
    ) -> _CoreSessionState:
        def journal_id_factory(purpose: str) -> str:
            value = f"{session.session_id}\0{purpose}".encode()
            return hashlib.sha256(value).hexdigest()[:32]

        journal = ProductSessionJournal(
            limits=self._journal_limits,
            id_factory=journal_id_factory,
            clock_ms=self._journal_clock_ms,
        )
        core = _CoreSessionState(journal=journal)
        journal.append_event(
            "session.created",
            criticality=ProductEventCriticality.AUTHORITATIVE,
            data={
                "control_revision": session.control_revision,
                "slot_generation": 0,
                "accepted_state_revision": 0,
            },
        )
        return core

    def _execute_command(self, command_id: str) -> None:
        with self._lock:
            now_ms, deadline_ms = self._now()
            self._expire_and_prune_locked(deadline_ms)
            located = self._locate_command_locked(command_id)
            if located is None:
                retained = self._retained_command_locked(command_id)
                if retained is not None:
                    if retained.dispatch_state is _DispatchState.QUEUED:
                        retained.dispatch_state = _DispatchState.ABANDONED
                        self._reconcile_worker_locked(
                            retained,
                            ProductExecutionResult.interrupted(
                                "cancelled_before_dispatch"
                            ),
                        )
                else:
                    self._reconcile_orphan_worker(
                        command_id,
                        ProductExecutionResult.interrupted("cancelled_before_dispatch"),
                    )
                return
            session, core, record = located
            if record.terminal:
                if record.dispatch_state is _DispatchState.QUEUED:
                    record.dispatch_state = _DispatchState.ABANDONED
                    self._reconcile_worker_locked(
                        record,
                        ProductExecutionResult.interrupted("cancelled_before_dispatch"),
                    )
                return
            if record.dispatch_state is not _DispatchState.QUEUED:
                return
            if record.cancel_event.is_set():
                record.dispatch_state = _DispatchState.ABANDONED
                result = ProductExecutionResult.interrupted("cancelled_before_dispatch")
                self._publish_execution_result_locked(session, core, record, result)
                return
            due_reason = _command_due_reason(record, deadline_ms)
            if due_reason is not None:
                record.dispatch_state = _DispatchState.ABANDONED
                record.fence_open = False
                record.cancel_event.set()
                self._admission.fence_job(record.command_id)
                self._publish_forced_terminal_locked(
                    session,
                    core,
                    record,
                    now_ms=now_ms,
                    reason=due_reason,
                )
                self._reconcile_worker_locked(
                    record,
                    ProductExecutionResult.failed(due_reason),
                )
                return
            record.dispatch_state = _DispatchState.RUNNING
            record.status = "running"
            try:
                core.journal.append_event(
                    "command.running",
                    criticality=ProductEventCriticality.INFORMATIONAL,
                    command_id=command_id,
                    data={"slot_generation": record.slot_generation},
                )
                request = ProductExecutionRequest(
                    service_epoch=self._service_epoch,
                    session_id=session.session_id,
                    command_id=record.command_id,
                    slot_generation=record.slot_generation,
                    canonical_command=record.canonical_command,
                    provisional_document=deepcopy(session.runtime_document),
                    checkpoint_namespace=record.checkpoint_namespace,
                    deadline_ms=record.deadline_at_ms,
                    max_tokens=record.reserved_tokens,
                    max_spend_units=record.reserved_spend_units,
                    max_attempts=record.max_attempts,
                    cancelled=record.cancel_event.is_set,
                )
            except Exception:
                record.dispatch_state = _DispatchState.ABANDONED
                self._publish_execution_result_locked(
                    session,
                    core,
                    record,
                    ProductExecutionResult.failed("infrastructure_failure"),
                )
                return
        output_publication_closed = threading.Event()

        def publish_output(chunk: ProductOutputChunk) -> bool:
            if output_publication_closed.is_set():
                return False
            keep_open = self._emit_output(
                command_id,
                chunk,
                publication_closed=output_publication_closed,
            )
            if not keep_open:
                output_publication_closed.set()
            return keep_open

        fatal_error: BaseException | None = None
        try:
            try:
                result = self._executor.execute(
                    request,
                    publish_output,
                )
                if not isinstance(result, ProductExecutionResult):
                    raise TypeError("executor returned an invalid product result")
            except Exception:
                # Dispatch crossed the executor boundary, so unknown usage must
                # consume the full reservation rather than under-account work.
                result = ProductExecutionResult.failed(
                    "infrastructure_failure",
                    usage_tokens=record.reserved_tokens,
                    usage_spend_units=record.reserved_spend_units,
                )
            result, validated_document = self._qualify_execution_result(record, result)
        except BaseException as error:
            # Fatal executor and detached-validator exits retain their Python
            # control-flow meaning, but physical dispatch has already happened.
            # Reconcile the full reservation before propagating the exception.
            fatal_error = error
            result = ProductExecutionResult.failed(
                "infrastructure_failure",
                usage_tokens=record.reserved_tokens,
                usage_spend_units=record.reserved_spend_units,
            )
            validated_document = None
        with self._lock:
            record.dispatch_state = _DispatchState.RETURNED
            located = self._locate_command_locked(command_id)
            if located is None:
                self._reconcile_worker_locked(record, result)
            else:
                session, core, record = located
                if record.terminal:
                    self._reconcile_worker_locked(record, result)
                else:
                    self._publish_execution_result_locked(
                        session,
                        core,
                        record,
                        result,
                        validated_document=validated_document,
                    )
        if fatal_error is not None:
            raise fatal_error

    def _emit_output(
        self,
        command_id: str,
        chunk: ProductOutputChunk,
        *,
        publication_closed: threading.Event,
    ) -> bool:
        if publication_closed.is_set():
            return False
        if not isinstance(chunk, ProductOutputChunk) or not chunk.text:
            return True
        with self._lock:
            if publication_closed.is_set():
                return False
            now_ms, deadline_ms = self._now()
            self._expire_and_prune_locked(deadline_ms)
            located = self._locate_command_locked(command_id)
            if located is None:
                publication_closed.set()
                return False
            session, core, record = located
            if record.terminal:
                publication_closed.set()
                return False
            due_reason = _command_due_reason(record, deadline_ms)
            if due_reason is not None:
                record.fence_open = False
                record.cancel_event.set()
                self._admission.fence_job(record.command_id)
                self._publish_forced_terminal_locked(
                    session,
                    core,
                    record,
                    now_ms=now_ms,
                    reason=due_reason,
                )
                publication_closed.set()
                return False
            if not record.fence_open:
                publication_closed.set()
                return False
            if record.output_chunks >= self._core_policy.max_output_chunks_per_command:
                record.output_clipped = True
                publication_closed.set()
                return False
            remaining = (
                self._core_policy.max_output_bytes_per_command - record.output_bytes
            )
            projected, omitted = _bounded_utf8_prefix(chunk.text, remaining)
            if not projected:
                record.output_clipped = True
                publication_closed.set()
                return False
            stream = chunk.stream.value
            index = record.stream_chunk_indices[stream]
            entry, _event = core.journal.append_command_output(
                TranscriptKind(stream),
                projected,
                command_id=command_id,
                omitted_after_bytes=omitted,
                chunk_index=index,
                final=False,
            )
            accepted_bytes = len(entry.text.encode("utf-8"))
            record.output_bytes += accepted_bytes
            record.output_chunks += 1
            record.stream_chunk_indices[stream] = index + 1
            record.output_clipped = record.output_clipped or entry.clipped
            if record.output_clipped:
                # Once the output publication boundary closes, the executor's
                # cached wrapper returns False without reacquiring this aggregate
                # lock. Exact use of a boundary remains complete unless a later
                # piece proves that bytes were actually omitted.
                publication_closed.set()
                return False
            return True

    def _qualify_execution_result(
        self,
        record: _CommandRecord,
        candidate: ProductExecutionResult,
    ) -> tuple[ProductExecutionResult, dict[str, object] | None]:
        """Validate detached candidate state without holding the aggregate lock."""
        if (
            candidate.usage_tokens > record.reserved_tokens
            or candidate.usage_spend_units > record.reserved_spend_units
        ):
            return (
                ProductExecutionResult.failed(
                    "resource_usage_exceeded",
                    usage_tokens=record.reserved_tokens,
                    usage_spend_units=record.reserved_spend_units,
                ),
                None,
            )
        if (
            candidate.outcome is not ProductExecutionOutcome.COMPLETED
            or candidate.state_effect is None
            or candidate.state_effect.kind is not ProductStateEffectKind.COMMITTED
        ):
            return candidate, None
        try:
            document = _validated_runtime_document(
                candidate.state_effect.provisional_document,
                maximum_bytes=self._core_policy.max_runtime_document_bytes,
            )
            self._validate_state_effect(deepcopy(document))
        except Exception:
            return (
                _with_rejected_state_effect(
                    candidate,
                    "state_validation_rejected",
                ),
                None,
            )
        return candidate, document

    def _publish_execution_result_locked(
        self,
        session: _SessionRecord,
        core: _CoreSessionState,
        record: _CommandRecord,
        candidate: ProductExecutionResult,
        *,
        validated_document: dict[str, object] | None = None,
    ) -> None:
        now_ms, deadline_ms = self._now()
        self._expire_and_prune_locked(deadline_ms)
        if record.terminal:
            self._reconcile_worker_locked(record, candidate)
            return
        current = self._sessions_by_reference.get(session.reference)
        current_identity = (
            current is not None and current.session_id == session.session_id
        )
        due_reason = _command_due_reason(record, deadline_ms)
        if not current_identity:
            effective = ProductExecutionResult.failed(
                "session_terminated",
                usage_tokens=candidate.usage_tokens,
                usage_spend_units=candidate.usage_spend_units,
            )
        elif due_reason is not None:
            record.fence_open = False
            record.cancel_event.set()
            self._admission.fence_job(record.command_id)
            effective = ProductExecutionResult.failed(
                due_reason,
                usage_tokens=candidate.usage_tokens,
                usage_spend_units=candidate.usage_spend_units,
            )
        elif not record.fence_open:
            if candidate.outcome is ProductExecutionOutcome.INTERRUPTED:
                effective = candidate
            elif candidate.outcome is ProductExecutionOutcome.FAILED:
                effective = candidate
            else:
                effective = ProductExecutionResult.failed(
                    "cancellation_not_established",
                    usage_tokens=candidate.usage_tokens,
                    usage_spend_units=candidate.usage_spend_units,
                )
        else:
            effective = candidate

        if (
            effective.usage_tokens > record.reserved_tokens
            or effective.usage_spend_units > record.reserved_spend_units
        ):
            effective = ProductExecutionResult.failed(
                "resource_usage_exceeded",
                usage_tokens=record.reserved_tokens,
                usage_spend_units=record.reserved_spend_units,
            )

        accepted_document: dict[str, object] | None = None
        if (
            effective.outcome is ProductExecutionOutcome.COMPLETED
            and effective.state_effect is not None
            and effective.state_effect.kind is ProductStateEffectKind.COMMITTED
        ):
            if validated_document is None:
                effective = _with_rejected_state_effect(
                    effective,
                    "state_validation_rejected",
                )
            elif core.accepted_state_revision != record.base_accepted_state_revision:
                effective = _with_rejected_state_effect(
                    effective,
                    "state_revision_conflict",
                )
            else:
                accepted_document = validated_document

        if accepted_document is not None:
            next_state_revision = core.accepted_state_revision + 1
            core.journal.append_event(
                "state.accepted",
                criticality=ProductEventCriticality.AUTHORITATIVE,
                command_id=record.command_id,
                data={
                    "source": "foreground_command",
                    "slot_generation": record.slot_generation,
                    "previous_accepted_state_revision": (core.accepted_state_revision),
                    "accepted_state_revision": next_state_revision,
                    "replacement_sha256": _canonical_sha256(accepted_document),
                },
            )
            session.runtime_document = accepted_document
            core.accepted_state_revision = next_state_revision

        self._append_final_output_markers_locked(core, record)
        projection = _terminal_result_projection(
            effective,
            accepted_state_revision=core.accepted_state_revision,
            output_clipped=record.output_clipped,
        )
        record.status = effective.outcome.value
        record.result = projection
        record.terminal_at_ms = now_ms
        record.fence_open = False
        if core.active_command_id == record.command_id:
            core.active_command_id = None
        core.journal.append_event(
            f"command.{record.status}",
            criticality=ProductEventCriticality.AUTHORITATIVE,
            command_id=record.command_id,
            data={
                "slot_generation": record.slot_generation,
                "accepted_state_revision": core.accepted_state_revision,
                "outcome": record.status,
                "output_clipped": record.output_clipped,
            },
        )
        self._reconcile_worker_locked(record, effective)

    def _append_final_output_markers_locked(
        self,
        core: _CoreSessionState,
        record: _CommandRecord,
    ) -> None:
        for stream in ("stdout", "stderr"):
            index = record.stream_chunk_indices[stream]
            _entry, _event = core.journal.append_command_output(
                TranscriptKind(stream),
                "",
                command_id=record.command_id,
                chunk_index=index,
                final=True,
            )

    def _reconcile_worker_locked(
        self,
        record: _CommandRecord,
        result: ProductExecutionResult,
    ) -> None:
        if record.worker_reconciled:
            return
        if record.dispatch_state not in {
            _DispatchState.RETURNED,
            _DispatchState.ABANDONED,
        }:
            raise RuntimeError("product worker cannot reconcile before return")
        reservation = self._admission.reserve_job(
            logical_job_id=record.command_id,
            session_id=record.session_id,
            category=ProductJobCategory.FOREGROUND_PRODUCT,
            reserved_tokens=record.reserved_tokens,
            reserved_spend_units=record.reserved_spend_units,
        )
        if (
            result.usage_tokens > reservation.reserved_tokens
            or result.usage_spend_units > reservation.reserved_spend_units
        ):
            actual_tokens = reservation.reserved_tokens
            actual_spend = reservation.reserved_spend_units
        else:
            actual_tokens = result.usage_tokens
            actual_spend = result.usage_spend_units
        settlement, _reap = self._admission.settle_and_reap(
            record.command_id,
            actual_tokens=actual_tokens,
            actual_spend_units=actual_spend,
        )
        record.actual_tokens = settlement.actual_tokens
        record.actual_spend_units = settlement.actual_spend_units
        record.worker_reconciled = True
        if record.result is not None:
            record.result["usage"] = {
                "state": "settled",
                "tokens": settlement.actual_tokens,
                "spend_units": settlement.actual_spend_units,
            }

    def _reconcile_orphan_worker(
        self,
        command_id: str,
        result: ProductExecutionResult,
    ) -> None:
        try:
            self._admission.settle_and_reap(
                command_id,
                actual_tokens=result.usage_tokens,
                actual_spend_units=result.usage_spend_units,
            )
        except ProductAdmissionDenied as denial:
            if (
                denial.reason
                is not AdmissionDenialReason.ACTUAL_USAGE_EXCEEDS_RESERVATION
            ):
                return
            # A pruned command record no longer carries its reservation.  An
            # invalid over-report must not strand the independently tracked
            # physical worker, so settle conservatively and reap it.
            try:
                self._admission.settle_and_reap(
                    command_id,
                    actual_tokens=0,
                    actual_spend_units=0,
                )
            except ProductAdmissionDenied:
                return

    def _discard_uninstalled_session_values_locked(
        self,
        session: _SessionRecord,
    ) -> None:
        for digest in (
            _credential_sha256(session.session_id),
            _credential_sha256(session.reference),
            session.session_authority_sha256,
        ):
            self._used_value_digests.discard(digest)

    def _rollback_created_session_locked(
        self,
        session: _SessionRecord,
        *,
        launch_grant_sha256: str,
        idempotency_scope: tuple[
            str,
            tuple[str, ProductRole],
            ProductOperation,
            str,
        ],
    ) -> None:
        """Compensate a Phase 1 create that core initialization could not install."""
        self._sessions_by_reference.pop(session.reference, None)
        self._sessions_by_id.pop(session.session_id, None)
        self._session_authorities.pop(session.session_authority_sha256, None)
        if self._current_by_actor.get(session.actor_key) == session.reference:
            self._current_by_actor.pop(session.actor_key, None)
        self._core_sessions.pop(session.session_id, None)
        self._admission.release_session(session.session_id)
        grant = self._launch_grants.get(launch_grant_sha256)
        if grant is not None and grant.consumed_session_id == session.session_id:
            grant.consumed_session_id = None
        self._idempotency.pop(idempotency_scope, None)
        self._discard_uninstalled_session_values_locked(session)

    def _fail_dispatch(self, command_id: str) -> None:
        with self._lock:
            located = self._locate_command_locked(command_id)
            if located is None:
                return
            session, core, record = located
            if record.terminal or record.dispatch_state is not _DispatchState.QUEUED:
                return
            record.dispatch_state = _DispatchState.ABANDONED
            self._publish_execution_result_locked(
                session,
                core,
                record,
                ProductExecutionResult.failed("worker_dispatch_failed"),
            )

    def _authorized_core_read_locked(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
    ) -> tuple[_SessionRecord, _CoreSessionState]:
        self._require_epoch(expected_epoch, mutation=False)
        now_ms, deadline_ms = self._now()
        actor = self._require_actor(actor_authority, deadline_ms)
        self._expire_and_prune_locked(deadline_ms)
        self._reconcile_due_commands_locked(
            now_ms=now_ms,
            deadline_ms=deadline_ms,
        )
        session = self._require_live_session(
            actor=actor,
            session_authority=session_authority,
            reference=reference,
            deadline_ms=deadline_ms,
        )
        return session, self._require_core_session_locked(session.session_id)

    def _reconcile_due_commands_locked(
        self,
        *,
        now_ms: int | None = None,
        deadline_ms: int | None = None,
    ) -> int:
        if now_ms is None or deadline_ms is None:
            now_ms, deadline_ms = self._now()
        reconciled = 0
        for session in tuple(self._sessions_by_reference.values()):
            core = self._core_sessions.get(session.session_id)
            if core is None or core.active_command_id is None:
                continue
            record = core.commands[core.active_command_id]
            reason = _command_due_reason(record, deadline_ms)
            if reason is None:
                continue
            queued = record.dispatch_state is _DispatchState.QUEUED
            if queued:
                record.dispatch_state = _DispatchState.ABANDONED
            record.fence_open = False
            record.cancel_event.set()
            self._admission.fence_job(record.command_id)
            self._publish_forced_terminal_locked(
                session,
                core,
                record,
                now_ms=now_ms,
                reason=reason,
            )
            if queued:
                self._reconcile_worker_locked(
                    record,
                    ProductExecutionResult.failed(reason),
                )
            reconciled += 1
        return reconciled

    def _publish_forced_terminal_locked(
        self,
        session: _SessionRecord,
        core: _CoreSessionState,
        record: _CommandRecord,
        *,
        now_ms: int,
        reason: str,
    ) -> None:
        if record.terminal:
            return
        self._append_final_output_markers_locked(core, record)
        record.status = "failed"
        record.result = _terminal_result_projection(
            ProductExecutionResult.failed(reason),
            accepted_state_revision=core.accepted_state_revision,
            output_clipped=record.output_clipped,
            usage_pending=(
                record.dispatch_state is _DispatchState.RUNNING
                and not record.worker_reconciled
            ),
        )
        record.terminal_at_ms = now_ms
        record.fence_open = False
        if core.active_command_id == record.command_id:
            core.active_command_id = None
        core.journal.append_event(
            "command.failed",
            criticality=ProductEventCriticality.AUTHORITATIVE,
            command_id=record.command_id,
            data={
                "slot_generation": record.slot_generation,
                "accepted_state_revision": core.accepted_state_revision,
                "outcome": "failed",
                "output_clipped": record.output_clipped,
            },
        )

    def _terminalize_locked(
        self,
        session: _SessionRecord,
        *,
        terminal_at_ms: int,
        terminal_deadline_ms: int,
        terminal_reason: str,
    ) -> _SessionTombstone:
        core = self._core_sessions.get(session.session_id)
        if core is not None and core.active_command_id is not None:
            record = core.commands[core.active_command_id]
            queued = record.dispatch_state is _DispatchState.QUEUED
            if queued:
                record.dispatch_state = _DispatchState.ABANDONED
            record.fence_open = False
            record.cancel_event.set()
            self._admission.fence_job(record.command_id)
            self._publish_forced_terminal_locked(
                session,
                core,
                record,
                now_ms=terminal_at_ms,
                reason="session_terminated",
            )
            if queued:
                self._reconcile_worker_locked(
                    record,
                    ProductExecutionResult.failed("session_terminated"),
                )
        tombstone = super()._terminalize_locked(
            session,
            terminal_at_ms=terminal_at_ms,
            terminal_deadline_ms=terminal_deadline_ms,
            terminal_reason=terminal_reason,
        )
        if core is not None:
            if not core.admission_released:
                self._admission.release_session(session.session_id)
                core.admission_released = True
            core.journal.append_event(
                "session.terminal",
                criticality=ProductEventCriticality.AUTHORITATIVE,
                data={
                    "control_revision": tombstone.control_revision,
                    "terminal_reason": tombstone.terminal_reason,
                    "slot_generation": core.slot_generation,
                    "accepted_state_revision": core.accepted_state_revision,
                },
            )
        return tombstone

    def _expire_and_prune_locked(self, deadline_ms: int) -> int:
        expired = super()._expire_and_prune_locked(deadline_ms)
        retained_ids = set(self._sessions_by_id)
        retained_ids.update(
            tombstone.session_id for tombstone in self._tombstones_by_reference.values()
        )
        for session_id, core in tuple(self._core_sessions.items()):
            outstanding_worker = any(
                not record.worker_reconciled for record in core.commands.values()
            )
            if session_id not in retained_ids and not outstanding_worker:
                self._core_sessions.pop(session_id, None)
        return expired

    def _record_core_error_locked(
        self,
        scope: tuple[str, tuple[str, ProductRole], ProductOperation, str],
        body_sha256: str,
        error: ProductLifecycleError,
        *,
        deadline_ms: int,
        session_id: str | None,
    ) -> None:
        retain_until, retained_session_id = self._failure_retention(
            deadline_ms=deadline_ms,
            session_id=session_id,
        )
        self._record_idempotent_error(
            scope,
            body_sha256,
            error,
            retain_until_deadline_ms=retain_until,
            session_id=retained_session_id,
        )

    def _require_core_session_locked(self, session_id: str) -> _CoreSessionState:
        core = self._core_sessions.get(session_id)
        if core is None:
            raise RuntimeError("product core session state is unavailable")
        return core

    def _locate_command_locked(
        self,
        command_id: str,
    ) -> tuple[_SessionRecord, _CoreSessionState, _CommandRecord] | None:
        for session_id, core in self._core_sessions.items():
            record = core.commands.get(command_id)
            if record is None:
                continue
            session = self._sessions_by_id.get(session_id)
            if session is None:
                # Retained terminal core state has no mutable runtime.
                return None
            return session, core, record
        return None

    def _retained_command_locked(self, command_id: str) -> _CommandRecord | None:
        for core in self._core_sessions.values():
            record = core.commands.get(command_id)
            if record is not None:
                return record
        return None

    def _enrichment_snapshot_locked(
        self,
        session: _SessionRecord,
        core: _CoreSessionState,
    ) -> ProductEnrichmentSessionSnapshot:
        current = self._sessions_by_id.get(session.session_id)
        if current is not session:
            raise ProductEnrichmentSessionUnavailable()
        return ProductEnrichmentSessionSnapshot(
            service_epoch=self._service_epoch,
            session_id=session.session_id,
            incarnation_id=session.reference,
            lifecycle_revision=session.control_revision,
            accepted_state_revision=core.accepted_state_revision,
            lifecycle=ProductEnrichmentLifecycle.ACTIVE,
            foreground_active=core.active_command_id is not None,
            document=cast(
                Mapping[str, JsonValue],
                deepcopy(session.runtime_document),
            ),
        )

    def _install_enrichment_document_locked(
        self,
        session: _SessionRecord,
        core: _CoreSessionState,
        replacement_document: Mapping[str, object],
        applied_event: ProductEnrichmentAppliedEvent,
    ) -> int:
        current = self._sessions_by_id.get(session.session_id)
        if (
            current is not session
            or self._core_sessions.get(session.session_id) is not core
        ):
            raise ProductEnrichmentSessionUnavailable()
        if core.active_command_id is not None:
            raise RuntimeError("foreground command prevents enrichment publication")
        if not isinstance(applied_event, ProductEnrichmentAppliedEvent):
            raise TypeError("applied_event must be ProductEnrichmentAppliedEvent")
        document = _validated_runtime_document(
            replacement_document,
            maximum_bytes=self._core_policy.max_runtime_document_bytes,
        )
        self._validate_state_effect(deepcopy(document))
        expected_revision = core.accepted_state_revision + 1
        if (
            applied_event.session_id != session.session_id
            or applied_event.previous_accepted_state_revision
            != core.accepted_state_revision
            or applied_event.resulting_accepted_state_revision != expected_revision
            or applied_event.replacement_sha256 != _canonical_sha256(document)
        ):
            raise RuntimeError(
                "enrichment event does not identify this state transition"
            )
        core.journal.append_event(
            "state.accepted",
            criticality=ProductEventCriticality.AUTHORITATIVE,
            data={
                "source": "enrichment",
                "proposal_id": applied_event.proposal_id,
                "previous_accepted_state_revision": core.accepted_state_revision,
                "accepted_state_revision": expected_revision,
                "replacement_sha256": applied_event.replacement_sha256,
            },
        )
        session.runtime_document = document
        core.accepted_state_revision = expected_revision
        return expected_revision

    def _command_projection(self, record: _CommandRecord) -> dict[str, object]:
        return {
            "command_id": record.command_id,
            "slot_generation": record.slot_generation,
            "status": record.status,
            "accepted_at_ms": record.accepted_at_ms,
            "deadline_at_ms": record.deadline_at_ms,
            "terminal_at_ms": record.terminal_at_ms,
            "output": {
                "bytes": record.output_bytes,
                "chunks": record.output_chunks,
                "clipped": record.output_clipped,
            },
            "result": deepcopy(record.result),
        }

    def _core_active_snapshot_locked(
        self,
        session: _SessionRecord,
        core: _CoreSessionState,
    ) -> dict[str, object]:
        snapshot = super()._active_snapshot(session)
        journal = core.journal.snapshot()
        snapshot["coordinates"] = {
            "control_revision": session.control_revision,
            "command_slot_generation": core.slot_generation,
            "accepted_state_revision": core.accepted_state_revision,
            "event": journal.events.position.to_dict(),
            "transcript": journal.transcript.position.to_dict(),
        }
        snapshot["command_slot"] = {
            "state": "occupied" if core.active_command_id is not None else "available",
            "generation": core.slot_generation,
            "active_command_id": core.active_command_id,
        }
        latest = (
            core.commands.get(core.latest_command_id)
            if core.latest_command_id is not None
            else None
        )
        snapshot["latest_command"] = (
            self._command_projection(latest) if latest is not None else None
        )
        snapshot["transcript"] = _transcript_page(journal.transcript)
        controls = [
            "snapshot",
            "events",
            "transcript",
            "submit",
            "reset",
            "switch",
            "close",
        ]
        if core.active_command_id is not None:
            controls.append("cancel")
        snapshot["available_controls"] = controls
        return snapshot

    def _core_terminal_snapshot_locked(
        self,
        tombstone: _SessionTombstone,
    ) -> dict[str, object]:
        snapshot = super()._terminal_snapshot(tombstone)
        core = self._core_sessions.get(tombstone.session_id)
        if core is None:
            return snapshot
        journal = core.journal.snapshot()
        snapshot["coordinates"] = {
            "control_revision": tombstone.control_revision,
            "command_slot_generation": core.slot_generation,
            "accepted_state_revision": core.accepted_state_revision,
            "event": journal.events.position.to_dict(),
            "transcript": journal.transcript.position.to_dict(),
        }
        snapshot["command_slot"] = {
            "state": "terminal",
            "generation": core.slot_generation,
            "active_command_id": None,
        }
        latest = (
            core.commands.get(core.latest_command_id)
            if core.latest_command_id is not None
            else None
        )
        snapshot["latest_command"] = (
            self._command_projection(latest) if latest is not None else None
        )
        snapshot["transcript"] = _transcript_page(journal.transcript)
        return snapshot

    def _new_core_identity_locked(
        self,
        purpose: str,
        prefix: str,
    ) -> tuple[str, str]:
        opaque = self._new_opaque_value(f"product_{purpose}")
        digest = hashlib.sha256(opaque.encode("utf-8")).hexdigest()
        return f"{prefix}:{digest[:32]}", digest

    def _journal_clock_ms(self) -> int:
        # Aggregate operations read both clocks once before mutation. Journal
        # appends reuse that validated projection so a second clock read cannot
        # split an authoritative transition from its canonical event.
        return self._started_at_ms + (
            self._last_deadline_ms - self._started_deadline_ms
        )


def _terminal_result_projection(
    result: ProductExecutionResult,
    *,
    accepted_state_revision: int,
    output_clipped: bool,
    usage_pending: bool = False,
) -> dict[str, object]:
    usage: dict[str, object]
    if usage_pending:
        usage = {
            "state": "pending",
            "tokens": None,
            "spend_units": None,
        }
    else:
        usage = {
            "state": "settled",
            "tokens": result.usage_tokens,
            "spend_units": result.usage_spend_units,
        }
    projection: dict[str, object] = {
        "outcome": result.outcome.value,
        "output_clipped": output_clipped,
        "usage": usage,
    }
    if result.outcome is ProductExecutionOutcome.COMPLETED:
        assert result.guest_termination is not None
        assert result.state_effect is not None
        termination: dict[str, object] = {
            "kind": result.guest_termination.kind.value,
        }
        if result.guest_termination.exit_status is not None:
            termination["exit_status"] = result.guest_termination.exit_status
        if result.guest_termination.signal is not None:
            termination["signal"] = result.guest_termination.signal
        state_effect: dict[str, object] = {"kind": result.state_effect.kind.value}
        if result.state_effect.kind is ProductStateEffectKind.COMMITTED:
            state_effect["accepted_state_revision"] = accepted_state_revision
        if result.state_effect.safe_reason is not None:
            state_effect["reason"] = result.state_effect.safe_reason
        projection["guest_termination"] = termination
        projection["state_effect"] = state_effect
    else:
        projection["reason"] = result.reason
    return projection


def _with_rejected_state_effect(
    result: ProductExecutionResult,
    reason: str,
) -> ProductExecutionResult:
    if result.outcome is not ProductExecutionOutcome.COMPLETED:
        raise ValueError("only completed execution can reject a state effect")
    assert result.guest_termination is not None
    return ProductExecutionResult(
        outcome=ProductExecutionOutcome.COMPLETED,
        guest_termination=result.guest_termination,
        state_effect=ProductStateEffect(
            ProductStateEffectKind.REJECTED,
            safe_reason=reason,
        ),
        usage_tokens=result.usage_tokens,
        usage_spend_units=result.usage_spend_units,
    )


def _event_page(page: EventJournalSnapshot) -> dict[str, object]:
    return {
        "kind": "event_page",
        "events": [event.to_dict() for event in page.events],
        "retention_floor": page.retention_floor.to_dict(),
        "position": page.position.to_dict(),
        "retained_payload_bytes": page.retained_payload_bytes,
    }


def _transcript_page(page: TranscriptJournalSnapshot) -> dict[str, object]:
    return {
        "kind": "transcript_page",
        "entries": [entry.to_dict() for entry in page.entries],
        "retention_floor": page.retention_floor.to_dict(),
        "position": page.position.to_dict(),
        "omitted_before_entries": page.omitted_before_entries,
        "omitted_after_entries": page.omitted_after_entries,
        "retained_text_bytes": page.retained_text_bytes,
    }


def _admission_error(denial: ProductAdmissionDenied) -> ProductLifecycleError:
    if denial.reason in {
        AdmissionDenialReason.TOKEN_BUDGET_EXHAUSTED,
        AdmissionDenialReason.SPEND_BUDGET_EXHAUSTED,
    }:
        return ProductLifecycleError(
            code="hosted_budget_exhausted",
            message="Product hosted-work budget is temporarily unavailable.",
            status=503,
            retry="after_delay",
            retry_after_ms=1_000,
        )
    if denial.retryable:
        return ProductLifecycleError(
            code="capacity_exhausted",
            message="Product command capacity is temporarily exhausted.",
            status=503,
            retry="after_delay",
            retry_after_ms=1_000,
        )
    return _error(
        "resource_unavailable",
        "Product command resources are unavailable.",
        status=409,
        retry="resume_snapshot",
    )


def _effective_positive_limit(
    limits: Mapping[str, int],
    name: str,
    policy_limit: int,
) -> int:
    configured = limits.get(name)
    if configured is None:
        return policy_limit
    return min(policy_limit, configured)


def _effective_optional_positive_limit(
    limits: Mapping[str, int],
    name: str,
    policy_limit: int | None,
) -> int | None:
    configured = limits.get(name)
    if configured is None:
        return policy_limit
    return configured if policy_limit is None else min(policy_limit, configured)


def _require_positive_bounded_policy_limit(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value >= 2**63
    ):
        raise ValueError(f"{name} must be a positive bounded integer")


def _command_due_reason(
    record: _CommandRecord,
    deadline_ms: int,
) -> str | None:
    if (
        record.cancel_deadline_ms is not None
        and deadline_ms >= record.cancel_deadline_ms
    ):
        return "cancellation_deadline_exceeded"
    if deadline_ms >= record.deadline_deadline_ms:
        return "command_deadline_exceeded"
    return None


def _require_positive_or_zero_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error(
            "invalid_request",
            "The command slot generation is invalid.",
            status=400,
        )
    return value


def _require_positive_generation(value: object) -> int:
    generation = _require_positive_or_zero_generation(value)
    if generation == 0:
        raise _error(
            "invalid_request",
            "The command slot generation is invalid.",
            status=400,
        )
    return generation


def _request_id(command_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{command_id}\0{key}".encode()).hexdigest()[:24]
    return f"request:{digest}"


def _validated_runtime_document(
    value: object,
    *,
    maximum_bytes: int,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("committed provisional state must be a mapping")
    _require_string_mapping_keys(value)
    try:
        canonical = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as error:
        raise ValueError(
            "committed provisional state must contain JSON values"
        ) from error
    if len(canonical) > maximum_bytes:
        raise ValueError("committed provisional state exceeds the size limit")
    parsed = json.loads(canonical)
    if not isinstance(parsed, dict):
        raise ValueError("committed provisional state must be an object")
    return parsed


def _require_string_mapping_keys(value: object) -> None:
    pending: list[object] = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise ValueError("committed provisional state keys must be strings")
                pending.append(nested)
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            pending.extend(current)


def _bounded_utf8_prefix(text: str, maximum_bytes: int) -> tuple[str, int]:
    encoded = text.encode("utf-8")
    if maximum_bytes <= 0:
        return "", len(encoded)
    if len(encoded) <= maximum_bytes:
        return text, 0
    prefix = encoded[:maximum_bytes]
    while prefix:
        try:
            projected = prefix.decode("utf-8")
            return projected, len(encoded) - len(prefix)
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return "", len(encoded)


def _required_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{name} is not an object")
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} is not a string")
    return value


__all__ = ["ProductCorePolicy", "ProductSessionCoreService"]
