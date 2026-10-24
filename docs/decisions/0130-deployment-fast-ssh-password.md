# Deployment Fast SSH Password

Status: Active
Audience: maintainers and deployment operators
Purpose: make the existing fast SSH login aliases usable in Docker deployments.

## Overview

The SSH runtime already accepts a separate password for `dreamland_fast` and
`dreamland_fast_simple`. The deployment launcher intentionally filters inherited
application environment variables and previously loaded only the normal SSH
password, leaving those aliases disabled even after a container-shell export.

## Details

Store the optional `ssh_fast_password` alongside the existing private deployment
secrets. Reuse bounded, owner-only, no-symlink, atomic secret storage. Load it as
`DREAMLAND_SSH_FAST_PASSWORD` only when launching SSH with credentials. Do not
inherit a fast password from the host/container shell or require one for an
existing deployment. Reinitialization preserves a stored fast password.

`python -m dreamland.deployment configure-ssh-fast --root /var/lib/dreamland`
requires lobby mode, an existing normal password, and a terminal with hidden
input. Confirm the new password and reject blank, invalid, or normal-password
values before changing storage. The command changes only the fast password;
provider credentials, configuration, worlds, and the normal password stay intact.
Normal credential setup also rejects a password equal to the stored fast one.

A host-controlled SSH restart applies a saved password. Exporting a variable in
an interactive shell does not change the running process. The login aliases
remain transport policy, rather than Linux users or world-profile accounts.

## Maintenance

Keep deployment storage, startup handoff, CLI prompts, [SSH Lobby](../SSH_LOBBY.md),
and [Operations](../OPERATIONS.md) aligned. Verify optional-secret preservation,
hidden input, rejected updates, credential isolation, and both fast aliases at
their owning layers; deployed SSH checks remain a separate manual checkpoint.
