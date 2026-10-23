# SSH Lobby Presentation Checkpoint

Status: Active
Audience: maintainers, operators, human terminal testers
Purpose: identify the Phase 1 implementation under review and provide reproducible, isolated human terminal checks.

## Overview

Phase 1 adds capability-gated screen clearing and basic foreground accents,
client TERM/resize handling, and status that remains visible when a lobby page
is redrawn. It retains the existing notice text, agreement, numbered actions,
paragraph paging, and Started/Ended reference markers. The static banner and
continuation wording now have a separate
[Phase 2 checkpoint](SSH_LOBBY_PRESENTATION_PHASE2_CHECKPOINT.md); animation
awaits later work.

Phase 1 was reviewed on `feature/ssh-lobby-presentation`, based on
`452af31852dfbb9538f065a6618a221ec1efdc56`. On 2026-09-11 the operator accepted
the presentation ("Much cleaner I like it") and explicitly directed "Commit and
continue" into Phase 2. Phase 1 was committed as
`4e29ff567bd7df56450cf05f9032c638a4e6f657`. This passes the Phase 1 commit gate.
Client/version details were not supplied; the individual matrix entries below remain
unverified rather than being inferred from that acceptance. The earlier
[lobby rehearsal](SSH_LOBBY_REHEARSAL.md) predates this presentation change and
does not count as its human acceptance.

## Details

### Controlled local test instance

The following launch documents the Phase 1 preview at its recorded commit.
The current checkout also includes Phase 2; use its
[launch and fit checks](SSH_LOBBY_PRESENTATION_PHASE2_CHECKPOINT.md#launch-the-isolated-preview)
for review of that change. Run the
[preview tool](../tools/ssh_lobby_presentation_preview.py) in a local operator
terminal from the repository root:

```sh
cd /home/code/source/dreamland
env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/ssh_lobby_presentation_preview.py \
  --welcome-path lobby/welcome.txt \
  --guidelines-path lobby/guidelines.txt \
  --contact-path lobby/contact.txt \
  --color auto --clear-screen auto
```

These explicit paths use the current operator copy referenced by the working
INI at implementation time. Replace them if that configuration changes. The
tool does not load the working INI or its environment overrides. Omitting a
notice path selects that notice's packaged default. Each new connection takes
its own immutable snapshot; reconnect after editing copy. The startup output
prints the initial notice SHA-256 so the operator can record the tested copy.

The listener binds only `127.0.0.1` on an automatically selected free port.
Startup prints the actual OpenSSH command, a disposable password, the host-key
fingerprint, effective presentation settings, and the temporary state directory.
In a second local terminal, run the printed command, verify the displayed host
key against the printed fingerprint, and enter the printed password. Its form
is:

```sh
ssh -tt -p PORT -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=ask visitor@127.0.0.1
```

`PORT` is the numeric port printed at startup. The `/dev/null` known-hosts option
is for this disposable local test key on macOS/Linux. PuTTY can connect to the
same numeric loopback port with username `visitor` and the printed password;
verify its key prompt against the preview fingerprint. If the client machine
differs from the development host, first use the operator's existing SSH access
to forward a client loopback port to the printed development-host loopback
port, then point the test client at that local forwarded port. Record that
tunnel in the results. The harness itself always remains bound to loopback.

Both worlds, the public-reference index, host key, catalog, session profile
index, and failure directory live under a new temporary directory. `shutdown`,
Ctrl-C, or EOF in the operator terminal stops this listener and cleans up its
temporary state. No production instance needs restarting. The preview uses the
actual SSH/lobby/console/visit code with the built-in deterministic agent:
`whoami`, `pwd`, and `exit` support the shell handoff check; other commands may
return a deterministic acknowledgment. It composes no model agent, enrichment
scheduler, provider client, or external tracing. This exercises presentation
and visit lifecycle, not emulation quality or live provider behavior.

Add `--langfuse-notice` to exercise the optional disclosure sentence as a copy
fixture. This flag changes displayed text only; it does not enable tracing.
Add `--log-level INFO` or `--log-level DEBUG` to inspect lifecycle/presentation
decisions in the local operator terminal. These are disposable operator test
events and must not be described as attendee evidence.

### Effective settings and alternative cases

The launch above explicitly sets `color=auto` and `clear_screen=auto`. The
capability decision uses each SSH client's negotiated TERM. Reconnect after
changing the terminal type. To disable accents while retaining supported
clearing, restart the preview with `--color off --clear-screen auto`. For linear
plain output use `--color off --clear-screen off`. CLI options are explicit
preview settings; normal deployment uses the documented lobby INI/environment
settings in [SSH Lobby](SSH_LOBBY.md).

On macOS/Linux, test automatic plain fallback by prefixing the printed SSH
command with `env TERM=dumb`, then separately `env TERM=unknown-preview-term`.
For example:

```sh
env TERM=dumb ssh -tt -p PORT -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=ask visitor@127.0.0.1
```

Keep the real terminal's window size while changing only the negotiated TERM.
PuTTY's equivalent is its configured terminal-type string. The shell may report
its own simulated TERM; that is not the SSH presentation capability source.

The default `--catalog normal` publishes two disposable worlds. The operator
console supports ordinary one-shot commands such as:

```text
lobby worlds list
lobby worlds update alpha --title "Preview Alpha Updated"
sessions --live
sessions --close PUBLIC_ID
```

Update a title while the attendee is at the picker, then submit the displayed
number to exercise stale-menu feedback. Refresh with `R` and Enter to recover.
Use a real printed public ID with `sessions --close` to exercise interrupted
visit feedback and retained Ended identity. Repeated input retries and refreshes
should retain the last visit reference.

For an unavailable world, restart with `--catalog unavailable` added to the
same launch command. World 2 (`Preview Beta`) is published and then its newly
created fixture profile is removed. Select 2, confirm the unavailable feedback
survives the new picker, and select 1 to recover. In the operator console,
`lobby worlds remove beta` removes the invalid fixture from the catalog; the
attendee can then refresh. Do not use deployment paths for this scenario.

For an empty catalog, restart with `--catalog empty`. Complete the disclosure
and agreement and confirm the empty-catalog explanation and refresh/quit
actions remain visible. Recover without reconnecting by entering this in the
operator console, then `R` and Enter in the attendee terminal:

```text
lobby worlds add alpha --title "Preview Alpha" --description "Recovered test world." --entry-username operator
```

The command includes the entry account required by the later catalog version 2
so this preview remains runnable; it does not change the recorded Phase 1
presentation acceptance.

### Human Phase 1 checklist

For each core client, record application/version, OS, negotiated TERM, viewport,
font/zoom, theme, ANSI-color settings, multiplexing/tunnel use, and pass/fail.
Run at 80x24 and a larger viewport, plus practical narrow/short sizes. Record any
client minimum-size limit rather than claiming smaller dimensions were tested.

1. Read the welcome and every guidelines page. Check clean page changes,
   preserved paragraph/contact/disclosure text, visible numbered actions, and
   correct any-key versus Enter wording. Continue guidelines only with the
   normal continuation input.
2. Submit a wrong agreement and wrong world number; refresh. Exercise the stale,
   unavailable, and empty cases above. Confirm feedback stays visible and each
   case can recover. Typing and ordinary retries must not unexpectedly clear
   the active editable prompt.
3. Select a world, run `whoami` and `pwd`, then `exit`. Check ordinary shell
   colors, exact Started/Ended marker text, and a copyable Ended public ID on
   the picker. Retry or refresh and check the ID remains. Visit again and check
   the new public ID differs. Repeat once with operator `sessions --close`.
4. Repeat readability checks on light and dark themes, with client ANSI colors
   disabled, with preview color off, with clearing off, and with both off.
   Reconnect using `dumb` and an unknown TERM and confirm functional plain
   output without literal presentation escape text.
5. Resize between pages, then resize during a partly typed agreement or world
   selection. Finish the action. Confirm the next boundary uses the new size
   and typed characters were not lost or overwritten. Inspect a practical tiny
   layout, keeping action instructions and the last public ID reachable.
6. Quit with `Q` and Enter and test Ctrl-C/Ctrl-D cancellation on separate
   connections. Confirm clean terminal styling after the lobby closes.

### Evidence and acceptance

| Evidence | Result |
| --- | --- |
| Branch/base | `feature/ssh-lobby-presentation` / `452af31852dfbb9538f065a6618a221ec1efdc56` |
| Reviewed Phase 1 diff fingerprint | `7388c1be87eb4bc839e360856e1a0af6b16264bbab524788ce6405be8f741dab` |
| Notice SHA-256 and presentation settings | `1ee4b799076a4441ac0fa498c3d0d11b0f025bad499ba185811d3cc32acccef6`; `color=auto`, `clear_screen=auto` for CLI rehearsal |
| Owning automated suites and checks | 228 regression tests passed; latest 15 focused presentation tests passed; Ruff lint/format and diff checks passed; scoped typing passed for all four changed runtime modules |
| Automated real-SSH smoke | Passed on 2026-09-11; actual preview CLI and Paramiko, as described below |
| Human acceptance | Accepted by the operator on 2026-09-11; explicit direction to commit and continue |
| Phase 1 commit | `4e29ff567bd7df56450cf05f9032c638a4e6f657` |

At 13:58 UTC on 2026-09-11, an automated Paramiko client connected to the actual
preview CLI running in an operator PTY with the explicit operator-copy paths
above. At 80x24 with `xterm-256color`, the client observed clearing/basic color,
advanced both pre-agreement actions, rejected a wrong agreement without a clear,
and retained invalid-selection feedback on the refreshed picker. It completed
two deterministic visits with distinct public IDs, checked plain `whoami`
output in the world shell, and verified the Ended ID survived a picker refresh.
A separate `dumb` connection received an escape-free welcome and quit cleanly.
The operator console's `shutdown` exited successfully, and the disposable state
directory was verified absent afterward. The tested notice SHA-256 was
`1ee4b799076a4441ac0fa498c3d0d11b0f025bad499ba185811d3cc32acccef6`.
This is automated transport/output evidence; no human client rendering result
is implied. The final diff fingerprint must be refreshed if implementation
changes after this rehearsal.

The baseline before edits passed all 94 existing lobby tests. The integrated
regression command passed 228 tests in 54 seconds:

```sh
env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest \
  tests.test_lobby_config tests.test_config tests.test_lobby_content \
  tests.test_lobby_catalog tests.test_lobby_identity tests.test_lobby_input \
  tests.test_lobby_presentation tests.test_ssh_lobby \
  tests.test_ssh_lobby_presentation tests.test_ssh_lobby_presentation_review \
  tests.test_ssh_lobby_presentation_preview tests.test_ssh_lobby_sessions \
  tests.test_ssh_lobby_audit tests.test_ssh_lobby_transport \
  tests.test_ssh_server tests.test_ssh_sessions tests.test_main
```

Three additional focused cleanup checks for shutdown, timeout, and controller
failure were then added. The latest `tests.test_ssh_lobby_presentation` run
passed all 15 checks, including those additions. The larger command above will
now discover 231 tests. An earlier invocation mistakenly named the nonexistent
`tests.test_ssh_metadata` module; that invocation is not passing evidence.
The corrected command above passed. Layout tests retain the original plain
20x5 through 120x100 matrix and strictly allow only the renderer's controls in
styled output. Additional checks cover unread-text reflow, retained IDs,
feedback, and actual title/description grouping.

Ruff lint and formatting passed for the 12 changed Python files, and
`git diff --check` passed. Scoped typing passed with:

```sh
.venv/bin/mypy --follow-imports=skip src/dreamland/config.py \
  src/dreamland/lobby_presentation.py src/dreamland/ssh_lobby.py \
  src/dreamland/ssh_transport.py
```

The unrestricted transitive mypy check stops in the installed
`numpy/__init__.pyi:737`: its `type` syntax needs Python 3.12 while the project
type-check target is 3.11. This dependency/tooling limitation remains open; the
scoped check does not establish a passing full transitive type check.

At the recorded Phase 1 commit, the plain [catalog preview](../lobby/preview.txt)
was regenerated from the
current operator notice files and catalog with the optional Langfuse sentence.
It remained byte-identical to the preceding four-page preview, confirming no
copy or ordinary plain-layout change. That fixture's notice hash is
`ca8fad4b8b840e2439ba9e6a0600d2e6bf5606e122cc1f55a25ea33d404e65ac`.

The fingerprint covers the base revision, binary diff against HEAD, and sorted
untracked-file hashes. To avoid a self-reference, only this document's
fingerprint table value is normalized to `<fingerprint>` before hashing.
The local preimage manifest and regeneration script are in
`tmp/SSH_LOBBY_PRESENTATION_PHASE1_FINGERPRINT.json` and
`tmp/fingerprint_ssh_lobby_presentation.py`. These are local review artifacts,
excluded from the pending change and never staged automatically.

| Human client | Result | Actual settings and notes |
| --- | --- | --- |
| PuTTY on Windows | Pending | Record version, terminal type, ANSI-color settings, theme, dimensions |
| macOS Terminal with OpenSSH | Pending | Record Terminal/macOS versions, profile/ANSI settings, theme, dimensions |
| GNOME Terminal or another VTE terminal | Pending | Record terminal/VTE versions, TERM, theme, dimensions |
| Konsole or xterm | Pending | Record terminal/version, TERM, theme, dimensions |
| tmux or GNU screen | Pending | Record multiplexer/version and outer terminal, TERM, dimensions |
| Plain/legacy/unknown terminal walkthrough | Pending | Record TERM/settings and readable recovery actions |

Automated checks do not substitute for human rendering results. The operator
accepted Phase 1 and authorized continuation without supplying the full client
matrix; those missing results remain explicit. Phase 2 still requires its own
[human checkpoint](SSH_LOBBY_PRESENTATION_PHASE2_CHECKPOINT.md) before committing
the static banner or beginning Phase 3.

## Maintenance

Update this record with the exact implementation/copy under test, automated
evidence, actual human client results, acceptance, and the resulting phase
commit. Keep Phase 1 evidence tied to its recorded commit; record later work in
its separate phase checkpoint. Keep missing client access visible and preserve
the separation between provider-free preview evidence and deployed attendee activity.
