from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

from dreamland.product.admission import (
    ProductAdmissionDenied,
    ProductAdmissionLedger,
    ProductAdmissionLimits,
    ProductJobCategory,
)
from dreamland.product.enrichment import (
    ProductEnrichmentAppliedEvent,
    ProductEnrichmentCoordinator,
    ProductEnrichmentDenialReason,
    ProductEnrichmentDenied,
    ProductEnrichmentGeneration,
    ProductEnrichmentLifecycle,
    ProductEnrichmentOutcomeKind,
    ProductEnrichmentPolicy,
    ProductEnrichmentPublicationError,
    ProductEnrichmentReason,
    ProductEnrichmentRequest,
    ProductEnrichmentSessionSnapshot,
    ProductEnrichmentSessionUnavailable,
)


class FakeAggregate:
    def __init__(self) -> None:
        self.service_epoch = "epoch:enrichment-test"
        self.session_id = "session:one"
        self.incarnation_id = "incarnation:one"
        self.lifecycle_revision = 1
        self.accepted_state_revision = 0
        self.lifecycle = ProductEnrichmentLifecycle.ACTIVE
        self.foreground_active = False
        self.document: dict[str, object] = {"world": {"facts": ["initial"]}}
        self.available = True
        self.capture_calls = 0
        self.transaction_calls = 0
        self.install_calls = 0
        self.lock_active = False
        self.operation_log: list[str] = []
        self.canonical_events: list[ProductEnrichmentAppliedEvent] = []
        self.fail_commit = False
        self._lock = threading.RLock()

    def capture_enrichment_snapshot(
        self, session_id: str
    ) -> ProductEnrichmentSessionSnapshot:
        with self._lock:
            self._enter_lock()
            try:
                self.capture_calls += 1
                if not self.available or session_id != self.session_id:
                    raise ProductEnrichmentSessionUnavailable()
                return self._snapshot()
            finally:
                self._leave_lock()

    def transact_enrichment(self, session_id: str, operation):
        with self._lock:
            self._enter_lock()
            try:
                self.transaction_calls += 1
                if not self.available or session_id != self.session_id:
                    raise ProductEnrichmentSessionUnavailable()
                return operation(self)
            finally:
                self._leave_lock()

    @property
    def enrichment_snapshot(self) -> ProductEnrichmentSessionSnapshot:
        if not self.lock_active:
            raise AssertionError(
                "live enrichment state was read outside aggregate lock"
            )
        return self._snapshot()

    def install_enrichment_document(self, replacement_document, applied_event):
        if not self.lock_active:
            raise AssertionError(
                "enrichment state was installed outside aggregate lock"
            )
        if self.fail_commit:
            raise RuntimeError("aggregate commit unavailable")
        if not isinstance(applied_event, ProductEnrichmentAppliedEvent):
            raise TypeError("applied_event must be ProductEnrichmentAppliedEvent")
        if applied_event.session_id != self.session_id:
            raise AssertionError("applied event targets another session")
        if (
            applied_event.previous_accepted_state_revision
            != self.accepted_state_revision
            or applied_event.resulting_accepted_state_revision
            != self.accepted_state_revision + 1
        ):
            raise AssertionError("applied event does not identify this transition")
        self.document = replacement_document
        self.accepted_state_revision += 1
        self.canonical_events.append(applied_event)
        self.install_calls += 1
        self.operation_log.append(f"commit:{self.accepted_state_revision}")
        return self.accepted_state_revision

    def set_foreground(self, active: bool) -> None:
        with self._lock:
            self.foreground_active = active

    def external_commit(self, document: dict[str, object] | None = None) -> None:
        with self._lock:
            self.document = document or {"world": {"facts": ["foreground"]}}
            self.accepted_state_revision += 1

    def close(self) -> None:
        with self._lock:
            self.lifecycle = ProductEnrichmentLifecycle.CLOSED
            self.lifecycle_revision += 1

    def replace_session(self) -> None:
        with self._lock:
            self.lifecycle = ProductEnrichmentLifecycle.REPLACED
            self.lifecycle_revision += 1
            self.incarnation_id = "incarnation:replacement"

    def _snapshot(self) -> ProductEnrichmentSessionSnapshot:
        return ProductEnrichmentSessionSnapshot(
            service_epoch=self.service_epoch,
            session_id=self.session_id,
            incarnation_id=self.incarnation_id,
            lifecycle_revision=self.lifecycle_revision,
            accepted_state_revision=self.accepted_state_revision,
            lifecycle=self.lifecycle,
            foreground_active=self.foreground_active,
            document=self.document,
        )

    def _enter_lock(self) -> None:
        if self.lock_active:
            raise AssertionError("test aggregate does not permit nested transactions")
        self.lock_active = True

    def _leave_lock(self) -> None:
        self.lock_active = False


class RecordingGenerator:
    def __init__(
        self,
        aggregate: FakeAggregate,
        *,
        document: dict[str, object] | None = None,
        usage_tokens: int = 12,
        usage_spend_units: int = 7,
        during_generate=None,
    ) -> None:
        self.aggregate = aggregate
        self.document = document or {"world": {"facts": ["enriched"]}}
        self.usage_tokens = usage_tokens
        self.usage_spend_units = usage_spend_units
        self.during_generate = during_generate
        self.calls = 0
        self.requests: list[ProductEnrichmentRequest] = []

    def generate(self, request: ProductEnrichmentRequest):
        if self.aggregate.lock_active:
            raise AssertionError("generation ran while the aggregate lock was held")
        self.calls += 1
        self.requests.append(request)
        if self.during_generate is not None:
            self.during_generate()
        return ProductEnrichmentGeneration(
            replacement_document=self.document,
            usage_tokens=self.usage_tokens,
            usage_spend_units=self.usage_spend_units,
        )


class ProductEnrichmentCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.aggregate = FakeAggregate()
        self.generator = RecordingGenerator(self.aggregate)
        self.events: list[ProductEnrichmentAppliedEvent] = []
        self.ledger = _ledger()
        self.coordinator = _coordinator(
            self.aggregate,
            self.generator,
            self.ledger,
            self.events,
        )

    def test_success_commits_canonical_event_then_observes_outside_lock(self) -> None:
        event_observations: list[tuple[bool, int, str, bool]] = []

        def observe(event: ProductEnrichmentAppliedEvent) -> None:
            event_observations.append(
                (
                    self.aggregate.lock_active,
                    self.aggregate.accepted_state_revision,
                    self.aggregate.operation_log[-1],
                    self.aggregate.canonical_events[-1] is event,
                )
            )
            self.events.append(event)

        self.coordinator = _coordinator(
            self.aggregate,
            self.generator,
            self.ledger,
            self.events,
            on_applied=observe,
        )
        result = self.coordinator.run(
            proposal_id="enrichment:one",
            session_id=self.aggregate.session_id,
        )

        self.assertEqual(result.kind, ProductEnrichmentOutcomeKind.APPLIED)
        self.assertIsNone(result.reason)
        self.assertEqual(result.accepted_state_revision, 1)
        self.assertFalse(result.replayed)
        self.assertEqual(self.generator.calls, 1)
        self.assertEqual(self.aggregate.install_calls, 1)
        self.assertEqual(
            self.aggregate.document,
            {"world": {"facts": ["enriched"]}},
        )
        self.assertEqual(event_observations, [(False, 1, "commit:1", True)])
        self.assertEqual(self.aggregate.canonical_events, self.events)
        self.assertEqual(self.events[0].previous_accepted_state_revision, 0)
        self.assertEqual(self.events[0].resulting_accepted_state_revision, 1)

        category = next(
            category
            for category in self.ledger.snapshot().categories
            if category.category is ProductJobCategory.PRODUCT_ENRICHMENT
        )
        self.assertEqual(category.logical_jobs, 1)
        self.assertEqual(category.settled_jobs, 1)
        self.assertEqual(category.physical_workers, 0)
        self.assertEqual(self.ledger.snapshot().consumed_tokens, 12)
        self.assertEqual(self.ledger.snapshot().consumed_spend_units, 7)

        replay = self.coordinator.run(
            proposal_id="enrichment:one",
            session_id=self.aggregate.session_id,
        )
        self.assertEqual(replay.kind, ProductEnrichmentOutcomeKind.APPLIED)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.generator.calls, 1)
        self.assertEqual(len(self.events), 1)

    def test_initial_foreground_activity_defers_without_reservation(self) -> None:
        self.aggregate.set_foreground(True)

        deferred = self.coordinator.run(
            proposal_id="enrichment:deferred",
            session_id=self.aggregate.session_id,
        )

        self.assertEqual(deferred.kind, ProductEnrichmentOutcomeKind.DEFERRED)
        self.assertEqual(deferred.reason, ProductEnrichmentReason.FOREGROUND_ACTIVE)
        self.assertIsNone(deferred.proposal)
        self.assertEqual(self.generator.calls, 0)
        self.assertEqual(self.ledger.snapshot().logical_jobs, 0)

        self.aggregate.set_foreground(False)
        applied = self.coordinator.run(
            proposal_id="enrichment:deferred",
            session_id=self.aggregate.session_id,
        )
        self.assertEqual(applied.kind, ProductEnrichmentOutcomeKind.APPLIED)
        self.assertEqual(self.generator.calls, 1)

    def test_foreground_claim_during_generation_defers_then_reuses_proposal(
        self,
    ) -> None:
        self.generator.during_generate = lambda: self.aggregate.set_foreground(True)

        deferred = self.coordinator.run(
            proposal_id="enrichment:race",
            session_id=self.aggregate.session_id,
        )

        self.assertEqual(deferred.kind, ProductEnrichmentOutcomeKind.DEFERRED)
        self.assertIsNotNone(deferred.proposal)
        self.assertEqual(self.generator.calls, 1)
        self.assertEqual(self.aggregate.install_calls, 0)
        self.assertEqual(self.ledger.snapshot().unsettled_jobs, 0)
        self.assertEqual(self.ledger.snapshot().physical_workers, 0)

        self.aggregate.set_foreground(False)
        applied = self.coordinator.retry_apply("enrichment:race")
        self.assertEqual(applied.kind, ProductEnrichmentOutcomeKind.APPLIED)
        self.assertTrue(applied.replayed)
        self.assertEqual(self.generator.calls, 1)
        self.assertEqual(self.aggregate.install_calls, 1)

    def test_explicit_fence_terminalizes_a_ready_deferred_proposal(self) -> None:
        self.generator.during_generate = lambda: self.aggregate.set_foreground(True)
        deferred = self.coordinator.run(
            proposal_id="enrichment:deferred-fence",
            session_id=self.aggregate.session_id,
        )
        self.assertEqual(deferred.kind, ProductEnrichmentOutcomeKind.DEFERRED)

        self.assertEqual(self.coordinator.fence_session(self.aggregate.session_id), 1)
        self.aggregate.set_foreground(False)
        replay = self.coordinator.retry_apply("enrichment:deferred-fence")

        self.assertEqual(replay.kind, ProductEnrichmentOutcomeKind.FENCED)
        self.assertEqual(replay.reason, ProductEnrichmentReason.EXPLICIT_FENCE)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.aggregate.install_calls, 0)

    def test_stale_accepted_revision_fences_generated_proposal(self) -> None:
        self.generator.during_generate = self.aggregate.external_commit

        result = self.coordinator.run(
            proposal_id="enrichment:stale",
            session_id=self.aggregate.session_id,
        )

        self.assertEqual(result.kind, ProductEnrichmentOutcomeKind.FENCED)
        self.assertEqual(
            result.reason,
            ProductEnrichmentReason.STALE_ACCEPTED_REVISION,
        )
        self.assertEqual(self.aggregate.accepted_state_revision, 1)
        self.assertEqual(self.aggregate.install_calls, 0)
        self.assertEqual(self.events, [])

    def test_close_and_replacement_during_generation_fence_publication(self) -> None:
        cases = (
            (
                "closed",
                lambda aggregate: aggregate.close(),
                ProductEnrichmentReason.SESSION_CLOSED,
            ),
            (
                "replaced",
                lambda aggregate: aggregate.replace_session(),
                ProductEnrichmentReason.SESSION_CHANGED,
            ),
        )
        for label, mutate, expected_reason in cases:
            with self.subTest(label=label):
                aggregate = FakeAggregate()
                generator = RecordingGenerator(
                    aggregate,
                    during_generate=lambda aggregate=aggregate, mutate=mutate: mutate(
                        aggregate
                    ),
                )
                events: list[ProductEnrichmentAppliedEvent] = []
                coordinator = _coordinator(
                    aggregate,
                    generator,
                    _ledger(),
                    events,
                )
                result = coordinator.run(
                    proposal_id=f"enrichment:{label}",
                    session_id=aggregate.session_id,
                )
                self.assertEqual(result.kind, ProductEnrichmentOutcomeKind.FENCED)
                self.assertEqual(result.reason, expected_reason)
                self.assertEqual(aggregate.install_calls, 0)
                self.assertEqual(events, [])

    def test_explicit_fence_during_generation_does_not_pretend_worker_stopped(
        self,
    ) -> None:
        fence_observations = []

        def fence() -> None:
            fenced = self.coordinator.fence_session(self.aggregate.session_id)
            snapshot = self.ledger.snapshot()
            fence_observations.append(
                (fenced, snapshot.fenced_jobs, snapshot.physical_workers)
            )

        self.generator.during_generate = fence
        result = self.coordinator.run(
            proposal_id="enrichment:fenced",
            session_id=self.aggregate.session_id,
        )

        self.assertEqual(result.kind, ProductEnrichmentOutcomeKind.FENCED)
        self.assertEqual(result.reason, ProductEnrichmentReason.EXPLICIT_FENCE)
        self.assertEqual(fence_observations, [(1, 1, 1)])
        final = self.ledger.snapshot()
        self.assertEqual(final.physical_workers, 0)
        self.assertEqual(final.unsettled_jobs, 0)

    def test_worker_capacity_denial_keeps_one_retryable_reserved_proposal(
        self,
    ) -> None:
        ledger = _single_worker_ledger()
        ledger.reserve_job(
            logical_job_id="foreground:blocker",
            session_id=self.aggregate.session_id,
            category=ProductJobCategory.FOREGROUND_PRODUCT,
            reserved_tokens=10,
            reserved_spend_units=5,
        )
        ledger.claim_worker("foreground:blocker")
        coordinator = _coordinator(
            self.aggregate,
            self.generator,
            ledger,
            self.events,
        )

        with self.assertRaises(ProductAdmissionDenied):
            coordinator.run(
                proposal_id="enrichment:queued",
                session_id=self.aggregate.session_id,
            )
        self.assertEqual(self.generator.calls, 0)
        self.assertEqual(ledger.snapshot().logical_jobs, 2)

        ledger.settle_and_reap(
            "foreground:blocker",
            actual_tokens=1,
            actual_spend_units=1,
        )
        applied = coordinator.run(
            proposal_id="enrichment:queued",
            session_id=self.aggregate.session_id,
        )
        self.assertEqual(applied.kind, ProductEnrichmentOutcomeKind.APPLIED)
        self.assertEqual(self.generator.calls, 1)
        self.assertEqual(ledger.snapshot().physical_workers, 0)

    def test_fencing_a_queued_reservation_releases_its_budget_without_worker(
        self,
    ) -> None:
        ledger = _single_worker_ledger()
        ledger.reserve_job(
            logical_job_id="foreground:blocker",
            session_id=self.aggregate.session_id,
            category=ProductJobCategory.FOREGROUND_PRODUCT,
            reserved_tokens=10,
            reserved_spend_units=5,
        )
        ledger.claim_worker("foreground:blocker")
        coordinator = _coordinator(
            self.aggregate,
            self.generator,
            ledger,
            self.events,
        )
        with self.assertRaises(ProductAdmissionDenied):
            coordinator.run(
                proposal_id="enrichment:queued-fence",
                session_id=self.aggregate.session_id,
            )

        self.assertEqual(coordinator.fence_session(self.aggregate.session_id), 1)
        category = next(
            category
            for category in ledger.snapshot().categories
            if category.category is ProductJobCategory.PRODUCT_ENRICHMENT
        )
        self.assertEqual(category.settled_jobs, 1)
        self.assertEqual(category.physical_workers, 0)
        replay = coordinator.run(
            proposal_id="enrichment:queued-fence",
            session_id=self.aggregate.session_id,
        )
        self.assertEqual(replay.kind, ProductEnrichmentOutcomeKind.FENCED)
        self.assertEqual(replay.reason, ProductEnrichmentReason.EXPLICIT_FENCE)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.generator.calls, 0)

    def test_validator_rejection_is_safe_terminal_and_accounted(self) -> None:
        validated_documents: list[dict[str, object]] = []

        def reject(document) -> None:
            validated_documents.append(document)
            raise ValueError("private validator details")

        coordinator = _coordinator(
            self.aggregate,
            self.generator,
            self.ledger,
            self.events,
            validate_replacement=reject,
        )
        result = coordinator.run(
            proposal_id="enrichment:rejected",
            session_id=self.aggregate.session_id,
        )

        self.assertEqual(result.kind, ProductEnrichmentOutcomeKind.REJECTED)
        self.assertEqual(result.reason, ProductEnrichmentReason.VALIDATION_FAILED)
        self.assertIsNone(result.proposal)
        self.assertEqual(self.aggregate.install_calls, 0)
        self.assertEqual(len(validated_documents), 1)
        validated_documents[0]["world"] = "mutated validator copy"
        self.assertEqual(
            self.generator.document,
            {"world": {"facts": ["enriched"]}},
        )
        self.assertEqual(self.ledger.snapshot().consumed_tokens, 12)

    def test_unchanged_candidate_does_not_advance_revision_or_emit(self) -> None:
        generator = RecordingGenerator(
            self.aggregate,
            document={"world": {"facts": ["initial"]}},
        )
        coordinator = _coordinator(
            self.aggregate,
            generator,
            self.ledger,
            self.events,
        )

        result = coordinator.run(
            proposal_id="enrichment:unchanged",
            session_id=self.aggregate.session_id,
        )

        self.assertEqual(result.kind, ProductEnrichmentOutcomeKind.UNCHANGED)
        self.assertEqual(result.accepted_state_revision, 0)
        self.assertEqual(self.aggregate.install_calls, 0)
        self.assertEqual(self.events, [])

    def test_generation_failure_is_safe_and_charges_reserved_ceiling(self) -> None:
        class FailingGenerator:
            def generate(self, _request):
                raise RuntimeError("raw provider response and secret")

        coordinator = _coordinator(
            self.aggregate,
            FailingGenerator(),
            self.ledger,
            self.events,
        )
        result = coordinator.run(
            proposal_id="enrichment:failed",
            session_id=self.aggregate.session_id,
        )

        self.assertEqual(result.kind, ProductEnrichmentOutcomeKind.FAILED)
        self.assertEqual(result.reason, ProductEnrichmentReason.GENERATION_FAILED)
        self.assertNotIn("provider", repr(result))
        snapshot = self.ledger.snapshot()
        self.assertEqual(
            snapshot.consumed_tokens, _policy().reserved_tokens_per_proposal
        )
        self.assertEqual(
            snapshot.consumed_spend_units,
            _policy().reserved_spend_units_per_proposal,
        )
        self.assertEqual(snapshot.physical_workers, 0)

    def test_fatal_generator_terminalizes_and_reaps_before_propagating(self) -> None:
        class FatalGeneration(BaseException):
            pass

        class FatalGenerator:
            def generate(self, _request):
                raise FatalGeneration("unsafe fatal provider detail")

        coordinator = _coordinator(
            self.aggregate,
            FatalGenerator(),
            self.ledger,
            self.events,
        )

        with self.assertRaises(FatalGeneration):
            coordinator.run(
                proposal_id="enrichment:fatal-generation",
                session_id=self.aggregate.session_id,
            )

        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.unsettled_jobs, 0)
        self.assertEqual(snapshot.physical_workers, 0)
        self.assertEqual(
            snapshot.consumed_tokens,
            _policy().reserved_tokens_per_proposal,
        )
        replay = coordinator.run(
            proposal_id="enrichment:fatal-generation",
            session_id=self.aggregate.session_id,
        )
        self.assertEqual(replay.kind, ProductEnrichmentOutcomeKind.FAILED)
        self.assertEqual(replay.reason, ProductEnrichmentReason.GENERATION_FAILED)
        self.assertTrue(replay.replayed)

    def test_fatal_validator_terminalizes_after_truthful_reap(self) -> None:
        class FatalValidation(BaseException):
            pass

        def validate(_document) -> None:
            raise FatalValidation("unsafe fatal validator detail")

        coordinator = _coordinator(
            self.aggregate,
            self.generator,
            self.ledger,
            self.events,
            validate_replacement=validate,
        )

        with self.assertRaises(FatalValidation):
            coordinator.run(
                proposal_id="enrichment:fatal-validation",
                session_id=self.aggregate.session_id,
            )

        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.unsettled_jobs, 0)
        self.assertEqual(snapshot.physical_workers, 0)
        self.assertEqual(snapshot.consumed_tokens, self.generator.usage_tokens)
        replay = coordinator.run(
            proposal_id="enrichment:fatal-validation",
            session_id=self.aggregate.session_id,
        )
        self.assertEqual(replay.kind, ProductEnrichmentOutcomeKind.REJECTED)
        self.assertEqual(replay.reason, ProductEnrichmentReason.VALIDATION_FAILED)
        self.assertTrue(replay.replayed)

    def test_clock_failures_terminalize_instead_of_stranding_generation(self) -> None:
        class FatalClock(BaseException):
            pass

        def ordinary_clock() -> int:
            raise RuntimeError("unsafe clock detail")

        def fatal_clock() -> int:
            raise FatalClock("unsafe fatal clock detail")

        for label, clock, expected_exception in (
            ("ordinary", ordinary_clock, None),
            ("fatal", fatal_clock, FatalClock),
        ):
            with self.subTest(label=label):
                aggregate = FakeAggregate()
                generator = RecordingGenerator(aggregate)
                ledger = _ledger()
                coordinator = ProductEnrichmentCoordinator(
                    aggregate=aggregate,
                    generator=generator,
                    admission=ledger,
                    validate_replacement=lambda _document: None,
                    on_applied=lambda _event: None,
                    policy=_policy(),
                    clock_ms=clock,
                )
                proposal_id = f"enrichment:clock-{label}"
                if expected_exception is None:
                    result = coordinator.run(
                        proposal_id=proposal_id,
                        session_id=aggregate.session_id,
                    )
                    self.assertEqual(result.kind, ProductEnrichmentOutcomeKind.FAILED)
                else:
                    with self.assertRaises(expected_exception):
                        coordinator.run(
                            proposal_id=proposal_id,
                            session_id=aggregate.session_id,
                        )
                snapshot = ledger.snapshot()
                self.assertEqual(snapshot.unsettled_jobs, 0)
                self.assertEqual(snapshot.physical_workers, 0)
                replay = coordinator.run(
                    proposal_id=proposal_id,
                    session_id=aggregate.session_id,
                )
                self.assertEqual(replay.kind, ProductEnrichmentOutcomeKind.FAILED)
                self.assertEqual(
                    replay.reason,
                    ProductEnrichmentReason.INVALID_GENERATION,
                )
                self.assertTrue(replay.replayed)

    def test_invalid_usage_discards_candidate_and_charges_reserved_ceiling(
        self,
    ) -> None:
        generator = RecordingGenerator(
            self.aggregate,
            usage_tokens=_policy().reserved_tokens_per_proposal + 1,
        )
        coordinator = _coordinator(
            self.aggregate,
            generator,
            self.ledger,
            self.events,
        )
        result = coordinator.run(
            proposal_id="enrichment:bad-usage",
            session_id=self.aggregate.session_id,
        )

        self.assertEqual(result.kind, ProductEnrichmentOutcomeKind.FAILED)
        self.assertEqual(result.reason, ProductEnrichmentReason.INVALID_GENERATION)
        self.assertEqual(self.aggregate.install_calls, 0)
        self.assertEqual(
            self.ledger.snapshot().consumed_tokens,
            _policy().reserved_tokens_per_proposal,
        )

    def test_proposal_and_base_snapshot_are_deeply_immutable_copies(self) -> None:
        source = {"world": {"facts": ["candidate"]}}
        self.generator.document = source
        result = self.coordinator.run(
            proposal_id="enrichment:immutable",
            session_id=self.aggregate.session_id,
        )
        proposal = result.proposal
        assert proposal is not None
        source["world"]["facts"][0] = "caller mutation"
        self.aggregate.document["world"] = {"facts": ["later mutation"]}

        replacement_world = proposal.replacement_document["world"]
        assert isinstance(replacement_world, MappingProxyType)
        facts = replacement_world["facts"]
        self.assertEqual(facts, ("candidate",))
        with self.assertRaises(TypeError):
            replacement_world["facts"] = ()  # type: ignore[index]
        base_world = proposal.base.document["world"]
        assert isinstance(base_world, MappingProxyType)
        self.assertEqual(base_world["facts"], ("initial",))
        with self.assertRaises(FrozenInstanceError):
            proposal.base.accepted_state_revision = 99  # type: ignore[misc]

    def test_event_callback_failure_records_applied_truth_for_recovery(self) -> None:
        callback_observations: list[tuple[bool, bool]] = []

        def fail_event(event) -> None:
            self.assertEqual(self.aggregate.accepted_state_revision, 1)
            callback_observations.append(
                (
                    self.aggregate.lock_active,
                    self.aggregate.canonical_events[-1] is event,
                )
            )
            raise RuntimeError("journal unavailable")

        coordinator = _coordinator(
            self.aggregate,
            self.generator,
            self.ledger,
            self.events,
            on_applied=fail_event,
        )
        with self.assertRaises(ProductEnrichmentPublicationError) as raised:
            coordinator.run(
                proposal_id="enrichment:event-failure",
                session_id=self.aggregate.session_id,
            )

        self.assertEqual(raised.exception.applied.resulting_accepted_state_revision, 1)
        self.assertEqual(self.aggregate.accepted_state_revision, 1)
        self.assertEqual(callback_observations, [(False, True)])
        self.assertEqual(
            self.aggregate.canonical_events,
            [raised.exception.applied],
        )
        replay = coordinator.run(
            proposal_id="enrichment:event-failure",
            session_id=self.aggregate.session_id,
        )
        self.assertEqual(replay.kind, ProductEnrichmentOutcomeKind.APPLIED)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.aggregate.install_calls, 1)
        self.assertEqual(len(self.aggregate.canonical_events), 1)

    def test_failed_aggregate_commit_changes_neither_document_nor_event(self) -> None:
        self.aggregate.fail_commit = True

        with self.assertRaisesRegex(RuntimeError, "aggregate commit unavailable"):
            self.coordinator.run(
                proposal_id="enrichment:commit-retry",
                session_id=self.aggregate.session_id,
            )

        self.assertEqual(self.aggregate.accepted_state_revision, 0)
        self.assertEqual(
            self.aggregate.document,
            {"world": {"facts": ["initial"]}},
        )
        self.assertEqual(self.aggregate.canonical_events, [])
        self.assertEqual(self.events, [])

        self.aggregate.fail_commit = False
        applied = self.coordinator.retry_apply("enrichment:commit-retry")
        self.assertEqual(applied.kind, ProductEnrichmentOutcomeKind.APPLIED)
        self.assertEqual(self.aggregate.accepted_state_revision, 1)
        self.assertEqual(len(self.aggregate.canonical_events), 1)
        self.assertEqual(self.aggregate.canonical_events, self.events)

    def test_missing_or_terminal_session_never_reserves_provider_work(self) -> None:
        self.aggregate.available = False
        missing = self.coordinator.run(
            proposal_id="enrichment:missing",
            session_id=self.aggregate.session_id,
        )
        self.assertEqual(missing.kind, ProductEnrichmentOutcomeKind.FENCED)
        self.assertEqual(missing.reason, ProductEnrichmentReason.SESSION_UNAVAILABLE)

        aggregate = FakeAggregate()
        aggregate.close()
        ledger = _ledger()
        coordinator = _coordinator(
            aggregate,
            RecordingGenerator(aggregate),
            ledger,
            [],
        )
        closed = coordinator.run(
            proposal_id="enrichment:closed-before",
            session_id=aggregate.session_id,
        )
        self.assertEqual(closed.reason, ProductEnrichmentReason.SESSION_CLOSED)
        self.assertEqual(ledger.snapshot().logical_jobs, 0)

    def test_identity_conflict_and_retained_proposal_capacity_are_bounded(self) -> None:
        coordinator = _coordinator(
            self.aggregate,
            self.generator,
            self.ledger,
            self.events,
            policy=replace(_policy(), max_proposals=1),
        )
        coordinator.run(
            proposal_id="enrichment:one",
            session_id=self.aggregate.session_id,
        )

        with self.assertRaises(ProductEnrichmentDenied) as conflict:
            coordinator.run(
                proposal_id="enrichment:one",
                session_id="session:other",
            )
        self.assertEqual(
            conflict.exception.reason,
            ProductEnrichmentDenialReason.PROPOSAL_IDENTITY_CONFLICT,
        )
        with self.assertRaises(ProductEnrichmentDenied) as capacity:
            coordinator.run(
                proposal_id="enrichment:two",
                session_id=self.aggregate.session_id,
            )
        self.assertEqual(
            capacity.exception.reason,
            ProductEnrichmentDenialReason.PROPOSAL_CAPACITY_EXHAUSTED,
        )

    def test_concurrent_exact_identity_generates_and_applies_once(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def block() -> None:
            started.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release generator")

        self.generator.during_generate = block
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                self.coordinator.run,
                proposal_id="enrichment:concurrent",
                session_id=self.aggregate.session_id,
            )
            self.assertTrue(started.wait(timeout=5))
            concurrent = self.coordinator.run(
                proposal_id="enrichment:concurrent",
                session_id=self.aggregate.session_id,
            )
            release.set()
            first = first_future.result(timeout=5)

        self.assertEqual(concurrent.kind, ProductEnrichmentOutcomeKind.IN_PROGRESS)
        self.assertEqual(first.kind, ProductEnrichmentOutcomeKind.APPLIED)
        self.assertEqual(self.generator.calls, 1)
        self.assertEqual(self.aggregate.install_calls, 1)


class ProductEnrichmentValidationTests(unittest.TestCase):
    def test_policy_and_json_documents_have_absolute_validated_bounds(self) -> None:
        defaults = _policy()
        for name in (
            "max_proposals",
            "max_document_bytes",
            "max_document_depth",
            "max_document_items",
        ):
            for invalid in (0, -1, True, 2**63):
                with self.subTest(name=name, invalid=invalid):
                    with self.assertRaises(ValueError):
                        replace(defaults, **{name: invalid})
        for name in (
            "reserved_tokens_per_proposal",
            "reserved_spend_units_per_proposal",
        ):
            for invalid in (-1, True, 2**63):
                with self.subTest(name=name, invalid=invalid):
                    with self.assertRaises(ValueError):
                        replace(defaults, **{name: invalid})

        invalid_documents = (
            {"set": {1, 2}},
            {1: "non-string-key"},
            {"number": float("nan")},
            {"number": 2**53},
            {"text": "bad \ud800"},
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                with self.assertRaises((TypeError, ValueError)):
                    ProductEnrichmentGeneration(document)  # type: ignore[arg-type]

    def test_constructor_requires_all_explicit_integration_seams(self) -> None:
        aggregate = FakeAggregate()
        generator = RecordingGenerator(aggregate)
        ledger = _ledger()
        valid = {
            "aggregate": aggregate,
            "generator": generator,
            "admission": ledger,
            "validate_replacement": lambda _document: None,
            "on_applied": lambda _event: None,
        }
        invalid = (
            {"aggregate": object()},
            {"generator": object()},
            {"admission": object()},
            {"validate_replacement": None},
            {"on_applied": None},
            {"policy": object()},
            {"clock_ms": 1},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises(TypeError):
                    ProductEnrichmentCoordinator(**(valid | changed))  # type: ignore[arg-type]
        self.assertEqual(generator.calls, 0)
        self.assertEqual(aggregate.capture_calls, 0)
        self.assertEqual(ledger.snapshot().logical_jobs, 0)

    def test_retry_missing_and_invalid_identities_fail_without_work(self) -> None:
        aggregate = FakeAggregate()
        generator = RecordingGenerator(aggregate)
        coordinator = _coordinator(
            aggregate,
            generator,
            _ledger(),
            [],
        )
        with self.assertRaises(ProductEnrichmentDenied) as missing:
            coordinator.retry_apply("enrichment:missing")
        self.assertEqual(
            missing.exception.reason,
            ProductEnrichmentDenialReason.PROPOSAL_NOT_FOUND,
        )
        with self.assertRaises(ValueError):
            coordinator.run(proposal_id="bad id", session_id=aggregate.session_id)
        self.assertEqual(generator.calls, 0)


def _policy() -> ProductEnrichmentPolicy:
    return ProductEnrichmentPolicy(
        max_proposals=20,
        max_document_bytes=10_000,
        max_document_depth=10,
        max_document_items=100,
        reserved_tokens_per_proposal=100,
        reserved_spend_units_per_proposal=50,
    )


def _ledger() -> ProductAdmissionLedger:
    ledger = ProductAdmissionLedger(
        ProductAdmissionLimits(
            max_active_sessions=4,
            max_logical_jobs=30,
            max_physical_workers=4,
            token_budget=10_000,
            spend_budget_units=10_000,
        )
    )
    ledger.admit_session("session:one")
    return ledger


def _single_worker_ledger() -> ProductAdmissionLedger:
    ledger = ProductAdmissionLedger(
        ProductAdmissionLimits(
            max_active_sessions=2,
            max_logical_jobs=10,
            max_physical_workers=1,
            token_budget=10_000,
            spend_budget_units=10_000,
        )
    )
    ledger.admit_session("session:one")
    return ledger


def _coordinator(
    aggregate,
    generator,
    ledger,
    events,
    *,
    validate_replacement=None,
    on_applied=None,
    policy=None,
) -> ProductEnrichmentCoordinator:
    return ProductEnrichmentCoordinator(
        aggregate=aggregate,
        generator=generator,
        admission=ledger,
        validate_replacement=validate_replacement or (lambda _document: None),
        on_applied=on_applied or events.append,
        policy=policy or _policy(),
        clock_ms=lambda: 1_234,
    )


if __name__ == "__main__":
    unittest.main()
