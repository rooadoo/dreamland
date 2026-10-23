from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import dreamland.product.authored_artifacts as authored_artifacts
from dreamland.product.authored_artifacts import (
    AUTHORED_REVISION_KIND,
    AUTHORED_REVISION_SCHEMA,
    MAX_AUTHORED_REVISION_BYTES,
    MAX_CONFIGURED_PROFILE_BYTES,
    ImmutableAuthoredStore,
    authored_revision_digest,
    canonical_authored_revision_document,
    import_configured_authored_profile,
    load_configured_authored_profile,
)
from dreamland.state_profiles import StateProfile


def _colonyship_profile(
    *,
    name: str = "colonyship-a1",
    created_at: str = "2026-08-27T12:00:00+00:00",
    source_session_id: str | None = "operator-session-private",
    comment: str | None = "private operator note",
) -> StateProfile:
    return StateProfile(
        name=name,
        level="world",
        created_at=created_at,
        source_session_id=source_session_id,
        comment=comment,
        state={
            "world_state": {
                "name": "ark-lightyear",
                "entry_host": "navigation-console",
                "active_host": "navigation-console",
                "purpose": {
                    "theme": "A multigenerational colony ship approaching Tau Ceti",
                    "description": (
                        "The navigation watch reconciles a late course correction "
                        "with aging flight computers."
                    ),
                },
                "hosts_by_name": {
                    "navigation-console": {
                        "login_banner": "Test console\n",
                        "hostname": "navigation-console",
                        "purpose": {"theme": "Monitor guidance and stellar fixes"},
                        "shell": {
                            "default": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["bash"],
                            }
                        },
                    }
                },
            }
        },
    )


def _source_document(profile: StateProfile | None = None) -> dict[str, object]:
    return (profile or _colonyship_profile()).to_document()


class ProductAuthoredArtifactTests(unittest.TestCase):
    def test_revision_identity_is_deterministic_and_metadata_independent(self) -> None:
        first = _colonyship_profile()
        second = _colonyship_profile(
            name="filename-does-not-matter",
            created_at="2040-01-01T00:00:00+00:00",
            source_session_id="different-private-session",
            comment="different private note",
        )

        self.assertEqual(
            canonical_authored_revision_document(first),
            canonical_authored_revision_document(second),
        )
        self.assertEqual(
            authored_revision_digest(first), authored_revision_digest(second)
        )
        document = canonical_authored_revision_document(first)
        self.assertEqual(
            document,
            {
                "kind": AUTHORED_REVISION_KIND,
                "schema_revision": AUTHORED_REVISION_SCHEMA,
                "profile": {"level": "world", "state": first.state},
            },
        )
        expected = (
            json.dumps(
                document,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(
            authored_revision_digest(first),
            f"sha256:{hashlib.sha256(expected).hexdigest()}",
        )

    def test_import_requires_exact_pin_before_store_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "friendly-filename.json"
            self._write_source(source)
            store = ImmutableAuthoredStore(root / "revisions")

            with self.assertRaises(ValueError):
                import_configured_authored_profile(
                    source,
                    expected_digest=f"sha256:{'0' * 64}",
                    store=store,
                )

            self.assertFalse(store.directory.exists())

    def test_invalid_profile_fails_validation_without_creating_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _source_document()
            state = document["state"]
            assert isinstance(state, dict)
            world = state["world_state"]
            assert isinstance(world, dict)
            world["entry_host"] = "missing"
            source = root / "invalid.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            store = ImmutableAuthoredStore(root / "revisions")

            with self.assertRaisesRegex(ValueError, "profile schema is invalid"):
                load_configured_authored_profile(source)
            with self.assertRaises(ValueError):
                import_configured_authored_profile(
                    source,
                    expected_digest=f"sha256:{'0' * 64}",
                    store=store,
                )

            self.assertFalse(store.directory.exists())

    def test_malformed_source_never_creates_an_artifact_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "invalid-utf8": b"\xff\n",
                "duplicate": (
                    b'{"kind":"dreamland.state_profile","kind":'
                    b'"dreamland.state_profile"}\n'
                ),
                "constant": b'{"value":Infinity}\n',
            }
            for name, data in cases.items():
                with self.subTest(name=name):
                    source = root / f"{name}.json"
                    source.write_bytes(data)
                    store = ImmutableAuthoredStore(root / f"{name}-revisions")
                    with self.assertRaises(ValueError):
                        import_configured_authored_profile(
                            source,
                            expected_digest=f"sha256:{'0' * 64}",
                            store=store,
                        )
                    self.assertFalse(store.directory.exists())

    def test_source_loader_is_strict_bounded_and_accepts_name_filename_mismatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "not-the-internal-name.json"
            self._write_source(valid)
            self.assertEqual(
                load_configured_authored_profile(valid).name, "colonyship-a1"
            )

            cases = {
                "invalid-utf8.json": b"\xff\n",
                "constant.json": b'{"value":NaN}\n',
                "duplicate.json": (
                    b'{"kind":"dreamland.state_profile","kind":'
                    b'"dreamland.state_profile"}\n'
                ),
                "unknown-key.json": json.dumps(
                    {**_source_document(), "profile_path": "/private/profile.json"}
                ).encode("utf-8"),
            }
            for filename, data in cases.items():
                with self.subTest(filename=filename):
                    path = root / filename
                    path.write_bytes(data)
                    with self.assertRaises(ValueError):
                        load_configured_authored_profile(path)

            oversized = root / "oversized.json"
            oversized.write_bytes(b"x" * (MAX_CONFIGURED_PROFILE_BYTES + 1))
            with self.assertRaises(ValueError):
                load_configured_authored_profile(oversized)

    def test_source_loader_rejects_symlink_and_nonregular_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            self._write_source(target)
            symlink = root / "linked.json"
            symlink.symlink_to(target)
            folder = root / "folder"
            folder.mkdir()

            for path in (symlink, folder):
                with self.subTest(path=path.name), self.assertRaises(ValueError):
                    load_configured_authored_profile(path)

    def test_store_is_owner_only_create_only_idempotent_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = _colonyship_profile()
            digest = authored_revision_digest(profile)
            store = ImmutableAuthoredStore(root / "revisions")

            first = store.save(profile, expected_digest=digest)
            second = store.save(profile, expected_digest=digest)
            reopened = store.load(first.revision_id, first.digest)
            artifact_path = self._artifact_path(store, first.digest)

            self.assertEqual(first, second)
            self.assertEqual(reopened, first)
            self.assertEqual(os.stat(store.directory).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(artifact_path).st_mode & 0o777, 0o600)
            self.assertEqual(artifact_path.read_bytes(), first.canonical_bytes())
            self.assertTrue(first.revision_id.startswith("world-revision:sha256:"))

    def test_concurrent_identical_saves_publish_one_complete_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _colonyship_profile()
            digest = authored_revision_digest(profile)
            store = ImmutableAuthoredStore(Path(directory) / "revisions")

            with ThreadPoolExecutor(max_workers=8) as executor:
                saved = list(
                    executor.map(
                        lambda _index: store.save(
                            profile,
                            expected_digest=digest,
                        ),
                        range(32),
                    )
                )

            self.assertTrue(all(item == saved[0] for item in saved))
            self.assertEqual(
                store.load(saved[0].revision_id, saved[0].digest), saved[0]
            )
            self.assertEqual(
                [path.name for path in store.directory.iterdir()],
                [f"sha256-{digest.removeprefix('sha256:')}.json"],
            )

    def test_orphan_temporary_file_does_not_wedge_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _colonyship_profile()
            digest = authored_revision_digest(profile)
            store = ImmutableAuthoredStore(Path(directory) / "revisions")
            store.directory.mkdir(mode=0o700)
            residue = store.directory / ".authored-interrupted-writer.tmp"
            residue.write_bytes(b"partial")
            residue.chmod(0o600)

            saved = store.save(profile, expected_digest=digest)

            self.assertEqual(store.load(saved.revision_id, saved.digest), saved)
            self.assertEqual(residue.read_bytes(), b"partial")

    def test_reopened_profile_has_deterministic_synthetic_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _colonyship_profile()
            store = ImmutableAuthoredStore(Path(directory) / "revisions")
            saved = store.save(
                profile, expected_digest=authored_revision_digest(profile)
            )

            first = saved.allocate_profile()
            second = store.load(saved.revision_id, saved.digest).allocate_profile()

            self.assertEqual(first, second)
            self.assertEqual(
                first.name, f"authored-world-{saved.digest.removeprefix('sha256:')}"
            )
            self.assertEqual(first.created_at, "1970-01-01T00:00:00+00:00")
            self.assertIsNone(first.source_session_id)
            self.assertIsNone(first.comment)

    def test_allocations_cannot_mutate_the_stored_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _colonyship_profile()
            store = ImmutableAuthoredStore(Path(directory) / "revisions")
            saved = store.save(
                profile, expected_digest=authored_revision_digest(profile)
            )
            original = saved.canonical_bytes()
            profile.state.clear()

            allocated_document = saved.allocate_document()
            allocated_document["kind"] = "mutated"
            allocated_profile = saved.allocate_profile()
            allocated_profile.state.clear()

            self.assertEqual(saved.canonical_bytes(), original)
            self.assertEqual(saved.allocate_document()["kind"], AUTHORED_REVISION_KIND)
            self.assertTrue(saved.allocate_profile().state)

    def test_import_snapshots_one_read_and_never_reconsults_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "selected-world.json"
            profile = _colonyship_profile()
            self._write_source(source, profile)
            store = ImmutableAuthoredStore(root / "revisions")
            reader = authored_artifacts._read_configured_source

            with patch.object(
                authored_artifacts,
                "_read_configured_source",
                wraps=reader,
            ) as read_source:
                saved = import_configured_authored_profile(
                    source,
                    expected_digest=authored_revision_digest(profile),
                    store=store,
                )

            self.assertEqual(read_source.call_count, 1)
            profile.state.clear()
            source.unlink()
            source.write_text('{"replacement":true}\n', encoding="utf-8")
            source.unlink()

            reopened = store.load(saved.revision_id, saved.digest)
            self.assertEqual(reopened, saved)
            self.assertEqual(
                reopened.allocate_profile().state["world_state"]["name"],
                "ark-lightyear",
            )

    def test_revision_and_repr_never_leak_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "private-path-colonyship.json"
            profile = _colonyship_profile(
                name="secret-profile-name",
                created_at="2037-04-05T06:07:08+00:00",
                source_session_id="secret-source-session",
                comment="secret profile comment",
            )
            self._write_source(source, profile)
            saved = import_configured_authored_profile(
                source,
                expected_digest=authored_revision_digest(profile),
                store=ImmutableAuthoredStore(root / "revisions"),
            )
            exposed = repr(saved) + saved.canonical_bytes().decode("utf-8")

            for secret in (
                str(source),
                source.name,
                profile.name,
                profile.created_at,
                profile.source_session_id,
                profile.comment,
            ):
                assert secret is not None
                self.assertNotIn(secret, exposed)
            self.assertFalse(hasattr(saved, "path"))
            self.assertFalse(hasattr(saved, "source_name"))

    def test_store_rejects_tamper_noncanonical_duplicate_and_oversize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = _colonyship_profile()
            store = ImmutableAuthoredStore(root / "revisions")
            saved = store.save(
                profile, expected_digest=authored_revision_digest(profile)
            )
            artifact_path = self._artifact_path(store, saved.digest)
            artifact_path.write_bytes(artifact_path.read_bytes() + b" ")
            with self.assertRaises(ValueError):
                store.load(saved.revision_id, saved.digest)

            document = canonical_authored_revision_document(profile)
            noncanonical = json.dumps(document, indent=2).encode("utf-8") + b"\n"
            duplicate = (
                b'{"kind":"dreamland.authored_world_revision","kind":'
                b'"dreamland.authored_world_revision"}\n'
            )
            oversized = b"x" * (MAX_AUTHORED_REVISION_BYTES + 1)
            for name, data in (
                ("noncanonical", noncanonical),
                ("duplicate", duplicate),
                ("oversized", oversized),
            ):
                with self.subTest(name=name):
                    revision_id, digest = self._write_raw_revision(store, data)
                    with self.assertRaises(ValueError):
                        store.load(revision_id, digest)

    def test_store_rejects_unsafe_directory_file_modes_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = _colonyship_profile()
            store = ImmutableAuthoredStore(root / "revisions")
            saved = store.save(
                profile, expected_digest=authored_revision_digest(profile)
            )
            path = self._artifact_path(store, saved.digest)

            os.chmod(store.directory, 0o750)
            with self.assertRaises(ValueError):
                store.load(saved.revision_id, saved.digest)
            os.chmod(store.directory, 0o700)
            os.chmod(path, 0o640)
            with self.assertRaises(ValueError):
                store.load(saved.revision_id, saved.digest)
            os.chmod(path, 0o600)

            outside = root / "outside.json"
            outside.write_text("outside\n", encoding="utf-8")
            path.unlink()
            path.symlink_to(outside)
            with self.assertRaises(ValueError):
                store.load(saved.revision_id, saved.digest)
            with self.assertRaises(ValueError):
                store.save(profile, expected_digest=authored_revision_digest(profile))
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

            real_store = root / "real-store"
            real_store.mkdir(mode=0o700)
            linked_store = root / "linked-store"
            linked_store.symlink_to(real_store, target_is_directory=True)
            with self.assertRaises(ValueError):
                ImmutableAuthoredStore(linked_store).save(
                    profile, expected_digest=authored_revision_digest(profile)
                )
            self.assertEqual(list(real_store.iterdir()), [])

    @unittest.skipUnless(os.geteuid() == 0, "changing file owner requires root")
    def test_store_rejects_revision_owned_by_another_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _colonyship_profile()
            store = ImmutableAuthoredStore(Path(directory) / "revisions")
            saved = store.save(
                profile, expected_digest=authored_revision_digest(profile)
            )
            os.chown(self._artifact_path(store, saved.digest), 65534, -1)
            with self.assertRaises(ValueError):
                store.load(saved.revision_id, saved.digest)

    def test_reopen_requires_exact_identity_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _colonyship_profile()
            store = ImmutableAuthoredStore(Path(directory) / "revisions")
            saved = store.save(
                profile, expected_digest=authored_revision_digest(profile)
            )
            other = "0" * 64
            for revision_id, digest in (
                ("not-a-revision", saved.digest),
                (saved.revision_id, "not-a-digest"),
                (f"world-revision:sha256:{other}", saved.digest),
                (saved.revision_id.upper(), saved.digest),
                (saved.revision_id, saved.digest.upper()),
            ):
                with self.subTest(revision_id=revision_id, digest=digest):
                    with self.assertRaises(ValueError):
                        store.load(revision_id, digest)

            forge_envelope = deepcopy(saved.allocate_document())
            forge_envelope["kind"] = "dreamland.world_forge_revision"
            data = (
                json.dumps(forge_envelope, separators=(",", ":"), sort_keys=True) + "\n"
            ).encode("utf-8")
            revision_id, digest = self._write_raw_revision(store, data)
            with self.assertRaises(ValueError):
                store.load(revision_id, digest)

    @staticmethod
    def _write_source(path: Path, profile: StateProfile | None = None) -> None:
        path.write_text(
            json.dumps(_source_document(profile), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _artifact_path(store: ImmutableAuthoredStore, digest: str) -> Path:
        return store.directory / f"sha256-{digest.removeprefix('sha256:')}.json"

    @staticmethod
    def _write_raw_revision(
        store: ImmutableAuthoredStore, data: bytes
    ) -> tuple[str, str]:
        digest_hex = hashlib.sha256(data).hexdigest()
        store.directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(store.directory, 0o700)
        path = store.directory / f"sha256-{digest_hex}.json"
        path.write_bytes(data)
        os.chmod(path, 0o600)
        return (
            f"world-revision:sha256:{digest_hex}",
            f"sha256:{digest_hex}",
        )


if __name__ == "__main__":
    unittest.main()
