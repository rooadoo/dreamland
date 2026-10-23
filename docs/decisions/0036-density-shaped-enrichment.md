# 0036 Density-Shaped Enrichment

Status: Active
Audience: maintainers, DREAMLAND operators, researchers, prompt authors, and
coding agents
Purpose: record why EnrichmentAgent expands candidate capacity and explicitly
shapes hierarchical and lifecycle depth without expanding safe mutation
authority.

Decision 0037, `automatic-enrichment-checkpoint`, adds automatic priority,
bounded deferral, interaction focus, and deterministic Integrator-output
normalization, and advances the graph identifier from `enrichment-v5` to
`enrichment-v6`. The density limits and shaping rules in this record remain
current.

## Overview

The candidate-driven workflow produced coherent, useful finishing material, but
its eight-item ceiling frequently made a successful run feel like a sketch. A
filesystem Expert could spend the entire budget on top-level directories, and a
process Expert could identify the principal services without leaving evidence
of helpers, intermittent activity, completed work, or residue. The foreground
emulator could add detail later, but the enrichment layer was not steering it
toward enough surrounding structure.

DREAMLAND's product order is engaging depth, continuity, believability, then
exact realism. Increasing only the item count would not satisfy that order: a
larger flat inventory remains flat. This checkpoint therefore changes both
bounded capacity and the deterministic shape of high-latitude work while
preserving the existing evidence, provenance, race, and safe-apply boundaries.

## Decision

At this checkpoint the sequential workflow was identified as `enrichment-v5`;
Decision 0037 advances the visible normalized graph to `enrichment-v6`. It retains one
Coordinator call, one independent Expert call, one Integrator call, one causal
cluster, and one fresh-state validation and apply path. Parallel fan-out and
multiple patch clusters remain deferred.

`creative-latitude-v4` maps the session's existing liberty setting as follows:

| Level | Candidate limit | Density intent |
| --- | ---: | --- |
| `strict` | 2 | Only directly supported or unavoidable net-new material. |
| `conservative` | 4 | A small, strongly implied addition. |
| `moderate` | 8 | One representative realization with supporting observable context. |
| `expansive` | 12 | A substantial connected layer using most capacity when coherent material exists. |
| `abundant` | 16 | A dense coherent layer using full capacity when distinct coherent net-new material supports it. |

The exact deterministic instruction, not the internal level label, still
crosses the Expert boundary. At higher levels it asks for applicable structural
nesting, concrete leaves, lifecycle variation, causal relationships, and
operational residue rather than a flat inventory. The stable Expert prompt adds
portable shape rules:

- hierarchical subjects should include leaf material below containers at more
  than one meaningful depth;
- time-varying subjects should distinguish persistent state, intermittent or
  short-lived activity, completed activity, and durable aftereffects, while
  preserving relevant parentage, attachment, or causal relationships; and
- these dimensions are open-ended and conditional, not a scenario taxonomy,
  one-shot answer, required checklist, or permission for generic clutter.

The Integrator receives a separate deterministic retention instruction. It
preserves distinct nested paths and leaf artifacts rather than collapsing a
tree to parent summaries. It preserves temporal qualification, durable
aftereffects, and relevant detached or orphaned relationships when
non-dedicated activity is stored in `facts_by_id` instead of rewriting every
candidate as permanently and simultaneously active.

Size constants live together in
`src/dreamland/background_agents/enrichment/limits.py`. Expert candidates and
patch contributions are capped at sixteen. Coordinator anchors, per-item basis
references, conditions, gates, relations, and per-contribution source IDs keep
separate eight-entry limits. The larger candidate ceiling does not implicitly
enlarge every auxiliary array.

Expert and Integrator output caps rise from 1,024/2,048 to 2,048/4,096
tokens to carry the larger bounded reports. Decision 0037 raises Coordinator
from 512 to 2,048 tokens, leaving room for compact JSON after reasoning without
requesting verbose reasoning or logging text.

Deterministic logs add capacity and shape measurements without asking a model
for explanations. Expert reporting records the effective candidate limit,
unused capacity, and whether it reached the limit. Patch provenance reporting
records contribution capacity, eligible/sourced/unsourced item counts,
allowlisted registry counts, common-separator path-segment depth,
ancestor-linked path counts, and maximum proposed ancestor-chain length. Raw
model-authored paths and artifact prose are not copied into these metrics.

Safe authority does not change. Enrichment remains additive and cannot expire,
stop, replace, or delete an entity. Recurring, transient, detached, or orphaned
activity can be represented by a lifecycle pattern, a temporally qualified
observation, or durable residue appropriate to the evidenced setting. Truly
time-driven appearance and disappearance requires a future lifecycle contract;
storing a short-lived process as unqualified permanent-current state would be
dishonest.

Filesystem artifact contributions must use concrete absolute syntax supported
by the evidenced platform. Safe application recognizes POSIX and Windows
absolute paths and rejects common relative or home-shorthand forms. Opaque
platform-native keys remain admissible rather than imposing a closed path
taxonomy on ambitious, historical, or unusual simulations.

## Alternatives Considered

Raising only the hard ceiling was rejected because models could spend sixteen
slots on sibling container labels or a flat process inventory.

Adding a dedicated writable `processes_by_id` registry was deferred. The
current state can carry stable process observations and lifecycle patterns in
`facts_by_id`, while a dedicated live-process registry would need explicit
expiry, transition, and foreground-rendering semantics before transient state
could remain truthful.

Giving Integrator delete or correction authority to simulate process churn was
rejected for this checkpoint. That would change EnrichmentAgent's capability
boundary rather than tune its finishing work.

Multiple Experts, parallel fan-out, and multiple independently committed
clusters were deferred until sequential sixteen-candidate traces provide
latency, cost, coherence, and conflict data.

## Consequences

High-liberty jobs can now steer the foreground toward materially deeper
hierarchies and more convincing signs of use. Moderate, the packaged fallback,
also grows from five to eight candidates so ordinary sessions are less sparse;
strict and conservative remain deliberately bounded.

Larger atomic clusters increase token use, latency, and the amount of work lost
if one dependency or contribution conflicts. This is accepted for the current
experimental concurrency posture and is visible through existing timing,
conflict, and new capacity metrics. Per-job limits remain latency and safety
controls, not a world-size ceiling; repeated triggers can add further layers.

## Maintenance

Update this decision when latitude limits, density-shaping rules, role output
caps, auxiliary limits, patch atomicity, process lifecycle semantics, or write
authority change. Keep `docs/ENRICHMENT_AGENT.md`, `docs/OPERATIONS.md`,
`ROADMAP.md`, prompts, configuration defaults, graph validation, telemetry, and
focused tests aligned.
