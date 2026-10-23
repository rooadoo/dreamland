# Command Stream Cancellation Respects Iterator Ownership

Status: Active
Audience: DREAMLAND maintainers, UI BFF developers, deployment owners, and
coding agents
Purpose: record the thread-ownership correction for live command cancellation
and the focused UI retest required before Phase 7D is merged.

## Overview

A Phase 7D live UI cancellation eventually reached the expected interrupted
state, but the backend logged `ValueError: generator already executing`. The
product cancellation watchdog ran on a different thread from the worker that
was advancing the LangGraph stream. `CommandExecutionControl.cancel()` invoked
registered stream-close and graph-recording callbacks on that watchdog thread,
allowing it to close an executing Python generator and access graph checkpoint
state concurrently with the stream owner.

The product core's logical cancellation and publication fence still worked,
but successful eventual recovery does not make concurrent generator or
checkpoint access safe. A live cancellation must not produce an internal
traceback or rely on an implementation-specific generator race.

## Decision

Treat every command graph stream as a single-owner iterator. The worker thread
that advances the iterator exclusively owns these operations:

- advancing or closing the iterator;
- reading or updating graph checkpoint state for the command; and
- installing the once-only interrupted graph result.

A cancelling request thread, product watchdog, SSH reader, or deadline observer
may set the thread-safe command interruption flag. Cross-thread cancellation
callbacks may invoke only collaborators explicitly designed as interruption
markers, such as the tracing callback's bounded interrupted flag. They must not
close or advance the graph stream and must not read, update, or delete its
checkpoint state.

The stream owner checks the interruption flag at iteration and completion
boundaries. Once it regains control after cancellation, it renders the bounded
interrupted result from `CommandExecutionControl`. Its owner-thread `finally`
path closes the iterator and performs the one-shot graph-recording fallback,
including when a provider exception unwinds the iterator. On the normal
cancellation path, that owner-thread record completes before the one
interrupted terminal is yielded. The existing claim prevents a second record,
and exact checkpoint-thread reaping remains after physical executor completion.

Keep the product core's logical cancellation semantics unchanged. When cancel
wins, command output and accepted-state publication are fenced immediately;
late provider output or completion cannot be accepted even if the provider SDK
does not return control to the stream owner immediately. The logical slot
retains truthful cancellation/interruption recovery until physical cleanup
settles. This remains cooperative provider cancellation, not a claim that
Python can forcibly terminate an in-flight network call.

Apply the ownership rule at the shared command-agent boundary so direct Stage,
Forge-to-Stage, source-neutral Presenter launch, authored attendee, and normal
SSH control paths cannot register an unsafe stream-close or graph-write
callback. Do not add a worker, monitor, timer, polling loop, provider call, or
listener to ordinary DREAMLAND composition. Existing product watchdogs and
control observation intervals remain unchanged.

Keep every published product API artifact, including `v0.7.0-rc.4`, byte-for-
byte immutable. The correction changes internal cancellation ownership only;
it does not change routes, DTOs, idempotency responses, authority, development
identity, generated clients, capability claims, or contract manifests.

## Consequences

Cancelling threads can no longer race Python generator execution or graph
checkpoint mutation. Iterator closure, interruption recording, and checkpoint
reaping have explicit ownership and ordering. The output and accepted-state
fence remains prompt even when physical provider cooperation is delayed.

Normal SSH Ctrl-C retains its existing behavior without new steady-state cost.
The shared correction removes unsafe cross-thread work rather than adding a
normal-runtime cancellation service. Provider transports that do not promptly
return from an in-flight call may still delay physical worker release; that
limitation remains visible alongside the existing product deadline, worker,
attempt, usage, and shutdown safeguards rather than being misrepresented as a
forcible transport abort.

Phase 7D must not merge solely on deterministic test evidence. After automated
coverage is clean, the UI team must repeat a focused cancellation against the
live combined target and confirm an interrupted terminal, stable recovery,
released resources, and absence of `generator already executing` or related
cross-thread cancellation tracebacks. Phase 8 begins only after that retest and
the Phase 7D merge.

## Evidence

Provider-free owning tests must cancel a deliberately executing stream from a
second thread and prove that iterator close and graph-state access occur only
on the owner. They must cover cancellation before iteration, during iteration,
and at the completion boundary; exactly one interrupted graph record and
terminal; late-output and accepted-state fencing; cancel idempotency replay;
event/result recovery; conservative usage settlement; resource cleanup; and
ordinary SSH Ctrl-C compatibility.

The focused live UI retest described in
`docs/PRODUCT_ATTENDEE_LIVE_DEVELOPMENT.md` is a release gate rather than an
automated provider test. Record its result here, along with the implementation
commit and owning automated suite results, before merging Phase 7D.

The correction was implemented by commit
`228e9375ed7e1264538d23f00481f85630e32910`. Automated verification passed the
complete 1,667-test repository suite with four expected skips, a 327-test
shared cancellation/runtime suite, and a 130-test Phase 7D contract/runtime
audit plus five focused cancellation baselines. The audit found no generator
re-entry, cancellation-callback, stream-close, or traceback diagnostics;
confirmed that normal disabled startup imports no product modules or extra
threads; and confirmed that no contract or UI-reference bytes changed. At that
retest-candidate publication, the focused live UI gate remained pending.

On 2026-08-29, after the retest candidate was handed off at
`e20f9e89aa92b43a7f521066be996ea2add09ea4`, the project owner directed that
Phase 7D be merged and development continue. That acceptance closes the focused
live UI retest gate; no additional cancellation defect accompanied the merge
direction.

## Maintenance

Update this record with `src/dreamland/agent_core.py`,
`src/dreamland/models.py`, `src/dreamland/product/live_agent.py`,
`src/dreamland/product/runtime_executor.py`, their cancellation tests,
`docs/ARCHITECTURE.md`, `docs/TESTING.md`, and
`docs/PRODUCT_ATTENDEE_LIVE_DEVELOPMENT.md` whenever command interruption
ownership, physical cancellation, graph recording, publication fencing,
checkpoint reaping, or the Phase 7D retest gate changes.
