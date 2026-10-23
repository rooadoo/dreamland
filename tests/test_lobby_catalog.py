import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from dreamland.lobby_catalog import (
    LobbyCatalog,
    LobbyCatalogError,
    LobbySelectionChanged,
)
from dreamland.lobby_content import read_bounded_file
from dreamland.state_profiles import (
    StateProfile,
    StateProfileStore,
    state_profile_to_session,
)


class LobbyCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = StateProfileStore(self.root / "profiles")
        self.path = self.root / "lobby" / "catalog.json"
        self.save("alpha")
        self.save("beta")
        self.catalog = LobbyCatalog(self.store, self.path)

    def save(self, name, *, level="world", state=None):
        state = deepcopy(state) if state is not None else {}
        world = state.setdefault("world_state", {})
        entry = world.setdefault("entry_host", "ubuntu")
        active = world.setdefault("active_host", entry)
        hosts = world.setdefault("hosts_by_name", {})
        for reference in (entry, active):
            host = hosts.setdefault(reference, {})
            host.setdefault("hostname", reference)
            host.setdefault("login_banner", "Catalog fixture host.\n")
        profile = StateProfile(
            name,
            level,
            state,
            "2026-09-11T00:00:00Z",
        )
        self.store.save(profile)
        return profile

    def add(self, name="alpha", **kwargs):
        return self.catalog.add(
            name,
            title=kwargs.get("title", name.title()),
            description=kwargs.get("description", "Explore an authored world."),
            entry_username=kwargs.get("entry_username", "operator"),
        )

    def test_all_curation_operations_persist_and_restart(self):
        self.assertEqual(self.catalog.snapshot().entries, ())
        self.add()
        self.add("beta")
        self.catalog.update(
            "alpha",
            title="After Hours",
            description="Explore after closing.",
            entry_username="reader",
        )
        snapshot = self.catalog.order(["beta", "alpha"])
        self.assertEqual(
            [entry.profile for entry in snapshot.entries], ["beta", "alpha"]
        )
        restored = LobbyCatalog(self.store, self.path)
        self.assertEqual(restored.snapshot().entries, snapshot.entries)
        self.assertEqual(restored.snapshot().entries[1].title, "After Hours")
        self.assertEqual(restored.snapshot().entries[1].entry_username, "reader")
        self.assertEqual(restored.snapshot().entries[0].entry_username, "operator")
        self.assertEqual(json.loads(self.path.read_text())["schema_version"], 2)
        self.catalog.remove("alpha")
        self.assertTrue(self.store.path_for("alpha").exists())
        self.assertEqual(
            LobbyCatalog(self.store, self.path).snapshot().entries[0].order, 1
        )
        self.catalog.remove("beta")
        self.assertEqual(LobbyCatalog(self.store, self.path).snapshot().entries, ())

    def test_reload_publishes_operator_edits(self):
        self.add()
        document = json.loads(self.path.read_text())
        document["worlds"][0]["title"] = "Changed by operator"
        self.path.write_text(json.dumps(document))
        self.assertEqual(self.catalog.snapshot().entries[0].title, "Alpha")
        self.assertEqual(self.catalog.reload().entries[0].title, "Changed by operator")

    def test_omitted_entry_username_resolves_saved_identity_and_entry_host(self):
        states = (
            {"login_username": "reader", "username": "effective"},
            {"username": "reader"},
            {
                "world_state": {
                    "entry_host": "terminal",
                    "active_host": "other",
                    "hosts_by_name": {
                        "terminal": {
                            "default_user": "reader",
                            "facts_by_id": {"maintenance": {"username": "admin"}},
                        },
                        "other": {"default_user": "other_user"},
                    },
                }
            },
            {
                "world_state": {
                    "entry_host": "terminal",
                    "hosts_by_name": {
                        "terminal": {
                            "facts_by_id": {
                                "session": {"username": "reader"},
                                "owner": {"local_username": "reader"},
                            }
                        },
                        "other": {"facts_by_id": {"session": {"username": "other"}}},
                    },
                }
            },
        )
        for level in ("world", "system"):
            for state in states:
                with self.subTest(level=level, state=state):
                    self.save("alpha", level=level, state=state)
                    snapshot = self.catalog.add(
                        "alpha", title="Alpha", description="Explore an authored world."
                    )
                    self.assertEqual(snapshot.entries[0].entry_username, "reader")
                    self.catalog.remove("alpha")

    def test_omitted_entry_username_supports_bundled_account_facts_and_pins_account(
        self,
    ):
        for name, username in (
            ("lobby-supermarket", "operator"),
            ("lobby-colonyship", "reader"),
            ("lobby-offshore", "leah"),
        ):
            with self.subTest(name=name):
                self.store.save(self.store.load(name))
                before = self.store.path_for(name).read_bytes()
                snapshot = self.catalog.add(
                    name, title="Example", description="Explore an authored world."
                )
                self.assertEqual(snapshot.entries[-1].entry_username, username)
                self.assertEqual(self.store.path_for(name).read_bytes(), before)
                world, profile = self.catalog.resolve_selection(
                    snapshot, len(snapshot.entries)
                )
                session = state_profile_to_session(
                    profile,
                    current_session_id="inferred-account",
                    authenticated_username=world.entry_username,
                )
                self.assertEqual(session.username, username)
                self.assertEqual(session.login_username, username)
                self.assertEqual(session.shell_stack[0].username, username)
                self.assertEqual(session.shell_environment.variables["USER"], username)
                self.assertEqual(
                    session.shell_environment.variables["LOGNAME"], username
                )
                # Changing descriptive source facts does not silently change the
                # account already published in the version-2 catalog.
                self.save(name, state={"username": "replacement", "world_state": {}})
                self.assertEqual(
                    self.catalog.reload().entries[-1].entry_username, username
                )
                self.catalog.update(name, title="Updated")
                self.assertEqual(
                    self.catalog.snapshot().entries[-1].entry_username, username
                )
        self.assertEqual(
            LobbyCatalog(self.store, self.path).snapshot().entries,
            self.catalog.snapshot().entries,
        )

    def test_missing_ambiguous_or_invalid_inferred_accounts_preserve_catalog(self):
        snapshot = self.add("beta")
        before = self.path.read_bytes()
        for state in (
            {"world_state": {}},
            {
                "world_state": {
                    "entry_host": "terminal",
                    "hosts_by_name": {
                        "terminal": {
                            "users_by_username": {"root": {"username": "root"}},
                            "facts_by_id": {
                                "session": {
                                    "description": "Start as reader",
                                    "privileged_username": "root",
                                }
                            },
                        },
                        "other": {"default_user": "reader"},
                    },
                }
            },
            {"username": "{username}"},
            {"login_username": 7, "username": "reader"},
            {
                "world_state": {
                    "entry_host": "terminal",
                    "hosts_by_name": {
                        "terminal": {
                            "facts_by_id": {
                                "public-session": {"username": "reader"},
                                "maintenance": {"maintenance_username": "operator"},
                            }
                        }
                    },
                }
            },
            {
                "world_state": {
                    "entry_host": "terminal",
                    "hosts_by_name": {"terminal": {"default_user": "reader\n"}},
                }
            },
        ):
            with self.subTest(state=state):
                self.save("alpha", state=state)
                with self.assertRaisesRegex(LobbyCatalogError, "--entry-username"):
                    self.catalog.add(
                        "alpha", title="Alpha", description="Explore an authored world."
                    )
                self.assertIs(self.catalog.snapshot(), snapshot)
                self.assertEqual(self.path.read_bytes(), before)
                self.add("alpha", entry_username="override")
                snapshot = self.catalog.remove("alpha")
                before = self.path.read_bytes()

    def test_snapshot_is_immutable(self):
        snapshot = self.add()
        with self.assertRaises(FrozenInstanceError):
            snapshot.entries[0].title = "Changed"

    def test_inferred_account_retains_fresh_profile_and_environment_validation(self):
        before = self.path.read_bytes()
        for level, state in (
            ("exact", {"username": "reader"}),
            ("world", {"username": "reader", "history": [{"command": "id"}]}),
            (
                "system",
                {
                    "username": "reader",
                    "shell_environment": {
                        "USER": "other",
                        "LOGNAME": "reader",
                        "HOME": "/home/reader",
                    },
                },
            ),
        ):
            with self.subTest(level=level, state=state):
                self.save("alpha", level=level, state=state)
                with self.assertRaises(LobbyCatalogError):
                    self.catalog.add(
                        "alpha", title="Alpha", description="Explore an authored world."
                    )
                self.assertEqual(self.catalog.snapshot().entries, ())
                self.assertEqual(self.path.read_bytes(), before)

    def test_reordering_never_reinterprets_old_number(self):
        self.add()
        snapshot = self.add("beta")
        self.catalog.order(["beta", "alpha"])
        with self.assertRaises(LobbySelectionChanged):
            self.catalog.resolve_selection(snapshot, 1)
        entry, profile = self.catalog.resolve_selection(self.catalog.snapshot(), 1)
        self.assertEqual(entry.profile, "beta")
        self.assertEqual(profile.name, "beta")

    def test_removal_and_metadata_edit_invalidate_snapshot(self):
        for mutate in (
            lambda: self.catalog.update("alpha", title="Updated"),
            lambda: self.catalog.update("alpha", entry_username="reader"),
            lambda: self.catalog.remove("alpha"),
        ):
            snapshot = self.catalog.snapshot()
            if not snapshot.entries:
                snapshot = self.add()
            mutate()
            with self.assertRaises(LobbySelectionChanged):
                self.catalog.resolve_selection(snapshot, 1)

    def test_deleted_profile_cannot_fall_back_and_recovers_after_repair(self):
        snapshot = self.add()
        self.store.delete("alpha")
        with self.assertRaisesRegex(LobbySelectionChanged, "unavailable") as raised:
            self.catalog.resolve_selection(snapshot, 1)
        self.assertNotIn(str(self.root), str(raised.exception))
        self.assertNotIn("alpha", str(raised.exception))
        self.save("alpha")
        self.assertEqual(self.catalog.resolve_selection(snapshot, 1)[1].name, "alpha")

    def test_selection_loads_one_validated_detached_profile(self):
        snapshot = self.add()
        with patch(
            "dreamland.lobby_catalog.read_bounded_file", wraps=read_bounded_file
        ) as load:
            _, selected = self.catalog.resolve_selection(snapshot, 1)
        load.assert_called_once()
        self.assertEqual(load.call_args.args[0], self.store.path_for("alpha"))
        selected.state["changed"] = True
        self.assertNotIn("changed", self.store.load("alpha").state)
        self.save("alpha", state={"world_state": {"purpose": "later"}})
        self.assertNotIn("purpose", selected.state["world_state"])

    def test_bad_numbers_do_not_load_any_profile(self):
        snapshot = self.add()
        with patch.object(self.store, "load") as load:
            for number in (0, -1, 2, "alpha", "../alpha", True, 1.0):
                with self.subTest(number=number), self.assertRaises(LobbyCatalogError):
                    self.catalog.resolve_selection(snapshot, number)
        load.assert_not_called()

    def test_failed_write_preserves_disk_and_published_snapshot(self):
        snapshot = self.add()
        before = self.path.read_bytes()
        with patch(
            "dreamland.lobby_catalog.os.replace",
            side_effect=OSError("private /secret/path"),
        ):
            with self.assertRaisesRegex(LobbyCatalogError, "persist") as raised:
                self.catalog.update("alpha", entry_username="reader")
        self.assertNotIn("/secret", str(raised.exception))
        self.assertIs(self.catalog.snapshot(), snapshot)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".catalog.json.*")), [])

    def test_failed_reload_preserves_published_snapshot_and_startup_fails(self):
        snapshot = self.add()
        for value in (
            "{",
            "[]",
            '{"kind":"wrong"}',
            '{"kind":"dreamland.ssh_lobby_catalog","schema_version":2,"worlds":{}}',
        ):
            self.path.write_text(value)
            with self.assertRaises(LobbyCatalogError):
                self.catalog.reload()
            self.assertIs(self.catalog.snapshot(), snapshot)
            with self.assertRaises(LobbyCatalogError):
                LobbyCatalog(self.store, self.path)

    def test_invalid_copy_and_profile_names_never_publish(self):
        before = self.path.read_bytes()
        attacks = (
            "\x1b[2J",
            "name\x00",
            "name\r",
            "name\n",
            "name\x7f",
            "name\u202e",
            "x" * 81,
        )
        for attack in attacks:
            with self.subTest(title=repr(attack)), self.assertRaises(LobbyCatalogError):
                self.add(title=attack)
        for name in ("../alpha", "/tmp/alpha", "alpha\n", "{username}", "alpha;id"):
            with self.subTest(profile=name), self.assertRaises(LobbyCatalogError):
                self.add(name)
        self.assertEqual(self.catalog.snapshot().entries, ())
        self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_catalog_requires_explicit_identity_migration(self):
        snapshot = self.add()
        document = json.loads(self.path.read_text())
        document["schema_version"] = 1
        del document["worlds"][0]["entry_username"]
        self.path.write_text(json.dumps(document))
        for action in (
            self.catalog.reload,
            lambda: LobbyCatalog(self.store, self.path),
        ):
            with self.assertRaisesRegex(
                LobbyCatalogError, "migration: add an explicit entry_username"
            ):
                action()
        self.assertIs(self.catalog.snapshot(), snapshot)
        document["schema_version"] = 2
        document["worlds"][0]["entry_username"] = "reader"
        self.path.write_text(json.dumps(document))
        self.assertEqual(self.catalog.reload().entries[0].entry_username, "reader")

    def test_missing_or_nontext_identity_rejects_reload_and_preserves_snapshot(self):
        snapshot = self.add()
        document = json.loads(self.path.read_text())
        for value in (None, 7, True, [], {}):
            with self.subTest(value=value):
                document["worlds"][0]["entry_username"] = value
                self.path.write_text(json.dumps(document))
                with self.assertRaisesRegex(LobbyCatalogError, "entry_username"):
                    self.catalog.reload()
                self.assertIs(self.catalog.snapshot(), snapshot)
        del document["worlds"][0]["entry_username"]
        self.path.write_text(json.dumps(document))
        with self.assertRaisesRegex(LobbyCatalogError, "entry_username"):
            self.catalog.reload()
        self.assertIs(self.catalog.snapshot(), snapshot)

    def test_invalid_identity_never_changes_disk_or_snapshot(self):
        before = self.path.read_bytes()
        for value in (
            "",
            " reader",
            "reader ",
            "reader\n",
            "reader\x00",
            "reader\x1b[2J",
            "../reader",
            "/reader",
            "{username}",
            "reader;id",
            "$(id)",
            "réader",
            "reader\u202e",
            "-reader",
            "x" * 65,
            None,
            7,
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(LobbyCatalogError, "entry_username"),
            ):
                self.add(entry_username=value)
        self.assertEqual(self.catalog.snapshot().entries, ())
        self.assertEqual(self.path.read_bytes(), before)
        snapshot = self.add()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(LobbyCatalogError, "entry_username"):
            self.catalog.update("alpha", entry_username="invalid name")
        self.assertIs(self.catalog.snapshot(), snapshot)
        self.assertEqual(self.path.read_bytes(), before)

    def test_profile_templates_use_entry_identity_without_mutating_saved_profile(self):
        self.save(
            "alpha",
            state={
                "cwd": "/srv/{username}/work",
                "shell_environment": {
                    "USER": "{username}",
                    "LOGNAME": "{username}",
                    "HOME": "/srv/{username}",
                },
                "world_state": {"purpose": "Welcome {username}"},
            },
        )
        before = self.store.path_for("alpha").read_bytes()
        snapshot = self.add(entry_username="reader")
        world, profile = self.catalog.resolve_selection(snapshot, 1)
        session = state_profile_to_session(
            profile,
            current_session_id="selected-profile",
            authenticated_username=world.entry_username,
        )
        self.assertEqual(session.username, "reader")
        self.assertEqual(session.login_username, "reader")
        self.assertEqual(session.shell_stack[0].username, "reader")
        self.assertEqual(session.shell_environment.variables["USER"], "reader")
        self.assertEqual(session.shell_environment.variables["LOGNAME"], "reader")
        self.assertEqual(session.shell_environment.variables["HOME"], "/srv/reader")
        self.assertEqual(session.cwd, "/srv/reader/work")
        self.assertEqual(session.world_state["purpose"], "Welcome reader")
        self.assertEqual(self.store.path_for("alpha").read_bytes(), before)
        self.assertEqual(profile.state["shell_environment"]["USER"], "{username}")

    def test_entry_identity_rejects_conflicting_authored_environment(self):
        snapshot = self.add()
        before = self.path.read_bytes()
        for key in ("USER", "LOGNAME"):
            for source in ("shell_environment", "host_environment"):
                with self.subTest(key=key, source=source):
                    environment = {
                        "USER": "operator",
                        "LOGNAME": "operator",
                        "HOME": "/home/operator",
                    }
                    environment[key] = "unexpected"
                    state = (
                        {"shell_environment": environment}
                        if source == "shell_environment"
                        else {
                            "world_state": {
                                "entry_host": "terminal",
                                "active_host": "terminal",
                                "hosts_by_name": {
                                    "terminal": {"environment": environment}
                                },
                            }
                        }
                    )
                    self.save("alpha", state=state)
                    with self.assertRaisesRegex(LobbyCatalogError, "USER and LOGNAME"):
                        self.catalog.update("alpha", title="Lost update")
                    with self.assertRaisesRegex(LobbySelectionChanged, "unavailable"):
                        self.catalog.resolve_selection(snapshot, 1)
                    self.assertIs(self.catalog.snapshot(), snapshot)
                    self.assertEqual(self.path.read_bytes(), before)

    def test_incomplete_environment_or_relative_home_cannot_seed_lobby(self):
        for environment in (
            {"HOME": "/home/operator"},
            {"USER": "operator", "LOGNAME": "operator"},
            {"USER": "operator", "LOGNAME": "operator", "HOME": "relative"},
            {"USER": "operator", "LOGNAME": "operator", "HOME": "/home/\noperator"},
        ):
            with self.subTest(environment=environment):
                self.save("alpha", state={"shell_environment": environment})
                with self.assertRaises(LobbyCatalogError):
                    self.add()

    def test_literal_account_names_and_root_home_are_supported(self):
        for username in ("reader", "root", "_service", "ops.reader-1", "x" * 64):
            with self.subTest(username=username):
                snapshot = self.add(entry_username=username)
                self.assertEqual(snapshot.entries[0].entry_username, username)
                self.catalog.resolve_selection(snapshot, 1)
                self.catalog.remove("alpha")

    def test_bundled_catalog_starts_authored_worlds_with_expected_accounts(self):
        repository = Path(__file__).resolve().parents[1]
        provenance = json.loads((repository / "lobby" / "provenance.json").read_text())
        # A fresh install gets these bytes from the package, without research
        # archives or an existing operator profile directory.
        for world in provenance["worlds"]:
            resource = files("dreamland.default_state_profiles").joinpath(
                f"{world['profile']}.json"
            )
            data = resource.read_bytes()
            self.assertEqual(
                hashlib.sha256(data).hexdigest(), world["packaged_profile_sha256"]
            )
            self.assertEqual(
                world["packaged_profile_sha256"], world["local_profile_sha256"]
            )
            document = json.loads(data)
            self.assertEqual(document["schema_version"], 2)
            canonical_world = json.dumps(
                document["state"]["world_state"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.assertEqual(
                hashlib.sha256(canonical_world).hexdigest(),
                world["packaged_world_state_sha256"],
            )
            self.assertEqual(
                world["packaged_world_state_sha256"],
                world["local_world_state_sha256"],
            )
            self.assertEqual(self.store.load(world["profile"]).level, "world")
            # Lobby publication still requires a deliberate regular-file copy.
            self.store.path_for(world["profile"]).write_bytes(data)
        catalog = LobbyCatalog(self.store, repository / "lobby" / "catalog.json")
        snapshot = catalog.snapshot()
        expected = {
            "lobby-supermarket": ("operator", "pt-por-042-pos03"),
            "lobby-colonyship": ("reader", "hearth-c17"),
            "lobby-offshore": ("leah", "leah-t14"),
        }
        self.assertEqual({world.profile for world in snapshot.entries}, set(expected))
        for number in range(1, len(snapshot.entries) + 1):
            world, profile = catalog.resolve_selection(snapshot, number)
            with self.subTest(profile=world.profile):
                username, hostname = expected[world.profile]
                self.assertEqual(world.entry_username, username)
                session = state_profile_to_session(
                    profile,
                    current_session_id="bundled-entry-smoke",
                    authenticated_username=world.entry_username,
                )
                self.assertEqual(session.username, username)
                self.assertEqual(session.login_username, username)
                self.assertEqual(session.shell_stack[0].username, username)
                self.assertEqual(session.shell_environment.variables["USER"], username)
                self.assertEqual(
                    session.shell_environment.variables["LOGNAME"], username
                )
                self.assertEqual(
                    session.shell_environment.variables["HOME"], f"/home/{username}"
                )
                self.assertEqual(session.cwd, f"/home/{username}")
                self.assertEqual(session.world_state["entry_host"], hostname)
                self.assertEqual(session.host_identity.hostname, hostname)

    def test_exact_and_resume_shaped_profiles_are_rejected(self):
        self.save("exact", level="exact")
        with self.assertRaisesRegex(LobbyCatalogError, "world/system"):
            self.add("exact")
        for key in ("shell_stack", "history", "interactive_program"):
            self.save(
                "resume",
                level="system",
                state={"world_state": {}, key: [{"secret": "old-visit"}]},
            )
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(LobbyCatalogError, "history or shell position"),
            ):
                self.add("resume")
        self.save("system", level="system")
        self.add("system")

    def test_packaged_fallback_and_symlink_do_not_publish(self):
        with self.assertRaisesRegex(LobbyCatalogError, "save the profile"):
            self.add("default")
        self.store.path_for("linked").symlink_to(self.store.path_for("alpha"))
        with self.assertRaisesRegex(LobbyCatalogError, "save the profile"):
            self.add("linked")

    def test_duplicate_and_incomplete_order_leave_catalog_unchanged(self):
        self.add()
        snapshot = self.add("beta")
        for action in (
            lambda: self.add(),
            lambda: self.catalog.order(["alpha"]),
            lambda: self.catalog.order(["alpha", "alpha"]),
            lambda: self.catalog.order(["alpha", "unknown"]),
        ):
            with self.assertRaises(LobbyCatalogError):
                action()
            self.assertIs(self.catalog.snapshot(), snapshot)

    def test_concurrent_adds_serialize_without_lost_updates(self):
        for number in range(10):
            self.save(f"world-{number}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.add, [f"world-{number}" for number in range(10)]))
        snapshot = self.catalog.snapshot()
        self.assertEqual(len(snapshot.entries), 10)
        self.assertEqual(
            [entry.order for entry in snapshot.entries], list(range(1, 11))
        )
        self.assertEqual(
            LobbyCatalog(self.store, self.path).snapshot().entries, snapshot.entries
        )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_fifo_catalog_does_not_block_initialization(self):
        path = self.root / "fifo.json"
        os.mkfifo(path)
        with self.assertRaises(LobbyCatalogError):
            LobbyCatalog(self.store, path)

    def test_deep_json_profile_fails_without_losing_catalog(self):
        snapshot = self.add()
        self.store.path_for("alpha").write_text("[" * 1500 + "]" * 1500)
        with self.assertRaises(LobbySelectionChanged):
            self.catalog.resolve_selection(snapshot, 1)
        self.assertIs(self.catalog.snapshot(), snapshot)

    def test_encoded_catalog_limit_preserves_previous_file_and_snapshot(self):
        snapshot = self.add()
        before = self.path.read_bytes()
        with patch("dreamland.lobby_catalog.MAX_CATALOG_BYTES", len(before)):
            with self.assertRaisesRegex(LobbyCatalogError, "file size limit"):
                self.catalog.update("alpha", description="\U0001f680" * 320)
        self.assertIs(self.catalog.snapshot(), snapshot)
        self.assertEqual(self.path.read_bytes(), before)
