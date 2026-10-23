from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from dreamland.models import CommandStateUpdate
from dreamland.product.forge_artifacts import ImmutableForgeStore
from dreamland.product.forge_runtime import (
    FORGE_RUNTIME_STATE_KIND,
    ForgeRuntimeCodecContext,
    ForgeStateProfileCodec,
    forge_runtime_origin,
)
from dreamland.session_state import apply_command_state_update
from dreamland.state_profiles import StateProfile


def _profile(hostname: str = "tracking-console") -> StateProfile:
    return StateProfile(
        name="mutable-generator-label",
        level="world",
        created_at="2026-08-26T00:00:00+00:00",
        source_session_id="forge-generator",
        comment="not part of the artifact",
        state={
            "world_state": {
                "name": "comet-observatory",
                "entry_host": hostname,
                "active_host": hostname,
                "purpose": {
                    "theme": "Weathered comet observatory",
                    "description": "Radio clocks disagree during a comet watch.",
                },
                "hosts_by_name": {
                    hostname: {
                        "hostname": hostname,
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


class ProductForgeRuntimeTests(unittest.TestCase):
    def test_codec_hydrates_canonical_forge_envelope_without_a_profile_store(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = ImmutableForgeStore(root / "revisions").save(_profile())
            codec = ForgeStateProfileCodec()

            runtime = codec.decode(
                revision.document,
                checkpoint_namespace="product:stage:forge:first",
            )

            self.assertEqual(runtime.session.cwd, "/home/guest")
            self.assertEqual(runtime.session.host_identity.hostname, "tracking-console")
            self.assertEqual(
                runtime.session.langfuse_trace_id, "product:stage:forge:first"
            )
            self.assertEqual(
                forge_runtime_origin(runtime).revision_id, revision.revision_id
            )
            self.assertEqual(forge_runtime_origin(runtime).digest, revision.digest)
            self.assertIsInstance(runtime.codec_context, ForgeRuntimeCodecContext)
            self.assertFalse((root / "state_profiles").exists())
            self.assertFalse((root / "default.json").exists())
            self.assertFalse((root / "session_index.json").exists())

    def test_codec_preserves_origin_separately_across_detached_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            revision = ImmutableForgeStore(Path(directory) / "revisions").save(
                _profile()
            )
            codec = ForgeStateProfileCodec()
            first = codec.decode(
                revision.document,
                checkpoint_namespace="product:stage:forge:first",
            )
            apply_command_state_update(
                first.session,
                CommandStateUpdate(cwd="/srv/observatory"),
            )

            detached = codec.encode(first)

            self.assertEqual(detached["kind"], FORGE_RUNTIME_STATE_KIND)
            origin = detached["origin"]
            self.assertIsInstance(origin, dict)
            assert isinstance(origin, dict)
            self.assertEqual(origin["revision_id"], revision.revision_id)
            self.assertEqual(origin["digest"], revision.digest)
            profile_document = detached["profile"]
            self.assertIsInstance(profile_document, dict)
            assert isinstance(profile_document, dict)
            self.assertEqual(
                set(profile_document),
                {"kind", "schema_version", "name", "level", "created_at", "state"},
            )
            self.assertEqual(profile_document["level"], "exact")
            self.assertEqual(profile_document["schema_version"], 2)
            self.assertEqual(
                profile_document["state"]["world_state"]["hosts_by_name"][
                    "tracking-console"
                ]["login_banner"],
                "Comet observatory tracking console\n",
            )
            self.assertNotIn("source_session_id", profile_document)
            self.assertNotIn("comment", profile_document)

            lifecycle_bytes = json.dumps(
                revision.document,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            stored_bytes = revision.path.read_bytes()
            self.assertEqual(
                revision.digest,
                f"sha256:{hashlib.sha256(stored_bytes).hexdigest()}",
            )
            self.assertNotEqual(
                hashlib.sha256(lifecycle_bytes).hexdigest(),
                revision.digest.removeprefix("sha256:"),
            )

            second = codec.decode(
                detached,
                checkpoint_namespace="product:stage:forge:second",
            )
            self.assertEqual(second.session.cwd, "/srv/observatory")
            self.assertEqual(forge_runtime_origin(second), forge_runtime_origin(first))
            apply_command_state_update(
                second.session,
                CommandStateUpdate(cwd="/var/lib/telescope"),
            )
            encoded_again = codec.encode(second)
            self.assertEqual(encoded_again["origin"], detached["origin"])

            with self.assertRaises(ValueError):
                ForgeStateProfileCodec().decode(
                    detached,
                    checkpoint_namespace="product:stage:forge:unverified",
                )

    def test_one_codec_accepts_multiple_verified_forge_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ImmutableForgeStore(Path(directory) / "revisions")
            first_revision = store.save(_profile())
            second_revision = store.save(_profile("spectrometer-console"))
            codec = ForgeStateProfileCodec()

            first = codec.decode(
                first_revision.document,
                checkpoint_namespace="product:stage:forge:a",
            )
            second = codec.decode(
                second_revision.document,
                checkpoint_namespace="product:stage:forge:b",
            )

            self.assertEqual(forge_runtime_origin(first).digest, first_revision.digest)
            self.assertEqual(
                forge_runtime_origin(second).digest, second_revision.digest
            )
            self.assertNotEqual(first.profile_name, second.profile_name)

    def test_codec_rejects_invalid_initial_and_detached_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            revision = ImmutableForgeStore(Path(directory) / "revisions").save(
                _profile()
            )
            codec = ForgeStateProfileCodec()
            malformed_initial = deepcopy(revision.document)
            malformed_initial["schema_revision"] = "2"
            invalid_initial = deepcopy(revision.document)
            profile = invalid_initial["profile"]
            assert isinstance(profile, dict)
            state = profile["state"]
            assert isinstance(state, dict)
            world = state["world_state"]
            assert isinstance(world, dict)
            world["entry_host"] = "missing"
            for document in (malformed_initial, invalid_initial, {"kind": "other"}):
                with self.subTest(document=document):
                    with self.assertRaises(ValueError):
                        codec.decode(document, checkpoint_namespace="product:bad")

            runtime = codec.decode(
                revision.document,
                checkpoint_namespace="product:stage:forge:first",
            )
            detached = dict(codec.encode(runtime))
            cases: list[dict[str, object]] = []
            missing_banner = deepcopy(detached)
            missing_banner["profile"]["state"]["world_state"]["hosts_by_name"][
                "tracking-console"
            ].pop("login_banner")
            cases.append(missing_banner)
            old_profile_schema = deepcopy(detached)
            old_profile_schema["profile"]["schema_version"] = 1
            cases.append(old_profile_schema)
            changed_origin = deepcopy(detached)
            origin = changed_origin["origin"]
            assert isinstance(origin, dict)
            origin["digest"] = f"sha256:{'0' * 64}"
            cases.append(changed_origin)
            changed_name = deepcopy(detached)
            profile_document = changed_name["profile"]
            assert isinstance(profile_document, dict)
            profile_document["name"] = "different"
            cases.append(changed_name)
            additive = deepcopy(detached)
            additive["unexpected"] = True
            cases.append(additive)
            for document in cases:
                with self.subTest(document=document):
                    with self.assertRaises(ValueError):
                        codec.decode(document, checkpoint_namespace="product:bad")


if __name__ == "__main__":
    unittest.main()
