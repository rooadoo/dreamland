# 0119 Optional Lobby Entry Username

Status: Active
Audience: maintainers, DREAMLAND operators, profile authors
Purpose: avoid repeating an authored starting account when adding a lobby world.

## Overview

The version-2 lobby catalog requires a concrete entry account so each fresh
visit starts coherently, independently of the visitor's SSH username. Requiring
operators to repeat that account at curation time is unnecessary when the saved
profile already declares it. This refines [decision 0097](0097-lobby-simulation-entry-usernames.md)
without changing session identity rules or adding state-profile schema fields.

## Details

`lobby worlds add PROFILE --title TITLE --description DESCRIPTION` accepts an
optional `--entry-username`. An explicit value overrides account resolution.
Otherwise, resolve the first declared source in this order:

1. Saved `state.login_username`.
2. Saved `state.username`.
3. `default_user` on `state.world_state.hosts_by_name[entry_host]`.
4. One distinct account across that entry host's structured `facts_by_id`
   fields named `username`, `local_username`, or `maintenance_username`.

The final fallback supports the existing colonyship, offshore, and supermarket
baselines without rewriting their packaged bytes or provenance hashes. It
examines only direct fields of entry-host facts, never prose, other hosts,
privileged-account fields, or an arbitrary account chosen from a user registry.
Repeated references to the same account are unambiguous. Multiple different
accounts require an explicit override. Invalid declared values also require an
override rather than silently falling through to a lower-priority source.
There is no fallback to guest or the operator/visitor identity.

Resolved accounts pass the existing literal-name and fresh-login/environment
validation. Saved-profile reads retain their bounded, regular-file, store-local
checks. Failed additions preserve the published catalog and file. The concrete
account is persisted in the existing version-2 catalog; reload and later title
or description updates do not re-infer it from changed source facts. `update`
without an account continues to preserve the current catalog value. Existing
catalog migration, launch behavior, and lifecycle logging remain unchanged.

Provider-free catalog and console tests cover resolution, source priority,
bundled profiles, overrides, missing/ambiguous/invalid values, failed-add
atomicity, account persistence, and fresh session identity. A manual checkpoint
is to add a saved world without the flag, check `lobby worlds list`, and enter
it through the lobby to check `whoami`. Repeat with an explicit override on a
compatible profile. These steps do not claim a completed human/live-model test.

Validation passed: 199 tests across catalog, console, state profiles, lobby
controller, audit, session, and presentation-preview suites, plus Ruff lint,
format checks, and `git diff --check`.

## Maintenance

Keep [lobby catalog resolution](../../src/dreamland/lobby_catalog.py), console
help, [SSH Lobby](../SSH_LOBBY.md), and [State Profiles](../STATE_PROFILES.md)
aligned when changing resolution sources or catalog identity behavior.
