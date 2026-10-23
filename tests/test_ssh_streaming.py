import unittest

from dreamland.backend import AgentBackend
from dreamland.models import CommandOutputChunk, CommandResult, CommandStreamResult
from dreamland.profile import server_profile
from dreamland.ssh_streaming import (
    LineBufferedCommandWriter,
    execute_backend_stream,
)


class PartialLineStreamingAgent:
    def respond(self, context):
        return CommandResult(stdout="fallback\n")

    def stream_respond(self, context):
        yield CommandOutputChunk("only partial")
        yield CommandStreamResult(CommandResult(stdout="only partial"))


class SSHStreamingTests(unittest.TestCase):
    def test_line_buffer_flushes_on_newline_and_completion(self):
        stdout = []
        stderr = []
        writer = LineBufferedCommandWriter(stdout.append, stderr.append)

        writer.write(CommandOutputChunk("alpha"))
        self.assertEqual(stdout, [])
        writer.write(CommandOutputChunk(" beta\npartial"))
        self.assertEqual(stdout, ["alpha beta\n"])
        writer.flush()

        self.assertEqual(stdout, ["alpha beta\n", "partial"])
        self.assertEqual(stderr, [])

    def test_line_buffer_can_force_prompt_separating_newline(self):
        stdout = []
        stderr = []
        writer = LineBufferedCommandWriter(stdout.append, stderr.append)

        writer.write(CommandOutputChunk("partial"))
        writer.flush(ensure_trailing_newline=True)

        self.assertEqual(stdout, ["partial", "\n"])
        self.assertEqual(stderr, [])

    def test_final_result_does_not_duplicate_partial_line(self):
        profile = server_profile()
        backend = AgentBackend(profile, agent=PartialLineStreamingAgent())
        session = backend.create_session("alice")
        stdout = []
        stderr = []
        writer = LineBufferedCommandWriter(stdout.append, stderr.append)

        result = execute_backend_stream(
            backend, session, "python manage.py shell", writer
        )
        writer.flush()

        self.assertEqual(result.stdout, "only partial")
        self.assertEqual(stdout, ["only partial"])
        self.assertEqual(stderr, [])


if __name__ == "__main__":
    unittest.main()
