# Generic Client MCP Surface

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: record why command-client evaluation uses a separate generic MCP
surface instead of the admin/operator MCP surface.

## Overview

DREAMLAND needs an independent-evaluator path that can exercise the client
command experience without exposing operator tools, diagnostics, telemetry, or
simulation-specific framing. The existing `/mcp` endpoint is intentionally
operator-facing and remains the right surface for maintainers and test agents
who need traceability, inspection, and cleanup controls.

## Details

Expose a second Streamable HTTP MCP endpoint at `/client-mcp`. It is mounted on
the same authenticated local API server as `/mcp`, but it uses a separate
`FastMCP` server, server name, resource namespace, tool list, and MCP session
lifecycle.

The generic endpoint exposes only:

- `client://guide`;
- `client_session_create`;
- `client_run_command`;
- `client_session_exit`.

The guide and tool descriptions use generic command-client language. They must
not mention DREAMLAND internals, simulation framing, Langfuse, telemetry,
operator resources, state profiles, structured failures, or diagnostic
inspection workflows.

The generic create and run tools still use the same command backend and session
registry as admin MCP sessions so the exercised behavior matches the real
client path. Their responses intentionally omit admin correlation fields such
as command ids, history positions, inspection targets, tags, metadata, trace
records, and state diagnostics.

One narrow exception is allowed for trusted evaluation harnesses:
`client_session_create` may report the created session's state-profile label so
the harness can verify that a run spec is testing the intended scenario before
command execution begins. This profile label is not returned by
`client_run_command` and is not part of the unwitting agent's command-visible
experience.

`client_session_exit` accepts one exact session id. It does not expose dry-run,
tag, label, origin, or bulk cleanup filters. Generic client sessions receive an
internal tag so this close tool can avoid closing sessions created through the
admin MCP surface.

The existing `mcp_write_tools` gate controls mutating tools on both MCP
endpoints. With the gate disabled, `/client-mcp` still exposes its minimal
guide resource but no tools.

## Maintenance

Update this record if `/client-mcp` gains additional tools, if its public
wording changes, if the endpoint stops using the normal command backend, or if
mutating-tool gating diverges between the admin and generic MCP surfaces.
