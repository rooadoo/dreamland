# Inspect SSH Pivot Budget

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: record why Inspect AI exploration separates charged commands from
raw command events and blocks SSH shortcut forms.

## Overview

The Inspect AI harness needs unwitting agents to explore DREAMLAND through
stateful shell pivots, not only through one-off remote SSH commands. One-off
SSH can produce plausible target-host output, but it does not move the client
MCP session context. That makes it poor evidence for N-deep exploration and
can cause reports to overstate pivot depth.

Use separate accounting for raw command events and agent-facing charged
commands. Keep every executed command in `run.commands.jsonl`, but exempt
successful standalone SSH pivots from the charged command budget when they
actually change host context.

## Details

The `run_command` tool enforces a charged-command budget from `max_commands`
and a separate raw-command safety cap of `max_commands +
free_ssh_pivot_allowance`. The default free SSH pivot allowance is `10`.

A command earns a free pivot only when all of these are true:

- The command is a standalone SSH invocation such as `ssh bastion-ot`.
- The command exits successfully.
- Post-command context shows the hostname changed from the previous command.

Failed SSH attempts, `exit`, local inspection commands, BatchMode SSH, quoted
one-off remote SSH, and ambiguous/chained SSH commands remain charged or are
not eligible for the free pivot allowance.

BatchMode SSH and quoted one-off remote SSH commands are disabled in automated
exploration. The tool records a nonzero harness-rejection command event and
returns guidance that tells the agent to run a standalone interactive SSH pivot
instead. The rejected shortcut is not sent to DREAMLAND, which prevents the
emulator from rewarding shortcut behavior with useful remote-host output while
still letting the agent recover and continue.

Run artifacts report both forms of accounting:

- `raw_command_count`: every command recorded in `run.commands.jsonl`.
- `charged_command_count`: commands counted against `max_commands`.
- `free_ssh_pivot_count`: successful host-changing standalone SSH pivots.
- `raw_command_cap`: hard safety cap for total tool calls.

Pivot scoring separately reports attempted bare SSH pivots and successful
context-changing pivots. `overall_score` includes pivot quality so a run that
produces plausible command output but fails to update session context during
SSH pivoting is visible in the top-line result.

The unwitting-agent prompt may tell the agent that successful standalone SSH
pivots are free, because that statement is enforced by the tool wrapper. The
raw audit trail remains complete.

## Maintenance

Update this record when SSH command classification changes, when the client
MCP surface adds a first-class pivot tool, when command-budget semantics change,
or when BatchMode/one-off SSH handling becomes configurable per run spec.
