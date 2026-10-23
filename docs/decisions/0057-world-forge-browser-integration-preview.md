# World Forge Browser Integration Preview

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and coding agents
Purpose: record the prepare-before-confirm and provider-free browser-integration boundary.

## Context

The Phase 4 foundation originally created a confirmed `WorldSeed` directly.
The UI correctly identified that the presenter must first review the exact
service-rendered representation. The UI also needed a real private transport
checkpoint before live generation, without allowing its broader publication
fixtures to become backend Forge semantics.

## Decision

Split preparation from confirmation. A private presenter creates an expiring
unconfirmed preparation containing exact validated answers and the deterministic
rendered sentence. Atomic confirm-and-submit consumes that reference, records
server time, constructs the canonical confirmed seed, and creates one
idempotent job. Exact retry is resolved before consumed-state rejection.

Use independent event coordinates: service epoch, opaque stream ID, event ID,
numeric sequence, and opaque cursor. Lifecycle events require authoritative
snapshot reconciliation. Missing history returns `resync_required`; in-memory
restart loss returns retry `none` and never redispatches an uncertain mutation.

Publish World Forge separately at
`contracts/world-forge-api/v0.1.0-rc.1/`. Provide an explicitly invoked,
loopback-only, provider-free target with owner-only bearer/bootstrap files and
deterministic timing/failure scenarios. Its selected receipt is visibly preview
provenance over a deterministic fixture revision. It provides no Stage,
publication, catalog, attendee, canary, or live-Forge authority.

## Consequences

The UI BFF can replace its fixture adapter and owners can review real transport,
refresh, cancellation, and recovery behavior while live Forge remains under
development. Ordinary DREAMLAND startup and SSH/runtime behavior remain
unchanged and incur no Forge listener, worker, timer, provider, or bookkeeping
cost.

The target is integration evidence only. Durable jobs, live A/B/C generation,
repair, independent validation, immutable live storage, shared provider budget,
and Forge-to-Stage remain later checkpoints.

Decision 0058 subsequently corrects the unfrozen integration contract in an
immutable RC2. RC1 remains the historical artifact described by this decision.

## Maintenance

Implementation: `9cf5b7130ab2d4c2419053e557b34652b65b9633`. Update this decision
before changing preparation consumption, server confirmation,
idempotency ordering, position/cursor semantics, restart loss, private file
format, source provenance, or the separation from downstream authority.
