# Provider-Backed Source-Neutral Presenter Launch

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the Phase 7B live authored/Forge composition, provenance
dispatch, registration boundary, and continued separation from public attendee
delivery.

## Overview

Phase 7A established a provider-free private Presenter selection boundary for
authored and Forge-origin immutable revisions. It deliberately left the live
authored Stage and live Forge-to-Stage targets separate because they used
different runtime document codecs and did not share one generic option
registry.

Phase 7B adds one explicitly started loopback development target that imports a
pinned authored profile and registers authoritative live Forge selections in
that source-neutral registry. Either source can then enter the existing private
Presenter Stage terminal and X-ray flow through the same launch-option and
selection-exchange API. This work does not add public attendee admission,
promotion, catalog publication, production capability evidence, or normal
DREAMLAND runtime cost.

## Decision

Add a strict source-neutral runtime codec. Initial authored revisions and their
detached runtime envelopes use the authored codec path. Initial Forge revisions
and their detached runtime envelopes use the existing Forge codec path. The
private codec context retains the closed source kind across accepted state
changes, and a detached runtime envelope is accepted only after its immutable
origin was verified by that codec instance. An accepted command cannot change
an authored session into a Forge session, discard Forge origin, or inject an
unrecognized profile kind.

Import the authored option before service publication through the existing
trusted startup importer. Startup configuration supplies a regular non-symlink
profile path and exact metadata-free SHA-256 pin. The importer verifies the
pin, profile shape, synthetic boot, content-addressed identity, and owner-only
artifact store before registering a safe option. Profile name, source path,
mutable store identity, source metadata, and authority do not enter HTTP
responses, startup announcements, or browser configuration. The verified
content digest is intentionally published as the non-secret immutable revision
identity.

Register a Forge option only after the live Forge service has atomically
committed an authoritative selected record. A trusted registrar resolves that
record with the shared Presenter actor and current Forge epoch, requires its
actor identity to match the adjacent Stage/registry actor, reopens the exact
revision and digest from the immutable Forge store, and atomically registers
the verified revision and option. Receipt fields, a job reference, a digest,
or an artifact path alone are never sufficient. Observer failure leaves the
Forge job truthfully selected, emits a safe failure event, and publishes no
partial generic option.

Derive browser-safe Forge offering and selection locators from a process-
private keyed digest of the selected record. They remain non-authorizing and
stable for an exact callback replay within the process epoch. The resulting
option expires no later than its authoritative selected record, and an
exchanged grant expires no later than that option. Actor context, transport
bearers, launch grants, session capabilities, and the locator key remain on the
BFF/server side.

Enforce externally stated actor, option, and grant UTC expiries as a hard wall-
clock bound in addition to monotonic deadlines. A backward wall adjustment
cannot extend the monotonic deadline, while a forward adjustment expires the
absolute authority immediately. Dynamic Stage derives its monotonic grant
deadline from the registry's private exact deadline evidence on a required
shared monotonic clock, capped by the remaining UTC lifetime at provisioning,
rather than from its startup projection. The evidence never enters an HTTP
projection. A forward adjustment, rollback during the provisioning callback,
or compound adjustment therefore cannot make Stage authority outlive the
registry grant.

Compatibility provisioning without private registry deadline evidence retains
the original startup-projected monotonic bound and caps it by current UTC
remaining time. The legacy Forge seam uses the same conservative fallback.
This preserves rollback resistance without weakening or falsely shortening the
evidence-backed generic path.

Use one Forge display helper for the generic and legacy handoff seams. A
revision therefore has the same title, source label, and digest-derived
revision label regardless of which compatible private route provisions it.
This preserves Stage's one-display-tuple-per-revision invariant and keeps the
legacy Forge handoff routes operational.

Construct one shared Presenter expiry and actor identity for Forge, the launch
registry, and Stage. Construct Stage first and bind the registry to Stage's
exact process epoch. Keep revision, offering, grant, idempotency, session,
worker, command, output, TTL, provider-attempt, token, spend, and cancellation
capacity finite. Unlimited command mode omits only the positive
`max_commands` launch limit; every other protection remains active.

Publish the live target under additive `v0.7.0-rc.2`. Preserve every RC1 route
and payload semantic, but identify the explicit composition truthfully as a
provider-backed live-development executor. Launch-option listing, exchange,
artifact provisioning, and X-ray projection remain provider-free operations;
only an explicit Forge build or accepted Stage command can dispatch a model.
Every product capability remains disabled and live capability evidence remains
false.

Importing the codec, registrar, or tool starts nothing. Normal DREAMLAND SSH,
admin, MCP, profile, command, and enrichment composition imports none of this
target and creates no product registry, artifact import, listener, worker,
timer, reservation, provider, or bookkeeping path.

## Consequences

The UI BFF can pin RC2, list one configured authored option, refresh the same
list after a Forge job reaches `selected`, exchange either locator for private
Stage authority, and use the existing create or explicit switch, terminal,
transcript, event, and X-ray operations. A small selected-state-to-option
registration window is possible because Forge truth commits before the
observer publishes the adjacent option; the BFF must refresh rather than infer
an option from a receipt.

The legacy Forge handoff remains available for its richer Forge job/reference
source projection. The generic Forge path carries only the closed generic
origin, immutable identity, and display-safe metadata promised by the
source-neutral contract.

A fresh Forge Presenter option is not promotion, catalog, or attendee-
eligibility evidence. Public `/session` admission, attendee isolation and
quotas, and generated-world publication remain separate later phases.

## Evidence

Implementation commit `117e7e54854419e4cac386c66b9bb260bf780eec` owns the
live composition and is the exact backend commit published by Product API
`v0.7.0-rc.2`.

Provider-free owning tests cover authored and Forge codec round trips,
accepted-state mutation, unknown and cross-source rejection, selected-only
registration, exact artifact identity, actor matching, expiry, replay,
capacity, shared display truth, observer-failure isolation, positive and
unlimited command limits, safe startup output, inert import, and the complete
authored-to-Forge Stage switch with terminal and X-ray collaborators.

Contract tests preserve published RC1 byte-for-byte, validate RC2's unchanged
21-route surface and all-disabled capabilities, and exercise the generated
client against canonical and hostile fixtures. Historical live Stage,
Forge-to-Stage, X-ray, and Phase 7A preview suites remain compatibility gates.

## Maintenance

Update this record together with `src/dreamland/product/launch_selection_*`,
`tools/product_launch_selection_live.py`, its contract generator and tests,
`docs/PRODUCT_LAUNCH_SELECTION_LIVE_DEVELOPMENT.md`,
`docs/PRODUCT_SESSION_API.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`, and
`ROADMAP.md` when changing live source dispatch, authored import, Forge
registration, shared actor/expiry composition, display truth, option expiry,
provider identity, limits, or the Presenter/attendee boundary.
