# 0118 All Runtime Roles Fast Tier

Status: Active
Audience: DREAMLAND maintainers and deployment operators
Purpose: make Fast the explicit default for every OpenAI runtime agent role and
record deliberate adoption in local and demo configuration.

## Overview

The owner requested Fast for all agents and confirmation of both package
defaults and demo configuration. Inspection found that only the emulator
explicitly requested `fast`. Spiritguide and the four enrichment roles had blank
serving tiers, inheriting the provider/project default. The previous demo probes
had also returned Standard serving for the Coordinator and Expert.

This instruction supersedes the unset Spiritguide/enrichment tier preferences
preserved by [decision 0117](0117-gpt-6-role-model-refresh.md). Model selections,
reasoning, summaries, verbosity, caps, endpoints, and simulation behavior retain
their existing values.

## Details

### Shared defaults and explicit settings

All six entries in [`ROLE_MODEL_DEFAULTS`](../../src/dreamland/model_defaults.py)
now set `openai_service_tier="fast"`: emulator, Spiritguide, Focused Coordinator,
Ambient Coordinator, Expert, and Integrator. Settings dataclasses, omitted INI
controls, generated deployment INIs, and the sanitized example share those
preferences. Current operations/model-fit/enrichment examples reflect them.

Existing explicit blanks or `default`/`flex` values remain overrides; a new image
does not replace them silently. The local working INI is updated explicitly,
with an exact private backup and preservation checks for other settings. The
demo likewise requires review, backup, deliberate tier adoption, and restart.
No OpenAI project-wide settings, credentials, or host firewall rules are changed.

For non-OpenAI providers, an omitted OpenAI tier remains unset. This change
does not enable Anthropic's separate `speed=fast` control or change xAI behavior.

### Request and delivery verification

The [official Fast-mode contract](https://developers.openai.com/api/docs/guides/fast-mode)
supports `service_tier="fast"` for Responses and Chat Completions. The provider
adapter already forwards the configured tier for either endpoint; no endpoint
or SDK changes are needed. Returned `fast` or its equivalent `priority` tier is
delivery evidence. A provider may return `default` under ramp-rate controls, so
requested settings alone do not guarantee every future request is served Fast.

Configuration/default/provider/deployment checks cover all six role preferences,
generated/example parity, independent overrides, and request serialization.
Demo verification must check all six effective tiers after restart and use
bounded direct probes to confirm returned tiers without creating world sessions.
This is configuration verification, not a speed benchmark. The older paused
comparison campaign on `docs/agent-fast-tier-test-plan` remains separate.

## Maintenance

Keep shared defaults, example INI, current role documentation, and owning
configuration checks aligned. Preserve explicit operator overrides. Record each
server's actual adoption and provider-returned tiers alongside its image and
backup identity rather than inferring them from source defaults.
