# World Access Recovery Correlation And Evaluator Truth

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the Phase 8A correction that lets a private BFF correlate a
World Access exchange with a recovered Product session while keeping preview
notice and timestamp claims truthful.

## Overview

World Access RC2 completes the normal hidden, reveal, catalog, exchange, and
Product-session path. It does not, however, give the private BFF a value that
can prove a Product `GET current` snapshot came from the exact World Access
exchange when the Product create response remains lost or undecodable. Notice
version equality is policy compatibility, not selection identity.

The first live UI smoke also exposed two preview-composition discrepancies. The
co-hosted authored and generated offerings carried different notice versions,
and the executable preview inherited a fixed test timestamp that appeared
expired to a current evaluator. These are backend truth defects; the UI must
not silently filter an offering or rewrite authoritative timestamps.

## Decision

Keep the published World Access `v0.1.0-rc.2` artifact byte-for-byte immutable.
Publish the response-shape correction as `v0.1.0-rc.3`, preserving all six
operations and the existing Product RC3 and World Forge RC4 dependency pins.

Add required `launch_binding_sha256` to the private World Access launch-
selection success response. It is the raw lowercase SHA-256 value already
computed while Product provisions the launch grant and later exposes as
`SessionSnapshot.launch_binding.sha256`. Carry that existing value through the
trusted provisioning receipt and the recorded World Access exchange result.
Do not duplicate Product's semantic-binding hash algorithm in the catalog,
HTTP, or contract layers.

The BFF correlates the pair
`(World Access service_epoch, launch_binding_sha256)` with
`(Product service_epoch, snapshot.launch_binding.sha256)`. Exact World Access
exchange replay and exact Product create replay retain the same pair. A digest
or epoch mismatch is not recovery: the BFF keeps the uncertain create fence,
does not attach World Access display metadata to the current session, and does
not permit a different mutation to cross that fence. Replaying the exact
Product create remains the primary recovery path; current-session correlation
is the fail-closed fallback when a usable create response is still unavailable.

Treat the digest only as BFF-side comparison evidence. No World Access or
Product request accepts it as actor, launch, session, reveal, catalog, or
control authority. It must not cross the browser DTO boundary. Existing Product
authored option and exchange responses remain unchanged; the additive field
belongs only to the separately versioned generic World Access response.

In the explicit co-hosted World Access preview, bind authored and generated
catalog offerings to the one Phase 8A notice version. Leave the standalone
Product attendee preview and its published contract behavior unchanged. A
different presented notice continues to fail closed for either origin before a
grant is published.

Anchor executable World Access preview timestamps to wall time at process
startup plus monotonic elapsed time. Preserve explicit deterministic clock
injection for automated tests and fault scenarios. Successful preparation and
reveal must report ordered timestamps whose expiry remains in the evaluator's
future; the UI does not reinterpret them.

Keep every change behind explicit Product/World Access construction. Importing
or starting ordinary DREAMLAND creates no World Access listener, clock,
provider, worker, authority registry, correlation record, or bookkeeping.

## Consequences

The UI BFF can recover conservatively after an ambiguous Product create. A
matching epoch and launch-binding digest proves that the current Product
session consumed the exact semantic grant returned by the recorded World
Access exchange, without disclosing its revision, selected record, access
record, actor, credential, or raw state.

The digest is deliberately equality evidence rather than durable identity.
Process restart rotates the epoch, invalidates authority, and prevents an old
exchange result from correlating with a new service generation even if other
display or policy fields happen to match.

One co-hosted notice keeps the mixed catalog truthful for the current Phase 8A
evaluation. Supporting genuinely different per-offering notices would require
a later safe notice-content or notice-locator contract and explicit UI consent
semantics; a version token alone is not sufficient.

The evaluator clock removes misleading historical dates without weakening the
dual-clock expiry model or making provider-free tests depend on real time.
Normal DREAMLAND runtime behavior and unused-path cost remain unchanged.

## Evidence

Owning tests must prove generated exchange-to-session binding equality, exact
exchange and create replay stability, distinct-grant inequality, epoch mismatch
rejection, output-only handling, malformed digest rejection, and absence of
private coordinates or credentials. Cross-service preview tests must exercise
the ambiguous-current comparison target, not infer identity from notice or
display fields.

Preview tests must prove an authored-only hidden catalog, a revealed mixed
catalog with one notice version, successful authored and generated exchanges
for that exact notice, stale-notice rejection without grant publication,
current ordered future-expiring timestamps, deterministic clock injection, and
unchanged standalone Product notice behavior.

Contract publication must preserve complete RC1 and published RC2 tree
identities, compile and exercise the RC3 server-only TypeScript client, and bind
the RC3 manifest to the exact tested implementation commit.

## Maintenance

Update this record with `src/dreamland/product/launch_selection.py`,
`src/dreamland/product/attendee.py`,
`src/dreamland/product/attendee_catalog.py`,
`tools/product_world_access_contract.py`,
`tools/product_world_access_preview.py`, the World Access RC3 artifact,
`docs/PRODUCT_WORLD_ACCESS_DEVELOPMENT.md`, `docs/PRODUCT_SESSION_API.md`,
`docs/ARCHITECTURE.md`, `docs/TESTING.md`, and `ROADMAP.md` whenever launch-
binding correlation, notice composition, evaluator time, recovery fencing,
contract publication, or normal-runtime isolation changes.
