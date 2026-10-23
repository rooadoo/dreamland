"""Bounded, process-local admission accounting for product provider work.

The ledger is deliberately protocol-neutral and inert until explicitly
constructed.  It starts no worker, timer, listener, provider, or runtime and
imports no DREAMLAND execution subsystem.

A logical job reserves its worst-case token and spend allowance exactly once.
Logical settlement and physical-worker reaping are separate because closing a
publication fence does not prove that already-dispatched provider work stopped.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from enum import StrEnum

_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_RESOURCE_UNITS = (1 << 63) - 1


class ProductJobCategory(StrEnum):
    """Product workloads sharing this admission boundary."""

    FOREGROUND_PRODUCT = "foreground_product"
    PRODUCT_ENRICHMENT = "product_enrichment"
    FUTURE_FORGE = "future_forge"


class ProductCapacityClass(StrEnum):
    """Hard admission partitions within one product provider boundary."""

    STANDARD = "standard"
    PRESENTER_STAGE = "presenter_stage"


class AdmissionDenialReason(StrEnum):
    """Stable, non-sensitive reasons an admission operation may be denied."""

    SESSION_CAPACITY_EXHAUSTED = "session_capacity_exhausted"
    SESSION_CONFLICT = "session_conflict"
    SESSION_UNAVAILABLE = "session_unavailable"
    LOGICAL_JOB_CAPACITY_EXHAUSTED = "logical_job_capacity_exhausted"
    LOGICAL_JOB_CONFLICT = "logical_job_conflict"
    LOGICAL_JOB_NOT_FOUND = "logical_job_not_found"
    LOGICAL_JOB_FENCED = "logical_job_fenced"
    LOGICAL_JOB_SETTLED = "logical_job_settled"
    PHYSICAL_WORKER_CAPACITY_EXHAUSTED = "physical_worker_capacity_exhausted"
    PHYSICAL_WORKER_NOT_CLAIMED = "physical_worker_not_claimed"
    TOKEN_BUDGET_EXHAUSTED = "token_budget_exhausted"
    SPEND_BUDGET_EXHAUSTED = "spend_budget_exhausted"
    SETTLEMENT_CONFLICT = "settlement_conflict"
    ACTUAL_USAGE_EXCEEDS_RESERVATION = "actual_usage_exceeds_reservation"


_DENIAL_MESSAGES: dict[AdmissionDenialReason, str] = {
    AdmissionDenialReason.SESSION_CAPACITY_EXHAUSTED: (
        "Product session capacity is temporarily exhausted."
    ),
    AdmissionDenialReason.SESSION_CONFLICT: (
        "The replacement product session identity is already active."
    ),
    AdmissionDenialReason.SESSION_UNAVAILABLE: (
        "The product session is unavailable for new work."
    ),
    AdmissionDenialReason.LOGICAL_JOB_CAPACITY_EXHAUSTED: (
        "Product logical-job capacity is exhausted."
    ),
    AdmissionDenialReason.LOGICAL_JOB_CONFLICT: (
        "The logical job identity is already bound to another reservation."
    ),
    AdmissionDenialReason.LOGICAL_JOB_NOT_FOUND: ("The logical job is unavailable."),
    AdmissionDenialReason.LOGICAL_JOB_FENCED: (
        "The logical job is fenced from provider dispatch."
    ),
    AdmissionDenialReason.LOGICAL_JOB_SETTLED: ("The logical job is already settled."),
    AdmissionDenialReason.PHYSICAL_WORKER_CAPACITY_EXHAUSTED: (
        "Product physical-worker capacity is temporarily exhausted."
    ),
    AdmissionDenialReason.PHYSICAL_WORKER_NOT_CLAIMED: (
        "The logical job has no physical worker to reap."
    ),
    AdmissionDenialReason.TOKEN_BUDGET_EXHAUSTED: (
        "The product token budget is exhausted."
    ),
    AdmissionDenialReason.SPEND_BUDGET_EXHAUSTED: (
        "The product spend budget is exhausted."
    ),
    AdmissionDenialReason.SETTLEMENT_CONFLICT: (
        "The logical job is already settled with different usage."
    ),
    AdmissionDenialReason.ACTUAL_USAGE_EXCEEDS_RESERVATION: (
        "Actual product usage exceeds the logical job reservation."
    ),
}

_RETRYABLE_DENIALS = frozenset(
    {
        AdmissionDenialReason.SESSION_CAPACITY_EXHAUSTED,
        AdmissionDenialReason.PHYSICAL_WORKER_CAPACITY_EXHAUSTED,
        AdmissionDenialReason.TOKEN_BUDGET_EXHAUSTED,
        AdmissionDenialReason.SPEND_BUDGET_EXHAUSTED,
    }
)


class ProductAdmissionDenied(RuntimeError):
    """Typed safe denial with no resource or identity details."""

    def __init__(self, reason: AdmissionDenialReason) -> None:
        super().__init__(_DENIAL_MESSAGES[reason])
        self.reason = reason
        self.message = _DENIAL_MESSAGES[reason]
        self.retryable = reason in _RETRYABLE_DENIALS


@dataclass(frozen=True, slots=True)
class ProductAdmissionLimits:
    """Hard process-local limits for one explicitly constructed ledger."""

    max_active_sessions: int = 64
    max_logical_jobs: int = 1_024
    max_physical_workers: int = 8
    token_budget: int = 10_000_000
    spend_budget_units: int = 10_000_000
    presenter_stage_reserved_sessions: int = 0
    presenter_stage_reserved_logical_jobs: int = 0
    presenter_stage_reserved_physical_workers: int = 0
    presenter_stage_reserved_token_budget: int = 0
    presenter_stage_reserved_spend_budget_units: int = 0

    def __post_init__(self) -> None:
        _require_positive_limit(self.max_active_sessions, "max_active_sessions")
        _require_positive_limit(self.max_logical_jobs, "max_logical_jobs")
        _require_positive_limit(
            self.max_physical_workers,
            "max_physical_workers",
        )
        _require_resource_units(self.token_budget, "token_budget")
        _require_resource_units(self.spend_budget_units, "spend_budget_units")
        for name in (
            "presenter_stage_reserved_sessions",
            "presenter_stage_reserved_logical_jobs",
            "presenter_stage_reserved_physical_workers",
            "presenter_stage_reserved_token_budget",
            "presenter_stage_reserved_spend_budget_units",
        ):
            _require_resource_units(getattr(self, name), name)
        if self.max_physical_workers > self.max_logical_jobs:
            raise ValueError("max_physical_workers must not exceed max_logical_jobs")
        reserved_pairs = (
            (self.presenter_stage_reserved_sessions, self.max_active_sessions),
            (self.presenter_stage_reserved_logical_jobs, self.max_logical_jobs),
            (
                self.presenter_stage_reserved_physical_workers,
                self.max_physical_workers,
            ),
            (self.presenter_stage_reserved_token_budget, self.token_budget),
            (
                self.presenter_stage_reserved_spend_budget_units,
                self.spend_budget_units,
            ),
        )
        if any(reserved > total for reserved, total in reserved_pairs):
            raise ValueError("presenter Stage reservation exceeds a hard product limit")


@dataclass(frozen=True, slots=True)
class SessionAdmission:
    replayed: bool


@dataclass(frozen=True, slots=True)
class SessionReplacement:
    replaced: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class SessionRelease:
    released: bool


@dataclass(frozen=True, slots=True)
class JobReservation:
    category: ProductJobCategory
    reserved_tokens: int
    reserved_spend_units: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class WorkerClaim:
    replayed: bool


@dataclass(frozen=True, slots=True)
class UndispatchedJobDiscard:
    discarded: bool
    worker_released: bool


@dataclass(frozen=True, slots=True)
class FenceResult:
    fenced: bool
    already_settled: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class JobSettlement:
    actual_tokens: int
    actual_spend_units: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class WorkerReap:
    reaped: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class CategoryAdmissionSnapshot:
    """Identity-free counts for one product workload category."""

    category: ProductJobCategory
    logical_jobs: int
    unsettled_jobs: int
    physical_workers: int
    fenced_jobs: int
    settled_jobs: int


@dataclass(frozen=True, slots=True)
class ProductAdmissionSnapshot:
    """Bounded identity-free resource and count projection."""

    active_sessions: int
    logical_jobs: int
    unsettled_jobs: int
    physical_workers: int
    fenced_jobs: int
    settled_jobs: int
    reserved_tokens: int
    consumed_tokens: int
    token_budget: int
    reserved_spend_units: int
    consumed_spend_units: int
    spend_budget_units: int
    categories: tuple[CategoryAdmissionSnapshot, ...]


@dataclass(slots=True)
class _JobRecord:
    session_id: str
    capacity_class: ProductCapacityClass
    category: ProductJobCategory
    reserved_tokens: int
    reserved_spend_units: int
    worker_claimed: bool = False
    worker_reaped: bool = False
    fenced: bool = False
    settlement: tuple[int, int] | None = None

    @property
    def worker_outstanding(self) -> bool:
        return self.worker_claimed and not self.worker_reaped


class ProductAdmissionLedger:
    """Thread-safe process-local product admission and budget ledger.

    ``max_logical_jobs`` is the process-epoch identity-record limit.  Settled
    records remain retained so an exact logical job identity can never reserve
    or charge twice during the ledger's lifetime.
    """

    def __init__(self, limits: ProductAdmissionLimits | None = None) -> None:
        self._limits = limits or ProductAdmissionLimits()
        self._lock = threading.RLock()
        self._active_sessions: dict[str, ProductCapacityClass] = {}
        self._jobs: dict[str, _JobRecord] = {}
        self._physical_workers = 0
        self._reserved_tokens = 0
        self._consumed_tokens = 0
        self._reserved_spend_units = 0
        self._consumed_spend_units = 0

    @property
    def limits(self) -> ProductAdmissionLimits:
        return self._limits

    def admit_session(
        self,
        session_id: str,
        *,
        capacity_class: ProductCapacityClass | str = ProductCapacityClass.STANDARD,
    ) -> SessionAdmission:
        """Admit one active product session, idempotently by exact identity."""
        identity = _require_identity(session_id, "session_id")
        requested_class = _require_capacity_class(capacity_class)
        with self._lock:
            existing_class = self._active_sessions.get(identity)
            if existing_class is not None:
                if existing_class is not requested_class:
                    raise ProductAdmissionDenied(AdmissionDenialReason.SESSION_CONFLICT)
                return SessionAdmission(replayed=True)
            if len(self._active_sessions) >= self._limits.max_active_sessions:
                raise ProductAdmissionDenied(
                    AdmissionDenialReason.SESSION_CAPACITY_EXHAUSTED
                )
            if self._session_count_locked(requested_class) >= self._class_session_limit(
                requested_class
            ):
                raise ProductAdmissionDenied(
                    AdmissionDenialReason.SESSION_CAPACITY_EXHAUSTED
                )
            self._active_sessions[identity] = requested_class
            return SessionAdmission(replayed=False)

    def release_session(self, session_id: str) -> SessionRelease:
        """Release active-session capacity once without touching its jobs."""
        identity = _require_identity(session_id, "session_id")
        with self._lock:
            if identity not in self._active_sessions:
                return SessionRelease(released=False)
            self._active_sessions.pop(identity)
            return SessionRelease(released=True)

    def replace_session(
        self,
        old_session_id: str,
        new_session_id: str,
    ) -> SessionReplacement:
        """Atomically transfer one active-session slot to a new identity.

        The transfer is count-neutral and does not inspect or rewrite retained
        job records.  The already-converged state where only the requested new
        identity is active is an exact replay; any simultaneous old/new active
        state is a conflicting replacement.
        """
        old_identity = _require_identity(old_session_id, "old_session_id")
        new_identity = _require_identity(new_session_id, "new_session_id")
        if old_identity == new_identity:
            raise ValueError("old_session_id and new_session_id must differ")

        with self._lock:
            old_class = self._active_sessions.get(old_identity)
            new_class = self._active_sessions.get(new_identity)
            old_active = old_class is not None
            new_active = new_class is not None
            if not old_active:
                if new_active:
                    return SessionReplacement(replaced=False, replayed=True)
                raise ProductAdmissionDenied(AdmissionDenialReason.SESSION_UNAVAILABLE)
            if new_active:
                raise ProductAdmissionDenied(AdmissionDenialReason.SESSION_CONFLICT)

            active_count = len(self._active_sessions)
            replacement = self._active_sessions.copy()
            replacement.pop(old_identity)
            assert old_class is not None
            replacement[new_identity] = old_class
            if len(replacement) != active_count:
                raise RuntimeError("product session replacement is not count-neutral")
            self._active_sessions = replacement
            self._assert_invariants_locked()
            return SessionReplacement(replaced=True, replayed=False)

    def reserve_job(
        self,
        *,
        logical_job_id: str,
        session_id: str,
        category: ProductJobCategory | str,
        reserved_tokens: int,
        reserved_spend_units: int,
    ) -> JobReservation:
        """Reserve worst-case resources once before a logical provider job."""
        job_identity = _require_identity(logical_job_id, "logical_job_id")
        session_identity = _require_identity(session_id, "session_id")
        job_category = _require_category(category)
        tokens = _require_resource_units(reserved_tokens, "reserved_tokens")
        spend = _require_resource_units(
            reserved_spend_units,
            "reserved_spend_units",
        )
        with self._lock:
            existing = self._jobs.get(job_identity)
            if existing is not None:
                if (
                    existing.session_id != session_identity
                    or existing.category is not job_category
                    or existing.reserved_tokens != tokens
                    or existing.reserved_spend_units != spend
                ):
                    raise ProductAdmissionDenied(
                        AdmissionDenialReason.LOGICAL_JOB_CONFLICT
                    )
                return JobReservation(
                    category=existing.category,
                    reserved_tokens=existing.reserved_tokens,
                    reserved_spend_units=existing.reserved_spend_units,
                    replayed=True,
                )
            capacity_class = self._active_sessions.get(session_identity)
            if capacity_class is None:
                raise ProductAdmissionDenied(AdmissionDenialReason.SESSION_UNAVAILABLE)
            if len(self._jobs) >= self._limits.max_logical_jobs:
                raise ProductAdmissionDenied(
                    AdmissionDenialReason.LOGICAL_JOB_CAPACITY_EXHAUSTED
                )
            class_usage = self._class_usage_locked(capacity_class)
            if class_usage["logical_jobs"] >= self._class_logical_job_limit(
                capacity_class
            ):
                raise ProductAdmissionDenied(
                    AdmissionDenialReason.LOGICAL_JOB_CAPACITY_EXHAUSTED
                )
            if (
                self._consumed_tokens + self._reserved_tokens + tokens
                > self._limits.token_budget
            ):
                raise ProductAdmissionDenied(
                    AdmissionDenialReason.TOKEN_BUDGET_EXHAUSTED
                )
            if class_usage["consumed_tokens"] + class_usage[
                "reserved_tokens"
            ] + tokens > self._class_token_budget(capacity_class):
                raise ProductAdmissionDenied(
                    AdmissionDenialReason.TOKEN_BUDGET_EXHAUSTED
                )
            if (
                self._consumed_spend_units + self._reserved_spend_units + spend
                > self._limits.spend_budget_units
            ):
                raise ProductAdmissionDenied(
                    AdmissionDenialReason.SPEND_BUDGET_EXHAUSTED
                )
            if class_usage["consumed_spend_units"] + class_usage[
                "reserved_spend_units"
            ] + spend > self._class_spend_budget(capacity_class):
                raise ProductAdmissionDenied(
                    AdmissionDenialReason.SPEND_BUDGET_EXHAUSTED
                )
            self._jobs[job_identity] = _JobRecord(
                session_id=session_identity,
                capacity_class=capacity_class,
                category=job_category,
                reserved_tokens=tokens,
                reserved_spend_units=spend,
            )
            self._reserved_tokens += tokens
            self._reserved_spend_units += spend
            self._assert_invariants_locked()
            return JobReservation(
                category=job_category,
                reserved_tokens=tokens,
                reserved_spend_units=spend,
                replayed=False,
            )

    def claim_worker(self, logical_job_id: str) -> WorkerClaim:
        """Claim bounded physical capacity immediately before dispatch."""
        identity = _require_identity(logical_job_id, "logical_job_id")
        with self._lock:
            record = self._require_job_locked(identity)
            if record.worker_claimed:
                return WorkerClaim(replayed=True)
            if record.settlement is not None:
                raise ProductAdmissionDenied(AdmissionDenialReason.LOGICAL_JOB_SETTLED)
            if record.fenced:
                raise ProductAdmissionDenied(AdmissionDenialReason.LOGICAL_JOB_FENCED)
            if self._physical_workers >= self._limits.max_physical_workers:
                raise ProductAdmissionDenied(
                    AdmissionDenialReason.PHYSICAL_WORKER_CAPACITY_EXHAUSTED
                )
            if self._class_usage_locked(record.capacity_class)[
                "physical_workers"
            ] >= self._class_physical_worker_limit(record.capacity_class):
                raise ProductAdmissionDenied(
                    AdmissionDenialReason.PHYSICAL_WORKER_CAPACITY_EXHAUSTED
                )
            record.worker_claimed = True
            self._physical_workers += 1
            self._assert_invariants_locked()
            return WorkerClaim(replayed=False)

    def reserve_and_claim_worker(
        self,
        *,
        logical_job_id: str,
        session_id: str,
        category: ProductJobCategory | str,
        reserved_tokens: int,
        reserved_spend_units: int,
    ) -> tuple[JobReservation, WorkerClaim]:
        """Atomically reserve a logical job and claim its worker capacity.

        If physical capacity rejects a newly created reservation, that
        reservation is rolled back before the denial becomes observable.  An
        exact pre-existing reservation is never removed by a failed retry.
        """
        identity = _require_identity(logical_job_id, "logical_job_id")
        with self._lock:
            existed = identity in self._jobs
            reservation = self.reserve_job(
                logical_job_id=identity,
                session_id=session_id,
                category=category,
                reserved_tokens=reserved_tokens,
                reserved_spend_units=reserved_spend_units,
            )
            try:
                claim = self.claim_worker(identity)
            except ProductAdmissionDenied:
                if not existed:
                    self._discard_new_job_locked(identity)
                raise
            return reservation, claim

    def discard_undispatched_job(
        self,
        logical_job_id: str,
    ) -> UndispatchedJobDiscard:
        """Roll back a job that was claimed but never dispatched.

        This narrow compensation primitive is only valid before externally
        observable acceptance and before provider dispatch.  Fenced or settled
        identities cannot be discarded.
        """
        identity = _require_identity(logical_job_id, "logical_job_id")
        with self._lock:
            record = self._jobs.get(identity)
            if record is None:
                return UndispatchedJobDiscard(
                    discarded=False,
                    worker_released=False,
                )
            if record.fenced:
                raise ProductAdmissionDenied(AdmissionDenialReason.LOGICAL_JOB_FENCED)
            if record.settlement is not None:
                raise ProductAdmissionDenied(AdmissionDenialReason.LOGICAL_JOB_SETTLED)
            worker_released = record.worker_outstanding
            self._discard_new_job_locked(identity)
            return UndispatchedJobDiscard(
                discarded=True,
                worker_released=worker_released,
            )

    def fence_job(self, logical_job_id: str) -> FenceResult:
        """Fence publication without releasing logical or physical capacity."""
        identity = _require_identity(logical_job_id, "logical_job_id")
        with self._lock:
            record = self._require_job_locked(identity)
            if record.fenced:
                return FenceResult(
                    fenced=True,
                    already_settled=record.settlement is not None,
                    replayed=True,
                )
            if record.settlement is not None:
                return FenceResult(
                    fenced=False,
                    already_settled=True,
                    replayed=False,
                )
            record.fenced = True
            return FenceResult(
                fenced=True,
                already_settled=False,
                replayed=False,
            )

    def settle_job(
        self,
        logical_job_id: str,
        *,
        actual_tokens: int,
        actual_spend_units: int,
    ) -> JobSettlement:
        """Settle worst-case reservations once, without implying worker reap."""
        identity = _require_identity(logical_job_id, "logical_job_id")
        tokens = _require_resource_units(actual_tokens, "actual_tokens")
        spend = _require_resource_units(actual_spend_units, "actual_spend_units")
        with self._lock:
            record = self._require_job_locked(identity)
            return self._settle_job_locked(record, tokens=tokens, spend=spend)

    def reap_worker(self, logical_job_id: str) -> WorkerReap:
        """Release a claimed physical worker exactly once."""
        identity = _require_identity(logical_job_id, "logical_job_id")
        with self._lock:
            record = self._require_job_locked(identity)
            return self._reap_worker_locked(record)

    def settle_and_reap(
        self,
        logical_job_id: str,
        *,
        actual_tokens: int,
        actual_spend_units: int,
    ) -> tuple[JobSettlement, WorkerReap]:
        """Atomically settle logical usage and release its physical worker."""
        identity = _require_identity(logical_job_id, "logical_job_id")
        tokens = _require_resource_units(actual_tokens, "actual_tokens")
        spend = _require_resource_units(actual_spend_units, "actual_spend_units")
        with self._lock:
            record = self._require_job_locked(identity)
            if not record.worker_claimed:
                raise ProductAdmissionDenied(
                    AdmissionDenialReason.PHYSICAL_WORKER_NOT_CLAIMED
                )
            settlement = self._settle_job_locked(
                record,
                tokens=tokens,
                spend=spend,
            )
            reap = self._reap_worker_locked(record)
            return settlement, reap

    def snapshot(self) -> ProductAdmissionSnapshot:
        """Return safe aggregate counts without session or job identities."""
        with self._lock:
            categories = tuple(
                self._category_snapshot_locked(category)
                for category in ProductJobCategory
            )
            settled_jobs = sum(
                record.settlement is not None for record in self._jobs.values()
            )
            fenced_jobs = sum(record.fenced for record in self._jobs.values())
            return ProductAdmissionSnapshot(
                active_sessions=len(self._active_sessions),
                logical_jobs=len(self._jobs),
                unsettled_jobs=len(self._jobs) - settled_jobs,
                physical_workers=self._physical_workers,
                fenced_jobs=fenced_jobs,
                settled_jobs=settled_jobs,
                reserved_tokens=self._reserved_tokens,
                consumed_tokens=self._consumed_tokens,
                token_budget=self._limits.token_budget,
                reserved_spend_units=self._reserved_spend_units,
                consumed_spend_units=self._consumed_spend_units,
                spend_budget_units=self._limits.spend_budget_units,
                categories=categories,
            )

    def _require_job_locked(self, logical_job_id: str) -> _JobRecord:
        record = self._jobs.get(logical_job_id)
        if record is None:
            raise ProductAdmissionDenied(AdmissionDenialReason.LOGICAL_JOB_NOT_FOUND)
        return record

    def _discard_new_job_locked(self, logical_job_id: str) -> None:
        record = self._jobs.pop(logical_job_id)
        self._reserved_tokens -= record.reserved_tokens
        self._reserved_spend_units -= record.reserved_spend_units
        if record.worker_outstanding:
            self._physical_workers -= 1
        self._assert_invariants_locked()

    def _settle_job_locked(
        self,
        record: _JobRecord,
        *,
        tokens: int,
        spend: int,
    ) -> JobSettlement:
        if record.settlement is not None:
            if record.settlement != (tokens, spend):
                raise ProductAdmissionDenied(AdmissionDenialReason.SETTLEMENT_CONFLICT)
            return JobSettlement(
                actual_tokens=tokens,
                actual_spend_units=spend,
                replayed=True,
            )
        if tokens > record.reserved_tokens or spend > record.reserved_spend_units:
            raise ProductAdmissionDenied(
                AdmissionDenialReason.ACTUAL_USAGE_EXCEEDS_RESERVATION
            )
        if not record.worker_claimed and (tokens or spend):
            raise ProductAdmissionDenied(
                AdmissionDenialReason.ACTUAL_USAGE_EXCEEDS_RESERVATION
            )
        self._reserved_tokens -= record.reserved_tokens
        self._reserved_spend_units -= record.reserved_spend_units
        self._consumed_tokens += tokens
        self._consumed_spend_units += spend
        record.settlement = (tokens, spend)
        self._assert_invariants_locked()
        return JobSettlement(
            actual_tokens=tokens,
            actual_spend_units=spend,
            replayed=False,
        )

    def _reap_worker_locked(self, record: _JobRecord) -> WorkerReap:
        if not record.worker_claimed:
            raise ProductAdmissionDenied(
                AdmissionDenialReason.PHYSICAL_WORKER_NOT_CLAIMED
            )
        if record.worker_reaped:
            return WorkerReap(reaped=False, replayed=True)
        record.worker_reaped = True
        self._physical_workers -= 1
        self._assert_invariants_locked()
        return WorkerReap(reaped=True, replayed=False)

    def _category_snapshot_locked(
        self,
        category: ProductJobCategory,
    ) -> CategoryAdmissionSnapshot:
        records = tuple(
            record for record in self._jobs.values() if record.category is category
        )
        settled = sum(record.settlement is not None for record in records)
        return CategoryAdmissionSnapshot(
            category=category,
            logical_jobs=len(records),
            unsettled_jobs=len(records) - settled,
            physical_workers=sum(record.worker_outstanding for record in records),
            fenced_jobs=sum(record.fenced for record in records),
            settled_jobs=settled,
        )

    def _session_count_locked(self, capacity_class: ProductCapacityClass) -> int:
        return sum(value is capacity_class for value in self._active_sessions.values())

    def _class_usage_locked(
        self,
        capacity_class: ProductCapacityClass,
    ) -> dict[str, int]:
        records = tuple(
            record
            for record in self._jobs.values()
            if record.capacity_class is capacity_class
        )
        return {
            "logical_jobs": len(records),
            "physical_workers": sum(record.worker_outstanding for record in records),
            "reserved_tokens": sum(
                record.reserved_tokens
                for record in records
                if record.settlement is None
            ),
            "consumed_tokens": sum(
                record.settlement[0]
                for record in records
                if record.settlement is not None
            ),
            "reserved_spend_units": sum(
                record.reserved_spend_units
                for record in records
                if record.settlement is None
            ),
            "consumed_spend_units": sum(
                record.settlement[1]
                for record in records
                if record.settlement is not None
            ),
        }

    def _class_session_limit(self, capacity_class: ProductCapacityClass) -> int:
        reserved = self._limits.presenter_stage_reserved_sessions
        if capacity_class is ProductCapacityClass.PRESENTER_STAGE:
            return reserved
        return self._limits.max_active_sessions - reserved

    def _class_logical_job_limit(self, capacity_class: ProductCapacityClass) -> int:
        reserved = self._limits.presenter_stage_reserved_logical_jobs
        if capacity_class is ProductCapacityClass.PRESENTER_STAGE:
            return reserved
        return self._limits.max_logical_jobs - reserved

    def _class_physical_worker_limit(
        self,
        capacity_class: ProductCapacityClass,
    ) -> int:
        reserved = self._limits.presenter_stage_reserved_physical_workers
        if capacity_class is ProductCapacityClass.PRESENTER_STAGE:
            return reserved
        return self._limits.max_physical_workers - reserved

    def _class_token_budget(self, capacity_class: ProductCapacityClass) -> int:
        reserved = self._limits.presenter_stage_reserved_token_budget
        if capacity_class is ProductCapacityClass.PRESENTER_STAGE:
            return reserved
        return self._limits.token_budget - reserved

    def _class_spend_budget(self, capacity_class: ProductCapacityClass) -> int:
        reserved = self._limits.presenter_stage_reserved_spend_budget_units
        if capacity_class is ProductCapacityClass.PRESENTER_STAGE:
            return reserved
        return self._limits.spend_budget_units - reserved

    def _assert_invariants_locked(self) -> None:
        if not 0 <= len(self._active_sessions) <= self._limits.max_active_sessions:
            raise RuntimeError("product session admission counter is inconsistent")
        if not 0 <= len(self._jobs) <= self._limits.max_logical_jobs:
            raise RuntimeError("product logical-job counter is inconsistent")
        outstanding_workers = sum(
            record.worker_outstanding for record in self._jobs.values()
        )
        if self._physical_workers != outstanding_workers or not (
            0 <= self._physical_workers <= self._limits.max_physical_workers
        ):
            raise RuntimeError("product physical-worker counter is inconsistent")
        outstanding_tokens = sum(
            record.reserved_tokens
            for record in self._jobs.values()
            if record.settlement is None
        )
        outstanding_spend = sum(
            record.reserved_spend_units
            for record in self._jobs.values()
            if record.settlement is None
        )
        if self._reserved_tokens != outstanding_tokens or self._reserved_tokens < 0:
            raise RuntimeError("product token reservation counter is inconsistent")
        if (
            self._reserved_spend_units != outstanding_spend
            or self._reserved_spend_units < 0
        ):
            raise RuntimeError("product spend reservation counter is inconsistent")
        if not (
            0
            <= self._consumed_tokens + self._reserved_tokens
            <= self._limits.token_budget
        ):
            raise RuntimeError("product token budget is overcommitted")
        if not (
            0
            <= self._consumed_spend_units + self._reserved_spend_units
            <= self._limits.spend_budget_units
        ):
            raise RuntimeError("product spend budget is overcommitted")
        for capacity_class in ProductCapacityClass:
            usage = self._class_usage_locked(capacity_class)
            if self._session_count_locked(capacity_class) > self._class_session_limit(
                capacity_class
            ):
                raise RuntimeError("product session partition is overcommitted")
            if usage["logical_jobs"] > self._class_logical_job_limit(capacity_class):
                raise RuntimeError("product logical-job partition is overcommitted")
            if usage["physical_workers"] > self._class_physical_worker_limit(
                capacity_class
            ):
                raise RuntimeError("product worker partition is overcommitted")
            if usage["consumed_tokens"] + usage[
                "reserved_tokens"
            ] > self._class_token_budget(capacity_class):
                raise RuntimeError("product token partition is overcommitted")
            if usage["consumed_spend_units"] + usage[
                "reserved_spend_units"
            ] > self._class_spend_budget(capacity_class):
                raise RuntimeError("product spend partition is overcommitted")


def _require_category(category: ProductJobCategory | str) -> ProductJobCategory:
    try:
        return ProductJobCategory(category)
    except (TypeError, ValueError) as error:
        raise ValueError("category is not a supported product job category") from error


def _require_capacity_class(
    capacity_class: ProductCapacityClass | str,
) -> ProductCapacityClass:
    try:
        return ProductCapacityClass(capacity_class)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "capacity_class is not a supported product partition"
        ) from error


def _require_identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not _IDENTITY_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be a bounded product identity")
    return value


def _require_positive_limit(value: int, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_RESOURCE_UNITS
    ):
        raise ValueError(f"{name} must be a positive bounded integer")
    return value


def _require_resource_units(value: int, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_RESOURCE_UNITS
    ):
        raise ValueError(f"{name} must be a nonnegative bounded integer")
    return value
