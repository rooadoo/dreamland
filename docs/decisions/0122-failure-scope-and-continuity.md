# 0122 Failure Scope and Continuity

Status: Active
Audience: backend maintainers, prompt authors, DREAMLAND operators
Purpose: prevent an isolated command failure from becoming an unsupported
persistent constraint through command state and enrichment.

## Overview

An isolated command error must not acquire unsupported persistent causes
through world-state recording and enrichment.

The fix narrows failure creation, recording, and enrichment through the
existing prompts. It preserves deliberate scenario faults and actual
consequences of simulated activity. It does not force commands to succeed or
silently heal existing worlds.

## Details

### Causal boundaries

1. The terminal interface's output and interaction limits are not evidence of
   host resource exhaustion. A workaround must remain local to the invocation
   or interface. A large finite command or many background launches does not
   alone establish an ongoing resource fault.
2. A command error establishes that operation's outcome at its observation
   point. State must distinguish the event from a supported continuing cause.
   Later commands are evaluated against their own requirements and current
   evidence. Actual recovery updates or removes obsolete active facts through
   existing directives; retries and shell exits alone do not prove recovery.
3. The focused Coordinator retains failure scope and time in the question.
   The independent Expert retains its real-system framing and creative
   latitude, but may not invent missing restrictive causes merely to explain
   an error. A symptom does not establish its underlying configuration or
   consuming process. Invented measurements and mutually supporting candidates
   cannot corroborate one another.
4. The Integrator applies the same boundary to safely representing eligible
   candidates against the current snapshot. Historical wording cannot turn
   an unsupported explanation into evidence. It omits unsafe candidates, uses
   the existing no-op contract when necessary, and preserves compatible
   diagnostic detail and unrelated enrichment. Current recovery evidence must
   not be overwritten by an older explanation.

### Beta scope

Only five production prompt modules change: terminal IO, world-state guidance,
focused Coordinator, Expert, and Integrator. There are no changes to runtime
schemas, parsers, patch authority, schedulers, model settings, or transport.
The existing graph refreshes the snapshot before integration and verifies
referenced state and patch dependencies. Those checks already reject stale
references; they do not decide whether arbitrary natural-language causes are
justified. This change adds no broad fault classifier or automatic repair.

### Validation and rollout

Provider-free regressions cover state semantics, prompt loading, graph
refresh, and safe application. Live checks assess model behavior separately.

Prompts are loaded when agents are constructed. A deployment must construct
fresh command and enrichment agents; opening a new session against existing
agents is insufficient. No migration or repair of the affected session is
included. Review any persisted poisoned state separately rather than silently
deleting facts whose original authority is no longer recoverable.

## Maintenance

Keep the command and enrichment rules aligned. When changing terminal covers,
creative latitude, failure recording, or recovery semantics, repeat both the
failure-amplification checks and controls for legitimate faults and ordinary
enrichment. Extend deterministic state authority only with an explicit schema
and owning-layer tests, not heuristics over arbitrary prose.
