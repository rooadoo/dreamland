# 0018 Detached Spiritguide Profile Workflows

Status: Active
Audience: maintainers, DREAMLAND operators, coding agents, and API clients
Purpose: record why broad Spiritguide world-building is modeled as detached
state-profile work instead of live-session retheming.

## Overview

Spiritguide initially adjusted one live session at a time. That worked for
small operator corrections, but broad new-world creation risked retheming an
existing session with stale scenario facts, command history, artifacts, users,
services, and network assumptions still present.

Operators also need an iterative way to build reusable worlds before applying
them to user-facing sessions. A live blank-slate option would reset a session,
but it would not make detached world construction a first-order admin workflow.

## Decision

Make detached state profiles the primary Spiritguide world-building workflow.
The admin surface supports:

- creating a new profile from a packaged blank world state;
- updating that saved profile through follow-up Spiritguide instructions;
- forking an existing profile, optionally with another Spiritguide instruction.

The default save level is `world`. World profiles preserve durable world facts,
known hosts, services, expansion directives, established facts, and registered
generated artifacts while excluding live-session position such as cwd, shell
stack, interactive program, and command history. Operators can request `system`
level when reusable host identity or system metadata should also survive.

Detached Spiritguide runs use synthetic sessions so existing Spiritguide prompt
and state-update handling can be reused. Synthetic session trace ids must remain
valid 32-character lowercase hex ids because Langfuse accepts trace ids only in
that format. Operation names and profile names belong in logs, result fields,
profile metadata, or comments, not in the trace id itself.

## Consequences

World-building no longer requires an active SSH session. Operators and MCP
clients can create, review, update, fork, and later apply profiles to sessions.
This reduces stale-history leakage and gives future UI work a protocol-neutral
backend workflow.

The feature intentionally reuses state-profile storage rather than creating a
new detached-session registry. That keeps the implementation small, but it
means detached profile iteration currently has profile snapshots rather than
rich live-session inspection, undo history, or per-step review/apply workflows.
Those can be added later if operator review needs grow.

## Maintenance

Update this record when detached profile create/update/fork semantics change,
when profile levels gain new preserved sections, or when DREAMLAND adds a
separate persistent detached-session workspace.
