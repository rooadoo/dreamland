# 0109 Private API Foundation Before Domain Deployment

Status: Active
Audience: DREAMLAND backend maintainers, deployment operators, companion UI team
Purpose: establish a truthful private integration boundary without promoting
development-only Product/Forge services into a supported release.

## Overview

After accepting the deployed SSH console and its project-selection helpers,
the owner authorized continued phased development. Phase 2 begins with a
reviewable private API and handoff foundation, followed by domain execution and
publication checkpoints. The accepted foundation was merged into local `main`;
Phase 2A branches from that state.

Implementation commit: `5a55423e6beebc170468a8b0b0809ffd922baf1d`.
The installed-image and independent companion rehearsal passed on the Ubuntu
test VPS on 2026-09-13; human acceptance remains a separate checkpoint.

## Details

The existing attendee and combined Forge/Presenter HTTP compositions live in
development tools, deliberately reject non-loopback bindings, and advertise
disabled capabilities under immutable release-candidate contracts. Their runtime
domain components are reusable, but supported transport, model mapping,
multi-world execution, sustained capacity, supervision, and durable publication
need explicit owning work. Do not expose those tools or change their existing
contract truth to accelerate deployment.

Create a separate `dreamland.deployment-control` contract and packaged HTTP
implementation for identity, process health, and domain readiness. A functioning
metadata listener returns domain readiness 503 with admission false. Domain
capabilities stay false and no Product contract set is advertised. The new
artifact remains a release candidate pending companion/human review.

Deploy independent attendee/operator metadata containers with separate private
credential volumes, mounted read-only when serving. Offline initialization and
explicit file export own credential lifecycle. Describe exports no secret bytes.
New credentials authorize metadata only, not future domain bootstrap or session
authority. An image/revision change or partial setup must not silently rewrite
identity or rotate credentials; supported updates arrive in a later checkpoint.

Use a backend-owned internal Docker network, project-qualified role aliases,
and no host HTTP publications. Check existing network ownership and reserved
alias conflicts before startup. The separate companion attaches the network as
external; it receives only public descriptors and its own role credential, never
the Docker socket or a backend/provider volume. A backend-owned client fixture
tests that topology, identity agreement, missing/wrong-role denial, and explicit
domain unavailability. These checks require no UI or NGINX changes.

HTTP handling and clients must be bounded, redact credentials/errors, avoid
automatic request replay, and close predictably. Metadata health does not prove
provider access, sustained user capacity, or conference readiness. Existing SSH
stays available on its tested image during this independent checkpoint.

## Maintenance

Update this decision and the [companion handoff](../BACKEND_DEPLOYMENT_UI_HANDOFF.md)
when adding domain authority, changing credential scope/topology, or advancing
contract readiness. Preserve published contract artifacts and record separate
companion acknowledgement and deployment evidence.
