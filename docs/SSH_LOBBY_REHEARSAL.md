# SSH Lobby Terminal Rehearsal

Status: Active
Audience: maintainers, operators, release reviewers
Purpose: record the implementation's manual terminal checkpoint and remaining deployment checks.

## Overview

The implementing agent exercised the feature through the system OpenSSH client
and a loopback DREAMLAND server on 2026-09-11, finishing at 02:04 UTC. Both the
client and local operator console were controlled through real PTYs. The server
used the actual lobby, catalog, public-reference index, shell, and deterministic
backend with disposable operator-created profiles. This rehearsal made no model
calls and did not use attendee activity.
The rehearsed implementation was subsequently committed as `8fdc86b`.

The [independent security assessment](SSH_LOBBY_SECURITY_AUDIT.md) and
[controlled live trace check](SSH_LOBBY_TRACE_CHECK.md) cover the complementary
adversarial and actual provider/tracing cases.

## Details

### Setup and observations

The local server used `entry_mode=lobby`, a disposable shared test password,
new temporary persistent host key, empty catalog, and two saved world profiles.
The terminal ran OpenSSH with forced PTY allocation. A disposable askpass helper
supplied its test password; earlier interactive attempts expired before a
password could be entered through the tool boundary. No authentication policy
or timeout was changed for this rehearsal.

| Checkpoint | Observed result |
| --- | --- |
| Add profiles `alpha` and `beta`, edit `alpha` title, order `beta alpha`, and list through the local console | Each update persisted; ordered public titles were `Rehearsal Two` and `After Hours`. |
| Read welcome and page through disclosure on an ordinary terminal | `DREAMLAND`, fresh-session guidance, privacy/non-publication notice, and report contact appeared with paging before agreement. No tagline appeared. |
| Submit `yes` at agreement | Exact agreement prompt repeated; no world started. |
| Submit `i agree`, then `../alpha` at picker | Agreement accepted; path input rejected and numbered menu repeated. |
| Select world 1, run `whoami`, inspect `sessions --live` | Fresh shell used `rehearsal`; native listing showed the same public ID as Started and profile `beta:world`. |
| Enter `exit` | Matching Ended marker and report contact appeared; picker returned on the same SSH channel with no repeated agreement. |
| Remove/re-add `alpha`, reload catalog, and select `R` from already-connected picker | Changes were visible without listener restart or parent reconnection. Source profiles were retained. |
| Select world 2; set console target using its public ID | New public/internal identity and an `alpha` visit were selected exactly before any new session listing. |
| Inspect the ended first reference and attempt Spiritguide against it | Console reported ended identity and unavailable local trace buffer honestly; live guidance failed explicitly as ended. |
| Close world 2 using `sessions --close PUBLIC_ID` | Ended marker appeared immediately in the attendee terminal, followed by the picker. |
| Select world 2 again; then Ctrl-D and Q | Another distinct public/internal identity was created. Ctrl-D returned to the picker; Q closed OpenSSH normally. |
| Reopen catalog in a separate Python process | Saved order/titles were recovered exactly: `beta` / `Rehearsal Two` / 1 and `alpha` / `After Hours` / 2. |

Public references observed during the three visits were:

- `DL-QRPC-MGNH-XX64-TJZK` (`beta`, normal exit).
- `DL-R7UJ-DZLJ-EVEN-UN2V` (`alpha`, operator close).
- `DL-HAKF-2E2Y-ZQCJ-EEN2` (`alpha`, Ctrl-D).

The server was stopped after the rehearsal and its disposable catalog,
profiles, host key, and index were removed by fixture cleanup. These local
references are rehearsal evidence, not retained operator lookup promises.
No transcript bodies are stored in this record.

### Complementary automated and live evidence

The actual Paramiko integration test covers nested SSH, repeated visits on one
channel, invalid/pasted admission input, idle and busy public-ID operator close,
new visits while an old provider is blocked, and suppressed late output. Owning
tests cover profile/catalog persistence across reconstruction, notice-version
stability, exact reference resolution, resource bounds, and provider-free direct
regressions. The controlled live smoke verifies the deployed Langfuse API filter
and originating public ID on all 45 observations.

### Remaining deployment checkpoint

Visually apply `dreamlandPublicId = DL-7E7N-XSEB-KTC4-ASNV` in the deployed
Langfuse observation interface and verify lifecycle, command, Spiritguide, and
enrichment observations are navigable. The authenticated API returned that
exact correlated set; no authenticated browser session was available for a
visual UI rehearsal. This is an operator release checkpoint, not a claim that
web UI verification occurred here.

Supply the actual curated worlds and deployment paths before public operation.
The implementation does not authorize a public deployment, expand processors,
or change the attendee non-publication commitment.

## Maintenance

Repeat the terminal and trace checkpoints when deterministic copy, paging,
input boundaries, curation, parent/visit lifecycle, public selectors, or external
tracing changes. Use fresh disposable operator activity and record the tested
revision, evidence, failures, and remaining checkpoints.
