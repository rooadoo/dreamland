# Inspect AI Harness

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: document how to install, run, read, and extend the optional Inspect AI
evaluation harness.

## Overview

The Inspect AI harness lives under `tools/inspect_ai/` and drives DREAMLAND as
an external client through the bounded `/client-mcp` surface. It is trusted
test infrastructure. The unwitting Inspect agent sees only user-visible command
results from `run_command`; it must not receive admin state, truth profiles,
scoring results, or scenario-specific prompt hints.

Use this harness to run repeatable exploration tests against a declared
DREAMLAND state profile, capture command-event JSONL, extract the agent report,
and score command realism, scenario coverage, artifact integrity, and internal
emulator failures.

## Details

Install the optional dependencies from the repository root:

```sh
.venv/bin/python -m pip install -e ".[inspect]"
```

Start DREAMLAND with the admin API and client MCP enabled. Export the admin
bearer token for the trusted harness:

```sh
export DREAMLAND_MCP_AUTH="<api bearer token>"
```

Run a trusted spec:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect run \
  tools/inspect_ai/dreamland_inspect/runs/specs/ridgeview-baseline.toml
```

After a live run, the command prints the run directory, Inspect return code,
score breakdown, finding counts by type, and direct paths for the score,
structured failures, command JSONL, and extracted agent report.

Review existing runs without launching Inspect:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect review \
  --recent 5
```

Pass one or more run directories to review specific runs:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect review \
  tools/inspect_ai/outputs/runs/<run-id>
```

Print the command-by-command host timeline for a run:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect timeline \
  tools/inspect_ai/outputs/runs/<run-id>
```

The timeline view shows host transitions first, then each command with compact
markers such as `ssh`, `pivot-ok`, `nonstandalone-pivot`, `embedded-ssh`,
`remote-ssh`, `batchmode`, and `rejected`. Use this when checking whether an
agent report's claimed pivot chain is supported by command-event metadata.

Use `--dry-run` to check the preflight metadata and generated Inspect
invocation without launching the unwitting agent:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect run \
  tools/inspect_ai/dreamland_inspect/runs/specs/ridgeview-baseline.toml \
  --dry-run
```

### Run Spec Format

Run specs are TOML files under `tools/inspect_ai/dreamland_inspect/runs/specs/`.
Keep reusable agent behavior scenario-neutral; put scenario identity and
expected coverage in the spec and state profile.

Supported fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | Yes | Stable human-readable run id. It is used in output directory names. |
| `description` | No | Trusted maintainer description of the run. |
| `state_profile` | No | Expected DREAMLAND profile label, usually `<profile>:system`. |
| `apply_state_profile` | No | Defaults to `true`; applies the profile as DREAMLAND's process-local default before Inspect starts. |
| `username` | No | Client username for the unwitting session. Defaults to `user`. |
| `mcp_url` | No | Client MCP endpoint. Defaults to `http://127.0.0.1:8765/client-mcp`. |
| `agent_model` | No | Provider-qualified Inspect model. Defaults to `openai/gpt-5.4-mini`. |
| `max_commands` | No | Charged command budget exposed to the unwitting agent. |
| `free_ssh_pivot_allowance` | No | Extra raw-command allowance for successful standalone SSH pivots that change host context. Defaults to `10`. |
| `command_timeout_seconds` | No | Per-command timeout. Timeouts are recorded as exit status `124`. |
| `message_limit` | No | Inspect solver message limit. |
| `task_variant` | No | Unwitting-agent prompt variant. Defaults to `explore`; use `bare-ssh-pivot-stress` for deep standalone SSH state-transition stress tests. |
| `expected_themes` | No | Scenario-neutral scoring terms expected in commands or the final report. |

Example:

```toml
id = "ridgeview-baseline"
description = "Baseline exploration of a public school district scenario."
state_profile = "school-district-network:system"
apply_state_profile = true
username = "user"
mcp_url = "http://127.0.0.1:8765/client-mcp"
agent_model = "openai/gpt-5.4-mini"
max_commands = 20
free_ssh_pivot_allowance = 10
command_timeout_seconds = 60
message_limit = 50
task_variant = "explore"
expected_themes = [
  "public school district",
  "schools",
  "students",
  "teachers",
  "staff",
]
```

Use the focused bare-SSH stress spec when validating nested SSH state
transitions:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect run \
  tools/inspect_ai/dreamland_inspect/runs/specs/regional-utilities-corridor-bare-ssh-pivot-stress.toml
```

That variant instructs the unwitting agent to discover targets from observed
evidence, issue standalone `ssh host` or `ssh user@host` commands by themselves,
verify basic OS and session details after each pivot, and continue deeper
rather than using quoted, piped, BatchMode, or one-off SSH.

### Run Artifacts

Each run creates:

```text
tools/inspect_ai/outputs/runs/<timestamp>_<spec-id>_<suffix>/
```

Important files:

| File | Meaning |
| --- | --- |
| `run.manifest.json` | Trusted run contract: spec, run id, artifact paths. |
| `preflight.json` | State-profile application result or dry-run preflight. |
| `inspect.invocation.json` | Exact Inspect command and dry-run flag. |
| `run.commands.jsonl` | User-visible command events captured by the trusted driver. |
| `inspect-logs/*.eval` | Native Inspect log archive. |
| `postflight.json` | Extracted Inspect metadata, final report, and structured-failure summary. |
| `agent_report.md` | The unwitting agent's final report when available. |
| `structured_failures.json` | Structured DREAMLAND failure capsules matched to this run. |
| `score.json` | Post-run scoring summary. |

### Reading Scores

`score.json` keeps related score dimensions separate:

| Section | Meaning |
| --- | --- |
| `command_realism` | Plausibility and cleanliness of user-visible command output. |
| `scenario_conformance` | Deterministic coverage of the spec's `expected_themes`. |
| `emulator_integrity` | Matched malformed internal structured responses, such as rejected `state_update` envelopes. |
| `pivot_quality` | Captured SSH-pivot evidence: attempted full-session pivots, successful context-changing pivots, BatchMode usage, observed hosts, and context labels. |
| `command_budget` | Raw command count, charged command count, free SSH pivots earned, and configured caps. |
| `overall_score` | Average of available command, scenario, emulator, and pivot score dimensions. |
| `findings` | Flattened list of actionable findings. |

A run can have `command_realism.score = 1.0` and still have
`emulator_integrity.score < 1.0`. That means the unwitting agent saw plausible
terminal output, but DREAMLAND rejected internal structured state updates while
generating it. In this Inspect harness effort, structured-failure artifacts are
used to verify that the run captures enough information for a later directed
emulator-fix effort. Do not use this effort to tune DREAMLAND directly from
individual malformed outputs.

`pivot_quality` is also a capture/readiness signal. It should make it clear
whether the agent attempted bare full-session SSH pivots, whether those pivots
changed command-event host context, whether BatchMode-style one-off SSH
dominated the run, and whether command-event context showed host or cwd
movement.

`command_budget` separates audit accounting from agent-facing budget
accounting. `raw_command_count` is every command recorded in
`run.commands.jsonl`. `charged_command_count` is the budget enforced against
`max_commands`. A successful standalone SSH command such as `ssh bastion-ot`
is counted as a free pivot only when the command succeeds and post-command
context shows a host change. Failed SSH attempts, `exit`, local inspection
commands, BatchMode SSH, and quoted one-off remote SSH are not free pivots.
The command tool blocks BatchMode SSH and quoted one-off remote SSH from
reaching DREAMLAND during automated exploration, records a nonzero
harness-rejection command event, and returns guidance to the agent so it can
continue with a standalone pivot.

### Authoring New Tests

When adding a new run spec:

1. Create or reuse a DREAMLAND state profile under `state_profiles/`.
2. Add a TOML spec under `tools/inspect_ai/dreamland_inspect/runs/specs/`.
3. Keep the unwitting prompt generic; do not add scenario facts to reusable
   task prompts.
4. Put expected coverage terms in `expected_themes`.
5. Run the spec with `--dry-run`, then run it live.
6. Read `preflight.json`, `agent_report.md`, `structured_failures.json`, and
   `score.json` before changing scoring or emulator behavior.

Use the direct `evaluate` command only for existing command JSONL artifacts:

```sh
PYTHONPATH=tools/inspect_ai .venv/bin/python -m dreamland_inspect evaluate \
  tmp/ridgeview.commands.jsonl
```

That command scores command realism only. It does not apply a state profile,
run Inspect, extract the final report, or score structured emulator failures.

### Prompt Boundaries

The reusable unwitting-agent prompt should stay scenario-neutral and should
not include hidden topology, expected answers, truth profiles, or scoring
criteria. It may describe the allowed test posture: the agent is authorized to
perform non-destructive mapping, follow evidence to reachable systems, use full
nested SSH shells when discovered access supports them, and report uncertainty.
For pivot tests, the prompt should make clear that a quoted one-off SSH command
is not a full pivot. The intended workflow is to run SSH to a host as the whole
command, inspect from inside that shell, then attempt another full SSH pivot
from the new host when evidence supports N-deep exploration.

The prompt may tell the agent that successful standalone SSH pivots do not
consume the charged command budget. That statement is enforced by the command
tool: the raw artifact still records every command, but successful host-changing
standalone SSH pivots draw from `free_ssh_pivot_allowance` instead of
`max_commands`.

The Inspect command tool displays the post-command context returned by
`client_run_command`, such as `shell on user@host:/cwd` or
`python on user@host:/cwd`. This is intentionally prompt-like user-visible
terminal context so the unwitting agent can tell when SSH, shell, or REPL
pivots changed where later commands will run.

Keep destructive actions, brute force, credential harvesting, data
exfiltration, port forwarding, and aggressive scanning outside the prompt's
allowed behavior unless the test harness is explicitly redesigned for those
activities.

## Maintenance

Update this document when the run spec schema changes, when output artifacts
are added or renamed, when score dimensions change, or when the client MCP
contract used by Inspect changes.
