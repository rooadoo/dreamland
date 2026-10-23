# Bounded Attendee Deployment Capacity Envelope

Status: Active
Audience: DREAMLAND maintainers, deployment owners, UI BFF developers, and
coding agents
Purpose: record the opt-in Phase 9C attendee capacity controls, their derived
resource ceilings, and the evidence they do and do not provide.

## Overview

The supervised attendee target retained the Phase 9A development defaults of
four context slots, four active sessions, two provider workers, and eight HTTP
connections. Those defaults are useful for contract and lifecycle testing, but
cannot exercise the agreed 10-to-20-client launch burst and three-to-five
simultaneous command workload.

Changing only the visible session or worker value would be unsafe. Actor pools,
launch grants, request admission, context bookkeeping, lifecycle tombstones,
idempotency history, logical-job accounting, and transport admission contained
smaller fixed ceilings. A nominally larger deployment could therefore fail at
context or command 17 under an unrelated retained-record quota.

## Decision

Keep the existing operator-selected workload defaults unchanged. Add explicit
capacity inputs only to the separately invoked attendee live and supervised
deployment tools. The hard
process-local maxima are 64 context slots, 64 active sessions, 32 admitted HTTP
connections, and eight provider workers. Startup rejects a worker count above
the active-session count or an active-session count above the context count
before importing an artifact, constructing a provider pool, or binding a
listener.

Context capacity is a service-epoch allocation, not a recyclable concurrency
counter. Each successful bootstrap permanently consumes one actor slot until
restart, even after revocation or expiry. Active-session capacity is the
separate concurrent-session bound. One session can have only one active command,
so provider concurrency is bounded by the smaller of active sessions and
workers.

The configured per-session command count continues to determine the
process-lifetime logical-job ceiling:

```text
L = max(8, active_sessions * max_commands_per_session)
```

Token and spend budgets remain the exact per-command reservation multiplied by
`L`. Settled logical jobs and their conservative usage remain retained for the
service epoch. Raising a capacity therefore raises a finite possible memory and
provider-spend envelope; it does not weaken command, output, deadline,
cancellation, attempt, state-document, token, or spend protection.

Fresh logical request admission remains eight operations per context per
one-second window. Its shared limit is derived as:

```text
max(16, 3 * context_capacity + physical_worker_capacity)
```

This covers one bootstrap, launch exchange, and session create for every
configured context plus one command submission per available worker in the same
window. It is intentionally still a shared finite limiter, rather than the full
per-context allowance multiplied across every context. The ledger tracks
exactly the configured context allocation. This preserves the accepted recovery
sequence at the boundary: a new context can first receive retryable
`logical_rate_limited`, while its identical retry after the fixed window reaches
the claimed, authoritative `attendee_context_capacity_exhausted` result.

Derive the retained authority and recovery stores from the same envelope. With
`C` contexts and `S` active sessions:

```text
launch grants                  = max(16, C + S)
selection idempotency          = max(64, launch grants)
selection token history        = max(128, C + 2)
bootstrap idempotency          = max(16, C + 1)
exchange idempotency           = max(64, launch grants)
revoke idempotency             = max(16, C)
tombstones                     = max(8, 2 * S)
lifecycle/core idempotency     = max(128, 2 * L + 4 * S)
```

The lifecycle formula covers the configured command submissions, a cancel for
every accepted command, and the create/terminal records permitted by the
two-session-wave tombstone allocation. Arbitrary distinct deterministic
rejections can still consume the explicitly bounded abuse ledger; this target
does not promise unbounded history. These are dictionary ceilings, not eager
allocations, so their larger values allocate no records until the explicit
attendee process handles work.

At the live default, deriving lifecycle/core idempotency from all 256 permitted
logical jobs raises that internal retained-record ceiling from 128 to 528. This
is the intended removal of a hidden process quota, not a new workload default;
no dictionary entries or provider resources are reserved eagerly.

The selected connection capacity sizes the threaded server's admitted
connection semaphore. A non-default capacity also sizes its listen backlog
before binding; leaving the capacity at eight preserves the accepted inherited
backlog behavior. The startup JSON reports the effective context, session,
worker, connection, logical-rate,
logical-job, budget, and retained-record limits together with their hard
maximums. It contains no credentials, paths, authority, profile identity, or
session data.

A development profile for the agreed UI workload is:

```text
context slots       = 20
active sessions     = 20
connections         = 32
provider workers    = 5
commands/session    = 64
```

That profile derives 1,280 process-lifetime logical jobs, a 65-operation shared
launch window, 40 retained launch grants, 40 tombstones, and 2,640 lifecycle
idempotency records. It remains loopback-only, private-BFF-only,
integration-only, and capability-disabled.

Product `v0.8.0-rc.4` remains byte-for-byte immutable. This change adds no
route, payload, error, browser authority, public listener, or production
capability. Importing the tools starts no listener, process, timer, worker,
provider client, credential read, or ordinary DREAMLAND composition. Normal
DREAMLAND and SSH operation therefore retain the same behavior and idle cost.

## Consequences

The UI team can start the existing private attendee target with a truthful
capacity profile large enough for its next integration rehearsal. Provider-free
tests exercise 20 independent bootstrap, selection, and session lifecycles,
typed overflow, complete close/revoke cleanup, and five-worker command
saturation and recovery without network or provider use.

This checkpoint is not deployed-load evidence. The worst-case retained state
grows with sessions, tombstones, commands, journals, runtime documents, and
worker processes; the 20/20/32/5 profile must be measured under a separate
real-process sustained-load rehearsal. It also does not prove provider account
or quota separation, upstream cancellation or billing termination,
provider-key rotation, rollout/rollback, public-edge behavior, UI/BFF browser
security, multi-instance coordination, durable recovery, or production
readiness. The backend continues to preserve `attendee_context_revoked`, stale
read `session_lost`, stale mutation `outcome_unknown`, and the explicit
context-start restart boundary. The UI alone owns its typed deliberate
`access-ended` mapping.

## Evidence

Owning tests pin the unchanged defaults and hard maxima, reject invalid
relationships before provider construction, verify CLI-to-runtime propagation,
inspect every derived internal policy, and validate safe startup reporting. A
provider-free service test creates 20 distinct contexts, grants, and sessions,
observes typed capacity rejection for the next bootstrap, then closes and
revokes all 20. A blocking deterministic executor separately proves five
simultaneous command slots, retryable sixth-command saturation, exact-request
recovery after one worker returns, and complete cleanup.

The existing four-context/four-session/two-worker/eight-connection subprocess
rehearsal and the one-command live-provider canary retain their accepted fixed
evidence scopes. Neither is silently reinterpreted as proof of the scaled
profile.

## Maintenance

Update this record with
`tools/product_attendee_security_preview.py`,
`tools/product_attendee_security_live.py`,
`tools/product_attendee_security_deployment.py`, their owning tests,
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`, `docs/PRODUCT_SESSION_API.md`,
`docs/ARCHITECTURE.md`, `docs/TESTING.md`, and `ROADMAP.md` whenever capacity
defaults, hard maxima, derivation formulas, startup reporting, evidence scope,
or deliberate-end/restart behavior changes.
