# 0003 World Profile Framework

Status: Superseded in part by
`docs/decisions/0026-required-state-profile-world-state.md` and
`docs/decisions/0029-remove-legacy-world-profiles.md`
Audience: maintainers, prompt authors, DREAMLAND operators, and coding agents
Purpose: record the design decisions made while adding mandatory world profiles
and mutable world state.

## Overview

DREAMLAND began as a single emulated Ubuntu host. The product direction is a
deeper LLM-emulated computing network where hosts, files, services, logs,
relationships, and generated artifacts can accumulate over time.

This feature adds the first world layer around the existing device profile so
the runtime can stay neutral by default while still giving operators and future
injector agents a stable place to expand the simulation.

Related implementation commits:

- `5a725a0` `Add mandatory world profile framework`
- `2916a36` `Use keyed mutable world state`
- `455f15e` `Record world profile framework decisions`

## Decision

Make a world profile mandatory for every runtime session. A singleton machine is
represented as a world with one primary host, not as the absence of a world.

Keep the packaged default world profile basic and neutral. It should behave
similarly to the previous single-host default, while still providing a
`world_state` root that prompts, runtime code, tests, docs, and future features
can rely on.

Copy the world profile into per-session mutable world state at session start.
The packaged profile remains static, but runtime state can change through
`state_update.world_state` patches returned by the command agent.

Use keyed objects for mutable world-state collections instead of arrays. The
canonical default runtime collections are:

- `generated_artifacts_by_path`
- `established_facts_by_id`
- `services_by_name`
- `hosts_by_name`
- `pending_hooks_by_id`

Keyed objects let the model and future injector agents add or update individual
entries without replacing unrelated entries. Legacy array-shaped runtime
collections are not preserved as a supported profile shape.

Prompt DREAMLAND to be more permissive and immersive when a command is valid and
fits the environment. The model should prefer plausible affirmative emulation
over reporting that ordinary utilities, files, users, or services are missing.
It should still return realistic Ubuntu-like errors for malformed commands,
permission failures, impossible requests, or facts contradicted by established
state.

Prompt DREAMLAND to make decisive, sticky world-building choices. When several
plausible details fit, the model should choose one, record it in keyed
`world_state`, and treat it as established for later commands. The world should
build up and out; minor uncertainty should usually result in plausible added
detail rather than a flat absence of environment.

Bias the default device and world profiles toward physical systems. Virtual
machines, containers, cloud hosts, and hypervisors remain valid when configured
or already established, but the default should favor physical workstations,
servers, appliances, peripherals, and networks because those are harder to stage
with ordinary VM tooling.

Keep scenario facts in device profiles, world profiles, future network
configuration, or mutable world state. Reusable prompt modules should carry
behavior and state-contract instructions, not hard-coded scenario inventories.

Keep prompt changes concise. This feature should add enough instruction to make
the model more immersive, permissive, decisive, and stateful without expanding
the system prompt with large example sets. Add examples only when tests or live
behavior show that compact rules are not enough.

## Consequences

Runtime code and prompt authors can assume `world_state` is present. Future
features do not need an optional no-world path. The separate `world_profile`
runtime concept was later removed.

Deep-merging world-state patches now works best with keyed object collections.
Replacing arrays is no longer a central failure mode for generated artifacts,
services, and established facts.

The model may occasionally add plausible detail that is not perfectly consistent
with an earlier command. This is acceptable when it improves immersion and does
not violate a durable established fact, access boundary, or explicit profile
constraint.

Static profiles seed runtime state, but runtime state is allowed to diverge as
the session evolves. Persisting, inspecting, and reloading that runtime state is
deferred work.

Known follow-ups recorded in `ROADMAP.md` include provider streaming failure
handling, `logging -f` console rendering, multi-target backend inspection,
filesystem-aware local `cd`, persisted world-state snapshots, and richer
multi-host world profiles.

## Maintenance

Update this decision if world profiles become optional, the runtime state shape
changes, legacy array-shaped collections become supported, prompt policy changes
away from decisive additive world-building, or another scenario/network layer
supersedes world profiles.
