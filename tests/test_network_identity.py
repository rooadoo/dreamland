import copy
import ipaddress
import unittest

from dreamland.network_identity import (
    ensure_host_address,
    resolve_host_ref,
    resolve_target,
    source_address_for_target,
)
from dreamland.world_state import sync_active_host


class NetworkIdentityTests(unittest.TestCase):
    def test_existing_profile_scalar_address_is_preserved(self):
        world = {
            "hosts_by_name": {
                "hearth-c17": {
                    "network": {
                        "ipv4_address": "10.72.24.17",
                        "prefix_length": 24,
                        "fqdn": "hearth-c17.common-ground.bsides",
                    }
                }
            }
        }
        before = copy.deepcopy(world)
        self.assertEqual(ensure_host_address(world, "hearth-c17"), "10.72.24.17")
        self.assertEqual(
            resolve_host_ref(world, "HEARTH-C17.COMMON-GROUND.BSIDES."), "hearth-c17"
        )
        self.assertEqual(world, before)

    def test_primary_interface_wins_over_loopback_and_list_order(self):
        world = {
            "hosts_by_name": {
                "host": {
                    "network": {
                        "primary_interface": "wan0",
                        "interfaces": [
                            {"name": "lo", "address": "::1/128"},
                            {"name": "lan0", "address": "10.2.3.4/24"},
                            {"name": "wan0", "address": "198.51.100.70/27"},
                        ],
                    }
                }
            }
        }
        self.assertEqual(ensure_host_address(world, "host"), "198.51.100.70")

    def test_keyed_interfaces_and_default_route_select_primary(self):
        world = {
            "hosts_by_name": {
                "host": {
                    "network": {
                        "interfaces": {
                            "lo": {"address": "127.0.0.1/8"},
                            "management": {"addresses": ["10.0.0.8/24"]},
                            "uplink": {"ipv4": {"address": "172.19.2.8/24"}},
                        },
                        "routes": [{"destination": "default", "dev": "uplink"}],
                    }
                }
            }
        }
        self.assertEqual(ensure_host_address(world, "host"), "172.19.2.8")

    def test_resolve_names_addresses_and_network_target_aliases(self):
        world = {
            "hosts_by_name": {
                "remote-key": {
                    "hostname": "remote",
                    "aliases": ["old-name"],
                    "network": {"interfaces": [{"address": "198.51.100.166/27"}]},
                }
            },
            "world_facts_by_id": {
                "network-anchor": {
                    "targets": {"welcome.anchor-mutual.bsides": "198.51.100.166"},
                }
            },
        }
        for name in (
            "remote-key",
            "remote",
            "old-name",
            "198.51.100.166",
            "welcome.anchor-mutual.bsides",
        ):
            with self.subTest(name=name):
                self.assertEqual(resolve_host_ref(world, name), "remote-key")
        self.assertIsNone(resolve_host_ref(world, "unknown"))

    def test_network_fact_materializes_target_under_returned_hostname(self):
        world = {
            "hosts_by_name": {},
            "world_facts_by_id": {
                "network-anchor": {
                    "targets": {"welcome.anchor-mutual.bsides": "198.51.100.166"},
                }
            },
        }
        result = resolve_target(
            world, "welcome.anchor-mutual.bsides", hostname="anchor-welcome-01"
        )
        self.assertEqual(result, ("anchor-welcome-01", "198.51.100.166"))
        self.assertEqual(resolve_target(world, "welcome.anchor-mutual.bsides"), result)
        self.assertEqual(
            resolve_host_ref(world, "anchor-welcome-01"), "anchor-welcome-01"
        )
        self.assertEqual(len(world["hosts_by_name"]), 1)

    def test_existing_target_identity_wins_and_keeps_returned_hostname_alias(self):
        world = {
            "hosts_by_name": {
                "target-key": {
                    "hostname": "target",
                    "network": {"ipv4_address": "10.9.0.2"},
                }
            }
        }
        self.assertEqual(
            resolve_target(world, "10.9.0.2", hostname="renamed"),
            ("target-key", "10.9.0.2"),
        )
        self.assertEqual(resolve_host_ref(world, "renamed"), "target-key")
        self.assertEqual(
            ensure_host_address(world, "target-key", preferred_address="10.9.0.99"),
            "10.9.0.2",
        )

    def test_explicit_secondary_target_does_not_replace_primary(self):
        world = {
            "hosts_by_name": {
                "host": {
                    "network": {"ipv4_address": "10.4.0.8"},
                }
            }
        }
        self.assertEqual(
            resolve_target(world, "198.51.100.88", hostname="host"),
            ("host", "198.51.100.88"),
        )
        self.assertEqual(ensure_host_address(world, "host"), "10.4.0.8")
        self.assertEqual(resolve_host_ref(world, "198.51.100.88"), "host")

    def test_secondary_endpoint_preserves_primary_stored_in_interface(self):
        world = {
            "hosts_by_name": {
                "host": {
                    "network": {
                        "interfaces": [{"name": "eth0", "address": "10.4.0.8/24"}],
                    }
                }
            }
        }
        self.assertEqual(
            resolve_target(world, "198.51.100.88", hostname="host"),
            ("host", "198.51.100.88"),
        )
        self.assertEqual(ensure_host_address(world, "host"), "10.4.0.8")
        self.assertEqual(resolve_host_ref(world, "198.51.100.88"), "host")

    def test_longest_route_selects_source_and_direct_route_beats_default(self):
        world = {
            "hosts_by_name": {
                "host": {
                    "network": {
                        "interfaces": {
                            "management": {"address": "10.0.0.8/24"},
                            "uplink": {"address": "172.19.2.8/24"},
                            "vpn": {"address": "10.8.0.2/24"},
                        },
                        "routes": [
                            {"destination": "default", "dev": "uplink"},
                            {"destination": "192.168.40.0/24", "dev": "vpn"},
                            {"destination": "192.168.40.12/32", "src": "10.8.0.3"},
                        ],
                    }
                }
            }
        }
        self.assertEqual(
            source_address_for_target(world, "host", "203.0.113.55"), "172.19.2.8"
        )
        self.assertEqual(
            source_address_for_target(world, "host", "192.168.40.10"), "10.8.0.2"
        )
        self.assertEqual(
            source_address_for_target(world, "host", "192.168.40.12"), "10.8.0.3"
        )
        self.assertEqual(
            source_address_for_target(world, "host", "10.0.0.12"), "10.0.0.8"
        )

    def test_ipv6_source_matches_family_and_preserves_primary_ipv4(self):
        world = {
            "hosts_by_name": {
                "host": {
                    "network": {
                        "ipv4_address": "10.0.0.8",
                        "interfaces": {
                            "eth0": {"addresses": ["::1/128", "fd12:3456::8/64"]}
                        },
                    }
                }
            }
        }
        self.assertEqual(
            source_address_for_target(world, "host", "fd12:3456::10"), "fd12:3456::8"
        )
        self.assertEqual(ensure_host_address(world, "host"), "10.0.0.8")

    def test_localhost_uses_active_host_and_loopback_endpoints(self):
        world = {
            "active_host": "host",
            "hosts_by_name": {
                "host": {
                    "network": {"ipv4_address": "10.0.0.8"},
                }
            },
        }
        self.assertEqual(
            resolve_target(world, "localhost", hostname="host"), ("host", "127.0.0.1")
        )
        self.assertEqual(
            resolve_target(world, "[::1]", hostname="host"), ("host", "::1")
        )
        self.assertEqual(source_address_for_target(world, "host", "::1"), "::1")
        self.assertEqual(ensure_host_address(world, "host"), "10.0.0.8")

    def test_allocation_reuses_explicit_host_subnet_without_address_collisions(self):
        world = {
            "hosts_by_name": {
                "new": {
                    "network": {
                        "subnet": "10.72.24.0/29",
                        "default_gateway": "10.72.24.1",
                    }
                },
                "occupied": {"network": {"ipv4_address": "10.72.24.2"}},
            },
            "world_facts_by_id": {
                "network-test": {"targets": {"reserved": "10.72.24.3"}}
            },
        }
        result = ensure_host_address(world, "new")
        self.assertIn(
            ipaddress.ip_address(result), ipaddress.ip_network("10.72.24.0/29")
        )
        self.assertNotIn(
            result,
            {"10.72.24.0", "10.72.24.1", "10.72.24.2", "10.72.24.3", "10.72.24.7"},
        )
        self.assertEqual(ensure_host_address(world, "new"), result)

    def test_domain_allocation_is_used_before_inventing_another_network(self):
        world = {
            "hosts_by_name": {},
            "world_facts_by_id": {
                "network-test": {
                    "domain": "example.bsides",
                    "private_allocation": "10.72.0.0/16",
                    "public_allocation": "198.51.100.160/27",
                }
            },
        }
        ref, address = resolve_target(world, "new.example.bsides", hostname="new")
        self.assertEqual(ref, "new")
        self.assertIn(
            ipaddress.ip_address(address), ipaddress.ip_network("10.72.0.0/16")
        )

    def test_new_private_allocations_are_stable_and_do_not_overlap_recorded_networks(
        self,
    ):
        world = {
            "name": "scenario",
            "hosts_by_name": {},
            "world_facts_by_id": {
                "network-existing": {"private_allocation": "10.0.0.0/9"},
            },
        }
        first = ensure_host_address(world, "first")
        second = ensure_host_address(world, "second")
        self.assertNotEqual(first, second)
        for ref, address in (("first", first), ("second", second)):
            self.assertNotIn(
                ipaddress.ip_address(address), ipaddress.ip_network("10.0.0.0/9")
            )
            self.assertEqual(ensure_host_address(world, ref), address)
            self.assertIn(
                "runtime_address_allocation", world["hosts_by_name"][ref]["network"]
            )

    def test_unmapped_public_name_uses_approved_pool_and_excludes_real_anycast(self):
        approved = [
            ipaddress.ip_network(value)
            for value in (
                "100.64.0.0/10",
                "192.88.99.0/24",
                "192.0.0.0/24",
                "198.18.0.0/15",
                "198.51.100.0/24",
                "203.0.113.0/24",
                "240.0.0.0/4",
            )
        ]
        world = {"hosts_by_name": {}}
        _, address = resolve_target(world, "visitor.new-company.bsides")
        self.assertTrue(any(ipaddress.ip_address(address) in pool for pool in approved))
        self.assertNotIn(address, {"192.0.0.9", "192.0.0.10", "255.255.255.255"})

    def test_default_route_never_becomes_an_allocation_pool(self):
        world = {
            "hosts_by_name": {
                "host": {
                    "network": {
                        "routes": [{"destination": "0.0.0.0/0", "via": "10.72.24.1"}],
                    }
                }
            }
        }
        address = ensure_host_address(world, "host")
        self.assertIn(ipaddress.ip_address(address), ipaddress.ip_network("10.0.0.0/8"))

    def test_missing_ipv6_source_is_materialized_once(self):
        world = {
            "name": "scenario",
            "hosts_by_name": {"host": {"network": {"ipv4_address": "10.0.0.8"}}},
        }
        result = source_address_for_target(world, "host", "fdab::123")
        self.assertEqual(ipaddress.ip_address(result).version, 6)
        self.assertEqual(source_address_for_target(world, "host", "fdab::456"), result)
        self.assertEqual(ensure_host_address(world, "host"), "10.0.0.8")

    def test_preferred_address_cannot_duplicate_another_host(self):
        world = {
            "hosts_by_name": {"existing": {"network": {"ipv4_address": "10.0.0.8"}}}
        }
        with self.assertRaisesRegex(ValueError, "another simulated host"):
            ensure_host_address(world, "new", preferred_address="10.0.0.8")

    def test_missing_primary_uses_known_gateway_subnet(self):
        world = {
            "hosts_by_name": {
                "host": {
                    "network": {
                        "default_gateway": "10.72.24.1",
                        "prefix_length": 24,
                    }
                }
            }
        }
        address = ensure_host_address(world, "host")
        self.assertIn(
            ipaddress.ip_address(address), ipaddress.ip_network("10.72.24.0/24")
        )
        self.assertNotEqual(address, "10.72.24.1")

    def test_default_route_source_is_a_known_primary_address(self):
        world = {
            "hosts_by_name": {
                "host": {
                    "network": {
                        "routes": [{"destination": "default", "src": "10.72.24.17"}],
                    }
                }
            }
        }
        self.assertEqual(ensure_host_address(world, "host"), "10.72.24.17")

    def test_numeric_hostname_placeholder_cannot_steal_an_existing_address(self):
        world = {
            "active_host": "entry",
            "hosts_by_name": {
                "entry": {
                    "hostname": "entry",
                    "network": {"ipv4_address": "10.0.0.10"},
                },
                "remote": {
                    "hostname": "remote",
                    "network": {"ipv4_address": "10.0.0.20"},
                },
            },
        }
        world = sync_active_host(world, "10.0.0.20")
        self.assertEqual(
            resolve_target(world, "10.0.0.20", hostname="10.0.0.20"),
            ("remote", "10.0.0.20"),
        )
        self.assertEqual(set(world["hosts_by_name"]), {"entry", "remote"})
        self.assertEqual(world["active_host"], "remote")
        self.assertEqual(sync_active_host(world, "10.0.0.20")["active_host"], "remote")

    def test_authenticated_hostname_reuses_pending_target_and_removes_empty_placeholder(
        self,
    ):
        world = {"hosts_by_name": {}}
        ref, address = resolve_target(world, "visitor.new.bsides")
        world = sync_active_host(world, "new-server")
        self.assertEqual(
            resolve_target(world, address, hostname="new-server"), (ref, address)
        )
        self.assertNotIn("new-server", world["hosts_by_name"])
        self.assertEqual(world["active_host"], ref)
        self.assertEqual(sync_active_host(world, "new-server")["active_host"], ref)

    def test_target_alias_placeholder_cannot_hide_known_network_fact_address(self):
        world = {
            "hosts_by_name": {
                "remote": {
                    "hostname": "remote",
                    "network": {"ipv4_address": "10.0.0.20"},
                }
            },
            "world_facts_by_id": {
                "network-test": {"targets": {"visitor.example.bsides": "10.0.0.20"}},
            },
        }
        world = sync_active_host(world, "visitor.example.bsides")
        self.assertEqual(
            resolve_target(world, "visitor.example.bsides"), ("remote", "10.0.0.20")
        )
        self.assertEqual(set(world["hosts_by_name"]), {"remote"})

    def test_conflicting_populated_hostname_is_preserved(self):
        independent = {
            "hostname": "other",
            "network": {"ipv4_address": "10.0.0.30"},
            "purpose": "independent host",
        }
        world = {
            "hosts_by_name": {
                "remote": {
                    "hostname": "remote",
                    "network": {"ipv4_address": "10.0.0.20"},
                },
                "other": copy.deepcopy(independent),
            }
        }
        self.assertEqual(
            resolve_target(world, "10.0.0.20", hostname="other"),
            ("remote", "10.0.0.20"),
        )
        self.assertEqual(world["hosts_by_name"]["other"], independent)
        self.assertEqual(resolve_host_ref(world, "other"), "other")

    def test_new_address_family_preserves_primary_interface_address(self):
        world = {
            "hosts_by_name": {
                "host": {
                    "network": {
                        "interfaces": {
                            "eth0": {"address": "fdab::12/64"},
                        }
                    }
                }
            }
        }
        self.assertEqual(ensure_host_address(world, "host"), "fdab::12")
        self.assertEqual(
            resolve_target(world, "10.0.0.20", hostname="host"), ("host", "10.0.0.20")
        )
        self.assertEqual(ensure_host_address(world, "host"), "fdab::12")
        self.assertEqual(
            source_address_for_target(world, "host", "10.0.0.30"), "10.0.0.20"
        )

    def test_service_mappings_preserve_existing_network_target_addresses(self):
        world = {
            "hosts_by_name": {},
            "world_facts_by_id": {
                "network-corporate": {
                    "service_mappings": {"core.corporate.bsides": "10.64.10.1"},
                }
            },
        }
        self.assertEqual(
            resolve_target(world, "core.corporate.bsides", hostname="core"),
            ("core", "10.64.10.1"),
        )


if __name__ == "__main__":
    unittest.main()
