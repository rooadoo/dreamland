from __future__ import annotations

import dataclasses
import multiprocessing
import threading
import time
import unittest
from collections.abc import Callable
from unittest import mock

import dreamland.product.attendee_provider as PROVIDER_POOL
from dreamland.product.attendee_provider import (
    AttendeeProviderLifecycle,
    AttendeeProviderPool,
    AttendeeProviderPoolError,
    AttendeeProviderPoolLimits,
    AttendeeProviderRejectionReason,
    UpstreamTerminationEvidence,
)
from dreamland.product.execution import (
    ProductExecutionOutcome,
    ProductExecutionRequest,
    ProductExecutionResult,
    ProductOutputChunk,
    ProductOutputStream,
    ProductStateEffect,
    ProductStateEffectKind,
)


def _utc_ms() -> int:
    return time.time_ns() // 1_000_000


class _ScenarioExecutor:
    def execute(self, request, emit):
        if request.canonical_command == "block":
            while True:
                time.sleep(0.01)
        if request.canonical_command == "cooperative":
            while not request.cancelled():
                time.sleep(0.005)
            return ProductExecutionResult.interrupted("cancelled")
        if request.canonical_command == "oversized-output":
            emit(ProductOutputChunk(ProductOutputStream.STDOUT, "x" * 4_096))
            return ProductExecutionResult.completed_exited(exit_status=0)
        if request.canonical_command == "invalid-result":
            return object()
        if request.canonical_command == "output-until-fenced":
            for index in range(100):
                accepted = emit(
                    ProductOutputChunk(ProductOutputStream.STDOUT, f"{index}\n")
                )
                if accepted is False:
                    return ProductExecutionResult.completed_exited(
                        exit_status=0,
                        usage_tokens=13,
                    )
                time.sleep(0.005)
            return ProductExecutionResult.completed_exited(
                exit_status=0,
                usage_tokens=1,
            )
        emit(ProductOutputChunk(ProductOutputStream.STDOUT, "hello\n"))
        emit(ProductOutputChunk(ProductOutputStream.STDERR, "warning\n"))
        document = dict(request.provisional_document)
        document["completed"] = True
        return ProductExecutionResult.completed_exited(
            exit_status=0,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document=document,
            ),
            usage_tokens=7,
            usage_spend_units=3,
        )


def _scenario_factory() -> _ScenarioExecutor:
    return _ScenarioExecutor()


def _failing_factory() -> _ScenarioExecutor:
    raise RuntimeError("provider bootstrap failed")


def _wedged_startup_factory() -> _ScenarioExecutor:
    time.sleep(30)
    return _ScenarioExecutor()


@dataclasses.dataclass(slots=True)
class _FirstReadyThenWedgedFactory:
    starts: object
    delay_seconds: float = 30

    def __call__(self) -> _ScenarioExecutor:
        with self.starts.get_lock():
            self.starts.value += 1
            ordinal = self.starts.value
        if ordinal > 1:
            time.sleep(self.delay_seconds)
        return _ScenarioExecutor()


class _BlockingSendConnection:
    def __init__(self, connection) -> None:
        self._connection = connection
        self._release = threading.Event()

    def send_bytes(self, _frame: bytes) -> None:
        self._release.wait(30)
        raise BrokenPipeError("test dispatch was fenced")

    def close(self) -> None:
        self._release.set()
        self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


class _FailingEvent:
    def __init__(
        self,
        event: object,
        *,
        fail_clear: bool = False,
        fail_set: bool = False,
    ) -> None:
        self._event = event
        self._fail_clear = fail_clear
        self._fail_set = fail_set

    def clear(self) -> None:
        if self._fail_clear:
            raise OSError("test event clear failed")
        self._event.clear()

    def set(self) -> None:
        if self._fail_set:
            raise OSError("test event set failed")
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


class _SecondCallCancellation:
    def __init__(self) -> None:
        self._calls = 0

    def __call__(self) -> bool:
        self._calls += 1
        return self._calls >= 2


class _ManualPoolClock:
    def __init__(self, value: int) -> None:
        self._value = value
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            return self._value

    def set(self, value: int) -> None:
        with self._lock:
            self._value = value


class _BoundaryCrossingResultConnection:
    def __init__(
        self,
        *,
        clock: _ManualPoolClock,
        deadline_ms: int,
        frame: bytes,
        advance_on: str,
        cancellation: threading.Event | None = None,
    ) -> None:
        self.clock = clock
        self.deadline_ms = deadline_ms
        self.frame = frame
        self.advance_on = advance_on
        self.cancellation = cancellation

    def _cross_boundary(self) -> None:
        self.clock.set(self.deadline_ms)
        if self.cancellation is not None:
            self.cancellation.set()

    def poll(self, _timeout: float) -> bool:
        if self.advance_on == "poll":
            self._cross_boundary()
        return True

    def recv_bytes(self, *, maxlength: int) -> bytes:
        if len(self.frame) > maxlength:
            raise AssertionError("test result frame exceeds its configured bound")
        if self.advance_on == "receive":
            self._cross_boundary()
        return self.frame


class _CancellationAtDeadline:
    def __init__(self, clock: _ManualPoolClock, deadline_ms: int) -> None:
        self.clock = clock
        self.deadline_ms = deadline_ms
        self._calls = 0

    def __call__(self) -> bool:
        self._calls += 1
        if self._calls == 1:
            return False
        self.clock.set(self.deadline_ms)
        return True


class _DeadlineWithoutCancellation:
    def __init__(self, clock: _ManualPoolClock, deadline_ms: int) -> None:
        self.clock = clock
        self.deadline_ms = deadline_ms
        self._calls = 0

    def __call__(self) -> bool:
        self._calls += 1
        if self._calls == 2:
            self.clock.set(self.deadline_ms)
        return False


class _TrackingLock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.acquire_calls = 0

    def acquire(self, *args, **kwargs) -> bool:
        self.acquire_calls += 1
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()


class _FakeConnection:
    def __init__(self, *, fail_poll: bool = False) -> None:
        self.closed = False
        self.fail_poll = fail_poll

    def close(self) -> None:
        self.closed = True

    def poll(self, _timeout: float) -> bool:
        if self.fail_poll:
            raise EOFError("fake readiness transport failed")
        return False


class _FakeSurvivingProcess:
    def __init__(
        self,
        *,
        alive: bool = True,
        survive_signals: bool = True,
        raise_cleanup: bool = False,
    ) -> None:
        self.alive = alive
        self.survive_signals = survive_signals
        self.raise_cleanup = raise_cleanup
        self.started = False
        self.closed = False
        self.daemon = False
        self.join_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0

    def start(self) -> None:
        self.started = True
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, _timeout: float) -> None:
        self.join_calls += 1
        if self.raise_cleanup:
            raise RuntimeError("fake join failure")

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.raise_cleanup:
            raise RuntimeError("fake terminate failure")
        if not self.survive_signals:
            self.alive = False

    def kill(self) -> None:
        self.kill_calls += 1
        if self.raise_cleanup:
            raise RuntimeError("fake kill failure")
        if not self.survive_signals:
            self.alive = False

    def close(self) -> None:
        self.closed = True


class _FakeSurvivingContext:
    def __init__(self, process: _FakeSurvivingProcess) -> None:
        self.process = process

    @staticmethod
    def get_start_method() -> str:
        return "spawn"

    @staticmethod
    def Event() -> threading.Event:
        return threading.Event()

    @staticmethod
    def Pipe(*, duplex: bool):
        if duplex is not True:
            raise AssertionError("provider IPC must remain duplex")
        return _FakeConnection(fail_poll=True), _FakeConnection()

    def Process(self, **_kwargs) -> _FakeSurvivingProcess:
        return self.process


def _fake_slot(
    process: _FakeSurvivingProcess,
    *,
    active: bool,
) -> PROVIDER_POOL._WorkerSlot:
    return PROVIDER_POOL._WorkerSlot(
        index=0,
        process=process,
        connection=_FakeConnection(),
        cancel_event=threading.Event(),
        suppress_output_event=threading.Event(),
        active=active,
    )


def _request(
    command: str = "normal",
    *,
    cancelled: Callable[[], bool] | None = None,
    deadline_delta_ms: int = 10_000,
    state: dict[str, object] | None = None,
) -> ProductExecutionRequest:
    return ProductExecutionRequest(
        service_epoch="epoch-private-do-not-project",
        session_id="session-private-do-not-project",
        command_id=f"command-private-{command}",
        slot_generation=1,
        canonical_command=command,
        provisional_document=state or {"host": "attendee"},
        checkpoint_namespace="checkpoint-private-do-not-project",
        deadline_ms=_utc_ms() + deadline_delta_ms,
        max_tokens=41,
        max_spend_units=17,
        max_attempts=2,
        cancelled=cancelled or (lambda: False),
    )


def _limits(**changes: int) -> AttendeeProviderPoolLimits:
    values = {
        "worker_capacity": 1,
        "startup_timeout_ms": 8_000,
        "dispatch_timeout_ms": 75,
        "request_timeout_ms": 1_000,
        "cancellation_grace_ms": 50,
        "shutdown_timeout_ms": 100,
        "terminate_grace_ms": 75,
        "max_command_bytes": 1_024,
        "max_state_bytes": 16_384,
        "max_output_chunk_bytes": 1_024,
        "max_output_bytes": 16_384,
        "max_output_chunks": 64,
        "max_ipc_frame_bytes": 32_768,
    }
    values.update(changes)
    return AttendeeProviderPoolLimits(**values)


class AttendeeProviderPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pools: list[AttendeeProviderPool] = []

    def tearDown(self) -> None:
        for pool in reversed(self.pools):
            try:
                pool.shutdown(timeout_ms=50)
            except AttendeeProviderPoolError:
                pass

    def pool(
        self,
        factory=_scenario_factory,
        *,
        limits: AttendeeProviderPoolLimits | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> AttendeeProviderPool:
        pool = AttendeeProviderPool(
            factory,
            limits=limits or _limits(),
            clock_ms=clock_ms,
        )
        self.pools.append(pool)
        return pool

    def assertEventually(
        self,
        predicate: Callable[[], bool],
        *,
        timeout: float = 3.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition did not become true before its deadline")

    def assertPoolEventuallyReady(self, pool: AttendeeProviderPool) -> None:
        """Wait through the pool's configured replacement-readiness bound."""
        timeout_ms = pool.limits.startup_timeout_ms + (
            2 * pool.limits.terminate_grace_ms
        )
        timeout = (timeout_ms / 1_000) + 1.0
        deadline = time.monotonic() + timeout
        last_snapshot = pool.snapshot()
        while time.monotonic() < deadline:
            last_snapshot = pool.snapshot()
            if (
                last_snapshot.lifecycle is AttendeeProviderLifecycle.READY
                and last_snapshot.ready_workers == last_snapshot.worker_capacity
                and last_snapshot.active_workers == 0
                and last_snapshot.available_workers == last_snapshot.worker_capacity
            ):
                return
            if last_snapshot.lifecycle in {
                AttendeeProviderLifecycle.FAILED,
                AttendeeProviderLifecycle.DRAINING,
                AttendeeProviderLifecycle.CLOSED,
            }:
                break
            time.sleep(0.01)
        self.fail(
            "provider pool did not become fully ready within its configured "
            f"replacement bound: {last_snapshot!r}"
        )

    def test_construction_is_inert_and_explicit_start_becomes_ready(self) -> None:
        before = {
            child.pid
            for child in multiprocessing.active_children()
            if child.name.startswith("dreamland-attendee-provider-")
        }
        pool = self.pool()

        constructed = pool.snapshot()
        after_construction = {
            child.pid
            for child in multiprocessing.active_children()
            if child.name.startswith("dreamland-attendee-provider-")
        }
        self.assertEqual(constructed.lifecycle, AttendeeProviderLifecycle.STARTING)
        self.assertEqual(constructed.ready_workers, 0)
        self.assertEqual(after_construction, before)

        ready = pool.start()

        self.assertEqual(ready.lifecycle, AttendeeProviderLifecycle.READY)
        self.assertEqual(ready.ready_workers, 1)
        self.assertEqual(ready.available_workers, 1)

    def test_normal_output_result_and_snapshot_are_safe(self) -> None:
        pool = self.pool()
        pool.start()
        output: list[ProductOutputChunk] = []

        result = pool.execute(_request(), lambda chunk: output.append(chunk))

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertEqual(result.guest_termination.exit_status, 0)
        self.assertEqual(
            result.state_effect.provisional_document,
            {"host": "attendee", "completed": True},
        )
        self.assertEqual(result.usage_tokens, 7)
        self.assertEqual(result.usage_spend_units, 3)
        self.assertEqual(
            output,
            [
                ProductOutputChunk(ProductOutputStream.STDOUT, "hello\n"),
                ProductOutputChunk(ProductOutputStream.STDERR, "warning\n"),
            ],
        )
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.accepted_dispatches, 1)
        self.assertEqual(snapshot.completed_dispatches, 1)
        self.assertEqual(snapshot.active_workers, 0)
        self.assertEqual(
            snapshot.forced_upstream_termination_evidence,
            UpstreamTerminationEvidence.UNCONFIRMED,
        )
        projection = repr(dataclasses.asdict(snapshot))
        self.assertNotIn("epoch-private", projection)
        self.assertNotIn("session-private", projection)
        self.assertNotIn("command-private", projection)
        self.assertNotIn("checkpoint-private", projection)

    def test_capacity_rejection_is_predispatch_and_active_cancel_is_reserved(
        self,
    ) -> None:
        pool = self.pool()
        pool.start()
        cancel = threading.Event()
        first_result: list[ProductExecutionResult] = []
        execution = threading.Thread(
            target=lambda: first_result.append(
                pool.execute(_request("block", cancelled=cancel.is_set), lambda _: True)
            )
        )
        execution.start()
        self.assertEventually(lambda: pool.snapshot().active_workers == 1)

        rejected = pool.execute(_request("normal"), lambda _: True)

        self.assertEqual(rejected.outcome, ProductExecutionOutcome.FAILED)
        self.assertEqual(
            rejected.reason,
            AttendeeProviderRejectionReason.PROVIDER_CAPACITY_EXHAUSTED,
        )
        self.assertEqual(rejected.usage_tokens, 0)
        self.assertEqual(rejected.usage_spend_units, 0)

        cancel.set()
        execution.join(2)
        self.assertFalse(execution.is_alive())
        self.assertEqual(
            first_result[0].reason,
            AttendeeProviderRejectionReason.PROVIDER_CANCEL_TIMEOUT,
        )
        self.assertEqual(first_result[0].usage_tokens, 41)
        self.assertEqual(first_result[0].usage_spend_units, 17)
        self.assertPoolEventuallyReady(pool)
        self.assertEqual(
            pool.execute(_request(), lambda _: True).outcome,
            ProductExecutionOutcome.COMPLETED,
        )

    def test_cooperative_cancellation_is_not_erased_in_child(self) -> None:
        pool = self.pool()
        pool.start()
        cancel = threading.Event()
        results: list[ProductExecutionResult] = []
        execution = threading.Thread(
            target=lambda: results.append(
                pool.execute(
                    _request("cooperative", cancelled=cancel.is_set),
                    lambda _: True,
                )
            )
        )
        execution.start()
        self.assertEventually(lambda: pool.snapshot().active_workers == 1)

        cancel.set()
        execution.join(2)

        self.assertFalse(execution.is_alive())
        self.assertEqual(results[0].outcome, ProductExecutionOutcome.INTERRUPTED)
        self.assertEqual(results[0].reason, "cancelled")
        self.assertEqual(pool.snapshot().lifecycle, AttendeeProviderLifecycle.READY)

    def test_cooperative_cancellation_wins_at_exact_request_deadline(self) -> None:
        clock = _ManualPoolClock(99)
        pool = self.pool(clock_ms=clock)
        pool.start()
        deadline_ms = 100
        request = dataclasses.replace(
            _request("cooperative"),
            deadline_ms=deadline_ms,
            cancelled=_CancellationAtDeadline(clock, deadline_ms),
        )

        result = pool.execute(request, lambda _: True)

        self.assertEqual(result.outcome, ProductExecutionOutcome.INTERRUPTED)
        self.assertEqual(result.reason, "cancelled")
        self.assertEqual(result.usage_tokens, 0)
        self.assertEqual(result.usage_spend_units, 0)
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.completed_dispatches, 1)
        self.assertEqual(snapshot.ambiguous_dispatches, 0)
        self.assertEqual(snapshot.locally_forced_workers, 0)
        self.assertEqual(snapshot.lifecycle, AttendeeProviderLifecycle.READY)

    def test_parent_output_rejection_is_propagated_to_child(self) -> None:
        pool = self.pool()
        pool.start()
        output: list[str] = []

        result = pool.execute(
            _request("output-until-fenced"),
            lambda chunk: output.append(chunk.text) or False,
        )

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertEqual(result.usage_tokens, 13)
        self.assertEqual(output, ["0\n"])

    def test_blocked_request_send_is_bounded_and_contained(self) -> None:
        limits = _limits(dispatch_timeout_ms=50, terminate_grace_ms=50)
        pool = self.pool(limits=limits)
        pool.start()
        slot = next(iter(pool._slots.values()))
        slot.connection = _BlockingSendConnection(slot.connection)
        started = time.monotonic()

        result = pool.execute(_request(), lambda _: True)

        self.assertLess(time.monotonic() - started, 0.75)
        self.assertEqual(
            result.reason,
            AttendeeProviderRejectionReason.PROVIDER_DISPATCH_FAILED,
        )
        self.assertEqual(result.usage_tokens, 41)
        self.assertPoolEventuallyReady(pool)

    def test_send_thread_start_failure_is_safe_and_replaces_worker(self) -> None:
        pool = self.pool(limits=_limits(terminate_grace_ms=50))
        pool.start()
        original_start = threading.Thread.start

        def fail_only_request_send(thread: threading.Thread) -> None:
            if thread.name.startswith("attendee-provider-send-"):
                raise RuntimeError("fake thread start failure")
            original_start(thread)

        with mock.patch.object(
            threading.Thread,
            "start",
            new=fail_only_request_send,
        ):
            result = pool.execute(_request(), lambda _: True)

        self.assertEqual(
            result.reason,
            AttendeeProviderRejectionReason.PROVIDER_DISPATCH_FAILED,
        )
        self.assertEqual(result.usage_tokens, 41)
        self.assertEqual(result.usage_spend_units, 17)
        self.assertFalse(pool._send_threads)
        self.assertEqual(pool.snapshot().active_workers, 0)
        self.assertPoolEventuallyReady(pool)

    def test_event_reset_failure_is_zero_charge_and_replaces_worker(self) -> None:
        for event_name in ("cancel_event", "suppress_output_event"):
            with self.subTest(event_name=event_name):
                pool = self.pool(limits=_limits(terminate_grace_ms=50))
                pool.start()
                slot = next(iter(pool._slots.values()))
                setattr(
                    slot,
                    event_name,
                    _FailingEvent(getattr(slot, event_name), fail_clear=True),
                )

                result = pool.execute(_request(), lambda _: True)

                self.assertEqual(
                    result.reason,
                    AttendeeProviderRejectionReason.PROVIDER_DISPATCH_FAILED,
                )
                self.assertEqual(result.usage_tokens, 0)
                self.assertEqual(result.usage_spend_units, 0)
                snapshot = pool.snapshot()
                self.assertEqual(snapshot.accepted_dispatches, 0)
                self.assertEqual(snapshot.ambiguous_dispatches, 0)
                self.assertEqual(snapshot.active_workers, 0)
                self.assertPoolEventuallyReady(pool)
                follow_up = pool.execute(_request(), lambda _: True)
                self.assertEqual(
                    follow_up.outcome,
                    ProductExecutionOutcome.COMPLETED,
                )

    def test_postdispatch_event_signal_failures_are_ambiguous_and_contained(
        self,
    ) -> None:
        scenarios = (
            (
                "cancel_event",
                _request("block", cancelled=_SecondCallCancellation()),
                lambda _chunk: True,
            ),
            (
                "suppress_output_event",
                _request("output-until-fenced"),
                lambda _chunk: False,
            ),
        )
        for event_name, request, emit in scenarios:
            with self.subTest(event_name=event_name):
                pool = self.pool(limits=_limits(terminate_grace_ms=50))
                pool.start()
                slot = next(iter(pool._slots.values()))
                setattr(
                    slot,
                    event_name,
                    _FailingEvent(getattr(slot, event_name), fail_set=True),
                )

                result = pool.execute(request, emit)

                self.assertEqual(
                    result.reason,
                    AttendeeProviderRejectionReason.PROVIDER_DISPATCH_FAILED,
                )
                self.assertEqual(result.usage_tokens, request.max_tokens)
                self.assertEqual(
                    result.usage_spend_units,
                    request.max_spend_units,
                )
                snapshot = pool.snapshot()
                self.assertEqual(snapshot.accepted_dispatches, 1)
                self.assertEqual(snapshot.ambiguous_dispatches, 1)
                self.assertEqual(snapshot.active_workers, 0)
                self.assertPoolEventuallyReady(pool)

    def test_ambiguous_dispatch_survivor_is_retained_until_shutdown_reaps_it(
        self,
    ) -> None:
        process = _FakeSurvivingProcess()
        slot = _fake_slot(process, active=True)
        pool = self.pool(limits=_limits(terminate_grace_ms=10))
        with pool._condition:
            pool._start_called = True
            pool._lifecycle = AttendeeProviderLifecycle.READY
            pool._slots[slot.index] = slot

        result = pool._ambiguous_failure(
            slot,
            AttendeeProviderRejectionReason.PROVIDER_REQUEST_TIMEOUT,
            _request(),
        )

        snapshot = pool.snapshot()
        self.assertEqual(
            result.reason,
            AttendeeProviderRejectionReason.PROVIDER_REQUEST_TIMEOUT,
        )
        self.assertEqual(result.usage_tokens, 41)
        self.assertEqual(snapshot.lifecycle, AttendeeProviderLifecycle.FAILED)
        self.assertEqual(snapshot.locally_forced_workers, 1)
        self.assertIs(pool._slots[slot.index], slot)
        self.assertTrue(slot.retired)
        self.assertFalse(slot.active)
        self.assertFalse(pool._replacement_indices)
        self.assertFalse(pool._replacement_threads)
        self.assertGreaterEqual(process.terminate_calls, 1)
        self.assertGreaterEqual(process.kill_calls, 1)

        with self.assertRaises(AttendeeProviderPoolError):
            pool.shutdown(timeout_ms=10)
        self.assertEqual(pool.snapshot().lifecycle, AttendeeProviderLifecycle.FAILED)
        self.assertTrue(pool._slots)
        self.assertEqual(pool.snapshot().locally_forced_workers, 1)

        process.survive_signals = False
        closed = pool.shutdown(timeout_ms=10)
        self.assertEqual(closed.lifecycle, AttendeeProviderLifecycle.CLOSED)
        self.assertTrue(process.closed)

    def test_drain_rejects_fresh_dispatch_and_shutdown_closes(self) -> None:
        pool = self.pool()
        pool.start()

        draining = pool.begin_drain(timeout_ms=100)
        rejected = pool.execute(_request(), lambda _: True)
        closed = pool.shutdown(timeout_ms=50)

        self.assertEqual(draining.lifecycle, AttendeeProviderLifecycle.DRAINING)
        self.assertEqual(
            rejected.reason,
            AttendeeProviderRejectionReason.PROVIDER_DRAINING,
        )
        self.assertEqual(rejected.usage_tokens, 0)
        self.assertEqual(closed.lifecycle, AttendeeProviderLifecycle.CLOSED)
        self.assertEqual(closed.ready_workers, 0)

    def test_deadline_hard_terminates_and_replaces_worker(self) -> None:
        limits = _limits(request_timeout_ms=2_000, terminate_grace_ms=50)
        pool = self.pool(limits=limits)
        pool.start()
        started = time.monotonic()

        result = pool.execute(
            _request("block", deadline_delta_ms=50),
            lambda _: True,
        )

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(
            result.reason,
            AttendeeProviderRejectionReason.PROVIDER_DEADLINE_EXCEEDED,
        )
        self.assertEqual(result.usage_tokens, 41)
        self.assertEqual(result.usage_spend_units, 17)
        self.assertPoolEventuallyReady(pool)
        follow_up = pool.execute(_request(), lambda _: True)
        self.assertEqual(follow_up.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertGreaterEqual(pool.snapshot().locally_forced_workers, 1)

    def test_deadline_cooperatively_stops_worker_without_ambiguous_dispatch(
        self,
    ) -> None:
        clock = _ManualPoolClock(99)
        limits = _limits(request_timeout_ms=2_000, cancellation_grace_ms=250)
        pool = self.pool(limits=limits, clock_ms=clock)
        pool.start()
        deadline_ms = 100

        result = pool.execute(
            dataclasses.replace(
                _request("cooperative"),
                deadline_ms=deadline_ms,
                cancelled=_DeadlineWithoutCancellation(clock, deadline_ms),
            ),
            lambda _: True,
        )

        self.assertEqual(result.outcome, ProductExecutionOutcome.FAILED)
        self.assertEqual(
            result.reason,
            AttendeeProviderRejectionReason.PROVIDER_DEADLINE_EXCEEDED,
        )
        self.assertEqual(result.usage_tokens, 41)
        self.assertEqual(result.usage_spend_units, 17)
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.completed_dispatches, 1)
        self.assertEqual(snapshot.ambiguous_dispatches, 0)
        self.assertEqual(snapshot.locally_forced_workers, 0)
        self.assertEqual(snapshot.lifecycle, AttendeeProviderLifecycle.READY)
        follow_up_output: list[ProductOutputChunk] = []
        follow_up = pool.execute(
            _request(),
            lambda chunk: follow_up_output.append(chunk) is None,
        )
        self.assertEqual(follow_up.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertEqual(
            [chunk.text for chunk in follow_up_output],
            ["hello\n", "warning\n"],
        )

    def test_result_crossing_deadline_during_poll_or_receive_stays_reserved(
        self,
    ) -> None:
        for advance_on in ("poll", "receive"):
            with self.subTest(advance_on=advance_on):
                clock = _ManualPoolClock(99)
                limits = _limits(request_timeout_ms=2_000, cancellation_grace_ms=250)
                pool = self.pool(limits=limits, clock_ms=clock)
                deadline_ms = 100
                request = dataclasses.replace(
                    _request(),
                    deadline_ms=deadline_ms,
                    cancelled=lambda: False,
                )
                frame = PROVIDER_POOL._encode_message(
                    PROVIDER_POOL._result_to_message(
                        ProductExecutionResult.completed_exited(
                            exit_status=0,
                            usage_tokens=7,
                            usage_spend_units=3,
                        ),
                        limits,
                    )
                )
                slot = _fake_slot(_FakeSurvivingProcess(), active=True)
                slot.connection = _BoundaryCrossingResultConnection(
                    clock=clock,
                    deadline_ms=deadline_ms,
                    frame=frame,
                    advance_on=advance_on,
                )

                result = pool._await_result(slot, request, lambda _: True)

                self.assertEqual(result.outcome, ProductExecutionOutcome.FAILED)
                self.assertEqual(
                    result.reason,
                    AttendeeProviderRejectionReason.PROVIDER_DEADLINE_EXCEEDED,
                )
                self.assertEqual(result.usage_tokens, request.max_tokens)
                self.assertEqual(
                    result.usage_spend_units,
                    request.max_spend_units,
                )
                self.assertTrue(slot.cancel_event.is_set())
                self.assertTrue(slot.suppress_output_event.is_set())
                snapshot = pool.snapshot()
                self.assertEqual(snapshot.completed_dispatches, 1)
                self.assertEqual(snapshot.ambiguous_dispatches, 0)
                self.assertEqual(snapshot.locally_forced_workers, 0)

    def test_cancellation_crossing_with_poll_wins_at_exact_deadline(self) -> None:
        clock = _ManualPoolClock(99)
        limits = _limits(request_timeout_ms=2_000, cancellation_grace_ms=250)
        pool = self.pool(limits=limits, clock_ms=clock)
        deadline_ms = 100
        cancellation = threading.Event()
        request = dataclasses.replace(
            _request(),
            deadline_ms=deadline_ms,
            cancelled=cancellation.is_set,
        )
        frame = PROVIDER_POOL._encode_message(
            PROVIDER_POOL._result_to_message(
                ProductExecutionResult.interrupted(
                    "cancelled",
                    usage_tokens=5,
                    usage_spend_units=2,
                ),
                limits,
            )
        )
        slot = _fake_slot(_FakeSurvivingProcess(), active=True)
        slot.connection = _BoundaryCrossingResultConnection(
            clock=clock,
            deadline_ms=deadline_ms,
            frame=frame,
            advance_on="poll",
            cancellation=cancellation,
        )

        result = pool._await_result(slot, request, lambda _: True)

        self.assertEqual(result.outcome, ProductExecutionOutcome.INTERRUPTED)
        self.assertEqual(result.reason, "cancelled")
        self.assertEqual(result.usage_tokens, 5)
        self.assertEqual(result.usage_spend_units, 2)
        self.assertTrue(slot.cancel_event.is_set())
        self.assertFalse(slot.suppress_output_event.is_set())
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.completed_dispatches, 1)
        self.assertEqual(snapshot.ambiguous_dispatches, 0)
        self.assertEqual(snapshot.locally_forced_workers, 0)

    def test_request_timeout_caps_deadline_cancellation_grace(self) -> None:
        clock = _ManualPoolClock(99)
        limits = _limits(
            request_timeout_ms=75,
            cancellation_grace_ms=1_000,
            terminate_grace_ms=50,
        )
        pool = self.pool(limits=limits, clock_ms=clock)
        pool.start()
        deadline_ms = 100

        result = pool.execute(
            dataclasses.replace(
                _request("block"),
                deadline_ms=deadline_ms,
                cancelled=_DeadlineWithoutCancellation(clock, deadline_ms),
            ),
            lambda _: True,
        )

        self.assertEqual(
            result.reason,
            AttendeeProviderRejectionReason.PROVIDER_REQUEST_TIMEOUT,
        )
        self.assertEqual(result.usage_tokens, 41)
        self.assertEqual(result.usage_spend_units, 17)
        self.assertPoolEventuallyReady(pool)
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.completed_dispatches, 0)
        self.assertEqual(snapshot.ambiguous_dispatches, 1)
        self.assertGreaterEqual(snapshot.locally_forced_workers, 1)

    def test_drain_deadline_caps_provider_deadline_cancellation_grace(self) -> None:
        clock = _ManualPoolClock(99)
        limits = _limits(
            request_timeout_ms=2_000,
            cancellation_grace_ms=1_000,
            terminate_grace_ms=50,
        )
        pool = self.pool(limits=limits, clock_ms=clock)
        pool.start()
        deadline_ms = 100
        results: list[ProductExecutionResult] = []

        execution = threading.Thread(
            target=lambda: results.append(
                pool.execute(
                    dataclasses.replace(
                        _request("block"),
                        deadline_ms=deadline_ms,
                        cancelled=_DeadlineWithoutCancellation(clock, deadline_ms),
                    ),
                    lambda _: True,
                )
            )
        )
        execution.start()
        self.assertEventually(lambda: pool.snapshot().active_workers == 1)
        pool.begin_drain(timeout_ms=50)
        execution.join(timeout=1)

        self.assertFalse(execution.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].reason,
            AttendeeProviderRejectionReason.PROVIDER_SHUTDOWN,
        )
        self.assertEqual(results[0].usage_tokens, 41)
        self.assertEqual(results[0].usage_spend_units, 17)
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.lifecycle, AttendeeProviderLifecycle.DRAINING)
        self.assertEqual(snapshot.completed_dispatches, 0)
        self.assertEqual(snapshot.ambiguous_dispatches, 1)
        self.assertEqual(snapshot.ready_workers, 0)
        closed = pool.shutdown(timeout_ms=50)
        self.assertEqual(closed.lifecycle, AttendeeProviderLifecycle.CLOSED)
        self.assertGreaterEqual(closed.locally_forced_workers, 1)

    def test_replacement_starting_waits_for_readiness_within_dispatch_bound(
        self,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        starts = context.Value("i", 0)
        factory = _FirstReadyThenWedgedFactory(starts, delay_seconds=0.15)
        limits = _limits(
            dispatch_timeout_ms=3_000,
            request_timeout_ms=75,
            terminate_grace_ms=50,
        )
        pool = self.pool(factory, limits=limits)
        pool.start()
        timed_out = pool.execute(_request("block"), lambda _: True)
        self.assertEqual(
            timed_out.reason,
            AttendeeProviderRejectionReason.PROVIDER_REQUEST_TIMEOUT,
        )

        result = pool.execute(_request(), lambda _: True)

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertEqual(pool.snapshot().lifecycle, AttendeeProviderLifecycle.READY)

    def test_concurrent_failures_replace_every_retired_worker(self) -> None:
        limits = _limits(
            worker_capacity=2,
            request_timeout_ms=125,
            terminate_grace_ms=50,
        )
        pool = self.pool(limits=limits)
        pool.start()
        results: list[ProductExecutionResult] = []
        executions = [
            threading.Thread(
                target=lambda: results.append(
                    pool.execute(_request("block"), lambda _: True)
                )
            )
            for _ in range(2)
        ]
        for execution in executions:
            execution.start()
        self.assertEventually(lambda: pool.snapshot().active_workers == 2)

        for execution in executions:
            execution.join(2)

        self.assertTrue(all(not execution.is_alive() for execution in executions))
        self.assertEqual(len(results), 2)
        self.assertTrue(
            all(
                result.reason
                == AttendeeProviderRejectionReason.PROVIDER_REQUEST_TIMEOUT
                for result in results
            )
        )
        self.assertPoolEventuallyReady(pool)
        self.assertEqual(
            pool.execute(_request(), lambda _: True).outcome,
            ProductExecutionOutcome.COMPLETED,
        )

    def test_startup_failure_rolls_back_without_live_children(self) -> None:
        pool = self.pool(_failing_factory, limits=_limits(startup_timeout_ms=500))

        with self.assertRaises(AttendeeProviderPoolError) as caught:
            pool.start()

        self.assertEqual(
            caught.exception.reason,
            AttendeeProviderRejectionReason.PROVIDER_FAILED,
        )
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.lifecycle, AttendeeProviderLifecycle.FAILED)
        self.assertEqual(snapshot.ready_workers, 0)
        self.assertEqual(snapshot.startup_failures, 1)
        self.assertFalse(
            any(
                child.name.startswith("dreamland-attendee-provider-")
                for child in multiprocessing.active_children()
            )
        )

    def test_startup_survivor_remains_tracked_when_cleanup_methods_raise(
        self,
    ) -> None:
        process = _FakeSurvivingProcess(raise_cleanup=True)
        context = _FakeSurvivingContext(process)
        pool = AttendeeProviderPool(
            _scenario_factory,
            limits=_limits(startup_timeout_ms=10, terminate_grace_ms=10),
            process_context=context,
        )
        self.pools.append(pool)

        with self.assertRaises(AttendeeProviderPoolError) as caught:
            pool.start()

        self.assertEqual(
            caught.exception.reason,
            AttendeeProviderRejectionReason.PROVIDER_FAILED,
        )
        self.assertEqual(pool.snapshot().lifecycle, AttendeeProviderLifecycle.FAILED)
        self.assertTrue(pool._spawning_slots)
        self.assertIn(
            process,
            [slot.process for slot in pool._spawning_slots.values()],
        )
        self.assertGreaterEqual(process.join_calls, 1)
        self.assertGreaterEqual(process.terminate_calls, 1)
        self.assertGreaterEqual(process.kill_calls, 1)

        process.raise_cleanup = False
        process.survive_signals = False
        closed = pool.shutdown(timeout_ms=10)
        self.assertEqual(closed.lifecycle, AttendeeProviderLifecycle.CLOSED)
        self.assertTrue(process.closed)

    def test_replacement_cleanup_survivor_cannot_restore_ready(self) -> None:
        process = _FakeSurvivingProcess()
        replacement = _fake_slot(process, active=False)
        pool = self.pool(limits=_limits(terminate_grace_ms=10))
        with pool._condition:
            pool._start_called = True
            pool._lifecycle = AttendeeProviderLifecycle.STARTING
            pool._replacement_indices.add(replacement.index)

        def replacement_arrives_after_failure(
            _index: int,
            _deadline_ms: int,
        ) -> PROVIDER_POOL._WorkerSlot:
            with pool._condition:
                pool._spawning_slots[id(replacement)] = replacement
                pool._lifecycle = AttendeeProviderLifecycle.FAILED
            return replacement

        with mock.patch.object(
            pool,
            "_spawn_ready_worker",
            side_effect=replacement_arrives_after_failure,
        ):
            pool._replace_worker(replacement.index)

        self.assertEqual(pool.snapshot().lifecycle, AttendeeProviderLifecycle.FAILED)
        self.assertIs(pool._spawning_slots[id(replacement)], replacement)
        self.assertNotIn(replacement.index, pool._replacement_indices)
        self.assertFalse(pool._replacement_threads)

        with self.assertRaises(AttendeeProviderPoolError):
            pool.shutdown(timeout_ms=10)
        self.assertEqual(pool.snapshot().lifecycle, AttendeeProviderLifecycle.FAILED)

        process.survive_signals = False
        closed = pool.shutdown(timeout_ms=10)
        self.assertEqual(closed.lifecycle, AttendeeProviderLifecycle.CLOSED)

    def test_wedged_startup_obeys_finite_rollback_bound(self) -> None:
        limits = _limits(startup_timeout_ms=75, terminate_grace_ms=50)
        pool = self.pool(_wedged_startup_factory, limits=limits)
        started = time.monotonic()

        with self.assertRaises(AttendeeProviderPoolError):
            pool.start()

        self.assertLess(time.monotonic() - started, 0.75)
        self.assertEqual(pool.snapshot().lifecycle, AttendeeProviderLifecycle.FAILED)

    def test_drain_fences_and_reaps_a_starting_worker(self) -> None:
        limits = _limits(startup_timeout_ms=2_000, terminate_grace_ms=50)
        pool = self.pool(_wedged_startup_factory, limits=limits)
        errors: list[AttendeeProviderPoolError] = []
        starter = threading.Thread(
            target=lambda: self._capture_start_error(pool, errors)
        )
        starter.start()
        self.assertEventually(lambda: bool(pool._spawning_slots))

        draining = pool.begin_drain(timeout_ms=100)
        starter.join(1)

        self.assertEqual(draining.lifecycle, AttendeeProviderLifecycle.DRAINING)
        self.assertFalse(starter.is_alive())
        self.assertEqual(
            errors[0].reason,
            AttendeeProviderRejectionReason.PROVIDER_DRAINING,
        )
        self.assertFalse(pool._spawning_slots)

    @staticmethod
    def _capture_start_error(
        pool: AttendeeProviderPool,
        errors: list[AttendeeProviderPoolError],
    ) -> None:
        try:
            pool.start()
        except AttendeeProviderPoolError as error:
            errors.append(error)

    def test_oversized_output_and_invalid_result_are_contained(self) -> None:
        limits = _limits(max_output_chunk_bytes=64, terminate_grace_ms=50)
        pool = self.pool(limits=limits)
        pool.start()

        oversized = pool.execute(_request("oversized-output"), lambda _: True)
        self.assertEqual(
            oversized.reason,
            AttendeeProviderRejectionReason.PROVIDER_WORKER_FAILED,
        )
        self.assertEqual(oversized.usage_tokens, 41)
        self.assertPoolEventuallyReady(pool)

        invalid = pool.execute(_request("invalid-result"), lambda _: True)
        self.assertEqual(
            invalid.reason,
            AttendeeProviderRejectionReason.PROVIDER_WORKER_FAILED,
        )
        self.assertEqual(invalid.usage_spend_units, 17)
        self.assertPoolEventuallyReady(pool)

    def test_wedged_child_shutdown_is_bounded_and_reaped(self) -> None:
        limits = _limits(shutdown_timeout_ms=50, terminate_grace_ms=50)
        pool = self.pool(limits=limits)
        result: list[ProductExecutionResult] = []
        pool.start()
        execution = threading.Thread(
            target=lambda: result.append(
                pool.execute(_request("block"), lambda _: True)
            )
        )
        execution.start()
        self.assertEventually(lambda: pool.snapshot().active_workers == 1)
        started = time.monotonic()

        snapshot = pool.shutdown(timeout_ms=50)

        elapsed_ms = (time.monotonic() - started) * 1_000
        self.assertLessEqual(
            elapsed_ms,
            limits.shutdown_upper_bound_ms(50) + 250,
        )
        execution.join(1)
        self.assertFalse(execution.is_alive())
        self.assertEqual(snapshot.lifecycle, AttendeeProviderLifecycle.CLOSED)
        self.assertEqual(
            result[0].reason,
            AttendeeProviderRejectionReason.PROVIDER_SHUTDOWN,
        )
        self.assertEqual(result[0].usage_tokens, 41)
        self.assertFalse(
            any(
                child.name.startswith("dreamland-attendee-provider-")
                for child in multiprocessing.active_children()
            )
        )

    def test_shutdown_serializes_slot_stopping_with_stop_lock(self) -> None:
        pool = self.pool()
        pool.start()
        slot = next(iter(pool._slots.values()))
        tracking_lock = _TrackingLock()
        slot.stop_lock = tracking_lock

        closed = pool.shutdown(timeout_ms=50)

        self.assertEqual(closed.lifecycle, AttendeeProviderLifecycle.CLOSED)
        self.assertGreaterEqual(tracking_lock.acquire_calls, 1)

    def test_shutdown_joins_inflight_replacement_and_its_child(self) -> None:
        context = multiprocessing.get_context("spawn")
        starts = context.Value("i", 0)
        factory = _FirstReadyThenWedgedFactory(starts)
        limits = _limits(
            request_timeout_ms=100,
            terminate_grace_ms=50,
        )
        pool = self.pool(factory, limits=limits)
        pool.start()

        timed_out = pool.execute(_request("block"), lambda _: True)
        self.assertEqual(
            timed_out.reason,
            AttendeeProviderRejectionReason.PROVIDER_REQUEST_TIMEOUT,
        )
        self.assertEventually(
            lambda: starts.value >= 2,
            timeout=(limits.startup_timeout_ms / 1_000) + 1.0,
        )
        started = time.monotonic()

        closed = pool.shutdown(timeout_ms=50)

        elapsed_ms = (time.monotonic() - started) * 1_000
        self.assertLessEqual(
            elapsed_ms,
            limits.shutdown_upper_bound_ms(50) + 250,
        )
        self.assertEqual(closed.lifecycle, AttendeeProviderLifecycle.CLOSED)
        self.assertFalse(pool._replacement_threads)
        self.assertFalse(pool._spawning_slots)
        self.assertFalse(
            any(
                child.name.startswith("dreamland-attendee-provider-")
                for child in multiprocessing.active_children()
            )
        )

    def test_drain_fences_inflight_replacement(self) -> None:
        context = multiprocessing.get_context("spawn")
        starts = context.Value("i", 0)
        factory = _FirstReadyThenWedgedFactory(starts)
        limits = _limits(
            request_timeout_ms=100,
            terminate_grace_ms=50,
        )
        pool = self.pool(factory, limits=limits)
        pool.start()
        pool.execute(_request("block"), lambda _: True)
        self.assertEventually(
            lambda: starts.value >= 2,
            timeout=(limits.startup_timeout_ms / 1_000) + 1.0,
        )

        draining = pool.begin_drain(timeout_ms=100)

        self.assertEqual(draining.lifecycle, AttendeeProviderLifecycle.DRAINING)
        self.assertEventually(
            lambda: not pool._replacement_threads and not pool._spawning_slots
        )
        self.assertEqual(
            pool.shutdown(timeout_ms=50).lifecycle,
            AttendeeProviderLifecycle.CLOSED,
        )

    def test_close_before_start_is_terminal(self) -> None:
        pool = self.pool()

        snapshot = pool.shutdown(timeout_ms=25)

        self.assertEqual(snapshot.lifecycle, AttendeeProviderLifecycle.CLOSED)
        with self.assertRaises(AttendeeProviderPoolError) as caught:
            pool.start()
        self.assertEqual(
            caught.exception.reason,
            AttendeeProviderRejectionReason.PROVIDER_CLOSED,
        )

    def test_shutdown_failure_never_publishes_closed(self) -> None:
        pool = self.pool()
        pool.start()
        slot = next(iter(pool._slots.values()))

        with mock.patch.object(
            pool,
            "_stop_slots_bounded",
            return_value=(0, (slot,)),
        ):
            with self.assertRaises(AttendeeProviderPoolError) as caught:
                pool.shutdown(timeout_ms=25)

        self.assertEqual(
            caught.exception.reason,
            AttendeeProviderRejectionReason.PROVIDER_FAILED,
        )
        self.assertEqual(pool.snapshot().lifecycle, AttendeeProviderLifecycle.FAILED)
        self.assertTrue(pool._slots)
        closed = pool.shutdown(timeout_ms=25)
        self.assertEqual(closed.lifecycle, AttendeeProviderLifecycle.CLOSED)

    def test_replacement_join_failure_never_publishes_closed(self) -> None:
        pool = self.pool()
        pool.start()

        with mock.patch.object(
            pool,
            "_join_replacement_threads",
            return_value=False,
        ):
            with self.assertRaises(AttendeeProviderPoolError):
                pool.shutdown(timeout_ms=25)

        self.assertEqual(pool.snapshot().lifecycle, AttendeeProviderLifecycle.FAILED)
        self.assertTrue(pool._slots)
        self.assertEqual(
            pool.shutdown(timeout_ms=25).lifecycle,
            AttendeeProviderLifecycle.CLOSED,
        )

    def test_utc_deadline_domain_rejects_an_expired_request(self) -> None:
        pool = self.pool()
        pool.start()
        request = dataclasses.replace(_request(), deadline_ms=_utc_ms() - 1)

        result = pool.execute(request, lambda _: True)

        self.assertEqual(
            result.reason,
            AttendeeProviderRejectionReason.PROVIDER_DEADLINE_EXCEEDED,
        )
        self.assertEqual(result.usage_tokens, 0)

    def test_invalid_request_is_rejected_before_lifecycle_or_capacity(self) -> None:
        pool = self.pool()
        cyclic: dict[str, object] = {}
        cyclic["self"] = cyclic
        request = _request(state=cyclic)

        rejected = pool.execute(request, lambda _: True)

        self.assertEqual(
            rejected.reason,
            AttendeeProviderRejectionReason.PROVIDER_REQUEST_INVALID,
        )
        self.assertEqual(rejected.usage_tokens, 0)
        self.assertEqual(pool.snapshot().accepted_dispatches, 0)

    def test_non_spawn_context_is_rejected(self) -> None:
        try:
            context = multiprocessing.get_context("fork")
        except ValueError:
            self.skipTest("fork context is unavailable")

        with self.assertRaisesRegex(ValueError, "spawn"):
            AttendeeProviderPool(_scenario_factory, process_context=context)

    def test_limits_expose_exact_shutdown_upper_bound(self) -> None:
        limits = _limits(shutdown_timeout_ms=100, terminate_grace_ms=25)

        self.assertEqual(limits.shutdown_upper_bound_ms(), 175)
        self.assertEqual(limits.shutdown_upper_bound_ms(40), 115)
        with self.assertRaisesRegex(ValueError, "timeout_ms"):
            limits.shutdown_upper_bound_ms(0)

    def test_runtime_document_preflight_matches_pool_json_policy(self) -> None:
        limits = _limits(max_state_bytes=256)
        original = {"host": {"values": [1, 2, 3]}}

        validated = limits.validate_runtime_document(original)

        self.assertEqual(validated, original)
        self.assertIsNot(validated, original)
        self.assertIsNot(validated["host"], original["host"])

        too_deep: object = "leaf"
        for _ in range(33):
            too_deep = {"next": too_deep}
        invalid_documents = (
            {"integer": 1 << 63},
            {"number": float("inf")},
            {"deep": too_deep},
            {"large": "x" * 256},
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    limits.validate_runtime_document(document)


if __name__ == "__main__":
    unittest.main()
