import unittest
from unittest.mock import patch

from langchain_core.callbacks import BaseCallbackHandler

from dreamland.config import SpiritguideSettings
from dreamland.models import CommandHistoryEntry, SessionState, default_shell_frame
from dreamland.spiritguide import (
    ANTHROPIC_SPIRITGUIDE_WARNING,
    ModelSpiritguide,
    SpiritguideContext,
    _message_content_to_text,
    spiritguide_result_from_model_response,
)


class SpiritguideTests(unittest.TestCase):
    def test_provider_receives_spiritguide_evaluation_controls(self):
        with patch("dreamland.spiritguide.build_chat_model") as factory:
            ModelSpiritguide(
                SpiritguideSettings(
                    model="openai:gpt-6-astra",
                    reasoning_effort="max",
                    openai_api="responses",
                    openai_service_tier="default",
                    openai_reasoning_mode="pro",
                ),
                api_key="test-key",
            )
        config = factory.call_args.args[0]
        self.assertEqual(config.model, "gpt-6-astra")
        self.assertEqual(config.reasoning_effort, "max")
        self.assertEqual(config.openai_api, "responses")
        self.assertEqual(config.openai_service_tier, "default")
        self.assertEqual(config.openai_reasoning_mode, "pro")

    def test_structured_response_parses_message_and_world_state_update(self):
        result = spiritguide_result_from_model_response(
            '{"message":"Added a lighthouse lab theme.",'
            '"state_update":{"world_state":'
            '{"world_facts_by_id":{"theme":"lighthouse lab"}}}}'
        )

        self.assertEqual(result.message, "Added a lighthouse lab theme.")
        self.assertEqual(
            result.state_update.world_state,
            {"world_facts_by_id": {"theme": "lighthouse lab"}},
        )
        self.assertTrue(result.metadata["structured_response_accepted"])

    def test_unstructured_response_becomes_operator_message_without_state(self):
        result = spiritguide_result_from_model_response("not json")

        self.assertEqual(result.message, "not json")
        self.assertIsNone(result.state_update.world_state)
        self.assertFalse(result.metadata["structured_response_accepted"])

    def test_message_text_ignores_reasoning_blocks(self):
        message = FakeMessage(
            [
                {
                    "id": "rs_123",
                    "type": "reasoning",
                    "summary": [{"text": "private reasoning"}],
                    "content": [],
                },
                {
                    "type": "text",
                    "text": (
                        '{"message":"Applied.",'
                        '"state_update":{"world_state":{"facts":{"x":1}}}}'
                    ),
                },
            ]
        )

        text = _message_content_to_text(message)
        result = spiritguide_result_from_model_response(text)

        self.assertEqual(result.message, "Applied.")
        self.assertEqual(result.state_update.world_state, {"facts": {"x": 1}})
        self.assertTrue(result.metadata["structured_response_accepted"])

    def test_model_spiritguide_sends_prompt_and_session_context_to_model(self):
        model = FakeModel(
            '{"message":"Tuned.","state_update":{"world_state":{"facts":{"x":1}}}}'
        )
        session = SessionState(
            username="alice",
            login_username="alice",
            langfuse_trace_id="session-a",
            shell_stack=[default_shell_frame(username="alice", cwd="/home/alice")],
            history=[CommandHistoryEntry(cwd="/home/alice", command="whoami")],
            world_state={
                "active_host": "active",
                "hosts_by_name": {
                    "active": {
                        "hostname": "active",
                        "login_banner": "SPIRITGUIDE-SAVED-LOGIN-BANNER\n",
                    },
                    "inactive": {
                        "hostname": "inactive",
                        "command_history": [
                            {
                                "command": "private-history",
                                "stdout": "SPIRITGUIDE-INACTIVE-HISTORY\n",
                            }
                        ],
                        "filesystem": {"private": "SPIRITGUIDE-INACTIVE-FILESYSTEM"},
                    },
                },
            },
        )
        spiritguide = ModelSpiritguide(
            SpiritguideSettings(model="openai:gpt-5.6-terra"),
            model=model,
            system_prompt="system spiritguide prompt",
        )

        result = spiritguide.respond(
            SpiritguideContext(
                instruction="deepen the lab theme",
                session_id="session-a",
                session=session,
            )
        )

        self.assertEqual(result.message, "Tuned.")
        self.assertEqual(result.state_update.world_state, {"facts": {"x": 1}})
        self.assertEqual(len(model.calls), 1)
        messages = model.calls[0]
        self.assertIsInstance(messages[0].content, str)
        self.assertIn("system spiritguide prompt", _message_text(messages[0]))
        self.assertEqual(model.configs, [{}])
        user_prompt = _message_text(messages[1])
        self.assertIn("Operator instruction:", user_prompt)
        self.assertIn("deepen the lab theme", user_prompt)
        self.assertIn("Current Dreamland context:", user_prompt)
        self.assertIn("SPIRITGUIDE-INACTIVE-HISTORY", user_prompt)
        self.assertIn("SPIRITGUIDE-INACTIVE-FILESYSTEM", user_prompt)
        self.assertIn("SPIRITGUIDE-SAVED-LOGIN-BANNER", user_prompt)
        self.assertNotIn("session_id: session-a", user_prompt)
        self.assertNotIn("history:", user_prompt)
        self.assertNotIn("whoami", user_prompt)

    def test_model_spiritguide_marks_anthropic_system_prompt_for_prompt_cache(self):
        model = FakeModel(
            '{"message":"Tuned.","state_update":{"world_state":{"facts":{"x":1}}}}'
        )
        session = SessionState(
            username="alice",
            login_username="alice",
            langfuse_trace_id="session-a",
            shell_stack=[default_shell_frame(username="alice", cwd="/home/alice")],
        )
        spiritguide = ModelSpiritguide(
            SpiritguideSettings(
                model="anthropic:claude-test",
                prompt_cache="automatic",
            ),
            model=model,
            system_prompt="system spiritguide prompt",
        )

        spiritguide.respond(
            SpiritguideContext(
                instruction="deepen the lab theme",
                session_id="session-a",
                session=session,
            )
        )

        system_content = model.calls[0][0].content
        self.assertIsInstance(system_content, list)
        self.assertEqual(system_content[0]["type"], "text")
        self.assertEqual(system_content[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(system_content[0]["text"], "system spiritguide prompt")

    def test_model_spiritguide_warns_when_configured_with_anthropic(self):
        model = FakeModel(
            '{"message":"Tuned.","state_update":{"world_state":{"facts":{"x":1}}}}'
        )

        with self.assertLogs("dreamland.spiritguide", level="WARNING") as logs:
            ModelSpiritguide(
                SpiritguideSettings(model="anthropic:claude-test"),
                model=model,
                system_prompt="system spiritguide prompt",
            )

        self.assertIn(ANTHROPIC_SPIRITGUIDE_WARNING, "\n".join(logs.output))

    def test_model_spiritguide_traces_as_spiritguide_sibling_observation(self):
        model = FakeModel(
            '{"message":"Traced.","state_update":{"world_state":{"facts":{"x":2}}}}'
        )
        callback = RecordingCallback()
        session = SessionState(
            username="alice",
            login_username="alice",
            langfuse_trace_id="session-a",
            shell_stack=[default_shell_frame(username="alice", cwd="/home/alice")],
            history=[CommandHistoryEntry(cwd="/home/alice", command="hostnamectl")],
        )
        spiritguide = ModelSpiritguide(
            SpiritguideSettings(model="openai:fake-model"),
            model=model,
            system_prompt="system spiritguide prompt",
            callbacks=(callback,),
        )

        instruction = "make this a compact router with a long operator note " + (
            "x" * 180
        )

        result = spiritguide.respond(
            SpiritguideContext(
                instruction=instruction,
                session_id="session-a",
                session=session,
                dry_run=True,
            )
        )

        self.assertEqual(result.message, "Traced.")
        self.assertEqual(
            callback.chain_starts[0]["name"],
            "dreamland-spiritguide-agent",
        )
        self.assertEqual(
            callback.chain_starts[0]["inputs"]["instruction"],
            instruction,
        )
        self.assertEqual(
            callback.chain_starts[0]["metadata"]["dreamland.operation"],
            "spiritguide",
        )
        self.assertNotIn(
            "dreamland.spiritguide.instruction",
            callback.chain_starts[0]["metadata"],
        )
        self.assertLessEqual(
            len(
                callback.chain_starts[0]["metadata"][
                    "dreamland.spiritguide.instruction_preview"
                ]
            ),
            163,
        )
        self.assertEqual(
            callback.chain_starts[0]["metadata"][
                "dreamland.spiritguide.instruction_length"
            ],
            str(len(instruction)),
        )
        self.assertEqual(
            len(
                callback.chain_starts[0]["metadata"][
                    "dreamland.spiritguide.instruction_sha256"
                ]
            ),
            16,
        )
        self.assertEqual(
            callback.chain_starts[0]["metadata"]["dreamland.spiritguide.dry_run"],
            "true",
        )
        self.assertIn("callbacks", model.configs[0])
        self.assertEqual(model.configs[0]["run_name"], "spiritguide-call-model")
        self.assertEqual(
            callback.chain_ends[0]["metadata"]["dreamland.spiritguide.patch_keys"],
            ["world_state"],
        )


class FakeModel:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.configs = []

    def invoke(self, messages, config=None):
        self.calls.append(messages)
        self.configs.append(config or {})
        return FakeMessage(self.response)


class FakeMessage:
    def __init__(self, content):
        self.content = content


def _message_text(message):
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(getattr(message, "content", message))


class RecordingCallback(BaseCallbackHandler):
    def __init__(self):
        self.chain_starts = []
        self.chain_ends = []

    def on_chain_start(self, serialized, inputs, **kwargs):
        self.chain_starts.append(
            {
                "name": serialized.get("name"),
                "inputs": inputs,
                "metadata": kwargs.get("metadata") or {},
                "tags": kwargs.get("tags") or [],
            }
        )

    def on_chain_end(self, outputs, **kwargs):
        self.chain_ends.append(outputs)


if __name__ == "__main__":
    unittest.main()
