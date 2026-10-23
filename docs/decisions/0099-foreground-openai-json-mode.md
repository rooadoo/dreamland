# Foreground OpenAI JSON Mode

Status: Active
Audience: DREAMLAND maintainers and operators
Purpose: prevent malformed foreground JSON through native API syntax constraints.

## Overview

A control run on main revision `00c0f12` reproduced seven extra-closing-brace
failures in thirty foreground model calls. The parser rejected their proposed
state updates even though the MCP explorer saw ordinary terminal output.
Prompt instructions alone did not reliably preserve the result envelope.

## Details

The foreground command agent passes `response_format={"type":"json_object"}`
on every OpenAI invocation. Its existing invocation helper combines this with
prompt-cache options, and the SDK translates the field for the selected API.
Streaming, dynamic world-state dictionaries, prompts, and parser validation
remain in place. The shared provider factory does not impose a format on
Spiritguide or enrichment. No new operator setting is needed.

This change uses JSON mode without strict schema enforcement. A strict schema
would require adapting arbitrary host, path, and metadata keys; that additional
work is outside this syntax fix. JSON mode does not prevent incomplete output
or guarantee semantic correctness, and this change adds no repair or retry.
See the [OpenAI JSON-mode documentation](https://developers.openai.com/api/docs/guides/structured-outputs#json-mode).

Tests cover both API request shapes with the installed SDK, preserved
streaming and caching, uncached OpenAI calls, and exclusion of other providers.
Comparative study results are published separately.

## Maintenance

Update this record if output enforcement expands to other roles, gains a schema
or configuration option, or changes parser and incomplete-output handling.
