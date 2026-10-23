# Architecture

Status: Active
Audience: maintainers and coding agents
Purpose: describe the current runtime components and ownership boundaries.

## Overview

DREAMLAND separates transport, session state, state profiles, agent
orchestration, prompts, and observability. The stable public facades are
intentionally thin. New behavior should usually go into the owning internal
module rather than expanding a facade.

## Entry Point

`src/dreamland/__main__.py` loads settings, configures runtime logging, loads the
required state profile, builds the server profile, constructs
`LangGraphCommandAgent`, wraps it in `AgentBackend`, creates the shared session
event bus and default EnrichmentAgent runtime, constructs the required
native SSH authenticator, and starts `DreamlandSSHServer`. The EnrichmentAgent
service starts with automatic scheduling and deterministic safe application
enabled by default. Explicit configuration may disable scheduling or select
non-mutating `shadow` mode while keeping manual inspection and submission
available.

The entry point also reads the separate `[product_api] enabled` master gate.
The default disabled branch does not import `dreamland.product`, invoke its
composer, or construct product runtime objects. The normal runtime has no live
product composition: an explicitly enabled gate calls the reserved composer and
fails startup closed before any product listener or runtime is created. The
Phase 1 lifecycle preview is available only through a separately invoked
development tool and does not change this branch.

Without `--daemon`, the SSH server runs in a background thread and the process
opens the local Dreamland console. With `--daemon`, the server runs in the
foreground and streams logs.

## Transport Layer

`src/dreamland/ssh_auth.py` owns the transport-independent SSH authentication
contract and the native shared-password implementation. The application loads
the static password from `DREAMLAND_SSH_PASSWORD` once at startup. An accepted
username is an untrusted simulated-session label rather than an authenticated
identity.

`src/dreamland/ssh_host_keys.py` owns the real SSH endpoint's transport
identity. It resolves the per-service-user default state path, validates the
private directory and key, securely loads an existing RSA key, or generates and
atomically publishes exactly one key without overwriting a concurrent winner.
It also derives the public OpenSSH SHA-256 fingerprint. This real transport key
is independent of the fictional host identity in a state or server profile.

`src/dreamland/ssh_transport.py` owns the Paramiko server. It handles listening
sockets, consumes the validated durable host key before binding,
password-only authentication negotiation, channel setup,
shell and exec requests, connection metadata, client cleanup, and lifecycle
logging. It permits at most one `session` channel per connection. Public-key,
none, keyboard-interactive, and GSSAPI authentication are denied, as are SFTP
and other subsystems, port forwarding, agent and X11 forwarding, unknown global
requests other than Paramiko's harmless forwarding-cancellation acknowledgement,
client environment requests, and non-session channel types. A cancellation is
a no-op because DREAMLAND never permits forwarding state to be established.
There is no normal-startup fallback to an ephemeral identity: key storage,
validation, or loading failure prevents the listener from opening.

`src/dreamland/ssh_server.py` is a compatibility facade. It re-exports the
public SSH server classes and selected legacy helper aliases.

## Session Layer

`src/dreamland/ssh_sessions.py` owns shell and exec session loops. It converts
terminal bytes into editor events, handles Ctrl-C and Ctrl-D behavior, rejects
attempts to enter the `!dreamland` operator namespace, writes command output
back to channels, and rebuilds editors when the active interactive program
changes. SSH authentication grants only attendee shell/exec access and never
operator authority. The first interactive entry writes the initialized active
host's saved `login_banner` before its prompt without invoking a model. Each
fresh lobby selection has its own first entry; later simulated logins remain
LLM-emulated. Exec requests do not receive the login banner.

`src/dreamland/ssh_streaming.py` adapts backend streaming into line-buffered
terminal writes. `src/dreamland/ssh_session_state.py` owns SSH-specific shell
closing and nested-shell fallback behavior. Transport-side exits such as
Ctrl-D use the shared session-state stack pop path and force the next command
to refresh checkpointed agent state.

`src/dreamland/session_registry.py` owns the live session registry used by
operator controls that need direct access to mutable `SessionState` objects.
SSH shell and exec sessions register while they are running and unregister when
they exit. Registered sessions include a per-session lock so operator-side state
changes can coordinate with command execution. The registry also publishes
neutral session registered/closing events to the shared background-agent bus.

`src/dreamland/state_profiles.py` owns session-derived state profile JSON
serialization. State profiles are operator-created save/load objects at three
depths: exact session state, system state without shell position/history, and
high-level world state. World-level profiles preserve durable world anchors,
known hosts, known services, generated artifacts, and high-level facts while
dropping session position and command history. The module validates the minimum
document schema and keeps console command code out of serialization details. It
also owns the thread-safe runtime default holder that lets operator console
commands change which saved profile future SSH sessions use.

## Terminal Editors

`src/dreamland/terminal/` contains line editing, history navigation, editor
events, and editor registration. Bash/readline behavior lives here rather than
inside SSH transport.

## Backend And Agents

`src/dreamland/backend.py` adapts `SessionState` into `CommandContext`, handles
local built-ins when the agent does not own them, records command history,
applies state updates, and logs command replies. It can optionally create new
sessions from a saved state profile while preserving the new live session trace
id and current connection metadata.

`src/dreamland/command_routing.py` prepares fully deterministic builtin lists on
detached state. Backend and graph entry paths share that decision. Any nonbuiltin
or deferred builtin sends the entire original line through one model execution.
Model results can carry an ordered state-effect sequence when parent-frame
changes must precede a shell/SSH transition. `session_state.py` applies those
effects and their connection reconciliation; aggregate command history and
completion remain owned by the caller. See [Agent State](AGENT_STATE.md).

`src/dreamland/agent.py` is the stable public agent facade.
`src/dreamland/agent_core.py` owns the LangGraph command agent, deterministic
agent, graph construction, state formatting, structured model response handling,
checkpoint interaction, streaming, interruption recording, and compatibility
helpers.
`src/dreamland/model_providers.py` owns provider-specific chat model
construction for the command agent and spiritguide. Agent orchestration code
should depend on the provider factory rather than importing provider SDK
integrations directly.

Focused agent modules own specific behavior:

- `agent_builtins.py`: built-in command routing.
- `agent_shell.py`: shell variable, environment, path, and small command logic.
- `agent_state.py`: state serialization and deserialization.
- `agent_structured.py`: structured response parsing helpers.
- `agent_tracing.py`: Langfuse callback setup, token logging, tracing metadata.

`src/dreamland/spiritguide.py` defines the operator-side spiritguide boundary.
Spiritguides do not emulate user commands. They receive admin instructions and
return state updates for DREAMLAND-owned state. Live-session guidance applies
to one registered session. `src/dreamland/spiritguide_profiles.py` owns the
detached profile workflow that builds synthetic sessions from a blank world or
saved state profile, runs Spiritguide, and saves the resulting world or system
state profile for future sessions. Spiritguide authors host-local initial-login
banners; detached save validation requires valid entry/active host references
and banners. Dry runs report the same contract failures without saving.

`src/dreamland/background_agents/` owns the neutral session-event bus, immutable
event and trigger contracts, deterministic subscriber gates, and command-surface
hints intended for reuse by independently authorized background agents. It also
owns bounded deterministic `interaction_focus`, derived from command syntax and
working-directory state and copied across correlated events as non-evidentiary
attention context, plus deterministic exact-artifact, related-path, and
surface-only comparison for later foreground usefulness telemetry.
`src/dreamland/background_agents/authority.py` owns the reusable bounded active
session-frame observation and stable session-semantics fingerprint. It returns
authority only for the active target host. Program arguments are count/length
bounded and recognized credential-bearing forms are redacted before model
handoff; arbitrary frame metadata, environment, history, output, and credential
input are excluded. This background projection boundary does not alter
DREAMLAND's intentional foreground credential recording. A separate complete,
credential-sanitized semantic digest is held outside traced graph state and is
attached only after LangGraph execution for final safe-apply race checks.
`src/dreamland/background_agents/enrichment/` owns the first such role: a
session-local asynchronous service with explicit `focused` and
`ambient_presence` planning modes. Deterministic trigger policy chooses the
mode before invocation. The `enrichment-v15` LangGraph follows initial snapshot
-> prepare Coordinator context -> focused Coordinator or Ambient Coordinator ->
prepare Expert brief -> Expert -> classify Expert items -> current snapshot ->
resolve references -> Integrator -> normalize Integrator output -> validate
Integrator output. The branch remains directly visible, and exactly one
Coordinator runs per job before both paths rejoin the shared Expert and
Integrator flow. The ambient role has a separate prompt and model configuration
but no separate evidence or mutation authority.

The agent also owns a distinct inspectable attempt-1 Integrator-refresh
LangGraph: fresh snapshot -> resolve original references -> Integrator ->
normalize -> validate. The service may invoke it only after final safe apply
finds a sole `foreground_evidence` authority conflict while the target remains
the active host and the original validated refresh basis exists. Coordinator
and Expert are reused rather than rerun. The refreshed proposal returns through
ordinary atomic safe apply; a second conflict, any `active_session` conflict,
or a host pivot is terminal. `enrichment graph` renders both graphs separately.

Both Coordinators and Integrator are DREAMLAND-aware. The prepared Coordinator
context includes the selected mode and one structural `coverage_summary` with
target-host counts for facts, filesystem artifacts, groups, services, and
users, helping work broaden coverage without model-generated bookkeeping. For
ambient mode only, concise `presence-gap-v2` adds the `independent_life`
objective, a preferred existing technical surface, the exact
`named_human_or_person_equivalent_excluding_foreground_user_and_service_accounts`
subject preference, and bounded anti-repetition hints. A current non-shell
surface hint wins; otherwise deterministic fallback chooses the least-covered
identity, service, or filesystem surface. The ambient causal target is a
recognizable named individual linked to time-qualified personal, work, or
social activity and connected observable residue, not a new actor key or
taxonomy. Service and machine identities may support but cannot center the
cluster. The object is planning priority rather than evidence. The generic
independent Expert receives only the neutral allowlisted six-field brief,
Coordinator-selected expertise lens, and deterministically translated creative
latitude created at the visible preparation boundary. Field-sensitive neutral-
brief validation keeps internal control markers prohibited throughout and
routing-role markers strict in the Expert question and object keys while
allowing ordinary practitioner titles in the Coordinator-selected
`expertise` values. Ambient brief construction appends the deterministic
`ambient-human-presence-v3` task constraint inside the ordinary question while
preserving all six fields; focused construction remains byte-equivalent. The
policy treats active, login, effective, and privileged
accounts plus established aliases as one foreground person and reserves every
directly observed login, session, process, or socket row to its observed or
unknown owner. A distinct historical or later event remains possible when it
is explicitly time-qualified, is not presented as that foreground row, and is
grounded independently of protected row values. A value learned only from a
protected row cannot connect the proposed person to the distinct event unless
established state, a supplied non-row observation, or a selected reference
separately supports that connection. When
the target is active, the selected Coordinator additionally sees
bounded foreground authority, Expert receives it as a neutral
`active_session` observation within the same six fields, and Integrator sees a
fresh capture. Its version-2 items are net-new candidates with explicit
origin and reference basis. Expert latitude deterministically carries paired
candidate and relation limits of 2, 4, 8, 12, or 24. Integrator receives a
deterministic eligible-only
candidate view; blocked material and Coordinator routing text do not cross that
boundary. Relation policy `expert-relations-v2` validates and deduplicates
relations in authored order, retains the first valid unique entries up to the
effective limit, and discards invalid, duplicate, or overflow relations
independently at the Expert graph-parse boundary. Conditions, gates, basis
references, anchors, and source IDs retain their separate eight-entry bounds;
item, basis, gate, and report contracts remain strict.
Ambient presence establishes scenario-appropriate historical life, other basic
human or person-equivalent identities, personal and work artifacts, logs,
connection context, and operational residue connected to those individuals.
Integrator returns `no_safe_mapping` for a service-only ambient report rather
than turning it into another system-centered patch. This rule is an
`ambient-human-integration-v3` suffix appended only to ambient initial and
refresh calls; the shared prompt and focused Integrator `SystemMessage` remain
byte-for-byte unchanged. The suffix omits material that transfers a direct row
to the proposed person or reuses a protected row value as the only grounding
for that person's event, preserves independently grounded person-centered
residue, and returns `no_safe_mapping` when the cluster depends on the transfer
or reuse. Ambient may also
materialize a bounded current observation when direct evidence, lifecycle
qualification, and durable context support it. It remains additive and does
not simulate autonomous personas or own temporal start, stop, or expiry
behavior. Explicit operator or profile direction for solitude or another
incompatible setting wins.

Enrichment context v4 adds public foreground-evidence schema version 1 and
private `foreground-evidence-v2` authority. A dedicated deterministic module
recognizes the newest successful standalone host-time command and at most four
recent complete standalone inventories for `/home`, login history, current
sessions, sockets, or all processes. Ambiguous, filtered, truncated,
structured-envelope-like, failed, or interrupted output remains ordinary
context. Custom `date +FORMAT` results require a calendar, year, or epoch
conversion before becoming host-time evidence. Public bounded observations
reach the existing role contracts; the independent Expert still uses its six-
field input and schema-v2 report. Private authority instead hashes the latest
recognized record per exhaustive `(kind, coverage)` scope across complete
history plus latest host time—or a stable empty-view sentinel—and binds it to
every focused or ambient patch after traced graph execution. Public expiry
alone does not change this dependency, while a new same-scope record, new
scope, or new host-time record does. An aged-out observation is no longer
prompt-visible as current negative evidence and is not a permanent absence
fact; it only stabilizes hidden authority against unrelated command cadence.
The deterministic normalization node prunes unsupported array leaves, admits
only pairwise-disjoint `merge_missing` writes to a shared target, and makes no
repair model call. The graph returns either a provenance- and
reference-dependency-checked bounded patch or a typed no-op and never commits
state. Deterministic runtime code privately binds an active-session fingerprint
to relevant clusters; a safe apply rechecks it under the final lock and skips a
stale cluster without exposing the fingerprint to a model. With the default
in-flight limit, the service owns at most three concurrent jobs. It always owns
at most one deferred automatic candidate per session plus one separate bounded
retry marker for an active host's bootstrap. Active-host bootstrap wins equal-
priority deferred contention against discovered-host bootstrap. Interaction
pulse becomes eligible after every four successful commands, has no elapsed-
time or per-scope cooldown, and resets only after its own acceptance; its
priority remains above only idle. The service also owns Enrichment-specific
trigger priority, 60-second candidate age,
one-shot release, short session locks, shadow/safe mode, current-state conflict
checks, active-session semantic race guards, apply, read-ahead readiness,
classified command-match timing, process-local automatic provider circuits, and
operator job history. Runtime generations isolate non-waiting stop/restart
boundaries: stale workers, deferred callbacks, and snapshotted event deliveries
cannot mutate or schedule work in the replacement runtime. The deferred
position is process-local subscriber policy, not an event bus queue. Terminal
circuits suppress automatic work only; manual jobs remain available for
operator diagnosis. When tracing is enabled, the
service-owned parent run
remains open through application while the LangGraph proposal is its visible
child. See
`docs/ENRICHMENT_AGENT.md` for the complete contract.

`src/dreamland/provider_failures.py` owns a provider-neutral bounded exception
taxonomy shared by foreground command failure reporting and enrichment. It
classifies terminal account/configuration conditions separately from transient
rate, timeout, and availability failures without retaining provider-authored
prose or depending on one provider SDK's exception classes.

`src/dreamland/structured_failures.py` owns durable structured failure capsules
for malformed command-agent response envelopes. The command parser records raw
response and failure classification metadata, and `AgentBackend` writes a
capsule before applying state updates or appending command history. Capsules
embed exact pre-command state for later parser replay and future command replay
tooling. The local console and MCP binding expose capsule listing, detail, and
parser-only replay.

## State Model

`src/dreamland/models.py` defines the shared dataclasses for interactive
programs, shell environments, shell frames, command results, command contexts,
connection metadata, world state, and session state.

`src/dreamland/session_state.py` owns generic shell-frame copying, active-frame
synchronization, shell-stack replacement and pop operations, command state
updates, and initial environment construction. Shell frames carry host identity
so nested SSH or future cross-host contexts can restore the parent host when
the active frame exits.

## Profiles And Prompts

`src/dreamland/profile.py` describes transport and generic session defaults for
the current entry point. Its real SSH protocol identity is neutral
`SSH-2.0-DREAMLAND`; it supplies no simulated OS personality or static greeting.
`src/dreamland/login_banner.py` validates and resolves saved host-local
initial-login text. `src/dreamland/state_profiles.py` loads required schema-2
state-profile JSON, including packaged defaults, and turns it into per-session
state. Version-1 documents are rejected without an automatic migration.
`src/dreamland/world_state.py` contains helpers for active-host lookup,
derived active metadata, prompt JSON rendering, and state synchronization.
`src/dreamland/emulator_context.py` owns the non-mutating active-host projection
used only when `src/dreamland/agent_core.py` formats command-emulator model
input. Stored `login_banner` fields are excluded from command-emulator and
enrichment projections. Backend, checkpoint, saved-profile, and Spiritguide state
stay complete. The redundant server-personality `profile:` prompt field is
removed; operational saved-profile names remain available to tracing and failure
records.
`src/dreamland/prompts/` contains versioned prompt modules.

Prompt modules describe behavior. State profiles describe simulated systems,
larger environment direction, scenario seeds, host metadata, and generation
policy.
Do not put scenario facts into prompt modules when they belong in a profile.

## Console And Observability

`src/dreamland/console.py` owns operator-command parsing and execution.
`src/dreamland/console_runtime.py` owns the local prompt-toolkit console
experience. SSH never routes commands into this console; its session layer
rejects the reserved `!dreamland` namespace. Enrichment controls expose runtime
status, manual live-session submission, recent jobs, and expanded Mermaid graph
output. A reusable console refresh descriptor keeps live status rendering in
the console layer; the local console runtime owns terminal capability checks,
alternate-screen entry and guaranteed restoration, home-and-clear redraw,
one-second timing, bounded stop-key consumption, cancellation, and
presentation-only writes. After restoring the main screen, it prints one final
status snapshot. Refresh failures retain only bounded failure labels and
exception types in logs and operator output. The EnrichmentAgent runtime
supplies only short thread-safe status snapshots and does not block for terminal
observation.
`src/dreamland/console_introspection.py` adapts Langfuse observations into
sessions, commands, inspections, and state diffs.

`src/dreamland/admin_interface.py` owns the protocol-neutral admin service
boundary for future REST and MCP bindings. It wraps live session registry,
runtime logs, session trace logs, state profile runtime, and Langfuse-backed
console introspection without importing web or MCP server dependencies. It also
owns headless MCP session creation, command execution through the normal
backend, typed inspection helpers, tag grouping, and MCP-created session
closure for agent workflows that bypass Paramiko without bypassing session
state.
`src/dreamland/admin_api.py` owns the optional FastAPI/uvicorn local HTTP
binding for authenticated REST admin endpoints and the MCP Streamable HTTP
endpoints mounted through the official MCP Python SDK. `/mcp` exposes the
operator/admin MCP surface, and `/client-mcp` exposes the generic command-client
evaluation surface.

## Private Product API And World Access Architecture

`src/dreamland/product/` is the boundary for the separate private product
service used by a UI-owned same-origin BFF. It is not an admin-service adapter:
the admin bearer, Operator resources and tools, raw session inspection, and
legacy MCP-created sessions are not product authority. Browser authentication,
cookies, CSRF, Origin/Host/CORS checks, public throttling, browser DTOs, and
browser recovery deadlines stay in the UI/BFF project; DREAMLAND owns the
private capability and future runtime/session semantics.

`src/dreamland/product/composition.py` still provides only the guarded
`compose_product_runtime(...)` seam. An enabled call raises
`ProductCompositionUnavailable`; no product lifecycle, route, worker, timer,
journal, provider reservation, event subscription, or session is constructed.
The default disabled runtime never imports this module. Product code therefore
adds no hook or bookkeeping to SSH, admin, MCP, state-profile, logging,
EnrichmentAgent, or ordinary command paths.

`src/dreamland/product/lifecycle.py` owns the Phase 1 protocol-neutral,
in-memory integration preview. It has its own immutable revision catalog,
epoch-bound actor/launch/session authority, idempotency ledger, actor-current
bindings, active records, and safe terminal tombstones. It imports no legacy
runtime module, calls no provider, and creates no worker or timer. Immutable
revision bytes are copied into isolated session data; lazy operation-boundary
cleanup owns expiry and bounded retention. Its lifecycle-only snapshots expose
none of the mutable revision document.

Phase 2 extends that boundary only when an integration caller constructs it.
`commands.py` owns the independent `product-single-v1` classifier;
`admission.py` owns a product-only session, logical-job, worker, token, and
spend ledger; `journal.py` owns bounded transcript and event recovery;
`execution.py` owns the tagged executor/result DTOs; and `core.py` composes
those pieces into the sole asynchronous command slot, exact cancellation and
terminal publication fence, accepted-state CAS, command-result query,
snapshots, reset/switch replacement, and lazy lifecycle reconciliation.
Provider work, foreground state validation, and replacement validation run
without holding the aggregate lock. Each accepted output chunk uses one bounded
journal transaction under that lock; reaching the byte/chunk cap closes the
publisher so later noisy callbacks avoid the aggregate entirely. Terminal
publication and replacement cutover recheck their exact source identities and
concurrency coordinates under the lock. Event/transcript acceptance and output
pairs, accepted enrichment state and its canonical event, and current-session
admission replacement each commit atomically at their owning boundary. Bounded
map, journal, cutover, and final enrichment transactions still use the shared
lifecycle aggregate lock; per-session-lock/load proof remains part of the
disabled capability gate.

`runtime_executor.py` is a manually injected adapter for isolated state-profile
documents and command-generation checkpoint namespaces. It never wraps
`AgentBackend`, never registers product sessions in ordinary runtime indexes,
and owns no live provider construction. `live_agent.py` supplies an explicitly
constructed development-only bridge to a streaming DREAMLAND command agent,
one-attempt control, exact checkpoint-thread deletion, and conservative
full-reservation usage settlement. The manual live Stage tool constructs the
provider outside normal runtime composition. This is useful live-development
evidence, but a future production provider collaborator must make attempt
bypass impossible, supply exact usage evidence, and use credentials/capacity
isolated from ordinary SSH and default enrichment. The manual live Stage may
omit the session command-count limit after explicitly normalizing launcher
sentinel `-1`. Its process job-identity ceiling and token/spend budgets remain
finite and independent of the default 12-command guard; no sentinel crosses the
product API boundary. Before constructing a live command provider, the shared
launcher requires a finite positive configured output maximum and derives the
command reservation as `max(8,192, max_output_tokens)`. Presenter and attendee
token budgets scale by that reservation and their existing logical-job
allocations. Blank or zero configuration fails before provider or listener
construction. Startup reports the effective maximum, reservation, and budget;
published wire artifacts remain immutable. `enrichment.py` likewise owns no
scheduler or worker: an explicit caller supplies generation, admission,
validation, and observation
collaborators. Foreground commands and product enrichment share only the
dedicated product ledger, not ordinary runtime accounting.

The live agent adapter also converts provider-token-sized output into
segmentation-independent publication units. Complete lines flush immediately;
unterminated text is UTF-8-safely bounded at 4 KiB, and stream switches flush in
order. The live chunk-count guard is sized so the 65,536-byte command ceiling is
always the first content boundary. Ordinary SSH streaming and deterministic
preview policy are unchanged.

LangGraph command streams are single-owner iterators. A cancellation or
deadline observed by another thread may set the shared command interruption
flag and invoke only thread-safe interruption markers; it must not close the
iterator or read/write graph checkpoint state from that observer thread. The
worker advancing the stream detects the flag, closes its own iterator, and
records the interrupted graph result once. Product cancellation still closes
the output and accepted-state publication fence immediately, so provider bytes
or state arriving after cancellation cannot become accepted session truth.
Checkpoint cleanup still follows physical executor completion. This ownership
rule adds no normal-runtime worker, callback thread, or polling loop.

Phase 3 adds `stage.py` as another explicitly constructed layer over the shared
core. It accepts only Presenter authority and uses the exact originating launch
grant—not revision equality—to decide whether an ordinary launch can resume the
current Stage. Changed selections require the shared expected-control-CAS,
provision-before-cutover replacement path. Each immutable revision has a
trusted bounded title, source label, and revision label; the Stage projection
contains those display fields and explicitly reports X-ray unavailable. It
never projects the runtime document or imports the future X-ray model.

`admission.py` now supports a zero-by-default Presenter Stage reservation inside
the same hard aggregate session, logical-job, worker, token, and spend limits.
An explicitly configured Stage service assigns Presenter sessions to that
partition. Standard product work can use only the remainder and cannot consume
the reserved Stage allocation; Stage also remains bounded by its own partition.
This is protocol-neutral accounting evidence, not proof that a deployment's
provider credentials or account-wide limits are isolated correctly.

Phase 4 begins with `forge.py`, a separately constructed protocol-neutral
World Forge boundary. It owns the exact typed BSides `WorldSeed`, deterministic
pre-provider validation, canonical hashing, and a bounded provider-free job
lifecycle used by owning-layer tests. Importing it starts no provider, worker,
timer, listener, or runtime composition. It does not change the default state
profile, join SSH/admin/session registries, expose a Stage launch operation or
wire authority, or enable any product capability. The separate
`contracts/world-forge-api/v0.1.0-rc.2/` kit and explicitly invoked
`tools/product_forge_preview.py` add only a provider-free owner/BFF integration
target. Its unconfirmed preparation precedes atomic server-time confirmation
and submission. Opaque cursor coordinates require snapshot reconciliation, and
restart loss never redispatches work. RC2 keeps the published RC1 immutable,
aligns every runtime error and cancellation retry with the contract, makes
recipe identity failures operation-level, prevents premature deadline expiry,
and enforces lifecycle/receipt/cancellation/provenance relationships in schema.
The corrective RC4 kit and `tools/product_forge_live.py` form a separate,
explicitly started headless vertical slice. One configured Spiritguide call
produces an isolated candidate; deterministic structural/resource validators
and synthetic boot run independently; a create-only SHA-256 store records only
valid canonical world state; and the lifecycle atomically selects the trusted
receipt only before its authoritative deadline. Deadline comparison and
terminal commit share the job lock, so watcher scheduling cannot admit a late
selection. Explicit-source RC4 operations preserve one source tuple through
recipe, preparation, submission, snapshot, events, cancellation, and receipt
responses while the immutable RC2 preview wire shape remains unchanged.
Referenced entry and active hosts must be objects, and entry-shell validation
is unconditional. The live source and receipt are distinguishable from
provider-free preview evidence. A trusted live selection now also commits an
immutable private presenter-owned selected record under the same service lock;
preview and non-selected outcomes never mint one, and the existing RC2/RC4 wire
projection does not expose it. Jobs remain in-memory, attempts are A-only, and
the lifecycle deadline does not kill an in-flight provider request. Durable
live coordination, hard provider deadlines, A/B/C selection/repair, and shared
hosted-budget integration remain later Phase 4 checkpoints.

Phase 5 adds three inert protocol-neutral seams. `forge_artifacts.py` reopens
the exact selected content-addressed file by revision and digest with owner,
mode, no-follow, canonical-byte, schema, and validation checks.
`forge_runtime.py` hydrates that canonical Forge envelope into a detached
session-local runtime without consulting or mutating the ordinary profile
store; after commands change state it retains the exact immutable Forge origin
beside the detached runtime document. The artifact digest covers canonical
file bytes including the terminating newline, while the existing lifecycle
revision SHA covers its newline-free canonical document. They are distinct
integrity domains.

`forge_stage.py` is the trusted in-process coordinator. It resolves only an
authenticated actor's current-epoch selected Forge job reference, provisions a
narrow Presenter grant into a dynamic-only `PresenterStageService`, and then
delegates exact create/resume or explicit expected-control-CAS replacement to
the existing Stage/core services. Dynamic Stage begins with empty revision and
grant catalogs and has no static rehearsal fallback. Provisioning validates
the exact Forge identity before atomically registering revision, grant, display
metadata, and source correlation. It does not alter the default profile or
ordinary SSH/admin/MCP registries. Forge-backed snapshots add the optional
`stage.source` tuple—selected-record ID, immutable Forge digest, Forge job ID,
and Forge reference—while continuing to report X-ray unavailable.

Phase 5.1 adds no runtime module or composition path. It corrects the contract
projection of the existing replacement cutover: `lifecycle.py` owns authority
revocation, control-revision advancement, and bounded terminal retention;
`core.py` owns the terminal command-slot and `session.terminal` journal
transition; and `stage.py` preserves the terminal session's Forge source. The
corrective fixture and validators represent those same layered semantics
rather than partially relabel an active snapshot. Request-relative CAS and
event-history checks remain owning service/operation tests, not claims inferred
from a response-only schema.

Phase 6 adds `product/xray.py` as an inert display-projection boundary and an
optional `StageXrayPolicy` on `PresenterStageService`. With no policy, existing
Stage snapshots and operations remain unchanged and no X-ray state is
allocated. The explicit combined Forge/Stage development target supplies the
policy. Under the existing product-session lock, Stage builds a bounded
allowlist from the accepted detached runtime document; it never serializes
`SessionState`, imports admin/runtime projections, or reads ordinary profile,
SSH, trace, provider, environment, or failure stores.

Each active session lazily owns an opaque X-ray stream and monotonic projection
sequence. An accepted-state revision change advances the X-ray position even
when the display allowlist is unchanged. A foreground activity or other
projection-only change may advance X-ray position without changing accepted
state. Snapshot and detail carry both coordinates; detail rejects either stale
coordinate with `xray_resync_required`. The Stage event position is captured
under the same lock but is only a correlation watermark. It does not order
X-ray items or precondition detail. Terminalization drops the X-ray projection
and keeps terminal and replacement behavior independent.

The initial allowlist contains bounded world display labels, host
identity/purpose/operating-system fields and aggregates, and accepted world or
host fact statements from explicitly named scalar fields. Stable entity IDs
are hashes of internal keys rather than raw keys. Text is clipped and Unicode
control/format characters are marked; collection omissions and clipping are
reported explicitly. Paths, users, environment, raw shell state, command text,
arbitrary nested values, prompts, reasoning, credentials, provider envelopes,
traces, failure capsules, and other sessions are absent. Projection failure is
isolated from Stage terminal, command, transcript, event, and snapshot
operations.

Phase 7A adds two further inert boundaries for private Presenter integration.
`authored_artifacts.py` imports one operator-configured world profile only from
a trusted startup path, requires its configured SHA-256 pin before committing
anything, and snapshots the metadata-free world state into an owner-only,
create-only, content-addressed artifact. Publication writes and fsyncs an
owner-only temporary inode before an atomic no-overwrite hard-link commit, so
concurrent identical imports cannot expose a partial final artifact. Profile
names, source paths, mutable profile metadata, and the ordinary profile store
are outside the immutable revision and never become product transport input or
output.

`launch_selection.py` owns a bounded, thread-safe, source-neutral registry for
immutable Presenter launch options. Its closed origins are
`authored_profile_snapshot` and `world_forge_selection`. A display-safe option
includes an `offering_id` and opaque `launch_selection_id`, but neither value
authorizes Stage. Listing and exchange still require the private BFF bearer,
current service epoch, and Presenter actor evidence. Only an exchanged,
actor-bound, operation-scoped, expiring `launch_grant` is authority, and the
BFF—not browser code—retains that grant and actor context.

`launch_selection_stage.py` coordinates that registry with a dynamic-only
`PresenterStageService`. The exchange provisions the resolved immutable
revision before publishing the grant or idempotent result. Stage accepts the
same closed origin set without accepting profile names, paths, raw state,
digests, revisions, or caller-created grants from the transport. This
source-neutral seam can drive ordinary create and explicit switch/replacement
without changing the existing Forge-specific handoff or its richer Forge
source projection. A freshly selected Forge revision remains private Presenter
authority only; it does not become attendee-eligible without the separate
Phase 8 promotion and catalog path.

Coordinator construction fails closed unless Presenter credential digests,
actor identity and expiry, operation families, and both Stage revision-size
ceilings cover every current and future registry offering. This prevents a
trusted miscomposition from publishing an option that can only fail at Stage.

The registry caps its option set at the contract's 128-item wire bound and
requires every offering for one immutable revision to share one Stage display
tuple. Dynamic Stage reclaims expired grant slots before adjacent provisioning;
it retains revision, display, and source projection data through any dependent
active session or recovery tombstone, then removes it. Each source-neutral
grant embeds a positive, epoch-local monotonic serial and a digest derived from
that serial, the service epoch, and private candidate entropy. The registry
therefore retains only bounded active grants instead of a lifetime grant-digest
history. Active-capacity accounting includes a callback currently provisioning,
including reentrant trusted callbacks. Grant UTC projections use the registry's
current wall clock while authority is bounded by both absolute UTC expiry and a
monotonic deadline. Registry and Stage must share one monotonic clock; the
trusted provisioning seam transfers the exact private registry deadline and
Stage also caps it by the remaining UTC lifetime. Forward, backward, callback-
boundary, or compound wall-clock adjustments therefore cannot make Stage and
the registry disagree about authority lifetime. Stage releases an expired
source-neutral digest while retaining the serial high-water mark, so an old
grant cannot become authoritative again. The existing Forge-specific seam
keeps its legacy digest-reservation behavior.

The `stage.launch_selection.exchange` idempotency claim is scoped to service
epoch, actor/role, key, and canonical request body. The claim occurs after
bearer, current-epoch, Presenter actor, key, and body validation but before
selection lookup. A deterministic `launch_selection_unavailable` result is
retained and replayed; changed-body reuse conflicts. Retryable adjacent
capacity rejection leaves the key unclaimed, and an old epoch returns
`outcome_unknown` without reminting. Exact replay returns the original grant,
including after that grant and its offering expire, from the projection recorded
with the original logical outcome.

`tools/product_launch_selection_preview.py` is the provider-free transport for
this checkpoint. It explicitly constructs a dynamic Stage, the registry, and
one authored plus one synthetic Forge option, while reusing the hardened
private Stage/X-ray transport and deterministic executor. It proves the
browser-integration mechanics.

Phase 7B adds the separate explicit live composition without changing the 7A
transport semantics. `launch_selection_runtime.py` dispatches only the closed
authored and Forge immutable/runtime kinds. Authored accepted state retains a
verified private origin envelope; Forge state delegates to the existing
origin-preserving Forge codec. A codec instance accepts a detached runtime
origin only after it verified that immutable origin, so a command cannot
change source kind or introduce an arbitrary state-profile document.

`launch_selection_forge.py` resolves an authoritative selected record through
the live Forge service with the exact shared Presenter actor, reopens its
content-addressed artifact, and atomically registers a safe generic option.
The observer runs only after Forge commits selected truth. Failure logs a safe
registration event and leaves the selected job unchanged without publishing a
partial option. Generic and legacy Forge seams use the same display helper so
the same revision cannot acquire conflicting Stage display metadata.

`tools/product_launch_selection_live.py` imports one configured, digest-pinned
authored snapshot before publication, builds Stage with the kind-dispatching
live command executor, binds the registry to Stage's exact epoch and shared
actor expiry, and starts the private Forge and Stage listeners only on
loopback. The target keeps legacy Forge handoff compatibility and registers
fresh generic Forge options after authoritative selection. Its startup output
reports safe model and limit data but no source path, profile name, pin,
credential, actor context, grant, or capability. Forge build and accepted
Stage command operations may call configured providers; listing, exchange,
artifact provisioning, and X-ray do not.

Phase 7C adds `attendee.py` as an authored-only private-BFF facade over a
separate attendee registry and the shared session core. Explicit composition
pre-seeds the same finite `ATTENDEE` actor pool into both owners. One
high-entropy gateway-context digest leases one actor slot for the epoch, and a
slot is never reassigned. The facade returns only BFF-held actor authority,
lists only authored attendee offerings, binds selection exchange to the
presented notice version, and delegates existing session operations only after
context validation. Attendee sessions always use the ledger's `STANDARD`
partition and cannot consume Presenter reservations. UTC and monotonic context
deadlines clamp actor, grant, and session lifetime; lazy coordinated cleanup
terminalizes expired state without creating a timer or worker. Late Forge
offerings remain hidden and unavailable pending Phase 8 eligibility.

Phase 7D composes that facade with the explicit Phase 7B live process in
`tools/product_attendee_live.py`. The combined Product listener preserves the
Presenter launch-selection, terminal, replacement, and X-ray routes while
routing shared session operations by exact actor ownership. Unknown actors
fall through to neither service. The attendee registry receives only the
startup-pinned authored revision; authoritative Forge registrations remain in
the Presenter registry and cannot become attendee options.

The attendee command path has its own live executor and in-memory checkpointer
and forces external tracing off. It shares the process admission ledger but
uses only a separately bounded `STANDARD` allocation; the Phase 7B Presenter
session, job, worker, and spend reservations remain unchanged, while Presenter
and attendee token allocations independently scale to cover their finite
configured provider maxima. The attendee offering, grant, session, and command
publication are fenced by the configured UTC and monotonic admission cutoff.
Context ownership can survive briefly for bounded terminal-current recovery,
while explicit shutdown revokes authority, cancels work, and fences late
completion before worker teardown.

Phase 8A adds two more inert, protocol-neutral boundaries without changing the
published Product attendee operations. `generated_access.py` resolves one
current authoritative selected Forge record and immutable artifact through a
trusted source, then records bounded hidden access truth under separate
Operator authority. Reveal is expected-record CAS: a trusted publication
callback must reserve and register the exact catalog offering before the
revealed successor commits. Paths, raw state, Forge authority, and caller-
chosen revision coordinates never enter the mutation request.

`attendee_catalog.py` is the only bridge from revealed access truth into the
attendee registry. Its bounded catalog combines authored offerings with active
revealed generated bindings while excluding bare Forge-origin registry entries.
Safe items contain opaque offering/revision locators, closed origin, bounded
display, notice, and expiry only. Exact catalog and notice evidence causes the
coordinator to mint an internal signed eligibility proof; the attendee facade
verifies that proof before its constructor-disabled generated provisioning seam
returns an ordinary Product launch grant. Existing Product attendee option and
exchange methods remain authored-only, and unchanged Product session creation
consumes the resulting grant. Product provisioning also returns its already-
computed launch-binding digest to the World Access exchange result. A private
BFF may compare that output-only digest and the shared epoch with an unchanged
Product snapshot after ambiguous create-response loss; no request consumes the
digest as authority.

The separate `contracts/world-access-api/v0.1.0-rc.3/` contract pins
provider-free Product API `v0.7.0-rc.3` and World Forge API
`v0.1.0-rc.4`; reviewed RC1 and published RC2 remain immutable. RC3 retains
RC2's structural lifecycle/retry and actor-class gates and adds the required
server-only launch-binding correlation field. Its loopback preview uses an
explicit deterministic provider-free selected-artifact adapter, a single
notice for the mixed authored/generated catalog, and evaluator-current time
anchored by monotonic elapsed time; deterministic clocks remain injectable.
It cannot claim live Forge, provider, capability, or production evidence.

Phase 8B adds the separate `tools/product_world_access_live.py` composition.
The explicit `serve` command first validates published World Access
`v0.1.0-rc.4`, published Product `v0.7.0-rc.4`, and published World Forge
`v0.1.0-rc.4` with exact dependency hashes. Only then does it load settings,
construct providers, build the existing Forge/Presenter plus attendee live
composition, and attach World Access before binding loopback listeners. The
World Access RC4 identity preserves all six RC3 operation and recovery shapes
while truthfully reporting provider calls, authoritative live Forge evidence,
and live attendee execution. It continues to report no live capability
evidence and an empty production-capability set.

The owner-only Phase 8B bootstrap contains two distinct credentials. Presenter
authority owns World Forge, Presenter registration, and Stage handoff. Operator
authority owns generated-world promotion, status, and reveal. A
`WorldForgeAccessSource` uses Presenter authority only inside the trusted
process to resolve the exact current selected record and immutable artifact;
the external Operator request supplies only a non-authorizing job reference.
The access service, attendee launch registry, generated catalog, attendee
facade, and attendee core all use the exact Product service epoch. A Product-
leased attendee actor therefore reads and exchanges the World Access catalog
without authority translation.

`product_attendee_live.py` keeps generated support disabled by default. Its
Phase 8B constructor seam requires an eligibility key, expands the otherwise
single-authored revision/offering capacities to 17 each, and sets a finite
generated-publication capacity of 16. `attendee_catalog.py` accounts prepared
and committed generated bindings for the full service epoch rather than only
currently visible catalog entries. Expiry hides an entry but does not reclaim
its offering or revision, because a launch grant or exact exchange replay may
still depend on that retained authority. Exhaustion returns the existing
retryable publication-capacity failure; restart is the explicit reclamation
boundary and rotates the epoch. `generated_access.py` additionally caps each
access record to the selected Forge record's remaining lifetime, even when the
configured access TTL is longer.

The generated route remains separate from Product's authored attendee option
and exchange operations. Only selected Forge truth followed by explicit
Operator promotion, reveal, exact catalog/notice evidence, and World Access
exchange can create the ordinary Product launch grant consumed by unchanged
session creation. World Access RC3 launch-binding correlation remains intact:
the BFF compares both service epoch and output-only digest after ambiguous
Product create, and no request accepts that digest as authority.

Unlimited human evaluation maps `--max-commands -1` to absence of only the
positive accepted-command count. The attendee `STANDARD` partition, Presenter
reservation, command and output bounds, one-attempt provider policy, finite
worker/token/spend budgets, TTLs, dual-clock cutoff, cancellation ownership,
and shutdown fences remain unchanged. No production capability is enabled.

A bounded provider-backed backend smoke on 2026-08-30 exercised the complete
live Forge selection through generated-attendee command path, including hidden
access, reveal, mixed catalog, exchange/create replay, shared-epoch launch-
binding correlation, unclipped output, and clean SIGINT shutdown. It provides
composition evidence only; browser security, focused cancellation, load,
deployment, and presentation gates remain independent.

Phase 9A adds a smaller private security boundary without changing Phase 8B.
`attendee_security.py` owns an optional bounded fixed-window ledger for fresh
logical attendee work. It keys only by a digest of the opaque gateway context
and a process-wide counter; it has no browser, cookie, IP, HTTP, provider, or
ordinary runtime dependency. The attendee facade resolves existing
idempotency replay/conflict truth before charging bootstrap, grant exchange,
create, submit, reset, or switch. A retryable rate denial creates no downstream
claim. Reads, cancellation, close, context revocation, expiry, shutdown, and
reconciliation remain uncharged so saturation cannot block safety or recovery.

The same facade adds a BFF-authorized, epoch-bound, idempotent context
revocation. Its semantic claim binds digests of both the gateway-context id and
its exact actor context. Success fences the context lease, selection actor and
grants, core actor and grants, sessions, commands, output, and accepted-state
publication. Exact replay resolves before the fenced context is examined;
changed-body reuse after a successful claim conflicts, while any fresh missing,
mismatched, expired, or fenced pair produces the same non-enumerating,
unclaimed authority denial. One successful replay slot is reserved per burnable
context, so invalid unique keys cannot exhaust legitimate teardown. The
response does not project either context, a session, or another credential.

`tools/product_attendee_security_preview.py` is the corresponding explicit
provider-free loopback target. It composes only the authored attendee registry,
facade, shared core, deterministic bounded workers, and logical ledger. An
exact route allowlist excludes Presenter Stage, X-ray, World Forge, World
Access, Operator, admin, MCP, providers, profile lookup, and normal runtime
registration. Product `v0.8.0-rc.1` retains all RC4 operations and adds this
private revoke operation and logical-rate failure; all production capabilities
remain disabled. Browser cookies, CSRF, Origin/Host policy, public-edge
throttling, client-IP policy, DTOs, and browser recovery remain owned by the UI
BFF, which must keep every DREAMLAND credential server-side.

The backend's successful deliberate teardown remains
`attendee_context_revoked`; the UI/BFF-owned typed `access-ended` result is not
a DREAMLAND wire error. Restart keeps its separate epoch fence: stale reads are
`session_lost`, stale mutations and revokes are `outcome_unknown`, and neither
layer automatically rebases, bootstraps, remints, or redispatches. A new
browser flow crosses either fence only through explicit context-start.

Phase 9B adds `attendee_live.py` and `attendee_provider.py` behind this exact
private attendee boundary. The former owns a frozen, non-secret model
configuration and child-local factory confined to one exact immutable authored
revision. The parent validates one owner-current-UID, exact-mode-`0600`,
regular, non-symlink credential without reading it. A spawned child revalidates
and reads the key, rejects and scrubs ambient provider/observability
credentials, disables external tracing and callbacks, redacts command text
from token/resource logs, and constructs its attendee provider with zero SDK
retries.

`AttendeeProviderPool` is a fixed-capacity, spawn-only process boundary. It
admits bounded command and immutable-state DTOs over bounded JSON IPC. That
per-command channel never transfers the transport bearer, actor/launch/session
authority, idempotency keys, internal paths, or caller callbacks. The spawn
factory separately carries the validated credential path and exact revision,
but not the key value. Every worker must become locally
ready before the listener binds; that means only local executor/provider-client
construction, not upstream reachability or quota. Dispatch covers the bounded
worker startup path plus both retired-child containment phases. Pool and
Product core share the same runtime-document policy: 4 MiB encoded bytes,
depth 32, 65,536 nodes, string object keys, finite numbers, and signed 63-bit
integers. The pinned initial document and every proposed committed state are
preflighted against it. The live composition derives retained
logical-job capacity from active-session capacity times the configured per-
session command maximum (256 at the defaults), and scales its token and spend
budgets to the same process-lifetime ceiling. Output, attempts, tokens, spend,
request time, cancellation, replacement, drain, shutdown, and controller
convergence remain finite.

Phase 9C-D removes the fixed four-session rehearsal ceiling only when an
operator explicitly configures the attendee live or supervised target. The
accepted workload defaults remain four epoch-lifetime context slots, four concurrent
sessions, two provider workers, and eight admitted connections. Hard maxima are
64 contexts, 64 sessions, eight workers, and 32 connections, with
`workers <= sessions <= contexts`. Every selected connection capacity sizes the
handler semaphore; an explicitly selected non-default value also sizes the
listener backlog before bind, while the accepted default retains its inherited
backlog behavior. The process-lifetime
logical-job and token/spend budgets scale from sessions times commands; the
one-second shared fresh-work limit scales to one three-operation launch per
context plus one submit per worker. Actor pools, grants, tombstones, request
tracking, and all context/selection/core idempotency stores derive from the
same envelope. Their exact finite values are reported at startup and allocate
no records until this explicit process handles work. Product RC4 remains
byte-identical and capability-disabled. Decision 0082 records the formulas,
20/20/32/5 UI rehearsal profile, and remaining load-evidence boundary.

An ambiguous or locally forced dispatch consumes its full reservation and is
never automatically redispatched. At the exact provider deadline,
cancellation already observed first retains interrupted/actual-usage truth. A
deadline observed first fences output and grants the child only the bounded
cancellation grace; a confirmed return is completed, non-ambiguous, and
reusable but remains a full-reservation `provider_deadline_exceeded` result. A
non-return remains ambiguous, fully reserved, and contained. Local
cancellation or process termination fences late DREAMLAND output and state
publication but does not prove the upstream request or billing stopped.
`begin_drain` gates fresh work but must be followed by bounded `shutdown` to
retire children; a production deployment also needs an external supervisor.
Decision 0080 records this exact-deadline containment and the unchanged RC4
`expired`/`absolute_expired` plus `session_terminated` terminal projection.

`tools/product_attendee_security_deployment_canary.py` is a separate,
explicitly invoked Phase 9C live-provider canary around the supervised target.
It neither changes nor regenerates Product `v0.8.0-rc.4`. The operator must
acknowledge provider spend and supply an exact profile path/digest, an
owner-only provider credential, an explicit model, and a canary-bounded
`max_output_tokens` value from 1 through 8,192. That bound limits the one
authorized provider response without changing the supervised target's range.
The tool owns a disposable private bootstrap, binds only loopback on an
ephemeral port, and fixes the deployment at one worker, one accepted
`uname -a`, a per-session command limit of one, and one logical provider
attempt. The canary alone fixes
the independent effective output-chunk maximum at 240; the supervised target
default is unchanged. This leaves room in the 256-event journal for running,
optional state, two final publications, and terminal overhead while keeping
accepted-cursor replay complete. Credential content is read only inside the
spawned provider worker. Importing the tool constructs no provider client and
adds no listener, process, credential read, or cost to ordinary DREAMLAND or
SSH startup.

The canary reconstructs the complete command event stream and transcript,
rejects clipping or inconsistent terminal truth, and verifies close,
`attendee_context_revoked`, and bounded shutdown. Its strict report records
single-run live-provider evidence while keeping
`production_capability_evidence=false`. Settled usage remains the conservative
full reservation: success verifies exact token and spend usage equal the
announced reservations. Neither value is actual provider token or billing
truth. The report also announces 65,536 output bytes and 240 output chunks per
command. The canary does not prove upstream request-count instrumentation,
provider account or quota separation, cancellation or billing termination,
deployed load,
rollout/rollback, a public edge, UI/BFF browser security, or production
readiness. Backend restart behavior remains `session_lost` for stale reads and
`outcome_unknown` for stale mutations/revokes; only the BFF owns its deliberate
browser `access-ended` result. Decision 0081 records this evidence boundary.

`tools/product_attendee_security_live.py` composes only the Phase 9A attendee
facade/core, one pinned authored revision, and this dedicated pool. It exposes
the same attendee-only loopback allowlist and no Stage, X-ray, World Forge,
World Access, Operator, admin, MCP, profile-path, or generic launch authority.
Product `v0.8.0-rc.2` is the published identity-only successor and remains
byte-for-byte immutable. Corrective `v0.8.0-rc.3` preserves RC2's wire,
authority, revocation, and recovery behavior while selecting the existing
reviewed provider-backed `notice:attendee-live-development-v1` and
`policy:attendee-live-authored-v1` through the launch offering. The provider-
free Phase 9A composition keeps its own security-preview identities. RC1 and
RC2 remain immutable and every production capability remains disabled.

Worker replacement readiness remains a pool-lifecycle fact, not a fixed test
delay. Replacement gates wait through
`startup_timeout_ms + (2 * terminate_grace_ms)` plus a one-second test
scheduler margin, fail on a terminal lifecycle, and require
`ready`, full ready and available capacity, zero active workers, and a
successful follow-up dispatch in the focused cancellation and concurrent-
failure cases. This test synchronization change leaves all runtime startup,
dispatch, cancellation, containment, and shutdown bounds unchanged.

Importing the Phase 7A through 9C-D modules starts nothing. Normal DREAMLAND
startup does not import them or construct a product object, listener, worker,
timer, provider, reservation, authority registry, publication counter, or
bookkeeping path, so ordinary SSH behavior and cost remain unchanged.

Retained contract roots and exact version-to-target mappings are listed in
[the backend contract inventory](../contracts/README.md). The source release
keeps distinct capability-discovery, lifecycle, command-core, Stage, X-ray,
launch-selection, authored-attendee, security-preview, live-provider, and
supervised-deployment identities. Superseded snapshots and their historical
byte-preservation tests are omitted; active artifact bytes are unchanged.

Current Forge/Stage handoff uses Product `v0.5.0-rc.2`, with additive X-ray
under `v0.6.0-rc.3`. Launch selection uses `v0.7.0-rc.1`/`rc.2`, authored
attendee targets use `v0.7.0-rc.3`/`rc.4`, and attendee security uses
`v0.8.0-rc.1` for provider-free checks, `rc.3` for the corrected live privacy
identity, and `rc.4` for supervised deployment. Each retains its existing
authority, recovery, capability, and startup boundaries.
Every production
capability, including
`stage_xray`, remains disabled. The X-ray routes remain evidence for the
explicitly started combined development targets; RC1 remains the provider-free
integration preview and RC2 identifies only the explicit Phase 7B live target.
All kits are documented in
`docs/PRODUCT_SESSION_API.md`. Their manual loopback tools include
`tools/product_contract.py`, `tools/product_lifecycle_preview.py`,
`tools/product_core_preview.py`, `tools/product_stage_preview.py`,
`tools/product_stage_live.py`, and
`tools/product_launch_selection_preview.py`, plus the explicit
`tools/product_launch_selection_live.py` and provider-free
`tools/product_attendee_preview.py`, and the explicit combined live
`tools/product_attendee_live.py`, plus the provider-free attendee-only
`tools/product_attendee_security_preview.py` and explicit dedicated live
`tools/product_attendee_security_live.py`, plus the separately invoked
supervised `tools/product_attendee_security_deployment.py`. The provider-free
black-box gate for that target is the separately invoked
`tools/product_attendee_security_deployment_rehearsal.py`; it uses the real
RC4 server, transport, services, watcher, supervisor, and fixed worker-process
pool but replaces the provider executor with a deterministic finite
collaborator. It emits only allowlisted local-rehearsal evidence and is never a
production capability or provider-reachability claim. The separately
versioned World Access kit lives under
`contracts/world-access-api/v0.1.0-rc.3/`; its generator and provider-free
loopback harness are `tools/product_world_access_contract.py` and
`tools/product_world_access_preview.py`. No runtime module imports these tools,
and they are not production transports or capabilities. The core and Stage
compatibility previews use a deterministic no-provider executor. The separate
live-development targets may invoke their explicitly configured provider only
after explicit startup and accepted provider work. Every target reports every
capability disabled.

The supervised attendee target resolves one exact UTC open, admission-close,
and recovery-close window before constructing attendee authority. The shared
attendee service projects those timestamps onto its monotonic deadline domain.
A target-owned watcher runs coordinated expiry and drain at admission close
even without incoming traffic while leaving bounded terminal recovery
available. The main thread owns signal and recovery-close teardown; it stops
accepting connections, joins admitted request threads, fences service
authority, converges the dedicated provider pool, and joins task threads. A
new process always creates a new epoch. Credentials are never hot-reloaded and
normal DREAMLAND startup never constructs the watcher or supervisor.

The deployment rehearsal drives that boundary through actual subprocesses and
loopback sockets at its accepted fixed four-context, four-session, two-worker,
and eight-connection limits. Valid requests use the immutable RC4 generated
server-side client; bounded raw-socket probes exercise an unsupported-media
request with unread pipelined-looking body bytes and stalled incomplete
headers. A body rejection that can leave request bytes unread forces connection
close so those bytes cannot become a second HTTP request. Saturated shutdown
accounting includes one deadline for an occupied handler slot and one for an
already accepted connection waiting on that slot. Workflow authority never
enters the machine-readable report. Restart evidence covers the transport
bearer and fresh epoch. The deterministic worker factory succeeds with a blank
owner-only provider file that would fail live content validation, proving the
rehearsal does not read credential content; provider-key consumption and
rotation remain unproven.

World Forge is separately versioned under `contracts/world-forge-api/`, not
added to the product-session artifact or capability list.
`tools/product_forge_stage_live.py` is the explicit combined evaluation target.
It constructs one live Forge provider collaborator and one live Stage command
collaborator, then binds two loopback listeners with distinct private bearers
and one shared Presenter actor. Forge submission may spend one logical
provider-backed Spiritguide attempt; accepted Stage commands may spend a
logical command-agent attempt. Handoff, artifact validation,
resume, and replacement provisioning do not call a provider. The selected
record and derived grant expire one hour after selection, bounding the Stage
session even though its configured absolute lifecycle TTL is longer; idle
expiry may shorten it. The tool does not provide the shared Forge/Stage
provider-budget enforcement required for capability enablement. Its operational
handoff is documented in `docs/FORGE_STAGE_LIVE_DEVELOPMENT.md`.
Phase 5.1 and Phase 6 acceptance inject the Forge and command collaborators, so
the terminal correction and X-ray HTTP tests add no provider call even while
they exercise both listeners. It also changes no default import: normal
DREAMLAND still constructs no product object, listener, worker, reservation,
or provider. The X-ray layer begins from the corrected terminal snapshot
baseline and remains opt-in and isolated from normal runtime.
The stateful preview accepts an owner-only, non-symlink bearer file, bounds
connection admission, applies read and absolute connection deadlines, and
normalizes parser/default failures into safe non-cacheable JSON. Those sockets,
request threads, and deadline timers exist only after the tool is invoked.
Normal tool serving is threaded; the explicit one-request mode dispatches its
handler synchronously so process exit cannot truncate a valid response.
The five product capabilities remain independently disabled until their owning
implementation and acceptance gates pass.

`src/dreamland/runtime_logging.py` configures event-filtered logging, bounded
in-memory log retention for console inspection, and MCP SDK log forwarding into
the `dreamland.mcp` runtime event category. It also retains structured
EnrichmentAgent events in a bounded deque per session, independently of the
global runtime ring, for exact session/job reads through the admin service.
Enrichment lifecycle, trigger, model, lock, snapshot, and patch records use the
separate configurable `enrichment` event category. Retained records also expose
bounded mode, Coordinator-role, coverage, presence-gap, foreground-evidence,
bootstrap-retry, pulse, authority, focus-match,
provider-failure, and circuit metadata; raw foreground authority, its
fingerprint, and provider response prose are not retained.

## Native SSH Lobby

`src/dreamland/ssh_lobby.py` owns the optional parent-connection state machine:
welcome, once-per-connection agreement, curated picker, fresh visit, retirement,
and return to the picker. `ssh_transport.py` fixes configured entry mode for a
connection and admits only interactive PTY shells in lobby mode. Direct remains
the default and retains existing shell/exec behavior.

`lobby_input.py` owns bounded ASCII input and phase-boundary byte handling.
`lobby_content.py` loads immutable versioned plain-text notices.
`lobby_catalog.py` validates explicit saved world/system profiles, publishes
atomic catalog updates, and rejects stale picker selections. A launch uses one
validated profile copy through the existing per-session backend API; no lobby
choice changes the global runtime default.

`lobby_identity.py` owns an operator-only SQLite public-reference index and
trusted runtime provenance. Native console aliases resolve complete public IDs
before legacy internal-ID behavior. REST/MCP and `LiveSessionRegistry.get()`
keep their existing lookup contract. Trusted public identity is projected into
lifecycle, command, Spiritguide, and enrichment observations without deriving
authority from saved or model-writable state.

`ssh_sessions.py` separates a lobby world's shell outcome from the real parent
channel's lifecycle. Every selection creates a new session/editor/history and
checkpoint namespace. Retirement stops command intake, cancels scheduled work,
unregisters the visit, suppresses late output, and releases obsolete resources.
Nested simulated shells retain their existing stack behavior. See
[Native SSH Lobby](SSH_LOBBY.md) for persistence, cleanup bounds, and operator
workflows, and the [development plan](SSH_LOBBY_DEVELOPMENT_PLAN.md) for required
release assessment.

## Maintenance

When changing a module boundary, update this document and keep compatibility
facades small. Add tests at the owning layer first; reserve real SSH transport
tests for behavior that requires Paramiko or socket integration. Keep decision
0076 aligned with the attendee-only credential, spawn-process, readiness,
capacity, conservative-accounting, drain/shutdown, and restart boundaries.
Keep decision 0077 aligned with immutable RC2 preservation, RC3 privacy
identity, lifecycle-derived replacement synchronization, and the unchanged
typed deliberate-end and explicit-restart boundaries.
Keep decision 0078 aligned with supervised availability and convergence,
decision 0079 aligned with the provider-free deployment rehearsal and its
local-only evidence, and decision 0080 aligned with exact provider
deadline/cancellation precedence, worker reuse, accounting, and RC4 terminal
projection. Keep decision 0081 aligned with the opt-in single-command canary,
complete-publication evidence, strict non-production claims, and unchanged
RC4 and restart semantics. Keep decision 0082 aligned with the opt-in attendee
capacity controls, derived stores and budgets, unchanged defaults, and the
separate measured-load gate.
