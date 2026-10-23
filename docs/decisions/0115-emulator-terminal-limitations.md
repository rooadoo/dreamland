# 0115 Emulator Terminal Limitations

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators
Purpose: keep continuous and screen-oriented commands within the emulator's
supported terminal interaction without exposing implementation limits.

## Overview

The emulator was treating commands such as unbounded `ping`, `top`, `vim`,
`less`, and `more` as new interactive stack frames. The current command/response
interface cannot sustain their indefinite output or screen redraw behavior.
The operator requested finite runs and imaginative, plausible in-world covers.

## Details

The [terminal IO prompt](../../src/dreamland/prompts/terminal_io.md) now directs
continuous commands to finish a short run. Unbounded `ping` defaults to about
ten probes with consistent final statistics; explicit finite stopping
conditions retain their meaning. Other continuous commands need a plausible
completion or failure rather than waiting for user interruption.

Screen-oriented programs finish with complete plain text where appropriate or
a plausible startup failure. Creative covers must preserve established file
content, package availability, permissions, and prior observations. Newly
established facts use the existing world-state persistence rules. Finite
noninteractive modes remain usable on hosts that support them.

[Decision 0122](0122-failure-scope-and-continuity.md) narrows these covers after
a session turned response-size pressure into persistent simulated memory
exhaustion. An interface workaround must not create an ongoing host failure or
disable unrelated commands. The earlier policy does not authorize inventing
host-wide resource exhaustion to shorten output.

The [state contract](../../src/dreamland/prompts/state_contract.md) narrows its
generic push instruction to supported line-oriented interaction. Bounded runs
and deflected screen programs stay in the calling frame. Shells, SSH,
authentication prompts, and line-oriented REPLs keep their existing lifecycle.
The change guides model behavior; it adds no runtime command filter or terminal
renderer, and automated prompt-loading checks cannot prove model adherence.

### Validation and manual checkpoint

On 2026-10-04, all eight tests passed across `tests.test_prompts` and
`tests.test_agent_state`; `git diff --check` also passed. No live model check was
run for this change.

Run the existing prompt-loading and agent-state tests. For a live check, restart
the emulator or construct a fresh command agent so the changed prompt is loaded;
a new session that reuses the startup agent is insufficient. See
[prompt loading](../STATE_PROFILES.md#prompt-modules).

1. Run `ping` against a reachable simulated host without a count, then with
   `-c 3`. Expect roughly ten and exactly three probes respectively, consistent
   statistics, and the calling program and stack depth preserved.
2. Try `vim`, `less`, `more`, and `top`. Expect a coherent failure or complete
   plain-text result, no redraw or pager/editor input, and no new frame. Repeat
   against files and installed tools already observed to check consistency.
3. Try a supported finite mode such as `top -b -n 1`, then enter and exit
   `python3` or a nested shell. The finite command stays in its calling frame;
   the supported interactive context pushes and later pops normally.

## Maintenance

Keep this policy aligned with terminal capabilities and the shell-stack
contract. Revisit the workarounds if DREAMLAND gains screen redraw or persistent
foreground streaming support; retain manual checks of model behavior until
there is corresponding deterministic enforcement.
