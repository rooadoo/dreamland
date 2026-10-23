"""Manually composed product-only adapter for detached command execution.

The adapter is inert until explicitly constructed.  It does not import or wrap
``AgentBackend`` and therefore has no access to the SSH parser, runtime session
registry, event bus, state-profile store, or structured-failure capsule store.
Its collaborators are product-owned and injected by the caller.

This checkpoint deliberately stops short of constructing a live model
provider.  A future provider adapter must satisfy the protocols here while
providing isolated credentials, removable checkpoint namespaces, safe
telemetry, and exact usage evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from dreamland.models import (
    CommandContext,
    CommandOutputChunk,
    CommandResult,
    CommandStateUpdate,
    SessionState,
)
from dreamland.network_connections import (
    projected_environment_metadata,
    provisional_connection_events,
)
from dreamland.product.execution import (
    ProductExecutionRequest,
    ProductExecutionResult,
    ProductOutputChunk,
    ProductOutputPublisher,
    ProductOutputStream,
    ProductStateEffect,
    ProductStateEffectKind,
)
from dreamland.session_state import active_shell_frame, apply_command_state_update
from dreamland.state_profiles import (
    STATE_PROFILE_METADATA_LEVEL,
    STATE_PROFILE_METADATA_NAME,
    StateProfile,
    session_state_for_level,
    state_profile_from_document,
    state_profile_to_session,
)
from dreamland.world_state import active_host_metadata_for_context, active_host_ref

LOGGER = logging.getLogger(__name__)

_SAFE_SIGNAL = re.compile(r"^SIG[A-Z0-9]{1,15}$")
_MAX_USAGE = (1 << 63) - 1
_OUTPUT_EVIDENCE_CHUNK_CHARACTERS = 64 * 1024


class ProductRuntimeStopReason(StrEnum):
    """Evidence that prevents a successful provisional publication."""

    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "command_deadline_exceeded"
    OBSERVATION_FAILED = "cancellation_observation_failed"
    ATTEMPT_LIMIT_EXCEEDED = "attempt_limit_exceeded"


@dataclass(frozen=True, slots=True)
class ProductRuntimeLimits:
    """Exact reservation limits visible to product-owned collaborators."""

    max_tokens: int
    max_spend_units: int
    max_attempts: int

    def __post_init__(self) -> None:
        for name, value in (
            ("max_tokens", self.max_tokens),
            ("max_spend_units", self.max_spend_units),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_USAGE
            ):
                raise ValueError(f"{name} must be a non-negative bounded integer")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= _MAX_USAGE
        ):
            raise ValueError("max_attempts must be a positive bounded integer")


@dataclass(frozen=True, slots=True)
class ProductRuntimeUsage:
    """Exact product-provider usage observed for one physical execution."""

    tokens: int = 0
    spend_units: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("tokens", self.tokens),
            ("spend_units", self.spend_units),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_USAGE
            ):
                raise ValueError(f"{name} must be a non-negative bounded integer")


@dataclass(slots=True)
class ProductRuntimeState:
    """Detached state hydrated by a product-owned state-profile codec."""

    profile_name: str
    session: SessionState = field(repr=False)
    codec_context: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.profile_name, str) or not self.profile_name:
            raise ValueError("runtime profile name is invalid")
        if not isinstance(self.session, SessionState):
            raise TypeError("runtime session must be SessionState")


class ProductRuntimeAgent(Protocol):
    """Product-owned physical command runner.

    ``execute`` must not return until its physical provider work has stopped.
    It may emit ordered candidate output while running.  The returned streams
    are the complete stdout/stderr values; the adapter emits only any suffix
    not already streamed.  Before every provider attempt, a live adapter must
    call ``control.claim_attempt()`` and stop when it returns ``None``.  This
    protocol requirement is cooperative: a composition cannot advertise hard
    attempt enforcement until its provider seam makes bypass impossible.
    """

    def execute(
        self,
        context: CommandContext,
        control: ProductRuntimeControl,
        emit: Callable[[CommandOutputChunk], None],
    ) -> CommandResult: ...


class ProductStateProfileCodec(Protocol):
    """Hydrate and serialize a detached product runtime document."""

    def decode(
        self,
        document: Mapping[str, object],
        *,
        checkpoint_namespace: str,
    ) -> ProductRuntimeState: ...

    def encode(self, state: ProductRuntimeState) -> Mapping[str, object]: ...


class ProductCheckpointReaper(Protocol):
    """Remove all checkpoint data for one exact product namespace."""

    def reap(self, checkpoint_namespace: str) -> None: ...


class ProductUsageMeter(Protocol):
    """Measure exact product-provider usage without receiving command text."""

    def begin(
        self,
        *,
        command_id: str,
        checkpoint_namespace: str,
        limits: ProductRuntimeLimits,
    ) -> object: ...

    def finish(self, measurement: object) -> ProductRuntimeUsage: ...


class DreamlandStateProfileCodec:
    """Store-free codec for DREAMLAND state-profile documents.

    Loading and saving go through pure state-profile functions only.  The
    provisional runtime is serialized at ``exact`` scope so session-local
    changes survive even when the selected source profile was ``world`` or
    ``system`` scoped.  Runtime-only profile metadata added during hydration is
    removed again before serialization.
    """

    def decode(
        self,
        document: Mapping[str, object],
        *,
        checkpoint_namespace: str,
    ) -> ProductRuntimeState:
        profile = state_profile_from_document(deepcopy(dict(document)))
        session = state_profile_to_session(
            profile,
            current_session_id=checkpoint_namespace,
        )
        return ProductRuntimeState(
            profile_name=profile.name,
            session=session,
            codec_context=profile,
        )

    def encode(self, state: ProductRuntimeState) -> Mapping[str, object]:
        profile = state.codec_context
        if not isinstance(profile, StateProfile) or profile.name != state.profile_name:
            raise ValueError("runtime codec context is invalid")
        profile_state = session_state_for_level(state.session, "exact")
        _remove_runtime_profile_metadata(profile_state)
        return replace(profile, level="exact", state=profile_state).to_document()


class ProductRuntimeControl:
    """Cooperative cancellation/deadline control with an active watchdog.

    The watchdog exists only during an explicitly invoked command.  It lets an
    injected provider cancel a blocked operation through registered callbacks;
    a provider that ignores cancellation still keeps ``execute`` blocked until
    physical completion, preserving truthful worker-reap semantics.
    """

    def __init__(
        self,
        *,
        cancelled: Callable[[], bool],
        deadline_ms: int,
        clock_ms: Callable[[], int],
        poll_interval_ms: int,
        limits: ProductRuntimeLimits,
    ) -> None:
        self._source_cancelled = cancelled
        self._deadline_ms = deadline_ms
        self._clock_ms = clock_ms
        self._poll_interval_seconds = poll_interval_ms / 1_000
        self._limits = limits
        self._attempts = 0
        self._reason: ProductRuntimeStopReason | None = None
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._monitor_done = threading.Event()
        self._monitor: threading.Thread | None = None

    @property
    def stop_reason(self) -> ProductRuntimeStopReason | None:
        self.refresh()
        with self._lock:
            return self._reason

    @property
    def limits(self) -> ProductRuntimeLimits:
        return self._limits

    @property
    def attempts(self) -> int:
        with self._lock:
            return self._attempts

    def claim_attempt(self) -> int | None:
        """Claim one provider attempt, or stop at the accepted hard cap."""
        if self.refresh() is not None:
            return None
        with self._lock:
            if self._reason is not None:
                return None
            if self._attempts < self._limits.max_attempts:
                self._attempts += 1
                return self._attempts
        self._mark_stopped(ProductRuntimeStopReason.ATTEMPT_LIMIT_EXCEEDED)
        return None

    def cancelled(self) -> bool:
        """Return whether cancellation, deadline, or observation failure won."""
        return self.stop_reason is not None

    def add_cancel_callback(self, callback: Callable[[], None]) -> None:
        if not callable(callback):
            raise TypeError("cancellation callback must be callable")
        call_now = False
        with self._lock:
            if self._reason is None:
                self._callbacks.append(callback)
            else:
                call_now = True
        if call_now:
            _invoke_cancel_callback(callback)

    def refresh(self) -> ProductRuntimeStopReason | None:
        with self._lock:
            if self._reason is not None:
                return self._reason
        try:
            source_cancelled = self._source_cancelled()
            if not isinstance(source_cancelled, bool):
                raise TypeError("cancelled evidence must be boolean")
        except Exception:
            return self._mark_stopped(ProductRuntimeStopReason.OBSERVATION_FAILED)
        if source_cancelled:
            return self._mark_stopped(ProductRuntimeStopReason.CANCELLED)
        try:
            now_ms = self._clock_ms()
            if isinstance(now_ms, bool) or not isinstance(now_ms, int):
                raise TypeError("deadline clock must return integer milliseconds")
        except Exception:
            return self._mark_stopped(ProductRuntimeStopReason.OBSERVATION_FAILED)
        if now_ms >= self._deadline_ms:
            return self._mark_stopped(ProductRuntimeStopReason.DEADLINE_EXCEEDED)
        return None

    def start_watchdog(self) -> None:
        if self.refresh() is not None:
            return
        with self._lock:
            if self._monitor is not None:
                return
            monitor = threading.Thread(
                target=self._watch,
                name="dreamland-product-command-watchdog",
                daemon=True,
            )
            self._monitor = monitor
        monitor.start()

    def close_watchdog(self) -> None:
        self._monitor_done.set()
        with self._lock:
            monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=self._poll_interval_seconds + 0.05)
            if monitor.is_alive():
                LOGGER.debug("Product command cancellation watchdog did not stop")

    def _watch(self) -> None:
        while not self._monitor_done.wait(self._poll_interval_seconds):
            if self.refresh() is not None:
                return

    def _mark_stopped(
        self,
        reason: ProductRuntimeStopReason,
    ) -> ProductRuntimeStopReason:
        callbacks: tuple[Callable[[], None], ...] = ()
        with self._lock:
            if self._reason is None:
                self._reason = reason
                callbacks = tuple(self._callbacks)
                self._callbacks.clear()
            selected = self._reason
        for callback in callbacks:
            _invoke_cancel_callback(callback)
        return selected


@dataclass(slots=True)
class _UsageMeasurement:
    value: object = None
    started: bool = False
    agent_dispatched: bool = False


class _StreamedOutputEvidence:
    """Constant-size evidence for one already-forwarded output prefix.

    The agent returns complete stdout/stderr after streaming candidate chunks.
    Retaining those chunks merely to de-duplicate the returned values would let
    an endless provider grow adapter memory even after the owning core clips
    output.  A rolling digest and character count preserve prefix verification
    without retaining the text itself.
    """

    __slots__ = ("_character_count", "_digest")

    def __init__(self) -> None:
        self._character_count = 0
        self._digest = hashlib.sha256()

    @property
    def character_count(self) -> int:
        return self._character_count

    def append(self, text: str) -> None:
        next_count = self._character_count + len(text)
        if next_count > _MAX_USAGE:
            raise OverflowError("streamed product output evidence is exhausted")
        self._digest.update(text.encode("utf-8"))
        self._character_count = next_count

    def matches_complete_prefix(self, complete: str) -> bool:
        if len(complete) < self._character_count:
            return False
        candidate = hashlib.sha256()
        for start in range(
            0,
            self._character_count,
            _OUTPUT_EVIDENCE_CHUNK_CHARACTERS,
        ):
            candidate.update(
                complete[
                    start : min(
                        start + _OUTPUT_EVIDENCE_CHUNK_CHARACTERS,
                        self._character_count,
                    )
                ].encode("utf-8")
            )
        return candidate.digest() == self._digest.digest()


class ProductRuntimeCommandExecutor:
    """Adapt a product-owned agent to the protocol-neutral command boundary."""

    def __init__(
        self,
        *,
        agent: ProductRuntimeAgent,
        state_codec: ProductStateProfileCodec,
        checkpoint_reaper: ProductCheckpointReaper,
        usage_meter: ProductUsageMeter,
        clock_ms: Callable[[], int],
        cancellation_poll_interval_ms: int = 10,
        publication_chunk_bytes: int | None = None,
    ) -> None:
        if not callable(clock_ms):
            raise TypeError("clock_ms must be callable")
        if (
            isinstance(cancellation_poll_interval_ms, bool)
            or not isinstance(cancellation_poll_interval_ms, int)
            or not 1 <= cancellation_poll_interval_ms <= 1_000
        ):
            raise ValueError("cancellation_poll_interval_ms must be between 1 and 1000")
        if publication_chunk_bytes is not None and (
            isinstance(publication_chunk_bytes, bool)
            or not isinstance(publication_chunk_bytes, int)
            or not 1 <= publication_chunk_bytes <= _MAX_USAGE
        ):
            raise ValueError(
                "publication_chunk_bytes must be a positive bounded integer"
            )
        self._agent = agent
        self._state_codec = state_codec
        self._checkpoint_reaper = checkpoint_reaper
        self._usage_meter = usage_meter
        self._clock_ms = clock_ms
        self._cancellation_poll_interval_ms = cancellation_poll_interval_ms
        self._publication_chunk_bytes = publication_chunk_bytes

    def execute(
        self,
        request: ProductExecutionRequest,
        emit: ProductOutputPublisher,
    ) -> ProductExecutionResult:
        control = ProductRuntimeControl(
            cancelled=request.cancelled,
            deadline_ms=request.deadline_ms,
            clock_ms=self._clock_ms,
            poll_interval_ms=self._cancellation_poll_interval_ms,
            limits=ProductRuntimeLimits(
                max_tokens=request.max_tokens,
                max_spend_units=request.max_spend_units,
                max_attempts=request.max_attempts,
            ),
        )
        usage = ProductRuntimeUsage()
        candidate: ProductExecutionResult | None = None
        measurement = _UsageMeasurement()
        measurement_failed = False
        cleanup_failed = False
        fatal_error: BaseException | None = None

        try:
            try:
                candidate = self._prepare_and_execute(
                    request,
                    emit,
                    control,
                    measurement,
                )
            except Exception:
                candidate = ProductExecutionResult.failed("infrastructure_failure")
            except BaseException as error:
                fatal_error = error
        finally:
            try:
                if measurement.started:
                    try:
                        measured = self._usage_meter.finish(measurement.value)
                        if not isinstance(measured, ProductRuntimeUsage):
                            raise TypeError("usage meter returned an invalid result")
                        usage = measured
                    except Exception:
                        measurement_failed = True
                    except BaseException as error:
                        measurement_failed = True
                        if fatal_error is None:
                            fatal_error = error
            finally:
                try:
                    self._checkpoint_reaper.reap(request.checkpoint_namespace)
                except Exception:
                    cleanup_failed = True
                except BaseException as error:
                    cleanup_failed = True
                    if fatal_error is None:
                        fatal_error = error
                try:
                    control.refresh()
                except BaseException as error:
                    if fatal_error is None:
                        fatal_error = error
                try:
                    control.close_watchdog()
                except BaseException as error:
                    if fatal_error is None:
                        fatal_error = error

        if fatal_error is not None:
            raise fatal_error

        accounting_usage = (
            ProductRuntimeUsage(
                tokens=control.limits.max_tokens,
                spend_units=control.limits.max_spend_units,
            )
            if measurement_failed and measurement.agent_dispatched
            else usage
        )
        if cleanup_failed:
            return _failed("checkpoint_cleanup_failed", accounting_usage)
        if control.stop_reason is ProductRuntimeStopReason.OBSERVATION_FAILED:
            return _failed(
                ProductRuntimeStopReason.OBSERVATION_FAILED.value,
                accounting_usage,
            )
        if measurement_failed:
            return _failed("usage_measurement_failed", accounting_usage)
        if (
            usage.tokens > control.limits.max_tokens
            or usage.spend_units > control.limits.max_spend_units
        ):
            return _failed("resource_usage_exceeded", usage)
        stopped = _stopped_result(
            control.stop_reason,
            usage,
            before_dispatch=not measurement.agent_dispatched,
        )
        if stopped is not None:
            return stopped
        if candidate is None:
            return _failed("agent_execution_failed", usage)
        return replace(
            candidate,
            usage_tokens=usage.tokens,
            usage_spend_units=usage.spend_units,
        )

    def _prepare_and_execute(
        self,
        request: ProductExecutionRequest,
        emit: ProductOutputPublisher,
        control: ProductRuntimeControl,
        measurement: _UsageMeasurement,
    ) -> ProductExecutionResult:
        stopped = _stopped_result(
            control.stop_reason,
            ProductRuntimeUsage(),
            before_dispatch=True,
        )
        if stopped is not None:
            return stopped
        try:
            detached_document = deepcopy(dict(request.provisional_document))
            runtime = self._state_codec.decode(
                detached_document,
                checkpoint_namespace=request.checkpoint_namespace,
            )
            if not isinstance(runtime, ProductRuntimeState):
                raise TypeError("state codec returned an invalid runtime")
            context = _command_context(runtime, request)
        except Exception:
            return ProductExecutionResult.failed("invalid_provisional_state")

        stopped = _stopped_result(
            control.stop_reason,
            ProductRuntimeUsage(),
            before_dispatch=True,
        )
        if stopped is not None:
            return stopped

        control.start_watchdog()
        try:
            ticket = self._usage_meter.begin(
                command_id=request.command_id,
                checkpoint_namespace=request.checkpoint_namespace,
                limits=control.limits,
            )
        except Exception:
            return ProductExecutionResult.failed("usage_measurement_failed")
        measurement.value = ticket
        measurement.started = True

        stopped = _stopped_result(
            control.stop_reason,
            ProductRuntimeUsage(),
            before_dispatch=True,
        )
        if stopped is not None:
            return stopped

        rendered = {
            "stdout": _StreamedOutputEvidence(),
            "stderr": _StreamedOutputEvidence(),
        }
        output_failure: list[str] = []
        output_lock = threading.Lock()
        output_suppressed = threading.Event()

        def remember_output_failure(reason: str) -> None:
            if not output_failure:
                output_failure.append(reason)

        def emit_runtime(chunk: CommandOutputChunk) -> None:
            if output_suppressed.is_set():
                return
            if control.cancelled():
                return
            with output_lock:
                if output_suppressed.is_set() or control.cancelled():
                    return
                if not isinstance(chunk, CommandOutputChunk):
                    remember_output_failure("invalid_agent_output")
                    raise TypeError("agent output must be CommandOutputChunk")
                if chunk.stream not in rendered or not isinstance(chunk.text, str):
                    remember_output_failure("invalid_agent_output")
                    raise ValueError("agent output chunk is invalid")
                if not chunk.text:
                    return
                evidence = rendered[chunk.stream]
                for text_slice in _text_slices(
                    chunk.text,
                    maximum_utf8_bytes=self._publication_chunk_bytes,
                ):
                    product_chunk = ProductOutputChunk(
                        ProductOutputStream(chunk.stream),
                        text_slice,
                    )
                    try:
                        accepted = emit(product_chunk)
                    except Exception:
                        remember_output_failure("output_delivery_failed")
                        raise
                    if accepted is False:
                        output_suppressed.set()
                        return
                    evidence.append(text_slice)

        measurement.agent_dispatched = True
        try:
            result = self._agent.execute(context, control, emit_runtime)
        except Exception:
            if output_failure:
                return ProductExecutionResult.failed(output_failure[0])
            stopped = _stopped_result(
                control.stop_reason,
                ProductRuntimeUsage(),
                before_dispatch=False,
            )
            return stopped or ProductExecutionResult.failed("agent_execution_failed")

        if output_failure:
            return ProductExecutionResult.failed(output_failure[0])
        stopped = _stopped_result(
            control.stop_reason,
            ProductRuntimeUsage(),
            before_dispatch=False,
        )
        if stopped is not None:
            return stopped
        if not _valid_command_result(result):
            return ProductExecutionResult.failed("invalid_agent_result")
        if not output_suppressed.is_set():
            try:
                suffixes_valid = _emit_result_suffixes(
                    result,
                    rendered,
                    emit_runtime,
                    stop_requested=output_suppressed.is_set,
                    maximum_utf8_bytes=self._publication_chunk_bytes,
                )
            except Exception:
                return ProductExecutionResult.failed(
                    output_failure[0] if output_failure else "output_delivery_failed"
                )
            if not suffixes_valid:
                return ProductExecutionResult.failed("invalid_agent_result")
        stopped = _stopped_result(
            control.stop_reason,
            ProductRuntimeUsage(),
            before_dispatch=False,
        )
        if stopped is not None:
            return stopped
        return self._map_result(runtime, result, command=request.canonical_command)

    def _map_result(
        self,
        runtime: ProductRuntimeState,
        result: CommandResult,
        *,
        command: str,
    ) -> ProductExecutionResult:
        if result.interrupted:
            return ProductExecutionResult.interrupted("agent_interrupted")
        state_effect = self._state_effect(runtime, result.state_update, command=command)
        try:
            if result.signal is not None:
                return ProductExecutionResult.completed_signaled(
                    signal=result.signal,
                    state_effect=state_effect,
                )
            return ProductExecutionResult.completed_exited(
                exit_status=result.exit_status,
                state_effect=state_effect,
            )
        except ValueError:
            return ProductExecutionResult.failed("invalid_agent_result")

    def _state_effect(
        self,
        runtime: ProductRuntimeState,
        update: CommandStateUpdate,
        *,
        command: str,
    ) -> ProductStateEffect:
        if update == CommandStateUpdate():
            return ProductStateEffect(ProductStateEffectKind.NONE)
        try:
            _baseline_document, baseline_canonical = _detached_json_document(
                self._state_codec.encode(runtime)
            )
            with provisional_connection_events():
                apply_command_state_update(runtime.session, update, command=command)
            provisional_document, provisional_canonical = _detached_json_document(
                self._state_codec.encode(runtime)
            )
        except Exception:
            return ProductStateEffect(
                ProductStateEffectKind.REJECTED,
                safe_reason="invalid_state_update",
            )
        if provisional_canonical == baseline_canonical:
            return ProductStateEffect(ProductStateEffectKind.NONE)
        return ProductStateEffect(
            ProductStateEffectKind.COMMITTED,
            provisional_document=provisional_document,
        )


def _command_context(
    runtime: ProductRuntimeState,
    request: ProductExecutionRequest,
) -> CommandContext:
    session = runtime.session
    frame = active_shell_frame(session)
    world_state = deepcopy(session.world_state)
    return CommandContext(
        username=frame.username,
        login_username=session.login_username or session.username,
        cwd=frame.cwd,
        hostname=frame.host_identity.hostname,
        command=request.canonical_command,
        langfuse_trace_id=request.checkpoint_namespace,
        command_id=request.command_id,
        host_identity=deepcopy(frame.host_identity),
        interactive_program=deepcopy(frame.interactive_program),
        shell_environment=deepcopy(frame.shell_environment),
        shell_stack=tuple(deepcopy(session.shell_stack)),
        history=tuple(deepcopy(session.history)),
        environment_metadata=projected_environment_metadata(session),
        world_state=world_state,
        active_host=active_host_ref(world_state),
        active_host_metadata=active_host_metadata_for_context(world_state),
        force_session_state=True,
        transport_connection=session.environment_metadata.connection,
    )


def _valid_command_result(result: object) -> bool:
    return bool(
        isinstance(result, CommandResult)
        and isinstance(result.stdout, str)
        and isinstance(result.stderr, str)
        and isinstance(result.exit_status, int)
        and not isinstance(result.exit_status, bool)
        and isinstance(result.interrupted, bool)
        and (
            result.signal is None
            or (
                isinstance(result.signal, str)
                and _SAFE_SIGNAL.fullmatch(result.signal) is not None
            )
        )
        and isinstance(result.state_update, CommandStateUpdate)
        and isinstance(result.metadata, dict)
    )


def _emit_result_suffixes(
    result: CommandResult,
    rendered: Mapping[str, _StreamedOutputEvidence],
    emit: Callable[[CommandOutputChunk], None],
    *,
    stop_requested: Callable[[], bool],
    maximum_utf8_bytes: int | None = None,
) -> bool:
    for stream, complete in (("stdout", result.stdout), ("stderr", result.stderr)):
        if stop_requested():
            return True
        evidence = rendered[stream]
        if not evidence.matches_complete_prefix(complete):
            return False
        for suffix in _text_slices(
            complete,
            start=evidence.character_count,
            maximum_utf8_bytes=maximum_utf8_bytes,
        ):
            emit(CommandOutputChunk(suffix, stream))  # type: ignore[arg-type]
            if stop_requested():
                return True
    return True


def _text_slices(
    text: str,
    *,
    start: int = 0,
    maximum_utf8_bytes: int | None = None,
) -> Iterator[str]:
    """Yield bounded slices without retaining the complete prefix."""

    while start < len(text):
        if maximum_utf8_bytes is None:
            end = min(start + _OUTPUT_EVIDENCE_CHUNK_CHARACTERS, len(text))
            yield text[start:end]
            start = end
            continue

        character_end = min(start + maximum_utf8_bytes, len(text))
        encoded = text[start:character_end].encode("utf-8")
        prefix = encoded[:maximum_utf8_bytes].decode("utf-8", errors="ignore")
        if not prefix:
            raise ValueError("publication chunk size cannot contain one character")
        yield prefix
        start += len(prefix)


def _detached_json_document(
    document: Mapping[str, object],
) -> tuple[dict[str, object], bytes]:
    if not isinstance(document, Mapping):
        raise TypeError("encoded runtime document must be a mapping")
    canonical = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    parsed = json.loads(canonical)
    if not isinstance(parsed, dict):
        raise TypeError("encoded runtime document must be an object")
    return parsed, canonical


def _remove_runtime_profile_metadata(state: dict[str, object]) -> None:
    environment = state.get("environment_metadata")
    if not isinstance(environment, dict):
        return
    metadata = environment.get("metadata")
    if not isinstance(metadata, dict):
        return
    metadata.pop(STATE_PROFILE_METADATA_NAME, None)
    metadata.pop(STATE_PROFILE_METADATA_LEVEL, None)


def _stopped_result(
    reason: ProductRuntimeStopReason | None,
    usage: ProductRuntimeUsage,
    *,
    before_dispatch: bool,
) -> ProductExecutionResult | None:
    if reason is ProductRuntimeStopReason.CANCELLED:
        return ProductExecutionResult.interrupted(
            "cancelled_before_dispatch" if before_dispatch else "cancelled",
            usage_tokens=usage.tokens,
            usage_spend_units=usage.spend_units,
        )
    if reason is ProductRuntimeStopReason.DEADLINE_EXCEEDED:
        return _failed(reason.value, usage)
    if reason is ProductRuntimeStopReason.OBSERVATION_FAILED:
        return _failed(reason.value, usage)
    if reason is ProductRuntimeStopReason.ATTEMPT_LIMIT_EXCEEDED:
        return _failed(reason.value, usage)
    return None


def _failed(reason: str, usage: ProductRuntimeUsage) -> ProductExecutionResult:
    return ProductExecutionResult.failed(
        reason,
        usage_tokens=usage.tokens,
        usage_spend_units=usage.spend_units,
    )


def _invoke_cancel_callback(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception:
        # Provider-authored exception text and trace details are not product-safe
        # telemetry. The fixed event preserves the lifecycle signal only.
        LOGGER.debug("Product command cancellation callback failed")


__all__ = [
    "DreamlandStateProfileCodec",
    "ProductCheckpointReaper",
    "ProductRuntimeAgent",
    "ProductRuntimeCommandExecutor",
    "ProductRuntimeControl",
    "ProductRuntimeLimits",
    "ProductRuntimeState",
    "ProductRuntimeStopReason",
    "ProductRuntimeUsage",
    "ProductStateProfileCodec",
    "ProductUsageMeter",
]
