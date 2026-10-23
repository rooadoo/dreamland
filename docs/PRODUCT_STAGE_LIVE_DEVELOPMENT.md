# Presenter Stage Live Development Target

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, and local evaluators
Purpose: run the Presenter Stage UI against a real model-backed DREAMLAND agent without enabling a production product capability.

## Overview

Phase 3D provides an explicit loopback target for development feedback. It
reuses the accepted Presenter Stage and shared-session semantics from
`0.4.0-rc.1`, but publishes a separate `0.4.0-rc.2` response identity so a
client cannot confuse real provider calls with the deterministic compatibility
preview.

This target is not part of `python -m dreamland`. Normal SSH, admin, MCP,
logging, state-profile, and EnrichmentAgent composition do not import or start
it. Product capabilities remain disabled, and `compose_product_runtime()`
continues to fail closed.

## Initialize Private Development Credentials

Choose durable local paths outside browser assets and create both files once:

```sh
.venv/bin/python tools/product_stage_live.py init \
  --token-file /path/to/stage-live.token \
  --bootstrap-file /path/to/stage-live-bootstrap.json
```

The command refuses to overwrite an existing path. Both files are created with
mode `0600`. The token authenticates the BFF-to-DREAMLAND transport. The
bootstrap file contains the Presenter actor context and launch grant. Treat all
three values as server-side credentials; never expose them to browser code,
logs, URLs, or screenshots.

## Start The Live Target

Ensure the configured agent provider has its API key. For an owner evaluation,
select the intended INI explicitly and disable only the accepted-command count
when desired:

```sh
DREAMLAND_CONFIG=/home/code/source/dreamland/dreamland.ini \
  .venv/bin/python tools/product_stage_live.py serve \
  --contract-root contracts/product-api/v0.4.0-rc.2 \
  --token-file /path/to/stage-live.token \
  --bootstrap-file /path/to/stage-live-bootstrap.json \
  --profile default \
  --max-commands -1 \
  --host 127.0.0.1 \
  --port 0
```

The first JSON line reports the actual port, profile, executor, and disabled
capability count, plus the resolved `provider:model`, finite
`max_output_tokens`, `command_reserved_tokens`, scaled `token_budget`, effective
`max_commands`, and output-publication policy. An unlimited accepted-command
guard is reported as JSON `null`. The announcement contains no credential.
Verify these non-secret settings before spending a provider call. The process
constructs the model client at explicit startup but calls the provider only
after an accepted command.

Omitting `--max-commands` preserves the 12-command development default. A
positive value accepts that many commands and then returns
`command_limit_reached`. `--max-commands -1` disables only this count guard;
zero and values below `-1` are rejected. The sentinel is never placed in the
launch limits map: unlimited launches omit `max_commands`, preserving the
positive-integer API contract.

Live command admission derives its token reservation from the configured
provider maximum before provider construction:

```text
command_reserved_tokens = max(8,192, max_output_tokens)
token_budget = 1,024 * command_reserved_tokens
```

The configured provider maximum must be a finite positive integer. Blank or
zero configuration resolves to an unbounded value and makes live product
startup fail before constructing a provider or binding a listener. Values at
or below 8,192 retain the conservative 8,192-token reservation; larger finite
values scale the reservation and process token budget so dispatch never occurs
under a smaller allowance. This rule belongs only to explicitly started
product live targets and does not change the ordinary DREAMLAND agent.

The BFF should use the server-side generated client under
`contracts/product-api/v0.4.0-rc.2/generated/`. It first reads capabilities to
obtain the service epoch, launches Stage with the actor context and launch
grant, and privately retains the returned session capability. The browser
receives only the BFF's browser-safe projection and cookie/session mechanism.

## Evaluation Workflow

The initial backend evaluation should verify:

1. capability discovery reports all five entries disabled and the live
   development executor identity;
2. Stage launches from the selected immutable state profile;
3. `pwd` or `ls -la` returns real model-backed output;
4. a state-changing sequence such as `touch phase3d-live-marker` followed by
   `ls -la` observes the accepted state;
5. refresh/current lookup recovers the same Stage rather than silently creating
   another world;
6. transcript and command-result recovery agree;
7. Ctrl+C or a deadline reaches a terminal interrupted/failed result; and
8. close followed by launch produces a clean new session.

The UI may label this source `LIVE DEVELOPMENT`. It must not label the target
production-ready or imply that a disabled capability passed its release gate.

## Output Publication

The live adapter removes provider SDK segmentation from guest-visible output.
It publishes each complete newline-terminated line immediately. An
unterminated line is emitted in deterministic UTF-8-safe 4 KiB pieces and any
remaining suffix is flushed before the terminal result. Switching between
stdout and stderr also flushes the prior stream, preserving exact order and
stream identity.

The live command byte ceiling remains 65,536 bytes. Its chunk allowance is
deliberately nonbinding before that byte ceiling, so more than 128 provider
pieces do not clip an otherwise bounded result. `output_clipped` becomes true
only when content was actually omitted, including content beyond the byte
ceiling; exact use of the boundary is complete.

The BFF and UI must preserve `output_clipped`. A completed command remains a
completed command, but the UI should display a conspicuous incomplete-output
warning and recommend the native SSH console for large-output exploration. It
must not imply that the SSH console continues the same private Stage session.

## Limits And Safety

The adapter claims one cooperative attempt before invoking the configured
LangGraph command agent. It deletes the exact in-memory checkpoint thread after
execution and uses detached state-profile encoding. Each dispatched command is
settled at the full derived token reservation and configured spend reservation.
This is conservative local accounting, not a measurement of exact provider
cost.

The target does not prove SDK retry enforcement, dedicated provider
credentials, deployment-wide isolation, browser authentication, CSRF/Origin/
Host policy, production cleanup, load behavior, or Gate 1/2 completion. It
supports only loopback and should be terminated after evaluation.

Unlimited accepted commands do not mean unbounded resources. The live process
retains an independent 1,024-job identity ceiling and finite token/spend
budgets, while the foreground slot, two-worker limit, TTLs, command and output
bounds, cancellation fence, one provider attempt, authentication, authority,
idempotency, and journal retention remain active. These independent limits may
still return their truthful resource errors; they are not derived from the
former 12-command default.

## Maintenance

Keep this document, `docs/PRODUCT_SESSION_API.md`, architecture and operations
documentation, decision 0070, the immutable `0.4.0-rc.2` kit and generated
client, live adapter/tool, and owning tests aligned. Do not weaken the disabled
normal-runtime invariant to make local startup more convenient.
