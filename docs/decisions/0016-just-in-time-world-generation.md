# 0016 Just-In-Time World Generation Directives

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators, and coding agents
Purpose: record the schema direction for high-level world guidance and
runtime-materialized artifacts.

## Overview

DREAMLAND should generate the world just in time. Spiritguide should guide the
shape, scale, and realism of a scenario without precomputing every employee,
host, file, ATM, workstation, or network device. The command emulator should
turn that guidance into concrete artifacts only as user commands expose or rely
on them.

Prior saved state profiles showed the risk of over-materialized world state.
The `bank` system profile contained many concrete hosts and artifacts and its
first inspected command used about 13k input tokens. Some sections, such as
branch and ATM inventory ranges, already showed the better pattern: approximate
counts, naming conventions, and examples without enumerating every endpoint.

## Decision

Keep existing keyed runtime collections as materialized registries:
`hosts_by_name`, `services_by_name`, `generated_artifacts_by_path`,
`established_facts_by_id`, and similar future collections. These collections
represent facts the emulator has already committed to the user's world.

Add high-level directive state under `runtime_state`:

- `generation_policy` records the materialization strategy and liberty level.
- `expansion_directives_by_id` records reusable world-building guidance keyed
  by stable directive IDs.

The supported liberty levels are `strict`, `conservative`, `moderate`,
`expansive`, and `abundant`. The default is `moderate`, the middle level.
`strict` is for worlds that are already heavily established and should avoid new
invention. Higher levels permit broader surrounding infrastructure and more
decisive representative choices when they are consistent with operator
direction and interaction context.

Spiritguide should normally write broad scenario changes into
`expansion_directives_by_id` rather than pre-materializing large inventories.
Directives can describe types of infrastructure, approximate counts, topology,
technology families, vendor families, OS families, naming conventions,
site/branch composition, population shape, network boundaries, operational
constraints, and materialization guidance.

Runtime materializers should create concrete details just in time. The command
emulator remains the foreground materializer; the session-local EnrichmentAgent
can add bounded finishing detail immediately ahead of likely user attention.
They should register major systems, important services, and files the user
interacts with while avoiding every incidental listing row, scan result,
generated table item, or example that the user has not acted on.

An independent domain role need not receive DREAMLAND's policy schema. A
deterministic boundary may translate the liberty level into versioned,
task-neutral creative latitude while preserving the same breadth and invention
semantics.

World-level state-profile saves preserve `generation_policy`,
`expansion_directives_by_id`, and registered generated artifacts. Broad
scenario guidance should still live in directives, but artifacts that have
already been registered under `generated_artifacts_by_path` are considered
durable world facts that can seed future sessions.

## Consequences

Prompt management can now distinguish world direction from materialized facts.
This should reduce prompt bloat, preserve emulator imagination, and support
large immersive worlds without forcing Spiritguide to enumerate them up front.

The schema remains a convention inside the existing flexible `world_state`
object. There is no runtime validator yet. Future work should add directive
validation, artifact pruning, and context selection that curates only the
world-state slices relevant to a command.

## Provider Dry-Run Findings

Dry-run Spiritguide tests on July 4, 2026 compared the same broad regional bank,
municipal emergency services, strict-liberty, and manufacturing prompts across
xAI, OpenAI, and Anthropic providers. These findings are experimental but useful
for prompt and schema design.

xAI followed the directive-first intent more closely after the prompt added
explicit just-in-time defaults. It produced valid nested JSON for the bank and
municipal cases, kept broad scenarios mostly in
`expansion_directives_by_id`, and used `generation_policy` as an object. It
still over-materialized a manufacturing retheme by adding concrete hosts,
services, and users, so directive-first behavior is not fully reliable.

OpenAI followed the schema most reliably. It avoided dotted keys and returned
`generation_policy` as an object, including `materialization:
just_in_time`. However, it strongly tended to over-materialize rich scenario
state for broad rethemes, adding many concrete hosts, services, users, groups,
files, inventory ranges, and operational-calendar entries even without an
operator request for exact inventory.

Anthropic understood the directive-first concept in the regional-bank dry run
and explicitly described just-in-time generation, but it still added concrete
anchor network devices and legacy-style `inventory_ranges` outside the new
`runtime_state` directive layer. For larger municipal and manufacturing
rethemes, Anthropic generated very large JSON-like patches that were malformed
or truncated before the parser could accept them, so DREAMLAND correctly applied
no state update.

These results suggest the single-prompt goal may remain viable, but provider
differences are meaningful:

- schema examples and hard JSON-shape rules help all providers;
- OpenAI may require stronger language that broad rethemes should not touch
  materialized registries unless explicitly requested;
- Anthropic may require stricter size and minimal-patch instructions, or a
  review/apply workflow that asks for smaller staged patches;
- xAI currently appears closest to the desired directive-first behavior, but
  still needs guardrails around broad manufacturing/industrial scenarios.

Provider tuning should avoid overfitting to one dry-run output. If the same
failure pattern persists across several operators and scenarios, update the
shared prompt first. If one provider continues to diverge, consider
provider-specific prompt modules only after the shared schema and examples are
exhausted.

Prompt review after these dry runs found that the shared Spiritguide prompt
still mixed directive-first guidance with language that could encourage
pre-materialized richness. Phrases about deep immersive solutions, strong
anchors, artifacts as either concrete objects or guidance, and broad
supersession of stale facts gave providers plausible reasons to write concrete
hosts, services, users, files, and cleanup patches for broad rethemes.

The shared prompt should therefore use a clear target hierarchy for
Spiritguide state updates:

- `runtime_state.generation_policy` for materialization strategy and liberty;
- `runtime_state.expansion_directives_by_id` for broad world-building guidance;
- `runtime_state.established_facts_by_id` for small high-signal facts that must
  remain stable;
- materialized runtime registries only for exact entries requested by the
  operator or specific committed items needed immediately.

This hierarchy preserves creativity at the directive layer while making large
materialized registries exceptional in Spiritguide output. Broad rethemes should
normally replace high-level identity facts and expansion directives rather than
enumerating cleanup across old generated registries.

Post-rewrite dry runs on July 4, 2026 showed mixed improvement:

- Anthropic handled the already-bank-shaped regional bank prompt with valid
  directive-first output, but the municipal emergency-services retheme still
  produced a very large malformed patch and was stopped there.
- xAI returned compact valid patches across the bank, municipal, strict-liberty,
  and manufacturing prompts. It avoided large materialized registries, but
  often used a broad `world_state.__dreamland_replace__` patch for rethemes.
- OpenAI improved on the bank prompt by mostly using directives, but still added
  a concrete active-user registry entry. Its municipal emergency-services
  retheme remained large and over-materialized hosts, services, users, groups,
  artifacts, and operational details, although the JSON was accepted. Testing
  stopped there.

The shared prompt rewrite appears to help xAI most and partially helps OpenAI.
Anthropic's large-patch failure likely needs an additional control beyond
wording, such as explicit maximum patch sizing, staged retheme workflows, or
runtime validation/retry that rejects oversized materialized registry updates
for broad scenario requests.

A limited Anthropic high-reasoning dry run on July 4, 2026 tested the municipal
emergency-services retheme with `reasoning_effort = high` and a reduced
Spiritguide output cap of 4096 tokens. The response still hit the output cap,
returned malformed JSON, and proposed broad replacement of contradictory
registries. Langfuse reported only input, output, total, and cache token usage
categories; no reasoning/thinking summary or separate reasoning token category
was captured. This does not support reasoning effort alone as a sufficient fix
for Anthropic's large-patch behavior.

Additional provider comparison on July 4, 2026 tested regional healthcare,
university research campus, and electric utility rethemes with a bounded
4096-token Spiritguide output cap. xAI `grok-4.3` returned accepted, compact
patches for all three scenarios. The patches focused on scenario identity,
small established facts, and expansion directives. They still used some broad
delete or replace directives, but did not pre-materialize large
host, service, user, group, or artifact registries. Langfuse reported roughly
7-11 seconds latency and 672-847 visible output tokens per run.

OpenAI `gpt-5.5` did not complete the same healthcare retheme under a 4096-token
cap: it spent the full cap as `output_reasoning` and returned no visible text.
A retry with an 8192-token cap behaved the same way, again returning no visible
text and no accepted state update. Earlier uncapped/12800-token OpenAI dry runs
could produce valid JSON, but tended to over-materialize broad scenario state.

A follow-up OpenAI run used `reasoning_effort = medium`, `verbosity = low`, and
a 32768-token cap to separate reasoning budget from visible output. Under that
configuration, OpenAI produced accepted healthcare and university rethemes. The
visible patches were detailed and mostly directive-oriented, but much longer
than the comparable xAI patches: roughly 2750-2777 visible output tokens and
43-47 seconds latency versus xAI's roughly 672-847 visible output tokens and
7-11 seconds latency. The OpenAI healthcare output was schema-valid and mostly
avoided materialized runtime registries, but included detailed concrete network
zones. The university output additionally materialized the active jump host in
`hosts_by_name`.

The same OpenAI configuration failed the electric-utility retheme. It generated
about 3561 visible output tokens plus 1537 reasoning tokens, but returned an
unaccepted response with a JSON parse error and over-materialized runtime
registries including `hosts_by_name`, `services_by_name`,
`artifacts_by_path`, `groups_by_name`, and `users_by_username`. This suggests
OpenAI can be made usable for some Spiritguide rethemes with a larger reasoning
budget, but still requires validation/retry or stronger output-size and
registry guardrails before it should be treated as equivalent to xAI for this
role.

Current Spiritguide provider guidance for this experimental branch is:

- first choice: xAI `grok-4.3`;
- next-best tested fallback: OpenAI `gpt-5.5` with medium reasoning, low
  verbosity, enough completion budget, and careful monitoring for parse failures
  or over-materialized registries;
- not recommended: Anthropic Claude for broad Spiritguide rethemes.

A manual seaport-logistics test on July 4, 2026 exposed a separate state
initialization problem. The seaport profile was generated by retheming a live
session whose base `state_profile` was `bank:system`, then saved as a new system
profile. The saved profile's high-level purpose and expansion directives were
seaport-oriented, but materialized bank registries such as SSH config entries,
bank admin paths, Meridian Bank hostnames, and `siem01` remained in
`runtime_state`. A later user session loaded `seaport-logistics:system`; Grok
command emulation then materialized visible bank facts such as
`siem01.sec.meridianbank.local` and `secops@meridianbank.local` during normal
filesystem and SSH exploration.

This does not contradict xAI's Spiritguide fit. It shows that broad new-world
creation needs a blank-slate rebuild workflow or stronger state replacement
semantics before generated profiles are saved as durable defaults. Otherwise,
even a directive-first Spiritguide patch can leave stale concrete registries
available for the emulator to reuse.

## Maintenance

Update this decision if the directive keys change, liberty levels change, world
state becomes formally typed, runtime validators are added, or materialization
and pruning move out of prompt policy into code.
