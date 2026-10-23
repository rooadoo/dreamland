# 0120 Interactive Input and Foreground Authentication Ownership

Status: Active
Audience: backend maintainers, prompt authors, operators
Purpose: explain program continuation, empty input, and foreground authentication
artifact ownership after the October 20 demo-session investigation.

## Overview

Session `DL-UWL2-R234-NJRW-TUAM` exposed a shared failure in sudo and ssh-keygen:
program phase updates without an explicit transition became implicit pushes.
Failure or completion popped one frame and exposed an older prompt. The first
key pair was saved, but the user remained in ssh-keygen. The command prompt also
omitted the saved program phase metadata, making history the model's only phase
evidence. Independent enrichment invented a passphrase-protected client key and
configured SSH to select it before the user generated a key.

## Details

One line-oriented invocation owns one frame. Entering pushes; updating phase,
prompt, filename, or retries replaces; completing pops. Runtime normalization
recovers omitted transitions for the same non-shell invocation and prevents
duplicate authentication/keygen frames. SSH/sudo clients can become a shell in
their existing frame. Explicit nesting of shells and REPLs remains supported.
The compatibility full-stack response path obeys the same continuation rule.
Program prompt and metadata now accompany every model-facing command context.

Blank input has program semantics. The narrow ordinary-shell list handles it
locally without provider cost; unfamiliar programs receive it. Shell continuation
uses explicit input_mode=line state and bypasses shell builtins until command
mode resumes. Transport CRLF normalization precedes editor dispatch so accepting
empty lines cannot accidentally consume a second prompt. This rule applies to
streaming and non-streaming backends, the command graph, and MCP admission.

Secret editor selection covers ssh-keygen/passwd as well as SSH/sudo, while
prompt deduplication applies to all explicit program prompts. Idle-dialogue
Ctrl-C pops once and refreshes the checkpoint. Provider-call cancellation keeps
its existing ownership boundary: a worker still unwinding owns its frame, so
an idle cancellation after cleanup performs the exit. No wait on that worker's
registry lock is introduced in the immediate cancellation path.

Additive enrichment alone does not protect authentication continuity: creating
a previously absent config or encrypted key can change the next command. The
runtime therefore reserves recognized SSH client-authentication paths belonging
to observed foreground homes and explicit key destinations. Per-host path sets
are session-owned, survive history pruning, and are absent from model updates,
checkpoints, and saved profiles. Final apply under the session lock rejects an
affected atomic cluster with foreground_authentication_conflict. Ordinary
foreground commands still create and modify their own artifacts.

This is bounded path protection, complemented by prompt rules against fabricated
foreground setup/verification. It does not prohibit scenario credentials or
unrelated actors' history, nor attempt universal validation of free-text claims.
The POSIX path/option-recognition limits are documented in
[Enrichment Agent](../ENRICHMENT_AGENT.md).

Validation belongs at the parser/backend, terminal, MCP, and enrichment owning
layers. [Testing](../TESTING.md#interactive-input-checkpoints) records the manual
model and terminal checks; live-model outcomes are separate from deterministic
test results.

### October 20 validation

Implementation commit: `fc461c6`.

The broad regression run completed 2,689 tests with eight skips. Its six
evaluation/provenance assertion failures were resolved by the documented
[compatibility audit](../EMULATION_JSON_ANNOTATIONS.md#interactive-input-runtime-compatibility-audit)
and corrected inherited-tier expectations; the complete 113-test evaluation
rerun passed. The final 282-test lifecycle/backend/MCP/terminal/SSH run and
affected enrichment regressions also passed, as did repository Ruff checks.
The standard mypy invocation was blocked by installed dependency stubs requiring
Python 3.12 while project checking targets 3.11; a 3.12 override still reported
132 repository typing errors. Type checking is not a passing validation result.

## Maintenance

Keep this decision aligned with program_input.py, agent_core.py, terminal/SSH
input handling, enrichment authentication scope and final patch application.
Update the ownership and codec rules if persistent shared worlds or additional
authentication artifact families are introduced.
