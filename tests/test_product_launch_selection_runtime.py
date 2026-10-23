from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from dreamland.models import CommandStateUpdate
from dreamland.product.authored_artifacts import (
    ImmutableAuthoredStore,
    authored_revision_digest,
)
from dreamland.product.forge_artifacts import ImmutableForgeStore
from dreamland.product.forge_runtime import FORGE_RUNTIME_STATE_KIND
from dreamland.product.launch_selection_runtime import (
    AUTHORED_RUNTIME_STATE_KIND,
    FORGE_REVISION_KIND,
    LaunchSelectionRuntimeCodecContext,
    LaunchSelectionStateProfileCodec,
)
from dreamland.product.runtime_executor import ProductRuntimeState
from dreamland.session_state import apply_command_state_update
from dreamland.state_profiles import StateProfile


def _profile(hostname: str) -> StateProfile:
    return StateProfile(
        name=f"configured-{hostname}",
        level="world",
        created_at="2026-08-27T00:00:00+00:00",
        source_session_id="source-session-not-retained",
        comment="source metadata is outside immutable launch identity",
        state={
            "world_state": {
                "name": f"world-{hostname}",
                "entry_host": hostname,
                "active_host": hostname,
                "purpose": {"theme": "Source-neutral codec test"},
                "hosts_by_name": {
                    hostname: {
                        "login_banner": "Test console\n",
                        "hostname": hostname,
                        "purpose": {"theme": "Run the selected world"},
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


class ProductLaunchSelectionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        authored_profile = _profile("authored-console")
        self.authored = ImmutableAuthoredStore(self.root / "authored-revisions").save(
            authored_profile,
            expected_digest=authored_revision_digest(authored_profile),
        )
        self.forged = ImmutableForgeStore(self.root / "forge-revisions").save(
            _profile("forge-console")
        )

    def test_one_codec_round_trips_mutated_authored_and_forge_state(self) -> None:
        codec = LaunchSelectionStateProfileCodec()
        authored = codec.decode(
            self.authored.allocate_document(),
            checkpoint_namespace="product:selection:authored:first",
        )
        forged = codec.decode(
            self.forged.document,
            checkpoint_namespace="product:selection:forge:first",
        )

        self.assertEqual(authored.session.host_identity.hostname, "authored-console")
        self.assertEqual(forged.session.host_identity.hostname, "forge-console")
        self.assertIsInstance(
            authored.codec_context, LaunchSelectionRuntimeCodecContext
        )
        self.assertIsInstance(forged.codec_context, LaunchSelectionRuntimeCodecContext)
        self.assertEqual(
            authored.codec_context.source_kind,
            "dreamland.authored_world_revision",
        )
        self.assertEqual(forged.codec_context.source_kind, FORGE_REVISION_KIND)

        apply_command_state_update(
            authored.session,
            CommandStateUpdate(cwd="/srv/authored"),
        )
        apply_command_state_update(
            forged.session,
            CommandStateUpdate(cwd="/srv/forged"),
        )
        authored_document = dict(codec.encode(authored))
        forged_document = dict(codec.encode(forged))

        self.assertEqual(authored_document["kind"], AUTHORED_RUNTIME_STATE_KIND)
        self.assertEqual(forged_document["kind"], FORGE_RUNTIME_STATE_KIND)
        authored_origin = authored_document["origin"]
        self.assertEqual(
            authored_origin,
            {
                "revision_id": self.authored.revision_id,
                "digest": self.authored.digest,
            },
        )
        forged_origin = forged_document["origin"]
        self.assertEqual(
            forged_origin,
            {
                "revision_id": self.forged.revision_id,
                "digest": self.forged.digest,
            },
        )

        authored_again = codec.decode(
            authored_document,
            checkpoint_namespace="product:selection:authored:second",
        )
        forged_again = codec.decode(
            forged_document,
            checkpoint_namespace="product:selection:forge:second",
        )
        self.assertEqual(authored_again.session.cwd, "/srv/authored")
        self.assertEqual(forged_again.session.cwd, "/srv/forged")
        self.assertEqual(
            authored_again.session.langfuse_trace_id,
            "product:selection:authored:second",
        )
        self.assertEqual(
            forged_again.session.langfuse_trace_id,
            "product:selection:forge:second",
        )
        self.assertFalse((self.root / "state_profiles").exists())
        self.assertFalse((self.root / "session_index.json").exists())

    def test_runtime_documents_require_prior_verified_origin(self) -> None:
        codec = LaunchSelectionStateProfileCodec()
        authored = codec.decode(
            self.authored.allocate_document(),
            checkpoint_namespace="product:selection:authored:verified",
        )
        forged = codec.decode(
            self.forged.document,
            checkpoint_namespace="product:selection:forge:verified",
        )
        authored_document = codec.encode(authored)
        forged_document = codec.encode(forged)

        with self.assertRaisesRegex(ValueError, "origin is not verified"):
            LaunchSelectionStateProfileCodec().decode(
                authored_document,
                checkpoint_namespace="product:selection:authored:unverified",
            )
        with self.assertRaisesRegex(ValueError, "origin is not verified"):
            LaunchSelectionStateProfileCodec().decode(
                forged_document,
                checkpoint_namespace="product:selection:forge:unverified",
            )

    def test_unknown_and_cross_source_documents_and_contexts_are_rejected(self) -> None:
        codec = LaunchSelectionStateProfileCodec()
        authored = codec.decode(
            self.authored.allocate_document(),
            checkpoint_namespace="product:selection:authored:cross",
        )
        forged = codec.decode(
            self.forged.document,
            checkpoint_namespace="product:selection:forge:cross",
        )
        authored_document = dict(codec.encode(authored))
        forged_document = dict(codec.encode(forged))

        unknown = deepcopy(authored_document)
        unknown["kind"] = "dreamland.future_world_runtime_state"
        forged_as_authored = deepcopy(forged_document)
        forged_as_authored["kind"] = AUTHORED_RUNTIME_STATE_KIND
        authored_as_forge = deepcopy(authored_document)
        authored_as_forge["kind"] = FORGE_RUNTIME_STATE_KIND
        for document in (unknown, forged_as_authored, authored_as_forge):
            with self.subTest(kind=document["kind"]):
                with self.assertRaises(ValueError):
                    codec.decode(
                        document,
                        checkpoint_namespace="product:selection:cross:rejected",
                    )

        authored_context = authored.codec_context
        forged_context = forged.codec_context
        assert isinstance(authored_context, LaunchSelectionRuntimeCodecContext)
        assert isinstance(forged_context, LaunchSelectionRuntimeCodecContext)
        invalid_states = (
            ProductRuntimeState(
                profile_name=authored.profile_name,
                session=authored.session,
                codec_context=LaunchSelectionRuntimeCodecContext(
                    "dreamland.future_world_revision",
                    authored_context.delegate_context,
                    authored_context.authored_origin,
                ),
            ),
            ProductRuntimeState(
                profile_name=authored.profile_name,
                session=authored.session,
                codec_context=LaunchSelectionRuntimeCodecContext(
                    FORGE_REVISION_KIND,
                    authored_context.delegate_context,
                    authored_context.authored_origin,
                ),
            ),
            ProductRuntimeState(
                profile_name=forged.profile_name,
                session=forged.session,
                codec_context=LaunchSelectionRuntimeCodecContext(
                    "dreamland.authored_world_revision",
                    forged_context.delegate_context,
                    None,
                ),
            ),
        )
        for state in invalid_states:
            with self.subTest(source=state.codec_context.source_kind):
                with self.assertRaises(ValueError):
                    codec.encode(state)

    def test_tampered_authored_runtime_identity_and_profile_are_rejected(self) -> None:
        codec = LaunchSelectionStateProfileCodec()
        runtime = codec.decode(
            self.authored.allocate_document(),
            checkpoint_namespace="product:selection:authored:tamper",
        )
        document = dict(codec.encode(runtime))
        cases: list[dict[str, object]] = []

        wrong_origin = deepcopy(document)
        origin = wrong_origin["origin"]
        assert isinstance(origin, dict)
        origin["digest"] = "sha256:" + "0" * 64
        cases.append(wrong_origin)

        wrong_name = deepcopy(document)
        profile = wrong_name["profile"]
        assert isinstance(profile, dict)
        profile["name"] = "configured-profile-name"
        cases.append(wrong_name)

        additive = deepcopy(document)
        additive["source_path"] = "/private/profile.json"
        cases.append(additive)

        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    codec.decode(
                        candidate,
                        checkpoint_namespace="product:selection:authored:rejected",
                    )


if __name__ == "__main__":
    unittest.main()
