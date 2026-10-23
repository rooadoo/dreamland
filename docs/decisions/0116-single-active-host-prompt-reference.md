# 0116 Single Active Host Prompt Reference

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators
Purpose: remove a redundant active host selector from command-emulator prompts.

## Overview

The unified world/host profile change (`799ac7e`) introduced a standalone
`active host:` line alongside world state and an active host metadata block.
The metadata block was later removed, but the selector continued to repeat
`world_state.active_host` on every model call.

## Details

Render the active host registry key only inside the prompt's world-state JSON.
Prompt instructions explicitly use `world_state.active_host` to select the
current host's full record and host-local command history.

Keep derived `active_host` fields in runtime command context and graph state.
This change removes prompt duplication without changing host synchronization,
internal consumers, state-profile persistence, or tracing metadata.
`world_state.entry_host` remains the fixed starting host and serves a separate
purpose from the mutable active host reference.

The `deployed` tag marks `7d2366c`, the baseline before this prompt change.
Use `git log deployed..HEAD` and `git diff deployed..HEAD` to inspect subsequent
commits and tracked changes relative to that baseline.

The existing command-agent regression checks the JSON selector before a host
pivot, on the destination host, and after exiting to the starting host. It also
checks that the standalone line is absent and the selected host retains its
full metadata. These provider-free checks validate rendering and transitions;
they do not measure live model adherence to the updated prompt.

Validation passed: 129 tests across `test_backend`, `test_prompts`,
`test_world_state`, `test_agent_state`, and `test_ssh_session_state`, plus Ruff
lint, formatting, and `git diff --check`.

For a manual trace check, restart the command agent to load the revised prompt,
run a command on the starting host, pivot to another host, then exit and run
another command. Inspect each Langfuse model input: `world_state.active_host`
should follow the current host, `entry_host` should stay fixed, and there should
be no standalone `active host:` line. No live model check was run for this fix.

## Maintenance

Keep prompt rendering and state-contract instructions aligned. Revisit the
internal derived fields separately if command-context or graph-state consumers
are refactored. Advance the deployment marker only for an actual deployment.
