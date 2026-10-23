from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass

from dreamland.config import LoggingSettings

EVENT_LOGGER_PREFIX = "dreamland."
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_reply_excerpt_chars = 160


@dataclass(frozen=True, slots=True)
class BufferedLogRecord:
    sequence: int
    text: str


@dataclass(frozen=True, slots=True)
class SessionTraceRecord:
    sequence: int
    session_id: str
    text: str


@dataclass(frozen=True, slots=True)
class EnrichmentEventRecord:
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
class EnrichmentEventRetention:
    capacity: int
    retained: int
    evicted: int
    oldest_sequence: int | None
    latest_sequence: int | None


class LogRecordBuffer(logging.Handler):
    def __init__(self, capacity: int) -> None:
        super().__init__()
        self.capacity = capacity
        self._records: deque[BufferedLogRecord] = deque(maxlen=capacity)
        self._sequence = 0
        self._condition = threading.Condition()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._condition:
            self._sequence += 1
            self._records.append(BufferedLogRecord(self._sequence, text))
            self._condition.notify_all()

    def latest(self, count: int) -> tuple[BufferedLogRecord, ...]:
        with self._condition:
            if count == 0:
                return ()
            return tuple(self._records)[-count:]

    def after(
        self,
        sequence: int,
        *,
        timeout: float | None = None,
    ) -> tuple[BufferedLogRecord, ...]:
        with self._condition:
            if not any(record.sequence > sequence for record in self._records):
                self._condition.wait(timeout)
            return tuple(
                record for record in self._records if record.sequence > sequence
            )

    @property
    def last_sequence(self) -> int:
        with self._condition:
            return self._sequence


class SessionTraceLogBuffer(logging.Handler):
    def __init__(self, capacity: int) -> None:
        super().__init__()
        self.capacity = capacity
        self._records: dict[str, deque[SessionTraceRecord]] = {}
        self._sequence = 0
        self._condition = threading.Condition()

    def emit(self, record: logging.LogRecord) -> None:
        session_id = getattr(record, "dreamland_session_id", None)
        if not isinstance(session_id, str) or not session_id:
            return
        try:
            text = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._condition:
            self._sequence += 1
            records = self._records.setdefault(
                session_id,
                deque(maxlen=self.capacity),
            )
            records.append(SessionTraceRecord(self._sequence, session_id, text))
            self._condition.notify_all()

    def latest(self, session_id: str, count: int) -> tuple[SessionTraceRecord, ...]:
        with self._condition:
            if count == 0:
                return ()
            return tuple(self._records.get(session_id, ()))[-count:]

    def after(
        self,
        session_id: str,
        sequence: int,
        *,
        timeout: float | None = None,
    ) -> tuple[SessionTraceRecord, ...]:
        with self._condition:
            records = tuple(self._records.get(session_id, ()))
            if not any(record.sequence > sequence for record in records):
                self._condition.wait(timeout)
            return tuple(
                record
                for record in self._records.get(session_id, ())
                if record.sequence > sequence
            )

    def last_sequence(self, session_id: str) -> int:
        with self._condition:
            records = self._records.get(session_id)
            if not records:
                return 0
            return records[-1].sequence


class EnrichmentEventLogBuffer(logging.Handler):
    """Retain structured enrichment lifecycle events independently per session."""

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self.capacity = capacity
        self._records: dict[str, deque[EnrichmentEventRecord]] = {}
        self._evicted: dict[str, int] = {}
        self._sequence = 0
        self._condition = threading.Condition()

    def emit(self, record: logging.LogRecord) -> None:
        session_id = getattr(record, "dreamland_session_id", None)
        event = getattr(record, "dreamland_enrichment_event", None)
        if not isinstance(session_id, str) or not session_id:
            return
        if not isinstance(event, str) or not event:
            return
        job_id = getattr(record, "dreamland_enrichment_job_id", None)
        if not isinstance(job_id, str) or not job_id:
            job_id = None
        structured = getattr(record, "dreamland_enrichment_fields", None)
        if not isinstance(structured, dict):
            structured = {}
        try:
            text = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._condition:
            self._sequence += 1
            if self.capacity <= 0:
                self._evicted[session_id] = self._evicted.get(session_id, 0) + 1
                self._condition.notify_all()
                return
            records = self._records.setdefault(
                session_id,
                deque(maxlen=self.capacity),
            )
            if len(records) == self.capacity:
                self._evicted[session_id] = self._evicted.get(session_id, 0) + 1
            records.append(
                EnrichmentEventRecord(
                    sequence=self._sequence,
                    session_id=session_id,
                    job_id=job_id,
                    event=event,
                    level=record.levelname,
                    text=text,
                    event_id=_optional_event_text(structured, "event_id"),
                    caused_by_event_id=_optional_event_text(
                        structured,
                        "caused_by_event_id",
                    ),
                    correlation_id=_optional_event_text(
                        structured,
                        "correlation_id",
                    ),
                    trigger_id=_optional_event_text(structured, "trigger_id"),
                    enrichment_mode=_optional_event_text(
                        structured,
                        "enrichment_mode",
                    ),
                    coordinator_role=_optional_event_text(
                        structured,
                        "coordinator_role",
                    ),
                    command_id=_optional_event_text(structured, "command_id"),
                    target_host=_optional_event_text(structured, "target_host"),
                    requested_surface=_optional_event_text(
                        structured,
                        "requested_surface",
                    ),
                    selected_surface=_optional_event_text(
                        structured,
                        "selected_surface",
                    ),
                    surface=_optional_event_text(structured, "surface"),
                    status=_optional_event_text(structured, "status"),
                    reason=_optional_event_text(structured, "reason"),
                    reason_code=_optional_event_text(structured, "reason_code"),
                    candidate_id=_optional_event_text(structured, "candidate_id"),
                    coalesced_into_candidate_id=_optional_event_text(
                        structured,
                        "coalesced_into_candidate_id",
                    ),
                    replaced_candidate_id=_optional_event_text(
                        structured,
                        "replaced_candidate_id",
                    ),
                    retained_candidate_id=_optional_event_text(
                        structured,
                        "retained_candidate_id",
                    ),
                    completed_job_id=_optional_event_text(
                        structured,
                        "completed_job_id",
                    ),
                    higher_priority_trigger=_optional_event_text(
                        structured,
                        "higher_priority_trigger",
                    ),
                    retained_trigger=_optional_event_text(
                        structured,
                        "retained_trigger",
                    ),
                    retained_enrichment_mode=_optional_event_text(
                        structured,
                        "retained_enrichment_mode",
                    ),
                    presence_probe_kind=_optional_event_text(
                        structured,
                        "presence_probe_kind",
                    ),
                    retained_presence_probe_kind=_optional_event_text(
                        structured,
                        "retained_presence_probe_kind",
                    ),
                    subject_key_sha256=_optional_event_text(
                        structured,
                        "subject_key_sha256",
                    ),
                    retained_subject_key_sha256=_optional_event_text(
                        structured,
                        "retained_subject_key_sha256",
                    ),
                    cooldown_scope_sha256=_optional_event_text(
                        structured,
                        "cooldown_scope_sha256",
                    ),
                    focus_key_sha256=_optional_event_text(
                        structured,
                        "focus_key_sha256",
                    ),
                    match_policy=_optional_event_text(structured, "match_policy"),
                    match_classification=_optional_event_text(
                        structured,
                        "match_classification",
                    ),
                    command_focus_key_sha256=_optional_event_text(
                        structured,
                        "command_focus_key_sha256",
                    ),
                    retained_focus_key_sha256=_optional_event_text(
                        structured,
                        "retained_focus_key_sha256",
                    ),
                    failure_kind=_optional_event_text(structured, "failure_kind"),
                    failure_disposition=_optional_event_text(
                        structured,
                        "failure_disposition",
                    ),
                    provider=_optional_event_text(structured, "provider"),
                    provider_code=_optional_event_text(structured, "provider_code"),
                    role=_optional_event_text(structured, "role"),
                    model=_optional_event_text(structured, "model"),
                    circuit_origin=_optional_event_text(
                        structured,
                        "circuit_origin",
                    ),
                    authority_domain=_optional_event_text(
                        structured,
                        "authority_domain",
                    ),
                    binding_reason=_optional_event_text(
                        structured,
                        "binding_reason",
                    ),
                    policy_version=_optional_event_text(
                        structured,
                        "policy_version",
                    ),
                    action=_optional_event_text(structured, "action"),
                    integrator_refresh_outcome=_optional_event_text(
                        structured,
                        "integrator_refresh_outcome",
                    ),
                    integrator_refresh_attempt=_optional_event_integer(
                        structured,
                        "integrator_refresh_attempt",
                    ),
                    integrator_refresh_latency_ms=_optional_event_integer(
                        structured,
                        "integrator_refresh_latency_ms",
                    ),
                    priority=_optional_event_integer(structured, "priority"),
                    retained_priority=_optional_event_integer(
                        structured,
                        "retained_priority",
                    ),
                    pulse_count=_optional_event_integer(
                        structured,
                        "pulse_count",
                    ),
                    retained_pulse_count=_optional_event_integer(
                        structured,
                        "retained_pulse_count",
                    ),
                    inflight=_optional_event_integer(structured, "inflight"),
                    mode_inflight=_optional_event_integer(
                        structured,
                        "mode_inflight",
                    ),
                    inflight_before=_optional_event_integer(
                        structured,
                        "inflight_before",
                    ),
                    inflight_after=_optional_event_integer(
                        structured,
                        "inflight_after",
                    ),
                    mode_inflight_before=_optional_event_integer(
                        structured,
                        "mode_inflight_before",
                    ),
                    mode_inflight_after=_optional_event_integer(
                        structured,
                        "mode_inflight_after",
                    ),
                    max_inflight=_optional_event_integer(
                        structured,
                        "max_inflight",
                    ),
                    age_ms=_optional_event_integer(structured, "age_ms"),
                    max_age_ms=_optional_event_integer(structured, "max_age_ms"),
                    focus_path_count=_optional_event_integer(
                        structured,
                        "focus_path_count",
                    ),
                    focus_artifact_count=_optional_event_integer(
                        structured,
                        "focus_artifact_count",
                    ),
                    command_focus_path_count=_optional_event_integer(
                        structured,
                        "command_focus_path_count",
                    ),
                    command_focus_artifact_count=_optional_event_integer(
                        structured,
                        "command_focus_artifact_count",
                    ),
                    retained_focus_path_count=_optional_event_integer(
                        structured,
                        "retained_focus_path_count",
                    ),
                    retained_focus_artifact_count=_optional_event_integer(
                        structured,
                        "retained_focus_artifact_count",
                    ),
                    provider_status_code=_optional_event_integer(
                        structured,
                        "provider_status_code",
                    ),
                    authority_dependency_count=_optional_event_integer(
                        structured,
                        "authority_dependency_count",
                    ),
                    provider_circuit_count=_optional_event_integer(
                        structured,
                        "provider_circuit_count",
                    ),
                    accepted=_optional_event_boolean(structured, "accepted"),
                    eligible=_optional_event_boolean(structured, "eligible"),
                    changed=_optional_event_boolean(structured, "changed"),
                    foreground_authority_present=_optional_event_boolean(
                        structured,
                        "foreground_authority_present",
                    ),
                    foreground_authority_changed=_optional_event_boolean(
                        structured,
                        "foreground_authority_changed",
                    ),
                    registry_targets=_event_registry_targets(structured),
                    relation_policy_version=_optional_event_text(
                        structured,
                        "relation_policy_version",
                    ),
                    relation_limit=_optional_event_integer(
                        structured,
                        "relation_limit",
                    ),
                    relation_received_count=_optional_event_integer(
                        structured,
                        "relation_received_count",
                    ),
                    relation_accepted_count=_optional_event_integer(
                        structured,
                        "relation_accepted_count",
                    ),
                    relation_invalid_count=_optional_event_integer(
                        structured,
                        "relation_invalid_count",
                    ),
                    relation_duplicate_count=_optional_event_integer(
                        structured,
                        "relation_duplicate_count",
                    ),
                    relation_overflow_count=_optional_event_integer(
                        structured,
                        "relation_overflow_count",
                    ),
                    relation_discarded_count=_optional_event_integer(
                        structured,
                        "relation_discarded_count",
                    ),
                    presence_gap_policy_version=_optional_event_text(
                        structured,
                        "presence_gap_policy_version",
                    ),
                    presence_gap_sha256=_optional_event_text(
                        structured,
                        "presence_gap_sha256",
                    ),
                    presence_gap_ambient_objective=_optional_event_text(
                        structured,
                        "presence_gap_ambient_objective",
                    ),
                    presence_gap_preferred_surface=_optional_event_text(
                        structured,
                        "presence_gap_preferred_surface",
                    ),
                    presence_gap_anti_repetition_subject_count=(
                        _optional_event_integer(
                            structured,
                            "presence_gap_anti_repetition_subject_count",
                        )
                    ),
                    presence_gap_anti_repetition_system_count=(
                        _optional_event_integer(
                            structured,
                            "presence_gap_anti_repetition_system_count",
                        )
                    ),
                    foreground_evidence_policy_version=_optional_event_text(
                        structured,
                        "foreground_evidence_policy_version",
                    ),
                    foreground_evidence_sha256=_optional_event_text(
                        structured,
                        "foreground_evidence_sha256",
                    ),
                    host_time_observed=_optional_event_boolean(
                        structured,
                        "host_time_observed",
                    ),
                    recent_exhaustive_evidence_count=_optional_event_integer(
                        structured,
                        "recent_exhaustive_evidence_count",
                    ),
                )
            )
            self._condition.notify_all()

    def latest(
        self,
        session_id: str,
        count: int,
        *,
        job_id: str | None = None,
    ) -> tuple[EnrichmentEventRecord, ...]:
        with self._condition:
            if count <= 0:
                return ()
            matching = self._matching_records(session_id, job_id=job_id)
            return matching[-count:]

    def after(
        self,
        session_id: str,
        sequence: int,
        *,
        job_id: str | None = None,
        timeout: float | None = None,
    ) -> tuple[EnrichmentEventRecord, ...]:
        with self._condition:
            matching = tuple(
                record
                for record in self._matching_records(session_id, job_id=job_id)
                if record.sequence > sequence
            )
            if not matching:
                self._condition.wait(timeout)
                matching = tuple(
                    record
                    for record in self._matching_records(session_id, job_id=job_id)
                    if record.sequence > sequence
                )
            return matching

    def retention(self, session_id: str) -> EnrichmentEventRetention:
        with self._condition:
            records = tuple(self._records.get(session_id, ()))
            return EnrichmentEventRetention(
                capacity=self.capacity,
                retained=len(records),
                evicted=self._evicted.get(session_id, 0),
                oldest_sequence=records[0].sequence if records else None,
                latest_sequence=records[-1].sequence if records else None,
            )

    def _matching_records(
        self,
        session_id: str,
        *,
        job_id: str | None,
    ) -> tuple[EnrichmentEventRecord, ...]:
        records = tuple(self._records.get(session_id, ()))
        if job_id is None:
            return records
        return tuple(record for record in records if record.job_id == job_id)


def _optional_event_text(fields: dict[str, object], name: str) -> str | None:
    value = fields.get(name)
    return value if isinstance(value, str) and value else None


def _optional_event_integer(fields: dict[str, object], name: str) -> int | None:
    value = fields.get(name)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _optional_event_boolean(fields: dict[str, object], name: str) -> bool | None:
    value = fields.get(name)
    return value if isinstance(value, bool) else None


def _event_registry_targets(
    fields: dict[str, object],
) -> tuple[tuple[str, ...], ...]:
    value = fields.get("registry_targets")
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        tuple(path)
        for path in value
        if isinstance(path, (list, tuple))
        and bool(path)
        and all(isinstance(segment, str) and segment for segment in path)
    )


class EventTypeFilter(logging.Filter):
    def __init__(self, enabled_events: frozenset[str]) -> None:
        super().__init__()
        self.enabled_events = enabled_events

    def filter(self, record: logging.LogRecord) -> bool:
        event_type = _event_type(record.name)
        return event_type is None or event_type in self.enabled_events


def configure_console_logging(
    settings: LoggingSettings,
    *,
    stream: bool = True,
    log_capacity: int = 1000,
) -> LogRecordBuffer:
    global _reply_excerpt_chars

    _reply_excerpt_chars = settings.reply_excerpt_chars
    logger = logging.getLogger("dreamland")
    for handler in list(logger.handlers):
        if getattr(handler, "_dreamland_runtime_handler", False):
            logger.removeHandler(handler)

    log_buffer = LogRecordBuffer(log_capacity)
    log_buffer._dreamland_runtime_handler = True  # type: ignore[attr-defined]
    log_buffer.setFormatter(logging.Formatter(LOG_FORMAT))
    log_buffer.addFilter(EventTypeFilter(settings.events))

    if not settings.enabled:
        logger.setLevel(logging.CRITICAL + 1)
        return log_buffer

    logger.addHandler(log_buffer)
    if stream:
        handler = logging.StreamHandler()
        handler._dreamland_runtime_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(EventTypeFilter(settings.events))
        logger.addHandler(handler)
    logger.setLevel(settings.level)
    logger.propagate = False
    return log_buffer


TRACE_LOGGER_NAME = "dreamland.trace"
TRACE_LOGGER = logging.getLogger(TRACE_LOGGER_NAME)
TRACE_LOGGER.propagate = False
TRACE_LOGGER.addHandler(logging.NullHandler())
ENRICHMENT_LOGGER_NAME = "dreamland.enrichment"
MCP_SOURCE_LOGGER_NAME = "mcp.server"
MCP_RUNTIME_LOGGER_NAME = "dreamland.mcp"


class _ForwardToRuntimeLogger(logging.Handler):
    def __init__(self, target_logger_name: str) -> None:
        super().__init__()
        self.target_logger_name = target_logger_name

    def emit(self, record: logging.LogRecord) -> None:
        target = logging.getLogger(self.target_logger_name)
        if not target.isEnabledFor(record.levelno):
            return
        try:
            message = record.getMessage()
        except Exception:
            self.handleError(record)
            return
        target.log(record.levelno, message, exc_info=record.exc_info)


def configure_mcp_logging(
    *,
    root_handlers_before: tuple[logging.Handler, ...] | None = None,
    root_level_before: int | None = None,
) -> None:
    if root_handlers_before is not None:
        root_logger = logging.getLogger()
        before = set(root_handlers_before)
        for handler in list(root_logger.handlers):
            if handler not in before:
                root_logger.removeHandler(handler)
                handler.close()
        if root_level_before is not None:
            root_logger.setLevel(root_level_before)

    source_logger = logging.getLogger(MCP_SOURCE_LOGGER_NAME)
    for handler in list(source_logger.handlers):
        if getattr(handler, "_dreamland_mcp_runtime_handler", False):
            source_logger.removeHandler(handler)
    handler = _ForwardToRuntimeLogger(MCP_RUNTIME_LOGGER_NAME)
    handler._dreamland_mcp_runtime_handler = True  # type: ignore[attr-defined]
    source_logger.addHandler(handler)
    source_logger.setLevel(logging.INFO)
    source_logger.propagate = False


def configure_session_trace_logging(log_capacity: int = 1000) -> SessionTraceLogBuffer:
    logger = logging.getLogger(TRACE_LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, "_dreamland_session_trace_handler", False):
            logger.removeHandler(handler)

    trace_buffer = SessionTraceLogBuffer(log_capacity)
    trace_buffer._dreamland_session_trace_handler = True  # type: ignore[attr-defined]
    trace_buffer.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(trace_buffer)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return trace_buffer


def configure_enrichment_event_logging(
    settings: LoggingSettings,
    log_capacity: int = 1000,
) -> EnrichmentEventLogBuffer:
    """Configure process-local retention for structured enrichment events."""

    logger = logging.getLogger(ENRICHMENT_LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, "_dreamland_enrichment_event_handler", False):
            logger.removeHandler(handler)

    event_buffer = EnrichmentEventLogBuffer(log_capacity)
    event_buffer._dreamland_enrichment_event_handler = True  # type: ignore[attr-defined]
    event_buffer.setFormatter(logging.Formatter(LOG_FORMAT))
    event_buffer.setLevel(settings.level)
    event_buffer.addFilter(EventTypeFilter(settings.events))
    if settings.enabled:
        logger.addHandler(event_buffer)
    return event_buffer


def trace_input(session_id: str, text: str) -> None:
    _trace(session_id, "type=input text=%r", text)


def trace_output(session_id: str, text: str, *, stream: str = "stdout") -> None:
    _trace(session_id, "type=output stream=%s text=%r", stream, text)


def trace_reasoning_summary(session_id: str, command: str, summary: str) -> None:
    _trace(
        session_id,
        "type=reasoning_summary command=%r text=%r",
        command,
        summary,
    )


def trace_spiritguide(
    session_id: str,
    *,
    instruction: str,
    dry_run: bool,
    structured_response_accepted: object,
    patch_keys: list[str],
    applied: bool,
) -> None:
    accepted = (
        "accepted"
        if structured_response_accepted is True
        else "rejected"
        if structured_response_accepted is False
        else "not_reported"
    )
    _trace(
        session_id,
        (
            "type=spiritguide instruction=%r dry_run=%s "
            "structured_response=%s state_update=%s applied=%s"
        ),
        _trace_preview(instruction),
        str(dry_run).lower(),
        accepted,
        ",".join(patch_keys) if patch_keys else "none",
        str(applied).lower(),
    )


def trace_state_profile_apply(
    session_id: str,
    *,
    name: str,
    level: str,
    source_session_id: str | None,
) -> None:
    _trace(
        session_id,
        "type=state_profile_apply name=%r level=%s source_session=%s",
        name,
        level,
        source_session_id or "none",
    )


def trace_state_profile_boot(
    session_id: str,
    *,
    name: str,
    level: str,
    source_session_id: str | None,
) -> None:
    _trace(
        session_id,
        "type=state_profile_boot name=%r level=%s source_session=%s",
        name,
        level,
        source_session_id or "none",
    )


def trace_shell_stack_event(
    session_id: str,
    *,
    event: str,
    marker: str,
    command: str | None = None,
    previous_depth: int | None = None,
    next_depth: int | None = None,
    previous_host: str | None = None,
    next_host: str | None = None,
    previous_program: str | None = None,
    next_program: str | None = None,
    force_session_state_next_command: bool | None = None,
) -> None:
    _trace(
        session_id,
        (
            "type=shell_stack event=%s marker=%s command=%r "
            "previous_depth=%s next_depth=%s previous_host=%s next_host=%s "
            "previous_program=%s next_program=%s "
            "force_session_state_next_command=%s"
        ),
        event,
        marker,
        _trace_preview(command) if command is not None else "",
        _trace_value(previous_depth),
        _trace_value(next_depth),
        _trace_value(previous_host),
        _trace_value(next_host),
        _trace_value(previous_program),
        _trace_value(next_program),
        _trace_bool(force_session_state_next_command),
    )


def trace_evaluation_config(session_id: str, snapshot: dict[str, object]) -> None:
    _trace(
        session_id,
        "type=evaluation_config config=%s",
        json.dumps(snapshot, sort_keys=True),
    )


def trace_network_connection_event(
    session_id: str,
    *,
    event: str,
    connection_id: str,
    source_host: str | None,
    destination_host: str,
    source: dict[str, object],
    destination: dict[str, object],
    phase: str,
    peer_overrides: dict[str, object] | None = None,
) -> None:
    _trace(
        session_id,
        "type=network_connection event=%s connection=%s source_host=%s destination_host=%s source=%s destination=%s phase=%s peer_overrides=%s",
        event,
        connection_id,
        source_host,
        destination_host,
        json.dumps(source, sort_keys=True),
        json.dumps(destination, sort_keys=True),
        phase,
        json.dumps(peer_overrides or {}, sort_keys=True),
    )


def _trace(session_id: str, message: str, *args: object) -> None:
    if not session_id:
        return
    TRACE_LOGGER.info(
        message,
        *args,
        extra={"dreamland_session_id": session_id},
    )


def _trace_preview(value: str, *, limit: int = 120) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def _trace_value(value: object | None) -> str:
    if value is None:
        return "none"
    return str(value)


def _trace_bool(value: bool | None) -> str:
    if value is None:
        return "none"
    return str(value).lower()


def reply_excerpt(stdout: str, stderr: str) -> str:
    text = stdout or stderr
    if not text or _reply_excerpt_chars == 0:
        return ""
    first_line = text.splitlines()[0] if text.splitlines() else text
    if len(first_line) <= _reply_excerpt_chars:
        return first_line
    return f"{first_line[:_reply_excerpt_chars]}..."


def _event_type(logger_name: str) -> str | None:
    if not logger_name.startswith(EVENT_LOGGER_PREFIX):
        return None
    event_type = logger_name.removeprefix(EVENT_LOGGER_PREFIX).split(".", maxsplit=1)[0]
    if event_type in {
        "lifecycle",
        "commands",
        "replies",
        "tokens",
        "console",
        "failures",
        "mcp",
        "enrichment",
        "trace",
    }:
        return event_type
    return None
