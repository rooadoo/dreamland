import os
import unittest

from dreamland.agent import LangGraphCommandAgent
from dreamland.backend import AgentBackend
from dreamland.config import load_settings
from dreamland.profile import server_profile

LIVE_TEST_ENABLED = os.environ.get("DREAMLAND_LIVE_OPENAI_TESTS") == "1"
LIVE_TEST_ENABLED = LIVE_TEST_ENABLED or (
    os.environ.get("DREAMLAND_LIVE_PROVIDER_TESTS") == "1"
)


@unittest.skipUnless(
    LIVE_TEST_ENABLED,
    "Set DREAMLAND_LIVE_PROVIDER_TESTS=1 to run live provider tests",
)
class LiveAgentTests(unittest.TestCase):
    def test_live_agent_tracks_created_file_in_directory_listing(self):
        settings = load_settings()
        model_reference = settings.agent.model_reference
        api_key = _api_key_for_provider(settings, model_reference.provider)
        self.assertIsNotNone(
            api_key,
            f"{model_reference.provider} API key is required",
        )

        profile = server_profile()
        agent = LangGraphCommandAgent(
            provider=model_reference.provider,
            model_name=model_reference.model,
            system_prompt_name=settings.agent.system_prompt_name,
            reasoning_effort=settings.agent.reasoning_effort,
            reasoning_summary=settings.agent.reasoning_summary,
            verbosity=settings.agent.verbosity,
            speed=settings.agent.speed,
            api_key=api_key,
        )
        backend = AgentBackend(profile, agent=agent)
        session = backend.create_session("alice")

        first_listing = backend.execute(session, "ls -la")
        touch_result = backend.execute(session, "touch magicfile4124")
        second_listing = backend.execute(session, "ls -la")

        self.assertNotIn("magicfile4124", first_listing.stdout)
        self.assertEqual(touch_result.stdout.strip(), "")
        self.assertIn("magicfile4124", second_listing.stdout)


def _api_key_for_provider(settings, provider):
    if provider == "anthropic":
        return settings.anthropic.api_key
    if provider == "openai":
        return settings.openai.api_key
    if provider == "xai":
        return settings.xai.api_key
    return None


if __name__ == "__main__":
    unittest.main()
