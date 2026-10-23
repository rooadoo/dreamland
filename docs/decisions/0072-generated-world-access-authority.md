# Generated-World Attendee Access Requires Separate Reveal Authority

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the Phase 8A promotion, reveal, catalog-eligibility, and
attendee launch-authority boundary without changing published Product behavior.

## Overview

An authoritative World Forge selection already supports private Presenter
Stage launch. It is not sufficient evidence that the same generated world may
be shown to or launched by an attendee. The accepted backend/UI agreement
requires a separate progression from selected immutable revision, through a
promotion/access record and generic catalog reveal, to a narrow attendee launch
selection.

Published Product RC3 attendee operations are authored-only. Product RC4 is an
identity-only live sibling with the same semantics. Adding generated eligibility
directly to either artifact would make a fresh Forge selection attendee
authority, break strict clients, and collapse Operator publication authority
into attendee selection authority.

## Decision

Implement Phase 8A as a separately versioned, explicitly constructed World
Access boundary. Define its corrected provider-free contract under
`contracts/world-access-api/v0.1.0-rc.2/`, with exact dependencies on Product
API `v0.7.0-rc.3` and World Forge API `v0.1.0-rc.4`. Keep both dependency trees
immutable, and preserve the reviewed RC1 tree byte-for-byte. The World Access
artifact has its own identity, six private BFF
operations, fixtures, manifest, and server-only generated client; it introduces
no event stream or Product capability identifier.

RC3 later corrects the server-only exchange recovery evidence while preserving
published RC2 byte-for-byte. It carries Product's already-computed launch-
binding digest as non-authorizing output, as specified by decision 0073; it
does not change the six operations or either pinned dependency.

Use two distinct authority classes:

- a trusted, epoch-bound Operator actor may promote a server-resolved selected
  Forge job and explicitly reveal its access record; and
- a leased, epoch-bound attendee actor may read the bounded catalog and
  exchange exact catalog and notice evidence for an ordinary Product launch
  grant.

Declare the required class on every OpenAPI operation. Identity is
`x-dreamland-actor-class: none` and remains bearer-authenticated without an
actor-context header; promotion, status, and reveal are `operator`; catalog and
exchange are `attendee`. Treat wrong-role evidence as HTTP authorization
scenarios rather than invalid JSON bodies.

Promotion accepts a Forge job reference rather than revision identity, digest,
path, content, or Forge authority. The trusted source resolves the current
selected record and exact immutable artifact in process. DREAMLAND revalidates
the selected-record policy, actor, origin, revision/digest relationship,
canonical bytes, and profile before it creates hidden access truth. The hidden
record is process-local, immutable by revision, bounded by dual-clock expiry,
and bound to one service epoch.

Reveal requires the exact current hidden access-record ID. It creates the
revealed successor only after a trusted catalog callback has reserved and
registered the exact generated offering. Publication-capacity failure is
retryable and leaves the reveal idempotency key unclaimed; it must not commit a
revealed record that the catalog cannot return. Exact reveal replay returns the
recorded result without republishing. Changed-record compare-and-set failure is
deterministic and claimed.

Treat catalog preparation as a reversible trusted transaction until the access
aggregate passes its post-publication cutoff and monotonic-clock checks. On
failure, remove the pending binding and only the exact offering, opaque-token
reservation, and otherwise-unreferenced revision installed by that attempt.
Never roll back pre-existing registry state or an offering that issued a grant.

Make the generated catalog coordinator the only path from revealed access
truth into attendee eligibility. It may combine authored offerings with active
revealed generated bindings, but it omits any bare Forge-origin registry entry.
Its transport projection contains only opaque offering and offering-revision
locators, closed origin, display-safe labels, notice version, expiry, and an
opaque catalog position. It never projects internal selection IDs, promotion or
access records, revision/digest coordinates, profile paths, raw state, or
authority.

Require exact offering revision and notice evidence on exchange. The trusted
coordinator mints an internal HMAC eligibility proof binding service epoch,
offering, access record, closed origin, immutable revision and digest, and
expiry. The attendee facade verifies the proof before provisioning a generated
revision. The proof is not a DTO or reusable external authority. Exchange
returns the existing Product-compatible launch grant, and unchanged Product
session creation remains the only operation that consumes it.

Keep generated provisioning constructor-disabled in both the attendee facade
and core. Preserve the existing authored-only Product option and exchange
methods behaviorally unchanged. Add a distinct catalog-offering exchange and
idempotency family so explicit World Access composition cannot reinterpret an
RC3/RC4 attendee request. Capacity checks must ensure the registry can hold any
revision and offering that the generated attendee core can provision.

Use the aggregate lock order access/catalog, attendee facade, launch registry,
then session core. Do not resolve access truth while holding registry or core
locks. The catalog coordinator acquires access and then catalog across the
reentrant reveal and pending-to-active move; status and catalog readers therefore
linearize before both changes or after both changes. Catalog reads snapshot
active revealed bindings before reading the registry; a pending publication is
never projected until reveal commits.

Keep the published authored-attendee options snapshot and its private
authored-origin filter under one registry lock. Reversible cleanup of a pending
generated offering must wait rather than invalidate a legacy Product response
between projection and filtering.

Provide a provider-free loopback preview with deterministic source and executor
inputs. Its selected-artifact source is an explicit fixture adapter, not a
claim that the provider-free Forge preview minted a live selected record. The
executable lifecycle clock reports evaluator-current time; exact deterministic
clock injection remains available to tests. The identity reports no provider
calls, no live Forge evidence, no live capability evidence, and no enabled
capabilities. A separately configured Phase 8B composition may use the live
World Forge source adapter, but it must preserve the same promotion, reveal,
catalog, proof, and Product-grant semantics.

Make the RC2 contract enforce state relationships structurally: hidden
promotion truth is never catalog eligible, revealed truth is always catalog
eligible, delayed retry always carries a bounded positive delay, and other
retry categories carry null. Make publication fail closed unless the exact
six-operation surface, security, parameters, actor metadata, request/response
references, and media types are canonical. The generated client must accept
JSON and problem JSON, validate status-appropriate content type before decode,
compile with backend-owned development tooling, and exercise all six methods
through a bounded transport before publication.

Importing any Phase 8A module starts no listener, worker, timer, provider,
reservation, or ordinary runtime composition. Normal DREAMLAND startup remains
unchanged and does not pay for World Access state or bookkeeping when the API
is unused.

## Consequences

The UI BFF can integrate a complete hidden-to-revealed generated-world attendee
flow without treating Forge or Presenter coordinates as attendee authority. It
can use one Product launch grant and the existing Product session surface after
World Access exchange, so generated sessions do not require a parallel runtime
API.

Publication is deliberately stricter than ordinary eventual catalog refresh:
the access record cannot become revealed unless its offering is first
reservable. This prevents a successful reveal response followed by an absent
catalog item, at the cost of making catalog capacity part of the synchronous
reveal transaction.

Process restart loses access records, catalog bindings, idempotency state, and
issued authority by rotating the epoch. This is truthful integration-preview
behavior, not durable publication. Catalog administration, scheduled reveal,
live provider composition, deployment isolation, canary and rollback control,
conference load, and presentation rehearsal remain later gates.

Phase 8A does not enable `public_attendee_sessions`, alter Product RC3/RC4,
authorize direct browser access, or establish production generated-world
attendee availability. The UI continues to own same-origin browser sessions,
CSRF, Host/Origin enforcement, throttling, browser DTOs, notice presentation,
and server-side retention of all private DREAMLAND authority.

## Evidence

Provider-free owning tests cover inert construction, selected-record and
artifact verification, hidden promotion, deterministic replay/conflict,
transient source and capacity recovery, reveal compare-and-set, publication
ordering and exact rollback after post-registration cutoff or clock failure,
response-time failure retention, expiry and epoch rotation, concurrent mutation,
status/catalog linearization across reveal activation, catalog omission before
reveal, omission and denial of a bare generated registry entry, mixed
authored/generated catalog projection, exact offering-revision and notice
binding, internal eligibility proof verification, Product-compatible grant
exchange, unchanged session launch, pending-rollback isolation for the legacy
authored-options read, and preservation of the authored-only Product endpoints.

Contract tests validate deterministic artifact generation, the exact six-route
surface, dependency pins, correlated lifecycle/retry schemas, cross-role HTTP
denial, generic authored/generated fixtures, generated-client compilation and
transport behavior, and
immutability of the published Product artifacts. The loopback preview exercises
the same hidden-to-reveal-to-catalog-to-grant-to-session path without provider
work, including exact World header grammar, complete one-request responses, and
loopback-only client configuration. All production capability claims remain
disabled.

## Maintenance

Update this record with `src/dreamland/product/generated_access.py`,
`src/dreamland/product/attendee_catalog.py`, the generated opt-in seams in
`src/dreamland/product/attendee.py` and
`src/dreamland/product/launch_selection.py`,
`tools/product_world_access_contract.py`,
`tools/product_world_access_preview.py`, the immutable World Access RC1 review
artifact, published RC2 artifact, and corrected RC3 artifact,
`docs/PRODUCT_WORLD_ACCESS_DEVELOPMENT.md`, `docs/PRODUCT_SESSION_API.md`,
`docs/ARCHITECTURE.md`, and `ROADMAP.md` whenever access authority, publication
ordering, catalog eligibility, notice binding, launch correlation, contract
dependencies, preview identity, or normal-runtime isolation changes.
