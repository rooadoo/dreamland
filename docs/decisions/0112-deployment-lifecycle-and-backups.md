# 0112 Deployment Lifecycle and Backups

Status: Active
Audience: backend maintainers, deployment operators, project owner
Purpose: record the separation between lifecycle and operation tooling, the
backup and rollback boundary, and why promotion replaces a deployment instead
of mutating its profile.

## Overview

Installing and upgrading the backend previously meant running a documented
sequence of Compose and deployment-CLI commands by hand. That sequence was
correct but easy to perform partially: an operator could start a service
without validating it, upgrade without a backup, or lose track of which image a
running deployment came from. Beta puts the backend in front of outside
testers, which makes an interrupted or unrecoverable change costly.

`deploy/dreamland-deploy` wraps that sequence. It owns installation, upgrade,
backup, rollback, world restore, uninstall, and lifecycle status.
`deploy/dreamlandctl` keeps day-to-day operation of a running deployment:
console, logs, status, stop, restart, and model defaults. The split is
deliberate; the two scripts have different blast radius and different
confirmation requirements.

## Details

The lifecycle script adds no deployment authority of its own. It validates
arguments, orders existing operations, and requires confirmation before
anything destructive. Credentials are never passed as arguments or read by the
script: interactive entry stays inside the deployment CLI in a container, and
`configure-ssh --only-missing` requests only the values a deployment has not
stored, so re-running installation is safe and never blanks a stored secret.

A backup is the complete deployment volume, including worlds, catalog,
configuration, SSH host identity, and credentials. A partial backup would not
support the stated goal of resuming or inspecting a point in time. Archives are
therefore owner-only, carry a sidecar manifest naming the project, volume, and
image they came from, and must be treated as secret material. `upgrade`,
`rollback --backup`, and `uninstall` take their own backup first unless the
operator opts out, and no command in this tooling ever deletes a backup or the
package it was installed from.

Rollback is two distinct operations. `--image` returns to a previously built
image and does not touch deployment data, which is the right response to a bad
build. `--backup` replaces the entire volume and is the right response to bad
data or a failed migration. Keeping them separate prevents an operator from
discarding good session state while trying to undo a code change. Volume
restore clears the volume before extracting so a restored point in time cannot
merge with unrelated newer files, and it is always preceded by its own backup.

Backup and restore need read and write access to an owner-only volume, so they
run in a one-shot container as root. That container has no network, no new
privileges, no published port, and mounts only the volume and the backup
directory, with the backup directory read-only on restore paths. Operator
supplied backup names reach a shell inside that container, so they are
restricted to a safe character set rather than trusted.

There is no profile-change command. Profile is coupled to ban policy and to
tracing, and mutating it would require a transition matrix, coordinated edits
across two stored files, and a decision about credentials that no longer apply.
Promotion happens once per deployment lifetime, so it is a fresh installation
plus `restore-worlds`, which copies only the lobby catalog, provenance, and
world profiles. It deliberately excludes credentials, deployment policy,
runtime configuration, SSH host identity, and the live visit registry, so a
promoted deployment cannot silently inherit a password that testers already
know.

The tooling assumes one deployment per host. `--project` is accepted and
validated so a second deployment is possible, but no discovery or ambiguity
handling is provided, and none is needed at this checkpoint.

## Maintenance

Keep this decision aligned with [`deploy/dreamland-deploy`](../../deploy/dreamland-deploy),
the [beta deployment plan](../BETA_SSH_DEPLOYMENT_PLAN.md), and
[Operations](../OPERATIONS.md#docker-deployment). Record a new decision before
adding a profile-change path, before narrowing what a backup contains, or
before any lifecycle command gains the ability to delete a backup. Phase 4
release manifests, registry delivery, and drain handling remain later work and
are not superseded by this tooling.
