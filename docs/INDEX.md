# Documentation Index

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: provide the preferred reading order and navigation map for project
documentation.

## Overview

Start here when you need project context. DREAMLAND documentation is split into
small topic files so the README can stay short and agents can load only the
relevant context for a change.

## Reading Order

1. `README.md`: setup, run, test, and top-level project summary.
2. `AGENTS.md`: repository instructions for coding agents.
3. `docs/PROJECT_MODEL.md`: product direction and simulation model.
4. `docs/DOCUMENTATION.md`: documentation schema and maintenance rules.
5. Topic docs tied to the change being made.
6. `docs/decisions/`: durable architecture and process decisions.
7. `ROADMAP.md`: planned work and deferred decisions.

## Topic Docs

- `docs/ARCHITECTURE.md`: runtime components and ownership boundaries.
- [Operations](OPERATIONS.md): configuration, running, Docker console access,
  model adoption, logging, and tracing.
- [Beta SSH Deployment](BETA_SSH_DEPLOYMENT_PLAN.md): traced SSH-only beta for
  outside testers, deployment lifecycle commands, exposure limits, and the
  promotion path out of beta.
- [UI Companion Deployment Handoff](BACKEND_DEPLOYMENT_UI_HANDOFF.md): private
  backend connection and compatibility contract for independently deployed UIs.
- [Native SSH Lobby](SSH_LOBBY.md): optional deterministic entry, hot world
  curation, versioned notices, fresh visits, public IDs, and operator rehearsal.
- [SSH Lobby Development Plan](SSH_LOBBY_DEVELOPMENT_PLAN.md): agreed native
  lobby behavior, implementation phases, and required security assessment.
- [SSH Lobby Security Assessment](SSH_LOBBY_SECURITY_AUDIT.md): independent
  adversarial review, fixed findings, and the remaining release checkpoint.
- [SSH Lobby Terminal Rehearsal](SSH_LOBBY_REHEARSAL.md) and
  [Trace Check](SSH_LOBBY_TRACE_CHECK.md): disposable operator validation procedures.
- [MCP Model Evaluation Controls](MCP_MODEL_EVALUATION.md): isolated per-session
  profiles and model settings, detached Spiritguide overrides, and run evidence.
- [Post-Run Emulator JSON Annotations](EMULATION_JSON_ANNOTATIONS.md): external
  `repaired`/`rejected` Langfuse scores, conservative evidence matching, and
  unchanged interactive command behavior.
- `docs/ENRICHMENT_AGENT.md`: default session-local background enrichment,
  triggers, graph, safe patching, logging, and controls.
- `docs/MODEL_FIT.md`: current packaged model preferences and per-role
  configuration overrides.
- `docs/ADMIN_INTERFACE.md`: planned local admin API and MCP bindings.
- `docs/ADMIN_API_V0.md`: concrete REST request, response, query, and event
  contract for `/api/v0`.
- `docs/PRODUCT_SESSION_API.md`: implemented private product API foundation,
  lifecycle, shared-core, Presenter Stage, Forge/X-ray, and source-neutral
  authored/Forge launch-selection preview/live targets, authored attendee
  integration preview/live and private-security targets, release-candidate
  artifacts, and agreed later session requirements.
- `docs/PRODUCT_STAGE_LIVE_DEVELOPMENT.md`: local live-provider Presenter Stage
  setup and the backend-to-BFF evaluation handoff.
- `docs/FORGE_STAGE_LIVE_DEVELOPMENT.md`: local live World Forge-to-Stage
  setup, exact selected-artifact handoff, and combined UI evaluation workflow.
- `docs/PRODUCT_LAUNCH_SELECTION_LIVE_DEVELOPMENT.md`: pinned authored plus
  authoritative Forge generic-selection startup and live UI evaluation flow.
- `docs/PRODUCT_ATTENDEE_LIVE_DEVELOPMENT.md`: combined live authored-attendee,
  Forge, and Presenter startup, isolation, cutoff, and private BFF evaluation
  flow.
- `docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`: attendee-only provider-free
  and credential-isolated live private BFF integration, context revocation,
  logical admission, dedicated worker capacity, supervised availability,
  provider-free deployment rehearsal, bounded live-provider canary, and public
  deployment gates.
- `docs/PRODUCT_WORLD_ACCESS_DEVELOPMENT.md`: generated-world promotion,
  reveal, catalog/exchange authority, and provider-free/live development
  workflows.
- `docs/WORLD_FORGE.md`: typed audience seed, browser-integration preview,
  headless live vertical slice, private contracts, recovery semantics, and
  remaining live Forge work.
- `docs/MCP_AGENT_WORKFLOWS.md`: external-agent workflows for using DREAMLAND
  MCP sessions, tools, and prompts.
- `docs/AGENT_STATE.md`: command-agent boundary and state contract.
- `docs/STRUCTURED_FAILURES.md`: structured command-agent failure capture,
  capsules, and parser replay scope.
- `docs/STATE_PROFILES.md`: state profiles, host metadata, active host, and
  prompt ownership rules.
- `docs/INSPECT_AI_HARNESS.md`: optional Inspect AI evaluation harness,
  TOML run specs, run artifacts, and score interpretation.
- `docs/TESTING.md`: test layout, test speed expectations, and live test
  policy.
- `docs/DEVELOPMENT_HISTORY.md`: historical project evolution from git.

## Decision Records

- [0101 MCP session command options](decisions/0101-mcp-session-command-options.md):
  direct admin model/tier arguments, standard OpenAI command defaults, and
  private runtimes for ordinary MCP sessions.
- [0087 Post-run emulator JSON annotations](decisions/0087-post-run-emulator-json-annotations.md):
  asynchronous trace scores, verified parser evidence, and scoped metadata.
- [0085 Isolated MCP evaluation runtimes](decisions/0085-isolated-mcp-evaluation-runtimes.md):
  private per-session agents, profile identity preservation and resolved settings.
- [0084 Multi-provider model evaluation controls](decisions/0084-multi-provider-model-evaluation-controls.md):
  per-role OpenAI options, Astra cache support, provider isolation, and MCP
  smoke-test preparation.

- `docs/decisions/0001-documentation-system.md`: documentation schema and
  required docs maintenance.
- `docs/decisions/0002-layered-ssh-tests.md`: split direct session/state tests
  from real Paramiko transport tests.
- `docs/decisions/0003-world-profile-framework.md`: mandatory world profiles,
  keyed mutable world state, and decisive additive world-building.
- `docs/decisions/0004-spiritguide-operator-agent.md`: session-scoped
  operator state guidance through spiritguide.
- `docs/decisions/0005-state-profile-images.md`: saved session-derived state
  profiles for exact, system, and world entry states.
- `docs/decisions/0006-early-langfuse-session-registration.md`: early
  Langfuse visibility for new SSH sessions.
- `docs/decisions/0007-frame-local-host-identity.md`: frame-local host identity
  and checkpoint refresh after transport-side nested exits.
- `docs/decisions/0008-local-admin-interface.md`: shared local admin service
  model, REST binding, and MCP binding.
- `docs/decisions/0009-mcp-headless-command-sessions.md`: MCP-created live
  sessions and non-SSH command execution through the normal backend.
- `docs/decisions/0010-mcp-typed-inspection-tools.md`: parameterized MCP
  inspection tools, tag grouping, cleanup, and workflow guidance.
- `docs/decisions/0011-compact-mcp-command-inspection.md`: compact MCP command
  inspection defaults with explicit full-detail escalation.
- `docs/decisions/0012-agent-session-lifecycle-and-command-ids.md`: direct
  command ids and shared console/MCP lifecycle management for agent tests.
- `docs/decisions/0013-external-mcp-agent-guidance.md`: external MCP agent
  workflow guidance kept separate from simulated command-agent prompts.
- `docs/decisions/0014-structured-failure-capsules.md`: durable capture of
  rejected structured command-agent responses before retry/salvage policy.
- `docs/decisions/0015-provider-qualified-model-references.md`: explicit
  provider-qualified model references in configuration.
- `docs/decisions/0016-just-in-time-world-generation.md`: high-level world
  directives and materialized artifact registries.
- `docs/decisions/0017-generic-client-mcp-surface.md`: separate generic MCP
  surface for command-client evaluation without admin/operator tooling.
- `docs/decisions/0020-inspect-run-contracts.md`: trusted run contracts,
  scenario-generic prompts, and Inspect run artifact scoring.
- `docs/decisions/0021-inspect-ssh-pivot-budget.md`: charged command budget,
  free SSH pivot accounting, and shortcut SSH rejection for Inspect runs.
- `docs/decisions/0022-langfuse-command-traces.md`: per-command Langfuse traces
  grouped under a stable DREAMLAND session id.
- `docs/decisions/0023-deterministic-secret-prompt-echo.md`: deterministic
  terminal echo suppression for recognized secret prompts.
- `docs/decisions/0102-whole-command-routing-and-ordered-effects.md`: whole-line
  model routing, deterministic builtin preflight, and ordered state effects.
- `docs/decisions/0024-command-list-and-cd-defer.md`: historical simple command-list
  sequencing and temporary `cd` filesystem hint fallback.
- `docs/decisions/0025-unified-world-host-profiles.md`: unified world profiles,
  host metadata, entry host, and mutable active host tracking.
- `docs/decisions/0026-required-state-profile-world-state.md`: required
  state-profile boot and single model-facing world state.
- `docs/decisions/0027-host-local-world-state.md`: host-local command
  history, services, artifacts, and facts inside unified world state.
- `docs/decisions/0028-state-profile-cleanup-boundaries.md`: removal of
  deprecated scope scaffolding, blank detached profile creation, and command
  history envelope cleanup.
- `docs/decisions/0029-remove-legacy-world-profiles.md`: removal of YAML world
  profiles and consolidation on JSON state-profile boot state.
- `docs/decisions/0030-adaptive-ambient-realism.md`: context-sensitive
  operational texture and selectively materialized ambient detail.
- `docs/decisions/0031-active-host-emulator-context-projection.md`:
  emulator-only active-host detail with compact inactive host cards.
- `docs/decisions/0032-openai-gpt-5-6-explicit-command-prompt-caching.md`:
  emulator-only GPT-5.6 explicit caching of the stable system prefix.
- `docs/decisions/0033-session-local-enrichment-agent.md`: separate
  session-local EnrichmentAgent roles, reusable triggers, and deterministic
  safe-apply authority.
- `docs/decisions/0034-portable-candidate-driven-enrichment.md`: portable
  Expert routing, deterministic creative latitude, candidate provenance,
  tagged Integrator no-op, deterministic structural normalization, and safe
  additive integration.
- `docs/decisions/0035-session-correlated-enrichment-event-retention.md`:
  independently sized per-session enrichment lifecycle retention, deferred
  candidate correlation, exact job filtering, and process-local REST/MCP
  inspection.
- `docs/decisions/0036-density-shaped-enrichment.md`: expanded deterministic
  candidate capacity, multi-depth and lifecycle texture, and unchanged safe
  mutation authority.
- `docs/decisions/0037-automatic-enrichment-checkpoint.md`: priority-based
  bounded automatic deferral, deterministic interaction focus, structural
  Integrator-output normalization, and automatic-session observability.
- `docs/decisions/0038-foreground-authority-and-semantic-consistency.md`:
  bounded active-session authority, apply-time semantic race protection,
  deterministic focus-match classes, and automatic terminal-provider
  suppression.
- `docs/decisions/0039-live-enrichment-status-gauge.md`: one-second in-place
  EnrichmentAgent status following, terminal capability boundaries, and
  transport-owned cancellation and observability.
- `docs/decisions/0040-enrichment-capacity-and-luna-comparison.md`: bounded
  sixteen-to-twenty-four artifact capacity and unchanged additive authority.
- `docs/decisions/0041-latitude-sized-expert-relations.md`: latitude-sized
  Expert relation capacity, authored-order validation and deduplication,
  bounded overflow retention, and the explicit
  separation between topology richness and evidence or mutation authority.
- `docs/decisions/0042-ambient-presence-enrichment.md`: deterministic focused
  and ambient-presence modes, a separately configurable Ambient Coordinator,
  expanded event-driven triggers, three-job session capacity, 10,000-record
  event retention, and the boundary with future autonomous world activity.
- `docs/decisions/0043-ambient-pulse-presence-gap-and-foreground-evidence.md`:
  progress-driven pulse admission, bounded active-host bootstrap retry,
  deterministic presence-gap routing, and strict apply-bound foreground time
  and complete-inventory evidence.
- `docs/decisions/0044-concise-ambient-independent-life-routing.md`: concise
  ambient-only independent-life planning, existing-surface preference,
  unchanged structural coverage, and preservation of focused Enrichment and
  safe-apply boundaries.
- `docs/decisions/0045-person-centered-ambient-and-bounded-integrator-refresh.md`:
  unambiguous human-centered ambient assignments, service-only mapping no-op,
  stable latest-per-scope foreground-evidence authority, one conservative
  Integrator-only refresh after genuine evidence drift, reserved foreground-
  row attribution, independently grounded no-row-value-reuse, field-sensitive
  neutral-brief vocabulary, and the complete EnrichmentAgent implementation
  lineage through `enrichment-v15`.
- `docs/decisions/0046-native-ssh-attendee-authentication.md`: native
  environment-only shared-password authentication, arbitrary attendee username
  labels, password-only negotiation, restricted SSH channel capabilities, and
  separation of attendee sessions from operator authority.
- `docs/decisions/0047-persistent-ssh-transport-identity.md`: dedicated
  host-local SSH transport identity, secure atomic first-run generation,
  stable fingerprint reporting, fail-closed reuse, and the operational
  backup/rotation boundary.
- `docs/decisions/0048-code-and-presentation-licensing.md`: MIT licensing for
  DREAMLAND code and project documentation, CC BY 4.0 licensing for published
  BSides Augusta presentation materials, and the third-party asset boundary.
- `docs/decisions/0049-default-safe-enrichment.md`: required default
  EnrichmentAgent scheduling, deterministic safe application, explicit opt-out
  and shadow overrides, and the conference inclusion boundary.
- `docs/decisions/0050-private-product-api-boundary.md`: UI-owned BFF and
  private DREAMLAND contract ownership, default-off composition, exact product
  capabilities, and separation from SSH/admin/MCP.
- `docs/decisions/0051-integration-only-product-lifecycle-preview.md`:
  protocol-neutral epoch-bound lifecycle semantics, stateful loopback preview,
  immutable revision and authority isolation, and continued fail-closed runtime
  composition.
- `docs/decisions/0052-product-idempotency-claim-and-once-completion.md`:
  explicit mutation claim and failure-retention semantics plus response-complete
  synchronous one-request integration tools.
- `docs/decisions/0053-unfrozen-product-session-core-preview.md`:
  asynchronous sole-slot commands, isolated publication fencing, bounded
  recovery journals, product-only admission, atomic replacement, and the
  unfrozen Phase 2 release gate.
- `docs/decisions/0054-unfrozen-presenter-stage-preview.md`: selection-bound
  Stage resume, explicit replacement, hard reserved admission, display-safe
  metadata, and the continued unfrozen/disabled Phase 3 gate.
- `docs/decisions/0055-live-development-presenter-stage.md`: explicit
  loopback-only real-agent evaluation target, conservative accounting, and the
  continued disabled production gate.
- `docs/decisions/0056-world-forge-foundation.md`: inert canonical WorldSeed,
  validation, hashing, and provider-free lifecycle foundation.
- `docs/decisions/0057-world-forge-browser-integration-preview.md`:
  prepare-before-confirm, private Forge transport, opaque recovery positions,
  and provider-free preview provenance.
- `docs/decisions/0058-world-forge-rc2-integration-stabilization.md`: immutable
  RC1-to-RC2 correction of safe errors, cancellation, recipe recovery,
  deadline truth, and lifecycle schema relationships.
- `docs/decisions/0059-world-forge-headless-live-vertical-slice.md`: explicit
  single-attempt provider generation, independent validation, immutable
  revision storage, safe receipts, and continued runtime isolation.
- `docs/decisions/0060-world-forge-rc4-live-integration-corrections.md`:
  invariant live provenance, atomic deadline selection, strict referenced-host
  validation, and runtime-faithful canonical receipts.
- `docs/decisions/0061-forge-to-stage-development-handoff.md`: authoritative
  selected records, exact artifact hydration, dynamic-only private Stage,
  additive contract identity, and continued capability isolation.
- `docs/decisions/0062-forge-stage-replacement-terminal-contract-correction.md`:
  immutable RC1-to-RC2 correction of the replacement terminal snapshot,
  operation-aware CAS/event evidence, provider-free UI handoff, and the Phase 6
  X-ray exclusion.
- `docs/decisions/0063-display-safe-stage-xray-development-slice.md`: opt-in
  display allowlist, dual accepted/projection coordinates, Stage watermark
  semantics, presenter-only detail reads, and terminal/runtime isolation.
- `docs/decisions/0064-stage-xray-rc2-contract-corrections.md`: strict generated
  X-ray transport validation and Forge-valid empty-display normalization.
- `docs/decisions/0065-stage-xray-rc3-integrity-corrections.md`: JSON-safe
  coordinate enforcement, strict success context, and collision-safe scoped
  X-ray entity identities.
- `docs/decisions/0066-source-neutral-launch-selection.md`: closed authored
  and Forge Presenter origins, trusted digest-pinned authored snapshots,
  non-authorizing browser locators, BFF-only grant authority, exact exchange
  idempotency, and the attendee/Phase 8 boundary.
- `docs/decisions/0067-provider-backed-source-neutral-launch.md`: explicit
  provider-backed authored/Forge composition, strict runtime kind dispatch,
  authoritative post-selection registration, and unchanged capability
  isolation.
- `docs/decisions/0068-authored-public-attendee-development-boundary.md`:
  bounded gateway-context leasing, authored-only attendee selection,
  notice-bound recovery, standard admission isolation, dual-clock expiry, and
  continued capability/Phase 8 separation.
- `docs/decisions/0069-provider-backed-authored-attendee-development.md`:
  combined provider-backed authored-attendee development, exact routing,
  tracing privacy, Stage reservation, cutoff, and RC3-compatible live identity.
- `docs/decisions/0070-live-command-token-reservation.md`: finite live command
  provider maxima, derived token reservations and budgets, startup audit,
  fail-closed unbounded configuration, and immutable wire artifacts.
- `docs/decisions/0071-command-stream-cancellation-ownership.md`: owner-thread
  stream close and interrupted-state recording, cross-thread cancellation
  marking, immediate publication fencing, and the Phase 7D UI retest gate.
- `docs/decisions/0072-generated-world-access-authority.md`: separately
  authorized promotion, hidden access truth, reveal, bounded catalog,
  notice-bound exchange, and generated-world attendee eligibility.
- `docs/decisions/0073-world-access-recovery-correlation.md`: output-only
  launch-binding correlation for ambiguous World Access exchange and Product
  session-create recovery.
- `docs/decisions/0074-live-world-access-composition.md`: explicit live
  Forge-to-attendee composition, distinct Operator/Presenter authority, shared
  Product epoch, and retained publication accounting.
- `docs/decisions/0075-public-attendee-private-security-boundary.md`:
  attendee-context revocation, replay-safe logical admission, an attendee-only
  private target, and continued public capability isolation.
- `docs/decisions/0076-attendee-dedicated-provider-boundary.md`: child-only
  attendee credentials, spawn-process provider capacity, readiness,
  conservative accounting, bounded drain/shutdown, and explicit recovery
  fences.
- `docs/decisions/0077-attendee-live-privacy-and-replacement-release-gate.md`:
  immutable RC2 preservation, reviewed live-provider privacy selection,
  lifecycle-derived worker replacement synchronization, and unchanged
  deliberate-end and explicit-restart behavior.
- `docs/decisions/0078-supervised-attendee-availability-and-restart-boundary.md`:
  exact scheduled availability, no-request cutoff, bounded recovery, signal
  convergence, and stop/replace/start authority and credential policy.
- `docs/decisions/0079-provider-free-attendee-deployment-rehearsal.md`:
  real-process loopback rehearsal, local saturation and recovery evidence,
  request-desynchronization closure, and non-production report scope.
- `docs/decisions/0080-attendee-provider-exact-deadline-containment.md`:
  cancellation-first ordering, deadline-first cooperative containment, worker
  reuse, conservative accounting, and unchanged RC4 terminal projection.
- `docs/decisions/0081-bounded-live-provider-attendee-deployment-canary.md`:
  explicit single-command live-provider evidence, complete publication checks,
  immutable RC4 reuse, strict non-production reporting, and unchanged
  deliberate-end/restart semantics.
- `docs/decisions/0082-bounded-attendee-deployment-capacity-envelope.md`:
  opt-in attendee capacities, derived budgets and retention, unchanged defaults,
  and the separate measured-load gate.

- [0114 SSH Handshake Diagnostics](decisions/0114-ssh-handshake-diagnostics.md):
  connection-local probe classification, safe lifecycle fields, retained
  unexpected-error tracebacks, and Paramiko upgrade checks.

- [0117 GPT-6 Role Model Refresh](decisions/0117-gpt-6-role-model-refresh.md):
  current Sol/Luna role mapping, local configuration staging, cache and
  reasoning compatibility, and explicit demo adoption.

- [0118 All Runtime Roles Fast Tier](decisions/0118-all-runtime-roles-fast-tier.md):
  explicit Fast defaults and local/demo adoption for every runtime model role.
- [0119 Optional Lobby Entry Username](decisions/0119-optional-lobby-entry-username.md):
  saved-account resolution when adding a lobby entry without an explicit account.

The [contract inventory](../contracts/README.md) lists retained backend
artifacts. The [developer tools review](../tools/README.md) records what remains
useful without the separate UI and identifies UI-specific removal candidates.

## Maintenance

Update this index whenever durable docs are added, renamed, removed, or
substantially reorganized.

Research evidence and study-specific tooling are published separately. See
[the source-release boundary](decisions/0128-source-release-research-boundary.md)
for retained generic tools and examples.
