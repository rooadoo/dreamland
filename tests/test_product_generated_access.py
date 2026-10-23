from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dreamland.product.forge import (
    BSIDES_RECIPE_ID,
    BSIDES_RECIPE_REVISION,
    DEFAULT_SELECTED_HANDOFF_POLICY_ID,
    ForgeActorSeed,
    ForgeJobState,
    ForgePolicy,
    ForgeSelectedRevisionRecord,
    ForgeServiceError,
    WorldForgeService,
    WorldSeedAnswer,
)
from dreamland.product.forge_artifacts import (
    FORGE_COMPILER_REVISION,
    FORGE_VALIDATOR_REVISION,
    ImmutableForgeStore,
    live_forge_receipt,
    live_forge_source,
)
from dreamland.product.generated_access import (
    GeneratedAccessActorSeed,
    GeneratedAccessError,
    GeneratedAccessPolicy,
    GeneratedAccessPublicationCapacityError,
    GeneratedAccessSourceUnavailable,
    GeneratedWorldAccessService,
    WorldForgeAccessSource,
)
from dreamland.state_profiles import StateProfile

OPERATOR_AUTHORITY = "generated-access-operator-authority-0001"
OPERATOR_ID = "operator:generated_access_0001"
JOB_REFERENCE = "forge-reference:selected-job-00000001"
OTHER_JOB_REFERENCE = "forge-reference:selected-job-00000002"


class _Clock:
    def __init__(self) -> None:
        self.wall_ms = int(datetime(2026, 8, 30, 12, 0, tzinfo=UTC).timestamp() * 1_000)
        self.deadline_ms = 10_000

    def wall(self) -> int:
        return self.wall_ms

    def monotonic(self) -> int:
        return self.deadline_ms

    def advance(self, milliseconds: int) -> None:
        self.wall_ms += milliseconds
        self.deadline_ms += milliseconds


class _Tokens:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self, purpose: str) -> str:
        self.index += 1
        return f"{purpose}:test_token_{self.index:016d}"


class _Source:
    def __init__(self, store: ImmutableForgeStore) -> None:
        self.store = store
        self.records: dict[str, ForgeSelectedRevisionRecord] = {}
        self.resolve_calls: list[str] = []
        self.load_calls: list[tuple[str, str]] = []
        self.transient_failures = 0
        self.advance_on_resolve_ms = 0

    def resolve_selected(self, job_reference: str) -> ForgeSelectedRevisionRecord:
        self.resolve_calls.append(job_reference)
        if self.transient_failures:
            self.transient_failures -= 1
            raise GeneratedAccessSourceUnavailable("fixture source is unavailable")
        if self.advance_on_resolve_ms:
            milliseconds = self.advance_on_resolve_ms
            self.advance_on_resolve_ms = 0
            self._advance(milliseconds)
        record = self.records.get(job_reference)
        if record is None:
            raise ForgeServiceError(
                "job_not_found",
                "The World Forge job is unavailable.",
                status=404,
            )
        return record

    def _advance(self, _milliseconds: int) -> None:
        raise RuntimeError("source clock advance hook is not configured")

    def load_revision(self, revision_id: str, immutable_digest: str):
        self.load_calls.append((revision_id, immutable_digest))
        return self.store.load(revision_id, immutable_digest)


class GeneratedWorldAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = _Clock()
        self.store = ImmutableForgeStore(self.root / "revisions")
        self.artifact = self.store.save(_profile())
        self.source = _Source(self.store)
        self.source._advance = self.clock.advance
        self.source.records[JOB_REFERENCE] = self._selected(JOB_REFERENCE)
        self.source.records[OTHER_JOB_REFERENCE] = self._selected(OTHER_JOB_REFERENCE)
        self.service = self._service()

    def test_selection_is_not_promotion_and_construction_is_inert(self) -> None:
        self.assertEqual(self.source.resolve_calls, [])
        self.assertEqual(self.source.load_calls, [])
        missing_promotion = "generated-access-promotion:not_promoted_00000001"
        missing_record = "generated-access-record:not_revealed_000000001"

        with self.assertRaises(GeneratedAccessError) as status_error:
            self.service.status(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=self.service.service_epoch,
                promotion_id=missing_promotion,
            )
        self.assertEqual(status_error.exception.code, "access_unavailable")
        with self.assertRaises(GeneratedAccessError) as resolve_error:
            self.service.resolve_revealed_revision(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=self.service.service_epoch,
                promotion_id=missing_promotion,
                access_record_id=missing_record,
            )
        self.assertEqual(resolve_error.exception.code, "access_unavailable")
        self.assertEqual(self.source.resolve_calls, [])
        self.assertEqual(self.source.load_calls, [])

    def test_promote_resolves_exact_artifact_and_projects_only_safe_truth(self) -> None:
        hidden = self._promote()

        self.assertEqual(self.source.resolve_calls, [JOB_REFERENCE])
        self.assertEqual(
            self.source.load_calls,
            [(self.artifact.revision_id, self.artifact.digest)],
        )
        self.assertEqual(
            set(hidden),
            {
                "kind",
                "service_epoch",
                "promotion_id",
                "access_record_id",
                "revision_number",
                "state",
                "origin",
                "revision",
                "created_at",
                "changed_at",
                "expires_at",
                "catalog_eligible",
                "replayed",
            },
        )
        self.assertEqual(hidden["state"], "hidden")
        self.assertEqual(hidden["revision_number"], 1)
        self.assertFalse(hidden["catalog_eligible"])
        self.assertFalse(hidden["replayed"])
        self.assertEqual(
            hidden["revision"],
            {
                "revision_id": self.artifact.revision_id,
                "sha256": self.artifact.digest.removeprefix("sha256:"),
            },
        )
        encoded = json.dumps(hidden, sort_keys=True)
        for private_value in (
            OPERATOR_AUTHORITY,
            JOB_REFERENCE,
            "selected-record-private-0001",
            str(self.artifact.path),
            "tracking-console",
        ):
            self.assertNotIn(private_value, encoded)

        current = self.service.status(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            promotion_id=str(hidden["promotion_id"]),
        )
        self.assertEqual(current, hidden)

    def test_concrete_forge_source_keeps_handoff_authority_server_side(self) -> None:
        presenter_authority = "forge-presenter-private-authority-0001"
        forge = WorldForgeService(
            actors=(
                ForgeActorSeed(
                    authority=presenter_authority,
                    actor_id="presenter:generated_access_0001",
                    expires_at=datetime.fromtimestamp(
                        (self.clock.wall_ms + 60 * 60 * 1_000) / 1_000,
                        tz=UTC,
                    ).isoformat(),
                ),
            ),
            policy=ForgePolicy(
                maximum_jobs=2,
                maximum_idempotency_records=4,
                selected_handoff_ttl_seconds=300,
            ),
            clock=lambda: datetime.fromtimestamp(self.clock.wall_ms / 1_000, tz=UTC),
            token_factory=_Tokens(),
            selected_record_token_factory=_Tokens(),
            source=live_forge_source(),
        )
        prepared = forge.prepare(
            actor_authority=presenter_authority,
            expected_epoch=forge.service_epoch,
            recipe_id=BSIDES_RECIPE_ID,
            recipe_revision=BSIDES_RECIPE_REVISION,
            answers=_answers(),
        )
        accepted = forge.confirm_and_submit(
            actor_authority=presenter_authority,
            expected_epoch=forge.service_epoch,
            idempotency_key="concrete-source-submit",
            preparation_reference=str(prepared["preparation_reference"]),
        )
        reference = str(accepted["reference"])
        for state in (
            ForgeJobState.QUEUED,
            ForgeJobState.BUILDING,
            ForgeJobState.GENERATED,
            ForgeJobState.VALIDATING,
        ):
            forge.preview_transition(reference=reference, state=state)
        forge.complete_selected(
            reference=reference,
            receipt=live_forge_receipt(
                compiler_revision=FORGE_COMPILER_REVISION,
                validator_revision=FORGE_VALIDATOR_REVISION,
                revision_id=self.artifact.revision_id,
                digest=self.artifact.digest,
            ),
        )
        access = GeneratedWorldAccessService(
            actors=(
                GeneratedAccessActorSeed(
                    authority=OPERATOR_AUTHORITY,
                    actor_id=OPERATOR_ID,
                    expires_at_ms=self.clock.wall_ms + 60 * 60 * 1_000,
                ),
            ),
            source=WorldForgeAccessSource(
                forge=forge,
                artifacts=self.store,
                forge_actor_authority=presenter_authority,
            ),
            clock_ms=self.clock.wall,
            monotonic_ms=self.clock.monotonic,
            token_factory=_Tokens(),
        )

        result = access.promote(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=access.service_epoch,
            idempotency_key="concrete-source-promote",
            job_reference=reference,
        )
        self.assertEqual(result["state"], "hidden")
        encoded = json.dumps(result)
        self.assertNotIn(presenter_authority, encoded)
        self.assertNotIn(forge.service_epoch, encoded)
        self.assertNotIn(reference, encoded)

    def test_promotion_success_replays_and_changed_body_conflicts(self) -> None:
        first = self._promote(key="promotion-idempotency")
        first_revision = first["revision"]
        self.assertIsInstance(first_revision, dict)
        first_revision["sha256"] = "caller-mutated"
        replay = self._promote(key="promotion-idempotency")

        self.assertNotEqual(replay["revision"], first_revision)
        self.assertEqual(
            replay["revision"],
            {
                "revision_id": self.artifact.revision_id,
                "sha256": self.artifact.digest.removeprefix("sha256:"),
            },
        )
        self.assertTrue(replay["replayed"])
        same_source = self._promote(key="another-key-same-selection")
        self.assertEqual(same_source["promotion_id"], replay["promotion_id"])
        self.assertEqual(same_source["access_record_id"], replay["access_record_id"])
        self.assertFalse(same_source["replayed"])
        self.assertEqual(self.source.resolve_calls, [JOB_REFERENCE])
        with self.assertRaises(GeneratedAccessError) as conflict:
            self._promote(key="promotion-idempotency", reference=OTHER_JOB_REFERENCE)
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.assertEqual(self.source.resolve_calls, [JOB_REFERENCE])

    def test_deterministic_selection_rejection_is_claimed_and_replayed(self) -> None:
        unavailable = "forge-reference:missing-selected-job-0001"
        for _attempt in range(2):
            with self.assertRaises(GeneratedAccessError) as rejected:
                self._promote(key="deterministic-rejection", reference=unavailable)
            self.assertEqual(rejected.exception.code, "selection_unavailable")
            self.assertEqual(rejected.exception.retry, "none")
        self.assertEqual(self.source.resolve_calls, [unavailable])

        with self.assertRaises(GeneratedAccessError) as conflict:
            self._promote(key="deterministic-rejection", reference=OTHER_JOB_REFERENCE)
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.assertEqual(self.source.resolve_calls, [unavailable])

    def test_delayed_deterministic_rejection_retains_key_from_response_time(
        self,
    ) -> None:
        unavailable = "forge-reference:slow-missing-selected-job-0001"
        service = self._service(
            policy=GeneratedAccessPolicy(
                max_actor_authorities=1,
                max_active_promotions=1,
                max_access_records=2,
                max_idempotency_records=4,
                max_token_history=8,
                access_ttl_ms=1_000,
                idempotency_ttl_ms=1_000,
                failed_operation_ttl_ms=100,
            )
        )
        self.source.advance_on_resolve_ms = 200

        with self.assertRaises(GeneratedAccessError) as rejected:
            self._promote(
                service=service,
                key="slow-deterministic-rejection",
                reference=unavailable,
            )
        self.assertEqual(rejected.exception.code, "selection_unavailable")
        with self.assertRaises(GeneratedAccessError) as replay:
            self._promote(
                service=service,
                key="slow-deterministic-rejection",
                reference=unavailable,
            )
        self.assertEqual(replay.exception.code, "selection_unavailable")
        self.assertEqual(self.source.resolve_calls, [unavailable])
        with self.assertRaises(GeneratedAccessError) as conflict:
            self._promote(
                service=service,
                key="slow-deterministic-rejection",
                reference=OTHER_JOB_REFERENCE,
            )
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.assertEqual(self.source.resolve_calls, [unavailable])

        self.clock.advance(101)
        recovered = self._promote(
            service=service,
            key="slow-deterministic-rejection",
            reference=OTHER_JOB_REFERENCE,
        )
        self.assertEqual(recovered["state"], "hidden")

    def test_retryable_source_failure_leaves_key_reusable_for_another_body(
        self,
    ) -> None:
        self.source.transient_failures = 1
        with self.assertRaises(GeneratedAccessError) as unavailable:
            self._promote(key="source-retry", reference=JOB_REFERENCE)
        self.assertEqual(unavailable.exception.code, "source_temporarily_unavailable")
        self.assertEqual(unavailable.exception.retry, "after_delay")

        promoted = self._promote(key="source-retry", reference=OTHER_JOB_REFERENCE)
        self.assertEqual(promoted["state"], "hidden")
        self.assertEqual(
            self.source.resolve_calls, [JOB_REFERENCE, OTHER_JOB_REFERENCE]
        )

    def test_capacity_is_unclaimed_and_recovers_after_expiry(self) -> None:
        service = self._service(
            policy=GeneratedAccessPolicy(
                max_actor_authorities=1,
                max_active_promotions=1,
                max_access_records=2,
                max_idempotency_records=4,
                max_token_history=8,
                access_ttl_ms=100,
                idempotency_ttl_ms=100,
                failed_operation_ttl_ms=50,
            )
        )
        self._promote(service=service, key="first-capacity")
        calls_before_capacity = len(self.source.resolve_calls)
        with self.assertRaises(GeneratedAccessError) as capacity:
            self._promote(
                service=service,
                key="recoverable-capacity",
                reference=OTHER_JOB_REFERENCE,
            )
        self.assertEqual(capacity.exception.code, "capacity_exhausted")
        self.assertEqual(capacity.exception.retry, "after_delay")
        self.assertEqual(len(self.source.resolve_calls), calls_before_capacity)

        self.clock.advance(101)
        recovered = self._promote(
            service=service,
            key="recoverable-capacity",
            reference=OTHER_JOB_REFERENCE,
        )
        self.assertEqual(recovered["state"], "hidden")

    def test_promotion_ttl_begins_after_slow_source_resolution(self) -> None:
        service = self._service(
            policy=GeneratedAccessPolicy(
                max_actor_authorities=1,
                max_active_promotions=1,
                max_access_records=2,
                max_idempotency_records=4,
                max_token_history=8,
                access_ttl_ms=1_000,
                idempotency_ttl_ms=1_000,
                failed_operation_ttl_ms=100,
            )
        )
        before = self.clock.wall_ms
        self.source.advance_on_resolve_ms = 2_000
        hidden = self._promote(service=service, key="slow-source-promotion")
        created_at = int(
            datetime.fromisoformat(str(hidden["created_at"])).timestamp() * 1_000
        )
        expires_at = int(
            datetime.fromisoformat(str(hidden["expires_at"])).timestamp() * 1_000
        )
        self.assertEqual(created_at, before + 2_000)
        self.assertEqual(expires_at - created_at, 1_000)
        replay = self._promote(service=service, key="slow-source-promotion")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["promotion_id"], hidden["promotion_id"])
        with self.assertRaises(GeneratedAccessError) as conflict:
            self._promote(
                service=service,
                key="slow-source-promotion",
                reference=OTHER_JOB_REFERENCE,
            )
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

    def test_promotion_never_outlives_selected_forge_record(self) -> None:
        selected = self.source.records[JOB_REFERENCE]
        selected_expires_at = self.clock.wall_ms + 250
        self.source.records[JOB_REFERENCE] = replace(
            selected,
            expires_at=datetime.fromtimestamp(
                selected_expires_at / 1_000,
                tz=UTC,
            ).isoformat(),
        )
        service = self._service(
            policy=GeneratedAccessPolicy(
                max_actor_authorities=1,
                max_active_promotions=1,
                max_access_records=2,
                max_idempotency_records=4,
                max_token_history=8,
                access_ttl_ms=1_000,
                idempotency_ttl_ms=1_000,
                failed_operation_ttl_ms=100,
            )
        )

        hidden = self._promote(service=service, key="source-bounded-access")
        expires_at = int(
            datetime.fromisoformat(str(hidden["expires_at"])).timestamp() * 1_000
        )
        self.assertEqual(expires_at, selected_expires_at)

        self.source.records[OTHER_JOB_REFERENCE] = replace(
            self.source.records[OTHER_JOB_REFERENCE],
            expires_at=datetime.fromtimestamp(
                (self.clock.wall_ms + 100) / 1_000,
                tz=UTC,
            ).isoformat(),
        )
        expiring_service = self._service()
        self.source.advance_on_resolve_ms = 101
        with self.assertRaises(GeneratedAccessError) as unavailable:
            self._promote(
                service=expiring_service,
                key="source-expired-during-resolution",
                reference=OTHER_JOB_REFERENCE,
            )
        self.assertEqual(unavailable.exception.code, "selection_unavailable")

    def test_reveal_cannot_commit_after_publisher_crosses_access_expiry(self) -> None:
        service = self._service(
            policy=GeneratedAccessPolicy(
                max_actor_authorities=1,
                max_active_promotions=1,
                max_access_records=2,
                max_idempotency_records=4,
                max_token_history=8,
                access_ttl_ms=100,
                idempotency_ttl_ms=100,
                failed_operation_ttl_ms=50,
            )
        )
        hidden = self._promote(service=service, key="expiring-reveal-promotion")
        with self.assertRaises(GeneratedAccessError) as unavailable:
            service.reveal(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=service.service_epoch,
                idempotency_key="slow-reveal-publisher",
                promotion_id=str(hidden["promotion_id"]),
                expected_access_record_id=str(hidden["access_record_id"]),
                publish=lambda _eligibility: self.clock.advance(100),
            )
        self.assertEqual(unavailable.exception.code, "reveal_unavailable")

    def test_configured_promotion_capacity_preserves_reveal_recovery_slots(
        self,
    ) -> None:
        service = self._service(
            policy=GeneratedAccessPolicy(
                max_actor_authorities=1,
                max_active_promotions=2,
                max_access_records=4,
                max_idempotency_records=4,
                max_token_history=7,
                access_ttl_ms=1_000,
                idempotency_ttl_ms=1_000,
                failed_operation_ttl_ms=100,
            )
        )
        hidden = (
            self._promote(service=service, key="promote-one"),
            self._promote(
                service=service,
                key="promote-two",
                reference=OTHER_JOB_REFERENCE,
            ),
        )

        revealed = tuple(
            service.reveal(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=service.service_epoch,
                idempotency_key=f"reveal-{index}",
                promotion_id=str(record["promotion_id"]),
                expected_access_record_id=str(record["access_record_id"]),
                publish=lambda _eligibility: None,
            )
            for index, record in enumerate(hidden)
        )
        self.assertEqual([record["state"] for record in revealed], ["revealed"] * 2)

    def test_reveal_is_immutable_cas_and_idempotent(self) -> None:
        hidden = self._promote()
        with self.assertRaises(GeneratedAccessError) as not_revealed:
            self._resolve(hidden)
        self.assertEqual(not_revealed.exception.code, "access_unavailable")

        revealed = self._reveal(hidden, key="reveal-idempotency")
        self.assertEqual(hidden["state"], "hidden")
        self.assertEqual(hidden["revision_number"], 1)
        self.assertNotEqual(revealed["access_record_id"], hidden["access_record_id"])
        self.assertEqual(revealed["revision_number"], 2)
        self.assertEqual(revealed["state"], "revealed")
        self.assertTrue(revealed["catalog_eligible"])

        replay = self._reveal(hidden, key="reveal-idempotency")
        self.assertEqual(replay["access_record_id"], revealed["access_record_id"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(GeneratedAccessError) as changed_body:
            self.service.reveal(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=self.service.service_epoch,
                idempotency_key="reveal-idempotency",
                promotion_id=str(hidden["promotion_id"]),
                expected_access_record_id=str(revealed["access_record_id"]),
                publish=lambda _eligibility: None,
            )
        self.assertEqual(changed_body.exception.code, "idempotency_conflict")

        idempotent_again = self.service.reveal(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            idempotency_key="second-reveal-key",
            promotion_id=str(hidden["promotion_id"]),
            expected_access_record_id=str(revealed["access_record_id"]),
            publish=lambda _eligibility: None,
        )
        self.assertEqual(
            idempotent_again["access_record_id"], revealed["access_record_id"]
        )
        with self.assertRaises(GeneratedAccessError) as stale_cas:
            self._reveal(hidden, key="stale-cas")
        self.assertEqual(stale_cas.exception.code, "access_record_changed")
        with self.assertRaises(GeneratedAccessError) as stale_cas_replay:
            self._reveal(hidden, key="stale-cas")
        self.assertEqual(stale_cas_replay.exception.code, "access_record_changed")

    def test_reveal_commits_only_after_publication_and_retries_capacity(self) -> None:
        service = self._service(
            policy=GeneratedAccessPolicy(
                max_actor_authorities=1,
                max_active_promotions=1,
                max_access_records=2,
                max_idempotency_records=4,
                max_token_history=4,
                access_ttl_ms=1_000,
                idempotency_ttl_ms=1_000,
                failed_operation_ttl_ms=100,
            )
        )
        hidden = self._promote(service=service)
        publications = []

        def full(eligibility):
            publications.append(eligibility)
            raise GeneratedAccessPublicationCapacityError()

        with self.assertRaises(GeneratedAccessError) as capacity:
            service.reveal(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=service.service_epoch,
                idempotency_key="publication-capacity",
                promotion_id=str(hidden["promotion_id"]),
                expected_access_record_id=str(hidden["access_record_id"]),
                publish=full,
            )
        self.assertEqual(capacity.exception.code, "publication_capacity_exhausted")
        self.assertEqual(capacity.exception.retry, "after_delay")
        current = service.status(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=service.service_epoch,
            promotion_id=str(hidden["promotion_id"]),
        )
        self.assertEqual(current["state"], "hidden")
        self.assertEqual(current["access_record_id"], hidden["access_record_id"])

        revealed = service.reveal(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=service.service_epoch,
            idempotency_key="publication-capacity",
            promotion_id=str(hidden["promotion_id"]),
            expected_access_record_id=str(hidden["access_record_id"]),
            publish=publications.append,
        )
        self.assertEqual(revealed["state"], "revealed")
        self.assertEqual(len(publications), 2)
        self.assertEqual(
            publications[-1].access_record_id,
            revealed["access_record_id"],
        )

    def test_trusted_resolution_requires_exact_current_reveal_and_detaches(
        self,
    ) -> None:
        hidden = self._promote()
        revealed = self._reveal(hidden)
        eligibility = self._resolve(revealed)

        self.assertEqual(eligibility.revision_id, self.artifact.revision_id)
        self.assertEqual(eligibility.immutable_digest, self.artifact.digest)
        self.assertNotIn("tracking-console", repr(eligibility))
        first = eligibility.allocate_document()
        first["profile"] = {"mutated": True}
        second = self._resolve(revealed).allocate_document()
        self.assertNotEqual(second["profile"], {"mutated": True})

        with self.assertRaises(GeneratedAccessError) as historical:
            self._resolve(hidden)
        self.assertEqual(historical.exception.code, "access_unavailable")

    def test_concurrent_reveal_has_one_cas_winner(self) -> None:
        hidden = self._promote()
        barrier = threading.Barrier(3)
        results: list[dict[str, object]] = []
        errors: list[str] = []

        def reveal(index: int) -> None:
            barrier.wait()
            try:
                results.append(self._reveal(hidden, key=f"concurrent-{index}"))
            except GeneratedAccessError as error:
                errors.append(error.code)

        threads = [threading.Thread(target=reveal, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(results), 1)
        self.assertEqual(errors, ["access_record_changed"])
        current = self.service.status(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            promotion_id=str(hidden["promotion_id"]),
        )
        self.assertEqual(current["state"], "revealed")
        self.assertEqual(current["revision_number"], 2)

    def test_authority_epoch_dual_clock_and_expiry_fail_closed(self) -> None:
        with self.assertRaises(GeneratedAccessError) as denied:
            self.service.promote(
                actor_authority="wrong-operator-authority-0000000001",
                expected_epoch=self.service.service_epoch,
                idempotency_key="denied",
                job_reference=JOB_REFERENCE,
            )
        self.assertEqual(denied.exception.code, "authority_denied")
        self.assertEqual(self.source.resolve_calls, [])

        hidden = self._promote(key="before-rotation")
        old_epoch = self.service.service_epoch
        new_epoch = self.service.preview_rotate_epoch()
        self.assertNotEqual(new_epoch, old_epoch)
        with self.assertRaises(GeneratedAccessError) as stale_mutation:
            self.service.reveal(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=old_epoch,
                idempotency_key="stale-reveal",
                promotion_id=str(hidden["promotion_id"]),
                expected_access_record_id=str(hidden["access_record_id"]),
                publish=lambda _eligibility: None,
            )
        self.assertEqual(stale_mutation.exception.code, "outcome_unknown")
        with self.assertRaises(GeneratedAccessError) as stale_read:
            self.service.status(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=old_epoch,
                promotion_id=str(hidden["promotion_id"]),
            )
        self.assertEqual(stale_read.exception.code, "service_epoch_changed")
        with self.assertRaises(GeneratedAccessError) as lost:
            self.service.status(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=new_epoch,
                promotion_id=str(hidden["promotion_id"]),
            )
        self.assertEqual(lost.exception.code, "access_unavailable")

        expiring = self._service(actor_ttl_ms=100)
        promoted = self._promote(service=expiring, key="actor-expiry")
        self.clock.deadline_ms += 100
        with self.assertRaises(GeneratedAccessError) as actor_expired:
            expiring.status(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=expiring.service_epoch,
                promotion_id=str(promoted["promotion_id"]),
            )
        self.assertEqual(actor_expired.exception.code, "authority_denied")

    def test_invalid_artifact_is_a_claimed_deterministic_rejection(self) -> None:
        invalid = replace(
            self.source.records[JOB_REFERENCE],
            immutable_digest="sha256:" + "f" * 64,
        )
        self.source.records[JOB_REFERENCE] = invalid
        for _attempt in range(2):
            with self.assertRaises(GeneratedAccessError) as rejected:
                self._promote(key="invalid-artifact")
            self.assertEqual(rejected.exception.code, "revision_unavailable")
        self.assertEqual(self.source.resolve_calls, [JOB_REFERENCE])
        self.assertEqual(
            self.source.load_calls,
            [(invalid.revision_id, invalid.immutable_digest)],
        )

    def test_policy_rejects_uncovered_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "hidden and revealed"):
            GeneratedAccessPolicy(
                max_active_promotions=2,
                max_access_records=3,
            )
        with self.assertRaisesRegex(ValueError, "access lifetime"):
            GeneratedAccessPolicy(
                access_ttl_ms=1_000,
                idempotency_ttl_ms=999,
            )
        with self.assertRaisesRegex(ValueError, "promotion and reveal recovery"):
            GeneratedAccessPolicy(
                max_active_promotions=2,
                max_access_records=4,
                max_idempotency_records=3,
            )
        with self.assertRaisesRegex(ValueError, "safe identifiers"):
            GeneratedAccessPolicy(accepted_source_kinds=frozenset({"bad source"}))

    def _service(
        self,
        *,
        policy: GeneratedAccessPolicy | None = None,
        actor_ttl_ms: int = 2 * 60 * 60 * 1_000,
    ) -> GeneratedWorldAccessService:
        return GeneratedWorldAccessService(
            actors=(
                GeneratedAccessActorSeed(
                    authority=OPERATOR_AUTHORITY,
                    actor_id=OPERATOR_ID,
                    expires_at_ms=self.clock.wall_ms + actor_ttl_ms,
                ),
            ),
            source=self.source,
            policy=policy,
            clock_ms=self.clock.wall,
            monotonic_ms=self.clock.monotonic,
            token_factory=_Tokens(),
        )

    def _selected(self, job_reference: str) -> ForgeSelectedRevisionRecord:
        selected_at = datetime.fromtimestamp(self.clock.wall_ms / 1_000, tz=UTC)
        return ForgeSelectedRevisionRecord(
            selected_record_id="selected-record-private-0001",
            status="selected",
            service_epoch="forge-epoch-private-00000001",
            job_id="forge-job-private-00000001",
            job_reference=job_reference,
            actor_id="presenter:private_0001",
            selected_at=selected_at.isoformat(),
            expires_at=(selected_at + timedelta(hours=1)).isoformat(),
            policy_id=DEFAULT_SELECTED_HANDOFF_POLICY_ID,
            revision_id=self.artifact.revision_id,
            immutable_digest=self.artifact.digest,
            source_kind="single_attempt_live_vertical_slice",
            source_fingerprint="sha256:" + "1" * 64,
            receipt_correlation_id="forge-receipt-private-00000001",
            receipt_fingerprint="sha256:" + "2" * 64,
        )

    def _promote(
        self,
        *,
        service: GeneratedWorldAccessService | None = None,
        key: str = "promote-generated-world",
        reference: str = JOB_REFERENCE,
    ) -> dict[str, object]:
        target = service or self.service
        return target.promote(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=target.service_epoch,
            idempotency_key=key,
            job_reference=reference,
        )

    def _reveal(
        self,
        record: dict[str, object],
        *,
        key: str = "reveal-generated-world",
    ) -> dict[str, object]:
        return self.service.reveal(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            idempotency_key=key,
            promotion_id=str(record["promotion_id"]),
            expected_access_record_id=str(record["access_record_id"]),
            publish=lambda _eligibility: None,
        )

    def _resolve(self, record: dict[str, object]):
        return self.service.resolve_revealed_revision(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            promotion_id=str(record["promotion_id"]),
            access_record_id=str(record["access_record_id"]),
        )


def _profile() -> StateProfile:
    return StateProfile(
        name="generated-access-candidate",
        level="world",
        created_at="2026-08-30T12:00:00+00:00",
        source_session_id="source-session-not-retained",
        state={
            "world_state": {
                "name": "generated-access-world",
                "entry_host": "tracking-console",
                "active_host": "tracking-console",
                "purpose": {"theme": "Generated access authority test"},
                "hosts_by_name": {
                    "tracking-console": {
                        "login_banner": "Test console\n",
                        "hostname": "tracking-console",
                        "purpose": {"theme": "Track immutable access"},
                        "shell": {
                            "default": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["bash"],
                            }
                        },
                    }
                },
            }
        },
    )


def _answers() -> tuple[WorldSeedAnswer, ...]:
    return (
        WorldSeedAnswer("day-or-date", "short-text", "Saturday"),
        WorldSeedAnswer("year", "integer", "+02026"),
        WorldSeedAnswer("adjective", "short-text", "weathered"),
        WorldSeedAnswer("industry-or-setting", "short-text", "observatory"),
        WorldSeedAnswer("technology-one", "short-text", "VAX terminals"),
        WorldSeedAnswer("technology-two", "short-text", "radio clocks"),
        WorldSeedAnswer("important-job", "short-text", "track a comet"),
        WorldSeedAnswer("wildcard-event", "short-text", "the clocks disagree"),
    )


if __name__ == "__main__":
    unittest.main()
