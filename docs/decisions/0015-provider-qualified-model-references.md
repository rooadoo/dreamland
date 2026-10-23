# 0015 Provider-Qualified Model References

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: record why model provider selection is encoded in model references and
why optional provider-specific controls fail open.

## Overview

DREAMLAND needs to test multiple frontier LLM providers without making the
runtime depend on one provider's configuration shape or feature vocabulary.
Provider selection is part of the model identity used by a session, so it is
encoded directly in model references such as `openai:gpt-5.5` and
`xai:grok-4.3`.

This is a pre-release breaking configuration change. DREAMLAND intentionally no
longer accepts unqualified model names because a bare model name is ambiguous
once multiple providers can run in the same deployment.

## Details

The runtime parses model references into provider and model components before
constructing provider-specific LangChain chat models. API keys remain in
provider-specific configuration sections, while command-agent and Spirit Guide
settings reference models with `provider:model` strings.

Optional controls are provider agnostic at the configuration boundary when
reasonable. If a provider supports a control such as reasoning effort, the
provider adapter maps it to that provider's native option. If a provider does
not support a configured optional control, DREAMLAND logs the ignored setting
at informational level and continues startup instead of failing the runtime.
Provider-specific performance controls follow the same rule. Anthropic fast
mode is exposed as `speed = fast` and maps to Claude's `speed` request parameter
and required beta header. Other providers ignore the setting with an
informational log.

Reasoning capture is normalized at the DREAMLAND observation layer. Provider
native reasoning payloads, such as xAI `reasoning_content` and Claude
`thinking` blocks, are extracted into `dreamland.reasoning_summary` metadata on
command-agent and graph observations. Raw provider generation observations may
still retain their native payload shape.

For Claude models that support adaptive thinking, a configured Anthropic
`reasoning_effort` also requests summarized thinking display so DREAMLAND can
capture reasoning when the provider returns it.

Provider observations from manual testing:

- xAI exposes reasoning through `reasoning_content`, which DREAMLAND can
  normalize without changing command-response parsing.
- xAI prompt caching is automatic for repeated starting messages. xAI also
  exposes cache-key and priority-processing knobs, but DREAMLAND does not send
  those provider-specific controls yet because the current command-runtime path
  does not maintain stable cache keys across providers.
- Claude exposes displayed reasoning as separate `thinking` content blocks
  alongside normal `text` blocks. DREAMLAND should capture the `thinking`
  content as reasoning metadata and parse only the `text` content as the
  command-response envelope.
- Claude supports prompt caching through `cache_control` content-block hints.
  DREAMLAND marks the stable system-prompt block for the default 5-minute
  ephemeral cache instead of marking the whole request, because command state
  and history change on every call. The short TTL has the lowest cache-write
  cost among Claude cache durations and matches DREAMLAND's frequently changing
  runtime context. Operators can disable this DREAMLAND-managed Anthropic hint
  with `prompt_cache = off`.
- Claude may wrap the complete structured command-response envelope in a
  Markdown code fence, including pretty-printed `json` fences. This violates the
  prompt contract but is a provider formatting quirk observed in manual
  sessions. DREAMLAND accepts a complete response-level `json` or unlabeled
  code fence when the fenced body is a JSON object envelope, while still
  treating fenced or JSON-looking text inside terminal streams as ordinary
  terminal output.
- Claude can alternate between fenced and raw JSON envelopes across repeated
  commands in the same session. Parser tolerance therefore belongs at the
  structured-envelope boundary, not only in prompts or provider-specific retry
  handling.
- Claude may also prepend Markdown terminal output and explanatory text before
  a valid trailing structured envelope. DREAMLAND accepts that trailing object
  only when it is a complete command-response envelope with the required keys;
  other trailing JSON remains terminal output.
- OpenAI prompt caching is automatic for eligible prompts. For GPT-5.6-family
  command-emulator calls, DREAMLAND additionally sends a deterministic cache
  key and an explicit breakpoint at the end of the stable system prompt. Older
  OpenAI models and Spiritguide keep provider defaults. This emulator-only
  behavior is recorded in
  `0032-openai-gpt-5-6-explicit-command-prompt-caching.md`.

## Maintenance

Update this decision when adding a new provider, changing the model reference
grammar, changing optional-control fail-open policy, or moving normalized
reasoning metadata to a different telemetry layer. Also update it when manual
provider testing reveals a recurring provider-specific response shape that
DREAMLAND intentionally normalizes.
