from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from dreamland.product.admission import (
    AdmissionDenialReason,
    ProductAdmissionDenied,
    ProductAdmissionLedger,
    ProductAdmissionLimits,
    ProductCapacityClass,
    ProductJobCategory,
)
from dreamland.product.core import ProductCorePolicy
from dreamland.product.execution import (
    ProductExecutionResult,
    ProductStateEffect,
    ProductStateEffectKind,
)
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductOperation,
    ProductRole,
)
from dreamland.product.stage import (
    PresenterStageService,
    StageAuthoredIdentitySeed,
    StageDisplayMetadataSeed,
    StageForgeIdentitySeed,
    StageLaunchSelectionIdentitySeed,
    StageLaunchSelectionOrigin,
)
from dreamland.product.xray import StageXrayPolicy, build_stage_xray_projection

PRESENTER_AUTHORITY = "presenter-authority-token-0001"
CREATE_GRANT = "presenter-stage-create-grant-0001"
OTHER_CREATE_GRANT = "presenter-stage-create-grant-0002"
SWITCH_GRANT = "presenter-stage-switch-grant-0001"


class FakeClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class SequenceTokens:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self, purpose: str) -> str:
        self.index += 1
        return f"{purpose}-opaque-value-{self.index:08d}"


class ManualTasks:
    def __init__(self) -> None:
        self.tasks: list[Callable[[], None]] = []

    def __call__(self, task: Callable[[], None]) -> None:
        self.tasks.append(task)


class SuccessfulExecutor:
    def execute(self, _request, _emit):
        return ProductExecutionResult.completed_exited(
            exit_status=0,
            usage_tokens=10,
            usage_spend_units=10,
        )


class FixedExecutor:
    def __init__(self, result: ProductExecutionResult) -> None:
        self.result = result

    def execute(self, _request, _emit):
        return self.result


class PresenterStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tasks = ManualTasks()
        self.admission = _stage_admission()
        self.service = _stage_service(
            clock=self.clock,
            tasks=self.tasks,
            admission=self.admission,
        )

    def test_import_and_construction_are_explicit_and_inert(self) -> None:
        code = """
import json
import sys
import threading

threads_before = tuple(thread.name for thread in threading.enumerate())
from dreamland.product.stage import StageDisplayMetadataSeed
metadata = StageDisplayMetadataSeed("revision-a", "Demo", "Authored", "A")
print(json.dumps({
    "threads_before": threads_before,
    "threads_after": tuple(thread.name for thread in threading.enumerate()),
    "title": metadata.title,
}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["threads_before"], payload["threads_after"])
        self.assertEqual(payload["title"], "Demo")

    def test_launch_create_replay_and_fresh_exact_resume_share_one_session(
        self,
    ) -> None:
        created = self._launch(CREATE_GRANT, "launch-1")
        replay = self._launch(CREATE_GRANT, "launch-1")
        resumed = self._launch(CREATE_GRANT, "launch-2")

        self.assertEqual(created["kind"], "stage_session_created")
        self.assertFalse(created["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(resumed["kind"], "stage_session_resumed")
        self.assertFalse(resumed["replayed"])
        self.assertEqual(
            created["snapshot"]["session_id"], resumed["snapshot"]["session_id"]
        )
        self.assertEqual(created["session_authority"], resumed["session_authority"])
        self.assertEqual(self.admission.snapshot().active_sessions, 1)

    def test_same_revision_different_selection_never_resumes_implicitly(self) -> None:
        created = self._launch(CREATE_GRANT, "launch-a")

        with self.assertRaises(ProductLifecycleError) as captured:
            self._launch(OTHER_CREATE_GRANT, "launch-b")

        error = captured.exception
        self.assertEqual(error.code, "current_session_exists")
        self.assertEqual(
            error.context,
            {
                "current_selected_record_id": "selected-stage-a",
                "requested_selected_record_id": "selected-stage-a-alternate",
            },
        )
        with self.assertRaises(ProductLifecycleError) as replayed:
            self._launch(OTHER_CREATE_GRANT, "launch-b")
        self.assertEqual(replayed.exception.context, error.context)
        self.assertEqual(
            self.service.current(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch=self.service.service_epoch,
            )["snapshot"]["session_id"],
            created["snapshot"]["session_id"],
        )

    def test_concurrent_launch_retries_allocate_exactly_one_stage(self) -> None:
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = tuple(
                executor.map(
                    lambda _index: self._launch(CREATE_GRANT, "shared-launch"),
                    range(12),
                )
            )

        self.assertEqual(sum(not result["replayed"] for result in results), 1)
        self.assertEqual(
            {result["snapshot"]["session_id"] for result in results},
            {results[0]["snapshot"]["session_id"]},
        )
        self.assertEqual(self.admission.snapshot().active_sessions, 1)

    def test_concurrent_fresh_exact_launches_resume_without_duplication(self) -> None:
        created = self._launch(CREATE_GRANT, "initial-launch")

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = tuple(
                executor.map(
                    lambda index: self._launch(CREATE_GRANT, f"resume-{index}"),
                    range(12),
                )
            )

        self.assertTrue(
            all(result["kind"] == "stage_session_resumed" for result in results)
        )
        self.assertEqual(
            {result["snapshot"]["session_id"] for result in results},
            {created["snapshot"]["session_id"]},
        )
        self.assertEqual(self.admission.snapshot().active_sessions, 1)

    def test_explicit_stage_replace_uses_control_cas_and_revokes_old_authority(
        self,
    ) -> None:
        created = self._launch(CREATE_GRANT, "launch-a")
        old_snapshot = created["snapshot"]
        replaced = self.service.replace(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=created["session_authority"],
            reference=old_snapshot["reference"],
            expected_epoch=self.service.service_epoch,
            idempotency_key="replace-stage",
            expected_control_revision=old_snapshot["coordinates"]["control_revision"],
            launch_grant=SWITCH_GRANT,
        )

        self.assertEqual(replaced["kind"], "stage_session_replaced")
        self.assertEqual(
            replaced["snapshot"]["starting_revision"]["revision_id"],
            "revision-b",
        )
        self.assertEqual(
            replaced["snapshot"]["stage"]["display"]["title"],
            "Second Demonstration World",
        )
        resumed = self._launch(SWITCH_GRANT, "resume-replacement")
        self.assertEqual(resumed["kind"], "stage_session_resumed")
        self.assertEqual(
            resumed["snapshot"]["session_id"],
            replaced["snapshot"]["session_id"],
        )
        with self.assertRaises(ProductLifecycleError) as stale:
            self.service.snapshot(
                actor_authority=PRESENTER_AUTHORITY,
                session_authority=created["session_authority"],
                reference=old_snapshot["reference"],
                expected_epoch=self.service.service_epoch,
            )
        self.assertEqual(stale.exception.code, "authority_denied")

    def test_stage_snapshot_is_bounded_metadata_not_raw_world_or_xray(self) -> None:
        snapshot = self._launch(CREATE_GRANT, "launch-a")["snapshot"]
        serialized = json.dumps(snapshot, sort_keys=True)

        self.assertEqual(
            snapshot["stage"],
            {
                "reference": snapshot["reference"],
                "display": {
                    "title": "Presenter Demonstration World",
                    "source_label": "Authored",
                    "revision_label": "Demo A",
                },
                "admission_class": "presenter_stage_reserved",
                "xray_available": False,
            },
        )
        self.assertNotIn("private-world-value", serialized)
        self.assertNotIn("world_state", serialized)
        self.assertNotIn("host_graph", serialized)

    def test_public_saturation_cannot_consume_reserved_stage_capacity(self) -> None:
        self.admission.admit_session("public-session-1")
        self.admission.admit_session("public-session-2")
        with self.assertRaises(ProductAdmissionDenied) as public_session_denial:
            self.admission.admit_session("public-session-3")
        self.assertEqual(
            public_session_denial.exception.reason,
            AdmissionDenialReason.SESSION_CAPACITY_EXHAUSTED,
        )
        self.admission.reserve_and_claim_worker(
            logical_job_id="public-job-1",
            session_id="public-session-1",
            category=ProductJobCategory.FOREGROUND_PRODUCT,
            reserved_tokens=200,
            reserved_spend_units=200,
        )
        self.admission.reserve_job(
            logical_job_id="public-job-2",
            session_id="public-session-2",
            category=ProductJobCategory.PRODUCT_ENRICHMENT,
            reserved_tokens=0,
            reserved_spend_units=0,
        )

        created = self._launch(CREATE_GRANT, "reserved-stage-launch")
        accepted = self.service.submit_command(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=self.service.service_epoch,
            idempotency_key="reserved-stage-command",
            expected_slot_generation=0,
            command="whoami",
        )

        self.assertEqual(accepted["kind"], "command_accepted")
        snapshot = self.admission.snapshot()
        self.assertEqual(snapshot.active_sessions, 3)
        self.assertEqual(snapshot.logical_jobs, 3)
        self.assertEqual(snapshot.physical_workers, 2)
        self.assertEqual(snapshot.reserved_tokens, 300)
        self.assertEqual(snapshot.reserved_spend_units, 300)

    def test_stage_partition_itself_fails_bounded_when_exhausted(self) -> None:
        self.admission.admit_session(
            "other-stage",
            capacity_class=ProductCapacityClass.PRESENTER_STAGE,
        )

        with self.assertRaises(ProductLifecycleError) as captured:
            self._launch(CREATE_GRANT, "stage-capacity-full")

        self.assertEqual(captured.exception.code, "capacity_exhausted")
        self.assertEqual(self.admission.snapshot().active_sessions, 1)

    def _launch(self, grant: str, key: str) -> dict[str, object]:
        return self.service.launch(
            actor_authority=PRESENTER_AUTHORITY,
            launch_grant=grant,
            expected_epoch=self.service.service_epoch,
            idempotency_key=key,
        )


class PresenterStageDynamicProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tasks = ManualTasks()
        self.service = _dynamic_stage_service(
            clock=self.clock,
            tasks=self.tasks,
            admission=_stage_admission(),
        )

    def test_ordinary_stage_still_rejects_an_empty_fixed_catalog(self) -> None:
        with self.assertRaisesRegex(ValueError, "grants must all"):
            _dynamic_stage_service(
                clock=self.clock,
                tasks=self.tasks,
                admission=_stage_admission(),
                dynamic_selection_only=False,
            )
        with self.assertRaisesRegex(ValueError, "cannot have fixed revisions"):
            _stage_service(
                clock=self.clock,
                tasks=self.tasks,
                admission=_stage_admission(),
                dynamic_selection_only=True,
            )

    def test_dynamic_stage_starts_empty_and_cannot_fall_back(self) -> None:
        self.assertEqual(self.service._revisions, {})
        self.assertEqual(self.service._launch_grants, {})
        self.assertEqual(self.service._stage_display_metadata, {})

        with self.assertRaises(ProductLifecycleError) as captured:
            self.service.launch(
                actor_authority=PRESENTER_AUTHORITY,
                launch_grant="unprovisioned-stage-grant-0001",
                expected_epoch=self.service.service_epoch,
                idempotency_key="no-fallback",
            )

        self.assertEqual(captured.exception.code, "authority_denied")
        self.assertEqual(self.service._revisions, {})
        self.assertEqual(self.service._launch_grants, {})
        self.assertEqual(self.service._stage_display_metadata, {})
        self.assertEqual(self.service._stage_forge_sources, {})

    def test_provision_is_exactly_idempotent_and_launch_projects_forge_source(
        self,
    ) -> None:
        revision, grant, metadata, source = _forge_selection()

        provisioned = self.service.provision_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            revision=revision,
            launch_grant=grant,
            display_metadata=metadata,
            forge_identity=source,
        )
        reused = self.service.provision_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            revision=revision,
            launch_grant=grant,
            display_metadata=metadata,
            forge_identity=source,
        )

        self.assertFalse(provisioned.reused)
        self.assertTrue(reused.reused)
        self.assertEqual(provisioned.service_epoch, self.service.service_epoch)
        self.assertEqual(provisioned.immutable_digest, source.immutable_digest)
        self.assertEqual(len(self.service._revisions), 1)
        self.assertEqual(len(self.service._launch_grants), 1)
        launched = self.service.launch(
            actor_authority=PRESENTER_AUTHORITY,
            launch_grant=grant.token,
            expected_epoch=self.service.service_epoch,
            idempotency_key="forge-launch",
        )
        snapshot = launched["snapshot"]
        self.assertEqual(
            snapshot["stage"],
            {
                "reference": snapshot["reference"],
                "display": {
                    "title": "Live Forged World",
                    "source_label": "World Forge",
                    "revision_label": "Selected candidate A",
                },
                "admission_class": "presenter_stage_reserved",
                "xray_available": False,
                "source": {
                    "kind": "world_forge_selection",
                    "selected_record_id": "selected-forge-record-0001",
                    "immutable_digest": source.immutable_digest,
                    "forge_job_id": "forge-job:live-00000001",
                    "forge_reference": "forge-reference:live-00000001",
                },
            },
        )
        self.assertEqual(
            snapshot["starting_revision"]["revision_id"],
            revision.revision_id,
        )
        self.assertNotEqual(
            snapshot["starting_revision"]["sha256"],
            source.immutable_digest.removeprefix("sha256:"),
        )

    def test_authored_snapshot_provisions_and_launches_without_source_leak(
        self,
    ) -> None:
        revision, grant, metadata, source = _authored_selection()

        provisioned = self.service.provision_authored_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            revision=revision,
            launch_grant=grant,
            display_metadata=metadata,
            authored_identity=source,
        )
        later = _authored_selection(serial=2)
        self.service.provision_authored_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            revision=later[0],
            launch_grant=later[1],
            display_metadata=later[2],
            authored_identity=later[3],
        )
        reused = self.service.provision_authored_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            revision=revision,
            launch_grant=grant,
            display_metadata=metadata,
            authored_identity=source,
        )

        self.assertFalse(provisioned.reused)
        self.assertTrue(reused.reused)
        self.assertEqual(self.service._source_neutral_launch_serial_high_water, 2)
        launched = self.service.launch(
            actor_authority=PRESENTER_AUTHORITY,
            launch_grant=grant.token,
            expected_epoch=self.service.service_epoch,
            idempotency_key="authored-launch",
        )
        snapshot = launched["snapshot"]
        self.assertEqual(
            snapshot["starting_revision"]["revision_id"], revision.revision_id
        )
        self.assertNotIn("source", snapshot["stage"])
        serialized = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn("profile", serialized)
        self.assertNotIn("path", serialized)
        self.assertEqual(self.service._stage_forge_sources, {})

    def test_authored_snapshot_rejects_wrong_kind_or_digest_without_mutation(
        self,
    ) -> None:
        revision, grant, metadata, source = _authored_selection()
        wrong_kind = _forge_selection(
            token=grant.token,
            selected_record_id=grant.selected_record_id,
        )[0]
        cases = (
            (wrong_kind, grant, metadata, source, "registered origin"),
            (
                revision,
                grant,
                metadata,
                StageAuthoredIdentitySeed(
                    selected_record_id=source.selected_record_id,
                    immutable_digest="sha256:" + "0" * 64,
                ),
                "digest does not match",
            ),
        )
        for (
            candidate_revision,
            candidate_grant,
            candidate_metadata,
            identity,
            message,
        ) in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.service.provision_authored_selection(
                        actor_authority=PRESENTER_AUTHORITY,
                        expected_epoch=self.service.service_epoch,
                        revision=candidate_revision,
                        launch_grant=candidate_grant,
                        display_metadata=candidate_metadata,
                        authored_identity=identity,
                    )
                self.assertEqual(self._registry_state(), (0, 0, 0, 0, 0, 0))

    def test_source_neutral_selection_provisions_forge_without_fake_provenance(
        self,
    ) -> None:
        token = _source_neutral_launch_grant(1)
        selected_record_id = _source_neutral_selected_record(token)
        revision, grant, metadata, source = _forge_selection(
            token=token,
            selected_record_id=selected_record_id,
        )

        result = self.service.provision_launch_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            revision=revision,
            launch_grant=grant,
            display_metadata=metadata,
            selection_identity=StageLaunchSelectionIdentitySeed(
                selected_record_id=source.selected_record_id,
                immutable_digest=source.immutable_digest,
                origin=StageLaunchSelectionOrigin.WORLD_FORGE_SELECTION,
            ),
        )

        self.assertFalse(result.reused)
        launched = self.service.launch(
            actor_authority=PRESENTER_AUTHORITY,
            launch_grant=grant.token,
            expected_epoch=self.service.service_epoch,
            idempotency_key="registered-forge-launch",
        )
        self.assertNotIn("source", launched["snapshot"]["stage"])
        self.assertEqual(self.service._stage_forge_sources, {})

    def test_source_neutral_grant_format_and_selected_record_are_exact(self) -> None:
        malformed = _authored_selection(token="launch-grant:not-the-final-format")
        wrong_record = _authored_selection(
            selected_record_id="selected-record:" + "0" * 64
        )
        zero_serial = _authored_selection(serial=0)

        for selection, message in (
            (malformed, "registered format"),
            (wrong_record, "must bind the launch grant"),
            (zero_serial, "serial is not newer"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.service.provision_authored_selection(
                        actor_authority=PRESENTER_AUTHORITY,
                        expected_epoch=self.service.service_epoch,
                        revision=selection[0],
                        launch_grant=selection[1],
                        display_metadata=selection[2],
                        authored_identity=selection[3],
                    )

        self.assertEqual(self._registry_state(), (0, 0, 0, 0, 0, 0))
        self.assertEqual(self.service._source_neutral_stage_grants, set())
        self.assertEqual(self.service._source_neutral_launch_serial_high_water, 0)

    def test_source_neutral_retention_stays_bounded_across_many_grants(self) -> None:
        service = _dynamic_stage_service(
            clock=self.clock,
            tasks=self.tasks,
            admission=_stage_admission(),
            max_revisions=1,
            max_launch_grants=2,
        )
        baseline_digest_count = len(service._used_value_digests)

        for serial in range(1, 25):
            selection = _authored_selection(
                serial=serial,
                expires_at_ms=self.clock.now_ms + 1,
            )
            provisioned = service.provision_authored_selection(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch=service.service_epoch,
                revision=selection[0],
                launch_grant=selection[1],
                display_metadata=selection[2],
                authored_identity=selection[3],
            )

            self.assertFalse(provisioned.reused)
            self.assertEqual(len(service._launch_grants), 1)
            self.assertEqual(len(service._dynamic_stage_grants), 1)
            self.assertEqual(len(service._source_neutral_stage_grants), 1)
            self.assertEqual(len(service._dynamic_launch_by_selection), 1)
            self.assertEqual(
                len(service._used_value_digests), baseline_digest_count + 1
            )
            self.assertEqual(service._source_neutral_launch_serial_high_water, serial)
            self.clock.now_ms += 1

    def test_expired_source_neutral_token_cannot_be_reprovisioned(self) -> None:
        first = _authored_selection(serial=1, expires_at_ms=2_000)
        self.service.provision_authored_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            revision=first[0],
            launch_grant=first[1],
            display_metadata=first[2],
            authored_identity=first[3],
        )
        first_digest = hashlib.sha256(first[1].token.encode("utf-8")).hexdigest()

        self.clock.now_ms = 2_000
        second = _authored_selection(serial=2, expires_at_ms=3_000)
        self.service.provision_authored_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            revision=second[0],
            launch_grant=second[1],
            display_metadata=second[2],
            authored_identity=second[3],
        )
        self.assertNotIn(first_digest, self.service._used_value_digests)
        self.assertNotIn(first_digest, self.service._source_neutral_stage_grants)

        stale = _authored_selection(serial=1, expires_at_ms=4_000)
        with self.assertRaisesRegex(ValueError, "serial is not newer"):
            self.service.provision_authored_selection(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch=self.service.service_epoch,
                revision=stale[0],
                launch_grant=stale[1],
                display_metadata=stale[2],
                authored_identity=stale[3],
            )
        self.assertEqual(self.service._source_neutral_launch_serial_high_water, 2)

    def test_forward_wall_adjustment_expires_dynamic_grant_and_actor(self) -> None:
        wall = FakeClock()
        monotonic = FakeClock()
        service = _dynamic_stage_service(
            clock=wall,
            monotonic_clock=monotonic,
            tasks=self.tasks,
            admission=_stage_admission(),
        )
        selection = _authored_selection(expires_at_ms=2_000)
        service.provision_authored_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=service.service_epoch,
            revision=selection[0],
            launch_grant=selection[1],
            display_metadata=selection[2],
            authored_identity=selection[3],
        )

        wall.now_ms = 2_000
        with self.assertRaises(ProductLifecycleError) as expired_grant:
            service.launch(
                actor_authority=PRESENTER_AUTHORITY,
                launch_grant=selection[1].token,
                expected_epoch=service.service_epoch,
                idempotency_key="forward-wall-grant-expiry",
            )
        self.assertEqual(expired_grant.exception.code, "authority_denied")
        self.assertEqual(service._dynamic_stage_grants, set())

        wall.now_ms = 100_000
        with self.assertRaises(ProductLifecycleError) as expired_actor:
            service.launch(
                actor_authority=PRESENTER_AUTHORITY,
                launch_grant=selection[1].token,
                expected_epoch=service.service_epoch,
                idempotency_key="forward-wall-actor-expiry",
            )
        self.assertEqual(expired_actor.exception.code, "authority_denied")

    def test_forward_then_rollback_cannot_extend_dynamic_grant(self) -> None:
        wall = FakeClock()
        monotonic = FakeClock()
        service = _dynamic_stage_service(
            clock=wall,
            monotonic_clock=monotonic,
            tasks=self.tasks,
            admission=_stage_admission(),
        )
        wall.now_ms = 1_500
        selection = _authored_selection(expires_at_ms=2_000)
        service.provision_authored_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=service.service_epoch,
            revision=selection[0],
            launch_grant=selection[1],
            display_metadata=selection[2],
            authored_identity=selection[3],
        )

        wall.now_ms = 1_000
        monotonic.now_ms = 1_500
        with self.assertRaises(ProductLifecycleError) as expired:
            service.launch(
                actor_authority=PRESENTER_AUTHORITY,
                launch_grant=selection[1].token,
                expected_epoch=service.service_epoch,
                idempotency_key="forward-rollback-grant-expiry",
            )
        self.assertEqual(expired.exception.code, "authority_denied")
        self.assertEqual(service._dynamic_stage_grants, set())

    def test_legacy_forge_wall_rollback_keeps_startup_deadline(self) -> None:
        wall = FakeClock()
        monotonic = FakeClock()
        service = _dynamic_stage_service(
            clock=wall,
            monotonic_clock=monotonic,
            tasks=self.tasks,
            admission=_stage_admission(),
        )
        wall.now_ms = 500
        selection = _forge_selection(expires_at_ms=2_000)
        service.provision_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=service.service_epoch,
            revision=selection[0],
            launch_grant=selection[1],
            display_metadata=selection[2],
            forge_identity=selection[3],
        )
        digest = hashlib.sha256(selection[1].token.encode("utf-8")).hexdigest()
        self.assertEqual(service._launch_grants[digest].expires_deadline_ms, 2_000)

        monotonic.now_ms = 2_000
        with self.assertRaises(ProductLifecycleError) as expired:
            service.launch(
                actor_authority=PRESENTER_AUTHORITY,
                launch_grant=selection[1].token,
                expected_epoch=service.service_epoch,
                idempotency_key="legacy-rollback-grant-expiry",
            )
        self.assertEqual(expired.exception.code, "authority_denied")
        self.assertEqual(service._dynamic_stage_grants, set())

    def test_source_neutral_compatibility_rollback_keeps_startup_deadline(
        self,
    ) -> None:
        wall = FakeClock()
        monotonic = FakeClock()
        service = _dynamic_stage_service(
            clock=wall,
            monotonic_clock=monotonic,
            tasks=self.tasks,
            admission=_stage_admission(),
        )
        wall.now_ms = 500
        selection = _authored_selection(expires_at_ms=2_000)
        service.provision_authored_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=service.service_epoch,
            revision=selection[0],
            launch_grant=selection[1],
            display_metadata=selection[2],
            authored_identity=selection[3],
        )
        digest = hashlib.sha256(selection[1].token.encode("utf-8")).hexdigest()
        self.assertEqual(service._launch_grants[digest].expires_deadline_ms, 2_000)

        monotonic.now_ms = 2_000
        with self.assertRaises(ProductLifecycleError) as expired:
            service.launch(
                actor_authority=PRESENTER_AUTHORITY,
                launch_grant=selection[1].token,
                expected_epoch=service.service_epoch,
                idempotency_key="compatibility-rollback-grant-expiry",
            )
        self.assertEqual(expired.exception.code, "authority_denied")
        self.assertEqual(service._dynamic_stage_grants, set())

    def test_capacity_failure_does_not_advance_source_neutral_serial(self) -> None:
        service = _dynamic_stage_service(
            clock=self.clock,
            tasks=self.tasks,
            admission=_stage_admission(),
            max_revisions=2,
            max_launch_grants=1,
        )
        legacy = _forge_selection(expires_at_ms=2_000)
        service.provision_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=service.service_epoch,
            revision=legacy[0],
            launch_grant=legacy[1],
            display_metadata=legacy[2],
            forge_identity=legacy[3],
        )
        rejected = _authored_selection(serial=99, expires_at_ms=3_000)

        with self.assertRaisesRegex(ValueError, "grant capacity"):
            service.provision_authored_selection(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch=service.service_epoch,
                revision=rejected[0],
                launch_grant=rejected[1],
                display_metadata=rejected[2],
                authored_identity=rejected[3],
            )
        self.assertEqual(service._source_neutral_launch_serial_high_water, 0)
        self.assertEqual(service._source_neutral_stage_grants, set())

        self.clock.now_ms = 2_000
        accepted = _authored_selection(serial=1, expires_at_ms=3_000)
        result = service.provision_authored_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=service.service_epoch,
            revision=accepted[0],
            launch_grant=accepted[1],
            display_metadata=accepted[2],
            authored_identity=accepted[3],
        )
        self.assertFalse(result.reused)
        self.assertEqual(service._source_neutral_launch_serial_high_water, 1)

    def test_conflicting_revision_grant_display_or_source_never_mutates(self) -> None:
        base = _forge_selection()
        self._provision(base)
        registry_state = self._registry_state()

        cases = (
            _forge_selection(
                token=base[1].token,
                selected_record_id=base[1].selected_record_id,
                world_value="changed-world",
            ),
            _forge_selection(
                token=base[1].token,
                selected_record_id=base[1].selected_record_id,
                policy_id="stage-policy-forge-changed",
            ),
            _forge_selection(
                token=base[1].token,
                selected_record_id=base[1].selected_record_id,
                title="Changed title",
            ),
            _forge_selection(
                token=base[1].token,
                selected_record_id=base[1].selected_record_id,
                forge_job_id="forge-job:changed-00000001",
            ),
        )
        for selection in cases:
            with self.subTest(selection=selection):
                with self.assertRaisesRegex(ValueError, "conflict"):
                    self._provision(selection)
                self.assertEqual(self._registry_state(), registry_state)

    def test_epoch_and_actor_are_checked_before_registry_mutation(self) -> None:
        selection = _forge_selection()
        for expected_epoch, actor_authority, code in (
            ("stale-service-epoch", PRESENTER_AUTHORITY, "outcome_unknown"),
            (
                self.service.service_epoch,
                "wrong-presenter-authority-0001",
                "authority_denied",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ProductLifecycleError) as captured:
                    self.service.provision_selection(
                        actor_authority=actor_authority,
                        expected_epoch=expected_epoch,
                        revision=selection[0],
                        launch_grant=selection[1],
                        display_metadata=selection[2],
                        forge_identity=selection[3],
                    )
                self.assertEqual(captured.exception.code, code)
                self.assertEqual(self._registry_state(), (0, 0, 0, 0, 0, 0))

    def test_provision_validates_digest_actor_operations_expiry_and_token(self) -> None:
        cases = (
            (
                _forge_selection(digest_override="sha256:" + "0" * 64),
                "digest does not match",
            ),
            (
                _forge_selection(actor_id="another-presenter"),
                "authorizing presenter",
            ),
            (
                _forge_selection(
                    allowed_operations=frozenset({ProductOperation.SWITCH})
                ),
                "must allow session.create",
            ),
            (
                _forge_selection(expires_at_ms=self.clock.now_ms),
                "grant is expired",
            ),
            (
                _forge_selection(token=PRESENTER_AUTHORITY),
                "token is not unique",
            ),
        )
        for selection, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self._provision(selection)
                self.assertEqual(self._registry_state(), (0, 0, 0, 0, 0, 0))

        bounded_service = _dynamic_stage_service(
            clock=FakeClock(),
            tasks=ManualTasks(),
            admission=_stage_admission(),
            max_runtime_document_bytes=64,
        )
        bounded_selection = _forge_selection()
        with self.assertRaisesRegex(ValueError, "runtime document limit"):
            bounded_service.provision_selection(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch=bounded_service.service_epoch,
                revision=bounded_selection[0],
                launch_grant=bounded_selection[1],
                display_metadata=bounded_selection[2],
                forge_identity=bounded_selection[3],
            )

    def test_revision_and_grant_capacities_fail_before_mutation(self) -> None:
        for max_revisions, max_grants, message in (
            (1, 2, "revision capacity"),
            (2, 1, "grant capacity"),
        ):
            with self.subTest(message=message):
                service = _dynamic_stage_service(
                    clock=FakeClock(),
                    tasks=ManualTasks(),
                    admission=_stage_admission(),
                    max_revisions=max_revisions,
                    max_launch_grants=max_grants,
                )
                first = _forge_selection()
                second = _forge_selection(
                    token="forge-stage-launch-grant-0002",
                    selected_record_id="selected-forge-record-0002",
                    world_value="second-world",
                    forge_job_id="forge-job:live-00000002",
                    forge_reference="forge-reference:live-00000002",
                )
                service.provision_selection(
                    actor_authority=PRESENTER_AUTHORITY,
                    expected_epoch=service.service_epoch,
                    revision=first[0],
                    launch_grant=first[1],
                    display_metadata=first[2],
                    forge_identity=first[3],
                )
                registry_state = (
                    len(service._revisions),
                    len(service._launch_grants),
                    len(service._stage_display_metadata),
                    len(service._stage_forge_sources),
                    len(service._dynamic_stage_grants),
                    len(service._dynamic_launch_by_selection),
                )
                with self.assertRaisesRegex(ValueError, message):
                    service.provision_selection(
                        actor_authority=PRESENTER_AUTHORITY,
                        expected_epoch=service.service_epoch,
                        revision=second[0],
                        launch_grant=second[1],
                        display_metadata=second[2],
                        forge_identity=second[3],
                    )
                self.assertEqual(
                    (
                        len(service._revisions),
                        len(service._launch_grants),
                        len(service._stage_display_metadata),
                        len(service._stage_forge_sources),
                        len(service._dynamic_stage_grants),
                        len(service._dynamic_launch_by_selection),
                    ),
                    registry_state,
                )

    def test_expired_dynamic_capacity_is_reclaimed_without_token_reuse(self) -> None:
        service = _dynamic_stage_service(
            clock=self.clock,
            tasks=self.tasks,
            admission=_stage_admission(),
            max_revisions=1,
            max_launch_grants=1,
        )
        first = _forge_selection(expires_at_ms=2_000)
        service.provision_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=service.service_epoch,
            revision=first[0],
            launch_grant=first[1],
            display_metadata=first[2],
            forge_identity=first[3],
        )

        self.clock.now_ms = 2_000
        second = _forge_selection(
            token="forge-stage-launch-grant-0002",
            selected_record_id="selected-forge-record-0002",
            world_value="second-world",
            expires_at_ms=3_000,
            forge_job_id="forge-job:live-00000002",
            forge_reference="forge-reference:live-00000002",
        )
        provisioned = service.provision_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=service.service_epoch,
            revision=second[0],
            launch_grant=second[1],
            display_metadata=second[2],
            forge_identity=second[3],
        )

        self.assertFalse(provisioned.reused)
        self.assertEqual(
            set(service._launch_grants),
            {hashlib.sha256(second[1].token.encode("utf-8")).hexdigest()},
        )
        self.assertEqual(set(service._revisions), {second[0].revision_id})
        self.assertEqual(set(service._stage_display_metadata), {second[0].revision_id})
        self.assertEqual(
            set(service._stage_forge_sources), {second[1].selected_record_id}
        )
        first_digest = hashlib.sha256(first[1].token.encode("utf-8")).hexdigest()
        self.assertIn(first_digest, service._used_value_digests)
        self.assertNotIn(first_digest, service._source_neutral_stage_grants)

        reused_token = _forge_selection(
            token=first[1].token,
            selected_record_id="selected-forge-record-0003",
            world_value="third-world",
            expires_at_ms=4_000,
            forge_job_id="forge-job:live-00000003",
            forge_reference="forge-reference:live-00000003",
        )
        self.clock.now_ms = 3_000
        with self.assertRaisesRegex(ValueError, "token is not unique"):
            service.provision_selection(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch=service.service_epoch,
                revision=reused_token[0],
                launch_grant=reused_token[1],
                display_metadata=reused_token[2],
                forge_identity=reused_token[3],
            )

    def test_dynamic_projection_data_outlives_grant_until_tombstone_cleanup(
        self,
    ) -> None:
        selection = _forge_selection(expires_at_ms=2_000)
        self._provision(selection)
        launched = self.service.launch(
            actor_authority=PRESENTER_AUTHORITY,
            launch_grant=selection[1].token,
            expected_epoch=self.service.service_epoch,
            idempotency_key="expiring-dynamic-launch",
        )

        self.clock.now_ms = 2_000
        terminal = self.service.current(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
        )
        self.assertEqual(terminal["kind"], "current_session_terminal")
        self.assertEqual(
            terminal["snapshot"]["stage"]["source"]["forge_job_id"],
            selection[3].forge_job_id,
        )
        self.assertEqual(self.service._launch_grants, {})
        self.assertEqual(self.service._dynamic_launch_by_selection, {})
        self.assertIn(selection[0].revision_id, self.service._revisions)
        self.assertIn(
            selection[1].selected_record_id, self.service._stage_forge_sources
        )

        self.clock.now_ms = 12_000
        current = self.service.current(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
        )
        self.assertEqual(current["kind"], "no_current_session")
        self.assertEqual(self.service._revisions, {})
        self.assertEqual(self.service._stage_display_metadata, {})
        self.assertEqual(self.service._stage_forge_sources, {})
        self.assertNotIn(
            hashlib.sha256(selection[1].token.encode("utf-8")).hexdigest(),
            self.service._launch_grants,
        )
        self.assertEqual(
            launched["snapshot"]["starting_revision"]["revision_id"],
            selection[0].revision_id,
        )

    def _provision(self, selection) -> None:
        revision, grant, metadata, source = selection
        self.service.provision_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
            revision=revision,
            launch_grant=grant,
            display_metadata=metadata,
            forge_identity=source,
        )

    def _registry_state(self) -> tuple[int, int, int, int, int, int]:
        return (
            len(self.service._revisions),
            len(self.service._launch_grants),
            len(self.service._stage_display_metadata),
            len(self.service._stage_forge_sources),
            len(self.service._dynamic_stage_grants),
            len(self.service._dynamic_launch_by_selection),
        )


class PresenterStageXrayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tasks = ManualTasks()
        self.service = _stage_service(
            clock=self.clock,
            tasks=self.tasks,
            admission=_stage_admission(),
            xray_policy=StageXrayPolicy(
                maximum_hosts=1,
                maximum_facts=1,
                maximum_text_characters=32,
            ),
        )
        self.created = self.service.launch(
            actor_authority=PRESENTER_AUTHORITY,
            launch_grant=CREATE_GRANT,
            expected_epoch=self.service.service_epoch,
            idempotency_key="xray-launch",
        )

    def test_snapshot_and_detail_are_bounded_allowlisted_and_dual_positioned(
        self,
    ) -> None:
        result = self._snapshot()
        xray = result["xray"]

        self.assertEqual(result["kind"], "stage_xray_snapshot")
        self.assertEqual(xray["schema_revision"], "stage-xray-v1")
        self.assertEqual(xray["accepted_state_revision"], 0)
        self.assertEqual(xray["projection_position"]["sequence"], 1)
        self.assertEqual(len(xray["hosts"]), 1)
        self.assertEqual(len(xray["facts"]), 1)
        self.assertFalse(xray["completeness"]["complete"])
        self.assertGreater(xray["completeness"]["hosts"]["omitted_count"], 0)
        self.assertGreater(xray["completeness"]["facts"]["omitted_count"], 0)

        entity_id = xray["hosts"][0]["entity_id"]
        detail = self.service.xray_detail(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=self.created["session_authority"],
            reference=self.created["snapshot"]["reference"],
            expected_epoch=self.service.service_epoch,
            entity_id=entity_id,
            expected_accepted_state_revision=xray["accepted_state_revision"],
            expected_xray_stream_id=xray["projection_position"]["stream_id"],
            expected_xray_sequence=xray["projection_position"]["sequence"],
        )
        self.assertEqual(detail["xray"]["detail"]["entity_id"], entity_id)
        self.assertEqual(
            detail["xray"]["projection_position"], xray["projection_position"]
        )

        serialized = json.dumps(result, sort_keys=True)
        for forbidden in (
            "private-world-value",
            "private_environment",
            "secret-path",
            "passwordless_sudo",
            "world_state",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_policy_cannot_exceed_published_projection_bounds(self) -> None:
        for values in (
            {"maximum_hosts": 65},
            {"maximum_facts": 129},
            {"maximum_fields_per_detail": 13},
            {"maximum_text_characters": 513},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                StageXrayPolicy(**values)

    def test_world_and_world_host_facts_have_distinct_stable_bound_details(
        self,
    ) -> None:
        document = {
            "world_state": {
                "active_host": "world",
                "entry_host": "world",
                "hosts_by_name": {
                    "world": {
                        "hostname": "world",
                        "facts_by_id": {"status": "Host accepted status"},
                    }
                },
                "world_facts_by_id": {"status": "World accepted status"},
            }
        }

        projection = build_stage_xray_projection(
            document,
            policy=StageXrayPolicy(),
        )
        rebuilt = build_stage_xray_projection(
            document,
            policy=StageXrayPolicy(),
        )
        facts_by_scope = {fact["scope"]: fact for fact in projection.snapshot["facts"]}
        world_fact = facts_by_scope["world"]
        host_fact = facts_by_scope["host"]
        host_id = projection.snapshot["hosts"][0]["entity_id"]

        self.assertEqual(len(facts_by_scope), 2)
        self.assertNotEqual(world_fact["entity_id"], host_fact["entity_id"])
        self.assertIsNone(world_fact["related_host_id"])
        self.assertEqual(host_fact["related_host_id"], host_id)
        self.assertEqual(
            self._detail_statement(projection, world_fact["entity_id"]),
            "World accepted status",
        )
        self.assertEqual(
            self._detail_statement(projection, host_fact["entity_id"]),
            "Host accepted status",
        )

        summaries = [
            *projection.snapshot["hosts"],
            *projection.snapshot["facts"],
        ]
        entity_ids = [summary["entity_id"] for summary in summaries]
        self.assertEqual(len(entity_ids), len(set(entity_ids)))
        self.assertEqual(set(projection.details), set(entity_ids))
        for summary in summaries:
            detail = projection.details[summary["entity_id"]]
            self.assertEqual(detail["entity_id"], summary["entity_id"])
            self.assertEqual(detail["entity_kind"], summary["entity_kind"])

        self.assertTrue(projection.snapshot["completeness"]["complete"])
        self.assertEqual(
            projection.snapshot["completeness"]["facts"],
            {"included_count": 2, "omitted_count": 0},
        )
        self.assertEqual(
            [fact["entity_id"] for fact in rebuilt.snapshot["facts"]],
            [fact["entity_id"] for fact in projection.snapshot["facts"]],
        )
        self.assertEqual(rebuilt.content_sha256, projection.content_sha256)

    def test_generated_entity_collision_is_omitted_without_detail_overwrite(
        self,
    ) -> None:
        collision_id = "fact:" + "f" * 32
        document = {
            "world_state": {
                "world_facts_by_id": {
                    "first": "First accepted statement",
                    "second": "Second accepted statement",
                }
            }
        }

        with patch("dreamland.product.xray._safe_id", return_value=collision_id):
            projection = build_stage_xray_projection(
                document,
                policy=StageXrayPolicy(),
            )

        self.assertEqual(len(projection.snapshot["facts"]), 1)
        self.assertEqual(projection.snapshot["facts"][0]["entity_id"], collision_id)
        self.assertEqual(
            self._detail_statement(projection, collision_id),
            "First accepted statement",
        )
        self.assertEqual(set(projection.details), {collision_id})
        self.assertEqual(
            projection.snapshot["completeness"]["facts"],
            {"included_count": 1, "omitted_count": 1},
        )
        self.assertFalse(projection.snapshot["completeness"]["complete"])

    def test_xray_detail_coordinates_are_bounded_to_json_safe_integers(self) -> None:
        xray = self._snapshot()["xray"]
        entity_id = xray["hosts"][0]["entity_id"]
        stream_id = xray["projection_position"]["stream_id"]
        maximum = 2**53 - 1

        for revision, sequence in ((maximum, 1), (0, maximum)):
            with self.subTest(revision=revision, sequence=sequence):
                with self.assertRaises(ProductLifecycleError) as captured:
                    self.service.xray_detail(
                        actor_authority=PRESENTER_AUTHORITY,
                        session_authority=self.created["session_authority"],
                        reference=self.created["snapshot"]["reference"],
                        expected_epoch=self.service.service_epoch,
                        entity_id=entity_id,
                        expected_accepted_state_revision=revision,
                        expected_xray_stream_id=stream_id,
                        expected_xray_sequence=sequence,
                    )
                self.assertEqual(captured.exception.code, "xray_resync_required")

        for revision, sequence in (
            (maximum + 1, 1),
            (0, maximum + 1),
            (-1, 1),
            (0, 0),
            (True, 1),
            (0, True),
        ):
            with self.subTest(revision=revision, sequence=sequence):
                with self.assertRaises(ProductLifecycleError) as captured:
                    self.service.xray_detail(
                        actor_authority=PRESENTER_AUTHORITY,
                        session_authority=self.created["session_authority"],
                        reference=self.created["snapshot"]["reference"],
                        expected_epoch=self.service.service_epoch,
                        entity_id=entity_id,
                        expected_accepted_state_revision=revision,
                        expected_xray_stream_id=stream_id,
                        expected_xray_sequence=sequence,
                    )
                self.assertEqual(captured.exception.code, "invalid_request")

    def test_xray_sequence_rotates_stream_at_json_safe_boundary(self) -> None:
        initial = self._snapshot()["xray"]
        state = next(iter(self.service._xray_sessions.values()))
        state.sequence = 2**53 - 1
        self.service.submit_command(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=self.created["session_authority"],
            reference=self.created["snapshot"]["reference"],
            expected_epoch=self.service.service_epoch,
            idempotency_key="xray-rollover-command",
            expected_slot_generation=0,
            command="hostname",
        )

        rolled = self._snapshot()["xray"]

        self.assertEqual(rolled["projection_position"]["sequence"], 1)
        self.assertNotEqual(
            rolled["projection_position"]["stream_id"],
            initial["projection_position"]["stream_id"],
        )

    def test_xray_never_publishes_accepted_revision_outside_json_safe_range(
        self,
    ) -> None:
        core = next(iter(self.service._core_sessions.values()))
        core.accepted_state_revision = 2**53 - 1
        bounded = self._snapshot()["xray"]
        self.assertEqual(bounded["accepted_state_revision"], 2**53 - 1)

        core.accepted_state_revision += 1
        with self.assertRaises(ProductLifecycleError) as captured:
            self._snapshot()
        self.assertEqual(captured.exception.code, "xray_unavailable")

    def test_xray_requires_exact_presenter_session_authority(self) -> None:
        with self.assertRaises(ProductLifecycleError) as captured:
            self.service.xray_snapshot(
                actor_authority="wrong-presenter-authority-0001",
                session_authority=self.created["session_authority"],
                reference=self.created["snapshot"]["reference"],
                expected_epoch=self.service.service_epoch,
            )
        self.assertEqual(captured.exception.code, "authority_denied")
        self.assertEqual(self.service._xray_sessions, {})

    def test_replacement_terminates_old_xray_and_rotates_projection_stream(
        self,
    ) -> None:
        old_xray = self._snapshot()["xray"]
        old_snapshot = self.created["snapshot"]
        replaced = self.service.replace(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=self.created["session_authority"],
            reference=old_snapshot["reference"],
            expected_epoch=self.service.service_epoch,
            idempotency_key="xray-replacement",
            expected_control_revision=old_snapshot["coordinates"]["control_revision"],
            launch_grant=SWITCH_GRANT,
        )
        self.assertFalse(replaced["previous_snapshot"]["stage"]["xray_available"])
        self.assertTrue(replaced["snapshot"]["stage"]["xray_available"])
        self.assertEqual(self.service._xray_sessions, {})

        with self.assertRaises(ProductLifecycleError) as captured:
            self._snapshot()
        self.assertEqual(captured.exception.code, "authority_denied")
        new_xray = self.service.xray_snapshot(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=replaced["session_authority"],
            reference=replaced["snapshot"]["reference"],
            expected_epoch=self.service.service_epoch,
        )["xray"]
        self.assertNotEqual(
            new_xray["projection_position"]["stream_id"],
            old_xray["projection_position"]["stream_id"],
        )

    def test_projection_only_activity_change_advances_only_xray_position(self) -> None:
        initial = self._snapshot()["xray"]
        accepted = self.service.submit_command(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=self.created["session_authority"],
            reference=self.created["snapshot"]["reference"],
            expected_epoch=self.service.service_epoch,
            idempotency_key="xray-command",
            expected_slot_generation=0,
            command="hostname",
        )
        self.assertEqual(accepted["kind"], "command_accepted")

        active = self._snapshot()["xray"]
        self.assertEqual(
            active["accepted_state_revision"], initial["accepted_state_revision"]
        )
        self.assertGreater(
            active["projection_position"]["sequence"],
            initial["projection_position"]["sequence"],
        )
        self.assertEqual(active["activity"]["foreground"]["state"], "active")

        entity_id = initial["hosts"][0]["entity_id"]
        with self.assertRaises(ProductLifecycleError) as captured:
            self.service.xray_detail(
                actor_authority=PRESENTER_AUTHORITY,
                session_authority=self.created["session_authority"],
                reference=self.created["snapshot"]["reference"],
                expected_epoch=self.service.service_epoch,
                entity_id=entity_id,
                expected_accepted_state_revision=initial["accepted_state_revision"],
                expected_xray_stream_id=initial["projection_position"]["stream_id"],
                expected_xray_sequence=initial["projection_position"]["sequence"],
            )
        self.assertEqual(captured.exception.code, "xray_resync_required")

        self.tasks.tasks.pop(0)()
        idle = self._snapshot()["xray"]
        self.assertEqual(idle["accepted_state_revision"], 0)
        self.assertGreater(
            idle["projection_position"]["sequence"],
            active["projection_position"]["sequence"],
        )
        self.assertEqual(idle["activity"]["foreground"]["state"], "idle")

    def test_accepted_commit_advances_both_coordinates_atomically(self) -> None:
        committed = {
            "world_state": {
                "name": "committed-world",
                "active_host": "alpha",
                "entry_host": "alpha",
                "hosts_by_name": {"alpha": {"hostname": "alpha"}},
                "world_facts_by_id": {"commit": "Accepted change"},
            }
        }
        tasks = ManualTasks()
        service = _stage_service(
            clock=self.clock,
            tasks=tasks,
            admission=_stage_admission(),
            executor=FixedExecutor(
                ProductExecutionResult.completed_exited(
                    exit_status=0,
                    state_effect=ProductStateEffect(
                        ProductStateEffectKind.COMMITTED,
                        provisional_document=committed,
                    ),
                    usage_tokens=10,
                    usage_spend_units=10,
                )
            ),
            xray_policy=StageXrayPolicy(),
        )
        created = service.launch(
            actor_authority=PRESENTER_AUTHORITY,
            launch_grant=CREATE_GRANT,
            expected_epoch=service.service_epoch,
            idempotency_key="commit-launch",
        )
        before = service.xray_snapshot(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=service.service_epoch,
        )["xray"]
        service.submit_command(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=service.service_epoch,
            idempotency_key="commit-command",
            expected_slot_generation=0,
            command="materialize",
        )
        tasks.tasks.pop(0)()
        after = service.xray_snapshot(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=service.service_epoch,
        )["xray"]

        self.assertEqual(after["accepted_state_revision"], 1)
        self.assertGreater(
            after["projection_position"]["sequence"],
            before["projection_position"]["sequence"],
        )
        self.assertEqual(after["facts"][0]["statement"]["text"], "Accepted change")

    def test_xray_is_independently_disabled_and_terminal_cleanup_is_bounded(
        self,
    ) -> None:
        disabled = _stage_service(
            clock=self.clock,
            tasks=ManualTasks(),
            admission=_stage_admission(),
        )
        created = disabled.launch(
            actor_authority=PRESENTER_AUTHORITY,
            launch_grant=CREATE_GRANT,
            expected_epoch=disabled.service_epoch,
            idempotency_key="disabled-launch",
        )
        self.assertFalse(created["snapshot"]["stage"]["xray_available"])
        with self.assertRaises(ProductLifecycleError) as captured:
            disabled.xray_snapshot(
                actor_authority=PRESENTER_AUTHORITY,
                session_authority=created["session_authority"],
                reference=created["snapshot"]["reference"],
                expected_epoch=disabled.service_epoch,
            )
        self.assertEqual(captured.exception.code, "xray_unavailable")

        self._snapshot()
        self.assertEqual(len(self.service._xray_sessions), 1)
        snapshot = self.created["snapshot"]
        self.service.close(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=self.created["session_authority"],
            reference=snapshot["reference"],
            expected_epoch=self.service.service_epoch,
            idempotency_key="xray-close",
            expected_control_revision=snapshot["coordinates"]["control_revision"],
        )
        self.assertEqual(self.service._xray_sessions, {})

    def test_hostile_text_is_marked_clipped_and_never_claimed_complete(self) -> None:
        projection = build_stage_xray_projection(
            {
                "profile": {
                    "state": {
                        "world_state": {
                            "name": "harbor\ncontrol\u202ehidden",
                            "active_host": "console",
                            "hosts_by_name": {
                                "console": {
                                    "hostname": "console\u0000private",
                                }
                            },
                        }
                    }
                }
            },
            policy=StageXrayPolicy(maximum_text_characters=8),
        )

        name = projection.snapshot["overview"]["world_name"]
        hostname = projection.snapshot["hosts"][0]["hostname"]
        self.assertTrue(name["clipped"])
        self.assertTrue(name["control_marked"])
        self.assertTrue(hostname["clipped"])
        self.assertTrue(hostname["control_marked"])
        self.assertFalse(projection.snapshot["completeness"]["complete"])
        serialized = json.dumps(dict(projection.snapshot), ensure_ascii=False)
        self.assertNotIn("hidden", serialized)
        self.assertNotIn("private", serialized)

    def test_projection_failure_does_not_break_stage_terminal_operations(self) -> None:
        with patch(
            "dreamland.product.stage.build_stage_xray_projection",
            side_effect=RuntimeError("projection failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "projection failed"):
                self._snapshot()

        stage = self.service.snapshot(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=self.created["session_authority"],
            reference=self.created["snapshot"]["reference"],
            expected_epoch=self.service.service_epoch,
        )
        self.assertTrue(stage["snapshot"]["stage"]["xray_available"])
        accepted = self.service.submit_command(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=self.created["session_authority"],
            reference=self.created["snapshot"]["reference"],
            expected_epoch=self.service.service_epoch,
            idempotency_key="after-xray-failure",
            expected_slot_generation=0,
            command="hostname",
        )
        self.assertEqual(accepted["kind"], "command_accepted")

    def _snapshot(self) -> dict[str, object]:
        return self.service.xray_snapshot(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=self.created["session_authority"],
            reference=self.created["snapshot"]["reference"],
            expected_epoch=self.service.service_epoch,
        )

    @staticmethod
    def _detail_statement(projection, entity_id: str) -> str:
        detail = projection.details[entity_id]
        return next(
            field["value"]["text"]
            for field in detail["fields"]
            if field["name"] == "statement"
        )


def _stage_admission() -> ProductAdmissionLedger:
    return ProductAdmissionLedger(
        ProductAdmissionLimits(
            max_active_sessions=3,
            max_logical_jobs=3,
            max_physical_workers=2,
            token_budget=300,
            spend_budget_units=300,
            presenter_stage_reserved_sessions=1,
            presenter_stage_reserved_logical_jobs=1,
            presenter_stage_reserved_physical_workers=1,
            presenter_stage_reserved_token_budget=100,
            presenter_stage_reserved_spend_budget_units=100,
        )
    )


def _stage_service(
    *,
    clock: FakeClock,
    tasks: ManualTasks,
    admission: ProductAdmissionLedger,
    dynamic_selection_only: bool = False,
    executor=None,
    xray_policy: StageXrayPolicy | None = None,
) -> PresenterStageService:
    return PresenterStageService(
        revisions=(
            ImmutableRevisionSeed(
                revision_id="revision-a",
                document={
                    "world_state": {
                        "name": "demo-world",
                        "active_host": "alpha",
                        "entry_host": "alpha",
                        "hosts_by_name": {
                            "alpha": {
                                "hostname": "alpha",
                                "purpose": {"theme": "Primary control host"},
                                "facts_by_id": {"local": "Local accepted fact"},
                                "private_environment": "secret-path",
                            },
                            "bravo": {"hostname": "bravo"},
                        },
                        "world_facts_by_id": {
                            "one": "World accepted fact",
                            "two": "Second accepted fact",
                        },
                        "secret": "private-world-value-a",
                    }
                },
            ),
            ImmutableRevisionSeed(
                revision_id="revision-b",
                document={"world_state": {"secret": "private-world-value-b"}},
            ),
        ),
        actors=(
            ActorAuthoritySeed(
                token=PRESENTER_AUTHORITY,
                actor_id="presenter-alpha",
                role=ProductRole.PRESENTER,
                expires_at_ms=100_000,
            ),
        ),
        launch_grants=(
            LaunchGrantSeed(
                token=CREATE_GRANT,
                selected_record_id="selected-stage-a",
                actor_id="presenter-alpha",
                role=ProductRole.PRESENTER,
                revision_id="revision-a",
                expires_at_ms=100_000,
                allowed_operations=frozenset({ProductOperation.CREATE}),
                limits=_stage_limits(),
                policy_id="stage-policy-a",
                notice_version="notice-a",
            ),
            LaunchGrantSeed(
                token=OTHER_CREATE_GRANT,
                selected_record_id="selected-stage-a-alternate",
                actor_id="presenter-alpha",
                role=ProductRole.PRESENTER,
                revision_id="revision-a",
                expires_at_ms=100_000,
                allowed_operations=frozenset({ProductOperation.CREATE}),
                limits=_stage_limits(),
                policy_id="stage-policy-b",
                notice_version="notice-a",
            ),
            LaunchGrantSeed(
                token=SWITCH_GRANT,
                selected_record_id="selected-stage-b",
                actor_id="presenter-alpha",
                role=ProductRole.PRESENTER,
                revision_id="revision-b",
                expires_at_ms=100_000,
                allowed_operations=frozenset(
                    {ProductOperation.CREATE, ProductOperation.SWITCH}
                ),
                limits=_stage_limits(),
                policy_id="stage-policy-b",
                notice_version="notice-a",
            ),
        ),
        display_metadata=(
            StageDisplayMetadataSeed(
                revision_id="revision-a",
                title="Presenter Demonstration World",
                source_label="Authored",
                revision_label="Demo A",
            ),
            StageDisplayMetadataSeed(
                revision_id="revision-b",
                title="Second Demonstration World",
                source_label="Authored",
                revision_label="Demo B",
            ),
        ),
        executor=executor or SuccessfulExecutor(),
        submit_task=tasks,
        lifecycle_policy=LifecyclePolicy(
            absolute_ttl_ms=60_000,
            idle_ttl_ms=30_000,
            tombstone_ttl_ms=10_000,
            failed_operation_ttl_ms=5_000,
            max_active_sessions=3,
            max_tombstones=6,
            max_idempotency_records=32,
            max_launch_grants=8,
            max_actor_authorities=4,
            max_revisions=4,
        ),
        core_policy=ProductCorePolicy(
            max_commands_per_session=4,
            command_deadline_ms=10_000,
            cancellation_deadline_ms=2_000,
            max_output_bytes_per_command=1_024,
            max_output_chunks_per_command=16,
            reserved_tokens_per_command=100,
            reserved_spend_units_per_command=100,
        ),
        admission=admission,
        clock_ms=clock,
        monotonic_ms=clock,
        token_factory=SequenceTokens(),
        dynamic_selection_only=dynamic_selection_only,
        xray_policy=xray_policy,
    )


def _stage_limits() -> dict[str, int]:
    return {
        "absolute_ttl_ms": 50_000,
        "idle_ttl_ms": 20_000,
        "max_commands": 4,
        "max_command_bytes": 1_024,
        "command_reserved_tokens": 100,
        "command_reserved_spend_units": 100,
    }


def _dynamic_stage_service(
    *,
    clock: FakeClock,
    monotonic_clock: FakeClock | None = None,
    tasks: ManualTasks,
    admission: ProductAdmissionLedger,
    dynamic_selection_only: bool = True,
    max_revisions: int = 4,
    max_launch_grants: int = 8,
    max_runtime_document_bytes: int = 4 * 1024 * 1024,
) -> PresenterStageService:
    return PresenterStageService(
        revisions=(),
        actors=(
            ActorAuthoritySeed(
                token=PRESENTER_AUTHORITY,
                actor_id="presenter-alpha",
                role=ProductRole.PRESENTER,
                expires_at_ms=100_000,
            ),
        ),
        launch_grants=(),
        display_metadata=(),
        executor=SuccessfulExecutor(),
        submit_task=tasks,
        lifecycle_policy=LifecyclePolicy(
            absolute_ttl_ms=60_000,
            idle_ttl_ms=30_000,
            tombstone_ttl_ms=10_000,
            failed_operation_ttl_ms=5_000,
            max_active_sessions=3,
            max_tombstones=6,
            max_idempotency_records=32,
            max_launch_grants=max_launch_grants,
            max_actor_authorities=4,
            max_revisions=max_revisions,
        ),
        core_policy=ProductCorePolicy(
            max_commands_per_session=4,
            command_deadline_ms=10_000,
            cancellation_deadline_ms=2_000,
            max_output_bytes_per_command=1_024,
            max_output_chunks_per_command=16,
            max_runtime_document_bytes=max_runtime_document_bytes,
            reserved_tokens_per_command=100,
            reserved_spend_units_per_command=100,
        ),
        admission=admission,
        clock_ms=clock,
        monotonic_ms=monotonic_clock or clock,
        token_factory=SequenceTokens(),
        dynamic_selection_only=dynamic_selection_only,
    )


def _forge_selection(
    *,
    token: str = "forge-stage-launch-grant-0001",
    selected_record_id: str = "selected-forge-record-0001",
    world_value: str = "forged-world",
    actor_id: str = "presenter-alpha",
    allowed_operations: frozenset[ProductOperation] = frozenset(
        {ProductOperation.CREATE, ProductOperation.SWITCH}
    ),
    expires_at_ms: int = 100_000,
    policy_id: str = "stage-policy-forge-v1",
    title: str = "Live Forged World",
    digest_override: str | None = None,
    forge_job_id: str = "forge-job:live-00000001",
    forge_reference: str = "forge-reference:live-00000001",
) -> tuple[
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    StageDisplayMetadataSeed,
    StageForgeIdentitySeed,
]:
    document = {
        "kind": "dreamland.world_forge_revision",
        "schema_revision": "1",
        "profile": {
            "level": "world",
            "state": {"world_state": {"value": world_value}},
        },
    }
    canonical = (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
    revision_id = f"world-revision:{digest}"
    return (
        ImmutableRevisionSeed(revision_id=revision_id, document=document),
        LaunchGrantSeed(
            token=token,
            selected_record_id=selected_record_id,
            actor_id=actor_id,
            role=ProductRole.PRESENTER,
            revision_id=revision_id,
            expires_at_ms=expires_at_ms,
            allowed_operations=allowed_operations,
            limits=_stage_limits(),
            policy_id=policy_id,
            notice_version="notice-forge-v1",
        ),
        StageDisplayMetadataSeed(
            revision_id=revision_id,
            title=title,
            source_label="World Forge",
            revision_label="Selected candidate A",
        ),
        StageForgeIdentitySeed(
            selected_record_id=selected_record_id,
            immutable_digest=digest_override or digest,
            forge_job_id=forge_job_id,
            forge_reference=forge_reference,
        ),
    )


def _authored_selection(
    *,
    serial: int = 1,
    token: str | None = None,
    selected_record_id: str | None = None,
    expires_at_ms: int = 100_000,
) -> tuple[
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    StageDisplayMetadataSeed,
    StageAuthoredIdentitySeed,
]:
    document = {
        "kind": "dreamland.authored_world_revision",
        "schema_revision": "1",
        "profile": {
            "level": "world",
            "state": {"world_state": {"value": "authored-world"}},
        },
    }
    canonical = (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
    revision_id = f"world-revision:{digest}"
    launch_grant = token or _source_neutral_launch_grant(serial)
    selected_record = selected_record_id or _source_neutral_selected_record(
        launch_grant
    )
    return (
        ImmutableRevisionSeed(revision_id=revision_id, document=document),
        LaunchGrantSeed(
            token=launch_grant,
            selected_record_id=selected_record,
            actor_id="presenter-alpha",
            role=ProductRole.PRESENTER,
            revision_id=revision_id,
            expires_at_ms=expires_at_ms,
            allowed_operations=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            limits=_stage_limits(),
            policy_id="stage-policy-authored-v1",
            notice_version="notice-authored-v1",
        ),
        StageDisplayMetadataSeed(
            revision_id=revision_id,
            title="Configured Authored World",
            source_label="Authored",
            revision_label="Pinned snapshot",
        ),
        StageAuthoredIdentitySeed(
            selected_record_id=selected_record,
            immutable_digest=digest,
        ),
    )


def _source_neutral_launch_grant(serial: int) -> str:
    digest = hashlib.sha256(f"source-neutral-grant-{serial}".encode()).hexdigest()
    return f"launch-grant:{serial:016x}_{digest}"


def _source_neutral_selected_record(launch_grant: str) -> str:
    digest = hashlib.sha256(launch_grant.encode("utf-8")).hexdigest()
    return f"selected-record:{digest}"
