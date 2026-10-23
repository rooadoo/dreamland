# 0090 Emulator Connected Network Continuity

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators
Purpose: record the prompt-only emulator attempt to sustain external depth,
recognizable organizations, and coherent fictional addressing beyond the seed.

## Overview

Spiritguide's connected-world seed improved the initial exploration, but manual
feedback described later systems as increasingly generic and difficult to place
organizationally. Repeated public address ranges reinforced that impression.
The emulator's existing permission to build outward did not explicitly require
concrete onward relationships, independent organizational context, or an
allocation policy for newly invented networks.

The operator approved emulator prompt changes before the next human test.
The prior Spiritguide checkpoint is committed as `923aa95`, following the
connected-world seed work in `59956ec`. This checkpoint implements the emulator
follow-ups from [decision 0088](0088-spiritguide-connected-world-direction.md)
and [decision 0089](0089-spiritguide-fictional-network-addressing.md).

## Decision

Extend `src/dreamland/prompts/world.md` with explicit defaults for continued
network expansion and organizational continuity. They apply when Spiritguide
provided no external destinations as well as when exploration exceeds its seed.
When commands investigate external connections, establish or reuse at least
two distinct external relationships, then carry onward relationships into each
new network. An eventual usable path reaches the simulated Internet or another
vast setting-appropriate network and remains explorable beyond that point.

Discovery commits concrete addresses or resolvable names, access paths, and
service relationships through ordinary command-visible evidence. Bound each
response to the observed surface and immediate next connections. Explicit
isolation, established facts, generation restrictions, and authentication and
routing constraints retain precedence. An individual endpoint can remain an
endpoint, and reachability need not imply shell access.

Give significant networks and hosts persistent ownership, operational purpose,
and relationships to their neighbors. Let those identities shape relevant
technology, personnel, maintenance, and artifacts. Preserve host aliases and
record NAT, proxy, virtual-address, or multihoming relationships when needed to
explain differing observations. Use categorized `network-` world facts for
cross-host commitments and the existing host registries for host-local detail.
Respect the state contract's compact SSH transition priority.

Extract the already approved address/domain rules into the shared prompt module
`network_addressing.md`, included by both `world.md` and `spiritguide.md`.
The private ranges, seven public candidate blocks, individual exclusions,
`.bsides` namespace policy, and continuity requirements have one source.
Spiritguide's composed prompt remains byte-for-byte identical to `923aa95`;
only the emulator receives additional behavior. No scenario-specific names or
allocations enter the reusable prompts.

This change adds no runtime topology engine, allocator, collision validator,
profile migration, or new state fields. Prompt compliance, allocation variety,
and exploration quality require manual observation. Existing world facts remain
authoritative; new prompt defaults guide further invention.

## Validation

Run the focused prompt, Spiritguide, state-profile, world-state, and model-agent
tests, including projection across host pivots. Check the fully composed
Spiritguide prompt against the previous checkpoint to verify the shared-module
extraction did not alter it. Inspect the diff for accidental scenario facts or
changes to the shell transition contract.

Use this manual checkpoint before judging the prompt attempt:

1. Generate and save a fresh world with the revised Spiritguide prompt. Capture
   the exact instruction, composed prompts, model settings, profile identity,
   and initial destination inventory so later generation beyond the seed can
   be distinguished from traversal within it. Use a fresh emulator instance
   for the session; see [prompt loading](../STATE_PROFILES.md#fictional-network-addressing).
2. Follow ordinary route, DNS, configuration, and service evidence through at
   least two organizations beyond the seed. At each boundary, check for onward
   destinations and a recognizable owner, purpose, and connection relationship.
   Reach the larger external network and continue. Judge the actual command
   output rather than a model's description of what it would generate.
3. Compare allocations and names against the initial inventory; revisit a host
   by both name and address, inspect its local identity, and save/reload the
   world. Check stable mappings and ownership, explanations for translated
   addresses, and retained facts. Compare several fresh generations before
   claiming reliable address diversity.
4. Separately exercise an older profile without Spiritguide expansion
   directives and a world with explicit isolation or restrictive generation
   direction. Check that independent defaults fill creative space while
   preserving established addresses, domains, access conditions, and boundaries.

Both agents load prompts when constructed. Detached Spiritguide overrides and
fresh per-session emulator overrides can create new instances without replacing
an existing session. An ordinary session on a long-running service may still
reuse its old shared agent. No live session is automatically rethemed.

## Maintenance

Update this decision, `docs/AGENT_STATE.md`, `docs/STATE_PROFILES.md`, and the
roadmap after the manual checkpoint. If the prompts still lose topology or
identities, use those concrete failures to scope runtime support. Keep shared
addressing rules in `network_addressing.md`, emulator expansion in `world.md`,
and concrete organizational facts in world state.

