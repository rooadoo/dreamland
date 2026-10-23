# Display-Safe Presenter Stage X-ray Development Slice

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and coding agents
Purpose: record the Phase 6 projection, concurrency, privacy, and isolation boundary used for live UI integration.

## Context

The corrected Forge-to-Stage RC2 provides an authoritative selected revision,
private Presenter Stage session, real command execution, transcript, events,
and atomic replacement. The presentation UI also needs an inspector of accepted
world state. Returning the runtime document, `SessionState`, admin projections,
or transcript-derived guesses would expose private/internal data and let the UI
combine facts from incompatible revisions.

The mutual backend/UI agreement defines accepted-state revision and X-ray
projection position as independent coordinates. It also defines the Stage event
position as a correlation watermark rather than a third X-ray revision. Phase
6 must preserve that model while adding no object, worker, provider call,
listener, reservation, or bookkeeping to normal DREAMLAND when the product API
and UI are not in use.

## Decision

Add an inert `dreamland.product.xray` projection module and an optional
`StageXrayPolicy` argument to `PresenterStageService`. A service without the
policy retains the previous behavior exactly and allocates no X-ray state. The
explicit combined Forge/Stage live-development harness supplies the policy.

Build X-ray under the product-session lock from the accepted detached runtime
document. Do not import or serialize `SessionState` or reuse admin, SSH,
profile-store, tracing, provider, environment, or failure projections. The
initial allowlist is:

- bounded world name, theme, and description;
- bounded host hostname, purpose, operating-system display fields, and safe
  aggregate counts;
- accepted world and host facts only when a named scalar statement/display
  field is available; and
- separately structured foreground/enrichment activity that is never styled
  as accepted state.

Hash internal host and fact keys into stable display entity IDs. Replace Unicode
control and format characters, record that marking, clip text at configured
bounds, and report per-section omissions and clipping. Exclude paths, users,
environment, shell state, command text, arbitrary nested values, prompts,
reasoning, credentials, provider envelopes, traces, failure capsules, admin
controls, and other sessions.

Give each active X-ray session an opaque stream ID and monotonic sequence. A
new accepted-state revision always advances the X-ray sequence on observation,
even when its visible allowlist is unchanged. A projection-only activity change
may advance the X-ray sequence without changing accepted state. Snapshot and
detail return:

1. X-ray schema revision;
2. accepted-state revision;
3. X-ray stream, sequence, and cursor; and
4. the Stage event cursor observed under the same lock as a correlation
   watermark only.

Require accepted-state revision and X-ray stream/sequence on every detail read.
Return `xray_resync_required` when either coordinate differs. Do not accept the
Stage event watermark as a detail precondition and do not add an X-ray event
stream in this slice.

Terminalize X-ray independently: close, expiry, and successful replacement
drop the predecessor projection and report terminal X-ray unavailable. X-ray
projection failure must not affect Stage snapshot, command, transcript, event,
close, or replacement operation.

Publish the additive private contract as `v0.6.0-rc.1`, leaving published RC2
unchanged. It adds only presenter-session X-ray snapshot and detail GET routes,
strict schemas, fixtures, and generated server-side client methods. All
production capability entries, including `stage_xray`, remain disabled with
`integration_preview_only`; the explicit loopback live target is development
evidence, not production enablement.

## Consequences

The UI can open a selected Forge revision in the existing terminal/X-ray view
and render real accepted host/fact state without reading terminal output as
truth. A stale master/detail combination fails closed and has one recovery
action: replace it with a fresh X-ray snapshot.

The projection is intentionally narrower than the runtime model. Unsupported
facts and rich evidence are omitted honestly rather than serialized through a
generic escape hatch. Adding a field later requires allowlist, bound, privacy,
schema, fixture, generated-client, and owning-test changes. X-ray events,
enrichment evidence, relationship edges, production capability enablement, and
public attendee access remain separate future gates.

Normal DREAMLAND retains zero X-ray cost because the default Stage composition
does not supply a policy and ordinary startup does not import or construct the
combined development target.

## Alternatives Considered

Returning the accepted runtime document was rejected because its schema and
privacy surface are much broader than display needs. Reusing admin projections
was rejected because it crosses authority and lifecycle boundaries. Deriving
facts from transcript output was rejected because plausible output is not
accepted state. Using Stage event position as the X-ray revision was rejected
because projection-only changes need not append Stage events. Embedding all
detail in the ordinary Stage snapshot was rejected because X-ray failure and
growth must remain independent from terminal recovery.

## Evidence

Implementation record: commit
`eecc036f3ea9e067683289287f231a3a4bd1ab59` adds the projection service,
private routes, release-candidate artifacts, generated client, documentation,
and owning tests described by this decision.

Owning service tests cover disabled-mode behavior, strict allowlisting, bounds,
omissions, hostile control/format text, stable details, projection-only and
accepted-state coordinate advancement, stale detail denial, terminal cleanup,
and failure isolation. Provider-free combined HTTP tests cover authoritative
Forge handoff, active X-ray snapshot/detail, generated-client validation,
stale-position rejection, and replacement terminal behavior. The
`v0.6.0-rc.1` validator and client smoke cover strict schemas, canonical
fixtures, privacy mutations, and coordinate drift.

## Maintenance

Update this decision, `docs/ARCHITECTURE.md`, `docs/PRODUCT_SESSION_API.md`,
`docs/FORGE_STAGE_LIVE_DEVELOPMENT.md`, the X-ray contract kit, and owning tests
together when changing the allowlist, bounds, entity IDs, coordinates, recovery
errors, authority, terminal behavior, or capability status.
