# 0051 Integration-Only Product Lifecycle Preview

Status: Active
Audience: maintainers, UI BFF developers, DREAMLAND operators, and coding agents
Purpose: record the isolation, authority, lifecycle, and transport boundaries of
the Phase 1 private product-session integration preview.

## Overview

Decision 0050 established an inert private-product composition seam and an
unfrozen capability-discovery release candidate. The UI now needs a real,
stateful backend checkpoint for integrating session allocation and lifecycle
recovery before the command, event, transcript, enrichment, and provider gates
that make the shared product-session core complete.

Phase 1 therefore implements the agreed lifecycle semantics as protocol-neutral
in-memory code and exposes them only through a separately invoked integration
preview. It does not turn the normal DREAMLAND process into a product server and
does not make any product capability live.

## Decision

### Preview-only composition

The protocol-neutral lifecycle implementation lives under
`src/dreamland/product/`. It is constructed only by the explicit development
tool in `tools/product_lifecycle_preview.py`. Normal `python -m dreamland`
composition is unchanged:

- when `[product_api] enabled` is false, no product module is imported or
  product object is constructed;
- when it is true, `compose_product_runtime()` continues to fail startup closed;
- no product listener, worker, timer, subscription, provider, tracing hook,
  capacity reservation, or background sweeper is added to the runtime.

The preview tool is loopback-only, private-bearer protected, and selected
explicitly by an integrator. Fault scenarios are process-start configuration,
never request input. Installing the package or importing the lifecycle module
does not allocate a session or start work.

For integration runs, the bearer is read from a bounded owner-only regular file
without following symbolic links so it need not appear in process arguments.
The standalone server bounds connection admission and applies per-read and
absolute connection deadlines. Parser/default failures are normalized to the
same safe, non-cacheable problem envelope as routed failures. The required
sockets, bounded request threads, and deadline timers are constructed only by
the explicitly invoked preview tool.

Capability discovery is the source of the running instance's non-authorizing
epoch; static fixtures do not supply one. The restart-loss scenario reports the
discarded prior epoch in startup metadata and uses separately indexed prior and
rotated-current synthetic authority fixtures, making the loss-and-fresh-action
flow externally testable without printing a credential.

### Immutable launch and authority

The preview assembler starts with a fixed synthetic integration catalog. Each
selection binds an actor authority class, exact immutable revision, allowed
operations, limits, policy and notice identity, and expiry policy. Revision
documents are canonicalized and hashed once; creating a session reparses the
canonical bytes so sessions cannot share mutable state or change the catalog.
The preview never consults the process-selected profile, accepts a profile name
or path, or writes the legacy profile-session index.

The simulator also starts with synthetic actor-context authority and launch
grants. There is intentionally no Phase 1 grant-minting HTTP route. This keeps
the five-route lifecycle preview focused while letting a BFF test adapter hold
the seeded private material server-side. A later catalog or selection-exchange
phase will own real grant resolution.

Actor IDs, selection IDs, revision IDs, service epochs, session IDs, and safe
references are locators or freshness evidence, not authority. Opaque actor,
launch, and per-session authority is bound to one service epoch, actor,
authority class, audience, operation set, and relevant revision or session.
Create consumes the launch grant. Close or expiry revokes the session
authority. Exact idempotent replay may return a recorded result after that
consumption or revocation; a new operation may not reuse the old authority.

### Epoch, idempotency, and current binding

Every preview service instance creates a new high-entropy epoch and retains no
state from another instance. Expected epoch is checked before authority,
idempotency, selection, lookup, allocation, or mutation:

- stale current or snapshot evidence returns `session_lost`;
- stale create or close evidence returns `outcome_unknown`;
- no stale request is rebound, redispatched, or used to allocate.

At most one active current session exists for a stable `(actor, authority
class)` pair. Current lookup never creates. A distinct create while an active
current session exists returns `current_session_exists`. After a terminal
tombstone is observed, a deliberate create can allocate only with a new grant
and a new idempotency key.

Create and close idempotency is scoped to epoch, actor, authority class,
operation family, and key. The ledger compares a canonical semantic-body
digest. An exact replay returns the one recorded result; another body under the
same scope returns `idempotency_conflict`. Epoch validation precedes replay, but
within the current epoch an exact replay precedes consumed or revoked authority
checks. Concurrent operations serialize at the lifecycle boundary so duplicate
requests cannot duplicate allocation or cleanup. Recorded conflicts remain
bound to the related session through its active lifetime and retained
tombstone, so cleanup cannot permit premature key reuse.

Decision 0052 refines the claim boundary: trusted scope and canonical-body
validation precede claim; deterministic resource-authority and business
rejections after that boundary are recorded; transient pre-work capacity is
explicitly unclaimed. It also defines bounded failed-operation retention.

### Lifecycle, expiry, and tombstones

A created session begins active at control revision 1. Close, absolute expiry,
or idle expiry terminalizes it exactly once, advances control revision, revokes
session authority, releases its isolated mutable revision, and retains only a
bounded safe tombstone. Launch limits may shorten but never extend configured
service TTLs. Lazy cleanup records the earliest deadline and its actual
projected timestamp even when the first later operation observes multiple
elapsed deadlines; observation delay cannot extend tombstone or idempotency
retention. Passive current and snapshot reads do not extend idle expiry. Expiry
and retention cleanup are lazy at operation boundaries, so the preview has no
background timer or idle CPU cost.

The Phase 1 snapshot is deliberately lifecycle-only. It contains safe immutable
starting identity and digest, authority class, lifecycle state and terminal
reason when applicable, control revision, timestamps and expiry, safe limits,
and available lifecycle controls. Session authority is returned beside an
active snapshot, never inside it. Terminal current lookup returns no session
authority. Snapshots contain no world state, profile name or path, prompt,
environment, command slot, accepted-state revision, command result, transcript,
event cursor, X-ray data, provider data, trace, or structured failure.

### Unfrozen integration contract

`contracts/product-api/v0.2.0-rc.2/` is the current exact release candidate. It
leaves `v0.1.0-rc.1` and the published lifecycle `v0.2.0-rc.1` unchanged,
advertises no server, keeps the exact five capabilities disabled with no
enabled revisions, and marks every session operation
`integration-preview-only`. Its operation set is limited to:

```text
GET  /product/v1/capabilities
POST /product/v1/sessions
GET  /product/v1/sessions/current
GET  /product/v1/sessions/{reference}
POST /product/v1/sessions/{reference}/close
```

There is no command, cancellation, event, transcript, reset, switch, Stage,
Forge, attendee-admission, or X-ray route. A Phase 1 mutation promises no event.
The promised-but-withheld-event recovery scenario remains with Phase 2, the
first phase that implements both mutation acknowledgement and event transport.

The RC remains unfrozen until the UI tracked-document acknowledgement and an
explicit coordinated schema freeze land. Preview integration evidence does not
satisfy a live capability gate, and a BFF must not call these routes as a
production fallback while `product_session_core` is disabled.

Within this unfrozen exact RC, the validator keeps a non-self-derived canonical
OpenAPI semantic digest. Contract refresh may update artifact and fixture
digests, but it cannot bless semantic drift until validator, adapter/client
assumptions, fixtures, tests, and the semantic pin are deliberately updated in
one reviewed change.

## Alternatives Considered

Composing the partial lifecycle behind `[product_api] enabled` was rejected
because it would turn an incomplete shared core into a live-looking service and
would add runtime state when the UI is unused. Reusing admin, SSH, profile, or
enrichment registries was rejected because their authority and lifecycle are
different and would couple browser integration to the established DREAMLAND
experience.

Adding a grant-resolution route was deferred because Phase 1 needs only a
deterministic server-side integration grant. Accepting a browser selection or
offering ID directly in create was rejected because display identity cannot be
runtime authority. Empty command, transcript, or event structures were rejected
because they would imply that the shared core exists before Phase 2.

## Consequences

The UI team receives a stateful backend-owned lifecycle target for generated
client and recovery integration while every production capability remains
truthfully disabled. The preview can test response loss, replay, authority
isolation, expiry, tombstones, and restart loss without model calls or provider
cost. The ordinary DREAMLAND experience retains the same startup, session,
capacity, and latency behavior.

Phase 2 may reuse the protocol-neutral lifecycle concepts, but it must still
complete and test the command slot, state publication fence, cancellation,
event/transcript recovery, replacement, enrichment coordination, and isolated
capacity gates before changing composition or enabling
`product_session_core`.

## Maintenance

Update this decision together with `docs/PRODUCT_SESSION_API.md`,
`docs/ARCHITECTURE.md`, the Phase 1 artifact, lifecycle implementation, preview
tool, and owning tests if preview composition, authority, epoch, idempotency,
retention, snapshot privacy, operation availability, or capability truth
changes. Supersede the preview-only composition section explicitly when a later
complete capability is allowed into the normal product composer.
