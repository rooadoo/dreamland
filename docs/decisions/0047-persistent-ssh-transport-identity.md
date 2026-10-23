# 0047 Persistent SSH Transport Identity

Status: Active
Audience: maintainers, DREAMLAND operators, security reviewers, and coding
agents
Purpose: record DREAMLAND's secure, persistent host-local SSH endpoint
identity and its operational boundary.

## Overview

An SSH endpoint is identified by its host key. DREAMLAND previously accepted an
optional key path at the transport layer, but ordinary application startup did
not supply one. A missing path therefore produced a fresh in-memory RSA key on
each process start. Clients could not distinguish an expected restart from a
different or intercepted endpoint.

The real SSH endpoint identity is infrastructure state. It is separate from
the simulated hostname, entry host, host registry, keys, and fingerprints that
may appear inside a DREAMLAND state profile.

## Decision

### Dedicated Host-Local Key

DREAMLAND owns a dedicated RSA-2048 host key. It does not reuse the host
operating system's `/etc/ssh/ssh_host_*` identity and does not store its private
key in the repository, INI file, image layer, state profile, or presentation
artifact.

Normal startup always resolves a durable path. Configuration precedence is:

1. absolute `DREAMLAND_SSH_HOST_KEY_PATH`;
2. `[ssh] host_key_path`, with relative values resolved against the active INI
   file's directory;
3. `$XDG_STATE_HOME/dreamland/ssh_host_rsa_key`; or
4. `~/.local/state/dreamland/ssh_host_rsa_key` when `XDG_STATE_HOME` is unset.

The service account plus configured path defines the local endpoint identity.
Changing the account, state directory, volume, or path intentionally selects a
different identity unless the original key is provisioned there.

RSA-2048 retains the transport's existing algorithm for this bounded
persistence change. Multiple host-key algorithms, Ed25519 migration, encrypted
private keys, SSH host certificates, and host CAs require separate design.

### Generate Once And Reuse

On first use, DREAMLAND creates an application-owned private directory and one
same-directory candidate key. On POSIX, it explicitly assigns directory mode
`0700` and key mode `0600`, flushes the candidate, and atomically publishes it
without overwriting an existing path. Concurrent creators converge on the one
published winner; every process reloads that canonical file before serving it.

Later starts load the existing key without rewriting its bytes, permissions,
or modification time. The lifecycle log reports generated versus loaded,
path, algorithm, and the public OpenSSH SHA-256 fingerprint. Private material
is never logged.

### Fail-Closed Validation

DREAMLAND validates the parent directory and private key through already-open
file descriptors rather than following the pathname again. It rejects unsafe
directory ownership or write permissions, symlinks, FIFOs and other
non-regular files, unsafe key ownership or modes, malformed or encrypted keys,
and RSA keys smaller than 2048 bits. It does not silently repair, replace, or
rotate an existing key and does not fall back to an ephemeral identity.

The secure first-run path depends on POSIX directory-relative open/link
primitives and directory durability sync. Unsupported filesystems or platforms
fail with a clear error instead of using a weaker creation path. A future
Windows implementation needs an equivalent ACL and atomic-publication design.

### Operational Boundary

The reported fingerprint is local observability, not public publication.
Deployment owns backing up and restoring the private key securely, persisting
its path across container or image replacement, and rehearsing intentional
rotation. Operators and canaries use the fingerprint to recognize unexpected
identity changes; attendee or presentation publication is not required.

There is no automatic rotation. Operators stop the listener, deliberately
replace or remove the key, start and verify the new identity, update canaries
and operator records, and only then expose the endpoint. An unexpected
fingerprint change remains a security signal.

Multiple replicas behind one endpoint must receive the same deliberately
provisioned key. Independently generated replica identities require distinct
endpoints; otherwise clients will see intermittent host-key failures.

## Alternatives Considered

Per-process ephemeral keys were rejected because they make every restart look
like endpoint replacement. Reusing the system SSH daemon's key was rejected
because DREAMLAND is a separate service with an independent trust and rotation
boundary. Storing a key in the repository, config file, or image was rejected
because it would distribute private identity material.

Writing directly to the final path, checking existence before writing, and
renaming over the final path were rejected because concurrent startup or a
crash could truncate or replace an established identity. Silent permission
repair or regeneration was rejected because it hides security and operational
failures.

## Consequences

Clients can pin one fingerprint and reconnect safely across ordinary process
restarts on the same service account and state storage. Startup now has a
deliberate secure-storage dependency and may fail when the configured directory
or filesystem cannot satisfy it. Deployment must preserve the key separately
from source code and ephemeral containers.

The application exposes the fingerprint through the SSH server and lifecycle
log. Deployment backup and rotation rehearsal remain operational work, but the
documented procedures satisfy the implementation acceptance boundary.

## Maintenance

Implementation: `1073e43` (`feat: persist SSH host identity`).

Update this decision when key algorithms, path precedence, platform support,
filesystem requirements, fingerprint reporting, backup, rotation, replicas,
SSH certificates, or host CA behavior changes. Keep `README.md`,
`docs/OPERATIONS.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`, `docs/INDEX.md`,
`ROADMAP.md`, `dreamland.example.ini`, and the owning tests aligned.
