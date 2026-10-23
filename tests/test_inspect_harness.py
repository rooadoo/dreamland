import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HARNESS_ROOT = Path(__file__).resolve().parents[1] / "tools" / "inspect_ai"
sys.path.insert(0, str(HARNESS_ROOT))

from dreamland_inspect.cli import main as inspect_main  # noqa: E402
from dreamland_inspect.drivers.client_mcp import ClientMcpDriver  # noqa: E402
from dreamland_inspect.drivers.recording import CommandEventRecorder  # noqa: E402
from dreamland_inspect.events import (  # noqa: E402
    CommandEvent,
    load_events_jsonl,
    write_events_jsonl,
)
from dreamland_inspect.parsers.observations import observe_event  # noqa: E402
from dreamland_inspect.parsers.sections import split_sections  # noqa: E402
from dreamland_inspect.runs.runner import (  # noqa: E402
    _artifacts,
    _command_budget,
    _emulator_integrity,
    _pivot_quality,
    _print_run_summary,
    _structured_failures_for_events,
    _theme_conformance,
)
from dreamland_inspect.scorers.command_realism import (  # noqa: E402
    evaluate_command_events,
)

try:
    from dreamland_inspect.scorers.inspect_command_realism import (  # noqa: E402
        dreamland_command_realism,
    )
    from dreamland_inspect.tasks.client_mcp_explore import (  # noqa: E402
        BARE_SSH_PIVOT_STRESS_OBJECTIVE,
        BARE_SSH_PIVOT_STRESS_SYSTEM_MESSAGE,
        BARE_SSH_PIVOT_STRESS_VARIANT,
        DEFAULT_AGENT_MODEL,
        DEFAULT_COMMAND_TIMEOUT_SECONDS,
        DEFAULT_FREE_SSH_PIVOT_ALLOWANCE,
        DEFAULT_OBJECTIVE,
        DEFAULT_TASK_VARIANT,
        SYSTEM_MESSAGE,
        _auth_headers,
        client_mcp_explore,
    )
    from dreamland_inspect.tools.client_shell import (  # noqa: E402
        client_mcp_command_tool,
        format_command_result,
    )
    from inspect_ai.tool import ToolError  # noqa: E402
except ImportError:
    dreamland_command_realism = None
    DEFAULT_AGENT_MODEL = None
    BARE_SSH_PIVOT_STRESS_OBJECTIVE = None
    BARE_SSH_PIVOT_STRESS_SYSTEM_MESSAGE = None
    BARE_SSH_PIVOT_STRESS_VARIANT = None
    DEFAULT_COMMAND_TIMEOUT_SECONDS = None
    DEFAULT_FREE_SSH_PIVOT_ALLOWANCE = None
    DEFAULT_OBJECTIVE = None
    DEFAULT_TASK_VARIANT = None
    SYSTEM_MESSAGE = None
    _auth_headers = None
    client_mcp_explore = None
    client_mcp_command_tool = None
    format_command_result = None
    ToolError = None


class InspectHarnessTests(unittest.TestCase):
    def test_jsonl_events_round_trip(self):
        events = [
            CommandEvent(
                command_id="cmd-1",
                command="hostname",
                stdout="it-adm01\n",
                session_id="session-1",
                hostname="it-adm01",
                cwd="/home/user",
                metadata={"source": "test"},
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "commands.jsonl"
            write_events_jsonl(path, events)
            loaded = load_events_jsonl(path)

        self.assertEqual(loaded, events)

    def test_command_event_recorder_appends_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "run.commands.jsonl"
            recorder = CommandEventRecorder(path)

            recorder.record(command_id="cmd-1", command="hostname", stdout="it-adm01\n")
            recorder.record(command_id="cmd-2", command="whoami", stdout="teacher\n")

            loaded = load_events_jsonl(path)

        self.assertEqual([event.command_id for event in loaded], ["cmd-1", "cmd-2"])
        self.assertEqual(loaded[0].command, "hostname")

    def test_command_event_recorder_can_reset_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "run.commands.jsonl"
            recorder = CommandEventRecorder(path)
            recorder.record(command_id="old-1", command="hostname", stdout="old\n")

            reset_recorder = CommandEventRecorder(path, reset=True)
            reset_recorder.record(command_id="new-1", command="hostname", stdout="new\n")
            loaded = load_events_jsonl(path)

        self.assertEqual([event.command_id for event in loaded], ["new-1"])

    def test_client_mcp_driver_records_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "run.commands.jsonl"
            driver = ClientMcpDriver(
                session=_FakeClientMcpSession(),
                recorder=CommandEventRecorder(path),
                run_id="ridgeview",
            )

            async def run_driver():
                run = await driver.start(username="alice")
                event = await driver.run_command("pwd")
                closed = await driver.close()
                return run, event, closed

            run, event, closed = asyncio.run(run_driver())
            loaded = load_events_jsonl(path)

        self.assertEqual(run.session_id, "session-1")
        self.assertEqual(event.command_id, "ridgeview-0001")
        self.assertEqual(event.command, "pwd")
        self.assertEqual(event.stdout, "/home/alice\n")
        self.assertEqual(event.session_id, "session-1")
        self.assertEqual(event.hostname, "it-adm01")
        self.assertEqual(event.cwd, "/home/alice")
        self.assertEqual(event.metadata["source"], "client_mcp")
        self.assertEqual(event.metadata["state_profile"], "ridgeview:system")
        self.assertEqual(event.metadata["username"], "alice")
        self.assertEqual(event.metadata["active_program"], "bash")
        self.assertEqual(event.metadata["prompt"], "alice@it-adm01:/home/alice$ ")
        self.assertEqual(
            event.metadata["context"]["label"],
            "shell on alice@it-adm01:/home/alice",
        )
        self.assertTrue(closed)
        self.assertEqual(loaded, [event])

    def test_client_mcp_driver_requires_started_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            driver = ClientMcpDriver(
                session=_FakeClientMcpSession(),
                recorder=CommandEventRecorder(Path(tmpdir) / "run.commands.jsonl"),
                run_id="ridgeview",
            )

            async def run_driver():
                await driver.run_command("pwd")

            with self.assertRaisesRegex(RuntimeError, "not been started"):
                asyncio.run(run_driver())

    @unittest.skipIf(format_command_result is None, "inspect_ai is not installed")
    def test_command_tool_formats_repl_context(self):
        result = format_command_result(
            CommandEvent(
                command_id="cmd-python",
                command="print('hi')",
                stdout="hi\n",
                exit_status=0,
                hostname="dc01",
                cwd="/home/rviewadmin",
                metadata={
                    "context": {
                        "username": "rviewadmin",
                        "hostname": "dc01",
                        "cwd": "/home/rviewadmin",
                        "active_program": "python",
                        "prompt": ">>> ",
                        "label": "python on rviewadmin@dc01:/home/rviewadmin",
                    }
                },
            )
        )

        self.assertIn(
            "[context: python on rviewadmin@dc01:/home/rviewadmin]",
            result,
        )
        self.assertIn("hi", result)
        self.assertIn("exit_status: 0", result)

    @unittest.skipIf(client_mcp_command_tool is None, "inspect_ai is not installed")
    def test_client_mcp_command_tool_records_and_formats_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "run.commands.jsonl"
            driver = ClientMcpDriver(
                session=_FakeClientMcpSession(),
                recorder=CommandEventRecorder(path),
                run_id="ridgeview",
            )

            async def run_tool():
                await driver.start(username="alice")
                try:
                    command_tool = client_mcp_command_tool(
                        driver,
                        max_commands=1,
                        timeout_seconds=60,
                    )
                    result = await command_tool("pwd")
                    return result
                finally:
                    await driver.close()

            result = asyncio.run(run_tool())
            loaded = load_events_jsonl(path)

        self.assertIn("/home/alice", result)
        self.assertIn("[context: shell on alice@it-adm01:/home/alice]", result)
        self.assertIn("exit_status: 0", result)
        self.assertEqual(loaded[0].command_id, "ridgeview-0001")

    @unittest.skipIf(client_mcp_command_tool is None, "inspect_ai is not installed")
    def test_client_mcp_command_tool_enforces_command_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            driver = ClientMcpDriver(
                session=_FakeClientMcpSession(),
                recorder=CommandEventRecorder(Path(tmpdir) / "run.commands.jsonl"),
                run_id="ridgeview",
            )

            async def run_tool():
                await driver.start(username="alice")
                try:
                    command_tool = client_mcp_command_tool(
                        driver,
                        max_commands=1,
                        timeout_seconds=60,
                    )
                    await command_tool("pwd")
                    await command_tool("whoami")
                finally:
                    await driver.close()

            with self.assertRaisesRegex(ToolError, "budget is exhausted"):
                asyncio.run(run_tool())

    @unittest.skipIf(client_mcp_command_tool is None, "inspect_ai is not installed")
    def test_client_mcp_command_tool_does_not_charge_successful_ssh_pivot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "run.commands.jsonl"
            driver = ClientMcpDriver(
                session=_PivotClientMcpSession(),
                recorder=CommandEventRecorder(path),
                run_id="ridgeview",
            )

            async def run_tool():
                await driver.start(username="alice")
                try:
                    command_tool = client_mcp_command_tool(
                        driver,
                        max_commands=1,
                        free_ssh_pivot_allowance=1,
                        timeout_seconds=60,
                    )
                    pivot = await command_tool("ssh dc01")
                    pwd = await command_tool("pwd")
                    return pivot, pwd
                finally:
                    await driver.close()

            pivot, pwd = asyncio.run(run_tool())
            loaded = load_events_jsonl(path)

        self.assertEqual([event.command for event in loaded], ["ssh dc01", "pwd"])
        self.assertIn("[budget: charged 0/1; free ssh pivots 1/1; raw 1/2]", pivot)
        self.assertIn("[context: shell on alice@dc01:/home/alice]", pivot)
        self.assertIn("[budget: charged 1/1; free ssh pivots 1/1; raw 2/2]", pwd)
        self.assertEqual(driver.command_count, 2)

    @unittest.skipIf(client_mcp_command_tool is None, "inspect_ai is not installed")
    def test_client_mcp_command_tool_rejects_batchmode_and_remote_ssh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "run.commands.jsonl"
            driver = ClientMcpDriver(
                session=_PivotClientMcpSession(),
                recorder=CommandEventRecorder(path),
                run_id="ridgeview",
            )

            async def run_tool():
                await driver.start(username="alice")
                try:
                    command_tool = client_mcp_command_tool(
                        driver,
                        max_commands=3,
                        timeout_seconds=60,
                    )
                    batchmode = await command_tool(
                        "ssh -o BatchMode=yes dc01 'hostname'"
                    )
                    remote = await command_tool("ssh dc01 'hostname'")
                    return batchmode, remote
                finally:
                    await driver.close()

            batchmode, remote = asyncio.run(run_tool())
            loaded = load_events_jsonl(path)

        self.assertIn("BatchMode SSH is disabled", batchmode)
        self.assertIn("exit_status: 2", batchmode)
        self.assertIn("one-off remote SSH commands are disabled", remote)
        self.assertIn("exit_status: 2", remote)
        self.assertEqual(
            [event.metadata["rejected_by_harness"] for event in loaded],
            [True, True],
        )
        self.assertEqual([event.exit_status for event in loaded], [2, 2])

    @unittest.skipIf(client_mcp_command_tool is None, "inspect_ai is not installed")
    def test_client_mcp_command_tool_records_timeout_as_failed_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "run.commands.jsonl"
            driver = ClientMcpDriver(
                session=_SlowClientMcpSession(),
                recorder=CommandEventRecorder(path),
                run_id="ridgeview",
            )

            async def run_tool():
                await driver.start(username="alice")
                try:
                    command_tool = client_mcp_command_tool(
                        driver,
                        max_commands=1,
                        timeout_seconds=0.01,
                    )
                    return await command_tool("sleep 2")
                finally:
                    await driver.close()

            result = asyncio.run(run_tool())
            loaded = load_events_jsonl(path)

        self.assertIn("command timed out", result)
        self.assertIn("exit_status: 124", result)
        self.assertTrue(loaded[0].metadata["timed_out"])
        self.assertEqual(loaded[0].exit_status, 124)

    @unittest.skipIf(client_mcp_explore is None, "inspect_ai is not installed")
    def test_client_mcp_explore_task_points_scorer_at_command_artifact(self):
        task = client_mcp_explore(
            mcp_url="http://127.0.0.1:9999/client-mcp",
            api_token="test-token",
            username="alice",
            run_id="ridgeview",
            output_dir="tools/inspect_ai/outputs/client-mcp",
            max_commands=3,
        )

        sample = task.dataset[0]

        self.assertEqual(sample.id, "ridgeview")
        self.assertEqual(
            sample.metadata["commands_path"],
            "tools/inspect_ai/outputs/client-mcp/ridgeview/run.commands.jsonl",
        )
        self.assertEqual(sample.metadata["max_commands"], 3)
        self.assertEqual(
            sample.metadata["free_ssh_pivot_allowance"],
            DEFAULT_FREE_SSH_PIVOT_ALLOWANCE,
        )
        self.assertIn("budget of up to 3 charged shell commands", sample.input)
        self.assertIn("standalone SSH pivots are free", sample.input)
        self.assertIsNone(sample.metadata["expected_state_profile"])
        self.assertEqual(sample.metadata["task_variant"], DEFAULT_TASK_VARIANT)
        self.assertEqual(
            sample.metadata["command_timeout_seconds"],
            DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
        self.assertEqual(DEFAULT_AGENT_MODEL, "openai/gpt-5.4-mini")
        self.assertEqual(task.model.name, "gpt-5.4-mini")
        self.assertEqual(_auth_headers("test-token"), {"Authorization": "Bearer test-token"})

    @unittest.skipIf(client_mcp_explore is None, "inspect_ai is not installed")
    def test_client_mcp_explore_records_expected_state_profile(self):
        task = client_mcp_explore(
            run_id="ridgeview",
            expected_state_profile="ridgeview:system",
        )

        sample = task.dataset[0]

        self.assertEqual(sample.metadata["expected_state_profile"], "ridgeview:system")
        self.assertIn("remote workstation", sample.input)
        self.assertIn("safe read-only pivots", sample.input)
        self.assertNotIn("public school district", sample.input)
        self.assertNotIn("Ridgeview", sample.input)

    @unittest.skipIf(client_mcp_explore is None, "inspect_ai is not installed")
    def test_client_mcp_prompt_guidance_is_scenario_neutral(self):
        normalized_system = " ".join(SYSTEM_MESSAGE.split())
        normalized_objective = " ".join(DEFAULT_OBJECTIVE.split())

        self.assertIn("Other systems checked", SYSTEM_MESSAGE)
        self.assertIn("Organization, roles, and user populations", SYSTEM_MESSAGE)
        self.assertIn("Pivot chains and depth", SYSTEM_MESSAGE)
        self.assertIn("full nested shell", normalized_system)
        self.assertIn(
            "run SSH to that host as the entire command",
            normalized_system,
        )
        self.assertIn(
            "subsequent commands run inside that pivoted shell",
            normalized_system,
        )
        self.assertIn("Aim for N-deep pivot chains", SYSTEM_MESSAGE)
        self.assertIn("BatchMode-style SSH and quoted one-off", normalized_system)
        self.assertIn("do not consume the charged command budget", SYSTEM_MESSAGE)
        self.assertIn("non-destructive mapping", SYSTEM_MESSAGE)
        self.assertIn("non-obvious links", SYSTEM_MESSAGE)
        self.assertIn("verify basic OS and session details", normalized_system)
        self.assertIn("uname -a", SYSTEM_MESSAGE)
        self.assertIn("uptime", SYSTEM_MESSAGE)
        self.assertNotIn("verify the new context", normalized_system)
        self.assertIn("safe read-only pivots", normalized_objective)
        self.assertIn(
            "running SSH to the host as the whole command",
            normalized_objective,
        )
        self.assertIn("continue N-deep", normalized_objective)
        for scenario_word in ["school district", "seaport", "banking", "Ridgeview"]:
            self.assertNotIn(scenario_word, DEFAULT_OBJECTIVE)
            self.assertNotIn(scenario_word, SYSTEM_MESSAGE)

    @unittest.skipIf(client_mcp_explore is None, "inspect_ai is not installed")
    def test_client_mcp_bare_ssh_pivot_stress_variant(self):
        task = client_mcp_explore(
            run_id="pivot-stress",
            max_commands=5,
            task_variant=BARE_SSH_PIVOT_STRESS_VARIANT,
        )

        sample = task.dataset[0]
        normalized_system = " ".join(
            BARE_SSH_PIVOT_STRESS_SYSTEM_MESSAGE.split()
        )
        normalized_objective = " ".join(
            BARE_SSH_PIVOT_STRESS_OBJECTIVE.split()
        )

        self.assertEqual(
            sample.metadata["task_variant"],
            BARE_SSH_PIVOT_STRESS_VARIANT,
        )
        self.assertIn("stress test for deep full-session SSH pivots", sample.input)
        self.assertIn("budget of up to 5 charged shell commands", sample.input)
        self.assertIn("standalone bare SSH commands", normalized_objective)
        self.assertIn("Do not use quoted remote SSH commands", normalized_system)
        self.assertIn("Issue the bare SSH command alone", normalized_system)
        self.assertIn("Successful standalone SSH pivots do not consume", normalized_system)
        self.assertIn("verify basic OS and session details", normalized_objective)
        self.assertIn("OS and session verification after pivots", normalized_system)
        self.assertIn("uname -a", BARE_SSH_PIVOT_STRESS_SYSTEM_MESSAGE)
        self.assertIn("uptime", BARE_SSH_PIVOT_STRESS_SYSTEM_MESSAGE)
        self.assertNotIn("verify the new context", normalized_system)
        for scenario_word in ["school district", "seaport", "banking", "Ridgeview"]:
            self.assertNotIn(scenario_word, BARE_SSH_PIVOT_STRESS_OBJECTIVE)
            self.assertNotIn(scenario_word, BARE_SSH_PIVOT_STRESS_SYSTEM_MESSAGE)

    @unittest.skipIf(client_mcp_explore is None, "inspect_ai is not installed")
    def test_client_mcp_auth_defaults_to_environment(self):
        with patch.dict("os.environ", {"DREAMLAND_MCP_AUTH": "env-token"}):
            self.assertEqual(
                _auth_headers(None),
                {"Authorization": "Bearer env-token"},
            )
            self.assertEqual(
                _auth_headers("explicit-token"),
                {"Authorization": "Bearer explicit-token"},
            )

    def test_split_sections_uses_agent_markers(self):
        sections = split_sections(
            "== identity ==\n"
            "it-adm01\n"
            "== network ==\n"
            "default via 10.50.1.1 dev eno1\n"
        )

        self.assertEqual([section.title for section in sections], ["identity", "network"])
        self.assertEqual(sections[0].body, "it-adm01")
        self.assertIn("10.50.1.1", sections[1].body)

    def test_observe_event_extracts_network_and_nmap_evidence(self):
        event = CommandEvent(
            command_id="cmd-2",
            command="ip -o -4 addr show; ip route; nmap -sT 10.50.1.5",
            stdout=(
                "2: eno1    inet 10.50.1.10/24 brd 10.50.1.255 "
                "scope global eno1\\       valid_lft forever preferred_lft forever\n"
                "default via 10.50.1.1 dev eno1 proto static\n"
                "Nmap scan report for dc01.ridgeviewps.edu (10.50.1.5)\n"
                "PORT    STATE SERVICE\n"
                "53/tcp  open  domain\n"
                "389/tcp open  ldap\n"
            ),
        )

        observation = observe_event(event)

        self.assertIn("10.50.1.10", observation.ipv4_addresses)
        self.assertEqual(observation.interfaces[0].interface, "eno1")
        self.assertEqual(observation.routes[0].gateway, "10.50.1.1")
        self.assertEqual(observation.nmap_hosts[0].target, "dc01.ridgeviewps.edu (10.50.1.5)")
        self.assertEqual(
            [(port.port, port.state, port.service) for port in observation.nmap_hosts[0].ports],
            [(53, "open", "domain"), (389, "open", "ldap")],
        )

    def test_evaluate_command_events_flags_envelope_leaks(self):
        event = CommandEvent(
            command_id="cmd-3",
            command="find /opt",
            stdout=(
                '{"stdout":"== files ==\\n/opt/it/inventory/hosts.txt\\n",'
                '"stderr":"","exit_status":0,"state_update":{}}\n'
            ),
        )

        report = evaluate_command_events([event])

        self.assertEqual(report.command_count, 1)
        self.assertLess(report.score, 1.0)
        self.assertEqual(
            [finding.finding_type for finding in report.findings],
            ["format_realism.envelope_leak"],
        )
        self.assertEqual(report.findings[0].severity, "critical")

    def test_evaluate_command_events_flags_timeouts(self):
        event = CommandEvent(
            command_id="cmd-timeout",
            command="find /",
            stdout="",
            stderr="command timed out after 60 seconds",
            exit_status=124,
            metadata={"timed_out": True, "timeout_seconds": 60},
        )

        report = evaluate_command_events([event])

        self.assertEqual(
            [finding.finding_type for finding in report.findings],
            ["command.timeout"],
        )
        self.assertEqual(report.findings[0].severity, "high")

    def test_evaluate_command_events_flags_mixed_artifacts(self):
        events = [
            CommandEvent(
                command_id="run-0001",
                command="hostname",
                stdout="a\n",
                session_id="session-a",
                metadata={"run_id": "run", "state_profile": "ridgeview:system"},
            ),
            CommandEvent(
                command_id="run-0001",
                command="hostname",
                stdout="b\n",
                session_id="session-b",
                metadata={"run_id": "run", "state_profile": "seaport:system"},
            ),
        ]

        report = evaluate_command_events(
            events,
            expected_state_profile="ridgeview:system",
        )

        self.assertIn(
            "artifact.duplicate_command_id",
            [finding.finding_type for finding in report.findings],
        )
        self.assertIn(
            "artifact.multiple_sessions",
            [finding.finding_type for finding in report.findings],
        )
        self.assertIn(
            "scenario.state_profile_mismatch",
            [finding.finding_type for finding in report.findings],
        )

    def test_cli_evaluate_returns_nonzero_for_findings(self):
        event = CommandEvent(
            command_id="cmd-4",
            command="find /opt",
            stdout='{"stdout":"/opt/it\\n","stderr":"","exit_status":0}\n',
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "commands.jsonl"
            write_events_jsonl(path, [event])
            output = StringIO()
            with redirect_stdout(output):
                status = inspect_main(["evaluate", "--json", str(path)])

        self.assertEqual(status, 1)
        self.assertIn("format_realism.envelope_leak", output.getvalue())

    def test_cli_run_dry_run_writes_manifest_and_invocation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            spec_path = tmp_path / "ridgeview.toml"
            spec_path.write_text(
                'id = "ridgeview"\n'
                'state_profile = "ridgeview:system"\n'
                'max_commands = 3\n',
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                status = inspect_main(
                    [
                        "run",
                        str(spec_path),
                        "--output-root",
                        str(tmp_path / "runs"),
                        "--dry-run",
                    ]
                )
            run_dirs = list((tmp_path / "runs").iterdir())
            invocation = (run_dirs[0] / "inspect.invocation.json").read_text(
                encoding="utf-8"
            )
            preflight = (run_dirs[0] / "preflight.json").read_text(
                encoding="utf-8"
            )

            self.assertEqual(status, 0)
            self.assertEqual(len(run_dirs), 1)
            self.assertTrue((run_dirs[0] / "run.manifest.json").exists())
            self.assertIn("expected_state_profile=ridgeview:system", invocation)
            self.assertIn("task_variant=explore", invocation)
            self.assertIn(
                "tools/inspect_ai/dreamland_inspect/tasks/client_mcp_explore.py",
                invocation,
            )
            self.assertNotIn(
                f"{Path.cwd()}/tools/inspect_ai/dreamland_inspect/tasks/client_mcp_explore.py",
                invocation,
            )
            self.assertIn("would_apply_default_state_profile", preflight)

    def test_cli_review_summarizes_existing_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            artifacts = _artifacts(tmp_path / "runs", "20260707T000000Z_demo_abc123")
            artifacts.run_dir.mkdir(parents=True)
            artifacts.score_path.write_text(
                json.dumps(
                    {
                        "score": 1.0,
                        "overall_score": 0.75,
                        "command_realism": {"score": 1.0},
                        "pivot_quality": {
                            "score": 0.5,
                            "bare_ssh_pivot_count": 1,
                            "successful_ssh_pivot_count": 1,
                            "failed_ssh_pivot_count": 0,
                            "batchmode_ssh_count": 2,
                            "unique_host_count": 2,
                        },
                        "findings": [{"type": "pivot.batchmode_dominant"}],
                    }
                ),
                encoding="utf-8",
            )
            artifacts.commands_path.write_text("", encoding="utf-8")

            output = StringIO()
            with redirect_stdout(output):
                status = inspect_main(
                    [
                        "review",
                        "--output-root",
                        str(tmp_path / "runs"),
                        "--recent",
                        "1",
                    ]
                )

        self.assertEqual(status, 0)
        summary = output.getvalue()
        self.assertIn("run_dir:", summary)
        self.assertIn("overall_score: 0.75", summary)
        self.assertIn(
            "pivot_score: 0.5 (bare=1, success=1, failed=0, batch=2, hosts=2)",
            summary,
        )
        self.assertIn("pivot.batchmode_dominant: 1", summary)

    def test_cli_timeline_prints_host_transitions_and_markers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            artifacts = _artifacts(tmp_path / "runs", "run-1")
            artifacts.run_dir.mkdir(parents=True)
            write_events_jsonl(
                artifacts.commands_path,
                [
                    CommandEvent(
                        command_id="cmd-1",
                        command="hostname",
                        stdout="ops-adm01\n",
                        hostname="ops-adm01",
                        cwd="/home/operator",
                        metadata={
                            "context": {
                                "label": "shell on operator@ops-adm01:/home/operator"
                            }
                        },
                    ),
                    CommandEvent(
                        command_id="cmd-2",
                        command="ssh gw-mgmt01",
                        stdout="",
                        hostname="gw-mgmt01",
                        cwd="/home/operator",
                        metadata={
                            "context": {
                                "label": "shell on operator@gw-mgmt01:/home/operator"
                            }
                        },
                    ),
                    CommandEvent(
                        command_id="cmd-3",
                        command="cat notes.txt; ssh rtr-water-core01",
                        stdout="",
                        hostname="rtr-water-core01",
                        cwd="/home/operator",
                        metadata={
                            "context": {
                                "label": (
                                    "shell on operator@rtr-water-core01:"
                                    "/home/operator"
                                )
                            }
                        },
                    ),
                    CommandEvent(
                        command_id="cmd-4",
                        command="ssh host 'hostname'",
                        stdout="",
                        stderr="Quoted or one-off remote SSH commands are disabled",
                        exit_status=2,
                        hostname="rtr-water-core01",
                        cwd="/home/operator",
                        metadata={
                            "rejected_by_harness": True,
                            "context": {
                                "label": (
                                    "shell on operator@rtr-water-core01:"
                                    "/home/operator"
                                )
                            },
                        },
                    ),
                ],
            )

            output = StringIO()
            with redirect_stdout(output):
                status = inspect_main(["timeline", str(artifacts.run_dir)])

        self.assertEqual(status, 0)
        timeline = output.getvalue()
        self.assertIn("commands: 4", timeline)
        self.assertIn("host_transitions: 2", timeline)
        self.assertIn("chained_or_nonstandalone_transitions: 1", timeline)
        self.assertIn("harness_rejections: 1", timeline)
        self.assertIn("ops-adm01 -> gw-mgmt01 via ssh gw-mgmt01", timeline)
        self.assertIn(
            "gw-mgmt01 -> rtr-water-core01 via cat notes.txt; ssh rtr-water-core01",
            timeline,
        )
        self.assertIn("[ssh host-change pivot-ok]", timeline)
        self.assertIn("[ssh embedded-ssh host-change nonstandalone-pivot]", timeline)
        self.assertIn("[rejected ssh remote-ssh]", timeline)

    def test_theme_conformance_scores_expected_terms(self):
        conformance = _theme_conformance(
            [
                "public school district",
                "schools",
                "students",
                "teachers",
                "staff",
            ],
            (
                "Ridgeview district routes school sites through the admin VLAN. "
                "Student and staff data must stay off this workstation."
            ),
        )

        self.assertEqual(conformance["score"], 0.8)
        self.assertEqual(conformance["matched_count"], 4)
        self.assertEqual(conformance["missing"], ["teachers"])

    def test_run_summary_prints_score_breakdown_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts = _artifacts(Path(tmpdir), "run-1")
            artifacts.run_dir.mkdir()
            artifacts.commands_path.write_text("", encoding="utf-8")
            artifacts.agent_report_path.write_text("report", encoding="utf-8")
            artifacts.structured_failures_path.write_text(
                '{"count": 2, "failures": []}',
                encoding="utf-8",
            )
            artifacts.score_path.write_text(
                json.dumps(
                    {
                        "score": 1.0,
                        "overall_score": 0.667,
                        "command_realism": {"score": 1.0, "findings": []},
                        "scenario_conformance": {
                            "score": 1.0,
                            "matched_count": 5,
                            "expected_count": 5,
                        },
                        "emulator_integrity": {
                            "score": 0.0,
                            "structured_failure_count": 2,
                        },
                        "pivot_quality": {
                            "score": 0.5,
                            "bare_ssh_pivot_count": 1,
                            "successful_ssh_pivot_count": 1,
                            "failed_ssh_pivot_count": 0,
                            "batchmode_ssh_count": 3,
                            "unique_host_count": 2,
                        },
                        "command_budget": {
                            "raw_command_count": 12,
                            "charged_command_count": 10,
                            "free_ssh_pivot_count": 2,
                            "max_charged_commands": 20,
                            "free_ssh_pivot_allowance": 10,
                            "raw_command_cap": 30,
                        },
                        "findings": [
                            {"type": "emulator.structured_failure"},
                            {"type": "emulator.structured_failure"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output = StringIO()
            with redirect_stdout(output):
                _print_run_summary(artifacts, 0)

        summary = output.getvalue()
        self.assertIn("overall_score: 0.667", summary)
        self.assertIn("command_score: 1.0", summary)
        self.assertIn("scenario_score: 1.0 (5/5 themes)", summary)
        self.assertIn("emulator_score: 0.0 (2 structured failures)", summary)
        self.assertIn(
            "pivot_score: 0.5 (bare=1, success=1, failed=0, batch=3, hosts=2)",
            summary,
        )
        self.assertIn(
            "command_budget: charged=10/20, free_ssh=2/10, raw=12/30",
            summary,
        )
        self.assertIn("finding_types:", summary)
        self.assertIn("  emulator.structured_failure: 2", summary)
        self.assertIn("score_json:", summary)
        self.assertIn("structured_failures:", summary)
        self.assertIn("commands_jsonl:", summary)
        self.assertIn("agent_report:", summary)

    def test_pivot_quality_flags_batchmode_without_full_pivots(self):
        quality = _pivot_quality(
            [
                CommandEvent(
                    command_id="cmd-1",
                    command="ssh -o BatchMode=yes dc01 'hostname'",
                    stdout="dc01\n",
                    hostname="it-adm01",
                    cwd="/home/user",
                ),
                CommandEvent(
                    command_id="cmd-2",
                    command="ssh -o BatchMode=yes files01 'hostname'",
                    stdout="files01\n",
                    hostname="it-adm01",
                    cwd="/home/user",
                ),
            ]
        )

        self.assertEqual(quality["ssh_command_count"], 2)
        self.assertEqual(quality["batchmode_ssh_count"], 2)
        self.assertEqual(quality["bare_ssh_pivot_count"], 0)
        self.assertIn(
            "pivot.no_full_ssh_pivots",
            [finding["type"] for finding in quality["findings"]],
        )

    def test_pivot_quality_recognizes_bare_pivot_and_host_change(self):
        quality = _pivot_quality(
            [
                CommandEvent(
                    command_id="cmd-0",
                    command="hostname",
                    stdout="it-adm01\n",
                    hostname="it-adm01",
                    cwd="/home/rviewadmin",
                    metadata={
                        "context": {
                            "label": "shell on rviewadmin@it-adm01:/home/rviewadmin"
                        }
                    },
                ),
                CommandEvent(
                    command_id="cmd-1",
                    command="ssh dc01",
                    stdout="",
                    hostname="dc01",
                    cwd="/home/rviewadmin",
                    metadata={
                        "context": {
                            "label": "shell on rviewadmin@dc01:/home/rviewadmin"
                        }
                    },
                ),
                CommandEvent(
                    command_id="cmd-2",
                    command="ssh files01",
                    stdout="",
                    hostname="files01",
                    cwd="/home/itadmin",
                    metadata={
                        "context": {
                            "label": "shell on itadmin@files01:/home/itadmin"
                        }
                    },
                ),
            ]
        )

        self.assertEqual(quality["bare_ssh_pivot_count"], 2)
        self.assertEqual(quality["successful_ssh_pivot_count"], 2)
        self.assertEqual(quality["failed_ssh_pivot_count"], 0)
        self.assertEqual(quality["unique_hosts"], ["dc01", "files01", "it-adm01"])
        self.assertEqual(quality["findings"], [])
        self.assertEqual(
            quality["context_labels"],
            [
                "shell on rviewadmin@it-adm01:/home/rviewadmin",
                "shell on rviewadmin@dc01:/home/rviewadmin",
                "shell on itadmin@files01:/home/itadmin",
            ],
        )

    def test_command_budget_counts_successful_host_change_pivots_as_free(self):
        budget = _command_budget(
            [
                CommandEvent(
                    command_id="cmd-1",
                    command="hostname",
                    stdout="it-adm01\n",
                    hostname="it-adm01",
                ),
                CommandEvent(
                    command_id="cmd-2",
                    command="ssh dc01",
                    stdout="",
                    hostname="dc01",
                ),
                CommandEvent(
                    command_id="cmd-3",
                    command="pwd",
                    stdout="/home/alice\n",
                    hostname="dc01",
                ),
                CommandEvent(
                    command_id="cmd-4",
                    command="ssh dc02 'hostname'",
                    stdout="dc02\n",
                    hostname="dc01",
                ),
            ],
            max_commands=3,
            free_ssh_pivot_allowance=2,
        )

        self.assertEqual(
            budget,
            {
                "raw_command_count": 4,
                "charged_command_count": 3,
                "free_ssh_pivot_count": 1,
                "max_charged_commands": 3,
                "free_ssh_pivot_allowance": 2,
                "raw_command_cap": 5,
            },
        )

    def test_structured_failures_match_session_and_command(self):
        event = CommandEvent(
            command_id="run-0007",
            command="ssh -o BatchMode=yes dc01 'hostname'",
            stdout="dc01\n",
            session_id="session-1",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            failure_dir = Path(tmpdir)
            (failure_dir / "failure.json").write_text(
                json.dumps(
                    {
                        "kind": "dreamland.structured_failure_capsule",
                        "id": "capsule-1",
                        "created_at": "2026-07-07T01:55:51Z",
                        "session_id": "session-1",
                        "command_id": "dreamland-command-1",
                        "command": "ssh -o BatchMode=yes dc01 'hostname'",
                        "failure_class": "invalid_json",
                        "parser_error": "Expecting ',' delimiter",
                    }
                ),
                encoding="utf-8",
            )
            (failure_dir / "unrelated.json").write_text(
                json.dumps(
                    {
                        "kind": "dreamland.structured_failure_capsule",
                        "id": "capsule-2",
                        "session_id": "other-session",
                        "command": "ssh -o BatchMode=yes dc01 'hostname'",
                    }
                ),
                encoding="utf-8",
            )

            failures = _structured_failures_for_events(
                [event],
                directory=failure_dir,
            )

        self.assertEqual(failures["count"], 1)
        self.assertEqual(failures["failures"][0]["id"], "capsule-1")
        self.assertEqual(
            failures["failures"][0]["dreamland_command_id"],
            "dreamland-command-1",
        )
        self.assertIn("failure.json", failures["failures"][0]["path"])

    def test_emulator_integrity_penalizes_structured_failures(self):
        integrity = _emulator_integrity(
            {
                "count": 2,
                "failures": [
                    {
                        "id": "capsule-1",
                        "dreamland_command_id": "dreamland-command-1",
                    },
                    {
                        "id": "capsule-2",
                        "dreamland_command_id": "dreamland-command-2",
                    },
                ],
            }
        )

        self.assertEqual(integrity["score"], 0.7)
        self.assertEqual(integrity["structured_failure_count"], 2)
        self.assertEqual(
            [finding["type"] for finding in integrity["findings"]],
            ["emulator.structured_failure", "emulator.structured_failure"],
        )

    @unittest.skipIf(dreamland_command_realism is None, "inspect_ai is not installed")
    def test_inspect_scorer_scores_command_artifact(self):
        event = CommandEvent(
            command_id="cmd-5",
            command="hostname",
            stdout="it-adm01\n",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "commands.jsonl"
            write_events_jsonl(path, [event])
            scorer = dreamland_command_realism()
            score = asyncio.run(
                scorer(
                    SimpleNamespace(metadata={"commands_path": str(path)}),
                    target=None,
                )
            )

        self.assertEqual(score.value, 1.0)
        self.assertEqual(score.metadata["command_count"], 1)


class _FakeClientMcpSession:
    async def call_tool(self, name, arguments=None):
        if name == "client_session_create":
            return {
                "session_id": "session-1",
                "username": arguments["username"],
                "hostname": "it-adm01",
                "cwd": "/home/alice",
                "state_profile": "ridgeview:system",
            }
        if name == "client_run_command":
            return {
                "session_id": arguments["session_id"],
                "command": arguments["command"],
                "stdout": "/home/alice\n",
                "stderr": "",
                "exit_status": 0,
                "interrupted": False,
                "signal": None,
                "username": "alice",
                "hostname": "it-adm01",
                "cwd": "/home/alice",
                "active_program": "bash",
                "prompt": "alice@it-adm01:/home/alice$ ",
                "context": {
                    "username": "alice",
                    "hostname": "it-adm01",
                    "cwd": "/home/alice",
                    "active_program": "bash",
                    "prompt": "alice@it-adm01:/home/alice$ ",
                    "label": "shell on alice@it-adm01:/home/alice",
                },
            }
        if name == "client_session_exit":
            return {"session_id": arguments["session_id"], "closed": True}
        raise AssertionError(f"unexpected tool call: {name}")


class _PivotClientMcpSession(_FakeClientMcpSession):
    def __init__(self):
        self.hostname = "it-adm01"

    async def call_tool(self, name, arguments=None):
        if name == "client_session_create":
            self.hostname = "it-adm01"
            return await super().call_tool(name, arguments)
        if name == "client_run_command":
            command = arguments["command"]
            if command == "ssh dc01":
                self.hostname = "dc01"
                stdout = "Last login: Tue Jul 7 12:01:00 2026 from it-adm01\n"
            else:
                stdout = "/home/alice\n"
            return {
                "session_id": arguments["session_id"],
                "command": command,
                "stdout": stdout,
                "stderr": "",
                "exit_status": 0,
                "interrupted": False,
                "signal": None,
                "username": "alice",
                "hostname": self.hostname,
                "cwd": "/home/alice",
                "active_program": "bash",
                "prompt": f"alice@{self.hostname}:/home/alice$ ",
                "context": {
                    "username": "alice",
                    "hostname": self.hostname,
                    "cwd": "/home/alice",
                    "active_program": "bash",
                    "prompt": f"alice@{self.hostname}:/home/alice$ ",
                    "label": f"shell on alice@{self.hostname}:/home/alice",
                },
            }
        return await super().call_tool(name, arguments)


class _SlowClientMcpSession(_FakeClientMcpSession):
    async def call_tool(self, name, arguments=None):
        if name == "client_run_command":
            await asyncio.sleep(1)
        return await super().call_tool(name, arguments)


if __name__ == "__main__":
    unittest.main()
