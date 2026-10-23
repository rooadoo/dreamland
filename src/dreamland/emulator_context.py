from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from ipaddress import ip_interface

from dreamland.world_state import active_host_ref


def _field_names(names: str) -> frozenset[str]:
    return frozenset(names.split())


_NETWORK_TRAVERSAL_EXCLUDED_KEYS = _field_names(
    """
    artifacts artifacts_by_path command_history environment environment_metadata
    files files_by_path filesystem groups groups_by_name history interactive_program
    shell shell_environment shell_stack users users_by_username
    """
)
_COMPACT_HOST_IDENTITY_KEYS = _field_names(
    """
    aliases distribution domain domain_name fqdn host hostname kind model name
    os_family platform release role site site_id type zone
    """
)
_COMPACT_PURPOSE_KEYS = _field_names("description name role summary theme type")
_COMPACT_OS_KEYS = _field_names(
    "distribution edition family name os_family platform release variant version"
)
_COMPACT_FACT_KEYS = _field_names(
    "description fact hostname os os_family role site summary text"
)
_HOST_OWNED_ADDRESS_KEYS = _field_names(
    """
    address addresses bind bind_address ip ip_address ip_addresses ipv4
    ipv4_address ipv4_addresses ipv6 ipv6_address ipv6_addresses listen
    listen_address listen_addresses
    """
)
_COMPACT_SERVICE_KEYS = _HOST_OWNED_ADDRESS_KEYS | _field_names(
    """
    endpoint endpoints fqdn health host hostname name port ports protocol protocols
    state status target targets transport upstream upstreams uri uris url urls
    """
)
_NETWORK_CONTEXT_KEYS = _field_names(
    "device family interface label name scope service type"
)
_NETWORK_SIGNAL_KEYS = _HOST_OWNED_ADDRESS_KEYS | _field_names(
    """
    cidr destination dns dns_server dns_servers domain domain_name endpoint endpoints
    fabric fqdn gateway health host hostname interface interfaces listening_port
    listening_ports mac mac_address netmask network network_interface
    network_interfaces networks port ports prefix prefix_length protocol protocols
    route routes routing segment segments state status subnet subnets target targets
    upstream upstreams uri uris url urls via vlan vlans
    """
)
_COMPACT_HOST_NETWORK_KEYS = _HOST_OWNED_ADDRESS_KEYS | _field_names(
    """
    cidr connectivity dns dns_server dns_servers gateway interface interfaces mac
    mac_address netmask network networking network_interface network_interfaces
    networks route routes routing subnet subnets
    """
)
_HOST_NETWORK_CONTAINER_KEYS = _field_names(
    """
    hardware interface interfaces management network networking network_interface
    network_interfaces
    """
)
_NON_OWNED_NETWORK_CONTAINER_KEYS = _field_names(
    """
    dns endpoint endpoints gateway gateways peer peers route routes routing service
    services
    """
)
_OMIT = object()


def project_world_state_for_command_emulator(
    world_state: dict[str, object],
    *,
    detailed_host_refs: Iterable[str] = (),
) -> dict[str, object]:
    """Return a non-mutating, host-focused view for command-emulator prompts.

    Every top-level field and host registry key remains present. The active host
    and any explicitly selected hosts retain their metadata except the saved
    initial login banner, which is runtime presentation data. Other
    hosts retain only enough identity, network, fact, and service information
    to anchor cross-host behavior without exposing unrelated local state.
    """
    if not isinstance(world_state, dict):
        return {}

    projected = {
        key: deepcopy(value)
        for key, value in world_state.items()
        if key != "hosts_by_name"
    }
    hosts = world_state.get("hosts_by_name")
    if not isinstance(hosts, dict):
        if "hosts_by_name" in world_state:
            projected["hosts_by_name"] = deepcopy(hosts)
        return projected

    detailed_registry_keys = set(
        detailed_host_registry_refs_for_command_emulator(
            world_state,
            detailed_host_refs=detailed_host_refs,
        )
    )

    projected_hosts: dict[str, object] = {}
    for host_ref, metadata in hosts.items():
        if host_ref in detailed_registry_keys:
            projected_host = deepcopy(metadata)
            if isinstance(projected_host, dict):
                projected_host.pop("login_banner", None)
            projected_hosts[host_ref] = projected_host
        elif isinstance(metadata, dict):
            projected_hosts[host_ref] = _compact_host_identity(metadata)
        else:
            projected_hosts[host_ref] = {}
    projected["hosts_by_name"] = projected_hosts
    return projected


def detailed_host_registry_refs_for_command_emulator(
    world_state: dict[str, object],
    *,
    detailed_host_refs: Iterable[str] = (),
) -> tuple[str, ...]:
    """Resolve active and explicitly detailed references to host registry keys."""
    hosts = world_state.get("hosts_by_name")
    if not isinstance(hosts, dict):
        return ()

    requested_refs = [active_host_ref(world_state)]
    if isinstance(detailed_host_refs, str):
        requested_refs.append(detailed_host_refs)
    else:
        requested_refs.extend(
            host_ref for host_ref in detailed_host_refs if isinstance(host_ref, str)
        )
    resolved = _resolve_host_registry_keys(hosts, requested_refs)
    return tuple(
        host_ref
        for host_ref in hosts
        if isinstance(host_ref, str) and host_ref in resolved
    )


def _resolve_host_registry_keys(
    hosts: dict[object, object],
    host_refs: Iterable[str],
) -> set[object]:
    refs = tuple(dict.fromkeys(host_refs))
    resolved: set[object] = set()
    for host_ref in refs:
        if host_ref in hosts:
            resolved.add(host_ref)
            continue

        registry_key = _first_host_key_matching(
            hosts,
            host_ref,
            _hostname_matches,
        )
        if registry_key is None:
            registry_key = _first_host_key_matching(
                hosts,
                host_ref,
                _aliases_include,
            )
        if registry_key is None:
            registry_key = _first_host_key_matching(
                hosts,
                host_ref,
                _host_owned_address_contains,
            )
        if registry_key is not None:
            resolved.add(registry_key)
    return resolved


def _first_host_key_matching(
    hosts: dict[object, object],
    host_ref: str,
    predicate: Callable[[dict[str, object], str], bool],
) -> object | None:
    for registry_key, metadata in hosts.items():
        if isinstance(metadata, dict) and predicate(metadata, host_ref):
            return registry_key
    return None


def _hostname_matches(metadata: dict[str, object], host_ref: str) -> bool:
    return metadata.get("hostname") == host_ref


def _aliases_include(metadata: dict[str, object], host_ref: str) -> bool:
    return _value_contains(metadata.get("aliases"), host_ref)


def _host_owned_address_contains(
    metadata: dict[str, object],
    host_ref: str,
) -> bool:
    for key, value in metadata.items():
        normalized = str(key).lower()
        if normalized in _HOST_OWNED_ADDRESS_KEYS and _value_contains(value, host_ref):
            return True
        if normalized in _HOST_NETWORK_CONTAINER_KEYS and _owned_network_value_contains(
            value,
            host_ref,
        ):
            return True
    return False


def _owned_network_value_contains(value: object, host_ref: str) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _NON_OWNED_NETWORK_CONTAINER_KEYS:
                continue
            if normalized in _HOST_OWNED_ADDRESS_KEYS and _value_contains(
                child,
                host_ref,
            ):
                return True
            if _owned_network_value_contains(child, host_ref):
                return True
    elif isinstance(value, list):
        return any(_owned_network_value_contains(item, host_ref) for item in value)
    elif isinstance(value, str):
        return _host_reference_matches(value, host_ref)
    return False


def _compact_host_identity(metadata: dict[str, object]) -> dict[str, object]:
    compact: dict[str, object] = {}
    for field_name, value in metadata.items():
        if field_name in _COMPACT_HOST_IDENTITY_KEYS:
            fields = _COMPACT_PURPOSE_KEYS if field_name == "role" else frozenset()
            projected = _compact_named_value(value, fields=fields)
        elif field_name == "purpose":
            projected = _compact_named_value(
                value,
                fields=_COMPACT_PURPOSE_KEYS,
            )
        elif field_name in {"operating_system", "os"}:
            projected = _compact_named_value(value, fields=_COMPACT_OS_KEYS)
        elif field_name in _COMPACT_HOST_NETWORK_KEYS:
            projected = _compact_network_signal(value)
        elif field_name == "facts_by_id":
            projected = _compact_fact_registry(value)
        elif field_name in {
            "configured_services",
            "services",
            "services_by_name",
        }:
            projected = _compact_service_registry(value)
        else:
            continue
        if projected is not _OMIT:
            compact[field_name] = projected

    network_projection = _compact_address_paths(metadata)
    if isinstance(network_projection, dict):
        _merge_compact_projection(compact, network_projection)
    return compact


def _compact_named_value(value: object, *, fields: frozenset[str]) -> object:
    scalar = _compact_scalar_value(value)
    if scalar is not _OMIT:
        return scalar
    if not fields or not isinstance(value, dict):
        return _OMIT
    projected = {
        key: compact
        for key, child in value.items()
        if key in fields and (compact := _compact_scalar_value(child)) is not _OMIT
    }
    return projected if projected else _OMIT


def _compact_fact_registry(value: object) -> object:
    if not isinstance(value, dict):
        return _OMIT
    projected: dict[str, object] = {}
    for fact_id, fact in value.items():
        scalar = _compact_scalar_value(fact)
        if scalar is not _OMIT:
            projected[fact_id] = scalar
            continue
        if not isinstance(fact, dict):
            continue
        compact_fact = {
            key: compact
            for key, child in fact.items()
            if key in _COMPACT_FACT_KEYS
            and (compact := _compact_scalar_value(child)) is not _OMIT
        }
        network_projection = _compact_address_paths(fact)
        if isinstance(network_projection, dict):
            _merge_compact_projection(compact_fact, network_projection)
        if compact_fact:
            projected[fact_id] = compact_fact
    return projected if projected else _OMIT


def _compact_service_registry(value: object) -> object:
    if isinstance(value, dict):
        return {
            service_name: (
                compact
                if (compact := _compact_service_metadata(service)) is not _OMIT
                else {}
            )
            for service_name, service in value.items()
        }
    if isinstance(value, list):
        projected_services = [
            compact
            for service in value
            if (compact := _compact_service_metadata(service)) is not _OMIT
        ]
        return projected_services if projected_services else _OMIT
    return _OMIT


def _compact_service_metadata(value: object) -> object:
    scalar = _compact_scalar_value(value)
    if scalar is not _OMIT:
        return scalar
    if not isinstance(value, dict):
        return _OMIT
    projected: dict[str, object] = {}
    for key, child in value.items():
        if key not in _COMPACT_SERVICE_KEYS:
            continue
        if key in _NETWORK_SIGNAL_KEYS:
            compact = _compact_network_signal(child)
        else:
            compact = _compact_scalar_value(child)
        if compact is not _OMIT:
            projected[key] = compact
    network_projection = _compact_address_paths(value)
    if isinstance(network_projection, dict):
        _merge_compact_projection(projected, network_projection)
    return projected


def _compact_address_paths(value: object) -> object:
    if isinstance(value, str) and _is_ip_reference(value):
        return value
    if isinstance(value, dict):
        projected: dict[str, object] = {}
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _NETWORK_TRAVERSAL_EXCLUDED_KEYS:
                continue
            if _is_ip_reference(str(key)):
                nested = _compact_address_paths(child)
                compact = nested if nested is not _OMIT else {}
            else:
                compact = _compact_address_paths(child)
            if compact is not _OMIT:
                projected[key] = compact
        if not projected:
            return _OMIT
        return projected
    if isinstance(value, list):
        projected_items = [
            compact
            for item in value
            if (compact := _compact_address_paths(item)) is not _OMIT
        ]
        return projected_items if projected_items else _OMIT
    return _OMIT


def _compact_network_signal(value: object) -> object:
    scalar = _compact_scalar_value(value)
    if scalar is not _OMIT:
        return scalar
    if isinstance(value, dict):
        projected: dict[str, object] = {}
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _NETWORK_TRAVERSAL_EXCLUDED_KEYS:
                continue
            if (
                normalized in _NETWORK_SIGNAL_KEYS
                or normalized in _NETWORK_CONTEXT_KEYS
                or _is_ip_reference(str(key))
            ):
                compact = _compact_network_signal(child)
                if compact is _OMIT and _is_ip_reference(str(key)):
                    compact = {}
            elif isinstance(child, str) and _is_ip_reference(child):
                compact = child
            else:
                compact = _compact_address_paths(child)
            if compact is not _OMIT:
                projected[key] = compact
        return projected if projected else _OMIT
    if isinstance(value, list):
        projected_items = []
        for item in value:
            compact = _compact_network_signal(item)
            if compact is not _OMIT:
                projected_items.append(compact)
        return projected_items if projected_items else _OMIT
    return _OMIT


def _compact_scalar_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return deepcopy(value)
    if isinstance(value, (list, tuple)) and all(
        child is None or isinstance(child, (str, int, float, bool)) for child in value
    ):
        return deepcopy(value)
    return _OMIT


def _merge_compact_projection(
    target: dict[str, object],
    source: dict[str, object],
) -> None:
    for key, value in source.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_compact_projection(current, value)
        elif key not in target:
            target[key] = deepcopy(value)


def _value_contains(value: object, expected: str) -> bool:
    if isinstance(value, str) and _host_reference_matches(value, expected):
        return True
    if isinstance(value, (list, tuple)):
        return any(_value_contains(child, expected) for child in value)
    if isinstance(value, dict):
        return any(
            _host_reference_matches(str(key), expected)
            or _value_contains(child, expected)
            for key, child in value.items()
        )
    return False


def _is_ip_reference(value: str) -> bool:
    return _ip_address_for_reference(value) is not None


def _host_reference_matches(candidate: str, expected: str) -> bool:
    if candidate == expected:
        return True
    candidate_ip = _ip_address_for_reference(candidate)
    expected_ip = _ip_address_for_reference(expected)
    return candidate_ip is not None and candidate_ip == expected_ip


def _ip_address_for_reference(value: str) -> object | None:
    candidate = value.strip().strip("[]")
    if not candidate:
        return None
    try:
        return ip_interface(candidate).ip
    except ValueError:
        return None
