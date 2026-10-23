# SSH Lobby Development Plan

Status: Draft
Audience: development agents, DREAMLAND maintainers, conference operators
Purpose: hand off the agreed optional SSH lobby, curated world selection,
public simulation references, and required security assessment for implementation.

## Overview

DREAMLAND will offer an optional deterministic lobby on its existing SSH
listener. After password authentication, attendees read an introduction, accept
short guidelines, and choose a curated saved world. Every selection starts a
fresh isolated simulated session. Leaving that world's outermost shell returns
to the picker on the same real SSH connection.

This implements an additional native SSH entry experience for the
[LLM-emulated network product model](PROJECT_MODEL.md). It is backend and native
terminal work, owned by the `dreamland` lane. It does not require web UI work or
the private Product/World Forge attendee API stack.

The product scope below was settled with the owner on 2026-09-10. This document
is an implementation handoff, not evidence that the feature or its security
assessment has been completed. The task that produced it authorized planning
and documentation only. When assigned implementation, use the settled decisions
without asking the owner to choose them again.

## Details

### Implementation Checkpoint — 2026-09-11

Implementation is on `feature/ssh-lobby`, branched from clean `main` at
`565f3f2`. The previously flagged research/connected-world commits were already
ancestors of `main`; no unmerged branch or unrelated working changes were
incorporated by this feature.

Phases 1–3 are implemented. Current contracts are documented in
[Native SSH Lobby](SSH_LOBBY.md) and
[decision 0093](decisions/0093-native-ssh-lobby-visits.md). Provider-free owning
and real SSH tests cover hot curation, fresh visits, lifecycle cleanup,
public-ID console targeting, and trace provenance.

Phase 4 has a completed [independent security assessment](SSH_LOBBY_SECURITY_AUDIT.md)
with fixed and retested findings, a [manual OpenSSH rehearsal](SSH_LOBBY_REHEARSAL.md),
and a [controlled live trace check](SSH_LOBBY_TRACE_CHECK.md). All 45 observations
in that live check carried the correct public ID and matched the deployed
metadata filter. The Spiritguide call's response was rejected by its existing
structured parser, so the trace check does not claim a successful guidance edit.

This plan remains Draft as a release checklist until the deployed Langfuse UI
filter has been visually rehearsed. Its authenticated API filter is verified.
Actual public curation/deployment remains an operator action; no public service
was deployed. The final full regression run completed 2,142 tests with 8 skipped
and no failures; details are recorded with the audit evidence.

### Settled Product Decisions

| Area | Required behavior |
| --- | --- |
| Listener | One SSH listener; select `direct` or `lobby` through configuration. |
| Default | `direct` preserves the current default-world connection experience. |
| Public use | Only lobby mode will be public. Direct entry will remain private. |
| Authentication | Preserve the configured shared password and arbitrary nonempty usernames with existing sanitization. |
| Lobby | All text, input rules, and transitions are deterministic application behavior. |
| Branding | Display `DREAMLAND`; no tagline, including no “Networks made of dreams.” |
| Agreement | Once per real parent SSH connection; returning to the picker does not repeat it. |
| Worlds | An explicit operator-curated list of saved profiles from `state_profiles/`. |
| Isolation | Each selection, including selecting the same world again, starts a fresh isolated visit. |
| Nested SSH | Preserve realistic behavior inside the simulation; no lobby messages or agreement on nested logins/exits. |
| Leaving a world | Exiting the outermost simulated shell returns to the world picker. |
| Curation | Add, edit, order, remove, and reload entries without restarting; persist changes. |
| Public reference | A new independent public simulation ID for each lobby world visit. |
| Direct-entry output | No public ID banners, lobby, agreement, or picker in direct mode. |
| Reports | Request the public simulation ID; use `admin@boxisalie.com` for the POC. |
| Experiments | Encourage attacks on simulated devices, LLM behavior, and world continuity, with reports to the owners. |
| Infrastructure | Clearly protect the real hosting infrastructure, other visitors, and their data. |
| Disclosure | Briefly explain collected activity, its operational/evaluation purpose, and processing by OpenAI. |
| Publication | Attendee activity will not be published or used as excerpts in slides or published research. |
| Security | Direct audit and adversarial assessment of every deterministic screen and its implementation are required before release. |

The public reference identifies a visit, not a person, account, machine, saved
world, or parent connection. A username is still untrusted presentation context
and confers no administrative authority.

### Repository Preparation

Before implementation, read [AGENTS.md](../AGENTS.md),
[Project Model](PROJECT_MODEL.md), [Architecture](ARCHITECTURE.md),
[Operations](OPERATIONS.md), [State Profiles](STATE_PROFILES.md),
[Testing](TESTING.md), and [Documentation](DOCUMENTATION.md). In particular,
preserve the existing [native SSH authentication boundary](decisions/0046-native-ssh-attendee-authentication.md)
and [persistent host identity](decisions/0047-persistent-ssh-transport-identity.md).

Recheck repository status and ancestry rather than assuming this handoff's
checkout state is current. At handoff, the checkout was
`localonly/spiritguide-evaluation-research` at `a3edec7`, an evaluation/conference
archive commit created while this plan was being written. That branch includes
the seven commits through `8aad9e6` also present on the unmerged
`feature/spiritguide-external-networks` branch; they include connected-world,
network-identity, and geography changes. These eight commits were not on `main`.
Do not discard, move, stage, or publish that unrelated work with this feature.

Use a feature branch from `main` when implementation begins. Inspect
`git branch --no-merged main` and deliberately account for relevant unmerged
work. Follow repository rules if protecting the existing dirty checkout needs
a separate durable worktree; do not create feature worktrees under `/tmp`.
This plan does not authorize merging the evaluation work. Test changes before
pushing, and follow the repository's completion/merge convention.

### Connection and Visit Lifecycle

The state machine is small and explicit:

```text
SSH password authentication
  direct -> existing direct shell/exec behavior
  lobby  -> require an interactive PTY shell
         -> welcome
         -> guidelines and agreement
         -> world picker <---------------------------+
         -> validate selection and create fresh visit |
         -> print Started marker                      |
         -> simulated shell and any nested sessions   |
         -> outermost shell exit                      |
         -> retire visit and print Ended marker ------+

Quit, actual transport disconnect, or server shutdown ends the parent connection.
```

- Keep `direct` as the default. Lobby mode must never be selected by username,
  an attendee-provided command, or content returned by the model.
- Resolve entry mode for the connection and keep it fixed for that connection.
  Changing entry mode may require restart; hot mode switching is not required.
  Hot catalog management is required.
- Lobby mode accepts an interactive PTY shell only. Reject remote exec and
  non-PTY shells with a clear instruction to reconnect interactively, such as
  `ssh -t USER@HOST`. Do not create a simulation on rejected requests.
- Preserve shared-password authentication, persistent host-key behavior, one
  session channel per connection, forwarding/subsystem restrictions, and denial
  of the attendee `!dreamland` operator namespace. Preserve direct-mode exec.
- Do not create/register a simulated session, start its model work, or schedule
  its enrichment before both agreement and a valid world selection. Shared
  application/provider initialization is separate from per-visit execution.
- Accept `i agree` case-insensitively after trimming surrounding ordinary
  whitespace. Require the complete phrase followed by Enter. Invalid input
  stays on the agreement step. Provide `q` to disconnect.
- Record the accepted notice version and time against the parent connection,
  then correlate subsequent visits with that acceptance. Keep the accepted
  version stable for that connection when operator copy changes.
- The welcome and agreement appear again after a new real SSH connection.
- Create a fresh runtime session ID, public ID, command history, terminal editor,
  shell stack, and model checkpoint namespace for each visit. Do not reset and
  reuse the same session object to implement switching.
- Load and deep-copy the chosen baseline through per-session profile selection.
  Do not change the global runtime default to launch an attendee's choice.
- `exit`, `logout`, and Ctrl-D follow existing program and nested-shell rules.
  Only leaving the outermost world shell returns to the picker. Actual channel
  EOF is a disconnect, not an instruction to start another visit.
- The lobby owns final SSH exit status and channel closure. A world shell must
  return an outcome that distinguishes world exit from transport failure.
- An operator closing a live lobby visit must stop its command intake, request
  cancellation of its work, retire it, print the Ended marker when possible,
  and return the connected attendee to the picker. Unregistering alone is
  insufficient if a shell can continue accepting commands. Shutdown still closes
  the parent connection.
- Before another visit starts, unregister and retire the previous visit, close
  its writers, cancel scheduled work, and release obsolete runtime resources.
  Late results must not write into the picker or a later visit. Verify checkpoint
  reclamation and bounded resource use under repeated switching. Do not promise
  that already-running provider calls can always be stopped immediately.
- Discard already-buffered tails from a completed phase/visit and consume known
  trailing CR/LF or partial key/escape sequences without interpreting them as
  the next step's response. Never replay queued world commands into the picker
  or another visit. Apply the new phase's exact grammar to subsequent input;
  do not use arbitrary sleep-based flushing. Raw SSH cannot distinguish delayed
  pasted/scripted bytes from newly typed bytes. Agreement is an explicit input
  requirement, not proof of human reading or an anti-automation mechanism.

### Deterministic Screen Copy

The following copy establishes the intended content and tone. Small edits for
terminal width and clarity are allowed without reopening settled product
decisions. Keep the introduction and guidelines concise. Use deterministic
wrapping or paging on short terminals so agreement follows readable guidelines.
ASCII framing is optional; it must not introduce a tagline.

#### Welcome and Short User Guide

```text
+-----------------------+
|       DREAMLAND       |
+-----------------------+

Welcome! DREAMLAND is an AI-emulated computing network.
Explore its machines, files, services, and connections.
Experiment, follow a hunch, and see how well the world holds together.

Every world selection starts a fresh, isolated session.
Your changes do not affect other visitors or carry into your
next visit, even if you choose the same world again.

At shell prompts, use exit or Ctrl-D to work your way back.
SSH connections inside the simulation behave normally.
Leaving your starting shell returns you to the world menu.
Choose Q at that menu to disconnect.

Agree once per SSH connection. Each world visit gets a public
simulation ID, shown at its start and end. Include it in reports.

Press any key to continue.
```

Cancellation and disconnection must also work here. A key represented by
multiple bytes must not leak its remaining bytes into the next input field.

#### Guidelines and Disclosure

```text
Explore the simulation. Take care of the real infrastructure.

Simulated devices are fair game. Break them, challenge the LLM,
try to escape its role, and look for contradictions in the world.
We encourage these experiments and reports of what you discover.

Protect the real infrastructure running DREAMLAND and its visitors.
Don't disrupt the service, interfere with others, or access their
data. If you reach real infrastructure or data outside your
simulation, stop there and report what happened.

We collect connection details, including IP address and username,
plus commands, responses, and diagnostic/model traces to operate,
troubleshoot, evaluate, and improve DREAMLAND. Commands and world
context are sent to third-party OpenAI models. Don't enter real
credentials, confidential information, or personal data.

We will not publish your session activity. Starting a fresh world
does not delete retained logs or traces.

Report findings to admin@boxisalie.com. Include your simulation ID,
the world, approximate time, and steps to reproduce.

Type i agree and press Enter to continue, or Q to disconnect.
```

The non-publication statement concerns public dissemination of attendee
activity. It does not imply that processing by OpenAI, private operator review,
or disclosed diagnostic storage is absent. If external Langfuse tracing is
enabled for the deployment, include a brief accurate sentence about trace
storage there; do not falsely imply all logs remain local. Keep the final notice
aligned with actual deployed processing and retention. Do not invent retention
durations or add consent for public excerpts: the owner explicitly ruled out
publication.

#### World Picker and Visit Markers

```text
Choose a world
Every selection starts a fresh, isolated session.

1. Offshore Operations
   Explore an offshore facility and its connected systems.

2. After Hours
   Investigate a business network after everyone has gone home.

Enter a number, R to refresh, or Q to quit:
```

These are illustrative entries, not the chosen conference catalog. The operator
will supply the actual profiles, titles, descriptions, and order.

Immediately before the selected world's opening output, print:

```text
[DREAMLAND simulation DL-7M2K-P9XR-W4TZ-C6HN Started]
Include this ID in reports to admin@boxisalie.com.
```

On normal world exit, before displaying the picker again, print:

```text
[DREAMLAND simulation DL-7M2K-P9XR-W4TZ-C6HN Ended]
Report findings to admin@boxisalie.com with this ID.
```

Print `Started` only for a successfully admitted visit. Record failed starts
with the allocated reference if one exists and show a short failure message,
not a false success. Record the end and its reason server-side on abnormal
termination; a final banner cannot be guaranteed after network disconnection.
These are application-generated boundary markers. The application must never
interpret matching text in simulated output as a lifecycle event.

### Catalog and Operator Controls

Use one small persistent catalog owned by the operator. Each entry needs a
stable identifier, a validated saved-profile name, a public title, a short public
description, and explicit display order. Using the profile name as the stable
entry identifier and rejecting duplicate entries is sufficient for V1. Attendees
select display numbers; they cannot submit arbitrary profile names or paths.

Public descriptions must be explicitly authored; do not display raw profile
comments, source prompts, JSON, paths, or internal identifiers as menu copy.
Prefer `world` and compatible `system` profiles that seed a fresh login. The
subsequent lobby entry-account change uses required catalog `entry_username`
values instead of the original sanitized connecting-username design; see
[decision 0097](decisions/0097-lobby-simulation-entry-usernames.md). An `exact`
snapshot can restore history,
identity, and shell position: do not silently offer resume semantics as a fresh
visit. For V1, reject unsuitable snapshots during curation with guidance to
prepare a compatible world/system profile.

Proposed native console interface:

```text
lobby worlds list
lobby worlds add PROFILE --title "TITLE" --description "DESCRIPTION" --entry-username ACCOUNT
lobby worlds update PROFILE --title "TITLE" --description "DESCRIPTION" --entry-username ACCOUNT
lobby worlds order PROFILE_A PROFILE_B PROFILE_C
lobby worlds remove PROFILE
lobby worlds reload
```

Finalize parser details during implementation while retaining these operations.
The examples include the later required entry-account option for compatibility
with current catalog version 2; `update` accepts any subset of the three
options. Version-1 migration requires explicit accounts and an offline schema
version update, as documented in [SSH Lobby](SSH_LOBBY.md#catalog-and-saved-worlds).
This is a local operator-console interface. Do not route attendee text to it or
silently broaden admin REST/MCP contracts.

- Console mutations validate and persist the complete update atomically before
  publishing it in memory. They apply without restart to subsequent selections
  and picker renders, including on already-open parent connections.
- `reload` explicitly loads operator edits from the catalog file. Filesystem
  watching and automatic directory publication are unnecessary.
- A failed validation or write preserves the last working catalog and reports
  an actionable operator error. Initial invalid lobby configuration must not
  fall back to direct entry. An explicitly empty catalog is valid.
- Display an internally consistent catalog snapshot. Resolve the selected
  number through the snapshot's stable entry identity, then revalidate against
  current published state. If it changed, disappeared, or became unavailable,
  explain, refresh, and require a new selection. Never reinterpret a stale
  number as another world or silently use the default profile.
- Resolve/load the selected saved profile once for a launch and boot from that
  validated copy. Later file changes must not alter the active visit. Do not
  cache a failed profile indefinitely; refresh must allow recovery.
- Removing an entry does not delete its source profile or close active visits.
- An empty/unavailable catalog offers a friendly explanation, refresh, and quit.
- Keep welcome, guidelines, and contact copy operator-editable as plain text.
  Content updates apply to new parent connections with a versioned notice.
  Avoid a general-purpose template language or attendee-controlled substitutions.
- Document catalog/text paths, precedence, persistence, validation limits,
  input bounds, and a finite configurable lobby idle timeout. These operational
  defaults can be chosen during implementation; they are not unresolved product
  questions. Resolve paths consistently with existing configuration conventions.

### Public Simulation Identity and Tracing

Mint one public ID per lobby visit using a cryptographically secure generator.
The illustrative `DL-` prefix and four groups of four uppercase human-readable
base32 characters give a practical copyable format. Use a restricted alphabet
without visually confusing letters. Check collisions against the retained
registry atomically and retry generation; never overwrite an existing binding.

The public ID must be independent of backend/Langfuse IDs, username, source IP,
and world name. It is not a credential or proof that the reporter owns a visit.
Knowing it grants no access, and no public lookup endpoint or Langfuse link is
introduced. This design does not depend on assumptions about whether Langfuse
identifiers themselves are sensitive.

Keep identity in trusted runtime provenance, outside model-writable state.
Choose the simplest representation that can enforce these invariants:

- The public ID remains fixed across nested shells, model updates, Spiritguide
  changes, and operator profile application within that visit.
- New visits never inherit an ID or authoritative binding from a saved profile,
  model response, user input, or restored simulated state.
- Runtime-owned metadata copied into tracing is a projection of the authoritative
  identity, not another writable source of truth.
- The internal session ID continues to own existing checkpoints, trace grouping,
  and backend interfaces. A public reference does not replace it.
- Retain an operator-only local mapping containing public ID, internal session
  reference, world/profile reference, timestamps, and lifecycle status. Keep the
  mapping after exit/restart for the configured retention period. It need not
  duplicate transcript bodies. Align retention with local trace/index policy and
  document explicit cleanup; do not promise indefinite history.
- Recover historical records as ended/interrupted rather than live after a
  process restart. Restoring an index must never recreate a simulation.
- Local IDs and lookup work when Langfuse is disabled or unavailable. Missing
  historical trace data produces an accurate unavailable result, not a claim
  that restoring the ID mapping restored the trace.

Export a consistent scalar metadata field such as
`dreamlandPublicId = DL-7M2K-P9XR-W4TZ-C6HN` on session start/end, command traces,
Spiritguide traces, and enrichment observations, including asynchronously
scheduled work. Capture the correct visit identity when work is scheduled so
later switching cannot stamp another visit's ID on its result. Include public
and internal references in operator lifecycle logs where appropriate; preserve
configurable logging levels/event categories and existing secret exclusions.

Langfuse supports metadata filtering. Its
[metadata documentation](https://langfuse.com/docs/observability/features/metadata)
describes propagation and filtering; verify the installed SDK and deployed UI
with a controlled integration check. The proposed alphanumeric key avoids
depending on punctuation support in propagated metadata. Test propagation to
all relevant observations, not just the initial session event.

### Console Reference Resolution

Extend the native console's existing session selectors to accept complete public
IDs. Examples:

```text
sessions --live
spiritguide --session DL-7M2K-P9XR-W4TZ-C6HN add an engineering workstation
sessions --set DL-7M2K-P9XR-W4TZ-C6HN
spiritguide add an engineering workstation
trace DL-7M2K-P9XR-W4TZ-C6HN
```

Spiritguide options precede the instruction, matching the current parser.
Display the public reference in applicable native session listings and details.
Use one shared console resolver so session-targeted inspection, selection,
guidance, and administration interpret references consistently.

- Recognize the public namespace first. Normalize letter case, validate the
  complete canonical format, and require an exact lookup. No prefix/fuzzy
  matching or guessing of missing characters for public references.
- Public lookup works without first listing sessions. Malformed, unknown,
  incomplete, or expired public references fail explicitly; never fall through
  to internal-ID prefix matching, the active session, or the only live session.
- Preserve existing internal-ID console behavior after public-ID handling.
- Retained ended IDs can resolve for inspection. Spiritguide and other live
  mutations report that the visit has ended, without invoking a model or
  targeting another session.
- Recheck the intended live registration under the appropriate lock when
  executing/applying a mutation. Closing a visit while guidance is pending must
  not transfer the instruction or result to a new visit.
- `sessions --close PUBLIC_ID` must use the actual visit-close lifecycle above,
  not merely remove its registry entry. Test it while the visit is idle and
  busy; after closure no further commands may be admitted for that ID. Already
  running calls follow the cancellation and late-result rules above.
- Keep this alias resolution at the native console boundary. Do not change
  `LiveSessionRegistry.get()` or silently add public aliases to REST/MCP APIs.

### Existing Implementation Anchors

These observations were made while planning; verify them on the implementation
branch before changing code.

| Area | Existing anchor and implication |
| --- | --- |
| Startup/config | [`__main__.py`](../src/dreamland/__main__.py), [`config.py`](../src/dreamland/config.py): compose shared profile/runtime/console and one listener; add optional lobby composition here. |
| Authentication | [`ssh_auth.py`](../src/dreamland/ssh_auth.py), [`ssh_transport.py`](../src/dreamland/ssh_transport.py): preserve current shared-password and restricted-channel policy. |
| Lobby insertion | `DreamlandSSHServer._handle_client` in [`ssh_transport.py`](../src/dreamland/ssh_transport.py): after accepted interactive shell request, before constructing a simulated `ShellSession`; PTY admission needs explicit tracking. |
| World lifecycle | [`ssh_sessions.py`](../src/dreamland/ssh_sessions.py): `ShellSession` currently constructs/registers a runtime immediately and sends exit status itself; separate visit outcome from parent-channel closure. |
| Nested exit | [`ssh_session_state.py`](../src/dreamland/ssh_session_state.py): preserve stack-aware exit/logout/Ctrl-D behavior. |
| Per-visit profile | [`backend.py`](../src/dreamland/backend.py), [`ssh_types.py`](../src/dreamland/ssh_types.py): `create_session(..., state_profile=...)` already accepts explicit profiles. |
| Saved state | [`state_profiles.py`](../src/dreamland/state_profiles.py): existing store, deep-copy boot, session index, and preservation of runtime provenance on profile apply; audit new-boot identity stripping. |
| Console | [`console.py`](../src/dreamland/console.py): existing prefix resolver uses recently listed internal IDs and passes unknown strings through; public aliases need explicit exact handling. |
| Runtime registration | [`session_registry.py`](../src/dreamland/session_registry.py): unregister publishes `SESSION_CLOSING`; native operator close currently unregisters without itself stopping the shell loop, so wire lobby visit retirement explicitly. Do not broaden registry lookup into a public alias resolver. |
| Cleanup | [`background_agents/enrichment/service.py`](../src/dreamland/background_agents/enrichment/service.py): closure cancels scheduled work and guards late application; verify in-flight work and native checkpoint/resource cleanup. |
| Trace metadata | [`agent_tracing.py`](../src/dreamland/agent_tracing.py), [`spiritguide.py`](../src/dreamland/spiritguide.py), [`background_agents/enrichment/role_outputs.py`](../src/dreamland/background_agents/enrichment/role_outputs.py): separate metadata builders need consistent public-reference projection. |

Prefer a small lobby controller, catalog service, and trusted public-reference
resolver with focused responsibilities. Do not turn the shell emulator or
operator console into a general lobby command interpreter. Preserve backend/API
contracts unrelated to this feature.

### Mandatory Audit and Adversarial Assessment

Audit all deterministic surfaces: welcome, agreement, picker, start/end/failure
messages, unavailable states, timeouts, cancellation, public-ID handling, and
catalog/operator errors. Audit the implementation and state transitions as well
as the text. Guidelines are explanatory; authorization and isolation must be
enforced by code.

Use fixed states and narrow input grammars. Attendee lobby input must never be
sent to an LLM, evaluated as code, passed to a real shell, interpreted as a
template, routed to the operator console, or used as an arbitrary profile path.
Escape/reject terminal control content from dynamic fields and keep input,
rendering, retry, and waiting behavior bounded. Errors must not disclose
credentials, trace authority, private paths, or internal stack traces.

The assessment must include direct code review plus deliberate attacks with
assertions about forbidden effects, not merely assertions about displayed text:

| Attack or failure | Required result |
| --- | --- |
| Exec, no PTY, extra channel, subsystem/forwarding, or operator namespace attempt | No admission bypass, backend session, model call, or operator authority. |
| Prompt-injection prose, shell metacharacters, path traversal, or template syntax in lobby input | Treated as invalid bounded input; no interpretation outside the current input grammar. |
| CR/LF variants, split packets, multi-byte key sequences, multi-line paste, queued shell commands | Discard buffered tails and complete known partial control sequences; no replay of commands across visits. Later explicit inputs must satisfy each phase's grammar, without claiming to detect automated agreement. |
| Oversized input, malformed encoding, NUL/control characters, ANSI/OSC escape sequences | Bounded handling and safe terminal display; no crash, terminal manipulation, or secret disclosure. |
| Stalled connection, repeated invalid input, disconnect during each phase, shutdown | Finite idle handling, clean closure, no implicit world entry or leaked visit. |
| Hostile username, catalog title/description, or error interpolation | Cannot inject terminal control sequences or forge trusted controller state. |
| Concurrent catalog update/removal/reordering, missing/malformed profile, failed persistence | Stable selection or explicit retry; no accidental different/default world and no partial catalog publication. |
| Forced random-ID collision or simultaneous allocation | Retry safely without overwriting a binding. |
| Same username/world in simultaneous visits | Distinct public/internal identities and exact operator targeting. |
| Fake ID in model output, profile data, simulated files, or state patches | Cannot change authoritative ID/mapping or drive lifecycle transitions. |
| Unknown/truncated/expired ID; lookup before listing; restart | Exact correct resolution or explicit failure; no fallback or resurrection of a visit. |
| Visit closes while Spiritguide/model/enrichment work is pending | No mutation/output reaches another visit; late traces retain the originating ID. |
| Operator closes an idle or busy visit by public ID | Its shell stops accepting commands, pending work is cancelled or its late results discarded, its identity is retired, and the parent connection returns to the picker. Unregistering cannot leave an interactive shell behind. |
| Repeated world switching and nested exits | Fresh isolated visits, realistic nested behavior, no retained active editors/checkpoints/jobs causing unbounded growth. |
| Langfuse disabled, unavailable, or delayed | Local admission/reference/console behavior remains correct; metadata failures are observable. |
| Direct-mode regression | Existing private default-world and exec behavior remains compatible, with no lobby/public banners. |

Add an independent review after the implementer has exercised the cases. Record
tested revision, reviewer, surfaces reviewed, payload classes, expected and
observed results, findings, fixes, and retest evidence in a durable audit record
using the documentation schema. Resolve identified feature security defects
before release. Do not characterize deterministic copy alone or passing tests
as proof of absolute security; completion requires concrete review evidence.

### Development Phases and Manual Checkpoints

1. **Foundation, catalog, and identity.** Add default-off lobby configuration,
   plain-text content loading/versioning, validated persistent catalog controls,
   and public ID allocation/index/resolution. Test atomic updates, collisions,
   restart recovery, and unchanged direct defaults without provider calls.
   Manually add, edit, reorder, remove, and reload two disposable profiles while
   the process runs; restart to confirm persistence.
2. **Deterministic admission and world entry.** Implement PTY policy, welcome,
   agreement, picker, input limits, cancellation/idle behavior, and explicit
   per-visit profile boot. Manually authenticate, try invalid agreement/selection,
   select each world, and verify fresh login state and the Started ID. Attempt
   exec/non-PTY entry and verify no simulation is created.
3. **Return flow, console, and tracing.** Implement visit retirement and the
   picker loop; public-ID console targeting; complete lifecycle/model metadata.
   Manually enter nested simulated SSH, exit to its parent, then exit the first
   shell to the picker. Select the same world and another world; verify fresh
   state/new IDs. From the operator console, target a live visit by public ID,
   then verify the ended ID cannot mutate a subsequent visit. Search Langfuse
   for a controlled visit and verify command, Spiritguide, and enrichment
   correlation. Record behavior with tracing unavailable too.
4. **Audit, adversarial assessment, and regression.** Execute the matrix above,
   perform independent review, fix findings, and retain retest evidence. Run
   required repository checks and a manual terminal rehearsal of the final copy,
   reports workflow, hot curation, fresh visits, and direct-mode compatibility.

Prefer owning-layer `unittest` tests with fake channels/providers for catalog,
input/state transitions, session resolution, identity, tracing metadata, and
cleanup. Use real SSH/Paramiko tests where transport behavior is under test:
authentication, PTY/exec/channel policy, a complete lobby round trip, and reuse
of one parent channel across visits. Extend existing nested-shell tests rather
than replacing them with slow transport-only coverage. Relevant owning suites
include `test_config`, `test_main`, `test_console`, `test_state_profiles`,
`test_backend`, `test_ssh_auth`, `test_ssh_server`, `test_ssh_sessions`,
`test_ssh_session_state`, and the affected tracing/enrichment suites.

Follow [Testing](TESTING.md) for exact commands, required checks, and live-call
policy. Keep ordinary tests provider-free. A controlled live smoke check is for
actual model/trace integration; it must not become a broad evaluation campaign.
Any such check uses disposable operator-created activity, not published attendee
transcripts.

### Completion and Scope Boundaries

Completion requires implemented behavior, passing owning/transport regression
checks, manual checkpoint evidence, successful public-ID report-to-console/trace
lookup, and the completed audit record. The operator must be able to explain and
demonstrate the full flow without consulting internal backend IDs.

Update operations/configuration, architecture, state-profile guidance where
affected, testing, documentation navigation, and the concise README/roadmap as
appropriate. Before committing meaningful behavior changes, record the durable
decisions in `docs/decisions/` and link implementation commits when practical.
The record should cover parent/visit ownership, exact public-ID resolution,
trusted provenance, dynamic catalog semantics, and the audited lobby boundary.

This feature does not add another listener, user accounts, shared persistent
worlds, resume/save for attendees, attendee-driven world generation, remote
operator access, public trace access, public ID banners for direct sessions,
web UI work, or publication of attendee activity. It does not authorize a public
deployment or a changeset containing the existing evaluation artifacts.

No product clarification remains open. The operator's actual curated world list,
deployment paths, operational limits/retention values, and final copy wrapping
are implementation or deployment inputs. Choose and document sensible defaults
where appropriate rather than repeatedly reopening the settled scope.

## Maintenance

Keep this plan marked Draft while it describes future behavior. Track phase
completion and evidence honestly; do not mark checks complete on the strength
of this planning review. Update it if the owner changes scope, then migrate
implemented operational contracts to their owning active documents and retain
this plan as the implementation/evaluation handoff or mark it Historical.

Maintain the screen copy, configuration, public-reference contract, console
selector behavior, lifecycle rules, tests, and audit record together. In
particular, a future change to publication, data processors, shared state,
authentication, listener structure, or direct-entry output requires an explicit
scope revision rather than an inferred extension of this plan.
