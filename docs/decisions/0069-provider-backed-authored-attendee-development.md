# Provider-Backed Authored Attendee Development

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the live authored-attendee development composition, cutoff,
privacy, capacity, and compatibility decisions without enabling a production
capability.

## Overview

Phase 7C established an authored-only attendee authority and complete
provider-free `/session` integration path. The next useful UI checkpoint needs
real model-backed attendee commands while retaining the already proven live
Forge and Presenter Stage flow. Building a separate attendee-only process would
force the UI to choose between those checkpoints and would duplicate product
authority and admission state.

Phase 7D therefore adds attendee execution to the existing explicitly started
combined target. It preserves the Phase 7C wire contract, keeps generated
worlds in the Presenter-only boundary, and leaves normal DREAMLAND composition
unchanged.

## Decision

Publish additive `v0.7.0-rc.4` as an identity-only live sibling of RC3. Preserve
all 24 paths, request/response schemas, safe errors, fixture semantics,
idempotency metadata, authority rules, and all-disabled capability claims.
Change only the contract revision and truthful development identity to report
that accepted attendee commands may call a provider. Bootstrap, listing,
notice-bound exchange, provisioning, reads, and lifecycle recovery remain
provider-free.

Implement `tools/product_attendee_live.py` as a combined continuation of the
Phase 7B live target. It retains authoritative World Forge registration,
generic authored/Forge Presenter selection, legacy Forge handoff, terminal,
and X-ray on the same two loopback listeners. The attendee registry is
separate, contains only the exactly pinned authored immutable revision, and
never observes Forge selection. The Product listener routes shared session
operations by exact Presenter authority or a non-authorizing attendee ownership
predicate; unknown authority cannot fall through to either service.

Use one separately constructed attendee command executor and in-memory
checkpointer. Force external attendee Langfuse tracing off independently of the
configured Presenter and Forge collaborators. Do not write attendee session,
checkpoint, transcript, or accepted-state data to the authored artifact store.
Only the startup-pinned authored immutable revision is persisted there.

Share one process admission ledger while retaining every Phase 7B Presenter
reservation boundary and capacity. Add a separately finite `STANDARD`
allocation for attendee sessions, logical jobs, physical workers, tokens, and
spend. Decision 0070 later requires the Presenter and attendee token amounts to
scale independently with their finite configured provider maxima; it does not
change the protected partitions or other reservation values. Attendee
saturation must not borrow Presenter capacity, and Presenter saturation must
not silently convert attendee authority into Presenter authority. Keep command-
size, output, attempt, provider, TTL, cancellation, and usage-accounting guards
enabled when the optional command count is unlimited.

Bound the authored attendee offering, issued grants, sessions, and command
publication by one configured admission close on both wall and monotonic clocks.
The context/actor lease may remain valid for its bounded recovery lifetime so a
BFF can observe terminal current-session truth after cutoff, but it cannot
exchange, create, submit, reset, or switch new work. Explicit process shutdown
revokes BFF/context/actor/grant authority, terminalizes attendee sessions,
cancels queued or running work, fences late completion, and settles admission.

Preserve RC3's private bootstrap ordering in this identity-only sibling.
Bootstrap is a bounded, provider-free namespace lease rather than launch
admission: a still-valid private BFF authority can consume a never-reused
context slot during cleanup grace, but the expired offering yields no attendee
option or grant and cannot dispatch work. Closing fresh bootstrap itself would
change RC3 idempotency and rejection semantics and therefore requires a later
additive contract rather than an undocumented RC4 difference.

Also retain RC3's two inherited `launch_selection_unavailable` and
`launch_selection_capacity_exhausted` safe-error messages even though their
wording says Presenter Stage. The wording is awkward on the attendee route, but
changing a canonical safe error in an identity-only sibling would break strict
clients. A later additive contract may introduce neutral wording deliberately.

Validate both contract roots and the byte-pinned World Forge dependency before
provider or listener publication. Report only safe startup identity and limits:
resolved models, maximum output tokens, effective command limit, cutoff,
capacities, and output policy. Never announce paths, profile identity, authored
digest, artifact coordinates, bearers, API keys, actor contexts, grants,
session capabilities, or provider secrets.

Keep the target behind an explicit `serve` command and loopback validation.
Importing its runtime, contract generator, or tests starts no listener, worker,
timer, provider, artifact write, admission reservation, or ordinary DREAMLAND
composition. All production capabilities remain disabled.

## Consequences

The UI can test its real authored `/session` terminal against a live configured
agent while retaining the working Forge-to-Presenter and X-ray flow in the same
backend process. RC3 integrations require only an intentional contract-root and
expected-development-identity change; their operation DTOs do not change.

Separate executor/checkpoint state prevents an attendee session from colliding
with or continuing a Presenter command thread. Forced-off external attendee
tracing avoids sending public-attendee content to an optional third-party trace
processor during this development checkpoint. The shared hard ledger still
provides process-wide accounting while reserved capacity protects the demo
Stage.

This work is not Gate 3 evidence. It does not provide an internet-facing
gateway, browser security controls, deployment-wide provider isolation,
conference saturation results, durable recovery, exact provider billing,
enabled `public_attendee_sessions`, or generated-world attendee eligibility.
Those remain independently gated. Phase 8 still owns promotion, publication,
catalog, eligibility, canary, rollback, and presentation rehearsal.

## Evidence

Provider-free tests cover contract identity-only compatibility, exact manifest
dispatch, inert import, pinned authored construction, separate provider
collaborators and checkpoint state, forced-off attendee tracing, exact authority
routing, provider-free facade operations, accepted command dispatch, shared
ledger partitions and saturation, dual-clock cutoff, terminal recovery,
shutdown cancellation, startup redaction, invalid limits, dependency pinning,
and preservation of Phase 7B and RC3 behavior. The published RC4 manifest
records implementation commit
`cc09190bfaf295ccdda957b52d0aa999e8c163df`, created after the implementation,
integration, and repository suites passed.

## Maintenance

Update this record with `src/dreamland/product/attendee.py`,
`tools/product_attendee_preview.py`, `tools/product_attendee_live.py`,
`tools/product_attendee_live_contract.py`, their tests, the RC4 artifact,
`docs/PRODUCT_ATTENDEE_LIVE_DEVELOPMENT.md`, `docs/PRODUCT_SESSION_API.md`,
`docs/ARCHITECTURE.md`, `docs/TESTING.md`, and `ROADMAP.md` whenever live
attendee execution, cutoff/recovery, privacy, authority routing, capacity,
contract identity, or capability boundaries change.
