# 0037 Automatic Enrichment Checkpoint

Status: Active
Audience: maintainers, DREAMLAND operators, researchers, prompt authors, and
coding agents
Purpose: record bounded automatic EnrichmentAgent scheduling, deterministic
interaction focus, structural Integrator-output normalization, and the
observability controls selected after the first sustained automatic safe-mode
session.

## Overview

Decision 0049, `default-safe-enrichment`, makes automatic scheduling with
`safe` application the packaged product default. This record remains
authoritative for the scheduling, normalization, and observability controls
that bound that default.

Scheduling must preserve higher-priority host pivots and concrete foreground
attention while bounding speculative work. Structural normalization may recover
representable Integrator shapes without inventing content or weakening final
safe apply.

This checkpoint keeps concurrency deliberately simple. It does not add another
simultaneous job, a persistent queue, restart recovery, or a model-authored
scheduler. It retains one bounded automatic candidate just beyond the current
job, gives concrete foreground attention to Coordinator deterministically, and
normalizes only structural output shapes that can be handled without inventing
content or weakening the final safe-apply boundary.

## Decision

### Priority And One Deferred Candidate

With the checkpoint default `max_inflight_per_session = 1`, Enrichment keeps
one running job and one deferred automatic trigger candidate per session. The
deferred bound remains one if the configurable in-flight cap is raised. Trigger
priority, from highest to lowest, is:

1. `active_host_changed`;
2. `host_discovered`;
3. `surface_focus`;
4. `state_materialized`; and
5. `session_idle`.

Priority is EnrichmentAgent policy, not an ordering imposed by the shared event
bus. Other background-agent types may reuse the same neutral events while
choosing different priorities, projections, limits, and authority.

Correlated events from one foreground interaction are arbitrated before they
can occupy both the running and deferred positions. Once the highest-priority
eligible candidate for a correlation is known, a lower-priority candidate from
that same correlation is terminally suppressed with
`higher_priority_correlated_candidate`; it is not deferred for later replay.
When the completed-command payload already announces a following active-host or
host-discovery event, the earlier generic candidate uses the same reason and
records the selected event type in `higher_priority_trigger`.

When an automatic candidate arrives while the session already has an in-flight
job, Enrichment may retain it in the single deferred position. A higher-priority
candidate replaces the retained candidate. A newer candidate of equal priority
also replaces it so the retained work follows the latest user attention. A
lower-priority arrival is discarded and the existing candidate remains. These
branches log both candidate identities, their priorities, and the replacement
or retention reason.

A deferred candidate is the original immutable `TriggerCandidate`, including
its causal event and interaction focus. It has no job id, does not increment the
accepted-job rate window, and does not begin cooldown until it is later accepted
as a job. It is process-local and expires after 60 seconds.

When a job completion opens a release opportunity, Enrichment releases the
retained candidate exactly once if it is still current enough. It rechecks session registration,
configuration, self-source and duplicate policy, in-flight capacity, cooldown,
and rate limits against current runtime state. A remaining cooldown or rate
limit is a terminal logged suppression. This checkpoint creates no cooldown or
rate retry timer. Expired candidates are discarded, and session close or
service stop cancels retained candidates. A new accepted command cancels a
deferred `session_idle` candidate but leaves more specific deferred work
available for its one-shot release. Manual submissions are never placed in the
deferred position and continue to use their explicit manual admission rules.

This bounded position is not a durable job queue. It is a small just-in-time
attention buffer intended to keep one important near-future gap from being lost
while preserving the experimental one-job concurrency model.

### Deterministic Interaction Focus

The backend derives one bounded `interaction_focus` object from each foreground
command with ordinary deterministic parsing and no model call. The object can
contain:

- `cwd`: the working directory before the interaction;
- `result_cwd`: the working directory after it;
- `paths`: up to four concrete normalized path identifiers;
- `artifacts`: up to four `{kind, id}` identifiers for such subjects as a path
  pattern, service, process, network endpoint, package, or database;
- `primary`: the first path or artifact selected by stable precedence; and
- `key`: a bounded stable `<kind>:<id>` identity for that primary focus.

Text fields are bounded to 256 characters. The same completed focus is copied
into the correlated `command.completed`, `active_host.changed`,
`state.materialized`, and later `session.idle` events. The accepted-command
event carries the starting focus so idle cancellation and foreground matching
can observe the user's new attention immediately.

Enrichment preserves this object in the trigger candidate and supplies it under
Coordinator `wake_reason` separately from event-anchor `focus_hints`.
`interaction_focus` is a targeting hint: it says what the user named or where
the interaction occurred. It does not prove that an artifact exists, establish
its contents, become an available reference, relax Expert evidence rules, or
authorize a state write. Coordinator may use it to select the most relevant gap
and question while anchors remain opaque IDs for established references.

### Idle Timing

The packaged and development idle delay is five seconds. A command accepted
before the timer fires cancels it. The timer still measures quiet after a
completed foreground interaction; it does not inspect or claim to understand
whether an interactive program is awaiting confirmation or credential input.
Five seconds is therefore a conservative timing adjustment, not a semantic
terminal-idleness guarantee.

### Visible Structural Normalization

The inspectable LangGraph includes a deterministic
`normalize_integrator_output` step between parsed Integrator output and final
proposal validation. Its policy is
`integrator-output-normalization-v1`, versioned independently from the model
prompt. It performs no model repair call and generates no model-authored
reasoning or logging text.

Normalization may remove an array-valued leaf from a contribution value because
safe enrichment cannot write arrays. It retains exact scalar and object siblings
without reading an array's elements to select or invent replacement content.
Objects made empty by this pruning are removed recursively. A contribution is
removed only if its complete value becomes empty. If no contribution remains,
the workflow ends deterministically as `no_supported_contribution`; it does not
ask Integrator to try again.

Several contributions may retain the same registry path and key only when every
one uses `merge_missing` and their flattened desired scalar leaf paths are
pairwise disjoint with no prefix collision. Contributions remain separate, as
do their IDs and source-item provenance, so the existing per-contribution
eight-source limit does not become a sixteen-source aggregate. All other
duplicate-target combinations remain invalid and use the existing
`duplicate_contribution_target` rejection.

The normalized proposal still passes target, registry, operation, provenance,
dependency, contribution-count, touched-path, and live-state validation. Arrays
are pruned rather than written, and compatible same-target contributions commit
atomically with the rest of their causal cluster. Direct safe-apply validation
continues to reject arrays and incompatible duplicate targets if they reach it
from any path that did not pass through graph normalization.

Deterministic normalization telemetry records the policy version, array-leaf
paths and counts, emptied-object and dropped-contribution counts, compatible
duplicate-target counts, and the terminal normalization result. It does not
copy array contents or ask a model to explain the change.

### Role Capacity And Event Retention

Coordinator's default output cap is 2,048 tokens, allowing room for its
compact JSON after reasoning. Expert and Integrator remain at 2,048 and 4,096
tokens. These caps do not request verbose reasoning or model-authored logs.

Enrichment events continue to use a dedicated bounded per-session process-local
buffer. Positive integer `[enrichment] event_log_capacity`, with environment
override `DREAMLAND_ENRICHMENT_EVENT_LOG_CAPACITY`, independently sizes it and
defaults to 4,000 records per session in the packaged and development
configurations. `[console] log_capacity` continues to size ordinary console and
session-trace rings. Deferred candidate events remain queryable by session and
candidate id before a job id exists. After acceptance, `deferred.scheduled`
carries that candidate and the new job id; `completed_job_id` identifies the
job whose completion opened the release attempt. Exact job filtering covers the
new job lifecycle from that scheduling record onward.

## Alternatives Considered

Running several jobs concurrently was deferred because it would increase model
cost and create additional consistency and apply-order questions before the
single-job policy is tuned.

An unbounded or FIFO trigger queue was rejected. Old idle and materialization
work should not build up behind newer host pivots, and a queue would require
staleness, cancellation, fairness, and restart policies that do not improve the
immediate experiment.

Having Coordinator choose trigger priority or restate foreground focus was
rejected because both are deterministic control information. Spending model
tokens on them would reduce repeatability without adding evidence.

Repairing malformed patches with another model call was rejected because it
adds latency, cost, and another nondeterministic branch. Structural pruning and
compatible-path checks are sufficient for the narrow recoverable cases observed
in the automatic session; ambiguous proposals remain rejected.

Converting array contents into strings, objects, or synthetic keys was rejected
because that would interpret and transform model-authored substance. Removing
the unsupported leaf is smaller and preserves safe authority.

## Consequences

Near-term host pivots and discoveries can survive one older running job without
allowing background work to accumulate indefinitely. A released candidate may
still be suppressed by cooldown or rate policy, and work older than 60 seconds
is deliberately lost. Both outcomes are visible and tunable from deterministic
events.

Concrete paths and named artifacts reach Coordinator without treating command
syntax as authoritative state. This should reduce broad assignments that miss
the user's immediate subject while preserving the independent Expert's neutral
role and the Integrator's state-aware responsibility.

Recoverable Integrator shape errors no longer discard an otherwise useful
cluster merely because one array leaf is unsupported or disjoint fields for one
target were split across contributions. Normalization cannot make a
contradictory or unsupported patch safe, and the complete normalized cluster
still wins or loses atomically at final apply.

The larger event buffer increases bounded process memory use but keeps enough
automatic scheduler and job history for operator analysis. None of the
scheduler, event, job, or graph state survives restart.

## Maintenance

Update this decision when automatic priority, deferred-candidate count or age,
release/retry policy, focus schema or bounds, idle semantics, normalization
rules, role output caps, enrichment-event capacity, job correlation, graph
topology, concurrency, or persistence changes. Keep `docs/ENRICHMENT_AGENT.md`,
`docs/OPERATIONS.md`, `docs/ARCHITECTURE.md`,
`ROADMAP.md`, configuration examples, event and
focus contracts, service scheduling, graph normalization, retention schemas,
telemetry, and focused tests aligned. Add the related implementation commit
after this checkpoint is committed.
