# Forge-to-Stage Live Development Target

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, and local evaluators
Purpose: explain how to evaluate an authoritative live World Forge selection
in the existing private Presenter Stage terminal without enabling a production
capability.

## Overview

The Phase 5 development target connects the published World Forge RC4 vertical
slice to the existing model-backed Presenter Stage. A successful live Forge job
commits a private presenter-owned selected-revision record. The handoff service
uses only that record to reopen the exact content-addressed artifact, provision
a narrow Stage launch grant, and create, resume, or explicitly replace the
current private Stage session.

This is enough for a UI development trial with this path:

```text
live Forge selection
  -> verified immutable artifact
  -> private dynamic Presenter Stage
  -> existing terminal and X-ray UI at /present/{reference}
```

The terminal and the bounded accepted-state X-ray projection are live in this
explicit development target. Active Forge-backed snapshots report
`xray_available = true`; terminal predecessors report it false. The current
contract identity is `v0.6.0-rc.3`. It corrects the generated presenter-only X-ray
snapshot and detail reads while every production product capability remains
disabled. This target is integration evidence, not Gate 1, Gate 2, full Phase
4, production deployment, or presentation-readiness evidence.

Normal `python -m dreamland` startup does not import or construct this target.
Importing its protocol-neutral modules creates no provider, worker, listener,
credential, file, session, or runtime registration. Only the explicit command
below constructs the two provider clients and loopback listeners.

## Phase 5.1 Replacement Gate

Published `v0.5.0-rc.1` remains immutable. Evidence already collected against
its initial Forge handoff, exact retry/resume, dynamic terminal route, refresh,
command, transcript, event, and source-identity checkpoints remains useful. Its
canonical successful replacement fixture is internally contradictory, however:
the old snapshot reports a `replaced` lifecycle while retaining active
controls, an available command slot, and non-tombstone recovery. Keep explicit
replacement unavailable in the UI while it is pinned to RC1. The current
generator, validator, and combined target are repinned to RC2; do not pass RC1
as their active contract root.

Phase 5.1 publishes the `v0.5.0-rc.2` release candidate with a
runtime-faithful terminal tombstone and semantic validation. It preserves the
old session's original Forge source and returns a separately identified active
replacement selection. Owning tests also prove old-authority revocation,
accepted-CAS-plus-one control revision, the authoritative terminal event
position, and idempotent replay without a second Stage allocation. The route,
BFF authority, selected-record resolution, World Forge RC4 pin, dynamic-only
provisioning, and failed-cutover behavior do not change.

The correction itself is provider-free: owning service and combined HTTP tests
inject both Forge and Stage collaborators, and handoff or replacement
provisioning calls no model. It adds no normal-runtime import, listener, worker,
reservation, or cost. It also adds no X-ray projection; snapshots continue to
report X-ray unavailable until the separate Phase 6 gate passes.

## Phase 6 X-ray Development Gate

The Phase 6 development slice adds an opt-in `StageXrayPolicy` only to the
combined Forge-to-Stage live harness. It does not change ordinary DREAMLAND,
SSH, admin, MCP, profile-store, or disabled product composition. The projection
is built under the product-session lock from the accepted detached runtime
document and returns only bounded world labels, host identity/purpose/OS
fields, accepted fact statements, aggregate counts, and separately marked
activity. Arbitrary nested state, paths, users, environment, prompts,
reasoning, credentials, provider envelopes, traces, and failure capsules are
not projection inputs.

Every snapshot carries accepted-state revision, an independent X-ray
stream/sequence/cursor, and the observed Stage event position. Details require
the first two coordinates. A mismatch returns `xray_resync_required`; the UI
must discard the detail and obtain a new X-ray snapshot. The Stage event value
is correlation only and is never a detail precondition or X-ray cursor.
Clipping, control marking, and per-section omission counts are explicit.
X-ray failure does not disable Stage snapshot, terminal, command, event, or
transcript operations.

## Private Files

Choose three different server-only paths in a private directory. Initialize
them once:

```sh
.venv/bin/python tools/product_forge_stage_live.py init \
  --forge-token-file /path/to/private/forge-stage/forge.token \
  --stage-token-file /path/to/private/forge-stage/stage.token \
  --bootstrap-file /path/to/private/forge-stage/bootstrap.json
```

The command refuses to overwrite an existing path. Each file is owner-only.
The two token files contain distinct private transport bearers. The bootstrap
contains the one Presenter actor context shared by Forge and Stage so the
handoff can prove that the selected record and Stage authority have the same
owner.

The BFF owns all three values. Never place them in browser code, URLs, browser
storage, logs, screenshots, fixtures, or public configuration. The browser
must receive only the non-authorizing Stage reference through the UI's existing
same-origin session mechanism.

## Start Both Live Services

Select the intended DREAMLAND configuration explicitly and use private,
persistent artifact storage:

```sh
DREAMLAND_CONFIG=/path/to/dreamland.ini \
  .venv/bin/python tools/product_forge_stage_live.py serve \
  --forge-token-file /path/to/private/forge-stage/forge.token \
  --stage-token-file /path/to/private/forge-stage/stage.token \
  --bootstrap-file /path/to/private/forge-stage/bootstrap.json \
  --forge-contract-root contracts/world-forge-api/v0.1.0-rc.4 \
  --contract-root contracts/product-api/v0.6.0-rc.3 \
  --artifact-root /path/to/private/forge-stage/artifacts \
  --deadline-seconds 120 \
  --max-commands -1 \
  --host 127.0.0.1 \
  --forge-port 0 \
  --stage-port 0
```

The first JSON line reports both resolved loopback ports, the exact Forge and
product contract revisions, resolved Forge and Stage models, each configured
maximum-output-token value, the Stage `command_reserved_tokens` and scaled
`token_budget`, the Forge lifecycle deadline, the effective Stage command
limit, output-publication bounds, X-ray schema and bounds, and the disabled
capability count.
It contains no bearer, actor context, launch grant, session capability, API key,
or provider secret. Verify these non-secret values before submitting a build.
Before provider construction or listener creation, startup validates both
contract roots and the product manifest's byte-exact RC4 manifest SHA-256 pin.

The Stage provider maximum must be a finite positive integer. Stage admission
reserves `max(8,192, max_output_tokens)` tokens per accepted command and scales
its 1,024-job Presenter token budget by that reservation. Blank or zero agent
output configuration fails before either live provider or listener is
constructed. A finite value above 8,192 therefore expands the visible Stage
reservation instead of dispatching under the historical floor. This does not
change World Forge's separate provider settings or make the Forge and Stage
budgets a production-wide shared provider boundary.

Port `0` lets the operating system choose independent ports. A fixed explicit
loopback port is also accepted for either listener; the ports must differ.
Non-loopback binding is rejected.

Omitting `--max-commands` preserves the Stage development default of 12
accepted commands. A positive value supplies that limit. `--max-commands -1`
disables only the count guard and omits `max_commands` from the positive-integer
launch-limits map. Concurrency, job identity, command size, output bytes,
output chunks, TTL, cancellation, provider-attempt, token, and spend controls
remain active.

## BFF Evaluation Flow

Use or mirror the generated server-side reference clients under
`contracts/product-api/v0.6.0-rc.3/generated/` and
`contracts/world-forge-api/v0.1.0-rc.4/generated/`. Keep the two bearers and
the shared actor context in the BFF; a BFF in another language must preserve
the same private fields and validation rather than moving authority into the
browser.

1. Read Forge identity with the Forge bearer and Stage capabilities with the
   Stage bearer. Retain both returned service epochs server-side.
2. Use the RC4 recipe, preparation, confirmation/submission, and snapshot
   operations to obtain a terminal live selection. Do not hand off a fixture,
   generated-but-unselected job, digest, artifact path, or catalog identity.
3. Call `launch_selected_forge_revision(...)` with the two epochs, shared actor
   context, a fresh idempotency key, and only the non-authorizing Forge job
   reference. The private route is
   `POST /product/v1/stage/forge-handoffs`.
4. Retain the returned Stage session capability in the BFF. Give browser code
   only the returned Stage reference, encode it as a route segment, and open
   the existing `/present/{reference}` terminal view.
5. Run commands through the existing Stage command, result, event, transcript,
   and snapshot operations. A refresh must resolve the same reference rather
   than launch a fallback world.
6. Confirm `snapshot.stage.source` matches the handoff result's exact selected
   record, immutable Forge digest, Forge job ID, and Forge reference. Confirm
   `snapshot.starting_revision.revision_id` matches the selected revision.
7. Call `stage_xray_snapshot(...)`, render its hosts and accepted facts, then
   call `stage_xray_detail(...)` with the returned accepted revision and X-ray
   stream/sequence. Never derive accepted facts from terminal output. On
   `xray_resync_required`, discard the detail and refresh the X-ray snapshot.

An exact same-key/body handoff retry returns the same logical Stage session.
Reusing the key with a changed Forge reference is an idempotency conflict.
The Forge expected-epoch header is semantic request evidence too; after Forge
epoch reconciliation, do not reuse a claimed key with the new epoch.
Ordinary launch of a different selection does not replace the current Stage.
The deliberate replacement path uses
`replace_stage_from_selected_forge_revision(...)`, current session authority,
and the exact expected control revision at
`POST /product/v1/stage/sessions/{reference}/forge-replacement`. Provisioning
and artifact validation occur before atomic cutover, so a failed replacement
leaves the existing Stage usable. Do not expose this action from the RC1
contract. With the UI pinned to RC2 or the additive X-ray RC1 and strict
decoder coverage restored, expose it only as a separate presenter-confirmed
action; ordinary
handoff of a different selection must continue to return
`current_session_exists`.

The RC3 generated client uses the explicit
`/product/v1/stage/sessions/{reference}/xray` route family. Its detail method
passes all three X-ray preconditions and rejects a response whose entity does
not exactly match the requested entity. Both generated validators enforce the
strict nested projection objects, including safe-text bounds and flags, kind/ID
agreement, required fields, and forbidden additive raw state. Optional empty
display fields are omitted; required host labels receive a safe fallback; facts
without a nonempty display statement are counted as omitted. It also validates
both observed and expected X-ray coordinates within JSON-safe integer bounds
before transport, requires an exact contract identity and valid service epoch
on successful responses, and retains schema-valid epoch-less safe errors.
World and host fact identities are scope-separated; summary and detail records
are inserted atomically with collision-safe omission and integrity checks.

Stop the process with Ctrl+C after evaluation. Both listeners and their owned
worker pools shut down together. Artifact files remain in the explicitly
selected private directory for inspection or later deliberate cleanup.

## Authority, Identity, And Lifetime

The browser-supplied handoff value is only a Forge job locator. At the locked
Forge and Stage authority boundaries, DREAMLAND resolves it to an immutable
selected record owned by the authenticated Presenter. Preview selections,
non-selected states, expired records, wrong actors, stale epochs, and missing
artifacts fail before Stage allocation.

The coordinator reopens the artifact by the selected revision ID and digest;
it never accepts an arbitrary path or profile name and never consults or
changes the ordinary state-profile store. Dynamic Stage starts with no static
revision or launch-grant fallback. It validates the exact artifact again,
creates a narrow Presenter grant for create/switch, and records the Forge
identity in `stage.source`.

The selected record and derived launch grant expire one hour after Forge
selection. Although the Stage lifecycle policy permits a two-hour absolute
TTL, grant expiry caps the effective session lifetime at the remaining part of
that one-hour selection window. The 30-minute idle TTL can end the session
earlier. The startup announcement therefore reports a 3,600-second maximum,
not a promise that every session receives a fresh hour at launch.

The Forge artifact digest is SHA-256 over its canonical UTF-8 JSON file bytes,
including the terminating newline. Product lifecycle also records its own
SHA-256 over the newline-free canonical revision document. Those hashes have
different domains and are expected to differ. `stage.source.immutable_digest`
is always the authoritative newline-inclusive Forge identity; the lifecycle
hash remains the existing session snapshot integrity value.

## Provider And Cost Boundary

Initialization, startup, identity/capability reads, artifact handoff, session
resume, and replacement provisioning make no provider call. Submitting a live
Forge build invokes one configured Spiritguide candidate attempt. Each
accepted Stage command invokes the configured command agent with one
cooperative attempt. The startup announcement identifies both resolved models
and output-token caps without exposing credentials.

The Stage ledger retains finite concurrency, token, and spend reservations and
settles each dispatched command conservatively at its full reservation. The
Forge lifecycle deadline fences late selection but does not forcibly terminate
an in-flight provider request. Forge and Stage do not yet share the production
provider-budget coordinator required by the agreed gate. Durable Forge jobs,
hard provider-call termination, bounded A/B/C attempts, repair, fixed
multi-attempt selection, exact provider usage, dedicated deployment
credentials, and non-overcommitting account-wide capacity remain unfinished.
Those gaps are why all capabilities remain disabled.

## Verification

Run the provider-free owning suites and validate both exact contract roots:

```sh
.venv/bin/python -m unittest \
  tests.test_product_forge \
  tests.test_product_forge_artifacts \
  tests.test_product_forge_runtime \
  tests.test_product_forge_stage \
  tests.test_product_stage

.venv/bin/python -m unittest \
  tests.test_product_forge_stage_contract \
  tests.test_product_xray_contract \
  tests.test_product_forge_stage_live

.venv/bin/python tools/product_forge_contract.py validate \
  contracts/world-forge-api/v0.1.0-rc.4
.venv/bin/python tools/product_forge_contract.py client-smoke \
  contracts/world-forge-api/v0.1.0-rc.4
.venv/bin/python tools/product_xray_contract.py validate \
  contracts/product-api/v0.6.0-rc.3
.venv/bin/python tools/product_xray_contract.py client-smoke \
  contracts/product-api/v0.6.0-rc.3
```

These suites inject provider-free Forge and command collaborators. They do not
spend provider capacity. The manual two-listener workflow above is the current
live evaluation path. RC1 immutability is a byte-exact tree-digest regression,
not a second accepted root for the current X-ray validator.

Published RC2 acceptance additionally requires the complete
previous-snapshot terminal semantics, old-authority revocation, control and
event-coordinate advancement, exact old/new Forge source identity,
same-cutover idempotent replay, and contradictory-snapshot rejection.

Return the backend implementation commit, X-ray RC3 contract identity, manifest
and OpenAPI digests, and exact command results to the UI team before it enables
X-ray. No opt-in live-provider test is required: the combined HTTP acceptance
uses injected provider-free collaborators, while the manual command above is
the evaluator's real-provider path.

## Maintenance

Keep this document aligned with `tools/product_forge_stage_live.py`, decision
0070, `src/dreamland/product/forge.py`, `forge_artifacts.py`, `forge_runtime.py`,
`forge_stage.py`, `stage.py`, the RC4 Forge contract, immutable
`v0.5.0-rc.1`, the corrective `v0.5.0-rc.2` release candidate, and their
additive `v0.6.0-rc.3` X-ray release candidate, and their owning tests.
Update it before changing credential ownership, artifact identity,
selected-record or grant lifetime, dynamic Stage provisioning, replacement UI
gating, provider-call boundaries, capability status, or the X-ray allowlist and
coordinate boundary.
