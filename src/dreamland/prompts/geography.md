## Geographic Grounding

For physical-world scenarios, place significant facilities, networks, and
systems in real geography even when the operator supplies no location. Choose
a real country, state/province or equivalent region, and city or nearby town
appropriate to the setting. A vague regional label alone is insufficient.
Organizations and facilities can be invented while their geographic setting is
real. Prefer confidently known places and coherent geographic relationships;
do not invent real-sounding cities or administrative regions. Broad placement
near a real locality is sufficient without a street address or coordinates.
Honor established geography and explicit operator choices, including fictional,
historical, off-world, or nonphysical settings, instead of forcing modern Earth
geography into an incompatible world.

Record each significant physical site in world_facts_by_id under a stable key
beginning network-site-, with category "network", a name, owner, role, and a
location object. For real-world sites, location contains country, region,
locality, and timezone using an appropriate IANA timezone identifier. Add
concise geographic context when it matters, such as placement outside a city
or the relationship to an operating territory. Keep fictional facility names
separate from real place names. Set hosts_by_name.<host>.site_id to the exact
site fact key and use site_ids in network facts to reference the relevant
sites. Referenced site records must exist. These categorized world facts and
scalar host references survive profile saving and inactive-host projection;
do not rely only on an arbitrary host location field or uncategorized site fact.

Reuse a known site when generating another colocated host, VLAN, or service.
When expansion establishes a separate facility, choose and persist its location
based on the organization's operations and relationships. Headquarters,
operating territory, physical hosting, and the users of a service are distinct
concepts. A WAN or distributed service can span several sites; represent that
scope and locate its concrete endpoints as they become relevant. The Internet
has no single physical site. A new SSH hop does not itself imply geographic
relocation. Geographic assignment must not change an established host's owner,
addresses, aliases, or network identity.

Use recorded sites as the source of geographic truth. Synthetic IP allocations,
.bsides names, and the real transport client's connection metadata are not
geolocation evidence. Preserve established locations on revisits and add detail
without silently moving systems. A site's civil timezone does not force every
host or log to use local time; UTC and other established clock policies remain
valid and should be distinguished from physical location.

Make geography observable through ordinary scenario descriptions, site codes,
inventory, configuration, maintenance or support records, operational
relationships, and time conventions when commands expose them. Keep regional
details mutually consistent and appropriate to the system's role. Generate
only evidence the active interface could normally reveal, without inserting
geographic narration into unrelated command output. Materialize site detail
just in time rather than precomputing maps or an exhaustive facility inventory.
