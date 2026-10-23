import unittest

from dreamland.agent_shell import (
    ShellCommandSegment,
    analyze_command,
    expand_shell_variables,
    normalize_path,
    split_command,
    split_command_list,
    split_leading_assignments,
)
from dreamland.models import ShellEnvironment


class AgentShellTests(unittest.TestCase):
    def test_split_command_handles_quotes(self):
        self.assertEqual(
            split_command("printf '%s %s' one two"),
            ["printf", "%s %s", "one", "two"],
        )

    def test_split_command_rejects_unclosed_quotes(self):
        self.assertEqual(split_command("echo 'unfinished"), [])

    def test_split_command_list_handles_common_separators(self):
        self.assertEqual(
            split_command_list("cd portops; ls"),
            [
                ShellCommandSegment("cd portops"),
                ShellCommandSegment("ls", operator_before=";"),
            ],
        )
        self.assertEqual(
            split_command_list("pwd && whoami || env"),
            [
                ShellCommandSegment("pwd"),
                ShellCommandSegment("whoami", operator_before="&&"),
                ShellCommandSegment("env", operator_before="||"),
            ],
        )

    def test_split_command_list_respects_quoted_separators(self):
        self.assertEqual(
            split_command_list('echo "a; b"; pwd'),
            [
                ShellCommandSegment('echo "a; b"'),
                ShellCommandSegment("pwd", operator_before=";"),
            ],
        )

    def test_split_command_list_rejects_unsupported_shell_syntax(self):
        self.assertIsNone(split_command_list("cat file | grep value"))
        self.assertIsNone(split_command_list("echo value > file"))
        self.assertIsNone(split_command_list("echo $(pwd); ls"))

    def test_analysis_preserves_source_and_quoted_operator_arguments(self):
        source = """ export X=";"  && printenv  X || env 'Y=&&' ; pwd """

        analysis = analyze_command(source)

        self.assertEqual(analysis.kind, "list")
        self.assertEqual(
            analysis.segments,
            (
                ShellCommandSegment('export X=";"'),
                ShellCommandSegment("printenv  X", "&&"),
                ShellCommandSegment("env 'Y=&&'", "||"),
                ShellCommandSegment("pwd", ";"),
            ),
        )
        self.assertEqual(
            analyze_command(r"""printenv ';' "&&" \|\|; pwd""").segments,
            (
                ShellCommandSegment(r"""printenv ';' "&&" \|\|"""),
                ShellCommandSegment("pwd", ";"),
            ),
        )

    def test_analysis_distinguishes_empty_simple_list_and_unsupported(self):
        examples = {
            "": "empty",
            " \t ": "empty",
            "pwd": "simple",
            "pwd;": "list",
            "pwd ; whoami": "list",
            "pwd &&": "unsupported",
            ";pwd": "unsupported",
            "pwd;; whoami": "unsupported",
            "pwd &": "unsupported",
            "pwd | cat": "unsupported",
            "pwd > /tmp/path": "unsupported",
            "pwd < /tmp/path": "unsupported",
            "pwd\nwhoami": "unsupported",
            "pwd # comment": "unsupported",
            "(pwd)": "unsupported",
            "{ pwd; }": "unsupported",
            "if pwd; then whoami; fi": "unsupported",
            "pwd 'unfinished": "unsupported",
            "pwd \\": "unsupported",
            "pwd \\\n": "unsupported",
            "export X=`pwd`; pwd": "unsupported",
            'export X="$(pwd)"; pwd': "unsupported",
            "export X=${HOME:-/tmp}; pwd": "unsupported",
        }
        for source, kind in examples.items():
            with self.subTest(source=source):
                self.assertEqual(analyze_command(source).kind, kind)

    def test_analysis_allows_literal_shell_punctuation(self):
        for source in (
            'printenv ";"',
            "printenv '&&'",
            "printenv '||'",
            r"printenv \;",
            "export X='$(pwd)'",
            "export X='`pwd`'",
            'export X="a; b"',
            "printenv 'a#b'",
            "printenv a#b",
            'cd "${HOME}/src"',
        ):
            with self.subTest(source=source):
                analysis = analyze_command(source)
                self.assertEqual(analysis.kind, "simple")
                self.assertEqual(analysis.segments, (ShellCommandSegment(source),))

    def test_leading_assignments_are_separated_from_command(self):
        assignments, command = split_leading_assignments(["ONE=1", "TWO=two", "env"])

        self.assertEqual(assignments, {"ONE": "1", "TWO": "two"})
        self.assertEqual(command, ["env"])

    def test_variable_expansion_supports_braced_and_plain_names(self):
        environment = ShellEnvironment(
            variables={"HOME": "/home/alice", "USER": "alice"},
        )

        expanded = expand_shell_variables(
            "${HOME}/src/$USER",
            environment,
        )

        self.assertEqual(expanded, "/home/alice/src/alice")

    def test_normalize_path_resolves_dot_segments(self):
        self.assertEqual(normalize_path("/home/alice/../bob/./src"), "/home/bob/src")


if __name__ == "__main__":
    unittest.main()
