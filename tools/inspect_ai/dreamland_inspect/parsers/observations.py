"""Extract lightweight observations from command output."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field

from dreamland_inspect.events import CommandEvent
from dreamland_inspect.parsers.sections import OutputSection, split_sections

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
FQDN_RE = re.compile(r"\b[a-zA-Z0-9][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9-]+)+\b")
IP_ADDR_RE = re.compile(
    r"^\d+:\s+(?P<interface>\S+)\s+inet\s+(?P<cidr>[0-9.]+/\d+)\b"
)
ROUTE_RE = re.compile(
    r"^(?P<destination>default|[0-9.]+/\d+)\s+"
    r"(?:(?:via\s+(?P<gateway>[0-9.]+)\s+)?dev\s+(?P<interface>\S+))?"
)
NMAP_HOST_RE = re.compile(r"^Nmap scan report for (?P<target>.+)$")
NMAP_PORT_RE = re.compile(
    r"^(?P<port>\d+)/(tcp|udp)\s+(?P<state>\S+)\s+(?P<service>\S+)"
)


@dataclass(frozen=True, slots=True)
class InterfaceAddress:
    interface: str
    cidr: str


@dataclass(frozen=True, slots=True)
class Route:
    destination: str
    gateway: str | None
    interface: str | None


@dataclass(frozen=True, slots=True)
class NmapPort:
    port: int
    state: str
    service: str


@dataclass(frozen=True, slots=True)
class NmapHost:
    target: str
    addresses: list[str] = field(default_factory=list)
    ports: list[NmapPort] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CommandObservation:
    command_id: str
    sections: list[OutputSection]
    envelope_leak: bool
    hostnames: list[str]
    domains: list[str]
    ipv4_addresses: list[str]
    interfaces: list[InterfaceAddress]
    routes: list[Route]
    nmap_hosts: list[NmapHost]


def observe_event(event: CommandEvent) -> CommandObservation:
    sections = split_sections(event.stdout)
    text = event.stdout
    return CommandObservation(
        command_id=event.command_id,
        sections=sections,
        envelope_leak=looks_like_envelope_leak(text),
        hostnames=_ordered_unique(FQDN_RE.findall(text)),
        domains=_domains_from_text(text),
        ipv4_addresses=_valid_ipv4s(text),
        interfaces=_parse_interfaces(text),
        routes=_parse_routes(text),
        nmap_hosts=_parse_nmap(text),
    )


def looks_like_envelope_leak(stdout: str) -> bool:
    stripped = stdout.strip()
    if not stripped.startswith("{"):
        return False
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return (
            stripped.startswith('{"stdout"')
            and '"exit_status"' in stripped
            and '"state_update"' in stripped
        )
    if not isinstance(value, dict):
        return False
    return {"stdout", "stderr", "exit_status"}.issubset(value)


def _domains_from_text(text: str) -> list[str]:
    domains: list[str] = []
    for fqdn in FQDN_RE.findall(text):
        parts = fqdn.lower().split(".")
        if len(parts) >= 2:
            domains.append(".".join(parts[-2:]))
        if len(parts) >= 3:
            domains.append(".".join(parts[-3:]))
    return _ordered_unique(domains)


def _valid_ipv4s(text: str) -> list[str]:
    values: list[str] = []
    for candidate in IPV4_RE.findall(text):
        try:
            ipaddress.IPv4Address(candidate)
        except ipaddress.AddressValueError:
            continue
        values.append(candidate)
    return _ordered_unique(values)


def _parse_interfaces(text: str) -> list[InterfaceAddress]:
    interfaces: list[InterfaceAddress] = []
    for line in text.splitlines():
        match = IP_ADDR_RE.match(line)
        if match:
            interfaces.append(
                InterfaceAddress(
                    interface=match.group("interface"),
                    cidr=match.group("cidr"),
                )
            )
    return interfaces


def _parse_routes(text: str) -> list[Route]:
    routes: list[Route] = []
    for line in text.splitlines():
        match = ROUTE_RE.match(line)
        if match:
            routes.append(
                Route(
                    destination=match.group("destination"),
                    gateway=match.group("gateway"),
                    interface=match.group("interface"),
                )
            )
    return routes


def _parse_nmap(text: str) -> list[NmapHost]:
    hosts: list[NmapHost] = []
    current_target: str | None = None
    current_addresses: list[str] = []
    current_ports: list[NmapPort] = []

    def flush() -> None:
        nonlocal current_target, current_addresses, current_ports
        if current_target is not None:
            hosts.append(
                NmapHost(
                    target=current_target,
                    addresses=list(current_addresses),
                    ports=list(current_ports),
                )
            )
        current_target = None
        current_addresses = []
        current_ports = []

    for line in text.splitlines():
        host_match = NMAP_HOST_RE.match(line)
        if host_match:
            flush()
            current_target = host_match.group("target").strip()
            current_addresses = _valid_ipv4s(current_target)
            continue
        if current_target is None:
            continue
        port_match = NMAP_PORT_RE.match(line)
        if port_match:
            current_ports.append(
                NmapPort(
                    port=int(port_match.group("port")),
                    state=port_match.group("state"),
                    service=port_match.group("service"),
                )
            )
    flush()
    return hosts


def _ordered_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result

