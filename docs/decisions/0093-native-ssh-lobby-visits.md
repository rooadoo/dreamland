# 0093 Native SSH Lobby and Isolated Visits

Status: Active
Audience: maintainers, operators, security reviewers
Purpose: record the native lobby admission boundary, visit ownership, and trusted public references.

## Overview

The optional lobby makes curated saved worlds accessible through the existing
SSH listener. Agreement belongs to the real connection; each world selection
owns a separate simulated session. This implements the agreed
[development plan](../SSH_LOBBY_DEVELOPMENT_PLAN.md) within the native backend
lane and preserves [shared-password authentication](0046-native-ssh-attendee-authentication.md)
and [persistent host identity](0047-persistent-ssh-transport-identity.md).

## Details

### Parent connection and visit ownership

`direct` remains the default and retains private shell/exec behavior. `lobby`
requires a PTY shell; mode/controller mismatch fails startup. Neither username,
remote exec, nor simulated output can select the mode. Unsupported SSH
capabilities and the attendee operator namespace remain denied.

The controller owns welcome, disclosure, exact agreement, the picker, boundary
markers, final exit status, and parent closure. It creates a backend session
only after valid agreement and a current catalog selection. Each visit receives
a new session, state copy, editor, history, shell stack, and checkpoint namespace.
Outermost shell exit returns a typed outcome without closing the real channel;
nested shells retain existing behavior.

Operator closure invokes the shell's close callback when unregistering,
immediately fencing command admission and writers. Pending provider calls can
finish later, but cannot write to another screen or visit. Command checkpoint
resources are released after their last worker exits. Four unfinished retired
shells per parent temporarily prevent new visits. Enrichment cancellation clears
queued session payloads; a finite executor backlog prevents repeated switching
from retaining unlimited queued work behind blocked providers.

### Catalog and notice semantics

A bounded persistent JSON catalog contains explicit local saved profile names,
public titles/descriptions, and contiguous display order. Updates validate the
complete candidate and persist it atomically before publishing an immutable
snapshot. A stale snapshot requires a new selection. Profile launch reads and
validates one detached saved copy, with no packaged/default fallback. `exact`
snapshots and saved shell/history restoration are unsuitable for fresh visits.

Public copy rejects terminal controls, invisible formatting, and reserved
controller markers. Plain-text notice files are read per parent connection;
their content hash, including contact and optional Langfuse disclosure, identifies
the accepted version. No template language or attendee interpolation is involved.
The agreed notice describes OpenAI processing, so normal lobby composition
rejects other configured model providers before catalog/index creation. Changing
the lobby processors requires an explicit scope and notice revision.

Phase boundaries discard bytes already buffered without sleep-based flushing.
Known trailing CR/LF and partial key sequences are completed with bounded input;
cancellation works even within a partial key. Subsequent input must satisfy the
new phase's grammar. This cannot distinguish delayed scripted bytes from typing
and does not prove a human read the notice.

### Curated evaluation worlds and terminal presentation

The initial [three-world catalog](../../lobby/README.md) uses the highest
reported rich-detail count within each Spiritguide scenario, with complete
premise support. Published aspect grades resolve the supermarket/offshore
ties; the earlier repeat resolves the identical colonyship grades. This avoids
inventing an aggregate quality score for research that reports separate
dimensions. Local lobby profiles preserve the complete archived state and
change only the profile name. The checked-in provenance records source and
local hashes so operators can recreate these ignored local profiles.

ASCII connected-node frames, section labels, indented descriptions, and
separate input prompts give each admission step a clear visual boundary. The
controller owns this decoration so operator notice files remain plain text.
The layout uses at most 80 columns and compact prompts on short or narrow
terminals, retaining paging and the existing agreement grammar. The initial
plain decoration is extended by capability-gated basic color and visible-screen
clearing in [decision 0096](0096-ssh-lobby-presentation.md), with its own pending
human checkpoint. Continuation and final actions share one
footer renderer; pagination reserves the actual wrapped action height to keep
instructions visible. Paragraphs and short closing contact lines stay with
their context when they fit. Agreement appears only after the final guidelines
page, and the input cursor is reserved for actions that require Enter. Runtime
indexes, local profiles, and the working INI stay outside version control.

### Public references and authority

A cryptographically generated, collision-checked `DL-XXXX-XXXX-XXXX-XXXX` reference
identifies one visit. An owner-only SQLite index retains its internal session,
profile, parent/acceptance correlation, timestamps, and lifecycle status. Startup
recovers nonterminal entries as interrupted without recreating runtime sessions.
Terminal retention defaults to 30 days, with explicit cleanup and cleanup during
startup/allocation. The index contains no transcript bodies and grants no access.

Immutable runtime provenance is separate from model-writable state and codecs.
Saved profiles, model patches, fake output markers, nested shells, and live
profile application cannot mint or replace an authoritative reference. Command,
Spiritguide, and queued enrichment contexts capture that provenance when work
is admitted. Tracing projects the scalar `dreamlandPublicId`; it does not become
another identity authority.

Native console selectors recognize the public namespace first and require an
exact complete match, with case normalization. Unknown, incomplete, expired,
and failed-start references do not fall through to prefixes, the active session,
or the only live session. Ended references support retained inspection while
rejecting live mutations. Registry and REST/MCP lookup contracts continue to use
internal IDs. A short exact-registration guard fences mutation application
against concurrent closure; the registry lock is never held across model calls.

### Verification and release boundary

Owning tests cover deterministic input, catalog persistence and failure,
identity collisions/recovery/spoofing, console targeting, trace metadata,
queued/running cancellation, and checkpoint reclamation. Real Paramiko coverage
exercises admission policy and four fresh visits on one channel, including
nested SSH and idle/busy public-ID closure. The historical command-capture
fingerprint was independently checked: removing the new quiescent checkpoint
retirement method reproduces the prior `agent_core.py` hash exactly.

The [independent security audit](../SSH_LOBBY_SECURITY_AUDIT.md) records attacks,
findings, fixes, and limitations. The [controlled trace check](../SSH_LOBBY_TRACE_CHECK.md)
verified all 45 observations and the deployed metadata filter, including five
real generations. Spiritguide's response in that check failed existing structured
parsing and applied no change; metadata correlation still succeeded. The
Langfuse web filter interface remains an operator visual rehearsal checkpoint.
No public deployment or attendee transcript publication is part of this change.

## Maintenance

Implementation: `8fdc86b` (`feat: add optional native SSH lobby and isolated visits`).

Keep [SSH Lobby operations](../SSH_LOBBY.md), admission copy, tests, audit evidence,
and the development plan aligned when changing connection/visit ownership,
public identity, selectors, catalog persistence, processors, or input limits.
Changing authentication, shared-state semantics, publication, or public access
requires a separate explicit scope decision.
