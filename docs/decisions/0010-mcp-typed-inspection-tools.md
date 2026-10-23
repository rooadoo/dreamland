# MCP Typed Inspection Tools

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: record why MCP inspection moved beyond fixed read resources.

## Overview

MCP-created command sessions need traceable, agent-friendly inspection paths.
Fixed MCP resources are useful for quick reads, but agent workflows often need
explicit parameters for limits, cursors, command filters, command targets, sort
order, state-delta sections, tag grouping, and cleanup of test sessions.

## Decision

Keep fixed MCP resources as the read-only default surface. Expose richer
parameterized inspection and lifecycle helpers as MCP tools behind the existing
`mcp_write_tools` flag. This preserves the conservative default while giving
trusted test agents a typed API instead of raw Dreamland console commands.

The enabled tool surface includes parameterized log reads, session lists,
live-session lists, tag groups, unified session inspection, live trace reads,
historical command summaries, command inspection, state deltas, filtered
MCP-session cleanup, and the existing state-profile, spiritguide, shutdown, and
headless command-session tools.

Command execution results remain intentionally small and user-visible. They now
include history-position correlation fields and an immediate inspection target
so a calling agent can connect the returned terminal output to subsequent
Langfuse/admin inspection without embedding heavy trace data in the command
result itself.

Expose a `dreamland_session_testing_workflow` MCP prompt to guide agents toward
the intended flow: create tagged sessions, run ordinary user-visible commands,
inspect with typed MCP resources/tools and Langfuse, avoid `!dreamland`, and
close MCP-created sessions.

## Maintenance

Update this decision when MCP inspection tooling changes shape, when read-only
parameterized tools are moved outside `mcp_write_tools`, or when historical
tag/group queries move from Langfuse-only inspection into DREAMLAND's own admin
service.
