import json
import tempfile
import unittest
from pathlib import Path

from dreamland.agent_structured import (
    STRUCTURED_FAILURE_CLASS_METADATA,
    command_result_from_model_response,
)
from dreamland.backend import AgentBackend
from dreamland.profile import server_profile
from dreamland.structured_failures import (
    STRUCTURED_FAILURE_CAPSULE_ID_METADATA,
    STRUCTURED_FAILURE_CAPSULE_PATH_METADATA,
    STRUCTURED_RAW_RESPONSE_METADATA,
    StructuredFailureStore,
    replay_structured_failure_parser,
    should_capture_structured_failure,
)


class MalformedStructuredAgent:
    handles_builtins = True

    def respond(self, context):
        return command_result_from_model_response('{"stdout": "unterminated"')


class ValidJsonStdoutAgent:
    handles_builtins = True

    def respond(self, context):
        schema = {
            "stdout": "literal field",
            "stderr": "literal field",
            "exit_status": "literal field",
            "state_update": {"type": "literal schema field"},
        }
        return command_result_from_model_response(
            json.dumps(
                {
                    "stdout": f"{json.dumps(schema, sort_keys=True)}\n",
                    "stderr": "",
                    "exit_status": 0,
                    "state_update": {},
                }
            )
        )


class StructuredFailureTests(unittest.TestCase):
    def test_parser_accepts_whole_json_markdown_fence(self):
        result = command_result_from_model_response(
            "\n```json\n"
            + json.dumps(
                {
                    "stdout": "ok\n",
                    "stderr": "",
                    "exit_status": 0,
                    "state_update": {},
                },
                indent=2,
            )
            + "\n```\n"
        )

        self.assertEqual(result.stdout, "ok\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.exit_status, 0)
        self.assertFalse(should_capture_structured_failure(result))

    def test_parser_accepts_trailing_structured_json_after_markdown(self):
        result = command_result_from_model_response(
            "```\n"
            "total 12\n"
            "drwxr-xr-x  2 root root 4096 Jan 1 00:00 .\n"
            "```\n\n"
            "I'll provide the JSON response.\n\n"
            + json.dumps(
                {
                    "stdout": "total 12\n",
                    "stderr": "",
                    "exit_status": 0,
                    "state_update": {},
                },
                indent=2,
            )
        )

        self.assertEqual(result.stdout, "total 12\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.exit_status, 0)
        self.assertFalse(should_capture_structured_failure(result))

    def test_parser_keeps_trailing_non_envelope_json_as_stdout(self):
        result = command_result_from_model_response(
            "terminal output\n"
            + json.dumps(
                {
                    "status": "ok",
                    "state_update": "literal text",
                }
            )
        )

        self.assertIn("terminal output", result.stdout)
        self.assertIn('"status": "ok"', result.stdout)
        self.assertFalse(should_capture_structured_failure(result))

    def test_fenced_structured_failure_preserves_raw_provider_response(self):
        text = '```json\n{"stdout": "unterminated"\n```'

        result = command_result_from_model_response(text)

        self.assertTrue(should_capture_structured_failure(result))
        self.assertEqual(
            result.metadata[STRUCTURED_RAW_RESPONSE_METADATA],
            text,
        )
        self.assertEqual(
            result.metadata[STRUCTURED_FAILURE_CLASS_METADATA],
            "invalid_json",
        )

    def test_valid_stdout_can_contain_json_with_structured_field_names(self):
        result = ValidJsonStdoutAgent().respond(None)

        self.assertIn('"stdout": "literal field"', result.stdout)
        self.assertIn('"state_update": {"type": "literal schema field"}', result.stdout)
        self.assertFalse(should_capture_structured_failure(result))
        self.assertNotEqual(result.metadata.get("structured_response_accepted"), False)

    def test_backend_captures_structured_failure_capsule_before_state_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StructuredFailureStore(Path(directory))
            backend = AgentBackend(
                server_profile(),
                agent=MalformedStructuredAgent(),
                structured_failure_store=store,
            )
            session = backend.create_session("alice")
            session.environment_metadata.metadata["state_profile.name"] = (
                "debian-office"
            )

            result = backend.execute(session, "cat broken-envelope.json")

            self.assertTrue(should_capture_structured_failure(result))
            self.assertEqual(
                result.metadata[STRUCTURED_FAILURE_CLASS_METADATA],
                "invalid_json",
            )
            self.assertIn(STRUCTURED_FAILURE_CAPSULE_ID_METADATA, result.metadata)
            self.assertIn(STRUCTURED_FAILURE_CAPSULE_PATH_METADATA, result.metadata)
            summaries = store.list()
            self.assertEqual(len(summaries), 1)
            capsule = store.load(summaries[0].id)
            self.assertEqual(capsule.command, "cat broken-envelope.json")
            self.assertEqual(capsule.profile_name, "debian-office")
            self.assertEqual(capsule.session_id, session.langfuse_trace_id)
            self.assertEqual(capsule.command_id, session.history[-1].id)
            self.assertEqual(capsule.failure_class, "invalid_json")
            self.assertEqual(
                capsule.raw_response,
                result.metadata[STRUCTURED_RAW_RESPONSE_METADATA],
            )
            profile_state = capsule.pre_command_state_profile["state"]
            self.assertEqual(profile_state["cwd"], "/home/alice")
            self.assertEqual(profile_state["history"], [])
            self.assertEqual(session.history[-1].command, "cat broken-envelope.json")

    def test_backend_can_disable_structured_failure_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StructuredFailureStore(Path(directory))
            backend = AgentBackend(
                server_profile(),
                agent=MalformedStructuredAgent(),
                structured_failure_store=store,
                structured_failure_capture_enabled=False,
            )
            session = backend.create_session("alice")

            result = backend.execute(session, "cat broken-envelope.json")

            self.assertTrue(should_capture_structured_failure(result))
            self.assertNotIn(STRUCTURED_FAILURE_CAPSULE_ID_METADATA, result.metadata)
            self.assertEqual(store.list(), [])

    def test_replay_structured_failure_parser_uses_raw_response(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StructuredFailureStore(Path(directory))
            backend = AgentBackend(
                server_profile(),
                agent=MalformedStructuredAgent(),
                structured_failure_store=store,
            )
            session = backend.create_session("alice")
            backend.execute(session, "bad json")
            capsule = store.load(store.list()[0].id)

            replayed = replay_structured_failure_parser(capsule)

            self.assertTrue(should_capture_structured_failure(replayed))
            self.assertEqual(
                replayed.metadata[STRUCTURED_FAILURE_CLASS_METADATA],
                capsule.failure_class,
            )
            self.assertEqual(
                replayed.metadata[STRUCTURED_RAW_RESPONSE_METADATA],
                capsule.raw_response,
            )


if __name__ == "__main__":
    unittest.main()
