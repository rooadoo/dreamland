# 0031 Active-Host Emulator Context Projection

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators, and coding agents
Purpose: define the model-facing host projection used by the command emulator
without reducing authoritative DREAMLAND state.

## Overview

Host-local state is intentionally durable and can grow as users explore a
network. Sending every discovered host's filesystem, users, shell state, and
exact command history to the command emulator on every turn creates two
problems: unrelated systems bias later emulation toward repeated layouts, and
input-token usage grows with details that are irrelevant to the active host.

The authoritative state is still valuable to DREAMLAND and Spiritguide. This
decision changes only the view formatted for command-emulator model calls.

## Decision

DREAMLAND creates a non-mutating active-host projection while formatting each
command-emulator prompt. It does not project `SessionState`, `CommandContext`,
LangGraph checkpoint state, saved state profiles, state-update application, or
Spiritguide context.

The projection retains every top-level world-state field and the complete
`hosts_by_name` registry keyset. In the first version:

- the active host retains its complete metadata, including its local command
  history, filesystem, users, groups, services, shell data, facts, and other
  established fields;
- every inactive host remains present as a compact card containing applicable
  identity, purpose, operating-system, recorded IP/network, compact fact, and
  service information;
- inactive local command history, filesystem, users, groups, shell,
  environment, interactive-program state, and unrelated internal detail are
  omitted from the model-facing card;
- the separate `active host metadata` prompt block is removed because the same
  complete record already exists under `hosts_by_name`;
- the `active host` prompt anchor remains explicit.

Recorded IPv4 and IPv6 references are preserved when they are structured in a
host record, including nested interface, management, service, fact, and other
network paths. Details that appear only in rejected model output or raw command
history have not become structured host facts and cannot be preserved as
compact host-card fields.

The projection API accepts additional detailed host references. Version one
selects the active context host only, but hostname, alias, and host-owned IP
resolution plus the multi-reference API leave room for later command-aware
selection without changing the state schema or replacing `hosts_by_name`.

## State And Prompt Ownership

The backend remains authoritative and unabridged. Model `world_state` patches
continue to merge into full session state, and host-local history continues to
be recorded on the source host. A projection is never written back into runtime
or saved state.

Spiritguide has a different role and continues to receive the full shell stack,
environment, and world state. Prompt instructions describe the complete active
`hosts_by_name` entry as authoritative and warn that missing local detail from
an inactive compact card is not evidence of absence.

The static system prompt remains before dynamic command context. The human
message keeps a stable order for session/interface fields, environment
metadata, the active-host anchor, projected world state, and the current
command. OpenAI GPT-5.6 explicit caching reuses the existing system/user
boundary without reordering prompt facts; that separate decision is recorded in
`0032-openai-gpt-5-6-explicit-command-prompt-caching.md`.

## Observability And Validation

Command traces record incoming-context projection mode, active and detailed
host references, total/detailed/compact host counts, and
full/projected/saved world-block character counts. The standard `tokens` log
records the same numeric counts without state contents or host references.
Trace count values use decimal strings because Langfuse's propagated-metadata
channel accepts strings; projection calculations and token-log arguments remain
integers.
These deterministic serialized character measurements are prepared from the
authoritative incoming `CommandContext`; they are not estimated token savings
or a claim that checkpoint state cannot drift. Removing the redundant
active-host block provides additional savings outside `saved_world_chars`.

Owning tests cover pure compaction and copying, structured IP retention,
multi-host resolution, backend-to-model pivots, unabridged backend state, and
unabridged Spiritguide context.

The pure projection helper was introduced in commit `d77b0d2`.

## Consequences

The emulator receives locally relevant detail while retaining enough peer
identity, addressing, and service context for wider-network coherence. Runtime
memory and saved profiles do not shrink; the benefit is lower model input and
less cross-host incidental reuse.

Commands that genuinely require another host's private state may need explicit
host promotion later. Active-host history pruning, summarization, structured
response repair, and shell/login-state normalization remain separate work.

No configuration option, compatibility adapter, or world-state schema change
is introduced during this pre-release iteration.

## Maintenance

Update this decision when detailed-host selection becomes command-aware,
compact-card fields change, projection moves to another runtime boundary,
Spiritguide context changes, or prompt-cache ordering is materially revised.
