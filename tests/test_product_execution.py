from __future__ import annotations

import unittest

from dreamland.product.execution import (
    ProductExecutionOutcome,
    ProductExecutionRequest,
    ProductExecutionResult,
    ProductGuestTermination,
    ProductGuestTerminationKind,
    ProductOutputChunk,
    ProductOutputStream,
    ProductStateEffect,
    ProductStateEffectKind,
)


class ProductExecutionValueTests(unittest.TestCase):
    def test_completed_exited_distinguishes_guest_and_state_effect(self) -> None:
        result = ProductExecutionResult.completed_exited(
            exit_status=7,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document={"world_state": {"value": 2}},
            ),
            usage_tokens=17,
            usage_spend_units=3,
        )

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertEqual(result.guest_termination.exit_status, 7)
        self.assertEqual(result.state_effect.kind, ProductStateEffectKind.COMMITTED)
        self.assertEqual(result.usage_tokens, 17)
        self.assertEqual(result.usage_spend_units, 3)

    def test_completed_signaled_is_not_product_interruption(self) -> None:
        result = ProductExecutionResult.completed_signaled(signal="SIGTERM")

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertEqual(
            result.guest_termination.kind,
            ProductGuestTerminationKind.SIGNALED,
        )
        self.assertIsNone(result.guest_termination.exit_status)

    def test_interrupted_and_failed_omit_guest_and_state_outcomes(self) -> None:
        interrupted = ProductExecutionResult.interrupted("cancelled")
        failed = ProductExecutionResult.failed("provider_unavailable")

        self.assertIsNone(interrupted.guest_termination)
        self.assertIsNone(interrupted.state_effect)
        self.assertIsNone(failed.guest_termination)
        self.assertIsNone(failed.state_effect)

    def test_illegal_result_shapes_are_rejected(self) -> None:
        cases = (
            lambda: ProductExecutionResult(ProductExecutionOutcome.COMPLETED),
            lambda: ProductExecutionResult(
                ProductExecutionOutcome.INTERRUPTED,
                guest_termination=ProductGuestTermination(
                    ProductGuestTerminationKind.EXITED,
                    exit_status=0,
                ),
                reason="cancelled",
            ),
            lambda: ProductExecutionResult(ProductExecutionOutcome.FAILED),
            lambda: ProductGuestTermination(
                ProductGuestTerminationKind.EXITED,
                exit_status=0,
                signal="SIGTERM",
            ),
            lambda: ProductGuestTermination(
                ProductGuestTerminationKind.SIGNALED,
                signal="term",
            ),
            lambda: ProductStateEffect(
                ProductStateEffectKind.NONE,
                provisional_document={},
            ),
            lambda: ProductStateEffect(ProductStateEffectKind.COMMITTED),
            lambda: ProductExecutionResult.failed(
                "provider_unavailable",
                usage_tokens=-1,
            ),
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    case()

    def test_execution_request_requires_isolated_identity_and_state(self) -> None:
        request = ProductExecutionRequest(
            service_epoch="epoch",
            session_id="session",
            command_id="command",
            slot_generation=1,
            canonical_command="uname -a",
            provisional_document={"world_state": {}},
            checkpoint_namespace="product/session/1",
            deadline_ms=1_000,
            max_tokens=100,
            max_spend_units=50,
            max_attempts=1,
            cancelled=lambda: False,
        )

        self.assertFalse(request.cancelled())
        self.assertEqual(request.slot_generation, 1)
        self.assertEqual(request.max_tokens, 100)
        self.assertEqual(request.max_spend_units, 50)
        self.assertEqual(request.max_attempts, 1)

    def test_output_chunk_is_typed_but_empty_final_output_is_legal(self) -> None:
        self.assertEqual(
            ProductOutputChunk(ProductOutputStream.STDOUT, "").text,
            "",
        )
        with self.assertRaises(ValueError):
            ProductOutputChunk("stdout", "text")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
