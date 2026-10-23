"""The host wrapper targets the intended service without starting another runtime."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class DeploymentControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.calls = self.directory / "calls.jsonl"
        fake = self.directory / "docker"
        fake.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(
                """\
                import json, os, sys
                arguments = sys.argv[1:]
                with open(os.environ['TEST_DOCKER_CALLS'], 'a') as out:
                    out.write(json.dumps(arguments) + '\\n')
                if arguments[0] == 'ps':
                    if os.environ.get('TEST_DISCOVERY_FAILURE'):
                        partial_output = os.environ.get('TEST_DISCOVERY_PARTIAL_OUTPUT')
                        if partial_output:
                            print(partial_output)
                        print('Cannot connect to Docker daemon', file=sys.stderr)
                        sys.exit(7)
                    filters = [
                        arguments[index + 1]
                        for index, value in enumerate(arguments)
                        if value == '--filter'
                    ]
                    include_stopped = '-a' in arguments or '--all' in arguments
                    for container in json.loads(os.environ.get('TEST_CONTAINERS', '[]')):
                        labels = container['labels']
                        if not include_stopped and container['state'] != 'running':
                            continue
                        matches = True
                        for item in filters:
                            if not item.startswith('label='):
                                raise ValueError('Unsupported fake Docker filter')
                            name, separator, value = item[6:].partition('=')
                            if name not in labels or (separator and labels[name] != value):
                                matches = False
                                break
                        if matches:
                            print(labels.get('com.docker.compose.project', ''))
                elif 'ps' in arguments and os.environ.get('TEST_CONTAINER_STATE'):
                    print(os.environ['TEST_CONTAINER_STATE'])
                """
            )
        )
        fake.chmod(0o700)
        self.environment = {
            "PATH": f"{self.directory}:/usr/bin:/bin",
            "TEST_DOCKER_CALLS": str(self.calls),
        }
        self.wrapper = Path(__file__).resolve().parents[1] / "deploy/dreamlandctl"

    def run_control(self, *arguments, project="review-backend"):
        self.calls.write_text("")
        options = [] if project is None else ["--project", project]
        result = subprocess.run(
            ["sh", str(self.wrapper), *options, *arguments],
            env=self.environment,
            input="",
            text=True,
            capture_output=True,
        )
        calls = (
            [json.loads(line) for line in self.calls.read_text().splitlines()]
            if self.calls.exists()
            else []
        )
        return result, calls

    def container(
        self,
        project,
        *,
        state="running",
        service="ssh",
        title="DREAMLAND backend",
        oneoff="False",
    ):
        labels = {
            "com.docker.compose.service": service,
            "org.opencontainers.image.title": title,
            "com.docker.compose.oneoff": oneoff,
        }
        if project is not None:
            labels["com.docker.compose.project"] = project
        return {"state": state, "labels": labels}

    def set_containers(self, *containers):
        self.environment["TEST_CONTAINERS"] = json.dumps(containers)

    def assert_compose_project(self, call, expected):
        self.assertEqual(call[0], "compose")
        self.assertEqual(call[call.index("--project-name") + 1], expected)

    def test_console_executes_only_client_and_preserves_literal_command(self):
        command = "spiritguide --dry-run 'literal $(no-shell-execution)'"
        result, calls = self.run_control("console", "--command", command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn("review-backend", calls[0])
        self.assertEqual(
            calls[0][-9:],
            [
                "exec",
                "-T",
                "--user",
                "10001:10001",
                "ssh",
                "python",
                "-m",
                "dreamland.console_client",
                "--command=" + command,
            ],
        )
        self.assertNotIn("up", calls[0])
        self.assertNotIn("run", calls[0])

    def test_stop_targets_ssh_via_compose(self):
        result, calls = self.run_control("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0][-2:], ["stop", "ssh"])

    def test_apply_requires_stopped_runtime(self):
        for state in ("running", "paused", "restarting", "dead", "unknown"):
            with self.subTest(state=state):
                self.calls.write_text("")
                self.environment["TEST_CONTAINER_STATE"] = state
                result, calls = self.run_control("model-defaults", "--apply")
                self.assertEqual(result.returncode, 1)
                self.assertIn("Stop the SSH", result.stderr)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("run", calls[0])

    def test_apply_accepts_stopped_container(self):
        self.environment["TEST_CONTAINER_STATE"] = "exited"
        result, calls = self.run_control("model-defaults", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertIn("run", calls[1])
        self.assertEqual(calls[1][-1], "--apply")

    def test_invalid_noninteractive_console_does_not_call_docker(self):
        result, calls = self.run_control("console")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])

    def test_model_preview_uses_offline_setup_and_no_apply(self):
        result, calls = self.run_control("model-defaults")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn("--pull", calls[0])
        self.assertIn("never", calls[0])
        self.assertNotIn("--apply", calls[0])
        self.assertEqual(
            calls[0][-3:], ["model-defaults", "--root", "/var/lib/dreamland"]
        )

    def test_explicit_project_takes_precedence_without_discovery(self):
        self.environment["DREAMLAND_PROJECT"] = "environment-project"
        self.environment["TEST_DISCOVERY_FAILURE"] = "1"
        result, calls = self.run_control("status", project="explicit-project")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assert_compose_project(calls[0], "explicit-project")

    def test_environment_project_takes_precedence_without_discovery(self):
        self.environment["DREAMLAND_PROJECT"] = "environment-project"
        self.environment["TEST_DISCOVERY_FAILURE"] = "1"
        result, calls = self.run_control("status", project=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assert_compose_project(calls[0], "environment-project")

    def test_unique_deployment_is_selected_for_each_action(self):
        self.set_containers(self.container("saved-deployment"))
        actions = (
            ("console", "--command", "sessions --live"),
            ("status",),
            ("logs",),
            ("stop",),
            ("restart",),
            ("model-defaults",),
        )
        for action in actions:
            with self.subTest(action=action):
                result, calls = self.run_control(*action, project=None)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0][0], "ps")
                self.assert_compose_project(calls[1], "saved-deployment")
                self.assertIn(
                    "Using DREAMLAND project saved-deployment.", result.stderr
                )

    def test_empty_environment_value_allows_discovery(self):
        self.environment["DREAMLAND_PROJECT"] = ""
        self.set_containers(self.container("saved-deployment"))
        result, calls = self.run_control("status", project=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_compose_project(calls[-1], "saved-deployment")

    def test_stopped_deployment_is_discoverable(self):
        self.set_containers(self.container("stopped-deployment", state="exited"))
        result, calls = self.run_control("restart", project=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_compose_project(calls[-1], "stopped-deployment")
        self.assertEqual(calls[-1][-2:], ["restart", "ssh"])

    def test_duplicate_containers_in_one_project_are_not_ambiguous(self):
        self.set_containers(
            self.container("saved-deployment"),
            self.container("saved-deployment", state="exited"),
        )
        result, calls = self.run_control("status", project=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_compose_project(calls[-1], "saved-deployment")

    def test_discovery_ignores_setup_smoke_and_unrelated_containers(self):
        self.set_containers(
            self.container("saved-deployment"),
            self.container("setup-project", service="init"),
            self.container("smoke-project", oneoff="True"),
            self.container("unrelated-project", title="Another SSH backend"),
            self.container(None),
        )
        result, calls = self.run_control("status", project=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_compose_project(calls[-1], "saved-deployment")

    def test_zero_deployments_requires_explicit_selection(self):
        result, calls = self.run_control("model-defaults", "--apply", project=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--project", result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "ps")

    def test_ambiguous_deployments_do_not_perform_action(self):
        self.set_containers(self.container("alpha"), self.container("beta"))
        result, calls = self.run_control("stop", project=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--project", result.stderr)
        self.assertIn("alpha", result.stderr)
        self.assertIn("beta", result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "ps")

    def test_discovery_failure_does_not_fall_back_or_perform_action(self):
        self.environment["TEST_DISCOVERY_FAILURE"] = "1"
        result, calls = self.run_control("restart", project=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker", result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "ps")

    def test_list_returns_sorted_unique_running_and_stopped_projects(self):
        self.set_containers(
            self.container("zulu", state="exited"),
            self.container("alpha"),
            self.container("alpha", state="exited"),
            self.container("setup-project", service="init"),
            self.container("smoke-project", oneoff="True"),
            self.container("unrelated-project", title="Another SSH backend"),
            self.container(None),
        )
        result, calls = self.run_control("list", project=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "alpha\nzulu\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "ps")

    def test_list_with_no_deployments_succeeds_without_stdout(self):
        result, calls = self.run_control("list", project=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "No DREAMLAND SSH deployments found.\n")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "ps")

    def test_list_discovery_failure_never_prints_partial_projects(self):
        self.environment["TEST_DISCOVERY_FAILURE"] = "1"
        for partial_output in ("", "alpha\nzulu"):
            with self.subTest(partial_output=partial_output):
                self.environment["TEST_DISCOVERY_PARTIAL_OUTPUT"] = partial_output
                result, calls = self.run_control("list", project=None)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("Docker", result.stderr)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], "ps")

    def test_list_ignores_target_and_compose_environment_settings(self):
        self.set_containers(self.container("alpha"), self.container("beta"))
        self.environment["DREAMLAND_PROJECT"] = "environment-project"
        self.environment["DREAMLAND_DEPLOYMENT_ENV"] = str(
            self.directory / "missing-environment.env"
        )
        for project, options in (
            (None, ()),
            (
                "explicit-project",
                ("--env-file", str(self.directory / "missing-explicit.env")),
            ),
        ):
            with self.subTest(project=project):
                result, calls = self.run_control(*options, "list", project=project)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "alpha\nbeta\n")
                self.assertEqual(result.stderr, "")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], "ps")

    def test_invalid_list_arguments_do_not_call_docker(self):
        for arguments in (("unexpected",), ("--apply",), ("--project", "alpha")):
            with self.subTest(arguments=arguments):
                result, calls = self.run_control("list", *arguments, project=None)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(calls, [])

    def test_invalid_explicit_project_does_not_discover(self):
        self.set_containers(self.container("saved-deployment"))
        for project in ("", "Bad-project", "-project", "space name", "line\nbreak"):
            with self.subTest(project=project):
                result, calls = self.run_control("status", project=project)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(calls, [])

    def test_invalid_environment_project_does_not_discover(self):
        self.environment["DREAMLAND_PROJECT"] = "Bad-project"
        self.set_containers(self.container("saved-deployment"))
        result, calls = self.run_control("status", project=None)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])

    def test_invalid_discovered_project_does_not_perform_action(self):
        self.set_containers(self.container("Bad-project"))
        result, calls = self.run_control("restart", project=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "ps")

    def test_invalid_arguments_do_not_trigger_discovery(self):
        for arguments in (
            (),
            ("unknown",),
            ("status", "unexpected"),
            ("console",),
            ("console", "--command"),
            ("model-defaults", "--unknown"),
            ("--project",),
        ):
            with self.subTest(arguments=arguments):
                result, calls = self.run_control(*arguments, project=None)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(calls, [])

    def test_help_does_not_trigger_discovery(self):
        result, calls = self.run_control("--help", project=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_apply_resolves_project_once_for_guard_and_mutation(self):
        self.environment["TEST_CONTAINER_STATE"] = "exited"
        self.set_containers(self.container("saved-deployment", state="exited"))
        result, calls = self.run_control("model-defaults", "--apply", project=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0], "ps")
        self.assert_compose_project(calls[1], "saved-deployment")
        self.assert_compose_project(calls[2], "saved-deployment")
        self.assertIn("ps", calls[1])
        self.assertIn("run", calls[2])
        self.assertEqual(calls[2][-1], "--apply")

    def test_discovery_preserves_running_service_apply_guard(self):
        self.environment["TEST_CONTAINER_STATE"] = "running"
        self.set_containers(self.container("saved-deployment"))
        result, calls = self.run_control("model-defaults", "--apply", project=None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stop the SSH", result.stderr)
        self.assertEqual(len(calls), 2)
        self.assert_compose_project(calls[-1], "saved-deployment")
        self.assertNotIn("run", calls[-1])


if __name__ == "__main__":
    unittest.main()
