# 0032 OpenAI GPT-5.6 Explicit Command Prompt Caching

Status: Active
Audience: maintainers, DREAMLAND operators, prompt authors, and coding agents
Purpose: record how DREAMLAND requests OpenAI prompt caching without adding
dynamic world state to the reusable command-emulator prefix.

## Overview

Active-host context projection reduces the dynamic world information sent to
the command emulator, but the emulator still sends a large, mostly stable
system prompt on every model call. OpenAI GPT-5.6 supports explicit cache
breakpoints and stable cache-routing keys. DREAMLAND uses that provider feature
at the existing boundary between stable system instructions and dynamic command
context.

The provider contract is defined by OpenAI's
[prompt-caching guide](https://developers.openai.com/api/docs/guides/prompt-caching).

Caching is an opportunistic transport optimization. It does not alter
authoritative DREAMLAND state, the model-visible facts, or the correctness of a
cache miss.

## Decision

For command-emulator calls whose provider is `openai` and whose model name is
exactly `gpt-5.6` or begins with `gpt-5.6-`, DREAMLAND sends all three parts of
OpenAI's explicit cache request together:

- the rendered system prompt is one LangChain text content block with
  `prompt_cache_breakpoint = {"mode": "explicit"}`;
- the request has `prompt_cache_options = {"mode": "explicit"}`;
- the request has a deterministic `prompt_cache_key`.

[Decision 0084](0084-multi-provider-model-evaluation-controls.md) extends this
same boundary to `gpt-6-astra` and `gpt-6-astra-*`, following current official
documentation and provider-free outbound serialization checks. It also adds
explicit API selection independently of reasoning summaries. The original
GPT-5.6 boundary, key, and dynamic-state exclusion remain as described here.

[Decision 0117](0117-gpt-6-role-model-refresh.md) extends the same gate to
GPT-6 Sol, GPT-6.1 Sol, GPT-6 Luna, and their hyphenated snapshots. The official
GPT-5.6-and-later cache contract and real SDK serialization checks support
these additions; the cache placement and key format remain unchanged.

The installed `langchain-openai>=1.3.5,<2` integration serializes this neutral
text block as `input_text` for the Responses API and `text` for Chat
Completions. It preserves the breakpoint and both first-class request fields,
so DREAMLAND does not force one OpenAI API or send an `extra_body` workaround.

DREAMLAND omits `ttl`, allowing OpenAI's current default minimum lifetime of 30
minutes to apply; OpenAI may retain the prefix longer. It also omits legacy
`prompt_cache_retention`. OpenAI models before GPT-5.6 reject
`prompt_cache_options` and `prompt_cache_breakpoint`, so the model gate is
intentionally strict. Later model families must be enabled deliberately after
their request contract is verified.

OpenAI requires a marked prefix of at least 1,024 tokens before it is
cacheable. DREAMLAND does not add a separate length gate because the packaged
command-emulator system prompt already exceeds that threshold; cache usage
telemetry remains authoritative if prompt composition changes later.

## Cache-Key And Prompt Boundary

The key format is `dreamland-command-v1:` followed by the first 32 hexadecimal
characters of a SHA-256 digest. Digest input is a deterministic compact JSON
array containing the namespace, provider, configured model name, and exact
rendered system prompt. The key therefore:

- remains stable when the session id, active host, projected world state, shell
  state, command, or command history changes;
- changes when the provider, model, base system prompt, or active-program
  instruction suffix changes;
- contains no plaintext prompt or plaintext secret, only the digest.

The complete rendered system prompt remains the first message. The projected
world state and current command remain in the following user message, in their
existing order. The explicit breakpoint is only on the system content block.
No prompt facts are reordered to obtain a cache hit.

Spiritguide is excluded. It continues to receive its complete world context and
uses the shared plain system-message path for OpenAI models. The existing
`prompt_cache` configuration setting remains an Anthropic-only control; no new
configuration field or world-state schema field is introduced.

## Cost And Observability

At the time of this decision, a GPT-5.6 cache write costs 1.25 times an ordinary
uncached input token, while a later matching cache read is discounted.
DREAMLAND therefore records both outcomes instead of treating every eligible
request as a saving.
LangChain-normalized and raw OpenAI Responses or Chat Completions usage fields
are mapped to:

- `input_cache_creation` for cache-write tokens;
- `input_cache_read` for cache-read tokens.

Priority- and flex-tier normalized field names are also accepted. Zero values
are preserved rather than replaced by a secondary usage source.

Cache placement and availability remain controlled by OpenAI. A missing cache
read is not an emulator failure. If traffic for one key approaches the
provider's current guidance of roughly 15 requests per minute, operators should
measure repeated writes and reduced hit rate before DREAMLAND adds deterministic
key sharding. The initial global model-and-prompt key maximizes reuse at the
project's current scale.

## Consequences

Repeated commands that use the same model and active-program system prompt can
reuse the large stable prefix while still receiving fresh active-host and shell
state. Host pivots do not invalidate the stable key unless they also change the
active program. Prompt edits and model changes invalidate it automatically.

The first request after a key change may be more expensive because it writes a
cache entry. Short or infrequently repeated sessions may not recover that write
cost. This tradeoff is observable through token logs and can be reevaluated from
live traces without changing state storage.

## Maintenance

Update this decision when OpenAI changes explicit-cache fields, TTL support,
eligible model families, write/read pricing, or key-routing guidance. Also
update it when DREAMLAND changes system-prompt composition, adds cache-key
sharding, exposes a cache configuration control, applies explicit caching to
Spiritguide, or upgrades `langchain-openai` across a serializer-contract change.
