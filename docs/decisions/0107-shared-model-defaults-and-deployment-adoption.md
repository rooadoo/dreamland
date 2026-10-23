# 0107 Shared Model Defaults And Deployment Adoption

Status: Active
Audience: DREAMLAND maintainers and deployment operators
Purpose: align new local and deployed model preferences with the owner's working
configuration while preserving explicit settings and requiring deliberate
adoption in an existing deployment.

## Overview

The owner requested that the project's agent selections and model controls
match the working `dreamland.ini`, including deployment. The owner also wants
to test those preferences alongside the detachable SSH operator console before
continuing the remaining backend API phases. This changes model defaults only;
it does not import the lab's credentials, paths, tracing, or other runtime
configuration into an installation.

After the shared defaults and console were committed in `721ff67`, the owner
requested `openai:gpt-6-astra` with high reasoning for Spiritguide instead of
Sol. The other Spiritguide controls remain unchanged: detailed summary,
16,384-token output cap, and no explicit serving tier. This later instruction
supersedes the initial working-INI model selection for that role. The same choice
was applied narrowly to the local INI's Spiritguide model and reasoning-effort
fields, preserving other bytes and credentials; that ignored file is not shipped.
The change does not establish live-provider validation.

The initial server image from commit `3a58e6f` used explicit GPT-5.5 command and
Spiritguide selections and inherited other controls from the package. Its
recorded validation remains evidence for that revision. The shared defaults
below supersede the model choices described in
[decision 0104](0104-deployment-foundation-and-phase-checkpoints.md), without
changing its storage or phase-review requirements.

## Details

The initial role preferences below record the September 2026 adoption.
[Decision 0117](0117-gpt-6-role-model-refresh.md) supersedes their Sol, Terra,
and Luna selections for the October 2026 refresh; the shared-default and
deployment-adoption mechanisms described here still apply. See
[Model Fit](../MODEL_FIT.md#packaged-model-preferences) for current defaults.

### One source for six model roles

[`model_defaults.py`](../../src/dreamland/model_defaults.py) holds immutable role
preferences shared by settings dataclasses, missing-configuration fallbacks, and
deployment INI generation. The sanitized example INI exposes the same choices;
tests compare it with the shared defaults and generated deployment settings.

| Role | Model | Reasoning effort | Summary | Output cap | OpenAI tier |
| --- | --- | --- | --- | --- | --- |
| Command emulator | `openai:gpt-5.6-sol` | low | detailed | 12,800 | fast |
| Spiritguide | `openai:gpt-6-astra` | high | detailed | 16,384 | unset |
| Focused and Ambient Coordinators | `openai:gpt-5.6-luna` | low | unset | 2,048 | unset |
| Expert | `openai:gpt-5.6-terra` | low | unset | 2,048 | unset |
| Integrator | `openai:gpt-5.6-luna` | low | unset | 4,096 | unset |

All enrichment roles use low verbosity. Command and Spiritguide verbosity remain
unset. Other optional model controls remain unset. The Expert spelling `terra`
was confirmed with the owner. The command tier is the literal configured value
`fast`; configured preferences alone do not demonstrate provider availability or
the tier actually used by a request.

Only the allowlisted model selection and control fields are shared. Prompt-file
selection, scheduling, logging, tracing, networking, storage, and credentials keep
their own configuration. The working local INI is not an input at deployment
time and is not copied into the image or setup output.

Explicit INI and environment choices remain authoritative. A blank optional
model control clears the value to the provider default; an explicitly empty or
whitespace-only environment value also clears an INI override or packaged
default. An absent environment variable inherits the INI value or role default.
When configuration selects a non-OpenAI model, an omitted OpenAI serving-tier
control remains unset. An explicitly selected xAI or Anthropic Spiritguide
retains provider-default reasoning effort unless the operator sets it. The
deployment foundation continues to support OpenAI credentials only.

### Existing deployment adoption

Ordinary `init` preserves an existing `dreamland.ini` byte for byte. An image
upgrade can still change effective values for settings omitted from that file,
because those values inherit package defaults. Preserving the file does not pin
an incomplete configuration to an older package's defaults.

Provide an explicit deployment command to inspect and adopt the complete model
preferences:

```sh
python -m dreamland.deployment model-defaults --root /var/lib/dreamland
python -m dreamland.deployment model-defaults --root /var/lib/dreamland --apply
```

The first command is a read-only preview: it lists changed setting names and
the proposed non-secret role values. Stop SSH before applying and restart it
afterwards. The command does not stop or restart the service itself.

`--apply` writes only the allowlisted model controls semantically. Other INI
sections and settings, including custom prompt selections, retain their values.
The INI is reserialized, so comments and formatting are not preserved in the
updated file. Before replacement, retain the original exact bytes in an
owner-only `0600` backup named
`dreamland.ini.before-model-defaults-HASH`, where `HASH` is the first 16 hex
characters of the original file's SHA-256. This backup may contain private
operator configuration and remains inside the persistent deployment directory.

Use the deployment storage lock, verify that the current INI still matches the
read bytes, and atomically replace it. Refuse unsafe or conflicting backup
paths. Recover a reserved temporary hardlink left by an interrupted backup
publication before retrying. Concurrent malformed configuration errors remain
sanitized. Other deployment files, credentials, host identity, profiles, and
catalog data are untouched. Reapplying already matching settings is a no-op.

This is a targeted model-preference adoption operation, not a general deployment
schema migration or reset. Preserve the unrelated private runtime configuration
and inspect the effective model choices before live-provider testing. Existing
provider-authentication observations remain unresolved by this change.

### Review and evidence

Local config tests cover all six roles, example/generated/default parity,
provider-specific fallbacks, explicit overrides, and blank controls. Deployment
tests cover read-only preview, backup and state preservation, repeat application,
concurrent edits, interrupted publication, and unsafe backup paths.

See [model adoption](../OPERATIONS.md#adopt-model-defaults) for the reusable
preview, backup, apply, and restart procedure. Record actual image and provider
checks separately for each deployment.

## Maintenance

Update the shared defaults, sanitized example, configuration tests, generated
deployment checks, and [Operations](../OPERATIONS.md) together when a role's
model preferences change. Keep the adoption scope and backup behavior aligned
with [`deployment/configuration.py`](../../src/dreamland/deployment/configuration.py)
and [`deployment/storage.py`](../../src/dreamland/deployment/storage.py). Preserve
historical model names and image identities in evaluation and deployment evidence.
