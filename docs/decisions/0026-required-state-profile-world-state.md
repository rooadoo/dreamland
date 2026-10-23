# 0026 Required State Profile World State

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators, and coding agents
Purpose: record the move from separate model-facing world profile and world
state context to required state-profile boot with a single mutable world state.

## Overview

After unifying device and world profiles, command context still carried both a
static `world_profile` block and mutable `world_state`. That duplicated the
same scenario facts, made context harder to review, and allowed stale static
profile language to appear beside the current runtime state.

The project is still pre-release, so this change intentionally makes a clean
break.

## Decision

Require a state profile for session boot. DREAMLAND ships a packaged
`default` state profile under `src/dreamland/default_state_profiles/` so the
runtime can start without local operator state.

Make `world_state` the only model-facing world record. It contains:

- static `entry_host`;
- mutable `active_host`;
- top-level `hosts_by_name`;
- broader world purpose, simulation policy, generation policy,
  expansion directives, world facts, and hooks.

Do not send a separate `world_profile` block to the command agent. Runtime,
SSH presentation, and tooling derive host metadata from `world_state` inside
the selected state profile.

When booting `system` or `world` state profiles for an authenticated login,
expand lightweight templates such as `{username}` so packaged defaults can stay
generic while runtime state is concrete.

If a state profile does not contain an explicit active interactive program,
derive the starting program from the active host metadata in
`world_state.hosts_by_name`.

## Consequences

`[state_profile].profile` defaults to `default` and blank settings resolve to
the packaged default. The command context contains `world_state`,
`active_host`, and derived `active_host_metadata`, but no `world_profile`.

State profiles now own the neutral boot scenario language. The packaged
default is intentionally sparse; richer worlds should be saved as local state
profiles or generated through Spiritguide profile workflows.

## Maintenance

Update this decision if state profile schema versions change or if a future
network/topology profile layer becomes the authoritative boot source.
