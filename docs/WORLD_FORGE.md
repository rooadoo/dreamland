# World Forge

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, and coding agents
Purpose: document the separately versioned World Forge predecessor and its implementation checkpoints.

Current source-release artifact paths are listed in [the contract inventory](../contracts/README.md).
Superseded snapshots discussed in historical phase notes may be omitted.

## Overview

World Forge turns a small, confirmed audience seed into an immutable validated
world revision. It is a predecessor to Forge-to-Stage handoff, not a sixth
session capability. A Forge fixture, generated-but-unselected candidate, or
ordinary state-profile name is not Stage launch authority.

Phase 4 includes two explicit integration targets. The provider-free RC2
preview remains the deterministic compatibility and recovery target. The RC4
headless vertical slice adds one real Spiritguide attempt, independent profile
validation, content-addressed immutable storage, and a presentation-safe
selection receipt. A trusted live selection now also commits the private
selected record consumed by the separate Phase 5 development handoff. Neither
target is imported or enabled by normal DREAMLAND startup.

Phase 5.1 corrects only the product contract's projection and validation of the
old Stage session after explicit replacement. It does not change World Forge
RC4, the selected-record authority, artifact identity, receipt, deadline, or
provider boundary. Corrective product RC2 continues to pin the exact published
RC4 manifest.

## Seed Preparation And Confirmation

`src/dreamland/product/forge.py` owns:

- the eight ordered BSides recipe fields and their exact UI-agreed identifiers,
  kinds, and limits;
- exact, untrimmed answer preservation and deterministic sentence rendering;
- an unconfirmed, expiring, presenter-bound preparation returned before the
  presenter reviews or confirms anything;
- atomic confirm-and-submit using server-received zoned confirmation evidence
  and a canonical SHA-256 seed identity;
- deterministic field-level validation before any provider boundary, including
  control/format characters and obvious email, URL, IP-address, credential, or
  secret-shaped input;
- strict document round-trip and hash verification with no unknown fields;
- a bounded presenter-owned job service with epoch checks, idempotency,
  capacity rejection replay, status/event recovery, cancellation, and
  provider-free fixture transitions; and
- an immutable internal selected-revision record minted only for an explicit
  live source and trusted selected receipt, with presenter, epoch, job,
  revision/digest, policy, independent expiry, source, and safe receipt
  correlation.

The browser does not provide confirmation time and cannot submit different
content under the reviewed preparation reference. Editing answers requires a
new preparation. Exact confirm-and-submit replay is checked before preparation
consumption, while a new key against a consumed preparation fails safely.

The lifecycle names are `accepted`, `queued`, `building`, `generated`,
`validating`, `selected`, `failed`, `cancelled`, and `deadline_exceeded`.
Selected, failed, cancelled, and deadline-exceeded records are terminal. The
fixture service never returns launch or publication authority.

Snapshot and event positions independently carry service epoch, opaque stream
ID, last event ID, numeric sequence, and opaque cursor. A cursor is valid only
for its stream and epoch. Events require authoritative snapshot reconciliation;
missing history returns `resync_required` rather than inventing continuation.
Service-epoch change reports the in-memory development build lost with retry
`none` and never redispatches uncertain old work.

Terminal cancellation and replay-ledger exhaustion also return retry `none`.
The latter cannot recover within the current in-memory service epoch because
idempotency records are not pruned. Ordinary job-capacity rejection remains an
exactly replayed `capacity_exhausted` result with retry `same_request`.
Unknown recipes and stale recipe revisions are top-level errors that direct the
consumer back to recipe discovery; answer-specific issues remain field-addressable
under `validation_failed`.

## Browser-Integration Contract And Target

The separate unfrozen kit lives at
`contracts/world-forge-api/v0.1.0-rc.2/`. It includes OpenAPI, a standalone
event schema, manifest digests, 39 backend fixtures, and a generated server-only
TypeScript client. The operations cover identity, recipe discovery, preparation,
confirm-and-submit, snapshot, events, and cancellation. No schema contains
Stage launch, publication, catalog, canary, attendee, or participant authority.
The previously published `v0.1.0-rc.1` tree remains immutable for reproducible
consumer pins.

RC2 conditionally requires the correct limits for each recipe-field kind,
requires bounded safe messages for unsuccessful terminal outcomes, binds
receipt presence to `selected`, binds cancellation availability and the
terminal flag to lifecycle state, and fixes preview provenance to the projection
source. Projection objects remain additive while unknown lifecycle and
provenance enums fail closed. Every safe-error fixture records whether it is an
HTTP condition or a schema-only defensive case.

Validate the kit:

```sh
.venv/bin/python tools/product_forge_contract.py validate \
  contracts/world-forge-api/v0.1.0-rc.2
.venv/bin/python tools/product_forge_contract.py client-smoke \
  contracts/world-forge-api/v0.1.0-rc.2
```

Create private files once, then start the target:

```sh
.venv/bin/python tools/product_forge_preview.py init \
  --token-file /path/to/forge-preview.token \
  --bootstrap-file /path/to/forge-preview-bootstrap.json

.venv/bin/python tools/product_forge_preview.py serve \
  --token-file /path/to/forge-preview.token \
  --bootstrap-file /path/to/forge-preview-bootstrap.json \
  --contract-root contracts/world-forge-api/v0.1.0-rc.2 \
  --port 0
```

Both files are exclusive-create, owner-only, regular, and no-follow. The token
file contains the private transport bearer. The bootstrap document contains
only `actor_context`. After bearer authentication the BFF calls the identity
operation to learn the current service epoch; neither private value enters the
browser.

The default `happy` scenario advances through all successful states. `failure`,
`deadline`, `capacity`, `idempotency-capacity`, `preparation-capacity`,
`preparation-expiry`, `cursor-loss`, `epoch-loss`, `submit-response-lost`, and
`withheld-event` provide deterministic recovery and negative-boundary checks.
The deadline scenario waits for the same advertised deadline used by the
projection, and the service rejects an early deadline transition.
`--step-delay-ms` controls manual observation timing. `--once` processes one
request synchronously before exit.

Every result identifies itself as
`provider_free_integration_preview`, reports provider calls false and live
Forge evidence false, and stops at selected or an unsuccessful terminal state.
The selected receipt hashes a deterministic fixture revision and never mints
Stage or publication authority.

## Headless Live Vertical Slice

The corrective contract at `contracts/world-forge-api/v0.1.0-rc.4/` accepts both
the RC2 preview provenance and the exact
`single_attempt_live_vertical_slice` provenance. Its additional canonical
selected fixture proves the live source, compiler revision, validator revision,
immutable SHA-256 revision identity, and absence of human-selection claims.
RC1 through RC3 remain byte-for-byte published artifacts and the provider-free
preview target continues to report RC2. RC4 supersedes RC3 for live UI
integration: identity, recipes, preparation, submission, snapshots, event
pages, cancellation results, and receipts all project one invariant source.

`src/dreamland/product/forge_artifacts.py` deterministically compiles the
confirmed typed seed into one versioned Spiritguide instruction. It validates
the returned world profile independently of the provider: world-level shape,
entry/active host references, executable starting shell, bounded canonical
bytes, host and materialization counts, absence of temporary scaffold residue,
and synthetic profile-to-session boot must all pass. Only then may
`ImmutableForgeStore` create or reuse the owner-only, content-addressed revision
file. Mutable profile names, timestamps, and session labels do not affect the
revision digest.

`tools/product_forge_live.py` is the explicit loopback-only live entry point.
It permits one job and exactly one provider attempt. The lifecycle deadline and
cancellation state fence every late worker result, so late work cannot select a
revision or attach a receipt. Selection checks the authoritative deadline under
the same service lock as the terminal transition; at or after the deadline it
commits `deadline_exceeded`, independent of watcher scheduling. The provider
call itself is not forcibly terminated by this checkpoint; it may finish after the lifecycle deadline and
leave an unselected validated content-addressed artifact. Hard provider
wall-clock cancellation, durable jobs, A/B/C attempts, repair, and shared spend
reservation remain later Phase 4 work.

When a trusted result wins that same selection fence, the service atomically
commits a private presenter-owned record for the exact revision. Default
provider-free selection, explicit non-live sources, preview transitions under a
live source, malformed receipts, every non-selected state, deadline loss,
cancellation, wrong authority, stale epoch, and expiry cannot yield usable
handoff authority. The record is intentionally absent from every existing RC2
and RC4 wire projection; only the trusted in-process Phase 5 coordinator may
resolve it.

Create private transport/bootstrap files once:

```sh
.venv/bin/python tools/product_forge_live.py init \
  --token-file /path/to/forge-live.token \
  --bootstrap-file /path/to/forge-live-bootstrap.json
```

Then explicitly start the live process with configured provider credentials:

```sh
DREAMLAND_CONFIG=/path/to/dreamland.ini \
  .venv/bin/python tools/product_forge_live.py serve \
  --token-file /path/to/forge-live.token \
  --bootstrap-file /path/to/forge-live-bootstrap.json \
  --contract-root contracts/world-forge-api/v0.1.0-rc.4 \
  --artifact-root /path/to/private/forge-artifacts \
  --deadline-seconds 120 \
  --port 0
```

The startup announcement reports only the contract revision, loopback address,
resolved model, maximum output tokens, lifecycle deadline, and non-secret
source identity. Lifecycle/failure logs omit seed text, provider response prose,
credentials, and exception messages; the live generator suppresses the shared
detached-profile request log that would otherwise contain its compiled
instruction. The bearer and actor context remain BFF-only. As with RC2, the
browser first reads identity and uses the returned service epoch for later
requests.

## Isolation And Cost

Importing the modules creates no provider, worker, timer, listener, file, state
profile, session, or runtime registration. Normal DREAMLAND startup does not
import or construct World Forge. Only explicit preview or live-tool startup
creates a bounded loopback server. Only live job submission invokes the
configured provider.

World Forge does not alter the process-global default state profile and does
not enter SSH, admin, MCP, catalog, participant, or ordinary EnrichmentAgent
paths. The separately started Phase 5 development composition may consume its
private selection into a dynamic Stage without joining those ordinary paths.
No product or session capability is enabled.

## Remaining Phase 4 Checkpoints

Later Phase 4 checkpoints add:

- durable jobs and explicit service-epoch recovery without uncertain-job
  redispatch;
- bounded A/B/C provider attempts, retry visibility, and constrained repair;
- fixed A-before-B-before-C selection at the declared deadline;
- shared provider capacity, token, and spend enforcement that preserves hard
  Presenter Stage reservations.

The Phase 5 development handoff now consumes only the authoritative selected
record and exact content-addressed artifact. It starts a dynamic-only private
Presenter Stage with no static fallback and preserves the exact Forge identity
in `stage.source`. Detailed startup and BFF evaluation instructions are in
`docs/FORGE_STAGE_LIVE_DEVELOPMENT.md`.

Phase 5.1 acceptance is provider-free and exercises the existing selected
record through injected Forge and Stage collaborators. It adds no Forge job,
attempt, model call, artifact, listener, worker, reservation, or normal-runtime
composition. Explicit Stage replacement remains gated on UI consumption of
published corrective product RC2; this does not reopen or extend Phase 4.

This does not declare Phase 4 complete. Durable jobs, hard provider-call
termination, bounded A/B/C attempts, repair, fixed multi-attempt selection, and
shared Forge/Stage provider-budget enforcement remain prerequisites for live
capability enablement. World Forge RC4, immutable product RC1, and the
corrective `v0.5.0-rc.2` release candidate therefore continue to report all
product capabilities disabled. Phase 6 Stage X-ray remains outside World Forge
and Phase 5.1.

## Maintenance

Update this document whenever the seed schema, recipe, validation envelope,
job lifecycle, durability, attempt topology, selection rule, immutable storage,
receipt, contract revision, private bootstrap, or preview scenarios change.
Keep `docs/PRODUCT_SESSION_API.md`, `docs/FORGE_STAGE_LIVE_DEVELOPMENT.md`,
`docs/ARCHITECTURE.md`, `ROADMAP.md`, and the relevant decision records aligned.
