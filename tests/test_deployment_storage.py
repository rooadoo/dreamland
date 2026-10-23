import fcntl
import hashlib
import json
import os
import stat
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from dreamland.deployment import storage
from dreamland.deployment.storage import (
    StorageError,
    initialize,
    read_configuration,
    read_managed,
    read_profile,
    read_secret,
    validate_secret,
    write_secret,
)


class DeploymentStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "deployment"
        self.document = {"kind": "test-deployment", "ssh_enabled": True}
        self.runtime_ini = "[server]\nentry_mode = lobby\n"

    def initialize(self):
        return initialize(self.root, self.document, self.runtime_ini)

    def test_fresh_layout_preserves_exact_packaged_profiles_and_provenance(self):
        report = self.initialize()
        self.assertTrue(report["initialized"])
        self.assertEqual(len(report["created"]), 7)
        self.assertEqual(report["preserved"], [])
        self.assertEqual(report["absent"], [])
        self.assertEqual(
            json.loads((self.root / "deployment.json").read_text()), self.document
        )
        self.assertEqual((self.root / "dreamland.ini").read_text(), self.runtime_ini)
        self.assertEqual(list((self.root / "secrets").iterdir()), [])

        repository = Path(__file__).resolve().parents[1]
        for name in ("catalog.json", "provenance.json"):
            self.assertEqual(
                (self.root / "lobby" / name).read_bytes(),
                (repository / "lobby" / name).read_bytes(),
            )
        provenance = json.loads((self.root / "lobby/provenance.json").read_bytes())
        for world in provenance["worlds"]:
            materialized = (
                self.root / "state_profiles" / f"{world['profile']}.json"
            ).read_bytes()
            self.assertEqual(
                hashlib.sha256(materialized).hexdigest(),
                world["packaged_profile_sha256"],
            )
            self.assertEqual(
                materialized,
                files("dreamland.default_state_profiles")
                .joinpath(f"{world['profile']}.json")
                .read_bytes(),
            )
        for path in [self.root, *self.root.rglob("*")]:
            self.assertEqual(
                stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600
            )

    def test_completed_setup_preserves_edits_and_deliberate_removals(self):
        self.initialize()
        modified = ["dreamland.ini", "deployment.json", "lobby/catalog.json"]
        for relative in modified:
            (self.root / relative).write_text("operator edit\n")
        removed = self.root / "state_profiles/lobby-offshore.json"
        removed.unlink()

        report = initialize(self.root, {"replacement": True}, "replacement ini")

        self.assertFalse(report["initialized"])
        self.assertEqual(report["created"], [])
        self.assertEqual(report["absent"], ["state_profiles/lobby-offshore.json"])
        for relative in modified:
            self.assertIn(relative, report["preserved"])
            self.assertEqual((self.root / relative).read_text(), "operator edit\n")
        self.assertFalse(removed.exists())

    def test_completed_setup_preserves_removed_catalog_directory(self):
        self.initialize()
        for name in ("catalog.json", "provenance.json"):
            (self.root / "lobby" / name).unlink()
        (self.root / "lobby").rmdir()
        report = self.initialize()
        self.assertEqual(
            set(report["absent"]), {"lobby/catalog.json", "lobby/provenance.json"}
        )
        self.assertFalse((self.root / "lobby").exists())

    def test_interrupted_first_setup_recovers_missing_seeds_without_replacing_edits(
        self,
    ):
        publish = storage._publish

        def interrupted_publish(directory_fd, name, content, **kwargs):
            if name == "catalog.json":
                raise OSError("simulated disk error")
            return publish(directory_fd, name, content, **kwargs)

        with patch.object(storage, "_publish", side_effect=interrupted_publish):
            with self.assertRaises(StorageError):
                self.initialize()
        self.assertFalse((self.root / ".initialized.json").exists())
        (self.root / "dreamland.ini").write_text("recovery edit\n")

        report = self.initialize()

        self.assertTrue(report["initialized"])
        self.assertIn("dreamland.ini", report["preserved"])
        self.assertEqual((self.root / "dreamland.ini").read_text(), "recovery edit\n")
        self.assertTrue((self.root / "state_profiles/lobby-offshore.json").exists())
        self.assertTrue((self.root / ".initialized.json").exists())

    def test_concurrent_setup_is_rejected_without_creating_files(self):
        self.root.mkdir(mode=0o700)
        lock = self.root / ".initialize.lock"
        with lock.open("w") as handle:
            lock.chmod(0o600)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(StorageError, "another deployment"):
                self.initialize()
            self.assertEqual(list(self.root.iterdir()), [lock])
        self.assertTrue(self.initialize()["initialized"])

    def test_recovery_handles_abrupt_exit_during_atomic_publication(self):
        self.initialize()
        # These are the two possible on-disk stages left by SIGKILL: an
        # unpublished private temporary file and an alias of the published file.
        orphan = self.root / "lobby" / (".setup-" + "a" * 32)
        orphan.write_bytes(b"partial staging bytes")
        orphan.chmod(0o600)
        alias = self.root / (".setup-" + "b" * 32)
        os.link(self.root / ".initialized.json", alias)
        (self.root / "state_profiles/lobby-offshore.json").unlink()

        report = self.initialize()

        self.assertFalse(report["initialized"])
        self.assertEqual(report["absent"], ["state_profiles/lobby-offshore.json"])
        self.assertFalse(orphan.exists())
        self.assertFalse(alias.exists())
        self.assertEqual((self.root / ".initialized.json").stat().st_nlink, 1)

    def test_root_symlink_parent_traversal_and_insecure_permissions_are_rejected(self):
        target = Path(self.temporary.name) / "target"
        target.mkdir(mode=0o700)
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(StorageError):
            self.initialize()
        with self.assertRaises(StorageError):
            initialize(self.root / "nested", self.document, self.runtime_ini)
        self.assertEqual(list(target.iterdir()), [])
        self.root.unlink()
        self.root.mkdir(mode=0o755)
        with self.assertRaisesRegex(StorageError, "owner-only"):
            self.initialize()
        with self.assertRaisesRegex(StorageError, "parent traversal"):
            initialize(self.root / ".." / "other", self.document, self.runtime_ini)

    def test_wrong_owner_is_rejected_before_initialization(self):
        self.root.mkdir(mode=0o700)
        with patch.object(storage.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(StorageError, "current user"):
                self.initialize()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unsafe_managed_targets_fail_before_seed_publication(self):
        for kind in ("symlink", "fifo", "hardlink", "directory"):
            with self.subTest(kind=kind):
                self.root = Path(self.temporary.name) / kind
                self.root.mkdir(mode=0o700)
                target = self.root / "dreamland.ini"
                if kind == "symlink":
                    target.symlink_to("missing-outside-target")
                elif kind == "fifo":
                    os.mkfifo(target, mode=0o600)
                elif kind == "hardlink":
                    outside = self.root / "outside"
                    outside.write_text("untouched")
                    outside.chmod(0o600)
                    os.link(outside, target)
                else:
                    target.mkdir(mode=0o700)
                with self.assertRaises(StorageError):
                    self.initialize()
                self.assertFalse((self.root / "deployment.json").exists())
                self.assertFalse((self.root / ".initialized.json").exists())

    def test_invalid_marker_cannot_trigger_reseeding(self):
        self.initialize()
        (self.root / ".initialized.json").write_text("not a marker")
        (self.root / "lobby/catalog.json").unlink()
        with self.assertRaisesRegex(StorageError, "marker"):
            self.initialize()
        self.assertFalse((self.root / "lobby/catalog.json").exists())

    def test_secret_rotation_is_atomic_and_keeps_owner_only_permissions(self):
        self.initialize()
        write_secret(self.root, "ssh_password", "first password")
        path = self.root / "secrets/ssh_password"
        with path.open("rb") as original:
            write_secret(self.root, "ssh_password", "second password")
            self.assertEqual(original.read(), b"first password\n")
        self.assertEqual(read_secret(self.root, "ssh_password"), "second password")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertNotIn("second password", json.dumps(self.initialize()))

    def test_configuration_and_asset_reads_return_persisted_bytes(self):
        self.initialize()
        document = {"kind": "edited-deployment", "ssh_enabled": False}
        (self.root / "deployment.json").write_text(json.dumps(document))
        (self.root / "dreamland.ini").write_text("[server]\nport = 2200\n")

        self.assertEqual(
            read_configuration(self.root),
            (document, "[server]\nport = 2200\n"),
        )
        for relative in (
            "lobby/catalog.json",
            "lobby/provenance.json",
            "state_profiles/lobby-supermarket.json",
        ):
            self.assertEqual(
                read_managed(self.root, relative), (self.root / relative).read_bytes()
            )

    def test_configuration_reads_require_completion_and_valid_json_object(self):
        self.initialize()
        marker = self.root / ".initialized.json"
        marker.unlink()
        with self.assertRaisesRegex(StorageError, "incomplete"):
            read_configuration(self.root)
        self.initialize()
        marker.write_text(
            '{"kind":"dreamland.deployment.initialized","schema_version":true}'
        )
        with self.assertRaisesRegex(StorageError, "marker"):
            read_configuration(self.root)
        marker.write_text(json.dumps(storage._MARKER_DOCUMENT))
        for content in ("[1, 2]", "secret-content-do-not-echo"):
            (self.root / "deployment.json").write_text(content)
            with self.assertRaises(StorageError) as caught:
                read_configuration(self.root)
            self.assertNotIn("secret-content", str(caught.exception))
        (self.root / "deployment.json").write_text("{}")
        (self.root / "dreamland.ini").write_bytes(b"secret-content-do-not-echo\xff")
        with self.assertRaises(StorageError) as caught:
            read_configuration(self.root)
        self.assertNotIn("secret-content", str(caught.exception))

    def test_managed_reads_enforce_allowlist_size_and_safe_files(self):
        self.initialize()
        for relative in (
            "../secret-content",
            "secrets/openai_api_key",
            "state_profiles/other.json",
        ):
            with self.assertRaises(StorageError) as caught:
                read_managed(self.root, relative)
            self.assertNotIn("secret-content", str(caught.exception))
        config = self.root / "deployment.json"
        config.write_bytes(b"x" * (64 * 1024 + 1))
        with self.assertRaisesRegex(StorageError, "size limit"):
            read_configuration(self.root)
        config.unlink()
        config.symlink_to(self.root / "dreamland.ini")
        with self.assertRaises(StorageError):
            read_configuration(self.root)
        config.unlink()
        os.link(self.root / "dreamland.ini", config)
        with self.assertRaisesRegex(StorageError, "hardlinks"):
            read_configuration(self.root)
        config.unlink()
        with self.assertRaisesRegex(StorageError, "missing"):
            read_managed(self.root, "deployment.json")

    def test_profile_reader_supports_custom_saved_worlds_with_safe_names(self):
        self.initialize()
        profile = self.root / "state_profiles/operator-world.v2.json"
        profile.write_bytes(b'{"private": "saved world"}\n')
        profile.chmod(0o600)
        self.assertEqual(
            read_profile(self.root, "operator-world.v2"), profile.read_bytes()
        )
        for name in ("../secret-content", "world/child", "", "world\n", ".", "x" * 129):
            with self.assertRaises(StorageError) as caught:
                read_profile(self.root, name)
            self.assertNotIn("secret-content", str(caught.exception))
        profile.unlink()
        with self.assertRaisesRegex(StorageError, "missing"):
            read_profile(self.root, "operator-world.v2")
        os.mkfifo(profile, mode=0o600)
        with self.assertRaisesRegex(StorageError, "regular files"):
            read_profile(self.root, "operator-world.v2")

    def test_profile_reader_rejects_symlink_directories_and_oversized_profiles(self):
        self.initialize()
        profile = self.root / "state_profiles/oversized.json"
        with profile.open("wb") as handle:
            handle.truncate(16 * 1024 * 1024 + 1)
        profile.chmod(0o600)
        with self.assertRaisesRegex(StorageError, "size limit"):
            read_profile(self.root, "oversized")
        profiles = self.root / "state_profiles"
        outside = Path(self.temporary.name) / "moved-profiles"
        profiles.rename(outside)
        profiles.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StorageError):
            read_profile(self.root, "lobby-supermarket")

    def test_secret_validation_never_discloses_input(self):
        self.initialize()
        for name, value in (
            ("../secret-name-do-not-echo", "secret-value-do-not-echo"),
            ("openai_api_key", "secret-value-do-not-echo\nsecond-line"),
            ("openai_api_key", "secret-value-do-not-echo" * 400),
        ):
            with self.subTest(name=name):
                with self.assertRaises(StorageError) as caught:
                    write_secret(self.root, name, value)
                self.assertNotIn("do-not-echo", str(caught.exception))
        with self.assertRaises(StorageError):
            read_secret(self.root, "ssh_password")

    def test_secret_prevalidation_needs_no_storage_and_matches_writer_policy(self):
        validate_secret("a valid credential")
        for value in ("", " ", "secret-content\nextra", "x" * 4097):
            with self.assertRaises(StorageError) as caught:
                validate_secret(value)
            self.assertNotIn("secret-content", str(caught.exception))
        self.assertFalse(self.root.exists())

    def test_secret_unsafe_targets_cannot_be_read_or_replaced(self):
        self.initialize()
        outside = Path(self.temporary.name) / "private-value"
        outside.write_text("never disclose this credential\n")
        outside.chmod(0o600)
        target = self.root / "secrets/openai_api_key"
        for kind in ("symlink", "hardlink", "fifo", "world-readable"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    target.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, target)
                elif kind == "fifo":
                    os.mkfifo(target, mode=0o600)
                else:
                    target.write_text("never disclose this credential\n")
                    target.chmod(0o644)
                for operation in (
                    lambda: read_secret(self.root, "openai_api_key"),
                    lambda: write_secret(self.root, "openai_api_key", "replacement"),
                ):
                    with self.assertRaises(StorageError) as caught:
                        operation()
                    self.assertNotIn("never disclose", str(caught.exception))
                self.assertEqual(
                    outside.read_text(), "never disclose this credential\n"
                )
                target.unlink()


if __name__ == "__main__":
    unittest.main()
