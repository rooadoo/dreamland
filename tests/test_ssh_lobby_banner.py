import unittest

from dreamland.lobby_presentation import (
    CLEAR,
    WELCOME_BANNER,
    WELCOME_BANNER_HEIGHT,
    WELCOME_BANNER_WIDTH,
    LobbyPresentation,
    welcome_banner_lines,
)
from dreamland.ssh_lobby import WELCOME_PROMPT, SSHLobby
from tests import test_ssh_lobby as fixtures
from tests.test_ssh_lobby_presentation import plain

STYLED = LobbyPresentation(True, True)


class BannerSelectionTests(unittest.TestCase):
    def select(self, **overrides):
        settings = {
            "banner": "auto",
            "presentation": LobbyPresentation(True, True),
            "columns": 47,
            "rows": 15,
            "body_rows": 9,
            "footer_rows": 2,
            "leading_rows": 1,
        }
        settings.update(overrides)
        return welcome_banner_lines(**settings)

    def test_static_art_has_approved_dimensions_and_printable_ascii(self):
        self.assertEqual(WELCOME_BANNER_WIDTH, 47)
        self.assertEqual(WELCOME_BANNER_HEIGHT, 3)
        self.assertEqual(tuple(map(len, WELCOME_BANNER)), (47, 47, 47))
        self.assertTrue(WELCOME_BANNER[0].endswith(" "))
        for line in WELCOME_BANNER:
            self.assertTrue(all(32 <= ord(character) <= 126 for character in line))

    def test_exact_fit_accounts_for_every_kind_of_occupied_row(self):
        self.assertEqual(self.select(), WELCOME_BANNER)
        for change in (
            {"columns": 46},
            {"rows": 14},
            {"body_rows": 10},
            {"footer_rows": 3},
            {"leading_rows": 2},
        ):
            with self.subTest(change=change):
                self.assertEqual(self.select(**change), ("DREAMLAND",))

    def test_color_and_clearing_independently_allow_the_art(self):
        for presentation in (
            LobbyPresentation(True, False),
            LobbyPresentation(False, True),
        ):
            with self.subTest(presentation=presentation):
                self.assertEqual(self.select(presentation=presentation), WELCOME_BANNER)

    def test_plain_simple_and_empty_notice_choose_simple_brand(self):
        for change in (
            {"presentation": LobbyPresentation()},
            {"banner": "simple"},
            {"body_rows": 0},
        ):
            with self.subTest(change=change):
                self.assertEqual(self.select(**change), ("DREAMLAND",))


class BannerLayoutTests(unittest.TestCase):
    def render(
        self,
        content,
        width=80,
        height=24,
        *,
        banner="auto",
        presentation=STYLED,
        heading="WELCOME",
        dimensions=None,
    ):
        reader = fixtures.PageReader()
        SSHLobby._screen(
            reader,
            content,
            width,
            height,
            heading=heading,
            prompt=WELCOME_PROMPT,
            keypress=True,
            banner=banner,
            presentation=presentation,
            dimensions=dimensions,
        )
        return reader.pages + [reader.output]

    def assert_fits(self, pages, width, height):
        for number, page in enumerate(pages, 1):
            with self.subTest(page=number):
                lines = plain(page).split("\n")
                self.assertLessEqual(len(lines), height)
                self.assertTrue(all(len(line) <= width for line in lines))
                self.assertIn("Q", plain(page))
                self.assertIn("continue", plain(page))

    def assert_has_art(self, page):
        lines = plain(page).split("\n")
        position = lines.index("  " + WELCOME_BANNER[0])
        self.assertEqual(
            lines[position : position + WELCOME_BANNER_HEIGHT],
            ["  " + line for line in WELCOME_BANNER],
        )

    def assert_simple(self, page):
        self.assertNotIn("o---o", plain(page))
        self.assertIn("DREAMLAND", plain(page))

    def test_current_notice_and_action_fit_alongside_intact_art(self):
        notice = fixtures.LobbyPresentationTests().screens()[0][1]
        pages = self.render(notice)
        self.assertEqual(len(pages), 1)
        self.assert_has_art(pages[0])
        self.assert_fits(pages, 80, 24)
        self.assertEqual(notice.split("\n\n", 1)[0], "DREAMLAND")
        for line in notice.split("\n\n", 1)[1].splitlines():
            if line:
                self.assertIn("  " + line, plain(pages[0]).split("\n"))
        self.assertIn("-- Page 1 of 1", plain(pages[0]))

    def test_custom_copy_and_its_indentation_are_preserved(self):
        notice = "OPERATOR WELCOME\n\n    Keep this indentation.\nCustom notice text."
        for banner in ("auto", "simple"):
            with self.subTest(banner=banner):
                pages = self.render(notice, banner=banner)
                self.assertEqual(len(pages), 1)
                for line in notice.splitlines():
                    if line:
                        self.assertIn("  " + line, plain(pages[0]).split("\n"))
                self.assert_fits(pages, 80, 24)

    def test_leading_standalone_wordmark_is_rendered_once_in_simple_mode(self):
        notice = "DREAMLAND\n\nOperator welcome text."
        for presentation in (STYLED, LobbyPresentation()):
            with self.subTest(presentation=presentation):
                pages = self.render(notice, banner="simple", presentation=presentation)
                self.assertEqual(len(pages), 1)
                self.assertEqual(plain(pages[0]).count("DREAMLAND"), 1)
                self.assertIn("Operator welcome text.", plain(pages[0]))

    def test_wordmark_replacement_leaves_nonheading_mentions_untouched(self):
        for notice in (
            "DREAMLAND is available here.",
            "Operator heading.\n\nDREAMLAND",
            "DREAMLAND\nThe same paragraph continues here.",
            "    DREAMLAND\n\nAn indented operator heading.",
        ):
            with self.subTest(notice=notice):
                pages = self.render(notice)
                self.assert_has_art(pages[0])
                for line in notice.splitlines():
                    if line:
                        self.assertIn("  " + line, plain(pages[0]).split("\n"))

    def test_current_copy_height_boundary_uses_the_replaced_heading(self):
        notice = fixtures.LobbyPresentationTests().screens()[0][1]
        for height, large in ((21, True), (20, False)):
            with self.subTest(height=height):
                pages = self.render(notice, 80, height)
                self.assertEqual(len(pages), 1)
                if large:
                    self.assert_has_art(pages[0])
                else:
                    self.assert_simple(pages[0])
                self.assert_fits(pages, 80, height)

    def test_art_width_includes_both_renderer_margins(self):
        for width, large in ((51, True), (50, False)):
            with self.subTest(width=width):
                pages = self.render("Custom welcome.", width, 24)
                self.assertEqual(len(pages), 1)
                if large:
                    self.assert_has_art(pages[0])
                else:
                    self.assert_simple(pages[0])
                self.assert_fits(pages, width, 24)

    def test_art_height_includes_body_leading_blank_and_footer(self):
        notice = "\n".join(f"Notice line {number}." for number in range(9))
        for height, large in ((15, True), (14, False)):
            with self.subTest(height=height):
                pages = self.render(notice, 80, height)
                self.assertEqual(len(pages), 1)
                if large:
                    self.assert_has_art(pages[0])
                else:
                    self.assert_simple(pages[0])
                self.assert_fits(pages, 80, height)

    def test_long_notice_fallback_preserves_copy_and_page_budgets(self):
        lines = [f"notice{number:03d}" for number in range(40)]
        notice = "\n".join(lines)
        automatic = self.render(notice, 80, 12)
        simple = self.render(notice, 80, 12, banner="simple")
        self.assertEqual(automatic, simple)
        self.assertGreater(len(automatic), 1)
        self.assert_fits(automatic, 80, 12)
        self.assert_simple(automatic[0])
        combined = "\n".join(plain(page) for page in automatic)
        for line in lines:
            self.assertEqual(combined.count(line), 1)
        for page in automatic[1:]:
            self.assertNotIn("o---o", plain(page))
            self.assertIn("DREAMLAND / WELCOME", plain(page))

    def test_short_notice_can_fit_art_without_extra_pages(self):
        for width, height in ((80, 24), (120, 100)):
            with self.subTest(size=(width, height)):
                pages = self.render("Welcome aboard.", width, height)
                self.assertEqual(len(pages), 1)
                self.assert_has_art(pages[0])
                self.assertIn("Welcome aboard.", plain(pages[0]))
                self.assert_fits(pages, min(width, 80), min(height, 100))

    def test_output_capabilities_and_forced_simple_preserve_functional_copy(self):
        cases = (
            (LobbyPresentation.select("xterm", color="off"), "auto", True),
            (LobbyPresentation.select("xterm", clear_screen="off"), "auto", True),
            (LobbyPresentation.select("vt100"), "auto", True),
            (LobbyPresentation.select("dumb"), "auto", False),
            (LobbyPresentation.select("unrecognized"), "auto", False),
            (LobbyPresentation.select(""), "auto", False),
            (LobbyPresentation(), "auto", False),
            (LobbyPresentation(True, True), "simple", False),
        )
        for presentation, banner, large in cases:
            with self.subTest(presentation=presentation, banner=banner):
                pages = self.render(
                    "Operator notice.", presentation=presentation, banner=banner
                )
                self.assertEqual(len(pages), 1)
                if large:
                    self.assert_has_art(pages[0])
                else:
                    self.assert_simple(pages[0])
                if not presentation.color and not presentation.clear:
                    self.assertNotIn("\x1b", pages[0])
                self.assertEqual(pages[0].count(CLEAR), int(presentation.clear))
                self.assertIn("Operator notice.", plain(pages[0]))
                self.assert_fits(pages, 80, 24)

    def test_compact_simple_brand_and_actions_share_the_real_budget(self):
        tokens = [f"token{number:03d}" for number in range(24)]
        for banner in ("auto", "simple"):
            with self.subTest(banner=banner):
                pages = self.render(" ".join(tokens), 20, 5, banner=banner)
                self.assert_simple(pages[0])
                self.assert_fits(pages, 20, 5)
                combined = "\n".join(plain(page) for page in pages)
                for token in tokens:
                    self.assertEqual(combined.count(token), 1)

    def test_minimal_terminals_still_have_simple_brand_and_complete_copy(self):
        for width, height in ((19, 5), (80, 4), (5, 2), (1, 1)):
            with self.subTest(size=(width, height)):
                pages = self.render("Operator notice.", width, height)
                self.assertEqual(len(pages), 1)
                self.assertNotIn("\x1b", pages[0])
                joined = "".join(pages[0].split())
                self.assertIn("DREAMLAND", joined)
                self.assertIn("Operatornotice.", joined)
                self.assertNotIn("o---o", pages[0])
                self.assertTrue(
                    all(len(line) <= width for line in pages[0].split("\n"))
                )

    def test_widening_on_continuation_does_not_introduce_art(self):
        reader = fixtures.PageReader()
        sizes = []

        def dimensions():
            size = (20, 5) if not reader.pages else (80, 24)
            sizes.append(size)
            return size

        tokens = [f"token{number:03d}" for number in range(36)]
        SSHLobby._screen(
            reader,
            " ".join(tokens),
            20,
            5,
            heading="WELCOME",
            prompt=WELCOME_PROMPT,
            keypress=True,
            banner="auto",
            presentation=LobbyPresentation(True, True),
            dimensions=dimensions,
        )
        pages = reader.pages + [reader.output]
        self.assertGreater(len(pages), 1)
        self.assertEqual(len(sizes), len(pages))
        for page, (width, height) in zip(pages, sizes, strict=True):
            self.assertNotIn("o---o", plain(page))
            self.assert_fits([page], width, height)
        self.assertIn("DREAMLAND / WELCOME", plain(pages[-1]))
        combined = "\n".join(plain(page) for page in pages)
        for token in tokens:
            self.assertEqual(combined.count(token), 1)

    def test_other_sections_cannot_show_the_welcome_art(self):
        for heading in ("GUIDELINES", "CHOOSE A WORLD"):
            with self.subTest(heading=heading):
                pages = self.render("Other section.", heading=heading)
                self.assertEqual(len(pages), 1)
                self.assertNotIn("o---o", plain(pages[0]))
                self.assertIn(f"{heading} | DREAMLAND", plain(pages[0]))
                self.assert_fits(pages, 80, 24)


if __name__ == "__main__":
    unittest.main()
