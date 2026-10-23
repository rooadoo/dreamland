"""Deployment boundaries, startup handoff and read-only configuration checks."""

from __future__ import annotations

import contextlib
import getpass
import io
import json
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from dreamland.deployment import configuration, storage
from dreamland.deployment.__main__ import main


class DeploymentConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "deployment"
        self.document = configuration.default_document()
        storage.initialize(
            self.root, self.document, configuration.default_ini(self.document)
        )

    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main([*arguments, "--root", str(self.root)])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_offline_validation_ignores_ambient_lab_keys_and_overrides(self) -> None:
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        with patch.dict(
            os.environ,
            {
                "DREAMLAND_API_ENABLED": "true",
                "DREAMLAND_SSH_HOST": "lab.invalid",
                "DREAMLAND_PRODUCT_API_ENABLED": "true",
                "OPENAI_API_KEY": "lab-canary",
                "LANGFUSE_ENABLED": "true",
            },
        ):
            code, output, error = self.invoke("validate")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "lab-canary")
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(report["world_count"], 3)
        self.assertFalse(report["public_release_ready"])
        self.assertFalse(report["backend_http_implemented"])
        self.assertFalse(report["ssh_bans_implemented"])
        self.assertNotIn("web_and_bans_implemented", report)
        self.assertFalse(report["credentials_checked"])
        self.assertNotIn("lab-canary", output + error)
        self.assertEqual(
            before,
            {
                path: path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            },
        )

    def test_backend_policy_has_only_backend_services_and_access(self) -> None:
        self.assertEqual(self.document["schema_version"], 2)
        self.assertEqual(self.document["requested_services"], {"ssh": True})
        self.assertEqual(
            self.document["planned_access"],
            {"ssh_bans": True, "forge_private_by_default": True},
        )
        document = configuration.default_document(
            ssh=False, ssh_entry_mode="direct", ssh_bans=False
        )
        self.assertEqual(document["requested_services"], {"ssh": False})
        self.assertEqual(document["ssh_entry_mode"], "direct")
        self.assertFalse(document["planned_access"]["ssh_bans"])

    def test_conference_policy_keeps_forge_private_and_disables_ssh_bans(self) -> None:
        document = configuration.default_document("conference")
        access = document["planned_access"]
        self.assertTrue(access["forge_private_by_default"])
        self.assertFalse(access["ssh_bans"])
        document["planned_access"]["forge_private_by_default"] = False
        with self.assertRaises(configuration.DeploymentError):
            configuration.validate_document(document)
        with self.assertRaises(configuration.DeploymentError):
            configuration.default_document("conference", ssh_bans=True)

    def test_web_cli_switches_are_rejected_before_setup(self) -> None:
        for switch in (
            "--attendee-web",
            "--no-attendee-web",
            "--operator-web",
            "--no-operator-web",
            "--attendee-web-bans",
            "--no-attendee-web-bans",
        ):
            with (
                self.subTest(switch=switch),
                patch("dreamland.deployment.storage.initialize") as initialize,
            ):
                with self.assertRaises(SystemExit) as raised:
                    self.invoke("init", switch)
                self.assertEqual(raised.exception.code, 2)
                initialize.assert_not_called()

    def test_web_policy_fields_are_not_accepted_in_backend_document(self) -> None:
        for section, key in (
            ("requested_services", "attendee_web"),
            ("requested_services", "operator_web"),
            ("planned_access", "attendee_basic_auth"),
            ("planned_access", "operator_basic_auth"),
            ("planned_access", "attendee_web_bans"),
        ):
            with self.subTest(key=key):
                document = configuration.default_document()
                document[section][key] = True
                with self.assertRaises(configuration.DeploymentError):
                    configuration.validate_document(document)

    def test_legacy_setup_requires_migration_without_mutating_saved_state(self) -> None:
        document = configuration.default_document()
        document["schema_version"] = 1
        document["requested_services"].update(
            {"attendee_web": True, "operator_web": True}
        )
        document["planned_access"].update(
            {
                "attendee_basic_auth": True,
                "operator_basic_auth": True,
                "attendee_web_bans": True,
            }
        )
        (self.root / "deployment.json").write_text(json.dumps(document))
        storage.write_secret(self.root, "openai_api_key", "do-not-print-this")
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        for command in ("init", "validate", "show", "configure-ssh", "serve-ssh"):
            with self.subTest(command=command), patch("os.execve") as execute:
                code, output, error = self.invoke(command)
                self.assertEqual(code, 1)
                self.assertIn("requires migration", error)
                self.assertNotIn("do-not-print-this", output + error)
                execute.assert_not_called()
                self.assertEqual(
                    before,
                    {
                        path: path.read_bytes()
                        for path in self.root.rglob("*")
                        if path.is_file()
                    },
                )

    def test_second_init_cannot_silently_promote_or_change_services(self) -> None:
        code, output, error = self.invoke("init", "--profile", "conference", "--no-ssh")
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(output)["initialized"])
        document, _ = storage.read_configuration(self.root)
        self.assertEqual(document, self.document)

    def test_repeated_init_reports_deliberately_removed_configuration(self) -> None:
        for relative in ("dreamland.ini", "deployment.json"):
            with self.subTest(relative=relative):
                (self.root / relative).unlink()
                code, output, error = self.invoke("init")
                self.assertEqual(code, 0, error)
                self.assertIn(relative, json.loads(output)["absent"])
                self.assertFalse((self.root / relative).exists())

    def test_runtime_settings_cannot_enable_private_api_or_other_provider(self) -> None:
        original = (self.root / "dreamland.ini").read_text()
        for old, new in (
            ("[api]\nenabled = false", "[api]\nenabled = true"),
            ("[product_api]\nenabled = false", "[product_api]\nenabled = true"),
            ("[langfuse]\nenabled = false", "[langfuse]\nenabled = true"),
            ("openai:gpt-6.1-sol", "xai:grok-4.3"),
            ("host = 0.0.0.0", "host = 127.0.0.1"),
        ):
            with self.subTest(new=new):
                (self.root / "dreamland.ini").write_text(original.replace(old, new))
                code, output, error = self.invoke("validate")
                self.assertEqual(code, 1)
                self.assertNotIn(new, output + error)

    def test_invalid_json_cannot_echo_credentials(self) -> None:
        (self.root / "deployment.json").write_text('{"secret":"do-not-print-this"')
        code, output, error = self.invoke("show")
        self.assertEqual(code, 1)
        self.assertNotIn("do-not-print-this", output + error)

    def test_invalid_ini_cannot_echo_credentials(self) -> None:
        for text in (
            "do-not-print-this",
            "[ssh]\npassword = secret\npassword = do-not-print-this",
        ):
            with self.subTest(text=text):
                (self.root / "dreamland.ini").write_text(text)
                code, output, error = self.invoke("validate")
                self.assertEqual(code, 1)
                self.assertNotIn("do-not-print-this", output + error)

    def test_symlinked_profiles_directory_is_rejected(self) -> None:
        directory = self.root / "state_profiles"
        saved = self.root / "saved_profiles"
        directory.rename(saved)
        directory.symlink_to(saved, target_is_directory=True)
        code, _, _ = self.invoke("validate")
        self.assertEqual(code, 1)

    def test_deleted_initial_profile_cannot_fall_back_to_packaged_copy(self) -> None:
        catalog_path = self.root / "lobby/catalog.json"
        catalog = json.loads(catalog_path.read_text())
        catalog["worlds"] = catalog["worlds"][1:]
        catalog_path.write_text(json.dumps(catalog))
        (self.root / "state_profiles/lobby-supermarket.json").unlink()
        code, _, _ = self.invoke("validate")
        self.assertEqual(code, 1)

    def test_missing_catalog_is_not_recreated(self) -> None:
        (self.root / "lobby/catalog.json").unlink()
        code, _, _ = self.invoke("validate")
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "lobby/catalog.json").exists())

    def test_serve_requires_stored_credentials(self) -> None:
        with patch("os.execve") as execute:
            code, _, _ = self.invoke("serve-ssh")
        self.assertEqual(code, 1)
        execute.assert_not_called()

    def test_serve_handoff_uses_only_deployment_credentials(self) -> None:
        storage.write_secret(self.root, "ssh_password", "deployment-password")
        storage.write_secret(self.root, "openai_api_key", "deployment-key")
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "lab-key",
                    "XAI_API_KEY": "lab-xai-key",
                    "OPENAI_BASE_URL": "https://lab.invalid",
                    "LANGFUSE_SECRET_KEY": "lab-trace",
                    "DREAMLAND_API_ENABLED": "true",
                    "HTTPS_PROXY": "http://lab.invalid",
                },
            ),
            patch("os.execve") as execute,
            patch("os.chdir") as chdir,
            patch("os.umask") as umask,
        ):
            code, output, error = self.invoke("serve-ssh")
        self.assertEqual(code, 0, error)
        executable, argv, environment = execute.call_args.args
        self.assertEqual(argv, [executable, "-m", "dreamland", "--daemon"])
        self.assertEqual(environment["OPENAI_API_KEY"], "deployment-key")
        self.assertEqual(environment["DREAMLAND_SSH_PASSWORD"], "deployment-password")
        self.assertEqual(environment["LANGFUSE_ENABLED"], "false")
        for key in (
            "XAI_API_KEY",
            "OPENAI_BASE_URL",
            "LANGFUSE_SECRET_KEY",
            "DREAMLAND_API_ENABLED",
            "HTTPS_PROXY",
        ):
            self.assertNotIn(key, environment)
        self.assertNotIn("deployment-key", output + error + str(argv))
        chdir.assert_called_once_with(self.root)
        umask.assert_called_once_with(0o077)

    def test_disabled_ssh_fails_before_exec(self) -> None:
        document = configuration.default_document(ssh=False)
        (self.root / "deployment.json").write_text(json.dumps(document))
        with patch("os.execve") as execute:
            code, _, error = self.invoke("serve-ssh")
        self.assertEqual(code, 1)
        self.assertIn("disabled", error)
        execute.assert_not_called()

    def test_private_console_path_is_forwarded_to_daemon(self) -> None:
        storage.write_secret(self.root, "openai_api_key", "deployment-key")
        storage.write_secret(self.root, "ssh_password", "deployment-password")
        path = self.root.parent / "runtime" / "console.sock"
        with patch("os.execve") as execute, patch("os.chdir"), patch("os.umask"):
            code, _, error = self.invoke("serve-ssh", "--console-socket", str(path))
        self.assertEqual(code, 0, error)
        argv = execute.call_args.args[1]
        self.assertEqual(argv[-2:], ["--console-socket", str(path)])

    def test_console_subcommand_never_loads_deployment_credentials(self) -> None:
        with (
            patch("dreamland.console_client.main", return_value=0) as client,
            patch("dreamland.deployment.configuration.validate") as validate,
            patch("dreamland.deployment.storage.read_secret") as read_secret,
            patch("os.execve") as execute,
        ):
            result = main(
                ["console", "--socket", "/tmp/control.sock", "--command", "sessions"]
            )
        self.assertEqual(result, 0)
        client.assert_called_once_with(
            ["--socket", "/tmp/control.sock", "--command=sessions"]
        )
        validate.assert_not_called()
        read_secret.assert_not_called()
        execute.assert_not_called()

    def test_secret_prompt_requires_terminal(self) -> None:
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("getpass.getpass") as prompt,
        ):
            code, _, error = self.invoke("configure-ssh")
        self.assertEqual(code, 1)
        self.assertIn("interactive terminal", error)
        prompt.assert_not_called()

    def test_secret_confirmation_failure_preserves_existing_pair(self) -> None:
        storage.write_secret(self.root, "ssh_password", "previous-password")
        storage.write_secret(self.root, "openai_api_key", "previous-key")
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch(
                "getpass.getpass", side_effect=["new-password", "different-password"]
            ),
        ):
            code, output, error = self.invoke("configure-ssh")
        self.assertEqual(code, 1)
        self.assertNotIn("new-password", output + error)
        self.assertEqual(
            storage.read_secret(self.root, "ssh_password"), "previous-password"
        )
        self.assertEqual(
            storage.read_secret(self.root, "openai_api_key"), "previous-key"
        )

    def test_secret_prompt_cannot_fall_back_to_echo(self) -> None:
        def unavailable_terminal(prompt: str) -> str:
            warnings.warn("terminal unavailable", getpass.GetPassWarning, stacklevel=2)
            self.fail("prompt must stop before echoed input can be read")

        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("getpass.getpass", side_effect=unavailable_terminal),
        ):
            code, _, _ = self.invoke("configure-ssh")
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "secrets/ssh_password").exists())

    def test_invalid_key_preserves_both_previous_credentials(self) -> None:
        storage.write_secret(self.root, "ssh_password", "previous-password")
        storage.write_secret(self.root, "openai_api_key", "previous-key")
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch(
                "getpass.getpass",
                side_effect=["new-password", "new-password", "bad\nkey"],
            ),
        ):
            code, _, _ = self.invoke("configure-ssh")
        self.assertEqual(code, 1)
        self.assertEqual(
            storage.read_secret(self.root, "ssh_password"), "previous-password"
        )
        self.assertEqual(
            storage.read_secret(self.root, "openai_api_key"), "previous-key"
        )


if __name__ == "__main__":
    unittest.main()
