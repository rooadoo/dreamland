# 0091 Shared Geographic Grounding

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators
Purpose: record the prompt-first geographic convention for fictional facilities,
networks, and systems in real places.

## Overview

The connected-world checkpoint gave organizations names, purposes, address
allocations, and onward relationships, but the saved powerplant still had no
real geographic setting. The operator described these worlds as insufficiently
grounded and approved shared Spiritguide/emulator guidance plus regeneration
from the same scenario instruction.

## Decision

Add `geography.md`, shared by Spiritguide and the emulator. Physical-world
scenarios should locate significant sites using a real country, region,
locality or nearby town, and civil timezone. A facility's identity remains
fictional while its geographic setting is real. Broad placement is sufficient;
street addresses, coordinates, maps, or a new geographic lookup service are
not required. Explicit operator geography and incompatible fictional,
historical, off-world, or nonphysical settings remain authoritative.

Store canonical site objects under `world_facts_by_id` with `network-site-*`
keys, `category: "network"`, and a location object. Associate physical hosts
through scalar `site_id` references to those exact keys and networks through
`site_ids`. These are conventions within existing extensible state, not new
runtime fields requiring schema changes.

This storage choice matters: the world-profile saver filters unrecognized
high-level facts, and inactive-host projection omits arbitrary host-level
`location` fields. Categorized network facts survive saving; top-level world
facts and scalar host `site_id` already survive projection. The regression
check exercises disk save/load and projection with each host active, ensuring
that geography survives while inactive filesystem details remain omitted.

Spiritguide states the starting location in the world purpose, supplies the
entry site and significant external endpoint sites, and records continuing
geographic direction. The emulator independently assigns or inherits sites
as new physical facilities become observable beyond the seed. Colocated hosts
reuse the site. Headquarters, operating territory, and service hosting remain
distinct; distributed services can span sites, and the Internet has no single
location. Synthetic IPs, invented domains, and real transport metadata are not
sources of geographic truth.

Ambient-realism guidance makes site geography apparent through appropriate
inventory, configuration, site identifiers, maintenance/support records, and
operational relationships. The site's civil timezone does not force every
host or log to use local time. Established clock conventions and command-native
output remain authoritative. Concrete places belong in profiles, not reusable
prompts.

## Validation

Run focused prompt, Spiritguide, state-profile, emulator-context, world-state,
and model-agent tests. The persistence regression verifies the geographic
record/reference convention; it does not demonstrate model compliance.

The implementation checkpoint passed all 100 tests in `tests.test_prompts`,
`tests.test_spiritguide`, `tests.test_state_profiles`,
`tests.test_emulator_context`, `tests.test_world_state`, and
`tests.test_backend.LangGraphCommandAgentTests`. Ruff passed for the changed
test file, and `git diff --check` passed.

Generate a fresh world using an operator-defined premise and resolved model
settings. Save a separate profile and capture the request,
composed prompts, actual model input, visible response, and trace identifiers.
Inspect the accepted saved state for site ownership, real place names,
appropriate timezones, valid references, geographic expansion direction, and
preserved addressing policy. Verify the chosen geography against sources and
check that site records survive saving and host projection.

The subsequent human checkpoint should discover geography through normal
commands, follow an external relationship to a new site, revisit an earlier
host, and save/reload. Record whether sites remain stable and whether generated
records and time conventions agree. Explicitly fictional settings and
distributed services need separate coverage before claiming general reliability.

## Maintenance

Update this decision and `docs/STATE_PROFILES.md` when geographic conventions or
projection change. Keep human findings with the generation evidence. Use
concrete failures to decide whether geographic validation or stronger runtime
support is necessary after the prompt-first checkpoint.
