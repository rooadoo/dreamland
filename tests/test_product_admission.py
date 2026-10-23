from __future__ import annotations

import json
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from dreamland.product.admission import (
    AdmissionDenialReason,
    ProductAdmissionDenied,
    ProductAdmissionLedger,
    ProductAdmissionLimits,
    ProductJobCategory,
)


class ProductAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = ProductAdmissionLedger(
            ProductAdmissionLimits(
                max_active_sessions=2,
                max_logical_jobs=8,
                max_physical_workers=2,
                token_budget=1_000,
                spend_budget_units=500,
            )
        )
        self.ledger.admit_session("session-a")

    def test_module_import_and_construction_are_inert(self) -> None:
        code = """
import json
import sys
import threading

import dreamland

modules_before = frozenset(sys.modules)
before = tuple(thread.name for thread in threading.enumerate())
from dreamland.product.admission import ProductAdmissionLedger
ledger = ProductAdmissionLedger()
after = tuple(thread.name for thread in threading.enumerate())
forbidden = [
    "dreamland.__main__",
    "dreamland.backend",
    "dreamland.background_agents.enrichment.service",
    "dreamland.admin_api",
    "dreamland.ssh_server",
]
print(json.dumps({
    "before": before,
    "after": after,
    "forbidden": [
        name for name in forbidden
        if name in sys.modules and name not in modules_before
    ],
    "snapshot": ledger.snapshot().logical_jobs,
}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["before"], payload["after"])
        self.assertEqual(payload["forbidden"], [])
        self.assertEqual(payload["snapshot"], 0)

    def test_limits_reject_boolean_negative_and_inconsistent_values(self) -> None:
        invalid = (
            {"max_active_sessions": 0},
            {"max_logical_jobs": True},
            {"max_physical_workers": -1},
            {"token_budget": -1},
            {"spend_budget_units": True},
            {"max_logical_jobs": 1, "max_physical_workers": 2},
        )

        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ProductAdmissionLimits(**values)

    def test_exact_product_categories_do_not_advertise_stage_reservation(self) -> None:
        self.assertEqual(
            [category.value for category in ProductJobCategory],
            [
                "foreground_product",
                "product_enrichment",
                "future_forge",
            ],
        )
        snapshot = asdict(self.ledger.snapshot())
        self.assertNotIn("stage", json.dumps(snapshot, sort_keys=True).lower())

    def test_session_admission_is_bounded_idempotent_and_releasable(self) -> None:
        replay = self.ledger.admit_session("session-a")
        admitted = self.ledger.admit_session("session-b")

        self.assertTrue(replay.replayed)
        self.assertFalse(admitted.replayed)
        self.assertEqual(self.ledger.snapshot().active_sessions, 2)
        denial = self._denial(
            lambda: self.ledger.admit_session("session-c"),
            AdmissionDenialReason.SESSION_CAPACITY_EXHAUSTED,
        )
        self.assertTrue(denial.retryable)

        self.assertTrue(self.ledger.release_session("session-a").released)
        self.assertFalse(self.ledger.release_session("session-a").released)
        self.assertFalse(self.ledger.admit_session("session-c").replayed)
        self.assertEqual(self.ledger.snapshot().active_sessions, 2)

    def test_session_replacement_is_atomic_count_neutral_and_preserves_jobs(
        self,
    ) -> None:
        self.ledger.admit_session("capacity-peer")
        self._reserve(job_id="job-a", tokens=100, spend=40)
        before = self.ledger.snapshot()

        replacement = self.ledger.replace_session("session-a", "session-b")

        self.assertTrue(replacement.replaced)
        self.assertFalse(replacement.replayed)
        self.assertEqual(self.ledger.snapshot(), before)
        self._denial(
            lambda: self._reserve(job_id="job-new", session_id="session-a"),
            AdmissionDenialReason.SESSION_UNAVAILABLE,
        )
        self.assertFalse(
            self._reserve(job_id="job-new", session_id="session-b").replayed
        )
        self.assertTrue(
            self._reserve(
                job_id="job-a",
                session_id="session-a",
                tokens=100,
                spend=40,
            ).replayed
        )
        self._denial(
            lambda: self._reserve(
                job_id="job-a",
                session_id="session-b",
                tokens=100,
                spend=40,
            ),
            AdmissionDenialReason.LOGICAL_JOB_CONFLICT,
        )

    def test_session_replacement_exact_replay_is_state_convergent(self) -> None:
        first = self.ledger.replace_session("session-a", "session-b")
        replay = self.ledger.replace_session("session-a", "session-b")

        self.assertTrue(first.replaced)
        self.assertFalse(first.replayed)
        self.assertFalse(replay.replaced)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.ledger.snapshot().active_sessions, 1)

    def test_session_replacement_rejects_conflict_without_mutation(self) -> None:
        self.ledger.admit_session("session-b")
        before = self.ledger.snapshot()

        denial = self._denial(
            lambda: self.ledger.replace_session("session-a", "session-b"),
            AdmissionDenialReason.SESSION_CONFLICT,
        )

        self.assertFalse(denial.retryable)
        self.assertEqual(self.ledger.snapshot(), before)

    def test_session_replacement_requires_an_available_old_identity(self) -> None:
        before = self.ledger.snapshot()

        denial = self._denial(
            lambda: self.ledger.replace_session("missing-session", "session-b"),
            AdmissionDenialReason.SESSION_UNAVAILABLE,
        )

        self.assertFalse(denial.retryable)
        self.assertEqual(self.ledger.snapshot(), before)

    def test_concurrent_exact_session_replacements_transfer_once(self) -> None:
        results = self._race(
            16,
            lambda _index: self.ledger.replace_session(
                "session-a",
                "session-b",
            ),
        )

        self.assertEqual(sum(result.replaced for result in results), 1)
        self.assertEqual(sum(result.replayed for result in results), 15)
        self.assertEqual(self.ledger.snapshot().active_sessions, 1)

    def test_reservation_requires_an_active_session(self) -> None:
        denial = self._denial(
            lambda: self._reserve(job_id="job-a", session_id="missing-session"),
            AdmissionDenialReason.SESSION_UNAVAILABLE,
        )

        self.assertFalse(denial.retryable)
        self.assertEqual(self.ledger.snapshot().logical_jobs, 0)

    def test_exact_job_retry_reserves_once_and_changed_identity_conflicts(self) -> None:
        first = self._reserve(job_id="job-a", tokens=100, spend=40)
        replay = self._reserve(job_id="job-a", tokens=100, spend=40)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.ledger.snapshot().logical_jobs, 1)
        self.assertEqual(self.ledger.snapshot().reserved_tokens, 100)
        self.assertEqual(self.ledger.snapshot().reserved_spend_units, 40)

        changed_requests = (
            {"session_id": "session-b"},
            {"category": ProductJobCategory.PRODUCT_ENRICHMENT},
            {"tokens": 101},
            {"spend": 41},
        )
        self.ledger.admit_session("session-b")
        for changed in changed_requests:
            values = {
                "job_id": "job-a",
                "session_id": "session-a",
                "category": ProductJobCategory.FOREGROUND_PRODUCT,
                "tokens": 100,
                "spend": 40,
                **changed,
            }
            with self.subTest(changed=changed):
                self._denial(
                    lambda values=values: self._reserve(**values),
                    AdmissionDenialReason.LOGICAL_JOB_CONFLICT,
                )
        self.assertEqual(self.ledger.snapshot().reserved_tokens, 100)
        self.assertEqual(self.ledger.snapshot().reserved_spend_units, 40)

    def test_released_session_denies_new_work_but_preserves_exact_replay(self) -> None:
        first = self._reserve(job_id="job-a", tokens=100, spend=40)
        self.ledger.release_session("session-a")

        replay = self._reserve(job_id="job-a", tokens=100, spend=40)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self._denial(
            lambda: self._reserve(job_id="job-b"),
            AdmissionDenialReason.SESSION_UNAVAILABLE,
        )

    def test_token_and_spend_denials_do_not_partially_reserve(self) -> None:
        self._reserve(job_id="job-a", tokens=900, spend=100)
        before = self.ledger.snapshot()

        token_denial = self._denial(
            lambda: self._reserve(job_id="job-token", tokens=101, spend=0),
            AdmissionDenialReason.TOKEN_BUDGET_EXHAUSTED,
        )
        spend_denial = self._denial(
            lambda: self._reserve(job_id="job-spend", tokens=0, spend=401),
            AdmissionDenialReason.SPEND_BUDGET_EXHAUSTED,
        )

        self.assertTrue(token_denial.retryable)
        self.assertTrue(spend_denial.retryable)
        self.assertEqual(self.ledger.snapshot(), before)

    def test_consumed_usage_is_not_replenished_after_settlement(self) -> None:
        self._reserve(job_id="job-a", tokens=600, spend=300)
        self.ledger.claim_worker("job-a")
        settlement, reap = self.ledger.settle_and_reap(
            "job-a",
            actual_tokens=550,
            actual_spend_units=275,
        )

        self.assertFalse(settlement.replayed)
        self.assertTrue(reap.reaped)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.reserved_tokens, 0)
        self.assertEqual(snapshot.consumed_tokens, 550)
        self.assertEqual(snapshot.reserved_spend_units, 0)
        self.assertEqual(snapshot.consumed_spend_units, 275)
        self._denial(
            lambda: self._reserve(job_id="job-b", tokens=451, spend=0),
            AdmissionDenialReason.TOKEN_BUDGET_EXHAUSTED,
        )
        self._denial(
            lambda: self._reserve(job_id="job-c", tokens=0, spend=226),
            AdmissionDenialReason.SPEND_BUDGET_EXHAUSTED,
        )

    def test_logical_identity_capacity_remains_bounded_after_settlement(self) -> None:
        ledger = ProductAdmissionLedger(
            ProductAdmissionLimits(
                max_active_sessions=1,
                max_logical_jobs=2,
                max_physical_workers=1,
                token_budget=10,
                spend_budget_units=10,
            )
        )
        ledger.admit_session("session-a")
        for index in range(2):
            job_id = f"job-{index}"
            ledger.reserve_job(
                logical_job_id=job_id,
                session_id="session-a",
                category=ProductJobCategory.FOREGROUND_PRODUCT,
                reserved_tokens=0,
                reserved_spend_units=0,
            )
            ledger.claim_worker(job_id)
            ledger.settle_and_reap(
                job_id,
                actual_tokens=0,
                actual_spend_units=0,
            )

        self._denial(
            lambda: ledger.reserve_job(
                logical_job_id="job-2",
                session_id="session-a",
                category=ProductJobCategory.FOREGROUND_PRODUCT,
                reserved_tokens=0,
                reserved_spend_units=0,
            ),
            AdmissionDenialReason.LOGICAL_JOB_CAPACITY_EXHAUSTED,
        )
        self.assertEqual(ledger.snapshot().logical_jobs, 2)
        self.assertEqual(ledger.snapshot().settled_jobs, 2)

    def test_physical_worker_capacity_is_independent_from_logical_reservation(
        self,
    ) -> None:
        self._reserve(job_id="job-a")
        self._reserve(job_id="job-b")
        self._reserve(job_id="job-c")
        self.ledger.claim_worker("job-a")
        self.ledger.claim_worker("job-b")

        denial = self._denial(
            lambda: self.ledger.claim_worker("job-c"),
            AdmissionDenialReason.PHYSICAL_WORKER_CAPACITY_EXHAUSTED,
        )
        self.assertTrue(denial.retryable)
        self.assertEqual(self.ledger.snapshot().unsettled_jobs, 3)
        self.assertEqual(self.ledger.snapshot().physical_workers, 2)

        self.ledger.settle_and_reap(
            "job-a",
            actual_tokens=10,
            actual_spend_units=5,
        )
        self.assertFalse(self.ledger.claim_worker("job-c").replayed)
        self.assertEqual(self.ledger.snapshot().physical_workers, 2)

    def test_atomic_reserve_and_claim_rolls_back_new_capacity_rejection(
        self,
    ) -> None:
        ledger = ProductAdmissionLedger(
            ProductAdmissionLimits(
                max_active_sessions=1,
                max_logical_jobs=2,
                max_physical_workers=1,
                token_budget=200,
                spend_budget_units=100,
            )
        )
        ledger.admit_session("session-a")
        ledger.reserve_and_claim_worker(
            logical_job_id="job-a",
            session_id="session-a",
            category=ProductJobCategory.FOREGROUND_PRODUCT,
            reserved_tokens=100,
            reserved_spend_units=50,
        )
        before = ledger.snapshot()

        denial = self._denial(
            lambda: ledger.reserve_and_claim_worker(
                logical_job_id="job-b",
                session_id="session-a",
                category=ProductJobCategory.FOREGROUND_PRODUCT,
                reserved_tokens=100,
                reserved_spend_units=50,
            ),
            AdmissionDenialReason.PHYSICAL_WORKER_CAPACITY_EXHAUSTED,
        )

        self.assertTrue(denial.retryable)
        self.assertEqual(ledger.snapshot(), before)
        ledger.settle_and_reap(
            "job-a",
            actual_tokens=0,
            actual_spend_units=0,
        )
        reservation, claim = ledger.reserve_and_claim_worker(
            logical_job_id="job-b",
            session_id="session-a",
            category=ProductJobCategory.FOREGROUND_PRODUCT,
            reserved_tokens=100,
            reserved_spend_units=50,
        )
        self.assertFalse(reservation.replayed)
        self.assertFalse(claim.replayed)

    def test_undispatched_job_compensation_releases_all_reserved_capacity(
        self,
    ) -> None:
        self.ledger.reserve_and_claim_worker(
            logical_job_id="job-a",
            session_id="session-a",
            category=ProductJobCategory.FOREGROUND_PRODUCT,
            reserved_tokens=100,
            reserved_spend_units=50,
        )

        discarded = self.ledger.discard_undispatched_job("job-a")
        replay = self.ledger.discard_undispatched_job("job-a")

        self.assertTrue(discarded.discarded)
        self.assertTrue(discarded.worker_released)
        self.assertFalse(replay.discarded)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.logical_jobs, 0)
        self.assertEqual(snapshot.physical_workers, 0)
        self.assertEqual(snapshot.reserved_tokens, 0)
        self.assertEqual(snapshot.reserved_spend_units, 0)

    def test_fence_and_settlement_do_not_release_physical_worker_until_reap(
        self,
    ) -> None:
        self._reserve(job_id="job-a", tokens=100, spend=50)
        self.ledger.claim_worker("job-a")

        first_fence = self.ledger.fence_job("job-a")
        replay_fence = self.ledger.fence_job("job-a")
        settlement = self.ledger.settle_job(
            "job-a",
            actual_tokens=100,
            actual_spend_units=50,
        )

        self.assertTrue(first_fence.fenced)
        self.assertFalse(first_fence.replayed)
        self.assertTrue(replay_fence.replayed)
        self.assertFalse(settlement.replayed)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.physical_workers, 1)
        self.assertEqual(snapshot.reserved_tokens, 0)
        self.assertEqual(snapshot.consumed_tokens, 100)

        first_reap = self.ledger.reap_worker("job-a")
        replay_reap = self.ledger.reap_worker("job-a")
        self.assertTrue(first_reap.reaped)
        self.assertFalse(first_reap.replayed)
        self.assertFalse(replay_reap.reaped)
        self.assertTrue(replay_reap.replayed)
        self.assertEqual(self.ledger.snapshot().physical_workers, 0)

    def test_fenced_queued_job_cannot_claim_a_worker_and_settles_at_zero(self) -> None:
        self._reserve(job_id="job-a", tokens=100, spend=50)
        self.ledger.fence_job("job-a")

        self._denial(
            lambda: self.ledger.claim_worker("job-a"),
            AdmissionDenialReason.LOGICAL_JOB_FENCED,
        )
        settlement = self.ledger.settle_job(
            "job-a",
            actual_tokens=0,
            actual_spend_units=0,
        )
        self.assertFalse(settlement.replayed)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.unsettled_jobs, 0)
        self.assertEqual(snapshot.physical_workers, 0)
        self.assertEqual(snapshot.reserved_tokens, 0)

    def test_unclaimed_job_cannot_report_provider_usage_or_be_reaped(self) -> None:
        self._reserve(job_id="job-a", tokens=100, spend=50)

        self._denial(
            lambda: self.ledger.settle_job(
                "job-a",
                actual_tokens=1,
                actual_spend_units=0,
            ),
            AdmissionDenialReason.ACTUAL_USAGE_EXCEEDS_RESERVATION,
        )
        self._denial(
            lambda: self.ledger.reap_worker("job-a"),
            AdmissionDenialReason.PHYSICAL_WORKER_NOT_CLAIMED,
        )
        self.assertEqual(self.ledger.snapshot().reserved_tokens, 100)

    def test_settle_and_reap_denial_is_atomic_without_a_claimed_worker(self) -> None:
        self._reserve(job_id="job-a", tokens=100, spend=50)
        before = self.ledger.snapshot()

        self._denial(
            lambda: self.ledger.settle_and_reap(
                "job-a",
                actual_tokens=0,
                actual_spend_units=0,
            ),
            AdmissionDenialReason.PHYSICAL_WORKER_NOT_CLAIMED,
        )

        self.assertEqual(self.ledger.snapshot(), before)

    def test_actual_usage_must_not_exceed_the_worst_case_reservation(self) -> None:
        self._reserve(job_id="job-a", tokens=100, spend=50)
        self.ledger.claim_worker("job-a")
        before = self.ledger.snapshot()

        for tokens, spend in ((101, 0), (0, 51)):
            with self.subTest(tokens=tokens, spend=spend):
                self._denial(
                    lambda tokens=tokens, spend=spend: self.ledger.settle_job(
                        "job-a",
                        actual_tokens=tokens,
                        actual_spend_units=spend,
                    ),
                    AdmissionDenialReason.ACTUAL_USAGE_EXCEEDS_RESERVATION,
                )
                self.assertEqual(self.ledger.snapshot(), before)

    def test_exact_settlement_replays_and_changed_usage_conflicts(self) -> None:
        self._reserve(job_id="job-a", tokens=100, spend=50)
        self.ledger.claim_worker("job-a")
        first = self.ledger.settle_job(
            "job-a",
            actual_tokens=80,
            actual_spend_units=30,
        )
        replay = self.ledger.settle_job(
            "job-a",
            actual_tokens=80,
            actual_spend_units=30,
        )

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        before = self.ledger.snapshot()
        self._denial(
            lambda: self.ledger.settle_job(
                "job-a",
                actual_tokens=79,
                actual_spend_units=30,
            ),
            AdmissionDenialReason.SETTLEMENT_CONFLICT,
        )
        self.assertEqual(self.ledger.snapshot(), before)

    def test_snapshot_contains_counts_not_job_or_session_identities(self) -> None:
        self._reserve(
            job_id="sensitive-job-id",
            category=ProductJobCategory.PRODUCT_ENRICHMENT,
            tokens=100,
            spend=50,
        )
        self.ledger.claim_worker("sensitive-job-id")
        snapshot = self.ledger.snapshot()
        serialized = json.dumps(asdict(snapshot), sort_keys=True)

        self.assertNotIn("sensitive-job-id", serialized)
        self.assertNotIn("session-a", serialized)
        self.assertEqual(snapshot.active_sessions, 1)
        self.assertEqual(snapshot.logical_jobs, 1)
        self.assertEqual(snapshot.unsettled_jobs, 1)
        self.assertEqual(snapshot.physical_workers, 1)
        self.assertEqual(
            [
                (category.category.value, category.logical_jobs)
                for category in snapshot.categories
            ],
            [
                ("foreground_product", 0),
                ("product_enrichment", 1),
                ("future_forge", 0),
            ],
        )

    def test_concurrent_exact_reservations_have_one_logical_effect(self) -> None:
        results = self._race(
            16,
            lambda _index: self._reserve(
                job_id="job-shared",
                tokens=100,
                spend=40,
            ),
        )

        self.assertEqual(sum(not result.replayed for result in results), 1)
        self.assertEqual(sum(result.replayed for result in results), 15)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.logical_jobs, 1)
        self.assertEqual(snapshot.reserved_tokens, 100)
        self.assertEqual(snapshot.reserved_spend_units, 40)

    def test_concurrent_distinct_reservations_never_overcommit_budget(self) -> None:
        ledger = ProductAdmissionLedger(
            ProductAdmissionLimits(
                max_active_sessions=1,
                max_logical_jobs=16,
                max_physical_workers=4,
                token_budget=300,
                spend_budget_units=120,
            )
        )
        ledger.admit_session("session-a")

        def reserve(index: int) -> AdmissionDenialReason | None:
            try:
                ledger.reserve_job(
                    logical_job_id=f"job-{index}",
                    session_id="session-a",
                    category=ProductJobCategory.FOREGROUND_PRODUCT,
                    reserved_tokens=100,
                    reserved_spend_units=40,
                )
            except ProductAdmissionDenied as error:
                return error.reason
            return None

        outcomes = self._race(12, reserve)

        self.assertEqual(outcomes.count(None), 3)
        self.assertEqual(
            sum(
                reason
                in {
                    AdmissionDenialReason.TOKEN_BUDGET_EXHAUSTED,
                    AdmissionDenialReason.SPEND_BUDGET_EXHAUSTED,
                }
                for reason in outcomes
            ),
            9,
        )
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.logical_jobs, 3)
        self.assertEqual(snapshot.reserved_tokens, 300)
        self.assertEqual(snapshot.reserved_spend_units, 120)

    def test_concurrent_worker_claims_obey_the_physical_limit(self) -> None:
        ledger = ProductAdmissionLedger(
            ProductAdmissionLimits(
                max_active_sessions=1,
                max_logical_jobs=12,
                max_physical_workers=3,
                token_budget=12,
                spend_budget_units=12,
            )
        )
        ledger.admit_session("session-a")
        for index in range(12):
            ledger.reserve_job(
                logical_job_id=f"job-{index}",
                session_id="session-a",
                category=ProductJobCategory.FUTURE_FORGE,
                reserved_tokens=1,
                reserved_spend_units=1,
            )

        def claim(index: int) -> AdmissionDenialReason | None:
            try:
                ledger.claim_worker(f"job-{index}")
            except ProductAdmissionDenied as error:
                return error.reason
            return None

        outcomes = self._race(12, claim)

        self.assertEqual(outcomes.count(None), 3)
        self.assertEqual(
            outcomes.count(AdmissionDenialReason.PHYSICAL_WORKER_CAPACITY_EXHAUSTED),
            9,
        )
        self.assertEqual(ledger.snapshot().physical_workers, 3)

    def test_concurrent_settle_and_reap_are_exactly_once(self) -> None:
        self._reserve(job_id="job-a", tokens=100, spend=50)
        self.ledger.claim_worker("job-a")
        self.ledger.fence_job("job-a")

        settlements = self._race(
            12,
            lambda _index: self.ledger.settle_job(
                "job-a",
                actual_tokens=90,
                actual_spend_units=45,
            ),
        )
        self.assertEqual(sum(not result.replayed for result in settlements), 1)
        self.assertEqual(self.ledger.snapshot().physical_workers, 1)

        reaps = self._race(
            12,
            lambda _index: self.ledger.reap_worker("job-a"),
        )
        self.assertEqual(sum(result.reaped for result in reaps), 1)
        self.assertEqual(sum(result.replayed for result in reaps), 11)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.physical_workers, 0)
        self.assertEqual(snapshot.reserved_tokens, 0)
        self.assertEqual(snapshot.consumed_tokens, 90)
        self.assertEqual(snapshot.reserved_spend_units, 0)
        self.assertEqual(snapshot.consumed_spend_units, 45)

    def test_invalid_inputs_fail_without_mutating_counters(self) -> None:
        before = self.ledger.snapshot()
        invalid_calls = (
            lambda: self.ledger.admit_session(""),
            lambda: self.ledger.replace_session("", "session-b"),
            lambda: self.ledger.replace_session("session-a", "bad session"),
            lambda: self.ledger.replace_session("session-a", "session-a"),
            lambda: self._reserve(job_id="job with spaces"),
            lambda: self._reserve(job_id="job-a", category="stage"),
            lambda: self._reserve(job_id="job-a", tokens=-1),
            lambda: self._reserve(job_id="job-a", spend=True),
        )

        for operation in invalid_calls:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()
        self.assertEqual(self.ledger.snapshot(), before)

    def _reserve(
        self,
        *,
        job_id: str,
        session_id: str = "session-a",
        category: ProductJobCategory | str = ProductJobCategory.FOREGROUND_PRODUCT,
        tokens: int = 10,
        spend: int = 5,
    ):
        return self.ledger.reserve_job(
            logical_job_id=job_id,
            session_id=session_id,
            category=category,
            reserved_tokens=tokens,
            reserved_spend_units=spend,
        )

    def _denial(
        self,
        operation,
        expected: AdmissionDenialReason,
    ) -> ProductAdmissionDenied:
        with self.assertRaises(ProductAdmissionDenied) as captured:
            operation()
        self.assertEqual(captured.exception.reason, expected)
        self.assertNotIn("session-a", captured.exception.message)
        self.assertNotIn("job-a", captured.exception.message)
        return captured.exception

    def _race(self, count, operation):
        barrier = threading.Barrier(count)

        def invoke(index):
            barrier.wait()
            return operation(index)

        with ThreadPoolExecutor(max_workers=count) as executor:
            return list(executor.map(invoke, range(count)))


if __name__ == "__main__":
    unittest.main()
