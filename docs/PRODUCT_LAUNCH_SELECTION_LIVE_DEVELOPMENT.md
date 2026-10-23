# Source-Neutral Launch-Selection Live Development Target

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, and local evaluators
Purpose: explain how to run the capability-disabled live authored/Forge
Presenter selection target and evaluate it through the existing terminal and
X-ray UI.

## Overview

The Phase 7B development target combines one exactly pinned operator-authored
world with newly selected live World Forge revisions behind the private
source-neutral launch-selection API. Either option can be exchanged for
Presenter Stage authority and opened through the existing terminal and X-ray
experience.

```text
pinned authored snapshot ----\
                              -> private launch option -> Stage terminal/X-ray
authoritative Forge selection /
```

The product contract identity is `v0.7.0-rc.2`; World Forge remains pinned to
`v0.1.0-rc.4`. Both listeners bind only to loopback. Forge builds and accepted
Stage commands may call the configured providers. Listing, exchange, immutable
artifact reopening, Stage provisioning, transcript/event reads, and X-ray do
not call a provider.

This target is manually invoked development evidence. Every product capability
remains disabled, `live_capability_evidence` remains false, and normal
`python -m dreamland` startup does not import or construct it. It does not
provide public `/session` attendance, promotion, or an attendee catalog.

## Pin One Authored World

Choose one trusted local world-level state-profile file. Calculate its
metadata-free immutable digest:

```sh
.venv/bin/python tools/product_launch_selection_live.py digest-authored \
  --profile-path /path/to/state_profiles/example.json
```

Record the returned `authored_revision_digest` in the private launch
configuration used to start the target. The digest is not a transport secret,
but it is an operator integrity decision and must not be supplied by browser
input.

The serve command reads the configured source with no symlink following,
requires the exact digest, validates world shape and synthetic boot, and
publishes only the complete immutable snapshot into owner-only
content-addressed storage. A changed source or incorrect pin fails before
provider construction or listener publication. The source profile name, path,
timestamps, comments, and mutable profile-store identity do not enter the
revision or any API response.

## Create Private Credentials

Choose three distinct paths in a private directory and initialize them once:

```sh
.venv/bin/python tools/product_launch_selection_live.py init \
  --forge-token-file /path/to/private/launch-live/forge.token \
  --stage-token-file /path/to/private/launch-live/stage.token \
  --bootstrap-file /path/to/private/launch-live/bootstrap.json
```

The command refuses to overwrite existing files. Both bearers and the shared
Presenter actor context are owner-only BFF/server credentials. Never place
them in browser code, URLs, browser storage, screenshots, fixtures, or public
configuration.

## Start Both Live Services

Use a persistent private artifact directory, the exact digest obtained above,
and the intended DREAMLAND provider configuration:

```sh
DREAMLAND_CONFIG=/path/to/dreamland.ini \
  .venv/bin/python tools/product_launch_selection_live.py serve \
  --forge-token-file /path/to/private/launch-live/forge.token \
  --stage-token-file /path/to/private/launch-live/stage.token \
  --bootstrap-file /path/to/private/launch-live/bootstrap.json \
  --forge-contract-root contracts/world-forge-api/v0.1.0-rc.4 \
  --contract-root contracts/product-api/v0.7.0-rc.2 \
  --artifact-root /path/to/private/launch-live/artifacts \
  --authored-profile-path /path/to/state_profiles/example.json \
  --authored-profile-digest sha256:REPLACE_WITH_EXACT_DIGEST \
  --deadline-seconds 120 \
  --max-commands -1 \
  --host 127.0.0.1 \
  --forge-port 0 \
  --stage-port 0
```

Optional `--authored-title` and `--authored-revision-label` values are bounded
display labels. They are trusted startup configuration, not HTTP fields.

Before constructing providers or listeners, startup validates the exact RC2
contract, its all-disabled capabilities, and its byte-pinned World Forge RC4
dependency. The authored pin is verified before constructing provider-backed
collaborators. Port `0` asks the operating system for distinct loopback ports;
fixed loopback ports are also accepted.

The first JSON line reports both bound addresses, exact contract revisions,
resolved Forge and Stage models, maximum output-token settings, the Stage
`command_reserved_tokens` and scaled `token_budget`, Forge deadline,
selected-record TTL, option/revision/grant capacities, output-publication
bounds, X-ray bounds, and the effective command limit. It does not report the
profile name or path, authored pin, artifact path, bearers, actor context,
launch grants, session capabilities, API keys, or provider secrets.

Omitting `--max-commands` keeps the live Stage default of 12 accepted commands.
A positive value sets another finite count. `--max-commands -1` omits only that
positive-integer launch limit. Session and idle TTL, active-session and worker
capacity, command size, output bytes/chunks, provider attempts, command
deadline, cancellation, token reservation, and spend reservation remain
enabled.

The Stage command provider maximum must be finite and positive. Before
constructing either provider or listener, this target derives
`command_reserved_tokens = max(8,192, max_output_tokens)` and scales the
existing 1,024-job Presenter token budget by that reservation. Blank or zero
agent output configuration is unbounded and fails startup. A configured value
above 8,192 is therefore admitted only with a matching larger reservation and
visible token budget; lower values retain the conservative floor. The rule does
not alter the separately configured World Forge provider or any ordinary
DREAMLAND agent path.

## BFF Evaluation Flow

Pin the live BFF to the generated clients under
`contracts/product-api/v0.7.0-rc.2/generated/` and
`contracts/world-forge-api/v0.1.0-rc.4/generated/`. Keep both bearers, the actor
context, exchanged launch grants, and session capabilities on the BFF side.

1. Read World Forge identity and Product capabilities and retain both current
   service epochs server-side.
2. List `GET /product/v1/stage/launch-options` with the Product bearer,
   Product epoch, and Presenter actor context. The initial list contains the
   configured authored option and exposes no profile path or authority.
3. Exchange its opaque `launch_selection_id` with a fresh idempotency key.
   Retain the returned `launch_grant` only in the BFF and use the existing
   Stage create operation. Give the browser only the resulting Stage reference
   through the UI's existing same-origin mechanism.
4. Run terminal commands and inspect X-ray through the existing Stage routes.
   The launch option and exchange selection report the closed authored origin;
   the active Stage snapshot reports the exact immutable starting revision and
   display metadata without a generic source projection.
5. Use the RC4 Forge recipe, preparation, submission, snapshot, and event flow
   to obtain a genuinely authoritative `selected` live result.
6. Refresh launch options. Registration happens immediately after Forge commits
   selected truth, but a short visibility window is possible; refresh rather
   than deriving authority from the receipt. Match the new option's immutable
   revision and digest to the selected Forge result.
7. Exchange the Forge option with a new idempotency key. Use the existing
   explicit Stage switch/replacement operation with current reference,
   session capability, and control revision. Never perform an implicit switch.
8. Continue terminal and X-ray evaluation in the replacement session. The
   Forge launch option and exchange selection report
   `world_forge_selection`; the replacement Stage snapshot reports the exact
   starting revision and display metadata. The legacy Forge handoff remains
   available when its richer private job/reference correlation is required.

Option and selection identifiers are locators, not credentials. A Forge job
reference, selected receipt, revision ID, digest, or artifact path is also not
generic Stage authority. Only a successful private exchange can mint the
actor-bound, operation-scoped, expiring grant.

## Current Limits

The target admits one authored option and at most sixteen Forge jobs within a
32-revision/32-option registry. Stage has separate bounded capacity for generic
and legacy Forge grants, two active sessions, finite recovery/idempotency
retention, and finite admission budgets. A Forge option expires with its
authoritative selected record; its exchanged grant cannot outlive that option.
The registry and Stage enforce this boundary against both absolute UTC and
monotonic time, including a forward wall adjustment followed by rollback.

The live vertical slice still lacks the full production World Forge durability,
hard provider deadline, redundant attempt/repair, shared Forge/Stage budget,
deployment, canary, rollback, and acceptance gates. It is suitable for private
UI development evaluation, not production enablement or presentation-readiness
claims.

## Maintenance

Update this document with `tools/product_launch_selection_live.py`, the
immutable RC2 contract, launch-selection runtime and Forge registrar modules,
decisions 0067 and 0070, and owning tests whenever startup inputs, authority
ownership, provider identity, capacities, authored pinning, Forge registration,
terminal/X-ray flow, or limitations change.
