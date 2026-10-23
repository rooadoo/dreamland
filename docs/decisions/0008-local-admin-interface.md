# 0008 Local Admin Interface

Status: Active
Audience: maintainers, DREAMLAND operators, UI developers, and coding agents
Purpose: record the architecture for DREAMLAND's planned local API and MCP
admin bindings.

## Overview

DREAMLAND's current operator surface is the console. The console can inspect
logs, traces, Langfuse-backed sessions and commands, state deltas, state
profiles, spiritguide operations, Langfuse flushes, and shutdown. Future UIs and
coding agents need the same level of visibility and control, but they should not
depend on terminal-oriented console text.

The project also needs an MCP path so coding agents and other model-driven
clients can inspect a running DREAMLAND instance directly. MCP tools are
model-invokable, so mutation authority needs a stricter default than ordinary
local admin REST calls.

## Decision

Create a protocol-neutral admin service layer as the canonical interface for
DREAMLAND operator capabilities. The console, HTTP REST API, and MCP binding
should converge on that typed service layer instead of duplicating behavior or
parsing each other's rendered output.

Expose the first HTTP API as an optional same-process local server. It is
disabled by default, starts only during DREAMLAND startup, and is protected with
a configured bearer token. The initial assumption is local use on the same
system, so transport encryption is not required for the first binding.

Use REST JSON for UI-oriented snapshot and control endpoints. Use server-sent
events for live logs, traces, and runtime events after the snapshot endpoints
are stable.

Expose MCP as a sibling binding over the same admin service, initially on the
same optional local server. MCP read-only data should be resources. MCP
mutations should be tools and must be disabled by default behind an explicit
configuration gate. This allows coding agents to inspect DREAMLAND safely while
requiring deliberate operator opt-in before a model can apply profiles, run
spiritguide, flush introspection, or request shutdown.

Do not implement web UI code in the main DREAMLAND project. The main project
owns the admin capabilities and protocol bindings required by UIs. UI projects
own web presentation and web-specific UX.

Keep web and MCP dependencies optional. Normal DREAMLAND operation without the
admin server must not require API framework imports or MCP server imports.

## Deferred Decisions

The initial interface does not create web-native DREAMLAND user sessions. SSH
remains the active user entry point. A future version may add browser-based
frontend sessions that supplement SSH.

The initial interface may continue to rely on Langfuse for historical command,
usage, cost, and inspection data. If that creates unacceptable limitations, a
future local observability store should be designed deliberately instead of
being added incidentally to the API layer.

The initial MCP transport is planned as Streamable HTTP for access to a running
DREAMLAND process. A future stdio bridge may be useful for clients that prefer
launching MCP servers as subprocesses, but that bridge should connect to the
running local admin server rather than owning runtime state itself.

The unmerged `feature/model-tuning-prompts` branch changes prompt and model
configuration behavior. Before implementing config/status API surfaces, review
whether that branch should be merged or reflected in the admin service model.

## Maintenance

Update this record if the canonical admin boundary moves, if REST or MCP stops
using the shared service layer, if MCP mutating tools become enabled by default,
if the API becomes remote-first, or if browser-based DREAMLAND user sessions
become part of the admin interface scope.
