from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from dreamland.product.forge import world_seed_from_document
from dreamland.product.forge_artifacts import (
    MAX_PROFILE_BYTES,
    ImmutableForgeStore,
    SingleAttemptForge,
    canonical_revision_document,
    compile_world_seed,
    validate_forge_profile,
)
from dreamland.state_profiles import StateProfile


def _seed():
    projection = json.loads(
        Path(
            "contracts/world-forge-api/v0.1.0-rc.2/fixtures/canonical-valid/lifecycle/accepted.json"
        ).read_text()
    )
    return world_seed_from_document(projection["seed"])


def _profile() -> StateProfile:
    return StateProfile(
        name="candidate-a",
        level="world",
        created_at="2026-08-25T20:00:00+00:00",
        source_session_id="mutable-session-label",
        state={
            "world_state": {
                "name": "comet-observatory",
                "entry_host": "tracking-console",
                "active_host": "tracking-console",
                "purpose": {
                    "theme": "Weathered comet observatory",
                    "description": "Radio clocks disagree during a comet watch.",
                },
                "hosts_by_name": {
                    "tracking-console": {
                        "hostname": "tracking-console",
                        "login_banner": "Comet observatory tracking console\n",
                        "purpose": {"theme": "Track the comet"},
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


class ProductForgeArtifactTests(unittest.TestCase):
    def test_seed_compiler_is_exact_and_deterministic(self) -> None:
        first = compile_world_seed(_seed())
        second = compile_world_seed(_seed())
        self.assertEqual(first, second)
        self.assertEqual(first.seed_hash, _seed().seed_hash)
        for answer in _seed().answers:
            self.assertIn(answer.value, first.text)
        self.assertNotIn("provider", first.to_document())

    def test_validator_checks_structure_residue_limits_and_synthetic_boot(self) -> None:
        valid = validate_forge_profile(_profile())
        self.assertTrue(valid.valid, valid.issues)
        self.assertTrue(valid.synthetic_boot_passed)

        cases = {}
        missing_entry = deepcopy(_profile().state)
        missing_entry["world_state"]["entry_host"] = "missing"
        cases["entry_host_invalid"] = missing_entry
        residue = deepcopy(_profile().state)
        residue["world_state"]["purpose"]["theme"] = "Operator-defined place"
        cases["blank_residue_present"] = residue
        shell = deepcopy(_profile().state)
        shell["world_state"]["hosts_by_name"]["tracking-console"]["shell"]["default"][
            "command"
        ] = "bash"
        cases["starting_shell_invalid"] = shell
        missing_banner = deepcopy(_profile().state)
        missing_banner["world_state"]["hosts_by_name"]["tracking-console"].pop(
            "login_banner"
        )
        cases["login_banner_invalid"] = missing_banner
        for expected, state in cases.items():
            with self.subTest(expected=expected):
                profile = StateProfile("bad", "world", state, _profile().created_at)
                report = validate_forge_profile(profile)
                self.assertFalse(report.valid)
                self.assertIn(expected, {issue.code for issue in report.issues})

    def test_revision_hash_excludes_mutable_profile_labels(self) -> None:
        first = _profile()
        second = StateProfile(
            "another-name",
            "world",
            deepcopy(first.state),
            "2030-01-01T00:00:00+00:00",
            source_session_id="another-session",
        )
        self.assertEqual(
            canonical_revision_document(first), canonical_revision_document(second)
        )

    def test_store_is_owner_only_content_addressed_and_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ImmutableForgeStore(Path(directory) / "revisions")
            first = store.save(_profile())
            second = store.save(_profile())
            self.assertEqual(first.revision_id, second.revision_id)
            self.assertEqual(first.path, second.path)
            self.assertTrue(first.path.name.startswith("sha256-"))
            self.assertEqual(os.stat(store.directory).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(first.path).st_mode & 0o777, 0o600)
            loaded = json.loads(first.path.read_text())
            self.assertEqual(loaded, first.document)

    def test_store_reopens_only_the_exact_verified_revision_without_side_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ImmutableForgeStore(root / "revisions")
            saved = store.save(_profile())
            before = sorted(path.relative_to(root) for path in root.rglob("*"))

            loaded = store.load(saved.revision_id, saved.digest)

            after = sorted(path.relative_to(root) for path in root.rglob("*"))
            self.assertEqual(after, before)
            self.assertEqual(loaded, saved)
            self.assertEqual(loaded.profile.level, "world")
            self.assertEqual(loaded.profile.state, _profile().state)
            self.assertTrue(loaded.profile.name.startswith("world-forge-"))
            self.assertEqual(
                loaded.profile.created_at,
                "1970-01-01T00:00:00+00:00",
            )
            self.assertFalse((root / "state_profiles").exists())
            self.assertFalse((root / "default.json").exists())
            self.assertFalse((root / "session_index.json").exists())

    def test_store_rejects_invalid_or_mismatched_reopen_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ImmutableForgeStore(Path(directory) / "revisions")
            saved = store.save(_profile())
            other = "0" * 64
            cases = (
                ("not-a-revision", saved.digest),
                (saved.revision_id, "not-a-digest"),
                (f"world-revision:sha256:{other}", saved.digest),
                (saved.revision_id.upper(), saved.digest),
                (saved.revision_id, saved.digest.upper()),
            )
            for revision_id, digest in cases:
                with self.subTest(revision_id=revision_id, digest=digest):
                    with self.assertRaises(ValueError):
                        store.load(revision_id, digest)
            missing_digest = f"sha256:{other}"
            with self.assertRaises(ValueError):
                store.load(f"world-revision:sha256:{other}", missing_digest)

    def test_store_rejects_unsafe_reopen_file_types_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ImmutableForgeStore(root / "revisions")
            saved = store.save(_profile())
            os.chmod(store.directory, 0o750)
            with self.assertRaises(ValueError):
                store.load(saved.revision_id, saved.digest)
            os.chmod(store.directory, 0o700)
            os.chmod(saved.path, 0o640)
            with self.assertRaises(ValueError):
                store.load(saved.revision_id, saved.digest)

            saved.path.unlink()
            saved.path.mkdir()
            with self.assertRaises(ValueError):
                store.load(saved.revision_id, saved.digest)
            saved.path.rmdir()
            target = root / "outside.json"
            target.write_text("outside\n", encoding="utf-8")
            saved.path.symlink_to(target)
            with self.assertRaises(ValueError):
                store.load(saved.revision_id, saved.digest)

    @unittest.skipUnless(os.geteuid() == 0, "changing file owner requires root")
    def test_store_rejects_a_revision_owned_by_another_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ImmutableForgeStore(Path(directory) / "revisions")
            saved = store.save(_profile())
            os.chown(saved.path, 65534, -1)
            with self.assertRaises(ValueError):
                store.load(saved.revision_id, saved.digest)

    def test_store_rejects_tampered_noncanonical_or_oversized_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ImmutableForgeStore(root / "revisions")
            saved = store.save(_profile())
            saved.path.write_bytes(saved.path.read_bytes() + b" ")
            with self.assertRaises(ValueError):
                store.load(saved.revision_id, saved.digest)

            noncanonical = json.dumps(saved.document, indent=2).encode("utf-8") + b"\n"
            revision_id, digest = self._write_raw_revision(root, noncanonical)
            with self.assertRaises(ValueError):
                store.load(revision_id, digest)

            oversized = b"x" * (MAX_PROFILE_BYTES + 1)
            revision_id, digest = self._write_raw_revision(root, oversized)
            with self.assertRaises(ValueError):
                store.load(revision_id, digest)

    def test_store_rejects_invalid_utf8_json_and_revision_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ImmutableForgeStore(root / "revisions")
            for name, data in (
                ("utf8", b"\xff\n"),
                ("json", b'{"kind":\n'),
                (
                    "duplicate",
                    b'{"kind":"dreamland.world_forge_revision","kind":'
                    b'"dreamland.world_forge_revision"}\n',
                ),
                (
                    "schema",
                    b'{"kind":"dreamland.world_forge_revision",'
                    b'"profile":{"level":"world","state":{}},'
                    b'"schema_revision":"2"}\n',
                ),
            ):
                with self.subTest(name=name):
                    revision_id, digest = self._write_raw_revision(root, data)
                    with self.assertRaises(ValueError):
                        store.load(revision_id, digest)

    def test_store_revalidates_artifact_profile_and_synthetic_boot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = canonical_revision_document(_profile())
            profile = document["profile"]
            assert isinstance(profile, dict)
            state = profile["state"]
            assert isinstance(state, dict)
            world = state["world_state"]
            assert isinstance(world, dict)
            world["entry_host"] = "missing"
            data = (
                json.dumps(
                    document,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            revision_id, digest = self._write_raw_revision(root, data)
            with self.assertRaises(ValueError):
                ImmutableForgeStore(root / "revisions").load(revision_id, digest)

    def test_invalid_profile_is_never_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = deepcopy(_profile().state)
            state["world_state"]["entry_host"] = "missing"
            profile = StateProfile("bad", "world", state, _profile().created_at)
            store = ImmutableForgeStore(Path(directory) / "revisions")
            with self.assertRaises(ValueError):
                store.save(profile)
            self.assertFalse(store.directory.exists())

    def test_malformed_referenced_hosts_fail_boot_and_never_create_a_revision(
        self,
    ) -> None:
        cases: dict[str, tuple[dict[str, object], set[str]]] = {}
        malformed_entry = deepcopy(_profile().state)
        malformed_entry["world_state"]["hosts_by_name"]["tracking-console"] = (
            "not-a-host"
        )
        cases["entry"] = (
            malformed_entry,
            {"entry_host_invalid", "active_host_invalid", "starting_shell_invalid"},
        )
        malformed_active = deepcopy(_profile().state)
        malformed_active["world_state"]["active_host"] = "active-console"
        malformed_active["world_state"]["hosts_by_name"]["active-console"] = (
            "not-a-host"
        )
        cases["active"] = (malformed_active, {"active_host_invalid"})

        class Generator:
            def __init__(self, profile: StateProfile) -> None:
                self.profile = profile

            def generate(self, _instruction):
                return self.profile

        for name, (state, expected) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                profile = StateProfile("bad", "world", state, _profile().created_at)
                report = validate_forge_profile(profile)
                self.assertFalse(report.valid)
                self.assertFalse(report.synthetic_boot_passed)
                self.assertTrue(
                    expected.issubset({issue.code for issue in report.issues})
                )
                store = ImmutableForgeStore(Path(directory) / "revisions")
                with self.assertRaises(ValueError):
                    SingleAttemptForge(Generator(profile), store).run(_seed())
                self.assertFalse(store.directory.exists())

    def test_store_refuses_an_existing_symlink_at_the_revision_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ImmutableForgeStore(Path(directory) / "revisions")
            revision = store.save(_profile())
            revision.path.unlink()
            target = Path(directory) / "outside.json"
            target.write_text("outside\n")
            revision.path.symlink_to(target)
            with self.assertRaises(ValueError):
                store.save(_profile())
            self.assertEqual(target.read_text(), "outside\n")

    def test_single_attempt_slice_selects_only_a_valid_immutable_revision(self) -> None:
        class Generator:
            def __init__(self, profile: StateProfile) -> None:
                self.profile = profile
                self.instructions = []

            def generate(self, instruction):
                self.instructions.append(instruction)
                return self.profile

        with tempfile.TemporaryDirectory() as directory:
            generator = Generator(_profile())
            result = SingleAttemptForge(
                generator, ImmutableForgeStore(Path(directory) / "revisions")
            ).run(_seed())
            self.assertEqual(generator.instructions, [result.instruction])
            self.assertTrue(result.validation.valid)
            self.assertEqual(result.receipt["human_selection"], "none")
            self.assertEqual(
                result.receipt["provenance"]["kind"],
                "single_attempt_live_vertical_slice",
            )
            self.assertTrue(result.revision.path.is_file())

    @staticmethod
    def _write_raw_revision(root: Path, data: bytes) -> tuple[str, str]:
        digest_hex = hashlib.sha256(data).hexdigest()
        revision_directory = root / "revisions"
        revision_directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(revision_directory, 0o700)
        path = revision_directory / f"sha256-{digest_hex}.json"
        path.write_bytes(data)
        os.chmod(path, 0o600)
        return (
            f"world-revision:sha256:{digest_hex}",
            f"sha256:{digest_hex}",
        )


if __name__ == "__main__":
    unittest.main()
