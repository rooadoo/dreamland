# 0117 GPT-6 Role Model Refresh

Status: Active
Audience: DREAMLAND maintainers and deployment operators
Purpose: record the October 2026 model-role refresh, initial local staging, and
subsequently authorized demo deployment.

## Overview

On October 6, 2026, the owner requested a review of project defaults and the
test/deployed configurations, an update to newer comparable models, and local
staging without deployment. This refresh supersedes the Sol, Terra, and Luna
selections in [decision 0107](0107-shared-model-defaults-and-deployment-adoption.md).
Its shared-default, explicit-override, and deployment-backup mechanisms remain
authoritative.

The initial configuration-maintenance pass stayed local. The owner then
authorized committing, pushing, and redeploying the demo server. Implementation
commit `9c92afded8b7bc4eca67310aa684d556ff82787c` was merged and pushed to `main`,
then deployed with the five reviewed model selections.

## Details

This record describes the model refresh and its first deployment. The later
all-role Fast instruction in [decision 0118](0118-all-runtime-roles-fast-tier.md)
supersedes the previously preserved unset Spiritguide/enrichment serving tiers.

### Role mapping

| Role | Previous package model | New model |
| --- | --- | --- |
| Command emulator | `openai:gpt-5.6-sol` | `openai:gpt-6.1-sol` |
| Spiritguide | `openai:gpt-6-astra` | `openai:gpt-6-astra` |
| Focused and Ambient Coordinators | `openai:gpt-5.6-luna` | `openai:gpt-6-luna` |
| Expert | `openai:gpt-5.6-terra` | `openai:gpt-6.1-sol` |
| Integrator | `openai:gpt-5.6-luna` | `openai:gpt-6-luna` |

The [official model guidance](https://developers.openai.com/api/docs/guides/latest-model)
lists GPT-6.1 Sol for complex work and GPT-6 Luna for efficient focused work.
It does not list a GPT-6 Terra. Choosing Sol for the Expert retains the higher
capability role above the Luna planning/integration tier. This is a role-based
choice, not a new DREAMLAND quality ranking. Astra already occupies the current
highest capability role for Spiritguide.

Reasoning effort, summaries, verbosity, output caps, serving tiers, prompts,
scheduling, logging, tracing, and credentials retain their existing values.
The emulator keeps low reasoning and Fast mode; Spiritguide keeps high
reasoning. Enrichment keeps low reasoning and low verbosity.

At Standard API rates for up to 272K input tokens, GPT-6.1 Sol costs $2 input
and $10 output per million tokens, compared with GPT-5.6 Sol's $4 and $20.
GPT-6 Luna costs $0.10 input and $0.50 output, compared with GPT-5.6 Luna's
$0.20 and $1.20. For the Expert, Sol retains Terra's $2 input rate and reduces
output from $12 to $10. Actual costs depend on generated tokens, caching,
context size, and serving tier. See [official API pricing](https://developers.openai.com/api/docs/pricing).

### Configuration review and local staging

The working local test INI matched the previous package selections and controls
except that its Expert model was spelled `openai:gpt-5.6-tera`. The refresh
replaces this value with `openai:gpt-6.1-sol` along with the emulator and three
Luna selections. Only the five model lines change; all other settings and INI
formatting are preserved. An exact owner-only backup and non-secret review
report are retained in the ignored local staging directory. Neither that INI
nor its private backup belongs in Git.

The deployed review was initially pending the administrative destination.
After the owner supplied and corrected it, read-only inspection confirmed that
the demo's configured and effective role controls matched the previous package
defaults, including the correct Terra spelling. Its explicit INI therefore
required the same five model updates; an image rebuild alone would retain the
old selections.

The existing `model-defaults --apply` command adopts all shared model controls.
The new image's preview was checked to contain exactly the five model fields
before application. Full backups preceded adoption, SSH was stopped during the
INI update, and semantic checks proved that non-model controls and other private
runtime settings were preserved. Other deployments still require their own
configuration review before adoption.

### Compatibility and verification

The command emulator already uses Responses through its detailed reasoning
summary. Other roles issue text requests without provider tool calling;
their existing endpoint selection remains supported. No endpoint, tool
handler, prompt, or output contract changes are required.

GPT-6.1 Sol does not support `none` or `minimal` reasoning effort. Reject these
combinations before provider construction, including model snapshots; retain
`low` in the packaged Sol roles. The existing general INI effort validator
already rejects `minimal`. Luna continues to support `none` for explicit
operator configurations. See [official migration guidance](https://developers.openai.com/api/docs/guides/latest-model#migration-quickstart).

Extend the strict explicit-cache family gate to GPT-6 Sol, GPT-6.1 Sol, and
GPT-6 Luna, including hyphenated snapshots. Preserve the single system-prefix
breakpoint, model/prompt-derived key, dynamic user-state exclusion, and omitted
TTL. Unknown model families and other providers stay excluded. The
[official prompt-caching contract](https://developers.openai.com/api/docs/guides/prompt-caching#how-caching-works-gpt-5-6-and-later)
supports these fields for GPT-5.6 and later. Real SDK serialization tests cover
the new model IDs without making provider calls.

Configuration/provider/default/deployment suites passed 104 tests. Backend,
main, console, Spiritguide, and enrichment-service regressions passed 304 tests.
These checks validate local compatibility and configuration preservation.
Subsequent demo startup and three bounded provider probes established access
through Sol Responses/Fast, Luna Chat Completions, and Sol Expert Chat
Completions. They do not establish comparative immersive-world quality.
Frozen evaluation inputs, historical model-fit results, prior deployment
evidence, and the separately configured Inspect exploration-agent baseline
retain their original model identities.

The model-update branch starts at `main` and deliberately preserves the
unmerged active-host prompt-duplication fix from `6ea54a7`. Its original branch
remains available. The later owner-authorized merge/push includes this preserved
fix and the model refresh.

## Maintenance

Update the shared role defaults, sanitized example INI, current operations and
enrichment documentation, configuration parity checks, and provider compatibility
tests together. Review each deployed configuration before adoption and record
actual server/image/provider evidence separately from local tests.
