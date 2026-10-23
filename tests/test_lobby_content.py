import os
import tempfile
import unittest
from pathlib import Path

from dreamland.lobby_content import LANGFUSE_DISCLOSURE, LobbyContent, LobbyContentError


class LobbyContentTests(unittest.TestCase):
    def test_defaults_disclose_processing_and_no_publication(self):
        notice = LobbyContent().snapshot()
        self.assertIn("DREAMLAND", notice.welcome)
        self.assertNotIn("Networks made of dreams", notice.welcome)
        self.assertIn("OpenAI", notice.guidelines)
        self.assertIn("We will not publish", notice.guidelines)
        self.assertIn("published research", notice.guidelines)
        self.assertEqual(notice.contact, "admin@boxisalie.com")
        self.assertEqual(len(notice.version), 64)
        self.assertNotIn(LANGFUSE_DISCLOSURE, notice.guidelines)
        self.assertEqual(notice, LobbyContent().snapshot())

    def test_langfuse_disclosure_changes_notice_version(self):
        notice = LobbyContent(langfuse_enabled=True).snapshot()
        self.assertIn(LANGFUSE_DISCLOSURE, notice.guidelines)
        self.assertNotEqual(notice.version, LobbyContent().snapshot().version)

    def test_operator_edits_apply_only_to_new_snapshots_without_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            welcome = Path(directory) / "welcome.txt"
            welcome.write_text(
                "DREAMLAND\r\nWelcome {username}: $(id)", encoding="utf-8"
            )
            content = LobbyContent(welcome_path=welcome)
            previous = content.snapshot()
            self.assertEqual(previous.welcome, "DREAMLAND\nWelcome {username}: $(id)")
            welcome.write_text("DREAMLAND\nA new notice", encoding="utf-8")
            current = content.snapshot()
        self.assertNotEqual(previous.version, current.version)
        self.assertIn("{username}", previous.welcome)

    def test_missing_invalid_or_oversized_copy_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notice.txt"
            with self.assertRaises(LobbyContentError):
                LobbyContent(welcome_path=path).snapshot()
            for data in (
                b"",
                b"\xff",
                b"\x1b]8;;https://invalid\x07",
                b"Hello\x00",
                b"x" * 16385,
                "Hello\u202e".encode(),
            ):
                path.write_bytes(data)
                with (
                    self.subTest(data=data[:20]),
                    self.assertRaises(LobbyContentError) as raised,
                ):
                    LobbyContent(welcome_path=path).snapshot()
                self.assertNotIn(str(path), str(raised.exception))

    def test_contact_is_one_bounded_plain_text_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contact.txt"
            path.write_text("help@example.com\n")
            self.assertEqual(
                LobbyContent(contact_path=path).snapshot().contact, "help@example.com"
            )
            path.write_text("help@example.com\nForged event")
            with self.assertRaises(LobbyContentError):
                LobbyContent(contact_path=path).snapshot()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_fifo_does_not_block_notice_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notice.txt"
            os.mkfifo(path)
            with self.assertRaisesRegex(LobbyContentError, "regular file"):
                LobbyContent(welcome_path=path).snapshot()
