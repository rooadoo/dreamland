"""Stable, compact runtime logging for enrichment decisions and timing."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

ENRICHMENT_LOGGER = logging.getLogger("dreamland.enrichment")

_RETAINED_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "caused_by_event_id",
        "correlation_id",
        "trigger_id",
        "enrichment_mode",
        "coordinator_role",
        "command_id",
        "target_host",
        "requested_surface",
        "selected_surface",
        "surface",
        "status",
        "reason",
        "reason_code",
        "candidate_id",
        "coalesced_into_candidate_id",
        "replaced_candidate_id",
        "retained_candidate_id",
        "completed_job_id",
        "higher_priority_trigger",
        "retained_trigger",
        "retained_enrichment_mode",
        "presence_probe_kind",
        "retained_presence_probe_kind",
        "subject_key_sha256",
        "retained_subject_key_sha256",
        "cooldown_scope_sha256",
        "focus_key_sha256",
        "match_policy",
        "match_classification",
        "command_focus_key_sha256",
        "retained_focus_key_sha256",
        "failure_kind",
        "failure_disposition",
        "provider",
        "provider_code",
        "role",
        "model",
        "circuit_origin",
        "authority_domain",
        "binding_reason",
        "policy_version",
        "integrator_refresh_outcome",
        "relation_policy_version",
        "presence_gap_policy_version",
        "presence_gap_sha256",
        "presence_gap_ambient_objective",
        "presence_gap_preferred_surface",
        "foreground_evidence_policy_version",
        "foreground_evidence_sha256",
        "action",
        "integrator_refresh_attempt",
        "integrator_refresh_latency_ms",
        "priority",
        "retained_priority",
        "pulse_count",
        "retained_pulse_count",
        "inflight",
        "mode_inflight",
        "inflight_before",
        "inflight_after",
        "mode_inflight_before",
        "mode_inflight_after",
        "max_inflight",
        "age_ms",
        "max_age_ms",
        "focus_path_count",
        "focus_artifact_count",
        "command_focus_path_count",
        "command_focus_artifact_count",
        "retained_focus_path_count",
        "retained_focus_artifact_count",
        "provider_status_code",
        "authority_dependency_count",
        "provider_circuit_count",
        "relation_limit",
        "relation_received_count",
        "relation_accepted_count",
        "relation_invalid_count",
        "relation_duplicate_count",
        "relation_overflow_count",
        "relation_discarded_count",
        "presence_gap_anti_repetition_subject_count",
        "presence_gap_anti_repetition_system_count",
        "recent_exhaustive_evidence_count",
        "accepted",
        "eligible",
        "changed",
        "host_time_observed",
        "foreground_authority_present",
        "foreground_authority_changed",
        "registry_targets",
    }
)

_RETAINED_INTEGER_FIELDS = frozenset(
    {
        "priority",
        "retained_priority",
        "pulse_count",
        "retained_pulse_count",
        "inflight",
        "mode_inflight",
        "inflight_before",
        "inflight_after",
        "mode_inflight_before",
        "mode_inflight_after",
        "max_inflight",
        "age_ms",
        "max_age_ms",
        "focus_path_count",
        "focus_artifact_count",
        "command_focus_path_count",
        "command_focus_artifact_count",
        "retained_focus_path_count",
        "retained_focus_artifact_count",
        "provider_status_code",
        "authority_dependency_count",
        "provider_circuit_count",
        "relation_limit",
        "relation_received_count",
        "relation_accepted_count",
        "relation_invalid_count",
        "relation_duplicate_count",
        "relation_overflow_count",
        "relation_discarded_count",
        "presence_gap_anti_repetition_subject_count",
        "presence_gap_anti_repetition_system_count",
        "recent_exhaustive_evidence_count",
        "integrator_refresh_attempt",
        "integrator_refresh_latency_ms",
    }
)
_RETAINED_BOOLEAN_FIELDS = frozenset(
    {
        "accepted",
        "eligible",
        "changed",
        "host_time_observed",
        "foreground_authority_present",
        "foreground_authority_changed",
    }
)


@dataclass(frozen=True, slots=True)
class EnrichmentLogContext:
    session_id: str
    job_id: str | None = None
    event_id: str | None = None
    caused_by_event_id: str | None = None
    correlation_id: str | None = None
    trigger_id: str | None = None
    enrichment_mode: str | None = None
    command_id: str | None = None
    target_host: str | None = None
    requested_surface: str | None = None
    selected_surface: str | None = None
    surface: str | None = None

    def fields(self) -> dict[str, object]:
        return {
            key: value
            for key, value in (
                ("session_id", self.session_id),
                ("job_id", self.job_id),
                ("event_id", self.event_id),
                ("caused_by_event_id", self.caused_by_event_id),
                ("correlation_id", self.correlation_id),
                ("trigger_id", self.trigger_id),
                ("enrichment_mode", self.enrichment_mode),
                ("command_id", self.command_id),
                ("target_host", self.target_host),
                ("requested_surface", self.requested_surface),
                ("selected_surface", self.selected_surface),
                ("surface", self.surface),
            )
            if value is not None
        }


def log_enrichment_event(
    event: str,
    *,
    level: int = logging.INFO,
    context: EnrichmentLogContext | None = None,
    **fields: object,
) -> None:
    """Emit one grep-friendly line with deterministic field ordering."""
    values: dict[str, object] = {}
    if context is not None:
        values.update(context.fields())
    values.update(fields)
    rendered = " ".join(
        f"{key}={_render_log_value(value)}"
        for key, value in values.items()
        if value is not None
    )
    message = f"event={event}"
    if rendered:
        message = f"{message} {rendered}"
    ENRICHMENT_LOGGER.log(
        level,
        message,
        extra={
            "dreamland_enrichment_event": event,
            "dreamland_session_id": (
                context.session_id if context is not None else None
            ),
            "dreamland_enrichment_job_id": (
                context.job_id if context is not None else None
            ),
            "dreamland_enrichment_fields": _retained_event_fields(values),
        },
    )


def _retained_event_fields(values: dict[str, object]) -> dict[str, object]:
    """Copy bounded operational fields without retaining model-authored prose."""

    retained: dict[str, object] = {}
    for key in _RETAINED_EVENT_FIELDS:
        value = values.get(key)
        if isinstance(value, str) and value:
            retained[key] = value
        elif (
            key in _RETAINED_INTEGER_FIELDS
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ):
            retained[key] = value
        elif key in _RETAINED_BOOLEAN_FIELDS and isinstance(value, bool):
            retained[key] = value
        elif key == "registry_targets" and isinstance(value, (list, tuple)):
            paths = tuple(
                tuple(path)
                for path in value
                if isinstance(path, (list, tuple))
                and bool(path)
                and all(isinstance(segment, str) for segment in path)
            )
            if paths:
                retained[key] = paths
    return retained


def _render_log_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    if isinstance(value, str):
        if value and all(
            character.isalnum() or character in "._:/-@" for character in value
        ):
            return value
        return json.dumps(value, ensure_ascii=False)
    return str(value)
