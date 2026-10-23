# Source-Neutral Private Presenter Launch Selection

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the Phase 7A authored/Forge launch boundary, authority model,
idempotency behavior, and separation from future public attendee delivery.

## Overview

The private Presenter Stage can already launch an authoritative Forge selection,
but the Forge-specific handoff cannot launch an operator-authored saved world
without accepting a profile name or path or pretending that the authored world
has Forge provenance. The UI needs one safe selection surface for both sources
while preserving the existing terminal/X-ray flow and the normal DREAMLAND
runtime's zero product cost.

Phase 7A is a narrow UI-integration checkpoint. It does not implement the
live public-attendee backend needed by the UI-owned `/session` route,
generated-world promotion or publication, an attendee catalog, or any
production capability gate.

## Decision

Introduce a bounded, source-neutral private Presenter launch registry with a
closed origin enumeration:

- `authored_profile_snapshot`; and
- `world_forge_selection`.

Trusted composition may register only an immutable revision and a bounded safe
offering that agree on identity, digest, origin, role, operations, policy,
notice, expiry, and display metadata. Keep the origin enumeration closed so an
unknown future source fails validation instead of silently inheriting Presenter
or attendee authority.

Expose two additive private operations in `v0.7.0-rc.1`: Presenter option
listing and selection exchange. The safe option projection may cross the BFF's
browser boundary and contains only `offering_id`, opaque
`launch_selection_id`, bounded display metadata, closed origin, immutable
revision identity/digest, and optional expiry. Neither identifier authorizes a
session. Listing and exchange require the private Product service bearer,
current service epoch, and BFF-held Presenter actor context. Only exchange may
mint an actor-bound, role-bound, operation-scoped, expiring `launch_grant`, and
that credential remains on the BFF side for existing private Stage operations.

Do not accept an offering ID as an exchange request and do not accept profile
names, profile paths, raw state, revision coordinates, Forge service authority,
session capabilities, or caller-created grants. Those values either lack
authority or would cross a local configuration/artifact boundary.

Snapshot authored worlds only through trusted startup configuration. The
explicitly constructed startup caller supplies a local world-profile path and
an exact digest pin. Read the configured source once with no symlink following
and hard byte and JSON bounds, validate it as a launchable world profile,
reduce it to a canonical envelope
containing only world level and state, and verify the expected digest before
artifact-store mutation. Store the result by content digest in owner-only,
create-only storage by fsyncing an owner-only temporary inode and atomically
hard-linking it to the previously absent digest name; verify it again on reopen.
Profile name, source path, timestamps, comments, source-session metadata, and a
mutable profile-store handle are not retained in the revision. Allocate
detached runtime state from the immutable artifact.

On exchange, use an inert source-neutral coordinator to resolve the selected
immutable revision entirely server-side and provision it into a dynamic-only
`PresenterStageService` before publishing the grant or idempotent result.
Validate origin-specific envelope kind, exact revision/digest identity,
Presenter actor binding, display identity, operation scope, and expiry before
atomic registration. A grant must allow create and may also allow explicit
switch. Reuse the existing Stage create and expected-control-CAS replacement
semantics; do not add an implicit replacement path. Keep the existing
Forge-specific handoff and its richer source correlation unchanged.

Fail coordinator construction unless registry Presenter actor credentials,
actor identities and expiries, allowed operation families, and maximum revision
bytes are compatible with the adjacent Stage. Apply both the core-configured
and lifecycle-hard revision ceilings. This covers future runtime registration,
not only options present at startup.

Cap the registry at the contract's 128-option maximum and require all offerings
for one immutable revision to share identical Stage display metadata. This
makes every published option provisionable through Stage's one-display-per-
revision model and rejects trusted configuration conflicts atomically.

Treat dynamic grant capacity as active, not lifetime, capacity. Before adjacent
provisioning, expire lifecycle state and remove expired dynamic grants and their
selected-record bindings. Retain revision, display, and Forge-source projection
data through dependent active sessions and recovery tombstones, then reclaim it.
Derive every source-neutral final grant from the service epoch, a positive
monotonic 64-bit serial, and private candidate entropy. Burn the serial once a
candidate is formed, even when adjacent provisioning fails. The registry keeps
only bounded active grants and counts an in-flight provisioning callback against
that capacity, including callback reentrancy. Enforce absolute authority expiry
against both the current wall clock and monotonic deadlines, and derive dynamic
Stage deadlines from private exact registry deadline evidence on one shared
monotonic clock, capped by the grant's remaining UTC lifetime at provisioning.
Wall-clock jumps therefore cannot extend Stage authority beyond the registry
deadline. Stage releases an expired source-neutral digest but retains
its serial high-water mark and rejects every old serial. Preserve the existing
Forge-specific seam's legacy digest reservation unchanged.

Use operation family `stage.launch_selection.exchange`. Scope idempotency by
service epoch, actor/role, key, and canonical selection body. Claim the key only
after bearer, current epoch, Presenter actor, key, and body validation, but
before resolving the selection. Retain and replay deterministic
`launch_selection_unavailable`; reject changed-body key reuse with
`idempotency_conflict`. Preserve the recorded logical error on replay while
allowing each HTTP attempt a fresh transport correlation ID. Leave the key
unclaimed when adjacent provisioning or registry capacity returns the retryable
`launch_selection_capacity_exhausted`. An old epoch yields `outcome_unknown`
without reminting. Exact committed replay returns the original grant, including
when the grant and offering are now expired, using the option projection stored
with the original logical outcome rather than current listing state. The BFF
therefore reconciles instead of silently redispatching. A retryable adjacent-
capacity rejection retains no unpublished grant digest but burns its formed
serial, so a later retry receives a distinct final credential and no expired or
unpublished credential can regain authority.

Publish Phase 7A as an additive, provider-free development contract and
loopback preview. Preserve published `v0.6.0-rc.3` byte-for-byte and retain all
existing Forge handoff routes and semantics. Report every product capability
disabled. Normal DREAMLAND startup must not import Phase 7A modules or create a
registry, artifact importer, listener, worker, timer, provider, reservation,
session, or bookkeeping path; ordinary SSH and provider cost remain unchanged.

## Consequences

The provider-free UI checkpoint can present authored and synthetic
Forge-origin immutable revisions through one private Presenter selection flow
and open either in the existing terminal/X-ray experience. The domain boundary
accepts only trusted, origin-valid immutable inputs, so a later explicit live
composition can register an imported authored snapshot or authoritative Forge
selection without exposing or guessing a profile filename and without
falsifying Forge provenance.

Phase 7A itself does not provide that provider-backed combined composition.
Phase 7B implements it as a separate explicitly started target under
`v0.7.0-rc.2`, joining the configured authored importer, authoritative Forge
registration, shared display metadata, and kind-dispatching runtime codec.
RC1 remains the provider-free integration identity, and the original live
authored Stage and Forge-to-Stage tools remain available unchanged. See
decision 0067 for the live composition and its additional evidence boundary.

`launch_selection_id` can safely be a browser-held navigation/exchange locator
because it is insufficient without private bearer, epoch, and actor evidence.
The resulting launch grant remains sensitive BFF authority. Compromise or
guessing of an offering or selection ID alone cannot authorize Stage.

This checkpoint deliberately does not make a freshly selected Forge revision
attendee-eligible. Later Phase 7 work must still provide public attendee
admission, isolation, quota, expiry, lifecycle, and projections before enabling
`public_attendee_sessions`. Phase 8 must separately establish generated-world
promotion/access truth, catalog publication, attendee eligibility, canaries,
and attendee selection. Neither Phase 7A origin nor Presenter offering identity
may substitute for that evidence.

## Alternatives Considered

Extending the Forge handoff to accept authored worlds was rejected because it
would create false Forge provenance and couple generic selection to Forge
epochs and selected-record shapes. Accepting profile names or paths over HTTP
was rejected because it would turn untrusted input into filesystem selection
and expose mutable operator configuration. Letting the UI send revision IDs,
digests, or raw documents was rejected because integrity coordinates are not
authority. Treating `offering_id` or `launch_selection_id` as bearer authority
was rejected because browser-safe navigation state and private Stage authority
must remain separate. Reusing a mutable profile-store entry at launch time was
rejected because the same selection could then resolve to different state.

## Evidence

Owning tests cover strict authored import, digest-before-mutation behavior,
owner-only atomic create-only artifact storage, concurrent identical import,
interrupted-writer residue, canonical reopen checks, detached
allocation, both closed origins, bounded and concurrent registration/exchange,
safe projections, actor/role/epoch/grant isolation, expiry, exact idempotent
success and deterministic rejection replay, changed-body conflict, unclaimed
capacity with a burned candidate serial and distinct retry,
post-offering-expiry replay, the exact 128-option cap, per-revision display
consistency, dynamic grant and projection cleanup after tombstone retention,
and dynamic Stage create/switch provisioning.
Contract and
generated-client tests cover the two exact private routes, strict headers and
request body, safe errors, mixed and empty option fixtures, origin and identity
relationships, forbidden source/authority fields, response-loss recovery,
forward-compatible top-level additions, and byte-exact preservation of
published RC3. The provider-free loopback preview exercises the UI-facing
sequence, including a selected-world terminal command, without constructing a
provider. It does not constitute a provider-backed authored/Forge composition.

The published RC1 manifest records tested implementation commit
`9679470cfb830bc45f4de128f8233f8c625d4ee5`.

## Maintenance

Update this decision, `docs/ARCHITECTURE.md`, `docs/PRODUCT_SESSION_API.md`,
`docs/TESTING.md`, `ROADMAP.md`, the Phase 7A contract and preview, and owning
tests together when changing origins, authored snapshot identity/storage,
option projection, BFF/browser authority ownership, grant scope, exchange
idempotency, Stage provisioning, capability status, or the Phase 7/Phase 8
boundary.
