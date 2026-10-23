from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import unified_diff
from typing import Any, Protocol

from dreamland.config import LangfuseSettings

LOGGER = logging.getLogger("dreamland.console")
DEFAULT_SESSION_LIMIT = 20
DEFAULT_COMMAND_LIMIT = 20
COMMAND_OBSERVATION_NAME = "dreamland-command-agent"
REASONING_SUMMARY_METADATA_KEY = "dreamland.reasoning_summary"
MAX_RENDER_CHARS = 200_000
MAX_TABLE_CELL_CHARS = 120
MAX_RENDER_DEPTH = 20


class IntrospectionUnavailable(RuntimeError):
    pass


class ConsoleIntrospection(Protocol):
    def available(self) -> bool:
        pass

    def flush(self) -> None:
        pass

    def list_sessions(self, *, limit: int) -> SessionList:
        pass

    def list_commands(
        self,
        *,
        session_id: str,
        limit: int,
        filter_text: str | None = None,
        sort: str = "time",
        descending: bool = True,
        include_io_summary: bool = True,
    ) -> list[CommandSummary]:
        pass

    def inspect_command(
        self,
        *,
        session_id: str,
        target: str | None,
        limit: int,
        detail: str = "full",
    ) -> CommandInspection:
        pass

    def state_delta(
        self,
        *,
        session_id: str,
        from_target: str | None,
        to_target: str,
        limit: int,
        sections: Sequence[str] = (),
    ) -> StateDelta:
        pass


@dataclass(frozen=True, slots=True)
class SessionSummary:
    id: str
    created_at: datetime | str | None = None
    environment: str | None = None
    state_profile: str | None = None


@dataclass(frozen=True, slots=True)
class SessionList:
    sessions: list[SessionSummary]
    total: int | None = None


@dataclass(frozen=True, slots=True)
class CommandSummary:
    id: str
    session_id: str | None
    timestamp: datetime | str | None
    command: str
    observation_id: str | None = None
    status: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    total_cost: float | None = None
    latency: float | None = None
    reasoning: bool = False
    summary: bool | None = False


@dataclass(frozen=True, slots=True)
class ObservationSummary:
    id: str
    type: str
    name: str | None
    input: Any = None
    output: Any = None
    metadata: Any = None
    model: str | None = None
    usage: dict[str, int] | None = None
    total_cost: float | None = None
    latency: float | None = None


@dataclass(frozen=True, slots=True)
class CommandInspection:
    command: CommandSummary
    detail: str = "full"
    trace_input: Any = None
    trace_output: Any = None
    trace_metadata: Any = None
    reasoning_summary: str | None = None
    observations: list[ObservationSummary] | None = None


@dataclass(frozen=True, slots=True)
class StateDelta:
    from_label: str
    to_label: str
    before_state: Any
    after_state: Any
    sections: tuple[str, ...] = ()


class DisabledIntrospection:
    def available(self) -> bool:
        return False

    def flush(self) -> None:
        raise IntrospectionUnavailable(_UNAVAILABLE_MESSAGE)

    def list_sessions(self, *, limit: int) -> SessionList:
        raise IntrospectionUnavailable(_UNAVAILABLE_MESSAGE)

    def list_commands(
        self,
        *,
        session_id: str,
        limit: int,
        filter_text: str | None = None,
        sort: str = "time",
        descending: bool = True,
        include_io_summary: bool = True,
    ) -> list[CommandSummary]:
        raise IntrospectionUnavailable(_UNAVAILABLE_MESSAGE)

    def inspect_command(
        self,
        *,
        session_id: str,
        target: str | None,
        limit: int,
        detail: str = "full",
    ) -> CommandInspection:
        raise IntrospectionUnavailable(_UNAVAILABLE_MESSAGE)

    def state_delta(
        self,
        *,
        session_id: str,
        from_target: str | None,
        to_target: str,
        limit: int,
        sections: Sequence[str] = (),
    ) -> StateDelta:
        raise IntrospectionUnavailable(_UNAVAILABLE_MESSAGE)


class LangfuseConsoleIntrospection:
    def __init__(
        self,
        settings: LangfuseSettings,
        *,
        api_client: Any | None = None,
        flush_client: Any | None = None,
    ) -> None:
        self.settings = settings
        self._api_client = api_client
        self._flush_client = flush_client

    @classmethod
    def from_settings(cls, settings: LangfuseSettings | None) -> ConsoleIntrospection:
        if (
            settings is None
            or not settings.enabled
            or not settings.public_key
            or not settings.secret_key
        ):
            return DisabledIntrospection()
        return cls(settings)

    def available(self) -> bool:
        return True

    def flush(self) -> None:
        LOGGER.debug("Langfuse flush start")
        try:
            client = self._flush()
            client.flush()
        except Exception as exc:
            LOGGER.warning("Langfuse flush failed error=%r", exc)
            raise IntrospectionUnavailable(f"Langfuse flush failed: {exc}") from exc
        LOGGER.debug("Langfuse flush complete")

    def list_sessions(self, *, limit: int) -> SessionList:
        LOGGER.debug("Langfuse sessions.list start page=1 limit=%s", limit)
        try:
            response = self._api().sessions.list(page=1, limit=limit)
        except Exception as exc:
            LOGGER.warning(
                "Langfuse sessions.list failed page=1 limit=%s error=%r",
                limit,
                exc,
            )
            raise IntrospectionUnavailable(
                f"Langfuse session query failed: {exc}"
            ) from exc
        sessions = [
            SessionSummary(
                id=str(_field(session, "id") or ""),
                created_at=_field(session, "created_at", "createdAt"),
                environment=_optional_str(_field(session, "environment")),
                state_profile=_optional_str(_field(session, "state_profile")),
            )
            for session in _field(response, "data", default=[])
        ]
        meta = _field(response, "meta")
        total = _field(meta, "total_items", "totalItems") if meta is not None else None
        LOGGER.debug(
            "Langfuse sessions.list complete page=1 limit=%s returned=%s total=%s",
            limit,
            len(sessions),
            total,
        )
        return SessionList(
            sessions=sessions, total=total if isinstance(total, int) else None
        )

    def list_commands(
        self,
        *,
        session_id: str,
        limit: int,
        filter_text: str | None = None,
        sort: str = "time",
        descending: bool = True,
        include_io_summary: bool = True,
    ) -> list[CommandSummary]:
        filter_json = _session_filter_json(session_id)
        fields = "core,basic,metadata,usage,model,metrics"
        LOGGER.debug(
            "Langfuse observations.get_many start purpose=commands limit=%s "
            "session_id=%s name=%s fields=%s filter=%s",
            limit,
            session_id,
            COMMAND_OBSERVATION_NAME,
            fields,
            filter_json,
        )
        try:
            response = self._api().observations.get_many(
                limit=limit,
                name=COMMAND_OBSERVATION_NAME,
                fields=fields,
                expand_metadata="dreamland.command",
                filter=filter_json,
            )
        except Exception as exc:
            LOGGER.warning(
                "Langfuse observations.get_many failed purpose=commands "
                "limit=%s session_id=%s name=%s fields=%s filter=%s error=%r",
                limit,
                session_id,
                COMMAND_OBSERVATION_NAME,
                fields,
                filter_json,
                exc,
            )
            raise IntrospectionUnavailable(
                f"Langfuse command query failed: {exc}"
            ) from exc
        commands = [
            _command_from_observation(observation)
            for observation in _field(response, "data", default=[])
        ]
        if commands:
            commands = self._add_child_observation_metrics(
                commands,
                session_id=session_id,
                limit=limit,
                filter_json=filter_json,
                fields=fields,
                include_io_summary=include_io_summary,
            )
        LOGGER.debug(
            "Langfuse observations.get_many complete purpose=commands "
            "limit=%s session_id=%s returned=%s",
            limit,
            session_id,
            len(commands),
        )
        if filter_text:
            lowered = filter_text.lower()
            commands = [
                command
                for command in commands
                if lowered in command.command.lower()
                or lowered in command.id.lower()
                or lowered in (command.observation_id or "").lower()
            ]
        return _sort_commands(commands, sort=sort, descending=descending)

    def _add_child_observation_metrics(
        self,
        commands: list[CommandSummary],
        *,
        session_id: str,
        limit: int,
        filter_json: str,
        fields: str,
        include_io_summary: bool,
    ) -> list[CommandSummary]:
        child_limit = min(max(limit * 10, 50), 1000)
        metric_fields = f"{fields},io" if include_io_summary else fields
        LOGGER.debug(
            "Langfuse observations.get_many start purpose=command-metrics "
            "limit=%s session_id=%s fields=%s include_io_summary=%s filter=%s",
            child_limit,
            session_id,
            metric_fields,
            include_io_summary,
            filter_json,
        )
        try:
            response = self._api().observations.get_many(
                limit=child_limit,
                fields=metric_fields,
                filter=filter_json,
            )
        except Exception as exc:
            LOGGER.warning(
                "Langfuse observations.get_many failed purpose=command-metrics "
                "limit=%s session_id=%s fields=%s include_io_summary=%s "
                "filter=%s error=%r",
                child_limit,
                session_id,
                metric_fields,
                include_io_summary,
                filter_json,
                exc,
            )
            return [_ensure_model(command) for command in commands]
        observations = list(_field(response, "data", default=[]))
        LOGGER.debug(
            "Langfuse observations.get_many complete purpose=command-metrics "
            "limit=%s session_id=%s returned=%s",
            child_limit,
            session_id,
            len(observations),
        )
        _log_observation_tree_debug(commands, observations)
        return _merge_child_metrics(commands, observations)

    def inspect_command(
        self,
        *,
        session_id: str,
        target: str | None,
        limit: int,
        detail: str = "full",
    ) -> CommandInspection:
        detail = _normalize_inspection_detail(detail)
        full_detail = detail == "full"
        commands = self.list_commands(
            session_id=session_id,
            limit=limit,
            include_io_summary=full_detail,
        )
        command = _resolve_command(commands, target)
        filter_json = _session_filter_json(session_id)
        fields = (
            "core,basic,io,metadata,usage,model,metrics"
            if full_detail
            else "core,basic,usage,model,metrics"
        )
        LOGGER.debug(
            "Langfuse observations.get_many start purpose=inspection "
            "observation_id=%s session_id=%s fields=%s detail=%s filter=%s",
            command.id,
            session_id,
            fields,
            detail,
            filter_json,
        )
        kwargs: dict[str, object] = {
            "limit": min(max(limit * 10, 50), 1000),
            "fields": fields,
            "filter": filter_json,
        }
        if full_detail:
            kwargs["expand_metadata"] = "dreamland.command"
        try:
            response = self._api().observations.get_many(**kwargs)
        except Exception as exc:
            LOGGER.warning(
                "Langfuse observations.get_many failed purpose=inspection "
                "observation_id=%s session_id=%s fields=%s detail=%s "
                "filter=%s error=%r",
                command.id,
                session_id,
                fields,
                detail,
                filter_json,
                exc,
            )
            raise IntrospectionUnavailable(
                f"Langfuse command inspection failed: {exc}"
            ) from exc
        observations = _field(response, "data", default=[])
        command_observations = _command_observation_tree(command, observations)
        if not command_observations:
            raise IntrospectionUnavailable(
                f"Langfuse command inspection found no observation {command.id!r}."
            )
        observation = command_observations[0]
        LOGGER.debug(
            "Langfuse observations.get_many complete purpose=inspection "
            "observation_id=%s returned=%s",
            command.id,
            len(command_observations),
        )
        return CommandInspection(
            command=command,
            detail=detail,
            trace_input=_field(observation, "input") if full_detail else None,
            trace_output=_field(observation, "output") if full_detail else None,
            trace_metadata=_field(observation, "metadata") if full_detail else None,
            reasoning_summary=_reasoning_summary_from_observations(
                command_observations
            ),
            observations=[
                _observation_from_api(command_observation)
                for command_observation in command_observations
            ],
        )

    def state_delta(
        self,
        *,
        session_id: str,
        from_target: str | None,
        to_target: str,
        limit: int,
        sections: Sequence[str] = (),
    ) -> StateDelta:
        commands = self.list_commands(
            session_id=session_id,
            limit=limit,
            include_io_summary=False,
        )
        from_command = _resolve_command(commands, from_target) if from_target else None
        to_command = _resolve_command(commands, to_target)
        filter_json = _session_filter_json(session_id)
        fields = "core,basic,io,metadata,usage,model,metrics"
        LOGGER.debug(
            "Langfuse observations.get_many start purpose=state-delta "
            "from_observation_id=%s to_observation_id=%s session_id=%s fields=%s "
            "filter=%s",
            from_command.id if from_command is not None else "",
            to_command.id,
            session_id,
            fields,
            filter_json,
        )
        try:
            response = self._api().observations.get_many(
                limit=min(max(limit * 10, 50), 1000),
                fields=fields,
                expand_metadata="dreamland.command",
                filter=filter_json,
            )
        except Exception as exc:
            LOGGER.warning(
                "Langfuse observations.get_many failed purpose=state-delta "
                "from_observation_id=%s to_observation_id=%s session_id=%s "
                "fields=%s filter=%s error=%r",
                from_command.id if from_command is not None else "",
                to_command.id,
                session_id,
                fields,
                filter_json,
                exc,
            )
            raise IntrospectionUnavailable(
                f"Langfuse state delta query failed: {exc}"
            ) from exc
        observations = list(_field(response, "data", default=[]))
        LOGGER.debug(
            "Langfuse observations.get_many complete purpose=state-delta "
            "from_observation_id=%s to_observation_id=%s returned=%s",
            from_command.id if from_command is not None else "",
            to_command.id,
            len(observations),
        )
        return _state_delta_from_observations(
            commands=commands,
            observations=observations,
            from_command=from_command,
            to_command=to_command,
            sections=sections,
        )

    def _api(self) -> Any:
        if self._api_client is None:
            try:
                from langfuse.api import LangfuseAPI
            except ImportError as exc:
                raise IntrospectionUnavailable(
                    "Langfuse introspection requires the langfuse package."
                ) from exc
            LOGGER.debug(
                "Langfuse API client init host=%s public_key_suffix=%s",
                self.settings.host or "https://cloud.langfuse.com",
                _key_suffix(self.settings.public_key),
            )
            self._api_client = LangfuseAPI(
                base_url=self.settings.host or "https://cloud.langfuse.com",
                x_langfuse_public_key=self.settings.public_key,
                username=self.settings.public_key,
                password=self.settings.secret_key,
                timeout=30,
            )
        return self._api_client

    def _flush(self) -> Any:
        if self._flush_client is None:
            try:
                from langfuse import Langfuse
            except ImportError as exc:
                raise IntrospectionUnavailable(
                    "Langfuse flushing requires the langfuse package."
                ) from exc
            LOGGER.debug(
                "Langfuse flush client init host=%s public_key_suffix=%s",
                self.settings.host or "https://cloud.langfuse.com",
                _key_suffix(self.settings.public_key),
            )
            self._flush_client = Langfuse(
                public_key=self.settings.public_key,
                secret_key=self.settings.secret_key,
                host=self.settings.host,
            )
        return self._flush_client


def render_sessions(
    sessions: SessionList,
    *,
    active_session_id: str | None,
) -> str:
    rows = [
        [
            "*" if session.id == active_session_id else "",
            session.id,
            _format_time(session.created_at),
            session.environment or "",
            session.state_profile or "",
        ]
        for session in sessions.sessions
    ]
    total = (
        f"{sessions.total} total, {len(sessions.sessions)} shown"
        if sessions.total is not None
        else f"{len(sessions.sessions)} shown"
    )
    return f"{total}\n{_table(['active', 'id', 'created', 'env', 'profile'], rows)}"


def render_commands(
    commands: Sequence[CommandSummary],
    *,
    columns: Sequence[str],
) -> str:
    headers = list(columns)
    rows = [
        [_command_column(command, index, column) for column in headers]
        for index, command in enumerate(commands, start=1)
    ]
    return _table(headers, rows)


def render_inspection(
    inspection: CommandInspection,
    *,
    show_prompt: bool,
    show_output: bool,
    show_reasoning: bool,
    show_metadata: bool,
    show_cost: bool,
    show_tokens: bool,
    show_timing: bool,
    show_observations: bool,
) -> str:
    command = inspection.command
    sections = [_inspection_header(command)]
    if show_reasoning:
        sections.extend(
            [
                "",
                _section_header("REASONING SUMMARY"),
                inspection.reasoning_summary or "No reasoning summary captured.",
            ]
        )
    if show_prompt:
        sections.extend(
            [
                "",
                _section_header("MODEL INPUT"),
                render_value(inspection.trace_input),
            ]
        )
    if show_output:
        sections.extend(
            [
                "",
                _section_header("MODEL OUTPUT"),
                render_value(inspection.trace_output),
            ]
        )
    if show_metadata:
        sections.extend(
            [
                "",
                _section_header("METADATA"),
                render_value(inspection.trace_metadata),
            ]
        )
    if show_cost:
        sections.extend(["", _section_header("COST"), _cost_table(command)])
    if show_tokens:
        sections.extend(["", _section_header("TOKENS"), _token_table(command)])
    if show_timing:
        sections.extend(["", _section_header("TIMING"), _timing_table(command)])
    if show_observations and inspection.observations is not None:
        sections.extend(["", _section_header("OBSERVATIONS")])
        for observation in inspection.observations:
            sections.append(_render_observation(observation))
    return "\n".join(sections).rstrip() + "\n"


def render_state_delta(
    delta: StateDelta,
    *,
    color: str = "auto",
) -> str:
    before = _canonical_state_text(delta.before_state).splitlines()
    after = _canonical_state_text(delta.after_state).splitlines()
    lines = list(
        unified_diff(
            before,
            after,
            fromfile=delta.from_label,
            tofile=delta.to_label,
            lineterm="",
        )
    )
    if not lines:
        lines = [
            f"--- {delta.from_label}",
            f"+++ {delta.to_label}",
            "No state changes.",
        ]
    rendered = [_color_diff_line(line, color=color) for line in lines]
    return "\n".join(rendered).rstrip() + "\n"


def _inspection_header(command: CommandSummary) -> str:
    return "\n".join(
        [
            _section_header("COMMAND"),
            _table(
                ["field", "value"],
                [
                    ["id", command.id],
                    ["session", command.session_id or ""],
                    ["time", _format_time(command.timestamp)],
                    ["command", command.command],
                    ["model", command.model or ""],
                ],
            ).rstrip(),
        ]
    )


def _section_header(title: str) -> str:
    border = "=" * max(12, len(title) + 8)
    return f"{border}\n=== {title} ===\n{border}"


def _cost_table(command: CommandSummary) -> str:
    return _table(
        ["field", "value"],
        [["total_cost", _money(command.total_cost)]],
    ).rstrip()


def _token_table(command: CommandSummary) -> str:
    return _table(
        ["field", "value"],
        [
            ["input_tokens", _format_optional_int(command.input_tokens)],
            ["output_tokens", _format_optional_int(command.output_tokens)],
            ["total_tokens", _format_optional_int(command.total_tokens)],
        ],
    ).rstrip()


def _format_optional_int(value: int | None) -> str:
    return "" if value is None else str(value)


def _timing_table(command: CommandSummary) -> str:
    return _table(
        ["field", "value"],
        [
            ["start_time", _format_time(command.timestamp)],
            ["latency", _seconds(command.latency)],
        ],
    ).rstrip()


def render_value(value: Any) -> str:
    parsed = _decode_json_string(value)
    rendered = _render_pretty(parsed, indent=0, depth=0)
    if len(rendered) > MAX_RENDER_CHARS:
        return rendered[:MAX_RENDER_CHARS] + "\n[truncated]\n"
    return rendered


def _command_from_observation(observation: Any) -> CommandSummary:
    metadata = _field(observation, "metadata")
    usage = _optional_dict(_field(observation, "usage_details", "usageDetails")) or {}
    observation_id = str(_field(observation, "id") or "")
    command_id = _metadata_value(metadata, "dreamland.command_id") or observation_id
    command = _metadata_value(metadata, "dreamland.command")
    summary = _reasoning_summary_from_observations([observation]) is not None
    return CommandSummary(
        id=command_id,
        observation_id=observation_id,
        session_id=_optional_str(_field(observation, "session_id", "sessionId")),
        timestamp=_field(observation, "timestamp", "start_time", "startTime"),
        command=command or _fallback_command(observation),
        status=_optional_str(
            _field(observation, "status_message", "statusMessage", "level")
        ),
        model=_optional_str(
            _field(observation, "provided_model_name", "providedModelName")
        ),
        input_tokens=_optional_int(usage.get("input") or usage.get("input_tokens")),
        output_tokens=_optional_int(usage.get("output") or usage.get("output_tokens")),
        total_tokens=_optional_int(usage.get("total") or usage.get("total_tokens")),
        total_cost=_optional_float(_field(observation, "total_cost", "totalCost")),
        latency=_optional_float(_field(observation, "latency")),
        summary=summary,
    )


def _normalize_inspection_detail(detail: str) -> str:
    normalized = detail.strip().lower() if isinstance(detail, str) else ""
    if normalized in {"compact", "full"}:
        return normalized
    raise ValueError("inspection detail must be 'compact' or 'full'")


def _observation_from_api(observation: Any) -> ObservationSummary:
    return ObservationSummary(
        id=str(_field(observation, "id") or ""),
        type=str(_field(observation, "type") or ""),
        name=_optional_str(_field(observation, "name")),
        input=_field(observation, "input"),
        output=_field(observation, "output"),
        metadata=_field(observation, "metadata"),
        model=_optional_str(
            _field(observation, "provided_model_name", "providedModelName")
        ),
        usage=_optional_dict(_field(observation, "usage_details", "usageDetails")),
        total_cost=_optional_float(_field(observation, "total_cost", "totalCost")),
        latency=_optional_float(_field(observation, "latency")),
    )


def _state_delta_from_observations(
    *,
    commands: Sequence[CommandSummary],
    observations: Sequence[Any],
    from_command: CommandSummary | None,
    to_command: CommandSummary,
    sections: Sequence[str],
) -> StateDelta:
    to_observation = _command_observation(to_command, observations)
    if to_observation is None:
        raise IntrospectionUnavailable(
            f"Langfuse state delta found no observation {to_command.id!r}."
        )
    if from_command is not None:
        from_observation = _command_observation(from_command, observations)
        if from_observation is None:
            raise IntrospectionUnavailable(
                f"Langfuse state delta found no observation {from_command.id!r}."
            )
        before_state = _state_from_observation_output(from_observation)
        from_label = _state_delta_label("after", from_command)
    else:
        previous_command = _previous_command(commands, to_command)
        if previous_command is not None:
            previous_observation = _command_observation(
                previous_command,
                observations,
            )
            if previous_observation is None:
                raise IntrospectionUnavailable(
                    "Langfuse state delta could not load the previous command "
                    f"{previous_command.id!r}; increase --number."
                )
            before_state = _state_from_observation_output(previous_observation)
            from_label = _state_delta_label("before", to_command)
        else:
            before_state = _state_from_observation_input(to_observation)
            from_label = _state_delta_label("before", to_command)
    after_state = _state_from_observation_output(to_observation)
    section_tuple = tuple(sections)
    return StateDelta(
        from_label=from_label,
        to_label=_state_delta_label("after", to_command),
        before_state=_filter_state_sections(before_state, section_tuple),
        after_state=_filter_state_sections(after_state, section_tuple),
        sections=section_tuple,
    )


def _previous_command(
    commands: Sequence[CommandSummary],
    command: CommandSummary,
) -> CommandSummary | None:
    for index, candidate in enumerate(commands):
        if candidate.id == command.id:
            previous_index = index + 1
            return commands[previous_index] if previous_index < len(commands) else None
    return None


def _state_from_observation_output(observation: Any) -> Any:
    state = _decode_json_string(_field(observation, "output"))
    if state is None:
        raise IntrospectionUnavailable(
            f"Observation {str(_field(observation, 'id') or '')!r} has no output state."
        )
    return state


def _state_from_observation_input(observation: Any) -> Any:
    state = _decode_json_string(_field(observation, "input"))
    if state is None:
        raise IntrospectionUnavailable(
            f"Observation {str(_field(observation, 'id') or '')!r} has no input state."
        )
    return state


def _filter_state_sections(state: Any, sections: Sequence[str]) -> Any:
    if not sections:
        return state
    if not isinstance(state, dict):
        return state
    return {section: state.get(section) for section in sections if section in state}


def _state_delta_label(kind: str, command: CommandSummary) -> str:
    return (
        f"{kind} {_short_id(command.id)} "
        f"{_format_time(command.timestamp)} {command.command}"
    ).rstrip()


def _command_observation_tree(
    command: CommandSummary, observations: Sequence[Any]
) -> list[Any]:
    by_id = {
        str(_field(observation, "id") or ""): observation
        for observation in observations
    }
    observation_id = _command_observation_id(command)
    root = by_id.get(observation_id)
    if root is None:
        return []
    children_by_parent: dict[str, list[Any]] = {}
    for observation in observations:
        parent_id = _optional_str(
            _field(observation, "parent_observation_id", "parentObservationId")
        )
        if parent_id is not None:
            children_by_parent.setdefault(parent_id, []).append(observation)
    return [root, *_descendant_observations(observation_id, children_by_parent)]


def _reasoning_summary_from_observations(observations: Sequence[Any]) -> str | None:
    summaries: list[str] = []
    for observation in observations:
        summaries.extend(_reasoning_summaries(_field(observation, "output")))
        summaries.extend(_reasoning_summaries(_field(observation, "metadata")))
    cleaned = [
        _dedupe_summary_sections(summary.strip())
        for summary in summaries
        if summary.strip()
    ]
    if not cleaned:
        return None
    return _dedupe_summary_sections("\n\n".join(dict.fromkeys(cleaned)))


def _dedupe_summary_sections(summary: str) -> str:
    sections = [section.strip() for section in summary.split("\n\n") if section.strip()]
    if len(sections) < 2 or len(sections) % 2 != 0:
        return summary
    midpoint = len(sections) // 2
    first_half = [
        _normalized_summary_section(section) for section in sections[:midpoint]
    ]
    second_half = [
        _normalized_summary_section(section) for section in sections[midpoint:]
    ]
    if first_half == second_half:
        return "\n\n".join(sections[:midpoint])
    return summary


def _normalized_summary_section(section: str) -> str:
    return "\n".join(line.rstrip() for line in section.splitlines()).strip()


def _reasoning_summaries(value: Any) -> list[str]:
    parsed = _decode_json_string(value)
    summaries: list[str] = []
    _collect_reasoning_summaries(parsed, summaries)
    return summaries


def _collect_reasoning_summaries(value: Any, summaries: list[str]) -> None:
    if isinstance(value, dict):
        metadata_summary = value.get(REASONING_SUMMARY_METADATA_KEY)
        if isinstance(metadata_summary, str):
            summaries.append(metadata_summary)
        if value.get("type") == "reasoning":
            summaries.extend(_summary_texts(value.get("summary")))
        if value.get("type") == "thinking":
            thinking = value.get("thinking")
            if isinstance(thinking, str):
                summaries.append(thinking)
        reasoning_content = value.get("reasoning_content")
        if isinstance(reasoning_content, str):
            summaries.append(reasoning_content)
        for key in ("content", "output", "generations", "message", "additional_kwargs"):
            if key in value:
                _collect_reasoning_summaries(value[key], summaries)
        result = value.get("result")
        if isinstance(result, dict):
            _collect_reasoning_summaries(result, summaries)
        metadata = value.get("metadata")
        if isinstance(metadata, dict):
            _collect_reasoning_summaries(metadata, summaries)
        return
    if isinstance(value, list):
        for item in value:
            _collect_reasoning_summaries(item, summaries)


def _summary_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        return texts
    return []


def _merge_child_metrics(
    commands: Sequence[CommandSummary],
    observations: Sequence[Any],
) -> list[CommandSummary]:
    children_by_parent: dict[str, list[Any]] = {}
    for observation in observations:
        parent_id = _optional_str(
            _field(observation, "parent_observation_id", "parentObservationId")
        )
        if parent_id is not None:
            children_by_parent.setdefault(parent_id, []).append(observation)
    return [
        _merge_one_command(
            command,
            _command_observation(command, observations),
            _descendant_observations(
                _command_observation_id(command),
                children_by_parent,
            ),
        )
        for command in commands
    ]


def _log_observation_tree_debug(
    commands: Sequence[CommandSummary],
    observations: Sequence[Any],
) -> None:
    if not LOGGER.isEnabledFor(logging.DEBUG):
        return
    children_by_parent: dict[str, list[Any]] = {}
    LOGGER.debug(
        "Langfuse command metric mapping start commands=%s observations=%s",
        [_short_id(command.id) for command in commands],
        len(observations),
    )
    for observation in observations:
        parent_id = _optional_str(
            _field(observation, "parent_observation_id", "parentObservationId")
        )
        if parent_id is not None:
            children_by_parent.setdefault(parent_id, []).append(observation)
    for command in commands:
        descendants = _descendant_observations(
            _command_observation_id(command),
            children_by_parent,
        )
        descendant_models = [
            model
            for observation in descendants
            for model in _model_candidates(observation)
        ]
        LOGGER.debug(
            "Langfuse command metric mapping command_id=%s descendants=%s "
            "descendant_models=%s descendant_ids=%s",
            _short_id(command.id),
            len(descendants),
            descendant_models,
            [
                _short_id(str(_field(observation, "id") or ""))
                for observation in descendants
            ],
        )


def _descendant_observations(
    observation_id: str,
    children_by_parent: dict[str, list[Any]],
) -> list[Any]:
    descendants: list[Any] = []
    stack = list(children_by_parent.get(observation_id, []))
    while stack:
        observation = stack.pop()
        descendants.append(observation)
        child_id = str(_field(observation, "id") or "")
        if child_id:
            stack.extend(children_by_parent.get(child_id, []))
    return descendants


def _command_observation(
    command: CommandSummary, observations: Sequence[Any]
) -> Any | None:
    observation_id = _command_observation_id(command)
    for observation in observations:
        if str(_field(observation, "id") or "") == observation_id:
            return observation
    return None


def _command_observation_id(command: CommandSummary) -> str:
    return command.observation_id or command.id


def _model_candidates(observation: Any) -> list[str]:
    display_candidates = [
        _field(observation, "provided_model_name", "providedModelName"),
        _field(observation, "model"),
    ]
    metadata = _field(observation, "metadata")
    if isinstance(metadata, dict):
        display_candidates.extend(
            [
                metadata.get("model"),
                metadata.get("model_name"),
                metadata.get("ls_model_name"),
                metadata.get("dreamland.model"),
            ]
        )
    display_models = [
        candidate
        for candidate in display_candidates
        if isinstance(candidate, str) and candidate
    ]
    if display_models:
        return display_models
    fallback_candidates = [
        _field(observation, "model_id", "modelId"),
        _field(observation, "internal_model_id", "internalModelId"),
    ]
    return [
        candidate
        for candidate in fallback_candidates
        if isinstance(candidate, str) and candidate
    ]


def _dict_keys(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return sorted(str(key) for key in value)


def _merge_one_command(
    command: CommandSummary,
    root_observation: Any | None,
    child_observations: Sequence[Any],
) -> CommandSummary:
    llm_children = [
        observation
        for observation in child_observations
        if _model_candidates(observation)
    ]
    if not llm_children:
        return _ensure_model(command)
    models = [
        model for child in llm_children for model in _model_candidates(child) if model
    ]
    input_tokens = _sum_optional_ints(
        _usage_token(child, "input", "input_tokens") for child in llm_children
    )
    output_tokens = _sum_optional_ints(
        _usage_token(child, "output", "output_tokens") for child in llm_children
    )
    total_tokens = _sum_optional_ints(
        _usage_token(child, "total", "total_tokens") for child in llm_children
    )
    total_cost = _sum_optional_floats(
        _cost_from_observation(child) for child in llm_children
    )
    reasoning = any(_reasoning_tokens(child) > 0 for child in llm_children)
    summary_observations = (
        [root_observation, *child_observations]
        if root_observation is not None
        else list(child_observations)
    )
    summary_found = any(
        _reasoning_summary_from_observations([child]) is not None
        for child in summary_observations
    )
    summary: bool | None = command.summary or summary_found
    if not summary and reasoning and not _observations_include_io(summary_observations):
        summary = None
    return replace(
        command,
        model=", ".join(dict.fromkeys(models)) or "local",
        input_tokens=input_tokens if input_tokens is not None else command.input_tokens,
        output_tokens=(
            output_tokens if output_tokens is not None else command.output_tokens
        ),
        total_tokens=total_tokens if total_tokens is not None else command.total_tokens,
        total_cost=total_cost if total_cost is not None else command.total_cost,
        reasoning=command.reasoning or reasoning,
        summary=summary,
    )


def _ensure_model(command: CommandSummary) -> CommandSummary:
    return command if command.model else replace(command, model="local")


def _usage_token(observation: Any, *keys: str) -> int | None:
    usage = _optional_dict(_field(observation, "usage_details", "usageDetails")) or {}
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return None


def _reasoning_tokens(observation: Any) -> int:
    usage = _optional_dict(_field(observation, "usage_details", "usageDetails")) or {}
    for key in ("output_reasoning", "reasoning", "reasoning_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, dict):
        value = output_details.get("reasoning_tokens")
        if isinstance(value, int):
            return value
    return 0


def _observations_include_io(observations: Sequence[Any]) -> bool:
    return any(
        _field(observation, "input") is not None
        or _field(observation, "output") is not None
        for observation in observations
    )


def _cost_from_observation(observation: Any) -> float | None:
    total_cost = _optional_float(_field(observation, "total_cost", "totalCost"))
    if total_cost is not None:
        return total_cost
    cost_details = _field(observation, "cost_details", "costDetails")
    if not isinstance(cost_details, dict):
        return None
    total = cost_details.get("total") or cost_details.get("total_cost")
    return _optional_float(total)


def _sum_optional_ints(values: Iterable[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _sum_optional_floats(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _resolve_command(
    commands: Sequence[CommandSummary],
    target: str | None,
) -> CommandSummary:
    if not commands:
        raise IntrospectionUnavailable("No commands found for the active session.")
    if target is None:
        return commands[0]
    if target.startswith("@"):
        return _resolve_relative_command(commands, target)
    matches = [
        command
        for command in commands
        if command.id == target
        or command.id.startswith(target)
        or command.observation_id == target
        or (command.observation_id or "").startswith(target)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise IntrospectionUnavailable(f"No command matches {target!r}.")
    raise IntrospectionUnavailable(f"Command id prefix {target!r} is ambiguous.")


def _resolve_relative_command(
    commands: Sequence[CommandSummary],
    target: str,
) -> CommandSummary:
    if not target.startswith("@-"):
        raise IntrospectionUnavailable(
            "Relative command positions must use @-N, for example @-1."
        )
    try:
        position = int(target[1:])
    except ValueError as exc:
        raise IntrospectionUnavailable(
            "Relative command positions must use @-N, for example @-1."
        ) from exc
    if position >= 0:
        raise IntrospectionUnavailable(
            "Relative command positions must use @-N, for example @-1."
        )
    index = abs(position) - 1
    if index < 0 or index >= len(commands):
        raise IntrospectionUnavailable(f"Command position {target!r} is out of range.")
    return commands[index]


def _sort_commands(
    commands: list[CommandSummary],
    *,
    sort: str,
    descending: bool,
) -> list[CommandSummary]:
    key_name = {
        "time": "timestamp",
        "command": "command",
        "tokens": "total_tokens",
        "cost": "total_cost",
        "latency": "latency",
    }.get(sort, "timestamp")
    return sorted(
        commands,
        key=lambda command: _sort_value(getattr(command, key_name)),
        reverse=descending,
    )


def _sort_value(value: object) -> tuple[int, object]:
    if value is None:
        return 0, ""
    return 1, value


def _render_observation(observation: ObservationSummary) -> str:
    lines = [
        f"- {observation.type} {_short_id(observation.id)} {observation.name or ''}".rstrip(),
        f"  model: {observation.model or ''}",
        f"  tokens: {observation.usage or {}}",
        f"  cost: {_money(observation.total_cost)}",
        f"  latency: {_seconds(observation.latency)}",
        "  input:",
        _indent(render_value(observation.input), "    "),
        "  output:",
        _indent(render_value(observation.output), "    "),
    ]
    return "\n".join(lines)


def _command_column(command: CommandSummary, index: int, column: str) -> str:
    if column == "pos":
        return str(index)
    if column == "id":
        return command.id
    if column == "time":
        return _format_time(command.timestamp)
    if column == "command":
        return command.command
    if column == "status":
        return command.status or ""
    if column == "model":
        return command.model or ""
    if column == "tokens":
        return _tokens(command)
    if column == "cost":
        return _money(command.total_cost)
    if column == "latency":
        return _seconds(command.latency)
    if column == "session":
        return _short_id(command.session_id or "")
    if column == "reasoning":
        return _bool_flag(command.reasoning)
    if column == "summary":
        return _summary_flag(command.summary)
    return ""


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    safe_rows = [[_safe_cell(value) for value in row] for row in rows]
    widths = [
        max([len(header), *(len(str(row[index])) for row in safe_rows)])
        for index, header in enumerate(headers)
    ]
    header_line = "  ".join(
        header.ljust(widths[index]) for index, header in enumerate(headers)
    )
    divider = "  ".join("-" * width for width in widths)
    body = [
        "  ".join(str(value).ljust(widths[index]) for index, value in enumerate(row))
        for row in safe_rows
    ]
    return "\n".join([header_line, divider, *body]).rstrip() + "\n"


def _render_pretty(value: Any, *, indent: int, depth: int) -> str:
    if depth > MAX_RENDER_DEPTH:
        return '"[max depth reached]"'
    value = _decode_json_string(value)
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for key, item in value.items():
            if isinstance(item, str) and "\n" in item:
                lines.append(f"{prefix}{json.dumps(str(key))}: |")
                lines.append(_indent(item.rstrip("\n"), " " * (indent + 2)))
            elif isinstance(item, (dict, list)):
                lines.append(f"{prefix}{json.dumps(str(key))}:")
                lines.append(_render_pretty(item, indent=indent + 2, depth=depth + 1))
            else:
                lines.append(
                    f"{prefix}{json.dumps(str(key))}: "
                    f"{_render_pretty(item, indent=indent + 2, depth=depth + 1).strip()}"
                )
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = []
        for item in value:
            if isinstance(item, str) and "\n" in item:
                lines.append(f"{prefix}- |")
                lines.append(_indent(item.rstrip("\n"), " " * (indent + 2)))
            elif isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.append(_render_pretty(item, indent=indent + 2, depth=depth + 1))
            else:
                lines.append(
                    f"{prefix}- {_render_pretty(item, indent=indent + 2, depth=depth + 1).strip()}"
                )
        return "\n".join(lines)
    if isinstance(value, str):
        return value if "\n" in value else json.dumps(value)
    return json.dumps(value)


def _canonical_state_text(value: Any) -> str:
    parsed = _decode_json_string(value)
    try:
        return json.dumps(
            parsed,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
    except TypeError:
        return render_value(parsed)


def _color_diff_line(line: str, *, color: str) -> str:
    if not _color_enabled(color):
        return line
    if line.startswith("@@"):
        return f"\033[36m{line}\033[0m"
    if line.startswith("+") and not line.startswith("+++"):
        return f"\033[32m{line}\033[0m"
    if line.startswith("-") and not line.startswith("---"):
        return f"\033[31m{line}\033[0m"
    if line.startswith(("---", "+++")):
        return f"\033[1m{line}\033[0m"
    return line


def _color_enabled(color: str) -> bool:
    if color == "always":
        return True
    if color == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def _decode_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if len(stripped) > MAX_RENDER_CHARS:
        return value
    if not stripped or stripped[0] not in '[{"':
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _session_filter_json(session_id: str) -> str:
    return json.dumps(
        [
            {
                "type": "string",
                "column": "sessionId",
                "operator": "=",
                "value": session_id,
            }
        ],
        separators=(",", ":"),
    )


def _id_filter_json(observation_id: str) -> str:
    return json.dumps(
        [
            {
                "type": "string",
                "column": "id",
                "operator": "=",
                "value": observation_id,
            }
        ],
        separators=(",", ":"),
    )


def _field(value: Any, *names: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(by_alias=True)
        for name in names:
            if name in dumped:
                return dumped[name]
    return default


def _metadata_value(metadata: Any, key: str) -> str | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    return value if isinstance(value, str) else None


def _fallback_command(trace: Any) -> str:
    trace_input = _field(trace, "input")
    if isinstance(trace_input, dict):
        command = trace_input.get("current_command") or trace_input.get("command")
        if isinstance(command, str):
            return command
    return ""


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _optional_dict(value: Any) -> dict[str, int] | None:
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    return None


def _short_id(value: str) -> str:
    return value[:12] if len(value) > 12 else value


def _bool_flag(value: bool) -> str:
    return "yes" if value else "no"


def _summary_flag(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return _bool_flag(value)


def _key_suffix(value: str | None) -> str:
    if not value:
        return ""
    return value[-6:]


def _safe_cell(value: object) -> str:
    text = str(value)
    escaped = "".join(_safe_cell_char(character) for character in text)
    if len(escaped) <= MAX_TABLE_CELL_CHARS:
        return escaped
    return escaped[: MAX_TABLE_CELL_CHARS - 1] + "…"


def _safe_cell_char(character: str) -> str:
    codepoint = ord(character)
    if character == "\n":
        return "\\n"
    if character == "\r":
        return "\\r"
    if character == "\t":
        return "\\t"
    if codepoint < 32 or codepoint == 127:
        return f"\\x{codepoint:02x}"
    return character


def _format_time(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value or ""


def _tokens(command: CommandSummary) -> str:
    if command.total_tokens is not None:
        return str(command.total_tokens)
    if command.input_tokens is not None or command.output_tokens is not None:
        return f"{command.input_tokens or 0}/{command.output_tokens or 0}"
    return ""


def _money(value: float | None) -> str:
    return "" if value is None else f"${value:.6f}"


def _seconds(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}s"


def _indent(value: str, prefix: str) -> str:
    if not value:
        return prefix.rstrip()
    return "\n".join(
        f"{prefix}{line}" if line else prefix.rstrip() for line in value.splitlines()
    )


_UNAVAILABLE_MESSAGE = (
    "Langfuse introspection is disabled. Enable [langfuse] and configure "
    "public_key and secret_key to use this command."
)
