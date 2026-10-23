import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_evaluation_annotations import observations

TOOL = Path(__file__).resolve().parents[1] / "tools" / "emulation_json_annotations.py"
SPEC = importlib.util.spec_from_file_location("emulation_json_annotations_tool", TOOL)
assert SPEC is not None and SPEC.loader is not None
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class AnnotationCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.input = Path(self.temp.name) / "observations.json"
        self.input.write_text(json.dumps({"observations": observations()}))
        self.args = ["--input", str(self.input), "--runtime-revision", "HEAD"]

    def test_offline_dry_run_has_no_network_and_no_raw_output(self):
        stdout = io.StringIO()
        with (
            patch.object(CLI, "_client", side_effect=AssertionError("network")),
            redirect_stdout(stdout),
        ):
            self.assertEqual(CLI.main(self.args), 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["counts"], {"repaired": 1})
        self.assertNotIn("hello", stdout.getvalue())

    def test_apply_reports_ingestion_acceptance_and_retains_custom_metadata(self):
        client = Mock()
        client.request.side_effect = lambda _method, _path, body: {
            "successes": [{"id": body["batch"][0]["id"], "status": 201}],
            "errors": [],
        }
        metadata = Path(self.temp.name) / "metadata.json"
        metadata.write_text('{"other_metric":{"value":7}}')
        report_path = Path(self.temp.name) / "report.json"
        with patch.object(CLI, "_client", return_value=client):
            status = CLI.main(
                self.args
                + [
                    "--apply",
                    "--report",
                    str(report_path),
                    "--metadata-json",
                    str(metadata),
                ]
            )
        self.assertEqual(status, 0)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["publication"][0]["status"], "accepted_for_ingestion")
        self.assertEqual(
            report["scores"][0]["body"]["metadata"]["dreamland.evaluation"][
                "annotations"
            ],
            {"other_metric": {"value": 7}},
        )

    def test_unknown_evidence_returns_coverage_code_and_writes_no_scores(self):
        data = observations()
        data.pop()
        self.input.write_text(json.dumps({"observations": data}))
        with (
            redirect_stdout(io.StringIO()),
            patch.object(CLI, "_client", side_effect=AssertionError("network")),
        ):
            self.assertEqual(CLI.main(self.args + ["--apply"]), 2)

    def test_input_error_does_not_print_raw_content_or_write(self):
        self.input.write_text("this is secret model content and is invalid input JSON")
        stderr = io.StringIO()
        with (
            redirect_stderr(stderr),
            patch.object(CLI, "_client", side_effect=AssertionError("network")),
        ):
            self.assertEqual(CLI.main(self.args + ["--apply"]), 1)
        self.assertNotIn("secret model", stderr.getvalue())

    def test_remote_read_failure_does_not_publish_partial_session(self):
        client = Mock()
        client.request.side_effect = [
            {"data": observations(), "meta": {"cursor": "more"}},
            OSError("failed"),
        ]
        with (
            patch.object(CLI, "_client", return_value=client),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                CLI.main(
                    ["--session-id", "session", "--runtime-revision", "HEAD", "--apply"]
                ),
                1,
            )
        self.assertTrue(all(c.args[0] == "GET" for c in client.request.call_args_list))

    def test_unwritable_report_and_input_collision_prevent_any_publication(self):
        for target in (self.input, Path(self.temp.name) / "missing" / "report.json"):
            with (
                self.subTest(target=target),
                redirect_stderr(io.StringIO()),
                patch.object(CLI, "_client", side_effect=AssertionError("network")),
            ):
                self.assertEqual(
                    CLI.main(self.args + ["--apply", "--report", str(target)]), 1
                )


if __name__ == "__main__":
    unittest.main()
