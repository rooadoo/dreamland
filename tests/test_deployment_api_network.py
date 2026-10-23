"""Read-only Docker topology guard with a subprocess-backed fake Docker CLI."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class ApiNetworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.calls = self.directory / "calls.jsonl"
        self.scenario_path = self.directory / "scenario.json"
        fake = self.directory / "docker"
        fake.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(
                """\
                import json, os, sys
                args = sys.argv[1:]
                with open(os.environ['TEST_DOCKER_CALLS'], 'a') as output:
                    output.write(json.dumps(args) + '\\n')
                with open(os.environ['TEST_DOCKER_SCENARIO']) as source:
                    scenario = json.load(source)
                operation = ' '.join(args[:2]) if args[0] == 'network' else args[0]
                if operation == scenario.get('failure'):
                    print('sensitive-docker-value')
                    print('sensitive-docker-error', file=sys.stderr)
                    sys.exit(17)
                if operation == 'compose':
                    print(json.dumps(scenario['config']))
                elif operation == 'network ls':
                    print(scenario['names'], end='')
                elif operation == 'network inspect':
                    print(json.dumps(scenario['network']))
                elif operation == 'inspect':
                    print(json.dumps(scenario['containers'][args[-1]]))
                else:
                    print('forbidden mutating or unexpected command', file=sys.stderr)
                    sys.exit(19)
                """
            )
        )
        fake.chmod(0o700)
        self.environment = {
            "PATH": f"{self.directory}:/usr/bin:/bin",
            "TEST_DOCKER_CALLS": str(self.calls),
            "TEST_DOCKER_SCENARIO": str(self.scenario_path),
        }
        self.script = (
            Path(__file__).resolve().parents[1] / "deploy/check-api-network.py"
        )
        self.project = "dreamland-demo"
        self.network_name = "dreamland-demo-companion"
        image = "sha256:" + "1" * 64
        config = {
            "name": self.project,
            "services": {},
            "volumes": {},
            "networks": {
                "companion": {
                    "name": self.network_name,
                    "internal": True,
                    "labels": {
                        "org.dreamland.network-purpose": "companion-api-foundation"
                    },
                }
            },
        }
        for role in ("attendee", "operator"):
            source = role + "-api-data"
            config["volumes"][source] = {"name": f"{self.project}_{source}"}
            config["services"][role + "-api"] = {
                "image": image,
                "networks": {"companion": {"aliases": [f"{self.project}-{role}-api"]}},
                "volumes": [
                    {
                        "type": "volume",
                        "source": source,
                        "target": "/var/lib/dreamland/api",
                        "read_only": True,
                    }
                ],
            }
        self.scenario = {
            "config": config,
            "names": "",
            "network": {
                "name": self.network_name,
                "driver": "bridge",
                "internal": True,
                "project": self.project,
                "network": "companion",
                "purpose": "companion-api-foundation",
                "containers": {},
            },
            "containers": {},
        }

    def run_check(self, *arguments: str) -> tuple[subprocess.CompletedProcess, list]:
        self.scenario_path.write_text(json.dumps(self.scenario))
        self.calls.write_text("")
        result = subprocess.run(
            [sys.executable, str(self.script), *arguments],
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        for call in calls:
            self.assertTrue(call[0] in {"compose", "network", "inspect"})
            if call[0] == "network":
                self.assertIn(call[1], {"ls", "inspect"})
            if call[0] == "compose":
                self.assertEqual(call[-3:], ["config", "--format", "json"])
        return result, calls

    def present(self) -> None:
        self.scenario["names"] = self.network_name + "\n"

    def connect(
        self,
        character: str,
        *,
        project: str,
        service: str,
        aliases: list,
        oneoff="False",
    ) -> None:
        identifier = character * 64
        self.scenario["network"]["containers"][identifier] = {"Name": "not-reported"}
        self.scenario["containers"][identifier] = {
            "project": project,
            "service": service,
            "oneoff": oneoff,
            "aliases": aliases,
        }

    def test_missing_network_allows_compose_creation_without_mutating(self) -> None:
        result, calls = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "validation": "passed",
                "project": self.project,
                "network": self.network_name,
                "network_state": "absent",
                "connected_containers": 0,
            },
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1:3], ["--env-file", "/dev/null"])
        self.assertEqual(
            calls[1],
            [
                "network",
                "ls",
                "--filter",
                "name=^dreamland-demo-companion$",
                "--format",
                "{{.Name}}",
            ],
        )

    def test_explicit_environment_file_is_passed_without_content_or_path_output(
        self,
    ) -> None:
        path = self.directory / "private-config.env"
        result, calls = self.run_check("--env-file", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0][1:3], ["--env-file", str(path)])
        self.assertNotIn(str(path), result.stdout + result.stderr)

    def test_existing_owned_internal_bridge_with_roles_and_foreign_ui_is_valid(
        self,
    ) -> None:
        self.present()
        for role, character in (("attendee", "a"), ("operator", "b")):
            self.connect(
                character,
                project=self.project,
                service=role + "-api",
                aliases=[f"{self.project}-{role}-api", role + "-api"],
            )
        self.connect(
            "c",
            project="independent-ui",
            service="bff",
            aliases=["control-bff", "independent-ui-bff-1"],
        )
        result, calls = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["connected_containers"], 3)
        self.assertEqual(json.loads(result.stdout)["network_state"], "present")
        self.assertEqual(len(calls), 6)
        for call in calls[3:]:
            self.assertNotIn(".Config.Env", call[2])
            self.assertNotIn("{{json .}}", call[2])
            self.assertIn(".Aliases", call[2])

    def test_foreign_network_or_wrong_settings_fail(self) -> None:
        self.present()
        original = copy.deepcopy(self.scenario["network"])
        for change in (
            {"project": "foreign-project"},
            {"network": "default"},
            {"purpose": "other"},
            {"internal": False},
            {"internal": 1},
            {"driver": "overlay"},
            {"name": "other"},
            {"containers": None},
        ):
            self.scenario["network"] = dict(original, **change)
            with self.subTest(change=change):
                result, calls = self.run_check()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(len(calls), 3)

    def test_foreign_client_cannot_claim_either_reserved_alias(self) -> None:
        self.present()
        for role in ("attendee", "operator"):
            self.scenario["network"]["containers"] = {}
            self.scenario["containers"] = {}
            self.connect(
                "a",
                project="ui-project",
                service="bff",
                aliases=[f"{self.project}-{role}-api"],
            )
            with self.subTest(role=role):
                result, _ = self.run_check()
                self.assertNotEqual(result.returncode, 0)

    def test_wrong_role_or_oneoff_cannot_claim_backend_alias(self) -> None:
        self.present()
        self.connect(
            "a",
            project=self.project,
            service="operator-api",
            aliases=[f"{self.project}-attendee-api"],
        )
        result, _ = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.scenario["containers"]["a" * 64].update(
            service="attendee-api", oneoff="True"
        )
        result, _ = self.run_check()
        self.assertNotEqual(result.returncode, 0)

    def test_duplicate_reserved_alias_on_two_expected_containers_fails(self) -> None:
        self.present()
        for character in ("a", "b"):
            self.connect(
                character,
                project=self.project,
                service="attendee-api",
                aliases=[f"{self.project}-attendee-api"],
            )
        result, _ = self.run_check()
        self.assertNotEqual(result.returncode, 0)

    def test_docker_failure_is_not_treated_as_network_absence_or_echoed(self) -> None:
        self.present()
        self.connect("a", project="ui-project", service="bff", aliases=["bff"])
        for operation in ("compose", "network ls", "network inspect", "inspect"):
            with self.subTest(operation=operation):
                self.scenario["failure"] = operation
                result, _ = self.run_check()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("sensitive-docker", result.stderr)

    def test_missing_docker_is_a_safe_failure(self) -> None:
        self.environment["PATH"] = "/missing-docker-binary-directory"
        result, calls = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(calls, [])
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("/missing-docker-binary-directory", result.stderr)

    def test_duplicate_exact_network_names_fail_and_substrings_do_not_match(
        self,
    ) -> None:
        self.scenario["names"] = (self.network_name + "\n") * 2
        result, calls = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)
        self.scenario["names"] = self.network_name + "-other\n"
        result, _ = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["network_state"], "absent")

    def test_invalid_rendered_role_images_ports_aliases_and_mounts_fail_before_inspect(
        self,
    ) -> None:
        original = copy.deepcopy(self.scenario["config"])
        mutations = (
            lambda d: d["services"]["attendee-api"].update(image="dreamland:latest"),
            lambda d: d["services"]["operator-api"].update(image="sha256:" + "2" * 64),
            lambda d: d["services"]["attendee-api"].update(
                ports=[{"published": "8080", "target": 8080}]
            ),
            lambda d: d["services"]["attendee-api"]["networks"]["companion"].update(
                aliases=["wrong-api"]
            ),
            lambda d: d["services"]["attendee-api"].update(network_mode="host"),
            lambda d: d["services"]["attendee-api"].update(secrets=["provider"]),
            lambda d: d["services"]["attendee-api"].update(configs=["backend-ini"]),
            lambda d: d["services"]["attendee-api"]["volumes"][0].update(
                type="bind", source="/private"
            ),
            lambda d: d["services"]["attendee-api"]["volumes"][0].update(
                source="operator-api-data"
            ),
            lambda d: d["services"]["attendee-api"]["volumes"][0].update(
                read_only=False
            ),
            lambda d: d["volumes"]["attendee-api-data"].update(
                name="dreamland-ssh-data"
            ),
            lambda d: d["networks"]["companion"].update(internal=False),
            lambda d: d["networks"]["companion"].update(driver="overlay"),
            lambda d: d["networks"]["companion"].update(external=True),
            lambda d: d.update(name="unsafe project"),
        )
        for index, mutate in enumerate(mutations):
            self.scenario["config"] = copy.deepcopy(original)
            mutate(self.scenario["config"])
            with self.subTest(index=index):
                result, calls = self.run_check()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("/private", result.stderr)

    def test_optional_setup_services_must_remain_offline_and_isolated(self) -> None:
        service = copy.deepcopy(self.scenario["config"]["services"]["attendee-api"])
        del service["networks"]
        service["network_mode"] = "none"
        service["volumes"][0].pop("read_only")
        self.scenario["config"]["services"]["attendee-init"] = service
        result, _ = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        service["network_mode"] = "host"
        result, _ = self.run_check()
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
