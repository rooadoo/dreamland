# 0113 SSH Guessing Stopgap Controls

Status: Active
Audience: backend maintainers, deployment operators, project owner
Purpose: record the interim password-guessing controls added for beta
exposure, what they do not provide, and why they are not the Phase 3 bans.

## Overview

Beta publishes the SSH listener to the Internet with one shared password, and
Phase 3 failure bans are not implemented. Before these controls, an attacker
could attempt passwords as fast as the network allowed, vary the username
freely with no product consequence, and open as many concurrent connections as
the container's limits permitted, with nothing in the backend slowing them
down or noticing.

Three deliberately simple controls close the worst of that gap without
building ban infrastructure: a fixed delay before every credential check, a
required username in lobby mode, and a cap on concurrent connections from one
source address. All three are per-process and hold no history, so they are
mitigations, not enforcement. Real failure bans with counting, expiry, and
manual unban remain Phase 3 work; see the
[beta deployment plan](../BETA_SSH_DEPLOYMENT_PLAN.md#security-posture).

Paramiko's own per-attempt auth line is deliberately not used for visibility:
its username is attacker-controlled and unsanitized, so logging it verbatim
would allow log or terminal injection. The owner chose to keep it filtered
rather than sanitize and keep it; there is currently no per-attempt
authentication log as a result.

## Details

`SSHServerConfig.password_check_delay_seconds` defaults to two seconds and is
applied in `check_auth_password` before the credential is checked. The delay is
unconditional, so a correct and an incorrect password cost the same and the
delay cannot be used as an oracle for whether a password was accepted. The
existing constant-time comparison in
[ssh_auth.py](../../src/dreamland/ssh_auth.py) is unchanged. `_ParamikoServer`
defaults this delay to zero, so unit construction stays fast and only the
transport applies the configured deployment value.

Paramiko independently disconnects a connection after ten failed attempts on
that connection. That threshold is library behavior, not a project setting, and
the owner accepted it rather than lowering it. Combined with the delay, one
connection therefore costs roughly twenty seconds for ten guesses.

`SSHServerConfig.max_connections_per_ip` defaults to five and is enforced in
the accept loop. An over-limit source has its socket closed before any SSH
negotiation or worker thread exists, so a flood cannot consume those resources,
and the refusal is logged with the peer, active count, and limit. Counts are
held in a map guarded by the existing client-thread lock and entries are
removed at zero, so a rotating attacker cannot grow that map. The cap bounds
concurrency only: an attacker may still reconnect serially, and nothing
remembers a source that has disconnected.

`SharedPasswordAuthenticator.required_username` defaults to `None`, preserving
the existing any-non-empty-username behavior everywhere it is not set.
`src/dreamland/__main__.py` sets it to the literal `dreamland` only when
`ssh.entry_mode == "lobby"`, because lobby mode never uses the transport
username as an in-world identity: the persona comes from the world's own
catalog account (`SSHLobby.run` receives it only for one audit log line). In
`direct` mode the transport username becomes the session persona, so it stays
operator-chosen and unrestricted there. The comparison provides no secrecy of
its own; `dreamland` must be documented for testers to connect, and its value
must never be treated as a second secret. `authenticate_password` always
hashes the supplied password even when the username does not match, so its own
cost does not depend on which check failed; the caller's fixed delay already
dominates any such difference, but the method does not rely on that for
correctness.

These controls assume the listener observes real client addresses. The current
Compose publication preserves them; an added proxy or address-translating layer
would make the cap count the wrong address and needs its own review.

The delay is also a self-denial-of-service consideration: connection handling
already runs one thread per connection, so a delayed attempt occupies its
thread for the duration. The per-IP cap bounds that cost per source, and
container PID limits bound it overall.

Neither control replaces operator attention. Log review and the manual
`DOCKER-USER` block described in the
[beta deployment plan](../BETA_SSH_DEPLOYMENT_PLAN.md) remain the response to a
sustained attack, and the owner's intended per-minute address blocking still
sits outside the application.

## Maintenance

Keep these defaults, the beta plan's security posture, and the Phase 3 ban
design aligned. Replace rather than extend these controls when real failure
bans arrive, and record that transition. Revisit the per-IP cap before putting
any proxy, load balancer, or address translation in front of the listener, and
revisit the delay if connection handling stops using a thread per connection.
