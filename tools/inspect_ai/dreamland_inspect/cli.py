"""Local command-line helpers for DREAMLAND evaluation artifacts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from dreamland_inspect.events import load_events_jsonl
from dreamland_inspect.runs.runner import print_timeline, review_runs, run_spec
from dreamland_inspect.scorers.command_realism import evaluate_command_events


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dreamland_inspect",
        description="Evaluate DREAMLAND command-event artifacts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    evaluate_parser = subparsers.add_parser(
        "evaluate",
        help="Score a command-events JSONL file.",
    )
    evaluate_parser.add_argument("path", help="Path to command-events JSONL.")
    evaluate_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a text summary.",
    )
    evaluate_parser.add_argument(
        "--expected-state-profile",
        help="Expected DREAMLAND state profile label for scenario consistency.",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Run a trusted Inspect exploration from a run spec.",
    )
    run_parser.add_argument("spec", help="Path to a TOML run spec.")
    run_parser.add_argument(
        "--output-root",
        default="tools/inspect_ai/outputs/runs",
        help="Directory where run directories are created.",
    )
    run_parser.add_argument(
        "--display",
        default="plain",
        help="Inspect display mode to pass through.",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write manifest and invocation without running Inspect.",
    )

    review_parser = subparsers.add_parser(
        "review",
        help="Review existing trusted Inspect run directories.",
    )
    review_parser.add_argument(
        "run_dirs",
        nargs="*",
        help="Run directories to summarize. Defaults to recent runs.",
    )
    review_parser.add_argument(
        "--output-root",
        default="tools/inspect_ai/outputs/runs",
        help="Directory containing trusted run directories.",
    )
    review_parser.add_argument(
        "--recent",
        type=int,
        default=None,
        help="Summarize this many recent runs when no run directories are given.",
    )

    timeline_parser = subparsers.add_parser(
        "timeline",
        help="Print a command timeline for one trusted Inspect run directory.",
    )
    timeline_parser.add_argument("run_dir", help="Run directory to inspect.")

    args = parser.parse_args(argv)
    if args.command == "evaluate":
        return _evaluate(
            path=args.path,
            as_json=args.json,
            expected_state_profile=args.expected_state_profile,
        )
    if args.command == "run":
        return run_spec(
            args.spec,
            output_root=args.output_root,
            dry_run=args.dry_run,
            display=args.display,
        )
    if args.command == "review":
        return review_runs(
            args.run_dirs,
            output_root=args.output_root,
            recent=args.recent,
        )
    if args.command == "timeline":
        return print_timeline(args.run_dir)
    parser.error(f"unknown command: {args.command}")
    return 2


def _evaluate(
    *,
    path: str,
    as_json: bool,
    expected_state_profile: str | None,
) -> int:
    events = load_events_jsonl(path)
    report = evaluate_command_events(
        events,
        expected_state_profile=expected_state_profile,
    )
    if as_json:
        print(
            json.dumps(
                {
                    "score": report.score,
                    "command_count": report.command_count,
                    "findings": [
                        finding.to_dict() for finding in report.findings
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"score: {report.score}")
        print(f"commands: {report.command_count}")
        print(f"findings: {len(report.findings)}")
        for finding in report.findings:
            command = f" command={finding.command_id}" if finding.command_id else ""
            print(f"- {finding.severity} {finding.finding_type}{command}")
            print(f"  {finding.message}")
            if finding.evidence:
                print(f"  evidence: {finding.evidence}")
    return 1 if report.findings else 0
