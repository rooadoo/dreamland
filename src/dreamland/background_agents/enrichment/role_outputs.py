"""Compact model-output contracts for EnrichmentAgent roles.

This module deliberately keeps schema parsing and validation separate from the
inspectable LangGraph topology in :mod:`dreamland.background_agents.enrichment.graph`.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from dreamland.background_agents.enrichment.context import (
    EnrichmentContextProjection,
    canonical_json,
)
from dreamland.background_agents.enrichment.latitude import CandidateOrigin
from dreamland.background_agents.enrichment.limits import (
    MAX_COORDINATOR_ANCHORS,
    MAX_EXPERT_AUXILIARY_ENTRIES,
    MAX_EXPERT_CANDIDATES,
    MAX_EXPERT_RELATIONS,
)
from dreamland.background_agents.enrichment.modes import EnrichmentMode
from dreamland.background_agents.surfaces import SUPPORTED_SURFACES
from dreamland.config import EnrichmentRoleSettings, EnrichmentSettings
from dreamland.lobby_identity import public_trace_metadata
from dreamland.model_providers import ChatModelProviderConfig
from dreamland.prompt_loader import load_prompt

MAX_EXPERTISE_TEXT = 80
MAX_EXPERT_REASON_TEXT = 160

CoordinatorStatus = Literal["work", "no_op"]
ExpertStatus = Literal["ok", "insufficient"]
ExpertOrigin = CandidateOrigin
ModelFactory = Callable[[ChatModelProviderConfig], Any]
ApiKeyResolver = Callable[[str], str | None]


class JobTraceContext(Protocol):
    @property
    def mode(self) -> EnrichmentMode:
        pass

    @property
    def job_id(self) -> str:
        pass

    @property
    def session_id(self) -> str:
        pass

    @property
    def trigger_id(self) -> str:
        pass

    @property
    def event_id(self) -> str:
        pass

    @property
    def target_host(self) -> str:
        pass

    @property
    def correlation_id(self) -> str | None:
        pass

    @property
    def caused_by_event_id(self) -> str | None:
        pass

    @property
    def command_id(self) -> str | None:
        pass

    @property
    def surface_hint(self) -> str | None:
        pass

    @property
    def anchors(self) -> tuple[str, ...]:
        pass

    @property
    def langfuse_session_id(self) -> str | None:
        pass

    @property
    def username(self) -> str | None:
        pass

    @property
    def profile_name(self) -> str | None:
        pass

    @property
    def thread_id(self) -> str:
        pass


class RoleOutputValidationError(ValueError):
    """Strict role-output rejection with a stable diagnostic reason."""

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


class _RelationValidationError(RoleOutputValidationError):
    """Strict relation failure with a more specific salvage rule."""

    def __init__(self, message: str, *, rule: str, location: str) -> None:
        super().__init__(
            message,
            reason_code="invalid_reference",
            location=location,
        )
        self.relation_rule = rule


@dataclass(frozen=True, slots=True)
class ExpertiseScope:
    """A short routing lens which selects knowledge without establishing facts."""

    domain: str
    perspective: str

    def to_payload(self) -> dict[str, str]:
        return {
            "domain": self.domain,
            "perspective": self.perspective,
        }


@dataclass(frozen=True, slots=True)
class CoordinatorDecision:
    status: CoordinatorStatus
    reason: str
    surface: str | None = None
    expertise: ExpertiseScope | None = None
    expert_task: str | None = None
    anchors: tuple[str, ...] = ()
    schema_version: int = 2

    def to_payload(self) -> dict[str, object]:
        return {
            "v": self.schema_version,
            "status": self.status,
            "surface": self.surface,
            "reason": self.reason,
            "expertise": (
                self.expertise.to_payload() if self.expertise is not None else None
            ),
            "expert_task": self.expert_task,
            "anchors": list(self.anchors),
        }


@dataclass(frozen=True, slots=True)
class ExpertCondition:
    id: str
    description: str

    def to_payload(self) -> list[str]:
        return [self.id, self.description]


@dataclass(frozen=True, slots=True)
class ExpertItem:
    id: str
    kind: str
    name: str
    detail: str
    origin: ExpertOrigin
    basis: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "basis", tuple(self.basis))

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "detail": self.detail,
            "origin": self.origin,
            "basis": list(self.basis),
        }


@dataclass(frozen=True, slots=True)
class ExpertGate:
    item_id: str
    condition_ids: tuple[str, ...]

    def to_payload(self) -> list[object]:
        return [self.item_id, list(self.condition_ids)]


@dataclass(frozen=True, slots=True)
class ExpertRelation:
    subject: str
    predicate: str
    object: str

    def to_payload(self) -> list[str]:
        return [self.subject, self.predicate, self.object]


@dataclass(frozen=True, slots=True)
class ExpertReport:
    status: ExpertStatus
    reason: str | None
    conditions: tuple[ExpertCondition, ...]
    items: tuple[ExpertItem, ...]
    gates: tuple[ExpertGate, ...]
    relations: tuple[ExpertRelation, ...]
    schema_version: int = 2

    def to_payload(self) -> dict[str, object]:
        return {
            "v": self.schema_version,
            "status": self.status,
            "reason": self.reason,
            "conditions": [item.to_payload() for item in self.conditions],
            "items": [item.to_payload() for item in self.items],
            "gates": [item.to_payload() for item in self.gates],
            "relations": [item.to_payload() for item in self.relations],
        }


@dataclass(frozen=True, slots=True)
class ExpertRelationRejection:
    """One auxiliary Expert relation removed at the deterministic boundary."""

    index: int
    rule: str
    reason_code: str
    location: str


@dataclass(frozen=True, slots=True)
class ExpertReportParseResult:
    """A valid Expert report plus bounded auxiliary-relation diagnostics."""

    report: ExpertReport
    rejected_relations: tuple[ExpertRelationRejection, ...] = ()


@dataclass(frozen=True, slots=True)
class RoleRuntime:
    """One configured model role consumed by the graph's logged call node."""

    role: str
    model: Any
    provider: str
    model_name: str
    system_prompt: str
    reasoning_effort: str | None
    verbosity: str | None
    max_output_tokens: int | None
    openai_api: str | None = None
    openai_service_tier: str | None = None
    openai_reasoning_mode: str | None = None


class LazyModel:
    """Delay provider construction until an enrichment role is first invoked."""

    def __init__(self, factory: ModelFactory, config: ChatModelProviderConfig) -> None:
        self._factory = factory
        self._config = config
        self._lock = threading.Lock()
        self._model: Any | None = None

    def invoke(self, *args: object, **kwargs: object) -> Any:
        return self._get().invoke(*args, **kwargs)

    def _get(self) -> Any:
        with self._lock:
            if self._model is None:
                self._model = self._factory(self._config)
            return self._model


def injected_role_runtimes(
    *,
    models: tuple[Any, Any, Any, Any],
    model_names: tuple[str, str, str, str],
    providers: tuple[str, str, str, str],
    prompts: tuple[str | None, str | None, str | None, str | None],
    reasoning_efforts: tuple[str | None, str | None, str | None, str | None],
    verbosities: tuple[str | None, str | None, str | None, str | None],
    max_output_tokens: tuple[int | None, int | None, int | None, int | None],
) -> dict[str, RoleRuntime]:
    roles = ("coordinator", "ambient_coordinator", "expert", "integrator")
    return {
        role: RoleRuntime(
            role=role,
            model=models[index],
            provider=providers[index],
            model_name=model_names[index],
            system_prompt=prompts[index] or load_prompt(f"enrichment_{role}.md"),
            reasoning_effort=reasoning_efforts[index],
            verbosity=verbosities[index],
            max_output_tokens=max_output_tokens[index],
        )
        for index, role in enumerate(roles)
    }


def configured_role_runtimes(
    settings: EnrichmentSettings,
    *,
    api_key_resolver: ApiKeyResolver,
    model_factory: ModelFactory,
    lazy_models: bool,
) -> dict[str, RoleRuntime]:
    return {
        role: _role_from_settings(
            role,
            getattr(settings, role),
            api_key_resolver,
            model_factory,
            lazy_model=lazy_models,
        )
        for role in ("coordinator", "ambient_coordinator", "expert", "integrator")
    }


def role_runtime_constructor_kwargs(
    roles: Mapping[str, RoleRuntime],
) -> dict[str, Any]:
    """Translate configured roles back to the stable injected-model API."""

    values: dict[str, Any] = {}
    for role in ("coordinator", "ambient_coordinator", "expert", "integrator"):
        runtime = roles[role]
        values.update(
            {
                f"{role}_model": runtime.model,
                f"{role}_model_name": runtime.model_name,
                f"{role}_provider": runtime.provider,
                f"{role}_prompt": runtime.system_prompt,
                f"{role}_reasoning_effort": runtime.reasoning_effort,
                f"{role}_verbosity": runtime.verbosity,
                f"{role}_max_output_tokens": runtime.max_output_tokens,
            }
        )
    return values


def enrichment_invoke_config(
    context: JobTraceContext,
    *,
    graph_version: str,
    callbacks: Any,
    coverage_summary: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    metadata = enrichment_trace_metadata(
        context,
        graph_version=graph_version,
        coverage_summary=coverage_summary,
    )
    config: dict[str, Any] = {
        "configurable": {"thread_id": context.thread_id},
        "run_name": "dreamland-enrichment-proposal-graph",
        "metadata": metadata,
        "tags": enrichment_trace_tags(
            graph_version,
            mode=_context_mode(context),
        ),
    }
    if callbacks:
        config["callbacks"] = callbacks
    return config


def enrichment_trace_metadata(
    context: JobTraceContext,
    *,
    graph_version: str,
    coverage_summary: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build shared outer-job and proposal-graph trace metadata."""

    metadata: dict[str, object] = {
        "dreamland.enrichment.graph_version": graph_version,
        "dreamland.enrichment.mode": _context_mode(context),
        "dreamland.enrichment.job_id": context.job_id,
        "dreamland.enrichment.trigger_id": context.trigger_id,
        "dreamland.enrichment.event_id": context.event_id,
        "dreamland.enrichment.target_host": context.target_host,
        "langfuse_session_id": context.langfuse_session_id or context.session_id,
        "langfuse_trace_name": f"dreamland:enrichment:{context.target_host}",
    }
    optional_metadata = {
        "dreamland.enrichment.correlation_id": context.correlation_id,
        "dreamland.enrichment.caused_by_event_id": context.caused_by_event_id,
        "dreamland.enrichment.command_id": context.command_id,
        "dreamland.enrichment.surface_hint": context.surface_hint,
        "langfuse_user_id": context.username,
        "dreamland.state_profile.name": context.profile_name,
    }
    metadata.update(
        {key: value for key, value in optional_metadata.items() if value is not None}
    )
    metadata.update(public_trace_metadata(context))
    if coverage_summary is not None:
        plain_coverage = json.loads(canonical_json(dict(coverage_summary)))
        metadata["dreamland.enrichment.coverage_summary"] = plain_coverage
        metadata["dreamland.enrichment.coverage_summary_sha256"] = hashlib.sha256(
            canonical_json(plain_coverage).encode("utf-8")
        ).hexdigest()
    return metadata


def enrichment_trace_tags(
    graph_version: str,
    *,
    mode: str | None = None,
) -> list[str]:
    """Return stable tags shared by the job trace and proposal subgraph."""

    tags = ["dreamland", "enrichment", graph_version]
    if mode is not None:
        tags.append(f"enrichment-mode:{mode}")
    return tags


def _context_mode(context: JobTraceContext) -> str:
    value = getattr(context, "mode", EnrichmentMode.FOCUSED)
    return EnrichmentMode(value).value


def parse_coordinator_decision(text: str) -> CoordinatorDecision:
    payload = _strict_json_object(text, role="coordinator")
    _require_exact_keys(
        payload,
        {
            "v",
            "status",
            "surface",
            "reason",
            "expertise",
            "expert_task",
            "anchors",
        },
        role="coordinator",
    )
    _require_schema_version(payload["v"], role="coordinator", expected=2)
    status = _require_choice(
        payload["status"], {"work", "no_op"}, "$.status", role="coordinator"
    )
    reason = _require_text(payload["reason"], "$.reason", role="coordinator")
    anchors = _require_text_array(
        payload["anchors"],
        "$.anchors",
        role="coordinator",
        maximum=MAX_COORDINATOR_ANCHORS,
    )
    if status == "work":
        surface = _require_choice(
            payload["surface"],
            set(SUPPORTED_SURFACES),
            "$.surface",
            role="coordinator",
        )
        expertise = _parse_expertise_scope(payload["expertise"])
        expert_task = _require_text(
            payload["expert_task"], "$.expert_task", role="coordinator"
        )
    else:
        if (
            payload["surface"] is not None
            or payload["expertise"] is not None
            or payload["expert_task"] is not None
        ):
            raise RoleOutputValidationError(
                "no_op requires null surface, expertise, and expert_task",
                reason_code="invalid_no_op",
                location="$",
            )
        if anchors:
            raise RoleOutputValidationError(
                "no_op requires an empty anchors array",
                reason_code="invalid_no_op",
                location="$.anchors",
            )
        surface = None
        expertise = None
        expert_task = None
    return CoordinatorDecision(
        status=cast(CoordinatorStatus, status),
        surface=surface,
        reason=reason,
        expertise=expertise,
        expert_task=expert_task,
        anchors=anchors,
    )


def parse_expert_report(
    text: str,
    *,
    reference_ids: Sequence[str] | None = None,
    max_items: int | None = None,
    max_relations: int | None = None,
) -> ExpertReport:
    """Parse the strict public contract without repairing model output."""

    return _parse_expert_report(
        text,
        reference_ids=reference_ids,
        max_items=max_items,
        max_relations=max_relations,
        salvage_invalid_relations=False,
    ).report


def parse_expert_report_with_relation_salvage(
    text: str,
    *,
    reference_ids: Sequence[str] | None = None,
    max_items: int | None = None,
    max_relations: int | None = None,
) -> ExpertReportParseResult:
    """Preserve a valid report while dropping invalid, duplicate, or excess relations."""

    return _parse_expert_report(
        text,
        reference_ids=reference_ids,
        max_items=max_items,
        max_relations=max_relations,
        salvage_invalid_relations=True,
    )


def _parse_expert_report(
    text: str,
    *,
    reference_ids: Sequence[str] | None,
    max_items: int | None,
    max_relations: int | None,
    salvage_invalid_relations: bool,
) -> ExpertReportParseResult:
    payload = _strict_json_object(text, role="expert")
    expected_keys = {
        "v",
        "status",
        "reason",
        "conditions",
        "items",
        "gates",
        "relations",
    }
    _require_exact_keys(payload, expected_keys, role="expert")
    _require_schema_version(payload["v"], role="expert", expected=2)
    status = _require_choice(
        payload["status"], {"ok", "insufficient"}, "$.status", role="expert"
    )
    if status == "ok":
        if payload["reason"] is not None:
            raise RoleOutputValidationError(
                "ok requires a null reason",
                reason_code="invalid_ok",
                location="$.reason",
            )
        reason = None
    else:
        reason = _require_bounded_text(
            payload["reason"],
            "$.reason",
            role="expert",
            maximum=MAX_EXPERT_REASON_TEXT,
        )
    conditions = _parse_conditions(payload["conditions"])
    items = _parse_items(
        payload["items"],
        reference_ids=reference_ids,
        maximum=_expert_item_limit(max_items),
    )
    gates = _parse_gates(payload["gates"], conditions=conditions, items=items)
    relations, rejected_relations = _parse_and_validate_relations(
        payload["relations"],
        items=items,
        reference_ids=reference_ids,
        maximum=_expert_relation_limit(max_relations),
        salvage_invalid=(salvage_invalid_relations and status == "ok"),
    )
    if status == "ok" and not items:
        raise RoleOutputValidationError(
            "ok requires at least one item",
            reason_code="missing_items",
            location="$.items",
        )
    if status == "insufficient" and (conditions or items or gates or relations):
        raise RoleOutputValidationError(
            "insufficient requires every array to be empty",
            reason_code="invalid_insufficient",
            location="$",
        )
    return ExpertReportParseResult(
        report=ExpertReport(
            status=cast(ExpertStatus, status),
            reason=reason,
            conditions=conditions,
            items=items,
            gates=gates,
            relations=relations,
        ),
        rejected_relations=rejected_relations,
    )


def _parse_conditions(value: object) -> tuple[ExpertCondition, ...]:
    values = _require_bounded_list(value, "$.conditions", role="expert")
    result: list[ExpertCondition] = []
    for index, item in enumerate(values):
        pair = _require_positional(item, 2, f"$.conditions[{index}]")
        result.append(
            ExpertCondition(
                id=_require_text(pair[0], f"$.conditions[{index}][0]", role="expert"),
                description=_require_text(
                    pair[1], f"$.conditions[{index}][1]", role="expert"
                ),
            )
        )
    _require_unique([item.id for item in result], "$.conditions")
    return tuple(result)


def _parse_expertise_scope(value: object) -> ExpertiseScope:
    if not isinstance(value, dict):
        raise RoleOutputValidationError(
            "coordinator expertise must be an object",
            reason_code="invalid_type",
            location="$.expertise",
        )
    expertise = cast(dict[str, object], value)
    _require_exact_keys(
        expertise,
        {"domain", "perspective"},
        role="coordinator expertise",
    )
    return ExpertiseScope(
        domain=_require_bounded_text(
            expertise["domain"],
            "$.expertise.domain",
            role="coordinator",
            maximum=MAX_EXPERTISE_TEXT,
        ),
        perspective=_require_bounded_text(
            expertise["perspective"],
            "$.expertise.perspective",
            role="coordinator",
            maximum=MAX_EXPERTISE_TEXT,
        ),
    )


def _parse_items(
    value: object,
    *,
    reference_ids: Sequence[str] | None,
    maximum: int,
) -> tuple[ExpertItem, ...]:
    values = _require_bounded_list(
        value,
        "$.items",
        role="expert",
        maximum=maximum,
    )
    supplied_reference_ids = set(reference_ids) if reference_ids is not None else None
    result: list[ExpertItem] = []
    for index, value_item in enumerate(values):
        location = f"$.items[{index}]"
        if not isinstance(value_item, dict):
            raise RoleOutputValidationError(
                "expected object",
                reason_code="invalid_type",
                location=location,
            )
        item = cast(dict[str, object], value_item)
        _require_exact_keys(
            item,
            {"id", "kind", "name", "detail", "origin", "basis"},
            role="expert",
        )
        origin = _require_choice(
            item["origin"],
            {"derived", "selected"},
            f"{location}.origin",
            role="expert",
        )
        basis = _require_text_array(
            item["basis"],
            f"{location}.basis",
            role="expert",
            maximum=MAX_EXPERT_AUXILIARY_ENTRIES,
        )
        for basis_index, reference_id in enumerate(basis):
            basis_location = f"{location}.basis[{basis_index}]"
            if reference_id.startswith("@"):
                raise RoleOutputValidationError(
                    "basis reference IDs must not use the relation prefix",
                    reason_code="invalid_reference",
                    location=basis_location,
                )
            if (
                supplied_reference_ids is not None
                and reference_id not in supplied_reference_ids
            ):
                raise RoleOutputValidationError(
                    "basis contains an unavailable reference",
                    reason_code="invalid_reference",
                    location=basis_location,
                )
        item_id = _require_text(item["id"], f"{location}.id", role="expert")
        if item_id.startswith("@"):
            raise RoleOutputValidationError(
                "item IDs must not use the external-reference prefix",
                reason_code="invalid_id",
                location=f"{location}.id",
            )
        result.append(
            ExpertItem(
                id=item_id,
                kind=_require_text(item["kind"], f"{location}.kind", role="expert"),
                name=_require_text(item["name"], f"{location}.name", role="expert"),
                detail=_require_text(
                    item["detail"], f"{location}.detail", role="expert"
                ),
                origin=cast(ExpertOrigin, origin),
                basis=basis,
            )
        )
    _require_unique([item.id for item in result], "$.items")
    return tuple(result)


def _parse_gates(
    value: object,
    *,
    conditions: tuple[ExpertCondition, ...],
    items: tuple[ExpertItem, ...],
) -> tuple[ExpertGate, ...]:
    values = _require_bounded_list(value, "$.gates", role="expert")
    condition_ids = {item.id for item in conditions}
    item_ids = {item.id for item in items}
    result: list[ExpertGate] = []
    for index, value_item in enumerate(values):
        location = f"$.gates[{index}]"
        pair = _require_positional(value_item, 2, location)
        item_id = _require_text(pair[0], f"{location}[0]", role="expert")
        references = _require_text_array(
            pair[1],
            f"{location}[1]",
            role="expert",
            maximum=MAX_EXPERT_AUXILIARY_ENTRIES,
        )
        if not references:
            raise RoleOutputValidationError(
                "gate must reference at least one condition",
                reason_code="empty_gate",
                location=f"{location}[1]",
            )
        if item_id not in item_ids or any(
            item not in condition_ids for item in references
        ):
            raise RoleOutputValidationError(
                "gate references an unknown item or condition",
                reason_code="invalid_reference",
                location=location,
            )
        result.append(ExpertGate(item_id=item_id, condition_ids=references))
    _require_unique([item.item_id for item in result], "$.gates")
    return tuple(result)


def _parse_and_validate_relations(
    value: object,
    *,
    items: Sequence[ExpertItem],
    reference_ids: Sequence[str] | None,
    maximum: int,
    salvage_invalid: bool,
) -> tuple[tuple[ExpertRelation, ...], tuple[ExpertRelationRejection, ...]]:
    values = (
        _require_list(value, "$.relations", role="expert")
        if salvage_invalid
        else _require_bounded_list(
            value,
            "$.relations",
            role="expert",
            maximum=maximum,
        )
    )
    result: list[ExpertRelation] = []
    rejected: list[ExpertRelationRejection] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(values):
        try:
            triple = _require_positional(item, 3, f"$.relations[{index}]")
            relation = ExpertRelation(
                subject=_require_text(
                    triple[0], f"$.relations[{index}][0]", role="expert"
                ),
                predicate=_require_text(
                    triple[1], f"$.relations[{index}][1]", role="expert"
                ),
                object=_require_text(
                    triple[2], f"$.relations[{index}][2]", role="expert"
                ),
            )
            _validate_relation_references(
                relation,
                index=index,
                items=items,
                reference_ids=reference_ids,
            )
        except RoleOutputValidationError as exc:
            if not salvage_invalid:
                raise
            rejected.append(
                ExpertRelationRejection(
                    index=index,
                    rule=getattr(exc, "relation_rule", exc.reason_code),
                    reason_code=exc.reason_code,
                    location=exc.location,
                )
            )
            continue
        identity = (relation.subject, relation.predicate, relation.object)
        if identity in seen:
            if not salvage_invalid:
                raise RoleOutputValidationError(
                    "duplicate relations are not allowed",
                    reason_code="duplicate_relation",
                    location=f"$.relations[{index}]",
                )
            rejected.append(
                ExpertRelationRejection(
                    index=index,
                    rule="duplicate_relation",
                    reason_code="duplicate_relation",
                    location=f"$.relations[{index}]",
                )
            )
            continue
        seen.add(identity)
        if len(result) >= maximum:
            if not salvage_invalid:
                raise RoleOutputValidationError(
                    f"expert array exceeds {maximum} entries",
                    reason_code="limit_exceeded",
                    location="$.relations",
                )
            rejected.append(
                ExpertRelationRejection(
                    index=index,
                    rule="relation_limit_exceeded",
                    reason_code="limit_exceeded",
                    location=f"$.relations[{index}]",
                )
            )
            continue
        result.append(relation)
    return tuple(result), tuple(rejected)


def _validate_relation_references(
    relation: ExpertRelation,
    *,
    index: int,
    items: Sequence[ExpertItem],
    reference_ids: Sequence[str] | None,
) -> None:
    items_by_id = {item.id: item for item in items}
    external_ids = set(reference_ids) if reference_ids is not None else None
    endpoints = ((0, relation.subject), (2, relation.object))
    item_endpoints = [
        endpoint for _offset, endpoint in endpoints if endpoint in items_by_id
    ]
    reference_endpoints: list[tuple[int, str]] = []
    for offset, endpoint in endpoints:
        if endpoint in items_by_id:
            continue
        if not endpoint.startswith("@") or not endpoint[1:]:
            raise _RelationValidationError(
                "relation endpoint is not an item or supplied reference",
                rule="unknown_endpoint",
                location=f"$.relations[{index}][{offset}]",
            )
        reference_id = endpoint[1:]
        if external_ids is not None and reference_id not in external_ids:
            raise _RelationValidationError(
                "relation endpoint is not an item or supplied reference",
                rule="unknown_endpoint",
                location=f"$.relations[{index}][{offset}]",
            )
        reference_endpoints.append((offset, reference_id))
    if not item_endpoints:
        raise _RelationValidationError(
            "a relation must involve at least one item",
            rule="item_endpoint_required",
            location=f"$.relations[{index}]",
        )
    if reference_endpoints:
        item = items_by_id[item_endpoints[0]]
        for offset, reference_id in reference_endpoints:
            if reference_id not in item.basis:
                raise _RelationValidationError(
                    "an item-to-reference relation must use the item's basis",
                    rule="reference_not_in_item_basis",
                    location=f"$.relations[{index}][{offset}]",
                )


def _expert_item_limit(max_items: int | None) -> int:
    if max_items is None:
        return MAX_EXPERT_CANDIDATES
    if type(max_items) is not int or not 1 <= max_items <= MAX_EXPERT_CANDIDATES:
        raise ValueError(
            f"max_items must be an integer from 1 to {MAX_EXPERT_CANDIDATES}"
        )
    return max_items


def _expert_relation_limit(max_relations: int | None) -> int:
    if max_relations is None:
        return MAX_EXPERT_RELATIONS
    if type(max_relations) is not int or not 1 <= max_relations <= MAX_EXPERT_RELATIONS:
        raise ValueError(
            f"max_relations must be an integer from 1 to {MAX_EXPERT_RELATIONS}"
        )
    return max_relations


def _strict_json_object(text: str, *, role: str) -> dict[str, object]:
    if not isinstance(text, str) or not text.strip():
        raise RoleOutputValidationError(
            "empty model response",
            reason_code="empty_output",
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RoleOutputValidationError(
            f"invalid JSON: {exc.msg}",
            reason_code="invalid_json",
            location=f"line {exc.lineno} column {exc.colno}",
        ) from exc
    if not isinstance(payload, dict):
        raise RoleOutputValidationError(
            f"{role} output must be an object",
            reason_code="invalid_type",
        )
    if any(not isinstance(key, str) for key in payload):
        raise RoleOutputValidationError(
            "object keys must be strings",
            reason_code="invalid_type",
        )
    return cast(dict[str, object], payload)


def _require_exact_keys(
    payload: Mapping[str, object], expected: set[str], *, role: str
) -> None:
    actual = set(payload)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    raise RoleOutputValidationError(
        f"{role} keys do not match contract; missing={missing!r} extra={extra!r}",
        reason_code="unexpected_keys",
    )


def _require_schema_version(value: object, *, role: str, expected: int) -> None:
    if type(value) is not int or value != expected:
        raise RoleOutputValidationError(
            f"unsupported {role} schema version",
            reason_code="unsupported_version",
            location="$.v",
        )


def _require_text(value: object, location: str, *, role: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoleOutputValidationError(
            f"{role} expected non-empty text",
            reason_code="invalid_text",
            location=location,
        )
    return value.strip()


def _require_bounded_text(
    value: object,
    location: str,
    *,
    role: str,
    maximum: int,
) -> str:
    text = _require_text(value, location, role=role)
    if len(text) > maximum:
        raise RoleOutputValidationError(
            f"{role} text exceeds {maximum} characters",
            reason_code="limit_exceeded",
            location=location,
        )
    return text


def _require_choice(
    value: object,
    choices: set[str],
    location: str,
    *,
    role: str,
) -> str:
    text = _require_text(value, location, role=role)
    if text not in choices:
        raise RoleOutputValidationError(
            f"{role} value must be one of {sorted(choices)!r}",
            reason_code="invalid_choice",
            location=location,
        )
    return text


def _require_bounded_list(
    value: object,
    location: str,
    *,
    role: str,
    maximum: int = MAX_EXPERT_AUXILIARY_ENTRIES,
) -> list[object]:
    values = _require_list(value, location, role=role)
    if len(values) > maximum:
        raise RoleOutputValidationError(
            f"{role} array exceeds {maximum} entries",
            reason_code="limit_exceeded",
            location=location,
        )
    return values


def _require_list(
    value: object,
    location: str,
    *,
    role: str,
) -> list[object]:
    if not isinstance(value, list):
        raise RoleOutputValidationError(
            f"{role} expected an array",
            reason_code="invalid_type",
            location=location,
        )
    return value


def _require_text_array(
    value: object,
    location: str,
    *,
    role: str,
    maximum: int,
) -> tuple[str, ...]:
    values = _require_bounded_list(value, location, role=role, maximum=maximum)
    result = tuple(
        _require_text(item, f"{location}[{index}]", role=role)
        for index, item in enumerate(values)
    )
    _require_unique(result, location)
    return result


def _require_positional(value: object, length: int, location: str) -> list[object]:
    if not isinstance(value, list) or len(value) != length:
        raise RoleOutputValidationError(
            f"expected an array of exactly {length} entries",
            reason_code="invalid_shape",
            location=location,
        )
    return value


def _require_unique(values: Sequence[str], location: str) -> None:
    if len(values) != len(set(values)):
        raise RoleOutputValidationError(
            "duplicate identifiers are not allowed",
            reason_code="duplicate_id",
            location=location,
        )


def _role_from_settings(
    role: str,
    settings: EnrichmentRoleSettings,
    api_key_resolver: ApiKeyResolver,
    model_factory: ModelFactory,
    *,
    lazy_model: bool,
) -> RoleRuntime:
    reference = settings.model_reference
    model_config = ChatModelProviderConfig(
        provider=reference.provider,
        model=reference.model,
        api_key=api_key_resolver(reference.provider),
        reasoning_effort=settings.reasoning_effort,
        reasoning_summary=settings.reasoning_summary,
        verbosity=settings.verbosity,
        speed=settings.speed,
        prompt_cache=settings.prompt_cache,
        max_output_tokens=settings.max_output_tokens,
        openai_api=settings.openai_api,
        openai_service_tier=settings.openai_service_tier,
        openai_reasoning_mode=settings.openai_reasoning_mode,
        streaming=False,
    )
    model = (
        LazyModel(model_factory, model_config)
        if lazy_model
        else model_factory(model_config)
    )
    return RoleRuntime(
        role=role,
        model=model,
        provider=reference.provider,
        model_name=reference.model,
        system_prompt=load_prompt(settings.system_prompt_name),
        reasoning_effort=settings.reasoning_effort,
        verbosity=settings.verbosity,
        max_output_tokens=settings.max_output_tokens,
        openai_api=settings.openai_api,
        openai_service_tier=settings.openai_service_tier,
        openai_reasoning_mode=settings.openai_reasoning_mode,
    )


def snapshot_document(
    snapshot: EnrichmentContextProjection | Mapping[str, object],
) -> dict[str, object]:
    if isinstance(snapshot, EnrichmentContextProjection):
        document: object = snapshot.document
    elif isinstance(snapshot, Mapping):
        document = snapshot
    else:
        raise TypeError("enrichment snapshot must be a mapping or context projection")
    try:
        plain = json.loads(canonical_json(document))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "enrichment snapshot must contain JSON-compatible data"
        ) from exc
    if not isinstance(plain, dict):
        raise ValueError("enrichment snapshot must be a JSON object")
    return cast(dict[str, object], plain)


def snapshot_sha256(
    snapshot: EnrichmentContextProjection | Mapping[str, object],
    document: Mapping[str, object],
) -> str:
    if isinstance(snapshot, EnrichmentContextProjection):
        return snapshot.projected_sha256
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def basis_host_from_snapshot(
    snapshot: Mapping[str, object], target_host: str
) -> dict[str, object]:
    world = snapshot.get("world", snapshot)
    if not isinstance(world, Mapping):
        raise ValueError("current snapshot world must be an object")
    hosts = world.get("hosts_by_name")
    if not isinstance(hosts, Mapping):
        raise ValueError("current snapshot has no host registry")
    host = hosts.get(target_host)
    if not isinstance(host, Mapping):
        raise ValueError("current snapshot target host is absent or invalid")
    return cast(dict[str, object], json.loads(canonical_json(host)))


def validated_text_tuple(
    values: Sequence[str], *, location: str, maximum: int
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{location} must be a sequence of strings")
    if len(values) > maximum:
        raise ValueError(f"{location} cannot exceed {maximum} entries")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{location} must contain non-empty strings")
        result.append(value.strip())
    if len(result) != len(set(result)):
        raise ValueError(f"{location} must not contain duplicate values")
    return tuple(result)


def require_nonempty_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def message_content_to_text(message: object) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(cast(str, item["text"]))
        return "\n".join(parts)
    return str(content)


__all__ = [
    "CoordinatorDecision",
    "CoordinatorStatus",
    "ExpertCondition",
    "ExpertiseScope",
    "ExpertGate",
    "ExpertItem",
    "ExpertOrigin",
    "ExpertRelation",
    "ExpertReport",
    "ExpertStatus",
    "MAX_COORDINATOR_ANCHORS",
    "MAX_EXPERTISE_TEXT",
    "RoleOutputValidationError",
    "RoleRuntime",
    "basis_host_from_snapshot",
    "configured_role_runtimes",
    "enrichment_invoke_config",
    "injected_role_runtimes",
    "message_content_to_text",
    "parse_coordinator_decision",
    "parse_expert_report",
    "require_nonempty_text",
    "role_runtime_constructor_kwargs",
    "snapshot_document",
    "snapshot_sha256",
    "validated_text_tuple",
]
