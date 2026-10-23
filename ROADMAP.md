# Dreamland Roadmap

This document tracks deferred work and follow-up feature areas that should stay
visible across implementation slices.

## Product Direction

- Expand from a single SSH-accessible host into an operator-guided,
  LLM-emulated computing network with coherent systems, users, programs,
  services, files, logs, and history.
- Expand mandatory state profiles into richer network and scenario
  configuration that can seed any operator-defined environment without
  hard-coding toy hosts in the default profile. Enterprise, home, research,
  industrial, embedded, historical, fictional, speculative, ambitious, and
  absurd settings are illustrative examples, not a closed taxonomy.
- Consider an optional operator-authored state-profile field describing the
  world's intended setting. Its value should be extensible rather than a closed
  environment taxonomy, and its absence must leave the simulation
  unconstrained. If adopted, inject it deterministically into Coordinator
  context instead of spending Coordinator tokens inferring or restating it.
  The field name and schema remain undecided; do not reserve a generic `type`
  until that contract is designed.
- Evaluate world building in product-priority order: engaging depth, internal
  coherence, real-world plausibility, and exact factual realism. Do not turn
  safety controls or bounded per-call work into a global ceiling on world depth.
- Design additional entry points beyond SSH while preserving SSH as the current
  tested transport.
- Add operator controls for scenario goals, world constraints, generated facts,
  state persistence, and observability.

## SSH Entry-Point Follow-Ups

Native shared-password authentication, the attendee/operator boundary, and
persistent host-local SSH transport identity are implemented by decisions 0046
and 0047. The arbitrary SSH username remains an untrusted session label and
must not become a source of authorization or attribution.

The optional [native SSH lobby](docs/SSH_LOBBY.md) provides deterministic
agreement, hot world curation, isolated visits, and durable public report
references. Its required assessment and deployment rehearsal remain tracked in
the [development plan](docs/SSH_LOBBY_DEVELOPMENT_PLAN.md). Broader operational
work remains:

- Provision one intended host-key identity across replicas behind the same
  endpoint and rehearse the documented backup/restore and intentional-rotation
  procedures on the selected deployment. The fingerprint remains available to
  operators and canaries but is not a presentation or attendee-publication
  requirement.
- Add source-level failed-authentication throttling, connection and command
  rate limits, global and per-session concurrency ceilings, token/spend budgets,
  monitoring, and an emergency kill switch.
- Define deployment-secret distribution and a restart-based shared-password
  rotation runbook that does not place the password in INI files, logs, process
  arguments, or release artifacts.
- Add deployment canaries that prove password-only authentication, rejection of
  SFTP, forwarding, additional channels, and `!dreamland`, and the absence of a
  directly reachable bypass listener.
- Introduce durable per-attendee identity only if product requirements later
  need individual attribution or revocation; do not infer it from a self-chosen
  SSH username or the shared admission password.

## Runtime World Lifetime

- Keep current runtime worlds isolated and temporal per session. Background
  enrichment and other runtime agents may read or modify only their explicitly
  targeted session, and their changes disappear with that session unless an
  operator explicitly saves a state profile.
- Preserve explicit state-profile saves as the only current path for carrying
  generated world state into later sessions. Do not make the initial
  EnrichmentAgent mutate a shared global world or silently update its source
  profile.
- Design a future persistent shared-world runtime for scenarios in which
  multiple sessions intentionally inhabit one evolving world. That feature
  will need an explicit world-instance identity, persistence and lifecycle
  rules, session attachment semantics, visibility boundaries, conflict policy,
  and operator controls. Treat it as separate future work rather than an
  implicit extension of session-local enrichment.

## Background Agent Family

- Introduce the first asynchronous runtime role as an `EnrichmentAgent` that
  performs just-in-time or slight-read-ahead finishing work for one isolated
  session. Keep its LangGraph flow, triggers, model calls, state snapshots,
  integration, and apply results visible through structured runtime logs and
  Langfuse traces.
- Keep the runtime design open to additional capability-scoped background
  agents without giving every agent the same authority. Future roles may
  include correction agents that repair recent emulator inconsistencies,
  adversarial agents that obstruct a user within scenario policy, assistant
  agents that guide a user, and intent agents that infer likely user direction.
- Give each background-agent type an explicit trigger policy, read projection,
  write capability, state target, model configuration, LangGraph definition,
  logging identity, and operator enable/disable controls. Reuse lifecycle and
  observability infrastructure where it stays simple, while keeping each
  logical agent graph directly inspectable rather than hiding behavior behind
  a generic dynamic workflow engine.

## Future WorldActivityAgent

- Design a distinct `WorldActivityAgent` only when DREAMLAND needs simulated
  actors and operational activity to change independently over time. The
  current EnrichmentAgent may establish historical activity, recurring
  patterns, present residue, log entries, and coherent connection ecology, but
  it must not turn those additive descriptions into autonomous personas or
  permanent claims about short-lived current activity.
- Give the activity feature typed actor identities and typed activity records,
  an explicit world clock, recurrence and expiry semantics, and a projection
  contract that distinguishes active observations from historical evidence.
  Human users are only one possible actor family; the design must remain open
  to automation, devices, institutions, synthetic actors, remote operators,
  historical occupants, and scenario-specific entities outside a closed
  taxonomy.
- Define controlled non-additive authority for starting, advancing, expiring,
  and correcting activity without silently broadening Enrichment's safe
  additive write contract. The feature needs explicit ordering, conflict, and
  foreground-authority rules before it can create or remove current sessions,
  transient processes, or live connections.
- Keep the eventual activity workflow in its own directly inspectable
  LangGraph with an explicit trigger policy, read projection, write
  capabilities, logging identity, and operator controls. It may reuse the
  background event bus and scheduler primitives, but it must not be hidden as
  an opaque branch of the Enrichment graph.

## EnrichmentAgent Follow-Ups

- Preserve person-centered ambient work and independent grounding under
  `ambient-human-presence-v3` and `ambient-human-integration-v3`. Retain
  deterministic practitioner-title tests and check nonhuman-person, explicit-
  solitude, second-refresh-conflict, and active-host-pivot cases. Measure
  bootstrap readiness, duplicate clusters, authority races, foreground latency,
  chronology, cost, and visible independent activity. Keep autonomous lifecycle
  changes outside additive Enrichment.
- Continue controlled automatic safe-mode checkpoints. Measure deferred
  replacement, release, expiry, cooldown/rate loss, applied registries,
  foreground use, contradictions, latency, cost, and user experience. Preserve
  automatic safe application as the product default; use explicit shadow mode
  for non-mutating comparisons.
- Use deterministic foreground match classes in subsequent checkpoints to
  distinguish exact artifact encounters, related-path exploration, and coarse
  surface-only overlap. Do not treat `surface_only` as evidence that a specific
  enriched artifact was rendered. Measure whether these classes are sufficient
  before adding output-derived matching or a model usefulness judge.
- Design explicit origin and temporal provenance for applied world records.
  Current state does not distinguish profile, command-emulator, EnrichmentAgent,
  and future-agent material after application. Coordinate provenance with
  correction authority rather than inferring source or age from registry
  location or descriptive prose.
- Evaluate additional typed authority domains only from concrete observations
  with explicit capture and race semantics. The current `active_session`
  authority protects current login/program behavior; it is not universal
  semantic contradiction detection. Defer any model judge until its latency,
  token cost, failure modes, and effect on DREAMLAND's depth-first product
  priority can be measured.
- Measure automatic terminal-provider circuit behavior under real credit/quota,
  authentication, configuration, and transient-failure conditions before
  adding operator reset, timeout, status, or persistence controls. The current
  circuit is process-local, per provider, automatic-only, has no timer, and
  resets only when the enrichment service stops and starts; manual success does
  not reset it. Now that focused and ambient Coordinators may use different
  providers, scope automatic circuit suppression and timer/deferred cancellation
  to the providers actually required by the selected mode instead of allowing
  an unused Coordinator branch to disable the other mode.
- Define a provisional-host materialization contract for strong host references
  observed before a pivot. A newly established hostname, address, or other
  stable system reference should create a minimal `hosts_by_name` entry early
  enough for the existing `host_discovered` trigger to begin read-ahead
  enrichment. Preserve the evidence source and provisional identity, avoid
  inventing platform or service facts, coalesce aliases, and do not register
  incidental examples or every row of noisy discovery output.
- Extend the reference contract with neutral world-scoped facts explicitly
  bound to the target host. Private bindings and final apply dependencies must
  distinguish host-relative and world-relative paths; Coordinator task text
  must not become an evidence substitute.
- Evaluate repeated layered enrichment so a bounded job can add one coherent
  causal cluster while later just-in-time or read-ahead passes deepen other
  surfaces. Per-job candidate and patch limits should control latency and race
  exposure without becoming a world-size limit.
- Evaluate the 24-candidate abundant ceiling as an artifact-throughput
  checkpoint rather than a target world size. Measure cap use, applied
  diversity, hierarchy, lifecycle texture, latency, cost, and depth. Preserve
  the separate latitude-sized relation contract. Improve opaque-reference
  copying and deterministic world-time context before attributing differences
  solely to model selection.
- Evaluate `foreground-evidence-v2` against chronology, negative evidence,
  expiry, and race cases. Its public recognizer exposes only bounded standalone
  host-time, home, login, session, socket, and process inventories. Private
  authority retains the latest recognized record per exhaustive scope. Verify
  expiry-only non-conflicts, genuine refresh, and terminal second-conflict
  handling before extending recognition to additional command shapes. Never
  treat malformed structured-response stdout as exhaustive authority.
- Add a deterministic current-state overlap prefilter before Integrator so
  exact already-materialized candidates do not spend Integrator tokens. Keep
  the check structural and explainable; semantic similarity across arbitrary
  prose remains a separate problem, and net-new detail for an existing subject
  must remain eligible.
- Evaluate one bounded retry when Expert returns no usable output because
  of an empty or malformed completion. Preserve the one-attempt Integrator-only
  refresh: genuine foreground-evidence conflicts reuse the original Expert
  report, recapture state and authority, pass normal safe apply, and stop on a
  second conflict. Active-host pivots remain terminal. Do not add an unbounded
  retry, optimistic bypass, partial-cluster salvage, or durable queue.
- Preserve concrete path and typed-artifact focus through coalescing, deferred
  replacement, one-shot release, and any future retry or requeue. Replacement
  should follow the winning candidate's current attention rather than retain a
  stale broad surface from the displaced candidate, and logs should retain only
  bounded counts, hashes, and decision fields.
- Measure foreground lock wait and hold occupancy alongside enrichment locks,
  not only enrichment's own short-lock timings. Correlate those deterministic
  measurements with command latency so a low model-call latency does not hide
  user-visible lock contention.
- Improve Langfuse job status and severity projection so role failures,
  rejected proposals, authority conflicts, and successful no-ops are visibly
  distinct without opening every trace. Derive the status from runtime outcomes
  and existing reason codes; do not ask models to narrate logs or spend output
  tokens on observability.
- Add explicit checkpoint checks for authentication and timeline coherence.
  Applied access, login, and audit material must respect direct active-session
  authority, while generated logs, rotations, transient activity, and residue
  should form a believable ordering relative to foreground observations. Minor
  plausible gaps remain preferable to flattening the world, but a direct newer
  observation must not be rewritten by speculative enrichment.
- Evaluate extending the initial syntax-derived `interaction_focus` with a
  bounded deterministic summary of concrete artifacts observed in command
  results. Preserve the distinction between user-visible output, accepted
  structured state, and a targeting hint; do not promote arbitrary output text
  into evidence or spend model output tokens producing the summary.
- Decide whether an unsuccessful foreground attempt should advance
  `surface_focus`. Failed inspection can reveal user intent, but repeated typos
  should not consume the automatic budget without measurement.
- Re-evaluate idle semantics if the five-second delay still overlaps materially
  with interactive programs. A future trigger may use explicit waiting-program
  state, but it must remain deterministic, reusable, and observable rather than
  asking a model whether the user is idle.
- Add surface-specific independent-Expert contracts where neutral generic
  output is too ambiguous to integrate safely. Filesystem work should return
  absolute paths and enough connected ancestor/leaf material to form a usable
  hierarchy; other surfaces should likewise retain the identifiers and
  relationships required for deterministic integration.
- Preserve useful lifecycle and relationship semantics in authoritative state
  instead of flattening all temporal, causal, ownership, dependency, and
  parent-child detail into isolated records or descriptive prose. Coordinate
  this with the explicit temporal lifecycle design below rather than granting
  deletion or correction authority implicitly.
- Provide each later enrichment job with a compact, deterministic summary of
  earlier job coverage and novelty so repeated passes broaden the world instead
  of repeatedly elaborating the same coherent cluster. Do not spend model
  output tokens generating this telemetry.
- Make the bounded reference catalog balanced or surface-aware before repeated
  layered enrichment can let large service, fact, or filesystem registries
  crowd other useful reference kinds out of the independent Expert brief.
- Design explicit temporal lifecycle semantics before any background agent can
  materialize entities that truly appear and disappear. Current safe enrichment
  may record intermittent or completed activity and its residue, but it has no
  expiry, stop, delete, or scheduled-transition authority and must not store a
  short-lived process as permanently current merely to create signs of life.
- Consider parallel Expert fan-out and reconciliation only after the sequential
  candidate-driven workflow has useful latency, novelty, and coherence data.
  Keep any expanded graph directly inspectable.
- Add broader host and world write surfaces only through explicit capability
  decisions. Do not infer correction, replacement, deletion, or cross-session
  authority from Enrichment's current additive host-local contract.
- Develop deterministic usefulness reporting that distinguishes a valid safety
  no-op from net-new usable material, redundant rematerialization, late work,
  and a contribution the user actually encounters. Distinguish a foreground
  surface attempt from reaching an enriched artifact and actually rendering
  enriched material; a command merely matching the same broad surface is not
  sufficient evidence of use. Keep this telemetry out of model output
  contracts.

## Next Feature: Foreground Structured-Output Reliability

- Investigate malformed foreground command-agent envelopes as a separate
  feature. Distinguish user-visible output from accepted structured state and
  assess downstream continuity when a proposed update is rejected.
- Use structured-failure capsules, raw provider responses, finish reasons,
  parser-only replay, exact pre-command state, prompt size, cache behavior, and
  output limits to distinguish provider-generation failures from parser-policy
  failures. Keep retry, repair, and salvage decisions evidence-driven.
- Treat this as a foreground-emulator reliability feature, not part of
  EnrichmentAgent. Enrichment may reveal the downstream cost of missing state,
  but it must not own command-envelope repair.

## Future Analyzer Agent

- Design an `analyze-*` console command suite for LLM-powered operator support,
  such as `analyze-session` and `analyze-command`. This should invoke a
  dedicated read-only analysis agent with broad access to the current
  environment, available state, Langfuse observations, traces, command history,
  and documentation. The analyzer should provide deep system insights to the
  caller without modifying Dreamland state.

## Direct Session-State Inspection

- Add a read-only console command for inspecting the current in-memory state of
  a live session without first saving a temporary state profile. It should
  target either the selected session or an explicit session ID, offer full
  machine-readable JSON and useful section filtering, and snapshot state under
  the session lock. The command must show state as it exists at inspection time,
  including asynchronous background-agent changes made after the most recent
  foreground command, rather than substituting Langfuse's last command state.
  Reuse the shared admin-service state projection where practical so console,
  REST, and MCP inspection remain consistent.

## Admin API And MCP

- The local admin interface now has a shared protocol-neutral service layer,
  optional same-process REST API, and MCP binding as described in
  `docs/ADMIN_INTERFACE.md`.
- Keep expanding REST and MCP coverage as console-equivalent capabilities grow.
  Mutating MCP tools must remain behind an explicit configuration gate so
  agents can inspect by default without receiving model-invokable write
  authority.
- Add server-sent event streams for live logs, traces, and runtime events after
  read-only snapshot endpoints are stable.
- Keep browser-native product sessions out of the Operator/admin interface.
  Build them through the separate default-off private product service and
  UI-owned BFF boundary in decision 0050.
- Revisit whether a local observability store is needed if Langfuse-backed
  historical inspection limits UI or agent workflows.
- Consider a future stdio MCP bridge for clients that prefer subprocess MCP
  servers, with the bridge connecting to the running local admin server instead
  of owning DREAMLAND runtime state.

## Private Product API And Browser Sessions

Every product phase should leave the UI team a pinned backend commit,
machine-readable artifacts and digests, canonical and failure fixtures, a
server-side generated-client smoke test, and an explicit integration target.
Incomplete production capabilities remain disabled and fail closed.

- Phase 0 establishes the default-off composition seam, exact five-capability
  manifest, `0.1.0-rc.1` artifacts, validator, and manual loopback simulator.
  It has no product listener or session runtime; explicit activation fails
  startup closed until a later phase supplies a complete lifecycle.
- Phase 1 supplies an integration-only lifecycle and authority preview:
  protocol-neutral implementation, an unfrozen `0.2.0-rc.2` kit, and an
  explicitly invoked loopback target. It covers immutable
  selected revisions, actor and grant isolation, service epoch, idempotent
  create/current/snapshot/close, expiry, tombstones, and restart-loss truth.
  `product_session_core` remains disabled because this is not a live or complete
  shared core.
- Phase 2 now provides an explicitly constructed, protocol-neutral shared-core
  integration checkpoint: the product-only command classifier, sole
  asynchronous command slot, provisional checkpoint/state boundary, terminal
  publication fence, exact cancellation races, bounded transcript/event and
  snapshot recovery, atomic replacement, product enrichment coordination, and
  product-only capacity/budget accounting. Its deterministic loopback preview
  performs no provider calls and all capability claims remain disabled. Enable
  `product_session_core` only after a real product provider adapter proves
  attempt enforcement and cleanup, deployment proves non-overcommitting
  provider isolation and hard limits, the private gateway and UI complete
  compatibility evidence, and the full core gate passes.
- Phase 3 now adds an explicitly constructed Presenter Stage integration
  checkpoint: exact-grant launch and identical-selection resume, explicit
  expected-control-CAS replacement, safe display metadata, and a hard Stage
  partition within the product admission totals. Its deterministic loopback
  target performs no provider work and `presenter_stage_terminal` remains
  disabled until the live gateway, provider, deployment, load, and Gate 2
  evidence pass.
- Phase 3D adds a manually invoked, loopback-only live-development target over
  that same Stage contract. It runs the configured DREAMLAND command agent and
  provider so maintainers can evaluate the UI against real model-backed,
  stateful commands before World Forge work begins. It uses owner-only BFF and
  launch bootstrap files, conservative full-reservation accounting derived
  from a required finite provider maximum, and an additive `0.4.0-rc.2` kit.
  Blank or zero output configuration fails before provider/listener
  construction; the published kit remains immutable. All product capabilities
  remain disabled; this checkpoint is developer feedback, not Gate 1, Gate 2,
  deployment, or presentation evidence.
- Phase 4 now includes an inert protocol-neutral WorldSeed/lifecycle foundation,
  the immutable provider-free RC2 contract/loopback target, and a corrective RC4
  headless live vertical slice. The explicit RC4 process performs one configured
  Spiritguide attempt, deterministic validation and synthetic boot, immutable
  content-addressed storage, invariant source projection, and atomically
  deadline-fenced receipt selection. RC1 through RC3 remain immutable.
  Remaining checkpoints add durable jobs, hard provider-call deadlines,
  bounded redundant attempts, repair, fixed multi-attempt selection, and
  shared provider-budget enforcement.
  Phase 4 does not itself enable a session capability.
- Phase 5 now provides a capability-disabled development checkpoint from an
  authoritative live RC4 selection through exact content-addressed artifact
  hydration into a dynamic-only private Presenter Stage. The additive
  `0.5.0-rc.1` kit pins RC4 exactly, retains the existing terminal surface,
  reports exact Forge identity under `stage.source`, supports identical
  selection resume and explicit CAS replacement, and has a manually invoked
  two-listener live evaluator. It does not enable `forge_to_stage_handoff`:
  full Phase 4 durability, hard provider-call deadlines, A/B/C attempts and
  repair, fixed selection, shared Forge/Stage provider-budget enforcement, and
  the live Forge, Stage, gateway, deployment, and acceptance gates must still
  pass together.
- Phase 5.1 now implements the required corrective closeout before Phase 6.
  Strict UI integration found that RC1's canonical replacement response labels
  the old session `replaced` while retaining active controls, an available
  command slot, and non-tombstone recovery. The corrective RC2 generator,
  schema, generated client, and owning tests preserve published RC1
  byte-for-byte and enforce a runtime-faithful terminal tombstone,
  request-relative CAS, terminal-event advancement, and complete old/new Forge
  source identity. Existing RC1-pinned initial handoff, exact replay/resume, and
  ordinary terminal UI evidence remains useful, but current backend tooling is
  repinned to published RC2 and explicit replacement remains hidden until the
  UI consumes `0.5.0-rc.2` and restores strict decoder coverage. This
  provider-free correction adds no route, provider work,
  normal-runtime cost, or capability claim.
- Phase 6 adds the allowlisted dual-position Stage X-ray projection and enables
  `stage_xray` independently after privacy, consistency, and recovery tests. It
  begins from the corrected Phase 5.1 snapshot baseline and does not absorb the
  replacement correction into X-ray scope.
- Phase 7A now adds a capability-disabled private Presenter integration
  checkpoint before public admission. The additive `0.7.0-rc.1` contract lists
  bounded authored and authoritative-Forge launch options and exchanges a
  non-authorizing opaque selection reference for BFF-held, actor-bound Stage
  authority. A trusted startup importer snapshots a configured authored world
  behind an exact digest pin into owner-only content-addressed storage. The
  source-neutral exchange provisions either closed origin into dynamic Stage
  for create or explicit switch without accepting profile paths, names, raw
  state, revision coordinates, or caller-created grants. Its deterministic
  preview uses authored and synthetic Forge-origin fixtures and performs no
  provider work. A later additive live target must combine configured authored
  import, authoritative Forge registration, shared display truth, and runtime
  codec dispatch. All capabilities remain disabled, and
  normal SSH/runtime composition incurs no import, object, listener, provider,
  reservation, bookkeeping, or cost.
- Phase 7B now supplies the additive, explicitly started provider-backed live
  target anticipated by Phase 7A. The `0.7.0-rc.2` identity keeps the same 21
  routes and BFF/browser authority split while truthfully reporting live
  provider potential. Startup imports one operator-configured authored world
  only through its exact metadata-free digest pin, and an authoritative Forge
  observer registers a generic option only after selected truth commits and
  the exact immutable artifact is reopened. A closed kind-dispatching runtime
  codec lets either source use the same live command agent, private Stage
  create or explicit switch, terminal, and X-ray. The legacy Forge handoff
  remains compatible, all capabilities remain disabled, and import or normal
  DREAMLAND startup still incurs no product/provider cost.
- Phase 7C now adds a capability-disabled, provider-free authored-world
  attendee checkpoint: bounded private gateway-context bootstrap,
  authored-only notice-bound selection, per-context lifecycle isolation,
  standard admission, dual-clock expiry, and safe projections through the
  shared core. It gives the UI-owned `/session` BFF a deterministic integration
  target but does not yet enable `public_attendee_sessions`, dispatch a live
  attendee agent, or establish production gateway/load/deployment evidence.
- Phase 7D now adds the separately invoked provider-backed authored-attendee
  development checkpoint. The identity-only `0.7.0-rc.4` contract preserves
  all 24 RC3 routes and wire semantics while truthfully identifying live
  command dispatch. One combined loopback process retains live Forge,
  source-neutral Presenter Stage terminal/X-ray, and an authored-only attendee
  path. Attendee commands use a separate executor/checkpointer with external
  tracing forced off and the ledger's bounded standard allocation; the existing
  Presenter reservation boundary remains intact. Presenter and attendee token
  reservations and budgets independently scale to cover their finite configured
  provider maxima, and startup exposes those bounds before dispatch. Dual-clock
  admission cutoff and shutdown fence attendee provider work while retaining
  bounded terminal current-session recovery. Every capability remains disabled,
  generated Forge revisions remain attendee-ineligible, and normal DREAMLAND
  startup imports or pays for none of this composition.
- Phase 8A now adds the separately authorized, provider-free generated-world
  promotion, explicit reveal, bounded catalog, and attendee-selection
  checkpoint. The corrected `0.1.0-rc.3` World Access contract pins Product RC3
  and World Forge RC4 without changing either published surface, while the
  reviewed RC1 and published RC2 trees remain immutable. RC3 retains RC2's
  lifecycle, retry, actor-class, media-type, and exact six-operation client
  guarantees and adds output-only launch-binding correlation for ambiguous
  Product create recovery.
  Hidden access
  truth must publish successfully before reveal commits; exact catalog and
  notice evidence produces an internal eligibility proof and then an ordinary
  Product attendee grant. The co-hosted preview uses one truthful notice and
  evaluator-current monotonic time. Bare Forge/Presenter offerings remain
  ineligible, existing Product attendee routes remain authored-only, all
  capabilities stay disabled, and normal DREAMLAND startup incurs no product
  cost.
- Phase 8B explicitly composes the existing live Forge selected-artifact source
  adapter, World Access, live attendee executor, and Product session service
  under shared World Access/Product epoch and authority truth while preserving
  the Phase 8A boundary. Its RC4 identity reports provider/live evidence
  truthfully, and human evaluation retains `--max-commands -1` without
  disabling any other finite safeguard. The process uses truthful retained-
  lifetime accounting for sixteen generated publications: expired entries
  leave the visible catalog but are not reclaimed while issued or replayable
  authority may depend on them. The complete provider-backed backend smoke has
  passed through generated attendee command completion and clean shutdown.
  Load, restart, cursor-loss, cutoff, privacy, bundle-boundary, canary,
  rollback, browser, and presentation rehearsals remain gates for only the
  capabilities a deployment claims. A freshly forged Phase 7A Presenter option
  remains neither promotion nor attendee-eligibility evidence.
- Phase 9A adds the provider-free private-attendee security checkpoint. Product
  `0.8.0-rc.1` preserves all RC4 operations and adds BFF-authorized,
  epoch-bound, idempotent gateway-context revocation plus the retryable
  `logical_rate_limited` result. An optional bounded ledger charges only fresh
  bootstrap, grant exchange, create, submit, reset, and switch after existing
  replay/conflict lookup. Cancel, close, revoke, reads, expiry, shutdown, and
  reconciliation remain available under saturation. A separate loopback
  attendee-only target excludes Stage, X-ray, Forge, World Access, Operator,
  admin, MCP, provider, and profile authority. Browser security and public-edge
  limits remain UI/BFF work; all production capabilities remain disabled.
- Phase 9B now adds the production-shaped, attendee-dedicated live provider
  checkpoint behind the Phase 9A private boundary. Published Product
  `0.8.0-rc.2` remains immutable. Corrective `0.8.0-rc.3` preserves RC1/RC2's
  25 routes, revocation, idempotency, deliberate `access-ended` mapping, and
  explicit restart behavior while selecting the existing reviewed
  `notice:attendee-live-development-v1` and
  `policy:attendee-live-authored-v1` for provider-backed attendee commands.
  RC1 and RC2 remain immutable and all five production capabilities remain
  disabled. The explicit loopback target pins one authored revision and uses a
  fixed spawn-process pool whose children alone read an owner-only exact-`0600`
  provider key.
  Ambient provider/tracing credentials are refused, external tracing is off,
  provider retries are zero, command text is redacted from token/resource logs,
  and Stage/Forge/World Access/Operator authority is absent. Complete local
  worker readiness precedes listen, but does not prove upstream reachability or
  quota. Retained logical-job and resource budgets derive from active-session
  capacity times the configured command maximum, avoiding a hidden eight-job
  process quota. Dispatch covers startup and old-child containment; core and
  pool enforce one byte-and-structure state policy before commit. Ambiguous
  work keeps its full reservation, work is never automatically redispatched,
  and drain is always followed by bounded shutdown. The replacement release
  gate now derives its test wait from startup and containment bounds, requires
  complete idle worker readiness plus a successful follow-up dispatch, and
  changes no runtime timeout. Local child termination
  fences DREAMLAND publication but does not prove upstream cancellation or
  billing termination.
- Phase 9C now begins with the wire-compatible, capability-disabled Product
  `0.8.0-rc.4` supervised availability checkpoint. The separately invoked
  loopback target accepts one exact UTC open, admission-close, and
  recovery-close window projected into the service's monotonic domain. A
  no-request watcher expires and drains at admission close while retaining a
  finite terminal-recovery listener; recovery close or `SIGINT`/`SIGTERM`
  performs one ordered listener, request-thread, service, provider-pool, and
  task-thread convergence. Credentials rotate only across stop/replace/start
  and a fresh epoch. RC1 through RC3 remain immutable, the UI/BFF typed
  deliberate `access-ended` result is not added to the backend wire, and
  restart still requires explicit context-start. The rest of Phase 9C owns
  deployed load, rollback, and provider-account/quota evidence.
- Phase 9C also adds a provider-free, real-process RC4 rehearsal for the
  private loopback boundary. It drives the immutable generated client plus raw
  unread-body/pipelining and stalled-header probes through the real supervised
  server and fixed two-worker process pool, certifying the current
  four-context, four-session, and eight-connection local limits,
  cancellation/cutoff fencing, recovery, signal convergence, and
  stop/replace/start transport-authority rotation. Exact provider-deadline
  handling preserves cancellation-first interrupted/actual-usage truth while
  deadline-first cooperation retains full-reservation deadline accounting and
  a reusable worker. Its allowlisted success report is explicitly
  `loopback_provider_free` and never production-capability evidence. It also
  closes unread-body HTTP/1.1 connections before their bytes can be reparsed
  and accounts for both connection deadlines possible during saturated
  shutdown.
- Phase 9C also adds an independently runnable,
  explicitly acknowledged, single-command live-provider canary around that
  same immutable RC4 target. It fixes the deployment at loopback, an ephemeral
  port, one worker, one accepted `uname -a`, one logical provider attempt, and
  a canary-only maximum of 8,192 output tokens plus an effective 240 output
  chunks per command while leaving the supervised target defaults and ranges
  unchanged. The token bound limits the one authorized provider response; the
  independent 240-chunk bound keeps accepted-cursor replay
  within the 256-event journal after lifecycle and terminal overhead. It then
  validates complete event/transcript recovery, closes, revokes, and shuts
  down. Its allowlisted report states 65,536 output bytes and 240 output chunks
  per command and verifies exact settled token and spend usage equal their
  announced full reservations. This is live execution evidence for that
  single command run and always sets `production_capability_evidence=false`.
  It does not establish actual provider billing or token truth, upstream
  request-count instrumentation, account/quota separation, cancellation or
  billing termination, deployed load, rollout/rollback, the public edge or UI/BFF
  browser-security boundary, or production readiness.
- Phase 9C-D adds an opt-in bounded capacity envelope to the attendee live and
  supervised tools without changing their defaults or Product RC4. Hard maxima
  are 64 epoch-lifetime contexts, 64 concurrent sessions, eight provider
  workers, and 32 admitted connections. Logical jobs, token/spend budgets,
  launch rate, actors, grants, tombstones, request tracking, and related
  idempotency stores all derive from the selected values and are announced at
  startup. Focused provider-free tests now pass 20 context/grant/session
  lifecycles, typed overflow and cleanup, plus five-command saturation and
  exact-request recovery. The accepted fixed subprocess rehearsal and
  one-command live canary retain their narrower evidence scopes; measured
  sustained real-process load remains the next backend gate.
  Public-edge/BFF security, real deployed load, provider-key rotation,
  upstream cancellation/billing, rollout/rollback, and provider-account/quota
  evidence remain open. Phase 9D may enable
  `public_attendee_sessions` only after those backend gates and the UI-owned
  same-origin cookie, CSRF, Origin, Host, DTO, public-throttle, and
  browser-recovery gates pass together.

The presentation-critical work now has development Forge-to-Stage, X-ray,
provider-free and live generic authored/Forge Presenter-selection paths,
provider-free plus provider-backed authored attendee-session paths, the live
generated-world promotion/catalog-to-session path, and an attendee-only private
security target with a dedicated live provider pool for UI integration.
Remaining Forge/Stage production gates and rehearsal still follow their
unfinished predecessors. Production public-attendee delivery remains
independently gated and need not weaken Stage capacity or the ordinary SSH
experience.

## Spiritguide Phase Carry-Over

The initial spiritguide is now a usable operator state-guidance agent. Remaining
spiritguide-related work should focus on operator inspection, controlled state
replacement, and stale-history handling:

- Make Spiritguide world-building a first-order admin-shell workflow built
  around detached state profiles. Operators should be able to create a new
  world-level state profile from a blank or neutral world state, update that profile through
  follow-up Spiritguide prompts, and optionally fork it into another profile
  before applying it to live or future sessions. The default saved profile level
  should be `world`, with registered world artifacts preserved in `world_state`
  and session-specific details such as cwd, shell stack, and command history
  excluded unless a later explicit system-profile workflow needs them. This
  detached workflow supersedes a live-session blank-slate retheme as the main
  path for broad new-world creation, so prior scenario facts, artifacts,
  services, users, and network assumptions cannot leak into the new world.
- Make spiritguide actions inspectable from the operator console, including
  instruction, model input/output, parsed patch, dry-run status, and apply
  result.
- Add explicit world-state supersession, replacement, deletion, or tombstone
  semantics so operator rethemes can remove stale incompatible facts.
- Teach spiritguide and the command emulator that newer operator corrections
  outrank older terminal history without rewriting historical command output.
- Enhance Spiritguide's profile-authoring instructions so new worlds can be
  pre-seeded with a coherent baseline of historical life, other identities,
  ordinary operational residue, log history, and connection context before a
  user enters. This is a separate line of effort from asynchronous enrichment:
  the current checkpoint accepts a short warm-up after session registration,
  while profile pre-seeding should eventually make the first command feel
  populated. Explicit operator direction for solitude, abandonment,
  minimalism, pristine operation, or another incompatible setting must take
  precedence over the general population preference.
- Consider richer review/apply workflows after the basic state contract is
  stable, such as proposed patches, operator-edited patches, or persisted
  snapshots.

Adjacent findings from spiritguide live testing should carry into future
command-agent and simulation work, not block the initial spiritguide:

- Manually evaluate the emulator's new connected-world, organizational
  continuity, and shared addressing prompts. Independent emulator generation
  now has defaults even without Spiritguide directives; verify discovery beyond
  the seed, onward traversal, recognizable owners and purposes, address/domain
  variety, and consistency after revisits and save/reload. Include explicit
  isolation and older profiles with established identities. If prompt guidance
  still produces terminal networks, identity drift, or allocation collisions,
  scope runtime topology/allocation support separately. Follow the checkpoint
  in [decision 0090](docs/decisions/0090-emulator-connected-network-continuity.md).
- Evaluate the shared geographic guidance during continued exploration:
  real places, coherent site inheritance, distinct headquarters/hosting/service
  areas, and visible operational evidence that survives revisits and saving.
  Check newly invented facilities beyond the initial seed and explicit
  fictional-world overrides. See
  [decision 0091](docs/decisions/0091-shared-geographic-grounding.md).
- Static state coherence needs a focused pass. Prompt, hostname, active shell
  environment, `hostnamectl`, nested SSH state, and command-agent metadata must
  agree after state mutation or nested-host transitions.
- Shell-level state mutations from spiritguide now have baseline automated
  coverage for cwd, interactive program, shell environment, and shell-stack
  transitions through the native operator console. Prompt rendering and broader
  live behavior still need manual validation before prompt policy encourages
  shell-state mutation broadly.

## Terminal And Shell Simulation

- Implement bash PS1 semantics after environment state exists. Program prompt
  templates are currently supported, but full bash prompt expansion such as
  `\u`, `\h`, `\w`, `\$`, colors, and nonprinting prompt markers is deferred.
- Extend the existing interactive program stack beyond input-driven shells,
  REPLs, and authentication prompts. A July 21, 2026 GPT-5.6 Terra command run
  recognized that an unbounded Linux `ping` remained active and pushed ping
  onto the stack, but the runtime cannot yet schedule time-driven output,
  preserve an explicitly empty foreground prompt, or route an idle `Ctrl-C` to
  that frame. Define a foreground-process lifecycle or a deliberate bounded
  fallback before broadly encouraging this state strategy.
- Decide history scope rules. Bash history should likely survive a temporary
  switch into another program, while other programs may need their own histories.
- Add real program-specific editors beyond bash/readline. Redis, Python REPL,
  database clients, pagers, and text UIs have different prompt, multiline, and
  control behavior.
- Add multiline input handling for bash continuations, heredocs, quoted strings,
  Python blocks, and other REPL continuation prompts.
- Improve root prompt rendering. Nested root shells currently use the configured
  prompt template; decide whether root should render `#` via bash PS1 semantics
  or shell-frame metadata.
- Expand shell stack coverage beyond `sudo -i`: add live and unit coverage for
  `su -`, launching nested `bash`/`zsh`, nested SSH sessions, repeated shell
  nesting, and returning via `exit`, `logout`, and `Ctrl-D`.
- Make locally processed `cd` filesystem-aware. The current built-in updates
  `cwd`, `PWD`, and `OLDPWD` without validating simulated path existence,
  permissions, or generated world artifacts, which can temporarily place the
  shell in a path the world has not established.

## Shell Variables Feature Follow-Ups

- Support command-local assignments for arbitrary commands, not just
  `env` and `printenv`, e.g. `FOO=bar python app.py`.
- Add formal device profile schema and documentation for initial environment
  variables.
- Tighten shell-specific behavior when `interactive_program` is not bash.
- Decide whether `export -n VAR`, `readonly`, `declare -x`, and invalid
  identifier edge cases need fuller bash compatibility.
- Run manual SSH smoke testing with the live LLM backend before merging.

## Readline Control Gaps

- Add meta/alt word movement and deletion controls.
- Add tab completion. This will need simulated filesystem, command, variable,
  and program-specific completion sources.
- Add quoted insert.
- Add full kill ring behavior, including kill-ring rotation.
- Add word-case transforms.
- Add `ctrl-x ctrl-e` editor launching behavior once simulated editor/program
  interactions exist.

## Agent And Simulation State

- Add durable graph checkpoint storage for long-running or resumable sessions.
- Investigate static state coherence after spiritguide and nested-session
  mutations. Exemplar Langfuse trace:
  `7c4c67c8852e4fe6ae4f00056816de8d`
  (`2026-06-14T12:02:01+00:00`, default). In that session the shell
  environment and prompt indicated `display-81`, while command-agent metadata
  still carried `dreamland.hostname=ubuntu` and `hostnamectl` later returned
  `Static hostname: ubuntu`.
- Fix remote/nested-host hostname propagation. The same exemplar had a good SSH
  transition into `display-81`, but later static hostname handling drifted back
  to the original Ubuntu host, creating visible emulator confusion.
- Make invalid commands fail faster. If the model can confidently identify a
  command as invalid, it should produce the appropriate terminal error directly
  instead of spending reasoning budget exploring the user's likely intent. In
  the exemplar, `tcpdump -nnr display81-tlm-1969-07-20T2018Z.pcap -n 50`
  correctly ended with `tcpdump: can't parse filter expression: syntax error`,
  but the reasoning summary spent multiple paragraphs debating whether the user
  intended `-c 50`, whether `50` was a filter, and whether an affirmative output
  might be expected.
- Structured failure capsules now capture malformed structured model responses
  with raw response text and exact pre-command state for parser replay. Console
  and MCP inspection now expose capsule list, detail, and parser-only replay.
  Continue this work by adding pre-command state replay, fixture export, and
  evidence-driven retry/salvage policy.
- Prevent malformed structured model responses from leaking DREAMLAND internals
  into terminal stdout after the failure corpus is better understood. Exemplar
  Langfuse trace:
  `9d704edba16d43fea92f71b29a51602a`; near the end, `sudo rm systemd-helper`
  returned a JSON-looking response containing `stdout`, `stderr`, `exit_status`,
  and `state_update` as literal SSH output. The parser rejected it as malformed
  structured JSON, and the fallback path emitted the raw model response to the
  user. Failed structured parsing should produce safe terminal behavior and
  observability metadata, but never expose internal state envelopes.
- Investigate interactive-program state drift around full-screen or persistent
  commands. In the same `9d704edba16d43fea92f71b29a51602a` trace, commands
  after `top` showed confusing mixed behavior: shell commands advanced cwd while
  later output still resembled `top` interaction. This is a strong use case for
  the future `stack` console command and stricter interactive-program handling.
- Tighten structured state delta validation. The current structured model
  response parser validates the top-level envelope and repairs common shell
  stack issues, but future work should reject or clearly diagnose impossible
  stack transitions, malformed frames, and invalid field types.
- Add structured tool calls for filesystem, network, and command simulation.
- Expand world-state schema and state profiles so they can represent multiple simulated hosts,
  services, user accounts, trust relationships, and generated scenario seeds.
- Add exact state-profile support for terminal editor recall history. Exact
  profiles currently preserve command/result history for the command agent, but
  not the readline up-arrow/down-arrow buffer held by live shell editors.
- Design a more robust local storage layer for DREAMLAND runtime data. The
  current state-profile session index is a small JSON file, but future work may
  need local persistence for automatic state snapshots, command streams,
  reasoning outputs, and other observability or replay data that is currently
  available only through Langfuse or in-memory logs.
- Explore LLM-assisted state-profile optimization after deterministic saves are
  stable, including human-friendly summaries, pruning, and semantic
  generalization of large generated artifact sets.
- Optimize long-running agent sessions so history can be summarized, trimmed, or
  selectively omitted instead of sending a continuously growing context window.
- Add configurable command/model-call timeouts for provider calls that stall
  before returning output or an exception. A manual test on June 11, 2026 saw
  `ls -la /opt` wait for about 301 seconds before the provider returned an
  `APIError`; streaming provider exceptions now return terminal errors, but
  proactive timeout policy remains deferred.
- Decide whether common profile-known filesystem commands such as `ls` need
  deterministic handling or a retry policy when provider calls fail or stall.
- Run a final live LLM SSH smoke test for `sudo -i`, `id`, `whoami`, `pwd`,
  `env`, `exit`, `Ctrl-D`, and closing the parent shell before considering the
  structured shell-state feature complete.

## Documentation And Operations

- Keep `docs/DOCUMENTATION.md` as the documentation schema for new docs.
- Expand the Inspect AI `review` command so it can compare recent runs across
  spec, model, profile, score dimensions, pivot quality, and structured-failure
  counts without rerunning the unwitting agent.
- Build a richer Inspect AI result dashboard after the CLI review workflow is
  stable. It should consume existing run directories rather than rerunning
  agents, compare runs by spec/model/profile, surface `score.json`,
  `pivot_quality`, `structured_failures.json`, agent reports, command timelines,
  and artifact links, and preserve the current trust boundary: the dashboard is
  for maintainers after the run, not information shown to the unwitting agent.
- Add formal schema validation for device profiles and future world/network
  profiles.
- Keep optional Langfuse tracing well supported without making it required for
  local development or distribution.
- Add a native console `stack` command that shows a compact view of the active
  Dreamland shell/program stack for a session, including shell frames,
  interactive programs, important environment variables, cwd, prompt-relevant
  identity, and other state needed to explain what the emulator believes is
  currently active.
- Expand the native Dreamland backend console `inspection` command after the
  world-state feature set is complete. It currently inspects one command target.
  Add support for multiple explicit command identifiers, relative targets,
  command ranges, and `@` as a shorthand for all commands in the selected
  session. The command should preserve existing options such as `--prompt` and
  apply the requested inspection output consistently to every selected command.
- Fix `logging -f` rendering in the native Dreamland backend console after the
  world-state feature set is complete. A manual test on June 11, 2026 showed
  followed log output preserving unusual cursor/line offsets: new log entries
  began far to the right, long Paramiko lines wrapped into odd columns, and some
  logger names split across lines, e.g. `dreamland.ssh` followed by
  `_server.paramiko.transport`. Investigate whether prompt-toolkit output,
  carriage-return handling, wrapping, or concurrent console prompt rendering is
  leaving the terminal cursor away from column 0 before writing follow-mode log
  lines.
