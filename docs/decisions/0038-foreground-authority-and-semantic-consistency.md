# 0038 Foreground Authority And Semantic Consistency

Status: Active
Audience: maintainers, DREAMLAND operators, researchers, prompt authors, and
coding agents
Purpose: record the bounded foreground-session authority, deterministic focus
matching, and automatic provider-failure suppression added after the first
sustained automatic safe-mode checkpoint.

## Overview

The active shell frame is newer authority about a live login than an
asynchronously materialized description. World-state dependencies alone cannot
detect changes to session semantics between integration and final apply.

This checkpoint adds a narrow typed authority channel and temporal guard. It
does not introduce a model judge, universal semantic theorem prover, profile
provenance system, correction authority, or a realism-first restriction on
creative world building. DREAMLAND still evaluates enrichment first by whether
it creates an engaging, deep, believable environment.

## Decision

### Bounded Active-Session Authority

`src/dreamland/background_agents/authority.py` owns a reusable typed capture of
the live foreground frame. It returns evidence only when the requested target
is the session's active host. The public `foreground_observation` contains the
target and frame hostname, active and login usernames, current working
directory, interactive-program name/command/bounded arguments/prompt template,
a small allowlist of shell variables, and shell-stack depth. Program arguments
use fixed count and length limits; recognized credential-bearing flags,
including conventional environment-variable assignments, and credentialed
URLs are deterministically redacted before model handoff.

The capture excludes arbitrary frame metadata, arbitrary environment values,
command history, command output, and credential input. This is a narrow
background-agent projection boundary; it does not redact or change DREAMLAND's
intentional foreground credential recording for honeypot and training uses. It
is copied while the same short session lock used for the world-state snapshot
is held. It is direct evidence only for the currently active session. It does
not establish a universal SSH policy, constrain another user or host, or make
unrelated filesystem, process, service, or operational texture less eligible.

Within that scope, current foreground evidence outranks incompatible
materialized descriptions. Coordinator receives it as a separate planning
field. The deterministic Expert-brief builder translates it into a neutral
`active_session` observation without expanding the Expert's six-field input or
exposing DREAMLAND workflow state. Integrator receives the fresh current
foreground observation beside its current state snapshot. These handoffs spend
no Coordinator output tokens and add no model-authored logging requirement.

### Hidden Apply-Time Dependency

The runtime computes a SHA-256 fingerprint for the active-session semantic
fields. The fingerprint covers target and frame host identity, active and login
usernames, the complete credential-sanitized interactive-program command and
argument list, selected shell variables, and stack depth. It does not inherit
the public projection's argument-count or text-length limits, so a transition
beyond the visible argument window still invalidates stale work. It
deliberately excludes the working directory so an ordinary `cd` does not
invalidate otherwise relevant work.

The private fingerprint is held in a runtime sidecar during LangGraph
execution. It is attached to the in-memory patch only after the traced graph
has completed, and is absent from role inputs, role outputs, traced graph state,
serialized patch payloads, and ordinary logs.

After Integrator returns, deterministic code binds an in-memory
`active_session` authority dependency when the selected surface is shell,
identity, or service, or when a proposed contribution touches login, prompt,
authentication, access, or other current-session semantics. Service is guarded
as a category because SSH daemon match rules and forced commands directly
control the observed session but do not have one stable registry shape. The
dependency is runtime-owned: it is not part of the Integrator output schema,
does not appear in serialized patch payloads or traced graph state, and cannot
be authored or removed by a model.

In safe mode, final apply captures the live foreground authority again under
the apply lock. A missing or different fingerprint skips the affected atomic
cluster as `semantic_authority_conflict`; current foreground state wins. This
protects the accepted snapshot-to-apply window without holding a lock during a
model call. Shadow mode still exercises and traces dependency selection but
does not mutate state.

The guard is intentionally conservative and typed. Surface and registry checks
cover known active-session domains, while a small deterministic token set can
recognize direct current-session wording in proposed values. This is temporal
conflict protection, not proof that arbitrary prose elsewhere in world state is
semantically consistent.

### Deterministic Foreground Match Classes

Read-ahead usefulness is no longer represented only by a shared coarse surface.
Existing eligibility still requires the same session, exact target host, and a
compatible effective surface, and still selects the newest eligible completed
job. After that coarse selection, deterministic focus comparison classifies the
causal job focus against the accepted-command focus as:

- `artifact_exact` when typed artifact identifiers, concrete paths, or path
  patterns identify the same subject;
- `path_related` when concrete paths have a meaningful ancestor/descendant
  relationship or a supplied path pattern matches the other focus; or
- `surface_only` when only the coarse surface aligns.

Working-directory context alone is not a subject match because adjacent
commands commonly share a directory while inspecting different artifacts. The
classification is logging and research telemetry, not write authority or proof
that enrichment output was rendered to the user. It requires no model call or
model-authored explanation. `enrichment.command_matched` and
`enrichment.late` retain the policy, class, expected and observed focus hashes,
and separate path/artifact counts without retaining raw paths.

### Terminal Provider Circuit For Automatic Work

Provider exceptions use one bounded provider-neutral taxonomy. Structured
provider codes are considered before HTTP status because exhausted credit may
be reported as HTTP 429 and must not be mistaken for ordinary transient rate
limiting. Credit or quota exhaustion, authentication, authorization, and
configuration failures are terminal; rate limits, timeouts, and provider
unavailability are transient; unknown errors remain unknown.

An observed terminal failure opens a process-local circuit for the affected
provider through `provider_circuit.opened`. A foreground command failure opens
it only from an authoritative command-emulator completion event and when that
provider is also used by the enrichment graph. Later
automatic EnrichmentAgent jobs that require the provider are suppressed at
admission or job preflight as
`trigger.suppressed reason=provider_circuit_open` before another model call.
Opening a circuit also cancels all current idle timers and bounded deferred
candidates through `idle.cancelled` and `deferred.cancelled` with that reason so
already-retained speculative work cannot immediately spend against the failed
provider.

Manual enrichment remains available so an operator can test a corrected
credential, model, or account condition. Manual success does not reset the
circuit or silently claim that the external problem is fixed. Transient and
unknown failures do not open it. There is no timer-based reset. Stopping and
starting the service clears process-local circuits and emits
`provider_circuit.reset reason=runtime_restarted` as a process-level log without
session context.

Each runtime start also creates a new in-process generation. A non-waiting stop
invalidates prior workers immediately and clears their scheduler accounting;
their later callbacks cannot apply a patch, open a circuit, release a current
deferred candidate, or block current admission. Event subscriptions capture
the generation and stale deliveries cannot affect the replacement runtime. A
waiting stop drains the current generation before returning. These guards are
in-process lifecycle isolation, not restart persistence.

The circuit is an operational cost and latency control, not restart-durable
state and not a new runtime-status API. Its origin, provider, bounded failure
classification, and suppression decision are available through retained
structured enrichment events. Provider response bodies and free-form error
messages are not retained in those fields. The same deterministic failure
classification can be carried by foreground command-completion events so
automatic background work does not immediately repeat a known terminal
provider condition. The process-level reset is available in the global runtime
log rather than a session's retained enrichment deque.

### Observability Without Model Logging Burden

The existing process-local per-session enrichment-event buffer retains the new
focus-match, provider-failure, circuit, and authority fields. Authority events
report policy version, domain, binding reason, dependency count, presence or
change, and conflict action. They do not log the foreground payload or its
fingerprint. Provider events retain only bounded provider/model identifiers,
classification, disposition, status/code when safe, circuit origin, and circuit
count. Focus events retain classifications, bounded counts, and hashes rather
than foreground command content.

No role receives a request to narrate its reasoning for this telemetry. The
checkpoint adds no judge call and no verbose model logging schema. Ordinary
per-role token callbacks remain the only model-token accounting; there is no
new token-generation burden for authority, focus, or circuit decisions.

## Limits And Deferred Semantics

The current authority is deliberately not universal semantic proof. It can
protect a typed active-session observation and known related contributions; it
cannot reliably decide whether arbitrary natural-language facts anywhere in a
large world contradict one another. A future semantic judge would add cost,
latency, and another fallible model decision and is not part of this
checkpoint.

Applied world records also do not yet distinguish state-profile provenance from
EnrichmentAgent provenance. Therefore DREAMLAND cannot generally order every
profile fact above or below every generated fact, nor can it safely correct an
older speculative record solely because a newer observation exists. Explicit
origin and temporal provenance are future state-model work.

This checkpoint preserves additive safe authority. It does not grant
Enrichment deletion, replacement, correction, or cross-session writes. A
semantically stale existing value may remain visible until future provenance
and correction contracts exist; the current guard prevents a dependent new
cluster from racing past fresher active-session evidence.

## Alternatives Considered

Passing the entire mutable `SessionState` to every role was rejected because it
would expose credential-bearing input/history, arbitrary environment and
program metadata, and DREAMLAND workflow details while making prompt boundaries
harder to reason about. The projection does not alter intentional foreground
capture of those inputs.

Having Coordinator restate foreground authority or focus expansion in its
assignment was rejected because deterministic control information should be
injected repeatably without spending output tokens.

Hashing the working directory was rejected because routine navigation would
turn an active-session semantic guard into a broad stale-work detector and
discard unrelated useful enrichment.

Using a model judge on every proposal was deferred. It may detect semantic
relationships beyond typed fields, but it would add output-token cost, latency,
nondeterminism, and an additional prompt that must preserve DREAMLAND's
world-building priority.

Suppressing manual work behind a provider circuit was rejected. Manual runs are
an explicit operator action and are useful for verifying that an external
terminal condition has been corrected.

## Consequences

Coordinator, Expert, and Integrator can now see the active login semantics at
their appropriate abstraction level, and safe apply rejects related work if
those semantics change during model latency. Unrelated enrichment remains free
to add dense, imaginative, believable depth.

Usefulness telemetry can distinguish an exact artifact encounter, a related
path exploration, and a broad surface-only match without claiming all three
are equivalent user-visible use. Automatic model spend stops repeating known
terminal provider failures, while transient provider recovery and explicit
manual diagnosis remain possible.

The added safety is bounded by typed evidence. Maintainers and presenters must
not describe it as complete semantic validation or as a realism clamp. The
product failure condition remains a flat world: small plausible inconsistencies
should be measured and improved without sacrificing depth, signs of life, or
creative breadth.

## Maintenance

Update this decision when the foreground-authority schema or digest scope,
authority-binding policy, focus-match classes, provider-failure taxonomy,
circuit lifetime or scope, manual/automatic policy, event fields, provenance
model, correction authority, or semantic-judge policy changes. Keep
`docs/ENRICHMENT_AGENT.md`, `docs/OPERATIONS.md`, `docs/ARCHITECTURE.md`,
`docs/ADMIN_API_V0.md`, `ROADMAP.md`, prompts,
runtime contracts, and focused tests aligned. Add the related implementation
commit after this checkpoint is committed.
