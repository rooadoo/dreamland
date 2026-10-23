# 0001 Documentation System

Status: Active
Audience: maintainers and coding agents
Purpose: record the decision to use a schema-driven documentation system.

## Overview

DREAMLAND needs durable project context because the product model is broader
than the current SSH entry point. Coding agents also need a reliable way to find
the right context before editing code, prompts, tests, or configuration.

## Decision

Use a small documentation system under `docs/` with required front matter-style
fields:

- `Status`
- `Audience`
- `Purpose`

Topic docs should also include overview, detail, and maintenance sections unless
a short reference format is clearer.

`README.md` remains concise. `AGENTS.md` directs coding agents to the project
model and documentation schema. `docs/INDEX.md` provides the preferred reading
order.

## Consequences

Documentation changes should land with behavior, prompt, profile,
configuration, operational, or test-policy changes. Durable decisions should be
captured in `docs/decisions/` when they explain why a direction was chosen.

The documentation schema is intentionally lightweight. It can be validated by
simple tests later without adding a documentation framework.

## Maintenance

Update this decision if the documentation schema, location, or agent
expectations change.
