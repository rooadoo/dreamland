"""Deterministic, subtractive normalization for Integrator patch proposals."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Final, Literal

from dreamland.background_agents.enrichment.contracts import (
    EnrichmentContribution,
    EnrichmentPatch,
    StatePath,
)

INTEGRATOR_NORMALIZATION_POLICY_VERSION: Final[str] = (
    "integrator-output-normalization-v1"
)
MAX_RETAINED_NORMALIZATION_PATHS: Final[int] = 8

ContributionNormalizationStatus = Literal["unchanged", "normalized", "dropped"]


@dataclass(frozen=True, slots=True)
class IntegratorContributionNormalization:
    """One contribution's bounded structural normalization outcome."""

    cluster_index: int
    contribution_index: int
    contribution_id: str
    status: ContributionNormalizationStatus
    pruned_array_field_count: int = 0
    pruned_empty_object_count: int = 0
    pruned_array_paths: tuple[StatePath, ...] = ()
    pruned_empty_object_paths: tuple[StatePath, ...] = ()

    @property
    def structural_paths_truncated(self) -> bool:
        return self.pruned_array_field_count > len(
            self.pruned_array_paths
        ) or self.pruned_empty_object_count > len(self.pruned_empty_object_paths)


@dataclass(frozen=True, slots=True)
class IntegratorNormalizationStats:
    """Aggregate counts suitable for deterministic runtime logging."""

    received_contribution_count: int
    retained_contribution_count: int
    dropped_contribution_count: int
    array_affected_contribution_count: int
    pruned_array_field_count: int
    pruned_empty_object_count: int


@dataclass(frozen=True, slots=True)
class IntegratorNormalizationResult:
    """A normalized patch, or ``None`` when all supplied contributions pruned."""

    patch: EnrichmentPatch | None
    stats: IntegratorNormalizationStats
    contributions: tuple[IntegratorContributionNormalization, ...]
    policy_version: str = INTEGRATOR_NORMALIZATION_POLICY_VERSION

    @property
    def all_contributions_pruned(self) -> bool:
        return (
            self.stats.received_contribution_count > 0
            and self.stats.retained_contribution_count == 0
        )


def normalize_integrator_patch(patch: EnrichmentPatch) -> IntegratorNormalizationResult:
    """Remove unsupported array fields without adding or rewriting model content.

    Structural contract arrays such as clusters, dependencies, and source item IDs
    are untouched. Only array-valued fields inside ``contribution.value`` are
    removed. Object parents made empty solely by that pruning are removed as well.
    Dependencies, contribution order, IDs, operations, targets, provenance, and all
    retained scalar values remain unchanged.
    """

    decisions: list[IntegratorContributionNormalization] = []
    normalized_clusters = []
    received_count = 0
    retained_count = 0
    dropped_count = 0
    affected_count = 0
    array_field_count = 0
    empty_object_count = 0
    changed = False

    for cluster_index, cluster in enumerate(patch.clusters):
        normalized_contributions: list[EnrichmentContribution] = []
        for contribution_index, contribution in enumerate(cluster.contributions):
            received_count += 1
            normalized_value, array_paths, empty_object_paths = _prune_array_fields(
                contribution.value
            )
            array_field_count += len(array_paths)
            empty_object_count += len(empty_object_paths)
            if array_paths:
                affected_count += 1
            if array_paths and not normalized_value:
                changed = True
                dropped_count += 1
                decisions.append(
                    IntegratorContributionNormalization(
                        cluster_index=cluster_index,
                        contribution_index=contribution_index,
                        contribution_id=contribution.id,
                        status="dropped",
                        pruned_array_field_count=len(array_paths),
                        pruned_empty_object_count=len(empty_object_paths),
                        pruned_array_paths=array_paths[
                            :MAX_RETAINED_NORMALIZATION_PATHS
                        ],
                        pruned_empty_object_paths=empty_object_paths[
                            :MAX_RETAINED_NORMALIZATION_PATHS
                        ],
                    )
                )
                continue

            retained_count += 1
            status: ContributionNormalizationStatus = "unchanged"
            normalized_contribution = contribution
            if array_paths:
                changed = True
                status = "normalized"
                normalized_contribution = replace(
                    contribution,
                    value=normalized_value,
                )
            normalized_contributions.append(normalized_contribution)
            decisions.append(
                IntegratorContributionNormalization(
                    cluster_index=cluster_index,
                    contribution_index=contribution_index,
                    contribution_id=contribution.id,
                    status=status,
                    pruned_array_field_count=len(array_paths),
                    pruned_empty_object_count=len(empty_object_paths),
                    pruned_array_paths=array_paths[:MAX_RETAINED_NORMALIZATION_PATHS],
                    pruned_empty_object_paths=empty_object_paths[
                        :MAX_RETAINED_NORMALIZATION_PATHS
                    ],
                )
            )
        normalized_clusters.append(
            replace(cluster, contributions=tuple(normalized_contributions))
            if len(normalized_contributions) != len(cluster.contributions)
            or any(
                decision.status == "normalized"
                for decision in decisions
                if decision.cluster_index == cluster_index
            )
            else cluster
        )

    normalized_patch: EnrichmentPatch | None
    if received_count > 0 and retained_count == 0:
        normalized_patch = None
    elif changed:
        normalized_patch = replace(patch, clusters=tuple(normalized_clusters))
    else:
        normalized_patch = patch

    return IntegratorNormalizationResult(
        patch=normalized_patch,
        stats=IntegratorNormalizationStats(
            received_contribution_count=received_count,
            retained_contribution_count=retained_count,
            dropped_contribution_count=dropped_count,
            array_affected_contribution_count=affected_count,
            pruned_array_field_count=array_field_count,
            pruned_empty_object_count=empty_object_count,
        ),
        contributions=tuple(decisions),
    )


def _prune_array_fields(
    value: dict[str, object],
    prefix: StatePath = (),
) -> tuple[dict[str, object], tuple[StatePath, ...], tuple[StatePath, ...]]:
    normalized: dict[str, object] = {}
    array_paths: list[StatePath] = []
    empty_object_paths: list[StatePath] = []
    for key, child in value.items():
        path = (*prefix, key)
        if isinstance(child, (list, tuple)):
            array_paths.append(path)
            continue
        if isinstance(child, dict):
            normalized_child, child_arrays, child_empty_objects = _prune_array_fields(
                child,
                path,
            )
            array_paths.extend(child_arrays)
            empty_object_paths.extend(child_empty_objects)
            if child and child_arrays and not normalized_child:
                empty_object_paths.append(path)
                continue
            normalized[key] = normalized_child
            continue
        normalized[key] = deepcopy(child)
    return normalized, tuple(array_paths), tuple(empty_object_paths)


__all__ = [
    "INTEGRATOR_NORMALIZATION_POLICY_VERSION",
    "MAX_RETAINED_NORMALIZATION_PATHS",
    "IntegratorContributionNormalization",
    "IntegratorNormalizationResult",
    "IntegratorNormalizationStats",
    "normalize_integrator_patch",
]
