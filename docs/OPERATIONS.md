# Operations

Status: Active
Audience: DREAMLAND operators and maintainers
Purpose: document setup, configuration, runtime controls, logging, and tracing.

## Setup

Install the package in a local virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Create local settings from the sanitized template:

```sh
cp dreamland.example.ini dreamland.ini
```

Do not commit `dreamland.ini`. It can contain API keys and local tracing
configuration.

## Running

Every application startup requires a nonempty attendee SSH password supplied
only through the process environment:

```sh
export DREAMLAND_SSH_PASSWORD='replace-with-a-local-secret'
```

Start the default process with the local Dreamland console:

```sh
.venv/bin/python -m dreamland
```

Connect through the current SSH entry point:

```sh
ssh -p 2222 test@127.0.0.1
```

Run without the local console:

```sh
.venv/bin/python -m dreamland --daemon
```

### SSH Transport Identity

The listener defaults to private `direct` entry. Set `[ssh] entry_mode = lobby`
for the optional interactive PTY lobby with a versioned agreement, curated saved
worlds, fresh isolated visits, and public report references. See
[Native SSH Lobby](SSH_LOBBY.md) for configuration precedence, hot console
curation, notice files, input bounds, reference retention, and manual operator
checkpoints. Lobby catalog/reference storage is not composed in direct mode.

DREAMLAND presents a dedicated RSA host key for its real SSH endpoint. This
transport identity is separate from the fictional entry-host and host records
inside a state profile. The first process using a path securely generates and
atomically publishes one key; later processes load the same bytes and report
the same OpenSSH `SHA256:` fingerprint. Concurrent first starts converge on one
published key rather than overwriting one another.

The default path is:

```text
$XDG_STATE_HOME/dreamland/ssh_host_rsa_key
```

When `XDG_STATE_HOME` is unset, the default is:

```text
~/.local/state/dreamland/ssh_host_rsa_key
```

Override it with `[ssh] host_key_path` or the higher-precedence
`DREAMLAND_SSH_HOST_KEY_PATH`. An INI-relative path is resolved relative to the
active configuration file. The environment override must be absolute. A
service deployment commonly uses:

```ini
[ssh]
host_key_path = /var/lib/dreamland/ssh_host_rsa_key
```

The containing directory must be owned by the service user and must not be
group/world writable. On POSIX, a generated private directory is `0700` and a
generated key is `0600`; an existing key may be `0400` or `0600`. Symlinks,
non-regular files, weak permissions, wrong ownership, encrypted or malformed
keys, RSA keys smaller than 2048 bits, and unsupported atomic-storage
semantics fail before the listener binds. DREAMLAND never silently replaces a
bad key or falls back to an ephemeral identity. Secure creation currently
requires POSIX directory-relative open/link primitives and durable directory
sync support.

The lifecycle log reports whether the key was generated or loaded, its path,
algorithm, and public fingerprint. It never includes private key material.
The fingerprint is operator and canary evidence for detecting an unexpected
identity change. DREAMLAND does not require publishing it to attendees or
placing it in presentation materials.

Container or image restarts retain identity only when the configured path uses
a host bind mount, persistent volume, or deliberately provisioned secret.
Replicas behind one public endpoint must receive the same intended key, or each
must use a distinct endpoint so clients do not receive inconsistent keys.

#### Backup And Restore

1. Stop the DREAMLAND listener.
2. Copy the private key through a protected operator channel while preserving
   its owner and `0400`/`0600` mode.
3. Restore it to the configured path before startup.
4. Start DREAMLAND and compare the reported fingerprint with the recorded
   fingerprint before exposing the endpoint.

Do not store the backup in the repository, image layer, presentation assets,
logs, or ordinary configuration files.

#### Intentional Rotation

1. Stop the listener; never replace a live endpoint key.
2. Archive or securely remove the old key according to the deployment's
   retention policy.
3. Install a new provisioned RSA key with safe ownership and permissions, or
   start once with the configured path absent so DREAMLAND generates one.
4. Verify the new fingerprint, update operator records and canaries, and only
   then expose the endpoint.

DREAMLAND performs no timed or automatic host-key rotation. An unexpected
fingerprint change is a security event, not self-healing behavior.

### SSH Attendee Policy

`DREAMLAND_SSH_PASSWORD` is sampled once during process startup and has no INI
or command-line equivalent. Missing, empty, or whitespace-only values fail
before the SSH listener opens. Rotate the static password by changing the
environment secret and restarting DREAMLAND. Effective configuration exposes
only whether it is configured, never its value.

In direct-entry mode, any nonempty SSH username is accepted when the password matches. DREAMLAND
sanitizes that username for use as the simulated login and session label. It is
not a verified identity: attendees may select the same label, and the label
must not be used for authorization or attribution.

Lobby mode accepts `dreamland` and `dreamland_simple` with the normal password.
Optionally set `DREAMLAND_SSH_FAST_PASSWORD` to a distinct, nonblank password to
enable `dreamland_fast` and `dreamland_fast_simple`. Those connections request
`ultrafast` for the emulator, while normal logins use the configured `[agent]`
tier (`fast` by default). Both passwords are environment-only startup settings;
restart to rotate them. An unset/empty demo password disables demo admission.
The demo password never admits a normal login, or vice versa. Spiritguide and
enrichment keep their configured tiers. See [SSH Lobby](SSH_LOBBY.md#demo-service-tier)
for connection lifetime, logging, and manual checks.

The listener advertises password authentication only. It explicitly rejects
public-key, none, keyboard-interactive, and GSSAPI authentication. Each
connection may open one `session` channel for either a shell or an exec request.
DREAMLAND rejects SFTP and every other subsystem, direct and reverse port
forwarding, SSH agent forwarding, X11 forwarding, unknown global requests, client
environment requests, additional session channels, and other channel types.
Paramiko acknowledges `cancel-tcpip-forward`, but DREAMLAND handles it as a
no-op because no forwarding request can establish state.

SSH is an attendee surface. A shell or exec command in the reserved
`!dreamland` namespace receives a fixed denial without reaching the command
agent or operator console. Use the process-local Dreamland console or the
separately authenticated loopback admin REST/MCP interfaces for logs, traces,
Spiritguide, profile changes, session administration, enrichment controls, and
shutdown.

The shared password is admission control, not per-attendee accountability. The
current listener does not itself provide source-level authentication throttling,
connection/command rate limits, concurrency quotas, or spend budgets; apply
deployment controls and treat those as required follow-up before broad public
exposure.

## Configuration

Configuration is loaded from `dreamland.ini` by default. Set
`DREAMLAND_CONFIG` to point at another INI file.

The current runtime boots from a required state profile:

```ini
[state_profile]
# Packaged `default` exists; local profiles live in state_profiles/.
profile = default
```

State profiles describe the starting session state and mutable world state.
World-level profiles include `entry_host`, mutable `active_host`, and the
top-level `hosts_by_name` registry. The command emulator receives a projected
copy of this single `world_state` record rather than a separate static world
profile. Stored `login_banner` fields are omitted from command and enrichment
model inputs; runtime state and Spiritguide context retain them. The initial
interactive shell sends the active host's saved banner immediately before the
first prompt without a provider call. The real SSH protocol uses the neutral
`SSH-2.0-DREAMLAND` identification string.

State-profile document schema version 2 is required. Entry and initial active
host references must resolve, and both host records must contain a valid
`login_banner` string. Version 1 is rejected without a conversion fallback.
Repair existing local and deployed profiles before starting the updated runtime;
local copies shadow package resources, and package/image updates do not rewrite
operator-owned stores. See [the banner contract and repair steps](STATE_PROFILES.md#saved-initial-login-banners).

Important environment overrides:

- `OPENAI_API_KEY`
- `XAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `DREAMLAND_MODEL`
- `DREAMLAND_SYSTEM_PROMPT`
- `DREAMLAND_REASONING_EFFORT`
- `DREAMLAND_REASONING_SUMMARY`
- `DREAMLAND_VERBOSITY`
- `DREAMLAND_MAX_OUTPUT_TOKENS`
- `DREAMLAND_SPIRITGUIDE_MODEL`
- `DREAMLAND_SPIRITGUIDE_SYSTEM_PROMPT`
- `DREAMLAND_SPIRITGUIDE_REASONING_EFFORT`
- `DREAMLAND_SPIRITGUIDE_REASONING_SUMMARY`
- `DREAMLAND_SPIRITGUIDE_VERBOSITY`
- `DREAMLAND_SPIRITGUIDE_MAX_OUTPUT_TOKENS`
- `DREAMLAND_ENRICHMENT_ENABLED`
- `DREAMLAND_ENRICHMENT_APPLY_MODE`
- `DREAMLAND_ENRICHMENT_TRIGGERS`
- `DREAMLAND_ENRICHMENT_IDLE_DELAY_MS`
- `DREAMLAND_ENRICHMENT_COOLDOWN_MS`
- `DREAMLAND_ENRICHMENT_MAX_JOBS_PER_MINUTE`
- `DREAMLAND_ENRICHMENT_MAX_INFLIGHT_PER_SESSION`
- `DREAMLAND_ENRICHMENT_EVENT_LOG_CAPACITY`
- `DREAMLAND_STATE_PROFILE`
- `DREAMLAND_SSH_HOST`
- `DREAMLAND_SSH_PORT`
- `DREAMLAND_SSH_HOST_KEY_PATH` (absolute path; overrides `[ssh] host_key_path`)
- `DREAMLAND_SSH_PASSWORD` (required, environment-only)
- `DREAMLAND_SSH_FAST_PASSWORD` (optional, environment-only; lobby demo logins)
- `DREAMLAND_LOG_ENABLED`
- `DREAMLAND_LOG_LEVEL`
- `DREAMLAND_LOG_EVENTS`
- `DREAMLAND_REPLY_EXCERPT_CHARS`
- `DREAMLAND_CONSOLE_LOG_CAPACITY`
- `DREAMLAND_PRODUCT_API_ENABLED`
- `DREAMLAND_API_ENABLED`
- `DREAMLAND_API_HOST`
- `DREAMLAND_API_PORT`
- `DREAMLAND_API_TOKEN`
- `DREAMLAND_API_MCP_WRITE_TOOLS`

### Private Product API Foundation, Core, And Stage Preview

The private BFF-facing product service has a separate master gate:

```ini
[product_api]
enabled = false
```

It defaults to disabled and is independent of the local Operator/admin
`[api]` section. `DREAMLAND_PRODUCT_API_ENABLED` overrides the INI value.

There is deliberately no production product listener. With the setting absent
or false, DREAMLAND does not import or compose product runtime code and starts
no product listener, worker, timer, journal, event subscription, provider
reservation, idempotency ledger, or session. Setting it to true still fails
startup closed with `ProductCompositionUnavailable`; it does not start the
Phase 1 lifecycle, Phase 2 core, or Phase 3 Stage preview and does not fall back
to admin/MCP, fixtures, SSH, or a fixed world. There are no product host, port,
bearer, or per-capability runtime settings in this phase.

The loopback capability, lifecycle, and core-preview tools described in
`docs/PRODUCT_SESSION_API.md` are manually started development tools. They are
not composed into DREAMLAND, must not be exposed as production endpoints, and
do not make any product capability live. Keep the private simulator/client
bearer and all actor, launch, and session authority server-side in the BFF;
never reuse the admin token or expose private authority in browser code.
Supply the Phase 1 preview bearer through its owner-only, non-symlink token-file
option so it does not appear in process arguments. The preview bounds admitted
connections and enforces read and total-connection deadlines; these safeguards
exist only in the manually started tool and add no normal runtime thread or
timer. All three development tools process `--once` synchronously and exit only
after the complete selected response is written. Ordinary serve mode remains
threaded.

The Phase 2 core preview adds command, result, cancel, event, transcript,
reset, and switch integration routes around a deterministic executor. It makes
no provider or host-command call, reports `integration_preview_only`, and keeps
all five capability entries disabled. It is appropriate only for a BFF's local
generated-client and recovery testing. Do not expose it outside loopback, use
it as a production fallback, or interpret a successful response as capability
evidence. A live core remains blocked on a dedicated provider adapter,
non-overcommitting deployment isolation and hard limits, gateway integration,
and the complete capability gate.

The Phase 3 Stage preview adds presenter-only exact launch/resume and explicit
replacement routes over those shared command and recovery routes. It uses an
in-process hard Stage admission partition and exposes only bounded configured
display labels; X-ray remains unavailable. Start it only on loopback:

```sh
.venv/bin/python tools/product_stage_preview.py \
  --contract-root contracts/product-api/v0.4.0-rc.1 \
  --token-file /path/to/owner-only-preview.token \
  --port 0
```

Select `stage-launch-response-lost` only at process start to test same-key
launch recovery. `command-accept-response-lost` and
`promised-terminal-event-withheld` exercise the shared command recovery paths.
The Stage preview is deterministic, performs no provider or host-command work,
and does not enable `presenter_stage_terminal`.

Phase 3D adds a separate live-development target for maintainers who need to
evaluate the UI against the configured model-backed command agent. Initialize
owner-only local credentials once, then start it explicitly:

```sh
.venv/bin/python tools/product_stage_live.py init \
  --token-file /path/to/stage-live.token \
  --bootstrap-file /path/to/stage-live-bootstrap.json

DREAMLAND_CONFIG=/home/code/source/dreamland/dreamland.ini \
  .venv/bin/python tools/product_stage_live.py serve \
  --contract-root contracts/product-api/v0.4.0-rc.2 \
  --token-file /path/to/stage-live.token \
  --bootstrap-file /path/to/stage-live-bootstrap.json \
  --profile default \
  --max-commands -1 \
  --port 0
```

The target refuses non-loopback hosts. It reads the ordinary configured agent
model and provider credential only during explicit composition, and makes no
provider call until a command is submitted. The token and bootstrap files must
be owner-only regular files. Only the BFF may read their bearer, actor context,
and launch grant; none belongs in browser JavaScript. The startup JSON reports
the selected ephemeral port without printing credentials.

Each dispatched development command is conservatively charged its complete
token and spend reservation. This bounds the local ledger but is not exact
provider-cost evidence. The target continues to report every product
capability disabled and must not be exposed directly to a browser or nonlocal
network. See `docs/PRODUCT_STAGE_LIVE_DEVELOPMENT.md` for the handoff and smoke
workflow.

The startup JSON includes the resolved non-secret model reference,
`max_output_tokens`, and accepted-command limit. Verify them before provider
work. Omitting `--max-commands` retains the 12-command default; positive values
set that guard and `-1` disables only that guard. Independent process job
retention, token/spend budgets, concurrency, TTL, command/output, and provider
attempt protections remain finite.

Explicit product live targets additionally require a finite positive agent
`max_output_tokens` before constructing a provider or listener. They reserve
`max(8,192, max_output_tokens)` tokens per accepted command and scale the
applicable Presenter or attendee token budget by its existing logical-job
capacity. Blank or zero configuration resolves to an unbounded value and fails
startup. Their safe startup JSON reports `max_output_tokens`,
`command_reserved_tokens`, and `token_budget`; verify that the reservation is
not smaller than the provider maximum. This development accounting rule does
not alter ordinary DREAMLAND agent configuration or startup.

The same announcement reports the live output-publication policy. Complete
lines flush immediately; unterminated output flushes at 4 KiB, and the finite
per-command ceiling remains 65,536 UTF-8 bytes. Provider stream-piece count is
not an earlier limit. If `output_clipped` is true, treat the result as visibly
incomplete even when the command itself completed.

The `[api]` section starts the optional local admin API when `enabled = true`.
Install the optional admin dependencies before enabling it, for example
`pip install -e ".[admin]"` from a source checkout. If those dependencies are
missing, startup fails before the API server thread is launched and reports the
missing optional admin dependency message directly. Other API startup failures,
such as bind errors, are reported through the normal startup wait instead of
appearing only as a timeout.
The current server exposes authenticated health, status, capabilities, redacted
effective config, logs, combined session summaries, live sessions, session
traces, retained enrichment lifecycle events, live session state, state-profile
summaries and control, spiritguide, shutdown, server-sent event streams,
Langfuse-backed command lists, command inspection, and state deltas under
`/api/v0`. `GET /api/v0/sessions` marks each
session with `live` and `langfuse` booleans so operators can tell whether it is
present in the current process, Langfuse, or both.
`GET /api/v0/sessions/{session_id}` returns a unified live/historical detail
object for UI and agent inspection. `GET /api/v0/live-sessions` returns only the
live in-memory registry. Historical command, state-delta, and
`POST /api/v0/introspection/flush` routes use the same Langfuse-backed
introspection layer as the console. State-profile default changes through the
API are process-local and do not rewrite `dreamland.ini`.

`GET /api/v0/events` streams runtime logs as server-sent events.
`GET /api/v0/sessions/{session_id}/events` streams live trace records for one
session. Both accept `after`, `limit`, `timeout`, and `once`; `once=true` is
useful for scripts that want a bounded poll instead of a long-lived stream.

The API host must be loopback-only, such as `127.0.0.1`, `::1`, or `localhost`;
broad bind addresses such as `0.0.0.0` are rejected. MCP access is enabled by
default when the API is enabled and is served through authenticated Streamable
HTTP JSON-RPC. The admin MCP endpoint is `/mcp`; the generic command-client
evaluation endpoint is `/client-mcp`.

Admin MCP read-only resources are available by default. External testing agents
should read `dreamland://agent-guide` or `docs/MCP_AGENT_WORKFLOWS.md` before
creating sessions. That guidance is for agents and operators outside the
simulation; it is not part of Dreamland's simulated command-agent prompt.

`mcp_write_tools = true` requires `enabled = true` and exposes
model-invokable write tools; leave it false unless an MCP client should be
allowed to mutate DREAMLAND admin state or create headless test sessions.
With write tools enabled, admin MCP clients can create tagged sessions, run
ordinary user-visible commands through the same backend used by SSH, close
MCP-created sessions, and run explicit admin tools such as state-profile and
spiritguide operations. Enabled admin clients also receive typed inspection
tools for logs, session lists, live traces, exact session/job enrichment events,
command summaries, command inspection, state deltas, tag groups, and filtered
session cleanup.
Session creation also accepts a saved `state_profile` and per-role
`config_overrides`. Explicit override sessions own independent command,
Spiritguide, and enrichment agents, and return a non-secret configuration
snapshot for run correlation. Detached Spiritguide profile operations accept
their own Spiritguide overrides. See
[MCP Model Evaluation Controls](MCP_MODEL_EVALUATION.md) for fields, inheritance,
provider boundaries, profile identity semantics, and the live restart checkpoint.
`dreamland_session_run_command` returns the direct command id alongside
stdout/stderr and exit status; agents should use that id for later inspection
instead of relative command targets.

MCP command inspection defaults to compact summaries; request full detail only
when raw Langfuse input/output or metadata is needed for diagnosis. While a
session is live, agents should prefer DREAMLAND MCP inspection over Langfuse MCP;
Langfuse MCP is mainly for historical analysis after DREAMLAND no longer has
live session context or when validating telemetry parity. Recent historical
command summaries can lag briefly after a command runs; use live trace for
immediate input/output and retry historical command inspection after a short
delay if needed. Shell commands beginning with `!dreamland` are rejected by the
admin MCP command runner; use explicit MCP resources or tools for operator
actions.

With write tools enabled, `/client-mcp` exposes only create, run-command, and
exact-session-exit tools for generic command-client evaluation.

If `token` is blank or set to
`replace-me`, DREAMLAND generates a temporary bearer token and prints it on
boot. Set `token` or
`DREAMLAND_API_TOKEN` to use a stable token.

Minimal API smoke test:

```sh
curl -H "Authorization: Bearer TOKEN" http://127.0.0.1:8765/api/v0/health
```

Minimal event-stream smoke test:

```sh
curl -N -H "Authorization: Bearer TOKEN" \
  "http://127.0.0.1:8765/api/v0/events?after=0&limit=5&once=true"
```

Minimal MCP smoke test:

```sh
curl -s \
  -H "Authorization: Bearer TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' \
  http://127.0.0.1:8765/mcp
```

Use the `Mcp-Session-Id` header returned by `initialize` on later MCP resource
and tool calls. Use `http://127.0.0.1:8765/client-mcp` for the generic
command-client evaluation endpoint; it has a separate MCP session id from
`/mcp`.

## Session Lifecycle

The console `sessions` command has live-session lifecycle helpers:

- `sessions --live` lists live in-memory sessions with tags, labels, origin,
  cwd, history count, and state-profile label.
- `sessions --live --tag TAG --label LABEL --origin ORIGIN` filters live
  sessions by metadata.
- `sessions --close SESSION_ID` closes one live session by id.
- `sessions --close-matching --tag TAG --origin mcp --dry-run` previews
  filtered cleanup.
- `sessions --close-matching --tag TAG --origin mcp` closes matching live
  sessions.

Filtered close requires at least one of `--tag`, `--label`, or `--origin`.
Agents should use the MCP `dreamland_sessions_close` tool with exact full
`session_ids` and `origin="mcp"` whenever possible. Agents should use tag,
label, or origin-only cleanup only when an exact session id is unavailable or an
operator explicitly asks for grouped cleanup. Non-id cleanup should first run
with `dry_run=true`; agents should report matches and get human/operator
approval before repeating with `dry_run=false`. DREAMLAND emits warning logs
when a non-dry-run close uses non-id filters or matches non-MCP sessions.

## Model Providers

The command-emulation agent, spiritguide, and each EnrichmentAgent role can use
separate chat-model providers. Model values must use `provider:model` format so
the provider is self-contained in each model setting.

```ini
[agent]
model = openai:gpt-6.1-sol
reasoning_effort = low
reasoning_summary = detailed
openai_service_tier = fast
max_output_tokens = 12800

[spiritguide]
model = openai:gpt-6-astra
openai_service_tier = ultrafast
reasoning_effort = high
reasoning_summary = detailed
max_output_tokens = 16384
```

New local and Docker deployment configurations share the role preferences in
[`model_defaults.py`](../src/dreamland/model_defaults.py). Command emulation
uses Sol with low reasoning, a detailed summary, a 12,800-token cap, and the
`fast` OpenAI serving tier. Spiritguide uses Astra with high reasoning, a detailed
summary, a 16,384-token cap, and the `ultrafast` OpenAI serving tier. The
emulator and enrichment roles default to `fast`. Focused and
Ambient Coordinators and the Integrator use Luna, while the Expert uses GPT-6.1 Sol.
All enrichment roles use low reasoning, low verbosity, and no requested summary;
their output caps remain 2,048 tokens, or 4,096 for the Integrator. These model
preferences do not copy tracing, credentials, paths, or other lab configuration.
Existing explicit configuration continues to override these defaults.
After commit `721ff67`, the owner selected Astra for Spiritguide in place of Sol;
its other controls remain unchanged. This configuration change does not establish
live-provider validation.

The demo deployment adopted the October 2026 Sol/Luna refresh after explicit
configuration review and backup. Other existing server INIs and running
processes require their own explicit update and restart. GPT-6.1 Sol rejects
`none` and `minimal` reasoning effort; the
packaged roles retain `low`. See
[decision 0117](decisions/0117-gpt-6-role-model-refresh.md).

[Decision 0118](decisions/0118-all-runtime-roles-fast-tier.md) records the owner's
follow-up instruction to use Fast for Spiritguide and every enrichment role as
well as the emulator. [Decision 0127](decisions/0127-spiritguide-ultrafast-default.md)
supersedes that tier default for Spiritguide, selecting `ultrafast`. Existing
explicit INI or environment overrides still take precedence; inspect effective settings and returned tiers
when verifying an existing deployment.

Supported providers are:

- `anthropic`: uses `ANTHROPIC_API_KEY`.
- `openai`: uses `OPENAI_API_KEY`.
- `xai`: uses `XAI_API_KEY`.

As of August 2, 2026, representative current Anthropic Claude API model
references are `anthropic:claude-haiku-4-5-20251001` for the fastest and most
economical tier, `anthropic:claude-sonnet-5` for a speed/intelligence balance,
`anthropic:claude-opus-5` for complex high-capability work, and
`anthropic:claude-fable-5` when the highest generally available capability is
more important than cost or latency. DREAMLAND's packaged role defaults remain
provider-specific choices based on tested fit; this list documents current
Anthropic representatives rather than changing every role to Claude.

DREAMLAND warns when Anthropic is selected for Spiritguide broad state
guidance. Review proposed patches and structured-response failures before
applying broad changes. Provider controls and output limits are independent
for each runtime role; the packaged preferences are listed above.

When an INI or environment override selects an xAI or Anthropic model, an
omitted OpenAI serving-tier control remains unset. An explicitly selected xAI
or Anthropic Spiritguide also retains provider-default reasoning effort unless
the operator sets it. Explicit controls in an existing INI remain authoritative.

`reasoning_effort` is supported by OpenAI and xAI reasoning models. xAI
documents `none`, `low`, `medium`, and `high` for `grok-4.3`, with `low` as the
provider default. DREAMLAND accepts `max` for the current OpenAI models;
known GPT-5 through GPT-5.5 families reject that combination before provider
construction. GPT-6 Astra rejects `none`; use `low` or higher. The new `max`
value is ignored with an informational log for xAI, whose existing effort
behavior otherwise remains unchanged.

Anthropic Claude models that support the `effort` control receive
`reasoning_effort` as Claude `effort`: `low`, `medium`, and `high` pass through,
and both `xhigh` and `max` map to Claude `max`. DREAMLAND also requests adaptive
thinking
with summarized display so Claude `thinking` blocks can be captured as
`dreamland.reasoning_summary`. `none` is ignored with an informational log.
Anthropic fast mode can be requested with `speed = fast`; DREAMLAND sends
`speed: "fast"` and the `fast-mode-2026-02-01` beta header. As of August 2,
2026, Anthropic documents fast mode for `claude-opus-5` and
`claude-opus-4-8`. Requests for fast mode on `claude-opus-4-7` now fail.

Anthropic prompt caching defaults to `prompt_cache = automatic`, which sends
Claude's ephemeral cache hint on the stable system-prompt content block and uses
the provider's default 5-minute TTL. This is the lowest-cost Anthropic cache
duration and avoids caching the volatile command-state user message. Set
`prompt_cache = off` to disable DREAMLAND-managed Anthropic cache hints. The
`prompt_cache` setting is Anthropic-specific.

For OpenAI GPT-5.6-family, GPT-6 Astra, GPT-6 Sol, GPT-6.1 Sol, and GPT-6 Luna
command-emulator calls, DREAMLAND automatically uses OpenAI's explicit
prompt-cache mode. It marks the end of the rendered system
prompt as the single explicit cache breakpoint and sends a deterministic,
non-secret cache key derived from the provider, model, and exact rendered
system prompt. The projected world state and current command stay in the later
dynamic user message and are not part of that key. DREAMLAND omits a TTL so
OpenAI's current default minimum lifetime of 30 minutes applies; OpenAI may
retain the prefix longer. DREAMLAND sends no cache-retention or `extra_body`
workaround. This behavior is limited to model names `gpt-5.6`, `gpt-5.6-*`,
`gpt-6-astra`, `gpt-6-sol`, `gpt-6.1-sol`, `gpt-6-luna`, and their hyphenated
snapshot suffixes; older OpenAI models and Spiritguide do not
receive these explicit fields. xAI
continues to use its provider-default automatic caching behavior. A marked
prefix must contain at least 1,024 tokens to be cacheable.

An eligible cache miss may report cache-write tokens and incurs the
provider's cache-write price; later matching requests may report cache-read
tokens. DREAMLAND normalizes these as `input_cache_creation` and
`input_cache_read` in token logs. Cache availability remains opportunistic and
does not affect emulator correctness.

OpenAI-specific controls are `reasoning_summary`, `verbosity`, `openai_api`,
`openai_service_tier`, and `openai_reasoning_mode`. When these
are set for an `xai:` or `anthropic:` model, DREAMLAND ignores the unsupported
control and logs an informational provider-control message instead of failing
startup or command execution.

The following optional controls are available independently in `[agent]`,
`[spiritguide]`, and all four `[enrichment.*]` role sections:

| Setting | Accepted values | Blank behavior |
| --- | --- | --- |
| `openai_api` | `responses`, `chat_completions` | Preserve adapter selection for existing models; Astra selects Responses. A summary or explicit reasoning mode also selects Responses. |
| `openai_service_tier` | `auto`, `default`, `flex`, `fast`, `priority`, `ultrafast` | Omit the request field and inherit project configuration. Use `default` explicitly for a Standard-tier comparison. Ultrafast availability depends on the selected model; see [OpenAI Ultrafast mode](https://developers.openai.com/api/docs/guides/ultrafast-mode). |
| `openai_reasoning_mode` | `standard`, `pro` | Omit mode and preserve provider behavior. Pro is an execution mode on the chosen GPT-5.6/Astra model, separate from effort. |

Explicit Chat Completions requires blank `reasoning_summary` and
`openai_reasoning_mode`; incompatible settings fail during INI loading and
direct provider construction. For reasoning with tools, choose Responses.
These choices do not enable a tool loop, persisted reasoning, or hosted
delegation. OpenAI service tiers remain distinct from Anthropic `speed = fast`.
Unknown future model names remain configurable; this validation is not a
complete provider capability catalog. API/account availability still requires
a successful live request.

For example, these settings select the ordinary Astra execution mode and
Standard serving tier without requesting a summary:

```ini
[agent]
model = openai:gpt-6-astra
reasoning_effort = low
reasoning_summary =
openai_api = responses
openai_service_tier = default
openai_reasoning_mode = standard
max_output_tokens = 12800
```

Environment overrides use the existing role prefix plus the uppercase setting:
`DREAMLAND_OPENAI_API`, `DREAMLAND_SPIRITGUIDE_OPENAI_SERVICE_TIER`, or
`DREAMLAND_ENRICHMENT_EXPERT_OPENAI_REASONING_MODE`, for example. Blank INI
fields omit the setting. An explicitly empty or whitespace-only environment
value clears the corresponding model control, including an INI override or a
packaged default. An absent environment variable inherits the INI value or
role default.

Provider construction logs `model.configured` under configurable lifecycle
logging, with non-secret requested controls. Enrichment role logs and traces
also retain requested controls. Token logs include returned `service_tier`
when supplied, otherwise `unknown`; the requested tier does not prove the tier
used. Both successful cache reads and charged cache writes remain visible.
See [decision 0084](decisions/0084-multi-provider-model-evaluation-controls.md)
for the compatibility boundary and validation scope.

`reasoning_summary` defaults to `detailed` in both `[agent]` and
`[spiritguide]` so test sessions capture as much model behavior as supported by
the selected provider.

xAI exposes reasoning summaries as `reasoning_content`. DREAMLAND maps that
provider-specific field into the same `dreamland.reasoning_summary` metadata
used by OpenAI reasoning summaries when it is available.

Anthropic exposes summarized Claude reasoning as `thinking` content blocks on
models and requests that return them. DREAMLAND maps those blocks into the same
`dreamland.reasoning_summary` metadata when they are available.

The xAI and Anthropic providers require the `langchain-xai` and
`langchain-anthropic` packages. Both are included in the normal project
dependencies.

## EnrichmentAgent

The EnrichmentAgent performs bounded, session-local just-in-time or slight
read-ahead world enrichment. Every job has deterministic mode `focused` or
`ambient_presence`. Focused work uses the existing DREAMLAND-aware Coordinator;
ambient work uses a separately configurable DREAMLAND-aware Ambient Coordinator
to plan one recognizable named human, or a scenario-supported person-
equivalent, with time-qualified personal, work, or social activity and
connected observable residue. Services and machine identities can support but
cannot center that cluster. The Ambient Coordinator is not an autonomous
persona simulator and has no additional mutation authority.

The focused-or-ambient Coordinator branch, independent Expert, and
DREAMLAND-aware Integrator are explicit LangGraph roles. A visible context node
organizes planning and adds the mode plus deterministic target-host coverage
counts. A visible brief node converts the selected decision and session liberty
into a neutral six-field Expert input, and visible candidate-classification,
fresh-current-state, and reference-resolution nodes separate Expert from
Integrator. A visible normalization node separates parsed Integrator output
from final proposal validation. Expert items carry candidate origin and an
explicit reference basis. Integrator returns either a tagged no-op or a patch
whose contributions cite accepted candidates. Automatic mode is enabled by
default; manual jobs remain available. The current visible graph identifier is
`enrichment-v15`. The default `safe` mode permits only validated additive
host-local patches, while explicit `shadow` mode proposes and traces patches
without mutation.

For the active target host, enrichment context v4 also captures a bounded
foreground-session observation under the same short lock as world state. It
contains current login, interactive-program, bounded and deterministically
credential-sanitized arguments, prompt, selected shell-variable, cwd, and stack
information but excludes arbitrary environment/frame metadata, history, output,
and credential input. That projection boundary does not redact
or change DREAMLAND's intentional foreground credential recording for honeypot
and training uses. Coordinator sees it as direct planning
authority, Expert sees a neutral `active_session` observation within the same
six-field brief, and Integrator sees the fresh observation. Safe apply privately
fingerprints session semantics, excluding cwd, for relevant shell/identity
clusters and skips the atomic cluster as `semantic_authority_conflict` if the
foreground semantics change before apply. This is bounded active-session race
protection, not universal semantic validation or correction authority.

Context v4 additionally derives public foreground-evidence schema version 1
and private `foreground-evidence-v2` authority for every target host. It
recognizes the newest successful standalone `date` or
`timedatectl` observation and at most four distinct complete inventories from
the last four commands for `/home`, login history, current sessions, sockets,
or all processes. Compound, filtered, ambiguous, oversized,
structured-response-envelope-like, failed, interrupted, or stderr-bearing
results remain ordinary bounded context. Custom `date +FORMAT` output must
contain a calendar, year, or epoch conversion; literal and partial-clock formats
remain ordinary context. Public observations constrain exact chronology and
their stated snapshots without asserting permanent absence.
A private digest of the latest complete recognized record per exhaustive
`(kind, coverage)` scope across full history, latest host time, or a stable
empty-view sentinel is bound to every focused or ambient patch and revalidated
under the final apply lock. This catches a first, new-scope, replacement, or
new host-time observation that arrives after the Integrator snapshot. Aging an
inventory out of the four-command public window does not change private
authority. Its content is no longer prompt-visible as recent negative evidence
and is not a permanent absence fact; the retained fingerprint only prevents
unrelated command cadence from causing a false conflict.

With the default `max_inflight_per_session = 3`, automatic scheduling permits
three concurrent jobs and retains at most one deferred automatic candidate per
session. The deferred bound remains one if the in-flight limit is tuned. One
separate bounded marker retains the latest active host whose presence bootstrap
was not accepted; command completion, idle, and job completion provide
eventual-admission opportunities without creating a second general queue.
Active-host bootstrap wins equal-priority deferred contention against
discovered-host bootstrap. Trigger
derivation assigns `focused` or `ambient_presence` mode deterministically and
includes it in candidate identity so the two intents are independently
inspectable. Concrete host pivots, discoveries, probes, and subject attention
take precedence over recurring ambient opportunity; `session_idle` remains the
lowest priority. A lower-priority candidate from the same foreground
correlation is terminally suppressed. While capacity is full, higher priority
replaces the deferred candidate, a newer equal priority replaces it, and lower
priority leaves the existing candidate in place. Deferred candidates expire
after 60 seconds, are released exactly once after a job completion opens a
release opportunity, and recheck every current gate. A cooldown or
rate rejection at release is terminal; no retry timer is created. Manual jobs
are never deferred. A new accepted command cancels a deferred idle candidate
but not more specific deferred work. All scheduler state is process-local.

Automatic trigger configuration now includes the existing host discovery,
active-host change, state materialization, repeated surface focus, and idle
signals plus:

- `host_presence_bootstrap` for asynchronous baseline ambient work on a newly
  registered or approached host;
- `presence_probe` for broad inspections of users, login history, processes,
  connections, services, scheduled work, or event activity;
- `subject_first_touch` for focused read-ahead around a newly named concrete
  path or typed artifact;
- `materialization_followthrough` for ambient consequences of relevant
  foreground-emulator materialization; and
- `interaction_pulse` as the primary recurring ambient source based on
  deterministic interaction progress rather than typing speed.

`session_idle` remains enabled as a lowest-priority opportunity to use quiet
capacity. This priority governs correlated and deferred arbitration. An
eligible interaction pulse cancels its correlated idle timer or deferred idle
candidate. An accepted idle job still consumes ordinary shared scheduler
budgets but does not reset pulse progress. Explicit profile or Spiritguide direction for solitude, abandonment,
minimalism, pristine operation, or another incompatible setting outranks the
general ambient-population preference.

`interaction_pulse` becomes eligible after every four successful,
uninterrupted commands on one host. It has no elapsed-time gate and bypasses
the per-scope cooldown; global rate, in-flight, deduplication, and provider
gates remain. Only an accepted pulse resets that host's count, so accepted
bootstrap, probe, follow-through, or idle work cannot starve progress-driven
ambient work. Pulse priority remains 20, above only idle at 10.

For ambient jobs, concise `presence-gap-v2` supplies one
`independent_life` objective, a preferred existing technical surface, a
`named_human_or_person_equivalent_excluding_foreground_user_and_service_accounts`
subject preference, and bounded soft anti-repetition hints. A valid
current non-`shell` surface hint is retained. Otherwise deterministic fallback
chooses the least-covered of identity, service, and filesystem; identity uses
the independent-user-plus-group count, while service and filesystem use their
existing structural coverage counts. The Ambient Coordinator plans one
scenario-compatible chain from a recognizable named individual through time-
qualified personal, work, or social activity to connected observable residue.
It uses a named person-equivalent only when the scenario supports nonhuman
people and never treats a service or machine identity as the subject. This does
not create a `history-bearing_actor` key, count, or closed actor taxonomy.
Explicit profile or Spiritguide direction remains authoritative. Integrator
receives the same object only as non-evidentiary mapping priority and returns
`no_safe_mapping` for a service-only ambient report. The rule comes from
inspectable `enrichment_ambient_integrator_suffix.md` policy
`ambient-human-integration-v3`, appended only to ambient initial and refresh
calls; the shared prompt and focused Integrator system message are byte-stable.
The independent Expert
receives neither this policy object nor DREAMLAND schema; deterministic
`ambient-human-presence-v3` text is appended inside its ordinary question while
the neutral six-field brief and schema-v2 report remain unchanged. Focused jobs
do not receive the policy or appended text, and their role flow is unchanged.
The v3 policies retain v2's treatment of active, login, effective, and
privileged accounts plus established aliases as one foreground person. A directly observed login,
session, process, or socket row retains its username, PID, TTY, endpoints,
source, time, and observed or unknown owner; ambient material cannot transfer
that row to a new person unless the row itself identifies them. A distinct
historical or later event remains allowed when explicitly time-qualified and
not presented as the observed row, but it must be independently grounded. A
value learned only from the protected row cannot connect the proposed person to
that event unless established state, a supplied non-row observation, or a
selected reference separately supports the connection.

Expert-brief vocabulary checks are field-sensitive. Internal control markers
remain prohibited throughout, and routing-role markers remain strict in the
Expert question and object keys. Real-world `expertise.domain` and
`expertise.perspective` values may legitimately contain words such as
`coordinator`, `integrator`, or `emulator`; those ordinary titles are not
workflow leakage by themselves. This changes no role, state, or telemetry
schema.

The backend also derives bounded `interaction_focus` from command syntax and
working-directory state without model work. It carries working directories,
concrete paths, and typed named artifacts into Coordinator `wake_reason` as an
attention hint. It is not proof that an artifact exists and does not enter the
Expert reference catalog or broaden Integrator authority. Later same-host,
compatible-surface foreground matching still selects the newest eligible
completed job, then deterministically compares its causal focus with the
accepted-command focus as `artifact_exact`, `path_related`, or `surface_only`;
shared cwd by itself does not count as an artifact match.

Known terminal provider failures open a process-local provider circuit for
automatic enrichment. Exhausted credit/quota, authentication, authorization,
and configuration conditions currently suppress all later automatic jobs at
admission or job preflight before another role call, even when the open
provider belongs only to the other Coordinator branch. Foreground
failures open only a circuit for a provider used by the enrichment graph.
Invalid foreground failure metadata or a non-enrichment provider produces the
bounded DEBUG event `provider_circuit.ignored` instead of opening a circuit.
Opening emits `provider_circuit.opened` and cancels every idle timer and bounded
deferred candidate through `idle.cancelled` and `deferred.cancelled` with
`reason=provider_circuit_open`. Transient rate limits, timeouts, and
unavailability do not open that terminal circuit. Manual
enrichment continues to attempt the configured provider so an operator can
verify a corrected external condition, but success does not reset the circuit.
There is no timed reset. Service stop/start clears circuits and emits
`provider_circuit.reset reason=runtime_restarted`. That reset is process-scoped
and appears in the global runtime log rather than a session's retained
enrichment deque. Circuit opening, automatic suppression, and related
cancellations retain their originating session when one exists. Circuit
visibility is event/log based, not a runtime-status field or model-authored
failure explanation.

Each enrichment service start has a new process-local generation.
`stop(wait=True)` drains accepted work before returning. `stop(wait=False)`
invalidates prior workers and resets scheduler admission state immediately;
late results cannot mutate a session, reopen a circuit, or release deferred
work owned by the replacement generation. Event-subscription deliveries are
also generation-bound. A concurrent start while stop cleanup is active is
rejected instead of partially constructing a second runtime. Inspect
`runtime.started`, `runtime.stopped`, `job.completed`, and
`trigger.suppressed reason=stale_runtime_event` when diagnosing lifecycle
alignment. This protection is not restart persistence.

Enrichment derives its creative breadth from the live session's
`generation_policy.liberty`; there is no separate `[enrichment]` latitude knob.
The deterministic `creative-latitude-v5` mapping defaults missing or invalid
values to `moderate`. The internal level selects both an Expert generation
instruction and a separate Integrator retention instruction. Only the
instructions and hard limits cross their respective model boundaries; the
evocative level label remains runtime-only. Ordinary Enrichment logs record the
configured/effective level, defaulting, candidate and relation limits, template
and relation-policy versions, and instruction hashes without another model call
or model-authored logging.
Every v5 Expert instruction also deterministically requires one affirmative,
coherent realization instead of inspection advice, possibilities, or
alternative menus. Candidate and relation limits both scale as 2, 4, 8, 12,
and 24 from strict through abundant. The Expert receives the resolved limits as
`latitude.candidate_limit` and `latitude.relation_limit`. Higher levels
deterministically ask for connected hierarchy,
leaf material, lifecycle variation, causal relationships, and residue rather
than a flat inventory. Expert candidates and patch contributions share the
24-item abundant maximum. Conditions, gates, Coordinator anchors, candidate
basis references, and per-contribution source IDs retain their separate
eight-entry bounds.
Every relation must involve a returned item, and an item-to-reference relation
must repeat that unprefixed reference ID in the item's basis. Invalid auxiliary
relations, exact duplicates, and entries beyond the effective limit are
discarded individually in authored order with bounded deterministic DEBUG
detail events and one default-visible aggregate warning; valid candidate items
continue. Per-entry diagnostics are not promised at higher log levels.
`expert.relations_evaluated` reports received, accepted, invalid, duplicate,
overflow, and discarded counts plus the effective limit and
`expert-relations-v2` policy version without retaining relation text.
The policy version, limit, and six aggregate count fields are also optional
typed fields in retained session-enrichment events and their REST/MCP views;
they are null on unrelated events.
This relation-array policy does not change JSON parsing or add another model
call.
`safe` describes mutation authority and race handling, not a requirement that
selected plausible detail already exist in state.
Safe enrichment may store durable lifecycle patterns, temporally qualified
observations, and residue that exists now. It does not schedule appearance or
disappearance and must not persist a short-lived entity as unqualified
permanent-current state. The same constraint applies when ambient work proposes
an active process, login, job, or network connection: a bounded current snapshot
needs compatible direct evidence, lifecycle qualification, and durable logs or
residue. Ordinary invented texture should be historical, relative, or
recurring. True start/stop/expiry churn belongs to the future
`WorldActivityAgent`.

After Integrator, deterministic structural normalization prunes unsupported
array-valued leaves and objects made empty by that pruning. It drops a
contribution only when its complete value becomes empty and returns
`no_supported_contribution` if all are removed. Same-target contributions remain
valid only when each uses `merge_missing` and their flattened scalar leaf paths
are pairwise disjoint with no prefix collision; their separate source
provenance is preserved. Every other duplicate-target shape remains rejected.
Normalization makes no model repair call and cannot invent content, operations,
targets, or dependencies.

Enrichment context v4 retains the last four target-host command results as
bounded, recorded continuity evidence. stdout/stderr excerpts are capped at
1,500 characters per entry and 6,000 characters in aggregate, with newest
entries receiving budget first; peer command histories remain metadata-only.
The excerpts show what foreground output was recorded and do not prove that a
structured state update was accepted. `snapshot.captured` and
`expert_brief.prepared` log evidence counts, characters, and truncation without
logging the excerpt text. Surface logs distinguish the trigger's
`requested_surface` from the Coordinator's `selected_surface`; patch proposal
logs also report the concrete `registry_targets` and per-target counts.

After Expert classification, DREAMLAND captures fresh state and checks each
candidate's explicit basis references against their initial projected values.
A missing or changed reference blocks only candidates that name it; unrelated
candidates continue to Integrator. If none remain, the job ends as
`expert_no_eligible_items` without spending the Integrator call. The
`reference.currentness_evaluated` INFO event gives aggregate outcomes, while
`reference.currentness_detail` keeps bounded per-candidate decisions at DEBUG.

At final safe apply, dependency comparisons are projection-aware. DREAMLAND
privately fingerprints each complete dependency value from the current snapshot
while exposing only the sanitized value to the Integrator. The projected fields
and the complete-value fingerprint must both match at apply. Unchanged omitted
fields do not create false drift, but any field added or changed after the
snapshot does. Full dependency values and fingerprints are not emitted in
ordinary logs or model payloads.

The main controls are in `[enrichment]`; each role has the standard independent
model controls in `[enrichment.coordinator]`, `[enrichment.expert]`, and
`[enrichment.integrator]`. The sanitized baseline is:

```ini
[enrichment]
enabled = true
apply_mode = safe
triggers = host_discovered,active_host_changed,host_presence_bootstrap,presence_probe,subject_first_touch,state_materialized,materialization_followthrough,surface_focus,interaction_pulse,session_idle
idle_delay_ms = 5000
cooldown_ms = 30000
max_jobs_per_minute = 6
max_inflight_per_session = 3
event_log_capacity = 10000

[enrichment.coordinator]
model = openai:gpt-6-luna
openai_service_tier = fast
reasoning_effort = low
reasoning_summary =
verbosity = low
max_output_tokens = 2048
system_prompt = enrichment_coordinator.md

[enrichment.ambient_coordinator]
model = openai:gpt-6-luna
openai_service_tier = fast
reasoning_effort = low
reasoning_summary =
verbosity = low
max_output_tokens = 2048
system_prompt = enrichment_ambient_coordinator.md
```

The focused and Ambient Coordinators have independent model controls and both
use Luna with a 2,048-token baseline output limit. The Expert uses Terra and the
Integrator uses Luna with the same fields; their baseline output limits are
2,048 and 4,096 respectively,
and their `reasoning_summary` values are also blank. A Coordinator's
2,048-token cap leaves room for its compact final JSON after low-effort
reasoning; it does not request a verbose response. A blank enrichment reasoning
summary avoids requesting extra
model-authored explanation; structured runtime events and Langfuse boundaries
remain available without adding role output tokens.

The configured Integrator prompt remains the shared base; DREAMLAND appends the
packaged `enrichment_ambient_integrator_suffix.md` only for ambient initial and
refresh calls. There is no second Integrator role configuration.

Environment overrides for role settings use `DREAMLAND_ENRICHMENT_COORDINATOR_*`,
`DREAMLAND_ENRICHMENT_AMBIENT_COORDINATOR_*`,
`DREAMLAND_ENRICHMENT_EXPERT_*`, or `DREAMLAND_ENRICHMENT_INTEGRATOR_*` with
the normal model-setting suffixes.

`event_log_capacity` must be a positive integer and independently bounds each
session's retained enrichment-event deque. Its environment override is
`DREAMLAND_ENRICHMENT_EVENT_LOG_CAPACITY`. The 10,000-record packaged and
development default does not change `[console] log_capacity`, which continues
to size ordinary console and session-trace retention.

`cooldown_ms` governs ordinary automatic scopes. It does not impose a cooldown
or elapsed-time interval on `interaction_pulse`; the pulse remains bounded by
its four-command progress counter and the global rate, in-flight,
deduplication, and provider gates.

Runtime role models are lazy. Merely using `enrichment status` or
`enrichment graph` does not initialize provider clients; the first admitted
automatic or manual job initializes a model only when its role is reached.

See `docs/ENRICHMENT_AGENT.md` for trigger semantics, all role settings, the
short-lock sequence, safe-patch authority, stable reason codes, logging fields,
and manual verification.

### Person-Centered Ambient And Authority-Refresh Manual Checkpoint

Verify person-centered ambient work, independently grounded historical events,
field-sensitive practitioner titles, and the one-attempt authority refresh.
Include nonhuman-person and explicit-solitude controls:

1. In a disposable safe-mode session with DEBUG enrichment logging, temporarily
   enable only `interaction_pulse,session_idle`. Run two successful commands,
   allow one idle job, then run two more. Verify one pulse is attempted on the
   fourth command, only accepted pulse logs `interaction_pulse.counter_reset`,
   and idle did not erase progress.
2. Restore the full trigger set, fill all three in-flight positions, and pivot
   to a newly known host. Verify `bootstrap.retry_*` retains and eventually
   admits the active host independently of the one general deferred candidate.
3. Run bounded standalone `date`, `/home` listing, login/session, socket, and
   process inventories, then inspect an ambient Langfuse trace. Confirm graph
   version, `presence-gap-v2`, its exact
   `named_human_or_person_equivalent_excluding_foreground_user_and_service_accounts`
   preference, typed host-time/inventory observations, and the unchanged
   neutral six-field Expert brief and schema-v2 report. Confirm the ordinary
   question includes deterministic `ambient-human-presence-v3` guidance only
   for ambient work, and Integrator appends `ambient-human-integration-v3` only
   for ambient initial and refresh calls. Verify ordinary expertise titles such
   as `OT operations coordinator`, `industrial systems integrator`, and
   `embedded emulator specialist` are accepted while internal workflow terms
   remain forbidden in Expert questions and object keys.
4. Inspect applied registries and repeat foreground probes. Look for one
   recognizable named human other than the foreground user—or a scenario-
   supported named person-equivalent—linked through time-qualified personal,
   work, or social activity to connected residue. Verify that a service account
   cannot substitute for the individual and service-only material maps to a
   typed no-op. Verify no directly observed login, session, process, or socket
   row is transferred to that person unless the row names them. A distinct
   historical or later event must be explicitly time-qualified, independently
   grounded, and not presented as the observed row. It must not reuse a value
   learned only from that row unless established state or a selected non-row
   reference separately supports the connection. Also verify exact chronology
   is consistent
   with host time and additions after complete snapshots are lifecycle-
   qualified. Supported bounded current observations are allowed; autonomous
   advancement and expiry are not.
5. Let a recognized inventory age out through unrelated commands and confirm
   its public prompt observation disappears while the private v2 authority
   remains unchanged. Then add a new recognized observation during focused and
   ambient final race windows. As a regression of the three successful v14
   examples, confirm exactly one Integrator-only refresh uses
   a fresh snapshot and the same Expert report, then passes through ordinary
   normalization, validation, and safe apply. Force another evidence change
   before the refreshed apply and confirm the second conflict is terminal. An
   active-host pivot must remain terminal without refresh. Logs should expose
   `integrator_refresh_attempt`, `authority_domain`,
   `integrator_refresh_outcome`, `integrator_refresh_latency_ms`, `eligible`,
   `changed`, hashes, counts, policy, action, and reasons without raw output or
   repetition-hint names.

## Output Safeguards

Each model call defaults to a configured output-token cap. Set
`max_output_tokens` to a positive integer to cap output. Set it blank or `0` to
remove the cap.

Reasoning summaries for supported OpenAI reasoning models can be requested with
`reasoning_summary = auto`, `concise`, or `detailed`, or left blank to avoid
requesting a summary. The EnrichmentAgent baseline leaves it blank for both
Coordinator configurations and the shared Expert and Integrator.

The `[spiritguide]` config section accepts the same model-control fields as
`[agent]`: `model`, `system_prompt`, `reasoning_effort`, `reasoning_summary`,
`verbosity`, `speed`, `prompt_cache`, `max_output_tokens`, `openai_api`,
`openai_service_tier`, and `openai_reasoning_mode`. These settings
are intentionally separate so operators can run a higher-capability
state-guidance model while using a lower-cost command-emulation model.
Spiritguide defaults to a 16,384-token output cap, compared with 12,800 for
command emulation, because state patches can include generated users, files,
hosts, services, and network facts.

## Runtime Logging

Logging uses standard Python logging with DREAMLAND event categories. Supported
event filters are:

- `lifecycle`
- `commands`
- `replies`
- `tokens`
- `console`
- `failures`
- `mcp`
- `enrichment`

The console logging command reads from a bounded in-memory buffer. Reply logs
store a configurable one-line excerpt rather than full command output.
SSH host-key startup emits a lifecycle record containing only the generated or
loaded disposition, configured path, public algorithm, and public SHA-256
fingerprint. It never records private key contents.
Provider/model-call failures are logged under `failures` and converted into a
terminal error instead of escaping through SSH session handling.
Provider failures also receive a bounded provider-neutral kind and disposition.
Structured provider codes take precedence over HTTP status so terminal credit
or quota exhaustion reported as 429 is not mistaken for transient throttling.
Free-form provider error text and response bodies do not enter retained
enrichment-event fields.
Token logs include provider-reported aggregate token counts and cache telemetry
when available, including `input_cache_creation` and `input_cache_read`.
They also record a projection-prepared event with total, detailed, and compact
host counts plus full, projected, and saved serialized world-block character
counts. These events contain no world-state contents or host references and do
not claim that character savings equal token savings.
MCP SDK request/session lifecycle logs are routed under `mcp` so they can be
inspected through the normal logging buffer without writing directly to the
interactive console.

Session traces use a separate Python logging handler and are not included in
the normal runtime log stream or the `logging` console command. Each session has
segregated trace records for submitted input, rendered output written back to
the user, and reasoning summaries when a model result includes one.

Enrichment lifecycle events also use a separate structured in-memory handler in
addition to their normal runtime log lines. Each session receives a bounded
deque using positive integer `[enrichment] event_log_capacity`, default 10,000;
the ordinary console and session trace continue to use
`[console] log_capacity`. Optional exact job filtering prevents one job's
trigger, lock, graph, patch, and readiness chain from being confused with
another. The REST enrichment-events endpoint and MCP
`dreamland_session_enrichment_events_read` return retention and eviction
metadata. Records remain available after session close but not after process
restart. This deterministic copy performs no model call and consumes no model
tokens. Logging enablement, level, and the `enrichment` event category still
govern what is retained. Event entries expose bounded correlation, causation,
enrichment mode, selected Coordinator role, surface, decision, candidate
priority and age, deferred/replaced candidate,
normalization, presence-gap, foreground-evidence, pulse-reset,
bootstrap-retry, and registry-target fields directly while retaining the
unchanged grep-friendly line. Scheduler and focus diagnostics include
`replaced_candidate_id`, `retained_candidate_id`, `completed_job_id`,
`higher_priority_trigger`, `retained_trigger`, `focus_key_sha256`, `priority`,
`retained_priority`, `age_ms`, `max_age_ms`, `focus_path_count`,
`focus_artifact_count`, and `accepted` when applicable. Deferred arbitration
may also retain the existing and incoming focus hashes and counts. Match
records add the deterministic policy/classification, the causal job's expected
focus hash/counts, and the accepted command's observed focus hash/counts.
Provider records add bounded kind, disposition, provider/model, safe
status/code, circuit origin, and circuit count. Authority
records add bounded domain, policy, binding reason, dependency count,
presence/change, and action fields without the foreground payload or digest.
Presence-gap records retain `presence_gap_policy_version`,
`presence_gap_sha256`, `presence_gap_ambient_objective`,
`presence_gap_preferred_surface`, and the bounded
`presence_gap_anti_repetition_*_count` fields without state-derived subject or
system names.
Foreground-evidence records retain policy and public-contract hash, whether
host time was observed, and complete-inventory count as typed fields. The
unchanged formatted log line may also contain bounded generic evidence kinds;
they are not promoted into typed retained/admin fields. Neither form includes
raw command output or the private apply digest.
Integrator-refresh records retain `integrator_refresh_attempt`,
`integrator_refresh_outcome`, `integrator_refresh_latency_ms`, `eligible`, and
`changed`, while reusing the existing `authority_domain`, status, reason, and
action fields. A formatted `conflict_domains` detail can describe a multi-domain
ineligible decision but is not promoted into typed retained/admin fields.
Expert human-task policy version and action use the existing retained fields;
policy hashes and character counts, plus all namespaced ambient-Integrator
policy diagnostics, remain formatted/trace-only. None includes appended prompt
prose.
Deferred records have no job id until one-shot release is accepted, so inspect
the session-wide view for their earlier lifecycle. The `deferred.scheduled`
record carries the newly accepted job id and begins exact-job correlation; it
also retains the completed job id that opened the release attempt. Routine
unchanged snapshot checks use `snapshot.compared` at `DEBUG`;
`snapshot.drifted` is reserved for an actual world, target, or foreground
authority change and is elevated to `INFO`.

Critical lifecycle, command, model-call, failure, interruption, and
resource-usage events should continue to be logged as new features are added.
Logging levels and event types must remain configurable.

## Docker Deployment

Use Docker Engine 28.0.0 or newer and the Compose plugin. The
[beta deployment guide](BETA_SSH_DEPLOYMENT_PLAN.md) documents package installation,
profile selection, credential setup, backups, upgrades, rollback, and world restore
through [`deploy/dreamland-deploy`](../deploy/dreamland-deploy). The wrapper
preserves existing configuration and credentials on repeated installation.
Offline validation does not verify provider access or deployed capacity.

The separate [metadata API setup](BACKEND_DEPLOYMENT_UI_HANDOFF.md#metadata-api-setup-and-handoff)
initializes attendee/operator credentials, describes their private endpoints,
and probes both roles. These listeners expose deployment identity, health, and
readiness only; domain capabilities remain disabled.

### Docker Operator Console

From the deployed source directory:

```sh
./deploy/dreamlandctl list
./deploy/dreamlandctl console
./deploy/dreamlandctl console --command 'sessions --live'
./deploy/dreamlandctl status
./deploy/dreamlandctl logs
./deploy/dreamlandctl stop
./deploy/dreamlandctl restart
```

The wrapper runs a client inside the existing SSH container, connecting to its
live console and session registry. Project selection uses `--project NAME`, then
`DREAMLAND_PROJECT`, then the only existing DREAMLAND SSH Compose project.
Discovery includes stopped services and excludes setup/one-off containers.
A missing or ambiguous project requires explicit selection. `list` prints all
discoverable projects without selecting one or invoking Compose.

`--env-file FILE` or `DREAMLAND_DEPLOYMENT_ENV` selects a Compose environment
file; the default is `/dev/null`, so a local `.env` is not loaded implicitly.
Exported Compose variables still apply. Project selectors are independent of
this environment file. Pin `DREAMLAND_BACKEND_IMAGE` for setup/model commands.

Interactive `console` requires a terminal; `--command` runs once and disconnects.
Follow commands in one-shot mode return only their initial snapshot. `disconnect`
or Ctrl-D closes the client while attendee sessions continue. Ctrl-C returns
from follow/refresh to the prompt. A disconnected request may still complete;
the client does not cancel mutations or retry them automatically. Reconnection
starts fresh operator context. Only one operator connection is admitted, and a
mutation still running after disconnect retains admission until completion.

The console uses the private Unix socket `/run/dreamland/console.sock`, with
Linux peer-UID validation and owner-only directory/socket permissions. It exposes
no TCP listener and grants no authority through attendee credentials. Remote
`shutdown` directs operators to the host `stop` or `restart` command. `restart`
restarts the existing container; a new image requires container recreation.
`status` reports container state, not provider readiness.

Outside Docker, enable the same console explicitly in daemon mode:

```sh
python -m dreamland --daemon --console-socket /absolute/private/run/console.sock
python -m dreamland.console_client --socket /absolute/private/run/console.sock
```

The socket directory may be created automatically; its parent must already exist.
Normal startup retains its stdin/stdout console. The alternate client entry point
`python -m dreamland.deployment console` does not read deployment credentials.

### Adopt Model Defaults

An existing INI retains its explicit values after an image upgrade. Settings
omitted from that INI inherit the new package defaults. Review the effective
settings before startup and explicitly adopt shared model controls when desired:

```sh
export DREAMLAND_BACKEND_IMAGE=REVIEWED_IMAGE_ID
./deploy/dreamlandctl --project dreamland model-defaults
./deploy/dreamlandctl --project dreamland stop
./deploy/dreamlandctl --project dreamland model-defaults --apply
```

Preview reads settings without changing files. Apply runs in an offline setup
container and refuses a running SSH service. Stop any other process using the
same deployment root too. Apply preserves non-model INI values, credentials,
worlds, and deployment policy, but rewrites INI formatting/comments. It retains
an exact `0600` original at `dreamland.ini.before-model-defaults-HASH` before
atomic replacement. That backup can contain secrets. Repeated application is a
no-op once settings match. Recreate the service with the reviewed image and
matching Compose settings afterwards; the adoption command does not start it.
See [decision 0107](decisions/0107-shared-model-defaults-and-deployment-adoption.md).

## Dreamland Console

The operator console is process-local. Ordinary startup opens it on stdin/stdout;
daemon mode can expose it through an optional private Unix socket. Docker
operators use `./deploy/dreamlandctl console` to connect to that same runtime.
See [Docker Operator Console](#docker-operator-console) for project selection,
disconnect behavior, host stop/restart, and [model adoption](#adopt-model-defaults).
SSH sessions cannot run console commands; the `!dreamland`
namespace is reserved and rejected at the SSH session boundary.

Core commands:

- `logging`
- `logging -n NUM`
- `logging -f`
- `trace`
- `trace SESSION_ID`
- `trace -n NUM`
- `trace -f`
- `sessions`
- `sessions --set SESSION_ID`
- `sessions --clear`
- `enrichment status`
- `enrichment status -f`
- `enrichment run [--session SESSION_ID] [--host HOST] [--surface SURFACE]`
- `enrichment jobs -n NUM`
- `enrichment graph`
- `spiritguide INSTRUCTION`
- `spiritguide --dry-run INSTRUCTION`
- `spiritguide --show-patch INSTRUCTION`
- `spiritguide --session SESSION_ID INSTRUCTION`
- `spiritguide profile create NAME INSTRUCTION`
- `spiritguide profile update NAME INSTRUCTION`
- `spiritguide profile fork SOURCE_NAME NAME [INSTRUCTION]`
- `spiritguide profile create NAME --level world|system --dry-run INSTRUCTION`
- `commands`
- `inspection`
- `state-delta`
- `state-profile save NAME`
- `state-profile save NAME --level exact|system|world --comment TEXT`
- `state-profile list`
- `state-profile show NAME`
- `state-profile show NAME --json`
- `state-profile apply NAME`
- `state-profile apply NAME --session SESSION_ID`
- `state-profile default`
- `state-profile default set NAME`
- `state-profile default set NAME --persist`
- `state-profile default clear`
- `state-profile delete NAME`
- `langfuse flush`
- `shutdown`

`sessions` lists Langfuse sessions. Its `env` column is the Langfuse runtime
environment, while the `profile` column shows the live Dreamland state profile
for sessions that are still registered in the running process.

`trace` defaults to the active local-console session. Pass a session id as the
optional positional argument to inspect a different retained session trace.

`enrichment status` reports runtime, automatic-mode, apply-mode, and job counts.
`enrichment status -f` (or `--follow`) enters the terminal's alternate screen
and redraws that status from the home position on a cleared display every
second until any key is pressed. It requires a usable interactive terminal;
local input/output must be terminals and `TERM` must not be missing or `dumb`.
Redirected or incapable terminals receive a clear error instead of terminal-
control output. On keypress, shutdown, or status failure, DREAMLAND restores
the main screen in guaranteed cleanup and prints one final status snapshot
there before the prompt resumes.

The stop key is consumed. Escape and encoded multibyte keys may arrive in
separate reads, so DREAMLAND drains the terminating key sequence within bounded
time and byte limits without waiting indefinitely; the bounded quiescence
window limits how much adjacent input can be consumed. The initial command
result retains ordinary tracing semantics. Refresh controls and subsequent
frames, including the final presentation snapshot, are untraced transport
writes. Following polls the process-wide status gauge and does not replace
retained session/job enrichment events or Langfuse traces for exact lifecycle
analysis. It performs no model calls and does not change enrichment scheduling.

Logs contain one bounded refresh-start record, one refresh-stop record, and at
most one terminal-rejection or status-read failure record for the corresponding
condition; successful polls are not logged individually. A read failure shown
to the operator or retained in logs contains only a stable failure label and
exception type, never exception prose, provider text, response bodies, or a
traceback.

`enrichment run` asynchronously submits one manual job for a live selected or
explicit session and defaults to its active host. `enrichment jobs` shows recent
in-memory outcomes and timing. `enrichment graph` prints two labelled expanded
Mermaid documents: the main proposal graph and the bounded attempt-1
Integrator-refresh graph. Manual submission is available when automatic
enrichment is disabled; global `shadow` or `safe` apply mode still applies.

Spiritguide invocations also add concise session trace records showing the
instruction preview, dry-run status, structured-response status, patch sections,
and apply result.

`spiritguide` sends an operator instruction to the selected live session's
spiritguide. The spiritguide can return a session state update; DREAMLAND
applies it to that session unless `--dry-run` is set.
If the model response is not valid structured state, DREAMLAND reports that no
state changes were applied.
Each response includes a concise result summary showing the parsed state-update
sections, whether the update was applied, dry-run status, and structured
response status. Dry runs also print the parsed `state_update` JSON so the
operator can inspect proposed changes before applying them. Use `--show-patch`
on non-dry-run calls to print the same parsed patch after applying it.

`spiritguide profile` makes world-building a detached state-profile workflow.
`create` starts from a blank/default world state and saves a new state profile.
`update` loads an existing state profile, runs a follow-up Spiritguide
instruction against that detached state, and saves it back. `fork` copies an
existing profile to a new name and can optionally run another Spiritguide
instruction before saving the fork. The default profile level is `world`, which
keeps durable world facts and registered generated artifacts while excluding
session-specific details such as cwd, shell stack, and command history. Use
`--level system` when the intended reusable profile should also carry system
identity fields such as host identity. Spiritguide authors the required initial
login banner as world state; deterministic validation rejects incomplete or
malformed results before saving. Dry runs expose those validation failures too.
An old schema-1 profile cannot be used as an update/fork source until it is
repaired offline or regenerated.

`state-profile` saves live session state into local JSON files under
`state_profiles/`. These files are operator runtime data and are ignored by git
because exact profiles can contain command output, generated scenario details,
connection metadata, and other local state.

State profile depths:

- `exact`: preserves the session-owned state DREAMLAND can serialize, including
  host identity, current user, current directory, shell stack, active program,
  shell environment, environment metadata, world state, and command history.
  Current exact profiles restore command/result history used by the command
  agent, but they do not yet restore the live readline up-arrow/down-arrow
  editor recall buffer.
- `system`: preserves the same simulated system and world artifacts, including
  host identity and environment metadata, but omits command history and
  shell-position details so it can seed a fresh fictional SSH session.
- `world`: preserves high-level world anchors such as purpose, simulation
  policy, known hosts, known services, registered generated
  artifacts, and high-level facts while dropping live session position and
  command history.

`state-profile apply NAME` fully replaces the selected live session's state
with the saved profile while preserving the live session id used for tracing.
The next command sends a full session state refresh to the command agent so
checkpointed agent state can realign with the operator-applied profile.
Applying a profile also writes a concise session trace entry. Native console
calls use the active session by default and can pass `--session SESSION_ID`.

Set `[state_profile] profile = NAME` or `DREAMLAND_STATE_PROFILE=NAME` to boot
new sessions from a saved profile under `state_profiles/`. Booted sessions keep
their fresh live trace id and current SSH connection metadata. `exact` profiles
restore their saved fictional user and host identity. `system` profiles seed a
fresh login shell using the current SSH login username while preserving the
saved host identity and system state. `world` profiles do not carry user or
host identity fields outside world state, so a fresh session takes its host and
shell from the saved initial active host and its username from the entry path.
Its saved host-local banner supplies the initial presentation. Later simulated
returns to that host use normal LLM login emulation.

`state-profile default set NAME` changes the runtime default for future SSH
sessions in the running process. It does not change existing live sessions and
does not persist across restart unless `--persist` is passed. `--persist`
updates the config file DREAMLAND loaded at startup, including paths supplied
through `DREAMLAND_CONFIG`. `state-profile default clear` removes the runtime
default and can also persist the cleared setting with `--persist`.

`state-profile delete NAME` removes the saved JSON profile. If the deleted
profile is the current runtime default, DREAMLAND clears that runtime default.
Pass `--persist` to also clear the persisted config setting when deleting the
active default.

DREAMLAND records the last state profile booted or applied for each session in
`state_profiles/session_index.json`. The `sessions` command uses this local
index to keep showing the last known state profile even after a session is no
longer live in the current process.

## Langfuse Tracing

Langfuse tracing is optional and should stay optional. Configure it in
`dreamland.ini` or matching environment variables:

```ini
[langfuse]
enabled = true
public_key = pk-lf-example
secret_key = sk-lf-example
host = https://cloud.langfuse.com
```

When enabled, DREAMLAND creates one stable Langfuse session id per SSH or MCP
session and records a lightweight `dreamland-session-start` event before the
first command runs. Each LangGraph command invocation creates its own Langfuse
trace and propagates the stable session id as `sessionId`, so Langfuse groups
the command traces under one session. Console introspection uses those
session-grouped command observations for command lists, inspection, token/cost
summaries, reasoning summary availability, and state diffs.
Command trace metadata also includes `dreamland.context_projection.*` fields
for projection mode, active/detailed host references, host counts, and
full/projected/saved world-block character counts. Metrics are prepared from
the incoming authoritative command context; model-backed traces can be compared
with their recorded prompt and provider token usage. Count values are encoded
as decimal strings because Langfuse propagated metadata accepts string values;
the corresponding `tokens` log fields retain numeric logging semantics.

Completed emulator commands can receive categorical `dreamland.json_status`
scores (`repaired` or `rejected`) through the external
[post-run JSON annotation tool](EMULATION_JSON_ANNOTATIONS.md). It uses existing
capture, defaults to dry run, and requires `--apply` to write scores. These are
Langfuse evaluation annotations, not native trace Status or tag changes. The
tool adds no processing or network calls to interactive commands and requires
no server restart. Missing scores require checking coverage rather than assuming
that a response needed no intervention.

Model-backed spiritguide calls attach to the same Langfuse session as sibling
operator observations named `dreamland-spiritguide-agent`. They include the
operator instruction, spiritguide prompt context, raw model output, parsed state
update, patch keys, and dry-run status. They are not listed as terminal commands.
The full operator instruction is stored in observation input; propagated
metadata stores only a bounded instruction preview, length, and hash.

Model-backed enrichment jobs use a runtime-owned parent named
`dreamland-enrichment-agent` and are grouped under the targeted live session
id. Its child proposal workflow is named
`dreamland-enrichment-proposal-graph`. The trace preserves the expanded
initial-snapshot, Coordinator-context preparation, explicit focused-or-ambient
Coordinator branch, Expert-brief preparation, Expert, current-snapshot,
reference-resolution, Integrator, and structural-normalization and validation
topology. It records graph, job, trigger, mode, target, deterministic coverage
summary and hash, ambient presence-gap metadata, typed foreground-evidence
summaries, interaction focus, and per-role token metadata. The parent
remains open through deterministic application and
closes with graph and application status, reason, state-change result, selected
surface, and duration. Checkpoint persistence is not configured; recent job
records are in-memory, and neither active work nor job history is recovered
after restart.
When a genuine foreground-evidence apply conflict is eligible, a sibling child
workflow named `dreamland-enrichment-integrator-refresh-graph` captures fresh
state, resolves references, and calls only Integrator before normalizing and
validating the refreshed proposal. It is tagged `integrator-refresh`, carries
attempt 1, and reuses the prior Coordinator and Expert material. Runtime events
retain `integrator_refresh_attempt`, singleton `authority_domain`,
`integrator_refresh_outcome`, `integrator_refresh_latency_ms`, `eligible`,
`changed`, status, reason, and action without raw authority values. The
formatted line may additionally include `conflict_domains` for an ineligible
multi-domain decision. A second conflict and every active-host pivot remain
terminal.
For active-target jobs, role inputs show the bounded foreground observation at
the Coordinator, neutral Expert, and fresh Integrator boundaries. The hidden
authority fingerprint remains runtime-owned and is not a role output or
ordinary trace field. Terminal provider-circuit suppression occurs before a
suppressed automatic job enters model work and is inspected through retained
enrichment events.
The dedicated enrichment event buffer is likewise process-local, but unlike the
shared runtime log ring it is isolated per session and exposes exact job
filtering plus explicit eviction counts through REST and MCP.

## Maintenance

Update this document when configuration keys, console commands, logging events,
model controls, tracing behavior, or process modes change.
