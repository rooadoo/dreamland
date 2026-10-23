from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dreamland.product.forge import (
    BSIDES_RECIPE_ID,
    BSIDES_RECIPE_REVISION,
    ForgeActorSeed,
    ForgeJobState,
    ForgePolicy,
    ForgeServiceError,
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
    forge_stage_display_metadata,
)
from dreamland.product.launch_selection import (
    LaunchSelectionActorSeed,
    LaunchSelectionPolicy,
    LaunchSelectionRegistry,
)
from dreamland.product.launch_selection_forge import (
    ForgeLaunchSelectionRegistrar,
)
from dreamland.product.lifecycle import ProductOperation, ProductRole
from dreamland.state_profiles import StateProfile

PRESENTER_AUTHORITY = "actor:phase7b-forge-presenter-authority-0001"
PRESENTER_ID = "phase7b-forge-presenter"


class SharedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

    def forge(self) -> datetime:
        return self.value

    def milliseconds(self) -> int:
        return int(self.value.timestamp() * 1_000)

    def advance(self, milliseconds: int) -> None:
        self.value += timedelta(milliseconds=milliseconds)


class FixedMonotonicClock:
    def __init__(self, milliseconds: int) -> None:
        self.value = milliseconds

    def milliseconds(self) -> int:
        return self.value


class SequenceTokens:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self, purpose: str) -> str:
        self.index += 1
        return f"{purpose}:phase7b-forge-test-{self.index:08d}"


class RegistryTokens:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self, purpose: str) -> str:
        self.index += 1
        if purpose == "epoch":
            return f"epoch:phase7b_registry_{self.index:08d}"
        if purpose == "launch-grant":
            return f"launch-grant:phase7b_candidate_{self.index:08d}"
        raise ValueError("unsupported registry token purpose")


class ProductLaunchSelectionForgeTests(unittest.TestCase):
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
                    expires_at=(self.clock.value + timedelta(hours=2)).isoformat(),
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
        self.registry = LaunchSelectionRegistry(
            actors=(
                LaunchSelectionActorSeed(
                    authority=PRESENTER_AUTHORITY,
                    actor_id=PRESENTER_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=self.clock.milliseconds() + 2 * 60 * 60 * 1_000,
                ),
            ),
            policy=LaunchSelectionPolicy(
                max_revisions=16,
                max_offerings=16,
                max_actor_authorities=2,
                max_active_launch_grants=16,
                max_idempotency_records=32,
                max_token_history=64,
                max_revision_bytes=4 * 1024 * 1024,
                max_launch_grant_ttl_ms=30 * 60 * 1_000,
                idempotency_ttl_ms=40 * 60 * 1_000,
            ),
            allowed_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            clock_ms=self.clock.milliseconds,
            monotonic_ms=self.clock.milliseconds,
            token_factory=RegistryTokens(),
        )
        self.stage_policy = ForgeStageHandoffPolicy(
            launch_limits={
                "absolute_ttl_ms": 600_000,
                "idle_ttl_ms": 60_000,
                "max_commands": 8,
                "max_command_bytes": 4_096,
                "command_reserved_tokens": 100,
                "command_reserved_spend_units": 100,
                "command_max_attempts": 1,
                "command_deadline_ms": 10_000,
            },
            notice_version="notice:phase7b-forge-selection-v1",
        )
        self.registrar = self._registrar()

    def _registrar(
        self,
        *,
        actor_id: str = PRESENTER_ID,
        artifacts: ImmutableForgeStore | None = None,
        registry: LaunchSelectionRegistry | None = None,
    ) -> ForgeLaunchSelectionRegistrar:
        return ForgeLaunchSelectionRegistrar(
            forge=self.forge,
            artifacts=artifacts or self.artifacts,
            registry=registry or self.registry,
            actor_authority=PRESENTER_AUTHORITY,
            actor_id=actor_id,
            stage_policy=self.stage_policy,
            locator_key=b"phase7b-test-locator-key-32bytes!",
            clock_ms=self.clock.milliseconds,
        )

    def test_only_authoritative_selected_record_can_register_exact_artifact(
        self,
    ) -> None:
        artifact = self.artifacts.save(_profile("selected-console"))
        reference = self._submit("selected-only")

        with self.assertRaises(ForgeServiceError) as unavailable:
            self.registrar.register_selected(reference)
        self.assertEqual(unavailable.exception.code, "job_not_found")
        self.assertEqual(self._options(), [])
        self.assertEqual(self.registry._revisions, {})

        self._complete(reference, artifact)
        selected = self.forge.resolve_selected_record(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.forge.service_epoch,
            job_reference=reference,
        )
        expected_display = forge_stage_display_metadata(selected, self.stage_policy)

        first = self.registrar.register_selected(reference)
        self.clock.advance(1_000)
        replay = self.registrar.register_selected(reference)
        options = self._options()

        self.assertEqual(replay, first)
        self.assertEqual(options, [first])
        self.assertEqual(first["origin"], {"kind": "world_forge_selection"})
        self.assertEqual(
            first["revision"],
            {
                "revision_id": artifact.revision_id,
                "sha256": artifact.digest.removeprefix("sha256:"),
            },
        )
        self.assertEqual(
            first["display"],
            {
                "title": expected_display.title,
                "source_label": expected_display.source_label,
                "revision_label": expected_display.revision_label,
            },
        )
        self.assertEqual(
            datetime.fromisoformat(str(first["expires_at"]).replace("Z", "+00:00")),
            datetime.fromisoformat(selected.expires_at.replace("Z", "+00:00")),
        )
        serialized = repr(first)
        for forbidden in (
            str(artifact.path),
            selected.selected_record_id,
            selected.job_reference,
            PRESENTER_AUTHORITY,
        ):
            self.assertNotIn(forbidden, serialized)

    def test_exact_reopen_failure_and_actor_mismatch_publish_nothing(self) -> None:
        artifact = self.artifacts.save(_profile("integrity-console"))
        reference = self._select(artifact, "integrity")

        wrong_actor = self._registrar(actor_id="different-presenter")
        with self.assertRaisesRegex(ValueError, "actor is incompatible"):
            wrong_actor.register_selected(reference)
        self.assertEqual(self._options(), [])

        artifact.path.write_bytes(artifact.path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            self.registrar.register_selected(reference)
        self.assertEqual(self._options(), [])
        self.assertEqual(self.registry._revisions, {})

    def test_distinct_selected_records_for_one_revision_share_display_truth(
        self,
    ) -> None:
        artifact = self.artifacts.save(_profile("shared-display-console"))
        first_reference = self._select(artifact, "shared-display-first")
        second_reference = self._select(artifact, "shared-display-second")

        first = self.registrar.register_selected(first_reference)
        second = self.registrar.register_selected(second_reference)
        options = self._options()

        self.assertNotEqual(first["offering_id"], second["offering_id"])
        self.assertNotEqual(first["launch_selection_id"], second["launch_selection_id"])
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(first["display"], second["display"])
        self.assertEqual(len(options), 2)
        self.assertEqual(
            {item["offering_id"] for item in options},
            {
                first["offering_id"],
                second["offering_id"],
            },
        )

    def test_registered_option_expires_with_selected_record(self) -> None:
        artifact = self.artifacts.save(_profile("expiring-console"))
        reference = self._select(artifact, "expiring")
        registered = self.registrar.register_selected(reference)
        self.assertEqual(self._options(), [registered])

        self.clock.advance(300_000)

        self.assertEqual(self._options(), [])
        with self.assertRaises(ForgeServiceError) as expired:
            self.registrar.register_selected(reference)
        self.assertEqual(expired.exception.code, "job_not_found")

    def test_forward_wall_adjustment_cannot_outlive_selected_record(self) -> None:
        monotonic = FixedMonotonicClock(self.clock.milliseconds())
        registry = LaunchSelectionRegistry(
            actors=(
                LaunchSelectionActorSeed(
                    authority=PRESENTER_AUTHORITY,
                    actor_id=PRESENTER_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=self.clock.milliseconds() + 2 * 60 * 60 * 1_000,
                ),
            ),
            policy=LaunchSelectionPolicy(
                max_revisions=16,
                max_offerings=16,
                max_actor_authorities=2,
                max_active_launch_grants=16,
                max_idempotency_records=32,
                max_token_history=64,
                max_revision_bytes=4 * 1024 * 1024,
                max_launch_grant_ttl_ms=30 * 60 * 1_000,
                idempotency_ttl_ms=40 * 60 * 1_000,
            ),
            allowed_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            clock_ms=self.clock.milliseconds,
            monotonic_ms=monotonic.milliseconds,
            token_factory=RegistryTokens(),
        )
        artifact = self.artifacts.save(_profile("forward-wall-console"))
        reference = self._select(artifact, "forward-wall")
        registered = self._registrar(registry=registry).register_selected(reference)
        issue = registry.exchange_selection(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=registry.service_epoch,
            launch_selection_id=str(registered["launch_selection_id"]),
            idempotency_key="forward-wall-exchange",
            provision=lambda _issue, _resolved: None,
            required_role=ProductRole.PRESENTER,
        )

        self.clock.advance(300_000)

        self.assertEqual(
            registry.offerings(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch=registry.service_epoch,
                required_role=ProductRole.PRESENTER,
            ),
            [],
        )
        with self.assertRaisesRegex(Exception, "unavailable") as expired_grant:
            registry.resolve_launch_grant(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch=registry.service_epoch,
                launch_grant=issue.launch_grant,
                operation=ProductOperation.CREATE,
            )
        self.assertEqual(expired_grant.exception.code, "launch_grant_unavailable")
        with self.assertRaises(ForgeServiceError) as expired_record:
            self.forge.resolve_selected_record(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch=self.forge.service_epoch,
                job_reference=reference,
            )
        self.assertEqual(expired_record.exception.code, "job_not_found")

    def _options(self) -> list[dict[str, object]]:
        return self.registry.offerings(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=self.registry.service_epoch,
            required_role=ProductRole.PRESENTER,
        )

    def _submit(self, key: str) -> str:
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
        return str(accepted["reference"])

    def _complete(
        self,
        reference: str,
        artifact: ImmutableForgeRevision,
    ) -> None:
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

    def _select(self, artifact: ImmutableForgeRevision, key: str) -> str:
        reference = self._submit(key)
        self._complete(reference, artifact)
        return reference


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
        created_at="2026-08-27T12:00:00+00:00",
        state={
            "world_state": {
                "name": f"world-{hostname}",
                "entry_host": hostname,
                "active_host": hostname,
                "purpose": {"theme": "Selected Forge registration test"},
                "hosts_by_name": {
                    hostname: {
                        "login_banner": "Test console\n",
                        "hostname": hostname,
                        "purpose": {"theme": "Track a generated world"},
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


if __name__ == "__main__":
    unittest.main()
