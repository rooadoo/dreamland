from __future__ import annotations

import ast
import hashlib
import threading
import unittest
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import cast

from dreamland.models import (
    CommandContext,
    CommandOutputChunk,
    CommandResult,
    CommandStateUpdate,
    SessionState,
)
from dreamland.product.execution import (
    ProductExecutionOutcome,
    ProductExecutionRequest,
    ProductGuestTerminationKind,
    ProductOutputChunk,
    ProductOutputStream,
    ProductStateEffectKind,
)
from dreamland.product.runtime_executor import (
    DreamlandStateProfileCodec,
    ProductRuntimeCommandExecutor,
    ProductRuntimeControl,
    ProductRuntimeLimits,
    ProductRuntimeState,
    ProductRuntimeUsage,
)


class MutableClock:
    def __init__(self, value: int = 100) -> None:
        self._value = value
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            return self._value

    def set(self, value: int) -> None:
        with self._lock:
            self._value = value


class FakeCodec:
    def __init__(self) -> None:
        self.decode_calls: list[tuple[dict[str, object], str]] = []
        self.encode_calls = 0
        self.fail_decode = False

    def decode(
        self,
        document,
        *,
        checkpoint_namespace: str,
    ) -> ProductRuntimeState:
        if self.fail_decode:
            raise ValueError("unsafe codec detail")
        copied = deepcopy(dict(document))
        self.decode_calls.append((copied, checkpoint_namespace))
        world_state = deepcopy(document.get("world_state", {}))
        if not isinstance(world_state, dict):
            raise ValueError("world state is invalid")
        # Mutating the codec's received object must not mutate aggregate input.
        document["codec_observation"] = True
        return ProductRuntimeState(
            profile_name="product-test",
            session=SessionState(
                username="guest",
                cwd=str(document.get("cwd", "/home/guest")),
                world_state=world_state,
            ),
            codec_context={"kind": "fake"},
        )

    def encode(self, state: ProductRuntimeState):
        self.encode_calls += 1
        if not isinstance(state.session.cwd, str):
            raise ValueError("unsafe state detail")
        return {
            "cwd": state.session.cwd,
            "world_state": deepcopy(state.session.world_state),
        }


class FakeMeter:
    def __init__(self, usage: ProductRuntimeUsage | None = None) -> None:
        self.usage = usage or ProductRuntimeUsage(tokens=7, spend_units=3)
        self.begin_calls: list[tuple[str, str, ProductRuntimeLimits]] = []
        self.finish_calls: list[object] = []
        self.fail_begin = False
        self.fail_finish = False
        self.before_begin: Callable[[], None] | None = None
        self.log: list[str] | None = None

    def begin(
        self,
        *,
        command_id: str,
        checkpoint_namespace: str,
        limits: ProductRuntimeLimits,
    ) -> object:
        if self.before_begin is not None:
            self.before_begin()
        if self.fail_begin:
            raise RuntimeError("unsafe meter detail")
        self.begin_calls.append((command_id, checkpoint_namespace, limits))
        return object()

    def finish(self, measurement: object) -> ProductRuntimeUsage:
        self.finish_calls.append(measurement)
        if self.log is not None:
            self.log.append("meter.finish")
        if self.fail_finish:
            raise RuntimeError("unsafe meter detail")
        return self.usage


class FakeReaper:
    def __init__(self) -> None:
        self.namespaces: list[str] = []
        self.fail = False
        self.before_reap: Callable[[], None] | None = None
        self.log: list[str] | None = None

    def reap(self, checkpoint_namespace: str) -> None:
        if self.before_reap is not None:
            self.before_reap()
        self.namespaces.append(checkpoint_namespace)
        if self.log is not None:
            self.log.append("reaper.reap")
        if self.fail:
            raise RuntimeError("unsafe checkpoint detail")


class CallableAgent:
    def __init__(self, execute) -> None:
        self._execute = execute
        self.contexts: list[CommandContext] = []
        self.controls: list[ProductRuntimeControl] = []

    def execute(self, context, control, emit):
        self.contexts.append(context)
        self.controls.append(control)
        return self._execute(context, control, emit)


class ProductRuntimeCommandExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.codec = FakeCodec()
        self.meter = FakeMeter()
        self.reaper = FakeReaper()

    def test_preserves_identity_limits_and_ordered_output_on_detached_state(
        self,
    ) -> None:
        document = _document()
        original = deepcopy(document)

        def run(context, control, emit):
            self.assertEqual(context.command_id, "command:one")
            self.assertEqual(context.langfuse_trace_id, "product:session:1:command")
            self.assertEqual(context.command, "printf safe")
            self.assertTrue(context.force_session_state)
            self.assertEqual(
                control.limits,
                ProductRuntimeLimits(50, 20, 2),
            )
            context.world_state["agent_only"] = True
            emit(CommandOutputChunk("hel"))
            return CommandResult(stdout="hello", stderr="warn", exit_status=7)

        agent = CallableAgent(run)
        chunks: list[ProductOutputChunk] = []
        result = self._executor(agent).execute(
            _request(document=document),
            chunks.append,
        )

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        termination = result.guest_termination
        state_effect = result.state_effect
        self.assertIsNotNone(termination)
        self.assertIsNotNone(state_effect)
        assert termination is not None
        assert state_effect is not None
        self.assertEqual(termination.exit_status, 7)
        self.assertEqual(state_effect.kind, ProductStateEffectKind.NONE)
        self.assertEqual(result.usage_tokens, 7)
        self.assertEqual(result.usage_spend_units, 3)
        self.assertEqual(
            [(chunk.stream, chunk.text) for chunk in chunks],
            [
                (ProductOutputStream.STDOUT, "hel"),
                (ProductOutputStream.STDOUT, "lo"),
                (ProductOutputStream.STDERR, "warn"),
            ],
        )
        self.assertEqual(document, original)
        self.assertEqual(
            self.meter.begin_calls,
            [
                (
                    "command:one",
                    "product:session:1:command",
                    ProductRuntimeLimits(50, 20, 2),
                )
            ],
        )
        self.assertEqual(len(self.meter.finish_calls), 1)
        self.assertEqual(self.reaper.namespaces, ["product:session:1:command"])

    def test_streamed_output_uses_bounded_evidence_and_forwarding_chunks(self) -> None:
        large_piece = "L" * (128 * 1024 + 3)
        repeated_piece = "abcd" * 1024
        repeat_count = 256
        suffix = "tail"
        complete = large_piece + repeated_piece * repeat_count + suffix

        def run(_context, _control, emit):
            emit(CommandOutputChunk(large_piece))
            for _index in range(repeat_count):
                emit(CommandOutputChunk(repeated_piece))
            return CommandResult(stdout=complete)

        forwarded = hashlib.sha256()
        forwarded_characters = 0
        largest_chunk = 0

        def observe(chunk: ProductOutputChunk) -> None:
            nonlocal forwarded_characters, largest_chunk
            forwarded.update(chunk.text.encode("utf-8"))
            forwarded_characters += len(chunk.text)
            largest_chunk = max(largest_chunk, len(chunk.text))

        result = self._executor(CallableAgent(run)).execute(_request(), observe)

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertEqual(forwarded_characters, len(complete))
        self.assertEqual(forwarded.digest(), hashlib.sha256(complete.encode()).digest())
        self.assertLessEqual(largest_chunk, 64 * 1024)

    def test_utf8_publication_limit_bounds_streamed_and_terminal_output(self) -> None:
        streamed_stdout = "streamed-" + ("\N{ROCKET}" * 1_500)
        stdout_suffix = "stdout-tail-" + ("\N{SNOWMAN}" * 1_500)
        complete_stdout = streamed_stdout + stdout_suffix
        complete_stderr = "structured-stderr-" + ("\N{GLOBE WITH MERIDIANS}" * 1_500)

        def run(_context, _control, emit):
            emit(CommandOutputChunk(streamed_stdout))
            return CommandResult(stdout=complete_stdout, stderr=complete_stderr)

        chunks: list[ProductOutputChunk] = []
        result = self._executor(
            CallableAgent(run),
            publication_chunk_bytes=4_096,
        ).execute(_request(), chunks.append)

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertTrue(chunks)
        self.assertTrue(
            all(len(chunk.text.encode("utf-8")) <= 4_096 for chunk in chunks)
        )
        self.assertEqual(
            "".join(
                chunk.text
                for chunk in chunks
                if chunk.stream is ProductOutputStream.STDOUT
            ),
            complete_stdout,
        )
        self.assertEqual(
            "".join(
                chunk.text
                for chunk in chunks
                if chunk.stream is ProductOutputStream.STDERR
            ),
            complete_stderr,
        )

    def test_default_publication_slicing_remains_character_bounded(self) -> None:
        complete = "x" * 5_000
        chunks: list[ProductOutputChunk] = []

        result = self._executor(
            CallableAgent(
                lambda _context, _control, _emit: CommandResult(stdout=complete)
            )
        ).execute(_request(), chunks.append)

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertEqual([chunk.text for chunk in chunks], [complete])

    def test_publisher_stop_signal_suppresses_noisy_late_output(self) -> None:
        attempted_chunks = 10_000
        cancellation_checks = 0

        def not_cancelled() -> bool:
            nonlocal cancellation_checks
            cancellation_checks += 1
            return False

        def run(_context, _control, emit):
            for _index in range(attempted_chunks):
                emit(CommandOutputChunk("noise"))
            return CommandResult(stdout="noise" * attempted_chunks)

        publication_calls = 0

        def stop_after_first(_chunk: ProductOutputChunk) -> bool:
            nonlocal publication_calls
            publication_calls += 1
            return False

        result = self._executor(CallableAgent(run)).execute(
            _request(cancelled=not_cancelled),
            stop_after_first,
        )

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertEqual(publication_calls, 1)
        self.assertLess(cancellation_checks, 50)

    def test_publisher_stop_avoids_slicing_late_complete_result(self) -> None:
        class SlicedText(str):
            slice_calls = 0

            def __getitem__(self, key):
                if isinstance(key, slice):
                    type(self).slice_calls += 1
                return super().__getitem__(key)

        complete = SlicedText("late" * (256 * 1024))

        def run(_context, _control, _emit):
            return CommandResult(stdout=complete)

        publication_calls = 0

        def stop_after_first(_chunk: ProductOutputChunk) -> bool:
            nonlocal publication_calls
            publication_calls += 1
            return False

        result = self._executor(CallableAgent(run)).execute(
            _request(),
            stop_after_first,
        )

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertEqual(publication_calls, 1)
        self.assertEqual(SlicedText.slice_calls, 1)

    def test_state_update_commits_only_encoded_detached_document(self) -> None:
        document = _document()
        original = deepcopy(document)
        agent = CallableAgent(
            lambda _context, _control, _emit: CommandResult(
                state_update=CommandStateUpdate(cwd="/srv/product")
            )
        )

        result = self._executor(agent).execute(
            _request(document=document), lambda _: None
        )

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        state_effect = result.state_effect
        self.assertIsNotNone(state_effect)
        assert state_effect is not None
        self.assertEqual(state_effect.kind, ProductStateEffectKind.COMMITTED)
        provisional_document = state_effect.provisional_document
        self.assertIsNotNone(provisional_document)
        assert provisional_document is not None
        self.assertEqual(
            provisional_document["cwd"],
            "/srv/product",
        )
        self.assertEqual(document, original)

    def test_noop_and_invalid_updates_are_none_and_rejected(self) -> None:
        cases = (
            (
                CommandStateUpdate(cwd="/home/guest"),
                ProductStateEffectKind.NONE,
                None,
            ),
            (
                CommandStateUpdate(cwd=object()),  # type: ignore[arg-type]
                ProductStateEffectKind.REJECTED,
                "invalid_state_update",
            ),
        )
        for update, expected_kind, expected_reason in cases:
            with self.subTest(kind=expected_kind):
                agent = CallableAgent(
                    lambda _context, _control, _emit, update=update: CommandResult(
                        state_update=update
                    )
                )
                result = self._executor(agent).execute(_request(), lambda _: None)

                self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
                state_effect = result.state_effect
                self.assertIsNotNone(state_effect)
                assert state_effect is not None
                self.assertEqual(state_effect.kind, expected_kind)
                self.assertEqual(state_effect.safe_reason, expected_reason)

    def test_maps_guest_signal_and_agent_interruption_without_state(self) -> None:
        cases = (
            (
                CommandResult(signal="SIGTERM", exit_status=143),
                ProductExecutionOutcome.COMPLETED,
                ProductGuestTerminationKind.SIGNALED,
                None,
            ),
            (
                CommandResult(interrupted=True, signal="SIGINT", exit_status=130),
                ProductExecutionOutcome.INTERRUPTED,
                None,
                "agent_interrupted",
            ),
        )
        for command_result, outcome, termination, reason in cases:
            with self.subTest(outcome=outcome):
                result = self._executor(
                    CallableAgent(
                        lambda _context, _control, _emit, value=command_result: value
                    )
                ).execute(_request(), lambda _: None)

                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.reason, reason)
                if termination is None:
                    self.assertIsNone(result.guest_termination)
                else:
                    guest_termination = result.guest_termination
                    self.assertIsNotNone(guest_termination)
                    assert guest_termination is not None
                    self.assertEqual(guest_termination.kind, termination)

    def test_predispatch_cancel_and_deadline_skip_codec_agent_and_meter(self) -> None:
        cases = (
            (
                lambda: True,
                1_000,
                100,
                ProductExecutionOutcome.INTERRUPTED,
                "cancelled_before_dispatch",
            ),
            (
                lambda: False,
                100,
                100,
                ProductExecutionOutcome.FAILED,
                "command_deadline_exceeded",
            ),
        )
        for cancelled, deadline, now, outcome, reason in cases:
            with self.subTest(reason=reason):
                self.clock.set(now)
                agent = CallableAgent(
                    lambda _context, _control, _emit: self.fail("agent dispatched")
                )
                result = self._executor(agent).execute(
                    _request(cancelled=cancelled, deadline_ms=deadline),
                    lambda _: None,
                )

                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.reason, reason)
                self.assertEqual(agent.contexts, [])
                self.assertEqual(self.meter.begin_calls, [])
                self.assertEqual(
                    self.reaper.namespaces,
                    ["product:session:1:command"],
                )
                self.reaper.namespaces.clear()

    def test_watchdog_propagates_inflight_cancellation_before_reap(self) -> None:
        cancelled = threading.Event()
        agent_started = threading.Event()
        cancellation_callback = threading.Event()
        physical_done = threading.Event()

        def run(_context, control, _emit):
            control.add_cancel_callback(cancellation_callback.set)
            agent_started.set()
            self.assertTrue(cancellation_callback.wait(1))
            physical_done.set()
            return CommandResult(interrupted=True)

        self.reaper.before_reap = lambda: self.assertTrue(physical_done.is_set())
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self._executor(CallableAgent(run)).execute,
                _request(cancelled=cancelled.is_set),
                lambda _: None,
            )
            self.assertTrue(agent_started.wait(1))
            cancelled.set()
            result = future.result(timeout=2)

        self.assertEqual(result.outcome, ProductExecutionOutcome.INTERRUPTED)
        self.assertEqual(result.reason, "cancelled")
        self.assertEqual(len(self.meter.finish_calls), 1)
        self.assertEqual(self.reaper.namespaces, ["product:session:1:command"])

    def test_watchdog_propagates_inflight_deadline(self) -> None:
        agent_started = threading.Event()
        deadline_callback = threading.Event()

        def run(_context, control, _emit):
            control.add_cancel_callback(deadline_callback.set)
            agent_started.set()
            self.assertTrue(deadline_callback.wait(1))
            return CommandResult(interrupted=True)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self._executor(CallableAgent(run)).execute,
                _request(deadline_ms=1_000),
                lambda _: None,
            )
            self.assertTrue(agent_started.wait(1))
            self.clock.set(1_000)
            result = future.result(timeout=2)

        self.assertEqual(result.outcome, ProductExecutionOutcome.FAILED)
        self.assertEqual(result.reason, "command_deadline_exceeded")

    def test_watchdog_close_is_bounded_if_cancel_callback_blocks(self) -> None:
        callback_started = threading.Event()
        release_callback = threading.Event()
        control = ProductRuntimeControl(
            cancelled=lambda: False,
            deadline_ms=101,
            clock_ms=self.clock,
            poll_interval_ms=1,
            limits=ProductRuntimeLimits(50, 20, 1),
        )

        def block_callback() -> None:
            callback_started.set()
            release_callback.wait(2)

        control.add_cancel_callback(block_callback)
        control.start_watchdog()
        self.clock.set(101)
        self.assertTrue(callback_started.wait(1))
        closer = threading.Thread(target=control.close_watchdog)
        closer.start()
        closer.join(0.5)
        try:
            self.assertFalse(closer.is_alive())
        finally:
            release_callback.set()
            closer.join(1)
            control.close_watchdog()

    def test_cancel_during_meter_setup_prevents_agent_dispatch(self) -> None:
        cancelled = threading.Event()
        meter_entered = threading.Event()
        release_meter = threading.Event()

        def hold_meter() -> None:
            meter_entered.set()
            self.assertTrue(release_meter.wait(1))

        self.meter.before_begin = hold_meter
        agent = CallableAgent(
            lambda _context, _control, _emit: self.fail("agent dispatched")
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self._executor(agent).execute,
                _request(cancelled=cancelled.is_set),
                lambda _: None,
            )
            self.assertTrue(meter_entered.wait(1))
            cancelled.set()
            release_meter.set()
            result = future.result(timeout=2)

        self.assertEqual(result.outcome, ProductExecutionOutcome.INTERRUPTED)
        self.assertEqual(result.reason, "cancelled_before_dispatch")
        self.assertEqual(agent.contexts, [])
        self.assertEqual(len(self.meter.finish_calls), 1)
        self.assertEqual(self.reaper.namespaces, ["product:session:1:command"])

    def test_late_cancel_during_cleanup_prevents_completion(self) -> None:
        cancelled = threading.Event()
        reaper_entered = threading.Event()
        release_reaper = threading.Event()
        cancellation_callback = threading.Event()

        def run(_context, control, _emit):
            control.add_cancel_callback(cancellation_callback.set)
            return CommandResult(state_update=CommandStateUpdate(cwd="/late"))

        def hold_reaper() -> None:
            reaper_entered.set()
            self.assertTrue(release_reaper.wait(1))

        self.reaper.before_reap = hold_reaper
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self._executor(CallableAgent(run)).execute,
                _request(cancelled=cancelled.is_set),
                lambda _: None,
            )
            self.assertTrue(reaper_entered.wait(1))
            cancelled.set()
            self.assertTrue(cancellation_callback.wait(1))
            release_reaper.set()
            result = future.result(timeout=2)

        self.assertEqual(result.outcome, ProductExecutionOutcome.INTERRUPTED)
        self.assertEqual(result.reason, "cancelled")
        self.assertIsNone(result.state_effect)

    def test_attempt_and_usage_caps_fail_closed_with_measured_usage(self) -> None:
        def exceed_attempts(_context, control, _emit):
            self.assertEqual(control.claim_attempt(), 1)
            self.assertIsNone(control.claim_attempt())
            return CommandResult()

        attempt_result = self._executor(CallableAgent(exceed_attempts)).execute(
            _request(max_attempts=1),
            lambda _: None,
        )
        self.assertEqual(attempt_result.outcome, ProductExecutionOutcome.FAILED)
        self.assertEqual(attempt_result.reason, "attempt_limit_exceeded")

        self.meter = FakeMeter(ProductRuntimeUsage(tokens=51, spend_units=3))
        usage_result = self._executor(
            CallableAgent(lambda _context, _control, _emit: CommandResult())
        ).execute(_request(max_tokens=50), lambda _: None)
        self.assertEqual(usage_result.outcome, ProductExecutionOutcome.FAILED)
        self.assertEqual(usage_result.reason, "resource_usage_exceeded")
        self.assertEqual(usage_result.usage_tokens, 51)

    def test_attempt_cap_requires_an_enforcing_provider_adapter(self) -> None:
        observed_attempts: list[int] = []

        def bypass_claim(_context, control, _emit):
            observed_attempts.append(control.attempts)
            return CommandResult()

        result = self._executor(CallableAgent(bypass_claim)).execute(
            _request(max_attempts=1),
            lambda _chunk: None,
        )

        # ProductRuntimeControl cannot count provider attempts hidden inside an
        # injected agent.  This explicit evidence keeps the live-capability gate
        # closed until the real provider seam makes claim_attempt mandatory.
        self.assertEqual(observed_attempts, [0])
        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)

    def test_agent_usage_and_cleanup_failures_still_finish_and_reap(self) -> None:
        log: list[str] = []
        self.meter.log = log
        self.reaper.log = log

        def fail(_context, _control, _emit):
            log.append("agent.physical_done")
            raise RuntimeError("unsafe provider detail")

        result = self._executor(CallableAgent(fail)).execute(_request(), lambda _: None)
        self.assertEqual(result.outcome, ProductExecutionOutcome.FAILED)
        self.assertEqual(result.reason, "agent_execution_failed")
        self.assertEqual(result.usage_tokens, 7)
        self.assertEqual(log, ["agent.physical_done", "meter.finish", "reaper.reap"])

        self.meter.fail_finish = True
        meter_result = self._executor(
            CallableAgent(lambda _context, _control, _emit: CommandResult())
        ).execute(_request(), lambda _: None)
        self.assertEqual(meter_result.reason, "usage_measurement_failed")
        self.assertEqual(meter_result.usage_tokens, 50)
        self.assertEqual(meter_result.usage_spend_units, 20)
        self.assertEqual(
            self.reaper.namespaces,
            ["product:session:1:command", "product:session:1:command"],
        )
        self.meter.fail_finish = False

        self.reaper.fail = True
        cleanup_result = self._executor(
            CallableAgent(lambda _context, _control, _emit: CommandResult())
        ).execute(_request(), lambda _: None)
        self.assertEqual(cleanup_result.reason, "checkpoint_cleanup_failed")
        self.assertEqual(cleanup_result.usage_tokens, 7)

    def test_combined_post_dispatch_failures_charge_full_reservation(self) -> None:
        self.meter.fail_finish = True
        self.reaper.fail = True

        cleanup_result = self._executor(
            CallableAgent(lambda _context, _control, _emit: CommandResult())
        ).execute(_request(), lambda _: None)

        self.assertEqual(cleanup_result.reason, "checkpoint_cleanup_failed")
        self.assertEqual(cleanup_result.usage_tokens, 50)
        self.assertEqual(cleanup_result.usage_spend_units, 20)

        agent_dispatched = False

        def observation_fails_after_dispatch(_context, _control, _emit):
            nonlocal agent_dispatched
            agent_dispatched = True
            return CommandResult()

        def eventually_invalid_observation() -> bool:
            if agent_dispatched:
                raise RuntimeError("unsafe cancellation detail")
            return False

        self.reaper.fail = False
        observation_result = self._executor(
            CallableAgent(observation_fails_after_dispatch)
        ).execute(
            _request(cancelled=eventually_invalid_observation),
            lambda _: None,
        )

        self.assertEqual(
            observation_result.reason,
            "cancellation_observation_failed",
        )
        self.assertEqual(observation_result.usage_tokens, 50)
        self.assertEqual(observation_result.usage_spend_units, 20)

    def test_fatal_final_observation_always_stops_watchdog(self) -> None:
        class FatalObservation(BaseException):
            pass

        test_thread_id = threading.get_ident()
        agent_returned = threading.Event()
        controls: list[ProductRuntimeControl] = []

        def run(_context, control, _emit):
            controls.append(control)
            agent_returned.set()
            return CommandResult()

        def fatal_after_agent_returns() -> bool:
            if agent_returned.is_set() and threading.get_ident() == test_thread_id:
                raise FatalObservation("unsafe fatal observation detail")
            return False

        with self.assertRaises(FatalObservation):
            self._executor(CallableAgent(run)).execute(
                _request(cancelled=fatal_after_agent_returns),
                lambda _: None,
            )

        self.assertEqual(len(controls), 1)
        control = controls[0]
        self.assertTrue(control._monitor_done.is_set())
        monitor = control._monitor
        if monitor is not None:
            self.assertFalse(monitor.is_alive())
        self.assertEqual(self.reaper.namespaces, ["product:session:1:command"])

    def test_invalid_provisional_output_and_terminal_shapes_fail_safely(self) -> None:
        self.codec.fail_decode = True
        invalid_state = self._executor(
            CallableAgent(lambda _context, _control, _emit: CommandResult())
        ).execute(_request(), lambda _: None)
        self.assertEqual(invalid_state.reason, "invalid_provisional_state")
        self.assertEqual(self.meter.begin_calls, [])
        self.assertEqual(self.reaper.namespaces, ["product:session:1:command"])

        self.codec = FakeCodec()

        def mismatched(_context, _control, emit):
            emit(CommandOutputChunk("already"))
            return CommandResult(stdout="different")

        invalid_result = self._executor(CallableAgent(mismatched)).execute(
            _request(),
            lambda _: None,
        )
        self.assertEqual(invalid_result.reason, "invalid_agent_result")

    def test_output_delivery_failure_cannot_publish_state(self) -> None:
        def run(_context, _control, emit):
            emit(CommandOutputChunk("candidate"))
            return CommandResult(state_update=CommandStateUpdate(cwd="/unsafe"))

        def reject_output(_chunk) -> None:
            raise RuntimeError("consumer unavailable")

        result = self._executor(CallableAgent(run)).execute(
            _request(),
            reject_output,
        )
        self.assertEqual(result.outcome, ProductExecutionOutcome.FAILED)
        self.assertEqual(result.reason, "output_delivery_failed")
        self.assertIsNone(result.state_effect)
        self.assertEqual(self.reaper.namespaces, ["product:session:1:command"])

    def test_observation_failure_is_typed_and_never_dispatches(self) -> None:
        agent = CallableAgent(
            lambda _context, _control, _emit: self.fail("agent dispatched")
        )
        result = self._executor(agent).execute(
            _request(cancelled=cast(Callable[[], bool], lambda: 1)),
            lambda _: None,
        )

        self.assertEqual(result.outcome, ProductExecutionOutcome.FAILED)
        self.assertEqual(result.reason, "cancellation_observation_failed")
        self.assertEqual(agent.contexts, [])
        self.assertEqual(self.reaper.namespaces, ["product:session:1:command"])

    def test_store_free_dreamland_codec_serializes_exact_state(self) -> None:
        codec = DreamlandStateProfileCodec()
        profile_document = _exact_profile_document()
        runtime = codec.decode(
            profile_document,
            checkpoint_namespace="product:isolated:checkpoint",
        )
        runtime.session.cwd = "/srv/isolated"

        encoded = codec.encode(runtime)

        self.assertEqual(encoded["name"], "profile-one")
        state = encoded["state"]
        self.assertIsInstance(state, Mapping)
        assert isinstance(state, Mapping)
        self.assertEqual(state["cwd"], "/srv/isolated")
        environment = state["environment_metadata"]
        self.assertIsInstance(environment, Mapping)
        assert isinstance(environment, Mapping)
        metadata = environment["metadata"]
        self.assertEqual(metadata, {"safe": "kept"})
        self.assertEqual(
            runtime.session.langfuse_trace_id, "product:isolated:checkpoint"
        )

    def test_world_profile_mutation_promotes_to_exact_provisional_state(self) -> None:
        executor = ProductRuntimeCommandExecutor(
            agent=CallableAgent(
                lambda _context, _control, _emit: CommandResult(
                    state_update=CommandStateUpdate(cwd="/srv/persistent")
                )
            ),
            state_codec=DreamlandStateProfileCodec(),
            checkpoint_reaper=self.reaper,
            usage_meter=self.meter,
            clock_ms=self.clock,
            cancellation_poll_interval_ms=1,
        )

        result = executor.execute(
            _request(document=_world_profile_document()),
            lambda _: None,
        )

        state_effect = result.state_effect
        self.assertIsNotNone(state_effect)
        assert state_effect is not None
        self.assertEqual(state_effect.kind, ProductStateEffectKind.COMMITTED)
        provisional = state_effect.provisional_document
        self.assertIsNotNone(provisional)
        assert provisional is not None
        self.assertEqual(provisional["level"], "exact")
        state = provisional["state"]
        self.assertIsInstance(state, Mapping)
        assert isinstance(state, Mapping)
        self.assertEqual(state["cwd"], "/srv/persistent")

    def test_module_has_no_legacy_runtime_or_transport_imports(self) -> None:
        source_path = (
            Path(__file__).parents[1]
            / "src"
            / "dreamland"
            / "product"
            / "runtime_executor.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        self.assertTrue(
            {
                "dreamland.backend",
                "dreamland.agent_core",
                "dreamland.agent_shell",
                "dreamland.admin_interface",
            }.isdisjoint(imported)
        )

    def _executor(
        self,
        agent: CallableAgent,
        *,
        publication_chunk_bytes: int | None = None,
    ) -> ProductRuntimeCommandExecutor:
        return ProductRuntimeCommandExecutor(
            agent=agent,
            state_codec=self.codec,
            checkpoint_reaper=self.reaper,
            usage_meter=self.meter,
            clock_ms=self.clock,
            cancellation_poll_interval_ms=1,
            publication_chunk_bytes=publication_chunk_bytes,
        )


def _request(
    *,
    document: dict[str, object] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    deadline_ms: int = 1_000,
    max_tokens: int = 50,
    max_spend_units: int = 20,
    max_attempts: int = 2,
) -> ProductExecutionRequest:
    return ProductExecutionRequest(
        service_epoch="epoch:one",
        session_id="session:one",
        command_id="command:one",
        slot_generation=1,
        canonical_command="printf safe",
        provisional_document=document if document is not None else _document(),
        checkpoint_namespace="product:session:1:command",
        deadline_ms=deadline_ms,
        max_tokens=max_tokens,
        max_spend_units=max_spend_units,
        max_attempts=max_attempts,
        cancelled=cancelled,
    )


def _document() -> dict[str, object]:
    return {
        "cwd": "/home/guest",
        "world_state": {
            "active_host": "ubuntu",
            "hosts_by_name": {"ubuntu": {"hostname": "ubuntu"}},
        },
    }


def _exact_profile_document() -> dict[str, object]:
    return {
        "kind": "dreamland.state_profile",
        "schema_version": 2,
        "name": "profile-one",
        "level": "exact",
        "created_at": "2026-08-23T00:00:00+00:00",
        "state": {
            "username": "guest",
            "login_username": "guest",
            "cwd": "/home/guest",
            "host_identity": {"hostname": "ubuntu"},
            "environment_metadata": {
                "connection": {},
                "metadata": {"safe": "kept"},
            },
            "world_state": {
                "active_host": "ubuntu",
                "entry_host": "ubuntu",
                "hosts_by_name": {"ubuntu": {"hostname": "ubuntu", "login_banner": ""}},
            },
        },
    }


def _world_profile_document() -> dict[str, object]:
    return {
        "kind": "dreamland.state_profile",
        "schema_version": 2,
        "name": "world-one",
        "level": "world",
        "created_at": "2026-08-23T00:00:00+00:00",
        "state": {
            "world_state": {
                "active_host": "ubuntu",
                "entry_host": "ubuntu",
                "hosts_by_name": {"ubuntu": {"hostname": "ubuntu", "login_banner": ""}},
            }
        },
    }


if __name__ == "__main__":
    unittest.main()
