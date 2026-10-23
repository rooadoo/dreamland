# 0085 Isolated MCP Evaluation Runtimes

Status: Active
Audience: DREAMLAND maintainers, operators, and testing agents
Purpose: explain per-session model configuration and connection-preserving profile application.

## Overview

After the provider-control preparation in `7d1386e` and a successful live MCP
command smoke test, review found two gaps. MCP session creation could only use
the process default profile. Applying a different profile afterward replaced
the session's login, MCP origin, tags, and other run metadata. Model settings
were readable through MCP but could only be changed at process startup.

The operator authorized closing these gaps before conducting A/B tests.

## Details

### Immutable Run Configuration And Provider Ownership

Add optional `state_profile` and `config_overrides` to admin MCP session
creation. Resolve overrides against startup settings without rereading
environment variables or writing the INI file. Reject unknown fields and
invalid types before constructing agents or registering sessions. A role's
`model` remains a `provider:model` reference using existing provider adapters
and server-owned credentials. Optional null fields clear inherited settings;
changing the model does not reset unrelated controls.

The override object mirrors the command, Spiritguide, and enrichment settings.
All four enrichment roles and the existing scheduler's principal controls can
be configured independently. Prompt modules, credentials, logging, and listener
settings remain process-owned. Runtime settings are fixed at creation; another
variant requires another session. No hot mutation of shared model objects or
global model defaults is introduced.

An explicit object, including `{}`, constructs a private command graph,
Spiritguide, enrichment graph, event bus, and enrichment scheduler. The shared
live-session registry holds the runtime alongside its existing session lock.
Registration and closing events go to that runtime's bus; command events use
its backend. This reuses the owning enrichment scheduler instead of adding
conditional per-session settings throughout its job and patch lifecycle.
The initial implementation retained a shared runtime path for default sessions.
[Decision 0101](0101-mcp-session-command-options.md) supersedes that default:
all model-backed MCP sessions now use private runtimes so omitted OpenAI command
tiers can select standard processing independently from SSH.

Unregistering an evaluation session stops its scheduler and invalidates pending
work. In-flight provider requests can finish but cannot apply further state
after closure. Application shutdown unregisters remaining evaluation sessions.
Workers and scheduler state are allocated per model-backed MCP session, so
operators should close completed runs promptly rather than accumulate idle variants.

### Profile Application And Connection Provenance

Load a requested profile before session registration, so enrichment and session
start telemetry see the chosen initial world. The default is never temporarily
switched. Existing process-default profile tools retain their semantics.

Profile application owns simulated state, not live connection provenance.
Preserve login identity, connection metadata, trace id, and the current
`dreamland.session.*` namespace. Strip that namespace from the replacement
before copying current values; otherwise a saved label or origin could leak
into a different run. MCP creation likewise replaces saved session provenance
with the new run's tags, label, and metadata.

World/system restores use the current login and expand username templates.
Exact restores keep their effective shell user, position, and history but
retain the live connection login. This deliberately replaces the old exact
restore behavior that copied another session's login identity. Owning state
profile and console tests record the new distinction.

### Detached Spiritguide And Evidence

Detached create/update/fork accept an optional Spiritguide-only override object
on MCP and REST. They continue using the blank scaffold and profile persistence
from [decision 0018](0018-detached-spiritguide-profile-workflows.md). A dry run
still performs inference without saving, and a plain copy fork rejects unused
overrides.

Return and record a non-secret resolved configuration with a stable SHA-256
digest. Session metadata, live traces, and telemetry correlate command and
Spiritguide runs with their request settings; detached results include the
synthetic session id. The digest covers settings, not world content, prompts,
code, or provider-delivered tier. Experiment manifests must capture those
additional inputs and outcomes separately.

Exclude evaluation configuration from model prompts so recording the variant
does not change the simulated environment presented to the model. Keep actual
provider controls and serving/usage telemetry in the shared provider adapters
and callbacks introduced or extended by earlier decisions.

### Validation

Provider-free owning tests exercise malformed inputs, immutable inheritance,
provider credential routing, real agent constructor forwarding, two simultaneous
variants, independent event streams, profile selection, connection identity,
cleanup, startup failure, prompt exclusion, and detached profile operations.
Actual loopback MCP and REST tests validate schema exposure, argument forwarding,
command execution, profile application, and exact cleanup with deterministic
provider substitutes. Live model access remains a separate restart checkpoint.

Implementation checkpoint: 471 focused evaluation, state, admin service,
console, main, backend, Spiritguide, telemetry, and enrichment tests passed;
all 56 loopback admin API/MCP tests passed with local socket access. Focused
Ruff checks passed, and Mypy with Python 3.12 and silent imported-module
checking reported no issues in the six evaluated configuration/runtime,
registry, admin service/API, and backend protocol files. One intermediate
enrichment test observed its final log after its idle check; the isolated
retry and complete final regression run passed. No provider inference was
needed for this implementation checkpoint.

## Maintenance

Update this decision and [MCP Model Evaluation Controls](../MCP_MODEL_EVALUATION.md)
when configuration scope, session ownership, restoration, or run evidence
changes. Keep the generic client MCP and web UI outside this admin feature.
