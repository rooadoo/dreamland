# Packaged Model Preferences

Status: Active
Audience: DREAMLAND operators, maintainers, and coding agents
Purpose: document packaged runtime model preferences and configuration overrides.

## Overview

Runtime roles have independent model controls. The values below describe the
packaged configuration, rather than comparative model-quality measurements.
Research findings and study evidence are published separately.

## Details

### Packaged Model Preferences

New local and Docker configurations use the operator's working role preferences
with the October 2026 Sol/Luna refresh and the Astra selection for Spiritguide,
defined in
[`model_defaults.py`](../src/dreamland/model_defaults.py):

| Role | Model | Reasoning | Summary | Output cap | Serving tier |
| --- | --- | --- | --- | --- | --- |
| Command emulator | `openai:gpt-6.1-sol` | low | detailed | 12,800 | fast |
| Spiritguide | `openai:gpt-6-astra` | high | detailed | 16,384 | ultrafast |
| Focused and Ambient Coordinators | `openai:gpt-6-luna` | low | none requested | 2,048 | fast |
| Expert | `openai:gpt-6.1-sol` | low | none requested | 2,048 | fast |
| Integrator | `openai:gpt-6-luna` | low | none requested | 4,096 | fast |

All enrichment roles use low verbosity. Other optional controls remain unset.
Explicit INI and environment settings override these defaults. Deployment setup
preserves an existing INI; settings omitted from it inherit the installed
image's defaults. An explicit model-defaults update writes the complete current
role preferences into that INI.

See [Operations](OPERATIONS.md) for provider controls and
[decision 0127](decisions/0127-spiritguide-ultrafast-default.md) for the
Spiritguide serving-tier preference.

## Maintenance

Update this reference with `model_defaults.py` and the example configuration.
Keep experimental measurements in the separate research publication.
