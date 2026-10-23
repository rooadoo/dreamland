from __future__ import annotations

import unittest

from dreamland.background_agents.focus import (
    MAX_FOCUS_ARTIFACTS,
    MAX_FOCUS_PATHS,
    InteractionFocusMatch,
    PresenceProbeKind,
    classify_interaction_focus_match,
    classify_presence_probe,
    infer_command_focus,
    interaction_focus_counts,
    interaction_focus_key,
    interaction_focus_subject_key,
)


class BackgroundAgentFocusTests(unittest.TestCase):
    def test_relative_filesystem_operand_uses_starting_directory(self) -> None:
        focus = infer_command_focus(
            "cat notes/route.txt",
            starting_cwd="/home/scott",
            result_cwd="/home/scott",
        )

        self.assertEqual(focus["paths"], ("/home/scott/notes/route.txt",))
        self.assertEqual(
            focus["primary"], {"kind": "path", "id": "/home/scott/notes/route.txt"}
        )
        self.assertEqual(
            interaction_focus_key(focus), "path:/home/scott/notes/route.txt"
        )

    def test_successful_cd_uses_result_directory_without_double_joining(self) -> None:
        focus = infer_command_focus(
            "cd warm",
            starting_cwd="/var/lib/reliquary",
            result_cwd="/var/lib/reliquary/warm",
        )

        self.assertEqual(focus["cwd"], "/var/lib/reliquary")
        self.assertEqual(focus["result_cwd"], "/var/lib/reliquary/warm")
        self.assertEqual(focus["paths"], ("/var/lib/reliquary/warm",))

    def test_path_patterns_are_hints_but_not_concrete_paths(self) -> None:
        focus = infer_command_focus(
            "cat route*",
            starting_cwd="/home/scott",
            result_cwd="/home/scott",
        )

        self.assertNotIn("paths", focus)
        self.assertEqual(
            focus["artifacts"],
            ({"kind": "path_pattern", "id": "/home/scott/route*"},),
        )

    def test_option_values_are_not_mistaken_for_paths_or_endpoints(self) -> None:
        file_focus = infer_command_focus(
            "head -n 5 logs/current.log",
            starting_cwd="/srv/app",
            result_cwd="/srv/app",
        )
        network_focus = infer_command_focus(
            "ping -c 4 cache.internal",
            starting_cwd="/srv/app",
            result_cwd="/srv/app",
        )

        self.assertEqual(file_focus["paths"], ("/srv/app/logs/current.log",))
        self.assertEqual(
            network_focus["artifacts"],
            ({"kind": "network_endpoint", "id": "cache.internal"},),
        )

    def test_service_process_and_network_identifiers_are_typed(self) -> None:
        service = infer_command_focus(
            "systemctl status catalogd reliquary-mirrord",
            starting_cwd="/",
            result_cwd="/",
        )
        process = infer_command_focus(
            "pgrep catalogd",
            starting_cwd="/",
            result_cwd="/",
        )
        network = infer_command_focus(
            "ssh operator@library-mirror-2",
            starting_cwd="/",
            result_cwd="/",
        )

        self.assertEqual(
            service["artifacts"],
            (
                {"kind": "service", "id": "catalogd"},
                {"kind": "service", "id": "reliquary-mirrord"},
            ),
        )
        self.assertEqual(
            process["artifacts"],
            ({"kind": "process", "id": "catalogd"},),
        )
        self.assertEqual(
            network["artifacts"],
            ({"kind": "network_endpoint", "id": "operator@library-mirror-2"},),
        )

    def test_malformed_command_retains_only_directory_context(self) -> None:
        focus = infer_command_focus(
            "cat 'unterminated",
            starting_cwd="/tmp",
            result_cwd="/tmp",
        )

        self.assertEqual(
            focus,
            {
                "cwd": "/tmp",
                "result_cwd": "/tmp",
                "primary": {"kind": "working_directory", "id": "/tmp"},
                "key": "working_directory:/tmp",
            },
        )

    def test_focus_collections_are_bounded(self) -> None:
        paths = " ".join(f"file-{index}" for index in range(10))
        artifacts = " ".join(f"service-{index}" for index in range(10))
        path_focus = infer_command_focus(
            f"cat {paths}",
            starting_cwd="/tmp",
            result_cwd="/tmp",
        )
        artifact_focus = infer_command_focus(
            f"systemctl status {artifacts}",
            starting_cwd="/tmp",
            result_cwd="/tmp",
        )

        self.assertEqual(len(path_focus["paths"]), MAX_FOCUS_PATHS)
        self.assertEqual(len(artifact_focus["artifacts"]), MAX_FOCUS_ARTIFACTS)
        self.assertEqual(
            interaction_focus_counts(path_focus),
            (MAX_FOCUS_PATHS, 0),
        )
        self.assertEqual(
            interaction_focus_counts(artifact_focus),
            (0, MAX_FOCUS_ARTIFACTS),
        )

    def test_exact_concrete_path_is_an_artifact_match(self) -> None:
        expected = infer_command_focus(
            "cat notes/route.txt",
            starting_cwd="/home/scott",
            result_cwd="/home/scott",
        )
        observed = infer_command_focus(
            "stat /home/scott/notes/route.txt",
            starting_cwd="/tmp",
            result_cwd="/tmp",
        )

        self.assertEqual(
            classify_interaction_focus_match(expected, observed),
            InteractionFocusMatch.ARTIFACT_EXACT,
        )

    def test_exact_secondary_typed_artifact_is_an_artifact_match(self) -> None:
        expected = infer_command_focus(
            "systemctl status catalogd reliquary-mirrord",
            starting_cwd="/",
            result_cwd="/",
        )
        observed = infer_command_focus(
            "journalctl -u reliquary-mirrord",
            starting_cwd="/",
            result_cwd="/",
        )

        self.assertEqual(
            classify_interaction_focus_match(expected, observed),
            InteractionFocusMatch.ARTIFACT_EXACT,
        )

    def test_direct_path_ancestry_is_related_but_siblings_are_surface_only(
        self,
    ) -> None:
        directory = infer_command_focus(
            "ls /home/scott/.ssh",
            starting_cwd="/",
            result_cwd="/",
        )
        descendant = infer_command_focus(
            "cat /home/scott/.ssh/known_hosts",
            starting_cwd="/",
            result_cwd="/",
        )
        sibling = infer_command_focus(
            "cat /home/scott/notes.txt",
            starting_cwd="/",
            result_cwd="/",
        )

        self.assertEqual(
            classify_interaction_focus_match(directory, descendant),
            InteractionFocusMatch.PATH_RELATED,
        )
        self.assertEqual(
            classify_interaction_focus_match(descendant, sibling),
            InteractionFocusMatch.SURFACE_ONLY,
        )

    def test_path_pattern_match_is_path_related(self) -> None:
        pattern = infer_command_focus(
            "cat /var/log/catalog/*.log",
            starting_cwd="/",
            result_cwd="/",
        )
        concrete = infer_command_focus(
            "tail /var/log/catalog/current.log",
            starting_cwd="/",
            result_cwd="/",
        )

        self.assertEqual(
            classify_interaction_focus_match(pattern, concrete),
            InteractionFocusMatch.PATH_RELATED,
        )

    def test_working_directory_alone_does_not_create_an_exact_match(self) -> None:
        expected = infer_command_focus(
            "ps -ef",
            starting_cwd="/srv/catalog",
            result_cwd="/srv/catalog",
        )
        observed = infer_command_focus(
            "id",
            starting_cwd="/srv/catalog",
            result_cwd="/srv/catalog",
        )

        self.assertEqual(
            classify_interaction_focus_match(expected, observed),
            InteractionFocusMatch.SURFACE_ONLY,
        )

    def test_windows_paths_compare_case_insensitively_with_drive_boundaries(
        self,
    ) -> None:
        expected = {"paths": (r"C:\Users\Scott\Notes",)}
        same = {"paths": (r"c:\users\scott\notes",)}
        descendant = {"paths": (r"C:\Users\Scott\Notes\today.txt",)}
        other_drive = {"paths": (r"D:\Users\Scott\Notes\today.txt",)}

        self.assertEqual(
            classify_interaction_focus_match(expected, same),
            InteractionFocusMatch.ARTIFACT_EXACT,
        )
        self.assertEqual(
            classify_interaction_focus_match(expected, descendant),
            InteractionFocusMatch.PATH_RELATED,
        )
        self.assertEqual(
            classify_interaction_focus_match(expected, other_drive),
            InteractionFocusMatch.SURFACE_ONLY,
        )

    def test_filesystem_roots_are_not_universal_path_matches(self) -> None:
        self.assertEqual(
            classify_interaction_focus_match(
                {"paths": ("/",)},
                {"paths": ("/etc/hosts",)},
            ),
            InteractionFocusMatch.SURFACE_ONLY,
        )
        self.assertEqual(
            classify_interaction_focus_match(
                {"paths": ("C:\\",)},
                {"paths": (r"C:\Windows\System32\drivers\etc\hosts",)},
            ),
            InteractionFocusMatch.SURFACE_ONLY,
        )

    def test_primary_artifact_fallback_is_bounded_and_malformed_focus_is_safe(
        self,
    ) -> None:
        expected = {
            "primary": {"kind": "service", "id": "catalogd"},
            "key": "service:catalogd",
        }
        observed = {
            "artifacts": ({"kind": "service", "id": "catalogd"},),
        }

        self.assertEqual(
            classify_interaction_focus_match(expected, observed),
            InteractionFocusMatch.ARTIFACT_EXACT,
        )
        self.assertEqual(
            classify_interaction_focus_match(
                {"paths": "not-a-collection", "artifacts": (None, 12)},
                None,
            ),
            InteractionFocusMatch.SURFACE_ONLY,
        )

    def test_presence_probe_classifies_broad_population_surfaces(self) -> None:
        expected = {
            "who": PresenceProbeKind.SESSION_PRESENCE,
            "last -n 20": PresenceProbeKind.LOGIN_HISTORY,
            "ps -ef": PresenceProbeKind.PROCESS_ACTIVITY,
            "ss -tunap": PresenceProbeKind.NETWORK_ACTIVITY,
            "lsof -i": PresenceProbeKind.NETWORK_ACTIVITY,
            "systemctl list-units": PresenceProbeKind.SERVICE_ACTIVITY,
            "systemctl list-timers --all": PresenceProbeKind.SCHEDULED_ACTIVITY,
            "journalctl --since today": PresenceProbeKind.EVENT_ACTIVITY,
        }

        self.assertEqual(
            {command: classify_presence_probe(command) for command in expected},
            expected,
        )

    def test_presence_probe_excludes_recognizable_narrow_queries(self) -> None:
        for command in (
            "who am i",
            "last alice",
            "ps -p 42",
            "top -p 42",
            "systemctl status sshd",
            "journalctl -u sshd",
        ):
            with self.subTest(command=command):
                self.assertIsNone(classify_presence_probe(command))

    def test_first_touch_subject_excludes_working_directory_only_focus(self) -> None:
        self.assertIsNone(
            interaction_focus_subject_key(
                {
                    "primary": {"kind": "working_directory", "id": "/home/alice"},
                    "key": "working_directory:/home/alice",
                }
            )
        )
        self.assertEqual(
            interaction_focus_subject_key(
                {
                    "primary": {"kind": "service", "id": "catalogd"},
                    "key": "service:catalogd",
                }
            ),
            "service:catalogd",
        )


if __name__ == "__main__":
    unittest.main()
