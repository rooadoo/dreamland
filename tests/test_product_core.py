from __future__ import annotations

import threading
import unittest
from collections.abc import Callable
from copy import deepcopy

from dreamland.product.admission import (
    ProductAdmissionLedger,
    ProductAdmissionLimits,
)
from dreamland.product.core import ProductCorePolicy, ProductSessionCoreService
from dreamland.product.execution import (
    ProductExecutionRequest,
    ProductExecutionResult,
    ProductOutputChunk,
    ProductOutputStream,
    ProductStateEffect,
    ProductStateEffectKind,
)
from dreamland.product.journal import EventCursor, JournalLimits
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductOperation,
    ProductRole,
)

ALICE_ACTOR = "alice-product-actor-authority-0001"
BOB_ACTOR = "bob-product-actor-authority-000001"
ALICE_GRANT = "alice-product-launch-grant-0000001"
ALICE_SWITCH_GRANT = "alice-product-switch-grant-000001"
BOB_GRANT = "bob-product-launch-grant-000000001"


class FakeClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self._now_ms = now_ms
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            return self._now_ms

    def advance(self, milliseconds: int) -> None:
        with self._lock:
            self._now_ms += milliseconds


class SequenceTokens:
    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()

    def __call__(self, purpose: str) -> str:
        with self._lock:
            self._count += 1
            return f"core-{purpose}-{self._count:04d}-opaque-value"


class ManualTasks:
    def __init__(self) -> None:
        self.tasks: list[Callable[[], None]] = []

    def __call__(self, task: Callable[[], None]) -> None:
        self.tasks.append(task)

    def run_next(self) -> None:
        self.tasks.pop(0)()


class RunThenRaiseTasks:
    def __call__(self, task: Callable[[], None]) -> None:
        task()
        raise RuntimeError("synthetic post-dispatch submitter failure")


class ScriptedExecutor:
    def __init__(
        self,
        result: ProductExecutionResult | None = None,
        chunks: tuple[ProductOutputChunk, ...] = (),
    ) -> None:
        self.result = result or ProductExecutionResult.completed_exited(exit_status=0)
        self.chunks = chunks
        self.requests: list[ProductExecutionRequest] = []

    def execute(self, request, emit):
        self.requests.append(request)
        for chunk in self.chunks:
            emit(chunk)
        return self.result


class RaisingExecutor:
    def execute(self, _request, _emit):
        raise RuntimeError("synthetic executor failure")


class BlockingExecutor:
    def __init__(self, result: ProductExecutionResult) -> None:
        self.result = result
        self.started = threading.Event()
        self.release = threading.Event()
        self.requests: list[ProductExecutionRequest] = []

    def execute(self, request, emit):
        self.requests.append(request)
        self.started.set()
        if not self.release.wait(2):
            raise RuntimeError("test executor release timed out")
        return self.result


class ConcurrentExecutor:
    def __init__(self) -> None:
        self.both_started = threading.Event()
        self.release = threading.Event()
        self.requests: list[ProductExecutionRequest] = []
        self.max_active = 0
        self._active = 0
        self._lock = threading.Lock()

    def execute(self, request, emit):
        with self._lock:
            self.requests.append(request)
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if self._active == 2:
                self.both_started.set()
        if not self.release.wait(2):
            raise RuntimeError("test executor release timed out")
        with self._lock:
            self._active -= 1
        return ProductExecutionResult.completed_exited(exit_status=0)


class ProductSessionCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tasks = ManualTasks()
        self.executor = ScriptedExecutor(
            chunks=(
                ProductOutputChunk(ProductOutputStream.STDOUT, "hello\n"),
                ProductOutputChunk(ProductOutputStream.STDERR, "warning\n"),
            )
        )
        self.service = _service(
            clock=self.clock,
            tasks=self.tasks,
            executor=self.executor,
        )
        self.created = _create(self.service)
        self.reference = str(self.created["snapshot"]["reference"])
        self.session_authority = str(self.created["session_authority"])

    def test_create_initializes_independent_core_coordinates_and_journal(self) -> None:
        snapshot = self.created["snapshot"]

        self.assertEqual(snapshot["coordinates"]["control_revision"], 1)
        self.assertEqual(snapshot["coordinates"]["command_slot_generation"], 0)
        self.assertEqual(snapshot["coordinates"]["accepted_state_revision"], 0)
        self.assertEqual(snapshot["coordinates"]["event"]["sequence"], 1)
        self.assertEqual(snapshot["coordinates"]["transcript"]["position"], 0)
        self.assertEqual(snapshot["command_slot"]["state"], "available")
        self.assertEqual(self.service.admission.snapshot().active_sessions, 1)

    def test_create_rolls_back_lifecycle_if_product_admission_changes(self) -> None:
        admission = ProductAdmissionLedger(
            ProductAdmissionLimits(
                max_active_sessions=4,
                max_logical_jobs=8,
                max_physical_workers=2,
                token_budget=1_000,
                spend_budget_units=500,
            )
        )
        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            admission=admission,
        )
        for index in range(4):
            admission.admit_session(f"external-session-{index}")

        code = self._error_code(
            lambda: _create(service, key="capacity-rollback-create")
        )

        self.assertEqual(code, "capacity_exhausted")
        current = service.current(
            actor_authority=ALICE_ACTOR,
            expected_epoch=service.service_epoch,
        )
        self.assertEqual(current["kind"], "no_current_session")
        for index in range(4):
            admission.release_session(f"external-session-{index}")
        created = _create(service, key="capacity-rollback-create")
        self.assertEqual(created["kind"], "session_created")
        self.assertEqual(admission.snapshot().active_sessions, 1)

    def test_create_fatal_initialization_rolls_back_post_admit_state(self) -> None:
        class FatalInitialization(BaseException):
            pass

        class AdmitThenFailOnce(ProductAdmissionLedger):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next = True

            def admit_session(self, session_id):
                admission = super().admit_session(session_id)
                if self.fail_next:
                    self.fail_next = False
                    raise FatalInitialization()
                return admission

        admission = AdmitThenFailOnce()
        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            admission=admission,
        )

        with self.assertRaises(FatalInitialization):
            _create(service, key="fatal-initialization-create")

        self.assertEqual(admission.snapshot().active_sessions, 0)
        self.assertEqual(service._sessions_by_reference, {})
        self.assertEqual(service._sessions_by_id, {})
        self.assertEqual(service._session_authorities, {})
        self.assertEqual(service._current_by_actor, {})
        self.assertEqual(service._core_sessions, {})
        self.assertEqual(service._idempotency, {})
        alice_grant = next(
            grant
            for grant in service._launch_grants.values()
            if grant.actor_id == "alice"
            and ProductOperation.CREATE in grant.allowed_operations
        )
        self.assertIsNone(alice_grant.consumed_session_id)

        retried = _create(service, key="fatal-initialization-create")

        self.assertEqual(retried["kind"], "session_created")
        self.assertFalse(retried["replayed"])
        self.assertEqual(admission.snapshot().active_sessions, 1)

    def test_submit_returns_acceptance_before_executor_and_result_is_queryable(
        self,
    ) -> None:
        accepted = self._submit()

        self.assertEqual(accepted["kind"], "command_accepted")
        self.assertEqual(accepted["canonical_command"], "uname -a")
        self.assertEqual(accepted["slot_generation"], 1)
        self.assertEqual(len(self.tasks.tasks), 1)
        self.assertEqual(self.executor.requests, [])
        self.assertFalse(self._result(str(accepted["command_id"]))["terminal"])

        self.tasks.run_next()

        result = self._result(str(accepted["command_id"]))
        self.assertTrue(result["terminal"])
        self.assertEqual(result["command"]["status"], "completed")
        self.assertEqual(
            result["command"]["result"]["guest_termination"],
            {"kind": "exited", "exit_status": 0},
        )
        transcript = self._transcript()
        self.assertEqual(
            [entry["kind"] for entry in transcript["entries"]],
            ["command", "stdout", "stderr", "stdout", "stderr"],
        )
        self.assertEqual(transcript["entries"][1]["text"], "hello\n")
        snapshot = self._snapshot()
        self.assertEqual(snapshot["command_slot"]["state"], "available")
        self.assertEqual(snapshot["coordinates"]["command_slot_generation"], 1)
        self.assertEqual(snapshot["coordinates"]["accepted_state_revision"], 0)

    def test_output_is_bounded_by_bytes_and_chunks_with_visible_clipping(self) -> None:
        tasks = ManualTasks()
        executor = ScriptedExecutor(
            chunks=(
                ProductOutputChunk(ProductOutputStream.STDOUT, "ééé"),
                ProductOutputChunk(ProductOutputStream.STDOUT, "tail"),
            )
        )
        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=executor,
            core_policy=ProductCorePolicy(
                max_commands_per_session=8,
                command_deadline_ms=10_000,
                cancellation_deadline_ms=2_000,
                max_output_bytes_per_command=5,
                max_output_chunks_per_command=1,
                reserved_tokens_per_command=100,
                reserved_spend_units_per_command=50,
            ),
        )
        created = _create(service, key="output-bounds-create")
        reference = str(created["snapshot"]["reference"])
        authority = str(created["session_authority"])
        accepted = _submit(
            service,
            reference=reference,
            session_authority=authority,
        )

        tasks.run_next()

        result = service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
            command_id=str(accepted["command_id"]),
        )
        transcript = service.transcript(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
        )
        output = result["command"]["output"]
        self.assertEqual(output, {"bytes": 4, "chunks": 1, "clipped": True})
        stdout = [
            entry
            for entry in transcript["entries"]
            if entry["kind"] == "stdout" and not entry["final"]
        ]
        self.assertEqual(len(stdout), 1)
        self.assertEqual(stdout[0]["text"], "éé")
        self.assertEqual(stdout[0]["omitted_after_bytes"], 2)

    def test_exact_output_boundary_is_complete_until_content_is_omitted(self) -> None:
        tasks = ManualTasks()
        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=ScriptedExecutor(
                chunks=(ProductOutputChunk(ProductOutputStream.STDOUT, "test"),)
            ),
            core_policy=ProductCorePolicy(
                max_commands_per_session=8,
                command_deadline_ms=10_000,
                cancellation_deadline_ms=2_000,
                max_output_bytes_per_command=4,
                max_output_chunks_per_command=4,
                reserved_tokens_per_command=100,
                reserved_spend_units_per_command=50,
            ),
        )
        created = _create(service, key="exact-output-bound-create")
        accepted = _submit(
            service,
            reference=str(created["snapshot"]["reference"]),
            session_authority=str(created["session_authority"]),
            key="exact-output-bound-command",
        )

        tasks.run_next()

        result = service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=str(created["session_authority"]),
            reference=str(created["snapshot"]["reference"]),
            expected_epoch=service.service_epoch,
            command_id=str(accepted["command_id"]),
        )
        self.assertEqual(
            result["command"]["output"],
            {"bytes": 4, "chunks": 1, "clipped": False},
        )

    def test_output_publisher_stops_reentering_core_after_limit_closes(self) -> None:
        class NoisyExecutor:
            def __init__(self) -> None:
                self.callback_results: list[bool | None] = []

            def execute(self, _request, emit):
                for index in range(100):
                    self.callback_results.append(
                        emit(
                            ProductOutputChunk(
                                ProductOutputStream.STDOUT,
                                f"chunk-{index}",
                            )
                        )
                    )
                return ProductExecutionResult.completed_exited(exit_status=0)

        tasks = ManualTasks()
        executor = NoisyExecutor()
        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=executor,
            core_policy=ProductCorePolicy(
                max_commands_per_session=8,
                command_deadline_ms=10_000,
                cancellation_deadline_ms=2_000,
                max_output_bytes_per_command=1_024,
                max_output_chunks_per_command=1,
                reserved_tokens_per_command=100,
                reserved_spend_units_per_command=50,
            ),
        )
        created = _create(service, key="noisy-output-create")
        reference = str(created["snapshot"]["reference"])
        authority = str(created["session_authority"])
        core_entries = 0
        original_emit = service._emit_output

        def count_core_entries(*args, **kwargs):
            nonlocal core_entries
            core_entries += 1
            return original_emit(*args, **kwargs)

        service._emit_output = count_core_entries  # type: ignore[method-assign]
        accepted = _submit(
            service,
            reference=reference,
            session_authority=authority,
            key="noisy-output-command",
        )

        tasks.run_next()

        self.assertEqual(core_entries, 2)
        self.assertEqual(executor.callback_results, [True] + [False] * 99)
        result = service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
            command_id=str(accepted["command_id"]),
        )
        self.assertEqual(
            result["command"]["output"],
            {"bytes": 7, "chunks": 1, "clipped": True},
        )

    def test_completed_signal_and_rejected_state_are_distinct_terminal_facts(
        self,
    ) -> None:
        self.executor.result = ProductExecutionResult.completed_signaled(
            signal="SIGTERM",
            state_effect=ProductStateEffect(
                ProductStateEffectKind.REJECTED,
                safe_reason="runtime_policy_rejected",
            ),
        )

        accepted = self._submit()
        self.tasks.run_next()

        result = self._result(str(accepted["command_id"]))["command"]["result"]
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(
            result["guest_termination"],
            {"kind": "signaled", "signal": "SIGTERM"},
        )
        self.assertEqual(
            result["state_effect"],
            {"kind": "rejected", "reason": "runtime_policy_rejected"},
        )
        self.assertEqual(
            self._snapshot()["coordinates"]["accepted_state_revision"],
            0,
        )

    def test_committed_state_effect_advances_only_accepted_state_revision(self) -> None:
        original = deepcopy(
            self.service._sessions_by_reference[self.reference].runtime_document
        )
        provisional = deepcopy(original)
        provisional["world_state"]["value"] = 2
        self.executor.result = ProductExecutionResult.completed_exited(
            exit_status=3,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document=provisional,
            ),
            usage_tokens=12,
            usage_spend_units=4,
        )

        accepted = self._submit()
        self.tasks.run_next()

        snapshot = self._snapshot()
        self.assertEqual(snapshot["coordinates"]["accepted_state_revision"], 1)
        self.assertEqual(snapshot["coordinates"]["control_revision"], 1)
        self.assertEqual(
            self.service._sessions_by_reference[self.reference].runtime_document[
                "world_state"
            ]["value"],
            2,
        )
        result = self._result(str(accepted["command_id"]))["command"]["result"]
        self.assertEqual(result["guest_termination"]["exit_status"], 3)
        self.assertEqual(
            result["state_effect"],
            {"kind": "committed", "accepted_state_revision": 1},
        )
        admission = self.service.admission.snapshot()
        self.assertEqual(admission.consumed_tokens, 12)
        self.assertEqual(admission.consumed_spend_units, 4)

    def test_invalid_or_oversized_state_effect_fails_without_live_mutation(
        self,
    ) -> None:
        tasks = ManualTasks()
        executor = ScriptedExecutor()
        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=executor,
            core_policy=ProductCorePolicy(
                max_commands_per_session=8,
                command_deadline_ms=10_000,
                cancellation_deadline_ms=2_000,
                max_output_bytes_per_command=1_024,
                max_output_chunks_per_command=16,
                max_runtime_document_bytes=128,
                reserved_tokens_per_command=100,
                reserved_spend_units_per_command=50,
            ),
        )
        created = _create(service, key="small-document-limit")
        reference = str(created["snapshot"]["reference"])
        authority = str(created["session_authority"])
        executor.result = ProductExecutionResult.completed_exited(
            exit_status=0,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document={"world_state": {"value": "x" * 256}},
            ),
        )

        accepted = _submit(
            service,
            reference=reference,
            session_authority=authority,
        )
        tasks.run_next()

        result = service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
            command_id=str(accepted["command_id"]),
        )
        snapshot = service.snapshot(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
        )["snapshot"]
        self.assertEqual(result["command"]["status"], "completed")
        self.assertEqual(
            result["command"]["result"]["state_effect"],
            {"kind": "rejected", "reason": "state_validation_rejected"},
        )
        self.assertEqual(snapshot["coordinates"]["accepted_state_revision"], 0)
        self.assertEqual(
            service._sessions_by_reference[reference].runtime_document["world_state"][
                "value"
            ],
            1,
        )

    def test_resource_overreport_fails_and_settles_within_reservation(self) -> None:
        self.executor.result = ProductExecutionResult.completed_exited(
            exit_status=0,
            usage_tokens=101,
            usage_spend_units=51,
        )

        accepted = self._submit()
        self.tasks.run_next()

        result = self._result(str(accepted["command_id"]))
        self.assertEqual(result["command"]["status"], "failed")
        self.assertEqual(
            result["command"]["result"]["reason"],
            "resource_usage_exceeded",
        )
        self.assertEqual(
            result["command"]["result"]["usage"],
            {"state": "settled", "tokens": 100, "spend_units": 50},
        )
        admission = self.service.admission.snapshot()
        self.assertEqual(admission.consumed_tokens, 100)
        self.assertEqual(admission.consumed_spend_units, 50)

    def test_unknown_post_dispatch_failure_consumes_full_reservation(self) -> None:
        self.service._executor = RaisingExecutor()
        accepted = self._submit(key="unknown-executor-failure")

        self.tasks.run_next()

        result = self._result(str(accepted["command_id"]))
        self.assertEqual(result["command"]["status"], "failed")
        self.assertEqual(
            result["command"]["result"]["reason"],
            "infrastructure_failure",
        )
        self.assertEqual(
            result["command"]["result"]["usage"],
            {"state": "settled", "tokens": 100, "spend_units": 50},
        )
        admission = self.service.admission.snapshot()
        self.assertEqual(admission.consumed_tokens, 100)
        self.assertEqual(admission.consumed_spend_units, 50)

    def test_fatal_executor_exit_reconciles_then_preserves_control_flow(self) -> None:
        class FatalExecutorExit(BaseException):
            pass

        class FatalExecutor:
            def execute(self, _request, _emit):
                raise FatalExecutorExit()

        self.service._executor = FatalExecutor()
        accepted = self._submit(key="fatal-executor-exit")

        with self.assertRaises(FatalExecutorExit):
            self.tasks.run_next()

        command_id = str(accepted["command_id"])
        result = self._result(command_id)
        record = self.service._core_sessions[
            str(self.created["snapshot"]["session_id"])
        ].commands[command_id]
        self.assertEqual(result["command"]["status"], "failed")
        self.assertEqual(
            result["command"]["result"]["reason"],
            "infrastructure_failure",
        )
        self.assertEqual(
            result["command"]["result"]["usage"],
            {"state": "settled", "tokens": 100, "spend_units": 50},
        )
        self.assertEqual(record.dispatch_state.value, "returned")
        self.assertTrue(record.worker_reconciled)
        self.assertIsNone(
            self.service._core_sessions[record.session_id].active_command_id
        )
        admission = self.service.admission.snapshot()
        self.assertEqual(admission.physical_workers, 0)
        self.assertEqual(admission.unsettled_jobs, 0)
        self.assertEqual(admission.settled_jobs, 1)
        self.assertEqual(admission.consumed_tokens, 100)
        self.assertEqual(admission.consumed_spend_units, 50)

    def test_authoritative_state_validator_rejects_provisional_commit(self) -> None:
        tasks = ManualTasks()
        executor = ScriptedExecutor()

        def reject(_document) -> None:
            raise RuntimeError("synthetic semantic rejection")

        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=executor,
            validate_state_effect=reject,
        )
        created = _create(service, key="state-validator-create")
        reference = str(created["snapshot"]["reference"])
        authority = str(created["session_authority"])
        provisional = deepcopy(
            service._sessions_by_reference[reference].runtime_document
        )
        provisional["world_state"]["value"] = 42
        executor.result = ProductExecutionResult.completed_exited(
            exit_status=0,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document=provisional,
            ),
        )

        accepted = _submit(
            service,
            reference=reference,
            session_authority=authority,
        )
        tasks.run_next()

        result = service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
            command_id=str(accepted["command_id"]),
        )
        self.assertEqual(
            result["command"]["result"]["state_effect"],
            {"kind": "rejected", "reason": "state_validation_rejected"},
        )
        self.assertEqual(
            service._sessions_by_reference[reference].runtime_document["world_state"][
                "value"
            ],
            1,
        )

    def test_fatal_state_validator_reconciles_full_reservation_before_raise(
        self,
    ) -> None:
        class FatalStateValidation(BaseException):
            pass

        tasks = ManualTasks()

        def validate(_document) -> None:
            raise FatalStateValidation()

        executor = ScriptedExecutor()
        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=executor,
            validate_state_effect=validate,
        )
        created = _create(service, key="fatal-state-validator-create")
        reference = str(created["snapshot"]["reference"])
        authority = str(created["session_authority"])
        session_id = str(created["snapshot"]["session_id"])
        provisional = deepcopy(
            service._sessions_by_reference[reference].runtime_document
        )
        provisional["world_state"]["value"] = 91
        executor.result = ProductExecutionResult.completed_exited(
            exit_status=0,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document=provisional,
            ),
            usage_tokens=3,
            usage_spend_units=2,
        )
        accepted = _submit(
            service,
            reference=reference,
            session_authority=authority,
            key="fatal-state-validator-command",
        )

        with self.assertRaises(FatalStateValidation):
            tasks.run_next()

        command_id = str(accepted["command_id"])
        result = service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
            command_id=command_id,
        )
        record = service._core_sessions[session_id].commands[command_id]
        self.assertEqual(result["command"]["status"], "failed")
        self.assertEqual(
            result["command"]["result"]["reason"],
            "infrastructure_failure",
        )
        self.assertEqual(
            result["command"]["result"]["usage"],
            {"state": "settled", "tokens": 100, "spend_units": 50},
        )
        self.assertEqual(record.dispatch_state.value, "returned")
        self.assertTrue(record.worker_reconciled)
        self.assertIsNone(service._core_sessions[session_id].active_command_id)
        self.assertEqual(
            service._sessions_by_reference[reference].runtime_document["world_state"][
                "value"
            ],
            1,
        )
        self.assertEqual(service._core_sessions[session_id].accepted_state_revision, 0)
        admission = service.admission.snapshot()
        self.assertEqual(admission.physical_workers, 0)
        self.assertEqual(admission.unsettled_jobs, 0)
        self.assertEqual(admission.settled_jobs, 1)
        self.assertEqual(admission.consumed_tokens, 100)
        self.assertEqual(admission.consumed_spend_units, 50)

    def test_state_effect_is_rejected_if_accepted_revision_changed(self) -> None:
        provisional = deepcopy(
            self.service._sessions_by_reference[self.reference].runtime_document
        )
        provisional["world_state"]["value"] = 42
        self.executor.result = ProductExecutionResult.completed_exited(
            exit_status=0,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document=provisional,
            ),
        )
        accepted = self._submit(key="state-revision-race")
        session_id = str(self.created["snapshot"]["session_id"])
        self.service._core_sessions[session_id].accepted_state_revision += 1

        self.tasks.run_next()

        result = self._result(str(accepted["command_id"]))
        self.assertEqual(result["command"]["status"], "completed")
        self.assertEqual(
            result["command"]["result"]["state_effect"],
            {"kind": "rejected", "reason": "state_revision_conflict"},
        )
        self.assertEqual(
            self.service._sessions_by_reference[self.reference].runtime_document[
                "world_state"
            ]["value"],
            1,
        )

    def test_detached_state_validation_does_not_block_unrelated_session(self) -> None:
        bob = _create_bob(self.service, key="bob-validation-create")
        validation_started = threading.Event()
        release_validation = threading.Event()

        def validate(_document) -> None:
            validation_started.set()
            if not release_validation.wait(2):
                raise RuntimeError("test validation release timed out")

        self.service._validate_state_effect = validate
        provisional = deepcopy(
            self.service._sessions_by_reference[self.reference].runtime_document
        )
        provisional["world_state"]["value"] = 17
        self.executor.result = ProductExecutionResult.completed_exited(
            exit_status=0,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document=provisional,
            ),
        )
        self._submit(key="alice-slow-validation")
        alice_worker = threading.Thread(target=self.tasks.run_next)
        alice_worker.start()
        self.assertTrue(validation_started.wait(1))
        bob_submitted = threading.Event()

        def submit_bob() -> None:
            _submit(
                self.service,
                actor_authority=BOB_ACTOR,
                reference=str(bob["snapshot"]["reference"]),
                session_authority=str(bob["session_authority"]),
                key="bob-during-alice-validation",
            )
            bob_submitted.set()

        bob_worker = threading.Thread(target=submit_bob)
        bob_worker.start()
        try:
            self.assertTrue(bob_submitted.wait(0.5))
        finally:
            release_validation.set()
            alice_worker.join(2)
            bob_worker.join(2)
        self.assertFalse(alice_worker.is_alive())
        self.assertFalse(bob_worker.is_alive())
        self.executor.result = ProductExecutionResult.completed_exited(exit_status=0)
        self.tasks.run_next()
        self.assertEqual(self.service.admission.snapshot().physical_workers, 0)

    def test_submit_replay_does_not_dispatch_or_reserve_twice(self) -> None:
        first = self._submit(key="same-submit")
        replay = self._submit(key="same-submit")

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["command_id"], first["command_id"])
        self.assertEqual(len(self.tasks.tasks), 1)
        self.assertEqual(self.service.admission.snapshot().logical_jobs, 1)

    def test_duplicate_dispatched_task_invokes_executor_exactly_once(self) -> None:
        accepted = self._submit(key="duplicate-dispatch")
        task = self.tasks.tasks.pop(0)

        task()
        task()

        self.assertEqual(len(self.executor.requests), 1)
        result = self._result(str(accepted["command_id"]))
        self.assertEqual(result["command"]["status"], "completed")
        admission = self.service.admission.snapshot()
        self.assertEqual(admission.physical_workers, 0)
        self.assertEqual(admission.settled_jobs, 1)

    def test_submitter_that_runs_then_raises_cannot_overwrite_terminal_truth(
        self,
    ) -> None:
        executor = ScriptedExecutor()
        service = _service(
            clock=self.clock,
            tasks=RunThenRaiseTasks(),
            executor=executor,
        )
        created = _create(service, key="synchronous-dispatch-create")

        accepted = _submit(
            service,
            reference=str(created["snapshot"]["reference"]),
            session_authority=str(created["session_authority"]),
            key="synchronous-dispatch",
        )
        result = service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=str(created["session_authority"]),
            reference=str(created["snapshot"]["reference"]),
            expected_epoch=service.service_epoch,
            command_id=str(accepted["command_id"]),
        )

        self.assertEqual(len(executor.requests), 1)
        self.assertEqual(result["command"]["status"], "completed")
        self.assertEqual(result["command"]["result"]["outcome"], "completed")

    def test_fatal_submitter_exit_terminalizes_and_exact_retry_replays(self) -> None:
        class FatalSubmitterExit(BaseException):
            pass

        class FatalSubmitter:
            def __call__(self, _task) -> None:
                raise FatalSubmitterExit()

        service = _service(
            clock=self.clock,
            tasks=FatalSubmitter(),
            executor=ScriptedExecutor(),
        )
        created = _create(service, key="fatal-submitter-create")
        reference = str(created["snapshot"]["reference"])
        authority = str(created["session_authority"])
        session_id = str(created["snapshot"]["session_id"])

        with self.assertRaises(FatalSubmitterExit):
            _submit(
                service,
                reference=reference,
                session_authority=authority,
                key="fatal-submitter-command",
            )

        replay = _submit(
            service,
            reference=reference,
            session_authority=authority,
            key="fatal-submitter-command",
        )
        command_id = str(replay["command_id"])
        result = service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
            command_id=command_id,
        )
        record = service._core_sessions[session_id].commands[command_id]
        self.assertTrue(replay["replayed"])
        self.assertEqual(result["command"]["status"], "failed")
        self.assertEqual(
            result["command"]["result"]["reason"],
            "worker_dispatch_failed",
        )
        self.assertEqual(
            result["command"]["result"]["usage"],
            {"state": "settled", "tokens": 0, "spend_units": 0},
        )
        self.assertEqual(record.dispatch_state.value, "abandoned")
        self.assertTrue(record.worker_reconciled)
        self.assertIsNone(service._core_sessions[session_id].active_command_id)
        admission = service.admission.snapshot()
        self.assertEqual(admission.physical_workers, 0)
        self.assertEqual(admission.unsettled_jobs, 0)
        self.assertEqual(admission.settled_jobs, 1)
        self.assertEqual(admission.reserved_tokens, 0)
        self.assertEqual(admission.reserved_spend_units, 0)
        self.assertEqual(service.admission.snapshot().physical_workers, 0)

    def test_submit_changed_body_conflicts_and_distinct_submit_is_busy(self) -> None:
        self._submit(key="claimed")

        self.assertEqual(
            self._error_code(lambda: self._submit(key="claimed", command="id")),
            "idempotency_conflict",
        )
        self.assertEqual(
            self._error_code(lambda: self._submit(key="another")),
            "command_in_progress",
        )
        self.assertEqual(len(self.tasks.tasks), 1)

    def test_retryable_worker_capacity_rejection_leaves_same_key_reusable(
        self,
    ) -> None:
        tasks = ManualTasks()
        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=ScriptedExecutor(),
            admission=ProductAdmissionLedger(
                ProductAdmissionLimits(
                    max_active_sessions=4,
                    max_logical_jobs=8,
                    max_physical_workers=1,
                    token_budget=1_000,
                    spend_budget_units=500,
                )
            ),
        )
        alice = _create(service, key="alice-create")
        bob = _create_bob(service, key="bob-create")
        alice_command = _submit(
            service,
            reference=str(alice["snapshot"]["reference"]),
            session_authority=str(alice["session_authority"]),
            key="alice-command",
        )
        before = service.admission.snapshot()

        code = self._error_code(
            lambda: _submit(
                service,
                actor_authority=BOB_ACTOR,
                reference=str(bob["snapshot"]["reference"]),
                session_authority=str(bob["session_authority"]),
                key="bob-capacity-retry",
            )
        )

        self.assertEqual(code, "capacity_exhausted")
        self.assertEqual(service.admission.snapshot(), before)
        tasks.run_next()
        accepted = _submit(
            service,
            actor_authority=BOB_ACTOR,
            reference=str(bob["snapshot"]["reference"]),
            session_authority=str(bob["session_authority"]),
            key="bob-capacity-retry",
        )
        self.assertEqual(accepted["kind"], "command_accepted")
        self.assertNotEqual(accepted["command_id"], alice_command["command_id"])
        self.assertEqual(len(tasks.tasks), 1)

    def test_unrelated_sessions_execute_concurrently_and_cancel_independently(
        self,
    ) -> None:
        tasks = ManualTasks()
        executor = ConcurrentExecutor()
        service = _service(clock=self.clock, tasks=tasks, executor=executor)
        alice = _create(service, key="alice-concurrent-create")
        bob = _create_bob(service, key="bob-concurrent-create")
        alice_reference = str(alice["snapshot"]["reference"])
        bob_reference = str(bob["snapshot"]["reference"])
        alice_authority = str(alice["session_authority"])
        bob_authority = str(bob["session_authority"])
        alice_command = _submit(
            service,
            reference=alice_reference,
            session_authority=alice_authority,
            key="alice-concurrent-command",
        )
        bob_command = _submit(
            service,
            actor_authority=BOB_ACTOR,
            reference=bob_reference,
            session_authority=bob_authority,
            key="bob-concurrent-command",
        )
        first_task = tasks.tasks.pop(0)
        second_task = tasks.tasks.pop(0)
        workers = (
            threading.Thread(target=first_task),
            threading.Thread(target=second_task),
        )
        for worker in workers:
            worker.start()

        self.assertTrue(executor.both_started.wait(2))
        cancel = service.cancel_command(
            actor_authority=ALICE_ACTOR,
            session_authority=alice_authority,
            reference=alice_reference,
            expected_epoch=service.service_epoch,
            idempotency_key="alice-concurrent-cancel",
            command_id=str(alice_command["command_id"]),
            slot_generation=1,
        )
        bob_while_alice_cancelled = service.command_result(
            actor_authority=BOB_ACTOR,
            session_authority=bob_authority,
            reference=bob_reference,
            expected_epoch=service.service_epoch,
            command_id=str(bob_command["command_id"]),
        )

        self.assertEqual(executor.max_active, 2)
        self.assertEqual(cancel["outcome"], "cancel_requested")
        self.assertFalse(bob_while_alice_cancelled["terminal"])
        executor.release.set()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())

        alice_result = service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=alice_authority,
            reference=alice_reference,
            expected_epoch=service.service_epoch,
            command_id=str(alice_command["command_id"]),
        )
        bob_result = service.command_result(
            actor_authority=BOB_ACTOR,
            session_authority=bob_authority,
            reference=bob_reference,
            expected_epoch=service.service_epoch,
            command_id=str(bob_command["command_id"]),
        )
        self.assertEqual(alice_result["command"]["status"], "failed")
        self.assertEqual(bob_result["command"]["status"], "completed")
        self.assertEqual(service.admission.snapshot().physical_workers, 0)

    def test_classifier_rejection_precedes_command_journal_and_admission(self) -> None:
        before = self._snapshot()

        code = self._error_code(lambda: self._submit(command="cat /etc/passwd | head"))

        self.assertEqual(code, "compound_syntax_unsupported")
        after = self._snapshot()
        self.assertEqual(after["coordinates"], before["coordinates"])
        self.assertEqual(self.service.admission.snapshot().logical_jobs, 0)
        self.assertEqual(self.tasks.tasks, [])

    def test_epoch_and_actor_boundary_precede_command_error_disclosure(self) -> None:
        before = self._snapshot()

        stale = self._error_code(
            lambda: self.service.submit_command(
                actor_authority=ALICE_ACTOR,
                session_authority=self.session_authority,
                reference=self.reference,
                expected_epoch=f"{self.service.service_epoch}-stale",
                idempotency_key="stale-invalid-command",
                expected_slot_generation=0,
                command="id | whoami",
            )
        )
        unauthorized = self._error_code(
            lambda: self.service.submit_command(
                actor_authority="unknown-product-actor-authority",
                session_authority=self.session_authority,
                reference=self.reference,
                expected_epoch=self.service.service_epoch,
                idempotency_key="unauthorized-invalid-command",
                expected_slot_generation=0,
                command="id | whoami",
            )
        )

        self.assertEqual(stale, "outcome_unknown")
        self.assertEqual(unauthorized, "authority_denied")
        self.assertEqual(self._snapshot()["coordinates"], before["coordinates"])
        self.assertEqual(self.service.admission.snapshot().logical_jobs, 0)
        self.assertEqual(self.tasks.tasks, [])

    def test_cancel_before_dispatch_is_replayable_and_never_invokes_executor(
        self,
    ) -> None:
        accepted = self._submit()
        command_id = str(accepted["command_id"])

        cancel = self._cancel(command_id, key="cancel-one")
        cancel_replay = self._cancel(command_id, key="cancel-one")
        self.tasks.run_next()
        late_replay = self._cancel(command_id, key="cancel-one")

        self.assertEqual(cancel["outcome"], "cancel_requested")
        self.assertTrue(cancel_replay["replayed"])
        self.assertTrue(late_replay["replayed"])
        self.assertEqual(late_replay["outcome"], "cancel_requested")
        self.assertEqual(self.executor.requests, [])
        result = self._result(command_id)
        self.assertEqual(result["command"]["status"], "interrupted")
        self.assertEqual(
            result["command"]["result"]["reason"],
            "cancelled_before_dispatch",
        )

    def test_terminal_first_cancel_returns_already_terminal(self) -> None:
        accepted = self._submit()
        command_id = str(accepted["command_id"])
        self.tasks.run_next()

        cancel = self._cancel(command_id)

        self.assertEqual(cancel["outcome"], "already_terminal")
        self.assertEqual(cancel["command_result"]["outcome"], "completed")

    def test_cancel_fence_suppresses_late_completion_and_state(self) -> None:
        provisional = deepcopy(
            self.service._sessions_by_reference[self.reference].runtime_document
        )
        provisional["world_state"]["value"] = 99
        blocking = BlockingExecutor(
            ProductExecutionResult.completed_exited(
                exit_status=0,
                state_effect=ProductStateEffect(
                    ProductStateEffectKind.COMMITTED,
                    provisional_document=provisional,
                ),
            )
        )
        self.service._executor = blocking
        accepted = self._submit()
        command_id = str(accepted["command_id"])
        worker = threading.Thread(target=self.tasks.run_next)
        worker.start()
        self.assertTrue(blocking.started.wait(2))

        cancel = self._cancel(command_id)
        blocking.release.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(cancel["outcome"], "cancel_requested")
        result = self._result(command_id)
        self.assertEqual(result["command"]["status"], "failed")
        self.assertEqual(
            result["command"]["result"]["reason"],
            "cancellation_not_established",
        )
        self.assertEqual(
            self.service._sessions_by_reference[self.reference].runtime_document[
                "world_state"
            ]["value"],
            1,
        )

    def test_cancellation_deadline_terminalizes_but_holds_worker_until_return(
        self,
    ) -> None:
        provisional = deepcopy(
            self.service._sessions_by_reference[self.reference].runtime_document
        )
        provisional["world_state"]["value"] = 77
        blocking = BlockingExecutor(
            ProductExecutionResult.completed_exited(
                exit_status=0,
                state_effect=ProductStateEffect(
                    ProductStateEffectKind.COMMITTED,
                    provisional_document=provisional,
                ),
            )
        )
        self.service._executor = blocking
        accepted = self._submit()
        command_id = str(accepted["command_id"])
        worker = threading.Thread(target=self.tasks.run_next)
        worker.start()
        self.assertTrue(blocking.started.wait(2))

        self._cancel(command_id)
        self.clock.advance(2_000)
        self.assertEqual(self.service.reconcile_due(), 1)

        result = self._result(command_id)
        self.assertEqual(result["command"]["status"], "failed")
        self.assertEqual(
            result["command"]["result"]["reason"],
            "cancellation_deadline_exceeded",
        )
        self.assertEqual(
            result["command"]["result"]["usage"],
            {"state": "pending", "tokens": None, "spend_units": None},
        )
        admission = self.service.admission.snapshot()
        self.assertEqual(admission.physical_workers, 1)
        self.assertEqual(admission.settled_jobs, 0)

        blocking.release.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(self.service.admission.snapshot().physical_workers, 0)
        self.assertEqual(self.service.admission.snapshot().settled_jobs, 1)
        self.assertEqual(
            self.service._sessions_by_reference[self.reference].runtime_document[
                "world_state"
            ]["value"],
            1,
        )
        self.assertEqual(
            self._result(command_id)["command"]["result"]["reason"],
            "cancellation_deadline_exceeded",
        )
        self.assertEqual(
            self._result(command_id)["command"]["result"]["usage"],
            {"state": "settled", "tokens": 0, "spend_units": 0},
        )

    def test_command_deadline_fences_late_completion_and_state(self) -> None:
        provisional = deepcopy(
            self.service._sessions_by_reference[self.reference].runtime_document
        )
        provisional["world_state"]["value"] = 88
        blocking = BlockingExecutor(
            ProductExecutionResult.completed_exited(
                exit_status=0,
                state_effect=ProductStateEffect(
                    ProductStateEffectKind.COMMITTED,
                    provisional_document=provisional,
                ),
            )
        )
        self.service._executor = blocking
        accepted = self._submit()
        command_id = str(accepted["command_id"])
        worker = threading.Thread(target=self.tasks.run_next)
        worker.start()
        self.assertTrue(blocking.started.wait(2))

        self.clock.advance(10_000)
        self.assertEqual(self.service.reconcile_due(), 1)

        result = self._result(command_id)
        self.assertEqual(result["command"]["status"], "failed")
        self.assertEqual(
            result["command"]["result"]["reason"],
            "command_deadline_exceeded",
        )
        self.assertEqual(self.service.admission.snapshot().physical_workers, 1)

        blocking.release.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(self.service.admission.snapshot().physical_workers, 0)
        self.assertEqual(
            self.service._sessions_by_reference[self.reference].runtime_document[
                "world_state"
            ]["value"],
            1,
        )

    def test_command_deadline_is_capped_to_refreshed_idle_expiry(self) -> None:
        tasks = ManualTasks()
        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=ScriptedExecutor(),
            core_policy=ProductCorePolicy(
                max_commands_per_session=8,
                command_deadline_ms=30_000,
                cancellation_deadline_ms=2_000,
                max_output_bytes_per_command=1_024,
                max_output_chunks_per_command=16,
                reserved_tokens_per_command=100,
                reserved_spend_units_per_command=50,
            ),
        )
        created = _create(service, key="idle-capped-deadline-create")
        reference = str(created["snapshot"]["reference"])
        authority = str(created["session_authority"])
        accepted = _submit(
            service,
            reference=reference,
            session_authority=authority,
            key="idle-capped-deadline-command",
        )

        result = service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
            command_id=str(accepted["command_id"]),
        )

        self.assertEqual(result["command"]["deadline_at_ms"], 21_000)
        tasks.run_next()
        self.assertEqual(
            service.admission.snapshot().physical_workers,
            0,
        )

    def test_close_forces_active_fence_and_late_task_releases_worker_once(self) -> None:
        accepted = self._submit()
        command_id = str(accepted["command_id"])

        closed = self.service.close(
            actor_authority=ALICE_ACTOR,
            session_authority=self.session_authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
            idempotency_key="close-core",
            expected_control_revision=1,
        )
        self.tasks.run_next()

        self.assertEqual(closed["snapshot"]["lifecycle"]["state"], "closed")
        self.assertEqual(
            closed["snapshot"]["latest_command"]["status"],
            "failed",
        )
        admission = self.service.admission.snapshot()
        self.assertEqual(admission.active_sessions, 0)
        self.assertEqual(admission.physical_workers, 0)
        self.assertEqual(admission.settled_jobs, 1)
        self.assertEqual(command_id, accepted["command_id"])

    def test_reset_provisions_clean_session_then_atomically_replaces(self) -> None:
        provisional = deepcopy(
            self.service._sessions_by_reference[self.reference].runtime_document
        )
        provisional["world_state"]["value"] = 8
        self.executor.result = ProductExecutionResult.completed_exited(
            exit_status=0,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document=provisional,
            ),
        )
        self._submit()
        self.tasks.run_next()
        old_snapshot = self.service.snapshot(
            actor_authority=ALICE_ACTOR,
            session_authority=self.session_authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
        )["snapshot"]
        old_core = self.service._core_sessions[old_snapshot["session_id"]]
        old_event_cursor = old_core.journal.event_position.to_dict()
        expected_control_revision = old_snapshot["coordinates"]["control_revision"]

        reset = self.service.reset(
            actor_authority=ALICE_ACTOR,
            session_authority=self.session_authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
            idempotency_key="reset-one",
            expected_control_revision=expected_control_revision,
        )
        replay = self.service.reset(
            actor_authority=ALICE_ACTOR,
            session_authority=self.session_authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
            idempotency_key="reset-one",
            expected_control_revision=expected_control_revision,
        )

        new_reference = str(reset["snapshot"]["reference"])
        previous = reset["previous_snapshot"]
        self.assertEqual(reset["kind"], "session_reset")
        self.assertEqual(previous["session_id"], old_snapshot["session_id"])
        self.assertEqual(previous["reference"], old_snapshot["reference"])
        self.assertEqual(
            previous["starting_revision"], old_snapshot["starting_revision"]
        )
        self.assertEqual(previous["launch_binding"], old_snapshot["launch_binding"])
        self.assertEqual(previous["available_controls"], [])
        self.assertEqual(
            previous["command_slot"],
            {
                "state": "terminal",
                "generation": old_snapshot["command_slot"]["generation"],
                "active_command_id": None,
            },
        )
        self.assertEqual(previous["latest_command"], old_snapshot["latest_command"])
        self.assertEqual(previous["transcript"], old_snapshot["transcript"])
        self.assertEqual(previous["lifecycle"]["state"], "replaced")
        self.assertEqual(previous["lifecycle"]["terminal_reason"], "replaced")
        self.assertTrue(previous["recovery"]["tombstone"])
        self.assertEqual(
            previous["recovery"]["retain_until_ms"],
            previous["lifecycle"]["terminal_at_ms"]
            + self.service._policy.tombstone_ttl_ms,
        )
        self.assertEqual(
            previous["coordinates"]["control_revision"],
            expected_control_revision + 1,
        )
        self.assertEqual(reset["snapshot"]["coordinates"]["control_revision"], 1)
        self.assertEqual(
            previous["coordinates"]["command_slot_generation"],
            old_snapshot["coordinates"]["command_slot_generation"],
        )
        self.assertEqual(
            previous["coordinates"]["accepted_state_revision"],
            old_snapshot["coordinates"]["accepted_state_revision"],
        )
        self.assertEqual(
            previous["coordinates"]["transcript"],
            old_snapshot["coordinates"]["transcript"],
        )
        old_journal = old_core.journal.event_snapshot()
        terminal_event = old_journal.events[-1]
        self.assertEqual(old_snapshot["coordinates"]["event"], old_event_cursor)
        self.assertEqual(
            previous["coordinates"]["event"], old_journal.position.to_dict()
        )
        self.assertEqual(
            previous["coordinates"]["event"], terminal_event.cursor.to_dict()
        )
        self.assertEqual(
            previous["coordinates"]["event"]["sequence"],
            old_event_cursor["sequence"] + 1,
        )
        self.assertEqual(terminal_event.event_type, "session.terminal")
        self.assertEqual(terminal_event.criticality.value, "authoritative")
        self.assertEqual(
            dict(terminal_event.data),
            {
                "control_revision": expected_control_revision + 1,
                "terminal_reason": "replaced",
                "slot_generation": old_snapshot["command_slot"]["generation"],
                "accepted_state_revision": old_snapshot["coordinates"][
                    "accepted_state_revision"
                ],
            },
        )
        self.assertNotEqual(new_reference, self.reference)
        self.assertEqual(
            self.service._sessions_by_reference[new_reference].runtime_document[
                "world_state"
            ]["value"],
            1,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["session_authority"], reset["session_authority"])
        self.assertEqual(replay["previous_snapshot"], previous)
        self.assertEqual(replay["snapshot"], reset["snapshot"])
        self.assertEqual(
            self._error_code(
                lambda: self.service.snapshot(
                    actor_authority=ALICE_ACTOR,
                    session_authority=self.session_authority,
                    reference=self.reference,
                    expected_epoch=self.service.service_epoch,
                )
            ),
            "authority_denied",
        )
        self.assertEqual(self.service.admission.snapshot().active_sessions, 1)

    def test_reset_does_not_replenish_consumed_command_allowance(self) -> None:
        for generation in range(8):
            self._submit(
                key=f"quota-command-{generation}",
                expected_slot_generation=generation,
            )
            self.tasks.run_next()

        reset = self.service.reset(
            actor_authority=ALICE_ACTOR,
            session_authority=self.session_authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
            idempotency_key="quota-reset",
            expected_control_revision=1,
        )

        self.assertEqual(
            self._error_code(
                lambda: _submit(
                    self.service,
                    reference=str(reset["snapshot"]["reference"]),
                    session_authority=str(reset["session_authority"]),
                    key="quota-after-reset",
                    expected_slot_generation=0,
                )
            ),
            "command_limit_reached",
        )
        self.assertEqual(self.service.admission.snapshot().active_sessions, 1)
        self.assertEqual(self.tasks.tasks, [])

    def test_replacement_preserves_terminal_capacity_for_live_session(self) -> None:
        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=60_000,
                idle_ttl_ms=30_000,
                tombstone_ttl_ms=10_000,
                failed_operation_ttl_ms=5_000,
                max_active_sessions=1,
                max_tombstones=3,
                max_idempotency_records=16,
                max_launch_grants=8,
                max_actor_authorities=8,
                max_revisions=4,
            ),
        )
        current = _create(service, key="terminal-capacity-create")

        for index in range(2):
            current = service.reset(
                actor_authority=ALICE_ACTOR,
                session_authority=str(current["session_authority"]),
                reference=str(current["snapshot"]["reference"]),
                expected_epoch=service.service_epoch,
                idempotency_key=f"terminal-capacity-reset-{index}",
                expected_control_revision=1,
            )

        authority = str(current["session_authority"])
        reference = str(current["snapshot"]["reference"])
        before_references = set(service._sessions_by_reference)
        self.assertEqual(
            self._error_code(
                lambda: service.reset(
                    actor_authority=ALICE_ACTOR,
                    session_authority=authority,
                    reference=reference,
                    expected_epoch=service.service_epoch,
                    idempotency_key="terminal-capacity-rejected-reset",
                    expected_control_revision=1,
                )
            ),
            "capacity_exhausted",
        )
        self.assertEqual(set(service._sessions_by_reference), before_references)
        self.assertEqual(service.admission.snapshot().active_sessions, 1)

        closed = service.close(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
            idempotency_key="terminal-capacity-close",
            expected_control_revision=1,
        )

        self.assertEqual(closed["snapshot"]["lifecycle"]["state"], "closed")
        self.assertEqual(len(service._tombstones_by_reference), 3)
        self.assertEqual(service.admission.snapshot().active_sessions, 0)

    def test_reset_is_busy_without_provisioning_during_active_command(self) -> None:
        self._submit()
        before_references = set(self.service._sessions_by_reference)

        code = self._error_code(
            lambda: self.service.reset(
                actor_authority=ALICE_ACTOR,
                session_authority=self.session_authority,
                reference=self.reference,
                expected_epoch=self.service.service_epoch,
                idempotency_key="reset-busy",
                expected_control_revision=1,
            )
        )

        self.assertEqual(code, "command_in_progress")
        self.assertEqual(set(self.service._sessions_by_reference), before_references)
        self.assertEqual(self.service.admission.snapshot().active_sessions, 1)

    def test_failed_reset_provisioning_preserves_old_authority_and_state(self) -> None:
        tasks = ManualTasks()

        def reject(_document) -> None:
            raise RuntimeError("synthetic validation failure")

        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=ScriptedExecutor(),
            validate_replacement=reject,
        )
        created = _create(service, key="create-validation")
        reference = str(created["snapshot"]["reference"])
        authority = str(created["session_authority"])

        code = self._error_code(
            lambda: service.reset(
                actor_authority=ALICE_ACTOR,
                session_authority=authority,
                reference=reference,
                expected_epoch=service.service_epoch,
                idempotency_key="reset-fails",
                expected_control_revision=1,
            )
        )

        self.assertEqual(code, "replacement_unavailable")
        snapshot = service.snapshot(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
        )["snapshot"]
        self.assertEqual(snapshot["reference"], reference)
        self.assertEqual(snapshot["lifecycle"]["state"], "active")
        self.assertEqual(service.admission.snapshot().active_sessions, 1)

    def test_slow_replacement_validation_does_not_block_unrelated_session(
        self,
    ) -> None:
        validation_started = threading.Event()
        release_validation = threading.Event()

        def validate(_document) -> None:
            validation_started.set()
            if not release_validation.wait(2):
                raise RuntimeError("replacement validation release timed out")

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
        )
        alice = _create(service, key="slow-reset-alice")
        bob = _create_bob(service, key="slow-reset-bob")
        replacement_result: list[dict[str, object]] = []
        replacement_errors: list[BaseException] = []

        def reset_alice() -> None:
            try:
                replacement_result.append(
                    service.reset(
                        actor_authority=ALICE_ACTOR,
                        session_authority=str(alice["session_authority"]),
                        reference=str(alice["snapshot"]["reference"]),
                        expected_epoch=service.service_epoch,
                        idempotency_key="slow-reset-operation",
                        expected_control_revision=1,
                    )
                )
            except BaseException as error:
                replacement_errors.append(error)

        replacement_thread = threading.Thread(target=reset_alice)
        replacement_thread.start()
        self.assertTrue(validation_started.wait(1))

        bob_read_finished = threading.Event()
        bob_read_errors: list[BaseException] = []

        def read_bob() -> None:
            try:
                service.snapshot(
                    actor_authority=BOB_ACTOR,
                    session_authority=str(bob["session_authority"]),
                    reference=str(bob["snapshot"]["reference"]),
                    expected_epoch=service.service_epoch,
                )
            except BaseException as error:
                bob_read_errors.append(error)
            finally:
                bob_read_finished.set()

        bob_thread = threading.Thread(target=read_bob)
        bob_thread.start()
        self.assertTrue(bob_read_finished.wait(1))
        self.assertEqual(bob_read_errors, [])

        release_validation.set()
        replacement_thread.join(2)
        bob_thread.join(2)

        self.assertFalse(replacement_thread.is_alive())
        self.assertEqual(replacement_errors, [])
        self.assertEqual(len(replacement_result), 1)
        self.assertEqual(replacement_result[0]["kind"], "session_reset")

    def test_replacement_rechecks_foreground_slot_after_detached_validation(
        self,
    ) -> None:
        validation_started = threading.Event()
        release_validation = threading.Event()

        def validate(_document) -> None:
            validation_started.set()
            if not release_validation.wait(2):
                raise RuntimeError("replacement validation release timed out")

        tasks = ManualTasks()
        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=ScriptedExecutor(),
            validate_replacement=validate,
        )
        created = _create(service, key="replacement-final-cas-create")
        authority = str(created["session_authority"])
        reference = str(created["snapshot"]["reference"])
        reset_errors: list[str] = []

        def reset() -> None:
            try:
                service.reset(
                    actor_authority=ALICE_ACTOR,
                    session_authority=authority,
                    reference=reference,
                    expected_epoch=service.service_epoch,
                    idempotency_key="replacement-final-cas-reset",
                    expected_control_revision=1,
                )
            except ProductLifecycleError as error:
                reset_errors.append(error.code)

        thread = threading.Thread(target=reset)
        thread.start()
        self.assertTrue(validation_started.wait(1))
        accepted = _submit(
            service,
            reference=reference,
            session_authority=authority,
            key="replacement-final-cas-command",
        )
        release_validation.set()
        thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(reset_errors, ["command_in_progress"])
        snapshot = service.snapshot(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
        )["snapshot"]
        self.assertEqual(
            snapshot["command_slot"]["active_command_id"],
            accepted["command_id"],
        )
        self.assertEqual(len(service._sessions_by_reference), 1)

    def test_replacement_claim_rejects_later_changed_body_for_same_key(self) -> None:
        validation_started = threading.Event()
        release_validation = threading.Event()

        def validate(_document) -> None:
            validation_started.set()
            if not release_validation.wait(2):
                raise RuntimeError("test validation release timed out")

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
        )
        created = _create(service, key="changed-body-claim-create")
        authority = str(created["session_authority"])
        reference = str(created["snapshot"]["reference"])
        replacement_results: list[dict[str, object]] = []
        replacement_errors: list[str] = []

        def replace() -> None:
            try:
                replacement_results.append(
                    service.reset(
                        actor_authority=ALICE_ACTOR,
                        session_authority=authority,
                        reference=reference,
                        expected_epoch=service.service_epoch,
                        idempotency_key="changed-body-claim",
                        expected_control_revision=1,
                    )
                )
            except ProductLifecycleError as error:
                replacement_errors.append(error.code)

        thread = threading.Thread(target=replace)
        thread.start()
        self.assertTrue(validation_started.wait(1))
        try:
            with self.assertRaises(ProductLifecycleError) as captured:
                service.reset(
                    actor_authority=ALICE_ACTOR,
                    session_authority="wrong-session-authority-000001",
                    reference=reference,
                    expected_epoch=service.service_epoch,
                    idempotency_key="changed-body-claim",
                    expected_control_revision=1,
                )
            self.assertEqual(captured.exception.code, "idempotency_conflict")
        finally:
            release_validation.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(replacement_errors, [])
        self.assertEqual(len(replacement_results), 1)
        self.assertEqual(replacement_results[0]["kind"], "session_reset")
        self.assertEqual(service._replacement_claims, {})

    def test_exact_concurrent_replacement_waits_and_replays_once(self) -> None:
        validation_started = threading.Event()
        release_validation = threading.Event()
        validation_lock = threading.Lock()
        validation_count = 0

        def validate(_document) -> None:
            nonlocal validation_count
            with validation_lock:
                validation_count += 1
            validation_started.set()
            if not release_validation.wait(2):
                raise RuntimeError("test validation release timed out")

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
        )
        created = _create(service, key="exact-wait-create")
        authority = str(created["session_authority"])
        reference = str(created["snapshot"]["reference"])
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []
        second_done = threading.Event()

        def replace(*, second: bool) -> None:
            try:
                results.append(
                    service.reset(
                        actor_authority=ALICE_ACTOR,
                        session_authority=authority,
                        reference=reference,
                        expected_epoch=service.service_epoch,
                        idempotency_key="exact-wait-reset",
                        expected_control_revision=1,
                    )
                )
            except BaseException as error:
                errors.append(error)
            finally:
                if second:
                    second_done.set()

        owner = threading.Thread(target=replace, kwargs={"second": False})
        waiter = threading.Thread(target=replace, kwargs={"second": True})
        owner.start()
        self.assertTrue(validation_started.wait(1))
        waiter.start()
        try:
            self.assertFalse(second_done.wait(0.05))
        finally:
            release_validation.set()
            owner.join(2)
            waiter.join(2)

        self.assertFalse(owner.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(validation_count, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(
            {bool(result["replayed"]) for result in results}, {False, True}
        )
        self.assertEqual(
            {str(result["session_authority"]) for result in results},
            {str(results[0]["session_authority"])},
        )
        self.assertEqual(service._replacement_claims, {})

    def test_replacement_rechecks_actor_expiry_after_detached_validation(self) -> None:
        def validate(_document) -> None:
            self.clock.advance(100_000)

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=60_000,
                idle_ttl_ms=30_000,
                tombstone_ttl_ms=10_000,
                failed_operation_ttl_ms=5_000,
                max_active_sessions=1,
                max_tombstones=2,
                max_idempotency_records=3,
                max_launch_grants=8,
                max_actor_authorities=8,
                max_revisions=4,
            ),
        )
        created = _create(service, key="replacement-actor-expiry-create")

        with self.assertRaises(ProductLifecycleError) as captured:
            service.reset(
                actor_authority=ALICE_ACTOR,
                session_authority=str(created["session_authority"]),
                reference=str(created["snapshot"]["reference"]),
                expected_epoch=service.service_epoch,
                idempotency_key="replacement-actor-expiry-reset",
                expected_control_revision=1,
            )

        self.assertEqual(captured.exception.code, "authority_denied")
        self.assertEqual(service._replacement_claims, {})
        self._assert_capacity_invariants(service)
        service.expire_due()
        self._assert_capacity_invariants(service)

    def test_replacement_rechecks_expiry_after_detached_validation(self) -> None:
        def validate(_document) -> None:
            self.clock.advance(25_000)

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=60_000,
                idle_ttl_ms=30_000,
                tombstone_ttl_ms=10_000,
                failed_operation_ttl_ms=5_000,
                max_active_sessions=1,
                max_tombstones=2,
                max_idempotency_records=3,
                max_launch_grants=8,
                max_actor_authorities=8,
                max_revisions=4,
            ),
        )
        created = _create(service, key="replacement-expiry-create")
        authority = str(created["session_authority"])
        reference = str(created["snapshot"]["reference"])

        with self.assertRaises(ProductLifecycleError) as captured:
            service.reset(
                actor_authority=ALICE_ACTOR,
                session_authority=authority,
                reference=reference,
                expected_epoch=service.service_epoch,
                idempotency_key="replacement-expiry-reset",
                expected_control_revision=1,
            )

        self.assertEqual(captured.exception.code, "authority_denied")
        current = service.current(
            actor_authority=ALICE_ACTOR,
            expected_epoch=service.service_epoch,
        )
        self.assertEqual(current["kind"], "current_session_terminal")
        self.assertEqual(
            current["snapshot"]["lifecycle"]["terminal_reason"],
            "idle_expired",
        )
        self.assertEqual(service.admission.snapshot().active_sessions, 0)
        self.assertEqual(service._replacement_claims, {})
        self._assert_capacity_invariants(service)

    def test_replacement_uses_fresh_time_projection_after_validation(self) -> None:
        def validate(_document) -> None:
            self.clock.advance(1_000)

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=60_000,
                idle_ttl_ms=30_000,
                tombstone_ttl_ms=10_000,
                failed_operation_ttl_ms=5_000,
                max_active_sessions=1,
                max_tombstones=2,
                max_idempotency_records=3,
                max_launch_grants=8,
                max_actor_authorities=8,
                max_revisions=4,
            ),
        )
        created = _create(service, key="replacement-fresh-time-create")

        reset = service.reset(
            actor_authority=ALICE_ACTOR,
            session_authority=str(created["session_authority"]),
            reference=str(created["snapshot"]["reference"]),
            expected_epoch=service.service_epoch,
            idempotency_key="replacement-fresh-time-reset",
            expected_control_revision=1,
        )

        self.assertEqual(reset["snapshot"]["lifecycle"]["created_at_ms"], 2_000)
        self.assertEqual(
            reset["previous_snapshot"]["lifecycle"]["terminal_at_ms"],
            2_000,
        )
        self.assertEqual(service._replacement_claims, {})

    def test_switch_rechecks_grant_expiry_after_detached_validation(self) -> None:
        def validate(_document) -> None:
            self.clock.advance(100)

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=60_000,
                idle_ttl_ms=30_000,
                tombstone_ttl_ms=10_000,
                failed_operation_ttl_ms=5_000,
                max_active_sessions=1,
                max_tombstones=2,
                max_idempotency_records=3,
                max_launch_grants=8,
                max_actor_authorities=8,
                max_revisions=4,
            ),
        )
        created = _create(service, key="switch-expiry-create")
        authority = str(created["session_authority"])
        reference = str(created["snapshot"]["reference"])
        switch_grant = next(
            grant
            for grant in service._launch_grants.values()
            if ProductOperation.SWITCH in grant.allowed_operations
        )
        switch_grant.expires_at_ms = self.clock() + 50
        switch_grant.expires_deadline_ms = self.clock() + 50

        with self.assertRaises(ProductLifecycleError) as captured:
            service.switch(
                actor_authority=ALICE_ACTOR,
                session_authority=authority,
                reference=reference,
                expected_epoch=service.service_epoch,
                idempotency_key="switch-expiry-operation",
                expected_control_revision=1,
                launch_grant=ALICE_SWITCH_GRANT,
            )

        self.assertEqual(captured.exception.code, "authority_denied")
        snapshot = service.snapshot(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
        )["snapshot"]
        self.assertEqual(snapshot["reference"], reference)
        self.assertEqual(snapshot["lifecycle"]["state"], "active")
        self.assertIsNone(switch_grant.consumed_session_id)
        self.assertEqual(service._replacement_claims, {})
        self._assert_capacity_invariants(service)

    def test_claim_reservation_prevents_post_validation_capacity_theft(
        self,
    ) -> None:
        validation_started = threading.Event()
        release_validation = threading.Event()

        def validate(_document) -> None:
            validation_started.set()
            if not release_validation.wait(2):
                raise RuntimeError("test validation release timed out")

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=60_000,
                idle_ttl_ms=30_000,
                tombstone_ttl_ms=10_000,
                failed_operation_ttl_ms=5_000,
                max_active_sessions=1,
                max_tombstones=4,
                max_idempotency_records=4,
                max_launch_grants=8,
                max_actor_authorities=8,
                max_revisions=4,
            ),
        )
        created = _create(service, key="transient-claim-create")
        authority = str(created["session_authority"])
        reference = str(created["snapshot"]["reference"])
        reset_results: list[dict[str, object]] = []
        reset_errors: list[str] = []

        def replace() -> None:
            try:
                reset_results.append(
                    service.reset(
                        actor_authority=ALICE_ACTOR,
                        session_authority=authority,
                        reference=reference,
                        expected_epoch=service.service_epoch,
                        idempotency_key="reserved-claim-reset",
                        expected_control_revision=1,
                    )
                )
            except ProductLifecycleError as error:
                reset_errors.append(error.code)

        thread = threading.Thread(target=replace)
        thread.start()
        self.assertTrue(validation_started.wait(1))
        filler_errors: list[str] = []
        for index in range(2):
            with self.assertRaises(ProductLifecycleError) as captured:
                service.submit_command(
                    actor_authority=ALICE_ACTOR,
                    session_authority="wrong-session-authority-000001",
                    reference=reference,
                    expected_epoch=service.service_epoch,
                    idempotency_key=f"capacity-filler-{index}",
                    expected_slot_generation=0,
                    command="id",
                )
            filler_errors.append(captured.exception.code)
            self._assert_capacity_invariants(service)
        release_validation.set()
        thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(filler_errors, ["authority_denied", "capacity_exhausted"])
        self.assertEqual(reset_errors, [])
        self.assertEqual(len(reset_results), 1)
        self.assertEqual(service._replacement_claims, {})
        replay = service.reset(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
            idempotency_key="reserved-claim-reset",
            expected_control_revision=1,
        )
        self.assertEqual(replay["kind"], "session_reset")
        self.assertTrue(replay["replayed"])
        self._assert_capacity_invariants(service)

    def test_transient_post_validation_capacity_releases_claim_for_retry(
        self,
    ) -> None:
        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
        )
        created = _create(service, key="transient-final-capacity-create")
        original_capacity = service._require_replacement_capacity_locked
        reject_final = True

        def capacity(*, additional_claims: int) -> None:
            nonlocal reject_final
            if additional_claims == 0 and reject_final:
                reject_final = False
                raise ProductLifecycleError(
                    code="capacity_exhausted",
                    message="Product lifecycle capacity is temporarily exhausted.",
                    status=503,
                    retry="after_delay",
                    retry_after_ms=1,
                )
            original_capacity(additional_claims=additional_claims)

        service._require_replacement_capacity_locked = capacity  # type: ignore[method-assign]
        with self.assertRaises(ProductLifecycleError) as captured:
            service.reset(
                actor_authority=ALICE_ACTOR,
                session_authority=str(created["session_authority"]),
                reference=str(created["snapshot"]["reference"]),
                expected_epoch=service.service_epoch,
                idempotency_key="transient-final-capacity-reset",
                expected_control_revision=1,
            )

        self.assertEqual(captured.exception.code, "capacity_exhausted")
        self.assertEqual(service._replacement_claims, {})
        retried = service.reset(
            actor_authority=ALICE_ACTOR,
            session_authority=str(created["session_authority"]),
            reference=str(created["snapshot"]["reference"]),
            expected_epoch=service.service_epoch,
            idempotency_key="transient-final-capacity-reset",
            expected_control_revision=1,
        )
        self.assertEqual(retried["kind"], "session_reset")
        self.assertFalse(retried["replayed"])

    def test_replacement_claim_prevents_same_thread_reentrant_deadlock(self) -> None:
        holder: dict[str, object] = {}
        nested_errors: list[str] = []

        def validate(_document) -> None:
            service = holder["service"]
            assert isinstance(service, ProductSessionCoreService)
            try:
                service.reset(
                    actor_authority=ALICE_ACTOR,
                    session_authority=str(holder["authority"]),
                    reference=str(holder["reference"]),
                    expected_epoch=service.service_epoch,
                    idempotency_key="reentrant-reset",
                    expected_control_revision=1,
                )
            except RuntimeError as error:
                nested_errors.append(str(error))

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
        )
        created = _create(service, key="reentrant-create")
        holder.update(
            service=service,
            authority=str(created["session_authority"]),
            reference=str(created["snapshot"]["reference"]),
        )

        result = service.reset(
            actor_authority=ALICE_ACTOR,
            session_authority=str(holder["authority"]),
            reference=str(holder["reference"]),
            expected_epoch=service.service_epoch,
            idempotency_key="reentrant-reset",
            expected_control_revision=1,
        )

        self.assertEqual(result["kind"], "session_reset")
        self.assertEqual(
            nested_errors,
            ["replacement validation cannot re-enter its own idempotency scope"],
        )
        self.assertEqual(service._replacement_claims, {})

    def test_replacement_owner_base_exception_releases_and_notifies_waiter(
        self,
    ) -> None:
        class FatalValidation(BaseException):
            pass

        validation_started = threading.Event()
        release_validation = threading.Event()
        validation_lock = threading.Lock()
        validation_count = 0

        def validate(_document) -> None:
            nonlocal validation_count
            with validation_lock:
                validation_count += 1
                attempt = validation_count
            if attempt == 1:
                validation_started.set()
                if not release_validation.wait(2):
                    raise RuntimeError("test validation release timed out")
                raise FatalValidation()

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
        )
        created = _create(service, key="base-exception-create")
        authority = str(created["session_authority"])
        reference = str(created["snapshot"]["reference"])
        owner_errors: list[str] = []
        waiter_results: list[dict[str, object]] = []
        waiter_errors: list[BaseException] = []

        def owner() -> None:
            try:
                service.reset(
                    actor_authority=ALICE_ACTOR,
                    session_authority=authority,
                    reference=reference,
                    expected_epoch=service.service_epoch,
                    idempotency_key="base-exception-reset",
                    expected_control_revision=1,
                )
            except FatalValidation:
                owner_errors.append("fatal_validation")

        def waiter() -> None:
            try:
                waiter_results.append(
                    service.reset(
                        actor_authority=ALICE_ACTOR,
                        session_authority=authority,
                        reference=reference,
                        expected_epoch=service.service_epoch,
                        idempotency_key="base-exception-reset",
                        expected_control_revision=1,
                    )
                )
            except BaseException as error:
                waiter_errors.append(error)

        owner_thread = threading.Thread(target=owner)
        waiter_thread = threading.Thread(target=waiter)
        owner_thread.start()
        self.assertTrue(validation_started.wait(1))
        waiter_thread.start()
        release_validation.set()
        owner_thread.join(2)
        waiter_thread.join(2)

        self.assertFalse(owner_thread.is_alive())
        self.assertFalse(waiter_thread.is_alive())
        self.assertEqual(owner_errors, ["fatal_validation"])
        self.assertEqual(waiter_errors, [])
        self.assertEqual(validation_count, 2)
        self.assertEqual(len(waiter_results), 1)
        self.assertEqual(waiter_results[0]["kind"], "session_reset")
        self.assertEqual(service._replacement_claims, {})

    def test_two_replacement_claims_reserve_global_idempotency_capacity(
        self,
    ) -> None:
        both_validating = threading.Event()
        release_validation = threading.Event()
        validation_lock = threading.Lock()
        validation_count = 0

        def validate(_document) -> None:
            nonlocal validation_count
            with validation_lock:
                validation_count += 1
                if validation_count == 2:
                    both_validating.set()
            if not release_validation.wait(2):
                raise RuntimeError("test validation release timed out")

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=60_000,
                idle_ttl_ms=30_000,
                tombstone_ttl_ms=10_000,
                failed_operation_ttl_ms=5_000,
                max_active_sessions=2,
                max_tombstones=4,
                max_idempotency_records=6,
                max_launch_grants=8,
                max_actor_authorities=8,
                max_revisions=4,
            ),
        )
        alice = _create(service, key="two-claims-alice-create")
        bob = _create_bob(service, key="two-claims-bob-create")
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def reset_actor(
            actor_authority: str,
            created: dict[str, object],
            key: str,
        ) -> None:
            try:
                results.append(
                    service.reset(
                        actor_authority=actor_authority,
                        session_authority=str(created["session_authority"]),
                        reference=str(created["snapshot"]["reference"]),
                        expected_epoch=service.service_epoch,
                        idempotency_key=key,
                        expected_control_revision=1,
                    )
                )
            except BaseException as error:
                errors.append(error)

        alice_thread = threading.Thread(
            target=reset_actor,
            args=(ALICE_ACTOR, alice, "two-claims-alice-reset"),
        )
        bob_thread = threading.Thread(
            target=reset_actor,
            args=(BOB_ACTOR, bob, "two-claims-bob-reset"),
        )
        alice_thread.start()
        bob_thread.start()
        self.assertTrue(both_validating.wait(1))
        self._assert_capacity_invariants(service)
        before_records = len(service._idempotency)

        operations = (
            lambda: service.create(
                actor_authority=ALICE_ACTOR,
                launch_grant="wrong-product-launch-grant-000001",
                expected_epoch=service.service_epoch,
                idempotency_key="claim-blocked-create-failure",
            ),
            lambda: service.submit_command(
                actor_authority=ALICE_ACTOR,
                session_authority="wrong-session-authority-000001",
                reference=str(alice["snapshot"]["reference"]),
                expected_epoch=service.service_epoch,
                idempotency_key="claim-blocked-submit-failure",
                expected_slot_generation=0,
                command="id",
            ),
            lambda: service.cancel_command(
                actor_authority=ALICE_ACTOR,
                session_authority="wrong-session-authority-000001",
                reference=str(alice["snapshot"]["reference"]),
                expected_epoch=service.service_epoch,
                idempotency_key="claim-blocked-cancel-failure",
                command_id="unknown-command-id",
                slot_generation=1,
            ),
            lambda: service.close(
                actor_authority=ALICE_ACTOR,
                session_authority="wrong-session-authority-000001",
                reference=str(alice["snapshot"]["reference"]),
                expected_epoch=service.service_epoch,
                idempotency_key="claim-blocked-close-failure",
                expected_control_revision=1,
            ),
        )
        try:
            for operation in operations:
                with self.assertRaises(ProductLifecycleError) as captured:
                    operation()
                self.assertEqual(captured.exception.code, "capacity_exhausted")
                self.assertEqual(len(service._idempotency), before_records)
                self._assert_capacity_invariants(service)
        finally:
            release_validation.set()
            alice_thread.join(2)
            bob_thread.join(2)

        self.assertFalse(alice_thread.is_alive())
        self.assertFalse(bob_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(service._replacement_claims, {})
        self._assert_capacity_invariants(service)

    def test_inflight_replacement_claim_reserves_terminal_capacity(self) -> None:
        validation_started = threading.Event()
        release_validation = threading.Event()

        def validate(_document) -> None:
            validation_started.set()
            if not release_validation.wait(2):
                raise RuntimeError("test validation release timed out")

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=60_000,
                idle_ttl_ms=30_000,
                tombstone_ttl_ms=10_000,
                failed_operation_ttl_ms=5_000,
                max_active_sessions=2,
                max_tombstones=3,
                max_idempotency_records=16,
                max_launch_grants=8,
                max_actor_authorities=8,
                max_revisions=4,
            ),
        )
        alice = _create(service, key="claim-capacity-alice")
        bob = _create_bob(service, key="claim-capacity-bob")
        alice_results: list[dict[str, object]] = []

        def replace_alice() -> None:
            alice_results.append(
                service.reset(
                    actor_authority=ALICE_ACTOR,
                    session_authority=str(alice["session_authority"]),
                    reference=str(alice["snapshot"]["reference"]),
                    expected_epoch=service.service_epoch,
                    idempotency_key="claim-capacity-alice-reset",
                    expected_control_revision=1,
                )
            )

        thread = threading.Thread(target=replace_alice)
        thread.start()
        self.assertTrue(validation_started.wait(1))
        try:
            with self.assertRaises(ProductLifecycleError) as captured:
                service.reset(
                    actor_authority=BOB_ACTOR,
                    session_authority=str(bob["session_authority"]),
                    reference=str(bob["snapshot"]["reference"]),
                    expected_epoch=service.service_epoch,
                    idempotency_key="claim-capacity-bob-reset",
                    expected_control_revision=1,
                )
            self.assertEqual(captured.exception.code, "capacity_exhausted")
        finally:
            release_validation.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(alice_results), 1)
        self.assertEqual(alice_results[0]["kind"], "session_reset")
        self.assertEqual(service.admission.snapshot().active_sessions, 2)
        self.assertEqual(service._replacement_claims, {})

    def test_inflight_replacement_claim_blocks_new_session_terminal_slot(
        self,
    ) -> None:
        validation_started = threading.Event()
        release_validation = threading.Event()

        def validate(_document) -> None:
            validation_started.set()
            if not release_validation.wait(2):
                raise RuntimeError("test validation release timed out")

        service = _service(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=ScriptedExecutor(),
            validate_replacement=validate,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=60_000,
                idle_ttl_ms=30_000,
                tombstone_ttl_ms=10_000,
                failed_operation_ttl_ms=5_000,
                max_active_sessions=2,
                max_tombstones=2,
                max_idempotency_records=8,
                max_launch_grants=8,
                max_actor_authorities=8,
                max_revisions=4,
            ),
        )
        alice = _create(service, key="claim-terminal-slot-alice")
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def reset_alice() -> None:
            try:
                results.append(
                    service.reset(
                        actor_authority=ALICE_ACTOR,
                        session_authority=str(alice["session_authority"]),
                        reference=str(alice["snapshot"]["reference"]),
                        expected_epoch=service.service_epoch,
                        idempotency_key="claim-terminal-slot-reset",
                        expected_control_revision=1,
                    )
                )
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=reset_alice)
        thread.start()
        self.assertTrue(validation_started.wait(1))
        try:
            self._assert_capacity_invariants(service)
            with self.assertRaises(ProductLifecycleError) as captured:
                _create_bob(service, key="claim-terminal-slot-bob")
            self.assertEqual(captured.exception.code, "capacity_exhausted")
            self.assertEqual(len(service._sessions_by_reference), 1)
            self._assert_capacity_invariants(service)
        finally:
            release_validation.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(service._replacement_claims, {})
        self._assert_capacity_invariants(service)

    def test_switch_consumes_exact_switch_grant_and_starts_target_revision(
        self,
    ) -> None:
        switched = self.service.switch(
            actor_authority=ALICE_ACTOR,
            session_authority=self.session_authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
            idempotency_key="switch-one",
            expected_control_revision=1,
            launch_grant=ALICE_SWITCH_GRANT,
        )
        replay = self.service.switch(
            actor_authority=ALICE_ACTOR,
            session_authority=self.session_authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
            idempotency_key="switch-one",
            expected_control_revision=1,
            launch_grant=ALICE_SWITCH_GRANT,
        )

        self.assertEqual(switched["kind"], "session_switched")
        self.assertEqual(
            switched["snapshot"]["starting_revision"]["revision_id"],
            "revision-b",
        )
        self.assertEqual(
            self.service._sessions_by_reference[
                switched["snapshot"]["reference"]
            ].runtime_document["world_state"]["value"],
            10,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["session_authority"], switched["session_authority"])

    def test_event_cursor_eviction_requires_snapshot(self) -> None:
        tasks = ManualTasks()
        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=ScriptedExecutor(),
            journal_limits=JournalLimits(
                max_events=2,
                max_event_bytes=4096,
                max_event_payload_bytes=2048,
                max_transcript_entries=16,
                max_transcript_bytes=4096,
                max_transcript_entry_bytes=1024,
            ),
        )
        created = _create(service, key="small-journal")
        reference = str(created["snapshot"]["reference"])
        authority = str(created["session_authority"])
        initial = created["snapshot"]["coordinates"]["event"]
        accepted = _submit(
            service,
            reference=reference,
            session_authority=authority,
        )
        tasks.run_next()

        code = self._error_code(
            lambda: service.events(
                actor_authority=ALICE_ACTOR,
                session_authority=authority,
                reference=reference,
                expected_epoch=service.service_epoch,
                after=EventCursor(
                    str(initial["stream_id"]),
                    int(initial["sequence"]),
                    str(initial["event_id"]),
                ),
            )
        )

        self.assertEqual(code, "snapshot_required")
        self.assertEqual(accepted["slot_generation"], 1)

    def test_core_rejects_journal_limits_below_mandatory_event_shape(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "cannot represent mandatory product-core events",
        ):
            _service(
                clock=self.clock,
                tasks=ManualTasks(),
                executor=ScriptedExecutor(),
                journal_limits=JournalLimits(
                    max_event_payload_bytes=511,
                ),
            )

    def test_minimum_supported_event_payload_handles_state_commit(self) -> None:
        tasks = ManualTasks()
        executor = ScriptedExecutor()
        service = _service(
            clock=self.clock,
            tasks=tasks,
            executor=executor,
            journal_limits=JournalLimits(
                max_events=32,
                max_event_bytes=16 * 1024,
                max_event_payload_bytes=512,
                max_payload_depth=2,
                max_payload_items=16,
                max_transcript_entries=32,
                max_transcript_bytes=16 * 1024,
                max_transcript_entry_bytes=2 * 1024,
            ),
        )
        created = _create(service, key="minimum-journal-create")
        reference = str(created["snapshot"]["reference"])
        authority = str(created["session_authority"])
        provisional = deepcopy(
            service._sessions_by_reference[reference].runtime_document
        )
        provisional["world_state"]["value"] = 12
        executor.result = ProductExecutionResult.completed_exited(
            exit_status=0,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document=provisional,
            ),
        )
        accepted = _submit(
            service,
            reference=reference,
            session_authority=authority,
            key="minimum-journal-command",
        )

        tasks.run_next()

        result = service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=authority,
            reference=reference,
            expected_epoch=service.service_epoch,
            command_id=str(accepted["command_id"]),
        )
        self.assertEqual(result["command"]["status"], "completed")
        self.assertEqual(
            result["command"]["result"]["state_effect"],
            {"kind": "committed", "accepted_state_revision": 1},
        )

    def _submit(
        self,
        *,
        key: str = "submit-one",
        command: str = "  uname -a\t",
        expected_slot_generation: int = 0,
    ) -> dict[str, object]:
        return _submit(
            self.service,
            reference=self.reference,
            session_authority=self.session_authority,
            key=key,
            command=command,
            expected_slot_generation=expected_slot_generation,
        )

    def _cancel(
        self,
        command_id: str,
        *,
        key: str = "cancel-command",
    ) -> dict[str, object]:
        return self.service.cancel_command(
            actor_authority=ALICE_ACTOR,
            session_authority=self.session_authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
            idempotency_key=key,
            command_id=command_id,
            slot_generation=1,
        )

    def _result(self, command_id: str) -> dict[str, object]:
        return self.service.command_result(
            actor_authority=ALICE_ACTOR,
            session_authority=self.session_authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
            command_id=command_id,
        )

    def _snapshot(self) -> dict[str, object]:
        return self.service.snapshot(
            actor_authority=ALICE_ACTOR,
            session_authority=self.session_authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
        )["snapshot"]

    def _transcript(self) -> dict[str, object]:
        return self.service.transcript(
            actor_authority=ALICE_ACTOR,
            session_authority=self.session_authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
        )

    def _error_code(self, operation: Callable[[], object]) -> str:
        with self.assertRaises(ProductLifecycleError) as captured:
            operation()
        return captured.exception.code

    def _assert_capacity_invariants(
        self,
        service: ProductSessionCoreService,
    ) -> None:
        with service._lock:
            active = len(service._sessions_by_reference)
            claims = len(service._replacement_claims)
            self.assertLessEqual(
                len(service._idempotency) + claims + active,
                service._policy.max_idempotency_records,
            )
            self.assertLessEqual(
                len(service._tombstones_by_reference) + active + claims,
                service._policy.max_tombstones,
            )


def _service(
    *,
    clock: FakeClock,
    tasks: Callable[[Callable[[], None]], None],
    executor,
    journal_limits: JournalLimits | None = None,
    validate_replacement=None,
    admission: ProductAdmissionLedger | None = None,
    core_policy: ProductCorePolicy | None = None,
    validate_state_effect=None,
    lifecycle_policy: LifecyclePolicy | None = None,
) -> ProductSessionCoreService:
    return ProductSessionCoreService(
        revisions=(
            ImmutableRevisionSeed(
                revision_id="revision-a",
                document={
                    "profile": {"name": "core-a"},
                    "world_state": {"value": 1},
                },
            ),
            ImmutableRevisionSeed(
                revision_id="revision-b",
                document={
                    "profile": {"name": "core-b"},
                    "world_state": {"value": 10},
                },
            ),
        ),
        actors=(
            ActorAuthoritySeed(
                token=ALICE_ACTOR,
                actor_id="alice",
                role=ProductRole.ATTENDEE,
                expires_at_ms=100_000,
            ),
            ActorAuthoritySeed(
                token=BOB_ACTOR,
                actor_id="bob",
                role=ProductRole.ATTENDEE,
                expires_at_ms=100_000,
            ),
        ),
        launch_grants=(
            LaunchGrantSeed(
                token=ALICE_GRANT,
                selected_record_id="selection-a",
                actor_id="alice",
                role=ProductRole.ATTENDEE,
                revision_id="revision-a",
                expires_at_ms=100_000,
                allowed_operations=frozenset({ProductOperation.CREATE}),
                limits={
                    "absolute_ttl_ms": 50_000,
                    "idle_ttl_ms": 20_000,
                    "max_commands": 8,
                    "max_command_bytes": 1024,
                    "command_reserved_tokens": 100,
                    "command_reserved_spend_units": 50,
                },
            ),
            LaunchGrantSeed(
                token=ALICE_SWITCH_GRANT,
                selected_record_id="selection-b-switch",
                actor_id="alice",
                role=ProductRole.ATTENDEE,
                revision_id="revision-b",
                expires_at_ms=100_000,
                allowed_operations=frozenset({ProductOperation.SWITCH}),
                limits={
                    "absolute_ttl_ms": 40_000,
                    "idle_ttl_ms": 15_000,
                    "max_commands": 8,
                },
            ),
            LaunchGrantSeed(
                token=BOB_GRANT,
                selected_record_id="selection-b",
                actor_id="bob",
                role=ProductRole.ATTENDEE,
                revision_id="revision-b",
                expires_at_ms=100_000,
                allowed_operations=frozenset({ProductOperation.CREATE}),
                limits={
                    "absolute_ttl_ms": 50_000,
                    "idle_ttl_ms": 20_000,
                },
            ),
        ),
        executor=executor,
        submit_task=tasks,
        lifecycle_policy=lifecycle_policy
        or LifecyclePolicy(
            absolute_ttl_ms=60_000,
            idle_ttl_ms=30_000,
            tombstone_ttl_ms=10_000,
            failed_operation_ttl_ms=5_000,
            max_active_sessions=4,
            max_tombstones=8,
            max_idempotency_records=64,
            max_launch_grants=8,
            max_actor_authorities=8,
            max_revisions=4,
        ),
        core_policy=core_policy
        or ProductCorePolicy(
            max_commands_per_session=8,
            command_deadline_ms=10_000,
            cancellation_deadline_ms=2_000,
            max_output_bytes_per_command=1024,
            max_output_chunks_per_command=16,
            reserved_tokens_per_command=100,
            reserved_spend_units_per_command=50,
        ),
        journal_limits=journal_limits,
        admission=admission or ProductAdmissionLedger(),
        clock_ms=clock,
        monotonic_ms=clock,
        token_factory=SequenceTokens(),
        validate_replacement=validate_replacement,
        validate_state_effect=validate_state_effect,
    )


def _create(
    service: ProductSessionCoreService,
    *,
    key: str = "create-core",
) -> dict[str, object]:
    return service.create(
        actor_authority=ALICE_ACTOR,
        launch_grant=ALICE_GRANT,
        expected_epoch=service.service_epoch,
        idempotency_key=key,
    )


def _create_bob(
    service: ProductSessionCoreService,
    *,
    key: str = "create-bob-core",
) -> dict[str, object]:
    return service.create(
        actor_authority=BOB_ACTOR,
        launch_grant=BOB_GRANT,
        expected_epoch=service.service_epoch,
        idempotency_key=key,
    )


def _submit(
    service: ProductSessionCoreService,
    *,
    actor_authority: str = ALICE_ACTOR,
    reference: str,
    session_authority: str,
    key: str = "submit-core",
    command: str = "uname -a",
    expected_slot_generation: int = 0,
) -> dict[str, object]:
    return service.submit_command(
        actor_authority=actor_authority,
        session_authority=session_authority,
        reference=reference,
        expected_epoch=service.service_epoch,
        idempotency_key=key,
        expected_slot_generation=expected_slot_generation,
        command=command,
    )


if __name__ == "__main__":
    unittest.main()
