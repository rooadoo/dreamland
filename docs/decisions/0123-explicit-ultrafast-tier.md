# 0123 Explicit Ultrafast Tier

Status: Active
Audience: DREAMLAND maintainers, operators, and evaluation reviewers
Purpose: permit explicit Ultrafast requests for bounded serving-tier comparisons.

## Overview

The owner requested a one-command comparison of GPT-6.1 Sol Fast and Ultrafast,
with a cost review before any larger evaluation. The provider adapter already
passes service tiers to OpenAI, and returned-tier logging recognizes Ultrafast,
but shared configuration validation rejected the new request value.

## Details

Add `ultrafast` to the shared accepted service tiers. This enables INI/environment
configuration and direct or nested MCP evaluation overrides through the existing
adapter. Keep Fast as the runtime default and Standard as the default for MCP
command sessions without an explicit tier. Do not infer model availability from
configuration acceptance; OpenAI currently documents GPT-6 Astra and GPT-6.1 Sol
support in its [Ultrafast guide](https://developers.openai.com/api/docs/guides/ultrafast-mode).

Extend the existing configuration, MCP override, and real SDK request
serialization checks. No timing harness, endpoint migration, or tracing changes
are required. Comparisons use Langfuse timing and token usage, verify returned
service tiers, and calculate USD estimates directly from OpenAI's
[published token rates](https://developers.openai.com/api/docs/pricing), rather
than Langfuse's internal pricing. Cache and reasoning components must be counted
once using the observed usage representation.

The owner explicitly authorized this small change and its descriptive commit on
the current branch, preserving unrelated uncommitted work. The authorized live
scope is one `hostnamectl` submission per tier, then a stop for cost review.

## Maintenance

Keep accepted tiers, operator documentation, and owning checks aligned with the
provider API. Recheck rates and supported models before future paid evaluations;
this decision does not authorize additional inference beyond the stated pilot.
