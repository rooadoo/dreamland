# Structured Failure Capsules

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: describe how DREAMLAND captures malformed structured command-agent
responses for retrospective diagnosis and replay.

## Overview

DREAMLAND command agents are expected to return a structured result envelope
with terminal streams, exit status, and state updates. When a model response
looks like a structured envelope but fails parsing or validation, the failure is
valuable operational evidence. DREAMLAND captures that evidence in a durable
structured failure capsule before applying command state changes or appending
the command to session history.

Capsules are an observability and replay facility. They do not currently retry
the model call, change terminal fallback behavior, or alter parser policy for
salvaged output. Those decisions are intentionally deferred until real failure
patterns can be inspected.

## Details

Phase 1 captures capsules for command results whose metadata contains
`structured_response_accepted: false`, a `structured_parse_error`, and the raw
structured response text recorded by the parser. Built-in commands and ordinary
unstructured terminal output are not captured.

Capsules are stored as JSON under:

```text
state_profiles/structured_failures/
```

Each capsule records:

- capsule id and timestamp;
- session id, command id, command text, starting cwd, user, hostname, and
  the selected saved state-profile name (`unsaved` for an ad hoc session);
- parser error, failure class, and raw model response;
- result stdout, stderr, exit status, and metadata;
- an embedded exact state profile representing the pre-command session state.

The embedded state profile is not written into the ordinary state profile list.
This keeps diagnostic captures separate from operator-authored resume profiles
while still preserving the data needed to build replay tooling.

Failure classes currently include:

- `invalid_json`;
- `wrong_top_level_type`;
- `missing_required_keys`;
- `invalid_state_update`;
- `validation_error`.

The first replay primitive is parser-only replay. It runs a capsule's raw model
response through the current structured parser without calling the model or
mutating state. This is intended for parser regression tests and policy
experiments.

The native Dreamland console exposes capsule inspection:

```sh
failures list
failures show CAPSULE_ID
failures show CAPSULE_ID --json
failures replay-parser CAPSULE_ID
```

The MCP binding exposes read-only resources:

```text
dreamland://structured-failures
dreamland://structured-failures/{failure_id}
dreamland://structured-failures/{failure_id}/replay-parser
```

When MCP tools are explicitly enabled, parameterized tools are also available:

```text
dreamland_structured_failures_list
dreamland_structured_failure_show
dreamland_structured_failure_replay_parser
```

Valid emulator output can contain JSON text with keys such as `stdout`,
`stderr`, `exit_status`, and `state_update` inside the outer structured
envelope's `stdout` field. Parser replay and capture only classify the outer
model response envelope. DREAMLAND must not recursively interpret terminal
stream content as control data.

Some providers occasionally wrap the whole structured envelope in a Markdown
code fence, commonly as `json`. DREAMLAND accepts a complete response-level
fence when the fenced body is a JSON object envelope. This tolerance is limited
to the outer model response; fenced or JSON-looking text inside terminal streams
remains ordinary terminal output.

Manual Claude testing also showed a mixed response shape where Markdown terminal
output and a short explanation preceded a valid trailing command-response
envelope. DREAMLAND accepts that trailing object only when it is a complete
outer envelope with the required `stdout`, `stderr`, `exit_status`, and
`state_update` keys. Other trailing JSON remains terminal output.

Foreground structured-output reliability is a separate emulator concern;
EnrichmentAgent does not own parser repair or salvage.

Deferred phases:

- restoring a capsule's embedded pre-command state into a fresh replay session;
- converting capsules into curated test fixtures;
- correlating rejected foreground envelopes with downstream loss of
  user-visible evidence for later command contexts and background agents;
- retry and terminal-output salvage policy.

## Maintenance

Update this document when the capsule schema, capture trigger, storage location,
replay behavior, or operator-facing inspection tools change. Keep
`docs/AGENT_STATE.md`, `docs/ARCHITECTURE.md`, and `ROADMAP.md` aligned with
implemented structured-failure behavior.
