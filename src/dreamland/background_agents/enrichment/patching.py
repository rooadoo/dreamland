from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from pathlib import PurePosixPath, PureWindowsPath

from dreamland.background_agents.enrichment.authentication import (
    ForegroundAuthenticationPaths,
)
from dreamland.background_agents.enrichment.contracts import (
    ENRICHMENT_PATCH_SCHEMA_VERSION,
    EnrichmentApplyOutcome,
    EnrichmentApplyResult,
    EnrichmentAuthorityDependency,
    EnrichmentCluster,
    EnrichmentClusterOutcome,
    EnrichmentContribution,
    EnrichmentContributionOutcome,
    EnrichmentDecisionStatus,
    EnrichmentDependency,
    EnrichmentPatch,
    EnrichmentPatchStatus,
    EnrichmentReasonCode,
    StatePath,
)
from dreamland.background_agents.enrichment.limits import (
    MAX_PATCH_CONTRIBUTIONS,
    MAX_SOURCE_ITEM_IDS_PER_CONTRIBUTION,
)

ALLOWED_HOST_REGISTRY_PATHS: frozenset[StatePath] = frozenset(
    {
        ("services_by_name",),
        ("filesystem", "artifacts_by_path"),
        ("users_by_username",),
        ("groups_by_name",),
        ("facts_by_id",),
    }
)
_WORLD_STATE_DIRECTIVES = frozenset({"__dreamland_delete__", "__dreamland_replace__"})
_MISSING = object()


class _Lookup:
    __slots__ = ("exists", "value", "wrong_parent")

    def __init__(
        self,
        *,
        exists: bool,
        value: object = _MISSING,
        wrong_parent: StatePath | None = None,
    ) -> None:
        self.exists = exists
        self.value = value
        self.wrong_parent = wrong_parent


def apply_enrichment_patch(
    current_world_state: dict[str, object],
    patch: EnrichmentPatch,
    *,
    current_authority_sha256_by_domain: Mapping[str, str] | None = None,
    protected_authentication_paths: ForegroundAuthenticationPaths | None = None,
) -> EnrichmentApplyResult:
    """Apply safe host-local enrichment without mutating either input.

    Each cluster is evaluated against its Integrator basis and committed to a
    private copy as one unit. A conflict in any contribution skips that cluster;
    unrelated changes elsewhere in the host or world do not affect it.
    """
    next_world_state = deepcopy(current_world_state)
    global_failure = _global_failure(next_world_state, patch)
    if global_failure is not None:
        reason_code, path, detail = global_failure
        clusters = tuple(
            _globally_skipped_cluster(cluster, patch, reason_code, path, detail)
            for cluster in patch.clusters
        )
        return EnrichmentApplyResult(
            world_state=next_world_state,
            outcome=EnrichmentApplyOutcome(
                target_host=patch.target_host,
                status="skipped",
                reason_code=reason_code,
                clusters=clusters,
            ),
        )

    hosts = next_world_state["hosts_by_name"]
    assert isinstance(hosts, dict)
    host = hosts[patch.target_host]
    assert isinstance(host, dict)
    cluster_outcomes: list[EnrichmentClusterOutcome] = []
    for cluster in patch.clusters:
        next_host, outcome = _apply_cluster(
            host,
            patch,
            cluster,
            current_authority_sha256_by_domain=(
                current_authority_sha256_by_domain or {}
            ),
            protected_authentication_paths=protected_authentication_paths,
        )
        if outcome.status == "applied":
            hosts[patch.target_host] = next_host
            host = next_host
        cluster_outcomes.append(outcome)

    patch_status, patch_reason = _aggregate_outcome(cluster_outcomes)
    return EnrichmentApplyResult(
        world_state=next_world_state,
        outcome=EnrichmentApplyOutcome(
            target_host=patch.target_host,
            status=patch_status,
            reason_code=patch_reason,
            clusters=tuple(cluster_outcomes),
        ),
    )


def _global_failure(
    world_state: dict[str, object],
    patch: EnrichmentPatch,
) -> tuple[EnrichmentReasonCode, StatePath | None, str] | None:
    if (
        patch.schema_version != ENRICHMENT_PATCH_SCHEMA_VERSION
        or not isinstance(patch.target_host, str)
        or not patch.target_host.strip()
        or not isinstance(patch.basis_host, dict)
        or not patch.clusters
    ):
        return "invalid_patch", None, "patch contract is incomplete or unsupported"
    if any(not isinstance(item, EnrichmentCluster) for item in patch.clusters):
        return "invalid_patch", None, "clusters must be typed and uniquely identified"
    for cluster in patch.clusters:
        if any(
            not isinstance(dependency, EnrichmentAuthorityDependency)
            or dependency.domain not in {"active_session", "foreground_evidence"}
            or not _valid_sha256(dependency.expected_sha256)
            for dependency in cluster.authority_dependencies
        ):
            return (
                "invalid_patch",
                None,
                "authority dependencies must be unique typed runtime fingerprints",
            )
        domains = [dependency.domain for dependency in cluster.authority_dependencies]
        if len(domains) != len(set(domains)):
            return (
                "invalid_patch",
                None,
                "authority dependencies must be unique typed runtime fingerprints",
            )
    trusted_hash_paths = [path for path, _digest in patch.trusted_dependency_hashes]
    if any(
        not path
        or any(not isinstance(segment, str) or not segment for segment in path)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for path, digest in patch.trusted_dependency_hashes
    ):
        return "invalid_patch", None, "patch contract is incomplete or unsupported"
    if len(trusted_hash_paths) != len(set(trusted_hash_paths)):
        return "invalid_patch", None, "patch contract is incomplete or unsupported"
    if patch.trusted_dependency_hashes:
        dependencies = [
            dependency
            for cluster in patch.clusters
            for dependency in cluster.dependencies
        ]
        if any(
            not isinstance(dependency, EnrichmentDependency)
            or not dependency.path
            or any(
                not isinstance(segment, str) or not segment
                for segment in dependency.path
            )
            for dependency in dependencies
        ):
            return "invalid_patch", None, "patch contract is incomplete or unsupported"
        dependency_paths = {
            dependency.path
            for dependency in dependencies
            if isinstance(dependency, EnrichmentDependency)
        }
        if set(trusted_hash_paths) != dependency_paths:
            return "invalid_patch", None, "patch contract is incomplete or unsupported"
    hosts = world_state.get("hosts_by_name", _MISSING)
    if hosts is _MISSING:
        return (
            "target_missing",
            ("hosts_by_name", patch.target_host),
            "world state has no host registry",
        )
    if not isinstance(hosts, dict):
        return (
            "wrong_parent_type",
            ("hosts_by_name",),
            "hosts_by_name must be an object",
        )
    if patch.target_host not in hosts:
        return (
            "target_missing",
            ("hosts_by_name", patch.target_host),
            "target host no longer exists",
        )
    if not isinstance(hosts[patch.target_host], dict):
        return (
            "wrong_parent_type",
            ("hosts_by_name", patch.target_host),
            "target host must be an object",
        )
    cluster_ids = [cluster.id for cluster in patch.clusters]
    if len(cluster_ids) != len(set(cluster_ids)):
        return "invalid_patch", None, "clusters must be typed and uniquely identified"
    return None


def _globally_skipped_cluster(
    cluster: EnrichmentCluster,
    patch: EnrichmentPatch,
    reason_code: EnrichmentReasonCode,
    conflict_path: StatePath | None,
    detail: str,
) -> EnrichmentClusterOutcome:
    contributions = tuple(
        EnrichmentContributionOutcome(
            contribution_id=contribution.id,
            status="skipped",
            reason_code=reason_code,
            target_path=_absolute_contribution_path(patch, contribution),
            conflict_path=conflict_path,
            detail=detail,
        )
        for contribution in cluster.contributions
    )
    return EnrichmentClusterOutcome(
        cluster_id=cluster.id,
        status="skipped",
        reason_code=reason_code,
        contributions=contributions,
        conflict_path=conflict_path,
        detail=detail,
    )


def _apply_cluster(
    current_host: dict[str, object],
    patch: EnrichmentPatch,
    cluster: EnrichmentCluster,
    *,
    current_authority_sha256_by_domain: Mapping[str, str],
    protected_authentication_paths: ForegroundAuthenticationPaths | None,
) -> tuple[dict[str, object], EnrichmentClusterOutcome]:
    invalid = _validate_cluster(cluster, patch)
    if invalid is not None:
        return deepcopy(current_host), invalid

    if protected_authentication_paths is not None:
        for contribution in cluster.contributions:
            if contribution.registry_path == ("filesystem", "artifacts_by_path") and (
                protected_authentication_paths.protects(contribution.key)
            ):
                return deepcopy(current_host), _dependency_failure(
                    cluster,
                    patch,
                    "foreground_authentication_conflict",
                    _absolute_contribution_path(patch, contribution),
                    "SSH client authentication artifact is reserved to foreground control",
                )

    authority_failure = _check_authority_dependencies(
        patch,
        cluster,
        current_authority_sha256_by_domain,
    )
    if authority_failure is not None:
        return deepcopy(current_host), authority_failure

    dependency_failure = _check_dependencies(
        patch.basis_host,
        current_host,
        patch,
        cluster,
    )
    if dependency_failure is not None:
        return deepcopy(current_host), dependency_failure

    candidate = deepcopy(current_host)
    outcomes: list[EnrichmentContributionOutcome] = []
    for index, contribution in enumerate(cluster.contributions):
        contribution_outcome = _apply_contribution(
            candidate,
            current_host,
            patch.basis_host,
            patch,
            contribution,
        )
        outcomes.append(contribution_outcome)
        if contribution_outcome.status != "skipped":
            continue

        atomic_outcomes = [
            _atomicity_skip(outcome, cluster.id)
            if outcome.status != "skipped"
            else outcome
            for outcome in outcomes
        ]
        atomic_outcomes.extend(
            EnrichmentContributionOutcome(
                contribution_id=remaining.id,
                status="skipped",
                reason_code="cluster_atomicity",
                target_path=_absolute_contribution_path(patch, remaining),
                detail=(
                    f"cluster {cluster.id!r} was skipped after another "
                    "contribution conflicted"
                ),
            )
            for remaining in cluster.contributions[index + 1 :]
        )
        return deepcopy(current_host), EnrichmentClusterOutcome(
            cluster_id=cluster.id,
            status="skipped",
            reason_code=contribution_outcome.reason_code,
            contributions=tuple(atomic_outcomes),
            conflict_path=contribution_outcome.conflict_path,
            detail=contribution_outcome.detail,
        )

    changed = any(outcome.status == "applied" for outcome in outcomes)
    status: EnrichmentDecisionStatus = "applied" if changed else "already_satisfied"
    reason: EnrichmentReasonCode = "applied" if changed else "already_satisfied"
    return candidate, EnrichmentClusterOutcome(
        cluster_id=cluster.id,
        status=status,
        reason_code=reason,
        contributions=tuple(outcomes),
    )


def _validate_cluster(
    cluster: EnrichmentCluster,
    patch: EnrichmentPatch,
) -> EnrichmentClusterOutcome | None:
    if (
        not isinstance(cluster.id, str)
        or not cluster.id.strip()
        or not cluster.contributions
    ):
        return _cluster_validation_failure(
            cluster,
            patch,
            "invalid_cluster",
            "cluster must have an id and at least one contribution",
        )
    if len(cluster.contributions) > MAX_PATCH_CONTRIBUTIONS:
        return _cluster_validation_failure(
            cluster,
            patch,
            "invalid_cluster",
            f"cluster cannot exceed {MAX_PATCH_CONTRIBUTIONS} contributions",
        )
    if any(
        not isinstance(dependency, EnrichmentDependency)
        or not dependency.path
        or any(
            not isinstance(segment, str) or not segment for segment in dependency.path
        )
        for dependency in cluster.dependencies
    ):
        return _cluster_validation_failure(
            cluster,
            patch,
            "invalid_cluster",
            "dependency paths must be non-empty host-relative string paths",
        )
    contribution_ids = [contribution.id for contribution in cluster.contributions]
    if any(
        not isinstance(contribution, EnrichmentContribution)
        for contribution in cluster.contributions
    ) or len(contribution_ids) != len(set(contribution_ids)):
        return _cluster_validation_failure(
            cluster,
            patch,
            "invalid_cluster",
            "contributions must be typed and uniquely identified",
        )
    if _has_incompatible_repeated_targets(cluster.contributions):
        return _cluster_validation_failure(
            cluster,
            patch,
            "duplicate_contribution_target",
            (
                "repeated registry targets require merge_missing contributions "
                "with pairwise disjoint desired leaf paths"
            ),
        )
    return None


def _check_authority_dependencies(
    patch: EnrichmentPatch,
    cluster: EnrichmentCluster,
    current_authority_sha256_by_domain: Mapping[str, str],
) -> EnrichmentClusterOutcome | None:
    """Reject a cluster when its runtime-owned session evidence went stale."""

    for dependency in cluster.authority_dependencies:
        if (
            current_authority_sha256_by_domain.get(dependency.domain)
            == dependency.expected_sha256
        ):
            continue
        return _dependency_failure(
            cluster,
            patch,
            "semantic_authority_conflict",
            None,
            f"{dependency.domain} changed after the Integrator snapshot",
        )
    return None


def _has_incompatible_repeated_targets(
    contributions: tuple[EnrichmentContribution, ...],
) -> bool:
    grouped: dict[StatePath, list[EnrichmentContribution]] = {}
    for contribution in contributions:
        grouped.setdefault(contribution.target_path, []).append(contribution)
    return any(
        len(group) > 1 and not _repeated_merge_target_is_disjoint(group)
        for group in grouped.values()
    )


def _repeated_merge_target_is_disjoint(
    contributions: list[EnrichmentContribution],
) -> bool:
    if any(
        contribution.operation != "merge_missing"
        or not isinstance(contribution.value, dict)
        or not contribution.value
        for contribution in contributions
    ):
        return False

    prior_paths: list[StatePath] = []
    for contribution in contributions:
        desired_paths = [
            path for path, _value in _iter_desired_leaves(contribution.value)
        ]
        if any(
            _paths_overlap(path, prior_path)
            for path in desired_paths
            for prior_path in prior_paths
        ):
            return False
        prior_paths.extend(desired_paths)
    return True


def _paths_overlap(left: StatePath, right: StatePath) -> bool:
    shared_length = min(len(left), len(right))
    return left[:shared_length] == right[:shared_length]


def _cluster_validation_failure(
    cluster: EnrichmentCluster,
    patch: EnrichmentPatch,
    reason_code: EnrichmentReasonCode,
    detail: str,
) -> EnrichmentClusterOutcome:
    return EnrichmentClusterOutcome(
        cluster_id=cluster.id,
        status="skipped",
        reason_code=reason_code,
        contributions=tuple(
            EnrichmentContributionOutcome(
                contribution_id=contribution.id,
                status="skipped",
                reason_code=reason_code,
                target_path=_absolute_contribution_path(patch, contribution),
                detail=detail,
            )
            for contribution in cluster.contributions
        ),
        detail=detail,
    )


def _check_dependencies(
    basis_host: dict[str, object],
    current_host: dict[str, object],
    patch: EnrichmentPatch,
    cluster: EnrichmentCluster,
) -> EnrichmentClusterOutcome | None:
    trusted_hashes = dict(patch.trusted_dependency_hashes)
    for dependency in cluster.dependencies:
        basis = _lookup(basis_host, dependency.path)
        current = _lookup(current_host, dependency.path)
        absolute_path = _absolute_host_path(patch, dependency.path)
        if basis.wrong_parent is not None:
            return _dependency_failure(
                cluster,
                patch,
                "wrong_parent_type",
                _absolute_host_path(patch, basis.wrong_parent),
                "dependency basis contains a non-object parent",
            )
        if not basis.exists or basis.value != dependency.expected:
            return _dependency_failure(
                cluster,
                patch,
                "dependency_basis_mismatch",
                absolute_path,
                "declared dependency does not match the Integrator basis",
            )
        if current.wrong_parent is not None:
            return _dependency_failure(
                cluster,
                patch,
                "wrong_parent_type",
                _absolute_host_path(patch, current.wrong_parent),
                "dependency path now has a non-object parent",
            )
        current_matches_projection = (
            current.exists
            and _projected_dependency_matches_current(
                dependency.expected,
                current.value,
            )
        )
        trusted_digest = trusted_hashes.get(dependency.path)
        current_matches_trusted_basis = current_matches_projection and (
            _json_value_sha256(current.value) == trusted_digest
            if trusted_digest is not None
            else current.value == basis.value
        )
        if not current_matches_trusted_basis:
            return _dependency_failure(
                cluster,
                patch,
                "dependency_changed",
                absolute_path,
                "declared dependency changed after the Integrator snapshot",
            )
    return None


def _json_value_sha256(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _projected_dependency_matches_current(expected: object, current: object) -> bool:
    """Compare every projected field without treating omitted fields as drift."""

    if isinstance(expected, dict):
        return isinstance(current, dict) and all(
            key in current
            and _projected_dependency_matches_current(value, current[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(current, list)
            and len(expected) == len(current)
            and all(
                _projected_dependency_matches_current(expected_item, current_item)
                for expected_item, current_item in zip(expected, current, strict=True)
            )
        )
    return current == expected


def _dependency_failure(
    cluster: EnrichmentCluster,
    patch: EnrichmentPatch,
    reason_code: EnrichmentReasonCode,
    conflict_path: StatePath | None,
    detail: str,
) -> EnrichmentClusterOutcome:
    return EnrichmentClusterOutcome(
        cluster_id=cluster.id,
        status="skipped",
        reason_code=reason_code,
        contributions=tuple(
            EnrichmentContributionOutcome(
                contribution_id=contribution.id,
                status="skipped",
                reason_code=reason_code,
                target_path=_absolute_contribution_path(patch, contribution),
                conflict_path=conflict_path,
                detail=detail,
            )
            for contribution in cluster.contributions
        ),
        conflict_path=conflict_path,
        detail=detail,
    )


def _apply_contribution(
    candidate: dict[str, object],
    current_host: dict[str, object],
    basis_host: dict[str, object],
    patch: EnrichmentPatch,
    contribution: EnrichmentContribution,
) -> EnrichmentContributionOutcome:
    validation_failure = _validate_contribution(patch, contribution)
    if validation_failure is not None:
        return validation_failure
    if contribution.operation == "put_if_absent":
        return _put_if_absent(
            candidate,
            current_host,
            basis_host,
            patch,
            contribution,
        )
    return _merge_missing(
        candidate,
        current_host,
        basis_host,
        patch,
        contribution,
    )


def _validate_contribution(
    patch: EnrichmentPatch,
    contribution: EnrichmentContribution,
) -> EnrichmentContributionOutcome | None:
    target = _absolute_contribution_path(patch, contribution)
    if contribution.registry_path not in ALLOWED_HOST_REGISTRY_PATHS:
        return _skipped_contribution(
            contribution,
            target,
            "forbidden_registry",
            "contribution does not target an allowlisted host-local registry",
        )
    if contribution.registry_path == (
        "filesystem",
        "artifacts_by_path",
    ) and not _artifact_path_key_is_admissible(contribution.key):
        return _skipped_contribution(
            contribution,
            target,
            "invalid_contribution",
            "filesystem artifact key uses recognized relative path syntax",
        )
    if contribution.operation not in ("put_if_absent", "merge_missing"):
        return _skipped_contribution(
            contribution,
            target,
            "unsupported_operation",
            "only put_if_absent and merge_missing are supported",
        )
    if (
        not isinstance(contribution.id, str)
        or not contribution.id.strip()
        or not isinstance(contribution.key, str)
        or not contribution.key.strip()
        or not isinstance(contribution.value, dict)
        or not contribution.value
        or not 1
        <= len(contribution.source_item_ids)
        <= MAX_SOURCE_ITEM_IDS_PER_CONTRIBUTION
        or any(
            not isinstance(source_item_id, str)
            or not source_item_id.strip()
            or source_item_id.strip().startswith("@")
            for source_item_id in contribution.source_item_ids
        )
        or len(contribution.source_item_ids)
        != len({item.strip() for item in contribution.source_item_ids})
    ):
        return _skipped_contribution(
            contribution,
            target,
            "invalid_contribution",
            (
                "contribution id, key, non-empty object value, and 1 to "
                f"{MAX_SOURCE_ITEM_IDS_PER_CONTRIBUTION} unique source item ids "
                "are required"
            ),
        )
    if contribution.key in _WORLD_STATE_DIRECTIVES:
        return _skipped_contribution(
            contribution,
            target,
            "forbidden_directive",
            "world-state directives are not valid enrichment keys",
        )
    value_failure = _validate_write_value(contribution.value)
    if value_failure is not None:
        reason_code, relative_path, detail = value_failure
        return _skipped_contribution(
            contribution,
            target,
            reason_code,
            detail,
            conflict_path=(*target, *relative_path),
        )
    return None


def _artifact_path_key_is_admissible(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    path = value.strip()
    if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
        return True
    if path in {".", "..", "~"} or path.startswith(
        ("./", ".\\", "../", "..\\", "~/", "~\\")
    ):
        return False
    if len(path) >= 2 and path[1] == ":":
        return False
    return "/" not in path and "\\" not in path


def _validate_write_value(
    value: object,
    path: StatePath = (),
) -> tuple[EnrichmentReasonCode, StatePath, str] | None:
    if isinstance(value, (list, tuple)):
        return (
            "forbidden_array",
            path,
            "enrichment contributions may not create or modify arrays",
        )
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                return (
                    "invalid_contribution",
                    path,
                    "enrichment object keys must be non-empty strings",
                )
            if key in _WORLD_STATE_DIRECTIVES:
                return (
                    "forbidden_directive",
                    (*path, key),
                    "world-state delete and replace directives are forbidden",
                )
            failure = _validate_write_value(child, (*path, key))
            if failure is not None:
                return failure
        return None
    if value is None or isinstance(value, (str, bool, int)):
        return None
    if isinstance(value, float) and math.isfinite(value):
        return None
    return (
        "invalid_contribution",
        path,
        "enrichment values must contain finite JSON scalar or object values",
    )


def _put_if_absent(
    candidate: dict[str, object],
    current_host: dict[str, object],
    basis_host: dict[str, object],
    patch: EnrichmentPatch,
    contribution: EnrichmentContribution,
) -> EnrichmentContributionOutcome:
    relative_path = contribution.target_path
    target = _absolute_host_path(patch, relative_path)
    basis = _lookup(basis_host, relative_path)
    current = _lookup(current_host, relative_path)
    parent_failure = _parent_failure(patch, contribution, basis, current)
    if parent_failure is not None:
        return parent_failure
    removed_parent = _removed_basis_ancestor(
        basis_host,
        current_host,
        relative_path,
    )
    if removed_parent is not None:
        return _skipped_contribution(
            contribution,
            target,
            "touched_path_changed",
            "a target parent present in the Integrator basis was removed",
            conflict_path=_absolute_host_path(patch, removed_parent),
            touched_paths=(target,),
        )
    if current.exists:
        if current.value == contribution.value:
            return EnrichmentContributionOutcome(
                contribution_id=contribution.id,
                status="already_satisfied",
                reason_code="already_satisfied",
                target_path=target,
                touched_paths=(target,),
            )
        reason: EnrichmentReasonCode = (
            "touched_path_changed"
            if not basis.exists or basis.value != current.value
            else "existing_value_conflict"
        )
        detail = (
            "target changed after the Integrator snapshot"
            if reason == "touched_path_changed"
            else "put_if_absent cannot replace an existing value"
        )
        return _skipped_contribution(
            contribution,
            target,
            reason,
            detail,
            conflict_path=target,
            touched_paths=(target,),
        )
    if basis.exists:
        return _skipped_contribution(
            contribution,
            target,
            "touched_path_changed",
            "an entity present in the Integrator basis was removed",
            conflict_path=target,
            touched_paths=(target,),
        )
    wrong_parent = _set_path(candidate, relative_path, contribution.value)
    if wrong_parent is not None:
        conflict = _absolute_host_path(patch, wrong_parent)
        return _skipped_contribution(
            contribution,
            target,
            "wrong_parent_type",
            "target registry has a non-object parent",
            conflict_path=conflict,
            touched_paths=(target,),
        )
    return EnrichmentContributionOutcome(
        contribution_id=contribution.id,
        status="applied",
        reason_code="applied",
        target_path=target,
        touched_paths=(target,),
    )


def _merge_missing(
    candidate: dict[str, object],
    current_host: dict[str, object],
    basis_host: dict[str, object],
    patch: EnrichmentPatch,
    contribution: EnrichmentContribution,
) -> EnrichmentContributionOutcome:
    entity_path = contribution.target_path
    target = _absolute_host_path(patch, entity_path)
    basis_entity = _lookup(basis_host, entity_path)
    current_entity = _lookup(current_host, entity_path)
    parent_failure = _parent_failure(
        patch,
        contribution,
        basis_entity,
        current_entity,
    )
    if parent_failure is not None:
        return parent_failure
    removed_parent = _removed_basis_ancestor(
        basis_host,
        current_host,
        entity_path,
    )
    if removed_parent is not None:
        return _skipped_contribution(
            contribution,
            target,
            "touched_path_changed",
            "an entity parent present in the Integrator basis was removed",
            conflict_path=_absolute_host_path(patch, removed_parent),
        )
    if basis_entity.exists and not current_entity.exists:
        return _skipped_contribution(
            contribution,
            target,
            "touched_path_changed",
            "an entity present in the Integrator basis was removed",
            conflict_path=target,
        )
    if basis_entity.exists and not isinstance(basis_entity.value, dict):
        return _skipped_contribution(
            contribution,
            target,
            "wrong_parent_type",
            "merge_missing requires an object entity in the Integrator basis",
            conflict_path=target,
        )
    if current_entity.exists and not isinstance(current_entity.value, dict):
        return _skipped_contribution(
            contribution,
            target,
            "wrong_parent_type",
            "merge_missing requires the current entity to be an object",
            conflict_path=target,
        )

    desired_leaves = tuple(_iter_desired_leaves(contribution.value))
    touched = tuple(
        _absolute_host_path(patch, (*entity_path, *path))
        for path, _value in desired_leaves
    )
    to_apply: list[tuple[StatePath, object]] = []
    for field_path, desired in desired_leaves:
        relative_path = (*entity_path, *field_path)
        absolute_path = _absolute_host_path(patch, relative_path)
        basis = _lookup(basis_host, relative_path)
        current = _lookup(current_host, relative_path)
        field_parent_failure = _parent_failure(
            patch,
            contribution,
            basis,
            current,
            target_path=target,
            touched_paths=touched,
        )
        if field_parent_failure is not None:
            return field_parent_failure
        removed_parent = _removed_basis_ancestor(
            basis_host,
            current_host,
            relative_path,
        )
        if removed_parent is not None:
            return _skipped_contribution(
                contribution,
                target,
                "touched_path_changed",
                "a field parent present in the Integrator basis was removed",
                conflict_path=_absolute_host_path(patch, removed_parent),
                touched_paths=touched,
            )
        if current.exists and current.value == desired:
            continue
        if basis.exists:
            if not current.exists or current.value != basis.value:
                return _skipped_contribution(
                    contribution,
                    target,
                    "touched_path_changed",
                    "a proposed field changed after the Integrator snapshot",
                    conflict_path=absolute_path,
                    touched_paths=touched,
                )
            return _skipped_contribution(
                contribution,
                target,
                "existing_value_conflict",
                "merge_missing cannot replace an existing field",
                conflict_path=absolute_path,
                touched_paths=touched,
            )
        if current.exists:
            return _skipped_contribution(
                contribution,
                target,
                "touched_path_changed",
                "a proposed field was populated after the Integrator snapshot",
                conflict_path=absolute_path,
                touched_paths=touched,
            )
        to_apply.append((relative_path, desired))

    for relative_path, desired in to_apply:
        wrong_parent = _set_path(candidate, relative_path, desired)
        if wrong_parent is not None:
            conflict = _absolute_host_path(patch, wrong_parent)
            return _skipped_contribution(
                contribution,
                target,
                "wrong_parent_type",
                "merge target has a non-object parent",
                conflict_path=conflict,
                touched_paths=touched,
            )
    if not to_apply:
        return EnrichmentContributionOutcome(
            contribution_id=contribution.id,
            status="already_satisfied",
            reason_code="already_satisfied",
            target_path=target,
            touched_paths=touched,
        )
    return EnrichmentContributionOutcome(
        contribution_id=contribution.id,
        status="applied",
        reason_code="applied",
        target_path=target,
        touched_paths=touched,
    )


def _parent_failure(
    patch: EnrichmentPatch,
    contribution: EnrichmentContribution,
    basis: _Lookup,
    current: _Lookup,
    *,
    target_path: StatePath | None = None,
    touched_paths: tuple[StatePath, ...] = (),
) -> EnrichmentContributionOutcome | None:
    target = target_path or _absolute_contribution_path(patch, contribution)
    wrong_parent = basis.wrong_parent or current.wrong_parent
    if wrong_parent is None:
        return None
    conflict = _absolute_host_path(patch, wrong_parent)
    return _skipped_contribution(
        contribution,
        target,
        "wrong_parent_type",
        "a touched path has a non-object parent",
        conflict_path=conflict,
        touched_paths=touched_paths,
    )


def _lookup(root: dict[str, object], path: StatePath) -> _Lookup:
    current: object = root
    traversed: list[str] = []
    for segment in path:
        if not isinstance(current, dict):
            return _Lookup(exists=False, wrong_parent=tuple(traversed))
        if segment not in current:
            return _Lookup(exists=False)
        current = current[segment]
        traversed.append(segment)
    return _Lookup(exists=True, value=current)


def _removed_basis_ancestor(
    basis_root: dict[str, object],
    current_root: dict[str, object],
    path: StatePath,
) -> StatePath | None:
    """Return the first basis parent removed after the Integrator snapshot."""

    for length in range(1, len(path)):
        prefix = path[:length]
        basis = _lookup(basis_root, prefix)
        current = _lookup(current_root, prefix)
        if basis.exists and not current.exists:
            return prefix
    return None


def _set_path(
    root: dict[str, object],
    path: StatePath,
    value: object,
) -> StatePath | None:
    current = root
    traversed: list[str] = []
    for segment in path[:-1]:
        child = current.get(segment, _MISSING)
        if child is _MISSING:
            child = {}
            current[segment] = child
        if not isinstance(child, dict):
            return tuple((*traversed, segment))
        current = child
        traversed.append(segment)
    current[path[-1]] = deepcopy(value)
    return None


def _iter_desired_leaves(
    value: dict[str, object],
    prefix: StatePath = (),
):
    for key, child in value.items():
        path = (*prefix, key)
        if isinstance(child, dict) and child:
            yield from _iter_desired_leaves(child, path)
        else:
            yield path, child


def _skipped_contribution(
    contribution: EnrichmentContribution,
    target_path: StatePath,
    reason_code: EnrichmentReasonCode,
    detail: str,
    *,
    conflict_path: StatePath | None = None,
    touched_paths: tuple[StatePath, ...] = (),
) -> EnrichmentContributionOutcome:
    return EnrichmentContributionOutcome(
        contribution_id=contribution.id,
        status="skipped",
        reason_code=reason_code,
        target_path=target_path,
        touched_paths=touched_paths,
        conflict_path=conflict_path,
        detail=detail,
    )


def _atomicity_skip(
    outcome: EnrichmentContributionOutcome,
    cluster_id: str,
) -> EnrichmentContributionOutcome:
    return replace(
        outcome,
        status="skipped",
        reason_code="cluster_atomicity",
        detail=f"cluster {cluster_id!r} was skipped atomically",
    )


def _absolute_host_path(patch: EnrichmentPatch, path: StatePath) -> StatePath:
    return ("hosts_by_name", patch.target_host, *path)


def _absolute_contribution_path(
    patch: EnrichmentPatch,
    contribution: EnrichmentContribution,
) -> StatePath:
    return _absolute_host_path(patch, contribution.target_path)


def _aggregate_outcome(
    clusters: list[EnrichmentClusterOutcome],
) -> tuple[EnrichmentPatchStatus, EnrichmentReasonCode]:
    statuses = [cluster.status for cluster in clusters]
    has_applied = "applied" in statuses
    has_skipped = "skipped" in statuses
    if has_applied:
        if has_skipped:
            return "partially_applied", "partially_applied"
        return "applied", "applied"
    if has_skipped:
        skipped_reasons = {
            cluster.reason_code for cluster in clusters if cluster.status == "skipped"
        }
        if len(skipped_reasons) == 1:
            return "skipped", skipped_reasons.pop()
        return "skipped", "all_clusters_skipped"
    return "already_satisfied", "already_satisfied"
