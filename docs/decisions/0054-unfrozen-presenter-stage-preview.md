# Unfrozen Presenter Stage Preview

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and coding agents
Purpose: record the Phase 3 Presenter Stage facade, reserved-capacity model, and continued live-capability gate.

## Context

Phase 2 established the protocol-neutral shared product-session core, but it
deliberately did not provide a Presenter Stage launch contract or claim
reserved capacity. The mutually accepted Revision 3 UI/backend agreement
requires Stage launch to resume only the identical originating selection or
grant under compatible effective policy. Revision equality alone is not
authority. It also requires an explicit expected-control-CAS replacement and
Stage capacity that public saturation cannot consume.

The normal DREAMLAND runtime must remain unchanged while the private product
API is disabled. A first Stage checkpoint must therefore be useful to the UI
without composing a product listener, provider, worker, journal, or Stage
session during ordinary startup.

## Decision

Add `PresenterStageService` as an explicitly constructed, protocol-neutral
facade over `ProductSessionCoreService`. It accepts only Presenter actor and
launch-grant seeds. Its ordinary launch operation behaves as follows:

- exact same-key and canonical-body replay returns the recorded result;
- with no current Stage, it creates the exact granted immutable revision;
- a fresh launch resumes only when the opaque launch-grant identity, selected
  record, immutable revision, actor/role, operation set, limits, expiry,
  policy, and notice binding remain compatible with the current Stage; and
- another selection returns replayable `current_session_exists` with only the
  bounded current/requested selected-record identifiers. The caller must use
  explicit expected-control-CAS replacement.

Store the originating launch-grant digest internally on session and tombstone
records. Continue exposing only the existing semantic launch-binding digest;
the credential-derived grant digest never crosses the private projection.
Stage replacement reuses the Phase 2 provision-before-cutover switch fence,
including busy rejection, failed-provision preservation, authority rotation,
late-work fencing, and count-neutral session admission transfer.

Extend `ProductAdmissionLedger` with two hard classes inside one aggregate
boundary: `standard` and `presenter_stage`. Stage reservations are explicit
subsets of the same total session, logical-job, physical-worker, token, and
spend limits. Standard work can consume only the total minus the configured
Stage reservation. Stage work can consume only its reservation. This prevents
public saturation from borrowing Stage capacity while preserving one
non-overcommitting hard total. Default reservation values are zero, so existing
Phase 2 construction retains its prior limits and behavior.

Expose only trusted, bounded Stage display metadata associated with each
immutable revision: title, source label, and revision label. The first Stage
snapshot explicitly reports that X-ray is unavailable. It does not include raw
runtime state, host graphs, prompts, reasoning, provider envelopes, internal
paths, credentials, or other sessions. Active X-ray remains a separate future
capability and contract.

Publish additive `0.4.0-rc.1` artifacts and a deterministic loopback Stage
preview. The preview reuses shared command, result, cancel, snapshot, event,
transcript, reset, and close routes and adds Stage launch/current/replacement
routes. It includes a deterministic lost-launch-response scenario and the
existing command/feed recovery scenarios. It makes no provider or host-command
call.

## Capability And Runtime Consequences

All five product capabilities remain disabled. In particular,
`presenter_stage_terminal` is not enabled by this checkpoint because the
following live gates remain unmet:

- production private-gateway integration and presenter browser authentication;
- a real product provider adapter with exact attempt/usage enforcement;
- deployment proof that Stage reservations and any shared provider-account
  limits do not overcommit;
- live terminal, reconnect, cancellation, load, cutoff, and cost evidence; and
- coordinated compatibility evidence and the complete Presenter Stage gate.

`compose_product_runtime()` continues to fail closed when product activation is
requested. Normal startup imports no Stage module and starts no Stage listener,
worker, timer, journal, provider reservation, or session. SSH, admin, MCP,
state-profile, logging, and default EnrichmentAgent behavior are unchanged.

The `0.4.0-rc.1` contract remains unfrozen pending the tracked UI documentation
acknowledgement. A successful preview response is integration evidence only,
not evidence that a live capability may be advertised.

## Alternatives Considered

Using independent Stage and public ledgers was rejected because separately
configured ledgers can overcommit one provider account or deployment-wide hard
limit. Allowing public work to borrow idle Stage capacity was rejected because
public saturation could then deny the presentation-critical reservation.

Resuming by immutable revision digest was rejected because two grants can name
the same revision while carrying different authority, limits, expiry, or
policy. Implicitly replacing on a changed selection was rejected because it
would bypass the explicit control CAS and provision-before-cutover guarantee.

Embedding raw or derived X-ray data in the first Stage snapshot was rejected.
The terminal facade needs only safe identity/display metadata; a later X-ray
projection has separate privacy, completeness, and concurrency requirements.

## Maintenance

The implementation and exact generated artifact bytes are recorded in commit
`712dd271cf39ecb1f9cddbec3c438fd68036a6be`. The immediately following
publication commit binds the manifest to that implementation commit.

Update this decision before changing Stage resume identity, Stage replacement
CAS, reserved-capacity partitioning, safe display metadata, X-ray separation,
preview composition, or the `presenter_stage_terminal` enablement gate.
