# Live World Access Composition And Retained Publication Accounting

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the Phase 8B live Forge-to-attendee composition, authority and
epoch joins, truthful contract identity, and bounded publication-retention
policy without enabling a production capability.

## Overview

Phase 8A proves generated-world promotion, reveal, catalog publication,
notice-bound exchange, and unchanged Product session creation with a
deterministic source and executor. Separately, Phase 7D proves configured
provider-backed attendee commands, and World Forge RC4 proves an authoritative
live selected-artifact source. Those processes cannot be combined by moving a
Phase 8A grant into Phase 7D: their service epochs, actors, registries, launch
authority, and recovery truth are process-local.

The next development checkpoint needs one explicitly started process in which
a freshly selected live Forge revision can pass through the reviewed World
Access boundary and become an interactive provider-backed attendee session.
It must preserve World Access RC3 authority and ambiguous-create recovery,
truthfully distinguish live execution from the provider-free preview, and add
no work or cost to normal DREAMLAND startup.

## Decision

Add `tools/product_world_access_live.py` as the only Phase 8B composition entry
point. Importing the tool remains inert. Its `serve` command validates exact
World Forge RC4, Product RC4, and World Access RC4 roots before loading provider
settings, constructing providers, or binding listeners. It then composes the
existing live Forge, Presenter Stage, attendee executor, and Product session
services and attaches World Access before any request can arrive. All listeners
remain loopback-only.

Create World Access `v0.1.0-rc.4` as an additive live-development sibling of
published RC3. Preserve the exact six operations, actor classes, request and
response semantics, safe errors, idempotency families, internal eligibility
proof, Product-compatible grant, and output-only launch-binding correlation.
Pin Product `v0.7.0-rc.4` and World Forge `v0.1.0-rc.4` by exact published
manifest hashes. RC4 identity reports:

- `provider_calls: true`;
- `live_forge_evidence: true`;
- `live_attendee_execution: true`;
- `live_capability_evidence: false`; and
- no enabled production capabilities.

The World Access RC4 artifact was kept draft until it could be bound to the
exact tested implementation commit. It is published against
`d1df607bce702475fa8ed79765263445d0e02c6b`. Provider-free composition tests and
client compilation are supplemented by a bounded provider-backed backend
smoke; broader human/browser evaluation remains an independent gate.

Use separate high-entropy Presenter and Operator contexts in one owner-only
Phase 8B bootstrap. Presenter authority continues to own Forge jobs, selected
records, Presenter registration, and Stage handoff. Operator authority can only
promote, inspect, and reveal generated access. The two contexts must differ and
must not appear in startup output, browser DTOs, URLs, logs, or contract
artifacts. The shared Product bearer is transport authentication, not either
actor authority.

Resolve promotion input through `WorldForgeAccessSource` over the same live
Forge service and immutable artifact store used by the process. The trusted
source uses Presenter authority internally. The Operator supplies only a
bounded, non-authorizing Forge job reference; it cannot choose a revision,
digest, artifact path, selected-record credential, or raw state. Accept only
the authoritative live source kind and selected handoff policy already reviewed
for World Forge RC4.

Use the Product service epoch as the access-service, attendee registry,
generated catalog, attendee facade, and attendee core epoch. Use the exact
Product-leased attendee actor for both Product and World Access attendee
operations. Do not translate or remint that actor at the catalog boundary.
Product's own attendee option and exchange routes remain authored-only. A Forge
selection or Presenter registration is not generated-attendee eligibility;
only explicit Operator promotion, reveal, catalog evidence, and World Access
exchange can provision the ordinary Product grant consumed by session create.

Keep World Access RC3 recovery unchanged. Exact exchange replay returns the
same grant and launch-binding digest. Exact Product create replay is the primary
ambiguous-response recovery. If a BFF must inspect current-session truth, it
associates the generated selection only when both service epoch and launch-
binding digest match. Neither service accepts that digest as authority.

Choose retained-lifetime accounting instead of pruning for this development
process. Permit at most 16 prepared or committed generated publications per
service epoch. Expand the attendee registry to one authored plus 16 generated
revision and offering slots. Expired bindings leave catalog visibility but
continue consuming capacity because issued launch grants, exact replay, and
changed-body conflict protection can still depend on the retained registry
records. Report safe capacity, used, and remaining counts at startup. After
exhaustion, return the existing retryable publication-capacity failure and
require process restart; restart rotates the epoch and invalidates old
authority.

Cap generated access lifetime to the smaller of the configured 30-minute
access TTL and the authoritative selected record's remaining lifetime. Continue
to clamp offering and grant lifetime by access truth, attendee admission, and
their existing policies. A short-lived selected record therefore cannot be
promoted into longer-lived access truth.

Retain `--max-commands -1` for human evaluation. Map it to absence of only the
positive accepted-command count. Do not relax the attendee `STANDARD`
partition, Presenter reservations, active-session and worker capacities,
logical-job accounting, command-size and output bounds, one-attempt provider
policy, deadlines, TTLs, token and spend budgets, admission cutoff,
cancellation ownership, or shutdown fencing.

Keep all Product capabilities disabled. This checkpoint does not establish
internet-facing gateway security, durable restart recovery, generic catalog
administration, safe withdrawal or pruning, canary/rollback behavior,
conference load capacity, deployment isolation, or presentation rehearsal.

## Consequences

The UI BFF can run one truthful live sequence from Forge generation through
hidden access, explicit reveal, mixed catalog, generated grant, Product session,
and configured model-backed command execution. The resulting session uses the
same terminal, transcript, event, cancellation, recovery, and close surfaces as
an authored attendee session.

Distinct Operator and Presenter credentials prevent publication authority from
collapsing into Forge or Stage authority. Sharing the Product epoch and attendee
actor avoids transferring opaque authority between independently constructed
services. The existing launch-binding pair remains sufficient for conservative
create recovery without exposing a revision, selected record, access record,
grant, or session capability to the browser.

Retained-lifetime accounting deliberately trades service-epoch throughput for
replay safety and simple bounded reasoning. Sixteen publications are sufficient
for the current development evaluation, but this design is not a generic long-
running catalog. Future withdrawal or pruning must prove that no issued or
replayable authority references a candidate record before reclaiming it.

The live identity tells operators and the BFF that this explicitly selected
process may call providers and consumes authoritative Forge results. The false
capability evidence and empty capability set prevent that development truth
from being mistaken for production enablement.

Normal `python -m dreamland`, SSH, admin, MCP, state-profile, logging, and
EnrichmentAgent paths remain outside the composition. When the tool is not
explicitly started, Phase 8B creates no listener, provider, worker, timer,
authority registry, publication counter, reservation, or runtime-operation
cost.

## Evidence

Provider-free owning tests must prove exact contract dependency validation,
truthful live response identity, route-correct Product and World Access headers,
distinct Operator/Presenter rejection, shared Product/World Access epoch,
authoritative selected-source resolution, hidden-before-reveal catalog truth,
generated exchange and Product session creation, RC3 launch-binding replay and
current-session correlation, authored-only Product routes, and side-effect-free
cross-role denial.

Capacity tests must prove the 16-entry retained-lifetime budget, safe accounting,
non-reclamation after expiry, exact replay retention, retryable publication
exhaustion, access-lifetime clamping to the selected record, and restart epoch
rotation. Live-composition tests must prove that generated commands use only
the attendee `STANDARD` partition, command 13 is admitted in unlimited mode,
positive command limits still reject their successor, and all other provider,
resource, output, cutoff, cancellation, and shutdown guards remain active.

Contract tests must preserve World Access RC1 through published RC3 byte-for-
byte, prove RC4 differs only in revision, Product dependency, and truthful live
identity, validate every fixture and exact operation, and compile/exercise the
server-only RC4 TypeScript client. Publication binds the artifact and all
owning implementation files to exact tested commit
`d1df607bce702475fa8ed79765263445d0e02c6b`.

On 2026-08-30 a bounded backend provider smoke generated and selected one live
Forge revision in 43.49 seconds end to end, verified hidden-before-reveal truth
and the mixed authored/generated catalog, replayed both exchange and Product
create, matched the shared epoch and launch-binding correlation, and completed
one generated attendee command. The command published 12 bytes in one unclipped
chunk. Deliberate SIGINT shutdown returned 130 with no traceback and no
`generator already executing` race; all production capabilities remained
disabled.

That smoke does not complete the broader human/browser gate. A later evaluation
must run multiple provider-backed commands, exercise focused cancellation and
browser recovery, and audit browser payloads. Do not infer those results from
the bounded backend evidence.

## Maintenance

Update this record with `src/dreamland/product/generated_access.py`,
`src/dreamland/product/attendee_catalog.py`,
`tools/product_attendee_live.py`, `tools/product_world_access_preview.py`,
`tools/product_world_access_live.py`,
`tools/product_world_access_live_contract.py`, World Access RC4, Product RC4,
World Forge RC4, `docs/PRODUCT_WORLD_ACCESS_DEVELOPMENT.md`,
`docs/PRODUCT_ATTENDEE_LIVE_DEVELOPMENT.md`, `docs/ARCHITECTURE.md`, and their
owning tests whenever live identity, dependency pins, authority composition,
epoch sharing, eligibility, correlation, retained capacity, selected lifetime,
limits, safeguards, or normal-runtime isolation changes.
