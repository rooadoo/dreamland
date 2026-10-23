# 0045 Person-Centered Ambient Presence And Bounded Integrator Refresh

Status: Active
Audience: maintainers, DREAMLAND operators, researchers, prompt authors, and
coding agents
Purpose: specialize ambient enrichment around recognizable people, preserve
foreground-row attribution through independent grounding, and recover one
safely re-integrable proposal after genuine foreground-evidence drift.

## Overview

Ambient presence centers a named human or scenario-supported person-equivalent
whose time-qualified activity leaves connected residue. Focused enrichment
continues to supply technical depth. Foreground evidence retains a bounded
public model view with stable latest-per-scope private authority and permits
exactly one Integrator-only refresh after genuine evidence drift.

Positive foreground-row associations remain reserved to their observed owners.
A distinct historical or later event must be grounded independently of protected
row values. Field-sensitive vocabulary checks permit ordinary practitioner
titles while rejecting internal workflow leakage. The current graph is
`enrichment-v15`; the ambient handoff policies use version 3. Additive authority,
atomic clusters, and final safe-apply checks remain intact.

## Decision

### Human-Centered Ambient Presence

The `presence-gap-v2` payload retains its existing five-field shape and
`independent_life` objective. Its exact `subject_preference` value becomes:

```text
named_human_or_person_equivalent_excluding_foreground_user_and_service_accounts
```

This string has one intended reading. Ambient work centers one recognizable,
named human individual other than the foreground user. A scenario-native name,
handle, call sign, or equivalent individual designation is sufficient. Only
when established world evidence or explicit direction calls for nonhuman
people may the subject be a named person-equivalent with individual personal
or social agency. A service, shared, role, daemon, machine, automation, device,
institution, or group identity never satisfies the preference.

The foreground user is the person behind the current interaction, not merely
the current effective account string. That person includes active, login,
effective, and privileged accounts plus any alias that established state or
direct evidence explicitly associates with the interaction. A `su`, `sudo`,
privilege, or nested-SSH transition does not create a new ambient individual.

Positive foreground observations also retain their associations and do not
become creative source material for a new person's event. A directly observed
login, session, process, or socket row—including its username, PID, TTY,
endpoints, source, and observation time—cannot be transferred to a newly
invented ambient person unless the row itself identifies that person. A value
learned only from that protected row also cannot ground a purportedly distinct
historical or later event for the proposed person. Reusing such a value is
permitted only when independent established state or supplied non-row evidence,
including a selected reference, separately connects it to that event. Ambient
work may still create a distinct historical or later event when it is
explicitly time-qualified, independently grounded, and not presented as the
observed foreground row. This
is an attribution and grounding rule, not a ban on generating process, login,
network, or session history or on using shared infrastructure established
elsewhere.

The causal quality target becomes:

```text
recognizable named individual
    -> time-qualified personal, work, or social activity
        -> connected observable residue
```

Services, processes, devices, organizations, and technical artifacts may
support that chain but cannot become its sole subject. This is not a new actor
registry, counter, dimension, or autonomous persona system. It is an
ambient-only planning and mapping constraint intended to complement, rather
than replace, focused technical enrichment.

Explicit operator, Spiritguide, and state-profile direction remains
authoritative. A world specified as solitary, abandoned, pristine, newly
provisioned, minimally populated, or otherwise incompatible with another
person must return a compatible no-op or compatible residue; Enrichment must
not invent a person to defeat that constraint.

### Role Boundaries

The Ambient Coordinator receives the exact v2 presence-gap preference and must
express a person-centered assignment. Deterministic brief construction appends
a versioned ambient-only requirement, `ambient-human-presence-v3`, to the
Coordinator's ordinary Expert question. The independent Expert still receives
the same neutral six-field brief, not DREAMLAND state, `presence_gap`, or patch
vocabulary. Focused brief construction remains byte-equivalent to its prior
behavior. Ambient brief preparation logs
`policy_version=ambient-human-presence-v3`, action, hash, and length metadata
without logging the appended prose; focused preparation emits none of those
fields. Version 3 retains the account-transition and row-ownership rules from
version 2 and additionally forbids grounding a proposed person's distinct
event with a protected row value unless an independent non-row observation or
reference establishes that connection.

The shared Integrator system prompt remains byte-for-byte unchanged.
Deterministic invocation appends the inspectable
`enrichment_ambient_integrator_suffix.md` under policy
`ambient-human-integration-v3`, separated by one fixed delimiter, only for
`ambient_presence` calls. The same rule applies to initial and refreshed
Integrator calls. Focused Integrator `SystemMessage` content remains exactly its
prior bytes. Version 3 retains version 2's transfer protection and makes
Integrator omit a purportedly distinct event when its identity, ownership, or
specific activity depends on reusing a value supplied only by a protected
foreground row. It preserves independently grounded person-centered residue
and returns `no_safe_mapping` when the cluster depends on the prohibited row
transfer or value reuse.

The suffix treats ambient guidance as non-evidentiary mapping priority. When
the eligible report is service-only or otherwise lacks both a supported named
individual and connected observable residue, Integrator returns
`no_safe_mapping` rather than materializing a system-centered ambient patch.
This is a proposal-level causal-cluster requirement; it does not create a
second candidate-eligibility classifier or broaden Integrator authority. Its
`ambient_integration_policy_version`, `ambient_integration_policy_sha256`, and
`ambient_integration_policy_chars` are formatted/trace diagnostics rather than
typed retained fields. They require no model narration and do not expose the
policy prose in ordinary logs.

Foreground-row ownership and independent grounding are therefore
deterministically injected policies at all three ambient model boundaries, not
a new hard semantic parser. Safe apply
cannot reliably infer arbitrary free-text ownership or person aliases beyond
its typed authority fields. Adding a fuzzy post-patch lexical reject was
rejected because shared endpoints, PID reuse, later events, and paraphrases
would create both false positives and false negatives. A future typed activity
model may make this association machine-verifiable; this checkpoint keeps the
bounded three-role policy and existing final authority checks.

Neutral-brief vocabulary validation is field-sensitive. DREAMLAND-specific
control keys and markers remain prohibited throughout the brief, and routing
role markers such as `Coordinator`, `Integrator`, and `emulator` remain strict
in the Expert question and object keys. The `expertise.domain` and
`expertise.perspective` values, however, are ordinary real-world routing
lenses. Legitimate titles such as `OT operations coordinator`, `industrial
systems integrator`, or `embedded emulator specialist` are not treated as
internal leakage merely because they contain a generic role word. This changes
deterministic validation only; it does not loosen the Expert question or expose
DREAMLAND workflow state.

### Stable Foreground-Evidence Authority

The public role input remains bounded to at most four distinct recognized
complete inventories found in the last four target-host command records, plus
the newest recognized host-time observation. An inventory that ages out remains
absent from new model prompts because its snapshot is no longer a current
negative-evidence constraint.

Private authority advances to `foreground-evidence-v2`. Its digest uses the
newest recognized complete record for every exhaustive evidence scope across
the complete target-host history, plus the latest recognized host-time record.
The private view is never placed in model inputs, serialized patches, or
ordinary logs. Unrelated command history and public-window expiry therefore do
not change the digest. A first observation, a newly observed scope, a
replacement observation for an existing scope, or a new host-time record does
change it.

The split is intentional: the public view answers what currently constrains
generation, while the private view answers whether new direct evidence arrived
during model latency. Removing expiry-only conflicts does not make an old
inventory permanent proof of absence and does not expose full history to a
model.

### One Integrator-Only Refresh

When final safe apply finds a genuine `foreground_evidence` semantic-authority
conflict, DREAMLAND may make exactly one bounded refresh attempt for that job:

```text
same Coordinator decision and eligible Expert report
    -> fresh current snapshot and authority
        -> Integrator only
            -> ordinary normalization and validation
                -> ordinary atomic safe apply
```

The Coordinator and Expert are not rerun. The refresh reuses their source
material, resolves it against fresh state, and asks the DREAMLAND-aware
Integrator for a new mapping. The resulting patch receives the fresh private
authority dependency and passes through the same additive, provenance,
dependency, touched-path, cluster-atomicity, and final-apply checks as any first
attempt.

A second semantic-authority conflict is terminal. `active_session` conflicts,
including those caused by an active-host pivot, are not retried; a pivot means
the original host and foreground context are no longer the user's current
authority. There is no
optimistic bypass, stale-digest rebinding, per-contribution salvage, partial
cluster application, third attempt, timer, or durable retry queue.

The Integrator-only path remains an explicit inspectable LangGraph flow. Logs
retain bounded `integrator_refresh_attempt`, singleton `authority_domain`,
`integrator_refresh_outcome`, `integrator_refresh_latency_ms`, `eligible`,
`changed`, status, reason, and action without raw authority, model reasoning, or
additional model-authored logging. Multi-domain detail remains a formatted
diagnostic.

### Feature Lineage

The EnrichmentAgent experiment is recorded as a sequence of bounded decisions
rather than one retrospective design. This table is the capstone map; the
individual records remain authoritative for their own contracts and evidence.

| Checkpoint | Durable decision | Implementation commit |
| --- | --- | --- |
| Separate session-local agent, visible three-role graph, short locks, and additive safe authority | 0033 | `4c507dc` |
| Portable Expert boundary, candidate provenance, creative latitude, and safe integration | 0034 | `062e3ca` |
| Declarative materialization and session-correlated retained events | 0034–0035 | `7e17165` |
| Relation salvage and final-application trace closure | 0033–0035 refinements | `1b9ba3b` |
| Density shaping, automatic triggers, one deferred candidate, and deterministic normalization | 0036–0037 | `4a54106` |
| Foreground authority, live status, capacity/model comparison, and relation sizing | 0038–0041 | `328ef2e` |
| Ambient-presence mode, expanded triggers, three in-flight jobs, and 10,000 retained events | 0042 | `b494419` |
| Progress pulse, presence-gap routing, foreground evidence, and concise independent-life planning | 0043–0044 | `aff2d2a` |
| Person-centered ambient work, stable evidence authority, bounded Integrator refresh, field-sensitive routing vocabulary, and independently grounded foreground-row protection | 0045 | `33bf7ba`, `dc95f72` |

## Alternatives Considered

Keeping the ambiguous `non_login` or `person_like_non_login` label was rejected
because either can be read as permitting a service account. The longer exact
preference deliberately spends deterministic input characters to avoid model
ambiguity and Coordinator output-token cost.

Adding another presence dimension, actor type, or registry was rejected. The
problem is causal emphasis, not missing state schema. `presence-gap-v2`,
coverage counts, and anti-repetition fields otherwise remain stable.

Removing process, login, session, and socket observations from ambient role
inputs was rejected. Those surfaces can ground valuable historical and later
human activity. Reserving each directly observed row's identity and ownership
associations preserves that grounding without allowing a new person to take
over the foreground observation.

Treating explicit historical wording as sufficient separation was rejected.
A model can label an event
historical while still copying a protected row's endpoint into the invented
person's causal chain. The event now needs independent non-row grounding for a
protected value. A blanket ban on every repeated address, host, account, or
other scalar was also rejected: shared infrastructure remains valid when
established state or another selected reference independently supports the
connection.

Adding a fuzzy lexical ownership validator after integration was rejected. It
could not distinguish a transferred foreground row from a distinct later event
or shared infrastructure reliably, and would create a misleading appearance of
hard semantic enforcement.

Treating `coordinator`, `integrator`, or `emulator` as forbidden substrings in
every string field was rejected because those are ordinary real-world titles
and technical terms. Removing routing vocabulary checks entirely was also
rejected. Field-sensitive validation protects Expert questions and structural
keys while keeping `expertise` lenses portable.

Dropping the foreground-evidence dependency was rejected because direct
foreground observations must outrank speculative enrichment. Treating public
window expiry as a conflict was also rejected: it produces no new authority
and unnecessarily discards work.

Blind rebinding, partial-cluster salvage, and optimistic application were
rejected as higher-risk ways to recover model cost. Rerunning Coordinator and
Expert was rejected as unnecessary expense when their real-world material can
be remapped safely. More than one Integrator refresh was rejected because a
third mapping would usually arrive too late and could amplify contention.

## Consequences

Ambient jobs use a narrower experiential responsibility while retaining broad
scenario portability. Ordinary human settings should gain recognizable people
and candid connected residue; nonhuman-person settings remain possible when
supported; explicitly empty worlds remain empty. Focused enrichment behavior
and technical breadth remain stable. Focused Coordinator, Expert brief,
Integrator system message, schemas, authority, patching, and trigger routing do
not receive either ambient v3 policy.

Ambient work loses the ability to explain a new person by opportunistically
claiming an anonymous foreground row or copying a value available only from
that row into a nominally distinct event. It may instead retain the person's
other supported residue, use independently established shared infrastructure,
create a clearly distinct and independently grounded time-qualified event, or
return a typed no-op. This can reduce one patch's apparent activity density,
but it prevents a direct user-visible association from being silently
rewritten.

Real-world occupational lenses no longer fail merely because their names
contain generic routing words. Internal workflow leakage in a question or key
remains a hard validation failure. The change adds no model call, output-token
cost, state field, or telemetry field.

The current visible contract is `enrichment-v15` with
`ambient-human-presence-v3` and `ambient-human-integration-v3`.
`presence-gap-v2` and `foreground-evidence-v2` deliberately do not advance:
the follow-up changes how ambient roles may use supplied evidence, not what the
deterministic planning or authority payloads contain.

Public model context stays small and temporally honest. Private authority grows
with the number of recognized exhaustive scopes rather than raw command-history
length. One genuine evidence race can spend one additional Integrator call and
add latency, but it reuses the expensive Coordinator and Expert work and never
weakens final safe apply.

The current policy still records a coherent snapshot and residue. It does not
advance or expire live people, sessions, processes, or connections; that remains
future `WorldActivityAgent` work.

## Maintenance

Update this decision when the exact ambient subject preference, ambient Expert
task policy, ambient Integrator suffix, service-only mapping rule, foreground-
row attribution or independent-grounding rule, field-sensitive vocabulary
boundary, foreground-evidence policy, refresh limit, retryable authority
domains, graph topology, or telemetry changes. Keep
`docs/ENRICHMENT_AGENT.md`, `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`,
`docs/ADMIN_API_V0.md`, `docs/STATE_PROFILES.md`,
`docs/INDEX.md`, `ROADMAP.md`, prompts, graphs,
runtime service, and owning tests aligned.
Decision 0044 remains the historical record of the broader
scenario-open `presence-gap-v2` checkpoint; this decision supersedes only its
subject preference and ambient role emphasis. Initial implementation commit:
`33bf7ba`; the v15/v3 correction is implemented by `dc95f72`.
