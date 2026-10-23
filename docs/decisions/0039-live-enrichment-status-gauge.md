# 0039 Live Enrichment Status Gauge

Status: Active
Audience: maintainers, DREAMLAND operators, presenters, and coding agents
Purpose: record the terminal-owned live status behavior for observing the
asynchronous EnrichmentAgent without turning status into an event stream.

## Overview

Automatic enrichment runs beside foreground interaction, so a static
`enrichment status` snapshot is often too brief for an operator to correlate
inflight and completed work with the visible user experience. Operators need a
low-cost live view during tuning and conference demonstrations, but that view
must not add model calls, alter scheduling, or flood terminal scrollback and
session traces.

## Decision

`enrichment status -f` and `enrichment status --follow` are a process-wide live
gauge. DREAMLAND renders the first status immediately and redraws the same
three-line frame once per second until any key is pressed. After terminal
capability checks pass, the transport enters the terminal's alternate screen
before the first frame. Every frame begins at the screen home position and
clears the display before rendering, so redraw is independent of frame height,
terminal wrapping, or scrollback position.

The stop key is consumed rather than becoming input to the resumed console.
Input draining is sequence-aware and bounded: when the first byte can begin a
terminal escape sequence or encoded multibyte key, the transport accepts its
continuation bytes within strict time and byte limits. It does not use an
unbounded read, and the bounded quiescence window limits how much adjacent
input can be consumed. This covers fragmented terminal key sequences without
adding noticeable exit latency.

Keypress, channel closure, shutdown, and status-read failure all pass through a
guaranteed cleanup path. Cleanup restores the main screen and prints one final
status snapshot there before the normal prompt resumes or the command exits.
The final snapshot provides a durable visible result without preserving the
alternate-screen refresh history. If the most recent status read failed, the
transport prints the same bounded type-only error in its place.

This is deliberately a gauge rather than an enrichment-event tail. Each tick
calls the existing thread-safe `EnrichmentControl.status()` snapshot and may
miss activity that begins and ends between ticks. Exact trigger, graph, patch,
and failure reconstruction continues to use retained enrichment events and
Langfuse traces. Follow mode creates no model calls, model output, worker
threads, scheduler work, or new configuration.

`src/dreamland/console.py` owns the reusable `ConsoleRefresh` description,
alternate-screen control constants, and status-frame renderer.
`src/dreamland/console_runtime.py` and `src/dreamland/ssh_sessions.py` own
blocking waits, one-second timing, sequence-aware bounded input consumption,
keypress/channel/shutdown detection, terminal line endings, alternate-screen
lifecycle, and cleanup. The enrichment service remains unaware of the view.

Live refresh requires a usable interactive terminal. The local console
requires terminal input and output and rejects `TERM=dumb`. SSH requires a PTY
request whose terminal type is nonempty and is not `dumb`; an exec request
without a usable PTY receives a clear error and exit status 2 instead of raw
cursor-control output.

The initial command result retains ordinary tracing semantics.
Alternate-screen controls, repeated frames, and the restored-screen final
snapshot are presentation-only transport writes. They do not create a
one-hertz stream of artificial session-output observations. Console logs record
one refresh start and one stop with bounded target, transport, reason, update
count, and duration fields. SSH records include the session id.
Terminal-capability rejection is logged once. A status-read failure emits one
warning and replaces the frame or final snapshot with a bounded error that
contains only a stable failure label and exception type. Neither logs nor
operator output retain exception prose, provider-authored text, response
bodies, or tracebacks. Polls and successful ticks are not logged individually.

## Alternatives Considered

Appending a complete status snapshot every second was rejected because it
would flood scrollback during long observations and make presentation output
harder to follow. Saving a cursor position in the main screen and repeatedly
erasing below it was also rejected: an initial frame can scroll before its
cursor anchor is useful, and later redraws can overwrite unrelated main-screen
content as terminal dimensions change.

Treating status follow as another log-tail mode was rejected because status is
a current process-wide gauge, while enrichment events have session and job
identity, retention, and cursor semantics.

Moving the wait loop into `EnrichmentRuntime.status()` or the scheduler was
rejected because terminal input, cursor control, and channel lifecycle belong
to transports. Status reads must remain short and non-blocking.

Silently emitting ANSI sequences to redirected or non-PTY output was rejected.
Operators can request a PTY explicitly when using SSH exec mode.

## Consequences

Operators can see inflight, queued, completed, and failed counts evolve without
manual command repetition or terminal spam. Multiple followers remain
independent readers and do not affect enrichment admission or state mutation.

The live animation deliberately disappears when DREAMLAND restores the main
screen. Only the final snapshot is added to ordinary scrollback. Terminal
cleanup and bounded sequence draining add small, fixed exit work but prevent a
partial key sequence from leaking into the next prompt under normal terminal
delivery timing.

The view is observational rather than lossless. A future session/job-scoped
`enrichment events -f` would be a separate feature over retained event cursors,
not an expansion of this status gauge.

## Maintenance

Update this decision when refresh timing, terminal requirements, status fields,
alternate-screen lifecycle, input-drain bounds, failure-reporting policy,
event-follow semantics, or the console/transport ownership boundary changes.
