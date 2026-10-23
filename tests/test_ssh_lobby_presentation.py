import re
import threading
import unittest
from unittest.mock import patch

from dreamland.config import LobbySettings
from dreamland.lobby_presentation import CLEAR, RESET, LobbyPresentation, Text
from dreamland.ssh_lobby import AGREEMENT_PROMPT, WORLD_PROMPT, SSHLobby
from tests import test_ssh_lobby as fixtures

ALLOWED_CONTROLS = re.compile(r"\x1b\[(?:0|32|35|36)m|\x1b\[2J|\x1b\[H")
SIZES = (
    (20, 5),
    (20, 8),
    (20, 12),
    (20, 24),
    (39, 12),
    (40, 12),
    (40, 24),
    (80, 24),
    (120, 100),
)


def plain(text):
    result = ALLOWED_CONTROLS.sub("", text)
    if "\x1b" in result:
        raise AssertionError("Unexpected terminal control")
    return result


class StyledLayoutTests(unittest.TestCase):
    def test_styling_preserves_every_existing_page_and_viewport(self):
        baseline = fixtures.LobbyPresentationTests()
        for width, height in SIZES:
            for screen in baseline.screens():
                with self.subTest(size=(width, height), heading=screen[0]):
                    reader = fixtures.PageReader()
                    SSHLobby._screen(
                        reader,
                        screen[1],
                        width,
                        height,
                        heading=screen[0],
                        prompt=screen[2],
                        keypress=screen[3],
                        presentation=LobbyPresentation(True, True),
                    )
                    pages = reader.pages + [reader.output]
                    self.assertEqual(
                        [plain(page) for page in pages],
                        baseline.render(screen, width, height),
                    )
                    for page in pages:
                        self.assertEqual(page.count(CLEAR), 1)
                        self.assertTrue(page.endswith(RESET))

    def test_last_id_stays_on_every_page_with_status_inside_the_budget(self):
        reference = "DL-2222-3333-4444-5555"
        for width, height in SIZES:
            with self.subTest(size=(width, height)):
                reader = fixtures.PageReader()
                SSHLobby._screen(
                    reader,
                    [
                        Text("Last ended world: First World"),
                        Text("The world could not continue. Please select again."),
                        Text("[1] First World", "selection"),
                        Text("    Explore again."),
                    ],
                    width,
                    height,
                    heading="CHOOSE A WORLD",
                    prompt=WORLD_PROMPT,
                    reference=reference,
                    presentation=LobbyPresentation(True, True),
                )
                pages = [plain(page) for page in reader.pages + [reader.output]]
                for page in pages:
                    self.assertIn(reference, "".join(page.split()))
                    self.assertLessEqual(len(page.split("\n")), height)
                    self.assertTrue(
                        all(len(line) <= width for line in page.split("\n"))
                    )
                    self.assertIn("Q", page)
                self.assertTrue(pages[-1].endswith("> "))
                self.assertIn("Enter", pages[-1])
                combined = " ".join(" ".join(pages).split())
                self.assertIn("The world could not", combined)
                self.assertIn("continue.", combined)
                self.assertIn("Last ended world:", combined)
                self.assertIn("First World", combined)

    def test_resize_rewraps_only_unseen_text_at_input_boundaries(self):
        sizes = ((80, 24), (20, 5), (40, 12), (120, 100))
        reader = fixtures.PageReader()
        calls = []

        def dimensions():
            size = sizes[min(len(reader.pages), len(sizes) - 1)]
            calls.append(size)
            return size

        tokens = [f"token{i:03d}" for i in range(150)]
        SSHLobby._screen(
            reader,
            " ".join(tokens),
            80,
            24,
            heading="GUIDELINES",
            prompt=AGREEMENT_PROMPT,
            dimensions=dimensions,
            presentation=LobbyPresentation(True, True),
        )
        pages = [plain(page) for page in reader.pages + [reader.output]]
        self.assertEqual(len(calls), len(pages))
        combined = " ".join(pages)
        for token in tokens:
            self.assertEqual(combined.count(token), 1)
        for page, (width, height) in zip(pages, calls, strict=True):
            self.assertLessEqual(len(page.split("\n")), height)
            self.assertTrue(all(len(line) <= width for line in page.split("\n")))
        for page in pages[:-1]:
            self.assertNotIn("i agree", page)

    def test_below_minimum_uses_real_width_and_plain_linear_output(self):
        for width, height in ((19, 5), (80, 4), (5, 2), (1, 1)):
            reader = fixtures.PageReader()
            SSHLobby._screen(
                reader,
                "These are the complete guidelines.",
                width,
                height,
                prompt=AGREEMENT_PROMPT,
                presentation=LobbyPresentation(True, True),
            )
            self.assertEqual(reader.pages, [])
            self.assertNotIn("\x1b", reader.output)
            self.assertTrue(
                all(len(line) <= width for line in reader.output.split("\n"))
            )
            self.assertIn(
                "Thesearethecompleteguidelines.", "".join(reader.output.split())
            )

    def test_widening_rejoins_unread_source_lines(self):
        reader = fixtures.PageReader()
        words = [f"word{i:03d}" for i in range(40)]
        SSHLobby._screen(
            reader,
            " ".join(words),
            20,
            5,
            prompt=AGREEMENT_PROMPT,
            dimensions=lambda: (20, 5) if not reader.pages else (80, 24),
        )
        self.assertEqual(len(reader.pages), 1)
        self.assertIn("word002 word003 word004 word005", reader.output)
        all_text = " ".join(reader.pages) + reader.output
        for word in words:
            self.assertEqual(all_text.count(word), 1)


class StyledControllerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SSHLobbyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.size = (80, 100)
        self.terminal_type = "xterm-256color"
        self.fixture.runner = threading.Thread(
            target=lambda: self.fixture.lobby.run(
                self.fixture.channel,
                "visitor",
                self.fixture.backend,
                None,
                console=self.fixture.console,
                stop_event=self.fixture.stop,
                terminal_type=self.terminal_type,
                dimensions=lambda: self.size,
            )
        )

    def visible(self):
        return plain(self.fixture.channel.output.rsplit(CLEAR, 1)[-1])

    def answer(self, payload, prompt="Enter a number"):
        f = self.fixture
        offset = len(f.channel.output)
        f.channel.incoming.put(payload)
        f.channel.wait_for(prompt, after=offset)

    def test_feedback_survives_clear_refresh_and_failed_start(self):
        f = self.fixture
        f.agree()
        self.answer(b"nonsense\n")
        self.assertIn("That input is not valid", self.visible())
        self.answer(b"r\n")
        self.assertIn("That input is not valid", self.visible())
        f.catalog.order(["beta", "alpha"])
        self.answer(b"1\n")
        self.assertIn("unavailable or the menu changed", self.visible())
        with patch.object(
            f.backend, "create_session", side_effect=ValueError("private")
        ):
            self.answer(b"1\n")
        self.assertIn("The world could not continue", self.visible())
        self.assertNotIn("private", f.channel.output)
        self.assertNotIn(" Started]", f.channel.output)
        f.catalog.remove("alpha")
        f.catalog.remove("beta")
        self.answer(b"r\n")
        self.assertIn("No worlds are available", self.visible())
        self.assertEqual(f.backend.sessions, [])

    def test_ended_id_persists_through_retry_refresh_failure_and_new_visit(self):
        f = self.fixture
        f.agree()
        first = f.select()
        first_id = first.runtime_provenance.public_id
        self.assertIn(f"[DREAMLAND simulation {first_id} Started]", f.channel.output)
        self.answer(b"exit\n")
        self.assertIn(f"[DREAMLAND simulation {first_id} Ended]", f.channel.output)
        for payload in (b"r\n", b"invalid\n", b"999\n"):
            self.answer(payload)
            self.assertIn(first_id, self.visible())
            self.assertIn("First World", self.visible())
        with patch.object(f.backend, "create_session", side_effect=ValueError):
            self.answer(b"1\n")
        self.assertIn(first_id, self.visible())
        second = f.select(b"2\n")
        second_id = second.runtime_provenance.public_id
        self.assertNotEqual(first_id, second_id)
        offset = len(f.channel.output)
        f.console.execute(f"sessions --close {second_id}", allow_exit=False)
        f.channel.wait_for("Enter a number", after=offset)
        self.assertIn(second_id, self.visible())
        self.assertNotIn(first_id, self.visible())
        self.assertIn("closed by the operator", self.visible())

    def test_resize_while_typing_does_not_redraw_or_lose_input(self):
        f = self.fixture
        f.begin()
        f.channel.incoming.put(b"i agr")
        f.channel.wait_for("> " + RESET + "i agr")
        before = f.channel.output
        self.size = (40, 24)
        self.assertEqual(f.channel.output, before)
        self.answer(b"ee\n", "Enter a number")
        self.assertIn("CHOOSE A WORLD", self.visible())
        self.assertTrue(all(len(line) <= 40 for line in self.visible().splitlines()))
        self.assertEqual(f.backend.sessions, [])

    def test_wrong_agreement_retries_without_clearing_or_repeating_disclosure(self):
        f = self.fixture
        f.begin()
        clears = f.channel.output.count(CLEAR)
        self.answer(b"yes\n", "Type i agree")
        self.assertEqual(f.channel.output.count(CLEAR), clears)
        self.assertIn("That input is not valid", self.visible())
        self.assertEqual(f.channel.output.count("We will not publish"), 1)

    def test_cancellation_finishes_with_style_reset(self):
        f = self.fixture
        f.begin()
        f.channel.incoming.put(b"\x03")
        f.runner.join(3)
        self.assertFalse(f.runner.is_alive())
        self.assertTrue(f.channel.output.endswith(RESET))
        self.assertEqual(f.backend.sessions, [])

    def test_shutdown_finishes_with_style_reset(self):
        f = self.fixture
        f.begin()
        f.stop.set()
        f.runner.join(3)
        self.assertFalse(f.runner.is_alive())
        self.assertTrue(f.channel.output.endswith(RESET))

    def test_timeout_finishes_with_style_reset(self):
        f = self.fixture
        f.lobby.settings = LobbySettings(
            idle_timeout_seconds=0.02, intro_animation="off"
        )
        f.runner.start()
        f.runner.join(3)
        self.assertFalse(f.runner.is_alive())
        self.assertIn("idle timeout", f.channel.output)
        self.assertTrue(f.channel.output.endswith(RESET))

    def test_controller_failure_finishes_with_style_reset(self):
        f = self.fixture
        f.begin()
        with patch.object(f.catalog, "snapshot", side_effect=ValueError("private")):
            f.channel.incoming.put(b"i agree\n")
            f.runner.join(3)
        self.assertFalse(f.runner.is_alive())
        self.assertIn("The lobby is unavailable", f.channel.output)
        self.assertNotIn("private", f.channel.output)
        self.assertTrue(f.channel.output.endswith(RESET))

    def test_minimal_connection_does_not_emit_cleanup_escape(self):
        self.size = (19, 4)
        f = self.fixture
        f.runner.start()
        f.channel.wait_for("Any key: continue")
        f.channel.incoming.put(b"q")
        f.runner.join(3)
        self.assertFalse(f.runner.is_alive())
        self.assertNotIn("\x1b", f.channel.output)

    def test_both_off_keep_connection_linear(self):
        f = self.fixture
        f.lobby.settings = LobbySettings(color="off", clear_screen="off")
        f.agree()
        self.answer(b"invalid\n")
        self.assertNotIn("\x1b", f.channel.output)
