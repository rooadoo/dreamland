# 0027 Host-Local World State

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators, and coding agents
Purpose: record the move away from a global runtime-state bucket toward
host-local materialized state.

## Overview

The first unified world-state schema still kept generated artifacts, services,
facts, directives, hooks, and similar runtime material under a top-level
`runtime_state` object. That made the command context harder to localize once
the simulation started tracking multiple hosts. It also kept exact command
history as a session-wide record even though commands are executed from a
specific active host.

Future network features need host-specific context: a file belongs to the host
where it was read or written, a service belongs to the host where it listens,
and command history should inform the emulator primarily for the host where
the command ran.

## Decision

Remove `runtime_state` from current state profiles and model-facing world
state.

Keep global material only when it is genuinely cross-host or world-scoped:

- `generation_policy`;
- `expansion_directives_by_id`;
- `world_facts_by_id`;
- `pending_hooks_by_id`;
- `hosts_by_name`.

Store host-applicable material under `hosts_by_name.<host>`:

- `services_by_name`;
- `filesystem.artifacts_by_path`;
- `facts_by_id`;
- `command_history`.

Exact command records are appended to the source host's `command_history`.
When a command pivots to another host, the pivot command remains on the source
host; subsequent commands are recorded under the new active host. DREAMLAND
still keeps the live `SessionState.history` list for admin and exact session
snapshot compatibility, but model context should use relevant host-local
history.

When the runtime does not yet know a hostname, the host key may be an IP
address or another stable host reference. That key can later be supplemented
or replaced through normal world-state updates.

## Consequences

State patches must update host-local keyed registries instead of global
service, artifact, or fact maps. Prompt modules and docs should direct models
to register materialized resources under the host where they apply.

This is a clean pre-release schema break. Legacy local profiles using
`runtime_state` should be regenerated or migrated before use.

## Maintenance

Update this decision if host identity aliases, cross-host artifact references,
or command-history reduction become first-class schema concepts.
