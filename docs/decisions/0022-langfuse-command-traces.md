# 0022 Langfuse Command Traces

Status: Active
Audience: maintainers, DREAMLAND operators, and coding agents
Purpose: record how DREAMLAND separates Langfuse command trace identity from
the stable DREAMLAND session id.

## Overview

DREAMLAND originally used `SessionState.langfuse_trace_id` as both the
DREAMLAND session identifier and the explicit Langfuse trace id for LangGraph
command callbacks. That made each shell or MCP session appear as one large
Langfuse trace. Langfuse also has session semantics that group observations
across multiple traces by `sessionId`, which better matches DREAMLAND command
inspection: one operator session contains many command executions.

## Decision

LangGraph command-agent invocations no longer pass the DREAMLAND session id as
an explicit Langfuse `trace_id`. The Langfuse callback handler can create a
fresh trace for each command invocation.

DREAMLAND continues to propagate the stable session id as
`langfuse_session_id` metadata so Langfuse groups all command traces under one
session and console introspection can continue querying command observations by
`sessionId`.

Do not rename `SessionState.langfuse_trace_id` in this phase. The field is used
as the live-session registry key, local trace-log key, state-profile provenance
id, and LangGraph checkpoint `thread_id`. Renaming it should be a separate
compatibility cleanup.

Do not add backend-level manual Langfuse traces for console commands, empty
commands, or deterministic fallback-agent built-ins in this phase. The normal
production command runtime uses `LangGraphCommandAgent`, where built-ins such as
`pwd` route through the graph and inherit the same command tracing behavior.

## Maintenance

Update this record if command tracing moves to the backend execution layer, if
the session id field is renamed, or if deterministic fallback command tracing
becomes part of the supported operator observability contract.
