# Supervised Attendee Availability And Restart Boundary

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the Phase 9C exact availability, supervised shutdown, and
restart-only credential-rotation boundary without enabling a production
capability.

## Overview

The Phase 9B attendee-only live target is explicitly started, loopback-only,
and finitely bounded, but its availability window is derived from process
startup and its command-line service path handles only an interactive
interrupt. It therefore cannot establish an operator-selected event cutoff,
no-request expiry, bounded termination-signal convergence, or restart-only
credential rotation as deployment evidence.

The UI/BFF already owns the typed `access-ended` browser result and presents a
process restart as a separate user-visible boundary. Phase 9C must strengthen
the backend deployment lifecycle without replacing either contract or adding a
new DREAMLAND wire error.

## Decision

Keep Product `v0.8.0-rc.1` through `v0.8.0-rc.3` byte-for-byte immutable. Add
Product `v0.8.0-rc.4` as an identity-only Phase 9C successor. RC4 preserves the
same 25 method/path operations, schemas, fixtures, live-provider notice and
policy identities, authority classes, revocation result, idempotency behavior,
and restart recovery semantics. All five production capabilities remain
disabled.

Add an optional immutable availability window to the attendee-only composition:

```text
admission_opens_at_ms < admission_closes_at_ms < recovery_closes_at_ms
```

The provider-free Phase 9A and dedicated-provider Phase 9B defaults remain
unchanged when no window is supplied. The explicitly invoked Phase 9C target
requires exact UTC values. Construction propagates one resolved window to the
launch offering, attendee actor authorities, private BFF authority, and
attendee admission policy before any request is accepted. The attendee service
projects those UTC boundaries to its existing monotonic deadline domain, so a
backward wall-clock adjustment cannot extend admission.

The deployment host must synchronize UTC before startup. The explicit process
captures UTC once and advances both of its wall-shaped/deadline values from
monotonic elapsed time. A later system wall-clock adjustment therefore does not
move the active process's window; changing the anchor requires a reviewed
process restart.

Add `tools/product_attendee_security_deployment.py` as a new explicit
loopback-only deployment target. Importing it starts no listener, timer,
provider, pool, worker, credential load, or ordinary DREAMLAND composition.
After every local provider worker is ready and before listener publication, it
constructs the RC4 service with the exact window. If admission or recovery has
already ended during startup, startup rolls back instead of announcing a ready
listener.

The deployment harness owns a no-request cutoff watcher. At the admission
cutoff it runs coordinated expiry to terminalize due sessions and fence active
command output and state publication, then begins the existing service and
provider-pool drain. The private listener remains available during the bounded
recovery interval so the BFF can read terminal truth and complete allowed
cleanup/reconciliation. Fresh bootstrap, exchange, create, command, reset, and
switch work remains closed. The watcher never invents an `access-ended`
response.

Because watcher scheduling cannot itself make a timestamp atomic, the Phase
9C composition also opts into a service-level bootstrap cutoff. Before open,
bootstrap remains available for private BFF preparation. At or after admission
close, exact claimed retries replay and changed bodies conflict, but a new
bootstrap key is rejected before logical admission or context-slot allocation.
The Phase 9A/9B composition default remains unchanged.

The main thread supervises both the recovery deadline and `SIGINT`/`SIGTERM`.
Signal handlers record a stop request only; they do not acquire service locks,
close sockets, or mutate provider state. The single teardown owner drains
fresh work, stops accepting connections, joins admitted request handlers,
shuts down the attendee service, performs the provider pool's bounded
cooperative/TERM/KILL convergence, and finally joins task threads. Scheduled
recovery completion exits normally; interrupt and termination signals retain
distinct process results. Startup output reports only safe window, model,
capacity, privacy, signal, and shutdown-bound facts.

Credential and authority rotation is stop/replace/start only. The operator
stops the old process, waits for bounded convergence, replaces owner-only
transport, bootstrap, or provider credential files while the process is down,
and starts a new process with a new service epoch. There is no SIGHUP reload,
automatic authority remint, epoch rebase, new-identity retry, or provider
redispatch. An old read after restart remains `session_lost`; an old mutation
or revoke remains `outcome_unknown`; new work requires an explicit UI/BFF
context-start.

The target remains a private loopback collaborator. The UI-owned BFF continues
to own public HTTPS, browser authentication, cookies, CSRF, Origin and Host
checks, browser DTOs, public throttling, the externally visible closed state,
and the typed deliberate `access-ended` response. Phase 9C does not add an
internet listener or enable `public_attendee_sessions`.

## Consequences

An operator can configure one exact event window and obtain backend admission
fencing even when no request happens at the cutoff. Active DREAMLAND work
cannot publish after the coordinated fence, while the BFF retains a finite
interval for authoritative terminal recovery. Termination and credential
rotation become explicit, testable process boundaries instead of assumptions
about an interactive shell.

Local provider readiness still does not prove upstream authentication, quota,
model availability, or billing state. Local worker termination still does not
prove an upstream request or its billing stopped. This checkpoint also does
not provide deployment load, abuse, malformed-public-traffic, canary,
rollback, provider-account/quota, or public-edge evidence. Those remaining
Phase 9C gates and the UI-owned browser gates must pass before Phase 9D may
enable a production capability.

## Evidence

Provider-free tests cover window validation and exact propagation, unchanged
Phase 9A/9B defaults, future-open and cutoff behavior, no-request expiry,
active-work publication fencing, inert import, loopback confinement, safe
startup output, signal convergence, request-thread joining, restart-only
rotation semantics, and all-disabled capabilities. Contract tests pin the
published RC3 bytes, require RC4's exact wire equivalence and live privacy
identity, reject a backend `access-ended` addition, and bind publication to the
exact Phase 9C implementation commit.

## Maintenance

Update this record with `tools/product_attendee_security_preview.py`,
`tools/product_attendee_security_live.py`,
`tools/product_attendee_security_deployment.py`,
`tools/product_attendee_security_deployment_contract.py`,
`tools/product_attendee_security_deployment_rehearsal.py`, Product
`v0.8.0-rc.4`, their owning tests,
`docs/decisions/0079-provider-free-attendee-deployment-rehearsal.md`,
`docs/decisions/0080-attendee-provider-exact-deadline-containment.md`,
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`,
`docs/PRODUCT_SESSION_API.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`, and
`ROADMAP.md` whenever the availability, cutoff, recovery interval, signal,
credential rotation, restart, private-route, or capability boundary changes.
