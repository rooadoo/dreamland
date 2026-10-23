# 0106 Detachable Operator Console

Status: Active
Audience: backend maintainers and deployment operators
Purpose: preserve native operator access to the running SSH daemon under Docker
without coupling attendee lifecycle to an administrative terminal.

## Overview

The deployed daemon retains a DreamlandConsole with its live registry but does
not read stdin. The owner approved an SSH-console checkpoint before deployment
Phase 2, with later explicit targets for Presenter and Forge runtimes.

## Details

Expose that existing object through an opt-in private Unix socket in the same
process. Compose exec starts a lightweight client which reuses the existing
terminal console. Starting another backend would create a different registry;
attaching daemon stdout would not restore an input loop. The existing admin API
also does not supply full native console/public-ID parity.

Use an owner-only ephemeral directory and socket, Linux peer-UID validation,
a versioned bounded JSON protocol, and one admitted operator at a time. Log
lifecycle and operation metadata without introducing raw payload capture.
Keep a disconnected in-flight operation admitted until completion; do not
cancel a Python thread, automatically replay a command, or accumulate workers.
Reconnect starts fresh command context and reports the runtime identity.

Client EOF and disconnection do not stop attendee sessions. Route the remote
`shutdown` command to instructions for explicit host Compose stop/restart,
because the daemon's restart policy would otherwise restart an exited process.
Preserve ordinary local-console shutdown. Keep sockets out of persistent data,
add no network console port, and grant no console authority to UI/attendee roles.

The implementation spans `console_protocol.py`, `console_socket.py`,
`console_client.py`, daemon startup, and `deploy/dreamlandctl`. Tests belong at
the console/transport owners, with one real SSH test for attendee survival.
Container and human terminal checks remain separate evidence, followed by a
review pause. See [Docker Operator Console](../OPERATIONS.md#docker-operator-console).

### Selecting the deployment

The owner requested that `--project` be optional when only one deployment is
configured. Use explicit `--project NAME`, then nonempty `DREAMLAND_PROJECT`,
then discovery of a single existing DREAMLAND SSH Compose project. Do not infer
the target from the checkout directory or silently fall back to `dreamland`:
an operator may run a newer wrapper against an existing differently named
deployment, and stop/model-adoption commands must target the intended service.

Discover all containers, including stopped services, with a Compose project
label, service `ssh`, `com.docker.compose.oneoff=False`, and the image label
`org.opencontainers.image.title=DREAMLAND backend`. Deduplicate project names.
Exclude setup and one-off smoke containers so retained test artifacts do not
create false ambiguity. A missing or ambiguous result requires explicit
selection; a volume without a service container is not enough for discovery.
Resolve once per invocation before any Compose action and preserve the stopped
service guard for model adoption. Docker discovery errors must halt the command
instead of being mistaken for an empty result or falling back to another project.

The owner also requested `dreamlandctl list` to show configured projects.
Share its discovery function with automatic selection so the two commands agree
about eligible deployments. Print sorted unique names, one per line, without
choosing a project or invoking Compose. Project selectors and Compose environment
files do not limit this global inventory. No matches is a successful empty list
with a message on stderr; a Docker failure must fail without publishing partial
results. This lists existing SSH service deployments, not volume-only or fresh
configurations that do not yet have a service container.

Validate arguments before discovery and report the automatically selected
project on stderr. This changes only the host wrapper; no backend restart or
new image is required. Owning fake-Docker tests cover selection, precedence,
filtering, ambiguity, and the existing lifecycle guards; actual Docker discovery
has a separate deployment check.

## Maintenance

Revisit this decision when adding service targets, changing console authority,
protocol compatibility, or command cancellation. Shared storage or network
membership must not imply a shared live registry across backend processes.
