import io
import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from dreamland.evaluation_annotations import (
    LangfuseHttpClient,
    RuntimeEvidence,
    build_report,
    fetch_session_observations,
    publish_scores,
    runtime_evidence,
)
from dreamland.evaluation_json import SCORE_NAME, SUPPORTED_RUNTIME_SOURCE_SHA256

RUNTIME = RuntimeEvidence("a" * 40, "verified-fixture-parser", True)
VALID = json.dumps(
    {"stdout": "hello\n", "stderr": "", "exit_status": 0, "state_update": {}}
)


def observations(raw=None, metadata=None):
    if raw is None:
        raw = "```json\n" + VALID + "\n```"
    common = {
        "traceId": "trace-1",
        "startTime": "2026-09-06T12:00:00Z",
        "endTime": "2026-09-06T12:00:02Z",
        "environment": "test",
    }
    result = {
        "stdout": "hello\n",
        "stderr": "",
        "exit_status": 0,
        "metadata": metadata or {},
        "interrupted": False,
    }
    return [
        {
            **common,
            "id": "root-1",
            "name": "dreamland-command-agent",
            "type": "CHAIN",
            "parentObservationId": None,
            "output": json.dumps({"result": result}),
        },
        {
            **common,
            "id": "call-1",
            "name": "call_model",
            "type": "CHAIN",
            "parentObservationId": "root-1",
        },
        {
            **common,
            "id": "gen-1",
            "name": "ChatOpenAI",
            "type": "GENERATION",
            "parentObservationId": "call-1",
            "output": json.dumps({"role": "assistant", "content": raw}),
        },
    ]


class AnnotationEvidenceTests(unittest.TestCase):
    def test_repair_trace_score_with_exact_generation_and_private_raw_hash(self):
        original = observations()
        before = deepcopy(original)
        report = build_report(
            original,
            runtime=RUNTIME,
            annotations={"campaign": "example", "other_metric": {"x": 3}},
        )
        self.assertEqual(original, before)
        self.assertEqual(report["counts"], {"repaired": 1})
        score = report["scores"][0]
        body = score["body"]
        self.assertEqual(body["name"], SCORE_NAME)
        self.assertEqual(body["value"], "repaired")
        self.assertNotIn("observationId", body)
        self.assertEqual(body["traceId"], "trace-1")
        meta = body["metadata"]["dreamland.evaluation"]
        self.assertEqual(meta["evidence"]["generation_id"], "gen-1")
        self.assertEqual(meta["annotations"]["other_metric"], {"x": 3})
        self.assertEqual(len(meta["evidence"]["raw_sha256"]), 64)
        self.assertNotIn("hello", json.dumps(report))
        self.assertNotIn("```", json.dumps(report))

    def test_score_identity_and_timestamp_stable_across_rerun(self):
        first = build_report(observations(), runtime=RUNTIME)
        second = build_report(list(reversed(observations())), runtime=RUNTIME)
        self.assertEqual(first["scores"], second["scores"])
        self.assertEqual(first["scores"][0]["timestamp"], "2026-09-06T12:00:00+00:00")

    def test_provider_content_blocks_and_decoded_mcp_io(self):
        data = observations()
        data[0]["output"] = json.loads(data[0]["output"])
        data[2]["output"] = {
            "content": [
                {"type": "thinking", "thinking": "not model output"},
                {"type": "text", "text": "```json\n" + VALID + "\n```"},
            ]
        }
        self.assertEqual(build_report(data, runtime=RUNTIME)["counts"], {"repaired": 1})

    def test_raw_string_and_storage_encoded_string_are_not_reserialized(self):
        for value in (VALID, json.dumps(VALID)):
            with self.subTest(value=value):
                data = observations()
                data[2]["output"] = value
                report = build_report(data, runtime=RUNTIME)
                self.assertEqual(report["counts"], {"none_observed": 1})
                self.assertEqual(report["scores"], [])

    def test_decoded_raw_envelope_cannot_prove_serialization(self):
        data = observations()
        data[2]["output"] = json.loads(VALID)
        self.assertEqual(build_report(data, runtime=RUNTIME)["counts"], {"unknown": 1})

    def test_explicit_rejection_without_known_parser(self):
        raw = VALID[:-1]
        data = observations(
            raw,
            {
                "structured_response_accepted": False,
                "dreamland.structured.failure_class": "invalid_json",
                "dreamland.structured.raw_response": raw,
            },
        )
        result = json.loads(data[0]["output"])
        result["result"]["stdout"] = raw + "\n"
        data[0]["output"] = json.dumps(result)
        report = build_report(data, runtime=RuntimeEvidence("unknown", None, False))
        self.assertEqual(report["counts"], {"rejected": 1})

    def test_unknown_parser_does_not_reconstruct_repairs(self):
        report = build_report(
            observations(), runtime=RuntimeEvidence("unknown", None, False)
        )
        self.assertEqual(report["counts"], {"unknown": 1})
        self.assertEqual(report["scores"], [])

    def test_rejection_raw_fallback_retains_its_evidence_source(self):
        raw = VALID[:-1]
        data = observations(
            raw,
            {
                "structured_response_accepted": False,
                "dreamland.structured.failure_class": "invalid_json",
                "dreamland.structured.raw_response": raw,
            },
        )
        data[-1]["output"] = None
        report = build_report(data, runtime=RuntimeEvidence("unknown", None, False))
        evidence = report["scores"][0]["body"]["metadata"]["dreamland.evaluation"][
            "evidence"
        ]
        self.assertEqual(evidence["raw_source"], "result_metadata")
        self.assertEqual(len(evidence["raw_sha256"]), 64)

    def test_ambiguous_or_incomplete_evidence_never_scores(self):
        def change_result(data, **fields):
            result = json.loads(data[0]["output"])
            result["result"].update(fields)
            data[0]["output"] = result

        mutations = {
            "missing_generation": lambda d: d.pop(),
            "missing_parent": lambda d: d.pop(1),
            "duplicate_generation": lambda d: d.append({**d[-1], "id": "gen-2"}),
            "duplicate_row": lambda d: d.append(deepcopy(d[-1])),
            "duplicate_root": lambda d: d.append({**d[0], "id": "root-2"}),
            "wrong_parent": lambda d: d[-1].update(parentObservationId="elsewhere"),
            "cycle": lambda d: d[1].update(parentObservationId="gen-1"),
            "unfinished_root": lambda d: d[0].update(endTime=None),
            "unfinished_generation": lambda d: d[-1].update(endTime=None),
            "timestamp_missing": lambda d: d[0].pop("startTime"),
            "timestamp_unzoned": lambda d: d[0].update(startTime="2026-09-06T12:00:00"),
            "provider_error": lambda d: d[-1].update(level="ERROR"),
            "missing_result": lambda d: d[0].update(output=None),
            "mismatched_output": lambda d: change_result(d, stdout="different"),
            "interrupted": lambda d: change_result(d, interrupted=True),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                data = observations()
                mutate(data)
                report = build_report(data, runtime=RUNTIME)
                self.assertEqual(report["counts"], {"unknown": 1})
                self.assertFalse(report["scores"])

    def test_orphan_generation_is_unknown(self):
        report = build_report(observations()[1:], runtime=RUNTIME)
        self.assertEqual(report["counts"], {"unknown": 1})

    def test_builtins_and_other_agents_are_out_of_scope(self):
        data = observations()[:-1]
        data[1]["name"] = "handle_builtin"
        for name in ("dreamland-spiritguide-agent", "dreamland-enrichment-agent"):
            extra = observations()
            for row in extra:
                row["traceId"] = name
            extra[0]["name"] = name
            data.extend(extra)
        report = build_report(data, runtime=RUNTIME)
        self.assertEqual(report["counts"], {"out_of_scope": 3})
        self.assertEqual(report["scores"], [])

    def test_unknown_rows_are_counted_and_not_silently_omitted(self):
        report = build_report([{"name": "missing trace"}], runtime=RUNTIME)
        self.assertEqual(report["invalid_observation_rows"], 1)

    def test_runtime_revision_uses_git_blob_not_dirty_working_copy(self):
        root = Path(__file__).resolve().parents[1]
        runtime = runtime_evidence(root, "HEAD")
        self.assertEqual(len(runtime.revision), 40)
        self.assertTrue(runtime.parser_verified)
        self.assertEqual(
            runtime_evidence(root, "unknown"), RuntimeEvidence("unknown", None, False)
        )
        with self.assertRaises(ValueError):
            runtime_evidence(root, "--help")
        with self.assertRaises(ValueError):
            runtime_evidence(root, "definitely-not-a-real-runtime-revision")

    def test_one_unaudited_source_prevents_reconstructed_repair_scores(self):
        root = Path(__file__).resolve().parents[1]
        sources = {
            path: (root / path).read_bytes() for path in SUPPORTED_RUNTIME_SOURCE_SHA256
        }
        for changed_path in sources:
            with self.subTest(source=changed_path):
                outputs = [Mock(stdout=("a" * 40 + "\n").encode())]
                outputs.extend(
                    Mock(
                        stdout=source
                        + (
                            b"\n# Unaudited source change\n"
                            if path == changed_path
                            else b""
                        )
                    )
                    for path, source in sources.items()
                )
                with patch(
                    "dreamland.evaluation_annotations.subprocess.run",
                    side_effect=outputs,
                ):
                    runtime = runtime_evidence(root, "recorded-revision")
                self.assertFalse(runtime.parser_verified)
                self.assertNotIn(
                    runtime.source_sha256[changed_path],
                    SUPPORTED_RUNTIME_SOURCE_SHA256[changed_path],
                )
                report = build_report(observations(), runtime=runtime)
                self.assertEqual(report["counts"], {"unknown": 1})
                self.assertEqual(report["scores"], [])


class LangfuseAnnotationTransportTests(unittest.TestCase):
    def test_paginates_all_sessions_and_preserves_session_filter(self):
        responses = [
            {"data": [{"id": "one"}], "meta": {"cursor": "next"}},
            {"data": [{"id": "two"}], "meta": {"cursor": None}},
            {"data": [{"id": "three"}], "meta": {"cursor": None}},
        ]
        request = Mock(side_effect=responses)
        result = fetch_session_observations(
            request, ["session-A", "session-A", "session-B"]
        )
        self.assertEqual([o["id"] for o in result], ["one", "two", "three"])
        for call, session in zip(
            request.call_args_list, ["session-A", "session-A", "session-B"], strict=True
        ):
            args = call.args
            self.assertEqual(args[0], "GET")
            query = parse_qs(urlsplit(args[1]).query)
            self.assertEqual(json.loads(query["filter"][0])[0]["value"], session)
            self.assertIn("io", query["fields"][0])
            self.assertNotIn("parseIoAsJson", query)
        self.assertEqual(
            parse_qs(urlsplit(request.call_args_list[1].args[1]).query)["cursor"],
            ["next"],
        )

    def test_missing_or_repeating_pagination_cannot_be_called_complete(self):
        for response in ({"data": []}, {"data": [], "meta": {"cursor": "same"}}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                fetch_session_observations(Mock(return_value=response), ["session"])

    def test_empty_requested_session_cannot_disappear_from_multi_session_coverage(self):
        request = Mock(
            side_effect=[
                {"data": observations(), "meta": {}},
                {"data": [], "meta": {}},
            ]
        )
        with self.assertRaisesRegex(ValueError, "no available"):
            fetch_session_observations(request, ["present", "not-yet-ingested"])

    def test_publish_only_score_events_and_check_item_acceptance(self):
        report = build_report(observations(), runtime=RUNTIME)
        requests = []

        def request(method, path, body):
            requests.append((method, path, body))
            event = body["batch"][0]
            # Verify against the installed SDK's actual public ingestion schema.
            from langfuse.api.ingestion.types.ingestion_event import (
                IngestionEvent_ScoreCreate,
            )

            IngestionEvent_ScoreCreate.model_validate(event)
            return {"successes": [{"id": event["id"], "status": 201}], "errors": []}

        first = publish_scores(request, report)
        second = publish_scores(request, report)
        self.assertEqual(first[0]["status"], "accepted_for_ingestion")
        self.assertEqual(first, second)
        self.assertEqual(requests[0][0:2], ("POST", "/api/public/ingestion"))
        event1, event2 = [r[2]["batch"][0] for r in requests]
        self.assertEqual(event1["type"], "score-create")
        self.assertNotEqual(event1["id"], event2["id"])
        self.assertEqual(event1["body"], event2["body"])
        self.assertEqual(event1["timestamp"], event2["timestamp"])

    def test_batch_errors_missing_ack_and_transport_failures_are_not_success(self):
        report = build_report(observations(), runtime=RUNTIME)
        bad_responses = [
            {"successes": [], "errors": [{"message": "rejected"}]},
            {"successes": [{"id": "wrong-event", "status": 201}], "errors": []},
            {"successes": None},
        ]
        for response in bad_responses:
            with (
                self.subTest(response=response),
                self.assertLogs("dreamland.evaluation.annotations", level="WARNING"),
            ):
                self.assertEqual(
                    publish_scores(Mock(return_value=response), report)[0]["status"],
                    "failed",
                )
        with self.assertLogs("dreamland.evaluation.annotations", level="WARNING"):
            self.assertEqual(
                publish_scores(Mock(side_effect=OSError("network")), report)[0][
                    "status"
                ],
                "failed",
            )

    def test_http_request_credentials_transport_and_sanitized_error(self):
        client = LangfuseHttpClient("https://example.invalid", "pk-test", "secret-test")
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(b'{"data": [], "meta": {}}'))
        response.__exit__ = Mock(return_value=False)
        with patch.object(client._opener, "open", return_value=response) as opened:
            client.request("GET", "/api/public/v2/observations", None)
        request = opened.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://example.invalid/api/public/v2/observations"
        )
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        error = HTTPError("https://example.invalid", 401, "secret-test", {}, None)
        with (
            patch.object(client._opener, "open", side_effect=error),
            self.assertRaises(OSError) as caught,
        ):
            client.request("GET", "/api/public/v2/observations", None)
        self.assertNotIn("secret-test", str(caught.exception))

    def test_url_cannot_embed_or_redirect_credentials(self):
        for url in (
            "https://user:secret@example.invalid",
            "file:///tmp/test",
            "https://example.invalid?key=x",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LangfuseHttpClient(url, "pk", "sk")


if __name__ == "__main__":
    unittest.main()
