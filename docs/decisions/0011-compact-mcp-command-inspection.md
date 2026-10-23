# Compact MCP Command Inspection

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: record why MCP command inspection defaults to compact payloads.

## Overview

MCP test agents need to correlate command results with Langfuse traces without
receiving large serialized session state on routine inspection calls. Full
Langfuse payloads remain useful for prompt debugging and state-transition
diagnosis, but they are expensive and can obscure the user-visible command
result an agent is trying to verify.

## Decision

Keep REST command inspection compatible by defaulting
`GET /api/v0/sessions/{session_id}/commands/{command_id}` to `detail=full`.
Default MCP command inspection to `detail="compact"` for both the parameterized
tool and command-inspection resource.

Compact inspection returns the command summary and lightweight observation
summaries while omitting raw model input, raw model output, and full observation
metadata. Full inspection remains available by passing `detail="full"` when a
diagnostic workflow needs raw Langfuse payloads.

This keeps the normal MCP agent workflow small and traceable: create a tagged
session, run a user-visible command, inspect compact command/trace summaries,
and escalate to full payloads only when diagnosing prompts or state behavior.

## Maintenance

Update this decision if MCP command inspection adds more detail modes, if REST
defaults change, or if Langfuse payload shaping moves into a separate
introspection profile system.
