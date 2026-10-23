import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from dreamland.emulator_context import project_world_state_for_command_emulator
from dreamland.login_banner import MAX_LOGIN_BANNER_BYTES, initial_login_banner
from dreamland.models import (
    CommandHistoryEntry,
    EnvironmentMetadata,
    HostIdentity,
    InteractiveProgram,
    SessionState,
    ShellEnvironment,
    ShellFrame,
)
from dreamland.state_profiles import (
    STATE_PROFILE_METADATA_LEVEL,
    STATE_PROFILE_METADATA_NAME,
    StateProfile,
    StateProfileError,
    StateProfileRuntime,
    StateProfileStore,
    apply_state_profile_to_session,
    create_state_profile,
    session_state_for_level,
    state_profile_from_document,
    state_profile_to_session,
)


class StateProfileTests(unittest.TestCase):
    def test_banner_survives_all_profile_levels_and_session_restore(self):
        session = _session()
        banner = "Debian GNU/Linux 12 — Lunar display console\r\n"
        session.world_state["hosts_by_name"]["display-81"]["login_banner"] = banner
        for level in ("exact", "system", "world"):
            with self.subTest(level=level):
                profile = create_state_profile(session, name="banner", level=level)
                self.assertEqual(profile.to_document()["schema_version"], 2)
                restored = state_profile_to_session(
                    state_profile_from_document(profile.to_document()),
                    current_session_id="restored-banner",
                )
                self.assertEqual(initial_login_banner(restored.world_state), banner)
                self.assertEqual(len(restored.history), 1 if level == "exact" else 0)

    def test_profile_rejects_old_schema_and_missing_initial_banners(self):
        document = create_state_profile(
            _session(), name="banner", level="world"
        ).to_document()
        document["schema_version"] = 1
        with self.assertRaisesRegex(StateProfileError, "schema version"):
            state_profile_from_document(document)
        document["schema_version"] = 2
        del document["state"]["world_state"]["hosts_by_name"]["display-81"][
            "login_banner"
        ]
        with self.assertRaisesRegex(StateProfileError, "login_banner"):
            state_profile_from_document(document)

    def test_invalid_programmatic_profile_cannot_save_or_boot(self):
        state = session_state_for_level(_session(), "world")
        state["world_state"]["hosts_by_name"]["display-81"].pop("login_banner")
        profile = StateProfile("invalid", "world", state, "2026-10-20T00:00:00Z")
        with tempfile.TemporaryDirectory() as directory:
            store = StateProfileStore(directory)
            with self.assertRaisesRegex(StateProfileError, "login_banner"):
                store.save(profile)
            self.assertFalse(store.path_for("invalid").exists())
        with self.assertRaisesRegex(StateProfileError, "login_banner"):
            state_profile_to_session(profile, current_session_id="invalid-boot")

    def test_banners_required_for_distinct_entry_and_initial_active_hosts(self):
        state = session_state_for_level(_session(), "world")
        state["world_state"]["hosts_by_name"]["entry"] = {"hostname": "entry"}
        state["world_state"]["entry_host"] = "entry"
        profile = StateProfile("invalid", "world", state, "2026-10-20T00:00:00Z")
        with self.assertRaisesRegex(StateProfileError, "'entry'.*login_banner"):
            state_profile_to_session(profile, current_session_id="invalid-entry")
        state["world_state"]["hosts_by_name"]["entry"]["login_banner"] = "Entry ready\n"
        self.assertEqual(
            initial_login_banner(
                state_profile_to_session(
                    profile, current_session_id="valid"
                ).world_state
            ),
            "Lunar display console\n",
        )

    def test_actual_initial_shell_host_must_have_banner(self):
        for level in ("system", "exact"):
            with self.subTest(level=level):
                state = session_state_for_level(_session(), level)
                state["host_identity"] = {"hostname": "other"}
                if level == "exact":
                    # Last frame owns the initial shell, ahead of host_identity.
                    state["host_identity"] = {"hostname": "display-81"}
                    state["shell_stack"][-1]["host_identity"] = {"hostname": "other"}
                state["world_state"]["hosts_by_name"]["other"] = {"hostname": "other"}
                profile = StateProfile("invalid", level, state, "2026-10-20T00:00:00Z")
                with self.assertRaisesRegex(StateProfileError, "'other'.*login_banner"):
                    state_profile_to_session(
                        profile, current_session_id="invalid-identity"
                    )
                with self.assertRaisesRegex(StateProfileError, "'other'.*login_banner"):
                    state_profile_from_document(profile.to_document())
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(
                        StateProfileError, "'other'.*login_banner"
                    ):
                        StateProfileStore(directory).save(profile)

    def test_effective_host_supports_registry_key_hostname_and_alias(self):
        for hostname in ("display-81", "display-81.bsides", "display"):
            with self.subTest(hostname=hostname):
                state = session_state_for_level(_session(), "system")
                state["world_state"]["hosts_by_name"]["display-81"].update(
                    {"hostname": "display-81.bsides", "aliases": ["display"]}
                )
                state["host_identity"] = {"hostname": hostname}
                profile = StateProfile(
                    "aliases", "system", state, "2026-10-20T00:00:00Z"
                )
                loaded = state_profile_from_document(profile.to_document())
                with tempfile.TemporaryDirectory() as directory:
                    StateProfileStore(directory).save(loaded)
                restored = state_profile_to_session(loaded, current_session_id="alias")
                self.assertEqual(restored.world_state["active_host"], "display-81")
                self.assertEqual(
                    initial_login_banner(restored.world_state),
                    "Lunar display console\n",
                )

    def test_banner_text_contract_accepts_silence_and_rejects_controls_and_size(self):
        document = create_state_profile(
            _session(), name="banner", level="world"
        ).to_document()
        for banner in ("", "Header\r\n\tReady\n", "a" * MAX_LOGIN_BANNER_BYTES):
            with self.subTest(valid=banner[:30]):
                candidate = deepcopy(document)
                candidate["state"]["world_state"]["hosts_by_name"]["display-81"][
                    "login_banner"
                ] = banner
                loaded = state_profile_from_document(candidate)
                self.assertEqual(
                    initial_login_banner(loaded.state["world_state"]), banner
                )
        for banner in (
            None,
            12,
            "NUL\x00",
            "ESC\x1b[2J",
            "DEL\x7f",
            "C1\x9b",
            "é" * MAX_LOGIN_BANNER_BYTES,
            "\ud800",
        ):
            with self.subTest(invalid=repr(banner)[:30]):
                candidate = deepcopy(document)
                candidate["state"]["world_state"]["hosts_by_name"]["display-81"][
                    "login_banner"
                ] = banner
                with self.assertRaisesRegex(StateProfileError, "login_banner"):
                    state_profile_from_document(candidate)

    def test_discovered_peer_does_not_need_saved_banner(self):
        session = _session()
        session.world_state["hosts_by_name"]["peer"] = {"hostname": "peer"}
        create_state_profile(session, name="world-with-peer", level="world")

    def test_exact_profile_preserves_session_owned_state(self):
        session = _session()

        state = session_state_for_level(session, "exact")

        self.assertEqual(state["username"], "root")
        self.assertEqual(state["login_username"], "alice")
        self.assertEqual(state["host_identity"], {"hostname": "display-81"})
        self.assertEqual(state["cwd"], "/opt/telemetry")
        self.assertIn("shell_stack", state)
        self.assertIn("shell_environment", state)
        self.assertEqual(state["history"][0]["command"], "ls /opt")
        host = state["world_state"]["hosts_by_name"]["display-81"]
        self.assertEqual(
            host["filesystem"]["artifacts_by_path"]["/opt/telemetry/frame.log"]["kind"],
            "log",
        )

    def test_system_profile_preserves_world_and_identity_without_shell_position(self):
        state = session_state_for_level(_session(), "system")

        self.assertEqual(state["username"], "alice")
        self.assertEqual(state["login_username"], "alice")
        self.assertEqual(state["host_identity"], {"hostname": "display-81"})
        self.assertIn("environment_metadata", state)
        self.assertIn("world_state", state)
        self.assertNotIn("cwd", state)
        self.assertNotIn("shell_stack", state)
        self.assertNotIn("history", state)
        self.assertIn(
            "/opt/telemetry/frame.log",
            state["world_state"]["hosts_by_name"]["display-81"]["filesystem"][
                "artifacts_by_path"
            ],
        )

    def test_world_state_preserves_registered_artifacts_and_world_anchors(self):
        state = session_state_for_level(_session(), "world")

        host = state["world_state"]["hosts_by_name"]["display-81"]
        self.assertEqual(host["description"], "Lunar display workstation.")
        self.assertEqual(state["world_state"]["active_host"], "display-81")
        self.assertEqual(
            host["services_by_name"]["redis"]["description"],
            "Local cache service.",
        )
        self.assertIn("system-description", state["world_state"]["world_facts_by_id"])
        self.assertEqual(
            state["world_state"]["generation_policy"]["liberty"],
            "moderate",
        )
        self.assertIn(
            "branch-workstations",
            state["world_state"]["expansion_directives_by_id"],
        )
        self.assertNotIn("artifact-file", state["world_state"]["world_facts_by_id"])
        self.assertIn(
            "/opt/telemetry/frame.log",
            host["filesystem"]["artifacts_by_path"],
        )

    def test_site_geography_survives_save_reload_and_host_projection(self):
        session = _session()
        sites = {
            "network-site-operations": {
                "category": "network",
                "name": "Operations facility",
                "owner": "Regional Systems",
                "role": "operations",
                "location": {
                    "country": "United States",
                    "region": "Pennsylvania",
                    "locality": "Pittsburgh",
                    "timezone": "America/New_York",
                },
            },
            "network-site-support": {
                "category": "network",
                "name": "Support facility",
                "owner": "Regional Systems",
                "role": "support",
                "location": {
                    "country": "United States",
                    "region": "Illinois",
                    "locality": "Chicago",
                    "timezone": "America/Chicago",
                },
            },
        }
        session.world_state["world_facts_by_id"] = {
            **sites,
            "network-regional-wan": {
                "category": "network",
                "site_ids": list(sites),
            },
        }
        hosts = session.world_state["hosts_by_name"]
        hosts["display-81"]["site_id"] = "network-site-operations"
        hosts["support-01"] = {
            "hostname": "support-01",
            "site_id": "network-site-support",
            "filesystem": {"artifacts_by_path": {}},
        }
        profile = create_state_profile(session, name="geography", level="world")
        with tempfile.TemporaryDirectory() as directory:
            store = StateProfileStore(directory)
            store.save(profile)
            restored = state_profile_to_session(
                store.load("geography"),
                current_session_id="restored-geography",
                fallback_username="alice",
                authenticated_username="alice",
            )

        for active in hosts:
            with self.subTest(active=active):
                restored.world_state["active_host"] = active
                projected = project_world_state_for_command_emulator(
                    restored.world_state
                )
                facts = projected["world_facts_by_id"]
                self.assertEqual(facts["network-regional-wan"]["site_ids"], list(sites))
                for host_ref, host in hosts.items():
                    card = projected["hosts_by_name"][host_ref]
                    self.assertEqual(card["site_id"], host["site_id"])
                    self.assertEqual(facts[card["site_id"]], sites[host["site_id"]])
                    if host_ref != active:
                        self.assertNotIn("filesystem", card)

    def test_state_profile_to_session_normalizes_host_command_history_envelopes(self):
        document = {
            "kind": "dreamland.state_profile",
            "schema_version": 2,
            "name": "polluted",
            "level": "world",
            "created_at": "2026-07-13T00:00:00+00:00",
            "state": {
                "world_state": {
                    "entry_host": "display-81",
                    "active_host": "display-81",
                    "hosts_by_name": {
                        "display-81": {
                            "hostname": "display-81",
                            "login_banner": "Lunar display console\n",
                            "command_history": [
                                {
                                    "cwd": "/opt",
                                    "command": "status",
                                    "stdout": json.dumps(
                                        {
                                            "stdout": "real status\n",
                                            "stderr": "",
                                            "exit_status": 0,
                                            "state_update": {},
                                        }
                                    ),
                                    "stderr": "",
                                    "exit_status": 0,
                                }
                            ],
                        }
                    },
                }
            },
        }

        session = state_profile_to_session(
            state_profile_from_document(document),
            current_session_id="session-a",
        )

        history = session.world_state["hosts_by_name"]["display-81"]["command_history"]
        self.assertEqual(history[0]["stdout"], "real status\n")

    def test_store_round_trips_profile_documents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            profile = create_state_profile(
                _session(),
                name="apollo-test",
                level="exact",
                comment="manual test point",
                created_at="2026-06-15T12:00:00+00:00",
            )

            path = store.save(profile)
            loaded = store.load("apollo-test")
            summaries = store.list()

        self.assertEqual(path.name, "apollo-test.json")
        self.assertEqual(loaded.name, "apollo-test")
        self.assertEqual(loaded.level, "exact")
        self.assertEqual(loaded.comment, "manual test point")
        self.assertEqual(loaded.state["history"][0]["command"], "ls /opt")
        self.assertEqual([summary.name for summary in summaries], ["apollo-test"])

    def test_store_deletes_profile_documents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            store.save(
                create_state_profile(
                    _session(),
                    name="apollo-test",
                    level="exact",
                    created_at="2026-06-15T12:00:00+00:00",
                )
            )

            deleted = store.delete("apollo-test")

            self.assertEqual(deleted.name, "apollo-test.json")
            self.assertEqual(store.list(), [])
            with self.assertRaises(StateProfileError):
                store.load("apollo-test")

    def test_runtime_default_can_be_changed_and_cleared(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            store.save(
                create_state_profile(
                    _session(),
                    name="apollo-system",
                    level="system",
                    created_at="2026-06-15T12:00:00+00:00",
                )
            )
            runtime = StateProfileRuntime(store=store)

            profile = runtime.set_default("apollo-system")
            previous = runtime.clear_default()

        self.assertEqual(profile.name, "apollo-system")
        self.assertEqual(previous.name, "apollo-system")
        self.assertIsNone(runtime.current_profile())

    def test_runtime_delete_clears_matching_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            store.save(
                create_state_profile(
                    _session(),
                    name="apollo-system",
                    level="system",
                    created_at="2026-06-15T12:00:00+00:00",
                )
            )
            runtime = StateProfileRuntime(store=store)
            runtime.set_default("apollo-system")

            path, cleared = runtime.delete_profile("apollo-system")

        self.assertEqual(path.name, "apollo-system.json")
        self.assertTrue(cleared)
        self.assertIsNone(runtime.current_profile())

    def test_store_records_session_profile_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            profile = create_state_profile(
                _session(),
                name="apollo-system",
                level="system",
                created_at="2026-06-15T12:00:00+00:00",
            )

            record = store.record_session_profile(
                session_id="session-a",
                profile=profile,
                source="apply",
                updated_at="2026-06-16T06:20:00+00:00",
            )
            reloaded = StateProfileStore(Path(tmpdir)).session_profile("session-a")

        self.assertEqual(record.label, "apollo-system:system")
        self.assertEqual(reloaded.label, "apollo-system:system")
        self.assertEqual(reloaded.source, "apply")
        self.assertEqual(reloaded.updated_at, "2026-06-16T06:20:00+00:00")

    def test_profile_name_rejects_path_traversal(self):
        with self.assertRaises(StateProfileError):
            create_state_profile(_session(), name="../bad", level="exact")

    def test_document_loader_enforces_minimum_schema(self):
        with self.assertRaises(StateProfileError):
            state_profile_from_document({"kind": "other"})

    def test_profile_can_rebuild_session_for_later_apply(self):
        profile = create_state_profile(
            _session(),
            name="apollo-test",
            level="exact",
            created_at="2026-06-15T12:00:00+00:00",
        )

        session = state_profile_to_session(
            profile,
            current_session_id="new-session",
        )

        self.assertEqual(session.langfuse_trace_id, "new-session")
        self.assertEqual(session.username, "root")
        self.assertEqual(session.login_username, "alice")
        self.assertEqual(session.host_identity.hostname, "display-81")
        self.assertEqual(session.cwd, "/opt/telemetry")
        self.assertEqual(session.history[0].command, "ls /opt")
        self.assertTrue(session.force_session_state_next_command)
        self.assertEqual(
            session.environment_metadata.metadata[STATE_PROFILE_METADATA_NAME],
            "apollo-test",
        )
        self.assertEqual(
            session.environment_metadata.metadata[STATE_PROFILE_METADATA_LEVEL],
            "exact",
        )

    def test_system_profile_rebuilds_fresh_login_shell_for_later_apply(self):
        profile = create_state_profile(
            _session(),
            name="apollo-system",
            level="system",
            created_at="2026-06-15T12:00:00+00:00",
        )

        session = state_profile_to_session(
            profile,
            current_session_id="new-session",
        )

        self.assertEqual(session.username, "alice")
        self.assertEqual(session.login_username, "alice")
        self.assertEqual(session.host_identity.hostname, "display-81")
        self.assertEqual(session.cwd, "/home/alice")
        self.assertEqual(session.shell_stack[-1].username, "alice")
        self.assertEqual(
            session.shell_environment.variables["HOSTNAME"],
            "display-81",
        )
        self.assertEqual(session.history, [])

    def test_system_profile_can_rebuild_fresh_login_shell_for_authenticated_user(self):
        profile = create_state_profile(
            _session(),
            name="apollo-system",
            level="system",
            created_at="2026-06-15T12:00:00+00:00",
        )

        session = state_profile_to_session(
            profile,
            current_session_id="new-session",
            authenticated_username="ssh-user",
        )

        self.assertEqual(session.username, "ssh-user")
        self.assertEqual(session.login_username, "ssh-user")
        self.assertEqual(session.host_identity.hostname, "display-81")
        self.assertEqual(session.cwd, "/home/ssh-user")
        self.assertEqual(session.shell_stack[-1].username, "ssh-user")
        self.assertEqual(session.history, [])

    def test_apply_profile_replaces_session_but_preserves_trace_id(self):
        profile = create_state_profile(
            _session(),
            name="apollo-test",
            level="exact",
            created_at="2026-06-15T12:00:00+00:00",
        )
        target = SessionState(
            username="bob",
            login_username="bob",
            langfuse_trace_id="target-session",
            world_state={
                "active_host": "display-81",
                "hosts_by_name": {
                    "display-81": {"hostname": "display-81", "services_by_name": {}}
                },
            },
        )

        apply_state_profile_to_session(target, profile)

        self.assertEqual(target.langfuse_trace_id, "target-session")
        self.assertEqual(target.username, "root")
        self.assertEqual(target.login_username, "bob")
        self.assertEqual(target.host_identity.hostname, "display-81")
        self.assertEqual(target.cwd, "/opt/telemetry")
        self.assertEqual(target.history[0].command, "ls /opt")
        self.assertIn(
            "redis",
            target.world_state["hosts_by_name"]["display-81"]["services_by_name"],
        )
        self.assertTrue(target.force_session_state_next_command)


def _session() -> SessionState:
    environment = ShellEnvironment(
        variables={
            "HOME": "/root",
            "HOSTNAME": "display-81",
            "PATH": "/usr/bin:/bin",
            "PWD": "/opt/telemetry",
            "SHELL": "/bin/bash",
            "USER": "root",
        },
        exported=frozenset({"HOME", "HOSTNAME", "PATH", "PWD", "SHELL", "USER"}),
    )
    program = InteractiveProgram(name="bash", command="/bin/bash", argv=("bash",))
    return SessionState(
        username="root",
        login_username="alice",
        host_identity=HostIdentity(hostname="display-81"),
        cwd="/opt/telemetry",
        interactive_program=program,
        shell_environment=environment,
        shell_stack=[
            ShellFrame(
                username="root",
                cwd="/opt/telemetry",
                interactive_program=program,
                shell_environment=environment,
                metadata={"source": "manual-test"},
            )
        ],
        langfuse_trace_id="session-a",
        history=[
            CommandHistoryEntry(
                cwd="/opt/telemetry",
                command="ls /opt",
                stdout="telemetry\n",
            )
        ],
        environment_metadata=EnvironmentMetadata(metadata={"scenario": "apollo"}),
        world_state={
            "name": "apollo-world",
            "entry_host": "display-81",
            "active_host": "display-81",
            "purpose": {"description": "Moon mission lab network."},
            "simulation": {"permissiveness": "moderate"},
            "generation_policy": {
                "materialization": "just_in_time",
                "liberty": "moderate",
            },
            "expansion_directives_by_id": {
                "branch-workstations": {
                    "domain": "branch workstations",
                    "scale": "about twelve small branch office devices",
                    "materialization_policy": "generate hosts when discovered",
                }
            },
            "world_facts_by_id": {
                "system-description": {
                    "category": "system",
                    "description": "Display control workstation.",
                }
            },
            "hosts_by_name": {
                "display-81": {
                    "description": "Lunar display workstation.",
                    "hostname": "display-81",
                    "login_banner": "Lunar display console\n",
                    "filesystem": {
                        "artifacts_by_path": {
                            "/opt/telemetry/frame.log": {"kind": "log"}
                        }
                    },
                    "services_by_name": {
                        "redis": {"description": "Local cache service."}
                    },
                    "facts_by_id": {
                        "artifact-file": {
                            "category": "artifact",
                            "path": "/opt/telemetry/frame.log",
                        }
                    },
                    "command_history": [],
                }
            },
        },
    )


if __name__ == "__main__":
    unittest.main()
