from __future__ import annotations

import unittest

from dreamland.models import (
    CommandContext,
    CommandOutputChunk,
    CommandResult,
    CommandStateUpdate,
    CommandStreamResult,
)
from dreamland.product.execution import (
    ProductExecutionOutcome,
    ProductExecutionRequest,
    ProductStateEffectKind,
)
from dreamland.product.live_agent import (
    ExactThreadCheckpointReaper,
    ProductLiveAgentAdapter,
    ReservedMaximumUsageMeter,
)
from dreamland.product.runtime_executor import (
    DreamlandStateProfileCodec,
    ProductRuntimeCommandExecutor,
    ProductRuntimeControl,
    ProductRuntimeLimits,
)
from dreamland.state_profiles import StateProfileStore


class StreamingAgent:
    def __init__(self, events) -> None:
        self.events = events
        self.controls = []

    def stream_respond(self, context, control=None):
        self.controls.append(control)
        yield from self.events(context, control)


class ProductLiveAgentTests(unittest.TestCase):
    def test_streams_one_claimed_attempt_and_returns_terminal(self) -> None:
        def events(context, control):
            self.assertEqual(context.command, "ls -la")
            self.assertIsNotNone(control)
            yield CommandOutputChunk("hello")
            yield CommandOutputChunk(" warning", "stderr")
            yield CommandStreamResult(
                CommandResult(stdout="hello", stderr=" warning", exit_status=7)
            )

        agent = StreamingAgent(events)
        adapter = ProductLiveAgentAdapter(agent)
        chunks = []
        control = _control()

        result = adapter.execute(_context(), control, chunks.append)

        self.assertEqual(result.exit_status, 7)
        self.assertEqual(control.attempts, 1)
        self.assertEqual(
            [(chunk.stream, chunk.text) for chunk in chunks],
            [("stdout", "hello"), ("stderr", " warning")],
        )

    def test_product_cancellation_reaches_agent_control(self) -> None:
        observed = []

        def events(_context, agent_control):
            assert agent_control is not None
            observed.append(agent_control)
            yield CommandStreamResult(CommandResult())

        source_cancelled = False
        control = _control(cancelled=lambda: source_cancelled)
        agent = StreamingAgent(events)
        adapter = ProductLiveAgentAdapter(agent)
        adapter.execute(_context(), control, lambda _chunk: None)
        source_cancelled = True
        control.refresh()

        self.assertTrue(observed[0].cancelled())

    def test_coalesces_provider_pieces_by_line_and_unterminated_byte_bound(
        self,
    ) -> None:
        text = "first line\nsecond line\n" + ("é" * 5)
        pieces = (
            "first ",
            "line\nsec",
            "ond line\n",
            "é",
            "éé",
            "éé",
        )

        chunks = _stream_chunks(pieces, complete=text, publication_chunk_bytes=5)

        self.assertEqual(
            [(chunk.stream, chunk.text) for chunk in chunks],
            [
                ("stdout", "first"),
                ("stdout", " line"),
                ("stdout", "\n"),
                ("stdout", "secon"),
                ("stdout", "d lin"),
                ("stdout", "e\n"),
                ("stdout", "éé"),
                ("stdout", "éé"),
                ("stdout", "é"),
            ],
        )
        self.assertEqual("".join(chunk.text for chunk in chunks), text)
        self.assertTrue(all(len(chunk.text.encode("utf-8")) <= 5 for chunk in chunks))

    def test_logical_output_is_independent_of_provider_segmentation(self) -> None:
        text = "alpha\nbeta\nunterminated"
        segmentations = (
            (text,),
            tuple(text),
            ("al", "pha\nbe", "ta\nun", "term", "inated"),
        )

        publications = [
            [
                (chunk.stream, chunk.text)
                for chunk in _stream_chunks(parts, complete=text)
            ]
            for parts in segmentations
        ]

        self.assertEqual(publications[0], publications[1])
        self.assertEqual(publications[1], publications[2])
        self.assertEqual(
            publications[0],
            [
                ("stdout", "alpha\n"),
                ("stdout", "beta\n"),
                ("stdout", "unterminated"),
            ],
        )

        fragmented = _stream_chunks(("x",) * 300, complete="x" * 300)
        self.assertEqual([chunk.text for chunk in fragmented], ["x" * 300])

    def test_stream_switch_flushes_without_reordering(self) -> None:
        def events(_context, _control):
            yield CommandOutputChunk("out")
            yield CommandOutputChunk("-err\n", "stderr")
            yield CommandOutputChunk("-tail", "stdout")
            yield CommandStreamResult(CommandResult(stdout="out-tail", stderr="-err\n"))

        chunks = []
        ProductLiveAgentAdapter(StreamingAgent(events)).execute(
            _context(),
            _control(),
            chunks.append,
        )

        self.assertEqual(
            [(chunk.stream, chunk.text) for chunk in chunks],
            [
                ("stdout", "out"),
                ("stderr", "-err\n"),
                ("stdout", "-tail"),
            ],
        )

    def test_cancellation_fence_rejects_buffered_late_output(self) -> None:
        cancelled = False

        def events(_context, _control):
            nonlocal cancelled
            yield CommandOutputChunk("buffered without newline")
            cancelled = True
            yield CommandStreamResult(
                CommandResult(stdout="buffered without newline", interrupted=True)
            )

        executor = ProductRuntimeCommandExecutor(
            agent=ProductLiveAgentAdapter(StreamingAgent(events)),
            state_codec=DreamlandStateProfileCodec(),
            checkpoint_reaper=ExactThreadCheckpointReaper(lambda _value: None),
            usage_meter=ReservedMaximumUsageMeter(),
            clock_ms=lambda: 100,
        )
        profile = StateProfileStore().load("default")
        chunks = []
        result = executor.execute(
            ProductExecutionRequest(
                service_epoch="epoch:live_test_00000001",
                session_id="session:live_test_00000001",
                command_id="command:live_test_00000001",
                slot_generation=1,
                canonical_command="slow output",
                provisional_document=profile.to_document(),
                checkpoint_namespace="checkpoint:live_test_00000001",
                deadline_ms=10_000,
                max_tokens=1_024,
                max_spend_units=100,
                max_attempts=1,
                cancelled=lambda: cancelled,
            ),
            chunks.append,
        )

        self.assertEqual(result.outcome, ProductExecutionOutcome.INTERRUPTED)
        self.assertEqual(chunks, [])

    def test_rejects_invalid_or_ambiguous_streams(self) -> None:
        cases = (
            lambda _context, _control: iter(()),
            lambda _context, _control: iter(
                (CommandStreamResult(CommandResult()), CommandOutputChunk("late"))
            ),
            lambda _context, _control: iter(
                (
                    CommandStreamResult(CommandResult()),
                    CommandStreamResult(CommandResult()),
                )
            ),
            lambda _context, _control: iter((object(),)),
        )
        for events in cases:
            with self.subTest(events=events):
                with self.assertRaises((RuntimeError, TypeError, ValueError)):
                    ProductLiveAgentAdapter(StreamingAgent(events)).execute(
                        _context(),
                        _control(),
                        lambda _chunk: None,
                    )

    def test_full_reservation_meter_is_explicit_and_bounded(self) -> None:
        meter = ReservedMaximumUsageMeter()
        limits = ProductRuntimeLimits(
            max_tokens=1_234,
            max_spend_units=57,
            max_attempts=1,
        )
        measurement = meter.begin(
            command_id="command:live_0001",
            checkpoint_namespace="checkpoint:live_0001",
            limits=limits,
        )

        self.assertEqual(meter.finish(measurement).tokens, 1_234)
        self.assertEqual(meter.finish(measurement).spend_units, 57)

    def test_exact_checkpoint_reaper_deletes_only_requested_thread(self) -> None:
        deleted = []
        reaper = ExactThreadCheckpointReaper(deleted.append)

        reaper.reap("product:session:command")

        self.assertEqual(deleted, ["product:session:command"])

    def test_adapter_commits_state_through_product_runtime_executor(self) -> None:
        def events(_context, _control):
            yield CommandStreamResult(
                CommandResult(state_update=CommandStateUpdate(cwd="/opt/live-demo"))
            )

        deleted = []
        executor = ProductRuntimeCommandExecutor(
            agent=ProductLiveAgentAdapter(StreamingAgent(events)),
            state_codec=DreamlandStateProfileCodec(),
            checkpoint_reaper=ExactThreadCheckpointReaper(deleted.append),
            usage_meter=ReservedMaximumUsageMeter(),
            clock_ms=lambda: 100,
        )
        profile = StateProfileStore().load("default")
        result = executor.execute(
            ProductExecutionRequest(
                service_epoch="epoch:live_test_00000001",
                session_id="session:live_test_00000001",
                command_id="command:live_test_00000001",
                slot_generation=1,
                canonical_command="cd /opt/live-demo",
                provisional_document=profile.to_document(),
                checkpoint_namespace="checkpoint:live_test_00000001",
                deadline_ms=10_000,
                max_tokens=1_024,
                max_spend_units=100,
                max_attempts=1,
                cancelled=lambda: False,
            ),
            lambda _chunk: None,
        )

        self.assertEqual(result.state_effect.kind, ProductStateEffectKind.COMMITTED)
        self.assertEqual(result.usage_tokens, 1_024)
        self.assertEqual(result.usage_spend_units, 100)
        self.assertEqual(deleted, ["checkpoint:live_test_00000001"])


def _control(*, cancelled=lambda: False) -> ProductRuntimeControl:
    return ProductRuntimeControl(
        cancelled=cancelled,
        deadline_ms=10_000,
        clock_ms=lambda: 100,
        poll_interval_ms=10,
        limits=ProductRuntimeLimits(
            max_tokens=1_024,
            max_spend_units=100,
            max_attempts=1,
        ),
    )


def _stream_chunks(
    pieces,
    *,
    complete: str,
    publication_chunk_bytes: int = 4 * 1024,
):
    def events(_context, _control):
        for piece in pieces:
            yield CommandOutputChunk(piece)
        yield CommandStreamResult(CommandResult(stdout=complete))

    chunks = []
    ProductLiveAgentAdapter(
        StreamingAgent(events),
        publication_chunk_bytes=publication_chunk_bytes,
    ).execute(_context(), _control(), chunks.append)
    return chunks


def _context() -> CommandContext:
    return CommandContext(
        username="presenter",
        cwd="/home/presenter",
        hostname="stage-host",
        command="ls -la",
        langfuse_trace_id="checkpoint:live_0001",
        command_id="command:live_0001",
        force_session_state=True,
    )


if __name__ == "__main__":
    unittest.main()
