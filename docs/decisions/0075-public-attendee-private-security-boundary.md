# Public Attendee Private Security Boundary

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the Phase 9A context-revocation, logical-admission, private
transport, and compatibility boundary without claiming an internet-facing
attendee capability.

## Overview

Phase 8B proves a complete live Forge-to-attendee development flow, but its
combined loopback process deliberately carries Forge, Operator, Presenter,
X-ray, and provider authority. It is useful evaluation evidence, not an
appropriate target for a public-attendee BFF. Before production deployment can
be considered, DREAMLAND needs a smaller private trust boundary and explicit
server-side controls for browser-context teardown and replay-safe logical
admission.

Browser security remains a UI/BFF responsibility. DREAMLAND does not receive
browser cookies, CSRF tokens, client IP addresses, or Origin/Host policy, and
the browser never receives DREAMLAND bearer, actor, launch, session, or
revocation authority.

## Decision

Add protocol-neutral attendee context revocation to
`PublicAttendeeSessionService`. A private BFF must present its transport-held
authority, the exact gateway-context identifier, the actor context bound to
that lease, expected service epoch evidence, and an idempotency key. A valid
operation binds its claim to digests of both context values, fences the context
lease, attendee actor, issued grants, active sessions, commands, output, and
accepted-state publication, and returns only a small revocation outcome. It
does not echo a context, actor, session, or credential.

Resolve an exact revocation replay before examining the now-fenced context so
a lost success response remains recoverable. Changed-body reuse after a
successful claim conflicts. A fresh request for a missing, mismatched, expired,
or already fenced actor/context pair receives the same `authority_denied`
response; it cannot probe context existence and remains unclaimed. Reserving
those invalid attempts would let unique mismatch keys exhaust the safety path.
Policy instead guarantees one successful replay slot per burnable context and
retains that bounded success claim through the private BFF authority lifetime.
Restart still rotates the service epoch and returns `outcome_unknown` for an
old uncertain mutation rather than rebasing it.

Add an optional, protocol-neutral logical-request admission ledger. It uses a
trusted monotonic clock and only a SHA-256 digest of the opaque gateway context,
plus a process-wide counter. It has no IP, cookie, HTTP, provider, or browser
knowledge and retains a bounded number of expiring windows. The ledger is not
constructed by existing RC4 compositions or normal DREAMLAND startup.

Charge only fresh operations that issue authority or create work: context
bootstrap, either launch-selection exchange, session create, command submit,
reset, and switch. Resolve an existing idempotency claim, including an exact
replay or changed-body conflict, before attempting a new charge. A temporary
`logical_rate_limited` rejection is retryable, creates no downstream
idempotency claim, and can be retried after the supplied delay. Reads, command
cancellation, session close, context revocation, expiry, shutdown, and
reconciliation do not consume this quota and remain available when the fresh-
work budget is exhausted.

Publish Product `v0.8.0-rc.1` as an additive, unfrozen release candidate. Keep
every RC4 operation and add only the private context-revocation operation and
the logical-rate error required by this boundary. Preserve all five production
capabilities as disabled. Product RC4 and World Access RC4 remain immutable;
the new release candidate does not change their published identity or make a
World Access compatibility claim.

Add `tools/product_attendee_security_preview.py` as a separate, explicitly
started provider-free integration target. It composes only an authored attendee
selection registry, attendee facade, shared session core, bounded deterministic
workers, and the logical-admission ledger. Its route allowlist excludes Forge,
World Access, Presenter Stage, X-ray, admin, MCP, provider construction, profile
path lookup, and ordinary runtime registration. The listener is loopback-only,
uses a distinct owner-only bearer and bootstrap, validates the new contract
before serving, and reports no enabled or live production capability.

The private attendee target is still behind the UI-owned same-origin BFF. The
BFF owns browser authentication, HttpOnly/Secure/SameSite cookie policy, CSRF,
Origin and Host validation, browser DTOs, public-edge rate limits, client-IP
policy, security headers, and browser recovery deadlines. The BFF maps its
authenticated browser session to a high-entropy server-side gateway context,
retains all DREAMLAND authority server-side, and invokes revocation when that
mapping is deliberately ended. DREAMLAND's logical ledger supplements those
controls; it does not replace them.

## Consequences

The UI team can integrate and test the private security lifecycle without a
provider, a generated-world publication workflow, or access to privileged
product surfaces. Saturation cannot prevent a user or operator from cancelling,
closing, revoking, or reconciling existing work. Exact mutation retries do not
double-charge the logical budget, and retryable rate denial cannot reserve an
idempotency key accidentally.

Revocation is immediate at DREAMLAND's accepted-truth boundary, while physical
provider interruption remains cooperative in a later provider-backed
composition. Late worker output or state cannot reopen a fenced command or
session. Process-local state still means restart loses sessions and context
mappings; durable resume is not introduced here.

This phase does not enable `public_attendee_sessions`, expose an internet
listener, accept browser credentials, call a provider, establish deployment
isolation, prove load or abuse capacity, implement durable state, or satisfy
the public presentation gate. Phase 9B and later work must separately define a
production-shaped provider collaborator, deployment topology, hard deadlines,
readiness/drain, security and saturation evidence, canary/rollback, and the
intentional capability transition.

Normal `python -m dreamland`, SSH, admin, MCP, state-profile, logging, and
EnrichmentAgent paths remain outside this composition. Unless the preview is
explicitly invoked, this decision adds no listener, worker, timer, provider,
registry, admission counter, request hook, or bookkeeping cost.

## Evidence

Owning tests cover exact revocation replay, post-success changed-body conflict,
actor/context binding, uniform unclaimed post-revocation denial, mismatch-flood
resistance, command and session fencing, bounded successful revocation claims,
per-context and global logical limits, window rollover,
replay-before-charge, retryable unclaimed denial, and unthrottled safety and
recovery operations. Transport tests cover the attendee-only allowlist,
privileged-route denial, loopback and credential restrictions, startup
redaction, complete one-request responses, bounded connection/body/time policy,
and inert import.

Contract validation must preserve the published RC4 tree, validate every new
fixture and exact route, compile and exercise the server-only generated client,
and bind the release candidate to the exact tested implementation commit before
publication. Repository tests must continue proving that existing Product,
World Access, live development, SSH, admin, MCP, and ordinary runtime paths are
unchanged.

## Maintenance

Update this record with `src/dreamland/product/attendee.py`,
`src/dreamland/product/attendee_security.py`,
`tools/product_attendee_security_preview.py`,
`tools/product_attendee_security_contract.py`, Product `v0.8.0-rc.1`, their
owning tests, `docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`,
`docs/PRODUCT_SESSION_API.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`, and
`ROADMAP.md` whenever the revocation, logical-admission, private-route,
authority, compatibility, or production-gate boundary changes.
