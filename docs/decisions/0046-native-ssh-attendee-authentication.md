# 0046 Native SSH Attendee Authentication

Status: Active
Audience: maintainers, DREAMLAND operators, security reviewers, and coding
agents
Purpose: record the native shared-password authentication and restricted
channel policy for DREAMLAND's attendee-facing SSH listener.

## Overview

DREAMLAND's SSH listener previously accepted every password and public key, and
an authenticated SSH session could route `!dreamland` commands into the
operator console. That behavior was useful during early local development, but
it did not provide an enforceable attendee boundary. Putting authentication in
an unbuilt external gateway would also introduce a second SSH termination point
and a possible path around the gateway.

The current listener therefore authenticates attendees directly. One static
password admits users to ordinary simulated shell or exec sessions. A supplied
SSH username is only a session label; it is not a durable account or an
operator identity. SSH authentication never grants DREAMLAND administrative
authority.

## Decision

### Native Shared-Password Authentication

The application requires `DREAMLAND_SSH_PASSWORD` at every startup that opens
the SSH listener. The value is environment-only: there is no INI setting or
command-line argument for the secret. Missing, empty, or whitespace-only values
fail before the listener starts.

Configuration samples the environment once during process startup. The static
password does not reload while the process is running, so rotation consists of
changing the environment secret and restarting DREAMLAND. Effective-config
output reports only whether the password is configured and must never expose
its value.

The listener accepts any nonempty SSH username when the supplied password
matches. It sanitizes that username for use as the simulated login/session
label. This label is untrusted presentation context, not proof of identity:
multiple attendees may choose the same value, and no authorization decision may
depend on it. Sanitization retains letters, digits, underscores, and hyphens,
limits the label to 64 characters, and uses `guest` when no safe characters
remain. Selecting a label such as `root` changes only the simulated session and
grants no DREAMLAND authority.

The server advertises only SSH password authentication. It rejects public-key,
`none`, keyboard-interactive, GSSAPI key-exchange, and GSSAPI-with-MIC
authentication. Password comparison uses a fixed digest and
`hmac.compare_digest`. Presented passwords are not retained or passed into
session state. Credential values and Paramiko's raw authentication-request
records are not written to DREAMLAND logs.

### Restricted SSH Capability

Each authenticated connection may open at most one `session` channel and may
request an ordinary shell or one exec command on that channel. The listener
rejects every other channel or capability currently exposed by Paramiko,
including:

- SFTP and all other subsystem requests;
- local/direct/dynamic and remote/reverse port forwarding;
- forwarded-agent and X11 requests;
- unknown global requests; and
- client environment-variable requests.

Paramiko acknowledges the standard `cancel-tcpip-forward` request regardless of
the server callback. DREAMLAND handles that acknowledgement as a no-op: no
forwarding establishment request can succeed, so there is no listener or route
to cancel.

These denials are transport policy, not simulated command behavior. A rejected
request must not create a DREAMLAND backend session or invoke the command
agent, and a rejection must not destabilize later connections to the listener.

### Operator Boundary

SSH is attendee-only. Shell and exec requests that attempt to invoke the
`!dreamland` operator namespace receive a fixed denial and do not reach either
the command agent or the operator console. The SSH password is never reused as
an operator credential.

Operators continue to use the process-local Dreamland console or the separately
authenticated loopback admin REST/MCP surface. Spiritguide, profile mutation,
logs, traces, session administration, enrichment controls, and shutdown remain
available through those operator surfaces according to their own policy.

## Alternatives Considered

An external SSH gateway was deferred. It would need to terminate SSH, propagate
trusted identity, own host-key presentation, make the backend listener
unreachable directly, and prove that its forwarding path cannot bypass
authorization. Native enforcement is smaller and directly testable for the
current deployment.

Binding the shared password to one configured username was rejected because
conference attendees should be able to choose a useful display label. Treating
that label as identity was also rejected because every attendee shares the same
admission secret.

Public-key or keyboard-interactive fallback was rejected. Advertising or
accepting unused authentication modes widens the public protocol surface and
could allow client-agent configuration to authenticate unexpectedly.

Restricted SSH access to `!dreamland` was rejected in favor of removing the
remote operator path entirely. A shared attendee password cannot safely double
as an administrative credential.

## Consequences

DREAMLAND now has one explicit attendee admission boundary at its native SSH
listener. Invalid credentials and unsupported SSH capabilities fail before a
simulated command session is created. The same policy applies on loopback and
non-loopback binds, so there is no permissive development mode to deploy by
accident.

The shared password controls admission, not individual accountability. It can
be copied, and arbitrary username labels cannot support per-person revocation
or attribution. Deployment still needs rate and concurrency limits, source-
level abuse controls, session and command budgets, monitoring, and an emergency
kill switch. Persistent host-key identity, publication of its fingerprint, and
host-key rotation are separate SSH work.

## Maintenance

Update this decision when SSH authentication methods, secret loading or
rotation, username semantics, channel limits, subsystem/forwarding policy, or
the attendee/operator boundary changes. Keep `README.md`,
`docs/OPERATIONS.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`,
`docs/ENRICHMENT_AGENT.md`, `docs/INDEX.md`, `ROADMAP.md`, the sanitized INI
example, and owning tests aligned.

This decision supersedes only the remote-console portions of decisions 0002
and 0004, the premise in decision 0009 that `!dreamland` remains an SSH
operator escape, and the SSH transport portions of decision 0039. Decision
0009's MCP-side rejection remains in force, as do the earlier layered-test,
local-console, Spiritguide, and local live-status decisions. The historical
files are not rewritten.
