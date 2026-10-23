# 0049 Default Safe Enrichment

Status: Active
Audience: maintainers, DREAMLAND operators, prompt authors, and coding agents
Purpose: record why EnrichmentAgent is a required default runtime role and why
automatic work uses safe application by default.

## Overview

The EnrichmentAgent supplies the just-in-time supporting detail that lets a
DREAMLAND environment deepen beyond the facts needed for the foreground
command. Earlier checkpoints deliberately shipped automatic scheduling disabled
and `shadow` application as conservative research defaults while the role
boundary, triggers, validation, race handling, and observability matured.

Those controls now form the bounded mutation boundary described by decisions
0033 through 0045. Leaving the role inactive by default no longer represents
the intended DREAMLAND product: background enrichment is necessary to the
normal experience rather than an optional conference-only experiment.

## Decision

Automatic EnrichmentAgent scheduling is enabled by default. Its default apply
mode is `safe`, which permits only validated, bounded, additive host-local
patches through the deterministic normalization, provenance, dependency,
authority, touched-path, and final apply checks already owned by the runtime.

The defaults apply to `EnrichmentSettings`, missing configuration values, and
the sanitized example configuration. Operators retain explicit configuration
and environment overrides. They may disable automatic scheduling for a
controlled no-background-call run or select `shadow` mode for proposal tuning
and comparison, but neither override is the normal product posture.

Default enablement does not grant broader authority. Enrichment remains
session-local, cannot mutate a source profile or shared global world, and may
not overwrite established state. Provider calls remain lazy until a job is
admitted. Operators must account for the latency and provider cost of automatic
jobs, and public deployments must retain their own spend, concurrency,
retention, monitoring, and failure controls.

For the BSides Augusta presentation and release, EnrichmentAgent is included
and enabled. Presenter Stage provenance must distinguish its background
contributions from seed facts and foreground-command materialization, and the
conference candidate still requires measured latency, cost, failure, and load
evidence. Default enablement is not evidence that those separate gates passed.

## Alternatives Considered

Keeping automatic work disabled was rejected because it makes an essential
world-building role opt-in and causes ordinary DREAMLAND sessions to omit an
intended source of coherent depth. Keeping `shadow` as the default was rejected
because it pays the full proposal cost while withholding validated results from
the session.

Unbounded or direct model mutation was rejected. The EnrichmentAgent continues
to propose work while deterministic runtime code owns validation and mutation
authority. Removing explicit disable and shadow overrides was also rejected;
they remain useful for cost isolation, incident response, research comparison,
and prompt tuning.

## Consequences

An ordinary session can schedule background provider work and apply safe
additive detail without extra configuration. This increases the importance of
honest provider-cost documentation and operational budgets. Tests and examples
must treat enabled plus safe as the default and must exercise explicit opt-out
and shadow overrides separately.

## Maintenance

Implementation is recorded by the feature that changes `EnrichmentSettings`,
configuration loading, and `dreamland.example.ini` together.

Update this record, `docs/PROJECT_MODEL.md`, `docs/ENRICHMENT_AGENT.md`,
`docs/OPERATIONS.md`, `docs/ARCHITECTURE.md`, `README.md`, the example
configuration, and owning tests together if automatic enablement, application
authority, cost posture, or conference inclusion changes.
