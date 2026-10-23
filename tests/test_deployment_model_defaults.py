"""Model adoption is explicit, reviewable, and preserves deployment state."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch

from dreamland.config import load_settings
from dreamland.deployment import configuration, storage
from dreamland.deployment.__main__ import main
from dreamland.model_defaults import ROLE_MODEL_DEFAULTS


class DeploymentModelDefaultsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "deployment"
        document = configuration.default_document()
        storage.initialize(self.root, document, configuration.default_ini(document))

    def legacy_ini(self):
        text = (self.root / "dreamland.ini").read_text()
        text = text.replace("openai:gpt-6.1-sol", "openai:gpt-5.5")
        text = text.replace("openai:gpt-6-astra", "openai:gpt-5.6-sol")
        text = text.replace("max_output_tokens = 12800", "max_output_tokens = 1024")
        text += "\n# Operator annotation\n[operator_notes]\nlabel = custom rehearsal\n"
        (self.root / "dreamland.ini").write_text(text)
        return text.encode()

    def snapshot(self):
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def test_new_deployment_and_empty_local_configuration_have_same_roles(self):
        empty = self.root.parent / "empty.ini"
        empty.write_text("")
        with patch.dict(os.environ, {}, clear=True):
            local = load_settings(empty)
            deployed = load_settings(self.root / "dreamland.ini")
        for section, defaults in ROLE_MODEL_DEFAULTS.items():
            left, right = local, deployed
            for part in section.split("."):
                left, right = getattr(left, part), getattr(right, part)
            for key in defaults.as_ini():
                with self.subTest(section=section, key=key):
                    self.assertEqual(getattr(left, key), getattr(right, key))

    def test_preview_changes_no_files_and_emits_no_credentials(self):
        self.legacy_ini()
        storage.write_secret(self.root, "openai_api_key", "secret-api-canary")
        storage.write_secret(self.root, "ssh_password", "secret-password-canary")
        before = self.snapshot()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["model-defaults", "--root", str(self.root)]), 0)
        report = json.loads(output.getvalue())
        self.assertFalse(report["applied"])
        self.assertIn("agent.model", report["changed_settings"])
        self.assertEqual(before, self.snapshot())
        self.assertNotIn("secret-api-canary", output.getvalue())
        self.assertNotIn("secret-password-canary", output.getvalue())

    def test_apply_backs_up_and_preserves_other_options_worlds_and_secrets(self):
        original = self.legacy_ini()
        storage.write_secret(self.root, "openai_api_key", "secret-api-canary")
        before = self.snapshot()
        report = configuration.update_model_defaults(self.root, apply=True)
        self.assertTrue(report["applied"])
        self.assertTrue(report["restart_required"])
        backup = self.root / report["backup"]
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        current = ConfigParser(interpolation=None)
        current.read(self.root / "dreamland.ini")
        old = ConfigParser(interpolation=None)
        old.read_string(original.decode())
        for section in old.sections():
            if section not in ROLE_MODEL_DEFAULTS:
                self.assertEqual(dict(current[section]), dict(old[section]))
        self.assertEqual(current["agent"]["model"], "openai:gpt-6.1-sol")
        self.assertEqual(current["agent"]["max_output_tokens"], "12800")
        self.assertEqual(current["spiritguide"]["model"], "openai:gpt-6-astra")
        self.assertEqual(current["spiritguide"]["reasoning_effort"], "high")
        self.assertEqual(current["enrichment.expert"]["model"], "openai:gpt-6.1-sol")
        for name, data in before.items():
            if name != "dreamland.ini":
                self.assertEqual((self.root / name).read_bytes(), data)
        configuration.validate(self.root)
        after = self.snapshot()
        second = configuration.update_model_defaults(self.root, apply=True)
        self.assertFalse(second["applied"])
        self.assertEqual(after, self.snapshot())

    def test_compare_and_replace_refuses_concurrent_edit(self):
        original = self.legacy_ini()
        (self.root / "dreamland.ini").write_bytes(original + b"# concurrent edit\n")
        before = self.snapshot()
        with self.assertRaisesRegex(storage.StorageError, "changed"):
            storage.replace_runtime_ini(
                self.root, expected=original, replacement=original
            )
        self.assertEqual(before, self.snapshot())

    def test_interrupted_backup_publication_is_recovered_before_retry(self):
        self.check_interrupted_backup(reinitialize=False)

    def test_init_recovers_interrupted_model_backup_without_changing_ini(self):
        self.check_interrupted_backup(reinitialize=True)

    def check_interrupted_backup(self, *, reinitialize):
        import hashlib

        original = self.legacy_ini()
        name = (
            "dreamland.ini.before-model-defaults-"
            + hashlib.sha256(original).hexdigest()[:16]
        )
        backup = self.root / name
        backup.write_bytes(original)
        backup.chmod(0o600)
        stage = self.root / (".setup-" + "a" * 32)
        os.link(backup, stage)
        if reinitialize:
            document = configuration.default_document()
            report = storage.initialize(
                self.root, document, configuration.default_ini(document)
            )
            self.assertFalse(report["initialized"])
            self.assertEqual((self.root / "dreamland.ini").read_bytes(), original)
        else:
            report = configuration.update_model_defaults(self.root, apply=True)
            self.assertTrue(report["applied"])
        self.assertFalse(stage.exists())
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_nlink, 1)

    def test_malformed_concurrent_edit_is_sanitized(self):
        output, error = io.StringIO(), io.StringIO()
        with (
            patch("dreamland.deployment.configuration.validate"),
            patch(
                "dreamland.deployment.storage.read_managed",
                return_value=b"secret-concurrent-canary",
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            result = main(["model-defaults", "--root", str(self.root), "--apply"])
        self.assertEqual(result, 1)
        self.assertIn("configuration changed", error.getvalue())
        self.assertNotIn(
            "secret-concurrent-canary", output.getvalue() + error.getvalue()
        )

    def test_unsafe_existing_backup_never_overwrites_its_target(self):
        import hashlib

        original = self.legacy_ini()
        name = (
            "dreamland.ini.before-model-defaults-"
            + hashlib.sha256(original).hexdigest()[:16]
        )
        target = self.root.parent / "unrelated"
        target.write_text("preserve me")
        (self.root / name).symlink_to(target)
        with self.assertRaises(storage.StorageError):
            configuration.update_model_defaults(self.root, apply=True)
        self.assertEqual(target.read_text(), "preserve me")
        self.assertEqual((self.root / "dreamland.ini").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
