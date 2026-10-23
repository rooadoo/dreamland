# 0101 MCP Session Command Options and Standard Serving

Status: Active
Audience: DREAMLAND maintainers, operators, and MCP testing agents
Purpose: explain direct command-model options and standard OpenAI serving for new MCP sessions.

## Overview

MCP model selection already existed through nested evaluation overrides, but
ordinary sessions shared the process command agent. A process configured with
`openai_service_tier = fast` therefore made ordinary MCP commands use Fast too.
The operator requested direct model and tier options on admin session creation
and accepted creation-time settings with independent per-role configuration.

## Details

Admin `dreamland_session_create` exposes `model` and `openai_service_tier` as
aliases for the same fields in `config_overrides.agent`. Direct arguments use
the existing provider-reference and tier validation. Identical duplicate values
are accepted; conflicting values produce a clear error before model
construction or registration. Direct null means omission. Nested null retains
its existing meaning of clearing an optional setting.

When neither form supplies a tier, the resolved OpenAI command agent receives
`openai_service_tier = default`. This applies to model-only overrides as well
as ordinary session creation. Omission no longer inherits the process tier.
Explicit tier selections, including Fast, remain available for experiments.
Explicit `auto` or nested null can inherit a Fast OpenAI project setting;
callers requesting standard serving should use `default`.

Only the command emulator receives the new default. Spiritguide, the four
enrichment roles, non-OpenAI provider controls, and detached profile generation
keep their existing configuration semantics. Process settings and SSH agents
are never mutated. Existing sessions retain their already-created runtime,
including SSH sessions addressed through admin MCP command tools.

All MCP sessions with an available model runtime factory now use the isolated
runtime machinery from [decision 0085](0085-isolated-mcp-evaluation-runtimes.md).
This is required to separate the command tier from the shared SSH backend.
Resolved snapshots, session-start records, traces, event routing, and cleanup
continue using the existing runtime ownership path. Each such session owns
worker and scheduler state; operators should close completed sessions promptly.
Embedded services without a runtime factory keep their supplied backend when
no options are requested and reject explicit controls as unavailable.

The operator chose to keep direct options on admin `/mcp` only.
`client_session_create` on `/client-mcp` retains its username-only contract;
its new model-backed sessions receive the same standard default from the common
service. The client response does not expose evaluation settings.

Owning tests cover direct and legacy nested options, omitted/default tiers,
explicit tiers and null clearing, conflict and invalid-input rejection before
construction, accurate snapshots, immutable process settings, event routing,
profile initialization, and cleanup. Real loopback MCP tests use deterministic
provider substitutes to verify both endpoint contracts. Actual serving-tier
verification remains the manual checkpoint in
[MCP Model Evaluation Controls](../MCP_MODEL_EVALUATION.md#validation-and-operator-checkpoint).

Validation on 2026-09-11: 145 tests passed across `test_evaluation`,
`test_admin_interface`, `test_admin_api`, `test_model_providers`, and `test_main`.
Ruff lint and formatting checks and local documentation links passed. Mypy
passed for the three changed implementation modules with `--python-version 3.12`,
matching the installed interpreter. The configured Python 3.11 check stopped
in an installed NumPy stub that requires Python 3.12 syntax. No live provider
inference or running-server restart was performed for this change.

## Maintenance

Update this decision, the MCP guide, and the admin API and evaluation docs when
argument precedence, default serving, session ownership, or endpoint scope changes.
