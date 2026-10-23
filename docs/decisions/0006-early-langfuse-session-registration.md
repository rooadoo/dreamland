# 0006 Early Langfuse Session Registration

Status: Active
Audience: maintainers, DREAMLAND operators, and coding agents
Purpose: record how DREAMLAND makes new SSH sessions visible for operator
actions before the first command runs.

## Overview

State-profile apply and spiritguide workflows can target live sessions through
the local registry, but operators also use Langfuse-backed session lists to find
session identifiers. Before this decision, a fresh SSH shell session had a local
`SessionState` and live registry entry before the first prompt, but it did not
necessarily appear in the Langfuse sessions table until a command-agent
observation was created.

Related implementation commit:

- This decision is implemented by the same feature commit that adds
  `dreamland-session-start` tracing.

## Decision

Record a lightweight Langfuse event named `dreamland-session-start` when an SSH
shell or exec session is constructed. This happens after the SSH channel request
selects shell or exec mode and before any user command is executed.

Use the existing `SessionState.langfuse_trace_id` as both the trace context and
Langfuse session id. Propagate Langfuse trace attributes for user id, session
id, trace name, tags, and compact Dreamland metadata so Langfuse session queries
can surface the fresh session without waiting for command-agent activity.

Flush the Langfuse client after the startup event. This favors operator
ergonomics for immediate profile application over batching the single startup
event indefinitely.

Do not move DREAMLAND session creation into Paramiko authentication callbacks.
Authentication can complete before a usable shell or exec channel exists, and
the current session ownership model lives in `ShellSession` and `ExecSession`.
Moving ownership earlier would require extra cleanup paths for authenticated
connections that never request a supported session channel. That remains
available as future work if DREAMLAND needs to observe authenticated idle
connections.

Keep the event optional with the existing Langfuse settings. If Langfuse is
disabled or unavailable, SSH session startup continues without blocking the
user-facing session.

## Maintenance

Update this record when SSH session ownership moves earlier in the transport
stack, when Langfuse session semantics change, or when startup observability
adds additional durable event types.
