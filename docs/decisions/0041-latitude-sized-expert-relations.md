# 0041 Latitude-Sized Expert Relations

Status: Active
Audience: maintainers, DREAMLAND operators, researchers, prompt authors, and
coding agents
Purpose: record why Expert relation capacity follows creative latitude and why
relation overflow is normalized without discarding otherwise useful material.

## Overview

Decision 0040 raised the abundant candidate and contribution limits to
twenty-four. Relations need a different bound from conditions and gates.

A connected tree over twenty-four candidate items can require twenty-three
relations before adding any useful link to an observed reference. Keeping
relations at eight therefore made the artifact and topology contracts
internally inconsistent. Conditions, gates, evidence bases, anchors, and source
provenance do not have the same structural requirement.

This change adds no JSON repair, structured-output mode, model retry, or
provider-specific behavior.

## Decision

Expert relations receive a distinct hard maximum,
`MAX_EXPERT_RELATIONS = 24`. The effective relation limit follows the same
deterministic creative-latitude mapping as candidate capacity:

| Latitude | Candidate limit | Relation limit |
| --- | ---: | ---: |
| `strict` | 2 | 2 |
| `conservative` | 4 | 4 |
| `moderate` | 8 | 8 |
| `expansive` | 12 | 12 |
| `abundant` | 24 | 24 |

The neutral six-field Expert brief carries that resolved value as
`latitude.relation_limit` beside `latitude.candidate_limit`. This is
task-neutral deterministic policy, not evidence and not a Coordinator-authored
assignment. The private latitude label still does not cross the model boundary,
and the mapping remains `creative-latitude-v5` because candidate capacity and
creative guidance have not changed.

Conditions and gates remain capped at eight. Per-item basis references,
Coordinator anchors, and per-contribution source item IDs also remain capped at
eight. Increasing connective topology does not broaden prerequisite, evidence,
selection, or mutation authority.

The graph-specific Expert parser applies relation policy
`expert-relations-v2` in authored order:

1. validate each relation independently against the existing endpoint and
   basis rules;
2. discard an invalid relation without discarding valid items;
3. discard an exact duplicate relation triple;
4. retain the first valid unique relations up to the effective relation limit;
   and
5. discard later overflow relations without rejecting the otherwise valid
   report.

The Expert prompt requires materially important backbone relations first,
including hierarchy, attachment, ownership, lifecycle, and causality, before
optional cross-links. This makes deterministic first-N retention useful when
the model still exceeds the supplied bound. Runtime code never invents,
rewrites, reorders, or semantically ranks a relation. The direct strict parser
continues to reject arrays beyond the hard twenty-four-entry public contract;
overflow salvage is an explicit LangGraph handoff policy.

The visible graph identifier advances from `enrichment-v8` to
`enrichment-v9`. The graph topology remains sequential and directly
inspectable; the version change records the Expert handoff and normalization
contract rather than a new role, retry, or branch. Expert report schema version
2 and Integrator mutation authority remain unchanged.

Telemetry records `expert-relations-v2`, the effective relation limit, and
received, accepted, invalid, duplicate, overflow, and total discarded counts.
When any relation is discarded, the aggregate `expert.relations_evaluated`
record is emitted as a default-visible warning with those counts. Each
discarded entry also emits a bounded DEBUG `expert.relation_discarded`
diagnostic with its authored index, stable rule, and validation location, but
per-entry diagnostics are not promised or retained when DEBUG logging is
disabled. Neither record copies model-authored relation text. This requires no
model-generated logging and no additional model call.

## Alternatives Considered

Keeping the eight-relation cap was rejected because it deterministically
discarded connected abundant reports that obeyed the requested artifact
capacity. Reducing Luna's richness or asking for flatter material would work
against DREAMLAND's priority of producing deep, engaging worlds.

Raising every auxiliary collection to twenty-four was rejected. The Luna
finding establishes a topology-capacity requirement, not a reason to broaden
conditions, gates, evidence bases, anchors, or source provenance.

Rejecting every overflow report was rejected because relation ordering and
bounded deterministic retention can preserve a useful causal backbone without
granting new authority. Unbounded relations were rejected because they would
remove the per-call cost, trace-size, and handoff boundary.

Increasing the Expert output-token limit was rejected for this correction. The
valid over-limit reports demonstrate a contract mismatch, not output-cap
exhaustion, and a larger token allowance would not make an eight-entry validator
accept them.

## Consequences

An abundant Expert can now describe topology commensurate with its twenty-four
candidate items. Lower-latitude jobs retain proportionally smaller reports, so
strict and conservative work does not inherit an unnecessary twenty-four-link
budget. Valid candidate material can reach Integrator even when individual
relations are invalid, duplicated, or beyond the resolved limit.

Authored order now determines which valid relations survive overflow. Prompting
for the backbone first makes that deterministic tradeoff explicit and
inspectable. Integrator still receives only relations whose item endpoints
survive candidate eligibility and fresh-reference filtering, and relations
still establish neither provenance nor patch dependencies.

The correction has now passed both the focused 10-to-16-relation regression
panel and a live Luna safe-mode panel. Success means reports advance to normal
eligibility and Integrator decisions; it does not require every candidate to
materialize or convert a legitimate safe no-op into a patch. Future panels
should continue watching relation discard counts and lower-latitude behavior,
but relation capacity is no longer an unvalidated implementation hypothesis.

## Maintenance

Update this decision when the hard or effective relation limits, latitude
mapping, relation normalization order, relation telemetry, Expert brief shape,
graph identifier, or relation authority changes. Keep
`docs/ARCHITECTURE.md`, `docs/ENRICHMENT_AGENT.md`, `docs/OPERATIONS.md`,
`docs/INDEX.md`, `ROADMAP.md`, the Expert prompt,
and focused relation tests aligned. Decision 0040 records the preceding
artifact-capacity contract.
