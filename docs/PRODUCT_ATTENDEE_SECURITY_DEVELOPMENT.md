# Public Attendee Private Security And Live Provider Development Targets

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
local evaluators
Purpose: explain how to run and evaluate the provider-free Phase 9A private
attendee security target, the Phase 9B attendee-dedicated live provider target,
the Phase 9C supervised availability and capacity target, and its bounded
live-provider canary without exposing DREAMLAND directly to a browser or
changing ordinary runtime behavior.

## Overview

Phase 9A adds the first deliberately attendee-only private integration target.
It gives the UI-owned BFF a small loopback service for testing context
bootstrap, authored selection, session lifecycle, replay-safe logical request
admission, and explicit context revocation. It does not compose Presenter
Stage, X-ray, World Forge, World Access, admin, MCP, a live provider, or an
internet-facing listener. Phase 9B keeps that exact attendee and authority
surface, but replaces the deterministic executor with a fixed pool of spawned,
credential-isolated live provider workers. It still does not compose any
Presenter, Forge, Operator, World Access, admin, or MCP authority.

Under the [project model](PROJECT_MODEL.md), this is entry-point-neutral work
toward the LLM-emulated computing network: a browser reaches the same
protocol-neutral world/session services through a replaceable BFF entry point,
not through web-specific runtime behavior. SSH remains the ordinary entry
point, and this target exists only when it is started explicitly.

```text
browser --same origin--> UI BFF --private bearer + authority--> DREAMLAND
                         |                                      attendee only
                         +-- owns cookie, CSRF, Origin/Host,
                             public throttling, and browser DTOs
```

The Phase 9A Product contract identity is `v0.8.0-rc.1`. It preserves the Product RC4
surface, adds one private context-revocation operation, and adds the safe
`logical_rate_limited` result used by the new boundary. All five production
capabilities remain disabled. Product RC4 and World Access RC4 are immutable.
Because this remains an additive full-Product artifact, its manifest retains
RC4's exact World Forge dependency for the dormant Forge/Stage routes. The
attendee-only executable neither exposes those routes nor constructs a Forge
object or provider; removing the artifact dependency belongs to a future
deliberately split attendee-only contract, not this compatible checkpoint.
Phase 9B first published the identity-only successor `v0.8.0-rc.2`: the same
25 routes and wire behavior, with truthful live-provider executor metadata.
That published tree remains immutable. The corrective Phase 9B target is
`v0.8.0-rc.3`; it preserves RC2's wire, authority, revocation, and recovery
behavior while binding launch selection to the already reviewed provider-backed
privacy identities `notice:attendee-live-development-v1` and
`policy:attendee-live-authored-v1`. RC1 and RC2 remain immutable, and every
release candidate keeps all five production capabilities disabled.

Phase 9C adds the identity-only `v0.8.0-rc.4` successor for an explicitly
scheduled deployment rehearsal. It preserves RC3's complete wire, privacy,
authority, revocation, and restart behavior while adding an exact UTC admission
window, a bounded terminal-recovery interval, no-request cutoff enforcement,
supervised `SIGINT`/`SIGTERM` convergence, and restart-only credential
rotation. RC1 through RC3 remain immutable. RC4 is still loopback-only,
integration-only, and capability-disabled; it is not a public listener.

The Phase 9A executable uses deterministic, provider-free authored state. It is
for BFF contract and security-lifecycle integration. The Phase 9B executable
uses one exact authored state and live model-backed attendee commands for
realism evaluation at the smaller private boundary. Continue to use the
separate Phase 8B target only when the live Forge, promotion, reveal, mixed
catalog, and attendee sequence itself is under evaluation.

## Create Private Credentials

Create a distinct owner-only transport bearer and BFF bootstrap authority in a
private durable directory:

```sh
.venv/bin/python tools/product_attendee_security_preview.py init \
  --token-file /path/to/private/attendee-security/product.token \
  --bootstrap-file /path/to/private/attendee-security/bootstrap.json
```

The command refuses to overwrite either file. Both remain on the BFF/backend
host. Never place their contents, an actor context, launch grant, session
authority, idempotency key, or gateway-context identifier in browser code,
browser-visible URLs, client-readable storage, fixtures, screenshots, or logs.

Use credentials dedicated to this target. A bearer from the combined live,
Stage, Forge, World Access, admin, or MCP services is neither accepted nor
appropriate here.

## Start The Attendee-Only Target

Start the listener explicitly and pin it to the new release candidate:

```sh
.venv/bin/python tools/product_attendee_security_preview.py serve \
  --token-file /path/to/private/attendee-security/product.token \
  --bootstrap-file /path/to/private/attendee-security/bootstrap.json \
  --contract-root contracts/product-api/v0.8.0-rc.1 \
  --host 127.0.0.1 \
  --port 0
```

Only loopback hosts are accepted; use numeric `127.0.0.1` for an unambiguous
deployment binding. Port `0` selects an ephemeral port.
Startup validates the exact contract before accepting requests and prints one
safe JSON announcement with the bound address, contract and executor identity,
route count, deterministic resource limits, logical-window limits, and
disabled-capability truth. It does not print a path, file content, bearer, BFF
authority, context, actor, grant, session capability, raw revision, provider
setting, or secret.

`--once` serves exactly one request synchronously and then exits after the full
response has been written. Normal serve mode uses bounded threaded request
handling. The target retains the product preview's finite connection count,
read and whole-request deadlines, request-body limit, worker/session/job/token/
spend bounds, command-size and output bounds, one-attempt execution policy,
TTLs, journals, idempotency stores, and shutdown fence.

Importing the module or contract generator starts nothing. Normal
`python -m dreamland`, SSH, admin, MCP, state-profile, logging, and
EnrichmentAgent composition never constructs this service and pays no listener,
worker, timer, registry, logical-admission, or request-path cost.

## Prepare The Phase 9B Live Target

Use credentials and storage dedicated to this attendee-only process. The
transport token and bootstrap are initialized separately from Phase 9A so an
evaluation cannot accidentally reuse another target's authority:

```sh
.venv/bin/python tools/product_attendee_security_live.py init \
  --token-file /path/to/private/attendee-security-live/product.token \
  --bootstrap-file /path/to/private/attendee-security-live/bootstrap.json
install -d -m 700 /path/to/private/attendee-security-live/artifacts
```

Create `/path/to/private/attendee-security-live/provider.key` without
overwriting an existing file and populate it through an approved interactive or
secret-management path. It must contain exactly one nonblank UTF-8 key line,
remain owned by the current service user, and retain exact mode `0600`. Do not
put the provider key in an INI file, environment variable, command argument,
bootstrap, artifact, or BFF record. The parent checks file identity and
permissions without reading the key; each spawned provider worker repeats the
checks and is the only code that reads it.

Choose one trusted world-level state profile and calculate its exact immutable,
metadata-free digest:

```sh
.venv/bin/python tools/product_attendee_security_live.py digest \
  --profile-path /path/to/state_profiles/example.json
```

The profile path and returned digest are trusted operator configuration, never
browser or HTTP input. Startup reopens the profile without following symlinks,
requires the exact digest, and imports the immutable snapshot into the owner-
only artifact directory before the listener is published. Only that exact
authored revision can be decoded by a worker; forged, generated, path-selected,
or caller-supplied revisions are not accepted.

Before startup, remove provider and observability variables whose names begin
with `OPENAI_`, `ANTHROPIC_`, `XAI_`, `LANGCHAIN_`, `LANGSMITH_`, or
`LANGFUSE_`, as well as the exact name `LANGFUSE`. Phase 9B fails closed when
one is present. It has no ambient-key or generic DREAMLAND-configuration
fallback.

## Start The Phase 9B Live Target

Start the attendee-only process with an explicit canonical provider/model and
finite model output maximum:

```sh
.venv/bin/python tools/product_attendee_security_live.py serve \
  --token-file /path/to/private/attendee-security-live/product.token \
  --bootstrap-file /path/to/private/attendee-security-live/bootstrap.json \
  --contract-root contracts/product-api/v0.8.0-rc.3 \
  --artifact-root /path/to/private/attendee-security-live/artifacts \
  --authored-profile-path /path/to/state_profiles/example.json \
  --authored-profile-digest sha256:REPLACE_WITH_EXACT_DIGEST \
  --provider-credential-file \
    /path/to/private/attendee-security-live/provider.key \
  --model PROVIDER:MODEL \
  --max-output-tokens POSITIVE_INTEGER \
  --worker-capacity 2 \
  --max-commands 64 \
  --host 127.0.0.1 \
  --port 0
```

Startup validates the exact RC3 artifact, executor and privacy identities,
25-route surface, and all-disabled capability truth before constructing
provider workers or binding a listener. The launch option advertises
`notice:attendee-live-development-v1`; a successful notice-bound exchange and
session create retain that notice and
`policy:attendee-live-authored-v1` in the launch binding. Phase 9A continues to
use its provider-free security-preview notice and policy.

The optional system-prompt and supported model-control arguments are bounded
provider settings, not authorization. Context and active-session capacity both
default to four, worker capacity defaults to two, and connection capacity
defaults to eight. Explicit capacity arguments may raise those values only as
far as 64 contexts, 64 active sessions, eight workers, and 32 connections;
startup requires workers not to exceed active sessions and active sessions not
to exceed contexts. The target derives its process-lifetime logical-job
capacity from active sessions and `--max-commands`. The default is 256 retained
jobs (four slots times 64 commands), so an unrelated renamed quota cannot make
command 13 fail. The target retains one provider
attempt per command, a 120-second request deadline, a two-second cancellation
grace, a 30-second worker-startup timeout, a 30.5-second dispatch timeout that
also covers both 250-millisecond retired-child containment phases, a ten-second
shutdown drain, and bounded local TERM/KILL containment phases.
`--max-commands` changes only the positive accepted-command limit and
does not remove any other safeguard. It requires a positive integer; zero and
negative values are rejected.

The Product core and worker protocol share the same runtime-document policy:
4 MiB maximum encoded size, depth at most 32, at most 65,536 JSON nodes,
string-only object keys, finite numbers, and integers within the signed 63-bit
range. The pinned initial document and every proposed committed state are
preflighted with that policy before they can become Product truth. Command
input remains capped at 4,096 bytes, and IPC frames are capped at 5 MiB.
Command output remains capped at 65,536 bytes and is published on each complete
newline, or at roughly 4 KiB when no newline arrives, so ordinary terminal
output is prompt without making provider segmentation determine clipping.
Terminal-only stdout/stderr suffixes use the same UTF-8-safe 4 KiB publication
boundary. Provider retries are zero. Each command reserves
`max(8,192, max_output_tokens)` tokens and 1,000 spend units before dispatch;
the token and spend budgets scale from the derived logical-job capacity. The
ledger retains settled jobs and consumed budget for the process lifetime. An
ambiguous or forcibly terminated request conservatively retains its full
reservation. At the exact provider deadline, cancellation already observed
first remains interrupted and reconciles authoritative usage. If the deadline
is observed first, output is fenced and cooperative cancellation receives only
the existing grace; a confirmed return keeps the worker reusable but remains a
full-reservation `provider_deadline_exceeded` result. A non-return remains
ambiguous, fully reserved, and contained. Decision 0080 records this ordering.

All configured children must construct their local executor and provider client
before the listener binds or the safe startup JSON is printed. This readiness
does not call the provider and does not prove upstream authentication,
reachability, model availability, quota, or billing state. The announcement
reports the canonical model, maximum output tokens, finite limits, worker
readiness, contract/executor identity, selected notice/policy identity, and
disabled-capability truth. It omits
paths, profile identity or digest, credentials, authority, commands, and
provider secrets.

`--once` processes one request synchronously, completes its response, and then
performs the same drain/shutdown sequence. Normal serve mode uses the existing
bounded threaded private transport.

On termination the tool gates fresh service and pool work, fences service
authority and cancellation, performs bounded pool shutdown and child reaping,
and then converges task threads. `begin_drain` and `shutdown` are a required
pair: drain alone does not terminate a wedged child. A local worker kill fences
DREAMLAND output and state publication but does not prove an upstream request
or its billing stopped; deployment accounting must retain that uncertainty.

## Start The Phase 9C Supervised Target

Use the Phase 9C executable when evaluating an operator-selected event window,
termination-signal behavior, or restart-only credential rotation. It accepts
the same dedicated provider, authored revision, transport, resource, and model
arguments as Phase 9B, plus three required canonical UTC boundaries:

```sh
.venv/bin/python tools/product_attendee_security_deployment.py serve \
  --token-file /path/to/private/attendee-security-live/product.token \
  --bootstrap-file /path/to/private/attendee-security-live/bootstrap.json \
  --contract-root contracts/product-api/v0.8.0-rc.4 \
  --artifact-root /path/to/private/attendee-security-live/artifacts \
  --authored-profile-path /path/to/state_profiles/example.json \
  --authored-profile-digest sha256:REPLACE_WITH_EXACT_DIGEST \
  --provider-credential-file \
    /path/to/private/attendee-security-live/provider.key \
  --model PROVIDER:MODEL \
  --max-output-tokens POSITIVE_INTEGER \
  --context-capacity 20 \
  --active-session-capacity 20 \
  --connection-capacity 32 \
  --worker-capacity 5 \
  --max-commands 64 \
  --availability-opens-at 2026-09-02T18:00:00Z \
  --availability-closes-at 2026-09-02T20:00:00Z \
  --recovery-closes-at 2026-09-02T20:10:00Z \
  --host 127.0.0.1 \
  --port 0
```

This example selects the bounded UI rehearsal profile. Omitting the four
capacity arguments preserves the accepted 4-context/4-session/8-connection/
2-worker behavior. A context slot is burned for the service epoch and is not
recycled by close, revoke, or expiry; size it for distinct context starts over
the process lifetime. Active sessions and connections are separate concurrent
limits. The worker count bounds simultaneous provider calls and is deliberately
smaller than the session count.

At 20 contexts, 20 sessions, five workers, and 64 commands per session, the
target derives 1,280 process-lifetime logical jobs. Its one-second shared
fresh-operation limit becomes 65: enough for bootstrap, launch exchange, and
session create for all 20 contexts plus one command per worker. The per-context
limit remains eight. Authority, tombstone, and idempotency stores scale from the
same envelope; startup reports their effective finite ceilings and the four
hard maximums. These dictionaries allocate entries only as work occurs.

The open boundary may be in the future. The private transport and context may
be prepared before it, but launch exchange and resulting session work return
the existing typed not-open result. At the admission close, the backend's
startup-UTC-anchored monotonic gate closes fresh work. A watcher runs the same
coordinated expiry even when no request arrives, terminalizes due sessions,
fences active command publication, and begins service/provider drain. The
private listener remains available until `recovery-closes-at` so the BFF can
read terminal truth and perform allowed recovery or cleanup. The recovery
boundary then closes the listener and completes bounded teardown.

For an active command, that RC4 terminal truth remains lifecycle `expired`
with terminal reason `absolute_expired`, and the command remains failed with
reason `session_terminated`, full reserved usage, and no accepted late output
or state. The pool-internal `provider_deadline_exceeded` result from a
deadline-first race is not added to the RC4 wire surface.

The exact cutoff is also enforced atomically inside fresh context bootstrap,
so a request cannot slip through while the no-request watcher is being
scheduled. Bootstrap remains available before the open boundary for private
BFF preparation. After close, an exact claimed bootstrap retry still replays
for response-loss recovery and changed-body reuse still conflicts, while a new
bootstrap key returns the existing typed `service_closed` result.

Synchronize the deployment host's UTC clock before startup. The process
captures that UTC value once and advances its wall-shaped and deadline clocks
from monotonic elapsed time, so a later wall-clock adjustment cannot extend or
shorten the active process's window. Stop and restart with reviewed timestamps
when the operator must re-anchor the schedule.

The main thread supervises both `SIGINT` and `SIGTERM`. A signal requests the
same ordered drain, request-handler join, service shutdown, provider-pool
containment, and task-thread convergence used at scheduled recovery close.
The safe startup announcement reports the exact window, supported signals,
restart-only rotation rule, finite shutdown bound, effective capacity and rate
limits, and derived retained-record ceilings. It never prints a file path,
bearer, bootstrap or actor authority, provider key, authored profile or digest,
command, or private state.

Rotate a transport bearer, BFF bootstrap, or provider credential only while
the process is stopped. Wait for bounded termination, replace the owner-only
file through the approved secret path, and start a new process. Restart creates
a new service epoch: stale reads remain `session_lost`, stale mutations or
revocation remain `outcome_unknown`, and the UI/BFF must offer an explicit
restart followed by a new context-start. The target has no SIGHUP reload,
authority rebase, automatic remint, or cross-epoch redispatch.

## Run The Provider-Free Deployment Rehearsal

Run the black-box Phase 9C rehearsal after changing the supervised private
target, shared Product transport, attendee admission, cancellation, cutoff, or
restart behavior:

```sh
.venv/bin/python tools/product_attendee_security_deployment_rehearsal.py run \
  --contract-root contracts/product-api/v0.8.0-rc.4
```

The command creates only temporary owner-private fixture material, starts the
real supervised server on loopback in child processes, uses the immutable RC4
generated server-side client for valid operations, and uses raw sockets only
for the unread-body/pipelining and stalled-header saturation probes. The real
two-worker process pool is retained, but its worker executor is deterministic
and provider-free. The fixture supplies a one-byte blank owner-only provider
file: it passes the parent's metadata validation but the live content loader
would reject it as an API key. A successful run therefore proves that no
provider credential content was read. No provider account or quota is used,
and no upstream provider network call is made.

The accepted subprocess rehearsal deliberately retains its fixed certification
boundary of four attendee contexts, four active sessions, two workers, and
eight connections. The rehearsal exercises
replay/conflict, capacity, worker cancellation, cutoff and retained recovery,
unread-body desynchronization protection, signal convergence, and
stop/replace/start transport-bearer and epoch rotation. Unsafe body rejections
close their connection before unread bytes can become another request. The
invalid placeholder makes the deterministic worker factory's non-read boundary
observable, while provider-key consumption and rotation remain outside this
evidence.

Successful stdout is one machine-readable report containing only allowlisted
public hashes, limits, fixed successful case outcomes, allowlisted error codes,
bounded timings and counts, signal statuses, and convergence truth. A failed
run exits nonzero with only a generic safe error on stderr. A successful report
always identifies itself as `loopback_provider_free` and sets
`production_capability_evidence` to `false`. Do not reinterpret it as evidence
for a public listener, UI/BFF browser security, real provider reachability or
cancellation, deployment load, canary/rollback, provider-key rotation,
provider account separation, quota, or billing.

## Run The Bounded Live-Provider Canary

Run this opt-in checkpoint only when one real provider-backed command is
intended and its cost is authorized. First derive and review the exact authored
profile digest:

```sh
.venv/bin/python tools/product_attendee_security_deployment.py digest \
  --profile-path /path/to/state_profiles/example.json
```

Store the provider key in a regular file owned by the current user with mode
`0600`, then run:

```sh
.venv/bin/python tools/product_attendee_security_deployment_canary.py run \
  --acknowledge-provider-spend \
  --contract-root contracts/product-api/v0.8.0-rc.4 \
  --profile-path /path/to/state_profiles/example.json \
  --profile-digest sha256:REPLACE_WITH_EXACT_DIGEST \
  --provider-credential-file \
    /path/to/private/attendee-security-live/provider.key \
  --model PROVIDER:MODEL \
  --max-output-tokens INTEGER_1_TO_8192
```

The acknowledgement is mandatory. The canary accepts `--max-output-tokens`
only from 1 through 8,192, limiting the one authorized provider response. This
does not change the supervised target's default or accepted range and is
independent of the publication-chunk bound below. Optional provider reasoning,
verbosity, speed, prompt-cache, and system-prompt arguments use the same
validated settings as the supervised target. The canary refuses a contract
other than the exact immutable Product `v0.8.0-rc.4` byte/tree identity or a
profile whose content does not match the supplied digest. It creates disposable
private transport/bootstrap material, binds the child target only to
`127.0.0.1` on an ephemeral port, and fixes worker capacity and the per-session
command limit to one. It alone fixes the effective output-chunk maximum at
240; the supervised target's default remains unchanged. The bound leaves space
in the 256-event journal for the running event, optional accepted state, two
final publications, and terminal overhead so accepted-cursor replay remains
complete. The only
submitted command is the fixed `uname -a`, and there is one logical provider
attempt with no automatic redispatch.

Provider credential content is read only in the spawned child worker. Startup
and the report exclude credentials, authorities, private paths, profile
content, command output, and provider response envelopes. Importing or not
running this explicit tool starts nothing and imposes no provider, listener,
process, or credential-read cost on ordinary DREAMLAND or SSH operation.

A successful run validates the complete, unclipped command event stream and
transcript against authoritative terminal truth, then closes the session,
revokes the attendee context, and verifies bounded process shutdown. Its
strict machine-readable report identifies live-provider execution for this
single command run and always sets `production_capability_evidence` to `false`.
The report states `output_bytes_per_command=65536` and
`output_chunks_per_command=240`. Success verifies that exact settled token and
spend usage equal their announced full reservations. This remains conservative
settlement, not actual provider token or billing truth.
The report's `contract.backend_commit` identifies the implementation revision
bound into immutable RC4; it is not the canary runner's source revision. Retain
the invoking DREAMLAND Git revision and dirty-state status beside any report
used as evidence.

This canary does not prove upstream request-count instrumentation, provider
account or hard-quota separation, provider-key rotation, upstream cancellation
or billing termination, deployed load or abuse capacity, rollout/rollback,
internet/public-edge operation, UI/BFF cookie, CSRF, Origin, Host, DTO,
throttle, or browser-recovery security, sustained provider availability, or
production readiness. Success proves only that the selected credential and
model completed that one command at that time.

Backend revoke success remains `attendee_context_revoked`; only the BFF may
map its deliberate browser-session end to `access-ended`. Across a target
restart, stale reads remain `session_lost`, stale mutations and revokes remain
`outcome_unknown`, and new work requires an explicit context-start. The canary
does not remint, rebase, retry, or redispatch work across an epoch.

## BFF Integration Flow

Generate the BFF's private server client from the selected target's exact
contract root: `v0.8.0-rc.1/generated/` for provider-free Phase 9A,
`v0.8.0-rc.3/generated/` for live Phase 9B, or `v0.8.0-rc.4/generated/` for the
supervised Phase 9C target. Keep the transport bearer and all returned
authority in a protected server-side browser-session record.

1. Read Product capabilities and retain the current service epoch. Treat the
   disabled capability result as integration-only truth, not permission to
   expose a public route.
2. Generate a high-entropy opaque gateway-context identifier on the server and
   bootstrap it through `POST /product/v1/attendee/contexts`. Store the returned
   attendee actor context only in the BFF record associated with the
   authenticated browser session.
3. List authored launch options, select the UI notice by the exact returned
   notice version, present it, and exchange the chosen opaque option using a
   fresh idempotency key. For RC3 the returned version is the reviewed
   provider-backed `notice:attendee-live-development-v1`, not Phase 9A's
   provider-free security-preview identity. Retain the returned grant
   server-side.
4. Create and operate the attendee session through the ordinary Product
   snapshot, transcript, events, result, command, cancel, reset, switch, and
   close operations. Replay only an identical key and semantic request after a
   lost mutation response.
5. When the BFF deliberately ends the browser-session mapping, call
   `DELETE /product/v1/attendee/contexts/{gateway_context_id}` with the bound
   actor context, current expected epoch, and a fresh idempotency key. If the
   response is lost, replay the exact same operation. Do not substitute a new
   key to discover whether a context still exists.
6. After successful revocation, discard the BFF's gateway-context identifier,
   actor, grants, session capabilities, idempotency state, and cached private
   projections according to its own recovery policy.

The successful DREAMLAND result remains `attendee_context_revoked`. The BFF
may convert its deliberately ended browser mapping into the UI contract's typed
`access-ended` response, but `access-ended` is not a DREAMLAND wire error and
must not be inferred from an arbitrary backend failure. Keep the ended mapping
fenced until the UI explicitly starts a new context.

An exact successful revoke replay returns the original safe result with
`replayed: true`, and changed-body reuse of that claimed key conflicts. A new
request for a missing, mismatched, expired, or already revoked pair receives the
same non-enumerating authority denial and remains unclaimed. This prevents
unique mismatch keys from consuming the replay capacity reserved one-per-
context for legitimate teardown.
Revocation fences active command output and accepted state, terminalizes owned
sessions, expires issued grants and actor authority, and leaves only bounded
replay truth. Restart rotates the epoch; an old uncertain revocation becomes
`outcome_unknown` rather than being silently repeated against a new process.
An old read after restart remains `session_lost`, and any old mutation or
revoke remains `outcome_unknown`. The BFF must present restart separately from
deliberate `access-ended`, and neither the backend nor BFF may automatically
rebase, bootstrap, remint authority, or redispatch across that boundary. New
work begins only through an explicit context-start flow.

## Logical Request Admission

The target enables a fixed monotonic logical-request window with both
per-context and process-wide finite limits. DREAMLAND stores only the SHA-256
digest of the high-entropy gateway context in this ledger; it neither accepts
nor derives a client IP, cookie, browser identity, or network reputation.
The executable anchors display/expiry time at startup and advances it from the
host monotonic clock, so windows reopen in normal serve mode without trusting a
changeable wall clock.

A charge occurs only for a fresh context bootstrap, authored or catalog grant
exchange, session create, command submit, reset, or switch. Existing
idempotency replay/conflict truth resolves first and consumes no second charge.
A `logical_rate_limited` response is retryable after its advertised delay and
does not reserve the operation's idempotency key.

Reads, command cancellation, session close, context revocation, expiry,
shutdown, and reconciliation remain available after the fresh-work limit is
full. This is intentional: throttling must not prevent interruption, cleanup,
or recovery. The UI should surface temporary saturation without automatically
changing the request body or key.

This private logical budget supplements the UI's public-edge controls. The BFF
still owns per-account/browser/network abuse policy, request coalescing, cookie
and CSRF validation, and any distributed limiter needed by a multi-instance
deployment.

## Route And Authority Boundary

The target allowlists only capabilities plus the attendee context, authored
selection, and shared attendee-session operations. Stage, Presenter selection,
Forge handoff, X-ray, World Access, Operator, admin, and MCP-shaped paths are
unavailable. Unknown or privileged paths do not become reachable through an
attendee bearer.

The bearer authenticates one private transport peer; it is not actor authority.
The BFF bootstrap can lease a bounded set of attendee actors; it cannot mint a
role, choose a raw revision, construct a launch grant, inspect privileged state,
or cross into another service. The gateway-context identifier is
non-authorizing by itself.

Browser-facing authentication and authorization are deliberately absent from
this executable. Do not reverse-proxy it directly as a public API. The UI BFF
must enforce same-origin routes, HttpOnly/Secure/SameSite cookie policy, CSRF,
Origin and Host checks, safe response DTOs and rendering, security headers,
public request/body limits, and browser recovery deadlines before any later
capability can be enabled.

## Validation

Run the owning provider-free gates:

```sh
.venv/bin/python tools/product_attendee_security_contract.py validate \
  contracts/product-api/v0.8.0-rc.1
.venv/bin/python tools/product_attendee_security_contract.py client-smoke \
  contracts/product-api/v0.8.0-rc.1
.venv/bin/python -m unittest -v \
  tests.test_product_attendee \
  tests.test_product_attendee_security \
  tests.test_product_attendee_security_contract \
  tests.test_product_attendee_security_preview
```

Validate the corrective Phase 9B successor and its provider-free process-
boundary tests independently:

```sh
.venv/bin/python tools/product_attendee_security_live_identity_contract.py validate \
  contracts/product-api/v0.8.0-rc.3
.venv/bin/python tools/product_attendee_security_live_identity_contract.py client-smoke \
  contracts/product-api/v0.8.0-rc.3
.venv/bin/python -m unittest -v \
  tests.test_product_attendee_live_boundary \
  tests.test_product_attendee_provider \
  tests.test_product_attendee_security_live_contract \
  tests.test_product_attendee_security_live_identity_contract \
  tests.test_product_attendee_security_live
```

Validate the wire-compatible Phase 9C successor and its provider-free
supervisor gates independently:

```sh
.venv/bin/python tools/product_attendee_security_deployment_contract.py validate \
  contracts/product-api/v0.8.0-rc.4
.venv/bin/python tools/product_attendee_security_deployment_contract.py client-smoke \
  contracts/product-api/v0.8.0-rc.4
.venv/bin/python -m unittest -v \
  tests.test_product_attendee_security_preview \
  tests.test_product_attendee_security_deployment \
  tests.test_product_attendee_security_deployment_contract \
  tests.test_product_attendee_security_deployment_rehearsal \
  tests.test_product_attendee_security_deployment_canary
.venv/bin/python tools/product_attendee_security_deployment_rehearsal.py run \
  --contract-root contracts/product-api/v0.8.0-rc.4
```

These tests inject provider-free worker collaborators; they do not read a real
provider credential or make an upstream call. They prove RC1 and published RC2
remain byte-for-byte unchanged, exercise the exact live notice through option
listing and exchange into the session launch binding, and wait for worker
replacement readiness using the test-only bound
`startup_timeout_ms + (2 * terminate_grace_ms)` plus a one-second scheduler
margin. Recovery requires
`ready`, full ready and available capacity, zero active workers, and a
successful follow-up dispatch. This synchronization changes no runtime
timeout. A release candidate is not published
until its manifest binds the exact tested implementation commit and all
predecessor immutability and repeated replacement gates pass. Phase 9C tests
also exercise exact-window validation and propagation, future opening,
no-request cutoff, terminal recovery, signal teardown, request-thread joining,
safe announcement redaction, and restart-only transport-authority rotation
without loading a real provider credential or making an upstream call. The
separate black-box rehearsal extends that evidence across real subprocesses,
sockets, worker processes, unread-body and stalled-header traffic, local
capacity, cancellation, and transport-authority restart while remaining
explicitly provider-free and non-production.

The focused capacity-envelope tests separately exercise 20 real service
contexts, grants, and sessions plus five simultaneous deterministic commands,
typed overflow, exact-request recovery after a worker slot returns, and full
close/revoke cleanup. They neither change the fixed subprocess rehearsal report
nor claim deployed-load or provider evidence.

The canary test suite is provider-free: it injects the finite local fixture and
verifies explicit spend acknowledgement, exact RC4 pinning, credential
metadata, fixed 65,536-byte/240-chunk limits and command, one logical attempt,
complete event/transcript validation, report privacy, close/revoke, and
shutdown. The
manual canary `run` command is the separate opt-in live-provider gate and can
make one logical provider attempt. The backend does not claim a physical
upstream request count.

## Remaining Production Gates

Neither Phase 9A, Phase 9B, nor the supervised Phase 9C checkpoint is the
`public_attendee_sessions` capability. The Phase 9C target establishes exact
backend cutoff, a bounded recovery interval, termination-signal convergence,
and the restart-only credential boundary. It still provides no internet
listener, distributed admission, durable session recovery, deployment secret
distribution, completed security assessment, load or abuse evidence,
canary/rollback evidence, or provider-account/quota proof. A distinct file and
code path do not by themselves prove a distinct provider account or hard
upstream quota.

The provider-free rehearsal exercises the local private boundary under its
accepted fixed limits, restart, saturation, unread-body traffic, cancellation,
privacy, and transport-authority rotation. The configurable capacity envelope
now removes the known four-session and adjacent retained-record blockers for a
20-session/five-worker UI rehearsal, but it is not sustained real-process load
evidence. The rest of Phase 9C must exercise the complete deployed
BFF-to-DREAMLAND boundary under measured load, real-provider
credential rotation and cancellation, canary, and rollback, and must establish
public-edge security plus provider account and quota evidence. The bounded
canary adds only one command run's provider execution and complete publication
evidence; it does not close any of those deployment gates.
Phase 9D may
enable `public_attendee_sessions` only after those backend gates and the UI-owned
same-origin cookie, CSRF, Origin, Host, browser-DTO, public-throttle, and
explicit browser-recovery gates pass together.

## Maintenance

Update this document with `src/dreamland/product/attendee.py`,
`src/dreamland/product/attendee_security.py`,
`src/dreamland/product/attendee_live.py`,
`src/dreamland/product/attendee_provider.py`,
`tools/product_attendee_security_preview.py`,
`tools/product_attendee_security_live.py`,
`tools/product_attendee_security_contract.py`,
`tools/product_attendee_security_live_contract.py`,
`tools/product_attendee_security_live_identity_contract.py`,
`tools/product_attendee_security_deployment.py`,
`tools/product_attendee_security_deployment_contract.py`,
`tools/product_attendee_security_deployment_rehearsal.py`,
`tools/product_attendee_security_deployment_canary.py`, Product
`v0.8.0-rc.1`, immutable `v0.8.0-rc.2`, corrective `v0.8.0-rc.3`, and
supervised `v0.8.0-rc.4`, their owning tests, decisions 0075 through 0082,
`docs/PRODUCT_SESSION_API.md`,
`docs/ARCHITECTURE.md`, `docs/TESTING.md`, and `ROADMAP.md` whenever the private
attendee target, logical admission, revocation, route allowlist, authority,
provider isolation, availability, cutoff, capacity, drain/shutdown, signal,
rotation, release-candidate identity, or production gate changes.
