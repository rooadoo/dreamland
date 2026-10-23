import os
import tempfile
import unittest
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch

from dreamland.config import (
    EnrichmentSettings,
    ProductApiSettings,
    load_settings,
    persist_state_profile_setting,
)


class ConfigTests(unittest.TestCase):
    def test_fast_ssh_password_is_optional_and_environment_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ini"
            path.write_text("[ssh]\nfast_password=ignored-ini-secret\n")
            for value in (None, "demo-secret", "", " \t"):
                environment = (
                    {} if value is None else {"DREAMLAND_SSH_FAST_PASSWORD": value}
                )
                with (
                    self.subTest(value=value),
                    patch.dict(os.environ, environment, clear=True),
                ):
                    settings = load_settings(path)
                self.assertEqual(settings.ssh.fast_password, value or None)
                self.assertNotIn("demo-secret", repr(settings.ssh))
                self.assertNotIn("ignored-ini-secret", repr(settings.ssh))

    def test_evaluation_controls_are_independent_for_all_six_roles(self):
        roles = (
            ("agent", "DREAMLAND", lambda s: s.agent),
            ("spiritguide", "DREAMLAND_SPIRITGUIDE", lambda s: s.spiritguide),
            *(
                (
                    f"enrichment.{role}",
                    f"DREAMLAND_ENRICHMENT_{role.upper()}",
                    lambda s, role=role: getattr(s.enrichment, role),
                )
                for role in (
                    "coordinator",
                    "ambient_coordinator",
                    "expert",
                    "integrator",
                )
            ),
        )
        for section, prefix, select in roles:
            with self.subTest(section=section), tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "test.ini"
                path.write_text(
                    f"[{section}]\nmodel=openai:gpt-6-astra\nreasoning_effort=max\n"
                    "reasoning_summary=\nopenai_api=responses\n"
                    "openai_service_tier=ultrafast\nopenai_reasoning_mode=pro\n",
                    encoding="utf-8",
                )
                with patch.dict(os.environ, {}, clear=True):
                    settings = load_settings(path)
                role = select(settings)
                self.assertEqual(role.reasoning_effort, "max")
                self.assertEqual(role.openai_api, "responses")
                self.assertEqual(role.openai_service_tier, "ultrafast")
                self.assertEqual(role.openai_reasoning_mode, "pro")
                for other_section, _, other_select in roles:
                    if other_section != section:
                        self.assertEqual(
                            other_select(settings).openai_service_tier,
                            "ultrafast" if other_section == "spiritguide" else "fast",
                        )
                with patch.dict(
                    os.environ,
                    {
                        f"{prefix}_OPENAI_API": "chat_completions",
                        f"{prefix}_OPENAI_SERVICE_TIER": "flex",
                        f"{prefix}_OPENAI_REASONING_MODE": " ",
                    },
                    clear=True,
                ):
                    override = select(load_settings(path))
                self.assertEqual(override.openai_api, "chat_completions")
                self.assertEqual(override.openai_service_tier, "flex")
                self.assertIsNone(override.openai_reasoning_mode)

    def test_known_incompatible_openai_settings_fail_at_config_load(self):
        cases = (
            (
                "model=openai:gpt-6-astra\nreasoning_effort=none",
                "reasoning_effort=none",
            ),
            (
                "model=openai:gpt-6.1-sol\nreasoning_effort=none",
                "reasoning_effort=none",
            ),
            (
                "model=openai:gpt-6.1-sol\nreasoning_effort=minimal",
                "minimal",
            ),
            ("model=openai:gpt-5.5\nreasoning_effort=max", "reasoning_effort=max"),
            ("openai_api=chat_completions", "requires blank"),
            ("openai_api=invalid", "openai_api"),
            ("openai_service_tier=invalid", "openai_service_tier"),
            ("openai_reasoning_mode=invalid", "openai_reasoning_mode"),
        )
        for values, error in cases:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "test.ini"
                path.write_text(f"[agent]\n{values}\n", encoding="utf-8")
                with (
                    patch.dict(os.environ, {}, clear=True),
                    self.assertRaisesRegex(ValueError, error),
                ):
                    load_settings(path)

    def test_openai_specific_combinations_do_not_restrict_other_providers(self):
        for model in ("anthropic:claude-test", "xai:grok-test"):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "test.ini"
                path.write_text(
                    f"[agent]\nmodel={model}\nreasoning_effort=max\n"
                    "openai_api=chat_completions\nopenai_reasoning_mode=pro\n"
                    "openai_service_tier=fast\n",
                    encoding="utf-8",
                )
                with patch.dict(os.environ, {}, clear=True):
                    settings = load_settings(path)
                self.assertEqual(settings.agent.model, model)
                self.assertEqual(settings.agent.reasoning_effort, "max")

    def test_product_api_dataclass_defaults_to_disabled(self):
        self.assertFalse(ProductApiSettings().enabled)

    def test_missing_config_defaults_product_api_to_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing.ini"
            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(path)

        self.assertFalse(settings.product_api.enabled)

    def test_product_api_ini_can_explicitly_enable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[product_api]\nenabled = true\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(path)

        self.assertTrue(settings.product_api.enabled)

    def test_product_api_environment_overrides_ini(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[product_api]\nenabled = true\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DREAMLAND_PRODUCT_API_ENABLED": "false"},
                clear=True,
            ):
                settings = load_settings(path)

        self.assertFalse(settings.product_api.enabled)

    def test_invalid_product_api_enabled_value_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[product_api]\nenabled = sometimes\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "Expected a boolean"):
                    load_settings(path)

    def test_example_product_api_is_explicitly_disabled(self):
        parser = ConfigParser()
        path = Path(__file__).resolve().parents[1] / "dreamland.example.ini"
        parser.read(path, encoding="utf-8")

        self.assertFalse(parser.getboolean("product_api", "enabled"))

    def test_enrichment_dataclass_defaults_to_enabled_safe_mode(self):
        settings = EnrichmentSettings()

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.apply_mode, "safe")

    def test_missing_config_defaults_enrichment_to_enabled_safe_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing.ini"
            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(path)

        self.assertTrue(settings.enrichment.enabled)
        self.assertEqual(settings.enrichment.apply_mode, "safe")

    def test_enrichment_ini_can_explicitly_disable_and_use_shadow_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[enrichment]\nenabled = false\napply_mode = shadow\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(path)

        self.assertFalse(settings.enrichment.enabled)
        self.assertEqual(settings.enrichment.apply_mode, "shadow")

    def test_enrichment_environment_overrides_ini_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[enrichment]\nenabled = true\napply_mode = safe\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "DREAMLAND_ENRICHMENT_ENABLED": "false",
                    "DREAMLAND_ENRICHMENT_APPLY_MODE": "shadow",
                },
                clear=True,
            ):
                settings = load_settings(path)

        self.assertFalse(settings.enrichment.enabled)
        self.assertEqual(settings.enrichment.apply_mode, "shadow")

    def test_example_enrichment_defaults_to_enabled_safe_mode(self):
        parser = ConfigParser()
        path = Path(__file__).resolve().parents[1] / "dreamland.example.ini"
        parser.read(path, encoding="utf-8")

        self.assertTrue(parser.getboolean("enrichment", "enabled"))
        self.assertEqual(parser.get("enrichment", "apply_mode"), "safe")

    def test_example_enrichment_roles_do_not_request_reasoning_summaries(self):
        parser = ConfigParser()
        path = Path(__file__).resolve().parents[1] / "dreamland.example.ini"
        parser.read(path, encoding="utf-8")

        for section in (
            "enrichment.coordinator",
            "enrichment.ambient_coordinator",
            "enrichment.expert",
            "enrichment.integrator",
        ):
            with self.subTest(section=section):
                self.assertEqual(parser.get(section, "reasoning_summary"), "")

    def test_loads_settings_from_ini_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "\n".join(
                    [
                        "[langfuse]",
                        "enabled = true",
                        "public_key = pk-test",
                        "secret_key = sk-langfuse",
                        "host = https://langfuse.local",
                        "",
                        "[agent]",
                        "model = xai:gpt-test",
                        "system_prompt = custom.md",
                        "reasoning_effort = high",
                        "reasoning_summary = auto",
                        "verbosity = low",
                        "speed = fast",
                        "prompt_cache = automatic",
                        "max_output_tokens = 768",
                        "",
                        "[spiritguide]",
                        "model = openai:gpt-spirit",
                        "system_prompt = custom-spirit.md",
                        "reasoning_effort = xhigh",
                        "reasoning_summary = detailed",
                        "verbosity = high",
                        "speed = fast",
                        "prompt_cache = off",
                        "max_output_tokens = 2048",
                        "",
                        "[enrichment]",
                        "enabled = true",
                        "apply_mode = safe",
                        "triggers = host_discovered, surface_focus",
                        "idle_delay_ms = 250",
                        "cooldown_ms = 12000",
                        "max_jobs_per_minute = 9",
                        "max_inflight_per_session = 2",
                        "event_log_capacity = 3000",
                        "",
                        "[enrichment.coordinator]",
                        "model = xai:coordinator-test",
                        "system_prompt = custom-coordinator.md",
                        "reasoning_effort = none",
                        "reasoning_summary = concise",
                        "verbosity = low",
                        "speed = fast",
                        "prompt_cache = automatic",
                        "max_output_tokens = 300",
                        "",
                        "[enrichment.ambient_coordinator]",
                        "model = openai:ambient-test",
                        "system_prompt = custom-ambient.md",
                        "reasoning_effort = low",
                        "reasoning_summary = concise",
                        "verbosity = low",
                        "speed = fast",
                        "prompt_cache = automatic",
                        "max_output_tokens = 350",
                        "",
                        "[enrichment.expert]",
                        "model = anthropic:expert-test",
                        "system_prompt = custom-expert.md",
                        "reasoning_effort = medium",
                        "reasoning_summary = auto",
                        "verbosity = medium",
                        "speed = fast",
                        "prompt_cache = off",
                        "max_output_tokens = 700",
                        "",
                        "[enrichment.integrator]",
                        "model = openai:integrator-test",
                        "system_prompt = custom-integrator.md",
                        "reasoning_effort = high",
                        "reasoning_summary = detailed",
                        "verbosity = high",
                        "speed = fast",
                        "prompt_cache = automatic",
                        "max_output_tokens = 1400",
                        "",
                        "[logging]",
                        "enabled = true",
                        "level = debug",
                        "events = lifecycle, commands, tokens, enrichment",
                        "reply_excerpt_chars = 96",
                        "",
                        "[state_profile]",
                        "profile = custom-state",
                        "",
                        "[ssh]",
                        "host = 0.0.0.0",
                        "port = 2022",
                        "host_key_path = runtime/ssh_host_rsa_key",
                        "password = ignored-file-secret",
                        "",
                        "[console]",
                        "log_capacity = 250",
                        "",
                        "[api]",
                        "enabled = true",
                        "host = 127.0.0.2",
                        "port = 8766",
                        "token = api-secret",
                        "mcp_write_tools = true",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "",
                    "XAI_API_KEY": "",
                    "ANTHROPIC_API_KEY": "",
                },
                clear=True,
            ):
                settings = load_settings(path)

        self.assertIsNone(settings.openai.api_key)
        self.assertIsNone(settings.xai.api_key)
        self.assertIsNone(settings.anthropic.api_key)
        self.assertEqual(settings.config_path, path)
        self.assertTrue(settings.langfuse.enabled)
        self.assertEqual(settings.langfuse.public_key, "pk-test")
        self.assertEqual(settings.langfuse.secret_key, "sk-langfuse")
        self.assertEqual(settings.langfuse.host, "https://langfuse.local")
        self.assertEqual(settings.agent.model, "xai:gpt-test")
        self.assertEqual(settings.agent.model_provider, "xai")
        self.assertEqual(settings.agent.model_name, "gpt-test")
        self.assertEqual(settings.agent.system_prompt_name, "custom.md")
        self.assertEqual(settings.agent.reasoning_effort, "high")
        self.assertEqual(settings.agent.reasoning_summary, "auto")
        self.assertEqual(settings.agent.verbosity, "low")
        self.assertEqual(settings.agent.speed, "fast")
        self.assertEqual(settings.agent.prompt_cache, "automatic")
        self.assertEqual(settings.agent.max_output_tokens, 768)
        self.assertEqual(settings.spiritguide.model, "openai:gpt-spirit")
        self.assertEqual(settings.spiritguide.model_provider, "openai")
        self.assertEqual(settings.spiritguide.model_name, "gpt-spirit")
        self.assertEqual(settings.spiritguide.system_prompt_name, "custom-spirit.md")
        self.assertEqual(settings.spiritguide.reasoning_effort, "xhigh")
        self.assertEqual(settings.spiritguide.reasoning_summary, "detailed")
        self.assertEqual(settings.spiritguide.verbosity, "high")
        self.assertEqual(settings.spiritguide.speed, "fast")
        self.assertEqual(settings.spiritguide.prompt_cache, "off")
        self.assertEqual(settings.spiritguide.max_output_tokens, 2048)
        self.assertTrue(settings.enrichment.enabled)
        self.assertEqual(settings.enrichment.apply_mode, "safe")
        self.assertEqual(
            settings.enrichment.triggers,
            frozenset({"host_discovered", "surface_focus"}),
        )
        self.assertEqual(settings.enrichment.idle_delay_ms, 250)
        self.assertEqual(settings.enrichment.cooldown_ms, 12000)
        self.assertEqual(settings.enrichment.max_jobs_per_minute, 9)
        self.assertEqual(settings.enrichment.max_inflight_per_session, 2)
        self.assertEqual(settings.enrichment.event_log_capacity, 3000)
        self.assertEqual(settings.enrichment.coordinator.model_provider, "xai")
        self.assertEqual(settings.enrichment.coordinator.model_name, "coordinator-test")
        self.assertEqual(
            settings.enrichment.coordinator.system_prompt_name,
            "custom-coordinator.md",
        )
        self.assertEqual(settings.enrichment.coordinator.reasoning_effort, "none")
        self.assertEqual(
            settings.enrichment.coordinator.reasoning_summary,
            "concise",
        )
        self.assertEqual(settings.enrichment.coordinator.verbosity, "low")
        self.assertEqual(settings.enrichment.coordinator.speed, "fast")
        self.assertEqual(settings.enrichment.coordinator.prompt_cache, "automatic")
        self.assertEqual(settings.enrichment.coordinator.max_output_tokens, 300)
        self.assertEqual(
            settings.enrichment.ambient_coordinator.model,
            "openai:ambient-test",
        )
        self.assertEqual(
            settings.enrichment.ambient_coordinator.system_prompt_name,
            "custom-ambient.md",
        )
        self.assertEqual(
            settings.enrichment.ambient_coordinator.max_output_tokens,
            350,
        )
        self.assertEqual(settings.enrichment.expert.model_provider, "anthropic")
        self.assertEqual(settings.enrichment.expert.model_name, "expert-test")
        self.assertEqual(
            settings.enrichment.expert.system_prompt_name,
            "custom-expert.md",
        )
        self.assertEqual(settings.enrichment.expert.reasoning_effort, "medium")
        self.assertEqual(settings.enrichment.expert.reasoning_summary, "auto")
        self.assertEqual(settings.enrichment.expert.verbosity, "medium")
        self.assertEqual(settings.enrichment.expert.speed, "fast")
        self.assertEqual(settings.enrichment.expert.prompt_cache, "off")
        self.assertEqual(settings.enrichment.expert.max_output_tokens, 700)
        self.assertEqual(settings.enrichment.integrator.model_provider, "openai")
        self.assertEqual(settings.enrichment.integrator.model_name, "integrator-test")
        self.assertEqual(
            settings.enrichment.integrator.system_prompt_name,
            "custom-integrator.md",
        )
        self.assertEqual(settings.enrichment.integrator.reasoning_effort, "high")
        self.assertEqual(
            settings.enrichment.integrator.reasoning_summary,
            "detailed",
        )
        self.assertEqual(settings.enrichment.integrator.verbosity, "high")
        self.assertEqual(settings.enrichment.integrator.speed, "fast")
        self.assertEqual(settings.enrichment.integrator.prompt_cache, "automatic")
        self.assertEqual(settings.enrichment.integrator.max_output_tokens, 1400)
        self.assertTrue(settings.logging.enabled)
        self.assertEqual(settings.logging.level, "DEBUG")
        self.assertEqual(
            settings.logging.events,
            frozenset({"lifecycle", "commands", "tokens", "enrichment"}),
        )
        self.assertEqual(settings.logging.reply_excerpt_chars, 96)
        self.assertEqual(settings.state_profile.profile, "custom-state")
        self.assertEqual(settings.ssh.host, "0.0.0.0")
        self.assertEqual(settings.ssh.port, 2022)
        self.assertEqual(
            settings.ssh.host_key_path,
            path.parent / "runtime" / "ssh_host_rsa_key",
        )
        self.assertIsNone(settings.ssh.password)
        self.assertEqual(settings.console.log_capacity, 250)
        self.assertTrue(settings.api.enabled)
        self.assertEqual(settings.api.host, "127.0.0.2")
        self.assertEqual(settings.api.port, 8766)
        self.assertEqual(settings.api.token, "api-secret")
        self.assertFalse(settings.api.temporary_token)
        self.assertTrue(settings.api.mcp_enabled)
        self.assertTrue(settings.api.mcp_write_tools)

    def test_environment_overrides_ini_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            environment_host_key_path = Path(tmpdir) / "env-ssh-host-key"
            path.write_text(
                "\n".join(
                    [
                        "[openai]",
                        "api_key = from-file",
                        "",
                        "[xai]",
                        "api_key = xai-file",
                        "",
                        "[anthropic]",
                        "api_key = anthropic-file",
                        "",
                        "[agent]",
                        "model = openai:file-model",
                        "",
                        "[ssh]",
                        "host = 127.0.0.1",
                        "port = 2222",
                        "host_key_path = file-ssh-host-key",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "from-env",
                    "XAI_API_KEY": "xai-env",
                    "ANTHROPIC_API_KEY": "anthropic-env",
                    "LANGFUSE_ENABLED": "true",
                    "LANGFUSE_PUBLIC_KEY": "pk-env",
                    "LANGFUSE_SECRET_KEY": "sk-env",
                    "LANGFUSE_HOST": "https://env.langfuse.local",
                    "DREAMLAND_MODEL": "xai:env-model",
                    "DREAMLAND_SYSTEM_PROMPT": "env.md",
                    "DREAMLAND_REASONING_EFFORT": "low",
                    "DREAMLAND_REASONING_SUMMARY": "detailed",
                    "DREAMLAND_VERBOSITY": "high",
                    "DREAMLAND_SPEED": "fast",
                    "DREAMLAND_PROMPT_CACHE": "automatic",
                    "DREAMLAND_MAX_OUTPUT_TOKENS": "512",
                    "DREAMLAND_SPIRITGUIDE_MODEL": "openai:env-spirit-model",
                    "DREAMLAND_SPIRITGUIDE_SYSTEM_PROMPT": "env-spirit.md",
                    "DREAMLAND_SPIRITGUIDE_REASONING_EFFORT": "medium",
                    "DREAMLAND_SPIRITGUIDE_REASONING_SUMMARY": "concise",
                    "DREAMLAND_SPIRITGUIDE_VERBOSITY": "medium",
                    "DREAMLAND_SPIRITGUIDE_SPEED": "fast",
                    "DREAMLAND_SPIRITGUIDE_PROMPT_CACHE": "off",
                    "DREAMLAND_SPIRITGUIDE_MAX_OUTPUT_TOKENS": "1536",
                    "DREAMLAND_ENRICHMENT_ENABLED": "true",
                    "DREAMLAND_ENRICHMENT_APPLY_MODE": "safe",
                    "DREAMLAND_ENRICHMENT_TRIGGERS": "session_idle,state_materialized",
                    "DREAMLAND_ENRICHMENT_IDLE_DELAY_MS": "400",
                    "DREAMLAND_ENRICHMENT_COOLDOWN_MS": "8000",
                    "DREAMLAND_ENRICHMENT_MAX_JOBS_PER_MINUTE": "11",
                    "DREAMLAND_ENRICHMENT_MAX_INFLIGHT_PER_SESSION": "3",
                    "DREAMLAND_ENRICHMENT_EVENT_LOG_CAPACITY": "7000",
                    "DREAMLAND_ENRICHMENT_COORDINATOR_MODEL": "openai:env-coordinator",
                    "DREAMLAND_ENRICHMENT_COORDINATOR_SYSTEM_PROMPT": "env-coordinator.md",
                    "DREAMLAND_ENRICHMENT_COORDINATOR_REASONING_EFFORT": "low",
                    "DREAMLAND_ENRICHMENT_COORDINATOR_REASONING_SUMMARY": "concise",
                    "DREAMLAND_ENRICHMENT_COORDINATOR_VERBOSITY": "low",
                    "DREAMLAND_ENRICHMENT_COORDINATOR_SPEED": "fast",
                    "DREAMLAND_ENRICHMENT_COORDINATOR_PROMPT_CACHE": "automatic",
                    "DREAMLAND_ENRICHMENT_COORDINATOR_MAX_OUTPUT_TOKENS": "400",
                    "DREAMLAND_ENRICHMENT_AMBIENT_COORDINATOR_MODEL": "openai:env-ambient",
                    "DREAMLAND_ENRICHMENT_AMBIENT_COORDINATOR_SYSTEM_PROMPT": "env-ambient.md",
                    "DREAMLAND_ENRICHMENT_AMBIENT_COORDINATOR_REASONING_EFFORT": "low",
                    "DREAMLAND_ENRICHMENT_AMBIENT_COORDINATOR_REASONING_SUMMARY": "concise",
                    "DREAMLAND_ENRICHMENT_AMBIENT_COORDINATOR_VERBOSITY": "low",
                    "DREAMLAND_ENRICHMENT_AMBIENT_COORDINATOR_SPEED": "fast",
                    "DREAMLAND_ENRICHMENT_AMBIENT_COORDINATOR_PROMPT_CACHE": "automatic",
                    "DREAMLAND_ENRICHMENT_AMBIENT_COORDINATOR_MAX_OUTPUT_TOKENS": "450",
                    "DREAMLAND_ENRICHMENT_EXPERT_MODEL": "xai:env-expert",
                    "DREAMLAND_ENRICHMENT_EXPERT_SYSTEM_PROMPT": "env-expert.md",
                    "DREAMLAND_ENRICHMENT_EXPERT_REASONING_EFFORT": "medium",
                    "DREAMLAND_ENRICHMENT_EXPERT_REASONING_SUMMARY": "auto",
                    "DREAMLAND_ENRICHMENT_EXPERT_VERBOSITY": "medium",
                    "DREAMLAND_ENRICHMENT_EXPERT_SPEED": "fast",
                    "DREAMLAND_ENRICHMENT_EXPERT_PROMPT_CACHE": "off",
                    "DREAMLAND_ENRICHMENT_EXPERT_MAX_OUTPUT_TOKENS": "800",
                    "DREAMLAND_ENRICHMENT_INTEGRATOR_MODEL": "anthropic:env-integrator",
                    "DREAMLAND_ENRICHMENT_INTEGRATOR_SYSTEM_PROMPT": "env-integrator.md",
                    "DREAMLAND_ENRICHMENT_INTEGRATOR_REASONING_EFFORT": "high",
                    "DREAMLAND_ENRICHMENT_INTEGRATOR_REASONING_SUMMARY": "detailed",
                    "DREAMLAND_ENRICHMENT_INTEGRATOR_VERBOSITY": "high",
                    "DREAMLAND_ENRICHMENT_INTEGRATOR_SPEED": "fast",
                    "DREAMLAND_ENRICHMENT_INTEGRATOR_PROMPT_CACHE": "automatic",
                    "DREAMLAND_ENRICHMENT_INTEGRATOR_MAX_OUTPUT_TOKENS": "1600",
                    "DREAMLAND_LOG_ENABLED": "false",
                    "DREAMLAND_LOG_LEVEL": "warning",
                    "DREAMLAND_LOG_EVENTS": "replies,tokens",
                    "DREAMLAND_REPLY_EXCERPT_CHARS": "80",
                    "DREAMLAND_STATE_PROFILE": "env-state",
                    "DREAMLAND_SSH_HOST": "localhost",
                    "DREAMLAND_SSH_PORT": "3022",
                    "DREAMLAND_SSH_HOST_KEY_PATH": str(environment_host_key_path),
                    "DREAMLAND_SSH_PASSWORD": "env-ssh-secret",
                    "DREAMLAND_CONSOLE_LOG_CAPACITY": "500",
                    "DREAMLAND_API_ENABLED": "true",
                    "DREAMLAND_API_HOST": "127.0.0.3",
                    "DREAMLAND_API_PORT": "8767",
                    "DREAMLAND_API_TOKEN": "env-api-secret",
                    "DREAMLAND_API_MCP_WRITE_TOOLS": "true",
                },
                clear=True,
            ):
                settings = load_settings(path)

        self.assertEqual(settings.openai.api_key, "from-env")
        self.assertEqual(settings.xai.api_key, "xai-env")
        self.assertEqual(settings.anthropic.api_key, "anthropic-env")
        self.assertTrue(settings.langfuse.enabled)
        self.assertEqual(settings.langfuse.public_key, "pk-env")
        self.assertEqual(settings.langfuse.secret_key, "sk-env")
        self.assertEqual(settings.langfuse.host, "https://env.langfuse.local")
        self.assertEqual(settings.agent.model, "xai:env-model")
        self.assertEqual(settings.agent.model_provider, "xai")
        self.assertEqual(settings.agent.model_name, "env-model")
        self.assertEqual(settings.agent.system_prompt_name, "env.md")
        self.assertEqual(settings.agent.reasoning_effort, "low")
        self.assertEqual(settings.agent.reasoning_summary, "detailed")
        self.assertEqual(settings.agent.verbosity, "high")
        self.assertEqual(settings.agent.speed, "fast")
        self.assertEqual(settings.agent.prompt_cache, "automatic")
        self.assertEqual(settings.agent.max_output_tokens, 512)
        self.assertEqual(settings.spiritguide.model, "openai:env-spirit-model")
        self.assertEqual(settings.spiritguide.model_provider, "openai")
        self.assertEqual(settings.spiritguide.model_name, "env-spirit-model")
        self.assertEqual(settings.spiritguide.system_prompt_name, "env-spirit.md")
        self.assertEqual(settings.spiritguide.reasoning_effort, "medium")
        self.assertEqual(settings.spiritguide.reasoning_summary, "concise")
        self.assertEqual(settings.spiritguide.verbosity, "medium")
        self.assertEqual(settings.spiritguide.speed, "fast")
        self.assertEqual(settings.spiritguide.prompt_cache, "off")
        self.assertEqual(settings.spiritguide.max_output_tokens, 1536)
        self.assertTrue(settings.enrichment.enabled)
        self.assertEqual(settings.enrichment.apply_mode, "safe")
        self.assertEqual(
            settings.enrichment.triggers,
            frozenset({"session_idle", "state_materialized"}),
        )
        self.assertEqual(settings.enrichment.idle_delay_ms, 400)
        self.assertEqual(settings.enrichment.cooldown_ms, 8000)
        self.assertEqual(settings.enrichment.max_jobs_per_minute, 11)
        self.assertEqual(settings.enrichment.max_inflight_per_session, 3)
        self.assertEqual(settings.enrichment.event_log_capacity, 7000)
        self.assertEqual(
            settings.enrichment.coordinator.model, "openai:env-coordinator"
        )
        self.assertEqual(
            settings.enrichment.coordinator.system_prompt_name,
            "env-coordinator.md",
        )
        self.assertEqual(settings.enrichment.coordinator.reasoning_effort, "low")
        self.assertEqual(
            settings.enrichment.coordinator.reasoning_summary,
            "concise",
        )
        self.assertEqual(settings.enrichment.coordinator.verbosity, "low")
        self.assertEqual(settings.enrichment.coordinator.speed, "fast")
        self.assertEqual(settings.enrichment.coordinator.prompt_cache, "automatic")
        self.assertEqual(settings.enrichment.coordinator.max_output_tokens, 400)
        self.assertEqual(
            settings.enrichment.ambient_coordinator.model,
            "openai:env-ambient",
        )
        self.assertEqual(
            settings.enrichment.ambient_coordinator.system_prompt_name,
            "env-ambient.md",
        )
        self.assertEqual(
            settings.enrichment.ambient_coordinator.max_output_tokens,
            450,
        )
        self.assertEqual(settings.enrichment.expert.model, "xai:env-expert")
        self.assertEqual(
            settings.enrichment.expert.system_prompt_name,
            "env-expert.md",
        )
        self.assertEqual(settings.enrichment.expert.reasoning_effort, "medium")
        self.assertEqual(settings.enrichment.expert.reasoning_summary, "auto")
        self.assertEqual(settings.enrichment.expert.verbosity, "medium")
        self.assertEqual(settings.enrichment.expert.speed, "fast")
        self.assertEqual(settings.enrichment.expert.prompt_cache, "off")
        self.assertEqual(settings.enrichment.expert.max_output_tokens, 800)
        self.assertEqual(
            settings.enrichment.integrator.model,
            "anthropic:env-integrator",
        )
        self.assertEqual(
            settings.enrichment.integrator.system_prompt_name,
            "env-integrator.md",
        )
        self.assertEqual(settings.enrichment.integrator.reasoning_effort, "high")
        self.assertEqual(
            settings.enrichment.integrator.reasoning_summary,
            "detailed",
        )
        self.assertEqual(settings.enrichment.integrator.verbosity, "high")
        self.assertEqual(settings.enrichment.integrator.speed, "fast")
        self.assertEqual(settings.enrichment.integrator.prompt_cache, "automatic")
        self.assertEqual(settings.enrichment.integrator.max_output_tokens, 1600)
        self.assertFalse(settings.logging.enabled)
        self.assertEqual(settings.logging.level, "WARNING")
        self.assertEqual(settings.logging.events, frozenset({"replies", "tokens"}))
        self.assertEqual(settings.logging.reply_excerpt_chars, 80)
        self.assertEqual(settings.state_profile.profile, "env-state")
        self.assertEqual(settings.ssh.host, "localhost")
        self.assertEqual(settings.ssh.port, 3022)
        self.assertEqual(settings.ssh.host_key_path, environment_host_key_path)
        self.assertEqual(settings.ssh.password, "env-ssh-secret")
        self.assertEqual(settings.console.log_capacity, 500)
        self.assertTrue(settings.api.enabled)
        self.assertEqual(settings.api.host, "127.0.0.3")
        self.assertEqual(settings.api.port, 8767)
        self.assertEqual(settings.api.token, "env-api-secret")
        self.assertFalse(settings.api.temporary_token)
        self.assertTrue(settings.api.mcp_enabled)
        self.assertTrue(settings.api.mcp_write_tools)

    def test_default_host_key_path_uses_xdg_state_home(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("", encoding="utf-8")
            state_home = Path(tmpdir) / "state"
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(state_home)},
                clear=True,
            ):
                settings = load_settings(path)

        self.assertEqual(
            settings.ssh.host_key_path,
            state_home / "dreamland" / "ssh_host_rsa_key",
        )

    def test_host_key_environment_path_must_be_absolute_and_nonblank(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("", encoding="utf-8")
            for configured in ("", "relative/ssh_host_rsa_key"):
                with self.subTest(configured=configured):
                    with patch.dict(
                        os.environ,
                        {"DREAMLAND_SSH_HOST_KEY_PATH": configured},
                        clear=True,
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "DREAMLAND_SSH_HOST_KEY_PATH",
                        ):
                            load_settings(path)

    def test_blank_ini_host_key_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[ssh]\nhost_key_path =\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "host_key_path"):
                    load_settings(path)

    def test_provider_api_keys_are_ignored_in_ini_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "\n".join(
                    [
                        "[openai]",
                        "api_key = from-file",
                        "",
                        "[xai]",
                        "api_key = xai-file",
                        "",
                        "[anthropic]",
                        "api_key = anthropic-file",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "",
                    "XAI_API_KEY": "",
                    "ANTHROPIC_API_KEY": "",
                },
                clear=True,
            ):
                settings = load_settings(path)

        self.assertIsNone(settings.openai.api_key)
        self.assertIsNone(settings.xai.api_key)
        self.assertIsNone(settings.anthropic.api_key)

    def test_placeholder_langfuse_keys_are_treated_as_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "\n".join(
                    [
                        "[langfuse]",
                        "enabled = false",
                        "public_key = replace-me",
                        "secret_key = replace-me",
                    ]
                ),
                encoding="utf-8",
            )

            settings = load_settings(path)

        self.assertIsNone(settings.langfuse.public_key)
        self.assertIsNone(settings.langfuse.secret_key)

    def test_api_defaults_to_disabled_without_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[api]\ntoken = replace-me\n", encoding="utf-8")

            settings = load_settings(path)

        self.assertFalse(settings.api.enabled)
        self.assertEqual(settings.api.host, "127.0.0.1")
        self.assertEqual(settings.api.port, 8765)
        self.assertIsNone(settings.api.token)
        self.assertFalse(settings.api.temporary_token)
        self.assertFalse(settings.api.mcp_enabled)
        self.assertFalse(settings.api.mcp_write_tools)

    def test_enabled_api_without_token_generates_temporary_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[api]\nenabled = true\n", encoding="utf-8")

            settings = load_settings(path)

        self.assertTrue(settings.api.enabled)
        self.assertTrue(settings.api.mcp_enabled)
        self.assertIsNotNone(settings.api.token)
        self.assertTrue(settings.api.temporary_token)
        self.assertFalse(settings.api.mcp_write_tools)

    def test_mcp_write_tools_require_api_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "\n".join(
                    [
                        "[api]",
                        "enabled = false",
                        "token = api-secret",
                        "mcp_write_tools = true",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "MCP write tools require API"):
                load_settings(path)

    def test_enabled_api_requires_loopback_host(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "\n".join(
                    [
                        "[api]",
                        "enabled = true",
                        "host = 0.0.0.0",
                        "token = api-secret",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "loopback"):
                load_settings(path)

    def test_enabled_api_accepts_localhost_host(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "\n".join(
                    [
                        "[api]",
                        "enabled = true",
                        "host = localhost",
                        "token = api-secret",
                    ]
                ),
                encoding="utf-8",
            )

            settings = load_settings(path)

        self.assertEqual(settings.api.host, "localhost")

    def test_persist_state_profile_setting_updates_ini_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[agent]\nmodel = openai:gpt-test\n", encoding="utf-8")

            persist_state_profile_setting(path, "manual-system")
            settings = load_settings(path)
            persist_state_profile_setting(path, None)
        cleared = load_settings(path)

        self.assertEqual(settings.state_profile.profile, "manual-system")
        self.assertEqual(cleared.state_profile.profile, "default")

    def test_invalid_langfuse_enabled_value_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[langfuse]\nenabled = maybe\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_settings(path)

    def test_optional_model_controls_use_role_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[agent]\nmodel = openai:gpt-test\n", encoding="utf-8")

            settings = load_settings(path)

        self.assertEqual(settings.agent.reasoning_effort, "low")
        self.assertEqual(settings.agent.reasoning_summary, "detailed")
        self.assertIsNone(settings.agent.verbosity)
        self.assertIsNone(settings.agent.speed)
        self.assertIsNone(settings.agent.prompt_cache)
        self.assertEqual(settings.agent.max_output_tokens, 12800)
        self.assertEqual(settings.agent.openai_service_tier, "fast")
        self.assertEqual(settings.spiritguide.model, "openai:gpt-6-astra")
        self.assertEqual(settings.spiritguide.model_provider, "openai")
        self.assertEqual(settings.spiritguide.model_name, "gpt-6-astra")
        self.assertEqual(settings.spiritguide.reasoning_effort, "high")
        self.assertEqual(settings.spiritguide.reasoning_summary, "detailed")
        self.assertEqual(settings.spiritguide.max_output_tokens, 16384)
        self.assertEqual(settings.spiritguide.openai_service_tier, "ultrafast")
        self.assertTrue(settings.enrichment.enabled)
        self.assertEqual(settings.enrichment.apply_mode, "safe")
        self.assertEqual(
            settings.enrichment.triggers,
            frozenset(
                {
                    "host_discovered",
                    "active_host_changed",
                    "host_presence_bootstrap",
                    "presence_probe",
                    "subject_first_touch",
                    "state_materialized",
                    "materialization_followthrough",
                    "surface_focus",
                    "interaction_pulse",
                    "session_idle",
                }
            ),
        )
        self.assertEqual(settings.enrichment.idle_delay_ms, 5000)
        self.assertEqual(settings.enrichment.cooldown_ms, 30000)
        self.assertEqual(settings.enrichment.max_jobs_per_minute, 6)
        self.assertEqual(settings.enrichment.max_inflight_per_session, 3)
        self.assertEqual(settings.enrichment.event_log_capacity, 10000)
        self.assertEqual(
            settings.enrichment.coordinator.model,
            "openai:gpt-6-luna",
        )
        self.assertEqual(
            settings.enrichment.coordinator.system_prompt_name,
            "enrichment_coordinator.md",
        )
        self.assertEqual(settings.enrichment.coordinator.reasoning_effort, "low")
        self.assertIsNone(settings.enrichment.coordinator.reasoning_summary)
        self.assertEqual(settings.enrichment.coordinator.verbosity, "low")
        self.assertEqual(settings.enrichment.coordinator.max_output_tokens, 2048)
        self.assertEqual(
            settings.enrichment.ambient_coordinator.system_prompt_name,
            "enrichment_ambient_coordinator.md",
        )
        self.assertEqual(
            settings.enrichment.ambient_coordinator.max_output_tokens,
            2048,
        )
        self.assertEqual(
            settings.enrichment.expert.system_prompt_name,
            "enrichment_expert.md",
        )
        self.assertEqual(settings.enrichment.expert.max_output_tokens, 2048)
        self.assertEqual(
            settings.enrichment.integrator.system_prompt_name,
            "enrichment_integrator.md",
        )
        self.assertEqual(settings.enrichment.integrator.max_output_tokens, 4096)

    def test_zero_max_output_tokens_disables_safeguard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[agent]\nmax_output_tokens = 0\n",
                encoding="utf-8",
            )

            settings = load_settings(path)

        self.assertIsNone(settings.agent.max_output_tokens)

    def test_blank_max_output_tokens_disables_safeguard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[agent]\nmax_output_tokens =\n",
                encoding="utf-8",
            )

            settings = load_settings(path)

        self.assertIsNone(settings.agent.max_output_tokens)

    def test_invalid_logging_event_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[logging]\nevents = commands,unknown\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "logging events"):
                load_settings(path)

    def test_invalid_enrichment_apply_mode_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[enrichment]\napply_mode = overwrite\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "enrichment apply_mode"):
                load_settings(path)

    def test_invalid_enrichment_trigger_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[enrichment]\ntriggers = session_idle,unknown\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "enrichment triggers"):
                load_settings(path)

    def test_enrichment_rate_and_inflight_limits_must_be_positive(self):
        values = (
            "max_jobs_per_minute",
            "max_inflight_per_session",
            "event_log_capacity",
        )
        for option in values:
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "dreamland.ini"
                path.write_text(
                    f"[enrichment]\n{option} = 0\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, "greater than zero"):
                    load_settings(path)

    def test_enrichment_delays_allow_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text(
                "[enrichment]\nidle_delay_ms = 0\ncooldown_ms = 0\n",
                encoding="utf-8",
            )

            settings = load_settings(path)

        self.assertEqual(settings.enrichment.idle_delay_ms, 0)
        self.assertEqual(settings.enrichment.cooldown_ms, 0)

    def test_invalid_reasoning_effort_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[agent]\nreasoning_effort = extreme\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "reasoning_effort"):
                load_settings(path)

    def test_invalid_model_provider_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[agent]\nmodel = unknown:test\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "provider"):
                load_settings(path)

    def test_unqualified_model_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[agent]\nmodel = gpt-test\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "provider:model"):
                load_settings(path)

    def test_invalid_reasoning_summary_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[agent]\nreasoning_summary = full\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "reasoning_summary"):
                load_settings(path)

    def test_invalid_verbosity_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[agent]\nverbosity = verbose\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "verbosity"):
                load_settings(path)

    def test_invalid_speed_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[agent]\nspeed = turbo\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "speed"):
                load_settings(path)

    def test_invalid_prompt_cache_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[agent]\nprompt_cache = forever\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "prompt_cache"):
                load_settings(path)

    def test_console_log_capacity_must_be_positive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dreamland.ini"
            path.write_text("[console]\nlog_capacity = 0\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "greater than zero"):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
