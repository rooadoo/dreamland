# Native SSH Lobby

Status: Active
Audience: DREAMLAND operators, maintainers, conference staff
Purpose: explain optional SSH lobby configuration, curated worlds, fresh visits,
public report references, and operator rehearsal.

## Overview

The existing SSH listener supports `direct` and `lobby` entry. `direct` remains
the default private shell/exec experience. In `lobby` mode an authenticated
attendee reads deterministic introductory text, accepts guidelines once per
real SSH connection, and selects an operator-curated saved world. Every
selection creates a fresh isolated simulation. Leaving the starting shell
returns to the picker on the same SSH connection.

This is native terminal/backend behavior and implements the
[project model](PROJECT_MODEL.md). Its local operator controls do not add web
UI, REST, or MCP authority. The implementation and release assessment are
tracked in the [development plan](SSH_LOBBY_DEVELOPMENT_PLAN.md); configured
copy and passing tests alone do not establish public deployment readiness.

## Details

### Configuration and Admission

Set the entry mode in the active INI file, then restart the service:

```ini
[ssh]
entry_mode = lobby

[lobby]
catalog_path = lobby/catalog.json
public_registry_path = lobby/public_visits.sqlite3
idle_timeout_seconds = 300
max_input_bytes = 256
max_invalid_attempts = 20
retention_days = 30
color = auto
clear_screen = auto
banner = auto
intro_animation = auto
```

`DREAMLAND_SSH_ENTRY_MODE` overrides `[ssh] entry_mode`. Each `[lobby]` setting
has a higher-precedence environment override named
`DREAMLAND_LOBBY_<UPPERCASE_SETTING>`. INI-relative paths, including defaults,
resolve against the active INI file's directory. Environment path overrides
must be absolute; paths expand `~`. Blank paths and invalid modes or bounds
fail configuration loading. Profile files retain the existing `state_profiles/`
directory convention, relative to the process working directory.

| Setting | Default | Accepted bounds or meaning |
| --- | --- | --- |
| `catalog_path` | `lobby/catalog.json` | Persistent curated catalog; a missing initial file creates an empty catalog. |
| `public_registry_path` | `lobby/public_visits.sqlite3` | Owner-only SQLite public-reference mapping; persist this file across restarts. |
| `welcome_path` | Packaged `welcome.txt` | Optional replacement UTF-8 plain text. |
| `guidelines_path` | Packaged `guidelines.txt` | Optional replacement UTF-8 plain text. |
| `contact_path` | Packaged `contact.txt` | One line; defaults to `admin@boxisalie.com`. |
| `idle_timeout_seconds` | 300 | Integer seconds, 1–86400; applies while waiting in lobby screens. |
| `max_input_bytes` | 256 | 16–4096 bytes per response, including editing/control bytes. |
| `max_invalid_attempts` | 20 | 1–1000 invalid responses before disconnect. |
| `retention_days` | 30 | 1–3650 days after a reference ends; independent of transcript storage. |
| `color` | `auto` | `auto` enables basic foreground accents for recognized color terminals; `off` disables them. |
| `clear_screen` | `auto` | `auto` clears the visible display on recognized terminals before a complete page; `off` retains linear output. |
| `banner` | `auto` | `auto` uses the large initial welcome wordmark when the full welcome and action fit; `simple` forces the text wordmark. |
| `intro_animation` | `auto` | `auto` enables the two-second introduction on a sufficiently large, recognized terminal with display clearing enabled; `off` shows the static welcome immediately. |

Lobby composition is enabled only for `lobby` mode. Invalid initial catalog,
copy, or mapping storage fails startup without falling back to direct entry.
The standard listener requires the SSH username `dreamland`, or
`dreamland_simple` for the simple presentation described below, with the same
configured shared password. Optional demo logins use a separate password as
described below. Persistent SSH host keys, one session channel,
and forwarding/subsystem restrictions still apply. The attendee `!dreamland`
namespace remains denied.
Only an interactive PTY shell can enter the lobby; exec and shells without a
PTY are rejected before simulation creation. For an interactive connection:

```sh
ssh -t -p 2222 dreamland@127.0.0.1
```

Use complete `i agree`, case-insensitive with surrounding spaces/tabs allowed,
then Enter. `Q`, Ctrl-C, or Ctrl-D cancels lobby input. Narrow terminals use
deterministic wrapping and paging. The picker accepts a displayed number, `R`
to refresh the published catalog, or `Q` to disconnect. Input limits or idle
expiry close the parent connection; neither starts a world. Input is never
routed to the LLM, operator console, real shell, or a profile path.

The welcome, guidelines, picker, and continuation pages share a numbered footer.
Guidelines and the picker use ASCII connected-node framing when the viewport has room. Compact layouts
remove framing and page labels to preserve content and action instructions.
Welcome and continuation prompts say `Press any key to continue, or Q to quit.`
Compact layouts use `Any key: continue` and `Q: quit` on separate lines. These
prompts accept a key immediately. Agreement and world selection show a `>`
input line and explicitly require Enter to submit. Only the final guidelines page asks for
agreement.

The pager reserves the space needed for the wrapped instructions before
placing content. It keeps paragraphs together when they fit, joins a short
closing contact line to its preceding context when possible, and never repeats
body text when continuing. Descriptions retain their indentation. Presentation
fits within 80 columns; below 40 columns or 12 rows, compact instructions take
priority over framing. The controller supplies decoration around the
operator-editable notices; their content and agreement requirements stay intact.

Already-buffered bytes are discarded at each phase/visit boundary; known
partial keys and CR/LF continuations are consumed before later input. A finite
64 KiB boundary-drain limit prevents a continuous sender from trapping the
controller. Completing a previously discarded partial key is also bounded by
`max_input_bytes`; Ctrl-C/Ctrl-D cancel even while that key is incomplete.
Raw SSH cannot distinguish delayed scripted bytes from newly typed
bytes; agreement is an explicit phrase requirement, not proof of human reading.

### Demo Service Tier

The optional, case-sensitive `_fast` login selects an emulator backend requesting
`ultrafast`. Normal logins use `[agent] openai_service_tier`, whose default is
`fast`. Keep that setting at `fast` for general use; a global `ultrafast` override
would still affect normal sessions.

| SSH username | Password environment variable | Emulator tier |
| --- | --- | --- |
| `dreamland` | `DREAMLAND_SSH_PASSWORD` | Configured `[agent]` tier, normally `fast` |
| `dreamland_simple` | `DREAMLAND_SSH_PASSWORD` | Configured `[agent]` tier, normally `fast` |
| `dreamland_fast` | `DREAMLAND_SSH_FAST_PASSWORD` | `ultrafast` |
| `dreamland_fast_simple` | `DREAMLAND_SSH_FAST_PASSWORD` | `ultrafast` |

Set the optional password in the server process environment and restart. Neither
password is read from the INI file. An unset or empty demo password disables the
demo aliases. A whitespace-only password or a password equal to the normal
password fails lobby startup. Each password works only for its own login family.
Reversed, repeated, or differently capitalized suffixes are rejected.

Docker deployments store the optional password in the private deployment volume.
Inside the updated container, initialize or rotate it with hidden prompts:

```sh
python -m dreamland.deployment configure-ssh-fast --root /var/lib/dreamland
```

Restart the SSH container from the host with `deploy/dreamlandctl restart`.
This command changes only the fast password; normal SSH, OpenAI, and Langfuse
credentials remain intact. The stored value enables both `dreamland_fast` and
`dreamland_fast_simple`. Existing deployments without it keep those aliases
disabled. An environment variable exported in an interactive container shell
does not change the running server, and the deployment launcher ignores ambient
credential overrides. See [decision 0130](decisions/0130-deployment-fast-ssh-password.md).

```sh
ssh -t -p 2222 dreamland_fast@127.0.0.1
ssh -t -p 2222 dreamland_fast_simple@127.0.0.1
```

The server chooses the backend once for the authenticated connection. All fresh
world visits on that connection retain its tier; concurrent normal connections
keep their own backend. Only the emulator tier changes: model, reasoning,
Spiritguide, enrichment, notices, catalog, and in-world usernames retain their
configured behavior. Session retirement releases the chosen agent's state after
outstanding command work stops. The combined `_fast_simple` alias also skips
animation and disables lobby color. Direct entry treats suffixes literally and
ignores the demo password.

Lifecycle logging records requested tier and login mode with parent/session
references; existing model/token logs retain provider-reported tier evidence.
Effective configuration reports only `fast_password_configured`. See
[decision 0125](decisions/0125-demo-ssh-lobby-tier.md) for the isolation rationale
and manual checkpoint. No running deployment is changed by enabling this in a
local checkout.

### Terminal Presentation

For clients that struggle with the animation or color, append the exact,
case-sensitive `_simple` suffix to the lobby login:

```sh
ssh -t -p 2222 dreamland_simple@127.0.0.1
```

This connection goes directly to the static welcome and disables lobby color
through the guidelines, picker, visit markers, and returns from worlds. It
does not modify shared settings or other connections. Screen clearing and the
static banner layout still follow `clear_screen` and `banner`; `_simple` is
not a guarantee of escape-free output. World-owned login banners and simulated
command output are unchanged. No INI edit is needed for this option.

Authentication requires the password for the selected base login: the normal
password for `dreamland_simple`, the demo password for `dreamland_fast_simple`.
`dreamland_static`, `other_simple`, and repeated suffixes such as
`dreamland_simple_simple` are not aliases for the standard `dreamland` account.
Audit logs retain the exact submitted SSH name, while each world's catalog
entry still supplies its simulated username. Direct-entry mode keeps suffixes
as literal parts of usernames and has no simple-login presentation override.
See [decision 0124](decisions/0124-simple-ssh-lobby-login.md).

Phase 1 implemented basic color and visible-display clearing in commit
`4e29ff567bd7df56450cf05f9032c638a4e6f657`. Its
[human terminal checkpoint](SSH_LOBBY_PRESENTATION_CHECKPOINT.md) was accepted
by the operator on 2026-09-11, authorizing the Phase 1 commit and Phase 2 work.
Phase 2 added a static welcome banner and simpler continuation wording in
`d9ef6f7295109c973d6d80a3e49d0baf967a61a0`; its
[human checkpoint](SSH_LOBBY_PRESENTATION_PHASE2_CHECKPOINT.md) was accepted
on 2026-09-11 with "fab. commit and continue", authorizing Phase 3.
The design and commit gate are recorded in
[decision 0096](decisions/0096-ssh-lobby-presentation.md).

Color, clearing, and introduction motion have separate off controls. Disabling
clearing also skips the introduction. Set
`color`, `clear_screen`, and `intro_animation` to `off` for a stable linear
presentation. The environment overrides are
`DREAMLAND_LOBBY_COLOR`, `DREAMLAND_LOBBY_CLEAR_SCREEN`, and
`DREAMLAND_LOBBY_INTRO_ANIMATION`; values are normalized
for case and surrounding whitespace. Blank values and choices other than
`auto` or `off` fail configuration loading.

The capability hint is the TERM sent by the SSH client in its PTY request.
[`lobby_presentation.py`](../src/dreamland/lobby_presentation.py) accepts at most
64 ASCII characters, starting with a letter or digit and followed only by
letters, digits, `+`, `_`, `.`, or `-`, then normalizes to lowercase. Capability
selection uses these exact names:

| Terminal family | Names with color and clearing |
| --- | --- |
| xterm | `xterm`, `xterm-color`, `xterm-16color`, `xterm-256color` |
| PuTTY | `putty`, `putty-256color` |
| rxvt | `rxvt`, `rxvt-color`, `rxvt-256color`, `rxvt-unicode`, `rxvt-unicode-256color` |
| Linux console | `linux` |
| GNU screen | `screen`, `screen-256color`, `screen-bce`, `screen-256color-bce` |
| tmux | `tmux`, `tmux-256color` |

`vt100`, `vt102`, `vt220`, and `xterm-mono` support clearing without color.
Missing, malformed, unknown, and `dumb` TERM use plain output. Arbitrary family
prefixes and extra suffixes grant no capabilities. Color, clearing, and motion
are separate capability fields; the listed terminals also support the optional
introduction. The server process's TERM is not consulted. SSH environment requests remain denied, so the client's
local `NO_COLOR` and `COLORTERM` do not change these settings. No terminal query
replies enter the agreement or selection stream.

The renderer wraps and budgets plain text before adding styles. Cyan accents
identify headings, frame rules, and selection lines; green identifies actions;
magenta identifies simulation references. Body text retains the default
foreground and background. Roles belong to renderer fragments, so mutable
notice phrases are never matched to decide their color. Notice and catalog
validation still rejects embedded control characters. Every styled fragment
ends with a reset, and a colored lobby resets before the world shell takes over
and when the connection ends.

The initial welcome uses a static 47-column, three-row ASCII DREAMLAND wordmark
with a connected-node motif. `[lobby] banner=auto` is the default;
`banner=simple` forces a single text heading. `DREAMLAND_LOBBY_BANNER` overrides
the INI value, with the same case/whitespace normalization and explicit rejection
of blank or unsupported values. The isolated preview exposes `--banner auto`
and `--banner simple`.

Artwork spacing is fixed data in `lobby_presentation.py`, separate from notice
wrapping. The renderer selects the large wordmark only when all welcome prose,
paragraph gaps, artwork, margins, leading row, and action/footer fit together.
It requires at least 51 reported columns because of the two-column margins on
both sides; the height threshold depends on the current copy and wrapping.
The artwork never wraps, truncates, splits, or creates extra welcome pages.
Long notices use the simple heading and normal paragraph paging. Continuation
pages use compact branded headings; guidelines and the picker retain their
existing framing. Later pages never introduce the large art after a resize.

Either enabled color or enabled clearing permits the artwork on a compatible
client. Disabling both, unsupported TERM, or the minimal layout below 20x5 forces
the simple heading. The cyan heading role supplies color when enabled; a
clearing-only terminal displays the same artwork without color. A notice's
exact leading standalone `DREAMLAND` paragraph is treated as its existing
wordmark and rendered once through the heading. All other prose is preserved,
and notice files are unchanged. This avoids repeating the brand while keeping
operator text intact. See the [simple preview](../lobby/preview.txt) and
[large-banner projection](../lobby/preview-banner.txt) for the current copy.

Phase 3 adds an optional two-second horizontal wordmark reveal before the first
welcome. The operator requested this larger, multicolor presentation in place
of the initial one-second, single-line design.
`[lobby] intro_animation=auto|off` and `DREAMLAND_LOBBY_INTRO_ANIMATION` use the
same normalization and validation as color/clearing. The default is `auto`,
following operator acceptance on 2026-09-11: "perfect, commit and continue".
Use `--intro-animation off` in the isolated preview for immediate static output.
The [Phase 3 checkpoint](SSH_LOBBY_PRESENTATION_PHASE3_CHECKPOINT.md) records
the reviewed implementation, acceptance, and remaining client-matrix gaps.

The introduction requires a recognized TERM with clearing and motion support,
`clear_screen=auto`, and at least 59 columns by 11 rows. A 57-column, nine-row
ASCII wordmark and network rails are centered within the reported terminal
viewport, capped at 240x100. Twenty frames reveal the image
from left to right across 2,000 ms of server scheduling, within a hard 16 KiB
output budget including cleanup. Basic cyan, magenta, and green accents follow
the horizontal reveal; `color=off` preserves the monochrome effect. Slow writes
drop obsolete frames, so network and terminal delivery can make the observed
duration longer without creating a frame backlog.

Motion clears the visible display before the first frame and again at the end,
including when skipped or resized. Numeric cursor-position controls place each
art row directly; frames contain no newlines and restore default color. The
normal static welcome then renders. Clearing disabled, insufficient space, or
unsupported TERM skips the introduction. `banner=simple` changes the final
heading independently of motion. No cursor hiding, scrollback erasure,
alternate screen, terminal query, background writer, model call, or simulated
boot/progress claim is involved.

Motion runs once per admitted lobby connection, before any interactive prompt.
Q, Ctrl-C, and Ctrl-D cancel; another key skips only the effect and leaves the
welcome waiting for a new continuation key. Buffered paste and known CR/LF,
escape, and UTF-8 tails are consumed by the existing finite boundary rules.
Arbitrarily delayed bytes cannot be distinguished from newly typed input.
A resize ends motion, clears its display, and the welcome uses the new
dimensions. Shutdown, disconnect, and idle timeout cancel within the bounded
input polling path.
Output and empty boundary drains never refresh idle activity. The lobby timer
explicitly resumes when an active world visit returns control to the picker.
There is no animation on pagination, agreement, refresh, retry, or world visits.

Compatible clients clear before complete screen/page renders and at both
introduction boundaries, using
visible-display erase (`ESC[2J`) and cursor home (`ESC[H`). Guidelines advance
only on the existing continuation input. Typing, normal echo, and an agreement
prompt retry do not clear the display. A submitted picker retry renders its
feedback as content in the new picker. The implementation does not erase saved
scrollback or use an alternate screen; retention of previous display contents
in scrollback depends on the client.

SSH window-change requests update a bounded viewport snapshot after PTY
admission. Zero dimensions mean unspecified and retain the preceding value;
positive values are bounded to 240 columns and 100 rows. The lobby reads this
snapshot at page and action boundaries and keeps its 80-column reading width.
Resizing during input does not redraw beneath the editable prompt. Unshown
content is repaginated at the next boundary, with space reserved for the actual
wrapped footer and repeated report ID before body content is placed.

At 20x5 and larger, paged output budgets every visible row and removes
decoration first. When a picker has a last ended ID and only five to seven
rows, its final action uses two compact lines:

```text
#:start R:refresh
Q:quit; Enter>
```

The ID repeats on every picker page, including the final action page. Its
world title and remaining status text are paged as necessary. Below 20 columns
or five rows, the lobby explicitly announces small-terminal plain output and
streams remaining content at the reported width without artificial paging,
color, or clearing. This minimal layout can scroll; it does not claim the
terminal is large enough to display the full action contract at once.

Invalid selections, changed/unavailable worlds, an empty catalog, pending
cleanup, failed starts, and interrupted visits remain in the next picker's
content rather than being immediately hidden by a clear. The exact Started
and Ended marker text is preserved. The last ended public ID remains available
through refreshes and retries until another started visit supersedes it or the
connection ends. This report reference does not rely on scrollback retention.
The existing configurable lifecycle logger records normalized TERM and selected
color/clearing modes and `simple=True|False` once per connection.
The configurable debug lifecycle log also records the selected large/simple
welcome banner and the viewport for its initial page.

### Catalog and Saved Worlds

The repository's [starter catalog](../lobby/catalog.json) selects three bundled
world examples: Morrowbasket Markets, Common Ground Colonyship, and Muirbank
Alpha. Their runnable schema-2 profiles ship in the Python package as documented
derivatives of the archived evaluated worlds, with saved host-local login
banners. The shell sends the selected world's active-host banner before its
first prompt without an LLM call. This is separate from the lobby welcome
wordmark and the real transport's neutral `SSH-2.0-DREAMLAND` identifier. Follow the
[starter-world setup](../lobby/README.md#fresh-checkout) to copy missing
profiles into the operator's local store before using that catalog. Lobby
publication still requires local regular files; installing the package alone
does not publish worlds or replace an existing catalog.

Publish explicit saved `world` or compatible `system` profiles through the
local operator console:

```text
lobby worlds list
lobby worlds add offshore --title "Offshore Operations" --description "Explore a facility and its connected systems."
lobby worlds add after-hours --title "After Hours" --description "Investigate a network after closing." --entry-username operator
lobby worlds update offshore --description "Explore the facility, offices, and remote connections."
lobby worlds update offshore --entry-username technician
lobby worlds order after-hours offshore
lobby worlds remove offshore
lobby worlds reload
```

The examples assume those profile files already exist; they are not a supplied
conference catalog. `add` accepts an optional `--entry-username` override. When
omitted, it resolves the starting account from the saved profile: first
`state.login_username`, then `state.username`, then the `default_user` on the
host selected by `world_state.entry_host`. If none is declared, it requires one
distinct literal account across that host's `facts_by_id` fields named
`username`, `local_username`, or `maintenance_username`. Missing, ambiguous,
or invalid accounts produce an error asking for `--entry-username`; there is
no prose parsing or guest/connecting-name fallback. The resolved account is
validated and stored in the catalog just like an explicit override. Later
source changes do not silently change it. `update` accepts any combination of
title, description, and entry username; omitting the account preserves the
current catalog value. Choose an account established by the saved world. `list`
shows the configured account alongside each profile and its public copy.
`order` requires every current profile exactly once. Removing an entry leaves
its source file and active visits intact. To empty the catalog, remove its
entries or reload a document with `"worlds": []`.

The catalog file has this explicit schema:

```json
{
  "kind": "dreamland.ssh_lobby_catalog",
  "schema_version": 2,
  "worlds": [
    {
      "profile": "offshore",
      "title": "Offshore Operations",
      "description": "Explore a facility and its connected systems.",
      "entry_username": "leah",
      "order": 1
    }
  ]
}
```

There are at most 100 entries. Profile names are 1–128 ASCII letters/numbers,
dots, dashes, or underscores, beginning with a letter/number; they never contain
paths. Titles are at most 80 characters; descriptions at most 320. Both require
explicit nonempty single-line text and reject terminal controls and invisible
formatting characters. The reserved `[DREAMLAND simulation` marker prefix is
also rejected, case-insensitively. Menu copy never comes from profile comments or JSON.
Display order is contiguous from 1, and duplicate profiles are rejected.
`entry_username` must be a literal 1–64-character ASCII account name matching
`[A-Za-z_][A-Za-z0-9_.-]{0,63}`. Whitespace, path separators, templates, controls,
and non-ASCII account names are rejected rather than sanitized.

Profiles must be regular saved files under the configured store, at most 16 MiB.
Packaged fallback names alone do not publish a world. Exact snapshots and
world/system documents carrying saved command history, shell stacks, or an
active program are rejected: prepare a compatible fresh-login profile first.
Validation boots the profile with its configured account and requires matching
current/login/shell usernames, matching `USER` and `LOGNAME`, and an absolute
`HOME` without control characters. Custom absolute home directories are valid.
Use `{username}` templates or the configured account for profile environment
overrides. Validation does not infer the intended account from world prose.
Automatic account resolution applies only when adding a catalog entry without
`--entry-username`, as described above.
The catalog's required `entry_username` seeds the new simulated login. A launch
reads and validates one detached saved copy, then boots that copy without
changing the global default. Later source edits cannot alter the active visit.

Catalog version 1 requires an explicit operator migration. Before starting the
updated service, edit the catalog offline: choose the correct account for every
profile, add its `entry_username`, and change `schema_version` to `2`. This also
applies to an empty version-1 catalog. Reload does not infer missing accounts or
automatically migrate old documents, and never falls back to the connecting SSH
name. An old schema or missing username fails startup; an invalid reload
preserves the last published catalog.
The bundled catalog already uses version 2.

Each mutation validates the complete prospective catalog and writes a private
temporary file, flushes it, and atomically replaces the destination before
publishing its in-memory snapshot. Failed validation/write preserves the last
published catalog. Operator file edits require `lobby worlds reload`; attendee
`R` only refreshes the published snapshot. Invalid reloads preserve the current
snapshot, including when profiles disappear or become malformed. Repair the
files and reload, or explicitly remove their entries. Initial invalid catalog
files fail startup. Catalog reads are bounded to 256 KiB. Catalog, content, and
profile reads use nonblocking regular-file descriptors and reject final-path
symlinks, FIFOs, and devices.

A picker selection is checked against the currently published revision. Any
intervening curation/reload requires a fresh selection, so an old display number
can never launch a different entry. Missing/malformed selected profiles return
an unavailable message with refresh/quit; restoring the file permits recovery
without restarting or cached failure state.

### Connection Username and Simulation Entry

Lobby authentication still accepts the existing shared password and sanitized
nonempty SSH usernames. After successful authentication, lifecycle logging
records the exact username supplied to the real SSH connection, before
sanitization. The value is escaped so punctuation and Unicode can be recovered
without inserting terminal controls or additional log lines. It is an
attendee-supplied account name, not a verified personal identity.

Each catalog selection supplies its configured `entry_username` when creating
the simulation. Both `session.username` and `session.login_username` start with
that account; initial shell identity, username templates, environment, and
model context follow the existing profile initialization rules. Explicit
profile home directories and environment facts remain profile-owned. Keep
those facts consistent with the catalog account when authoring a world.

The original SSH name stays in transport/audit data and is not added to
simulated state or model context. A parent connection reference correlates
authentication and subsequent visit lifecycle records; each visit is
associated with its profile, configured entry account, public simulation ID,
and internal session ID. These records use the existing configurable
`lifecycle` log category and level. Retain these logs when original SSH-name
correlation is required; the public-reference database is not a username log.

At INFO, `SSH lobby authenticated` records `parent` and `ssh_username` after
successful authentication; `SSH lobby opened` repeats them when the controller
starts. `ssh_username` uses Python's escaped quoted representation (`%r`).
`SSH lobby visit entering` records `parent`, `public_id`, `profile`, and
`entry_username`; `SSH lobby visit created` adds `session`, and
`SSH lobby visit ended` also records `reason`. `SSH lobby connection closed`
records the same `parent` when the real connection ends. The raw name is not
added to session snapshots or trace metadata.

Leaving and reentering a world starts again with its configured account.
Selecting another world uses that world's account. Account changes through
simulated `su`, `sudo`, or nested SSH retain the usual runtime behavior; the
catalog controls fresh entry, not later account transitions. A catalog update
affects future selections and leaves active visits intact. Direct SSH shell
and exec, API, MCP, and other session entry paths keep their existing identity
rules. See [decision 0097](decisions/0097-lobby-simulation-entry-usernames.md).

### Editable Notices and Reports

Default copy is plain text in
[`src/dreamland/lobby_text/`](../src/dreamland/lobby_text/). Copy it into durable
operator files and set the three optional paths to customize a deployment.
There is no template substitution. Files are at most 16 KiB each, UTF-8, with
LF or CRLF line endings; terminal control characters and invisible formatting
characters are rejected. The contact file contains one nonempty line at most
256 characters. The controller owns prompts and visit boundary markers.

Each new parent connection reads one immutable notice snapshot. Its SHA-256
version covers welcome, guidelines, contact, and the automatic Langfuse
disclosure when tracing is enabled. The connection's acceptance time/version
remain associated with all its visits even after operator copy changes. File
updates apply to later connections; invalid updated copy prevents new lobby
admission without invalidating an existing accepted snapshot.

Default guidelines explain IP/username and activity collection, operational and
evaluation purposes, OpenAI processing, and non-publication of attendee activity
including slide/research excerpts. Lobby startup requires OpenAI for command,
Spiritguide, and enabled enrichment roles so the configured processors match
that settled disclosure. Direct mode retains its existing provider choices.
Adding another lobby processor requires a scope and notice revision. When
Langfuse is enabled, a sentence explains external trace storage. Keep custom
copy aligned with actual retained storage; a fresh world does not erase logs
or traces.

Every admitted visit receives an independent cryptographically generated public
ID, for example `DL-7M2K-P9XR-W4TZ-C6HN`, printed in `Started` and `Ended`
markers. The same ID remains through nested shells and profile application.
The marker is application output; similar simulated text cannot trigger a
lifecycle transition. A network failure can prevent delivery of the final
marker. Failed starts are recorded without printing a false Started marker.

Reports go to `admin@boxisalie.com` by default and should include the complete
simulation ID, world, approximate time, and reproduction steps. IDs identify
visits and grant no access or ownership claim. No public lookup or trace link
is exposed.

### Console Resolution, Tracing, and Retirement

Use complete public references in the native console without first listing
sessions:

```text
sessions --live
sessions --set DL-7M2K-P9XR-W4TZ-C6HN
spiritguide --session DL-7M2K-P9XR-W4TZ-C6HN add an engineering workstation
trace DL-7M2K-P9XR-W4TZ-C6HN
sessions --close DL-7M2K-P9XR-W4TZ-C6HN
```

Public IDs normalize case and require an exact canonical lookup. Malformed,
truncated, unknown, or expired public references fail explicitly; they do not
fall back to internal prefixes or the active session. Retained ended references
remain useful for inspection when historical data exists, and fail live
mutations. Internal-ID behavior remains available. These aliases exist only at
the native console boundary, not in registry, REST, or MCP lookup contracts.

The SQLite index contains public/internal IDs, profile, parent connection,
notice/acceptance, timestamps, and lifecycle status, with no transcript bodies.
It must be an owner-only regular file. Startup marks previously live/allocated
records interrupted and never recreates a session. Expired ended references are
hidden from lookup immediately and removed at startup and new allocation.
The registry's `cleanup()` method also removes them explicitly; invoke it on
the existing process-owned registry, not by constructing another registry
against a running service. For offline maintenance, a restart performs recovery
and cleanup. Retaining this index does not restore missing trace data. Keep
local/Langfuse trace retention operationally aligned with the reference period;
this feature does not change their retention settings.

Trusted runtime provenance projects `dreamlandPublicId` into lifecycle,
command, Spiritguide, and enrichment metadata. Model/profile fields with that
name have no authority. Inspect the deployed tracing integration during the
release rehearsal; local allocation/resolution works with Langfuse disabled or
unavailable, while trace lookup reports missing data accurately.

Outermost `exit`, `logout`, or Ctrl-D retires the visit and returns to the
picker. Nested simulated connections preserve their normal shell-stack rules.
Each subsequent visit has fresh runtime/checkpoint identity, public ID,
history, editor, and shell stack. Operator close stops its shell's input,
requests cancellation, unregisters it, suppresses obsolete output, and returns
the connected attendee to the picker. Actual EOF/disconnect and shutdown end
the parent connection. Already-running provider calls may finish later; their
results cannot reach a later visit. Four unfinished retired shell cleanups per
parent connection temporarily prevent further admission until previous work
settles; this is a local bound, not a global deployment capacity control.
Queued enrichment payloads are cleared on closure. The shared enrichment
executor bounds outstanding queue items at `max(16, 4 * max_workers)` and
reports `worker_queue_full` while blocked owners prevent draining them.

### Manual Operator Checkpoints

The [Phase 1 presentation checkpoint](SSH_LOBBY_PRESENTATION_CHECKPOINT.md)
provides an isolated provider-free SSH preview, the client/settings matrix,
readability and resize checks, and the acceptance record. Human acceptance is
required before committing Phase 1 or starting Phase 2; earlier lobby rehearsal
results do not satisfy this new presentation checkpoint.

1. Prepare two disposable saved world/system profiles, start with lobby mode,
   and add both with authored public copy and explicit entry accounts. List
   them and verify the accounts, edit one, reorder both,
   remove/re-add one, and edit the JSON file followed by `reload`. Confirm each
   update is visible after `R` on an already-open SSH connection. Restart and
   confirm the catalog persists. Try invalid JSON/control text and confirm the
   last published menu remains usable.
2. Connect interactively, read the paged guide, submit invalid agreement and
   selection text, then accept and choose each world. Confirm no visit exists
   before selection. Try remote exec and non-PTY shell requests and confirm
   rejection creates no simulated session.
3. Change files in a disposable world, enter/leave a nested simulated shell,
   and exit the starting shell. Choose the same world again and verify fresh
   state and a new ID. Use each complete ID in the console. Close an idle and a
   busy visit and confirm the picker returns; the ended ID cannot mutate the
   next visit. Refresh a picker after concurrently reordering its catalog and
   confirm stale numbers require selection again.
4. With controlled operator-created activity, verify trace correlation for a
   command, Spiritguide instruction, and enrichment work, including any late
   completion. Repeat local reference lookup with tracing disabled/unavailable.
   Return to direct mode and verify private shell/exec compatibility with no
   lobby or public markers. Record the tested revision and evidence in the
   required independent assessment; do not publish attendee transcripts.

For the entry-account change, record the following checks separately; these
instructions do not claim a completed human walkthrough:

1. With lifecycle logging enabled at INFO, connect using an arbitrary name,
   for example `ssh -t -p 2222 -l guest-review@conference 127.0.0.1`. In the
   bundled catalog, enter Morrowbasket Markets, Common Ground Colonyship, and
   Muirbank Alpha. Check `whoami`, `pwd`, `$USER`, `$LOGNAME`, and `$HOME` against
   `operator`, `reader`, and `leah`, respectively, and their profile home
   directories. Run shell-variable checks with ordinary commands such as
   `echo "$USER"`. Live emulator commands can call the configured provider.
2. Exit each starting shell and select another world on the same connection,
   then reenter the first. Confirm fresh simulation IDs and the selected
   world's configured starting account every time. Open a second connection
   with a different SSH name and verify the same per-world entry identities.
3. Inspect lifecycle records: the original `guest-review@conference` name is
   recoverable, all visits correlate to its parent connection, and each visit
   records its selected profile, entry account, and public/internal IDs.
   Confirm simulation inspection contains the configured account rather than
   the connecting SSH name.
4. In a disposable catalog, test an omitted/invalid account and a version-1
   document. Confirm an invalid reload preserves the current catalog; restart
   with invalid configuration and confirm startup fails clearly. Restore the
   version-2 document, reload, and verify account updates apply on fresh entry.
   Start a separate direct-mode instance and check shell/exec still use the
   original sanitized SSH login rules.

## Maintenance

Update this document with `config.py`, `lobby_catalog.py`, `lobby_content.py`,
`lobby_identity.py`, `lobby_input.py`, `lobby_presentation.py`, `ssh_lobby.py`,
native console, or SSH visit-lifecycle changes. Keep the example INI,
state-profile guidance, tests,
development plan, and security assessment consistent with actual behavior.
