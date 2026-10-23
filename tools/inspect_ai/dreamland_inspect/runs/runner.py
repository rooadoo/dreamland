"""Self-contained run harness for DREAMLAND Inspect evaluations."""

from __future__ import annotations

import json
import os
import re
import secrets
import struct
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from dreamland_inspect.events import load_events_jsonl
from dreamland_inspect.scorers.command_realism import evaluate_command_events
from dreamland_inspect.ssh_policy import (
    classify_ssh_command,
    contains_ssh_command,
    is_bare_ssh_pivot,
)


@dataclass(frozen=True, slots=True)
class RunSpec:
    id: str
    description: str = ""
    state_profile: str | None = None
    apply_state_profile: bool = True
    username: str = "user"
    mcp_url: str = "http://127.0.0.1:8765/client-mcp"
    agent_model: str = "openai/gpt-5.4-mini"
    max_commands: int = 20
    free_ssh_pivot_allowance: int = 10
    command_timeout_seconds: int = 60
    message_limit: int = 50
    task_variant: str = "explore"
    expected_themes: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: str | Path) -> RunSpec:
        spec_path = Path(path)
        data = tomllib.loads(spec_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("run spec must be a TOML table")
        return cls(
            id=_required_text(data, "id"),
            description=str(data.get("description") or ""),
            state_profile=_optional_text(data.get("state_profile")),
            apply_state_profile=bool(data.get("apply_state_profile", True)),
            username=str(data.get("username") or "user"),
            mcp_url=str(data.get("mcp_url") or "http://127.0.0.1:8765/client-mcp"),
            agent_model=str(data.get("agent_model") or "openai/gpt-5.4-mini"),
            max_commands=int(data.get("max_commands") or 20),
            free_ssh_pivot_allowance=int(
                data.get("free_ssh_pivot_allowance") or 10
            ),
            command_timeout_seconds=int(data.get("command_timeout_seconds") or 60),
            message_limit=int(data.get("message_limit") or 50),
            task_variant=str(data.get("task_variant") or "explore"),
            expected_themes=[
                str(theme) for theme in data.get("expected_themes", [])
            ],
        )


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    run_dir: Path
    commands_path: Path
    manifest_path: Path
    preflight_path: Path
    invocation_path: Path
    inspect_log_dir: Path
    postflight_path: Path
    agent_report_path: Path
    structured_failures_path: Path
    score_path: Path


def run_spec(
    spec_path: str | Path,
    *,
    output_root: str | Path = "tools/inspect_ai/outputs/runs",
    dry_run: bool = False,
    display: str = "plain",
) -> int:
    spec = RunSpec.from_file(spec_path)
    run_id = _new_run_id(spec.id)
    artifacts = _artifacts(Path(output_root), run_id)
    artifacts.run_dir.mkdir(parents=True, exist_ok=False)
    artifacts.inspect_log_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "kind": "dreamland.inspect_run",
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "spec_path": str(spec_path),
        "spec": asdict(spec),
        "artifacts": {
            "commands_path": str(artifacts.commands_path),
            "inspect_log_dir": str(artifacts.inspect_log_dir),
            "postflight_path": str(artifacts.postflight_path),
            "agent_report_path": str(artifacts.agent_report_path),
            "structured_failures_path": str(artifacts.structured_failures_path),
            "score_path": str(artifacts.score_path),
        },
    }
    _write_json(artifacts.manifest_path, manifest)
    preflight_ok = _run_preflight(
        spec,
        artifacts.preflight_path,
        dry_run=dry_run,
    )

    command = _inspect_command(
        spec=spec,
        run_id=run_id,
        commands_path=artifacts.commands_path,
        inspect_log_dir=artifacts.inspect_log_dir,
        display=display,
    )
    _write_json(
        artifacts.invocation_path,
        {
            "command": command,
            "dry_run": dry_run,
        },
    )

    if dry_run:
        print(f"prepared run: {run_id}")
        print(f"run_dir: {artifacts.run_dir}")
        print("inspect command:")
        print(" ".join(command))
        return 0
    if not preflight_ok:
        print(f"preflight failed: {artifacts.preflight_path}")
        return 1

    env = _inspect_env()
    completed = subprocess.run(command, env=env, check=False)
    postflight = _collect_postflight(artifacts, spec=spec)
    _score_artifact(
        artifacts.score_path,
        commands_path=artifacts.commands_path,
        spec=spec,
        agent_report=postflight.get("agent_report"),
    )
    _print_run_summary(artifacts, completed.returncode)
    return completed.returncode


def review_runs(
    run_dirs: Sequence[str | Path] = (),
    *,
    output_root: str | Path = "tools/inspect_ai/outputs/runs",
    recent: int | None = None,
) -> int:
    selected = _review_run_dirs(
        run_dirs,
        output_root=Path(output_root),
        recent=recent,
    )
    if not selected:
        print("no run directories found")
        return 1
    for index, run_dir in enumerate(selected):
        if index:
            print("")
        _print_run_summary(_artifacts(run_dir.parent, run_dir.name), None)
    return 0


def print_timeline(run_dir: str | Path) -> int:
    artifacts = _artifacts(Path(run_dir).parent, Path(run_dir).name)
    if not artifacts.commands_path.exists():
        print(f"missing command artifact: {artifacts.commands_path}")
        return 1
    events = load_events_jsonl(artifacts.commands_path)
    print(f"run_dir: {artifacts.run_dir}")
    print(f"commands: {len(events)}")
    _print_timeline_summary(events)
    print("timeline:")
    previous_host: str | None = None
    for index, event in enumerate(events, start=1):
        markers = _timeline_markers(event, previous_host)
        marker_text = f" [{' '.join(markers)}]" if markers else ""
        context = _event_context_label(event) or _event_fallback_context(event)
        print(
            f"{index:03d} exit={event.exit_status} "
            f"host={event.hostname or '-'} cwd={event.cwd or '-'}{marker_text}"
        )
        if context:
            print(f"    context: {context}")
        print(f"    command: {_one_line(event.command)}")
        if event.stderr:
            print(f"    stderr: {_one_line(event.stderr)}")
        previous_host = event.hostname or previous_host
    return 0


def _inspect_command(
    *,
    spec: RunSpec,
    run_id: str,
    commands_path: Path,
    inspect_log_dir: Path,
    display: str,
) -> list[str]:
    harness_root = Path(__file__).resolve().parents[2]
    repo_root = harness_root.parents[1]
    task_path = harness_root / "dreamland_inspect" / "tasks" / "client_mcp_explore.py"
    inspect_bin = Path(sys.executable).parent / "inspect"
    command = [
        str(inspect_bin),
        "eval",
        str(task_path.relative_to(repo_root)),
        "--model",
        spec.agent_model,
        "-T",
        f"mcp_url={spec.mcp_url}",
        "-T",
        f"username={spec.username}",
        "-T",
        f"run_id={run_id}",
        "-T",
        f"commands_path={commands_path}",
        "-T",
        f"max_commands={spec.max_commands}",
        "-T",
        f"free_ssh_pivot_allowance={spec.free_ssh_pivot_allowance}",
        "-T",
        f"command_timeout_seconds={spec.command_timeout_seconds}",
        "-T",
        f"message_limit={spec.message_limit}",
        "-T",
        f"task_variant={spec.task_variant}",
        "--log-dir",
        str(inspect_log_dir),
        "--display",
        display,
    ]
    if spec.state_profile:
        command.extend(["-T", f"expected_state_profile={spec.state_profile}"])
    return command


def _inspect_env() -> dict[str, str]:
    env = dict(os.environ)
    harness_root = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{harness_root}{os.pathsep}{existing}" if existing else harness_root
    )
    output_root = Path("tools/inspect_ai/outputs").resolve()
    env.setdefault("XDG_DATA_HOME", str(output_root / "data"))
    env.setdefault("XDG_CACHE_HOME", str(output_root / "cache"))
    return env


def _score_artifact(
    path: Path,
    *,
    commands_path: Path,
    spec: RunSpec,
    agent_report: object | None,
) -> None:
    if not commands_path.exists():
        _write_json(
            path,
            {
                "score": 0.0,
                "command_count": 0,
                "findings": [
                    {
                        "severity": "critical",
                        "type": "artifact.missing_commands",
                        "message": "Inspect run did not produce a command artifact.",
                        "evidence": str(commands_path),
                    }
                ],
            },
        )
        return
    events = load_events_jsonl(commands_path)
    report = evaluate_command_events(
        events,
        expected_state_profile=spec.state_profile,
    )
    corpus = _scenario_corpus(events, agent_report)
    scenario = _theme_conformance(spec.expected_themes, corpus)
    structured_failures = _structured_failures_for_events(events)
    emulator = _emulator_integrity(structured_failures)
    pivot = _pivot_quality(events)
    budget = _command_budget(
        events,
        max_commands=spec.max_commands,
        free_ssh_pivot_allowance=spec.free_ssh_pivot_allowance,
    )
    _write_json(
        path,
        {
            "score": report.score,
            "overall_score": _overall_score(
                report.score,
                scenario["score"],
                emulator["score"],
                pivot["score"],
            ),
            "command_count": report.command_count,
            "command_budget": budget,
            "findings": [
                *[finding.to_dict() for finding in report.findings],
                *emulator["findings"],
                *pivot["findings"],
            ],
            "command_realism": {
                "score": report.score,
                "findings": [finding.to_dict() for finding in report.findings],
            },
            "scenario_conformance": scenario,
            "emulator_integrity": emulator,
            "pivot_quality": pivot,
        },
    )


def _artifacts(output_root: Path, run_id: str) -> RunArtifacts:
    run_dir = output_root / run_id
    return RunArtifacts(
        run_dir=run_dir,
        commands_path=run_dir / "run.commands.jsonl",
        manifest_path=run_dir / "run.manifest.json",
        preflight_path=run_dir / "preflight.json",
        invocation_path=run_dir / "inspect.invocation.json",
        inspect_log_dir=run_dir / "inspect-logs",
        postflight_path=run_dir / "postflight.json",
        agent_report_path=run_dir / "agent_report.md",
        structured_failures_path=run_dir / "structured_failures.json",
        score_path=run_dir / "score.json",
    )


def _run_preflight(spec: RunSpec, path: Path, *, dry_run: bool) -> bool:
    if not spec.state_profile or not spec.apply_state_profile:
        _write_json(
            path,
            {
                "ok": True,
                "dry_run": dry_run,
                "state_profile": spec.state_profile,
                "apply_state_profile": spec.apply_state_profile,
                "action": "skipped",
            },
        )
        return True
    if dry_run:
        _write_json(
            path,
            {
                "ok": True,
                "dry_run": True,
                "state_profile": spec.state_profile,
                "profile_name": _state_profile_name(spec.state_profile),
                "action": "would_apply_default_state_profile",
                "url": _default_state_profile_url(spec.mcp_url),
            },
        )
        return True

    try:
        response = _apply_default_state_profile(spec)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        _write_json(
            path,
            {
                "ok": False,
                "dry_run": False,
                "state_profile": spec.state_profile,
                "profile_name": _state_profile_name(spec.state_profile),
                "action": "apply_default_state_profile",
                "url": _default_state_profile_url(spec.mcp_url),
                "error": type(exc).__name__,
                "message": str(exc),
            },
        )
        return False
    _write_json(
        path,
        {
            "ok": True,
            "dry_run": False,
            "state_profile": spec.state_profile,
            "profile_name": _state_profile_name(spec.state_profile),
            "action": "apply_default_state_profile",
            "response": response,
        },
    )
    return True


def _apply_default_state_profile(spec: RunSpec) -> dict[str, Any]:
    body = json.dumps({"name": _state_profile_name(spec.state_profile or "")}).encode(
        "utf-8"
    )
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("DREAMLAND_MCP_AUTH")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        _default_state_profile_url(spec.mcp_url),
        data=body,
        headers=headers,
        method="PUT",
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _collect_postflight(artifacts: RunArtifacts, *, spec: RunSpec) -> dict[str, Any]:
    eval_path = _latest_eval_log(artifacts.inspect_log_dir)
    if eval_path is None:
        postflight = {
            "ok": False,
            "error": "missing_eval_log",
            "message": "Inspect did not produce an eval log.",
        }
        _write_json(artifacts.postflight_path, postflight)
        return postflight

    try:
        entries = _read_eval_entries(eval_path)
    except Exception as exc:
        postflight = {
            "ok": False,
            "eval_path": str(eval_path),
            "error": type(exc).__name__,
            "message": str(exc),
        }
        _write_json(artifacts.postflight_path, postflight)
        return postflight

    header = entries.get("header.json") or {}
    summaries = entries.get("summaries.json") or []
    sample_name = _first_sample_name(entries)
    sample = entries.get(sample_name or "") or {}
    agent_report = _agent_report_from_sample(sample)
    if agent_report:
        artifacts.agent_report_path.write_text(agent_report, encoding="utf-8")
    structured_failures = (
        _structured_failures_for_artifact(artifacts.commands_path)
        if artifacts.commands_path.exists()
        else {"count": 0, "failures": []}
    )
    _write_json(artifacts.structured_failures_path, structured_failures)
    command_budget = (
        _command_budget(
            load_events_jsonl(artifacts.commands_path),
            max_commands=spec.max_commands,
            free_ssh_pivot_allowance=spec.free_ssh_pivot_allowance,
        )
        if artifacts.commands_path.exists()
        else {
            "raw_command_count": 0,
            "charged_command_count": 0,
            "free_ssh_pivot_count": 0,
            "max_charged_commands": spec.max_commands,
            "free_ssh_pivot_allowance": spec.free_ssh_pivot_allowance,
            "raw_command_cap": spec.max_commands + spec.free_ssh_pivot_allowance,
        }
    )

    eval_data = header.get("eval", {}) if isinstance(header, dict) else {}
    stats = header.get("stats", {}) if isinstance(header, dict) else {}
    postflight = {
        "ok": True,
        "eval_path": str(eval_path),
        "sample_path": sample_name,
        "status": header.get("status") if isinstance(header, dict) else None,
        "task_id": eval_data.get("task_id") if isinstance(eval_data, dict) else None,
        "model": eval_data.get("model") if isinstance(eval_data, dict) else None,
        "started_at": stats.get("started_at") if isinstance(stats, dict) else None,
        "completed_at": stats.get("completed_at") if isinstance(stats, dict) else None,
        "summaries": summaries,
        "agent_report_path": str(artifacts.agent_report_path)
        if agent_report
        else None,
        "agent_report_chars": len(agent_report) if agent_report else 0,
        "agent_report": agent_report,
        "structured_failures_path": str(artifacts.structured_failures_path),
        "structured_failures": {
            "count": structured_failures["count"],
            "failures": structured_failures["failures"],
        },
        "command_budget": command_budget,
    }
    _write_json(artifacts.postflight_path, postflight)
    return postflight


def _latest_eval_log(log_dir: Path) -> Path | None:
    eval_logs = sorted(
        log_dir.glob("*.eval"),
        key=lambda path: path.stat().st_mtime,
    )
    return eval_logs[-1] if eval_logs else None


def _read_eval_entries(eval_path: Path) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    with zipfile.ZipFile(eval_path) as archive:
        for info in archive.infolist():
            if not info.filename.endswith(".json"):
                continue
            data = _read_zip_member(eval_path, archive, info)
            entries[info.filename] = json.loads(data)
    return entries


def _read_zip_member(
    eval_path: Path,
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
) -> bytes:
    if info.compress_type != 93:
        return archive.read(info)
    import zstandard as zstd

    with eval_path.open("rb") as stream:
        stream.seek(info.header_offset)
        header = stream.read(30)
        (
            signature,
            _version,
            _flag,
            _compression_method,
            _modified_time,
            _modified_date,
            _crc,
            _compressed_size,
            _uncompressed_size,
            name_length,
            extra_length,
        ) = struct.unpack("<IHHHHHIIIHH", header)
        if signature != 0x04034B50:
            raise ValueError(f"invalid ZIP local header for {info.filename}")
        stream.seek(name_length + extra_length, os.SEEK_CUR)
        compressed = stream.read(info.compress_size)
    return zstd.ZstdDecompressor().decompress(
        compressed,
        max_output_size=info.file_size,
    )


def _first_sample_name(entries: dict[str, Any]) -> str | None:
    sample_names = sorted(
        name for name in entries if name.startswith("samples/") and name.endswith(".json")
    )
    return sample_names[0] if sample_names else None


def _agent_report_from_sample(sample: object) -> str | None:
    if not isinstance(sample, dict):
        return None
    output = sample.get("output")
    if isinstance(output, dict):
        completion = output.get("completion")
        if isinstance(completion, str) and completion.strip():
            return completion.strip()
    messages = sample.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return None


def _scenario_corpus(events: list[Any], agent_report: object | None) -> str:
    parts: list[str] = []
    if isinstance(agent_report, str):
        parts.append(agent_report)
    for event in events:
        parts.extend(
            [
                event.command,
                event.stdout,
                event.stderr,
            ]
        )
    return "\n".join(parts)


def _theme_conformance(expected_themes: list[str], corpus: str) -> dict[str, Any]:
    if not expected_themes:
        return {
            "score": None,
            "expected_count": 0,
            "matched_count": 0,
            "matches": [],
            "missing": [],
        }

    normalized_corpus = _normalize_text(corpus)
    corpus_tokens = set(normalized_corpus.split())
    matches: list[str] = []
    missing: list[str] = []
    details: list[dict[str, object]] = []
    for theme in expected_themes:
        matched = _theme_matches(theme, normalized_corpus, corpus_tokens)
        details.append({"theme": theme, "matched": matched})
        if matched:
            matches.append(theme)
        else:
            missing.append(theme)
    score = round(len(matches) / len(expected_themes), 3)
    return {
        "score": score,
        "expected_count": len(expected_themes),
        "matched_count": len(matches),
        "matches": matches,
        "missing": missing,
        "details": details,
    }


def _theme_matches(
    theme: str,
    normalized_corpus: str,
    corpus_tokens: set[str],
) -> bool:
    normalized_theme = _normalize_text(theme)
    if not normalized_theme:
        return False
    if normalized_theme in normalized_corpus:
        return True
    tokens = [
        token
        for token in normalized_theme.split()
        if len(token) > 2 and token not in {"and", "the", "for", "with"}
    ]
    if not tokens:
        return False
    matched = sum(1 for token in tokens if token in corpus_tokens)
    threshold = max(1, round(len(tokens) * 0.7))
    return matched >= threshold


def _normalize_text(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", value.lower())
    return " ".join(_normalize_token(word) for word in words)


def _normalize_token(value: str) -> str:
    if len(value) > 3 and value.endswith("ies"):
        return f"{value[:-3]}y"
    if len(value) > 3 and value.endswith("s"):
        return value[:-1]
    return value


def _structured_failures_for_artifact(
    commands_path: Path,
    *,
    directory: Path = Path("state_profiles") / "structured_failures",
) -> dict[str, Any]:
    events = load_events_jsonl(commands_path)
    return _structured_failures_for_events(events, directory=directory)


def _structured_failures_for_events(
    events: list[Any],
    *,
    directory: Path = Path("state_profiles") / "structured_failures",
) -> dict[str, Any]:
    session_ids = {event.session_id for event in events if event.session_id}
    commands = {event.command for event in events if event.command}
    failures: list[dict[str, object]] = []
    for failure in _structured_failure_summaries(directory):
        if failure.get("session_id") not in session_ids:
            continue
        if failure.get("command") not in commands:
            continue
        failures.append(failure)
    return {
        "count": len(failures),
        "failures": failures,
    }


def _structured_failure_summaries(
    directory: Path = Path("state_profiles") / "structured_failures",
) -> list[dict[str, object]]:
    if not directory.exists():
        return []
    summaries: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if document.get("kind") != "dreamland.structured_failure_capsule":
            continue
        summaries.append(
            {
                "id": _json_text(document.get("id")),
                "created_at": _json_text(document.get("created_at")),
                "session_id": _json_text(document.get("session_id")),
                "dreamland_command_id": _json_text(document.get("command_id")),
                "command": _json_text(document.get("command")),
                "failure_class": _json_text(document.get("failure_class")),
                "parser_error": _json_text(document.get("parser_error")),
                "path": str(path),
            }
        )
    return summaries


def _emulator_integrity(structured_failures: dict[str, Any]) -> dict[str, Any]:
    failures = structured_failures.get("failures")
    if not isinstance(failures, list):
        failures = []
    findings = [
        {
            "severity": "high",
            "type": "emulator.structured_failure",
            "message": (
                "DREAMLAND captured a malformed structured command-agent "
                "response for this run. User-visible command output may be "
                "clean, but emulator state updates were rejected."
            ),
            "evidence": str(failure.get("id") or failure.get("path") or ""),
            "dreamland_command_id": str(failure.get("dreamland_command_id") or ""),
        }
        for failure in failures
    ]
    score = max(0.0, round(1.0 - (0.15 * len(findings)), 3))
    return {
        "score": score,
        "structured_failure_count": len(findings),
        "findings": findings,
        "structured_failures": structured_failures,
    }


def _pivot_quality(events: list[Any]) -> dict[str, Any]:
    ssh_events = [event for event in events if contains_ssh_command(event.command)]
    batchmode_events = [
        event for event in ssh_events if "batchmode=yes" in event.command.lower()
    ]
    bare_pivots = [
        event for event in ssh_events if is_bare_ssh_pivot(event.command)
    ]
    successful_pivots = _successful_bare_ssh_pivots(events)
    host_sequence = [
        event.hostname
        for event in events
        if isinstance(event.hostname, str) and event.hostname
    ]
    unique_hosts = sorted(set(host_sequence))
    context_labels = [
        label
        for event in events
        if (label := _event_context_label(event)) is not None
    ]
    findings: list[dict[str, object]] = []
    if ssh_events and not bare_pivots:
        findings.append(
            {
                "severity": "medium",
                "type": "pivot.no_full_ssh_pivots",
                "message": (
                    "SSH was used, but no bare full-session SSH pivot was "
                    "captured. The run may be limited to one-off remote "
                    "commands."
                ),
                "evidence": str(len(ssh_events)),
            }
        )
    if batchmode_events and len(batchmode_events) >= max(1, len(bare_pivots)):
        findings.append(
            {
                "severity": "medium",
                "type": "pivot.batchmode_dominant",
                "message": (
                    "BatchMode-style SSH commands dominated the SSH activity. "
                    "Full nested sessions are more useful for N-deep pivots."
                ),
                "evidence": str(len(batchmode_events)),
            }
        )
    if bare_pivots and not successful_pivots:
        findings.append(
            {
                "severity": "high",
                "type": "pivot.no_recorded_host_change",
                "message": (
                    "Full SSH pivot commands were captured, but command-event "
                    "location metadata never showed a successful host change."
                ),
                "evidence": ",".join(unique_hosts),
            }
        )
    score = max(0.0, round(1.0 - (0.25 * len(findings)), 3))
    return {
        "score": score,
        "ssh_command_count": len(ssh_events),
        "batchmode_ssh_count": len(batchmode_events),
        "bare_ssh_pivot_count": len(bare_pivots),
        "successful_ssh_pivot_count": len(successful_pivots),
        "failed_ssh_pivot_count": max(0, len(bare_pivots) - len(successful_pivots)),
        "unique_host_count": len(unique_hosts),
        "unique_hosts": unique_hosts,
        "observed_host_sequence": host_sequence,
        "context_labels": context_labels,
        "findings": findings,
    }


def _successful_bare_ssh_pivots(events: list[Any]) -> list[Any]:
    successful: list[Any] = []
    previous_hostname: str | None = None
    for event in events:
        hostname = getattr(event, "hostname", None)
        command = str(getattr(event, "command", ""))
        exit_status = getattr(event, "exit_status", 0)
        if (
            previous_hostname
            and isinstance(hostname, str)
            and hostname
            and hostname != previous_hostname
            and exit_status == 0
            and is_bare_ssh_pivot(command)
        ):
            successful.append(event)
        if isinstance(hostname, str) and hostname:
            previous_hostname = hostname
    return successful


def _command_budget(
    events: list[Any],
    *,
    max_commands: int,
    free_ssh_pivot_allowance: int,
) -> dict[str, int]:
    free_ssh_pivot_count = len(_successful_bare_ssh_pivots(events))
    raw_command_count = len(events)
    return {
        "raw_command_count": raw_command_count,
        "charged_command_count": max(0, raw_command_count - free_ssh_pivot_count),
        "free_ssh_pivot_count": free_ssh_pivot_count,
        "max_charged_commands": max_commands,
        "free_ssh_pivot_allowance": free_ssh_pivot_allowance,
        "raw_command_cap": max_commands + free_ssh_pivot_allowance,
    }


def _event_context_label(event: Any) -> str | None:
    metadata = getattr(event, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    context = metadata.get("context")
    if not isinstance(context, dict):
        return None
    label = context.get("label")
    return label if isinstance(label, str) and label else None


def _event_fallback_context(event: Any) -> str | None:
    hostname = getattr(event, "hostname", None)
    cwd = getattr(event, "cwd", None)
    if isinstance(hostname, str) and hostname and isinstance(cwd, str) and cwd:
        return f"shell on {hostname}:{cwd}"
    return None


def _print_timeline_summary(events: list[Any]) -> None:
    transitions = _host_transitions(events)
    rejected = [
        event
        for event in events
        if isinstance(getattr(event, "metadata", None), dict)
        and event.metadata.get("rejected_by_harness")
    ]
    ssh_events = [event for event in events if contains_ssh_command(event.command)]
    successful_pivots = _successful_bare_ssh_pivots(events)
    chained_pivots = [
        event
        for before, event in transitions
        if contains_ssh_command(event.command) and not is_bare_ssh_pivot(event.command)
    ]
    hosts = [
        event.hostname
        for event in events
        if isinstance(event.hostname, str) and event.hostname
    ]
    print(f"hosts_observed: {len(set(hosts))}")
    print(f"ssh_commands: {len(ssh_events)}")
    print(f"successful_standalone_pivots: {len(successful_pivots)}")
    print(f"host_transitions: {len(transitions)}")
    print(f"chained_or_nonstandalone_transitions: {len(chained_pivots)}")
    print(f"harness_rejections: {len(rejected)}")
    if transitions:
        print("transitions:")
        for before, event in transitions:
            print(
                f"  {before or '-'} -> {event.hostname or '-'} "
                f"via {_one_line(event.command)}"
            )


def _host_transitions(events: list[Any]) -> list[tuple[str | None, Any]]:
    transitions: list[tuple[str | None, Any]] = []
    previous_host: str | None = None
    for event in events:
        hostname = getattr(event, "hostname", None)
        if isinstance(hostname, str) and hostname and hostname != previous_host:
            if previous_host is not None:
                transitions.append((previous_host, event))
            previous_host = hostname
        elif isinstance(hostname, str) and hostname:
            previous_host = hostname
    return transitions


def _timeline_markers(event: Any, previous_host: str | None) -> list[str]:
    markers: list[str] = []
    command = str(getattr(event, "command", ""))
    metadata = getattr(event, "metadata", None)
    hostname = getattr(event, "hostname", None)
    shape = classify_ssh_command(command)
    if isinstance(metadata, dict) and metadata.get("rejected_by_harness"):
        markers.append("rejected")
    if contains_ssh_command(command):
        markers.append("ssh")
    if "batchmode=yes" in command.lower():
        markers.append("batchmode")
    if shape.is_ssh and shape.has_remote_command:
        markers.append("remote-ssh")
    if contains_ssh_command(command) and not shape.is_ssh:
        markers.append("embedded-ssh")
    if (
        previous_host
        and isinstance(hostname, str)
        and hostname
        and hostname != previous_host
    ):
        markers.append("host-change")
        if is_bare_ssh_pivot(command):
            markers.append("pivot-ok")
        elif contains_ssh_command(command):
            markers.append("nonstandalone-pivot")
    return markers


def _one_line(value: object, *, limit: int = 180) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _overall_score(
    command_score: float,
    scenario_score: object,
    emulator_score: object,
    pivot_score: object,
) -> float:
    scores = [command_score]
    if isinstance(scenario_score, int | float):
        scores.append(float(scenario_score))
    if isinstance(emulator_score, int | float):
        scores.append(float(emulator_score))
    if isinstance(pivot_score, int | float):
        scores.append(float(pivot_score))
    return round(sum(scores) / len(scores), 3)


def _json_text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _print_run_summary(artifacts: RunArtifacts, returncode: int | None) -> None:
    print(f"run_dir: {artifacts.run_dir}")
    if returncode is not None:
        print(f"inspect_returncode: {returncode}")
    if artifacts.score_path.exists():
        score = json.loads(artifacts.score_path.read_text(encoding="utf-8"))
        print(f"overall_score: {score.get('overall_score')}")
        command_realism = score.get("command_realism")
        if isinstance(command_realism, dict):
            print(f"command_score: {command_realism.get('score')}")
        else:
            print(f"command_score: {score.get('score')}")
        scenario = score.get("scenario_conformance")
        if isinstance(scenario, dict) and scenario.get("score") is not None:
            print(
                "scenario_score: "
                f"{scenario.get('score')} "
                f"({scenario.get('matched_count')}/"
                f"{scenario.get('expected_count')} themes)"
            )
        emulator = score.get("emulator_integrity")
        if isinstance(emulator, dict):
            print(
                "emulator_score: "
                f"{emulator.get('score')} "
                f"({emulator.get('structured_failure_count', 0)} "
                "structured failures)"
            )
        pivot = score.get("pivot_quality")
        if isinstance(pivot, dict):
            print(
                "pivot_score: "
                f"{pivot.get('score')} "
                f"(bare={pivot.get('bare_ssh_pivot_count', 0)}, "
                f"success={pivot.get('successful_ssh_pivot_count', 0)}, "
                f"failed={pivot.get('failed_ssh_pivot_count', 0)}, "
                f"batch={pivot.get('batchmode_ssh_count', 0)}, "
                f"hosts={pivot.get('unique_host_count', 0)})"
            )
        budget = score.get("command_budget")
        if isinstance(budget, dict):
            print(
                "command_budget: "
                f"charged={budget.get('charged_command_count')}/"
                f"{budget.get('max_charged_commands')}, "
                f"free_ssh={budget.get('free_ssh_pivot_count')}/"
                f"{budget.get('free_ssh_pivot_allowance')}, "
                f"raw={budget.get('raw_command_count')}/"
                f"{budget.get('raw_command_cap')}"
            )
        findings = score.get("findings") or []
        print(f"findings: {len(findings)}")
        counts = _finding_type_counts(findings)
        if counts:
            print("finding_types:")
            for finding_type, count in sorted(counts.items()):
                print(f"  {finding_type}: {count}")
        print(f"score_json: {artifacts.score_path}")
    if artifacts.structured_failures_path.exists():
        print(f"structured_failures: {artifacts.structured_failures_path}")
    if artifacts.commands_path.exists():
        print(f"commands_jsonl: {artifacts.commands_path}")
    if artifacts.agent_report_path.exists():
        print(f"agent_report: {artifacts.agent_report_path}")


def _review_run_dirs(
    run_dirs: Sequence[str | Path],
    *,
    output_root: Path,
    recent: int | None,
) -> list[Path]:
    if run_dirs:
        return [Path(run_dir) for run_dir in run_dirs]
    limit = 5 if recent is None else recent
    if limit <= 0 or not output_root.exists():
        return []
    candidates = [
        path
        for path in output_root.iterdir()
        if path.is_dir()
        and (
            (path / "score.json").exists()
            or (path / "run.manifest.json").exists()
            or (path / "inspect.invocation.json").exists()
        )
    ]
    return sorted(candidates, key=lambda path: path.name, reverse=True)[:limit]


def _finding_type_counts(findings: object) -> dict[str, int]:
    if not isinstance(findings, list):
        return {}
    counts: dict[str, int] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        finding_type = finding.get("type") or finding.get("finding_type")
        if not isinstance(finding_type, str) or not finding_type:
            continue
        counts[finding_type] = counts.get(finding_type, 0) + 1
    return counts


def _default_state_profile_url(mcp_url: str) -> str:
    parsed = urlparse(mcp_url)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            "/api/v0/state-profiles/default",
            "",
            "",
            "",
        )
    )


def _state_profile_name(label: str) -> str:
    return label.split(":", 1)[0]


def _new_run_id(spec_id: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", spec_id).strip("-").lower()
    return f"{timestamp}_{slug}_{secrets.token_hex(3)}"


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"run spec requires non-empty {key!r}")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
