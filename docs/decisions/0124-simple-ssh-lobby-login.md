# Simple SSH Lobby Login

Status: Active
Audience: maintainers, DREAMLAND operators, terminal testers
Purpose: record the per-connection static, monochrome SSH lobby option and its
authentication and compatibility boundaries.

## Overview

Some attendee SSH terminals do not handle the animated lobby introduction well.
The operator requested an opt-in username variant that skips animation and
disables color, named `_simple`. This implements an accessibility and terminal
compatibility option for the current SSH entry point in the
[project model](../PROJECT_MODEL.md).

## Details

- The standard lobby accepts `dreamland_simple` alongside `dreamland`, using
  the same configured shared password. The suffix is exact and case-sensitive.
- `SharedPasswordAuthenticator.allow_simple_login` defaults to false. Standard
  startup enables it only for lobby mode, preserving the authenticator protocol
  and direct-entry username behavior. An enabled authenticator accepts its exact
  required username or that name with one appended `_simple`; it never strips
  repeatedly, trims, or case-folds a login. An exact configured name that already
  ends in `_simple` remains valid. Without a required username, a simple alias
  must have a nonempty base name.
- Both password digest comparisons run per attempt after the optional demo
  credential was added in [decision 0125](0125-demo-ssh-lobby-tier.md). Existing transport
  password-check delays, request restrictions, and admission flow are unchanged.
- The lobby derives a connection-local override from the submitted suffix:
  introduction mode and color are both off. It does not mutate `LobbySettings`,
  so later and concurrent ordinary connections retain the configured behavior.
- Screen clearing, static banner layout, wrapping, paging, agreement, and world
  selection retain their existing settings and behavior. The option does not
  strip terminal controls from world-owned banners or simulated command output.
  It is a static, monochrome lobby option, not a full plain-text terminal mode.
- Transport audit logs preserve the submitted username, including the suffix.
  The presentation lifecycle event adds `simple=True|False`. Catalog entry
  usernames still determine in-world identity, as in
  [decision 0097](0097-lobby-simulation-entry-usernames.md).

Implementation is in [authentication](../../src/dreamland/ssh_auth.py),
[startup](../../src/dreamland/__main__.py), and
[lobby presentation](../../src/dreamland/ssh_lobby.py). No new INI setting or
operator configuration migration is required.

### Validation

Owning tests cover alias authentication, malformed/repeated suffix rejection,
wrong passwords, preservation of configured names already ending in the suffix,
direct entry, startup scoping, and unchanged per-attempt delay/audit behavior.
Controller tests cover welcome/admission, two world visits, refresh, unchanged
in-world identities and saved banners, absence of animation/color controls, and
an ordinary connection sharing the same controller. A real loopback Paramiko
test verifies both login variants on one listener using a capable color TERM.
These tests use deterministic fixtures and make no provider calls.

Human checkpoint on an affected terminal:

1. Connect with `dreamland_simple` and the existing password. Verify an immediate
   monochrome welcome; proceed through guidelines and world selection.
2. Exit a world, refresh the picker, and enter another world. Verify lobby color
   and animation remain disabled and each world retains its configured account.
3. Reconnect as `dreamland`. With animation/color configured to `auto` and a
   supported, sufficiently large terminal, verify the usual presentation.
4. Try a wrong password and `other_simple`; both must be rejected.

Human terminal results must be recorded separately; automated byte-stream tests
do not establish compatibility with every client.

## Maintenance

Update this decision, [SSH Lobby](../SSH_LOBBY.md), and the owning authentication,
startup, controller, and transport tests if suffix resolution, presentation
scope, direct-entry behavior, or required usernames change.
