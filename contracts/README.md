# Backend Contract Inventory

Status: Active
Audience: maintainers, API client authors, and operators
Purpose: identify retained backend contracts and the source-release retention policy.

## Overview

The source release preserves functional backend interfaces independently of the
separately developed `dreamland-ui`. Contract versions identify different
capability, preview, live-development, and deployment targets. A numerically
newer version does not make every earlier target interchangeable.

## Details

| Family | Retained versions | Reason |
| --- | --- | --- |
| [backend-deployment](backend-deployment/v0.1.0-rc.1/) | `v0.1.0-rc.1` | Private deployment metadata and transport identity. |
| [product-api](product-api/README.md) | `v0.1.0-rc.1`, `v0.2.0-rc.2`, `v0.3.0-rc.1`, `v0.4.0-rc.1`, `v0.4.0-rc.2`, `v0.5.0-rc.2`, `v0.6.0-rc.3`, `v0.7.0-rc.1`, `v0.7.0-rc.2`, `v0.7.0-rc.3`, `v0.7.0-rc.4`, `v0.8.0-rc.1`, `v0.8.0-rc.3`, `v0.8.0-rc.4` | Distinct lifecycle, command, Stage, launch, attendee, and security targets. |
| [world-forge-api](world-forge-api/v0.1.0-rc.4/) | `v0.1.0-rc.2`, `v0.1.0-rc.4` | RC2 provider-free preview and RC4 live generation. |
| [world-access-api](world-access-api/v0.1.0-rc.4/) | `v0.1.0-rc.3`, `v0.1.0-rc.4` | RC3 provider-free preview and RC4 live generated-world access. |

The removed snapshots were:

- `product-api`: `v0.2.0-rc.1`, `v0.5.0-rc.1`, `v0.6.0-rc.1`, `v0.6.0-rc.2`, `v0.8.0-rc.2`.
- `world-forge-api`: `v0.1.0-rc.1`, `v0.1.0-rc.3`.
- `world-access-api`: `v0.1.0-rc.1`, `v0.1.0-rc.2`.

Their historical comparisons and byte-preservation checks are omitted from
the release tests. Current schema, operation, authority, recovery, dependency,
generated-client, and immutability checks remain. Retained artifact bytes and
manifest digests are unchanged. Historical decision records may name omitted
versions; use this inventory for paths present in the source release.

The Product API remains default-off during normal DREAMLAND startup. Explicit
development targets and metadata services retain their existing capability and
production-readiness limits. UI acknowledgement fields in historical manifests
record the original coordination process; they do not require distributing
`dreamland-ui` with the software.

See [the tool review](../tools/README.md) for target launchers, generators, and
retired UI-specific coordination support.

## Maintenance

Retain each contract while a distinct target or an active dependency consumes
it. Remove superseded artifacts together with archival-only tests, and preserve
current behavior and security tests. Change wire contracts through deliberate
new revisions rather than editing published artifacts during cleanup.
