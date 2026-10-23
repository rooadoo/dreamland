# 0034 Portable Candidate-Driven Enrichment

Status: Active
Audience: maintainers, DREAMLAND operators, prompt authors, and coding agents
Purpose: record portable Expert routing, bounded recorded command evidence,
deterministic creative and integration guidance, candidate provenance, and safe
integration for the session-local EnrichmentAgent.

Decision 0036, `density-shaped-enrichment`, supersedes this record's candidate
limits, latitude-template version, graph version, and density-shaping guidance.
The portable role boundaries, provenance, eligibility, and safe additive
authority recorded here remain current. Decision 0037,
`automatic-enrichment-checkpoint`, adds deterministic interaction focus and a
visible structural normalization boundary while preserving those role and
authority boundaries.

## Overview

Decision 0033 established a session-local EnrichmentAgent with a
DREAMLAND-aware Coordinator and Integrator around a neutral independent Expert.
A proof-oriented Expert contract can make the workflow safe yet ineffective
by withholding plausible new detail from ordinary creative space.

DREAMLAND is first a system for building deep, engaging worlds. Internal
coherence and believable real-world behavior support that purpose; exact factual
realism must not become a reason to leave the environment flat. Established
state and operator constraints remain authoritative, but ordinary
underspecification is creative space in which the system should make and retain
plausible choices.

This decision refines role and evidence contracts. It does not change the
session lifetime, reusable trigger substrate, short-lock strategy, or additive
write authority recorded in decision 0033.

## Decision

### Portable Expert Routing

The Expert uses one stable generic system prompt and acts as an independent
subject-matter specialist. It is not permanently framed for an enterprise or
any other closed family of environments. For each work decision, the
DREAMLAND-aware Coordinator selects a compact routing lens with exactly
`domain` and `perspective`, a focused task, a semantic surface, and optional
job-local reference IDs. The lens identifies the relevant body of knowledge and
practical viewpoint without letting Coordinator author a replacement Expert
system prompt.

Scenario facts remain in state profiles and runtime state rather than reusable
role prompts. Stable prompts use field definitions and neutral placeholders,
not a repeated narrow one-shot scenario.

### Deterministic Creative Latitude

Coordinator does not spend output tokens explaining how expansive the Expert
should be. The deterministic `prepare_expert_brief` boundary resolves the
session's `generation_policy.liberty` and injects a task-neutral `latitude`
object into the Expert brief. The internal policy object and field names do not
cross the independent-role boundary.

The mapping is versioned as `creative-latitude-v3`:

| Level | Expert intent | Integrator retention intent | Candidate limit | Allowed origins |
| --- | --- | --- | ---: | --- |
| `strict` | Return only net-new details directly supported or logically unavoidable from the evidence. | Materialize every directly and unambiguously mappable eligible candidate without interpretive substance. | 2 | `derived` |
| `conservative` | Return a small number of net-new routine details strongly implied by the evidence. | Materialize every clearly mappable eligible candidate while avoiding marginal or lossy encodings. | 3 | `derived` |
| `moderate` | Construct one coherent representative realization and choose plausible ordinary details instead of leaving every choice unresolved. | Preserve a coherent representative cluster and distinct safely mappable artifacts that add net-new observable substance. | 5 | `derived`, `selected` |
| `expansive` | Produce multiple mutually supporting operational details and select plausible specifics where evidence leaves ordinary creative space. | Preserve most safely mappable material, including distinct kinds and relationships rather than a minimal summary. | 7 | `derived`, `selected` |
| `abundant` | Build a rich coherent causal cluster with candid operational texture and residue, preferring believable choices unless they conflict with evidence. | Preserve the fullest safe cluster up to the patch limit, including cross-surface artifacts, identities, residue, and relationships. | 8 | `derived`, `selected` |

Missing, malformed, or unsupported liberty values deterministically fall back
to `moderate`. Runtime logs record configured and effective levels, whether the
default was used, the candidate limit, template version, and instruction hash
without asking a model to narrate the decision.

Every level receives a common task-neutral prefix requiring affirmative,
concrete, mutually consistent details from one realization and forbidding
inspection advice, possibility lists, and alternative menus. This prefix does
not reduce the candidate limit or creative latitude. It defines the shape of
materializable output; the level-specific instruction still determines how
much compatible detail the Expert should select.

The Expert receives exactly six top-level input fields:

```json
{
  "expertise": {
    "domain": "<relevant technical domain>",
    "perspective": "<relevant practical viewpoint>"
  },
  "question": "<focused real-world task>",
  "latitude": {
    "instruction": "<deterministic neutral instruction>",
    "candidate_limit": 5
  },
  "host": {},
  "observations": [],
  "references": []
}
```

`expertise`, `question`, and `latitude` route and bound generation; none is
evidence that the described host already has a property. `host`, observations,
and references carry neutral real-world context.

The effective level remains private runtime policy used for deterministic
origin enforcement, Integrator guidance, and logging. The Expert receives only
the resolved `instruction` and `candidate_limit`; repeating the internal level
label would add no authority or evidence and could bias the Expert toward an
opaque category name instead of the exact behavior requested.

### Bounded Recorded Command Evidence

Enrichment context version 2 retains bounded output excerpts from the last four
recorded commands on the target host. Peer command histories remain
metadata-only. Target-host output has a 6,000-character aggregate budget and a
1,500-character per-entry budget; newest entries receive budget first, and
oversized stdout or stderr uses deterministic head-and-tail clipping with
omitted-character counts. Each retained result is labeled
`evidence_kind = recorded_command_result` and carries a deterministic command
surface.

Within the recorded-command portion of Expert observation selection, command
results matching Coordinator's selected surface are ordered first and then by
recency. Selected entries cross the independent-role boundary as
`recent_command_result` observations within the existing observation-count
limit. Their excerpt strings are untrusted evidence of foreground output
recorded in session history. They do not prove that the foreground response's
structured state update parsed or was accepted, and they do not weaken
Integrator's fresh-state authority. The handoff introduces no parser repair,
retry, salvage, or acceptance policy.

Runtime telemetry records evidence counts, excerpt characters, truncation, and
omission without copying excerpt content into logs. Enabled Langfuse traces
continue to expose the bounded role input for operator inspection.

### Candidate-Centered Expert Contract

The Expert returns schema version 2 with exactly
`{v,status,reason,conditions,items,gates,relations}`. Its items are candidate
details for addition, not restatements of supplied evidence. Each item has
exactly `{id,kind,name,detail,origin,basis}`:

`status = ok` requires at least one item and a null `reason`.
`status = insufficient` carries one short functional reason and empty
conditions, items, gates, and relations.

- `origin = derived` marks a net-new consequence reasonably drawn from supplied
  context without choosing among materially different alternatives. The
  effective latitude determines whether that consequence must be unavoidable,
  strongly implied, or may use broader coherent inference;
- `origin = selected` marks one believable concrete realization chosen from
  several ordinary possibilities.

`basis` is an explicit array of supplied reference IDs. It may be empty when a
candidate depends only on neutral host or observation context, but the runtime
never infers provenance by scanning `detail`, `reason`, or relationship prose.
Every referenced ID must exist in the Expert brief. Relations preserve compact
causal structure between items and references; they are not the source of
reference dependency discovery. Every relation must involve at least one
returned item. An item-to-reference relation is valid only when that item's
`basis` contains the same unprefixed reference ID.

Relations are auxiliary rather than an authority or provenance boundary. For
an otherwise valid `ok` report, DREAMLAND therefore validates each relation
independently, preserves valid relations in their original order, and discards
an invalid relation without discarding valid candidate items. The parser returns
structured rejection details, and the graph node logs only the original index,
stable deterministic rule, validation code, and location. Neither layer repairs
a relation, invents a missing basis, copies relation text into runtime logs, or
spends another model call. Core JSON, status, item, basis, gate, and count
failures remain strict, and `insufficient` reports cannot be legalized by this
filtering.

Each `ok` item describes affirmative current-world material in one coherent
realization. When several implementations could fill the same role, Expert
selects one when the effective latitude permits. It returns more than one only
when supplied evidence supports their distinct coexistence. Candidate breadth
is measured in mutually supporting detail, not competing substitutes.

The deterministic parser and classifier apply the effective candidate limit,
origin rules, novelty check, and gates. `derived` items are admitted at every
level;
`selected` items are admitted at `moderate`, `expansive`, and `abundant`;
any gated item remains blocked until a future deterministic condition contract
can establish its prerequisites. A candidate can therefore be
admissible because the operator-authorized latitude permits a coherent
selection, not because the detail was already a materialized fact.

Existing references remain constraints and provenance handles. They should not
be emitted as candidates merely to satisfy the output contract. If no net-new
candidate remains, the workflow stops before Integrator rather than paying for
a model call that can only duplicate state.

### Candidate Provenance And Integration

After the fresh snapshot, deterministic currentness filtering compares every
reference used by an accepted candidate with its initial projected binding. A
missing or changed reference blocks only candidates whose `basis` names it.
Independent candidates continue, and the runtime rebuilds the eligible-only
report, relations, blocked count, and binding union before Integrator. If every
candidate is blocked, the graph ends as `expert_no_eligible_items` without an
Integrator call. The runtime never recovers a missing reference through label
matching or model inference.

Integrator therefore receives only retained candidate items, relations whose
item endpoints remain retained, the effective eligibility envelope, a fresh
current state projection, and current bindings for the union of explicit
reference IDs in retained item `basis` arrays. Coordinator prose and blocked
candidates do not cross this boundary.

The same private effective liberty also selects deterministic top-level
`integration_guidance = {"instruction": "<deterministic instruction>"}` for
Integrator. This non-evidentiary instruction asks Integrator to retain more of
the eligible causal cluster and its useful cross-surface texture as liberty
increases. It does not establish host facts, override fresh state, expand write
authority, weaken provenance or dependency checks, or forbid a typed no-op.
This preserves repeatability and avoids spending model tokens having
Coordinator restate an expansion policy.

Each patch contribution retains a nonempty `source_item_ids` list. Deterministic
validation requires each source ID to name a retained Expert candidate. Every
reference binding in the union of the bases for candidates actually cited by
patch contributions is a mandatory exact patch dependency. Bindings belonging
only to retained but unused candidates do not constrain that patch. The
Integrator cannot omit the source-specific race checks, and runtime code does
not depend on an optional relation or free-text rationale to discover them.

Integrator must map candidates to net-new world material. It may return a typed
no-op when all material is already present, cannot be represented within the
allowed registries, or has no supported contribution. A structurally safe patch
that merely copies an existing value into a second registry is not a useful
enrichment outcome and should be rejected or recorded as a novelty no-op where
the current deterministic and model boundaries can identify it.

As a narrow representational backstop, Integrator may remove advice or
possibility framing when an eligible candidate already identifies exactly one
unambiguous concrete detail. It may not add facts or choose between competing
options while doing so. Conditional candidates and alternative menus have no
safe declarative mapping and must be omitted.

### Deterministic Structural Normalization

Decision 0037 adds a visible `normalize_integrator_output` node after Integrator
and before final proposal validation. Versioned policy
`integrator-output-normalization-v1` prunes array-valued leaves, then recursively
prunes objects made empty by that removal. Exact scalar and object siblings
remain unchanged; runtime code neither reads array elements to select content
nor invents a replacement representation. A contribution is discarded only
when its complete value becomes empty. If every contribution is discarded, the
workflow ends deterministically as `no_supported_contribution` without a model
repair call.

Multiple contributions may retain one registry path and key only when all use
`merge_missing` and their flattened desired scalar leaf paths are pairwise
disjoint with no prefix collision. The contributions, IDs, and source-item
lists remain separate, preserving the per-contribution eight-source provenance
bound. Every other duplicate-target combination remains invalid. Normalization
therefore salvages only structural shapes whose desired writes are
unambiguous; it does not add facts, merge provenance, choose between values,
change operations, or broaden write authority.

Normalization logs its policy version, affected structural paths and counts,
empty-object and dropped-contribution counts, compatible duplicate-target
counts, and terminal outcome. It does not log array contents, invoke a model,
or ask Integrator to narrate the change. The resulting proposal still passes
all provenance, dependency, registry, touched-path, live-state, and atomic
safe-apply checks.

### Safe Mutation Is Not Proof-Only Generation

`shadow` mode executes the complete proposal, normalization, and validation path
without mutation. `safe` mode permits only bounded `put_if_absent` and
`merge_missing` additions to allowlisted host-local keyed registries. It does
not permit deletion, replacement, correction, array writes, or overwriting
current state. Arrays removed by graph normalization are never written; direct
safe-apply validation remains strict if an array reaches it through another
caller.

These controls govern authority, touched-path conflicts, dependency races, and
atomic application. They do not require all candidate material to have existed
before enrichment. Deliberately selected details are valid at the appropriate
latitude because materializing coherent plausible choices is the purpose of the
agent.

Final dependency checks account for the boundary between projected Integrator
state and complete live state. The runtime privately retains the complete target
host from the current snapshot, then records only a SHA-256 fingerprint for each
declared complete dependency value on the in-memory patch. Final apply requires
both the projected expected fields and that complete-value fingerprint to match.
This prevents an unchanged omitted artifact body from causing a false race while
still rejecting keys or values—visible or omitted—added or changed after the
Integrator snapshot. Complete dependency values and their fingerprints are not
part of the model payload or ordinary runtime logs.

### Inspectable Workflow And Observability

The current workflow is `enrichment-v6`:

```text
START
  -> snapshot_initial
  -> prepare_coordinator_context
  -> coordinator_agent
  -> prepare_expert_brief
  -> expert_agent
  -> classify_expert_items
  -> snapshot_current
  -> resolve_current_references
  -> integrator_agent
  -> normalize_integrator_output
  -> validate_integrator_output
  -> END
```

The graph remains deliberately explicit and sequential. Classification,
candidate origin, basis use, candidate-local reference currentness, tagged
Integrator decisions, structural normalization, provenance checks, terminal
reason codes, and effective latitude are logged from parsed structures. This
adds no model calls and no model-authored logging burden. INFO records keep
bounded counts and aggregates; bounded per-ID detail remains DEBUG data.

`integrator.guidance_prepared` records the private effective latitude, template
version, integration-instruction hash, and instruction length without logging
the instruction. Snapshot telemetry records command-evidence count, characters,
truncation, and omission. Expert-brief telemetry records selected and
same-surface counts, characters, truncation, and the deterministic selection
strategy. Neither event logs excerpts.

Surface telemetry distinguishes the trigger or manual `requested_surface` from
Coordinator's canonical `selected_surface`. Patch telemetry records separate
host-relative `registry_targets` and per-registry contribution counts; registry
paths are application destinations, not semantic surfaces.

## Alternatives Considered

Having Coordinator author expansion or retention paragraphs was rejected
because it spends Coordinator output tokens, varies semantically equivalent
Expert or Integrator inputs, and makes trace comparison harder. A deterministic
mapping is cheaper, repeatable, and independently testable.

A fixed enterprise-specialist identity was rejected because DREAMLAND can model
any operator-defined computing context. Enterprise, home, research, industrial,
embedded, historical, fictional, speculative, ambitious, and absurd settings
are illustrative examples rather than a closed taxonomy.

Proof-only eligibility was rejected as the general enrichment policy. Manual
testing showed that it preserved already-established facts while blocking the
new texture the agent exists to create. `strict` and `conservative` still offer
operator-controlled narrow behavior without imposing it on expansive worlds.

Deriving reference use from `@reference` relations or free-text rationale was
rejected because a reference used only in rationale could disappear from
the dependency graph. Explicit per-item `basis` preserves that dependency.

A dynamic Expert system prompt authored by Coordinator was rejected because it
would vary the highest-authority Expert instruction per job. The bounded routing
lens and deterministic latitude field provide specialization and creative
freedom while the stable system contract remains inspectable.

A lexical runtime filter for advisory verbs was rejected. Ordinary technical
facts can legitimately contain the same words, and text matching cannot decide
whether alternatives coexist. Declarative generation belongs in the role
contracts, with Integrator providing only the narrow semantic backstop above.

Omitting command output from enrichment context entirely was rejected because
the user can observe foreground detail even when its structured state
proposal fails. Passing full
raw histories was also rejected because it would increase prompt size and
expose unrelated peer activity. Bounded target-host excerpts preserve recent
continuity evidence without treating visible output as accepted state.

Model-authored logging explanations were rejected as a general observability
mechanism. They cost tokens and latency and are less repeatable than events
derived from parsers and validators.

## Consequences

The workflow can generate a deep world without treating established state as
the complete universe of permissible detail. Higher liberty levels deliberately
authorize concrete representative choices, while explicit candidate origins and
bases preserve traceability about how those choices entered the integration
path.

The same deterministic liberty now guides both candidate breadth and retention
through integration, while the effective label remains private. This reduces
the tendency to flatten an abundant Expert cluster into a sparse single-surface
patch without converting expansion guidance into evidence or mutation
authority.

Bounded recorded command results give the Expert continuity with foreground
details the user has already seen, including when a structured update failed.
That improves consistency but creates an explicit distinction between recorded
output evidence and accepted world state; Integrator must continue to preserve
the latter as authoritative.

Minor plausible inconsistencies remain experimental tuning signals rather than
a reason to collapse the world into sparse certainty. Direct contradictions of
established facts, newer state, or operator constraints remain invalid.

Reference drift no longer discards an otherwise useful causal cluster before
integration. Only candidates coupled to the missing or changed basis are
removed, preserving independent finishing work while preventing stale evidence
from reaching Integrator.

Per-job candidate and patch limits bound latency, token use, and write size.
They are not a global cap on world richness: repeated just-in-time and read-ahead
passes may add further coherent causal layers as user attention moves.

The current reference catalog remains host-local. Target-bound world-scoped
references are deferred. Until the binding and final-apply
contracts can identify and revalidate world-relative paths, Coordinator routing
text must not be treated as a substitute evidence channel.

Session state remains isolated and temporal. Enrichment does not create a
shared world, mutate a source profile, or persist state across sessions unless
an operator explicitly saves a state profile.

## Maintenance

Update this decision when liberty mappings, Expert input or output schemas,
candidate origins, basis semantics, currentness filtering, gate resolution,
reference scopes, projection-aware dependency matching, Integrator provenance,
structural normalization, graph version, or write authority changes. Keep
`docs/ENRICHMENT_AGENT.md`, the three role
prompts, parsers, graph, validators, telemetry, and focused tests aligned. Add
the related implementation commit after this experimental revision is committed.
