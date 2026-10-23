# 0088 Spiritguide Connected World Direction

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators
Purpose: record the first prompt-only attempt to seed external networks and
continued world expansion for manual evaluation.

## Overview

The emulator already encourages building the world up and out, but that general
permission does not require concrete external destinations or onward network
relationships. The inspected local powerplant profile used `abundant` liberty
while concentrating topology direction on the plant and supporting business
network. Raising generation liberty alone would not state the missing rule.

## Decision

Start with Spiritguide. New worlds and broad world-building or deepening
requests treat the requested setting as the starting region of a larger world.
Unless explicit operator constraints require closure, establish at least two
external networks, onward relationships from each, and an eventual usable path
to the simulated Internet or a comparably vast network suitable for the setting.
The larger network remains explorable beyond the initial seed.

Use existing `expansion_directives_by_id` for recurring expansion and discovery
guidance. Use a few `world_facts_by_id` entries for stable connection identities,
gateway or service targets, access paths, and the command-visible evidence that
will reveal them. These facts provide concrete starting destinations without
precomputing ordinary host, route, or artifact inventories. Both collections
already survive the emulator's active-host projection.

Connection facts must have a `network-` key and `category: "network"`. The
first generated model response contained concrete connection facts, but the
world-profile saver dropped them because their unclassified keys did not match
its high-level fact filter. This prompt requirement uses the existing save
contract; it does not change runtime projection behavior.

Keep `moderate` as the generation default. Explicit isolation, established
facts, exclusions, and generation restrictions remain authoritative. Network
reachability does not imply permission to log in, but a sensitive setting or
sparse inventory alone must not create a closed world. Favor multiple usable
avenues and few network-level dead ends while allowing individual host endpoints.

The authorized checkpoint changes Spiritguide and its documentation. Emulator
prompt changes follow operator feedback on a freshly generated world. Runtime
topology enforcement, new state schemas, enrichment changes, and automatic
migration of existing profiles are deferred.

The subsequent authorized emulator prompt checkpoint is recorded in
[decision 0090](0090-emulator-connected-network-continuity.md). It extends this
direction to independent emulator generation before the next human test.

## Validation

Run the existing prompt, Spiritguide, and state-profile checks. Generate a new
detached world from an ordinary scenario request without asking for external
networks in that request. Record the generation instruction, loaded prompt
identity, model settings, and saved profile identity with the manual checkpoint.
Inspect the accepted profile for entry-host consistency, concrete external
connections, onward relationships, discovery evidence, and continued expansion
direction. Keep the generated profile as local operator state.

The operator then checks whether ordinary commands reveal and use those
connections, whether further networks continue beyond the initial seed, and
whether revisiting destinations preserves state. Generation success alone does
not establish emulator traversal quality or reliability across model calls.

## Maintenance

Update this decision and `docs/STATE_PROFILES.md` when manual feedback changes
the rule, emulator support is added, or runtime enforcement becomes necessary.
The reusable behavior lives in `src/dreamland/prompts/spiritguide.md`; concrete
scenario facts belong in generated world state.

