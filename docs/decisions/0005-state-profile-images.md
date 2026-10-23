# 0005 State Profile Images

Status: Superseded in part by
`docs/decisions/0026-required-state-profile-world-state.md` and
`docs/decisions/0029-remove-legacy-world-profiles.md`
Audience: maintainers, DREAMLAND operators, and coding agents
Purpose: record the initial design for saved session-derived state profiles.

## Overview

DREAMLAND needs operator-created state objects that can preserve useful
environmental data and later seed another session. The first implementation is
session-scoped because the current runtime owns one mutable `SessionState` per
SSH session. This keeps persistence aligned with the existing state contract
while leaving room for a future unified state object. Later decisions removed
device profiles and YAML world profiles as separate boot layers.

Related implementation commits:

- `195f16d` `Add state profile save and inspection`
- `fe57e2c` `Add state profile live apply`
- `044e048` `Add state profile boot support`
- `e86504c` `Add runtime state profile controls`

## Decision

Use the command family `state-profile` for operator-created save/load state
objects. The name intentionally avoids treating these only as snapshots:
profiles can be exact save points, reusable system entry states, or high-level
world anchors.

Store profiles as JSON documents under local `state_profiles/`. JSON matches
the structured state shape already used by the command agent and spiritguide.
The document has a small required envelope: kind, schema version, name, level,
creation time, optional source session id, optional operator comment, and a
flexible state object. The envelope is validated rigidly enough to prevent
unknown file types or path traversal, while the nested state object remains
flexible for spiritguide-generated and command-generated world details.

Define three deterministic depths:

- `exact`: the fullest session-owned state DREAMLAND currently serializes,
  including shell position, shell stack, active program, shell environment,
  environment metadata, world state, and command history. Exact profiles do not
  yet preserve the live readline editor recall buffer used for up-arrow and
  down-arrow navigation.
- `system`: the same simulated system and generated artifacts, without command
  history or shell-position details. This represents a fresh fictional SSH
  session into the same environment.
- `world`: high-level world anchors, preserving names and descriptions for
  known hosts, services, and broad facts while dropping specific generated
  artifacts.

Keep Langfuse separate from state profiles. Live apply logs an operator state
event and writes a concise session trace record, but saved profiles do not
attempt to preserve or replay Langfuse traces or LangGraph checkpoints. Trace
history should remain a linear record of what happened in the current runtime.

Apply profiles by replacing the selected live `SessionState` fields while
preserving that session's trace id and registered object identity. Preserving
the object identity keeps SSH session ownership and registry references valid.
Preserving the trace id keeps current runtime observability linear even when
the fictional environment state jumps to a saved profile. Applied profiles set
`force_session_state_next_command` so the command agent receives a full state
refresh on the next command.

In the first stage, state profiles were saved runtime state alongside separate
static boot configuration. Later decisions made JSON state profiles the only
boot source of truth.

Booted sessions must preserve runtime identity separately from fictional state.
DREAMLAND gives each new session a fresh trace id and records current SSH
connection metadata, even when the profile was saved from a different session.
`exact` profiles carry saved fictional user and host identity. `system` profiles
carry saved host identity and system state, but initial boot uses the current
SSH login username for the fresh login shell so command-agent context reflects
the authenticated connection. `world`-level state profiles intentionally do
not carry user or host identity, so booted world-level state-profile sessions
use the SSH login username and derive host identity from adopted world state.

Allow operators to change the default profile for future sessions at runtime.
The runtime default is held in a shared state-profile object read by
`AgentBackend.create_session` each time a new SSH session starts. Changing the
runtime default does not mutate existing live sessions. Persisting that runtime
choice is explicit through `--persist`, which writes the `[state_profile]`
setting in the config file DREAMLAND loaded at startup.

Deleting a state profile removes the local JSON file and clears the runtime
default if it pointed at that deleted profile. Existing live sessions are not
changed by profile deletion.

Expose the active state profile in the console `sessions` table as a separate
`profile` column. The existing `env` column comes from Langfuse runtime
environment metadata and should not be overloaded with Dreamland world/profile
state.

Persist the latest booted or applied state profile per session in a small local
JSON index at `state_profiles/session_index.json`. This lets the console show
the last known state profile for inactive sessions without depending on
Langfuse metadata queries. The index is intentionally narrow and should not be
treated as the final local persistence architecture.

## Findings

Manual testing confirmed that exact profiles restore command/result history for
the command agent but not the live readline up-arrow/down-arrow recall buffer.
That editor history is currently held by live terminal editors rather than
`SessionState`, so exact editor recall needs a later terminal-state feature.

The console `sessions` table already had an `env` column, but that value comes
from Langfuse environment metadata. DREAMLAND state-profile identity should be
displayed separately as `profile` so operators can distinguish tracing
environment from simulated state.

The local `session_index.json` is enough to memorialize the last booted or
applied state profile for inactive sessions, but it is not a general local
runtime store. Future local persistence may need to cover automatic state
snapshots, command streams, reasoning outputs, and replay or audit data that
currently lives only in Langfuse or in memory.

## Maintenance

Update this record when state profiles gain LLM-assisted summarization, a more
robust local storage layer, or a schema change. When the format changes,
increment the schema version and update `src/dreamland/state_profiles.py`,
`docs/OPERATIONS.md`, and tests together.
