# MCP Model Evaluation Controls

Status: Active
Audience: DREAMLAND operators, researchers, and external testing agents
Purpose: describe profile selection and isolated model configuration for MCP runs.

## Overview

The admin MCP surface can start a session from a chosen saved profile and give
that session its own command agent, Spiritguide, and four enrichment role
settings. Separate sessions can compare providers and models without changing
the running process defaults. New MCP sessions use standard OpenAI command
processing unless a tier is explicitly supplied. Detached Spiritguide profile
creation, update, and fork also support per-operation Spiritguide settings.

The generic `/client-mcp` surface retains its ordinary command-client contract.
Evaluation controls belong to the authenticated admin `/mcp` surface with write
tools enabled. `dreamland://capabilities` reports `evaluation_overrides` when
the service has a model runtime factory; the normal application wires one in.

## Details

### Session Creation

`dreamland_session_create` accepts `username`, `tags`, `label`, and `metadata`,
plus these configuration arguments:

| Argument | Behavior |
| --- | --- |
| `state_profile` | Optional saved profile name. Loaded before session creation and registration. Omission uses the current process default. |
| `model` | Optional `provider:model` reference for the command emulator. Omission or direct `null` uses the nested agent override or the configured command model. |
| `openai_service_tier` | Optional command-emulator tier: `auto`, `default`, `flex`, `fast`, `priority`, or `ultrafast`. If neither direct nor nested arguments supply a tier, OpenAI commands use `default`. Direct `null` behaves like omission. |
| `config_overrides` | Optional object containing `agent`, `spiritguide`, and/or `enrichment` settings. With a runtime factory, every new MCP session gets an isolated runtime, including omitted overrides and `{}`. |

Direct `model` and `openai_service_tier` arguments are aliases for the same
fields under `config_overrides.agent`. Identical duplicates are accepted;
different values fail with a `bad_request` error before model construction or
session registration. These arguments select the foreground command emulator;
Spiritguide and enrichment remain independently configured through the nested
role objects.

For a standard-tier command session:

```json
{
  "username": "alice",
  "model": "openai:gpt-5.6-sol",
  "openai_service_tier": "default"
}
```

Standard processing is an explicit request setting, not project inheritance.
An explicit `auto` or nested `"openai_service_tier": null` uses the existing
provider/project semantics and can select Fast if the OpenAI project does.
Explicit `fast`, `priority`, and `ultrafast` are available for deliberate
comparisons. OpenAI currently supports Ultrafast for GPT-6 Astra and GPT-6.1 Sol;
see [Ultrafast mode](https://developers.openai.com/api/docs/guides/ultrafast-mode).
Acceptance of a tier in configuration does not establish provider availability
or delivered service. Verify the response tier and calculate costs from usage
and the corresponding [published rates](https://developers.openai.com/api/docs/pricing).
An omitted tier no longer inherits a Fast tier from the process, including
when a caller supplies only a command model. Non-OpenAI provider controls retain
their existing semantics.

The generic `client_session_create` tool still accepts only `username`. Its new
sessions receive the same standard OpenAI command default through the shared
session-creation service. Existing sessions retain their chosen settings;
running an admin MCP command against an existing SSH session uses that session's
runtime. Detached Spiritguide operations keep their separate override behavior.

The normal application supplies the model runtime factory. Embedded services
without `evaluation_overrides` retain their supplied command backend for calls
without controls and reject explicit model, tier, or configuration overrides
as unavailable; the standard-tier policy cannot configure those custom backends.

Example arguments, using a previously saved `evaluation-lab` profile:

```json
{
  "username": "alice",
  "state_profile": "evaluation-lab",
  "label": "command-comparison-a",
  "tags": ["agent:codex", "run:comparison-1", "variant:a"],
  "config_overrides": {
    "agent": {
      "model": "openai:gpt-5.6-terra",
      "reasoning_effort": "low",
      "max_output_tokens": 4096
    },
    "spiritguide": {"model": "xai:grok-4.3"},
    "enrichment": {
      "enabled": false,
      "expert": {"model": "anthropic:claude-sonnet-4-6"}
    }
  }
}
```

Model names are operator inputs, not an allowlist or a statement that a model
is available to an account. Provider routing uses the existing OpenAI,
Anthropic, and xAI adapters and the server's existing credential resolver.
Each role can choose its provider independently. Provider-specific controls
retain the adapter's validation and unsupported-control behavior.

Each role accepts `model`, `reasoning_effort`, `reasoning_summary`, `verbosity`,
`speed`, `prompt_cache`, `max_output_tokens`, `openai_api`,
`openai_service_tier`, and `openai_reasoning_mode`. The same names and values
are documented in [Operations](OPERATIONS.md). Omission inherits the role's
startup setting except for the MCP command agent's OpenAI tier default described
above. Nested JSON `null` clears an optional control; it is invalid for
`model`. For example, selecting explicit OpenAI Chat Completions requires
clearing any inherited reasoning summary and reasoning mode. A model change
does not silently reset the other fields.

`enrichment` contains the role objects `coordinator`, `ambient_coordinator`,
`expert`, and `integrator`. It also accepts:

| Field | Accepted value |
| --- | --- |
| `enabled` | Boolean; `false` disables automatic scheduling for this session. |
| `apply_mode` | `safe` or `shadow`. |
| `triggers` | List of supported trigger names from [Enrichment Agent](ENRICHMENT_AGENT.md); `[]` disables those triggers. |
| `idle_delay_ms`, `cooldown_ms` | Nonnegative integers. |
| `max_jobs_per_minute`, `max_inflight_per_session` | Positive integers. |

Credentials, environment variables, prompt paths, transport settings, and
logging configuration cannot be supplied through overrides. Unknown fields
and invalid types fail before constructing models or registering a session.
Prompt modules and tracing settings inherit from the process. Overrides are
fixed at creation; start another session to change a test variant.

An isolated runtime owns its command graph, Spiritguide, enrichment graph,
event bus, worker pool, timers, and scheduler state. The main registry still
provides session inspection and the shared lock used by foreground commands
and background patches. Events go to the owning runtime, so default enrichment
does not also process the MCP session. Exact session cleanup stops its
enrichment scheduler and prevents pending work from applying further patches;
already running provider requests may finish in the background. Application
shutdown closes remaining evaluation runtimes.
Each model-backed MCP session now allocates its own scheduler and worker state;
close completed sessions promptly.

### Profiles And Connection Identity

Profile selection at creation does not change the default. Existing
`dreamland_state_profile_set_default` and `clear_default` tools remain available
for subsequent connections. Their changes are process-local and do not rewrite
`dreamland.ini`.

Applying a profile to a live session preserves its trace id, login username,
connection metadata, MCP origin, tags, label, custom metadata, and evaluation
configuration. `world` and `system` profiles use the current login and expand
`{username}` templates. `exact` profiles restore the saved effective shell user,
position, and command history while retaining live connection provenance.
Applying a profile is a state replacement; use fresh sessions for comparable
A/B starting conditions.

### Detached Spiritguide

The `dreamland_spiritguide_profile_create`, `update`, and `fork` tools accept
`config_overrides` with only a `spiritguide` object. These fields also work on
the corresponding admin REST endpoints. Example create arguments:

```json
{
  "name": "evaluation-lab",
  "level": "world",
  "instruction": "Create a small physical Linux laboratory with one workstation and one file server. Establish a coherent network and an alice account.",
  "dry_run": true,
  "config_overrides": {
    "spiritguide": {
      "model": "openai:gpt-5.6-sol",
      "reasoning_effort": "medium",
      "max_output_tokens": 8192
    }
  }
}
```

Create starts from a blank scaffold. A dry run performs inference and returns
the proposed update without saving. A later non-dry call performs a new model
call; it does not commit the exact prior preview. An instruction-free fork
copies a profile without inference and rejects model overrides as unused.

### Run Evidence

Session creation returns `evaluation_config` containing `schema_version`,
`sha256`, and `settings`. The digest identifies the resolved non-secret request
settings, including inherited values. It does not identify profile content,
prompts, code revision, or actual provider-delivered behavior. Optional null
values retain adapter/provider default semantics.

The snapshot is also stored under `dreamland.session.evaluation_config` in
session metadata, the live trace, and session/command telemetry. Detached
Spiritguide result metadata includes `evaluation_config` and the detached
`session_id`; its trace records the same settings. Evaluation configuration is
excluded from command and Spiritguide model prompts.

For reproducibility, save the snapshot along with the code revision, profile
artifact, prompts, run label/tags, complete session id, command ids, outputs,
and observed token/latency/tier metrics. Preserve caller artifacts separately
from process-memory traces, which have bounded retention.

### Validation And Operator Checkpoint

Owning tests cover validation, provider routing, two simultaneous variants,
event isolation, profile selection, identity preservation, cleanup, telemetry,
prompt exclusion, and detached create/update/fork. Admin API tests exercise
the actual local MCP transport and REST forwarding with deterministic agents.

After installing this change, restart DREAMLAND and refresh the MCP connection
so its tool schemas include the new arguments. The live checkpoint is:

1. Read capabilities and confirm `evaluation_overrides` is true.
2. Create two tagged sessions with the same saved profile and username,
   different command settings, and an explicit enrichment policy. Check each
   returned profile, username, and configuration snapshot.
3. Run `pwd`, `whoami`, and a model-backed command in each session. Inspect
   traces and direct command ids. Exercise detached Spiritguide with a dry run.
4. Close only those exact session ids with `origin="mcp"`, `dry_run=false`,
   and verify they no longer appear in live sessions.

To check the direct arguments and standard default after restarting and
refreshing MCP tool schemas:

1. Create a tagged admin session with a direct `model` and no tier. Confirm
   `evaluation_config.settings.agent.openai_service_tier` is `default`.
2. Create another with a direct `openai_service_tier: "default"` and nested
   `agent.reasoning_effort`. Confirm both controls in its snapshot.
3. Supply direct `default` and nested `fast` for the tier. Confirm a conflict
   error and no new live session.
4. Run a model-backed command in a valid session and inspect the returned tier
   in token logs. Close the exact session ids. The automated transport tests
   use deterministic providers; this live check verifies actual provider serving.

These checks verify live bindings and provider access. Comparative studies
and their evidence are published separately.

## Maintenance

Update this document, the MCP agent guide, and the admin API reference when
override fields, runtime ownership, profile identity semantics, or evidence
change. Keep provider translation in the shared provider adapters.
