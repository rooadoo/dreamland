"""Read explicit SSH endpoint translation facts, without inferring network paths."""

from __future__ import annotations

import ipaddress

from dreamland.network_identity import known_host_addresses, resolve_host_ref


def translated_ssh_target(
    world: dict[str, object],
    source_host: str,
    target: str,
    port: int | None,
) -> dict[str, object] | None:
    """Return one validated matching translation; absent/ambiguous rules return None.

    A rule is a category-network world fact's ``connection_translation`` object.
    ``source`` is the peer observed by the server after source translation;
    ``destination`` is the server-local endpoint after destination translation.
    Neither endpoint is written to world state by this read-only resolver.
    """
    facts = world.get("world_facts_by_id")
    if not isinstance(facts, dict):
        return None
    requested_port = _port(port) if port is not None else 22
    if requested_port is None:
        return None
    canonical_source = resolve_host_ref(world, source_host) or _name(source_host)
    matches = []
    for fact_id, fact in facts.items():
        if not isinstance(fact, dict) or fact.get("category") != "network":
            continue
        rule = fact.get("connection_translation")
        if not isinstance(rule, dict):
            continue
        rule_target = rule.get("target")
        if not isinstance(rule_target, str) or not _same_target(
            world, rule_target, target
        ):
            continue
        scope = rule.get("source_host")
        if scope is not None:
            if not isinstance(scope, str) or not scope.strip():
                continue
            if (resolve_host_ref(world, scope) or _name(scope)) != canonical_source:
                continue
        if "target_port" in rule:
            rule_port = _port(rule["target_port"])
            if rule_port is None or rule_port != requested_port:
                continue
        else:
            rule_port = None
        normalized = _validated_rule(world, rule, target)
        if normalized is None:
            # A matching malformed rule must not silently select a different
            # conflicting mapping according to JSON insertion order.
            return None
        target_address = _address(target) or _address(rule_target)
        target_addresses = _target_addresses(world, target)
        destination = normalized.get("destination")
        local_address = destination.get("ip") if isinstance(destination, dict) else None
        if target_address is None and len(target_addresses) == 1:
            target_address = next(iter(target_addresses))
        if target_address is None and "destination" not in rule:
            if local_address in target_addresses or not target_addresses:
                target_address = local_address
        if target_address is None and ("destination" in rule or target_addresses):
            # DNAT needs the actual public endpoint observed by the client.
            # A hostname with no established mapping cannot supply that fact.
            return None
        normalized["target_address"] = target_address
        normalized.update({"fact_id": str(fact_id), "target": _name(rule_target)})
        if scope is not None:
            normalized["source_host"] = canonical_source
        if rule_port is not None:
            normalized["target_port"] = rule_port
        matches.append(normalized)
    return matches[0] if len(matches) == 1 else None


def _validated_rule(
    world: dict[str, object], rule: dict[str, object], requested_target: str
) -> dict[str, object] | None:
    destination_name = rule.get("destination_host")
    if not isinstance(destination_name, str) or not destination_name.strip():
        return None
    destination_host = (
        resolve_host_ref(world, destination_name) or destination_name.strip()
    )
    result: dict[str, object] = {"destination_host": destination_host}
    for key in ("source", "destination"):
        if key not in rule:
            continue
        endpoint = _endpoint(rule[key])
        if endpoint is None:
            return None
        result[key] = endpoint
    if "source" not in result and "destination" not in result:
        return None
    local_addresses = known_host_addresses(world, destination_host)
    destination = result.get("destination")
    if isinstance(destination, dict):
        address = destination["ip"]
        # An explicit mapping may supply a missing host address, but cannot
        # renumber an established host or appropriate another host's address.
        owner = resolve_host_ref(world, str(address))
        if owner is not None and owner != destination_host:
            return None
        if local_addresses and address not in local_addresses:
            return None
        local_addresses = (str(address),)
    target_addresses = _target_addresses(world, requested_target)
    source = result.get("source")
    if not isinstance(destination, dict):
        usable = [
            address
            for address in local_addresses
            if not target_addresses or address in target_addresses
        ]
        if isinstance(source, dict):
            family = ipaddress.ip_address(str(source["ip"])).version
            usable = [
                address
                for address in usable
                if ipaddress.ip_address(address).version == family
            ]
        if usable:
            local_addresses = (usable[0],)
            result["destination"] = {"ip": usable[0]}
        elif local_addresses and target_addresses:
            return None
        elif len(target_addresses) == 1 and not local_addresses:
            local_addresses = tuple(target_addresses)
            result["destination"] = {"ip": local_addresses[0]}
    local_families = {
        ipaddress.ip_address(address).version for address in local_addresses
    }
    target_families = {
        ipaddress.ip_address(address).version for address in target_addresses
    }
    if (
        local_families
        and target_families
        and not local_families.intersection(target_families)
    ):
        return None
    if isinstance(source, dict):
        family = ipaddress.ip_address(str(source["ip"])).version
        if local_families and family not in local_families:
            return None
        if target_families and family not in target_families:
            return None
        if not local_families and not target_families and family != 4:
            # An unknown host otherwise receives a generated IPv4 primary. Do
            # not approve a mapping that would require inferring NAT64.
            return None
    return result


def _same_target(world: dict[str, object], first: str, second: str) -> bool:
    if not _name(first) or not _name(second):
        return False
    if _name(first) == _name(second):
        return True
    return bool(
        _target_addresses(world, first).intersection(_target_addresses(world, second))
    )


def _target_addresses(world: dict[str, object], target: str) -> set[str]:
    address = _address(target)
    if address is not None:
        return {address}
    addresses = set()
    facts = world.get("world_facts_by_id")
    if isinstance(facts, dict):
        for fact in facts.values():
            if not isinstance(fact, dict):
                continue
            for key in ("targets", "service_mappings"):
                mappings = fact.get(key)
                if not isinstance(mappings, dict):
                    continue
                for name, value in mappings.items():
                    if _name(name) != _name(target):
                        continue
                    if isinstance(value, dict):
                        value = (
                            value.get("ip")
                            or value.get("address")
                            or value.get("ipv4_address")
                            or value.get("ipv6_address")
                        )
                    address = _address(value)
                    if address is not None:
                        addresses.add(address)
    if addresses:
        return addresses
    host = resolve_host_ref(world, target)
    return set(known_host_addresses(world, host)) if host is not None else set()


def _endpoint(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    address = _address(value.get("ip"))
    if address is None:
        return None
    endpoint: dict[str, object] = {"ip": address}
    if "port" in value:
        port = _port(value["port"])
        if port is None:
            return None
        endpoint["port"] = port
    return endpoint


def _address(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None
    if (
        address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or str(address) == "255.255.255.255"
    ):
        return None
    return str(address)


def _port(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if 1 <= value <= 65535 else None


def _name(value: object) -> str:
    return str(value or "").strip().strip("[]").rstrip(".").lower()
