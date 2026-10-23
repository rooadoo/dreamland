# Private Product Session API

Status: Active
Audience: maintainers, UI BFF developers, DREAMLAND operators, and coding agents
Purpose: document the implemented product integration foundation, lifecycle,
shared core, Presenter Stage, Forge-to-Stage, X-ray, and source-neutral launch
selection preview/live targets, authored attendee integration preview, the
provider-backed authored attendee development target, the separate
provider-free generated-world access checkpoint, the live World Access
composition, the private attendee security boundary, the attendee-dedicated
live provider boundary, and the agreed
requirements that later product-session phases must satisfy.

Current source-release artifact paths are listed in [the contract inventory](../contracts/README.md).
Superseded snapshots discussed in historical phase notes may be omitted.

## Overview

The private product API is the future BFF-facing entry point for browser
attendee and Presenter Stage sessions. It is distinct from DREAMLAND's
Operator/admin API and from the UI-owned browser-to-BFF contract.

Under the [project model](PROJECT_MODEL.md), this is broader
replaceable-entry-point work toward the future LLM-emulated network. It does
not replace or change the current SSH-host behavior. SSH remains the ordinary
entry point unless a separately gated product capability is implemented and
enabled. `Active` describes the implemented Phase 0 foundation and Phase 1
through Phase 9B integration/development targets that this document must keep
current. It does not describe an enabled production capability.

Phase 0 is a contract and composition foundation, not a working session API.
The ordinary DREAMLAND process exposes no product listener or product route.
The default-off configuration seam, release-candidate artifacts, deterministic
fixtures, validation tools, and explicit integration simulators are
implemented. Phase 1 additionally supplies a protocol-neutral, in-memory
lifecycle and authority implementation. Phase 2 supplies an explicitly
constructed protocol-neutral command core, product-only admission and
enrichment coordination, an isolated runtime-adapter seam, and a deterministic
no-provider loopback preview. Phase 3 adds an explicitly constructed
Presenter-only Stage facade, exact selection-bound resume, explicit
replacement, bounded display metadata, and hard reserved Stage admission
inside the product ledger. Phase 3D separately permits an explicitly started
loopback target to construct the configured provider for local UI evaluation.
Phase 4 adds the separate World Forge predecessor, and Phase 5 now connects one
authoritative live selection to an exact dynamic Stage in a combined
development target. Phase 5.1 implements the required contract-correction
closeout: published RC1 remains immutable and useful for launch, resume, and
ordinary terminal integration, while explicit replacement remains unavailable
to the UI until the published runtime-faithful RC2 terminal snapshot is
consumed.
Phase 6 adds the opt-in dual-position display-safe X-ray snapshot/detail slice
to the combined development target without changing normal runtime.
Phase 7A adds a private Presenter-only source-neutral launch-selection
checkpoint. It lets the UI list and exchange safe options backed by either a
configured authored snapshot or an authoritative selected Forge revision,
then use the resulting private grant with the existing dynamic Stage. Phase 7B
adds the explicitly started live provider-backed composition for one pinned
authored snapshot and freshly selected Forge revisions, using the same private
selection and Stage terminal/X-ray surfaces. Phase 7C adds a provider-free,
authored-only attendee authority and HTTP integration checkpoint for the
UI-owned `/session` BFF while leaving the production capability disabled.
Phase 7D adds its explicitly started live-agent sibling to the combined
Forge/Presenter process without changing the RC3 wire semantics or enabling a
capability. Phase 8A adds a separately versioned, provider-free promotion,
reveal, attendee catalog, and Product-compatible launch-selection checkpoint.
It does not change the authored-only Product routes or make its deterministic
selected artifact live Forge evidence.
Phase 8B adds an explicitly started loopback composition for live Forge,
Operator promotion and reveal, mixed authored/generated attendee selection,
and provider-backed attendee commands while preserving the RC4 authority and
recovery contracts. Phase 9A adds a separate provider-free attendee-only
private target with context revocation and replay-safe logical request
admission. It remains behind the UI-owned BFF and enables no production
capability. Phase 9B adds its explicitly started live sibling: an attendee-
dedicated, credential-isolated fixed spawn-process provider pool with finite
readiness, dispatch, cancellation, drain, shutdown, IPC, token, and spend
bounds. It preserves Phase 9A authority and recovery, includes no Stage or
Forge authority, and still enables no production capability. Its corrective
RC3 also separates provider-backed privacy identity from Phase 9A's
provider-free notice without changing that wire or authority boundary.

There is still no production product listener, deployment-isolation proof,
enabled live Stage or Forge-handoff capability,
enabled production X-ray capability, or public attendee capability.

The semantic boundary in decision 0050 is active. Transport field names and
schemas remain a release candidate until the UI lands its tracked-document
acknowledgement and both sides record the freeze. Do not describe this document
or `0.1.0-rc.1`, either `0.2.0` release candidate, `0.3.0-rc.1`, or either
`0.4.0` release candidate, `0.5.0-rc.1`, or the corrective
`0.5.0-rc.2`, any `0.6.0`, `0.7.0`, or `0.8.0` release candidate, or any World
Access `0.1.0` release candidate as the frozen v1 contract.

## Implemented Phase 0 Foundation

### Runtime status

`ProductApiSettings` in `src/dreamland/config.py` exposes one setting:

```ini
[product_api]
enabled = false
```

`DREAMLAND_PRODUCT_API_ENABLED` is the environment override. The setting is
independent of `[api] enabled`; enabling the admin API does not enable product
behavior, and enabling product behavior does not grant Operator/admin access.

When `product_api` is absent or disabled, the entry point does not import
`dreamland.product`, call the product composer, or construct any product
runtime component. SSH, admin, MCP, state profiles, logging, the default
EnrichmentAgent, and ordinary provider behavior follow their existing paths.

`src/dreamland/product/composition.py` reserves the
`compose_product_runtime(...)` composition boundary. Phase 0 has no live
product lifecycle to return. Setting `enabled = true` raises the safe
`ProductCompositionUnavailable` startup error before any product listener,
runtime, provider, worker, journal, timer, subscription, or reservation is
created. There are intentionally no product host, port, token, or individual
capability settings yet.

### Capability status

The five exact product-session capabilities are declared independently:

| Capability | Dependency | Phase 0 runtime status |
| --- | --- | --- |
| `product_session_core` | none | disabled; no enabled revision |
| `presenter_stage_terminal` | shared core | disabled; no enabled revision |
| `public_attendee_sessions` | shared core | disabled; no enabled revision |
| `forge_to_stage_handoff` | Stage plus compatible live World Forge | disabled; no enabled revision |
| `stage_xray` | Stage accepted-state model | disabled; no enabled revision |

`product_api` is a master configuration gate, not a sixth capability. Live
World Forge is a separately versioned predecessor, not a session capability.
The generated-world promotion/catalog/attendee-selection path is also separate
from these five capabilities; Phase 8A implements only its provider-free
integration checkpoint.

No successful admin health or capability response, simulator response,
installed Python module, reusable schema component, or UI fixture makes one of
these capabilities live. Consumers fail closed for missing, disabled, unknown,
or incompatible capability evidence.

## Contract Kit

The backend-owned release candidate is:

```text
contracts/product-api/
  README.md
  v0.1.0-rc.1/
    manifest.schema.json
    manifest.json
    openapi.json
    events.schema.json
    fixtures/
      index.json
      canonical-valid/
      forward-compat/
      invalid/
```

The manifest records contract revision `0.1.0-rc.1`,
`exact-release-candidate` compatibility, freeze status
`pending_ui_tracked_doc_acknowledgement`, artifact and fixture-set SHA-256
digests, and all five disabled capability claims.
`backend_commit` and `published_at` remain null in the implementation commit;
a later release-metadata commit fills them with the tested implementation
commit and publication time. Artifact digests cover exact file bytes. The
fixture-set digest covers the exact canonical `fixtures/index.json` bytes; that
index also records every fixture's class, schema target, expected result, and
individual digest.

In this release candidate, `publication_status = published` means only that
release metadata was attached to a tested, still-unfrozen RC. It does not
override `freeze_status`, make wire names canonical, prove that the UI consumed
the artifacts, or count as schema-freeze or integration evidence. Those claims
remain unavailable until the UI's tracked-document acknowledgement lands and
both sides explicitly record the freeze and resulting integration evidence.

`openapi.json` is OpenAPI 3.1. Its only implemented operation is private
capability discovery:

```text
GET /product/v1/capabilities
```

That operation is currently served only by the manual integration simulator,
not by the DREAMLAND runtime. It requires a private gateway bearer and returns
the contract revision, service epoch, and the exact five capability entries.
Each entry is disabled and has no enabled revision. Reusable, unreferenced
components may capture agreed value shapes for design and generator testing;
they do not make a session, mutation, Stage, Forge, X-ray, or event operation
available. A Phase 0 generated client is guaranteed only for capability
discovery because generators vary in their treatment of unreferenced schemas.

`events.schema.json` uses JSON Schema Draft 2020-12 and defines reusable event
envelope and value primitives. The event schema marks transport unavailable,
there is no event route, and Phase 0 contains no canonical event instance.
Forward-compatibility fixtures exercise the agreed fail-closed distinction
between unknown informational and authoritative event types without claiming a
live stream. AsyncAPI should be added only with a real channel, cursor,
retention, and snapshot-recovery contract.

Canonical-valid fixtures cover only implemented capability discovery and safe
private errors. Forward-compatible fixtures exercise additive consumer data.
Invalid fixtures exercise exact capability membership, impossible Phase 0
enablement, missing enabled revisions, and unsafe or malformed errors. Planned
session examples are not canonical fixtures until their owning operations
exist.

### Validation and publication

Validate the artifact and fixtures with:

```sh
.venv/bin/python tools/product_contract.py validate \
  contracts/product-api/v0.1.0-rc.1
```

Validation checks duplicate JSON keys, JSON metaschemas, OpenAPI structure,
local references, fixture expectations and hashes, exact capability membership,
the all-disabled Phase 0 invariant, safe-error bounds, and manifest digests.
This is a focused repository structural validator, not full external OpenAPI
conformance. It does not replace an independent standards validator, a
third-party generator matrix, or live transport compatibility, authorization,
security, concurrency, or load tests.

After an intentional artifact or fixture edit, regenerate deterministic
digests and validate again:

```sh
.venv/bin/python tools/product_contract.py refresh \
  contracts/product-api/v0.1.0-rc.1
```

Exercise server-side client generation independently of the UI repository:

```sh
.venv/bin/python tools/product_contract.py client-smoke \
  contracts/product-api/v0.1.0-rc.1
```

`client-smoke` generates and compiles the repository's small capability
reference client. It proves that reference path only; it is not evidence that
an external OpenAPI generator, the UI BFF client, or any unimplemented session
operation conforms.

Publish release metadata only after the tested implementation/artifact commit
exists:

```sh
.venv/bin/python tools/product_contract.py publish \
  contracts/product-api/v0.1.0-rc.1 \
  --backend-commit IMPLEMENTATION_COMMIT_40_HEX \
  --published-at RFC3339_TIMESTAMP
```

The generated manifest belongs in a following release-metadata commit because
a commit cannot contain a manifest that truthfully hashes and names itself.
The UI should pin both the recorded implementation commit and the published
artifact digests as integration prerequisites. Pinning alone is not integration
or schema-freeze evidence.

### Explicit integration simulator

Start the loopback-only simulator manually with a private test bearer:

```sh
.venv/bin/python tools/product_contract.py serve \
  contracts/product-api/v0.1.0-rc.1 \
  --token local-integration-token \
  --port 0
```

The tool binds only to loopback and reports the selected ephemeral port. The
caller supplies the private test bearer; it must not be reused as a browser or
production credential.
It is imported by no DREAMLAND runtime module and has no provider, profile,
session, command-agent, admin-service, or product-runtime dependency. It serves
the capability operation; every other product route returns 404.

Scenarios are selected when the process starts and deterministically cover the
all-disabled response, missing capability, incompatible revision, additive
data, bounded response delay, and dropped response. Requests cannot select
faults through a header or query parameter. The simulator is development
infrastructure, may not be composed into a production process, and is never
evidence that a capability or transport is live.

Phase 0 does not simulate an acknowledgement that promises an event and then
withhold that event. There is no mutation acknowledgement or event feed to do
so truthfully. The accepted promised-feed/missing-event recovery fixture moves
to the first phase that implements a mutation acknowledgement and event
transport; that phase must deliver the deterministic withheld-event scenario
alongside its operation/result query and snapshot-reconciliation behavior.

The UI team can use Phase 0 to generate its private server-side capability
client, implement fail-closed capability handling, verify additive-field
behavior, exercise finite request recovery, and prove that private DTOs and
credentials do not enter its browser bundle. Browser cookies, CSRF, Origin,
Host, CORS, cache rules, public throttling, browser deadlines, and browser-safe
copy remain UI/BFF-owned.

## Implemented Phase 1 Integration Preview

Phase 1 implements lifecycle and authority semantics without composing a live
product service. `src/dreamland/product/lifecycle.py` is a protocol-neutral,
in-memory aggregate. The ordinary entry point does not import or construct it,
and `compose_product_runtime()` still fails closed when the product master gate
is explicitly enabled. The only HTTP adapter is the manually invoked,
loopback-only `tools/product_lifecycle_preview.py` development tool.

All five capabilities remain disabled with no enabled revision. In particular,
the presence of lifecycle code and preview routes does not enable
`product_session_core`. A production BFF must continue to fail closed on that
capability and must not use the preview as a fallback.

### Lifecycle and authority

The preview starts with a fixed synthetic integration catalog, actor-context
authorities, and narrow launch grants. A grant binds one actor, authority class,
exact immutable revision, operation, service epoch, expiry, policy, notice, and
safe limits. There is no Phase 1 grant-minting route. The BFF preview adapter
holds the seeded private material server-side; create never accepts an offering
ID, profile name, path, raw state document, or caller-selected digest.

Revision inputs are canonicalized and hashed at service construction. Each
successful create reparses those bytes into isolated session-owned data. It
never reads or changes the process-selected profile and never registers with
the SSH/admin session or state-profile indexes. Allocation performs no model or
provider call.

Each service instance has a fresh high-entropy epoch. Every operation requires
explicit current-epoch evidence, but the epoch itself grants no authority.
Stale current and snapshot operations return `session_lost`; stale create and
close operations return conservative `outcome_unknown`. Stale work is never
rebound, recovered, redispatched, or allocated in the new epoch.

Create and close are idempotent within an epoch. Scope includes stable actor,
authority class, operation family, and key; a canonical semantic-body digest
detects changed-body reuse. Exact replay returns the recorded logical result,
including after the original launch grant was consumed or the session authority
was revoked. Changed-body reuse returns `idempotency_conflict`. Epoch validation
always precedes replay.

The key is not claimed until service authority, the current epoch, actor
context, operation family, bounded key, and canonical semantic body are valid.
Malformed transport or body input and failures before that trusted boundary
remain unclaimed. After the boundary, launch-grant and session-authority denial
and deterministic business conflicts are recorded before returning. An exact
retry replays the safe rejection; changed-body reuse conflicts. The explicit
exception is `capacity_exhausted`: it is a transient pre-work rejection, claims
no key, and permits the same request to be evaluated again after its advertised
delay.

Unrelated deterministic failures use the bounded failed-operation retention
TTL. Records related through an unguessable consumed grant or session authority
remain protected through the session's active lifetime and retained tombstone;
a caller-supplied reference alone cannot extend retention or become an
existence oracle. Terminalization aligns related records to the actual tombstone
deadline. Cleanup cannot make a key silently acquire a different meaning while
that recovery truth remains. Claim identity and rejection records retain
canonical authority digests and safe outcomes, not raw supplied actor, launch,
or session authority. A successful create record retains its newly issued
session authority as part of the bounded original result so a lost response can
be recovered, then removes it with the related tombstone. Decision 0052 records
this claim and retention boundary.

At most one active current session exists for an actor and authority class.
Current lookup never creates. A distinct create while one is active returns
`current_session_exists`. A terminal tombstone may be shown through current
lookup without returning mutation authority. A deliberate later create needs a
fresh launch grant and idempotency key.

Created sessions begin active at control revision 1. Grant lifecycle limits can
only reduce the configured service TTLs. Close and exact-boundary idle or
absolute expiry terminalize once, advance the control revision, revoke session
authority, discard isolated mutable revision data, and retain a bounded safe
tombstone. If cleanup first observes a session after more than one deadline, it
records the earliest deadline and its true timestamp; late observation never
extends retention. Passive reads do not refresh idle expiry. Expiry and
retention cleanup happen lazily at operation boundaries, so no background
timer, sweeper, or worker exists.

The protocol-neutral Phase 1 snapshot contains lifecycle identity, the starting
immutable revision identity and digest, authority class, control revision,
timestamps and expiry, safe limits, and available lifecycle controls. The wire
adapter allowlists the selected-record and immutable-revision identities but
does not expose its internal binding or document digests. Active create/current
responses return session authority beside the snapshot. Authority never appears
inside the snapshot, and a terminal current response contains none. The
snapshot has no raw world or session state, profile path, command slot,
accepted-state coordinate, command result, transcript, event position, X-ray
projection, provider data, trace, prompt, environment, or structured failure.

### Phase 1 contract kit

The current unfrozen lifecycle RC lives at
`contracts/product-api/v0.2.0-rc.2/`. It leaves the published Phase 0 and Phase
1 `rc.1` artifacts unchanged, advertises `servers: []`, and marks lifecycle
operations `integration-preview-only`. `rc.2` changes only the contract
revision and explicit idempotency-policy metadata; operation, request,
response, status, and capability shapes are unchanged. It contains exactly
these private routes:

```text
GET  /product/v1/capabilities
POST /product/v1/sessions
GET  /product/v1/sessions/current
GET  /product/v1/sessions/{reference}
POST /product/v1/sessions/{reference}/close
```

Every route requires the private service bearer. Lifecycle operations also use
explicit epoch and actor-context evidence; create and close require an
idempotency key, create consumes a launch grant, direct snapshot and close use
session authority, and close carries the expected control revision. These are
private BFF-to-DREAMLAND values. They must not be accepted from or returned to
browser JavaScript.

The kit includes canonical, additive-compatibility, fail-closed, and safe-error
fixtures plus a strict validator and bounded server-side reference-client
smoke. It is still an exact release candidate with schema freeze pending the UI
tracked-document acknowledgement. Its fixtures and simulator evidence are
integration-preview evidence only, not a production transport or capability
gate.

Its exact-RC validator has a canonical OpenAPI semantic trust anchor that is
independent of manifest refresh. Intentional schema work must update that pin
together with the validator, generated-client assumptions, fixtures, and tests;
`refresh` cannot silently bless a weakened or expanded contract.

Validate the exact kit and generate the server-side reference client with:

```sh
.venv/bin/python tools/product_lifecycle_contract.py validate \
  contracts/product-api/v0.2.0-rc.2
.venv/bin/python tools/product_lifecycle_contract.py client-smoke \
  contracts/product-api/v0.2.0-rc.2
```

Start the stateful preview explicitly with:

```sh
.venv/bin/python tools/product_lifecycle_preview.py \
  --contract-root contracts/product-api/v0.2.0-rc.2 \
  --token-file /path/to/owner-only-preview.token \
  --port 0
```

The token file must be a regular file owned by the preview process user, grant
no group or other permissions, and contain one bounded visible-ASCII token
line. The loader refuses symbolic links and bounds the read. This keeps the
private service bearer out of process arguments; `--token` remains available
only for disposable local test invocations where command-line disclosure is
acceptable.

The explicit preview bounds admitted connections and applies both per-read and
absolute connection deadlines. Parser failures and unknown HTTP methods use the
same display-safe JSON problem envelope, cache prohibition, and content-type
hardening as routed failures; the stdlib HTML error path is never exposed.
Normal serving remains threaded. `--once` dispatches synchronously and exits
only after the complete response has been flushed and the selected connection
has closed.

The BFF first reads the actual `service_epoch` from capability discovery; the
authority fixtures contain `expected_epoch_source` rather than a static epoch.
The process-start scenarios are `lifecycle-happy`,
`create-response-dropped-after-commit`,
`close-response-dropped-after-commit`, `expires-on-next-read`, and
`epoch-loss`. The last scenario reports the prior non-authorizing epoch in its
startup record and uses the separately indexed rotated-current authority
fixture. Fault selection is never accepted from an HTTP request.

Phase 1 has no mutation acknowledgement that promises an event and no event
transport. The Phase 2 artifact and preview now own the deterministic
lost-command-response and promised-event-withholding recovery scenarios because
Phase 2 is the first phase with both mutation acknowledgement and event
transport.

## Implemented Phase 2 Unfrozen Core Preview

Phase 2 implements the difficult shared-session state machine behind an
explicit construction seam. `ProductSessionCoreService` is a protocol-neutral,
in-memory aggregate; it is not imported or constructed by normal startup.
`compose_product_runtime()` still fails closed, and all five capability entries
remain disabled with no enabled revision. The manually invoked
`tools/product_core_preview.py` is the only Phase 2 HTTP adapter. It is a
deterministic, loopback-only BFF compatibility harness, not a production
transport or fallback.

### Command, publication, and recovery semantics

The backend-owned `product-single-v1` classifier runs before command identity,
slot, journal, event, budget, or execution allocation. It decodes a string or
UTF-8 bytes exactly once, trims only ASCII space and horizontal tab, performs
no Unicode normalization, bounds scalar and byte length, rejects line/NUL
controls, and rejects unsupported unquoted shell process-control syntax. It is
independent of and makes no change to SSH command parsing.

An accepted command receives a stable command ID and the next exact slot
generation, occupies the session's sole foreground slot, records transcript
and event acceptance atomically, and then dispatches through an injected task
boundary. Provider work and detached foreground validation occur outside the
aggregate lock. Each accepted output chunk uses one bounded journal transaction;
once bytes or chunks clip, a stop signal suppresses further publication and
aggregate-lock acquisition even if the executor remains noisy. Terminal
publication rechecks the exact active session, command generation, open fence,
deadline, and base accepted-state revision under one lock. Only a validated
`committed` state effect advances accepted state; a completed guest result whose
effect is invalid or loses the state CAS remains completed with a typed rejected
state effect.

Submit and cancel use epoch-, actor-, authority-, operation-, key-, and
canonical-body-scoped idempotency. Exact replay returns the original logical
result; changed-body reuse conflicts. Deterministic post-claim rejection is
replayable. A transient pre-dispatch capacity rejection rolls back the
reservation and leaves the key reusable. Cancel and terminal publication race
at the same fence: terminal-first returns `already_terminal`; cancel-first
returns the replayable nonterminal `cancel_requested`, prevents later accepted
state publication, and keeps the slot occupied until truthful terminal evidence
arrives. If a deadline must force terminal recovery while a physical worker is
still outstanding, the immutable outcome reports usage as pending; later reap
settles only its accounting projection and never rewrites terminal truth.

The event and transcript journals are separate bounded domains with separate
epoch/stream/sequence cursors. Accepted command/output pairs are constructed
and committed atomically. Cursor eviction returns `snapshot_required`; stale
epoch evidence returns service loss. Authoritative command-result query and an
internally consistent snapshot support response-loss, delayed-event, and
reconnect recovery. Slow readers do not hold publication open or create
unbounded storage.

Reset and switch require exact control CAS and return busy while the foreground
slot is nonterminal. They validate and admit a replacement before one atomic
current-binding cutover; failure preserves the old session and authority.
Successful replacement fences the old incarnation and transfers session
admission without a capacity gap. The per-session command count is carried
across reset and switch, so replacement cannot replenish the actor's command
quota. In-flight replacement claims count globally against idempotency and
terminal-retention capacity, so unrelated mutations cannot consume their
reserved recovery slots. Close and lifecycle expiry fence stale work and
release logical resources exactly once; a cancellation-ignoring physical
worker retains its reservation until it is actually reaped.

### Product-only execution, enrichment, and resources

`runtime_executor.py` defines an inert, injected adapter for a deep-copied
state-profile document, command-generation checkpoint namespace, cooperative
cancel/deadline control, usage measurement, and exact namespace cleanup. It
does not wrap `AgentBackend`, access ordinary registries or failure capsules,
or construct a provider. The real provider collaborator remains future work:
it must claim every attempt through the supplied control, honor product-only
credentials and hard limits, provide exact usage evidence, and prove isolated
checkpoint cleanup before the core capability can be enabled.

`admission.py` accounts product sessions, logical jobs, physical workers,
worst-case attempts, tokens, and spend without touching ordinary SSH or default
EnrichmentAgent accounting. Unknown usage after physical dispatch is charged
conservatively to the full reservation. `enrichment.py` has no scheduler,
listener, timer, or provider; an explicit caller supplies its generator and
invokes it. Generation uses detached state, while compare-and-apply serializes
with the same product aggregate. Accepted document, accepted-state revision,
and canonical `state.accepted` event commit together before an optional
post-commit observer runs.

### Phase 2 contract and loopback target

The unfrozen Phase 2 kit lives at
`contracts/product-api/v0.3.0-rc.1/`. It leaves every prior release-candidate
directory unchanged, advertises `servers: []`, labels every operation
`integration-preview-only`, and keeps all five capabilities disabled. It adds
these private routes to the lifecycle surface:

```text
GET  /product/v1/capabilities
POST /product/v1/sessions
GET  /product/v1/sessions/current
GET  /product/v1/sessions/{reference}
GET  /product/v1/sessions/{reference}/transcript
GET  /product/v1/sessions/{reference}/events
POST /product/v1/sessions/{reference}/commands
GET  /product/v1/sessions/{reference}/commands/{command_id}
POST /product/v1/sessions/{reference}/commands/{command_id}/cancel
POST /product/v1/sessions/{reference}/reset
POST /product/v1/sessions/{reference}/switch
POST /product/v1/sessions/{reference}/close
```

All operations require the private test bearer. Except capability discovery,
they require explicit expected-epoch and actor evidence; session-specific
operations also require session authority. Mutations require an idempotency
key, and their bodies carry the operation's exact slot or control precondition.
The event route returns a bounded JSON page by default and an SSE page when the
caller requests `text/event-stream`; cursor coordinates remain explicit and
never grant authority.

Validate the exact kit and its server-side-only generated reference client:

```sh
.venv/bin/python tools/product_session_contract.py validate \
  contracts/product-api/v0.3.0-rc.1
.venv/bin/python tools/product_session_contract.py client-smoke \
  contracts/product-api/v0.3.0-rc.1
```

Start the no-provider preview explicitly:

```sh
.venv/bin/python tools/product_core_preview.py \
  --contract-root contracts/product-api/v0.3.0-rc.1 \
  --token-file /path/to/owner-only-preview.token \
  --port 0
```

The preview reuses the hardened token-file, connection-admission, parser,
deadline, safe-problem, and synchronous `--once` boundary from Phase 1. Normal
serve mode remains threaded. Duplicate body-framing/media headers are rejected;
event representation negotiation honors media quality and exclusion; and
cursor tuples must be complete and internally consistent. The generated client
omits the canonical zero/null initial cursor rather than serializing it. The
deterministic executor recognizes only its documented special control commands
and otherwise returns deterministic preview output. It performs no provider,
network, or host-command work and cannot be evidence that
`product_session_core` is live.

Faults are selected only at process start. `core-happy` serves the ordinary
flow; `command-accept-response-lost` drops the first post-commit acceptance
response and permits exact same-key replay; and
`promised-terminal-event-withheld` drops the first terminal event response
without deleting canonical journal truth, so result/snapshot recovery and later
gap-free replay remain available. Requests cannot select or modify a scenario.

The kit remains unfrozen pending the UI tracked-document acknowledgement. Live
enablement additionally requires a production private gateway, real product
provider adapter and attempt enforcement, non-overcommitting provider/account
isolation, exact deployment budgets, per-session contention/load evidence for
the remaining bounded aggregate transactions, cost/restart evidence, and the
complete Gate 1 matrix. Until all of those exist, the BFF must continue to fail
closed on `product_session_core = false`.

## Implemented Phase 3 Unfrozen Presenter Stage Preview

Phase 3 adds `PresenterStageService` as an explicitly constructed facade over
the Phase 2 core. It is not imported or constructed by normal startup, and
`compose_product_runtime()` still fails closed. The service accepts only
Presenter actor and launch-grant seeds; attendee and Operator authorities are
not interchangeable with it.

### Exact launch, resume, and replacement

With no current Stage, launch creates the exact immutable revision bound to the
opaque presenter grant. Exact same-key/body retry replays its recorded result.
A fresh ordinary launch resumes the current Stage only when all of the
following still match the originating launch:

- the exact launch-grant credential identity and selected-record identity;
- presenter actor and role;
- immutable revision and semantic launch-binding digest;
- allowed operation set, effective limits, and expiry boundary; and
- policy and notice versions.

Revision equality alone is insufficient. Another selection, including one that
resolves to the same revision, returns replayable `current_session_exists`.
Its optional safe error context contains only the current and requested
selected-record identifiers. It does not expose either launch credential,
session authority, or runtime state.

Changing selection uses the explicit Stage replacement operation with current
session authority and expected control revision. It reuses the shared Phase 2
provision-before-cutover fence: command-busy and stale-CAS failures allocate no
replacement; validation or admission failure leaves the old Stage usable; and
success rotates current authority, terminalizes the old Stage as replaced,
transfers admission count-neutrally, and prevents stale work from publishing.
The target grant can authorize both create and switch so a later ordinary
launch can resume the successfully replaced Stage by the same exact origin.

### Hard Stage admission reservation

`ProductAdmissionLedger` now has `standard` and `presenter_stage` capacity
classes under one hard aggregate. The Stage reservation is configured as
explicit subsets of total active sessions, logical jobs, physical workers,
tokens, and spend. Standard work can consume only the total minus those
subsets. Stage work can consume only its reserved subsets. Therefore standard
or future public saturation cannot borrow the Stage allocation, and Stage
exhaustion remains independently bounded without creating a second ledger that
could overcommit the same provider total.

All reservation fields default to zero. Existing Phase 2 callers continue to
use the standard class and retain their former limits. `PresenterStageService`
requires every Stage reservation dimension to be positive and assigns its
sessions to the Stage class. This proves the protocol-neutral accounting
boundary only. Live enablement still requires deployment evidence that the
real product credentials, provider pool, and account-wide hard limits agree
with it.

### Display-safe Stage metadata

Trusted startup composition supplies one bounded metadata record for every
immutable revision: title, source label, and revision label. The Stage snapshot
adds only that metadata, its existing non-authorizing Stage/session reference,
the `presenter_stage_reserved` admission class, and
`xray_available = false`. The runtime document is never read to construct this
projection. Raw world state, host graphs, facts, prompts, reasoning, provider
envelopes, credentials, internal paths, and other sessions remain excluded.
The dual-position Stage X-ray was deliberately deferred to the separate Phase
6 contract described below.

### Phase 3 contract and loopback target

The additive unfrozen Phase 3 kit lives at
`contracts/product-api/v0.4.0-rc.1/`. It retains the shared-core operations and
adds:

```text
POST /product/v1/stage/sessions
GET  /product/v1/stage/sessions/current
POST /product/v1/stage/sessions/{reference}/replace
```

The Stage launch operation returns either `stage_session_created` or
`stage_session_resumed`. Replacement returns `stage_session_replaced`. Shared
snapshot, command, result, cancel, event, transcript, reset, and close
operations continue to use the Phase 2 coordinates and recovery semantics.
Every Stage operation remains private and non-cacheable; browser cookies,
presenter login, CSRF, Origin, Host, and browser-facing DTOs remain UI/BFF
responsibilities.

Validate the kit and its server-side generated client:

```sh
.venv/bin/python tools/product_stage_contract.py validate \
  contracts/product-api/v0.4.0-rc.1
.venv/bin/python tools/product_stage_contract.py client-smoke \
  contracts/product-api/v0.4.0-rc.1
```

Start the deterministic no-provider Stage target explicitly:

```sh
.venv/bin/python tools/product_stage_preview.py \
  --contract-root contracts/product-api/v0.4.0-rc.1 \
  --token-file /path/to/owner-only-preview.token \
  --port 0
```

`stage-happy` serves the ordinary flow.
`stage-launch-response-lost` drops the first launch response after commit so an
identical retry proves replay without a second allocation.
`command-accept-response-lost` and `promised-terminal-event-withheld` retain the
shared command/reconciliation faults. Scenarios are process-start choices and
cannot be selected by requests.

The kit remains unfrozen, every capability entry remains disabled, and the
preview performs no provider, network, or host-command work. Enabling
`presenter_stage_terminal` still requires Gate 1 plus the production private
gateway, presenter browser authentication, real provider/attempt enforcement,
non-overcommitting deployment isolation, reserved-capacity load evidence,
cutoff/cost/restart evidence, and the complete Gate 2 matrix.

## Implemented Phase 3D Live Development Target

Phase 3D adds a separate, manually invoked way to exercise the Phase 3 Stage
through the configured DREAMLAND command agent and model provider. It exists so
maintainers can evaluate real latency, streamed output, state continuity,
cancellation, and recovery through the UI before the World Forge phase begins.
It does not change the production capability gate.

`src/dreamland/product/live_agent.py` bridges one streaming command-agent
invocation into the existing `ProductRuntimeCommandExecutor`. It claims one
cooperative attempt, forwards only typed output chunks, requires exactly one
terminal result, maps product cancellation into the command agent, and deletes
the exact in-memory LangGraph checkpoint thread after execution. It continues
using detached state-profile documents and never wraps `AgentBackend` or joins
the SSH/admin live-session registry.

The development usage meter settles a dispatched command at its full token and
spend reservation. This safely bounds the product ledger but deliberately does
not claim exact provider cost, SDK retry enforcement, or production attempt
isolation. Those remain Gate 1 work.

Decision 0070 closes a development accounting gap before dispatch. Every live
Stage-bearing composition requires a finite positive configured
`max_output_tokens` and derives `command_reserved_tokens` as the greater of
that value and the 8,192-token conservative floor. Its token budget scales by
the unchanged logical-job capacity. Blank or zero configuration fails before a
provider or listener is constructed. The startup announcement reports the
effective maximum, reservation, and budget so an operator can audit the bound.

The additive kit lives at `contracts/product-api/v0.4.0-rc.2/`. Its routes and
payload semantics remain the same 15-operation Presenter Stage surface from
`rc.1`. Its response preview identity instead declares:

```json
{
  "test_only": true,
  "integration_only": true,
  "executor": "development-live-langgraph-v1",
  "provider_calls": true,
  "live_capability_evidence": false
}
```

The generated client rejects deterministic-preview responses when pinned to
this kit, just as the `rc.1` client rejects a live-development identity. Every
capability remains disabled.

`tools/product_stage_live.py init` creates an owner-only BFF bearer and
Presenter bootstrap file without printing their contents. The `serve` command
loads an immutable state profile, explicitly constructs the agent/provider and
Stage service, validates the `rc.2` kit, and binds only to loopback. It starts
no normal DREAMLAND listener and makes no provider call before a command is
accepted. Detailed startup and evaluation steps are in
`docs/PRODUCT_STAGE_LIVE_DEVELOPMENT.md`.

The development launcher retains a 12-command default, accepts positive
overrides, and treats `--max-commands -1` as an internal request to disable only
the accepted-command guard. Unlimited launches omit `max_commands` from the
positive-integer API limits map. Independent finite process job retention,
token/spend budgets, concurrency, TTL, output, and provider-attempt limits stay
enabled. This launcher-only behavior does not change the published `rc.2`
schema or bytes.

The live agent coalesces provider pieces into deterministic publication units:
newline-terminated lines flush immediately, unterminated text flushes at 4 KiB,
and stdout/stderr switches preserve order. The live chunk allowance cannot be
reached before the existing 65,536-byte command ceiling. Exact use of a core
output boundary is not clipping; `output_clipped` records actual omission. This
launcher-only publication policy does not change the `rc.2` schema or bytes.

Phase 3D is complete backend evidence for a local UI development trial. It is
not a claim that browser authentication, production transport, exact provider
accounting, capacity isolation, Gate 1, Gate 2, or the presentation path is
complete. The original sequence resumes with Phase 4 World Forge.

## Implemented Phase 4 World Forge Foundation

Phase 4 now has an explicitly constructed protocol-neutral foundation in
`src/dreamland/product/forge.py`. It locks the eight ordered BSides recipe
fields used by the UI adapter, preserves exact confirmed answer text, reproduces
the rendered sentence without a free-form rewrite, validates input before any
provider boundary, and binds the strict document to a canonical SHA-256 seed
identity.

The same module provides a bounded presenter-owned in-memory job service for
backend integration tests. It proves epoch-bound idempotent submission,
changed-body conflict, deterministic capacity-rejection replay, safe status and
cursor recovery, exact cancellation, and monotonic provider-free fixture
states. It is imported and constructed by no normal runtime path and enables no
session capability.

The separately versioned provider-free browser-integration artifact
lives at `contracts/world-forge-api/v0.1.0-rc.2/`, with its explicit owner-only
loopback target documented in `docs/WORLD_FORGE.md`. It resolves the
prepare-before-confirm flow, opaque event coordinates, snapshot reconciliation,
safe restart loss, canonical safe-error and cancellation retry behavior,
truthful deadline timing, and preview receipt provenance without modifying this
product-session contract or capability list. The published RC1 remains
immutable.

The corrective `v0.1.0-rc.4` artifact and explicit live tool provide headless
single-attempt evidence: deterministic seed compilation, one configured
Spiritguide candidate, provider-independent validation and synthetic boot,
immutable content-addressed storage, cancellation/deadline selection fences,
and a safe selected receipt. RC4 supersedes RC3 for live UI integration by
making operation provenance invariant, synchronizing selection with the
authoritative deadline, rejecting malformed referenced hosts, and generating
the canonical live fixture from the runtime receipt builder. RC1 through RC3
remain immutable. The standalone RC4 wire contract still returns no Stage or
publication authority. Durable jobs, hard provider-call termination, A/B/C attempts,
retry/repair, fixed multi-attempt selection, and shared provider-budget
enforcement remain Phase 4 work. The exact checkpoint boundary is maintained
in `docs/WORLD_FORGE.md`.

## Implemented Phase 5 Forge-to-Stage Development Handoff

Phase 5 adds an explicitly constructed private path from one authoritative live
Forge selection to the existing Presenter Stage terminal. It remains outside
normal runtime composition and enables no product capability.

`WorldForgeService` commits an immutable private selected-revision record only
when an explicitly live source reaches `selected` through its trusted receipt
before the authoritative deadline. The record binds one opaque selected-record
ID, Forge service epoch, job/reference and presenter actor, selected time,
independent expiry, policy ID, exact revision ID/digest, source fingerprint,
and safe receipt correlation. The combined target configures that expiry to one
hour. Default/preview selection, a preview
transition under a live source, every non-selected state, late completion,
cancellation, wrong actor, stale epoch, and expiry cannot produce usable
handoff authority. Existing World Forge response projections do not add the
record.

`ForgeStageHandoffService` accepts only the private Forge job reference plus
authenticated Presenter and epoch evidence. It resolves the record in process,
reopens the exact content-addressed artifact by revision and digest, and
provisions a narrow create/switch grant into a dynamic-only
`PresenterStageService`. No caller may supply an artifact path, profile name,
bare digest, catalog identifier, or launch grant. Dynamic Stage starts with no
static revision or grant, so handoff failure cannot fall back to the rehearsal
world or mutate the ordinary profile store.

The Forge runtime codec preserves two intentional integrity values. The
authoritative Forge digest covers the exact canonical artifact file including
its terminating newline. The existing product lifecycle SHA covers the
newline-free canonical revision document. Forge-backed Stage projections carry
the former under optional `stage.source` together with selected-record, Forge
job, and Forge-reference correlation; the existing starting-revision
projection retains the lifecycle SHA. A command-updated detached runtime keeps
the immutable Forge origin without treating mutable session state as a new
selected artifact.

An ordinary handoff creates or resumes only the identical selected-record and
compatible grant binding. Exact same-key/body retry replays the original
result; a changed body conflicts. The Forge expected-epoch header is also
semantic request evidence: changing it on an already claimed key conflicts and
requires reconciliation plus a fresh key. A different selection cannot replace
the current Stage through ordinary launch, even if it resolves to the same
revision. The additive explicit Forge-replacement operation requires current
session authority and exact expected control revision, provisions first, and
then uses the shared atomic cutover fence. Failure leaves the current Stage
usable.

The retained corrective private contract kit lives at
`contracts/product-api/v0.5.0-rc.2/`. It retains all 15 Phase 3D routes and
adds:

```text
POST /product/v1/stage/forge-handoffs
POST /product/v1/stage/sessions/{reference}/forge-replacement
```

The manifest declares an exact, SHA-256-pinned dependency on published
`contracts/world-forge-api/v0.1.0-rc.4/manifest.json`, and combined startup
checks that byte identity before constructing either provider collaborator.
`stage.source` is optional in the general Stage projection so previous static
Stage values remain valid, but every Forge-handoff success requires and cross-validates
exact selection/source/starting-revision identity. Its generated server-only
client provides `launch_selected_forge_revision(...)` and
`replace_stage_from_selected_forge_revision(...)`.

Phase 5.1 repins the generator, validator, generated client, and combined
development target to corrective RC2. RC1 immutability is retained through a
byte-exact tree-digest regression; the current repinned tools do not accept RC1
as their active contract root.

`tools/product_forge_stage_live.py` explicitly starts two loopback listeners:
one World Forge RC4 transport and one additive private Stage transport. They
use distinct owner-only bearers and one shared Presenter actor. Forge build
submission may make one logical configured Spiritguide attempt; each accepted
Stage command may call the configured command model. Startup, reads, artifact
handoff, resume, and replacement provisioning call no provider. Setup, UI/BFF
flow, lifetime, cost, and focused verification are documented in
`docs/FORGE_STAGE_LIVE_DEVELOPMENT.md`.

The selected record and derived grant expire one hour after selection. This
caps Stage at the remaining portion of that window even though its configured
absolute lifecycle TTL is two hours; the 30-minute idle TTL can end it earlier.
The launcher may omit only the accepted-command count with
`--max-commands -1`. All other Stage safeguards remain finite.

Every RC1 capability entry remains disabled, and corrective RC2 changes no
capability claim. RC2 Stage snapshots report `xray_available = false`; the
additive Phase 6 target below owns X-ray truth. Full Phase 4
durability, hard provider-call deadlines, bounded A/B/C attempts and repair,
fixed selection, shared Forge/Stage provider-budget enforcement, production
gateway and browser controls, deployment isolation, load/restart/cutoff proof,
and the independent capability gates remain unfinished.

## Implemented Phase 5.1 Replacement Contract Correction

Strict UI integration found one contradiction in RC1's canonical successful
replacement fixture. Its `previous_snapshot` reports a `replaced` lifecycle but
retains active controls, an available command slot, and active recovery
metadata. A browser cannot safely decide whether the old authority is terminal,
so the UI must keep explicit replacement unavailable. This finding does not
block first handoff, exact retry/resume, dynamic Stage routing, refresh, or
ordinary terminal commands against RC1.

The owning runtime transition already defines the required truth: successful
cutover revokes the old authority, increments its control revision, appends an
authoritative `session.terminal` event, projects a terminal command slot, and
retains a bounded tombstone. The terminal Stage snapshot has no available
controls, no active command ID, and a non-null retention time later than its
terminal time. It preserves the old session and Forge source identity; the new
active snapshot preserves the replacement selection and agrees with the
top-level `forge_selection`.

Phase 5.1 preserves every published `v0.5.0-rc.1` byte. Its published
corrective release candidate is `v0.5.0-rc.2`, with the same 17 routes, exact
World Forge RC4 manifest dependency, disabled capability claims, dynamic-only
Stage, BFF authority, and provision-before-cutover fence. Its canonical fixture
comes from the same terminal projection and tombstone policy as the runtime
rather than from a partial mutation of an active snapshot.

The shared Stage snapshot schema and generated server-only client reject a
non-active lifecycle with non-empty controls, a nonterminal command slot, an
active command ID, or non-tombstone/unbounded recovery. Operation-aware
validation and owning tests prove the request-relative relationships that
a response-only schema cannot: old control revision equals accepted CAS plus
one, the returned event position includes the replacement's terminal event,
old authority is revoked, same-key/body retry returns the same cutover without
another allocation, and both old and new Forge source identities remain exact.

Acceptance is provider-free. It consists of the owning lifecycle/core/Stage
and Forge-to-Stage tests, the combined injected-collaborator HTTP tests, exact
RC2 validation, and generated-client mutation smoke. Handoff, replacement
provisioning, and contract validation invoke no provider. Validate published
RC2 with:

```sh
.venv/bin/python tools/product_forge_stage_contract.py validate \
  contracts/product-api/v0.5.0-rc.2
.venv/bin/python tools/product_forge_stage_contract.py client-smoke \
  contracts/product-api/v0.5.0-rc.2
```

The backend handoff to the UI includes the implementation commit, exact RC2
identity, manifest and OpenAPI digests, and those command results. The UI then
repins the contract, regenerates its server-only client, restores the canonical
replacement fixture to strict decoder tests, and may add a separate
presenter-confirmed replacement action. Ordinary handoff of another selection
continues to return `current_session_exists`; it never becomes implicit
replacement.

This is a Phase 5 correction, not the first Phase 6 slice. It introduces no
X-ray data or coordinate, provider call, normal-runtime import or object,
production gateway, public attendee authority, durable Forge work, deployment
evidence, or capability enablement. Phase 6 starts from the corrected RC2
snapshot baseline and separately owns the allowlisted dual-position X-ray
projection and its privacy, consistency, and recovery evidence.

## Implemented Phase 6 Stage X-ray Development Slice

The corrective additive `v0.6.0-rc.3` release candidate preserves published
X-ray RC1/RC2, Stage RC2, and their
exact World Forge RC4 dependency. It adds two private presenter-session reads:

- `GET /product/v1/stage/sessions/{reference}/xray`; and
- `GET /product/v1/stage/sessions/{reference}/xray/details/{xray_entity_id}`.

The detail request carries `accepted_state_revision`, `xray_stream_id`, and
`xray_sequence`. The accepted revision and X-ray stream/sequence are mandatory
preconditions; any mismatch returns `xray_resync_required` with
`resume_xray_snapshot`. The Stage event cursor returned in each X-ray response
is an observed correlation watermark only. It is not accepted as a detail
precondition, cannot order X-ray entities, and is not an X-ray replay cursor.
This slice offers no X-ray event stream.

`PresenterStageService` constructs X-ray only when an explicit composition
supplies `StageXrayPolicy`. The normal Stage constructors remain disabled and
allocation-free. The combined Forge/Stage development tool supplies the policy
and reports active-session X-ray availability; close, expiry, and replacement
remove the predecessor projection and report terminal X-ray unavailable.
Projection construction and both coordinates are read under the product
session lock. Accepted commits advance accepted revision and X-ray position.
Observed foreground activity may advance only the X-ray position. A Stage
event watermark may change without changing either X-ray coordinate.

The `stage-xray-v1` allowlist exposes bounded safe text for world display,
host identity/purpose/operating-system fields, host aggregates, and accepted
world/host fact statements from named scalar fields. Stable public entity IDs
are hashes of source keys. It returns per-section included/omitted counts,
clipping and control-marking counts, separate foreground/enrichment activity,
and explicit evidence-unavailable state. It does not return arbitrary nested
state, raw `SessionState` or `world_state`, command text, paths, users,
environment, prompts, reasoning, credentials, provider envelopes, traces,
failure capsules, admin controls, or another session.

Provider-free owning and loopback tests prove bounds, hostile text marking,
accepted-versus-projection-only advancement, stale detail rejection, session
authority, terminal cleanup, replacement isolation, X-ray failure isolation,
strict schemas, fixtures, and generated-client coordinate validation. RC2
omits optional empty display values, supplies an unambiguous fallback for
required host labels, counts facts without a nonempty statement as omitted,
uses the explicit Presenter Stage route prefix in generated methods, binds a
detail response to the requested entity, and makes generated validation enforce
the strict nested projection schemas. RC3 additionally enforces JSON-safe
coordinate bounds before comparison or transport, exact success response
context, unambiguous scope-separated fact identities, and collision-safe
summary/detail integrity. The
production `stage_xray` capability remains disabled with reason
`integration_preview_only`; this release candidate and live target are UI
integration evidence, not deployment enablement.

## Implemented Phase 7A Source-Neutral Presenter Launch Selection

Phase 7A publishes the additive, unfrozen `v0.7.0-rc.1` release candidate. It
preserves published Stage X-ray RC3 and the existing live Forge handoff, and
adds exactly two private Presenter operations:

- `GET /product/v1/stage/launch-options`; and
- `POST /product/v1/stage/launch-selections`.

Both operations require the private Product service bearer, the expected
launch-selection service epoch, and BFF-held Presenter actor context. Exchange
also requires an idempotency key. The request body contains exactly one opaque
`launch_selection_id`; it cannot carry an offering ID, profile name, profile
path, raw state, revision/digest pair, Forge epoch, session capability, or
caller-supplied launch grant.

The option projection is bounded and display-safe. It exposes a bounded
`offering_id`, opaque `launch_selection_id`, display labels, immutable revision
identity and digest, optional expiry, and one of the closed source origins
`authored_profile_snapshot` or `world_forge_selection`. The offering and
selection identifiers are locators, not authority. Browser code may render
them, but only the BFF may present private actor evidence and retain the
exchanged, actor-bound, role-bound, operation-scoped, expiring `launch_grant`.
The BFF uses that grant with the existing private Stage create or explicit
switch/replacement flow; it must never expose the grant or actor context to the
browser.

`src/dreamland/product/launch_selection.py` owns the bounded source-neutral
registry and exchange. Trusted composition registers immutable revisions and
safe offerings; untrusted transport data cannot select a filesystem object or
inject revision content. `src/dreamland/product/launch_selection_stage.py`
coordinates the registry with a dynamic-only `PresenterStageService`.
Exchange resolves the exact registered revision and provisions it before the
grant or idempotent success becomes visible. The Stage seam validates the
revision, digest, origin, display metadata, actor binding, and allowed
operations before atomic registration. Existing Forge-specific handoff routes
and their fuller Forge source correlation remain unchanged.

Coordinator construction verifies exact Presenter credential/actor/expiry
compatibility, operation-family containment, and both Stage revision-size
ceilings before any option can be listed. Registry active-grant capacity counts
in-flight provisioning callbacks. Absolute actor, option, and grant expiries
are enforced against the current wall clock as well as monotonic deadlines;
registry and Stage must share one monotonic clock, and the trusted provisioning
seam transfers the exact private registry deadline while Stage also caps it by
the remaining UTC lifetime. Reentrant callbacks and forward, backward,
callback-boundary, or compound wall-clock adjustments therefore cannot exceed
capacity or leave Stage authority alive after registry expiry.

The registry never exposes more than the contract's 128 options. Trusted
registration also requires offerings that name one immutable revision to use
identical Stage display metadata, so every listed option is provisionable and a
conflict fails before publishing the option or reserving its locator. Dynamic
Stage removes expired grants from active capacity before provisioning another
selection. Revision, display, and Forge-source projection data remain retained
while an active session or tombstone depends on them and are reclaimed only
after that recovery state expires. A source-neutral grant carries a positive,
epoch-local monotonic serial plus a digest derived from that serial, the epoch,
and private candidate entropy. The registry retains only bounded active grants,
and Stage can release an expired source-neutral digest while retaining the
serial high-water mark that rejects the old credential. The existing
Forge-specific seam continues to reserve its legacy grant digests.

`src/dreamland/product/authored_artifacts.py` owns trusted startup import for
authored worlds. An explicitly constructed trusted startup caller supplies a
local world-profile path and the expected digest of its metadata-free canonical
revision. Import reads a regular, non-symlink source within a hard size bound,
validates world-profile shape and synthetic boot eligibility, verifies the
digest before store mutation, and writes or reopens an owner-only, create-only
content-addressed artifact. It fsyncs a private temporary inode and publishes
with an atomic no-overwrite hard link, so concurrent importers see either no
final artifact or the complete canonical bytes. The immutable authored envelope
contains only world level and state.
Source filename, profile name, path, timestamps, comments, and source-session
metadata do not contribute to launch identity and never cross the product
wire. Runtime allocation reconstructs a detached profile and does not retain
or mutate the ordinary profile store.

The exchange operation family is `stage.launch_selection.exchange`.
Idempotency is scoped by service epoch, actor and role, key, and canonical
selection request. The key is claimed after bearer, current-epoch, Presenter
actor, key, and body validation but before selection lookup. Therefore an
unavailable or expired selection is a deterministic claimed rejection: an
exact retry replays it, and changed-body reuse returns
`idempotency_conflict`. Replay preserves the recorded logical error while the
HTTP adapter assigns each attempt its own transport correlation ID. Retryable
adjacent Stage or registry capacity returns
`launch_selection_capacity_exhausted` and leaves the key unclaimed. After an
epoch change, an uncertain exchange returns `outcome_unknown` without minting
replacement authority. A committed exact retry returns the original grant,
even when that grant and its offering have since expired; replay uses the
immutable option projection recorded with the outcome and never re-resolves
current listing state. The BFF must then reconcile rather than redispatch
automatically. A failed adjacent-capacity attempt retains no unpublished grant
digest but permanently burns its formed serial. A later identical retry can
therefore make progress with a distinct credential without making any prior or
unpublished credential reusable.

The release-candidate fixtures and generated server-side client cover mixed
authored/Forge options, empty options, fresh and replayed exchange, lost
response recovery, strict request rejection, safe errors, closed origin
handling, keyed uniqueness, and allowed top-level additive response fields.
The provider-free integration preview seeds authored and synthetic Forge
revisions and exercises option listing, exchange, dynamic Stage create/switch,
terminal command execution, X-ray, and recovery without constructing a
provider. It does not import a configured profile or consume a live Forge
selection, and it is not a real-agent combined target. Phase 7B supplies that
composition separately under RC2; the original live authored Stage and
Forge-to-Stage tools remain available unchanged. These artifacts are
development evidence only. All five
product capabilities remain disabled, and normal
DREAMLAND startup imports no Phase 7A module and constructs no product object,
listener, worker, timer, provider, reservation, or bookkeeping path. Ordinary
SSH, admin, MCP, state-profile, command, and EnrichmentAgent behavior and cost
remain unchanged.

Validate the release candidate and its generated client independently:

```sh
.venv/bin/python tools/product_launch_selection_contract.py validate \
  contracts/product-api/v0.7.0-rc.1
.venv/bin/python tools/product_launch_selection_contract.py client-smoke \
  contracts/product-api/v0.7.0-rc.1
```

Start the deterministic loopback preview only with a private owner-controlled
bearer file. It validates the pinned contract before binding and announces the
actual address and all-disabled/no-provider identity:

```sh
.venv/bin/python tools/product_launch_selection_preview.py \
  --contract-root contracts/product-api/v0.7.0-rc.1 \
  --token-file /path/to/owner-only-preview.token \
  --host 127.0.0.1 \
  --port 0
```

The preview actor context is
`actor:launch_selection_presenter_0001`. The bearer and actor context stay in
the BFF; they are not browser configuration.

This checkpoint gives the UI one provider-free generic private Presenter
selection surface for authored and synthetic Forge-origin immutable revisions,
and defines the trusted boundary the Phase 7B live composition uses for
configured authored and authoritative Forge revisions. It does not make a
fresh Forge result attendee authority, implement a browser-facing catalog, or
provide live attendee support for the UI-owned `/session` route.
Public attendee admission, isolation, quota, lifecycle, expiry, and safe
projection are supplied by the later attendee phases. Generated-world
promotion, catalog publication, eligibility, and selection are supplied only
by the separate Phase 8A boundary, not this Presenter checkpoint.

## Implemented Phase 7B Live Source-Neutral Presenter Composition

Phase 7B publishes additive `v0.7.0-rc.2` without changing the 21 routes,
request bodies, response bodies, authority headers, idempotency semantics, or
closed origins introduced by RC1. RC2 changes the explicit preview identity to
the provider-backed live-development executor. Its capability response still
reports all five production capabilities disabled and
`live_capability_evidence = false`. The launch-option and selection-exchange
operations themselves perform no provider call; only a submitted Forge build
or accepted Stage command may dispatch the configured models.

The explicitly started live target imports one trusted authored world through
`ImmutableAuthoredStore` only after its metadata-free digest matches the
configured pin. It constructs Stage with
`LaunchSelectionStateProfileCodec`, which dispatches authored and Forge
immutable/runtime envelopes through separate origin-preserving paths. One
shared Presenter actor identity and expiry spans Forge, the selection registry,
and Stage, and the registry uses Stage's exact service epoch.

After a live Forge worker commits `selected`, an isolated observer invokes
`ForgeLaunchSelectionRegistrar`. The registrar resolves the authoritative
record, verifies the selected actor, reopens the exact content-addressed
artifact, and atomically registers a generic option whose expiry cannot outlive
the selected record. Callback failure never changes the Forge job's selected
truth and never publishes a partial option. The generic and legacy Forge seams
share exact Stage display metadata, so the existing Forge handoff remains
compatible.

The UI BFF pins RC2 for the live target, retains both private bearers, actor
context, exchanged grants, and Stage session capabilities server-side, and may
use the same generated view model as RC1. It lists the configured authored
option, refreshes launch options after Forge reaches `selected`, exchanges one
opaque locator, then uses the existing Stage create or explicit switch,
terminal, transcript, event, and X-ray operations. A selected Forge receipt or
job reference is not generic launch authority, and the browser never receives
the private grant.

Validate RC2 and its generated client independently:

```sh
.venv/bin/python tools/product_launch_selection_live_contract.py validate \
  contracts/product-api/v0.7.0-rc.2
.venv/bin/python tools/product_launch_selection_live_contract.py client-smoke \
  contracts/product-api/v0.7.0-rc.2
```

Operational startup and the safe BFF evaluation sequence are documented in
`docs/PRODUCT_LAUNCH_SELECTION_LIVE_DEVELOPMENT.md`. The target remains
loopback-only, manually invoked, finite in every provider/spend/concurrency/TTL
dimension, and absent from normal DREAMLAND startup. `--max-commands -1`
removes only the accepted-command count; all other protections remain active.

This phase provides real-agent private Presenter selection evidence. It still
does not enable a production capability, expose a public attendee backend,
promote a generated world, publish an attendee catalog, or make a fresh Forge
result attendee-eligible.

## Implemented Phase 7C Authored Attendee Integration Checkpoint

Phase 7C publishes additive `v0.7.0-rc.3`. It preserves all 21 RC2 operations
and adds three private BFF operations:

```text
POST /product/v1/attendee/contexts
GET  /product/v1/attendee/launch-options
POST /product/v1/attendee/launch-selections
```

The private Product Service bearer authenticates the BFF transport. Bootstrap
also requires current expected-epoch evidence, an idempotency key, and one
high-entropy `gateway_context_id`. DREAMLAND leases a never-reused epoch-local
attendee actor slot and returns its private `actor_context` to the BFF. The BFF
retains that context and adapts it to its same-origin browser session; it never
places the Product Service bearer, actor context, launch grant, or session
authority in browser code.

Launch-option listing returns only bounded display-safe authored-attendee
projections and the exact notice version the UI must present. Exchange binds
the opaque selection and presented notice version to one idempotency claim,
then returns an actor-bound, operation-scoped launch grant. An authored offering
is not itself authority, and profile names, paths, raw state, revision input,
and caller-created grants are not accepted.

The BFF then uses the unchanged session create, current, snapshot, command,
command-result, event, transcript, reset, switch, cancellation, and close
operations with that actor context and the narrower returned capabilities.
Every delegated operation revalidates the leased context before reaching the
shared core. Different contexts have distinct actor, grant, current-session,
idempotency, session, transcript, event, command, and mutable runtime scopes.

Context slots are pre-seeded at explicit composition and permanently burned
for the service epoch. Bootstrap success and deterministic terminal capacity
are claimed and replayable; changed-body reuse conflicts. Temporary
idempotency-ledger capacity is unclaimed and retryable after delay. Selection
claims include notice evidence and use a derived inner registry key, preventing
a selection-only replay from crossing notice or retention boundaries.

Attendee actors, grants, and sessions expire at the earliest UTC or monotonic
context boundary. Lazy coordinated cleanup fences and terminalizes session
work, releases standard admission once, and prunes bounded registry and facade
records. Attendee traffic uses only the `STANDARD` admission partition, so it
cannot borrow Presenter Stage reservations.

The RC3 preview is deterministic, loopback-only, provider-free, and explicitly
started. It is suitable for BFF integration of an authored `/session` flow,
but all five production capabilities remain disabled. It is not an internet-
facing attendee service, live-provider attendee evidence, deployment/cost/load
proof, or a capability-enablement artifact. Normal DREAMLAND startup imports
none of this composition and incurs no attendee API cost.

Generated-world attendee access is outside Phase 7C. A Forge revision that
appears in a Presenter registry remains hidden and unavailable to this attendee
facade; only the separate Phase 8A promotion, publication, eligibility, and
catalog boundary can produce provider-free attendee truth, with live canary and
rollback evidence still later work.

Validate the RC3 kit and generated client independently:

```sh
.venv/bin/python tools/product_attendee_contract.py validate \
  contracts/product-api/v0.7.0-rc.3
.venv/bin/python tools/product_attendee_contract.py client-smoke \
  contracts/product-api/v0.7.0-rc.3
```

Start the deterministic integration listener only on loopback with a private
owner-controlled bearer:

```sh
.venv/bin/python tools/product_attendee_preview.py \
  --contract-root contracts/product-api/v0.7.0-rc.3 \
  --token-file /path/to/owner-only-product.token \
  --host 127.0.0.1 \
  --port 0
```

The startup JSON reports the selected ephemeral port and the all-disabled,
no-provider preview identity without reporting the bearer, private BFF
authority, actor contexts, or launch/session capabilities.

## Implemented Phase 7D Live Authored Attendee Composition

Phase 7D publishes identity-only `v0.7.0-rc.4`. It preserves all 24 RC3 paths,
request and response schemas, safe errors, idempotency behavior, attendee
authority, and disabled capability claims. Its development identity truthfully
reports that accepted attendee session commands may call the configured
provider. The three attendee facade operations and lifecycle reads remain
provider-free.

`tools/product_attendee_live.py` extends the Phase 7B two-listener composition
rather than replacing it. The World Forge listener still produces and commits
authoritative selected revisions. The Product listener still supports generic
authored/Forge Presenter selection, legacy Forge handoff, Stage terminal,
replacement, and X-ray, and now also supports the RC3 authored attendee flow
through a live agent. Exact actor ownership routes shared session operations;
an unknown actor cannot fall through to the Presenter or attendee service.

The attendee registry is separate and contains only the exactly pinned authored
revision. Forge selection registers only with the Presenter registry, so a
freshly forged revision remains unavailable to attendee listing and exchange.
Phase 8A now owns the separate provider-free promotion, publication, catalog,
and eligibility checkpoint. Live Forge composition, canary, rollback, and
production truth remain later Phase 8 work.

Attendee commands use a separate live executor and in-memory checkpointer.
External Langfuse tracing is forced off for attendee content even when tracing
is configured for other explicit development collaborators. Attendee state is
process-local and never written into the pinned authored artifact store.

One shared ledger keeps the existing Presenter Stage reservations and adds a
finite `STANDARD` attendee allocation for sessions, logical jobs, physical
workers, tokens, and spend. Attendee saturation cannot consume the reserved
Presenter allocation. `--max-commands -1` disables only the per-session accepted
command count; all process, concurrency, TTL, command-size, output, attempt,
deadline, cancellation, provider, token, and spend protections remain active.

The Presenter and attendee executors each require a finite positive configured
provider maximum before construction. Each command reserves
`max(8,192, max_output_tokens)` tokens. The Presenter reservation is multiplied
by its existing 1,024-job capacity, the `STANDARD` attendee reservation is
multiplied by its existing 64-job capacity, and the shared ledger total is the
sum of those protected allocations. Startup reports both effective maxima,
reservations, and budgets. Blank or zero configuration fails before either
provider or listener is constructed; no request can dispatch under a smaller
reservation.

The configured admission close bounds authored attendee offering, grant,
session, and provider-publication truth on both UTC and monotonic clocks. At
cutoff, new selection and work fail closed, active attendee sessions become
terminal, queued or running command work is cancelled/fenced, and admission is
released once. A still-valid attendee context can recover its bounded terminal
current-session projection but cannot resume work. Explicit process shutdown
also revokes private attendee authority before worker teardown.

Validate and smoke the RC4 contract independently:

```sh
.venv/bin/python tools/product_attendee_live_contract.py validate \
  contracts/product-api/v0.7.0-rc.4
.venv/bin/python tools/product_attendee_live_contract.py client-smoke \
  contracts/product-api/v0.7.0-rc.4
```

The owner-only credential setup, authored digest pin, combined startup command,
safe announcement, and BFF evaluation sequence are documented in
`docs/PRODUCT_ATTENDEE_LIVE_DEVELOPMENT.md`.

RC4 is private loopback development evidence only. All five capabilities remain
disabled. It is not an internet-facing attendee backend, browser security
boundary, Gate 3, load/deployment/provider-isolation proof, generated-world
attendee access, or presentation-readiness evidence. Normal DREAMLAND startup
imports and constructs none of it. This accounting correction changes only the
explicit live composition and startup audit; published RC4 routes, DTOs,
fixtures, generated client, manifest, and capability identity remain immutable.

## Implemented Phase 8A Provider-Free Generated-World Access Checkpoint

Phase 8A defines a separate, unfrozen World Access release candidate under
`contracts/world-access-api/v0.1.0-rc.3/`; reviewed RC1 and published RC2 remain
byte-exact. RC3 pins Product API
`v0.7.0-rc.3` and World Forge API `v0.1.0-rc.4` by exact manifest digest; it
does not revise either dependency. The six private BFF operations are:

```text
GET  /world-access/v1/identity
POST /world-access/v1/promotions
GET  /world-access/v1/promotions/{promotion_id}
POST /world-access/v1/promotions/{promotion_id}/reveal
GET  /world-access/v1/catalog
POST /world-access/v1/launch-selections
```

`src/dreamland/product/generated_access.py` owns separate Operator promotion
and reveal authority. Promotion accepts only a current-epoch private actor,
idempotency key, and non-authorizing Forge job reference. A trusted source
resolves the selected record and exact immutable artifact server-side and
revalidates their canonical identity before creating the hidden access record.
Paths, raw state, Forge credentials, selected-record authority, and caller-
chosen revision coordinates do not cross the operation boundary.

Reveal requires the expected hidden access-record ID. It commits the revealed
successor only after the trusted catalog publisher has reserved and registered
the exact generated offering. A retryable publication-capacity failure cannot
leave a revealed record absent from catalog. Exact idempotent replay returns the
original result. A cutoff or clock failure after reservation rolls back the
attempt's pending binding and exact newly installed registry state before the
failure returns; deterministic cutoff, changed-body, and changed-record
rejections remain claimed. All authority, records, replay state, retention, and
expiry remain bounded and epoch-local.

The coordinator holds access and then catalog across the reentrant reveal and
pending-to-active move. Concurrent status and catalog reads therefore observe
either the prior hidden/omitted state or the revealed/published state, never a
revealed record whose catalog offering is still pending.

The existing authored-attendee options operation holds the launch-registry lock
through snapshot and origin filtering. Reversible cleanup of a still-pending
generated registration cannot invalidate that published Product read.

`src/dreamland/product/attendee_catalog.py` is the only generated eligibility
bridge into the attendee registry. Its catalog combines authored offerings with
revealed generated bindings while filtering every bare generated registry
entry. A safe item contains only opaque offering and offering-revision locators,
closed origin, bounded display, notice version, and expiry. It exposes no
internal launch selection, promotion/access record, revision, digest, path, raw
state, or authority.

Exchange binds the exact catalog offering revision and presented notice to the
attendee actor and idempotency claim. The coordinator mints an internal signed
eligibility proof over the epoch, offering/access binding, origin, immutable
revision/digest, and expiry. The attendee facade verifies that proof before its
constructor-disabled generated provisioning seam returns a standard Product
`launch_grant`. The response also returns required output-only
`launch_binding_sha256`, carried from the semantic digest Product already
computed while provisioning the grant. The BFF then uses the unchanged Product
RC3 session create and lifecycle operations. Existing Product attendee option
and exchange operations remain authored-only, with a separate World Access
idempotency family.

Exact Product create replay remains the primary response-loss recovery path.
If recovery reaches the current-session read, the BFF may associate that
session with the World Access selection only when both service epochs match and
the World Access digest equals `SessionSnapshot.launch_binding.sha256`. A
mismatch retains the uncertain-create fence and cannot relabel the session.
Notice equality alone is never selection identity. No request accepts the
digest, and it remains outside browser DTOs.

Validate and smoke the separate contract independently:

```sh
node --version  # Node.js 20 or newer
npm ci --prefix tools/world_access_client_smoke
.venv/bin/python tools/product_world_access_contract.py validate \
  contracts/world-access-api/v0.1.0-rc.3
.venv/bin/python tools/product_world_access_contract.py client-smoke \
  contracts/world-access-api/v0.1.0-rc.3
```

RC3 preserves the six operations and RC2 lifecycle/retry shapes while adding
the required launch-binding output to successful selection exchange.
Each operation declares its required `none`, `operator`, or `attendee` actor
class. Publication verifies the exact method/path/security/parameter/schema
matrix, compiles the server-only TypeScript client, and exercises all six calls
through a bounded transport. The client accepts JSON and problem JSON and
validates the status-appropriate content type before decoding.

`tools/product_world_access_preview.py` explicitly constructs a deterministic
selected-artifact source, access aggregate, authored/generated catalog,
generated-opt-in attendee facade/core, and unchanged Product session surface.
The co-hosted catalog uses one Phase 8A notice for both origins. Executable
timestamps use startup wall time plus monotonic elapsed time, while tests can
inject an exact deterministic clock. It is loopback-only and makes no provider
call. Its identity reports no live
Forge evidence, no live capability evidence, and no enabled capabilities. The
full startup and BFF evaluation sequence is documented in
`docs/PRODUCT_WORLD_ACCESS_DEVELOPMENT.md`.

Phase 8A is integration evidence, not a production attendee capability,
internet-facing gateway, live Forge adapter, live command target, durable
publication service, deployment/load proof, canary/rollback system, or
presentation rehearsal. Importing it starts nothing, and normal DREAMLAND
startup does not construct its service, catalog, listener, worker, timer,
provider, reservation, or bookkeeping. Phase 8B composes the existing live
Forge source adapter explicitly while preserving this authority boundary.

## Implemented Phase 8B Live Generated-World Composition

The separate `tools/product_world_access_live.py` target now composes the
authoritative live Forge source, distinct Presenter and Operator authority,
World Access promotion/reveal/catalog, the Product attendee facade and core,
and an isolated model-backed attendee executor under one Product epoch. World
Access `v0.1.0-rc.4` truthfully identifies that live composition while keeping
all production capabilities disabled and leaving Product `v0.7.0-rc.4`
unchanged.

Generated access still requires the complete selected Forge, Operator
promotion, hidden access, explicit reveal, catalog/notice exchange, and
ordinary Product grant/session sequence. The live target retains sixteen
prepared or committed generated publications for the full service epoch so
expiry cannot invalidate an issued grant or exact replay. Its provider-backed
backend smoke passed through command completion and clean shutdown. Browser
security, deployment, load, canary, rollback, and capability evidence remain
outside that result. The exact startup, accounting, and evaluation boundary is
documented in `docs/PRODUCT_WORLD_ACCESS_DEVELOPMENT.md` and decision 0074.

## Implemented Phase 9A Private Attendee Security Checkpoint

Product `v0.8.0-rc.1` is the additive, provider-free private-security release
candidate. It preserves all 24 RC4 operations and adds:

```text
DELETE /product/v1/attendee/contexts/{gateway_context_id}
```

The private BFF supplies its transport-held bootstrap authority, the actor
context bound to that gateway lease, expected epoch evidence, and an
idempotency key. Exact replay resolves before post-revocation authority checks;
changed-body reuse after a successful claim conflicts; a fresh missing,
mismatched, expired, or fenced pair produces one uniform unclaimed authority
denial. Policy reserves one successful replay slot per burnable context, so
invalid unique keys cannot exhaust legitimate teardown. Success fences the actor, grants,
sessions, active command, output, and accepted-state publication and returns no
context or session credential.

`src/dreamland/product/attendee_security.py` adds an optional bounded logical-
request ledger keyed only by the digest of the opaque gateway context and a
process-global counter. Existing idempotency replay/conflict truth resolves
before a fresh operation is charged. Bootstrap, either grant exchange, create,
submit, reset, and switch consume the budget; reads, cancel, close, revoke,
expiry, shutdown, and reconciliation remain available at saturation. The
retryable `logical_rate_limited` result reserves no downstream idempotency
claim.

`tools/product_attendee_security_preview.py` explicitly constructs only one
authored attendee registry/facade/core, deterministic bounded workers, and the
logical ledger. Its loopback allowlist exposes attendee operations only: it
does not compose Stage, X-ray, Forge, World Access, Operator, admin, MCP,
profile lookup, or a provider. All capabilities remain disabled. Browser
cookies, CSRF, Origin/Host policy, browser DTOs, public-edge limits, client-IP
policy, and browser recovery continue to belong to the same-origin UI BFF.
The startup and BFF handoff are documented in
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md` and decision 0075.

Deliberate context teardown does not add a new DREAMLAND error. A successful
backend response remains `attendee_context_revoked`; the UI-owned BFF may map
its deliberately ended browser mapping to the typed `access-ended` UI result.
After restart, an old read remains `session_lost` and an old mutation or revoke
remains `outcome_unknown`. The UI presents restart separately, and an explicit
context-start is the only boundary for new work. Neither layer automatically
rebases, bootstraps, remints authority, or redispatches across either fence.

## Implemented Phase 9B Attendee-Dedicated Live Provider Checkpoint

Product `v0.8.0-rc.2` is the published identity-only live successor to RC1.
It remains byte-for-byte immutable. Product `v0.8.0-rc.3` is the corrective
Phase 9B identity: it preserves the same 25 method/path operations, request and
response schemas, context revocation, logical admission, idempotency, epoch,
and recovery behavior while truthfully marking accepted attendee command
execution and its privacy notice as provider-backed. RC1 and RC2 remain
immutable. All five production capabilities remain disabled.

The live composition binds its launch offering to the existing reviewed
provider-backed pair `notice:attendee-live-development-v1` and
`policy:attendee-live-authored-v1`. The returned launch option carries that
notice version; exact notice-bound exchange carries it into the granted
selection; and session create retains both identities in `launch_binding`.
Phase 9A retains `notice:attendee-security-preview-v1` and
`policy:attendee-security-preview-v1`. Selection therefore cannot silently
reuse a provider-free notice for provider-backed commands.

`src/dreamland/product/attendee_live.py` defines a frozen, non-secret worker
configuration and an exact-authored-revision worker factory. The parent
validates a dedicated provider credential as a bounded owner-current-UID,
exact-mode-`0600`, regular, non-symlink file without reading it. Each spawned
child repeats validation, reads the single key, and constructs its attendee
executor with external tracing and callbacks disabled, command text redacted
from token/resource logs, and provider retries set to zero. Ambient provider
and observability credential variables are rejected and scrubbed; generic
DREAMLAND configuration is not a fallback.

`src/dreamland/product/attendee_provider.py` owns a fixed-capacity, spawn-only
process pool and bounded JSON command protocol. Complete local worker readiness
is required before listener publication, but readiness proves only local
executor/provider-client construction, not upstream reachability,
authentication, quota, model availability, or billing state. Dispatch,
request, cancellation, replacement, drain, shutdown, command, state, output,
and IPC bounds remain finite. The dispatch timeout covers worker startup plus
both bounded retired-child containment phases. The Product core and pool use
the same runtime-document byte and structural policy, and both the initial
authored document and every proposed committed state are checked before the
state can become Product truth.

Replacement acceptance still uses the existing pool lifecycle. Owning tests
wait through `startup_timeout_ms + (2 * terminate_grace_ms)` plus a one-second
test scheduler margin and fail immediately on a terminal pool state. Success
requires lifecycle `ready`, `ready_workers == worker_capacity`, no active
worker, full available capacity, and a successful follow-up dispatch in the
focused cancellation and concurrent-failure cases. This replaces a fixed
three-second test assumption that was shorter than the test pool's valid
eight-second startup window; it does not weaken or rename any runtime timeout.

The composition derives its process-lifetime logical-job capacity from active
session capacity times the configured per-session command maximum: the default
four active sessions and 64 commands produce 256 retained jobs. Token and spend
budgets scale from that value. Settled admission records remain for process-
lifetime replay and accounting, so this explicit aggregate bound is not a
renamed eight-command quota.

Each accepted command has one finite maximum-token and spend reservation. A
response confirmed before its provider deadline may reconcile authoritative
usage; an ambiguous, timed-out, protocol-lost, or forcibly terminated dispatch
consumes the full reservation. Cancellation already observed at the exact
deadline wins and retains interrupted/actual-usage truth. If the deadline is
observed first, output is fenced and the child receives only the existing
bounded cancellation grace. A confirmed return is completed, non-ambiguous,
and reusable at the pool, but remains a full-reservation
`provider_deadline_exceeded` result; a non-return remains ambiguous, fully
reserved, and contained. The pool never automatically redispatches. Local
termination prevents that child from publishing late output or state, but it
does not prove the upstream provider request or billing stopped. An external
deployment supervisor remains required. Decision 0080 records the exact edge.

`tools/product_attendee_security_live.py` explicitly composes one pinned
authored revision, the attendee-only Phase 9A service/core, and the dedicated
pool. It waits for every child before binding one loopback listener. Its exact
route allowlist contains no Stage, X-ray, Forge, World Access, Operator, admin,
MCP, profile-path, or generic launch authority. Shutdown always pairs service
and pool `begin_drain` with bounded service and pool `shutdown`; drain alone is
not child termination. The startup, credential, BFF, and validation workflow is
documented in `docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md` and decision 0076.
The privacy-identity and replacement release-gate correction is recorded in
decision 0077.

Importing these modules starts nothing. Normal DREAMLAND startup, SSH, admin,
MCP, state profiles, logging, and EnrichmentAgent composition do not construct
the live attendee service, listener, provider, child, timer, reservation,
registry, admission ledger, or request hook.

## Implemented Phase 9C Supervised Availability Checkpoint

Product `v0.8.0-rc.4` is the wire-compatible successor for the first Phase 9C
deployment slice. It keeps RC3's 25 operations, request/response schemas,
provider-aware notice and policy, context revocation, logical admission,
idempotency, epoch fencing, and explicit restart behavior. Published RC1
through RC3 remain immutable, and every production capability remains
disabled.

`AttendeeSecurityComposition` may now carry one exact immutable availability
window. When absent, the Phase 9A/9B startup-derived defaults are unchanged.
The supervised target requires an operator-selected UTC open, admission-close,
and recovery-close boundary. Construction propagates the resolved window into
the offering, actors, BFF authority, and attendee policy before requests are
accepted. `PublicAttendeeSessionService` projects the same UTC values to its
existing monotonic deadline domain, preventing a backward wall-clock change
from extending admission.

`tools/product_attendee_security_deployment.py` is a separate explicitly
invoked loopback composition around the Phase 9B provider pool. Its no-request
watcher expires due sessions and begins drain at admission close, fencing active
output and state publication while retaining the listener for bounded terminal
recovery. At recovery close, or after `SIGINT`/`SIGTERM`, one main-thread
supervisor stops admission, stops accepting connections, joins admitted
request handlers, shuts down the service, converges the provider pool within
its existing local containment bounds, and joins task threads. Signal handlers
only record the stop request; they never run teardown inside asynchronous
signal context.

The deployment composition opts into an exact service-level bootstrap cutoff
in addition to its no-request watcher. Bootstrap before admission opens remains
valid preparation. At or after admission close, exact claimed retries retain
their response-loss replay and changed bodies retain idempotency conflict, but
an unclaimed bootstrap cannot consume a context slot while watcher scheduling
catches up. Phase 9A and Phase 9B retain their prior default bootstrap policy.

Credential rotation is deliberately a process replacement. The operator stops
the old process, changes owner-only files while it is down, and starts a new
process and epoch. The UI/BFF continues to own its deliberate typed
`access-ended` result and must present restart separately before an explicit
new context-start. Stale reads remain `session_lost`; stale mutations and
revocation remain `outcome_unknown`. There is no hot reload, authority rebase,
automatic remint, or cross-epoch provider redispatch.

This checkpoint is deployment lifecycle evidence, not production capability
evidence. It provides no internet listener, public browser policy, completed
load or abuse evidence, provider-account/quota proof, canary, or rollback.
The workflow and remaining gates are documented in
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md` and decision 0078.

### Opt-in attendee capacity envelope

Phase 9C-D keeps the accepted 4-context/4-session/2-worker/8-connection defaults
but lets the explicitly started live and supervised tools select a larger
finite private envelope. Contexts and sessions are capped at 64, provider
workers at eight, and admitted connections at 32. Startup rejects
`workers > sessions` or `sessions > contexts` before provider construction.
Context slots are burned for the service epoch; closing or revoking a context
does not recycle its actor authority.

Logical-job and token/spend budgets derive from active sessions times the
positive command count. The fresh logical-request budget derives enough shared
capacity for one bootstrap/exchange/create sequence per context plus one submit
per worker. Actor, request-tracking, grant, tombstone, and every related
idempotency store scale from the same values. The safe startup announcement
reports the effective limits, hard maxima, and retained-record ceilings rather
than leaving a secondary quota implicit.

Focused provider-free tests exercise 20 distinct contexts and sessions, typed
overflow and cleanup, plus five-command worker saturation and exact-request
recovery. The accepted subprocess rehearsal remains fixed at 4/4/2/8 and the
live canary remains fixed at one command; neither is silently reclassified as
scaled-load evidence. Product `v0.8.0-rc.4` is unchanged, every capability is
still disabled, and the target remains loopback/private-BFF only. Decision 0082
records the formulas and remaining measured-load boundary.

### Provider-free private-boundary rehearsal

`tools/product_attendee_security_deployment_rehearsal.py` adds a black-box
Phase 9C gate around that unchanged RC4 target. It launches the real supervised
server in subprocesses, drives the immutable generated client over actual
loopback sockets, and uses the real fixed worker-process pool with a finite
deterministic executor in place of provider access. Its accepted fixed local
matrix is four contexts, four active sessions, two workers, and eight
connections.

The rehearsal covers replay/conflict, bounded capacity, worker cancellation,
cutoff and terminal recovery, unread-body/pipelining protection, stalled-header
connection saturation, signal convergence, and restart-only transport-bearer
and epoch rotation. Unsafe body rejections close their HTTP/1.1 connection
before unread bytes can be parsed as another request. The saturated-shutdown
announcement includes both possible connection-deadline intervals. Its
deterministic worker factory succeeds with a blank owner-only provider file
that the live content loader would reject, proving the rehearsal does not read
credential content. It does not prove provider-key consumption or rotation.

At cutoff the RC4 snapshot remains lifecycle `expired` with terminal reason
`absolute_expired`; an active command remains failed with reason
`session_terminated`, full reserved usage, and no accepted late output or
state. The pool-internal deadline result does not become a new wire result.

On success its report contains only allowlisted contract hashes, fixed
successful case outcomes, public error codes, bounded counts/timings, signal
results, and convergence truth; failure emits only a generic safe error and
exits nonzero. The success report is explicitly scoped
`loopback_provider_free` with `production_capability_evidence=false`; it is not
provider, provider-key-rotation, public-edge, load, canary, rollback, or
capability-enablement evidence. RC4 and its generated client are not
regenerated or modified. Decision 0079 records this boundary, and decision
0080 records the exact provider deadline/cancellation semantics and unchanged
RC4 terminal projection.

### Bounded live-provider deployment canary

`tools/product_attendee_security_deployment_canary.py` adds one explicitly
acknowledged live-provider check around the same immutable RC4 target. It
requires the exact authored profile path and digest, a current-user-owned
`0600` credential file, and explicit model and maximum-output settings. It
accepts only 1 through 8,192 maximum output tokens, limiting the one authorized
provider response without changing the supervised target default or range.
It binds only loopback on an ephemeral port and fixes one worker, a per-session
command limit of one, the command `uname -a`, and one logical provider attempt.
The canary independently fixes 240 output chunks per command while leaving the
supervised target default unchanged. This preserves accepted-cursor replay
within the 256-event journal after running, optional state, two final, and
terminal events. Credential content remains child-local; importing the tool
has no provider or ordinary-runtime effect.

Success requires complete, unclipped event and transcript reconstruction,
consistent terminal result and snapshot truth, session close, backend
`attendee_context_revoked`, and bounded process shutdown. The allowlisted
report is evidence that the configured provider completed that single command
at that time, but sets `production_capability_evidence=false`. Its settled
token and spend usage must exactly equal the announced full reservations. The
report also states 65,536 output bytes and 240 output chunks per command. This
is conservative settlement, not actual provider billing or token truth. It
does not prove upstream request-count instrumentation,
account/quota separation, cancellation or billing termination, load,
rollout/rollback, public-edge or BFF/browser security, or production readiness.

RC4 wire and capability truth do not change. The backend does not gain an
`access-ended` result: deliberate browser `access-ended` remains BFF-owned.
After restart, stale reads remain `session_lost`, stale mutations and revokes
remain `outcome_unknown`, and an explicit new context-start is required.
Decision 0081 records this narrow evidence boundary.

## Agreed Semantics And Remaining Capability Work

The requirements below remain the accepted end-state. Phase 1 through Phase
9C-D now implement the lifecycle, shared-core, Presenter Stage, local
live-provider, World Forge, exact Forge-to-Stage development, replacement
contract correction, X-ray, source-neutral private Presenter selection, and
its explicit live authored/Forge composition plus provider-free authored
attendee integration, provider-backed authored attendee development, and
provider-free and live generated-world promotion/catalog exchange, plus the
provider-free private attendee security checkpoint and attendee-dedicated live
provider process boundary, supervised availability checkpoint, and opt-in
capacity envelope described above. Capability enablement, the remaining live
World Forge checkpoints, measured deployed attendee load/security evidence, and
public attendee capability enablement remain future work.
Later phases must move only their newly completed behavior into an implemented
section with owning acceptance evidence.

### First mutation and event phase

- The first phase that exposes a mutation acknowledgement and an event channel
  must add deterministic stalled/dropped mutation-response and promised-event
  withholding scenarios. The withheld-event scenario begins from an
  authoritative acknowledgement that actually promises an event, withholds it
  beyond the consumer deadline, and permits same-key result query or snapshot
  reconciliation without redispatch.
- This obligation was deliberately not represented by a Phase 0 capability
  fixture, because inventing a feed or mutation route would falsely imply live
  transport. Moving it to its first owning phase changes delivery order, not the
  mutually accepted recovery requirement.

### Authority, epoch, and immutable launch

- A launch selection or grant is opaque, narrow, role- and operation-scoped,
  expiry-bound, and resolved server-side to one immutable selected revision.
- Session construction deep-copies the explicit revision. It does not mutate
  the process default profile or write product sessions into legacy indexes.
- Conference product state, authority, streams, positions, idempotency, and
  tombstones are process-local and bound to a high-entropy service epoch.
- Every private read, mutation, and retry carries expected-epoch evidence.
  Restart makes old reads `session_lost` and uncertain old mutations
  `outcome_unknown`; it never silently rebases or redispatches them.
- Attendee, Presenter, and Operator authority remain distinct. A public
  offering, catalog identifier, or Stage reference is non-authorizing.

### Generated-world promotion and catalog exchange

- DREAMLAND owns the immutable promotion/access record, authoritative offering
  eligibility, a bounded private catalog projection, and the server-side
  exchange from an eligible offering identifier to an opaque, narrow attendee
  launch selection. The offering identifier alone never authorizes a runtime
  session.
- The UI-owned BFF owns browser-facing catalog routes and DTOs, presentation,
  browser authority, and the adaptation from its same-origin exchange to the
  private DREAMLAND operation. It does not mint DREAMLAND launch authority or
  receive a general product capability in browser code.
- Full catalog administration, presentation-specific card metadata and
  ordering, deployment publication, and promotion canaries remain a separate
  product/deployment workflow. Its successful record may feed the bounded
  catalog projection, but Forge-to-Stage does not implement or waive it.
- This separate path does not block shared core or the first Stage terminal. It
  still blocks any claim that generated-world attendee access or the
  presentation-complete path is live until the later live/deployment gates
  pass.
- Phase 7A Presenter `offering_id` and `launch_selection_id` values are not a
  promotion/access record and convey no attendee eligibility. Phase 8A now
  mints and enforces separate provider-free attendee-facing eligibility without
  treating a fresh private Forge selection as publication authority; Phase 8B
  must preserve that boundary for the live adapter.

### Session lifecycle and replacement

- Current lookup never creates. A distinct create while a current session
  exists returns `current_session_exists`; exact same-key/body replay returns
  the recorded result within the epoch.
- Lifecycle/control CAS, command-slot generation, accepted-state revision,
  transcript position, event position, and X-ray projection position are
  separate coordinates used only for their specified purposes.
- Reset and switch return typed busy while the sole command slot is occupied.
  They provision and validate a replacement before atomic cutover; failure
  preserves the old session and authority.
- Close, expiry, and successful replacement fence every stale command and
  enrichment publisher, reconcile capacity once, and retain only bounded safe
  tombstone/recovery state. A future service cutoff must invoke and prove that
  same fence before this capability can be enabled.
- Ordinary Stage launch resumes only the identical originating selection or
  grant with a compatible effective policy. Revision equality alone is not
  authority. Another selection requires explicit expected-control-CAS
  replacement.

### Commands and cancellation

- DREAMLAND's product-only `product-single-v1` classifier validates one
  canonical UTF-8 command before slot allocation, provider reservation,
  transcript mutation, or event creation. It rejects malformed UTF-8, unpaired
  surrogates, NUL, CR, LF, and unsupported shell-control syntax; it trims only
  leading/trailing ASCII space or horizontal tab and performs no Unicode
  normalization. SSH syntax is unchanged.
- Every accepted product command occupies the session's only command slot until
  authoritative terminal truth exists. There is no command-text-derived
  read-only bypass.
- Potential mutations run against isolated provisional state and a slot-scoped
  checkpoint. Generation happens outside the publication lock. One product
  compare-and-set installs terminal truth and any accepted state effect; a
  losing, cancelled, stale, replaced, expired, or revoked provisional identity
  can never publish accepted state.
- `completed` carries exactly one tagged guest termination—`exited` with an
  integer status or `signaled` with a canonical guest signal—and exactly one
  state effect of `none`, `committed`, or `rejected`. Only `committed` advances
  accepted-state revision. `interrupted` and `failed` carry a typed reason and
  omit guest termination, state effect, and resulting accepted revision.
- Exact same-key cancel replay returns its recorded acknowledgement. A new
  cancel and terminalization serialize at one publication fence. Terminal
  first yields `already_terminal`; cancel-fence first yields nonterminal
  `cancel_requested`, prevents later completion/state promotion, and eventually
  records `interrupted` or a typed `failed` cleanup result.

### Recovery, enrichment, resources, and X-ray

- Bounded ordered events, transcript, authoritative command-result query, and
  snapshot recovery provide reconciliation after response loss, event delay,
  cursor eviction, and reconnect. Within-epoch cursor loss is
  `snapshot_required`; old-epoch cursor use is epoch loss.
- Product enrichment has a separate operation identity and provisional state.
  Proposal work may run concurrently, but APPLY waits for foreground terminal
  truth and revalidates under the same product lock. Accepted patch, accepted
  revision, and journal transition install atomically before external events.
- Every workload sharing constrained provider credentials participates in one
  admission ledger. The initial live deployment uses dedicated product
  credentials and a separate, non-overcommitting product provider pool. It
  cannot borrow ordinary SSH/EnrichmentAgent capacity, and its configured
  reservations must remain within any provider-account or deployment-wide hard
  limit. Product commands, product enrichment, Forge, and retries that share
  those credentials share that product ledger. If deployment cannot prove the
  separation and hard-cap accounting, the affected product capabilities remain
  disabled. Stage reservation is advertised only when that guarantee is real.
  Phase 9B's dedicated file and local pool prove code-path separation only;
  provider account and upstream quota separation remain unfinished Phase 9C
  evidence.
- Stage X-ray is an allowlisted display projection, never raw `SessionState`,
  prompts, reasoning, credentials, provider envelopes, traces, environment,
  paths, failure capsules, or another session. Accepted-state revision and
  X-ray projection position are its two concurrency coordinates. The reported
  Stage event position is only a correlation watermark, never an X-ray replay
  cursor or third revision.

### Retention, tracing, and redaction ownership

- DREAMLAND owns bounds and deletion for process-local product sessions,
  transcripts, event journals, idempotency records, and tombstones. Close,
  expiry, and restart promises apply only to that local product state according
  to its documented retention policy.
- DREAMLAND owns redaction at the source for product logs and traces. Private
  service and browser credentials, provider keys, raw capabilities, prompts,
  reasoning, provider envelopes, failure capsules, environment values, and
  internal paths must not cross the product telemetry boundary. Safe
  allowlisted fields are emitted only when the later implementation and its
  tests define them.
- The deployment/operator owns whether external product tracing is enabled,
  the approved destinations, their retention and deletion policy, and the
  corresponding published notice. External tracing for public attendee traffic
  starts disabled and remains disabled unless that data handling is explicitly
  approved and tested. A DREAMLAND close never claims deletion from OpenAI,
  Langfuse, or infrastructure providers.
- The UI/BFF owns browser and gateway logs, analytics, redaction, retention,
  browser-notice delivery, and protection of its cookie/session material. It
  must not forward private DREAMLAND capabilities or credentials into those
  systems. Notice-version evidence sent to DREAMLAND is minimal delivery
  evidence, not a claim of legal consent.

## Compatibility and Capability Gates

Compatible additive fields may evolve only within a declared compatibility
range. Required-field, semantic, authority, security, state-machine, or
enumeration changes require a new major revision or coordinated migration.
Consumers preserve unknown additive data where required but never infer an
unknown capability or event type as supported.

A capability can be enabled only when its implementation, transport,
authorization, fixtures, generated-client smoke test, concurrency and restart
tests, deployment controls, load/cost evidence, and artifact digests pass its
independent gate. Master configuration and release artifacts must agree. A
partial implementation remains disabled even in an integration environment;
prerelease simulators and fixtures must be labeled as such.

## Maintenance

Update this document with each product phase. Keep the implemented section tied
to `src/dreamland/product/`, `src/dreamland/config.py`,
`contracts/product-api/`, `tools/product_contract.py`,
`tools/product_lifecycle_contract.py`, `tools/product_lifecycle_preview.py`,
`tools/product_session_contract.py`, `tools/product_core_preview.py`,
`tools/product_forge_stage_contract.py`, `tools/product_xray_contract.py`,
`tools/product_launch_selection_contract.py`,
`tools/product_launch_selection_preview.py`, `tools/product_forge_stage_live.py`,
`contracts/world-access-api/`, `tools/product_world_access_contract.py`,
`tools/product_world_access_preview.py`, `tools/product_world_access_live.py`,
`tools/product_attendee_security_contract.py`,
`tools/product_attendee_security_preview.py`,
`tools/product_attendee_security_live_contract.py`,
`tools/product_attendee_security_live_identity_contract.py`,
`tools/product_attendee_security_live.py`,
`tools/product_attendee_security_deployment_contract.py`,
`tools/product_attendee_security_deployment.py`,
`tools/product_attendee_security_deployment_rehearsal.py`,
`tools/product_attendee_security_deployment_canary.py`,
`src/dreamland/product/attendee_security.py`,
`src/dreamland/product/attendee_live.py`,
`src/dreamland/product/attendee_provider.py`, and owning tests. Move
requirements from the future section only when behavior exists and its
capability remains truthfully gated. Update decision 0050 before changing
trust ownership, capability identifiers, the master gate, disabled behavior,
or the admin/SSH separation. Update decisions 0050, 0051, and 0053 before
changing product-provider isolation, preview composition, command/publication
semantics, telemetry/retention ownership, generated-world catalog exchange
ownership, publication/freeze evidence, or promised-event simulator sequencing.
Update decision 0061 before changing selected-record authority, exact artifact
hydration, dynamic Stage provisioning, Forge-source projection, or combined
live-development isolation. Keep decision 0062 aligned with the immutable RC1
boundary, Phase 5.1 terminal snapshot correction, provider-free acceptance, UI
replacement gate, and Phase 6 exclusion. Keep decision 0063 aligned with the
X-ray allowlist, bounds, dual coordinates, watermark semantics, terminal
cleanup, and normal-runtime isolation. Keep decision 0066 aligned with the
source-neutral origin set, authored snapshot trust boundary, BFF-only launch
authority, exchange idempotency, Stage provisioning, attendee/Phase 8
exclusions, and normal-runtime isolation.
Keep decision 0072 aligned with generated access authority, reveal/publication
ordering, internal eligibility proofs, authored Product compatibility, exact
contract dependencies, and provider-free preview identity.
Keep decision 0073 aligned with generated-session recovery correlation and
evaluator-visible identity, decision 0074 aligned with the live World Access
composition and retained publication accounting, and decision 0075 aligned
with private attendee revocation, logical admission, BFF trust ownership, and
normal-runtime isolation. Keep decision 0076 aligned with the attendee-only
credential, spawn-process, capacity, readiness, drain/shutdown, conservative
accounting, and explicit restart boundaries.
Keep decision 0077 aligned with immutable RC2 preservation, RC3 live privacy
identity, lifecycle-derived replacement synchronization, and the unchanged
typed deliberate-end and explicit-restart behavior.
Keep decision 0078 aligned with exact supervised availability, bounded
recovery, signal convergence, and restart-only rotation; decision 0079 aligned
with the provider-free rehearsal and its strictly local evidence; and decision
0080 aligned with exact provider deadline/cancellation precedence, worker
reuse, conservative accounting, and unchanged RC4 terminal projection. Keep
decision 0081 aligned with the explicitly acknowledged single-command canary,
complete-publication evidence, non-production report, and unchanged RC4,
deliberate-end, and restart semantics. Keep decision 0082 aligned with the
opt-in capacity envelope, derived retention limits, unchanged defaults, and
separate sustained-load evidence gate.
