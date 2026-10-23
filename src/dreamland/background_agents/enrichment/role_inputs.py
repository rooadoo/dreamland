"""Role-specific prompt inputs for the EnrichmentAgent graph.

The Coordinator is intentionally DREAMLAND-aware.  The Expert is not: its
brief is built from a small allowlist of ordinary systems observations, and no
generic world-state object crosses that boundary.  Reference IDs are opaque,
job-local handles that reconnect the Expert's report to current state for the
Integrator.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Final

from dreamland.background_agents.enrichment.foreground_evidence import (
    FOREGROUND_EVIDENCE_SCHEMA_VERSION,
)
from dreamland.background_agents.enrichment.latitude import (
    CreativeLatitude,
    creative_latitude_from_snapshot,
)
from dreamland.background_agents.enrichment.modes import EnrichmentMode
from dreamland.background_agents.surfaces import SUPPORTED_SURFACES

MAX_REFERENCE_CATALOG: Final[int] = 48
MAX_EXPERT_OBSERVATIONS: Final[int] = 12
PRESENCE_GAP_POLICY_VERSION: Final[str] = "presence-gap-v2"
AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_POLICY_VERSION: Final[str] = (
    "ambient-human-presence-v3"
)

_AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_SUFFIX: Final[str] = (
    "Additional requirement: center one named human individual other than the "
    "active interactive user. Only when supplied evidence or explicit direction "
    "establishes nonhuman people in this setting, center one named "
    "person-equivalent individual with personal or social agency instead. "
    "Service, shared, role, daemon, machine, automation, device, institution, "
    "and group identities may provide supporting context but cannot satisfy the "
    "requested individual. Return connected evidence of that individual's "
    "time-qualified personal, work, or social activity and concrete observable "
    "residue. If supplied evidence requires solitude or excludes such an "
    "individual, do not substitute a service identity. The foreground person "
    "includes that person's active, login, effective, or privileged accounts "
    "and any established alias; an account transition does not create a new "
    "person. Treat every directly observed login, session, process, or socket "
    "row and its username, PID, TTY, endpoints, source, and observation time as "
    "reserved to its observed or unknown owner. Do not reassign that row to the "
    "requested individual unless the row itself identifies the individual. A "
    "distinct historical or later event is allowed only when explicitly "
    "time-qualified and independently grounded by supplied evidence outside "
    "the directly observed row. Do not copy or reuse the row's username, PID, "
    "TTY, endpoint, port, source, or observation time for the requested "
    "individual merely by shifting chronology. Time qualification alone does "
    "not authorize reuse, and the distinct event must not be presented as "
    "explaining the observed foreground row."
)
_AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_SHA256: Final[str] = hashlib.sha256(
    _AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_SUFFIX.encode("utf-8")
).hexdigest()

_CONTROL_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("generation_policy", "generation_policy"),
    ("simulation", "simulation_policy"),
    ("expansion_directives_by_id", "expansion_directives"),
)
_HOST_IDENTITY_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("hostname", "hostname"),
    ("aliases", "aliases"),
    ("fqdn", "fully_qualified_name"),
    ("domain", "domain"),
    ("domain_name", "domain"),
    ("role", "role"),
    ("site", "site"),
    ("site_id", "site"),
    ("zone", "zone"),
    ("purpose", "purpose"),
    ("operating_system", "operating_system"),
    ("os", "operating_system"),
    ("platform", "platform"),
)
_PURPOSE_REFERENCE_FIELDS: Final[tuple[str, ...]] = (
    "description",
    "name",
    "role",
    "summary",
    "theme",
    "type",
)
_PURPOSE_FIELDS: Final[frozenset[str]] = frozenset(_PURPOSE_REFERENCE_FIELDS)
_REFERENCE_REGISTRIES: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("services_by_name",), "service"),
    (("facts_by_id",), "fact"),
    (("filesystem", "artifacts_by_path"), "artifact"),
    (("users_by_username",), "user"),
    (("groups_by_name",), "group"),
)
_DIRECT_REFERENCES: Final[tuple[tuple[str, str], ...]] = (
    ("operating_system", "operating_system"),
    ("os", "operating_system"),
    ("platform", "platform"),
    ("network", "network"),
    ("networking", "network"),
    ("hardware", "hardware"),
    ("shell", "shell"),
)
_SURFACE_OBSERVATION_FIELDS: Final[dict[str, tuple[tuple[str, str, str], ...]]] = {
    "database": (
        ("databases", "database", "databases"),
        ("database", "database", "database"),
    ),
    "filesystem": (("filesystem", "filesystem", "filesystem"),),
    "identity": (
        ("users", "identity", "users"),
        ("groups", "identity", "groups"),
    ),
    "network": (
        ("network", "network", "network"),
        ("networking", "network", "network"),
    ),
    "packages": (
        ("packages", "packages", "packages"),
        ("installed_packages", "packages", "packages"),
    ),
    "process": (
        ("processes", "process", "processes"),
        ("processes_by_id", "process", "processes"),
    ),
    "service": (("configured_services", "service", "services"),),
    "shell": (("shell", "shell", "shell"),),
}
_INTERNAL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "expansion_directives_by_id",
        "generation_policy",
        "hosts_by_name",
        "simulation",
        "world_facts_by_id",
        "world_state",
    }
)
_INTERNAL_TEXT_MARKERS: Final[tuple[str, ...]] = (
    "dreamland",
    "enrichmentagent",
    "enrichment agent",
    "world state",
    "expansion_directives_by_id",
    "expansion directives",
    "generation_policy",
    "generation policy",
    "world_state",
    "operator_bounds",
    "operator bounds",
    "materialized_state",
    "materialized state",
    "available_references",
    "available references",
    "reference_bindings",
    "reference bindings",
    "job_id",
    "trigger_id",
    "event_id",
    "session_id",
    "correlation_id",
    "put_if_absent",
    "merge_missing",
    "enrichment patch",
)
_INTERNAL_ROUTING_MARKERS: Final[tuple[str, ...]] = (
    "coordinator",
    "integrator",
    "emulator",
)
_OMIT = object()
_MISSING = object()

_COVERAGE_REGISTRIES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("facts", ("facts_by_id",)),
    ("filesystem_artifacts", ("filesystem", "artifacts_by_path")),
    ("groups", ("groups_by_name",)),
    ("services", ("services_by_name",)),
    ("users", ("users_by_username",)),
)
_PRESENCE_MATERIALIZATION_SURFACES: Final[tuple[str, ...]] = (
    "identity",
    "service",
    "filesystem",
)
_PRESENCE_HINT_LIMIT: Final[int] = 4
_PRESENCE_TOKEN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
_PRESENCE_SYSTEM_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "activity",
        "agent",
        "artifacts",
        "cache",
        "config",
        "configuration",
        "current",
        "data",
        "etc",
        "facts",
        "file",
        "files",
        "group",
        "groups",
        "history",
        "home",
        "host",
        "lib",
        "local",
        "log",
        "logs",
        "process",
        "processes",
        "record",
        "records",
        "run",
        "service",
        "services",
        "state",
        "system",
        "target",
        "unit",
        "user",
        "users",
        "usr",
        "var",
    }
)


class RoleInputValidationError(ValueError):
    """Reject a role handoff without placing sensitive input in logs."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        location: str = "$",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.location = location


@dataclass(frozen=True, slots=True)
class EstablishedReference:
    """One observed target-host entity with a private state binding."""

    id: str
    kind: str
    label: str
    observation: object
    path: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation", deepcopy(self.observation))
        object.__setattr__(self, "path", tuple(self.path))

    def public_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "observation": deepcopy(self.observation),
        }


@dataclass(frozen=True, slots=True)
class CoordinatorPromptInput:
    payload: dict[str, object]
    references: tuple[EstablishedReference, ...]
    omitted_references: int


def build_coordinator_prompt_input(
    snapshot: Mapping[str, object],
    *,
    target_host: str,
    trigger: str,
    surface_hint: str | None,
    focus_hints: Sequence[str],
    interaction_focus: Mapping[str, object] | None = None,
    mode: EnrichmentMode = EnrichmentMode.FOCUSED,
    coverage_summary: Mapping[str, object] | None = None,
) -> CoordinatorPromptInput:
    """Organize state around the Coordinator's planning responsibility."""

    world = _snapshot_world(snapshot)
    resolved_mode = EnrichmentMode(mode)
    references, omitted = build_reference_catalog(
        snapshot,
        target_host=target_host,
    )
    resolved_coverage = (
        build_coordinator_coverage_summary(snapshot, target_host=target_host)
        if coverage_summary is None
        else _validated_coverage_summary(coverage_summary)
    )
    operator_bounds: dict[str, object] = {}
    materialized_state: dict[str, object] = {}
    control_names = {source for source, _destination in _CONTROL_FIELDS}
    for key, value in world.items():
        if key not in control_names:
            materialized_state[str(key)] = deepcopy(value)
    for source, destination in _CONTROL_FIELDS:
        if source in world:
            operator_bounds[destination] = deepcopy(world[source])

    wake_reason: dict[str, object] = {
        "trigger": trigger,
        "focus_hints": list(focus_hints),
    }
    if surface_hint is not None:
        wake_reason["surface_hint"] = surface_hint
    if interaction_focus:
        wake_reason["interaction_focus"] = deepcopy(dict(interaction_focus))
    payload: dict[str, object] = {
        "enrichment_mode": resolved_mode.value,
        "coverage_summary": resolved_coverage,
        "host_time": _snapshot_host_time(snapshot),
        "recent_exhaustive_evidence": _snapshot_exhaustive_evidence(snapshot),
        "target_host": target_host,
        "wake_reason": wake_reason,
        "operator_bounds": operator_bounds,
        "available_references": [item.public_payload() for item in references],
        "materialized_state": materialized_state,
    }
    if resolved_mode is EnrichmentMode.AMBIENT_PRESENCE:
        payload["presence_gap"] = build_presence_gap(
            snapshot,
            target_host=target_host,
            surface_hint=surface_hint,
            coverage_summary=resolved_coverage,
        )
    foreground_authority = snapshot.get("foreground_authority")
    if isinstance(foreground_authority, Mapping):
        payload["foreground_authority"] = deepcopy(dict(foreground_authority))
    return CoordinatorPromptInput(
        payload=payload,
        references=references,
        omitted_references=omitted,
    )


def build_coordinator_coverage_summary(
    snapshot: Mapping[str, object],
    *,
    target_host: str,
) -> dict[str, int]:
    """Return compact deterministic counts of already materialized host detail.

    The summary is deliberately structural. It helps a Coordinator broaden
    coverage without asking a model to produce bookkeeping or treating the
    absence of a registry entry as proof that the represented world is empty.
    """

    host = _target_host(snapshot, target_host)
    summary: dict[str, int] = {}
    for name, path in _COVERAGE_REGISTRIES:
        value = _value_at_path(host, path)
        summary[name] = len(value) if isinstance(value, Mapping) else 0
    return summary


def build_presence_gap(
    snapshot: Mapping[str, object],
    *,
    target_host: str,
    surface_hint: str | None,
    coverage_summary: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Select a compact deterministic ambient-materialization priority.

    This is a planning aid, not evidence and not a generated assignment. A
    useful non-shell wake surface is preserved. Otherwise, the least-covered
    identity, service, or filesystem surface is selected, with the foreground
    login discounted from identity coverage. Explicit world or profile
    direction remains authoritative over every preference here.
    """

    host = _target_host(snapshot, target_host)
    coverage = (
        build_coordinator_coverage_summary(snapshot, target_host=target_host)
        if coverage_summary is None
        else _validated_coverage_summary(coverage_summary)
    )
    login_names = _foreground_login_names(snapshot, target_host=target_host)
    users = _value_at_path(host, ("users_by_username",))
    independent_users = 0
    if isinstance(users, Mapping):
        independent_users = sum(
            1 for username in users if str(username).casefold() not in login_names
        )

    materialization_counts = {
        "identity": independent_users + coverage["groups"],
        "service": coverage["services"],
        "filesystem": coverage["filesystem_artifacts"],
    }
    preferred_surface = (
        surface_hint
        if surface_hint in SUPPORTED_SURFACES and surface_hint != "shell"
        else min(
            _PRESENCE_MATERIALIZATION_SURFACES,
            key=materialization_counts.__getitem__,
        )
    )
    subjects, systems = _presence_repetition_hints(
        host,
        foreground_login_names=login_names,
    )
    return {
        "policy_version": PRESENCE_GAP_POLICY_VERSION,
        "ambient_objective": "independent_life",
        "preferred_surface": preferred_surface,
        "subject_preference": (
            "named_human_or_person_equivalent_excluding_foreground_user_"
            "and_service_accounts"
        ),
        "anti_repetition": {
            "subjects": subjects,
            "systems": systems,
        },
    }


def _foreground_login_names(
    snapshot: Mapping[str, object],
    *,
    target_host: str,
) -> frozenset[str]:
    authority = snapshot.get("foreground_authority")
    if not isinstance(authority, Mapping):
        return frozenset()
    authority_target = authority.get("target_host")
    if (
        isinstance(authority_target, str)
        and authority_target
        and authority_target != target_host
    ):
        return frozenset()
    return frozenset(
        value.casefold()
        for field in ("active_username", "login_username")
        if isinstance((value := authority.get(field)), str) and value.strip()
    )


def _presence_repetition_hints(
    host: Mapping[str, object],
    *,
    foreground_login_names: frozenset[str],
) -> tuple[list[str], list[str]]:
    """Return bounded state-derived names as soft, job-local repeat hints."""

    subjects: list[str] = []
    for registry_name, prefix in (
        ("users_by_username", "user"),
        ("groups_by_name", "group"),
    ):
        registry = host.get(registry_name)
        if not isinstance(registry, Mapping):
            continue
        for key in sorted((str(item) for item in registry), key=str.casefold):
            if registry_name == "users_by_username" and key.casefold() in (
                foreground_login_names
            ):
                continue
            hint = _bounded_presence_hint(key)
            if not hint:
                continue
            subjects.append(f"{prefix}:{hint}")
            if len(subjects) >= _PRESENCE_HINT_LIMIT:
                break
        if len(subjects) >= _PRESENCE_HINT_LIMIT:
            break

    service_registry = host.get("services_by_name")
    systems: list[str] = []
    if isinstance(service_registry, Mapping):
        for key in sorted(
            (str(item) for item in service_registry),
            key=str.casefold,
        )[:_PRESENCE_HINT_LIMIT]:
            hint = _bounded_presence_hint(key)
            if hint:
                systems.append(f"service:{hint}")
    if len(systems) < _PRESENCE_HINT_LIMIT:
        systems.extend(
            _repeated_materialized_system_terms(
                host,
                excluded={item.partition(":")[2].casefold() for item in systems},
            )[: _PRESENCE_HINT_LIMIT - len(systems)]
        )
    return subjects, systems


def _repeated_materialized_system_terms(
    host: Mapping[str, object],
    *,
    excluded: set[str],
) -> list[str]:
    occurrences: Counter[str] = Counter()
    paths = (
        ("facts_by_id",),
        ("filesystem", "artifacts_by_path"),
        ("services_by_name",),
    )
    for path in paths:
        registry = _value_at_path(host, path)
        if not isinstance(registry, Mapping):
            continue
        for key in registry:
            tokens = {
                token
                for token in _PRESENCE_TOKEN.findall(str(key).casefold())
                if (
                    len(token) >= 3
                    and not token.isdigit()
                    and token not in _PRESENCE_SYSTEM_STOPWORDS
                )
            }
            occurrences.update(tokens)
    return [
        token
        for token, count in sorted(
            occurrences.items(),
            key=lambda item: (-item[1], item[0]),
        )
        if count >= 2 and token not in excluded
    ]


def _bounded_presence_hint(value: str) -> str:
    return " ".join(value.split())[:64]


def _validated_coverage_summary(
    coverage_summary: Mapping[str, object],
) -> dict[str, int]:
    expected = {name for name, _path in _COVERAGE_REGISTRIES}
    if set(coverage_summary) != expected:
        raise RoleInputValidationError(
            "coverage summary fields do not match the deterministic contract",
            reason_code="invalid_coverage_summary",
            location="$.coverage_summary",
        )
    validated: dict[str, int] = {}
    for name, _path in _COVERAGE_REGISTRIES:
        value = coverage_summary[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RoleInputValidationError(
                "coverage summary counts must be nonnegative integers",
                reason_code="invalid_coverage_summary",
                location=f"$.coverage_summary.{name}",
            )
        validated[name] = value
    return validated


def build_reference_catalog(
    snapshot: Mapping[str, object],
    *,
    target_host: str,
    maximum: int = MAX_REFERENCE_CATALOG,
) -> tuple[tuple[EstablishedReference, ...], int]:
    """Create bounded, deterministic references from established target state."""

    if maximum < 1:
        raise ValueError("maximum must be at least one")
    host = _target_host(snapshot, target_host)
    candidates: list[tuple[str, str, object, tuple[str, ...]]] = []

    host_binding = _first_present_path(host, ("hostname", "aliases", "purpose"))
    if host_binding is not None:
        field = host_binding[0]
        observed_identity = _neutral_host_field(field, host[field])
        if _has_neutral_content(observed_identity):
            candidates.append(
                (
                    "host",
                    _neutral_identifier(target_host, fallback="target_host"),
                    {
                        "name": _neutral_identifier(
                            target_host,
                            fallback="target_host",
                        ),
                        field: observed_identity,
                    },
                    host_binding,
                )
            )

    seen_direct_kinds: set[str] = set()
    for field, kind in _DIRECT_REFERENCES:
        if field not in host or kind in seen_direct_kinds:
            continue
        observation = _neutral_host_field(field, host[field])
        if not _has_neutral_content(observation):
            continue
        candidates.append((kind, kind, observation, (field,)))
        seen_direct_kinds.add(kind)

    purpose = host.get("purpose")
    if isinstance(purpose, Mapping):
        for field in _PURPOSE_REFERENCE_FIELDS:
            if field not in purpose:
                continue
            observation = _neutral_copy(purpose[field])
            if not _has_neutral_content(observation):
                continue
            candidates.append(("purpose", field, observation, ("purpose", field)))

    for registry_path, kind in _REFERENCE_REGISTRIES:
        registry = _value_at_path(host, registry_path)
        if not isinstance(registry, Mapping):
            continue
        for label in sorted(key for key in registry if isinstance(key, str)):
            if _contains_internal_text(label):
                continue
            observation = _neutral_copy(registry[label])
            if observation is _OMIT:
                continue
            candidates.append((kind, label, observation, (*registry_path, label)))

    kept = candidates[:maximum]
    references = tuple(
        EstablishedReference(
            id=f"r{index}",
            kind=kind,
            label=label,
            observation=observation,
            path=path,
        )
        for index, (kind, label, observation, path) in enumerate(kept, start=1)
    )
    return references, max(0, len(candidates) - len(kept))


def build_expert_brief(
    snapshot: Mapping[str, object],
    *,
    target_host: str,
    surface: str,
    expertise: Mapping[str, str],
    question: str,
    selected_reference_ids: Sequence[str],
    reference_catalog: Sequence[EstablishedReference],
    mode: EnrichmentMode = EnrichmentMode.FOCUSED,
) -> tuple[
    dict[str, object],
    tuple[EstablishedReference, ...],
    CreativeLatitude,
]:
    """Translate planning state into a neutral real-world domain brief."""

    if _contains_internal_text(question):
        raise RoleInputValidationError(
            "expert question contains internal system vocabulary",
            reason_code="internal_vocabulary",
            location="$.expert_task",
        )
    resolved_mode = EnrichmentMode(mode)
    expert_question = _expert_question_for_mode(question, mode=resolved_mode)
    references_by_id = {item.id: item for item in reference_catalog}
    selected: list[EstablishedReference] = []
    for index, reference_id in enumerate(selected_reference_ids):
        reference = references_by_id.get(reference_id)
        if reference is None:
            raise RoleInputValidationError(
                "Coordinator selected an unavailable reference",
                reason_code="unknown_reference",
                location=f"$.anchors[{index}]",
            )
        selected.append(reference)

    host = _target_host(snapshot, target_host)
    foreground_authority = snapshot.get("foreground_authority")
    observations = _expert_observations(
        host,
        surface=surface,
        host_time=_snapshot_host_time(snapshot),
        recent_exhaustive_evidence=_snapshot_exhaustive_evidence(snapshot),
        foreground_authority=(
            foreground_authority if isinstance(foreground_authority, Mapping) else None
        ),
    )
    latitude = creative_latitude_from_snapshot(snapshot)
    brief: dict[str, object] = {
        "expertise": deepcopy(dict(expertise)),
        "question": expert_question,
        "latitude": latitude.public_payload(),
        "host": _neutral_host_identity(host, target_host=target_host),
        "observations": observations,
        "references": [item.public_payload() for item in selected],
    }
    _validate_expert_brief(brief)
    return brief, tuple(selected), latitude


def _expert_question_for_mode(
    question: str,
    *,
    mode: EnrichmentMode,
) -> str:
    """Apply the versioned ambient-only subject constraint deterministically."""

    if mode is not EnrichmentMode.AMBIENT_PRESENCE:
        return question
    return f"{question.rstrip()} {_AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_SUFFIX}"


def expert_task_policy_log_fields(
    *,
    mode: EnrichmentMode,
) -> dict[str, object]:
    """Describe an applied deterministic Expert-task policy without its prose."""

    resolved_mode = EnrichmentMode(mode)
    if resolved_mode is not EnrichmentMode.AMBIENT_PRESENCE:
        return {}
    return {
        "policy_version": AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_POLICY_VERSION,
        "action": "ambient_human_expert_constraint_appended",
        "policy_sha256": _AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_SHA256,
        "policy_chars": len(_AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_SUFFIX),
    }


def resolve_current_reference_bindings(
    current_host: Mapping[str, object],
    references: Sequence[EstablishedReference],
) -> list[dict[str, object]]:
    """Resolve selected job-local handles against the Integrator's fresh host."""

    bindings: list[dict[str, object]] = []
    for reference in references:
        expected = _value_at_path(current_host, reference.path)
        if expected is _MISSING:
            raise RoleInputValidationError(
                "selected reference is absent from current state",
                reason_code="reference_not_current",
                location=f"$.references.{reference.id}",
            )
        bindings.append(
            {
                "id": reference.id,
                "kind": reference.kind,
                "path": list(reference.path),
                "expected": deepcopy(expected),
            }
        )
    return bindings


def _snapshot_world(snapshot: Mapping[str, object]) -> Mapping[str, object]:
    world = snapshot.get("world")
    if not isinstance(world, Mapping):
        raise RoleInputValidationError(
            "snapshot has no world object",
            reason_code="invalid_snapshot",
            location="$.world",
        )
    return world


def _snapshot_host_time(snapshot: Mapping[str, object]) -> dict[str, object]:
    value = snapshot.get("host_time")
    if not isinstance(value, Mapping):
        return {
            "v": FOREGROUND_EVIDENCE_SCHEMA_VERSION,
            "status": "unknown",
        }
    return deepcopy(dict(value))


def _snapshot_exhaustive_evidence(
    snapshot: Mapping[str, object],
) -> list[dict[str, object]]:
    value = snapshot.get("recent_exhaustive_evidence")
    if not isinstance(value, (list, tuple)):
        return []
    return [deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]


def _target_host(
    snapshot: Mapping[str, object],
    target_host: str,
) -> Mapping[str, object]:
    world = _snapshot_world(snapshot)
    hosts = world.get("hosts_by_name")
    if not isinstance(hosts, Mapping) or not isinstance(
        hosts.get(target_host), Mapping
    ):
        raise RoleInputValidationError(
            "target host is absent from snapshot",
            reason_code="target_missing",
            location=f"$.world.hosts_by_name.{target_host}",
        )
    return hosts[target_host]


def _neutral_host_identity(
    host: Mapping[str, object],
    *,
    target_host: str,
) -> dict[str, object]:
    identity: dict[str, object] = {
        "name": _neutral_identifier(target_host, fallback="described_host")
    }
    for source, destination in _HOST_IDENTITY_FIELDS:
        if source not in host or destination in identity:
            continue
        value = _neutral_host_field(source, host[source])
        if _has_neutral_content(value):
            identity[destination] = value
    return identity


def _neutral_host_field(field: str, value: object) -> object:
    if field != "purpose" or not isinstance(value, Mapping):
        return _neutral_copy(value)
    purpose: dict[str, object] = {}
    for key, child in value.items():
        key_text = str(key)
        if key_text not in _PURPOSE_FIELDS:
            continue
        copied = _neutral_copy(child)
        if copied is not _OMIT:
            purpose[key_text] = copied
    return purpose


def _expert_observations(
    host: Mapping[str, object],
    *,
    surface: str,
    host_time: Mapping[str, object],
    recent_exhaustive_evidence: Sequence[Mapping[str, object]],
    foreground_authority: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    typed_command_ids = {
        value
        for evidence in (host_time, *recent_exhaustive_evidence)
        if isinstance((value := evidence.get("command_id")), str) and value
    }
    if foreground_authority is not None:
        details = _neutral_copy(foreground_authority)
        if _has_neutral_content(details):
            observations.append(
                {
                    "kind": "active_session",
                    "label": "directly observed active session",
                    "details": details,
                }
            )
    neutral_host_time = _neutral_copy(host_time)
    if _has_neutral_content(neutral_host_time):
        observations.append(
            {
                "kind": "host_time",
                "label": "recorded host time",
                "details": neutral_host_time,
            }
        )
    for evidence in recent_exhaustive_evidence:
        details = _neutral_copy(evidence)
        if not _has_neutral_content(details):
            continue
        observations.append(
            {
                "kind": "recent_exhaustive_observation",
                "label": str(evidence.get("kind", "foreground inventory")),
                "details": details,
            }
        )
    for field, kind, label in _SURFACE_OBSERVATION_FIELDS.get(surface, ()):
        if field not in host:
            continue
        value = host[field]
        if field == "filesystem" and isinstance(value, Mapping):
            value = {
                str(key): child
                for key, child in value.items()
                if key != "artifacts_by_path"
            }
        details = _neutral_copy(value)
        if not _has_neutral_content(details):
            continue
        observations.append({"kind": kind, "label": label, "details": details})

    history = host.get("command_history")
    if isinstance(history, list):
        recent = [
            (index, item)
            for index, item in enumerate(history[-4:])
            if isinstance(item, Mapping)
        ]
        recent.sort(
            key=lambda indexed: (
                0 if indexed[1].get("surface") == surface else 1,
                -indexed[0],
            )
        )
        for _index, item in recent:
            if item.get("id") in typed_command_ids:
                continue
            details = _neutral_copy(item)
            if not _has_neutral_content(details):
                continue
            is_recorded_result = item.get("evidence_kind") == "recorded_command_result"
            observations.append(
                {
                    "kind": (
                        "recent_command_result"
                        if is_recorded_result
                        else "recent_activity"
                    ),
                    "label": "command",
                    "details": details,
                }
            )
    return observations[:MAX_EXPERT_OBSERVATIONS]


def _neutral_copy(value: object) -> object:
    if isinstance(value, str):
        return _OMIT if _contains_internal_text(value) else value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            key_text = str(key)
            if key_text.lower() in _INTERNAL_KEYS or _contains_internal_text(
                key_text,
                routing=True,
            ):
                continue
            copied = _neutral_copy(child)
            if copied is not _OMIT:
                result[key_text] = copied
        return result
    if isinstance(value, (list, tuple)):
        copied_items = [
            copied for child in value if (copied := _neutral_copy(child)) is not _OMIT
        ]
        return copied_items
    return deepcopy(value)


def _contains_internal_text(value: str, *, routing: bool = False) -> bool:
    normalized = _normalized_text(value)
    markers = (
        (*_INTERNAL_TEXT_MARKERS, *_INTERNAL_ROUTING_MARKERS)
        if routing
        else _INTERNAL_TEXT_MARKERS
    )
    return any(_normalized_text(marker) in normalized for marker in markers)


def _normalized_text(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _neutral_identifier(value: str, *, fallback: str) -> str:
    return fallback if _contains_internal_text(value) else value


def _validate_expert_brief(value: object, *, location: str = "$") -> None:
    if isinstance(value, str):
        is_routing_text = location == "$.question"
        if _contains_internal_text(value, routing=is_routing_text):
            raise RoleInputValidationError(
                "Expert brief contains internal system vocabulary",
                reason_code="internal_vocabulary",
                location=location,
            )
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if _contains_internal_text(key_text, routing=True):
                raise RoleInputValidationError(
                    "Expert brief contains an internal system key",
                    reason_code="internal_vocabulary",
                    location=f"{location}.{key_text}",
                )
            _validate_expert_brief(child, location=f"{location}.{key_text}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_expert_brief(child, location=f"{location}[{index}]")


def _has_neutral_content(value: object) -> bool:
    return value is not _OMIT and value != {} and value != []


def _first_present_path(
    host: Mapping[str, object],
    fields: Sequence[str],
) -> tuple[str, ...] | None:
    return next(((field,) for field in fields if field in host), None)


def _value_at_path(root: Mapping[str, object], path: Sequence[str]) -> object:
    current: object = root
    for segment in path:
        if not isinstance(current, Mapping) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


__all__ = [
    "AMBIENT_HUMAN_PRESENCE_EXPERT_TASK_POLICY_VERSION",
    "CoordinatorPromptInput",
    "EstablishedReference",
    "MAX_EXPERT_OBSERVATIONS",
    "MAX_REFERENCE_CATALOG",
    "PRESENCE_GAP_POLICY_VERSION",
    "RoleInputValidationError",
    "build_coordinator_coverage_summary",
    "build_coordinator_prompt_input",
    "build_expert_brief",
    "build_presence_gap",
    "build_reference_catalog",
    "expert_task_policy_log_fields",
    "resolve_current_reference_bindings",
]
