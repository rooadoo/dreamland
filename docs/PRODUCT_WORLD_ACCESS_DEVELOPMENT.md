# Generated-World Access Development

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, and local evaluators
Purpose: explain the provider-free Phase 8A boundary and the explicitly started,
capability-disabled Phase 8B live Forge-to-attendee composition.

Current source-release artifact paths are listed in [the contract inventory](../contracts/README.md).
Superseded snapshots discussed in historical phase notes may be omitted.

## Overview

Phase 8A implements the previously separate generated-world attendee path:

```text
selected immutable Forge revision
  -> hidden DREAMLAND access record
  -> explicit reveal and catalog publication
  -> exact catalog-and-notice exchange
  -> Product-compatible attendee launch grant
  -> unchanged Product session create
```

The corrected integration contract is separately versioned as
`contracts/world-access-api/v0.1.0-rc.3/`. It pins Product API
`v0.7.0-rc.3` and World Forge API `v0.1.0-rc.4` rather than adding routes to
either published artifact. The existing Product attendee option and exchange
operations remain authored-only. A selected Forge record, Presenter offering,
catalog locator, access-record identifier, or immutable revision coordinate is
not attendee launch authority.

RC3 preserves the six RC1/RC2 operations and the corrected RC2 lifecycle,
retry, actor-class, media-type, and dependency boundaries. It adds one required
server-only launch-binding digest so response-loss recovery can correlate the
exact World Access exchange with an unchanged Product session snapshot. The
reviewed RC1 and published RC2 artifacts remain byte-exact and unmodified.

The Phase 8A preview has a deterministic source and executor, is provider-free,
process-local, and loopback-only, and reports evaluator-current lifecycle
timestamps. It supplies integration evidence for the UI-owned BFF but is not
live Forge evidence, provider-backed attendee evidence, production capability
evidence, or presentation-readiness evidence.

Phase 8B adds a separate, explicitly invoked composition in
`tools/product_world_access_live.py`. It joins the authoritative live Forge
selected-artifact source, World Access, the live attendee executor, and Product
session service under one shared World Access/Product service epoch. It does
not widen the Phase 8A authority boundary or enable a production capability.
The additive World Access `v0.1.0-rc.4` artifact is a published live-
development identity bound to implementation commit
`d1df607bce702475fa8ed79765263445d0e02c6b`. A bounded backend provider smoke
passed on 2026-08-30; it is evidence for this composition, not production
capability or presentation readiness.

## Details

### Contract And Dependencies

The private World Access contract exposes six operations:

```text
GET  /world-access/v1/identity
POST /world-access/v1/promotions
GET  /world-access/v1/promotions/{promotion_id}
POST /world-access/v1/promotions/{promotion_id}/reveal
GET  /world-access/v1/catalog
POST /world-access/v1/launch-selections
```

Use Node.js 20 or newer to install the isolated, lockfile-pinned TypeScript
development compiler, then
validate the artifact and its server-only generated client with:

```sh
node --version
npm ci --prefix tools/world_access_client_smoke
.venv/bin/python tools/product_world_access_contract.py validate \
  contracts/world-access-api/v0.1.0-rc.3
.venv/bin/python tools/product_world_access_contract.py client-smoke \
  contracts/world-access-api/v0.1.0-rc.3
```

The manifest pins the exact Product RC3 and World Forge RC4 manifest digests.
Product RC3 supplies the provider-free authored-attendee facade and unchanged
session operations. World Forge RC4 defines the authoritative selected-record
and immutable-artifact boundary. Product RC4 remains an identity-only live
sibling and is deliberately not the provider-free dependency for Phase 8A.

The published live World Access RC4 sibling preserves the same six operations,
actor classes, safe errors, launch-binding correlation, and recovery semantics.
It instead pins Product API `v0.7.0-rc.4` and World Forge API
`v0.1.0-rc.4`. Its response identity truthfully reports
`provider_calls: true`, `live_forge_evidence: true`, and
`live_attendee_execution: true`, while `live_capability_evidence` remains false
and the enabled-capability set remains empty. Validate the artifact and its
separate server-only client smoke with:

```sh
.venv/bin/python tools/product_world_access_live_contract.py validate \
  contracts/world-access-api/v0.1.0-rc.4
.venv/bin/python tools/product_world_access_live_contract.py client-smoke \
  contracts/world-access-api/v0.1.0-rc.4
```

This synchronous checkpoint publishes no event schema. A BFF reconciles a lost
promotion or reveal response with exact idempotent replay and the promotion
status read. Catalog state is recovered by reading the bounded catalog again;
the BFF does not infer publication from a Forge job or locally mint authority.

### Promotion And Reveal Authority

`src/dreamland/product/generated_access.py` owns the bounded generated-access
aggregate. Trusted composition installs private Operator actors and a narrow
selected-revision source. A promotion request supplies only current epoch and
Operator authority, an idempotency key, and a non-authorizing Forge job
reference. The aggregate resolves the authoritative selected record and exact
immutable artifact server-side, revalidates their relationship and canonical
content, and creates a hidden immutable access-record revision.

The request and response never accept or expose an artifact path, raw world
state, Forge bearer, selected-record authority, or caller-chosen digest.
Promotion status exposes only bounded access identity, lifecycle state, safe
origin, immutable revision coordinates, timestamps, catalog eligibility, and
replay truth.

Reveal is a separate Operator mutation with expected-access-record compare and
set. The catalog publisher must successfully reserve and register the exact
eligible revision before the aggregate commits the revealed access-record
revision. Retryable publication capacity therefore cannot leave a revealed
record absent from the catalog. A changed or expired record fails closed.
If the access cutoff or monotonic-clock check fails after catalog reservation,
the trusted publication receipt removes that pending binding and only the exact
offering, token reservation, and otherwise-unreferenced revision installed by
the attempt before failure returns. A deterministic cutoff rejection remains
claimed and replayable.
The coordinator holds the access lock and then the catalog lock across the
reentrant reveal and pending-to-active move. Status and catalog readers therefore
linearize either before the hidden record changes or after the revealed record
and offering are both visible; they cannot observe a revealed-but-omitted gap.
Successful replay returns the original result rather than publishing another
offering.

Promotion and reveal state, actor authority, idempotency, token history, and
retention are finite and bound to one high-entropy service epoch. Deterministic
rejections are claimed and replayable; expressly transient source or capacity
failures leave the key unclaimed. The backend permits a later request to bind
that key, while BFF recovery policy retries only the identical body. Restart
rotates the epoch and invalidates old authority instead of rebasing an uncertain
mutation.

The publication validator owns an exact six-operation matrix: method, path,
bearer security, effective headers and path parameters, actor class, request
schema, success schema, and safe-problem schema must all match, with no extra
operation admitted. The generated server-only client advertises JSON success
and problem JSON failure, validates the status-appropriate response media type
before decoding, compiles under the lockfile-pinned development toolchain, and
exercises all six calls through a bounded credential-free test transport.

### Catalog And Attendee Exchange

`src/dreamland/product/attendee_catalog.py` is the only bridge from revealed
access truth into the attendee launch registry. Its bounded projection may
contain authored offerings and revealed generated offerings. Each item carries
only an opaque offering ID, opaque offering revision, closed origin, bounded
display tuple, notice version, and expiry. It omits the internal launch-
selection ID, access record, promotion ID, revision ID, digest, path, raw
state, actor context, and launch authority.

A generated offering remains hidden until reveal has atomically published it.
Directly registering a Forge-origin offering does not satisfy access
eligibility: the catalog omits that entry, and exchange cannot turn it into a
grant. Catalog positions are opaque observation coordinates, not authority or
launch input.

Exchange binds all of the following to one idempotent semantic request:

- current epoch and leased attendee actor;
- exact offering ID and offering revision;
- exact notice version presented by the UI; and
- current server-owned catalog eligibility.

The coordinator mints a short-lived signed eligibility proof only inside the
trusted composition boundary. That proof binds the epoch, offering and access
record, origin, immutable revision and digest, and expiry. The attendee facade
verifies it before using the separate generated-revision provisioning seam.
Neither the proof nor an internal selection identifier crosses the private API.
The response contains the normal Product-compatible `launch_grant`, which the
BFF retains server-side and supplies to the unchanged Product session-create
operation. It also contains output-only `launch_binding_sha256`, copied from
the semantic binding Product computed while provisioning that grant. The value
is comparison evidence, not authority, and is never accepted by a request.
Exact exchange replay returns the same digest.

After an ambiguous Product create response, the BFF first replays the identical
Product key and body. If it must reconcile through `GET current`, it treats the
session as the exchanged selection only when both the World Access/Product
service epochs and the World Access `launch_binding_sha256`/Product snapshot
`launch_binding.sha256` values match. Notice, display, or revision-label
equality is insufficient. A mismatch retains the uncertain mutation fence and
must not relabel the current session. The digest remains in protected BFF state
and does not cross the browser DTO boundary.

Generated support in `PublicAttendeeSessionService` and
`AttendeeSessionCoreService` is constructor-disabled by default. Existing
`/product/v1/attendee/launch-options` and
`/product/v1/attendee/launch-selections` remain authored-only even when a
separate World Access coordinator is explicitly constructed. The new offering
exchange has a separate idempotency family, so it cannot reinterpret or collide
with an existing Product attendee request.

The legacy authored-options read keeps the launch registry locked through its
snapshot and authored-only filter. A concurrent rollback of an unpublished
generated registration therefore waits and cannot make a projected option
disappear inside the published Product operation.

### Provider-Free Preview Workflow

The integration harness co-hosts the pinned Product RC3 and World Access routes
on one loopback listener. This is a preview convenience, not a production
topology or a merger of their contract identities. Start it explicitly with an
owner-controlled bearer file and both pinned contract roots:

```sh
.venv/bin/python tools/product_world_access_preview.py \
  --contract-root contracts/world-access-api/v0.1.0-rc.3 \
  --product-contract-root contracts/product-api/v0.7.0-rc.3 \
  --token-file /path/to/owner-only/world-access-preview.token \
  --host 127.0.0.1 \
  --port 0
```

The startup announcement reports the selected loopback address, deterministic
Forge job reference, preview Operator context, and all-disabled/no-provider
status. The provider-free source and executor remain deterministic, while
executable lifecycle timestamps advance from startup wall time using monotonic
elapsed time. Tests may inject an exact deterministic clock. These fixture
coordinates and private authority are
printed only to the invoking operator's terminal and remain server-side BFF
inputs; they are not browser configuration.
The announcement does not report the bearer, access capability, launch grant,
artifact path, raw state, or a production capability claim. The identity route
returns the current service epoch and exact dependency identities.

The BFF evaluation sequence is:

1. Read World Access identity and retain the current epoch and exact dependency
   revisions.
2. Bootstrap an attendee context through the co-hosted Product RC3 private
   endpoint and retain its actor context server-side.
3. Use the announced preview Operator context to promote its deterministic
   selected Forge artifact. Retain the returned promotion ID and hidden access-
   record ID server-side.
4. Confirm the generated offering is absent from the attendee catalog while the
   record is hidden.
5. Reveal with the exact hidden access-record ID and a fresh idempotency key.
6. Read the catalog again and confirm the authored and generated offerings are
   both governed by the one Phase 8A notice version. Present that notice and the
   selected safe offering fields through the UI-owned browser DTO.
7. Exchange the exact offering ID, offering revision, and presented notice
   version. Retain the returned Product launch grant, service epoch, and launch-
   binding digest in the BFF.
8. Use that Product RC3 attendee actor and unchanged Product session-create,
   command, transcript, event, recovery, and close operations. The deterministic
   executor performs no provider call.
9. If create recovery falls back to the current-session read, require exact
   epoch and launch-binding-digest equality before associating World Access
   display metadata or clearing the uncertain-create fence.

The BFF replays only an identical key and semantic body after a lost mutation
response. It does not expose the private bearer, Operator/attendee actor
contexts, grant, or Product session capability to browser code or storage.
The generated private client accepts only credential-free plain-HTTP base URLs
rooted at `127.0.0.1` or `localhost`; it rejects query, fragment, and subpath
components before attaching the bearer. Cross-role requests fail uniformly:
attendee authority cannot invoke promotion/status/reveal, and Operator
authority cannot read or exchange the attendee catalog. These cross-role
rejections occur before idempotency reservation and create no promotion,
publication, grant, or session state.

### Phase 8B Live Development Workflow

The Phase 8B target is opt-in and loopback-only. First calculate the exact
metadata-free digest for one trusted authored fallback profile:

```sh
.venv/bin/python tools/product_world_access_live.py digest-authored \
  --profile-path /path/to/state_profiles/example.json
```

Create distinct owner-only World Forge and Product bearer files plus one
bootstrap containing separate Presenter and Operator contexts:

```sh
.venv/bin/python tools/product_world_access_live.py init \
  --forge-token-file /path/to/private/world-access-live/forge.token \
  --stage-token-file /path/to/private/world-access-live/product.token \
  --bootstrap-file /path/to/private/world-access-live/bootstrap.json
```

The command refuses to overwrite existing files. The Presenter context is the
private authority used by the existing Forge/Presenter composition. The
Operator context is a different authority used only for World Access promotion,
status, and reveal. The UI-owned BFF launcher may read both from the owner-only
bootstrap; neither context may enter browser code, browser storage, URLs,
screenshots, or the startup announcement.

Start the combined target with all three exact contract roots:

```sh
DREAMLAND_CONFIG=/path/to/dreamland.ini \
  .venv/bin/python tools/product_world_access_live.py serve \
  --forge-token-file /path/to/private/world-access-live/forge.token \
  --stage-token-file /path/to/private/world-access-live/product.token \
  --bootstrap-file /path/to/private/world-access-live/bootstrap.json \
  --forge-contract-root contracts/world-forge-api/v0.1.0-rc.4 \
  --product-contract-root contracts/product-api/v0.7.0-rc.4 \
  --world-access-contract-root contracts/world-access-api/v0.1.0-rc.4 \
  --artifact-root /path/to/private/world-access-live/artifacts \
  --authored-profile-path /path/to/state_profiles/example.json \
  --authored-profile-digest sha256:REPLACE_WITH_EXACT_DIGEST \
  --deadline-seconds 120 \
  --max-commands -1 \
  --max-contexts 64 \
  --context-ttl-minutes 60 \
  --admission-duration-minutes 720 \
  --host 127.0.0.1 \
  --forge-port 0 \
  --stage-port 0
```

Contract roots and their exact dependency hashes are checked before settings,
providers, or listeners are constructed. The startup JSON reports safe
listener, model, output, limit, live-identity, and retained-publication
accounting. It does not report paths, profile identities, digests, bearers,
provider keys, Presenter or Operator contexts, grants, or session capabilities.

For human evaluation, `--max-commands -1` disables only the accepted-command
count. Finite concurrent-session, logical-job, worker, command-size,
output-byte/chunk, provider-attempt, TTL, cancellation, deadline, token, spend,
and admission-cutoff safeguards remain enabled. Omitting the flag retains the
12-command default; a positive value supplies a different finite count.

The live BFF sequence is:

1. Generate a World Forge job through the live Forge API and wait for its
   authoritative selected result. Keep its non-authorizing job reference in
   protected BFF state.
2. Read World Access identity and Product capabilities. Require the World
   Access and Product service epochs to match and retain the exact RC4
   dependency identities.
3. Bootstrap a Product attendee context. That same attendee actor reads and
   exchanges the World Access catalog; the backend does not remap it into a
   second authority.
4. Use only the distinct Operator context to promote the selected Forge job.
   Confirm the hidden record is absent from the catalog.
5. Reveal with the exact current access-record ID, then read the mixed authored
   and generated catalog under the one Phase 8B notice.
6. Exchange exact offering, offering-revision, and notice evidence. Keep the
   returned Product launch grant, service epoch, and launch-binding digest in
   protected BFF state.
7. Create and operate the session through the unchanged Product RC4 session
   routes. Accepted generated-attendee commands use the live attendee executor.
8. After ambiguous create-response loss, first replay the identical Product
   request. If current-session recovery is still required, accept correlation
   only when both the service epoch and launch-binding digest match.

A Forge selection or Presenter offering alone remains attendee-ineligible.
Only explicit Operator promotion, reveal, exact catalog evidence, and World
Access exchange can provision a generated attendee launch grant. Product RC4's
own attendee option and exchange routes remain authored-only.

### Retained-Lifetime Publication Accounting

The Phase 8B process accepts at most 16 generated publications in one service
epoch. A prepared or committed generated binding consumes this retained-
lifetime quota; expiry removes it from visible catalog results but does not
reclaim its registry offering or revision. This preserves issued launch grants,
exact exchange replay, and changed-body conflict protection. Startup reports
safe `capacity`, `used`, and `remaining` values and states that restart is
required after exhaustion. Restart rotates the service epoch and invalidates
old authority rather than silently recycling it.

Promotion also caps the access-record lifetime to the smaller of the configured
30-minute access TTL and the selected Forge record's remaining lifetime.
Catalog offering and grant lifetimes are further bounded by access truth,
attendee admission, and their existing finite policies. This retained-
lifetime design is appropriate for a bounded development run; withdrawal,
pruning, and generic long-running administration remain later work.

### Isolation And Limits

Importing the access, catalog, attendee, registry, contract, or preview modules
starts nothing. The preview is constructed only by its explicit command and
uses finite actor, promotion, access-record, offering, catalog, grant,
idempotency, session, journal, output, TTL, and tombstone capacities. It starts
no provider and makes no model call.

Normal `python -m dreamland`, SSH, admin, MCP, state-profile, logging,
EnrichmentAgent, and ordinary command paths do not import or construct Phase 8A
composition. When World Access and the Product API are unused, they add no
listener, worker, timer, provider reservation, session bookkeeping, catalog
read, or runtime-operation cost.

Neither Phase 8A nor Phase 8B enables `public_attendee_sessions` or any other
Product capability. Phase 8B adds live Forge and attendee provider composition
only to its explicit development process. It does not provide internet-facing
browser security, durable restart recovery, catalog administration, scheduled
reveal, canary/rollback control, conference load evidence, provider/deployment
isolation, or presentation rehearsal. On 2026-08-30 a bounded backend smoke
used the configured providers to select one live Forge revision, preserve
hidden-before-reveal truth, expose the mixed catalog, replay exchange and
Product create, preserve launch-binding correlation, and complete one generated
attendee command with 12 unclipped output bytes. Shutdown returned 130 after
the deliberate SIGINT and logged neither a traceback nor the generator race.
This is not the broader browser, repeated-command, focused-cancellation, load,
or rehearsal evaluation. Later Phase 8 checkpoints retain independent
capability, deployment, canary, rollback, load, and rehearsal gates.

## Maintenance

Update this document with `src/dreamland/product/generated_access.py`,
`src/dreamland/product/attendee_catalog.py`, the generated opt-in seams in
`src/dreamland/product/attendee.py` and
`src/dreamland/product/launch_selection.py`,
`tools/product_world_access_contract.py`,
`tools/product_world_access_preview.py`,
`tools/product_world_access_live_contract.py`,
`tools/product_world_access_live.py`, the immutable World Access RC1 review
artifact, published RC2/RC3 artifacts, live RC4 artifact, decisions 0072
through 0074, and their owning tests whenever promotion/reveal authority,
catalog eligibility, notice binding, session correlation, live composition,
retained publication accounting, idempotency, dependencies, identity, or
normal-runtime isolation changes.
