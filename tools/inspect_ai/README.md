# DREAMLAND Inspect AI Harness

Status: Draft
Audience: DREAMLAND maintainers and trusted developers
Purpose: provide an optional local evaluation harness for exploratory DREAMLAND
testing without coupling Inspect AI code into the runtime package.

## Overview

This directory is for Inspect AI tasks that drive DREAMLAND as an external user.
The harness should connect through public or deliberately test-facing
interfaces, such as SSH, the admin API, captured logs, and exported state
snapshots. Product runtime code belongs under `src/dreamland`; evaluation code
belongs here unless it exposes a reusable DREAMLAND API needed by other
operators or tools.

See `docs/INSPECT_AI_HARNESS.md` for the durable run-spec format, run
artifacts, and score interpretation guide.

## Install

Install the optional dependencies from the repository root:

```sh
pip install -e ".[inspect]"
```

Use the project virtual environment when working locally:

```sh
.venv/bin/python -m pip install -e ".[inspect]"
```

The `inspect` extra includes Inspect AI and the MCP client library used by the
live DREAMLAND client driver.

## Layout

- `dreamland_inspect/agents/`: agent prompts, personas, and solver helpers.
- `dreamland_inspect/drivers/`: DREAMLAND process, SSH, admin API, and log
  capture helpers. `CommandEventRecorder` is the first shared writer for
  `run.commands.jsonl`-style artifacts.
- `dreamland_inspect/events.py`: JSONL command-event records captured from
  client sessions.
- `dreamland_inspect/parsers/`: lightweight parsers for compound command output
  and command observations.
- `dreamland_inspect/scorers/`: Inspect scorers for consistency, believability,
  task success, and transcript checks.
- `dreamland_inspect/tasks/`: Inspect task definitions.
- `dreamland_inspect/fixtures/`: small deterministic fixtures for harness tests.
- `dreamland_inspect/profiles/`: evaluation-only world or device profiles.
- `outputs/`: local Inspect logs and run artifacts; ignored by git.

## Command Event Generation

The trusted harness owns `run.commands.jsonl`. The unwitting agent should only
interact with DREAMLAND through the bounded client MCP tools. The harness driver
calls `client_session_create`, `client_run_command`, and `client_session_exit`,
then writes each user-visible command result through `CommandEventRecorder`.

The current live-driver building blocks are:

- `ClientMcpDriver`: runs commands through a client MCP session and records
  command events with harness-local ids such as `run-0001`.
- `open_http_client_mcp`: opens a streamable-HTTP MCP client session for a
  running DREAMLAND client MCP endpoint, usually `/client-mcp`.
- `run_command`: the only tool exposed to the unwitting Inspect agent. It
  returns command-visible stdout, stderr, exit status, and a prompt-like
  post-command context line such as `shell on user@host:/cwd`. It enforces a
  charged command budget while allowing successful standalone SSH pivots that
  change host context to draw from a separate free-pivot allowance.

## Automated Client MCP Exploration

Start DREAMLAND with the admin API and client MCP write tools enabled. Export
the bearer token as `DREAMLAND_MCP_AUTH`, then run a trusted run spec:

```sh
export DREAMLAND_MCP_AUTH="<api bearer token>"

PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect run \
  tools/inspect_ai/dreamland_inspect/runs/specs/ridgeview-baseline.toml
```

The run helper creates a unique directory under:

```text
tools/inspect_ai/outputs/runs/<timestamp>_<spec-id>_<suffix>/
```

Each run directory contains a manifest, preflight result, the exact Inspect
invocation, the command JSONL artifact, Inspect logs, postflight summary,
extracted agent report, any matched structured emulator failures, and a
post-run score summary. The run spec can declare an expected DREAMLAND state
profile. By default, the trusted harness applies that profile as DREAMLAND's
process-local default before launching Inspect, validates that the created
client session reports the same profile before the unwitting agent runs
commands, and flags mixed-session, duplicate-command, or wrong-profile command
artifacts.

When `expected_themes` are present in the run spec, `score.json` also includes
a deterministic `scenario_conformance` section. This is intentionally separate
from `command_realism`: a run can have clean command artifacts while still
showing partial theme coverage that should guide future scenario or agent
tuning.

`structured_failures.json` records DREAMLAND structured-failure capsules that
match the run's client session and command text. `score.json` includes those
under `emulator_integrity`, because malformed internal state-update envelopes
can indicate emulator problems even when the command output shown to the
unwitting agent looked plausible.

`score.json` and `postflight.json` also include `command_budget`, which reports
raw commands, charged commands, free SSH pivots earned, and configured caps.
BatchMode SSH and quoted one-off remote SSH are blocked before reaching
DREAMLAND and recorded as nonzero harness rejections during automated
exploration; use standalone commands like `ssh bastion-ot` so subsequent
commands run from the pivoted shell.

For a focused stress test of nested SSH state transitions, run:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect run \
  tools/inspect_ai/dreamland_inspect/runs/specs/regional-utilities-corridor-bare-ssh-pivot-stress.toml
```

That spec uses the `bare-ssh-pivot-stress` task variant, which explicitly asks
the unwitting agent to discover targets, issue standalone bare SSH commands,
verify basic OS and session details after each pivot, and keep pivoting deeper.

The unwitting-agent objective is intentionally scenario-generic. Scenario facts
belong in DREAMLAND state profiles and trusted run specs, not in the agent
prompt.

Run the underlying Inspect task directly only when you need to override the
run-helper behavior:

```sh
export DREAMLAND_MCP_AUTH="<api bearer token>"

XDG_DATA_HOME="$PWD/tools/inspect_ai/outputs/data" \
XDG_CACHE_HOME="$PWD/tools/inspect_ai/outputs/cache" \
PYTHONPATH=tools/inspect_ai \
.venv/bin/inspect eval tools/inspect_ai/dreamland_inspect/tasks/client_mcp_explore.py \
  --model openai/gpt-5.4-mini \
  -T mcp_url=http://127.0.0.1:8765/client-mcp \
  -T run_id=ridgeview-auto-001 \
  -T max_commands=20 \
  -T free_ssh_pivot_allowance=10 \
  -T command_timeout_seconds=60 \
  --log-dir tools/inspect_ai/outputs \
  --display plain
```

The task default is also `openai/gpt-5.4-mini`; passing `--model` makes the
unwitting-agent model explicit and is the preferred way to compare model runs.
Each command has a 60-second timeout by default. A timeout is recorded in
`run.commands.jsonl` as a failed command with exit status `124`, then returned
to the unwitting agent so it can continue.

When invoked directly without `commands_path`, the trusted harness writes
command events to:

```text
tools/inspect_ai/outputs/client-mcp/<run_id>/run.commands.jsonl
```

Score the generated command artifact:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect evaluate \
  tools/inspect_ai/outputs/client-mcp/ridgeview-auto-001/run.commands.jsonl
```

Review trusted run directories without launching Inspect:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect review --recent 5
```

Print the command timeline for a run when validating pivot claims:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect timeline \
  tools/inspect_ai/outputs/runs/<run-id>
```

## Boundaries

Keep `inspect_ai` imports out of `src/dreamland`. When the harness needs better
visibility into DREAMLAND, prefer adding a small runtime/admin API with ordinary
tests over reaching into private internals from this directory.

## First Evaluation Slice

The first local evaluator accepts command events, splits compound output by
agent section markers such as `== identity ==`, extracts lightweight host,
network, DNS, and nmap observations, and emits findings. The highest-priority
initial finding is an exposed structured DREAMLAND result envelope in stdout,
because that leaks the simulation boundary into the user-visible terminal.

Run the focused harness tests from the repository root:

```sh
.venv/bin/python -m unittest tests.test_inspect_harness
```

Evaluate a command-event JSONL artifact:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect evaluate tmp/ridgeview.commands.jsonl
```

Emit JSON:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect evaluate --json tmp/ridgeview.commands.jsonl
```

Run the same evaluator through Inspect AI:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/inspect eval \
  tools/inspect_ai/dreamland_inspect/tasks/command_realism.py \
  -T commands_path=tmp/ridgeview.commands.jsonl \
  --log-dir tools/inspect_ai/outputs \
  --display none
```

If Inspect cannot create its default local data directory in the current
environment, point it at the ignored outputs directory:

```sh
XDG_DATA_HOME="$PWD/tools/inspect_ai/outputs/data" PYTHONPATH=tools/inspect_ai \
  .venv/bin/inspect eval tools/inspect_ai/dreamland_inspect/tasks/command_realism.py \
  -T commands_path=tmp/ridgeview.commands.jsonl \
  --log-dir tools/inspect_ai/outputs \
  --display none
```
