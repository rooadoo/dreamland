# 0014 Structured Failure Capsules

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: record why malformed structured command-agent responses are captured
before retry or salvage policy is changed.

## Overview

Malformed structured model responses can leak DREAMLAND's internal result
envelope into terminal output and can leave maintainers without enough context
to understand whether the root cause was parser behavior, prompt pressure,
state drift, model sampling, or a real command-output ambiguity.

Rather than immediately adding retries or changing terminal salvage behavior,
DREAMLAND now captures structured failure capsules. Capsules preserve the raw
model response and exact pre-command state so maintainers can retrospect,
replay parser behavior, and build a failure corpus before changing user-visible
runtime policy.

## Details

When the command-agent parser rejects a structured-looking response, it records
metadata on the `CommandResult`:

- `structured_response_accepted: false`;
- `structured_parse_error`;
- `dreamland.structured.failure_class`;
- `dreamland.structured.raw_response`.

`AgentBackend` treats that metadata as a capture trigger. Before applying the
result's state update or appending command history, it writes a capsule under
`state_profiles/structured_failures/`. The capsule embeds an exact state
profile for the pre-command session state. It also records command/session
identity and the parser diagnosis.

This decision intentionally keeps diagnostic captures separate from normal
state profiles. Operators should not see automatic failure capsules as ordinary
boot profiles unless a later tool explicitly exports one.

Operators can inspect capsules through the native console and MCP binding. Both
surfaces expose listing, detail, and parser-only replay. REST endpoints are
intentionally not part of the initial inspection surface.

This decision also defers retry and salvage policy. Valid terminal output may
itself contain JSON text with keys such as `stdout`, `stderr`, `exit_status`,
and `state_update`; future parser changes must preserve the distinction between
the outer model envelope and terminal stream content inside that envelope.

## Maintenance

Revisit this decision after console/API/MCP capsule inspection, pre-command
state replay, and a curated failure fixture corpus exist. If retry or salvage
policy changes, add a follow-up decision that cites the failure evidence that
motivated the runtime behavior.
