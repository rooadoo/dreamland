# 0042 Ambient-Presence Enrichment

Status: Active
Audience: maintainers, DREAMLAND operators, researchers, prompt authors, and
coding agents
Purpose: record the ambient-presence Enrichment mode, its deterministic trigger
policy, and the capacity boundary chosen to make session-local worlds feel
inhabited without introducing autonomous personas.

## Overview

Focused Enrichment has produced useful technical depth, but a richly described
host can still feel as though it exists only for the foreground user. Process,
connection, login, filesystem, and log views too often lack ordinary evidence
of other users, automation, devices, institutions, remote operators, historical
occupants, or other scenario-appropriate sources of activity.

The command emulator already receives instructions to render ambient realism.
The selected foreground model remains valuable for its speed and cost, however,
and prompting it more insistently on every command would still leave the
surrounding causes implicit. Low-cost asynchronous enrichment can instead
materialize coherent causes in session state before a later command asks the
emulator to render their effects.

This checkpoint expands the existing EnrichmentAgent rather than creating a
general activity simulator. It establishes historical life, other identities,
ordinary operational residue, log history, recurring automation, and bounded
present-activity observations. It does not create autonomous personas, advance
their goals, or independently start and expire simulated processes, sessions,
and connections. Those behaviors require a distinct temporal feature.

Decision 0043 is the follow-up for pulse admission, active-host bootstrap
retry, the first deterministic presence-gap routing, and typed foreground
evidence. Decision 0044 subsequently replaces only that v1 presence-gap
payload with concise independent-life routing. Together they supersede this
decision's 30-second pulse interval, ambient-wide pulse-counter reset, shared
pulse/idle cooldown, single-path bootstrap-admission, and initial ambient
planning details while preserving the mode and authority boundaries
established here.

## Decision

### Two Explicit Enrichment Modes

Every Enrichment job has one deterministic mode:

- `focused` performs the existing just-in-time or slight-read-ahead finishing
  work around concrete user attention; and
- `ambient_presence` establishes one coherent causal cluster of activity not
  centered solely on the foreground user.

Mode is selected by deterministic trigger policy before LangGraph invocation.
A model does not decide which role it is playing. Focused work uses the
existing Coordinator. Ambient work uses a separately configured Ambient
Coordinator with its own prompt and model controls. Only one Coordinator runs
for a job, and both branches return the same compact planning contract to the
shared neutral Expert, fresh-state Integrator, validation, and safe-apply path.
The ambient role therefore has a distinct purpose without duplicating evidence,
patching, or mutation authority.

The Coordinator input includes the selected mode and a deterministic structural
coverage summary for the target host. The summary counts materialized facts,
filesystem artifacts, groups, services, and users. It helps an Ambient
Coordinator broaden a sparse or repeatedly enriched host without spending
model output tokens on bookkeeping. A zero count is an attention hint, not
proof that the represented world is truly empty.

The initial ambient checkpoint's visible graph identifier was
`enrichment-v10`. Its Coordinator
branch remains explicit in Mermaid and Langfuse rather than being hidden behind
a generic dynamic agent runner:

```text
prepare_coordinator_context
    +-- focused ----------> Coordinator
    `-- ambient_presence -> Ambient Coordinator
                                  |
                         shared Expert
                                  |
                    fresh state and Integrator
                                  |
                    validation and safe apply
```

Mode, selected Coordinator role, trigger, coverage counts and hash, job
lifecycle, model usage, and application outcome are deterministic trace and log
metadata. Models are not asked to narrate scheduling or generate verbose
logging.

### Ambient-Presence Content Boundary

Ambient presence means scenario-appropriate signs that a world has a life
beyond the current foreground interaction. A useful cluster may involve human
users, automation, devices, institutions, synthetic actors, remote operators,
historical occupants, or stranger setting-specific entities. Those examples
are deliberately not a closed taxonomy, and enterprise conventions are not a
default.

Within the existing additive host-local registries, ambient work may establish:

- other basic identities, groups, ownership, and ordinary work products;
- historical login, maintenance, audit, and operational evidence;
- logs, queues, caches, checkpoints, failed attempts, completed work, and
  other durable residue;
- recurring services, jobs, workers, remote peers, and connection ecology; and
- bounded signs of present activity, including active processes and network
  connections, when they fit established state and are represented as an
  observation rather than an autonomous lifecycle.

The goal is one connected realization, not generic clutter or a checklist.
Established state, foreground authority, and operator constraints continue to
win. An explicit state profile or Spiritguide instruction describing solitude,
abandonment, minimalism, pristine operation, or another incompatible setting
overrides the general preference for population.

Ambient Enrichment does not create a behavioral persona, make an actor take
future actions, or schedule an entity to appear and disappear. It has no new
delete, replace, expiry, correction, or cross-session authority. Present-tense
observations can become stale under this additive contract, so true transient
activity and autonomous actors remain future work.

### Deterministic Trigger Expansion

The automatic trigger set expands with five reusable signals:

- `host_presence_bootstrap` starts asynchronous baseline presence work when a
  session registers or a host is newly discovered or activated;
- `presence_probe` responds to foreground inspection of population and
  activity surfaces such as users, login history, processes, connections, or
  broad service activity;
- `subject_first_touch` provides focused read-ahead when a successful,
  uninterrupted command first names a concrete path, service, process,
  endpoint, package, database, or similar subject;
- `materialization_followthrough` adds coherent ambient consequences after the
  foreground emulator establishes a relevant identity, service, peer,
  artifact, or activity fact; and
- `interaction_pulse` periodically admits ambient work from deterministic
  interaction progress rather than typing speed.

`interaction_pulse` is the primary recurring background source intended to use
otherwise available enrichment capacity as a session advances. Concrete host
pivots, discoveries, probes, and first touches retain stronger causal priority.
At this checkpoint, `session_idle` remained available as the lowest-priority
opportunity to use a quiet slot; it is not the main population mechanism. Idle
yields during same-correlation and deferred-candidate arbitration, but an idle
job that has already been admitted consumes the shared in-flight, rate, and
cooldown budgets and resets ambient pulse progress like any other accepted
ambient job.

That reset behavior and the interval described below were superseded by
decision 0043 after live trigger-mix review.

The initial pulse became eligible after four successful, uninterrupted
commands on one host, requires at least 30 seconds since the last accepted
ambient job there, and resets the host interaction count whenever any ambient
candidate is accepted. Fixed automatic priority from highest to lowest is
`active_host_changed`, `host_discovered`, `subject_first_touch`,
`surface_focus`, `state_materialized`, `host_presence_bootstrap`,
`presence_probe`, `materialization_followthrough`, `interaction_pulse`, and
`session_idle`. Focused triggers therefore win the single deferred position
over ambient opportunities while a focused and ambient candidate from one
foreground correlation may still run together.

All trigger derivation, counting, correlation, deduplication, cooldown, and
admission remain deterministic and reusable. Trigger construction makes mode
first-class so ambient and focused candidates do not become accidental
duplicates merely because they target the same host and coarse surface. The
single bounded deferred-candidate position remains in place; the feature does
not add an unbounded queue.

Deduplication included mode plus concrete focus, probe, or subject identity.
Cooldown also included mode and a trigger-appropriate subject; interaction
pulse and idle shared the same ambient-general cooldown. A first
touch is consumed at deterministic detection even if later admission suppresses
it, while a host is marked bootstrapped only after its bootstrap job is
accepted.

Manual work, active-host changes, host discovery, subject first touch, repeated
surface focus, and ordinary state materialization route to `focused`.
Bootstrap, presence probes, ambient follow-through, interaction pulses, and
idle route to `ambient_presence`.

### Three In-Flight Jobs And Retention

The packaged and development default rises from one to three in-flight
Enrichment jobs per session. This lets a concrete focused job and additional
ambient work overlap while preserving the existing per-session rate limit,
short-lock snapshot/apply sequence, one deferred automatic candidate, atomic
cluster validation, and current-state-wins conflict policy. Three is a capacity
ceiling, not a requirement to keep every slot occupied.

The dedicated per-session enrichment-event buffer rises from 4,000 to 10,000
records. Expanded automatic triggering and concurrent job lifecycles otherwise
make it too easy for early trigger, lock, and application evidence to be evicted
during a useful manual test. This does not resize ordinary console or session
trace buffers and does not add restart durability.

These values supersede decision 0037's one-job and 4,000-record checkpoint
defaults. Decision 0037 remains the historical record of the automatic
scheduler and one-deferred-candidate experiment that preceded this expansion.

### Onset Population And Future Activity

Asynchronous host-presence bootstrap is accepted for this checkpoint, even
though the first command can arrive before the warm-up patch. A separate
Spiritguide/profile-authoring effort should pre-seed baseline historical life
when an operator needs the world to feel populated from the first command.

A future `WorldActivityAgent` will own genuinely changing activity. Its design
needs typed actors and activity records, a world clock, recurrence and expiry,
active-versus-historical projection, and explicitly controlled non-additive
authority. It must use its own directly inspectable LangGraph and trigger policy
rather than silently extending Enrichment's additive contract.

## Alternatives Considered

Adding only more foreground-emulator prompt language was rejected as the sole
solution. It asks the foreground model to repeatedly reinvent ambient causes
and gives later commands less structured material to preserve. Enrichment can
materialize those causes once and let the faster emulator render their visible
consequences.

Creating the full WorldActivityAgent now was rejected because autonomous
personas and transient lifecycle changes require substantially different state,
time, correction, and mutation contracts. Historical life and observational
signs can be tested safely within Enrichment first.

Using one Coordinator prompt for both intents was rejected because focused
near-term finishing and ambient world-population planning have different
objectives and tuning needs. A separate role configuration keeps the purposes
inspectable while still sharing the expensive safety and integration path.

Random background clutter was rejected. Depth comes from connected,
scenario-derived causes and residue, not an arbitrary quota of usernames,
processes, sockets, or log lines.

Removing idle triggering was deferred. Idle remains useful as a lowest-priority
opportunity when capacity is available, but interaction progress is the more
repeatable primary driver.

Blocking the first foreground command on enrichment was rejected for this
checkpoint because it would place background model latency directly in the
entry experience. Asynchronous warm-up is accepted while profile pre-seeding is
developed separately.

## Consequences

DREAMLAND can use inexpensive background calls more frequently to create a
world that feels inhabited before the user explicitly asks for every detail.
The faster foreground emulator receives concrete identities, artifacts,
history, and activity relationships instead of relying solely on an abstract
instruction to be realistic.

Three concurrent jobs increase the chance of snapshot drift and overlapping
proposals. Existing atomic cluster checks and current-state conflict handling
remain the governing boundary; this decision does not claim universal semantic
conflict detection. Mode, trigger, role, lock, and patch telemetry must remain
visible so cost, latency, duplicate work, and user-visible inconsistencies can
be tuned from evidence.

The larger retained-event buffer consumes more bounded process memory. All
jobs, timers, trigger memory, retained events, and generated session state
remain process-local and temporal unless an operator explicitly saves a state
profile.

This checkpoint intentionally produces evidence of life without simulating a
complete living actor. That distinction keeps Enrichment useful now and leaves
room for a bespoke persona or WorldActivityAgent feature later.

## Maintenance

Update this decision when enrichment modes, Coordinator routing or
configuration, coverage-summary shape, trigger derivation or priority,
interaction-pulse policy, idle semantics, in-flight or deferred capacity,
event retention, graph topology, ambient write authority, or the boundary with
future actor simulation changes. Keep `docs/ENRICHMENT_AGENT.md`,
`docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`, `docs/STATE_PROFILES.md`,
`docs/INDEX.md`, `ROADMAP.md`, configuration examples, prompt modules,
scheduler code, graph code, telemetry, and focused tests aligned. Add the
related implementation commit after this checkpoint is committed.
