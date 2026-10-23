# SSH Lobby Presentation Phase 3 Checkpoint

Status: Active
Audience: maintainers, operators, human terminal testers
Purpose: identify the accepted centered two-second introduction and its reproducible timing, input, and human acceptance checks.

## Overview

Phase 3 adds a multicolor horizontal wordmark reveal before the first welcome
on an admitted SSH lobby connection. It settles into the accepted static banner and
welcome, and does not repeat during pagination, refresh, or world visits.
The operator accepted Phase 2 on 2026-09-11 with "fab. commit and continue";
that phase is commit `d9ef6f7295109c973d6d80a3e49d0baf967a61a0` on
`feature/ssh-lobby-presentation`, the base of this checkpoint.

The operator's latest review requested two seconds, larger artwork, centering,
multicolor horizontal movement, and clearing before and after the effect.
This revision replaces the initial one-second, single-line sweep. Evidence
from that earlier design does not establish results for the revised artwork.

The operator accepted this revision on 2026-09-11 with "perfect, commit and
continue". The planned default is now `auto` in runtime configuration and the
preview, with explicit `off` overrides retained. Individual client/version
details remain unverified. On 2026-09-11 the operator abandoned Phase 4 and
requested restoration of this accepted Phase 3, followed by merge to `main`
and push. The release scope ends at Phase 3.
No operator notice files, working INI, live service, or model behavior changed.

## Details

### Appearance, timing, and controls

A 57-column, nine-row ASCII DREAMLAND wordmark with connected network rails
reveals from left to right, with cyan, magenta, and green accents. It is centered horizontally
and vertically in the reported viewport, capped at 240 columns and 100 rows.
The artwork requires at least one column and row of margin on each side, so
motion is available at 59x11 or larger.

The final revealed artwork, shown here without color or terminal positioning:

```text
o---o-----o-----o-----o-----o-----o-----o-----o-----o---o
  ####  ####  #####  ###  #   # #      ###  #   # ####
  #   # #   # #     #   # ## ## #     #   # ##  # #   #
  #   # #   # #     #   # # # # #     #   # ##  # #   #
  #   # ####  ####  ##### # # # #     ##### # # # #   #
  #   # # #   #     #   # #   # #     #   # #  ## #   #
  #   # #  #  #     #   # #   # #     #   # #  ## #   #
  ####  #   # ##### #   # #   # ##### #   # #   # ####
o---o-----o-----o-----o-----o-----o-----o-----o-----o---o
```

There are twenty scheduled frames over 2,000 ms, with a hard 16 KiB output
budget including cleanup. The visible display clears and the cursor homes
before the first frame. Numeric cursor positions place every art row without
frame newlines; each frame restores the default color. Completion clears and
homes again before the accepted static welcome renders. Skip and resize use
the same full-display cleanup. There is no progress meter, boot claim, model
call, hidden cursor, or background writer.

Slow writes cause obsolete frames to be dropped. Server scheduling does not
promise exactly two seconds of client-observed time over a delayed connection.
Receive waits are bounded by 100 ms, the next frame deadline, and the input
idle deadline. Output and empty input drains do not count as activity. Returning
from a world explicitly resumes a fresh lobby idle period.

| Setting or condition | Result |
| --- | --- |
| `[lobby] intro_animation=auto` | Enable on recognized clear/motion terminals at 59x11 or larger, with display clearing enabled; accepted default |
| `[lobby] intro_animation=off` | Immediate static welcome; explicit override |
| `DREAMLAND_LOBBY_INTRO_ANIMATION` | Higher-precedence `auto`/`off`, normalized for case/whitespace; blank/invalid values fail |
| Missing, malformed, unknown, or `dumb` TERM | Immediate plain static welcome |
| Fewer than 59 columns or 11 rows | Immediate static welcome at the real viewport size |
| `color=off` | Monochrome effect, when motion is explicitly enabled |
| `clear_screen=off` | Skip introduction; full-display clearing remains disabled |
| `banner=simple` | Same optional introduction, then the simple static heading |
| All color, clearing, and motion controls off | Plain linear presentation |

The recognized TERM list remains in [SSH Lobby](SSH_LOBBY.md#terminal-presentation).
Motion uses visible-display erase plus cursor home (`ESC[2J` and `ESC[H`),
numeric cursor position (`ESC[row;columnH`), and the existing basic foreground
colors and reset. It never sends a terminal query, scrollback erase,
alternate-buffer switch, or cursor-hide sequence. The final welcome uses the
accepted static renderer and its configured banner choice.

Q, Ctrl-C, and Ctrl-D cancel the connection during motion. Any other key skips
the effect; the welcome then waits for a new continuation key. Buffered paste
including `i agree`, selections, and commands is discarded at the motion
boundary. Known partial escape/UTF-8 sequences and CR/LF tails remain filtered.
Raw SSH cannot distinguish arbitrarily delayed paste from newly typed input.
Cancellation already available when a resize arrives takes precedence over
resize cleanup. No session is created until the normal agreement and valid
world selection complete.

A resize stops motion, clears the display, and renders the static welcome at
the new dimensions. Full-display cleanup removes any art rows reflowed during
a shrink. With clearing disabled, motion is skipped entirely.

### Isolated launch and connection

From the repository root, run:

```sh
env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B \
  tools/ssh_lobby_presentation_preview.py \
  --welcome-path lobby/welcome.txt \
  --guidelines-path lobby/guidelines.txt \
  --contact-path lobby/contact.txt \
  --color auto --clear-screen auto --banner auto --intro-animation auto
```

The provider-free preview prints its loopback port, host-key fingerprint,
exact SSH command, and generated instance password. Run the printed SSH command
in a real terminal and use that password. It requests a PTY; use the client's
normal TERM. Keep credentials local. The preview creates two disposable worlds
and temporary runtime state, without loading live provider credentials or the
working `dreamland.ini`. Use `whoami` and `exit` for deterministic world checks.
Type `shutdown` in the preview operator console or press Ctrl-C to stop it and
remove its temporary state.

Start with 80x24. Repeat with `--intro-animation off`, `--color off`,
`--clear-screen off`, and `--banner simple`. For explicit plain mode, disable
motion, color, and clearing together. For fallback coverage, connect with
`TERM=dumb` or a genuinely narrow/short client viewport. Record actual dimensions
rather than claiming a client can achieve a size it does not support.

### Human checklist

1. Reconnect and watch the approximately two-second reveal. Confirm the display
   clears before the first frame, the nine-row art is centered, and the
   horizontal reveal has readable cyan, magenta, and green accents. Confirm the
   display clears again into a stable welcome with readable copy and actions.
2. In separate connections, press a key, Q, Ctrl-C, and Ctrl-D during motion.
   A normal key skips; it must take another key to advance the welcome. The
   cancellation keys disconnect immediately without showing later prompts.
3. Paste `i agree`, a selection, and a command during the effect. Confirm the
   welcome still waits and no world starts. Then complete the normal flow.
   Check partial escape/CRLF tails separately from arbitrarily delayed input.
4. Resize during motion, including shrinking below the artwork's required size.
   Check that the full display clears and static text uses the new dimensions,
   with no remaining art fragments. Repeat with display clearing disabled to
   confirm motion is skipped. On a slower connection, stale frames should not replay.
5. Visit both worlds, run `whoami`, `exit`, and refresh. Check distinct public
   IDs, retained last-ended ID, clean shell styling, and no repeated motion.
   Reconnect to see the introduction again.
6. Repeat in off/plain/small modes and across the client matrix below.

### Phase 4 closeout

On 2026-09-11 the operator chose to abandon menu transition animation after
review and retain the accepted Phase 3 presentation. All uncommitted Phase 4
implementation, configuration, tests, and checkpoint material were removed.
The runtime, tests, and preview are restored to
`0baec8b21c09df031287e975e93f9c32ebe17031`.

The two-second introduction remains enabled by default and runs once per
connection. Menu changes remain immediate. No transition code or setting is
included, and no Phase 4 checkpoint remains pending.

### Evidence and acceptance

| Evidence | Result |
| --- | --- |
| Branch / source base | `feature/ssh-lobby-presentation` / `d9ef6f7295109c973d6d80a3e49d0baf967a61a0` |
| Reviewed pending-diff fingerprint | `cd6268d35482b5bf73526f929fa67d2d8c17c4f89001ad2e3c50a705aa7eca4d` |
| Notice SHA-256 | `1ee4b799076a4441ac0fa498c3d0d11b0f025bad499ba185811d3cc32acccef6` without optional tracing disclosure |
| Effective preview settings | Color/clearing/banner/introduction `auto`; runtime and preview now default to the reviewed `auto` introduction |
| Automated checks | 262 tests passed; Ruff lint/format, scoped typing, and diff checks passed |
| Real SSH preview rehearsal | Passed with the revised provider-free CLI and Paramiko on 2026-09-11 |
| Human acceptance | Accepted 2026-09-11: "perfect, commit and continue" |
| Phase 3 commit | `0baec8b21c09df031287e975e93f9c32ebe17031` |
| Phase 4 | Abandoned by the operator on 2026-09-11; uncommitted changes removed and Phase 3 restored |

After Phase 4 removal on 2026-09-11, the combined regression groups below
passed all 262 tests again in 65.653 seconds. The restored runtime,
configuration, tests, and preview match the accepted Phase 3 commit byte for
byte; transition functions and settings are absent. This verifies the restored
release scope without treating the abandoned experiment's results as release
evidence.

`test_lobby_intro` uses fake time for exact duration/frame/byte bounds, centered
artwork and horizontal progression, allowed controls, clear-before/after and
cursor cleanup, slow writes, skip/paste/partial tails, cancellation,
resize, shutdown/disconnect, idle expiry, failures, and budget exhaustion.
`test_lobby_input` owns deadlines, restored receive timeouts, activity accounting,
finite drains, and explicit idle resumption. `test_ssh_lobby_intro` exercises
the real controller, including a 600-second fake-clock world visit followed
by a usable picker with a five-second lobby timeout. Existing plain/styled
layout, content, banner, audit, visit, and SSH checks remain regression coverage.

On 2026-09-11 the revised implementation passed all 228 owning/shared-path
checks in 25.949 seconds:

```sh
env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest \
  tests.test_lobby_config tests.test_config tests.test_lobby_presentation \
  tests.test_lobby_intro tests.test_ssh_lobby tests.test_ssh_lobby_intro \
  tests.test_ssh_lobby_presentation tests.test_ssh_lobby_presentation_review \
  tests.test_ssh_lobby_presentation_preview tests.test_lobby_input \
  tests.test_lobby_content tests.test_ssh_lobby_audit tests.test_ssh_lobby_banner \
  tests.test_ssh_lobby_sessions tests.test_ssh_sessions tests.test_ssh_streaming \
  tests.test_ssh_session_state
```

The separate actual SSH transport/server regression run passed all 34 tests
in 43.294 seconds, including existing direct-mode admission behavior:

```sh
env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest \
  tests.test_ssh_lobby_transport tests.test_ssh_server
```

Ruff lint and formatting passed for all eleven changed Python files, as did
`git diff --check` and scoped typing for the five runtime modules:

```sh
.venv/bin/mypy --follow-imports=skip src/dreamland/config.py \
  src/dreamland/lobby_input.py src/dreamland/lobby_intro.py \
  src/dreamland/lobby_intro_art.py src/dreamland/ssh_lobby.py
```

After operator acceptance, the planned `auto` default was promoted in runtime
configuration and the preview. Existing layout and transport fixtures explicitly
select `off`; dedicated introduction tests retain motion coverage. On
2026-09-11, the combined groups above passed all 262 tests in 60.860 seconds
with those changes. Ruff lint/format passed for all fourteen changed Python
files, scoped typing passed for five runtime modules, and the diff check passed.
The reviewed artwork, timing, and input behavior were unchanged by promotion.

Retain the ready cancellation poll before resize cleanup, so already-received
quit input wins when both arrive together. Check all four quit keys separately.

Scoped typing must not be reported as a full transitive pass: the
[Phase 1 record](SSH_LOBBY_PRESENTATION_CHECKPOINT.md#evidence-and-acceptance)
documents the installed NumPy stub syntax limitation relative to Python 3.11.

The actual preview CLI passed its revised Paramiko rehearsal using the current
operator paths and all four presentation controls set to `auto`:

```sh
env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B \
  tmp/rehearse_ssh_lobby_presentation_phase3_reveal.py
```

At 80x24 the effect sent twenty frames and 10,215 bytes, with its top-left
corner at row 8, column 12. Server scheduling measured 2,000 ms and client
observation 2,004 ms. At 120x40 it sent twenty frames and 10,255 bytes, centered
at row 16, column 32; server/client times were 2,000/2,003 ms. Every artwork row
and horizontal reveal prefix, the final three color bands, and both clear/reset
boundaries were verified against actual channel output.

Paste/partial-escape and CRLF skipping preserved the welcome input boundary;
Q, uppercase Q, Ctrl-C, and Ctrl-D cancelled within 42 ms, with cleanup and no
unwanted welcome or new session. Resizing to 40x10 stopped after one frame
and the static output fit in nine rows. Dumb, unknown, narrow/short, off,
clearing-off, and all-off cases skipped motion; clearing-off emitted no display
clear. Two explicitly selected visits completed `whoami` and `exit` with
normal shell colors, distinct ended IDs retained through refresh, and no intro
replay. Four disposable CLI instances shut down and removed their temporary
state. No passwords or transcripts were retained.

The first rehearsal launch hit the sandbox's loopback socket restriction;
the authorized rerun passed. Automated transport/output results are not human
terminal rendering evidence. The earlier single-line implementation's evidence
is superseded for this checkpoint.

The reviewed pending-diff fingerprint was verified before acceptance records,
the planned default promotion, and matching fixture updates. It hashes the source base and sorted changed/new
file manifest (path, Git mode, content SHA-256). Only this document's
fingerprint cell is normalized to `<fingerprint>` to avoid self-reference.
The local script and manifest are
`tmp/fingerprint_ssh_lobby_presentation_phase3.py` and
`tmp/SSH_LOBBY_PRESENTATION_PHASE3_FINGERPRINT.json`; neither is committed.

| Human client | Result | Actual settings and notes |
| --- | --- | --- |
| PuTTY on Windows | Pending | Version, TERM, dimensions, ANSI settings, theme |
| macOS Terminal with OpenSSH | Pending | Terminal/macOS versions, TERM, profile, dimensions |
| GNOME Terminal or another VTE terminal | Pending | Terminal/VTE versions, TERM, dimensions, theme |
| Konsole or xterm | Pending | Terminal/version, TERM, dimensions, theme |
| tmux or GNU screen | Pending | Multiplexer/version, outer terminal, TERM, dimensions |
| Plain/legacy/unknown terminal walkthrough | Pending | Actual TERM/settings and readable immediate fallback |

Automated protocol and timing results are not human client rendering evidence.
Individual Phase 1/2/3 client/version details were not supplied and remain
unverified despite explicit operator acceptance of those implementations.
Record the actual tester, settings, phase, and result for any human checks.

## Maintenance

Record any additional human client results. The operator acceptance satisfies
this phase's commit gate; unavailable client results remain unverified. Phase 4
was abandoned, and the completed presentation scope ends at the accepted Phase 3.
Keep [SSH Lobby](SSH_LOBBY.md), [Testing](TESTING.md), the example configuration,
and [decision 0096](decisions/0096-ssh-lobby-presentation.md) aligned.
