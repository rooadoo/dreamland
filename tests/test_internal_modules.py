import unittest

from dreamland.models import (
    InteractiveProgram,
    SessionState,
    ShellEnvironment,
    ShellFrame,
)
from dreamland.session_state import (
    copy_shell_frame,
    replace_shell_stack,
)
from dreamland.ssh_server import (
    _execute_backend_stream,
    _LineBufferedCommandWriter,
)
from dreamland.ssh_streaming import (
    LineBufferedCommandWriter,
    execute_backend_stream,
)


class InternalModuleTests(unittest.TestCase):
    def test_ssh_server_preserves_streaming_facade_aliases(self):
        self.assertIs(_LineBufferedCommandWriter, LineBufferedCommandWriter)
        self.assertIs(_execute_backend_stream, execute_backend_stream)

    def test_shell_frame_copy_is_independent(self):
        frame = ShellFrame(
            username="alice",
            cwd="/home/alice",
            interactive_program=InteractiveProgram(
                name="bash",
                command="/bin/bash",
                metadata={"mode": "interactive"},
            ),
            shell_environment=ShellEnvironment(
                variables={"PWD": "/home/alice"},
                exported=frozenset({"PWD"}),
            ),
            metadata={"level": 1},
        )

        copied = copy_shell_frame(frame)
        copied.metadata["level"] = 2
        copied.interactive_program.metadata["mode"] = "changed"
        copied.shell_environment.variables["PWD"] = "/tmp"

        self.assertEqual(frame.metadata["level"], 1)
        self.assertEqual(frame.interactive_program.metadata["mode"], "interactive")
        self.assertEqual(frame.shell_environment.variables["PWD"], "/home/alice")

    def test_replacing_shell_stack_syncs_legacy_session_fields(self):
        frame = ShellFrame(
            username="root",
            cwd="/root",
            interactive_program=InteractiveProgram(name="bash", command="/bin/bash"),
            shell_environment=ShellEnvironment(
                variables={"PWD": "/root"},
                exported=frozenset({"PWD"}),
            ),
        )
        session = SessionState(username="alice", cwd="/home/alice")

        replace_shell_stack(session, [frame])

        self.assertEqual(session.username, "root")
        self.assertEqual(session.cwd, "/root")
        self.assertIsNot(session.shell_stack[0], frame)


if __name__ == "__main__":
    unittest.main()
