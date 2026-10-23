from __future__ import annotations

import unittest
from copy import deepcopy

from dreamland.background_agents.authority import ForegroundAuthoritySnapshot
from dreamland.background_agents.enrichment.context import (
    ENRICHMENT_CONTEXT_VERSION,
    RECENT_COMMAND_ENTRY_OUTPUT_CHAR_LIMIT,
    RECENT_COMMAND_OUTPUT_CHAR_LIMIT,
    canonical_json,
    project_world_state_for_enrichment,
    resolve_enrichment_host_ref,
)


class EnrichmentContextTests(unittest.TestCase):
    def test_initial_banners_are_excluded_from_all_model_facing_hosts(self) -> None:
        for host in self.world["hosts_by_name"].values():
            host["login_banner"] = "INITIAL-BANNER-EXCLUDED\n"

        projection = project_world_state_for_enrichment(self.world, target_host="cache")

        self.assertNotIn("INITIAL-BANNER-EXCLUDED", projection.prompt_json())
        self.assertNotIn("login_banner", projection.prompt_json())
        self.assertEqual(
            projection.trusted_target_host["login_banner"],
            "INITIAL-BANNER-EXCLUDED\n",
        )
        self.assertEqual(
            self.world["hosts_by_name"]["app"]["login_banner"],
            "INITIAL-BANNER-EXCLUDED\n",
        )

    def setUp(self) -> None:
        self.world = {
            "active_host": "app",
            "name": "test-world",
            "hosts_by_name": {
                "app": {
                    "hostname": "app-01",
                    "aliases": ["frontend"],
                    "purpose": {"role": "application"},
                    "command_history": [
                        {
                            "id": "c1",
                            "cwd": "/srv",
                            "command": "ps aux",
                            "stdout": "large output",
                            "stderr": "",
                            "exit_status": 0,
                        },
                        {
                            "id": "c2",
                            "cwd": "/srv",
                            "command": "ss -lntp",
                            "stdout": "more output",
                            "exit_status": 0,
                        },
                    ],
                    "filesystem": {
                        "artifacts_by_path": {
                            "/srv/config": {
                                "description": "service configuration",
                                "content": "secret-shaped bulk text",
                            }
                        }
                    },
                },
                "cache": {
                    "hostname": "redis-01",
                    "aliases": ["redis.internal"],
                    "purpose": {"role": "cache"},
                    "services_by_name": {"redis": {"state": "running", "port": 6379}},
                    "filesystem": {
                        "artifacts_by_path": {
                            "/etc/redis/redis.conf": {"content": "bind 0.0.0.0"}
                        }
                    },
                },
            },
        }

    def test_projection_is_host_focused_bounded_and_non_mutating(self) -> None:
        original = deepcopy(self.world)

        projection = project_world_state_for_enrichment(
            self.world,
            target_host="redis.internal",
            recent_history_limit=1,
        )

        self.assertEqual(projection.target_host, "cache")
        self.assertIn("app", projection.detailed_hosts)
        self.assertIn("cache", projection.detailed_hosts)
        world = projection.document["world"]
        self.assertIsInstance(world, dict)
        hosts = world["hosts_by_name"]
        self.assertIsInstance(hosts, dict)
        self.assertNotIn(
            "content",
            hosts["cache"]["filesystem"]["artifacts_by_path"]["/etc/redis/redis.conf"],
        )
        self.assertEqual(
            projection.trusted_target_host["filesystem"]["artifacts_by_path"][
                "/etc/redis/redis.conf"
            ]["content"],
            "bind 0.0.0.0",
        )
        self.assertNotIn("bind 0.0.0.0", repr(projection))
        self.assertEqual(hosts["app"]["command_history"][0]["id"], "c2")
        self.assertNotIn("stdout", hosts["app"]["command_history"][0])
        self.assertEqual(self.world, original)
        self.assertEqual(projection.projected_chars, len(projection.prompt_json()))
        self.assertEqual(projection.command_evidence_count, 0)
        self.assertEqual(
            projection.document["host_time"],
            {"v": 1, "status": "unknown"},
        )
        self.assertEqual(projection.document["recent_exhaustive_evidence"], [])
        self.assertFalse(projection.host_time_observed)
        self.assertEqual(projection.recent_exhaustive_evidence_count, 0)
        self.assertEqual(len(projection.foreground_evidence_sha256), 64)

    def test_target_history_retains_bounded_recorded_result_evidence(self) -> None:
        world = deepcopy(self.world)
        history = world["hosts_by_name"]["app"]["command_history"]
        history.clear()
        for index in range(5):
            history.append(
                {
                    "id": f"c{index}",
                    "cwd": "/srv",
                    "command": "ps -ef" if index % 2 == 0 else "ss -lntp",
                    "stdout": f"HEAD-{index}\n"
                    + (str(index) * 3_000)
                    + f"\nTAIL-{index}",
                    "stderr": f"ERR-{index}\n" + ("e" * 400),
                    "exit_status": 0,
                }
            )

        projection = project_world_state_for_enrichment(world, target_host="app")

        projected_history = projection.document["world"]["hosts_by_name"]["app"][
            "command_history"
        ]
        self.assertEqual(
            [item["id"] for item in projected_history], ["c1", "c2", "c3", "c4"]
        )
        self.assertEqual(projection.document["v"], ENRICHMENT_CONTEXT_VERSION)
        self.assertEqual(
            projection.document["scope"]["command_history"]["semantics"],
            "recorded_output_not_accepted_state",
        )
        self.assertEqual(projection.command_evidence_count, 4)
        self.assertEqual(projection.command_evidence_omitted_count, 1)
        self.assertEqual(projection.command_evidence_truncated_count, 4)
        self.assertLessEqual(
            projection.command_evidence_chars,
            RECENT_COMMAND_OUTPUT_CHAR_LIMIT,
        )
        for item in projected_history:
            excerpt_chars = len(item.get("stdout_excerpt", "")) + len(
                item.get("stderr_excerpt", "")
            )
            self.assertLessEqual(
                excerpt_chars,
                RECENT_COMMAND_ENTRY_OUTPUT_CHAR_LIMIT,
            )
            self.assertEqual(item["evidence_kind"], "recorded_command_result")
            self.assertIn(item["surface"], {"network", "process"})
            self.assertTrue(item["stdout_excerpt"].startswith("HEAD-"))
            self.assertTrue(item["stdout_excerpt"].rstrip().endswith(item["id"][-1]))
            self.assertGreater(item["stdout_omitted_chars"], 0)

        peer = projection.document["world"]["hosts_by_name"]["cache"]
        self.assertNotIn("filesystem", peer)

    def test_hashes_are_canonical_and_target_hash_ignores_peer_drift(self) -> None:
        first = project_world_state_for_enrichment(self.world, target_host="cache")
        reordered = {
            "hosts_by_name": deepcopy(self.world["hosts_by_name"]),
            "name": "test-world",
            "active_host": "app",
        }
        second = project_world_state_for_enrichment(reordered, target_host="cache")
        self.assertEqual(first.full_world_sha256, second.full_world_sha256)
        self.assertEqual(first.target_host_sha256, second.target_host_sha256)

        reordered["hosts_by_name"]["app"]["purpose"] = {"role": "changed"}
        peer_changed = project_world_state_for_enrichment(
            reordered,
            target_host="cache",
        )
        self.assertNotEqual(first.full_world_sha256, peer_changed.full_world_sha256)
        self.assertEqual(first.target_host_sha256, peer_changed.target_host_sha256)

    def test_malformed_structured_envelope_is_bounded_but_not_reinterpreted(
        self,
    ) -> None:
        world = deepcopy(self.world)
        raw = (
            '{"stdout":"visible process listing\\n","stderr":"",'
            '"exit_status":0,"state_update":{"hosts_by_name":{"app":'
        )
        history = world["hosts_by_name"]["app"]["command_history"]
        history[-1]["stdout"] = raw

        projection = project_world_state_for_enrichment(
            world,
            target_host="app",
            recent_history_limit=1,
        )

        entry = projection.document["world"]["hosts_by_name"]["app"]["command_history"][
            0
        ]
        self.assertEqual(entry["stdout_excerpt"], raw)
        self.assertEqual(entry["evidence_kind"], "recorded_command_result")
        self.assertEqual(
            projection.document["scope"]["command_history"]["semantics"],
            "recorded_output_not_accepted_state",
        )

    def test_host_resolution_prefers_registry_key_then_hostname_and_alias(self) -> None:
        self.assertEqual(resolve_enrichment_host_ref(self.world, "cache"), "cache")
        self.assertEqual(
            resolve_enrichment_host_ref(self.world, "redis-01"),
            "cache",
        )
        self.assertEqual(
            resolve_enrichment_host_ref(self.world, "redis.internal"),
            "cache",
        )
        self.assertIsNone(resolve_enrichment_host_ref(self.world, "missing"))

    def test_missing_target_and_negative_history_window_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "target host not found"):
            project_world_state_for_enrichment(self.world, target_host="missing")
        with self.assertRaisesRegex(ValueError, "zero or greater"):
            project_world_state_for_enrichment(
                self.world,
                target_host="cache",
                recent_history_limit=-1,
            )

    def test_prompt_json_is_canonical(self) -> None:
        projection = project_world_state_for_enrichment(
            self.world,
            target_host="cache",
        )
        self.assertEqual(projection.prompt_json(), canonical_json(projection.document))

    def test_foreground_authority_is_projected_and_fingerprinted(self) -> None:
        authority = ForegroundAuthoritySnapshot(
            target_host="app",
            hostname="app-01",
            active_username="alice",
            login_username="alice",
            cwd="/srv/app",
            program_name="bash",
            program_command="/bin/bash",
            program_argv=("/bin/bash",),
            program_prompt_template=None,
            shell_environment=(("SHELL", "/bin/bash"),),
            shell_stack_depth=1,
        )

        projection = project_world_state_for_enrichment(
            self.world,
            target_host="app",
            foreground_authority=authority,
        )

        self.assertEqual(
            projection.document["foreground_authority"],
            authority.public_payload(),
        )
        self.assertEqual(
            projection.foreground_authority_sha256,
            authority.session_semantics_sha256,
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            project_world_state_for_enrichment(
                self.world,
                target_host="cache",
                foreground_authority=authority,
            )

    def test_context_v4_projects_bounded_time_and_recent_exhaustive_evidence(
        self,
    ) -> None:
        world = deepcopy(self.world)
        history = world["hosts_by_name"]["app"]["command_history"]
        history.clear()
        history.extend(
            [
                {
                    "id": "time-1",
                    "cwd": "/home/alice",
                    "command": "date",
                    "stdout": "Thu Nov 12 16:38:27 GST 2318\n",
                    "stderr": "",
                    "exit_status": 0,
                    "interrupted": False,
                    "signal": None,
                },
                {
                    "id": "home-1",
                    "cwd": "/home/alice",
                    "command": "ls -la /home",
                    "stdout": "alice\nbob\n",
                    "stderr": "",
                    "exit_status": 0,
                    "interrupted": False,
                    "signal": None,
                },
                {
                    "id": "ordinary-1",
                    "cwd": "/home/alice",
                    "command": "pwd",
                    "stdout": "/home/alice\n",
                    "stderr": "",
                    "exit_status": 0,
                    "interrupted": False,
                    "signal": None,
                },
            ]
        )
        original = deepcopy(world)

        projection = project_world_state_for_enrichment(
            world,
            target_host="app",
            recent_history_limit=2,
        )

        self.assertEqual(projection.document["v"], 4)
        self.assertEqual(projection.document["host_time"]["status"], "observed")
        self.assertEqual(
            projection.document["host_time"]["output_excerpt"],
            "Thu Nov 12 16:38:27 GST 2318\n",
        )
        exhaustive = projection.document["recent_exhaustive_evidence"]
        self.assertEqual(len(exhaustive), 1)
        self.assertEqual(exhaustive[0]["kind"], "home_directory_inventory")
        self.assertEqual(exhaustive[0]["command_id"], "home-1")
        projected_history = projection.document["world"]["hosts_by_name"]["app"][
            "command_history"
        ]
        self.assertEqual([item["id"] for item in projected_history], ["ordinary-1"])
        self.assertTrue(projection.host_time_observed)
        self.assertEqual(projection.recent_exhaustive_evidence_count, 1)
        self.assertNotIn(
            projection.foreground_evidence_sha256,
            projection.prompt_json(),
        )
        self.assertNotIn(
            projection.foreground_evidence_sha256,
            repr(projection),
        )
        self.assertEqual(world, original)


if __name__ == "__main__":
    unittest.main()
