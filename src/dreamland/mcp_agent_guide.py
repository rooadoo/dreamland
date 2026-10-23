"""Agent-facing guidance for using DREAMLAND through MCP."""

MCP_AGENT_GUIDE = """# DREAMLAND MCP Agent Guide

Audience: external coding agents, testing agents, and DREAMLAND operators.

Purpose: use DREAMLAND from the outside through MCP. This guidance is not part
of the simulated command-agent prompt or the emulated user environment.

## Core Model

DREAMLAND MCP lets an external agent create and inspect real DREAMLAND sessions
without SSH. A command sent through dreamland_session_run_command is an ordinary
user-visible shell command. Treat its stdout, stderr, exit_status, and
interrupted fields as the result a user would see from that session.

MCP metadata exists for correlation and cleanup. Do not mistake diagnostic
metadata for user-visible terminal output.

Never run !dreamland inside an MCP command session. A normal user does not have
that shell command. Use explicit DREAMLAND MCP resources and tools for admin,
inspection, lifecycle, and operator actions.

## Tool Categories

- Discovery: dreamland://status, dreamland://capabilities, and
  dreamland://agent-guide.
- Session lifecycle: dreamland_session_create, dreamland_live_sessions_list,
  dreamland_sessions_list, dreamland_sessions_close, and
  dreamland_session_tag_groups.
- User commands: dreamland_session_run_command.
- Live inspection: dreamland_session_trace_read and live session resources.
- Enrichment inspection: dreamland_session_enrichment_events_read, optionally
  filtered by exact job_id; records remain readable after the session closes
  until their per-session process-memory buffer evicts them or DREAMLAND restarts.
- Historical inspection: dreamland_session_commands_list,
  dreamland_session_command_inspect, and dreamland_session_state_delta.
- Telemetry parity: dreamland_introspection_flush before historical inspection;
  Langfuse MCP only when DREAMLAND no longer has live context or when comparing
  telemetry sources.
- Operator controls: state profile, spiritguide, and shutdown tools. Use these
  only for explicit operator workflows, not as shell commands.

## Evaluation Configuration

dreamland_session_create accepts username, state_profile, model,
openai_service_tier, and config_overrides.
Select the profile at creation for a clean starting world without changing the
process default. config_overrides merges agent, spiritguide, and enrichment
objects with process settings. Model strings use provider:model; enrichment
contains coordinator, ambient_coordinator, expert, and integrator role objects
plus scheduling controls such as enabled and apply_mode. Direct model and
openai_service_tier alias config_overrides.agent fields for the command emulator;
conflicting duplicates are rejected. OpenAI command sessions use the default
(standard) tier when neither form supplies one, even if the process uses Fast.
Other omitted values inherit. Nested null clears optional role controls;
null or auto for the nested OpenAI tier can inherit a Fast project setting.
Direct null means omission. Credentials and prompt paths are not accepted.
Check capabilities.evaluation_overrides before requesting model overrides.

Save the returned evaluation_config and its sha256 with the run manifest. It
records resolved request settings, not provider-delivered tier or latency.
Every model-backed MCP session has independent agents and enrichment scheduling,
including sessions with no overrides. Settings are fixed at creation; use a new
session to change models or tiers. The minimal client_session_create still
accepts only username and receives the same standard OpenAI command default.
The three detached Spiritguide profile tools also accept config_overrides, with
only the spiritguide section. Their result metadata records the configuration
and detached session_id. A dry run calls the model without saving a profile.

Applying a profile replaces simulated state while preserving the current login,
connection provenance, tags, label, custom MCP metadata, and evaluation settings.
An exact snapshot still restores its effective shell user and command history.
World/system profiles use the current login and expand username templates.

## Normal Test Workflow

1. Read dreamland://status and dreamland://capabilities.
2. Create a session with dreamland_session_create. Add tags for agent id, run id,
   and scenario id. Use a label when the run has a stable name.
3. Save the full session_id. Agents should use full ids, not abbreviated ids.
4. Run user-visible commands with dreamland_session_run_command.
5. For each result, save command_id, stdout, stderr, exit_status,
   history_position, and inspection_target.
6. Use stdout, stderr, exit_status, interrupted, and signal to understand what
   the user experienced.
7. Use dreamland_session_trace_read to review live input/output records.
8. When testing enrichment, use dreamland_session_enrichment_events_read with
   the submitted job_id to correlate trigger, graph, patch, and readiness events.
9. Use dreamland_session_commands_list and dreamland_session_command_inspect
   with the returned direct command_id for historical command diagnostics.
10. Use dreamland_session_state_delta when you need to explain state changes
   between commands.
11. Close sessions when finished. Prefer exact cleanup with
    dreamland_sessions_close using session_ids, origin="mcp", and
    dry_run=false. Use tag, label, or origin-only filtered cleanup only after
    dry_run=true and human/operator approval.

## Command Diagnostics Workflow

When a command behaves unexpectedly:

1. Record the session_id, command_id, command, stdout, stderr, and exit_status.
2. Read the live trace with dreamland_session_trace_read.
3. Inspect the command by direct command_id with
   dreamland_session_command_inspect.
4. If needed, inspect state change with dreamland_session_state_delta.
5. Flush telemetry with dreamland_introspection_flush before using historical
   data that depends on Langfuse ingestion.
6. If dreamland_session_commands_list or dreamland_session_command_inspect does
   not show a recent command immediately after flush, retry after a short delay.
   Live trace is the immediate source for current user-visible input/output;
   historical command inspection depends on telemetry ingestion.
7. Prefer DREAMLAND MCP while the session is live. Use Langfuse MCP for
   historical session analysis after live DREAMLAND state is gone or for
   explicit telemetry parity checks.

## Reporting Expectations

When reporting a test result, include:

- session_id
- tags and label
- each command and command_id
- stdout, stderr, and exit_status for user-visible behavior
- the DREAMLAND MCP inspection tools used
- any Langfuse comparison, clearly labeled as telemetry/historical data
- cleanup result

Keep output compact. Include only metadata needed to correlate behavior,
diagnose failures, or clean up sessions.
"""


def mcp_agent_guide_text() -> str:
    """Return the current external-agent MCP guide."""
    return MCP_AGENT_GUIDE.strip()
