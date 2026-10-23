"""Annotate completed emulator traces; never imported by DREAMLAND runtime."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from dreamland.evaluation_annotations import (
    LangfuseHttpClient,
    build_report,
    fetch_session_observations,
    publish_scores,
    runtime_evidence,
)


def _client() -> LangfuseHttpClient:
    return LangfuseHttpClient(
        os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or "https://cloud.langfuse.com",
        os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
        os.environ.get("LANGFUSE_SECRET_KEY", ""),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--session-id",
        action="append",
        help="completed Langfuse session; repeat for multiple sessions",
    )
    source.add_argument(
        "--input", type=Path, help="existing observation export: {observations: [...]}"
    )
    parser.add_argument(
        "--runtime-revision",
        required=True,
        help="actual runtime Git revision, or 'unknown' for marker-only review",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="publish planned scores to Langfuse (default: dry-run)",
    )
    parser.add_argument(
        "--report", type=Path, help="write the review report as JSON instead of stdout"
    )
    parser.add_argument(
        "--metadata-json",
        type=Path,
        help="additional JSON object retained under annotations",
    )
    parser.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="WARNING"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level)
    try:
        if args.report and any(
            source and source.resolve() == args.report.resolve()
            for source in (args.input, args.metadata_json)
        ):
            raise ValueError("report output cannot overwrite an input file")
        runtime = runtime_evidence(
            Path(__file__).resolve().parents[1], args.runtime_revision
        )
        annotations: dict[str, Any] = {}
        if args.metadata_json:
            annotations = json.loads(args.metadata_json.read_text())
            if not isinstance(annotations, dict):
                raise ValueError("--metadata-json must contain a JSON object")
        client = None
        if args.input:
            exported = json.loads(args.input.read_text())
            observations = (
                exported.get("observations") if isinstance(exported, dict) else None
            )
            if not isinstance(observations, list) or not all(
                isinstance(o, dict) for o in observations
            ):
                raise ValueError(
                    "--input must contain an observations array of objects"
                )
        else:
            client = _client()
            observations = fetch_session_observations(client.request, args.session_id)
        report = build_report(observations, runtime=runtime, annotations=annotations)
        # Retain the review plan before publishing. An unavailable report
        # destination must not cause unreported score writes.
        rendered = (
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        if args.report:
            args.report.write_text(rendered, encoding="utf-8")
        if args.apply:
            if report["scores"]:
                client = client or _client()
                report["publication"] = publish_scores(client.request, report)
            else:
                report["publication"] = []
            report["mode"] = "apply"
        rendered = (
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        if args.report:
            args.report.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        if any(item["status"] == "failed" for item in report.get("publication", [])):
            return 1
        if (
            report["invalid_observation_rows"]
            or report["counts"].get("unknown")
            or not report["coverage"]
        ):
            return 2
        return 0
    except (OSError, ValueError) as exc:
        # Avoid printing response bodies, credentials, or raw model output.
        print(
            f"Annotation failed: {type(exc).__name__}. Check inputs, credentials and Langfuse availability.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
