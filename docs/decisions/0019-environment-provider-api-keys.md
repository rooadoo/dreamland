# Environment Provider API Keys

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: record why LLM provider API keys are configured only through standard
provider environment variables.

## Overview

DREAMLAND supports provider-qualified model references for OpenAI, xAI, and
Anthropic. Provider credentials are operational secrets and should not be stored
in `dreamland.ini` files.

## Details

The runtime now reads LLM provider API keys only from these environment
variables:

- `OPENAI_API_KEY`;
- `XAI_API_KEY`;
- `ANTHROPIC_API_KEY`.

The `[openai]`, `[xai]`, and `[anthropic]` `api_key` ini settings are ignored.
Model selection, model controls, Langfuse settings, admin API settings, and
other non-provider-secret configuration remain ini-configurable unless a
specific environment override exists.

This keeps DREAMLAND aligned with provider SDK conventions and reduces the
chance that local project configuration, sample configs, or support artifacts
capture live LLM credentials.

## Maintenance

Update this decision if DREAMLAND adds another LLM provider, changes provider
credential discovery, or deliberately reintroduces file-backed secret loading.
