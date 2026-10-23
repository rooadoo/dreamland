import re
import threading
import unittest
from unittest.mock import patch

from dreamland.lobby_presentation import CLEAR, LobbyPresentation
from dreamland.ssh_lobby import WORLD_PROMPT, SSHLobby
from tests import test_ssh_lobby as fixtures
from tests import test_ssh_lobby_presentation as presentation_tests
from tests import test_ssh_lobby_sessions as session_fixtures


class PresentationReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SSHLobbyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.runner = threading.Thread(
            target=lambda: self.fixture.lobby.run(
                self.fixture.channel,
                "visitor",
                self.fixture.backend,
                None,
                console=self.fixture.console,
                stop_event=self.fixture.stop,
                terminal_type="xterm-256color",
                width=80,
                height=100,
            )
        )

    def visible(self):
        return presentation_tests.plain(
            self.fixture.channel.output.rsplit(CLEAR, 1)[-1]
        )

    def test_catalog_accents_keep_each_description_with_its_numbered_world(self):
        f = self.fixture
        # Capture the controller's actual menu rather than a hand-constructed
        # list that could accidentally omit a grouping regression.
        with patch.object(f.lobby, "_screen", wraps=f.lobby._screen) as render:
            f.agree()
            menu = render.call_args.args[1]
        self.assertIsInstance(menu, list)

        for height, expected_pages in ((12, 2), (16, 1)):
            for presentation in (LobbyPresentation(), LobbyPresentation(True, True)):
                with self.subTest(height=height, presentation=presentation):
                    reader = fixtures.PageReader()
                    SSHLobby._screen(
                        reader,
                        menu,
                        80,
                        height,
                        heading="CHOOSE A WORLD",
                        prompt=WORLD_PROMPT,
                        presentation=presentation,
                    )
                    pages = [
                        presentation_tests.plain(page)
                        for page in reader.pages + [reader.output]
                    ]
                    self.assertEqual(len(pages), expected_pages)
                    for title, description in (
                        ("[1] First World", "Explore first."),
                        ("[2] Second World", "Explore second."),
                    ):
                        matching = [page for page in pages if title in page]
                        self.assertEqual(len(matching), 1)
                        self.assertIn(description, matching[0])
                        self.assertRegex(
                            matching[0],
                            re.escape(title) + r"\n +" + re.escape(description),
                        )
                    for page in pages:
                        self.assertLessEqual(len(page.split("\n")), height)
                        self.assertTrue(
                            all(len(line) <= 80 for line in page.split("\n"))
                        )

    def test_refresh_clears_completed_cleanup_feedback_but_keeps_ended_visit(self):
        f = self.fixture
        blocked = session_fixtures.BlockingAgent()
        self.addCleanup(blocked.finish.set)
        f.backend.agent = blocked
        f.agree()
        session = f.select()
        public_id = session.runtime_provenance.public_id
        f.channel.incoming.put(b"agent-check\n")
        self.assertTrue(blocked.started.wait(2))
        offset = len(f.channel.output)
        f.console.execute(f"sessions --close {public_id}", allow_exit=False)
        f.channel.wait_for("Enter a number", after=offset)
        self.assertIn(
            "Previous work is still stopping.", " ".join(self.visible().split())
        )
        self.assertIn(public_id, self.visible())

        blocked.finish.set()
        self.assertTrue(blocked.released.wait(2))
        offset = len(f.channel.output)
        f.channel.incoming.put(b"r\n")
        f.channel.wait_for("Enter a number", after=offset)
        self.assertNotIn("still stopping", self.visible())
        self.assertIn("closed by the operator", self.visible())
        self.assertIn(public_id, self.visible())
        self.assertIn("First World", self.visible())
        self.assertEqual(len(f.backend.sessions), 1)


if __name__ == "__main__":
    unittest.main()
