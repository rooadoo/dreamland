# World Forge Headless Live Vertical Slice

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and coding agents
Purpose: record the first provider-backed World Forge checkpoint and its deliberate limits.

## Context

RC2 gives the UI a deterministic, internally consistent WorldSeed and job
lifecycle target, but its selected output is fixture evidence. The next
presentation checkpoint requires one strange typed seed to become one real,
validated immutable world profile and a safe receipt without composing World
Forge into ordinary DREAMLAND runtime startup.

## Decision

Publish the additive `v0.1.0-rc.3` contract and a separately invoked loopback
live process. Preserve RC1 and RC2 unchanged. RC3 distinguishes provider-free
preview provenance from `single_attempt_live_vertical_slice` provenance and
adds a canonical live selected fixture.

Compile the confirmed seed deterministically into a versioned exact
Spiritguide instruction. Generate only candidate A in an isolated owner-only
profile directory. Independently validate world structure, starting-shell
integrity, bounded materialization, scaffold removal, and synthetic boot before
using create-only content-addressed storage. Exclude mutable profile names,
timestamps, and session labels from revision identity.

Permit one in-memory job and one provider call. Cancellation and the advertised
lifecycle deadline atomically fence selection and receipt publication. Do not
claim the Python worker can forcibly cancel an already running provider call;
late valid output may remain as an unselected content-addressed artifact.
Suppress the detached-profile request log in this path because it contains the
compiled audience seed; retain only safe lifecycle, result, and failure-class
records.
Future durable coordination owns crash recovery, hard provider deadlines,
A/B/C attempts, repair, fixed multi-attempt selection, and shared spend/capacity
reservations.

Keep construction explicit. Normal SSH, admin, MCP, product-session, state
profile, and EnrichmentAgent startup imports and paths remain unchanged. The
live receipt grants neither Stage launch nor publication authority.

## Consequences

The UI can integrate and manually evaluate a genuine headless build while
seeing provenance that cannot be confused with fixtures. Provider-free tests
cover every new backend rule; an operator-controlled real smoke remains
separate because it spends provider capacity.

This checkpoint is not the full live World Forge predecessor required for
Forge-to-Stage. Service restart loses in-memory work, provider calls are not
hard-cancelled, redundant attempts and repair do not exist, and provider spend
is bounded only by the single attempt plus configured output cap.

## Maintenance

Implementation: `9bf81e6778107c5a195c905546029fb2e794daef`. Update this
decision when changing compiler or validator revision, canonical revision
identity, attempt count, deadline/cancellation fencing, provenance, artifact
authority, or the normal runtime isolation boundary.
