# 0044 Concise Ambient Independent-Life Routing

Status: Active
Audience: maintainers, DREAMLAND operators, researchers, prompt authors, and
coding agents
Purpose: replace categorical ambient presence ranking with a small,
scenario-neutral objective that directs Enrichment toward observable
independent life without changing focused enrichment.

## Overview

Decision 0043 introduced `presence-gap-v1`. Registry category balance alone
does not express whether a world feels inhabited.

The design must preserve DREAMLAND's primary goal: a deep, engaging, believable
world. Perfectly balanced registry counts are neither that goal nor reliable
evidence of it. At the same time, adding a `history-bearing_actor` counter would
mix an experiential quality requirement with storage categories and begin an
unbounded actor taxonomy.

This decision therefore narrows the deterministic ambient handoff. It leaves
focused Enrichment, trigger routing, the Expert and Integrator schemas, safe
patch authority, and the five-field structural coverage summary unchanged.
Only the ambient `presence_gap` contract and corresponding planning guidance
change. The visible graph identifier advances to `enrichment-v12` so traces can
distinguish the contract.

## Decision

### One Ambient Objective

Ambient planning uses one scenario-neutral objective named
`independent_life`. The Ambient Coordinator should plan one coherent causal
cluster with this shape:

```text
independent source
    -> temporally qualified activity
        -> concrete observable residue
```

An independent source may be a person, automation, device, institution,
environmental system, synthetic actor, historical occupant, remote operator,
or something stranger when the represented world supports it. This list is
illustrative, not a closed taxonomy. The cluster should create signs that the
world has causes and activity beyond the foreground user's immediate command.
Explicit state-profile or Spiritguide direction for solitude, abandonment,
minimalism, pristine operation, or another incompatible condition remains
authoritative.

`history-bearing_actor` is deliberately not a state key, policy field, counter,
dimension, registry, or actor classification. Historical or recurring activity
is part of the causal-cluster quality contract. A bare username or service
account does not satisfy the objective by itself; a compatible source linked to
qualified activity and inspectable residue can.

### Concise `presence_gap` Contract

`coverage_summary` remains unchanged and keeps exactly these structural counts:

```json
{
  "facts": 0,
  "filesystem_artifacts": 0,
  "groups": 0,
  "services": 0,
  "users": 0
}
```

For `ambient_presence` only, `presence_gap` becomes `presence-gap-v2` and has
exactly this shape:

```json
{
  "policy_version": "presence-gap-v2",
  "ambient_objective": "independent_life",
  "preferred_surface": "network",
  "subject_preference": "non_login",
  "anti_repetition": {
    "subjects": [],
    "systems": []
  }
}
```

The payload carries no duplicated direct-observable counts, ranked category
list, history-bearing-actor key, or model-authored bookkeeping.
`anti_repetition` remains bounded deterministic context derived from already
materialized state. It is a soft diversity hint, not evidence that a named
subject or system is active, recent, or exhausted.

`preferred_surface` reuses the existing technical-surface vocabulary. A valid
current non-`shell` surface hint is retained so ambient work can reinforce what
the user is about to observe. When the hint is absent or `shell`, deterministic
fallback chooses the least-covered of:

- `identity`, measured as independent users plus groups;
- `service`, measured as services; and
- `filesystem`, measured as concrete filesystem artifacts.

The stable fallback order is `identity`, `service`, then `filesystem` for equal
counts. These counts select a practical materialization surface; they do not
claim that the world lacks an entity or that balanced registries produce a
believable world.

### Role And Authority Boundaries

The Ambient Coordinator receives `presence_gap` as concise planning guidance
and expresses the resulting real-world assignment through its ordinary
Coordinator output. The independent Expert still receives the neutral
six-field brief and no DREAMLAND policy object or schema vocabulary. The
Integrator receives the same ambient guidance only as non-evidentiary mapping
priority so it can retain concrete residue when safe.

Focused mode does not receive `presence_gap` and is otherwise unchanged. This
decision does not alter focused Coordinator behavior, trigger-to-mode mapping,
creative-latitude limits, Expert report schema, Integrator patch schema,
foreground-evidence binding, additive registries, atomic safe apply, or
concurrency policy. The graph topology remains explicitly inspectable; only the
ambient contract revision advances its identifier to `enrichment-v12`.

### Deterministic Observability

Runtime telemetry records the v2 policy, objective, preferred surface,
canonical hash, and bounded anti-repetition counts. It does not retain
state-derived subject or system names, model reasoning, or new model-authored
logging. Existing coverage-summary telemetry remains the sole structural-count
record.

## Alternatives Considered

Adding `history-bearing_actor` as a fifth peer dimension was rejected because
it would require a new inferred actor taxonomy while mixing causal quality with
registry counts.

Keeping four ranked categories and adding saturation or rotation was rejected
because it would make the bookkeeping more elaborate without correcting the
underlying mismatch between structural balance and subjective population.

Removing deterministic guidance entirely was rejected because a small
repeatable planning input avoids spending Coordinator output tokens on
self-generated policy or counts.

Changing the shared Expert, Integrator, focused Coordinator, or safe-apply
contracts was rejected for this revision. The observed issue is ambient task
selection, so the change remains at that boundary.

## Consequences

Ambient input becomes smaller and easier to inspect. The Coordinator retains
freedom to invent broad scenario-appropriate sources while receiving a concrete
surface hint and one causal quality target. Existing structural counts remain
available once through `coverage_summary` rather than being duplicated inside
`presence_gap`.

The objective is intentionally semantic, so it cannot be proven by one
deterministic count. Evaluation must inspect both applied state and foreground
experience: whether a coherent independent source, activity, and residue became
available and whether subsequent commands made the world feel less centered on
the foreground user.

The policy still does not create autonomous actors or temporal lifecycle
authority. Enrichment may add historical, recurring, residual, and supported
bounded-current material; a future `WorldActivityAgent` remains responsible for
advancing or expiring live activity.

## Maintenance

Update this decision when the ambient objective, v2 payload fields, preferred-
surface selection, anti-repetition bounds, graph version, role boundary, or
WorldActivityAgent boundary changes. Keep `docs/ENRICHMENT_AGENT.md`,
`docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`, `docs/STATE_PROFILES.md`,
`docs/INDEX.md`, `ROADMAP.md`, prompts, graph,
telemetry, and owning tests aligned. Decision 0043 remains the historical
record of `presence-gap-v1`, pulse admission, bootstrap retry, and foreground
evidence; this decision supersedes only its presence-gap payload and ranking.
Add the related implementation commit after this checkpoint is committed.
