# Admin Interface

Status: Draft
Audience: maintainers, coding agents, DREAMLAND operators, and UI developers
Purpose: define the planned local admin interface for DREAMLAND API and MCP
bindings.

## Overview

DREAMLAND needs an optional local admin interface so UIs, coding agents, and
operator tools can inspect and eventually control a running instance without
scraping console output. The interface is auxiliary: running DREAMLAND must not
require an API server, a UI, or MCP support.

The canonical contract is a protocol-neutral admin capability model. HTTP REST
and MCP are bindings over that model. Neither binding should own DREAMLAND
runtime behavior, state mutation, authorization rules, or response semantics.

The first implementation checkpoints are `src/dreamland/admin_interface.py`, a
dependency-light service layer that exposes typed status, capability, logs,
trace, enrichment-event, live-session, historical-introspection, and
state-profile read models,
and `src/dreamland/admin_api.py`, an optional FastAPI/uvicorn local HTTP
server. The current HTTP server implements authenticated health, status,
capabilities, logs, combined session summaries, live sessions, session traces,
live session state, historical command, command inspection, state-delta, and
state-profile summary/detail endpoints. It also mounts an authenticated MCP
Streamable HTTP endpoint at `/mcp` through the official MCP Python SDK, plus a
separate generic client MCP endpoint at `/client-mcp`.

Initial REST scope is console-equivalent observability and control. The MCP
binding also exposes a headless user-command path for coding agents: agents can
create tagged live sessions, run ordinary user-visible commands through the
same backend used by SSH, and close the sessions they created. Future versions
may expose web-native user sessions that supplement SSH, but that is outside
the first admin interface.

## Details

### Design Goals

- Keep DREAMLAND's SSH runtime and command path fast. Admin APIs must be
  optional and must not sit in the command execution hot path.
- Share behavior with the console through typed services where practical.
  Expose operator actions as explicit resources or tools instead of raw console
  command strings.
- Return structured data and let clients decide how to display it.
- Bind only to the local system by default. The initial transport does not need
  TLS because the assumed UI and agents run on the same host.
- Use bearer token authentication for local admin access.
- Treat API compatibility as pre-production. Breaking changes are acceptable
  when documented and reflected in versioned bindings.
- Make MCP safer than general REST control by exposing read-only resources first
  and requiring an explicit config gate for mutating MCP tools.

### Shared Admin Model

The shared service layer should expose typed operations for:

- runtime status, health, capabilities, and redacted effective configuration;
- bounded runtime logs;
- per-session trace records;
- bounded per-session enrichment lifecycle events with optional exact job
  filtering and explicit eviction metadata;
- live session summaries and live session state snapshots;
- Langfuse-backed historical sessions, commands, command inspection, usage,
  cost, timing, reasoning summaries, and state deltas;
- state profile list, show, save, apply, default, and delete operations;
- structured failure capsule list, show, and parser replay operations;
- spiritguide dry-run and apply operations;
- MCP-created headless sessions, tagged for Langfuse grouping and live-session
  filtering;
- user-command execution against live sessions through the normal backend while
  bypassing Paramiko;
- Langfuse flush;
- process shutdown.

Service outputs should be JSON-safe dataclasses or plain structured values.
Protocol adapters may translate those values into HTTP responses, MCP resource
contents, or MCP tool results, but adapters should not duplicate business
logic.

### HTTP REST Binding

The planned HTTP binding is versioned under `/api/v0`. It is disabled by
default and starts only at process startup when enabled in configuration.
Concrete REST request, response, query, and SSE shapes are documented in
`docs/ADMIN_API_V0.md`.
The current implementation exposes health, status, capabilities, logs, combined
session summaries, live sessions, session traces, live session state, and
retained enrichment events, and state-profile summaries/details. It also
exposes redacted effective config,
Langfuse-backed command list, command inspection, state-delta, state-profile
control, spiritguide, shutdown, flush, and server-sent event endpoints for
runtime logs and live session traces.

Initial read-only endpoints:

- `GET /api/v0/status`
- `GET /api/v0/capabilities`
- `GET /api/v0/health`
- `GET /api/v0/config/effective`
- `GET /api/v0/logs`
- `GET /api/v0/sessions` returns a combined collection with `live` and
  `langfuse` attributes on each session.
- `GET /api/v0/live-sessions`
- `GET /api/v0/live-sessions/{session_id}`
- `GET /api/v0/sessions/{session_id}` returns unified live and historical
  session detail. It accepts `historical_limit`, defaulting to `100`, because
  the current historical backend exposes list lookup rather than direct session
  lookup.
- `GET /api/v0/events` streams runtime log events as server-sent events. It
  accepts `after`, `limit`, `timeout`, and `once`.
- `GET /api/v0/sessions/{session_id}/state`
- `GET /api/v0/sessions/{session_id}/trace`
- `GET /api/v0/sessions/{session_id}/enrichment-events` reads process-local
  lifecycle events and accepts `job_id`, `limit`, and `after`. The session need
  not remain live.
- `GET /api/v0/sessions/{session_id}/events` streams live session trace events
  as server-sent events. It accepts `after`, `limit`, `timeout`, and `once`.
- `GET /api/v0/sessions/{session_id}/commands`
  accepts `limit`, `filter`, `sort`, `descending`, and `include_io_summary`.
- `GET /api/v0/sessions/{session_id}/commands/{command_id}`
  accepts `limit` and `detail`.
- `GET /api/v0/sessions/{session_id}/state-delta`
  accepts `from`, `to`, `limit`, and repeated `section` parameters.
- `GET /api/v0/state-profiles`
- `GET /api/v0/state-profiles/{name}`

Initial control endpoints:

- `POST /api/v0/introspection/flush`
- `POST /api/v0/state-profiles` saves a profile from a live session. Body:
  `name`, `level`, `session_id`, optional `comment`.
- `POST /api/v0/sessions/{session_id}/state-profile` applies a profile to a
  live session. Body: `name`.
- `GET /api/v0/state-profiles/default`
- `PUT /api/v0/state-profiles/default` sets the process default profile. Body:
  `name`. It does not persist the default to `dreamland.ini`.
- `DELETE /api/v0/state-profiles/default`
- `DELETE /api/v0/state-profiles/{name}`
- `POST /api/v0/sessions/{session_id}/spiritguide` runs spiritguide against a
  live session. Body: `instruction`, optional `dry_run`.
- `POST /api/v0/shutdown`

Server-sent event responses use standard `text/event-stream` framing. The JSON
event payload includes at least:

```json
{
  "sequence": 123,
  "type": "log",
  "source": "dreamland.runtime",
  "data": {
    "text": "..."
  }
}
```

### Admin MCP Binding

The admin MCP binding should expose the same shared admin model through a local MCP
endpoint, initially planned as `/mcp` on the same optional API server. This is
for coding agents, model-backed operators, and other MCP clients that need
direct access to a running DREAMLAND instance.

The current MCP endpoint is implemented with the official MCP Python SDK's
`FastMCP` server and is mounted into the FastAPI app. MCP clients must follow
normal Streamable HTTP lifecycle behavior: send `initialize`, retain the
returned `Mcp-Session-Id`, send `notifications/initialized`, and include the
session id on later resource and tool requests. Resource bodies are currently
JSON strings returned as MCP text resources.

MCP read-only data is exposed as resources:

- `dreamland://status`
- `dreamland://health`
- `dreamland://capabilities`
- `dreamland://agent-guide`
- `dreamland://config/effective`
- `dreamland://logs`
- `dreamland://events`
- `dreamland://sessions`
- `dreamland://live-sessions`
- `dreamland://live-sessions/{session_id}`
- `dreamland://sessions/{session_id}` returns the same unified live and
  historical session detail as `GET /api/v0/sessions/{session_id}`, using a
  historical lookup limit of `100`.
- `dreamland://sessions/{session_id}/events` returns discovery metadata for the
  REST session event stream.
- `dreamland://sessions/{session_id}/state`
- `dreamland://sessions/{session_id}/trace`
- `dreamland://sessions/{session_id}/enrichment-events`
- `dreamland://sessions/{session_id}/commands`
- `dreamland://sessions/{session_id}/commands/{command_id}`
- `dreamland://sessions/{session_id}/state-delta`
- `dreamland://state-profiles`
- `dreamland://state-profiles/default`
- `dreamland://state-profiles/{name}`
- `dreamland://structured-failures`
- `dreamland://structured-failures/{failure_id}`
- `dreamland://structured-failures/{failure_id}/replay-parser`

`dreamland://agent-guide` is a plain-text guide for external coding agents,
testing agents, and operators who use DREAMLAND through MCP. It is not part of
the simulated command-agent prompt or emulated user environment.

MCP tools are disabled by default behind an explicit configuration flag. The
current endpoint exposes these parameterized read and write tools only when
`mcp_write_tools = true`:

- `dreamland_introspection_flush`
- `dreamland_logs_read`
- `dreamland_sessions_list`
- `dreamland_live_sessions_list`
- `dreamland_session_tag_groups`
- `dreamland_session_inspect`
- `dreamland_session_trace_read`
- `dreamland_session_enrichment_events_read`
- `dreamland_session_commands_list`
- `dreamland_session_command_inspect`
- `dreamland_session_state_delta`
- `dreamland_session_create`
- `dreamland_session_run_command`
- `dreamland_sessions_close`
- `dreamland_state_profile_save`
- `dreamland_state_profile_apply`
- `dreamland_state_profile_set_default`
- `dreamland_state_profile_clear_default`
- `dreamland_state_profile_delete`
- `dreamland_spiritguide_profile_create`
- `dreamland_spiritguide_profile_update`
- `dreamland_spiritguide_profile_fork`
- `dreamland_structured_failures_list`
- `dreamland_structured_failure_show`
- `dreamland_structured_failure_replay_parser`
- `dreamland_spiritguide`
- `dreamland_shutdown`

Admin MCP session creation supports optional `state_profile` selection, direct
`model` and `openai_service_tier` command-emulator arguments, and
`config_overrides` for independent command, Spiritguide, and enrichment roles.
The direct arguments alias the matching nested agent fields and reject
conflicts. An omitted OpenAI command tier resolves to `default`; other model
settings inherit unless explicitly overridden. With a model runtime factory,
every new MCP session uses an isolated runtime, including ordinary sessions
from `/client-mcp`, whose create tool still accepts only `username`.
The service reports `evaluation_overrides` in capabilities and returns a
resolved `evaluation_config` snapshot. Detached Spiritguide create/update/fork
tools and REST endpoints accept Spiritguide-only overrides. See
[MCP Model Evaluation Controls](MCP_MODEL_EVALUATION.md) for fields and evidence.

`dreamland_session_create` creates a real live `SessionState` through the
normal command backend, registers it in the live-session registry, records the
same Langfuse session-start event used by SSH when Langfuse is enabled, and
stores session tags under session environment metadata. The tool always tags
the session with `mcp` and accepts additional caller tags, a label, and compact
caller metadata for grouping test runs. Live session summaries include `tags`,
`label`, and `origin` when available.

`dreamland_session_run_command` runs a command in a live session through the
same `AgentBackend` execution path used by SSH. The response is intentionally
small: session id, direct command id, command, stdout, stderr, exit status,
interruption fields, history position, an immediate inspection target, and a
prompt-like post-command context. The context includes user, host, cwd, active
program, prompt, and a short label so callers can understand normal terminal
location after SSH, shells, or REPL transitions. The direct command id is
intended for agents and should be preferred over relative targets. Relative
targets such as `@-1` remain useful for humans at the console. State inspection
remains available through existing Langfuse/admin resources and the
parameterized MCP inspection tools rather than through a second command-result
side channel. The command tool rejects `!dreamland` as the first shell token;
agents must use explicit admin resources/tools for operator actions instead of
raw Dreamland console command strings.

`dreamland_logs_read`, `dreamland_sessions_list`,
`dreamland_live_sessions_list`, `dreamland_session_inspect`,
`dreamland_session_trace_read`, `dreamland_session_commands_list`,
`dreamland_session_command_inspect`, and `dreamland_session_state_delta` expose
the same typed inspection data as REST with explicit parameters for limits,
cursors, filters, sort order, command targets, and state-delta sections.
Command inspection defaults to `detail="compact"` for MCP callers so routine
agent workflows get command and observation summaries without raw model IO or
large metadata payloads. Callers can request `detail="full"` when they need raw
Langfuse payloads for prompt or state-transition diagnosis.
Recent Langfuse-backed command summaries may lag briefly even after an explicit
flush. Live session trace is the immediate source for current user-visible
input/output; agents should retry historical command list or inspection after a
short delay when a just-run command is not visible yet.

`dreamland_session_tag_groups` groups live sessions by tag. `dreamland_sessions_list`
and `dreamland_live_sessions_list` can filter by tag, label, and origin. These
filters apply to live session metadata. While a session is live, agents should
prefer DREAMLAND MCP inspection and lifecycle tools because they expose the same
session registry and command correlation used by the console. Langfuse MCP is
best reserved for historical analysis after DREAMLAND no longer has live session
context or for cross-checking telemetry.

`dreamland_sessions_close` is the generic lifecycle close operation. It closes
live sessions by exact session ids or by tag, label, and origin filters, supports
`dry_run`, and rejects unfiltered close requests. Agents should use exact full
session ids with `origin="mcp"` for targeted cleanup. Agents should use tag,
label, or origin-only cleanup only when an exact session id is unavailable or an
operator explicitly requests grouped cleanup. They should call
`dreamland_sessions_close` with `dry_run=true`, report the matched sessions, and
get human/operator approval before repeating the same filtered close with
`dry_run=false`. DREAMLAND logs warning records when a non-dry-run close uses
non-id filters or matches non-MCP sessions.

MCP also exposes `dreamland_session_testing_workflow`, a prompt that guides
agents to create tagged sessions, run user-visible commands, inspect through
typed MCP resources/tools, avoid `!dreamland`, use direct ids, and close
sessions. `dreamland_session_lifecycle_workflow` guides tagged run cleanup and
dry-run filtered close operations. `dreamland_command_diagnostics_workflow`
guides agents from user-visible command results through live trace, command
inspection, state delta, and optional Langfuse parity checks.

Additional MCP prompts may be added after resources exist to guide common agent
workflows:

- `dreamland_session_review`
- `dreamland_cost_review`
- `dreamland_trace_investigation`
- `dreamland_state_delta_review`
- `dreamland_spiritguide_plan`

Prompts should help an agent choose and interpret resources and tools. They
must not imply hidden authority beyond the exposed resources and enabled tools.

### Generic Client MCP Binding

The generic client MCP binding is mounted at `/client-mcp` on the same
authenticated local server. It exists for independent evaluators that need to
test the command-client experience without receiving operator/admin inspection
tools or DREAMLAND-specific guidance.

The endpoint uses a separate `FastMCP` server named `Command Client`. It has a
separate MCP session lifecycle from `/mcp`, so clients must initialize it
independently and retain the `Mcp-Session-Id` returned by `/client-mcp`.

The only resource is:

- `client://guide`

The guide is intentionally minimal. It describes only how to create a command
session, run commands, and end the session. It must not describe DREAMLAND,
simulation internals, telemetry, Langfuse, operator resources, or diagnostic
inspection paths.

When `mcp_write_tools = true`, `/client-mcp` exposes only:

- `client_session_create`
- `client_run_command`
- `client_session_exit`

`client_session_create` creates a live command session through the same service
operation used by the admin MCP headless session tool. Internally, sessions are
tagged so the generic close tool can distinguish them from admin-created MCP
sessions, but the generic response does not expose tags, metadata, command ids,
history positions, inspection targets, telemetry, or state diagnostics.

`client_run_command` returns only the fields needed to judge the command
experience: `session_id`, `command`, `stdout`, `stderr`, `exit_status`,
`interrupted`, `signal`, and prompt-like post-command context fields. The
context fields are user-visible terminal location cues, not admin inspection
state; they include username, hostname, cwd, active program, prompt, and a
short label.

`client_session_exit` accepts only one exact `session_id`. It performs no
tag, label, origin, or dry-run filtered cleanup. It closes only sessions created
through the generic client path and returns whether that exact session was
closed.

### Configuration

The configuration shape is parsed by `src/dreamland/config.py`:

```ini
[api]
enabled = false
host = 127.0.0.1
port = 8765
token = replace-me
mcp_write_tools = false
```

`enabled` controls whether the local admin server starts. When the API is
enabled, the admin MCP endpoint at `/mcp` and the generic client MCP endpoint
at `/client-mcp` are enabled by default. `mcp_write_tools` controls whether MCP
clients can invoke mutating tools on either MCP endpoint. REST control
endpoints remain admin-token-protected and are governed by the API binding's
route set.

If the API is enabled with a missing, blank, or placeholder token, DREAMLAND
generates a temporary bearer token and prints it on boot. Operators should set
`token` or `DREAMLAND_API_TOKEN` when they need a stable token across restarts.

The HTTP and MCP server dependencies are optional. Install DREAMLAND with the
`admin` extra before enabling `[api]`:

```sh
pip install -e ".[admin]"
```

### Authentication And Local Safety

All REST and MCP HTTP requests should require:

```http
Authorization: Bearer <configured-token>
```

The server should bind to `127.0.0.1` by default. If a future configuration
allows non-local binding, documentation must call out the additional security
requirements before that is recommended for operators.
The current implementation rejects non-loopback API hosts.

For MCP Streamable HTTP support, the server should validate `Origin` for local
browser-accessible requests to reduce DNS rebinding risk.

Secrets must never be returned by `/config/effective` or equivalent MCP
resources. Secret fields should report redacted values or presence booleans.

### Error Shape

REST errors should use a stable JSON envelope:

```json
{
  "error": {
    "code": "not_found",
    "message": "session not found",
    "details": {}
  }
}
```

MCP errors should use normal MCP tool/resource error mechanisms while preserving
the same stable internal error codes when practical.

### Versioning

REST starts under `/api/v0`. MCP resource URIs and tool names should remain
stable within a documented interface version, but the project is pre-production
and can make breaking changes when the docs are updated in the same change.

Breaking changes should update this document, operations documentation, and any
decision records that explain why the shape changed.

## Maintenance

Update this document when admin service capabilities, REST endpoints, MCP
resources, MCP tools, authentication, configuration, or event shapes change.
When runtime behavior is implemented, also update `docs/OPERATIONS.md`,
`docs/ARCHITECTURE.md`, `docs/TESTING.md`, and `dreamland.example.ini` as
appropriate.
