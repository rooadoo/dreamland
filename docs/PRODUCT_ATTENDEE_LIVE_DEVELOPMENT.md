# Authored Attendee Live Development Target

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, and local evaluators
Purpose: explain how to run the capability-disabled provider-backed authored
attendee target and evaluate the UI-owned `/session` flow without changing
ordinary DREAMLAND runtime behavior, including the separate Phase 8B generated-
world opt-in composition.

## Overview

Phase 7D adds the Phase 7C attendee facade to the existing live Forge and
Presenter Stage composition. One explicitly started process exposes the World
Forge listener and one private Product listener. The Product listener retains
the authored/Forge Presenter terminal and X-ray flow and adds a real
model-backed attendee session for the exactly pinned authored revision.

```text
pinned authored revision -> private attendee selection -> isolated live session
                       \-> private Presenter Stage terminal/X-ray
live Forge selection -----------------------------------/
```

The Product contract identity is `v0.7.0-rc.4`; World Forge remains pinned to
`v0.1.0-rc.4`. RC4 preserves every RC3 route and request/response shape and
changes only the development identity from deterministic attendee execution to
provider-backed attendee command execution.

Both listeners are loopback-only. Only an accepted Forge job, Presenter Stage
command, or attendee command can invoke a configured provider. Attendee
bootstrap, option listing, notice-bound exchange, create, reads, recovery,
close, and X-ray are provider-free. All five production capabilities remain
disabled, and importing the tool starts nothing.

## Prepare The Authored Revision And Credentials

Choose one trusted world-level state profile and calculate its immutable,
metadata-free digest:

```sh
.venv/bin/python tools/product_attendee_live.py digest-authored \
  --profile-path /path/to/state_profiles/example.json
```

Create distinct owner-only files for the World Forge bearer, Product bearer,
and private Presenter bootstrap:

```sh
.venv/bin/python tools/product_attendee_live.py init \
  --forge-token-file /path/to/private/attendee-live/forge.token \
  --stage-token-file /path/to/private/attendee-live/product.token \
  --bootstrap-file /path/to/private/attendee-live/bootstrap.json
```

The command refuses to overwrite existing files. Keep all three files and all
authority returned by the private API on the server side of the UI BFF. Never
place a bearer, actor context, launch grant, session authority, or bootstrap
file in browser code, URLs, browser storage, fixtures, or screenshots.

## Start The Combined Live Target

Use the intended DREAMLAND provider configuration, the exact authored digest,
and a private persistent artifact directory:

```sh
DREAMLAND_CONFIG=/path/to/dreamland.ini \
  .venv/bin/python tools/product_attendee_live.py serve \
  --forge-token-file /path/to/private/attendee-live/forge.token \
  --stage-token-file /path/to/private/attendee-live/product.token \
  --bootstrap-file /path/to/private/attendee-live/bootstrap.json \
  --forge-contract-root contracts/world-forge-api/v0.1.0-rc.4 \
  --contract-root contracts/product-api/v0.7.0-rc.4 \
  --artifact-root /path/to/private/attendee-live/artifacts \
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

The authored digest is trusted startup configuration, never an HTTP input.
Optional authored title and revision-label arguments are bounded display data.
Port `0` requests distinct ephemeral loopback ports; fixed loopback ports are
also accepted.

Startup first validates the exact RC4 Product and RC4 World Forge artifacts,
then pins and imports the authored revision before constructing providers or
listeners. The first JSON line reports the bound addresses, contract and
executor identities, resolved models, finite maximum output-token settings,
Presenter and attendee `command_reserved_tokens` and `token_budget` values,
effective command limit, output bounds, admission cutoff, and finite
capacities. It does not report paths, profile names, digests, bearers, API keys,
actor contexts, grants, session capabilities, or provider secrets.

Omitting `--max-commands` keeps the default limit of 12 accepted commands per
session. A positive value selects another finite limit. `-1` disables only that
per-session count; the process job ledger, session and worker capacities,
command size, output bytes/chunks, one-attempt provider policy, command and
cancellation deadlines, TTLs, token reservation, and spend reservation remain
finite.

Both command executors require a finite positive configured agent
`max_output_tokens`. Blank or zero configuration is unbounded and fails before
either provider or listener is constructed. Each command reservation is
derived independently as `max(8,192, max_output_tokens)`. The Presenter token
budget is that reservation times its existing 1,024 logical-job allocation;
the attendee `STANDARD` token allocation is its reservation times the existing
64 logical-job capacity. The shared total is the sum of those protected
allocations. Startup exposes all four values so an operator can verify that
neither provider can dispatch under a smaller token reservation.

## BFF Evaluation Flow

Pin the BFF's private Product client to
`contracts/product-api/v0.7.0-rc.4/generated/` and keep the Product bearer and
every returned authority server-side.

1. Read Product capabilities and retain the current service epoch. The five
   capabilities remain disabled; this development endpoint is selected by
   explicit configuration, not inferred from capability enablement.
2. Bootstrap the BFF's high-entropy gateway context with
   `POST /product/v1/attendee/contexts`. Retain the returned attendee actor
   context in the BFF's protected browser-session record.
3. List `GET /product/v1/attendee/launch-options`. The list contains only the
   exactly pinned authored revision and its notice version. It exposes no
   source path, raw profile, Forge option, or authority.
4. Present that exact notice in the UI, then exchange the opaque selection and
   notice version through `POST /product/v1/attendee/launch-selections` using a
   fresh idempotency key. Retain the returned launch grant server-side.
5. Use the unchanged product-session create and observation routes with the
   attendee actor. Accepted commands now dispatch the configured live command
   agent; transcript, event, result, and terminal recovery retain the RC3 wire
   contract.
6. Use the same Product listener's Presenter routes and the World Forge
   listener when evaluating the existing authored/Forge Presenter terminal and
   X-ray flow. Presenter and attendee actors, grants, current bindings,
   sessions, mutable state, journals, and command checkpoints remain separate.

The BFF should replay only an identical idempotency key and semantic request
when a private mutation response is lost. It should reconcile through current,
snapshot, result, and event reads according to the generated client and
contract metadata rather than infer an outcome or mint authority locally.

## Phase 8B Generated-World Opt-In

`tools/product_attendee_live.py` remains the authored-only Phase 7D target.
Phase 8B does not silently change that command. The separate
`tools/product_world_access_live.py` target calls its constructor-disabled
generated-world seams with explicit eligibility keys, larger finite revision
and offering capacities, and a 16-publication service-epoch budget. It then
attaches the authoritative live Forge source and the World Access promotion,
reveal, catalog, and exchange services before either listener accepts work.

World Access `v0.1.0-rc.4` pins this same Product `v0.7.0-rc.4` live
identity and World Forge `v0.1.0-rc.4`. Product's three attendee-facade routes
remain authored-only. A generated revision can reach the unchanged Product
session routes only through the separate sequence:

```text
authoritative selected Forge record
  -> distinct Operator promotion
  -> hidden access truth
  -> explicit reveal and retained catalog publication
  -> exact attendee catalog/notice exchange
  -> ordinary Product launch grant and session create
```

The Product attendee actor, launch registry, core, and World Access catalog
share one service epoch. The Presenter context used by Forge and Presenter
Stage is distinct from the Operator context used for promotion and reveal.
Neither is announced or returned to browser code. World Access exchange retains
the RC3 recovery contract: its output-only launch-binding digest and epoch must
match Product create replay or current-session truth before a BFF associates a
generated catalog selection with an ambiguously created session.

Generated attendee commands use the same attendee executor, in-memory
checkpointer, `STANDARD` admission partition, forced-off external tracing,
cutoff watcher, cancellation ownership, complete output publication, and
derived token/spend reservations described in this document. Starting with
`--max-commands -1` disables only the accepted-command count and omits that
positive-integer launch limit; it does not relax any other safeguard.

The generated publication budget is retained for the entire service epoch.
Expired entries leave the visible catalog but continue consuming capacity so
issued grants and exact replays remain valid. The safe startup announcement
reports capacity, use, and remaining count; after all 16 publications are
consumed, restart is required and rotates the epoch. See
`docs/PRODUCT_WORLD_ACCESS_DEVELOPMENT.md` for the complete startup command and
BFF/manual evaluation sequence. The published RC4 artifact is bound to exact
implementation commit `d1df607bce702475fa8ed79765263445d0e02c6b`. A bounded
provider-backed backend smoke passed on 2026-08-30: one generated attendee
command completed with unclipped output after the live Forge, promotion,
reveal, catalog, exchange, and Product create sequence. The broader browser,
multiple-command, and focused-cancellation evaluation remains separate.

## Focused Cancellation Retest

Phase 7D must pass one focused live UI cancellation retest before it is merged.
Using the combined target above:

1. Start a model-backed attendee or Presenter command that remains visibly in
   flight long enough to cancel.
2. Send Ctrl-C once through the existing terminal UI, then confirm the cancel
   mutation by reading the unchanged command/result and event surfaces.
3. Confirm that the backend output projection does not advance after
   cancellation wins, the command eventually becomes `interrupted`, reconnect/
   recovery shows that same terminal truth, and an identical cancel retry
   replays its original result.
4. Submit one follow-up command to prove that the command slot, checkpoint, job,
   and worker resources were released normally.
5. Inspect backend logs for the entire interval. There must be no `generator
   already executing`, cross-thread stream-close, concurrent checkpoint, or
   cancellation-callback traceback.

Cancellation remains cooperative at the provider boundary: the logical output
and accepted-state fence closes when cancellation wins, while an already
running provider iterator may take time to return control to its owning worker.
Only that owner closes the iterator and records interrupted graph state. The UI
must not treat a delayed physical provider return as permission to publish late
output or state.

## Isolation And Cutoff

Attendee commands use a separate command-agent/checkpointer instance and the
shared ledger's `STANDARD` partition. The existing Presenter Stage reservation
boundary and job capacity remain protected; its token amount scales only with
the configured finite provider maximum. Attendee saturation cannot consume the
Presenter allocation.
External Langfuse tracing is forced off for attendee command content even when
the local DREAMLAND configuration enables tracing for other explicitly started
development collaborators.

The configured admission close bounds the authored attendee offering, grants,
sessions, and command publication on both UTC and monotonic clocks. Reaching
either boundary rejects new attendee selection and provider work, terminalizes
active attendee sessions, cancels/fences queued or running work, and releases
admission exactly once. A still-valid context can recover the bounded terminal
current-session projection; it cannot restart work. Process shutdown revokes
the private BFF and attendee authority and fences attendee workers before the
shared worker pool is torn down.

Gateway contexts and actor slots are finite and never recycled within a
service epoch. RC4 preserves RC3 bootstrap semantics: during cleanup grace a
still-valid private BFF can consume a new bounded context slot, but the expired
offering produces no launch option, grant, session, or provider work. Restart
rotates the epoch and truthfully loses process-local session state. Generated
Forge revisions remain Presenter-only in this authored target. Only the
separately started Phase 8B World Access composition establishes the additional
promotion, reveal, catalog, and attendee-eligibility truth. This combined
target still has no rollout, measured conference-load, public-edge, or
presentation-readiness claim; the later attendee-only canary does not widen its
evidence.

## Current Limits

This target is appropriate for private development evaluation, not internet
exposure, capability enablement, conference load proof, or production
deployment. It retains in-memory process-local state, one provider attempt per
command, conservative maximum-use accounting, bounded output publication, and
the unfinished production provider/deployment gates. The UI BFF continues to
own browser authentication, HttpOnly cookies, CSRF, Host/Origin checks, public
rate limits, notice presentation, and browser-safe DTOs.

Normal `python -m dreamland`, SSH, admin, MCP, state-profile, logging, and
EnrichmentAgent paths import none of this live composition and incur no
attendee listener, worker, timer, provider, registry, journal, reservation, or
bookkeeping cost.

Phases 9A through 9C-D do not widen this combined target. Their separate provider-free
`tools/product_attendee_security_preview.py` process removes Stage, Forge,
World Access, X-ray, and provider authority so the UI BFF can integrate context
revocation and replay-safe logical admission against Product
`v0.8.0-rc.1`. The separate attendee-only live/supervised targets add the
dedicated provider pool, exact cutoff, fixed-scope rehearsal and canary, and an
opt-in 20-session/five-worker development envelope while keeping Product RC4
immutable and every capability disabled. See
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`.

## Maintenance

Update this document with `tools/product_attendee_live.py`,
`tools/product_world_access_live.py`,
`src/dreamland/product/attendee.py`, the RC4 contract and generator, decisions
0068 through 0071 and 0074, and the owning live/contract tests whenever provider
identity, startup inputs, authority ownership, authored or generated
eligibility, retained publication capacity, cutoff, tracing, cancellation
ownership, wire compatibility, or the Phase 8 boundary changes. Record future
provider-backed smoke evidence only after the live workflow actually passes;
the first bounded Phase 8B backend smoke passed on 2026-08-30.
