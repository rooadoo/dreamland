from __future__ import annotations

import argparse
import json
import logging
import shlex
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Never

from dreamland.agent_state import (
    host_identity_to_state,
    interactive_program_to_state,
    shell_environment_to_state,
    shell_frame_to_state,
)
from dreamland.background_agents.enrichment.control import (
    EnrichmentControl,
    EnrichmentJobSnapshot,
    EnrichmentRuntimeStatus,
    EnrichmentSubmission,
)
from dreamland.config import (
    ConsoleSettings,
    LangfuseSettings,
    persist_state_profile_setting,
)
from dreamland.console_introspection import (
    DEFAULT_COMMAND_LIMIT,
    DEFAULT_SESSION_LIMIT,
    ConsoleIntrospection,
    IntrospectionUnavailable,
    LangfuseConsoleIntrospection,
    SessionList,
    render_commands,
    render_inspection,
    render_sessions,
    render_state_delta,
)
from dreamland.lobby_catalog import LobbyCatalog, LobbyCatalogError
from dreamland.lobby_identity import PublicSimulationRegistry, is_public_reference
from dreamland.models import CommandResult
from dreamland.runtime_logging import (
    LogRecordBuffer,
    SessionTraceLogBuffer,
    trace_spiritguide,
    trace_state_profile_apply,
)
from dreamland.session_metadata import (
    normalize_session_tags,
    session_label_from_metadata,
    session_origin_from_metadata,
    session_tags_from_metadata,
)
from dreamland.session_registry import LiveSessionRegistry
from dreamland.session_state import apply_external_state_update
from dreamland.spiritguide import (
    DeterministicSpiritguide,
    Spiritguide,
    SpiritguideContext,
    SpiritguideResult,
)
from dreamland.spiritguide_profiles import (
    SpiritguideProfileOperationResult,
    create_spiritguide_profile,
    fork_spiritguide_profile,
    update_spiritguide_profile,
)
from dreamland.state_profiles import (
    STATE_PROFILE_LEVELS,
    STATE_PROFILE_METADATA_LEVEL,
    STATE_PROFILE_METADATA_NAME,
    StateProfile,
    StateProfileError,
    StateProfileRuntime,
    StateProfileStore,
    StateProfileSummary,
    apply_state_profile_to_session,
    create_state_profile,
)
from dreamland.structured_failures import (
    StructuredFailureCapsule,
    StructuredFailureError,
    StructuredFailureStore,
    StructuredFailureSummary,
    replay_structured_failure_parser,
)

LOGGER = logging.getLogger("dreamland.console")
CONSOLE_PREFIX = "!dreamland"
CONSOLE_PROMPT = "dreamland> "
ENRICHMENT_STATUS_REFRESH_SECONDS = 1.0
CONSOLE_REFRESH_ENTER_SCREEN = "\x1b[?1049h\x1b[H\x1b[2J"
CONSOLE_REFRESH_EXIT_SCREEN = "\x1b[?1049l"


@dataclass(frozen=True, slots=True)
class ConsoleRefresh:
    """A transport-rendered live console frame.

    ``DreamlandConsole`` owns the inexpensive snapshot/render callback while
    local and SSH transports own terminal capability, timing, input, and
    cursor control.  Keeping those responsibilities separate prevents a live
    operator view from blocking the enrichment runtime itself.
    """

    name: str
    render: Callable[[], str] = field(repr=False, compare=False)
    interval_seconds: float = ENRICHMENT_STATUS_REFRESH_SECONDS

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("console refresh name must be non-empty")
        if self.interval_seconds <= 0:
            raise ValueError("console refresh interval must be positive")


def redraw_console_frame(current_frame: str) -> str:
    """Return ANSI output that replaces one previously rendered frame."""

    return f"\x1b[H\x1b[2J{current_frame}"


def console_refresh_terminal_error(refresh: ConsoleRefresh) -> str:
    """Render the stable error used when a refresh has no real terminal."""

    command_name = refresh.name.replace("_", " ")
    return f"error: {command_name} --follow requires an interactive terminal\n"


@dataclass(frozen=True, slots=True)
class ConsoleResult:
    output: str = ""
    follow: bool = False
    trace_session_id: str | None = None
    follow_trace_session_id: str | None = None
    refresh: ConsoleRefresh | None = None
    leave: bool = False
    shutdown: bool = False


@dataclass(slots=True)
class ConsoleContext:
    active_session_id: str | None = None
    default_session_id: str | None = None
    recent_session_ids: tuple[str, ...] = field(default_factory=tuple)

    def selected_session_id(self) -> str | None:
        return self.active_session_id or self.default_session_id

    def remember_sessions(self, session_ids: list[str]) -> None:
        self.recent_session_ids = tuple(session_ids)

    def resolve_session_id(self, session_id: str) -> tuple[str | None, str | None]:
        if session_id in self.recent_session_ids:
            return session_id, None
        matches = [
            known_session_id
            for known_session_id in self.recent_session_ids
            if known_session_id.startswith(session_id)
        ]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, f"session id prefix {session_id!r} is ambiguous"
        return session_id, None


class ConsoleCommandParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"{message}\n{self.format_usage()}")

    def exit(self, status: int = 0, message: str | None = None) -> Never:
        raise ConsoleHelp(message or self.format_help())

    def print_help(self, file: object | None = None) -> Never:
        raise ConsoleHelp(self.format_help())


class ConsoleHelp(Exception):
    pass


class DreamlandConsole:
    def __init__(
        self,
        settings: ConsoleSettings,
        logs: LogRecordBuffer,
        request_shutdown: Callable[[], None],
        *,
        trace_logs: SessionTraceLogBuffer | None = None,
        langfuse_settings: LangfuseSettings | None = None,
        introspection: ConsoleIntrospection | None = None,
        session_registry: LiveSessionRegistry | None = None,
        spiritguide: Spiritguide | None = None,
        state_profiles: StateProfileStore | None = None,
        state_profile_runtime: StateProfileRuntime | None = None,
        structured_failure_store: StructuredFailureStore | None = None,
        enrichment_control: EnrichmentControl | None = None,
        config_path: Path | str | None = None,
        lobby_catalog: LobbyCatalog | None = None,
        public_simulations: PublicSimulationRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.logs = logs
        self.trace_logs = trace_logs or SessionTraceLogBuffer(settings.log_capacity)
        self.session_registry = session_registry or LiveSessionRegistry()
        self.spiritguide = spiritguide or DeterministicSpiritguide()
        self.state_profile_runtime = state_profile_runtime or StateProfileRuntime(
            store=state_profiles
        )
        self.state_profiles = self.state_profile_runtime.store
        self.structured_failures = structured_failure_store or StructuredFailureStore()
        self.enrichment_control = enrichment_control
        self.config_path = config_path
        self.lobby_catalog = lobby_catalog
        self.public_simulations = public_simulations
        self.langfuse_settings = langfuse_settings
        self._request_shutdown = request_shutdown
        self._shutdown_event = threading.Event()
        self._parser = _build_parser()
        self.introspection = (
            introspection
            if introspection is not None
            else LangfuseConsoleIntrospection.from_settings(langfuse_settings)
        )

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    def request_shutdown(self) -> None:
        self._shutdown_event.set()
        self._request_shutdown()

    def execute(
        self,
        command: str,
        *,
        allow_exit: bool,
        context: ConsoleContext | None = None,
    ) -> ConsoleResult:
        context = context or ConsoleContext()
        try:
            arguments = shlex.split(command)
        except ValueError as exc:
            return ConsoleResult(output=f"error: {exc}\n")
        if arguments and arguments[0] == CONSOLE_PREFIX:
            arguments = arguments[1:]
        if not arguments:
            return ConsoleResult()
        if arguments[0] == "exit":
            return ConsoleResult(
                output="error: exit is not a Dreamland console command\n"
            )
        try:
            namespace = self._parser.parse_args(arguments)
            if namespace.command in {"state-delta", "state-diff", "delta"}:
                _normalize_state_delta_targets(namespace)
        except ConsoleHelp as exc:
            return ConsoleResult(output=str(exc))
        except ValueError as exc:
            return ConsoleResult(output=f"error: {exc}\n")

        if namespace.command == "lobby":
            return self._execute_lobby(namespace)
        if namespace.command == "logging":
            records = self.logs.latest(namespace.count)
            output = "".join(f"{record.text}\n" for record in records)
            LOGGER.info(
                "Console logging requested count=%s follow=%s",
                namespace.count,
                namespace.follow,
            )
            return ConsoleResult(output=output, follow=namespace.follow)
        if namespace.command == "trace":
            return self._execute_trace(namespace, context)
        if namespace.command in {"sessions", "session"}:
            return self._execute_sessions(namespace, context)
        if namespace.command == "commands":
            return self._execute_commands(namespace, context)
        if namespace.command in {"inspection", "inspect"}:
            return self._execute_inspection(namespace, context)
        if namespace.command in {"state-delta", "state-diff", "delta"}:
            return self._execute_state_delta(namespace, context)
        if namespace.command == "state-profile":
            return self._execute_state_profile(namespace, context)
        if namespace.command in {"failures", "structured-failures"}:
            return self._execute_failures(namespace)
        if namespace.command == "spiritguide":
            return self._execute_spiritguide(namespace, context)
        if namespace.command == "enrichment":
            return self._execute_enrichment(namespace, context)
        if namespace.command == "langfuse":
            return self._execute_langfuse(namespace)
        if namespace.command == "shutdown":
            LOGGER.warning("Dreamland shutdown requested from console")
            self.request_shutdown()
            return ConsoleResult(
                output="Dreamland shutdown requested.\n", shutdown=True
            )
        return ConsoleResult(output=self._parser.format_help())

    def _resolve_session_id(
        self,
        context: ConsoleContext,
        reference: str,
        *,
        live: bool = False,
    ) -> tuple[str | None, str | None]:
        # Public references never enter the legacy prefix/fallback resolver.
        if is_public_reference(reference):
            if self.public_simulations is None:
                return None, "public simulation lookup is unavailable"
            try:
                record = self.public_simulations.get(reference)
            except ValueError as exc:
                return None, str(exc)
            if record is None:
                return None, "unknown or expired public simulation ID"
            if live and record.status != "live":
                return (
                    None,
                    "simulation visit has ended; live operations are unavailable",
                )
            if record.session_id is None:
                return None, "simulation did not start; no session trace is available"
            return record.session_id, None
        resolved, error = context.resolve_session_id(reference)
        if live and resolved and error is None and self.public_simulations is not None:
            record = self.public_simulations.get_by_session_id(resolved)
            if record is not None and record.status != "live":
                return (
                    None,
                    "simulation visit has ended; live operations are unavailable",
                )
        return resolved, error

    def _public_id_for_session(self, session_id: str) -> str | None:
        if self.public_simulations is None:
            return None
        record = self.public_simulations.get_by_session_id(session_id)
        return record.public_id if record is not None else None

    def _execute_lobby(self, namespace: argparse.Namespace) -> ConsoleResult:
        catalog = self.lobby_catalog
        if catalog is None:
            return ConsoleResult(
                output="error: lobby catalog is unavailable; configure lobby entry mode\n"
            )
        try:
            action = namespace.lobby_action
            if action == "add":
                catalog.add(
                    namespace.profile,
                    title=namespace.title,
                    description=namespace.description,
                    entry_username=namespace.entry_username,
                )
            elif action == "update":
                catalog.update(
                    namespace.profile,
                    title=namespace.title,
                    description=namespace.description,
                    entry_username=namespace.entry_username,
                )
            elif action == "order":
                catalog.order(namespace.profiles)
            elif action == "remove":
                catalog.remove(namespace.profile)
            elif action == "reload":
                catalog.reload()
            elif action != "list":
                return ConsoleResult(
                    output="Use lobby worlds list, add, update, order, remove, or reload.\n"
                )
            snapshot = catalog.snapshot()
        except (LobbyCatalogError, OSError) as exc:
            return ConsoleResult(output=f"error: {_safe_console_cell(str(exc))}\n")
        LOGGER.info(
            "Lobby catalog action=%s revision=%s count=%s",
            action,
            snapshot.revision,
            len(snapshot.entries),
        )
        rows = [
            [
                str(index),
                entry.profile,
                entry.entry_username,
                entry.title,
                entry.description,
            ]
            for index, entry in enumerate(snapshot.entries, 1)
        ]
        return ConsoleResult(
            output=_plain_table(
                ["order", "profile", "entry_username", "title", "description"], rows
            )
            if rows
            else "No lobby worlds configured.\n"
        )

    def _execute_trace(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        requested_session_id = namespace.session_id or context.selected_session_id()
        session_id = None
        if requested_session_id is not None:
            session_id, error = self._resolve_session_id(context, requested_session_id)
            if error is not None:
                return ConsoleResult(output=f"error: {error}\n")
        if session_id is None:
            return ConsoleResult(
                output=(
                    "error: no active session; run sessions --set SESSION_ID "
                    "or pass a session id to trace\n"
                )
            )
        records = self.trace_logs.latest(session_id, namespace.count)
        output = "".join(f"{record.text}\n" for record in records)
        if not records and self.public_simulations is not None:
            record = self.public_simulations.get_by_session_id(session_id)
            if record is not None:
                output = f"{record.public_id}: {record.status}. No local trace data retained; historical traces may be available in Langfuse.\n"
        LOGGER.info(
            "Console trace requested session=%s count=%s follow=%s",
            session_id,
            namespace.count,
            namespace.follow,
        )
        return ConsoleResult(
            output=output,
            follow=namespace.follow,
            trace_session_id=session_id,
            follow_trace_session_id=session_id if namespace.follow else None,
        )

    def _execute_sessions(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        if namespace.clear:
            context.active_session_id = None
            return ConsoleResult(output="Active session cleared.\n")
        if namespace.set_session:
            session_id, error = self._resolve_session_id(context, namespace.set_session)
            if error is not None:
                return ConsoleResult(output=f"error: {error}\n")
            context.active_session_id = session_id
            suffix = (
                f" (matched {namespace.set_session})"
                if session_id != namespace.set_session
                else ""
            )
            return ConsoleResult(
                output=f"Active session set to {session_id}{suffix}.\n"
            )
        if namespace.close_session:
            return self._execute_live_session_close(namespace, context)
        if namespace.close_matching:
            return self._execute_live_session_close_matching(namespace, context)
        if namespace.live or namespace.tags or namespace.label or namespace.origin:
            sessions = self._live_session_rows(
                tags=namespace.tags,
                label=namespace.label,
                origin=namespace.origin,
            )
            context.remember_sessions(
                [str(session["session_id"]) for session in sessions]
            )
            return ConsoleResult(
                output=_render_live_sessions(
                    sessions,
                    active_session_id=context.selected_session_id(),
                )
            )
        try:
            if namespace.flush:
                self.introspection.flush()
            historical_sessions = self.introspection.list_sessions(
                limit=namespace.count
            )
        except IntrospectionUnavailable as exc:
            return ConsoleResult(output=f"error: {exc}\n")
        historical_sessions = self._with_live_session_profiles(historical_sessions)
        context.remember_sessions(
            [session.id for session in historical_sessions.sessions]
        )
        output = render_sessions(
            historical_sessions, active_session_id=context.selected_session_id()
        )
        if self.public_simulations is not None:
            for session in historical_sessions.sessions:
                record = self.public_simulations.get_by_session_id(session.id)
                if record is not None:
                    output += f"{record.public_id} session={session.id} status={record.status}\n"
        return ConsoleResult(output=output)

    def _execute_live_session_close(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        session_id, error = self._resolve_session_id(
            context, namespace.close_session, live=True
        )
        if error is not None:
            return ConsoleResult(output=f"error: {error}\n")
        registration = self.session_registry.get(session_id)
        if registration is None:
            return ConsoleResult(
                output=f"error: live session {session_id!r} not found\n"
            )
        if namespace.dry_run:
            return ConsoleResult(output=f"Would close live session {session_id}.\n")
        self.session_registry.unregister(session_id, session=registration.session)
        if context.active_session_id == session_id:
            context.active_session_id = None
        LOGGER.warning("Live session closed from console session=%s", session_id)
        return ConsoleResult(output=f"Closed live session {session_id}.\n")

    def _execute_live_session_close_matching(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        if not _has_live_session_filters(namespace):
            return ConsoleResult(
                output=(
                    "error: --close-matching requires --tag, --label, or --origin\n"
                )
            )
        sessions = self._live_session_rows(
            tags=namespace.tags,
            label=namespace.label,
            origin=namespace.origin,
        )
        if namespace.dry_run:
            return ConsoleResult(
                output=_render_close_matching_preview(sessions, dry_run=True)
            )
        closed: list[str] = []
        for session in sessions:
            session_id = session["session_id"]
            registration = self.session_registry.get(session_id)
            if registration is None:
                continue
            self.session_registry.unregister(session_id, session=registration.session)
            closed.append(session_id)
        if context.active_session_id in closed:
            context.active_session_id = None
        LOGGER.warning(
            "Live sessions closed from console sessions=%s", ",".join(closed)
        )
        return ConsoleResult(
            output=_render_close_matching_preview(sessions, dry_run=False)
        )

    def _with_live_session_profiles(self, sessions: SessionList) -> SessionList:
        enriched = []
        for session in sessions.sessions:
            registration = self.session_registry.get(session.id)
            if registration is None:
                indexed = self._indexed_state_profile(session.id)
                if indexed is not None:
                    enriched.append(replace(session, state_profile=indexed))
                else:
                    enriched.append(session)
                continue
            metadata = registration.session.environment_metadata.metadata
            profile_name = metadata.get(STATE_PROFILE_METADATA_NAME)
            profile_level = metadata.get(STATE_PROFILE_METADATA_LEVEL)
            if profile_name is None:
                indexed = self._indexed_state_profile(session.id)
                if indexed is not None:
                    enriched.append(replace(session, state_profile=indexed))
                else:
                    enriched.append(session)
                continue
            label = str(profile_name)
            if profile_level is not None:
                label = f"{label}:{profile_level}"
            enriched.append(replace(session, state_profile=label))
        return SessionList(sessions=enriched, total=sessions.total)

    def _live_session_rows(
        self,
        *,
        tags: list[str] | None,
        label: str | None,
        origin: str | None,
    ) -> list[dict[str, object]]:
        expected_tags = normalize_session_tags(tags)
        expected_label = label.strip() if isinstance(label, str) else ""
        expected_origin = origin.strip() if isinstance(origin, str) else ""
        rows: list[dict[str, object]] = []
        for registration in self.session_registry.list_sessions():
            with registration.lock:
                session = registration.session
                metadata = session.environment_metadata.metadata
                session_tags = list(session_tags_from_metadata(metadata))
                session_label = session_label_from_metadata(metadata)
                session_origin = session_origin_from_metadata(metadata)
                if expected_tags and not all(
                    tag in session_tags for tag in expected_tags
                ):
                    continue
                if expected_label and session_label != expected_label:
                    continue
                if expected_origin and session_origin != expected_origin:
                    continue
                rows.append(
                    {
                        "session_id": session.langfuse_trace_id,
                        "public_id": self._public_id_for_session(
                            session.langfuse_trace_id
                        ),
                        "username": session.username,
                        "login_username": session.login_username,
                        "hostname": session.host_identity.hostname,
                        "cwd": session.cwd,
                        "history_count": len(session.history),
                        "state_profile": self._indexed_or_live_state_profile(
                            session.langfuse_trace_id,
                            metadata,
                        ),
                        "tags": session_tags,
                        "label": session_label,
                        "origin": session_origin,
                    }
                )
        return rows

    def _indexed_or_live_state_profile(
        self,
        session_id: str,
        metadata: dict[str, object],
    ) -> str | None:
        profile_name = metadata.get(STATE_PROFILE_METADATA_NAME)
        profile_level = metadata.get(STATE_PROFILE_METADATA_LEVEL)
        if profile_name is not None:
            label = str(profile_name)
            if profile_level is not None:
                label = f"{label}:{profile_level}"
            return label
        return self._indexed_state_profile(session_id)

    def _indexed_state_profile(self, session_id: str) -> str | None:
        try:
            indexed = self.state_profiles.session_profile(session_id)
        except StateProfileError:
            return None
        return indexed.label if indexed is not None else None

    def _execute_commands(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        requested_session_id = namespace.session_id or context.selected_session_id()
        session_id = None
        if requested_session_id is not None:
            session_id, error = self._resolve_session_id(context, requested_session_id)
            if error is not None:
                return ConsoleResult(output=f"error: {error}\n")
        if session_id is None:
            return ConsoleResult(
                output=(
                    "error: no active session; run sessions --set SESSION_ID "
                    "or pass --session SESSION_ID\n"
                )
            )
        try:
            if namespace.flush:
                self.introspection.flush()
            if session_id != requested_session_id:
                LOGGER.debug(
                    "Resolved console session id requested=%s resolved=%s",
                    requested_session_id,
                    session_id,
                )
            commands = self.introspection.list_commands(
                session_id=session_id,
                limit=namespace.count,
                filter_text=namespace.filter_text,
                sort=namespace.sort,
                descending=not namespace.asc,
                include_io_summary=not namespace.quick,
            )
        except IntrospectionUnavailable as exc:
            return ConsoleResult(output=f"error: {exc}\n")
        return ConsoleResult(
            output=render_commands(
                commands,
                columns=_columns(namespace.columns),
            )
        )

    def _execute_inspection(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        requested_session_id = context.selected_session_id()
        session_id = None
        if requested_session_id is not None:
            session_id, error = self._resolve_session_id(context, requested_session_id)
            if error is not None:
                return ConsoleResult(output=f"error: {error}\n")
        if session_id is None:
            return ConsoleResult(
                output=("error: no active session; run sessions --set SESSION_ID\n")
            )
        explicit_output_mode = namespace.output or namespace.reasoning
        show_prompt = namespace.prompt or namespace.all or not explicit_output_mode
        show_output = (
            namespace.output
            or namespace.all
            or (not namespace.prompt and not namespace.reasoning)
        )
        try:
            if namespace.flush:
                self.introspection.flush()
            if session_id != requested_session_id:
                LOGGER.debug(
                    "Resolved console session id requested=%s resolved=%s",
                    requested_session_id,
                    session_id,
                )
            inspection = self.introspection.inspect_command(
                session_id=session_id,
                target=namespace.target,
                limit=namespace.count,
            )
        except IntrospectionUnavailable as exc:
            return ConsoleResult(output=f"error: {exc}\n")
        return ConsoleResult(
            output=render_inspection(
                inspection,
                show_prompt=show_prompt,
                show_output=show_output,
                show_reasoning=namespace.reasoning or namespace.all,
                show_metadata=namespace.metadata or namespace.all,
                show_cost=namespace.cost or namespace.all,
                show_tokens=namespace.tokens or namespace.all,
                show_timing=namespace.timing or namespace.all,
                show_observations=namespace.observations or namespace.all,
            )
        )

    def _execute_state_delta(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        requested_session_id = context.selected_session_id()
        session_id = None
        if requested_session_id is not None:
            session_id, error = self._resolve_session_id(context, requested_session_id)
            if error is not None:
                return ConsoleResult(output=f"error: {error}\n")
        if session_id is None:
            return ConsoleResult(
                output=("error: no active session; run sessions --set SESSION_ID\n")
            )
        try:
            if namespace.flush:
                self.introspection.flush()
            if session_id != requested_session_id:
                LOGGER.debug(
                    "Resolved console session id requested=%s resolved=%s",
                    requested_session_id,
                    session_id,
                )
            state_delta = self.introspection.state_delta(
                session_id=session_id,
                from_target=namespace.from_target,
                to_target=namespace.to_target,
                limit=namespace.count,
                sections=namespace.sections or (),
            )
        except IntrospectionUnavailable as exc:
            return ConsoleResult(output=f"error: {exc}\n")
        return ConsoleResult(
            output=render_state_delta(state_delta, color=namespace.color)
        )

    def _execute_langfuse(self, namespace: argparse.Namespace) -> ConsoleResult:
        if namespace.langfuse_command == "flush":
            try:
                self.introspection.flush()
            except IntrospectionUnavailable as exc:
                return ConsoleResult(output=f"error: {exc}\n")
            LOGGER.info("Langfuse flush requested from console")
            return ConsoleResult(output="Langfuse flush complete.\n")
        return ConsoleResult(output=self._parser.format_help())

    def _execute_state_profile(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        if namespace.state_profile_command == "save":
            return self._execute_state_profile_save(namespace, context)
        if namespace.state_profile_command == "list":
            summaries = self.state_profiles.list()
            LOGGER.info("State profiles listed count=%s", len(summaries))
            return ConsoleResult(output=_render_state_profile_list(summaries))
        if namespace.state_profile_command == "show":
            try:
                profile = self.state_profiles.load(namespace.name)
            except StateProfileError as exc:
                return ConsoleResult(output=f"error: {exc}\n")
            LOGGER.info(
                "State profile shown name=%s level=%s json=%s",
                profile.name,
                profile.level,
                namespace.json,
            )
            return ConsoleResult(
                output=_render_state_profile(profile, show_json=namespace.json)
            )
        if namespace.state_profile_command == "apply":
            return self._execute_state_profile_apply(namespace, context)
        if namespace.state_profile_command == "default":
            return self._execute_state_profile_default(namespace)
        if namespace.state_profile_command == "delete":
            return self._execute_state_profile_delete(namespace)
        return ConsoleResult(output=self._parser.format_help())

    def _execute_state_profile_save(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        requested_session_id = namespace.session_id or context.selected_session_id()
        session_id = None
        if requested_session_id is not None:
            session_id, error = self._resolve_session_id(
                context, requested_session_id, live=True
            )
            if error is not None:
                return ConsoleResult(output=f"error: {error}\n")
        if session_id is None:
            live_sessions = self.session_registry.list_sessions()
            if len(live_sessions) == 1:
                session_id = live_sessions[0].session_id
            else:
                return ConsoleResult(
                    output=(
                        "error: no active session; run sessions --set SESSION_ID "
                        "or pass --session SESSION_ID\n"
                    )
                )
        registration = self.session_registry.get(session_id)
        if registration is None:
            return ConsoleResult(
                output=(
                    f"error: session {session_id} is not live; state-profile save "
                    "can only save active sessions\n"
                )
            )
        try:
            with registration.lock:
                if self.session_registry.get(session_id) is not registration:
                    return ConsoleResult(
                        output="error: visit has ended; no profile saved\n"
                    )
                profile = create_state_profile(
                    registration.session,
                    name=namespace.name,
                    level=namespace.level,
                    comment=namespace.comment,
                )
            path = self.state_profiles.save(profile)
        except StateProfileError as exc:
            return ConsoleResult(output=f"error: {exc}\n")
        LOGGER.info(
            "State profile saved name=%s level=%s session=%s path=%s",
            profile.name,
            profile.level,
            session_id,
            path,
        )
        return ConsoleResult(
            output=(
                f"Saved state profile {profile.name!r} ({profile.level}) to {path}.\n"
            )
        )

    def _execute_state_profile_apply(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        requested_session_id = namespace.session_id or context.selected_session_id()
        session_id = None
        if requested_session_id is not None:
            session_id, error = self._resolve_session_id(
                context, requested_session_id, live=True
            )
            if error is not None:
                return ConsoleResult(output=f"error: {error}\n")
        if session_id is None:
            live_sessions = self.session_registry.list_sessions()
            if len(live_sessions) == 1:
                session_id = live_sessions[0].session_id
            else:
                return ConsoleResult(
                    output=(
                        "error: no active session; run sessions --set SESSION_ID "
                        "or pass --session SESSION_ID\n"
                    )
                )
        registration = self.session_registry.get(session_id)
        if registration is None:
            return ConsoleResult(
                output=(
                    f"error: session {session_id} is not live; state-profile apply "
                    "can only modify active sessions\n"
                )
            )
        try:
            profile = self.state_profiles.load(namespace.name)
        except StateProfileError as exc:
            return ConsoleResult(output=f"error: {exc}\n")
        with registration.lock:
            with self.session_registry.if_live(registration) as live:
                if not live:
                    return ConsoleResult(
                        output="error: visit has ended; no profile applied\n"
                    )
                apply_state_profile_to_session(registration.session, profile)
        LOGGER.warning(
            "State profile applied name=%s level=%s session=%s source_session=%s",
            profile.name,
            profile.level,
            session_id,
            profile.source_session_id,
        )
        trace_state_profile_apply(
            session_id,
            name=profile.name,
            level=profile.level,
            source_session_id=profile.source_session_id,
        )
        try:
            self.state_profiles.record_session_profile(
                session_id=session_id,
                profile=profile,
                source="apply",
            )
        except Exception as exc:
            LOGGER.warning(
                "State profile apply index update failed session=%s error=%s",
                session_id,
                type(exc).__name__,
            )
        return ConsoleResult(
            output=(
                f"Applied state profile {profile.name!r} "
                f"({profile.level}) to session {session_id}.\n"
            )
        )

    def _execute_state_profile_default(
        self,
        namespace: argparse.Namespace,
    ) -> ConsoleResult:
        if namespace.default_action is None:
            profile = self.state_profile_runtime.current_profile()
            if profile is None:
                return ConsoleResult(output="No default state profile set.\n")
            return ConsoleResult(
                output=(f"Default state profile: {profile.name} ({profile.level}).\n")
            )
        if namespace.default_action == "set":
            if namespace.name is None:
                return ConsoleResult(
                    output="error: state-profile default set requires NAME\n"
                )
            try:
                profile = self.state_profile_runtime.set_default(namespace.name)
                if namespace.persist:
                    self._persist_state_profile_default(profile.name)
            except (OSError, StateProfileError) as exc:
                return ConsoleResult(output=f"error: {exc}\n")
            LOGGER.warning(
                "State profile default set name=%s level=%s persist=%s",
                profile.name,
                profile.level,
                namespace.persist,
            )
            suffix = " and persisted" if namespace.persist else ""
            return ConsoleResult(
                output=(
                    f"Default state profile set to {profile.name!r} "
                    f"({profile.level}){suffix}.\n"
                )
            )
        if namespace.name is not None:
            return ConsoleResult(
                output="error: state-profile default clear does not accept NAME\n"
            )
        previous = self.state_profile_runtime.clear_default()
        try:
            if namespace.persist:
                self._persist_state_profile_default(None)
        except OSError as exc:
            return ConsoleResult(output=f"error: {exc}\n")
        LOGGER.warning(
            "State profile default cleared previous=%s persist=%s",
            previous.name if previous is not None else None,
            namespace.persist,
        )
        suffix = " and persisted" if namespace.persist else ""
        return ConsoleResult(output=f"Default state profile cleared{suffix}.\n")

    def _execute_state_profile_delete(
        self,
        namespace: argparse.Namespace,
    ) -> ConsoleResult:
        try:
            path, cleared_default = self.state_profile_runtime.delete_profile(
                namespace.name
            )
            if namespace.persist and cleared_default:
                self._persist_state_profile_default(None)
        except (OSError, StateProfileError) as exc:
            return ConsoleResult(output=f"error: {exc}\n")
        LOGGER.warning(
            "State profile deleted name=%s path=%s cleared_default=%s persist=%s",
            namespace.name,
            path,
            cleared_default,
            namespace.persist,
        )
        suffix = " Default state profile cleared." if cleared_default else ""
        if namespace.persist and cleared_default:
            suffix = " Default state profile cleared and persisted."
        return ConsoleResult(
            output=f"Deleted state profile {namespace.name!r}.{suffix}\n"
        )

    def _persist_state_profile_default(self, name: str | None) -> None:
        if self.config_path is None:
            raise OSError("no config path is available for --persist")
        persist_state_profile_setting(self.config_path, name)

    def _execute_failures(self, namespace: argparse.Namespace) -> ConsoleResult:
        if namespace.failures_command == "list":
            summaries = self.structured_failures.list()
            LOGGER.info("Structured failures listed count=%s", len(summaries))
            return ConsoleResult(output=_render_structured_failure_list(summaries))
        if namespace.failures_command == "show":
            try:
                capsule = self.structured_failures.load(namespace.id)
            except StructuredFailureError as exc:
                return ConsoleResult(output=f"error: {exc}\n")
            LOGGER.info(
                "Structured failure shown id=%s json=%s",
                capsule.id,
                namespace.json,
            )
            return ConsoleResult(
                output=_render_structured_failure(capsule, show_json=namespace.json)
            )
        if namespace.failures_command == "replay-parser":
            try:
                capsule = self.structured_failures.load(namespace.id)
            except StructuredFailureError as exc:
                return ConsoleResult(output=f"error: {exc}\n")
            result = replay_structured_failure_parser(capsule)
            LOGGER.info(
                "Structured failure parser replayed id=%s accepted=%s",
                capsule.id,
                result.metadata.get("structured_response_accepted") is not False,
            )
            return ConsoleResult(
                output=_render_structured_failure_replay(capsule, result)
            )
        return ConsoleResult(output=self._parser.format_help())

    def _execute_enrichment(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        control = self.enrichment_control
        if control is None:
            return ConsoleResult(
                output="error: EnrichmentAgent control is unavailable in this runtime\n"
            )
        try:
            if namespace.enrichment_command == "status":
                status = control.status()
                LOGGER.info(
                    "Enrichment status requested follow=%s interval_ms=%s",
                    namespace.follow,
                    (
                        round(ENRICHMENT_STATUS_REFRESH_SECONDS * 1000)
                        if namespace.follow
                        else None
                    ),
                )
                if not namespace.follow:
                    return ConsoleResult(output=_render_enrichment_status(status))
                return ConsoleResult(
                    output=_render_enrichment_status_follow_frame(status),
                    refresh=ConsoleRefresh(
                        name="enrichment_status",
                        render=lambda: _render_enrichment_status_follow_frame(
                            control.status()
                        ),
                    ),
                )
            if namespace.enrichment_command == "jobs":
                return ConsoleResult(
                    output=_render_enrichment_jobs(control.list_jobs(namespace.count))
                )
            if namespace.enrichment_command == "graph":
                graph = control.graph_mermaid().strip()
                if not graph:
                    return ConsoleResult(
                        output="error: EnrichmentAgent graph is unavailable\n"
                    )
                return ConsoleResult(
                    output=f"EnrichmentAgent graphs (Mermaid):\n{graph}\n"
                )
            if namespace.enrichment_command == "run":
                return self._execute_enrichment_run(namespace, context, control)
        except Exception as exc:
            LOGGER.exception(
                "Enrichment console operation failed action=%s error=%s",
                namespace.enrichment_command,
                type(exc).__name__,
            )
            return ConsoleResult(
                output=(
                    f"error: EnrichmentAgent control failed [{type(exc).__name__}]\n"
                )
            )
        return ConsoleResult(output=self._parser.format_help())

    def _execute_enrichment_run(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
        control: EnrichmentControl,
    ) -> ConsoleResult:
        requested_session_id = namespace.session_id or context.selected_session_id()
        session_id = None
        if requested_session_id is not None:
            session_id, error = self._resolve_session_id(
                context, requested_session_id, live=True
            )
            if error is not None:
                return ConsoleResult(output=f"error: {error}\n")
        if session_id is None:
            return ConsoleResult(
                output=(
                    "error: no active session; run sessions --set SESSION_ID "
                    "or pass --session SESSION_ID\n"
                )
            )
        if self.session_registry.get(session_id) is None:
            return ConsoleResult(
                output=(
                    f"error: session {session_id} is not live; enrichment can only "
                    "run against active sessions\n"
                )
            )
        result = control.submit_manual(
            session_id,
            target_host=namespace.target_host,
            surface=namespace.surface,
        )
        LOGGER.info(
            "Enrichment manual submission session=%s accepted=%s code=%s job_id=%s "
            "target_host=%s surface=%s",
            session_id,
            result.accepted,
            result.code,
            result.job_id,
            namespace.target_host,
            namespace.surface,
        )
        return ConsoleResult(
            output=_render_enrichment_submission(result, session_id=session_id)
        )

    def _execute_spiritguide(
        self,
        namespace: argparse.Namespace,
        context: ConsoleContext,
    ) -> ConsoleResult:
        if namespace.instruction and namespace.instruction[0] == "help":
            return ConsoleResult(output=_spiritguide_help_text())
        if namespace.instruction and namespace.instruction[0] == "profile":
            return self._execute_spiritguide_profile(namespace.instruction[1:])
        instruction = " ".join(namespace.instruction).strip()
        if not instruction:
            return ConsoleResult(output="error: spiritguide requires an instruction\n")
        requested_session_id = namespace.session_id or context.selected_session_id()
        session_id = None
        if requested_session_id is not None:
            session_id, error = self._resolve_session_id(
                context, requested_session_id, live=True
            )
            if error is not None:
                return ConsoleResult(output=f"error: {error}\n")
        if session_id is None:
            return ConsoleResult(
                output=(
                    "error: no active session; run sessions --set SESSION_ID "
                    "or pass --session SESSION_ID\n"
                )
            )
        registration = self.session_registry.get(session_id)
        if registration is None:
            return ConsoleResult(
                output=(
                    f"error: session {session_id} is not live; spiritguide can only "
                    "modify active sessions\n"
                )
            )

        LOGGER.info(
            "Spiritguide requested session=%s dry_run=%s instruction=%r",
            session_id,
            namespace.dry_run,
            instruction,
        )
        applied = False
        try:
            with registration.lock:
                if self.session_registry.get(session_id) is not registration:
                    return ConsoleResult(
                        output="error: visit has ended; no guidance started\n"
                    )
                result = self.spiritguide.respond(
                    SpiritguideContext(
                        instruction=instruction,
                        session_id=session_id,
                        session=registration.session,
                        dry_run=namespace.dry_run,
                    )
                )
                structured_response_accepted = result.metadata.get(
                    "structured_response_accepted"
                )
                state_update_rejected = structured_response_accepted is False
                with self.session_registry.if_live(registration) as live:
                    if not live:
                        return ConsoleResult(
                            output="error: visit has ended; guidance result discarded\n"
                        )
                    if not namespace.dry_run and not state_update_rejected:
                        apply_external_state_update(
                            registration.session,
                            result.state_update,
                        )
                        applied = bool(_spiritguide_patch_keys(result))
        except Exception as exc:
            LOGGER.exception(
                "Spiritguide failed session=%s error=%s",
                session_id,
                type(exc).__name__,
            )
            return ConsoleResult(
                output=(
                    "error: spiritguide provider error; instruction could not "
                    "be completed\n"
                )
            )
        LOGGER.info(
            "Spiritguide completed session=%s dry_run=%s",
            session_id,
            namespace.dry_run,
        )
        trace_spiritguide(
            session_id,
            instruction=instruction,
            dry_run=namespace.dry_run,
            structured_response_accepted=result.metadata.get(
                "structured_response_accepted"
            ),
            patch_keys=_spiritguide_patch_keys(result),
            applied=applied,
        )
        output = _ensure_trailing_newline(result.message)
        output += _render_spiritguide_result_summary(
            result,
            dry_run=namespace.dry_run,
            applied=applied,
        )
        if namespace.dry_run or namespace.show_patch:
            output += _render_spiritguide_patch(result)
        if result.metadata.get("structured_response_accepted") is False:
            output += (
                "Spiritguide response was not valid structured state; "
                "no state changes applied.\n"
            )
        if namespace.dry_run:
            output += "Dry run: no state changes applied.\n"
        return ConsoleResult(output=output)

    def _execute_spiritguide_profile(self, args: list[str]) -> ConsoleResult:
        if not args or args[0] in {"help", "--help", "-h"}:
            return ConsoleResult(output=_spiritguide_profile_help_text())
        try:
            namespace = _parse_spiritguide_profile_args(args)
            if namespace.profile_action == "create":
                result = create_spiritguide_profile(
                    store=self.state_profiles,
                    spiritguide=self.spiritguide,
                    name=namespace.name,
                    instruction=" ".join(namespace.instruction).strip(),
                    level=namespace.level,
                    dry_run=namespace.dry_run,
                    comment=namespace.comment,
                )
            elif namespace.profile_action == "update":
                result = update_spiritguide_profile(
                    store=self.state_profiles,
                    spiritguide=self.spiritguide,
                    name=namespace.name,
                    instruction=" ".join(namespace.instruction).strip(),
                    level=namespace.level,
                    dry_run=namespace.dry_run,
                    comment=namespace.comment,
                )
            elif namespace.profile_action == "fork":
                result = fork_spiritguide_profile(
                    store=self.state_profiles,
                    spiritguide=self.spiritguide,
                    source_name=namespace.source_name,
                    name=namespace.name,
                    instruction=" ".join(namespace.instruction).strip(),
                    level=namespace.level,
                    dry_run=namespace.dry_run,
                    comment=namespace.comment,
                )
            else:
                return ConsoleResult(
                    output=_build_spiritguide_profile_parser().format_help()
                )
        except ConsoleHelp as exc:
            return ConsoleResult(output=str(exc))
        except (ValueError, StateProfileError) as exc:
            return ConsoleResult(output=f"error: {exc}\n")
        except Exception as exc:
            LOGGER.exception(
                "Detached Spiritguide profile operation failed error=%s",
                type(exc).__name__,
            )
            return ConsoleResult(
                output=(
                    "error: spiritguide provider error; profile operation could "
                    "not be completed\n"
                )
            )
        LOGGER.info(
            "Detached Spiritguide profile console completed action=%s name=%s "
            "dry_run=%s saved=%s",
            result.action,
            result.name,
            result.dry_run,
            result.saved,
        )
        output = _ensure_trailing_newline(result.message)
        output += _render_spiritguide_profile_result_summary(result)
        if result.dry_run or namespace.show_patch:
            output += _render_spiritguide_profile_patch(result)
        if result.structured_response_accepted is False:
            output += (
                "Spiritguide response was not valid structured state; "
                "no profile saved.\n"
            )
        if result.dry_run:
            output += "Dry run: no profile saved.\n"
        return ConsoleResult(output=output)

    def complete(self, line: str, *, allow_exit: bool) -> str | None:
        command_line = line
        prefix_offset = ""
        if line.startswith(f"{CONSOLE_PREFIX} "):
            prefix_offset = f"{CONSOLE_PREFIX} "
            command_line = line[len(prefix_offset) :]
        commands = [
            "commands",
            "enrichment",
            "failures",
            "help",
            "inspection",
            "inspect",
            "langfuse",
            "logging",
            "session",
            "sessions",
            "state-profile",
            "state-delta",
            "state-diff",
            "structured-failures",
            "delta",
            "shutdown",
            "spiritguide",
            "trace",
        ]
        candidates: list[str]
        prefix: str
        if " " not in command_line:
            prefix = command_line
            candidates = [command for command in commands if command.startswith(prefix)]
        elif command_line.startswith("logging "):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = ["--help", "--number", "-f", "-n"]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith("trace "):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = ["--help", "--follow", "--number", "-f", "-n"]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith(("sessions ", "session ")):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = [
                "--clear",
                "--close",
                "--close-matching",
                "--dry-run",
                "--flush",
                "--help",
                "--label",
                "--live",
                "--number",
                "--origin",
                "--set",
                "--tag",
                "-f",
                "-n",
            ]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith("commands "):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = [
                "--help",
                "--columns",
                "--filter",
                "--flush",
                "--number",
                "--session",
                "--sort",
                "--quick",
                "--asc",
                "-f",
                "-n",
            ]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith(("inspection ", "inspect ")):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = [
                "--help",
                "--all",
                "--cost",
                "--flush",
                "--metadata",
                "--number",
                "--observations",
                "--output",
                "--prompt",
                "--reasoning",
                "--timing",
                "--tokens",
                "-f",
                "-n",
            ]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith(("state-delta ", "state-diff ", "delta ")):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = [
                "--help",
                "--color",
                "--flush",
                "--number",
                "--section",
                "-f",
                "-n",
            ]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith("state-profile "):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = [
                "apply",
                "default",
                "delete",
                "list",
                "save",
                "show",
                "--help",
                "--comment",
                "--json",
                "--level",
                "--persist",
                "--session",
            ]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith(("failures ", "structured-failures ")):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = ["list", "replay-parser", "show", "--help", "--json"]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith("enrichment status "):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = ["--follow", "--help", "-f"]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith("enrichment run "):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = ["--help", "--host", "--session", "--surface"]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith("enrichment jobs "):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = ["--help", "--number", "-n"]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith("enrichment graph "):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = ["--help"]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith("enrichment "):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = [
                "graph",
                "jobs",
                "run",
                "status",
                "--help",
            ]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith("spiritguide "):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = [
                "profile",
                "create",
                "update",
                "fork",
                "--comment",
                "--dry-run",
                "--help",
                "--level",
                "--session",
                "--show-patch",
            ]
            candidates = [option for option in options if option.startswith(prefix)]
        elif command_line.startswith("langfuse "):
            prefix = command_line.rsplit(" ", maxsplit=1)[-1]
            options = ["flush", "--help"]
            candidates = [option for option in options if option.startswith(prefix)]
        else:
            return None
        if len(candidates) != 1:
            return None
        completed = f"{command_line[: len(command_line) - len(prefix)]}{candidates[0]} "
        return f"{prefix_offset}{completed}"


def _render_enrichment_status(status: EnrichmentRuntimeStatus) -> str:
    runtime = "started" if status.started else "stopped"
    automatic = "enabled" if status.automatic_enabled else "disabled"
    configured = "yes" if status.configured else "no"
    return (
        f"EnrichmentAgent: {runtime}; configured={configured}; "
        f"automatic={automatic}; apply_mode={status.apply_mode}\n"
        f"Jobs: inflight={status.inflight} queued={status.queued} "
        f"completed={status.completed} failed={status.failed}\n"
    )


def _render_enrichment_status_follow_frame(
    status: EnrichmentRuntimeStatus,
) -> str:
    return (
        "Enrichment status (refreshing every 1s; press any key to stop)\n"
        f"{_render_enrichment_status(status)}"
    )


def _render_enrichment_submission(
    submission: EnrichmentSubmission,
    *,
    session_id: str,
) -> str:
    if not submission.accepted:
        return (
            f"error: enrichment request rejected [{submission.code}]: "
            f"{submission.reason}\n"
        )
    job = submission.job_id or "pending"
    suffix = f" ({submission.reason})" if submission.reason else ""
    return f"Enrichment job {job} queued for session {session_id}{suffix}.\n"


def _render_enrichment_jobs(jobs: tuple[EnrichmentJobSnapshot, ...]) -> str:
    if not jobs:
        return "No enrichment jobs recorded.\n"
    lines = [f"Recent enrichment jobs ({len(jobs)}):"]
    for job in jobs:
        details = [
            job.status,
            job.job_id,
            f"session={job.session_id}",
            f"trigger={job.trigger}",
            f"mode={job.mode}",
        ]
        if job.target_host:
            details.append(f"target={job.target_host}")
        if job.surface:
            details.append(f"surface={job.surface}")
        timing = _enrichment_job_timing(job)
        if timing:
            details.append(f"timing={timing}")
        if job.reason:
            details.append(f"reason={job.reason}")
        lines.append("  " + " ".join(details))
    return "\n".join(lines) + "\n"


def _enrichment_job_timing(job: EnrichmentJobSnapshot) -> str | None:
    if job.duration_ms is not None:
        return f"{job.duration_ms}ms"
    if job.started_at:
        return f"started:{job.started_at}"
    if job.queued_at:
        return f"queued:{job.queued_at}"
    return None


def _build_parser() -> ConsoleCommandParser:
    parser = ConsoleCommandParser(
        prog="dreamland",
        description="Introspect and control the Dreamland backend.",
    )
    subparsers = parser.add_subparsers(dest="command")
    lobby_parser = subparsers.add_parser(
        "lobby", help="Manage the native SSH lobby catalog."
    )
    lobby_commands = lobby_parser.add_subparsers(dest="lobby_command", required=True)
    worlds_parser = lobby_commands.add_parser("worlds")
    world_commands = worlds_parser.add_subparsers(dest="lobby_action", required=True)
    world_commands.add_parser("list")
    world_commands.add_parser("reload")
    for action in ("add", "update"):
        world_parser = world_commands.add_parser(action)
        world_parser.add_argument("profile")
        world_parser.add_argument("--title", required=action == "add")
        world_parser.add_argument("--description", required=action == "add")
        world_parser.add_argument(
            "--entry-username",
            help=(
                "entry account (add defaults to the saved profile's starting account; "
                "update preserves the current account when omitted)"
            ),
        )
    world_commands.add_parser("remove").add_argument("profile")
    world_commands.add_parser("order").add_argument("profiles", nargs="+")
    logging_parser = subparsers.add_parser(
        "logging",
        help="Show recent Dreamland logs.",
        description="Show recent Dreamland logs.",
    )
    logging_parser.add_argument(
        "-n",
        "--number",
        dest="count",
        type=_nonnegative_count,
        default=20,
        help="number of retained records to show (default: 20)",
    )
    logging_parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="follow new records until any key is pressed",
    )
    trace_parser = subparsers.add_parser(
        "trace",
        help="Show traced IO for one Dreamland session.",
        description=(
            "Show traced submitted input, rendered output, and reasoning summaries "
            "for a single session. Uses the active session by default; pass "
            "SESSION_ID to inspect another session."
        ),
    )
    _add_count_option(
        trace_parser,
        default=20,
        help_text="number of retained trace records to show (default: 20)",
    )
    trace_parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="follow new trace records until any key is pressed",
    )
    trace_parser.add_argument(
        "session_id",
        nargs="?",
        metavar="SESSION_ID",
        help="session id to trace instead of the active session",
    )
    sessions_parser = subparsers.add_parser(
        "sessions",
        aliases=("session",),
        help="List Langfuse sessions or set the active session.",
        description=(
            "List recent Langfuse sessions. Use --set SESSION_ID to make commands "
            "and inspection default to that session. SSH one-shot commands default "
            "to their own SSH session."
        ),
    )
    _add_flush_option(sessions_parser)
    _add_count_option(
        sessions_parser,
        default=DEFAULT_SESSION_LIMIT,
        help_text=f"number of sessions to show (default: {DEFAULT_SESSION_LIMIT})",
    )
    sessions_parser.add_argument(
        "--set",
        dest="set_session",
        metavar="SESSION_ID",
        help="set the active session for subsequent console commands",
    )
    sessions_parser.add_argument(
        "--clear",
        action="store_true",
        help="clear the explicitly active session and return to the caller default",
    )
    sessions_parser.add_argument(
        "--live",
        action="store_true",
        help="list live in-memory sessions with tags, labels, and origin",
    )
    sessions_parser.add_argument(
        "--tag",
        dest="tags",
        action="append",
        metavar="TAG",
        help="filter live sessions by tag; may be repeated",
    )
    sessions_parser.add_argument(
        "--label",
        metavar="LABEL",
        help="filter live sessions by label",
    )
    sessions_parser.add_argument(
        "--origin",
        metavar="ORIGIN",
        help="filter live sessions by origin, for example mcp or ssh",
    )
    sessions_parser.add_argument(
        "--close",
        dest="close_session",
        metavar="SESSION_ID",
        help="close one live session by id",
    )
    sessions_parser.add_argument(
        "--close-matching",
        action="store_true",
        help="close live sessions matching --tag, --label, and --origin filters",
    )
    sessions_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show which live sessions would be closed without closing them",
    )
    commands_parser = subparsers.add_parser(
        "commands",
        help="List commands for one Langfuse session.",
        description=(
            "List command traces for a single session. Uses the active session by "
            "default; pass --session SESSION_ID to inspect another session."
        ),
    )
    _add_flush_option(commands_parser)
    _add_count_option(
        commands_parser,
        default=DEFAULT_COMMAND_LIMIT,
        help_text=f"number of commands to show (default: {DEFAULT_COMMAND_LIMIT})",
    )
    commands_parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        help="session id to list instead of the active session",
    )
    commands_parser.add_argument(
        "--filter",
        dest="filter_text",
        metavar="TEXT",
        help="only show commands whose id or command text contains TEXT",
    )
    commands_parser.add_argument(
        "--sort",
        choices=("time", "command", "tokens", "cost", "latency"),
        default="time",
        help="sort column (default: time)",
    )
    commands_parser.add_argument(
        "--asc",
        action="store_true",
        help="sort ascending instead of descending",
    )
    commands_parser.add_argument(
        "--columns",
        default="pos,id,time,command,model,reasoning,summary,tokens,cost,latency",
        help=(
            "comma-separated columns: pos,id,time,command,status,model,tokens,"
            "cost,latency,session,reasoning,summary"
        ),
    )
    commands_parser.add_argument(
        "--quick",
        action="store_true",
        help=(
            "return faster by skipping raw Langfuse IO payloads used only to "
            "detect reasoning summaries in older traces; summary may show "
            "unknown when reasoning tokens exist"
        ),
    )
    inspection_parser = subparsers.add_parser(
        "inspection",
        aliases=("inspect",),
        help="Inspect one command trace and its raw LLM IO.",
        description=(
            "Inspect one command by command id or relative position. Without a "
            "target, the most recent command in the active session is inspected. "
            "Relative positions use @-N: @-1 is newest, @-2 is previous."
        ),
    )
    _add_flush_option(inspection_parser)
    _add_count_option(
        inspection_parser,
        default=DEFAULT_COMMAND_LIMIT,
        help_text=(
            "number of recent commands to search when resolving a relative "
            f"position or command id (default: {DEFAULT_COMMAND_LIMIT})"
        ),
    )
    inspection_parser.add_argument(
        "target",
        nargs="?",
        help="command id or relative @-N position; default: latest command",
    )
    inspection_parser.add_argument(
        "--prompt",
        action="store_true",
        help="show the exact raw prompt/input sent to the LLM",
    )
    inspection_parser.add_argument(
        "--output",
        action="store_true",
        help="show the exact raw output received from the LLM",
    )
    inspection_parser.add_argument(
        "--reasoning",
        action="store_true",
        help="show the captured reasoning summary, if Langfuse stored one",
    )
    inspection_parser.add_argument(
        "--metadata",
        action="store_true",
        help="show trace metadata",
    )
    inspection_parser.add_argument(
        "--cost",
        action="store_true",
        help="show cost details for the inspected command",
    )
    inspection_parser.add_argument(
        "--tokens",
        action="store_true",
        help="show token counts for the inspected command",
    )
    inspection_parser.add_argument(
        "--timing",
        action="store_true",
        help="show timing details for the inspected command",
    )
    inspection_parser.add_argument(
        "--observations",
        action="store_true",
        help="show observation-level raw inputs and outputs",
    )
    inspection_parser.add_argument(
        "--all",
        action="store_true",
        help="show prompt, output, metadata, and observations",
    )
    state_delta_parser = subparsers.add_parser(
        "state-delta",
        aliases=("state-diff", "delta"),
        help="Show a diff of Dreamland session state between commands.",
        description=(
            "Show a unified diff of backend state between two commands. Targets "
            "may be command ids or relative positions such as @-1. If one target "
            "is provided, it compares the state before and after that command."
        ),
    )
    _add_flush_option(state_delta_parser)
    _add_count_option(
        state_delta_parser,
        default=DEFAULT_COMMAND_LIMIT,
        help_text=(
            "number of recent commands to search when resolving relative "
            f"positions or command ids (default: {DEFAULT_COMMAND_LIMIT})"
        ),
    )
    state_delta_parser.add_argument(
        "targets",
        nargs="+",
        metavar="TARGET",
        help=(
            "one command target for before/after, or two targets FROM TO for "
            "after-state comparison"
        ),
    )
    state_delta_parser.add_argument(
        "--section",
        dest="sections",
        action="append",
        metavar="NAME",
        help="limit the diff to a top-level state section; may be repeated",
    )
    state_delta_parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize diff output (default: auto)",
    )
    spiritguide_parser = subparsers.add_parser(
        "spiritguide",
        help="Guide live sessions or detached state profiles with Spiritguide.",
        description=(
            "Send an operator instruction to the selected live-session "
            "spiritguide, or use `spiritguide profile` to build detached "
            "state profiles. Live guidance may return state changes for the "
            "active Dreamland session. SSH one-shot commands default to their "
            "own SSH session."
        ),
    )
    spiritguide_parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        help="live session id to guide instead of the active session",
    )
    spiritguide_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ask the spiritguide for changes without applying them",
    )
    spiritguide_parser.add_argument(
        "--show-patch",
        action="store_true",
        help="print the parsed state_update JSON; dry runs show it automatically",
    )
    spiritguide_parser.add_argument(
        "instruction",
        nargs=argparse.REMAINDER,
        help=(
            "operator instruction for the spiritguide, or `profile create`, "
            "`profile update`, or `profile fork`"
        ),
    )
    state_profile_parser = subparsers.add_parser(
        "state-profile",
        help="Save and inspect Dreamland session state profiles.",
        description=(
            "Save live session-derived state profiles that can later seed or "
            "restore Dreamland sessions."
        ),
    )
    state_profile_subparsers = state_profile_parser.add_subparsers(
        dest="state_profile_command"
    )
    state_profile_save_parser = state_profile_subparsers.add_parser(
        "save",
        help="Save a live session as a state profile.",
    )
    state_profile_save_parser.add_argument(
        "name",
        help="state profile name; letters, numbers, dots, dashes, and underscores",
    )
    state_profile_save_parser.add_argument(
        "--level",
        choices=STATE_PROFILE_LEVELS,
        default="exact",
        help="profile depth: exact, system, or world (default: exact)",
    )
    state_profile_save_parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        help="live session id to save instead of the active session",
    )
    state_profile_save_parser.add_argument(
        "--comment",
        metavar="TEXT",
        help="operator comment stored with the profile",
    )
    state_profile_subparsers.add_parser(
        "list",
        help="List saved state profiles.",
    )
    state_profile_default_parser = state_profile_subparsers.add_parser(
        "default",
        help="Show or change the runtime default state profile for new sessions.",
    )
    state_profile_default_parser.add_argument(
        "default_action",
        nargs="?",
        choices=("set", "clear"),
        help="set or clear the runtime default; omit to show it",
    )
    state_profile_default_parser.add_argument(
        "name",
        nargs="?",
        help="state profile name when using default set",
    )
    state_profile_default_parser.add_argument(
        "--persist",
        action="store_true",
        help="also write the default to the loaded Dreamland config file",
    )
    state_profile_delete_parser = state_profile_subparsers.add_parser(
        "delete",
        help="Delete a saved state profile.",
    )
    state_profile_delete_parser.add_argument(
        "name",
        help="state profile name to delete",
    )
    state_profile_delete_parser.add_argument(
        "--persist",
        action="store_true",
        help="also clear the persisted default if the deleted profile is active",
    )
    state_profile_apply_parser = state_profile_subparsers.add_parser(
        "apply",
        help="Apply a saved state profile to a live session.",
    )
    state_profile_apply_parser.add_argument(
        "name",
        help="state profile name to apply",
    )
    state_profile_apply_parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        help="live session id to replace instead of the active session",
    )
    state_profile_show_parser = state_profile_subparsers.add_parser(
        "show",
        help="Show metadata for a saved state profile.",
    )
    state_profile_show_parser.add_argument(
        "name",
        help="state profile name to show",
    )
    state_profile_show_parser.add_argument(
        "--json",
        action="store_true",
        help="show the full state profile JSON document",
    )
    failures_parser = subparsers.add_parser(
        "failures",
        aliases=("structured-failures",),
        help="Inspect structured command-agent failure capsules.",
        description=(
            "List and inspect structured failure capsules captured when a "
            "command-agent response envelope fails parsing or validation."
        ),
    )
    failures_subparsers = failures_parser.add_subparsers(dest="failures_command")
    failures_subparsers.add_parser(
        "list",
        help="List captured structured failure capsules.",
    )
    failures_show_parser = failures_subparsers.add_parser(
        "show",
        help="Show one structured failure capsule.",
    )
    failures_show_parser.add_argument(
        "id",
        help="structured failure capsule id",
    )
    failures_show_parser.add_argument(
        "--json",
        action="store_true",
        help="show the full structured failure capsule JSON document",
    )
    failures_replay_parser = failures_subparsers.add_parser(
        "replay-parser",
        help="Replay one capsule's raw response through the current parser.",
    )
    failures_replay_parser.add_argument(
        "id",
        help="structured failure capsule id",
    )
    enrichment_parser = subparsers.add_parser(
        "enrichment",
        help="Inspect and control the session-local EnrichmentAgent.",
        description=(
            "Inspect the EnrichmentAgent runtime, submit manual work for a live "
            "session, review recent jobs, or print its LangGraph topology."
        ),
    )
    enrichment_subparsers = enrichment_parser.add_subparsers(dest="enrichment_command")
    enrichment_status_parser = enrichment_subparsers.add_parser(
        "status",
        help="Show runtime state and job counts.",
    )
    enrichment_status_parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="redraw current status every second until any key is pressed",
    )
    enrichment_run_parser = enrichment_subparsers.add_parser(
        "run",
        help="Queue enrichment for a live session.",
    )
    enrichment_run_parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        help="live session id instead of the active session",
    )
    enrichment_run_parser.add_argument(
        "--host",
        dest="target_host",
        metavar="HOST",
        help="specific host to enrich; defaults to the session's active host",
    )
    enrichment_run_parser.add_argument(
        "--surface",
        metavar="SURFACE",
        help="specific surface or artifact cluster to enrich",
    )
    enrichment_jobs_parser = enrichment_subparsers.add_parser(
        "jobs",
        help="Show recent enrichment jobs.",
    )
    _add_count_option(
        enrichment_jobs_parser,
        default=20,
        help_text="number of recent enrichment jobs to show (default: 20)",
    )
    enrichment_subparsers.add_parser(
        "graph",
        help="Print the EnrichmentAgent LangGraph as Mermaid.",
    )
    langfuse_parser = subparsers.add_parser(
        "langfuse",
        help="Interact with Langfuse integration.",
        description="Langfuse integration commands.",
    )
    langfuse_subparsers = langfuse_parser.add_subparsers(dest="langfuse_command")
    langfuse_subparsers.add_parser(
        "flush",
        help="Flush pending Langfuse telemetry immediately.",
    )
    subparsers.add_parser("shutdown", help="Stop the Dreamland process.")
    subparsers.add_parser("help", help="Show this help.")
    return parser


def _build_spiritguide_profile_parser() -> ConsoleCommandParser:
    parser = ConsoleCommandParser(
        prog="spiritguide profile",
        description=(
            "Build detached DREAMLAND state profiles with Spiritguide. These "
            "commands do not require a live session."
        ),
    )
    subparsers = parser.add_subparsers(dest="profile_action", required=True)
    create_parser = subparsers.add_parser(
        "create",
        help="Create a detached state profile from the default boot state.",
    )
    _add_spiritguide_profile_options(create_parser)
    create_parser.add_argument("name", help="state profile name to create")
    create_parser.add_argument(
        "instruction",
        nargs="*",
        help="operator instruction for the detached profile",
    )

    update_parser = subparsers.add_parser(
        "update",
        help="Update an existing detached profile with Spiritguide.",
    )
    _add_spiritguide_profile_options(update_parser, level_required=False)
    update_parser.add_argument("name", help="state profile name to update")
    update_parser.add_argument(
        "instruction",
        nargs="*",
        help="operator instruction for the detached profile",
    )

    fork_parser = subparsers.add_parser(
        "fork",
        help="Fork an existing detached profile, optionally with Spiritguide.",
    )
    _add_spiritguide_profile_options(fork_parser, level_required=False)
    fork_parser.add_argument("source_name", help="state profile name to fork")
    fork_parser.add_argument("name", help="new state profile name")
    fork_parser.add_argument(
        "instruction",
        nargs="*",
        help="optional operator instruction for the forked profile",
    )
    return parser


def _add_spiritguide_profile_options(
    parser: argparse.ArgumentParser,
    *,
    level_required: bool = True,
) -> None:
    parser.add_argument(
        "--level",
        choices=("world", "system"),
        default="world" if level_required else None,
        help=(
            "state profile level to save; defaults to world for create and "
            "the source profile level for update/fork"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ask Spiritguide for changes without saving a profile",
    )
    parser.add_argument(
        "--show-patch",
        action="store_true",
        help="print the parsed state_update JSON; dry runs show it automatically",
    )
    parser.add_argument(
        "--comment",
        metavar="TEXT",
        help="operator comment stored with the profile",
    )


def _parse_spiritguide_profile_args(args: list[str]) -> argparse.Namespace:
    namespace, instruction_tail = _build_spiritguide_profile_parser().parse_known_args(
        args
    )
    if instruction_tail:
        namespace.instruction.extend(instruction_tail)
    return namespace


def _spiritguide_help_text() -> str:
    return (
        _spiritguide_parser().format_help() + "\nDetached profile workflow:\n"
        "  spiritguide profile create NAME [OPTIONS] INSTRUCTION\n"
        "  spiritguide profile update NAME [OPTIONS] INSTRUCTION\n"
        "  spiritguide profile fork SOURCE_NAME NAME [OPTIONS] [INSTRUCTION]\n"
        "\n"
        "Examples:\n"
        "  spiritguide profile create factory-lab build a small factory OT network\n"
        "  spiritguide profile update factory-lab add a legacy engineering workstation\n"
        "  spiritguide profile fork factory-lab factory-lab-v2 make it a hospital biomedical network\n"
        "\n"
        "Run `spiritguide profile help` for detached profile options.\n"
    )


def _spiritguide_parser() -> argparse.ArgumentParser:
    subparsers = next(
        action
        for action in _build_parser()._actions
        if getattr(action, "dest", None) == "command"
    )
    return subparsers.choices["spiritguide"]


def _spiritguide_profile_help_text() -> str:
    return (
        _build_spiritguide_profile_parser().format_help() + "\nUsage:\n"
        "  spiritguide profile create NAME [--level world|system] [--dry-run] [--show-patch] [--comment TEXT] INSTRUCTION\n"
        "  spiritguide profile update NAME [--level world|system] [--dry-run] [--show-patch] [--comment TEXT] INSTRUCTION\n"
        "  spiritguide profile fork SOURCE_NAME NAME [--level world|system] [--dry-run] [--show-patch] [--comment TEXT] [INSTRUCTION]\n"
        "\n"
        "The default level is world. World-level state profiles preserve durable world facts,\n"
        "services, hosts, and registered generated artifacts while excluding cwd,\n"
        "shell stack, command history, and other live session position.\n"
        "\n"
        "Examples:\n"
        "  spiritguide profile create factory-lab build a small factory OT network\n"
        "  spiritguide profile update factory-lab add an air-gapped PLC segment\n"
        "  spiritguide profile fork factory-lab factory-lab-review\n"
    )


def _add_count_option(
    parser: argparse.ArgumentParser,
    *,
    default: int,
    help_text: str,
) -> None:
    parser.add_argument(
        "-n",
        "--number",
        dest="count",
        type=_nonnegative_count,
        default=default,
        help=help_text,
    )


def _add_flush_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-f",
        "--flush",
        action="store_true",
        help="flush pending Langfuse telemetry before querying",
    )


def _normalize_state_delta_targets(namespace: argparse.Namespace) -> None:
    targets = namespace.targets
    if len(targets) == 1:
        namespace.from_target = None
        namespace.to_target = targets[0]
        return
    if len(targets) == 2:
        namespace.from_target = targets[0]
        namespace.to_target = targets[1]
        return
    raise ValueError("state-delta expects one target or two targets\n")


def _columns(value: str) -> list[str]:
    allowed = {
        "pos",
        "id",
        "time",
        "command",
        "status",
        "model",
        "tokens",
        "cost",
        "latency",
        "session",
        "reasoning",
        "summary",
    }
    columns = [column.strip() for column in value.split(",") if column.strip()]
    invalid = [column for column in columns if column not in allowed]
    if invalid:
        return [
            "pos",
            "id",
            "time",
            "command",
            "model",
            "reasoning",
            "summary",
            "tokens",
            "cost",
            "latency",
        ]
    return columns or [
        "pos",
        "id",
        "time",
        "command",
        "model",
        "reasoning",
        "summary",
        "tokens",
        "cost",
        "latency",
    ]


def _nonnegative_count(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _ensure_trailing_newline(value: str) -> str:
    if not value:
        return ""
    return value if value.endswith("\n") else f"{value}\n"


def _has_live_session_filters(namespace: argparse.Namespace) -> bool:
    return bool(
        normalize_session_tags(namespace.tags)
        or (namespace.label.strip() if isinstance(namespace.label, str) else "")
        or (namespace.origin.strip() if isinstance(namespace.origin, str) else "")
    )


def _render_live_sessions(
    sessions: list[dict[str, object]],
    *,
    active_session_id: str | None,
) -> str:
    if not sessions:
        return "No live sessions found.\n"
    headers = [
        "active",
        "session_id",
        "public_id",
        "user",
        "host",
        "cwd",
        "history",
        "origin",
        "label",
        "tags",
        "state_profile",
    ]
    rows = [
        [
            "*" if session["session_id"] == active_session_id else "",
            str(session["session_id"]),
            str(session.get("public_id") or ""),
            str(session["username"]),
            str(session["hostname"]),
            str(session["cwd"]),
            str(session["history_count"]),
            str(session["origin"] or ""),
            str(session["label"] or ""),
            ",".join(str(tag) for tag in session["tags"]),
            str(session["state_profile"] or ""),
        ]
        for session in sessions
    ]
    return _plain_table(headers, rows)


def _render_close_matching_preview(
    sessions: list[dict[str, object]],
    *,
    dry_run: bool,
) -> str:
    verb = "Would close" if dry_run else "Closed"
    if not sessions:
        return f"{verb} 0 live sessions.\n"
    lines = [f"{verb} {len(sessions)} live session(s):"]
    lines.extend(f"  {session['session_id']}" for session in sessions)
    return "\n".join(lines) + "\n"


def _plain_table(headers: list[str], rows: list[list[str]]) -> str:
    safe_rows = [[_safe_console_cell(value) for value in row] for row in rows]
    widths = [
        max([len(header), *(len(row[index]) for row in safe_rows)])
        for index, header in enumerate(headers)
    ]
    header_line = "  ".join(
        header.ljust(widths[index]) for index, header in enumerate(headers)
    )
    divider = "  ".join("-" * width for width in widths)
    body = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in safe_rows
    ]
    return "\n".join([header_line, divider, *body]).rstrip() + "\n"


def _safe_console_cell(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) > 80:
        return f"{normalized[:77]}..."
    return normalized


def _render_state_profile_list(summaries: list[StateProfileSummary]) -> str:
    if not summaries:
        return "No state profiles saved.\n"
    lines = [f"{len(summaries)} state profile(s):"]
    for summary in summaries:
        comment = f" - {summary.comment}" if summary.comment else ""
        source = (
            f" source={summary.source_session_id}"
            if summary.source_session_id is not None
            else ""
        )
        lines.append(
            f"  {summary.name}  level={summary.level}  "
            f"created={summary.created_at}{source}{comment}"
        )
    return "\n".join(lines) + "\n"


def _render_state_profile(profile: StateProfile, *, show_json: bool) -> str:
    if show_json:
        return json.dumps(profile.to_document(), indent=2, sort_keys=True) + "\n"
    state_keys = ", ".join(sorted(profile.state)) if profile.state else "none"
    lines = [
        f"name: {profile.name}",
        f"level: {profile.level}",
        f"created_at: {profile.created_at}",
        f"source_session_id: {profile.source_session_id or 'none'}",
        f"comment: {profile.comment or 'none'}",
        f"state_sections: {state_keys}",
    ]
    return "\n".join(lines) + "\n"


def _render_structured_failure_list(
    summaries: list[StructuredFailureSummary],
) -> str:
    if not summaries:
        return "No structured failure capsules saved.\n"
    headers = [
        "id",
        "created",
        "class",
        "session",
        "command_id",
        "command",
    ]
    rows = [
        [
            summary.id,
            summary.created_at,
            summary.failure_class,
            summary.session_id,
            summary.command_id,
            summary.command,
        ]
        for summary in summaries
    ]
    return _plain_table(headers, rows)


def _render_structured_failure(
    capsule: StructuredFailureCapsule,
    *,
    show_json: bool,
) -> str:
    if show_json:
        return json.dumps(capsule.to_document(), indent=2, sort_keys=True) + "\n"
    profile = capsule.pre_command_state_profile
    state = profile.get("state") if isinstance(profile, dict) else {}
    state_keys = (
        ", ".join(sorted(state)) if isinstance(state, dict) and state else "none"
    )
    lines = [
        f"id: {capsule.id}",
        f"created_at: {capsule.created_at}",
        f"session_id: {capsule.session_id}",
        f"command_id: {capsule.command_id}",
        f"command: {capsule.command}",
        f"starting_cwd: {capsule.starting_cwd}",
        f"user: {capsule.username}",
        f"host: {capsule.hostname}",
        f"profile: {capsule.profile_name}",
        f"failure_class: {capsule.failure_class}",
        f"parser_error: {capsule.parser_error}",
        f"result_exit_status: {capsule.result_exit_status}",
        f"result_stdout_chars: {len(capsule.result_stdout)}",
        f"result_stderr_chars: {len(capsule.result_stderr)}",
        f"raw_response_chars: {len(capsule.raw_response)}",
        f"pre_command_state_sections: {state_keys}",
    ]
    return "\n".join(lines) + "\n"


def _render_structured_failure_replay(
    capsule: StructuredFailureCapsule,
    result: CommandResult,
) -> str:
    accepted = result.metadata.get("structured_response_accepted") is not False
    failure_class = result.metadata.get("dreamland.structured.failure_class")
    parser_error = result.metadata.get("structured_parse_error")
    lines = [
        f"id: {capsule.id}",
        f"accepted: {'yes' if accepted else 'no'}",
        f"failure_class: {failure_class or 'none'}",
        f"parser_error: {parser_error or 'none'}",
        f"exit_status: {result.exit_status}",
        f"stdout_chars: {len(result.stdout)}",
        f"stderr_chars: {len(result.stderr)}",
    ]
    return "\n".join(lines) + "\n"


def _render_spiritguide_result_summary(
    result: SpiritguideResult,
    *,
    dry_run: bool,
    applied: bool,
) -> str:
    structured = result.metadata.get("structured_response_accepted")
    structured_label = (
        "accepted"
        if structured is True
        else "rejected"
        if structured is False
        else "not reported"
    )
    patch_keys = _spiritguide_patch_keys(result)
    if applied:
        applied_label = "yes"
    elif dry_run:
        applied_label = "no (dry run)"
    elif structured is False:
        applied_label = "no (structured response rejected)"
    elif not patch_keys:
        applied_label = "no (no state update)"
    else:
        applied_label = "no"
    return (
        "Spiritguide result:\n"
        f"  state_update: {', '.join(patch_keys) if patch_keys else 'none'}\n"
        f"  applied: {applied_label}\n"
        f"  dry_run: {'yes' if dry_run else 'no'}\n"
        f"  structured_response: {structured_label}\n"
    )


def _render_spiritguide_patch(result: SpiritguideResult) -> str:
    state_update = _spiritguide_state_update_to_dict(result)
    return f"Spiritguide patch:\n{json.dumps(state_update, indent=2, sort_keys=True)}\n"


def _render_spiritguide_profile_result_summary(
    result: SpiritguideProfileOperationResult,
) -> str:
    structured = result.structured_response_accepted
    structured_label = (
        "accepted"
        if structured is True
        else "rejected"
        if structured is False
        else "not reported"
    )
    if result.saved:
        saved_label = "yes"
    elif result.dry_run:
        saved_label = "no (dry run)"
    elif structured is False:
        saved_label = "no (structured response rejected)"
    else:
        saved_label = "no"
    lines = [
        "Spiritguide profile result:",
        f"  action: {result.action}",
        f"  profile: {result.name}",
        f"  level: {result.level}",
        f"  state_update: {', '.join(result.patch_keys) if result.patch_keys else 'none'}",
        f"  applied_to_detached_state: {'yes' if result.applied else 'no'}",
        f"  saved: {saved_label}",
        f"  dry_run: {'yes' if result.dry_run else 'no'}",
        f"  structured_response: {structured_label}",
    ]
    if result.source_name is not None:
        lines.insert(3, f"  source_profile: {result.source_name}")
    if result.path is not None:
        lines.append(f"  path: {result.path}")
    if result.excluded_patch_keys:
        lines.append(
            "  excluded_from_profile: " + ", ".join(result.excluded_patch_keys)
        )
    return "\n".join(lines) + "\n"


def _render_spiritguide_profile_patch(
    result: SpiritguideProfileOperationResult,
) -> str:
    return (
        "Spiritguide profile patch:\n"
        f"{json.dumps(result.state_update, indent=2, sort_keys=True)}\n"
    )


def _spiritguide_state_update_to_dict(
    result: SpiritguideResult,
) -> dict[str, object]:
    update = result.state_update
    state: dict[str, object] = {}
    if update.host_identity is not None:
        state["host_identity"] = host_identity_to_state(update.host_identity)
    if update.cwd is not None:
        state["cwd"] = update.cwd
    if update.interactive_program is not None:
        state["interactive_program"] = interactive_program_to_state(
            update.interactive_program
        )
    if update.shell_environment is not None:
        state["shell_environment"] = shell_environment_to_state(
            update.shell_environment
        )
    if update.shell_stack is not None:
        state["shell_stack"] = [
            shell_frame_to_state(frame) for frame in update.shell_stack
        ]
    if update.shell_stack_transition is not None:
        state["shell_stack_transition"] = {
            "operation": update.shell_stack_transition.operation,
            "username": update.shell_stack_transition.username,
            "metadata": dict(update.shell_stack_transition.metadata),
        }
    if update.world_state is not None:
        state["world_state"] = update.world_state
    return state


def _spiritguide_patch_keys(result: SpiritguideResult) -> list[str]:
    return [
        name
        for name in (
            "host_identity",
            "cwd",
            "interactive_program",
            "shell_environment",
            "shell_stack",
            "shell_stack_transition",
            "world_state",
        )
        if getattr(result.state_update, name) is not None
    ]
