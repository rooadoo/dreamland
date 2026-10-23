import json
import unittest

from dreamland.emulator_context import (
    detailed_host_registry_refs_for_command_emulator,
    project_world_state_for_command_emulator,
)


class EmulatorContextTests(unittest.TestCase):
    def test_initial_banners_are_excluded_from_active_detailed_and_peer_hosts(self):
        hosts = {
            name: {
                "hostname": name,
                "os": {"distribution": "Debian", "version": "12"},
                "login_banner": f"SAVED-BANNER-{name}\n",
            }
            for name in ("active", "detailed", "peer")
        }
        world = {"active_host": "active", "hosts_by_name": hosts}

        projected = project_world_state_for_command_emulator(
            world, detailed_host_refs=("detailed",)
        )

        self.assertNotIn("SAVED-BANNER", json.dumps(projected))
        for name, host in hosts.items():
            self.assertNotIn("login_banner", projected["hosts_by_name"][name])
            self.assertEqual(host["login_banner"], f"SAVED-BANNER-{name}\n")
            self.assertEqual(projected["hosts_by_name"][name]["os"]["version"], "12")

    def test_command_emulator_projection_keeps_full_active_and_compacts_peers(self):
        state = {
            "name": "hospital",
            "active_host": "jump01",
            "world_facts_by_id": {"site": {"summary": "Shared world fact."}},
            "future_world_field": {"preserved": ["exactly"]},
            "hosts_by_name": {
                "jump01": {
                    "hostname": "jump01",
                    "command_history": [{"command": "id", "stdout": "user\n"}],
                    "filesystem": {
                        "artifacts_by_path": {"/secret": {"content": "ACTIVE-FULL"}}
                    },
                },
                "db01": {
                    "hostname": "db01",
                    "aliases": ["reporting-db"],
                    "site": "dc-a",
                    "role": "reporting database",
                    "purpose": {
                        "theme": "Clinical reporting database",
                        "description": "Serves reporting workloads.",
                        "private_detail": "PURPOSE-HIDDEN",
                    },
                    "operating_system": {
                        "family": "linux",
                        "distribution": "RHEL",
                        "private_detail": "OS-HIDDEN",
                    },
                    "facts_by_id": {
                        "role": {
                            "summary": "Primary reporting database.",
                            "evidence": "FACT-HIDDEN",
                        },
                        "legacy": {"fact": "Uses a legacy client protocol."},
                    },
                    "services_by_name": {
                        "postgresql": {
                            "address": "10.40.8.12",
                            "port": 5432,
                            "protocol": "tcp",
                            "state": "running",
                            "health": "healthy",
                            "upstream": "db-router.example.test",
                            "credentials": "SERVICE-HIDDEN",
                        }
                    },
                    "command_history": [
                        {"command": "cat /large", "stdout": "HISTORY-HIDDEN"}
                    ],
                    "filesystem": {
                        "artifacts_by_path": {
                            "/large": {"content": "FILESYSTEM-HIDDEN"}
                        }
                    },
                    "users_by_username": {"analyst": {"detail": "USER-HIDDEN"}},
                    "groups_by_name": {"dba": {"detail": "GROUP-HIDDEN"}},
                    "shell": {"default": {"command": "/bin/bash"}},
                    "hardware": {"serial": "HARDWARE-HIDDEN"},
                    "processes": [
                        {
                            "name": "patient-import",
                            "status": "running",
                            "type": "batch",
                        }
                    ],
                    "credentials": {
                        "name": "vault-root",
                        "status": "enabled",
                    },
                },
            },
        }

        projected = project_world_state_for_command_emulator(state)

        self.assertEqual(projected["name"], "hospital")
        self.assertEqual(projected["world_facts_by_id"], state["world_facts_by_id"])
        self.assertEqual(projected["future_world_field"], state["future_world_field"])
        self.assertEqual(
            projected["hosts_by_name"]["jump01"],
            state["hosts_by_name"]["jump01"],
        )
        peer = projected["hosts_by_name"]["db01"]
        self.assertEqual(peer["hostname"], "db01")
        self.assertEqual(peer["aliases"], ["reporting-db"])
        self.assertEqual(peer["site"], "dc-a")
        self.assertEqual(peer["purpose"]["theme"], "Clinical reporting database")
        self.assertNotIn("private_detail", peer["purpose"])
        self.assertEqual(peer["operating_system"]["distribution"], "RHEL")
        self.assertNotIn("private_detail", peer["operating_system"])
        self.assertEqual(
            peer["facts_by_id"]["role"],
            {"summary": "Primary reporting database."},
        )
        self.assertEqual(
            peer["facts_by_id"]["legacy"],
            {"fact": "Uses a legacy client protocol."},
        )
        self.assertEqual(
            peer["services_by_name"]["postgresql"],
            {
                "address": "10.40.8.12",
                "port": 5432,
                "protocol": "tcp",
                "state": "running",
                "health": "healthy",
                "upstream": "db-router.example.test",
            },
        )
        for hidden_key in (
            "command_history",
            "filesystem",
            "groups_by_name",
            "hardware",
            "shell",
            "users_by_username",
            "processes",
            "credentials",
        ):
            self.assertNotIn(hidden_key, peer)
        peer["aliases"].append("projection-only")
        peer["services_by_name"]["postgresql"]["address"] = "192.0.2.200"
        self.assertEqual(state["hosts_by_name"]["db01"]["aliases"], ["reporting-db"])
        self.assertEqual(
            state["hosts_by_name"]["db01"]["services_by_name"]["postgresql"]["address"],
            "10.40.8.12",
        )

    def test_command_emulator_projection_preserves_structured_host_addresses(self):
        state = {
            "entry_host": "source",
            "active_host": "source",
            "hosts_by_name": {
                "source": {"hostname": "source"},
                "10.99.10.5": {
                    "hostname": "bastion",
                    "fqdn": "bastion.example.test",
                    "ip": "10.99.10.5",
                    "network": {
                        "interfaces": [
                            {
                                "name": "eth0",
                                "ipv4": ["10.99.10.5/24"],
                                "ipv6": ["2001:db8:99::5/64"],
                                "packet_capture": "NETWORK-HIDDEN",
                            }
                        ],
                        "management": {"address": "172.16.0.5"},
                        "recorded_peer_ips": ["192.0.2.7", "2001:db8:99::7"],
                        "credentials": "NETWORK-CREDENTIAL-HIDDEN",
                    },
                    "facts_by_id": {
                        "route": {
                            "summary": "Reachable through 10.99.10.1.",
                            "gateway": "10.99.10.1",
                            "raw_scan": "FACT-SCAN-HIDDEN",
                        }
                    },
                    "services_by_name": {
                        "ssh": {
                            "endpoints": [
                                {
                                    "address": "10.99.10.5",
                                    "port": 22,
                                    "protocol": "tcp",
                                    "banner": "SERVICE-BANNER-HIDDEN",
                                }
                            ]
                        }
                    },
                    "configured_services": ["ssh", "node-exporter"],
                    "hardware": {
                        "management_controller": {
                            "ip": "172.16.0.6",
                            "name": "IDRAC-HIDDEN",
                            "status": "licensed-premium-hidden",
                        },
                        "serial": "HARDWARE-HIDDEN",
                    },
                    "credentials": {
                        "name": "VAULT-NAME-HIDDEN",
                        "endpoint": {"ip": "198.51.100.2"},
                    },
                    "filesystem": {
                        "artifacts_by_path": {
                            "/capture": {"content": "192.0.2.99 FILE-HIDDEN"}
                        }
                    },
                    "shell_environment": {"REMOTE_ADDR": "203.0.113.44"},
                    "command_history": [{"stdout": "198.51.100.99 HISTORY-HIDDEN"}],
                },
            },
        }

        projected = project_world_state_for_command_emulator(state)

        self.assertIn("10.99.10.5", projected["hosts_by_name"])
        peer = projected["hosts_by_name"]["10.99.10.5"]
        rendered = json.dumps(peer)
        for expected in (
            "10.99.10.5",
            "10.99.10.5/24",
            "2001:db8:99::5/64",
            "172.16.0.5",
            "172.16.0.6",
            "192.0.2.7",
            "198.51.100.2",
            "2001:db8:99::7",
            "10.99.10.1",
        ):
            self.assertIn(expected, rendered)
        self.assertEqual(peer["configured_services"], ["ssh", "node-exporter"])
        for hidden in (
            "NETWORK-HIDDEN",
            "NETWORK-CREDENTIAL-HIDDEN",
            "FACT-SCAN-HIDDEN",
            "SERVICE-BANNER-HIDDEN",
            "HARDWARE-HIDDEN",
            "IDRAC-HIDDEN",
            "licensed-premium-hidden",
            "VAULT-NAME-HIDDEN",
            "FILE-HIDDEN",
            "HISTORY-HIDDEN",
            "203.0.113.44",
        ):
            self.assertNotIn(hidden, rendered)

    def test_command_emulator_projection_can_promote_multiple_host_refs(self):
        state = {
            "active_host": "active-key",
            "hosts_by_name": {
                "active-key": {
                    "hostname": "active-host",
                    "filesystem": {"sentinel": "ACTIVE-FULL"},
                },
                "db-key": {
                    "hostname": "db01",
                    "aliases": ["database"],
                    "filesystem": {"sentinel": "ALIAS-PROMOTED"},
                },
                "router-key": {
                    "hostname": "router01",
                    "network": {"routes": [{"gateway": "10.0.0.8"}]},
                    "filesystem": {"sentinel": "ROUTER-NOT-PROMOTED"},
                },
                "cache-key": {
                    "hostname": "cache01",
                    "network": {
                        "interfaces": [{"ipv4": ["10.0.0.8/24"]}],
                    },
                    "filesystem": {"sentinel": "IP-PROMOTED"},
                },
                "db01": {
                    "hostname": "collision-host",
                    "filesystem": {"sentinel": "EXACT-KEY-PROMOTED"},
                },
            },
        }

        projected = project_world_state_for_command_emulator(
            state,
            detailed_host_refs=["database", "10.0.0.8", "db01"],
        )
        detailed_refs = detailed_host_registry_refs_for_command_emulator(
            state,
            detailed_host_refs=["database", "10.0.0.8", "db01"],
        )

        self.assertEqual(
            detailed_refs,
            ("active-key", "db-key", "cache-key", "db01"),
        )
        self.assertEqual(
            projected["hosts_by_name"]["active-key"]["filesystem"]["sentinel"],
            "ACTIVE-FULL",
        )
        self.assertEqual(
            projected["hosts_by_name"]["db-key"]["filesystem"]["sentinel"],
            "ALIAS-PROMOTED",
        )
        self.assertEqual(
            projected["hosts_by_name"]["cache-key"]["filesystem"]["sentinel"],
            "IP-PROMOTED",
        )
        self.assertNotIn("filesystem", projected["hosts_by_name"]["router-key"])
        self.assertEqual(
            projected["hosts_by_name"]["db01"]["filesystem"]["sentinel"],
            "EXACT-KEY-PROMOTED",
        )

    def test_command_emulator_projection_is_deep_copy_and_handles_malformed_hosts(self):
        state = {
            "active_host": "active",
            "world_facts_by_id": {"shared": {"summary": "unchanged"}},
            "hosts_by_name": {
                "active": ["malformed", {"nested": "active"}],
                "peer": "malformed peer payload",
            },
        }

        projected = project_world_state_for_command_emulator(state)

        self.assertEqual(
            projected["hosts_by_name"]["active"],
            ["malformed", {"nested": "active"}],
        )
        self.assertEqual(projected["hosts_by_name"]["peer"], {})
        projected["world_facts_by_id"]["shared"]["summary"] = "changed"
        projected["hosts_by_name"]["active"][1]["nested"] = "changed"
        self.assertEqual(
            state["world_facts_by_id"]["shared"]["summary"],
            "unchanged",
        )
        self.assertEqual(state["hosts_by_name"]["active"][1]["nested"], "active")

        malformed_hosts = ["invalid"]
        malformed_registry = {"name": "world", "hosts_by_name": malformed_hosts}
        malformed_projection = project_world_state_for_command_emulator(
            malformed_registry
        )
        self.assertEqual(malformed_projection, malformed_registry)
        self.assertIsNot(malformed_projection["hosts_by_name"], malformed_hosts)


if __name__ == "__main__":
    unittest.main()
