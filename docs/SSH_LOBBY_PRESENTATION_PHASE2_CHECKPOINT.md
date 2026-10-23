# SSH Lobby Presentation Phase 2 Checkpoint

Status: Active
Audience: maintainers, operators, human terminal testers
Purpose: identify the static welcome banner and continuation wording under review, with reproducible fit and human acceptance checks.

## Overview

Phase 2 adds a larger ASCII DREAMLAND wordmark to the initial welcome when the
entire welcome and its action fit. It also implements the operator's requested
continuation wording: `Press any key to continue, or Q to quit.` The input
grammar, agreement requirement, notice prose, and visit lifecycle are unchanged.

The implementation is on `feature/ssh-lobby-presentation`, based on accepted
Phase 1 commit `4e29ff567bd7df56450cf05f9032c638a4e6f657`. On 2026-09-11 the
operator accepted Phase 1 with "Much cleaner I like it" and directed "Commit
and continue" into Phase 2. Individual client details were not supplied; the
[Phase 1 record](SSH_LOBBY_PRESENTATION_CHECKPOINT.md) preserves those gaps.
On 2026-09-11 the operator accepted Phase 2 with "fab. commit and continue".
That acceptance authorizes this phase commit and Phase 3 work. Individual
client/version details remain unverified. No animation is included here.

## Details

### Static design and copy contract

The wordmark is 47 columns by three rows, with connected nodes on the left:

```text
o---o   _   _   __                           _
|      | \ |_) |_   /\  |\/| |     /\  |\ | | \
o---o  |_/ | \ |__ /--\ |  | |___ /--\ | \| |_/
```

[`WELCOME_BANNER`](../src/dreamland/lobby_presentation.py) is the canonical
fixed-line artwork, including a final space in its first row. Cyan uses the
accepted heading color role; body text retains the terminal's default colors.
A compatible clearing-only terminal can display the artwork without color.
Review the [large-banner projection](../lobby/preview-banner.txt) and
[simple projection](../lobby/preview.txt) for the current operator copy.
These saved projections contain no escape sequences and do not establish
client color, clearing, or human rendering results.

The full art appears only on the initial welcome, and only when all wrapped
welcome prose, paragraph gaps, artwork, margins, leading row, and action/footer
fit together. It never wraps, truncates, splits, or adds welcome pages.
Continuation pages, guidelines, and the world picker retain compact branding.
Widening the window on a continuation page does not introduce the initial art.

`banner=auto` is the default; `banner=simple` forces the text heading. Normal
deployment accepts `[lobby] banner` and the higher-precedence
`DREAMLAND_LOBBY_BANNER` override. Both values are case/whitespace normalized;
blank and unsupported settings fail explicitly. The preview accepts
`--banner auto` and `--banner simple`. Unsupported TERM, color and clearing both
disabled, and minimal layouts below 20x5 all use simple branding. Color-only
and clearing-only compatible modes can show full art when it fits.

An exact leading standalone `DREAMLAND` paragraph in a welcome is its existing
wordmark and appears once through the large/simple heading. All other text and
indentation remain notice content. The editable and packaged notice files are
unchanged. Compact continuation instructions use two lines:

```text
Any key: continue
Q: quit
```

Welcome and paging still accept a key immediately. Agreement and world
selection retain their Enter instructions and editable prompt.

### Launch the isolated preview

From the repository root, run:

```sh
env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/ssh_lobby_presentation_preview.py \
  --welcome-path lobby/welcome.txt \
  --guidelines-path lobby/guidelines.txt \
  --contact-path lobby/contact.txt \
  --color auto --clear-screen auto --banner auto
```

These paths select the current operator notice files. The preview does not
load `dreamland.ini` or its environment overrides; adjust paths explicitly if
the operator configuration has changed. Each connection snapshots the current
copy, so reconnect after edits. Record the startup notice SHA-256 and printed
presentation settings for the tested copy.

Use the exact connection command and disposable password printed at startup,
and verify the host-key prompt against the printed fingerprint. The listener
binds only `127.0.0.1` on an automatically selected port. The
[Phase 1 launch details](SSH_LOBBY_PRESENTATION_CHECKPOINT.md#controlled-local-test-instance)
explain PuTTY, loopback forwarding from another machine, temporary state, and
the supported deterministic `whoami`, `pwd`, and `exit` shell checks. The
preview uses the actual SSH/lobby code with isolated disposable worlds and no
provider, enrichment, or tracing clients. `shutdown`, Ctrl-C, or EOF in the
operator terminal stops it and removes its temporary state.

Add `--langfuse-notice` to display the optional sentence as a copy fixture;
tracing remains disabled. Add `--log-level DEBUG` to inspect the chosen
large/simple banner and viewport in the operator terminal. These are local
test events, not deployed attendee records.

### Fit boundaries and custom copy

The fixed 47-column artwork requires 51 reported terminal columns because the
renderer reserves two-column margins on both sides. Height depends on notice
wrapping. For the current `lobby/welcome.txt`, an 80x21 viewport fits the full
welcome and art; at 80x20 it uses the simple heading and still shows the entire
welcome on one page. Reconnect at each size to test the initial welcome. If
the copy changes, record its hash and find the new height threshold.

For stable synthetic edges, create separate disposable notice fixtures without
editing the operator files:

```sh
.venv/bin/python -c 'from pathlib import Path; root = Path("tmp"); root.mkdir(exist_ok=True); (root / "lobby-banner-short.txt").write_text("Custom welcome.\n", encoding="utf-8"); (root / "lobby-banner-nine-lines.txt").write_text("\n".join(f"Notice line {n}." for n in range(1, 10)) + "\n", encoding="utf-8"); (root / "lobby-banner-long.txt").write_text("\n".join(f"Notice line {n:03d}." for n in range(1, 41)) + "\n", encoding="utf-8")'
```

Restart the same preview command with its `--welcome-path` replaced by the
fixture path, then reconnect at each listed size:

| Welcome fixture | Size | Expected initial welcome |
| --- | --- | --- |
| `tmp/lobby-banner-short.txt` | 51x24 | Full intact art, short notice, visible action |
| `tmp/lobby-banner-short.txt` | 50x24 | Simple heading, same notice and action |
| `tmp/lobby-banner-nine-lines.txt` | 80x15 | Full art and all nine lines fit exactly with leading row and footer |
| `tmp/lobby-banner-nine-lines.txt` | 80x14 | Simple heading and all nine lines on one page |
| `tmp/lobby-banner-long.txt` | 80x12 | Simple heading and normal paging; each of the 40 lines appears once |

Repeat the long case with `--banner simple` and compare page counts. Auto must
not create additional pages for artwork. Check at a practical 20x5 size and
with enlarged fonts or zoom; record terminal minimum-size limits. Restore the
original operator paths for the final walkthrough.

### Human Phase 2 checklist

For every tested client, record application/version, OS, negotiated TERM,
viewport, font/zoom, theme, ANSI-color settings, and any multiplexer or tunnel.

1. At 80x24 and a wide size, review the actual DREAMLAND lettering, connected
   nodes, spacing, and cyan palette in light and dark themes. Confirm the full
   current welcome and its continuation action fit together.
2. Test the current-copy and synthetic boundaries above. Shrink just below the
   fit dimensions and confirm a clean simple heading, readable copy, and
   visible controls. Confirm the art never splits or gains its own page.
3. Continue through every guidelines page, enter `i agree`, select a world,
   run `whoami` and `pwd`, and `exit`. Confirm the large wordmark is confined
   to the initial welcome, the new continuation sentence is clear, agreement
   and selection still explain Enter, and the last ended ID remains copyable.
4. Restart with `--banner simple`. Then test `--color off --clear-screen auto`,
   `--color auto --clear-screen off`, and both off. Reconnect using `dumb` and
   unknown TERM. Confirm compatible individual effects can retain the artwork,
   and plain/minimal modes display a simple heading without escape text.
5. Reconnect with current operator copy, including the optional disclosure
   fixture. Compare the complete prose and contact information. For long
   welcome copy, resize on a continuation boundary and verify remaining text
   survives and the large art does not appear later. Quit immediately with
   `Q` on a continuation page and check normal terminal styling afterward.

### Evidence and acceptance

| Evidence | Result |
| --- | --- |
| Branch / source base | `feature/ssh-lobby-presentation` / `4e29ff567bd7df56450cf05f9032c638a4e6f657` |
| Reviewed pending-diff fingerprint | `d9fbd027676c77ff959ed300ef4b0526cd5b13d6aa72caf04a04189997911895` |
| Notice SHA-256 and presentation settings | `1ee4b799076a4441ac0fa498c3d0d11b0f025bad499ba185811d3cc32acccef6`; color/clearing `auto`, banner `auto` plus forced `simple` |
| Automated owning and regression checks | 152 tests passed; Ruff lint/format, diff check, and scoped typing passed |
| Automated preview/SSH rehearsal | Passed on 2026-09-11; actual preview CLI with Paramiko at 80x24 |
| Human acceptance | Accepted 2026-09-11: "fab. commit and continue" |
| Phase 2 commit | `d9ef6f7295109c973d6d80a3e49d0baf967a61a0` |

On 2026-09-11, the integrated run passed all 152 tests in 9.129 seconds:

```sh
env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest \
  tests.test_lobby_config tests.test_config tests.test_lobby_presentation \
  tests.test_ssh_lobby tests.test_ssh_lobby_presentation \
  tests.test_ssh_lobby_presentation_review tests.test_ssh_lobby_presentation_preview \
  tests.test_lobby_input tests.test_lobby_content tests.test_ssh_lobby_transport \
  tests.test_ssh_lobby_audit tests.test_ssh_lobby_banner
```

This includes all 18 dedicated banner checks. An initial sandboxed run could
not open the loopback sockets for four SSH transport checks; the integrated
rerun with socket permission passed. No runtime change was needed for that
environment restriction. Ruff lint and formatting passed for the nine changed
Python files, and `git diff --check` passed. Scoped typing passed for all three
changed runtime modules:

```sh
.venv/bin/mypy --follow-imports=skip src/dreamland/config.py \
  src/dreamland/lobby_presentation.py src/dreamland/ssh_lobby.py
```

The Phase 1 record documents an
existing [transitive type-check limitation](SSH_LOBBY_PRESENTATION_CHECKPOINT.md#evidence-and-acceptance);
a scoped check must not be reported as a full transitive pass.

The actual preview CLI also passed an automated Paramiko rehearsal at 80x24
using the current operator paths above. With `xterm-256color` and `banner=auto`,
all three artwork rows appeared intact once alongside the full welcome prose
and new continuation sentence. Every guidelines paragraph and contact appeared
before agreement; the artwork did not repeat during guidelines, a deterministic
`whoami`/`exit` visit, the returned picker, or refresh. The ended visit's public
ID remained visible. Separate `dumb` and forced-simple `xterm-256color`
connections each displayed one simple heading and no artwork; `dumb` emitted
no presentation escapes. `Q` quit without Enter at the continuation prompt.
All three CLI instances shut down normally and their temporary state was
verified removed. No credentials or transcripts were retained. This is
automated output/transport evidence, not a human rendering result.

Both checked-in projections were generated from the current operator notice
files and catalog with the optional Langfuse disclosure. They contain four
pages: welcome, two guidelines pages, and the picker. Their notice SHA-256 is
`ca8fad4b8b840e2439ba9e6a0600d2e6bf5606e122cc1f55a25ea33d404e65ac`.
The source notice and catalog files remain unchanged.

The reviewed pending-diff fingerprint, verified before acceptance-only documentation edits, hashes the base revision and a sorted manifest
of every changed/new file's path, Git file mode, and content SHA-256. Only this
document's fingerprint table value is normalized to `<fingerprint>` to avoid a
self-reference. Its local manifest and regeneration script are
`tmp/SSH_LOBBY_PRESENTATION_PHASE2_FINGERPRINT.json` and
`tmp/fingerprint_ssh_lobby_presentation_phase2.py`; these local review artifacts
are not staged or included in the pending diff.

| Human client | Result | Actual settings and notes |
| --- | --- | --- |
| PuTTY on Windows | Pending | Version, terminal type, ANSI settings, theme, font, dimensions |
| macOS Terminal with OpenSSH | Pending | Terminal/macOS versions, profile/ANSI settings, theme, dimensions |
| GNOME Terminal or another VTE terminal | Pending | Terminal/VTE versions, TERM, theme, dimensions |
| Konsole or xterm | Pending | Terminal/version, TERM, theme, dimensions |
| tmux or GNU screen | Pending | Multiplexer/version, outer terminal, TERM, dimensions |
| Plain/legacy/unknown terminal walkthrough | Pending | TERM/settings, simple fallback, readable actions |

Automated layout and protocol checks do not substitute for human rendering
results. Individual client results remain pending; the explicit operator
acceptance authorizes committing this reviewed implementation and continuing.

## Maintenance

Update this checkpoint with the exact reviewed diff, copy hash, automated
evidence, human settings/results, acceptance, and eventual implementation commit.
Keep it aligned with [SSH Lobby](SSH_LOBBY.md),
[decision 0096](decisions/0096-ssh-lobby-presentation.md), the canonical artwork,
preview tool, and renderer fit rules. Introduction animation follows this
accepted checkpoint with its own human gate.
