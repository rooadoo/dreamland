from __future__ import annotations

import hashlib
import json
import unittest
from copy import deepcopy
from typing import cast

from dreamland.background_agents.enrichment import (
    EnrichmentAuthorityDependency,
    EnrichmentCluster,
    EnrichmentContribution,
    EnrichmentDependency,
    EnrichmentPatch,
    EnrichmentPatchValidationError,
    apply_enrichment_patch,
    enrichment_patch_from_json,
    enrichment_patch_from_payload,
    enrichment_patch_to_json,
)
from dreamland.background_agents.enrichment.authentication import (
    ForegroundAuthenticationPaths,
)
from dreamland.background_agents.enrichment.contracts import (
    EnrichmentNoOp,
    EnrichmentOperation,
    enrichment_integrator_output_from_json,
    enrichment_integrator_output_from_payload,
    enrichment_integrator_output_to_json,
)


def _world() -> dict[str, object]:
    return {
        "name": "test-world",
        "active_host": "cache01",
        "hosts_by_name": {
            "cache01": {
                "hostname": "cache01",
                "operating_system": {"family": "linux"},
                "services_by_name": {"redis": {"state": "running", "port": 6379}},
                "filesystem": {"artifacts_by_path": {}},
                "facts_by_id": {},
            },
            "jump01": {"hostname": "jump01", "facts_by_id": {}},
        },
    }


def _host(state: dict[str, object], name: str = "cache01") -> dict[str, object]:
    hosts = state["hosts_by_name"]
    assert isinstance(hosts, dict)
    host = hosts[name]
    assert isinstance(host, dict)
    return host


def _contribution(
    *,
    contribution_id: str = "redis-description",
    registry_path: tuple[str, ...] = ("services_by_name",),
    key: str = "redis",
    operation: EnrichmentOperation = "merge_missing",
    value: dict[str, object] | None = None,
    source_item_ids: tuple[str, ...] = ("expert-item-1",),
) -> EnrichmentContribution:
    return EnrichmentContribution(
        id=contribution_id,
        registry_path=registry_path,
        key=key,
        operation=operation,
        value=value or {"description": "Session-local Redis cache"},
        source_item_ids=source_item_ids,
    )


def _patch(
    basis_host: dict[str, object],
    *contributions: EnrichmentContribution,
    dependencies: tuple[EnrichmentDependency, ...] = (),
    authority_dependencies: tuple[EnrichmentAuthorityDependency, ...] = (),
    trusted_dependency_hashes: tuple[tuple[tuple[str, ...], str], ...] = (),
    cluster_id: str = "redis-finishing",
) -> EnrichmentPatch:
    return EnrichmentPatch(
        target_host="cache01",
        basis_host=basis_host,
        clusters=(
            EnrichmentCluster(
                id=cluster_id,
                dependencies=dependencies,
                authority_dependencies=authority_dependencies,
                contributions=contributions or (_contribution(),),
            ),
        ),
        trusted_dependency_hashes=trusted_dependency_hashes,
    )


def _value_sha256(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class EnrichmentPatchApplicationTests(unittest.TestCase):
    def test_foreground_authentication_paths_block_missing_artifacts_atomically(self):
        current = _world()
        basis = deepcopy(_host(current))
        protected = ForegroundAuthenticationPaths(
            ssh_directories=frozenset({"/home/alice/.ssh"}),
            key_paths=frozenset({"/home/alice/a", "/home/alice/a.pub"}),
        )
        for key in (
            "/home/alice/.ssh",
            "/home/alice/.ssh/config",
            "/home/alice/.ssh/config.d",
            "/home/alice/.ssh/id_ed25519_github",
            "/home/alice/.ssh/id_ed25519_github.pub",
            "/home/alice/.ssh/known_hosts",
            "/home/alice/a",
        ):
            with self.subTest(key=key):
                patch = _patch(
                    basis,
                    _contribution(),
                    _contribution(
                        contribution_id="invented-key",
                        registry_path=("filesystem", "artifacts_by_path"),
                        key=key,
                        operation="put_if_absent",
                        value={
                            "description": "Passphrase-protected key selected by SSH"
                        },
                    ),
                )
                result = apply_enrichment_patch(
                    current, patch, protected_authentication_paths=protected
                )
                self.assertEqual(result.world_state, current)
                self.assertEqual(
                    result.outcome.reason_code, "foreground_authentication_conflict"
                )

    def test_authentication_guard_preserves_seed_and_allows_unrelated_worldbuilding(
        self,
    ):
        current = _world()
        host = _host(current)
        seed = {"content": "scenario-provided key", "passphrase_required": False}
        host["filesystem"]["artifacts_by_path"]["/home/alice/.ssh/id_ed25519"] = seed
        basis = deepcopy(host)
        patch = _patch(
            basis,
            _contribution(),
            _contribution(
                contribution_id="other-user-key",
                registry_path=("filesystem", "artifacts_by_path"),
                key="/home/bob/.ssh/id_ed25519",
                operation="put_if_absent",
                value={
                    "description": "Independent actor's established client identity"
                },
            ),
        )
        result = apply_enrichment_patch(
            current,
            patch,
            protected_authentication_paths=ForegroundAuthenticationPaths(
                ssh_directories=frozenset({"/home/alice/.ssh"})
            ),
        )
        self.assertEqual(result.outcome.status, "applied")
        artifacts = _host(result.world_state)["filesystem"]["artifacts_by_path"]
        self.assertEqual(artifacts["/home/alice/.ssh/id_ed25519"], seed)
        self.assertIn("/home/bob/.ssh/id_ed25519", artifacts)

    def test_active_session_authority_dependency_checks_apply_time_digest(self):
        current = _world()
        basis = deepcopy(_host(current))
        dependency = EnrichmentAuthorityDependency(
            domain="active_session",
            expected_sha256="a" * 64,
        )
        patch = _patch(
            basis,
            authority_dependencies=(dependency,),
        )

        applied = apply_enrichment_patch(
            current,
            patch,
            current_authority_sha256_by_domain={"active_session": "a" * 64},
        )
        changed = apply_enrichment_patch(
            current,
            patch,
            current_authority_sha256_by_domain={"active_session": "b" * 64},
        )
        missing = apply_enrichment_patch(current, patch)

        self.assertEqual(applied.outcome.status, "applied")
        for result in (changed, missing):
            self.assertEqual(result.world_state, current)
            self.assertEqual(
                result.outcome.clusters[0].reason_code,
                "semantic_authority_conflict",
            )
            self.assertIsNone(result.outcome.clusters[0].conflict_path)

    def test_foreground_evidence_dependency_checks_apply_time_digest(self):
        current = _world()
        basis = deepcopy(_host(current))
        patch = _patch(
            basis,
            authority_dependencies=(
                EnrichmentAuthorityDependency(
                    domain="foreground_evidence",
                    expected_sha256="c" * 64,
                ),
            ),
        )

        applied = apply_enrichment_patch(
            current,
            patch,
            current_authority_sha256_by_domain={"foreground_evidence": "c" * 64},
        )
        stale = apply_enrichment_patch(
            current,
            patch,
            current_authority_sha256_by_domain={"foreground_evidence": "d" * 64},
        )

        self.assertEqual(applied.outcome.status, "applied")
        self.assertEqual(stale.world_state, current)
        self.assertEqual(
            stale.outcome.clusters[0].reason_code,
            "semantic_authority_conflict",
        )
        self.assertIn("foreground_evidence", stale.outcome.clusters[0].detail)

    def test_authority_dependency_is_hidden_and_legacy_patch_is_unaffected(self):
        current = _world()
        basis = deepcopy(_host(current))
        patch = _patch(
            basis,
            authority_dependencies=(
                EnrichmentAuthorityDependency(
                    domain="active_session",
                    expected_sha256="a" * 64,
                ),
            ),
        )

        self.assertNotIn("authority", json.dumps(patch.to_payload()))
        legacy_patch = _patch(basis)
        self.assertNotEqual(patch, legacy_patch)
        legacy = apply_enrichment_patch(current, legacy_patch)
        self.assertEqual(legacy.outcome.status, "applied")

    def test_invalid_authority_dependency_is_rejected(self):
        current = _world()
        basis = deepcopy(_host(current))
        patch = _patch(
            basis,
            authority_dependencies=(
                EnrichmentAuthorityDependency(
                    domain="active_session",
                    expected_sha256="not-a-digest",
                ),
            ),
        )

        result = apply_enrichment_patch(current, patch)

        self.assertEqual(result.outcome.reason_code, "invalid_patch")
        self.assertEqual(result.world_state, current)

    def test_direct_patch_application_rejects_twenty_five_contributions(self):
        current = _world()
        basis = deepcopy(_host(current))
        contributions = tuple(
            _contribution(
                contribution_id=f"fact-{index}",
                registry_path=("facts_by_id",),
                key=f"fact-{index}",
                operation="put_if_absent",
                value={"kind": "fact", "index": index},
            )
            for index in range(1, 26)
        )

        result = apply_enrichment_patch(current, _patch(basis, *contributions))

        self.assertEqual(result.outcome.status, "skipped")
        self.assertEqual(
            result.outcome.clusters[0].reason_code,
            "invalid_cluster",
        )
        self.assertEqual(result.world_state, current)

    def test_filesystem_artifact_keys_require_absolute_paths(self):
        invalid_paths = ("workspace/notes.txt", "~/notes.txt", "./notes.txt")

        for path in invalid_paths:
            with self.subTest(path=path):
                current = _world()
                basis = deepcopy(_host(current))
                patch = _patch(
                    basis,
                    _contribution(
                        contribution_id="relative-artifact",
                        registry_path=("filesystem", "artifacts_by_path"),
                        key=path,
                        value={"kind": "file"},
                    ),
                )

                result = apply_enrichment_patch(current, patch)

                self.assertEqual(result.outcome.status, "skipped")
                self.assertEqual(
                    result.outcome.clusters[0].reason_code,
                    "invalid_contribution",
                )
                self.assertEqual(result.world_state, current)

    def test_filesystem_artifact_keys_accept_absolute_and_platform_native_paths(self):
        valid_paths = (
            "/var/lib/app/state.db",
            "C:\\Users\\alice\\state.db",
            "SYS$SYSDEVICE:[USERS.ALICE]STATE.DAT",
        )

        for path in valid_paths:
            with self.subTest(path=path):
                current = _world()
                basis = deepcopy(_host(current))
                patch = _patch(
                    basis,
                    _contribution(
                        contribution_id="absolute-artifact",
                        registry_path=("filesystem", "artifacts_by_path"),
                        key=path,
                        value={"kind": "file"},
                    ),
                )

                result = apply_enrichment_patch(current, patch)

                self.assertEqual(result.outcome.status, "applied")
                artifacts = _host(result.world_state)["filesystem"]["artifacts_by_path"]
                self.assertIn(path, artifacts)

    def test_repeated_merge_target_accepts_disjoint_leaf_paths_and_provenance(self):
        current = _world()
        original = deepcopy(current)
        basis = deepcopy(_host(current))
        first = _contribution(
            contribution_id="report-first-half",
            registry_path=("facts_by_id",),
            key="inspection-report",
            operation="merge_missing",
            value={"metadata": {f"first-{index}": index for index in range(1, 9)}},
            source_item_ids=tuple(f"i{index}" for index in range(1, 9)),
        )
        second = _contribution(
            contribution_id="report-second-half",
            registry_path=("facts_by_id",),
            key="inspection-report",
            operation="merge_missing",
            value={"metadata": {f"second-{index}": index for index in range(9, 17)}},
            source_item_ids=tuple(f"i{index}" for index in range(9, 17)),
        )

        result = apply_enrichment_patch(current, _patch(basis, first, second))

        self.assertEqual(result.outcome.status, "applied")
        self.assertEqual(
            [item.status for item in result.outcome.clusters[0].contributions],
            ["applied", "applied"],
        )
        report = _host(result.world_state)["facts_by_id"]["inspection-report"]
        self.assertIsInstance(report, dict)
        assert isinstance(report, dict)
        metadata = report["metadata"]
        self.assertIsInstance(metadata, dict)
        assert isinstance(metadata, dict)
        self.assertEqual(len(metadata), 16)
        self.assertEqual(metadata["first-1"], 1)
        self.assertEqual(metadata["second-16"], 16)
        self.assertEqual(current, original)

    def test_repeated_merge_target_rejects_overlapping_and_prefix_leaf_paths(self):
        cases = (
            (
                {"metadata": {"owner": "platform"}},
                {"metadata": {"owner": "operations"}},
            ),
            (
                {"metadata": "legacy"},
                {"metadata": {"owner": "operations"}},
            ),
        )
        for first_value, second_value in cases:
            with self.subTest(first_value=first_value, second_value=second_value):
                current = _world()
                basis = deepcopy(_host(current))
                first = _contribution(
                    contribution_id="first",
                    registry_path=("facts_by_id",),
                    key="inspection-report",
                    operation="merge_missing",
                    value=first_value,
                )
                second = _contribution(
                    contribution_id="second",
                    registry_path=("facts_by_id",),
                    key="inspection-report",
                    operation="merge_missing",
                    value=second_value,
                )

                result = apply_enrichment_patch(
                    current,
                    _patch(basis, first, second),
                )

                self.assertEqual(result.outcome.status, "skipped")
                self.assertEqual(
                    result.outcome.clusters[0].reason_code,
                    "duplicate_contribution_target",
                )
                self.assertEqual(result.world_state, current)

    def test_repeated_target_rejects_put_if_absent_and_mixed_operations(self):
        operation_pairs = (
            ("put_if_absent", "put_if_absent"),
            ("put_if_absent", "merge_missing"),
        )
        for first_operation, second_operation in operation_pairs:
            with self.subTest(
                first_operation=first_operation,
                second_operation=second_operation,
            ):
                current = _world()
                basis = deepcopy(_host(current))
                first = _contribution(
                    contribution_id="first",
                    registry_path=("facts_by_id",),
                    key="inspection-report",
                    operation=cast(EnrichmentOperation, first_operation),
                    value={"first": True},
                )
                second = _contribution(
                    contribution_id="second",
                    registry_path=("facts_by_id",),
                    key="inspection-report",
                    operation=cast(EnrichmentOperation, second_operation),
                    value={"second": True},
                )

                result = apply_enrichment_patch(
                    current,
                    _patch(basis, first, second),
                )

                self.assertEqual(
                    result.outcome.clusters[0].reason_code,
                    "duplicate_contribution_target",
                )
                self.assertEqual(result.world_state, current)

    def test_applies_additive_cluster_without_mutating_inputs(self):
        current = _world()
        original = deepcopy(current)
        basis = deepcopy(_host(current))
        patch = _patch(
            basis,
            _contribution(
                value={
                    "description": "Session-local Redis cache",
                    "monitoring": {"agent": "redis_exporter"},
                }
            ),
            _contribution(
                contribution_id="redis-config",
                registry_path=("filesystem", "artifacts_by_path"),
                key="/etc/redis/redis.conf",
                operation="put_if_absent",
                value={"kind": "file", "owner": "redis"},
            ),
            dependencies=(
                EnrichmentDependency(
                    path=("services_by_name", "redis", "state"),
                    expected="running",
                ),
            ),
        )

        result = apply_enrichment_patch(current, patch)

        self.assertEqual(result.outcome.status, "applied")
        self.assertEqual(result.outcome.reason_code, "applied")
        self.assertEqual(result.outcome.clusters[0].status, "applied")
        result_host = _host(result.world_state)
        self.assertEqual(
            result_host["services_by_name"]["redis"]["monitoring"]["agent"],
            "redis_exporter",
        )
        self.assertIn(
            "/etc/redis/redis.conf",
            result_host["filesystem"]["artifacts_by_path"],
        )
        self.assertEqual(current, original)
        self.assertNotIn("description", basis["services_by_name"]["redis"])
        self.assertIsNot(result.world_state, current)
        self.assertNotIn("source_item_ids", json.dumps(result.world_state))

    def test_identical_values_are_already_satisfied(self):
        current = _world()
        _host(current)["services_by_name"]["redis"]["description"] = (
            "Session-local Redis cache"
        )
        basis = deepcopy(_host(current))

        result = apply_enrichment_patch(current, _patch(basis))

        self.assertEqual(result.outcome.status, "already_satisfied")
        contribution = result.outcome.clusters[0].contributions[0]
        self.assertEqual(contribution.status, "already_satisfied")
        self.assertEqual(contribution.reason_code, "already_satisfied")
        self.assertEqual(result.world_state, current)
        self.assertIsNot(result.world_state, current)

    def test_unrelated_host_and_world_drift_is_allowed(self):
        basis_world = _world()
        basis = deepcopy(_host(basis_world))
        current = deepcopy(basis_world)
        current["operator_note"] = "unrelated world change"
        host = _host(current)
        host["facts_by_id"]["rack"] = {"value": "R14"}
        host["services_by_name"]["redis"]["last_observed"] = "recently"

        result = apply_enrichment_patch(current, _patch(basis))

        self.assertEqual(result.outcome.status, "applied")
        result_host = _host(result.world_state)
        self.assertEqual(result_host["facts_by_id"]["rack"]["value"], "R14")
        self.assertEqual(
            result_host["services_by_name"]["redis"]["last_observed"],
            "recently",
        )

    def test_touched_path_drift_skips_entire_cluster_atomically(self):
        basis_world = _world()
        basis = deepcopy(_host(basis_world))
        current = deepcopy(basis_world)
        _host(current)["services_by_name"]["redis"]["description"] = (
            "Foreground command established a different purpose"
        )
        patch = _patch(
            basis,
            _contribution(
                contribution_id="redis-config",
                registry_path=("filesystem", "artifacts_by_path"),
                key="/etc/redis/redis.conf",
                operation="put_if_absent",
                value={"kind": "file"},
            ),
            _contribution(),
        )

        result = apply_enrichment_patch(current, patch)

        cluster = result.outcome.clusters[0]
        self.assertEqual(cluster.status, "skipped")
        self.assertEqual(cluster.reason_code, "touched_path_changed")
        self.assertEqual(
            cluster.contributions[0].reason_code,
            "cluster_atomicity",
        )
        self.assertEqual(
            cluster.contributions[1].reason_code,
            "touched_path_changed",
        )
        self.assertNotIn(
            "/etc/redis/redis.conf",
            _host(result.world_state)["filesystem"]["artifacts_by_path"],
        )

    def test_current_identical_value_wins_as_no_op_despite_race(self):
        basis_world = _world()
        basis = deepcopy(_host(basis_world))
        current = deepcopy(basis_world)
        _host(current)["facts_by_id"]["redis-owner"] = {"team": "platform"}
        contribution = _contribution(
            contribution_id="owner",
            registry_path=("facts_by_id",),
            key="redis-owner",
            operation="put_if_absent",
            value={"team": "platform"},
        )

        result = apply_enrichment_patch(current, _patch(basis, contribution))

        self.assertEqual(result.outcome.status, "already_satisfied")
        self.assertEqual(
            result.outcome.clusters[0].contributions[0].reason_code,
            "already_satisfied",
        )

    def test_existing_basis_value_is_never_replaced(self):
        current = _world()
        basis = deepcopy(_host(current))
        contribution = _contribution(value={"state": "stopped"})

        result = apply_enrichment_patch(current, _patch(basis, contribution))

        cluster = result.outcome.clusters[0]
        self.assertEqual(cluster.status, "skipped")
        self.assertEqual(cluster.reason_code, "existing_value_conflict")
        self.assertEqual(
            _host(result.world_state)["services_by_name"]["redis"]["state"],
            "running",
        )

    def test_removed_basis_entity_is_not_recreated(self):
        basis_world = _world()
        basis = deepcopy(_host(basis_world))
        current = deepcopy(basis_world)
        del _host(current)["services_by_name"]["redis"]

        result = apply_enrichment_patch(current, _patch(basis))

        self.assertEqual(
            result.outcome.clusters[0].reason_code,
            "touched_path_changed",
        )
        self.assertNotIn("redis", _host(result.world_state)["services_by_name"])

    def test_deleted_basis_registry_is_not_recreated_for_either_operation(self):
        for operation in ("put_if_absent", "merge_missing"):
            with self.subTest(operation=operation):
                basis_world = _world()
                basis = deepcopy(_host(basis_world))
                current = deepcopy(basis_world)
                del _host(current)["facts_by_id"]
                contribution = _contribution(
                    contribution_id=f"owner-{operation}",
                    registry_path=("facts_by_id",),
                    key="redis-owner",
                    operation=operation,
                    value={"team": "platform"},
                )

                result = apply_enrichment_patch(
                    current,
                    _patch(basis, contribution),
                )

                outcome = result.outcome.clusters[0].contributions[0]
                self.assertEqual(outcome.reason_code, "touched_path_changed")
                self.assertEqual(
                    outcome.conflict_path,
                    ("hosts_by_name", "cache01", "facts_by_id"),
                )
                self.assertNotIn("facts_by_id", _host(result.world_state))

    def test_merge_missing_does_not_recreate_deleted_nested_basis_object(self):
        basis_world = _world()
        _host(basis_world)["services_by_name"]["redis"]["monitoring"] = {}
        basis = deepcopy(_host(basis_world))
        current = deepcopy(basis_world)
        del _host(current)["services_by_name"]["redis"]["monitoring"]
        contribution = _contribution(
            value={"monitoring": {"agent": "redis_exporter"}},
        )

        result = apply_enrichment_patch(current, _patch(basis, contribution))

        outcome = result.outcome.clusters[0].contributions[0]
        self.assertEqual(outcome.reason_code, "touched_path_changed")
        self.assertEqual(
            outcome.conflict_path,
            (
                "hosts_by_name",
                "cache01",
                "services_by_name",
                "redis",
                "monitoring",
            ),
        )
        self.assertNotIn(
            "monitoring",
            _host(result.world_state)["services_by_name"]["redis"],
        )

    def test_changed_dependency_skips_cluster_with_exact_path(self):
        basis_world = _world()
        basis = deepcopy(_host(basis_world))
        current = deepcopy(basis_world)
        _host(current)["operating_system"]["family"] = "freebsd"
        patch = _patch(
            basis,
            dependencies=(
                EnrichmentDependency(
                    path=("operating_system", "family"),
                    expected="linux",
                ),
            ),
        )

        result = apply_enrichment_patch(current, patch)

        cluster = result.outcome.clusters[0]
        self.assertEqual(cluster.reason_code, "dependency_changed")
        self.assertEqual(
            cluster.conflict_path,
            ("hosts_by_name", "cache01", "operating_system", "family"),
        )

    def test_dependency_must_match_trusted_basis(self):
        current = _world()
        basis = deepcopy(_host(current))
        patch = _patch(
            basis,
            dependencies=(
                EnrichmentDependency(
                    path=("operating_system", "family"),
                    expected="windows",
                ),
            ),
        )

        result = apply_enrichment_patch(current, patch)

        self.assertEqual(
            result.outcome.clusters[0].reason_code,
            "dependency_basis_mismatch",
        )

    def test_projected_dependency_ignores_omitted_fields_but_checks_visible_fields(
        self,
    ):
        current = _world()
        current_host = _host(current)
        current_host["filesystem"] = {
            "artifacts_by_path": {
                "/var/lib/app/cache.db": {
                    "kind": "file",
                    "content": "omitted from model projection",
                }
            }
        }
        basis = deepcopy(current_host)
        basis["filesystem"]["artifacts_by_path"]["/var/lib/app/cache.db"] = {
            "kind": "file"
        }
        dependency = EnrichmentDependency(
            path=(
                "filesystem",
                "artifacts_by_path",
                "/var/lib/app/cache.db",
            ),
            expected={"kind": "file"},
        )
        contribution = _contribution(
            registry_path=("facts_by_id",),
            key="cache-index-worker",
            operation="put_if_absent",
            value={"kind": "process", "name": "cache-index-worker"},
        )
        patch = _patch(
            basis,
            contribution,
            dependencies=(dependency,),
            trusted_dependency_hashes=(
                (
                    dependency.path,
                    _value_sha256(
                        current_host["filesystem"]["artifacts_by_path"][
                            "/var/lib/app/cache.db"
                        ]
                    ),
                ),
            ),
        )

        applied = apply_enrichment_patch(current, patch)

        self.assertEqual(applied.outcome.status, "applied")
        self.assertEqual(
            _host(applied.world_state)["filesystem"]["artifacts_by_path"][
                "/var/lib/app/cache.db"
            ]["content"],
            "omitted from model projection",
        )

        changed = deepcopy(current)
        _host(changed)["filesystem"]["artifacts_by_path"]["/var/lib/app/cache.db"][
            "kind"
        ] = "directory"
        rejected = apply_enrichment_patch(changed, patch)

        self.assertEqual(
            rejected.outcome.clusters[0].reason_code,
            "dependency_changed",
        )

        added_after_snapshot = deepcopy(current)
        _host(added_after_snapshot)["filesystem"]["artifacts_by_path"][
            "/var/lib/app/cache.db"
        ]["state"] = "changed-after-snapshot"
        added_result = apply_enrichment_patch(added_after_snapshot, patch)

        self.assertEqual(
            added_result.outcome.clusters[0].reason_code,
            "dependency_changed",
        )

    def test_target_deletion_and_wrong_host_shape_are_structured_skips(self):
        baseline = _world()
        basis = deepcopy(_host(baseline))
        patch = _patch(basis)

        deleted = deepcopy(baseline)
        del deleted["hosts_by_name"]["cache01"]
        deleted_result = apply_enrichment_patch(deleted, patch)
        self.assertEqual(deleted_result.outcome.reason_code, "target_missing")
        self.assertEqual(
            deleted_result.outcome.clusters[0].contributions[0].reason_code,
            "target_missing",
        )

        malformed = deepcopy(baseline)
        malformed["hosts_by_name"]["cache01"] = "not-an-object"
        malformed_result = apply_enrichment_patch(malformed, patch)
        self.assertEqual(
            malformed_result.outcome.reason_code,
            "wrong_parent_type",
        )

    def test_wrong_registry_parent_is_not_replaced(self):
        current = _world()
        _host(current)["filesystem"] = "mounted elsewhere"
        basis = deepcopy(_host(current))
        contribution = _contribution(
            registry_path=("filesystem", "artifacts_by_path"),
            key="/etc/redis/redis.conf",
            operation="put_if_absent",
            value={"kind": "file"},
        )

        result = apply_enrichment_patch(current, _patch(basis, contribution))

        outcome = result.outcome.clusters[0]
        self.assertEqual(outcome.reason_code, "wrong_parent_type")
        self.assertEqual(_host(result.world_state)["filesystem"], "mounted elsewhere")

    def test_forbidden_registries_arrays_and_directives_are_rejected(self):
        cases = (
            (
                _contribution(
                    registry_path=("shell",),
                    key="default",
                    operation="put_if_absent",
                    value={"name": "zsh"},
                ),
                "forbidden_registry",
            ),
            (
                _contribution(value={"companions": ["sentinel"]}),
                "forbidden_array",
            ),
            (
                _contribution(value={"__dreamland_delete__": True}),
                "forbidden_directive",
            ),
            (
                _contribution(
                    operation=cast(EnrichmentOperation, "replace"),
                ),
                "unsupported_operation",
            ),
        )
        for contribution, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                current = _world()
                basis = deepcopy(_host(current))

                result = apply_enrichment_patch(current, _patch(basis, contribution))

                self.assertEqual(
                    result.outcome.clusters[0].reason_code,
                    reason_code,
                )
                self.assertEqual(result.world_state, current)

    def test_missing_allowlisted_registry_is_created(self):
        current = _world()
        del _host(current)["facts_by_id"]
        basis = deepcopy(_host(current))
        contribution = _contribution(
            registry_path=("facts_by_id",),
            key="redis-owner",
            operation="put_if_absent",
            value={"team": "platform"},
        )

        result = apply_enrichment_patch(current, _patch(basis, contribution))

        self.assertEqual(result.outcome.status, "applied")
        self.assertEqual(
            _host(result.world_state)["facts_by_id"]["redis-owner"]["team"],
            "platform",
        )

    def test_clusters_are_independently_atomic(self):
        current = _world()
        basis = deepcopy(_host(current))
        patch = EnrichmentPatch(
            target_host="cache01",
            basis_host=basis,
            clusters=(
                EnrichmentCluster(
                    id="conflict",
                    contributions=(_contribution(value={"state": "stopped"}),),
                ),
                EnrichmentCluster(
                    id="safe",
                    contributions=(
                        _contribution(
                            contribution_id="owner",
                            registry_path=("facts_by_id",),
                            key="redis-owner",
                            operation="put_if_absent",
                            value={"team": "platform"},
                        ),
                    ),
                ),
            ),
        )

        result = apply_enrichment_patch(current, patch)

        self.assertEqual(result.outcome.status, "partially_applied")
        self.assertEqual(result.outcome.clusters[0].status, "skipped")
        self.assertEqual(result.outcome.clusters[1].status, "applied")
        self.assertIn("redis-owner", _host(result.world_state)["facts_by_id"])

    def test_skipped_and_satisfied_clusters_are_not_partially_applied(self):
        current = _world()
        _host(current)["services_by_name"]["redis"]["description"] = (
            "Session-local Redis cache"
        )
        original = deepcopy(current)
        basis = deepcopy(_host(current))
        patch = EnrichmentPatch(
            target_host="cache01",
            basis_host=basis,
            clusters=(
                EnrichmentCluster(
                    id="satisfied",
                    contributions=(
                        _contribution(contribution_id="description-satisfied"),
                    ),
                ),
                EnrichmentCluster(
                    id="conflict",
                    contributions=(
                        _contribution(
                            contribution_id="state-conflict",
                            value={"state": "stopped"},
                        ),
                    ),
                ),
            ),
        )

        result = apply_enrichment_patch(current, patch)

        self.assertEqual(result.outcome.status, "skipped")
        self.assertEqual(result.outcome.reason_code, "existing_value_conflict")
        self.assertEqual(
            [cluster.status for cluster in result.outcome.clusters],
            ["already_satisfied", "skipped"],
        )
        self.assertEqual(result.world_state, original)


class EnrichmentPatchParsingTests(unittest.TestCase):
    def test_compact_json_round_trip_omits_trusted_basis(self):
        state = _world()
        basis = deepcopy(_host(state))
        patch = _patch(
            basis,
            dependencies=(
                EnrichmentDependency(
                    path=("services_by_name", "redis", "state"),
                    expected="running",
                ),
            ),
        )

        text = enrichment_patch_to_json(patch)
        payload = json.loads(text)
        parsed = enrichment_patch_from_json(text, basis_host=basis)

        self.assertNotIn("basis_host", payload)
        self.assertEqual(payload["status"], "patch")
        self.assertEqual(
            payload["clusters"][0]["contributions"][0]["source_item_ids"],
            ["expert-item-1"],
        )
        self.assertEqual(parsed, patch)
        self.assertNotIn(": ", text)
        self.assertNotIn(", ", text)

    def test_parser_rejects_unknown_fields(self):
        state = _world()
        basis = deepcopy(_host(state))
        payload = _patch(basis).to_payload()
        payload["commentary"] = "model prose is not part of this contract"

        with self.assertRaises(EnrichmentPatchValidationError) as raised:
            enrichment_patch_from_payload(payload, basis_host=basis)

        self.assertEqual(raised.exception.reason_code, "invalid_patch")
        self.assertEqual(raised.exception.location, "$")
        self.assertIn("unknown keys", str(raised.exception))

    def test_parser_rejects_unsupported_operation_with_reason_code(self):
        state = _world()
        basis = deepcopy(_host(state))
        payload = _patch(basis).to_payload()
        payload["clusters"][0]["contributions"][0]["operation"] = "replace"

        with self.assertRaises(EnrichmentPatchValidationError) as raised:
            enrichment_patch_from_payload(payload, basis_host=basis)

        self.assertEqual(raised.exception.reason_code, "unsupported_operation")
        self.assertEqual(
            raised.exception.location,
            "$.clusters[0].contributions[0].operation",
        )

    def test_union_parser_accepts_tagged_patch_and_no_op(self):
        state = _world()
        basis = deepcopy(_host(state))
        patch = _patch(basis)
        no_op = EnrichmentNoOp(reason_code="already_materialized")

        parsed_patch = enrichment_integrator_output_from_json(
            enrichment_integrator_output_to_json(patch),
            basis_host=basis,
        )
        parsed_no_op = enrichment_integrator_output_from_payload(
            no_op.to_payload(),
            basis_host=basis,
        )

        self.assertEqual(parsed_patch, patch)
        self.assertEqual(parsed_no_op, no_op)
        self.assertEqual(
            no_op.to_payload(),
            {
                "v": 2,
                "status": "no_op",
                "reason_code": "already_materialized",
            },
        )

    def test_patch_specific_parser_rejects_no_op(self):
        basis = deepcopy(_host(_world()))

        with self.assertRaises(EnrichmentPatchValidationError) as raised:
            enrichment_patch_from_payload(
                EnrichmentNoOp(reason_code="no_safe_mapping").to_payload(),
                basis_host=basis,
            )

        self.assertEqual(raised.exception.reason_code, "invalid_patch")
        self.assertEqual(raised.exception.location, "$")
        self.assertIn("missing keys", str(raised.exception))
        self.assertIn("unknown keys", str(raised.exception))

    def test_no_op_parser_rejects_unknown_key_and_reason(self):
        basis = deepcopy(_host(_world()))
        cases = (
            (
                {
                    "v": 2,
                    "status": "no_op",
                    "reason_code": "no_safe_mapping",
                    "detail": "model prose",
                },
                "$",
            ),
            (
                {
                    "v": 2,
                    "status": "no_op",
                    "reason_code": "uncertain",
                },
                "$.reason_code",
            ),
        )

        for payload, expected_location in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(EnrichmentPatchValidationError) as raised:
                    enrichment_integrator_output_from_payload(
                        payload,
                        basis_host=basis,
                    )

                self.assertEqual(raised.exception.location, expected_location)

    def test_union_parser_rejects_missing_or_unknown_status(self):
        basis = deepcopy(_host(_world()))
        cases = (
            ({"v": 2}, "$", "missing keys: status"),
            ({"v": 2, "status": "maybe"}, "$.status", "unsupported"),
        )

        for payload, expected_location, expected_detail in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(EnrichmentPatchValidationError) as raised:
                    enrichment_integrator_output_from_payload(
                        payload,
                        basis_host=basis,
                    )

                self.assertEqual(raised.exception.location, expected_location)
                self.assertIn(expected_detail, str(raised.exception))

    def test_parser_requires_bounded_unique_unprefixed_source_item_ids(self):
        basis = deepcopy(_host(_world()))
        invalid_source_lists = (
            [],
            [f"item-{index}" for index in range(9)],
            ["item-1", "item-1"],
            ["@r1"],
            ["  "],
        )

        for source_item_ids in invalid_source_lists:
            with self.subTest(source_item_ids=source_item_ids):
                payload = _patch(basis).to_payload()
                contribution = payload["clusters"][0]["contributions"][0]
                contribution["source_item_ids"] = source_item_ids

                with self.assertRaises(EnrichmentPatchValidationError) as raised:
                    enrichment_patch_from_payload(payload, basis_host=basis)

                self.assertEqual(
                    raised.exception.reason_code,
                    "invalid_contribution",
                )
                self.assertIn(
                    ".source_item_ids",
                    raised.exception.location,
                )

    def test_direct_patch_with_invalid_provenance_is_safely_skipped(self):
        state = _world()
        basis = deepcopy(_host(state))
        patch = _patch(
            basis,
            _contribution(source_item_ids=()),
        )

        result = apply_enrichment_patch(state, patch)

        self.assertEqual(result.outcome.status, "skipped")
        self.assertEqual(
            result.outcome.clusters[0].reason_code,
            "invalid_contribution",
        )
        self.assertEqual(result.world_state, state)


if __name__ == "__main__":
    unittest.main()
