# Documentation System

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: define how project documentation is organized and how new docs should
be written.

## Documentation Map

- `README.md`: short entry point, setup, run, test, and links.
- `docs/PROJECT_MODEL.md`: product intent and long-range project model.
- `docs/ARCHITECTURE.md`: runtime components and module boundaries.
- `docs/OPERATIONS.md`: configuration, running, Docker console access, model
  adoption, logging, and tracing.
- `docs/BACKEND_DEPLOYMENT_UI_HANDOFF.md`: the private API, credential, and
  compatibility handoff planned for a separately deployed UI companion.
- `docs/SSH_LOBBY.md`: optional native SSH lobby configuration, catalog,
  notices, visit/reference lifecycle, and manual operator checkpoints.
- `docs/SSH_LOBBY_DEVELOPMENT_PLAN.md`: agreed native lobby scope, implementation
  phases, and mandatory direct/independent security assessment.
- `docs/SSH_LOBBY_SECURITY_AUDIT.md`: independent adversarial review, fixes,
  retests, and remaining release checkpoint.
- `docs/SSH_LOBBY_REHEARSAL.md`: manual terminal validation.
- `docs/SSH_LOBBY_TRACE_CHECK.md`: reusable tracing integration procedure.
- [`docs/SSH_LOBBY_PRESENTATION_CHECKPOINT.md`](SSH_LOBBY_PRESENTATION_CHECKPOINT.md):
  accepted Phase 1 terminal presentation evidence, isolated provider-free
  preview, and human client/settings checks with remaining matrix gaps.
- [`docs/SSH_LOBBY_PRESENTATION_PHASE2_CHECKPOINT.md`](SSH_LOBBY_PRESENTATION_PHASE2_CHECKPOINT.md):
  static welcome banner, fit/fallback and copy checks, simpler continuation
  wording, and human acceptance before the Phase 2 commit or introduction work.
- [`docs/SSH_LOBBY_PRESENTATION_PHASE3_CHECKPOINT.md`](SSH_LOBBY_PRESENTATION_PHASE3_CHECKPOINT.md):
  centered two-second multicolor reveal, timing/input evidence, isolated motion
  preview, accepted default enablement, and remaining human client-matrix gaps.
- `docs/ENRICHMENT_AGENT.md`: default session-local background enrichment
  contract, triggers, graph, safe patching, and observability.
- `docs/MODEL_FIT.md`: packaged model preferences and configuration overrides.
- `docs/ADMIN_API_V0.md`: concrete local admin REST contract for `/api/v0`.
- `docs/MCP_MODEL_EVALUATION.md`: per-session profiles, independent provider/model
  settings, detached Spiritguide overrides, and evaluation run evidence.
- `docs/PRODUCT_SESSION_API.md`: default-off private product composition,
  integration-only lifecycle, shared-core, Presenter Stage, and authored
  attendee preview/live and private-security targets, release-candidate BFF
  contract artifacts, and agreed later session behavior.
- `docs/PRODUCT_STAGE_LIVE_DEVELOPMENT.md`: explicit local live-agent Stage
  startup, credential handoff, derived token accounting, limitations, and UI
  evaluation workflow.
- `docs/FORGE_STAGE_LIVE_DEVELOPMENT.md`: explicit live World Forge-to-Stage
  startup, dual-service authority handoff, limits, and UI evaluation workflow.
- `docs/PRODUCT_LAUNCH_SELECTION_LIVE_DEVELOPMENT.md`: explicit live generic
  authored/Forge selection startup, pinned import, and BFF evaluation workflow.
- `docs/PRODUCT_ATTENDEE_LIVE_DEVELOPMENT.md`: explicit combined live
  authored-attendee, Forge, and Presenter startup, isolated/scaled token
  accounting, cutoff, and BFF evaluation workflow.
- `docs/PRODUCT_ATTENDEE_SECURITY_DEVELOPMENT.md`: provider-free and
  credential-isolated live attendee-only private BFF targets, context
  revocation, replay-safe logical admission, dedicated process capacity,
  supervised deployment, its bounded live-provider canary, and the remaining
  public-deployment boundary.
- `docs/PRODUCT_WORLD_ACCESS_DEVELOPMENT.md`: provider-free and explicitly
  started live generated-world promotion, reveal, attendee catalog, and
  Product-compatible launch-selection integration workflows.
- `docs/WORLD_FORGE.md`: separately versioned World Forge seed, lifecycle,
  isolation, and phased implementation boundary.
- `docs/AGENT_STATE.md`: command-agent contract and state semantics.
- `docs/NETWORK_CONNECTIONS.md`: simulated SSH endpoint identity, per-host socket
  observations, and explicit source/destination translation facts.
- `docs/STRUCTURED_FAILURES.md`: structured command-agent failure capture and
  parser replay.
- `docs/STATE_PROFILES.md`: state profiles, host metadata, active host, and
  prompt authoring guidance.
- `docs/INSPECT_AI_HARNESS.md`: optional Inspect AI evaluation harness,
  run specs, artifacts, and scoring interpretation.
- `docs/TESTING.md`: test layout, test speed expectations, live test policy.
- `docs/DEVELOPMENT_HISTORY.md`: factual project evolution from git history.
- `docs/INDEX.md`: preferred reading order and documentation navigation.
- `docs/decisions/`: architecture and process decision records.
- `ROADMAP.md`: planned work and deferred decisions.
- `AGENTS.md`: instructions that coding agents must read before editing.

Research evidence, study reports, and campaign-specific collection, grading,
and presentation scripts are published separately. This source tree retains
generic evaluation tools and their usage documentation.

The [contract inventory](../contracts/README.md) lists retained backend
artifacts. The [developer tools review](../tools/README.md) records what remains
useful without the separate UI and identifies UI-specific removal candidates.

## Document Schema

Every durable Markdown document under `docs/` should use this structure unless a
short reference table would be clearer:

```md
# Title

Status: Draft | Active | Historical
Audience: one or more intended readers
Purpose: one sentence explaining why the document exists.

## Overview

The stable context a reader needs before details.

## Details

The concrete behavior, rules, commands, interfaces, or decisions.

## Maintenance

When to update the document and which code paths or workflows it tracks.
```

Use `Status: Active` for docs that should stay current with code. Use
`Status: Historical` only when the doc intentionally records past state and
should not be rewritten as current behavior.

## Writing Rules

- Prefer direct, verifiable statements over aspiration.
- Distinguish current behavior from future intent.
- Link to source files when a doc describes a module boundary or contract.
- Keep `README.md` concise. Move detail into `docs/`.
- Keep examples sanitized. Do not include local keys, secrets, private hosts, or
  user-specific config values.
- Update docs in the same change as behavior, configuration, prompt, or test
  policy changes.
- If the project model changes, update `docs/PROJECT_MODEL.md` first and then
  align the narrower docs.

## Coding Agent Expectations

Coding agents should read `AGENTS.md`, this document, and any topic doc tied to
the files being edited. If a change affects runtime behavior, also check whether
`README.md`, `ROADMAP.md`, or a topic doc needs to change.

For documentation-only changes, run focused tests that validate docs-adjacent
assets when possible. For code changes, follow `docs/TESTING.md`.
