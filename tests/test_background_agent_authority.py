from __future__ import annotations

import json
import unittest
from copy import deepcopy

from dreamland.background_agents.authority import (
    FOREGROUND_AUTHORITY_CLASS,
    FOREGROUND_AUTHORITY_SCHEMA_VERSION,
    ForegroundAuthoritySnapshot,
    capture_foreground_authority,
)
from dreamland.models import (
    CommandHistoryEntry,
    HostIdentity,
    InteractiveProgram,
    SessionState,
    ShellEnvironment,
    ShellFrame,
)


def _session(
    *,
    cwd: str = "/srv/reliquary/catalogue",
    program: InteractiveProgram | None = None,
    stack_depth: int = 1,
) -> SessionState:
    active_program = program or InteractiveProgram(
        name="bash",
        command="/bin/bash",
        argv=("bash", "-l"),
        prompt_template="scott@reliquary-catalog-1:~$ ",
        metadata={"private_phase": "must-not-cross"},
    )
    environment = ShellEnvironment(
        variables={
            "SHELL": "/bin/bash",
            "USER": "scott",
            "LOGNAME": "scott",
            "HOME": "/home/scott",
            "HOSTNAME": "reliquary-catalog-1",
            "PWD": cwd,
            "PATH": "/private/bin:/usr/bin",
            "ACCESS_TOKEN": "must-not-cross",
        },
        exported=frozenset(
            {
                "SHELL",
                "USER",
                "LOGNAME",
                "HOME",
                "HOSTNAME",
                "PWD",
                "PATH",
                "ACCESS_TOKEN",
            }
        ),
    )
    frames = [
        ShellFrame(
            username="scott",
            cwd=cwd,
            interactive_program=active_program,
            shell_environment=environment,
            host_identity=HostIdentity(hostname="reliquary-catalog-1"),
            metadata={"private_frame_note": "must-not-cross"},
        )
        for _index in range(stack_depth)
    ]
    return SessionState(
        username="scott",
        login_username="console-login",
        host_identity=HostIdentity(hostname="reliquary-catalog-1"),
        cwd=cwd,
        interactive_program=active_program,
        shell_environment=environment,
        shell_stack=frames,
        history=[
            CommandHistoryEntry(
                cwd=cwd,
                command="cat /private/operator-command",
                stdout="private command output",
            )
        ],
        world_state={
            "active_host": "reliquary-catalog-1.chrysalis.internal",
            "hosts_by_name": {
                "reliquary-catalog-1.chrysalis.internal": {
                    "hostname": "reliquary-catalog-1",
                    "command_history": [
                        {
                            "command": "private world command",
                            "stdout": "private world output",
                        }
                    ],
                },
                "commons-gate-17": {"hostname": "commons-gate-17"},
            },
        },
    )


class BackgroundAgentAuthorityTests(unittest.TestCase):
    def test_capture_returns_bounded_public_evidence_for_active_target(self) -> None:
        authority = capture_foreground_authority(
            _session(stack_depth=2),
            target_host="reliquary-catalog-1.chrysalis.internal",
        )

        self.assertIsInstance(authority, ForegroundAuthoritySnapshot)
        assert authority is not None
        self.assertEqual(
            authority.public_payload(),
            {
                "v": FOREGROUND_AUTHORITY_SCHEMA_VERSION,
                "class": FOREGROUND_AUTHORITY_CLASS,
                "target_host": "reliquary-catalog-1.chrysalis.internal",
                "hostname": "reliquary-catalog-1",
                "active_username": "scott",
                "login_username": "console-login",
                "interactive_program": {
                    "name": "bash",
                    "command": "/bin/bash",
                    "argv": ["bash", "-l"],
                    "prompt_template": "scott@reliquary-catalog-1:~$ ",
                },
                "shell_environment": {
                    "SHELL": "/bin/bash",
                    "USER": "scott",
                    "LOGNAME": "scott",
                    "HOME": "/home/scott",
                    "HOSTNAME": "reliquary-catalog-1",
                },
                "shell_stack_depth": 2,
                "cwd": "/srv/reliquary/catalogue",
            },
        )
        serialized = json.dumps(authority.public_payload())
        for omitted in (
            "private_phase",
            "private_frame_note",
            "ACCESS_TOKEN",
            "/private/bin",
            "private world command",
            "private world output",
            "private/operator-command",
            "private command output",
        ):
            self.assertNotIn(omitted, serialized)

    def test_capture_returns_none_for_non_active_target(self) -> None:
        authority = capture_foreground_authority(
            _session(),
            target_host="commons-gate-17",
        )

        self.assertIsNone(authority)

    def test_semantics_digest_excludes_cwd_and_command_history(self) -> None:
        first_session = _session(cwd="/home/scott")
        second_session = _session(cwd="/srv/reliquary")
        second_session.history.append(
            CommandHistoryEntry(
                cwd="/srv/reliquary",
                command="another private command",
            )
        )
        second_session.world_state["hosts_by_name"][
            "reliquary-catalog-1.chrysalis.internal"
        ]["command_history"].append({"command": "yet another private command"})

        first = capture_foreground_authority(
            first_session,
            target_host="reliquary-catalog-1.chrysalis.internal",
        )
        second = capture_foreground_authority(
            second_session,
            target_host="reliquary-catalog-1.chrysalis.internal",
        )

        assert first is not None and second is not None
        self.assertNotEqual(
            first.public_payload()["cwd"], second.public_payload()["cwd"]
        )
        self.assertEqual(
            first.session_semantics_payload(),
            second.session_semantics_payload(),
        )
        self.assertEqual(
            first.session_semantics_sha256,
            second.session_semantics_sha256,
        )

    def test_program_transition_changes_semantics_digest(self) -> None:
        bash = capture_foreground_authority(
            _session(),
            target_host="reliquary-catalog-1.chrysalis.internal",
        )
        catalogue = capture_foreground_authority(
            _session(
                program=InteractiveProgram(
                    name="catalogue",
                    command="/usr/local/libexec/reliquary-catalog-session",
                    argv=("catalogue",),
                    prompt_template="catalogue> ",
                )
            ),
            target_host="reliquary-catalog-1.chrysalis.internal",
        )

        assert bash is not None and catalogue is not None
        self.assertNotEqual(
            bash.session_semantics_sha256,
            catalogue.session_semantics_sha256,
        )

    def test_public_payload_is_detached_from_snapshot(self) -> None:
        authority = capture_foreground_authority(
            _session(),
            target_host="reliquary-catalog-1.chrysalis.internal",
        )
        assert authority is not None

        payload = authority.public_payload()
        payload["interactive_program"]["argv"].append("changed")
        payload["shell_environment"]["SHELL"] = "/bin/changed"

        fresh = authority.public_payload()
        self.assertEqual(fresh["interactive_program"]["argv"], ["bash", "-l"])
        self.assertEqual(fresh["shell_environment"]["SHELL"], "/bin/bash")

    def test_model_projection_bounds_and_redacts_credential_bearing_arguments(
        self,
    ) -> None:
        secret = "do-not-send-to-enrichment-models"
        session = _session(
            program=InteractiveProgram(
                name="mysql",
                command=f"mysql --password={secret}",
                argv=(
                    "mysql",
                    "--password",
                    secret,
                    f"--token={secret}",
                    f"mysql://alice:{secret}@db.example/catalogue",
                    f"-p{secret}",
                    *(f"argument-{index}-" + ("x" * 400) for index in range(20)),
                ),
                prompt_template="mysql> " + ("p" * 800),
            )
        )

        authority = capture_foreground_authority(
            session,
            target_host="reliquary-catalog-1.chrysalis.internal",
        )

        assert authority is not None
        payload = authority.public_payload()
        program = payload["interactive_program"]
        serialized = json.dumps(payload)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(secret, repr(authority))
        self.assertEqual(
            program["command"],
            "mysql --password=<redacted-credential-argument>",
        )
        self.assertLessEqual(len(program["argv"]), 12)
        self.assertTrue(any("redacted" in argument for argument in program["argv"]))
        self.assertTrue(all(len(argument) <= 256 for argument in program["argv"]))
        self.assertLessEqual(len(program["prompt_template"]), 512)

    def test_model_projection_redacts_common_assignment_and_uri_credentials(
        self,
    ) -> None:
        cases = (
            (
                "bash",
                "TOKEN=token-secret /bin/bash",
                ("TOKEN=token-secret", "/bin/bash"),
                "token-secret",
            ),
            (
                "psql",
                "psql postgresql://alice:postgres-secret@db.example/catalogue",
                (
                    "psql",
                    "postgresql://alice:postgres-secret@db.example/catalogue",
                ),
                "postgres-secret",
            ),
            (
                "redis-cli",
                "redis-cli -u redis://:redis-secret@cache.example/0",
                ("redis-cli", "-u", "redis://:redis-secret@cache.example/0"),
                "redis-secret",
            ),
            (
                "env",
                "env MYSQL_PWD=mysql-secret PGPASSWORD=pg-secret "
                "AWS_SECRET_ACCESS_KEY=aws-secret mysql",
                (
                    "env",
                    "MYSQL_PWD=mysql-secret",
                    "PGPASSWORD=pg-secret",
                    "AWS_SECRET_ACCESS_KEY=aws-secret",
                    "mysql",
                ),
                "mysql-secret",
            ),
        )

        for name, command, argv, primary_secret in cases:
            with self.subTest(name=name):
                authority = capture_foreground_authority(
                    _session(
                        program=InteractiveProgram(
                            name=name,
                            command=command,
                            argv=argv,
                            prompt_template=f"{name}> ",
                        )
                    ),
                    target_host="reliquary-catalog-1.chrysalis.internal",
                )

                assert authority is not None
                serialized = json.dumps(authority.public_payload())
                self.assertNotIn(primary_secret, serialized)
                self.assertNotIn("pg-secret", serialized)
                self.assertNotIn("aws-secret", serialized)
                self.assertIn("redacted-credential", serialized)

    def test_private_semantics_digest_covers_arguments_beyond_public_limit(
        self,
    ) -> None:
        common = tuple(f"option-{index}" for index in range(12))
        first = capture_foreground_authority(
            _session(
                program=InteractiveProgram(
                    name="ssh",
                    command="/usr/bin/ssh",
                    argv=(*common, "first-target.example"),
                )
            ),
            target_host="reliquary-catalog-1.chrysalis.internal",
        )
        second = capture_foreground_authority(
            _session(
                program=InteractiveProgram(
                    name="ssh",
                    command="/usr/bin/ssh",
                    argv=(*common, "second-target.example"),
                )
            ),
            target_host="reliquary-catalog-1.chrysalis.internal",
        )

        assert first is not None and second is not None
        self.assertEqual(
            first.public_payload()["interactive_program"]["argv"],
            second.public_payload()["interactive_program"]["argv"],
        )
        self.assertNotEqual(
            first.session_semantics_sha256,
            second.session_semantics_sha256,
        )

    def test_model_projection_redacts_quoted_assignments_and_common_flags(
        self,
    ) -> None:
        cases = (
            (
                "env",
                'env MYSQL_PWD="ultraviolet canary" mysql',
                ("env", "MYSQL_PWD=ultraviolet canary", "mysql"),
                ("ultraviolet", "canary"),
            ),
            (
                "client",
                "client --client-secret client-canary",
                ("client", "--client-secret", "client-canary"),
                ("client-canary",),
            ),
            (
                "curl",
                "curl -u alice:curl-canary https://example.test",
                ("curl", "-u", "alice:curl-canary", "https://example.test"),
                ("curl-canary",),
            ),
            (
                "sshpass",
                "sshpass -p sshpass-canary ssh host.example",
                ("sshpass", "-p", "sshpass-canary", "ssh", "host.example"),
                ("sshpass-canary",),
            ),
        )

        for name, command, argv, secret_parts in cases:
            with self.subTest(name=name):
                authority = capture_foreground_authority(
                    _session(
                        program=InteractiveProgram(
                            name=name,
                            command=command,
                            argv=argv,
                        )
                    ),
                    target_host="reliquary-catalog-1.chrysalis.internal",
                )

                assert authority is not None
                serialized = json.dumps(authority.public_payload())
                for secret_part in secret_parts:
                    self.assertNotIn(secret_part, serialized)

    def test_private_digest_preserves_nonsecret_uri_username(self) -> None:
        def authority_for(username: str, password: str):
            uri = f"postgresql://{username}:{password}@db.example/catalogue"
            return capture_foreground_authority(
                _session(
                    program=InteractiveProgram(
                        name="psql",
                        command=f"psql {uri}",
                        argv=("psql", uri),
                    )
                ),
                target_host="reliquary-catalog-1.chrysalis.internal",
            )

        alice = authority_for("alice", "first-password")
        bob = authority_for("bob", "first-password")
        rotated_password = authority_for("alice", "second-password")

        assert alice is not None and bob is not None and rotated_password is not None
        self.assertNotEqual(
            alice.session_semantics_sha256,
            bob.session_semantics_sha256,
        )
        self.assertEqual(
            alice.session_semantics_sha256,
            rotated_password.session_semantics_sha256,
        )

    def test_empty_stack_uses_synthesized_active_frame_and_depth_one(self) -> None:
        session = _session()
        session.shell_stack.clear()
        session.interactive_program = InteractiveProgram(
            name="zsh",
            command="/usr/bin/zsh",
            argv=("zsh", "-l"),
            prompt_template="% ",
        )
        session.shell_environment = ShellEnvironment(
            variables={"SHELL": "/usr/bin/zsh", "USER": "scott"},
            exported=frozenset({"SHELL", "USER"}),
        )

        authority = capture_foreground_authority(
            session,
            target_host="reliquary-catalog-1.chrysalis.internal",
        )

        assert authority is not None
        self.assertEqual(authority.shell_stack_depth, 1)
        self.assertEqual(authority.program_name, "zsh")
        self.assertEqual(
            authority.public_payload()["shell_environment"],
            {"SHELL": "/usr/bin/zsh", "USER": "scott"},
        )

    def test_invalid_target_is_rejected_without_reading_session(self) -> None:
        session = deepcopy(_session())

        with self.assertRaisesRegex(ValueError, "target_host"):
            capture_foreground_authority(session, target_host="  ")


if __name__ == "__main__":
    unittest.main()
