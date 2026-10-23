# 0127 Spiritguide Ultrafast Default

Status: Active
Audience: maintainers and DREAMLAND operators
Purpose: record the operator-requested Ultrafast default for Spiritguide.

## Overview

The owner requested Ultrafast as Spiritguide's default serving tier. This
supersedes the Spiritguide portion of decision 0118's all-role Fast default.

## Details

Set `SPIRITGUIDE_MODEL_DEFAULTS.openai_service_tier` to `ultrafast` and align the
example INI. The shared preferences supply direct settings, omitted INI values,
new deployment configurations, and explicit deployment model-default adoption.
Spiritguide retains Astra, high reasoning, detailed summaries, and its
16,384-token output cap. Emulator and enrichment defaults remain `fast`.

Explicit INI and environment overrides retain precedence; an explicit blank
clears the requested tier. Other providers do not inherit the OpenAI tier.
The local ignored INI also selects `ultrafast` for Spiritguide. Existing running
processes need a restart to adopt it. Other deployments require an explicit
configuration update where their INI already specifies a tier.

Configuration and deployment tests verify defaults and override handling without
provider calls. This change makes no claim about returned live-provider tiers.

## Maintenance

Update this record with changes to Spiritguide's serving-tier default or
configuration adoption policy. Keep the operations guide and model-fit table
aligned with `src/dreamland/model_defaults.py`.
