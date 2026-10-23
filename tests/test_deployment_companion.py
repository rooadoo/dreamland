"""Role storage, non-secret handoff and exact metadata contract boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

from dreamland.deployment import companion, storage

CONTRACT = (
    Path(__file__).resolve().parents[1] / "contracts/backend-deployment/v0.1.0-rc.1"
)


class CompanionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "role"
        self.intent = {
            "role": "attendee",
            "project": "dreamland-demo",
            "network": "dreamland-demo-companion",
            "image": "sha256:" + "1" * 64,
            "revision": "2" * 40,
        }

    def initialize(self) -> dict:
        return companion.initialize(self.root, **self.intent)

    def snapshot(self) -> dict:
        return {
            path.name: (
                path.read_bytes(),
                path.stat().st_ino,
                path.stat().st_mtime_ns,
                stat.S_IMODE(path.stat().st_mode),
            )
            for path in self.root.iterdir()
        }

    def rewrite(self, document: object) -> None:
        (self.root / "descriptor.json").write_text(json.dumps(document))

    def test_initialization_is_private_nonsecret_and_exactly_preserved(self) -> None:
        report = self.initialize()
        self.assertTrue(report["initialized"])
        descriptor, token = companion.load_service(self.root, role="attendee")
        self.assertEqual(report["handoff"], descriptor)
        self.assertNotIn(token, json.dumps(report))
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertTrue(all(value[3] == 0o600 for value in self.snapshot().values()))
        self.assertRegex(token, r"^dreamland-attendee-transport:[A-Za-z0-9_-]{64}$")
        self.assertEqual(
            descriptor["transport"]["alias"], "dreamland-demo-attendee-api"
        )
        self.assertFalse(any(descriptor["capabilities"].values()))
        self.assertEqual(descriptor["product_contracts"], [])
        before = self.snapshot()
        again = self.initialize()
        self.assertEqual(again, {"initialized": False, "handoff": descriptor})
        self.assertEqual(before, self.snapshot())

    def test_describe_never_reads_token_bytes(self) -> None:
        self.initialize()
        original = storage._read_file
        names = []

        def read(directory_fd, name, *, maximum):
            self.assertEqual(name, "descriptor.json")
            names.append(name)
            return original(directory_fd, name, maximum=maximum)

        with patch.object(storage, "_read_file", side_effect=read):
            descriptor = companion.describe(self.root)
        self.assertEqual(names, ["descriptor.json"])
        self.assertEqual(descriptor["role"], "attendee")

    def test_independent_roles_have_distinct_credentials_and_ids(self) -> None:
        attendee = self.initialize()["handoff"]
        operator_root = self.directory / "operator"
        operator = companion.initialize(
            operator_root, **dict(self.intent, role="operator")
        )["handoff"]
        self.assertNotEqual(attendee["credential_id"], operator["credential_id"])
        self.assertNotEqual(
            companion.load_service(self.root, role="attendee")[1],
            companion.load_service(operator_root, role="operator")[1],
        )
        for root, wrong_role in ((self.root, "operator"), (operator_root, "attendee")):
            with (
                self.subTest(role=wrong_role),
                self.assertRaises(companion.CompanionError),
            ):
                companion.load_service(root, role=wrong_role)

    def test_changed_intent_never_rotates_or_rewrites(self) -> None:
        self.initialize()
        before = self.snapshot()
        for change in (
            {"role": "operator"},
            {"project": "other"},
            {"network": "other"},
            {"image": "sha256:" + "3" * 64},
            {"revision": "4" * 40},
        ):
            with (
                self.subTest(change=change),
                self.assertRaises(companion.CompanionError),
            ):
                companion.initialize(self.root, **dict(self.intent, **change))
            self.assertEqual(before, self.snapshot())

    def test_invalid_intent_fails_before_creating_directories(self) -> None:
        for change in (
            {"role": "presenter"},
            {"role": []},
            {"project": "bad/name"},
            {"project": "bad_name"},
            {"project": "a" * 41},
            {"network": "bad network"},
            {"network": "a" * 64},
            {"image": "dreamland:latest"},
            {"revision": "abcdef0"},
        ):
            with (
                self.subTest(change=change),
                self.assertRaises(companion.CompanionError),
            ):
                companion.initialize(self.root, **dict(self.intent, **change))
            self.assertFalse(self.root.exists())

    def test_partial_initialization_is_not_repaired_or_rotated(self) -> None:
        original = storage._publish

        def publish(directory_fd, name, content):
            if name == "descriptor.json":
                raise OSError("sensitive-injected-error")
            return original(directory_fd, name, content)

        with (
            patch.object(storage, "_publish", side_effect=publish),
            self.assertRaises(companion.CompanionError) as error,
        ):
            self.initialize()
        self.assertNotIn("sensitive-injected-error", str(error.exception))
        before = self.snapshot()
        with self.assertRaises(companion.CompanionError):
            self.initialize()
        self.assertEqual(before, self.snapshot())
        with self.assertRaises(companion.CompanionError):
            companion.describe(self.root)

    def test_concurrent_initializer_is_rejected_before_secret_publication(self) -> None:
        self.root.mkdir(mode=0o700)
        with (
            storage._root_directory(self.root, create=False) as root_fd,
            storage._lock(root_fd),
        ):
            with self.assertRaises(companion.CompanionError):
                self.initialize()
            self.assertEqual(
                {path.name for path in self.root.iterdir()}, {".initialize.lock"}
            )
        self.assertTrue(self.initialize()["initialized"])

    def test_unrelated_directory_contents_are_rejected(self) -> None:
        self.root.mkdir(mode=0o700)
        (self.root / "dreamland.ini").write_text("unrelated-runtime")
        with self.assertRaises(companion.CompanionError):
            self.initialize()
        self.assertEqual({path.name for path in self.root.iterdir()}, {"dreamland.ini"})

    def test_directory_symlink_and_traversal_are_rejected(self) -> None:
        self.initialize()
        alias = self.directory / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        for root in (alias, alias / "child", self.root / ".." / "role"):
            with self.subTest(root=root), self.assertRaises(companion.CompanionError):
                companion.describe(root)

    def test_broad_directory_and_file_permissions_are_rejected(self) -> None:
        self.initialize()
        self.root.chmod(0o750)
        with self.assertRaises(companion.CompanionError):
            companion.describe(self.root)
        self.root.chmod(0o700)
        for name in ("descriptor.json", "transport.token", ".initialize.lock"):
            for mode in (0o644, 0o400, 0o700):
                with self.subTest(name=name, mode=mode):
                    (self.root / name).chmod(mode)
                    with self.assertRaises(companion.CompanionError):
                        companion.describe(self.root)
                    (self.root / name).chmod(0o600)

    def test_symlinks_hardlinks_and_special_files_are_rejected(self) -> None:
        self.initialize()
        for name in ("descriptor.json", "transport.token", ".initialize.lock"):
            with self.subTest(name=name):
                path = self.root / name
                saved = path.read_bytes()
                path.unlink()
                outside = self.directory / "outside"
                outside.write_bytes(saved)
                outside.chmod(0o600)
                path.symlink_to(outside)
                with self.assertRaises(companion.CompanionError):
                    companion.describe(self.root)
                path.unlink()
                os.link(outside, path)
                with self.assertRaises(companion.CompanionError):
                    companion.describe(self.root)
                path.unlink()
                outside.unlink()
                os.mkfifo(path, 0o600)
                with self.assertRaises(companion.CompanionError):
                    companion.describe(self.root)
                path.unlink()
                path.write_bytes(saved)
                path.chmod(0o600)

    def test_missing_files_and_oversized_descriptor_fail_closed(self) -> None:
        self.initialize()
        for name in ("descriptor.json", "transport.token", ".initialize.lock"):
            path = self.root / name
            saved = path.read_bytes()
            path.unlink()
            with self.subTest(name=name), self.assertRaises(companion.CompanionError):
                companion.describe(self.root)
            path.write_bytes(saved)
            path.chmod(0o600)
        (self.root / "descriptor.json").write_bytes(b" " * (16 * 1024 + 1))
        with self.assertRaises(companion.CompanionError):
            companion.describe(self.root)

    def test_duplicate_json_fields_are_rejected_without_echo(self) -> None:
        self.initialize()
        for content in (
            b'{"role":"attendee","role":"secret-value"}',
            b'{"transport":{"network":"a","network":"secret-value"}}',
            b"\xffprivate-value",
        ):
            with self.subTest(content=content):
                (self.root / "descriptor.json").write_bytes(content)
                with self.assertRaises(companion.CompanionError) as error:
                    companion.describe(self.root)
                self.assertNotIn("secret-value", str(error.exception))
                self.assertNotIn("private-value", str(error.exception))

    def test_descriptor_semantics_reject_extra_authority_and_false_readiness(
        self,
    ) -> None:
        original = self.initialize()["handoff"]
        mutations = (
            lambda d: d.update(secret="private-value"),
            lambda d: d.update(schema_version="1.0.0"),
            lambda d: d["transport"].update(alias="other-attendee-api"),
            lambda d: d["transport"].update(base_url="http://public.invalid:8080"),
            lambda d: d["transport"].update(port=8080.0),
            lambda d: d["contract"].update(openapi_sha256="0" * 64),
            lambda d: d["capabilities"].update(attendee_sessions=True),
            lambda d: d["capabilities"].update(attendee_sessions=0),
            lambda d: d.update(product_contracts=[{"name": "Product"}]),
            lambda d: d.update(credential_id="credential:" + "0" * 64),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                document = copy.deepcopy(original)
                mutate(document)
                with self.assertRaises(companion.CompanionError):
                    companion.validate_descriptor(document)

    def test_wrong_or_malformed_role_token_does_not_load(self) -> None:
        self.initialize()
        path = self.root / "transport.token"
        token = path.read_bytes()
        for bad in (
            token.replace(b"attendee", b"operator"),
            token.rstrip(b"\n"),
            token + b"\n",
            b"dreamland-attendee-transport:short\n",
            b"x" * 257,
        ):
            with self.subTest(value_length=len(bad)):
                path.write_bytes(bad)
                self.assertEqual(companion.describe(self.root)["role"], "attendee")
                with self.assertRaises(companion.CompanionError):
                    companion.load_service(self.root, role="attendee")

    def test_export_creates_only_new_private_file_and_preserves_source(self) -> None:
        self.initialize()
        target = self.directory / "handoff"
        target.mkdir(mode=0o700)
        path = target / "attendee.token"
        before = self.snapshot()
        self.assertIsNone(companion.export_token(self.root, path))
        self.assertEqual(
            path.read_bytes(), (self.root / "transport.token").read_bytes()
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.stat().st_nlink, 1)
        with self.assertRaises(companion.CompanionError):
            companion.export_token(self.root, path)
        self.assertEqual(before, self.snapshot())
        self.assertEqual([value.name for value in target.iterdir()], ["attendee.token"])

    def test_export_rejects_unsafe_or_missing_destination_without_creating_it(
        self,
    ) -> None:
        self.initialize()
        private = self.directory / "handoff"
        private.mkdir(mode=0o700)
        alias = self.directory / "handoff-alias"
        alias.symlink_to(private, target_is_directory=True)
        for path in (
            self.directory / "missing" / "token",
            self.root / "export.token",
            alias / "token",
            private / ".." / "token",
        ):
            with self.subTest(path=path), self.assertRaises(companion.CompanionError):
                companion.export_token(self.root, path)
        private.chmod(0o750)
        with self.assertRaises(companion.CompanionError):
            companion.export_token(self.root, private / "token")
        self.assertEqual(list(private.iterdir()), [])
        self.assertFalse((self.directory / "missing").exists())

    def test_export_rejects_existing_symlink_and_does_not_overwrite_target(
        self,
    ) -> None:
        self.initialize()
        private = self.directory / "handoff"
        private.mkdir(mode=0o700)
        target = private / "existing"
        target.write_bytes(b"preserve")
        target.chmod(0o600)
        alias = private / "token"
        alias.symlink_to(target)
        with self.assertRaises(companion.CompanionError):
            companion.export_token(self.root, alias)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(target.read_bytes(), b"preserve")

    def test_response_validation_keeps_health_distinct_from_admission(self) -> None:
        for name in ("identity", "health", "readiness"):
            document = json.loads(
                (CONTRACT / "fixtures" / (name + ".json")).read_bytes()
            )
            self.assertEqual(companion.validate_response(name, document), document)
            document["service_epoch"] = "invalid"
            with self.assertRaises(companion.CompanionError):
                companion.validate_response(name, document)
        readiness = json.loads((CONTRACT / "fixtures/readiness.json").read_bytes())
        for field, value in (
            ("admission_ready", True),
            ("admission_ready", 0),
            ("transport_ready", 1),
            ("transport_ready", False),
            ("reason", "provider_unavailable"),
            ("capabilities", {**companion.CAPABILITIES, "world_forge": True}),
        ):
            document = dict(readiness, **{field: value})
            with self.assertRaises(companion.CompanionError):
                companion.validate_response("readiness", document)

    def test_identity_and_health_responses_reject_semantic_and_shape_drift(
        self,
    ) -> None:
        mutations = (
            (
                "identity",
                lambda d: d["handoff"]["transport"].update(alias="other-attendee-api"),
            ),
            ("identity", lambda d: d.update(unexpected="not-allowed")),
            ("health", lambda d: d.update(status="ready")),
            ("health", lambda d: d.update(role="presenter")),
        )
        for index, (operation, mutate) in enumerate(mutations):
            with self.subTest(operation=operation, mutation=index):
                document = json.loads(
                    (CONTRACT / "fixtures" / f"{operation}.json").read_bytes()
                )
                mutate(document)
                with self.assertRaises(companion.CompanionError):
                    companion.validate_response(operation, document)

    def test_error_response_requires_exact_bounded_envelope_without_epoch(self) -> None:
        value = {"error": {"code": "unauthorized", "message": "Authority is required."}}
        self.assertEqual(companion.validate_response("error", value), value)
        for invalid in (
            {},
            {"error": "unauthorized"},
            {"error": {"code": "unauthorized"}},
            {"error": {"code": "unauthorized", "message": ""}},
            {"error": {"code": "unauthorized", "message": "x" * 161}},
            {"error": {"code": "unauthorized", "message": True}},
            {"error": {"code": "UNAUTHORIZED", "message": "denied"}},
            {"error": {"code": "unauthorized", "message": "denied", "secret": "bad"}},
            dict(value, service_epoch="not-permitted-here"),
        ):
            with (
                self.subTest(value=invalid),
                self.assertRaises(companion.CompanionError),
            ):
                companion.validate_response("error", invalid)

    def test_contract_matches_installed_bytes_and_fixture_expectations(self) -> None:
        self.assertEqual(
            companion.validate_contract(CONTRACT),
            {"validation": "passed", "fixtures": 11},
        )
        artifacts = companion.contract_artifacts()
        manifest = json.loads(artifacts["manifest.json"])
        self.assertEqual(
            hashlib.sha256(artifacts["openapi.json"]).hexdigest(),
            manifest["artifacts"]["openapi_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(artifacts["descriptor.schema.json"]).hexdigest(),
            manifest["artifacts"]["descriptor_schema_sha256"],
        )
        schema = json.loads(artifacts["descriptor.schema.json"])
        jsonschema.Draft202012Validator.check_schema(schema)
        openapi = json.loads(artifacts["openapi.json"])
        for name, value in openapi["components"]["schemas"].items():
            jsonschema.Draft202012Validator.check_schema(value)
        fixture_index = json.loads((CONTRACT / "fixtures/index.json").read_bytes())
        for fixture in fixture_index["fixtures"]:
            if fixture["valid"]:
                document = json.loads(
                    (CONTRACT / "fixtures" / fixture["path"]).read_bytes()
                )
                validator = jsonschema.Draft202012Validator(
                    {
                        "$ref": "#/components/schemas/" + fixture["schema"],
                        "components": openapi["components"],
                    }
                )
                validator.validate(document)

    def test_contract_artifact_or_fixture_changes_fail_validation(self) -> None:
        copied = self.directory / "contract"
        shutil.copytree(CONTRACT, copied)
        for name in (
            "openapi.json",
            "manifest.json",
            "descriptor.schema.json",
            "fixtures/index.json",
            "fixtures/handoff-attendee.json",
        ):
            path = copied / name
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            with self.subTest(name=name), self.assertRaises(companion.CompanionError):
                companion.validate_contract(copied)
            path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
