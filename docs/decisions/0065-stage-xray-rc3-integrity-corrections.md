# Stage X-ray RC3 Integrity Corrections

Status: Active
Audience: DREAMLAND maintainers and UI BFF developers
Purpose: record the remaining Phase 6 coordinate, context, and entity-integrity
corrections published in the third X-ray release candidate.

## Overview

Published product contracts `v0.6.0-rc.1` and `v0.6.0-rc.2` remain immutable.
RC2 corrected routes, empty display normalization, strict nested validation,
and requested-detail binding. UI integration then found that RC2 still accepted
out-of-range or incorrectly typed X-ray coordinates, validated success context
less strictly than OpenAPI, and could alias a world fact with a same-named fact
on a host literally named `world`.

## Decision

Publish these corrections as `v0.6.0-rc.3` without changing the two-coordinate
model or public identifier shapes. Both observed and expected accepted-state
revision and X-ray sequence must be non-boolean JSON-safe integers with the
same bounds as OpenAPI. Expected detail preconditions are validated before HTTP
dispatch and canonical integers are used in query serialization.

RC3-generated clients require successful responses to carry a valid service
epoch and an exact three-field contract identity. Top-level response additions
remain allowed where OpenAPI permits them. Safe error envelopes deliberately do
not require `service_epoch`, so HTTP error decoding uses the same exact contract
identity but retains the schema's epoch-less error behavior. This strict mode is
opted into by the RC3 X-ray generator; historical contract generators and their
published generated clients remain byte-identical.

Fact identity inputs use canonical, explicitly tagged world-fact and host-fact
components. Projection entities are admitted only with a unique ID, and summary
plus detail insertion is atomic. A generated collision is omitted and counted,
never published twice or allowed to replace another detail. Final projection
integrity requires the summary ID set to equal the detail-map key set with
matching IDs and entity kinds.

## Consequences

The UI can treat validated coordinates as JavaScript-safe exact integers and
cannot dispatch malformed detail preconditions. A world fact and a same-named
fact on host `world` remain distinct and resolve to their own accepted details.
Impossible hash collisions degrade through explicit omission and incomplete
metadata rather than ambiguous entity binding.

The changes remain inside the explicitly enabled Presenter Stage X-ray service
and generated development client. Normal DREAMLAND startup constructs no X-ray
policy, projection, provider, listener, worker, or state. Every production
capability remains disabled.

## Evidence

Owning tests cover coordinate minima, maxima, overflow, negative, boolean,
string, and float values; pre-transport rejection; canonical query integers;
strict success context and additive top-level compatibility; world/host fact
separation; forced collision omission; stable identifiers; and exact
summary/detail integrity. Existing provider-free loopback, stale recovery,
unavailable entity, projection isolation, close cleanup, replacement rotation,
and startup-inert tests remain required.

The tested RC3 backend implementation is recorded by commit
`a6674bd3c43eda6e1c72403c136a5a69160ca7f6`. The published manifest binds the
generated contract to that exact commit.

## Maintenance

Keep OpenAPI integer bounds, generated-client validation, transport parsing,
service coordinates, and entity-integrity tests aligned. Any expansion of
coordinate types, context fields, or entity identity semantics requires a new
additive contract revision and UI acknowledgement.
