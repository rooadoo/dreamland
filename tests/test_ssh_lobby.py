import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dreamland.backend import AgentBackend
from dreamland.config import ConsoleSettings, LobbySettings
from dreamland.console import ConsoleContext, DreamlandConsole
from dreamland.lobby_catalog import LobbyCatalog
from dreamland.lobby_content import LANGFUSE_DISCLOSURE, LobbyContent
from dreamland.lobby_identity import PublicSimulationRegistry
from dreamland.profile import server_profile
from dreamland.runtime_logging import LogRecordBuffer
from dreamland.ssh_lobby import AGREEMENT_PROMPT, WELCOME_PROMPT, WORLD_PROMPT, SSHLobby
from dreamland.state_profiles import (
    StateProfile,
    StateProfileRuntime,
    StateProfileStore,
    create_state_profile,
)
from tests.test_lobby_input import TerminalChannel
from tests.test_ssh_sessions import RecordingAgent


class RecordingBackend(AgentBackend):
    def __init__(self, store):
        super().__init__(
            server_profile(),
            state_profile_runtime=StateProfileRuntime(store=store),
        )
        self.sessions = []

    def create_session(self, *args, **kwargs):
        session = super().create_session(*args, **kwargs)
        self.sessions.append(session)
        return session


class SSHLobbyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = StateProfileStore(self.root / "profiles")
        for name in ("alpha", "beta"):
            self.store.save(
                StateProfile(
                    name,
                    "world",
                    {
                        "world_state": {
                            "entry_host": "ubuntu",
                            "active_host": "ubuntu",
                            "hosts_by_name": {
                                "ubuntu": {
                                    "hostname": "ubuntu",
                                    "login_banner": "Authored fixture host.\n",
                                }
                            },
                        }
                    },
                    "2026-09-11T00:00:00Z",
                )
            )
        self.catalog = LobbyCatalog(self.store, self.root / "catalog.json")
        self.catalog.add(
            "alpha",
            title="First World",
            description="Explore first.",
            entry_username="operator",
        )
        self.catalog.add(
            "beta",
            title="Second World",
            description="Explore second.",
            entry_username="reader",
        )
        self.identities = PublicSimulationRegistry(self.root / "visits.sqlite3")
        self.console = DreamlandConsole(
            ConsoleSettings(),
            LogRecordBuffer(10),
            lambda: None,
            state_profiles=self.store,
            lobby_catalog=self.catalog,
            public_simulations=self.identities,
        )
        self.backend = RecordingBackend(self.store)
        self.connection_username = "visitor"
        self.parent_connection_id = "test-parent-connection"
        self.channel = TerminalChannel()
        self.stop = threading.Event()
        self.lobby = SSHLobby(
            self.catalog,
            self.identities,
            LobbyContent(),
            LobbySettings(intro_animation="off"),
        )
        self.runner = threading.Thread(
            target=lambda: self.lobby.run(
                self.channel,
                self.connection_username,
                self.backend,
                None,
                console=self.console,
                stop_event=self.stop,
                width=100,
                height=100,
                parent_connection_id=self.parent_connection_id,
            )
        )
        self.addCleanup(self.finish)

    def finish(self):
        self.stop.set()
        if self.runner.ident is not None:
            self.runner.join(3)

    def begin(self):
        self.runner.start()
        self.channel.wait_for("Press any key to continue")
        self.channel.incoming.put(b" ")
        self.channel.wait_for("Type i agree")

    def agree(self):
        self.begin()
        self.channel.incoming.put(b"I AGREE\r\n1\nwhoami\n")
        self.channel.wait_for("Enter a number")
        self.assertEqual(self.backend.sessions, [])

    def select(self, number=b"1\n"):
        offset = len(self.channel.output)
        self.channel.incoming.put(number)
        self.channel.wait_for("@ubuntu:", after=offset)
        return self.backend.sessions[-1]

    def assert_entry_identity(self, session, expected):
        self.assertEqual(session.username, expected)
        self.assertEqual(session.login_username, expected)
        self.assertEqual(session.cwd, f"/home/{expected}")
        self.assertEqual(len(session.shell_stack), 1)
        frame = session.shell_stack[0]
        self.assertEqual(frame.username, expected)
        self.assertEqual(frame.cwd, session.cwd)
        for environment in (session.shell_environment, frame.shell_environment):
            self.assertEqual(environment.variables["USER"], expected)
            self.assertEqual(environment.variables["LOGNAME"], expected)
            self.assertEqual(environment.variables["HOME"], f"/home/{expected}")
            self.assertEqual(environment.variables["PWD"], f"/home/{expected}")

    def test_distinct_ssh_logins_start_the_same_simulation_account(self):
        for login in ("alice.smith@example", "different+attendee"):
            with self.subTest(login=login):
                fixture = SSHLobbyTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                fixture.connection_username = login
                fixture.agree()
                session = fixture.select()
                self.assert_entry_identity(session, "operator")
                self.assertNotIn(login, fixture.channel.output)
                fixture.finish()

    def test_switching_and_reentering_worlds_restores_configured_accounts(self):
        self.agree()
        visited_ids = set()
        for number, expected in (
            (b"1\n", "operator"),
            (b"2\n", "reader"),
            (b"1\n", "operator"),
        ):
            session = self.select(number)
            self.assert_entry_identity(session, expected)
            self.assertEqual(session.history, [])
            self.assertNotIn(session.langfuse_trace_id, visited_ids)
            visited_ids.add(session.langfuse_trace_id)
            offset = len(self.channel.output)
            self.channel.incoming.put(b"exit\n")
            self.channel.wait_for("Enter a number", after=offset)

    def test_lifecycle_logs_correlate_real_login_and_simulated_visit_identity(self):
        self.connection_username = "real.user\r\n\x1b[2J@example"
        with self.assertLogs("dreamland.lifecycle", level="INFO") as logs:
            self.agree()
            session = self.select()
            offset = len(self.channel.output)
            self.channel.incoming.put(b"exit\n")
            self.channel.wait_for("Enter a number", after=offset)
            self.channel.incoming.put(b"q\n")
            self.runner.join(3)
            self.assertFalse(self.runner.is_alive())
        messages = [record.getMessage() for record in logs.records]
        opened = next(message for message in messages if "SSH lobby opened " in message)
        self.assertIn(f"parent={self.parent_connection_id}", opened)
        self.assertIn(f"ssh_username={self.connection_username!r}", opened)
        self.assertNotIn("\r", opened)
        self.assertNotIn("\n", opened)
        self.assertNotIn("\x1b", opened)
        public_id = session.runtime_provenance.public_id
        self.assertEqual(
            self.identities.get(public_id).parent_connection_id,
            self.parent_connection_id,
        )
        for event in ("created", "ended"):
            message = next(
                message
                for message in messages
                if f"SSH lobby visit {event} " in message
            )
            for field in (
                f"parent={self.parent_connection_id}",
                f"public_id={public_id}",
                f"session={session.langfuse_trace_id}",
                "profile=alpha",
                "entry_username=operator",
            ):
                self.assertIn(field, message)
            self.assertNotIn(self.connection_username, message)

    def test_entry_identity_expands_templates_and_keeps_ssh_name_out_of_model_state(
        self,
    ):
        self.connection_username = "real.ssh+visitor@example"
        self.store.save(
            StateProfile(
                "alpha",
                "world",
                {
                    "world_state": {
                        "entry_host": "ubuntu",
                        "active_host": "ubuntu",
                        "hosts_by_name": {
                            "ubuntu": {
                                "hostname": "ubuntu",
                                "login_banner": "Authored entry template host.\n",
                                "facts_by_id": {
                                    "entry-account": {
                                        "username": "{username}",
                                        "home": "/home/{username}",
                                    },
                                },
                            }
                        },
                    }
                },
                "2026-09-11T00:00:00Z",
            )
        )
        agent = RecordingAgent()
        self.backend.agent = agent
        self.agree()
        session = self.select()
        self.assert_entry_identity(session, "operator")
        facts = session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
        self.assertEqual(
            facts["entry-account"],
            {
                "username": "operator",
                "home": "/home/operator",
            },
        )
        offset = len(self.channel.output)
        self.channel.incoming.put(b"identity-probe\n")
        self.channel.wait_for("recorded", after=offset)
        self.channel.wait_for("operator@", after=offset)
        context = agent.contexts[-1]
        self.assertEqual(context.username, "operator")
        self.assertEqual(context.login_username, "operator")
        self.assertNotIn(self.connection_username, repr(context))
        for level in ("world", "system", "exact"):
            saved = create_state_profile(session, name="saved-entry", level=level)
            self.assertNotIn(self.connection_username, json.dumps(saved.to_document()))
        self.assertEqual(
            self.store.load("alpha").state["world_state"]["hosts_by_name"]["ubuntu"][
                "facts_by_id"
            ]["entry-account"]["username"],
            "{username}",
        )

    def test_direct_backend_session_keeps_its_supplied_username(self):
        session = self.backend.create_session(
            "direct-user", state_profile=self.store.load("alpha")
        )
        self.addCleanup(self.backend.release_session, session)
        self.assert_entry_identity(session, "direct-user")

    def test_complete_fresh_visits_console_resolution_and_no_agreement_replay(self):
        self.agree()
        first = self.select()
        first_id = first.runtime_provenance.public_id
        self.assertIn(
            first_id, self.console.execute("sessions --live", allow_exit=False).output
        )
        self.assertIs(
            self.console.session_registry.get(first.langfuse_trace_id).session, first
        )
        self.assertIsNone(self.console.session_registry.get(first_id))
        self.assertIn(
            "Active session set",
            self.console.execute(
                f"sessions --set {first_id.lower()}", allow_exit=False
            ).output,
        )
        offset = len(self.channel.output)
        self.channel.incoming.put(b"exit\r\n1\nwhoami\n\x1b[")
        self.channel.wait_for("Enter a number", after=offset)
        self.assertEqual(len(self.backend.sessions), 1)
        second = self.select(b"A1\n")
        second_id = second.runtime_provenance.public_id
        self.assertNotEqual(first_id, second_id)
        self.assertNotEqual(first.langfuse_trace_id, second.langfuse_trace_id)
        self.assertEqual(second.history, [])
        self.assertEqual(self.channel.output.count("Type i agree"), 1)
        self.assertEqual(self.identities.get(first_id).status, "ended")
        with patch.object(self.console.spiritguide, "respond") as respond:
            result = self.console.execute(
                f"spiritguide --session {first_id} add a host", allow_exit=False
            )
            self.assertIn("ended", result.output)
            respond.assert_not_called()
        offset = len(self.channel.output)
        result = self.console.execute(f"sessions --close {second_id}", allow_exit=False)
        self.assertIn("Closed", result.output)
        self.channel.wait_for("Enter a number", after=offset)
        self.assertEqual(self.identities.get(second_id).end_reason, "operator_close")
        self.assertNotIn("Type i agree", self.channel.output[offset:])
        self.channel.incoming.put(b"q\n")
        self.runner.join(3)
        self.assertFalse(self.runner.is_alive())
        self.assertEqual(self.channel.exit_status, 0)
        self.assertEqual(self.console.session_registry.list_sessions(), ())

    def test_invalid_payloads_never_create_session_or_operator_authority(self):
        self.begin()
        for payload in (
            b"yes\n",
            b"i agree;1\n",
            b"!dreamland shutdown\n",
            b"{{i agree}}\n",
            b"i\x00 agree\n",
            b"\x1b[31mi agree\n",
        ):
            offset = len(self.channel.output)
            self.channel.incoming.put(payload)
            self.channel.wait_for("Type i agree", after=offset)
            self.assertFalse(self.stop.is_set())
            self.assertEqual(self.backend.sessions, [])
        self.channel.incoming.put(b"i agree\n")
        self.channel.wait_for("Enter a number")
        for payload in (
            b"../alpha\n",
            b"alpha\n",
            b"!dreamland shutdown\n",
            b"1;whoami\n",
            b"999\n",
        ):
            offset = len(self.channel.output)
            self.channel.incoming.put(payload)
            self.channel.wait_for("Enter a number", after=offset)
            self.assertEqual(self.backend.sessions, [])
        self.channel.incoming.put(b"q\n")
        self.runner.join(3)

    def test_stale_picker_does_not_launch_reordered_world(self):
        self.agree()
        self.catalog.order(["beta", "alpha"])
        offset = len(self.channel.output)
        self.channel.incoming.put(b"1\n")
        self.channel.wait_for("Enter a number", after=offset)
        self.assertEqual(self.backend.sessions, [])
        session = self.select()
        self.assertEqual(
            self.identities.get(session.runtime_provenance.public_id).profile_name,
            "beta",
        )

    def test_failed_start_does_not_emit_success_or_retain_live_identity(self):
        self.agree()
        offset = len(self.channel.output)
        with (
            self.assertLogs("dreamland.lifecycle", level="INFO") as logs,
            patch.object(
                self.backend,
                "create_session",
                side_effect=ValueError("SECRET /private/path"),
            ) as create,
        ):
            self.channel.incoming.put(b"1\n")
            self.channel.wait_for("Enter a number", after=offset)
        self.assertEqual(create.call_args.args[0], "operator")
        messages = [record.getMessage() for record in logs.records]
        entering = next(message for message in messages if "visit entering " in message)
        ended = next(
            message for message in messages if "SSH lobby visit ended " in message
        )
        for field in (
            f"parent={self.parent_connection_id}",
            "profile=alpha",
            "entry_username=operator",
        ):
            self.assertIn(field, entering)
            self.assertIn(field, ended)
        self.assertIn("session=None", ended)
        self.assertIn("reason=failed_start", ended)
        self.assertFalse(any("visit created " in message for message in messages))
        self.assertNotIn(" Started]", self.channel.output)
        self.assertNotIn("SECRET", self.channel.output)
        self.assertNotIn("/private", self.channel.output)
        self.assertEqual(self.console.session_registry.list_sessions(), ())

    def test_public_unknown_prefix_expired_never_falls_back_to_only_live_session(self):
        self.agree()
        session = self.select()
        public_id = session.runtime_provenance.public_id
        context = ConsoleContext(active_session_id=session.langfuse_trace_id)
        with patch.object(self.console.spiritguide, "respond") as respond:
            for reference in (
                "DL",
                public_id[:-1],
                "DL-2222-2222-2222-2222",
                "DL-../../alpha",
            ):
                for command in (
                    f"spiritguide --session {reference} mutate",
                    f"state-profile save x --session {reference}",
                    f"trace {reference}",
                ):
                    result = self.console.execute(
                        command, context=context, allow_exit=False
                    )
                    self.assertIn("error:", result.output)
            respond.assert_not_called()
        self.assertIsNotNone(
            self.console.session_registry.get(session.langfuse_trace_id)
        )

    def test_closing_during_spiritguide_discards_late_patch(self):
        from dreamland.models import CommandStateUpdate
        from dreamland.spiritguide import SpiritguideResult

        self.agree()
        first = self.select()
        public_id = first.runtime_provenance.public_id
        entered = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)

        def delayed(context):
            entered.set()
            finish.wait(5)
            return SpiritguideResult(
                message="late guidance",
                state_update=CommandStateUpdate(
                    world_state={"forbidden_late_patch": True}
                ),
            )

        replies = []
        with patch.object(self.console.spiritguide, "respond", side_effect=delayed):
            worker = threading.Thread(
                target=lambda: replies.append(
                    self.console.execute(
                        f"spiritguide --session {public_id} change world",
                        allow_exit=False,
                    )
                )
            )
            worker.start()
            self.assertTrue(entered.wait(2))
            offset = len(self.channel.output)
            self.console.execute(f"sessions --close {public_id}", allow_exit=False)
            self.channel.wait_for("Enter a number", after=offset)
            second = self.select(b"2\n")
            finish.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertIn("ended", replies[0].output)
        self.assertNotIn("forbidden_late_patch", first.world_state)
        self.assertNotIn("forbidden_late_patch", second.world_state)
        self.assertNotEqual(first.runtime_provenance, second.runtime_provenance)

    def test_hot_curation_console_and_restart(self):
        commands = (
            'lobby worlds update alpha --title "After Hours"',
            "lobby worlds order beta alpha",
            "lobby worlds remove beta",
            'lobby worlds add beta --title "Second World" --description "Another world" --entry-username reader',
            "lobby worlds reload",
        )
        for command in commands:
            result = self.console.execute(command, allow_exit=False)
            self.assertNotIn("error:", result.output)
        restored = LobbyCatalog(self.store, self.catalog.path)
        self.assertEqual(restored.snapshot().entries, self.catalog.snapshot().entries)
        self.assertEqual(restored.snapshot().entries[0].title, "After Hours")

    def test_disconnect_before_agreement_creates_no_visit(self):
        self.begin()
        self.channel.incoming.put(b"")
        self.runner.join(3)
        self.assertFalse(self.runner.is_alive())
        self.assertEqual(self.backend.sessions, [])

    def test_idle_limit_disconnects_without_creating_a_visit(self):
        self.lobby.settings = LobbySettings(idle_timeout_seconds=0.02)
        self.runner.start()
        self.runner.join(3)
        self.assertFalse(self.runner.is_alive())
        self.assertIn("idle timeout", self.channel.output)
        self.assertEqual(self.backend.sessions, [])

    def test_short_terminal_pages_before_agreement(self):
        from dreamland.lobby_input import LobbyInput

        reader = LobbyInput(self.channel, stop_event=self.stop)
        worker = threading.Thread(
            target=lambda: SSHLobby._screen(
                reader,
                "\n".join(str(i) for i in range(9)),
                20,
                8,
                prompt=AGREEMENT_PROMPT,
            )
        )
        worker.start()
        self.channel.wait_for("Any key: continue")
        self.assertNotIn("i agree: accept", self.channel.output)
        offset = len(self.channel.output)
        self.channel.incoming.put(b" ")
        self.channel.wait_for("Any key: continue", after=offset)
        self.assertNotIn("i agree: accept", self.channel.output)
        self.channel.incoming.put(b" ")
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIn("8\r\ni agree: accept", self.channel.output)
        self.assertTrue(self.channel.output.endswith("> "))

    def test_standard_terminal_pages_before_agreement_and_starts_selected_world(self):
        self.lobby.content = LobbyContent(langfuse_enabled=True)
        self.runner = threading.Thread(
            target=lambda: self.lobby.run(
                self.channel,
                "visitor",
                self.backend,
                None,
                console=self.console,
                stop_event=self.stop,
                width=80,
                height=24,
            )
        )
        self.runner.start()
        self.channel.wait_for("Press any key to continue")
        self.assertNotIn("Type i agree", self.channel.output)
        offset = len(self.channel.output)
        self.channel.incoming.put(b" ")
        self.channel.wait_for("Press any key to continue, or Q to quit.", after=offset)
        self.assertIn("Page 1 of 2", self.channel.output)
        self.assertNotIn("Type i agree", self.channel.output)
        self.assertEqual(self.backend.sessions, [])
        self.channel.incoming.put(b" ")
        self.channel.wait_for("Type i agree")
        self.assertIn("Page 2 of 2", self.channel.output)
        self.assertIn("Report findings to admin@boxisalie.com.", self.channel.output)
        self.assertEqual(self.backend.sessions, [])
        offset = len(self.channel.output)
        self.channel.incoming.put(b"yes\n")
        self.channel.wait_for("Type i agree", after=offset)
        self.assertEqual(self.channel.output.count("Diagnostic/model traces"), 1)
        self.assertEqual(self.backend.sessions, [])
        self.channel.incoming.put(b"i agree\n")
        self.channel.wait_for("Enter a number")
        session = self.select()
        self.assertIsNotNone(session.runtime_provenance)
        offset = len(self.channel.output)
        self.channel.incoming.put(b"exit\n")
        self.channel.wait_for("Enter a number", after=offset)
        self.channel.incoming.put(b"q\n")
        self.runner.join(3)
        self.assertFalse(self.runner.is_alive())
        self.assertEqual(self.channel.exit_status, 0)


class PageReader:
    """Capture the complete terminal viewport at each input boundary."""

    def __init__(self):
        self.pages = []
        self.output = ""

    def send(self, text):
        self.output += text

    def any_key(self):
        self.pages.append(self.output)
        self.output = ""


class LobbyPresentationTests(unittest.TestCase):
    def screens(self):
        notice = LobbyContent(langfuse_enabled=True).snapshot()
        return (
            ("WELCOME", notice.welcome, WELCOME_PROMPT, True),
            (
                "GUIDELINES",
                notice.guidelines + f"\n\nReport findings to {notice.contact}.",
                AGREEMENT_PROMPT,
                False,
            ),
            (
                "CHOOSE A WORLD",
                "Every selection starts a fresh, isolated session.\n\n"
                "[1] First World\n    Explore its machines, files, and connections.\n\n"
                "[2] Second World\n    Explore another world with fresh state.",
                WORLD_PROMPT,
                False,
            ),
        )

    def render(self, screen, width=80, height=24):
        heading, text, prompt, keypress = screen
        reader = PageReader()
        SSHLobby._screen(
            reader,
            text,
            width,
            height,
            heading=heading,
            prompt=prompt,
            keypress=keypress,
        )
        return reader.pages + [reader.output]

    def test_all_pages_fit_content_and_action_in_the_reported_terminal(self):
        for width, height in (
            (20, 5),
            (20, 8),
            (20, 12),
            (20, 24),
            (39, 12),
            (40, 12),
            (40, 24),
            (80, 24),
            (120, 100),
        ):
            for screen in self.screens():
                with self.subTest(width=width, height=height, heading=screen[0]):
                    pages = self.render(screen, width, height)
                    for page in pages:
                        lines = page.split("\n")
                        self.assertLessEqual(len(lines), height)
                        self.assertTrue(all(len(line) <= width for line in lines))
                        self.assertNotIn("\x1b", page)
                        self.assertIn("Q", page)
                    if not screen[3]:
                        self.assertTrue(pages[-1].endswith("> "))
                    for page in pages[:-1]:
                        self.assertFalse(page.endswith("> "))

    def test_guidelines_and_contact_appear_once_before_final_agreement(self):
        screen = self.screens()[1]
        pages = self.render(screen)
        self.assertEqual(len(pages), 2)
        combined = " ".join("\n".join(pages).split())
        self.assertEqual(combined.count(LANGFUSE_DISCLOSURE), 1)
        self.assertEqual(combined.count("Report findings to admin@boxisalie.com."), 1)
        for page in pages[:-1]:
            self.assertIn("Press any key to continue, or Q to quit.", page)
            self.assertNotIn("Type i agree", page)
            self.assertNotIn("i agree: accept", page)
        self.assertIn("We will not publish", pages[-1])
        self.assertIn("Diagnostic/model traces", pages[-1])
        self.assertIn("Report findings to admin@boxisalie.com.", pages[-1])
        self.assertIn("Type i agree and press Enter to accept", pages[-1])
        self.assertNotIn("Press any key to continue", pages[-1])

    def test_every_stage_and_continuation_uses_the_same_numbered_footer(self):
        for screen in self.screens():
            pages = self.render(screen)
            for number, page in enumerate(pages, 1):
                with self.subTest(heading=screen[0], page=number):
                    self.assertIn(f"| {screen[0]}", page)
                    rule = f"  -- Page {number} of {len(pages)} "
                    self.assertEqual(page.count(rule), 1)
                    body, footer = page.split(rule)
                    self.assertTrue(body.strip())
                    self.assertIn("Q", footer)
                    self.assertNotIn("Press any key", body)
                    self.assertNotIn("Type i agree", body)
                    self.assertNotIn("Enter a number", body)

    def test_paragraph_paging_does_not_drop_or_repeat_content(self):
        text = "\n\n".join(
            f"unique-{i:03d} " + "word " * (i % 7 + 1) for i in range(40)
        )
        for width, height in ((20, 5), (40, 24), (80, 24)):
            pages = self.render(
                ("GUIDELINES", text, AGREEMENT_PROMPT, False), width, height
            )
            displayed = " ".join("\n".join(pages).split())
            for i in range(40):
                self.assertEqual(displayed.count(f"unique-{i:03d}"), 1)
