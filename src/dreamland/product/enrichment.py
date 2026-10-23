"""Explicit, product-only enrichment proposal coordination.

This module owns no scheduler, worker, timer, listener, provider, or lifecycle
registry.  A caller explicitly invokes :meth:`ProductEnrichmentCoordinator.run`
and supplies an aggregate adapter, generator, admission ledger, document
validator, and post-commit observer.  Generation occurs after the aggregate
snapshot method has returned; compare-and-apply and canonical event publication
occur together through the adapter's documented aggregate-lock transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, TypeAlias

from dreamland.product.admission import (
    ProductAdmissionDenied,
    ProductAdmissionLedger,
    ProductJobCategory,
)

_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_COUNTER = 2**63 - 1
_MAX_SAFE_INTEGER = 2**53 - 1
_ABSOLUTE_DOCUMENT_BYTES = 16 * 1024 * 1024
_ABSOLUTE_DOCUMENT_DEPTH = 64
_ABSOLUTE_DOCUMENT_ITEMS = 250_000

JsonValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | tuple["JsonValue", ...]
    | Mapping[str, "JsonValue"]
)


class ProductEnrichmentLifecycle(StrEnum):
    """Minimal lifecycle truth required at proposal publication."""

    ACTIVE = "active"
    CLOSED = "closed"
    REPLACED = "replaced"


class ProductEnrichmentOutcomeKind(StrEnum):
    """Stable outcomes from one explicit enrichment attempt."""

    APPLIED = "applied"
    DEFERRED = "deferred"
    FENCED = "fenced"
    UNCHANGED = "unchanged"
    REJECTED = "rejected"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"


class ProductEnrichmentReason(StrEnum):
    """Safe reasons that do not expose generated or aggregate state."""

    FOREGROUND_ACTIVE = "foreground_active"
    SESSION_UNAVAILABLE = "session_unavailable"
    SESSION_CHANGED = "session_changed"
    LIFECYCLE_CHANGED = "lifecycle_changed"
    SESSION_CLOSED = "session_closed"
    SESSION_REPLACED = "session_replaced"
    STALE_ACCEPTED_REVISION = "stale_accepted_revision"
    EXPLICIT_FENCE = "explicit_fence"
    VALIDATION_FAILED = "validation_failed"
    GENERATION_FAILED = "generation_failed"
    INVALID_GENERATION = "invalid_generation"


class ProductEnrichmentDenialReason(StrEnum):
    """Safe coordinator-local denials, separate from provider admission."""

    PROPOSAL_CAPACITY_EXHAUSTED = "proposal_capacity_exhausted"
    PROPOSAL_IDENTITY_CONFLICT = "proposal_identity_conflict"
    PROPOSAL_NOT_FOUND = "proposal_not_found"


_DENIAL_MESSAGES = {
    ProductEnrichmentDenialReason.PROPOSAL_CAPACITY_EXHAUSTED: (
        "Product enrichment proposal capacity is exhausted."
    ),
    ProductEnrichmentDenialReason.PROPOSAL_IDENTITY_CONFLICT: (
        "The enrichment proposal identity is bound to another session."
    ),
    ProductEnrichmentDenialReason.PROPOSAL_NOT_FOUND: (
        "The enrichment proposal is unavailable."
    ),
}


class ProductEnrichmentDenied(RuntimeError):
    """A bounded coordinator-local operation was denied safely."""

    def __init__(self, reason: ProductEnrichmentDenialReason) -> None:
        super().__init__(_DENIAL_MESSAGES[reason])
        self.reason = reason
        self.message = _DENIAL_MESSAGES[reason]


class ProductEnrichmentSessionUnavailable(RuntimeError):
    """Aggregate adapter signal that a session cannot be observed."""


class ProductEnrichmentPublicationError(RuntimeError):
    """State and its canonical event committed, but an observer callback failed."""

    def __init__(self, applied: ProductEnrichmentAppliedEvent) -> None:
        super().__init__(
            "Product enrichment and its canonical event were applied, but a "
            "post-commit observer failed."
        )
        self.applied = applied


@dataclass(frozen=True, slots=True)
class ProductEnrichmentPolicy:
    """Hard limits for one explicitly constructed coordinator."""

    max_proposals: int = 256
    max_document_bytes: int = 4 * 1024 * 1024
    max_document_depth: int = 32
    max_document_items: int = 100_000
    reserved_tokens_per_proposal: int = 4_096
    reserved_spend_units_per_proposal: int = 5_000

    def __post_init__(self) -> None:
        _positive(self.max_proposals, "max_proposals")
        _positive(self.max_document_bytes, "max_document_bytes")
        _positive(self.max_document_depth, "max_document_depth")
        _positive(self.max_document_items, "max_document_items")
        _nonnegative(
            self.reserved_tokens_per_proposal,
            "reserved_tokens_per_proposal",
        )
        _nonnegative(
            self.reserved_spend_units_per_proposal,
            "reserved_spend_units_per_proposal",
        )
        if self.max_document_bytes > _ABSOLUTE_DOCUMENT_BYTES:
            raise ValueError("max_document_bytes exceeds the absolute safety bound")
        if self.max_document_depth > _ABSOLUTE_DOCUMENT_DEPTH:
            raise ValueError("max_document_depth exceeds the absolute safety bound")
        if self.max_document_items > _ABSOLUTE_DOCUMENT_ITEMS:
            raise ValueError("max_document_items exceeds the absolute safety bound")


@dataclass(frozen=True, slots=True)
class ProductEnrichmentSessionSnapshot:
    """Immutable isolated state captured under the aggregate lock."""

    service_epoch: str
    session_id: str
    incarnation_id: str
    lifecycle_revision: int
    accepted_state_revision: int
    lifecycle: ProductEnrichmentLifecycle
    foreground_active: bool
    document: Mapping[str, JsonValue] = field(repr=False)
    document_sha256: str = field(init=False)
    document_bytes: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _identity(self.service_epoch, "service_epoch")
        _identity(self.session_id, "session_id")
        _identity(self.incarnation_id, "incarnation_id")
        _positive(self.lifecycle_revision, "lifecycle_revision")
        _nonnegative(self.accepted_state_revision, "accepted_state_revision")
        if not isinstance(self.lifecycle, ProductEnrichmentLifecycle):
            raise ValueError("enrichment lifecycle is invalid")
        if not isinstance(self.foreground_active, bool):
            raise ValueError("foreground_active must be boolean")
        frozen, canonical = _bounded_document(
            self.document,
            max_bytes=_ABSOLUTE_DOCUMENT_BYTES,
            max_depth=_ABSOLUTE_DOCUMENT_DEPTH,
            max_items=_ABSOLUTE_DOCUMENT_ITEMS,
        )
        object.__setattr__(self, "document", frozen)
        object.__setattr__(
            self, "document_sha256", hashlib.sha256(canonical).hexdigest()
        )
        object.__setattr__(self, "document_bytes", len(canonical))


@dataclass(frozen=True, slots=True)
class ProductEnrichmentRequest:
    """Immutable generator input; it carries no live mutation authority."""

    proposal_id: str
    snapshot: ProductEnrichmentSessionSnapshot

    def __post_init__(self) -> None:
        _identity(self.proposal_id, "proposal_id")
        if not isinstance(self.snapshot, ProductEnrichmentSessionSnapshot):
            raise TypeError("snapshot must be ProductEnrichmentSessionSnapshot")


@dataclass(frozen=True, slots=True)
class ProductEnrichmentGeneration:
    """A generator candidate plus bounded actual resource usage."""

    replacement_document: Mapping[str, JsonValue] = field(repr=False)
    usage_tokens: int = 0
    usage_spend_units: int = 0
    replacement_sha256: str = field(init=False)
    document_bytes: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _nonnegative(self.usage_tokens, "usage_tokens")
        _nonnegative(self.usage_spend_units, "usage_spend_units")
        frozen, canonical = _bounded_document(
            self.replacement_document,
            max_bytes=_ABSOLUTE_DOCUMENT_BYTES,
            max_depth=_ABSOLUTE_DOCUMENT_DEPTH,
            max_items=_ABSOLUTE_DOCUMENT_ITEMS,
        )
        object.__setattr__(self, "replacement_document", frozen)
        object.__setattr__(
            self,
            "replacement_sha256",
            hashlib.sha256(canonical).hexdigest(),
        )
        object.__setattr__(self, "document_bytes", len(canonical))


@dataclass(frozen=True, slots=True)
class ProductEnrichmentProposal:
    """Immutable generated proposal bound to one exact base snapshot."""

    proposal_id: str
    base: ProductEnrichmentSessionSnapshot
    replacement_document: Mapping[str, JsonValue] = field(repr=False)
    replacement_sha256: str
    generated_at_ms: int

    def __post_init__(self) -> None:
        _identity(self.proposal_id, "proposal_id")
        if not isinstance(self.base, ProductEnrichmentSessionSnapshot):
            raise TypeError("base must be ProductEnrichmentSessionSnapshot")
        frozen, canonical = _bounded_document(
            self.replacement_document,
            max_bytes=_ABSOLUTE_DOCUMENT_BYTES,
            max_depth=_ABSOLUTE_DOCUMENT_DEPTH,
            max_items=_ABSOLUTE_DOCUMENT_ITEMS,
        )
        expected_sha256 = hashlib.sha256(canonical).hexdigest()
        if self.replacement_sha256 != expected_sha256:
            raise ValueError("replacement_sha256 does not identify the document")
        _nonnegative(self.generated_at_ms, "generated_at_ms")
        object.__setattr__(self, "replacement_document", frozen)


@dataclass(frozen=True, slots=True)
class ProductEnrichmentAppliedEvent:
    """Safe canonical record of one accepted enrichment transition."""

    proposal_id: str
    session_id: str
    previous_accepted_state_revision: int
    resulting_accepted_state_revision: int
    replacement_sha256: str
    source: str = "product_enrichment"

    def __post_init__(self) -> None:
        _identity(self.proposal_id, "proposal_id")
        _identity(self.session_id, "session_id")
        _nonnegative(
            self.previous_accepted_state_revision,
            "previous_accepted_state_revision",
        )
        _positive(
            self.resulting_accepted_state_revision,
            "resulting_accepted_state_revision",
        )
        if (
            self.resulting_accepted_state_revision
            != self.previous_accepted_state_revision + 1
        ):
            raise ValueError("an enrichment event must advance one accepted revision")
        if not isinstance(
            self.replacement_sha256, str
        ) or not _SHA256_PATTERN.fullmatch(self.replacement_sha256):
            raise ValueError("replacement_sha256 is invalid")
        if self.source != "product_enrichment":
            raise ValueError("enrichment event source is invalid")


@dataclass(frozen=True, slots=True)
class ProductEnrichmentAttempt:
    """Immutable outcome of one explicit generation or apply attempt."""

    kind: ProductEnrichmentOutcomeKind
    reason: ProductEnrichmentReason | None
    proposal: ProductEnrichmentProposal | None
    accepted_state_revision: int | None
    replayed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProductEnrichmentOutcomeKind):
            raise ValueError("enrichment outcome kind is invalid")
        if self.reason is not None and not isinstance(
            self.reason, ProductEnrichmentReason
        ):
            raise ValueError("enrichment outcome reason is invalid")
        if self.proposal is not None and not isinstance(
            self.proposal, ProductEnrichmentProposal
        ):
            raise TypeError("proposal must be ProductEnrichmentProposal")
        if self.accepted_state_revision is not None:
            _nonnegative(
                self.accepted_state_revision,
                "accepted_state_revision",
            )
        if not isinstance(self.replayed, bool):
            raise ValueError("replayed must be boolean")


class ProductEnrichmentGenerator(Protocol):
    """Generate against an isolated snapshot, never the live aggregate."""

    def generate(
        self, request: ProductEnrichmentRequest
    ) -> ProductEnrichmentGeneration: ...


class ProductEnrichmentSessionAccess(Protocol):
    """Live session access valid only inside an aggregate transaction."""

    @property
    def enrichment_snapshot(self) -> ProductEnrichmentSessionSnapshot: ...

    def install_enrichment_document(
        self,
        replacement_document: Mapping[str, object],
        applied_event: ProductEnrichmentAppliedEvent,
    ) -> int:
        """Atomically replace state, advance its revision, and publish the event."""
        ...


class ProductEnrichmentAggregate(Protocol):
    """Adapter seam into the owning product-session aggregate."""

    def capture_enrichment_snapshot(
        self, session_id: str
    ) -> ProductEnrichmentSessionSnapshot:
        """Copy one session under the aggregate lock, then release it."""
        ...

    def transact_enrichment(
        self,
        session_id: str,
        operation: Callable[[ProductEnrichmentSessionAccess], ProductEnrichmentAttempt],
    ) -> ProductEnrichmentAttempt:
        """Invoke ``operation`` exactly once while holding the aggregate lock."""
        ...


class _ProposalState(StrEnum):
    RESERVED = "reserved"
    GENERATING = "generating"
    READY = "ready"
    TERMINAL = "terminal"


@dataclass(slots=True)
class _ProposalRecord:
    proposal_id: str
    session_id: str
    request: ProductEnrichmentRequest
    fence: threading.Event = field(default_factory=threading.Event, repr=False)
    generate_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    apply_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    state: _ProposalState = _ProposalState.RESERVED
    proposal: ProductEnrichmentProposal | None = None
    result: ProductEnrichmentAttempt | None = None


class ProductEnrichmentCoordinator:
    """Coordinate explicit generation, atomic publication, and observation."""

    def __init__(
        self,
        *,
        aggregate: ProductEnrichmentAggregate,
        generator: ProductEnrichmentGenerator,
        admission: ProductAdmissionLedger,
        validate_replacement: Callable[[Mapping[str, object]], None],
        on_applied: Callable[[ProductEnrichmentAppliedEvent], None],
        policy: ProductEnrichmentPolicy | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not callable(getattr(aggregate, "capture_enrichment_snapshot", None)):
            raise TypeError("aggregate must implement capture_enrichment_snapshot")
        if not callable(getattr(aggregate, "transact_enrichment", None)):
            raise TypeError("aggregate must implement transact_enrichment")
        if not callable(getattr(generator, "generate", None)):
            raise TypeError("generator must implement generate")
        if not isinstance(admission, ProductAdmissionLedger):
            raise TypeError("admission must be ProductAdmissionLedger")
        if not callable(validate_replacement):
            raise TypeError("validate_replacement must be callable")
        if not callable(on_applied):
            raise TypeError("on_applied must be callable")
        if policy is not None and not isinstance(policy, ProductEnrichmentPolicy):
            raise TypeError("policy must be ProductEnrichmentPolicy")
        if clock_ms is not None and not callable(clock_ms):
            raise TypeError("clock_ms must be callable")
        self._aggregate = aggregate
        self._generator = generator
        self._admission = admission
        self._validate_replacement = validate_replacement
        self._on_applied = on_applied
        self._policy = policy or ProductEnrichmentPolicy()
        self._clock_ms = clock_ms or _system_clock_ms
        self._records: dict[str, _ProposalRecord] = {}
        self._lock = threading.RLock()

    @property
    def policy(self) -> ProductEnrichmentPolicy:
        return self._policy

    def run(self, *, proposal_id: str, session_id: str) -> ProductEnrichmentAttempt:
        """Explicitly generate once and attempt compare-and-apply."""
        proposal_identity = _identity(proposal_id, "proposal_id")
        session_identity = _identity(session_id, "session_id")
        with self._lock:
            existing = self._records.get(proposal_identity)
        if existing is not None:
            return self._continue_existing(existing, session_identity)

        try:
            captured = self._aggregate.capture_enrichment_snapshot(session_identity)
        except ProductEnrichmentSessionUnavailable:
            return _attempt(
                ProductEnrichmentOutcomeKind.FENCED,
                ProductEnrichmentReason.SESSION_UNAVAILABLE,
            )
        try:
            snapshot = self._validated_snapshot(captured, session_identity)
        except ProductEnrichmentSessionUnavailable:
            return _attempt(
                ProductEnrichmentOutcomeKind.FENCED,
                ProductEnrichmentReason.SESSION_UNAVAILABLE,
            )
        initial = self._pre_generation_outcome(snapshot)
        if initial is not None:
            return initial

        request = ProductEnrichmentRequest(proposal_identity, snapshot)
        with self._lock:
            existing = self._records.get(proposal_identity)
            if existing is None:
                if len(self._records) >= self._policy.max_proposals:
                    raise ProductEnrichmentDenied(
                        ProductEnrichmentDenialReason.PROPOSAL_CAPACITY_EXHAUSTED
                    )
                reservation = self._admission.reserve_job(
                    logical_job_id=proposal_identity,
                    session_id=session_identity,
                    category=ProductJobCategory.PRODUCT_ENRICHMENT,
                    reserved_tokens=self._policy.reserved_tokens_per_proposal,
                    reserved_spend_units=self._policy.reserved_spend_units_per_proposal,
                )
                if reservation.replayed:
                    raise ProductEnrichmentDenied(
                        ProductEnrichmentDenialReason.PROPOSAL_IDENTITY_CONFLICT
                    )
                record = _ProposalRecord(
                    proposal_id=proposal_identity,
                    session_id=session_identity,
                    request=request,
                )
                self._records[proposal_identity] = record

        if existing is not None:
            return self._continue_existing(existing, session_identity)

        return self._claim_generate_and_apply(record)

    def retry_apply(self, proposal_id: str) -> ProductEnrichmentAttempt:
        """Retry only publication of an already generated deferred proposal."""
        proposal_identity = _identity(proposal_id, "proposal_id")
        with self._lock:
            record = self._records.get(proposal_identity)
            if record is None:
                raise ProductEnrichmentDenied(
                    ProductEnrichmentDenialReason.PROPOSAL_NOT_FOUND
                )
            if record.state is _ProposalState.READY:
                pass
            elif record.result is not None:
                return replace(record.result, replayed=True)
            else:
                return _attempt(
                    ProductEnrichmentOutcomeKind.IN_PROGRESS,
                    None,
                    replayed=True,
                )
        return self._attempt_apply(record, replayed=True)

    def proposal(self, proposal_id: str) -> ProductEnrichmentProposal | None:
        """Return the immutable generated proposal when retained."""
        proposal_identity = _identity(proposal_id, "proposal_id")
        with self._lock:
            record = self._records.get(proposal_identity)
            return None if record is None else record.proposal

    def fence_session(self, session_id: str) -> int:
        """Fence retained work for close/replacement without reaping workers."""
        session_identity = _identity(session_id, "session_id")
        with self._lock:
            records = tuple(
                record
                for record in self._records.values()
                if record.session_id == session_identity
                and record.state is not _ProposalState.TERMINAL
            )
        fenced_count = 0
        for record in records:
            with record.apply_lock:
                with self._lock:
                    if record.state is _ProposalState.TERMINAL:
                        continue
                    state = record.state
                    proposal = record.proposal
                    record.fence.set()
                self._admission.fence_job(record.proposal_id)
                if state is _ProposalState.RESERVED:
                    self._admission.settle_job(
                        record.proposal_id,
                        actual_tokens=0,
                        actual_spend_units=0,
                    )
                    result = _attempt(
                        ProductEnrichmentOutcomeKind.FENCED,
                        ProductEnrichmentReason.EXPLICIT_FENCE,
                    )
                elif state is _ProposalState.READY:
                    result = _attempt(
                        ProductEnrichmentOutcomeKind.FENCED,
                        ProductEnrichmentReason.EXPLICIT_FENCE,
                        proposal=proposal,
                    )
                else:
                    result = None
                if result is not None:
                    with self._lock:
                        if record.state is not _ProposalState.TERMINAL:
                            self._terminalize_locked(record, result)
                fenced_count += 1
        return fenced_count

    def _continue_existing(
        self, record: _ProposalRecord, session_id: str
    ) -> ProductEnrichmentAttempt:
        with self._lock:
            if record.session_id != session_id:
                raise ProductEnrichmentDenied(
                    ProductEnrichmentDenialReason.PROPOSAL_IDENTITY_CONFLICT
                )
            if record.state is _ProposalState.RESERVED:
                should_generate = True
                should_apply = False
            elif record.state is _ProposalState.READY:
                should_generate = False
                should_apply = True
            elif record.result is not None:
                return replace(record.result, replayed=True)
            else:
                return _attempt(
                    ProductEnrichmentOutcomeKind.IN_PROGRESS,
                    None,
                    replayed=True,
                )
        if should_generate:
            return self._claim_generate_and_apply(record)
        if should_apply:
            return self._attempt_apply(record, replayed=True)
        raise RuntimeError("unreachable enrichment proposal state")

    def _claim_generate_and_apply(
        self, record: _ProposalRecord
    ) -> ProductEnrichmentAttempt:
        with record.generate_lock:
            with self._lock:
                if record.state is _ProposalState.TERMINAL:
                    if record.result is None:
                        raise RuntimeError("terminal proposal is missing its result")
                    return replace(record.result, replayed=True)
                if record.state is _ProposalState.READY:
                    should_apply = True
                else:
                    should_apply = False
                    record.state = _ProposalState.GENERATING
            if should_apply:
                return self._attempt_apply(record, replayed=True)
            if record.fence.is_set():
                return self._settle_unclaimed_fence(record)
            try:
                self._admission.claim_worker(record.proposal_id)
            except ProductAdmissionDenied:
                with self._lock:
                    if record.fence.is_set():
                        return self._settle_unclaimed_fence(record)
                    record.state = _ProposalState.RESERVED
                raise
            if record.fence.is_set():
                self._admission.settle_and_reap(
                    record.proposal_id,
                    actual_tokens=0,
                    actual_spend_units=0,
                )
                result = _attempt(
                    ProductEnrichmentOutcomeKind.FENCED,
                    ProductEnrichmentReason.EXPLICIT_FENCE,
                )
                with self._lock:
                    self._terminalize_locked(record, result)
                return result
            return self._generate_and_apply(record)

    def _settle_unclaimed_fence(
        self, record: _ProposalRecord
    ) -> ProductEnrichmentAttempt:
        self._admission.settle_job(
            record.proposal_id,
            actual_tokens=0,
            actual_spend_units=0,
        )
        result = _attempt(
            ProductEnrichmentOutcomeKind.FENCED,
            ProductEnrichmentReason.EXPLICIT_FENCE,
        )
        with self._lock:
            self._terminalize_locked(record, result)
        return result

    def _generate_and_apply(self, record: _ProposalRecord) -> ProductEnrichmentAttempt:
        generation: ProductEnrichmentGeneration | None = None
        generation_reason: ProductEnrichmentReason | None = None
        fatal_generation_error: BaseException | None = None
        try:
            generated = self._generator.generate(record.request)
            if not isinstance(generated, ProductEnrichmentGeneration):
                generation_reason = ProductEnrichmentReason.INVALID_GENERATION
            elif (
                generated.usage_tokens > self._policy.reserved_tokens_per_proposal
                or generated.usage_spend_units
                > self._policy.reserved_spend_units_per_proposal
                or generated.document_bytes > self._policy.max_document_bytes
            ):
                generation_reason = ProductEnrichmentReason.INVALID_GENERATION
            else:
                try:
                    _bounded_document(
                        generated.replacement_document,
                        max_bytes=self._policy.max_document_bytes,
                        max_depth=self._policy.max_document_depth,
                        max_items=self._policy.max_document_items,
                    )
                except (TypeError, ValueError):
                    generation_reason = ProductEnrichmentReason.INVALID_GENERATION
                else:
                    generation = generated
        except BaseException as error:
            generation_reason = ProductEnrichmentReason.GENERATION_FAILED
            if not isinstance(error, Exception):
                fatal_generation_error = error

        if generation is None:
            self._admission.settle_and_reap(
                record.proposal_id,
                actual_tokens=self._policy.reserved_tokens_per_proposal,
                actual_spend_units=self._policy.reserved_spend_units_per_proposal,
            )
            result = _attempt(
                ProductEnrichmentOutcomeKind.FAILED,
                generation_reason or ProductEnrichmentReason.INVALID_GENERATION,
            )
            with self._lock:
                self._terminalize_locked(record, result)
            if fatal_generation_error is not None:
                raise fatal_generation_error
            return result

        self._admission.settle_and_reap(
            record.proposal_id,
            actual_tokens=generation.usage_tokens,
            actual_spend_units=generation.usage_spend_units,
        )
        fatal_validation_error: BaseException | None = None
        try:
            self._validate_replacement(
                _allocate_mapping(generation.replacement_document)
            )
        except BaseException as error:
            if not isinstance(error, Exception):
                fatal_validation_error = error
            result = _attempt(
                ProductEnrichmentOutcomeKind.REJECTED,
                ProductEnrichmentReason.VALIDATION_FAILED,
            )
            with self._lock:
                self._terminalize_locked(record, result)
            if fatal_validation_error is not None:
                raise fatal_validation_error from None
            return result

        fatal_proposal_error: BaseException | None = None
        try:
            proposal = ProductEnrichmentProposal(
                proposal_id=record.proposal_id,
                base=record.request.snapshot,
                replacement_document=generation.replacement_document,
                replacement_sha256=generation.replacement_sha256,
                generated_at_ms=self._read_clock(),
            )
        except BaseException as error:
            if not isinstance(error, Exception):
                fatal_proposal_error = error
            result = _attempt(
                ProductEnrichmentOutcomeKind.FAILED,
                ProductEnrichmentReason.INVALID_GENERATION,
            )
            with self._lock:
                self._terminalize_locked(record, result)
            if fatal_proposal_error is not None:
                raise fatal_proposal_error from None
            return result
        with self._lock:
            record.proposal = proposal
            if record.fence.is_set():
                result = _attempt(
                    ProductEnrichmentOutcomeKind.FENCED,
                    ProductEnrichmentReason.EXPLICIT_FENCE,
                    proposal=proposal,
                )
                self._terminalize_locked(record, result)
                return result
            record.state = _ProposalState.READY
        return self._attempt_apply(record, replayed=False)

    def _attempt_apply(
        self,
        record: _ProposalRecord,
        *,
        replayed: bool,
    ) -> ProductEnrichmentAttempt:
        with record.apply_lock:
            with self._lock:
                if record.state is _ProposalState.TERMINAL:
                    if record.result is None:
                        raise RuntimeError("terminal proposal is missing its result")
                    return replace(record.result, replayed=True)
            return self._attempt_apply_serialized(record, replayed=replayed)

    def _attempt_apply_serialized(
        self,
        record: _ProposalRecord,
        *,
        replayed: bool,
    ) -> ProductEnrichmentAttempt:
        proposal = record.proposal
        if proposal is None:
            return _attempt(
                ProductEnrichmentOutcomeKind.IN_PROGRESS,
                None,
                replayed=True,
            )

        applied_event: ProductEnrichmentAppliedEvent | None = None

        def apply_locked(
            access: ProductEnrichmentSessionAccess,
        ) -> ProductEnrichmentAttempt:
            nonlocal applied_event
            current = access.enrichment_snapshot
            fenced = self._publication_fence(current, proposal, record)
            if fenced is not None:
                return replace(fenced, replayed=replayed)
            if current.foreground_active:
                return _attempt(
                    ProductEnrichmentOutcomeKind.DEFERRED,
                    ProductEnrichmentReason.FOREGROUND_ACTIVE,
                    proposal=proposal,
                    accepted_state_revision=current.accepted_state_revision,
                    replayed=replayed,
                )
            if current.document_sha256 == proposal.replacement_sha256:
                return _attempt(
                    ProductEnrichmentOutcomeKind.UNCHANGED,
                    None,
                    proposal=proposal,
                    accepted_state_revision=current.accepted_state_revision,
                    replayed=replayed,
                )
            expected_revision = current.accepted_state_revision + 1
            event = ProductEnrichmentAppliedEvent(
                proposal_id=proposal.proposal_id,
                session_id=current.session_id,
                previous_accepted_state_revision=current.accepted_state_revision,
                resulting_accepted_state_revision=expected_revision,
                replacement_sha256=proposal.replacement_sha256,
            )
            resulting_revision = access.install_enrichment_document(
                _allocate_mapping(proposal.replacement_document),
                event,
            )
            if resulting_revision != expected_revision:
                raise RuntimeError(
                    "aggregate adapter did not increment accepted revision exactly once"
                )
            applied_event = event
            return _attempt(
                ProductEnrichmentOutcomeKind.APPLIED,
                None,
                proposal=proposal,
                accepted_state_revision=resulting_revision,
                replayed=replayed,
            )

        try:
            result = self._aggregate.transact_enrichment(
                record.session_id, apply_locked
            )
        except ProductEnrichmentSessionUnavailable:
            result = _attempt(
                ProductEnrichmentOutcomeKind.FENCED,
                ProductEnrichmentReason.SESSION_UNAVAILABLE,
                proposal=proposal,
                replayed=replayed,
            )

        if (
            result.kind is ProductEnrichmentOutcomeKind.APPLIED
            and applied_event is None
        ):
            raise RuntimeError(
                "aggregate returned an applied result without publishing its event"
            )

        with self._lock:
            if result.kind is ProductEnrichmentOutcomeKind.DEFERRED:
                record.result = result
                record.state = _ProposalState.READY
            else:
                self._terminalize_locked(record, result)

        if applied_event is not None:
            try:
                self._on_applied(applied_event)
            except Exception as error:
                raise ProductEnrichmentPublicationError(applied_event) from error
        return result

    def _publication_fence(
        self,
        current: ProductEnrichmentSessionSnapshot,
        proposal: ProductEnrichmentProposal,
        record: _ProposalRecord,
    ) -> ProductEnrichmentAttempt | None:
        base = proposal.base
        if record.fence.is_set():
            reason = ProductEnrichmentReason.EXPLICIT_FENCE
        elif (
            current.service_epoch != base.service_epoch
            or current.session_id != base.session_id
            or current.incarnation_id != base.incarnation_id
        ):
            reason = ProductEnrichmentReason.SESSION_CHANGED
        elif current.lifecycle is ProductEnrichmentLifecycle.CLOSED:
            reason = ProductEnrichmentReason.SESSION_CLOSED
        elif current.lifecycle is ProductEnrichmentLifecycle.REPLACED:
            reason = ProductEnrichmentReason.SESSION_REPLACED
        elif current.lifecycle is not ProductEnrichmentLifecycle.ACTIVE:
            reason = ProductEnrichmentReason.LIFECYCLE_CHANGED
        elif current.lifecycle_revision != base.lifecycle_revision:
            reason = ProductEnrichmentReason.LIFECYCLE_CHANGED
        elif current.accepted_state_revision != base.accepted_state_revision:
            reason = ProductEnrichmentReason.STALE_ACCEPTED_REVISION
        else:
            return None
        return _attempt(
            ProductEnrichmentOutcomeKind.FENCED,
            reason,
            proposal=proposal,
            accepted_state_revision=current.accepted_state_revision,
        )

    def _validated_snapshot(
        self,
        snapshot: ProductEnrichmentSessionSnapshot,
        expected_session_id: str,
    ) -> ProductEnrichmentSessionSnapshot:
        if not isinstance(snapshot, ProductEnrichmentSessionSnapshot):
            raise TypeError(
                "aggregate snapshot must be ProductEnrichmentSessionSnapshot"
            )
        if snapshot.session_id != expected_session_id:
            raise ProductEnrichmentSessionUnavailable(
                "aggregate returned another session"
            )
        if snapshot.document_bytes > self._policy.max_document_bytes:
            raise ValueError("aggregate snapshot exceeds max_document_bytes")
        _bounded_document(
            snapshot.document,
            max_bytes=self._policy.max_document_bytes,
            max_depth=self._policy.max_document_depth,
            max_items=self._policy.max_document_items,
        )
        return snapshot

    @staticmethod
    def _pre_generation_outcome(
        snapshot: ProductEnrichmentSessionSnapshot,
    ) -> ProductEnrichmentAttempt | None:
        if snapshot.lifecycle is ProductEnrichmentLifecycle.CLOSED:
            return _attempt(
                ProductEnrichmentOutcomeKind.FENCED,
                ProductEnrichmentReason.SESSION_CLOSED,
                accepted_state_revision=snapshot.accepted_state_revision,
            )
        if snapshot.lifecycle is ProductEnrichmentLifecycle.REPLACED:
            return _attempt(
                ProductEnrichmentOutcomeKind.FENCED,
                ProductEnrichmentReason.SESSION_REPLACED,
                accepted_state_revision=snapshot.accepted_state_revision,
            )
        if snapshot.lifecycle is not ProductEnrichmentLifecycle.ACTIVE:
            return _attempt(
                ProductEnrichmentOutcomeKind.FENCED,
                ProductEnrichmentReason.LIFECYCLE_CHANGED,
                accepted_state_revision=snapshot.accepted_state_revision,
            )
        if snapshot.foreground_active:
            return _attempt(
                ProductEnrichmentOutcomeKind.DEFERRED,
                ProductEnrichmentReason.FOREGROUND_ACTIVE,
                accepted_state_revision=snapshot.accepted_state_revision,
            )
        return None

    def _read_clock(self) -> int:
        value = self._clock_ms()
        _nonnegative(value, "clock_ms")
        return value

    @staticmethod
    def _terminalize_locked(
        record: _ProposalRecord, result: ProductEnrichmentAttempt
    ) -> None:
        record.state = _ProposalState.TERMINAL
        record.result = result


def _attempt(
    kind: ProductEnrichmentOutcomeKind,
    reason: ProductEnrichmentReason | None,
    *,
    proposal: ProductEnrichmentProposal | None = None,
    accepted_state_revision: int | None = None,
    replayed: bool = False,
) -> ProductEnrichmentAttempt:
    return ProductEnrichmentAttempt(
        kind=kind,
        reason=reason,
        proposal=proposal,
        accepted_state_revision=accepted_state_revision,
        replayed=replayed,
    )


def _bounded_document(
    document: Mapping[str, object],
    *,
    max_bytes: int,
    max_depth: int,
    max_items: int,
) -> tuple[Mapping[str, JsonValue], bytes]:
    if not isinstance(document, Mapping):
        raise TypeError("enrichment document must be a mapping")
    item_count = [0]
    frozen = _freeze_json(
        document,
        depth=0,
        max_depth=max_depth,
        max_items=max_items,
        item_count=item_count,
    )
    if not isinstance(frozen, Mapping):
        raise RuntimeError("enrichment document did not remain a mapping")
    try:
        canonical = json.dumps(
            _allocate_document(frozen),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError) as error:
        raise ValueError("enrichment document must be canonical UTF-8 JSON") from error
    if len(canonical) > max_bytes:
        raise ValueError("enrichment document exceeds max_document_bytes")
    return frozen, canonical


def _freeze_json(
    value: object,
    *,
    depth: int,
    max_depth: int,
    max_items: int,
    item_count: list[int],
) -> JsonValue:
    if depth > max_depth:
        raise ValueError("enrichment document exceeds max_document_depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise ValueError("enrichment document integer is outside JSON-safe range")
        return value
    if isinstance(value, float):
        if not (-float("inf") < value < float("inf")):
            raise ValueError("enrichment document numbers must be finite")
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise ValueError("enrichment document strings must be UTF-8") from error
        return value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, nested in value.items():
            item_count[0] += 1
            if item_count[0] > max_items:
                raise ValueError("enrichment document exceeds max_document_items")
            if not isinstance(key, str):
                raise TypeError("enrichment document keys must be strings")
            try:
                key.encode("utf-8", errors="strict")
            except UnicodeEncodeError as error:
                raise ValueError("enrichment document keys must be UTF-8") from error
            result[key] = _freeze_json(
                nested,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                item_count=item_count,
            )
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        result_items: list[JsonValue] = []
        for nested in value:
            item_count[0] += 1
            if item_count[0] > max_items:
                raise ValueError("enrichment document exceeds max_document_items")
            result_items.append(
                _freeze_json(
                    nested,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_items=max_items,
                    item_count=item_count,
                )
            )
        return tuple(result_items)
    raise TypeError("enrichment document must contain only JSON-safe values")


def _allocate_document(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _allocate_document(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_allocate_document(nested) for nested in value]
    return value


def _allocate_mapping(value: Mapping[str, JsonValue]) -> dict[str, object]:
    return {key: _allocate_document(nested) for key, nested in value.items()}


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTITY_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be a bounded product identity")
    return value


def _positive(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_COUNTER
    ):
        raise ValueError(f"{name} must be a positive bounded integer")
    return value


def _nonnegative(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_COUNTER
    ):
        raise ValueError(f"{name} must be a nonnegative bounded integer")
    return value


def _system_clock_ms() -> int:
    return time.time_ns() // 1_000_000


__all__ = [
    "ProductEnrichmentAggregate",
    "ProductEnrichmentAppliedEvent",
    "ProductEnrichmentAttempt",
    "ProductEnrichmentCoordinator",
    "ProductEnrichmentDenied",
    "ProductEnrichmentDenialReason",
    "ProductEnrichmentGeneration",
    "ProductEnrichmentGenerator",
    "ProductEnrichmentLifecycle",
    "ProductEnrichmentOutcomeKind",
    "ProductEnrichmentPolicy",
    "ProductEnrichmentProposal",
    "ProductEnrichmentPublicationError",
    "ProductEnrichmentReason",
    "ProductEnrichmentRequest",
    "ProductEnrichmentSessionAccess",
    "ProductEnrichmentSessionSnapshot",
    "ProductEnrichmentSessionUnavailable",
]
