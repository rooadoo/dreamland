from __future__ import annotations

import hashlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from dreamland.product.attendee import AttendeeContextPolicy
from dreamland.product.attendee_security import (
    AttendeeRequestAdmissionDenied,
    AttendeeRequestAdmissionLedger,
    AttendeeRequestAdmissionPolicy,
)
from dreamland.product.launch_selection import LaunchSelectionError
from dreamland.product.lifecycle import ProductLifecycleError
from tests.test_product_attendee import (
    BFF_AUTHORITY,
    NOTICE_VERSION,
    SELECTION_ID,
    SHARED_EPOCH,
    CountingExecutor,
    ManualClock,
    ManualTasks,
    _bootstrap,
    _context_id,
    _exchange,
    _service,
)


class AttendeeRequestAdmissionTests(unittest.TestCase):
    def test_policy_rejects_boolean_zero_and_unsafe_values(self) -> None:
        for name, value in (
            ("window_ms", 0),
            ("max_fresh_operations_per_context", True),
            ("max_fresh_operations_global", -1),
            ("max_tracked_contexts", 2**53),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                AttendeeRequestAdmissionPolicy(**{name: value})

    def test_context_and_global_windows_are_atomic_bounded_and_monotonic(
        self,
    ) -> None:
        ledger = AttendeeRequestAdmissionLedger(
            AttendeeRequestAdmissionPolicy(
                window_ms=100,
                max_fresh_operations_per_context=2,
                max_fresh_operations_global=3,
                max_tracked_contexts=2,
            )
        )
        context_a = hashlib.sha256(b"context-a").hexdigest()
        context_b = hashlib.sha256(b"context-b").hexdigest()
        ledger.admit_fresh(context_sha256=context_a, deadline_ms=10)
        ledger.admit_fresh(context_sha256=context_a, deadline_ms=10)
        with self.assertRaises(AttendeeRequestAdmissionDenied) as captured:
            ledger.admit_fresh(context_sha256=context_a, deadline_ms=10)
        self.assertEqual(captured.exception.retry_after_ms, 100)
        ledger.admit_fresh(context_sha256=context_b, deadline_ms=10)
        with self.assertRaises(AttendeeRequestAdmissionDenied) as captured:
            ledger.admit_fresh(context_sha256=context_b, deadline_ms=10)
        self.assertEqual(captured.exception.retry_after_ms, 100)
        self.assertEqual(
            ledger.snapshot(),
            {"window_ms": 100, "tracked_contexts": 2, "global_accepted": 3},
        )

        ledger.admit_fresh(context_sha256=context_b, deadline_ms=110)
        self.assertEqual(ledger.snapshot()["tracked_contexts"], 1)
        with self.assertRaises(RuntimeError):
            ledger.admit_fresh(context_sha256=context_b, deadline_ms=109)
        with self.assertRaises(ValueError):
            ledger.admit_fresh(context_sha256="127.0.0.1", deadline_ms=110)

    def test_concurrent_admission_cannot_oversubscribe_context_limit(self) -> None:
        ledger = AttendeeRequestAdmissionLedger(
            AttendeeRequestAdmissionPolicy(
                max_fresh_operations_per_context=5,
                max_fresh_operations_global=100,
                max_tracked_contexts=1,
            )
        )
        context_sha256 = hashlib.sha256(b"one-context").hexdigest()
        barrier = threading.Barrier(20)

        def admit(_index: int) -> bool:
            barrier.wait()
            try:
                ledger.admit_fresh(
                    context_sha256=context_sha256,
                    deadline_ms=1,
                )
            except AttendeeRequestAdmissionDenied:
                return False
            return True

        with ThreadPoolExecutor(max_workers=20) as pool:
            accepted = tuple(pool.map(admit, range(20)))
        self.assertEqual(sum(accepted), 5)
        self.assertEqual(ledger.snapshot()["global_accepted"], 5)


class PublicAttendeeSecurityTests(unittest.TestCase):
    def test_revoke_replay_capacity_must_cover_every_context(self) -> None:
        positional_rc4_policy = AttendeeContextPolicy(
            2,
            5_000,
            8,
            32,
            100,
            10_000,
        )
        self.assertEqual(positional_rc4_policy.admission_opens_at_ms, 100)
        self.assertEqual(positional_rc4_policy.admission_closes_at_ms, 10_000)
        self.assertFalse(positional_rc4_policy.bootstrap_closes_at_admission)
        prior_compatible = AttendeeContextPolicy(
            max_contexts=1_024,
            max_bootstrap_idempotency_records=2_048,
        )
        self.assertGreaterEqual(
            prior_compatible.max_revoke_idempotency_records,
            prior_compatible.max_contexts,
        )
        with self.assertRaisesRegex(ValueError, "revoke idempotency capacity"):
            AttendeeContextPolicy(
                max_contexts=2,
                max_revoke_idempotency_records=1,
            )
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            AttendeeContextPolicy(bootstrap_closes_at_admission=1)

    def _composition(
        self,
        *,
        per_context: int,
        global_limit: int = 100,
    ):
        clock = ManualClock()
        tasks = ManualTasks()
        executor = CountingExecutor()
        admission = AttendeeRequestAdmissionLedger(
            AttendeeRequestAdmissionPolicy(
                window_ms=1_000,
                max_fresh_operations_per_context=per_context,
                max_fresh_operations_global=global_limit,
                max_tracked_contexts=2,
            )
        )
        service, registry, core, actors = _service(
            clock=clock,
            tasks=tasks,
            executor=executor,
            request_admission=admission,
        )
        return clock, tasks, executor, admission, service, registry, core, actors

    def test_drain_blocks_fresh_private_work_after_replay_classification(
        self,
    ) -> None:
        (
            _clock,
            _tasks,
            _executor,
            admission,
            service,
            _registry,
            _core,
            _actors,
        ) = self._composition(per_context=100)
        context_id = _context_id("a")
        actor = _bootstrap(service, context_id, "bootstrap")["actor_context"]
        _exchange(service, actor, "exchange")

        # Exercise the catalog exchange claim independently of a live generated
        # world.  The invalid eligibility proof is a deterministic, claimed
        # result whose exact replay must remain stable while draining.
        catalog_arguments = {
            "actor_authority": actor,
            "expected_epoch": SHARED_EPOCH,
            "offering_id": "offering:attendee-authored-a1",
            "offering_revision": "offering-revision:drain-000000000000000000",
            "notice_version_presented": NOTICE_VERSION,
            "idempotency_key": "catalog-error",
        }
        with (
            patch.object(service, "_generated_world_access", True),
            patch.object(service, "_generated_eligibility_key", b"x" * 32),
            self.assertRaises(LaunchSelectionError) as catalog_error,
        ):
            service.exchange_catalog_offering(**catalog_arguments)
        self.assertEqual(catalog_error.exception.code, "launch_selection_unavailable")
        accepted_before_drain = admission.snapshot()["global_accepted"]

        self.assertEqual(service.lifecycle_state, "ready")
        self.assertTrue(service.begin_drain())
        self.assertFalse(service.begin_drain())
        self.assertEqual(service.lifecycle_state, "draining")

        replayed_bootstrap = _bootstrap(service, context_id, "bootstrap")
        self.assertTrue(replayed_bootstrap["replayed"])
        with self.assertRaises(LaunchSelectionError) as conflict:
            _bootstrap(service, _context_id("b"), "bootstrap")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        with self.assertRaises(LaunchSelectionError) as closed:
            _bootstrap(service, _context_id("b"), "fresh-bootstrap")
        self.assertEqual(closed.exception.code, "service_closed")

        replayed_exchange = _exchange(service, actor, "exchange")
        self.assertTrue(replayed_exchange["replayed"])
        with self.assertRaises(LaunchSelectionError) as conflict:
            service.exchange(
                actor_authority=actor,
                expected_epoch=SHARED_EPOCH,
                launch_selection_id=SELECTION_ID,
                notice_version_presented="notice:changed-v1",
                idempotency_key="exchange",
            )
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        with self.assertRaises(LaunchSelectionError) as closed:
            _exchange(service, actor, "fresh-exchange")
        self.assertEqual(closed.exception.code, "service_closed")

        with (
            patch.object(service, "_generated_world_access", True),
            patch.object(service, "_generated_eligibility_key", b"x" * 32),
        ):
            with self.assertRaises(LaunchSelectionError) as replayed_error:
                service.exchange_catalog_offering(**catalog_arguments)
            self.assertEqual(
                replayed_error.exception.code,
                "launch_selection_unavailable",
            )
            with self.assertRaises(LaunchSelectionError) as conflict:
                service.exchange_catalog_offering(
                    **dict(
                        catalog_arguments,
                        offering_revision=(
                            "offering-revision:changed-0000000000000000"
                        ),
                    )
                )
            self.assertEqual(conflict.exception.code, "idempotency_conflict")
            with self.assertRaises(LaunchSelectionError) as closed:
                service.exchange_catalog_offering(
                    **dict(catalog_arguments, idempotency_key="fresh-catalog")
                )
            self.assertEqual(closed.exception.code, "service_closed")

        self.assertEqual(
            admission.snapshot()["global_accepted"],
            accepted_before_drain,
        )
        self.assertEqual(
            service.options(
                actor_authority=actor,
                expected_epoch=SHARED_EPOCH,
            )["kind"],
            "attendee_launch_options",
        )

        revoked = service.revoke_context(
            bff_authority=BFF_AUTHORITY,
            actor_authority=actor,
            expected_epoch=SHARED_EPOCH,
            gateway_context_id=context_id,
            idempotency_key="revoke-during-drain",
        )
        replayed_revoke = service.revoke_context(
            bff_authority=BFF_AUTHORITY,
            actor_authority=actor,
            expected_epoch=SHARED_EPOCH,
            gateway_context_id=context_id,
            idempotency_key="revoke-during-drain",
        )
        self.assertEqual(revoked["outcome"], "revoked")
        self.assertTrue(replayed_revoke["replayed"])
        with self.assertRaises(ProductLifecycleError) as denied:
            service.current(
                actor_authority=actor,
                expected_epoch=SHARED_EPOCH,
            )
        self.assertEqual(denied.exception.code, "authority_denied")

        service.shutdown()
        self.assertEqual(service.lifecycle_state, "closed")
        self.assertFalse(service.begin_drain())

    def test_drain_preserves_product_replays_and_blocks_fresh_work(self) -> None:
        for operation in ("create", "submit_command", "reset", "switch"):
            with self.subTest(operation=operation):
                (
                    _clock,
                    _tasks,
                    _executor,
                    admission,
                    service,
                    _registry,
                    _core,
                    _actors,
                ) = self._composition(per_context=100)
                actor = _bootstrap(service, _context_id(operation[0]), "bootstrap")[
                    "actor_context"
                ]
                first_grant = _exchange(service, actor, "exchange-one")["launch_grant"]
                created = service.create(
                    actor_authority=actor,
                    launch_grant=first_grant,
                    expected_epoch=SHARED_EPOCH,
                    idempotency_key="initial-create",
                )
                snapshot = created["snapshot"]
                if operation == "create":
                    changed_grant = _exchange(service, actor, "exchange-changed")[
                        "launch_grant"
                    ]
                    arguments = {
                        "actor_authority": actor,
                        "launch_grant": first_grant,
                        "expected_epoch": SHARED_EPOCH,
                        "idempotency_key": "initial-create",
                    }
                    changed = dict(arguments, launch_grant=changed_grant)
                elif operation == "submit_command":
                    arguments = {
                        "actor_authority": actor,
                        "session_authority": created["session_authority"],
                        "reference": snapshot["reference"],
                        "expected_epoch": SHARED_EPOCH,
                        "idempotency_key": "submit",
                        "expected_slot_generation": 0,
                        "command": "id",
                    }
                    service.submit_command(**arguments)
                    changed = dict(arguments, command="whoami")
                elif operation == "reset":
                    arguments = {
                        "actor_authority": actor,
                        "session_authority": created["session_authority"],
                        "reference": snapshot["reference"],
                        "expected_epoch": SHARED_EPOCH,
                        "idempotency_key": "reset",
                        "expected_control_revision": snapshot["coordinates"][
                            "control_revision"
                        ],
                    }
                    service.reset(**arguments)
                    changed = dict(
                        arguments,
                        expected_control_revision=(
                            arguments["expected_control_revision"] + 1
                        ),
                    )
                else:
                    switch_grant = _exchange(service, actor, "exchange-switch")[
                        "launch_grant"
                    ]
                    changed_grant = _exchange(service, actor, "exchange-changed")[
                        "launch_grant"
                    ]
                    arguments = {
                        "actor_authority": actor,
                        "session_authority": created["session_authority"],
                        "reference": snapshot["reference"],
                        "expected_epoch": SHARED_EPOCH,
                        "idempotency_key": "switch",
                        "expected_control_revision": snapshot["coordinates"][
                            "control_revision"
                        ],
                        "launch_grant": switch_grant,
                    }
                    service.switch(**arguments)
                    changed = dict(arguments, launch_grant=changed_grant)

                accepted_before_drain = admission.snapshot()["global_accepted"]
                self.assertTrue(service.begin_drain())
                replayed = getattr(service, operation)(**arguments)
                self.assertTrue(replayed["replayed"])
                with self.assertRaises(ProductLifecycleError) as conflict:
                    getattr(service, operation)(**changed)
                self.assertEqual(conflict.exception.code, "idempotency_conflict")
                with self.assertRaises(ProductLifecycleError) as closed:
                    getattr(service, operation)(
                        **dict(arguments, idempotency_key="fresh-during-drain")
                    )
                self.assertEqual(closed.exception.code, "service_closed")
                self.assertEqual(
                    admission.snapshot()["global_accepted"],
                    accepted_before_drain,
                )

    def test_drain_keeps_reads_cancellation_close_and_restart_fences(self) -> None:
        (
            _clock,
            tasks,
            executor,
            admission,
            service,
            _registry,
            _core,
            _actors,
        ) = self._composition(per_context=100)
        context_id = _context_id("a")
        actor = _bootstrap(service, context_id, "bootstrap")["actor_context"]
        grant = _exchange(service, actor, "exchange")["launch_grant"]
        created = service.create(
            actor_authority=actor,
            launch_grant=grant,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create",
        )
        submitted = service.submit_command(
            actor_authority=actor,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="submit",
            expected_slot_generation=0,
            command="id",
        )
        accepted_before_drain = admission.snapshot()["global_accepted"]
        service.begin_drain()

        session_arguments = {
            "actor_authority": actor,
            "session_authority": created["session_authority"],
            "reference": created["snapshot"]["reference"],
            "expected_epoch": SHARED_EPOCH,
        }
        self.assertEqual(
            service.current(
                actor_authority=actor,
                expected_epoch=SHARED_EPOCH,
            )["kind"],
            "current_session",
        )
        self.assertEqual(
            service.snapshot(**session_arguments)["kind"], "session_snapshot"
        )
        self.assertEqual(
            service.command_result(
                **session_arguments,
                command_id=submitted["command_id"],
            )["kind"],
            "command_result",
        )
        self.assertEqual(service.events(**session_arguments)["kind"], "event_page")
        self.assertEqual(
            service.transcript(**session_arguments)["kind"],
            "transcript_page",
        )
        self.assertEqual(service.reconcile_due(), 0)
        self.assertEqual(service.expire_due(), 0)

        with self.assertRaises(ProductLifecycleError) as stale_read:
            service.current(
                actor_authority=actor,
                expected_epoch="service-epoch:after-restart",
            )
        self.assertEqual(stale_read.exception.code, "session_lost")
        with self.assertRaises(ProductLifecycleError) as stale_mutation:
            service.submit_command(
                **dict(
                    session_arguments,
                    expected_epoch="service-epoch:after-restart",
                    idempotency_key="stale-submit",
                    expected_slot_generation=0,
                    command="pwd",
                )
            )
        self.assertEqual(stale_mutation.exception.code, "outcome_unknown")

        cancelled = service.cancel_command(
            **session_arguments,
            idempotency_key="cancel-during-drain",
            command_id=submitted["command_id"],
            slot_generation=submitted["slot_generation"],
        )
        replayed_cancel = service.cancel_command(
            **session_arguments,
            idempotency_key="cancel-during-drain",
            command_id=submitted["command_id"],
            slot_generation=submitted["slot_generation"],
        )
        self.assertFalse(cancelled["replayed"])
        self.assertTrue(replayed_cancel["replayed"])
        tasks.tasks[0]()
        self.assertEqual(executor.calls, 0)

        current = service.current(
            actor_authority=actor,
            expected_epoch=SHARED_EPOCH,
        )
        closed = service.close(
            **session_arguments,
            idempotency_key="close-during-drain",
            expected_control_revision=current["snapshot"]["coordinates"][
                "control_revision"
            ],
        )
        replayed_close = service.close(
            **session_arguments,
            idempotency_key="close-during-drain",
            expected_control_revision=current["snapshot"]["coordinates"][
                "control_revision"
            ],
        )
        self.assertEqual(closed["kind"], "session_closed")
        self.assertTrue(replayed_close["replayed"])
        self.assertEqual(
            admission.snapshot()["global_accepted"],
            accepted_before_drain,
        )

    def test_selection_replay_precedes_rate_and_retryable_denial_is_unclaimed(
        self,
    ) -> None:
        (
            clock,
            _tasks,
            _executor,
            admission,
            service,
            registry,
            core,
            _actors,
        ) = self._composition(per_context=1)
        actor = _bootstrap(service, _context_id("a"), "bootstrap")["actor_context"]

        with self.assertRaises(LaunchSelectionError) as captured:
            _exchange(service, actor, "exchange")
        self.assertEqual(captured.exception.code, "logical_rate_limited")
        self.assertEqual(captured.exception.status, 429)
        self.assertEqual(captured.exception.retry, "after_delay")
        self.assertEqual(captured.exception.retry_after_ms, 1_000)
        self.assertEqual(service._exchange_idempotency, {})
        self.assertEqual(registry._idempotency, {})
        self.assertEqual(core._launch_grants, {})

        clock.wall += 1_000
        clock.monotonic += 1_000
        granted = _exchange(service, actor, "exchange")
        replayed = _exchange(service, actor, "exchange")
        self.assertFalse(granted["replayed"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(admission.snapshot()["global_accepted"], 1)
        with self.assertRaises(LaunchSelectionError) as captured:
            service.exchange(
                actor_authority=actor,
                expected_epoch=SHARED_EPOCH,
                launch_selection_id=SELECTION_ID,
                notice_version_presented="notice:changed",
                idempotency_key="exchange",
            )
        self.assertEqual(captured.exception.code, "idempotency_conflict")
        self.assertEqual(admission.snapshot()["global_accepted"], 1)

    def test_product_replay_and_conflict_precede_rate_charging(self) -> None:
        (
            clock,
            tasks,
            executor,
            admission,
            service,
            _registry,
            core,
            _actors,
        ) = self._composition(per_context=3)
        actor = _bootstrap(service, _context_id("a"), "bootstrap")["actor_context"]
        grant = _exchange(service, actor, "exchange")["launch_grant"]
        created = service.create(
            actor_authority=actor,
            launch_grant=grant,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create",
        )
        submit = {
            "actor_authority": actor,
            "session_authority": created["session_authority"],
            "reference": created["snapshot"]["reference"],
            "expected_epoch": SHARED_EPOCH,
            "idempotency_key": "submit",
            "expected_slot_generation": 0,
            "command": "id",
        }
        before_claims = len(core._idempotency)
        with self.assertRaises(ProductLifecycleError) as captured:
            service.submit_command(**submit)
        self.assertEqual(captured.exception.code, "logical_rate_limited")
        self.assertEqual(captured.exception.retry, "after_delay")
        self.assertEqual(captured.exception.retry_after_ms, 1_000)
        self.assertEqual(len(core._idempotency), before_claims)
        self.assertEqual(tasks.tasks, [])
        self.assertEqual(executor.calls, 0)

        clock.wall += 1_000
        clock.monotonic += 1_000
        accepted = service.submit_command(**submit)
        replayed = service.submit_command(**submit)
        self.assertFalse(accepted["replayed"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(len(tasks.tasks), 1)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(admission.snapshot()["global_accepted"], 1)
        with self.assertRaises(ProductLifecycleError) as captured:
            service.submit_command(**dict(submit, command="whoami"))
        self.assertEqual(captured.exception.code, "idempotency_conflict")
        self.assertEqual(admission.snapshot()["global_accepted"], 1)

    def test_concurrent_exact_product_retry_is_charged_once(self) -> None:
        (
            _clock,
            tasks,
            _executor,
            admission,
            service,
            _registry,
            _core,
            _actors,
        ) = self._composition(per_context=4)
        actor = _bootstrap(service, _context_id("a"), "bootstrap")["actor_context"]
        grant = _exchange(service, actor, "exchange")["launch_grant"]
        created = service.create(
            actor_authority=actor,
            launch_grant=grant,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create",
        )
        submit = {
            "actor_authority": actor,
            "session_authority": created["session_authority"],
            "reference": created["snapshot"]["reference"],
            "expected_epoch": SHARED_EPOCH,
            "idempotency_key": "concurrent-submit",
            "expected_slot_generation": 0,
            "command": "id",
        }
        original_admit = admission.admit_fresh
        first_admission_entered = threading.Event()
        release_first_admission = threading.Event()
        unexpected_second_admission = threading.Event()
        admission_calls = 0
        calls_lock = threading.Lock()

        def blocking_admit(*, context_sha256: str, deadline_ms: int) -> None:
            nonlocal admission_calls
            original_admit(
                context_sha256=context_sha256,
                deadline_ms=deadline_ms,
            )
            with calls_lock:
                admission_calls += 1
                call_number = admission_calls
            if call_number == 1:
                first_admission_entered.set()
                self.assertTrue(release_first_admission.wait(1))
            else:
                unexpected_second_admission.set()

        admission.admit_fresh = blocking_admit  # type: ignore[method-assign]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.submit_command, **submit)
            self.assertTrue(first_admission_entered.wait(1))
            second = pool.submit(service.submit_command, **submit)
            self.assertFalse(unexpected_second_admission.wait(0.1))
            release_first_admission.set()
            results = (first.result(timeout=1), second.result(timeout=1))

        self.assertEqual(admission_calls, 1)
        self.assertEqual(sum(not result["replayed"] for result in results), 1)
        self.assertEqual(len(tasks.tasks), 1)
        self.assertEqual(admission.snapshot()["global_accepted"], 4)
        self.assertEqual(service._product_operation_gates, {})

    def test_cancel_close_recovery_and_revoke_bypass_exhausted_rate(self) -> None:
        (
            _clock,
            tasks,
            executor,
            admission,
            service,
            _registry,
            _core,
            _actors,
        ) = self._composition(per_context=4)
        context_id = _context_id("a")
        actor = _bootstrap(service, context_id, "bootstrap")["actor_context"]
        grant = _exchange(service, actor, "exchange")["launch_grant"]
        created = service.create(
            actor_authority=actor,
            launch_grant=grant,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create",
        )
        submitted = service.submit_command(
            actor_authority=actor,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="submit",
            expected_slot_generation=0,
            command="id",
        )
        self.assertEqual(admission.snapshot()["global_accepted"], 4)
        cancel = service.cancel_command(
            actor_authority=actor,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="cancel",
            command_id=submitted["command_id"],
            slot_generation=submitted["slot_generation"],
        )
        replayed_cancel = service.cancel_command(
            actor_authority=actor,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="cancel",
            command_id=submitted["command_id"],
            slot_generation=submitted["slot_generation"],
        )
        self.assertFalse(cancel["replayed"])
        self.assertTrue(replayed_cancel["replayed"])
        tasks.tasks[0]()
        self.assertEqual(executor.calls, 0)
        current = service.current(
            actor_authority=actor,
            expected_epoch=SHARED_EPOCH,
        )
        closed = service.close(
            actor_authority=actor,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="close",
            expected_control_revision=current["snapshot"]["coordinates"][
                "control_revision"
            ],
        )
        self.assertEqual(closed["kind"], "session_closed")
        self.assertEqual(
            service.current(
                actor_authority=actor,
                expected_epoch=SHARED_EPOCH,
            )["kind"],
            "current_session_terminal",
        )
        revoked = service.revoke_context(
            bff_authority=BFF_AUTHORITY,
            actor_authority=actor,
            expected_epoch=SHARED_EPOCH,
            gateway_context_id=context_id,
            idempotency_key="revoke",
        )
        self.assertEqual(revoked["outcome"], "revoked")
        self.assertEqual(admission.snapshot()["global_accepted"], 4)

    def test_revoke_correlates_context_and_fences_queued_work_with_recovery(
        self,
    ) -> None:
        (
            _clock,
            tasks,
            executor,
            _admission,
            service,
            registry,
            core,
            _actors,
        ) = self._composition(per_context=20)
        context_a = _context_id("a")
        context_b = _context_id("b")
        actor_a = _bootstrap(service, context_a, "bootstrap-a")["actor_context"]
        actor_b = _bootstrap(service, context_b, "bootstrap-b")["actor_context"]
        grant = _exchange(service, actor_a, "exchange-a")["launch_grant"]
        created = service.create(
            actor_authority=actor_a,
            launch_grant=grant,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create-a",
        )
        service.submit_command(
            actor_authority=actor_a,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="submit-a",
            expected_slot_generation=0,
            command="id",
        )

        with self.assertRaises(LaunchSelectionError) as captured:
            service.revoke_context(
                bff_authority=BFF_AUTHORITY,
                actor_authority=actor_b,
                expected_epoch=SHARED_EPOCH,
                gateway_context_id=context_a,
                idempotency_key="cross-revoke",
            )
        self.assertEqual(captured.exception.code, "authority_denied")
        self.assertEqual(
            service.options(
                actor_authority=actor_b,
                expected_epoch=SHARED_EPOCH,
            )["kind"],
            "attendee_launch_options",
        )

        revoked = service.revoke_context(
            bff_authority=BFF_AUTHORITY,
            actor_authority=actor_a,
            expected_epoch=SHARED_EPOCH,
            gateway_context_id=context_a,
            idempotency_key="revoke-a",
        )
        replayed = service.revoke_context(
            bff_authority=BFF_AUTHORITY,
            actor_authority=actor_a,
            expected_epoch=SHARED_EPOCH,
            gateway_context_id=context_a,
            idempotency_key="revoke-a",
        )
        self.assertEqual(
            revoked,
            {
                "kind": "attendee_context_revoked",
                "service_epoch": SHARED_EPOCH,
                "replayed": False,
                "outcome": "revoked",
            },
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["outcome"], "revoked")
        self.assertNotIn(context_a, repr(service._revoke_idempotency))
        self.assertNotIn(actor_a, repr(service._revoke_idempotency))

        with self.assertRaises(LaunchSelectionError) as captured:
            service.revoke_context(
                bff_authority=BFF_AUTHORITY,
                actor_authority=actor_a,
                expected_epoch=SHARED_EPOCH,
                gateway_context_id=context_a,
                idempotency_key="fresh-after-revoke",
            )
        self.assertEqual(captured.exception.code, "authority_denied")
        with self.assertRaises(ProductLifecycleError) as captured:
            service.current(
                actor_authority=actor_a,
                expected_epoch=SHARED_EPOCH,
            )
        self.assertEqual(captured.exception.code, "authority_denied")
        self.assertEqual(core._sessions_by_reference, {})
        self.assertEqual(core.admission.snapshot().active_sessions, 0)
        self.assertEqual(core.admission.snapshot().unsettled_jobs, 0)
        self.assertEqual(registry._grants, {})
        self.assertEqual(core._launch_grants, {})
        tasks.tasks[0]()
        self.assertEqual(executor.calls, 0)

        with self.assertRaises(LaunchSelectionError) as captured:
            service.revoke_context(
                bff_authority=BFF_AUTHORITY,
                actor_authority=actor_b,
                expected_epoch=SHARED_EPOCH,
                gateway_context_id=context_b,
                idempotency_key="revoke-a",
            )
        self.assertEqual(captured.exception.code, "idempotency_conflict")

    def test_mismatch_rejections_cannot_exhaust_legitimate_revocation(self) -> None:
        clock = ManualClock()
        tasks = ManualTasks()
        executor = CountingExecutor()
        service, _registry, _core, _actors = _service(
            clock=clock,
            tasks=tasks,
            executor=executor,
            max_contexts=2,
            revoke_idempotency_capacity=2,
        )
        context_a = _context_id("a")
        context_b = _context_id("b")
        actor_a = _bootstrap(service, context_a, "bootstrap-a")["actor_context"]
        actor_b = _bootstrap(service, context_b, "bootstrap-b")["actor_context"]

        for index in range(8):
            with self.assertRaises(LaunchSelectionError) as captured:
                service.revoke_context(
                    bff_authority=BFF_AUTHORITY,
                    actor_authority=actor_b,
                    expected_epoch=SHARED_EPOCH,
                    gateway_context_id=context_a,
                    idempotency_key=f"mismatch-{index}",
                )
            self.assertEqual(captured.exception.code, "authority_denied")
        self.assertEqual(service._revoke_idempotency, {})

        revoked = service.revoke_context(
            bff_authority=BFF_AUTHORITY,
            actor_authority=actor_a,
            expected_epoch=SHARED_EPOCH,
            gateway_context_id=context_a,
            idempotency_key="legitimate-revoke",
        )
        replayed = service.revoke_context(
            bff_authority=BFF_AUTHORITY,
            actor_authority=actor_a,
            expected_epoch=SHARED_EPOCH,
            gateway_context_id=context_a,
            idempotency_key="legitimate-revoke",
        )
        self.assertEqual(revoked["outcome"], "revoked")
        self.assertFalse(revoked["replayed"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(len(service._revoke_idempotency), 1)

    def test_revoke_uses_first_clock_sample_and_cannot_split_core_fencing(
        self,
    ) -> None:
        (
            clock,
            tasks,
            executor,
            _admission,
            service,
            registry,
            core,
            _actors,
        ) = self._composition(per_context=20)
        context_id = _context_id("a")
        actor = _bootstrap(service, context_id, "bootstrap-clock")["actor_context"]
        grant = _exchange(service, actor, "exchange-clock")["launch_grant"]
        created = service.create(
            actor_authority=actor,
            launch_grant=grant,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create-clock",
        )
        service.submit_command(
            actor_authority=actor,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="submit-clock",
            expected_slot_generation=0,
            command="id",
        )

        # The facade/registry clock sample is valid. A former second sample in
        # the core would observe this injected rollback and raise after the
        # lease and registry grant had already been fenced.
        with patch.object(
            core,
            "_monotonic_clock_ms",
            return_value=clock.monotonic - 1,
        ):
            revoked = service.revoke_context(
                bff_authority=BFF_AUTHORITY,
                actor_authority=actor,
                expected_epoch=SHARED_EPOCH,
                gateway_context_id=context_id,
                idempotency_key="revoke-clock",
            )

        self.assertEqual(revoked["outcome"], "revoked")
        self.assertEqual(core._sessions_by_reference, {})
        self.assertEqual(registry._grants, {})
        self.assertEqual(core._launch_grants, {})
        self.assertEqual(core.admission.snapshot().unsettled_jobs, 0)
        tasks.tasks[0]()
        self.assertEqual(executor.calls, 0)
        replayed = service.revoke_context(
            bff_authority=BFF_AUTHORITY,
            actor_authority=actor,
            expected_epoch=SHARED_EPOCH,
            gateway_context_id=context_id,
            idempotency_key="revoke-clock",
        )
        self.assertTrue(replayed["replayed"])


if __name__ == "__main__":
    unittest.main()
