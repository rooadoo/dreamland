"""Resolve and establish simulated host endpoints without consulting real networking."""

from __future__ import annotations

import hashlib
import ipaddress
from collections.abc import Iterator
from dataclasses import dataclass

_Address = ipaddress.IPv4Address | ipaddress.IPv6Address
_Network = ipaddress.IPv4Network | ipaddress.IPv6Network
_ADDRESS_KEYS = (
    "primary_address",
    "ipv4_address",
    "ipv6_address",
    "ip_address",
    "address",
    "ip",
    "addresses",
    "ip_addresses",
    "ipv4_addresses",
    "ipv6_addresses",
    "ipv4",
    "ipv6",
    "secondary_addresses",
)
_PUBLIC_POOLS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "100.64.0.0/10",
        "192.88.99.0/24",
        "192.0.0.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "240.0.0.0/4",
    )
)
_EXCLUDED_NEW_ADDRESSES = {"192.0.0.9", "192.0.0.10", "255.255.255.255"}


@dataclass(frozen=True)
class _HostAddress:
    address: _Address
    network: _Network | None = None
    interface: str = ""
    priority: int = 3


def resolve_host_ref(world_state: dict[str, object], name: str) -> str | None:
    """Find a canonical registry key by host name, alias, address or target fact."""
    cleaned = _clean_name(name)
    hosts = _hosts(world_state)
    if cleaned in {"localhost", "localhost.localdomain"} or (
        (address := _ip(cleaned)) is not None and address.is_loopback
    ):
        active = world_state.get("active_host") or world_state.get("entry_host")
        if isinstance(active, str) and active in hosts:
            return active
    address = _ip(cleaned)
    if address is not None:
        for ref, host in hosts.items():
            if isinstance(host, dict) and any(
                item.address == address for item in _host_addresses(host)
            ):
                return str(ref)
    placeholder = None
    for ref, host in hosts.items():
        if not isinstance(ref, str) or not isinstance(host, dict):
            continue
        if cleaned == _clean_name(ref) or cleaned in _host_names(host):
            if host == {"hostname": ref}:
                placeholder = ref
            else:
                return ref
    mapped = _target_mapping(world_state, cleaned)
    if mapped is not None:
        mapped_address = _mapped_address(mapped)
        mapped_name = _mapped_name(mapped)
        for ref, host in hosts.items():
            if not isinstance(host, dict):
                continue
            if mapped_name and (
                _clean_name(ref) == mapped_name or mapped_name in _host_names(host)
            ):
                return str(ref)
            if mapped_address is not None and any(
                item.address == mapped_address for item in _host_addresses(host)
            ):
                return str(ref)
    return placeholder


def ensure_host_address(
    world_state: dict[str, object],
    host_ref: str,
    *,
    preferred_address: str | None = None,
) -> str:
    """Return a stable primary address, establishing one only when absent.

    Existing addresses win over a preference. The preference represents an
    already observed simulated endpoint, not an instruction to renumber a host.
    """
    ref = resolve_host_ref(world_state, host_ref) or host_ref
    host = _ensure_host(world_state, ref)
    candidates = _host_addresses(host)
    if candidates:
        return str(candidates[0].address)
    preferred = _usable_ip(preferred_address)
    if preferred is not None:
        owner = resolve_host_ref(world_state, str(preferred))
        if owner is not None and owner != ref:
            raise ValueError("preferred address belongs to another simulated host")
    address = preferred or _allocate_address(world_state, ref, host, version=4)
    _record_address(host, address)
    return str(address)


def known_host_addresses(
    world_state: dict[str, object], host_ref: str
) -> tuple[str, ...]:
    """Return established addresses in primary order without materialization."""
    ref = resolve_host_ref(world_state, host_ref) or host_ref
    host = _hosts(world_state).get(ref)
    if not isinstance(host, dict):
        return ()
    return tuple(dict.fromkeys(str(item.address) for item in _host_addresses(host)))


def resolve_target(
    world_state: dict[str, object],
    target: str,
    *,
    hostname: str | None = None,
) -> tuple[str, str]:
    """Resolve an observed SSH target and remote hostname to a host and endpoint."""
    cleaned = _clean_name(target)
    if not cleaned:
        raise ValueError("simulated network target must not be empty")
    mapping = _target_mapping(world_state, cleaned)
    endpoint = _ip(cleaned) or _mapped_address(mapping)
    if cleaned in {"localhost", "localhost.localdomain"}:
        endpoint = ipaddress.ip_address("127.0.0.1")
    ref = resolve_host_ref(world_state, cleaned)
    if ref is None and hostname:
        ref = resolve_host_ref(world_state, hostname)
    if ref is None:
        ref = _mapped_name(mapping) or (
            hostname.strip() if hostname else target.strip()
        )
    host = _ensure_host(world_state, ref)
    aliases = list(_strings(host.get("aliases")))
    for alias in (target, hostname):
        if alias and _clean_name(alias) != _clean_name(ref) and alias not in aliases:
            owner = resolve_host_ref(world_state, alias)
            if (
                owner is not None
                and owner != ref
                and _hosts(world_state).get(owner) != {"hostname": owner}
            ):
                continue
            aliases.append(alias)
    if aliases:
        host["aliases"] = aliases
    for alias in (target, hostname):
        if not alias or alias == ref:
            continue
        hosts = _hosts(world_state)
        if hosts.get(alias) == {"hostname": alias}:
            # Shell synchronization may create an empty record before the SSH
            # target is resolved. Reuse its proven endpoint identity; populated
            # host records retain their independently established facts.
            del hosts[alias]
            if world_state.get("active_host") == alias:
                world_state["active_host"] = ref
    if endpoint is not None and endpoint.is_loopback:
        address = str(endpoint)
    else:
        primary = ensure_host_address(
            world_state,
            ref,
            preferred_address=str(endpoint) if endpoint is not None else None,
        )
        address = str(endpoint) if endpoint is not None else primary
        if endpoint is not None and endpoint not in {
            item.address for item in _host_addresses(host)
        }:
            owner = resolve_host_ref(world_state, address)
            if owner is not None and owner != ref:
                raise ValueError("target address belongs to another simulated host")
            _record_address(host, endpoint)
    return ref, address


def source_address_for_target(
    world_state: dict[str, object],
    source_ref: str,
    destination_address: str,
) -> str:
    """Use the longest matching route/interface, falling back to a family primary."""
    destination = _ip(destination_address)
    if destination is None:
        raise ValueError("destination must be a simulated IP address")
    if destination.is_loopback:
        return "127.0.0.1" if destination.version == 4 else "::1"
    ref = resolve_host_ref(world_state, source_ref) or source_ref
    host = _ensure_host(world_state, ref)
    candidates = [
        item
        for item in _host_addresses(host)
        if item.address.version == destination.version
    ]
    directly_connected = [
        item
        for item in candidates
        if item.network is not None and destination in item.network
    ]
    direct = max(
        directly_connected, key=lambda item: item.network.prefixlen, default=None
    )
    routes = []
    for route in _routes(host):
        network = _route_network(route, destination.version)
        if network is not None and destination in network:
            routes.append((network.prefixlen, route))
    for prefixlen, route in sorted(routes, key=lambda item: item[0], reverse=True):
        if direct is not None and direct.network.prefixlen > prefixlen:
            return str(direct.address)
        source = _usable_ip(
            route.get("src") or route.get("source") or route.get("preferred_source")
        )
        if source is not None and source.version == destination.version:
            return str(source)
        interface = str(route.get("dev") or route.get("interface") or "")
        for candidate in candidates:
            if interface and candidate.interface == interface:
                return str(candidate.address)
    if direct is not None:
        return str(direct.address)
    if candidates:
        return str(candidates[0].address)
    address = _allocate_address(world_state, ref, host, version=destination.version)
    _record_address(host, address)
    return str(address)


def _hosts(world: dict[str, object]) -> dict[str, object]:
    hosts = world.get("hosts_by_name")
    return hosts if isinstance(hosts, dict) else {}


def _ensure_host(world: dict[str, object], ref: str) -> dict[str, object]:
    hosts = world.setdefault("hosts_by_name", {})
    if not isinstance(hosts, dict):
        raise ValueError("hosts_by_name must be an object")
    host = hosts.setdefault(ref, {"hostname": ref})
    if not isinstance(host, dict):
        raise ValueError("simulated host metadata must be an object")
    return host


def _clean_name(name: object) -> str:
    return str(name or "").strip().strip("[]").rstrip(".").lower()


def _ip(value: object) -> _Address | None:
    if not isinstance(value, str):
        return None
    try:
        return ipaddress.ip_interface(value.strip().strip("[]")).ip
    except ValueError:
        return None


def _usable_ip(value: object) -> _Address | None:
    address = _ip(value)
    if (
        address is None
        or address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or str(address) == "255.255.255.255"
    ):
        return None
    return address


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        yield from (str(key) for key in value)


def _host_names(host: dict[str, object]) -> set[str]:
    names = set()
    for container in (host, _network(host)):
        for key in ("hostname", "fqdn", "aliases", "hostnames"):
            names.update(_clean_name(value) for value in _strings(container.get(key)))
    return names


def _network(host: dict[str, object]) -> dict[str, object]:
    value = host.get("network") or host.get("networking")
    return value if isinstance(value, dict) else {}


def _records(value: object) -> Iterator[tuple[str, dict[str, object]]]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield (
                    str(
                        item.get("name")
                        or item.get("interface")
                        or item.get("dev")
                        or ""
                    ),
                    item,
                )
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict):
                yield str(item.get("name") or key), item


def _address_values(value: object) -> Iterator[object]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _address_values(item)
    elif isinstance(value, dict):
        for key in _ADDRESS_KEYS:
            yield from _address_values(value.get(key))


def _host_addresses(host: dict[str, object]) -> list[_HostAddress]:
    network = _network(host)
    primary_interface = str(network.get("primary_interface") or "")
    default_interfaces = {
        str(route.get("dev") or route.get("interface") or "")
        for route in _routes(host)
        if str(route.get("destination") or route.get("dst") or route.get("to"))
        in {"default", "0.0.0.0/0", "::/0"}
    }
    containers = [("", host, 2), ("", network, 2)]
    for container in (host, network):
        for key in ("interfaces", "interfaces_by_name", "network_interfaces"):
            for name, interface in _records(container.get(key)):
                priority = 3
                if interface.get("primary") is True or name == primary_interface:
                    priority = 0
                elif name and name in default_interfaces:
                    priority = 1
                containers.append((name, interface, priority))
    candidates = []
    for name, container, priority in containers:
        for key in _ADDRESS_KEYS:
            for value in _address_values(container.get(key)):
                address = _usable_ip(value)
                if address is None:
                    continue
                subnet = None
                try:
                    prefix = (
                        container.get("prefix_length")
                        or container.get("prefixlen")
                        or container.get("netmask")
                    )
                    if "/" in str(value):
                        subnet = ipaddress.ip_interface(str(value)).network
                    elif prefix is not None:
                        subnet = ipaddress.ip_interface(f"{address}/{prefix}").network
                except ValueError:
                    pass
                candidates.append(
                    _HostAddress(
                        address,
                        subnet,
                        name,
                        0
                        if key == "primary_address"
                        else 4
                        if key == "secondary_addresses"
                        else priority,
                    )
                )
    for route in _routes(host):
        if str(route.get("destination") or route.get("dst") or route.get("to")) in {
            "default",
            "0.0.0.0/0",
            "::/0",
        }:
            source = _usable_ip(
                route.get("src") or route.get("source") or route.get("preferred_source")
            )
            if source is not None:
                candidates.append(_HostAddress(source, priority=1))
    return sorted(candidates, key=lambda item: item.priority)


def _routes(host: dict[str, object]) -> Iterator[dict[str, object]]:
    for container in (host, _network(host)):
        for key in ("routes", "routes_by_destination", "routing"):
            for destination, route in _records(container.get(key)):
                if not any(key in route for key in ("destination", "dst", "to")):
                    route = {"destination": destination, **route}
                yield route


def _route_network(route: dict[str, object], version: int) -> _Network | None:
    destination = route.get("destination") or route.get("dst") or route.get("to")
    if destination == "default":
        destination = "0.0.0.0/0" if version == 4 else "::/0"
    try:
        result = ipaddress.ip_network(str(destination), strict=False)
    except ValueError:
        return None
    return result if result.version == version else None


def _facts(world: dict[str, object]) -> Iterator[dict[str, object]]:
    for container in (world, *_hosts(world).values()):
        if not isinstance(container, dict):
            continue
        for key in ("world_facts_by_id", "facts_by_id", "networks_by_id"):
            facts = container.get(key)
            if isinstance(facts, dict):
                yield from (fact for fact in facts.values() if isinstance(fact, dict))


def _target_mapping(world: dict[str, object], target: str) -> object:
    for fact in _facts(world):
        for key in ("targets", "service_mappings"):
            targets = fact.get(key)
            if isinstance(targets, dict):
                for name, value in targets.items():
                    if _clean_name(name) == target:
                        return value
    return None


def _mapped_address(value: object) -> _Address | None:
    if isinstance(value, str):
        return _ip(value)
    if isinstance(value, dict):
        for candidate in _address_values(value):
            address = _ip(candidate)
            if address is not None:
                return address
    return None


def _mapped_name(value: object) -> str | None:
    if isinstance(value, str) and _ip(value) is None:
        return _clean_name(value) or None
    if isinstance(value, dict):
        name = value.get("host_ref") or value.get("hostname") or value.get("host")
        return _clean_name(name) if name else None
    return None


def _record_address(host: dict[str, object], address: _Address) -> None:
    has_primary = bool(_host_addresses(host))
    network = host.setdefault("network", {})
    if not isinstance(network, dict):
        raise ValueError("simulated host network metadata must be an object")
    key = "ipv4_address" if address.version == 4 else "ipv6_address"
    if key not in network and not has_primary:
        network[key] = str(address)
    else:
        addresses = list(_strings(network.get("secondary_addresses")))
        if str(address) not in addresses:
            addresses.append(str(address))
        network["secondary_addresses"] = addresses


def _subnets(value: object, version: int) -> Iterator[_Network]:
    if isinstance(value, str) and "/" in value:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            return
        if network.version == version:
            yield network
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _subnets(key, version)
            yield from _subnets(child, version)
    elif isinstance(value, list):
        for child in value:
            yield from _subnets(child, version)


def _allocation_candidates(
    world: dict[str, object],
    ref: str,
    host: dict[str, object],
    version: int,
) -> list[_Network]:
    network = _network(host)
    candidates = []
    for key in (
        "subnet",
        "subnets",
        "cidr",
        "allocation",
        "private_allocation",
        "public_allocation",
        "runtime_address_allocation",
        "runtime_ipv6_allocation",
    ):
        candidates.extend(_subnets(network.get(key), version))
    for item in _host_addresses(host):
        if item.network is not None and item.network.version == version:
            candidates.append(item.network)
    gateway = _usable_ip(network.get("default_gateway") or network.get("gateway"))
    prefix = network.get("prefix_length") or network.get("netmask")
    if gateway is not None and gateway.version == version and prefix is not None:
        try:
            candidates.insert(0, ipaddress.ip_interface(f"{gateway}/{prefix}").network)
        except ValueError:
            pass
    names = _host_names(host) | {_clean_name(ref)}
    for fact in _facts(world):
        domain = str(fact.get("domain") or "").lower()
        sites = set(_strings(fact.get("site_ids")))
        matches = (
            bool(
                domain
                and any(name == domain or name.endswith("." + domain) for name in names)
            )
            or bool(host.get("site_id") and host.get("site_id") in sites)
            or bool(
                network.get("network_id")
                and network.get("network_id") == fact.get("network_id")
            )
        )
        if matches:
            keys = (
                ("public_allocation", "private_allocation")
                if network.get("public") is True
                else ("private_allocation", "public_allocation")
            )
            for key in keys:
                candidates.extend(_subnets(fact.get(key), version))
    return [
        candidate
        for candidate in candidates
        if not candidate.is_loopback and not candidate.is_multicast
    ]


def _occupied_addresses(world: dict[str, object]) -> set[_Address]:
    occupied = set()
    for host in _hosts(world).values():
        if isinstance(host, dict):
            occupied.update(item.address for item in _host_addresses(host))
            for value in _walk_strings(_network(host)):
                if (address := _ip(value)) is not None:
                    occupied.add(address)
    for fact in _facts(world):
        for key in ("targets", "service_mappings"):
            for value in _walk_strings(fact.get(key)):
                if (address := _ip(value)) is not None:
                    occupied.add(address)
    return occupied


def _walk_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def _stable_number(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big")


def _allocate_address(
    world: dict[str, object],
    ref: str,
    host: dict[str, object],
    *,
    version: int,
) -> _Address:
    occupied = _occupied_addresses(world)
    candidates = _allocation_candidates(world, ref, host, version)
    for candidate in candidates:
        address = _unused_address(candidate, occupied, ref)
        if address is not None:
            return address
    # No allocation is established for this host. Persist a bounded new subnet
    # before assigning its address, so later materialization can reuse it.
    seed = _stable_number(f"{world.get('name', '')}:{ref}")
    reserved = []
    for fact in _facts(world):
        reserved.extend(_subnets(fact, version))
    for other in _hosts(world).values():
        if isinstance(other, dict):
            other_network = _network(other)
            for key in (
                "subnet",
                "subnets",
                "cidr",
                "allocation",
                "private_allocation",
                "public_allocation",
                "runtime_address_allocation",
                "runtime_ipv6_allocation",
            ):
                reserved.extend(_subnets(other_network.get(key), version))
            reserved.extend(
                item.network
                for item in _host_addresses(other)
                if item.network is not None and item.network.version == version
            )
    public = any(
        name.endswith(".bsides") for name in _host_names(host) | {_clean_name(ref)}
    )
    if version == 6:
        pools = [
            ipaddress.ip_network(
                f"fd{seed & 0xFF:02x}:{(seed >> 8) & 0xFFFF:x}:{(seed >> 24) & 0xFFFF:x}::/48"
            )
        ]
        prefix = 64
    elif public:
        pools = list(
            _PUBLIC_POOLS[seed % len(_PUBLIC_POOLS) :]
            + _PUBLIC_POOLS[: seed % len(_PUBLIC_POOLS)]
        )
        prefix = 28
    else:
        pools = [
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        ]
        prefix = 24
    for pool in pools:
        if any(pool.subnet_of(used) for used in reserved):
            continue
        count = 1 << (prefix - pool.prefixlen)
        step = 0
        while step < count:
            subnet_index = (seed + step) % count
            offset = subnet_index << (pool.max_prefixlen - prefix)
            subnet = ipaddress.ip_network((int(pool.network_address) + offset, prefix))
            overlapping = [used for used in reserved if subnet.overlaps(used)]
            if overlapping:
                # Skip the entire recorded range, rather than iterating through
                # millions of small subnets inside an occupied public block.
                last = max(int(used.broadcast_address) for used in overlapping)
                last_index = min(
                    count - 1,
                    (last - int(pool.network_address)) >> (pool.max_prefixlen - prefix),
                )
                step += max(1, last_index - subnet_index + 1)
                continue
            step += 1
            if any(
                address.version == version and address in subnet for address in occupied
            ):
                continue
            address = _unused_address(subnet, occupied, ref)
            if address is not None:
                network = host.setdefault("network", {})
                if not isinstance(network, dict):
                    raise ValueError(
                        "simulated host network metadata must be an object"
                    )
                network[
                    "runtime_address_allocation"
                    if version == 4
                    else "runtime_ipv6_allocation"
                ] = str(subnet)
                return address
    raise ValueError("no unused simulated network address is available")


def _unused_address(
    network: _Network, occupied: set[_Address], seed: str
) -> _Address | None:
    # /31 and /127 links, and host routes, legitimately use their edge addresses.
    trim = 1 if network.num_addresses > 2 else 0
    count = network.num_addresses - 2 * trim
    start = _stable_number(seed) % count
    for step in range(min(count, len(occupied) + 4)):
        address = ipaddress.ip_address(
            int(network.network_address) + trim + (start + step) % count
        )
        if (
            address not in occupied
            and str(address) not in _EXCLUDED_NEW_ADDRESSES
            and _usable_ip(str(address)) is not None
        ):
            return address
    return None
