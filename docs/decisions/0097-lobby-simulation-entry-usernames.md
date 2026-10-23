# 0097 Lobby Simulation Entry Usernames

Status: Active
Audience: maintainers, DREAMLAND operators, profile authors
Purpose: record explicit per-world lobby entry accounts and separate real SSH username auditing.

## Overview

A curated world establishes a starting host, role, and account. Seeding that
world with an arbitrary connecting SSH name can produce a shell identity and
home directory that conflict with its authored users and scenario. The operator
approved assigning a static expected account per listed simulation and logging
the original SSH username, affecting lobby mode only.

This refines the fresh-visit design in
[decision 0093](0093-native-ssh-lobby-visits.md) and supports the
[project model's](../PROJECT_MODEL.md) requirement for coherent entry into an
LLM-emulated network. No reusable behavior prompt needs a scenario account.

## Details

### Explicit catalog configuration

The operator-owned lobby catalog controls launch configuration, so catalog
version 2 requires `entry_username` on every world entry. It is a literal ASCII
account name matching `[A-Za-z_][A-Za-z0-9_.-]{0,63}`. The local console originally
required `--entry-username` when adding an entry.
[Decision 0119](0119-optional-lobby-entry-username.md) makes it optional by
resolving the saved profile's starting account at add time. The console accepts
it when updating an entry and shows it in `lobby worlds list`. The account is operator configuration, not
attendee input or a name inferred from profile prose.

The bundled catalog assigns `operator` to `lobby-supermarket`, `reader` to
`lobby-colonyship`, and `leah` to `lobby-offshore`, matching their authored
starting accounts. Source world-profile facts remain unchanged. This requires
no general state-profile schema field and leaves other entry surfaces alone.

Version-1 catalogs must be explicitly edited offline: add the intended account
to every entry and set `schema_version` to `2`. An empty old catalog also needs
the version update. Startup rejects old versions, missing accounts, and invalid
values; an invalid reload preserves the current published catalog. Automatic
migration cannot reliably choose the intended role among a world's users, and
a connecting-name fallback would recreate inconsistent entry.

Profile validation boots a detached copy with the configured account and checks
the starting current/login/shell identity, matching `USER` and `LOGNAME`, and an
absolute `HOME` without control characters. A custom absolute home remains
valid. Authored environment overrides must use `{username}` templates or values
consistent with the configured account. Validation checks structural entry
consistency; the operator still chooses the account that fits the scenario.

### Apply identity before session construction

Each selected world supplies its configured account to the existing session
constructor. Both `session.username` and `session.login_username` begin with
that value. Profile templates, initial shell frame, environment defaults, and
model context therefore agree before the first command. This avoids a later
username patch that leaves booted state or model context using the original
connection name.

Every selection, including repeated entry on one connection, applies the
selected catalog account to a fresh session. Catalog changes affect future
selections and preserve active visits. Live profile application retains that
visit's simulated login identity; simulated account changes and nested SSH keep
their normal behavior. Direct SSH shell/exec, API, MCP, and offline profile
loading retain their existing identity rules.

### Preserve connection identity in audit data

After successful lobby authentication, `dreamland.lifecycle` logs the exact
supplied SSH username before sanitization using Python's escaped quoted
representation. This preserves punctuation and Unicode while containing log
line breaks and terminal controls. It records a visitor-supplied name rather
than a verified personal identity. Existing authentication policy is unchanged.

A parent connection reference is allocated before authentication and carried
into the lobby. Authentication/controller records contain that reference and
the original SSH name; visit records correlate it with the selected profile,
configured entry account, public simulation ID, and internal session ID.
The real transport's close record carries the same parent reference.

These events use existing configurable lifecycle logging at INFO. The raw SSH
name stays in transport/controller memory and audit messages; it is not added
to simulation state, saved profiles, model context, or trace metadata. The
public-reference database remains a visit index, so retaining lifecycle logs
is necessary to retain the original-name association.

### Verification

Provider-free owning tests cover catalog/console persistence and rejection,
identity and environment initialization, multiple visitors, switching and
reentry, exact escaped name logging, failed-authentication exclusion, parent
correlation, and the absence of the raw name from simulated context. Direct
entry regressions preserve the existing surface boundary. Real SSH tests cover
transport authentication and connection dispatch only where those behaviors
require it.

[Manual operator checkpoints](../SSH_LOBBY.md#manual-operator-checkpoints)
describe arbitrary-name entry, per-world account/home checks, fresh reentry,
log correlation, schema migration, and direct-mode checks. The instructions are
a walkthrough procedure, not a claim that a human has completed those checks.

On 2026-09-11, the operator reported the change was "working great" and
authorized committing, merging, and pushing it. This records acceptance of the
observed behavior; individual checklist cases were not separately reported.

## Maintenance

Keep [`lobby_catalog.py`](../../src/dreamland/lobby_catalog.py), the native
console, SSH authentication/transport, [`ssh_lobby.py`](../../src/dreamland/ssh_lobby.py),
the bundled catalog, [SSH Lobby](../SSH_LOBBY.md),
[State Profiles](../STATE_PROFILES.md), and [Testing](../TESTING.md) aligned when
changing catalog identity, validation, session initialization, or audit fields.
The implementation is tracked by the commit introducing this decision.
