# 0102 Whole Command Routing and Ordered State Effects

Status: Active
Audience: DREAMLAND maintainers, prompt authors, operators
Purpose: reduce command-model calls without losing deterministic builtin sequencing or parent-shell state.

## Overview

Decision 0024 split every supported command list before builtin dispatch. This
fixed `cd portops; ls` being mistaken for one `cd` invocation, but it also made
`id; w` perform two model calls. The operator approved favoring the original
whole line whenever any command needs the model, while retaining local handling
for fully deterministic builtin lists. This follows the LLM-emulated network
direction in [Project Model](../PROJECT_MODEL.md).

## Details

### Routing before execution

The source-preserving analyzer recognizes simple `;`, `&&`, and `||` lists and
distinguishes them from unsupported grammar. Quoted/escaped separators retain
their literal meaning. Unsupported input never falls through to a local handler
merely because its first token names a builtin.

Every syntactic segment must be a supported local builtin before considering
short-circuiting. If that check succeeds, existing builtin handlers evaluate the
executed segments on detached state. Unknown `cd` targets and unsupported local
forms defer the entire original line. No local prefix is applied, recorded,
published, or sent to an agent before this decision completes. Wrapped external
commands, such as `env ls`, are model-backed. Non-shell input is interpreted by
its active program rather than split using shell rules.

Backend local chains record their executed segments and force the next graph
invocation to refresh authoritative state. Direct graph callers share the same
eligibility rule and apply the combined local result without a model call.
Mixed lines retain their original text, one command history entry, and one
accepted/completed lifecycle. The call-count promise is one foreground model
invocation per mixed line, not a change to provider retry policy or independently
scheduled enrichment.

### Ordered effects in one response

A single final active-frame update cannot preserve `cd /tmp; ssh host`: applying
the directory change only to the pushed frame leaves the hidden parent at its
old directory. Requiring a model to reconstruct the entire hidden stack would
weaken runtime authority and can lose preceding environment changes.

An additive alternative state envelope therefore contains an ordered `effects`
array. Each effect has a `command` attribution and an ordinary `state_update`.
The model still receives one unchanged line and returns one result. Ordinary
final-state responses remain supported. Ordered effects preserve parent changes
before pushes, restores before later changes, and per-transition runtime
connection reconciliation. The model cannot write connection IDs or substitute
the hidden stack inside an effect.

The complete bounded sequence is validated before application. Nested effects,
full-stack effects, mixed envelope forms, and malformed fields are rejected
without accepting a prefix. The 64-effect model limit bounds transition work;
ordinary same-frame changes can be represented by a combined final update.
Source-command attribution supports connection and hostname continuity only;
it is never another instruction for the runtime to execute.

Graph checkpoints and accepted sessions use the shared state application code.
Network allocation belongs to accepted runtime application, not detached
planning or graph normalization. Cancellation discards unaccepted effects.
Transitions, model usage, failures, and routing decisions remain observable
through the existing configurable logs and trace metadata.

### History and observation boundaries

Aggregate output and a final exit status do not reveal which component produced
a line or succeeded. History-derived filesystem hints therefore skip aggregate
entries. Enrichment focus, surfaces, and presence probes use conservative
aggregate hints rather than inventing per-command observations. Ordered world
patches still publish one materialization event for the accepted command.

The model interprets command order, short-circuiting, active-program boundaries,
combined output, and final status. This change does not add a complete local
shell interpreter or per-segment model responses.

### Verification

Owning tests cover original text and call counts, unknown-builtin fallback with
no prefix effects, builtin conditionals and environment dependencies, graph
refresh, malformed sequence rejection, parent cwd/exports after exit, SSH and
hostname connection continuity, history/focus guards, and cancellation.
The manual checkpoint is in [Testing](../TESTING.md#whole-command-routing-checkpoint).

The compatibility audit in [JSON Annotations](../EMULATION_JSON_ANNOTATIONS.md#whole-command-runtime-compatibility-audit)
updates the exact parser fingerprints for this runtime and the preceding main
revision. Ordered transition normalization retains its trace marker; it is not
classified as a JSON syntax repair. This work changes no evaluation policy or
historical scores.

Provider-free validation on 2026-09-11 passed 106 focused runtime/API checks,
116 evaluator/annotation checks, and the final 15 ordered-effects checks after
the normalization-marker correction. Changed Python files pass Ruff lint and
format checks, and changed Markdown file links resolve. A focused mypy check
of routing, backend, SSH exit, focus/surfaces, and admin code passes with the
installed Python 3.12 typing target and imported modules followed silently.
The repository-wide Ruff run also finds an existing import-order error in
unchanged `tests/test_inspect_harness.py`, confirmed against the starting main
revision. Live provider tests remain opt-in; these results establish runtime
invariants with deterministic model substitutes.

The full regression run completed 2,421 tests in 1,267.708 seconds with eight
skips. Its four failures were the outdated JSON-evaluator fingerprint checks
and their two annotation CLI consequences; no other failures occurred. That
run began before the compatibility audit and retained the older allowlist in
memory. The subsequent 116-test evaluator/annotation/CLI run passed with the
audited fingerprints. The final runtime changes also passed the focused checks
above; the full 21-minute suite was not repeated solely for fingerprint updates.

## Maintenance

Keep this decision, [Agent State](../AGENT_STATE.md), the state-contract prompt,
parser, shared planner, graph/backend entrypoints, state codecs, connection
reconciliation, and owning tests aligned when changing command routing or
effect ordering. Preserve older ordinary state-update compatibility.
