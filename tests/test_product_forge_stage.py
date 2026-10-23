from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dreamland.product.admission import (
    ProductAdmissionLedger,
    ProductAdmissionLimits,
)
from dreamland.product.core import ProductCorePolicy
from dreamland.product.execution import ProductExecutionResult
from dreamland.product.forge import (
    BSIDES_RECIPE_ID,
    BSIDES_RECIPE_REVISION,
    ForgeActorSeed,
    ForgeJobState,
    ForgePolicy,
    WorldForgeService,
    WorldSeedAnswer,
)
from dreamland.product.forge_artifacts import (
    FORGE_COMPILER_REVISION,
    FORGE_VALIDATOR_REVISION,
    ImmutableForgeRevision,
    ImmutableForgeStore,
    live_forge_receipt,
    live_forge_source,
)
from dreamland.product.forge_stage import (
    ForgeStageHandoffPolicy,
    ForgeStageHandoffService,
    forge_stage_display_metadata,
)
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductRole,
)
from dreamland.product.stage import PresenterStageService
from dreamland.state_profiles import StateProfile

PRESENTER_AUTHORITY = "actor:forge-stage-presenter-authority-0001"
PRESENTER_ID = "forge-stage-presenter"


class SharedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    def forge(self) -> datetime:
        return self.value

    def milliseconds(self) -> int:
        return int(self.value.timestamp() * 1_000)


class SequenceTokens:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self, purpose: str) -> str:
        self.index += 1
        return f"{purpose}:forge-stage-test-{self.index:08d}"


class ManualTasks:
    def __init__(self) -> None:
        self.tasks: list[Callable[[], None]] = []

    def __call__(self, task: Callable[[], None]) -> None:
        self.tasks.append(task)


class SuccessfulExecutor:
    def execute(self, _request, _emit):
        return ProductExecutionResult.completed_exited(
            exit_status=0,
            usage_tokens=1,
            usage_spend_units=1,
        )


class ForgeStageHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = SharedClock()
        self.artifacts = ImmutableForgeStore(self.root / "revisions")
        self.forge = WorldForgeService(
            actors=(
                ForgeActorSeed(
                    authority=PRESENTER_AUTHORITY,
                    actor_id=PRESENTER_ID,
                    expires_at=(self.clock.value + timedelta(hours=1)).isoformat(),
                ),
            ),
            policy=ForgePolicy(
                maximum_jobs=8,
                maximum_idempotency_records=32,
                selected_handoff_ttl_seconds=300,
            ),
            clock=self.clock.forge,
            token_factory=SequenceTokens(),
            selected_record_token_factory=SequenceTokens(),
            source=live_forge_source(),
        )
        self.tasks = ManualTasks()
        self.admission = ProductAdmissionLedger(_admission_limits())
        self.stage = PresenterStageService(
            revisions=(),
            actors=(
                ActorAuthoritySeed(
                    token=PRESENTER_AUTHORITY,
                    actor_id=PRESENTER_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=(self.clock.milliseconds() + 60 * 60 * 1_000),
                ),
            ),
            launch_grants=(),
            display_metadata=(),
            executor=SuccessfulExecutor(),
            submit_task=self.tasks,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=600_000,
                idle_ttl_ms=60_000,
                tombstone_ttl_ms=30_000,
                failed_operation_ttl_ms=15_000,
                max_active_sessions=2,
                max_tombstones=4,
                max_idempotency_records=64,
                max_launch_grants=8,
                max_actor_authorities=2,
                max_revisions=8,
            ),
            core_policy=ProductCorePolicy(
                max_commands_per_session=8,
                command_deadline_ms=10_000,
                cancellation_deadline_ms=2_000,
                max_output_bytes_per_command=16 * 1_024,
                max_output_chunks_per_command=256,
                reserved_tokens_per_command=100,
                reserved_spend_units_per_command=100,
            ),
            admission=self.admission,
            clock_ms=self.clock.milliseconds,
            monotonic_ms=self.clock.milliseconds,
            token_factory=SequenceTokens(),
            dynamic_selection_only=True,
        )
        self.handoff = ForgeStageHandoffService(
            forge=self.forge,
            artifacts=self.artifacts,
            stage=self.stage,
            policy=ForgeStageHandoffPolicy(
                launch_limits=_launch_limits(),
                notice_version="notice:forge-stage-development-v1",
            ),
            token_factory=SequenceTokens(),
        )

    def test_import_is_inert(self) -> None:
        code = """
import json
import threading
before = [thread.name for thread in threading.enumerate()]
from dreamland.product.forge_stage import ForgeStageHandoffPolicy
policy = ForgeStageHandoffPolicy(
    launch_limits={"absolute_ttl_ms": 1000},
    notice_version="notice:test",
)
after = [thread.name for thread in threading.enumerate()]
print(json.dumps({"before": before, "after": after, "title": policy.title}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["before"], payload["after"])
        self.assertEqual(payload["title"], "Audience-created DREAMLAND world")

    def test_exact_selection_launch_replay_and_fresh_resume(self) -> None:
        artifact = self.artifacts.save(_profile("tracking-console"))
        reference = self._select(artifact, "build-alpha")

        created = self._launch(reference, "handoff-alpha")
        replayed = self._launch(reference, "handoff-alpha")
        resumed = self._launch(reference, "handoff-alpha-resume")

        self.assertEqual(created["kind"], "forge_stage_session_created")
        self.assertFalse(created["replayed"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(resumed["kind"], "forge_stage_session_resumed")
        self.assertFalse(resumed["replayed"])
        self.assertEqual(
            created["snapshot"]["session_id"],
            resumed["snapshot"]["session_id"],
        )
        self.assertEqual(created["session_authority"], resumed["session_authority"])
        self._assert_exact_identity(created, artifact, reference)
        self._assert_exact_identity(replayed, artifact, reference)
        self._assert_exact_identity(resumed, artifact, reference)
        self.assertEqual(self.admission.snapshot().active_sessions, 1)
        self.assertFalse((self.root / "state_profiles").exists())
        self.assertFalse((self.root / "default.json").exists())
        self.assertFalse((self.root / "session_index.json").exists())

    def test_concurrent_identical_handoff_allocates_one_session(self) -> None:
        artifact = self.artifacts.save(_profile("tracking-console"))
        reference = self._select(artifact, "build-concurrent")

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = tuple(
                executor.map(
                    lambda _index: self._launch(reference, "shared-handoff-key"),
                    range(12),
                )
            )

        self.assertEqual(sum(not result["replayed"] for result in results), 1)
        self.assertEqual(
            {result["snapshot"]["session_id"] for result in results},
            {results[0]["snapshot"]["session_id"]},
        )
        self.assertEqual(self.admission.snapshot().active_sessions, 1)

    def test_same_revision_different_selection_requires_explicit_replace(self) -> None:
        artifact = self.artifacts.save(_profile("tracking-console"))
        first_reference = self._select(artifact, "build-first")
        second_reference = self._select(artifact, "build-second")
        third_reference = self._select(artifact, "build-third")
        created = self._launch(first_reference, "launch-first")

        with self.assertRaises(ProductLifecycleError) as denied:
            self._launch(second_reference, "different-selection")
        self.assertEqual(denied.exception.code, "current_session_exists")
        with self.assertRaises(ProductLifecycleError) as replayed:
            self._launch(second_reference, "different-selection")
        self.assertEqual(replayed.exception.code, "current_session_exists")
        self.assertEqual(replayed.exception.context, denied.exception.context)
        with self.assertRaises(ProductLifecycleError) as changed:
            self._launch(third_reference, "different-selection")
        self.assertEqual(changed.exception.code, "idempotency_conflict")
        current = self.stage.current(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.stage.service_epoch,
        )
        self.assertEqual(
            current["snapshot"]["session_id"], created["snapshot"]["session_id"]
        )
        self.assertEqual(
            current["snapshot"]["stage"]["source"],
            created["snapshot"]["stage"]["source"],
        )
        still_active = self.stage.snapshot(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=created["session_authority"],
            reference=created["snapshot"]["reference"],
            expected_epoch=self.stage.service_epoch,
        )
        self.assertEqual(
            still_active["snapshot"]["session_id"],
            created["snapshot"]["session_id"],
        )
        self.assertEqual(self.stage._tombstones_by_reference, {})
        self.assertEqual(self.admission.snapshot().active_sessions, 1)

    def test_explicit_replacement_uses_new_exact_forge_artifact(self) -> None:
        first_artifact = self.artifacts.save(_profile("tracking-console"))
        second_artifact = self.artifacts.save(_profile("spectrometer-console"))
        first_reference = self._select(first_artifact, "build-original")
        second_reference = self._select(second_artifact, "build-replacement")
        created = self._launch(first_reference, "launch-original")
        old_snapshot = created["snapshot"]
        old_core = self.stage._core_sessions[old_snapshot["session_id"]]
        old_event_cursor = old_core.journal.event_position.to_dict()
        expected_control_revision = old_snapshot["coordinates"]["control_revision"]
        replacement_arguments = {
            "actor_authority": PRESENTER_AUTHORITY,
            "forge_expected_epoch": self.forge.service_epoch,
            "stage_expected_epoch": self.stage.service_epoch,
            "forge_reference": second_reference,
            "idempotency_key": "replace-with-forge-selection",
            "session_authority": created["session_authority"],
            "stage_reference": old_snapshot["reference"],
            "expected_control_revision": expected_control_revision,
        }

        replaced = self.handoff.replace(**replacement_arguments)

        self.assertEqual(replaced["kind"], "forge_stage_session_replaced")
        self.assertFalse(replaced["replayed"])
        self._assert_exact_identity(replaced, second_artifact, second_reference)
        previous = replaced["previous_snapshot"]
        self.assertEqual(previous["session_id"], old_snapshot["session_id"])
        self.assertEqual(previous["reference"], old_snapshot["reference"])
        self.assertEqual(
            previous["starting_revision"], old_snapshot["starting_revision"]
        )
        self.assertEqual(previous["launch_binding"], old_snapshot["launch_binding"])
        self.assertEqual(previous["stage"], old_snapshot["stage"])
        self.assertEqual(previous["available_controls"], [])
        self.assertEqual(
            previous["command_slot"],
            {
                "state": "terminal",
                "generation": old_snapshot["command_slot"]["generation"],
                "active_command_id": None,
            },
        )
        self.assertEqual(previous["lifecycle"]["state"], "replaced")
        self.assertEqual(previous["lifecycle"]["terminal_reason"], "replaced")
        self.assertTrue(previous["recovery"]["tombstone"])
        self.assertEqual(
            previous["recovery"]["retain_until_ms"],
            previous["lifecycle"]["terminal_at_ms"]
            + self.stage._policy.tombstone_ttl_ms,
        )
        self.assertEqual(
            previous["coordinates"]["control_revision"],
            expected_control_revision + 1,
        )
        self.assertEqual(replaced["snapshot"]["coordinates"]["control_revision"], 1)
        self.assertEqual(
            previous["coordinates"]["command_slot_generation"],
            old_snapshot["coordinates"]["command_slot_generation"],
        )
        self.assertEqual(
            previous["coordinates"]["accepted_state_revision"],
            old_snapshot["coordinates"]["accepted_state_revision"],
        )
        self.assertEqual(
            previous["coordinates"]["transcript"],
            old_snapshot["coordinates"]["transcript"],
        )
        self.assertEqual(previous["transcript"], old_snapshot["transcript"])
        self.assertEqual(old_snapshot["coordinates"]["event"], old_event_cursor)

        old_journal = old_core.journal.event_snapshot()
        terminal_event = old_journal.events[-1]
        self.assertEqual(
            previous["coordinates"]["event"], old_journal.position.to_dict()
        )
        self.assertEqual(
            previous["coordinates"]["event"], terminal_event.cursor.to_dict()
        )
        self.assertEqual(
            previous["coordinates"]["event"]["stream_id"],
            old_event_cursor["stream_id"],
        )
        self.assertEqual(
            previous["coordinates"]["event"]["sequence"],
            old_event_cursor["sequence"] + 1,
        )
        self.assertEqual(terminal_event.event_type, "session.terminal")
        self.assertEqual(terminal_event.criticality.value, "authoritative")
        self.assertEqual(
            dict(terminal_event.data),
            {
                "control_revision": expected_control_revision + 1,
                "terminal_reason": "replaced",
                "slot_generation": old_snapshot["command_slot"]["generation"],
                "accepted_state_revision": old_snapshot["coordinates"][
                    "accepted_state_revision"
                ],
            },
        )
        self.assertEqual(
            previous["stage"]["source"]["immutable_digest"],
            first_artifact.digest,
        )
        replacement_source = replaced["snapshot"]["stage"]["source"]
        replacement_selection = replaced["forge_selection"]
        self.assertEqual(
            replacement_source,
            {
                "kind": "world_forge_selection",
                "selected_record_id": replacement_selection["selected_record_id"],
                "immutable_digest": replacement_selection["revision"][
                    "immutable_digest"
                ],
                "forge_job_id": replacement_selection["job_id"],
                "forge_reference": replacement_selection["reference"],
            },
        )
        self.assertNotEqual(
            replaced["snapshot"]["session_id"], old_snapshot["session_id"]
        )
        with self.assertRaises(ProductLifecycleError) as revoked:
            self.stage.snapshot(
                actor_authority=PRESENTER_AUTHORITY,
                session_authority=created["session_authority"],
                reference=old_snapshot["reference"],
                expected_epoch=self.stage.service_epoch,
            )
        self.assertEqual(revoked.exception.code, "authority_denied")

        active_session_ids = set(self.stage._sessions_by_id)
        tombstone_references = set(self.stage._tombstones_by_reference)
        replayed = self.handoff.replace(**replacement_arguments)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["kind"], "forge_stage_session_replaced")
        self.assertEqual(replayed["previous_snapshot"], previous)
        self.assertEqual(replayed["snapshot"], replaced["snapshot"])
        self.assertEqual(replayed["session_authority"], replaced["session_authority"])
        self.assertEqual(replayed["forge_selection"], replacement_selection)
        self.assertEqual(set(self.stage._sessions_by_id), active_session_ids)
        self.assertEqual(set(self.stage._tombstones_by_reference), tombstone_references)
        self.assertEqual(len(active_session_ids), 1)
        self.assertEqual(len(tombstone_references), 1)
        self.assertEqual(
            old_core.journal.event_position.to_dict(),
            previous["coordinates"]["event"],
        )
        self.assertEqual(self.admission.snapshot().active_sessions, 1)

    def test_unselected_unknown_expired_and_stale_epochs_allocate_nothing(
        self,
    ) -> None:
        artifact = self.artifacts.save(_profile("tracking-console"))
        prepared = self.forge.prepare(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.forge.service_epoch,
            recipe_id=BSIDES_RECIPE_ID,
            recipe_revision=BSIDES_RECIPE_REVISION,
            answers=_answers(),
        )
        accepted = self.forge.confirm_and_submit(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.forge.service_epoch,
            idempotency_key="unselected-build",
            preparation_reference=str(prepared["preparation_reference"]),
        )
        for reference, key in (
            (str(accepted["reference"]), "unselected-handoff"),
            ("forge-reference:unknown-selection", "unknown-handoff"),
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(ProductLifecycleError) as captured:
                    self._launch(reference, key)
                self.assertEqual(captured.exception.code, "selected_record_unavailable")

        selected_reference = self._select(artifact, "expiring-build")
        with self.assertRaises(ProductLifecycleError) as reserved_rejection:
            self._launch(str(accepted["reference"]), "unselected-handoff")
        self.assertEqual(
            reserved_rejection.exception.code,
            "selected_record_unavailable",
        )
        with self.assertRaises(ProductLifecycleError) as changed_rejection:
            self._launch(selected_reference, "unselected-handoff")
        self.assertEqual(changed_rejection.exception.code, "idempotency_conflict")
        self.clock.value += timedelta(seconds=300)
        with self.assertRaises(ProductLifecycleError) as expired:
            self._launch(selected_reference, "expired-handoff")
        self.assertEqual(expired.exception.code, "selected_record_unavailable")

        self.assertEqual(self.stage._revisions, {})
        self.assertEqual(self.stage._launch_grants, {})
        self.assertEqual(self.admission.snapshot().active_sessions, 0)

        with self.assertRaises(ProductLifecycleError) as stale_stage:
            self.handoff.launch(
                actor_authority=PRESENTER_AUTHORITY,
                forge_expected_epoch=self.forge.service_epoch,
                stage_expected_epoch="epoch:stale-stage-service",
                forge_reference=selected_reference,
                idempotency_key="stale-stage-handoff",
            )
        self.assertEqual(stale_stage.exception.code, "outcome_unknown")

        old_forge_epoch = self.forge.service_epoch
        self.forge.preview_rotate_epoch()
        with self.assertRaises(ProductLifecycleError) as stale_forge:
            self.handoff.launch(
                actor_authority=PRESENTER_AUTHORITY,
                forge_expected_epoch=old_forge_epoch,
                stage_expected_epoch=self.stage.service_epoch,
                forge_reference=selected_reference,
                idempotency_key="stale-forge-handoff",
            )
        self.assertEqual(stale_forge.exception.code, "outcome_unknown")

    def test_tampered_artifact_rejection_is_replayed_without_stage_mutation(
        self,
    ) -> None:
        artifact = self.artifacts.save(_profile("tracking-console"))
        reference = self._select(artifact, "tampered-build")
        artifact.path.write_bytes(artifact.path.read_bytes() + b" ")

        for _attempt in range(2):
            with self.assertRaises(ProductLifecycleError) as captured:
                self._launch(reference, "tampered-handoff")
            self.assertEqual(captured.exception.code, "selected_artifact_unavailable")
        self.assertEqual(self.stage._revisions, {})
        self.assertEqual(self.stage._launch_grants, {})
        self.assertEqual(self.admission.snapshot().active_sessions, 0)

    def test_selected_record_expiry_caps_the_created_session(self) -> None:
        artifact = self.artifacts.save(_profile("tracking-console"))
        reference = self._select(artifact, "ttl-capped-build")
        limits = _launch_limits()
        limits["absolute_ttl_ms"] = 600_000
        handoff = ForgeStageHandoffService(
            forge=self.forge,
            artifacts=self.artifacts,
            stage=self.stage,
            policy=ForgeStageHandoffPolicy(
                launch_limits=limits,
                notice_version="notice:ttl-cap-v1",
            ),
            token_factory=SequenceTokens(),
        )

        created = handoff.launch(
            actor_authority=PRESENTER_AUTHORITY,
            forge_expected_epoch=self.forge.service_epoch,
            stage_expected_epoch=self.stage.service_epoch,
            forge_reference=reference,
            idempotency_key="ttl-capped-handoff",
        )

        self.assertEqual(
            created["snapshot"]["lifecycle"]["absolute_expires_at_ms"],
            self.clock.milliseconds() + 300_000,
        )

    def test_wrong_actor_is_denied_before_claim_or_stage_mutation(self) -> None:
        artifact = self.artifacts.save(_profile("tracking-console"))
        reference = self._select(artifact, "wrong-actor-build")

        with self.assertRaises(ProductLifecycleError) as denied:
            self.handoff.launch(
                actor_authority="actor:another-presenter-authority-0001",
                forge_expected_epoch=self.forge.service_epoch,
                stage_expected_epoch=self.stage.service_epoch,
                forge_reference=reference,
                idempotency_key="wrong-actor-handoff",
            )

        self.assertEqual(denied.exception.code, "authority_denied")
        self.assertEqual(self.stage._revisions, {})
        self.assertEqual(self.handoff._claims, {})
        self.assertEqual(self.admission.snapshot().active_sessions, 0)

    def test_policy_and_coordinator_capacities_are_bounded(self) -> None:
        limits = _launch_limits()
        policy = ForgeStageHandoffPolicy(
            launch_limits=limits,
            notice_version="notice:bounded-v1",
            maximum_bindings=1,
            maximum_idempotency_records=2,
        )
        limits["max_commands"] = 99
        self.assertEqual(policy.launch_limits["max_commands"], 8)
        with self.assertRaises(TypeError):
            policy.launch_limits["max_commands"] = 4  # type: ignore[index]

        for invalid_limits, notice in (
            ({"Bad Limit": 1}, "notice:valid"),
            ({"max_commands": 0}, "notice:valid"),
            ({"max_commands": 1_000_000_001}, "notice:valid"),
            ({"max_commands": 1}, "unsafe notice"),
        ):
            with self.subTest(limits=invalid_limits, notice=notice):
                with self.assertRaises(ValueError):
                    ForgeStageHandoffPolicy(
                        launch_limits=invalid_limits,
                        notice_version=notice,
                    )
        for bindings, records in ((True, 2), (0, 2), (1, True), (2, 1), (1, 257)):
            with self.subTest(bindings=bindings, records=records):
                with self.assertRaises(ValueError):
                    ForgeStageHandoffPolicy(
                        launch_limits={"max_commands": 1},
                        notice_version="notice:valid",
                        maximum_bindings=bindings,
                        maximum_idempotency_records=records,
                    )

        bounded = ForgeStageHandoffService(
            forge=self.forge,
            artifacts=self.artifacts,
            stage=self.stage,
            policy=policy,
            token_factory=SequenceTokens(),
        )
        first = self.artifacts.save(_profile("tracking-console"))
        second = self.artifacts.save(_profile("spectrometer-console"))
        first_reference = self._select(first, "bounded-first")
        second_reference = self._select(second, "bounded-second")
        bounded.launch(
            actor_authority=PRESENTER_AUTHORITY,
            forge_expected_epoch=self.forge.service_epoch,
            stage_expected_epoch=self.stage.service_epoch,
            forge_reference=first_reference,
            idempotency_key="bounded-first-handoff",
        )
        for _attempt in range(2):
            with self.assertRaises(ProductLifecycleError) as exhausted:
                bounded.launch(
                    actor_authority=PRESENTER_AUTHORITY,
                    forge_expected_epoch=self.forge.service_epoch,
                    stage_expected_epoch=self.stage.service_epoch,
                    forge_reference=second_reference,
                    idempotency_key="bounded-second-handoff",
                )
            self.assertEqual(
                exhausted.exception.code,
                "handoff_capacity_exhausted",
            )
        with self.assertRaises(ProductLifecycleError) as replay_capacity:
            bounded.launch(
                actor_authority=PRESENTER_AUTHORITY,
                forge_expected_epoch=self.forge.service_epoch,
                stage_expected_epoch=self.stage.service_epoch,
                forge_reference=first_reference,
                idempotency_key="third-handoff-claim",
            )
        self.assertEqual(
            replay_capacity.exception.code,
            "idempotency_capacity_exhausted",
        )
        self.assertEqual(self.admission.snapshot().active_sessions, 1)

    def _select(self, artifact: ImmutableForgeRevision, key: str) -> str:
        prepared = self.forge.prepare(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.forge.service_epoch,
            recipe_id=BSIDES_RECIPE_ID,
            recipe_revision=BSIDES_RECIPE_REVISION,
            answers=_answers(),
        )
        accepted = self.forge.confirm_and_submit(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.forge.service_epoch,
            idempotency_key=key,
            preparation_reference=str(prepared["preparation_reference"]),
        )
        reference = str(accepted["reference"])
        for state in (
            ForgeJobState.QUEUED,
            ForgeJobState.BUILDING,
            ForgeJobState.GENERATED,
            ForgeJobState.VALIDATING,
        ):
            self.forge.preview_transition(reference=reference, state=state)
        self.forge.complete_selected(
            reference=reference,
            receipt=live_forge_receipt(
                compiler_revision=FORGE_COMPILER_REVISION,
                validator_revision=FORGE_VALIDATOR_REVISION,
                revision_id=artifact.revision_id,
                digest=artifact.digest,
            ),
        )
        return reference

    def _launch(self, reference: str, key: str) -> dict[str, object]:
        return self.handoff.launch(
            actor_authority=PRESENTER_AUTHORITY,
            forge_expected_epoch=self.forge.service_epoch,
            stage_expected_epoch=self.stage.service_epoch,
            forge_reference=reference,
            idempotency_key=key,
        )

    def _assert_exact_identity(
        self,
        result: dict[str, object],
        artifact: ImmutableForgeRevision,
        forge_reference: str,
    ) -> None:
        selection = result["forge_selection"]
        self.assertEqual(selection["reference"], forge_reference)
        self.assertEqual(selection["revision"]["revision_id"], artifact.revision_id)
        self.assertEqual(selection["revision"]["immutable_digest"], artifact.digest)
        snapshot = result["snapshot"]
        self.assertEqual(
            snapshot["starting_revision"]["revision_id"], artifact.revision_id
        )
        self.assertEqual(
            snapshot["stage"]["source"]["immutable_digest"], artifact.digest
        )
        self.assertEqual(
            snapshot["stage"]["source"]["forge_reference"], forge_reference
        )
        selected = self.forge.resolve_selected_record(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.forge.service_epoch,
            job_reference=forge_reference,
        )
        expected_display = forge_stage_display_metadata(
            selected,
            self.handoff._policy,
        )
        self.assertEqual(
            snapshot["stage"]["display"],
            {
                "title": expected_display.title,
                "source_label": expected_display.source_label,
                "revision_label": expected_display.revision_label,
            },
        )
        # Product lifecycle identity covers its newline-free runtime document;
        # it must never be relabelled as the immutable Forge stored-byte digest.
        self.assertNotEqual(
            snapshot["starting_revision"]["sha256"],
            artifact.digest.removeprefix("sha256:"),
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


def _profile(hostname: str) -> StateProfile:
    return StateProfile(
        name=f"candidate-{hostname}",
        level="world",
        created_at="2026-08-26T12:00:00+00:00",
        state={
            "world_state": {
                "name": f"world-{hostname}",
                "entry_host": hostname,
                "active_host": hostname,
                "purpose": {
                    "theme": "Weathered comet observatory",
                    "description": "Radio clocks disagree during a comet watch.",
                },
                "hosts_by_name": {
                    hostname: {
                        "login_banner": "Test console\n",
                        "hostname": hostname,
                        "purpose": {"theme": "Track the comet"},
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


def _launch_limits() -> dict[str, int]:
    return {
        "absolute_ttl_ms": 120_000,
        "idle_ttl_ms": 60_000,
        "max_commands": 8,
        "max_command_bytes": 4_096,
        "command_reserved_tokens": 100,
        "command_reserved_spend_units": 100,
        "command_max_attempts": 1,
        "command_deadline_ms": 10_000,
    }


def _admission_limits() -> ProductAdmissionLimits:
    return ProductAdmissionLimits(
        max_active_sessions=2,
        max_logical_jobs=8,
        max_physical_workers=2,
        token_budget=800,
        spend_budget_units=800,
        presenter_stage_reserved_sessions=1,
        presenter_stage_reserved_logical_jobs=4,
        presenter_stage_reserved_physical_workers=1,
        presenter_stage_reserved_token_budget=400,
        presenter_stage_reserved_spend_budget_units=400,
    )


if __name__ == "__main__":
    unittest.main()
