import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dreamland.__main__ import (
    _command_agent,
    _compose_fast_lobby_backend,
    _compose_product_runtime_if_enabled,
    _parse_arguments,
    _print_temporary_api_token,
    _redacted_effective_config,
    _ssh_server_config,
    main,
)
from dreamland.backend import AgentBackend
from dreamland.background_agents.event_bus import SessionEventBus
from dreamland.config import ApiSettings, ProductApiSettings, load_settings
from dreamland.profile import server_profile
from dreamland.ssh_auth import SharedPasswordAuthenticator


class MainTests(unittest.TestCase):
    def test_fast_backend_changes_only_emulator_tier_and_shares_lifecycle_services(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ini"
            path.write_text(
                "[ssh]\nentry_mode=lobby\n[agent]\nopenai_service_tier=fast\n"
            )
            with patch.dict(
                os.environ, {"DREAMLAND_SSH_FAST_PASSWORD": "demo-secret"}, clear=True
            ):
                settings = load_settings(path)
        backend = AgentBackend(server_profile(), event_bus=SessionEventBus())
        with patch("dreamland.__main__.LangGraphCommandAgent") as model:
            normal_agent = _command_agent(settings)
            normal_arguments = model.call_args.kwargs.copy()
            fast_backend = _compose_fast_lobby_backend(settings, backend)
            fast_arguments = model.call_args.kwargs.copy()
        self.assertEqual(normal_arguments.pop("openai_service_tier"), "fast")
        self.assertEqual(fast_arguments.pop("openai_service_tier"), "ultrafast")
        self.assertEqual(normal_arguments, fast_arguments)
        self.assertIsNotNone(normal_agent)
        self.assertIsNotNone(fast_backend)
        self.assertIsNot(fast_backend, backend)
        for attribute in (
            "profile",
            "state_profile_runtime",
            "structured_failure_store",
            "event_bus",
        ):
            self.assertIs(getattr(fast_backend, attribute), getattr(backend, attribute))
        self.assertEqual(settings.agent.openai_service_tier, "fast")
        with patch("dreamland.__main__._command_agent") as create_agent:
            for ssh in (
                replace(settings.ssh, entry_mode="direct"),
                replace(settings.ssh, fast_password=None),
            ):
                self.assertIsNone(
                    _compose_fast_lobby_backend(replace(settings, ssh=ssh), backend)
                )
            create_agent.assert_not_called()

    def test_simple_login_alias_is_enabled_only_for_lobby_startup(self):
        authenticators = []

        def create_authenticator(*args, **kwargs):
            authenticator = SharedPasswordAuthenticator(*args, **kwargs)
            authenticators.append(authenticator)
            return authenticator

        for entry_mode in ("lobby", "direct"):
            with (
                self.subTest(entry_mode=entry_mode),
                tempfile.TemporaryDirectory() as tmp,
            ):
                path = Path(tmp) / "dreamland.ini"
                path.write_text(f"[ssh]\nentry_mode = {entry_mode}\n", encoding="utf-8")
                with patch.dict(
                    os.environ,
                    {
                        "DREAMLAND_SSH_PASSWORD": "test-password",
                        "DREAMLAND_SSH_FAST_PASSWORD": "demo-password",
                    },
                    clear=True,
                ):
                    settings = load_settings(path)
                authenticators.clear()

                with (
                    patch(
                        "dreamland.__main__._parse_arguments",
                        return_value=SimpleNamespace(daemon=True),
                    ),
                    patch("dreamland.__main__.load_settings", return_value=settings),
                    patch(
                        "dreamland.__main__.SharedPasswordAuthenticator",
                        side_effect=create_authenticator,
                    ),
                    patch(
                        "dreamland.__main__.configure_console_logging",
                        side_effect=RuntimeError("stop after auth setup"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "stop after auth setup"),
                ):
                    main()
                self.assertEqual(len(authenticators), 1)
                authenticator = authenticators[0]
                self.assertEqual(
                    authenticator.allow_simple_login, entry_mode == "lobby"
                )
                for username in ("dreamland", "dreamland_simple"):
                    self.assertTrue(
                        authenticator.authenticate_password(username, "test-password")
                    )
                self.assertEqual(
                    authenticator.authenticate_password(
                        "other_simple", "test-password"
                    ),
                    entry_mode == "direct",
                )
                for username in ("dreamland_fast", "dreamland_fast_simple"):
                    self.assertEqual(
                        authenticator.authenticate_password(username, "demo-password"),
                        entry_mode == "lobby",
                    )
                    self.assertEqual(
                        authenticator.authenticate_password(username, "test-password"),
                        entry_mode == "direct",
                    )

    def test_console_socket_requires_daemon_and_has_no_default_listener(self):
        with patch.object(sys, "argv", ["dreamland"]):
            self.assertIsNone(_parse_arguments().console_socket)
        with (
            patch.object(
                sys, "argv", ["dreamland", "--console-socket", "/tmp/console.sock"]
            ),
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as caught:
                _parse_arguments()
        self.assertEqual(caught.exception.code, 2)

    def test_daemon_socket_uses_owning_console_and_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "dreamland.ini"
            config.write_text("")
            with patch.dict(
                os.environ, {"DREAMLAND_SSH_PASSWORD": "unit-password"}, clear=True
            ):
                settings = load_settings(config)
            for startup_fails in (False, True):
                with self.subTest(startup_fails=startup_fails), ExitStack() as stack:
                    stack.enter_context(
                        patch("dreamland.__main__.load_settings", return_value=settings)
                    )
                    path = Path(directory) / "runtime/console.sock"
                    stack.enter_context(
                        patch(
                            "dreamland.__main__._parse_arguments",
                            return_value=SimpleNamespace(
                                daemon=True, console_socket=path
                            ),
                        )
                    )
                    mocks = {
                        name: stack.enter_context(patch(f"dreamland.__main__.{name}"))
                        for name in (
                            "LangGraphCommandAgent",
                            "ModelSpiritguide",
                            "EnrichmentAgent",
                            "EnrichmentRuntimeService",
                            "AgentBackend",
                            "DreamlandConsole",
                            "DreamlandSSHServer",
                            "ConsoleSocketServer",
                            "configure_console_logging",
                            "build_langfuse_callbacks",
                        )
                    }
                    control = mocks["ConsoleSocketServer"].return_value
                    if startup_fails:
                        control.start.side_effect = RuntimeError("socket unavailable")
                        with self.assertRaisesRegex(RuntimeError, "socket unavailable"):
                            main()
                        mocks[
                            "DreamlandSSHServer"
                        ].return_value.serve_forever.assert_not_called()
                    else:
                        main()
                        mocks[
                            "DreamlandSSHServer"
                        ].return_value.serve_forever.assert_called_once()
                    mocks["ConsoleSocketServer"].assert_called_once_with(
                        mocks["DreamlandConsole"].return_value, path
                    )
                    control.stop.assert_called_once()
                    mocks[
                        "EnrichmentRuntimeService"
                    ].return_value.stop.assert_called_once()

    def test_disabled_product_api_stays_unimported_in_fresh_interpreter(self):
        script = (
            "import sys\n"
            "from dreamland.__main__ import _compose_product_runtime_if_enabled\n"
            "from dreamland.config import ProductApiSettings\n"
            "from types import SimpleNamespace\n"
            "_compose_product_runtime_if_enabled("
            "SimpleNamespace(product_api=ProductApiSettings()))\n"
            "assert not any(name == 'dreamland.product' or "
            "name.startswith('dreamland.product.') for name in sys.modules)\n"
        )

        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        )

    def test_disabled_product_api_does_not_import_product_modules(self):
        settings = SimpleNamespace(product_api=ProductApiSettings())
        attempted_product_imports: list[str] = []
        real_import = __import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "dreamland.product" or name.startswith("dreamland.product."):
                attempted_product_imports.append(name)
                raise AssertionError(f"unexpected disabled product import: {name}")
            return real_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=guarded_import):
            result = _compose_product_runtime_if_enabled(settings)

        self.assertIsNone(result)
        self.assertEqual(attempted_product_imports, [])

    def test_enabled_product_api_fails_closed_without_runtime_activity(self):
        settings = SimpleNamespace(product_api=ProductApiSettings(enabled=True))

        with (
            patch.object(socket, "socket") as socket_constructor,
            patch.object(threading, "Thread") as thread_constructor,
            self.assertRaisesRegex(
                RuntimeError,
                "Product API support is not available.*No product listener",
            ),
        ):
            _compose_product_runtime_if_enabled(settings)

        socket_constructor.assert_not_called()
        thread_constructor.assert_not_called()

    def test_enabled_product_api_fails_before_logging_or_baseline_startup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[product_api]\nenabled = true\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DREAMLAND_SSH_PASSWORD": "test-password"},
                clear=True,
            ):
                settings = load_settings(path)

        with (
            patch(
                "dreamland.__main__._parse_arguments",
                return_value=SimpleNamespace(daemon=True),
            ),
            patch("dreamland.__main__.load_settings", return_value=settings),
            patch("dreamland.__main__.configure_console_logging") as configure_logging,
            patch("dreamland.__main__.StateProfileStore") as state_profile_store,
            patch("dreamland.__main__.DreamlandSSHServer") as ssh_server,
            patch(
                "dreamland.__main__.SharedPasswordAuthenticator"
            ) as ssh_authenticator,
            self.assertRaisesRegex(
                RuntimeError, "Product API support is not available"
            ),
        ):
            main()

        configure_logging.assert_not_called()
        state_profile_store.assert_not_called()
        ssh_server.assert_not_called()
        ssh_authenticator.assert_not_called()

    def test_startup_rejects_missing_or_blank_ssh_password_before_logging(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("", encoding="utf-8")
            for environment in ({}, {"DREAMLAND_SSH_PASSWORD": " \t"}):
                with self.subTest(environment=environment):
                    with patch.dict(os.environ, environment, clear=True):
                        settings = load_settings(path)
                    with (
                        patch(
                            "dreamland.__main__._parse_arguments",
                            return_value=SimpleNamespace(),
                        ),
                        patch(
                            "dreamland.__main__.load_settings",
                            return_value=settings,
                        ),
                        patch(
                            "dreamland.__main__.configure_console_logging"
                        ) as configure_logging,
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "DREAMLAND_SSH_PASSWORD",
                        ):
                            main()
                    configure_logging.assert_not_called()

    def test_invalid_fast_password_fails_lobby_startup_before_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ini"
            path.write_text("[ssh]\nentry_mode=lobby\n")
            for fast_password in (" \t", "normal-secret"):
                with patch.dict(
                    os.environ,
                    {
                        "DREAMLAND_SSH_PASSWORD": "normal-secret",
                        "DREAMLAND_SSH_FAST_PASSWORD": fast_password,
                    },
                    clear=True,
                ):
                    settings = load_settings(path)
                with (
                    self.subTest(fast_password=fast_password),
                    patch("dreamland.__main__._parse_arguments"),
                    patch("dreamland.__main__.load_settings", return_value=settings),
                    patch("dreamland.__main__.configure_console_logging") as logging,
                    self.assertRaisesRegex(ValueError, "DREAMLAND_SSH_FAST_PASSWORD"),
                ):
                    main()
                logging.assert_not_called()

    def test_effective_config_redacts_ssh_password(self):
        sentinel = "unique-ssh-password-sentinel"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "DREAMLAND_SSH_PASSWORD": sentinel,
                    "DREAMLAND_SSH_FAST_PASSWORD": "demo-sentinel",
                },
                clear=True,
            ):
                settings = load_settings(path)

        effective = _redacted_effective_config(settings)
        ssh_config = effective.get("ssh")
        product_api_config = effective.get("product_api")

        self.assertNotIn(sentinel, repr(settings.ssh))
        self.assertNotIn(sentinel, repr(effective))
        self.assertNotIn("demo-sentinel", repr(effective))
        self.assertNotIn("demo-sentinel", repr(settings.ssh))
        self.assertIsInstance(ssh_config, dict)
        assert isinstance(ssh_config, dict)
        self.assertEqual(ssh_config["password_configured"], True)
        self.assertNotIn("password", ssh_config)
        self.assertTrue(ssh_config["fast_password_configured"])
        self.assertNotIn("fast_password", ssh_config)
        self.assertEqual(product_api_config, {"enabled": False})

    def test_normal_startup_forwards_durable_host_key_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            host_key_path = Path(tmpdir) / "runtime" / "ssh_host_rsa_key"
            path.write_text(
                f"[ssh]\nhost_key_path = {host_key_path}\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DREAMLAND_SSH_PASSWORD": "test-password"},
                clear=True,
            ):
                settings = load_settings(path)

        profile = object()
        config = _ssh_server_config(settings, profile)

        self.assertEqual(config.host_key_path, host_key_path)
        self.assertIs(config.profile, profile)

    def test_temporary_api_token_prints_boot_notice(self):
        settings = SimpleNamespace(
            api=ApiSettings(
                enabled=True,
                token="temporary-token",
                temporary_token=True,
            )
        )
        stream = io.StringIO()

        with redirect_stderr(stream):
            _print_temporary_api_token(settings)

        output = stream.getvalue()
        self.assertIn("temporary bearer token", output)
        self.assertIn("temporary-token", output)
        self.assertIn("DREAMLAND_API_TOKEN", output)

    def test_stable_api_token_does_not_print_boot_notice(self):
        settings = SimpleNamespace(
            api=ApiSettings(
                enabled=True,
                token="stable-token",
                temporary_token=False,
            )
        )
        stream = io.StringIO()

        with redirect_stderr(stream):
            _print_temporary_api_token(settings)

        self.assertEqual(stream.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
