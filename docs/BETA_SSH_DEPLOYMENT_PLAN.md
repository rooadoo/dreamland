# Beta SSH Deployment

Status: Active
Audience: project owner, deployment operators
Purpose: run the SSH backend for outside beta testers with session tracing,
using the deployment lifecycle tooling, while UI and HTTP work stays paused.

## Overview

The beta path puts the existing SSH backend in front of a small group of
outside testers before the Phase 2 runtimes exist. It adds deployed session
tracing and external SSH exposure to the Phase 1 foundation. It adds no HTTP
API, no companion UI, and no new world features.

Beta is a temporary bridge, not a promotion. SSH failure bans remain
unimplemented, so this deployment depends on a strong shared password and
active operator monitoring rather than automatic enforcement. Do not treat a
successful beta as production readiness; see
[decision 0104](decisions/0104-deployment-foundation-and-phase-checkpoints.md)
for the phase boundary this sits inside.

What beta changes against the Phase 1 foundation:

- Session tracing is enabled and required, under the user agreement. See
  [decision 0111](decisions/0111-beta-and-conference-session-tracing.md).
- SSH may publish on an external interface instead of host loopback.
- Installation, upgrade, backup, and rollback run through
  [`deploy/dreamland-deploy`](../deploy/dreamland-deploy). See
  [decision 0112](decisions/0112-deployment-lifecycle-and-backups.md).

What beta keeps unchanged: the three curated lobby worlds, lobby entry mode,
the OpenAI-only role model stack, the operator console, container isolation,
and the deployment volume layout.

## Details

### Profiles

| Profile | Tracing | SSH bans | Intended use |
| --- | --- | --- | --- |
| `beta` | Required | Unimplemented | Invited outside testers |
| `conference` | Required | Disabled by policy | Announced public password |
| `prerelease` | Disabled | Unimplemented | Earlier private rehearsal |

Beta and conference both collect session traces. Prerelease does not and is not
expected to be used again. A profile is fixed at initialization: there is no
profile-change command, and promotion is a fresh install plus a world restore,
as described under [Promotion](#promotion-out-of-beta) below.

### Installation

The server needs Docker Engine 28.0.0 or newer with the Compose plugin. It does
not need Python: every deployment command runs inside the backend container.

Build a package on the workstation with
[`deploy/package-for-server.sh`](../deploy/package-for-server.sh), copy it to
the server, extract it, and install:

```sh
tar -xzf dreamland-backend-TIMESTAMP.tar.gz
cd dreamland-backend-TIMESTAMP
DREAMLAND_SSH_HOST=0.0.0.0 deploy/dreamland-deploy install --profile beta
```

`install` builds the image, initializes deployment state, requests only the
credentials that are missing, validates, and starts SSH. It is safe to re-run:
existing worlds, configuration, and stored credentials are preserved.

Installation requests the shared SSH password, the deployment OpenAI key, and
the Langfuse public key, secret key, and host. Values are typed into the
container with hidden prompts and never appear in arguments, files, or logs.
Use a dedicated Langfuse project and a dedicated OpenAI key for the deployment;
never copy a lab key or lab configuration onto the server.

Omitting `DREAMLAND_SSH_HOST` keeps SSH on host loopback, which is the correct
setting while preparing a deployment that is not ready for testers yet.

Confirm the result before inviting testers:

```sh
deploy/dreamland-deploy status
```

Expect `"stage": "beta-ssh-langfuse"`, `"langfuse_tracing_enabled": true`,
`"world_count": 3`, and `"ssh_bans_implemented": false`. `validate` is offline:
`"credentials_checked": false` means no provider has accepted the stored key.

### Operating the deployment

Day-to-day operation stays in
[`deploy/dreamlandctl`](../deploy/dreamlandctl):

```sh
deploy/dreamlandctl status
deploy/dreamlandctl logs
deploy/dreamlandctl console
deploy/dreamlandctl console --command 'sessions --live'
deploy/dreamlandctl stop | restart
```

The console is the live operator view of sessions, worlds, logs, traces, and
enrichment. It reaches the running backend through a private socket and grants
no Docker authority. See the
[Docker Operator Console](OPERATIONS.md#docker-operator-console).

### Security posture

Container isolation is unchanged from Phase 1: non-root UID 10001, read-only
root filesystem, dropped capabilities, no new privileges, a private console
tmpfs, and memory and PID limits. The Docker socket is never mounted into the
application. Credentials live in the deployment volume, not in the image.

The exposure beta adds is deliberate and incompletely defended:

- **No SSH failure bans.** Nothing counts failures across connections, expires
  a block, or supports manual unban. This is Phase 3 work and is not
  implemented. Two interim controls reduce the guessing rate without providing
  enforcement: every password check is preceded by a two-second delay, and one
  source address may hold at most five concurrent connections, with over-limit
  sockets closed before SSH negotiation and logged. Paramiko separately drops a
  connection after ten failed attempts on it. An attacker can still reconnect
  serially, and nothing remembers a disconnected source, so depend on a long
  random shared password, active log review, and manual blocking. See
  [decision 0113](decisions/0113-ssh-guessing-stopgap-controls.md).
- **One shared password for every tester.** There is no per-user identity to
  revoke. Rotating the password affects everyone at once.
- **No automatic response.** Nothing reacts to an attack; an operator must.

In lobby mode the SSH username has no product meaning (the in-world persona
comes from the world's own catalog account, not the transport username), so
the authenticator additionally requires the literal username `dreamland`. This
rejects most generic scanners before any password is checked and gives future
log-based detection a stable field to key on; it adds no real secrecy, since
`dreamland` must be documented for testers to connect. The rejection is
delayed the same two seconds as any other attempt, so a wrong username costs
exactly as much as a wrong password and cannot be distinguished by timing.

Keep administrative SSH on port 22 with key-only authentication, separate from
the DREAMLAND listener on 2222. A ban or firewall action must affect only the
DREAMLAND port.

### Authentication attempt logging

Paramiko's own per-attempt line carries a raw, attacker-controlled username and
is deliberately filtered before it reaches any log; a client can put a
terminal escape sequence or fake log line in that field, so it is never safe
to record or display verbatim. See
[decision 0113](decisions/0113-ssh-guessing-stopgap-controls.md). There is
currently no per-attempt authentication log; the lifecycle log records
completed lobby sessions, and the connection-limit refusal described below
records only the source address.

```sh
deploy/dreamlandctl --project dreamland logs
```

### Incoming handshake diagnostics

TCP scans and clients that close, reset, time out, or send an invalid SSH banner
produce one INFO `event=ssh_handshake_failed` lifecycle line rather than a
Paramiko error traceback. Unsupported SSH versions or algorithms produce a
WARNING; unrecognized/internal failures retain ERROR tracebacks. Each event
includes `connection_id`, socket `peer_ip`/`peer_port`, `phase`, `reason`, and
`elapsed_ms`, without raw banner or credential content. These events use the
existing logging level and `lifecycle` event controls. A failed handshake is
not a simulated session and does not create a Langfuse session trace.

This is diagnostic classification, not banning or flood prevention. Existing
timeouts, authentication controls, and connection caps are unchanged. See
[decision 0114](decisions/0114-ssh-handshake-diagnostics.md) for the reason codes,
privacy boundary, compatibility dependency, and manual verification steps.

### Incident response

Block a source address without stopping the service. An INPUT rule is not
enough for forwarded container traffic, so use the Docker chain:

```sh
sudo iptables -I DOCKER-USER -s ATTACKER_ADDRESS -j DROP
sudo iptables -L DOCKER-USER -n -v
sudo iptables -D DOCKER-USER -s ATTACKER_ADDRESS -j DROP
```

Host firewall edits affect other services too. Review the intended traffic scope
and preserve administrative access before changing rules.

Stop admitting sessions immediately:

```sh
deploy/dreamlandctl stop
```

Rotate the shared password, which requires a restart to take effect:

```sh
deploy/dreamlandctl stop
docker compose -f deploy/compose.yaml --profile setup run --rm init \
    python -m dreamland.deployment configure-ssh --root /var/lib/dreamland
deploy/dreamlandctl restart
```

Notify every tester when the password changes; the old one stops working.

### Tracing and tester disclosure

Traces carry attendee commands, model replies, token counts, and timing. They
do not carry the SSH password, the OpenAI key, or backend file paths. The
Langfuse project boundary is the access and retention boundary, so beta uses a
project separate from development and evaluation work.

Tell testers their sessions are recorded before they connect. The lobby already
carries the user agreement, so no separate in-session notice is needed.

### Backups and rollback

Back up before any change worth undoing. A backup is the complete deployment
volume, so it restores a whole point in time, including worlds, configuration,
credentials, and SSH host identity:

```sh
deploy/dreamland-deploy backup --label before-tester-round-2
```

Archives are owner-only and contain secrets. Keep them off shared storage and
out of Git. `upgrade`, `rollback --backup`, and `uninstall` take their own
backup first unless `--no-backup` is given, and no lifecycle command ever
deletes one.

Upgrade by extracting the newer package and running its own script:

```sh
tar -xzf dreamland-backend-NEWER.tar.gz
cd dreamland-backend-NEWER
deploy/dreamland-deploy upgrade
```

Roll back the code without touching data, or restore an entire point in time:

```sh
deploy/dreamland-deploy rollback --image IMAGE_ID
deploy/dreamland-deploy rollback --backup BACKUP_NAME
```

### Promotion out of beta

Promotion is a fresh installation, not a converted deployment. Beta's shared
password is known to testers and its stored state carries beta credentials, so
carrying that volume forward would carry those with it.

```sh
deploy/dreamland-deploy backup --label final-beta
deploy/dreamland-deploy install --profile conference --project dreamland-conf
deploy/dreamland-deploy --project dreamland-conf restore-worlds --from BACKUP_NAME
```

`restore-worlds` copies only the lobby catalog, provenance, and world profiles.
It does not copy credentials, deployment policy, runtime configuration, SSH
host identity, or the live visit registry. Enter fresh credentials for the new
deployment and announce its own password.

Conference additionally requires the Phase 3 exposure work before a public
announcement; tracing and a shared password alone are not that review.

### Beta acceptance

Record results rather than inferring them. Separate offline validation from
live evidence, and provider success from capacity measurement.

Functional:

- Testers connect over external SSH and reach the lobby.
- All three curated worlds load and accept commands.
- Traces arrive in the beta Langfuse project for command, Spiritguide, and
  enrichment roles.
- The operator console stays usable while sessions are live.

Operational:

- A restart preserves worlds, catalog, SSH host identity, and stored
  credentials, and testers reconnect afterwards.
- `backup` and `rollback --backup` were each exercised at least once.
- Authentication logging and the manual block procedure were both tested.
- Log volume and disk use stay within the server's capacity.

Record provider-authentication failures and measure latency and concurrent
capacity on the intended deployment before treating beta as accepted.

### Paused work

Beta pauses everything outside SSH. Phase 2A metadata services, the
deployment-control contract review, the companion UI handoff, the attendee,
Presenter, Forge, and publication runtimes, and all browser work stay where
they are until this checkpoint is reviewed. The existing development guards
stay intact; beta does not relax them.

## Maintenance

Update this document with the deployed profile, exposure, tracing, lifecycle
commands, and security posture as they change. Record dated beta evidence and
incidents in private deployment records, keeping offline validation separate
from live results. Keep [Operations](OPERATIONS.md) and decisions
[0111](decisions/0111-beta-and-conference-session-tracing.md),
[0112](decisions/0112-deployment-lifecycle-and-backups.md),
[0113](decisions/0113-ssh-guessing-stopgap-controls.md), and
[0114](decisions/0114-ssh-handshake-diagnostics.md) aligned. Archive
this document when beta closes and the next phase resumes.
