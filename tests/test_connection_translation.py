import copy
import unittest

from dreamland.connection_translation import translated_ssh_target


def world_with_rule(**changes):
    rule = {
        "source_host": "workstation",
        "target": "visitor.anchor.bsides",
        "target_port": 2222,
        "destination_host": "visitor-server",
        "destination": {"ip": "10.184.0.8", "port": 22},
        "source": {"ip": "203.0.113.66"},
        **changes,
    }
    return {
        "hosts_by_name": {
            "workstation-key": {
                "hostname": "workstation",
                "aliases": ["client"],
                "network": {"ipv4_address": "10.72.24.17"},
            },
            "visitor-server": {
                "hostname": "visitor-server",
                "network": {"ipv4_address": "10.184.0.8"},
            },
            "gateway": {
                "hostname": "gateway",
                "network": {"ipv4_address": "198.51.100.166"},
            },
        },
        "world_facts_by_id": {
            "network-anchor": {
                "category": "network",
                "targets": {
                    "visitor.anchor.bsides": "198.51.100.166",
                    "alternate.anchor.bsides": "198.51.100.166",
                },
            },
            "network-ssh-translation": {
                "category": "network",
                "connection_translation": rule,
            },
        },
    }


class ConnectionTranslationTests(unittest.TestCase):
    def test_explicit_snat_and_dnat_return_normalized_endpoints_without_mutation(self):
        world = world_with_rule()
        before = copy.deepcopy(world)
        result = translated_ssh_target(world, "client", "VISITOR.ANCHOR.BSIDES.", 2222)
        self.assertEqual(result["source_host"], "workstation-key")
        self.assertEqual(result["destination_host"], "visitor-server")
        self.assertEqual(result["destination"], {"ip": "10.184.0.8", "port": 22})
        self.assertEqual(result["source"], {"ip": "203.0.113.66"})
        self.assertEqual(result["fact_id"], "network-ssh-translation")
        self.assertEqual(result["target_address"], "198.51.100.166")
        self.assertEqual(world, before)

    def test_numeric_and_mapped_dns_aliases_match_same_public_target(self):
        for target in ("198.51.100.166", "alternate.anchor.bsides"):
            with self.subTest(target=target):
                self.assertIsNotNone(
                    translated_ssh_target(
                        world_with_rule(), "workstation-key", target, 2222
                    )
                )

    def test_unrelated_source_target_or_port_does_not_match(self):
        for source, target, port in (
            ("other", "visitor.anchor.bsides", 2222),
            ("client", "unknown.bsides", 2222),
            ("client", "visitor.anchor.bsides", 22),
        ):
            with self.subTest(source=source, target=target, port=port):
                self.assertIsNone(
                    translated_ssh_target(world_with_rule(), source, target, port)
                )

    def test_optional_source_scope_and_target_port(self):
        world = world_with_rule()
        rule = world["world_facts_by_id"]["network-ssh-translation"][
            "connection_translation"
        ]
        del rule["source_host"]
        del rule["target_port"]
        result = translated_ssh_target(
            world, "another-source", "visitor.anchor.bsides", None
        )
        self.assertNotIn("source_host", result)
        self.assertNotIn("target_port", result)

    def test_explicit_source_port_is_preserved(self):
        result = translated_ssh_target(
            world_with_rule(source={"ip": "203.0.113.66", "port": 61234}),
            "client",
            "visitor.anchor.bsides",
            2222,
        )
        self.assertEqual(result["source"]["port"], 61234)

    def test_missing_destination_host_can_be_established_by_explicit_mapping(self):
        world = world_with_rule(
            destination_host="new-private-host",
            destination={"ip": "10.184.0.9", "port": 22},
        )
        result = translated_ssh_target(world, "client", "visitor.anchor.bsides", 2222)
        self.assertEqual(result["destination_host"], "new-private-host")
        self.assertNotIn("new-private-host", world["hosts_by_name"])

    def test_dnat_cannot_renumber_existing_host_or_take_another_hosts_address(self):
        for address in ("10.184.0.9", "10.72.24.17"):
            with self.subTest(address=address):
                self.assertIsNone(
                    translated_ssh_target(
                        world_with_rule(destination={"ip": address}),
                        "client",
                        "visitor.anchor.bsides",
                        2222,
                    )
                )

    def test_invalid_endpoint_addresses_and_ports_are_rejected(self):
        for endpoint in (
            {"ip": "not-an-ip"},
            {"ip": "0.0.0.0"},
            {"ip": "127.0.0.1"},
            {"ip": "203.0.113.66", "port": 0},
            {"ip": "203.0.113.66", "port": 65536},
            {"ip": "203.0.113.66", "port": True},
            {"ip": "203.0.113.66", "port": "22"},
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIsNone(
                    translated_ssh_target(
                        world_with_rule(source=endpoint),
                        "client",
                        "visitor.anchor.bsides",
                        2222,
                    )
                )

    def test_cross_family_translation_is_rejected(self):
        self.assertIsNone(
            translated_ssh_target(
                world_with_rule(source={"ip": "fdab::1"}),
                "client",
                "visitor.anchor.bsides",
                2222,
            )
        )

    def test_multiple_matching_rules_are_ambiguous(self):
        world = world_with_rule()
        world["world_facts_by_id"]["network-conflicting"] = copy.deepcopy(
            world["world_facts_by_id"]["network-ssh-translation"]
        )
        self.assertIsNone(
            translated_ssh_target(world, "client", "visitor.anchor.bsides", 2222)
        )

    def test_prose_and_unrelated_categories_are_not_interpreted_as_nat(self):
        world = world_with_rule()
        fact = world["world_facts_by_id"]["network-ssh-translation"]
        fact["category"] = "notes"
        fact["description"] = "Source NAT is 203.0.113.66."
        self.assertIsNone(
            translated_ssh_target(world, "client", "visitor.anchor.bsides", 2222)
        )

    def test_snat_only_rule_retains_actual_target_endpoint(self):
        world = world_with_rule(destination_host="gateway")
        del world["world_facts_by_id"]["network-ssh-translation"][
            "connection_translation"
        ]["destination"]
        result = translated_ssh_target(world, "client", "visitor.anchor.bsides", 2222)
        self.assertEqual(result["destination"], {"ip": "198.51.100.166"})
        self.assertNotIn("port", result["source"])

    def test_ipv6_snat_selects_known_ipv6_target_on_dual_stack_host(self):
        world = world_with_rule(
            target="fdab::8",
            destination_host="visitor-server",
            source={"ip": "fd12::5"},
        )
        rule = world["world_facts_by_id"]["network-ssh-translation"][
            "connection_translation"
        ]
        del rule["destination"]
        world["hosts_by_name"]["visitor-server"]["network"]["secondary_addresses"] = [
            "fdab::8"
        ]
        result = translated_ssh_target(world, "client", "fdab::8", 2222)
        self.assertEqual(result["destination"], {"ip": "fdab::8"})

    def test_dnat_requires_known_public_target_address(self):
        world = world_with_rule(target="unknown.anchor.bsides")
        self.assertIsNone(
            translated_ssh_target(world, "client", "unknown.anchor.bsides", 2222)
        )

    def test_snat_only_can_use_known_destination_for_unmapped_target_name(self):
        world = world_with_rule(
            target="unmapped.anchor.bsides", destination_host="gateway"
        )
        del world["world_facts_by_id"]["network-ssh-translation"][
            "connection_translation"
        ]["destination"]
        result = translated_ssh_target(world, "client", "unmapped.anchor.bsides", 2222)
        self.assertEqual(result["target_address"], "198.51.100.166")


if __name__ == "__main__":
    unittest.main()
