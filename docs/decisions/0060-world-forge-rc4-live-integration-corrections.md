# World Forge RC4 Live Integration Corrections

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and coding agents
Purpose: record the corrective contract and runtime boundary required after RC3 UI integration review.

## Context

RC3 proved a real single-attempt provider path, but strict UI integration found
four contradictions. Recipes retained preview provenance under the live
transport, deadline enforcement depended on watcher scheduling, malformed
referenced hosts could pass synthetic boot, and the canonical live fixture
retained preview receipt values. A fail-closed BFF therefore could not safely
enter the live seed flow or rely on all RC3 evidence.

## Decision

Preserve RC1 through RC3 unchanged and publish the corrections as RC4.

An explicitly sourced service projects that exact source through every
successful operation envelope, including event pages and cancellation results.
The legacy provider-free preview constructs the default source and retains its
published RC2 response shape.

Selection compares the authoritative deadline while holding the same service
lock used for the terminal transition. A selection request at or after the
deadline becomes `deadline_exceeded`; a watcher or cancellation that already
won remains terminal and fences the receipt.

Independent validation requires both referenced entry and active hosts to be
objects. Entry-shell name, absolute command, and non-empty string arguments are
mandatory. Structural failure prevents synthetic-boot success and immutable
storage.

One shared runtime receipt builder owns the live validation summary, selection
reason, revision identity, labels, compiler revision, and validator revision.
The RC4 live fixture uses that builder, and the provider-free HTTP suite compares
the actual selected response with the fixture receipt.

## Consequences

The UI can pin one source tuple from identity and require it through the whole
interaction. A selected receipt is guaranteed to have committed before its
advertised deadline, and canonical fixture tests represent the live wire shape.

RC4 remains a single-attempt, in-memory development predecessor. It does not add
hard provider cancellation, durability, repair, A/B/C selection, Stage launch,
publication, catalog, or attendee authority. Normal DREAMLAND startup remains
unaware of World Forge and invokes no provider for it.

## Maintenance

Implementation: `541ac680dd7ea73b4f07e2f4359c7004e2c9ca26`. Update this
decision before changing source projection compatibility, deadline comparison,
referenced-host requirements, live receipt construction, or immutable prior-RC
boundaries.
