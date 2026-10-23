# 0007 Frame-Local Host Identity

Status: Active
Audience: maintainers, DREAMLAND operators, and coding agents
Purpose: record why shell frames own host identity and why transport-side pops
force command-agent checkpoint refresh.

## Overview

DREAMLAND shell stacks represent nested execution contexts. A nested SSH command
can move the active prompt, environment, and command behavior from one simulated
host to another while preserving the parent frame underneath. Before this
decision, the active `SessionState` had one global `host_identity`, while
`ShellFrame` objects owned username, current directory, interactive program,
and shell environment.

A Langfuse trace from June 17, 2026 showed the mismatch. Entering a nested SSH
session pushed a frame for `swift-gw01`, but Ctrl-D was processed locally by the
SSH session loop. The local stack depth returned to one frame, while the global
host identity and LangGraph checkpoint could still describe the nested host.
The next command therefore behaved as if the session had not fully returned to
the parent host.

Related implementation commit:

- This decision is implemented by the same feature commit that adds
  frame-local `host_identity` and transport-pop checkpoint refresh.

## Decision

Store `HostIdentity` on every `ShellFrame`. The active frame is now the
authoritative source for command context host identity. Replacing or popping the
shell stack restores username, current directory, interactive program, shell
environment, and host identity from the new active frame.

Keep the top-level `SessionState.host_identity` for compatibility and prompt
rendering, but synchronize it from the active frame whenever the active frame is
replaced. Serialized frame payloads include `host_identity`. Older frame
payloads that omit it are backfilled from that frame's `HOSTNAME` environment
variable, falling back to the top-level hostname.

Treat transport-side nested exits, including Ctrl-D and local fallback pops
after shell `exit` or `logout`, as external state updates. These operations do
not pass through the normal command-agent `record_history` path, so they mark
`force_session_state_next_command`. The next command then sends full session
state to LangGraph instead of relying on checkpoint state that may still contain
the popped frame.

Session traces include `type=shell_stack` records with the stable marker
`shell-stack-exit-v1` to verify the fixed path is loaded for the active session.
Operators should see `transport_exit_pop` when Ctrl-D pops a nested frame,
`nested_exit_fallback_applied` when DREAMLAND locally applies an unmodeled nested
shell exit, and `command_context_force_refresh` on the next command before
LangGraph receives full session state.

Do not make the model responsible for restoring hidden parent host identity.
The model may request `push`, `pop`, or `replace_active`, but DREAMLAND owns the
hidden stack and applies those transitions against authoritative runtime state.

## Maintenance

Update this record if host identity moves into a broader future network-node
object, if shell frames are replaced by a generalized protocol context stack, or
if LangGraph checkpoint synchronization changes so external state updates no
longer need an explicit refresh marker.
