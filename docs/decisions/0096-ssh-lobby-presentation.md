# 0096 SSH Lobby Presentation

Status: Active
Audience: maintainers, operators, human terminal testers
Purpose: record terminal compatibility, rendering ownership, retained feedback, static banner fit, bounded motion, and phased human acceptance.

## Overview

The native SSH lobby needs visually distinct pages while preserving readable
notice text, explicit agreement, and copyable simulation references. This
extends the presentation described in
[decision 0093](0093-native-ssh-lobby-visits.md) within the native backend lane
and the [project model](../PROJECT_MODEL.md). Phase 1 introduces visible-screen
clearing and basic foreground accents. The
[presentation checkpoint](../SSH_LOBBY_PRESENTATION_CHECKPOINT.md) identifies
the reviewed implementation and human testing. On 2026-09-11 the operator
accepted Phase 1 and explicitly authorized its commit and Phase 2 work with
"Commit and continue". Phase 1 is commit
`4e29ff567bd7df56450cf05f9032c638a4e6f657`.
Individual client/version results were not supplied and remain unverified.
Phase 2 adds the static initial welcome wordmark and clearer continuation
instructions; its [checkpoint](../SSH_LOBBY_PRESENTATION_PHASE2_CHECKPOINT.md)
records operator acceptance on 2026-09-11: "fab. commit and continue".

## Details

### Client capabilities and operator control

The SSH PTY request supplies a bounded, normalized ASCII TERM hint. An exact
allowlist in
[`lobby_presentation.py`](../../src/dreamland/lobby_presentation.py) recognizes
specific xterm, PuTTY, rxvt, Linux console, GNU screen, and tmux names.
`vt100`, `vt102`, `vt220`, and `xterm-mono` retain clearing without color.
Missing, malformed, unknown, or `dumb` TERM selects plain output. Arbitrary
family prefixes grant no capabilities. The accepted names and 64-character
normalization rules are documented in [SSH Lobby](../SSH_LOBBY.md#terminal-presentation).

Color, clearing, and motion remain separate capabilities so a legacy
monochrome terminal does not receive color merely because it can clear.
Phase 1 uses only color and clearing. `[lobby] color` and `clear_screen` each
accept `auto` or `off`, defaulting to `auto`, with independent
`DREAMLAND_LOBBY_COLOR` and `DREAMLAND_LOBBY_CLEAR_SCREEN` overrides. Blank or
unsupported values fail explicitly. Disabling both produces stable linear
output. Capability selection reads no server-process TERM, trusts no arbitrary
terminal prefix, and issues no terminal queries. SSH environment requests
remain denied; client-local `NO_COLOR` and `COLORTERM` are not forwarded.

### Renderer ownership and display boundaries

The renderer owns a small fixed set of basic ANSI foreground accents and
resets. It assigns roles to headings, frame rules, selection lines, actions,
and public references. Body copy uses the default foreground/background and
is never styled by matching mutable notice wording. Notice/catalog validation
continues rejecting escape and control characters. Layout is calculated from
plain fragments before styling, and each styled fragment ends with a reset.
The lobby restores normal styling before handing off to a world shell and
when a colored connection ends.

Visible-display erase (`ESC[2J`) followed by cursor home (`ESC[H`) occurs before
a complete screen/page render on a compatible client. Guidelines still advance
only on the existing continuation input. Typing, echo, and agreement-prompt
retries do not trigger clears. A submitted picker retry produces a complete
picker with feedback included in its content. This requires no alternate
screen, saved-scrollback erasure, title change, cursor hiding, terminal query,
or background renderer. Client scrollback behavior is not a retention contract.

### Viewport and retained report references

SSH window-change notifications update one synchronized viewport snapshot.
Positive dimensions are bounded to 240 columns and 100 rows; zero leaves the
preceding dimension intact. The controller reads current dimensions at page
and action boundaries, retaining the 80-column reading width. A resize during
typing takes effect after the input boundary, with no redraw beneath an
editable prompt. The pager rewraps remaining content and reserves actual
footer and reference rows before placing body text.

At 20x5 and larger, decoration gives way to content and actions as space
shrinks. The last ended public ID is pinned on every picker page, including
the final action page, until a later started visit supersedes it or the real
connection ends. Its world title and other status remain paged content.
With a retained ID and five to seven rows, the final picker prompt uses
`#:start R:refresh` and `Q:quit; Enter> ` on two lines. This keeps the reference
and action reachable even when the ID wraps across two rows. Below 20 columns
or five rows, an explicit minimal/plain fallback streams the remaining content
at the actual width with no clearing, styling, or artificial paging; it may
scroll because the viewport cannot contain the full action contract.

Invalid selections, stale/unavailable worlds, an empty catalog, pending
cleanup, failed starts, and interrupted visits appear as bounded picker
content so a subsequent clear cannot silently hide their feedback. Exact
Started/Ended marker text remains intact, and repeated ended references do not
depend on scrollback. The existing configurable lifecycle logger records
normalized terminal metadata and selected color/clearing modes once per
connection. No notice-copy or simulation-state change is required.

### Static welcome artwork and continuation wording

Phase 2 stores a 47-column, three-row ASCII DREAMLAND wordmark with a
connected-node motif as fixed lines in `lobby_presentation.py`. Significant
spaces remain part of the artwork. It uses the existing heading color role
and requires no font generator, new terminal controls, or motion capability.
The large art is confined to the initial welcome page; continuation pages,
guidelines, and the picker keep compact branding.

The fit decision includes the entire welcome prose, paragraph gaps, footer,
leading row, and margins. The large art appears only when that complete screen
fits. It cannot wrap, truncate, split, or create additional welcome pagination.
This conservative rule makes operator copy authoritative and avoids an
artwork-only page. At least 51 reported columns are needed for the 47-column
art plus the renderer's two-column margins on each side; height depends on
the actual wrapped copy. Long copy selects simple branding and retains the
existing pager.

`[lobby] banner` accepts `auto` or `simple`, defaulting to `auto`, with the
higher-precedence `DREAMLAND_LOBBY_BANNER` override and preview `--banner` flag.
Configuration normalizes case and surrounding whitespace and rejects invalid
values explicitly. Auto artwork requires either enabled color or enabled
clearing; both disabled, unsupported TERM, and minimal layouts select simple
branding. Thus a compatible monochrome client can display intact uncolored
art without receiving color controls. Debug lifecycle logging records the
large/simple selection and initial viewport.

An exact leading standalone `DREAMLAND` paragraph in the welcome is its
existing wordmark, rendered once by the new large/simple header. Matching is
limited to that position and exact text. Other prose, indentation, and notice
files remain intact; the static design introduces no tagline or disclosure
rewrite.

The operator also requested simpler continuation wording. Welcome and paging
share `Press any key to continue, or Q to quit.` Compact output uses
`Any key: continue` and `Q: quit` on separate lines. This is presentation copy;
the existing any-key, quit, agreement, and selection input grammar is unchanged.

### Bounded connection introduction

Phase 3 uses a synchronous helper in `lobby_intro.py`, invoked once after the
notice snapshot and before the initial welcome. It shares the lobby's single
writer and input owner, so no effect can continue after static input or a world
shell begins. Following review, the operator requested a more ambitious
two-second horizontal reveal, multicolor artwork at least five to ten rows
tall, centering, and clearing before and after. This supersedes the initial
one-second single-line sweep; the accepted Phase 2 static banner is unchanged.
A 57-column, nine-row ASCII wordmark and network rails use twenty scheduled
frames over 2,000 ms of monotonic time; a hard 16 KiB byte budget includes cleanup.
Frame selection follows current time after every write and drops obsolete
frames instead of replaying a backlog. Delivery latency is outside that
server scheduling promise. The effect makes no model call or progress claim.

The effect clears the visible display and homes the cursor before drawing and
on cleanup, including skip and resize. Numeric cursor-position controls place
the nine art rows without frame newlines. The art is centered in the reported
viewport, capped at 240x100, with a required one-row/column margin on every side
(minimum viewport 59x11).
Basic cyan, magenta, and green color bands follow the left-to-right reveal;
`color=off` uses the same monochrome art. The cursor stays visible, color resets
per frame, and no alternate buffer, scrollback erasure, or terminal query is
introduced. Full-display clearing is necessary to remove the larger image and
any rows reflowed during a shrink, so the effect requires `clear_screen=auto`
and recognized clear/motion capabilities. Clearing disabled, small, or
unsupported terminals proceed immediately to the static welcome. Optional
motion retains its `intro_animation=auto|off` setting. The default is `auto`
following operator acceptance on 2026-09-11: "perfect, commit and continue".
Explicit `off` overrides remain available.

`LobbyInput.poll_key` uses absolute monotonic deadlines and receive waits
bounded by 100 ms, remaining frame time, and remaining input idle time. It
retains finite byte limits and partial-key filtering. Q/Ctrl-C/Ctrl-D cancel;
other keys consume the skip and buffered tail before the welcome requests a
new key. Completion and resize also drain a finite received snapshot. Raw SSH
cannot classify arbitrarily delayed paste as distinct from newly typed input.
A resize ends motion; shutdown, disconnect, and inactivity abort promptly.

An empty boundary drain no longer resets input inactivity. This prevents
animation cleanup from extending the idle timeout. Because an active visit
suspends lobby idleness, its return now explicitly calls `resume_idle` before
input cleanup. This preserves the prior full idle period on the returned
picker, including after a long visit. Output never counts as input activity.

### Verification and phased acceptance

Owning configuration/presentation suites cover settings, capability matching,
plain output, allowed controls, and fragment cleanup. Deterministic controller
tests cover styled row/column budgets, full notice paging, boundary resizing,
status and reference retention, tiny prompts, and shell/failure/cancellation
cleanup. Banner tests cover exact spacing, width/height edges, custom copy,
leading-wordmark handling, forced simple/plain modes, initial-page-only art,
and preservation of all prose without art-added pagination.
Actual SSH tests own TERM/window-change dispatch and connection
round trips. The isolated provider-free preview and required human client,
theme, and settings matrix live in the
[presentation checkpoint](../SSH_LOBBY_PRESENTATION_CHECKPOINT.md).

The requested development plan requires automated checks and explicit human
acceptance before each phase is committed. Phase 1 passed that gate through the
operator's explicit acceptance. The operator then accepted Phase 2 with
"fab. commit and continue", authorizing its commit and Phase 3. Individual
client/version results remain unverified. The operator accepted Phase 3 with
"perfect, commit and continue", authorizing its commit and planned `auto`
default. On 2026-09-11 the operator abandoned Phase 4 menu transitions after
review and requested restoration of Phase 3, merge to `main`, and push.
The uncommitted Phase 4 changes were removed. The accepted two-second
introduction remains; menu changes remain immediate, with no transition
configuration or implementation. The completed presentation scope ends at
Phase 3, and no Phase 4 checkpoint remains pending.

## Maintenance

Implementation base: `452af31852dfbb9538f065a6618a221ec1efdc56` on
`feature/ssh-lobby-presentation`. Phase 1 implementation and acceptance commit:
`4e29ff567bd7df56450cf05f9032c638a4e6f657`. Phase 2 builds on that commit and
is commit `d9ef6f7295109c973d6d80a3e49d0baf967a61a0`. Phase 3 builds on that
accepted base and is commit `0baec8b21c09df031287e975e93f9c32ebe17031`.
The Phase 4 closeout restores that accepted implementation; only documentation
records the operator's abandonment and release scope.

Keep [SSH Lobby](../SSH_LOBBY.md), the example INI,
[testing ownership](../TESTING.md), the presentation helper/controller, SSH
metadata handling, and the checkpoint aligned. Update this decision when
terminal capability policy, control ownership, viewport boundaries, retained
references, or the agreed phase gates change.
