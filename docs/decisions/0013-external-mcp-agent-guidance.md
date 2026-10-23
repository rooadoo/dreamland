# External MCP Agent Guidance

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: record why MCP workflow guidance is exposed as external-agent
documentation, resources, and prompts instead of command-agent prompt content.

## Overview

DREAMLAND's simulated command agent must only receive instructions that preserve
the emulated computing environment. Guidance about MCP tools, admin resources,
Langfuse parity, session cleanup, or testing workflows belongs to agents and
operators outside the simulation.

## Details

External agents need clear instructions for using DREAMLAND MCP to create
sessions, run ordinary user-visible commands, preserve direct command ids,
inspect live and historical behavior, compare Langfuse telemetry when needed,
and clean up sessions. That guidance is now provided through:

- `docs/MCP_AGENT_WORKFLOWS.md`;
- the MCP resource `dreamland://agent-guide`;
- MCP prompts for session testing, lifecycle cleanup, and command diagnostics;
- MCP tool descriptions that clarify direct ids, user-visible output, and
  cleanup behavior.

This guidance must not be copied into `src/dreamland/prompts/` or injected into
the simulated command-agent system prompt. Doing so would expose operator/admin
concepts to the emulated environment and violate the project model.

## Maintenance

Update this decision if DREAMLAND changes how external agents discover MCP
workflows, or if prompt ownership boundaries between external operators and
simulated command agents change.
