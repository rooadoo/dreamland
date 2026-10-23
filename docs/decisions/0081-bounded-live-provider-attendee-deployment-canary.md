# Bounded Live-Provider Attendee Deployment Canary

Status: Active
Audience: DREAMLAND maintainers, deployment owners, UI BFF developers, and
coding agents
Purpose: record the explicit single-command Phase 9C live-provider evidence
gate and the production claims that it deliberately does not make.

## Overview

The provider-free Phase 9C rehearsal proves the supervised Product
`v0.8.0-rc.4` process, transport, capacity, cutoff, recovery, cancellation,
signal, and restart boundaries without reading a provider credential. It
cannot show that a deployment-selected credential and model can complete a
real provider-backed attendee command or that all output reaches the private
client.

A broad live test would mix that narrow question with load, public-edge,
browser-security, provider-account, quota, cancellation, billing, and rollout
claims that the current development target cannot support. The live check must
therefore be explicit, bounded, private, and honest about its evidence.

## Decision

Add `tools/product_attendee_security_deployment_canary.py` as a separate,
opt-in operator command. It reuses the exact immutable Product
`v0.8.0-rc.4` contract and generated client; it does not change a schema,
fixture, capability, route, error, or release-candidate byte.

The `run` command requires `--acknowledge-provider-spend`, the exact authored
profile path and canonical SHA-256 digest, a current-user-owned regular
provider credential file with mode `0600`, and explicit provider model and
maximum-output settings from 1 through 8,192. This canary-only bound limits the
one authorized provider response and leaves the supervised target default and
range unchanged. Optional provider settings use the same
validation as the supervised target. Local inputs and the pinned RC4 byte/tree
identity are validated before provider work.

The canary owns disposable private transport/bootstrap material and an
explicit supervised child. Binding is fixed to numeric loopback on an
ephemeral port. Worker capacity is one, the per-session command limit is one,
the submitted command is exactly `uname -a`, and the workflow permits one
logical provider attempt with no automatic retry or redispatch. The canary's
1-through-8,192 token bound is independent of its publication limit. It
alone fixes an effective maximum of 240 output chunks per command; the
supervised target default remains unchanged. This keeps accepted-cursor replay
inside the 256-event journal after its running event, optional accepted state,
two final publications, and terminal overhead. Provider
credential content is read only inside the spawned provider worker. Importing
the canary starts no listener, process, timer, worker, credential read, or
provider client, so ordinary DREAMLAND and SSH operation incur no cost.

The generated RC4 client performs context bootstrap, reviewed live launch
selection, session creation, command submission, terminal reconciliation,
session close, and context revocation. Success requires reconstruction of the
complete, unclipped event stream and transcript, agreement with authoritative
terminal result and snapshot truth, backend `attendee_context_revoked`, and
bounded child shutdown. Failures are safe and typed without publishing private
provider bodies, credentials, authorities, paths, profile content, command
output, or state.

The allowlisted report identifies live-provider execution for only this
single-command run and always sets `production_capability_evidence=false`.
Settled
token and spend usage must exactly equal the announced full reservations. The
report states `output_bytes_per_command=65536` and
`output_chunks_per_command=240`. This is conservative settlement, not actual
provider token consumption or billing truth.

The backend wire and recovery contracts remain unchanged. The BFF alone may
map a deliberately ended browser session to `access-ended`; DREAMLAND revoke
success remains `attendee_context_revoked`. After process restart, stale reads
remain `session_lost`, stale mutations and revokes remain `outcome_unknown`,
and new work requires explicit context-start. The canary does not remint,
rebase, retry, or redispatch work across an epoch.

## Consequences

A successful canary is evidence that the configured owner-controlled,
loopback RC4 process accepted the selected credential/model for one logical
attempt, completed one provider-backed attendee command, published its complete
private terminal evidence, and converged through cleanup at that time.

It is not evidence of actual provider billing or token truth, upstream
request-count instrumentation, provider-account identity or hard-quota
separation, provider-key rotation, upstream cancellation or billing
termination, deployed load or abuse capacity, rollout/rollback, sustained
provider availability, an internet/public edge, UI/BFF cookie, CSRF, Origin,
Host, DTO, throttle, or browser-recovery security, or production capability
readiness. None of the five Product production capabilities is enabled.

## Evidence

Provider-free automated tests inject the finite local canary fixture. They
cover acknowledgement and argument validation, exact RC4 and profile identity,
credential metadata, fixed limits and command, one-attempt enforcement,
complete and unclipped event/transcript reconciliation, terminal consistency,
exact settled token/spend equality with the announced reservations, report
privacy, close/revoke, bounded shutdown, inert import, and failure
cleanup without reading a live credential or making an upstream call.

The real provider command is a separate human-authorized gate and may incur
provider spend through its one logical attempt. Its successful report is
retained only as the narrow live evidence described above. Provider invoices,
account configuration, quota controls, and external request telemetry require
independent evidence. The report's `contract.backend_commit` identifies the
implementation bound into immutable RC4, not the canary runner; retain the
invoking Git revision and dirty-state status beside any report used as
evidence.

## Maintenance

Update this record with
`tools/product_attendee_security_deployment_canary.py`, its owning fixture and
tests, `tools/product_attendee_security_deployment.py`, Product
`v0.8.0-rc.4` immutability checks,
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`,
`docs/PRODUCT_SESSION_API.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`, and
`ROADMAP.md` whenever the canary's spend acknowledgement, fixed workload,
provider boundary, publication validation, report schema, cleanup, evidence
claims, or unchanged deliberate-end/restart semantics change.
