# 0104 Deployment Foundation And Phase Checkpoints

Status: Active
Audience: DREAMLAND maintainers, deployment operators, companion UI maintainers
Purpose: record the backend deployment foundation and explicit phase/remote
review requirements.

## Overview

The owner wants an Ubuntu VPS prerelease that can later become the presentation
release. DREAMLAND owns native SSH lobby, attendee session APIs, Presenter Stage
and World Forge APIs, and explicit world publication. The UI project owns its
companion deployment and browser edge under
[decision 0105](0105-backend-and-ui-deployment-ownership.md).
[Operations](../OPERATIONS.md#docker-deployment) documents deployment commands.

The owner authorized phases with a human review stop after each. Begin with
packaging and isolated SSH so configuration, assets, credentials, and restart
behavior can be reviewed before further runtime integration. The
[project model](../PROJECT_MODEL.md) remains entry-point neutral; SSH is the first
checkpoint rather than the complete product.

## Details

### Foundation

Use `python -m dreamland.deployment` for explicit initialization, non-secret
inspection, validation, hidden-prompt SSH credentials, and an existing-daemon
wrapper. Store configuration, the three
[bundled worlds](0103-bundled-lobby-example-worlds.md), catalog/provenance,
credentials, and initialization marker under an operator-selected root.

Backend-only schema 2 records the SSH service request, optional SSH bans, and
private-by-default Forge policy. Validation declares
`stage="phase1-ssh-foundation"`, `public_release_ready=false`,
`backend_http_implemented=false`, and `ssh_bans_implemented=false`. Requested
future policy is not current enforcement. Ordinary startup and guarded Product
API composition remain authoritative.

Initialize with packaged assets and fresh secrets. Preserve configuration, world
edits, credentials, and catalog removals on rerun. The initialization marker
separates seeding from later invocation; rerun is neither reset nor automatic
prerelease-to-conference promotion. Reject legacy schema-1 configuration until
an explicitly reviewed migration or initialization in another directory; never
silently discard legacy fields or state.

CLI switches select SSH, lobby/direct entry, and SSH bans. Conference bans cannot
be enabled. `show` exposes stored policy and world count; models stay in the INI.
`credentials_checked=false` distinguishes offline validation from consumption.
The wrapper discards ambient lab keys, runtime/provider URL overrides, proxies,
and tracing settings. Every active role uses OpenAI; command and Spiritguide
default to `openai:gpt-5.5`. Tracing is off and configurable logging excludes
reply payloads by default.

Hidden input must be available. Both secrets are validated before either file is
replaced, but the pair is not one atomic transaction. Rotate with SSH stopped and
complete both before restart.

Phase 1 uses non-root UID 10001, a named volume at `/var/lib/dreamland`, explicit
initialization, and SSH published at host `127.0.0.1:2222`. Durable identity and
catalog data survive replacement; live sessions remain temporary. See the
[Dockerfile](../../deploy/Dockerfile) and [Compose file](../../deploy/compose.yaml).
Record actual image/runtime validation separately from file presence.

Docker stop uses `SIGINT` to match graceful shutdown. Separate build/runtime locks
contain exact compatible wheel hashes for CPython 3.12/Linux-amd64. Dependency
updates require clean lock installation, package checks, an image build, and review.

Install `docker.io`, `docker-compose-v2`, and `docker-buildx` from Ubuntu's normal
repositories. Require Engine 28.0.0 or newer because earlier versions have a
same-network exception for localhost-published ports; see
[Docker port publishing](https://docs.docker.com/engine/network/port-publishing/).
Verify the version before container validation and preserve manual iptables
alongside Docker-managed network rules.

### Later phases and backend access policy

The phases are foundation; backend runtimes and companion API contract; backend
SSH exposure and optional bans; backend updates/diagnostics; backend rehearsal
and release review. Stop for human approval between phases. Automated checks
provide review evidence and do not authorize continuing.

Phase 2 provides private HTTP connectivity for separately deployed UI services,
attendee/operator credential separation, backend authorization, release and API
compatibility metadata, and health/capability checks. Never bypass development
guards. New Forge builds stay private until deliberate operator publication of
validated immutable revisions to attendee API and SSH catalogs.

SSH bans are optional, default on in prerelease, and off at the conference.
The shared SSH password remains required even when publicly announced. Backend
API credentials remain required in both profiles, independently of browser
authentication. Session/admission/provider limits remain independent of SSH bans.

Use manual iptables for backend SSH; do not introduce UFW or manage web-edge rules.
Respect Docker forwarding, preserve administrative SSH, and cover IPv4/IPv6.
Disabling bans removes active/persisted enforcement without affecting other rules.

### Deployment records

Keep actual server inventories, rollout results, and approval history in private
operator records.

Do not commit private addresses, account/key details, or keys. Authorized key use
never prints private contents. Registry and diagnostic capture/retention remain
pending; they do not block the isolated foundation.

## Maintenance

Keep [Operations](../OPERATIONS.md), the
[ownership decision](0105-backend-and-ui-deployment-ownership.md), CLI, container
assets, and owning tests aligned. Record actual validation and approvals without
presenting later phases as delivered. Foundation implementation started in
`1784fe5`; this active decision follows the owner's later scope correction rather
than retaining superseded UI setup responsibilities.
