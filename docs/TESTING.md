# Testing Strategy

Status: Active
Audience: maintainers and coding agents
Purpose: define how tests should be organized and run.

## Overview

The test suite currently uses `unittest`. The full suite passes locally but is
slowed mostly by real SSH transport tests. Future cleanup should make fast unit
tests the default feedback loop and keep socket/Paramiko coverage in a smaller
integration layer.

Test counts and timings change frequently and are intentionally not fixed here.
Use the full-suite command below as the current baseline and retain its output
with release or conference-readiness evidence. Real Paramiko transport and
admin API tests are ordinarily the slowest groups.

Research campaigns, study datasets, and campaign-specific scripts are
published separately. Generic Inspect AI and JSON annotation tooling remain
available; see [Inspect AI Harness](INSPECT_AI_HARNESS.md) and
[JSON Annotations](EMULATION_JSON_ANNOTATIONS.md).

## Initial-login banner checkpoints

Owning tests cover schema-2 load/save rejection for missing or invalid host
references and banners, preservation through profile serialization and
Spiritguide updates, detached creation failure before saving, and explicit
silence. Command-emulator and enrichment context tests prove banner exclusion
without mutating authoritative state; Spiritguide receives the complete field.
Shell/lobby session tests exercise first output without a model invocation,
while existing transport tests cover the neutral SSH version. Exec output
remains command output only. Provenance tests verify current package hashes and
the archived-source versus runnable-derivative distinction.

For manual terminal/model verification:

1. Start from valid schema-2 profiles. Enter each bundled lobby world and inspect
   its saved initial banner before typing a command. Morrowbasket must identify
   Debian 12; Common Ground must present its public civic terminal; Leah's laptop
   must identify its established Ubuntu 24.04 environment. Check the immediate
   direct-shell entry path too.
2. Inspect a fresh trace: entry display must add no model call or command-history
   row. Submit an ordinary command and run a manual enrichment job; neither role's
   model input should contain the stored `login_banner` field or its unique text.
   Authoritative operator state and saved JSON must retain it.
3. Create a detached world with Spiritguide, inspect the saved entry/active host
   references and banners, then enter it. Try a malformed banner in an offline
   copy and confirm deterministic rejection before use. Never overwrite the
   original evaluation evidence to make a runnable copy.
4. Through simulated SSH, leave and return to the original host. Inspect the
   command trace and verify the LLM handles this later greeting. Start a fresh
   lobby visit and verify its first entry still uses the saved banner. Check an
   SSH exec request for command output without an initial-login greeting.

These are manual checkpoints, not claims of live model or human-client results.
Record completed checks separately from deterministic automated validation.

## Deployment Dependency Lock Updates

The [runtime lock](../deploy/requirements-runtime.txt) pins the complete
`dreamland[admin]` dependency closure for CPython 3.12 on Linux/amd64. The separate
[build lock](../deploy/requirements-build.txt) pins the wheel builder. Every
entry records a compatible wheel hash; Docker installs with `--require-hashes`
and `--only-binary=:all:`. Other Python/platform targets require their own review.

For a deliberate dependency update:

1. Resolve the reviewed `pyproject.toml` requirements in a clean CPython 3.12
   Linux/amd64 environment. Keep workstation/dev extras out of the runtime
   closure and review direct and transitive version changes.
2. Download the selected versions as wheels and calculate their SHA-256 hashes
   with `python -m pip hash WHEEL_PATH`. Update exact version/hash entries in the
   appropriate lock. Do not retain an old artifact's hash for a changed wheel.
3. Review the pinned Python base-image digest in the Dockerfile alongside the
   lock changes. Keep it pinned; do not replace it with a floating tag.
4. Install from the locks in a clean environment and run `python -m pip check`,
   the owning deployment tests, package-resource checks, and the clean Docker
   build. A missing target wheel is a failed update, not permission to bypass
   hashes or install an unreviewed source build.
5. Rehearse container startup and record the tested image identity and results
   separately from local checks before adopting the updated deployment.

The current owning test command is:

```sh
.venv/bin/python -m unittest tests.test_deployment_storage \
  tests.test_deployment_configuration
```

## Test Groups

`test_ssh_auth`, `test_config`, and `test_main` cover separate optional lobby
demo credentials, exact aliases, disabled admission, password separation,
secret redaction, startup scoping, and emulator-only tier composition.
`test_ssh_lobby_fast` exercises simultaneous normal/demo commands, two demo
world visits, catalog-owned identities, requested-tier logs, fail-closed missing
backend behavior, and release through the selected agent. Presentation and
loopback SSH tests cover all four login variants without provider calls. The
manual activation/tier checkpoint is in
[decision 0125](decisions/0125-demo-ssh-lobby-tier.md); automated tests do not
claim live provider delivery, billing, or human client results.

The deployment foundation has provider-free owning suites
`test_deployment_storage` and `test_deployment_configuration`. They cover
create-once seeding and crash recovery, preservation of operator edits/removals,
private file access, redacted failures, backend-only deployment policy, rejection
of legacy combined UI/backend intent, and the daemon's credential/environment
handoff. Fast-password checks cover hidden setup/rotation, preservation of
other credentials, confirmation and collision rejection, unsafe-file refusal,
restart handoff, and authentication of both fast aliases. These checks do not
prove Docker networking,
container startup, provider access, or conference capacity. Record those results
separately from the automated test results.

The detachable operator console adds `test_console_socket` and
`test_console_client`: private path/peer checks, bounded framing and admission,
command context/completion/follow/refresh, uncertain outcomes after disconnect,
shutdown policy, cleanup, and fresh reconnect context. `test_console_ssh_integration`
uses actual localhost Paramiko only to prove attendee survival through operator
detach/reconnect. It runs a static agent and makes no provider calls. Local socket
permissions must allow these transport tests; sandbox denial is not a product
failure. Docker execution and human terminal behavior are separate checkpoints.

`test_model_defaults` compares shared controls with dataclass/parser/example
settings. `test_deployment_model_defaults` checks fresh local/deployment agreement,
preview, exact backup/adoption, unrelated setting preservation, crash recovery,
and redacted failures. `test_deployment_control` uses a fake Docker executable
to validate client-only targeting, project listing, explicit/automatic project
selection, rejected ambiguity, and the stopped-container guard; it does not prove
Compose behavior.
Retain configuration, main, console, agent, Spiritguide,
and enrichment owning regressions when changing model controls or daemon wiring.
See [Docker Operator Console](OPERATIONS.md#docker-operator-console).

The [private API foundation](decisions/0109-private-api-foundation.md) adds
`test_deployment_companion`, `test_deployment_companion_cli`,
`test_deployment_companion_client`, `test_deployment_companion_http`, and
`test_deployment_api_network`. They cover private role storage/exports, strict
contract fixtures, metadata identity/readiness, bounded HTTP/client lifecycle,
cross-role denial, and Docker network ownership checks. Actual installed-image
and separate-Compose connectivity remain a distinct recorded checkpoint.

The former UI review kit's semantic cases now live in the owning suites.
`test_deployment_companion` checks exact descriptors, identity/health shapes,
readiness booleans/reason/capabilities, and bounded error envelopes.
`test_deployment_companion_client` checks both roles, exact authenticated request
sequences, role and epoch agreement, redirects, and missing/wrong-role denial.
These checks use synthetic local state and require no UI, provider, or external
network. Canonical artifact and fixture integrity remains covered by
`test_deployment_companion`.

The optional native SSH lobby adds provider-free owning suites
`test_lobby_config`, `test_lobby_content`, `test_lobby_catalog`,
`test_lobby_identity`, `test_lobby_input`, `test_lobby_presentation`, `test_ssh_lobby`,
`test_ssh_lobby_audit`, and `test_ssh_lobby_sessions`, plus
existing config, console, state-profile, backend, tracing, enrichment, and SSH
regressions. Catalog checks include atomic failed writes, concurrent updates,
stale display numbers, malformed/oversized/control text, profile recovery, and
nonblocking rejection of FIFO/device files. Identity checks cover collision
retry, restart recovery, expiry, and exact resolution. Admission and shell
tests assert forbidden effects: no simulation before agreement/selection,
no replay of buffered commands, no cross-visit mutation/output, and bounded
resource cleanup.

Lobby entry-account tests belong in the existing catalog, console, controller,
session, and SSH authentication suites. Cover version-2 catalog persistence,
required/invalid persisted `entry_username`, omitted console/add account
resolution from saved login fields, entry-host defaults, and bundled structured facts,
explicit override precedence, missing/ambiguous/invalid inference rejection,
preservation of resolved accounts after source edits, explicit version-1
migration rejection, unchanged publication after invalid updates, console add/update/list behavior,
and profile initialization using the configured account. Verify current/login
usernames, initial shell frames, username templates, environment values, and
custom absolute home directories. Different SSH visitors must start the same
world with the same configured account; one connection switching worlds or
reentering a world must use each fresh selection's account. A changed catalog
account must not rewrite an active session.

The starter-catalog check reads the three world baselines through
`importlib.resources`, checks their provenance hashes, materializes local
copies, and verifies each catalog entry's account and host. It must work
without reconstructing profiles from evaluation archives. When changing
packaging, additionally build a wheel and verify its installed resources and
fresh local catalog initialization outside the source checkout.

Authentication checks preserve the exact successful SSH name, including
punctuation and Unicode, in escaped lobby lifecycle records. They must cover
control-character/log-line injection, failed-authentication exclusion, parent
connection correlation across multiple public/internal visit IDs, and absence
of the raw name from simulated state and model-facing context. Retain direct
shell/exec and API/MCP identity regressions. Use provider-free collaborators for
this matrix; real SSH is required only for transport/authentication behavior.
The entry-account walkthrough in
[Manual Operator Checkpoints](SSH_LOBBY.md#manual-operator-checkpoints) remains
human verification until actual results are recorded.

Simple-login checks belong in `test_ssh_auth`, `test_main`,
`test_ssh_server`, `test_ssh_lobby_intro`, and `test_ssh_lobby_transport`.
Verify the exact `_simple` alias requires the allowed base username and the same
password, rejects repeated/malformed aliases, and preserves a configured name
already ending in the suffix. Startup must enable aliases only for lobby mode;
direct-entry usernames stay literal. Retain per-attempt delays and escaped raw
audit identities. Exercise agreement, world entry/exit, and refresh without
animation or color controls; verify ordinary connections sharing the controller
keep their configured presentation. The real SSH check verifies alias admission
and both presentation variants on one listener. The human checkpoint in
[decision 0124](decisions/0124-simple-ssh-lobby-login.md) remains separate from
these deterministic checks.

`test_lobby_presentation` owns bounded ASCII TERM normalization, the exact
capability allowlist, separate monochrome capabilities, independent auto/off
switches, allowed renderer controls, per-fragment style resets, and the 20x5
presentation cutoff. `test_lobby_config` owns settings defaults, normalization,
INI/environment precedence, and explicit invalid-value rejection.
`test_ssh_lobby_banner` owns the fixed ASCII dimensions and spacing, exact-fit
and one-column/one-row-below-fit decisions, operator `auto`/`simple` selection,
color-only and clearing-only artwork, plain/minimal fallback, leading-wordmark
handling, custom short/long notice preservation, initial-page-only artwork,
and tiny/resize row budgets. Full art must fit all welcome prose and its action
on one page; automatic selection must not add welcome pagination.
`test_ssh_lobby` uses deterministic channels to check styled/plain layout
equivalence, complete content through paging and boundary resizes, minimal
linear output below 20x5, visible error/status content after clears, pinned
last-ended IDs with tiny-screen actions, and clean shell/exit styling. Retain
the existing fresh-visit and buffered-input boundary checks.

`test_lobby_intro` uses an injected monotonic clock and timed channel for exact
scheduling, twenty-frame/16-KiB limits, horizontal reveal progression, centered
nine-row artwork, slow-write frame dropping, the strict display-clear,
numeric cursor-position, and basic-color control set, clear-before/after and
cursor/color cleanup, capability/size gating, clearing-off fallback,
monochrome motion, skip/paste/partial tails, resize,
shutdown, disconnect, idle expiry, and output failures. `test_lobby_input`
owns deadline polling, timeout restoration, finite boundary drains, and
explicit idle resumption after a visit. `test_ssh_lobby_intro` owns once per
connection placement, prompt sequencing, no visit before valid selection,
no late frames, and idle resumption at the actual visit boundary. Keep normal
transport/layout fixtures motion-disabled; explicit motion walkthroughs live
in the [Phase 3 checkpoint](SSH_LOBBY_PRESENTATION_PHASE3_CHECKPOINT.md). The operator
accepted Phase 3 on 2026-09-11 with "perfect, commit and continue". Runtime
and preview now default to `intro_animation=auto`; explicit fixture overrides
keep layout/transport suites focused while dedicated intro checks own motion.

Reserve actual Paramiko tests for PTY/exec/channel admission, TERM and
window-change dispatch, and complete single-connection lobby round trips
(`test_ssh_lobby_transport` and the existing
`test_ssh_server`). Deterministic fake channels/providers own
grammar, lifecycle, cancellation, profile selection, and trace metadata checks.
See [Native SSH Lobby](SSH_LOBBY.md#manual-operator-checkpoints) for manual
curation and fresh-visit checkpoints. Release also requires the direct audit
and independent adversarial review recorded in the
[development plan](SSH_LOBBY_DEVELOPMENT_PLAN.md); an actual deployed model/
Langfuse smoke is controlled operator activity, not an attendee transcript
publication or an evaluation campaign.

The completed implementation evidence is in the
[independent audit](SSH_LOBBY_SECURITY_AUDIT.md),
[OpenSSH rehearsal](SSH_LOBBY_REHEARSAL.md), and
[live tracing check](SSH_LOBBY_TRACE_CHECK.md).

The [Phase 1 presentation checkpoint](SSH_LOBBY_PRESENTATION_CHECKPOINT.md)
records accepted implementation commit
`4e29ff567bd7df56450cf05f9032c638a4e6f657`, automated evidence, the isolated
provider-free SSH preview, and the human client/settings matrix. The operator
accepted that phase on 2026-09-11 and directed "Commit and continue"; individual
client/version results were not supplied and remain unverified.
Automated protocol/layout checks cannot establish human readability or client
rendering. The [Phase 2 checkpoint](SSH_LOBBY_PRESENTATION_PHASE2_CHECKPOINT.md)
records the accepted static banner and continuation wording, exact
short-notice fit cases, and current-copy checks. On 2026-09-11 the operator
accepted Phase 2 with "fab. commit and continue", authorizing its commit and
Phase 3 work. Individual client/version results remain unverified.
Record actual TERM, dimensions, font/zoom, theme, ANSI-color settings, and any
multiplexer or tunnel for each tested client, and leave unavailable client
results pending.

Recommended ownership:

- Agent state codecs: `tests/test_agent_state.py`
- Agent shell helpers and built-ins: `tests/test_agent_shell.py` and backend
  tests near built-in behavior.
- Agent API compatibility: `tests/test_agent_api.py`
- Structured failure capture and parser replay:
  `tests/test_structured_failures.py`
- Post-run emulator JSON classification, annotation transport, and CLI:
  `tests/test_evaluation_json.py`, `tests/test_evaluation_annotations.py`, and
  `tests/test_emulation_json_annotations.py`. Use captured fixtures and fake
  transports; these tests require no live model or Langfuse write.
- Backend state and LangGraph behavior: `tests/test_backend.py`
- Config parsing: `tests/test_config.py`
- MCP evaluation configuration and isolated runtime ownership:
  `tests/test_evaluation.py`, with actual MCP/REST transport coverage in
  `tests/test_admin_api.py`. These tests use deterministic provider substitutes;
  the live restart checkpoint is in `docs/MCP_MODEL_EVALUATION.md`.
- Console and Langfuse introspection: `tests/test_console.py`
- Admin service and MCP bindings, including headless sessions, parameterized
  inspection tools, tag grouping, and MCP-session cleanup:
  `tests/test_admin_interface.py` and `tests/test_admin_api.py`
- Private product configuration/composition, protocol-neutral lifecycle and
  shared core, preview transports, and release-candidate artifacts:
  `tests/test_config.py`,
  `tests/test_main.py`, `tests/test_product_contract.py`,
  `tests/test_product_lifecycle.py`, `tests/test_product_lifecycle_contract.py`,
  `tests/test_product_lifecycle_preview.py`, `tests/test_product_commands.py`,
  `tests/test_product_admission.py`, `tests/test_product_journal.py`,
  `tests/test_product_execution.py`, `tests/test_product_core.py`,
  `tests/test_product_core_enrichment.py`, `tests/test_product_enrichment.py`,
  `tests/test_product_runtime_executor.py`,
  `tests/test_product_session_contract.py`, and
  `tests/test_product_core_preview.py`, plus
  `tests/test_product_live_agent.py`, `tests/test_product_stage_live.py`, and
  `tests/test_product_stage_live_contract.py`, plus
  `tests/test_product_forge.py`, `tests/test_product_forge_contract.py`, and
  `tests/test_product_forge_preview.py` for the inert WorldSeed/lifecycle,
  separate contract kit, secure loopback transport, and recovery scenarios.
  `tests/test_product_forge_artifacts.py`,
  `tests/test_product_forge_runtime.py`, `tests/test_product_forge_stage.py`,
  `tests/test_product_forge_stage_contract.py`, and
  `tests/test_product_forge_stage_live.py`, plus
  `tests/test_product_xray_contract.py`, own immutable Forge artifact loading,
  detached Forge runtime hydration, selected-record-to-Stage coordination,
  the additive private contracts, the display-safe dual-position X-ray, and the
  combined two-listener development target.
  `tests/test_product_authored_artifacts.py` owns strict configured authored
  profile import, digest pinning, metadata-free canonical snapshots,
  owner-only create-only storage, corruption rejection, and detached profile
  reconstruction. `tests/test_product_launch_selection.py` owns the bounded
  source-neutral option registry, closed origins, safe projections, actor/role/
  epoch/grant authority, idempotency, capacity, expiry, and concurrency.
  `tests/test_product_launch_selection_stage.py` owns registry-to-dynamic-Stage
  coordination, path-free projections, create/switch behavior, shared epoch,
  and retryable provisioning capacity.
  `tests/test_product_launch_selection_contract.py` and
  `tests/test_product_launch_selection_preview.py` own the additive Phase 7A
  schemas, fixtures, generated client, provider-free private transport, and
  dynamic Stage create/switch integration.
  `tests/test_product_launch_selection_runtime.py` and
  `tests/test_product_launch_selection_forge.py` own strict authored/Forge
  runtime dispatch, origin retention, authoritative Forge registration, actor
  binding, expiry, replay, and shared display truth.
  `tests/test_product_launch_selection_live_contract.py` and
  `tests/test_product_launch_selection_live.py` own the Phase 7B live contract
  identity, inert composition, pinned startup, limits, safe announcement, and
  combined provider-free collaborator flow. `tests/test_product_attendee.py`,
  `tests/test_product_attendee_contract.py`, and
  `tests/test_product_attendee_preview.py` own the Phase 7C authority,
  contract, and deterministic private transport.
  `tests/test_product_attendee_live_contract.py` and
  `tests/test_product_attendee_live.py` own the Phase 7D identity-only live
  contract, combined composition, tracing isolation, cutoff, capacity, safe
  announcement, derived Presenter/attendee token accounting, and injected
  provider-free collaborator flow. `tests/test_product_stage_live.py` owns the
  shared finite provider-maximum and reservation derivation used by every live
  Stage-bearing target.
  `tests/test_product_attendee_security_contract.py` and
  `tests/test_product_attendee_security_preview.py`, together with the owning
  security cases in `tests/test_product_attendee.py`, own Phase 9A context
  revocation, replay-before-rate admission, attendee-only private routes,
  capability-disabled RC identity, startup redaction, and provider-free
  transport behavior. `tests/test_product_attendee_live_boundary.py`,
  `tests/test_product_attendee_provider.py`,
  `tests/test_product_attendee_security_live_contract.py`,
  `tests/test_product_attendee_security_live_identity_contract.py`, and
  `tests/test_product_attendee_security_live.py` own Phase 9B exact-authored
  worker confinement, child-only credential loading, environment and tracing
  isolation, zero provider retries, spawn-process readiness, finite IPC,
  cancellation and forced replacement, conservative reservation, drain and
  shutdown, the identity-only RC, and attendee-only live composition.
  `tests/test_product_generated_access.py` and
  `tests/test_product_attendee_catalog.py` own the Phase 8A hidden/reveal
  aggregate, reversible publication, catalog eligibility, internal proof, and
  generic authored/generated exchange. `tests/test_product_world_access_contract.py`
  and `tests/test_product_world_access_preview.py` own the separate RC3
  artifact, exact actor/route/media gates, compiled server-only client,
  cross-role denial, generic fixtures, launch-binding correlation, coherent
  notice/evaluator time, and provider-free loopback sequence.
  `tests/test_product_stage.py` owns
  the X-ray allowlist, bounds, coordinate advancement, stale-read denial,
  hostile text marking, cleanup, terminal failure isolation, and source-neutral
  dynamic Stage provisioning at the service layer.
  The loopback tools are
  integration-only and do not replace owning-layer tests or make a production
  capability live. Preview transport coverage includes raw malformed methods
  and parser input, stalled and over-capacity connections, owner-only token-file
  loading, concurrent expiry reads, and resource-warning cleanup.
- State profiles and boot state: `tests/test_state_profiles.py`
- Command-emulator host projection: `tests/test_emulator_context.py`, with
  backend prompt/pivot integration coverage in `tests/test_backend.py`.
- Active SSH connection lifecycle, host projection, and runtime snapshots:
  `tests/test_network_connections.py`, `tests/test_network_identity.py`,
  `tests/test_connection_translation.py`, `tests/test_connection_profiles.py`,
  and `tests/test_connection_backend.py`, with incremental LangGraph metadata and
  JSON projection coverage in `tests/test_connection_context.py`. Use fake
  command providers for authentication, pivot, cancellation, and port continuity;
  real transport is needed only for native entry/lobby wiring.
  `tests/test_connection_tracing.py` owns scoped simulated/transport trace
  metadata and exclusion of raw ingress provenance from model context.
- Provider construction and outbound OpenAI cache serialization:
  `tests/test_model_providers.py`, with command-emulator cache-boundary coverage
  in `tests/test_backend.py` and Spiritguide exclusion coverage in
  `tests/test_spiritguide.py`. Provider-free evaluation-control cases exercise
  the installed OpenAI serializer, explicit endpoint/tier/mode settings, Astra
  cache fields, and isolation from Anthropic/xAI adapters. `tests/test_config.py`
  covers the independent six-role INI/environment controls; enrichment and
  live Stage owning tests cover forwarding. Successful serialization does not
  establish live model access or successful provider execution.
- World-state helper behavior: `tests/test_world_state.py`
- Prompt loading: `tests/test_prompts.py`
- Runtime logging and token callbacks: `tests/test_runtime_logging.py`
- Enrichment defaults, graph contracts, safe application, scheduling, and
  lifecycle behavior: `tests/test_config.py`, `tests/test_enrichment_graph.py`,
  and `tests/test_enrichment_service.py`.
- Native shared-password authentication policy: `tests/test_ssh_auth.py`
- Persistent SSH host-key lifecycle, permissions, fingerprint, and concurrent
  first-run publication: `tests/test_ssh_host_keys.py`
- SSH session loops: `tests/test_ssh_sessions.py`
- SSH session state transitions: `tests/test_ssh_session_state.py`
- SSH streaming helpers: `tests/test_ssh_streaming.py`
- Terminal editors: `tests/test_terminal.py`
- Interactive blank input, program phase normalization, checkpoint history,
  nested-frame preservation, and shell read handling:
  `tests/test_interactive_input.py`; MCP admission is covered by
  `tests/test_admin_interface.py`. Foreground SSH artifact protection belongs
  to `tests/test_enrichment_authentication.py`, `tests/test_enrichment_patches.py`,
  and `tests/test_enrichment_service.py`.
- SSH transport integration, including known-host trust across restart and
  deliberate-rotation rejection: `tests/test_ssh_server.py`
- Incoming SSH handshake classification, real TCP probes, per-connection noise
  suppression, cleanup, and concurrent unexpected-error visibility:
  `tests/test_ssh_handshake.py`
- Live provider smoke tests: `tests/test_live_agent.py`

## Interactive Input Checkpoints

Use fresh disposable sessions with the model and enrichment settings recorded.
The configured scenario must require a password for sudo retry testing.

1. Press Enter repeatedly at an ordinary Bash prompt. Verify no model calls or
   history entries. Start ssh-keygen; accept the default filename and empty
   passphrase using Enter. Check one key pair is created and completion restores
   the original shell. Filename/passphrase/confirmation phases keep depth two.
2. Retry sudo with three incorrect passwords. Verify one return to the parent
   after failure. In a fresh invocation, use the scenario's correct password and
   check a root shell replaces the authentication frame and exits once.
3. Generate a key with a custom filename and passphrase. Verify filename/overwrite
   answers echo, passphrases do not, prompts occur once, and Ctrl-C at an idle
   prompt returns to the parent. Verify Bash Ctrl-C still only clears its line.
   Run the terminal tests for CRLF packet splits and worker-cleanup cancellation;
   do not infer those transport properties from a headless model run.
4. Complete a Python multiline statement with an empty line. Exercise a shell
   read with an empty answer and with `exit` as data, then confirm ordinary shell
   blanks become no-ops again.
5. With enrichment enabled, create .ssh and collect a host key into known_hosts.
   Inspect subsequent focused jobs: the active user's protected client config
   and key paths must not gain fabricated setup or passphrase requirements.
   Unrelated historical world-building and foreground key generation remain
   eligible. Check `foreground_authentication_conflict` for rejected clusters.

## Active SSH Connection Checkpoints

Use a disposable simulated session and record the observed endpoints at each
step. These are manual model-rendering checks alongside deterministic runtime
tests; they are not an emulation evaluation campaign.

1. Connect through a real loopback listener on a nonstandard deployment port.
   On host A, run `ss -nat` and inspect `SSH_CONNECTION` and `SSH_CLIENT`.
   The initial simulated local endpoint must be A's primary address on port 22.
   Repeat inspection and verify the client source port remains unchanged.
2. SSH from A to B, then B to C. On each host inspect `ss -nat` and the SSH
   environment. C sees B as its login peer, B retains A's incoming connection
   and its outgoing connection to C, and A retains its entry connection and
   connection to B. Inspect a parent through a supported second shell, or check
   its projected runtime state while the nested login remains active. Exit one
   level at a time and verify only that hop disappears. Repeat using Ctrl-D.
3. Exercise host-key/password prompts, failed authentication, and Ctrl-C during
   authentication. Retries retain a source port; failure/cancellation removes
   that connection. Successful authentication preserves the same endpoint pair.
   A client awaiting authentication remains on the originating host.
4. Exercise an explicit destination port, a host alias, `ssh localhost`, an IPv6
   target, and a local subshell or `sudo` context. Only SSH creates a connection;
   loopback exposes both local endpoints. Compare `ss -lnt`, `ss -4nat`, and
   `ss -6nat` to verify listeners, established rows, and address-family filters.
   Unset or assign SSH environment variables and confirm sockets stay unchanged.
5. Save/restore an exact snapshot with a nested login and verify stable nested
   IDs/ports with a reconciled current entry connection. Check successive product
   commands across runtime reconstruction. Start fresh world/system sessions
   and consecutive lobby visits and verify old sockets do not return. Load a
   legacy profile and create a headless session without connection metadata;
   neither should invent an external SSH login.

Runtime socket state is authoritative; generated table text remains an LLM
rendering boundary. Record any disagreement with its command context and raw
response. Passing provider-free tests does not establish live rendering quality.

## Whole Command Routing Checkpoint

The owning provider-free suites are `test_command_routing`,
`test_builtin_plan`, `test_builtin_routing`, `test_command_state_effects`,
`test_compound_command_consumers`, and existing backend, graph, shell-state,
network, admin, and product-runtime tests. Assert model invocation counts and
the exact model input, not just plausible terminal output. A discarded builtin
preflight must leave no live state, history, events, or network changes.

For an opt-in manual check using a fresh session on the changed runtime:

1. Run `cd /tmp ; pwd`. Expect `/tmp` and no command-model generation.
2. Run `id; w`, then `cd /tmp; pwd ; touch routing-check`. Each line should
   produce one command-model generation and one aggregate command history row.
   Inspect the actual model input to confirm unchanged separators and quoting.
3. Run `export ROUTING_DEST=/tmp; cd "$ROUTING_DEST"; pwd`. Expect local state
   sequencing and no command-model generation. Try an unestablished `cd` target
   in a chain and inspect a single whole-line model call from the original cwd.
4. With an established reachable simulated SSH destination, run
   `cd /tmp; ssh destination`, then `exit`. Confirm the parent prompt and `pwd`
   return to `/tmp`, and the parent's connection identity remains stable.
   Run `export ROUTING_CHECK=kept; bash`, then `exit` and
   `printenv ROUTING_CHECK`; expect `kept` in the parent.
5. Interrupt a model-backed mixed line. Confirm one interrupted command record,
   no accepted prefix/late state changes, and correct state on the next command.

Record model, runtime revision, command traces, output, and resulting state.
Foreground call counts exclude independent enrichment and provider retries.
Provider-free tests establish routing/state invariants; live model adherence
to ordered effects requires this separate manual checkpoint.

## Cleanup Direction

Tests should be split by the layer under test:

- Keep real Paramiko tests only for authentication negotiation, denied auth
  methods, subsystem/forwarding/channel restrictions, exec and shell channel
  wiring, SSH operator-namespace rejection, and a small number of end-to-end
  smoke paths.
- Move shell-stack, Ctrl-D, editor rebuild, line buffering, environment, and
  nested program behavior into direct unit tests where possible.
- Avoid testing the same behavior through both direct unit tests and real SSH
  unless transport behavior is the point.
- Replace fixed sleeps with synchronization primitives or bounded polling where
  practical.
- Keep live provider tests opt-in and well documented.

## Commands

Run all tests:

```sh
.venv/bin/python -m unittest
```

Run focused groups:

```sh
.venv/bin/python -m unittest tests.test_backend
.venv/bin/python -m unittest tests.test_console
.venv/bin/python -m unittest tests.test_config tests.test_enrichment_graph tests.test_enrichment_service
.venv/bin/python -m unittest tests.test_config tests.test_main tests.test_product_contract
.venv/bin/python -m unittest tests.test_product_lifecycle tests.test_product_lifecycle_contract tests.test_product_lifecycle_preview
.venv/bin/python -m unittest tests.test_product_commands tests.test_product_admission tests.test_product_journal tests.test_product_execution
.venv/bin/python -m unittest tests.test_product_core tests.test_product_core_enrichment tests.test_product_enrichment tests.test_product_runtime_executor
.venv/bin/python -m unittest tests.test_product_session_contract tests.test_product_core_preview
.venv/bin/python -m unittest tests.test_product_stage tests.test_product_stage_contract tests.test_product_stage_preview
.venv/bin/python -m unittest tests.test_product_live_agent tests.test_product_stage_live_contract tests.test_product_stage_live
.venv/bin/python -m unittest tests.test_product_forge
.venv/bin/python -m unittest tests.test_product_forge_contract tests.test_product_forge_preview
.venv/bin/python -m unittest tests.test_product_forge_artifacts tests.test_product_forge_live
.venv/bin/python -m unittest tests.test_product_forge_runtime tests.test_product_forge_stage tests.test_product_stage
.venv/bin/python -m unittest tests.test_product_forge_stage_contract tests.test_product_xray_contract tests.test_product_forge_stage_live
.venv/bin/python -m unittest \
  tests.test_product_authored_artifacts tests.test_product_launch_selection \
  tests.test_product_launch_selection_stage tests.test_product_stage
.venv/bin/python -m unittest \
  tests.test_product_launch_selection_contract \
  tests.test_product_launch_selection_preview
.venv/bin/python -m unittest \
  tests.test_product_launch_selection_runtime \
  tests.test_product_launch_selection_forge \
  tests.test_product_launch_selection_live_contract \
  tests.test_product_launch_selection_live
.venv/bin/python -m unittest tests.test_ssh_auth
.venv/bin/python -m unittest tests.test_ssh_server
```

The live Stage group is provider-free by default. It verifies positive and
unlimited accepted-command limits, command 13 in unlimited mode, sentinel
omission from the API limits map, unchanged independent safeguards, and the
non-secret startup configuration projection. Only the explicitly selected
live test below spends a provider call.

The same provider-free group verifies line-aware output coalescing, identical
transcripts across provider segmentations, more than 128 source/publication
pieces below the byte ceiling, stdout/stderr order, UTF-8-safe clipping, and
truthful exact-boundary completion.

The World Forge preview group is provider-free. It verifies prepare-before-
confirm, exact-value retention, server-time confirmation, idempotent submission
and cancellation, lost responses, withheld events, opaque cursor resync,
restart loss, capacity replay, safe terminal messages, private file handling,
request/media/authority boundaries, and synchronous one-shot completion.

The live vertical-slice group is also provider-free by default. Injected
generators verify exact instruction delivery, independent validation, synthetic
boot, owner-only no-follow immutable storage, one-attempt selection, safe
failure, both deadline race orders, cancellation fences, invariant operation
sources, malformed referenced-host rejection, runtime-faithful receipts, and
the RC4 loopback flow. It does not
contact a provider unless an operator explicitly starts the live tool and
submits a build.

The Forge-to-Stage groups are provider-free by default. They verify that only
an authoritative live selection mints a presenter-owned record; preview,
non-selected, malformed, expired, wrong-authority, stale-epoch, cancelled, and
late outcomes fail before Stage allocation. Artifact tests exercise exact
revision/digest lookup, owner-only no-follow reads, canonical newline-inclusive
hashing, and corruption rejection. Runtime and coordinator tests cover detached
hydration, exact Forge-origin retention, dynamic-only provisioning, identical
selection resume, changed-body conflict, explicit provision-before-cutover
replacement, Stage source identity, TTL capping, and unchanged normal/static
Stage behavior. The combined HTTP test uses injected provider-free Forge and
command collaborators despite exercising both loopback listeners.

Phase 5.1 now extends those owning groups before explicit replacement is a UI
checkpoint. Successful replacement must return the old session as a coherent
terminal tombstone: no controls, a terminal command slot with no active command
ID, bounded tombstone recovery, accepted-CAS-plus-one control revision, and an
event position that includes `session.terminal`. Tests must also prove old
authority revocation, independent coordinates for the new session, exact
old/new Forge source preservation, and exact same-key/body replay without
another Stage allocation. Contract and generated-client mutation smoke must
reject a terminal lifecycle paired with active controls, a nonterminal slot,
an active command ID, or active/unbounded recovery. Request-relative CAS and
event-history truth belong to operation-aware/runtime tests rather than a
response-only schema.

The Phase 7A launch-selection groups are provider-free. They verify strict,
one-read configured authored-profile loading; required digest pins before
artifact mutation; owner/mode/no-follow/canonical artifact checks; atomic
concurrent publication and interrupted-writer residue; detached allocation;
closed authored/Forge origin validation; safe option projections without
profile names or paths; non-authorizing offering and selection IDs; Presenter
actor, role, epoch, operation, expiry, and revision-size compatibility; grant
scope and expiry under forward/backward wall-clock jumps; deterministic
unavailable-result replay; changed-body conflict; unclaimed retryable capacity;
old-epoch uncertainty; replay after both grant and offering expiry; capacity
candidate-serial burn without credential reuse; monotonic grant identity beyond
the active-grant cap; reentrant and concurrent hard-cap enforcement; the
128-option wire bound; consistent per-revision display metadata; expired
dynamic grant, revision, and source cleanup after dependent tombstone retention;
and atomic dynamic Stage provisioning for create and explicit switch. Contract
and loopback tests cover strict headers and bodies, safe errors, mixed and empty
options, generated-client validation, response-loss recovery, malformed input,
a selected-world terminal command, and unchanged all-disabled capability
evidence. These tests construct no provider. Together with the existing
main-entry composition tests, they retain proof that normal startup is free of
Phase 7A imports, objects, listeners, reservations, and bookkeeping.

The Phase 7B live-composition group is provider-free unless a human explicitly
starts the documented serve command. It verifies authored and Forge
decode/mutate/encode round trips; unknown, unverified, and cross-source
rejection; no ordinary profile-store mutation; exact authored pinning before
provider/listener construction; selected-only Forge registration through exact
artifact reopening; actor matching; stable replay; selected-record expiry;
generic/legacy display consistency; observer-failure isolation; one shared
actor expiry, Stage epoch, and monotonic clock; raw-UTC plus monotonic option/
grant enforcement under forward and compound wall adjustments; positive and
unlimited command limits; unchanged finite TTL, concurrency, output, attempt,
token, spend, deadline, and cancellation safeguards; secret-free startup
output and RC2 live identity. Injected fake Forge and command collaborators own
the complete authored option through Forge replacement, terminal, and X-ray
acceptance path without network provider work.

The Phase 7C and 7D attendee groups remain provider-free in automated tests.
They verify private BFF bootstrap, never-reused context actors, authored-only
notice-bound selection, exact actor routing, isolated current/session/state and
recovery coordinates, standard admission, dual-clock expiry, deterministic
claim/replay behavior, and shutdown fencing. Phase 7D additionally verifies
that RC4 changes only the live identity; the combined target preserves Forge
and Presenter behavior; attendee commands receive a distinct injected
executor/checkpointer; external attendee tracing is forced off; facade and read
operations do not dispatch; accepted commands do; Presenter reservations remain
protected while their token amount scales to the configured provider maximum;
standard capacity is finite; startup is secret-free; and importing or
validating the target creates no provider, listener, worker, artifact, or
ordinary runtime composition.

Decision 0070 accounting coverage must remain provider-free. It exercises a
12,800-token maximum, the exact 8,192 floor, a lower finite maximum, blank/zero
configuration resolving to `None`, and unsupported budget arithmetic. It
proves each command reservation is at least the provider request maximum,
direct and combined token budgets scale by their unchanged logical-job
capacities, and an unbounded value fails before provider or listener
construction. Startup assertions cover effective `max_output_tokens`,
`command_reserved_tokens`, and `token_budget` without exposing secrets. The
same change must retain command idempotency, budget-exhaustion, cancellation
fencing, conservative settlement, published-contract immutability, and normal-
runtime inertness coverage.

Command-stream cancellation coverage must enforce decision 0071's ownership
rule. A deterministic stream blocked on one thread must be cancelled from a
different thread without that cancelling thread calling the iterator's
`close()` method or reading/updating graph checkpoint state. After the stream
owner resumes, it must close the iterator, install exactly one interrupted
graph result, and emit exactly one interrupted terminal. Product-core and HTTP
coverage must retain immediate late-output/accepted-state fencing, exact cancel
idempotency replay, eventual interrupted terminal recovery, usage settlement,
and checkpoint/job cleanup. Retain ordinary SSH Ctrl-C coverage because the
ownership correction is in the shared command-agent boundary, even though it
adds no worker or polling to normal runtime.

Validate the Phase 0 private product contract independently:

```sh
.venv/bin/python tools/product_contract.py validate contracts/product-api/v0.1.0-rc.1
.venv/bin/python tools/product_contract.py client-smoke contracts/product-api/v0.1.0-rc.1
```

Validate the unfrozen Phase 1 lifecycle-preview contract independently:

```sh
.venv/bin/python tools/product_lifecycle_contract.py validate contracts/product-api/v0.2.0-rc.2
.venv/bin/python tools/product_lifecycle_contract.py client-smoke contracts/product-api/v0.2.0-rc.2
```

Validate the unfrozen, disabled Phase 2 shared-core preview independently:

```sh
.venv/bin/python tools/product_session_contract.py validate contracts/product-api/v0.3.0-rc.1
.venv/bin/python tools/product_session_contract.py client-smoke contracts/product-api/v0.3.0-rc.1
```

Validate the unfrozen, disabled Phase 3 Presenter Stage preview independently:

```sh
.venv/bin/python tools/product_stage_contract.py validate contracts/product-api/v0.4.0-rc.1
.venv/bin/python tools/product_stage_contract.py client-smoke contracts/product-api/v0.4.0-rc.1
```

Validate the capability-disabled Phase 3D live-development identity:

```sh
.venv/bin/python tools/product_stage_live_contract.py validate contracts/product-api/v0.4.0-rc.2
.venv/bin/python tools/product_stage_live_contract.py client-smoke contracts/product-api/v0.4.0-rc.2
```

Validate the separate provider-free World Forge browser-integration contract:

```sh
.venv/bin/python tools/product_forge_contract.py validate contracts/world-forge-api/v0.1.0-rc.2
.venv/bin/python tools/product_forge_contract.py client-smoke contracts/world-forge-api/v0.1.0-rc.2
```

Validate the additive headless live World Forge contract:

```sh
.venv/bin/python tools/product_forge_contract.py validate contracts/world-forge-api/v0.1.0-rc.4
.venv/bin/python tools/product_forge_contract.py client-smoke contracts/world-forge-api/v0.1.0-rc.4
```

Validate the corrective, capability-disabled Forge-to-Stage RC2 release
candidate and its exact World Forge RC4 dependency:

```sh
.venv/bin/python tools/product_forge_stage_contract.py validate contracts/product-api/v0.5.0-rc.2
.venv/bin/python tools/product_forge_stage_contract.py client-smoke contracts/product-api/v0.5.0-rc.2
```

Validate the additive capability-disabled Stage X-ray development contract:

```sh
.venv/bin/python tools/product_xray_contract.py validate contracts/product-api/v0.6.0-rc.3
.venv/bin/python tools/product_xray_contract.py client-smoke contracts/product-api/v0.6.0-rc.3
```

Validate the additive capability-disabled Phase 7A Presenter launch-selection
contract:

```sh
.venv/bin/python tools/product_launch_selection_contract.py validate \
  contracts/product-api/v0.7.0-rc.1
.venv/bin/python tools/product_launch_selection_contract.py client-smoke \
  contracts/product-api/v0.7.0-rc.1
```

Validate the same-route capability-disabled Phase 7B live-development
identity independently:

```sh
.venv/bin/python tools/product_launch_selection_live_contract.py validate \
  contracts/product-api/v0.7.0-rc.2
.venv/bin/python tools/product_launch_selection_live_contract.py client-smoke \
  contracts/product-api/v0.7.0-rc.2
```

Validate the additive capability-disabled Phase 7C authored-attendee contract
and generated client independently:

```sh
.venv/bin/python tools/product_attendee_contract.py validate \
  contracts/product-api/v0.7.0-rc.3
.venv/bin/python tools/product_attendee_contract.py client-smoke \
  contracts/product-api/v0.7.0-rc.3
```

The provider-free owning suites are `tests.test_product_attendee`,
`tests.test_product_attendee_contract`, and
`tests.test_product_attendee_preview`. They cover inert import, gateway-context
authority and isolation, deterministic and retryable claim boundaries,
authored-only selection, notice evidence, dual-clock cleanup, standard/Stage
admission separation, strict private HTTP routing, complete synchronous
`--once` responses, generated-client behavior, and preservation of published
RC2 bytes without provider calls.

Validate the identity-only Phase 7D live attendee contract and generated client
independently:

```sh
.venv/bin/python tools/product_attendee_live_contract.py validate \
  contracts/product-api/v0.7.0-rc.4
.venv/bin/python tools/product_attendee_live_contract.py client-smoke \
  contracts/product-api/v0.7.0-rc.4
```

`tests.test_product_attendee_live_contract` and
`tests.test_product_attendee_live` are also provider-free. A real provider call
occurs only when a human explicitly starts the command documented in
`docs/PRODUCT_ATTENDEE_LIVE_DEVELOPMENT.md` and submits a Forge job, Presenter
command, or attendee command, or starts the attendee-only command documented in
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md` and submits an attendee command.

Validate the additive, capability-disabled Phase 9A private-attendee security
contract and generated server-only client independently:

```sh
.venv/bin/python tools/product_attendee_security_contract.py validate \
  contracts/product-api/v0.8.0-rc.1
.venv/bin/python tools/product_attendee_security_contract.py client-smoke \
  contracts/product-api/v0.8.0-rc.1
.venv/bin/python -m unittest -v \
  tests.test_product_attendee \
  tests.test_product_attendee_security \
  tests.test_product_attendee_security_contract \
  tests.test_product_attendee_security_preview
```

These tests make no provider call. They must preserve Product RC4 byte-for-
byte; prove exact revoke replay, changed-body conflict, uniform fresh denial,
session/command fencing, per-context/global saturation, window rollover,
unclaimed retryable rejection, and unthrottled safety/recovery operations; and
exercise the exact attendee-only route allowlist through bounded loopback
transport. Publication additionally binds the RC to the exact tested
implementation commit.

Validate the corrective, capability-disabled Phase 9B live-attendee successor
and generated server-only client independently:

```sh
.venv/bin/python tools/product_attendee_security_live_identity_contract.py validate \
  contracts/product-api/v0.8.0-rc.3
.venv/bin/python tools/product_attendee_security_live_identity_contract.py client-smoke \
  contracts/product-api/v0.8.0-rc.3
.venv/bin/python -m unittest -v \
  tests.test_product_attendee_live_boundary \
  tests.test_product_attendee_provider \
  tests.test_product_attendee_security_live_contract \
  tests.test_product_attendee_security_live_identity_contract \
  tests.test_product_attendee_security_live
```

These tests are provider-free: worker-process tests inject a finite local
executor and never read a real provider key or send an upstream request. They
must preserve the provider-free preview and current live identity; all 25
method/path and revocation/restart shapes remain identical; no backend
`access-ended` error is introduced; and all five capabilities remain disabled.
Worker tests must cover
owner-only exact-`0600` child credential loading, ambient provider and tracing
rejection, exact authored-source confinement, command-log privacy, zero
provider retries, full readiness before listen, finite process/IPC/output
bounds, replacement, cancellation, deadlines, no redispatch, conservative
ambiguous reservations, controller-event failure containment, tracked child
survivors, and bounded drain plus shutdown.

Exact-deadline tests use an injected pool clock rather than scheduler timing.
They prove cancellation observed first at the deadline retains interrupted
precedence. They also prove that deadline-first cooperative return produces
`provider_deadline_exceeded` with the full reservation, one completed dispatch,
no ambiguity or local force, and a reusable ready worker; the existing
non-return case remains ambiguous, fully reserved, contained, and replaced.
Decision 0080 owns this matrix.

The live identity gate bootstraps an attendee context, lists the launch option,
rejects Phase 9A's provider-free notice at exchange, accepts the exact reviewed
`notice:attendee-live-development-v1`, creates the session, and verifies that
`launch_binding` retains that notice and
`policy:attendee-live-authored-v1`. Phase 9A's owning tests continue to assert
its unchanged security-preview identities.

Replacement tests must synchronize against the pool's configured lifecycle
bound rather than a shorter fixed wall-clock delay. The test-only wait is
`startup_timeout_ms + (2 * terminate_grace_ms)` plus a one-second scheduler
margin. Tests fail immediately if the pool enters a terminal lifecycle and
accept recovery only when lifecycle is `ready`, ready and available workers
equal capacity, active workers are zero, and the focused cancellation and
concurrent-failure cases complete a follow-up dispatch. This changes no
runtime timeout. The release gate repeats the concurrent-
failure and cancellation-timeout recovery cases in fresh processes so spawn
scheduling variance cannot be mistaken for product failure while a real
failure to return to `ready` remains visible.

Composition tests also require the dispatch timeout to cover the complete
worker-startup timeout and both retired-child containment phases. Pool and core
must enforce the same byte, nesting-depth, node-count, key, number, and integer
runtime-document policy before the initial document or a proposed state can be
committed. Tests exercise at least thirteen settled admissions and prove that
logical-job and resource budgets derive from active-session capacity times the
per-session command limit. They verify that readiness means local client
construction only, and that startup output contains no credential, path,
digest, authority, command, or secret. Local worker termination is asserted as
a DREAMLAND publication fence, never as proof of upstream cancellation or
billing termination.

Validate the wire-compatible, capability-disabled Phase 9C supervised
attendee successor independently:

```sh
.venv/bin/python tools/product_attendee_security_deployment_contract.py validate \
  contracts/product-api/v0.8.0-rc.4
.venv/bin/python tools/product_attendee_security_deployment_contract.py client-smoke \
  contracts/product-api/v0.8.0-rc.4
.venv/bin/python -m unittest -v \
  tests.test_product_attendee_security_preview \
  tests.test_product_attendee_security_deployment \
  tests.test_product_attendee_security_deployment_contract
```

These tests inject provider-free collaborators. They must pin RC1 through RC3,
preserve all 25 wire operations and the live notice/policy pair, keep every
capability disabled, and reject any backend `access-ended` error. Runtime tests
cover exact UTC-window validation and propagation, monotonic cutoff, future
opening, atomic fresh-bootstrap cutoff with replay/conflict recovery,
no-request expiry and drain, terminal recovery before the recovery close, safe
startup redaction, loopback-only binding, `SIGINT`/`SIGTERM`
teardown, admitted-handler joining, inert import, and restart-only
transport-authority replacement with a fresh epoch. They do not prove upstream
provider reachability, cancellation, provider-key rotation, account identity,
quota, or billing state.

The same focused suites own the Phase 9C-D capacity envelope. They must preserve
the exact 4-context/4-session/2-worker/8-connection defaults; reject values over
64 contexts, 64 sessions, eight workers, or 32 connections; reject
`workers > sessions` and `sessions > contexts`; and verify every derived actor,
grant, request, lifecycle, budget, and retained-record ceiling. Provider-free
service coverage must create 20 distinct contexts, grants, and sessions, return
typed `attendee_context_capacity_exhausted` for the next bootstrap, then close
and revoke all 20. A blocking deterministic executor must prove five
simultaneous commands, retryable sixth-command saturation, identical-request
acceptance after one worker returns, and complete cleanup without fixed sleeps.
This is owning-layer capacity logic evidence, not deployed-load evidence.

Development continues with owning and affected integration regression checks.
Full load, deployed security, provider operations,
rollout/rollback, and presentation qualification may follow stabilization;
material later changes invalidate the affected evidence. The known loopback
response-reset failures remain open verification work and are not waived by
their reproduction outside DREAMLAND or by UI integration acceptance.

Run the Phase 9C provider-free black-box rehearsal separately:

```sh
.venv/bin/python tools/product_attendee_security_deployment_rehearsal.py run \
  --contract-root contracts/product-api/v0.8.0-rc.4
.venv/bin/python -m unittest -v \
  tests.test_product_attendee_security_deployment_rehearsal
```

This gate uses real subprocesses, loopback sockets, the supervised RC4 server,
and the fixed two-worker process pool. Its worker executor is deterministic and
provider-free. Its accepted evidence remains the fixed
four-context/four-session/eight-connection local
boundary, replay/conflict, worker saturation and cancellation, cutoff and
recovery, unread-body/pipelining protection, stalled-header saturation, signal
convergence, and restart-only transport-bearer/epoch rotation. The deterministic
worker factory must succeed with a blank owner-only provider file that would
fail live content validation; this is still not provider-key rotation evidence.
A successful report must remain allowlisted, state
`scope=loopback_provider_free`, and set
`production_capability_evidence=false`; a failed run emits only a generic safe
error and exits nonzero. It is neither a live-provider test nor evidence for a
public capability. The owning transport suite separately proves that unread
rejected request bodies close their connection and cannot be reparsed as a
pipelined request.

Validate the bounded Phase 9C live-provider canary without provider spend:

```sh
.venv/bin/python -m unittest -v \
  tests.test_product_attendee_security_deployment_canary
```

This automated suite uses the provider-free fixture. It owns spend-
acknowledgement rejection, exact immutable RC4 and profile-digest checks,
owner-only credential metadata, the canary-only 1-through-8,192 output-token
range, fixed loopback/ephemeral-port/one-worker/
one-command limits, the fixed `uname -a`, one logical attempt, complete and
unclipped event/transcript recovery under the canary-only 65,536-byte and
240-chunk maxima, exact settled token/spend equality with announced full
reservations, terminal consistency, close/revoke,
bounded shutdown, and strict report privacy. It must never read a live
credential or call an upstream provider.

The real canary is a separate human-authorized operation documented in
`docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`. It requires
`--acknowledge-provider-spend` and can make one logical provider attempt. Its
report is single-attempt live execution evidence with
`production_capability_evidence=false`; full reserved settlement is not actual
provider billing/token truth. The canary's 240-chunk effective limit keeps
accepted-cursor replay within the 256-event journal and does not change the
supervised target default. Its independent 8,192-token maximum bounds the one
authorized provider response without changing the target's general range. Do
archive the invoking Git revision and dirty-state status beside a retained
report; `contract.backend_commit` identifies immutable RC4, not the canary
runner revision. Do not treat it as upstream request-count,
account/quota, cancellation/billing-termination, load, rollout/rollback,
public-edge, BFF/browser-security, or production-readiness evidence.

Validate the provider-free Phase 8A World Access RC3 artifact and compile its
generated server-only client independently. The compile harness requires
Node.js 20 or newer and installs only the lockfile-pinned TypeScript development
dependency; it is never imported by normal DREAMLAND startup:

```sh
node --version
npm ci --prefix tools/world_access_client_smoke
.venv/bin/python tools/product_world_access_contract.py validate \
  contracts/world-access-api/v0.1.0-rc.3
.venv/bin/python tools/product_world_access_contract.py client-smoke \
  contracts/world-access-api/v0.1.0-rc.3
.venv/bin/python -m unittest \
  tests.test_product_world_access_contract \
  tests.test_product_world_access_preview
```

The client smoke compiles under strict TypeScript settings, exercises all six
methods through a bounded fake fetch with fake credentials, validates success
and problem media types, and rejects missing or status-inconsistent content
types. The preview suite binds temporary loopback ports but calls no provider.
Publication must cite the current backend commit containing the exact RC3
artifact, implementation, generator, and pinned client toolchain.

Superseded RC1/RC2 World Access snapshots and their archival tree checks are
omitted from the source release. Current operation/security matrices, dependency
pins, generated clients, publication guards, and provider-free/live compatibility
remain covered. See [the retained contract inventory](../contracts/README.md).

RC3 publication evidence includes owning access/catalog/attendee/registry
tests, the combined injected-collaborator HTTP group, exact exchange-to-Product
binding equality and replay tests, both contract commands, and manifest/OpenAPI
digests. The preview tests also cover a single mixed-catalog notice and
startup-current monotonic evaluator time. These checks spend no provider
capacity.

Run optional live tests:

```sh
DREAMLAND_LIVE_PROVIDER_TESTS=1 .venv/bin/python -m unittest tests.test_live_agent
DREAMLAND_LIVE_PROVIDER_TESTS=1 .venv/bin/python -m unittest \
  tests.test_product_stage_live.ProductStageLiveTests.test_opt_in_real_agent_preserves_state_across_commands
```

The legacy `DREAMLAND_LIVE_OPENAI_TESTS=1` flag still enables
`tests.test_live_agent` for OpenAI-focused workflows. The generic
`DREAMLAND_LIVE_PROVIDER_TESTS=1` flag uses the configured `[agent]` model
reference and provider API key.

## Maintenance

Update this document after reorganizing tests, changing test commands, changing
live-test policy, or adding new major runtime layers.
