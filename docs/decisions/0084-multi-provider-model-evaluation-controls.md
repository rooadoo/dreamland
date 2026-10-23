# 0084 Multi-Provider Model Evaluation Controls

Status: Active
Audience: maintainers, DREAMLAND operators, researchers, and coding agents
Purpose: record the configuration and adapter preparation for controlled model
comparisons while preserving independent provider choices.

## Overview

Model selection needs independent controls for reasoning effort, endpoint,
service tier, and reasoning mode, plus consistent command-prompt caching.
These controls belong in normal configuration and provider adapters.

## Details

### Independent Role Settings And Provider Boundaries

Add optional `openai_api`, `openai_service_tier`, and `openai_reasoning_mode`
fields to the command agent, Spiritguide, and all four enrichment role
settings. Preserve the existing environment-prefix convention. World Forge
continues to use Spiritguide configuration; live Stage composition forwards
the command settings. The separate credential-isolated attendee CLI keeps its
existing finite provider-settings schema.

OpenAI-only names make their scope clear. Anthropic and xAI adapters log and
omit these fields, matching the existing unsupported-control policy. Model,
credential selection, Anthropic speed/cache behavior, and xAI's existing
effort behavior remain provider-owned. `max` is newly accepted by common role
configuration, maps directly to Anthropic max (as existing xhigh already did),
and is omitted with an informational log on xAI. It is not silently translated
to another effort label.

OpenAI accepts explicit Responses or Chat Completions selection. Blank keeps
existing adapter selection except that Astra selects Responses. Summaries and
an explicit reasoning mode require Responses; conflicting explicit Chat
Completions requests fail clearly. Known older GPT-5 families reject `max` and
the new mode field; Astra rejects `none`. These checks run without constructing
a provider during config loading and again for direct provider construction.
Unknown model names remain configurable rather than requiring a global model
allowlist.

The owning implementation is [config.py](../../src/dreamland/config.py),
[model_providers.py](../../src/dreamland/model_providers.py), and the shared
[command agent](../../src/dreamland/agent_core.py). Enrichment keeps its full
[role descriptors](../../src/dreamland/background_agents/enrichment/role_outputs.py)
when constructing the graph.

Blank mode and tier omit their API fields. `standard`/`pro` mode is independent
of `low`/`medium`/other effort levels. `default` explicitly requests Standard
serving; `auto` inherits the project tier; `flex`, `fast`, and `priority` are
also accepted. Access-controlled Ultrafast is outside this initial control
surface. No model defaults, prompts, caps, or local operator settings change.

Sources: [Astra guidance](https://developers.openai.com/api/docs/guides/latest-model),
[GPT-5.6 guidance](https://developers.openai.com/api/docs/guides/latest-model/gpt-5.6),
[reasoning](https://developers.openai.com/api/docs/guides/reasoning), and
[Fast mode](https://developers.openai.com/api/docs/guides/fast-mode).

### Cache And Observability

Extend [decision 0032](0032-openai-gpt-5-6-explicit-command-prompt-caching.md)
only to the documented Astra family. Keep the exact stable system-prefix
boundary, hashed cache key, default TTL, and dynamic-state exclusion. Other
providers, older OpenAI models, Spiritguide, and enrichment do not receive
these command-cache fields. Official
[prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
documents compatible GPT-5.6-and-later fields.

Log non-secret OpenAI construction settings under the configurable lifecycle
category. Enrichment preserves its complete configured role descriptors during
construction so requested options remain available in role logs and traces.
Token logging records the returned serving tier when present and marks missing
metadata as unknown. This keeps requested settings distinct from provider
delivery, including Fast requests served at Standard tier. Existing token and
cache read/write accounting continues unchanged.

### Validation And Next Checkpoint

Tests cover INI/environment overrides for all six roles, invalid OpenAI
combinations before construction, multi-provider field isolation, command and
Spiritguide forwarding, enrichment descriptor preservation, and Stage
forwarding. Provider-free tests use the installed LangChain serializer to
check actual outbound Responses/Chat payloads, including Astra cache markers,
max effort, pro mode, caps and tiers. Existing backend/state and enrichment
tests remain the behavioral regression boundary.

Implementation checkpoint: 371 focused backend/config/provider/enrichment
tests passed; the Stage/telemetry group ran 19 tests with 18 passing and the
opt-in live-provider test skipped. Focused Ruff checks passed. Mypy at the
repository's Python 3.11 target stops on an installed NumPy stub requiring
Python 3.12 syntax. With the local Python 3.12 target, the eight inspected
source files report the same 15 diagnostics on this change and on committed
`main`, verified using baseline shadow files. Static checking is therefore not
an all-green gate, and this change adds no diagnostic in that comparison.

No inference or live MCP smoke is claimed by serializer tests. The next
operator checkpoint is to start DREAMLAND, read MCP discovery resources,
create one tagged session, run ordinary commands including a model-backed
command, inspect the returned ids/output and trace, and close that exact
session. Then prepare the separate A/B case and run manifest. Persisted
reasoning, strict API schemas, hosted delegation, async tools, and mid-turn
steering remain separate runtime experiments.

## Maintenance

Update this decision and [Operations](../OPERATIONS.md) when controls,
provider mappings, known compatibility checks, cache eligibility, or serving
telemetry change. Study results and evidence are published separately.
