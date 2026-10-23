from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

from dreamland.console_introspection import (
    CommandInspection,
    CommandSummary,
    ConsoleIntrospection,
    DisabledIntrospection,
    IntrospectionUnavailable,
    SessionList,
    SessionSummary,
    StateDelta,
)
from dreamland.evaluation_config import (
    EVALUATION_CONFIG_KEY,
    evaluation_snapshot,
    resolve_mcp_session_config,
)
from dreamland.models import (
    ConnectionMetadata,
    EnvironmentMetadata,
    SessionState,
)
from dreamland.program_input import ignores_blank_input
from dreamland.runtime_logging import (
    EnrichmentEventLogBuffer,
    LogRecordBuffer,
    SessionTraceLogBuffer,
    trace_evaluation_config,
    trace_input,
    trace_output,
    trace_spiritguide,
    trace_state_profile_apply,
)
from dreamland.session_metadata import (
    MCP_SESSION_ORIGIN,
    SESSION_LABEL_METADATA_KEY,
    SESSION_METADATA_METADATA_KEY,
    SESSION_ORIGIN_METADATA_KEY,
    SESSION_TAGS_METADATA_KEY,
    normalize_session_tags,
    session_label_from_metadata,
    session_origin_from_metadata,
    session_tags_from_metadata,
)
from dreamland.session_registry import LiveSessionRegistry
from dreamland.session_state import active_shell_frame, apply_external_state_update
from dreamland.spiritguide import (
    DeterministicSpiritguide,
    Spiritguide,
    SpiritguideContext,
    SpiritguideResult,
)
from dreamland.spiritguide_profiles import (
    SpiritguideProfileOperationResult,
    create_spiritguide_profile,
    fork_spiritguide_profile,
    update_spiritguide_profile,
)
from dreamland.ssh_streaming import LineBufferedCommandWriter, execute_backend_stream
from dreamland.ssh_types import CommandBackend
from dreamland.state_profiles import (
    STATE_PROFILE_LEVELS,
    STATE_PROFILE_METADATA_LEVEL,
    STATE_PROFILE_METADATA_NAME,
    StateProfile,
    StateProfileError,
    StateProfileLevel,
    StateProfileRuntime,
    StateProfileStore,
    StateProfileSummary,
    apply_state_profile_to_session,
    create_state_profile,
    session_state_for_level,
)
from dreamland.structured_failures import (
    StructuredFailureError,
    StructuredFailureStore,
    replay_structured_failure_parser,
)

LOGGER = logging.getLogger("dreamland.admin")
if TYPE_CHECKING:
    from dreamland.config import DreamlandSettings
    from dreamland.evaluation_runtime import EvaluationRuntimeFactory
_Record = TypeVar("_Record")


@dataclass(frozen=True, slots=True)
class AdminCapabilitySummary:
    logs: bool
    traces: bool
    live_sessions: bool
    live_session_state: bool
    historical_introspection: bool
    state_profiles: bool
    structured_failures: bool
    spiritguide: bool
    shutdown: bool
    mcp_write_tools: bool = False
    evaluation_overrides: bool = False


@dataclass(frozen=True, slots=True)
class AdminRuntimeStatus:
    live_session_count: int
    introspection_available: bool
    default_state_profile: str | None = None
    shutdown_requested: bool = False


@dataclass(frozen=True, slots=True)
class AdminLogEntry:
    sequence: int
    text: str


@dataclass(frozen=True, slots=True)
class AdminTraceEntry:
    sequence: int
    session_id: str
    text: str


@dataclass(frozen=True, slots=True)
class AdminEnrichmentEventEntry:
    sequence: int
    session_id: str
    job_id: str | None
    event: str
    level: str
    text: str
    event_id: str | None = None
    caused_by_event_id: str | None = None
    correlation_id: str | None = None
    trigger_id: str | None = None
    enrichment_mode: str | None = None
    coordinator_role: str | None = None
    command_id: str | None = None
    target_host: str | None = None
    requested_surface: str | None = None
    selected_surface: str | None = None
    surface: str | None = None
    status: str | None = None
    reason: str | None = None
    reason_code: str | None = None
    candidate_id: str | None = None
    coalesced_into_candidate_id: str | None = None
    replaced_candidate_id: str | None = None
    retained_candidate_id: str | None = None
    completed_job_id: str | None = None
    higher_priority_trigger: str | None = None
    retained_trigger: str | None = None
    retained_enrichment_mode: str | None = None
    presence_probe_kind: str | None = None
    retained_presence_probe_kind: str | None = None
    subject_key_sha256: str | None = None
    retained_subject_key_sha256: str | None = None
    cooldown_scope_sha256: str | None = None
    focus_key_sha256: str | None = None
    match_policy: str | None = None
    match_classification: str | None = None
    command_focus_key_sha256: str | None = None
    retained_focus_key_sha256: str | None = None
    failure_kind: str | None = None
    failure_disposition: str | None = None
    provider: str | None = None
    provider_code: str | None = None
    role: str | None = None
    model: str | None = None
    circuit_origin: str | None = None
    authority_domain: str | None = None
    binding_reason: str | None = None
    policy_version: str | None = None
    action: str | None = None
    integrator_refresh_outcome: str | None = None
    integrator_refresh_attempt: int | None = None
    integrator_refresh_latency_ms: int | None = None
    priority: int | None = None
    retained_priority: int | None = None
    pulse_count: int | None = None
    retained_pulse_count: int | None = None
    inflight: int | None = None
    mode_inflight: int | None = None
    inflight_before: int | None = None
    inflight_after: int | None = None
    mode_inflight_before: int | None = None
    mode_inflight_after: int | None = None
    max_inflight: int | None = None
    age_ms: int | None = None
    max_age_ms: int | None = None
    focus_path_count: int | None = None
    focus_artifact_count: int | None = None
    command_focus_path_count: int | None = None
    command_focus_artifact_count: int | None = None
    retained_focus_path_count: int | None = None
    retained_focus_artifact_count: int | None = None
    provider_status_code: int | None = None
    authority_dependency_count: int | None = None
    provider_circuit_count: int | None = None
    accepted: bool | None = None
    eligible: bool | None = None
    changed: bool | None = None
    foreground_authority_present: bool | None = None
    foreground_authority_changed: bool | None = None
    registry_targets: tuple[tuple[str, ...], ...] = ()
    relation_policy_version: str | None = None
    relation_limit: int | None = None
    relation_received_count: int | None = None
    relation_accepted_count: int | None = None
    relation_invalid_count: int | None = None
    relation_duplicate_count: int | None = None
    relation_overflow_count: int | None = None
    relation_discarded_count: int | None = None
    presence_gap_policy_version: str | None = None
    presence_gap_sha256: str | None = None
    presence_gap_ambient_objective: str | None = None
    presence_gap_preferred_surface: str | None = None
    presence_gap_anti_repetition_subject_count: int | None = None
    presence_gap_anti_repetition_system_count: int | None = None
    foreground_evidence_policy_version: str | None = None
    foreground_evidence_sha256: str | None = None
    host_time_observed: bool | None = None
    recent_exhaustive_evidence_count: int | None = None


@dataclass(frozen=True, slots=True)
class AdminEnrichmentEventRetention:
    capacity: int
    retained: int
    evicted: int
    oldest_sequence: int | None
    latest_sequence: int | None
    storage: str = "process_memory"
    restart_durable: bool = False


@dataclass(frozen=True, slots=True)
class AdminEnrichmentEventCollection:
    session_id: str
    job_id: str | None
    after_sequence: int | None
    returned: int
    events: list[AdminEnrichmentEventEntry]
    retention: AdminEnrichmentEventRetention


@dataclass(frozen=True, slots=True)
class AdminLiveSessionSummary:
    session_id: str
    username: str
    login_username: str | None
    hostname: str
    cwd: str
    history_count: int
    state_profile: str | None = None
    tags: list[str] = field(default_factory=list)
    label: str | None = None
    origin: str | None = None


@dataclass(frozen=True, slots=True)
class AdminLiveSessionState:
    session_id: str
    state: dict[str, object]


@dataclass(frozen=True, slots=True)
class AdminSessionSummary:
    id: str
    live: bool
    langfuse: bool
    created_at: object = None
    environment: str | None = None
    state_profile: str | None = None
    username: str | None = None
    login_username: str | None = None
    hostname: str | None = None
    cwd: str | None = None
    history_count: int | None = None
    tags: list[str] = field(default_factory=list)
    label: str | None = None
    origin: str | None = None


@dataclass(frozen=True, slots=True)
class AdminSessionCollection:
    sessions: list[AdminSessionSummary]
    total: int
    langfuse_available: bool


@dataclass(frozen=True, slots=True)
class AdminSessionTagGroup:
    tag: str
    count: int
    session_ids: list[str]


@dataclass(frozen=True, slots=True)
class AdminSessionInspection:
    id: str
    live: bool
    langfuse: bool
    langfuse_available: bool
    summary: AdminSessionSummary
    inspections: dict[str, bool]
    live_session: AdminLiveSessionSummary | None = None
    historical_session: SessionSummary | None = None
    historical_lookup_limit: int = 100
    historical_lookup_complete: bool | None = None


@dataclass(frozen=True, slots=True)
class AdminEventStreamSummary:
    transport: str
    endpoint: str
    event_type: str
    cursor_parameter: str
    last_sequence: int


@dataclass(frozen=True, slots=True)
class AdminSessionEventStreamSummary:
    session_id: str
    transport: str
    endpoint: str
    event_type: str
    cursor_parameter: str
    last_sequence: int
    live: bool


@dataclass(frozen=True, slots=True)
class AdminStateProfileSummary:
    name: str
    level: str
    created_at: str
    source_session_id: str | None
    comment: str | None
    path: str


@dataclass(frozen=True, slots=True)
class AdminDefaultStateProfile:
    name: str
    level: str
    created_at: str
    source_session_id: str | None
    comment: str | None


@dataclass(frozen=True, slots=True)
class AdminStateProfileDetail:
    name: str
    level: str
    created_at: str
    source_session_id: str | None
    comment: str | None
    state: dict[str, object]


@dataclass(frozen=True, slots=True)
class AdminStructuredFailureSummary:
    id: str
    created_at: str
    session_id: str
    command_id: str
    command: str
    failure_class: str
    parser_error: str
    path: str


@dataclass(frozen=True, slots=True)
class AdminStructuredFailureDetail:
    id: str
    created_at: str
    session_id: str
    command_id: str
    command: str
    starting_cwd: str
    username: str
    hostname: str
    profile_name: str
    failure_class: str
    parser_error: str
    raw_response: str
    result: dict[str, object]
    pre_command_state_profile: dict[str, object]
    path: str


@dataclass(frozen=True, slots=True)
class AdminStructuredFailureReplay:
    id: str
    accepted: bool
    failure_class: str | None
    parser_error: str | None
    stdout: str
    stderr: str
    exit_status: int
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class AdminStateProfileSaveResult:
    name: str
    level: str
    session_id: str
    path: str


@dataclass(frozen=True, slots=True)
class AdminStateProfileApplyResult:
    name: str
    level: str
    session_id: str
    source_session_id: str | None


@dataclass(frozen=True, slots=True)
class AdminStateProfileDeleteResult:
    name: str
    path: str
    cleared_default: bool


@dataclass(frozen=True, slots=True)
class AdminStateProfileClearDefaultResult:
    previous: AdminDefaultStateProfile | None


@dataclass(frozen=True, slots=True)
class AdminSpiritguideResult:
    session_id: str
    message: str
    dry_run: bool
    applied: bool
    structured_response_accepted: object
    patch_keys: list[str]
    state_update: dict[str, object]
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class AdminSpiritguideProfileResult:
    action: str
    name: str
    level: str
    message: str
    dry_run: bool
    saved: bool
    applied: bool
    structured_response_accepted: object
    patch_keys: list[str]
    state_update: dict[str, object]
    metadata: dict[str, object]
    excluded_patch_keys: list[str]
    path: str | None = None
    source_name: str | None = None


@dataclass(frozen=True, slots=True)
class AdminMcpSessionCreateResult:
    session_id: str
    username: str
    login_username: str | None
    hostname: str
    cwd: str
    state_profile: str | None
    tags: list[str]
    label: str | None
    metadata: dict[str, object]
    evaluation_config: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdminMcpCommandContext:
    username: str
    hostname: str
    cwd: str
    active_program: str
    prompt: str | None
    label: str


@dataclass(frozen=True, slots=True)
class AdminMcpCommandResult:
    session_id: str
    command_id: str | None
    command: str
    stdout: str
    stderr: str
    exit_status: int
    interrupted: bool
    signal: str | None
    history_index: int | None
    history_position: int | None
    history_count: int
    inspection_target: str | None
    context: AdminMcpCommandContext


@dataclass(frozen=True, slots=True)
class AdminSessionCloseManyResult:
    closed: list[str]
    matched: int
    missing: list[str] = field(default_factory=list)
    dry_run: bool = False
    filters: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdminShutdownResult:
    status: str


class AdminServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class DreamlandAdminService:
    def __init__(
        self,
        *,
        logs: LogRecordBuffer,
        trace_logs: SessionTraceLogBuffer,
        enrichment_event_logs: EnrichmentEventLogBuffer | None = None,
        session_registry: LiveSessionRegistry,
        state_profile_runtime: StateProfileRuntime,
        introspection: ConsoleIntrospection | None = None,
        spiritguide: Spiritguide | None = None,
        request_shutdown: Callable[[], None] | None = None,
        shutdown_requested: Callable[[], bool] | None = None,
        spiritguide_available: bool = False,
        shutdown_available: bool = False,
        mcp_write_tools: bool = False,
        effective_config: dict[str, object] | None = None,
        command_backend: CommandBackend | None = None,
        record_session_start: Callable[[SessionState], None] | None = None,
        structured_failure_store: StructuredFailureStore | None = None,
        evaluation_factory: EvaluationRuntimeFactory | None = None,
    ) -> None:
        self.logs = logs
        self.trace_logs = trace_logs
        self.enrichment_event_logs = enrichment_event_logs or EnrichmentEventLogBuffer(
            logs.capacity
        )
        self.session_registry = session_registry
        self.state_profile_runtime = state_profile_runtime
        self.state_profiles = state_profile_runtime.store
        self.introspection = introspection or DisabledIntrospection()
        self.spiritguide = spiritguide or DeterministicSpiritguide()
        self.request_shutdown_callback = request_shutdown
        self.shutdown_requested = shutdown_requested or (lambda: False)
        self._shutdown_requested = False
        self.spiritguide_available = spiritguide_available
        self.shutdown_available = shutdown_available
        self.mcp_write_tools = mcp_write_tools
        self.effective_config = effective_config or {}
        self.command_backend = command_backend
        self.record_session_start = record_session_start
        self.structured_failures = structured_failure_store or StructuredFailureStore()
        self.evaluation_factory = evaluation_factory

    def capabilities(self) -> AdminCapabilitySummary:
        return AdminCapabilitySummary(
            logs=True,
            traces=True,
            live_sessions=True,
            live_session_state=True,
            historical_introspection=self.introspection.available(),
            state_profiles=True,
            structured_failures=True,
            spiritguide=self.spiritguide_available,
            shutdown=self.shutdown_available,
            mcp_write_tools=self.mcp_write_tools,
            evaluation_overrides=self.evaluation_factory is not None,
        )

    def status(self) -> AdminRuntimeStatus:
        return AdminRuntimeStatus(
            live_session_count=len(self.session_registry.list_sessions()),
            introspection_available=self.introspection.available(),
            default_state_profile=_default_profile_label(
                self.state_profile_runtime.current_profile()
            ),
            shutdown_requested=self._shutdown_requested or self.shutdown_requested(),
        )

    def config_effective(self) -> dict[str, object]:
        return self.effective_config

    def recent_logs(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
    ) -> list[AdminLogEntry]:
        records = (
            self.logs.after(after_sequence, timeout=0)
            if after_sequence is not None
            else self.logs.latest(limit)
        )
        return [
            AdminLogEntry(sequence=record.sequence, text=record.text)
            for record in _limited(records, limit)
        ]

    def wait_for_logs(
        self,
        *,
        after_sequence: int,
        limit: int,
        timeout: float,
    ) -> list[AdminLogEntry]:
        records = self.logs.after(after_sequence, timeout=timeout)
        return [
            AdminLogEntry(sequence=record.sequence, text=record.text)
            for record in _limited(records, limit)
        ]

    def session_trace(
        self,
        *,
        session_id: str,
        limit: int,
        after_sequence: int | None = None,
    ) -> list[AdminTraceEntry]:
        records = (
            self.trace_logs.after(session_id, after_sequence, timeout=0)
            if after_sequence is not None
            else self.trace_logs.latest(session_id, limit)
        )
        return [
            AdminTraceEntry(
                sequence=record.sequence,
                session_id=record.session_id,
                text=record.text,
            )
            for record in _limited(records, limit)
        ]

    def wait_for_session_trace(
        self,
        *,
        session_id: str,
        after_sequence: int,
        limit: int,
        timeout: float,
    ) -> list[AdminTraceEntry]:
        records = self.trace_logs.after(
            session_id,
            after_sequence,
            timeout=timeout,
        )
        return [
            AdminTraceEntry(
                sequence=record.sequence,
                session_id=record.session_id,
                text=record.text,
            )
            for record in _limited(records, limit)
        ]

    def session_enrichment_events(
        self,
        *,
        session_id: str,
        limit: int,
        job_id: str | None = None,
        after_sequence: int | None = None,
    ) -> AdminEnrichmentEventCollection:
        """Read retained enrichment events without requiring a live session."""

        normalized_job_id = (
            job_id.strip() if isinstance(job_id, str) and job_id.strip() else None
        )
        records = (
            self.enrichment_event_logs.after(
                session_id,
                after_sequence,
                job_id=normalized_job_id,
                timeout=0,
            )
            if after_sequence is not None
            else self.enrichment_event_logs.latest(
                session_id,
                limit,
                job_id=normalized_job_id,
            )
        )
        events = [
            AdminEnrichmentEventEntry(
                sequence=record.sequence,
                session_id=record.session_id,
                job_id=record.job_id,
                event=record.event,
                level=record.level,
                text=record.text,
                event_id=record.event_id,
                caused_by_event_id=record.caused_by_event_id,
                correlation_id=record.correlation_id,
                trigger_id=record.trigger_id,
                enrichment_mode=record.enrichment_mode,
                coordinator_role=record.coordinator_role,
                command_id=record.command_id,
                target_host=record.target_host,
                requested_surface=record.requested_surface,
                selected_surface=record.selected_surface,
                surface=record.surface,
                status=record.status,
                reason=record.reason,
                reason_code=record.reason_code,
                candidate_id=record.candidate_id,
                coalesced_into_candidate_id=(record.coalesced_into_candidate_id),
                replaced_candidate_id=record.replaced_candidate_id,
                retained_candidate_id=record.retained_candidate_id,
                completed_job_id=record.completed_job_id,
                higher_priority_trigger=record.higher_priority_trigger,
                retained_trigger=record.retained_trigger,
                retained_enrichment_mode=record.retained_enrichment_mode,
                presence_probe_kind=record.presence_probe_kind,
                retained_presence_probe_kind=(record.retained_presence_probe_kind),
                subject_key_sha256=record.subject_key_sha256,
                retained_subject_key_sha256=record.retained_subject_key_sha256,
                cooldown_scope_sha256=record.cooldown_scope_sha256,
                focus_key_sha256=record.focus_key_sha256,
                match_policy=record.match_policy,
                match_classification=record.match_classification,
                command_focus_key_sha256=record.command_focus_key_sha256,
                retained_focus_key_sha256=record.retained_focus_key_sha256,
                failure_kind=record.failure_kind,
                failure_disposition=record.failure_disposition,
                provider=record.provider,
                provider_code=record.provider_code,
                role=record.role,
                model=record.model,
                circuit_origin=record.circuit_origin,
                authority_domain=record.authority_domain,
                binding_reason=record.binding_reason,
                policy_version=record.policy_version,
                action=record.action,
                integrator_refresh_outcome=record.integrator_refresh_outcome,
                integrator_refresh_attempt=record.integrator_refresh_attempt,
                integrator_refresh_latency_ms=(record.integrator_refresh_latency_ms),
                priority=record.priority,
                retained_priority=record.retained_priority,
                pulse_count=record.pulse_count,
                retained_pulse_count=record.retained_pulse_count,
                inflight=record.inflight,
                mode_inflight=record.mode_inflight,
                inflight_before=record.inflight_before,
                inflight_after=record.inflight_after,
                mode_inflight_before=record.mode_inflight_before,
                mode_inflight_after=record.mode_inflight_after,
                max_inflight=record.max_inflight,
                age_ms=record.age_ms,
                max_age_ms=record.max_age_ms,
                focus_path_count=record.focus_path_count,
                focus_artifact_count=record.focus_artifact_count,
                command_focus_path_count=record.command_focus_path_count,
                command_focus_artifact_count=record.command_focus_artifact_count,
                retained_focus_path_count=record.retained_focus_path_count,
                retained_focus_artifact_count=(record.retained_focus_artifact_count),
                provider_status_code=record.provider_status_code,
                authority_dependency_count=record.authority_dependency_count,
                provider_circuit_count=record.provider_circuit_count,
                accepted=record.accepted,
                eligible=record.eligible,
                changed=record.changed,
                foreground_authority_present=record.foreground_authority_present,
                foreground_authority_changed=record.foreground_authority_changed,
                registry_targets=record.registry_targets,
                relation_policy_version=record.relation_policy_version,
                relation_limit=record.relation_limit,
                relation_received_count=record.relation_received_count,
                relation_accepted_count=record.relation_accepted_count,
                relation_invalid_count=record.relation_invalid_count,
                relation_duplicate_count=record.relation_duplicate_count,
                relation_overflow_count=record.relation_overflow_count,
                relation_discarded_count=record.relation_discarded_count,
                presence_gap_policy_version=record.presence_gap_policy_version,
                presence_gap_sha256=record.presence_gap_sha256,
                presence_gap_ambient_objective=(record.presence_gap_ambient_objective),
                presence_gap_preferred_surface=(record.presence_gap_preferred_surface),
                presence_gap_anti_repetition_subject_count=(
                    record.presence_gap_anti_repetition_subject_count
                ),
                presence_gap_anti_repetition_system_count=(
                    record.presence_gap_anti_repetition_system_count
                ),
                foreground_evidence_policy_version=(
                    record.foreground_evidence_policy_version
                ),
                foreground_evidence_sha256=record.foreground_evidence_sha256,
                host_time_observed=record.host_time_observed,
                recent_exhaustive_evidence_count=(
                    record.recent_exhaustive_evidence_count
                ),
            )
            for record in _limited(records, limit)
        ]
        retention = self.enrichment_event_logs.retention(session_id)
        return AdminEnrichmentEventCollection(
            session_id=session_id,
            job_id=normalized_job_id,
            after_sequence=after_sequence,
            returned=len(events),
            events=events,
            retention=AdminEnrichmentEventRetention(
                capacity=retention.capacity,
                retained=retention.retained,
                evicted=retention.evicted,
                oldest_sequence=retention.oldest_sequence,
                latest_sequence=retention.latest_sequence,
            ),
        )

    def event_stream(self) -> AdminEventStreamSummary:
        return AdminEventStreamSummary(
            transport="server-sent-events",
            endpoint="/api/v0/events",
            event_type="log",
            cursor_parameter="after",
            last_sequence=self.logs.last_sequence,
        )

    def session_event_stream(
        self,
        session_id: str,
    ) -> AdminSessionEventStreamSummary:
        live_session = self._live_session(session_id)
        if live_session is None:
            raise AdminServiceError(
                "not_found",
                "session not found",
                details={"session_id": session_id},
            )
        return AdminSessionEventStreamSummary(
            session_id=session_id,
            transport="server-sent-events",
            endpoint=f"/api/v0/sessions/{session_id}/events",
            event_type="trace",
            cursor_parameter="after",
            last_sequence=self.trace_logs.last_sequence(session_id),
            live=True,
        )

    def live_sessions(
        self,
        *,
        tags: Sequence[object] | None = None,
        label: str | None = None,
        origin: str | None = None,
    ) -> list[AdminLiveSessionSummary]:
        sessions: list[AdminLiveSessionSummary] = []
        for registration in self.session_registry.list_sessions():
            with registration.lock:
                sessions.append(self._live_session_summary(registration.session))
        return [
            session
            for session in sessions
            if _summary_matches_filters(
                session,
                tags=tags,
                label=label,
                origin=origin,
            )
        ]

    def live_session_tag_groups(
        self,
        *,
        origin: str | None = None,
    ) -> list[AdminSessionTagGroup]:
        groups: dict[str, list[str]] = {}
        for session in self.live_sessions(origin=origin):
            for tag in session.tags:
                groups.setdefault(tag, []).append(session.session_id)
        return [
            AdminSessionTagGroup(
                tag=tag, count=len(session_ids), session_ids=session_ids
            )
            for tag, session_ids in sorted(groups.items())
        ]

    def live_session(self, session_id: str) -> AdminLiveSessionSummary:
        summary = self._live_session(session_id)
        if summary is not None:
            return summary
        raise AdminServiceError(
            "not_found",
            "session not found",
            details={"session_id": session_id},
        )

    def _live_session(self, session_id: str) -> AdminLiveSessionSummary | None:
        registration = self.session_registry.get(session_id)
        if registration is None:
            return None
        with registration.lock:
            return self._live_session_summary(registration.session)

    def _live_session_summary(self, session: SessionState) -> AdminLiveSessionSummary:
        metadata = session.environment_metadata.metadata
        return AdminLiveSessionSummary(
            session_id=session.langfuse_trace_id,
            username=session.username,
            login_username=session.login_username,
            hostname=session.host_identity.hostname,
            cwd=session.cwd,
            history_count=len(session.history),
            state_profile=self._state_profile_label(session.langfuse_trace_id),
            tags=list(session_tags_from_metadata(metadata)),
            label=session_label_from_metadata(metadata),
            origin=session_origin_from_metadata(metadata),
        )

    def live_session_state(self, session_id: str) -> AdminLiveSessionState:
        registration = self.session_registry.get(session_id)
        if registration is None:
            raise AdminServiceError(
                "not_found",
                "session not found",
                details={"session_id": session_id},
            )
        with registration.lock:
            return AdminLiveSessionState(
                session_id=session_id,
                state=session_state_for_level(registration.session, "exact"),
            )

    def create_mcp_session(
        self,
        *,
        username: str = "user",
        tags: Sequence[object] | None = None,
        label: str | None = None,
        metadata: dict[str, object] | None = None,
        state_profile: str | None = None,
        config_overrides: dict[str, object] | None = None,
        model: str | None = None,
        openai_service_tier: str | None = None,
    ) -> AdminMcpSessionCreateResult:
        if self.command_backend is None:
            raise AdminServiceError("unavailable", "command backend is not available")
        user = username.strip() if username else ""
        if not user:
            user = "user"
        normalized_tags = normalize_session_tags((MCP_SESSION_ORIGIN, *(tags or ())))
        profile = None
        if state_profile is not None:
            try:
                profile = self.state_profiles.load(state_profile)
            except StateProfileError as exc:
                raise AdminServiceError("not_found", "state profile not found") from exc
        runtime = None
        snapshot: dict[str, object] = {}
        if (
            self.evaluation_factory is not None
            or config_overrides is not None
            or model is not None
            or openai_service_tier is not None
        ):
            if self.evaluation_factory is None:
                raise AdminServiceError(
                    "unavailable", "evaluation overrides are not configured"
                )
            try:
                settings = resolve_mcp_session_config(
                    self.evaluation_factory.settings,
                    config_overrides,
                    model=model,
                    openai_service_tier=openai_service_tier,
                )
            except ValueError as exc:
                raise AdminServiceError("bad_request", str(exc)) from exc
            try:
                runtime = self.evaluation_factory.create(settings)
            except Exception as exc:
                LOGGER.warning(
                    "Evaluation runtime construction failed error=%s",
                    type(exc).__name__,
                )
                raise AdminServiceError(
                    "provider_error", "evaluation runtime could not be constructed"
                ) from exc
            snapshot = deepcopy(runtime.snapshot)
        backend = runtime.backend if runtime is not None else self.command_backend
        try:
            if profile is None:
                session = backend.create_session(
                    user, ConnectionMetadata(protocol=MCP_SESSION_ORIGIN)
                )
            else:
                session = backend.create_session(
                    user,
                    ConnectionMetadata(protocol=MCP_SESSION_ORIGIN),
                    state_profile=profile,
                )
        except Exception:
            if runtime is not None:
                runtime.close()
            raise
        self._mark_mcp_session(
            session,
            tags=normalized_tags,
            label=label,
            metadata=metadata,
        )
        if snapshot:
            session.environment_metadata.metadata[EVALUATION_CONFIG_KEY] = deepcopy(
                snapshot
            )
            LOGGER.info(
                "MCP evaluation session=%s config=%s",
                session.langfuse_trace_id,
                snapshot,
            )
            trace_evaluation_config(session.langfuse_trace_id, snapshot)
        try:
            if runtime is not None:
                runtime.enrichment.start()
            registration = self.session_registry.register(session, runtime=runtime)
        except Exception:
            self.session_registry.unregister(session.langfuse_trace_id, session=session)
            if runtime is not None:
                runtime.close()
            raise
        try:
            if self.record_session_start is not None:
                self.record_session_start(session)
        except Exception as exc:
            LOGGER.warning(
                "MCP session start recorder failed session=%s error=%s",
                session.langfuse_trace_id,
                type(exc).__name__,
            )
        LOGGER.info(
            "MCP session created session=%s user=%s tags=%s",
            registration.session_id,
            session.username,
            ",".join(normalized_tags) if normalized_tags else "none",
        )
        summary = self._live_session_summary(session)
        return AdminMcpSessionCreateResult(
            session_id=summary.session_id,
            username=summary.username,
            login_username=summary.login_username,
            hostname=summary.hostname,
            cwd=summary.cwd,
            state_profile=summary.state_profile,
            tags=summary.tags,
            label=summary.label,
            metadata=self._mcp_session_metadata(session),
            evaluation_config=snapshot,
        )

    def run_mcp_session_command(
        self,
        *,
        session_id: str,
        command: str,
    ) -> AdminMcpCommandResult:
        if self.command_backend is None:
            raise AdminServiceError("unavailable", "command backend is not available")
        if _is_forbidden_shell_console_command(command.strip()):
            raise AdminServiceError(
                "bad_request",
                "use explicit MCP resources or tools instead of !dreamland in shell",
            )
        registration = self.session_registry.get(session_id)
        if registration is None:
            raise AdminServiceError(
                "not_found",
                "session not found",
                details={"session_id": session_id},
            )

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def write_stdout(text: str) -> None:
            stdout_parts.append(text)
            trace_output(session_id, text, stream="stdout")

        def write_stderr(text: str) -> None:
            stderr_parts.append(text)
            trace_output(session_id, text, stream="stderr")

        with registration.lock:
            if ignores_blank_input(
                active_shell_frame(registration.session).interactive_program, command
            ):
                raise AdminServiceError("bad_request", "command must not be blank")
            backend = (
                registration.runtime.backend
                if registration.runtime is not None
                else self.command_backend
            )
            starting_history_count = len(registration.session.history)
            trace_input(session_id, command)
            writer = LineBufferedCommandWriter(
                stdout=write_stdout,
                stderr=write_stderr,
            )
            result = execute_backend_stream(
                backend,
                registration.session,
                command,
                writer,
            )
            writer.flush()
            history_count = len(registration.session.history)
            context = _mcp_command_context(
                registration.session,
                command_backend=backend,
            )
        rendered_stdout = "".join(stdout_parts)
        rendered_stderr = "".join(stderr_parts)
        history_index = (
            starting_history_count if history_count > starting_history_count else None
        )
        command_id = (
            registration.session.history[history_index].id
            if history_index is not None and history_index < history_count
            else None
        )
        return AdminMcpCommandResult(
            session_id=session_id,
            command_id=command_id,
            command=command,
            stdout=rendered_stdout,
            stderr=rendered_stderr,
            exit_status=result.exit_status,
            interrupted=result.interrupted,
            signal=result.signal,
            history_index=history_index,
            history_position=(history_index + 1 if history_index is not None else None),
            history_count=history_count,
            inspection_target="@-1" if history_index is not None else None,
            context=context,
        )

    def close_live_sessions(
        self,
        *,
        session_ids: Sequence[object] | None = None,
        tags: Sequence[object] | None = None,
        label: str | None = None,
        origin: str | None = None,
        dry_run: bool = False,
    ) -> AdminSessionCloseManyResult:
        requested_ids = _normalize_session_ids(session_ids)
        if not requested_ids and not _has_summary_filters(
            tags=tags,
            label=label,
            origin=origin,
        ):
            raise AdminServiceError(
                "bad_request",
                "provide at least one session id or filter before closing sessions",
            )

        missing: list[str] = []
        matches: list[AdminLiveSessionSummary] = []
        if requested_ids:
            for session_id in requested_ids:
                summary = self._live_session(session_id)
                if summary is None:
                    missing.append(session_id)
                    continue
                if _summary_matches_filters(
                    summary,
                    tags=tags,
                    label=label,
                    origin=origin,
                ):
                    matches.append(summary)
        else:
            matches = self.live_sessions(tags=tags, label=label, origin=origin)

        closed: list[str] = []
        if not dry_run:
            _log_live_session_close_risk(
                session_ids=requested_ids,
                tags=tags,
                label=label,
                origin=origin,
                matches=matches,
            )
            for summary in matches:
                registration = self.session_registry.get(summary.session_id)
                if registration is None:
                    continue
                self.session_registry.unregister(
                    summary.session_id,
                    session=registration.session,
                )
                closed.append(summary.session_id)
                LOGGER.info("Live session closed session=%s", summary.session_id)
        return AdminSessionCloseManyResult(
            closed=closed,
            matched=len(matches),
            missing=missing,
            dry_run=dry_run,
            filters=_session_close_filters(
                session_ids=requested_ids,
                tags=tags,
                label=label,
                origin=origin,
            ),
        )

    def sessions(
        self,
        *,
        limit: int,
        tags: Sequence[object] | None = None,
        label: str | None = None,
        origin: str | None = None,
    ) -> AdminSessionCollection:
        live_sessions = {
            session.session_id: session for session in self.live_sessions()
        }
        historical_sessions = []
        historical_total: int | None = None
        langfuse_available = self.introspection.available()
        if langfuse_available:
            try:
                historical = self.list_historical_sessions(limit=limit)
            except IntrospectionUnavailable:
                langfuse_available = False
            else:
                historical_sessions = historical.sessions
                historical_total = historical.total
        combined: list[AdminSessionSummary] = []
        seen: set[str] = set()
        for session in historical_sessions:
            live_session = live_sessions.get(session.id)
            seen.add(session.id)
            combined.append(
                _session_summary(
                    session_id=session.id,
                    live_session=live_session,
                    langfuse=True,
                    created_at=session.created_at,
                    environment=session.environment,
                    state_profile=session.state_profile,
                )
            )
        for session_id, live_session in live_sessions.items():
            if session_id in seen:
                continue
            combined.append(
                _session_summary(
                    session_id=session_id,
                    live_session=live_session,
                    langfuse=False,
                )
            )
        combined = [
            session
            for session in combined
            if _summary_matches_filters(
                session,
                tags=tags,
                label=label,
                origin=origin,
            )
        ]
        return AdminSessionCollection(
            sessions=combined,
            total=(
                len(combined)
                if _has_summary_filters(tags=tags, label=label, origin=origin)
                else historical_total
                if historical_total is not None
                else len(combined)
            ),
            langfuse_available=langfuse_available,
        )

    def session(
        self,
        session_id: str,
        *,
        historical_limit: int = 100,
    ) -> AdminSessionInspection:
        live_session = self._live_session(session_id)
        historical_session: SessionSummary | None = None
        historical_lookup_complete: bool | None = None
        langfuse_available = self.introspection.available()
        if langfuse_available:
            try:
                historical = self.list_historical_sessions(limit=historical_limit)
            except IntrospectionUnavailable:
                langfuse_available = False
            else:
                historical_lookup_complete = (
                    historical.total is None
                    or historical.total <= len(historical.sessions)
                )
                historical_session = next(
                    (
                        session
                        for session in historical.sessions
                        if session.id == session_id
                    ),
                    None,
                )

        if live_session is None and historical_session is None:
            raise AdminServiceError(
                "not_found",
                "session not found",
                details={
                    "session_id": session_id,
                    "langfuse_available": langfuse_available,
                    "historical_lookup_limit": historical_limit,
                    "historical_lookup_complete": historical_lookup_complete,
                },
            )

        summary = _session_summary(
            session_id=session_id,
            live_session=live_session,
            langfuse=historical_session is not None,
            created_at=(
                historical_session.created_at
                if historical_session is not None
                else None
            ),
            environment=(
                historical_session.environment
                if historical_session is not None
                else None
            ),
            state_profile=(
                historical_session.state_profile
                if historical_session is not None
                else None
            ),
        )
        return AdminSessionInspection(
            id=session_id,
            live=live_session is not None,
            langfuse=historical_session is not None,
            langfuse_available=langfuse_available,
            summary=summary,
            live_session=live_session,
            historical_session=historical_session,
            historical_lookup_limit=historical_limit,
            historical_lookup_complete=historical_lookup_complete,
            inspections={
                "live_state": live_session is not None,
                "live_trace": live_session is not None,
                "historical_commands": historical_session is not None,
                "historical_command_detail": historical_session is not None,
                "historical_state_delta": historical_session is not None,
            },
        )

    def list_historical_sessions(
        self,
        *,
        limit: int,
    ) -> SessionList:
        return self._with_state_profile_labels(
            self.introspection.list_sessions(limit=limit)
        )

    def list_historical_commands(
        self,
        *,
        session_id: str,
        limit: int,
        filter_text: str | None = None,
        sort: str = "time",
        descending: bool = True,
        include_io_summary: bool = True,
    ) -> list[CommandSummary]:
        return self.introspection.list_commands(
            session_id=session_id,
            limit=limit,
            filter_text=filter_text,
            sort=sort,
            descending=descending,
            include_io_summary=include_io_summary,
        )

    def inspect_command(
        self,
        *,
        session_id: str,
        target: str | None,
        limit: int,
        detail: str = "full",
    ) -> CommandInspection:
        return self.introspection.inspect_command(
            session_id=session_id,
            target=target,
            limit=limit,
            detail=detail,
        )

    def state_delta(
        self,
        *,
        session_id: str,
        from_target: str | None,
        to_target: str,
        limit: int,
        sections: Sequence[str] = (),
    ) -> StateDelta:
        return self.introspection.state_delta(
            session_id=session_id,
            from_target=from_target,
            to_target=to_target,
            limit=limit,
            sections=sections,
        )

    def list_state_profiles(self) -> list[AdminStateProfileSummary]:
        return [
            _state_profile_summary(summary) for summary in self.state_profiles.list()
        ]

    def default_state_profile(self) -> AdminDefaultStateProfile | None:
        profile = self.state_profile_runtime.current_profile()
        if profile is None:
            return None
        return AdminDefaultStateProfile(
            name=profile.name,
            level=profile.level,
            created_at=profile.created_at,
            source_session_id=profile.source_session_id,
            comment=profile.comment,
        )

    def state_profile(self, name: str) -> AdminStateProfileDetail:
        try:
            profile = self.state_profiles.load(name)
        except Exception as exc:
            raise AdminServiceError(
                "not_found",
                "state profile not found",
                details={"name": name},
            ) from exc
        return AdminStateProfileDetail(
            name=profile.name,
            level=profile.level,
            created_at=profile.created_at,
            source_session_id=profile.source_session_id,
            comment=profile.comment,
            state=profile.state,
        )

    def list_structured_failures(self) -> list[AdminStructuredFailureSummary]:
        return [
            AdminStructuredFailureSummary(
                id=summary.id,
                created_at=summary.created_at,
                session_id=summary.session_id,
                command_id=summary.command_id,
                command=summary.command,
                failure_class=summary.failure_class,
                parser_error=summary.parser_error,
                path=_path_text(summary.path),
            )
            for summary in self.structured_failures.list()
        ]

    def structured_failure(self, failure_id: str) -> AdminStructuredFailureDetail:
        try:
            capsule = self.structured_failures.load(failure_id)
            path = self.structured_failures.path_for(failure_id)
        except StructuredFailureError as exc:
            raise AdminServiceError(
                "not_found",
                "structured failure capsule not found",
                details={"id": failure_id},
            ) from exc
        return AdminStructuredFailureDetail(
            id=capsule.id,
            created_at=capsule.created_at,
            session_id=capsule.session_id,
            command_id=capsule.command_id,
            command=capsule.command,
            starting_cwd=capsule.starting_cwd,
            username=capsule.username,
            hostname=capsule.hostname,
            profile_name=capsule.profile_name,
            failure_class=capsule.failure_class,
            parser_error=capsule.parser_error,
            raw_response=capsule.raw_response,
            result={
                "stdout": capsule.result_stdout,
                "stderr": capsule.result_stderr,
                "exit_status": capsule.result_exit_status,
                "metadata": dict(capsule.result_metadata),
            },
            pre_command_state_profile=dict(capsule.pre_command_state_profile),
            path=_path_text(path),
        )

    def replay_structured_failure_parser(
        self,
        failure_id: str,
    ) -> AdminStructuredFailureReplay:
        try:
            capsule = self.structured_failures.load(failure_id)
        except StructuredFailureError as exc:
            raise AdminServiceError(
                "not_found",
                "structured failure capsule not found",
                details={"id": failure_id},
            ) from exc
        result = replay_structured_failure_parser(capsule)
        accepted = result.metadata.get("structured_response_accepted")
        return AdminStructuredFailureReplay(
            id=capsule.id,
            accepted=accepted is not False,
            failure_class=_optional_metadata_string(
                result.metadata,
                "dreamland.structured.failure_class",
            ),
            parser_error=_optional_metadata_string(
                result.metadata,
                "structured_parse_error",
            ),
            stdout=result.stdout,
            stderr=result.stderr,
            exit_status=result.exit_status,
            metadata=dict(result.metadata),
        )

    def save_state_profile(
        self,
        *,
        name: str,
        level: str,
        session_id: str,
        comment: str | None = None,
    ) -> AdminStateProfileSaveResult:
        if level not in STATE_PROFILE_LEVELS:
            raise AdminServiceError(
                "bad_request",
                "unsupported state profile level",
                details={"level": level},
            )
        registration = self.session_registry.get(session_id)
        if registration is None:
            raise AdminServiceError(
                "not_found",
                "session not found",
                details={"session_id": session_id},
            )
        try:
            with registration.lock:
                profile = create_state_profile(
                    registration.session,
                    name=name,
                    level=cast(StateProfileLevel, level),
                    comment=comment,
                )
            path = self.state_profiles.save(profile)
        except StateProfileError as exc:
            raise AdminServiceError("bad_request", str(exc)) from exc
        LOGGER.info(
            "State profile saved name=%s level=%s session=%s path=%s",
            profile.name,
            profile.level,
            session_id,
            path,
        )
        return AdminStateProfileSaveResult(
            name=profile.name,
            level=profile.level,
            session_id=session_id,
            path=str(path),
        )

    def apply_state_profile(
        self,
        *,
        name: str,
        session_id: str,
    ) -> AdminStateProfileApplyResult:
        registration = self.session_registry.get(session_id)
        if registration is None:
            raise AdminServiceError(
                "not_found",
                "session not found",
                details={"session_id": session_id},
            )
        try:
            profile = self.state_profiles.load(name)
        except StateProfileError as exc:
            raise AdminServiceError(
                "not_found",
                "state profile not found",
                details={"name": name},
            ) from exc
        with registration.lock:
            apply_state_profile_to_session(registration.session, profile)
        LOGGER.warning(
            "State profile applied name=%s level=%s session=%s source_session=%s",
            profile.name,
            profile.level,
            session_id,
            profile.source_session_id,
        )
        trace_state_profile_apply(
            session_id,
            name=profile.name,
            level=profile.level,
            source_session_id=profile.source_session_id,
        )
        try:
            self.state_profiles.record_session_profile(
                session_id=session_id,
                profile=profile,
                source="apply",
            )
        except Exception as exc:
            LOGGER.warning(
                "State profile apply index update failed session=%s error=%s",
                session_id,
                type(exc).__name__,
            )
        return AdminStateProfileApplyResult(
            name=profile.name,
            level=profile.level,
            session_id=session_id,
            source_session_id=profile.source_session_id,
        )

    def set_default_state_profile(self, name: str) -> AdminDefaultStateProfile:
        try:
            profile = self.state_profile_runtime.set_default(name)
        except StateProfileError as exc:
            raise AdminServiceError(
                "not_found",
                "state profile not found",
                details={"name": name},
            ) from exc
        LOGGER.warning(
            "State profile default set name=%s level=%s persist=false",
            profile.name,
            profile.level,
        )
        return AdminDefaultStateProfile(
            name=profile.name,
            level=profile.level,
            created_at=profile.created_at,
            source_session_id=profile.source_session_id,
            comment=profile.comment,
        )

    def clear_default_state_profile(self) -> AdminStateProfileClearDefaultResult:
        previous = self.state_profile_runtime.clear_default()
        LOGGER.warning(
            "State profile default cleared previous=%s persist=false",
            previous.name if previous is not None else None,
        )
        return AdminStateProfileClearDefaultResult(
            previous=(
                AdminDefaultStateProfile(
                    name=previous.name,
                    level=previous.level,
                    created_at=previous.created_at,
                    source_session_id=previous.source_session_id,
                    comment=previous.comment,
                )
                if previous is not None
                else None
            )
        )

    def delete_state_profile(self, name: str) -> AdminStateProfileDeleteResult:
        try:
            path, cleared_default = self.state_profile_runtime.delete_profile(name)
        except StateProfileError as exc:
            raise AdminServiceError(
                "not_found",
                "state profile not found",
                details={"name": name},
            ) from exc
        LOGGER.warning(
            "State profile deleted name=%s path=%s cleared_default=%s persist=false",
            name,
            path,
            cleared_default,
        )
        return AdminStateProfileDeleteResult(
            name=name,
            path=str(path),
            cleared_default=cleared_default,
        )

    def run_spiritguide(
        self,
        *,
        session_id: str,
        instruction: str,
        dry_run: bool = False,
    ) -> AdminSpiritguideResult:
        instruction = instruction.strip()
        if not instruction:
            raise AdminServiceError("bad_request", "spiritguide requires instruction")
        registration = self.session_registry.get(session_id)
        if registration is None:
            raise AdminServiceError(
                "not_found",
                "session not found",
                details={"session_id": session_id},
            )
        LOGGER.info(
            "Spiritguide requested session=%s dry_run=%s instruction=%r",
            session_id,
            dry_run,
            instruction,
        )
        applied = False
        try:
            with registration.lock:
                guide = (
                    registration.runtime.spiritguide
                    if registration.runtime is not None
                    else self.spiritguide
                )
                result = guide.respond(
                    SpiritguideContext(
                        instruction=instruction,
                        session_id=session_id,
                        session=registration.session,
                        dry_run=dry_run,
                    )
                )
                structured_response_accepted = result.metadata.get(
                    "structured_response_accepted"
                )
                state_update_rejected = structured_response_accepted is False
                if not dry_run and not state_update_rejected:
                    apply_external_state_update(
                        registration.session,
                        result.state_update,
                    )
                    applied = bool(_spiritguide_patch_keys(result))
        except Exception as exc:
            LOGGER.exception(
                "Spiritguide failed session=%s error=%s",
                session_id,
                type(exc).__name__,
            )
            raise AdminServiceError(
                "provider_error",
                "spiritguide provider error; instruction could not be completed",
            ) from exc
        LOGGER.info("Spiritguide completed session=%s dry_run=%s", session_id, dry_run)
        patch_keys = _spiritguide_patch_keys(result)
        trace_spiritguide(
            session_id,
            instruction=instruction,
            dry_run=dry_run,
            structured_response_accepted=result.metadata.get(
                "structured_response_accepted"
            ),
            patch_keys=patch_keys,
            applied=applied,
        )
        return AdminSpiritguideResult(
            session_id=session_id,
            message=result.message,
            dry_run=dry_run,
            applied=applied,
            structured_response_accepted=result.metadata.get(
                "structured_response_accepted"
            ),
            patch_keys=patch_keys,
            state_update=_spiritguide_state_update_to_dict(result),
            metadata=dict(result.metadata),
        )

    def create_spiritguide_profile(
        self,
        *,
        name: str,
        instruction: str,
        level: str = "world",
        dry_run: bool = False,
        comment: str | None = None,
        config_overrides: dict[str, object] | None = None,
    ) -> AdminSpiritguideProfileResult:
        guide = self._detached_spiritguide(config_overrides)
        try:
            result = create_spiritguide_profile(
                store=self.state_profiles,
                spiritguide=guide,
                name=name,
                instruction=instruction,
                level=level,
                dry_run=dry_run,
                comment=comment,
            )
        except StateProfileError as exc:
            raise AdminServiceError("bad_request", str(exc)) from exc
        except Exception as exc:
            LOGGER.exception(
                "Detached Spiritguide profile create failed name=%s error=%s",
                name,
                type(exc).__name__,
            )
            raise AdminServiceError(
                "provider_error",
                "spiritguide provider error; profile operation could not be completed",
            ) from exc
        return _admin_spiritguide_profile_result(result)

    def update_spiritguide_profile(
        self,
        *,
        name: str,
        instruction: str,
        level: str | None = None,
        dry_run: bool = False,
        comment: str | None = None,
        config_overrides: dict[str, object] | None = None,
    ) -> AdminSpiritguideProfileResult:
        guide = self._detached_spiritguide(config_overrides)
        try:
            result = update_spiritguide_profile(
                store=self.state_profiles,
                spiritguide=guide,
                name=name,
                instruction=instruction,
                level=level,
                dry_run=dry_run,
                comment=comment,
            )
        except StateProfileError as exc:
            raise AdminServiceError("bad_request", str(exc)) from exc
        except Exception as exc:
            LOGGER.exception(
                "Detached Spiritguide profile update failed name=%s error=%s",
                name,
                type(exc).__name__,
            )
            raise AdminServiceError(
                "provider_error",
                "spiritguide provider error; profile operation could not be completed",
            ) from exc
        return _admin_spiritguide_profile_result(result)

    def fork_spiritguide_profile(
        self,
        *,
        source_name: str,
        name: str,
        instruction: str | None = None,
        level: str | None = None,
        dry_run: bool = False,
        comment: str | None = None,
        config_overrides: dict[str, object] | None = None,
    ) -> AdminSpiritguideProfileResult:
        if config_overrides is not None and not (instruction or "").strip():
            raise AdminServiceError(
                "bad_request",
                "config_overrides requires a Spiritguide instruction when forking",
            )
        guide = self._detached_spiritguide(config_overrides)
        try:
            result = fork_spiritguide_profile(
                store=self.state_profiles,
                spiritguide=guide,
                source_name=source_name,
                name=name,
                instruction=instruction,
                level=level,
                dry_run=dry_run,
                comment=comment,
            )
        except StateProfileError as exc:
            raise AdminServiceError("bad_request", str(exc)) from exc
        except Exception as exc:
            LOGGER.exception(
                "Detached Spiritguide profile fork failed source=%s name=%s error=%s",
                source_name,
                name,
                type(exc).__name__,
            )
            raise AdminServiceError(
                "provider_error",
                "spiritguide provider error; profile operation could not be completed",
            ) from exc
        return _admin_spiritguide_profile_result(result)

    def _resolve_evaluation(
        self, overrides: object, *, detached: bool = False
    ) -> DreamlandSettings:
        if self.evaluation_factory is None:
            raise AdminServiceError(
                "unavailable", "evaluation overrides are not configured"
            )
        try:
            return self.evaluation_factory.resolve(overrides, detached=detached)
        except ValueError as exc:
            raise AdminServiceError("bad_request", str(exc)) from exc

    def _detached_spiritguide(self, overrides: dict[str, object] | None) -> Spiritguide:
        from dreamland.evaluation_runtime import RecordedSpiritguide

        if overrides is None:
            if self.evaluation_factory is None:
                return self.spiritguide
            return RecordedSpiritguide(
                self.spiritguide,
                evaluation_snapshot(self.evaluation_factory.settings, detached=True),
            )
        settings = self._resolve_evaluation(overrides, detached=True)
        assert self.evaluation_factory is not None
        try:
            return self.evaluation_factory.spiritguide(settings, detached=True)
        except Exception as exc:
            LOGGER.warning(
                "Evaluation Spiritguide construction failed error=%s",
                type(exc).__name__,
            )
            raise AdminServiceError(
                "provider_error", "evaluation Spiritguide could not be constructed"
            ) from exc

    def flush_introspection(self) -> None:
        self.introspection.flush()

    def shutdown(self) -> AdminShutdownResult:
        if not self.shutdown_available or self.request_shutdown_callback is None:
            raise AdminServiceError("unavailable", "shutdown is not available")
        self._shutdown_requested = True
        LOGGER.warning("Dreamland shutdown requested from admin API")
        self.request_shutdown_callback()
        return AdminShutdownResult(status="shutdown_requested")

    def _with_state_profile_labels(self, sessions: SessionList) -> SessionList:
        return SessionList(
            sessions=[
                replace(
                    session,
                    state_profile=(
                        self._state_profile_label(session.id) or session.state_profile
                    ),
                )
                for session in sessions.sessions
            ],
            total=sessions.total,
        )

    def _state_profile_label(self, session_id: str) -> str | None:
        registration = self.session_registry.get(session_id)
        if registration is not None:
            metadata = registration.session.environment_metadata.metadata
            name = metadata.get(STATE_PROFILE_METADATA_NAME)
            level = metadata.get(STATE_PROFILE_METADATA_LEVEL)
            if name is not None:
                return f"{name}:{level}" if level is not None else str(name)
        return _indexed_state_profile_label(self.state_profiles, session_id)

    def _mark_mcp_session(
        self,
        session: SessionState,
        *,
        tags: Sequence[str],
        label: str | None,
        metadata: dict[str, object] | None,
    ) -> None:
        current = dict(session.environment_metadata.metadata)
        for key in tuple(current):
            if key.startswith("dreamland.session."):
                del current[key]
        current[SESSION_ORIGIN_METADATA_KEY] = MCP_SESSION_ORIGIN
        current[SESSION_TAGS_METADATA_KEY] = list(tags)
        normalized_label = label.strip() if isinstance(label, str) else ""
        if normalized_label:
            current[SESSION_LABEL_METADATA_KEY] = normalized_label
        custom_metadata = _normalize_mcp_metadata(metadata)
        if custom_metadata:
            current[SESSION_METADATA_METADATA_KEY] = custom_metadata
        session.environment_metadata = EnvironmentMetadata(
            connection=session.environment_metadata.connection,
            metadata=current,
        )

    def _mcp_session_metadata(self, session: SessionState) -> dict[str, object]:
        metadata = session.environment_metadata.metadata.get(
            SESSION_METADATA_METADATA_KEY
        )
        return dict(metadata) if isinstance(metadata, dict) else {}


def _default_profile_label(profile: StateProfile | None) -> str | None:
    if profile is None:
        return None
    return f"{profile.name}:{profile.level}"


def _has_summary_filters(
    *,
    tags: Sequence[object] | None,
    label: str | None,
    origin: str | None,
) -> bool:
    return bool(
        normalize_session_tags(tags)
        or (label.strip() if isinstance(label, str) else "")
        or (origin.strip() if isinstance(origin, str) else "")
    )


def _normalize_session_ids(session_ids: Sequence[object] | None) -> list[str]:
    normalized: list[str] = []
    for value in session_ids or ():
        if value is None:
            continue
        session_id = str(value).strip()
        if session_id:
            normalized.append(session_id)
    return list(dict.fromkeys(normalized))


def _session_close_filters(
    *,
    session_ids: Sequence[str],
    tags: Sequence[object] | None,
    label: str | None,
    origin: str | None,
) -> dict[str, object]:
    filters: dict[str, object] = {}
    if session_ids:
        filters["session_ids"] = list(session_ids)
    normalized_tags = normalize_session_tags(tags)
    if normalized_tags:
        filters["tags"] = list(normalized_tags)
    normalized_label = label.strip() if isinstance(label, str) else ""
    if normalized_label:
        filters["label"] = normalized_label
    normalized_origin = origin.strip() if isinstance(origin, str) else ""
    if normalized_origin:
        filters["origin"] = normalized_origin
    return filters


def _log_live_session_close_risk(
    *,
    session_ids: Sequence[str],
    tags: Sequence[object] | None,
    label: str | None,
    origin: str | None,
    matches: Sequence[AdminLiveSessionSummary],
) -> None:
    filters = _session_close_filters(
        session_ids=session_ids,
        tags=tags,
        label=label,
        origin=origin,
    )
    non_id_filters = {
        key: value for key, value in filters.items() if key != "session_ids"
    }
    if not session_ids and non_id_filters:
        LOGGER.warning(
            "Filtered live session close requested matched=%s filters=%s "
            "session_ids=%s",
            len(matches),
            non_id_filters,
            [summary.session_id for summary in matches],
        )
    non_mcp_matches = [
        summary for summary in matches if summary.origin != MCP_SESSION_ORIGIN
    ]
    if non_mcp_matches:
        LOGGER.warning(
            "Non-MCP live session close requested matched=%s filters=%s sessions=%s",
            len(non_mcp_matches),
            filters,
            [
                {
                    "session_id": summary.session_id,
                    "origin": summary.origin,
                    "username": summary.username,
                    "label": summary.label,
                    "tags": summary.tags,
                }
                for summary in non_mcp_matches
            ],
        )


def _summary_matches_filters(
    summary: AdminLiveSessionSummary | AdminSessionSummary,
    *,
    tags: Sequence[object] | None,
    label: str | None,
    origin: str | None,
) -> bool:
    expected_tags = normalize_session_tags(tags)
    if expected_tags and not all(tag in summary.tags for tag in expected_tags):
        return False
    expected_label = label.strip() if isinstance(label, str) else ""
    if expected_label and summary.label != expected_label:
        return False
    expected_origin = origin.strip() if isinstance(origin, str) else ""
    if expected_origin and summary.origin != expected_origin:
        return False
    return True


def _is_forbidden_shell_console_command(command: str) -> bool:
    return command == "!dreamland" or command.startswith("!dreamland ")


def _normalize_mcp_metadata(
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise AdminServiceError("bad_request", "metadata must be an object")
    normalized: dict[str, object] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not key.strip():
            raise AdminServiceError(
                "bad_request",
                "metadata keys must be non-empty strings",
            )
        normalized[key.strip()] = value
    return normalized


def _indexed_state_profile_label(
    state_profiles: StateProfileStore,
    session_id: str,
) -> str | None:
    try:
        indexed = state_profiles.session_profile(session_id)
    except Exception:
        return None
    return indexed.label if indexed is not None else None


def _state_profile_summary(
    summary: StateProfileSummary,
) -> AdminStateProfileSummary:
    return AdminStateProfileSummary(
        name=summary.name,
        level=summary.level,
        created_at=summary.created_at,
        source_session_id=summary.source_session_id,
        comment=summary.comment,
        path=_path_text(summary.path),
    )


def _admin_spiritguide_profile_result(
    result: SpiritguideProfileOperationResult,
) -> AdminSpiritguideProfileResult:
    return AdminSpiritguideProfileResult(
        action=result.action,
        name=result.name,
        source_name=result.source_name,
        level=result.level,
        message=result.message,
        dry_run=result.dry_run,
        saved=result.saved,
        applied=result.applied,
        structured_response_accepted=result.structured_response_accepted,
        patch_keys=result.patch_keys,
        state_update=result.state_update,
        metadata=result.metadata,
        excluded_patch_keys=result.excluded_patch_keys,
        path=_path_text(result.path) if result.path is not None else None,
    )


def _mcp_command_context(
    session: SessionState,
    *,
    command_backend: CommandBackend,
) -> AdminMcpCommandContext:
    frame = active_shell_frame(session)
    program = frame.interactive_program.name or "bash"
    prompt = command_backend.prompt(session)
    context_name = "shell" if program in {"bash", "sh", "zsh"} else program
    label = (
        f"{context_name} on {frame.username}@{frame.host_identity.hostname}:{frame.cwd}"
    )
    return AdminMcpCommandContext(
        username=frame.username,
        hostname=frame.host_identity.hostname,
        cwd=frame.cwd,
        active_program=program,
        prompt=prompt,
        label=label,
    )


def _session_summary(
    *,
    session_id: str,
    live_session: AdminLiveSessionSummary | None,
    langfuse: bool,
    created_at: object = None,
    environment: str | None = None,
    state_profile: str | None = None,
) -> AdminSessionSummary:
    return AdminSessionSummary(
        id=session_id,
        live=live_session is not None,
        langfuse=langfuse,
        created_at=created_at,
        environment=environment,
        state_profile=(
            live_session.state_profile
            if live_session is not None and live_session.state_profile is not None
            else state_profile
        ),
        username=live_session.username if live_session is not None else None,
        login_username=(
            live_session.login_username if live_session is not None else None
        ),
        hostname=live_session.hostname if live_session is not None else None,
        cwd=live_session.cwd if live_session is not None else None,
        history_count=live_session.history_count if live_session is not None else None,
        tags=live_session.tags if live_session is not None else [],
        label=live_session.label if live_session is not None else None,
        origin=live_session.origin if live_session is not None else None,
    )


def _path_text(path: Path) -> str:
    return str(path)


def _optional_metadata_string(
    metadata: dict[str, object],
    key: str,
) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None


def _spiritguide_state_update_to_dict(
    result: SpiritguideResult,
) -> dict[str, object]:
    update = result.state_update
    state: dict[str, object] = {}
    if update.host_identity is not None:
        from dreamland.agent_state import host_identity_to_state

        state["host_identity"] = host_identity_to_state(update.host_identity)
    if update.cwd is not None:
        state["cwd"] = update.cwd
    if update.interactive_program is not None:
        from dreamland.agent_state import interactive_program_to_state

        state["interactive_program"] = interactive_program_to_state(
            update.interactive_program
        )
    if update.shell_environment is not None:
        from dreamland.agent_state import shell_environment_to_state

        state["shell_environment"] = shell_environment_to_state(
            update.shell_environment
        )
    if update.shell_stack is not None:
        from dreamland.agent_state import shell_frame_to_state

        state["shell_stack"] = [
            shell_frame_to_state(frame) for frame in update.shell_stack
        ]
    if update.shell_stack_transition is not None:
        state["shell_stack_transition"] = {
            "operation": update.shell_stack_transition.operation,
            "username": update.shell_stack_transition.username,
            "metadata": dict(update.shell_stack_transition.metadata),
        }
    if update.world_state is not None:
        state["world_state"] = update.world_state
    return state


def _spiritguide_patch_keys(result: SpiritguideResult) -> list[str]:
    return [
        name
        for name in (
            "host_identity",
            "cwd",
            "interactive_program",
            "shell_environment",
            "shell_stack",
            "shell_stack_transition",
            "world_state",
        )
        if getattr(result.state_update, name) is not None
    ]


def _limited(records: Sequence[_Record], limit: int) -> Sequence[_Record]:
    if limit <= 0:
        return ()
    return records[-limit:]
