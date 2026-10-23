# 0098 Mandatory Foreground Host Activity

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators
Purpose: record the accepted prompt experiment requiring immediate, scaled
background activity and its remaining validation limits.

## Overview

Initial process and socket inventories have often shown little beyond the
foreground login. Existing ambient realism guidance requests surrounding
activity, but its optional breadth, sparse-world interpretation, and allowance
for little historical residue can leave ordinary operational systems sterile.
Asynchronous enrichment also cannot guarantee that activity appears in the
first inspection response.

The user approved an experimental foreground prompt change on 2026-09-11,
including minimum listeners, active remote connections, another active user
on multi-user systems, and sustained web traffic. The experiment implements
the rich, coherent computing-network direction in
[Project Model](../PROJECT_MODEL.md). On 2026-09-11 the user authorized a
checkpoint commit after incorporating main's foreground JSON-mode fix
(`eb3c944`), followed by separate continuity prompt changes for human review.
After reviewing the continuity changes and their live replay, the user accepted
the current experimental state on 2026-09-11 and authorized committing, merging
into `main`, and pushing it. The accepted checkpoint retains the limitations in
[decision 0100](0100-host-activity-continuity-rules.md).

## Details

### Immediate population and scale

Before answering the first process, listener, network-connection, or active-user
inspection on each host, the command emulator must establish a linked
population in that same response. This includes inventories through `ps`, `ss`,
`netstat`, `lsof`, `who`, `w`, and equivalent platform commands. The first
inspection establishes processes, services, socket activity, and relevant user
sessions together, so the order of later commands does not determine whether
those surfaces contain independent activity.

The experimental floors are:

| Host context | Minimum independent activity |
| --- | --- |
| Ordinary general-purpose server | Five background processes beyond the foreground login/inspection chain |
| Networked system | Two distinct additional non-SSH listening port numbers and two active background connections to remote systems |
| Multi-user system | One other distinct user's active login connection |
| Running web server | Five simultaneously active HTTP(S) client connections at every observation during normal operation |

Current DREAMLAND-session sockets do not satisfy the background requirements.
IPv4/IPv6 listener copies do not make distinct port numbers, and a service
account or historical login does not constitute the required other active
user. Qualifying web clients may satisfy the general two-connection floor as
well; five web clients do not imply an additional two mandatory connections.
Web traffic should remain active throughout normal operation, with plausible
connection lifetimes and turnover rather than five permanently frozen rows.

These counts are minimums, not targets or caps. Infer believable volume from
the host's role, expected users and concurrent usage, time and activity context,
and environment scale. A busy institution's shared host or web service should
not stop at the same small counts as a lightly used small system. Platform
capabilities determine the appropriate processes and services; constrained
appliances should not acquire an unrelated general-purpose server stack.

### Continuity and authority

Processes, service ownership, command lines, PIDs, listening ports, remote peers,
user identities, login sessions, and timestamps must agree across views.
Materialize compact `services_by_name`, `users_by_username`, and `facts_by_id`
anchors in the same response using existing keyed world-state collections.
Retain the ownership and lifecycle facts needed to support later observations
even when command history is pruned. Compact persistence must not suppress a
realistic volume of terminal output.

The general ambient evidence examples remain optional; the live-population
minimums and scaling rules are mandatory. Generation liberty governs
discretionary detail and incidental risk without reducing required population.
The prompt consolidates identity and activity registration in one persistence
section. Earlier complete observations remain true at their observation time
without freezing later activity or preventing ordinary connection turnover.

Sparse seeds and ordinary lists of configured services are not exhaustive
absence claims. Low generation liberty, minimal inference, clean installation,
or little historical residue do not independently waive live population.
Explicit operator exclusions, established complete observations, and lifecycle
changes remain authoritative. Do not regenerate a quota on every listing,
resurrect stopped services, or reconnect a user that was explicitly
disconnected. Apply filters, access restrictions, flags, and output limits to
the coherent underlying population, including when they correctly produce no
visible rows.

The runtime-owned current-session SSH registry and its host-local projection
remain authoritative as described in
[decision 0094](0094-host-connection-continuity.md). Background activity is
separate world content: it must not copy, overwrite, or recast the foreground
connection's endpoints or owner. A complete runtime projection of current
session sockets is not an exhaustive list of all ordinary background sockets
on the host.

### Scope and review checkpoint

The behavior lives in the emulator's
[ambient realism](../../src/dreamland/prompts/ambient_realism.md),
[world](../../src/dreamland/prompts/world.md), and
[environment metadata](../../src/dreamland/prompts/environment_metadata.md)
instructions. It does not introduce a state schema, deterministic socket-table
renderer, new configuration switch, or autonomous activity agent. Existing
command/model/state logging provides review evidence. Automated prompt checks
can confirm composition and required instructions; they cannot prove a model
will render correct populations.

1. On an ordinary shared server, run `ps aux`, `ss -lntup`, `ss -ntp`, and
   `who` or `w`. Start another fresh session with the socket command first.
   Verify immediate independent activity and the same coherent population
   across command surfaces.
2. Inspect one generated process, service, remote endpoint, and other user's
   login. Repeat inventories with `ps -ef` and a suitable `lsof` or `netstat`
   view. Check ownership, endpoints, times, and applied state anchors.
3. On a running web server, inspect established HTTP(S) connections repeatedly.
   Verify at least five simultaneous client connections during normal
   operation, plausible continuity or turnover, and volume appropriate to the
   expected users. Compare a small deployment and a busy environment to ensure
   the floors have not become fixed output sizes.
4. Stop a generated nonessential service and disconnect an additional user.
   Verify the associated rows disappear and remain absent, and that normal
   filters and permission restrictions still work. Check an explicitly
   constrained or isolated host for preserved scenario boundaries.

## Maintenance

Keep this decision, [State Profiles](../STATE_PROFILES.md), emulator prompt
modules, and prompt-loading checks aligned when population thresholds, scaling,
identity ownership, or observation/lifecycle rules change. Add review evidence
when the operator evaluates or revises the experiment.

