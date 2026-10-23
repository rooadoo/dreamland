# Backend deployment control 0.1.0-rc.1

Status: Review candidate
Audience: backend maintainers, companion client authors, deployment operators
Purpose: define the private metadata handoff without claiming that attendee,
Presenter, World Forge, or publication APIs are available.

## Overview

This is a separate deployment-control contract. It does not extend or replace
the Product, World Forge, or World Access contracts. The metadata services run
as independent attendee and operator role containers, with separate private
state roots and transport credentials. They contain no provider credentials,
SSH authority, actor contexts, session capabilities, or world data.

The canonical artifacts preserve their original release-candidate identity and
review metadata. The historical UI-specific review kit and acknowledgement
records are retired; source publication does not depend on distributing a
particular UI. `review_candidate` does not establish production domain capability.
Preserve canonical artifact bytes and publish a new revision for wire changes.

## Details

`descriptor.schema.json` describes the non-secret handoff. `openapi.json`
defines three authenticated GET operations under `/deployment/v1`:

| Operation | HTTP result | Meaning |
| --- | --- | --- |
| `identity` | 200 | Exact handoff and process-specific UUID service epoch. |
| `health` | 200 | The role's metadata HTTP handler is running. |
| `readiness` | 503 | Metadata transport is ready; domain admission is unavailable. |

Readiness reports `transport_ready=true`, `admission_ready=false`, and
`reason=domain_services_unavailable`. Its four domain capabilities are false.
A successful identity or health read does not authorize browser admission,
validate provider credentials, or promise that the future Product APIs exist.
All responses require the exact role's private bearer. Each process start
receives a new epoch. Callers must correlate role, handoff identity and epoch
across probes, rejecting changes before proceeding.

The descriptor names an explicitly managed private Docker network and the
alias `{project}-{role}-api`, using plain HTTP on container port 8080. Project
names are lowercase DNS-compatible names up to 40 characters; network names
are up to 63 characters. The alias and URL must exactly match the project and
role. There is no supported public host HTTP exposure in this contract.
Network membership alone grants no authority.

The non-secret `credential_id` is an opaque UUIDv4 identifier, not a hash of a
secret. The credential is stored separately in `transport.token`, with the
exact ASCII format:

```text
dreamland-{role}-transport:{64 URL-safe base64 characters}\n
```

Here `\n` denotes exactly one terminating LF byte. Each private role directory
must have mode 0700 and be owned by the service's effective UID. Its
`descriptor.json`, `transport.token`, and `.initialize.lock` must be regular,
single-link files owned by that UID with mode 0600. Symlinks and directory
traversal are rejected. Initializing the same intent preserves exact existing
bytes; different intent or incomplete state fails instead of rotating or
repairing credentials implicitly. No migration, hot rotation, or in-place
release update is supported by this first metadata checkpoint.

The operator explicitly exports only the required role token into a new 0600
file within a separate existing 0700 handoff directory. Exports never overwrite
an existing path. Deliver that file to the appropriate BFF through protected
operator tooling and mount it read-only there. Do not mount the backend's role
state directory, SSH data volume, Docker socket, or provider credentials into a
companion. Never place the token in arguments, descriptor JSON, browser code,
images, logs, or diagnostic reports.

`manifest.json` pins the descriptor schema, OpenAPI bytes, and fixture index.
The handoff's `contract.openapi_sha256` must match this kit's exact OpenAPI
bytes. JSON Schema alone cannot validate the digest or correlated alias/URL;
the backend semantic validator covers those checks. The fixture index includes
both structural and semantic rejection cases. Published Product and Forge
artifacts remain unchanged.

The installed package includes byte-identical copies of the three main
artifacts. The test fixtures remain a review/validation kit outside runtime
state. Validate this exact kit locally with:

```sh
.venv/bin/python -m unittest -v tests.test_deployment_companion
```

Backend tooling may call
`dreamland.deployment.companion.validate_contract(Path(...))` for exact artifact
and fixture validation, `validate_descriptor(document)` for handoff validation,
and `validate_response(operation, document)` for identity, health and readiness
semantics. `contract_artifacts()` returns the installed artifact bytes for
independent companion packaging. `describe(root)` reads descriptor bytes and
credential-file metadata without reading the transport token's bytes.

## Maintenance

Keep the source and packaged artifact copies byte-identical. Changes to routes,
response shapes, transport trust, credential formats, readiness meaning, role
authority or descriptor validation require a new reviewed contract revision
after this candidate is committed. Update the owning tests and client integration
guidance with that revision.
