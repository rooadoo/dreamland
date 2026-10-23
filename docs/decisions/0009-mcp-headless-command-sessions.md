# 0009 MCP Headless Command Sessions

Status: Active
Audience: maintainers, DREAMLAND operators, and coding agents
Purpose: record why MCP can create and drive user-command sessions without SSH.

## Overview

DREAMLAND's product boundary is an LLM-emulated computing network. SSH is the
current user entry point, but it is not the runtime boundary. Coding agents need
a faster way to create sessions, run command sequences, inspect Langfuse traces,
and iterate on simulation behavior without paying the overhead of Paramiko
transport setup for every test.

Related implementation commit:

- This decision is implemented by the feature commit that adds
  `dreamland_session_create`, `dreamland_session_run_command`,
  and generic lifecycle cleanup through `dreamland_sessions_close`.

## Decision

Expose headless command sessions through the existing authenticated MCP binding
when `mcp_write_tools = true`. MCP-created sessions are real live
`SessionState` objects created by the same command backend used by SSH,
registered in the live-session registry, and visible through existing admin and
Langfuse inspection surfaces.

Keep command execution in normal mode. The MCP command runner uses the normal
backend path so built-ins, shell stack, command history, state updates,
checkpointing, world state, and model-backed command behavior match SSH command
execution as closely as possible while bypassing Paramiko.

Preserve traceability. MCP-created sessions receive an early Langfuse
`dreamland-session-start` event when Langfuse is enabled. Session tags, labels,
origin, and compact caller metadata are stored in session environment metadata
and copied into Langfuse trace metadata and tags for grouping test runs.
Session tags are represented as a string in propagated Langfuse metadata
because propagated attributes accept only scalar values, while the Langfuse
trace `tags` field remains the grouping source of truth.

Separate user commands from operator commands. `dreamland_session_run_command`
rejects shell commands beginning with `!dreamland` because ordinary users should
not rely on that SSH console escape. MCP clients should use explicit admin
resources/tools when they need operator capabilities; raw Dreamland console
command strings are not exposed as an MCP tool.

Require explicit lifecycle control. MCP-created sessions stay live until an
MCP client calls `dreamland_sessions_close` or the process exits. Agents should
prefer exact full `session_ids` with `origin="mcp"`. Filtered cleanup should use
`dry_run=true`, report matches, and proceed with `dry_run=false` only after
human/operator approval.

Route MCP SDK logs through DREAMLAND runtime logging. The MCP SDK configures
standard Python logging for request/session lifecycle messages; DREAMLAND
forwards `mcp.server.*` records into the `dreamland.mcp` event category and
prevents SDK handlers from writing directly to the interactive console.

## Deferred Decisions

Streaming command output, Ctrl-C, Ctrl-D, idle timeouts, and cheaper/budgeted
execution policies are deferred. They should be added only when a concrete
testing workflow needs them and after deciding how each feature affects
traceability and parity with ordinary user sessions.

No REST route is added for headless command execution in this decision. REST can
gain equivalent controls later if a UI or non-MCP client needs them.

## Maintenance

Update this record if MCP command execution stops using the normal backend,
if MCP session lifecycle rules change, if `!dreamland` becomes available to
normal users, or if cheaper execution modes become part of the committed
testing workflow.
