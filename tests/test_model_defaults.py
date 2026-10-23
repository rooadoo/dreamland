import os
import tempfile
import unittest
from configparser import ConfigParser
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from dreamland.config import (
    AgentSettings,
    EnrichmentSettings,
    SpiritguideSettings,
    load_settings,
)
from dreamland.model_defaults import ROLE_MODEL_DEFAULTS


class ModelDefaultTests(unittest.TestCase):
    def test_missing_config_example_and_dataclasses_share_six_role_preferences(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, {}, clear=True),
        ):
            missing = load_settings(Path(tmpdir) / "missing.ini")
            example = load_settings(
                Path(__file__).resolve().parents[1] / "dreamland.example.ini"
            )
        direct = {
            "agent": AgentSettings(),
            "spiritguide": SpiritguideSettings(),
            **{
                f"enrichment.{role}": getattr(EnrichmentSettings(), role)
                for role in (
                    "coordinator",
                    "ambient_coordinator",
                    "expert",
                    "integrator",
                )
            },
        }
        expected = {
            "agent": ("openai:gpt-6.1-sol", "low", "detailed", None, 12800, "fast"),
            "spiritguide": (
                "openai:gpt-6-astra",
                "high",
                "detailed",
                None,
                16384,
                "ultrafast",
            ),
            "enrichment.coordinator": (
                "openai:gpt-6-luna",
                "low",
                None,
                "low",
                2048,
                "fast",
            ),
            "enrichment.ambient_coordinator": (
                "openai:gpt-6-luna",
                "low",
                None,
                "low",
                2048,
                "fast",
            ),
            "enrichment.expert": (
                "openai:gpt-6.1-sol",
                "low",
                None,
                "low",
                2048,
                "fast",
            ),
            "enrichment.integrator": (
                "openai:gpt-6-luna",
                "low",
                None,
                "low",
                4096,
                "fast",
            ),
        }
        fields = (
            "model",
            "reasoning_effort",
            "reasoning_summary",
            "verbosity",
            "max_output_tokens",
            "openai_service_tier",
        )
        for section, values in expected.items():
            for source in (missing, example, direct):
                with self.subTest(section=section, source=type(source).__name__):
                    if isinstance(source, dict):
                        role = source[section]
                    elif section.startswith("enrichment."):
                        role = getattr(source.enrichment, section.partition(".")[2])
                    else:
                        role = getattr(source, section)
                    self.assertEqual(tuple(getattr(role, f) for f in fields), values)
                    self.assertIsNone(role.openai_api)
                    self.assertIsNone(role.openai_reasoning_mode)
                    self.assertIsNone(role.speed)
                    self.assertIsNone(role.prompt_cache)

    def test_serialized_preferences_round_trip_without_runtime_settings(self):
        parser = ConfigParser(interpolation=None)
        for section, defaults in ROLE_MODEL_DEFAULTS.items():
            parser[section] = defaults.as_ini()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, {}, clear=True),
        ):
            path = Path(tmpdir) / "models.ini"
            with path.open("w", encoding="utf-8") as file:
                parser.write(file)
            settings = load_settings(path)
        self.assertEqual(len(parser.sections()), 6)
        for section, defaults in ROLE_MODEL_DEFAULTS.items():
            if section.startswith("enrichment."):
                role = getattr(settings.enrichment, section.partition(".")[2])
            else:
                role = getattr(settings, section)
            self.assertEqual(
                {field: getattr(role, field) for field in asdict(defaults)},
                asdict(defaults),
            )
            self.assertNotIn("system_prompt", parser[section])
            self.assertNotIn("api_key", parser[section])

    def test_other_providers_preserve_spiritguide_effort_and_do_not_inherit_tier(self):
        for provider in ("xai", "anthropic"):
            with (
                self.subTest(provider=provider),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                path = Path(tmpdir) / "test.ini"
                path.write_text(
                    f"[agent]\nmodel={provider}:test\n"
                    f"[spiritguide]\nmodel={provider}:test\n",
                    encoding="utf-8",
                )
                with patch.dict(os.environ, {}, clear=True):
                    settings = load_settings(path)
                self.assertIsNone(settings.agent.openai_service_tier)
                self.assertIsNone(settings.spiritguide.reasoning_effort)
                with patch.dict(
                    os.environ,
                    {"DREAMLAND_SPIRITGUIDE_REASONING_EFFORT": "high"},
                    clear=True,
                ):
                    self.assertEqual(
                        load_settings(path).spiritguide.reasoning_effort, "high"
                    )

    def test_explicit_ini_blanks_clear_new_defaults(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, {}, clear=True),
        ):
            path = Path(tmpdir) / "test.ini"
            path.write_text(
                "[agent]\nopenai_service_tier=\nreasoning_summary=\n"
                "[spiritguide]\nreasoning_effort=\nmax_output_tokens=\nopenai_service_tier=\n",
                encoding="utf-8",
            )
            settings = load_settings(path)
        self.assertIsNone(settings.agent.openai_service_tier)
        self.assertIsNone(settings.agent.reasoning_summary)
        self.assertIsNone(settings.spiritguide.reasoning_effort)
        self.assertIsNone(settings.spiritguide.max_output_tokens)
        self.assertIsNone(settings.spiritguide.openai_service_tier)

    def test_empty_environment_values_clear_ini_controls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.ini"
            path.write_text(
                "[agent]\nopenai_service_tier=flex\nreasoning_summary=detailed\n"
                "[spiritguide]\nreasoning_effort=high\nmax_output_tokens=4096\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "DREAMLAND_OPENAI_SERVICE_TIER": "",
                    "DREAMLAND_REASONING_SUMMARY": "",
                    "DREAMLAND_SPIRITGUIDE_REASONING_EFFORT": "",
                    "DREAMLAND_SPIRITGUIDE_MAX_OUTPUT_TOKENS": "",
                },
                clear=True,
            ):
                settings = load_settings(path)
        self.assertIsNone(settings.agent.openai_service_tier)
        self.assertIsNone(settings.agent.reasoning_summary)
        self.assertIsNone(settings.spiritguide.reasoning_effort)
        self.assertIsNone(settings.spiritguide.max_output_tokens)


if __name__ == "__main__":
    unittest.main()
