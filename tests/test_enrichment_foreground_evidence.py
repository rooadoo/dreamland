from __future__ import annotations

import unittest
from copy import deepcopy

from dreamland.background_agents.enrichment.foreground_evidence import (
    EXHAUSTIVE_OUTPUT_CHAR_LIMIT,
    HOST_TIME_OUTPUT_CHAR_LIMIT,
    capture_foreground_evidence,
)


def _entry(
    command_id: str,
    command: str,
    stdout: str,
    **overrides: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "id": command_id,
        "cwd": "/home/alice",
        "command": command,
        "stdout": stdout,
        "stderr": "",
        "exit_status": 0,
        "interrupted": False,
        "signal": None,
    }
    value.update(overrides)
    return value


class ForegroundEvidenceTests(unittest.TestCase):
    def test_unknown_time_and_empty_inventory_have_stable_private_digest(self) -> None:
        snapshot = capture_foreground_evidence({"command_history": []})

        self.assertEqual(snapshot.host_time_payload(), {"v": 1, "status": "unknown"})
        self.assertEqual(snapshot.recent_exhaustive_payload(), [])
        self.assertFalse(snapshot.host_time_observed)
        self.assertEqual(snapshot.recent_exhaustive_count, 0)
        self.assertEqual(len(snapshot.private_sha256), 64)
        self.assertEqual(
            snapshot.private_sha256,
            capture_foreground_evidence({}).private_sha256,
        )

    def test_latest_valid_host_time_uses_complete_history_not_recent_window(
        self,
    ) -> None:
        host = {
            "command_history": [
                _entry("date-old", "date", "Thu Nov 12 16:38:27 GST 2318\n"),
                _entry("ordinary-1", "pwd", "/home/alice\n"),
                _entry("date-query", "date --date yesterday", "yesterday\n"),
                _entry("date-failed", "date", "wrong\n", exit_status=1),
                _entry("ordinary-2", "id", "uid=1000(alice)\n"),
            ]
        }

        snapshot = capture_foreground_evidence(host, recent_history_limit=1)

        payload = snapshot.host_time_payload()
        self.assertEqual(payload["status"], "observed")
        self.assertEqual(payload["kind"], "recorded_host_time")
        self.assertEqual(payload["command_id"], "date-old")
        self.assertEqual(payload["output_excerpt"], "Thu Nov 12 16:38:27 GST 2318\n")
        self.assertTrue(snapshot.host_time_observed)
        self.assertIsNotNone(snapshot.private_sha256)

    def test_safe_date_formats_and_timedatectl_status_are_recognized(self) -> None:
        commands = (
            ("date -u +%FT%TZ", "2026-08-09T12:00:00Z\n"),
            (
                "date +report-%Y-%m-%dT%H:%M:%S%z",
                "report-2318-11-12T16:38:27+0000\n",
            ),
            ("date +%s", "1100133507\n"),
            ("date +%F", "2318-11-12\n"),
            ("date +%D", "11/12/18\n"),
            ("date +%c", "Thu Nov 12 16:38:27 2318\n"),
            ("date +%x", "11/12/2318\n"),
            ("date -Iseconds", "2318-11-12T16:38:27+00:00\n"),
            ("timedatectl status", "Local time: Thu 2318-11-12 16:38 GST\n"),
        )
        for index, (command, output) in enumerate(commands):
            with self.subTest(command=command):
                snapshot = capture_foreground_evidence(
                    {"command_history": [_entry(f"c{index}", command, output)]}
                )
                self.assertEqual(snapshot.host_time_payload()["output_excerpt"], output)

    def test_host_time_rejects_shell_composition_mutation_and_bad_results(
        self,
    ) -> None:
        rejected = (
            _entry("pipe", "date | head -1", "now\n"),
            _entry("sequence", "date; who", "now\n"),
            _entry("set", "date --set 2318-11-12", "now\n"),
            _entry("reference", "date --reference /tmp/file", "now\n"),
            _entry("literal-format", "date +hello", "hello\n"),
            _entry("escaped-format", "date +%%Y", "%Y\n"),
            _entry("layout-format", "date +%n", "\n\n"),
            _entry("time-only-format", "date +%H:%M", "16:38\n"),
            _entry("month-day-format", "date +%m-%d", "11-12\n"),
            _entry("empty", "date", ""),
            _entry("stderr", "date", "now\n", stderr="warning\n"),
            _entry("interrupt", "date", "now\n", interrupted=True),
        )
        for entry in rejected:
            with self.subTest(command=entry["command"]):
                snapshot = capture_foreground_evidence({"command_history": [entry]})
                self.assertEqual(snapshot.host_time_payload()["status"], "unknown")
                self.assertEqual(
                    snapshot.private_sha256,
                    capture_foreground_evidence({}).private_sha256,
                )

    def test_strict_exhaustive_commands_expose_typed_coverage(self) -> None:
        examples = (
            (
                "ls -la /home/",
                "total 8\ndrwxr-xr-x alice\n",
                "home_directory_inventory",
                {"path": "/home", "entries": "including_hidden"},
            ),
            (
                "last",
                "alice pts/0 10.0.0.2\n\nwtmp begins Thu Nov 12 00:00:00 2318\n",
                "login_history",
                {"database": "wtmp"},
            ),
            (
                "w",
                "16:40 up 4 days, 1 user\nalice pts/0 10.0.0.2\n",
                "session_presence",
                {"sessions": "current"},
            ),
            (
                "ss -tunap",
                "Netid State Local Address Peer Address\n",
                "socket_snapshot",
                {"states": "all", "protocols": "tcp,udp", "family": "all"},
            ),
            (
                "ps -ef",
                "UID PID PPID C STIME TTY TIME CMD\nroot 1 0 0 00:00 ? init\n",
                "process_snapshot",
                {"processes": "all"},
            ),
        )
        for index, (command, output, kind, expected_coverage) in enumerate(examples):
            with self.subTest(command=command):
                snapshot = capture_foreground_evidence(
                    {"command_history": [_entry(f"c{index}", command, output)]}
                )
                self.assertEqual(snapshot.recent_exhaustive_count, 1)
                payload = snapshot.recent_exhaustive_payload()[0]
                self.assertEqual(payload["kind"], kind)
                self.assertEqual(payload["source"], "recorded_command_result")
                self.assertEqual(payload["command"], command)
                self.assertEqual(
                    {key: payload["coverage"][key] for key in expected_coverage},
                    expected_coverage,
                )

    def test_ambiguous_narrow_and_incomplete_commands_are_not_exhaustive(
        self,
    ) -> None:
        rejected = (
            _entry("ls-other", "ls /home/alice", "notes\n"),
            _entry("ls-hidden", "ls --hide=alice /home", "bob\n"),
            _entry("last-limit", "last -n 2", "alice pts/0\n"),
            _entry("last-cut", "last", "alice pts/0\n"),
            _entry("w-user", "w alice", "alice pts/0\n"),
            _entry("ss-filter", "ss state established", "tcp ESTAB\n"),
            _entry("ps-pid", "ps -p 1", "PID CMD\n1 init\n"),
            _entry("pipe", "ps -ef | grep ssh", "root 4 sshd\n"),
            _entry(
                "capsule",
                "ps -ef",
                '{"stdout":"root 4 sshd\\n","stderr":"",'
                '"exit_status":0,"state_update":{"hosts_by_name":{}}',
            ),
            _entry("failed", "ps -ef", "", exit_status=1),
            _entry("warning", "ss -a", "", stderr="permission denied\n"),
        )
        for entry in rejected:
            with self.subTest(command=entry["command"]):
                snapshot = capture_foreground_evidence({"command_history": [entry]})
                self.assertEqual(snapshot.recent_exhaustive_payload(), [])
                self.assertEqual(
                    snapshot.private_sha256,
                    capture_foreground_evidence({}).private_sha256,
                )

    def test_recent_window_expires_public_evidence_but_keeps_authority(
        self,
    ) -> None:
        host = {
            "command_history": [
                _entry("old-ps", "ps aux", "old process view\n"),
                _entry("new-ps", "ps -ef", "new process view\n"),
                _entry("ordinary", "pwd", "/home/alice\n"),
                _entry("ordinary-2", "id", "uid=1000(alice)\n"),
            ]
        }

        retained = capture_foreground_evidence(host, recent_history_limit=4)
        self.assertEqual(retained.recent_exhaustive_count, 1)
        self.assertEqual(
            retained.recent_exhaustive_payload()[0]["command_id"],
            "new-ps",
        )

        expired = capture_foreground_evidence(host, recent_history_limit=2)
        self.assertEqual(expired.recent_exhaustive_payload(), [])
        self.assertEqual(retained.private_sha256, expired.private_sha256)
        self.assertNotEqual(
            expired.private_sha256,
            capture_foreground_evidence({}).private_sha256,
        )

    def test_unrecognized_command_aging_inventory_out_keeps_authority(self) -> None:
        history = [
            _entry("ps", "ps -ef", "PID CMD\n1 init\n"),
            _entry("ordinary-1", "pwd", "/home/alice\n"),
            _entry("ordinary-2", "id", "uid=1000(alice)\n"),
            _entry("ordinary-3", "uname", "Linux\n"),
        ]
        before = capture_foreground_evidence({"command_history": history})

        history.append(_entry("ordinary-4", "pwd", "/home/alice\n"))
        after = capture_foreground_evidence({"command_history": history})

        self.assertEqual(before.recent_exhaustive_count, 1)
        self.assertEqual(after.recent_exhaustive_count, 0)
        self.assertEqual(before.private_sha256, after.private_sha256)

    def test_new_same_scope_observation_changes_authority(self) -> None:
        history = [
            _entry("ps-old", "ps -ef", "PID CMD\n1 init\n"),
            _entry("ordinary-1", "pwd", "/home/alice\n"),
            _entry("ordinary-2", "id", "uid=1000(alice)\n"),
            _entry("ordinary-3", "uname", "Linux\n"),
            _entry("ordinary-4", "pwd", "/home/alice\n"),
        ]
        before = capture_foreground_evidence({"command_history": history})

        # Even a repeated command with byte-identical output is a new
        # observation and must invalidate work bound to the prior command ID.
        history.append(_entry("ps-new", "ps -ef", "PID CMD\n1 init\n"))
        after = capture_foreground_evidence({"command_history": history})

        self.assertEqual(before.recent_exhaustive_payload(), [])
        self.assertEqual(after.recent_exhaustive_count, 1)
        self.assertEqual(
            after.recent_exhaustive_payload()[0]["command_id"],
            "ps-new",
        )
        self.assertNotEqual(before.private_sha256, after.private_sha256)

    def test_new_different_scope_observation_changes_authority(self) -> None:
        history = [
            _entry("ps", "ps -ef", "PID CMD\n1 init\n"),
            _entry("ordinary", "pwd", "/home/alice\n"),
        ]
        before = capture_foreground_evidence({"command_history": history})

        history.append(
            _entry(
                "ss",
                "ss -tunap",
                "Netid State Local Address Peer Address\n",
            )
        )
        after = capture_foreground_evidence({"command_history": history})

        self.assertNotEqual(before.private_sha256, after.private_sha256)

    def test_authority_ignores_superseded_records_within_one_scope(self) -> None:
        latest = _entry("ps-new", "ps -ef", "PID CMD\n1 init\n5 sshd\n")
        with_superseded = capture_foreground_evidence(
            {
                "command_history": [
                    _entry("ps-old", "ps aux", "PID CMD\n1 init\n"),
                    latest,
                ]
            }
        )
        latest_only = capture_foreground_evidence(
            {"command_history": [deepcopy(latest)]}
        )

        self.assertEqual(
            with_superseded.private_sha256,
            latest_only.private_sha256,
        )

    def test_authority_hash_is_deterministic_across_public_window_sizes(self) -> None:
        host = {
            "command_history": [
                _entry("date", "date", "Thu Nov 12 16:38:27 GST 2318\n"),
                _entry("ps", "ps -ef", "PID CMD\n1 init\n"),
                _entry(
                    "ss",
                    "ss -tunap",
                    "Netid State Local Address Peer Address\n",
                ),
                _entry("ordinary-1", "pwd", "/home/alice\n"),
                _entry("ordinary-2", "id", "uid=1000(alice)\n"),
                _entry("ordinary-3", "uname", "Linux\n"),
                _entry("ordinary-4", "pwd", "/home/alice\n"),
            ]
        }

        first = capture_foreground_evidence(host)
        repeated = capture_foreground_evidence(deepcopy(host))
        no_public_window = capture_foreground_evidence(host, recent_history_limit=0)

        self.assertEqual(first.recent_exhaustive_payload(), [])
        self.assertEqual(first.private_sha256, repeated.private_sha256)
        self.assertEqual(first.private_sha256, no_public_window.private_sha256)

    def test_truncated_inventory_is_not_typed_as_exhaustive_evidence(self) -> None:
        output = "process\n" * (EXHAUSTIVE_OUTPUT_CHAR_LIMIT // 4)
        snapshot = capture_foreground_evidence(
            {"command_history": [_entry("ps", "ps -ef", output)]}
        )

        self.assertGreater(len(output), EXHAUSTIVE_OUTPUT_CHAR_LIMIT)
        self.assertEqual(snapshot.recent_exhaustive_payload(), [])
        self.assertEqual(
            snapshot.private_sha256,
            capture_foreground_evidence({}).private_sha256,
        )

    def test_bounded_host_time_digest_covers_omitted_middle(self) -> None:
        prefix = "P" * 800
        suffix = "S" * 800
        first_output = prefix + ("A" * 2_000) + suffix
        second_output = prefix + ("B" * 2_000) + suffix
        first_host = {"command_history": [_entry("date", "date", first_output)]}
        second_host = deepcopy(first_host)
        second_host["command_history"][0]["stdout"] = second_output

        first = capture_foreground_evidence(first_host)
        second = capture_foreground_evidence(second_host)

        first_payload = first.host_time_payload()
        second_payload = second.host_time_payload()
        self.assertLessEqual(
            len(first_payload["output_excerpt"]),
            HOST_TIME_OUTPUT_CHAR_LIMIT,
        )
        self.assertEqual(
            first_payload["output_excerpt"], second_payload["output_excerpt"]
        )
        self.assertGreater(first_payload["output_omitted_chars"], 0)
        self.assertNotEqual(first.private_sha256, second.private_sha256)
        self.assertNotIn("A" * 100, repr(first))


if __name__ == "__main__":
    unittest.main()
