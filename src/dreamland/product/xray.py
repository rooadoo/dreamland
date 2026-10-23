"""Display-safe Presenter Stage X-ray projection primitives.

This module is inert until an explicit Presenter Stage composition constructs an
X-ray-enabled service.  It accepts a detached immutable product runtime document
and emits only a small, bounded allowlist.  It deliberately does not import the
runtime ``SessionState`` model, profile stores, provider adapters, tracing, SSH,
or admin projections.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

XRAY_SCHEMA_REVISION = "stage-xray-v1"

_POLICY_MAXIMUMS = {
    "maximum_hosts": 64,
    "maximum_facts": 128,
    "maximum_fields_per_detail": 12,
    "maximum_text_characters": 512,
}
_FACT_VALUE_KEYS = (
    "statement",
    "description",
    "value",
    "summary",
    "status",
)
_FACT_DETAIL_KEYS = (
    "statement",
    "description",
    "value",
    "status",
    "kind",
    "category",
)
_BIDI_CONTROL_CLASSES = frozenset(
    {
        "LRE",
        "RLE",
        "LRO",
        "RLO",
        "PDF",
        "LRI",
        "RLI",
        "FSI",
        "PDI",
    }
)


@dataclass(frozen=True, slots=True)
class StageXrayPolicy:
    """Hard display and collection limits for one X-ray composition."""

    maximum_hosts: int = 64
    maximum_facts: int = 128
    maximum_fields_per_detail: int = 12
    maximum_text_characters: int = 512

    def __post_init__(self) -> None:
        for name, maximum in _POLICY_MAXIMUMS.items():
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= maximum
            ):
                raise ValueError(f"{name} must be between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class StageXrayProjection:
    """One coordinate-free projection and its same-snapshot detail records."""

    snapshot: Mapping[str, object]
    details: Mapping[str, Mapping[str, object]]
    content_sha256: str


def build_stage_xray_projection(
    document: Mapping[str, object],
    *,
    policy: StageXrayPolicy,
    active_command: Mapping[str, object] | None = None,
) -> StageXrayProjection:
    """Build one bounded projection without retaining or returning raw state."""

    if not isinstance(document, Mapping):
        raise TypeError("X-ray runtime document must be a mapping")
    if not isinstance(policy, StageXrayPolicy):
        raise TypeError("X-ray policy must be StageXrayPolicy")

    profile = _mapping(document.get("profile"))
    state = (
        _mapping(profile.get("state"))
        if profile is not None
        else _mapping(document.get("state"))
    ) or document
    world = _mapping(state.get("world_state")) or {}
    hosts = _mapping(world.get("hosts_by_name")) or {}
    active_host_key = world.get("active_host")
    entry_host_key = world.get("entry_host")

    details: dict[str, Mapping[str, object]] = {}
    host_summaries: list[dict[str, object]] = []
    fact_summaries: list[dict[str, object]] = []
    controls_marked = 0
    clipped_fields = 0
    invalid_hosts = 0
    invalid_facts = 0

    host_items = _safe_mapping_items(hosts)
    host_ids_by_key: dict[str, str] = {}
    host_id_owners: dict[str, str] = {}
    colliding_host_keys: set[str] = set()
    for host_key, _host_value in host_items:
        host_id = _safe_id("host", host_key)
        owner = host_id_owners.get(host_id)
        if owner is not None and owner != host_key:
            colliding_host_keys.add(host_key)
            continue
        host_id_owners[host_id] = host_key
        host_ids_by_key[host_key] = host_id

    selected_hosts = host_items[: policy.maximum_hosts]
    for host_key, host_value in selected_hosts:
        if host_key in colliding_host_keys:
            invalid_hosts += 1
            continue
        metadata = _mapping(host_value)
        if metadata is None:
            invalid_hosts += 1
            continue
        host_id = host_ids_by_key[host_key]
        hostname = _required_safe_text(
            metadata.get("hostname"),
            fallback=host_key or "Unavailable",
            maximum=policy.maximum_text_characters,
        )
        controls_marked += int(bool(hostname["control_marked"]))
        clipped_fields += int(bool(hostname["clipped"]))
        purpose = _mapping(metadata.get("purpose")) or {}
        theme = _optional_safe_text(
            purpose.get("theme"), policy.maximum_text_characters
        )
        description = _optional_safe_text(
            purpose.get("description"), policy.maximum_text_characters
        )
        for value in (theme, description):
            if value is not None:
                controls_marked += int(bool(value["control_marked"]))
                clipped_fields += int(bool(value["clipped"]))
        local_facts = _mapping(metadata.get("facts_by_id")) or {}
        services = _mapping(metadata.get("services_by_name")) or {}
        summary: dict[str, object] = {
            "entity_id": host_id,
            "entity_kind": "host",
            "hostname": hostname,
            "active": host_key == active_host_key,
            "entry": host_key == entry_host_key,
            "accepted_fact_count": len(_safe_mapping_items(local_facts)),
            "service_count": len(_safe_mapping_items(services)),
        }
        if theme is not None:
            summary["theme"] = theme
        fields: list[dict[str, object]] = [
            _field("hostname", "Hostname", hostname),
        ]
        if theme is not None:
            fields.append(_field("purpose_theme", "Purpose", theme))
        if description is not None:
            fields.append(_field("purpose_description", "Description", description))
        operating_system = _mapping(metadata.get("operating_system")) or {}
        for key, label in (
            ("family", "Operating system family"),
            ("distribution", "Distribution"),
            ("version", "Operating system version"),
        ):
            value = _optional_safe_text(
                operating_system.get(key), policy.maximum_text_characters
            )
            if value is not None:
                controls_marked += int(bool(value["control_marked"]))
                clipped_fields += int(bool(value["clipped"]))
                fields.append(_field(f"operating_system_{key}", label, value))
        if not _publish_entity(
            summaries=host_summaries,
            details=details,
            summary=summary,
            detail={
                "entity_id": host_id,
                "entity_kind": "host",
                "fields": fields[: policy.maximum_fields_per_detail],
                "accepted": True,
                "evidence": {"state": "unavailable"},
            },
        ):
            invalid_hosts += 1

    fact_candidates: list[tuple[str, str | None, object]] = []
    for fact_key, fact_value in _safe_mapping_items(
        _mapping(world.get("world_facts_by_id")) or {}
    ):
        fact_candidates.append((fact_key, None, fact_value))
    for host_key, host_value in host_items:
        metadata = _mapping(host_value)
        if metadata is None:
            continue
        for fact_key, fact_value in _safe_mapping_items(
            _mapping(metadata.get("facts_by_id")) or {}
        ):
            fact_candidates.append((fact_key, host_key, fact_value))

    for fact_key, host_key, fact_value in fact_candidates[: policy.maximum_facts]:
        if host_key in colliding_host_keys:
            invalid_facts += 1
            continue
        fact_id = _fact_entity_id(fact_key, host_key=host_key)
        if fact_id in details:
            invalid_facts += 1
            continue
        host_id = host_ids_by_key.get(host_key) if host_key is not None else None
        statement_source = _fact_statement(fact_value)
        if statement_source is None:
            invalid_facts += 1
            continue
        statement = _optional_safe_text(
            statement_source, policy.maximum_text_characters
        )
        if statement is None:
            invalid_facts += 1
            continue
        controls_marked += int(bool(statement["control_marked"]))
        clipped_fields += int(bool(statement["clipped"]))
        label_source = (
            fact_value.get("label", fact_value.get("name", fact_key))
            if isinstance(fact_value, Mapping)
            else fact_key
        )
        label = _required_safe_text(
            label_source,
            fallback=fact_key or "Fact",
            maximum=policy.maximum_text_characters,
        )
        controls_marked += int(bool(label["control_marked"]))
        clipped_fields += int(bool(label["clipped"]))
        summary = {
            "entity_id": fact_id,
            "entity_kind": "fact",
            "label": label,
            "statement": statement,
            "scope": "host" if host_id is not None else "world",
            "related_host_id": host_id,
        }
        fact_fields: list[dict[str, object]] = []
        if isinstance(fact_value, Mapping):
            for key in _FACT_DETAIL_KEYS:
                value = _optional_safe_text(
                    fact_value.get(key), policy.maximum_text_characters
                )
                if value is None:
                    continue
                controls_marked += int(bool(value["control_marked"]))
                clipped_fields += int(bool(value["clipped"]))
                fact_fields.append(_field(key, key.replace("_", " ").title(), value))
        if not fact_fields:
            fact_fields.append(_field("statement", "Statement", statement))
        if not _publish_entity(
            summaries=fact_summaries,
            details=details,
            summary=summary,
            detail={
                "entity_id": fact_id,
                "entity_kind": "fact",
                "fields": fact_fields[: policy.maximum_fields_per_detail],
                "accepted": True,
                "related_host_id": host_id,
                "evidence": {"state": "unavailable"},
            },
        ):
            invalid_facts += 1

    world_name = _optional_safe_text(world.get("name"), policy.maximum_text_characters)
    world_purpose = _mapping(world.get("purpose")) or {}
    world_theme = _optional_safe_text(
        world_purpose.get("theme"), policy.maximum_text_characters
    )
    world_description = _optional_safe_text(
        world_purpose.get("description"), policy.maximum_text_characters
    )
    for value in (world_name, world_theme, world_description):
        if value is not None:
            controls_marked += int(bool(value["control_marked"]))
            clipped_fields += int(bool(value["clipped"]))

    overview: dict[str, object] = {}
    if world_name is not None:
        overview["world_name"] = world_name
    if world_theme is not None:
        overview["theme"] = world_theme
    if world_description is not None:
        overview["description"] = world_description
    if isinstance(active_host_key, str) and active_host_key not in colliding_host_keys:
        overview["active_host_id"] = host_ids_by_key.get(
            active_host_key,
            _safe_id("host", active_host_key),
        )

    activity = _activity_projection(active_command)
    included_facts = len(fact_summaries)
    omitted_hosts = max(0, len(host_items) - len(selected_hosts)) + invalid_hosts
    omitted_facts = max(0, len(fact_candidates) - policy.maximum_facts) + invalid_facts
    snapshot: dict[str, object] = {
        "schema_revision": XRAY_SCHEMA_REVISION,
        "overview": overview,
        "hosts": host_summaries,
        "facts": fact_summaries,
        "activity": activity,
        "completeness": {
            "complete": (
                omitted_hosts == 0 and omitted_facts == 0 and clipped_fields == 0
            ),
            "hosts": {
                "included_count": len(host_summaries),
                "omitted_count": omitted_hosts,
            },
            "facts": {
                "included_count": included_facts,
                "omitted_count": omitted_facts,
            },
            "clipped_field_count": clipped_fields,
            "control_marked_field_count": controls_marked,
        },
    }
    _require_projection_entity_invariants(
        host_summaries=host_summaries,
        fact_summaries=fact_summaries,
        details=details,
    )
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return StageXrayProjection(
        snapshot=MappingProxyType(snapshot),
        details=MappingProxyType(details),
        content_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _activity_projection(
    active_command: Mapping[str, object] | None,
) -> dict[str, object]:
    if active_command is None:
        return {"foreground": {"state": "idle"}, "enrichment": {"state": "idle"}}
    command_id = active_command.get("command_id")
    status = active_command.get("status")
    if not isinstance(command_id, str) or not isinstance(status, str):
        return {
            "foreground": {"state": "unavailable"},
            "enrichment": {"state": "idle"},
        }
    return {
        "foreground": {
            "state": "active",
            "command_id": command_id,
            "status": status
            if status in {"accepted", "running", "cancel_requested"}
            else "active",
        },
        "enrichment": {"state": "idle"},
    }


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return value


def _safe_mapping_items(value: Mapping[str, object]) -> list[tuple[str, object]]:
    return sorted(
        ((key, item) for key, item in value.items() if isinstance(key, str)),
        key=lambda pair: pair[0].encode("utf-8", errors="replace"),
    )


def _safe_id(kind: str, source: str) -> str:
    digest = hashlib.sha256(
        f"{kind}\0{source}".encode("utf-8", errors="replace")
    ).hexdigest()
    return f"{kind}:{digest[:32]}"


def _fact_entity_id(fact_key: str, *, host_key: str | None) -> str:
    components = (
        ["world-fact", fact_key]
        if host_key is None
        else ["host-fact", host_key, fact_key]
    )
    source = json.dumps(
        components,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return _safe_id("fact", source)


def _publish_entity(
    *,
    summaries: list[dict[str, object]],
    details: dict[str, Mapping[str, object]],
    summary: dict[str, object],
    detail: dict[str, object],
) -> bool:
    entity_id = summary.get("entity_id")
    entity_kind = summary.get("entity_kind")
    if (
        not isinstance(entity_id, str)
        or entity_kind not in {"host", "fact"}
        or detail.get("entity_id") != entity_id
        or detail.get("entity_kind") != entity_kind
    ):
        raise RuntimeError("Stage X-ray entity summary and detail do not match")
    if entity_id in details:
        return False
    details[entity_id] = MappingProxyType(detail)
    summaries.append(summary)
    return True


def _require_projection_entity_invariants(
    *,
    host_summaries: list[dict[str, object]],
    fact_summaries: list[dict[str, object]],
    details: dict[str, Mapping[str, object]],
) -> None:
    summaries = [*host_summaries, *fact_summaries]
    entity_ids = [summary.get("entity_id") for summary in summaries]
    if len(entity_ids) != len(set(entity_ids)) or set(details) != set(entity_ids):
        raise RuntimeError("Stage X-ray projection entity identities are inconsistent")
    for summary in summaries:
        entity_id = summary["entity_id"]
        detail = details[entity_id]
        if detail.get("entity_id") != entity_id or detail.get(
            "entity_kind"
        ) != summary.get("entity_kind"):
            raise RuntimeError("Stage X-ray projection entity detail is inconsistent")


def _safe_text(value: object, maximum: int) -> dict[str, object]:
    if isinstance(value, bool):
        raw = "true" if value else "false"
    elif isinstance(value, (int, float)) and not isinstance(value, complex):
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    else:
        raw = "Unavailable"
    marked = False
    safe_characters: list[str] = []
    for character in raw:
        category = unicodedata.category(character)
        if (
            category in {"Cc", "Cf", "Cs"}
            or unicodedata.bidirectional(character) in _BIDI_CONTROL_CLASSES
        ):
            safe_characters.append("�")
            marked = True
        else:
            safe_characters.append(character)
    safe = "".join(safe_characters)
    clipped = len(safe) > maximum
    if clipped:
        safe = safe[:maximum]
    return {"text": safe, "clipped": clipped, "control_marked": marked}


def _optional_safe_text(value: object, maximum: int) -> dict[str, object] | None:
    if not isinstance(value, (str, int, float, bool)):
        return None
    safe = _safe_text(value, maximum)
    return safe if safe["text"] else None


def _required_safe_text(
    value: object,
    *,
    fallback: str,
    maximum: int,
) -> dict[str, object]:
    safe = _optional_safe_text(value, maximum)
    if safe is not None:
        return safe
    return _safe_text(fallback, maximum)


def _fact_statement(value: object) -> object | None:
    if isinstance(value, (str, int, float, bool)):
        return value
    if not isinstance(value, Mapping):
        return None
    for key in _FACT_VALUE_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and not candidate:
            continue
        if isinstance(candidate, (str, int, float, bool)):
            return candidate
    return None


def _field(name: str, label: str, value: Mapping[str, object]) -> dict[str, object]:
    return {"name": name, "label": label, "value": dict(value)}
