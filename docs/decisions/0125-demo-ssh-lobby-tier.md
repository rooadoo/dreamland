# Separate Demo SSH Lobby Credential and Tier

Status: Active
Audience: maintainers, DREAMLAND operators, demo testers
Purpose: keep higher-cost emulator delivery behind a separate lobby credential
without changing concurrent general-use sessions.

## Overview

A global `ultrafast` emulator setting makes every attendee use the demo tier.
The operator requested a separate `_fast` login so routine use can retain `fast`
and demo connections can request `ultrafast`. This changes the current SSH entry
point described in the [project model](../PROJECT_MODEL.md).

## Details

- Lobby startup optionally reads `DREAMLAND_SSH_FAST_PASSWORD`. It has no INI
  equivalent. An unset/empty value disables demo admission; a nonempty blank
  value or one equal to the normal password fails before listener admission.
- Exact `dreamland_fast` and `dreamland_fast_simple` aliases accept only the
  demo password. Normal `dreamland` and `dreamland_simple` accept only the normal
  password. Only the documented suffix order is accepted. Direct entry retains
  literal usernames and ignores the optional demo credential.
- Every password attempt hashes its supplied value and performs both digest
  comparisons regardless of username or enabled demo admission. Existing
  configurable transport delays and password-only SSH restrictions still apply.
  Password values and digests stay out of repr, logs, and effective config;
  effective config exposes only a presence boolean.
- Startup constructs a separate command agent/backend for demo admission, with
  only `openai_service_tier` changed to `ultrafast`. Normal connections retain
  `[agent]` controls (`fast` by default). The local operator INI was returned
  from its global `ultrafast` override to `fast`; packaged defaults stay `fast`.
- The authenticated lobby connection chooses its backend before agreement and
  world selection. Every visit and its cleanup use that backend. Missing demo
  composition fails closed. No shared agent or settings object is mutated while
  admitting or running a connection, so concurrent normal/demo visits remain
  isolated and repeated selections retain their tier.
- Both backends share the startup-owned profile runtime, structured failure
  store, and event bus. Existing session registration, enrichment, lifecycle,
  cancellation, and deferred release continue to operate for either backend.
  Spiritguide and enrichment models retain their configured tiers.
- Requested tier is included in connection routing and visit-created lifecycle
  events. Existing model construction and provider token logging remain the
  source for request and returned-tier evidence. Authentication suffixes never
  replace the catalog's in-world entry username.
- `_fast_simple` composes the existing static/monochrome lobby option with demo
  admission. Other presentation and world behavior remain configured as before.

Implementation lives in [authentication](../../src/dreamland/ssh_auth.py),
[configuration](../../src/dreamland/config.py),
[startup](../../src/dreamland/__main__.py), and
[lobby routing](../../src/dreamland/ssh_lobby.py).

### Validation and manual checkpoint

Owning tests cover credential/suffix rejection, optional environment-only
loading, direct-mode scoping, redaction, composition of tier-only agent changes,
concurrent normal/demo commands, fresh world visits, release, and simple
presentation. Loopback SSH verifies all four aliases on one listener without
provider calls.

For an operator checkpoint after setting both passwords and restarting:

1. Keep `[agent] openai_service_tier = fast`. Connect as `dreamland` and
   `dreamland_fast` concurrently, using their respective passwords. Enter a
   world and run an ordinary model-backed command in each. Correlate their
   lifecycle/session IDs and model logs with requested `fast`/`ultrafast` tiers;
   inspect provider-reported tiers separately.
2. Exit the demo world and enter another. Verify the requested tier is still
   `ultrafast`, while another normal command still requests `fast`.
3. Connect as `dreamland_fast_simple`; verify the immediate monochrome welcome
   and the demo tier. Check `dreamland_simple` with the normal password too.
4. Try each password against the other login family, and try
   `dreamland_simple_fast`. All must fail. In an isolated restart with no demo
   password, verify both demo aliases fail while normal login still works.

Human terminal and live-provider results must be recorded separately from
automated checks. Deployment rollout is a separate operator action; this
decision records no remote configuration, credential, or service change.

## Maintenance

Update this decision, [SSH Lobby](../SSH_LOBBY.md), [Operations](../OPERATIONS.md),
and owning tests when credential storage, suffix parsing, tier composition,
backend ownership, or lifecycle logging changes.
