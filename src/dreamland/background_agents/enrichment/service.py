"""Session-local asynchronous runtime for the EnrichmentAgent graph."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from dreamland.background_agents.authority import (
    SESSION_SEMANTICS_AUTHORITY_DOMAIN,
    capture_foreground_authority,
)
from dreamland.background_agents.enrichment.authentication import (
    foreground_authentication_paths,
)
from dreamland.background_agents.enrichment.context import (
    EnrichmentContextProjection,
    EnrichmentTargetNotFoundError,
    canonical_json,
    project_world_state_for_enrichment,
    resolve_enrichment_host_ref,
)
from dreamland.background_agents.enrichment.control import (
    EnrichmentJobSnapshot,
    EnrichmentRuntimeStatus,
    EnrichmentSubmission,
)
from dreamland.background_agents.enrichment.foreground_evidence import (
    FOREGROUND_EVIDENCE_AUTHORITY_DOMAIN,
    FOREGROUND_EVIDENCE_POLICY_VERSION,
    capture_foreground_evidence,
)
from dreamland.background_agents.enrichment.graph import (
    ENRICHMENT_GRAPH_VERSION,
    EnrichmentAgent,
    EnrichmentGraphResult,
    EnrichmentGraphStatus,
    EnrichmentJobContext,
    EnrichmentModelFailure,
)
from dreamland.background_agents.enrichment.modes import EnrichmentMode
from dreamland.background_agents.enrichment.patching import apply_enrichment_patch
from dreamland.background_agents.enrichment.role_outputs import (
    enrichment_trace_metadata,
    enrichment_trace_tags,
)
from dreamland.background_agents.enrichment.telemetry import (
    EnrichmentLogContext,
    log_enrichment_event,
)
from dreamland.background_agents.event_bus import (
    EventSubscription,
    SessionEventBus,
    new_session_event,
)
from dreamland.background_agents.events import (
    EventSource,
    SessionEvent,
    SessionEventType,
    TriggerCandidate,
    TriggerDecision,
    TriggerDecisionCode,
    TriggerDecisionReason,
)
from dreamland.background_agents.focus import (
    PresenceProbeKind,
    classify_interaction_focus_match,
    interaction_focus_counts,
    interaction_focus_key,
    interaction_focus_subject_key,
)
from dreamland.background_agents.triggers import (
    TriggerGatePolicy,
    TriggerGateState,
    evaluate_trigger_gate,
)
from dreamland.config import EnrichmentSettings
from dreamland.models import RuntimeProvenance
from dreamland.provider_failures import (
    ProviderFailureClassification,
    ProviderFailureDisposition,
    ProviderFailureKind,
)
from dreamland.session_registry import LiveSessionRegistry, RegisteredSession
from dreamland.world_state import active_host_ref

ENRICHMENT_AGENT_ID = "enrichment"
_RATE_WINDOW_MS = 60_000
_LONG_LOCK_WAIT_MS = 100
_JOB_HISTORY_LIMIT = 200
_FOCUS_TRIGGER_THRESHOLD = 2
_MAX_JOB_ANCHORS = 8
_DEFERRED_MAX_AGE_MS = 60_000
_FOCUS_MATCH_POLICY = "interaction-focus-match-v1"
_INTERACTION_PULSE_THRESHOLD = 4
_TRIGGER_MODE = {
    "manual": EnrichmentMode.FOCUSED,
    "active_host_changed": EnrichmentMode.FOCUSED,
    "host_discovered": EnrichmentMode.FOCUSED,
    "subject_first_touch": EnrichmentMode.FOCUSED,
    "surface_focus": EnrichmentMode.FOCUSED,
    "state_materialized": EnrichmentMode.FOCUSED,
    "host_presence_bootstrap": EnrichmentMode.AMBIENT_PRESENCE,
    "presence_probe": EnrichmentMode.AMBIENT_PRESENCE,
    "materialization_followthrough": EnrichmentMode.AMBIENT_PRESENCE,
    "interaction_pulse": EnrichmentMode.AMBIENT_PRESENCE,
    "session_idle": EnrichmentMode.AMBIENT_PRESENCE,
}
_TRIGGER_PRIORITY = {
    # Focused work always wins the single deferred slot over ambient work.
    "manual": 160,
    "active_host_changed": 150,
    "host_discovered": 140,
    "subject_first_touch": 130,
    "surface_focus": 120,
    "state_materialized": 110,
    "host_presence_bootstrap": 50,
    "presence_probe": 40,
    "materialization_followthrough": 30,
    "interaction_pulse": 20,
    "session_idle": 10,
}


@dataclass(slots=True)
class _JobRecord:
    job_id: str
    candidate_id: str
    event: SessionEvent
    trigger_id: str
    mode: EnrichmentMode
    target_host: str
    surface: str | None
    selected_surface: str | None
    status: str
    reason: str | None
    queued_at: datetime
    queued_monotonic: float
    automatic: bool
    runtime_generation: int
    started_at: datetime | None = None
    started_monotonic: float | None = None
    completed_at: datetime | None = None
    completed_monotonic: float | None = None
    runtime_provenance: RuntimeProvenance | None = None
    cancellation_reason: str | None = None

    def snapshot(self) -> EnrichmentJobSnapshot:
        duration_ms = None
        if self.started_monotonic is not None and self.completed_monotonic is not None:
            duration_ms = round(
                (self.completed_monotonic - self.started_monotonic) * 1000
            )
        return EnrichmentJobSnapshot(
            job_id=self.job_id,
            session_id=self.event.session_id,
            trigger=self.trigger_id,
            target_host=self.target_host,
            surface=self.surface,
            status=self.status,
            mode=self.mode.value,
            reason=self.reason,
            queued_at=_format_time(self.queued_at),
            started_at=_format_time(self.started_at),
            completed_at=_format_time(self.completed_at),
            duration_ms=duration_ms,
        )


@dataclass(frozen=True, slots=True)
class _DeferredRecord:
    """One process-local automatic candidate retained behind active work."""

    candidate: TriggerCandidate
    registration: RegisteredSession
    runtime_generation: int
    deferred_at_ms: int
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class _ActiveBootstrapRetry:
    """One bounded eventual-admission marker for the session's active host."""

    event: SessionEvent
    registration: RegisteredSession
    runtime_generation: int
    target_host: str
    retained_at_ms: int


@dataclass(frozen=True, slots=True)
class _IntegratorRefreshRequest:
    """Validated inputs retained for one Integrator-only recovery attempt."""

    context: EnrichmentJobContext
    previous_result: EnrichmentGraphResult
    initial_snapshot: EnrichmentContextProjection
    capture_current_snapshot: Callable[
        [EnrichmentJobContext],
        EnrichmentContextProjection,
    ]
    trace_run_manager: Any | None


class _IntegratorRefreshTargetNotForeground(RuntimeError):
    """The target stopped being foreground before the refresh snapshot."""


@dataclass(frozen=True, slots=True)
class _ProviderCircuit:
    """One terminal provider condition suppressing automatic model work."""

    provider: str
    session_id: str
    classification: ProviderFailureClassification
    origin: str
    opened_at: datetime
    role: str | None = None
    model: str | None = None

    def log_fields(self) -> dict[str, str | int]:
        fields: dict[str, str | int] = {
            "provider": self.provider,
            "circuit_origin": self.origin,
            **self.classification.to_log_fields(),
        }
        if self.role is not None:
            fields["role"] = self.role
        if self.model is not None:
            fields["model"] = self.model
        return fields


@dataclass(slots=True)
class _ScheduledJob:
    future: Future[None]
    candidate: TriggerCandidate | None
    registration: RegisteredSession | None


class EnrichmentRuntimeService:
    """Coordinates triggers, snapshots, graph calls, and safe patch application."""

    def __init__(
        self,
        settings: EnrichmentSettings,
        *,
        session_registry: LiveSessionRegistry,
        event_bus: SessionEventBus,
        agent: EnrichmentAgent,
        callbacks: Sequence[Any] = (),
        max_workers: int = 4,
    ) -> None:
        self.settings = settings
        self.session_registry = session_registry
        self.event_bus = event_bus
        self.agent = agent
        self.callbacks = tuple(callbacks)
        self.max_workers = max(1, max_workers)
        self._max_scheduled_jobs = max(16, self.max_workers * 4)
        self._scheduled_jobs: dict[str, _ScheduledJob] = {}
        self._lock = threading.RLock()
        self._started = False
        self._stopping = False
        self._runtime_generation = 0
        self._draining_generations: set[int] = set()
        self._subscription: EventSubscription | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._jobs: dict[str, _JobRecord] = {}
        self._job_order: deque[str] = deque()
        self._pending_by_key: dict[str, str] = {}
        self._inflight_by_session: dict[str, int] = {}
        self._inflight_by_session_mode: dict[tuple[str, EnrichmentMode], int] = {}
        self._last_accepted_by_scope: dict[str, int] = {}
        self._accepted_by_session: dict[str, deque[int]] = {}
        self._idle_timers: dict[str, threading.Timer] = {}
        self._idle_tokens: dict[str, str] = {}
        self._focus: dict[str, tuple[str, str, int]] = {}
        self._interaction_counts: dict[tuple[str, str], int] = {}
        self._seen_focus_subjects: set[tuple[str, str, str]] = set()
        self._presence_bootstrapped_hosts: set[tuple[str, str]] = set()
        self._deferred_by_session: dict[str, _DeferredRecord] = {}
        self._active_bootstrap_retries: dict[str, _ActiveBootstrapRetry] = {}
        self._active_bootstrap_retrying: set[str] = set()
        self._deferred_release_count = 0
        self._provider_circuits: dict[str, _ProviderCircuit] = {}
        self._completed_count = 0
        self._failed_count = 0
        self._available_jobs: set[str] = set()
        self._matched_jobs: set[str] = set()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            if self._stopping or self._draining_generations:
                raise RuntimeError("enrichment runtime is stopping or draining")
            runtime_generation = self._runtime_generation + 1
            executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="dreamland-enrichment",
            )

            def handle_runtime_event(event: SessionEvent) -> None:
                self._handle_event(
                    event,
                    runtime_generation=runtime_generation,
                )

            try:
                subscription = self.event_bus.subscribe(
                    ENRICHMENT_AGENT_ID,
                    handle_runtime_event,
                )
            except Exception:
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            reset_circuits = tuple(self._provider_circuits.values())
            self._provider_circuits.clear()
            self._runtime_generation = runtime_generation
            self._executor = executor
            self._subscription = subscription
            self._started = True
        for circuit in reset_circuits:
            log_enrichment_event(
                "provider_circuit.reset",
                context=EnrichmentLogContext(session_id=circuit.session_id),
                reason="runtime_restarted",
                provider_circuit_count=len(reset_circuits),
                **circuit.log_fields(),
            )
        log_enrichment_event(
            "runtime.started",
            runtime_generation=runtime_generation,
            automatic_enabled=self.settings.enabled,
            apply_mode=self.settings.apply_mode,
            triggers=sorted(self.settings.triggers),
            max_jobs_per_minute=self.settings.max_jobs_per_minute,
            max_inflight_per_session=self.settings.max_inflight_per_session,
            cooldown_ms=self.settings.cooldown_ms,
            idle_delay_ms=self.settings.idle_delay_ms,
            event_log_capacity=self.settings.event_log_capacity,
            max_scheduled_jobs=self._max_scheduled_jobs,
            deferred_max_age_ms=_DEFERRED_MAX_AGE_MS,
            interaction_pulse_threshold=_INTERACTION_PULSE_THRESHOLD,
            trigger_priority=dict(_TRIGGER_PRIORITY),
            trigger_modes={
                trigger_id: mode.value for trigger_id, mode in _TRIGGER_MODE.items()
            },
        )

    def stop(self, *, wait: bool = True) -> None:
        with self._lock:
            if not self._started:
                return
            runtime_generation = self._runtime_generation
            self._started = False
            self._stopping = True
            if wait:
                self._draining_generations.add(runtime_generation)
            subscription = self._subscription
            self._subscription = None
            executor = self._executor
            self._executor = None
            timers = tuple(self._idle_timers.values())
            self._idle_timers.clear()
            self._idle_tokens.clear()
            deferred = tuple(self._deferred_by_session.values())
            self._deferred_by_session.clear()
            self._pending_by_key.clear()
            self._inflight_by_session.clear()
            self._inflight_by_session_mode.clear()
            self._last_accepted_by_scope.clear()
            self._accepted_by_session.clear()
            self._focus.clear()
            self._interaction_counts.clear()
            self._seen_focus_subjects.clear()
            self._presence_bootstrapped_hosts.clear()
            self._active_bootstrap_retries.clear()
            self._active_bootstrap_retrying.clear()
        try:
            if subscription is not None:
                subscription.close()
            for timer in timers:
                timer.cancel()
            for record in deferred:
                self._log_deferred_cancelled(record, reason="runtime_stopped")
            if executor is not None:
                executor.shutdown(wait=wait, cancel_futures=not wait)
        finally:
            with self._lock:
                self._draining_generations.discard(runtime_generation)
                self._stopping = False
        log_enrichment_event(
            "runtime.stopped",
            wait=wait,
            runtime_generation=runtime_generation,
        )

    def status(self) -> EnrichmentRuntimeStatus:
        with self._lock:
            queued = sum(
                job.status == "queued"
                and job.runtime_generation == self._runtime_generation
                for job in self._jobs.values()
            )
            return EnrichmentRuntimeStatus(
                configured=True,
                started=self._started,
                automatic_enabled=self.settings.enabled,
                apply_mode=self.settings.apply_mode,
                inflight=sum(self._inflight_by_session.values()),
                queued=queued,
                completed=self._completed_count,
                failed=self._failed_count,
            )

    def submit_manual(
        self,
        session_id: str,
        target_host: str | None = None,
        surface: str | None = None,
    ) -> EnrichmentSubmission:
        with self._lock:
            if not self._started:
                return EnrichmentSubmission(
                    accepted=False,
                    code="suppressed",
                    reason="runtime_not_started",
                )
        registration = self.session_registry.get(session_id)
        if registration is None:
            return EnrichmentSubmission(
                accepted=False,
                code="suppressed",
                reason="session_missing",
            )
        resolved_target = target_host or self._active_target(
            registration,
            stage="manual_target",
        )
        event = new_session_event(
            session_id=session_id,
            event_type=SessionEventType.STATE_MATERIALIZED,
            source=EventSource.ADMIN,
            target_host=resolved_target,
            surface=surface,
            payload={"kind": "manual"},
        )
        return self._consider(
            event,
            trigger_id="manual",
            target_host=resolved_target,
            surface=surface,
            automatic=False,
        )

    def list_jobs(self, limit: int) -> tuple[EnrichmentJobSnapshot, ...]:
        if limit < 0:
            raise ValueError("limit must be zero or greater")
        with self._lock:
            job_ids = tuple(reversed(self._job_order))[:limit]
            return tuple(self._jobs[job_id].snapshot() for job_id in job_ids)

    def graph_mermaid(self) -> str:
        main = self.agent.topology_mermaid(expanded=True)
        refresh_topology = getattr(
            self.agent,
            "integrator_refresh_topology_mermaid",
            None,
        )
        if not callable(refresh_topology):
            return main
        refresh = refresh_topology(expanded=True)
        return (
            "Main enrichment proposal graph\n\n"
            f"```mermaid\n{main.rstrip()}\n```\n\n"
            "Bounded Integrator refresh graph (attempt 1)\n\n"
            f"```mermaid\n{refresh.rstrip()}\n```"
        )

    def wait_until_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if (
                    not self._inflight_by_session
                    and not any(
                        job.status == "queued"
                        and job.runtime_generation == self._runtime_generation
                        for job in self._jobs.values()
                    )
                    and not self._deferred_by_session
                    and self._deferred_release_count == 0
                ):
                    return True
            time.sleep(0.01)
        return False

    def _handle_event(
        self,
        event: SessionEvent,
        *,
        runtime_generation: int,
    ) -> None:
        log_context = _event_log_context(event)
        if not self._runtime_is_current(runtime_generation):
            with self._lock:
                current_runtime_generation = self._runtime_generation
            log_enrichment_event(
                "trigger.suppressed",
                level=logging.DEBUG,
                context=log_context,
                reason="stale_runtime_event",
                event_type=event.event_type.value,
                event_runtime_generation=runtime_generation,
                runtime_generation=current_runtime_generation,
            )
            return
        log_enrichment_event(
            "trigger.considered",
            level=logging.DEBUG,
            context=log_context,
            event_type=event.event_type.value,
            source=event.source.value,
        )
        if event.event_type is SessionEventType.SESSION_CLOSING:
            self._forget_session(
                event.session_id,
                runtime_generation=runtime_generation,
            )
            return
        if event.event_type is SessionEventType.COMMAND_ACCEPTED:
            self._cancel_idle(
                event.session_id,
                reason="command_accepted",
                runtime_generation=runtime_generation,
            )
            self._cancel_deferred_idle(
                event.session_id,
                reason="command_accepted",
                runtime_generation=runtime_generation,
            )
            self._observe_enrichment_command_match(
                event,
                runtime_generation=runtime_generation,
            )
            return
        if not self.settings.enabled:
            if event.event_type in {
                SessionEventType.SESSION_REGISTERED,
                SessionEventType.COMMAND_COMPLETED,
                SessionEventType.ACTIVE_HOST_CHANGED,
                SessionEventType.STATE_MATERIALIZED,
                SessionEventType.SESSION_IDLE,
            }:
                log_enrichment_event(
                    "trigger.suppressed",
                    level=logging.DEBUG,
                    context=log_context,
                    reason="automatic_disabled",
                    event_type=event.event_type.value,
                )
            return
        if event.event_type is SessionEventType.SESSION_REGISTERED:
            self._consider_host_presence_bootstrap(
                event,
                target_host=event.target_host,
                runtime_generation=runtime_generation,
                active_host=True,
            )
            return
        if event.event_type is SessionEventType.COMMAND_COMPLETED:
            self._observe_foreground_provider_failure(
                event,
                runtime_generation=runtime_generation,
            )
            pulse_count = self._record_interaction(event, runtime_generation)
            pulse_candidate = (
                "interaction_pulse" in self.settings.triggers
                and event.target_host is not None
                and pulse_count >= _INTERACTION_PULSE_THRESHOLD
            )
            if pulse_candidate:
                self._cancel_idle(
                    event.session_id,
                    reason="interaction_pulse_candidate",
                    runtime_generation=runtime_generation,
                )
                self._cancel_deferred_idle(
                    event.session_id,
                    reason="interaction_pulse_candidate",
                    runtime_generation=runtime_generation,
                    correlation_id=event.correlation_id,
                )
                if "session_idle" in self.settings.triggers:
                    log_enrichment_event(
                        "trigger.suppressed",
                        level=logging.DEBUG,
                        context=_event_log_context(
                            event,
                            trigger_id="session_idle",
                            enrichment_mode=EnrichmentMode.AMBIENT_PRESENCE,
                        ),
                        reason="higher_priority_correlated_candidate",
                        higher_priority_trigger="interaction_pulse",
                        pulse_count=pulse_count,
                    )
            else:
                self._schedule_idle(
                    event,
                    runtime_generation=runtime_generation,
                )
            focused_followup = self._command_followup_priority(
                event,
                mode=EnrichmentMode.FOCUSED,
            )
            if focused_followup is None:
                if not self._consider_subject_first_touch(
                    event,
                    runtime_generation=runtime_generation,
                ):
                    self._consider_surface_focus(
                        event,
                        runtime_generation=runtime_generation,
                    )
            else:
                for trigger_id in ("subject_first_touch", "surface_focus"):
                    self._log_command_candidate_preempted(
                        event,
                        trigger_id=trigger_id,
                        higher_priority_trigger=focused_followup,
                    )

            self._retry_active_host_bootstrap(
                event.session_id,
                runtime_generation=runtime_generation,
                action="command_completed",
            )

            ambient_followup = self._command_followup_priority(
                event,
                mode=EnrichmentMode.AMBIENT_PRESENCE,
            )
            if ambient_followup is not None:
                for trigger_id in ("presence_probe", "interaction_pulse"):
                    self._log_command_candidate_preempted(
                        event,
                        trigger_id=trigger_id,
                        higher_priority_trigger=ambient_followup,
                    )
            elif not self._consider_presence_probe(
                event,
                runtime_generation=runtime_generation,
            ):
                self._consider_interaction_pulse(
                    event,
                    pulse_count=pulse_count,
                    runtime_generation=runtime_generation,
                )
            return
        if event.event_type is SessionEventType.ACTIVE_HOST_CHANGED:
            self._consider_configured(
                event,
                "active_host_changed",
                runtime_generation=runtime_generation,
            )
            self._consider_host_presence_bootstrap(
                event,
                target_host=event.target_host,
                runtime_generation=runtime_generation,
                active_host=True,
            )
            return
        if event.event_type is SessionEventType.STATE_MATERIALIZED:
            kind = str(event.payload.get("kind", "state_materialized"))
            if kind == "host_discovered":
                target_host = event.anchors[0] if event.anchors else event.target_host
                if (
                    event.payload.get("active_host_changed") is True
                    and "active_host_changed" in self.settings.triggers
                ):
                    self._log_command_candidate_preempted(
                        event,
                        trigger_id="host_discovered",
                        higher_priority_trigger="active_host_changed",
                    )
                else:
                    self._consider_configured(
                        event,
                        "host_discovered",
                        target_host=target_host,
                        runtime_generation=runtime_generation,
                    )
                discovered_hosts = event.anchors or _text_tuple(
                    event.payload.get("new_hosts")
                )
                if not discovered_hosts and target_host is not None:
                    discovered_hosts = (target_host,)
                for discovered_host in dict.fromkeys(discovered_hosts):
                    self._consider_host_presence_bootstrap(
                        event,
                        target_host=discovered_host,
                        runtime_generation=runtime_generation,
                        active_host=False,
                    )
                if "host_presence_bootstrap" not in self.settings.triggers:
                    self._consider_configured(
                        event,
                        "materialization_followthrough",
                        target_host=target_host,
                        runtime_generation=runtime_generation,
                    )
                return
            self._consider_configured(
                event,
                "state_materialized",
                runtime_generation=runtime_generation,
            )
            self._consider_configured(
                event,
                "materialization_followthrough",
                runtime_generation=runtime_generation,
            )
            return
        if event.event_type is SessionEventType.SESSION_IDLE:
            self._retry_active_host_bootstrap(
                event.session_id,
                runtime_generation=runtime_generation,
                action="session_idle",
            )
            self._consider_configured(
                event,
                "session_idle",
                runtime_generation=runtime_generation,
            )

    def _command_followup_priority(
        self,
        event: SessionEvent,
        *,
        mode: EnrichmentMode,
    ) -> str | None:
        """Identify a more specific signal that the backend emits next."""

        active_changed = event.payload.get("active_host_changed") is True
        new_hosts = _text_tuple(event.payload.get("new_hosts"))
        if mode is EnrichmentMode.FOCUSED:
            if active_changed and "active_host_changed" in self.settings.triggers:
                return "active_host_changed"
            if new_hosts and "host_discovered" in self.settings.triggers:
                return "host_discovered"
            if (
                event.payload.get("state_update_present") is True
                and "state_materialized" in self.settings.triggers
            ):
                return "state_materialized"
            return None
        if (
            active_changed or new_hosts
        ) and "host_presence_bootstrap" in self.settings.triggers:
            return "host_presence_bootstrap"
        if (
            event.payload.get("state_update_present") is True
            and "materialization_followthrough" in self.settings.triggers
        ):
            return "materialization_followthrough"
        return None

    def _log_command_candidate_preempted(
        self,
        event: SessionEvent,
        *,
        trigger_id: str,
        higher_priority_trigger: str,
    ) -> None:
        if trigger_id not in self.settings.triggers:
            return
        mode = _trigger_mode(trigger_id)
        log_enrichment_event(
            "trigger.suppressed",
            level=logging.DEBUG,
            context=_event_log_context(
                event,
                trigger_id=trigger_id,
                enrichment_mode=mode,
            ),
            reason="higher_priority_correlated_candidate",
            higher_priority_trigger=higher_priority_trigger,
        )

    def _consider_host_presence_bootstrap(
        self,
        event: SessionEvent,
        *,
        target_host: str | None,
        runtime_generation: int,
        active_host: bool,
    ) -> bool:
        trigger_id = "host_presence_bootstrap"
        if trigger_id not in self.settings.triggers:
            self._log_trigger_not_configured(event, trigger_id)
            return False
        registration = self.session_registry.get(event.session_id)
        if registration is None:
            self._log_missing_session(event, trigger_id)
            return False
        if target_host is None:
            target_host = self._active_target(registration, stage="bootstrap_target")
        bootstrap_key = (event.session_id, target_host)
        with self._lock:
            if not self._runtime_is_current_locked(runtime_generation):
                return False
            if bootstrap_key in self._presence_bootstrapped_hosts:
                log_enrichment_event(
                    "trigger.suppressed",
                    level=logging.DEBUG,
                    context=_event_log_context(
                        event,
                        trigger_id=trigger_id,
                        target_host=target_host,
                        enrichment_mode=EnrichmentMode.AMBIENT_PRESENCE,
                    ),
                    reason="host_presence_already_bootstrapped",
                )
                return False
        result = self._consider(
            event,
            trigger_id=trigger_id,
            target_host=target_host,
            surface="host",
            automatic=True,
            _expected_registration=registration,
            _expected_runtime_generation=runtime_generation,
            _detail_overrides={"active_host_bootstrap": active_host},
        )
        if active_host and not result.accepted:
            self._retain_active_host_bootstrap_retry(
                event,
                registration=registration,
                runtime_generation=runtime_generation,
                target_host=target_host,
                reason=result.reason,
            )
        return result.accepted

    def _retain_active_host_bootstrap_retry(
        self,
        event: SessionEvent,
        *,
        registration: RegisteredSession,
        runtime_generation: int,
        target_host: str,
        reason: str,
    ) -> None:
        """Retain at most the current active host for eventual admission."""

        now_ms = _monotonic_ms()
        new_record = _ActiveBootstrapRetry(
            event=event,
            registration=registration,
            runtime_generation=runtime_generation,
            target_host=target_host,
            retained_at_ms=now_ms,
        )
        with self._lock:
            if (
                not self._runtime_is_current_locked(runtime_generation)
                or self.session_registry.get(event.session_id) is not registration
                or (event.session_id, target_host) in self._presence_bootstrapped_hosts
            ):
                return
            existing = self._active_bootstrap_retries.get(event.session_id)
            if (
                existing is not None
                and existing.registration is registration
                and existing.target_host == target_host
            ):
                action = "retained"
                retained = existing
            else:
                action = "replaced" if existing is not None else "stored"
                self._active_bootstrap_retries[event.session_id] = new_record
                retained = new_record
        log_enrichment_event(
            f"bootstrap.retry_{action}",
            context=_event_log_context(
                event,
                trigger_id="host_presence_bootstrap",
                target_host=target_host,
                enrichment_mode=EnrichmentMode.AMBIENT_PRESENCE,
            ),
            action=action,
            reason=reason,
            priority=_trigger_priority("host_presence_bootstrap"),
            age_ms=max(0, now_ms - retained.retained_at_ms),
            replaced_candidate_id=(
                existing.event.event_id
                if action == "replaced" and existing is not None
                else None
            ),
        )

    def _retry_active_host_bootstrap(
        self,
        session_id: str,
        *,
        runtime_generation: int,
        action: str,
        completed_job_id: str | None = None,
    ) -> None:
        """Try one bounded active-host marker without occupying the deferred slot."""

        cancellation_reason: str | None = None
        with self._lock:
            if not self._runtime_is_current_locked(runtime_generation):
                return
            record = self._active_bootstrap_retries.get(session_id)
            if record is None or session_id in self._active_bootstrap_retrying:
                return
            if (
                record.runtime_generation != runtime_generation
                or self.session_registry.get(session_id) is not record.registration
            ):
                del self._active_bootstrap_retries[session_id]
                cancellation_reason = "session_registration_changed"
            elif (
                session_id,
                record.target_host,
            ) in self._presence_bootstrapped_hosts:
                del self._active_bootstrap_retries[session_id]
                cancellation_reason = "host_presence_already_bootstrapped"
            else:
                self._active_bootstrap_retrying.add(session_id)
        context = _event_log_context(
            record.event,
            trigger_id="host_presence_bootstrap",
            target_host=record.target_host,
            enrichment_mode=EnrichmentMode.AMBIENT_PRESENCE,
        )
        age_ms = max(0, _monotonic_ms() - record.retained_at_ms)
        if cancellation_reason is not None:
            log_enrichment_event(
                "bootstrap.retry_cancelled",
                context=context,
                action=action,
                reason=cancellation_reason,
                completed_job_id=completed_job_id,
                accepted=False,
                age_ms=age_ms,
                priority=_trigger_priority("host_presence_bootstrap"),
            )
            return
        log_enrichment_event(
            "bootstrap.retry_attempted",
            level=logging.DEBUG,
            context=context,
            action=action,
            reason="eventual_admission",
            completed_job_id=completed_job_id,
            age_ms=age_ms,
            priority=_trigger_priority("host_presence_bootstrap"),
        )
        detail_overrides: dict[str, Any] = {
            "active_host_bootstrap": True,
            "bootstrap_retry": True,
            "bootstrap_retry_action": action,
        }
        if completed_job_id is not None:
            detail_overrides["bootstrap_retry_completed_job_id"] = completed_job_id
        try:
            result = self._consider(
                record.event,
                trigger_id="host_presence_bootstrap",
                target_host=record.target_host,
                surface="host",
                automatic=True,
                _allow_defer=False,
                _expected_registration=record.registration,
                _expected_runtime_generation=runtime_generation,
                _detail_overrides=detail_overrides,
                _skip_correlated_priority=True,
            )
            if not result.accepted:
                with self._lock:
                    retained = self._active_bootstrap_retries.get(session_id) is record
                if retained:
                    log_enrichment_event(
                        "bootstrap.retry_retained",
                        context=context,
                        action=action,
                        reason=result.reason,
                        completed_job_id=completed_job_id,
                        accepted=False,
                        age_ms=max(0, _monotonic_ms() - record.retained_at_ms),
                        priority=_trigger_priority("host_presence_bootstrap"),
                    )
        finally:
            with self._lock:
                self._active_bootstrap_retrying.discard(session_id)

    def _consider_subject_first_touch(
        self,
        event: SessionEvent,
        *,
        runtime_generation: int,
    ) -> bool:
        trigger_id = "subject_first_touch"
        if trigger_id not in self.settings.triggers or not _successful_command(event):
            return False
        if event.target_host is None:
            return False
        subject_key = interaction_focus_subject_key(
            event.payload.get("interaction_focus")
        )
        subject_digest = _focus_key_digest(subject_key)
        if subject_digest is None:
            return False
        seen_key = (event.session_id, event.target_host, subject_digest)
        with self._lock:
            if not self._runtime_is_current_locked(runtime_generation):
                return False
            if seen_key in self._seen_focus_subjects:
                return False
            self._seen_focus_subjects.add(seen_key)
        self._consider(
            event,
            trigger_id=trigger_id,
            target_host=event.target_host,
            surface=event.surface,
            automatic=True,
            _expected_runtime_generation=runtime_generation,
            _detail_overrides={"subject_key_sha256": subject_digest},
        )
        return True

    def _consider_presence_probe(
        self,
        event: SessionEvent,
        *,
        runtime_generation: int,
    ) -> bool:
        trigger_id = "presence_probe"
        if trigger_id not in self.settings.triggers or not _successful_command(event):
            return False
        value = event.payload.get("presence_probe")
        try:
            probe_kind = PresenceProbeKind(str(value))
        except ValueError:
            return False
        if event.target_host is None:
            return False
        self._consider(
            event,
            trigger_id=trigger_id,
            target_host=event.target_host,
            surface=event.surface,
            automatic=True,
            _expected_runtime_generation=runtime_generation,
            _detail_overrides={"presence_probe_kind": probe_kind.value},
        )
        return True

    def _record_interaction(
        self,
        event: SessionEvent,
        runtime_generation: int,
    ) -> int:
        if not _successful_command(event) or event.target_host is None:
            return 0
        key = (event.session_id, event.target_host)
        with self._lock:
            if not self._runtime_is_current_locked(runtime_generation):
                return 0
            count = self._interaction_counts.get(key, 0) + 1
            self._interaction_counts[key] = count
            return count

    def _consider_interaction_pulse(
        self,
        event: SessionEvent,
        *,
        pulse_count: int,
        runtime_generation: int,
    ) -> bool:
        trigger_id = "interaction_pulse"
        if trigger_id not in self.settings.triggers:
            return False
        if pulse_count < _INTERACTION_PULSE_THRESHOLD or event.target_host is None:
            return False
        result = self._consider(
            event,
            trigger_id=trigger_id,
            target_host=event.target_host,
            surface=event.surface,
            automatic=True,
            _expected_runtime_generation=runtime_generation,
            _detail_overrides={"pulse_count": pulse_count},
        )
        return result.accepted

    def _consider_surface_focus(
        self,
        event: SessionEvent,
        *,
        runtime_generation: int,
    ) -> None:
        if "surface_focus" not in self.settings.triggers:
            self._log_trigger_not_configured(event, "surface_focus")
            return
        target = event.target_host
        surface = event.surface
        if target is None or surface is None:
            return
        with self._lock:
            if not self._runtime_is_current_locked(runtime_generation):
                return
            previous = self._focus.get(event.session_id)
            count = (
                previous[2] + 1
                if previous is not None and previous[:2] == (target, surface)
                else 1
            )
            self._focus[event.session_id] = (target, surface, count)
        if count < _FOCUS_TRIGGER_THRESHOLD:
            log_enrichment_event(
                "trigger.suppressed",
                level=logging.DEBUG,
                context=_event_log_context(event, trigger_id="surface_focus"),
                reason="focus_threshold_not_reached",
                observed=count,
                threshold=_FOCUS_TRIGGER_THRESHOLD,
            )
            return
        with self._lock:
            if not self._runtime_is_current_locked(runtime_generation):
                return
            self._focus[event.session_id] = (target, surface, 0)
        self._consider(
            event,
            trigger_id="surface_focus",
            target_host=target,
            surface=surface,
            automatic=True,
            _expected_runtime_generation=runtime_generation,
        )

    def _consider_configured(
        self,
        event: SessionEvent,
        trigger_id: str,
        *,
        target_host: str | None = None,
        runtime_generation: int,
    ) -> None:
        if trigger_id not in self.settings.triggers:
            self._log_trigger_not_configured(event, trigger_id)
            return
        target = target_host or event.target_host
        if target is None:
            registration = self.session_registry.get(event.session_id)
            if registration is None:
                self._log_missing_session(event, trigger_id)
                return
            target = self._active_target(registration, stage="trigger_target")
        self._consider(
            event,
            trigger_id=trigger_id,
            target_host=target,
            surface=event.surface,
            automatic=True,
            _expected_runtime_generation=runtime_generation,
        )

    def _consider(
        self,
        event: SessionEvent,
        *,
        trigger_id: str,
        target_host: str,
        surface: str | None,
        automatic: bool,
        _candidate: TriggerCandidate | None = None,
        _allow_defer: bool = True,
        _expected_registration: RegisteredSession | None = None,
        _expected_runtime_generation: int | None = None,
        _detail_overrides: Mapping[str, Any] | None = None,
        _skip_correlated_priority: bool = False,
    ) -> EnrichmentSubmission:
        mode = _trigger_mode(trigger_id)
        with self._lock:
            if not self._started:
                log_enrichment_event(
                    "trigger.suppressed",
                    level=logging.DEBUG,
                    context=_event_log_context(
                        event,
                        trigger_id=trigger_id,
                        target_host=target_host,
                        enrichment_mode=mode,
                    ),
                    reason="runtime_not_started",
                )
                return EnrichmentSubmission(
                    False,
                    "suppressed",
                    "runtime_not_started",
                )
            runtime_generation = self._runtime_generation
            if (
                _expected_runtime_generation is not None
                and _expected_runtime_generation != runtime_generation
            ):
                log_enrichment_event(
                    "trigger.suppressed",
                    level=logging.DEBUG,
                    context=_event_log_context(
                        event,
                        trigger_id=trigger_id,
                        target_host=target_host,
                        enrichment_mode=mode,
                    ),
                    reason="runtime_restarted",
                )
                return EnrichmentSubmission(
                    False,
                    "suppressed",
                    "runtime_restarted",
                )
        registration = self.session_registry.get(event.session_id)
        if registration is None:
            self._log_missing_session(event, trigger_id)
            return EnrichmentSubmission(False, "suppressed", "session_missing")
        if (
            _expected_registration is not None
            and registration is not _expected_registration
        ):
            log_enrichment_event(
                "trigger.suppressed",
                context=_event_log_context(
                    event,
                    trigger_id=trigger_id,
                    target_host=target_host,
                    enrichment_mode=mode,
                ),
                reason="session_registration_changed",
            )
            return EnrichmentSubmission(
                False,
                "suppressed",
                "session_registration_changed",
            )
        now_ms = _monotonic_ms()
        candidate = _candidate
        if candidate is None:
            candidate_id = uuid4().hex
            anchors, omitted_anchor_count = _bounded_job_anchors(
                event.anchors,
                target_host=target_host,
            )
            if omitted_anchor_count:
                log_enrichment_event(
                    "trigger.anchors_bounded",
                    level=logging.DEBUG,
                    context=_event_log_context(
                        event,
                        trigger_id=trigger_id,
                        target_host=target_host,
                        enrichment_mode=mode,
                    ),
                    original_count=len(event.anchors),
                    retained_count=len(anchors),
                    omitted_count=omitted_anchor_count,
                )
            focus = event.payload.get("interaction_focus")
            focus_key = interaction_focus_key(focus)
            focus_digest = _focus_key_digest(focus_key)
            details: dict[str, Any] = {
                "automatic": automatic,
                "enrichment_mode": mode.value,
            }
            if _detail_overrides is not None:
                details.update(_detail_overrides)
            identity = _candidate_identity(details, focus_digest=focus_digest)
            deduplication_key = ":".join(
                (
                    event.session_id,
                    target_host,
                    mode.value,
                    surface or "any",
                    trigger_id,
                    identity,
                )
            )
            if isinstance(focus, Mapping):
                details["interaction_focus"] = focus
            if focus_key is not None:
                details["focus_key"] = focus_key
            candidate = TriggerCandidate(
                candidate_id=candidate_id,
                trigger_id=trigger_id,
                agent_id=ENRICHMENT_AGENT_ID,
                event=event,
                deduplication_key=deduplication_key,
                created_at_ms=now_ms,
                target_host=target_host,
                surface=surface,
                anchors=anchors,
                details=details,
            )
        else:
            deduplication_key = candidate.deduplication_key
            mode = _candidate_mode(candidate)

        higher_priority_trigger = (
            self._higher_priority_correlated_trigger(candidate)
            if automatic and not _skip_correlated_priority
            else None
        )
        if higher_priority_trigger is not None:
            log_enrichment_event(
                "trigger.suppressed",
                context=_event_log_context(
                    event,
                    trigger_id=trigger_id,
                    target_host=target_host,
                    enrichment_mode=mode,
                ),
                reason="higher_priority_correlated_candidate",
                higher_priority_trigger=higher_priority_trigger,
                candidate_id=candidate.candidate_id,
                priority=_trigger_priority(candidate.trigger_id),
                **_candidate_focus_log_fields(candidate),
            )
            return EnrichmentSubmission(
                False,
                "suppressed",
                "higher_priority_correlated_candidate",
            )

        if automatic:
            circuit = self._automatic_provider_circuit()
            if circuit is not None:
                self._log_provider_circuit_suppression(
                    candidate,
                    circuit,
                )
                return EnrichmentSubmission(
                    False,
                    "suppressed",
                    "provider_circuit_open",
                )

        scope_key = _candidate_cooldown_scope(candidate, mode=mode)
        cooldown_scope_sha256 = _focus_key_digest(scope_key)
        deferred_change: tuple[str, _DeferredRecord | None, _DeferredRecord] | None
        pulse_reset_count: int | None = None
        bootstrap_retry_admitted: _ActiveBootstrapRetry | None = None
        with self._lock:
            if self.session_registry.get(event.session_id) is not registration:
                return EnrichmentSubmission(
                    False, "suppressed", "session_registration_changed"
                )
            if len(self._scheduled_jobs) >= self._max_scheduled_jobs:
                log_enrichment_event(
                    "trigger.suppressed",
                    context=_candidate_log_context(candidate),
                    reason="worker_queue_full",
                    max_scheduled_jobs=self._max_scheduled_jobs,
                )
                return EnrichmentSubmission(False, "suppressed", "worker_queue_full")
            if not self._runtime_is_current_locked(runtime_generation):
                log_enrichment_event(
                    "trigger.suppressed",
                    context=_candidate_log_context(candidate),
                    reason=self._runtime_inactive_reason_locked(runtime_generation),
                )
                return EnrichmentSubmission(
                    False,
                    "suppressed",
                    self._runtime_inactive_reason_locked(runtime_generation),
                )
            accepted_times = self._accepted_by_session.setdefault(
                event.session_id,
                deque(),
            )
            while accepted_times and now_ms - accepted_times[0] >= _RATE_WINDOW_MS:
                accepted_times.popleft()
            policy = TriggerGatePolicy(
                agent_id=ENRICHMENT_AGENT_ID,
                cooldown_ms=(
                    self.settings.cooldown_ms
                    if automatic and trigger_id != "interaction_pulse"
                    else 0
                ),
                rate_limit_count=(
                    self.settings.max_jobs_per_minute if automatic else None
                ),
                rate_limit_window_ms=_RATE_WINDOW_MS if automatic else None,
                max_inflight=self.settings.max_inflight_per_session,
            )
            inflight_before = self._inflight_by_session.get(event.session_id, 0)
            mode_key = (event.session_id, mode)
            mode_inflight_before = self._inflight_by_session_mode.get(mode_key, 0)
            state = TriggerGateState(
                now_ms=now_ms,
                last_accepted_at_ms=self._last_accepted_by_scope.get(scope_key),
                rate_window_started_at_ms=(
                    accepted_times[0] if accepted_times else None
                ),
                accepted_in_window=len(accepted_times),
                inflight=inflight_before,
                pending_by_deduplication_key=self._pending_by_key,
            )
            decision = evaluate_trigger_gate(candidate, policy, state)
            if decision.code is TriggerDecisionCode.ACCEPTED:
                job_id = uuid4().hex
                record = _JobRecord(
                    job_id=job_id,
                    candidate_id=candidate.candidate_id,
                    event=event,
                    trigger_id=trigger_id,
                    mode=mode,
                    target_host=target_host,
                    surface=surface,
                    selected_surface=None,
                    status="queued",
                    reason=None,
                    queued_at=_utc_now(),
                    queued_monotonic=time.monotonic(),
                    automatic=automatic,
                    runtime_generation=runtime_generation,
                    runtime_provenance=registration.session.runtime_provenance,
                )
                self._jobs[job_id] = record
                scheduled = _ScheduledJob(Future(), candidate, registration)
                self._scheduled_jobs[job_id] = scheduled
                self._job_order.append(job_id)
                self._trim_jobs()
                self._pending_by_key[deduplication_key] = candidate.candidate_id
                self._inflight_by_session[event.session_id] = inflight_before + 1
                self._inflight_by_session_mode[mode_key] = mode_inflight_before + 1
                self._last_accepted_by_scope[scope_key] = now_ms
                accepted_times.append(now_ms)
                host_key = (event.session_id, target_host)
                if trigger_id == "interaction_pulse":
                    pulse_reset_count = self._interaction_counts.get(host_key, 0)
                    self._interaction_counts[host_key] = 0
                elif trigger_id == "host_presence_bootstrap":
                    self._presence_bootstrapped_hosts.add(host_key)
                    pending_retry = self._active_bootstrap_retries.get(event.session_id)
                    if (
                        pending_retry is not None
                        and pending_retry.target_host == target_host
                    ):
                        bootstrap_retry_admitted = pending_retry
                        del self._active_bootstrap_retries[event.session_id]
                executor = self._executor
                deferred_change = None
            else:
                job_id = None
                executor = None
                if (
                    automatic
                    and _allow_defer
                    and decision.reason is TriggerDecisionReason.INFLIGHT_LIMIT_REACHED
                ):
                    deferred_change = self._defer_candidate_locked(
                        candidate,
                        registration,
                        now_ms=now_ms,
                    )
                else:
                    deferred_change = None
            inflight_after = self._inflight_by_session.get(event.session_id, 0)
            mode_inflight_after = self._inflight_by_session_mode.get(mode_key, 0)
        self._log_decision(
            event,
            decision,
            target_host,
            surface,
            job_id=job_id,
            candidate=candidate,
            cooldown_scope_sha256=cooldown_scope_sha256,
            inflight_before=inflight_before,
            inflight_after=inflight_after,
            mode_inflight_before=mode_inflight_before,
            mode_inflight_after=mode_inflight_after,
        )
        if pulse_reset_count is not None:
            log_enrichment_event(
                "interaction_pulse.counter_reset",
                context=_candidate_log_context(candidate, job_id=job_id),
                action="counter_reset",
                reason="interaction_pulse_accepted",
                reset_reason="interaction_pulse_accepted",
                pulse_count=pulse_reset_count,
            )
        if bootstrap_retry_admitted is not None:
            log_enrichment_event(
                "bootstrap.retry_admitted",
                context=_candidate_log_context(candidate, job_id=job_id),
                action=str(
                    candidate.details.get("bootstrap_retry_action")
                    or "candidate_accepted"
                ),
                reason="trigger_accepted",
                completed_job_id=candidate.details.get(
                    "bootstrap_retry_completed_job_id"
                ),
                accepted=True,
                age_ms=max(
                    0,
                    now_ms - bootstrap_retry_admitted.retained_at_ms,
                ),
                priority=_trigger_priority(trigger_id),
            )
        if deferred_change is not None:
            self._log_deferred_change(candidate, deferred_change)
        if decision.code is not TriggerDecisionCode.ACCEPTED:
            return EnrichmentSubmission(
                accepted=False,
                code=decision.code.value,
                reason=decision.reason.value,
            )
        if executor is None:
            self._finish_job(
                job_id,
                deduplication_key,
                status="failed",
                reason="runtime_not_started",
                failed=True,
            )
            with self._lock:
                self._scheduled_jobs.pop(job_id, None)
                scheduled.candidate = None
                scheduled.registration = None
            scheduled.future.cancel()
            return EnrichmentSubmission(False, "suppressed", "runtime_not_started")
        assert job_id is not None
        log_enrichment_event(
            "job.queued",
            context=_candidate_log_context(candidate, job_id=job_id),
            automatic=automatic,
            apply_mode=self.settings.apply_mode,
            runtime_generation=runtime_generation,
            inflight=inflight_after,
            mode_inflight=mode_inflight_after,
            max_inflight=self.settings.max_inflight_per_session,
            cooldown_scope_sha256=cooldown_scope_sha256,
        )
        scheduled.future.add_done_callback(
            lambda completed: self._job_future_done(
                completed,
                job_id=job_id,
                deduplication_key=deduplication_key,
            )
        )
        try:
            future = executor.submit(
                self._execute_scheduled_job,
                job_id,
                deduplication_key,
            )
        except Exception as exc:
            log_enrichment_event(
                "job.submit_failed",
                level=logging.WARNING,
                context=_candidate_log_context(candidate, job_id=job_id),
                reason="runtime_stopping",
                error_type=type(exc).__name__,
            )
            self._finish_job(
                job_id,
                deduplication_key,
                status="failed",
                reason="runtime_stopping",
                failed=True,
            )
            with self._lock:
                self._scheduled_jobs.pop(job_id, None)
                scheduled.candidate = None
                scheduled.registration = None
            scheduled.future.cancel()
            return EnrichmentSubmission(False, "suppressed", "runtime_stopping")
        future.add_done_callback(
            lambda completed: self._scheduled_job_done(job_id, completed)
        )
        return EnrichmentSubmission(True, "accepted", "eligible", job_id)

    def _execute_scheduled_job(self, job_id: str, deduplication_key: str) -> None:
        # Executor work items capture only scalar IDs. A queued visit may close
        # while every provider worker is blocked; clearing this payload releases
        # its session immediately without relying on the executor to drain.
        with self._lock:
            scheduled = self._scheduled_jobs[job_id]
            if not scheduled.future.set_running_or_notify_cancel():
                return
            candidate = scheduled.candidate
            registration = scheduled.registration
            scheduled.candidate = None
            scheduled.registration = None
        try:
            assert candidate is not None and registration is not None
            self._run_job(job_id, candidate, deduplication_key, registration)
        except BaseException as exc:
            scheduled.future.set_exception(exc)
        else:
            scheduled.future.set_result(None)

    def _scheduled_job_done(self, job_id: str, future: Future[None]) -> None:
        with self._lock:
            scheduled = self._scheduled_jobs.pop(job_id, None)
            if scheduled is None:
                return
            scheduled.candidate = None
            scheduled.registration = None
        if future.cancelled():
            scheduled.future.cancel()
        elif not scheduled.future.done():
            error = future.exception()
            if error is not None:
                scheduled.future.set_exception(error)

    def _run_job(
        self,
        job_id: str,
        candidate: TriggerCandidate,
        deduplication_key: str,
        registration: RegisteredSession,
    ) -> None:
        record = self._job_record(job_id)
        if record is None:
            return
        if not self._runtime_allows_generation(record.runtime_generation):
            reason = self._runtime_inactive_reason(record.runtime_generation)
            self._finish_job(
                job_id,
                deduplication_key,
                status="skipped",
                reason=reason,
                failed=False,
            )
            return
        if record.automatic:
            circuit = self._automatic_provider_circuit()
            if circuit is not None:
                self._log_provider_circuit_suppression(
                    candidate,
                    circuit,
                    job_id=job_id,
                    stage="job_preflight",
                )
                self._finish_job(
                    job_id,
                    deduplication_key,
                    status="skipped",
                    reason="provider_circuit_open",
                    failed=False,
                )
                return
        with self._lock:
            record.status = "running"
            record.started_at = _utc_now()
            record.started_monotonic = time.monotonic()
        log_context = _candidate_log_context(candidate, job_id=job_id)
        log_enrichment_event(
            "job.started",
            context=log_context,
            runtime_generation=record.runtime_generation,
            queue_wait_ms=round(
                (record.started_monotonic - record.queued_monotonic) * 1000
            ),
        )
        current_registration = self.session_registry.get(candidate.session_id)
        if current_registration is not registration:
            reason = (
                "session_missing"
                if current_registration is None
                else "session_registration_changed"
            )
            self._finish_job(
                job_id,
                deduplication_key,
                status="skipped",
                reason=reason,
                failed=False,
            )
            return
        trace_run_manager: Any | None = None
        trace_graph_status: str | None = None
        trace_graph_reason: str | None = None
        trace_application_changed: bool | None = None
        try:
            if candidate.target_host is None:
                raise RuntimeError("target_host_missing")
            initial, session_metadata = self._capture_snapshot(
                registration,
                candidate.target_host,
                stage="initial",
                log_context=log_context,
            )
            with self._lock:
                record.target_host = initial.target_host
            graph_context = EnrichmentJobContext(
                job_id=job_id,
                session_id=candidate.session_id,
                target_host=initial.target_host,
                trigger_id=candidate.trigger_id,
                mode=record.mode,
                event_id=candidate.event_id,
                correlation_id=candidate.correlation_id,
                caused_by_event_id=candidate.event.caused_by_event_id,
                command_id=candidate.event.command_id,
                surface_hint=candidate.surface,
                anchors=candidate.anchors,
                interaction_focus=_candidate_interaction_focus(candidate),
                langfuse_session_id=candidate.session_id,
                username=session_metadata[0],
                profile_name=session_metadata[1],
                runtime_provenance=record.runtime_provenance,
            )
            trace_run_manager = self._start_job_trace(graph_context)

            def capture_current(
                _context: EnrichmentJobContext,
            ) -> EnrichmentContextProjection:
                current_registration = self.session_registry.get(candidate.session_id)
                if current_registration is None:
                    raise RuntimeError("session_missing")
                if current_registration is not registration:
                    raise RuntimeError("session_registration_changed")
                current, _metadata = self._capture_snapshot(
                    current_registration,
                    initial.target_host,
                    stage="current",
                    log_context=log_context,
                )
                world_changed = initial.full_world_sha256 != current.full_world_sha256
                target_changed = (
                    initial.target_host_sha256 != current.target_host_sha256
                )
                foreground_authority_changed = (
                    initial.foreground_authority_sha256
                    != current.foreground_authority_sha256
                )
                drifted = (
                    world_changed or target_changed or foreground_authority_changed
                )
                log_enrichment_event(
                    "snapshot.drifted" if drifted else "snapshot.compared",
                    level=logging.INFO if drifted else logging.DEBUG,
                    context=log_context,
                    drifted=drifted,
                    world_changed=world_changed,
                    target_changed=target_changed,
                    foreground_authority_changed=foreground_authority_changed,
                    initial_world_sha256=initial.full_world_sha256,
                    current_world_sha256=current.full_world_sha256,
                    initial_target_sha256=initial.target_host_sha256,
                    current_target_sha256=current.target_host_sha256,
                )
                return current

            if trace_run_manager is not None:
                graph_result = self.agent.invoke(
                    graph_context,
                    initial_snapshot=initial,
                    capture_current_snapshot=capture_current,
                    callbacks=trace_run_manager.get_child(),
                )
            else:
                graph_result = self.agent.invoke(
                    graph_context,
                    initial_snapshot=initial,
                    capture_current_snapshot=capture_current,
                )
            if not self._runtime_allows_generation(record.runtime_generation):
                reason = self._runtime_inactive_reason(record.runtime_generation)
                self._finish_job(
                    job_id,
                    deduplication_key,
                    status="skipped",
                    reason=reason,
                    failed=False,
                )
                return
            trace_graph_status = graph_result.status.value
            trace_graph_reason = graph_result.reason_code.value
            selected_surface = (
                graph_result.coordinator.surface
                if graph_result.coordinator is not None
                else None
            )
            with self._lock:
                record.selected_surface = selected_surface
            result_log_context = replace(
                log_context,
                selected_surface=selected_surface,
                surface=selected_surface or log_context.surface,
            )
            if (
                graph_result.model_failure is not None
                and graph_result.model_failure.classification.is_terminal
            ):
                self._open_provider_circuit_from_model_failure(
                    graph_result.model_failure,
                    context=result_log_context,
                    runtime_generation=record.runtime_generation,
                )
            if graph_result.status is not EnrichmentGraphStatus.PATCH_PROPOSED:
                failed = graph_result.status is EnrichmentGraphStatus.REJECTED
                self._finish_job(
                    job_id,
                    deduplication_key,
                    status="rejected" if failed else "no_op",
                    reason=graph_result.reason_code.value,
                    failed=failed,
                )
                return
            patch = graph_result.patch
            assert patch is not None
            patch_payload = patch.to_payload()
            cluster = patch.clusters[0]
            registry_targets = sorted(
                {
                    tuple(contribution.registry_path)
                    for contribution in cluster.contributions
                }
            )
            registry_target_counts = {
                "/".join(path): sum(
                    contribution.registry_path == path
                    for contribution in cluster.contributions
                )
                for path in registry_targets
            }
            log_enrichment_event(
                "patch.proposed",
                context=result_log_context,
                cluster_id=cluster.id,
                contribution_count=len(cluster.contributions),
                dependency_count=len(cluster.dependencies),
                authority_dependency_count=len(cluster.authority_dependencies),
                proposal_bytes=len(canonical_json(patch_payload).encode("utf-8")),
                registry_paths=[
                    list(contribution.registry_path)
                    for contribution in cluster.contributions
                ],
                registry_targets=[list(path) for path in registry_targets],
                registry_target_counts=registry_target_counts,
            )
            if self.settings.apply_mode == "shadow":
                log_enrichment_event(
                    "patch.skipped",
                    context=result_log_context,
                    reason="shadow_mode",
                    cluster_id=cluster.id,
                )
                self._finish_job(
                    job_id,
                    deduplication_key,
                    status="shadow",
                    reason="shadow_mode",
                    failed=False,
                )
                return
            trace_application_changed = self._apply_patch(
                registration,
                patch,
                job_id=job_id,
                candidate=candidate,
                deduplication_key=deduplication_key,
                log_context=result_log_context,
                runtime_generation=record.runtime_generation,
                refresh_request=_IntegratorRefreshRequest(
                    context=graph_context,
                    previous_result=graph_result,
                    initial_snapshot=initial,
                    capture_current_snapshot=capture_current,
                    trace_run_manager=trace_run_manager,
                ),
            )
        except EnrichmentTargetNotFoundError as exc:
            log_enrichment_event(
                "snapshot.rejected",
                level=logging.INFO,
                context=log_context,
                snapshot="initial",
                reason="target_missing",
                error_type=type(exc).__name__,
            )
            self._finish_job(
                job_id,
                deduplication_key,
                status="skipped",
                reason="target_missing",
                failed=False,
            )
        except Exception as exc:
            log_enrichment_event(
                "job.exception",
                level=logging.ERROR,
                context=log_context,
                reason="unhandled_runtime_error",
                error_type=type(exc).__name__,
            )
            self._finish_job(
                job_id,
                deduplication_key,
                status="failed",
                reason=f"unhandled_runtime_error:{type(exc).__name__}",
                failed=True,
            )
        finally:
            if trace_run_manager is not None:
                self._finish_job_trace(
                    trace_run_manager,
                    record=record,
                    graph_status=trace_graph_status,
                    graph_reason=trace_graph_reason,
                    application_changed=trace_application_changed,
                )

    def _job_future_done(
        self,
        future: Future[None],
        *,
        job_id: str,
        deduplication_key: str,
    ) -> None:
        record = self._job_record(job_id)
        session_id = record.event.session_id if record is not None else None
        if future.cancelled():
            self._finish_job(
                job_id,
                deduplication_key,
                status="skipped",
                reason=(record.cancellation_reason if record is not None else None)
                or "runtime_stopped",
                failed=False,
            )
        else:
            error = future.exception()
            if error is not None:
                log_enrichment_event(
                    "job.exception",
                    level=logging.ERROR,
                    context=(
                        EnrichmentLogContext(
                            session_id=record.event.session_id,
                            job_id=job_id,
                            event_id=record.event.event_id,
                            correlation_id=record.event.correlation_id,
                            trigger_id=record.trigger_id,
                            enrichment_mode=record.mode,
                            target_host=record.target_host,
                            requested_surface=record.surface,
                            selected_surface=record.selected_surface,
                            surface=record.selected_surface or record.surface,
                        )
                        if record is not None
                        else None
                    ),
                    reason="worker_future_failed",
                    error_type=type(error).__name__,
                )
                self._finish_job(
                    job_id,
                    deduplication_key,
                    status="failed",
                    reason=f"worker_future_failed:{type(error).__name__}",
                    failed=True,
                )
        if session_id is not None:
            try:
                if record is not None:
                    self._release_deferred(
                        session_id,
                        completed_job_id=job_id,
                        runtime_generation=record.runtime_generation,
                    )
            except Exception as exc:
                log_enrichment_event(
                    "deferred.release_failed",
                    level=logging.ERROR,
                    context=(
                        EnrichmentLogContext(
                            session_id=session_id,
                            job_id=job_id,
                            event_id=record.event.event_id,
                            correlation_id=record.event.correlation_id,
                            trigger_id=record.trigger_id,
                            enrichment_mode=record.mode,
                            target_host=record.target_host,
                        )
                        if record is not None
                        else EnrichmentLogContext(
                            session_id=session_id,
                            job_id=job_id,
                        )
                    ),
                    reason="release_error",
                    error_type=type(exc).__name__,
                )
            try:
                if record is not None:
                    self._retry_active_host_bootstrap(
                        session_id,
                        runtime_generation=record.runtime_generation,
                        action="job_completed",
                        completed_job_id=job_id,
                    )
            except Exception as exc:
                log_enrichment_event(
                    "bootstrap.retry_failed",
                    level=logging.ERROR,
                    context=(
                        EnrichmentLogContext(
                            session_id=session_id,
                            job_id=job_id,
                            event_id=record.event.event_id,
                            correlation_id=record.event.correlation_id,
                            trigger_id="host_presence_bootstrap",
                            enrichment_mode=EnrichmentMode.AMBIENT_PRESENCE,
                            target_host=record.target_host,
                        )
                        if record is not None
                        else EnrichmentLogContext(
                            session_id=session_id,
                            job_id=job_id,
                        )
                    ),
                    action="job_completed",
                    reason="retry_error",
                    error_type=type(exc).__name__,
                )

    def _start_job_trace(self, context: EnrichmentJobContext) -> Any | None:
        if not self.callbacks:
            return None
        try:
            from langchain_core.callbacks.manager import CallbackManager

            callback_manager = CallbackManager.configure(
                inheritable_callbacks=list(self.callbacks),
                inheritable_tags=enrichment_trace_tags(ENRICHMENT_GRAPH_VERSION),
                inheritable_metadata=enrichment_trace_metadata(
                    context,
                    graph_version=ENRICHMENT_GRAPH_VERSION,
                ),
            )
            run_manager = callback_manager.on_chain_start(
                {"name": "dreamland-enrichment-agent"},
                {
                    "job_id": context.job_id,
                    "trigger_id": context.trigger_id,
                    "enrichment_mode": context.mode.value,
                    "target_host": context.target_host,
                    "requested_surface": context.surface_hint,
                    "apply_mode": self.settings.apply_mode,
                },
            )
        except Exception as exc:
            log_enrichment_event(
                "trace.start_failed",
                level=logging.WARNING,
                context=_job_log_context(context),
                reason="callback_error",
                error_type=type(exc).__name__,
            )
            return None
        log_enrichment_event(
            "trace.started",
            level=logging.DEBUG,
            context=_job_log_context(context),
            graph_version=ENRICHMENT_GRAPH_VERSION,
        )
        return run_manager

    def _finish_job_trace(
        self,
        run_manager: Any,
        *,
        record: _JobRecord,
        graph_status: str | None,
        graph_reason: str | None,
        application_changed: bool | None,
    ) -> None:
        with self._lock:
            snapshot = record.snapshot()
            selected_surface = record.selected_surface
            event = record.event
        graph_proposed = graph_status == EnrichmentGraphStatus.PATCH_PROPOSED.value
        application_status = snapshot.status if graph_proposed else "not_run"
        application_reason = (
            snapshot.reason if graph_proposed else graph_reason or snapshot.reason
        )
        outputs = {
            "job_id": record.job_id,
            "enrichment_mode": record.mode.value,
            "graph_status": graph_status,
            "graph_reason": graph_reason,
            "application": {
                "mode": self.settings.apply_mode,
                "status": application_status,
                "reason": application_reason,
                "changed": bool(application_changed),
            },
            "selected_surface": selected_surface,
            "duration_ms": snapshot.duration_ms,
        }
        context = EnrichmentLogContext(
            session_id=snapshot.session_id,
            job_id=record.job_id,
            event_id=event.event_id,
            caused_by_event_id=event.caused_by_event_id,
            correlation_id=event.correlation_id,
            trigger_id=snapshot.trigger,
            enrichment_mode=record.mode,
            command_id=event.command_id,
            target_host=snapshot.target_host,
            requested_surface=snapshot.surface,
            selected_surface=selected_surface,
            surface=selected_surface or snapshot.surface,
        )
        try:
            run_manager.on_chain_end(outputs)
        except Exception as exc:
            log_enrichment_event(
                "trace.finish_failed",
                level=logging.WARNING,
                context=context,
                reason="callback_error",
                error_type=type(exc).__name__,
            )
            return
        log_enrichment_event(
            "trace.completed",
            level=logging.DEBUG,
            context=context,
            graph_status=graph_status,
            application_status=application_status,
            application_reason=application_reason,
            application_changed=bool(application_changed),
        )

    def _capture_snapshot(
        self,
        registration: RegisteredSession,
        target_host: str,
        *,
        stage: str,
        log_context: EnrichmentLogContext,
    ) -> tuple[EnrichmentContextProjection, tuple[str, str | None]]:
        with self._session_lock(
            registration, stage=f"snapshot_{stage}", context=log_context
        ):
            world_state = deepcopy(registration.session.world_state)
            resolved_target = resolve_enrichment_host_ref(world_state, target_host)
            foreground_authority = (
                capture_foreground_authority(
                    registration.session,
                    target_host=resolved_target,
                )
                if resolved_target is not None
                else None
            )
            foreground_authentication_paths(
                registration.session,
                target_host=resolved_target or target_host,
            )
            username = registration.session.username
            profile_name_value = registration.session.environment_metadata.metadata.get(
                "state_profile.name"
            )
        projection = project_world_state_for_enrichment(
            world_state,
            target_host=target_host,
            foreground_authority=foreground_authority,
        )
        log_enrichment_event(
            "snapshot.captured",
            context=log_context,
            snapshot=stage,
            target_host=projection.target_host,
            world_sha256=projection.full_world_sha256,
            target_sha256=projection.target_host_sha256,
            projected_sha256=projection.projected_sha256,
            full_world_chars=projection.full_world_chars,
            projected_chars=projection.projected_chars,
            command_evidence_count=projection.command_evidence_count,
            command_evidence_chars=projection.command_evidence_chars,
            command_evidence_truncated_count=(
                projection.command_evidence_truncated_count
            ),
            command_evidence_omitted_count=projection.command_evidence_omitted_count,
            foreground_authority_present=(foreground_authority is not None),
            detailed_hosts=list(projection.detailed_hosts),
        )
        return projection, (
            username,
            str(profile_name_value) if profile_name_value is not None else None,
        )

    def _apply_patch(
        self,
        registration: RegisteredSession,
        patch: object,
        *,
        job_id: str,
        candidate: TriggerCandidate,
        deduplication_key: str,
        log_context: EnrichmentLogContext,
        runtime_generation: int,
        refresh_request: _IntegratorRefreshRequest | None = None,
        integrator_refresh_attempt: int = 0,
        integrator_refresh_started_at: float | None = None,
        integrator_refresh_conflict_domains: tuple[str, ...] = (),
    ) -> bool:
        from dreamland.background_agents.enrichment.contracts import EnrichmentPatch

        if not isinstance(patch, EnrichmentPatch):
            raise TypeError("graph returned an invalid enrichment patch")
        if not self._runtime_allows_generation(runtime_generation):
            reason = self._runtime_inactive_reason(runtime_generation)
            log_enrichment_event(
                "patch.skipped",
                context=log_context,
                changed=False,
                status="skipped",
                reason=reason,
            )
            self._finish_job(
                job_id,
                deduplication_key,
                status="skipped",
                reason=reason,
                failed=False,
            )
            self._log_integrator_refresh_completion(
                log_context,
                attempt=integrator_refresh_attempt,
                started_at=integrator_refresh_started_at,
                conflict_domains=integrator_refresh_conflict_domains,
                outcome="application_skipped",
                reason=reason,
                changed=False,
            )
            return False
        if self.session_registry.get(candidate.session_id) is not registration:
            log_enrichment_event(
                "patch.skipped",
                context=log_context,
                changed=False,
                status="skipped",
                reason="session_registration_changed",
            )
            self._finish_job(
                job_id,
                deduplication_key,
                status="skipped",
                reason="session_registration_changed",
                failed=False,
            )
            self._log_integrator_refresh_completion(
                log_context,
                attempt=integrator_refresh_attempt,
                started_at=integrator_refresh_started_at,
                conflict_domains=integrator_refresh_conflict_domains,
                outcome="application_skipped",
                reason="session_registration_changed",
                changed=False,
            )
            return False
        with self._session_lock(registration, stage="patch_apply", context=log_context):
            if self.session_registry.get(candidate.session_id) is not registration:
                log_enrichment_event(
                    "patch.skipped",
                    context=log_context,
                    changed=False,
                    status="skipped",
                    reason="session_registration_changed",
                )
                self._finish_job(
                    job_id,
                    deduplication_key,
                    status="skipped",
                    reason="session_registration_changed",
                    failed=False,
                )
                self._log_integrator_refresh_completion(
                    log_context,
                    attempt=integrator_refresh_attempt,
                    started_at=integrator_refresh_started_at,
                    conflict_domains=integrator_refresh_conflict_domains,
                    outcome="application_skipped",
                    reason="session_registration_changed",
                    changed=False,
                )
                return False
            foreground_authority = capture_foreground_authority(
                registration.session,
                target_host=patch.target_host,
            )
            if integrator_refresh_attempt == 1 and foreground_authority is None:
                reason = "semantic_authority_conflict"
                log_enrichment_event(
                    "authority.semantic_conflict",
                    context=log_context,
                    policy_version="foreground-authority-v1",
                    authority_domain=SESSION_SEMANTICS_AUTHORITY_DOMAIN,
                    authority_dependency_count=sum(
                        len(cluster.authority_dependencies)
                        for cluster in patch.clusters
                    ),
                    action="cluster_skipped",
                    reason="target_not_foreground",
                )
                log_enrichment_event(
                    "patch.skipped",
                    context=log_context,
                    changed=False,
                    status="skipped",
                    reason=reason,
                )
                self._finish_job(
                    job_id,
                    deduplication_key,
                    status="skipped",
                    reason=reason,
                    failed=False,
                )
                self._log_integrator_refresh_completion(
                    log_context,
                    attempt=integrator_refresh_attempt,
                    started_at=integrator_refresh_started_at,
                    conflict_domains=integrator_refresh_conflict_domains,
                    outcome="refresh_skipped",
                    reason="target_not_foreground",
                    changed=False,
                )
                return False
            current_authority_sha256_by_domain = (
                {
                    SESSION_SEMANTICS_AUTHORITY_DOMAIN: (
                        foreground_authority.session_semantics_sha256
                    )
                }
                if foreground_authority is not None
                else {}
            )
            if any(
                dependency.domain == FOREGROUND_EVIDENCE_AUTHORITY_DOMAIN
                for cluster in patch.clusters
                for dependency in cluster.authority_dependencies
            ):
                hosts = registration.session.world_state.get("hosts_by_name")
                host = (
                    hosts.get(patch.target_host) if isinstance(hosts, Mapping) else None
                )
                if isinstance(host, Mapping):
                    evidence = capture_foreground_evidence(host)
                    current_authority_sha256_by_domain[
                        FOREGROUND_EVIDENCE_AUTHORITY_DOMAIN
                    ] = evidence.private_sha256
            result = apply_enrichment_patch(
                registration.session.world_state,
                patch,
                current_authority_sha256_by_domain=(current_authority_sha256_by_domain),
                protected_authentication_paths=foreground_authentication_paths(
                    registration.session, target_host=patch.target_host
                ),
            )
            changed = result.world_state != registration.session.world_state
            with self._lock:
                if not self._runtime_allows_generation_locked(runtime_generation):
                    reason = self._runtime_inactive_reason_locked(runtime_generation)
                    log_enrichment_event(
                        "patch.skipped",
                        context=log_context,
                        changed=False,
                        status="skipped",
                        reason=reason,
                    )
                    self._finish_job(
                        job_id,
                        deduplication_key,
                        status="skipped",
                        reason=reason,
                        failed=False,
                    )
                    self._log_integrator_refresh_completion(
                        log_context,
                        attempt=integrator_refresh_attempt,
                        started_at=integrator_refresh_started_at,
                        conflict_domains=integrator_refresh_conflict_domains,
                        outcome="application_skipped",
                        reason=reason,
                        changed=False,
                    )
                    return False
                if changed:
                    with self.session_registry.if_live(registration) as live:
                        if not live:
                            self._finish_job(
                                job_id,
                                deduplication_key,
                                status="skipped",
                                reason="session_registration_changed",
                                failed=False,
                            )
                            return False
                        registration.session.world_state = result.world_state
                        registration.session.force_session_state_next_command = True
        outcome = result.outcome
        mismatched_domains: list[str] = []
        if outcome.reason_code == "semantic_authority_conflict":
            dependencies = {
                dependency.domain: dependency.expected_sha256
                for cluster in patch.clusters
                for dependency in cluster.authority_dependencies
            }
            mismatched_domains = [
                domain
                for domain, expected in dependencies.items()
                if current_authority_sha256_by_domain.get(domain) != expected
            ]
            for domain in mismatched_domains:
                log_enrichment_event(
                    "authority.semantic_conflict",
                    context=log_context,
                    policy_version=(
                        FOREGROUND_EVIDENCE_POLICY_VERSION
                        if domain == FOREGROUND_EVIDENCE_AUTHORITY_DOMAIN
                        else "foreground-authority-v1"
                    ),
                    authority_domain=domain,
                    authority_dependency_count=len(dependencies),
                    action="cluster_skipped",
                )
        outcome_fields = outcome.to_log_fields()
        log_enrichment_event(
            "patch.applied" if changed else "patch.skipped",
            context=log_context,
            changed=changed,
            status=outcome.status,
            reason=outcome.reason_code,
            cluster_count=outcome_fields["cluster_count"],
            clusters=outcome_fields["clusters"],
        )
        if (
            outcome.reason_code == "semantic_authority_conflict"
            and integrator_refresh_attempt == 0
        ):
            mismatch_set = set(mismatched_domains)
            refresh_method = getattr(self.agent, "refresh_integrator", None)
            retry_reason = "foreground_evidence_conflict"
            retry_eligible = True
            if mismatch_set != {FOREGROUND_EVIDENCE_AUTHORITY_DOMAIN}:
                retry_eligible = False
                retry_reason = (
                    "active_session_conflict"
                    if SESSION_SEMANTICS_AUTHORITY_DOMAIN in mismatch_set
                    else "unsupported_authority_conflict"
                )
            elif foreground_authority is None:
                retry_eligible = False
                retry_reason = "target_not_foreground"
            elif (
                refresh_request is None
                or refresh_request.previous_result.integrator_refresh_basis is None
                or not callable(refresh_method)
            ):
                retry_eligible = False
                retry_reason = "refresh_basis_unavailable"
            log_enrichment_event(
                "integrator.refresh.decision",
                context=log_context,
                integrator_refresh_attempt=1,
                eligible=retry_eligible,
                reason=retry_reason,
                action="retry" if retry_eligible else "skip",
                conflict_domains=sorted(mismatch_set),
                **_single_authority_domain_log_fields(mismatch_set),
            )
            if retry_eligible:
                assert refresh_request is not None
                return self._refresh_integrator_after_conflict(
                    registration,
                    job_id=job_id,
                    candidate=candidate,
                    deduplication_key=deduplication_key,
                    log_context=log_context,
                    runtime_generation=runtime_generation,
                    request=refresh_request,
                    conflict_domains=tuple(sorted(mismatch_set)),
                )
        self._finish_job(
            job_id,
            deduplication_key,
            status=outcome.status,
            reason=outcome.reason_code,
            failed=False,
        )
        self._log_integrator_refresh_completion(
            log_context,
            attempt=integrator_refresh_attempt,
            started_at=integrator_refresh_started_at,
            conflict_domains=integrator_refresh_conflict_domains,
            outcome=("applied" if changed else outcome.status),
            reason=outcome.reason_code,
            changed=changed,
        )
        if changed:
            self.event_bus.publish(
                new_session_event(
                    session_id=candidate.session_id,
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.BACKGROUND_AGENT,
                    source_agent_id=ENRICHMENT_AGENT_ID,
                    correlation_id=candidate.correlation_id,
                    caused_by_event_id=candidate.event_id,
                    command_id=candidate.event.command_id,
                    target_host=patch.target_host,
                    surface=log_context.selected_surface or candidate.surface,
                    payload={
                        "kind": "enrichment_applied",
                        "job_id": job_id,
                        "apply_status": outcome.status,
                    },
                )
            )
        return changed

    def _refresh_integrator_after_conflict(
        self,
        registration: RegisteredSession,
        *,
        job_id: str,
        candidate: TriggerCandidate,
        deduplication_key: str,
        log_context: EnrichmentLogContext,
        runtime_generation: int,
        request: _IntegratorRefreshRequest,
        conflict_domains: tuple[str, ...],
    ) -> bool:
        """Run one Integrator-only refresh without holding the session lock."""

        attempt = 1
        started_at = time.perf_counter()
        log_enrichment_event(
            "integrator.refresh.started",
            context=log_context,
            integrator_refresh_attempt=attempt,
            conflict_domains=conflict_domains,
            action="started",
            reused_coordinator=True,
            reused_expert=True,
            **_single_authority_domain_log_fields(conflict_domains),
        )
        if not self._runtime_allows_generation(runtime_generation):
            reason = self._runtime_inactive_reason(runtime_generation)
            self._finish_job(
                job_id,
                deduplication_key,
                status="skipped",
                reason=reason,
                failed=False,
            )
            self._log_integrator_refresh_completion(
                log_context,
                attempt=attempt,
                started_at=started_at,
                conflict_domains=conflict_domains,
                outcome="refresh_skipped",
                reason=reason,
                changed=False,
            )
            return False
        if self.session_registry.get(candidate.session_id) is not registration:
            reason = "session_registration_changed"
            self._finish_job(
                job_id,
                deduplication_key,
                status="skipped",
                reason=reason,
                failed=False,
            )
            self._log_integrator_refresh_completion(
                log_context,
                attempt=attempt,
                started_at=started_at,
                conflict_domains=conflict_domains,
                outcome="refresh_skipped",
                reason=reason,
                changed=False,
            )
            return False

        refresh_method = getattr(self.agent, "refresh_integrator", None)
        if not callable(refresh_method):
            raise RuntimeError("Integrator refresh method became unavailable")
        try:
            refresh_callbacks = (
                request.trace_run_manager.get_child()
                if request.trace_run_manager is not None
                else ()
            )

            def capture_refresh_snapshot(
                context: EnrichmentJobContext,
            ) -> EnrichmentContextProjection:
                snapshot = request.capture_current_snapshot(context)
                if snapshot.foreground_authority_sha256 is None:
                    raise _IntegratorRefreshTargetNotForeground(
                        "target is no longer the foreground host"
                    )
                return snapshot

            refreshed = refresh_method(
                request.context,
                previous_result=request.previous_result,
                initial_snapshot=request.initial_snapshot,
                capture_current_snapshot=capture_refresh_snapshot,
                callbacks=refresh_callbacks,
                attempt=attempt,
            )
        except _IntegratorRefreshTargetNotForeground:
            log_enrichment_event(
                "authority.semantic_conflict",
                context=log_context,
                policy_version="foreground-authority-v1",
                authority_domain=SESSION_SEMANTICS_AUTHORITY_DOMAIN,
                action="refresh_skipped",
                reason="target_not_foreground",
            )
            self._finish_job(
                job_id,
                deduplication_key,
                status="skipped",
                reason="semantic_authority_conflict",
                failed=False,
            )
            self._log_integrator_refresh_completion(
                log_context,
                attempt=attempt,
                started_at=started_at,
                conflict_domains=conflict_domains,
                outcome="refresh_skipped",
                reason="target_not_foreground",
                changed=False,
            )
            return False
        except Exception as exc:
            reason = f"integrator_refresh_failed:{type(exc).__name__}"
            log_enrichment_event(
                "integrator.refresh.failed",
                level=logging.ERROR,
                context=log_context,
                integrator_refresh_attempt=attempt,
                integrator_refresh_outcome="failed",
                conflict_domains=conflict_domains,
                action="failed",
                reason="runtime_error",
                changed=False,
                error_type=type(exc).__name__,
                integrator_refresh_latency_ms=round(
                    (time.perf_counter() - started_at) * 1000
                ),
                **_single_authority_domain_log_fields(conflict_domains),
            )
            self._finish_job(
                job_id,
                deduplication_key,
                status="failed",
                reason=reason,
                failed=True,
            )
            return False

        if not self._runtime_allows_generation(runtime_generation):
            reason = self._runtime_inactive_reason(runtime_generation)
            self._finish_job(
                job_id,
                deduplication_key,
                status="skipped",
                reason=reason,
                failed=False,
            )
            self._log_integrator_refresh_completion(
                log_context,
                attempt=attempt,
                started_at=started_at,
                conflict_domains=conflict_domains,
                outcome="refresh_discarded",
                reason=reason,
                changed=False,
            )
            return False
        if refreshed.model_failure is not None and (
            refreshed.model_failure.classification.is_terminal
        ):
            self._open_provider_circuit_from_model_failure(
                refreshed.model_failure,
                context=log_context,
                runtime_generation=runtime_generation,
            )
        if self.session_registry.get(candidate.session_id) is not registration:
            reason = "session_registration_changed"
            self._finish_job(
                job_id,
                deduplication_key,
                status="skipped",
                reason=reason,
                failed=False,
            )
            self._log_integrator_refresh_completion(
                log_context,
                attempt=attempt,
                started_at=started_at,
                conflict_domains=conflict_domains,
                outcome="refresh_discarded",
                reason=reason,
                changed=False,
            )
            return False
        if refreshed.status is not EnrichmentGraphStatus.PATCH_PROPOSED:
            target_pivoted = (
                refreshed.reason_code.value == "current_snapshot_capture_failed"
                and refreshed.rejection_detail
                == _IntegratorRefreshTargetNotForeground.__name__
            )
            failed = refreshed.status is EnrichmentGraphStatus.REJECTED
            if target_pivoted:
                log_enrichment_event(
                    "authority.semantic_conflict",
                    context=log_context,
                    policy_version="foreground-authority-v1",
                    authority_domain=SESSION_SEMANTICS_AUTHORITY_DOMAIN,
                    action="refresh_skipped",
                    reason="target_not_foreground",
                )
            self._finish_job(
                job_id,
                deduplication_key,
                status=(
                    "skipped" if target_pivoted else ("rejected" if failed else "no_op")
                ),
                reason=(
                    "semantic_authority_conflict"
                    if target_pivoted
                    else refreshed.reason_code.value
                ),
                failed=failed and not target_pivoted,
            )
            self._log_integrator_refresh_completion(
                log_context,
                attempt=attempt,
                started_at=started_at,
                conflict_domains=conflict_domains,
                outcome=(
                    "refresh_skipped" if target_pivoted else refreshed.status.value
                ),
                reason=(
                    "target_not_foreground"
                    if target_pivoted
                    else refreshed.reason_code.value
                ),
                changed=False,
            )
            return False

        refreshed_patch = refreshed.patch
        assert refreshed_patch is not None
        refreshed_cluster = refreshed_patch.clusters[0]
        log_enrichment_event(
            "integrator.refresh.patch_proposed",
            context=log_context,
            integrator_refresh_attempt=attempt,
            conflict_domains=conflict_domains,
            action="patch_proposed",
            cluster_id=refreshed_cluster.id,
            contribution_count=len(refreshed_cluster.contributions),
            dependency_count=len(refreshed_cluster.dependencies),
            authority_dependency_count=len(refreshed_cluster.authority_dependencies),
            **_single_authority_domain_log_fields(conflict_domains),
        )
        return self._apply_patch(
            registration,
            refreshed_patch,
            job_id=job_id,
            candidate=candidate,
            deduplication_key=deduplication_key,
            log_context=log_context,
            runtime_generation=runtime_generation,
            integrator_refresh_attempt=attempt,
            integrator_refresh_started_at=started_at,
            integrator_refresh_conflict_domains=conflict_domains,
        )

    @staticmethod
    def _log_integrator_refresh_completion(
        context: EnrichmentLogContext,
        *,
        attempt: int,
        started_at: float | None,
        conflict_domains: tuple[str, ...],
        outcome: str,
        reason: str,
        changed: bool,
    ) -> None:
        if attempt <= 0 or started_at is None:
            return
        log_enrichment_event(
            "integrator.refresh.completed",
            context=context,
            integrator_refresh_attempt=attempt,
            conflict_domains=conflict_domains,
            integrator_refresh_outcome=outcome,
            reason=reason,
            action="completed",
            changed=changed,
            integrator_refresh_latency_ms=round(
                (time.perf_counter() - started_at) * 1000
            ),
            **_single_authority_domain_log_fields(conflict_domains),
        )

    @contextmanager
    def _session_lock(
        self,
        registration: RegisteredSession,
        *,
        stage: str,
        context: EnrichmentLogContext,
    ) -> Iterator[None]:
        waiting_at = time.perf_counter()
        log_enrichment_event(
            "lock.waiting",
            level=logging.DEBUG,
            context=context,
            stage=stage,
        )
        registration.lock.acquire()
        acquired_at = time.perf_counter()
        wait_ms = round((acquired_at - waiting_at) * 1000)
        log_enrichment_event(
            "lock.acquired",
            level=(logging.WARNING if wait_ms >= _LONG_LOCK_WAIT_MS else logging.DEBUG),
            context=context,
            stage=stage,
            wait_ms=wait_ms,
            long_wait=wait_ms >= _LONG_LOCK_WAIT_MS,
        )
        try:
            yield
        finally:
            held_ms = round((time.perf_counter() - acquired_at) * 1000)
            registration.lock.release()
            log_enrichment_event(
                "lock.released",
                level=logging.DEBUG,
                context=context,
                stage=stage,
                wait_ms=wait_ms,
                held_ms=held_ms,
            )

    def _active_target(self, registration: RegisteredSession, *, stage: str) -> str:
        context = EnrichmentLogContext(session_id=registration.session_id)
        with self._session_lock(registration, stage=stage, context=context):
            return active_host_ref(registration.session.world_state)

    def _schedule_idle(
        self,
        event: SessionEvent,
        *,
        runtime_generation: int,
    ) -> None:
        if not self._runtime_is_current(runtime_generation):
            return
        if "session_idle" not in self.settings.triggers:
            self._log_trigger_not_configured(event, "session_idle")
            return
        circuit = self._automatic_provider_circuit()
        if circuit is not None:
            log_enrichment_event(
                "trigger.suppressed",
                context=_event_log_context(event, trigger_id="session_idle"),
                reason="provider_circuit_open",
                stage="idle_schedule",
                **circuit.log_fields(),
            )
            return
        self._cancel_idle(
            event.session_id,
            reason="rescheduled",
            runtime_generation=runtime_generation,
        )
        token = uuid4().hex
        idle_payload: dict[str, Any] = {
            "idle_delay_ms": self.settings.idle_delay_ms,
        }
        interaction_focus = event.payload.get("interaction_focus")
        if isinstance(interaction_focus, Mapping):
            idle_payload["interaction_focus"] = interaction_focus

        def publish_idle() -> None:
            with self._lock:
                if (
                    not self._runtime_is_current_locked(runtime_generation)
                    or self._idle_tokens.get(event.session_id) != token
                ):
                    return
                self._idle_tokens.pop(event.session_id, None)
                self._idle_timers.pop(event.session_id, None)
            self.event_bus.publish(
                new_session_event(
                    session_id=event.session_id,
                    event_type=SessionEventType.SESSION_IDLE,
                    source=EventSource.SESSION_RUNTIME,
                    correlation_id=event.correlation_id,
                    caused_by_event_id=event.event_id,
                    command_id=event.command_id,
                    target_host=event.target_host,
                    surface=event.surface,
                    anchors=event.anchors,
                    payload=idle_payload,
                )
            )

        timer = threading.Timer(self.settings.idle_delay_ms / 1000, publish_idle)
        timer.daemon = True
        with self._lock:
            if not self._runtime_is_current_locked(runtime_generation):
                return
            self._idle_tokens[event.session_id] = token
            self._idle_timers[event.session_id] = timer
        timer.start()
        log_enrichment_event(
            "idle.scheduled",
            level=logging.DEBUG,
            context=_event_log_context(event, trigger_id="session_idle"),
            delay_ms=self.settings.idle_delay_ms,
        )

    def _cancel_idle(
        self,
        session_id: str,
        *,
        reason: str,
        runtime_generation: int | None = None,
    ) -> None:
        with self._lock:
            if runtime_generation is not None and not self._runtime_is_current_locked(
                runtime_generation
            ):
                return
            timer = self._idle_timers.pop(session_id, None)
            self._idle_tokens.pop(session_id, None)
        if timer is not None:
            timer.cancel()
            log_enrichment_event(
                "idle.cancelled",
                level=logging.DEBUG,
                context=EnrichmentLogContext(session_id=session_id),
                reason=reason,
            )

    def _cancel_deferred_idle(
        self,
        session_id: str,
        *,
        reason: str,
        runtime_generation: int | None = None,
        correlation_id: str | None = None,
    ) -> None:
        with self._lock:
            if runtime_generation is not None and not self._runtime_is_current_locked(
                runtime_generation
            ):
                return
            record = self._deferred_by_session.get(session_id)
            if record is None or record.candidate.trigger_id != "session_idle":
                return
            if (
                correlation_id is not None
                and record.candidate.correlation_id != correlation_id
            ):
                return
            del self._deferred_by_session[session_id]
        self._log_deferred_cancelled(record, reason=reason)

    def _defer_candidate_locked(
        self,
        candidate: TriggerCandidate,
        registration: RegisteredSession,
        *,
        now_ms: int,
    ) -> tuple[str, _DeferredRecord | None, _DeferredRecord]:
        new_record = _DeferredRecord(
            candidate=candidate,
            registration=registration,
            runtime_generation=self._runtime_generation,
            deferred_at_ms=now_ms,
            expires_at_ms=now_ms + _DEFERRED_MAX_AGE_MS,
        )
        existing = self._deferred_by_session.get(candidate.session_id)
        if existing is None:
            self._deferred_by_session[candidate.session_id] = new_record
            return "stored", None, new_record
        candidate_priority = _trigger_priority(candidate.trigger_id)
        existing_priority = _trigger_priority(existing.candidate.trigger_id)
        replace_existing = candidate_priority >= existing_priority
        if (
            candidate_priority == existing_priority
            and candidate.trigger_id == "host_presence_bootstrap"
            and existing.candidate.trigger_id == "host_presence_bootstrap"
        ):
            candidate_is_active = _active_host_bootstrap_candidate(candidate)
            existing_is_active = _active_host_bootstrap_candidate(existing.candidate)
            if existing_is_active and not candidate_is_active:
                replace_existing = False
            elif candidate_is_active and not existing_is_active:
                replace_existing = True
        if replace_existing:
            self._deferred_by_session[candidate.session_id] = new_record
            return "replaced", existing, new_record
        return "retained", existing, new_record

    def _log_deferred_change(
        self,
        candidate: TriggerCandidate,
        change: tuple[str, _DeferredRecord | None, _DeferredRecord],
    ) -> None:
        action, existing, new_record = change
        retained = existing if action == "retained" else new_record
        if retained is None:
            retained = new_record
        active_host_preferred = (
            existing is not None
            and candidate.trigger_id == "host_presence_bootstrap"
            and existing.candidate.trigger_id == "host_presence_bootstrap"
            and _active_host_bootstrap_candidate(candidate)
            != _active_host_bootstrap_candidate(existing.candidate)
            and _active_host_bootstrap_candidate(retained.candidate)
        )
        log_enrichment_event(
            f"deferred.{action}",
            context=_candidate_log_context(candidate),
            reason="active_host_preferred" if active_host_preferred else None,
            candidate_id=candidate.candidate_id,
            priority=_trigger_priority(candidate.trigger_id),
            max_age_ms=_DEFERRED_MAX_AGE_MS,
            replaced_candidate_id=(
                existing.candidate.candidate_id
                if action == "replaced" and existing is not None
                else None
            ),
            retained_candidate_id=retained.candidate.candidate_id,
            retained_trigger=retained.candidate.trigger_id,
            retained_priority=_trigger_priority(retained.candidate.trigger_id),
            **_candidate_focus_log_fields(candidate),
            **_candidate_trigger_log_fields(candidate),
            **_candidate_focus_log_fields(
                retained.candidate,
                prefix="retained_",
            ),
            **_candidate_trigger_log_fields(
                retained.candidate,
                prefix="retained_",
            ),
        )

    def _release_deferred(
        self,
        session_id: str,
        *,
        completed_job_id: str,
        runtime_generation: int,
    ) -> None:
        with self._lock:
            record = self._deferred_by_session.get(session_id)
            if (
                record is None
                or record.runtime_generation != runtime_generation
                or not self._runtime_is_current_locked(runtime_generation)
            ):
                return
            del self._deferred_by_session[session_id]
            self._deferred_release_count += 1
        try:
            self._release_deferred_record(
                record,
                completed_job_id=completed_job_id,
                runtime_generation=runtime_generation,
            )
        finally:
            with self._lock:
                self._deferred_release_count -= 1

    def _release_deferred_record(
        self,
        record: _DeferredRecord,
        *,
        completed_job_id: str,
        runtime_generation: int,
    ) -> None:
        now_ms = _monotonic_ms()
        age_ms = max(0, now_ms - record.deferred_at_ms)
        context = _candidate_log_context(record.candidate)
        if now_ms >= record.expires_at_ms:
            log_enrichment_event(
                "deferred.expired",
                context=context,
                candidate_id=record.candidate.candidate_id,
                completed_job_id=completed_job_id,
                age_ms=age_ms,
                max_age_ms=_DEFERRED_MAX_AGE_MS,
                priority=_trigger_priority(record.candidate.trigger_id),
                **_candidate_focus_log_fields(record.candidate),
                **_candidate_trigger_log_fields(record.candidate),
            )
            return
        if (
            self.session_registry.get(record.candidate.session_id)
            is not record.registration
        ):
            self._log_deferred_cancelled(
                record,
                reason="session_registration_changed",
            )
            return
        log_enrichment_event(
            "deferred.released",
            context=context,
            candidate_id=record.candidate.candidate_id,
            completed_job_id=completed_job_id,
            age_ms=age_ms,
            max_age_ms=_DEFERRED_MAX_AGE_MS,
            priority=_trigger_priority(record.candidate.trigger_id),
            **_candidate_focus_log_fields(record.candidate),
            **_candidate_trigger_log_fields(record.candidate),
        )
        target_host = record.candidate.target_host
        if target_host is None:
            self._log_deferred_cancelled(record, reason="target_missing")
            return
        result = self._consider(
            record.candidate.event,
            trigger_id=record.candidate.trigger_id,
            target_host=target_host,
            surface=record.candidate.surface,
            automatic=True,
            _candidate=record.candidate,
            _allow_defer=False,
            _expected_registration=record.registration,
            _expected_runtime_generation=runtime_generation,
        )
        log_enrichment_event(
            "deferred.scheduled" if result.accepted else "deferred.discarded",
            context=(
                replace(context, job_id=result.job_id)
                if result.job_id is not None
                else context
            ),
            candidate_id=record.candidate.candidate_id,
            completed_job_id=completed_job_id,
            accepted=result.accepted,
            reason=result.reason,
            priority=_trigger_priority(record.candidate.trigger_id),
            age_ms=age_ms,
            max_age_ms=_DEFERRED_MAX_AGE_MS,
            **_candidate_focus_log_fields(record.candidate),
            **_candidate_trigger_log_fields(record.candidate),
        )

    def _log_deferred_cancelled(
        self,
        record: _DeferredRecord,
        *,
        reason: str,
    ) -> None:
        log_enrichment_event(
            "deferred.cancelled",
            context=_candidate_log_context(record.candidate),
            candidate_id=record.candidate.candidate_id,
            reason=reason,
            age_ms=max(0, _monotonic_ms() - record.deferred_at_ms),
            max_age_ms=_DEFERRED_MAX_AGE_MS,
            priority=_trigger_priority(record.candidate.trigger_id),
            **_candidate_focus_log_fields(record.candidate),
            **_candidate_trigger_log_fields(record.candidate),
        )

    def _observe_foreground_provider_failure(
        self,
        event: SessionEvent,
        *,
        runtime_generation: int,
    ) -> None:
        """Open the automatic-work circuit from bounded foreground metadata."""

        if event.source is not EventSource.COMMAND_EMULATOR:
            if event.payload.get("provider_failure") is not None:
                log_enrichment_event(
                    "provider_circuit.ignored",
                    level=logging.DEBUG,
                    context=_event_log_context(event),
                    reason="untrusted_event_source",
                    source=event.source.value,
                )
            return
        value = event.payload.get("provider_failure")
        if not isinstance(value, Mapping):
            return
        provider = value.get("provider")
        kind = value.get("kind")
        disposition = value.get("disposition")
        if not all(
            isinstance(item, str) and item.strip()
            for item in (provider, kind, disposition)
        ):
            return
        assert isinstance(provider, str)
        try:
            classification = ProviderFailureClassification(
                kind=ProviderFailureKind(str(kind)),
                disposition=ProviderFailureDisposition(str(disposition)),
                provider_code=(
                    str(value["code"]) if isinstance(value.get("code"), str) else None
                ),
                status_code=(
                    int(value["status"])
                    if isinstance(value.get("status"), int)
                    and not isinstance(value.get("status"), bool)
                    else None
                ),
            )
        except (TypeError, ValueError):
            log_enrichment_event(
                "provider_circuit.ignored",
                level=logging.DEBUG,
                context=_event_log_context(event),
                reason="invalid_provider_failure_metadata",
            )
            return
        if not classification.is_terminal:
            return
        normalized_provider = provider.strip().casefold()
        if normalized_provider not in self._agent_provider_names():
            log_enrichment_event(
                "provider_circuit.ignored",
                level=logging.DEBUG,
                context=_event_log_context(event),
                reason="provider_not_used_by_enrichment",
                provider=normalized_provider,
                **classification.to_log_fields(),
            )
            return
        self._open_provider_circuit(
            provider=normalized_provider,
            classification=classification,
            origin="foreground_command",
            context=_event_log_context(event),
            model=(
                str(value["model"]) if isinstance(value.get("model"), str) else None
            ),
            expected_runtime_generation=runtime_generation,
        )

    def _open_provider_circuit_from_model_failure(
        self,
        failure: EnrichmentModelFailure,
        *,
        context: EnrichmentLogContext,
        runtime_generation: int,
    ) -> None:
        self._open_provider_circuit(
            provider=failure.provider,
            classification=failure.classification,
            origin="enrichment_role",
            context=context,
            role=failure.role,
            model=failure.model,
            expected_runtime_generation=runtime_generation,
        )

    def _open_provider_circuit(
        self,
        *,
        provider: str,
        classification: ProviderFailureClassification,
        origin: str,
        context: EnrichmentLogContext,
        role: str | None = None,
        model: str | None = None,
        expected_runtime_generation: int | None = None,
    ) -> None:
        if not classification.is_terminal:
            return
        normalized_provider = provider.strip().casefold()
        if not normalized_provider:
            return
        circuit = _ProviderCircuit(
            provider=normalized_provider,
            session_id=context.session_id,
            classification=classification,
            origin=origin,
            opened_at=_utc_now(),
            role=role,
            model=model,
        )
        with self._lock:
            if expected_runtime_generation is not None and (
                not self._runtime_allows_generation_locked(expected_runtime_generation)
            ):
                return
            if normalized_provider in self._provider_circuits:
                return
            self._provider_circuits[normalized_provider] = circuit
            timers = tuple(self._idle_timers.items())
            self._idle_timers.clear()
            self._idle_tokens.clear()
            deferred = tuple(self._deferred_by_session.values())
            self._deferred_by_session.clear()
            circuit_count = len(self._provider_circuits)
        log_enrichment_event(
            "provider_circuit.opened",
            level=logging.WARNING,
            context=context,
            reason="terminal_provider_failure",
            provider_circuit_count=circuit_count,
            **circuit.log_fields(),
        )
        for session_id, timer in timers:
            timer.cancel()
            log_enrichment_event(
                "idle.cancelled",
                level=logging.DEBUG,
                context=EnrichmentLogContext(session_id=session_id),
                reason="provider_circuit_open",
                provider=normalized_provider,
            )
        for record in deferred:
            self._log_deferred_cancelled(
                record,
                reason="provider_circuit_open",
            )

    def _automatic_provider_circuit(self) -> _ProviderCircuit | None:
        with self._lock:
            if not self._provider_circuits:
                return None
            return min(
                self._provider_circuits.values(),
                key=lambda item: (item.opened_at, item.provider),
            )

    def _runtime_is_current(self, generation: int) -> bool:
        with self._lock:
            return self._runtime_is_current_locked(generation)

    def _runtime_is_current_locked(self, generation: int) -> bool:
        return self._started and generation == self._runtime_generation

    def _runtime_allows_generation(self, generation: int) -> bool:
        with self._lock:
            return self._runtime_allows_generation_locked(generation)

    def _runtime_allows_generation_locked(self, generation: int) -> bool:
        return (
            self._runtime_is_current_locked(generation)
            or generation in self._draining_generations
        )

    def _runtime_inactive_reason(self, generation: int) -> str:
        with self._lock:
            return self._runtime_inactive_reason_locked(generation)

    def _runtime_inactive_reason_locked(self, generation: int) -> str:
        if generation == self._runtime_generation:
            return "runtime_stopped"
        return "runtime_restarted"

    def _agent_provider_names(self) -> frozenset[str]:
        value = getattr(self.agent, "provider_names", ())
        if callable(value):
            value = value()
        if isinstance(value, str):
            value = (value,)
        if not isinstance(value, (set, frozenset, list, tuple)):
            return frozenset()
        return frozenset(
            item.strip().casefold()
            for item in value
            if isinstance(item, str) and item.strip()
        )

    def _log_provider_circuit_suppression(
        self,
        candidate: TriggerCandidate,
        circuit: _ProviderCircuit,
        *,
        job_id: str | None = None,
        stage: str = "trigger_admission",
    ) -> None:
        log_enrichment_event(
            "trigger.suppressed",
            context=_candidate_log_context(candidate, job_id=job_id),
            reason="provider_circuit_open",
            stage=stage,
            **circuit.log_fields(),
            **_candidate_focus_log_fields(candidate),
        )

    def _higher_priority_correlated_trigger(
        self,
        candidate: TriggerCandidate,
    ) -> str | None:
        if (
            candidate.event.source is EventSource.BACKGROUND_AGENT
            and candidate.event.source_agent_id == ENRICHMENT_AGENT_ID
        ):
            return None
        mode = _candidate_mode(candidate)
        candidate_priority = _trigger_priority(candidate.trigger_id)
        if candidate_priority == 0:
            return None
        with self._lock:
            triggers = [
                record.trigger_id
                for record in self._jobs.values()
                if record.event.session_id == candidate.session_id
                and record.runtime_generation == self._runtime_generation
                and record.event.correlation_id == candidate.correlation_id
                and record.mode is mode
            ]
            deferred = self._deferred_by_session.get(candidate.session_id)
            if (
                deferred is not None
                and deferred.runtime_generation == self._runtime_generation
                and deferred.candidate.correlation_id == candidate.correlation_id
                and _candidate_mode(deferred.candidate) is mode
            ):
                triggers.append(deferred.candidate.trigger_id)
        if not triggers:
            return None
        # Event routing submits the highest known candidate first. Once one
        # candidate exists, later correlated signals in the same mode are
        # terminally suppressed so one command can launch at most one job per
        # planning intent. A focused and ambient job may still run together.
        return max(triggers, key=_trigger_priority)

    def _forget_session(
        self,
        session_id: str,
        *,
        runtime_generation: int,
    ) -> None:
        self._cancel_idle(
            session_id,
            reason="session_closing",
            runtime_generation=runtime_generation,
        )
        with self._lock:
            if not self._runtime_is_current_locked(runtime_generation):
                return
            deferred = self._deferred_by_session.pop(session_id, None)
            self._active_bootstrap_retries.pop(session_id, None)
            self._active_bootstrap_retrying.discard(session_id)
            self._focus.pop(session_id, None)
            self._accepted_by_session.pop(session_id, None)
            for interaction_key in tuple(self._interaction_counts):
                if interaction_key[0] == session_id:
                    del self._interaction_counts[interaction_key]
            self._seen_focus_subjects = {
                key for key in self._seen_focus_subjects if key[0] != session_id
            }
            self._presence_bootstrapped_hosts = {
                key for key in self._presence_bootstrapped_hosts if key[0] != session_id
            }
            scope_prefix = f"{session_id}:"
            for scope_key in tuple(self._last_accepted_by_scope):
                if scope_key.startswith(scope_prefix):
                    del self._last_accepted_by_scope[scope_key]
            session_job_ids = {
                job_id
                for job_id, record in self._jobs.items()
                if record.event.session_id == session_id
            }
            self._available_jobs.difference_update(session_job_ids)
            self._matched_jobs.difference_update(session_job_ids)
            pending = []
            for job_id in session_job_ids:
                scheduled = self._scheduled_jobs.get(job_id)
                if scheduled is not None and not scheduled.future.running():
                    self._jobs[job_id].cancellation_reason = "session_closing"
                    scheduled.candidate = None
                    scheduled.registration = None
                    pending.append(scheduled.future)
            # Cancel under the scheduling lock, before a worker can claim the
            # now-cleared payload. Callbacks use this reentrant lock as well.
            for future in pending:
                future.cancel()
        if deferred is not None:
            self._log_deferred_cancelled(deferred, reason="session_closing")
        log_enrichment_event(
            "session.forgotten",
            level=logging.DEBUG,
            context=EnrichmentLogContext(session_id=session_id),
        )

    def _finish_job(
        self,
        job_id: str | None,
        deduplication_key: str,
        *,
        status: str,
        reason: str,
        failed: bool,
    ) -> None:
        if job_id is None:
            return
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.completed_at is not None:
                return
            record.status = status
            record.reason = reason
            record.completed_at = _utc_now()
            record.completed_monotonic = time.monotonic()
            session_id = record.event.session_id
            if record.runtime_generation == self._runtime_generation:
                if self._pending_by_key.get(deduplication_key) == record.candidate_id:
                    self._pending_by_key.pop(deduplication_key, None)
                inflight = self._inflight_by_session.get(session_id, 0) - 1
                if inflight > 0:
                    self._inflight_by_session[session_id] = inflight
                else:
                    self._inflight_by_session.pop(session_id, None)
                mode_key = (session_id, record.mode)
                mode_inflight = self._inflight_by_session_mode.get(mode_key, 0) - 1
                if mode_inflight > 0:
                    self._inflight_by_session_mode[mode_key] = mode_inflight
                else:
                    self._inflight_by_session_mode.pop(mode_key, None)
            if failed:
                self._failed_count += 1
            else:
                self._completed_count += 1
            if status in {
                "applied",
                "already_satisfied",
                "partially_applied",
                "shadow",
            }:
                self._available_jobs.add(job_id)
            snapshot = record.snapshot()
            remaining_inflight = self._inflight_by_session.get(session_id, 0)
            remaining_mode_inflight = self._inflight_by_session_mode.get(
                (session_id, record.mode), 0
            )
        log_enrichment_event(
            "job.failed" if failed else "job.completed",
            level=logging.WARNING if failed else logging.INFO,
            context=EnrichmentLogContext(
                session_id=snapshot.session_id,
                job_id=snapshot.job_id,
                event_id=record.event.event_id,
                correlation_id=record.event.correlation_id,
                trigger_id=record.trigger_id,
                enrichment_mode=record.mode,
                target_host=snapshot.target_host,
                requested_surface=snapshot.surface,
                selected_surface=record.selected_surface,
                surface=record.selected_surface or snapshot.surface,
            ),
            status=status,
            reason=reason,
            duration_ms=snapshot.duration_ms,
            inflight=remaining_inflight,
            mode_inflight=remaining_mode_inflight,
        )
        if job_id in self._available_jobs:
            log_enrichment_event(
                "enrichment.ready",
                context=EnrichmentLogContext(
                    session_id=snapshot.session_id,
                    job_id=snapshot.job_id,
                    event_id=record.event.event_id,
                    correlation_id=record.event.correlation_id,
                    trigger_id=record.trigger_id,
                    enrichment_mode=record.mode,
                    target_host=snapshot.target_host,
                    requested_surface=snapshot.surface,
                    selected_surface=record.selected_surface,
                    surface=record.selected_surface or snapshot.surface,
                ),
                status=status,
                apply_mode=self.settings.apply_mode,
                trigger_to_ready_ms=round(
                    (record.completed_monotonic - record.queued_monotonic) * 1000
                ),
            )

    def _observe_enrichment_command_match(
        self,
        event: SessionEvent,
        *,
        runtime_generation: int,
    ) -> None:
        with self._lock:
            if not self._runtime_is_current_locked(runtime_generation):
                return
            matching = [
                self._jobs[job_id]
                for job_id in reversed(self._job_order)
                if self._jobs[job_id].event.session_id == event.session_id
                and self._jobs[job_id].target_host == event.target_host
                and _job_surface_matches(self._jobs[job_id], event.surface)
            ]
            late = next(
                (
                    job
                    for job in matching
                    if job.runtime_generation == runtime_generation
                    and job.status in {"queued", "running"}
                ),
                None,
            )
            available = next(
                (
                    job
                    for job in matching
                    if job.job_id in self._available_jobs
                    and job.job_id not in self._matched_jobs
                ),
                None,
            )
            if available is not None:
                self._matched_jobs.add(available.job_id)
        if late is not None:
            match_fields = _job_command_match_log_fields(late, event)
            log_enrichment_event(
                "enrichment.late",
                context=EnrichmentLogContext(
                    session_id=event.session_id,
                    job_id=late.job_id,
                    event_id=event.event_id,
                    caused_by_event_id=event.caused_by_event_id,
                    correlation_id=event.correlation_id,
                    trigger_id=late.trigger_id,
                    enrichment_mode=late.mode,
                    command_id=event.command_id,
                    target_host=event.target_host,
                    requested_surface=late.surface,
                    selected_surface=late.selected_surface,
                    surface=late.selected_surface or late.surface,
                ),
                job_status=late.status,
                command_surface=event.surface,
                elapsed_ms=round((time.monotonic() - late.queued_monotonic) * 1000),
                **match_fields,
            )
        if available is not None:
            match_fields = _job_command_match_log_fields(available, event)
            lead_time_ms = None
            if available.completed_monotonic is not None:
                lead_time_ms = round(
                    (time.monotonic() - available.completed_monotonic) * 1000
                )
            log_enrichment_event(
                "enrichment.command_matched",
                context=EnrichmentLogContext(
                    session_id=event.session_id,
                    job_id=available.job_id,
                    event_id=event.event_id,
                    caused_by_event_id=event.caused_by_event_id,
                    correlation_id=event.correlation_id,
                    trigger_id=available.trigger_id,
                    enrichment_mode=available.mode,
                    command_id=event.command_id,
                    target_host=event.target_host,
                    requested_surface=available.surface,
                    selected_surface=available.selected_surface,
                    surface=available.selected_surface or available.surface,
                ),
                lead_time_ms=lead_time_ms,
                job_status=available.status,
                command_surface=event.surface,
                **match_fields,
            )

    def _job_record(self, job_id: str) -> _JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _trim_jobs(self) -> None:
        while len(self._job_order) > _JOB_HISTORY_LIMIT:
            oldest = self._job_order.popleft()
            record = self._jobs.get(oldest)
            if record is not None and record.completed_at is None:
                self._job_order.appendleft(oldest)
                return
            self._jobs.pop(oldest, None)
            self._available_jobs.discard(oldest)
            self._matched_jobs.discard(oldest)

    def _log_decision(
        self,
        event: SessionEvent,
        decision: TriggerDecision,
        target_host: str,
        surface: str | None,
        *,
        job_id: str | None,
        candidate: TriggerCandidate,
        cooldown_scope_sha256: str | None,
        inflight_before: int,
        inflight_after: int,
        mode_inflight_before: int,
        mode_inflight_after: int,
    ) -> None:
        level = (
            logging.INFO
            if decision.code is not TriggerDecisionCode.SUPPRESSED
            or decision.reason.value
            in {"rate_limit_reached", "cooldown_active", "inflight_limit_reached"}
            else logging.DEBUG
        )
        log_enrichment_event(
            f"trigger.{decision.code.value}",
            level=level,
            context=EnrichmentLogContext(
                session_id=event.session_id,
                job_id=job_id,
                event_id=event.event_id,
                caused_by_event_id=event.caused_by_event_id,
                correlation_id=event.correlation_id,
                trigger_id=decision.trigger_id,
                enrichment_mode=_candidate_mode(candidate),
                command_id=event.command_id,
                target_host=target_host,
                requested_surface=surface,
                surface=surface,
            ),
            reason=decision.reason.value,
            policy=decision.policy_name,
            metric=decision.metric.value if decision.metric is not None else None,
            observed=decision.observed,
            threshold=decision.threshold,
            retry_after_ms=decision.retry_after_ms,
            candidate_id=decision.candidate_id,
            coalesced_into_candidate_id=decision.coalesced_into_candidate_id,
            priority=_trigger_priority(candidate.trigger_id),
            cooldown_scope_sha256=cooldown_scope_sha256,
            inflight_before=inflight_before,
            inflight_after=inflight_after,
            mode_inflight_before=mode_inflight_before,
            mode_inflight_after=mode_inflight_after,
            max_inflight=self.settings.max_inflight_per_session,
            **_candidate_focus_log_fields(candidate),
            **_candidate_trigger_log_fields(candidate),
        )

    def _log_trigger_not_configured(
        self,
        event: SessionEvent,
        trigger_id: str,
    ) -> None:
        log_enrichment_event(
            "trigger.suppressed",
            level=logging.DEBUG,
            context=_event_log_context(
                event,
                trigger_id=trigger_id,
                enrichment_mode=_trigger_mode(trigger_id),
            ),
            reason="trigger_not_configured",
        )

    def _log_missing_session(self, event: SessionEvent, trigger_id: str) -> None:
        log_enrichment_event(
            "trigger.suppressed",
            context=_event_log_context(
                event,
                trigger_id=trigger_id,
                enrichment_mode=_trigger_mode(trigger_id),
            ),
            reason="session_missing",
        )


def _event_log_context(
    event: SessionEvent,
    *,
    trigger_id: str | None = None,
    target_host: str | None = None,
    enrichment_mode: EnrichmentMode | None = None,
) -> EnrichmentLogContext:
    return EnrichmentLogContext(
        session_id=event.session_id,
        event_id=event.event_id,
        caused_by_event_id=event.caused_by_event_id,
        correlation_id=event.correlation_id,
        trigger_id=trigger_id,
        enrichment_mode=(
            enrichment_mode
            if enrichment_mode is not None
            else (_trigger_mode(trigger_id) if trigger_id is not None else None)
        ),
        command_id=event.command_id,
        target_host=target_host or event.target_host,
        requested_surface=event.surface,
        surface=event.surface,
    )


def _candidate_log_context(
    candidate: TriggerCandidate,
    *,
    job_id: str | None = None,
) -> EnrichmentLogContext:
    return EnrichmentLogContext(
        session_id=candidate.session_id,
        job_id=job_id,
        event_id=candidate.event_id,
        caused_by_event_id=candidate.event.caused_by_event_id,
        correlation_id=candidate.correlation_id,
        trigger_id=candidate.trigger_id,
        enrichment_mode=_candidate_mode(candidate),
        command_id=candidate.event.command_id,
        target_host=candidate.target_host,
        requested_surface=candidate.surface,
        surface=candidate.surface,
    )


def _job_log_context(context: EnrichmentJobContext) -> EnrichmentLogContext:
    return EnrichmentLogContext(
        session_id=context.session_id,
        job_id=context.job_id,
        event_id=context.event_id,
        caused_by_event_id=context.caused_by_event_id,
        correlation_id=context.correlation_id,
        trigger_id=context.trigger_id,
        enrichment_mode=context.mode,
        command_id=context.command_id,
        target_host=context.target_host,
        requested_surface=context.surface_hint,
        surface=context.surface_hint,
    )


def _bounded_job_anchors(
    anchors: tuple[str, ...],
    *,
    target_host: str,
) -> tuple[tuple[str, ...], int]:
    """Keep the read-ahead target when a materialization exposes many hosts."""

    unique = tuple(dict.fromkeys(anchors))
    if target_host in unique:
        unique = (target_host, *(anchor for anchor in unique if anchor != target_host))
    retained = unique[:_MAX_JOB_ANCHORS]
    return retained, max(0, len(anchors) - len(retained))


def _candidate_interaction_focus(
    candidate: TriggerCandidate,
) -> dict[str, object]:
    value = candidate.details.get("interaction_focus")
    if not isinstance(value, Mapping):
        return {}
    plain = _plain_json_value(value)
    return plain if isinstance(plain, dict) else {}


def _plain_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain_json_value(child) for child in value]
    return value


def _candidate_focus_log_fields(
    candidate: TriggerCandidate,
    *,
    prefix: str = "",
) -> dict[str, object]:
    focus = candidate.details.get("interaction_focus")
    path_count, artifact_count = interaction_focus_counts(focus)
    key = interaction_focus_key(focus)
    return {
        f"{prefix}focus_key_sha256": _focus_key_digest(key),
        f"{prefix}focus_path_count": path_count,
        f"{prefix}focus_artifact_count": artifact_count,
    }


def _candidate_trigger_log_fields(
    candidate: TriggerCandidate,
    *,
    prefix: str = "",
) -> dict[str, object]:
    fields: dict[str, object] = {
        f"{prefix}enrichment_mode": _candidate_mode(candidate).value,
    }
    for key in ("presence_probe_kind", "subject_key_sha256", "pulse_count"):
        value = candidate.details.get(key)
        if isinstance(value, str) and value:
            fields[f"{prefix}{key}"] = value
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            fields[f"{prefix}{key}"] = value
    return fields


def _active_host_bootstrap_candidate(candidate: TriggerCandidate) -> bool:
    return (
        candidate.trigger_id == "host_presence_bootstrap"
        and candidate.details.get("active_host_bootstrap") is True
    )


def _candidate_mode(candidate: TriggerCandidate) -> EnrichmentMode:
    value = candidate.details.get("enrichment_mode")
    try:
        return EnrichmentMode(str(value))
    except ValueError:
        return _trigger_mode(candidate.trigger_id)


def _trigger_mode(trigger_id: str) -> EnrichmentMode:
    return _TRIGGER_MODE.get(trigger_id, EnrichmentMode.FOCUSED)


def _candidate_identity(
    details: Mapping[str, object],
    *,
    focus_digest: str | None,
) -> str:
    for key in ("presence_probe_kind", "subject_key_sha256"):
        value = details.get(key)
        if isinstance(value, str) and value:
            return value
    return focus_digest or "general"


def _candidate_cooldown_scope(
    candidate: TriggerCandidate,
    *,
    mode: EnrichmentMode,
) -> str:
    focus_digest = _focus_key_digest(
        interaction_focus_key(candidate.details.get("interaction_focus"))
    )
    if candidate.trigger_id == "presence_probe":
        subject = str(candidate.details.get("presence_probe_kind") or "general")
    elif candidate.trigger_id == "subject_first_touch":
        subject = str(candidate.details.get("subject_key_sha256") or "general")
    elif candidate.trigger_id in {
        "state_materialized",
        "surface_focus",
        "materialization_followthrough",
    }:
        subject = focus_digest or candidate.surface or "general"
    elif candidate.trigger_id == "host_presence_bootstrap":
        subject = "bootstrap"
    elif candidate.trigger_id in {"interaction_pulse", "session_idle"}:
        subject = "ambient-general"
    else:
        subject = candidate.surface or "general"
    return ":".join(
        (
            candidate.session_id,
            candidate.target_host or "unknown",
            mode.value,
            subject,
        )
    )


def _successful_command(event: SessionEvent) -> bool:
    exit_status = event.payload.get("exit_status")
    return (
        isinstance(exit_status, int)
        and not isinstance(exit_status, bool)
        and exit_status == 0
        and event.payload.get("interrupted") is not True
        and event.payload.get("provider_failure") is None
    )


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _single_authority_domain_log_fields(
    domains: Iterable[str],
) -> dict[str, str]:
    unique = sorted(set(domains))
    if len(unique) != 1:
        return {}
    return {"authority_domain": unique[0]}


def _job_command_match_log_fields(
    record: _JobRecord,
    event: SessionEvent,
) -> dict[str, object]:
    expected = record.event.payload.get("interaction_focus")
    observed = event.payload.get("interaction_focus")
    expected_path_count, expected_artifact_count = interaction_focus_counts(expected)
    expected_key = interaction_focus_key(expected)
    observed_path_count, observed_artifact_count = interaction_focus_counts(observed)
    observed_key = interaction_focus_key(observed)
    return {
        "match_policy": _FOCUS_MATCH_POLICY,
        "match_classification": classify_interaction_focus_match(
            expected,
            observed,
        ).value,
        "focus_key_sha256": _focus_key_digest(expected_key),
        "focus_path_count": expected_path_count,
        "focus_artifact_count": expected_artifact_count,
        "command_focus_key_sha256": _focus_key_digest(observed_key),
        "command_focus_path_count": observed_path_count,
        "command_focus_artifact_count": observed_artifact_count,
    }


def _focus_key_digest(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _trigger_priority(trigger_id: str) -> int:
    return _TRIGGER_PRIORITY.get(trigger_id, 0)


def _job_surface_matches(record: _JobRecord, command_surface: str | None) -> bool:
    effective_surface = record.selected_surface or record.surface
    return (
        effective_surface is None
        or command_surface is None
        or effective_surface == command_surface
    )


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
