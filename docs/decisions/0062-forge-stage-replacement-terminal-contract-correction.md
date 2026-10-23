# Forge-to-Stage Replacement Terminal Contract Correction

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and coding agents
Purpose: record the Phase 5.1 correction required before explicit Forge-to-Stage replacement or Phase 6 Stage X-ray integration proceeds.

## Context

Phase 5 published `dreamland-private-product-api` `v0.5.0-rc.1` as the
capability-disabled development contract for authoritative Forge launch,
identical-selection resume, and explicit expected-control-CAS replacement.
Strict UI integration found that the canonical replacement response does not
represent the old session's actual post-cutover state. Its
`previous_snapshot.lifecycle` says `replaced`, but the same snapshot retains
active controls, an available command slot, and non-tombstone recovery.

The contradiction is in the contract fixture construction and validation
boundary, not in the intended replacement transition. The owning lifecycle and
core services revoke the old authority, advance its control revision, append
`session.terminal`, expose a terminal command slot, and retain a bounded
tombstone. Stage preserves the old Forge source while the new active snapshot
identifies the replacement selection. Phase 5.1 owning tests now assert that
complete transition and its replay behavior.

Initial Forge launch, exact replay and resume, dynamic Stage, refresh, and
ordinary terminal commands remain useful RC1 integration checkpoints. A
fail-closed UI cannot expose explicit replacement from RC1 because its strict
decoder cannot safely interpret the contradictory old snapshot.

## Decision

Insert a Phase 5.1 corrective closeout before Phase 6. Preserve every published
`v0.5.0-rc.1` byte and publish the correction as `v0.5.0-rc.2`; do not silently
regenerate RC1. RC2 retains the same route surface, disabled capability claims,
dynamic-only Stage behavior, and exact dependency on World Forge
`v0.1.0-rc.4`. It adds no X-ray projection and makes no product capability
live. A byte-exact tree-digest regression protects RC1; the current generator,
validator, generated client, and combined development target are repinned to
RC2 rather than treating RC1 as a second active root.

The corrective generator builds the canonical `previous_snapshot` from the
same terminal projection and retention policy as the runtime, rather than
changing only the lifecycle fields of an active snapshot. A terminal Stage
snapshot has:

- an empty `available_controls` array;
- `command_slot.state = terminal` and no active command ID;
- tombstone recovery with a non-null retention time later than
  `lifecycle.terminal_at_ms`;
- an old-session control revision exactly one greater than the replacement
  request's accepted control CAS;
- an event position after the authoritative `session.terminal` append; and
- the old session's original selected record, Forge job and reference,
  immutable digest, starting revision, session ID, and non-authorizing
  reference.

For the deterministic canonical transition, the request CAS is `1`, the old
terminal control revision is `2`, `terminal_at_ms` is `1777000001000`, bounded
`retain_until_ms` is `1777000601000`, and the old event sequence advances from
`1` to `2`. The new active session begins with its own independent control
revision `1`; its coordinates are not inherited from the tombstone.

The new active snapshot continues to identify the replacement selection and
must agree with the top-level Forge selection. Ordinary handoff of a different
selection remains non-replacing and returns `current_session_exists`; only the
separate operation with current session authority and exact expected control
revision may cut over.

RC2 uses schema conditionals and generated-client validation for value-local
terminal relationships. Operation-aware validation and owning runtime tests
cover request-relative CAS, event-history advancement, old-authority
revocation, and old/new source identity. A response-only schema cannot prove a
historical event transition. Client smoke mutates each locally decidable
invariant and rejects the contradictory forms that RC1 accepted.

Phase 5.1 acceptance requires the owning lifecycle/core/Stage replacement
tests, Forge-to-Stage coordinator tests, combined loopback HTTP tests, exact
RC2 contract validation, and generated-client smoke to pass. The replacement
flow must also prove exact same-key/body replay returns the same logical
cutover without allocating another Stage session. These checks use injected
provider-free collaborators. They require no Forge generation or Stage command
provider call.

Return the backend implementation commit, RC2 manifest and OpenAPI digests,
and exact validation and client-smoke results to the UI team. The UI may then
repin RC2, regenerate its server-only client, restore the canonical replacement
fixture to strict decoder tests, and implement a separate
presenter-confirmed replacement action. Explicit replacement remains
unavailable in the UI until that handoff is complete.

Normal DREAMLAND startup remains unchanged. It does not import product
composition, construct Forge or Stage services, open listeners, allocate
workers, reserve provider capacity, or incur provider cost. The correction is
limited to explicitly constructed protocol-neutral services, contract
generation and validation, generated clients, tests, and the manually invoked
development target.

## Consequences

Phase 5.1 implements the closeout of a Phase 5 publication defect instead of
treating it as new Phase 6 functionality. The UI gate closes only after RC2
publication and handoff. Phase 6 may be designed in parallel, but its Stage
snapshot baseline and published integration artifact must descend from the
corrected RC2 contract. `stage_xray` remains disabled and snapshots continue to
report X-ray unavailable until Phase 6 separately proves its allowlist,
privacy, consistency, concurrency, and recovery requirements.

The correction does not add durable Forge jobs, hard provider-call
termination, A/B/C attempts, repair, fixed multi-attempt selection, shared
Forge/Stage provider-budget enforcement, a production private gateway, public
attendee authority, catalog or promotion flows, deployment/load/restart proof,
or capability enablement. It does not change selected-record authority,
artifact/hash identity, session lifetime, BFF credential ownership, normal
runtime isolation, or the provision-before-cutover replacement fence.

Decision 0061 remains the historical RC1 handoff decision and implementation
binding. This decision records only the corrective successor and does not
rewrite that published history.

Publication record: `v0.5.0-rc.2` was published at
`2026-08-26T21:29:18Z` against tested implementation commit
`3d1e14e750f1975bd77aa6316c1fc3af568864a4`. Its manifest SHA-256 is
`7b7f75ca606cc21c281032363229bfc7023cb950f3527731bc44b3b248b74f7f`,
its OpenAPI SHA-256 is
`d9973f6d568cceb1a06bf351ec6a9ba4987f93a1f85477f22ccba9d17019fcae`,
its generated-client SHA-256 is
`706b93de26c8cf91a24f465398ef71d8d93fc368a2cc56de02f73b29835f14e2`,
and its fixture-index SHA-256 is
`98f32b5726141eb271ab446a63f49e06a9160c35d0c0ea0825935d06d7ca9d27`.

## Maintenance

Update this decision if the RC2 publication binding changes, or before changing
terminal snapshot invariants,
replacement CAS/event semantics, old/new Forge identity preservation, the UI
replacement gate, normal-runtime isolation, provider-free acceptance, the
immutable RC1 boundary, or the Phase 6 X-ray exclusion.
