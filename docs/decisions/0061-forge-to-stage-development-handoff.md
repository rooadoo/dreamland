# Forge-to-Stage Development Handoff

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and coding agents
Purpose: record the exact-authority and runtime-isolation decisions behind the
Phase 5 live development handoff.

## Context

The RC4 World Forge vertical slice can produce a real validated,
content-addressed world revision, and the Phase 3D Presenter Stage can run a
real command agent. They previously had no trusted connection. A browser Forge
reference, bare digest, artifact path, profile name, UI fixture, or catalog
identity could not safely authorize Stage construction. The UI development
checkpoint needs the exact selected world in the existing Stage terminal
without weakening normal DREAMLAND, adding a static fallback, or implying that
unfinished production gates have passed.

## Decision

World Forge atomically commits an immutable presenter-owned selected-revision
record only when an explicitly live source with provider-call and live-evidence
claims reaches `selected` through a trusted receipt before its deadline. The
record binds its service epoch, opaque record identity, job and reference,
actor, selection time, independent expiry, policy, exact revision ID and
digest, source fingerprint, and safe receipt correlation. Preview selection,
every non-selected state, wrong authority, stale epoch, expiry, cancellation,
and late completion mint no usable record.

Use a trusted in-process `ForgeStageHandoffService` to resolve that record,
reopen the exact owner-only content-addressed artifact, provision a narrow
Presenter create/switch grant, and delegate create, identical-selection
resume, or explicit expected-control-CAS replacement to
`PresenterStageService`. The transport accepts only the non-authorizing Forge
reference. It does not accept an artifact path, revision/digest pair, profile
name, fixture selection, catalog identity, or launch grant from the caller.
The Forge expected-epoch header is semantic idempotency evidence alongside the
canonical body, so an already claimed key cannot be rebound after Forge epoch
reconciliation.

Dynamic Stage provisioning begins with empty revision and grant catalogs. It
revalidates the immutable Forge envelope and identity before atomically adding
the revision, grant, display metadata, and Forge source binding. It never reads
or mutates the default state-profile store and never registers the session in
ordinary SSH, admin, or MCP runtime indexes. Failed provisioning and failed
replacement preserve the existing Stage and do not install fallback state.

Preserve both hash domains. The Forge digest hashes the exact canonical
artifact file, including its terminating newline. The existing product
lifecycle SHA hashes the newline-free canonical revision document. The values
are expected to differ. Stage exposes the Forge digest and selected
correlations under its optional `stage.source`; it keeps the lifecycle SHA in
the existing starting-revision projection.

Publish the strictly additive private product release candidate as
`v0.5.0-rc.1`. It retains every RC2 route and adds only the selected Forge
launch and explicit replacement routes. Its manifest pins an exact dependency
on published World Forge `v0.1.0-rc.4` by manifest SHA-256, and the combined
startup verifies that pin before provider construction. The generated
server-side client checks that handoff selection, Stage source, and starting
revision agree.

Provide one explicitly started development tool with two loopback listeners,
two distinct private bearers, and one shared Presenter actor. It constructs the
live Forge and Stage provider collaborators only during `serve`. The BFF keeps
both bearers, actor context, service epochs, launch/session capabilities, and
idempotency state; browser code receives only the non-authorizing Stage
reference and opens its existing local Presenter route.

Do not enable a capability. `product_session_core`,
`presenter_stage_terminal`, `forge_to_stage_handoff`, `stage_xray`, and the
other product capabilities remain disabled. X-ray remains explicitly
unavailable and belongs to Phase 6. Normal DREAMLAND startup continues not to
import product composition or incur Forge/Stage listeners, workers,
bookkeeping, or provider cost.

## Consequences

Maintainers and the UI team can evaluate one genuinely generated world through
the existing private terminal and prove exact Forge-to-Stage identity,
same-request replay, refresh, commands, and deliberate replacement. The flow
cannot silently launch the rehearsal world or accept a forged revision.

In the combined development target, the selected record and grant expire one
hour after selection, so that window caps Stage even though its configured
absolute lifecycle TTL is longer; idle expiry may shorten it further.
Unlimited accepted commands disable only the
count guard and do not remove concurrency, TTL, output, token, spend, or
provider-attempt bounds.

This remains development-only. In-memory Forge jobs, non-terminating lifecycle
deadlines, single A-only generation, no repair, no fixed A/B/C selection, no
shared Forge/Stage provider-budget authority, conservative rather than exact
Stage usage, no production private gateway, and incomplete deployment/load/
restart evidence prevent capability enablement.

The published `v0.5.0-rc.1` metadata binds this decision and contract to tested
implementation commit `262fb8f9b6e4c5cf0458ac4c787ffbbc08833af1`.

## Maintenance

Update this decision before changing selected-record authority, artifact/hash
identity, trusted provisioning, dynamic-only Stage behavior, handoff replay or
replacement semantics, BFF credential ownership, contract dependency, normal
runtime isolation, capability status, the cited implementation commit, or the
X-ray phase boundary.
