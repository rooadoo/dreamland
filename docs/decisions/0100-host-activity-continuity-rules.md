# 0100 Host Activity Continuity Rules

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators
Purpose: record the follow-up prompt experiment preserving identity, login,
activity, and time continuity while retaining mandatory foreground population.

## Overview

These prompt rules preserve continuity across network exploration, repeated
inventories, account transitions, and asynchronous enrichment.

The richer-world objective and its coherence requirements follow
[Project Model](../PROJECT_MODEL.md). Population floors and scaling remain as
recorded in [decision 0098](0098-mandatory-foreground-host-activity.md).
The separate [JSON-mode fix](0099-foreground-openai-json-mode.md) addresses
syntax; these rules address the meaning and persistence of generated facts.

## Details

### Prompt responsibilities

| Module | Additional responsibility |
| --- | --- |
| `world.md` | Resolve canonical hosts before population; persist structured interface ownership, platform identity, destination SSH keys, and coherent host clock/boot observations. |
| `ambient_realism.md` | Keep living process and session identities stable; evolve counters and short-lived clients with consistent lifecycle consequences and summaries. |
| `environment_metadata.md` | Separate original login, current host's authenticated login, and effective account; preserve login observations while respecting runtime socket authority. |
| `state_contract.md` | Permit minimal required continuity anchors alongside SSH transitions; demonstrate immediate host-key persistence without treating pending authentication as success. |
| `spiritguide.md` | Write chosen network addresses in runtime-readable interface records before the initial connection is allocated. |
| `enrichment_expert.md`, `enrichment_integrator.md` | Prevent discovery or authentication attempts from implying successful sessions, account provisioning, or unsupported access policy, including on inactive targets. |

Facts that must survive host changes use concise `summary` fields because
inactive host cards preserve these fields. Owned addresses belong in structured
`network.interfaces_by_name` records; prose can explain but cannot replace them.
SSH keys belong to the destination service and algorithm, while known-hosts
acceptance belongs to each client. Login observations carry identity and time,
without duplicating runtime connection IDs or endpoint registries.

Stable service bindings, process ownership/start times, and ongoing login
identities change only with a compatible lifecycle event. Counters, idle time,
and client turnover may evolve. Clock observations guide later output but do
not freeze time. Historical file and login timestamps do not become current
time. Explicit stops, disconnects, reboots, and clock changes remain effective.

### Scope and limitations

This is a prompt-only behavior experiment using existing state collections.
It adds no resolver repair, login-owner projection, simulated clock, key
generator, deterministic population renderer, or new configuration. Earlier
conflicting seeds and already allocated connection endpoints are not repaired
retroactively. Prompts cannot recover evidence absent from the supplied context
or guarantee cryptographic consistency between invented public-key bytes and
fingerprints. If live review still exposes these failures, runtime ownership
and evidence projection remain possible follow-up work.

The shared Integrator prompt now deliberately changes for both enrichment modes.
Its obsolete pre-ambient byte-freeze assertion is removed; existing role
serialization, prompt composition, graph handoff, and authority checks remain.
Automated checks validate prompt loading and contracts, not model adherence.

Validation at this checkpoint: 209 focused tests passed before the baseline
commit, covering the population prompts and incoming JSON-mode fix. The
follow-up passed 150 focused prompt, enrichment graph/authority, and connection
tests. A direct parser replay accepted all nine single-line state-contract
examples, including the host-key patch. A resolver/projection check confirmed
that the documented structured address resolves to the canonical host and that
address, host-key summary, and clock summary survive inactive-host compaction.

### Manual review

Restart/recreate the command and enrichment agents before further testing;
reconnecting alone does not
reload prompts. Use a newly generated world when evaluating Spiritguide's
address authoring, and a fresh session when comparing emulator behavior.

1. Inspect `hostnamectl`, `ip -br addr`, `ss -lntup`, `ss -ntp`, `ps -ef`, and
   `w`; repeat in a different order. Check scaled activity and shared identities,
   PID/owner/bind consistency, user counts, and structured address ownership.
2. Connect to an already observed address of the same host. Confirm its
   canonical host/platform remains the same. Observe a remote fingerprint,
   fail or cancel authentication, and retry from a different client; compare
   algorithm and fingerprint before logging in successfully.
3. Log in to a remote host with a different account, then use `sudo -i` or
   `su`. Compare `whoami`, `w`, session-related process rows, and socket views.
   Confirm privilege changes preserve the remote login owner and other users.
4. With enrichment enabled, inspect destination state around failed and pending
   authentication. Check that no successful session or restrictive access
   policy was introduced solely from that attempt.
5. Compare `date -Is`, uptime, process starts, user login times, and a diagnostic
   script before and after host hops. Stop a nonessential background service
   or disconnect an ambient user and confirm corresponding rows stay absent.

## Maintenance

Keep this decision, [State Profiles](../STATE_PROFILES.md),
[Enrichment Agent](../ENRICHMENT_AGENT.md), and the affected prompt modules
aligned. Add evidence as follow-up checks are completed; distinguish prompt
improvements from later runtime guarantees.
