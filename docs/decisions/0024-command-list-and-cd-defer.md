# 0024 Command Lists And Cd Defer

Status: Historical
Audience: maintainers, DREAMLAND operators, and coding agents
Purpose: record the temporary boundary between deterministic shell parsing and
model-backed filesystem decisions.

## Overview

The uniform command-list splitting decision was superseded by
[decision 0102](0102-whole-command-routing-and-ordered-effects.md). The
conservative `cd` filesystem authority remains current; mixed/deferred lists
now reach the model as one unchanged line.

Early DREAMLAND built-ins parsed the entire input line as one command argv. That
made `cd portops; ls` reach the local `cd` built-in as if `portops; ls` were
directory arguments, producing `bash: cd: too many arguments` instead of
sequencing the two shell commands.

The same code path also let local `cd` update session state without any
filesystem checks. That was acceptable while `cd` was only a convenience, but it
can establish a current directory that the simulated host has not made real.

## Decision

DREAMLAND now splits simple shell command lists in `AgentBackend` before
built-in dispatch. The supported operators are `;`, `&&`, and `||`, with
quote-aware parsing. Unsupported shell syntax stays on the model path.

`cd` uses a conservative deterministic hint pass before the local built-in
runs. The hint pass accepts obvious baseline directories and paths established
by prior successful `cd`, `pwd`, or `ls -l` style output. It rejects targets
known to be files, missing, or inaccessible from prior output. If the target is
ambiguous, the local built-in declines to handle it and the command agent makes
the scenario-specific decision.

Standard root-only paths such as `/root` are treated as inaccessible for
non-root users in this temporary layer. This prevents local `cd` from accepting
privileged directories before the model can apply Unix permissions.

The filesystem hint helper is intentionally narrow and documented in code as a
temporary bridge. It should be removed or replaced when DREAMLAND gains a
first-class filesystem/state model that can answer existence, type, and
permission checks directly.

## Consequences

Common chained commands such as `cd portops; ls`, `pwd && whoami`, and
`printenv MISSING || whoami` now behave more like an interactive shell. Each
executed segment is recorded as its own history entry so later agent context
sees the actual state transitions and outputs.

The deterministic `cd` path remains incomplete by design. It avoids guessing
for unknown relative paths, which means some `cd` commands that used to update
state locally now go to the model. This keeps the emulator from inventing local
filesystem truth in the wrong layer while preserving deterministic errors for
clear cases.

## Maintenance

Update this record if command-list support expands to pipes, redirects,
subshells, command substitution, or shell grammar beyond simple list operators.
Also update it when `cd` validation moves from history-derived hints to a real
filesystem/state contract.
