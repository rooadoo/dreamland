# Inspect Run Contracts

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: record why Inspect AI evaluations use trusted run contracts and
generic unwitting-agent prompts.

## Overview

Automated DREAMLAND evaluations need to distinguish command-output realism from
run validity. A command artifact can score well while still being invalid for a
scenario if it mixes sessions, reuses stale command JSONL output, or runs
against a different state profile than the test intended.

Use trusted run specs and per-run artifact directories to bind together the
expected DREAMLAND state profile, preflight scenario application, Inspect
invocation, command JSONL artifact, and post-run scoring. Keep the unwitting
agent prompt scenario-generic so it does not receive privileged assumptions
about the environment.

## Details

The Inspect harness owns a run contract before the unwitting agent starts. The
contract records the run id, spec path, expected state profile, model, command
budget, timeout, command artifact path, and Inspect log directory.

When requested by the run spec, the harness applies the named state profile as
DREAMLAND's process-local default before launching Inspect. This keeps the test
setup with the trusted harness and avoids scenario facts in the unwitting agent
prompt. The created client session is still checked against the expected
profile before commands are run.

Each run gets a fresh output directory. The command recorder resets the command
JSONL artifact at run start so repeated spec names cannot silently append stale
events.

The bounded `/client-mcp` surface remains the only tool surface exposed to the
unwitting Inspect agent. The generic client create tool reports the created
session's state-profile label to the trusted harness so the harness can fail
fast when the applied DREAMLAND profile does not match the run spec. Command
execution results remain limited to user-visible command output fields.

Scoring treats artifact integrity as a prerequisite for interpreting realism.
Duplicate command ids, multiple session ids, multiple state profiles, and
expected-profile mismatches are findings even if individual command outputs
look plausible.

The run helper also extracts Inspect postflight metadata and the unwitting
agent's final report into normal files in the run directory. If the run spec
declares `expected_themes`, the helper records deterministic theme coverage in
`score.json`. Theme coverage is reported separately from command realism so it
can guide scenario tuning without hiding low-level artifact failures.

The run helper also matches DREAMLAND structured-failure capsules to the run by
client session id and command text. These failures are written to
`structured_failures.json` and scored under `emulator_integrity`. This keeps
internal malformed `state_update` envelopes visible to trusted maintainers
without exposing those failures to the unwitting agent during exploration.

Scenario conformance should be evaluated from trusted spec metadata, post-run
artifacts, and operator-only inspection data. Scenario facts should not be
placed in reusable behavior prompts or in the unwitting agent objective.

## Maintenance

Update this record when the run spec schema changes, when the client MCP
surface exposes additional harness-only validation fields, or when scenario or
emulator-integrity scoring becomes a separate durable scorer.
