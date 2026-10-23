# Stage X-ray RC2 Contract Corrections

Status: Active
Audience: DREAMLAND maintainers and UI BFF developers
Purpose: record why the Phase 6 X-ray client and projection corrections require
a new immutable release candidate.

## Overview

Published product contract `v0.6.0-rc.1` remains immutable. UI integration
found that its generated client used generic product-session paths for X-ray,
accepted nested payloads rejected by its own OpenAPI schemas, did not bind a
detail response to the requested entity, and could receive empty display text
from an otherwise Forge-valid selected revision. These are implementation and
publication defects, not changes to the accepted dual-coordinate X-ray model.

## Decision

Publish the corrections as `v0.6.0-rc.2`. Generated X-ray methods use the
explicit Presenter Stage route family and retain percent-encoding and all three
detail preconditions. Detail validation requires the exact requested entity ID.
Generated snapshot and detail validation enforce every strict nested X-ray
object, collection bound, enum, identifier relationship, safe-text constraint,
required field, and `additionalProperties: false` boundary represented by the
contract. Additive response fields remain allowed only where the published
response schema permits them.

Projection normalization treats empty display values according to their role:
optional world, purpose, and operating-system text is omitted; required host
display text falls back to the nonempty host key or `Unavailable`; and a fact
without any nonempty display statement is omitted and counted. Control marking
and clipping remain explicit and bounded. Forge validation is not tightened,
because these values are legitimate accepted inputs and the display projection
owns their safe presentation.

## Consequences

The UI can invoke both generated X-ray methods against the implemented routes
and treat successful validation as the backend-owned trust boundary described
by RC2. A valid detail for another entity cannot be attached to the presenter's
selection. Every X-ray response built from the tested Forge-valid empty,
control-marked, and clipped edge document validates against RC2's response
schemas.

RC1 remains byte-for-byte unchanged. The correction constructs no X-ray policy,
provider, listener, worker, or state in normal DREAMLAND startup. Production
capabilities remain disabled, and projection failure remains isolated from the
Stage terminal and lifecycle surfaces.

## Evidence

Implementation record: commit
`3275152ea60ad9aef96512247969176335ac9954` contains the runtime normalization,
generated-client transport and validation corrections, RC2 artifacts,
documentation, and owning tests described by this decision.

Owning contract tests exercise forbidden nested state, malformed safe text,
kind/ID disagreement, missing and extra detail fields, and mismatched returned
entities. The provider-free combined HTTP test invokes both generated methods
against real loopback handlers, checks every stale coordinate and documented
recovery, and retains unavailable-entity behavior. Runtime and schema tests
cover Forge-valid empty strings, control/format characters, clipping bounds,
close/replacement cleanup, failure isolation, and normal-startup inertness.

## Maintenance

Keep the runtime projection, generated validators, OpenAPI schemas, and live
transport tests aligned. Any future relaxation or expansion of nested X-ray
objects requires a new additive contract revision and explicit privacy review.
