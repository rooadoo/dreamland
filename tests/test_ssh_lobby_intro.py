import re
import threading
import unittest
from unittest.mock import patch

from dreamland.config import LobbySettings
from dreamland.lobby_content import LobbyContent
from dreamland.lobby_input import LobbyInput
from dreamland.lobby_intro import run_intro
from dreamland.lobby_presentation import CLEAR
from tests import test_ssh_lobby as fixtures
from tests.test_lobby_input import TerminalChannel
from tests.test_ssh_lobby_presentation import plain

INTRO_FRAME_PATTERN = r"\x1b\[[1-9][0-9]*;[1-9][0-9]*H"
WELCOME_ACTION = "Press any key to continue"


class ObservedChannel(TerminalChannel):
    """Expose the controller waiting for fresh input, without test sleeps."""

    def __init__(self):
        super().__init__()
        self.empty_read = threading.Event()

    def recv(self, count):
        if self.incoming.empty():
            self.empty_read.set()
        return super().recv(count)


class IntroControllerTests(unittest.TestCase):
    def fixture(
        self, *, username="visitor", terminal_type="xterm", size=(80, 100), **settings
    ):
        fixture = fixtures.SSHLobbyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.channel = ObservedChannel()
        fixture.lobby.settings = LobbySettings(
            **{"intro_animation": "auto", **settings}
        )
        fixture.size = size
        fixture.runner = threading.Thread(
            target=lambda: fixture.lobby.run(
                fixture.channel,
                username,
                fixture.backend,
                None,
                console=fixture.console,
                stop_event=fixture.stop,
                terminal_type=terminal_type,
                dimensions=lambda: fixture.size,
            )
        )
        return fixture

    def wait_for_fresh_input(self, fixture):
        fixture.channel.empty_read.clear()
        self.assertTrue(fixture.channel.empty_read.wait(1))

    def wait_for_motion(self, fixture):
        with fixture.channel.condition:
            self.assertTrue(
                fixture.channel.condition.wait_for(
                    lambda: re.search(INTRO_FRAME_PATTERN, fixture.channel.output),
                    3,
                ),
                fixture.channel.output,
            )

    def quit(self, fixture, payload=b"q"):
        fixture.channel.incoming.put(payload)
        fixture.runner.join(3)
        self.assertFalse(fixture.runner.is_alive())
        self.assertEqual(fixture.channel.exit_status, 0)

    def assert_motion_precedes_welcome(self, fixture):
        output = fixture.channel.output
        motion = list(re.finditer(INTRO_FRAME_PATTERN, output))
        self.assertTrue(motion)
        welcome = output.index(WELCOME_ACTION)
        self.assertIn(CLEAR, output[: motion[0].start()])
        self.assertIn(CLEAR, output[motion[-1].end() : welcome])
        self.assertNotRegex(output[welcome:], INTRO_FRAME_PATTERN)

    def test_intro_runs_once_across_refresh_and_two_fresh_visits(self):
        fixture = self.fixture()
        with patch("dreamland.ssh_lobby.run_intro", wraps=run_intro) as intro:
            fixture.runner.start()
            self.wait_for_motion(fixture)
            fixture.channel.incoming.put(b" ")
            fixture.channel.wait_for(WELCOME_ACTION)
            fixture.channel.incoming.put(b" ")
            fixture.channel.wait_for("Type i agree")
            fixture.channel.incoming.put(b"i agree\n")
            fixture.channel.wait_for("Enter a number")
            offset = len(fixture.channel.output)
            fixture.channel.incoming.put(b"r\n")
            fixture.channel.wait_for("Enter a number", after=offset)
            first = fixture.select()
            offset = len(fixture.channel.output)
            fixture.channel.incoming.put(b"exit\n")
            fixture.channel.wait_for("Enter a number", after=offset)
            second = fixture.select(b"2\n")
            offset = len(fixture.channel.output)
            fixture.channel.incoming.put(b"exit\n")
            fixture.channel.wait_for("Enter a number", after=offset)
            self.quit(fixture, b"q\n")
            self.assertEqual(intro.call_count, 1)
            reconnect = self.fixture()
            reconnect.runner.start()
            self.wait_for_motion(reconnect)
            self.quit(reconnect)
            self.assertEqual(intro.call_count, 2)
            self.assertEqual(reconnect.backend.sessions, [])
        self.assertNotEqual(
            first.runtime_provenance.public_id, second.runtime_provenance.public_id
        )
        self.assertEqual(len(fixture.backend.sessions), 2)
        self.assertEqual(fixture.channel.output.count("Type i agree"), 1)
        self.assert_motion_precedes_welcome(fixture)

    def test_normal_completion_waits_at_welcome_without_late_frames(self):
        fixture = self.fixture()
        boundaries = []

        def record_boundary(*args, **kwargs):
            result = run_intro(*args, **kwargs)
            boundaries.append(len(fixture.channel.output))
            return result

        with patch("dreamland.ssh_lobby.run_intro", side_effect=record_boundary):
            fixture.runner.start()
            fixture.channel.wait_for(WELCOME_ACTION)
            self.wait_for_fresh_input(fixture)
        self.assertEqual(len(boundaries), 1)
        self.assertTrue(fixture.channel.output[: boundaries[0]].endswith(CLEAR))
        self.assertNotRegex(
            fixture.channel.output[boundaries[0] :], INTRO_FRAME_PATTERN
        )
        self.assertEqual(fixture.backend.sessions, [])
        self.assertNotIn("Type i agree", fixture.channel.output)
        self.assert_motion_precedes_welcome(fixture)
        self.quit(fixture)
        self.assert_motion_precedes_welcome(fixture)

    def test_simple_login_stays_static_and_monochrome_across_visits(self):
        normal = self.fixture()
        fixture = self.fixture(username="visitor_simple")
        # Both connections use one controller/settings object in production.
        fixture.lobby = normal.lobby
        settings = fixture.lobby.settings
        with self.assertLogs("dreamland.lifecycle", level="INFO") as logs:
            fixture.runner.start()
            fixture.channel.wait_for(WELCOME_ACTION)
            self.assertEqual(fixture.backend.sessions, [])
            self.assertNotIn("Type i agree", fixture.channel.output)
            fixture.channel.incoming.put(b" ")
            fixture.channel.wait_for("Type i agree")
            offset = len(fixture.channel.output)
            fixture.channel.incoming.put(b"no\n")
            fixture.channel.wait_for("Type i agree", after=offset)
            self.assertEqual(fixture.backend.sessions, [])
            fixture.channel.incoming.put(b"i agree\n")
            fixture.channel.wait_for("Enter a number")
            for number, username in ((b"1\n", "operator"), (b"2\n", "reader")):
                session = fixture.select(number)
                fixture.assert_entry_identity(session, username)
                offset = len(fixture.channel.output)
                fixture.channel.incoming.put(b"exit\n")
                fixture.channel.wait_for("Enter a number", after=offset)
            offset = len(fixture.channel.output)
            fixture.channel.incoming.put(b"r\n")
            fixture.channel.wait_for("Enter a number", after=offset)
            self.quit(fixture, b"q\n")
        self.assertNotRegex(fixture.channel.output, INTRO_FRAME_PATTERN)
        self.assertNotRegex(fixture.channel.output, r"\x1b\[[0-9;]*m")
        self.assertIn(CLEAR, fixture.channel.output)
        self.assertNotIn("visitor_simple", fixture.channel.output)
        self.assertIn("Authored fixture host.", fixture.channel.output)
        self.assertTrue(any("simple=True" in message for message in logs.output))
        self.assertIs(fixture.lobby.settings, settings)
        self.assertEqual(settings.color, "auto")
        self.assertEqual(settings.intro_animation, "auto")
        normal.runner.start()
        self.wait_for_motion(normal)
        self.assertRegex(normal.channel.output, r"\x1b\[(?:32|35|36)m")
        self.quit(normal)

    def test_simple_login_honors_clearing_off_and_unknown_terminal(self):
        for settings in ({"clear_screen": "off"}, {"terminal_type": "dumb"}):
            with self.subTest(settings=settings):
                fixture = self.fixture(username="visitor_simple", **settings)
                fixture.runner.start()
                fixture.channel.wait_for(WELCOME_ACTION)
                self.quit(fixture)
                self.assertNotIn("\x1b", fixture.channel.output)

    def test_fast_simple_login_retains_static_monochrome_presentation(self):
        fixture = self.fixture(username="dreamland_fast_simple")
        fixture.lobby.fast_backend = fixture.backend
        with patch("dreamland.ssh_lobby.run_intro", wraps=run_intro) as intro:
            fixture.runner.start()
            fixture.channel.wait_for(WELCOME_ACTION)
            self.assertEqual(intro.call_args.kwargs["mode"], "off")
            self.assertNotRegex(fixture.channel.output, INTRO_FRAME_PATTERN)
            self.assertNotRegex(fixture.channel.output, r"\x1b\[[0-9;]*m")
            self.quit(fixture)

    def test_skip_paste_and_partial_escape_cannot_advance_or_accept(self):
        fixture = self.fixture()
        fixture.runner.start()
        self.wait_for_motion(fixture)
        fixture.channel.incoming.put(b"i agree\r\n1\nwhoami\n\x1b[")
        fixture.channel.wait_for(WELCOME_ACTION)
        self.wait_for_fresh_input(fixture)
        self.assertNotIn("Type i agree", fixture.channel.output)
        self.assertEqual(fixture.backend.sessions, [])
        fixture.channel.incoming.put(b"1;5A")
        self.wait_for_fresh_input(fixture)
        self.assertNotIn("Type i agree", fixture.channel.output)
        fixture.channel.incoming.put(b" ")
        fixture.channel.wait_for("Type i agree")
        self.wait_for_fresh_input(fixture)
        self.assertNotIn("Enter a number", fixture.channel.output)
        fixture.channel.incoming.put(b"i agree\n")
        fixture.channel.wait_for("Enter a number")
        self.wait_for_fresh_input(fixture)
        self.assertEqual(fixture.backend.sessions, [])
        self.assertNotIn("whoami", fixture.channel.output)
        self.quit(fixture, b"q\n")
        self.assert_motion_precedes_welcome(fixture)

    def test_skip_crlf_tail_still_requires_a_new_welcome_key(self):
        fixture = self.fixture()
        fixture.runner.start()
        self.wait_for_motion(fixture)
        fixture.channel.incoming.put(b"\r")
        fixture.channel.wait_for(WELCOME_ACTION)
        fixture.channel.incoming.put(b"\n")
        self.wait_for_fresh_input(fixture)
        self.assertNotIn("Type i agree", fixture.channel.output)
        self.assertEqual(fixture.backend.sessions, [])
        self.quit(fixture)

    def test_cancel_keys_during_intro_never_show_welcome_or_create_session(self):
        for payload in (b"q", b"Q", b"\x03", b"\x04"):
            with self.subTest(payload=payload):
                fixture = self.fixture()
                fixture.runner.start()
                self.wait_for_motion(fixture)
                self.quit(fixture, payload)
                self.assertNotIn(WELCOME_ACTION, fixture.channel.output)
                self.assertEqual(fixture.backend.sessions, [])

    def test_disabled_unknown_and_small_modes_show_static_welcome_directly(self):
        for settings in (
            {"intro_animation": "off"},
            {"terminal_type": "dumb"},
            {"terminal_type": "unrecognized"},
            {"terminal_type": ""},
            {"clear_screen": "off"},
            {"clear_screen": "off", "color": "off"},
            {"size": (50, 100)},
            {"size": (80, 8)},
        ):
            with self.subTest(settings=settings):
                fixture = self.fixture(**settings)
                fixture.runner.start()
                action = "Any key: continue" if fixture.size[1] < 12 else WELCOME_ACTION
                fixture.channel.wait_for(action)
                self.wait_for_fresh_input(fixture)
                self.assertNotRegex(fixture.channel.output, INTRO_FRAME_PATTERN)
                self.assertEqual(fixture.backend.sessions, [])
                self.quit(fixture)

    def test_resize_during_motion_uses_new_static_viewport(self):
        fixture = self.fixture()
        welcome = fixture.root / "short-welcome.txt"
        welcome.write_text("Resize destination.", encoding="utf-8")
        fixture.lobby.content = LobbyContent(welcome_path=welcome)
        boundaries = []

        def record_boundary(*args, **kwargs):
            result = run_intro(*args, **kwargs)
            boundaries.append(len(fixture.channel.output))
            return result

        with patch("dreamland.ssh_lobby.run_intro", side_effect=record_boundary):
            fixture.runner.start()
            self.wait_for_motion(fixture)
            fixture.size = (20, 5)
            fixture.channel.wait_for("Any key: continue")
            self.wait_for_fresh_input(fixture)
            self.assertEqual(len(boundaries), 1)
            static = plain(fixture.channel.output[boundaries[0] :])
            lines = static.replace("\r\n", "\n").split("\n")
            self.assertLessEqual(len(lines), 5)
            self.assertTrue(all(len(line) <= 20 for line in lines))
            self.assertIn("Resize destination.", static)
            self.assertNotRegex(static, INTRO_FRAME_PATTERN)
            self.assertEqual(fixture.backend.sessions, [])
            self.quit(fixture)

    def test_shutdown_and_disconnect_during_motion_never_reach_static_prompt(self):
        for reason in ("shutdown", "disconnect"):
            with self.subTest(reason=reason):
                fixture = self.fixture()
                fixture.runner.start()
                self.wait_for_motion(fixture)
                if reason == "shutdown":
                    fixture.stop.set()
                else:
                    fixture.channel.incoming.put(b"")
                fixture.runner.join(3)
                self.assertFalse(fixture.runner.is_alive())
                self.assertNotIn(WELCOME_ACTION, fixture.channel.output)
                self.assertEqual(fixture.backend.sessions, [])

    def test_long_world_visit_starts_a_fresh_lobby_idle_period_on_return(self):
        fixture = self.fixture(intro_animation="off", idle_timeout_seconds=5)
        clock = [0.0]

        def reader(channel, **kwargs):
            return LobbyInput(channel, monotonic=lambda: clock[0], **kwargs)

        with patch("dreamland.ssh_lobby.LobbyInput", side_effect=reader):
            fixture.runner.start()
            fixture.channel.wait_for(WELCOME_ACTION)
            fixture.channel.incoming.put(b" ")
            fixture.channel.wait_for("Type i agree")
            fixture.channel.incoming.put(b"i agree\n")
            fixture.channel.wait_for("Enter a number")
            session = fixture.select()
            clock[0] += 600.0
            offset = len(fixture.channel.output)
            fixture.channel.incoming.put(b"exit\n")
            fixture.channel.wait_for("Enter a number", after=offset)
            self.wait_for_fresh_input(fixture)
            self.assertIn(session.runtime_provenance.public_id, fixture.channel.output)
            self.assertNotIn("idle timeout", fixture.channel.output)
            offset = len(fixture.channel.output)
            fixture.channel.incoming.put(b"r\n")
            fixture.channel.wait_for("Enter a number", after=offset)
            self.quit(fixture, b"q\n")


if __name__ == "__main__":
    unittest.main()
