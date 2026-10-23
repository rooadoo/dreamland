import unittest

from dreamland.models import InteractiveProgram
from dreamland.terminal import (
    BashLineEditor,
    SecretLineEditor,
    create_interactive_editor,
    program_uses_secret_input,
)


class BashLineEditorTests(unittest.TestCase):
    def setUp(self):
        self.output = []
        self.editor = BashLineEditor(self.output.append, "$ ")

    def test_submits_printable_command(self):
        command = self._feed("pwd\n")

        self.assertEqual(command, "pwd")
        self.assertEqual("".join(self.output), "pwd\r\n")

    def test_up_arrow_recalls_previous_command(self):
        self._feed("pwd\n")
        self.editor.start_prompt("$ ")
        self._feed("whoami\n")
        self.editor.start_prompt("$ ")

        command = self._feed("\x1b[A\n")

        self.assertEqual(command, "whoami")
        self.assertIn("\r$ whoami", "".join(self.output))

    def test_down_arrow_restores_draft_line(self):
        self._feed("pwd\n")
        self.editor.start_prompt("$ ")
        self._feed("whoami\n")
        self.editor.start_prompt("$ ")

        command = self._feed("ec\x1b[A\x1b[Bho hello\n")

        self.assertEqual(command, "echo hello")

    def test_left_arrow_inserts_at_cursor(self):
        command = self._feed("abcd\x1b[D\x1b[DX\n")

        self.assertEqual(command, "abXcd")

    def test_right_arrow_moves_cursor_forward(self):
        command = self._feed("abcd\x1b[D\x1b[D\x1b[CY\n")

        self.assertEqual(command, "abcYd")

    def test_backspace_deletes_before_cursor(self):
        command = self._feed("abcd\x1b[D\x1b[D\b\n")

        self.assertEqual(command, "acd")

    def test_ctrl_a_and_ctrl_e_move_to_line_boundaries(self):
        command = self._feed("bd\x01a\x05c\n")

        self.assertEqual(command, "abdc")

    def test_ctrl_b_and_ctrl_f_move_by_character(self):
        command = self._feed("abcd\x02\x02\x06X\n")

        self.assertEqual(command, "abcXd")

    def test_ctrl_p_and_ctrl_n_navigate_history(self):
        self._feed("pwd\n")
        self.editor.start_prompt("$ ")
        self._feed("whoami\n")
        self.editor.start_prompt("$ ")

        command = self._feed("\x10\x10\x0e\n")

        self.assertEqual(command, "whoami")

    def test_ctrl_u_kills_to_start(self):
        command = self._feed("abcd\x1b[D\x1b[D\x15X\n")

        self.assertEqual(command, "Xcd")

    def test_ctrl_k_kills_to_end(self):
        command = self._feed("abcd\x1b[D\x1b[D\x0bX\n")

        self.assertEqual(command, "abX")

    def test_ctrl_w_deletes_previous_word(self):
        command = self._feed("echo hello world\x17you\n")

        self.assertEqual(command, "echo hello you")

    def test_ctrl_y_yanks_last_killed_text(self):
        command = self._feed("echo hello world\x17you \x19\n")

        self.assertEqual(command, "echo hello you world")

    def test_ctrl_t_transposes_characters(self):
        command = self._feed("sl\x14\n")

        self.assertEqual(command, "ls")

    def test_ctrl_t_transposes_before_cursor(self):
        command = self._feed("abcd\x1b[D\x1b[D\x14\n")

        self.assertEqual(command, "acbd")

    def test_ctrl_d_deletes_character_under_cursor(self):
        command = self._feed("abcd\x1b[D\x1b[D\x04\n")

        self.assertEqual(command, "abd")

    def test_ctrl_d_exits_on_empty_line(self):
        event = self.editor.handle_byte(4)

        self.assertIsNotNone(event)
        self.assertTrue(event.should_exit)

    def test_ctrl_c_clears_current_line(self):
        command = self._feed("bad\x03pwd\n")

        self.assertEqual(command, "pwd")
        self.assertIn("^C\r\n$ ", "".join(self.output))

    def test_ctrl_l_clears_screen_and_redraws_line(self):
        self._feed("pwd\x0c")

        output = "".join(self.output)
        self.assertIn("\x1b[H\x1b[2J", output)
        self.assertIn("\r$ pwd", output)

    def test_ctrl_r_reverse_search_submits_match(self):
        self._feed("pwd\n")
        self.editor.start_prompt("$ ")
        self._feed("echo hello\n")
        self.editor.start_prompt("$ ")

        command = self._feed("\x12pw\n")

        self.assertEqual(command, "pwd")
        self.assertIn("(reverse-i-search)`pw': pwd", "".join(self.output))

    def test_ctrl_r_repeats_to_older_match(self):
        self._feed("echo first\n")
        self.editor.start_prompt("$ ")
        self._feed("echo second\n")
        self.editor.start_prompt("$ ")

        command = self._feed("\x12echo\x12\n")

        self.assertEqual(command, "echo first")

    def test_ctrl_r_cancel_restores_original_line(self):
        self._feed("pwd\n")
        self.editor.start_prompt("$ ")

        command = self._feed("ec\x12pw\x03ho hello\n")

        self.assertEqual(command, "echo hello")

    def _feed(self, text):
        command = None
        for byte in text.encode("utf-8"):
            event = self.editor.handle_byte(byte)
            if event is not None and event.command is not None:
                command = event.command
        return command


class SecretLineEditorTests(unittest.TestCase):
    def test_reverse_search_control_cannot_echo_secret_input(self):
        output = []
        editor = SecretLineEditor(output.append, "Enter passphrase: ")
        event = None
        for byte in b"first\x12second\n":
            event = editor.handle_byte(byte)

        self.assertEqual(event.command, "firstsecond")
        self.assertEqual("".join(output), "\r\n")

    def test_secret_input_cannot_be_recalled_after_submission(self):
        output = []
        editor = SecretLineEditor(output.append, "Enter passphrase: ")
        for byte in b"private-value\n":
            editor.handle_byte(byte)
        editor.start_prompt("Enter same passphrase again: ")
        for byte in b"\x1b[A\x10":
            editor.handle_byte(byte)

        self.assertEqual(editor.handle_byte(10).command, "")
        self.assertNotIn("private-value", "".join(output))

    def test_submits_line_without_echoing_secret_text(self):
        output = []
        editor = SecretLineEditor(output.append, "root@host's password: ")

        command = None
        for byte in b"bob123\n":
            event = editor.handle_byte(byte)
            if event is not None and event.command is not None:
                command = event.command

        self.assertEqual(command, "bob123")
        self.assertEqual("".join(output), "\r\n")

    def test_redraw_does_not_expose_secret_text(self):
        output = []
        editor = SecretLineEditor(output.append, "root@host's password: ")

        for byte in b"bob123\x7f":
            editor.handle_byte(byte)

        rendered = "".join(output)
        self.assertNotIn("bob123", rendered)
        self.assertNotIn("bob12", rendered)
        self.assertIn("\rroot@host's password: \x1b[K", rendered)


class InteractiveEditorSelectionTests(unittest.TestCase):
    def test_secret_prompt_overrides_stale_visible_phase(self):
        for name, phase, prompt in (
            ("ssh-keygen", "filename", "Enter passphrase (empty for no passphrase): "),
            ("ssh-keygen", "overwrite", "Enter same passphrase again: "),
            ("passwd", "filename", "(current) UNIX password: "),
            ("sudo", "filename", "[sudo] password for bob: "),
            ("ssh", "host_key_confirmation", "bob@host's password: "),
        ):
            with self.subTest(name=name, phase=phase):
                program = InteractiveProgram(name, name, metadata={"phase": phase})
                output = []
                editor = create_interactive_editor(program, output.append, prompt)
                for byte in b"synthetic-secret\n":
                    editor.handle_byte(byte)
                self.assertIsInstance(editor, SecretLineEditor)
                self.assertEqual("".join(output), "\r\n")

    def test_authentication_secret_phases_use_hidden_input(self):
        for name, phase, prompt in (
            (
                "ssh-keygen",
                "passphrase",
                "Enter passphrase (empty for no passphrase): ",
            ),
            ("ssh-keygen", "passphrase_confirmation", "Enter same passphrase again: "),
            ("passwd", "current_password", "(current) UNIX password: "),
            ("passwd", "new_password", "New password: "),
            ("passwd", "confirm_password", "Retype new password: "),
            ("sudo", "", "[sudo] password for bob: "),
            ("ssh", "", "root@host's password: "),
        ):
            with self.subTest(name=name, phase=phase):
                program = InteractiveProgram(name, name, metadata={"phase": phase})
                self.assertTrue(program_uses_secret_input(program, prompt))
                self.assertIsInstance(
                    create_interactive_editor(program, lambda text: None, prompt),
                    SecretLineEditor,
                )

    def test_explicit_filename_overwrite_and_host_key_phases_remain_visible(self):
        for name, phase, prompt in (
            ("ssh-keygen", "filename", "Enter file (/home/bob/passphrase-key): "),
            ("ssh-keygen", "overwrite", "/home/bob/passphrase-key exists. Overwrite? "),
            ("ssh-keygen", "overwrite", "/home/bob/password exists. Overwrite? "),
            ("ssh", "host_key_confirmation", "Trust password.bsides? "),
            ("bash", "", "Password hint: "),
        ):
            with self.subTest(name=name, phase=phase):
                program = InteractiveProgram(name, name, metadata={"phase": phase})
                self.assertFalse(program_uses_secret_input(program, prompt))
                self.assertIsInstance(
                    create_interactive_editor(program, lambda text: None, prompt),
                    BashLineEditor,
                )


if __name__ == "__main__":
    unittest.main()
