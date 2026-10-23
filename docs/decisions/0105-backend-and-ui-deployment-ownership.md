# 0105 Backend And UI Deployment Ownership

Status: Active
Audience: DREAMLAND backend and UI maintainers, deployment operators
Purpose: separate backend and UI deployment ownership while defining the backend
handoff needed for a companion installer.

## Overview

After reviewing the Phase 1 snapshot and image footprint, the owner requested
that DREAMLAND deployment exclude NGINX and web UI setup. The UI is a separate
project and will supply its own deployment process. This replaces the combined
scope from initial planning; backend attendee, Presenter Stage, and World Forge
functionality remains in scope.

## Details

### Ownership

DREAMLAND owns backend images and Compose services, SSH lobby, provider keys,
attendee/operator APIs, backend authentication and authorization, private-first
Forge publication, durable backend state, resource controls, optional SSH bans,
backend SSH firewall instructions, and backend release/update, backup, rollback,
and diagnostic operations.

The UI project owns UI images and services, NGINX, TLS, hostnames, browser Basic
Auth, cookies, CSRF/origin handling, browser proxy settings, web bans, and UI
installation/updates. The owner's separate attendee/operator hostnames and always
protected private browser pages remain UI requirements. This repository neither
generates those settings nor provisions those services. Coordinated presentation
rehearsal does not transfer ownership.

Backend authorization remains authoritative regardless of browser policy. Each
companion service receives only its role-specific backend credential. Attendee
credentials cannot operate Stage, Forge, publication, or administrative APIs.
Private builds become available through backend-authorized explicit publication;
hiding a browser page is not an authorization boundary.

### Companion contract

Phase 2 must provide supported private HTTP deployment and a versioned handoff
containing backend release/API versions, capabilities, private endpoints, and
health/readiness checks. The [UI handoff](../BACKEND_DEPLOYMENT_UI_HANDOFF.md)
tracks current availability and the planned integration contract. Use stable
aliases on a documented external Docker network that separate UI Compose
services can join. Keep backend HTTP unpublished
on public host interfaces. Networking provides connectivity; protected APIs still
authenticate and authorize clients. Existing development guards remain intact
until the supported path is implemented and tested.

Publish backend setup and smoke-check instructions sufficient for the UI team to
write its companion installer. Hand off credentials privately and document
rotation/restart behavior. Keep secrets out of discovery metadata and release
manifests. UI runtime/BFF containers need neither the Docker socket, provider
keys, nor direct access to persistent backend state. The companion installer may
use the host Docker CLI to manage its own services.

Each project owns its release and update scripts. The backend publishes API
compatibility and maintenance/admission information so companion deployment can
detect incompatibility or unavailability. Backend scripts do not deploy or
restart UI services. Incompatible API changes require explicit coordination
between independently versioned releases.

These are Phase 2 deliverables. Phase 1 remains SSH-only and explicitly reports
that backend HTTP and SSH bans are unimplemented.

### Configuration transition

Backend-only schema 2 removes UI requests, web authentication, and web ban switches.
The Phase 1 service request is `ssh`; planned policy contains `ssh_bans` and
`forge_private_by_default`. SSH bans remain optional in prerelease and disabled
in the conference profile.

Reject schema-1 configurations instead of silently discarding fields or altering
state. Existing directories need an explicitly reviewed migration or initialization
in another directory. Reruns preserve files and do not migrate or promote the
deployment. This scope correction implements no automatic migration.

## Maintenance

Update this decision and the [UI handoff](../BACKEND_DEPLOYMENT_UI_HANDOFF.md)
when connectivity, release compatibility, or ownership changes. Maintain concrete
handoff artifacts with backend APIs after Phase 2. Keep the
[foundation decision](0104-deployment-foundation-and-phase-checkpoints.md) and
[Operations](../OPERATIONS.md) aligned with the backend-only configuration boundary.
