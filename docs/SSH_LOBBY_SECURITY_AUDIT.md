# SSH Lobby Security Assessment

Status: Active
Audience: DREAMLAND maintainers, conference operators, development agents
Purpose: retain independent review, deliberate attack, fix, and retest evidence
for the optional native SSH lobby.

## Overview

An independent Codex review agent, `/root/security_review`, reviewed the
implementation on 2026-09-11. This agent did not implement the feature or its
fixes. It owned only the additional
[audit tests](../tests/test_ssh_lobby_audit.py) and this record, and reported
findings to the implementing agent for correction.

The review found two defects in cancellation and public catalog presentation.
Both were corrected and independently retested. No unresolved feature security
defect was identified in the reviewed surfaces. The tests and review provide
bounded evidence, not proof of absolute security or a public deployment load
assessment. The deployed Langfuse API was checked with disposable operator
activity; visual confirmation of the deployed UI filter remains a manual
checkpoint.

The scope follows the mandatory matrix in the
[development plan](SSH_LOBBY_DEVELOPMENT_PLAN.md). Native SSH and backend
implementation were reviewed; no web UI or public reference lookup API was
introduced.

## Details

### Reviewed revision and method

- Branch: `feature/ssh-lobby`.
- Implementation commit: `8fdc86b` (`feat: add optional native SSH lobby and isolated visits`).
- Base commit: `565f3f2897ed5492a8bd5e26b49671ef07922c37`.
- Reviewed implementation: the feature working tree based on that commit,
  including the fixes below.
- Source, test, and configuration diff SHA-256:
  `46fb0a3a05968c830979a6ac4f8f0696a42a95db04f85be050959ad3c258f0ce`.
- Digest scope: `src/dreamland/`, `tests/`, `pyproject.toml`, and
  `dreamland.example.ini`. Documentation is excluded to avoid a self-referential
  fingerprint. Include every changed/new file in lexical path order. For files
  present at the base, concatenate the bytes from
  `git diff --no-ext-diff --no-color --binary BASE -- PATH`; for new files,
  concatenate `git diff --no-ext-diff --no-color --binary --no-index /dev/null PATH`.
  Hash the concatenation. This includes new files even before staging.

The reviewer read the complete new lobby controller, input parser, catalog,
identity index, packaged text, and their tests. It inspected the integration
changes in configuration/startup, transport/authentication, shell lifecycle,
native console resolution, profile codecs, runtime provenance, command tracing,
Spiritguide, session registration, checkpoint retirement, and enrichment
scheduling/application. It also reviewed the narrowly added evaluated-runtime
fingerprint entry: the command-agent addition only reclaims completed checkpoint
namespaces, as documented in
[the JSON annotation compatibility audit](EMULATION_JSON_ANNOTATIONS.md#ssh-lobby-runtime-compatibility-audit).

The independent attack suite extends the implementers' owning-layer tests. Its
fixtures create disposable catalog files and profiles; fake agents/channels are
used when the transport itself is not under test. Assertions cover absence of
backend creation, operator execution, model invocation, unintended profile
resolution, unauthorized catalog publication, identity changes, and surviving
live registrations. A separate real Paramiko suite exercises wire behavior.

### Independent findings and retests

| Finding | Before correction | Correction | Independent retest |
| --- | --- | --- | --- |
| LOBBY-01: cancellation swallowed while completing a discarded key | After `discard_boundary(b"\x1b[")`, input `b"\x03Ai agree\n"` returned `i agree`: Ctrl-C was consumed as part of the previous CSI sequence. The shell adapter similarly consumed cancellation inside partial keys. | Check Ctrl-C/Ctrl-D before suppressing pending-key bytes in the lobby. Forward these controls through the shell adapter and reset its pending-key state. | ESC, CSI, SS3, OSC, and incomplete UTF-8 prefixes followed by each cancellation control now quit the lobby immediately. The full welcome-to-agreement probe creates no visit and never reaches the picker. Shell-adapter controls reach the shell. |
| LOBBY-02: public catalog copy could impersonate a controller marker | Ordinary ASCII marker strings passed public-field validation. A sufficiently long description could wrap a marker onto its own line. This was presentation spoofing; it did not change controller state or allocate an ID. | Reject the reserved `[DREAMLAND simulation` prefix case-insensitively in catalog title/description fields before publishing the update. | Both Started and Ended marker payloads fail with catalog file and published snapshot unchanged. Existing control, newline, OSC, and bidi payloads also fail. Fake marker text returned by the simulated agent remains inert: the actual visit stays live and its authoritative ID is unchanged. |

The reviewer also independently retested the implementer's byte limit for
suppressed unterminated OSC sequences. Exceeding the configured input limit
disconnects the lobby, or ends and closes the adapted shell channel, without
reframing the remaining attack as agreement or a selection.

### Mandatory attack and failure matrix

| Surface and deliberate payload/failure | Expected forbidden effect | Observed result and evidence |
| --- | --- | --- |
| Remote exec, shell without PTY, repeated channel/request, SFTP, TCP/agent forwarding, environment override, `!dreamland shutdown` | No admission bypass, runtime session, model call, or operator authority | Independent policy probes assert no backend calls; a rejected request cannot recover through a later PTY/shell request. Real SSH server/auth tests pass all denial paths and preserve password-only authentication and host identity. |
| Agreement and picker prose, `$(touch /tmp/forbidden)`, shell metacharacters, `../../etc/passwd`, `{{i agree}}`, `{{1}}`, operator commands | No evaluation, shell/template execution, profile path resolution, or LLM/console dispatch | Narrow grammar rejects the inputs. Independent mocks assert zero create-session/model/console calls, and invalid nonnumeric picker inputs never call the selection resolver. |
| CR/LF variants, split ESC/CSI/SS3/UTF-8 keys, multiline agreement paste, `exit` plus queued commands and a partial key | No queued input replay into a later phase or visit | Input and lifecycle tests consume known trailing sequences, discard received tails, require a new selection, and keep wire order through a busy-command drain. Acceptance still requires an exact complete phrase. |
| Long/unterminated input, NUL, malformed UTF-8, ANSI, OSC title/clipboard text, bidi formatting | No unbounded input state, crash, terminal control injection, or secret disclosure | Input limits terminate oversized lines and suppressed OSC completions. Independent picker probes do not echo OSC contents, ESC, or NUL. Catalog/content bounds, regular-file/FIFO checks, nested JSON failure, and encoded catalog size tests pass. |
| Inactivity, invalid-input retry exhaustion, disconnect/shutdown at welcome, agreement, and picker; active-world shutdown/EOF | No implicit visit or leaked registration | Independent phase-by-phase probes finish with zero sessions/registrations. Timeout and retry limits close cleanly. Active-world tests distinguish shutdown/disconnect from a world exit and retire the visit. |
| Hostile connecting username, title/description, and exception details containing private paths/secrets | No terminal controls or counterfeit controller state from public dynamic fields | Username remains sanitized and at most 64 characters. Invalid public copy cannot publish. Failed-start/profile messages omit injected secrets and private paths. Reserved catalog marker issue LOBBY-02 is fixed; simulated output does not drive lifecycle. |
| Catalog reorder/update/remove during an open picker, concurrent additions, missing/malformed saved profile, failed atomic replace | No alternate/default world through a stale number; no partial publication | Owning tests show immutable revision snapshots, explicit reselection after changes, serialized additions, unchanged disk/memory on failure, and a single detached validated profile per launch. Independent empty/malformed-profile probe creates no session until the profile is repaired. |
| Forced ID collision, persistent collision, simultaneous allocation/binding | No overwrite or ambiguous binding | SQLite transaction tests preserve the first record, retry collisions, terminate after the bounded retry budget, and bind 32 concurrent allocations to distinct public/internal identities. |
| Same username and profile across simultaneous/fresh visits | No shared authoritative identity or console target | Identity concurrency and lobby round-trip tests show independent IDs. Exact public selection works before a session listing, and public aliases do not appear in `LiveSessionRegistry.get()`. |
| Fake IDs in model state, saved profile metadata, simulated output/files, state patch, and operator profile application | No authoritative rebind or lifecycle event | Codec/provenance tests strip profile-derived projections, preserve the frozen runtime identity through updates, and use captured provenance in tracing. Independent marker-output probe leaves the original registration and mapping live and creates no counterfeit mapping. |
| Unknown, truncated, malformed, expired, or historical ID; index restart with a previously live visit | No fallback to the active/only session or resurrection | Resolver tests reject malformed/unknown references and refuse live mutations for ended IDs without invoking Spiritguide. Retention cleanup removes expired records; restart marks unfinished rows interrupted and does not create runtime sessions. Missing local trace data is reported as unavailable. |
| Visit closure during command, Spiritguide, or enrichment work | No late mutation or output in the picker/replacement visit; no trace identity transfer | Owning tests fence command writers, request cancellation, and defer checkpoint deletion until the command owner finishes. Guidance results are discarded after exact-registration recheck. Queued/running enrichment keeps the originating ID and cannot mutate a replacement registration. |
| Operator closes idle/busy visit by its public ID | No further admitted commands for that visit; no orphan interactive shell | Independent rerun of the real four-visit Paramiko test covers idle/busy close with the same parent channel reused. Owning tests assert stopped intake, retired registration, cancelled control, no late output, Ended status, and return to the picker. |
| Repeated switching, nested shell exit, stalled cancelled provider work | No reused editor/history/checkpoint namespace or growing backlog of obsolete visit payloads | Nested Ctrl-D and simulated SSH exit preserve shell depth until the outer exit. Eight real LangGraph checkpoint reclamation visits pass. Pending command ownership is bounded per parent; enrichment queue saturation tests release cancelled session payloads and preserve queue bounds. |
| Langfuse disabled, failing, delayed, or completing after closure | No dependence of local identity/console admission on external trace availability; no wrong-visit metadata | Disabled/unavailable tracing tests preserve local mappings and report tracing failures. Captured-provenance tests retain originating IDs after scheduling/closure. Controlled live evidence below verifies all observation classes and metadata filtering on the deployed service. |
| Direct mode regression and notice/provider mismatch | No lobby/public banners or loss of private direct exec; no undisclosed non-OpenAI processing admitted by lobby composition | Real direct shell/exec tests pass. Independent composition tests return `None` before lobby filesystem effects in direct mode, accept an all-OpenAI lobby, reject each non-OpenAI active role before catalog/index creation, and ignore disabled enrichment roles. |

### Test execution evidence

The independent reviewer executed these commands from the feature checkout:

```sh
env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest -v \
  tests.test_ssh_lobby_audit tests.test_lobby_catalog tests.test_lobby_config \
  tests.test_lobby_content tests.test_lobby_identity tests.test_lobby_input \
  tests.test_ssh_lobby tests.test_ssh_lobby_sessions tests.test_session_registry \
  tests.test_enrichment_service

env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest -v \
  tests.test_ssh_lobby_transport tests.test_ssh_server tests.test_ssh_auth

env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest -v \
  tests.test_ssh_lobby_audit

.venv/bin/ruff check tests/test_ssh_lobby_audit.py
.venv/bin/ruff format --check tests/test_ssh_lobby_audit.py
```

The first command passed 165 tests in 5.719 seconds when the independent module
contained its initial 11 tests. The transport/authentication command passed all
35 tests in 38.342 seconds, including the real four-visit lobby round trip and
existing direct-mode behavior. The final independent suite adds acceptance
version stability, unavailable-world recovery, and startup/provider checks;
all 16 final independent tests passed in 1.200 seconds. Ruff lint and formatting
checks passed for the independent audit module, and `git diff --check` passed.

The implementer also checked every changed Python file together: all 34 files
passed Ruff lint and formatting checks. A scoped Python 3.12 mypy run passed for
the six new lobby/composition modules. Repository-wide Ruff still reports the
two existing import-order findings in untouched `agent_shell.py` and
`test_inspect_harness.py`. The configured Python 3.11 whole-project mypy run
cannot parse the installed NumPy Python 3.12 stubs; a Python 3.12 whole-project
run also reports existing typing issues across the repository. Those broad
static checks are not claimed as passing.

The final complete regression run used `.venv/bin/python -m unittest --buffer`
and completed **2,142 tests in 1,168.441 seconds, with 8 skipped and no failures**.
The earlier full run exposed an outdated queued-closure expectation and the
required evaluated-runtime source fingerprint update; both were corrected and
passed this final run. The reviewed source/test/configuration digest above was
recomputed and matched before staging the feature. The
[OpenSSH rehearsal](SSH_LOBBY_REHEARSAL.md) records the implementer's separate
manual terminal and local-console checkpoint.

### Limits and remaining operator checkpoint

Before public release, repeat the [tracing check](SSH_LOBBY_TRACE_CHECK.md)
with disposable operator activity. Verify exact metadata filtering and UI
navigation for command, Spiritguide, enrichment, and lifecycle observations.

Raw SSH cannot distinguish delayed scripted/pasted bytes from fresh typing.
Buffered tails and known partial controls are consumed; agreement is an explicit
input requirement, not a proof that a person read the notice. Application
markers are not authentication tokens and simulated output can resemble them;
only trusted controller events change the real lifecycle.

Cancellation cannot guarantee immediate termination of an upstream provider
call. Output/application fences and bounded pending work protect subsequent
visits; the retiring owner releases its checkpoint after it finishes. The tests
do not establish host-wide capacity under many simultaneous real connections,
cover every terminal emulator, or audit the external model/tracing services.
The local catalog/index and custom notice files remain operator-owned resources.
The publication commitment applies to attendee activity; these integration
artifacts use disposable operator activity only.

## Maintenance

Reassess this record when lobby input grammars, deterministic text rendering,
catalog publication, native console resolution, provider disclosure, trusted
identity, shutdown/close ownership, checkpoint cleanup, or tracing propagation
change. Record the next reviewed revision, new payloads, observed forbidden-effect
assertions, findings, fixes, and retests. Link the final feature commit after
commit creation without changing the reviewed source digest.
