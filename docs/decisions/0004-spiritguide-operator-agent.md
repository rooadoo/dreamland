# 0004 Spiritguide Operator Agent

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators, and coding agents
Purpose: record the design decisions made while adding the spiritguide operator
agent.

## Overview

DREAMLAND's product model makes the operator responsible for guiding the world
while the command emulator maintains the user's terminal experience. The
spiritguide feature creates a dedicated operator-facing agent for shaping the
selected live session's state without treating admin instructions as terminal
commands.

Related implementation commits:

- `3f7a9c8` `Add live session registry`
- `d450756` `Add spiritguide console command`
- `8779793` `Add model-backed spiritguide`
- This phase: add spiritguide Langfuse tracing

## Decision

Add a live session registry so operator controls can target active
`SessionState` objects directly. The registry is separate from Langfuse
introspection because traces are historical observability data, not live state.
Registered sessions carry a per-session lock so command execution and
operator-side state changes can coordinate.

Mark sessions after operator-side state updates so the next command forces a
full command-agent context refresh. LangGraph checkpoint reuse remains useful
for ordinary command flow, but external spiritguide mutations must be copied
back into checkpointed state before subsequent emulation relies on it.

Make spiritguide session-scoped. SSH `!dreamland spiritguide` commands target
the calling SSH session by default. The native console may target the active
session or a live session passed with `--session`, but spiritguide does not
inspect or mutate unrelated sessions.

Make spiritguide an operator state-guidance agent, not a command emulator. It
receives admin instructions and returns a concise operator message plus a
`CommandStateUpdate`. DREAMLAND applies that update to the selected live session
unless `--dry-run` is set.

Allow full state authority through the existing `CommandStateUpdate` shape.
Spiritguide can modify `world_state`, current directory, shell environment,
interactive program, and shell stack. Prompt policy should still prefer
`world_state` for theme, scenario, host, service, artifact, inventory, log,
user, network, and story facts, and reserve intricate shell-state mutation for
explicit operator intent or necessary coherence.

Use a separate `[spiritguide]` configuration section with the same model-control
fields as `[agent]`. Operators are expected to experiment with higher-capability
models for spiritguide while using cheaper or faster models for command
emulation.

Prompt spiritguide with project awareness: DREAMLAND is an LLM-emulated
computing network; spiritguide serves the Dreamland operator; it achieves goals
by modifying state for the emulator; and immersion, deep world structure,
creative scenario coherence, and operator direction are primary.

Keep core DREAMLAND role and component definitions in a reusable prompt module
(`dreamland_roles.md`) instead of baking them only into the spiritguide prompt.
These definitions identify DREAMLAND, the trusted operator/admin, the untrusted
terminal user, the console, command emulator, spiritguide, session, world state,
profiles, artifacts, established facts, command history, state updates, and
Langfuse traces. Spiritguide applies those shared definitions to its own role.

Prompt spiritguide to favor creative direction over exhaustive materialization.
For broad requests to populate users, files, services, or networks, it should
create directional categories and a few strong anchors, then trust the command
emulator to fill ordinary command-level gaps. Concrete artifacts remain valid
when explicitly requested, when they provide important future-command anchors,
or when exact content must remain stable, but large generated inventories and
full file contents should not be the default.

Trace model-backed spiritguide calls in Langfuse as operator activity under the
same Langfuse session, but not as terminal commands. The root observation is
named `dreamland-spiritguide-agent`, and the model call is a child observation.
This keeps spiritguide visible alongside the command-agent flow while preserving
the meaning of command lists as user terminal activity.

Spiritguide traces record the operator instruction, formatted spiritguide
context, raw model output, parsed state update, parse metadata, patch keys, and
dry-run status. Apply status is intentionally left for the console/apply layer
because the model agent returns a proposed `CommandStateUpdate` before the
console decides whether to mutate the live session.

Use a larger default output-token cap for spiritguide than for command
emulation. Live testing showed a broad state-seeding instruction could be cut
off mid-JSON at the command-agent-oriented default, producing no parseable
`CommandStateUpdate`. Operators may still override or remove the cap, but the
default should fit ordinary spiritguide patches.

If a model-backed spiritguide response is not accepted as structured state,
DREAMLAND must not apply the returned state update and must tell the operator
that no state changes were applied. This makes truncation and malformed JSON
fail visibly instead of looking like a successful state change.

When OpenAI Responses-style reasoning content is present, parse only text
content blocks as the spiritguide response. Reasoning blocks are useful for
observability but are not part of the strict JSON state contract, and converting
them to strings can corrupt an otherwise valid JSON payload before parsing.

## Live Testing Follow-Up

This tracing phase resolves direct visibility of spiritguide model calls in
Langfuse. The first lightweight console inspection step is to print a concise
result summary after each `spiritguide` command: parsed state-update sections,
apply status, dry-run status, and structured-response status. This keeps routine
operator visibility simple without coupling the console apply path back into
Langfuse trace mutation.

Dry-run inspection should show the parsed patch body by default. A dry run that
only reports the existence of a patch is not operationally useful enough: the
operator needs to see what would change before deciding whether to apply it.
Non-dry-run calls remain concise by default but can print the parsed patch with
`--show-patch`.

Session trace logs should record a concise spiritguide summary so operators can
use the existing `trace` command for live inspection without depending on
Langfuse. The trace record intentionally stays compact: instruction preview,
dry-run status, structured-response status, patch sections, and apply result.

Keep full spiritguide instructions out of propagated Langfuse metadata. Langfuse
propagated metadata drops values over 200 characters, creating noisy warnings
for ordinary operator instructions. Store the full instruction in observation
input, and keep metadata to bounded preview, length, and hash values.

Console commands for historical spiritguide observation queries remain future
work. They should be added only if manual use shows enough demand to justify
more Langfuse introspection code.

The clean rollback anchor before adding world-state directive semantics is
`238d06b` (`Trace spiritguide console actions`). If the directive approach
causes bad state behavior, revert the directive checkpoint back to that commit
and re-plan supersession semantics.

The first supersession checkpoint is runtime-only: add reserved world-state
directives for keyed delete and subtree replacement, then make the
command-agent graph and live session application use the same merge helper.
Prompting remains unchanged until the runtime primitive is reviewed.

The second supersession checkpoint teaches prompts and durable state
documentation to use those directives. Spiritguide should actively supersede
incompatible facts when the operator changes scenario identity, using replace,
or delete instead of only adding new facts beside stale ones.

The directive prompt examples must match runtime semantics. Replacement
directives carry the replacement object directly:
`{"__dreamland_replace__": {...}}`. Do not wrap replacements in a `"new"`
object unless that wrapper is intentionally part of the resulting state.

The third validation checkpoint covers shell-level state changes from
spiritguide. Automated tests now verify that externally applied updates for
`cwd`, `interactive_program`, `shell_environment`, and `shell_stack_transition`
synchronize the live session and reach the next command context through both
the native console and `!dreamland` SSH path. This exposed and fixed a bug where
shell-stack transitions were ignored for sessions without an initialized
`shell_stack`; external transitions now build from the active session frame.

Host identity should be first-class mutable session state, not inferred from
`world_state`. `host_identity.hostname` is the minimum supported shape. It feeds
the command context and command-line prompt, while `world_state` remains the
broader narrative/topology memory that can describe all known hosts.

Live testing of `spiritguide Rename the active host to jump01.` showed that
allowing Spiritguide to mirror a hostname change across `host_identity`,
`shell_environment`, `shell_stack`, and `world_state` encouraged oversized,
malformed JSON responses. Hostname-only changes should use a minimal
`host_identity` patch. DREAMLAND owns synchronizing `$HOSTNAME` from that
runtime identity.

One important limit remains:

- Prior terminal history can contain old, now-superseded command outputs. The
  command emulator may treat that history as stronger evidence than a newer
  operator correction and reintroduce stale facts into `world_state`.

Future implementation phases should treat these as product requirements before
additional prompt tuning:

- make spiritguide actions inspectable from the operator console;
- teach the command emulator that newer operator/spiritguide corrections outrank
  older command history for identity facts;
- preserve historical command records rather than rewriting past terminal
  output.

## Consequences

The console can now mutate live session state without routing through a terminal
command. Tests can inject deterministic spiritguides while production uses a
model-backed spiritguide.

Local console commands that target historical Langfuse sessions may fail for
spiritguide unless the selected session is still live. This is intentional:
spiritguide modifies runtime state, not archived traces.

The model-backed spiritguide uses a strict JSON response shape with `message`
and `state_update`. Invalid JSON is treated as an operator message with no state
change, and the console explicitly reports that no state changes were applied,
so malformed or truncated responses do not corrupt session state.

Future work may add richer review/apply workflows, operator-editable proposed
patches, persisted world-state snapshots, and stronger state validation before
applying intricate shell-stack or environment changes.

Direct spiritguide observability and state supersession are no longer optional
polish. They are required to debug and control operator-driven identity changes
without stale command history pulling the emulation back to old facts.

As of the initial Spiritguide completion, the feature includes native console
and `!dreamland` entry points, model-backed session-scoped state guidance,
Langfuse tracing, dry-run/show-patch operator feedback, world-state
supersession directives, baseline shell-state validation, and mutable
`host_identity` for prompt-relevant active host identity. Remaining items are
future feature phases rather than blockers for the initial Spiritguide.

## Maintenance

Update this decision if spiritguide becomes cross-session, gains durable state
outside selected live sessions, stops using `CommandStateUpdate`, or changes
from operator state guidance into command emulation. Also update it when
spiritguide tracing, world-state deletion, supersession, or stale-history
handling are implemented.
