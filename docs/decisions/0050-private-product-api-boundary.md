# 0050 Private Product API Boundary

Status: Active
Audience: maintainers, UI BFF developers, DREAMLAND operators, and coding agents
Purpose: record the trust, composition, capability, and compatibility boundaries
for DREAMLAND's browser product integration.

## Overview

DREAMLAND needs browser attendee and Presenter Stage experiences without making
the browser an Operator, exposing the existing admin bearer, or changing the
SSH runtime. The browser integration therefore uses a same-origin UI-owned BFF
and a separate private DREAMLAND product contract.

The first implementation checkpoint is intentionally inert. It establishes a
default-off composition seam and a release-candidate contract kit for early BFF
integration. It does not implement product sessions or add a product listener
to the normal DREAMLAND process.

This is classified under the [project model](../PROJECT_MODEL.md) as broader
replaceable-entry-point work toward the future LLM-emulated network. It is not a
change to the current SSH-host runtime model; SSH remains the ordinary entry
point unless independently implemented and gated product behavior is enabled.

## Decision

### Two separately owned contracts

The UI project owns the browser-to-BFF contract. That boundary owns browser
routes and DTOs, Secure/HttpOnly/SameSite cookies, browser-context bootstrap,
presenter authentication, CSRF, Origin, Host, CORS, cache behavior, public
throttling, browser-safe error copy, and the request and promised-event
deadlines used by the browser.

DREAMLAND owns the private BFF-to-DREAMLAND contract. That boundary owns
private launch authority, service epoch, runtime capabilities, sessions,
commands, cancellation, accepted state, transcript, snapshots, event recovery,
limits, safe private errors, and the canonical machine-readable artifacts.
The private generated client and capability material remain in server-side BFF
code. They must not enter browser JavaScript, URLs, public DTOs, analytics,
logs, cache keys, source maps, or other browser artifacts.

The existing admin REST/MCP bearer remains a separate Operator authority. The
private product service is not an extension, alias, or reduced view of
`DreamlandAdminService`, `/api/v0`, `/mcp`, or `/client-mcp`.

### Separate, default-off composition

Product behavior is composed through `src/dreamland/product/composition.py`,
separately from the existing SSH, admin, MCP, profile, logging, and
EnrichmentAgent paths. The `[product_api] enabled` setting is the master
activation switch. It defaults to `false` and is not a product capability.

The disabled branch is decided before any product listener, coordinator,
executor, journal, sweeper, timer, subscription, provider reservation,
idempotency ledger, session allocation, or product-specific enrichment object
is constructed. Dormant source and import footprint is acceptable; material
product-attributable idle CPU, memory, network, provider, tracing, or runtime
work is not.

Phase 0 deliberately has no live product composition. Setting
`[product_api] enabled = true` fails startup closed with a stable unavailable
error rather than starting a partial service or silently falling back to the
admin API, fixtures, SSH, or a fixed world. Later phases may replace that
failure with an optional product lifecycle aggregate only when the advertised
capability gate is complete.

### Independent capability truth

The private product contract has exactly five independently versioned session
capabilities:

1. `product_session_core`
2. `presenter_stage_terminal`
3. `public_attendee_sessions`
4. `forge_to_stage_handoff`
5. `stage_xray`

Stage terminal and public attendee sessions depend on the shared core.
Forge-to-Stage additionally depends on Stage and a compatible, separately
versioned live World Forge predecessor. Stage X-ray depends on a Stage session
and its accepted-state model but not on Forge-to-Stage. No capability implies
another extension capability or grants Operator authority.

The generated-world attendee path is also separate:

```text
selected and validated Forge revision
  -> promotion/access record
  -> generic catalog reveal
  -> attendee launch selection
```

It is not supplied by Forge-to-Stage and is not silently folded into one of the
five session capability identifiers.

DREAMLAND owns the later immutable promotion/access record, authoritative
offering eligibility, bounded private catalog projection, and server-side
offering-to-launch-selection exchange. The offering ID remains display/catalog
identity, never runtime authority. The UI-owned BFF owns browser catalog routes,
DTOs, presentation, browser authority, and private-to-browser adaptation; it
does not mint DREAMLAND authority. Full catalog administration,
presentation-specific metadata and ordering, deployment publication, and
promotion canaries remain a separate workflow. Consequently this path does not
block shared core or the first live Stage terminal, but it does gate any claim
that generated-world attendee access or the complete presentation path is live.

Missing, disabled, unknown, or incompatible capability evidence fails closed.
Master activation never makes an incomplete capability true. In Phase 0 all
five release and runtime claims are disabled and have no enabled revision.

### Authority and runtime isolation

Future product sessions must start from an explicitly authorized immutable
world revision. They must not change the process-global default profile, write
the legacy profile-session index, expose raw structured-failure capsules, or
alter the behavior of SSH/admin/MCP sessions. Browser actor, attendee,
Presenter, and Operator authorities remain non-interchangeable. Safe public or
Stage references are identifiers, not runtime authority.

Conference product-session state is process-local and bound to a high-entropy
service epoch. Restart rotates the epoch instead of recovering or redispatching
uncertain in-memory work. The detailed epoch, idempotency, command,
cancellation, replacement, enrichment, and projection requirements are kept in
`docs/PRODUCT_SESSION_API.md` and will receive owning implementation decisions
as their phases are built.

### Initial provider isolation

The first live product deployment uses dedicated product provider credentials
and a separate, non-overcommitting product pool. Product work cannot borrow
ordinary SSH or EnrichmentAgent capacity. All product work sharing those
credentials—including commands, product enrichment, Forge, handoff work, and
retry/quarantine identities—uses one product admission ledger, and configured
reservations remain inside provider-account and deployment-wide hard limits.
If this separation and accounting cannot be proved, affected capabilities stay
disabled; in particular, Stage may not advertise reserved capacity.

This is intentionally stricter than merely assigning product work a logical
queue. It protects ordinary DREAMLAND latency and capacity even when the API is
enabled and ensures that a separate pool does not overcommit a shared upstream
account.

### Retention, tracing, and redaction ownership

DREAMLAND owns the bounds and deletion behavior of process-local product
sessions, transcripts, events, idempotency records, and tombstones. Product
close, expiry, and restart promises cover only that local state. DREAMLAND also
owns source redaction for its product logs and traces: private service/browser
credentials, provider keys, raw capabilities, prompts, reasoning, provider
envelopes, failure capsules, environment values, and internal paths do not
cross the product telemetry boundary. Later implementations may emit only an
explicit safe allowlist with owning tests.

The deployment/operator owns external product-tracing enablement, approved
destinations, third-party retention/deletion policy, and the published notice.
External tracing for public attendee traffic begins disabled and requires an
explicit data-handling approval and tests before enablement. Local close never
claims deletion from OpenAI, Langfuse, or infrastructure providers.

The UI/BFF owns browser and gateway logging, analytics, redaction, retention,
browser-notice delivery, and protection of its cookie/session material. It may
send minimal notice-version delivery evidence to DREAMLAND, but that evidence
is not legal consent and no private DREAMLAND capability or credential may
enter browser or gateway telemetry.

### Release-candidate artifacts

The backend-owned release candidate is
`contracts/product-api/v0.1.0-rc.1/`. It is exact-release-candidate-only and
records `pending_ui_tracked_doc_acknowledgement` as its freeze status. Its only
implemented private HTTP operation is capability discovery in the explicit
integration simulator. Reusable unreferenced component schemas are design
inputs, not claims that their operations exist.

Setting the manifest publication status to `published` only attaches tested
implementation commit and publication-time metadata to an unfrozen RC. It does
not override the freeze status, make its wire names canonical, demonstrate UI
consumption, or satisfy schema-freeze or integration evidence. Those claims
require the tracked UI documentation acknowledgement, an explicit coordinated
freeze, and the applicable generated-client/integration results.

The repository validator is a focused structural validator for duplicate keys,
schemas, local references, fixtures, hashes, safe errors, and Phase 0
invariants. Its reference-client smoke covers only the repository's capability
client path. Neither is full external OpenAPI conformance or a substitute for
independent validators, third-party generator coverage, live authorization,
transport, concurrency, security, or load evidence.

The separately invoked simulator is loopback-only, bearer-protected, and
test-only. It is imported by no DREAMLAND runtime module, serves no session or
mutation routes, and cannot satisfy a live capability gate. Canonical transport
schema freeze waits for the UI's tracked-document acknowledgement. After
freeze, published artifacts and their digests become canonical for wire names
and shapes; this decision continues to govern the trust and no-impact boundary.

Phase 0 does not deliver a promised-feed/withheld-event scenario because it has
no mutation acknowledgement or event feed. The first phase that implements
both owns the deterministic scenario in which an authoritative acknowledgement
promises an event, the simulator withholds it beyond the consumer deadline, and
the client reconciles by same-key result query or authoritative snapshot
without redispatch. Deferring that scenario to its first truthful owning phase
changes sequencing, not the accepted recovery requirement.

## Alternatives Considered

Extending the admin REST/MCP service was rejected because its Operator bearer,
DTOs, inspection data, and lifecycle are broader than a browser product
boundary. Allowing a browser to call DREAMLAND directly was rejected because it
would place private authority and transport types in an untrusted bundle and
duplicate browser security policy in the runtime.

Using the process-selected state profile or legacy session registry for product
launch was rejected because it would couple browser behavior to ordinary SSH
operation and weaken immutable selection authority. Treating `product_api` as a
sixth capability, or enabling all extensions together, was rejected because it
would make deployment configuration look like acceptance evidence.

Constructing idle product workers or provider reservations while the gate is
off was rejected because an unused UI/API must impose no material cost on the
underlying DREAMLAND experience. Starting a fixture-backed or partially
implemented production listener in Phase 0 was rejected because it would turn
development scaffolding into false capability evidence.

Sharing ordinary DREAMLAND provider credentials or reservable capacity with the
initial product deployment was rejected because product load could then reduce
SSH or EnrichmentAgent capacity. Treating external tracing retention as local
session cleanup was rejected because DREAMLAND cannot delete provider-owned
records. Letting the UI mint launch authority from catalog identity was rejected
because a display identifier is not an authorization grant.

## Consequences

The UI BFF must translate between two contracts and keep the private generated
client server-side. DREAMLAND must maintain a distinct product service,
transport authentication boundary, capability projection, lifecycle, and
acceptance evidence. This costs some adapter code but prevents browser
concerns from entering the simulation runtime or Operator API.

Normal DREAMLAND operation remains baseline-equivalent while `product_api` is
disabled. Phase 0 gives the UI team deterministic artifacts and failure
fixtures for early integration but no live session functionality. Each later
capability remains unavailable until its owning behavior, security,
concurrency, load, and compatibility gates pass.

## Maintenance

The Phase 0 composition seam, release-candidate artifacts, simulator, and
owning tests are recorded in implementation commit
`e0e48809ed3967b5068c55c8b2a2c09c16a461d2`.

Update this record, `docs/PRODUCT_SESSION_API.md`, `docs/ARCHITECTURE.md`,
`docs/OPERATIONS.md`, `docs/INDEX.md`, `ROADMAP.md`, the example configuration,
contract manifest, and owning tests together if the trust boundary, master
gate, capability identifiers, disabled-cost invariant, artifact freeze policy,
product-provider isolation, telemetry/retention ownership, generated-world
catalog exchange ownership, promised-event simulator sequencing, or separation
from SSH/admin/MCP changes. Record narrower lifecycle and concurrency
implementation decisions in new decision records rather than rewriting planned
behavior as if it were already live.
