# 0002 Layered SSH Tests

Status: Active
Audience: maintainers and coding agents
Purpose: record the decision to keep real SSH tests focused on transport
behavior.

## Overview

The original SSH test module mixed Paramiko/socket integration coverage with
session-loop, shell-state, editor, console-routing, and nested-program behavior.
That made the default test suite slow and made many failures harder to localize.

## Decision

Keep `tests/test_ssh_server.py` focused on behavior that needs real Paramiko
transport or sockets. Move direct shell session behavior into
`tests/test_ssh_sessions.py`, and move SSH shell close/pop state behavior into
`tests/test_ssh_session_state.py`.

Real SSH tests should remain for:

- authentication and exec request wiring;
- shell channel smoke coverage;
- remote console transport smoke paths;
- connection metadata observed from sockets;
- client disconnect cleanup;
- Ctrl-C behavior that depends on channel polling.

## Consequences

The full suite is faster and test failures point to a narrower layer. New tests
should start at the owning layer and use real SSH only when transport behavior
is the point.

This does not remove integration coverage. It keeps a smaller transport suite
while preserving moved behavior through direct unit-level tests.

## Maintenance

Update this decision if the test layout changes, SSH ceases to be the main
transport, or another integration-test grouping replaces the current modules.
