# Admin API V0 Contract

Status: Draft
Audience: UI developers, coding agents, DREAMLAND maintainers, and operators
Purpose: define the concrete JSON, query, and event shapes for the local
`/api/v0` admin API.

## Overview

The admin API is an optional local HTTP interface for inspecting and controlling
a running DREAMLAND process. It is enabled at process startup through `[api]`
configuration and is protected by bearer token authentication.

This document records the current `/api/v0` contract for UI and agent clients.
The API is still pre-production, so breaking changes are acceptable when this
document and `docs/ADMIN_INTERFACE.md` are updated in the same change.

## Details

### General Rules

All REST endpoints are rooted under `/api/v0`.

Every request requires:

```http
Authorization: Bearer <token>
```

Responses are JSON unless the endpoint is a server-sent event stream.
Datetimes are serialized as ISO 8601 strings. Fields marked nullable may be
`null` when DREAMLAND does not have that data or when Langfuse did not return
it. Object fields named `state`, `metadata`, `trace_input`, `trace_output`,
`trace_metadata`, `before_state`, and `after_state` are intentionally
open-ended JSON values because they mirror simulated session state or provider
payloads.

Integer query parameters must be non-negative integers. Boolean query
parameters accept `true`, `false`, `1`, `0`, `yes`, `no`, `on`, and `off`.
Unknown query parameters are ignored. Repeated query parameters use the first
value except `section`, which may be repeated on `state-delta`.

The current API has default limits but does not enforce hard maximum limits.
Clients should still use bounded `limit` values. The default list limit is `20`
unless an endpoint says otherwise. Historical session lookup defaults to `100`.
List endpoints currently do not support offset or page cursors. Logs, traces,
retained enrichment events, and SSE streams use `after` sequence cursors;
historical sessions and commands return a bounded first/latest set from their
backing stores.

### Error Envelope

Errors use a stable JSON envelope:

```json
{
  "error": {
    "code": "not_found",
    "message": "endpoint not found",
    "details": {}
  }
}
```

Common codes:

- `unauthorized`: bearer token is missing or invalid.
- `forbidden`: MCP origin is not allowed.
- `bad_request`: request body or query parameter is invalid.
- `not_found`: endpoint, session, profile, or command was not found.
- `method_not_allowed`: HTTP method is not supported.
- `unavailable`: Langfuse introspection or another optional capability is not
  available.

### Shared Schemas

`AdminCapabilitySummary`:

```json
{
  "logs": true,
  "traces": true,
  "live_sessions": true,
  "live_session_state": true,
  "historical_introspection": true,
  "state_profiles": true,
  "spiritguide": true,
  "shutdown": true,
  "mcp_write_tools": false
}
```

`AdminRuntimeStatus`:

```json
{
  "live_session_count": 1,
  "introspection_available": true,
  "default_state_profile": "bank:system",
  "shutdown_requested": false
}
```

`AdminLogEntry`:

```json
{
  "sequence": 42,
  "text": "Console logging requested count=20 follow=false"
}
```

`AdminTraceEntry`:

```json
{
  "sequence": 7,
  "session_id": "8af27532247d4f34ba45a7356f287fe4",
  "text": "user=user cwd=/home/user command='whoami'"
}
```

`AdminEnrichmentEventCollection`:

```json
{
  "session_id": "8af27532247d4f34ba45a7356f287fe4",
  "job_id": "0123456789abcdef0123456789abcdef",
  "after_sequence": 120,
  "returned": 1,
  "events": [
    {
      "sequence": 121,
      "session_id": "8af27532247d4f34ba45a7356f287fe4",
      "job_id": "0123456789abcdef0123456789abcdef",
      "event": "job.completed",
      "level": "INFO",
      "text": "2026-08-02 12:00:00,000 INFO dreamland.enrichment: event=job.completed ...",
      "event_id": "89abcdef0123456789abcdef01234567",
      "caused_by_event_id": null,
      "correlation_id": "89abcdef0123456789abcdef01234567",
      "trigger_id": "manual",
      "enrichment_mode": "ambient_presence",
      "coordinator_role": "ambient_coordinator",
      "command_id": null,
      "target_host": "ubuntu",
      "requested_surface": "process",
      "selected_surface": "process",
      "surface": "process",
      "status": "applied",
      "reason": "applied",
      "reason_code": null,
      "candidate_id": null,
      "coalesced_into_candidate_id": null,
      "replaced_candidate_id": null,
      "retained_candidate_id": null,
      "completed_job_id": null,
      "higher_priority_trigger": null,
      "retained_trigger": null,
      "retained_enrichment_mode": null,
      "presence_probe_kind": null,
      "retained_presence_probe_kind": null,
      "subject_key_sha256": null,
      "retained_subject_key_sha256": null,
      "cooldown_scope_sha256": null,
      "focus_key_sha256": null,
      "match_policy": null,
      "match_classification": null,
      "command_focus_key_sha256": null,
      "retained_focus_key_sha256": null,
      "failure_kind": null,
      "failure_disposition": null,
      "provider": null,
      "provider_code": null,
      "role": null,
      "model": null,
      "circuit_origin": null,
      "authority_domain": null,
      "binding_reason": null,
      "policy_version": null,
      "integrator_refresh_outcome": null,
      "relation_policy_version": null,
      "action": null,
      "integrator_refresh_attempt": null,
      "integrator_refresh_latency_ms": null,
      "priority": null,
      "retained_priority": null,
      "pulse_count": null,
      "retained_pulse_count": null,
      "inflight": null,
      "mode_inflight": null,
      "inflight_before": null,
      "inflight_after": null,
      "mode_inflight_before": null,
      "mode_inflight_after": null,
      "max_inflight": null,
      "age_ms": null,
      "max_age_ms": null,
      "focus_path_count": null,
      "focus_artifact_count": null,
      "command_focus_path_count": null,
      "command_focus_artifact_count": null,
      "retained_focus_path_count": null,
      "retained_focus_artifact_count": null,
      "provider_status_code": null,
      "authority_dependency_count": null,
      "provider_circuit_count": null,
      "relation_limit": null,
      "relation_received_count": null,
      "relation_accepted_count": null,
      "relation_invalid_count": null,
      "relation_duplicate_count": null,
      "relation_overflow_count": null,
      "relation_discarded_count": null,
      "presence_gap_policy_version": null,
      "presence_gap_sha256": null,
      "presence_gap_ambient_objective": null,
      "presence_gap_preferred_surface": null,
      "presence_gap_anti_repetition_subject_count": null,
      "presence_gap_anti_repetition_system_count": null,
      "foreground_evidence_policy_version": null,
      "foreground_evidence_sha256": null,
      "host_time_observed": null,
      "recent_exhaustive_evidence_count": null,
      "accepted": null,
      "eligible": null,
      "changed": null,
      "foreground_authority_present": null,
      "foreground_authority_changed": null,
      "registry_targets": []
    }
  ],
  "retention": {
    "capacity": 10000,
    "retained": 83,
    "evicted": 4,
    "oldest_sequence": 39,
    "latest_sequence": 121,
    "storage": "process_memory",
    "restart_durable": false
  }
}
```

Sequence values are process-local. `retained` and `evicted` describe the whole
session buffer even when `job_id` filters the returned event list. Records do
not require a currently live session and disappear on process restart.

`AdminLiveSessionSummary`:

```json
{
  "session_id": "8af27532247d4f34ba45a7356f287fe4",
  "username": "user",
  "login_username": "user",
  "hostname": "rpi-admin01",
  "cwd": "/home/user",
  "history_count": 3,
  "state_profile": "bank:system",
  "tags": ["mcp", "scenario:login"],
  "label": "agent-run-1",
  "origin": "mcp"
}
```

`AdminSessionSummary`:

```json
{
  "id": "8af27532247d4f34ba45a7356f287fe4",
  "live": true,
  "langfuse": true,
  "created_at": "2026-06-22T14:34:32.288000+00:00",
  "environment": "default",
  "state_profile": "bank:system",
  "username": "user",
  "login_username": "user",
  "hostname": "rpi-admin01",
  "cwd": "/home/user",
  "history_count": 3,
  "tags": ["mcp", "scenario:login"],
  "label": "agent-run-1",
  "origin": "mcp"
}
```

For live-only sessions, `langfuse` is `false` and Langfuse-derived fields may
be `null`. For historical-only sessions, `live` is `false` and live fields may
be `null`.

`AdminSessionCollection`:

```json
{
  "sessions": [
    {
      "id": "8af27532247d4f34ba45a7356f287fe4",
      "live": true,
      "langfuse": true,
      "created_at": "2026-06-22T14:34:32.288000+00:00",
      "environment": "default",
      "state_profile": "bank:system",
      "username": "user",
      "login_username": "user",
      "hostname": "rpi-admin01",
      "cwd": "/home/user",
      "history_count": 3,
      "tags": ["mcp", "scenario:login"],
      "label": "agent-run-1",
      "origin": "mcp"
    }
  ],
  "total": 154,
  "langfuse_available": true
}
```

`AdminSessionInspection`:

```json
{
  "id": "8af27532247d4f34ba45a7356f287fe4",
  "live": true,
  "langfuse": true,
  "langfuse_available": true,
  "summary": {
    "id": "8af27532247d4f34ba45a7356f287fe4",
    "live": true,
    "langfuse": true,
    "created_at": "2026-06-22T14:34:32.288000+00:00",
    "environment": "default",
    "state_profile": "bank:system",
    "username": "user",
    "login_username": "user",
    "hostname": "rpi-admin01",
    "cwd": "/home/user",
    "history_count": 3,
    "tags": ["mcp", "scenario:login"],
    "label": "agent-run-1",
    "origin": "mcp"
  },
  "inspections": {
    "live_state": true,
    "live_trace": true,
    "historical_commands": true,
    "historical_command_detail": true,
    "historical_state_delta": true
  },
  "live_session": {
    "session_id": "8af27532247d4f34ba45a7356f287fe4",
    "username": "user",
    "login_username": "user",
    "hostname": "rpi-admin01",
    "cwd": "/home/user",
    "history_count": 3,
    "state_profile": "bank:system",
    "tags": ["mcp", "scenario:login"],
    "label": "agent-run-1",
    "origin": "mcp"
  },
  "historical_session": {
    "id": "8af27532247d4f34ba45a7356f287fe4",
    "created_at": "2026-06-22T14:34:32.288000+00:00",
    "environment": "default",
    "state_profile": "bank:system"
  },
  "historical_lookup_limit": 100,
  "historical_lookup_complete": false
}
```

`historical_lookup_complete` is `false` when the historical backend reports a
larger total than the lookup window. A session may exist beyond the lookup
window even if this endpoint returns `404`.

`AdminLiveSessionState`:

```json
{
  "session_id": "8af27532247d4f34ba45a7356f287fe4",
  "state": {
    "username": "user",
    "cwd": "/home/user"
  }
}
```

`CommandSummary`:

```json
{
  "id": "d348879954a6725c",
  "observation_id": "obs-1",
  "session_id": "8af27532247d4f34ba45a7356f287fe4",
  "timestamp": "2026-06-22T14:35:01.000000+00:00",
  "command": "whoami",
  "status": "success",
  "model": "gpt-5.5",
  "input_tokens": 120,
  "output_tokens": 40,
  "total_tokens": 160,
  "total_cost": 0.00042,
  "latency": 1.25,
  "reasoning": false,
  "summary": true
}
```

`id` is DREAMLAND's direct command id when available. Older historical
observations may use the Langfuse observation id as a fallback. `observation_id`
is the underlying Langfuse observation id and is included for telemetry
correlation. Langfuse-derived command cost, latency, model, token, reasoning,
and summary fields are best-effort. They may be `null` or `false` when
unavailable.

`ObservationSummary`:

```json
{
  "id": "obs-1",
  "type": "generation",
  "name": "dreamland-command-agent",
  "input": {},
  "output": {},
  "metadata": {},
  "model": "gpt-5.5",
  "usage": {
    "input": 120,
    "output": 40,
    "total": 160
  },
  "total_cost": 0.00042,
  "latency": 1.25
}
```

`CommandInspection`:

```json
{
  "detail": "full",
  "command": {
    "id": "d348879954a6725c",
    "session_id": "8af27532247d4f34ba45a7356f287fe4",
    "timestamp": "2026-06-22T14:35:01.000000+00:00",
    "command": "whoami",
    "status": "success",
    "model": "gpt-5.5",
    "input_tokens": 120,
    "output_tokens": 40,
    "total_tokens": 160,
    "total_cost": 0.00042,
    "latency": 1.25,
    "reasoning": false,
    "summary": true
  },
  "trace_input": {},
  "trace_output": {},
  "trace_metadata": {},
  "reasoning_summary": null,
  "observations": []
}
```

`StateDelta`:

```json
{
  "from_label": "@-2",
  "to_label": "@-1",
  "before_state": {},
  "after_state": {},
  "sections": [
    "environment"
  ]
}
```

`AdminStateProfileSummary`:

```json
{
  "name": "bank",
  "level": "system",
  "created_at": "2026-06-17T06:39:00+00:00",
  "source_session_id": "01b343b36ba849c9bcf11774539e46f2",
  "comment": "basic banking network",
  "path": "state_profiles/bank.json"
}
```

The current API exposes `path` for operator troubleshooting. UI clients should
treat it as an implementation detail and avoid making workflows depend on it.

`AdminDefaultStateProfile`:

```json
{
  "name": "bank",
  "level": "system",
  "created_at": "2026-06-17T06:39:00+00:00",
  "source_session_id": "01b343b36ba849c9bcf11774539e46f2",
  "comment": "basic banking network"
}
```

`AdminStateProfileDetail`:

```json
{
  "name": "bank",
  "level": "system",
  "created_at": "2026-06-17T06:39:00+00:00",
  "source_session_id": "01b343b36ba849c9bcf11774539e46f2",
  "comment": "basic banking network",
  "state": {}
}
```

State profile levels are:

- `exact`: restore a narrow exact session state.
- `system`: restore the current simulated system.
- `world`: restore broader world/network context.

`AdminStateProfileSaveResult`:

```json
{
  "name": "lab",
  "level": "system",
  "session_id": "8af27532247d4f34ba45a7356f287fe4",
  "path": "state_profiles/lab.json"
}
```

`AdminStateProfileApplyResult`:

```json
{
  "name": "lab",
  "level": "system",
  "session_id": "8af27532247d4f34ba45a7356f287fe4",
  "source_session_id": "01b343b36ba849c9bcf11774539e46f2"
}
```

`AdminStateProfileDeleteResult`:

```json
{
  "name": "lab",
  "path": "state_profiles/lab.json",
  "cleared_default": false
}
```

`AdminStateProfileClearDefaultResult`:

```json
{
  "previous": {
    "name": "bank",
    "level": "system",
    "created_at": "2026-06-17T06:39:00+00:00",
    "source_session_id": "01b343b36ba849c9bcf11774539e46f2",
    "comment": "basic banking network"
  }
}
```

`previous` is `null` when no default profile was set.

`AdminSpiritguideResult`:

```json
{
  "session_id": "8af27532247d4f34ba45a7356f287fe4",
  "message": "Changed cwd.",
  "dry_run": false,
  "applied": true,
  "structured_response_accepted": true,
  "patch_keys": [
    "cwd"
  ],
  "state_update": {
    "cwd": "/srv/app"
  },
  "metadata": {
    "structured_response_accepted": true
  }
}
```

`AdminSpiritguideProfileResult`:

```json
{
  "action": "create",
  "name": "factory-lab",
  "source_name": null,
  "level": "world",
  "message": "Factory world created.",
  "dry_run": false,
  "saved": true,
  "applied": true,
  "structured_response_accepted": true,
  "patch_keys": [
    "world_state"
  ],
  "state_update": {
    "world_state": {
      "hosts_by_name": {
        "ubuntu": {
          "services_by_name": {
            "plc-gateway": {
              "port": 502
            }
          }
        }
      }
    }
  },
  "metadata": {
    "structured_response_accepted": true
  },
  "excluded_patch_keys": [],
  "path": "state_profiles/factory-lab.json"
}
```

`excluded_patch_keys` lists valid Spiritguide state-update sections that were
not persisted by the selected profile level. For example, `world` profiles
preserve `world_state` but exclude session-specific fields such as `cwd` and
`shell_stack`.

`AdminShutdownResult`:

```json
{
  "status": "shutdown_requested"
}
```

### Endpoints

#### `GET /api/v0/health`

Query parameters: none.

Response:

```json
{
  "status": "ok",
  "api_version": "v0"
}
```

#### `GET /api/v0/status`

Query parameters: none.

Response: `AdminRuntimeStatus`.

#### `GET /api/v0/capabilities`

Query parameters: none.

Response: `AdminCapabilitySummary`.

#### `GET /api/v0/config/effective`

Query parameters: none.

Response: redacted effective configuration. Secrets are represented as booleans
or sanitized values. The exact sections follow the loaded DREAMLAND
configuration and may grow as configuration grows.

#### `GET /api/v0/logs`

Query parameters:

- `limit`: integer, default `20`.
- `after`: optional integer cursor. When present, returns records with sequence
  greater than `after`; otherwise returns the latest records.

Response: array of `AdminLogEntry`.

#### `GET /api/v0/sessions/{session_id}/enrichment-events`

Query parameters:

- `job_id`: optional exact job id. Omit it to include session-level events and
  every retained job.
- `limit`: integer, default `20`.
- `after`: optional process-local sequence cursor. When present, returns
  records with sequence greater than `after`; otherwise returns the latest
  records.

Response: `AdminEnrichmentEventCollection`. This is a process-memory diagnostic
read, not a live-session lookup, so an unknown or expired session id returns an
empty collection rather than `404`.

Each event retains the unchanged formatted `text` and exposes these bounded
fields directly when available: `event_id`, `caused_by_event_id`,
`correlation_id`, `trigger_id`, `enrichment_mode`, `coordinator_role`,
`command_id`, `target_host`,
`requested_surface`, `selected_surface`, `surface`, `status`, `reason`,
`reason_code`, `candidate_id`, `coalesced_into_candidate_id`,
`replaced_candidate_id`, `retained_candidate_id`, `completed_job_id`,
`higher_priority_trigger`, `retained_trigger`, `retained_enrichment_mode`,
`presence_probe_kind`, `retained_presence_probe_kind`, `subject_key_sha256`,
`retained_subject_key_sha256`, `cooldown_scope_sha256`, `focus_key_sha256`,
`priority`,
`match_policy`, `match_classification`, `command_focus_key_sha256`,
`retained_focus_key_sha256`, `failure_kind`, `failure_disposition`, `provider`,
`provider_code`, `role`, `model`, `circuit_origin`, `authority_domain`,
`binding_reason`, `policy_version`, `integrator_refresh_outcome`,
`relation_policy_version`, `action`, `integrator_refresh_attempt`,
`integrator_refresh_latency_ms`,
`retained_priority`, `pulse_count`, `retained_pulse_count`, `inflight`,
`mode_inflight`, `inflight_before`, `inflight_after`, `mode_inflight_before`,
`mode_inflight_after`, `max_inflight`, `age_ms`, `max_age_ms`,
`focus_path_count`,
`focus_artifact_count`, `command_focus_path_count`,
`command_focus_artifact_count`,
`retained_focus_path_count`, `retained_focus_artifact_count`,
`provider_status_code`, `authority_dependency_count`,
`provider_circuit_count`, `relation_limit`, `relation_received_count`,
`relation_accepted_count`, `relation_invalid_count`,
`relation_duplicate_count`, `relation_overflow_count`,
`relation_discarded_count`, `presence_gap_policy_version`,
`presence_gap_sha256`, `presence_gap_ambient_objective`,
`presence_gap_preferred_surface`,
`presence_gap_anti_repetition_subject_count`,
`presence_gap_anti_repetition_system_count`,
`foreground_evidence_policy_version`, `foreground_evidence_sha256`,
`host_time_observed`, `recent_exhaustive_evidence_count`, `accepted`,
`eligible`, `changed`,
`foreground_authority_present`, `foreground_authority_changed`, and
`registry_targets`. Missing scalar fields are `null`; missing registry targets
are `[]`. Registry targets are
host-relative path arrays, for example
`[["filesystem","artifacts_by_path"]]`. Focus SHA fields are digests of
bounded focus keys, not foreground content. `interaction-focus-match-v1` uses
`artifact_exact`, `path_related`, or `surface_only`; the last is coarse
same-host/surface evidence, not proof that an enriched artifact was rendered.
Authority fields expose only policy and decision metadata, never the bounded
foreground payload or its hidden fingerprint. Provider fields expose bounded
classification and safe machine identifiers, never provider-authored error
prose or response bodies. `circuit_origin`, when present, is
`foreground_command` or `enrichment_role`; terminal/transient/unknown meaning
is carried by `failure_disposition`. These fields come from deterministic
runtime logging metadata rather than parsing `text` or requesting
model-authored explanations.

Integrator-refresh fields expose the bounded one-attempt recovery boundary.
`integrator_refresh_attempt` is currently `1` when present;
`integrator_refresh_outcome`, `reason`, and `changed` describe its terminal
result, while `integrator_refresh_latency_ms` measures the refresh from start
through final application. `eligible` records the deterministic retry decision.
These fields never include authority fingerprints, model reasoning, or role
output.

Ambient-scheduler fields make routing and capacity decisions inspectable without
requesting model-authored logs. `enrichment_mode` distinguishes focused from
ambient-presence work, and `coordinator_role` records the selected graph role.
The retained mode, probe, subject, and pulse fields describe the one bounded
deferred candidate when replacement or coalescing is evaluated. Subject and
cooldown values are SHA-256 digests rather than raw command or artifact text.
In-flight fields expose the session-wide and same-mode counts at the logged
decision; before/after pairs describe slot transitions, and `max_inflight`
records the effective session cap.

Relation fields identify the deterministic relation policy and expose its
effective limit plus aggregate received, accepted, invalid, duplicate,
overflow, and total discarded counts. A discarded-relation aggregate is
default-visible as a warning. Per-entry `expert.relation_discarded` diagnostics
are DEBUG events and are not represented by additional typed API fields; their
model-authored relation text is never retained.

Presence-gap fields expose the deterministic ambient routing policy, a SHA-256
digest of its bounded input, its `independent_life` objective, preferred
materialization surface, and aggregate anti-repetition counts. They make
Coordinator routing decisions inspectable without retaining state-derived
subject or system names. The raw presence-gap object and its hint arrays are
never retained by this endpoint.

Foreground-evidence fields expose the deterministic evidence policy and a
SHA-256 digest of its bounded projection, whether authoritative host time was
observed, and the aggregate number of recent exhaustive observations. Evidence
kind names and raw command output are not retained as typed event fields.

Deferred lifecycle events are initially session- and candidate-scoped because
no job exists yet. A successful `deferred.scheduled` event carries both its
`candidate_id` and the newly accepted `job_id`; `completed_job_id` identifies
the job whose completion opened the one-shot release opportunity. This lets an
exact `job_id` read begin at deferred acceptance without rewriting earlier
candidate-only records.

`provider_circuit.opened` identifies a process-local per-provider terminal
circuit. Later automatic admission or preflight suppression appears as
`trigger.suppressed` with `reason = "provider_circuit_open"`; idle timers and
deferred candidates cancelled by the opening appear as `idle.cancelled` and
`deferred.cancelled` with the same reason. Manual jobs remain callable and do
not reset the circuit on success. Service stop/start
clears circuits and emits `provider_circuit.reset` with
`reason = "runtime_restarted"`. That reset record is process-scoped and has no
session id, so it appears through the global runtime log/SSE surfaces rather
than `AdminEnrichmentEventCollection`. There is no circuit field in
`AdminRuntimeStatus`; inspect the originating session's retained circuit and
suppression records plus the global reset log instead.

Authority dependency selection uses bounded `authority.*` events. A final
active-session race appears as `authority.semantic_conflict` and a skipped
patch outcome with `reason_code = "semantic_authority_conflict"`. The event
schema intentionally cannot return the authority payload or digest.

#### `GET /api/v0/events`

Query parameters:

- `after`: integer cursor, default `0`.
- `limit`: integer, default `20`.
- `timeout`: integer seconds, default `15`.
- `once`: boolean, default `false`.

Response: `text/event-stream`.

Each event frame uses:

```text
id: 42
event: log
data: {"sequence":42,"type":"log","source":"dreamland.runtime","data":{"text":"message"}}
```

`once=true` returns one bounded poll and closes the stream. Without `once`, the
connection remains open until the client disconnects or the server stops.
Long-lived streams emit SSE comment heartbeats when no records are available.

#### `GET /api/v0/sessions`

Query parameters:

- `limit`: integer, default `20`.

Response: `AdminSessionCollection`.

The collection combines live in-memory sessions and Langfuse historical
sessions. Each item includes `live` and `langfuse` booleans.

#### `GET /api/v0/live-sessions`

Query parameters: none.

Response: array of `AdminLiveSessionSummary`.

#### `GET /api/v0/live-sessions/{session_id}`

Query parameters: none.

Response: `AdminLiveSessionSummary`.

Returns `404` when the session is not live in the current process.

#### `GET /api/v0/sessions/{session_id}`

Query parameters:

- `historical_limit`: integer, default `100`.

Response: `AdminSessionInspection`.

This endpoint unifies live and historical inspection metadata. Historical
lookup is currently list-based; a historical session beyond `historical_limit`
may not be found.

#### `GET /api/v0/sessions/{session_id}/state`

Query parameters: none.

Response: `AdminLiveSessionState`.

This is live-only. Historical state inspection is currently exposed through
`state-delta`.

#### `GET /api/v0/sessions/{session_id}/trace`

Query parameters:

- `limit`: integer, default `20`.
- `after`: optional integer cursor. When present, returns trace records with
  sequence greater than `after`; otherwise returns latest records.

Response: array of `AdminTraceEntry`.

#### `GET /api/v0/sessions/{session_id}/events`

Query parameters:

- `after`: integer cursor, default `0`.
- `limit`: integer, default `20`.
- `timeout`: integer seconds, default `15`.
- `once`: boolean, default `false`.

Response: `text/event-stream`.

Each event frame uses:

```text
id: 7
event: trace
data: {"sequence":7,"type":"trace","source":"dreamland.trace","data":{"session_id":"8af27532247d4f34ba45a7356f287fe4","text":"output"}}
```

Returns `404` when the session is not live in the current process.

#### `GET /api/v0/sessions/{session_id}/commands`

Query parameters:

- `limit`: integer, default `20`.
- `filter`: optional string. Filters command text through the historical
  backend.
- `sort`: string, default `time`. Supported values are `time`, `command`,
  `tokens`, `cost`, and `latency`. Unknown values currently fall back to
  `time`.
- `descending`: boolean, default `true`.
- `include_io_summary`: boolean, default `true`.

Response: array of `CommandSummary`.

Requires historical introspection.

#### `GET /api/v0/sessions/{session_id}/commands/{command_id}`

Query parameters:

- `limit`: integer, default `20`.
- `detail`: `full` or `compact`, default `full`. `full` returns raw model
  input, output, metadata, and observation payloads. `compact` returns command
  and observation summaries without raw IO or full metadata payloads.

Response: `CommandInspection`.

`command_id` may be a full command id, an unambiguous id prefix, or a relative
target such as `@-1` for the most recent command.

Requires historical introspection.

#### `GET /api/v0/sessions/{session_id}/state-delta`

Query parameters:

- `from`: optional command id, id prefix, or relative target. If omitted, the
  state before the selected `to` command is used when available.
- `to`: command id, id prefix, or relative target, default `@-1`.
- `limit`: integer, default `20`.
- `section`: repeatable string. When present, filters `before_state` and
  `after_state` to matching top-level state sections.

Response: `StateDelta`.

Requires historical introspection.

#### `GET /api/v0/state-profiles`

Query parameters: none.

Response: array of `AdminStateProfileSummary`.

#### `GET /api/v0/state-profiles/default`

Query parameters: none.

Response: `AdminDefaultStateProfile`.

Returns JSON `null` when no default state profile is set.

#### `GET /api/v0/state-profiles/{name}`

Query parameters: none.

Response: `AdminStateProfileDetail`.

#### `POST /api/v0/introspection/flush`

Request body: optional empty JSON object.

Response:

```json
{
  "status": "ok"
}
```

Requires historical introspection.

#### `POST /api/v0/state-profiles`

Request body:

```json
{
  "name": "lab",
  "level": "system",
  "session_id": "8af27532247d4f34ba45a7356f287fe4",
  "comment": "operator note"
}
```

Fields:

- `name`: required non-empty string.
- `level`: required string, one of `exact`, `system`, `world`.
- `session_id`: required non-empty string for a live session.
- `comment`: optional string.

Response: `AdminStateProfileSaveResult`.

#### `POST /api/v0/sessions/{session_id}/state-profile`

Request body:

```json
{
  "name": "lab"
}
```

Response: `AdminStateProfileApplyResult`.

Applies a saved profile to a live session.
The connection login, connection metadata, and `dreamland.session.*` metadata
remain attached to the live session. World/system profiles use the current
login and expand username templates. Exact profiles restore effective shell
state and history while retaining the connection login.

#### `PUT /api/v0/state-profiles/default`

Request body:

```json
{
  "name": "lab"
}
```

Response: `AdminDefaultStateProfile`.

This changes the process-local default and does not rewrite `dreamland.ini`.

#### `DELETE /api/v0/state-profiles/default`

Request body: none.

Response: `AdminStateProfileClearDefaultResult`.

#### `DELETE /api/v0/state-profiles/{name}`

Request body: none.

Response: `AdminStateProfileDeleteResult`.

#### `POST /api/v0/spiritguide/profiles`

Request body:

```json
{
  "name": "factory-lab",
  "instruction": "Build a small factory OT network",
  "level": "world",
  "dry_run": false,
  "comment": "operator note"
}
```

Fields:

- `name`: required non-empty state profile name.
- `instruction`: required non-empty Spiritguide instruction.
- `level`: optional string, `world` or `system`, default `world`.
- `dry_run`: optional boolean, default `false`.
- `comment`: optional string.

Response: `AdminSpiritguideProfileResult`.

Creates a detached state profile from the packaged blank world state. The
operation does not require a live session. When `dry_run` is `true`, no profile
is saved.

Create, update, and fork also accept optional `config_overrides` with only a
`spiritguide` object, using the fields in
[MCP Model Evaluation Controls](MCP_MODEL_EVALUATION.md). Result `metadata`
includes `evaluation_config` and the detached `session_id` when a configured
runtime factory records the call. An instruction-free copy fork rejects model
overrides because it performs no inference. These arguments are also exposed
by the corresponding three admin MCP tools.

#### `POST /api/v0/spiritguide/profiles/{name}`

Request body:

```json
{
  "instruction": "Add a legacy engineering workstation",
  "level": "world",
  "dry_run": false,
  "comment": "operator note"
}
```

Fields:

- `instruction`: required non-empty Spiritguide instruction.
- `level`: optional string, `world` or `system`; defaults to the existing
  profile level.
- `dry_run`: optional boolean, default `false`.
- `comment`: optional string.

Response: `AdminSpiritguideProfileResult`.

Loads an existing state profile, runs Spiritguide against that detached state,
and saves the updated profile back to the same name unless `dry_run` is true.

#### `POST /api/v0/spiritguide/profiles/{source_name}/fork`

Request body:

```json
{
  "name": "factory-lab-v2",
  "instruction": "Make this a hospital biomedical network",
  "level": "world",
  "dry_run": false,
  "comment": "operator note"
}
```

Fields:

- `name`: required non-empty target state profile name.
- `instruction`: optional Spiritguide instruction for the forked profile.
- `level`: optional string, `world` or `system`; defaults to the source profile
  level.
- `dry_run`: optional boolean, default `false`.
- `comment`: optional string.

Response: `AdminSpiritguideProfileResult`.

Copies an existing detached profile to a new name and optionally runs
Spiritguide against the copied state before saving it. When `instruction` is
omitted, the fork is a plain profile copy.

#### `POST /api/v0/sessions/{session_id}/spiritguide`

Request body:

```json
{
  "instruction": "Move the session into /srv/app",
  "dry_run": true
}
```

Fields:

- `instruction`: required non-empty string.
- `dry_run`: optional boolean, default `false`.

Response: `AdminSpiritguideResult`.

When `dry_run` is `true`, the response describes the proposed update but does
not mutate the live session.

#### `POST /api/v0/shutdown`

Request body: optional empty JSON object.

Response: `AdminShutdownResult`.

Requests shutdown of the running DREAMLAND process.

### MCP Parity Notes

The MCP binding is mounted at `/mcp` on the same authenticated local server.
MCP read resources expose the same shared service data as the REST API with
small fixed defaults such as `limit=20` and `historical_limit=100`.
`dreamland://agent-guide` is an MCP-only plain-text resource for external
agents and operators who use DREAMLAND. It is intentionally separate from the
simulated command-agent prompts.
Parameterized MCP tools are hidden unless `mcp_write_tools = true`.

MCP-only session execution tools currently do not have REST equivalents:

- `dreamland_session_create`: returns a session id, login/user/host/cwd
  summary, tags, optional label, and caller metadata. The created session is a
  live session with origin `mcp`.
  Optional `state_profile` selects a saved boot profile without changing the
  default. Optional direct `model` and `openai_service_tier` configure the
  command emulator and alias the matching `config_overrides.agent` fields;
  conflicting duplicates fail before model construction. An omitted OpenAI
  command tier uses `default`, even if the process is configured for Fast.
  Optional `config_overrides` resolves `agent`, `spiritguide`, and
  `enrichment` fields into a private runtime. With a model runtime factory,
  sessions also get private runtimes when no controls are supplied.
  `evaluation_config` in the result
  contains the resolved non-secret settings, schema version, and SHA-256 digest.
  See [MCP Model Evaluation Controls](MCP_MODEL_EVALUATION.md) for the complete
  field and inheritance contract. `evaluation_overrides` in capabilities
  indicates whether the service has a configured runtime factory.
- `dreamland_session_run_command`: returns `session_id`, direct `command_id`,
  `command`, `stdout`, `stderr`, `exit_status`, `interrupted`, `signal`,
  `history_index`, and `history_count`, plus `history_position` and
  `inspection_target` for correlation. It also returns a prompt-like `context`
  describing the post-command user, host, cwd, active program, and prompt.
  These fields describe the user-visible command result and correlation
  position only; use existing session, trace, command, and state-delta
  resources or the parameterized MCP inspection tools for deeper inspection.
- `dreamland_sessions_close`: closes live sessions by exact ids or metadata
  filters and supports dry-run previews. It rejects unfiltered close requests.
  Agents should prefer exact `session_ids` with `origin="mcp"`. Tag, label, or
  origin-only cleanup should use `dry_run=true`, report matches, and proceed
  with `dry_run=false` only after human/operator approval.

The MCP binding also exposes parameterized tools for log reads, session lists,
live-session lists, tag groups, unified session inspection, live trace reads,
retained enrichment-event reads, historical command lists, command inspection,
and state deltas. These mirror the REST query parameters where practical and
keep agents away from raw console commands.
`dreamland_session_enrichment_events_read` accepts `session_id`, optional exact
`job_id`, `limit`, and `after_sequence`; its corresponding fixed-default
resource is `dreamland://sessions/{session_id}/enrichment-events`.
Recent Langfuse-backed historical command data may lag briefly even after
`dreamland_introspection_flush`. Live trace remains the immediate source for
current user-visible input/output; retry command list or command inspection
after a short delay if a just-run command is not visible yet.
Non-dry-run close operations that use non-id filters or match non-MCP sessions
emit warning logs through the DREAMLAND runtime logging facility.

`dreamland_session_run_command` rejects shell commands beginning with
`!dreamland`. Use explicit MCP resources or tools for operator actions instead
of raw Dreamland console command strings.

MCP prompts provide reusable external-agent workflows for session testing,
session lifecycle cleanup, and command diagnostics. These prompts guide MCP
tool use; they are not Dreamland command-agent behavior prompts.

A second MCP binding is mounted at `/client-mcp` for generic command-client
evaluation. It has an independent MCP session lifecycle and must be initialized
separately from `/mcp`. It exposes only `client://guide` as a resource. When
`mcp_write_tools = true`, it exposes only these tools:

- `client_session_create`
- `client_run_command`
- `client_session_exit`

The generic client tools do not expose admin resources, diagnostic inspection,
structured failures, command ids, history positions, trace metadata,
telemetry, or bulk cleanup. `client_session_create` returns the created
session's `state_profile` label so trusted external harnesses can verify they
are testing the intended scenario before command execution begins; subsequent
command results remain user-visible command data only. `client_run_command`
includes prompt-like post-command context fields such as username, hostname,
cwd, active program, and prompt so generic clients can understand nested SSH or
REPL location changes without using admin inspection state. `client_session_exit`
accepts one exact session id and only closes sessions created through the
generic client path.

`client_session_create` still accepts only `username`. Its new model-backed
sessions use the same standard OpenAI command tier default as admin MCP;
direct model/tier selection and nested overrides belong to admin `/mcp`.

Use `docs/ADMIN_INTERFACE.md` for the MCP resource and tool list. Keep this
contract and the MCP list aligned when REST endpoints, resource URIs, request
bodies, or response schemas change.

## Maintenance

Update this document whenever `/api/v0` routes, query parameters, request
bodies, response fields, error codes, SSE payloads, MCP parity assumptions, or
default limits change. If a field is added only for diagnostics or may be
removed before production, mark it as implementation detail here.
