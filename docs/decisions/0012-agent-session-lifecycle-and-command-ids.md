# Agent Session Lifecycle And Command IDs

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: record lifecycle and command-correlation rules for agent-driven tests.

## Overview

MCP agents need to create, exercise, inspect, and clean up DREAMLAND sessions
without relying on SSH or hidden console commands. They also need stable direct
identifiers so command results can be correlated with later DREAMLAND and
Langfuse inspection.

## Decision

Generate a DREAMLAND command id for each backend command execution. Store it in
live session history, include it in Langfuse command metadata as
`dreamland.command_id`, and return it from `dreamland_session_run_command`.
Historical command summaries prefer this direct id while retaining the Langfuse
observation id for telemetry correlation.

Agents should use full direct session ids and command ids in automated
workflows. Relative command targets such as `@-1` remain supported for human
console ergonomics, but they are not the preferred agent interface.

Live session lifecycle management belongs in shared DREAMLAND surfaces, not only
in MCP. The console exposes live-session listing and filtered close operations
through `sessions --live`, `sessions --close`, and `sessions --close-matching`.
The admin service exposes the same generic close operation, and MCP binds it as
`dreamland_sessions_close`. Broad close operations require explicit ids or
metadata filters and support dry-run previews.

While a session is live, agents should prefer DREAMLAND MCP for session and
command inspection because it reflects the current process registry and command
correlation. Langfuse MCP remains useful for historical analysis after a session
is no longer live and for validating telemetry parity.

## Maintenance

Update this decision if command ids move to a different identifier scheme, if
session lifecycle cleanup gains age-based retention, or if Dreamland MCP and
Langfuse MCP responsibilities change.
