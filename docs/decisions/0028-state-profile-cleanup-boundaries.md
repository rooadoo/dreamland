# 0028 State Profile Cleanup Boundaries

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators, and coding agents
Purpose: record the cleanup of deprecated world-state scaffolding and profile
creation boundaries after host-local world state landed.

## Overview

After unifying world and runtime state, model context still contained older
world-profile scaffolding. `world_state.scope.topology` and
`world_state.scope.expansion_policy` duplicated newer generation fields and
made multi-host sessions look like singleton worlds. Detached Spiritguide
profile creation also started from the packaged neutral `default` state, which
caused the `ubuntu` seed host to remain in newly built scenario profiles unless
the model explicitly removed it.

Command history also exposed another cleanup issue: some saved host-local
history entries contained a full structured model-result JSON envelope in
`stdout` instead of the terminal stdout string inside that envelope.

## Decision

Remove `scope` from packaged state/world profiles, world-level state
projection, docs, and tests. World-level direction should live in `purpose`,
`simulation`, `generation_policy`, `expansion_directives_by_id`, and
`world_facts_by_id`.

Create detached Spiritguide profiles from a blank world-state scaffold, not
from the packaged neutral `default` state profile. The packaged `default`
profile remains the boot fallback so DREAMLAND can start without local
operator state, but it is not the seed for building unrelated scenario worlds.

Keep exact command history under each host and remove Spiritguide's separate
session-history context block. When command history is saved or loaded,
collapse accidental structured result envelopes so
`hosts_by_name.<host>.command_history[].stdout` contains the command's actual
stdout.

## Consequences

This is a clean pre-release schema break. Local profiles containing
`world_state.scope` should be regenerated or manually cleaned. New scenario
profiles created through detached Spiritguide should no longer inherit an
unrelated `ubuntu` host unless the operator or model deliberately adds one.

The command emulator may still emit JSON as legitimate terminal output. The
history cleanup only collapses objects that match DREAMLAND's structured result
envelope shape: `stdout`, `stderr`, `exit_status`, and `state_update`.

## Maintenance

Update this decision if a future topology, network-scope, or scenario-boundary
schema is introduced as a first-class replacement for the removed `scope`
field, or if command-history reduction becomes a separate persisted artifact.
