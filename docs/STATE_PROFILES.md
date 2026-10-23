# State Profiles, World State, And Host Metadata

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators
Purpose: explain how saved state profiles, host metadata, runtime world state,
and behavior instructions are split.

## Overview

State profiles are the required boot profile layer. Each new session starts
from a `dreamland.state_profile` JSON document. The packaged fallback profile
is `src/dreamland/default_state_profiles/default.json`, so DREAMLAND can start
without local operator state.

The command emulator receives one model-facing world record: a non-mutating
projection of mutable `world_state`. It does not receive a separate static
`world_profile` or active-host metadata block. Authoritative session and saved
state remain complete, and Spiritguide receives the complete world state.

Do not hard-code scenario facts into prompts when they belong in state
profiles or mutable world state. Do not put behavioral state-contract
instructions into a state profile.

## State Profiles

State profiles are JSON files loaded by `src/dreamland/state_profiles.py`.
Local profiles live under `state_profiles/`; packaged defaults live under
`src/dreamland/default_state_profiles/`.

Loading a name checks its local file first, then the installed package resource.
The package includes the neutral `default` profile and three example worlds:
`lobby-supermarket`, `lobby-colonyship`, and `lobby-offshore`. The examples are
runnable schema-version-2 derivatives of the selected evaluated worlds, with
authored initial-login banners. Their archived evaluation originals remain
unchanged; current file hashes and the limited derivation are recorded in
[lobby provenance](../lobby/provenance.json). The examples are available from
source checkouts and wheels. They do not change the configured default.
Local copies override bundled resources; listing and deletion operate on local
files. An installed example becomes locally editable when the operator copies
it into `state_profiles/`.

A world-level state profile should include `state.world_state` with:

- `entry_host`: the static configured starting host reference;
- `active_host`: the initial runtime active host reference, usually matching
  `entry_host`;
- top-level `hosts_by_name`: keyed host metadata, where keys may be hostnames,
  IP addresses, or other stable host references; the entry host and initial
  active host must be registered and each must have a `login_banner` string;
- broad purpose, simulation, and generation guidance.

`entry_host` represents the configured starting point. `active_host` is
mutable runtime state and is synchronized with the active shell frame as users
pivot between systems.

System-level and world-level state profiles may use lightweight string
templates such as `{username}` in saved state. DREAMLAND expands those
templates for the session's login when booting a new session. Native lobby
visits use the catalog's configured entry account; direct SSH retains its
authenticated, sanitized connecting account.

## Saved Initial-Login Banners

The current state-profile document schema is `2`. Each profile requires
`state.world_state.entry_host` and `active_host` to resolve to host records.
Each of those records requires a host-local `login_banner` string; they normally
reference the same host. Any other host's supplied banner is also validated.
For example, a Debian retail host can contain:

```json
{
  "hostname": "pt-por-042-pos03",
  "login_banner": "Debian GNU/Linux 12\nMorrowbasket Markets - PT-POR-042 - checkout lane 3\nMaintenance console: pt-por-042-pos03\n"
}
```

The banner is saved world content authored by Spiritguide, consistent with the
host's actual OS, purpose, and setting. It may omit an OS label where no release
is established. An explicit empty string means a deliberately silent login;
a missing field is invalid. The limit is 16,384 UTF-8 bytes. Newlines, carriage
returns, and tabs are allowed; other Unicode control characters in category
`Cc`, including terminal escape sequences, are rejected. Use ordinary display
text rather than dynamic placeholders for dates, elapsed time, or last-login
claims. Normal saved-profile username templates are expanded during session
initialization.

The shell entry path reads the initialized active host's saved banner and
writes it before the first prompt. Direct SSH shell entry and each fresh lobby
world visit use this path. It performs no model call and does not wait for
background enrichment. SSH exec responses do not include this presentation.
Later simulated SSH logins, including a return to the original host, are
emulated normally by the LLM. They do not replay the saved banner directly.

The banner remains in authoritative session state, checkpoints, saved profiles,
and Spiritguide's full world context for creation, updates, and repair. It is
removed from command-emulator and enrichment input projections, including
active-host metadata. Initial display creates no command-history entry. This
exclusion concerns the stored presentation field: independently authored
filesystem content such as `/etc/motd` remains ordinary inspectable world data.

Spiritguide creation must supply both valid starting-host references and their
banners. Detached creation/update validates the resulting state before reporting
success or saving, including dry runs. Missing or malformed banners produce an
operator-visible validation failure rather than an inferred Ubuntu greeting.
The former model-facing server-personality `profile:` field is removed. Host
OS and shell come from host metadata; saved scenario names remain available for
operational session identity and tracing. The real SSH protocol identifies
itself neutrally as `SSH-2.0-DREAMLAND` before any world is selected.

### Compatibility break and profile repair

Version-1 profiles are unsupported. There is no runtime conversion, generated
fallback greeting, or automatic replacement of local operator files. Loading
and saving enforce the version-2 contract. Merely changing `schema_version` is
insufficient: inspect the world, repair invalid host references, and author the
required host-local banners before validating the document. Because the current
loader rejects version 1, repair those documents offline or regenerate them
through Spiritguide creation; Spiritguide update starts from a valid profile.

Saving from a newly discovered active host also requires an authored banner for
that host, because the saved profile may start there. Have Spiritguide supply
the banner before saving; ordinary exploration does not require banners on
every discovered peer. System host identity and exact-profile shell frames are
validated against the host that will actually be entered.
Keep a backup of operator-owned data before editing it.

The four bundled profiles and 57 active local profiles present in this checkout
were repaired together. Local `state_profiles/` files are ignored runtime data;
their changes do not ship in a Git commit. Historical evaluation profiles,
frozen sources, failure capsules, and session indexes retain their original
schemas as evidence. To run an old evaluation world with this runtime, create
a separately identified schema-2 derivative and retain the original archive.

The local reference repair preserves established world content:

- `colonyship3`: the record keyed `operator-defined` already described hostname
  `bridge.aetheria`; it is rekeyed to `bridge.aetheria`, and `active_host` now
  matches the established entry reference.
- `colonyship4` and `colonyship5`: initial active references now select the
  authored `bridge` and `aether-core` entry hosts. Unused placeholder records
  remain in the registry so no existing record is discarded.
- `colonyship2`: `entry_host: ubuntu` and `active_host: AETHER-NAV` each already
  identified a registered host. Both references are preserved, and each receives
  its own coherent banner. The first shell displays the active host's banner.

Deployed profile storage is not rewritten by a package or image update. An old
local profile shadows its new bundled counterpart and will fail validation.
Operators must explicitly repair or replace each affected local/deployed profile
before restarting the updated service. See the banner checkpoints in
[Testing](TESTING.md#initial-login-banner-checkpoints) and
[decision 0121](decisions/0121-world-owned-initial-login-banners.md).

## Host Metadata

The starting system is not special beyond being referenced by `entry_host`.
It is stored in `hosts_by_name` alongside every other discovered or generated
host. Host metadata can describe:

- purpose and roleplay guidance;
- hardware identity or infrastructure bias;
- operating system hints;
- saved initial-login presentation (`login_banner`);
- default shell;
- users and privileges;
- operational age, lifecycle, activity, persistence, and maintenance culture;
- environment overrides;
- filesystem hints;
- installed tools;
- configured services;
- interfaces, listening ports, and network facts.

Host metadata is intentionally permissive. Missing sections are creative space
unless contradicted by established world state or command history. World
builders choose how detailed the entry host should be. If a host is first
known only by an IP address or another stable reference, that reference can be
used as its `hosts_by_name` key until better identity is established.

### Mandatory foreground host activity experiment

The command emulator's [ambient realism prompt](../src/dreamland/prompts/ambient_realism.md)
requires a coherent background population in the same response as the first
process, listener, network-connection, or active-user inspection on each host.
Examples include `ps`, `ss`, `netstat`, `lsof`, `who`, and `w`. The first such
inspection establishes linked processes, services, sockets, and user activity
without waiting for asynchronous enrichment or requiring pre-seeded rows.
Sparse service/listener declarations describe known entries unless explicitly
exhaustive; absent entries do not establish an otherwise empty host.

The experimental minimums are:

- Ordinary general-purpose servers have at least five background processes
  beyond the foreground login and inspection chain.
- Networked systems have at least two distinct additional non-SSH listening
  port numbers and at least two active background connections to remote
  systems, independent of the current DREAMLAND session. IPv4/IPv6 copies of
  one listening port do not count as separate ports.
- Multi-user systems have at least one other distinct user's active login
  connection. A service account or historical login alone does not satisfy
  this requirement.
- Running web servers have at least five simultaneously active HTTP(S) client
  connections at every observation throughout normal operation. Qualifying
  connections also count toward the general active-connection minimum; this
  does not require seven connections.

Every count scales upward with host role, expected users and concurrency,
activity/time context, and the scale of the surrounding environment. These are
floors, not preferred totals or caps. A large shared system should expose a
believable volume of processes, listeners, remote connections, and other users.
Command flags, filters, permissions, and output limits still determine which
rows are visible; a correctly filtered view can be empty.

The emulator records compact host-local `services_by_name`,
`users_by_username`, and `facts_by_id` anchors for ownership, PIDs, endpoints,
states, and lifetimes, then reuses them across inspections. Simulated background
connections remain ordinary world content, distinct from runtime-owned current
SSH sockets. Explicit operator exclusions and established complete observations
remain authoritative. Stopped services, disconnected users, and closed
connections are not resurrected to refill a quota. Sparse seeds, low generation
liberty, minimal inference, pristine operation, or little historical residue do
not by themselves excuse absent live activity. A constrained platform receives
activity appropriate to its actual role and capabilities.

This is an experimental prompt requirement, not a deterministic population
enforcer or TCP simulator. The user accepted the current experimental state on
2026-09-11 after reviewing a live replay with remaining continuity and population
failures; see [decision 0098](decisions/0098-mandatory-foreground-host-activity.md)
for the accepted checkpoint and validation limits.

The follow-up continuity prompt experiment requires host identity resolution
before population. Store known interface ownership under the canonical host's
`network.interfaces_by_name`, using keyed interface records with
`ipv4_address` or `ipv6_address` and known prefixes; store aliases in
`network.aliases`. Spiritguide must write these fields when choosing addresses,
before entry-connection initialization. A narrative `facts_by_id` attachment
alone is not consumed by the runtime's host-address resolver. The foreground
emulator records newly observed ownership through the same structured fields
and preserves established platform identity, service bindings, and SSH keys.
It cannot retroactively repair an already allocated runtime connection by
rewriting its endpoint.

Foreground prompts also distinguish the original DREAMLAND login, each host's
authenticated login owner, and the effective account after `sudo`/`su`. They
persist compact login observations and host clock/boot references, while the
runtime continues to own SSH socket endpoints and lifecycle. Repeated views
retain stable process/session identities and permit time-qualified transient
activity. Host-key observations persist even when authentication fails;
successful login observations require success. These rules are prompt guidance,
not new runtime identity or clock mechanisms. See
[decision 0100](decisions/0100-host-activity-continuity-rules.md) for evidence,
limitations, and further review checks.

### Background enrichment and identity continuity

Asynchronous ambient enrichment continues to add identities, historical work,
ordinary residue, logs, recurring automation, and connection context appropriate
to the scenario. Explicit profile or Spiritguide exclusions remain
authoritative. A clean or minimal environment can have little incidental
residue while still having the live activity required above.

The Ambient Coordinator's deterministic `presence-gap-v2` is not a state-
profile field or a replacement for scenario direction. It carries one
`independent_life` objective, an existing technical-surface preference, a
`named_human_or_person_equivalent_excluding_foreground_user_and_service_accounts`
subject preference, and bounded anti-repetition hints only after a session
starts. The intended causal cluster joins a recognizable named individual to
time-qualified personal, work, or social activity and connected observable
residue. In an ordinary human setting that individual is a human other than the
foreground user. A named person-equivalent with individual agency is permitted
only when the supplied scenario supports nonhuman people; a service, shared,
role, daemon, machine, automation, device, institution, or group identity does
not satisfy the preference. There is no `history-bearing_actor` state key or
count. Explicit profile facts and instructions always override ambient
planning, including solitude or other population limits.

The foreground-person exclusion follows established meaning rather than one
Unix username. When a profile explicitly associates a named person with the
entry interaction, privileged route, workstation, or active administrative
role, that person remains the foreground subject across active, login,
effective, privileged, `su`, `sudo`, and nested-SSH account transitions. No new
profile key is required for this rule; use existing host, user, purpose, and
world facts to state the association clearly. Likewise, a directly observed
login, session, process, or socket row retains its identity, endpoint, and
ownership associations. Ambient enrichment may add separate, explicitly time-
qualified historical or later activity, but cannot assign the observed row to
a new person unless the row itself identifies them. The separate event also
cannot be grounded by copying a username, PID, TTY, endpoint, port, source,
time, or other value learned only from the protected row. A profile fact,
materialized state record, or selected non-row reference must independently
establish any shared infrastructure or association used for the proposed
person's event.
World builders who intend two people to use the same host, route, endpoint, or
account should state that relationship in profile data rather than relying on
ambient enrichment to infer it from one foreground observation.

Likewise, Enrichment context v4's `host_time` is a direct observation derived
from a strictly recognized successful foreground `date` or `timedatectl`
command, not an implicit interpretation of arbitrary profile prose. World
builders that require exact chronology should still establish it explicitly in
state; future world-clock design must define how that state becomes typed
runtime time authority.

## Mutable World State

Model responses can return `state_update.world_state`. DREAMLAND deep-merges
that patch into session world state so generated facts persist for later
commands. Mutable collections should be keyed objects such as:

- top-level `hosts_by_name`;
- top-level `expansion_directives_by_id`;
- top-level `world_facts_by_id`;
- `hosts_by_name.<host>.services_by_name`;
- `hosts_by_name.<host>.filesystem.artifacts_by_path`;
- `hosts_by_name.<host>.users_by_username`;
- `hosts_by_name.<host>.groups_by_name`;
- `hosts_by_name.<host>.facts_by_id`;
- `hosts_by_name.<host>.command_history`.

`generation_policy` and `expansion_directives_by_id` are high-level
just-in-time generation guidance. Both the foreground command emulator and the
session-local EnrichmentAgent consume that guidance. Enrichment resolves
`generation_policy.liberty` deterministically into a task-neutral `latitude`
instruction; it does not expose the internal policy object to its independent
Expert. `world_facts_by_id` holds cross-host or whole-world facts. Host-local
registries hold concrete services, files, facts, and exact command history for
the host where they apply.

For command-emulator calls, the active host's registry entry is presented in
full. Every inactive host key remains present, but its model-facing value is a
compact identity, purpose, operating-system, recorded IP/network, fact, and
service card. Inactive filesystem, users, groups, shell state, and command
history are omitted from that prompt view without being removed from runtime
state. Structured IPv4 and IPv6 references remain available on compact cards.
The projection can select more detailed hosts in future without changing this
state-profile schema.

Generation liberty levels are `strict`, `conservative`, `moderate`,
`expansive`, and `abundant`; `moderate` is the packaged default and the
EnrichmentAgent fallback for a missing or unrecognized value. Higher levels
authorize progressively richer representative choices while established facts
and operator constraints remain authoritative.

## Connected World Direction

Spiritguide's new-world and broad world-building prompts require external
network direction by default. A scenario's named site, organization, or initial
inventory is its starting region. Unless explicit constraints require a closed
world, Spiritguide establishes at least two external networks, their onward
relationships, and an eventual usable path to the simulated Internet or a
comparably vast network appropriate to the setting.

Use `expansion_directives_by_id` for continued expansion and just-in-time
discovery guidance. Use concise `world_facts_by_id` entries for stable network
identities, gateway or service targets, access paths, and the ordinary command
surfaces that should reveal them. Give connection facts `network-` keys and
`category: "network"`: world-level saving retains facts only when their keys
contain a recognized high-level word or their category, scope, or type matches
one. Untagged connection facts can otherwise disappear during profile saving.
These starting connection facts do not require precomputed host or artifact
inventories. The existing emulator
projection retains both top-level collections across host changes.

Network reachability and service or login permission remain distinct. Explicit
isolation, exclusions, established state, and generation restrictions take
precedence; a sensitive theme or sparse starting state alone does not establish
isolation. Liberty still defaults to `moderate` and controls materialization,
while the connection directives describe the surrounding world's shape.

Spiritguide supplies the initial direction through generated world state. The
emulator also has its own default in `world.md`, including for profiles without
Spiritguide expansion directives. When commands investigate external
connections, it should establish or reuse at least two external relationships
and continue revealing onward destinations beyond the seed. Commands reveal
concrete targets, paths, and access conditions through ordinary operational
evidence. Each significant network or host retains an owner, purpose, and
relationship to the surrounding environment. Alias and translation mappings
should keep observations consistent across DNS, routing, services, and host
interfaces. None of these defaults overrides established facts or explicit
isolation and generation constraints.

These are prompt policies; reliable discovery, traversal, and continuity still
require manual evaluation. Saved profiles acquire new seed facts only through
generation or deliberate updates. The revised emulator can apply its defaults
to further invention while preserving old facts. See
[decision 0088](decisions/0088-spiritguide-connected-world-direction.md) for the
initial Spiritguide checkpoint and
[decision 0090](decisions/0090-emulator-connected-network-continuity.md) for the
emulator checkpoint.

## Fictional Network Addressing

Spiritguide and the emulator share `network_addressing.md`, which defines the
address and domain policy for new IP-based worlds and new network allocations:

- Private networks receive distinct, non-overlapping, appropriately sized
  allocations from `10.0.0.0/8`, `172.16.0.0/12`, or `192.168.0.0/16`.
  Organizations subdivide their allocations into canonical site and segment
  subnets. Established or explicitly requested overlap retains its routing or
  NAT context.
- Fictional public IPv4 allocations draw from `100.64.0.0/10`,
  `192.88.99.0/24`, `192.0.0.0/24`, `198.18.0.0/15`, `198.51.100.0/24`,
  `203.0.113.0/24`, and `240.0.0.0/4`. The prompt asks for random parent-block
  selection before address selection, variation across independent
  organizations, collision checks, and stable reuse within an allocation.
  Exclude `192.0.0.9`, `192.0.0.10`, and `255.255.255.255`; use valid host
  addresses for the chosen subnet.
- Newly invented domains use `.bsides` with distinct organization namespaces
  and consistent host, DNS, URL, email, certificate, and configuration names.
  This is DREAMLAND's presentation convention; the policy does not claim that
  `.bsides` is an officially reserved TLD.

The public pool is an operator-approved collection of simulation stand-ins,
with different real-world special-purpose meanings. Inside the world, the
addresses can represent ordinary Internet-facing services. The two excluded
`192.0.0.*` addresses are globally reachable anycast assignments; the excluded
`255.255.255.255` address is limited broadcast. See the
[IANA IPv4 special-purpose registry](https://www.iana.org/assignments/iana-ipv4-special-registry).

Spiritguide records allocation ownership, domain namespaces, and initial
gateway/service mappings in categorized `network-` world facts, using the same
save convention as connection facts. It also writes the complete pool,
exclusions, and continuing allocation rules into `expansion_directives_by_id`
for later emulator use. Addresses and names already established in a profile
remain authoritative unless the operator explicitly requests migration.

These are prompt instructions, with no runtime allocator or validation
enforcement. Prompted random choice does not guarantee a uniform distribution.
The emulator applies the shared policy to independent network creation even
without Spiritguide addressing directives, consulting and preserving existing
allocations and names. See
[decision 0089](decisions/0089-spiritguide-fictional-network-addressing.md) for
the policy rationale and
[decision 0090](decisions/0090-emulator-connected-network-continuity.md) for the
current manual evaluation checkpoint.

Spiritguide loads its prompt when the model instance is constructed. For a
detached MCP/API generation against an already running development service,
use `config_overrides: {"spiritguide": {}}` to construct a fresh instance with
the configured model settings and current prompt. The shared startup instance
retains its previously loaded prompt until the service is restarted.

The emulator also loads its prompt at model construction. Creating a fresh
MCP/API session with `config_overrides: {"agent": {}}` constructs an independent
runtime using the configured model settings and current emulator prompt. An
ordinary new session can reuse the shared startup agent, so a new session
alone does not necessarily load prompt edits. Existing sessions retain their
agent instance.

## Geographic Grounding

Spiritguide and the emulator share `geography.md`. For physical-world scenarios,
significant sites receive a real country, region, locality or nearby town, and
IANA timezone. Facilities and organizations may remain fictional. Explicit
operator geography, established locations, and deliberately fictional or
nonphysical settings take precedence over the default.

Use existing extensible state collections for the geographic convention:

- Store each significant site in `world_facts_by_id` under a stable
  `network-site-*` key, with `category: "network"`, `name`, `owner`, `role`,
  and a `location` object containing `country`, `region`, `locality`, and
  `timezone`. Optional context can describe placement near the named locality.
- Set a physical host's scalar `site_id` to the exact site fact key. Use
  `site_ids` in network facts to reference one or more established sites.
- Reuse the site for colocated systems. Distinguish headquarters, service
  territory, and hosting locations. A WAN or distributed service may span
  sites, and the Internet has no single physical site.

The categorized keys preserve site records through world-profile saving, while
top-level world facts and host `site_id` references survive inactive-host
projection. An arbitrary host-level `location` field alone would be omitted
from the compact card. This convention adds no runtime schema or geocoder.

Spiritguide includes the entry location in `purpose.description`, links the
entry host and significant seeded external endpoints to sites, and writes
continuing geographic direction in `expansion_directives_by_id`. The emulator
also applies the default independently when no site facts exist. As commands
expose new physical facilities, it should commit and reuse their locations.

Geography should appear through ordinary inventory, site codes, configuration,
maintenance/support records, and operational relationships. A site's civil
timezone is separate from configured host or log time; UTC remains valid.
Synthetic addresses, `.bsides` domains, and the real transport client's
metadata must not become geographic evidence. See
[decision 0091](decisions/0091-shared-geographic-grounding.md) for validation and
the prompt-first checkpoint.

## Prompt Modules

Default prompt loading composes these modules:

- `persona.md`
- `terminal_io.md`
- `state_contract.md`
- `environment_metadata.md`
- `world.md`
- `ambient_realism.md`
- `security.md`
- `environment.md`

`world.md` and Spiritguide both include `network_addressing.md` and
`geography.md`, keeping the addressing and geographic conventions aligned.

Prompt modules describe behavior and state contracts. State profiles describe
scenario seeds, host metadata, and generation policy.

The [command agent](../src/dreamland/agent_core.py) loads its system prompt at
initialization. Restart the running emulator or recreate its command agent to
review prompt-file edits, then start a fresh session. Reconnecting to an
existing agent or applying a state profile does not reload its prompt files.

## Live Profile Application

Live profile application preserves the connection's login identity, transport
metadata, and `dreamland.session.*` provenance, including MCP tags, labels,
custom metadata, and evaluation configuration. World/system profiles use that
login and expand username templates. Exact profiles restore the saved effective
shell user and history while retaining the connection login. MCP clients can
select `state_profile` during session creation without changing the process
default; see [MCP Model Evaluation Controls](MCP_MODEL_EVALUATION.md).

## Active Connection Persistence

Session SSH sockets are runtime-owned state, separate from authored services,
network relationships, and ambient connection facts. Ordinary world/system
saves preserve durable host addresses and services, not the saving visit's
active sockets. A fresh session or lobby visit receives fresh SSH connections
after its actual starting host is selected. The initial native connection is
represented at that host's primary interface on port 22, regardless of the real
listener's bind address or deployment port. Profiles can express primary
addresses through their existing network address/interface fields; an absent
address is materialized and retained by the runtime before it is used.

Exact runtime snapshots retain stable connection IDs, endpoints, source ports,
authentication phase, and shell-frame ownership so reconstruction between
product commands does not create fresh sockets. Applying an exact profile to
an existing live session reconciles saved nested connections with the current
entry connection. Legacy profiles without connection state remain loadable.
Offline profile loading and headless sessions do not acquire an SSH entry
connection unless one is explicitly supplied. Leaving a lobby world discards
that visit's connection registry.

The model sees a host-local projection through `environment_metadata`, while
the registry itself is not a writable model field. Keep historical login facts
and ambient services distinct from the current login and its active sockets.
Explicit source/destination translations use categorized world facts following
the [Network Connections schema](NETWORK_CONNECTIONS.md). These durable mappings
survive world saving; the sockets created from them remain session-owned.

See [Agent State](AGENT_STATE.md#host-local-active-ssh-connections) and
[decision 0094](decisions/0094-host-connection-continuity.md).

## Curated Native SSH Worlds

The optional [SSH lobby](SSH_LOBBY.md) publishes an explicit catalog of regular
saved `world` and compatible `system` files under `state_profiles/`. A packaged
fallback alone does not publish a world. The operator must author its public
title and description. `lobby worlds add` can resolve the required catalog
`entry_username` from the saved login identity, entry-host `default_user`, or
unambiguous structured entry-host account facts. An explicit `--entry-username`
overrides this resolution; missing or ambiguous accounts require that flag.
See [SSH Lobby](SSH_LOBBY.md#catalog-and-saved-worlds) for resolution order and
supported fact fields. Profile comments, source prompts, paths, and JSON never
become menu copy. Adding/removing an entry changes only catalog publication.

The bundled examples have an existing version-2
[starter catalog](../lobby/catalog.json): Morrowbasket Markets starts as
`operator`, Common Ground Colonyship as `reader`, and Muirbank Alpha as `leah`.
Follow the [fresh-checkout instructions](../lobby/README.md#fresh-checkout) to
copy missing package resources into local regular files without overwriting
existing profiles. Packaged availability alone does not satisfy lobby curation.
The [provenance record](../lobby/provenance.json) retains the historical
evaluation sources; those archives are not needed to initialize the examples.

The planned server installer will initially offer these three baselines through
both browser and SSH catalogs. Installer updates must preserve local profile
edits and curated catalog additions, ordering, and removals. The installer and
browser provisioning remain future work; the current package adds example
resources and leaves publication explicit. See
[decision 0103](decisions/0103-bundled-lobby-example-worlds.md).

Lobby curation rejects `exact` snapshots and documents that restore history,
shell stacks, or an active program. Prepare a fresh-login world/system profile
instead. Bundled schema-2 examples preserve evaluated world content with the
documented banner additions; they are not `exact` snapshots. A world profile
can still contain authored fictional files and host
history, so review its content and provenance before publication; the level does
not remove private data. Each selected profile is loaded and validated once into
a detached copy and seeds a new session with its catalog `entry_username`,
independently of the real SSH name. This initializes both current and login usernames,
username templates, and the starting shell through the existing boot rules.
Choose the account expected by the entry host and keep profile user records,
`USER`, `LOGNAME`, and home-directory facts consistent with that account.
The catalog validates the booted identity/environment; an absolute custom home
directory remains valid. This does not add a state-profile schema field or
change direct SSH, API, MCP, or offline profile-loading identity rules.
The selected profile does not replace the process default, and changing its
source file cannot change an existing visit.

Each fresh selection, including reentry on the same connection, applies its
configured starting account. Live profile application retains this simulated
login identity. Later simulated account changes follow the usual shell-stack
rules. The original supplied SSH name is escaped in lobby lifecycle audit logs
and correlated by parent connection; it is not copied into simulation state,
model context, or saved profile data. See the
[catalog and migration instructions](SSH_LOBBY.md#catalog-and-saved-worlds).

Public visit identity lives in trusted runtime provenance, separate from saved
profile data. New visits never inherit public authority from a profile; live
profile application retains the visit's existing identity. A new selection,
including the same world, gets a fresh identity and isolated state. Public
references do not change profile name or internal session ID semantics.

## Maintenance

Update this document when state-profile fields, prompt composition,
world-state or projection semantics, active-host behavior, bundled examples,
initialization, or prompt ownership rules change.
When adding scenario facts, place them in state profiles or mutable world
state, not in reusable behavior prompts.
