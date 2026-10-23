"""Offline evidence joins and Langfuse score publication; never a runtime hook."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from dreamland.evaluation_json import (
    PARSER_SOURCE_PATH,
    POLICY_VERSION,
    SCORE_NAME,
    SUPPORTED_RUNTIME_SOURCE_SHA256,
    JsonAssessment,
    assess_json_response,
)

LOGGER = logging.getLogger("dreamland.evaluation.annotations")
SCHEMA_VERSION = 1
ROOT_NAME = "dreamland-command-agent"
JsonRequest = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


@dataclass(frozen=True)
class RuntimeEvidence:
    revision: str
    parser_sha256: str | None
    parser_verified: bool
    source_sha256: dict[str, str] = field(default_factory=dict)


def runtime_evidence(repository: Path, revision: str) -> RuntimeEvidence:
    """Verify source compatibility, not that a remote server loaded that source."""
    if revision == "unknown":
        return RuntimeEvidence(revision, None, False)
    if not revision or revision.startswith("-"):
        raise ValueError("runtime revision must be a Git revision or 'unknown'")
    try:
        resolved = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    revision + "^{commit}",
                ],
                check=True,
                capture_output=True,
                timeout=10,
            )
            .stdout.decode()
            .strip()
        )
        source_sha256 = {}
        for path in SUPPORTED_RUNTIME_SOURCE_SHA256:
            source = subprocess.run(
                ["git", "-C", str(repository), "show", f"{resolved}:{path}"],
                check=True,
                capture_output=True,
                timeout=10,
            ).stdout
            source_sha256[path] = hashlib.sha256(source).hexdigest()
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError("cannot resolve the runtime revision's parser source") from exc
    verified = all(
        source_sha256[path] in supported
        for path, supported in SUPPORTED_RUNTIME_SOURCE_SHA256.items()
    )
    return RuntimeEvidence(
        resolved, source_sha256[PARSER_SOURCE_PATH], verified, source_sha256
    )


def _json_field(value: object) -> object:
    """The v2 API JSON-encodes I/O; MCP/export inputs may already be decoded."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            return value
    return value


def _field(observation: Mapping[str, Any], camel: str, snake: str) -> Any:
    return observation.get(camel, observation.get(snake))


def _raw_output(value: object) -> str | None:
    # Decode the storage representation once, then match emulator message text
    # extraction. Do not json.loads the assistant's content itself.
    original = value
    value = _json_field(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if {"stdout", "stderr", "exit_status", "state_update"} <= value.keys():
            # Preserve an exported raw JSON string byte-for-byte. An already
            # decoded object cannot establish the original serialization.
            return original if isinstance(original, str) else None
        elif "content" in value:
            value = value["content"]
        else:
            return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts) if parts else None
    return None


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat()


def _is_descendant(
    observation: Mapping[str, Any],
    root_id: str,
    by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    parent = _field(observation, "parentObservationId", "parent_observation_id")
    visited: set[str] = set()
    while isinstance(parent, str) and parent and parent not in visited:
        if parent == root_id:
            return True
        visited.add(parent)
        parent = _field(
            by_id.get(parent, {}), "parentObservationId", "parent_observation_id"
        )
    return False


def build_report(
    observations: Sequence[Mapping[str, Any]],
    *,
    runtime: RuntimeEvidence,
    annotations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a reviewable score plan without I/O or changes to source evidence."""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    invalid_rows = 0
    for observation in observations:
        trace = _field(observation, "traceId", "trace_id")
        if not isinstance(trace, str) or not trace:
            invalid_rows += 1
            continue
        groups[trace].append(observation)
    coverage: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    for trace_id, group in sorted(groups.items()):
        roots = [o for o in group if o.get("name") == ROOT_NAME]
        if not roots:
            # Do not silently lose generations whose parent has not ingested yet.
            excluded_agent = any(
                o.get("name")
                in {
                    "dreamland-spiritguide-agent",
                    "dreamland-enrichment-agent",
                    "dreamland-enrichment-proposal-graph",
                    "dreamland-enrichment-integrator-refresh-graph",
                }
                for o in group
            )
            has_generation = not excluded_agent and any(
                o.get("type") == "GENERATION" for o in group
            )
            coverage.append(
                {
                    "trace_id": trace_id,
                    "status": "unknown" if has_generation else "out_of_scope",
                    "reason": "no_emulator_root",
                }
            )
            continue
        row: dict[str, Any] = {"trace_id": trace_id, "status": "unknown"}
        coverage.append(row)
        ids = [o.get("id") for o in group]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(
            ids
        ):
            row["reason"] = "missing_or_duplicate_observation_id"
            continue
        if len(roots) != 1:
            row["reason"] = "ambiguous_emulator_roots"
            continue
        root = roots[0]
        root_id = root["id"]
        row["root_observation_id"] = root_id
        by_id = {o["id"]: o for o in group}
        generations = [o for o in group if o.get("type") == "GENERATION"]
        if not generations:
            builtin = any(
                o.get("name") == "handle_builtin" and _is_descendant(o, root_id, by_id)
                for o in group
            )
            row.update(
                status="out_of_scope" if builtin else "unknown",
                reason="builtin" if builtin else "missing_generation",
            )
            continue
        if len(generations) != 1 or not _is_descendant(generations[0], root_id, by_id):
            row["reason"] = "ambiguous_generation_link"
            continue
        generation = generations[0]
        row["generation_id"] = generation["id"]
        if not all(
            _timestamp(_field(o, "endTime", "end_time")) for o in (root, generation)
        ):
            row["reason"] = "unfinished_observation"
            continue
        timestamp = _timestamp(_field(root, "startTime", "start_time"))
        if timestamp is None:
            row["reason"] = "missing_original_timestamp"
            continue
        if any(o.get("level") == "ERROR" for o in (root, generation)):
            row["reason"] = "execution_error"
            continue
        output = _json_field(root.get("output"))
        result = output.get("result") if isinstance(output, dict) else None
        if not isinstance(result, dict):
            row["reason"] = "missing_processed_result"
            continue
        raw = _raw_output(generation.get("output"))
        raw_source = "generation_output" if raw is not None else "unavailable"
        metadata = result.get("metadata")
        if raw is None and isinstance(metadata, dict):
            recorded_raw = metadata.get("dreamland.structured.raw_response")
            if isinstance(recorded_raw, str):
                raw = recorded_raw
                raw_source = "result_metadata"
        assessment: JsonAssessment = assess_json_response(
            raw,
            result,
            parser_verified=runtime.parser_verified,
        )
        row.update(asdict(assessment))
        if assessment.status not in {"repaired", "rejected"}:
            continue
        evidence = {
            "trace_id": trace_id,
            "root_observation_id": root_id,
            "generation_id": generation["id"],
            "raw_source": raw_source,
            "raw_sha256": hashlib.sha256(raw.encode()).hexdigest()
            if raw is not None
            else None,
            "processed_result_sha256": _digest(result),
        }
        # Independent metric/policy identities preserve other evaluators' scores.
        score_id = _digest([SCORE_NAME, POLICY_VERSION, trace_id, root_id])
        body: dict[str, Any] = {
            "id": score_id,
            "traceId": trace_id,
            "name": SCORE_NAME,
            "value": assessment.status,
            "dataType": "CATEGORICAL",
            "comment": assessment.reason,
            "metadata": {
                "dreamland.evaluation": {
                    "schema_version": SCHEMA_VERSION,
                    "metric": "json_status",
                    "policy_version": POLICY_VERSION,
                    "runtime": asdict(runtime),
                    "events": list(assessment.events),
                    "evidence": evidence,
                    "annotations": dict(annotations or {}),
                }
            },
        }
        if isinstance(root.get("environment"), str) and root["environment"]:
            body["environment"] = root["environment"]
        scores.append({"timestamp": timestamp, "body": body})
    counts = dict(Counter(row["status"] for row in coverage))
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "runtime": asdict(runtime),
        "mode": "dry_run",
        "counts": counts,
        "invalid_observation_rows": invalid_rows,
        "coverage": coverage,
        "scores": scores,
    }


def fetch_session_observations(
    request: JsonRequest,
    session_ids: Sequence[str],
    *,
    max_pages: int = 1000,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for session_id in dict.fromkeys(session_ids):
        if not session_id:
            raise ValueError("session IDs must be nonempty")
        session_start = len(observations)
        cursor = None
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            params = {
                "fields": "core,basic,io,metadata",
                "limit": "1000",
                "filter": json.dumps(
                    [
                        {
                            "type": "string",
                            "column": "sessionId",
                            "operator": "=",
                            "value": session_id,
                        }
                    ]
                ),
            }
            if cursor:
                params["cursor"] = cursor
            response = request(
                "GET", "/api/public/v2/observations?" + urlencode(params), None
            )
            data, meta = response.get("data"), response.get("meta")
            if not isinstance(data, list) or not all(isinstance(o, dict) for o in data):
                raise ValueError("invalid Langfuse observation page")
            if not isinstance(meta, dict):
                raise ValueError("missing Langfuse pagination metadata")
            observations.extend(data)
            cursor = meta.get("cursor")
            if cursor is None or cursor == "":
                break
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise ValueError("invalid or repeated Langfuse pagination cursor")
            seen_cursors.add(cursor)
        else:
            raise ValueError("Langfuse observation pagination exceeded the limit")
        if len(observations) == session_start:
            raise ValueError(
                "a requested session has no available Langfuse observations"
            )
        LOGGER.info(
            "session_observations_read session=%s count=%s",
            session_id,
            len(observations) - session_start,
        )
    return observations


def publish_scores(
    request: JsonRequest, report: Mapping[str, Any]
) -> list[dict[str, str]]:
    """Send only score-create events; acknowledge every item, not just HTTP 207."""
    outcomes = []
    for score in report["scores"]:
        event_id = uuid4().hex
        event = {"id": event_id, "type": "score-create", **score}
        score_id = score["body"]["id"]
        try:
            response = request("POST", "/api/public/ingestion", {"batch": [event]})
            successes = response.get("successes", [])
            accepted = isinstance(successes, list) and any(
                isinstance(s, dict)
                and s.get("id") == event_id
                and isinstance(s.get("status"), int)
                and 200 <= s["status"] < 300
                for s in successes
            )
            if response.get("errors") or not accepted:
                raise ValueError("Langfuse did not acknowledge the score event")
        except (ValueError, OSError) as exc:
            LOGGER.warning(
                "score_ingestion_failed score=%s error_type=%s",
                score_id,
                type(exc).__name__,
            )
            outcomes.append({"score_id": score_id, "status": "failed"})
        else:
            LOGGER.info("score_ingestion_accepted score=%s", score_id)
            outcomes.append({"score_id": score_id, "status": "accepted_for_ingestion"})
    return outcomes


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class LangfuseHttpClient:
    """Public REST calls from the evaluator process, with no tracing exporter."""

    def __init__(
        self, base_url: str, public_key: str, secret_key: str, *, timeout: float = 30
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Langfuse URL must be an HTTP(S) URL without credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Langfuse URL cannot contain a query or fragment")
        if not public_key or not secret_key:
            raise ValueError("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._authorization = (
            "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        )
        self._opener = build_opener(_NoRedirects())

    def request(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        data = json.dumps(body, allow_nan=False).encode() if body is not None else None
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": self._authorization,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "dreamland-evaluation-annotations/1",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                decoded = json.load(response)
        except HTTPError as exc:
            raise OSError(f"Langfuse HTTP request failed (status {exc.code})") from None
        except (URLError, TimeoutError) as exc:
            raise OSError("Langfuse request could not complete") from exc
        if not isinstance(decoded, dict):
            raise ValueError("Langfuse returned a non-object response")
        return decoded
