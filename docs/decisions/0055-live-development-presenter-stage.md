# Live Development Presenter Stage

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and coding agents
Purpose: record the explicit real-agent development target inserted between the Presenter Stage preview and World Forge.

## Context

Phase 3 proved Presenter Stage semantics through a deterministic no-provider
target. That is sufficient for contract compatibility but not for a maintainer
to judge the developing UI against real DREAMLAND latency, output, state
mutation, and provider failures. Waiting for the production Gate 1 and Gate 2
work would delay this feedback, while relabeling the deterministic target or
partially enabling a product capability would make its evidence dishonest.

## Decision

Add Phase 3D before the existing Phase 4 World Forge work. Provide a separate,
manually invoked, loopback-only Stage tool that composes the accepted Stage
service with `ProductRuntimeCommandExecutor` and a product-owned streaming
bridge to the configured `LangGraphCommandAgent`.

The bridge claims one cooperative product attempt, forwards bounded streaming
output, translates cancellation through a command-agent control, and requires
exactly one terminal result. A dedicated in-memory LangGraph checkpointer is
deleted by exact command namespace after execution. Detached state-profile
encoding remains the only state path; the adapter does not wrap `AgentBackend`
or register an SSH/admin session.

Settle every dispatched development command at its complete reserved token and
spend allowance. This conservative policy prevents undercharging the local
product ledger without claiming unavailable exact provider-cost evidence. It
also keeps the remaining production provider/attempt/usage work visible.

The manual launcher keeps a finite 12-command default and accepts a positive
override. An explicit `-1` disables only the accepted-command count. The
sentinel is normalized before service construction: the launch limits map
omits `max_commands`, and the product core has no count guard. Independent
finite live-process job identity retention and token/spend budgets are sized
separately from the default count, so command 13 cannot fail under a renamed
12-command quota. Concurrency, TTL, command/output, cancellation, attempt,
authority, idempotency, and journal safeguards remain active.

Create transport bearer and Presenter bootstrap credentials only through
explicit owner-only files. The BFF consumes them server-side. The browser never
receives the private transport bearer, actor context, launch grant, or returned
session capability.

Publish `0.4.0-rc.2` rather than altering the published `rc.1`. Preserve all 15
route semantics, but identify responses as `development-live-langgraph-v1`
with provider calls true. Continue reporting all five product capabilities
disabled and live-capability evidence false.

## Consequences

Maintainers can now evaluate the UI with real model-backed, stateful commands
before World Forge adds another asynchronous system. Provider latency and
failure behavior become visible early. The target consumes no resources unless
explicitly started and given commands, so ordinary DREAMLAND remains
baseline-equivalent.

The startup announcement exposes only the resolved non-secret model reference,
provider output-token cap, and effective accepted-command limit in addition to
its existing target identity. This lets an operator catch a missing
`DREAMLAND_CONFIG` selection before making a provider call.

Provider SDK stream segmentation is not guest-visible policy. The live adapter
flushes each completed line immediately and emits unterminated text in
UTF-8-safe 4 KiB units, flushing on stream changes and terminal completion. Its
chunk allowance cannot bind before the 65,536-byte command ceiling. Exact use
of a publication boundary is complete; clipping truth records actual omitted
content. The startup announcement reports this non-secret policy.

Phase 3D does not satisfy Gate 1 or Gate 2. It does not prove browser security,
provider retry enforcement, exact usage, dedicated credentials, deployment
isolation, load, cutoff, or presentation readiness. Phase 4 remains World Forge
after this development checkpoint.

## Alternatives Considered

Changing the deterministic Stage preview to call a provider was rejected
because it would invalidate its repeatable compatibility role and response
identity. Enabling `presenter_stage_terminal` only in development was rejected
because capability projection is release evidence, not a convenience switch.
Wrapping `AgentBackend` was rejected because it would couple product sessions
to SSH/admin indexes and ordinary runtime behavior.

## Maintenance

The implementation and exact generated artifact bytes are recorded in commit
`37083b1e2d3ecc2faad6bf473d6e5e3eb6fce6d8`. The immediately following
publication commit binds the manifest to that implementation commit.
The launcher-only configurable command guard, independent live-process budgets,
and non-secret resolved-settings announcement are recorded in commit
`3f7d417c0b607d867c2ed9c095e0038910eecc44`; the published contract bytes did
not change.
Line-aware live output publication, the nonbinding chunk guard, and exact
clipping truth are recorded in commit
`48abaedc749bd77015f12b463e65f3d39c3f8b71`; the published contract bytes again
remain unchanged.

Update this decision before changing the live-development response identity,
provider composition boundary, conservative accounting, checkpoint cleanup,
credential-file handoff, command-limit normalization and independent admission
budgets, startup configuration projection, loopback restriction, or
capability-disabled status. Also update it before changing line-aware output
coalescing, the live byte/chunk relationship, or clipping truth.
