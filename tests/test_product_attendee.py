from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

from dreamland.product.admission import (
    ProductAdmissionLedger,
    ProductAdmissionLimits,
)
from dreamland.product.attendee import (
    AttendeeBffAuthoritySeed,
    AttendeeContextPolicy,
    AttendeeSessionCoreService,
    PublicAttendeeSessionService,
)
from dreamland.product.attendee_security import AttendeeRequestAdmissionLedger
from dreamland.product.core import ProductCorePolicy
from dreamland.product.execution import ProductExecutionResult
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

SHARED_EPOCH = "epoch:publicattendeetest0001"
BFF_AUTHORITY = "bff-authority:private-gateway-test-00000001"
SELECTION_ID = "launch-selection:attendee-authored-opaque-000001"
NOTICE_VERSION = "notice:attendee-v1"


class ManualClock:
    def __init__(self, value: int = 1_000) -> None:
        self.wall = value
        self.monotonic = value

    def __call__(self) -> int:
        return self.wall

    def monotonic_ms(self) -> int:
        return self.monotonic


class RegistryTokens:
    def __init__(self, epoch: str = SHARED_EPOCH) -> None:
        self.epoch = epoch
        self.index = 0

    def __call__(self, purpose: str) -> str:
        if purpose == "epoch":
            return self.epoch
        self.index += 1
        return f"launch-grant:attendee-candidate-{self.index:08d}"


class CoreTokens:
    def __init__(self, epoch: str = SHARED_EPOCH) -> None:
        self.epoch = epoch
        self.index = 0

    def __call__(self, purpose: str) -> str:
        if purpose == "service_epoch":
            return self.epoch
        self.index += 1
        return f"{purpose}:attendee-core-test-{self.index:08d}"


class ManualTasks:
    def __init__(self) -> None:
        self.tasks: list[Callable[[], None]] = []

    def __call__(self, task: Callable[[], None]) -> None:
        self.tasks.append(task)


class CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _request, _emit):
        self.calls += 1
        return ProductExecutionResult.completed_exited(
            exit_status=0,
            usage_tokens=1,
            usage_spend_units=1,
        )


class BlockingExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.release = threading.Event()

    def execute(self, request, _emit):
        self.calls += 1
        self.started.set()
        while not request.cancelled() and not self.release.wait(0.01):
            pass
        if request.cancelled():
            self.cancelled.set()
            return ProductExecutionResult.interrupted("cancelled")
        return ProductExecutionResult.completed_exited(
            exit_status=0,
            usage_tokens=1,
            usage_spend_units=1,
        )


class PublicAttendeeSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.tasks = ManualTasks()
        self.executor = CountingExecutor()
        self.service, self.registry, self.core, self.actors = _service(
            clock=self.clock,
            tasks=self.tasks,
            executor=self.executor,
        )

    def test_import_is_inert(self) -> None:
        code = """
import json
import threading
before = [thread.name for thread in threading.enumerate()]
from dreamland.product.attendee import PublicAttendeeSessionService
after = [thread.name for thread in threading.enumerate()]
print(json.dumps({"before": before, "after": after, "name": PublicAttendeeSessionService.__name__}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["before"], payload["after"])
        self.assertEqual(payload["name"], "PublicAttendeeSessionService")

    def test_bootstrap_is_private_idempotent_and_context_bound(self) -> None:
        context_a = _context_id("a")
        context_b = _context_id("b")
        created = self._bootstrap(context_a, "bootstrap-key")
        replayed = self._bootstrap(context_a, "bootstrap-key")

        self.assertFalse(created["replayed"])
        self.assertFalse(created["context_reused"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(created["actor_context"], replayed["actor_context"])
        self.assertNotIn(context_a, json.dumps(created, sort_keys=True))
        self.assertNotIn(context_a, repr(self.service._contexts_by_digest))
        self.assertIn(
            hashlib.sha256(context_a.encode()).hexdigest(),
            self.service._contexts_by_digest,
        )

        with self.assertRaises(LaunchSelectionError) as captured:
            self._bootstrap(context_b, "bootstrap-key")
        self.assertEqual(captured.exception.code, "idempotency_conflict")
        self.assertEqual(self.service._next_slot, 1)

        resumed = self._bootstrap(context_a, "another-bootstrap-key")
        self.assertTrue(resumed["context_reused"])
        self.assertEqual(resumed["actor_context"], created["actor_context"])
        self.assertEqual(self.service._next_slot, 1)

    def test_concurrent_same_context_bootstrap_allocates_one_actor(self) -> None:
        workers = 32
        barrier = threading.Barrier(workers)

        def bootstrap() -> dict[str, object]:
            barrier.wait()
            return self._bootstrap(_context_id("a"), "concurrent-bootstrap")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = tuple(pool.map(lambda _index: bootstrap(), range(workers)))

        self.assertEqual(
            {result["actor_context"] for result in results},
            {results[0]["actor_context"]},
        )
        self.assertEqual(sum(not result["replayed"] for result in results), 1)
        self.assertEqual(self.service._next_slot, 1)
        self.assertEqual(len(self.service._contexts_by_digest), 1)

    def test_actor_ownership_requires_a_leased_slot_and_survives_fencing(self) -> None:
        self.assertFalse(self.service.owns_actor_authority(self.actors[0].authority))
        self.assertFalse(self.service.owns_actor_authority("actor:unknown-attendee"))
        actor = self._bootstrap(_context_id("a"), "bootstrap")["actor_context"]
        self.assertTrue(self.service.owns_actor_authority(actor))
        self.assertFalse(self.service.owns_actor_authority(self.actors[1].authority))

        self.clock.wall += 5_001
        self.clock.monotonic += 5_001
        self.service.expire_due()
        self.assertTrue(self.service.owns_actor_authority(actor))
        self.service.shutdown()
        self.assertTrue(self.service.owns_actor_authority(actor))
        self.assertEqual(self.registry._grants, {})
        self.assertEqual(self.core._sessions_by_reference, {})
        self.assertEqual(self.executor.calls, 0)

    def test_bootstrap_rejects_weak_context_and_never_reuses_slots(self) -> None:
        with self.assertRaises(LaunchSelectionError) as captured:
            self._bootstrap("gateway-context:short", "weak")
        self.assertEqual(captured.exception.code, "invalid_request")

        first = self._bootstrap(_context_id("a"), "first")
        self._bootstrap(_context_id("b"), "second")
        for _attempt in range(2):
            with self.assertRaises(LaunchSelectionError) as captured:
                self._bootstrap(_context_id("c"), "third")
            self.assertEqual(
                captured.exception.code,
                "attendee_context_capacity_exhausted",
            )
        self.assertEqual(self.service._next_slot, 2)
        with self.assertRaises(LaunchSelectionError) as captured:
            self._bootstrap(_context_id("a"), "third")
        self.assertEqual(captured.exception.code, "idempotency_conflict")

        # A distinct key can still resume the context that was safely bound.
        resumed = self._bootstrap(_context_id("a"), "resume-first")
        self.assertTrue(resumed["context_reused"])

        self.clock.wall += 5_001
        self.clock.monotonic += 5_001
        with self.assertRaises(LaunchSelectionError) as captured:
            self.service.options(
                actor_authority=first["actor_context"],
                expected_epoch=SHARED_EPOCH,
            )
        self.assertEqual(captured.exception.code, "authority_denied")

        with self.assertRaises(LaunchSelectionError) as captured:
            self._bootstrap(_context_id("c"), "fourth")
        self.assertEqual(captured.exception.code, "attendee_context_capacity_exhausted")
        self.assertEqual(self.service._next_slot, 2)

    def test_bootstrap_authority_and_epoch_precede_untrusted_body_validation(
        self,
    ) -> None:
        with self.assertRaises(LaunchSelectionError) as captured:
            self.service.bootstrap_context(
                bff_authority="bff-authority:unknown-private-gateway-0001",
                expected_epoch=SHARED_EPOCH,
                gateway_context_id="malformed",
                idempotency_key="bad key with spaces",
            )
        self.assertEqual(captured.exception.code, "authority_denied")

        with self.assertRaises(LaunchSelectionError) as captured:
            self.service.bootstrap_context(
                bff_authority=BFF_AUTHORITY,
                expected_epoch="epoch:stale-attendee-test",
                gateway_context_id="malformed",
                idempotency_key="bad key with spaces",
            )
        self.assertEqual(captured.exception.code, "outcome_unknown")
        self.assertEqual(self.service._next_slot, 0)
        self.assertEqual(self.service._bootstrap_idempotency, {})

    def test_bootstrap_replay_ledger_capacity_is_transient_and_unclaimed(
        self,
    ) -> None:
        service, _registry, _core, _actors = _service(
            clock=ManualClock(),
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            bootstrap_idempotency_capacity=3,
        )
        first = _bootstrap(service, _context_id("a"), "bootstrap-1")
        _bootstrap(service, _context_id("a"), "bootstrap-2")
        _bootstrap(service, _context_id("a"), "bootstrap-3")
        for context_id in (_context_id("a"), _context_id("b")):
            with self.subTest(context_id=context_id):
                with self.assertRaises(LaunchSelectionError) as captured:
                    _bootstrap(service, context_id, "bootstrap-4")
                self.assertEqual(
                    captured.exception.code,
                    "attendee_context_idempotency_capacity_exhausted",
                )
                self.assertEqual(captured.exception.retry, "after_delay")
                self.assertEqual(captured.exception.retry_after_ms, 1_000)
        replay = _bootstrap(service, _context_id("a"), "bootstrap-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["actor_context"], first["actor_context"])
        self.assertEqual(len(service._bootstrap_idempotency), 3)
        self.assertEqual(service._next_slot, 1)

    def test_missing_and_stale_epoch_are_distinct(self) -> None:
        for value in (None, ""):
            with self.subTest(value=value):
                with self.assertRaises(LaunchSelectionError) as captured:
                    self.service.bootstrap_context(
                        bff_authority=BFF_AUTHORITY,
                        expected_epoch=value,
                        gateway_context_id=_context_id("a"),
                        idempotency_key="epoch-required",
                    )
                self.assertEqual(captured.exception.code, "expected_epoch_required")
                self.assertEqual(captured.exception.status, 400)
        with self.assertRaises(LaunchSelectionError) as captured:
            self.service.bootstrap_context(
                bff_authority=BFF_AUTHORITY,
                expected_epoch="epoch:stale-attendee-test",
                gateway_context_id=_context_id("a"),
                idempotency_key="epoch-stale",
            )
        self.assertEqual(captured.exception.code, "outcome_unknown")

    def test_bootstrap_authority_enforces_wall_and_monotonic_expiry(self) -> None:
        self.clock.wall = 100_000
        with self.assertRaises(LaunchSelectionError) as captured:
            self._bootstrap(_context_id("a"), "wall-expired")
        self.assertEqual(captured.exception.code, "authority_denied")

        clock = ManualClock()
        service, _registry, _core, _actors = _service(
            clock=clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            bff_expires_at_ms=1_500,
        )
        clock.monotonic = 1_500
        with self.assertRaises(LaunchSelectionError) as captured:
            service.bootstrap_context(
                bff_authority=BFF_AUTHORITY,
                expected_epoch=SHARED_EPOCH,
                gateway_context_id=_context_id("m"),
                idempotency_key="monotonic-expired",
            )
        self.assertEqual(captured.exception.code, "authority_denied")

    def test_options_are_authored_path_free_and_require_claimed_context(self) -> None:
        unclaimed = self.actors[0].authority
        with self.assertRaises(LaunchSelectionError) as captured:
            self.service.options(
                actor_authority=unclaimed,
                expected_epoch=SHARED_EPOCH,
            )
        self.assertEqual(captured.exception.code, "authority_denied")

        actor = self._bootstrap(_context_id("a"), "bootstrap")["actor_context"]
        result = self.service.options(
            actor_authority=actor,
            expected_epoch=SHARED_EPOCH,
        )
        self.assertEqual(result["kind"], "attendee_launch_options")
        self.assertEqual(len(result["options"]), 1)
        self.assertEqual(
            result["options"][0]["origin"],
            {"kind": "authored_profile_snapshot"},
        )
        self.assertEqual(result["options"][0]["notice_version"], NOTICE_VERSION)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("/operator/private", serialized)
        self.assertNotIn("private baseline", serialized)
        self.assertNotIn("world_state", serialized)
        self.assertNotIn(actor, serialized)
        self.assertEqual(self.executor.calls, 0)
        self.assertEqual(self.tasks.tasks, [])

    def test_notice_rejection_replays_and_changed_body_conflicts(self) -> None:
        actor = self._bootstrap(_context_id("a"), "bootstrap")["actor_context"]
        for _attempt in range(2):
            with self.assertRaises(LaunchSelectionError) as captured:
                self.service.exchange(
                    actor_authority=actor,
                    expected_epoch=SHARED_EPOCH,
                    launch_selection_id=SELECTION_ID,
                    notice_version_presented="notice:wrong-v1",
                    idempotency_key="notice-key",
                )
            self.assertEqual(captured.exception.code, "notice_version_mismatch")
        with self.assertRaises(LaunchSelectionError) as captured:
            self.service.exchange(
                actor_authority=actor,
                expected_epoch=SHARED_EPOCH,
                launch_selection_id=SELECTION_ID,
                notice_version_presented=NOTICE_VERSION,
                idempotency_key="notice-key",
            )
        self.assertEqual(captured.exception.code, "idempotency_conflict")
        self.assertEqual(self.registry._grants, {})
        self.assertEqual(self.core._launch_grants, {})

    def test_inner_exchange_claim_is_bound_to_notice_and_outer_retention(self) -> None:
        actor = self._bootstrap(_context_id("a"), "bootstrap")["actor_context"]
        granted = _exchange(self.service, actor, "exchange-boundary")
        self.assertFalse(granted["replayed"])
        self.assertNotIn("launch_binding_sha256", granted)
        self.assertEqual(len(self.registry._grants), 1)
        self.assertEqual(len(self.core._launch_grants), 1)

        outer = next(iter(self.service._exchange_idempotency.values()))
        outer.retain_until_deadline_ms = self.clock.monotonic
        with self.assertRaises(LaunchSelectionError) as captured:
            self.service.exchange(
                actor_authority=actor,
                expected_epoch=SHARED_EPOCH,
                launch_selection_id=SELECTION_ID,
                notice_version_presented="notice:wrong-v1",
                idempotency_key="exchange-boundary",
            )
        self.assertEqual(captured.exception.code, "notice_version_mismatch")
        self.assertEqual(len(self.registry._grants), 1)
        self.assertEqual(len(self.core._launch_grants), 1)

        # Expiring only the outer record and retrying the exact semantic body
        # reaches the inner replay without allocating or provisioning again.
        outer = next(iter(self.service._exchange_idempotency.values()))
        outer.retain_until_deadline_ms = self.clock.monotonic
        replayed = _exchange(self.service, actor, "exchange-boundary")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(len(self.registry._grants), 1)
        self.assertEqual(len(self.core._launch_grants), 1)

    def test_admission_window_precedes_provisioning(self) -> None:
        clock = ManualClock()
        service, registry, core, _actors = _service(
            clock=clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            admission_opens_at_ms=2_000,
            admission_closes_at_ms=3_000,
        )
        actor = _bootstrap(service, _context_id("a"), "bootstrap")["actor_context"]
        with self.assertRaises(LaunchSelectionError) as captured:
            _exchange(service, actor, "before-open")
        self.assertEqual(captured.exception.code, "service_not_open")
        self.assertEqual(registry._grants, {})
        self.assertEqual(core._launch_grants, {})

        clock.wall = clock.monotonic = 2_000
        granted = _exchange(service, actor, "during-window")
        self.assertFalse(granted["replayed"])
        self.assertEqual(granted["selection"]["notice_version"], NOTICE_VERSION)
        self.assertEqual(len(core._launch_grants), 1)

        clock.wall = clock.monotonic = 3_000
        with self.assertRaises(LaunchSelectionError) as captured:
            _exchange(service, actor, "after-close")
        self.assertEqual(captured.exception.code, "service_closed")
        self.assertEqual(len(core._launch_grants), 0)

    def test_product_admission_failures_preserve_typed_retry_fields(self) -> None:
        clock = ManualClock()
        service, _registry, _core, _actors = _service(
            clock=clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            admission_opens_at_ms=2_000,
            admission_closes_at_ms=3_000,
        )
        actor = _bootstrap(service, _context_id("a"), "bootstrap")["actor_context"]
        arguments = {
            "actor_authority": actor,
            "launch_grant": "launch-grant:unavailable",
            "expected_epoch": SHARED_EPOCH,
            "idempotency_key": "typed-admission",
        }
        with self.assertRaises(ProductLifecycleError) as captured:
            service.create(**arguments)
        self.assertEqual(captured.exception.code, "service_not_open")
        self.assertEqual(captured.exception.status, 503)
        self.assertEqual(captured.exception.retry, "after_delay")
        self.assertEqual(captured.exception.retry_after_ms, 1_000)

        clock.wall = clock.monotonic = 3_000
        with self.assertRaises(ProductLifecycleError) as captured:
            service.create(**arguments)
        self.assertEqual(captured.exception.code, "service_closed")
        self.assertEqual(captured.exception.status, 410)
        self.assertEqual(captured.exception.retry, "none")
        self.assertIsNone(captured.exception.retry_after_ms)

    def test_post_close_bootstrap_is_bounded_and_cannot_authorize_work(self) -> None:
        clock = ManualClock()
        executor = CountingExecutor()
        service, registry, core, _actors = _service(
            clock=clock,
            tasks=ManualTasks(),
            executor=executor,
            admission_closes_at_ms=2_000,
        )
        clock.wall = clock.monotonic = 2_000
        actor = _bootstrap(service, _context_id("a"), "post-close-bootstrap")[
            "actor_context"
        ]
        options = service.options(
            actor_authority=actor,
            expected_epoch=SHARED_EPOCH,
        )
        self.assertEqual(options["options"], [])
        with self.assertRaises(LaunchSelectionError) as captured:
            _exchange(service, actor, "post-close-exchange")
        self.assertEqual(captured.exception.code, "service_closed")
        self.assertEqual(registry._grants, {})
        self.assertEqual(core._sessions_by_reference, {})
        self.assertEqual(executor.calls, 0)

    def test_opt_in_bootstrap_cutoff_is_exact_and_preserves_recovery_replay(
        self,
    ) -> None:
        clock = ManualClock()
        service, _registry, _core, _actors = _service(
            clock=clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            admission_opens_at_ms=1_000,
            admission_closes_at_ms=2_000,
            bootstrap_closes_at_admission=True,
        )
        context_id = _context_id("a")
        first = _bootstrap(service, context_id, "cutoff-bootstrap")
        self.assertFalse(first["replayed"])

        clock.wall = clock.monotonic = 2_000
        replay = _bootstrap(service, context_id, "cutoff-bootstrap")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["actor_context"], first["actor_context"])
        with self.assertRaises(LaunchSelectionError) as conflict:
            _bootstrap(service, _context_id("b"), "cutoff-bootstrap")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        with self.assertRaises(LaunchSelectionError) as closed:
            _bootstrap(service, _context_id("b"), "cutoff-bootstrap-fresh")
        self.assertEqual(closed.exception.code, "service_closed")
        self.assertEqual(service._next_slot, 1)

    def test_claimed_new_work_replays_and_conflicts_after_cutoff(self) -> None:
        for operation in ("create", "submit_command", "reset", "switch"):
            with self.subTest(operation=operation):
                clock = ManualClock()
                tasks = ManualTasks()
                service, _registry, _core, _actors = _service(
                    clock=clock,
                    tasks=tasks,
                    executor=CountingExecutor(),
                    admission_closes_at_ms=2_000,
                )
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
                    second_grant = _exchange(service, actor, "exchange-two")[
                        "launch_grant"
                    ]
                    arguments = {
                        "actor_authority": actor,
                        "launch_grant": first_grant,
                        "expected_epoch": SHARED_EPOCH,
                        "idempotency_key": "initial-create",
                    }
                    changed = dict(arguments, launch_grant=second_grant)
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
                    changed = dict(arguments, command="pwd")
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

                clock.wall = clock.monotonic = 2_000
                replay = getattr(service, operation)(**arguments)
                self.assertTrue(replay["replayed"])
                with self.assertRaises(ProductLifecycleError) as captured:
                    getattr(service, operation)(**changed)
                self.assertEqual(captured.exception.code, "idempotency_conflict")
                with self.assertRaises(ProductLifecycleError) as captured:
                    getattr(service, operation)(
                        **dict(arguments, idempotency_key="new-after-cutoff")
                    )
                self.assertEqual(captured.exception.code, "service_closed")

    def test_admission_cutoff_clamps_grant_and_session_on_either_clock(self) -> None:
        for dimension in ("wall", "monotonic"):
            with self.subTest(dimension=dimension):
                clock = ManualClock()
                service, registry, core, actors = _service(
                    clock=clock,
                    tasks=ManualTasks(),
                    executor=CountingExecutor(),
                    context_ttl_ms=10_000,
                    grant_ttl_ms=10_000,
                    absolute_ttl_ms=10_000,
                    admission_closes_at_ms=2_000,
                )
                actor = _bootstrap(service, _context_id(dimension[0]), "bootstrap")[
                    "actor_context"
                ]
                lease = next(iter(service._contexts_by_digest.values()))
                self.assertEqual(lease.expires_at_ms, 11_000)
                self.assertEqual(lease.expires_deadline_ms, 11_000)
                digest = hashlib.sha256(actor.encode()).hexdigest()
                self.assertEqual(registry._actors[digest].expires_at_ms, 11_000)
                self.assertEqual(core._actors[digest].expires_at_ms, 11_000)

                grant = _exchange(service, actor, "exchange")["launch_grant"]
                created = service.create(
                    actor_authority=actor,
                    launch_grant=grant,
                    expected_epoch=SHARED_EPOCH,
                    idempotency_key="create",
                )
                session = core._sessions_by_id[created["snapshot"]["session_id"]]
                self.assertEqual(session.absolute_expires_at_ms, 2_000)
                self.assertEqual(session.absolute_deadline_ms, 2_000)

                setattr(clock, dimension, 2_000)
                self.assertEqual(service.expire_due(), 1)
                self.assertEqual(core.admission.snapshot().active_sessions, 0)
                current = service.current(
                    actor_authority=actor,
                    expected_epoch=SHARED_EPOCH,
                )
                self.assertEqual(current["kind"], "current_session_terminal")
                with self.assertRaises(LaunchSelectionError) as captured:
                    service.exchange(
                        actor_authority=actor,
                        expected_epoch=SHARED_EPOCH,
                        launch_selection_id=SELECTION_ID,
                        notice_version_presented=NOTICE_VERSION,
                        idempotency_key="after-cutoff",
                    )
                self.assertEqual(captured.exception.code, "service_closed")
                self.assertTrue(service.owns_actor_authority(actors[0].authority))

    def test_shutdown_is_idempotent_and_fences_queued_work(self) -> None:
        actor = self._bootstrap(_context_id("a"), "bootstrap")["actor_context"]
        grant = _exchange(self.service, actor, "exchange")["launch_grant"]
        created = self.service.create(
            actor_authority=actor,
            launch_grant=grant,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create",
        )
        self.service.submit_command(
            actor_authority=actor,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="submit",
            expected_slot_generation=0,
            command="id",
        )
        self.assertEqual(len(self.tasks.tasks), 1)
        self.assertEqual(self.core.admission.snapshot().logical_jobs, 1)

        self.assertEqual(self.service.shutdown(), 1)
        self.assertEqual(self.service.shutdown(), 0)
        admission = self.core.admission.snapshot()
        self.assertEqual(admission.active_sessions, 0)
        self.assertEqual(admission.logical_jobs, 1)
        self.assertEqual(admission.unsettled_jobs, 0)
        self.assertEqual(admission.physical_workers, 0)
        self.assertEqual(self.registry._grants, {})
        self.assertEqual(self.core._launch_grants, {})
        self.tasks.tasks[0]()
        self.assertEqual(self.executor.calls, 0)

        with self.assertRaises(LaunchSelectionError) as captured:
            self._bootstrap(_context_id("b"), "after-shutdown")
        self.assertEqual(captured.exception.code, "authority_denied")
        with self.assertRaises(LaunchSelectionError) as captured:
            self.service.exchange(
                actor_authority=actor,
                expected_epoch=SHARED_EPOCH,
                launch_selection_id=SELECTION_ID,
                notice_version_presented=NOTICE_VERSION,
                idempotency_key="new-after-shutdown",
            )
        self.assertEqual(captured.exception.code, "authority_denied")

    def test_shutdown_cancels_running_work_and_fences_late_completion(self) -> None:
        clock = ManualClock()
        tasks = ManualTasks()
        executor = BlockingExecutor()
        service, _registry, core, _actors = _service(
            clock=clock,
            tasks=tasks,
            executor=executor,
        )
        actor = _bootstrap(service, _context_id("a"), "bootstrap")["actor_context"]
        grant = _exchange(service, actor, "exchange")["launch_grant"]
        created = service.create(
            actor_authority=actor,
            launch_grant=grant,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create",
        )
        service.submit_command(
            actor_authority=actor,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="submit",
            expected_slot_generation=0,
            command="id",
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(tasks.tasks[0])
            self.assertTrue(executor.started.wait(1))
            self.assertEqual(service.shutdown(), 1)
            self.assertTrue(executor.cancelled.wait(1))
            future.result(timeout=1)

        admission = core.admission.snapshot()
        self.assertEqual(admission.active_sessions, 0)
        self.assertEqual(admission.logical_jobs, 1)
        self.assertEqual(admission.unsettled_jobs, 0)
        self.assertEqual(admission.physical_workers, 0)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(len(core._sessions_by_reference), 0)
        self.assertEqual(len(core._tombstones_by_reference), 1)

    def test_exchange_create_and_isolated_standard_sessions(self) -> None:
        actor_a = self._bootstrap(_context_id("a"), "boot-a")["actor_context"]
        actor_b = self._bootstrap(_context_id("b"), "boot-b")["actor_context"]
        grant_a = _exchange(self.service, actor_a, "exchange-a")["launch_grant"]
        grant_b = _exchange(self.service, actor_b, "exchange-b")["launch_grant"]
        created_a = self.service.create(
            actor_authority=actor_a,
            launch_grant=grant_a,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="same-textual-create-key",
        )
        created_b = self.service.create(
            actor_authority=actor_b,
            launch_grant=grant_b,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="same-textual-create-key",
        )
        session_a = self.core._sessions_by_id[created_a["snapshot"]["session_id"]]
        session_b = self.core._sessions_by_id[created_b["snapshot"]["session_id"]]
        session_a.runtime_document["profile"]["state"]["world_state"]["name"] = (
            "changed only in A"
        )
        self.assertEqual(
            session_b.runtime_document["profile"]["state"]["world_state"]["name"],
            "private baseline",
        )
        revision = next(iter(self.core._revisions.values())).allocate()
        self.assertEqual(
            revision["profile"]["state"]["world_state"]["name"],
            "private baseline",
        )
        admission = self.core.admission.snapshot()
        self.assertEqual(admission.active_sessions, 2)

        with self.assertRaises(ProductLifecycleError) as captured:
            self.service.create(
                actor_authority=actor_a,
                launch_grant=grant_b,
                expected_epoch=SHARED_EPOCH,
                idempotency_key="cross-grant",
            )
        self.assertEqual(captured.exception.code, "authority_denied")

        foreign_arguments = {
            "actor_authority": actor_b,
            "session_authority": created_a["session_authority"],
            "reference": created_a["snapshot"]["reference"],
            "expected_epoch": SHARED_EPOCH,
        }
        with self.assertRaises(ProductLifecycleError) as captured:
            self.service.snapshot(**foreign_arguments)
        self.assertEqual(captured.exception.code, "authority_denied")
        with self.assertRaises(ProductLifecycleError) as captured:
            self.service.submit_command(
                **foreign_arguments,
                idempotency_key="cross-submit",
                expected_slot_generation=1,
                command="id",
            )
        self.assertEqual(captured.exception.code, "authority_denied")
        with self.assertRaises(ProductLifecycleError) as captured:
            self.service.close(
                **foreign_arguments,
                idempotency_key="cross-close",
                expected_control_revision=created_a["snapshot"]["coordinates"][
                    "control_revision"
                ],
            )
        self.assertEqual(captured.exception.code, "authority_denied")
        self.assertEqual(self.executor.calls, 0)
        self.assertEqual(self.core.admission.snapshot().active_sessions, 2)

    def test_attendee_saturation_preserves_presenter_reservation(self) -> None:
        service, _registry, core, _actors = _service(
            clock=ManualClock(),
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            standard_session_capacity=1,
        )
        actor_a = _bootstrap(service, _context_id("a"), "boot-a")["actor_context"]
        actor_b = _bootstrap(service, _context_id("b"), "boot-b")["actor_context"]
        grant_a = _exchange(service, actor_a, "exchange-a")["launch_grant"]
        grant_b = _exchange(service, actor_b, "exchange-b")["launch_grant"]
        service.create(
            actor_authority=actor_a,
            launch_grant=grant_a,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create-a",
        )
        with self.assertRaises(ProductLifecycleError) as captured:
            service.create(
                actor_authority=actor_b,
                launch_grant=grant_b,
                expected_epoch=SHARED_EPOCH,
                idempotency_key="create-b",
            )
        self.assertEqual(captured.exception.code, "capacity_exhausted")
        self.assertEqual(core.admission.snapshot().active_sessions, 1)
        self.assertEqual(
            core.admission.limits.presenter_stage_reserved_sessions,
            1,
        )

    def test_facade_checks_context_before_every_delegated_operation(self) -> None:
        unclaimed = self.actors[0].authority
        cases = {
            "create": {},
            "current": {},
            "snapshot": {},
            "submit_command": {},
            "cancel_command": {},
            "command_result": {},
            "events": {},
            "transcript": {},
            "reset": {},
            "switch": {},
            "close": {},
        }
        for method_name, extra in cases.items():
            with self.subTest(method_name=method_name):
                with patch.object(
                    self.core,
                    method_name,
                    side_effect=AssertionError("core must not be called"),
                ):
                    with self.assertRaises(ProductLifecycleError) as captured:
                        getattr(self.service, method_name)(
                            actor_authority=unclaimed,
                            expected_epoch=SHARED_EPOCH,
                            **extra,
                        )
                    self.assertEqual(captured.exception.code, "authority_denied")

    def test_actor_and_grant_use_wall_and_monotonic_expiry(self) -> None:
        clock = ManualClock()
        service, _registry, _core, _actors = _service(
            clock=clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            grant_ttl_ms=1_000,
        )
        actor = _bootstrap(service, _context_id("a"), "bootstrap")["actor_context"]
        grant = _exchange(service, actor, "exchange")["launch_grant"]
        clock.wall = 2_001
        clock.monotonic = 1_100
        with self.assertRaises(ProductLifecycleError) as captured:
            service.create(
                actor_authority=actor,
                launch_grant=grant,
                expected_epoch=SHARED_EPOCH,
                idempotency_key="wall-expired-grant",
            )
        self.assertEqual(captured.exception.code, "authority_denied")

        clock = ManualClock()
        service, _registry, _core, _actors = _service(
            clock=clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            grant_ttl_ms=1_000,
        )
        actor = _bootstrap(service, _context_id("m"), "bootstrap")["actor_context"]
        grant = _exchange(service, actor, "exchange")["launch_grant"]
        clock.wall = 1_100
        clock.monotonic = 2_001
        with self.assertRaises(ProductLifecycleError) as captured:
            service.create(
                actor_authority=actor,
                launch_grant=grant,
                expected_epoch=SHARED_EPOCH,
                idempotency_key="monotonic-expired-grant",
            )
        self.assertEqual(captured.exception.code, "authority_denied")

    def test_short_context_ttl_terminalizes_sessions_on_either_clock(self) -> None:
        for dimension in ("wall", "monotonic"):
            with self.subTest(dimension=dimension):
                clock = ManualClock()
                service, _registry, core, _actors = _service(
                    clock=clock,
                    tasks=ManualTasks(),
                    executor=CountingExecutor(),
                    context_ttl_ms=100,
                    grant_ttl_ms=5_000,
                    absolute_ttl_ms=5_000,
                )
                actor = _bootstrap(service, _context_id(dimension[0]), "bootstrap")[
                    "actor_context"
                ]
                grant = _exchange(service, actor, "exchange")["launch_grant"]
                service.create(
                    actor_authority=actor,
                    launch_grant=grant,
                    expected_epoch=SHARED_EPOCH,
                    idempotency_key="create",
                )
                self.assertEqual(core.admission.snapshot().active_sessions, 1)
                setattr(clock, dimension, 1_101)
                self.assertEqual(service.expire_due(), 1)
                self.assertEqual(core.admission.snapshot().active_sessions, 0)
                self.assertEqual(service.expire_due(), 0)
                self.assertEqual(core.admission.snapshot().active_sessions, 0)

    def test_wall_expiry_terminalizes_and_releases_active_session(self) -> None:
        clock = ManualClock()
        service, _registry, core, _actors = _service(
            clock=clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            grant_ttl_ms=1_000,
            context_ttl_ms=5_000,
        )
        actor = _bootstrap(service, _context_id("a"), "bootstrap")["actor_context"]
        grant = _exchange(service, actor, "exchange")["launch_grant"]
        service.create(
            actor_authority=actor,
            launch_grant=grant,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create",
        )
        self.assertEqual(core.admission.snapshot().active_sessions, 1)
        clock.wall = 2_001
        clock.monotonic = 1_100
        current = service.current(
            actor_authority=actor,
            expected_epoch=SHARED_EPOCH,
        )
        self.assertEqual(current["kind"], "current_session_terminal")
        self.assertEqual(core.admission.snapshot().active_sessions, 0)
        self.assertEqual(len(core._sessions_by_reference), 0)
        self.assertEqual(len(core._tombstones_by_reference), 1)

    def test_dynamic_grant_prunes_while_tombstone_retains_safe_projection(self) -> None:
        clock = ManualClock()
        service, _registry, core, _actors = _service(
            clock=clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            grant_ttl_ms=100,
            absolute_ttl_ms=500,
            tombstone_ttl_ms=500,
        )
        actor = _bootstrap(service, _context_id("a"), "bootstrap")["actor_context"]
        grant = _exchange(service, actor, "exchange")["launch_grant"]
        created = service.create(
            actor_authority=actor,
            launch_grant=grant,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create",
        )
        snapshot = created["snapshot"]
        service.close(
            actor_authority=actor,
            session_authority=created["session_authority"],
            reference=snapshot["reference"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="close",
            expected_control_revision=snapshot["coordinates"]["control_revision"],
        )
        digest = hashlib.sha256(grant.encode()).hexdigest()
        clock.wall = clock.monotonic = 1_101
        core.expire_due()
        self.assertNotIn(digest, core._launch_grants)
        self.assertEqual(len(core._tombstones_by_reference), 1)
        tombstone = next(iter(core._tombstones_by_reference.values()))
        self.assertEqual(tombstone.notice_version, NOTICE_VERSION)
        self.assertEqual(tombstone.revision_id, next(iter(core._revisions)))

        clock.wall = clock.monotonic = 2_001
        core.expire_due()
        self.assertNotIn(digest, core._launch_grants)
        self.assertEqual(core._tombstones_by_reference, {})
        self.assertEqual(len(core._revisions), 1)

    def test_composition_rejects_forge_and_presenter_authority(self) -> None:
        document = _authored_document()
        forged = dict(document)
        forged["kind"] = "dreamland.world_forge_revision"
        with self.assertRaises(ValueError):
            AttendeeSessionCoreService(
                revisions=(ImmutableRevisionSeed("forged", forged),),
                actors=(
                    ActorAuthoritySeed(
                        token=self.actors[0].authority,
                        actor_id=self.actors[0].actor_id,
                        role=ProductRole.ATTENDEE,
                        expires_at_ms=100_000,
                    ),
                ),
                executor=self.executor,
                submit_task=self.tasks,
                clock_ms=self.clock,
                monotonic_ms=self.clock.monotonic_ms,
                token_factory=CoreTokens(),
            )
        with self.assertRaises(ValueError):
            AttendeeSessionCoreService(
                revisions=(
                    ImmutableRevisionSeed(
                        next(iter(self.registry._revisions)), document
                    ),
                ),
                actors=(
                    ActorAuthoritySeed(
                        token="presenter-authority-test-00000001",
                        actor_id="presenter:test",
                        role=ProductRole.PRESENTER,
                        expires_at_ms=100_000,
                    ),
                ),
                executor=self.executor,
                submit_task=self.tasks,
                clock_ms=self.clock,
                monotonic_ms=self.clock.monotonic_ms,
                token_factory=CoreTokens(),
            )

    def test_late_forge_offering_is_hidden_and_uniformly_unavailable(self) -> None:
        actor = self._bootstrap(_context_id("a"), "bootstrap")["actor_context"]
        forged = _authored_document()
        forged["kind"] = "dreamland.world_forge_revision"
        revision = _launch_revision(forged)
        self.registry.register_revision(revision)
        forged_selection = "launch-selection:forged-attendee-opaque-000001"
        self.registry.register_offering(
            LaunchOfferingSeed(
                offering_id="offering:forged-attendee-a1",
                launch_selection_id=forged_selection,
                revision_id=revision.revision_id,
                origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
                title="Unpublished Forge world",
                source_label="World Forge",
                revision_label="Private selection",
                role=ProductRole.ATTENDEE,
                allowed_operations=frozenset({ProductOperation.CREATE}),
                limits={
                    "absolute_ttl_ms": 1_000,
                    "idle_ttl_ms": 1_000,
                    "max_commands": 1,
                    "max_command_bytes": 1_024,
                    "command_reserved_tokens": 100,
                    "command_reserved_spend_units": 100,
                },
                policy_id="policy:unpublished-forge-v1",
                notice_version=NOTICE_VERSION,
                expires_at_ms=None,
                launch_grant_ttl_ms=1_000,
            )
        )
        options = self.service.options(
            actor_authority=actor,
            expected_epoch=SHARED_EPOCH,
        )
        self.assertEqual(len(options["options"]), 1)
        self.assertNotIn(forged_selection, json.dumps(options, sort_keys=True))

        for _attempt in range(2):
            with self.assertRaises(LaunchSelectionError) as captured:
                self.service.exchange(
                    actor_authority=actor,
                    expected_epoch=SHARED_EPOCH,
                    launch_selection_id=forged_selection,
                    notice_version_presented=NOTICE_VERSION,
                    idempotency_key="forged-selection",
                )
            self.assertEqual(captured.exception.code, "launch_selection_unavailable")
        with self.assertRaises(LaunchSelectionError) as captured:
            self.service.exchange(
                actor_authority=actor,
                expected_epoch=SHARED_EPOCH,
                launch_selection_id=SELECTION_ID,
                notice_version_presented=NOTICE_VERSION,
                idempotency_key="forged-selection",
            )
        self.assertEqual(captured.exception.code, "idempotency_conflict")
        self.assertEqual(self.core._launch_grants, {})

    def test_composition_rejects_an_expired_slot_before_it_can_block_pool(self) -> None:
        digest = hashlib.sha256(self.actors[0].authority.encode()).hexdigest()
        self.registry._actors[digest] = replace(
            self.registry._actors[digest],
            expires_at_ms=self.clock.wall,
            expires_deadline_ms=self.clock.monotonic,
        )
        self.core._actors[digest] = replace(
            self.core._actors[digest],
            expires_at_ms=self.clock.wall,
            expires_deadline_ms=self.clock.monotonic,
        )
        expired_seed = replace(self.actors[0], expires_at_ms=self.clock.wall)
        with self.assertRaises(ValueError):
            PublicAttendeeSessionService(
                registry=self.registry,
                core=self.core,
                context_actors=(expired_seed, self.actors[1]),
                bff_authorities=(
                    AttendeeBffAuthoritySeed(
                        authority=BFF_AUTHORITY,
                        authority_id="bff:gateway-test",
                        expires_at_ms=100_000,
                    ),
                ),
                policy=AttendeeContextPolicy(
                    max_contexts=2,
                    max_bootstrap_idempotency_records=8,
                ),
            )

    def _bootstrap(self, context_id: str, key: str) -> dict[str, object]:
        return _bootstrap(self.service, context_id, key)


def _service(
    *,
    clock: ManualClock,
    tasks: ManualTasks,
    executor: CountingExecutor,
    max_contexts: int = 2,
    context_ttl_ms: int = 5_000,
    grant_ttl_ms: int = 5_000,
    admission_opens_at_ms: int = 0,
    admission_closes_at_ms: int = 90_000,
    bootstrap_closes_at_admission: bool = False,
    bff_expires_at_ms: int = 100_000,
    actor_expires_at_ms: int = 100_000,
    standard_session_capacity: int = 2,
    absolute_ttl_ms: int = 5_000,
    tombstone_ttl_ms: int = 1_000,
    bootstrap_idempotency_capacity: int | None = None,
    revoke_idempotency_capacity: int | None = None,
    request_admission: AttendeeRequestAdmissionLedger | None = None,
) -> tuple[
    PublicAttendeeSessionService,
    LaunchSelectionRegistry,
    AttendeeSessionCoreService,
    tuple[LaunchSelectionActorSeed, ...],
]:
    document = _authored_document()
    revision = _launch_revision(document)
    actors = tuple(
        LaunchSelectionActorSeed(
            authority=f"attendee-authority:pool-slot-{index:08d}",
            actor_id=f"attendee:browser-slot-{index}",
            role=ProductRole.ATTENDEE,
            expires_at_ms=actor_expires_at_ms,
        )
        for index in range(max_contexts)
    )
    launch_policy = LaunchSelectionPolicy(
        max_revisions=2,
        max_offerings=2,
        max_actor_authorities=max_contexts,
        max_active_launch_grants=16,
        max_idempotency_records=32,
        max_token_history=max(32, max_contexts + 3),
        max_revision_bytes=4 * 1024 * 1024,
        max_launch_grant_ttl_ms=max(5_000, grant_ttl_ms),
        idempotency_ttl_ms=max(10_000, grant_ttl_ms + 1),
    )
    registry = LaunchSelectionRegistry(
        revisions=(revision,),
        offerings=(
            LaunchOfferingSeed(
                offering_id="offering:attendee-authored-a1",
                launch_selection_id=SELECTION_ID,
                revision_id=revision.revision_id,
                origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                title="Colony Systems",
                source_label="Authored",
                revision_label="Pinned A1",
                role=ProductRole.ATTENDEE,
                allowed_operations=frozenset(
                    {ProductOperation.CREATE, ProductOperation.SWITCH}
                ),
                limits={
                    "absolute_ttl_ms": absolute_ttl_ms,
                    "idle_ttl_ms": min(absolute_ttl_ms, 3_000),
                    "max_commands": 4,
                    "max_command_bytes": 1_024,
                    "command_reserved_tokens": 100,
                    "command_reserved_spend_units": 100,
                },
                policy_id="policy:attendee-v1",
                notice_version=NOTICE_VERSION,
                expires_at_ms=admission_closes_at_ms,
                launch_grant_ttl_ms=grant_ttl_ms,
            ),
        ),
        actors=actors,
        policy=launch_policy,
        allowed_operation_families=frozenset(
            {ProductOperation.CREATE, ProductOperation.SWITCH}
        ),
        clock_ms=clock,
        monotonic_ms=clock.monotonic_ms,
        token_factory=RegistryTokens(),
    )
    lifecycle_policy = LifecyclePolicy(
        absolute_ttl_ms=absolute_ttl_ms,
        idle_ttl_ms=min(absolute_ttl_ms, 3_000),
        tombstone_ttl_ms=tombstone_ttl_ms,
        failed_operation_ttl_ms=500,
        max_active_sessions=max(2, standard_session_capacity),
        max_tombstones=max(4, standard_session_capacity * 2),
        max_idempotency_records=32,
        max_launch_grants=16,
        max_actor_authorities=max_contexts,
        max_revisions=2,
    )
    total_sessions = standard_session_capacity + 1
    admission = ProductAdmissionLedger(
        ProductAdmissionLimits(
            max_active_sessions=total_sessions,
            max_logical_jobs=8,
            max_physical_workers=4,
            token_budget=2_000,
            spend_budget_units=2_000,
            presenter_stage_reserved_sessions=1,
            presenter_stage_reserved_logical_jobs=1,
            presenter_stage_reserved_physical_workers=1,
            presenter_stage_reserved_token_budget=100,
            presenter_stage_reserved_spend_budget_units=100,
        )
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
        executor=executor,
        submit_task=tasks,
        lifecycle_policy=lifecycle_policy,
        core_policy=ProductCorePolicy(
            max_commands_per_session=4,
            command_deadline_ms=1_000,
            cancellation_deadline_ms=500,
            max_output_bytes_per_command=4_096,
            max_output_chunks_per_command=32,
            max_runtime_document_bytes=4 * 1024 * 1024,
            reserved_tokens_per_command=100,
            reserved_spend_units_per_command=100,
        ),
        admission=admission,
        clock_ms=clock,
        monotonic_ms=clock.monotonic_ms,
        token_factory=CoreTokens(),
    )
    service = PublicAttendeeSessionService(
        registry=registry,
        core=core,
        context_actors=actors,
        bff_authorities=(
            AttendeeBffAuthoritySeed(
                authority=BFF_AUTHORITY,
                authority_id="bff:gateway-test",
                expires_at_ms=bff_expires_at_ms,
            ),
        ),
        policy=AttendeeContextPolicy(
            max_contexts=max_contexts,
            context_ttl_ms=context_ttl_ms,
            max_bootstrap_idempotency_records=(
                bootstrap_idempotency_capacity
                if bootstrap_idempotency_capacity is not None
                else max(8, max_contexts + 1)
            ),
            max_exchange_idempotency_records=32,
            max_revoke_idempotency_records=(
                revoke_idempotency_capacity
                if revoke_idempotency_capacity is not None
                else max(8, max_contexts)
            ),
            admission_opens_at_ms=admission_opens_at_ms,
            admission_closes_at_ms=admission_closes_at_ms,
            bootstrap_closes_at_admission=bootstrap_closes_at_admission,
        ),
        request_admission=request_admission,
    )
    return service, registry, core, actors


def _authored_document() -> dict[str, object]:
    return {
        "kind": "dreamland.authored_world_revision",
        "schema_revision": "1",
        "profile": {
            "level": "world",
            "state": {
                "world_state": {
                    "name": "private baseline",
                    "profile_path": "/operator/private/colony.json",
                    "active_host": "console",
                    "entry_host": "console",
                    "hosts_by_name": {"console": {"hostname": "console"}},
                }
            },
        },
    }


def _launch_revision(document: dict[str, object]) -> LaunchRevisionSeed:
    canonical = (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    return LaunchRevisionSeed(
        revision_id=f"world-revision:sha256:{digest}",
        document=document,
    )


def _context_id(character: str) -> str:
    return "gateway-context:" + character * 32


def _bootstrap(
    service: PublicAttendeeSessionService,
    context_id: str,
    key: str,
) -> dict[str, object]:
    return service.bootstrap_context(
        bff_authority=BFF_AUTHORITY,
        expected_epoch=SHARED_EPOCH,
        gateway_context_id=context_id,
        idempotency_key=key,
    )


def _exchange(
    service: PublicAttendeeSessionService,
    actor_authority: str,
    key: str,
) -> dict[str, object]:
    return service.exchange(
        actor_authority=actor_authority,
        expected_epoch=SHARED_EPOCH,
        launch_selection_id=SELECTION_ID,
        notice_version_presented=NOTICE_VERSION,
        idempotency_key=key,
    )


if __name__ == "__main__":
    unittest.main()
