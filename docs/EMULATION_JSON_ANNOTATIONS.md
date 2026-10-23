# Post-Run Emulator JSON Annotations

Status: Active
Audience: evaluation authors, DREAMLAND operators, and maintainers
Purpose: make emulator JSON recovery and rejection visible in Langfuse without adding work to interactive commands.

## Overview

[emulation_json_annotations.py](../tools/emulation_json_annotations.py) reviews
completed emulator command observations and adds categorical Langfuse scores
named `dreamland.json_status`. It uses existing captured evidence after the run;
it adds no model calls, command callbacks, network requests, flushes, or waits to
DREAMLAND's input/output path. The server needs no restart for this tool.

The values `repaired` and `rejected` describe JSON handling only. They are scores,
visible through Langfuse's score surfaces, rather than mutations of the native
trace Status, tags, or metadata. Langfuse supports attaching scores after a trace
has been ingested; its current data model does not support reliably rewriting
ingested traces or observations. See [Langfuse's update guidance](https://langfuse.com/faq/all/tracing-data-updates)
and [Scores via API/SDK](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk).

## Details

### Scope and evidence

The tool selects emulator command roots and requires an unambiguous associated
model generation. Spiritguide, enrichment, built-ins, incomplete records, and
ambiguous or multiple-generation associations do not receive JSON scores.
Report assessments are `repaired`, `rejected`, `none_observed`, `unknown`, and
`out_of_scope`; coverage and missing-evidence reasons remain in the report.
A missing score
does not mean that a response was accepted or that no intervention occurred.

| Value | Evidence required | Meaning |
| --- | --- | --- |
| `rejected` | Explicit processed-result metadata: `structured_response_accepted=false` and `dreamland.structured.failure_class=invalid_json` | DREAMLAND rejected the JSON structure. This does not establish that no terminal text was streamed. |
| `repaired` | Raw generation, processed result, and a verified parser revision establish accepted JSON envelope recovery | DREAMLAND recovered an existing complete envelope by removing an outer JSON fence or extracting trailing JSON. This is an inferred structural intervention, not a judgment of semantic correctness. |
| No score | No qualifying intervention, unsupported evidence, or an excluded route | Consult report coverage to distinguish a reviewed ordinary response from unknown evidence. |

The audited [parser](../src/dreamland/agent_structured.py) does not add missing
braces, fix commas, close strings, or retry malformed JSON. It unwraps complete
outer fences, can extract an existing complete trailing envelope, and then uses
strict JSON decoding. Malformed JSON alone therefore cannot justify `repaired`.
Whitespace trimming and ordinary output rendering are not repairs in this policy.
Schema errors such as missing required fields or a non-object `state_update`
remain outside this metric even when the JSON itself parses.

The required `--runtime-revision` identifies the recorded runtime Git revision
for source verification. The operator attests that this was the running code;
source verification cannot independently establish the server's revision.
The tool checks the source digests of `agent_structured.py`, `agent_core.py`,
and `agent_state.py` at that revision before inferring recovery. All three must
match supported fingerprints to verify the extraction and output-coercion
behavior; the current checkout is not evidence of what a past server ran.
It reads historical source
for comparison rather than executing arbitrary code from that revision. Explicit
runtime JSON-rejection markers remain stronger evidence than retrospective
inference. Keep the two evidence methods distinct in analysis.
Use the literal revision `unknown` when the running revision cannot be
established; only explicit runtime rejection evidence can then produce a score.
An explicit JSON-rejection marker can also support a score when raw text is
unavailable, provided the completed root/generation association is still unique.
Raw evidence uses generation output when available, or recorded raw text in the
processed result's failure metadata. `raw_source` records `generation_output`,
`result_metadata`, or `unavailable`; `raw_sha256` is `null` only when both sources
are absent, preserving that evidence gap. Missing root/generation
records or contradictory markers remain unknown; recovery inference always
requires the raw response and matching processed output.

### Running the tool

Run the tool after the selected session has stopped and capture is available.
Default execution is a dry run that writes no Langfuse scores. To inspect one
session:

```sh
.venv/bin/python tools/emulation_json_annotations.py \
  --session-id example-session-id \
  --runtime-revision RECORDED_GIT_REVISION \
  --report /tmp/emulator-json-review.json
```

Repeat `--session-id` to select additional sessions. To use an existing local
export instead of fetching observations, supply `--input observations.json`.
The input shape is `{"observations": [...]}`; preserve complete observation IDs,
trace IDs, parent links, timestamps, input, output, and metadata. This is a reader
for existing capture, not a replacement collector or session driver.
If any requested session returns no observations, fetching fails with exit
status `1`, even if other sessions have data; the tool writes no scores from that
partial selection. Completing pagination does not establish that ingestion has
finished or that every original command was captured. Compare expected command
and generation IDs against the original run evidence before claiming whole-run
coverage.

Live reads and score writes use `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and
`LANGFUSE_BASE_URL` or `LANGFUSE_HOST`. Use environment-based credential handling;
do not place secrets in exported observations, report metadata, command arguments,
or configuration dumps. Local dry runs do not need Langfuse credentials.

`--report` writes the JSON report to the selected file instead of stdout.
The destination is validated and the planned report written before any score
publication; it cannot overwrite the input export or caller-metadata file.
`--log-level` controls tool logging and defaults to `WARNING`. Exit status `0`
means the review completed without unknown coverage, `1` means input, transport,
or publication failed, and `2` means evidence is incomplete or no usable coverage
was found. Review the report even when some annotations were accepted for
ingestion: `--apply` can publish supported scores and still return `2` for gaps
elsewhere in the selected evidence.

After reviewing the proposed scores and coverage, repeat the command with
`--apply` to write the annotations. Historical score writes and campaign
resumption are separate operator actions; installing or validating the tool
does not perform either action. The report contains identifiers, classifications,
and evidence summaries rather than copies of raw prompts or model responses.

### Score and metadata contract

Scores attach to command traces. The supporting root and exact generation IDs
are retained in score metadata. This does not rewrite the completed generation
or extend its recorded duration. The score name is `dreamland.json_status`, its
Langfuse data type is `CATEGORICAL`, and its only written values are `repaired`
and `rejected`.

Tool-owned metadata lives under `dreamland.evaluation` and records schema and
policy versions, metric `json_status`, rule events, evidence method, runtime
revision, and root/generation identifiers. The initial policy is
`emulator-json-v1`. Optional `--metadata-json PATH` accepts additional caller
annotations under `dreamland.evaluation.annotations`; it cannot replace the
tool-owned classification or provenance fields. Keep caller annotations sanitized.

The exact metadata shape is:

```json
{
  "dreamland.evaluation": {
    "schema_version": 1,
    "metric": "json_status",
    "policy_version": "emulator-json-v1",
    "runtime": {
      "revision": "RECORDED_GIT_REVISION",
      "parser_sha256": "SOURCE_DIGEST",
      "source_sha256": {
        "src/dreamland/agent_structured.py": "STRUCTURED_SOURCE_DIGEST",
        "src/dreamland/agent_core.py": "CORE_SOURCE_DIGEST",
        "src/dreamland/agent_state.py": "STATE_SOURCE_DIGEST"
      },
      "parser_verified": true
    },
    "events": [
      {
        "status": "repaired",
        "rule": "markdown_fence_removed",
        "evidence": "reconstructed"
      }
    ],
    "evidence": {
      "trace_id": "TRACE_ID",
      "root_observation_id": "ROOT_ID",
      "generation_id": "GENERATION_ID",
      "raw_source": "generation_output",
      "raw_sha256": "RAW_RESPONSE_DIGEST",
      "processed_result_sha256": "RESULT_DIGEST"
    },
    "annotations": {}
  }
}
```

The score identifier, score name, policy, and original root start timestamp are
stable across reruns of the same annotation. This preserves Langfuse's identity
requirements
for updating a score rather than generating duplicates. Future metrics use
separate `dreamland.*` score names and their own metric metadata. The tool does
not replace whole trace metadata or delete or overwrite unrelated scores.
Different policy versions preserve their own scores. A successful write reports
`accepted_for_ingestion` after checking the ingestion item's acceptance; read
visibility in Langfuse remains eventual.

### Significant handling outside JSON structure

Source review found these existing emulator behaviors. They are capabilities,
not measured counts of events in a campaign:

- [agent_core.py](../src/dreamland/agent_core.py) can synthesize an implicit
  program push or replace an ongoing interactive-program frame, and relocate `cwd`,
  `interactive_program`, and `shell_environment` fields out of transition data.
  These adjustments carry `dreamland.transition.normalized` markers.
- The same module rejects invalid shell-transition objects or operations, with
  `dreamland.transition.accepted=false` and a rejection reason. This can reject
  a transition while other result fields remain usable.
- Legacy full-stack handling can preserve enclosing frames, restore defaults,
  and infer or synchronize user/environment data. These transformations do not
  all have dedicated markers.
- Structured schema validation rejects missing required keys or invalid update
  types. These are contract failures beyond JSON serialization.

These behaviors do not become `dreamland.json_status` events. Historical split command
combinations need component-level evidence: a combined result retaining only
its final component's metadata cannot classify earlier components. Whole-line
model execution instead produces one generation and one response envelope for
the complete input; its JSON assessment belongs to that complete response.

### Verification and campaign use

Validation belongs in provider-free classifier and fake-transport tests. Cover
ordinary accepted JSON, both supported envelope recoveries, malformed JSON,
non-JSON schema/transition failures, ambiguous generations, missing capture,
revision mismatches, dry runs, score identity, and preservation of unrelated
metadata. No live model session or server restart is needed to exercise these
contracts. Verify a dry-run report before any authorized score write.

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest \
  tests.test_evaluation_json tests.test_evaluation_annotations \
  tests.test_emulation_json_annotations
```

This utility changes evaluation annotations only. A retrospective annotation
must retain its policy, evidence method, and verified runtime revision.

### SSH lobby runtime compatibility audit

On 2026-09-11, the SSH lobby implementation added
`LangGraphCommandAgent.release_session(session_id)` to reclaim a retired visit's
checkpoint namespace. Its caller waits for every command owner to finish before
deleting that namespace. It changes neither raw model capture nor structured
response extraction, output coercion, rejection markers, transition handling,
or JSON classification. Completed observation evidence is retained separately
from these transient checkpoints.

The source review compared the complete `agent_core.py` delta with the previous
audited source. Removing only the new method from the current bytes reproduced
the previous SHA-256 exactly:

| Audited source | SHA-256 |
| --- | --- |
| Original `agent_core.py` | `84298e21d62f24557bdf9a3aa81d442b9664bafdfc357ae9fb42e8ef6a60112c` |
| `agent_core.py` with visit retirement | `643d32a12c0561be1c0086407f9e8aabd6e011374cb0e1108315893639276847` |

Both fingerprints remain supported so historical revisions keep their existing
verification. `agent_structured.py` and `agent_state.py` retain their original
audited fingerprints. The `emulator-json-v1` policy and every reconstruction
rule are unchanged; an arbitrary new fingerprint still fails verification.

Codex's SSH lifecycle implementation agent performed this compatibility review.
The provider-free classifier, annotation, and CLI suites named above passed,
including recorded-revision lookup through Git blobs, unknown-revision rejection,
raw/processed-result consistency, and fake-transport publication behavior. The
SSH lifecycle tests separately passed repeated real LangGraph checkpoint
reclamation and deferred reclamation after a cancelled command owner finishes.
This audit permits classification for the additional reviewed runtime source;
it performs no historical score writes and changes no frozen campaign artifacts.

### Whole-command runtime compatibility audit

On 2026-09-11, the whole-command routing change exposed a stale compatibility
baseline: the committed `agent_core.py` on `main` at
`bc572310021f67232f475399ced3d1752440af60` already differed from the supported
SSH lobby revision `8fdc86b9c658c9a322f9b11b5a615c1e4b182ab0`. This review covers
both that committed source and the subsequent whole-command implementation in
[decision 0102](decisions/0102-whole-command-routing-and-ordered-effects.md).

The complete delta from the supported lobby source to `bc572310` changes only
the command input's network metadata refresh, JSON rendering of that metadata,
and OpenAI invocation options requesting JSON mode. It leaves response
extraction, output coercion, and JSON rejection markers unchanged.

The subsequent change preserves the original input, routes deterministic
builtin lists locally, and applies ordered state effects through shared
runtime state handling. Its added `effects` schema validation is a state
contract intervention. Rejected effects retain terminal output and carry
`structured_response_accepted=false` with failure class `validation_error`;
the existing policy classifies them as `out_of_scope`, including when their
otherwise valid JSON envelope was fenced. Accepted effects retain the ordinary
stdout/stderr/exit-status coercions. Implicit program-transition normalization
inside effects now carries indexed `dreamland.transition.normalized` markers,
preserving the same non-JSON intervention evidence as ordinary state updates.

Source comparison verified that `_result_from_structured_model_payload`,
`_unparsed_structured_model_response`, `_coerce_output`, and
`_ensure_trailing_newline` have identical syntax trees across all three
reviewed versions. The full contents of `agent_structured.py` and
`agent_state.py` retain their original supported fingerprints. JSON fence
removal, trailing-envelope extraction, invalid-JSON detection, and integer
coercion therefore keep the existing reconstruction rules.

| Additional audited `agent_core.py` source | SHA-256 |
| --- | --- |
| Network projection and OpenAI JSON mode (`bc572310`) | `ab3ad3a6e839ee25cee83854b7bf50822dd495bef11271b819daefb891e68a07` |
| Whole-command routing and ordered effects | `9c7462c03a4a695330409b5366a42d45538fe2c0b471a973be8e80d9d0759903` |

Both exact fingerprints join the existing allowlist; arbitrary new runtime
sources still fail verification. The `emulator-json-v1` policy, score name,
classification rules, and historical fingerprints remain unchanged.

Provider-free differential tests feed the real runtime parser's processed
results into the offline evaluator. They compare ordinary and ordered state
updates for plain JSON, fenced JSON, trailing envelopes, and output coercion;
verify matching treatment of implicit transition normalization; keep malformed
JSON as `rejected`; and keep malformed effects as non-JSON `out_of_scope`
failures. The evaluator, annotation, and annotation CLI suites also validate
recorded-revision lookup against Git blobs and retain unknown-revision rejection.
This compatibility update performs no historical score writes.

### Interactive input runtime compatibility audit

On 2026-10-20, the interactive input fix exposed another stale supported
fingerprint. The committed `agent_core.py` on `main` at `51918a9` differed from
the previously audited whole-command source only by removal of the redundant
active-host prompt line in `3ba89f6`. This review covers that source and the
interactive input changes in [decision 0120](decisions/0120-interactive-input-lifecycle.md).

The current change adds active program prompt/metadata to model context,
delivers meaningful empty program input, preserves its history, and corrects
continuing dialogue frames. Those are input and state-transition changes.
They do not alter JSON fence removal, trailing-envelope extraction, strict
decoding, output coercion, or structured JSON rejection markers. In particular,
`active_program_replace_active` and `active_dialogue_replace_active` are
state normalization evidence, not JSON repairs.

Source comparison verified identical syntax trees for
`_result_from_structured_model_payload`, `_unparsed_structured_model_response`,
`_coerce_output`, and `_ensure_trailing_newline` across the previously audited
whole-command source, `51918a9`, and the interactive input implementation.
The complete `agent_structured.py` and `agent_state.py` files retain their
original supported fingerprints.

| Additional audited `agent_core.py` source | SHA-256 |
| --- | --- |
| Active-host prompt deduplication (`3ba89f6`) | `41373ebb277bc7e9cb47808d1d9504985f0abd53305f8920e9e44878610b58d4` |
| Interactive input lifecycle | `09adb254658450c96011a0e14e3be49e9050785e59d5602a4c0f50c7d0270d76` |

Both exact fingerprints join the supported set without removing historical
fingerprints or changing the `emulator-json-v1` policy. Differential tests feed
real parser results for ongoing `ssh-keygen` and successful sudo transitions
into the offline evaluator: plain JSON with only state normalization stays
`out_of_scope`, while supported fence/trailing-envelope recovery remains
`repaired`. Existing coercion, rejection, recorded-Git-revision, and CLI tests
continue to verify the original classification boundaries. A separate regression
changes each verified source individually and confirms that one unaudited hash
prevents reconstructed repair scores. No frozen campaign artifact or historical
score is changed.

### World-owned login presentation runtime compatibility audit

On 2026-10-20, the world-owned initial-login change in
[decision 0121](decisions/0121-world-owned-initial-login-banners.md) removed the
redundant server profile label from command context and deterministic debug
output. The complete `agent_core.py` delta against audited `main` revision
`d951f838d52ca2b61df5eab9e0e33b71f4977e80` changes only
`DeterministicCommandAgent.respond`, `_format_command_context`,
`_state_input_from_context`, `_state_from_context_for_builtin`, and
`_format_command_state`. The deterministic debug prefix is outside model JSON
reconstruction. The other changes remove input/context identity fields; they
do not change raw response capture or processed results.

Source comparison verified identical syntax trees and exact function-source
bytes for `_result_from_structured_model_payload`,
`_unparsed_structured_model_response`, `_coerce_output`,
`_ensure_trailing_newline`, `_message_content_to_text`,
`_state_update_from_structured_model_payload`, `_result_to_state`, and
`_result_from_state`. Complete `agent_structured.py` and `agent_state.py` bytes
also match the audited revision, retaining fingerprints
`9d8144f0a48bd7b002ae4fa41c0428029b44b448f623726ae2f5e3e0e5fb7b69` and
`b429531d133f4cc232bd4e7d09ffff411d20f29ef40d7d7e79023ba1ab949998`.
Fence removal, trailing-envelope extraction, strict decoding, output coercion,
and structured rejection evidence therefore keep their existing behavior.

| Audited `agent_core.py` source | SHA-256 |
| --- | --- |
| Interactive-input baseline (`d951f838`) | `09adb254658450c96011a0e14e3be49e9050785e59d5602a4c0f50c7d0270d76` |
| World-owned initial login and removed server profile label | `2196009a70f62e38458d8806eac069bb2b8bffd94abd6e63b692f5660acae526` |

The new fingerprint joins the historical supported set. The
`emulator-json-v1` policy, score names, classification rules, and arbitrary-hash
rejection remain unchanged. The provider-free evaluator and annotation suites
validate output-coercion differentials, JSON/transition boundaries, recorded Git
blob verification, and rejection of each independently changed unaudited source.
This compatibility audit performs no historical score writes and changes no
frozen evaluation artifact.

## Maintenance

Keep this document synchronized with
[evaluation_json.py](../src/dreamland/evaluation_json.py),
[evaluation_annotations.py](../src/dreamland/evaluation_annotations.py), and the
CLI. When parser, output-coercion, or state-codec behavior changes, review the
supported fingerprints for all three verified source modules,
classifications, and policy version before annotating new runtime revisions.
Keep [decision 0087](decisions/0087-post-run-emulator-json-annotations.md),
testing guidance, and the comparison's trace-status contract aligned. Do not
silently broaden this metric to semantic or state-transition interventions.
