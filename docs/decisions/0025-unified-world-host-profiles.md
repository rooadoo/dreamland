# 0025 Unified World Host Profiles

Status: Superseded in part by
`docs/decisions/0026-required-state-profile-world-state.md`
Audience: maintainers, prompt authors, DREAMLAND operators, and coding agents
Purpose: record the clean break from separate device profiles to host-aware
world profiles.

## Overview

DREAMLAND originally booted from a device profile plus a world profile. That
split made the starting host more rigid than the world around it and required
runtime, prompts, and saved state to carry two overlapping profile concepts.

The project is still pre-release, so this change intentionally removes
backward compatibility for `[device]` configuration and packaged device YAMLs.

## Decision

Use the world profile as the only boot profile. A world profile defines:

- static `entry_host`;
- mutable initial `active_host`;
- top-level `hosts_by_name` keyed by hostname, IP address, or another stable
  host reference;
- broader world purpose, simulation policy, and runtime generation
  policy.

Treat the starting system as an ordinary host in `hosts_by_name`. Its metadata
is mutable in runtime `world_state`, like any other discovered host. The
configured `entry_host` remains the static default entry point, while
`world_state.active_host` tracks the active simulated system as shell frames
and SSH pivots move through the world.

Keep host metadata permissive. Sparse metadata is guidance, not proof that the
host lacks ordinary system capabilities. World creators decide how detailed the
starting host should be.

Move host registries to top-level `world_state.hosts_by_name`. Runtime
collections for services, artifacts, facts, directives, and hooks remain under
`runtime_state`.

## Consequences

Configuration no longer supports `[device]` or `DREAMLAND_DEVICE_PROFILE`.
Packaged device profiles and device-profile prompt blocks are removed.

Command context included `world_profile`, `world_state`, `active_host`, and
`active_host_metadata` in this step. Decision 0026 supersedes that context
shape: command context now exposes `world_state`, `active_host`, and derived
`active_host_metadata` without a separate model-facing `world_profile`. Saved
state profiles preserve the world host registry and active host reference as
part of saved `world_state`.

Runtime code synchronizes `world_state.active_host` from the active shell
frame's host identity and creates a minimal host registry entry when a new host
reference is first encountered.

## Maintenance

Update this decision if host registries move away from `hosts_by_name`, if
`entry_host` becomes mutable, or if a new network-profile layer supersedes
world-host metadata.
