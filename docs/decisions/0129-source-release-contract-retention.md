# Source Release Contract Retention

Status: Active
Audience: maintainers, API client authors, and coding agents
Purpose: remove obsolete artifacts while preserving functional interfaces independently of dreamland-ui.

## Overview

The separate UI is not part of the public source release. The backend retains
its SSH, admin REST, MCP, deployment metadata, Product, Forge, and World Access
interfaces and their current development/release restrictions.

## Details

Retain contracts for distinct active targets and exact current dependencies.
Remove superseded snapshots whose only remaining consumers preserve historical
bytes or compare old revisions. Preserve current schema, authority, privacy,
recovery, dependency, generated-client, and immutability regressions.

[The contract inventory](../../contracts/README.md) records the retained and
omitted versions. Retained artifacts keep their exact bytes and manifest hashes.
Historical decision records can describe versions omitted from the source tree.

Source generators are shared code even when their original generated snapshot
is retired. Keep layered builders and backend preview/live entry points until
a deliberate refactor replaces their behavior. No src runtime code changes are
part of this cleanup.

The pinned UI deployment review kit and its dedicated validator, tests,
handoff, acknowledgement record, and coordination decision are retired.
[The tools review](../../tools/README.md) records the retained tools. Unique
identity/health rejection, readiness semantics, operator-role success, epoch
agreement, and redirect cases now live in the owning deployment-companion and
client tests. Existing descriptor, error-envelope, authorization, file-safety,
and canonical-artifact checks remain in those suites.

The canonical deployment contract and packaged artifacts stay byte-identical.
No runtime code, HTTP route, credential scope, or capability changes. The
historical UI-review fields in immutable manifests are preserved as metadata;
source publication does not depend on releasing the separate UI.

## Maintenance

Update the inventories and owning tests with contract retirement. Change runtime
interfaces or wire identities through a separate behavior change, not by deleting
artifacts or editing published manifests during release cleanup.
