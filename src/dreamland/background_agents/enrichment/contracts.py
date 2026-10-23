from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal, TypeAlias, cast

from dreamland.background_agents.enrichment.limits import (
    MAX_SOURCE_ITEM_IDS_PER_CONTRIBUTION,
)

ENRICHMENT_PATCH_SCHEMA_VERSION = 2

StatePath: TypeAlias = tuple[str, ...]
EnrichmentOperation: TypeAlias = Literal["put_if_absent", "merge_missing"]
EnrichmentNoOpReasonCode: TypeAlias = Literal[
    "already_materialized",
    "no_safe_mapping",
    "no_supported_contribution",
]
EnrichmentDecisionStatus: TypeAlias = Literal[
    "applied",
    "already_satisfied",
    "skipped",
]
EnrichmentPatchStatus: TypeAlias = Literal[
    "applied",
    "already_satisfied",
    "partially_applied",
    "skipped",
]
EnrichmentReasonCode: TypeAlias = Literal[
    "applied",
    "already_satisfied",
    "partially_applied",
    "all_clusters_skipped",
    "cluster_atomicity",
    "dependency_changed",
    "dependency_basis_mismatch",
    "duplicate_contribution_target",
    "existing_value_conflict",
    "forbidden_array",
    "forbidden_directive",
    "forbidden_registry",
    "foreground_authentication_conflict",
    "invalid_cluster",
    "invalid_contribution",
    "invalid_patch",
    "semantic_authority_conflict",
    "target_missing",
    "touched_path_changed",
    "unsupported_operation",
    "wrong_parent_type",
]

EnrichmentAuthorityDomain: TypeAlias = Literal[
    "active_session",
    "foreground_evidence",
]


@dataclass(frozen=True, slots=True)
class EnrichmentAuthorityDependency:
    """A runtime-owned non-world dependency used for semantic race checks.

    Authority dependencies never cross the model-output contract. DREAMLAND
    attaches them after parsing an Integrator proposal and compares them with
    freshly captured live-session evidence during safe application.
    """

    domain: EnrichmentAuthorityDomain
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class EnrichmentDependency:
    """A host-relative fact the Integrator relied on when making its patch."""

    path: StatePath
    expected: object

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", tuple(self.path))
        object.__setattr__(self, "expected", deepcopy(self.expected))

    def to_payload(self) -> dict[str, object]:
        return {"path": list(self.path), "expected": deepcopy(self.expected)}


@dataclass(frozen=True, slots=True)
class EnrichmentContribution:
    """One additive change to an allowlisted host-local keyed registry."""

    id: str
    registry_path: StatePath
    key: str
    operation: EnrichmentOperation
    value: dict[str, object]
    source_item_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "registry_path", tuple(self.registry_path))
        object.__setattr__(self, "value", deepcopy(self.value))
        object.__setattr__(self, "source_item_ids", tuple(self.source_item_ids))

    @property
    def target_path(self) -> StatePath:
        return (*self.registry_path, self.key)

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "registry_path": list(self.registry_path),
            "key": self.key,
            "operation": self.operation,
            "value": deepcopy(self.value),
            "source_item_ids": list(self.source_item_ids),
        }


@dataclass(frozen=True, slots=True)
class EnrichmentCluster:
    """A causal group which is committed or skipped as one unit."""

    id: str
    contributions: tuple[EnrichmentContribution, ...]
    dependencies: tuple[EnrichmentDependency, ...] = ()
    authority_dependencies: tuple[EnrichmentAuthorityDependency, ...] = field(
        default=(),
        repr=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "contributions", tuple(self.contributions))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(
            self,
            "authority_dependencies",
            tuple(self.authority_dependencies),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "dependencies": [item.to_payload() for item in self.dependencies],
            "contributions": [item.to_payload() for item in self.contributions],
        }


@dataclass(frozen=True, slots=True)
class EnrichmentPatch:
    """An Integrator proposal tied to the host snapshot used to create it.

    ``basis_host`` and ``trusted_dependency_hashes`` are supplied by DREAMLAND
    rather than accepted from model output. This keeps the trusted projected
    basis and complete-value race fingerprints outside the Integrator's
    serialized response while still making application a deterministic pure
    operation.
    """

    target_host: str
    basis_host: dict[str, object]
    clusters: tuple[EnrichmentCluster, ...]
    schema_version: int = ENRICHMENT_PATCH_SCHEMA_VERSION
    trusted_dependency_hashes: tuple[tuple[StatePath, str], ...] = field(
        default=(),
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "basis_host", deepcopy(self.basis_host))
        object.__setattr__(self, "clusters", tuple(self.clusters))
        object.__setattr__(
            self,
            "trusted_dependency_hashes",
            tuple(
                (tuple(path), digest) for path, digest in self.trusted_dependency_hashes
            ),
        )

    def to_payload(self) -> dict[str, object]:
        """Return the compact model-facing form; the trusted basis is omitted."""
        return {
            "v": self.schema_version,
            "status": "patch",
            "target_host": self.target_host,
            "clusters": [cluster.to_payload() for cluster in self.clusters],
        }


@dataclass(frozen=True, slots=True)
class EnrichmentNoOp:
    """A typed Integrator decision that intentionally proposes no state change."""

    reason_code: EnrichmentNoOpReasonCode
    schema_version: int = ENRICHMENT_PATCH_SCHEMA_VERSION

    def to_payload(self) -> dict[str, object]:
        return {
            "v": self.schema_version,
            "status": "no_op",
            "reason_code": self.reason_code,
        }


EnrichmentIntegratorOutput: TypeAlias = EnrichmentPatch | EnrichmentNoOp


class EnrichmentPatchValidationError(ValueError):
    """A strict model-payload parsing failure with a stable logging code."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "invalid_patch",
        location: str = "$",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.location = location

    def to_log_fields(self) -> dict[str, str]:
        return {
            "reason_code": self.reason_code,
            "location": self.location,
            "detail": str(self),
        }


@dataclass(frozen=True, slots=True)
class EnrichmentContributionOutcome:
    contribution_id: str
    status: EnrichmentDecisionStatus
    reason_code: EnrichmentReasonCode
    target_path: StatePath
    touched_paths: tuple[StatePath, ...] = ()
    conflict_path: StatePath | None = None
    detail: str = ""

    def to_log_fields(self) -> dict[str, object]:
        return {
            "contribution_id": self.contribution_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "target_path": list(self.target_path),
            "touched_paths": [list(path) for path in self.touched_paths],
            "conflict_path": (
                list(self.conflict_path) if self.conflict_path is not None else None
            ),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class EnrichmentClusterOutcome:
    cluster_id: str
    status: EnrichmentDecisionStatus
    reason_code: EnrichmentReasonCode
    contributions: tuple[EnrichmentContributionOutcome, ...] = ()
    conflict_path: StatePath | None = None
    detail: str = ""

    def to_log_fields(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "conflict_path": (
                list(self.conflict_path) if self.conflict_path is not None else None
            ),
            "detail": self.detail,
            "contributions": [
                contribution.to_log_fields() for contribution in self.contributions
            ],
        }


@dataclass(frozen=True, slots=True)
class EnrichmentApplyOutcome:
    target_host: str
    status: EnrichmentPatchStatus
    reason_code: EnrichmentReasonCode
    clusters: tuple[EnrichmentClusterOutcome, ...] = ()

    def to_log_fields(self) -> dict[str, object]:
        return {
            "target_host": self.target_host,
            "status": self.status,
            "reason_code": self.reason_code,
            "cluster_count": len(self.clusters),
            "clusters": [cluster.to_log_fields() for cluster in self.clusters],
        }


@dataclass(frozen=True, slots=True)
class EnrichmentApplyResult:
    world_state: dict[str, object]
    outcome: EnrichmentApplyOutcome


def enrichment_patch_to_json(patch: EnrichmentPatch) -> str:
    return json.dumps(patch.to_payload(), separators=(",", ":"), sort_keys=True)


def enrichment_integrator_output_to_json(
    output: EnrichmentIntegratorOutput,
) -> str:
    return json.dumps(output.to_payload(), separators=(",", ":"), sort_keys=True)


def enrichment_integrator_output_from_json(
    text: str,
    *,
    basis_host: dict[str, object],
) -> EnrichmentIntegratorOutput:
    payload = _load_json(text)
    return enrichment_integrator_output_from_payload(
        payload,
        basis_host=basis_host,
    )


def enrichment_integrator_output_from_payload(
    payload: object,
    *,
    basis_host: dict[str, object],
) -> EnrichmentIntegratorOutput:
    root = _require_object(payload, "$")
    _require_supported_schema_version(root)
    if "status" not in root:
        raise EnrichmentPatchValidationError(
            "missing keys: status",
            location="$",
        )
    status = root["status"]
    if status == "patch":
        return enrichment_patch_from_payload(root, basis_host=basis_host)
    if status == "no_op":
        return enrichment_no_op_from_payload(root)
    raise EnrichmentPatchValidationError(
        f"unsupported enrichment Integrator status {status!r}",
        location="$.status",
    )


def enrichment_patch_from_json(
    text: str,
    *,
    basis_host: dict[str, object],
) -> EnrichmentPatch:
    payload = _load_json(text)
    return enrichment_patch_from_payload(payload, basis_host=basis_host)


def enrichment_patch_from_payload(
    payload: object,
    *,
    basis_host: dict[str, object],
) -> EnrichmentPatch:
    root = _require_object(payload, "$")
    _require_exact_keys(root, {"v", "status", "target_host", "clusters"}, "$")
    version = _require_supported_schema_version(root)
    if root["status"] != "patch":
        raise EnrichmentPatchValidationError(
            "an enrichment patch must have status 'patch'",
            location="$.status",
        )
    target_host = _require_nonempty_string(root["target_host"], "$.target_host")
    clusters_payload = _require_list(root["clusters"], "$.clusters")
    if not clusters_payload:
        raise EnrichmentPatchValidationError(
            "an enrichment patch must contain at least one cluster",
            location="$.clusters",
        )
    clusters = tuple(
        _cluster_from_payload(item, f"$.clusters[{index}]")
        for index, item in enumerate(clusters_payload)
    )
    cluster_ids = [cluster.id for cluster in clusters]
    if len(cluster_ids) != len(set(cluster_ids)):
        raise EnrichmentPatchValidationError(
            "cluster ids must be unique",
            location="$.clusters",
        )
    return EnrichmentPatch(
        target_host=target_host,
        basis_host=basis_host,
        clusters=clusters,
        schema_version=version,
    )


def enrichment_no_op_from_payload(payload: object) -> EnrichmentNoOp:
    root = _require_object(payload, "$")
    _require_exact_keys(root, {"v", "status", "reason_code"}, "$")
    version = _require_supported_schema_version(root)
    if root["status"] != "no_op":
        raise EnrichmentPatchValidationError(
            "an enrichment no-op must have status 'no_op'",
            location="$.status",
        )
    reason_code = root["reason_code"]
    if reason_code not in (
        "already_materialized",
        "no_safe_mapping",
        "no_supported_contribution",
    ):
        raise EnrichmentPatchValidationError(
            f"unsupported enrichment no-op reason code {reason_code!r}",
            location="$.reason_code",
        )
    return EnrichmentNoOp(
        reason_code=reason_code,
        schema_version=version,
    )


def _cluster_from_payload(payload: object, location: str) -> EnrichmentCluster:
    item = _require_object(payload, location)
    _require_exact_keys(item, {"id", "dependencies", "contributions"}, location)
    cluster_id = _require_nonempty_string(item["id"], f"{location}.id")
    dependencies_payload = _require_list(
        item["dependencies"], f"{location}.dependencies"
    )
    contributions_payload = _require_list(
        item["contributions"], f"{location}.contributions"
    )
    if not contributions_payload:
        raise EnrichmentPatchValidationError(
            "an enrichment cluster must contain at least one contribution",
            location=f"{location}.contributions",
        )
    dependencies = tuple(
        _dependency_from_payload(value, f"{location}.dependencies[{index}]")
        for index, value in enumerate(dependencies_payload)
    )
    contributions = tuple(
        _contribution_from_payload(value, f"{location}.contributions[{index}]")
        for index, value in enumerate(contributions_payload)
    )
    contribution_ids = [contribution.id for contribution in contributions]
    if len(contribution_ids) != len(set(contribution_ids)):
        raise EnrichmentPatchValidationError(
            "contribution ids must be unique within a cluster",
            location=f"{location}.contributions",
        )
    return EnrichmentCluster(
        id=cluster_id,
        dependencies=dependencies,
        contributions=contributions,
    )


def _dependency_from_payload(payload: object, location: str) -> EnrichmentDependency:
    item = _require_object(payload, location)
    _require_exact_keys(item, {"path", "expected"}, location)
    return EnrichmentDependency(
        path=_path_from_payload(item["path"], f"{location}.path"),
        expected=deepcopy(item["expected"]),
    )


def _contribution_from_payload(
    payload: object,
    location: str,
) -> EnrichmentContribution:
    item = _require_object(payload, location)
    _require_exact_keys(
        item,
        {
            "id",
            "registry_path",
            "key",
            "operation",
            "value",
            "source_item_ids",
        },
        location,
    )
    operation = item["operation"]
    if operation not in ("put_if_absent", "merge_missing"):
        raise EnrichmentPatchValidationError(
            f"unsupported enrichment operation {operation!r}",
            reason_code="unsupported_operation",
            location=f"{location}.operation",
        )
    value = _require_object(item["value"], f"{location}.value")
    if not value:
        raise EnrichmentPatchValidationError(
            "contribution value must not be empty",
            reason_code="invalid_contribution",
            location=f"{location}.value",
        )
    source_item_ids = _source_item_ids_from_payload(
        item["source_item_ids"],
        f"{location}.source_item_ids",
    )
    return EnrichmentContribution(
        id=_require_nonempty_string(item["id"], f"{location}.id"),
        registry_path=_path_from_payload(
            item["registry_path"], f"{location}.registry_path"
        ),
        key=_require_nonempty_string(item["key"], f"{location}.key"),
        operation=operation,
        value=deepcopy(value),
        source_item_ids=source_item_ids,
    )


def _source_item_ids_from_payload(
    payload: object,
    location: str,
) -> tuple[str, ...]:
    values = _require_list(payload, location)
    if not 1 <= len(values) <= MAX_SOURCE_ITEM_IDS_PER_CONTRIBUTION:
        raise EnrichmentPatchValidationError(
            "source_item_ids must contain between 1 and "
            f"{MAX_SOURCE_ITEM_IDS_PER_CONTRIBUTION} ids",
            reason_code="invalid_contribution",
            location=location,
        )
    normalized: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise EnrichmentPatchValidationError(
                "source item ids must be non-empty strings",
                reason_code="invalid_contribution",
                location=f"{location}[{index}]",
            )
        normalized.append(value.strip())
    source_item_ids = tuple(normalized)
    for index, source_item_id in enumerate(source_item_ids):
        if source_item_id.startswith("@"):
            raise EnrichmentPatchValidationError(
                "source item ids must not start with '@'",
                reason_code="invalid_contribution",
                location=f"{location}[{index}]",
            )
    if len(source_item_ids) != len(set(source_item_ids)):
        raise EnrichmentPatchValidationError(
            "source item ids must be unique",
            reason_code="invalid_contribution",
            location=location,
        )
    return source_item_ids


def _path_from_payload(payload: object, location: str) -> StatePath:
    values = _require_list(payload, location)
    if not values:
        raise EnrichmentPatchValidationError(
            "path must contain at least one segment",
            location=location,
        )
    path: list[str] = []
    for index, value in enumerate(values):
        path.append(_require_nonempty_string(value, f"{location}[{index}]"))
    return tuple(path)


def _require_object(payload: object, location: str) -> dict[str, object]:
    if not isinstance(payload, dict) or any(
        not isinstance(key, str) for key in payload
    ):
        raise EnrichmentPatchValidationError(
            "expected a JSON object",
            location=location,
        )
    return cast(dict[str, object], payload)


def _require_list(payload: object, location: str) -> list[object]:
    if not isinstance(payload, list):
        raise EnrichmentPatchValidationError(
            "expected a JSON array",
            location=location,
        )
    return cast(list[object], payload)


def _require_nonempty_string(payload: object, location: str) -> str:
    if not isinstance(payload, str) or not payload.strip():
        raise EnrichmentPatchValidationError(
            "expected a non-empty string",
            location=location,
        )
    return payload.strip()


def _require_exact_keys(
    payload: dict[str, object],
    expected: set[str],
    location: str,
) -> None:
    actual = set(payload)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing keys: {', '.join(missing)}")
    if extra:
        details.append(f"unknown keys: {', '.join(extra)}")
    raise EnrichmentPatchValidationError(
        "; ".join(details),
        location=location,
    )


def _load_json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise EnrichmentPatchValidationError(
            f"invalid JSON: {exc.msg}",
            location=f"line {exc.lineno} column {exc.colno}",
        ) from exc


def _require_supported_schema_version(payload: dict[str, object]) -> int:
    if "v" not in payload:
        raise EnrichmentPatchValidationError(
            "missing keys: v",
            location="$",
        )
    version = payload["v"]
    if type(version) is not int or version != ENRICHMENT_PATCH_SCHEMA_VERSION:
        raise EnrichmentPatchValidationError(
            f"unsupported enrichment patch schema version {version!r}",
            location="$.v",
        )
    return version
