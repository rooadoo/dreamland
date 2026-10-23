from __future__ import annotations

import unittest
from copy import deepcopy

from dreamland.background_agents.enrichment.contracts import (
    EnrichmentCluster,
    EnrichmentContribution,
    EnrichmentDependency,
    EnrichmentPatch,
)
from dreamland.background_agents.enrichment.normalization import (
    INTEGRATOR_NORMALIZATION_POLICY_VERSION,
    MAX_RETAINED_NORMALIZATION_PATHS,
    normalize_integrator_patch,
)


def _contribution(
    contribution_id: str,
    value: dict[str, object],
    *,
    source_item_ids: tuple[str, ...] = ("i1",),
) -> EnrichmentContribution:
    return EnrichmentContribution(
        id=contribution_id,
        registry_path=("facts_by_id",),
        key=contribution_id,
        operation="put_if_absent",
        value=value,
        source_item_ids=source_item_ids,
    )


def _patch(*contributions: EnrichmentContribution) -> EnrichmentPatch:
    return EnrichmentPatch(
        target_host="host-1",
        basis_host={"hostname": "host-1", "facts_by_id": {}},
        clusters=(
            EnrichmentCluster(
                id="cluster-1",
                dependencies=(
                    EnrichmentDependency(
                        path=("facts_by_id", "existing", "members"),
                        expected=["alpha", "beta"],
                    ),
                ),
                contributions=contributions,
            ),
        ),
    )


class IntegratorNormalizationTests(unittest.TestCase):
    def test_prunes_array_fields_and_newly_empty_parents_only(self) -> None:
        contribution = _contribution(
            "sshd",
            {
                "kind": "service",
                "state": "running",
                "listeners": ["127.0.0.1:22", "10.72.2.17:22"],
                "metadata": {
                    "owner": "root",
                    "aliases": ["ssh"],
                    "nested": {"ports": [22]},
                },
                "preexisting_empty": {},
            },
        )
        patch = _patch(contribution)
        original_payload = deepcopy(patch.to_payload())

        result = normalize_integrator_patch(patch)

        self.assertEqual(result.policy_version, INTEGRATOR_NORMALIZATION_POLICY_VERSION)
        self.assertIsNotNone(result.patch)
        assert result.patch is not None
        normalized = result.patch.clusters[0].contributions[0]
        self.assertEqual(
            normalized.value,
            {
                "kind": "service",
                "state": "running",
                "metadata": {"owner": "root"},
                "preexisting_empty": {},
            },
        )
        self.assertEqual(normalized.source_item_ids, contribution.source_item_ids)
        self.assertEqual(
            result.patch.clusters[0].dependencies,
            patch.clusters[0].dependencies,
        )
        self.assertEqual(patch.to_payload(), original_payload)
        self.assertEqual(
            result.contributions[0].pruned_array_paths,
            (
                ("listeners",),
                ("metadata", "aliases"),
                ("metadata", "nested", "ports"),
            ),
        )
        self.assertEqual(
            result.contributions[0].pruned_empty_object_paths,
            (("metadata", "nested"),),
        )
        self.assertEqual(result.contributions[0].status, "normalized")
        self.assertEqual(result.stats.received_contribution_count, 1)
        self.assertEqual(result.stats.retained_contribution_count, 1)
        self.assertEqual(result.stats.dropped_contribution_count, 0)
        self.assertEqual(result.stats.array_affected_contribution_count, 1)
        self.assertEqual(result.stats.pruned_array_field_count, 3)
        self.assertEqual(result.stats.pruned_empty_object_count, 1)
        self.assertFalse(result.all_contributions_pruned)

    def test_all_pruned_is_structured_without_inventing_a_no_op(self) -> None:
        patch = _patch(
            _contribution(
                "listeners-only",
                {
                    "listeners": ["127.0.0.1:22"],
                    "metadata": {"aliases": ["ssh"]},
                },
            )
        )

        result = normalize_integrator_patch(patch)

        self.assertIsNone(result.patch)
        self.assertTrue(result.all_contributions_pruned)
        self.assertEqual(result.stats.received_contribution_count, 1)
        self.assertEqual(result.stats.retained_contribution_count, 0)
        self.assertEqual(result.stats.dropped_contribution_count, 1)
        self.assertEqual(result.stats.pruned_array_field_count, 2)
        self.assertEqual(result.stats.pruned_empty_object_count, 1)
        self.assertEqual(result.contributions[0].status, "dropped")
        self.assertEqual(
            result.contributions[0].pruned_empty_object_paths,
            (("metadata",),),
        )

    def test_drops_only_emptied_contribution_and_preserves_order_and_provenance(
        self,
    ) -> None:
        dropped = _contribution("drop", {"members": ["one", "two"]})
        normalized = _contribution(
            "normalize",
            {"kind": "report", "tags": ["reviewed"]},
            source_item_ids=("i2", "i3"),
        )
        unchanged = _contribution(
            "unchanged",
            {"kind": "residue", "state": "present"},
            source_item_ids=("i4",),
        )
        patch = _patch(dropped, normalized, unchanged)

        result = normalize_integrator_patch(patch)

        self.assertIsNotNone(result.patch)
        assert result.patch is not None
        retained = result.patch.clusters[0].contributions
        self.assertEqual([item.id for item in retained], ["normalize", "unchanged"])
        self.assertEqual(retained[0].value, {"kind": "report"})
        self.assertEqual(retained[0].source_item_ids, ("i2", "i3"))
        self.assertEqual(retained[1], unchanged)
        self.assertEqual(
            [decision.contribution_index for decision in result.contributions],
            [0, 1, 2],
        )
        self.assertEqual(
            [decision.status for decision in result.contributions],
            ["dropped", "normalized", "unchanged"],
        )
        self.assertEqual(result.stats.received_contribution_count, 3)
        self.assertEqual(result.stats.retained_contribution_count, 2)
        self.assertEqual(result.stats.dropped_contribution_count, 1)
        self.assertEqual(result.stats.array_affected_contribution_count, 2)
        self.assertEqual(result.stats.pruned_array_field_count, 2)

    def test_invalid_empty_input_is_not_reclassified_as_all_pruned(self) -> None:
        empty = EnrichmentContribution(
            id="invalid-empty",
            registry_path=("facts_by_id",),
            key="invalid-empty",
            operation="put_if_absent",
            value={},
            source_item_ids=("i1",),
        )
        patch = _patch(empty)

        result = normalize_integrator_patch(patch)

        self.assertIs(result.patch, patch)
        self.assertFalse(result.all_contributions_pruned)
        self.assertEqual(result.contributions[0].status, "unchanged")

    def test_structural_locations_are_bounded_while_counts_remain_exact(self) -> None:
        patch = _patch(
            _contribution(
                "many-arrays",
                {
                    "kind": "bounded-location-test",
                    **{f"array-{index}": [index] for index in range(12)},
                },
            )
        )

        result = normalize_integrator_patch(patch)

        decision = result.contributions[0]
        self.assertEqual(decision.pruned_array_field_count, 12)
        self.assertEqual(result.stats.pruned_array_field_count, 12)
        self.assertEqual(
            len(decision.pruned_array_paths),
            MAX_RETAINED_NORMALIZATION_PATHS,
        )
        self.assertTrue(decision.structural_paths_truncated)


if __name__ == "__main__":
    unittest.main()
