# 0043 Ambient Pulse Admission, Presence-Gap Routing, And Foreground Evidence

Status: Active
Audience: maintainers, DREAMLAND operators, researchers, prompt authors, and
coding agents
Purpose: record the post-ambient-checkpoint changes that make recurring
Enrichment work reachable, direct it toward visible independent life, and bind
ambient application to recent foreground observations.

## Overview

Ambient enrichment needs recurring progress-driven admission, retryable active-
host bootstrap, and typed foreground constraints for chronology and complete
inventories. These refine decision 0042 while preserving additive authority.
Decision 0044 supersedes only the presence-gap payload and ranking with
`presence-gap-v2`; pulse, bootstrap retry, and foreground evidence remain current.

## Decision

### Progress-Driven Pulse Admission

`interaction_pulse` becomes eligible after every four successful,
uninterrupted commands on one host. It has no elapsed-time gate and bypasses
the per-scope cooldown. The global per-session accepted-job rate limit,
in-flight ceiling, deduplication, provider circuit, and one bounded deferred
automatic candidate remain in force.

Only acceptance of an `interaction_pulse` resets that host's interaction
counter. Acceptance of bootstrap, probe, follow-through, or idle work does not
erase pulse progress. If a pulse is eligible but cannot be admitted, progress
is retained so a later successful command can try again. An eligible pulse
cancels its correlated idle timer or deferred idle candidate and records the
suppression deterministically. Pulse priority remains 20, above only
`session_idle` at 10, so concrete host, subject, surface, materialization,
bootstrap, and probe triggers continue to win.

### Bounded Active-Host Bootstrap Retry

The scheduler retains one process-local active-host bootstrap retry marker per
session when an active host's baseline candidate is not accepted. This marker
is separate from the one general deferred automatic candidate, so a more
important focused candidate cannot permanently consume the active host's only
warm-up opportunity. It is retried after command completion, session idle, or
job completion and remains subject to current registration, trigger, provider,
rate, and in-flight gates.

The marker holds only the latest active target and is removed when that host's
bootstrap is accepted, its registration changes, the session closes, or the
runtime stops. It is not a queue and adds no restart durability. When active-
and discovered-host bootstrap candidates have equal priority in the ordinary
deferred position, the active-host candidate wins; the decision is logged as
`active_host_preferred`.

### Deterministic Presence-Gap Routing

At this checkpoint, Ambient Coordinator input gained `presence_gap` under policy
`presence-gap-v1`. Runtime code ranks directly observable host categories by
current structural coverage: independent users, groups, services, and concrete
filesystem artifacts. The active/login username is excluded from the
independent-user count when typed foreground authority identifies it. The
payload also carries bounded, deterministic repetition hints derived from
already materialized users, groups, services, and repeated system terms.

`presence_gap` is a planning and materialization-priority aid, not evidence.
It cannot establish absence, provenance, recency, or a fact. Explicit state
profile and Spiritguide direction remain authoritative. The Ambient
Coordinator should follow the preferred observable when it is compatible with
that direction and established state, vary away from repeated subjects and
systems when useful, and center a non-login subject by default. The subject may
be human, automated, device-driven, institutional, synthetic, remote,
historical, environmental, or otherwise appropriate to the represented world;
the policy does not impose a closed actor taxonomy.

The shared independent Expert remains schema version 2. It receives no
`presence_gap` field and no DREAMLAND workflow vocabulary. Instead, the
Ambient Coordinator turns the deterministic routing preference into its
ordinary neutral question. Integrator received `presence_gap` only as
non-evidentiary mapping priority so safely mappable identities, memberships,
services, files, logs, and residue are not unnecessarily flattened into
abstract facts. The visible graph identifier advances to `enrichment-v11`.

Decision 0044 replaces this four-category routing payload after its first live
checkpoint. It preserves the structural `coverage_summary` and role boundary,
but uses one concise independent-life objective plus a preferred existing
technical surface and bounded anti-repetition hints. `presence-gap-v1` remains
documented here as historical behavior rather than current agent input.

### Typed Foreground Evidence

Enrichment context version 4 adds policy `foreground-evidence-v1` with two
strictly recognized observation families:

- the newest successful standalone `date` or `timedatectl` result provides a
  host-reported chronology anchor; and
- at most four distinct complete inventories from the last four command
  records may cover `/home` immediate children, login history, current
  sessions, sockets, or all processes.

Recognition is intentionally conservative. Commands must be successful,
uninterrupted, standalone forms with accepted display/scope options. Compound,
filtered, ambiguous, failed, interrupted, stderr-bearing, or oversized
inventory commands remain ordinary bounded command context. Output resembling
a structured command-response envelope is also excluded, preventing malformed
foreground fallback output from becoming inventory authority. A `last` result
is complete only when its recorded output includes the `wtmp begins`
terminator. A custom `date +FORMAT` qualifies only when it contains a calendar,
year, or epoch conversion; literal, time-of-day-only, and month/day-only output
does not become an absolute chronology anchor. An oversized inventory is never
treated as exhaustive negative evidence.

The public bounded observations reach Coordinator and Expert, and the fresh
current snapshot reaches Integrator. Host time constrains exact chronology; if
it is unknown, generated material should use relative timing. A complete
inventory is direct evidence only for its stated coverage at that observation
point. It may rule out a row in that snapshot but does not assert permanent
absence; a later addition needs an explicit compatible creation, arrival,
start, rotation, or other lifecycle relationship.

For every focused or ambient patch, deterministic runtime code binds a hidden
`foreground_evidence` digest captured with the Integrator snapshot. The empty
evidence view also has a stable digest, so the user's first recognized
observation during the Integrator-to-apply window invalidates an older patch.
Safe apply recaptures the evidence view under the final state lock and skips the
atomic cluster as `semantic_authority_conflict` if that digest changed. The
private digest covers complete recognized records before public excerpt
clipping, does not enter role inputs or serialized patch output, and is not
logged. An ordinary command does not change it while the selected evidence set
remains the same; enough later commands can intentionally age a recent
inventory out of the four-record evidence window and therefore change the
digest. This is an optimistic race guard for typed observations, not a lexical
temporal classifier, universal contradiction detector, or correction
mechanism.

### Deterministic Observability

Pulse reset, correlated idle suppression, bootstrap retry lifecycle, active-
host deferred preference, presence-gap selection, and foreground-evidence
capture and dependency binding are runtime-authored events. Logs retain bounded
counts, policy versions, hashes, categories, and reason codes rather than raw
observed output, state-derived subject names, or model reasoning. No model call
or output token is added for this instrumentation.

## Alternatives Considered

Keeping the 30-second pulse interval was rejected because accepted ambient work
could continuously postpone progress-driven work. The global rate and in-flight
gates already provide the intended cost and concurrency bounds.

Resetting pulse progress after every ambient acceptance was rejected because
bootstrap, probe, follow-through, and idle work serve different causes. Their
success should not erase the deterministic interaction-progress signal.

Adding a general second deferred queue was rejected. One dedicated active-host
bootstrap marker addresses the demonstrated onset gap without turning the
scheduler into an unbounded backlog.

Asking the Ambient Coordinator to count registries, remember earlier subjects,
or generate its own logging was rejected. Deterministic `presence-gap-v1`
provides repeatable inputs without spending Coordinator output tokens.

A broad lexical validator for phrases such as “active,” “current,” or
“recent” was rejected. Those words are legitimate for persistent services and
many ordinary facts, while arbitrary prose cannot safely encode temporal
semantics. Strict command recognition, prompt constraints, and an apply-time
evidence digest provide a narrow explainable checkpoint while typed activity
state remains future work.

## Consequences

Recurring ambient work is now driven by actual interaction progress rather
than wall-clock spacing between other background jobs. It remains deliberately
low priority and can still yield to rate, capacity, provider, or more concrete
causal work.

Ambient planning receives a repeatable nudge toward visible evidence of a world
beyond the foreground login without boxing DREAMLAND into an enterprise or
human-only simulation. Structural ranking can still be imperfect: a registry
count measures materialized representation, not subjective richness, and
repetition hints are bounded soft signals.

Typed foreground evidence makes direct time and complete-inventory observations
harder to overwrite during the snapshot-to-apply race. Its deliberately small
recognizer leaves most shell output as untrusted ordinary context. It neither
repairs malformed command envelopes nor supplies live actor lifecycle,
expiration, deletion, or autonomous behavior.

The extra active-bootstrap marker and evidence digest are process-local. They
do not create restart durability or shared state across sessions.

## Maintenance

Update this decision when pulse counting, priority, cooldown or admission
semantics, idle correlation, bootstrap retry capacity, presence-gap categories
or ranking, foreground command recognition, evidence digest binding, graph or
context versions, or the WorldActivityAgent boundary changes. Keep
`docs/ENRICHMENT_AGENT.md`, `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`,
`docs/STATE_PROFILES.md`, `docs/INDEX.md`,
`ROADMAP.md`, prompts, scheduler, graph, telemetry, and owning tests aligned.
Decision 0042 remains the historical record of the initial ambient-presence
checkpoint; this decision supersedes its 30-second pulse interval, ambient-wide
counter reset, shared pulse/idle cooldown, and single-path bootstrap admission
details. Add the related implementation commit after this checkpoint is
committed. Decision 0044 supersedes this decision's `presence-gap-v1` payload
and ranking while retaining its pulse, bootstrap-retry, and foreground-evidence
contracts.
