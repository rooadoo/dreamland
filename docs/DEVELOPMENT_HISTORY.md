# Development History

Status: Historical
Audience: maintainers and coding agents
Purpose: summarize project evolution as inferred from repository history.

## Overview

This history is based on git commit subjects and current code structure. It is a
maintenance aid, not a full changelog.

## Timeline

Early work established the first SSH simulation slice: a Paramiko-backed server,
server profile, command backend, deterministic behavior for tests, and localhost
SSH coverage.

The shell simulation then grew support for bash-style history and terminal line
editing. Roadmap notes were added to track deferred terminal and shell behavior.

Session metadata was added so agent contexts can include connection details.
This allows prompts and traces to distinguish the external connection into a
DREAMLAND session from the simulated world profile entry host.

Shell variables and environment state were introduced for commands such as
assignment, `env`, `printenv`, `export`, `unset`, `set`, and `declare`.

Streaming LLM output was added for SSH clients, followed by Ctrl-C interruption
support. Interrupted commands record rendered output, interruption state, and
signal information.

Structured shell state became a major feature. Model responses were moved toward
a JSON envelope carrying terminal output and explicit state deltas. Nested shell
handling, structured response validation, and shell stack repair logic followed.

The codebase was refactored into public facades and focused internal modules.
Agent orchestration, state codecs, built-ins, structured parsing, tracing, SSH
transport, session loops, streaming, and session-state transitions now have
separate owners.

Configurable model controls were added for model name, reasoning effort,
reasoning summaries, verbosity, and output-token limits.

Runtime logging and output safeguards were added. Logging now covers lifecycle,
commands, replies, tokens, console events, interruptions, and output-cap hits
with configurable levels and event filters.

The Dreamland console feature added local process control, bounded log
inspection, Langfuse-backed session and command introspection, raw inspection,
state deltas, tab completion, and shutdown.

Spiritguide added a session-scoped operator agent for guiding live state
without emulating terminal commands. Its model calls have direct tracing,
structured state updates, and explicit apply and dry-run inspection.

## Maintenance

Append major project phases after they merge. Keep this document factual and
grounded in repository history.
