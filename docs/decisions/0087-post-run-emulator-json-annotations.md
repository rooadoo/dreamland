# 0087 Post-Run Emulator JSON Annotations

Status: Active
Audience: DREAMLAND maintainers, operators, and evaluation authors
Purpose: record why JSON intervention visibility uses external Langfuse scores and conservative evidence matching.

## Overview

The operator needs deterministic emulator response interventions visible beside
existing Langfuse test evidence. The approved tactical scope covers JSON
structural rejection and existing envelope recovery only, with no additional
work in the interactive input/output path. Spiritguide, enrichment, state
normalization, transition validation, and new repair behavior are excluded.

## Details

### Post-run scores preserve the interaction boundary

The model-generation observation closes before DREAMLAND finishes parsing.
Reopening it would couple annotation work to tracing lifecycle behavior, and
Langfuse's current storage contract does not reliably permit rewriting an
ingested trace or observation. Its supported enrichment mechanism is a score
attached to the existing trace or observation after ingestion. See
[Langfuse update guidance](https://langfuse.com/faq/all/tracing-data-updates).

Use a standalone CLI over captured observations. It runs after the emulator
session and writes categorical trace scores named `dreamland.json_status` only
when evidence supports `repaired` or `rejected`. No foreground parser, callback,
server startup, output buffering, or runtime flush behavior changes. Dry run is
the default; `--apply` is the explicit write operation. Reading or writing old-run
scores does not restart the server or resume a held comparison campaign.

A Langfuse code evaluator can inspect one observation's captured fields, but
cannot independently join a command root to a child generation through network
calls. The external utility supports this correlation and can operate on an
existing export without requiring new evaluator infrastructure. See
[the evaluator context and runtime contract](https://langfuse.com/docs/evaluation/evaluation-methods/code-evaluators).

### Recorded runtime behavior determines the annotation

Require one emulator command root and an unambiguous generation association.
Record incomplete or ambiguous evidence without assigning a score. An explicit
`invalid_json` runtime rejection marker supports `rejected`. Recoveries require
raw and processed evidence plus verification of historical `agent_structured.py`,
`agent_core.py`, and `agent_state.py` source against supported fingerprints.
All three must match so extraction and output coercions are verified together.
Never infer runtime behavior from the
current checkout alone or execute arbitrary historical source to classify it.

The audited parser unwraps complete outer JSON fences and extracts existing
complete trailing envelopes. It does not repair missing braces or other malformed
JSON syntax. Therefore `repaired` means evidenced envelope recovery, not a
successful semantic correction. Syntax and schema failures stay separate, and
missing annotations stay distinct from a measured zero.

A requested session with no observations fails the fetch before any writes,
including in a multi-session selection. Exhausted pagination alone cannot
establish complete ingestion; whole-run coverage requires comparison with the
original run's expected command and generation IDs.

### Extensible metadata without replacing existing evidence

Use a stable score ID, name, and original timestamp for reruns. Tool-owned
provenance lives under `dreamland.evaluation` with schema and policy versions,
metric, evidence method, rule events, runtime revision, and observation IDs.
Caller data lives under `dreamland.evaluation.annotations`. Future metrics receive separate
`dreamland.*` score names rather than repurposing JSON status or replacing whole
trace metadata. Unrelated scores and original observation records remain intact.
Policy versions have separate score identities, and writes report ingestion
acceptance rather than promising immediate read visibility.

The implementation lives in
[evaluation_json.py](../../src/dreamland/evaluation_json.py),
[evaluation_annotations.py](../../src/dreamland/evaluation_annotations.py), and
[emulation_json_annotations.py](../../tools/emulation_json_annotations.py).
Provider-free tests own evidence classification, transport behavior, write
gating, and score identity. No live result is implied by those tests.

## Maintenance

Update this decision when annotation placement, evidence rules, policy version,
or the interactive-path boundary changes. Keep the operator contract in
[Post-Run Emulator JSON Annotations](../EMULATION_JSON_ANNOTATIONS.md) aligned
with the implementation. The initial implementation is committed together with
the completed comparison and its evaluation workflow; use this file's Git
history to locate that delivery. Later annotation changes must preserve frozen
campaign records and distinguish their original evidence from new policy results.
