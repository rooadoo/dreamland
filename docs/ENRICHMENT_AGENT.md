# Session-Local EnrichmentAgent

Status: Active
Audience: maintainers, DREAMLAND operators, prompt authors, and coding agents
Purpose: document the default asynchronous agent that adds bounded just-in-time
detail to one live session's world state.

## Overview

The EnrichmentAgent adds finishing touches just ahead of a user's likely
attention. The command emulator remains responsible for constructing hosts,
services, and the facts required to answer commands. Enrichment adds one bounded
cluster of net-new supporting detail, such as processes behind an established
service, operational files around a workflow, or candid residue consistent with
the host's history and setting.

Enrichment also has an `ambient_presence` planning mode for making a host feel
as though it has a life beyond the foreground user. That mode can establish
one recognizable named human, or a scenario-supported person-equivalent, with
time-qualified personal, work, or social activity and connected residue.
Supporting automation, logs, connections, work products, and technical
artifacts make that individual observable but do not replace them as the
cluster's subject. It does not create autonomous personas or independently
advance transient activity; those require a future temporal activity feature.
The existing near-term technical planning behavior is named `focused` mode.

DREAMLAND prioritizes engaging depth, internal coherence, believable
plausibility, and then exact factual realism. Established facts and operator
constraints remain authoritative, but safe enrichment is not limited to facts
already proven by state. At moderate and higher creative latitude, the Expert
may select one coherent representative realization when several ordinary
answers fit. Per-job limits bound latency and mutation; repeated just-in-time
passes may continue deepening the world.

Failure explanations have a narrower continuity boundary. The focused
Coordinator preserves the failed operation's scope and time. The Expert must
not manufacture resource limits, exhaustion measurements, or defective
dependencies merely to explain an error. The Integrator checks those claims
against the current snapshot even when candidates are deterministically
eligible. A symptom such as acute memory pressure does not establish a memory
leak, overcommit policy, swap configuration, or resource ceiling. Recasting
invented causes as historical measurements or logs does not make them safe to
materialize. Supported scenario faults, compatible diagnostic residue, and
ordinary operational enrichment remain valid. Intervening recovery must not
be undone by an old candidate. This is prompt guidance alongside the existing
structural provenance and freshness guards, not a deterministic interpreter
of arbitrary fault prose. See
[decision 0122](decisions/0122-failure-scope-and-continuity.md).

This is a separate runtime role from Spiritguide. Spiritguide follows an
explicit operator instruction and can make broad DREAMLAND-aware state changes.
Enrichment reacts to neutral session events, asks an independent Expert for
compact source material, and can only propose bounded additive host-local
changes.

Every enrichment job belongs to one live session. Its changes are temporal and
disappear when that session ends unless an operator explicitly saves a state
profile. The agent does not update the source profile, share a mutable world
with another session, or write global world state.

Saved host `login_banner` fields are omitted from enrichment
`world_state` and active-host metadata projections. The initial-login shell
reads that text directly from authoritative session state; enrichment is not
required to generate, repair, or delay its display. Spiritguide retains the
complete banner field for operator-directed creation and repair. See
[State Profiles](STATE_PROFILES.md#saved-initial-login-banners).

Automatic enrichment is enabled by default. The default `safe` apply mode runs
the complete proposal and validation workflow and permits only validated,
bounded, additive host-local patches. Operators can explicitly disable
automatic scheduling or select `shadow` mode to exercise the workflow without
changing session state.

## Runtime Boundaries

The implementation is split by responsibility:

- `src/dreamland/background_agents/events.py` defines immutable neutral session
  events, trigger candidates, and explainable gate decisions.
- `src/dreamland/background_agents/event_bus.py` fans events out to named
  subscribers. Delivery failures are isolated so one future background agent
  cannot prevent another subscriber from seeing an event.
- `src/dreamland/background_agents/triggers.py` contains deterministic,
  reusable self-source, coalescing, in-flight, cooldown, and rate gates.
- `src/dreamland/background_agents/surfaces.py` derives coarse command surfaces
  without a model call.
- `src/dreamland/background_agents/focus.py` derives bounded concrete
  interaction focus from command syntax and working-directory state without a
  model call and classifies later foreground alignment as exact-artifact,
  related-path, or surface-only.
- `src/dreamland/background_agents/authority.py` captures a bounded typed view
  of the active session frame for reuse by independently authorized background
  agents. It exposes neither arbitrary environment/frame metadata nor command
  history.
- `src/dreamland/background_agents/enrichment/context.py` builds the bounded
  DREAMLAND-aware planning projection.
- `src/dreamland/background_agents/enrichment/foreground_evidence.py` strictly
  recognizes bounded host-time and recent complete-inventory observations and
  computes a latest-per-scope private apply-time digest independently of the
  public four-command window.
- `src/dreamland/background_agents/enrichment/role_inputs.py` organizes the
  Coordinator input, builds and resolves deterministic references, and creates
  the neutral allowlisted brief passed across the Expert boundary.
- `src/dreamland/background_agents/enrichment/latitude.py` deterministically
  translates world generation liberty into task-neutral Expert guidance,
  paired candidate and relation limits, allowed candidate origins, and
  non-evidentiary Integrator retention guidance.
- `src/dreamland/background_agents/enrichment/modes.py` defines the explicit
  `focused` and `ambient_presence` planning intents. Trigger policy chooses one
  before graph invocation; models do not choose their own mode.
- `src/dreamland/background_agents/enrichment/limits.py` keeps role and patch
  size boundaries explicit and prevents a candidate-capacity change from
  silently expanding unrelated auxiliary arrays.
- `src/dreamland/background_agents/enrichment/normalization.py` performs
  subtractive, versioned Integrator-output normalization without a model call.
- `src/dreamland/background_agents/enrichment/graph.py` owns the explicit
  initial-snapshot, Coordinator-context preparation, focused-or-ambient
  Coordinator branch, Expert-brief preparation, Expert, Expert-item
  classification, current-snapshot, reference-resolution, Integrator,
  structural normalization, and validation graph. It proposes a patch but never
  applies one.
- `src/dreamland/background_agents/enrichment/patching.py` applies a validated
  patch as a pure, race-aware operation over a current world state.
- `src/dreamland/background_agents/enrichment/service.py` owns runtime
  subscription, scheduling, session locks, model-work dispatch, apply mode, job
  history, and operator control.
- `src/dreamland/provider_failures.py` owns the provider-neutral bounded failure
  taxonomy used to distinguish terminal account/configuration conditions from
  transient rate, timeout, and availability failures.

The event substrate is intentionally neutral. Future correction, adversarial,
assistant, or intent agents can subscribe to the same event types while using
their own projections, trigger policies, graphs, logging identities, and write
capabilities. Reuse of the event bus does not grant those agents Enrichment's
authority.

## Events And Just-In-Time Triggers

The backend and live-session registry publish these reusable events:

| Event | Current producer | Enrichment use |
| --- | --- | --- |
| `session.registered` | live-session registry | Can start one asynchronous ambient-presence bootstrap for the entry host. |
| `command.accepted` | backend | Cancels a pending idle timer or deferred idle candidate and carries starting interaction focus for concrete-subject tracking. |
| `command.completed` | backend | Carries completed interaction focus, updates repeated surface focus and interaction progress, recognizes activity probes and first touches, and schedules an idle event. |
| `active_host.changed` | backend | Can enrich the newly active host, bootstrap its ambient presence, and retain the correlated interaction focus. |
| `state.materialized` | backend or a named background agent | Can enrich a newly materialized entity or surface and can derive an ambient follow-through candidate from foreground-emulator materialization. |
| `session.idle` | Enrichment's timer through the shared bus | Can fill a near-term world gap after a quiet delay using completed interaction focus. |
| `session.closing` | live-session registry | Cancels timers, deferred work, and other session-local trigger state. |

The automatic trigger names accepted in `[enrichment]` are:

- `host_discovered`: targets the first newly discovered host reference,
  providing slight read-ahead before the user pivots.
- `active_host_changed`: targets the host the user has just entered.
- `host_presence_bootstrap`: runs asynchronous ambient baseline work once for a
  newly registered, discovered, or first-activated host so the first few
  interactions do not remain centered solely on the foreground user. A host is
  marked bootstrapped only after this candidate is accepted.
- `presence_probe`: recognizes a broad foreground inspection of users, login
  history, processes, network connections, or similar activity and prepares
  related ambient evidence for a subsequent observation. Only successful,
  uninterrupted commands qualify, and narrow selectors are excluded where
  deterministic parsing can recognize them.
- `subject_first_touch`: targets the first observed concrete path, service,
  process, endpoint, package, database, or comparable interaction subject for
  focused read-ahead. It requires a successful, uninterrupted command and does
  not treat working-directory context alone as a new subject.
- `state_materialized`: follows a command-emulator world-state update that is
  not classified as host discovery.
- `materialization_followthrough`: adds ambient consequences around a relevant
  identity, service, peer, artifact, or activity fact newly established by the
  foreground command emulator.
- `surface_focus`: fires after two consecutive completed commands have the same
  target host and coarse surface. Current surfaces include shell, network,
  process, service, packages, identity, filesystem, and database.
- `interaction_pulse`: periodically admits `ambient_presence` work from
  deterministic command progress on one host. It is the primary recurring
  background trigger and is based on interaction count rather than typing
  speed. The current policy becomes eligible after four successful,
  uninterrupted commands on the host. It has no elapsed-time gate or per-scope
  cooldown. Only an accepted interaction pulse resets the host count; an idle,
  bootstrap, probe, or follow-through job does not. If admission fails, the
  accumulated progress remains and a later successful command may try again.
- `session_idle`: fires after `idle_delay_ms` of quiet following command
  completion. A later accepted command cancels the pending timer. Idle remains
  the lowest-priority opportunity and is not the primary population mechanism.
  It yields during correlated and deferred arbitration and uses the ordinary
  rate, in-flight, and cooldown gates. An eligible interaction pulse cancels
  its correlated idle timer or deferred candidate.

Mode routing is fixed: `manual`, `active_host_changed`, `host_discovered`,
`subject_first_touch`, `surface_focus`, and `state_materialized` are `focused`;
`host_presence_bootstrap`, `presence_probe`,
`materialization_followthrough`, `interaction_pulse`, and `session_idle` are
`ambient_presence`. `runtime.started` logs this complete mapping so an operator
can audit it without inferring mode from prompt content.

The packaged delay is five seconds. It is a quiet-time heuristic, not a claim
that DREAMLAND understands whether an interactive program is waiting for
confirmation or credential input.

`enrichment run` creates a separate `manual` trigger. Manual work remains
available when automatic mode is disabled and bypasses automatic cooldown and
per-minute rate rejection for that submission. An accepted manual job still
counts in the shared accepted-job window, causing later speculative automatic
work to yield when the provider/user-experience budget is full. Manual work
requires a live session and obeys duplicate and per-session in-flight limits.
Manual candidates are never stored in the automatic deferred position.

Automatic admission also observes a process-local provider circuit. A terminal
credit/quota, authentication, authorization, or configuration failure from an
Enrichment role, or a bounded terminal provider-failure notice carried by a
command-emulator completion event whose provider is used by the enrichment graph, opens
the circuit for that provider as `provider_circuit.opened`. The current circuit
gate is conservatively graph-wide: later automatic jobs in either mode are
suppressed at admission or job preflight as
`trigger.suppressed reason=provider_circuit_open` before another model call.
Opening a circuit cancels every current idle timer and bounded deferred
candidate through `idle.cancelled` and `deferred.cancelled` with the same
reason. Ordinary rate limiting, timeouts, unavailability, and unknown errors do
not open this terminal circuit. Manual
submissions remain available so an operator can test a corrected credential,
model, or account condition, but manual success does not reset the circuit.
There is no timed reset. A service stop/start clears the process-local circuits
and emits `provider_circuit.reset reason=runtime_restarted`. Circuit state is
visible through retained events rather than a new status field or
model-authored explanation.

Every service start owns a monotonically increasing in-process runtime
generation. `stop(wait=True)` stops admission and drains that generation before
returning. `stop(wait=False)` invalidates it immediately, cancels queued work,
and clears pending, in-flight, focus, rate-window, and deferred scheduler
accounting so an old or hung call cannot block a replacement runtime. A late
old worker may still return, but its generation cannot apply a patch, open a
provider circuit, or release a current deferred candidate. Subscription
callbacks carry the generation that created them, so an event delivery already
snapshotted by the event bus cannot cross a stop/start boundary. Concurrent
start is rejected while stop cleanup is in progress. Lifecycle logs include the
runtime generation and distinguish `runtime_stopped` from
`runtime_restarted`. This is process-local isolation, not restart durability.

Automatic candidates use this fixed Enrichment-specific priority, highest
first:

1. `active_host_changed`;
2. `host_discovered`;
3. `subject_first_touch`;
4. `surface_focus`;
5. `state_materialized`;
6. `host_presence_bootstrap`;
7. `presence_probe`;
8. `materialization_followthrough`;
9. `interaction_pulse`; and
10. `session_idle`.

Focused triggers occupy the higher priority band so concrete foreground
attention wins the single deferred position over ambient opportunity.
`interaction_pulse` remains the primary *recurring* ambient source even though
more specific events outrank it. The shared event bus does not impose this
ordering on future background agents. The backend includes newly discovered
host refs and active-host-change status in the completed-command event so
Enrichment can arbitrate the correlated trigger family. Arbitration is
mode-local: one foreground correlation can launch at most one focused and one
ambient job, but those two jobs may run together. A lower-priority candidate in
the same mode and correlation is terminally suppressed as
`higher_priority_correlated_candidate`; it is not retained for later work.
Large discovery batches retain the target-host hint plus at most seven
supporting event hints; logs record how many were omitted.

For each remaining candidate, gates and scheduling run in a stable order:

1. suppress an event emitted by the same named background agent;
2. coalesce an identical pending session/host/mode/surface/trigger/subject
   candidate;
3. retain or arbitrate one deferred automatic candidate when the session's
   configured in-flight capacity is occupied;
4. enforce the automatic mode- and subject-aware cooldown scope; and
5. enforce the automatic per-session 60-second rate window before acceptance.

Deduplication includes session, host, mode, surface, trigger, and a deterministic
focus/probe/subject identity. Cooldown includes session, host, and mode, then a
subject chosen by trigger: probe kind for `presence_probe`, concrete-subject
hash for `subject_first_touch`, a fixed bootstrap marker, `ambient-general` for
idle, and otherwise the interaction-focus hash or surface. Interaction pulse
bypasses this per-scope cooldown; global rate, in-flight, deduplication, and
provider-circuit gates still apply. This lets focused and ambient work proceed
independently without allowing idle to evade its ordinary cooldown. A concrete
first touch is marked seen when deterministically
detected even if later admission suppresses that candidate; it is a one-time
attention signal, not a retry queue.

Each session retains at most one immutable deferred automatic
`TriggerCandidate`. A higher-priority candidate replaces it; a newer
equal-priority candidate also replaces it; a lower-priority arrival is
discarded while the existing candidate remains. A deferred candidate preserves
its causal event, concrete focus, anchors, target, and surface, but has no job
id and does not enter cooldown or the accepted-job rate window. It expires after
60 seconds and is cancelled on session close or service stop. A new accepted
command cancels a deferred `session_idle` candidate but leaves more specific
deferred work available for one-shot release.

One additional bounded marker is reserved for an active host whose
`host_presence_bootstrap` was not accepted. It does not occupy or enlarge the
general deferred position and is not a job queue. DREAMLAND retries the marker
after command completion, session idle, or job completion, rechecking current
registration and every ordinary admission gate. It retains only the latest
active target and is cleared on acceptance, registration change, session close,
or runtime stop. When active- and discovered-host bootstrap candidates compete
at equal priority in the general deferred position, the active-host candidate
wins and the decision records `active_host_preferred`.

The deferred candidate is released exactly once after a job completion opens a
release opportunity. Enrichment rechecks current session registration, configuration,
self-source and duplicate rules, in-flight capacity, cooldown, and rate limits.
A cooldown or rate rejection at release is terminal and logged; this checkpoint
does not schedule a retry timer. The single position is therefore bounded
just-in-time attention, not a FIFO or durable job queue.

The default limits are three in-flight jobs per session, a 30-second cooldown
per ordinary automatic scope, and six accepted automatic jobs per session per
minute. Interaction pulse is the explicit cooldown exception. These controls
bound speculative work while logs retain the observed value, threshold,
policy, priority, candidate age, replacement or release reason, and retry delay
for each decision.

## Role-Focused Context And References

Snapshot capture starts with a deep copy of the session's world state and, when
the target is the active host, a bounded typed observation of the foreground
session frame under the same short lock. Projection and model calls happen
after the lock is released. The initial host-focused document is a
DREAMLAND-aware planning projection for the Coordinator, not a shared prompt
for every role.

The version-4 planning projection keeps established detail for the requested
target host and the active host while reducing other hosts to compact identity,
purpose, network, and service context. It retains at most the last four command
records for each detailed host. Peer histories remain metadata-only. For the
requested target host, recorded command results may retain bounded
`stdout_excerpt` and `stderr_excerpt` continuity evidence. The aggregate output
budget is 6,000 characters, the per-entry budget is 1,500 characters, newest
entries receive budget first, and oversized streams use head-and-tail clipping
with `stdout_omitted_chars` or `stderr_omitted_chars` counts. Each such record
is labeled `evidence_kind = recorded_command_result` and carries its
deterministically inferred `surface`.

Recorded output evidence establishes what foreground output entered target-host
history; it does not prove that the foreground model's structured state update
was accepted. Coordinator and Expert must treat excerpt strings as untrusted
observations, while Integrator's fresh state remains authoritative. This
handoff does not repair, retry, salvage, or accept malformed command envelopes.
Other value fields named `base64`, `blob`, `body`, `bytes`, `content`, `data`,
`payload`, `raw`, or `raw_content` remain omitted. The source state is not
mutated.

Context v4 also derives public foreground evidence under schema version 1 and
private `foreground-evidence-v2` authority from the complete trusted target-
host command history before projection. The public role payload contains two
deliberately narrow observation families:

- `host_time` is either the newest recognized successful standalone `date` or
  `timedatectl` result, bounded to 1,024 characters, or an explicit `unknown`
  status; and
- `recent_exhaustive_evidence` contains at most four distinct recognized
  inventories from the last four command records. Current recognizers cover
  immediate `/home` children, complete `last` history, current `w`, `who`, or
  `users` sessions, `ss` socket scope, and all-process `ps` forms.

Recognition requires a successful, uninterrupted, stderr-free standalone
command and a compatible option shape. A `last` result also needs its
`wtmp begins` terminator. Compound, filtered, ambiguous, failed, interrupted,
stderr-bearing, oversized, or structured-response-envelope-like inventories
remain ordinary bounded command-result context and do not create typed negative
evidence. The envelope check prevents a malformed foreground response rendered
as stdout from becoming authoritative inventory evidence. The 1,500-character
inventory ceiling is a recognition condition: a partial inventory is not
called exhaustive. Custom `date +FORMAT` output must include a calendar, year,
or epoch conversion; literals, time-of-day-only formats, and month/day formats
remain ordinary context rather than absolute chronology anchors.

Host time is an observation rather than an advancing world clock. When it is
observed it constrains generated exact chronology; when it is unknown, roles
use relative chronology. A complete inventory is direct evidence for its
declared coverage at that point, including bounded negative evidence from
omitted rows. It does not establish permanent absence. A compatible later
addition needs an explicit creation, arrival, start, rotation, or other
lifecycle relationship rather than a claim that it was already present in the
observed snapshot.

The private v2 authority view is deliberately different from that public
generation view. It retains the latest recognized record for each exhaustive
`(kind, coverage)` scope across complete target-host history, plus the latest
recognized host-time record, and hashes their complete pre-clipping payloads.
An inventory can age out of the four-command public prompt window without
changing private authority. A first observation, a new scope, a newer record
for an existing scope—even with byte-identical output and a new command ID—or a
new host-time record changes the digest. Superseded older records in one scope
do not. The aged-out content is no longer prompt-visible as recent negative
evidence and does not become a permanent absence fact; v2 only distinguishes
unrelated command cadence from the arrival of new direct evidence during model
latency.

The optional `foreground_authority` field is a direct observation of only the
active target-host session. Its schema contains active and login usernames,
frame hostname, cwd, interactive-program identity, bounded arguments and prompt,
a small allowlist of shell variables, and shell-stack depth. Program arguments
have fixed count/length limits, and deterministic handling redacts recognized
credential-bearing flags, conventional credential environment assignments,
and credentialed URLs before any model handoff. Arbitrary environment, frame
metadata, command history, output, and credential input are excluded. This
projection boundary does not redact or change DREAMLAND's intentional
foreground credential recording for honeypot and training uses. For current
session behavior it outranks incompatible descriptive world material, but it
does not establish a universal login policy or constrain unrelated world
detail. A non-active enrichment target receives no foreground authority.

The continuity prompt experiment also constrains authentication in that
non-active-target case. The Expert and Integrator must distinguish discovery,
reachability, host-key exchange, attempted target accounts, successful logins,
and effective users. A prompt or failed attempt does not establish remote
account provisioning, shell access, session ownership, or a universal key-only
or password-disabled policy. Empty destination fields do not make conflicting
additions valid. These claims require independent evidence, and unrelated
eligible activity remains available for enrichment. `sudo`/`su` does not rename
an observed login. The rule applies to focused and ambient enrichment through
their shared Expert/Integrator prompts; it does not expand the evidence supplied
to either role or change deterministic patch authority. See
[decision 0100](decisions/0100-host-activity-continuity-rules.md).

The visible `prepare_coordinator_context` node separates operator controls from
materialized state, adds deterministic mode, trigger, target, host-time, and
recent-exhaustive-evidence context, builds a structural `coverage_summary`, and
builds a deterministic `available_references` catalog. Coverage contains counts
for facts, filesystem artifacts, groups, services, and users on the target
host. It helps planning broaden an underdeveloped world without claiming that a
missing registry entry proves real absence. For ambient mode only, the node
also adds concise deterministic `presence-gap-v2` with exactly one
`independent_life` objective, a preferred existing technical surface, a
`named_human_or_person_equivalent_excluding_foreground_user_and_service_accounts`
subject preference, and bounded `anti_repetition` subject and system hints. A
current non-`shell` surface hint is retained; otherwise the
least-covered identity, service, or filesystem surface is selected. Identity
coverage combines independent users and groups, while the other two reuse
their structural coverage counts. The ambient-only object has exactly this
shape:

```json
{
  "policy_version": "presence-gap-v2",
  "ambient_objective": "independent_life",
  "preferred_surface": "filesystem",
  "subject_preference": "named_human_or_person_equivalent_excluding_foreground_user_and_service_accounts",
  "anti_repetition": {
    "subjects": [],
    "systems": []
  }
}
```

It carries no duplicate coverage counts, ranked observable list,
history-bearing-actor field, or model-authored bookkeeping. Presence gap is
planning guidance, not evidence, provenance, an absence claim, or an actor
taxonomy. The mode selects either the focused Coordinator or the separately
configured Ambient Coordinator. Both are
DREAMLAND-aware and receive the same core organized planning contract; exactly
one runs per job. When present, foreground authority is a separate trusted field
rather than an anchor or materialized registry fact; the selected Coordinator
must not plan a current shell, login, prompt, or access description that
conflicts with it. Each catalog entry has the neutral shape
`{id, kind, label, observation}`. Its `id` is an opaque job-local identifier
such as `r1` or `r2`; `kind`, `label`, and `observation` describe only an entity
or observation already present in the captured projection. IDs are assigned
deterministically within one job so every role and trace uses the same names,
but they are not durable world-state identifiers and must not be reused across
jobs.

The current reference allowlist includes target-host identity, platform,
network, hardware, shell, keyed host entities, and selected purpose fields.
`purpose.theme` is deliberately admissible in the neutral host brief and
reference catalog. The Expert can use the established setting to propose more
candid, contextually appropriate details; selecting its reference ID also
creates an explicit cross-role handle. Theme remains ordinary scenario context,
not an instruction channel. DREAMLAND generation policy, role-play policy,
expansion directives, and other internal control fields are excluded.

Within the recorded-command portion of Expert observation selection, results
matching the selected surface are ordered before other command results and then
by recency, within the existing observation-count limit. They reach the Expert
as `recent_command_result` observations with the bounded excerpts and omission
counts above. They are evidence of user-visible foreground continuity, not
evidence that an accompanying state proposal was accepted.

References are currently host-local. Neutral world facts explicitly bound to a
target host are not yet catalogued because the binding and final-apply contracts
cannot distinguish host-relative and world-relative dependency paths.
World-scoped reference bindings are deferred in `ROADMAP.md`. Coordinator task text must not substitute for that
missing evidence channel.

Each foreground command also produces one bounded deterministic
`interaction_focus` object. It may contain the starting `cwd`, completed
`result_cwd`, up to four normalized `paths`, up to four `{kind, id}`
`artifacts`, and a deterministic `primary` plus stable `<kind>:<id>` `key`.
Recognized artifact kinds include path patterns, services, processes, network
endpoints, packages, and databases. Text fields are limited to 256 characters.
The backend computes this object from command syntax and working-directory
state without a model call, then copies the same completed object across the
correlated completed-command, active-host, state-materialization, and idle
events. This lets a deferred candidate preserve the exact attention hint that
caused it even if later commands occur.

`prepare_coordinator_context` places `interaction_focus` under
`wake_reason`, separate from event-anchor `focus_hints`. It is explicitly
non-evidentiary: it identifies what the user named or where the interaction
occurred, but does not prove the subject exists, establish its contents, add an
available reference, relax Expert evidence rules, or authorize Integrator to
write it. Coordinator may use it to choose a near-term gap and focused question;
only the captured state, recorded observations, and selected reference catalog
establish facts across the role boundaries.

When later foreground work is compared with enrichment, existing coarse
eligibility still requires the same session, exact target host, and compatible
effective surface and still selects the newest eligible completed job.
Deterministic `interaction-focus-match-v1` then compares that causal job focus
with the accepted-command focus and records one of three alignment classes.
`artifact_exact` means both focus objects identify the same typed artifact,
concrete path, or path pattern. `path_related` means their concrete paths have a
meaningful ancestor/descendant relationship or a path pattern matches the other
focus. `surface_only` means the coarse surface is the only established
alignment. Shared cwd alone is deliberately ignored because adjacent commands
commonly stay in one directory while inspecting unrelated subjects. These
classes refine `enrichment.command_matched` and `enrichment.late` telemetry;
they do not prove that a specific enrichment contribution was rendered or
broaden write authority.

Session events may carry raw host or surface hints in their own `anchors`
field. Those trigger hints are inputs to planning, not Expert references, and
do not cross the role boundary unchanged. In the Coordinator output, an
**anchor** specifically means one available reference ID selected from the
catalog and copied exactly into `anchors`. It is not a topic word, a new fact,
or permission to invent attributes. It carries only the catalog observation
attached to that ID. Coordinator anchors bound the real-world task sent to the
Expert and let accepted candidates declare exact state bases. The Coordinator
may select at most eight and may not invent an ID absent from
`available_references`. It may select none when neutral host details and
observations are enough for the task; a candidate's explicit `basis` may then be
empty.

Coordinator output uses schema version 2. A `work` decision selects a focused
surface, question, anchors, and an `expertise` routing lens with exactly
`domain` and `perspective`. A `no_op` decision sets the work-only fields to
null or empty values. The lens selects the body of knowledge and practical
viewpoint needed for this job; it does not establish a host fact or replace the
Expert's stable system prompt.

The focused Coordinator selects one near-term technical gap around concrete
user attention. The Ambient Coordinator selects one coherent causal cluster of
activity beyond the foreground user's immediate command. It must center one
recognizable named human individual other than the foreground user. Only when
established world evidence or explicit direction supports nonhuman people may
it instead center one named person-equivalent with personal or social agency.
A service, shared, role, daemon, machine, automation, device, institution, or
group identity may support the cluster but cannot be its subject. The role
plans signs and residue of individual life, not an autonomous persona, and must
honor explicit operator, profile, or Spiritguide direction for solitude,
abandonment, minimalism, pristine operation, or another incompatible
condition. The two Coordinators use separate model/prompt configuration but
the same output schema, anchor semantics, neutral Expert boundary, and safe
Integrator authority. Under `presence-gap-v2`, the Ambient Coordinator joins
the individual to time-qualified personal, work, or social activity and
connected observable residue on the preferred technical surface. It varies
away from bounded repeated subjects and systems. `history-bearing_actor` is not
a key, count, registry, dimension, or closed taxonomy. These are planning and
causal-quality requirements, not proof, and explicit profile, Spiritguide, or
operator direction always wins.

For this policy, the foreground user is the person behind the interaction, not
only the current effective-account string. Active, login, effective, and
privileged accounts plus explicitly established aliases remain one excluded
foreground subject across `su`, `sudo`, privilege, and nested-SSH transitions.
A directly observed login, session, process, or socket row also retains its
username, PID, TTY, endpoints, source, observation time, and observed or
unknown owner. Ambient work cannot transfer that row to a new person unless the
row itself identifies them. It may create a separate historical or later event
when the event is explicitly time-qualified and is not presented as the
observed foreground row.

The visible `prepare_expert_brief` graph node validates that selection, resolves
creative latitude deterministically, and builds an exact six-field input for
the Expert:

```json
{
  "expertise": {
    "domain": "<relevant technical domain>",
    "perspective": "<relevant practical viewpoint>"
  },
  "question": "<focused real-world question>",
  "latitude": {
    "instruction": "<deterministic task-neutral instruction>",
    "candidate_limit": 8,
    "relation_limit": 8
  },
  "host": {},
  "observations": [],
  "references": []
}
```

`expertise`, `question`, and `latitude` route and bound the Expert's work; none
is evidence that the described host already has a property. `host` contains
neutral identity, platform, and allowlisted purpose fields. Each optional observation has
`{kind, label, details}` and represents allowlisted technical context or
bounded recent activity. `references` contains only the selected neutral
catalog views and provides the job-local handles that can be rebound to current
state for Integrator.

Neutral-brief vocabulary validation is field-sensitive. DREAMLAND control
markers remain prohibited throughout, while generic workflow-role markers
remain strict in the Expert question and object keys. Ordinary
`expertise.domain` and `expertise.perspective` values may use legitimate
real-world titles such as `OT operations coordinator`, `industrial systems
integrator`, or `embedded emulator specialist`; those words alone do not
expose DREAMLAND workflow. The question cannot ask the Expert to consult a
Coordinator, Integrator, or emulator, and internal keys remain rejected.

For `ambient_presence` only, deterministic brief construction appends the
versioned `ambient-human-presence-v3` requirement to the Coordinator-authored
question. It asks the Expert to center the named individual and connected
personal, work, or social residue while excluding service identities as a
substitute. It also reserves direct foreground rows to their observed or
unknown owners and treats foreground account transitions and established
aliases as one person. A protected row or a value learned only from it cannot
ground a distinct event for the proposed person; selected non-row evidence or
established state must independently support any reused value. The Expert still
receives exactly the six fields above;
it does not receive DREAMLAND state, `presence_gap`, policy vocabulary, or a
second schema. Default and explicit focused brief construction remain byte-
equivalent to the pre-policy behavior.

When foreground authority exists, deterministic brief construction inserts one
`active_session` observation ahead of the other observations. This preserves
the same six top-level Expert fields. The neutral observation contains bounded
direct session details, not DREAMLAND registry paths, job identifiers, patch
contracts, or the hidden authority fingerprint. The Expert must preserve that
current-session evidence while remaining free to create unrelated candid and
expansive operational material. The observation is not assigned a job-local
reference ID, so it is not repeated in an item `basis`; deterministic runtime
code supplies the separate authority dependency for a related proposed cluster.

Deterministic brief construction also turns typed context-v4 evidence into
neutral observations within the existing `observations` array. `host_time`
reports either its bounded direct observation or explicit unknown status, and
each `recent_exhaustive_observation` carries its command, coverage, and bounded
output. A command represented by one of these typed observations is not
duplicated as a generic recent command result. This does not create Expert
schema version 3: the brief still has the same six top-level fields and the
Expert report remains schema version 2. `presence_gap` never crosses the
independent Expert boundary; the Coordinator expresses its selected direction
through the ordinary neutral question.

`latitude` comes from a versioned deterministic mapping of
`generation_policy.liberty`. The internal policy object never crosses the
Expert boundary:

| Level | Candidate limit | Relation limit | Expert behavior | Integrator retention behavior | Allowed origins |
| --- | ---: | ---: | --- | --- | --- |
| `strict` | 2 | 2 | Only net-new details directly supported or logically unavoidable from evidence. | Materialize every directly and unambiguously mappable eligible candidate without interpretive substance. | `derived` |
| `conservative` | 4 | 4 | A small number of net-new routine details strongly implied by evidence. | Materialize every clearly mappable eligible candidate while avoiding marginal or lossy encodings. | `derived` |
| `moderate` | 8 | 8 | One coherent representative realization with supporting observable context around the focal detail. | Preserve a coherent representative cluster and distinct safely mappable artifacts that add net-new observable substance. | `derived`, `selected` |
| `expansive` | 12 | 12 | A substantial connected layer that uses most available capacity when coherent material exists and develops structural, lifecycle, causal, and residual depth. | Preserve distinct kinds, nested paths, leaf artifacts, lifecycle distinctions, and relationships rather than a flat summary. | `derived`, `selected` |
| `abundant` | 24 | 24 | A dense coherent causal layer that uses full capacity when distinct coherent net-new material supports it, with structural nesting, leaf material, lifecycle variation, causal relationships, and signs of present or past activity. | Preserve the fullest safe layer, including hierarchy, lifecycle distinctions, cross-surface residue, identities, and relationships. | `derived`, `selected` |

The mapping version is `creative-latitude-v5`. Missing, malformed, or unknown
values fall back to `moderate`. The effective level remains private runtime
policy used for deterministic origin enforcement, Integrator guidance, and
logging. The Expert receives only `instruction`, `candidate_limit`, and
`relation_limit`; repeating an opaque level label would add no authority or
evidence beyond those resolved values. Relation capacity follows candidate
capacity at 2, 4, 8, 12, and 24, so connected high-latitude work is not forced
through a lower topology ceiling. Resolution and logging require no model call
and no Coordinator output tokens.

Every v5 Expert instruction begins with the same task-neutral materialization
invariant: report affirmative, concrete, mutually consistent details from one
realization, not inspection advice, possibilities, or a menu of competing
implementations. The latitude-specific remainder still controls how much
ordinary uncertainty the Expert may resolve and how many candidates it may
return. Breadth therefore means more compatible texture within a coherent
world, not more mutually exclusive answers to the same role.

The stable Expert contract also shapes that breadth without hard-coding a
scenario. Hierarchical work should include concrete leaves below containers at
multiple meaningful depths instead of spending the budget on sibling directory
labels. Time-varying work should distinguish persistent state, intermittent or
short-lived activity, completed activity, and durable aftereffects. It should
preserve parentage, attachment, and causal relationships, including detached or
orphaned states, when relevant. These are open-ended depth dimensions rather
than a checklist; they do not authorize generic clutter or contradict a
deliberately minimal environment.

Current safe mode can store a durable lifecycle pattern, a temporally qualified
observation, or residue that is present now. It does not schedule appearance or
disappearance and must not turn a short-lived entity into an unqualified
permanent-current fact. True process and artifact transitions require a future
temporal lifecycle contract.

Ambient presence follows that same boundary for active processes, logins, and
network connections. It may materialize a bounded active process, login, job,
or connection snapshot when compatible direct evidence establishes it, the
claim is lifecycle-qualified, and durable logs or residue give it context. It
may also store a durable recurring pattern that the emulator can render
coherently. Otherwise it must use historical,
relative, or recurring evidence. Typed host time constrains exact dates, and a
recent exhaustive inventory constrains its stated snapshot without becoming a
permanent absence rule. Enrichment cannot later expire an additive current
claim; true live churn belongs to the future `WorldActivityAgent`. This
checkpoint does not add a lexical temporal classifier or ask a model to judge
all prose.

The Expert uses one stable generic prompt and treats the described host as a
real system in the supplied context. It is not permanently framed as a
specialist for any closed family of environments. The routing lens can adapt it
to any evidenced setting, including but not limited to enterprise, home,
research, industrial, embedded, historical, fictional, speculative, ambitious,
or absurd simulations. Stable prompts use contract descriptions and neutral
placeholders rather than a repeated narrow one-shot scenario.

The Expert never receives the planning projection, DREAMLAND generation or
role-play policy objects, expansion directives, state registry paths, patch
schema, trigger or job identifiers, Coordinator workflow fields, or internal
reference bindings. It receives only the deterministic neutral `latitude`
translation of generation liberty. Adding a new DREAMLAND state field therefore
does not expose it to the Expert unless the brief builder deliberately maps it
into this neutral contract. Source field names are translated to canonical
domain labels, nested keys and values containing internal workflow vocabulary
are omitted, and a final recursive validation rejects the complete brief if any
internal marker still remains. A Coordinator question that fails that
validation never reaches the Expert model. Generic domain words such as
`emulator`, `coordinator`, and `integrator` are rejected in routing text but
remain valid scenario evidence when they occur in allowlisted state values;
this preserves real retro-computing and distributed-system descriptions.

Coordinator asks the Expert what is present or occurs in one concrete
realization. It does not ask what an administrator should inspect, what might be
present, or which alternatives are possible. Expert items are proposals for
direct materialization and use affirmative current-world wording. Integrator
may remove advisory or modal framing only when one candidate already names a
single unambiguous detail; it must omit conditional material and unresolved
alternative menus rather than storing them as simultaneous facts. This
normalization cannot invent or select new substance.

Each Expert item carries an explicit `basis` array. An entry such as `r1`
refers only to the supplied reference with that exact ID; the opaque ID does not
assert identity or unstated properties. A relation endpoint such as `@r1` may
still describe causal structure, while unprefixed endpoints name report items,
but relations no longer determine reference use or patch dependencies. Runtime
code never scans model prose to infer a basis.

After the Expert returns, the deterministic Expert parser and item classifier
validate candidate count, origin, basis IDs, novelty, and gates against the
effective latitude. If candidates remain, the runtime captures a fresh current
projection. The visible `resolve_current_references` node then compares each
reference used by an accepted candidate with the same binding in the initial
projection. A reference that disappeared is `basis_reference_missing`; one
whose projected value changed is `basis_reference_changed`.

Currentness filtering is candidate-local. Only candidates whose `basis` names a
missing or changed reference are blocked; candidates with current bases continue.
The eligible-only Expert view, relations, blocked count, and reference bindings
are rebuilt before Integrator. If this removes every candidate, the graph ends
without an Integrator call as `expert_no_eligible_items` at the
`current_reference_filter` stage. A missing or changed reference is never
recovered by label matching or model guesswork, but it also does not discard
independent work.

Each retained binding has shape `{id, kind, path, expected}`: `id` is the same
job-local reference, `path` is its host-relative current-state path, and
`expected` is copied exactly from the fresh projection. Integrator receives the
selected surface, current state, the rebuilt eligible-only candidate view, a
count-based eligibility envelope, and only bindings named by retained item
bases. Blocked items, their conditions, and relations that touch them do not
cross this boundary. This prevents blocked material from being copied and makes
every current-state dependency explicit.

Integrator also receives the foreground authority captured with that fresh
projection, or `null` for a non-active target. It must omit shell, login,
prompt, authentication, access, or current-session material that conflicts
with the direct observation. This precedence is intentionally narrow: it does
not suppress unrelated filesystem, service, process, identity, or operational
depth and does not convert one active login into a universal policy.

The Integrator also receives top-level
`integration_guidance = {"instruction": "<deterministic instruction>"}`. The
same effective liberty that resolves Expert latitude selects this task-neutral
instruction without another model call. It tells Integrator how strongly to
preserve useful eligible material and coherent cross-surface texture when a
safe mapping exists. It does not establish facts, override current state,
authorize unsupported registries, weaken provenance or dependency checks, or
require a patch when a typed no-op is correct. The effective level remains
private and does not cross this model boundary either.

Canonical SHA-256 hashes and character counts are computed for the complete
world, target host, and projected document. Runtime logs contain these hashes
and sizes, not full world-state documents or raw artifact bodies. Enabled
Langfuse traces show each role's distinct prompt boundary: the Coordinator's
planning input, the neutral Expert brief, and the Integrator's fresh state and
bindings.

The planning projection is structurally scoped but does not yet enforce a hard
item, scalar-length, or byte ceiling on the detailed hosts. The logged character
counts make oversized prompts visible during tuning; configurable hard budgets
and deterministic truncation remain deferred.

## Inspectable LangGraph Workflow

The `enrichment-v15` graph has one explicit deterministic Coordinator branch
and remains sequential within each job. It avoids parallel Expert fan-out while
the prompts, latency, and consistency rules are still being tuned. Several jobs
may run concurrently at the service layer; each invocation still follows one
visible graph path.

```text
START
  -> snapshot_initial
  -> prepare_coordinator_context
       -> focus_coordinator_agent
            -> call_focus_coordinator_model
            -> parse_coordinator_output
       OR
       -> ambient_presence_coordinator_agent
            -> call_ambient_presence_coordinator_model
            -> parse_coordinator_output
  -> prepare_expert_brief
  -> expert_agent
       -> call_expert_model
       -> parse_expert_output
  -> classify_expert_items
  -> snapshot_current
  -> resolve_current_references
  -> integrator_agent
       -> call_integrator_model
       -> parse_integrator_output
  -> normalize_integrator_output
  -> validate_integrator_output
  -> END
```

The mode chosen before invocation routes from `prepare_coordinator_context` to
exactly one Coordinator subgraph. Conditional edges stop early when that
Coordinator returns `no_op`, selected
references or Expert-brief preparation are invalid, the Expert returns
`insufficient`, classification finds no eligible Expert candidates, a model
call fails, a role response is invalid, current snapshot capture fails,
currentness filtering blocks every remaining candidate, Integrator returns
`no_op`, structural normalization prunes every contribution, or proposal
validation rejects the Integrator output.

Genuine foreground-evidence drift may enter one separate bounded graph after
the first final-apply attempt releases its lock. That graph reuses the already
validated Coordinator decision and Expert report; it does not call either role
again:

```text
START
  -> snapshot_integrator_refresh
  -> resolve_integrator_refresh_references
  -> integrator_refresh_agent
       -> call_integrator_refresh_model
       -> parse_integrator_refresh_output
  -> normalize_integrator_refresh_output
  -> validate_integrator_refresh_output
  -> END
```

The refreshed result returns to the same service-owned safe-apply boundary.
Conditional edges can end the refresh before the model or proposal when the
fresh target, references, eligible items, model call, parse, normalization, or
validation no longer supports a patch. `enrichment graph` prints both expanded
Mermaid documents, labelled `Main enrichment proposal graph` and `Bounded
Integrator refresh graph (attempt 1)`, so neither role flow must be reconstructed
from service code.

The service captures and projects the initial state immediately before graph
invocation; `snapshot_initial` makes acceptance of that exact input and hash
visible inside the graph. `snapshot_current` invokes the runtime callback that
takes the second lock and projection after Expert completes.

The roles are distinct:

- `prepare_coordinator_context` is deterministic DREAMLAND code. It organizes
  the raw projection into named planning concerns, adds the selected mode,
  structural coverage counts, typed foreground evidence, and ambient-only
  deterministic presence gap, and builds the catalog before one Coordinator
  call.
- The focused Coordinator is a DREAMLAND-aware near-term planner. It sees the
  initial planning projection, trigger context, coverage summary, available
  reference catalog, and bounded foreground authority when the target is
  active. It either selects one semantic surface, one causal cluster, one
  `{domain, perspective}` expertise lens, and the exact grounding reference
  IDs or returns `no_op`.
- The Ambient Coordinator receives the same organized input and returns the
  same schema, but selects one recognizable named human or scenario-supported
  person-equivalent and a connected cluster of activity beyond the foreground
  user's immediate command. Its separately configurable prompt and model make
  that intent inspectable without giving it different write authority.
  `presence-gap-v2` provides one independent-life objective, a deterministic
  preferred existing surface, the exact foreground-user-and-service-account
  exclusion, and bounded anti-repetition hints without asking the model to
  perform bookkeeping.
- `prepare_expert_brief` is deterministic DREAMLAND code, not another model. It
  validates selected IDs, resolves effective creative latitude, and translates
  the plan into the neutral six-field real-world brief, including a neutral
  `active_session` observation when foreground authority exists. Ambient mode
  also appends the versioned human-presence task requirement; focused mode does
  not.
- The Expert is a generic independent subject-matter role. It sees only that
  brief, applies its supplied expertise lens, answers the focused question with
  compact domain material, and has no DREAMLAND state or workflow awareness.
  Host-time and complete-inventory observations occupy the existing neutral
  observation channel; ambient presence-gap policy does not cross this
  boundary.
- `classify_expert_items` is deterministic DREAMLAND code. It exposes which
  candidate items are accepted or blocked by latitude, origin, basis, and gates,
  and stops before Integrator when no supported contribution remains.
- `resolve_current_references` is deterministic DREAMLAND code. It compares
  every reference named by an accepted candidate basis with its initial
  host-relative binding, blocks only candidates that depend on a missing or
  changed reference, and produces current bindings for the retained candidates.
- The Integrator is DREAMLAND-aware. It sees the rebuilt eligible-only Expert
  view, a freshly captured current snapshot, current bindings named by retained
  candidate bases, fresh foreground authority, the eligible item IDs, and
  deterministic non-evidentiary integration guidance, then returns either one
  host-local EnrichmentPatch cluster or an explicit no-op. Ambient jobs also
  supply presence gap as mapping priority so concrete visible entities are not
  unnecessarily flattened into facts; it does not establish or admit an item.
  Deterministic invocation appends the inspectable
  `enrichment_ambient_integrator_suffix.md` under policy
  `ambient-human-integration-v3` only for ambient initial and refresh calls.
  That suffix maps a service-only eligible report, or one lacking a supported
  named individual with connected residue, to `no_safe_mapping` rather than a
  system-centered patch. It also omits material that transfers a directly
  observed foreground row to the proposed person, and it also omits a distinct
  event whose person-specific grounding merely reuses a value learned from
  that row. Independently supported person-centered residue remains eligible;
  a historical or later event must be both explicitly time-qualified and
  independently grounded. The shared Integrator prompt and every
  focused Integrator `SystemMessage` remain byte-for-byte unchanged.
- `normalize_integrator_output` is deterministic DREAMLAND code. It prunes
  unsupported array-valued leaves and newly empty containers, admits only
  unambiguous disjoint `merge_missing` contributions for a shared target, and
  ends as `no_supported_contribution` when pruning removes every contribution.
  It does not make a repair call, invent content, merge source provenance, or
  commit state.
- Deterministic validation and apply remain outside model authority. The graph
  can propose state but cannot commit it. Runtime code privately binds an
  active-session fingerprint to a relevant cluster after model output parsing;
  no role can author or remove that dependency.

### Compact Expert Contract

The Expert report uses schema version 2 and compact JSON with no Markdown. Its
exact top-level keys are
`{v,status,reason,conditions,items,gates,relations}`. The following is a shape
template; angle-bracketed values describe fields rather than a scenario to copy:

```json
{
  "v": 2,
  "status": "ok",
  "reason": null,
  "conditions": [],
  "items": [
    {
      "id": "i1",
      "kind": "<artifact kind>",
      "name": "<concise candidate name>",
      "detail": "<specific net-new detail>",
      "origin": "selected",
      "basis": ["r1"]
    }
  ],
  "gates": [],
  "relations": [["i1", "<relationship>", "@r1"]]
}
```

Each item has exactly `{id,kind,name,detail,origin,basis}` and represents
potential net-new material, not an established reference rewritten as an item.
`origin` is:

- `derived` for a consequence reasonably drawn from supplied context without
  choosing among materially different alternatives. The effective latitude
  determines whether that consequence must be unavoidable, strongly implied,
  or may use broader coherent inference;
- `selected` for one believable concrete choice among several ordinary
  realizations.

`basis` is an explicit array of selected reference IDs. It may be empty when an
item depends only on neutral host or observation context. Every nonempty entry
must exactly match a reference supplied in the brief. The classifier never
discovers a basis by scanning `reason`, `detail`, or relation prose.

Conditions use compact `[id, description]` pairs. Gates connect items to genuine
hard prerequisites represented by condition IDs. Any gated item remains
unresolved, regardless of origin, because the current condition contract does
not provide machine-verifiable state bindings. `derived` is allowed at every
latitude. `selected` is allowed only at `moderate`, `expansive`, and `abundant`.
The effective latitude also imposes the deterministic item limit shown above.
Expert `items` and relations may each contain at most twenty-four entries. The
effective relation limit is injected as `latitude.relation_limit` and follows
the effective candidate limit: 2, 4, 8, 12, or 24. Conditions, gates, per-item
basis references, and Coordinator anchors retain separate eight-entry bounds;
raising connective-topology capacity does not broaden evidence, prerequisite,
or provenance authority.

The initial novelty filter is deliberately exact and explainable. It blocks a
same-kind candidate only when both its normalized name and detail exactly repeat
the selected reference label or scalar observation. A matching name with
genuinely net-new detail remains eligible, as does useful cross-kind material.
It does not claim full semantic duplicate detection across arbitrary state.
Integrator still compares accepted candidates with the fresh host projection
and may return `already_materialized`; richer deterministic novelty analysis is
a follow-up measurement area.

Integrator receives an eligibility envelope with accepted IDs, blocked counts,
and unresolved-condition counts. Only accepted items and relations whose item
endpoints are accepted cross the boundary. After the fresh snapshot, a
candidate-local currentness filter can further remove items whose basis
references changed or disappeared. Current reference bindings are selected from
the union of the retained item bases. When no item remains at either filter,
`expert_no_eligible_items` ends the graph without an Integrator call.

Relation endpoints beginning with `@` must be exact IDs from the brief's
references; other endpoints must be IDs from `items`. Every relation must
involve at least one returned item, so a relation cannot connect two supplied
references directly. When a relation connects an item and `@reference`, the
unprefixed reference ID must occur in that item's `basis`. Relations retain a
bounded causal structure but do not establish provenance or dependencies.

Relation policy `expert-relations-v2` validates relation entries independently
in authored order, discards invalid entries, removes exact duplicate triples,
then retains the first valid unique entries up to the effective
`latitude.relation_limit`. Later overflow relations are discarded without
discarding an otherwise valid report. The Expert therefore puts important
hierarchy, attachment, ownership, lifecycle, and causal backbone relations
before optional cross-links. The direct strict parser still rejects a relation
array above the hard twenty-four-entry contract; tolerant normalization is an
explicit graph behavior, not a weakening of the public parser.

When DEBUG logging is enabled, each discarded invalid, duplicate, or overflow
relation emits one bounded diagnostic with its original index, stable rule, and
validation location, without logging model-authored relation content. The
default-visible aggregate warning retains the effective limit and received,
accepted, invalid, duplicate, overflow, and total discarded counts; individual
discard diagnostics are not promised at higher log levels. The deterministic
boundary never invents a missing basis entry or changes a relation. JSON
parsing, top-level shape, status, items, bases, gates, and their limits remain
unchanged by this policy. An `insufficient` report cannot become valid through
relation filtering. `ok`
requires at least one item and a null `reason`; `insufficient` requires a short
functional reason plus empty condition, item, gate, and relation arrays. This
shape is optimized for compact low-token responses while remaining readable in
traces and independent of DREAMLAND's patch schema.

## Lock Sequence And Accepted Race

No session lock is held during a model call. One full job follows this exact
sequence:

1. Accept and enqueue a trigger without changing world state.
2. Acquire the session lock for `snapshot_initial`, deep-copy state and session
   metadata, capture bounded foreground authority when the target is active,
   capture typed foreground evidence from target-host history, then release it.
3. Project the copy, enter `prepare_coordinator_context`, organize the planning
   input, build the structural coverage summary and available reference
   catalog, and run the DREAMLAND-aware focused or Ambient Coordinator selected
   by the job's deterministic mode.
4. Enter `prepare_expert_brief`, validate the Coordinator's selected anchor
   IDs, resolve and inject deterministic creative latitude, construct the
   neutral six-field brief, and run the Expert against that brief only.
5. Enter `classify_expert_items`. Stop as
   `expert_no_eligible_items` if no candidate is admitted by the effective
   latitude, origin, basis, and gate rules.
6. Enter the visible `snapshot_current` graph node.
7. Acquire the session lock for `snapshot_current`, deep-copy the latest state,
   recapture bounded foreground authority and typed foreground evidence, then
   release it. Projection and drift logging follow outside the lock.
8. Enter `resolve_current_references`, compare the reference IDs used by accepted
   candidate bases with their initial bindings, and block only candidates that
   depend on a missing or changed reference. If any candidates remain, run
   Integrator against the selected surface, current projection, rebuilt
   eligible-only candidate view, count-based eligibility envelope, bindings
   named by retained item bases, and fresh foreground authority. Integrator may
   return a tagged patch or tagged no-op.
   DREAMLAND injects the exact target-host basis from this snapshot into the
   parsed patch; the model cannot supply or replace that trusted basis. For a
   cluster related to current-session semantics, deterministic code also binds
   the current authority fingerprint after parsing; the model cannot supply,
   serialize, or remove it. Deterministic code similarly binds the current
   typed foreground-evidence digest to every focused or ambient patch,
   including a stable digest for an empty evidence view.
9. Validate the proposal and each contribution's provenance. In `shadow` mode,
   record a valid patch and stop here.
10. In `safe` mode, acquire the session lock for `patch_apply`, verify that the
    live registration is still the same session object, recapture foreground
    authority and recognized foreground evidence for bound dependencies, and
    apply the patch against the then-current world state.
11. If the complete cluster is skipped solely because the
    `foreground_evidence` authority changed, the target is still the active
    foreground host, and the validated refresh basis is available, release the
    apply lock and invoke the separate Integrator-refresh graph exactly once.
    It captures another current snapshot, resolves the original Expert report's
    references again, calls only Integrator, normalizes and validates the new
    output, and binds its fresh authority dependencies.
12. Send a refreshed patch through the ordinary `patch_apply` lock and checks.
    A second conflict is terminal. An `active_session` conflict, including a
    real host pivot, is terminal without a refresh.
13. If state changed, replace the session world state, force a full
    command-agent state refresh on the next command, release the lock, and
    publish a named background-agent `state.materialized` event.

Manual or underspecified event targets may require an additional short
`manual_target` or `trigger_target` lock before job acceptance to resolve the
active host. User command execution coordinates through the same session lock,
so an enrichment snapshot or apply can still wait behind an in-progress
command. Every wait is logged, and a wait of at least 100 milliseconds is
elevated to `WARNING`; provider work is never moved inside the lock.

State can change after the current snapshot in step 7 and before final apply in
step 10. The initial experiment deliberately accepts that race instead of
serializing model latency behind the session lock. The final apply is therefore
not a blind merge: it compares every declared dependency and touched path with
the trusted Integrator basis and the live state under the apply lock. Current
state wins on conflict, the affected causal cluster is skipped, and unrelated
world or host drift does not block safe additions. Proper ancestors are checked
as well, so a registry or nested object deleted after the Integrator snapshot
is never silently recreated by a later leaf addition.

Dependency matching at final apply is projection-aware without treating every
extra live key as harmless. The current-snapshot callback privately retains the
complete target-host copy outside all role inputs and serialized patch output.
After dependency validation, deterministic code places only a SHA-256
fingerprint of each complete dependency value on the in-memory patch. Apply
requires the projected expected fields to match and the complete live value to
retain that fingerprint. An unchanged artifact body or other field deliberately
omitted from the projection therefore does not create a false conflict, while a
new field or any visible or omitted value changed after the snapshot produces
`dependency_changed`. The full values and fingerprints are not runtime log
fields or model-authored telemetry.

World-state dependencies cannot represent a change to the live shell frame,
because the frame is session state rather than a host registry value. For
clusters selected on `shell`, `identity`, or `service`, writing user/group
registries, or containing deterministic current-session markers, DREAMLAND
therefore adds one runtime-owned `active_session` authority dependency when
foreground evidence is available. Service is included because SSH daemon match
rules and forced commands directly control a live session but have no single
stable registry shape. The SHA-256 digest covers login and active usernames,
host identity, the complete credential-sanitized interactive-program command
and argument list, selected shell variables, and stack depth; it is not limited
to the shorter public argument projection. It excludes cwd so ordinary
directory movement does not invalidate unrelated work. The digest remains in a
runtime sidecar until traced LangGraph execution has ended, then is attached
only to the in-memory patch. Safe apply recaptures that digest under the final
lock; absence or change skips the atomic cluster as
`semantic_authority_conflict`.

This is a typed temporal guard, not universal semantic validation. It protects
the observed active-session domain and known related proposal shapes. It does
not prove that every free-text fact elsewhere in world state is compatible, and
it does not grant Enrichment authority to correct an already materialized
description. The bounded foreground payload and its digest never appear in
ordinary logs, and the private digest does not enter role inputs, role outputs,
or traced graph state; only presence, change, domain, policy, binding reason,
and outcome metadata are retained.

Focused and ambient patches add a second runtime-owned authority domain,
`foreground_evidence`, under policy `foreground-evidence-v2`. The current-
snapshot callback computes a private SHA-256 digest from the latest complete
strictly recognized record for every exhaustive evidence scope across complete
target-host history, plus the latest recognized host-time record, before public
excerpt clipping. The empty evidence view has a stable digest as well. The graph
carries only the public bounded observations; the private digest stays outside
traced graph state and serialized model output. After the graph returns,
deterministic code attaches that digest to the in-memory cluster. Safe apply
recaptures the private evidence view under the final lock and reports
`semantic_authority_conflict` if the digest changed. This includes the race
where the user's first or newer recognized observation arrives after the
Integrator snapshot. Unrelated commands and public four-command-window expiry
do not change authority. This protects the Integrator-snapshot-to-apply window
without treating arbitrary prose as temporal evidence or aged-out inventories
as permanent model-visible absence claims.

One genuine `foreground_evidence` conflict can reuse the already validated
Coordinator decision, Expert report, selected references, eligible-item set,
creative latitude, unresolved conditions, and ambient presence guidance in the
separate Integrator-refresh graph. It captures fresh state and authority,
re-resolves the original references, and asks only Integrator to remap the
eligible material. The refreshed patch receives the fresh basis and then faces
normal normalization, provenance, dependency, touched-path, atomicity, and
final authority checks. This is not stale-digest rebinding or partial salvage.
The service permits attempt 1 only; a second conflict is terminal. Any conflict
set containing `active_session`, any non-foreground target, or unavailable
refresh basis is ineligible.

## Foreground SSH Client Authentication Paths

The foreground interaction owns SSH client authentication setup for every
host/home it has operated. Runtime-only `SessionState` path sets accumulate
authoritative shell-frame `HOME/.ssh` directories and explicit `ssh-keygen -f`
and `ssh -i` paths, including custom filename answers and recognized key-path
metadata from active `ssh-keygen`. Capture runs before commands, after accepted
state updates, at enrichment snapshots, and again under the final apply lock.
Only paths are retained; password and passphrase inputs are not interpreted as
paths. Cwd alone never claims another user's home. Scope is host-local and
survives shell exit, account changes, host hops, and command-history compaction
for the lifetime of that session. These sets are excluded from model state,
saved profiles, and checkpoints.

Safe apply rejects a complete causal cluster as
`foreground_authentication_conflict` if any filesystem contribution targets a
reserved explicit key path (or its public half), or a recognized client artifact
under a reserved `.ssh` directory: `config`, `config.d/*`, `known_hosts*`, or
`id_*`. The `.ssh` and `config.d` directory records are also protected so nested
artifact descriptions cannot bypass the boundary. Missing paths are protected
too. The guard prevents enrichment from
inventing a passphrase-protected key or selecting it in SSH config while the
user is setting up access. It does not delete or alter scenario-seeded or
foreground-created artifacts, and unrelated artifacts and other users' homes
remain eligible. Both an already-known boundary and one established after the
Integrator snapshot are checked at application time; no model-authored
dependency is needed.

Prompts also prohibit inventing the foreground user's completed key generation,
passphrase choice, credential setup, or fingerprint verification as notes,
history, or facts. Established scenario credentials and unrelated actors'
coherent history remain valid world-building context. Runtime path protection
does not validate arbitrary prose, interpret shell pipelines, expand variables,
follow filesystem symlinks, recognize every custom SSH configuration include,
or enforce a general credential policy. Its explicit path recognition currently
uses POSIX paths. Model evaluation remains necessary for semantic claims beyond
this bounded mutation contract.

## Shadow And Safe Apply Modes

`apply_mode = shadow` runs the selected focused or Ambient Coordinator,
Expert-brief preparation, Expert, deterministic candidate classification,
current snapshot capture, Integrator, and validation, then records
`shadow_mode` without mutation. Use it to tune prompts, triggers, modes, models,
creative latitude, candidate novelty, evidence fit, and latency.

Integrator output is a schema-version-2 tagged union. A patch has the exact
root shape `{v, status, target_host, clusters}` with `status = "patch"`. A
no-op has `{v, status, reason_code}` with `status = "no_op"` and one of:

- `already_materialized`;
- `no_safe_mapping`;
- `no_supported_contribution`.

This no-op branch lets Integrator decline work when candidate material is
already present or cannot be mapped safely. The graph records it as
`integrator_no_op`; it is a normal terminal outcome, not an invalid patch.

Every contribution in a patch carries a nonempty `source_item_ids` array.
Validation requires each source ID to name an Expert candidate and to remain in
the accepted set after currentness filtering. An unknown or blocked source
rejects the whole proposal as `integrator_provenance_invalid`. Provenance does
not replace current-state dependencies: it records which accepted candidate led
to a contribution, while dependencies and touched paths protect final apply
against races. Every reference binding named in a retained candidate's `basis` is
supplied to Integrator. For a proposed patch, deterministic validation takes
the union of bases for the accepted candidates actually cited in contribution
`source_item_ids`; each binding in that source-specific union must appear in the
cluster with its exact path and expected value. A basis used only by an accepted
but unused candidate is not mandatory for that patch. A missing or mismatched
required dependency rejects the proposal as
`integrator_reference_dependency_invalid`; the Integrator cannot make this
race check optional.

Safe application governs mutation authority, atomicity, dependencies, and
conflict preservation. It is not a proof-only content policy. A `selected`
candidate is admissible at moderate and higher latitude even though the exact
detail was not previously materialized; making and retaining that believable
choice is enrichment's purpose. Existing facts and operator constraints still
bound the choice, and current state wins every apply conflict.

`apply_mode = safe` permits only additive contributions to these host-relative
keyed registries:

| Registry path | Intended content |
| --- | --- |
| `services_by_name` | Established host services |
| `filesystem.artifacts_by_path` | Materialized filesystem artifacts |
| `users_by_username` | Host-local user records |
| `groups_by_name` | Host-local group records |
| `facts_by_id` | Keyed operational and environmental facts |

Filesystem artifact keys must use concrete absolute syntax supported by the
evidenced platform. Safe application recognizes POSIX and Windows absolute
paths and rejects their common relative forms, home shorthand, and guessed
working directories. Unfamiliar platform-native keys are not rejected merely
for falling outside a closed path taxonomy.

Two operations are accepted:

- `put_if_absent` creates a keyed object only while that key is absent. An
  identical live object is `already_satisfied`; a different existing value is
  never replaced.
- `merge_missing` adds absent object leaves to a new or existing keyed object.
  Identical leaves are already satisfied; existing or newly populated
  conflicting leaves are never overwritten.

The `enrichment-v15` graph requires exactly one causal cluster with at most
twenty-four contributions. Every cluster is atomic: if a dependency or
contribution conflicts, none of its other contributions commit. Contribution
values may contain finite JSON scalars and objects, but no arrays. Delete/replace
directives, unsupported operations, incompatible duplicate targets, empty
values, missing hosts, and non-object parents are rejected or skipped. The apply
function does not mutate either of its inputs.

Before final proposal validation, the visible deterministic
`normalize_integrator_output` node applies structural policy
`integrator-output-normalization-v1`. It removes array-valued leaves without
reading or translating their contents, recursively removes objects made empty,
and drops a contribution only when no value remains. Exact scalar and object
siblings are preserved. If all contributions are removed, the workflow ends as
`no_supported_contribution` without a repair model call.

Several contributions may retain the same registry path and key only when each
uses `merge_missing` and their flattened desired scalar leaf paths are pairwise
disjoint with no prefix collision. Their contributions, IDs, and source item
lists remain separate. This permits a large causal artifact to retain more than
eight total Expert sources without weakening the existing maximum of eight
sources for any one contribution. Any shared scalar leaf, parent/child path
collision, `put_if_absent`, or other duplicate-target combination remains
`duplicate_contribution_target`. Normalization does not add a target, operation,
value, source ID, or dependency, and the resulting cluster remains atomic.

Arrays never enter state: graph normalization prunes them, while direct
safe-apply validation still returns `forbidden_array` if another caller supplies
one. Deterministic normalization logs record structural paths and counts,
empty-container and dropped-contribution counts, compatible duplicate counts,
the policy version, and the terminal result without array contents or
model-authored explanation.

Apply outcomes use stable reason codes:

- successful/idempotent aggregation: `applied`, `already_satisfied`, and
  `partially_applied`;
- cluster aggregation: `all_clusters_skipped` and `cluster_atomicity`;
- race and dependency checks: `dependency_changed`,
  `dependency_basis_mismatch`, `existing_value_conflict`,
  `semantic_authority_conflict`, and `touched_path_changed`;
- authority and shape checks: `forbidden_array`, `forbidden_directive`,
  `forbidden_registry`, `unsupported_operation`, `wrong_parent_type`, and
  `target_missing`;
- contract checks: `invalid_patch`, `invalid_cluster`,
  `invalid_contribution`, and `duplicate_contribution_target`.

Graph rejection codes separately identify the failed boundary, including
`patch_proposed`, `coordinator_no_op`, `coordinator_model_failed`,
`coordinator_output_invalid`, `coordinator_context_invalid`,
`expert_brief_invalid`, `expert_model_failed`, `expert_output_invalid`,
`expert_insufficient`, `expert_no_eligible_items`,
`current_snapshot_capture_failed`,
`current_snapshot_invalid`, `current_reference_invalid`,
`integrator_model_failed`, `integrator_output_invalid`, `integrator_no_op`,
`integrator_provenance_invalid`,
`integrator_reference_dependency_invalid`, `integrator_target_mismatch`,
`integrator_cluster_count_invalid`, `integrator_forbidden_registry`, and
`integrator_limit_exceeded`.

`current_reference_invalid` is reserved for a malformed or otherwise invalid
reference-resolution boundary. An ordinary missing or changed basis reference
is a candidate-local filter result, not a graph rejection; it appears through
`basis_reference_missing` or `basis_reference_changed` counts and can lead to
the `expert_no_eligible_items` no-op only when every remaining candidate is
blocked.

Trigger gates use the stable decision reasons `eligible`,
`source_self_suppressed`, `duplicate_coalesced`, `inflight_limit_reached`,
`cooldown_active`, and `rate_limit_reached`. Runtime pre-gate suppressions add
specific reasons such as `automatic_disabled`, `focus_threshold_not_reached`,
`higher_priority_correlated_candidate`, `provider_circuit_open`,
`trigger_not_configured`, and `session_missing` rather than a generic skip.
Correlated suppression records the selected event type separately as
`higher_priority_trigger`. Deferral is a scheduler lifecycle, not an accepted
job or a new bypass around those gates;
stored, replaced, retained, released, expired, cancelled, discarded, and
finally scheduled outcomes retain their specific reasons. Oversized or
duplicate anchor sets produce `trigger.anchors_bounded` with retained and
omitted counts.

`partially_applied` and `all_clusters_skipped` are patch-library aggregation
codes for callers that supply several clusters. The current graph accepts
exactly one cluster, so runtime jobs normally expose that cluster's direct
reason instead.

## Logging And Langfuse

Runtime events use the `dreamland.enrichment` logger and the configurable
`enrichment` logging category. The shared bus logs under
`dreamland.enrichment.events`, which is included by the same category.

Context-bearing events are copied by a dedicated logging handler into a bounded
in-memory deque per session, independent of the shared runtime-log ring. Each
record retains structured sequence, session id, optional job id, event name,
level, and the unchanged grep-friendly line. It also retains bounded
first-class operational fields when present: event and causation IDs,
correlation and trigger IDs, enrichment mode, selected Coordinator role,
command ID, target host,
requested/selected/effective surfaces, status, `reason`, `reason_code`, trigger
candidate and coalescing IDs, `replaced_candidate_id`,
`retained_candidate_id`, `completed_job_id`, `higher_priority_trigger`,
`retained_trigger`, retained mode, probe kind, subject and cooldown-scope hashes,
interaction pulse and retained-pulse counts, total and per-mode in-flight counts before and after
admission, configured in-flight maximum, `focus_key_sha256`, match policy and classification,
command/retained focus hashes and counts, bounded provider failure and circuit
fields, authority domain/policy/action fields, `priority`, `retained_priority`,
`age_ms`, `max_age_ms`, `focus_path_count`, `focus_artifact_count`, authority
presence/change flags, the boolean `accepted` result, and normalized patch
registry targets. Presence-gap policy, hash, selected observable, coverage and
repetition counts, plus foreground-evidence policy, hash, observed-time flag,
and exhaustive-evidence count are retained when present. It does not promote
Coordinator `reason_preview` or other model-authored prose. Positive integer
`[enrichment] event_log_capacity`, default 10,000, independently sizes each
session's enrichment deque; `[console] log_capacity` continues to size ordinary
console and session-trace buffers. Admin REST and MCP can query one exact session
and optional exact job and receive retained/evicted counts plus sequence bounds,
even after the live session closes. Records do not survive process restart.
This handler derives metadata from normal deterministic logging; it adds no
model call, prompt field, output token, or model-authored reasoning burden.

Deferred lifecycle records have a candidate id but no job id. They are visible
through the session-wide query until a successful release associates the
candidate with a newly accepted job. `deferred.scheduled` carries that job id
and `completed_job_id` for the job whose completion opened the release attempt.
Exact job filtering starts with the scheduling record and does not
retroactively rewrite earlier candidate-only records.

`runtime.started` records the complete configured trigger set, deterministic
trigger-to-mode mapping and priority table, four-command interaction-pulse
threshold, three-job default ceiling, and 10,000-record event capacity. There
is no pulse interval setting. Admission logs retain total and per-mode in-flight
counts so operators can distinguish unused capacity from mode-specific trigger
suppression without asking a model to explain scheduling.

The main event families are:

- `runtime.*`, `session.*`, and `idle.*` for service lifecycle and timers;
- `interaction_pulse.counter_reset` for the only event that clears accumulated
  pulse progress; it records the accepted-pulse reason and prior count;
- `bootstrap.retry_stored`, `bootstrap.retry_replaced`,
  `bootstrap.retry_retained`, `bootstrap.retry_attempted`,
  `bootstrap.retry_admitted`, `bootstrap.retry_cancelled`, and
  `bootstrap.retry_failed` for the separate active-host eventual-admission
  marker;
- `trigger.considered`, `trigger.accepted`, `trigger.suppressed`, and
  `trigger.coalesced` for every decision path;
- `deferred.stored`, `deferred.replaced`, `deferred.retained`,
  `deferred.released`, `deferred.scheduled`, `deferred.expired`, and
  `deferred.discarded` or `deferred.cancelled` for the bounded automatic
  attention position;
- `job.queued`, `job.started`, `job.completed`, and `job.failed` for job
  lifecycle and timing;
- `graph.started` and `graph.completed` for the nested proposal graph
  lifecycle, kept distinct from the service-owned `job.*` lifecycle;
- `enrichment.ready`, `enrichment.late`, and
  `enrichment.command_matched` for trigger-to-ready time, work that was not
  ready when a matching command arrived, and available enrichment matched to a
  later command; late and matched records retain
  `interaction-focus-match-v1`, the exact/related/surface-only classification,
  both bounded focus hashes, and both focus subject counts;
- `provider_circuit.opened`, `provider_circuit.reset`, and
  `provider_circuit.ignored` for process-local per-provider terminal
  suppression, service-restart clearing, and bounded DEBUG explanations for
  invalid foreground failure metadata or a provider the enrichment graph does
  not use; automatic admission/preflight suppression, idle cancellation, and
  deferred cancellation use `reason=provider_circuit_open`; the reset is a
  process-level log without session context, while open/suppression/cancellation
  records retain their originating session when one exists;
- `lock.waiting`, `lock.acquired`, and `lock.released` for each short lock;
- service-owned `snapshot.captured`, graph-owned `snapshot.accepted`, and one
  service comparison event: `snapshot.compared` at `DEBUG` when neither world
  state, target host, nor foreground authority changed, or `snapshot.drifted`
  at `INFO` when any of them changed;
- `reference.catalog_prepared`, `coordinator.context_prepared`,
  `expert_brief.prepared`, `integrator.guidance_prepared`, and
  `reference.bindings_resolved` for deterministic role handoffs; Expert-brief
  preparation records configured/effective latitude, defaulting, template
  version, candidate and relation limits, relation-policy version, instruction
  length, and instruction hash. Ambient Expert-brief preparation additionally
  records `policy_version=ambient-human-presence-v3`,
  `action=ambient_human_expert_constraint_appended`, and bounded
  `policy_chars` and `policy_sha256` diagnostics; only version and action are
  projected into retained typed event records. Focused preparation emits none
  of those policy fields.
  Coordinator preparation also records
  `presence_gap_policy_version`, `presence_gap_sha256`,
  `presence_gap_ambient_objective`, `presence_gap_preferred_surface`, and the
  two bounded `presence_gap_anti_repetition_*_count` fields without
  state-derived names; structural counts remain in the separate coverage
  summary;
  Integrator-guidance preparation records the private effective latitude,
  template version, integration-instruction length, and instruction hash.
  Ambient initial and refresh preparation also records
  `ambient_integration_policy_version=ambient-human-integration-v3`,
  `ambient_integration_policy_sha256`, and
  `ambient_integration_policy_chars` as formatted and trace diagnostics rather
  than typed retained fields; focused preparation emits none of those suffix
  fields;
  `handoff.rejected` records a stable boundary and validation code without
  logging the rejected content;
- `reference.selection_evaluated`, `reference.usage_evaluated`,
  `reference.currentness_evaluated`, `condition.resolution_evaluated`, and
  `expert.items_evaluated` for structured evidence selection, explicit
  candidate-basis use, initial/current reference comparison, origin counts, and
  item eligibility; `expert.report` also records the candidate limit, unused
  capacity, and whether the Expert reached its limit; bounded
  `reference.selection_detail`,
  `reference.usage_detail`, `reference.currentness_detail`,
  `condition.resolution_detail`, and `expert.item_decisions` records retain
  per-ID detail at `DEBUG`;
- `expert.relations_evaluated` for received, accepted, invalid, duplicate,
  overflow, and total discarded relation counts, the effective relation limit,
  and relation-policy version; when any relation is discarded, this aggregate
  is a default-visible warning. Each discarded relation produces a bounded
  DEBUG `expert.relation_discarded` detail event with original index, stable
  rule, validation code, and location but no relation text; per-entry details
  are not guaranteed to be retained when DEBUG logging is disabled;
- `role.started`, `role.completed`, `role.accepted`, `role.rejected`, and
  `role.failed` for model and parser boundaries;
- `integrator.decision` and `workflow.no_op` for tagged patch/no-op decisions
  and deterministic early exits;
- `integrator.output_normalized` for the structural normalization policy,
  pruned paths and counts, compatible duplicate-target counts, and an
  all-pruned `no_supported_contribution` result without a repair call;
- `patch.provenance_validated`, `patch.provenance_detail`, and
  `patch.provenance_rejected` for contribution-to-Expert-item linkage. The
  validated event records eligible, sourced, and unsourced counts, contribution
  capacity, registry counts, common-separator path-segment depth, proposed
  ancestor-link counts, and maximum proposed ancestor-chain length without
  retaining model-authored paths or spending model tokens;
- `patch.reference_dependencies_validated`,
  `patch.reference_dependency_detail`, and
  `patch.reference_dependency_rejected` for mandatory direct-reference race
  protection;
- `authority.dependency_bound`, `authority.dependency_unavailable`, and
  `authority.semantic_conflict` for runtime-owned active-session and ambient
  foreground-evidence dependencies and final safe-apply rejection without
  logging authority content or private fingerprints;
- `integrator.refresh.decision`, `integrator.refresh.started`,
  `integrator.refresh.graph_started`, `integrator.refresh.graph_completed`,
  `integrator.refresh.patch_proposed`, `integrator.refresh.completed`, and
  `integrator.refresh.failed` for the one-attempt recovery boundary. Decision
  logs retain `integrator_refresh_attempt`, `eligible`, reason, action, and the
  singleton `authority_domain`; start logs state that Coordinator and Expert
  are reused; graph logs retain v14 status; completion retains
  `integrator_refresh_outcome`, reason, action, `changed`, and
  `integrator_refresh_latency_ms`. A formatted `conflict_domains` field can
  explain multi-domain ineligibility without authority values. Failures retain
  only bounded exception type and latency, not authority data or model
  reasoning;
- `patch.proposed`, `patch.validated`, `patch.rejected`, `patch.applied`, and
  `patch.skipped` for the write boundary;
- `event.published` and `event.delivery_failed` for the reusable event bus.

Exceptional scheduling or worker escapes use `job.submit_failed` or
`job.exception` as diagnostics, followed by exactly one terminal job event.
Initial target disappearance uses `snapshot.rejected` with `target_missing`
rather than being reported as an unhandled exception.

Common correlation fields are `session_id`, `job_id`, `event_id`,
`caused_by_event_id`, `correlation_id`, `trigger_id`, `command_id`,
`target_host`, `enrichment_mode`, `requested_surface`, `selected_surface`, and
`surface`.
`requested_surface` is the trigger, event, or manual hint;
`selected_surface` is Coordinator's canonical work selection; `surface` remains
the effective selected-or-requested compatibility field. Completed-job
readiness and foreground matching use Coordinator's selected surface when it is
available and fall back to the requested hint before selection. `patch.proposed`
separately records unique host-relative `registry_targets` and contribution
counts by registry. Registry targets describe where a patch maps material, not
another semantic surface. Trigger logs add `policy`, `metric`, `observed`,
`threshold`, `retry_after_ms`, candidate id, priority, age, deferred or replaced
candidate id, coalescing target, and release or cancellation reason. Retained
scheduler metadata exposes `replaced_candidate_id`, `retained_candidate_id`,
`completed_job_id`, `higher_priority_trigger`, `retained_trigger`, `priority`,
`retained_priority`, `age_ms`, `max_age_ms`, and `accepted` when present. Focus
logs expose `focus_key_sha256`, `focus_path_count`, and
`focus_artifact_count` without foreground content or model-authored
description. Match logs additionally expose `match_policy`,
`match_classification`, the causal job's expected focus hash/counts, and the
accepted command's observed focus hash/counts. No raw paths are logged.
Authority logs expose bounded policy, domain, binding,
dependency-count, presence/change, and action fields without the authority
payload or digest. Provider diagnostics expose bounded `failure_kind`,
`failure_disposition`, provider/model identifiers, safe status/code fields,
circuit origin, and circuit count without retaining provider-authored prose or
response bodies. Model logs add graph version, role, provider, model, configured
reasoning/verbosity/output limit, response size, and latency. The separate
`tokens` category receives a per-role callback labelled with enrichment job id
and role. Lock logs add stage,
wait time, held time, and the long-wait flag. Snapshot and patch logs add hashes,
sizes, drift flags, registry paths, proposal bytes, cluster outcomes, touched
paths, conflict paths, and stable reason codes. Enrichment readiness and
command-match logs add
`trigger_to_ready_ms`, `elapsed_ms`, or `lead_time_ms` as appropriate. A shadow
proposal can be enrichment-ready for latency measurement even though it did not
mutate state.

`snapshot.captured` reports bounded command-evidence telemetry as
`command_evidence_count`, `command_evidence_chars`,
`command_evidence_truncated_count`, `command_evidence_omitted_count`, and
`foreground_authority_present`. Snapshot and Coordinator handoff events also
report `foreground-evidence-v2`, a public-contract hash,
`host_time_observed`, and `recent_exhaustive_evidence_count` as typed fields.
The ordinary formatted line may also show bounded generic evidence kinds; they
are not promoted into structured retained/admin fields, and no raw command
output is logged.
`expert_brief.prepared` reports `command_evidence_count`,
`same_surface_command_evidence_count`, `command_evidence_chars`,
`command_evidence_truncated_count`, and
`command_evidence_selection = surface_then_recency`. These counts are
deterministic. Runtime logs do not include the command excerpts; the role inputs
remain inspectable in Langfuse when tracing is enabled.

Evidence and provenance logs are generated from parsed role objects and
deterministic classification and validation results. They do not add model
calls, ask models to narrate their reasoning for logs, or increase role output
schemas with verbose telemetry fields. INFO records keep bounded counts,
candidate-origin aggregates, basis-reference counts, and reason-code
aggregates; per-ID decisions and contribution mappings remain bounded DEBUG
detail. Reference use comes only from each Expert item's structured `basis`
array. Runtime code does not scan model-authored reason, detail, or relationship
text to infer evidence use.

`coordinator.decision` includes a one-line, 160-character `reason_preview`, the
complete reason length, and an anchor count so operators can align a planning
decision with user experience. It also identifies the focused or Ambient
Coordinator role and enrichment mode. `coordinator.context_prepared` records
the deterministic coverage counts and their canonical hash. The selected
anchor IDs are isolated in bounded `coordinator.decision_detail` at `DEBUG`.
The preview is model-authored content
and may repeat a bounded piece of supplied state; full role prompts and
responses remain trace data rather than runtime log fields.

Expected high-volume detail, such as trigger consideration, ordinary short
locks, drift hashes, timer changes, and routine suppression, is `DEBUG`.
Accepted/coalesced work, cooldown/rate/in-flight pressure, job lifecycle,
successful model calls, snapshots, and patch outcomes are `INFO`. A lock wait of
at least 100 milliseconds, provider failure, invalid role output, invalid patch,
or event-subscriber failure is `WARNING`. An unhandled runtime exception is
`ERROR`.

Provider failures are classified deterministically before they reach circuit
policy. Known exhausted credit/quota, authentication, authorization, and
configuration conditions are terminal; ordinary rate limits, timeouts, and
unavailability are transient; unrecognized failures remain unknown. Structured
provider codes take precedence over HTTP status so terminal quota or credit
exhaustion reported as 429 is not logged as ordinary throttling. The
classification retains bounded identifiers only. It never copies a provider
message or response body into enrichment-event fields and requires no role to
generate an explanation.

When Langfuse is enabled, the runtime owns a parent
`dreamland-enrichment-agent` run grouped under the same stable session id as
the live DREAMLAND session. The proposal workflow is its child
`dreamland-enrichment-proposal-graph`. The parent remains open through
deterministic shadow or safe application and closes with graph status/reason,
application mode/status/reason/change, selected surface, and total job duration.
Safe application remains runtime-owned and outside model authority. Trace
metadata includes the graph version, job, trigger, event, target, optional
command/surface, enrichment mode, deterministic coverage summary and hash,
presence-gap summary metadata where applicable, user, and state-profile
identifiers. The explicit focused and ambient
Coordinator subgraphs and `prepare_coordinator_context`,
`prepare_expert_brief`, `snapshot_current`, `resolve_current_references`,
`classify_expert_items`, and `validate_integrator_output` nodes remain visible
along with `snapshot_initial`, and each role receives token-usage callbacks. The
trace therefore exposes mode routing, role separation, deterministic latitude, candidate
filtering, basis use, deterministic Integrator retention guidance, and the
tagged Integrator decision without exposing DREAMLAND state inside the Expert
prompt.
An eligible authority refresh appears as the separate child workflow
`dreamland-enrichment-integrator-refresh-graph`, tagged `integrator-refresh`
with attempt 1. Its nodes expose fresh snapshot capture, reference resolution,
Integrator, normalization, and validation; no Coordinator or Expert node is
present.
Each invocation gets a unique LangGraph thread id for trace and invocation
isolation. Checkpoint persistence is deliberately disabled in this experiment.

## Configuration

Use sanitized provider-qualified model references. The example configuration
uses GPT-5.6 Terra with low reasoning and low verbosity for each role, while
retaining independent tuning controls:

Each role also accepts optional `openai_api`, `openai_service_tier`, and
`openai_reasoning_mode` controls, with its usual role-specific environment
prefix. Anthropic and xAI ignore those OpenAI-only controls with an
informational log. OpenAI `max` effort and Anthropic's native `max` mapping are
available; unsupported xAI `max` is logged and omitted. See
[Model Providers](OPERATIONS.md#model-providers) for values and restrictions.
Role log and trace fields label these as requested settings, while token logs
report the actual service tier when the provider supplies it. The enrichment
graph's state authority, prompts, scheduling, and apply rules are unchanged.

```ini
[enrichment]
enabled = true
apply_mode = safe
triggers = host_discovered,active_host_changed,host_presence_bootstrap,presence_probe,subject_first_touch,state_materialized,materialization_followthrough,surface_focus,interaction_pulse,session_idle
idle_delay_ms = 5000
cooldown_ms = 30000
max_jobs_per_minute = 6
max_inflight_per_session = 3
event_log_capacity = 10000

[enrichment.coordinator]
model = openai:gpt-6-luna
openai_service_tier = fast
reasoning_effort = low
reasoning_summary =
verbosity = low
max_output_tokens = 2048
system_prompt = enrichment_coordinator.md

[enrichment.ambient_coordinator]
model = openai:gpt-6-luna
openai_service_tier = fast
reasoning_effort = low
reasoning_summary =
verbosity = low
max_output_tokens = 2048
system_prompt = enrichment_ambient_coordinator.md

[enrichment.expert]
model = openai:gpt-6.1-sol
openai_service_tier = fast
reasoning_effort = low
reasoning_summary =
verbosity = low
max_output_tokens = 2048
system_prompt = enrichment_expert.md

[enrichment.integrator]
model = openai:gpt-6-luna
openai_service_tier = fast
reasoning_effort = low
reasoning_summary =
verbosity = low
max_output_tokens = 4096
system_prompt = enrichment_integrator.md

[logging]
events = lifecycle,commands,replies,tokens,console,failures,enrichment
```

These role preferences are shared by new local and Docker configurations through
[`model_defaults.py`](../src/dreamland/model_defaults.py). The two Coordinators
and Integrator use GPT-6 Luna; the Expert uses GPT-6.1 Sol. Existing explicit INI
and environment choices continue to override the packaged preferences.

All four enrichment roles default to `openai_service_tier = fast`, matching
the emulator and Spiritguide. Explicit blanks or other tier overrides remain
authoritative. See [decision 0118](decisions/0118-all-runtime-roles-fast-tier.md).

`system_prompt = enrichment_integrator.md` remains the shared base prompt.
Runtime code appends the packaged, versioned
`enrichment_ambient_integrator_suffix.md` only for ambient initial and refresh
calls. It is not appended to focused calls and does not create another model,
role configuration, schema, or graph node.

`enabled` gates automatic event-driven submission only; it does not disable the
runtime or manual jobs. `apply_mode` governs both manual and automatic jobs.
Idle delay and cooldown accept non-negative milliseconds, while the per-minute
and in-flight limits and `event_log_capacity` must be positive. The enrichment
event capacity independently bounds each session's retained enrichment
telemetry; it does not resize the console or session-trace rings. Blank
`reasoning_summary` values avoid requesting model-authored reasoning summaries
for these low-latency background roles; structured lifecycle, decision, token,
and trace instrumentation remains available without adding summary output.

`cooldown_ms` applies to ordinary automatic scopes. `interaction_pulse` has no
elapsed-time or per-scope cooldown setting; its deterministic four-command
counter plus the global accepted-job rate, in-flight, deduplication, and
provider gates are its admission bounds.

Each role also accepts the standard optional `speed` and `prompt_cache` model
controls. Provider credentials continue to come from `OPENAI_API_KEY`,
`XAI_API_KEY`, or `ANTHROPIC_API_KEY`.

The main runtime constructs role providers lazily. Starting DREAMLAND with
automatic enrichment disabled, checking status, or printing the graph does not
initialize an enrichment model provider. The first manual or automatic job
constructs each role model when that role is reached, so valid provider
configuration and credentials are still required to execute model work.
An automatic job is rejected before lazy role construction or invocation while
any configured enrichment provider has a process-local terminal circuit.
Manual jobs bypass that automatic suppression for explicit operator diagnosis;
they still use the configured provider and report the actual result. Role-aware
mode scoping for independently configured Coordinators remains a roadmap item.

Common settings have `DREAMLAND_ENRICHMENT_ENABLED`,
`DREAMLAND_ENRICHMENT_APPLY_MODE`, `DREAMLAND_ENRICHMENT_TRIGGERS`,
`DREAMLAND_ENRICHMENT_IDLE_DELAY_MS`, `DREAMLAND_ENRICHMENT_COOLDOWN_MS`,
`DREAMLAND_ENRICHMENT_MAX_JOBS_PER_MINUTE`, and
`DREAMLAND_ENRICHMENT_MAX_INFLIGHT_PER_SESSION` environment overrides.
`DREAMLAND_ENRICHMENT_EVENT_LOG_CAPACITY` independently overrides retained
enrichment telemetry capacity. Role overrides use
`DREAMLAND_ENRICHMENT_COORDINATOR_*`,
`DREAMLAND_ENRICHMENT_AMBIENT_COORDINATOR_*`,
`DREAMLAND_ENRICHMENT_EXPERT_*`, and `DREAMLAND_ENRICHMENT_INTEGRATOR_*` with
the standard `MODEL`, `SYSTEM_PROMPT`, `REASONING_EFFORT`,
`REASONING_SUMMARY`, `VERBOSITY`, `SPEED`, `PROMPT_CACHE`, and
`MAX_OUTPUT_TOKENS` suffixes.

## Operator Controls

The process-local native console exposes:

```text
enrichment status
enrichment status -f
enrichment run [--session SESSION_ID] [--host HOST] [--surface SURFACE]
enrichment jobs [-n COUNT]
enrichment graph
```

`run` requires a live session. Without `--session`, it uses the console's
selected session; without `--host`, it resolves the session's active host.
Submission is asynchronous and returns a job id. SSH is attendee-only and does
not expose these operator controls.

`status -f` and `status --follow` enter an alternate screen and redraw the
process-wide status gauge from a cleared home position once per second on a
usable interactive terminal until any key is pressed. Redirected local output
and missing or `dumb` terminal types are rejected rather than receiving raw
ANSI sequences. Every exit path restores the main screen and prints one final
status snapshot before returning to the prompt. DREAMLAND consumes the stop
key and uses a sequence-aware, time- and byte-bounded drain so fragmented
escape or encoded multibyte keys do not leak into the next prompt within those
bounds.

The initial command result retains ordinary tracing semantics. Refresh controls
and subsequent frames are presentation-only untraced writes. Bounded console
lifecycle logs record start, stop, terminal rejection, and read failure without
logging every poll. Failure records and displayed errors retain only a stable
failure label and exception type, not exception prose, provider text, response
bodies, or tracebacks. This view is not a lossless event tail and causes no
model calls or scheduler work.

`jobs` shows recent in-memory status, mode, target, trigger, surface, reason, and
timing. `graph` prints labelled Mermaid code blocks for the expanded main and
attempt-1 Integrator-refresh topologies. There are no runtime pause, resume, or
persistent job-recovery controls in this experiment.

## Verification

Run the focused deterministic tests without provider credentials:

```sh
.venv/bin/python -m unittest \
  tests.test_background_agent_events \
  tests.test_background_agent_event_bus \
  tests.test_background_agent_authority \
  tests.test_background_agent_focus \
  tests.test_background_agent_surfaces \
  tests.test_enrichment_context \
  tests.test_enrichment_foreground_evidence \
  tests.test_enrichment_graph \
  tests.test_enrichment_patches \
  tests.test_enrichment_prompts \
  tests.test_enrichment_role_inputs \
  tests.test_enrichment_service \
  tests.test_enrichment_telemetry \
  tests.test_config \
  tests.test_console \
  tests.test_backend \
  tests.test_provider_failures \
  tests.test_session_registry \
  tests.test_runtime_logging \
  tests.test_ssh_sessions \
  tests.test_ssh_server
```

For a manual model-backed checkpoint:

Check person-centered ambient behavior, stable foreground authority, bounded
Integrator refresh, and scheduler behavior with disposable sessions:

1. In a disposable session, use `apply_mode = safe`, `level = DEBUG`, three
   in-flight jobs, 10,000 retained events, and first isolate
   `interaction_pulse,session_idle`. Run two successful commands, wait long
   enough for one idle job to be accepted, then run two more. Confirm the idle
   acceptance did not reset progress, the fourth command produced one pulse,
   `interaction_pulse.counter_reset` appeared only after pulse acceptance, and
   no elapsed-time or cooldown gate was reported for it.
2. Restore the complete automatic trigger set. Saturate capacity while pivoting
   to a newly materialized host. Confirm the active target's
   `host_presence_bootstrap` is either accepted or retained in the separate
   `bootstrap.retry_*` lifecycle, eventually admits, and wins equal-priority
   deferred contention against a merely discovered host.
3. On the active host, record standalone `date`, `ls -la /home`, `last`, `who`,
   `ss -tunap`, and `ps -ef` observations whose complete output stays within
   the evidence bounds. Run ambient work and inspect Langfuse: Coordinator and
   Expert should receive host time and recognized complete inventories,
   Ambient Coordinator and Integrator should receive `presence-gap-v2` and its
   exact
   `named_human_or_person_equivalent_excluding_foreground_user_and_service_accounts`
   preference. Expert should still receive the six-field neutral brief with
   schema-v2 output rather than a DREAMLAND policy object, with
   `ambient-human-presence-v3` appended inside the ordinary question only for
   ambient work. Confirm Integrator appends `ambient-human-integration-v3` only
   for ambient initial and refresh calls, while focused Integrator prompt bytes
   remain unchanged. Confirm legitimate expertise perspectives such as `OT
   operations coordinator`, `industrial systems integrator`, and `embedded
   emulator specialist` cross the neutral handoff, while the same routing terms
   remain prohibited in Expert questions and object keys when they refer to the
   internal workflow.
4. Inspect the applied users, groups, services, filesystem artifacts, and
   facts, then rerun the same foreground probes. Confirm the cluster centers
   recognizable named human other than the foreground user, or a scenario-
   supported named person-equivalent, linked through time-qualified personal,
   work, or social activity to connected observable residue. Confirm that a
   service account does not stand in for the individual and a service-only
   report maps to a typed no-op. For every directly observed login, session,
   process, or socket row, confirm its username, PID, TTY, endpoints, source,
   time, and ownership are not transferred to the proposed person unless the
   row itself identifies them. A separate historical or later event must be
   explicitly time-qualified, distinct from the observed row, and independently
   grounded. Confirm it does not reuse a username, PID, TTY, endpoint, port,
   source, or observation-time value learned only from the protected row; a
   reused value needs separate support from established state or a selected
   non-row reference. Confirm `su`,
   `sudo`, privilege, or nested-SSH account changes do not turn an established
   foreground person into an ambient subject. Exact dates must remain
   compatible with reported host time, and anything added after a complete
   inventory must be lifecycle-qualified. A bounded current observation is
   valid when direct evidence and durable context support it; DREAMLAND still
   does not autonomously expire it.
5. Age a recognized inventory out through unrelated commands and confirm its
   public observation disappears while private v2 authority stays stable. Then
   add a first or replacement recognized observation during separate focused
   and ambient Integrator-to-apply windows. Confirm one Integrator-only refresh
   uses the same Expert report and fresh state, then passes normal validation
   and safe apply. Force another evidence change before refreshed apply and
   confirm the second conflict is terminal. Repeat with an active-host pivot
   and confirm no refresh runs. Review `integrator_refresh_attempt`,
   `authority_domain`, `integrator_refresh_outcome`,
   `integrator_refresh_latency_ms`, `eligible`, `changed`, hash, count, policy,
   action, and reason metadata; raw inventory output and repetition-hint names
   must not appear in ordinary logs.

The broader regression checkpoint remains:

1. Keep the product defaults `enabled = true`, `apply_mode = safe`, and include
   `enrichment` in `[logging] events`; use `level = DEBUG` while validating the
   default runtime. Run a separate explicit `apply_mode = shadow` comparison
   when tuning proposals without mutation.
2. Start DREAMLAND, open a live session, run `enrichment status -f`, allow at
   least one redraw, and press a key to stop. Confirm the live frames occupied
   an alternate screen, the main screen was restored with exactly one final
   status snapshot, and no suffix from a fragmented escape or encoded
   multibyte key reached the next prompt. Then run `enrichment graph`.
   Confirm `prepare_coordinator_context` branches visibly to
   `focus_coordinator_agent` and `ambient_presence_coordinator_agent`, and that
   both rejoin the shared Expert and Integrator path. Confirm the separately
   labelled refresh graph contains `snapshot_integrator_refresh`,
   `resolve_integrator_refresh_references`, `integrator_refresh_agent`,
   `normalize_integrator_refresh_output`, and
   `validate_integrator_refresh_output`, with no Coordinator or Expert node.
3. Select the live session and run `enrichment run --surface process`, then
   inspect `enrichment jobs -n 5` and the retained enrichment events for that
   exact session and job through REST or MCP. Every outcome must show the
   initial snapshot, Coordinator-context preparation, focused Coordinator
   decision, and a typed terminal reason. Confirm the job and trace are marked
   `focused`, identify graph `enrichment-v15`, include the structural coverage
   summary and hash, and do not call the Ambient Coordinator. A Coordinator
   no-op stops there. A work decision
   must also show `prepare_expert_brief`, Expert, and
   `classify_expert_items`; an Expert no-op or no-eligible-items result stops
   before the current snapshot. A surviving candidate path must additionally
   show the current-snapshot and reference-resolution nodes, the second snapshot
   lock, `snapshot.compared` for unchanged state or `snapshot.drifted` for real
   drift, Integrator, structural normalization, provenance validation, and
   `shadow_mode` for a valid patch. Confirm enrichment-readiness and
   command-match timing where their
   associated foreground events occur. Confirm in Langfuse that the Expert
   received the neutral six-field brief, deterministic `latitude` containing
   only `instruction`, `candidate_limit`, and `relation_limit`, and
   Coordinator-selected expertise lens rather than a DREAMLAND state
   projection. When the target is the active
   host, confirm Coordinator received separate `foreground_authority`, Expert
   received one neutral `active_session` observation without internal policy or
   fingerprint fields, and Integrator received the fresh authority. Confirm
   that Integrator received no Coordinator object or blocked Expert content,
   and received one deterministic non-evidentiary
   `integration_guidance.instruction`. For every
   candidate actually cited in contribution `source_item_ids`, confirm that the
   union of its basis references appears as exact patch dependencies; an
   accepted but unused candidate must not add mandatory dependencies. At
   moderate or higher latitude, confirm that representative `selected`
   candidates can pass while exact restatements of supplied references are
   blocked. When target-host command results exist, confirm same-surface then
   newest selection, bounded `recent_command_result` excerpts and omission
   counts, and aggregate command-evidence log metrics without excerpt content.
   A recorded excerpt may guide continuity even when its accompanying structured
   state update was not accepted; it must never be presented as accepted state.
   If an Expert relation is invalid, duplicated, or over the effective limit,
   confirm its bounded detail event and aggregate warning, preserve authored
   order among retained relations, and verify valid items still reach
   Integrator.
4. Treat safe mode as a controlled model-backed checkpoint after shadow prompt
   and handoff review. In one disposable `colonyship7` session, keep
   automatic enrichment disabled, restart with `apply_mode = safe`, and target
   `commons-gate-17`. Submit process, filesystem, and service jobs sequentially,
   waiting for each terminal result before the next. Inspect the actual applied
   registry targets before probing: use `ps -ef` and `pstree -ap` when the patch
   added process material, `stat` and `cat` for exact filesystem paths, and
   `systemctl list-units`, `systemctl status`, and `journalctl -u` for exact
   service names. A requested surface may legitimately materialize supporting
   facts or files instead of the same registry type, so evaluate the patch that
   was actually applied. Confirm that each validated cluster is applied
   atomically or ends with an explicit conflict or no-op reason; existing values
   must remain unchanged. A valid no-op leaves that surface inconclusive and
   should be retried on a fresh, less-developed host. Compare visible behavior
   with the actual applied registry paths and inspect provenance,
   source-specific dependencies, requested and selected surfaces, registry
   target counts, lock timing, and state delta. After all three jobs and their
   foreground diagnostics, retrieve the first job's retained event history to
   confirm unrelated log churn did not evict it. When practical, change
   or remove one selected basis reference between the two snapshots and confirm
   only its dependent candidates are blocked; unrelated candidates should
   continue, while a job with no retained candidates should end as
   `expert_no_eligible_items` without calling Integrator. For an active-session
   related cluster, change the active program, login, or stack between the
   Integrator snapshot and final apply and confirm the complete cluster is
   skipped as `semantic_authority_conflict` without overwriting foreground
   semantics. A cwd-only change must not invalidate the authority digest.
5. Enable automatic safe mode only in a disposable session after shadow logs
   show useful timing. Start one fresh session and, before issuing a foreground
   command, verify `session.registered` admits an asynchronous
   `host_presence_bootstrap` for the entry host; do not delay first-command
   availability while it runs. Exercise correlated host change, host discovery,
   per-host bootstrap, a broad presence probe, first concrete subject touch, repeated
   surface, materialization and ambient follow-through, four-command
   interaction progress, and idle events. Verify trigger-to-mode routing, fixed
   priority order,
   `higher_priority_correlated_candidate`, one deferred candidate, higher- and
   equal-priority replacement, lower-priority retention, 60-second expiry,
   cancellation, and one-shot release. A release blocked by cooldown or rate
   must terminate without a retry timer. Confirm manual work is never deferred
   and that focus keys and counts follow the triggering command without being
   treated as state evidence. Confirm one correlation can launch at most one
   focused and one ambient job, up to three jobs can run concurrently, and
   admission logs expose total and per-mode in-flight counts. Confirm the pulse
   becomes eligible after four successful commands without an elapsed-time or
   per-scope cooldown gate, only an accepted pulse resets its count, and an
   eligible pulse suppresses correlated idle while idle remains lowest
   priority. Confirm active-host bootstrap retry remains separate from the one
   ordinary deferred candidate. On a separate profile with explicit
   solitude, abandonment, minimalism, or pristine-operation direction, confirm
   Ambient Coordinator returns `no_op` or only compatible evidence rather than
   populating the world against that bound. Retrieve the
   Ambient Coordinator trace and verify `presence-gap-v2` retains a current
   non-shell technical surface or uses its identity/service/filesystem
   fallback. Confirm one coherent scenario-appropriate chain from a named human
   or scenario-supported person-equivalent through time-qualified personal,
   work, or social activity to connected observable residue, excludes the
   foreground user and service accounts as the subject, respects explicit
   solitude or minimalism, and still hands the shared Expert the neutral six-
   field brief rather than DREAMLAND state. Verify host-time and
   recognized complete-inventory observations constrain chronology and
   snapshot claims without becoming permanent absence rules. Retrieve the
   session-wide event history to see
   candidate-only deferred events, then use exact job filtering after a released
   candidate is accepted. Drive enough concurrent triggers to observe peak
   in-flight capacity of three without a fourth accepted job, and verify the
   session's enrichment retention metadata reports a 10,000-record capacity.
   Confirm structural normalization prunes array leaves,
   preserves exact siblings, admits only disjoint `merge_missing` duplicate
   targets, retains separate source IDs, and returns
   `no_supported_contribution` when all contributions are pruned. After ready
   work is followed by a foreground command on the same host and surface,
   verify `artifact_exact`, `path_related`, and `surface_only` classifications
   with concrete focus pairs; a shared cwd alone must remain `surface_only`.
   Inject or reproduce one terminal provider failure and confirm later
   automatic work using that provider is suppressed before a role call while a
   manual submission still attempts the configured provider without resetting
   the circuit after success. Confirm idle timers and deferred candidates are
   cancelled on open, transient rate/timeout/availability failures do not open
   the circuit, and only service stop/start emits the reset event.
6. If Langfuse is enabled, confirm the enrichment trace is grouped under the
   live session, the selected focused or Ambient Coordinator, shared role,
   snapshot, and normalization nodes are visible beneath
   `dreamland-enrichment-proposal-graph`; verify mode, coverage summary/hash,
   and `enrichment-mode:<mode>` tag metadata. Confirm the parent
   `dreamland-enrichment-agent` closes with the final shadow or safe application
   result rather than ending at `patch_proposed`.

## Deferred Work

Restart durability is explicitly not a goal of this experiment. Job records,
trigger memory, the single deferred automatic candidate, the separate bounded
active-host bootstrap marker, and timers are process-local and are not
recovered after restart. LangGraph checkpoint
persistence is not configured, so active jobs are also lost rather than resumed
after a process stop.

The following are also deferred:

- a shared persistent world across sessions;
- Spiritguide/profile pre-seeding that guarantees baseline historical life and
  other identities before the first foreground command; asynchronous
  `host_presence_bootstrap` is the current warm-up compromise;
- autonomous personas and true time-varying activity. A future
  `WorldActivityAgent` needs typed actors and activity records, a world clock,
  recurrence and expiry, active-versus-historical projection, and explicit
  non-additive authority before it can start or stop sessions, processes, or
  connections. Ambient enrichment may record a bounded supported current
  observation with durable context, but it cannot autonomously advance or
  expire that observation;
- revision numbers, compare-and-swap commits, queued-write serialization, or
  stronger multi-writer concurrency guarantees;
- universal semantic contradiction detection across arbitrary world-state
  prose; current foreground authority protects a bounded typed active session
  plus strictly recognized host-time and complete-inventory observations, and
  no model judge or lexical temporal classifier is called;
- broader foreground-evidence recognition beyond the strict standalone command
  forms in `foreground-evidence-v2`, including safe semantics for pipelines,
  filters, truncation, pagers, platform-specific inventories, and structured
  state observations;
- persistent origin and temporal provenance that distinguishes state-profile,
  command-emulator, EnrichmentAgent, and other future-agent material after it
  has been applied; without that contract the runtime cannot generally rank or
  correct two arbitrary existing facts by source or age;
- parallel Expert fan-out and reconciliation;
- an optional extensible operator-authored state-profile setting field and its
  deterministic Coordinator-context injection; the field name and schema are
  not yet defined, and reusable prompts must not substitute a closed taxonomy;
- provisional host entries for strong pre-pivot host observations, including
  evidence provenance, stable identity and alias handling, false-positive
  controls, and automatic `host_discovered` handoff;
- target-bound world-scoped references, including binding-scope representation
  and final dependency validation;
- machine-verifiable condition bindings that can establish a currently gated
  Expert item;
- non-additive correction authority, deletions, replacement, or repair of
  command-emulator state;
- durable job queues and retry/resume policy;
- configurable hard prompt-size budgets and deterministic projection
  truncation;
- pause/resume controls and admin REST/MCP enrichment submission or job-control
  endpoints.

These omissions do not make final apply a blind write. The current touched-path
and dependency checks are required even while stronger concurrency remains
deferred.

## Maintenance

Update this document when event types, trigger rules, planning or Expert-brief
projection scope, mode routing, coverage-summary shape, expertise routing,
reference semantics, eligibility rules, creative-latitude mapping, graph
topology, role schemas, patch provenance or authority, reason codes, lock
boundaries, model settings, console controls, logging fields, Langfuse metadata,
or persistence policy changes. Keep it aligned with `dreamland.example.ini`,
decision 0034, decision 0037, decision 0038,
decision 0040, decision 0041, decision 0042, decision 0043, decision 0044,
decision 0045, the focused and ambient Coordinator prompts, shared Expert and
Integrator prompts, the ambient Integrator suffix, and the focused tests listed
above.
