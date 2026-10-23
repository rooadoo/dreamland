# Simulated Network Connections

Status: Active
Audience: maintainers, state-profile authors, DREAMLAND operators
Purpose: document host endpoint identity and explicit SSH translation facts.

## Overview

Runtime connection state represents the sockets participating in the current
simulated session. The initial SSH connection terminates at the selected entry
host's primary interface on port 22. A nested SSH connection has its own host
identities, endpoint addresses, service port, source port, and frame lifetime.
Physical listener addresses and ports remain transport provenance.

The runtime resolves established host keys, names, aliases, interface addresses,
and exact network-fact `targets` or `service_mappings` entries. Missing addresses
are established once in session world state. Existing primary addresses remain
primary when another address family or a secondary endpoint is observed.

## Details

### Explicit endpoint translation

Source and destination translation can give each host a different view of one
connection. For example, a client may connect to a public gateway while the
server receives that connection on a private address. The server may also see
an explicitly recorded translated client address. These observations do not
change the client host's interface address or assign the gateway's public
address to the private server.

Record a bounded mapping in a `world_state.world_facts_by_id` entry with
`category: "network"` and a `connection_translation` object:

```json
{
  "network-visitor-ssh": {
    "category": "network",
    "targets": {
      "visitor.harbor.bsides": "198.51.100.166"
    },
    "connection_translation": {
      "source_host": "workstation",
      "target": "visitor.harbor.bsides",
      "target_port": 2222,
      "destination_host": "visitor-server",
      "destination": {"ip": "10.184.0.8", "port": 22},
      "source": {"ip": "203.0.113.66"}
    }
  }
}
```

Here the client's peer is `198.51.100.166:2222`. The server's local endpoint is
`10.184.0.8:22`, and its peer address is `203.0.113.66`. The translated source
port is the original source port unless `source.port` explicitly supplies a
different port. SSH login environment variables describe the server's view.

The schema has these rules:

- `target` and `destination_host` are required names or addresses. Target names
  compare without case or a trailing DNS dot. Exact known DNS-to-IP mappings
  and host aliases may identify the same target.
- `source_host` optionally limits the rule to one canonical host or known alias.
  Without it, any source can match the explicit target.
- `target_port` optionally limits the matching SSH service port. Ports must be
  JSON integers from 1 through 65535.
- At least one of `source` or `destination` must describe a translation.
  Each endpoint requires a literal `ip`; `port` is optional. Omitting an
  endpoint port preserves the corresponding unmodified connection port.
- `source` describes the client endpoint as observed by the destination server.
  `destination` describes the server-local endpoint after destination
  translation. They never describe additional TCP sessions through a proxy.
- An explicit destination address must already belong to `destination_host`,
  or supply an address for a host with no established address. A mapping cannot
  take another host's address or renumber an established host.
- Destination translation requires an established public target address so
  both host observations are concrete. An unmapped DNS name does not supply
  that address. Source-only translation may use the established server address.
- Matching rules must be unambiguous. Invalid endpoints, incompatible address
  families, and multiple matching rules produce no translation selection.

The resolver reads facts without mutating world state or performing DNS. It
returns the canonical destination host, normalized endpoints, the original
numeric `target_address` when known, and the fact ID. Connection creation owns
the eventual address materialization and snapshots the selected endpoint views
for the connection lifetime.

This is a bounded mapping contract. Narrative routing descriptions and generic
egress labels do not define a complete translation rule. Automatic NAT policy
discovery, proxy chains, and cross-family NAT64 inference are outside this
contract.

## Maintenance

Update this document alongside endpoint or translation schema changes in
[network_identity.py](../src/dreamland/network_identity.py),
[connection_translation.py](../src/dreamland/connection_translation.py), and
[network_connections.py](../src/dreamland/network_connections.py). Keep examples
consistent with [State Profiles](STATE_PROFILES.md) and the fictional addressing
policy. Related runtime ownership decisions are recorded in
[decision 0094](decisions/0094-host-connection-continuity.md).
