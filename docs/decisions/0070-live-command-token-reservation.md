# Live Command Token Reservation Follows The Provider Maximum

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record how explicitly started product live targets reserve tokens
before provider dispatch without changing ordinary DREAMLAND or published API
wire artifacts.

## Overview

The product live targets historically reserved 8,192 tokens for each accepted
Presenter or attendee command. The configured agent used for Phase 7D human
evaluation permits up to 12,800 output tokens. Passing that larger maximum to
the provider while reserving only 8,192 made the initial admission claim
smaller than the provider request's allowed worst case. Conservative settlement
after execution cannot repair an under-reservation made before dispatch.

This is a valid accounting defect even though all product capabilities remain
disabled and the affected targets are private development tools. Provider work
must never begin under a smaller token reservation than its configured request
maximum.

## Decision

Derive one finite command reservation for every explicitly started live
Presenter Stage or attendee executor:

```text
command_reserved_tokens = max(8,192, configured max_output_tokens)
```

The 8,192-token value remains the conservative floor. A lower finite provider
maximum therefore keeps the existing reservation, while a larger finite
maximum scales the reservation upward. The exact derived value must be used in
the launch limits, `ProductCorePolicy`, admission ledger, and startup audit
projection for that executor.

Blank or zero `max_output_tokens` configuration resolves to `None` in the
shared configuration model. A product live target must reject that unbounded
value, as well as any invalid or unrepresentable derived budget, before it
constructs a provider or publishes a listener. It must not invent a provider
default or dispatch under the 8,192-token floor.

Scale the Presenter token budget and Presenter reserved-token budget by the
derived reservation and the existing Presenter logical-job capacity. In the
combined Phase 7D process, derive the attendee command reservation from its
separately constructed executor, scale the finite `STANDARD` attendee token
allocation by the existing attendee logical-job capacity, and add that amount
to the shared total. Preserve the existing session, logical-job, physical-
worker, spend, TTL, command-size, output, attempt, deadline, cancellation, and
idempotency controls. Attendee saturation still cannot consume the Presenter
reservation.

Every live startup announcement must report the effective finite
`max_output_tokens`, `command_reserved_tokens`, and applicable `token_budget`
alongside the existing safe model, command-count, output, and capacity fields.
The values are non-secret operator evidence. No bearer, authority, path,
profile identity, digest, provider key, or session capability may be added to
the announcement.

Apply this rule in the shared product live-executor construction used by the
direct Stage, Forge-to-Stage, source-neutral launch-selection, and authored-
attendee targets. Do not apply it to ordinary `python -m dreamland`, SSH,
admin, MCP, state-profile, logging, EnrichmentAgent, or other provider
composition. Those paths retain their existing configuration and behavior.

Keep the published product API artifacts, including `v0.7.0-rc.4`, byte-for-
byte immutable. The correction changes development composition and startup
audit values, not HTTP routes, request or response DTOs, capability claims,
generated clients, fixtures, or contract identity. Existing launch-option
limit fields already carry positive integer command reservations and require
no schema addition.

## Consequences

The 12,800-token evaluation configuration reserves at least 12,800 tokens
before either Presenter or attendee provider dispatch. Configurations at or
below 8,192 retain the previous conservative floor. Higher finite values remain
bounded by their scaled process token budgets rather than silently borrowing
from a smaller reservation.

An operator who intentionally disables the ordinary agent output safeguard
with blank or zero configuration cannot start a product live target. They must
choose a finite positive maximum first. Failure occurs before provider or
listener construction, so it cannot create an uncertain request or a partially
published service.

Token budgets can be larger than their historical fixed values when the
configured provider maximum is larger. Spend budgets and all other safeguards
remain independently finite. The startup audit makes that expanded token
envelope visible before a human submits provider work.

## Evidence

Owning tests must cover a 12,800-token configuration, the exact 8,192 boundary,
a lower finite value, `None`, zero/blank configuration through the relevant
configuration seam, arithmetic overflow or unsupported values, scaled direct
Stage and combined Presenter/attendee budgets, and startup field consistency.
They must prove rejection precedes provider/listener construction and retain
the existing command idempotency, budget-exhaustion, cancellation fencing,
settlement, contract immutability, and inert-import evidence.

The correction was implemented by commit
`79a6145e492b1010a4989e184038ccbf5a00a9e3`. Verification on that behavior
passed the complete 1,662-test repository suite with four expected skips, a
242-test cross-path product regression with one expected skip, and an
80-test live-contract and inertness audit with one expected skip. The audit
also confirmed that no published contract bytes changed and ordinary
`dreamland` startup imports none of the four product live tools.

## Maintenance

Update this record with `tools/product_stage_live.py`,
`tools/product_forge_stage_live.py`, `tools/product_launch_selection_live.py`,
`tools/product_attendee_live.py`, their owning tests, the four live-development
runbooks, `docs/PRODUCT_SESSION_API.md`, `docs/ARCHITECTURE.md`, and
`docs/TESTING.md` whenever the configured provider maximum, reservation floor,
budget arithmetic, startup audit, or live-composition boundary changes.
