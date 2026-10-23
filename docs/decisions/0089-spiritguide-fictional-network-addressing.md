# 0089 Spiritguide Fictional Network Addressing

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators
Purpose: record the address and domain policy for fictional networks and the
Spiritguide-first implementation checkpoint.

## Overview

Manual exploration of the connected-world checkpoint found repeated public
addresses in `198.18.0.0/15` and invented domains ending in `.example`.
Independent organizations and onward networks consequently resembled one
expanding lab network. The Spiritguide prompt did not specify an address
allocation policy, and the initial seed gave later emulation a repetitive
pattern to continue. This is a plausible contributor to the observed behavior,
not a claim about the model's internal reasoning.

The operator requested distinct private ranges, a broader explicit pool for
fictional public addresses, and `.bsides` domains. The first change applies to
Spiritguide; the operator subsequently authorized independent emulator guidance
before the next human test, as recorded in
[decision 0090](0090-emulator-connected-network-continuity.md).
This extends [decision 0088](0088-spiritguide-connected-world-direction.md)
and its prompt work in commit `59956ec`.

## Decision

Use the policy documented in
[State Profiles](../STATE_PROFILES.md#fictional-network-addressing): private
allocations come from RFC 1918 space, fictional public allocations come from
the seven operator-approved blocks, and invented domains end in `.bsides`.
Choose an eligible public parent block before selecting an allocation so the
largest range does not dominate by size. Encourage variation between unrelated
organizations while preserving coherent allocations within each organization.
The model chooses addresses; this phase adds no deterministic random sampler,
collision validator, address translation layer, or state schema.

Retain every requested candidate block. Exclude the globally reachable anycast
assignments `192.0.0.9` and `192.0.0.10` within `192.0.0.0/24` to honor the
operator's intent to avoid presenting real routable service addresses. Exclude
limited broadcast `255.255.255.255` within `240.0.0.0/4` and use valid host
addresses for the chosen subnet. These exceptions follow the
[IANA IPv4 special-purpose registry](https://www.iana.org/assignments/iana-ipv4-special-registry),
consulted on 2026-09-06. The pool includes several kinds of special-purpose
space; this decision does not assert identical real-world routing properties
for every block. Within DREAMLAND they serve as fictional public addresses.
`.bsides` is the chosen presentation namespace, without a claim of official
TLD reservation.

Make allocation ownership explicit in concise `world_facts_by_id` records,
along with organization namespaces and initial gateway/service mappings.
Use `network-` keys and `category: "network"` so world-profile saving retains
these commitments. Store the full public pool, exclusions, private allocation
rules, domain policy, and continuity requirements in
`expansion_directives_by_id`. The existing emulator can consume this generated
direction during continued exploration without a change to its own prompt.

Preserve established addresses, names, and ownership unless an operator
explicitly requests migration. Saved profiles, live sessions, and the archived
exact prompts from earlier evaluations are not automatically rewritten.
Independent emulator defaults now share the addressing policy through
`network_addressing.md`, as recorded in decision 0090. Stronger runtime
enforcement remains conditional on manual findings in the
[roadmap](../../ROADMAP.md#spiritguide-phase-carry-over).

## Validation

Run the existing prompt-loading, Spiritguide, and state-profile tests to check
integration. These checks do not demonstrate model compliance or randomness.
Use a fresh detached world for the manual checkpoint:

1. Generate from an ordinary scenario request with a fresh Spiritguide instance
   so it loads the revised prompt. Detached MCP/API calls can supply
   `config_overrides: {"spiritguide": {}}` while preserving configured model
   settings. Capture the exact request,
   composed system prompt, model settings, and accepted saved profile so the
   generation can be inspected and reused.
2. Inspect entry and external networks for distinct private allocations,
   canonical subnets, public addresses within the approved pool and outside
   exclusions, distinct organization namespaces under `.bsides`, and coherent
   owners and gateway/service mappings. Check that saving retains both the
   allocation facts and full continuing policy.
3. Explore beyond the seed, then revisit earlier systems and save/reload.
   Check that visible DNS, routes, service references, and identities agree
   with the allocations. Separate Spiritguide seed quality from failures in
   later independent emulation.
4. Compare several fresh worlds for public-block diversity before claiming
   that the repeated-range problem is resolved. Prompt-only variety and
   collision avoidance remain subject to model compliance.

The implementation checkpoint passed all 26 tests in `tests.test_prompts`,
`tests.test_spiritguide`, and `tests.test_state_profiles`, plus
`git diff --check`. Fresh model generation and the manual checks above remain
pending; the unit checks do not establish behavioral improvement.

## Maintenance

Update this decision, `docs/STATE_PROFILES.md`, and the roadmap when manual
feedback changes the policy, independent emulator support is implemented, or a
runtime allocator is introduced. Recheck registry exceptions if the approved
pool changes. Reusable addressing rules live in
`src/dreamland/prompts/network_addressing.md`, with Spiritguide's world-authoring
instructions in `spiritguide.md`; individual organizations and allocations
belong in generated world state.
