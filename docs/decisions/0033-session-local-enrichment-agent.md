# 0033 Session-Local EnrichmentAgent

Status: Active
Audience: maintainers, DREAMLAND operators, prompt authors, and coding agents
Purpose: record the role, authority, workflow, and lifetime boundaries of the
first asynchronous DREAMLAND background agent.

## Overview

Decision 0034, `portable-candidate-driven-enrichment`, supersedes this record's
versioned prompt, role-envelope, graph, and Integrator-output details. This
record remains authoritative for the EnrichmentAgent's separate identity,
session-local lifetime, trigger substrate, short-lock sequence, and additive
write authority. Decision 0037, `automatic-enrichment-checkpoint`, supersedes
the original same-command precedence and drop-on-in-flight scheduling details;
it retains the neutral event substrate and one running job while adding one
bounded deferred automatic candidate per session.
Decision 0049, `default-safe-enrichment`, changes the packaged enablement and
application defaults to automatic plus `safe`; the identity, lifetime, and
authority boundaries in this record remain unchanged.

DREAMLAND needs operational texture to appear just before a user examines it,
without forcing the command emulator to fully materialize every possible host
detail in advance. The first background feature must also remain explainable
enough to tune latency and deterministic emulator behavior during an early
experiment and to present its logical flow clearly.

Spiritguide already owns deliberate operator-guided state changes. Extending it
with an implicit periodic loop would combine two different authorities and make
operator intent harder to distinguish from speculative runtime work.

## Decision

DREAMLAND implements a separately named `EnrichmentAgent`. User-facing branding
may change later, but the runtime identity and logging boundary are distinct
from Spiritguide.

Enrichment is session-local and temporal. It reads and, in safe mode, modifies
only one explicitly targeted live session. It does not mutate a shared world or
the state profile from which the session was created. An explicit state-profile
save remains the only current way to carry enriched state into a later session.

The proposal workflow uses one directly inspectable LangGraph with three
distinct roles:

- Coordinator is DREAMLAND-aware. It receives an initial host-focused planning
  projection plus a deterministic catalog of available references, selects one
  near-term semantic surface, and can stop with no work.
- Expert acts as an independent domain specialist. It receives only a neutral,
  allowlisted real-world brief and returns a compact report that is not coupled
  to DREAMLAND state, policy, workflow, or patch schemas.
- Integrator understands DREAMLAND's current state. It receives a fresh
  snapshot and current bindings for the references selected by Coordinator,
  then translates supported Expert material into one bounded additive patch
  cluster.

The graph contains a visible initial-snapshot node, a deterministic
`prepare_coordinator_context` node before Coordinator, a visible
`prepare_expert_brief` node between Coordinator and Expert, a visible
current-snapshot node after Expert, and a deterministic
`resolve_current_references` node before Integrator. Coordinator uses the
initial planning projection. Deterministic code organizes its input, validates
its reference selection, and creates the Expert's neutral brief; Expert never
receives the projection. Integrator uses a second snapshot taken after the
Expert returns and current bindings resolved from that snapshot. Model calls
remain sequential in the initial experiment. Each role has independent provider,
model, prompt, reasoning, verbosity, and output-limit configuration.
The role-boundary revision is identified in traces as `enrichment-v2`; the
compact version-1 Coordinator, Expert, and patch response objects remain
unchanged.

Available references use opaque job-local IDs such as `r1` and `r2`. Each
Coordinator catalog entry has shape `{id, kind, label, observation}` and
describes only an entity or observation already present in the initial
projection. Coordinator's `anchors` output selects exact IDs from that catalog;
an anchor is neither free-form subject matter nor a proposed fact. IDs are
assigned deterministically within the job but are not durable state
identifiers.

The Expert receives exactly `{question, host, observations, references}`. Its
selected `references` retain the opaque IDs and neutral observations but expose
no DREAMLAND paths or bindings. Expert relation endpoints such as `@r1` can
therefore remain traceable without revealing where an entity lives in state.
Integrator receives current `reference_bindings` with shape
`{id, kind, path, expected}`; paths are host-relative and expected values come
from the fresh snapshot. An unresolved reference cannot be reconstructed by
label matching or model guesswork.

Models do not directly commit state. Integrator can only propose strict JSON.
DREAMLAND injects the trusted basis captured for Integrator, validates target,
cluster count, contribution count, and allowlisted registries, and applies the
proposal through a deterministic safe-patch boundary. `shadow` mode exercises
the complete proposal path without mutation. `safe` mode permits only
`put_if_absent` and `merge_missing` additions to selected host-local keyed
registries; it permits no deletion, replacement, correction, or array writes.

Snapshot copy and final apply use separate short session locks. No lock spans a
model call. State may therefore change between Integrator's snapshot and final
apply. This race is accepted for the experiment, but apply must compare declared
dependencies and every touched path against the trusted basis and live state
under the final lock. Current state wins; a conflict skips the complete causal
cluster; unrelated drift is preserved. Proper ancestors of touched fields are
part of that comparison so foreground deletion of a registry or nested object
is not undone by enrichment.

The backend and live-session registry publish immutable, neutral session events
through a small shared event bus. Trigger candidates and gates belong to each
subscriber rather than to the bus. This substrate can support future background
agents without forcing them to share Enrichment's triggers, projection, graph,
or write authority.

Specific read-ahead signals take precedence over generic focus for the same
command. Decision 0037 generalizes this into a complete Enrichment-specific
priority order, same-correlation suppression, and one process-local deferred
automatic candidate. Those scheduling rules remain subscriber policy rather
than behavior built into the neutral event bus.

Runtime logging and Langfuse tracing are part of the feature contract. Trigger
decisions, limit pressure, jobs, locks, snapshots, roles, validation, and apply
outcomes carry correlation fields and stable reason codes. Enrichment-ready,
late, and command-matched timing connects background latency to the user's next
matching command without claiming that the foreground model incorporated a
particular enriched fact. The expanded graph is available to operators as
Mermaid.

The runtime owns an outer `dreamland-enrichment-agent` trace run and keeps it
open through deterministic application. The inspectable LangGraph proposal is
its `dreamland-enrichment-proposal-graph` child. The parent closes with both the
graph decision and final shadow or safe application result. This makes the
write outcome visible without moving mutation authority into the model graph or
creating a detached trace that must be correlated later.

Restart durability is not a goal of this feature. LangGraph checkpoint
persistence is not configured; trigger memory, timers, recent job records, and
active work are lost when the process stops.

## Alternatives Considered

Extending Spiritguide with a background loop was rejected because an explicit
operator agent and a speculative just-in-time agent have different purposes,
triggers, authority, and observability requirements.

A single model that both invents details and writes state was rejected because
it couples domain generation to DREAMLAND schema knowledge and gives model
output too much authority. Separating Expert from Integrator allows the Expert
format to favor low latency and low token count while keeping application rules
deterministic.

Passing the initial DREAMLAND planning projection directly to Expert was also
rejected. Generation policy, expansion directives, registry layout, trigger
identifiers, and Coordinator workflow fields distract a real-world domain role
and quietly turn it into another state-aware agent. The explicit brief
preparation boundary keeps Coordinator and Integrator responsible for
translation while preserving reference-level traceability across the Expert
call.

A monolithic Coordinator/Integrator role was rejected for the initial contract.
Separate roles make the decision to spend a model call, the independent source
material, and the state-aware translation independently visible and tunable.

Parallel Expert fan-out was deferred. It may reduce read-ahead latency for
several surfaces, but it introduces reconciliation and consistency work before
single-cluster behavior has been measured.

Holding a session lock across the graph was rejected because provider latency
would directly block user commands. Durable queues, persistent graph savers,
and a shared cross-session world were also deferred because they do not improve
the first experiment's just-in-time behavior.

## Consequences

Operators can run the feature manually while automatic triggers remain disabled
and can use shadow mode to inspect model fit, trigger timing, and proposals
before granting additive write authority. Three sequential model calls can
still miss the user's next command, so role model choice, reasoning effort,
output limits, cooldown, rate, and slight-read-ahead triggers remain tunable.

The neutral Expert brief is intentionally narrower than the Coordinator's
planning view. Expert may return `insufficient` when the deterministic allowlist
does not carry enough domain evidence; resolving that requires deliberate brief
projection changes, not exposing the complete DREAMLAND state. Job-local
reference IDs preserve traceability in logs and model reports without becoming
another persistent identifier namespace.

The final apply is conservative: a useful proposal may be skipped when the user
or command emulator touches the same field first. This is preferable to
overwriting newer user-visible state. Minor inconsistencies outside touched
paths are accepted during early testing and are observable through drift and
outcome logs.

The reusable event substrate creates a clean extension point for correction,
adversarial, assistant, and intent agents. Each future role still requires an
explicit capability decision and inspectable LangGraph; sharing events is not a
reason to hide several agents behind one dynamic workflow engine.

## Maintenance

Revisit this decision before adding shared-world state, restart recovery,
parallel experts, non-additive writes, stronger commit serialization, or a new
background-agent authority. Update it if Spiritguide and Enrichment role
boundaries, planning projection, Expert brief, reference catalog or bindings,
the three-role graph, session lifetime, or the safe apply contract changes.
