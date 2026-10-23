# World Forge Foundation

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, and coding agents
Purpose: record why Phase 4 begins as an isolated typed seed and lifecycle boundary.

## Context

The accepted product sequence requires a separately versioned live World Forge
before Forge-to-Stage can be enabled. The UI already has a removable fixture
adapter and an agreed eight-field BSides recipe, but DREAMLAND had no canonical
typed seed, job lifecycle, or idempotency owner. Attaching early Forge work to
the SSH runtime or existing Stage API would impose idle cost and blur the
capability boundary.

## Decision

Begin Phase 4 with `dreamland.product.forge`, an inert protocol-neutral module.
Adopt the exact recipe field IDs, kinds, order, and limits already used at the
UI adapter boundary. Preserve accepted values byte-for-byte, reproduce the
rendered sentence deterministically, bind the confirmed document to a canonical
hash, and reject invalid or obviously sensitive input before provider work.

Add an explicitly constructed bounded in-memory lifecycle foundation. Bind
idempotency to authenticated actor, operation family, service epoch, key, and
semantic body. Replay exact accepted and deterministic rejected results and
reject changed-body reuse. Leave pre-authentication rejection outside the
authenticated scope. Expose monotonic provider-free fixture transitions and
cursor recovery without returning Stage launch authority.

Do not import or compose this boundary from normal startup. Do not change the
global state profile, ordinary SSH/session paths, Stage contract, or any
capability projection. Publish a separately versioned Forge HTTP contract only
after its loopback operations and fixtures exist.

## Consequences

Backend validation and lifecycle behavior can be tested before live provider
and durability work. The UI contract can be built on a stable canonical seed
instead of treating browser fixtures as backend truth. Ordinary DREAMLAND has
no new listener, thread, provider call, bookkeeping, or state mutation.

This checkpoint is not live Forge evidence. In-memory selected states remain
fixtures; durability, redundant attempts, independent validators, immutable
profile storage, receipts, shared hosted-budget enforcement, and the private
HTTP artifact remain required before Phase 4 is complete.

Decision 0057 subsequently supplies the private provider-free HTTP artifact and
replaces direct confirmation with prepare-before-confirm semantics. It does not
change this foundation's inert runtime boundary or make Forge live.

## Maintenance

The implementation and owning-layer tests are recorded in commit
`7511371ef05a699ac781f5aec460b7e6ae3fbebd`.

Update this decision before changing recipe identity/order/limits, exact-value
preservation, canonical hashing, authenticated idempotency scope, lifecycle
states, or the inert composition boundary. Add the implementation commit after
the foundation is committed.
