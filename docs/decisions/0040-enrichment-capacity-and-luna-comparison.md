# 0040 Enrichment Capacity

Status: Active
Audience: maintainers, DREAMLAND operators, researchers, prompt authors, and
coding agents
Purpose: record the paired Enrichment artifact capacity and its authority limits.

## Overview

This decision raises the paired Expert-candidate and Integrator-contribution
ceiling from sixteen to twenty-four, preserving bounded additive authority.
It supersedes decision 0036 only for capacity and latitude-version values.

## Decision

The hard Expert-candidate and Integrator-contribution limits rise together from
16 to 24. The `abundant` creative-latitude level receives the new 24-candidate
budget; `strict`, `conservative`, `moderate`, and `expansive` remain at 2, 4, 8,
and 12. The deterministic mapping identifier advances from
`creative-latitude-v4` to `creative-latitude-v5`.

The visible sequential graph identifier advances from `enrichment-v7` to
`enrichment-v8` because the graph's validated Expert and Integrator artifact
contract changed. Its topology does not change: one Coordinator, one Expert,
one Integrator, one causal cluster, and one fresh-state validation and apply
path remain directly inspectable. The graph version does not imply parallel
fan-out, another retry branch, or broader mutation authority.

The tested increase was deliberately limited to the two paired
artifact-throughput ceilings. Coordinator anchors, Expert conditions, gates,
relations, and other auxiliary collections remained capped at eight. Candidate
basis references and per-contribution `source_item_ids` retained their existing
eight-entry bounds. Relations require a separately sized contract for a
connected twenty-four-item report. Decision 0041 records the subsequent
`enrichment-v9` correction to that relation bound.

Safe application remains additive, host-local, dependency checked, and atomic
per causal cluster. Twenty-four candidates are an opportunity to build a richer
connected layer, not a quota to fill with generic clutter. Integrator may still
return a typed no-op or omit material that cannot be represented safely.

## Alternatives Considered

Keeping the ceiling at sixteen limits the connected detail available to each
job. Raising every auxiliary bound together was rejected: conditions, gates,
anchors, provenance, and connective relations have distinct requirements.
Decision 0041 records the separate relation-capacity contract.

Parallel Experts remain deferred because they add reconciliation and race
surfaces to the sequential workflow.

## Consequences

An abundant job can now propose and safely materialize 50 percent more distinct
artifacts. This better aligns the cost of Coordinator, Expert, and Integrator
with DREAMLAND's depth-first product priority, while retaining a finite per-job
latency and race boundary.

Larger reports and atomic clusters may consume more output tokens, take longer
to validate, and lose more work when one cluster-level dependency fails. Cap
utilization, unused capacity, accepted and applied contribution counts,
latency, token usage, cost, conflicts, lateness, foreground matches, and
subjective depth remain required checkpoint measurements.

## Maintenance

Update this decision when Expert or patch artifact limits, lower latitude
levels, auxiliary/evidence/provenance bounds, graph identity or topology, role
models, output caps, or packaged model defaults change. Keep
`docs/ARCHITECTURE.md`, `docs/ENRICHMENT_AGENT.md`, `docs/OPERATIONS.md`,
`docs/INDEX.md`, `ROADMAP.md`, prompts,
configuration, graph validation, telemetry, and focused tests aligned. Preserve
decision 0036 as the historical rationale for the earlier eight-to-sixteen
checkpoint.
