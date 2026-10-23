## Fictional Network Addressing

Apply this policy when creating or extending IP-based worlds. Network addresses
and domains should make separate organizations recognizable as exploration
continues. Preserve established addresses, names, and ownership unless the
operator explicitly asks to renumber or rename them; apply this policy to new
allocations without silently migrating existing systems.

For company and other private networks, choose appropriately sized subnets from
10.0.0.0/8, 172.16.0.0/12, or 192.168.0.0/16. Give independently administered
networks distinct, non-overlapping allocations within the world. Subdivide each
organization's allocation coherently for its sites and routed segments. Use
canonical CIDR network boundaries, keep child subnets inside their allocation,
and avoid reusing an occupied subnet or assigning an entire private pool to a
small organization. Preserve overlap only when established facts or explicit
operator direction require it, with the relevant routing or NAT context.

When a fictitious entity needs a public-facing IPv4 address, randomly choose an
eligible parent block from this complete approved simulation pool, then choose
an unused, appropriately sized allocation and usable host addresses within it:

- 100.64.0.0/10
- 192.88.99.0/24
- 192.0.0.0/24
- 198.18.0.0/15
- 198.51.100.0/24
- 203.0.113.0/24
- 240.0.0.0/4

Choose the parent block before the address so the largest block does not
dominate selection. Spread unrelated organizations' public allocations across
different eligible blocks where space permits; do not default every external
network to 198.18.0.0/15 or always start with the first listed block. Keep each
organization's addresses coherent within its chosen allocation instead of
randomizing every host independently. Check recorded allocations for overlap
before choosing a new one, and keep the choice stable once committed. Exclude
192.0.0.9 and 192.0.0.10, which identify globally reachable real services, and
255.255.255.255, the limited broadcast address. Use valid host addresses for the
chosen subnet. Do not invent public IPv4 addresses outside the approved pool
or borrow real public DNS, cloud, or vendor service addresses as shortcuts.

These public addresses are stand-ins within the simulated world. Give them
ordinary Internet-facing roles, routing, and services consistent with the
scenario. Their real-world special-purpose classifications alone do not imply
a lab setting or make a simulated route unusable.

All newly invented DNS domains must end in .bsides. Choose distinct domain
stems for independent organizations and coherent site or service subdomains
within each organization. Apply the same names consistently to DNS, FQDNs,
URLs, email addresses, certificates, configuration, and service references.
Do not use .example for new invented domains or borrow real organizations'
domains. Ordinary short hostnames can remain unqualified.
