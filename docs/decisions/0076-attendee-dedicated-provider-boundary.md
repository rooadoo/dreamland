# Attendee-Dedicated Provider Boundary

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the Phase 9B live attendee provider, process-isolation,
credential, capacity, shutdown, and recovery boundary without enabling a
production capability.

## Overview

Phase 9A provides the provider-free attendee-only private transport, context
revocation, and replay-safe logical admission needed by the UI-owned BFF. It
does not prove that live provider work can be isolated from Presenter Stage,
World Forge, ordinary SSH, or other DREAMLAND roles, and a Python thread cannot
forcibly contain a provider SDK call that stops cooperating.

Phase 9B adds a production-shaped live collaborator behind that same private
attendee boundary. The checkpoint is still loopback/development-only. It is
evidence about one local process boundary and its finite accounting, not an
internet deployment, an upstream cancellation guarantee, or authority to
enable `public_attendee_sessions`.

## Decision

Add `AttendeeProviderPool` as a fixed-capacity pool of child processes created
only with Python's `spawn` start method. The parent admits one command into one
available worker and communicates through a bounded, versioned JSON protocol.
Per-command IPC sends only the command DTO and immutable runtime document
required by the attendee executor. Product bearers, actor contexts, launch or
session authority, idempotency keys, provider credentials, paths, and caller
callbacks do not cross that per-command boundary. The frozen spawn factory
separately carries the validated credential path and exact authored revision
needed to construct the child, but never carries the credential value.

Do not construct a provider in the parent. A frozen worker factory carries one
canonical provider/model configuration and one exact verified authored
revision, but no API key. The parent validates that the dedicated credential is
a bounded, owner-current-UID, exact-mode-`0600`, regular, non-symlink file
without reading its contents. Each spawned child repeats those metadata checks,
reads exactly one nonblank UTF-8 key, and constructs its own attendee command
agent. There is no fallback to generic DREAMLAND settings, ambient provider
keys, or an ordinary SSH, Enrichment, Stage, or Forge provider.

Reject provider and observability credential environment variables before
starting the pool, and scrub and reject them again in each child. External
tracing is forced off, callbacks are empty, and token/resource logging receives
a redacted command marker rather than attendee command text. Provider retries
are pinned to zero so one admitted Product attempt cannot become an SDK retry
sequence. These restrictions apply only to this explicitly invoked attendee
composition; existing DREAMLAND provider construction retains its defaults.

Start every configured worker and wait for all of them before binding or
announcing the listener. `ready` means only that the local child constructed
its executor and provider client. It does not prove upstream reachability,
authentication, quota, model availability, or billing state. Replacement
workers use the same bounded startup path. The configured dispatch timeout
must cover the worker startup timeout plus both bounded retired-child
containment phases so an already accepted command is not failed merely because
its replacement is still within the advertised startup window.

Keep command, state, output-chunk, total-output, output-count, and IPC-frame
limits finite. Product core and pool must use the same runtime-document byte
and structural policy; validate the initial authored document and every
proposed committed state through that policy, or a state accepted by the core
could poison its next dispatch. Each live request has one
finite maximum-token and spend reservation. Work confirmed before its provider
deadline reconciles from authoritative usage when available. A worker-response
loss, protocol loss, timeout, forced worker, or otherwise ambiguous dispatch
consumes the full reservation. Decision 0080 refines the exact-deadline edge:
cancellation already observed at that instant retains interrupted/actual-usage
truth, while a deadline observed first remains a full-reservation deadline
failure even when the child then returns cooperatively. Derive the retained
logical-job ceiling from active-session
capacity times the configured per-session command maximum, and scale token and
spend budgets with it; the default is 256 process-lifetime jobs rather than a
hidden eight-command quota. The pool never redispatches a command automatically.

Cancellation and deadlines first fence DREAMLAND publication, then retire and
locally terminate a worker that cannot complete cooperatively. This prevents
late output or provisional state from becoming accepted Product truth. Killing
the child does not prove that an already dispatched upstream request stopped or
that upstream billing ceased. Deployment accounting therefore remains
conservative, and a production service still requires an external supervisor
deadline around this in-process pool.

Shutdown is a required two-level sequence. First call the attendee service and
provider pool drain gates to reject fresh bootstrap, exchange, create, command,
reset, and switch work while retaining replay/conflict classification and
cleanup/recovery operations. Then invoke bounded service shutdown and pool
`shutdown`; `begin_drain` alone is not a process-termination API. Do not report
the pool closed while a child or owned controller survives its shutdown bound.

Add `tools/product_attendee_security_live.py` as the explicit composition. It
pins one exact immutable authored revision, constructs the attendee-only
facade/core and the fixed provider pool, waits for complete local readiness,
and exposes only the Phase 9A attendee route allowlist on loopback. It contains
no Presenter Stage, X-ray, World Forge, World Access, Operator, admin, MCP,
profile-path, or generic launch authority. Importing the tool or any Phase 9B
module starts nothing, and normal DREAMLAND startup does not import or
construct this composition.

Create Product `v0.8.0-rc.2` as an unfrozen identity-only successor to RC1.
Preserve the exact attendee route, request, response, revocation, authority,
idempotency, and recovery semantics while truthfully identifying provider-
backed accepted attendee commands. Keep RC1 immutable and keep all five
production capabilities disabled. Do not publish RC2 until its manifest can
bind the exact tested implementation commit and predecessor immutability has
been revalidated.

Preserve the deliberate context-end and restart boundary. The backend revoke
success remains `attendee_context_revoked`; DREAMLAND does not add an
`access-ended` wire error. The UI/BFF owns its typed `access-ended` browser
result and ended-context fence. A stale read after process restart remains
`session_lost`, while a stale mutation or revoke remains `outcome_unknown`.
Neither side automatically rebases, bootstraps, remints authority, retries with
a different identity, or redispatches work across restart. The UI must present
restart separately and perform an explicit context-start before new work.

The UI-owned BFF continues to own browser authentication, cookies, CSRF,
Origin and Host validation, browser DTOs, public-edge and network throttling,
security headers, and browser recovery policy. No DREAMLAND credential or
authority enters browser code.

## Consequences

The UI team can exercise a live authored attendee session against a locally
contained and finitely accounted provider pool without exposing the combined
Forge/Presenter service or changing ordinary DREAMLAND behavior. A wedged SDK
call can no longer keep a reusable in-process worker thread alive or publish
late Product truth after the pool fences and replaces its child.

The process boundary is not an upstream service boundary. Local readiness is
not a provider health check, distinct credential-file handling is not proof of
a distinct provider account or quota, and local worker termination is not proof
of upstream cancellation or billing termination. Those claims require
deployment and provider evidence in Phase 9C.

Phase 9B does not add durable recovery, generated-world attendee composition,
an internet listener, public identity, distributed admission, deployment
secret distribution, account/quota proof, load evidence, a security review,
credential rotation, canary, rollback, or capability enablement. It also does
not weaken the typed deliberate-end or explicit restart recovery contracts.

## Evidence

Provider-free owning tests cover inert import and construction, credential
metadata and child-only loading, forbidden-environment handling, exact authored
revision confinement, tracing and command-log privacy, zero retries, spawn-only
worker creation, complete readiness, finite IPC bounds, thirteen-plus command
admission, output, cancellation, timeout, controller-event failures, surviving-
child tracking, forced replacement, conservative reservations, no redispatch,
drain, and bounded shutdown. Composition and contract tests cover the attendee-
only allowlist, exact core/pool byte-and-structure state policy, complete
replacement dispatch bound, safe startup output, exact implementation-commit
binding, disabled capabilities, RC1 immutability, and unchanged revocation/
restart wire behavior.

Phase 9C still owns end-to-end deployment, load, restart, saturation, malformed-
traffic, cancellation, privacy, credential-rotation, canary, rollback, and
provider-account/quota evidence. Phase 9D may enable
`public_attendee_sessions` only after those backend gates and the UI-owned
same-origin browser gates pass together.

## Maintenance

Update this record with `src/dreamland/product/attendee.py`,
`src/dreamland/agent_core.py`, `src/dreamland/model_providers.py`,
`src/dreamland/product/attendee_live.py`,
`src/dreamland/product/attendee_provider.py`,
`src/dreamland/product/runtime_executor.py`,
`tools/product_attendee_security_preview.py`,
`tools/product_attendee_security_live.py`,
`tools/product_attendee_security_live_contract.py`, Product
`v0.8.0-rc.2`, their owning tests,
`docs/decisions/0080-attendee-provider-exact-deadline-containment.md`,
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`,
`docs/PRODUCT_SESSION_API.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`, and
`ROADMAP.md` whenever the credential, provider, capacity, drain/shutdown,
recovery, private-route, or capability boundary changes.
