# 0114 SSH Handshake Diagnostics

Status: Active
Audience: backend maintainers, deployment operators
Purpose: distinguish expected public-listener probes from internal transport
failures without hiding unrelated errors or changing SSH admission policy.

## Overview

A completed TCP connection does not necessarily start an SSH session. Port
scans, health checks, wrong-protocol clients, and early disconnects can fail
before authentication. Paramiko logs some of these failures, including a
multiline traceback, inside its transport thread before `start_server` raises
the exception to DREAMLAND. Catching that exception only in the application
does not remove the earlier error output.

This change builds on checkpoint `709a206`, retaining its password-check delay,
required lobby username, per-IP connection cap, and authentication-log privacy.
It changes diagnostics, not authentication, protocol parsing, or deployment.

## Details

[ssh_handshake.py](../../src/dreamland/ssh_handshake.py) supplies a small Paramiko
transport subclass. `_check_banner` records the transition from `banner` to
`key_exchange` only after Paramiko accepts the banner. `_log` suppresses only
the corresponding exception heading and traceback for allowlisted initial
handshake failures. Classification uses the active thread's exception, not a
shared flag or a temporary process-wide logger setting. Established-session
errors, including rekey failures, retain their existing handling.

The synchronous `start_server` caller emits one `ssh_handshake_failed` event
on `dreamland.lifecycle`:

| Level | Reason | Meaning |
| --- | --- | --- |
| INFO | `peer_closed` | EOF before initial negotiation completed |
| INFO | `peer_reset` | Reset, aborted connection, broken pipe, or disconnected socket |
| INFO | `timeout` | An explicit timeout exception, including banner-read timeout |
| INFO | `invalid_banner` | Known banner structure/encoding failure or exhausted preamble |
| WARNING | `incompatible_peer` | Unsupported SSH version or no compatible algorithms |
| ERROR | `unexpected_error` | Failure outside the allowlist; traceback retained |

The event includes `connection_id`, `peer_ip`, `peer_port`, `phase`, `reason`,
and monotonic `elapsed_ms` as both log-record fields and readable key/value text.
Lobby connections reuse their parent connection ID; direct connections receive
a diagnostic ID. The peer is the socket source address: the diagnostic does not
infer a public address through NAT or trust an application-provided header.
Normal logging level and `lifecycle` event selection control this output.

Paramiko wraps banner-read exceptions. Classification inspects the underlying
exception so an internal `ValueError`, bad file descriptor, or an unrecognized
SSH exception is not downgraded merely because the wrapper mentions a banner.
Unexpected failures retain Paramiko's output and receive an ERROR lifecycle
event with exception information. This deliberately favors visible errors over
silencing an unfamiliar failure mode.

Known-failure events never include exception text, credentials, or banner bytes.
The subclass also omits raw banner/preamble debug lines and replaces Paramiko's
client-identification INFO line with a fixed message. This is not a general
sanitizer for all library diagnostics; unexpected-error tracebacks and other
existing Paramiko logs retain their normal behavior. The pre-existing auth-line
and connection-reset filters are unchanged.

The application still closes sockets/transports and releases per-IP slots in
its existing `finally` blocks. Failed handshakes do not create simulated
sessions, model calls, or Langfuse session traces. No bans, persistent source
tracking, global rate limit, shortened production deadlines, or sampling are
introduced. An HTTP client that closes during banner reading is reported as
`peer_closed`, not guessed to be HTTP. A key-exchange timer can surface as EOF
in Paramiko and is not claimed to be an explicit `timeout`.

### Validation and operator checkpoint

`tests.test_ssh_handshake` uses real local TCP/Paramiko for immediate close,
orderly EOF, TCP reset, silence/incomplete banners, HTTP/binary input,
malformed/excess preamble, incompatible protocol/algorithms, and disconnect or
stall after an accepted banner.
It checks one failure event, safe fields, no routine ERROR output, socket/slot
cleanup, and no simulated-session creation. Injected internal faults verify
traceback retention, including while a probe and successful authenticated
connection run concurrently. Additional checks verify lobby parent-ID reuse
without entering the lobby, and that failed authentication after successful
negotiation is not reported as a failed handshake. A successful login with a
preamble/client banner containing control characters confirms those bytes do
not leak into DREAMLAND logs. Unit checks cover classification and preservation
of post-handshake error output. Short deadlines are test-only.

Before deployment, run these owning tests plus the existing SSH server, lobby
transport, console integration, authentication, and runtime-logging regressions.
On an explicitly approved disposable or deployed instance:

1. Connect to the DREAMLAND port with a plain TCP client and close it without
   sending an SSH banner. Expect one INFO event, with no error traceback.
2. Attempt an ordinary SSH login and run a harmless simulated command. Confirm
   the usual authentication/lobby/session behavior still works.
3. Check that the logged source address matches the network path and that
   completed probe connections do not retain connection slots.

These checks target the DREAMLAND listener, not administrative OpenSSH. Local
automated evidence is not a claim of production deployment or a load test.

### Local validation checkpoint (2026-09-19)

The 17 handshake tests passed on Paramiko 3.5.1. The combined focused run also
passed all 212 tests across handshake, SSH server/lobby transport, console SSH
integration, authentication, host keys, session/state/streaming, runtime logging,
main, and deployment configuration/storage/model-default/control suites. Ruff
lint/format checks and `git diff --check` passed. Real-socket tests required
execution outside the filesystem/network sandbox; its socket denial was not a
product failure. The full repository suite, live deployment, and load testing
were not run for this checkpoint.

## Maintenance

The adapter depends on Paramiko's private `_check_banner` and `_log` hooks and
its wrapped banner exceptions, verified against 3.5.1. Rerun the owning real
transport tests and inspect these hooks whenever the supported Paramiko version
changes. Do not replace the allowlist with blanket `SSHException` suppression.
Keep the beta logging guidance, test inventory, and this decision aligned.
