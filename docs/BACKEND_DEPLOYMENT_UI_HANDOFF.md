# Backend Deployment Handoff for the UI Team

Status: Active
Audience: DREAMLAND backend maintainers, UI/BFF maintainers, deployment operators
Purpose: define the companion deployment interface the backend will provide
while keeping the web UI and its public web infrastructure independently owned.

## Overview

The backend deployment builds and runs DREAMLAND's SSH and simulation services.
The UI project supplies its own images, deployment script, browser-facing
backend-for-frontend (BFF), and web infrastructure. Both deployments may share
one VPS without sharing a Compose project, application data volume, or release
schedule. See [Operations](OPERATIONS.md#docker-deployment).

The [private API foundation](decisions/0109-private-api-foundation.md) adds a
versioned metadata handoff, separate attendee/operator transport credentials,
private identity/health/readiness APIs, and a separate-Compose client fixture.
It exposes no supported Product, Presenter, Forge, or publication operations:
domain readiness is explicitly false. The new
[deployment-control contract](../contracts/backend-deployment/v0.1.0-rc.1/README.md)
is a release candidate pending companion/human review, not a frozen domain API.
The installed-image and separate-Compose metadata rehearsal passed on the Ubuntu
test VPS on 2026-09-13; the linked checkpoint records the image and tested scope.
The source release keeps the canonical contract and generic client validation.
The historical UI-specific review kit and acknowledgement records are retired.
Semantic rejection and complete-probe coverage live in the deployment-companion
tests; no source-release step depends on the separate UI repository.
The [SSH operator console](OPERATIONS.md#docker-operator-console)
is a host-administration interface; it grants no UI/BFF socket or Docker access
and does not substitute for these role-specific APIs.

## Details

### Ownership

| Backend deployment owns | Companion UI deployment owns |
| --- | --- |
| Backend image, runtime configuration, provider keys, SSH host key, and backend data | UI/BFF images, browser assets, and UI runtime configuration |
| Attendee session, Presenter Stage, World Forge, and publication APIs | Browser routes, cookies, CSRF/Origin/Host checks, and browser-facing response shapes |
| Separate private attendee and operator API authority, backend admission limits, and credential rotation | Separate attendee and operator BFF credentials kept server-side |
| Native SSH configuration and optional SSH guessing protection | NGINX, TLS, hostnames, HTTP Basic Auth, and web guessing protection |
| Backend health, readiness, drain, backups, and update compatibility | UI health, UI updates, UI storage, and public web routing |

The existing browser access requirements transfer to the UI team's deployment:
separate attendee and control hostnames; attendee Basic Auth enabled for
prerelease testing and removed for the conference; mandatory Basic Auth for
control/private pages at every stage; optional attendee web bans, disabled for
the conference; and operator web protection scoped to the control hostname.
These are UI deployment inputs, not backend setup switches or NGINX assets.

The backend must enforce API authorization even when the UI authenticates its
users. Attendee credentials must not authorize Stage, X-ray, Forge, publication,
or administrative operations. New Forge builds remain private; an authorized
operator explicitly publishes a validated immutable revision. Backend services
own publication and attendee eligibility for both browser sessions and SSH.
Existing sessions retain their starting revision.

### Private connectivity between deployments

Phase 2A implements the shared-network arrangement for metadata only. A host-based
BFF loopback option remains later work and must be explicitly shipped and tested:

| UI BFF location | Backend connection |
| --- | --- |
| Separate containers on the same VPS | Backend-owned named internal Docker network, declared `external` by the companion project; project-qualified attendee/operator metadata aliases on port 8080. |
| A process running directly on the VPS | Not supplied in Phase 2A. Later work may add explicit numeric `127.0.0.1` HTTP publications. |

The backend setup owns creation and validation of the integration network. The
UI companion script attaches only its server-side BFF services to the required
network. The network is an integration resource with its own lifecycle: stopping
or replacing either application must not delete a network the other uses.
The descriptor specifies network name, stable aliases, and ports independently
of container IDs. The backend's `deploy/check-api-network.py` refuses conflicting
reserved aliases or unexpected existing network ownership/settings before startup.

Container loopback belongs to that container: `127.0.0.1` in a UI BFF container
does not reach the VPS's loopback listener. The container arrangement therefore
requires a supported backend listener on the private integration network.
It must not publish backend HTTP on host `0.0.0.0` or `[::]` to make connectivity
work. A listener on a container interface and a public host port publication
are different settings and require separate reachability checks. Docker network
membership does not replace API authentication.

Plain HTTP is the intended local/private backend transport. Public browser
HTTPS terminates in infrastructure owned by the UI deployment. Running the UI
on a different host would require a separately reviewed private transport; this
proposal does not make backend HTTP an Internet service.

### Metadata API setup and handoff

Build the backend image from `deploy/Dockerfile`, then record its immutable
image digest and the full source commit in a private, non-secret Compose
environment file. The image includes the package and its contract assets; it
does not need repository development tools or a checkout of the older Product
contracts.

```dotenv
DREAMLAND_API_PROJECT=dreamland-api
DREAMLAND_API_NETWORK=dreamland-api-companion
DREAMLAND_BACKEND_IMAGE=sha256:REPLACE_WITH_TESTED_IMAGE_DIGEST
DREAMLAND_BACKEND_REVISION=REPLACE_WITH_FULL_SOURCE_COMMIT
```

The project name is lowercase DNS-compatible and at most 40 characters; the
network name is at most 63. Keep them distinct from another backend's names.
Use the same environment file for all backend API commands; do not override
the Compose project independently of `DREAMLAND_API_PROJECT`.

```sh
docker compose --env-file api.env -f deploy/compose.api.yaml --profile setup run --rm --no-deps --pull never attendee-init
docker compose --env-file api.env -f deploy/compose.api.yaml --profile setup run --rm --no-deps --pull never operator-init
python3 deploy/check-api-network.py --env-file api.env
docker compose --env-file api.env -f deploy/compose.api.yaml --profile attendee --profile operator up -d --no-deps --no-build --pull never
```

The network check reads Docker metadata and refuses conflicting ownership or
reserved aliases before startup. The backend creates its named network through
Compose when absent. Starting only one role is possible by choosing only its
profile/service. Setup still needs the intended image/revision and matching role.

Initialization preserves an identical completed role byte-for-byte. A different
intent, unsafe file, or partial setup fails instead of overwriting a credential.
There is no automatic migration or rotation. Each volume holds an owner-only
`descriptor.json` and `transport.token`; the credential generation identifier
is opaque and is not derived from the secret. Never mount one role's volume
into the other role or into the companion.

To export the public descriptor, run an offline setup container with its command
overridden to `python -m dreamland.deployment.companion_cli describe --root
/var/lib/dreamland/api`. Describe reads no token bytes. To hand off the token,
mount a separate private destination directory owned by UID/GID 10001 and run
the explicit `export-token --root /var/lib/dreamland/api --output
/handoff/attendee.token` operation using that role's setup service. Token export
creates a new 0600 file and refuses an existing destination; it never prints the
token. Preserve 0700 destination-directory permissions and give the eventual
BFF only its role's token with appropriate ownership. No provider key is exported.

### Separate companion fixture

The backend-owned `deploy/companion-client.compose.yaml` is an API client, not
a UI deployment. Its separate Compose project mounts both non-secret role
descriptors and only the corresponding token for each client. It mounts no
backend data volume and receives no Docker authority.

The private handoff directory has `attendee.json`, `operator.json`,
`attendee.token`, and `operator.token`. Token files must be 0600 and owned by the
fixture's UID 10001. Set `DREAMLAND_HANDOFF_DIRECTORY` to that absolute directory
in the fixture's environment file, together with the image and network settings.

```sh
docker compose --env-file companion.env -f deploy/companion-client.compose.yaml run --rm --no-deps --pull never attendee-check
docker compose --env-file companion.env -f deploy/companion-client.compose.yaml run --rm --no-deps --pull never operator-check
```

Each probe checks exact descriptor/epoch agreement, health, truthful 503 domain
readiness, missing-auth rejection, and rejection of its own token by the other
role. `transport_verified=true` and `domain_ready=false` is the expected result.
The client does not follow redirects or use ambient HTTP proxy configuration.
Its full probe, including DNS, runs in a supervised worker with a 15-second
deadline; a process health probe has a 3-second deadline. Termination/kill
cleanup is bounded separately by another half second. Credentials are read only
inside the worker and are never returned to its supervisor.
The two deployments can stop independently; the backend owns the shared network.
No fixture command restarts SSH or calls an LLM.

### Machine-readable handoff

Phase 2A provides `python -m dreamland.deployment.companion_cli describe --root
PATH` for a non-secret per-role JSON handoff. See the
[descriptor schema](../contracts/backend-deployment/v0.1.0-rc.1/descriptor.schema.json)
and [setup/export instructions](#metadata-api-setup-and-handoff).
The companion can configure its private connection without parsing backend logs
or internal INI files. The descriptor includes release/image identity, role and
opaque credential generation, network/alias/port, deployment-control contract
digest and paths, all-false domain capabilities, and an empty Product contract set.

Later domain deployment must extend the reviewed handoff to carry:

- Handoff schema version, deployment identity, backend release/image digest,
  and configuration revision.
- Enabled API roles and their connection information for the selected
  arrangement: network name and service DNS aliases, or host-loopback URLs.
- Exact supported contract names, revisions, artifact digests, and dependency
  relationships for each role; a release must supply the corresponding
  machine-readable contract artifacts independently of the backend data volume.
- The authenticated identity, capability, readiness, and drain-check mechanisms
  supported by that release, including their response contract versions.
- Supported credential roles and credential generation identifiers, together
  with the separately documented secret-file delivery requirements.

The export contains no bearer, bootstrap authority, provider key, SSH password,
session capability, raw world state, or command transcript. It is deployment
configuration for the companion script, not a browser endpoint. Secret file
contents never become fields of this manifest.

Manifest identity describes the installed release; runtime probes establish
whether that release is currently ready. A stale handoff file or a running
container cannot substitute for authenticated live capability evidence.

### Credentials and data isolation

Phase 2A provisions separate attendee/operator metadata transport credentials.
They authorize only the three deployment-control operations, not future domain
operations. If Forge, Product, or publication services require distinct
transport credentials or bootstrap authorities, each remains separate and
limited to the required role. The handoff must document issuance, exact file
format, ownership/mode checks, rotation, revocation, and restart requirements.

The operator will deliver only those BFF credentials through protected files
or an equivalent secret-management channel to the companion deployment. Each
BFF mounts only its required files read-only; browser code, image layers,
command arguments, logs, and the non-secret manifest never contain their
values. Rotation must be deliberate, with a documented cutover and recovery
sequence rather than silent credential regeneration during setup.

The UI needs no provider API keys, SSH credentials, backend configuration
directory, or backend data-volume mount. It uses authenticated APIs to discover
world offerings and to request publication or sessions. Backend backups,
private Forge artifacts, and mutable session data remain backend-owned.
The UI keeps its own browser-session and other BFF storage separately.

### Existing contracts and the production gap

Existing contract artifacts are useful integration references, not current
deployment promises:

| Reference | Verified present boundary |
| --- | --- |
| [Product session API](PRODUCT_SESSION_API.md) and [Product contract catalog](../contracts/product-api/README.md) | Private BFF contracts with explicit authority, epoch, replay, capability, and recovery semantics. Ordinary startup still does not provide a production Product listener. |
| [Attendee security development](PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md) and [Product `v0.8.0-rc.4` manifest](../contracts/product-api/v0.8.0-rc.4/manifest.json) | Attendee-only deployment rehearsal remains explicitly invoked, loopback-only, integration-only, and production-capability-disabled. |
| [Forge-to-Stage development](FORGE_STAGE_LIVE_DEVELOPMENT.md) and [Forge `v0.1.0-rc.4` manifest](../contracts/world-forge-api/v0.1.0-rc.4/manifest.json) | Separate private Forge/Stage authorities exist for the explicit local development composition. |
| [World Access development](PRODUCT_WORLD_ACCESS_DEVELOPMENT.md) and [World Access `v0.1.0-rc.4` manifest](../contracts/world-access-api/v0.1.0-rc.4/manifest.json) | Private promotion, reveal, catalog, and launch-selection development contracts pin Product `v0.7.0-rc.4` and Forge `v0.1.0-rc.4` exactly. |

Choosing the highest version number independently from each contract family
does not establish compatibility. Phase 2 must publish and validate a coherent
deployment contract set, preserve immutable published artifacts, and obtain
the required UI coordination acknowledgement for any new supported interface.

Existing development listener, origin, capability, and credential guards must
remain intact. The deployment work must introduce and validate a supported
private service composition; republishing a loopback preview or bypassing its
guards is not that implementation. Proposed health or export operations must
not be presented as already implemented API routes.

### Companion install and independent updates

Once the Phase 2 handoff is implemented, the UI team's script should be able to:

1. Read the backend handoff and reject an unsupported schema, contract set, or
   release combination before changing the running UI.
2. Select the documented private connection arrangement, verify it from the
   BFF's actual network context, and obtain only the required role credentials
   through the operator's approved secret-file handoff.
3. Check authenticated backend identity, current capabilities, and readiness
   for the requested attendee/operator features. A backend may support SSH
   while its browser-facing API roles remain unavailable.
4. Install or replace only UI-owned services and validate their integration
   against the supported contract fixtures and live readiness evidence.
5. Record both image digests and contract identities in deployment evidence so
   the tested backend/UI pair can be reproduced and rolled back.

The UI image and backend image can update independently when their tested
contract compatibility permits it. An incompatible release requires a planned
coordinated update; it must not silently rewrite or restart the other team's
deployment. Each team owns its own rollback and persistent-data compatibility.

Backend draining and restart must be visible to the BFF. The BFF must refuse
new affected work when the required service is unavailable, draining,
unauthorized, capability-disabled, or incompatible. It must not fall back to
an operator credential, a development endpoint, or another service epoch.
Recovery follows the exact supported contract's idempotency and epoch rules;
fresh LLM execution is not an equivalent replay of a lost response. Backend
restart does not promise restoration of in-memory sessions. User-facing
reconnection and maintenance messages remain UI-owned.

### Phase 2 acceptance evidence

The handoff is complete only after the reviewed export schema, supported API
composition, secret delivery, and connectivity arrangements are implemented.
Validation must demonstrate:

- A companion running in a separate Compose project connects using the
  exported integration settings, without mounting backend data or reading
  provider keys; host BFF loopback access is checked if that option is shipped.
- Attendee credentials cannot use operator-only operations, and public world
  availability changes only through explicit authorized publication.
- Missing or incompatible contract/capability evidence prevents UI admission;
  authenticated readiness and drain checks distinguish service states.
- UI-only restart/update leaves the backend and its data intact; backend
  restart or compatible update follows the documented loss/recovery behavior.
- Backend HTTP is unreachable through unapproved host/public interfaces,
  while the approved private path works from the actual BFF context.

This evidence and the UI team's companion contract review are a human
checkpoint. The owner has authorized Phase 2A implementation and disclosed remote
application/Docker work; neither writing this document nor passing its metadata
checks authorizes serving unavailable domain features or skips that human review.

## Maintenance

Update this handoff with the deployment plan whenever ownership, connectivity,
credential delivery, contract compatibility, or runtime readiness changes.
Replace proposed names with verified commands and schema links only when the
owning phase is implemented and reviewed. Keep web setup instructions in the
UI repository and backend runtime operations in this repository.
