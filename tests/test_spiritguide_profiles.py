from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy

from dreamland.models import CommandStateUpdate, HostIdentity, SessionState
from dreamland.spiritguide import SpiritguideContext, SpiritguideResult
from dreamland.spiritguide_profiles import (
    create_spiritguide_profile,
    update_spiritguide_profile,
)
from dreamland.state_profiles import StateProfileError, StateProfileStore


class SpiritguideProfileTests(unittest.TestCase):
    def test_valid_create_dry_run_validates_candidate_without_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateProfileStore(directory)
            spiritguide = FakeSpiritguide(_new_world())
            result = create_spiritguide_profile(
                store=store,
                spiritguide=spiritguide,
                name="ship",
                instruction="Create a ship bridge console",
                dry_run=True,
            )
            self.assertTrue(result.structured_response_accepted)
            self.assertFalse(result.saved)
            self.assertFalse(result.applied)
            self.assertIsNone(result.path)
            self.assertFalse(store.path_for("ship").exists())
            self.assertEqual(
                spiritguide.original_session.world_state["entry_host"],
                "operator-defined",
            )
            self.assertNotIn(
                "bridge", spiritguide.original_session.world_state["hosts_by_name"]
            )

    def test_invalid_create_dry_run_rejects_banner_and_unfinished_draft(self):
        missing_banner = _new_world()
        missing_banner["hosts_by_name"]["bridge"].pop("login_banner")
        for world, error in (
            (missing_banner, "login_banner"),
            (
                {"hosts_by_name": {"operator-defined": {"login_banner": ""}}},
                "draft entry host",
            ),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                store = StateProfileStore(directory)
                with self.assertRaisesRegex(StateProfileError, error):
                    create_spiritguide_profile(
                        store=store,
                        spiritguide=FakeSpiritguide(world),
                        name="invalid",
                        instruction="Create a world",
                        dry_run=True,
                    )
                self.assertFalse(store.path_for("invalid").exists())

    def test_update_dry_run_validates_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateProfileStore(directory)
            create_spiritguide_profile(
                store=store,
                spiritguide=FakeSpiritguide(_new_world()),
                name="ship",
                instruction="Create a ship bridge console",
            )
            expected = store.path_for("ship").read_bytes()
            for banner, valid in (("BridgeOS 5\n", True), ("\x00", False)):
                spiritguide = FakeSpiritguide(
                    {"hosts_by_name": {"bridge": {"login_banner": banner}}}
                )
                with self.subTest(valid=valid):
                    if valid:
                        result = update_spiritguide_profile(
                            store=store,
                            spiritguide=spiritguide,
                            name="ship",
                            instruction="Repair the banner",
                            dry_run=True,
                        )
                        self.assertFalse(result.saved)
                        self.assertFalse(result.applied)
                    else:
                        with self.assertRaisesRegex(StateProfileError, "login_banner"):
                            update_spiritguide_profile(
                                store=store,
                                spiritguide=spiritguide,
                                name="ship",
                                instruction="Repair the banner",
                                dry_run=True,
                            )
                    self.assertEqual(store.path_for("ship").read_bytes(), expected)
                    self.assertEqual(
                        spiritguide.original_session.world_state["hosts_by_name"][
                            "bridge"
                        ]["login_banner"],
                        "BridgeOS 4\nAuthorized crew access only.\n",
                    )

    def test_rejected_create_response_does_not_validate_or_save_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateProfileStore(directory)
            spiritguide = FakeSpiritguide({}, accepted=False)
            result = create_spiritguide_profile(
                store=store,
                spiritguide=spiritguide,
                name="rejected",
                instruction="Create a world",
                dry_run=True,
            )
            self.assertFalse(result.structured_response_accepted)
            self.assertFalse(result.saved)
            self.assertFalse(result.applied)
            self.assertFalse(store.path_for("rejected").exists())

    def test_create_authors_banner_and_saves_clean_runnable_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateProfileStore(directory)
            spiritguide = FakeSpiritguide(_new_world())
            result = create_spiritguide_profile(
                store=store,
                spiritguide=spiritguide,
                name="ship",
                instruction="Create a ship bridge console",
            )
            saved = store.load("ship")
            world = saved.state["world_state"]
            self.assertTrue(result.saved)
            self.assertEqual(world["active_host"], "bridge")
            self.assertNotIn("operator-defined", world["hosts_by_name"])
            self.assertEqual(
                world["hosts_by_name"]["bridge"]["login_banner"],
                "BridgeOS 4\nAuthorized crew access only.\n",
            )
            scaffold = spiritguide.contexts[0]
            self.assertIsInstance(scaffold.session, SessionState)
            self.assertNotIn(
                "login_banner",
                scaffold.session.world_state["hosts_by_name"]["operator-defined"],
            )

    def test_missing_or_invalid_generated_banner_cannot_be_saved(self):
        for banner in (None, "Bad\x1b[2J", "x" * 16_385):
            with self.subTest(banner=repr(banner)[:30]):
                world = _new_world()
                if banner is None:
                    world["hosts_by_name"]["bridge"].pop("login_banner")
                else:
                    world["hosts_by_name"]["bridge"]["login_banner"] = banner
                with tempfile.TemporaryDirectory() as directory:
                    store = StateProfileStore(directory)
                    with self.assertRaisesRegex(StateProfileError, "login_banner"):
                        create_spiritguide_profile(
                            store=store,
                            spiritguide=FakeSpiritguide(world),
                            name="invalid",
                            instruction="Create a ship bridge console",
                        )
                    self.assertFalse(store.path_for("invalid").exists())

    def test_scaffold_cannot_be_saved_without_real_entry_host(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateProfileStore(directory)
            with self.assertRaisesRegex(StateProfileError, "draft entry host"):
                create_spiritguide_profile(
                    store=store,
                    spiritguide=FakeSpiritguide(
                        {"hosts_by_name": {"operator-defined": {"login_banner": ""}}}
                    ),
                    name="invalid",
                    instruction="Create a world",
                )
            self.assertFalse(store.path_for("invalid").exists())

    def test_update_can_edit_banner_but_invalid_update_preserves_saved_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateProfileStore(directory)
            create_spiritguide_profile(
                store=store,
                spiritguide=FakeSpiritguide(_new_world()),
                name="ship",
                instruction="Create a ship bridge console",
            )
            update_spiritguide_profile(
                store=store,
                spiritguide=FakeSpiritguide(
                    {"hosts_by_name": {"bridge": {"login_banner": "BridgeOS 5\n"}}}
                ),
                name="ship",
                instruction="Upgrade the bridge OS",
            )
            expected = store.path_for("ship").read_bytes()
            with self.assertRaisesRegex(StateProfileError, "login_banner"):
                update_spiritguide_profile(
                    store=store,
                    spiritguide=FakeSpiritguide(
                        {"hosts_by_name": {"bridge": {"login_banner": "\x00"}}}
                    ),
                    name="ship",
                    instruction="Repair the banner",
                )
            self.assertEqual(store.path_for("ship").read_bytes(), expected)


def _new_world() -> dict[str, object]:
    return {
        "name": "ship-bridge",
        "entry_host": "bridge",
        "active_host": "bridge",
        "hosts_by_name": {
            "bridge": {
                "hostname": "bridge",
                "login_banner": "BridgeOS 4\nAuthorized crew access only.\n",
                "shell": {
                    "default": {
                        "name": "bridge-shell",
                        "command": "/bin/bridge-shell",
                        "argv": ["bridge-shell"],
                    }
                },
            }
        },
    }


class FakeSpiritguide:
    def __init__(self, world: dict[str, object], *, accepted: bool = True):
        self.world = world
        self.accepted = accepted
        self.contexts: list[SpiritguideContext] = []
        self.original_session: SessionState

    def respond(self, context: SpiritguideContext) -> SpiritguideResult:
        self.original_session = context.session
        self.contexts.append(deepcopy(context))
        return SpiritguideResult(
            message="World updated.",
            state_update=CommandStateUpdate(
                host_identity=HostIdentity(hostname="bridge"),
                world_state=deepcopy(self.world),
            ),
            metadata={"structured_response_accepted": self.accepted},
        )


if __name__ == "__main__":
    unittest.main()
