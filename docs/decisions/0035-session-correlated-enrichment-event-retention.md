# 0035 Session-Correlated Enrichment Event Retention

Status: Active
Audience: maintainers, DREAMLAND operators, researchers, and coding agents
Purpose: record why EnrichmentAgent lifecycle events have dedicated bounded
session and job correlation outside the shared runtime log ring.

Decision 0037, `automatic-enrichment-checkpoint`, extends this record with
independent capacity configuration and candidate-only correlation for the
bounded deferred automatic scheduler. The same process-local retention and
non-model logging principles remain current.

## Overview

EnrichmentAgent emits deterministic lifecycle, trigger, model-boundary, lock,
snapshot, validation, patch, and availability events through normal Python
logging. During manual analysis, unrelated runtime and MCP diagnostic traffic
could evict the beginning of an enrichment job from the shared process log ring
before its later events were inspected. String filtering the remaining lines
could not recover the missing lifecycle or reliably distinguish jobs.

This feature is experimental and restart durability is not a goal. Operators do
need enough process-local retention to align model work, deterministic decisions,
state application, and foreground user experience without asking a model to
narrate its reasoning or generate logging text.

## Decision

DREAMLAND retains structured enrichment events in a dedicated in-memory buffer
in addition to their existing grep-friendly runtime log lines. The logging call
attaches private `LogRecord` attributes for session id, optional job id, event
name, and a bounded structured operational mapping. The handler derives the
remaining record fields from ordinary logging metadata. This path performs no
model call and consumes no model input or output tokens.

Retention is a bounded deque per session. Decision 0037 separates its capacity
from the console and session-trace rings through positive integer
`[enrichment] event_log_capacity`, with environment override
`DREAMLAND_ENRICHMENT_EVENT_LOG_CAPACITY`. The packaged and development default
is 4,000 records per session. `[console] log_capacity` continues to size the
ordinary runtime-log and session-trace buffers. One noisy session cannot evict
another session's enrichment events, and normal runtime or MCP log traffic
cannot evict them. Records remain readable after the live session closes, until
their per-session deque evicts them or the process exits. The collection reports
capacity, retained count, evicted count, and retained sequence bounds so
incomplete history is visible.

Each retained record contains:

- a process-local monotonic sequence;
- session id and optional exact job id;
- event name and logging level;
- optional event, causation, correlation, trigger, and command IDs;
- optional target host and requested, selected, and effective surfaces;
- optional status, distinct `reason` and `reason_code`, trigger candidate and
  coalescing IDs, `replaced_candidate_id`, `retained_candidate_id`,
  `completed_job_id`, `higher_priority_trigger`, `retained_trigger`,
  `focus_key_sha256`, `priority`, `retained_priority`, `age_ms`, `max_age_ms`,
  `focus_path_count`, `focus_artifact_count`, the boolean `accepted` result,
  and normalized patch registry targets; and
- the unchanged formatted runtime log line.

The retained mapping deliberately excludes Coordinator `reason_preview` and
other model-authored prose. Large patch outcomes and DEBUG detail remain in the
bounded formatted line rather than being duplicated as a second structured
payload. Missing scalar fields serialize as `null`; missing registry targets
serialize as an empty array.

The protocol-neutral admin service owns typed reads by exact session, optional
job, optional sequence cursor, and limit. REST exposes
`GET /api/v0/sessions/{session_id}/enrichment-events`; admin MCP exposes
`dreamland_session_enrichment_events_read`. Neither read requires the session
to remain live. These interfaces filter structured fields rather than parsing
rendered log text.

Accepted trigger records receive the assigned job id before they are emitted,
so an exact job query includes the acceptance decision as well as queue, graph,
apply, readiness, and foreground-match events. Suppressed triggers have no job
id and remain available through the session-wide view.

Decision 0037 also retains deterministic lifecycle events for the one deferred
automatic candidate per session. A deferred candidate has a candidate id but
no job id and therefore appears only in the session-wide view while it is
stored, retained over a lower-priority arrival, replaced, expired, or cancelled.
When one-shot release passes the current gates, `deferred.scheduled` carries the
candidate id and newly accepted job id, while `completed_job_id` identifies the
job whose completion opened the release opportunity. Exact job filtering covers
the new job lifecycle from that scheduling record onward. The earlier
candidate-only events are not retroactively rewritten with a job id.

The scheduler's 60-second candidate maximum age is distinct from event-log
retention. Candidate state disappears on acceptance, replacement, expiry,
session close, service stop, or process exit, while its already emitted event
records remain until ordinary per-session event-buffer eviction or process
exit.

## Alternatives Considered

Increasing the shared global ring was rejected because it only delays eviction
and lets unrelated sessions and MCP diagnostics consume the same capacity.

Putting enrichment events in the session trace buffer was rejected because
session traces represent user-visible input, output, and reasoning-summary
continuity. Background-agent control telemetry is an operator concern with a
different retention and schema contract.

Recovering correlation by parsing rendered log strings was rejected because the
structured values already exist at emission time and parsing would create a
second fragile log grammar.

Persisting LangGraph checkpoints, jobs, or event records across restart was
rejected for this checkpoint. Langfuse continues to retain model traces when
configured, but it does not replace service-owned trigger, lock, apply, and
availability events.

Model-authored explanations were rejected because they add latency and token
cost, vary between runs, and cannot reliably describe deterministic runtime
branches that occur outside the model call.

## Consequences

Researchers can retrieve a complete retained lifecycle for one session or job
after running foreground diagnostics, even when those diagnostics generate
heavy global log traffic. Eviction is bounded and explicit rather than silently
appearing complete.

Memory use is bounded per session but the number of retained session keys is not
yet globally capped, matching the existing process-local session-trace pattern.
The 4,000-record default deliberately retains substantially more automatic
scheduler and graph activity than the ordinary console ring; operators can tune
it independently. All records disappear at restart. Sequence values are
process-local and must not be treated as durable identifiers.

Changing the normal logging enablement, level, or `enrichment` event category
also changes what enters the dedicated buffer. The buffer is not a hidden audit
channel that bypasses operator logging policy.

## Maintenance

Update this decision when enrichment event fields, retention capacity,
process-lifetime behavior, filtering, REST or MCP contracts, or logging policy
changes. Keep `src/dreamland/runtime_logging.py`, enrichment telemetry and
service correlation, the admin service and bindings, operations documentation,
and owning tests aligned. Add the related implementation commit after this
checkpoint is committed.
