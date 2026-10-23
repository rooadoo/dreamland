# World Forge RC2 Integration Stabilization

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and coding agents
Purpose: record the corrective RC between provider-free browser integration and live World Forge.

## Context

UI integration against World Forge RC1 confirmed the prepare-before-confirm,
event-recovery, cancellation-fence, and provenance boundaries, but found four
contract/runtime contradictions. The runtime could emit an unpublished
idempotency-capacity error, terminal cancellation advertised a retry that could
not change its outcome, recipe identity errors changed shape at HTTP, and the
deterministic deadline scenario could expire before its advertised timestamp.
The broad schema also left existing lifecycle relationships to handwritten
consumer validation.

## Decision

Preserve the published `v0.1.0-rc.1` artifact exactly and publish the correction
as `v0.1.0-rc.2` before beginning live provider work.

Expose `idempotency_capacity_exhausted` with retry `none`. The current bounded
in-memory replay ledger has no within-epoch expiry, so repeating the request
cannot succeed before epoch rotation. Keep ordinary job-capacity rejection as
the separately replayed `capacity_exhausted` result with retry `same_request`.
Return terminal `job_terminal` with retry `none`, while preserving exact replay
of an earlier successful cancellation.

Map unknown recipes and stale revisions to their published top-level errors;
retain field-addressable answer defects under `validation_failed`. Require the
deadline transition to observe the job's authoritative deadline, and have the
deterministic worker wait on that same value.

Condition recipe-field limits by kind. Bind terminal flags and cancellation
availability to lifecycle state, require safe messages for unsuccessful
terminal states, require a receipt exactly for `selected`, and fix receipt and
projection provenance to the same preview source. Responses remain additive,
while unknown lifecycle and provenance enums fail closed. Mark every safe-error
fixture as HTTP-reachable or explicitly schema-only; only the defensive
`response_too_large` and future `temporarily_unavailable` cases are schema-only.

## Consequences

The UI can pin one internally consistent artifact for its first human review.
The correction does not add durability, provider calls, live selection, Stage
handoff, publication, catalog, or attendee authority. It remains an explicitly
started provider-free backend integration preview.

Normal DREAMLAND composition remains inert: no Forge import, listener, worker,
timer, provider, reservation, or bookkeeping is added to SSH or ordinary
runtime paths.

## Maintenance

Implementation: `640075890c638e4c2c3dc99c906f0bc72300b472`. Update this
decision before changing safe-error reachability, idempotency-capacity retry semantics,
recipe error mapping, deadline authority, lifecycle schema relationships, or
the immutable RC1 boundary.
