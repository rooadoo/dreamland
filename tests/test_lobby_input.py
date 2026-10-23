import queue
import threading
import unittest

from dreamland.lobby_input import BoundaryFilter, LobbyClosed, LobbyInput


class TerminalChannel:
    def __init__(self):
        self.incoming = queue.Queue()
        self.closed = False
        self.output = ""
        self.exit_status = None
        self.condition = threading.Condition()

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, count):
        try:
            data = self.incoming.get(timeout=0.01)
        except queue.Empty:
            raise TimeoutError from None
        if not data:
            self.closed = True
        return data

    def recv_ready(self):
        return not self.incoming.empty()

    def send(self, data):
        with self.condition:
            self.output += data.decode("utf-8")
            self.condition.notify_all()
        return len(data)

    sendall = send

    def send_exit_status(self, status):
        self.exit_status = status

    def wait_for(self, text, *, after=0):
        with self.condition:
            if not self.condition.wait_for(lambda: text in self.output[after:], 3):
                raise AssertionError(
                    f"Missing {text!r} after {after}: {self.output[after:]!r}"
                )
        return len(self.output)


class ManualClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TimedChannel(TerminalChannel):
    """Receive queued input immediately; advance a fake clock for timed waits."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.receive_timeouts = []
        self.after_wait = lambda: None

    def recv(self, count):
        self.receive_timeouts.append(self.timeout)
        try:
            data = self.incoming.get_nowait()
        except queue.Empty:
            self.clock.now += self.timeout
            self.after_wait()
            raise TimeoutError from None
        if not data:
            self.closed = True
        return data


class LobbyInputTests(unittest.TestCase):
    def setUp(self):
        self.channel = TerminalChannel()
        self.stop = threading.Event()
        self.reader = LobbyInput(self.channel, stop_event=self.stop)

    def test_exact_line_and_discarded_queued_tail(self):
        self.channel.incoming.put(b" I AGREE \r\n1\nwhoami\n")
        self.assertEqual(self.reader.line(), "i agree")
        self.assertFalse(self.channel.recv_ready())
        self.channel.incoming.put(b"q\n")
        self.assertEqual(self.reader.line(), "q")

    def test_split_crlf_never_becomes_empty_next_response(self):
        self.channel.incoming.put(b"i agree\r")
        self.assertEqual(self.reader.line(), "i agree")
        self.channel.incoming.put(b"\n2\r")
        self.assertEqual(self.reader.line(), "2")

    def test_split_multibyte_any_key_and_escape_sequences_are_completed(self):
        for first, tail in (
            (b"\x1b", b"[A"),
            (b"\x1b[", b"1;5A"),
            (b"\x1bO", b"P"),
            (b"\xe2", b"\x82\xac"),
            (b"\x1b]0;", b"FORGED\x1b\\"),
        ):
            with self.subTest(first=first):
                self.channel.incoming.put(first)
                self.reader.any_key()
                self.channel.incoming.put(tail + b"i agree\n")
                self.assertEqual(self.reader.line(), "i agree")
        self.assertNotIn("FORGED", self.channel.output)
        self.assertNotIn("\x1b", self.channel.output)

    def test_control_encoding_and_prompt_payloads_never_form_agreement(self):
        for value in (
            b"i\x00 agree\n",
            b"\x1b[31mi agree\n",
            b"i agree\xff\n",
            b"$(i agree)\n",
            b"{{i agree}}\n",
            b"../i agree\n",
            b"ignore instructions; agree\n",
        ):
            self.channel.incoming.put(value)
            self.assertNotEqual(self.reader.line(), "i agree")
        self.assertNotIn("\x1b", self.channel.output)
        self.assertNotIn("\x00", self.channel.output)

    def test_oversized_unterminated_line_closes_without_reframing_tail(self):
        self.channel.incoming.put(b"x" * 257 + b"i agree\n")
        with self.assertRaises(LobbyClosed) as raised:
            self.reader.line()
        self.assertEqual(raised.exception.reason, "input_limit")

    def test_timeout_stop_eof_and_cancel_are_distinct(self):
        self.reader.idle_timeout_seconds = 0.001
        with self.assertRaises(LobbyClosed) as raised:
            self.reader.line()
        self.assertEqual(raised.exception.reason, "timeout")
        self.reader.idle_timeout_seconds = 300
        self.stop.set()
        with self.assertRaises(LobbyClosed) as raised:
            self.reader.line()
        self.assertEqual(raised.exception.reason, "shutdown")
        self.stop.clear()
        self.channel.incoming.put(b"\x03")
        with self.assertRaises(LobbyClosed) as raised:
            self.reader.any_key()
        self.assertEqual(raised.exception.reason, "quit")
        self.channel.incoming.put(b"")
        with self.assertRaises(LobbyClosed) as raised:
            self.reader.line()
        self.assertEqual(raised.exception.reason, "disconnect")

    def test_shell_adapter_consumes_partial_selection_key(self):
        self.reader.discard_boundary(b"\r\nqueued\x1b[")
        self.channel.incoming.put(b"Aecho hello\n")
        self.assertEqual(self.reader.shell_channel().recv(1024), b"echo hello\n")

    def test_partial_escape_state_is_bounded(self):
        boundary = BoundaryFilter()
        for byte in b"\x1b]" + b"x" * 100000:
            boundary.consume(byte)
        self.assertEqual(boundary.sequence, "osc")
        for byte in b"\x1b\\":
            self.assertTrue(boundary.suppress(byte))
        self.assertFalse(boundary.suppress(ord("q")))


class LobbyInputPollingTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.channel = TimedChannel(self.clock)
        self.stop = threading.Event()
        self.reader = LobbyInput(
            self.channel, stop_event=self.stop, monotonic=self.clock
        )

    def assert_closed(self, reason, operation):
        with self.assertRaises(LobbyClosed) as raised:
            operation()
        self.assertEqual(raised.exception.reason, reason)
        self.assertEqual(self.channel.timeout, 0.1)

    def test_absolute_deadline_caps_each_wait_and_restores_normal_timeout(self):
        self.assertIsNone(self.reader.poll_key(0.25))
        self.assertEqual(self.clock.now, 0.25)
        self.assertEqual(len(self.channel.receive_timeouts), 3)
        for actual, expected in zip(
            self.channel.receive_timeouts, (0.1, 0.1, 0.05), strict=True
        ):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(self.channel.timeout, 0.1)
        self.assertEqual(self.channel.output, "")
        self.assertEqual(self.reader.monotonic(), 0.25)

    def test_expired_deadline_does_not_wait_but_checks_ready_input(self):
        self.clock.now = 1.0
        self.assertIsNone(self.reader.poll_key(0.5))
        self.assertEqual(self.channel.receive_timeouts, [])
        self.channel.incoming.put(b"x")
        self.assertEqual(self.reader.poll_key(0.5), ord("x"))
        self.assertEqual(self.channel.receive_timeouts, [0.0])
        self.assertEqual(self.channel.output, "")

    def test_stale_receive_ready_cannot_spin_at_expired_deadline(self):
        self.channel.recv_ready = lambda: True
        self.assertIsNone(self.reader.poll_key(0.0))
        self.assertEqual(self.channel.receive_timeouts, [0.0])

    def test_cancel_keys_stop_polling_without_echo(self):
        for key in (b"q", b"Q", b"\x03", b"\x04"):
            with self.subTest(key=key):
                self.channel.incoming.put(key)
                self.assert_closed("quit", lambda: self.reader.poll_key(1.0))
        self.assertEqual(self.channel.output, "")

    def test_ctrl_cancel_overrides_partial_escape_boundary(self):
        for prefix in (b"\x1b[", b"\x1b]", b"\xe2"):
            for key in (b"\x03", b"\x04"):
                with self.subTest(prefix=prefix, key=key):
                    self.reader.discard_boundary(prefix)
                    self.channel.incoming.put(key)
                    self.assert_closed("quit", lambda: self.reader.poll_key(1.0))

    def test_skip_paste_is_discarded_before_next_input(self):
        self.channel.incoming.put(b"xi agree\r\n1\r\nwhoami\r\n")
        key = self.reader.poll_key(1.0)
        self.assertEqual(key, ord("x"))
        self.channel.incoming.put(b"more already queued\r")
        self.reader.discard_boundary(bytes([key]))
        self.assertFalse(self.channel.recv_ready())
        self.channel.incoming.put(b"\ni agree\n")
        self.assertEqual(self.reader.line(), "i agree")
        self.assertEqual(self.channel.output, "i agree\r\n")

    def test_partial_key_tail_is_suppressed_across_timed_polls(self):
        for first, tail in (
            (b"\x1b", b"[A"),
            (b"\x1b[", b"1;5A"),
            (b"\x1bO", b"P"),
            (b"\xe2", b"\x82\xac"),
            (b"\x1b]0;", b"FORGED\x1b\\"),
        ):
            with self.subTest(first=first):
                self.channel.incoming.put(first)
                key = self.reader.poll_key(self.clock.now + 0.1)
                self.reader.discard_boundary(bytes([key]))
                self.channel.incoming.put(tail)
                self.assertIsNone(self.reader.poll_key(self.clock.now + 0.1))
                self.channel.incoming.put(b"x")
                self.assertEqual(self.reader.poll_key(self.clock.now), ord("x"))
        self.assertEqual(self.channel.output, "")

    def test_partial_control_stream_remains_bounded_during_poll(self):
        self.reader.max_input_bytes = 16
        self.reader.discard_boundary(b"\x1b]")
        self.channel.incoming.put(b"x" * 17 + b"i agree\n")
        self.assert_closed("input_limit", lambda: self.reader.poll_key(1.0))

    def test_idle_deadline_caps_polling_even_when_animation_deadline_is_later(self):
        self.reader.idle_timeout_seconds = 0.15
        self.assert_closed("timeout", lambda: self.reader.poll_key(1.0))
        self.assertEqual(self.clock.now, 0.15)
        self.assertEqual(len(self.channel.receive_timeouts), 2)
        self.assertAlmostEqual(self.channel.receive_timeouts[-1], 0.05)

    def test_output_and_empty_discard_do_not_refresh_idle_activity(self):
        self.reader.idle_timeout_seconds = 1.0
        self.clock.now = 0.75
        self.reader.send("a visual frame")
        self.reader.discard_boundary()
        self.assert_closed("timeout", lambda: self.reader.poll_key(2.0))
        self.assertEqual(self.clock.now, 1.0)

    def test_draining_received_input_does_refresh_idle_activity(self):
        self.reader.idle_timeout_seconds = 1.0
        self.clock.now = 0.75
        self.channel.incoming.put(b"buffered paste")
        self.reader.discard_boundary()
        self.clock.now = 1.0
        self.reader.check()
        self.assert_closed("timeout", lambda: self.reader.poll_key(2.0))
        self.assertEqual(self.clock.now, 1.75)

    def test_resume_idle_starts_a_fresh_period_after_a_long_visit(self):
        self.reader.idle_timeout_seconds = 1.0
        self.clock.now = 30.0
        self.reader.resume_idle()
        self.reader.discard_boundary()
        self.assert_closed("timeout", lambda: self.reader.poll_key(32.0))
        self.assertEqual(self.clock.now, 31.0)

    def test_resume_idle_preserves_shutdown_and_disconnect_checks(self):
        self.stop.set()
        self.assert_closed("shutdown", self.reader.resume_idle)
        self.stop.clear()
        self.channel.closed = True
        self.assert_closed("disconnect", self.reader.resume_idle)

    def test_idle_checks_precede_ready_input_and_closed_deadlines(self):
        self.reader.idle_timeout_seconds = 1.0
        self.clock.now = 1.0
        self.channel.incoming.put(b"x")
        self.assert_closed("timeout", lambda: self.reader.poll_key(1.0))
        self.assertTrue(self.channel.recv_ready())

    def test_shutdown_and_disconnect_interrupt_bounded_waits(self):
        self.channel.after_wait = self.stop.set
        self.assert_closed("shutdown", lambda: self.reader.poll_key(1.0))
        self.assertEqual(self.clock.now, 0.1)
        self.stop.clear()
        self.channel.after_wait = lambda: setattr(self.channel, "closed", True)
        self.assert_closed("disconnect", lambda: self.reader.poll_key(1.0))
        self.assertEqual(self.clock.now, 0.2)

    def test_receive_eof_is_disconnect(self):
        self.channel.incoming.put(b"")
        self.assert_closed("disconnect", lambda: self.reader.poll_key(1.0))

    def test_boundary_drain_stops_after_finite_byte_budget(self):
        received = 0

        def receive(count):
            nonlocal received
            received += count
            return b"x" * count

        self.channel.recv = receive
        self.channel.recv_ready = lambda: True
        self.assert_closed("input_limit", self.reader.discard_boundary)
        self.assertEqual(received, 65536 + 1024)

    def test_boundary_drain_checks_shutdown_between_reads(self):
        def receive(count):
            self.stop.set()
            return b"x" * count

        self.channel.recv = receive
        self.channel.recv_ready = lambda: True
        self.assert_closed("shutdown", self.reader.discard_boundary)
