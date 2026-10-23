from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Final

from dreamland.background_agents.authority import ForegroundAuthoritySnapshot
from dreamland.background_agents.enrichment.foreground_evidence import (
    capture_foreground_evidence,
)
from dreamland.background_agents.surfaces import infer_command_surface
from dreamland.emulator_context import (
    detailed_host_registry_refs_for_command_emulator,
    project_world_state_for_command_emulator,
)
from dreamland.world_state import active_host_ref

ENRICHMENT_CONTEXT_VERSION: Final[int] = 4
DEFAULT_RECENT_HISTORY_LIMIT: Final[int] = 4
RECENT_COMMAND_OUTPUT_CHAR_LIMIT: Final[int] = 6_000
RECENT_COMMAND_ENTRY_OUTPUT_CHAR_LIMIT: Final[int] = 1_500
_OUTPUT_TRIM_MARKER: Final[str] = "\n…output trimmed…\n"
_OMITTED_VALUE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "base64",
        "blob",
        "body",
        "bytes",
        "content",
        "data",
        "payload",
        "raw",
        "raw_content",
        "stderr",
        "stdout",
    }
)
_HISTORY_SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "id",
    "cwd",
    "command",
    "exit_status",
    "interrupted",
    "signal",
)


@dataclass(frozen=True, slots=True)
class EnrichmentContextProjection:
    """A bounded, immutable-by-convention prompt view of one world snapshot."""

    document: dict[str, object]
    trusted_target_host: dict[str, object] = field(repr=False, compare=False)
    target_host: str
    detailed_hosts: tuple[str, ...]
    full_world_sha256: str
    target_host_sha256: str
    projected_sha256: str
    full_world_chars: int
    projected_chars: int
    command_evidence_count: int
    command_evidence_chars: int
    command_evidence_truncated_count: int
    command_evidence_omitted_count: int
    foreground_authority_sha256: str | None
    foreground_evidence_sha256: str = field(repr=False)
    host_time_observed: bool = False
    recent_exhaustive_evidence_count: int = 0

    def __post_init__(self) -> None:
        # This full host never enters a role prompt or serialized projection. It
        # is retained only long enough to fingerprint final patch dependencies.
        object.__setattr__(
            self,
            "trusted_target_host",
            deepcopy(self.trusted_target_host),
        )

    def prompt_json(self) -> str:
        return canonical_json(self.document)


class EnrichmentTargetNotFoundError(ValueError):
    """The requested host disappeared or was invalid at snapshot time."""


def project_world_state_for_enrichment(
    world_state: dict[str, object],
    *,
    target_host: str | None = None,
    recent_history_limit: int = DEFAULT_RECENT_HISTORY_LIMIT,
    foreground_authority: ForegroundAuthoritySnapshot | None = None,
) -> EnrichmentContextProjection:
    """Build the host-focused DREAMLAND context used around the Expert boundary.

    The target and active host retain their established structured facts. Peers
    are reduced to identity and network/service anchors by the command-emulator
    projector. Raw artifact bodies remain excluded. A short target-host command
    window retains bounded recorded stdout/stderr excerpts as continuity
    evidence; peer histories remain metadata-only. The Coordinator receives an
    organized view derived from this projection and the Integrator receives a
    fresh projection; the Expert receives only a neutral allowlisted brief.
    """
    if recent_history_limit < 0:
        raise ValueError("recent_history_limit must be zero or greater")
    source = deepcopy(world_state) if isinstance(world_state, dict) else {}
    resolved_target = resolve_enrichment_host_ref(source, target_host)
    if resolved_target is None:
        requested = target_host or active_host_ref(source)
        raise EnrichmentTargetNotFoundError(
            f"enrichment target host not found: {requested!r}"
        )
    if (
        foreground_authority is not None
        and foreground_authority.target_host != resolved_target
    ):
        raise ValueError("foreground authority does not match the enrichment target")

    detailed_hosts = detailed_host_registry_refs_for_command_emulator(
        source,
        detailed_host_refs=(resolved_target,),
    )
    projected_world = project_world_state_for_command_emulator(
        source,
        detailed_host_refs=(resolved_target,),
    )
    target_metadata = _target_metadata(source, resolved_target)
    foreground_evidence = capture_foreground_evidence(
        target_metadata,
        recent_history_limit=recent_history_limit,
    )
    sanitized_world = _sanitize_projection(
        projected_world,
        recent_history_limit=recent_history_limit,
    )
    (
        target_history,
        command_evidence_count,
        command_evidence_chars,
        command_evidence_truncated_count,
        command_evidence_omitted_count,
    ) = _summarize_target_history(
        target_metadata.get("command_history"),
        recent_history_limit,
    )
    typed_evidence_command_ids = {
        item.command_id for item in foreground_evidence.recent_exhaustive
    }
    if foreground_evidence.host_time is not None:
        typed_evidence_command_ids.add(foreground_evidence.host_time.command_id)
    target_history = [
        item
        for item in target_history
        if item.get("id") not in typed_evidence_command_ids
    ]
    _replace_target_history(
        sanitized_world,
        resolved_target,
        target_history,
        history_was_present="command_history" in target_metadata,
    )
    document: dict[str, object] = {
        "v": ENRICHMENT_CONTEXT_VERSION,
        "scope": {
            "target_host": resolved_target,
            "detailed_hosts": list(detailed_hosts),
            "peer_hosts": "compact",
            "command_history": {
                "target_host": (
                    f"last_{recent_history_limit}_bounded_results_"
                    "excluding_typed_foreground_evidence"
                ),
                "peer_hosts": f"last_{recent_history_limit}_metadata_only",
                "output_char_limit": RECENT_COMMAND_OUTPUT_CHAR_LIMIT,
                "semantics": "recorded_output_not_accepted_state",
            },
            "raw_values": "omitted_except_bounded_command_results",
        },
        "world": sanitized_world,
        "host_time": foreground_evidence.host_time_payload(),
        "recent_exhaustive_evidence": (foreground_evidence.recent_exhaustive_payload()),
    }
    if foreground_authority is not None:
        document["foreground_authority"] = foreground_authority.public_payload()
    full_json = canonical_json(source)
    projected_json = canonical_json(document)
    return EnrichmentContextProjection(
        document=document,
        trusted_target_host=target_metadata,
        target_host=resolved_target,
        detailed_hosts=detailed_hosts,
        full_world_sha256=_sha256(full_json),
        target_host_sha256=_sha256(canonical_json(target_metadata)),
        projected_sha256=_sha256(projected_json),
        full_world_chars=len(full_json),
        projected_chars=len(projected_json),
        command_evidence_count=command_evidence_count,
        command_evidence_chars=command_evidence_chars,
        command_evidence_truncated_count=command_evidence_truncated_count,
        command_evidence_omitted_count=command_evidence_omitted_count,
        foreground_authority_sha256=(
            foreground_authority.session_semantics_sha256
            if foreground_authority is not None
            else None
        ),
        foreground_evidence_sha256=foreground_evidence.private_sha256,
        host_time_observed=foreground_evidence.host_time_observed,
        recent_exhaustive_evidence_count=(foreground_evidence.recent_exhaustive_count),
    )


def resolve_enrichment_host_ref(
    world_state: dict[str, object],
    requested_host: str | None,
) -> str | None:
    hosts = world_state.get("hosts_by_name")
    if not isinstance(hosts, dict):
        return None
    cleaned = requested_host.strip() if isinstance(requested_host, str) else ""
    candidate = cleaned or active_host_ref(world_state)
    if candidate in hosts and isinstance(candidate, str):
        return candidate
    for registry_key, metadata in hosts.items():
        if not isinstance(registry_key, str) or not isinstance(metadata, dict):
            continue
        if metadata.get("hostname") == candidate:
            return registry_key
        aliases = metadata.get("aliases")
        if isinstance(aliases, (list, tuple)) and candidate in aliases:
            return registry_key
    return None


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _target_metadata(
    world_state: dict[str, object],
    target_host: str,
) -> dict[str, object]:
    hosts = world_state.get("hosts_by_name")
    if not isinstance(hosts, dict):
        return {}
    metadata = hosts.get(target_host)
    return deepcopy(metadata) if isinstance(metadata, dict) else {}


def _sanitize_projection(
    value: object,
    *,
    recent_history_limit: int,
    parent_key: str | None = None,
) -> object:
    if parent_key == "command_history":
        return _summarize_history(value, recent_history_limit)
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _OMITTED_VALUE_KEYS:
                continue
            sanitized[str(key)] = _sanitize_projection(
                child,
                recent_history_limit=recent_history_limit,
                parent_key=normalized,
            )
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_projection(
                item,
                recent_history_limit=recent_history_limit,
                parent_key=parent_key,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            _sanitize_projection(
                item,
                recent_history_limit=recent_history_limit,
                parent_key=parent_key,
            )
            for item in value
        ]
    return deepcopy(value)


def _summarize_history(value: object, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list) or limit == 0:
        return []
    summaries: list[dict[str, object]] = []
    for entry in value[-limit:]:
        if not isinstance(entry, dict):
            continue
        summaries.append(
            {key: deepcopy(entry[key]) for key in _HISTORY_SUMMARY_KEYS if key in entry}
        )
    return summaries


def _summarize_target_history(
    value: object,
    limit: int,
) -> tuple[list[dict[str, object]], int, int, int, int]:
    """Retain bounded target-host result excerpts without interpreting them."""

    if not isinstance(value, list) or limit == 0:
        omitted_count = (
            sum(isinstance(entry, dict) for entry in value)
            if isinstance(value, list)
            else 0
        )
        return [], 0, 0, 0, omitted_count

    valid_entries = [entry for entry in value if isinstance(entry, dict)]
    selected = valid_entries[-limit:]
    omitted_count = len(valid_entries) - len(selected)
    remaining_chars = RECENT_COMMAND_OUTPUT_CHAR_LIMIT
    reverse_summaries: list[dict[str, object]] = []
    evidence_count = len(selected)
    evidence_chars = 0
    truncated_count = 0

    for entry in reversed(selected):
        summary = {
            key: deepcopy(entry[key]) for key in _HISTORY_SUMMARY_KEYS if key in entry
        }
        command = str(entry.get("command", ""))
        summary["surface"] = infer_command_surface(command)
        summary["evidence_kind"] = "recorded_command_result"

        stdout = str(entry.get("stdout", ""))
        stderr = str(entry.get("stderr", ""))
        entry_budget = min(
            RECENT_COMMAND_ENTRY_OUTPUT_CHAR_LIMIT,
            remaining_chars,
        )
        excerpts = _bounded_stream_excerpts(stdout, stderr, entry_budget)
        included_chars = 0
        entry_truncated = False
        for stream_name, (excerpt, omitted_chars) in excerpts.items():
            if excerpt:
                summary[f"{stream_name}_excerpt"] = excerpt
                included_chars += len(excerpt)
            if omitted_chars:
                summary[f"{stream_name}_omitted_chars"] = omitted_chars
                entry_truncated = True
        if stdout or stderr:
            if included_chars:
                evidence_chars += included_chars
                remaining_chars -= included_chars
            if entry_truncated:
                truncated_count += 1
        reverse_summaries.append(summary)

    return (
        list(reversed(reverse_summaries)),
        evidence_count,
        evidence_chars,
        truncated_count,
        omitted_count,
    )


def _bounded_stream_excerpts(
    stdout: str,
    stderr: str,
    budget: int,
) -> dict[str, tuple[str, int]]:
    streams = [("stdout", stdout), ("stderr", stderr)]
    nonempty = [(name, text) for name, text in streams if text]
    if not nonempty or budget <= 0:
        return {name: ("", len(text)) for name, text in nonempty}

    allocations = {
        name: min(len(text), budget // len(nonempty)) for name, text in nonempty
    }
    remaining = budget - sum(allocations.values())
    for name, text in nonempty:
        if remaining <= 0:
            break
        extra = min(len(text) - allocations[name], remaining)
        allocations[name] += extra
        remaining -= extra
    return {name: _bounded_excerpt(text, allocations[name]) for name, text in nonempty}


def _bounded_excerpt(value: str, limit: int) -> tuple[str, int]:
    if limit <= 0:
        return "", len(value)
    if len(value) <= limit:
        return value, 0
    omitted = len(value) - limit
    if limit <= len(_OUTPUT_TRIM_MARKER):
        return value[:limit], omitted
    content_budget = limit - len(_OUTPUT_TRIM_MARKER)
    head_chars = (content_budget + 1) // 2
    tail_chars = content_budget - head_chars
    excerpt = (
        value[:head_chars]
        + _OUTPUT_TRIM_MARKER
        + (value[-tail_chars:] if tail_chars else "")
    )
    return excerpt, len(value) - head_chars - tail_chars


def _replace_target_history(
    sanitized_world: object,
    target_host: str,
    history: list[dict[str, object]],
    *,
    history_was_present: bool,
) -> None:
    if not isinstance(sanitized_world, dict):
        return
    hosts = sanitized_world.get("hosts_by_name")
    if not isinstance(hosts, dict):
        return
    host = hosts.get(target_host)
    if not isinstance(host, dict):
        return
    if history_was_present:
        host["command_history"] = history


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
