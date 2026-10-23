# MCP Agent Workflows

Status: Active
Audience: external coding agents, testing agents, DREAMLAND operators
Purpose: define how agents should use DREAMLAND MCP to create, exercise,
inspect, and clean up DREAMLAND sessions.

## Overview

DREAMLAND MCP is for agents and operators outside the simulation. It gives them
direct access to DREAMLAND admin resources, live session lifecycle tools, and
headless command sessions without SSH. These instructions are not part of the
simulated command-agent system prompt and must not be copied into Dreamland's
emulated user environment.

An MCP command session should behave like a normal user shell session. The
calling agent sends commands in sequence, reads the command result, and keeps
track of its position in the command sequence. DREAMLAND owns shell stack,
current working directory, environment, state transitions, command history, and
trace correlation.

The extra MCP metadata exists for correlation, inspection, and cleanup. It is
not user-visible terminal output.

## Details

### Tool Categories

- Discovery resources: `dreamland://status`, `dreamland://capabilities`,
  `dreamland://agent-guide`.
- Session lifecycle tools: `dreamland_session_create`,
  `dreamland_live_sessions_list`, `dreamland_sessions_list`,
  `dreamland_session_tag_groups`, `dreamland_sessions_close`.
- User command tool: `dreamland_session_run_command`.
- Live inspection tools: `dreamland_session_trace_read` and live session
  resources.
- Enrichment inspection:
  `dreamland_session_enrichment_events_read` and
  `dreamland://sessions/{session_id}/enrichment-events`. Events are retained in
  bounded process memory and can be filtered by exact job id after a session
  closes.
- Historical inspection tools: `dreamland_session_commands_list`,
  `dreamland_session_command_inspect`, `dreamland_session_state_delta`.
- Structured failure tools and resources:
  `dreamland://structured-failures`,
  `dreamland_structured_failures_list`,
  `dreamland_structured_failure_show`, and
  `dreamland_structured_failure_replay_parser`.
- Telemetry bridge: `dreamland_introspection_flush` before recent historical
  inspection that depends on Langfuse ingestion.
- Operator controls: state profile, spiritguide, and shutdown tools. Use these
  only for explicit operator workflows.

Agents must not run `!dreamland` inside MCP command sessions. Normal Dreamland
users do not have that shell command. Agents should use explicit MCP resources
and tools for admin, inspection, lifecycle, and operator actions.

### Normal Testing Workflow

1. Read `dreamland://agent-guide`, `dreamland://status`, and
   `dreamland://capabilities`.
2. Create a session with `dreamland_session_create`.
3. Tag the session with agent id, run id, and scenario id when possible. Use a
   label for stable run names.
4. Save the full `session_id`. Automated agents should use full ids rather than
   abbreviated ids.
5. Run ordinary user commands with `dreamland_session_run_command`.
6. Treat `stdout`, `stderr`, `exit_status`, `interrupted`, and `signal` as the
   result a real user would see.
7. Save each returned `command_id` for correlation and later inspection.
8. Read live input/output records with `dreamland_session_trace_read`.
9. When testing enrichment, read the exact job lifecycle with
   `dreamland_session_enrichment_events_read` rather than depending on
   correlation or retention in the shared runtime logs.
10. Use `dreamland_session_commands_list` and
   `dreamland_session_command_inspect` with direct `command_id` values for
   command diagnostics.
11. Use `dreamland_session_state_delta` when diagnosis depends on state changes
    between commands.
12. Close sessions when finished.

### Binding Smoke Check Before Model Comparisons

For profile selection at session creation, per-role model overrides, detached
Spiritguide comparisons, and configuration snapshots, use
[MCP Model Evaluation Controls](MCP_MODEL_EVALUATION.md). Explicit override
sessions own independent agents and enrichment scheduling. Applying a profile
preserves live login identity, connection provenance, and MCP run metadata.

When the operator is starting the server, distinguish tool availability from
live connectivity. Discovering `dreamland_session_create` and
`dreamland_session_run_command` bindings does not prove that the instance is
reachable or that a model request succeeds. Wait for the operator's startup
signal before this checkpoint.

Read the discovery resources, then create one distinctly labeled MCP session.
Run `pwd`, `whoami`, and `uname -a` sequentially, preserving each command id.
Read its live trace and inspect command results for ordinary output, exit
status, and state continuity. Include a command that reaches the model;
built-ins alone only establish transport and session routing. Close only the
exact session created for this check, including on failure. Report transport,
provider, parser, and cleanup outcomes separately. A successful smoke check
establishes command access, not comparative model quality.

### Command Result Interpretation

`dreamland_session_run_command` returns the compact command result an agent
needs for normal testing:

- `stdout` and `stderr`: user-visible terminal streams.
- `exit_status`: shell-style command status.
- `interrupted` and `signal`: interruption state.
- `history_index` and `history_position`: command history correlation.
- `inspection_target`: immediate relative target for human convenience.
- `command_id`: direct command id for automated inspection.

Agents should prefer `command_id` for follow-up inspection. Relative targets
such as `@-1` are useful for humans and console workflows, but automated agents
should not rely on them when a direct id is available.

### Diagnostics Workflow

When behavior is surprising:

1. Record the `session_id`, `command_id`, command, `stdout`, `stderr`, and
   `exit_status`.
2. Read live trace records with `dreamland_session_trace_read`.
3. If background enrichment is involved, read retained events using the exact
   session and job ids. Use the structured `event` and correlation fields; do
   not parse them back out of the formatted `text` line.
4. Inspect the command by direct `command_id` with
   `dreamland_session_command_inspect`.
5. Inspect state transitions with `dreamland_session_state_delta` when the
   behavior depends on cwd, shell stack, environment, or world/session state.
6. Check `dreamland://structured-failures` or
   `dreamland_structured_failures_list` when the result suggests malformed
   structured model output. Use `dreamland_structured_failure_show` for raw
   response and pre-command state details, and
   `dreamland_structured_failure_replay_parser` for parser-only replay.
7. Call `dreamland_introspection_flush` before relying on recent historical
   Langfuse-backed inspection.
8. If `dreamland_session_commands_list` or
   `dreamland_session_command_inspect` does not show a recent command
   immediately after flush, retry after a short delay. Live trace is the
   immediate source for current user-visible input/output; historical command
   inspection depends on telemetry ingestion.
9. Prefer DREAMLAND MCP while the session is live. Use Langfuse MCP mainly for
   historical analysis after live DREAMLAND context is gone or for explicit
   telemetry parity checks.

### Lifecycle Cleanup

Agents should clean up sessions they create.

Use exact full `session_id` values whenever possible. The preferred MCP cleanup
shape is `dreamland_sessions_close(session_ids=[session_id], origin="mcp",
dry_run=false)`.

Use tag, label, or origin-only filtered cleanup only when an exact session id is
not available or an operator explicitly asks for grouped cleanup. Before any
non-id cleanup, call `dreamland_sessions_close` with `dry_run=true`, report the
matched sessions, and get human/operator approval before repeating with
`dry_run=false` and the same filters.

DREAMLAND logs prominent warnings when a close operation uses non-id filters or
matches non-MCP sessions. These warnings help operators detect and redirect
agents before unsafe cleanup patterns become normal.

### Reporting Expectations

A useful agent report should include:

- session id, label, and tags;
- each command and direct command id;
- stdout, stderr, and exit status for user-visible behavior;
- which DREAMLAND MCP inspection tools were used;
- any Langfuse comparison, clearly labeled as telemetry or historical data;
- cleanup result.

Keep reports compact. Include only metadata needed to correlate behavior,
diagnose failures, or clean up sessions.

## Maintenance

Update this document when DREAMLAND MCP resources, tools, prompts, command
result shapes, lifecycle behavior, or recommended agent workflows change. Keep
this guidance separate from simulated command-agent prompts under
`src/dreamland/prompts/`.
