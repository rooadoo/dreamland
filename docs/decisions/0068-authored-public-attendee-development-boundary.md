# Authored Public-Attendee Development Boundary

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the first authored-world attendee authority, isolation,
admission, recovery, and transport boundary without enabling a public product
capability.

## Overview

Phase 7A and Phase 7B provide private Presenter launch selection for immutable
authored and authoritative Forge revisions. They deliberately do not authorize
the UI-owned `/session` experience. Presenter authority, Stage references, and
fresh Forge selections cannot be repurposed as attendee authority.

Phase 7C adds the first provider-free attendee integration checkpoint. A
trusted UI BFF can bootstrap a bounded gateway context, list only authored
attendee options, exchange an opaque selection with notice-version evidence,
and use the existing private product-session operations through an isolated
attendee actor. This checkpoint remains explicitly started, loopback-only,
capability-disabled, and absent from normal DREAMLAND composition.

## Decision

Add an attendee-only facade over the launch-selection registry and shared
product-session core. Trusted composition pre-seeds the exact same finite pool
of `ATTENDEE` actor authorities into both components. A private BFF authority
may lease one slot to one high-entropy `gateway_context_id`; DREAMLAND stores
only its digest. A slot is never assigned to another gateway context in the
same service epoch. This prevents a later browser from inheriting an old
current-session binding, idempotency scope, authority, or tombstone.

The returned `actor_context` remains BFF-held. It is not a browser credential,
cookie, public bearer, profile locator, or general product capability. The UI
BFF continues to own same-origin browser sessions, CSRF, Host/Origin checks,
public throttling, notice presentation, and browser-safe DTOs. The private
DREAMLAND bearer authorizes the BFF transport; the narrower actor context
authorizes only its own attendee resources.

Require both the actor role and launch origin at every attendee launch seam.
Only `ATTENDEE` offerings backed by `authored_profile_snapshot` revisions are
listed or provisioned. A late Forge-origin attendee offering is omitted from
listing and uniformly unavailable to exchange. Phase 8 must separately create
promotion, publication, catalog, eligibility, and canary truth before a
generated revision can become attendee-accessible.

Bind every context, grant, session, idempotency record, and recovery coordinate
to one high-entropy service epoch. Missing epoch evidence is a request error;
stale mutation evidence is `outcome_unknown`; stale reads report session loss.
Epoch and private BFF authority checks precede diagnostics for attacker-
controlled bootstrap fields.

Bootstrap claims its idempotency key after current-epoch private authority,
key, and canonical body validation but before context lookup or terminal pool
capacity. Exact success or deterministic rejection replays; a changed body
conflicts. Exhaustion of the bounded idempotency ledger is transient,
unclaimed, and retryable after delay. An exhausted epoch-local actor pool is a
claimed terminal result because a burned slot cannot safely become reusable.

Attendee selection binds the outer claim to both the opaque selection and the
presented notice version. It derives a private inner registry key from that
complete semantic body and the caller key, so different retention boundaries
cannot alias a selection-only registry replay across notice changes.
Deterministic notice, origin, selection, admission-window, and authority
outcomes replay; only explicitly retryable resource capacity leaves the claim
reusable.

Clamp the leased actor, issued grant, and resulting session to both the UTC
expiry and exact monotonic deadline of the gateway lease. Either clock reaching
its boundary denies new work, terminalizes active session truth, fences late
publication, and releases admission once. Explicit lazy cleanup coordinates
the facade, registry, and core bounded stores; no timer or worker is started.

Place every attendee session and command in the shared ledger's `STANDARD`
partition. Attendee traffic cannot consume the configured
`PRESENTER_STAGE` reservation. Session, job, worker, token, spend, command,
output, TTL, grant, actor, context, idempotency, journal, and tombstone limits
remain finite.

Publish the additive private contract as `v0.7.0-rc.3`. Preserve the 21 RC2
operations and add only private context bootstrap, authored attendee option
listing, and attendee selection exchange. Existing session operations are
reused with the leased attendee actor and returned launch/session authority.
All product capabilities remain disabled and the deterministic preview is not
production or live-provider evidence.

Importing the attendee module or preview starts nothing. Normal DREAMLAND SSH,
admin, MCP, profile, command, and EnrichmentAgent paths import none of the
attendee composition and incur no listener, worker, timer, provider,
reservation, registry, journal, or bookkeeping cost.

## Consequences

The UI team can integrate the complete authored-world `/session` BFF sequence
against a deterministic backend before provider-backed attendee composition is
introduced. Different gateway contexts begin from the same immutable authored
revision but receive distinct actor, grant, session, mutable state,
idempotency, transcript, and event scopes.

The bounded actor pool intentionally trades in-epoch context reuse for strong
recovery isolation. Capacity planning must therefore size actor and
idempotency pools for the deployment's maximum issued gateway contexts, not
only its simultaneously active sessions. Restart creates a new epoch and
invalidates every old context and capability rather than rebasing them.

This checkpoint does not enable `public_attendee_sessions`, expose DREAMLAND
directly to the internet, dispatch a live model, establish deployment-wide
provider isolation, prove conference load, or authorize a Forge revision for
attendees. Those remain independent capability and deployment gates.

## Evidence

Provider-free owning tests cover inert import, private bootstrap and request
precedence, deterministic claim/replay/conflict behavior, concurrent bootstrap,
authored-only projections, notice binding across nested ledgers, cross-context
grant and session denial, isolated mutable state, standard-capacity saturation,
dual-clock authority and session expiry, coordinated cleanup, dynamic-grant
retention, and late Forge rejection. Adjacent admission, lifecycle, core,
selection, and Presenter-selection suites remain compatibility gates.

The published RC3 manifest records implementation commit
`01bffa2d878101c9f7e629195772c20c6768a23d`, created after the implementation,
preview, and repository suites passed.

## Maintenance

Update this record together with `src/dreamland/product/attendee.py`,
`tools/product_attendee_preview.py`, `tools/product_attendee_contract.py`, their
tests and published contract, `docs/PRODUCT_SESSION_API.md`,
`docs/ARCHITECTURE.md`, `docs/TESTING.md`, and `ROADMAP.md` when changing
gateway authority, context leasing, authored eligibility, notice evidence,
idempotency, expiry, admission partitioning, transport ownership, or the Phase
8 boundary.
