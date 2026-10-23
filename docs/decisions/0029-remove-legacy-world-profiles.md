# 0029 Remove Legacy World Profiles

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators, and coding agents
Purpose: record the removal of YAML world profiles as a runtime and packaged
profile layer.

## Overview

DREAMLAND previously carried YAML `world_profiles` after state profiles became
the required boot source. That left two editable representations for the same
neutral world seed: packaged state-profile JSON and packaged world-profile
YAML. The duplication made ownership unclear and let SSH presentation defaults
depend on a legacy profile path that was no longer model-facing.

## Decision

Remove packaged YAML world profiles and the public `WorldProfile` loader API.
The selected JSON state profile is the single boot source, and its
`state.world_state` supplies entry-host metadata, active-host metadata, shell
defaults, SSH destination metadata, and model context.

Rename world-profile helper code to world-state helper code. Runtime helpers
for active-host lookup, host metadata, prompt rendering, and active-host
synchronization live under `src/dreamland/world_state.py`.

Render model-facing world state as formatted JSON in prompts. State profiles
are JSON documents, command-agent state updates are JSON objects, and using the
same representation for prompt context removes the final YAML dependency from
the boot/profile path.

Keep Spiritguide detached profile creation independent from the packaged
neutral `default` state profile by seeding it with a blank world-state scaffold
inside the profile-creation workflow.

## Consequences

This is a clean pre-release break. Code importing `dreamland.world_profile`,
`WorldProfile`, or `load_world_profile` must migrate to state-profile loading
or `dreamland.world_state` helpers.

State profiles are now the only packaged boot profile format. Future
operator-authored scenario import/export can still choose a friendlier authoring
format, but that should be a deliberate conversion tool rather than a second
runtime source of truth.

## Maintenance

Update this decision if a non-JSON authoring layer is reintroduced, if
state-profile JSON stops being the authoritative boot format, or if world-state
prompt rendering changes representation again.
