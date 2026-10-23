# 0053 Unfrozen Product Session Core Preview

Status: Active
Audience: maintainers, UI BFF developers, DREAMLAND operators, and coding agents
Purpose: record the Phase 2 execution, publication, resource, and release-gate
boundaries for the shared private product-session core.

## Overview

Phase 1 established process-local launch, authority, lifecycle, epoch, and
idempotency truth through a manually invoked integration preview. Phase 2 adds
the command and recovery substrate that makes a shared product session useful:
one asynchronous command slot, cancellation and publication fencing, bounded
transcript and events, authoritative recovery, replacement, isolated execution,
and product-only resource admission.

The Revision 3 UI/backend agreement authorizes this protocol-neutral work, but
the UI's tracked-document acknowledgement has not landed. Phase 2 therefore
implements and publishes an unfrozen integration release candidate. It cannot
freeze the private wire contract, advertise BFF compatibility, or enable
`product_session_core` until the remaining UI, provider, deployment, and
complete Gate 1 evidence exists.

## Decision

### One gate, backend checkpoints

Phase 2 remains one capability gate even though it is implemented through
reviewable backend checkpoints. Intermediate code and a manually invoked
loopback target may support deterministic integration testing, but all five
product capability claims remain disabled until their independent gates pass.
The published Phase 0 and Phase 1 artifacts remain immutable.

Normal DREAMLAND composition remains unchanged while the product master switch
is disabled: no product module import, aggregate, journal, executor, worker,
timer, listener, event subscription, provider reservation, or legacy-path
bookkeeping is created. An incomplete explicitly enabled product build still
fails startup closed.

The integration core still uses the lifecycle aggregate lock for bounded map,
journal, cutover, and final enrichment transactions. It never holds that lock
through provider execution, output delivery, foreground state validation, or
replacement validation, and it rechecks the full publication/cutover CAS after
detached validation. Phase 2 proves unrelated provider work is concurrent, but
does not claim final per-session-lock/load evidence for every bounded control
operation. That evidence remains part of the disabled capability's full gate.

### Product command boundary

Browser command input is classified by a new backend-owned
`product-single-v1` boundary before command identity, slot, transcript, event,
budget, or provider allocation. It decodes UTF-8 once, rejects invalid scalar
data and embedded line or NUL controls, trims only ASCII space and horizontal
tab, performs no Unicode normalization, enforces byte and scalar bounds, and
rejects unquoted shell process-control syntax. Existing SSH parsing and command
behavior are unchanged.

Every accepted command occupies the sole session slot. Lifecycle/control CAS,
slot generation, accepted-state revision, event position, and transcript
position are independent coordinates. A stable command identity and slot
generation exist before execution dispatch.

The aggregate records command acceptance only after exact slot CAS and
product-ledger reservation succeed. Transcript and event acceptance are built
and committed as one journal operation. Submit idempotency is claimed only at
the trusted semantic boundary: deterministic authority or business rejection
is replayable and changed-body reuse conflicts, while retryable pre-dispatch
capacity rejection rolls back the claim and all generated/allocation state.
The same rule applies to downstream create admission, so recovery never leaves
a phantom session or consumes a key for a transient no-work refusal.

### Isolated execution and terminal publication

The product aggregate supplies an executor with a deep-copied provisional
runtime document, a command-generation-specific checkpoint namespace, bounded
execution context, and cancellation observation. Generation and provider work
occur outside the aggregate lock. The executor never receives the live
aggregate or mutation authority.

Potential committed documents and their injected validators are also evaluated
against detached data outside the aggregate lock. Publication then performs a
final accepted-state revision CAS. A slow validator for one session therefore
cannot serialize unrelated sessions, and a foreground or enrichment change
that wins during validation safely changes the candidate effect to rejected.

One compare-and-set under the aggregate lock in this checkpoint verifies the
active session, exact command and generation, open publication fence, deadline,
and current runtime identity before it installs terminal truth and any
qualified state effect. A completed result has exactly one tagged guest
termination and one
`none`, `committed`, or `rejected` state effect. Only `committed` advances the
accepted-state revision. Interrupted and failed results carry typed reasons and
cannot publish state or provisional checkpoints.

A new exact cancel operation races at that same publication fence. Terminal
truth first returns `already_terminal`. Cancellation first records replayable,
nonterminal `cancel_requested`, closes publication, and requests best-effort
physical cancellation. The logical slot remains occupied until truthful
`interrupted` or typed `failed` terminal evidence is installed. Physical worker
capacity remains reserved until the worker is actually reaped.

Forced terminal recovery cannot invent zero usage while a dispatched worker is
still outstanding. Its immutable terminal outcome carries a `pending` usage
projection; actual reap later changes only that accounting projection to
`settled`. Unexpected post-dispatch failure, missing measurement, or reported
usage outside the reservation is conservatively charged to the full token and
spend reservation. Terminal outcome, guest termination, failure reason, state
effect, and accepted-state revision are never rewritten by reconciliation.

### Recovery and replacement

The aggregate owns bounded, session-local transcript and typed event journals.
Event and transcript positions are separate. Event replay is at least once;
cursor eviction or stream repair returns `snapshot_required` within the current
epoch. Command-result query and an internally consistent bounded snapshot remain
authoritative if a response or promised event is lost. Slow consumers cannot
block terminal publication or create unbounded memory.

Reset and switch are idempotent provision-before-cutover replacements. They
require exact control CAS and return typed busy while a foreground command is
nonterminal. Failed provisioning preserves the old session and authority;
successful cutover atomically changes the current binding, revokes stale
publishers and authority, and retains only bounded recovery truth. Close and
expiry instead force the active command fence and reconcile local resources
exactly once. A future deployment cutoff must invoke and prove the same fencing
and reconciliation behavior before the capability can be enabled.

Admission replacement is count-neutral and atomic rather than release-then-
admit. The aggregate carries the actor's consumed command count into every
replacement incarnation, so reset or switch cannot replenish the configured
session command quota. Replacement reserves the old tombstone and the live
replacement's eventual terminal slot before detached validation. Every other
mutation and new-session capacity check includes those in-flight claims, so an
unrelated request cannot consume their idempotency or terminal reservation.
Cutover then rechecks capacity and source identity. Core construction rejects
journal limits too small to represent mandatory recovery events.

### Product-only resources and enrichment

Phase 2 owns a bounded in-memory admission ledger for product sessions, logical
jobs, physical workers, worst-case attempts, tokens, and hosted spend. Same-key
retries and cancel operations never reserve twice. A late or cancellation-
ignoring worker retains its physical reservation until reap. The ledger can
represent later Stage and Forge categories, but Phase 2 advertises no real
Stage reservation or Forge capacity.

The initial live product deployment must use a dedicated non-overcommitting
provider pool or credentials so product load cannot consume ordinary SSH or
default EnrichmentAgent capacity. If deployment cannot prove that isolation and
hard-cap accounting, `product_session_core` remains disabled; Phase 2 does not
add shared accounting to ordinary runtime paths.

Product enrichment uses its own provisional identity and the product ledger.
Proposal generation may overlap foreground work, but APPLY waits for foreground
terminal truth, revalidates lifecycle and accepted-state identity under the
same product lock, and installs any accepted patch, accepted-state revision,
and journal transition before external event delivery. Close, replacement,
expiry, or identity change fences stale APPLY work; a future deployment cutoff
must exercise that same fence.

Injected command, state-validation, task-submission, admission-initialization,
enrichment-generation, enrichment-validation, and enrichment-clock fatal exits
preserve Python control-flow semantics only after their product records and
resource reservations converge. Unknown post-dispatch usage is charged to the
full reservation. Watchdog and checkpoint cleanup still run, and exact retries
observe terminal recovery truth rather than a permanently in-progress record.

The coordinator has no scheduler, listener, timer, or provider of its own. One
aggregate transaction installs the accepted document, next accepted-state
revision, and canonical `state.accepted` event. Optional observer delivery runs
after commit, so observer failure cannot roll back or duplicate canonical
truth.

The product runtime adapter is explicitly injected and never wraps the ordinary
`AgentBackend`. It uses detached state-profile codecs, an exact provisional
checkpoint namespace, cooperative cancellation/deadline control, usage
measurement, bounded watchdog shutdown, bounded output forwarding chunks, and
constant-size streamed-prefix evidence instead of retaining all streamed text.
No real provider is constructed in Phase 2. A future provider adapter must
claim every physical attempt through the supplied control protocol and prove
that an agent cannot bypass the attempt cap, along with cancellation, usage,
checkpoint cleanup, and separate-credential isolation; the core capability
remains disabled until that evidence exists.

### Release evidence

Phase 2 publishes `contracts/product-api/v0.3.0-rc.1/` rather than mutating an
older artifact. It includes private REST and event schemas,
canonical/forward-compatible/invalid fixtures, deterministic digests, generated
server-side client smoke evidence, and explicit response-loss and promised-
event-withholding scenarios. The manually invoked integration target is not a
production BFF or proof that a capability is live.

`tools/product_session_contract.py` validates the exact unfrozen artifact and
generated client. `tools/product_core_preview.py` is loopback-only, reuses the
hardened Phase 1 private transport boundary, reports all five capabilities
disabled, and injects a deterministic executor that performs no provider,
network, or host-command work. It is imported by no normal runtime module.
The shared preview parser rejects duplicate body-framing and media headers. The
core preview negotiates JSON/SSE with quality weights, rejects ambiguous cursor
coordinates, and its generated client normalizes the canonical initial cursor
to omitted query parameters.

Schema freeze and capability enablement require the UI tracked-document
acknowledgement, complete Gate 1 backend evidence, compatible private gateway
integration, deployment provider isolation and limits, and explicit release
metadata. UI-owned browser cookies, CSRF, Origin/Host validation, browser
deadlines, browser DTOs, and bundle-boundary evidence remain outside DREAMLAND.

## Alternatives Considered

Wrapping the admin/MCP command operation was rejected because it holds the
session lock through provider work, mutates live state and ordinary checkpoint
identity, discovers command identity too late, and may capture operator-only
failure artifacts. Adding product accounting to ordinary SSH and enrichment
paths was rejected because unused product support must impose no cost or
behavioral risk. Enabling a partially implemented core in an integration
environment was rejected because capability evidence must remain truthful.

## Consequences

The backend can complete and test the difficult concurrency and recovery
semantics before asking the UI to migrate. The later UI integration receives a
machine-readable, server-side-only target rather than prose DTOs. Until the
remaining gates land, consumers must continue to observe
`product_session_core = false` and fail closed.

## Maintenance

Update this decision together with `ROADMAP.md`,
`docs/PRODUCT_SESSION_API.md`, `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`,
`docs/TESTING.md`, the current product contract artifact, the product aggregate,
execution and admission boundaries, integration transport, and owning tests if
Phase 2 semantics or release evidence changes. Update decision 0050 before
changing trust ownership, normal-runtime composition, or capability identity.
