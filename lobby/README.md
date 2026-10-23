# Curated Spiritguide SSH Lobby

Status: Active
Audience: DREAMLAND operators and maintainers
Purpose: document the three bundled example worlds and their curated SSH lobby.

## Overview

The repository ships three evaluated example worlds, [catalog.json](catalog.json),
and editable notices in this directory. The world baselines are also included
in the installed Python package. Follow the fresh-checkout steps below to create
local copies and enable lobby mode. Launch DREAMLAND from the repository root
so its `state_profiles/` store resolves correctly. The service reads the entry
mode at startup; restart an existing service after changing it.

[Preview the lobby screens](preview.txt) in an 80-column, 24-row terminal:
welcome, two guidelines pages, and the world picker. Each page uses the same
numbered action footer; guidelines and the picker retain connected-node
framing. Only the last guidelines page asks for
`i agree` followed by Enter. Continuation prompts take a key immediately;
agreement and selection show a `>` input line. Below 40 columns or 12 rows,
compact instructions take priority over decoration.

The saved preview is the plain-text projection with a simple welcome heading.
The [large-banner projection](preview-banner.txt) shows the Phase 2 static
ASCII artwork at 80x24 with escape sequences removed for review. Full art
appears only on the initial welcome, when all copy and actions fit together;
otherwise the runtime uses the simple heading. The exact leading standalone
`DREAMLAND` notice paragraph supplies the heading once, and all other prose and
notice file bytes remain unchanged. Continuation prompts now say
`Press any key to continue, or Q to quit.`

The operator accepted Phase 1 on 2026-09-11 and directed "Commit and continue";
commit `4e29ff567bd7df56450cf05f9032c638a4e6f657` records its color, clearing,
resize, and retained-ID changes. Individual client/version results remain
unverified in the [Phase 1 checkpoint](../docs/SSH_LOBBY_PRESENTATION_CHECKPOINT.md).
The [Phase 2 checkpoint](../docs/SSH_LOBBY_PRESENTATION_PHASE2_CHECKPOINT.md)
provides an isolated provider-free preview using this current copy, including
`--banner auto` and `--banner simple`. On 2026-09-11 the operator accepted
Phase 2 with "fab. commit and continue", authorizing its commit and Phase 3.

Phase 2 is committed as `d9ef6f7295109c973d6d80a3e49d0baf967a61a0`.
The [Phase 3 checkpoint](../docs/SSH_LOBBY_PRESENTATION_PHASE3_CHECKPOINT.md)
adds a two-second horizontal reveal of a centered nine-row ASCII wordmark and
network rails, with cyan, magenta, and green accents. The visible display clears
before and after the effect, then the accepted static welcome renders. The
operator accepted it with "perfect, commit and continue" on 2026-09-11. Motion
now defaults to `auto`; pass `--intro-animation off` for immediate static output.
A key skips the effect and the welcome still waits for its own continuation.
Q, Ctrl-C, and Ctrl-D cancel. Motion requires a recognized TERM, display
clearing enabled, and at least 59 columns by 11 rows. It never repeats during
visits or refresh. Static saved projections
remain the final screen appearance.

## Details

### Selected worlds

| Menu world | Bundled profile | Entry username |
| --- | --- | --- |
| Morrowbasket Markets | [lobby-supermarket](../src/dreamland/default_state_profiles/lobby-supermarket.json) | `operator` |
| Common Ground Colonyship | [lobby-colonyship](../src/dreamland/default_state_profiles/lobby-colonyship.json) | `reader` |
| Muirbank Alpha | [lobby-offshore](../src/dreamland/default_state_profiles/lobby-offshore.json) | `leah` |

These runnable schema-version-2 world baselines ship in the Python package.
[provenance.json](provenance.json) records packaged and local paths with file
and world-state hashes for integrity checks. World-state hashes use compact
UTF-8 JSON with sorted keys and unescaped Unicode. Research datasets, model
comparisons, grading, and study provenance are published separately.
Fresh setup loads the bundled profiles directly.

Every lobby visit uses the selected entry account. The supplied SSH username
remains an untrusted connection label; it does not seed the simulated login.
Entry accounts apply to lobby mode; other entry paths retain their identity rules.

### Files and operation

- [catalog.json](catalog.json) supplies menu order, titles, descriptions, and
  required per-world `entry_username` values. An older version-1 catalog must
  be edited offline to add explicit accounts and set `schema_version` to `2`
  before startup; no account is inferred from its profile or the connecting
  SSH name.
- [welcome.txt](welcome.txt), [guidelines.txt](guidelines.txt), and
  [contact.txt](contact.txt) copy the packaged notices for local editing. The
  runtime appends the Langfuse disclosure when tracing is enabled.
- `public_visits.sqlite3` is created automatically at service startup with
  owner-only permissions. It and SQLite transaction files are ignored by Git.
- The working INI and `state_profiles/` are already ignored by Git. They are
  operator-owned setup files. A local profile takes precedence over a bundled
  profile with the same name. Package updates do not overwrite local copies.

### Fresh checkout

Install the package and prepare the local INI as described in
[Operations](../docs/OPERATIONS.md#setup). From the repository root, copy each
missing baseline from the installed package without overwriting existing files:

```sh
.venv/bin/python - <<'PY'
from importlib.resources import files
from pathlib import Path

bundled = files("dreamland.default_state_profiles")
destination = Path("state_profiles")
destination.mkdir(parents=True, exist_ok=True)
for name in ("lobby-supermarket", "lobby-colonyship", "lobby-offshore"):
    payload = bundled.joinpath(f"{name}.json").read_bytes()
    path = destination / f"{name}.json"
    try:
        with path.open("xb") as output:
            output.write(payload)
    except FileExistsError:
        print(f"Preserved existing {path}")
    else:
        print(f"Created {path}")
PY
```

This copies the exact packaged bytes. Existing local files remain authoritative;
the lobby still validates that they are compatible regular files. State-profile
schema version 1 is unsupported: repair or explicitly replace old local copies
before startup, including operator-owned deployed storage. Package/image updates
do not replace them. The [banner contract and repair guidance](../docs/STATE_PROFILES.md#saved-initial-login-banners)
explains the required host fields and manual checks. The same
Python snippet works with a wheel installation when run with that installation's
Python from the intended working directory. The catalog and editable notices
in this repository must also be provisioned for that installation.

Set the following in the active INI and supply the required
`DREAMLAND_SSH_PASSWORD` through the environment:

```ini
[ssh]
entry_mode = lobby

[lobby]
catalog_path = lobby/catalog.json
public_registry_path = lobby/public_visits.sqlite3

[spiritguide]
model = openai:gpt-5.5
```

The example INI selects OpenAI for Spiritguide. Keep all lobby roles
compatible with the lobby's OpenAI processing notice. Command and enabled
enrichment roles must also use OpenAI, as they do in the example INI. Supply
`OPENAI_API_KEY` through the environment before starting the live runtime.

See [SSH Lobby](../docs/SSH_LOBBY.md) for optional notice paths, provider
requirements, catalog commands, and reference retention. The private `direct`
entry default and neutral `default` state profile remain available. Merely
installing the three resources neither enables lobby mode nor publishes them.

Run from the repository root:

```sh
.venv/bin/python -m dreamland
```

Connect from another terminal:

```sh
ssh -t -p 2222 attendee@127.0.0.1
```

Continue past the welcome, enter `i agree`, and select 1, 2, or 3. Verify the
starting hostname: `pt-por-042-pos03`, `hearth-c17`, or `leah-t14`, respectively.
Check `whoami`, `pwd`, `echo "$USER"`, `echo "$LOGNAME"`, and `echo "$HOME"`;
the starting accounts should be `operator`, `reader`, and `leah` for worlds
1, 2, and 3. Repeat with an arbitrary SSH name to confirm entry is consistent.
Use `exit` to return to the picker, then select another world and check that its
public simulation ID is different. Leave with `Q` from the picker.
The operator accepted the entry-account behavior on 2026-09-11; see
[decision 0097](../docs/decisions/0097-lobby-simulation-entry-usernames.md).
For audit, migration, and reentry checks, see
[Manual Operator Checkpoints](../docs/SSH_LOBBY.md#manual-operator-checkpoints).

### Planned server setup

The planned deployment installer will offer all three worlds in its initial
browser and SSH catalogs. It will preserve operator edits, additions, ordering,
and removals during updates; restoring bundled defaults will be a deliberate
action. That installer and browser catalog provisioning are future work. This
asset change supplies the baselines and existing SSH catalog only. See
[decision 0103](../docs/decisions/0103-bundled-lobby-example-worlds.md).

## Maintenance

Keep the bundled profiles, provenance record, entry accounts, and catalog tests
aligned with deliberate recuration. Research evidence is distributed
separately from the software release. Preserve operator-local profiles and catalog
choices when updating examples. After editing catalog JSON in a running
service, use `lobby worlds reload` in the native console.
