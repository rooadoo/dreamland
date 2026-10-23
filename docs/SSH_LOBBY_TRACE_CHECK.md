# SSH Lobby Trace Integration Check

Status: Active
Audience: DREAMLAND maintainers, operators, security reviewers
Purpose: describe a disposable check of visit identity propagation and trace filtering.

## Overview

Use operator-created disposable activity to check the deployed tracing integration.
A trace-correlation check does not certify model quality or public deployment readiness.
Historical run evidence is published separately.

## Details

1. Enable tracing for a disposable lobby world and record its public visit ID.
2. Make one bounded command request, one Spiritguide request, and one manual
   enrichment submission. Inspect apply outcomes separately from trace export.
3. Query each exact lifecycle, command, Spiritguide, and enrichment trace. Check
   that outer observations, graph steps, and model generations contain the same
   scalar `dreamlandPublicId`. Follow pagination to inspect every observation.
4. Query the same time interval with an exact metadata filter for that public ID.
   Compare the returned observation IDs with the independent per-trace queries.
5. Search the public ID in the deployed Langfuse UI and confirm the observations
   are navigable. API filtering and UI behavior require separate checks.
6. Close the disposable visit and verify retirement and queued-work isolation.

Provider-free identity and tracing tests cover missing credentials, disabled
tracing, immutable provenance, queued-job capture, and closed-visit isolation.
See [SSH Lobby](SSH_LOBBY.md) for operation and
[Testing](TESTING.md) for owning suites.

## Maintenance

Repeat the affected checks after changes to metadata builders, callback
propagation, asynchronous context capture, SDK versions, or deployment tracing.
Keep run outputs and trace identifiers outside the source release.
