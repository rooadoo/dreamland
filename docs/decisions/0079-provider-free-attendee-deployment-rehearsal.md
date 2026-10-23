# Provider-Free Attendee Deployment Rehearsal Evidence

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: define the Phase 9C private-boundary rehearsal and the evidence it may
produce without treating deterministic local work as production capability
proof.

## Overview

The Phase 9C supervised attendee target now has exact admission and recovery
windows, signal-owned teardown, joined request handlers, and restart-only
credential policy. Its owning tests prove those components in isolation, but
they do not yet exercise one complete RC4 client-to-process path under bounded
local contention, unread-body desynchronization traffic, cancellation, cutoff,
and restart.

That missing evidence cannot be obtained safely by spending real provider
quota in CI, exposing the private listener to the internet, or adding a fixture
mode to ordinary DREAMLAND startup. It also cannot be represented as evidence
for the disabled `public_attendee_sessions` capability.

## Decision

Add a separately invoked, provider-free Phase 9C deployment rehearsal. The
rehearsal starts the real `AttendeeSecurityDeploymentServer`, real bounded HTTP
transport, real attendee/core services, real cutoff watcher, real process
supervisor, and real fixed worker-process pool on loopback. Only the provider
executor is replaced with a finite deterministic collaborator. The published
Product `v0.8.0-rc.4` contract and generated client remain byte-for-byte
immutable.

Use the exact RC4 generated server-side client for every well-formed Product
operation. Use raw loopback sockets only for traffic that the generated client
correctly refuses to construct: an unsupported-media request whose unread
declared body resembles a pipelined request, and deliberately stalled
incomplete headers for connection saturation. Private workflow values remain
confined to owner-private fixture state and loopback traffic, never enter the
public report, and are discarded after validation.

The checkpoint certifies the current local boundary of four attendee contexts,
four active sessions, two provider workers, and eight HTTP connections. It
exercises replay and changed-body conflict, context and session capacity,
worker saturation and cancellation, recovery operations during saturation,
unread-body and stalled-header traffic, admission cutoff, signal convergence,
and stop/replace/start transport-authority rotation. A restart creates a new
epoch; stale reads remain `session_lost`, stale mutations remain
`outcome_unknown`, old transport authority is rejected, and new work still
requires explicit context bootstrap. The fixture supplies a one-byte blank
owner-only provider file that passes parent metadata validation but would fail
the live content loader. Successful execution therefore proves that the
deterministic worker path did not read credential content; it does not prove
provider-key consumption or rotation.

Any request-body rejection that may leave declared bytes unread must close the
HTTP/1.1 connection. This prevents unread body bytes from being reinterpreted
as a pipelined request. Fully consumed invalid JSON may use ordinary response
handling because no ambiguous request bytes remain. Under full connection
saturation, the advertised shutdown bound includes both the deadline that can
release an occupied handler slot and the deadline for an already accepted
waiting connection after that slot becomes available.

The black-box cutoff case also exposed the provider deadline/cancellation race
recorded in decision 0080. Cancellation already visible at the exact deadline
wins and retains interrupted/actual-usage truth. If the deadline is observed
first, the pool fences child output, signals cooperative cancellation, and
grants only the existing bounded cancellation grace. A confirmed return is a
completed, non-ambiguous dispatch on a reusable worker, but its pool-internal
result remains `provider_deadline_exceeded` with the full reservation charged;
a child that does not return remains ambiguous, locally contained, and fully
charged. The unchanged RC4 terminal projection remains
`expired`/`absolute_expired` with command reason `session_terminated`.

On success the rehearsal emits one allowlisted, machine-readable report. It may
contain only the public contract identity and hashes, fixed successful case
identifiers, allowlisted public error codes, bounded durations and counts,
resolved public limits, signal exit statuses, and resource-convergence facts.
On failure it exits nonzero and emits only a generic safe error to stderr. A
successful report must state:

```text
scope = loopback_provider_free
production_capability_evidence = false
```

The report must not contain a bearer, BFF/actor/session/grant authority,
gateway-context or idempotency value, request ID, command or output, runtime
state, provider setting, profile identity or digest, private path, port, or a
hash derived from any of those values. A final recursive allowlist and sentinel
scan runs before serialization.

Importing the rehearsal module starts no listener, worker, process, timer,
credential read, provider client, or ordinary DREAMLAND composition. Running
normal DREAMLAND, SSH, admin, MCP, state profiles, or EnrichmentAgent never
imports or invokes it.

## Consequences

Maintainers and the UI team gain a repeatable black-box RC4 integration gate
without provider spend or a web-UI contract change. Failures can be localized
to transport, authority, admission, cancellation, cutoff, recovery, or restart
semantics while the report remains safe to retain in CI.

This evidence proves only the deterministic loopback boundary. It does not
prove a public edge, BFF cookie/CSRF/Origin/Host behavior, public throttling,
multi-instance or durable recovery, real deployment load, canary or rollback,
provider reachability, provider account separation, upstream quota,
cancellation, billing termination, provider-key rotation, or production
capability readiness. Those remain Phase 9C gates before Phase 9D can enable
anything.

## Evidence

Owning tests run the rehearsal as a real subprocess, validate its complete
safe report, exercise its high-contention cases, verify unread-body rejection
closes the connection before pipelined bytes can be reparsed, and assert clean
signal/process convergence. Existing RC4 contract validation and hash checks
prove the rehearsal changed no published artifact.

## Maintenance

Update this record with
`src/dreamland/product/attendee_provider.py`,
`tools/product_attendee_security_deployment_rehearsal.py`,
`tools/product_attendee_security_deployment.py`,
`tools/product_lifecycle_preview.py`, their owning tests,
`docs/decisions/0080-attendee-provider-exact-deadline-containment.md`,
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`, `docs/TESTING.md`,
`docs/ARCHITECTURE.md`, `docs/PRODUCT_SESSION_API.md`, and `ROADMAP.md` whenever
the rehearsal scope, public limits, evidence allowlist, unread-body and
stalled-header probe policy, shutdown accounting, or remaining production
gates change.
