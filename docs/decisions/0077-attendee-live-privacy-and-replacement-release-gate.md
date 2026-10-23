# Attendee Live Privacy And Replacement Release Gate

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the corrective Phase 9B privacy identity and reliable worker-
replacement release gate while preserving the private attendee authority and
recovery boundary.

## Overview

Published Product `v0.8.0-rc.2` truthfully reports provider-backed command
execution, but its live composition inherited Phase 9A's
`notice:attendee-security-preview-v1` and
`policy:attendee-security-preview-v1`. Those identities describe the
provider-free private lifecycle preview. A UI choosing its disclosure from the
returned launch option could therefore present provider-free claims before a
command was sent to a configured provider.

The Phase 9B worker replacement implementation also had tests that waited a
fixed three seconds for the pool to return to `ready`. The test pool permits an
eight-second worker-startup window. Spawn scheduling can legitimately exceed
the unrelated three-second test delay, so the release gate could fail even
while replacement remained inside its configured bound. This finding does not
justify weakening a runtime timeout or publishing readiness before the whole
pool is ready.

## Decision

Keep the published `v0.8.0-rc.2` tree byte-for-byte immutable. Publish
`v0.8.0-rc.3` as the corrective Phase 9B identity. RC3 preserves all 25
method/path operations, request and response schemas, authority classes,
idempotency behavior, context revocation, epoch fencing, and disabled
capability truth from RC2.

Make notice version and policy ID explicit inputs to the private attendee
composition. Phase 9A keeps its provider-free defaults. The explicitly started
Phase 9B live composition selects the existing reviewed provider-backed pair:

```text
notice:attendee-live-development-v1
policy:attendee-live-authored-v1
```

The launch option carries the live notice version. Exchange requires the exact
returned notice, and session creation retains both live identities in the
launch binding. A Phase 9A notice must fail against the live offering instead
of aliasing it. This is server-selected privacy and policy identity; it does
not expose a path, provider credential, model authority, or private capability
to the browser.

Keep worker replacement governed by the existing pool lifecycle and configured
deadlines. Tests that need replacement recovery derive their wait from the
pool's `startup_timeout_ms + (2 * terminate_grace_ms)` and add a one-second
test scheduler margin. They fail early if the pool reaches `failed`,
`draining`, or `closed`, and accept recovery only when lifecycle is `ready`,
ready and available workers equal capacity, active workers are zero, and the
focused cancellation and concurrent-failure cases complete a follow-up
dispatch. This is a test synchronization bound, not a larger runtime timeout.
Repeat the concurrent-timeout and cancellation-timeout replacement cases in
fresh processes as a pre-publication stress gate.

Preserve the deliberate access-end and restart distinction. DREAMLAND revoke
success remains `attendee_context_revoked`; `access-ended` remains a typed
UI/BFF result for a deliberately ended browser mapping, not a DREAMLAND wire
error. A stale read after restart remains `session_lost`; a stale mutation or
revoke remains `outcome_unknown`. Neither layer automatically rebases,
bootstraps, remints authority, retries under a new identity, or redispatches.
New work crosses the fence only through explicit context-start.

RC3 remains an explicit-start, loopback-only development target. It composes
no Stage, X-ray, Forge, World Access, Operator, admin, or MCP authority. All
five production capabilities remain disabled, and normal DREAMLAND/SSH startup
constructs no Product listener, provider pool, worker, or admission ledger.

## Consequences

The UI can select a disclosure that truthfully covers configured-provider
processing before exchanging a live attendee launch option. The provider-free
Phase 9A target retains its existing identity and behavior. Because RC2 is not
rewritten, integrations and provenance checks against its published bytes
remain reproducible; consumers opt into the correction by selecting RC3.

Replacement tests now distinguish scheduler variance inside the configured
startup window from an actual terminal or incomplete pool recovery. Runtime
containment, cancellation, dispatch, accounting, and shutdown deadlines are
unchanged. Local worker termination still fences DREAMLAND publication but
does not prove upstream cancellation or billing termination.

This correction does not provide an internet listener, deployment isolation,
durable restart recovery, distributed admission, provider-account or quota
proof, load evidence, security assessment, credential rotation, canary,
rollback, or capability enablement. Those remain Phase 9C and Phase 9D gates.

## Evidence

Provider-free composition tests pin the unchanged Phase 9A privacy identities.
Live composition tests perform context bootstrap, option listing, old-notice
rejection, exact live-notice exchange, session creation, and exact launch-
binding notice/policy verification. Contract tests pin the complete RC2 tree,
validate RC3 and its generated client, preserve the RC2 routes and recovery
shapes, reject a backend `access-ended` addition, and keep all capabilities
disabled.

Provider-pool tests cover concurrent worker timeouts and cancellation-timeout
replacement using lifecycle-derived synchronization. The publication gate
runs those focused cases repeatedly in fresh processes before the complete
provider-free Phase 9B suite, contract validation, and client smoke.

## Maintenance

Update this record with `tools/product_attendee_security_preview.py`,
`tools/product_attendee_security_live.py`,
`tools/product_attendee_security_live_contract.py`,
`tools/product_attendee_security_live_identity_contract.py`,
`src/dreamland/product/attendee_provider.py`, Product `v0.8.0-rc.2` and
`v0.8.0-rc.3`, their owning tests,
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`,
`docs/PRODUCT_SESSION_API.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`, and
`ROADMAP.md` whenever privacy selection, release-candidate immutability,
replacement readiness, deliberate access-end, explicit restart, or capability
truth changes.
