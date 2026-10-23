# 0103 Bundled Lobby Example Worlds

Status: Active
Audience: DREAMLAND maintainers, operators, and deployment authors
Purpose: record how the three selected lobby worlds ship as reproducible examples
while preserving operator state and deliberate publication.

## Overview

The working SSH lobby uses three evaluated worlds whose local profile files are
ignored by Git. Fresh setup previously required reconstructing those copies
from research archives. The operator requested these same worlds as repository
examples and the initial worlds for the planned server deployment.

This implements the [project model](../PROJECT_MODEL.md) through reusable
operator-selected world baselines. It does not change the private default
scenario or introduce a shared persistent runtime world.

## Details

Ship `lobby-supermarket.json`, `lobby-colonyship.json`, and
`lobby-offshore.json` in
[`dreamland.default_state_profiles`](../../src/dreamland/default_state_profiles/).
Retain the exact bytes of the working baselines, including world state and
provenance fields. The existing [provenance record](../../lobby/provenance.json)
identifies the bundled file and world-state hashes. Research evidence and
study provenance are published separately and are not installation inputs.

The [version-2 catalog](../../lobby/catalog.json) preserves the existing titles,
ordering, profile names, and explicit entry accounts: `operator`, `reader`, and
`leah`. These are `world` baselines. Exact byte retention does not turn them into
`exact` session snapshots or authorize publishing later attendee transcripts.

Use the existing package-resource fallback in
[`StateProfileStore`](../../src/dreamland/state_profiles.py). A local file
continues to take precedence, and the neutral packaged `default` remains the
ordinary fallback. Do not change profile listing, deletion, direct SSH defaults,
or lobby admission to make the examples appear automatically.

Lobby publication still requires compatible regular files in the local profile
store and an explicit catalog. The documented initialization copies missing
package bytes with exclusive creation, preserving existing operator files.
Setup from an installed wheel uses the same resources without requiring the
research checkout. The catalog and notices still need separate provisioning.

The planned deployment installer will initialize browser and SSH catalogs with
all three examples. This asset promotion does not implement that installer or
browser provisioning. Future updates must preserve local edits, additions,
ordering, and removals; restoring example defaults must be an explicit action.

Validation should cover packaged-resource loading, exact baseline hashes,
fresh local initialization, preservation of local overrides, and the existing
lobby entry-account checks. Wheel validation must load resources without a
source-tree or research-archive dependency.

## Maintenance

Keep this decision, [State Profiles](../STATE_PROFILES.md), the
[lobby guide](../../lobby/README.md), bundled resources, catalog, provenance,
and owning tests aligned when examples or initialization policy change. Record
deliberate recuration without rewriting archived research or operator state.
