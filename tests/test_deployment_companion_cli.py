"""Setup CLI handoff is private and does not alter existing role credentials."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from dreamland.deployment import companion_cli


class CompanionCLITests(unittest.TestCase):
    def test_init_describe_export_and_repeat_preserve_private_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "role"
            arguments = [
                "init",
                "--root",
                str(root),
                "--role",
                "attendee",
                "--project",
                "example",
                "--network",
                "example-companion",
                "--image",
                "sha256:" + "1" * 64,
                "--revision",
                "2" * 40,
            ]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(companion_cli.main(arguments), 0)
            descriptor = json.loads(output.getvalue())["handoff"]
            token = (root / "transport.token").read_bytes()
            for command in (arguments, ["describe", "--root", str(root)]):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(companion_cli.main(command), 0)
                self.assertNotIn(token.decode().strip(), output.getvalue())
                self.assertEqual((root / "transport.token").read_bytes(), token)
            self.assertEqual(json.loads(output.getvalue()), descriptor)
            destination = Path(directory) / "ui.token"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    companion_cli.main(
                        [
                            "export-token",
                            "--root",
                            str(root),
                            "--output",
                            str(destination),
                        ]
                    ),
                    0,
                )
            self.assertEqual(destination.read_bytes(), token)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(
                    companion_cli.main(
                        [
                            "export-token",
                            "--root",
                            str(root),
                            "--output",
                            str(destination),
                        ]
                    ),
                    1,
                )
            self.assertEqual(destination.read_bytes(), token)
            self.assertNotIn(str(destination), errors.getvalue())

    def test_missing_setup_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private-operator-path"
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(
                    companion_cli.main(["describe", "--root", str(root)]), 1
                )
            self.assertNotIn(str(root), errors.getvalue())
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
