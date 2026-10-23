import re
import threading
import unittest
from unittest.mock import patch

from dreamland.lobby_input import LobbyClosed, LobbyInput
from dreamland.lobby_intro import (
    INTRO_DURATION,
    INTRO_FRAME_COUNT,
    INTRO_FRAMES,
    INTRO_MAX_BYTES,
    INTRO_MIN_COLUMNS,
    INTRO_MIN_ROWS,
    run_intro,
)
from dreamland.lobby_intro_art import INTRO_ART, INTRO_ART_HEIGHT, INTRO_ART_WIDTH
from dreamland.lobby_presentation import CLEAR, RESET, LobbyPresentation


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now


class TimedChannel:
    """A synchronous channel whose waits/writes advance a controlled clock."""

    def __init__(self, clock):
        self.clock = clock
        self.closed = False
        self.timeout = 0.1
        self.writes = []
        self.events = []
        self.incoming = []
        self.write_delay = 0.0
        self.after_write = lambda: None

    def settimeout(self, timeout):
        self.timeout = timeout

    def due(self):
        while self.events and self.events[0][0] <= self.clock():
            _, action = self.events.pop(0)
            action()

    def recv_ready(self):
        self.due()
        return bool(self.incoming)

    def recv(self, count):
        self.due()
        if not self.incoming:
            deadline = self.clock() + self.timeout
            self.clock.now = (
                min(deadline, self.events[0][0]) if self.events else deadline
            )
            self.due()
        if self.incoming:
            return self.incoming.pop(0)
        raise TimeoutError

    def sendall(self, data):
        self.writes.append((self.clock(), data))
        self.clock.now += self.write_delay
        self.after_write()


class Screen:
    def __init__(self, width, height, fill=" "):
        self.width, self.height = width, height
        self.cells = [[fill] * width for _ in range(height)]
        self.colors = [[0] * width for _ in range(height)]
        self.row = self.column = self.color = 0

    def feed(self, text):
        while text:
            if text.startswith("\x1b["):
                match = re.match(r"\x1b\[([0-9;]*)([A-Za-z])", text)
                if match is None:
                    raise AssertionError("malformed terminal control")
                parameters, command = match.groups()
                if command == "m" and parameters in {"0", "32", "35", "36"}:
                    self.color = int(parameters)
                elif command == "J" and parameters == "2":
                    self.cells = [[" "] * self.width for _ in range(self.height)]
                    self.colors = [[0] * self.width for _ in range(self.height)]
                elif command == "H":
                    if parameters == "":
                        self.row = self.column = 0
                    else:
                        row, column = map(int, parameters.split(";"))
                        if not (1 <= row <= self.height and 1 <= column <= self.width):
                            raise AssertionError("cursor outside viewport")
                        self.row, self.column = row - 1, column - 1
                else:
                    raise AssertionError("unapproved terminal control")
                text = text[match.end() :]
            else:
                if not (32 <= ord(text[0]) <= 126 and self.column < self.width - 1):
                    raise AssertionError("nonprinting byte or terminal wrapping")
                self.cells[self.row][self.column] = text[0]
                self.colors[self.row][self.column] = self.color
                self.column += 1
                text = text[1:]


class LobbyIntroTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.channel = TimedChannel(self.clock)
        self.stop = threading.Event()
        self.reader = LobbyInput(
            self.channel, stop_event=self.stop, monotonic=self.clock
        )
        self.size = (80, 24)

    def run_intro(self, **kwargs):
        options = dict(
            mode="auto",
            terminal_type="xterm-256color",
            presentation=LobbyPresentation(True, True),
            dimensions=lambda: self.size,
        )
        options.update(kwargs)
        return run_intro(self.reader, **options)

    def event(self, delay, action):
        self.channel.events.append((self.clock() + delay, action))
        self.channel.events.sort(key=lambda item: item[0])

    def output(self):
        return b"".join(data for _, data in self.channel.writes).decode("ascii")

    def test_twenty_frames_end_at_two_seconds_with_hard_byte_and_control_bounds(self):
        started = self.clock()
        result = self.run_intro()
        self.assertEqual(result.reason, "complete")
        self.assertEqual(result.frames, 20)
        self.assertAlmostEqual(self.clock() - started, 2.0)
        self.assertEqual(result.bytes_sent, len(self.output().encode("ascii")))
        self.assertLessEqual(result.bytes_sent, INTRO_MAX_BYTES)
        for index, (when, data) in enumerate(self.channel.writes[:-1]):
            self.assertAlmostEqual(when - started, index * 0.1)
            self.assertEqual(
                len(re.findall(rb"\x1b\[[1-9][0-9]*;[1-9][0-9]*H", data)), 9
            )
            self.assertLess(when - started, INTRO_DURATION)
        self.assertTrue(self.channel.writes[0][1].startswith((RESET + CLEAR).encode()))
        self.assertEqual(self.channel.writes[-1][1], (RESET + CLEAR).encode())
        self.assertEqual(self.output().count(CLEAR), 2)
        self.assertNotIn("\n", self.output())
        self.assertNotIn("\r", self.output())
        visible = re.sub(
            r"\x1b\[(?:[1-9][0-9]*;[1-9][0-9]*H|2J|H|36m|35m|32m|0m)", "", self.output()
        )
        self.assertNotIn("\x1b", visible)

    def test_artwork_is_nine_fixed_ascii_rows_with_network_rails(self):
        self.assertEqual((INTRO_ART_WIDTH, INTRO_ART_HEIGHT), (57, 9))
        self.assertTrue(all(len(line) == 57 for line in INTRO_ART))
        self.assertTrue(
            all(32 <= ord(char) <= 126 for line in INTRO_ART for char in line)
        )
        self.assertEqual(INTRO_ART[0], INTRO_ART[-1])
        self.assertTrue(INTRO_ART[0].startswith("o---o"))
        self.assertEqual(INTRO_FRAMES[-1], INTRO_ART)

    def test_horizontal_reveal_is_centered_in_both_axes_and_keeps_three_colors(self):
        for width, height in ((59, 11), (80, 24), (120, 40), (240, 100)):
            with self.subTest(size=(width, height)):
                self.channel.writes.clear()
                self.size = (width, height)
                result = self.run_intro()
                terminal = Screen(width, height, fill="X")
                top, left = (height - 9) // 2, (width - 57) // 2
                previous_reveal = 0
                for index, (_, data) in enumerate(self.channel.writes[:-1]):
                    terminal.feed(data.decode())
                    revealed = len(INTRO_FRAMES[index][0])
                    self.assertGreater(revealed, previous_reveal)
                    previous_reveal = revealed
                    for row in range(height):
                        expected = " " * width
                        if top <= row < top + 9:
                            content = INTRO_ART[row - top][:revealed]
                            expected = (
                                " " * left + content + " " * (width - left - revealed)
                            )
                        self.assertEqual("".join(terminal.cells[row]), expected)
                    self.assertEqual(terminal.color, 0)
                for column, color in ((0, 36), (19, 35), (38, 32)):
                    self.assertEqual(terminal.colors[top][left + column], color)
                terminal.feed(self.channel.writes[-1][1].decode())
                self.assertTrue(all(set(row) == {" "} for row in terminal.cells))
                self.assertEqual(
                    (terminal.row, terminal.column, terminal.color), (0, 0, 0)
                )
                self.assertLessEqual(result.bytes_sent, INTRO_MAX_BYTES)

    def test_slow_writes_drop_obsolete_frames_without_backlog(self):
        self.channel.write_delay = 0.37
        result = self.run_intro()
        self.assertEqual(result.frames, 6)
        # Writes at 0,.37,.74,1.11,1.48,1.85 choose frames0,3,7,11,14,18.
        for (_, data), index in zip(
            self.channel.writes[:-1], (0, 3, 7, 11, 14, 18), strict=True
        ):
            first_line = re.split(rb"\x1b\[[1-9][0-9]*;[1-9][0-9]*H", data)[1]
            first_line = re.sub(rb"\x1b\[(?:0|32|35|36)m", b"", first_line)
            self.assertEqual(first_line.decode(), INTRO_FRAMES[index][0])
        self.assertTrue(all(when < 12.0 for when, _ in self.channel.writes[:-1]))
        self.assertEqual(self.channel.writes[-1][1], (RESET + CLEAR).encode())

    def test_large_slow_write_never_replays_a_second_frame(self):
        self.channel.write_delay = 2.0
        self.assertEqual(self.run_intro().frames, 1)
        self.assertEqual(len(self.channel.writes), 2)

    def test_configuration_capability_and_size_fallbacks_have_no_output_or_delay(self):
        for options in (
            {"mode": "off"},
            {"terminal_type": ""},
            {"terminal_type": "dumb"},
            {"terminal_type": "xterm-untrusted"},
            {"dimensions": lambda: (INTRO_MIN_COLUMNS - 1, 24)},
            {"dimensions": lambda: (80, INTRO_MIN_ROWS - 1)},
            {"dimensions": lambda: (19, 4)},
        ):
            with self.subTest(options=options):
                self.assertEqual(self.run_intro(**options).frames, 0)
                self.assertEqual(self.clock(), 10.0)
                self.assertEqual(self.output(), "")

    def test_monochrome_reveal_still_clears_and_uses_identical_geometry(self):
        result = self.run_intro(
            terminal_type="vt100",
            presentation=LobbyPresentation(False, True),
            dimensions=lambda: (59, 11),
        )
        self.assertEqual(result.frames, INTRO_FRAME_COUNT)
        self.assertNotIn(RESET, self.output())
        self.assertNotRegex(self.output(), r"\x1b\[[0-9]+m")
        self.assertEqual(self.output().count(CLEAR), 2)
        self.assertTrue(self.output().startswith(CLEAR))
        self.assertTrue(self.output().endswith(CLEAR))

    def test_clearing_off_skips_full_screen_motion_without_delay_or_controls(self):
        for presentation in (LobbyPresentation(), LobbyPresentation(True, False)):
            with self.subTest(presentation=presentation):
                self.assertEqual(
                    self.run_intro(presentation=presentation).reason, "clearing_off"
                )
                self.assertEqual(self.output(), "")
                self.assertEqual(self.clock(), 10.0)

    def test_skip_consumes_ready_paste_and_waits_for_a_new_welcome_key(self):
        self.event(
            0.2, lambda: self.channel.incoming.extend([b" i agree\r\n1\n", b"whoami\n"])
        )
        result = self.run_intro()
        self.assertEqual(result.reason, "skipped")
        self.assertLess(self.clock(), 11.0)
        self.assertIsNone(self.reader.poll_key(self.clock() + 0.2))
        self.channel.incoming.append(b" ")
        self.assertEqual(self.reader.poll_key(self.clock()), ord(" "))
        self.assertNotIn("i agree", self.output())

    def test_partial_escape_and_crlf_tails_stay_at_the_intro_boundary(self):
        for first, tail in ((b"\x1b[", b"A"), (b"\r", b"\n"), (b"\xe2", b"\x82\xac")):
            with self.subTest(first=first):
                self.channel.incoming.append(first)
                self.assertEqual(self.run_intro().reason, "skipped")
                self.channel.incoming.append(tail)
                self.assertIsNone(self.reader.poll_key(self.clock() + 0.1))
                self.channel.incoming.append(b" ")
                self.assertEqual(self.reader.poll_key(self.clock()), ord(" "))

    def test_quit_and_interrupt_keys_cancel_without_static_handoff(self):
        for key in (b"q", b"Q", b"\x03", b"\x04"):
            with self.subTest(key=key):
                self.event(0.05, lambda key=key: self.channel.incoming.append(key))
                with self.assertRaises(LobbyClosed) as caught:
                    self.run_intro()
                self.assertEqual(caught.exception.reason, "quit")
                self.assertTrue(self.output().endswith(RESET + CLEAR))

    def test_resize_stops_motion_and_clears_entire_display_at_new_size(self):
        self.event(0.05, lambda: setattr(self, "size", (8, 4)))
        result = self.run_intro()
        self.assertEqual(result.reason, "resized")
        self.assertEqual(result.frames, 1)
        self.assertLess(self.clock(), 10.2)
        self.assertEqual(self.channel.writes[-1][1], (RESET + CLEAR).encode())

    def test_output_and_empty_drains_do_not_extend_idle_deadline(self):
        self.reader.idle_timeout_seconds = 0.25
        with self.assertRaises(LobbyClosed) as caught:
            self.run_intro()
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertAlmostEqual(self.clock(), 10.25)

    def test_ready_cancellation_wins_over_a_simultaneous_resize(self):
        for key in (b"q", b"Q", b"\x03", b"\x04"):
            with self.subTest(key=key):
                self.size = (80, 24)

                def resize_and_cancel(key=key):
                    self.size = (40, 24)
                    self.channel.incoming.append(key)
                    self.channel.after_write = lambda: None

                self.channel.after_write = resize_and_cancel
                with self.assertRaises(LobbyClosed) as caught:
                    self.run_intro()
                self.assertEqual(caught.exception.reason, "quit")

    def test_unexpected_failure_is_logged_as_failure_and_cleans_up(self):
        # Fail during a frame, but allow cleanup to write successfully.
        def fail_once():
            self.channel.after_write = lambda: None
            raise RuntimeError("broken adapter")

        self.channel.after_write = fail_once
        with self.assertLogs("dreamland.lifecycle", level="DEBUG") as captured:
            with self.assertRaises(RuntimeError):
                self.run_intro()
        self.assertIn("introduction started", captured.output[0])
        self.assertIn("reason=failure", captured.output[-1])
        self.assertTrue(self.output().endswith(RESET + CLEAR))

    def test_shutdown_cancels_within_receive_bound(self):
        self.event(0.05, self.stop.set)
        with self.assertRaises(LobbyClosed) as caught:
            self.run_intro()
        self.assertEqual(caught.exception.reason, "shutdown")
        self.assertLessEqual(self.clock(), 10.15)
        self.assertEqual(len(self.channel.writes), 2)

    def test_disconnect_never_attempts_late_cleanup_on_closed_channel(self):
        self.event(0.05, lambda: setattr(self.channel, "closed", True))
        with self.assertRaises(LobbyClosed) as caught:
            self.run_intro()
        self.assertEqual(caught.exception.reason, "disconnect")
        self.assertEqual(len(self.channel.writes), 1)

    def test_send_failure_ends_effect_without_more_frames(self):
        def fail():
            raise OSError("closed transport")

        self.channel.after_write = fail
        with self.assertRaises(OSError):
            self.run_intro()
        self.assertEqual(len(self.channel.writes), 2)
        self.assertEqual(self.channel.writes[-1][1], (RESET + CLEAR).encode())

    def test_byte_budget_reserves_cleanup_before_sending_each_frame(self):
        with patch("dreamland.lobby_intro.INTRO_MAX_BYTES", 250):
            result = self.run_intro()
        self.assertEqual(result.reason, "budget")
        self.assertEqual(result.frames, 1)
        self.assertLessEqual(result.bytes_sent, 250)
        self.assertTrue(self.output().endswith(RESET + CLEAR))


if __name__ == "__main__":
    unittest.main()
