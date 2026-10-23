# Agent State Contract

Status: Active
Audience: maintainers and coding agents
Purpose: explain the command-agent boundary and state semantics.

## Overview

Commands do not execute on the host operating system. DREAMLAND converts user
input into a `CommandContext`, sends that context to built-ins or an agent, and
then applies the returned `CommandResult` to session state.

User commands may arrive through SSH or through MCP-created headless sessions.
Both paths use the same backend command execution and session-state contract.
MCP clients must not use shell commands beginning with `!dreamland`; operator
console actions are exposed through native admin/MCP tools instead of the
simulated shell.

The state contract lets LLM output remain creative while DREAMLAND owns the
parts that must stay coherent: terminal streams, current directory, shell
environment, active interactive program, shell stack, connection metadata, and
history. DREAMLAND also owns mutable world state so generated environmental
facts can be carried across later commands in the same session.

Operator-side spiritguides use the same `CommandStateUpdate` structure when
they alter state, but they are not command emulators and do not produce a
`CommandResult`. A spiritguide can receive admin instructions for one live
session or for a detached state-profile workflow. Live-session guidance is
applied under that session's live-session lock. Detached profile guidance runs
against a synthetic session built from a blank world or saved state profile and
saves the resulting state profile without requiring a live SSH session.

External state updates mark the session so the next command sends full session
state to the command agent even if a LangGraph checkpoint already exists. This
keeps checkpointed agent state aligned with operator-side changes.

## Command Result Envelope

Model-backed responses use a JSON object with this top-level shape:

```json
{
  "stdout": "",
  "stderr": "",
  "exit_status": 0,
  "state_update": {}
}
```

`stdout` and `stderr` contain only terminal output. `state_update` contains only
state changes.

Foreground OpenAI calls also enable native JSON mode (`json_object`) through
the API. The SDK sends `response_format` to Chat Completions and `text.format`
to Responses while retaining streaming and the existing prompt-cache settings.
This constrains JSON syntax without imposing a strict schema on dynamically
keyed world state. The existing parser still validates the envelope and state
updates, and incomplete generation can still fail. This setting belongs to the
foreground command agent; Spiritguide, enrichment, and other providers retain
their existing output behavior. See
[the decision record](decisions/0099-foreground-openai-json-mode.md).

When a model response looks like a structured envelope but fails parsing or
validation, DREAMLAND records parser metadata on the command result. The current
runtime also writes a structured failure capsule before command state updates
are applied. Capsules preserve the raw model response and exact pre-command
state for diagnosis and parser replay; they do not currently imply automatic
retry or terminal-output salvage policy. See `docs/STRUCTURED_FAILURES.md`.

Valid state update fields are:

- `host_identity`
- `cwd`
- `interactive_program`
- `shell_environment`
- `shell_stack_transition`
- `shell_stack`
- `world_state`
- `effects`, as an alternative envelope containing only an ordered effect list;
  see [Whole-Line State Effects](#whole-line-state-effects).

Prefer `shell_stack_transition` for stack lifecycle changes. Use `shell_stack`
only for compatibility or repair cases.

`host_identity` carries explicit mutable host identity for the active frame.
Its minimum field is `hostname`. DREAMLAND projects the active frame hostname
into command context and prompt rendering. World state may also describe hosts
at a narrative or topology level, but prompt and command mechanics should use
`host_identity`. When only the hostname changes, agents should emit
`host_identity` without duplicating that change into `shell_environment` or
`shell_stack`; DREAMLAND synchronizes `HOSTNAME` from `host_identity`.

The command prompt carries the active host registry key once, as
`world_state.active_host`, and includes the selected `hosts_by_name` entry with
the saved initial `login_banner` omitted.
It does not render a separate `active host:` line. Runtime command context and
graph state retain derived `active_host` fields for internal consumers. Shell
transitions synchronize these references from the active frame's host identity;
`world_state.entry_host` remains the fixed starting host. See
[the decision record](decisions/0116-single-active-host-prompt-reference.md).

`world_state` is a minimal object patch for durable environment facts,
generation directives, or materialized artifacts created or established by the
current command. DREAMLAND deep-merges that patch into the session's active
world state. Mutable runtime collections are keyed objects, not arrays, so
updates can add or replace individual entries without losing other generated
facts. Use stable keys such as directive IDs, absolute paths, service names,
hostnames, or concise fact identifiers.

Failure observations retain the operation and observation point they describe.
An error alone does not establish a persistent cause or disable unrelated
operations. Repetition is not independent evidence for a broader cause; later
commands must respect their own requirements and intervening observations.
Explicit scenario faults and supported consequences of simulated activity
remain authoritative. When an action actually resolves a condition, the agent
updates or removes obsolete active facts with the existing state directives
while retaining historical evidence. A retry, shell exit, or attempted recovery
does not by itself establish resolution. These are prompt-level continuity
rules, not automatic state repair; see
[decision 0122](decisions/0122-failure-scope-and-continuity.md).

World state distinguishes high-level generation guidance from materialized
registries. Top-level `generation_policy` records how freely the emulator
should materialize new details. Top-level `expansion_directives_by_id` stores
reusable just-in-time generation guidance such as organization scale,
infrastructure families, network zones, technology/vendor families, naming
patterns, site composition, user population shape, and materialization rules.
Top-level `hosts_by_name`, top-level `world_facts_by_id`, and host-local
collections such as `services_by_name`, `filesystem.artifacts_by_path`,
`users_by_username`, `groups_by_name`, `facts_by_id`, and `command_history` are
materialized registries for facts the emulator has already committed to the
user's world.

Supported liberty levels are:

- `strict`: massively reduce new invention; prefer already materialized state.
- `conservative`: fill nearby details when strongly implied.
- `moderate`: default; build a realistic immersive world without flooding state.
- `expansive`: create broader surrounding infrastructure when commands expose it.
- `abundant`: support a very large richly populated world while preserving
  coherence.

Command agents should materialize details just in time. Register major hosts or
network systems when command output commits to them, services when their
endpoint or behavior matters later, and files when the user reads, writes,
executes, edits, transfers, or otherwise relies on them. Do not register every
incidental listing row, broad scan result, generated table item, or example.
World-level state profiles preserve registered generated artifacts under the
applicable host's `filesystem.artifacts_by_path` so detached world-building can
carry materialized files and similar artifacts into future sessions.

The emulator's `world.md` prompt treats the initial scenario as the starting
region of a connected world, including when Spiritguide supplied no expansion
directives. Commands investigating network boundaries should reveal concrete
external destinations and onward relationships with an eventual usable path
to the simulated Internet or a similarly vast network. Explicit isolation,
generation restrictions, routing, and access constraints remain authoritative.
Materialization remains bounded by the command and generation liberty.

Significant hosts and networks retain an owner, operational purpose, and
relationship to neighboring systems. Cross-host allocation, namespace,
connection, and alias/translation commitments use categorized `network-` world
facts so they remain in the model's projection and in saved world profiles.
New addresses and invented domains follow the shared
[fictional network addressing policy](STATE_PROFILES.md#fictional-network-addressing).
Existing mappings are preserved. SSH transitions still take precedence over
optional inventory in a response. These are prompt instructions rather than
runtime topology or address validation; see
[decision 0090](decisions/0090-emulator-connected-network-continuity.md).

Significant physical sites also receive geographic world facts under
`network-site-*` keys with category `network`. Host `site_id` values and network
fact `site_ids` reference those records, keeping country, region, locality,
timezone, ownership, and role available across host changes. Colocated systems
reuse a site; distinct facilities extend the geography just in time. The shared
[geographic guidance](STATE_PROFILES.md#geographic-grounding) preserves explicit
settings, distinguishes hosting from headquarters and service territory, and
keeps civil timezone separate from a host's configured clock. It changes prompt
direction without changing the runtime state schema.

Just-in-time generation includes adaptive ambient realism. When a command
observes a population, inventory, history, namespace, or operational surface,
the command agent should materialize a plausible surrounding ensemble rather
than only objects named by the command or profile. Its breadth and character
come from the system's purpose, lifecycle, age, capabilities, activity,
persistence, maintenance culture, shared use, world directives, and generation
liberty. This is an open-ended inference and is not limited to a catalog of
system types. Environments that are new, minimal, stateless, immutable, tightly
controlled, or rarely used may expose little or no residue.

Incidental details should form causal clusters. Related identities, ownership,
timestamps, files, configuration, jobs, logs, and service state should support
a plausible history instead of appearing as unrelated random noise. Most
incidental details should be mundane and non-actionable. Under `strict`
liberty, the agent must not invent accidental credential exposure, security
mistakes, or unsafe leftovers; higher liberty levels allow these details with
increasing but still occasional frequency when they fit the world.

Identity registration is selective. Record a root identity when the active
environment has one and it becomes observable. Record materialized humans,
special entities, and service or automation identities whose ownership,
access, activity, or behavior matters. Routine built-in identities can remain
implicit when ordinary platform conventions can reproduce them. Record groups
when membership or permissions matter. Incidental rows can remain supported by
host-local command history, but facts needed for later identity, ownership,
permission, content, service, or lifecycle consistency require compact keyed
world-state records.

Reserved directive objects can be used as keyed `world_state` values when facts
need to stop being active:

- `{"__dreamland_delete__": true}` removes that key from its parent object.
- `{"__dreamland_replace__": {...}}` replaces that whole keyed subtree.

Use delete for obsolete active facts, replace when old nested details are
incompatible with a new identity.

## Shell Stack

The active shell or program is represented by a `ShellFrame`. A frame owns its
username, current directory, interactive program, shell environment, and host
identity. Nested shells, login contexts, SSH sessions, and supported
line-oriented REPLs or programs should push frames. Exiting them should pop frames
and restore all parent-frame fields, including host identity. Updating the
active frame without changing depth should replace the active frame.

The emulator's [terminal IO prompt](../src/dreamland/prompts/terminal_io.md)
works around the command/response interface's lack of screen redraw and
indefinite foreground streaming. Continuous commands finish a short simulated
run in the calling frame: unbounded `ping` defaults to about ten probes plus
matching statistics, while explicit finite stopping conditions are respected.
Screen-oriented programs such as `vim`, `less`, `more`, and `top` instead finish
with plausible in-world output or a startup failure. A short file may be shown
completely without paging; known content, package availability, and permissions
must remain consistent. Finite noninteractive modes still work when supported.
Output size and interface limitations do not establish host resource
exhaustion. Any cover must stay local to that invocation or interface and must
not create an ongoing host failure. Background launches do not require the
foreground response to contain every child's complete lifetime output, and
their count alone does not prove exhaustion.
These invocations do not change the active program or stack, including through
temporary ordered effects. Normal SSH, sudo authentication, nested shells, and
line-oriented REPL transitions still apply.

This is prompt guidance rather than runtime enforcement. See
[decision 0115](decisions/0115-emulator-terminal-limitations.md) for rationale
and the manual model-adherence checkpoint.

Supported transition operations:

- `push`
- `pop`
- `replace_active`

The transition object may contain `username` and opaque `metadata`. It must not
contain frame fields such as `cwd`, `interactive_program`, or
`shell_environment`; those belong directly in `state_update`.

SSH transitions require special care because an SSH command can either enter a
client-side interactive prompt or establish a remote shell immediately. A bare
`ssh user@host` command from an ordinary shell should push a nested context. If
authentication and remote shell startup complete in the same command result,
the pushed frame is the remote shell on the target host. It must not use
`replace_active`, because that would overwrite the parent shell instead of
nesting the remote login.

If the SSH command reaches a host-key confirmation, password, passphrase,
keyboard-interactive, or MFA prompt, the pushed frame is the local SSH client.
That frame remains on the original client host and user, with target details and
authentication phase stored in `interactive_program.metadata`. Later input is
interpreted by the active SSH client. If the SSH client remains in progress but
changes phase, the active SSH frame should be replaced with another SSH frame.
Repeated password prompts after failed attempts are still the same SSH client
frame and should not push another frame. If authentication fails and the client
exits, the SSH frame should pop. If authentication succeeds and a remote shell
starts, the active SSH frame should be replaced with the remote shell frame.

SSH transition responses should stay compact. When the important effect of a
command is entering an SSH client prompt or a remote shell, prioritize the
`shell_stack_transition` and active-frame fields needed for that transition.
Avoid bundling optional host inventory or service details into `world_state` in
the same response unless those durable facts cannot be inferred later. A valid
small transition is more important than an oversized state patch.

For aliased or otherwise nontrivial SSH commands, a transition can include
`metadata.ssh` with `target` and `port`, for example
`{"operation":"push","metadata":{"ssh":{"target":"host-b","port":2222}}}`.
This identifies the SSH operation without relying on a hostname change:
`hostname` changes and local subshells do not create SSH sockets, while
`ssh localhost` does. Client authentication frames also retain target and phase
in `interactive_program.metadata` as described above.

The emulator receives the active program's prompt and metadata as well as its
name, command, and argv. Phase, chosen filename, pending confirmation, and retry
state are authoritative input, including after checkpoint refresh or history
pruning. Updating this metadata continues the same invocation. When a model
omits a transition, matching non-shell programs update the active frame;
SSH/sudo authentication can also replace their client frame with a shell.
An explicit push from an already-active SSH, sudo, ssh-keygen, or passwd
dialogue is normalized to replace_active. Genuine nested shell/REPL pushes
remain supported. The legacy equal-depth shell_stack format follows the same
continuation rule. Normalization and resulting depth are traced.

Interactive SSH, sudo, ssh-keygen, and passwd secret prompts use non-echoing
terminal input when program state or prompt text indicates password, passphrase,
or similar secret entry. Filename and overwrite prompts remain visible. Secret
entries are not saved in terminal recall history, and reverse search cannot echo
subsequent secret input. This does not change the existing operator tracing
policy. If output already ends with the active explicit program prompt, the
transport adds neither a separating newline nor a duplicate prompt.

At an idle authentication/key-generation prompt, Ctrl-C records SIGINT/130 and
pops the active frame once, restoring the parent and forcing checkpoint refresh.
Bash Ctrl-C clears the current line. Cancellation while a model call is still
running retains the existing immediate cancellation behavior; it does not
mutate a frame still owned by the worker. Once cleanup finishes, idle Ctrl-C can
leave that dialogue. Repeated Ctrl-C during cleanup must not block on its lock.

## Empty Interactive Input

`program_input.py` owns the blank-input policy shared by the backend and command
graph. At ordinary bash, sh, dash, ksh, and zsh prompts, empty/whitespace-only
lines are local no-ops: no model call or command-history entry. Program identity
is independent of the line editor; unfamiliar programs accept input.

All other interactive programs receive the original input, including an empty
string or whitespace-only passphrase. Empty input can accept a filename default,
choose no passphrase, submit an authentication attempt, or complete a REPL
statement. Meaningful empty submissions retain command IDs, history, state
updates, and normal tracing. MCP permits them when the active program needs
input; its ordinary-shell blank-request rejection remains unchanged.

A shell awaiting read, continuation, or here-document input sets
`interactive_program.metadata.input_mode` to `line` using replace_active, then
returns it to `command` when ready for shell commands. Existing non-command
phase metadata and conventional continuation prompts also accept input. Such
inputs bypass shell builtins: an entered `exit` is data. The terminal treats
CRLF as one Enter across packet splits and frame changes, while repeated Enter
presses remain separate submissions.

See [decision 0120](decisions/0120-interactive-input-lifecycle.md) for the failure
evidence and ownership boundaries.

## Host-Local Active SSH Connections

The session owns a hidden registry of simulated SSH connections. Each connection
has stable source and destination host identities, endpoints, an ID, and an
authentication phase. Shell-frame ownership keeps an outer connection active
while nested SSH or local programs run. Authentication retries retain the same
connection and source port once the SSH client has reached the peer. A local
key-passphrase prompt can precede socket creation. Authentication failure,
cancellation, and departure from its owning SSH context remove that connection;
leaving a local subshell does not close the login that contains it. A frame
replaced by `exec ssh` retains its upstream ownership until the replacement
exits.

`CommandContext.environment_metadata.connection` is the active frame's
simulated login ingress, or `None` when none is supplied. It is not the real
DREAMLAND listener. The initial native SSH login reaches the selected starting
host's primary interface on port 22, including a lobby-selected world.
Deployment addresses and ports remain transport diagnostics. The runtime
initializes `SSH_CONNECTION` and `SSH_CLIENT` on each new SSH login; later shell
assignments, unsets, and environment-clearing commands retain normal semantics.

`environment_metadata.metadata.active_network` is a JSON projection containing
`host`, `connections`, compatible SSH `listeners`, and
`authoritative_session_connections`. Each connection row has `id`, `protocol`,
`service`, `state`, `direction`, `local`, `peer`, and `phase`. Endpoint objects
contain `ip` and `port`; direction is relative to the named host. A host sees
both its incoming and outgoing connections, and loopback SSH exposes both local
socket endpoints. The original entry socket is visible only on its starting
host. These facts take precedence over contradictory old output or ambient
claims about this session. Unrelated authored and ambient sockets remain part
of world simulation. Authentication-phase sockets do not imply a completed
remote-user login, and historical `Last login` text does not identify the
current connection.

Explicit [connection translation facts](NETWORK_CONNECTIONS.md) preserve each
host's local and peer endpoints across SNAT/DNAT. Login environment variables
use the server's observed endpoints. If real ingress and the starting primary
address have incompatible families, the runtime selects a stable fictional
source in the primary address's family and retains the real source in transport
provenance.

Models report lifecycle through the existing stack transition contract. The
registry is not a new writable `state_update` field and transient socket rows
do not belong in durable world inventory. Runtime-normalized login environment
and address changes trigger a full agent-state refresh; every command also
refreshes the projected environment metadata even when a graph checkpoint is
otherwise reused.

This implements continuity for foreground session SSH connections. Network
table text remains model-generated: `ss` and `netstat` must obey ordinary
protocol, family, state, and endpoint filters, but runtime authority alone does
not guarantee every generated row is rendered correctly. The
[manual checkpoints](TESTING.md#active-ssh-connection-checkpoints) validate that
remaining rendering boundary. See
[decision 0094](decisions/0094-host-connection-continuity.md) for ownership and
persistence scope.

Tracing keeps ingress provenance separate from these model-facing facts.
Command trace `dreamland.connection.*` fields have scope `simulated_host`;
session lifecycle traces retain legacy ingress fields with scope
`transport_ingress`. Explicit `dreamland.transport.connection.*` fields identify
the real admission endpoints, preferring the raw socket capture when available.
`CommandContext.transport_connection` carries this provenance only to tracing
and is excluded from the command prompt and graph state serializers.
Detached product effects use `proposed_` connection event names before the
product core accepts their provisional state.

## Built-Ins

Some shell behavior is handled locally for speed and consistency. Current
built-ins include simple directory, identity, environment, assignment, export,
unset, set, declare, printenv, env, exit, and logout behavior.

The deterministic agent owns built-ins directly. `AgentBackend` handles built-in
commands before calling agents that do not declare `handles_builtins`.

Command routing examines the complete input before executing any segment.
Simple lists using `;`, `&&`, and `||` execute locally only when every segment
is a supported builtin and every executed segment can be resolved locally.
Eligibility uses a detached session copy, including preceding cwd/environment
changes and short-circuit status. It produces no live state, history, events,
network effects, or model calls. `AgentBackend` records each executed local
segment and refreshes the command-agent checkpoint on its next invocation.

Any nonbuiltin anywhere in a list selects one command-model invocation for the
whole unchanged input, even in a conditional branch that might be skipped.
A builtin that defers, including an unknown `cd` target, also selects the whole
line; the runtime never applies a prefix before falling back. The original
spacing, quoting, and separators reach the model. Unsupported syntax such as
pipes, redirection, backgrounding, subshells, command substitution, and unsupported
builtin options also stays intact for the model. Shell parsing never determines
the meaning of input to a non-shell interactive program.

The local builtin set is DREAMLAND's supported forms, not Bash's full builtin
inventory. For example, `echo`, `true`, and `false` remain model-backed, and
`env ls` requires the model although plain `env` is local. `cd /tmp; pwd`
is local; `cd /tmp; pwd; touch file` reaches the model once. See
[decision 0102](decisions/0102-whole-command-routing-and-ordered-effects.md).

Ctrl-D is handled by the SSH session loop rather than the command agent. When
Ctrl-D exits a nested frame, DREAMLAND treats that as an external state update
and marks the next command for full session-state refresh so checkpointed agent
state cannot retain the popped frame.

`cd` updates `cwd`, `PWD`, and `OLDPWD` when the target is deterministically
known to be a directory. The current validation is a temporary conservative
bridge, not a complete filesystem model. It accepts baseline shell directories
and paths established by prior successful `cd`, `pwd`, or `ls -l` style output;
it rejects targets known to be files, known missing, or known inaccessible from
prior command output. It also treats `/root` and its descendants as root-only
baseline paths. If the target is ambiguous, DREAMLAND does not force the local
built-in and instead routes `cd` to the command agent so the model can make the
scenario-specific determination.

Filesystem hints derived from history accept only simple standalone commands.
The output and final status of a compound line cannot establish that its first
`cd`, `pwd`, or `ls` succeeded or produced a particular output line. Likewise,
aggregate inputs supply conservative directory focus and general shell surface
hints to enrichment; they do not fabricate per-segment presence evidence.

## Whole-Line State Effects

A model-backed line produces one result, one command history entry, and one
accepted/completed lifecycle. The model interprets execution order, conditional
branches, combined stdout/stderr, and the final executed command's exit status.
Ordinary final cwd/environment/world changes use the existing `state_update`.
When ordering affects parent frames or connection lifecycle, the alternative
form contains only `effects`:

```json
{
  "stdout": "",
  "stderr": "",
  "exit_status": 0,
  "state_update": {
    "effects": [
      {"command": "cd /tmp", "state_update": {"cwd": "/tmp"}},
      {"command": "bash", "state_update": {"shell_stack_transition": {"operation": "push"}}}
    ]
  }
}
```

The example illustrates ordering; a real directory change must also include
the changed `PWD`/`OLDPWD` shell environment as required by the state contract.
The parent is updated before the child is pushed, so exiting restores `/tmp`.
The same ordering preserves exports before nested shells and parent state
before SSH. Effects contain source-command attribution and ordinary state
updates; attribution is never executed. SSH reconciliation uses the applicable
simple effect command or explicit transition SSH annotation instead of parsing
the first word of a compound line as the destination.

Model envelopes allow 1–64 effects, reject nested effects and full `shell_stack`
replacement inside effects, and cannot mix effects with ordinary root fields.
The complete sequence is validated before applying any effect. DREAMLAND owns
hidden parent frames and reconciles connections in order. History and completion
remain attached to the submitted line rather than invented segment outputs.
Cancelled commands discard unaccepted state effects; there are no intermediate
model round trips or accepted prefix changes for a model-backed line.

## Model-Facing Emulator Projection

The backend, `CommandContext`, LangGraph checkpoint, and saved state-profile
state remain complete. Immediately before a model-backed command is formatted,
DREAMLAND creates a non-mutating view of `world_state` for the command emulator.
This view keeps all top-level fields and every `hosts_by_name` key. The active
host retains its established metadata except `login_banner`; inactive hosts are compact identity, purpose,
operating-system, recorded network/IP, fact, and service cards. Inactive local
history, filesystem, users, groups, shell, environment, and interactive-program
detail are omitted.

The prompt keeps an explicit active-host anchor, while the complete active host
record appears once inside `hosts_by_name`; there is no second active-host
metadata block. Omission from an inactive compact card is not evidence that the
underlying host lacks the omitted state.

The saved `login_banner` is runtime presentation for initial shell entry and
never enters command or enrichment model input through host metadata. It remains
in saved state and Spiritguide's context. The redundant `profile:` prompt label
is removed; host OS and shell metadata provide platform identity. Saved scenario
names remain operational metadata for tracing and failure capture.

Projection does not change state-update semantics. Model patches merge into the
complete runtime world, and Spiritguide continues to receive complete state.
The projector accepts additional detailed host references for future
command-aware multi-host contexts, although the first runtime version selects
only the active context host.

## History And Checkpoints

Each session has a stable DREAMLAND session id, currently stored on
`SessionState.langfuse_trace_id` for historical compatibility. LangGraph uses
that id as the checkpoint thread id. Langfuse command tracing treats the same
value as the Langfuse `sessionId`, while each LangGraph command invocation can
create its own Langfuse trace inside that session. When checkpoint state already
exists, new invocations send the current command and the current runtime
environment metadata, then rely on checkpointed state for remaining fields
unless a full refresh is required. Runtime-normalized changes after transitions
and externally applied changes request that refresh.

Command history records command, cwd, stdout, stderr, exit status, interruption
status, and signal. Result metadata is intentionally excluded from session
history to avoid prompt bloat.

## Interruptions

Ctrl-C creates an interrupted result with exit status `130` and signal
`SIGINT`. DREAMLAND records rendered output only, discards buffered partial
output, logs the interruption, and attempts to record final provider token
usage when available.

## Maintenance

Update this document when `CommandContext`, `CommandResult`,
`CommandStateUpdate`, world-state directive or projection semantics,
shell-stack semantics, built-in behavior, checkpointing, or interruption
handling changes. Keep
`src/dreamland/prompts/state_contract.md` aligned with the runtime parser and
`src/dreamland/prompts/world.md` aligned with world-state generation policy.
