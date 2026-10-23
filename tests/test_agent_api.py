import unittest
import warnings
from unittest.mock import patch

import dreamland
from dreamland.agent import (
    DeterministicCommandAgent,
    LangGraphCommandAgent,
    MockCommandAgent,
)
from dreamland.backend import AgentBackend
from dreamland.models import CommandContext
from dreamland.profile import server_profile


class AgentApiTests(unittest.TestCase):
    def test_deterministic_agent_is_exported_from_package(self):
        self.assertIs(dreamland.DeterministicCommandAgent, DeterministicCommandAgent)

    def test_deprecated_mock_agent_warns_at_call_site(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            agent = MockCommandAgent()

        self.assertIsInstance(agent, DeterministicCommandAgent)
        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, DeprecationWarning)
        self.assertEqual(caught[0].filename, __file__)

    def test_default_backend_uses_deterministic_agent(self):
        backend = AgentBackend(server_profile())

        self.assertIsInstance(backend.agent, DeterministicCommandAgent)

    def test_deterministic_agent_has_stable_output(self):
        agent = DeterministicCommandAgent()
        context = CommandContext(
            username="alice",
            cwd="/home/alice",
            hostname="ubuntu",
            command="uname -a",
            langfuse_trace_id="trace",
        )

        first = agent.respond(context)
        second = agent.respond(context)

        self.assertEqual(first, second)
        self.assertIn("[deterministic-agent]", first.stdout)

    def test_model_controls_are_passed_to_chat_openai(self):
        with (
            patch("langchain_openai.ChatOpenAI") as chat_openai,
            patch.object(LangGraphCommandAgent, "_build_graph", return_value=object()),
        ):
            LangGraphCommandAgent(
                model_name="gpt-test",
                system_prompt_name="system.md",
                reasoning_effort="low",
                verbosity=None,
                max_output_tokens=640,
                max_retries=0,
                api_key="sk-test",
            )

        chat_openai.assert_called_once()
        kwargs = chat_openai.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-test")
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertIsNone(kwargs["verbosity"])
        self.assertEqual(kwargs["max_completion_tokens"], 640)
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertTrue(kwargs["streaming"])
        self.assertTrue(kwargs["stream_usage"])
        self.assertNotIn("callbacks", kwargs)

    def test_token_usage_command_redaction_is_explicit_and_default_compatible(self):
        context = CommandContext(
            username="alice",
            cwd="/home/alice",
            hostname="ubuntu",
            command="private attendee command",
            langfuse_trace_id=None,
        )
        with patch.object(
            LangGraphCommandAgent,
            "_build_graph",
            return_value=object(),
        ):
            ordinary = LangGraphCommandAgent(
                model_name="gpt-test",
                system_prompt_name="system.md",
                model=object(),
            )
            private_attendee = LangGraphCommandAgent(
                model_name="gpt-test",
                system_prompt_name="system.md",
                model=object(),
                redact_token_usage_command=True,
            )

        ordinary_callback = ordinary._callbacks_for_context(context)[0]
        attendee_callback = private_attendee._callbacks_for_context(context)[0]
        self.assertEqual(ordinary_callback.command, "private attendee command")
        self.assertEqual(attendee_callback.command, "[redacted]")

        with self.assertRaisesRegex(TypeError, "must be a boolean"):
            LangGraphCommandAgent(
                model_name="gpt-test",
                system_prompt_name="system.md",
                model=object(),
                redact_token_usage_command=1,
            )
        with self.assertRaisesRegex(ValueError, "max_retries"):
            LangGraphCommandAgent(
                model_name="gpt-test",
                system_prompt_name="system.md",
                model=object(),
                max_retries=-1,
            )

    def test_reasoning_summary_uses_responses_reasoning_payload(self):
        with (
            patch("langchain_openai.ChatOpenAI") as chat_openai,
            patch.object(LangGraphCommandAgent, "_build_graph", return_value=object()),
        ):
            LangGraphCommandAgent(
                model_name="gpt-test",
                system_prompt_name="system.md",
                reasoning_effort="low",
                reasoning_summary="auto",
                api_key="sk-test",
            )

        kwargs = chat_openai.call_args.kwargs
        self.assertEqual(kwargs["reasoning"], {"effort": "low", "summary": "auto"})
        self.assertEqual(kwargs["output_version"], "responses/v1")
        self.assertNotIn("reasoning_effort", kwargs)


if __name__ == "__main__":
    unittest.main()
