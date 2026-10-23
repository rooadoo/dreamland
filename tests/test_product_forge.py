from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

from dreamland.product.forge import (
    BSIDES_RECIPE_ID,
    BSIDES_RECIPE_REVISION,
    ForgeActorSeed,
    ForgeJobState,
    ForgePolicy,
    ForgeSelectedRevisionRecord,
    ForgeServiceError,
    WorldForgeService,
    WorldSeedAnswer,
    WorldSeedValidationError,
    confirm_world_seed,
    prepare_world_seed,
    render_bsides_world_seed,
    world_seed_from_document,
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


def _seed(**changes):
    answers = changes.pop("answers", _answers())
    confirmed_at = changes.pop("confirmed_at", "2026-08-25T12:34:56Z")
    preparation = prepare_world_seed(
        recipe_id=changes.pop("recipe_id", BSIDES_RECIPE_ID),
        recipe_revision=changes.pop("recipe_revision", BSIDES_RECIPE_REVISION),
        answers=answers,
        **changes,
    )
    return confirm_world_seed(
        preparation,
        confirmed_at=confirmed_at,
    )


def _live_source() -> dict[str, object]:
    return {
        "kind": "single_attempt_live_vertical_slice",
        "label": "Single-attempt live World Forge vertical slice",
        "provider_calls": True,
        "live_forge_evidence": True,
        "compiler_revision": "1",
    }


def _live_receipt() -> dict[str, object]:
    return {
        "validation": {
            "state": "passed",
            "safe_summary": "The candidate passed the configured validators.",
            "validator_revision": "1",
        },
        "fixed_selection_reason": "Attempt A was the fixed selected candidate.",
        "revision": {
            "revision_id": "world-revision:sha256:" + "a" * 64,
            "revision_label": "Audience world candidate",
            "immutable_digest": "sha256:" + "a" * 64,
        },
        "human_selection": "none",
        "provenance": _live_source(),
    }


def _fingerprint(document: object) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class WorldSeedTests(unittest.TestCase):
    def test_import_is_inert(self) -> None:
        code = """
import json
import threading
before = [thread.name for thread in threading.enumerate()]
from dreamland.product.forge import BSIDES_RECIPE_ID
after = [thread.name for thread in threading.enumerate()]
print(json.dumps({"before": before, "after": after, "recipe": BSIDES_RECIPE_ID}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["before"], payload["after"])
        self.assertEqual(payload["recipe"], BSIDES_RECIPE_ID)

    def test_round_trip_is_exact_and_deterministic(self) -> None:
        seed = _seed()

        document = seed.to_document()
        round_trip = world_seed_from_document(
            json.loads(json.dumps(document, ensure_ascii=False))
        )

        self.assertEqual(round_trip, seed)
        self.assertEqual(round_trip.to_document(), document)
        self.assertEqual(round_trip.answers[1].value, "+02026")
        self.assertEqual(
            seed.rendered_sentence,
            "On Saturday in +02026, inside a weathered observatory, people rely "
            "on VAX terminals and radio clocks to track a comet. Today, the "
            "clocks disagree.",
        )

    def test_one_exact_field_change_changes_hash(self) -> None:
        original = _seed()
        changed_answers = list(_answers())
        changed_answers[2] = WorldSeedAnswer("adjective", "short-text", "dusty")
        changed = _seed(answers=tuple(changed_answers))

        self.assertNotEqual(original.seed_hash, changed.seed_hash)

    def test_preparation_is_unconfirmed_and_rendered_only_by_service(self) -> None:
        preparation = prepare_world_seed(
            recipe_id=BSIDES_RECIPE_ID,
            recipe_revision=BSIDES_RECIPE_REVISION,
            answers=_answers(),
        )
        self.assertEqual(preparation.confirmation_state, "unconfirmed")
        self.assertNotIn("confirmed_at", preparation.to_document())
        self.assertEqual(
            preparation.rendered_sentence, render_bsides_world_seed(_answers())
        )

    def test_confirmed_document_rendered_sentence_cannot_be_rewritten(self) -> None:
        document = _seed().to_document()
        document["rendered_sentence"] = "A presenter-improved scenario."
        with self.assertRaises(WorldSeedValidationError) as captured:
            world_seed_from_document(document)

        self.assertIn(
            "rendered_sentence_mismatch",
            {issue.code for issue in captured.exception.issues},
        )

    def test_field_order_kind_and_limits_are_backend_enforced(self) -> None:
        answers = list(_answers())
        answers[0], answers[1] = answers[1], answers[0]
        answers[7] = WorldSeedAnswer("wildcard-event", "short-text", "x" * 161)

        with self.assertRaises(WorldSeedValidationError) as captured:
            _seed(answers=tuple(answers))

        issues = {(issue.code, issue.field_id) for issue in captured.exception.issues}
        self.assertIn(("answer_order_invalid", "year"), issues)
        self.assertIn(("answer_order_invalid", "day-or-date"), issues)
        self.assertIn(("text_length_invalid", "wildcard-event"), issues)

    def test_field_level_rejections_precede_any_provider_boundary(self) -> None:
        cases = {
            "blank": "   ",
            "unsafe_character": "quiet\u202e",
            "sensitive_identifier": "admin@example.com",
            "ipv4": "10.23.45.67",
            "secret": "api_key=abcdefghijklmnopqrstuvwxyz012345",
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                answers = list(_answers())
                answers[3] = WorldSeedAnswer("industry-or-setting", "short-text", value)
                with self.assertRaises(WorldSeedValidationError) as captured:
                    _seed(answers=tuple(answers))
                self.assertTrue(
                    any(
                        issue.field_id == "industry-or-setting"
                        for issue in captured.exception.issues
                    )
                )

    def test_year_preserves_exact_valid_text_and_rejects_invalid_values(self) -> None:
        self.assertEqual(_seed().answers[1].value, "+02026")
        for value in (" 2026", "0", "10000", "20.26"):
            with self.subTest(value=value):
                answers = list(_answers())
                answers[1] = WorldSeedAnswer("year", "integer", value)
                with self.assertRaises(WorldSeedValidationError):
                    _seed(answers=tuple(answers))

    def test_document_hash_and_shape_are_verified(self) -> None:
        document = _seed().to_document()
        document["seed_hash"] = "sha256:" + "0" * 64
        with self.assertRaises(WorldSeedValidationError) as captured:
            world_seed_from_document(document)
        self.assertEqual(captured.exception.issues[0].code, "seed_hash_mismatch")

        document = _seed().to_document()
        document["surprise"] = True
        with self.assertRaises(WorldSeedValidationError) as captured:
            world_seed_from_document(document)
        self.assertEqual(captured.exception.issues[0].code, "document_shape_invalid")

    def test_confirmation_requires_a_zoned_timestamp(self) -> None:
        for value in ("2026-08-25T12:34:56", "tomorrow", "2026-02-30T12:00:00Z"):
            with self.subTest(value=value):
                with self.assertRaises(WorldSeedValidationError) as captured:
                    _seed(confirmed_at=value)
                self.assertIn(
                    "confirmation_time_invalid",
                    {issue.code for issue in captured.exception.issues},
                )


class SequenceTokens:
    def __init__(self) -> None:
        self.value = 0
        self.purposes: list[str] = []

    def __call__(self, purpose: str) -> str:
        self.purposes.append(purpose)
        self.value += 1
        return f"{purpose}:test-{self.value:08d}"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class WorldForgeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.tokens = SequenceTokens()
        self.selected_tokens = SequenceTokens()
        self.authority = "forge-presenter-authority"
        self.service = self._service()

    def _service(
        self,
        *,
        policy: ForgePolicy | None = None,
        source: dict[str, object] | None = None,
    ) -> WorldForgeService:
        return WorldForgeService(
            actors=(
                ForgeActorSeed(
                    authority=self.authority,
                    actor_id="presenter-alpha",
                    expires_at=(self.clock.value + timedelta(hours=1)).isoformat(),
                ),
            ),
            policy=policy,
            clock=self.clock,
            token_factory=self.tokens,
            selected_record_token_factory=self.selected_tokens,
            source=source,
        )

    def _submit(self, key: str = "submit-1", seed=None):
        prepared = self._prepare(
            answers=(seed.answers if seed is not None else _answers())
        )
        return self.service.confirm_and_submit(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            idempotency_key=key,
            preparation_reference=str(prepared["preparation_reference"]),
        )

    def _prepare(self, *, answers=None):
        return self.service.prepare(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            recipe_id=BSIDES_RECIPE_ID,
            recipe_revision=BSIDES_RECIPE_REVISION,
            answers=answers or _answers(),
        )

    def _advance_to_validating(self, reference: str) -> None:
        for state in (
            ForgeJobState.QUEUED,
            ForgeJobState.BUILDING,
            ForgeJobState.GENERATED,
            ForgeJobState.VALIDATING,
        ):
            self.service.preview_transition(reference=reference, state=state)

    def _select_live(self, key: str = "live-submit"):
        accepted = self._submit(key)
        reference = str(accepted["reference"])
        self._advance_to_validating(reference)
        projection = self.service.complete_selected(
            reference=reference, receipt=_live_receipt()
        )
        return accepted, projection

    def test_submit_and_exact_replay_allocate_one_job(self) -> None:
        prepared = self._prepare()
        accepted = self.service.confirm_and_submit(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            idempotency_key="submit-1",
            preparation_reference=str(prepared["preparation_reference"]),
        )
        replayed = self.service.confirm_and_submit(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            idempotency_key="submit-1",
            preparation_reference=str(prepared["preparation_reference"]),
        )

        self.assertFalse(accepted["replayed"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(accepted["job_id"], replayed["job_id"])
        projection = self.service.status(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            reference=str(accepted["reference"]),
        )
        self.assertEqual(projection["lifecycle"]["state"], "accepted")
        self.assertEqual(projection["seed"]["confirmation_state"], "confirmed")
        self.assertEqual(projection["seed"]["confirmed_at"], "2026-08-25T12:00:00.000Z")

    def test_prepare_returns_exact_unconfirmed_review_and_recipe_limits(self) -> None:
        recipe = self.service.recipe()
        year = next(field for field in recipe["fields"] if field["field_id"] == "year")
        self.assertEqual(year["maximum_input_characters"], 16)
        self.assertEqual(year["minimum"], 1)
        self.assertEqual(year["maximum"], 9999)

        prepared = self._prepare()
        preparation = prepared["preparation"]
        self.assertEqual(preparation["confirmation_state"], "unconfirmed")
        self.assertNotIn("confirmed_at", preparation)
        self.assertEqual(preparation["answers"][1]["value"], "+02026")
        self.assertEqual(
            prepared["display"]["rendered_sentence"]["text"],
            render_bsides_world_seed(_answers()),
        )

    def test_new_key_cannot_resubmit_consumed_preparation(self) -> None:
        prepared = self._prepare()
        reference = str(prepared["preparation_reference"])
        self.service.confirm_and_submit(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            idempotency_key="first-submit",
            preparation_reference=reference,
        )
        for _index in range(2):
            with self.assertRaises(ForgeServiceError) as captured:
                self.service.confirm_and_submit(
                    actor_authority=self.authority,
                    expected_epoch=self.service.service_epoch,
                    idempotency_key="second-submit",
                    preparation_reference=reference,
                )
            self.assertEqual(captured.exception.code, "preparation_consumed")

    def test_expired_preparation_cannot_be_confirmed(self) -> None:
        self.service = self._service(policy=ForgePolicy(preparation_ttl_seconds=1))
        prepared = self._prepare()
        self.clock.value += timedelta(seconds=2)
        with self.assertRaises(ForgeServiceError) as captured:
            self.service.confirm_and_submit(
                actor_authority=self.authority,
                expected_epoch=self.service.service_epoch,
                idempotency_key="expired-preparation",
                preparation_reference=str(prepared["preparation_reference"]),
            )
        self.assertEqual(captured.exception.code, "preparation_expired")

    def test_changed_body_reuse_conflicts(self) -> None:
        first = self._prepare()
        second_answers = list(_answers())
        second_answers[2] = WorldSeedAnswer("adjective", "short-text", "dusty")
        second = self._prepare(answers=tuple(second_answers))
        self.service.confirm_and_submit(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            idempotency_key="submit-1",
            preparation_reference=str(first["preparation_reference"]),
        )
        with self.assertRaises(ForgeServiceError) as captured:
            self.service.confirm_and_submit(
                actor_authority=self.authority,
                expected_epoch=self.service.service_epoch,
                idempotency_key="submit-1",
                preparation_reference=str(second["preparation_reference"]),
            )
        self.assertEqual(captured.exception.code, "idempotency_conflict")

    def test_rejected_authority_does_not_claim_an_authenticated_scope(self) -> None:
        with self.assertRaises(ForgeServiceError) as captured:
            self.service.confirm_and_submit(
                actor_authority="not-an-authority",
                expected_epoch=self.service.service_epoch,
                idempotency_key="shared-key",
                preparation_reference="forge-preparation:unknown",
            )
        self.assertEqual(captured.exception.code, "authority_denied")
        accepted = self._submit("shared-key")
        self.assertFalse(accepted["replayed"])

    def test_capacity_rejection_is_reserved_and_replayed(self) -> None:
        self.service = self._service(
            policy=ForgePolicy(maximum_jobs=1, maximum_idempotency_records=4)
        )
        self._submit("first")
        capacity_preparation = self._prepare()

        for _index in range(2):
            with self.assertRaises(ForgeServiceError) as captured:
                self.service.confirm_and_submit(
                    actor_authority=self.authority,
                    expected_epoch=self.service.service_epoch,
                    idempotency_key="capacity",
                    preparation_reference=str(
                        capacity_preparation["preparation_reference"]
                    ),
                )
            self.assertEqual(captured.exception.code, "capacity_exhausted")
            self.assertEqual(captured.exception.retry, "same_request")

        changed_preparation = self._prepare()
        with self.assertRaises(ForgeServiceError) as changed:
            self.service.confirm_and_submit(
                actor_authority=self.authority,
                expected_epoch=self.service.service_epoch,
                idempotency_key="capacity",
                preparation_reference=str(changed_preparation["preparation_reference"]),
            )
        self.assertEqual(changed.exception.code, "idempotency_conflict")

    def test_idempotency_capacity_is_not_retryable_within_the_epoch(self) -> None:
        self.service = self._service(
            policy=ForgePolicy(maximum_jobs=1, maximum_idempotency_records=1)
        )
        self._submit("first")
        preparation = self._prepare()
        for _index in range(2):
            with self.assertRaises(ForgeServiceError) as captured:
                self.service.confirm_and_submit(
                    actor_authority=self.authority,
                    expected_epoch=self.service.service_epoch,
                    idempotency_key="ledger-full",
                    preparation_reference=str(preparation["preparation_reference"]),
                )
            self.assertEqual(captured.exception.code, "idempotency_capacity_exhausted")
            self.assertEqual(captured.exception.retry, "none")

    def test_preview_lifecycle_and_events_are_monotonic(self) -> None:
        accepted = self._submit()
        reference = str(accepted["reference"])
        states = (
            ForgeJobState.QUEUED,
            ForgeJobState.BUILDING,
            ForgeJobState.GENERATED,
            ForgeJobState.VALIDATING,
            ForgeJobState.SELECTED,
        )
        for state in states:
            self.clock.value += timedelta(seconds=1)
            projection = self.service.preview_transition(
                reference=reference, state=state
            )
        self.assertTrue(projection["terminal"])
        self.assertEqual(projection["lifecycle"]["state"], "selected")
        self.assertEqual(projection["receipt"]["human_selection"], "none")
        self.assertTrue(
            projection["receipt"]["revision"]["immutable_digest"].startswith("sha256:")
        )
        self.assertEqual(
            projection["receipt"]["provenance"]["kind"],
            "provider_free_integration_preview",
        )
        self.assertFalse(projection["receipt"]["provenance"]["live_forge_evidence"])
        self.assertNotIn("publication", projection)
        self.assertNotIn("stage", projection)

        events = self.service.events(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            reference=reference,
            after_cursor=str(accepted["position"]["cursor"]),
        )
        self.assertEqual(
            [event["state"] for event in events["events"]],
            [state.value for state in states],
        )
        self.assertTrue(events["terminal"])
        self.assertEqual(events["stream_id"], projection["position"]["stream_id"])
        self.assertEqual(events["position"], projection["position"])

    def test_cursor_is_opaque_and_invalid_cursor_requires_resync(self) -> None:
        accepted = self._submit()
        cursor = str(accepted["position"]["cursor"])
        self.assertNotEqual(cursor, str(accepted["position"]["sequence"]))
        with self.assertRaises(ForgeServiceError) as captured:
            self.service.events(
                actor_authority=self.authority,
                expected_epoch=self.service.service_epoch,
                reference=str(accepted["reference"]),
                after_cursor="1",
            )
        self.assertEqual(captured.exception.code, "resync_required")
        self.assertEqual(captured.exception.retry, "resume")

    def test_cancel_is_exact_and_terminal(self) -> None:
        accepted = self._submit()
        cancelled = self.service.cancel(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            reference=str(accepted["reference"]),
            idempotency_key="cancel-1",
        )
        replayed = self.service.cancel(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            reference=str(accepted["reference"]),
            idempotency_key="cancel-1",
        )
        self.assertFalse(cancelled["replayed"])
        self.assertTrue(replayed["replayed"])
        projection = self.service.status(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            reference=str(accepted["reference"]),
        )
        self.assertEqual(projection["lifecycle"]["state"], "cancelled")
        self.assertEqual(
            projection["lifecycle"]["safe_message"],
            "The World Forge build was cancelled.",
        )
        self.assertEqual(projection["cancellation"]["state"], "unavailable")

    def test_selected_job_cannot_be_cancelled_or_advanced(self) -> None:
        accepted = self._submit()
        reference = str(accepted["reference"])
        for state in (
            ForgeJobState.QUEUED,
            ForgeJobState.BUILDING,
            ForgeJobState.GENERATED,
            ForgeJobState.VALIDATING,
            ForgeJobState.SELECTED,
        ):
            self.service.preview_transition(reference=reference, state=state)
        with self.assertRaises(ForgeServiceError) as captured:
            self.service.cancel(
                actor_authority=self.authority,
                expected_epoch=self.service.service_epoch,
                reference=reference,
                idempotency_key="too-late",
            )
        self.assertEqual(captured.exception.code, "job_terminal")
        self.assertEqual(captured.exception.retry, "none")
        with self.assertRaises(ForgeServiceError) as captured:
            self.service.preview_transition(
                reference=reference, state=ForgeJobState.FAILED
            )
        self.assertEqual(captured.exception.code, "transition_invalid")

    def test_deadline_transition_cannot_precede_advertised_deadline(self) -> None:
        accepted = self._submit()
        reference = str(accepted["reference"])
        self.service.preview_transition(reference=reference, state=ForgeJobState.QUEUED)
        self.service.preview_transition(
            reference=reference, state=ForgeJobState.BUILDING
        )
        with self.assertRaises(ForgeServiceError) as early:
            self.service.preview_transition(
                reference=reference, state=ForgeJobState.DEADLINE_EXCEEDED
            )
        self.assertEqual(early.exception.code, "transition_invalid")
        deadline = datetime.fromisoformat(str(accepted["deadline_at"]))
        self.clock.value = deadline
        projection = self.service.preview_transition(
            reference=reference, state=ForgeJobState.DEADLINE_EXCEEDED
        )
        changed_at = datetime.fromisoformat(projection["lifecycle"]["changed_at"])
        self.assertGreaterEqual(changed_at, deadline)

    def test_selection_and_deadline_commit_atomically_in_both_race_orders(
        self,
    ) -> None:
        def validating_job(key: str):
            accepted = self._submit(key)
            reference = str(accepted["reference"])
            for state in (
                ForgeJobState.QUEUED,
                ForgeJobState.BUILDING,
                ForgeJobState.GENERATED,
                ForgeJobState.VALIDATING,
            ):
                self.service.preview_transition(reference=reference, state=state)
            return accepted, reference

        accepted, reference = validating_job("selection-before-deadline")
        deadline = datetime.fromisoformat(str(accepted["deadline_at"]))
        self.clock.value = deadline - timedelta(milliseconds=1)
        selected = self.service.complete_selected(
            reference=reference,
            receipt={"provenance": self.service.source},
        )
        self.assertEqual(selected["lifecycle"]["state"], "selected")
        self.assertLess(
            datetime.fromisoformat(selected["lifecycle"]["changed_at"]), deadline
        )

        self.service = self._service()
        accepted, reference = validating_job("selection-after-deadline")
        deadline = datetime.fromisoformat(str(accepted["deadline_at"]))
        self.clock.value = deadline
        expired = self.service.complete_selected(
            reference=reference,
            receipt={"provenance": self.service.source},
        )
        self.assertEqual(expired["lifecycle"]["state"], "deadline_exceeded")
        self.assertIsNone(expired["receipt"])

        self.service = self._service()
        accepted, reference = validating_job("watcher-before-selection")
        deadline = datetime.fromisoformat(str(accepted["deadline_at"]))
        self.clock.value = deadline
        watcher = self.service.preview_transition(
            reference=reference, state=ForgeJobState.DEADLINE_EXCEEDED
        )
        with self.assertRaises(ForgeServiceError):
            self.service.complete_selected(
                reference=reference,
                receipt={"provenance": self.service.source},
            )
        preserved = self.service.status(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            reference=reference,
        )
        self.assertEqual(preserved, watcher)
        self.assertIsNone(preserved["receipt"])

    def test_live_selection_commits_exact_immutable_private_record(self) -> None:
        policy = ForgePolicy(
            selected_handoff_policy_id="conference-stage:v7",
            selected_handoff_ttl_seconds=90,
        )
        self.service = self._service(policy=policy, source=_live_source())
        accepted = self._submit("authoritative-selection")
        reference = str(accepted["reference"])
        self._advance_to_validating(reference)
        self.clock.value += timedelta(seconds=7)
        receipt = _live_receipt()

        projection = self.service.complete_selected(
            reference=reference, receipt=receipt
        )
        record = self.service.resolve_selected_record(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            job_reference=reference,
        )

        self.assertIsInstance(record, ForgeSelectedRevisionRecord)
        self.assertEqual(record.status, "selected")
        self.assertEqual(record.service_epoch, self.service.service_epoch)
        self.assertEqual(record.job_id, accepted["job_id"])
        self.assertEqual(record.job_reference, reference)
        self.assertEqual(record.actor_id, "presenter-alpha")
        self.assertEqual(record.selected_at, projection["lifecycle"]["changed_at"])
        self.assertEqual(
            datetime.fromisoformat(record.expires_at),
            datetime.fromisoformat(record.selected_at) + timedelta(seconds=90),
        )
        self.assertEqual(record.policy_id, "conference-stage:v7")
        self.assertEqual(
            record.revision_id,
            receipt["revision"]["revision_id"],
        )
        self.assertEqual(
            record.immutable_digest,
            receipt["revision"]["immutable_digest"],
        )
        self.assertEqual(record.source_kind, _live_source()["kind"])
        self.assertEqual(record.source_fingerprint, _fingerprint(_live_source()))
        self.assertEqual(record.receipt_fingerprint, _fingerprint(receipt))
        self.assertTrue(record.selected_record_id.startswith("forge-selected-record:"))
        self.assertTrue(record.receipt_correlation_id.startswith("forge-receipt:"))
        self.assertEqual(
            self.selected_tokens.purposes,
            ["forge-selected-record", "forge-receipt"],
        )
        self.assertNotIn("forge-selected-record", self.tokens.purposes)
        self.assertNotIn("selected_record_id", projection)
        self.assertNotIn("receipt_correlation_id", projection)
        self.assertIs(
            record,
            self.service.resolve_selected_record(
                actor_authority=self.authority,
                expected_epoch=self.service.service_epoch,
                job_reference=reference,
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            record.policy_id = "changed"  # type: ignore[misc]

    def test_only_explicit_live_trusted_selection_mints_a_record(self) -> None:
        non_live_source = {
            "kind": "explicit_provider_free_source",
            "label": "Explicit provider-free source",
            "provider_calls": False,
            "live_forge_evidence": False,
        }
        cases = (
            ("default-preview", None, False),
            ("explicit-non-live", non_live_source, True),
            ("live-preview-transition", _live_source(), False),
        )
        for label, source, trusted_completion in cases:
            with self.subTest(label=label):
                self.clock = MutableClock()
                self.tokens = SequenceTokens()
                self.selected_tokens = SequenceTokens()
                self.service = self._service(source=source)
                accepted = self._submit(label)
                reference = str(accepted["reference"])
                self._advance_to_validating(reference)
                if trusted_completion:
                    receipt = _live_receipt()
                    receipt["provenance"] = source
                    projection = self.service.complete_selected(
                        reference=reference, receipt=receipt
                    )
                else:
                    projection = self.service.preview_transition(
                        reference=reference, state=ForgeJobState.SELECTED
                    )
                self.assertEqual(projection["lifecycle"]["state"], "selected")
                with self.assertRaises(ForgeServiceError) as captured:
                    self.service.resolve_selected_record(
                        actor_authority=self.authority,
                        expected_epoch=self.service.service_epoch,
                        job_reference=reference,
                    )
                self.assertEqual(captured.exception.code, "job_not_found")
                self.assertEqual(self.selected_tokens.purposes, [])

    def test_every_nonselected_state_is_ineligible_without_record_minting(
        self,
    ) -> None:
        cases = (
            ForgeJobState.ACCEPTED,
            ForgeJobState.QUEUED,
            ForgeJobState.BUILDING,
            ForgeJobState.GENERATED,
            ForgeJobState.VALIDATING,
            ForgeJobState.FAILED,
            ForgeJobState.CANCELLED,
            ForgeJobState.DEADLINE_EXCEEDED,
        )
        paths = {
            ForgeJobState.ACCEPTED: (),
            ForgeJobState.QUEUED: (ForgeJobState.QUEUED,),
            ForgeJobState.BUILDING: (
                ForgeJobState.QUEUED,
                ForgeJobState.BUILDING,
            ),
            ForgeJobState.GENERATED: (
                ForgeJobState.QUEUED,
                ForgeJobState.BUILDING,
                ForgeJobState.GENERATED,
            ),
            ForgeJobState.VALIDATING: (
                ForgeJobState.QUEUED,
                ForgeJobState.BUILDING,
                ForgeJobState.GENERATED,
                ForgeJobState.VALIDATING,
            ),
            ForgeJobState.FAILED: (ForgeJobState.FAILED,),
            ForgeJobState.CANCELLED: (ForgeJobState.CANCELLED,),
            ForgeJobState.DEADLINE_EXCEEDED: (
                ForgeJobState.QUEUED,
                ForgeJobState.BUILDING,
                ForgeJobState.DEADLINE_EXCEEDED,
            ),
        }
        for target in cases:
            with self.subTest(state=target.value):
                self.clock = MutableClock()
                self.tokens = SequenceTokens()
                self.selected_tokens = SequenceTokens()
                self.service = self._service(source=_live_source())
                accepted = self._submit(f"nonselected-{target.value}")
                reference = str(accepted["reference"])
                if target is ForgeJobState.DEADLINE_EXCEEDED:
                    for state in paths[target][:-1]:
                        self.service.preview_transition(
                            reference=reference, state=state
                        )
                    self.clock.value = datetime.fromisoformat(
                        str(accepted["deadline_at"])
                    )
                    self.service.preview_transition(
                        reference=reference, state=paths[target][-1]
                    )
                else:
                    for state in paths[target]:
                        self.service.preview_transition(
                            reference=reference, state=state
                        )
                with self.assertRaises(ForgeServiceError) as captured:
                    self.service.resolve_selected_record(
                        actor_authority=self.authority,
                        expected_epoch=self.service.service_epoch,
                        job_reference=reference,
                    )
                self.assertEqual(captured.exception.code, "job_not_found")
                self.assertEqual(self.selected_tokens.purposes, [])

    def test_selected_record_fails_closed_on_expiry_actor_and_epoch(self) -> None:
        self.service = self._service(
            policy=ForgePolicy(selected_handoff_ttl_seconds=10),
            source=_live_source(),
        )
        accepted, _projection = self._select_live("expiring-record")
        reference = str(accepted["reference"])
        epoch = self.service.service_epoch

        with self.assertRaises(ForgeServiceError) as stale:
            self.service.resolve_selected_record(
                actor_authority=self.authority,
                expected_epoch="stale-forge-epoch",
                job_reference=reference,
            )
        self.assertEqual(stale.exception.code, "service_epoch_changed")

        wrong_actor_service = WorldForgeService(
            actors=(
                ForgeActorSeed(
                    authority=self.authority,
                    actor_id="presenter-alpha",
                    expires_at=(self.clock.value + timedelta(hours=1)).isoformat(),
                ),
                ForgeActorSeed(
                    authority="forge-other-presenter-authority",
                    actor_id="presenter-bravo",
                    expires_at=(self.clock.value + timedelta(hours=1)).isoformat(),
                ),
            ),
            policy=ForgePolicy(selected_handoff_ttl_seconds=10),
            clock=self.clock,
            token_factory=self.tokens,
            selected_record_token_factory=self.selected_tokens,
            source=_live_source(),
        )
        prepared = wrong_actor_service.prepare(
            actor_authority=self.authority,
            expected_epoch=wrong_actor_service.service_epoch,
            recipe_id=BSIDES_RECIPE_ID,
            recipe_revision=BSIDES_RECIPE_REVISION,
            answers=_answers(),
        )
        other_job = wrong_actor_service.confirm_and_submit(
            actor_authority=self.authority,
            expected_epoch=wrong_actor_service.service_epoch,
            idempotency_key="wrong-actor-record",
            preparation_reference=str(prepared["preparation_reference"]),
        )
        other_reference = str(other_job["reference"])
        for state in (
            ForgeJobState.QUEUED,
            ForgeJobState.BUILDING,
            ForgeJobState.GENERATED,
            ForgeJobState.VALIDATING,
        ):
            wrong_actor_service.preview_transition(
                reference=other_reference, state=state
            )
        wrong_actor_service.complete_selected(
            reference=other_reference, receipt=_live_receipt()
        )
        with self.assertRaises(ForgeServiceError) as wrong_actor:
            wrong_actor_service.resolve_selected_record(
                actor_authority="forge-other-presenter-authority",
                expected_epoch=wrong_actor_service.service_epoch,
                job_reference=other_reference,
            )
        self.assertEqual(wrong_actor.exception.code, "job_not_found")

        self.clock.value += timedelta(seconds=10)
        with self.assertRaises(ForgeServiceError) as expired:
            self.service.resolve_selected_record(
                actor_authority=self.authority,
                expected_epoch=epoch,
                job_reference=reference,
            )
        self.assertEqual(expired.exception.code, "job_not_found")

        new_epoch = self.service.preview_rotate_epoch()
        with self.assertRaises(ForgeServiceError) as lost:
            self.service.resolve_selected_record(
                actor_authority=self.authority,
                expected_epoch=new_epoch,
                job_reference=reference,
            )
        self.assertEqual(lost.exception.code, "job_not_found")

    def test_actor_expiry_denies_selected_record_before_handoff_expiry(self) -> None:
        service = WorldForgeService(
            actors=(
                ForgeActorSeed(
                    authority=self.authority,
                    actor_id="presenter-alpha",
                    expires_at=(self.clock.value + timedelta(seconds=5)).isoformat(),
                ),
            ),
            policy=ForgePolicy(selected_handoff_ttl_seconds=30),
            clock=self.clock,
            token_factory=self.tokens,
            selected_record_token_factory=self.selected_tokens,
            source=_live_source(),
        )
        self.service = service
        accepted, _projection = self._select_live("actor-expiry")
        self.clock.value += timedelta(seconds=5)
        with self.assertRaises(ForgeServiceError) as captured:
            service.resolve_selected_record(
                actor_authority=self.authority,
                expected_epoch=service.service_epoch,
                job_reference=str(accepted["reference"]),
            )
        self.assertEqual(captured.exception.code, "authority_denied")

    def test_malformed_live_receipt_cannot_select_or_mint_a_record(self) -> None:
        self.service = self._service(source=_live_source())
        accepted = self._submit("malformed-receipt")
        reference = str(accepted["reference"])
        self._advance_to_validating(reference)
        malformed_receipts = []
        failed_validation = _live_receipt()
        failed_validation["validation"] = {"state": "failed"}
        malformed_receipts.append(failed_validation)
        missing_revision = _live_receipt()
        del missing_revision["revision"]
        malformed_receipts.append(missing_revision)
        invalid_digest = _live_receipt()
        invalid_digest["revision"]["immutable_digest"] = "sha256:not-a-digest"
        malformed_receipts.append(invalid_digest)

        for receipt in malformed_receipts:
            with self.subTest(receipt=receipt):
                with self.assertRaises(ForgeServiceError) as captured:
                    self.service.complete_selected(reference=reference, receipt=receipt)
                self.assertEqual(captured.exception.code, "transition_invalid")
                projection = self.service.status(
                    actor_authority=self.authority,
                    expected_epoch=self.service.service_epoch,
                    reference=reference,
                )
                self.assertEqual(projection["lifecycle"]["state"], "validating")
                self.assertIsNone(projection["receipt"])
                self.assertEqual(self.selected_tokens.purposes, [])

        selected = self.service.complete_selected(
            reference=reference, receipt=_live_receipt()
        )
        self.assertEqual(selected["lifecycle"]["state"], "selected")

    def test_deadline_and_cancellation_fence_live_record_commit(self) -> None:
        self.service = self._service(source=_live_source())
        accepted = self._submit("late-live-record")
        reference = str(accepted["reference"])
        self._advance_to_validating(reference)
        self.clock.value = datetime.fromisoformat(str(accepted["deadline_at"]))
        projection = self.service.complete_selected(
            reference=reference, receipt=_live_receipt()
        )
        self.assertEqual(projection["lifecycle"]["state"], "deadline_exceeded")
        self.assertEqual(self.selected_tokens.purposes, [])
        with self.assertRaises(ForgeServiceError) as late:
            self.service.resolve_selected_record(
                actor_authority=self.authority,
                expected_epoch=self.service.service_epoch,
                job_reference=reference,
            )
        self.assertEqual(late.exception.code, "job_not_found")

        self.clock = MutableClock()
        self.tokens = SequenceTokens()
        self.selected_tokens = SequenceTokens()
        self.service = self._service(source=_live_source())
        accepted = self._submit("cancelled-live-record")
        reference = str(accepted["reference"])
        self._advance_to_validating(reference)
        self.service.cancel(
            actor_authority=self.authority,
            expected_epoch=self.service.service_epoch,
            reference=reference,
            idempotency_key="cancel-live-record",
        )
        with self.assertRaises(ForgeServiceError) as completion:
            self.service.complete_selected(reference=reference, receipt=_live_receipt())
        self.assertEqual(completion.exception.code, "transition_invalid")
        self.assertEqual(self.selected_tokens.purposes, [])
        with self.assertRaises(ForgeServiceError) as cancelled:
            self.service.resolve_selected_record(
                actor_authority=self.authority,
                expected_epoch=self.service.service_epoch,
                job_reference=reference,
            )
        self.assertEqual(cancelled.exception.code, "job_not_found")

    def test_selected_handoff_policy_defaults_and_bounds(self) -> None:
        defaults = ForgePolicy()
        self.assertEqual(
            defaults.selected_handoff_policy_id, "forge-to-stage-handoff:v1"
        )
        self.assertEqual(defaults.selected_handoff_ttl_seconds, 300)
        for ttl in (True, 1.5, "300", 0, 3_601):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ValueError):
                    ForgePolicy(selected_handoff_ttl_seconds=ttl)
        for policy_id in (
            "",
            "x" * 129,
            "unsafe\npolicy",
            "unsafe policy",
            "policý:v1",
        ):
            with self.subTest(policy_id=policy_id):
                with self.assertRaises(ValueError):
                    ForgePolicy(selected_handoff_policy_id=policy_id)

    def test_epoch_and_actor_expiry_fail_closed(self) -> None:
        with self.assertRaises(ForgeServiceError) as stale:
            self.service.confirm_and_submit(
                actor_authority=self.authority,
                expected_epoch="old-epoch",
                idempotency_key="stale",
                preparation_reference="forge-preparation:stale",
            )
        self.assertEqual(stale.exception.code, "service_epoch_changed")
        self.assertEqual(stale.exception.retry, "none")

        self.clock.value += timedelta(hours=2)
        with self.assertRaises(ForgeServiceError) as expired:
            self._submit("expired")
        self.assertEqual(expired.exception.code, "authority_denied")


if __name__ == "__main__":
    unittest.main()
