# 0052 Product Idempotency Claim And One-Request Completion

Status: Active
Audience: maintainers, UI BFF developers, DREAMLAND operators, and coding agents
Purpose: record the failure-replay boundary and response-completion guarantees
for the private product integration tools.

## Overview

Phase 1 introduced an epoch-scoped lifecycle ledger and two manually invoked
loopback integration servers. Integration review found two ambiguous or unsafe
edges: resource-authority rejections occurred before an idempotency key was
bound, and the threaded servers could return from `--once` before their daemon
handler finished writing the response.

Both issues belong to the integration checkpoint that introduced them. They
are corrected without composing a product service into normal DREAMLAND,
enabling a product capability, or changing the SSH, admin, provider, profile,
logging, or enrichment paths.

## Decision

### Idempotency claim boundary

Create and close use a deliberate claim boundary. A request does not claim an
idempotency key until DREAMLAND has validated the private service boundary,
current service epoch, actor context, operation family, bounded key, and a
canonical semantic request body. Failures before that boundary can be
re-evaluated because DREAMLAND cannot yet construct the complete trusted
idempotency scope or body identity.

After that boundary, the lifecycle aggregate binds the epoch, actor and
authority class, operation family, key, and canonical body digest before it
checks resource authority or deterministic business state. It records and
replays deterministic launch-grant and session-authority denial,
`current_session_exists`, `control_revision_conflict`, and successful mutation
results. An exact retry restores the recorded logical result without repeating
authority exchange, allocation, or mutation. Another canonical body under the
claimed scope returns `idempotency_conflict`.

Claim identity and rejection records store canonical request and authority
digests plus bounded safe outcomes; they do not retain raw supplied actor,
launch, or session credentials. A successful create replay record necessarily
retains the newly issued session authority as part of the original bounded
logical result until related tombstone cleanup. Resource-authority failures
remain non-enumerating on the wire.

### Transient capacity and retention

`capacity_exhausted` is the explicit exception after canonicalization. It is a
pre-work, retry-after-delay outcome and does not claim the key. The same key and
body may therefore be re-evaluated after capacity becomes available; no
allocation or mutation occurred on the rejected attempt.

An unrelated deterministic rejection is retained for
`failed_operation_ttl_ms`. A record related through an unguessable consumed
grant or session authority is retained through that session and its tombstone,
and terminalization aligns its deadline with the tombstone's actual retention
deadline. A caller-supplied reference alone never establishes that relationship
or becomes a retention oracle. Lazy operation-boundary cleanup may release a
failed-operation key only after its documented deadline. Service epoch rotation
discards the entire namespace; stale mutations still return `outcome_unknown`
and never redispatch, while deliberate new work requires current-epoch authority
and a new key.

### Response-complete `--once`

Normal development serving remains bounded and threaded. The explicit
`--once` path instead processes its selected request synchronously. The handler
finishes and flushes the complete status line, hardened headers, and bounded
body before the server closes and the process exits. The lifecycle preview
keeps its connection-admission accounting in that synchronous path and closes
the one HTTP/1.1 exchange rather than waiting for another keep-alive request.

Real subprocess tests exercise both tools over loopback, read the complete
response, verify the required headers and body, and require clean process
termination. A delayed Phase 0 scenario makes the prior daemon-handler race a
deterministic regression case.

### Contract revision

The already published `v0.2.0-rc.1` artifact remains unchanged. The clarified
machine-readable policy is published as `v0.2.0-rc.2` with the same operation,
request, response, status, and capability shapes. All five product capabilities
remain disabled and the lifecycle routes remain integration-preview-only.
The published manifest binds the artifact to backend implementation commit
`e711efe72ff4bbe83ff251953c441ae5c9879a84`.

## Alternatives Considered

Recording every failure from the first received byte was rejected because
unauthenticated, stale-epoch, malformed, or noncanonical requests do not supply
a trusted scope and body identity. Recording `capacity_exhausted` was rejected
because it would turn an explicitly retryable pre-work admission result into a
pinned rejection after capacity recovered.

Changing the threaded servers to use non-daemon handlers globally was rejected
because it would alter ordinary development-server shutdown behavior. Mutating
the published RC in place was rejected because existing artifact pins must
remain reproducible.

## Consequences

The BFF can retry an identical create or close after a lost response without
inventing its own claim interpretation. Deterministic post-claim rejections
cannot be converted into a different request under the same key, while
documented transient admission can recover. One-request compatibility tests no
longer observe a successful child process that truncated or reset its response.

The changes allocate no normal-runtime ledger, socket, thread, timer, provider
reservation, or background work. They exist only when the protocol-neutral
lifecycle service or a manual integration tool is explicitly constructed.

## Maintenance

Update this decision together with `docs/PRODUCT_SESSION_API.md`,
`docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`, the current lifecycle contract,
the lifecycle aggregate, both integration tools, and their owning tests if the
claim boundary, rejection taxonomy, retention, epoch behavior, server dispatch,
or capability/composition truth changes.
