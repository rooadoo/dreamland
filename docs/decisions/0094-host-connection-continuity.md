# 0094 Host-Local Active SSH Connection Continuity

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators
Purpose: record runtime ownership, host projection, and persistence of active SSH connections.

## Overview

Network inspection previously received the same external transport metadata on
every simulated host. An entry host whose profile used an unrecognized address
shape could also expose the real loopback listener and deployment port. Together
these behaviors carried the entry socket across SSH pivots and contradicted
host-local network state. This change implements the approved connection
continuity plan within the backend lane and supports the computing-network
direction in [Project Model](../PROJECT_MODEL.md).

## Details

### Runtime ownership and host projection

Active foreground SSH connections belong to a session-local runtime registry.
Stable IDs, endpoint pairs, source ports, authentication phase, and shell-frame
ownership survive nested context changes. Each participating host receives its
own local/peer socket rows; the starting host retains its incoming connection
while later hosts see their own immediate SSH peer. Local shells inherit their
login association. Explicit SSH transition metadata supports alias commands and
loopback connections without mistaking a hostname change for a network login.

The entry connection is initialized after selecting the actual world and uses
its starting host's primary address on port 22. Later SSH connections retain
their destination service ports. Real bind addresses and ports remain transport
diagnostics. Existing profile address shapes are accepted, and missing required
host addresses are materialized once for continuity. Endpoint selection reuses
established addresses, aliases, and structured source routes. Explicit
`connection_translation` network facts preserve SNAT/DNAT through separate
client/server peer observations; see [Network Connections](../NETWORK_CONNECTIONS.md).
Narrative topology is not parsed into routes or translation rules.

When `exec ssh` replaces an owning shell, its frame retains the upstream socket
ownership as well as its new connection. Exiting the replacement closes both
connections owned by that frame. A local key-passphrase prompt opens no socket
until the client reaches the remote peer.

The command emulator receives the active frame's simulated login ingress in
`environment_metadata.connection` and the active host's socket projection in
`metadata.active_network`. This metadata is refreshed on every command,
including incremental LangGraph calls. Runtime-normalized shell environment
and world changes must take precedence over cached model state. New SSH logins
initialize `SSH_CONNECTION` and `SSH_CLIENT`, after which normal shell editing
and environment-clearing behavior still applies.

The registry remains hidden runtime state. The model communicates SSH lifecycle
with the existing stack transition contract and optional compact
`metadata.ssh` target/port annotations. It cannot directly patch socket IDs or
the full registry. Authored services, ordinary ambient sockets, historical
login records, and durable network facts remain separate world content.

### Snapshot and visit boundaries

Exact runtime snapshots retain connection IDs and ownership because product
execution can reconstruct session state between commands. Applying an exact
snapshot to a live connection reconciles nested connections with that live
entry connection. World/system saves establish durable worlds and fresh login
state rather than reviving old sockets. Offline profile loads and headless
sessions create no implicit SSH entry socket. Native lobby visits own separate
registries and release them when the visit ends. Older profiles without a
registry retain load compatibility.

Transport observations capture raw socket endpoints separately from simulated
command trace endpoints. Detached product connection events carry a `proposed_`
prefix until the product core accepts their state effect. Cancellation fences
discard remote state changes, including provider adapters without streaming and
subsequent commands in a shell command list.

### Verification and limits

Owning-layer tests cover endpoint selection, lifecycle, environment semantics,
snapshot round trips, and checkpoint metadata refresh. The
[manual checkpoints](../TESTING.md#active-ssh-connection-checkpoints) cover
native entry, A → B → C hops, authentication, custom ports, local contexts,
save/restore, and fresh lobby visits.

`ss` and `netstat` text remains model-generated. Runtime facts eliminate the
shared transport source of contradiction but do not guarantee faithful table
rendering or implement a general TCP stack. Repeated live inspection is a
release checkpoint; a deterministic TCP-table renderer backed by this registry
is a separate bounded follow-up if rendering remains inconsistent.

On 2026-09-11, 622 automated regression tests passed across the owning network,
codec, prompt, graph, backend, SSH/Paramiko, lobby, admin, console, enrichment,
and product suites. A final offline profile-application correction then passed
all 61 connection-profile, state-profile, and product-runtime checks. Ruff lint,
format checks, and `git diff --check` passed.

## Maintenance

Keep the registry/session lifecycle, agent context, environment/state prompts,
profile codecs, [Agent State](../AGENT_STATE.md), and
[State Profiles](../STATE_PROFILES.md) aligned when changing endpoint ownership,
frame association, snapshot behavior, or network inspection authority.

