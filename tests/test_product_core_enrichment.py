from __future__ import annotations

import unittest
from copy import deepcopy

from dreamland.product.enrichment import (
    ProductEnrichmentCoordinator,
    ProductEnrichmentGeneration,
    ProductEnrichmentOutcomeKind,
    ProductEnrichmentReason,
)
from dreamland.product.execution import (
    ProductExecutionResult,
    ProductStateEffect,
    ProductStateEffectKind,
)
from tests.test_product_core import (
    ALICE_ACTOR,
    FakeClock,
    ManualTasks,
    ScriptedExecutor,
    _create,
    _service,
    _submit,
)


class StaticEnrichmentGenerator:
    def __init__(
        self,
        replacement_document: dict[str, object],
        on_generate=None,
    ) -> None:
        self.replacement_document = replacement_document
        self.on_generate = on_generate or (lambda: None)
        self.calls = 0

    def generate(self, _request):
        self.calls += 1
        self.on_generate()
        return ProductEnrichmentGeneration(
            self.replacement_document,
            usage_tokens=7,
            usage_spend_units=3,
        )


class ProductCoreEnrichmentIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tasks = ManualTasks()
        self.executor = ScriptedExecutor()
        self.service = _service(
            clock=self.clock,
            tasks=self.tasks,
            executor=self.executor,
        )
        self.created = _create(self.service, key="enrichment-core-create")
        self.session_id = str(self.created["snapshot"]["session_id"])
        self.reference = str(self.created["snapshot"]["reference"])
        self.authority = str(self.created["session_authority"])

    def test_apply_advances_state_and_canonical_event_under_core_lock(self) -> None:
        replacement = self._replacement(2)
        observed = []
        coordinator = self._coordinator(replacement, observed.append)

        result = coordinator.run(
            proposal_id="proposal:core-apply",
            session_id=self.session_id,
        )

        self.assertEqual(result.kind, ProductEnrichmentOutcomeKind.APPLIED)
        self.assertEqual(result.accepted_state_revision, 1)
        self.assertEqual(len(observed), 1)
        snapshot = self._snapshot()
        self.assertEqual(snapshot["coordinates"]["accepted_state_revision"], 1)
        self.assertEqual(
            self.service._sessions_by_reference[self.reference].runtime_document[
                "world_state"
            ]["value"],
            2,
        )
        state_events = [
            event
            for event in self._events()["events"]
            if event["type"] == "state.accepted"
        ]
        self.assertEqual(len(state_events), 1)
        self.assertEqual(state_events[0]["data"]["source"], "enrichment")
        self.assertEqual(
            state_events[0]["data"]["accepted_state_revision"],
            1,
        )

    def test_foreground_command_defers_apply_until_slot_is_terminal(self) -> None:
        accepted = []

        def submit_during_generation() -> None:
            accepted.append(
                _submit(
                    self.service,
                    reference=self.reference,
                    session_authority=self.authority,
                    key="enrichment-foreground-command",
                )
            )

        coordinator = self._coordinator(
            self._replacement(3),
            on_generate=submit_during_generation,
        )

        deferred = coordinator.run(
            proposal_id="proposal:foreground-defer",
            session_id=self.session_id,
        )
        self.tasks.run_next()
        applied = coordinator.retry_apply("proposal:foreground-defer")

        self.assertEqual(deferred.kind, ProductEnrichmentOutcomeKind.DEFERRED)
        self.assertEqual(deferred.reason, ProductEnrichmentReason.FOREGROUND_ACTIVE)
        self.assertEqual(applied.kind, ProductEnrichmentOutcomeKind.APPLIED)
        self.assertEqual(
            self.service.command_result(
                actor_authority=ALICE_ACTOR,
                session_authority=self.authority,
                reference=self.reference,
                expected_epoch=self.service.service_epoch,
                command_id=str(accepted[0]["command_id"]),
            )["command"]["status"],
            "completed",
        )

    def test_command_state_commit_fences_stale_enrichment_proposal(self) -> None:
        command_document = self._replacement(4)
        self.executor.result = ProductExecutionResult.completed_exited(
            exit_status=0,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document=command_document,
            ),
        )
        coordinator = self._coordinator(
            self._replacement(5),
            on_generate=lambda: _submit(
                self.service,
                reference=self.reference,
                session_authority=self.authority,
                key="enrichment-racing-command",
            ),
        )
        deferred = coordinator.run(
            proposal_id="proposal:stale-after-command",
            session_id=self.session_id,
        )

        self.tasks.run_next()
        fenced = coordinator.retry_apply("proposal:stale-after-command")

        self.assertEqual(deferred.kind, ProductEnrichmentOutcomeKind.DEFERRED)
        self.assertEqual(fenced.kind, ProductEnrichmentOutcomeKind.FENCED)
        self.assertEqual(
            fenced.reason,
            ProductEnrichmentReason.STALE_ACCEPTED_REVISION,
        )
        self.assertEqual(
            self.service._sessions_by_reference[self.reference].runtime_document[
                "world_state"
            ]["value"],
            4,
        )

    def test_close_fences_deferred_proposal_before_apply(self) -> None:
        coordinator = self._coordinator(
            self._replacement(6),
            on_generate=lambda: _submit(
                self.service,
                reference=self.reference,
                session_authority=self.authority,
                key="enrichment-close-command",
            ),
        )
        deferred = coordinator.run(
            proposal_id="proposal:close-fence",
            session_id=self.session_id,
        )

        self.service.close(
            actor_authority=ALICE_ACTOR,
            session_authority=self.authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
            idempotency_key="enrichment-close",
            expected_control_revision=1,
        )
        fenced = coordinator.retry_apply("proposal:close-fence")

        self.assertEqual(deferred.kind, ProductEnrichmentOutcomeKind.DEFERRED)
        self.assertEqual(fenced.kind, ProductEnrichmentOutcomeKind.FENCED)
        self.assertEqual(fenced.reason, ProductEnrichmentReason.SESSION_UNAVAILABLE)

    def test_failed_core_commit_leaves_state_and_event_unchanged_for_retry(
        self,
    ) -> None:
        attempts = 0

        def fail_once(_document) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("synthetic core validation failure")

        self.service._validate_state_effect = fail_once
        coordinator = self._coordinator(self._replacement(7))
        before = self._snapshot()["coordinates"]

        with self.assertRaises(RuntimeError):
            coordinator.run(
                proposal_id="proposal:atomic-retry",
                session_id=self.session_id,
            )

        self.assertEqual(self._snapshot()["coordinates"], before)
        self.assertFalse(
            any(event["type"] == "state.accepted" for event in self._events()["events"])
        )
        applied = coordinator.retry_apply("proposal:atomic-retry")
        self.assertEqual(applied.kind, ProductEnrichmentOutcomeKind.APPLIED)
        self.assertEqual(applied.accepted_state_revision, 1)

    def _coordinator(self, replacement, on_applied=None, on_generate=None):
        return ProductEnrichmentCoordinator(
            aggregate=self.service,
            generator=StaticEnrichmentGenerator(replacement, on_generate),
            admission=self.service.admission,
            validate_replacement=lambda _document: None,
            on_applied=on_applied or (lambda _event: None),
            clock_ms=self.clock,
        )

    def _replacement(self, value: int) -> dict[str, object]:
        document = deepcopy(
            self.service._sessions_by_reference[self.reference].runtime_document
        )
        document["world_state"]["value"] = value
        return document

    def _snapshot(self) -> dict[str, object]:
        return self.service.snapshot(
            actor_authority=ALICE_ACTOR,
            session_authority=self.authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
        )["snapshot"]

    def _events(self) -> dict[str, object]:
        return self.service.events(
            actor_authority=ALICE_ACTOR,
            session_authority=self.authority,
            reference=self.reference,
            expected_epoch=self.service.service_epoch,
        )


if __name__ == "__main__":
    unittest.main()
