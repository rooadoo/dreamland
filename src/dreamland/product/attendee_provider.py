"""Attendee-only provider worker boundary with hard parent-side deadlines.

The module is inert at import and construction.  An explicit ``start`` call
creates a fixed set of spawn-process workers.  Each worker constructs its
provider executor inside the child process, receives only bounded JSON command
DTOs, and returns bounded JSON output/result DTOs.  Product callbacks,
authorities, bearer credentials, and provider credentials never cross the
command IPC boundary.

Killing a local worker closes DREAMLAND's publication and process boundary.  It
does *not* prove that an already-dispatched upstream provider request stopped or
that provider billing ceased.  Ambiguous and forcibly terminated dispatches
therefore consume the request's full token and spend reservation.

The documented bounds cover waits, TERM/KILL phases, and controller-thread
joins owned by this pool.  Local ``cancelled``/``emit`` callbacks are trusted
protocol collaborators and must return promptly; Python and kernel process/IPC
primitives are likewise outside an in-process scheduler's ability to preempt.
A production deployment still needs an external supervisor deadline around the
whole attendee worker service.  Local SIGKILL is containment evidence, never
evidence that an upstream provider completed cancellation.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from multiprocessing.connection import Connection
from typing import Protocol

from dreamland.product.execution import (
    ProductCommandExecutor,
    ProductExecutionOutcome,
    ProductExecutionRequest,
    ProductExecutionResult,
    ProductGuestTermination,
    ProductGuestTerminationKind,
    ProductOutputChunk,
    ProductOutputPublisher,
    ProductOutputStream,
    ProductStateEffect,
    ProductStateEffectKind,
)

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = 1
_MAX_SAFE_INTEGER = (1 << 63) - 1
_MAX_LIMIT = 1 << 31
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 65_536
_MAX_IDENTITY_BYTES = 512
_POLL_INTERVAL_MS = 10


class AttendeeProviderLifecycle(StrEnum):
    """Observable lifecycle for one explicitly started provider pool."""

    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    CLOSED = "closed"
    FAILED = "failed"


class AttendeeProviderRejectionReason(StrEnum):
    """Stable safe reasons returned through ``ProductExecutionResult``."""

    PROVIDER_STARTING = "provider_starting"
    PROVIDER_DRAINING = "provider_draining"
    PROVIDER_CLOSED = "provider_closed"
    PROVIDER_FAILED = "provider_failed"
    PROVIDER_CAPACITY_EXHAUSTED = "provider_capacity_exhausted"
    PROVIDER_DEADLINE_EXCEEDED = "provider_deadline_exceeded"
    PROVIDER_CANCELLED_BEFORE_DISPATCH = "provider_cancelled_before_dispatch"
    PROVIDER_REQUEST_INVALID = "provider_request_invalid"
    PROVIDER_DISPATCH_FAILED = "provider_dispatch_failed"
    PROVIDER_REQUEST_TIMEOUT = "provider_request_timeout"
    PROVIDER_CANCEL_TIMEOUT = "provider_cancel_timeout"
    PROVIDER_PROTOCOL_ERROR = "provider_protocol_error"
    PROVIDER_WORKER_FAILED = "provider_worker_failed"
    PROVIDER_OUTPUT_FAILED = "provider_output_failed"
    PROVIDER_SHUTDOWN = "provider_shutdown"


class UpstreamTerminationEvidence(StrEnum):
    """Evidence available after DREAMLAND locally terminates a worker."""

    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class AttendeeProviderPoolLimits:
    """Finite process, deadline, and IPC limits for an attendee pool."""

    worker_capacity: int = 4
    startup_timeout_ms: int = 10_000
    dispatch_timeout_ms: int = 1_000
    request_timeout_ms: int = 120_000
    cancellation_grace_ms: int = 2_000
    shutdown_timeout_ms: int = 10_000
    terminate_grace_ms: int = 250
    max_command_bytes: int = 16_384
    max_state_bytes: int = 1_048_576
    max_output_chunk_bytes: int = 16_384
    max_output_bytes: int = 1_048_576
    max_output_chunks: int = 4_096
    max_ipc_frame_bytes: int = 2_097_152

    def __post_init__(self) -> None:
        for name in (
            "worker_capacity",
            "startup_timeout_ms",
            "dispatch_timeout_ms",
            "request_timeout_ms",
            "cancellation_grace_ms",
            "shutdown_timeout_ms",
            "terminate_grace_ms",
            "max_command_bytes",
            "max_state_bytes",
            "max_output_chunk_bytes",
            "max_output_bytes",
            "max_output_chunks",
            "max_ipc_frame_bytes",
        ):
            _require_positive_limit(getattr(self, name), name)
        if self.max_output_chunk_bytes > self.max_output_bytes:
            raise ValueError("max_output_chunk_bytes exceeds max_output_bytes")
        if self.max_command_bytes >= self.max_ipc_frame_bytes:
            raise ValueError("max_command_bytes must fit within one IPC frame")
        if self.max_state_bytes >= self.max_ipc_frame_bytes:
            raise ValueError("max_state_bytes must fit within one IPC frame")
        if self.max_output_chunk_bytes >= self.max_ipc_frame_bytes:
            raise ValueError("max_output_chunk_bytes must fit within one IPC frame")

    def shutdown_upper_bound_ms(self, timeout_ms: int | None = None) -> int:
        """Return the finite parent-side shutdown bound.

        Shutdown first allows at most ``timeout_ms`` for ordinary drain, then
        allows one shared grace period after TERM and one after KILL.  A final
        shared grace period joins replacement and request-send controller
        threads after their child has been fenced.  The bound does not claim that an upstream
        provider request or its billing stopped when a local worker did.
        """
        timeout = self.shutdown_timeout_ms if timeout_ms is None else timeout_ms
        _require_positive_limit(timeout, "timeout_ms")
        return _safe_add(timeout, self.terminate_grace_ms * 3)

    def validate_runtime_document(
        self,
        document: Mapping[str, object],
    ) -> dict[str, object]:
        """Validate and detach state with the exact request-IPC policy."""
        return _normalize_json_document(
            document,
            self.max_state_bytes,
            "runtime document",
        )


@dataclass(frozen=True, slots=True)
class AttendeeProviderPoolSnapshot:
    """Secret- and identity-free provider-pool readiness projection."""

    lifecycle: AttendeeProviderLifecycle
    worker_capacity: int
    ready_workers: int
    active_workers: int
    available_workers: int
    accepted_dispatches: int
    completed_dispatches: int
    ambiguous_dispatches: int
    locally_forced_workers: int
    startup_failures: int
    forced_upstream_termination_evidence: UpstreamTerminationEvidence


class AttendeeProviderPoolError(RuntimeError):
    """Safe lifecycle failure raised by explicit pool control operations."""

    def __init__(self, reason: AttendeeProviderRejectionReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class AttendeeProviderWorkerFactory(Protocol):
    """Construct one dedicated executor inside a spawned child process.

    A production factory must load attendee-only credentials in the child.  It
    must not capture a Stage, Forge, ordinary SSH, admin, or MCP authority.
    The returned executor is reused serially and therefore must not retain a
    prior request's identity, provisional document, cancellation callback, or
    command-scoped checkpoint after that request reaches a terminal result.
    """

    def __call__(self) -> ProductCommandExecutor: ...


@dataclass(slots=True)
class _WorkerSlot:
    index: int
    process: object
    connection: Connection
    cancel_event: object
    suppress_output_event: object
    active: bool = False
    retired: bool = False
    forced_reason: AttendeeProviderRejectionReason | None = None
    shutdown_sent: bool = False
    ambiguity_recorded: bool = False
    force_recorded: bool = False
    stop_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(frozen=True, slots=True)
class _StopSlotResult:
    """Bounded local-stop outcome retained by non-shutdown callers."""

    forced: bool
    newly_forced: bool
    survived: bool


class _WorkerPublication:
    """Per-dispatch child publisher whose fence cannot bleed into a later job."""

    def __init__(
        self,
        connection: Connection,
        cancel_event: object,
        suppress_output_event: object,
        limits: AttendeeProviderPoolLimits,
    ) -> None:
        self._connection = connection
        self._cancel_event = cancel_event
        self._suppress_output_event = suppress_output_event
        self._limits = limits
        self._lock = threading.Lock()
        self._open = True
        self._output_bytes = 0
        self._output_chunks = 0

    def emit(self, chunk: ProductOutputChunk) -> bool:
        with self._lock:
            if (
                not self._open
                or self._cancel_event.is_set()
                or self._suppress_output_event.is_set()
            ):
                return False
            output_message = _output_to_message(chunk, self._limits)
            encoded = _encode_message(output_message)
            self._output_chunks += 1
            self._output_bytes += len(chunk.text.encode("utf-8"))
            if (
                self._output_chunks > self._limits.max_output_chunks
                or self._output_bytes > self._limits.max_output_bytes
                or len(encoded) > self._limits.max_ipc_frame_bytes
            ):
                self._open = False
                raise ValueError("worker output exceeds IPC limits")
            self._connection.send_bytes(encoded)
            return True

    def close(self) -> None:
        with self._lock:
            self._open = False


class AttendeeProviderPool:
    """Fixed-capacity spawn-process executor for attendee provider work.

    The pool implements :class:`ProductCommandExecutor`.  ``execute`` returns
    safe terminal results for lifecycle, admission, protocol, and deadline
    failures rather than leaking child/provider exceptions.
    """

    def __init__(
        self,
        worker_factory: AttendeeProviderWorkerFactory,
        *,
        limits: AttendeeProviderPoolLimits | None = None,
        process_context: object | None = None,
        clock_ms: Callable[[], int] | None = None,
        steady_ms: Callable[[], int] | None = None,
    ) -> None:
        if not callable(worker_factory):
            raise TypeError("worker_factory must be callable")
        if clock_ms is not None and not callable(clock_ms):
            raise TypeError("clock_ms must be callable")
        if steady_ms is not None and not callable(steady_ms):
            raise TypeError("steady_ms must be callable")
        self._worker_factory = worker_factory
        self._limits = limits or AttendeeProviderPoolLimits()
        self._context = process_context or multiprocessing.get_context("spawn")
        get_start_method = getattr(self._context, "get_start_method", None)
        if not callable(get_start_method) or get_start_method() != "spawn":
            raise ValueError("process_context must use the spawn start method")
        self._clock_ms = clock_ms or _utc_ms
        self._steady_ms = steady_ms or _monotonic_ms
        self._condition = threading.Condition(threading.RLock())
        self._lifecycle = AttendeeProviderLifecycle.STARTING
        self._start_called = False
        self._slots: dict[int, _WorkerSlot] = {}
        self._spawning_slots: dict[int, _WorkerSlot] = {}
        self._drain_deadline_ms: int | None = None
        self._replacement_threads: set[threading.Thread] = set()
        self._send_threads: set[threading.Thread] = set()
        self._replacement_indices: set[int] = set()
        self._closing = threading.Event()
        self._shutdown_in_progress = False
        self._accepted_dispatches = 0
        self._completed_dispatches = 0
        self._ambiguous_dispatches = 0
        self._locally_forced_workers = 0
        self._startup_failures = 0

    @property
    def limits(self) -> AttendeeProviderPoolLimits:
        return self._limits

    def start(self) -> AttendeeProviderPoolSnapshot:
        """Start every configured worker and become ready only when all reply."""
        with self._condition:
            if self._lifecycle in {
                AttendeeProviderLifecycle.DRAINING,
                AttendeeProviderLifecycle.CLOSED,
                AttendeeProviderLifecycle.FAILED,
            }:
                raise AttendeeProviderPoolError(self._lifecycle_reason_locked())
            if self._start_called:
                if self._lifecycle is AttendeeProviderLifecycle.READY:
                    return self._snapshot_locked()
                raise AttendeeProviderPoolError(self._lifecycle_reason_locked())
            self._start_called = True
            self._lifecycle = AttendeeProviderLifecycle.STARTING

        deadline_ms = _safe_add(self._steady_now(), self._limits.startup_timeout_ms)
        created: list[_WorkerSlot] = []
        try:
            for index in range(self._limits.worker_capacity):
                created.append(self._spawn_ready_worker(index, deadline_ms))
        except Exception:
            surviving_ids: set[int] = set()
            for slot in created:
                stopped = self._stop_slot(
                    slot,
                    deadline_ms=deadline_ms,
                    force_if_needed=True,
                )
                self._record_nonshutdown_stop(
                    slot,
                    stopped,
                    source="initial_start_rollback",
                )
                if stopped.survived:
                    surviving_ids.add(id(slot))
            with self._condition:
                for slot in created:
                    if id(slot) not in surviving_ids:
                        self._spawning_slots.pop(id(slot), None)
                if self._lifecycle not in {
                    AttendeeProviderLifecycle.DRAINING,
                    AttendeeProviderLifecycle.CLOSED,
                }:
                    self._startup_failures += 1
                    self._lifecycle = AttendeeProviderLifecycle.FAILED
                self._condition.notify_all()
            logger.error("attendee_provider_pool_start_failed")
            with self._condition:
                reason = self._lifecycle_reason_locked()
            raise AttendeeProviderPoolError(reason) from None

        with self._condition:
            publish = (
                self._lifecycle is AttendeeProviderLifecycle.STARTING
                and not self._closing.is_set()
            )
            if publish:
                self._slots = {slot.index: slot for slot in created}
                for slot in created:
                    self._spawning_slots.pop(id(slot), None)
                self._lifecycle = AttendeeProviderLifecycle.READY
                snapshot = self._snapshot_locked()
                self._condition.notify_all()
            else:
                reason = self._lifecycle_reason_locked()
        if not publish:
            surviving_ids = set()
            for slot in created:
                stopped = self._stop_slot(
                    slot,
                    deadline_ms=self._steady_now(),
                    force_if_needed=True,
                )
                self._record_nonshutdown_stop(
                    slot,
                    stopped,
                    source="initial_start_publish_cleanup",
                )
                if stopped.survived:
                    surviving_ids.add(id(slot))
            with self._condition:
                for slot in created:
                    if id(slot) not in surviving_ids:
                        self._spawning_slots.pop(id(slot), None)
                reason = self._lifecycle_reason_locked()
                self._condition.notify_all()
            raise AttendeeProviderPoolError(reason)
        logger.info(
            "attendee_provider_pool_ready workers=%d",
            self._limits.worker_capacity,
        )
        return snapshot

    def execute(
        self,
        request: ProductExecutionRequest,
        emit: ProductOutputPublisher,
    ) -> ProductExecutionResult:
        """Execute one request without accepting output beyond its parent fence."""
        if not isinstance(request, ProductExecutionRequest) or not callable(emit):
            return _predispatch_failure(
                AttendeeProviderRejectionReason.PROVIDER_REQUEST_INVALID
            )
        try:
            request_frame = _encode_request_frame(request, self._limits)
        except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
            return _predispatch_failure(
                AttendeeProviderRejectionReason.PROVIDER_REQUEST_INVALID
            )

        acquired = self._acquire_worker(request)
        if isinstance(acquired, ProductExecutionResult):
            return acquired
        slot = acquired
        try:
            slot.cancel_event.clear()
            slot.suppress_output_event.clear()
        except Exception:
            logger.exception("attendee_provider_dispatch_event_reset_failed")
            return self._predispatch_slot_failure(
                slot,
                AttendeeProviderRejectionReason.PROVIDER_DISPATCH_FAILED,
            )
        send_reason, send_thread = self._send_frame_bounded(
            slot,
            request_frame,
            request,
        )
        if send_reason is not None:
            result = self._ambiguous_failure(
                slot,
                send_reason,
                request,
            )
            if send_thread is not None:
                send_thread.join(self._limits.terminate_grace_ms / 1_000)
            if send_thread is not None and send_thread.is_alive():
                with self._condition:
                    self._lifecycle = AttendeeProviderLifecycle.FAILED
                    self._condition.notify_all()
                logger.critical("attendee_provider_dispatch_send_survived_fence")
            return result

        with self._condition:
            self._accepted_dispatches += 1

        return self._await_result(slot, request, emit)

    def _send_frame_bounded(
        self,
        slot: _WorkerSlot,
        frame: bytes,
        request: ProductExecutionRequest,
    ) -> tuple[
        AttendeeProviderRejectionReason | None,
        threading.Thread | None,
    ]:
        """Transmit one request without blocking the dispatch caller forever."""
        done = threading.Event()
        failed = threading.Event()

        def send() -> None:
            try:
                slot.connection.send_bytes(frame)
            except Exception:
                failed.set()
            finally:
                done.set()
                with self._condition:
                    self._send_threads.discard(threading.current_thread())
                    self._condition.notify_all()

        thread = threading.Thread(
            target=send,
            name=f"attendee-provider-send-{slot.index}",
            daemon=True,
        )
        with self._condition:
            self._send_threads.add(thread)
            try:
                thread.start()
            except Exception:
                self._send_threads.discard(thread)
                self._condition.notify_all()
                logger.exception("attendee_provider_dispatch_send_thread_failed")
                return (
                    AttendeeProviderRejectionReason.PROVIDER_DISPATCH_FAILED,
                    None,
                )
        deadline_ms = _safe_add(self._steady_now(), self._limits.dispatch_timeout_ms)
        while not done.wait(_POLL_INTERVAL_MS / 1_000):
            if self._closing.is_set():
                return AttendeeProviderRejectionReason.PROVIDER_SHUTDOWN, thread
            if self._clock_now() >= request.deadline_ms:
                return (
                    AttendeeProviderRejectionReason.PROVIDER_DEADLINE_EXCEEDED,
                    thread,
                )
            if self._steady_now() >= deadline_ms:
                return (
                    AttendeeProviderRejectionReason.PROVIDER_DISPATCH_FAILED,
                    thread,
                )
        if failed.is_set():
            return AttendeeProviderRejectionReason.PROVIDER_DISPATCH_FAILED, thread
        return None, thread

    def begin_drain(
        self,
        *,
        timeout_ms: int | None = None,
    ) -> AttendeeProviderPoolSnapshot:
        """Reject new dispatch while allowing active work until a hard deadline."""
        timeout = self._limits.shutdown_timeout_ms if timeout_ms is None else timeout_ms
        _require_positive_limit(timeout, "timeout_ms")
        deadline_ms = _safe_add(self._steady_now(), timeout)
        with self._condition:
            if self._lifecycle is AttendeeProviderLifecycle.CLOSED:
                return self._snapshot_locked()
            if self._lifecycle is AttendeeProviderLifecycle.FAILED:
                raise AttendeeProviderPoolError(
                    AttendeeProviderRejectionReason.PROVIDER_FAILED
                )
            self._closing.set()
            self._lifecycle = AttendeeProviderLifecycle.DRAINING
            if self._drain_deadline_ms is None:
                self._drain_deadline_ms = deadline_ms
            else:
                self._drain_deadline_ms = min(
                    self._drain_deadline_ms,
                    deadline_ms,
                )
            snapshot = self._snapshot_locked()
            self._condition.notify_all()
        logger.info("attendee_provider_pool_draining")
        return snapshot

    def shutdown(
        self,
        *,
        timeout_ms: int | None = None,
    ) -> AttendeeProviderPoolSnapshot:
        """Drain and close every local worker and replacement controller.

        The finite upper bound is returned by
        :meth:`AttendeeProviderPoolLimits.shutdown_upper_bound_ms`: one caller
        supplied drain timeout, one shared TERM grace, one shared KILL grace,
        and one shared controller-thread join grace.  All worker phases use
        shared deadlines, so the bound does not grow with worker capacity.
        """
        timeout = self._limits.shutdown_timeout_ms if timeout_ms is None else timeout_ms
        _require_positive_limit(timeout, "timeout_ms")
        shutdown_deadline_ms = _safe_add(self._steady_now(), timeout)
        terminate_deadline_ms = _safe_add(
            shutdown_deadline_ms,
            self._limits.terminate_grace_ms,
        )
        kill_deadline_ms = _safe_add(
            terminate_deadline_ms,
            self._limits.terminate_grace_ms,
        )
        thread_deadline_ms = _safe_add(
            kill_deadline_ms,
            self._limits.terminate_grace_ms,
        )

        with self._condition:
            if self._lifecycle is AttendeeProviderLifecycle.CLOSED:
                return self._snapshot_locked()
            if self._shutdown_in_progress:
                while (
                    self._shutdown_in_progress
                    and self._steady_now() < thread_deadline_ms
                ):
                    remaining_ms = thread_deadline_ms - self._steady_now()
                    self._condition.wait(min(remaining_ms, _POLL_INTERVAL_MS) / 1_000)
                if self._lifecycle is AttendeeProviderLifecycle.CLOSED:
                    return self._snapshot_locked()
                raise AttendeeProviderPoolError(
                    AttendeeProviderRejectionReason.PROVIDER_SHUTDOWN
                )
            self._shutdown_in_progress = True
            self._closing.set()
            if self._lifecycle is not AttendeeProviderLifecycle.FAILED:
                self._lifecycle = AttendeeProviderLifecycle.DRAINING
            if self._drain_deadline_ms is None:
                self._drain_deadline_ms = shutdown_deadline_ms
            else:
                self._drain_deadline_ms = min(
                    self._drain_deadline_ms,
                    shutdown_deadline_ms,
                )
            self._condition.notify_all()
            while self._active_workers_locked():
                self._signal_idle_shutdown_locked()
                remaining_ms = shutdown_deadline_ms - self._steady_now()
                if remaining_ms <= 0:
                    break
                self._condition.wait(min(remaining_ms, _POLL_INTERVAL_MS) / 1_000)

            self._signal_idle_shutdown_locked()
            slots_by_identity = {
                id(slot): slot
                for slot in (*self._slots.values(), *self._spawning_slots.values())
            }
            slots = tuple(slots_by_identity.values())
            for slot in self._slots.values():
                if slot.active and not slot.retired:
                    slot.forced_reason = (
                        AttendeeProviderRejectionReason.PROVIDER_SHUTDOWN
                    )
                    slot.retired = True
            self._condition.notify_all()

        forced, surviving_slots = self._stop_slots_bounded(
            slots,
            graceful_deadline_ms=shutdown_deadline_ms,
            terminate_deadline_ms=terminate_deadline_ms,
            kill_deadline_ms=kill_deadline_ms,
        )
        with self._condition:
            self._locally_forced_workers += forced

        replacements_joined = self._join_replacement_threads(thread_deadline_ms)
        sends_joined = self._join_send_threads(thread_deadline_ms)

        with self._condition:
            if surviving_slots or not replacements_joined or not sends_joined:
                self._lifecycle = AttendeeProviderLifecycle.FAILED
                self._shutdown_in_progress = False
                self._condition.notify_all()
                logger.critical(
                    "attendee_provider_pool_shutdown_failed survivors=%d "
                    "replacement_threads_joined=%s send_threads_joined=%s",
                    len(surviving_slots),
                    replacements_joined,
                    sends_joined,
                )
                raise AttendeeProviderPoolError(
                    AttendeeProviderRejectionReason.PROVIDER_FAILED
                )
            self._slots.clear()
            self._spawning_slots.clear()
            self._lifecycle = AttendeeProviderLifecycle.CLOSED
            self._shutdown_in_progress = False
            snapshot = self._snapshot_locked()
            self._condition.notify_all()
        logger.info(
            "attendee_provider_pool_closed locally_forced_workers=%d",
            snapshot.locally_forced_workers,
        )
        return snapshot

    def snapshot(self) -> AttendeeProviderPoolSnapshot:
        """Return bounded readiness and containment evidence without identities."""
        with self._condition:
            if self._lifecycle is AttendeeProviderLifecycle.READY:
                ready = self._ready_workers_locked()
                if ready != self._limits.worker_capacity:
                    self._lifecycle = AttendeeProviderLifecycle.FAILED
                    self._startup_failures += 1
                    self._condition.notify_all()
            return self._snapshot_locked()

    def _acquire_worker(
        self,
        request: ProductExecutionRequest,
    ) -> _WorkerSlot | ProductExecutionResult:
        dispatch_deadline_ms = _safe_add(
            self._steady_now(), self._limits.dispatch_timeout_ms
        )
        with self._condition:
            while True:
                lifecycle_reason = self._dispatch_rejection_locked()
                starting_in_progress = (
                    lifecycle_reason
                    is AttendeeProviderRejectionReason.PROVIDER_STARTING
                    and self._start_called
                )
                if lifecycle_reason is not None and not starting_in_progress:
                    return _predispatch_failure(lifecycle_reason)
                try:
                    if request.cancelled():
                        return ProductExecutionResult.interrupted(
                            AttendeeProviderRejectionReason.PROVIDER_CANCELLED_BEFORE_DISPATCH
                        )
                except Exception:
                    return ProductExecutionResult.interrupted(
                        AttendeeProviderRejectionReason.PROVIDER_CANCELLED_BEFORE_DISPATCH
                    )
                if self._clock_now() >= request.deadline_ms:
                    return _predispatch_failure(
                        AttendeeProviderRejectionReason.PROVIDER_DEADLINE_EXCEEDED
                    )
                for slot in self._slots.values() if not starting_in_progress else ():
                    if slot.retired or slot.active:
                        continue
                    if not _process_alive(slot.process):
                        slot.retired = True
                        self._lifecycle = AttendeeProviderLifecycle.FAILED
                        self._startup_failures += 1
                        self._condition.notify_all()
                        return _predispatch_failure(
                            AttendeeProviderRejectionReason.PROVIDER_FAILED
                        )
                    slot.active = True
                    slot.forced_reason = None
                    return slot
                remaining_ms = dispatch_deadline_ms - self._steady_now()
                if remaining_ms <= 0:
                    return _predispatch_failure(
                        AttendeeProviderRejectionReason.PROVIDER_CAPACITY_EXHAUSTED
                    )
                self._condition.wait(min(remaining_ms, _POLL_INTERVAL_MS) / 1_000)

    def _await_result(
        self,
        slot: _WorkerSlot,
        request: ProductExecutionRequest,
        emit: ProductOutputPublisher,
    ) -> ProductExecutionResult:
        hard_deadline_ms = _safe_add(
            self._steady_now(), self._limits.request_timeout_ms
        )
        cancellation_deadline_ms: int | None = None
        provider_deadline_cancellation = False
        accepting_output = True
        output_bytes = 0
        output_chunks = 0

        def sample_control_boundaries() -> tuple[int, int] | ProductExecutionResult:
            nonlocal accepting_output
            nonlocal cancellation_deadline_ms
            nonlocal provider_deadline_cancellation

            with self._condition:
                drain_deadline_ms = self._drain_deadline_ms

            steady_now = self._steady_now()
            try:
                cancelled = request.cancelled()
            except Exception:
                cancelled = True
            if cancelled and cancellation_deadline_ms is None:
                try:
                    slot.cancel_event.set()
                except Exception:
                    logger.exception("attendee_provider_cancel_signal_failed")
                    return self._ambiguous_failure(
                        slot,
                        AttendeeProviderRejectionReason.PROVIDER_DISPATCH_FAILED,
                        request,
                    )
                cancellation_deadline_ms = _safe_add(
                    steady_now,
                    self._limits.cancellation_grace_ms,
                )

            if (
                cancellation_deadline_ms is None
                and self._clock_now() >= request.deadline_ms
            ):
                # The provider poll can observe an exact command/session
                # deadline just before the lifecycle owner publishes its
                # cancellation Event.  Fence child output and request a
                # cooperative stop for the existing bounded cancellation
                # grace.  A confirmed child result remains a typed deadline
                # failure to the caller; a child that does not return is still
                # retired as ambiguous below.
                try:
                    slot.cancel_event.set()
                    slot.suppress_output_event.set()
                except Exception:
                    logger.exception("attendee_provider_deadline_signal_failed")
                    return self._ambiguous_failure(
                        slot,
                        AttendeeProviderRejectionReason.PROVIDER_DISPATCH_FAILED,
                        request,
                    )
                accepting_output = False
                cancellation_deadline_ms = _safe_add(
                    steady_now,
                    self._limits.cancellation_grace_ms,
                )
                provider_deadline_cancellation = True
            deadlines = [hard_deadline_ms]
            if cancellation_deadline_ms is not None:
                deadlines.append(cancellation_deadline_ms)
            if drain_deadline_ms is not None:
                deadlines.append(drain_deadline_ms)
            effective_deadline_ms = min(deadlines)
            if steady_now >= effective_deadline_ms:
                if (
                    drain_deadline_ms is not None
                    and effective_deadline_ms == drain_deadline_ms
                ):
                    reason = AttendeeProviderRejectionReason.PROVIDER_SHUTDOWN
                elif cancellation_deadline_ms is not None and (
                    effective_deadline_ms == cancellation_deadline_ms
                ):
                    reason = (
                        AttendeeProviderRejectionReason.PROVIDER_DEADLINE_EXCEEDED
                        if provider_deadline_cancellation
                        else AttendeeProviderRejectionReason.PROVIDER_CANCEL_TIMEOUT
                    )
                else:
                    reason = AttendeeProviderRejectionReason.PROVIDER_REQUEST_TIMEOUT
                return self._ambiguous_failure(slot, reason, request)
            return steady_now, effective_deadline_ms

        while True:
            forced = self._forced_failure(slot, request)
            if forced is not None:
                return forced

            sampled = sample_control_boundaries()
            if isinstance(sampled, ProductExecutionResult):
                return sampled
            steady_now, effective_deadline_ms = sampled

            poll_ms = min(effective_deadline_ms - steady_now, _POLL_INTERVAL_MS)
            try:
                available = slot.connection.poll(poll_ms / 1_000)
            except Exception:
                return self._ambiguous_failure(
                    slot,
                    AttendeeProviderRejectionReason.PROVIDER_WORKER_FAILED,
                    request,
                )
            if not available:
                if not _process_alive(slot.process):
                    return self._ambiguous_failure(
                        slot,
                        AttendeeProviderRejectionReason.PROVIDER_WORKER_FAILED,
                        request,
                    )
                continue

            # ``poll`` may become readable only after a wall, hard-request, or
            # drain boundary crossed.  Re-sample before consuming the frame so
            # a result that arrived after its deadline cannot reconcile actual
            # usage or state merely because the prior poll began in time.
            sampled = sample_control_boundaries()
            if isinstance(sampled, ProductExecutionResult):
                return sampled
            try:
                frame = slot.connection.recv_bytes(
                    maxlength=self._limits.max_ipc_frame_bytes
                )
                message = _decode_frame(frame, self._limits.max_ipc_frame_bytes)
            except Exception:
                return self._ambiguous_failure(
                    slot,
                    AttendeeProviderRejectionReason.PROVIDER_PROTOCOL_ERROR,
                    request,
                )

            # A bounded frame receive and decode can itself straddle a control
            # boundary.  Cancellation remains sampled before the provider
            # deadline, while the existing hard-request/drain minimum retains
            # precedence over the cooperative deadline grace.
            sampled = sample_control_boundaries()
            if isinstance(sampled, ProductExecutionResult):
                return sampled

            forced = self._forced_failure(slot, request)
            if forced is not None:
                return forced
            kind = message.get("kind")
            if kind == "output":
                try:
                    chunk = _output_from_message(message, self._limits)
                except (TypeError, ValueError):
                    return self._ambiguous_failure(
                        slot,
                        AttendeeProviderRejectionReason.PROVIDER_PROTOCOL_ERROR,
                        request,
                    )
                output_chunks += 1
                output_bytes += len(chunk.text.encode("utf-8"))
                if (
                    output_chunks > self._limits.max_output_chunks
                    or output_bytes > self._limits.max_output_bytes
                ):
                    return self._ambiguous_failure(
                        slot,
                        AttendeeProviderRejectionReason.PROVIDER_PROTOCOL_ERROR,
                        request,
                    )
                if not accepting_output:
                    continue
                try:
                    accepted = emit(chunk)
                except Exception:
                    return self._ambiguous_failure(
                        slot,
                        AttendeeProviderRejectionReason.PROVIDER_OUTPUT_FAILED,
                        request,
                    )
                if accepted is False:
                    accepting_output = False
                    try:
                        slot.suppress_output_event.set()
                    except Exception:
                        logger.exception(
                            "attendee_provider_output_suppression_signal_failed"
                        )
                        return self._ambiguous_failure(
                            slot,
                            AttendeeProviderRejectionReason.PROVIDER_DISPATCH_FAILED,
                            request,
                        )
                continue

            if kind == "result":
                try:
                    result = _result_from_message(message, self._limits)
                except (TypeError, ValueError):
                    return self._ambiguous_failure(
                        slot,
                        AttendeeProviderRejectionReason.PROVIDER_PROTOCOL_ERROR,
                        request,
                    )
                self._release_worker(slot, completed=True)
                if provider_deadline_cancellation:
                    return _reserved_failure(
                        AttendeeProviderRejectionReason.PROVIDER_DEADLINE_EXCEEDED,
                        request,
                    )
                return result

            if kind == "error":
                try:
                    _validate_worker_error(message)
                except (TypeError, ValueError):
                    reason = AttendeeProviderRejectionReason.PROVIDER_PROTOCOL_ERROR
                else:
                    reason = AttendeeProviderRejectionReason.PROVIDER_WORKER_FAILED
                return self._ambiguous_failure(slot, reason, request)

            return self._ambiguous_failure(
                slot,
                AttendeeProviderRejectionReason.PROVIDER_PROTOCOL_ERROR,
                request,
            )

    def _forced_failure(
        self,
        slot: _WorkerSlot,
        request: ProductExecutionRequest,
    ) -> ProductExecutionResult | None:
        with self._condition:
            forced_reason = slot.forced_reason
        if forced_reason is None:
            return None
        self._record_ambiguous(slot)
        self._release_worker(slot, completed=False)
        return _reserved_failure(forced_reason, request)

    def _ambiguous_failure(
        self,
        slot: _WorkerSlot,
        reason: AttendeeProviderRejectionReason,
        request: ProductExecutionRequest,
    ) -> ProductExecutionResult:
        should_replace = False
        should_stop = False
        with self._condition:
            effective_reason = slot.forced_reason or reason
            if not slot.retired:
                slot.retired = True
                slot.forced_reason = effective_reason
                should_stop = not self._closing.is_set()
                if (
                    self._lifecycle
                    in {
                        AttendeeProviderLifecycle.READY,
                        AttendeeProviderLifecycle.STARTING,
                    }
                    and not self._closing.is_set()
                    and slot.index not in self._replacement_indices
                ):
                    should_replace = True
                    self._replacement_indices.add(slot.index)
                    self._lifecycle = AttendeeProviderLifecycle.STARTING
            if not slot.ambiguity_recorded:
                slot.ambiguity_recorded = True
                self._ambiguous_dispatches += 1
            self._condition.notify_all()

        if should_stop:
            stopped = self._stop_slot(
                slot,
                deadline_ms=self._steady_now(),
                force_if_needed=True,
            )
            self._record_nonshutdown_stop(
                slot,
                stopped,
                source="ambiguous_dispatch",
            )
            if stopped.survived:
                should_replace = False
                with self._condition:
                    self._replacement_indices.discard(slot.index)
                    self._condition.notify_all()
        self._release_worker(slot, completed=False)
        if should_replace:
            self._start_replacement(slot.index)
        logger.warning(
            "attendee_provider_dispatch_contained reason=%s "
            "upstream_termination_evidence=%s",
            effective_reason.value,
            UpstreamTerminationEvidence.UNCONFIRMED.value,
        )
        return _reserved_failure(effective_reason, request)

    def _predispatch_slot_failure(
        self,
        slot: _WorkerSlot,
        reason: AttendeeProviderRejectionReason,
    ) -> ProductExecutionResult:
        """Retire a suspect acquired child before any request bytes are sent."""
        should_replace = False
        should_stop = False
        with self._condition:
            if not slot.retired:
                slot.retired = True
                slot.forced_reason = reason
                should_stop = not self._closing.is_set()
                if (
                    self._lifecycle
                    in {
                        AttendeeProviderLifecycle.READY,
                        AttendeeProviderLifecycle.STARTING,
                    }
                    and not self._closing.is_set()
                    and slot.index not in self._replacement_indices
                ):
                    should_replace = True
                    self._replacement_indices.add(slot.index)
                    self._lifecycle = AttendeeProviderLifecycle.STARTING
            self._condition.notify_all()

        if should_stop:
            stopped = self._stop_slot(
                slot,
                deadline_ms=self._steady_now(),
                force_if_needed=True,
            )
            self._record_nonshutdown_stop(
                slot,
                stopped,
                source="predispatch_event_failure",
            )
            if stopped.survived:
                should_replace = False
                with self._condition:
                    self._replacement_indices.discard(slot.index)
                    self._condition.notify_all()
        self._release_worker(slot, completed=False)
        if should_replace:
            self._start_replacement(slot.index)
        logger.warning(
            "attendee_provider_predispatch_contained reason=%s",
            reason.value,
        )
        return _predispatch_failure(reason)

    def _record_ambiguous(self, slot: _WorkerSlot) -> None:
        with self._condition:
            if not slot.ambiguity_recorded:
                slot.ambiguity_recorded = True
                self._ambiguous_dispatches += 1

    def _release_worker(self, slot: _WorkerSlot, *, completed: bool) -> None:
        with self._condition:
            if slot.active:
                slot.active = False
                if completed:
                    self._completed_dispatches += 1
            self._condition.notify_all()

    def _start_replacement(self, index: int) -> None:
        thread = threading.Thread(
            target=self._replace_worker,
            args=(index,),
            name=f"attendee-provider-replace-{index}",
            daemon=True,
        )
        with self._condition:
            if self._closing.is_set() or self._lifecycle not in {
                AttendeeProviderLifecycle.STARTING,
                AttendeeProviderLifecycle.READY,
            }:
                self._replacement_indices.discard(index)
                self._condition.notify_all()
                return
            self._replacement_threads.add(thread)
            try:
                thread.start()
            except Exception:
                self._replacement_threads.discard(thread)
                self._replacement_indices.discard(index)
                self._lifecycle = AttendeeProviderLifecycle.FAILED
                self._startup_failures += 1
                self._condition.notify_all()
                logger.exception("attendee_provider_replacement_thread_failed")

    def _replace_worker(self, index: int) -> None:
        replacement: _WorkerSlot | None = None
        replacement_survived = False
        try:
            deadline_ms = _safe_add(self._steady_now(), self._limits.startup_timeout_ms)
            replacement = self._spawn_ready_worker(index, deadline_ms)
            with self._condition:
                if (
                    self._lifecycle is AttendeeProviderLifecycle.STARTING
                    and not self._closing.is_set()
                ):
                    self._slots[index] = replacement
                    self._spawning_slots.pop(id(replacement), None)
                    replacement = None
                self._condition.notify_all()
        except Exception:
            with self._condition:
                if (
                    self._lifecycle
                    in {
                        AttendeeProviderLifecycle.STARTING,
                        AttendeeProviderLifecycle.FAILED,
                    }
                    and not self._closing.is_set()
                ):
                    self._lifecycle = AttendeeProviderLifecycle.FAILED
                    self._startup_failures += 1
                self._condition.notify_all()
            logger.error("attendee_provider_worker_replacement_failed")
        finally:
            if replacement is not None:
                stopped = self._stop_slot(
                    replacement,
                    deadline_ms=self._steady_now(),
                    force_if_needed=True,
                )
                replacement_survived = stopped.survived
                self._record_nonshutdown_stop(
                    replacement,
                    stopped,
                    source="replacement_cleanup",
                )
            current = threading.current_thread()
            with self._condition:
                if replacement is not None and not replacement_survived:
                    self._spawning_slots.pop(id(replacement), None)
                self._replacement_indices.discard(index)
                self._replacement_threads.discard(current)
                if (
                    self._lifecycle is AttendeeProviderLifecycle.STARTING
                    and not self._replacement_indices
                    and self._ready_workers_locked() == self._limits.worker_capacity
                ):
                    self._lifecycle = AttendeeProviderLifecycle.READY
                    logger.info("attendee_provider_pool_ready_after_replacement")
                self._condition.notify_all()

    def _spawn_ready_worker(
        self,
        index: int,
        deadline_ms: int,
    ) -> _WorkerSlot:
        parent_connection, child_connection = self._context.Pipe(duplex=True)
        cancel_event = self._context.Event()
        suppress_output_event = self._context.Event()
        process = self._context.Process(
            target=_worker_main,
            args=(
                child_connection,
                cancel_event,
                suppress_output_event,
                self._worker_factory,
                self._limits,
            ),
            name=f"dreamland-attendee-provider-{index}",
        )
        process.daemon = False
        slot = _WorkerSlot(
            index,
            process,
            parent_connection,
            cancel_event,
            suppress_output_event,
        )
        try:
            with self._condition:
                if self._closing.is_set():
                    raise RuntimeError("provider pool is closing")
                self._spawning_slots[id(slot)] = slot
                process.start()
                self._condition.notify_all()
        except BaseException:
            try:
                child_connection.close()
            except Exception:
                pass
            stopped = self._stop_slot(
                slot,
                deadline_ms=self._steady_now(),
                force_if_needed=True,
            )
            self._record_nonshutdown_stop(
                slot,
                stopped,
                source="spawn_start_failure",
            )
            with self._condition:
                if not stopped.survived:
                    self._spawning_slots.pop(id(slot), None)
                self._condition.notify_all()
            raise
        child_connection.close()
        try:
            while self._steady_now() < deadline_ms:
                if self._closing.is_set():
                    raise RuntimeError("provider pool is closing")
                remaining_ms = deadline_ms - self._steady_now()
                if parent_connection.poll(min(remaining_ms, _POLL_INTERVAL_MS) / 1_000):
                    frame = parent_connection.recv_bytes(
                        maxlength=self._limits.max_ipc_frame_bytes
                    )
                    message = _decode_frame(
                        frame,
                        self._limits.max_ipc_frame_bytes,
                    )
                    _validate_ready(message)
                    if self._closing.is_set():
                        raise RuntimeError("provider pool is closing")
                    return slot
                if not _process_alive(process):
                    raise RuntimeError("provider worker exited during startup")
            raise TimeoutError("provider worker readiness deadline expired")
        except BaseException:
            stop_deadline_ms = (
                self._steady_now() if self._closing.is_set() else deadline_ms
            )
            stopped = self._stop_slot(
                slot,
                deadline_ms=stop_deadline_ms,
                force_if_needed=True,
            )
            self._record_nonshutdown_stop(
                slot,
                stopped,
                source="spawn_readiness_failure",
            )
            with self._condition:
                if not stopped.survived:
                    self._spawning_slots.pop(id(slot), None)
                self._condition.notify_all()
            raise

    def _stop_slot(
        self,
        slot: _WorkerSlot,
        *,
        deadline_ms: int,
        force_if_needed: bool,
    ) -> _StopSlotResult:
        with slot.stop_lock:
            forced = False
            newly_forced = False
            process = slot.process
            try:
                if (
                    _process_alive(process)
                    and not slot.active
                    and not slot.shutdown_sent
                ):
                    _close_idle_endpoint(slot)
                remaining_ms = max(0, deadline_ms - self._steady_now())
                try:
                    process.join(remaining_ms / 1_000)
                except Exception:
                    logger.exception("attendee_provider_worker_join_failed")
                if _process_alive(process) and force_if_needed:
                    forced = True
                    newly_forced = not slot.force_recorded
                    slot.force_recorded = True
                    try:
                        process.terminate()
                    except Exception:
                        logger.exception("attendee_provider_worker_terminate_failed")
                    try:
                        process.join(self._limits.terminate_grace_ms / 1_000)
                    except Exception:
                        logger.exception(
                            "attendee_provider_worker_terminate_join_failed"
                        )
                    if _process_alive(process):
                        try:
                            process.kill()
                        except Exception:
                            logger.exception("attendee_provider_worker_kill_failed")
                        try:
                            process.join(self._limits.terminate_grace_ms / 1_000)
                        except Exception:
                            logger.exception(
                                "attendee_provider_worker_kill_join_failed"
                            )
            finally:
                try:
                    slot.connection.close()
                except Exception:
                    pass
            survived = _process_alive(process)
            if survived:
                logger.critical(
                    "attendee_provider_worker_survived_local_kill index=%d",
                    slot.index,
                )
            else:
                _close_stopped_process(process)
            return _StopSlotResult(
                forced=forced,
                newly_forced=newly_forced,
                survived=survived,
            )

    def _record_nonshutdown_stop(
        self,
        slot: _WorkerSlot,
        stopped: _StopSlotResult,
        *,
        source: str,
    ) -> None:
        """Retain a local-kill survivor and make the pool terminally failed."""
        with self._condition:
            if stopped.newly_forced:
                self._locally_forced_workers += 1
            if stopped.survived:
                slot.retired = True
                if slot.forced_reason is None:
                    slot.forced_reason = AttendeeProviderRejectionReason.PROVIDER_FAILED
                if not any(candidate is slot for candidate in self._slots.values()):
                    self._spawning_slots[id(slot)] = slot
                self._lifecycle = AttendeeProviderLifecycle.FAILED
            self._condition.notify_all()
        if stopped.survived:
            logger.critical(
                "attendee_provider_survivor_retained source=%s index=%d",
                source,
                slot.index,
            )

    def _signal_idle_shutdown_locked(self) -> None:
        slots = {
            id(slot): slot
            for slot in (*self._slots.values(), *self._spawning_slots.values())
        }
        for slot in slots.values():
            if slot.active or slot.shutdown_sent or not _process_alive(slot.process):
                continue
            _close_idle_endpoint(slot)

    def _stop_slots_bounded(
        self,
        slots: tuple[_WorkerSlot, ...],
        *,
        graceful_deadline_ms: int,
        terminate_deadline_ms: int,
        kill_deadline_ms: int,
    ) -> tuple[int, tuple[_WorkerSlot, ...]]:
        """Stop slots using shared deadlines independent of pool capacity."""
        unique = {id(slot): slot for slot in slots}
        locked: list[_WorkerSlot] = []
        forced: dict[int, _WorkerSlot] = {}
        for slot in unique.values():
            remaining_ms = kill_deadline_ms - self._steady_now()
            if remaining_ms <= 0:
                break
            if slot.stop_lock.acquire(timeout=remaining_ms / 1_000):
                locked.append(slot)

        try:
            for slot in locked:
                if (
                    slot.active
                    or slot.shutdown_sent
                    or not _process_alive(slot.process)
                ):
                    continue
                _close_idle_endpoint(slot)

            self._join_slots_until(locked, graceful_deadline_ms)
            forced = {id(slot): slot for slot in locked if _process_alive(slot.process)}
            newly_forced = sum(1 for slot in forced.values() if not slot.force_recorded)
            for slot in forced.values():
                slot.force_recorded = True
                try:
                    slot.process.terminate()
                except Exception:
                    pass
            self._join_slots_until(forced.values(), terminate_deadline_ms)

            survivors = [
                slot for slot in forced.values() if _process_alive(slot.process)
            ]
            for slot in survivors:
                try:
                    slot.process.kill()
                except Exception:
                    pass
            self._join_slots_until(survivors, kill_deadline_ms)
        finally:
            for slot in reversed(locked):
                slot.stop_lock.release()

        for slot in unique.values():
            try:
                slot.connection.close()
            except Exception:
                pass
            if _process_alive(slot.process):
                logger.critical(
                    "attendee_provider_worker_survived_local_kill index=%d",
                    slot.index,
                )
            else:
                _close_stopped_process(slot.process)
        remaining = tuple(
            slot for slot in unique.values() if _process_alive(slot.process)
        )
        return newly_forced, remaining

    def _join_slots_until(
        self,
        slots: object,
        deadline_ms: int,
    ) -> None:
        for slot in slots:
            remaining_ms = deadline_ms - self._steady_now()
            if remaining_ms <= 0:
                return
            try:
                slot.process.join(remaining_ms / 1_000)
            except Exception:
                continue

    def _join_replacement_threads(self, deadline_ms: int) -> bool:
        while True:
            with self._condition:
                threads = tuple(
                    thread
                    for thread in self._replacement_threads
                    if thread is not threading.current_thread()
                )
            if not threads:
                return True
            for thread in threads:
                remaining_ms = deadline_ms - self._steady_now()
                if remaining_ms <= 0:
                    logger.critical(
                        "attendee_provider_replacement_join_deadline_exceeded"
                    )
                    return False
                thread.join(remaining_ms / 1_000)

    def _join_send_threads(self, deadline_ms: int) -> bool:
        while True:
            with self._condition:
                threads = tuple(self._send_threads)
            if not threads:
                return True
            for thread in threads:
                remaining_ms = deadline_ms - self._steady_now()
                if remaining_ms <= 0:
                    logger.critical("attendee_provider_send_join_deadline_exceeded")
                    return False
                thread.join(remaining_ms / 1_000)

    def _dispatch_rejection_locked(
        self,
    ) -> AttendeeProviderRejectionReason | None:
        if self._lifecycle is AttendeeProviderLifecycle.READY:
            return None
        if self._lifecycle is AttendeeProviderLifecycle.STARTING:
            return AttendeeProviderRejectionReason.PROVIDER_STARTING
        if self._lifecycle is AttendeeProviderLifecycle.DRAINING:
            return AttendeeProviderRejectionReason.PROVIDER_DRAINING
        if self._lifecycle is AttendeeProviderLifecycle.CLOSED:
            return AttendeeProviderRejectionReason.PROVIDER_CLOSED
        return AttendeeProviderRejectionReason.PROVIDER_FAILED

    def _lifecycle_reason_locked(self) -> AttendeeProviderRejectionReason:
        return self._dispatch_rejection_locked() or (
            AttendeeProviderRejectionReason.PROVIDER_FAILED
        )

    def _ready_workers_locked(self) -> int:
        return sum(
            not slot.retired and _process_alive(slot.process)
            for slot in self._slots.values()
        )

    def _active_workers_locked(self) -> int:
        return sum(slot.active for slot in self._slots.values())

    def _snapshot_locked(self) -> AttendeeProviderPoolSnapshot:
        ready_workers = self._ready_workers_locked()
        active_workers = self._active_workers_locked()
        available_workers = sum(
            not slot.retired and not slot.active and _process_alive(slot.process)
            for slot in self._slots.values()
        )
        return AttendeeProviderPoolSnapshot(
            lifecycle=self._lifecycle,
            worker_capacity=self._limits.worker_capacity,
            ready_workers=ready_workers,
            active_workers=active_workers,
            available_workers=available_workers,
            accepted_dispatches=self._accepted_dispatches,
            completed_dispatches=self._completed_dispatches,
            ambiguous_dispatches=self._ambiguous_dispatches,
            locally_forced_workers=self._locally_forced_workers,
            startup_failures=self._startup_failures,
            forced_upstream_termination_evidence=(
                UpstreamTerminationEvidence.UNCONFIRMED
            ),
        )

    def _clock_now(self) -> int:
        return _clock_value(self._clock_ms(), "clock_ms")

    def _steady_now(self) -> int:
        return _clock_value(self._steady_ms(), "steady_ms")


def _worker_main(
    connection: Connection,
    cancel_event: object,
    suppress_output_event: object,
    worker_factory: AttendeeProviderWorkerFactory,
    limits: AttendeeProviderPoolLimits,
) -> None:
    """Spawn-process entry point; never expose child exceptions over IPC."""
    try:
        executor = worker_factory()
        if not callable(getattr(executor, "execute", None)):
            raise TypeError("worker factory returned an invalid executor")
        connection.send_bytes(
            _encode_message({"kind": "ready", "protocol_version": _PROTOCOL_VERSION})
        )
    except BaseException:
        _send_worker_error(connection, "bootstrap_failed")
        connection.close()
        return

    while True:
        try:
            frame = connection.recv_bytes(maxlength=limits.max_ipc_frame_bytes)
            message = _decode_frame(frame, limits.max_ipc_frame_bytes)
        except (EOFError, BrokenPipeError):
            break
        except BaseException:
            _send_worker_error(connection, "invalid_request")
            break
        if message == {"kind": "shutdown"}:
            break
        try:
            request = _request_from_message(message, cancel_event, limits)
        except BaseException:
            _send_worker_error(connection, "invalid_request")
            continue

        publication = _WorkerPublication(
            connection,
            cancel_event,
            suppress_output_event,
            limits,
        )

        try:
            result = executor.execute(request, publication.emit)
            publication.close()
            result_message = _result_to_message(result, limits)
            encoded_result = _encode_message(result_message)
            if len(encoded_result) > limits.max_ipc_frame_bytes:
                raise ValueError("worker result exceeds IPC limits")
            connection.send_bytes(encoded_result)
        except BaseException:
            publication.close()
            _send_worker_error(connection, "execution_failed")

    connection.close()


def _encode_request_frame(
    request: ProductExecutionRequest,
    limits: AttendeeProviderPoolLimits,
) -> bytes:
    for value in (
        request.service_epoch,
        request.session_id,
        request.command_id,
        request.checkpoint_namespace,
    ):
        _require_bounded_text(value, _MAX_IDENTITY_BYTES, "request identity")
    _require_bounded_text(
        request.canonical_command,
        limits.max_command_bytes,
        "canonical command",
    )
    document = _normalize_json_document(
        request.provisional_document,
        limits.max_state_bytes,
        "provisional document",
    )
    slot_generation = _require_positive_integer(
        request.slot_generation,
        "slot_generation",
    )
    deadline_ms = _require_nonnegative_integer(request.deadline_ms, "deadline_ms")
    max_tokens = _require_nonnegative_integer(request.max_tokens, "max_tokens")
    max_spend_units = _require_nonnegative_integer(
        request.max_spend_units,
        "max_spend_units",
    )
    max_attempts = _require_positive_integer(request.max_attempts, "max_attempts")
    message = {
        "kind": "execute",
        "protocol_version": _PROTOCOL_VERSION,
        "request": {
            "service_epoch": request.service_epoch,
            "session_id": request.session_id,
            "command_id": request.command_id,
            "slot_generation": slot_generation,
            "canonical_command": request.canonical_command,
            "provisional_document": document,
            "checkpoint_namespace": request.checkpoint_namespace,
            "deadline_ms": deadline_ms,
            "max_tokens": max_tokens,
            "max_spend_units": max_spend_units,
            "max_attempts": max_attempts,
        },
    }
    encoded = _encode_message(message)
    if len(encoded) > limits.max_ipc_frame_bytes:
        raise ValueError("provider request exceeds IPC frame limit")
    return encoded


def _request_from_message(
    message: Mapping[str, object],
    cancel_event: object,
    limits: AttendeeProviderPoolLimits,
) -> ProductExecutionRequest:
    _require_exact_keys(message, {"kind", "protocol_version", "request"})
    if message["kind"] != "execute" or message["protocol_version"] != _PROTOCOL_VERSION:
        raise ValueError("invalid execute message")
    values = _require_mapping(message["request"], "request")
    _require_exact_keys(
        values,
        {
            "service_epoch",
            "session_id",
            "command_id",
            "slot_generation",
            "canonical_command",
            "provisional_document",
            "checkpoint_namespace",
            "deadline_ms",
            "max_tokens",
            "max_spend_units",
            "max_attempts",
        },
    )
    for name in (
        "service_epoch",
        "session_id",
        "command_id",
        "checkpoint_namespace",
    ):
        _require_bounded_text(values[name], _MAX_IDENTITY_BYTES, name)
    _require_bounded_text(
        values["canonical_command"],
        limits.max_command_bytes,
        "canonical_command",
    )
    document = _normalize_json_document(
        values["provisional_document"],
        limits.max_state_bytes,
        "provisional_document",
    )
    return ProductExecutionRequest(
        service_epoch=values["service_epoch"],
        session_id=values["session_id"],
        command_id=values["command_id"],
        slot_generation=_require_positive_integer(
            values["slot_generation"],
            "slot_generation",
        ),
        canonical_command=values["canonical_command"],
        provisional_document=document,
        checkpoint_namespace=values["checkpoint_namespace"],
        deadline_ms=_require_nonnegative_integer(values["deadline_ms"], "deadline_ms"),
        max_tokens=_require_nonnegative_integer(values["max_tokens"], "max_tokens"),
        max_spend_units=_require_nonnegative_integer(
            values["max_spend_units"],
            "max_spend_units",
        ),
        max_attempts=_require_positive_integer(values["max_attempts"], "max_attempts"),
        cancelled=cancel_event.is_set,
    )


def _output_to_message(
    chunk: ProductOutputChunk,
    limits: AttendeeProviderPoolLimits,
) -> dict[str, object]:
    if not isinstance(chunk, ProductOutputChunk):
        raise TypeError("worker emitted an invalid output chunk")
    _require_bounded_text(chunk.text, limits.max_output_chunk_bytes, "output text")
    return {"kind": "output", "stream": chunk.stream.value, "text": chunk.text}


def _output_from_message(
    message: Mapping[str, object],
    limits: AttendeeProviderPoolLimits,
) -> ProductOutputChunk:
    _require_exact_keys(message, {"kind", "stream", "text"})
    if message["kind"] != "output":
        raise ValueError("invalid output message")
    _require_bounded_text(message["text"], limits.max_output_chunk_bytes, "output text")
    try:
        stream = ProductOutputStream(message["stream"])
    except (TypeError, ValueError) as error:
        raise ValueError("invalid output stream") from error
    return ProductOutputChunk(stream, message["text"])


def _result_to_message(
    result: ProductExecutionResult,
    limits: AttendeeProviderPoolLimits,
) -> dict[str, object]:
    if not isinstance(result, ProductExecutionResult):
        raise TypeError("worker returned an invalid result")
    termination: dict[str, object] | None = None
    if result.guest_termination is not None:
        termination = {
            "kind": result.guest_termination.kind.value,
            "exit_status": result.guest_termination.exit_status,
            "signal": result.guest_termination.signal,
        }
    state_effect: dict[str, object] | None = None
    if result.state_effect is not None:
        document = None
        if result.state_effect.provisional_document is not None:
            document = _normalize_json_document(
                result.state_effect.provisional_document,
                limits.max_state_bytes,
                "result provisional document",
            )
        state_effect = {
            "kind": result.state_effect.kind.value,
            "provisional_document": document,
            "safe_reason": result.state_effect.safe_reason,
        }
    return {
        "kind": "result",
        "result": {
            "outcome": result.outcome.value,
            "guest_termination": termination,
            "state_effect": state_effect,
            "reason": result.reason,
            "usage_tokens": result.usage_tokens,
            "usage_spend_units": result.usage_spend_units,
        },
    }


def _result_from_message(
    message: Mapping[str, object],
    limits: AttendeeProviderPoolLimits,
) -> ProductExecutionResult:
    _require_exact_keys(message, {"kind", "result"})
    if message["kind"] != "result":
        raise ValueError("invalid result message")
    values = _require_mapping(message["result"], "result")
    _require_exact_keys(
        values,
        {
            "outcome",
            "guest_termination",
            "state_effect",
            "reason",
            "usage_tokens",
            "usage_spend_units",
        },
    )
    try:
        outcome = ProductExecutionOutcome(values["outcome"])
    except (TypeError, ValueError) as error:
        raise ValueError("invalid result outcome") from error

    guest_termination = None
    if values["guest_termination"] is not None:
        termination = _require_mapping(values["guest_termination"], "guest_termination")
        _require_exact_keys(termination, {"kind", "exit_status", "signal"})
        try:
            termination_kind = ProductGuestTerminationKind(termination["kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("invalid guest termination kind") from error
        guest_termination = ProductGuestTermination(
            termination_kind,
            exit_status=termination["exit_status"],
            signal=termination["signal"],
        )

    state_effect = None
    if values["state_effect"] is not None:
        effect = _require_mapping(values["state_effect"], "state_effect")
        _require_exact_keys(
            effect,
            {"kind", "provisional_document", "safe_reason"},
        )
        try:
            effect_kind = ProductStateEffectKind(effect["kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("invalid state effect kind") from error
        document = effect["provisional_document"]
        if document is not None:
            document = _normalize_json_document(
                document,
                limits.max_state_bytes,
                "result provisional document",
            )
        state_effect = ProductStateEffect(
            effect_kind,
            provisional_document=document,
            safe_reason=effect["safe_reason"],
        )
    return ProductExecutionResult(
        outcome=outcome,
        guest_termination=guest_termination,
        state_effect=state_effect,
        reason=values["reason"],
        usage_tokens=_require_integer(values["usage_tokens"]),
        usage_spend_units=_require_integer(values["usage_spend_units"]),
    )


def _validate_ready(message: Mapping[str, object]) -> None:
    _require_exact_keys(message, {"kind", "protocol_version"})
    if message["kind"] != "ready" or message["protocol_version"] != _PROTOCOL_VERSION:
        raise ValueError("invalid worker readiness message")


def _validate_worker_error(message: Mapping[str, object]) -> None:
    _require_exact_keys(message, {"kind", "code"})
    if message["kind"] != "error" or message["code"] not in {
        "bootstrap_failed",
        "execution_failed",
        "invalid_request",
    }:
        raise ValueError("invalid worker error message")


def _send_worker_error(connection: Connection, code: str) -> None:
    try:
        connection.send_bytes(_encode_message({"kind": "error", "code": code}))
    except BaseException:
        return


def _encode_message(message: Mapping[str, object]) -> bytes:
    return json.dumps(
        message,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _decode_frame(frame: bytes, maximum_bytes: int) -> dict[str, object]:
    if not isinstance(frame, bytes) or not frame or len(frame) > maximum_bytes:
        raise ValueError("invalid IPC frame size")
    try:
        decoded = json.loads(
            frame.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid IPC JSON") from error
    if not isinstance(decoded, dict):
        raise ValueError("IPC message must be an object")
    _validate_json_tree(decoded)
    return decoded


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is invalid: {value}")


def _normalize_json_document(
    value: object,
    maximum_bytes: int,
    name: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    _validate_json_tree(value)
    encoded = _encode_message(value)
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{name} exceeds its byte limit")
    return _decode_frame(encoded, maximum_bytes)


def _validate_json_tree(value: object) -> None:
    nodes = 0

    def visit(candidate: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError("JSON structure exceeds its complexity limit")
        if candidate is None or isinstance(candidate, (str, bool)):
            return
        if isinstance(candidate, int):
            if not -_MAX_SAFE_INTEGER <= candidate <= _MAX_SAFE_INTEGER:
                raise ValueError("JSON integer exceeds its bounded range")
            return
        if isinstance(candidate, float):
            if candidate != candidate or candidate in (float("inf"), float("-inf")):
                raise ValueError("JSON number must be finite")
            return
        if isinstance(candidate, list):
            for item in candidate:
                visit(item, depth + 1)
            return
        if isinstance(candidate, dict):
            for key, item in candidate.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                visit(item, depth + 1)
            return
        raise ValueError("value is not JSON-compatible")

    visit(value, 0)


def _require_exact_keys(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError("IPC message has an invalid shape")


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _require_bounded_text(value: object, maximum_bytes: int, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum_bytes
    ):
        raise ValueError(f"{name} is invalid")


def _require_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("IPC integer is invalid")
    return value


def _require_nonnegative_integer(value: object, name: str) -> int:
    result = _require_integer(value)
    if not 0 <= result <= _MAX_SAFE_INTEGER:
        raise ValueError(f"{name} is outside its bounded range")
    return result


def _require_positive_integer(value: object, name: str) -> int:
    result = _require_integer(value)
    if not 1 <= result <= _MAX_SAFE_INTEGER:
        raise ValueError(f"{name} is outside its bounded range")
    return result


def _require_positive_limit(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_LIMIT
    ):
        raise ValueError(f"{name} must be a bounded positive integer")


def _clock_value(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_SAFE_INTEGER
    ):
        raise RuntimeError(f"{name} returned an invalid value")
    return value


def _safe_add(value: int, increment: int) -> int:
    if value > _MAX_SAFE_INTEGER - increment:
        return _MAX_SAFE_INTEGER
    return value + increment


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _utc_ms() -> int:
    return time.time_ns() // 1_000_000


def _process_alive(process: object) -> bool:
    try:
        return bool(process.is_alive())
    except (AssertionError, ValueError):
        return False
    except Exception:
        logger.exception("attendee_provider_worker_liveness_unknown")
        return True


def _close_stopped_process(process: object) -> None:
    if _process_alive(process):
        return
    try:
        process.close()
    except Exception:
        return


def _close_idle_endpoint(slot: _WorkerSlot) -> None:
    try:
        slot.connection.close()
    except Exception:
        pass
    slot.shutdown_sent = True


def _predispatch_failure(
    reason: AttendeeProviderRejectionReason,
) -> ProductExecutionResult:
    return ProductExecutionResult.failed(reason.value)


def _reserved_failure(
    reason: AttendeeProviderRejectionReason,
    request: ProductExecutionRequest,
) -> ProductExecutionResult:
    return ProductExecutionResult.failed(
        reason.value,
        usage_tokens=request.max_tokens,
        usage_spend_units=request.max_spend_units,
    )


__all__ = [
    "AttendeeProviderLifecycle",
    "AttendeeProviderPool",
    "AttendeeProviderPoolError",
    "AttendeeProviderPoolLimits",
    "AttendeeProviderPoolSnapshot",
    "AttendeeProviderRejectionReason",
    "AttendeeProviderWorkerFactory",
    "UpstreamTerminationEvidence",
]
