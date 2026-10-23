# Attendee Provider Exact-Deadline Cooperative Containment

Status: Active
Audience: DREAMLAND maintainers, deployment owners, UI BFF developers, and
coding agents
Purpose: record deterministic cancellation, publication, worker-lifecycle, and
resource-accounting semantics when an attendee provider request reaches its
exact command or session deadline.

## Overview

The attendee provider pool observes both a lifecycle-owned cancellation
predicate and the request's absolute deadline. At an admission cutoff, those
facts can become visible in adjacent polls: the pool may see cancellation at
the exact deadline, or it may see the deadline immediately before the
lifecycle owner publishes cancellation.

Treating both orderings as an immediate ambiguous timeout unnecessarily retires
a child that cooperated, loses authoritative cancellation truth, and makes
resource accounting depend on thread scheduling. Waiting without a bound would
weaken the provider deadline and publication fence.

## Decision

Keep cancellation observation before deadline comparison in each provider-pool
poll. If cancellation is already observable when the clock equals the request
deadline, cancellation wins. The pool signals the child, waits only through the
existing bounded cancellation grace, and returns the child's confirmed
interrupted result and authoritative usage. A child that ignores this
cancellation still follows the existing cancellation-timeout containment and
full-reservation rules.

If the pool observes the request deadline first, it immediately suppresses
further child output, signals cooperative cancellation, and waits only through
the same bounded cancellation grace. The effective wait remains capped by the
hard pool request timeout and any earlier drain deadline.

When that child returns within the grace, release it as a completed,
non-ambiguous dispatch. Do not retire, replace, or count it as locally forced;
the worker remains reusable. The pool result nevertheless remains the typed
`provider_deadline_exceeded` failure, and token and spend usage remain the
request's full reservation. A result received after the authoritative deadline
cannot reconcile the reservation or publish output/state merely because the
child stopped cooperatively.

When the child does not return within the grace, retain the existing
conservative path: record an ambiguous deadline dispatch, charge the full
reservation, contain and retire the child, and replace it when the pool is
still accepting work. Local force remains publication containment, not proof
that an upstream request or billing stopped.

These pool-internal semantics do not change Product RC4 or its UI recovery
surface. At the scheduled attendee cutoff, authoritative session truth remains
lifecycle `expired` with terminal reason `absolute_expired`; its active command
remains failed with reason `session_terminated`, full reserved usage, and no
late output or accepted state. The provider-deadline reason is not introduced
as a new RC4 wire result.

## Consequences

The same exact instant now has deterministic precedence instead of a
scheduler-dependent worker-retirement outcome. Cooperative cancellation keeps
its interrupted result when already visible, while deadline-first handling
keeps the hard publication and accounting boundary without destroying a
healthy local worker.

Confirmed local cooperation still does not establish upstream cancellation or
billing termination. Provider-backed deployment evidence remains required for
those claims, and no production capability is enabled by this refinement.

## Evidence

Provider-pool tests use an injected clock to cover both exact orderings. They
assert interrupted precedence when cancellation is observed first, and typed
deadline/full-reservation accounting with one completed dispatch, zero
ambiguous dispatches, zero locally forced workers, and a reusable ready pool
when the deadline is observed first. Existing non-cooperative deadline tests
retain ambiguous containment, full accounting, replacement, and successful
follow-up dispatch evidence.

The provider-free Phase 9C black-box rehearsal separately proves the RC4 cutoff
projection: `expired`/`absolute_expired` session truth,
`session_terminated` command truth, full reserved usage, fenced late output and
state, retained recovery, and clean pool convergence.

## Maintenance

Update this record with `src/dreamland/product/attendee_provider.py`,
`tests/test_product_attendee_provider.py`,
`tools/product_attendee_security_deployment_rehearsal.py`, its owning tests,
`docs/decisions/0076-attendee-dedicated-provider-boundary.md`,
`docs/decisions/0079-provider-free-attendee-deployment-rehearsal.md`,
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`,
`docs/PRODUCT_SESSION_API.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`, and
`ROADMAP.md` whenever provider deadline/cancellation precedence, output
suppression, worker reuse, terminal projection, or reservation accounting
changes.
