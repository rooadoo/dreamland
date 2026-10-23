# DREAMLAND Product API Contracts

Status: Active
Audience: maintainers and private backend API client authors
Purpose: identify the retained Product API targets and contract tooling.

## Overview

These contracts describe explicit backend targets. They are usable by compatible
clients without the separate `dreamland-ui` application. Normal DREAMLAND
startup retains its default-off Product API gate, and the development targets
retain their existing loopback, authentication, and capability restrictions.

## Details

| Contract | Backend target or dependency |
| --- | --- |
| [0.1.0-rc.1](v0.1.0-rc.1/manifest.json) | Capability discovery and cross-cutting contract shapes |
| [0.2.0-rc.2](v0.2.0-rc.2/manifest.json) | Provider-free lifecycle preview |
| [0.3.0-rc.1](v0.3.0-rc.1/manifest.json) | Provider-free asynchronous command/session core |
| [0.4.0-rc.1](v0.4.0-rc.1/manifest.json) | Provider-free Presenter Stage |
| [0.4.0-rc.2](v0.4.0-rc.2/manifest.json) | Live Presenter Stage |
| [0.5.0-rc.2](v0.5.0-rc.2/manifest.json) | Forge-to-Stage handoff contract |
| [0.6.0-rc.3](v0.6.0-rc.3/manifest.json) | Live Forge/Stage and display-safe X-ray |
| [0.7.0-rc.1](v0.7.0-rc.1/manifest.json) | Provider-free source-neutral launch selection |
| [0.7.0-rc.2](v0.7.0-rc.2/manifest.json) | Live source-neutral launch selection |
| [0.7.0-rc.3](v0.7.0-rc.3/manifest.json) | Provider-free authored attendee and World Access dependency |
| [0.7.0-rc.4](v0.7.0-rc.4/manifest.json) | Live authored attendee and live World Access dependency |
| [0.8.0-rc.1](v0.8.0-rc.1/manifest.json) | Provider-free attendee security boundary |
| [0.8.0-rc.3](v0.8.0-rc.3/manifest.json) | Live attendee security boundary with provider-aware privacy identity |
| [0.8.0-rc.4](v0.8.0-rc.4/manifest.json) | Supervised attendee deployment target |

Each artifact includes an OpenAPI definition, manifest, schemas, fixtures, and
version-owned generated client as applicable. Manifests identify the exact
`validate`, `client-smoke`, generation, and publication commands. Run commands
from the repository root. Example:

```sh
.venv/bin/python tools/product_attendee_security_deployment_contract.py validate contracts/product-api/v0.8.0-rc.4
.venv/bin/python tools/product_attendee_security_deployment_contract.py client-smoke contracts/product-api/v0.8.0-rc.4
```

Publication requires matching implementation and artifact commits. Retained
artifacts remain immutable and retain their existing digests. No artifact
publication or capability enablement is implied by this source cleanup.
Historical UI acknowledgement fields describe the original integration process.

See [the complete inventory](../README.md) for omitted snapshots and sibling
World Forge, World Access, and deployment-control contracts.
[The tools review](../../tools/README.md) distinguishes reusable backend tooling
from the original UI-specific review kit.

## Maintenance

Keep this table aligned with target identities and current dependencies. Retire
only superseded snapshots; preserve functional interfaces and generated-client
validation independently of whether a particular UI is distributed.
