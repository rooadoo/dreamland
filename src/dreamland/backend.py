from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Coroutine, Iterator
from copy import deepcopy
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

from dreamland.agent import (
    CommandAgent,
    DeterministicCommandAgent,
    handle_builtin_command_context,
)
from dreamland.background_agents.enrichment.authentication import (
    observe_foreground_authentication_paths,
)
from dreamland.background_agents.event_bus import SessionEventBus, new_session_event
from dreamland.background_agents.events import EventSource, SessionEventType
from dreamland.background_agents.focus import (
    classify_presence_probe,
    infer_command_focus,
)
from dreamland.background_agents.surfaces import infer_command_surface
from dreamland.command_routing import plan_builtin_command_list
from dreamland.models import (
    CommandContext,
    CommandExecutionControl,
    CommandHistoryEntry,
    CommandOutputChunk,
    CommandResult,
    CommandStateUpdate,
    CommandStreamEvent,
    CommandStreamResult,
    ConnectionMetadata,
    SessionState,
    ShellFrame,
    normalize_command_history_streams,
)
from dreamland.network_connections import (
    close_session_connections,
    projected_environment_metadata,
)
from dreamland.profile import ServerProfile
from dreamland.program_input import ignores_blank_input
from dreamland.provider_failures import (
    ProviderFailureDisposition,
    ProviderFailureKind,
    classify_provider_failure,
)
from dreamland.runtime_logging import (
    reply_excerpt,
    trace_reasoning_summary,
    trace_shell_stack_event,
    trace_state_profile_boot,
)
from dreamland.session_state import (
    SHELL_STACK_EXIT_FIX_MARKER,
    active_shell_frame,
    append_host_command_history,
    apply_command_state_update,
)
from dreamland.state_profiles import (
    StateProfile,
    StateProfileRuntime,
    state_profile_to_session,
)
from dreamland.structured_failures import (
    STRUCTURED_FAILURE_CAPSULE_ID_METADATA,
    STRUCTURED_FAILURE_CAPSULE_PATH_METADATA,
    StructuredFailureStore,
    create_structured_failure_capsule,
    should_capture_structured_failure,
)
from dreamland.world_state import (
    active_host_metadata_for_context,
    active_host_ref,
    sync_active_host,
)

COMMAND_LOGGER = logging.getLogger("dreamland.commands")
REPLY_LOGGER = logging.getLogger("dreamland.replies")
FAILURE_LOGGER = logging.getLogger("dreamland.failures")
LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")
REASONING_SUMMARY_METADATA_KEY = "dreamland.reasoning_summary"
_PROVIDER_FAILURE_EXIT_STATUS = 75
_PROVIDER_FAILURE_MESSAGE = (
    "dreamland: transient provider error; command could not be completed\n"
)
_MAX_PROVIDER_LABEL_CHARS = 32
_MAX_MODEL_LABEL_CHARS = 160
_MAX_PROVIDER_CODE_CHARS = 96
_SAFE_AGENT_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*\Z")
_PROVIDER_FAILURE_KINDS = frozenset(kind.value for kind in ProviderFailureKind)
_PROVIDER_FAILURE_DISPOSITIONS = frozenset(
    disposition.value for disposition in ProviderFailureDisposition
)


class AgentBackend:
    """Backend adapter that turns session commands into agent contexts."""

    def __init__(
        self,
        profile: ServerProfile,
        agent: CommandAgent | None = None,
        initial_state_profile: StateProfile | None = None,
        state_profile_runtime: StateProfileRuntime | None = None,
        structured_failure_store: StructuredFailureStore | None = None,
        structured_failure_capture_enabled: bool = True,
        event_bus: SessionEventBus | None = None,
    ) -> None:
        self.profile = profile
        self.agent = agent or DeterministicCommandAgent()
        self.state_profile_runtime = state_profile_runtime or StateProfileRuntime(
            current_profile=initial_state_profile
        )
        self.structured_failure_store = (
            structured_failure_store or StructuredFailureStore()
        )
        self.structured_failure_capture_enabled = structured_failure_capture_enabled
        self.event_bus = event_bus

    def create_session(
        self,
        username: str,
        connection_metadata: ConnectionMetadata | None = None,
        *,
        state_profile: StateProfile | None = None,
    ) -> SessionState:
        return self._create_profile_session(
            username,
            connection_metadata,
            state_profile or self.state_profile_runtime.required_current_profile(),
        )

    def _create_profile_session(
        self,
        username: str,
        connection_metadata: ConnectionMetadata | None,
        profile: StateProfile,
    ) -> SessionState:
        session = state_profile_to_session(
            profile,
            current_session_id=uuid4().hex,
            fallback_username=username or "guest",
            connection_metadata=connection_metadata,
            authenticated_username=username,
        )
        LIFECYCLE_LOGGER.info(
            "State profile boot name=%s level=%s session=%s source_session=%s",
            profile.name,
            profile.level,
            session.langfuse_trace_id,
            profile.source_session_id,
        )
        trace_state_profile_boot(
            session.langfuse_trace_id,
            name=profile.name,
            level=profile.level,
            source_session_id=profile.source_session_id,
        )
        try:
            self.state_profile_runtime.store.record_session_profile(
                session_id=session.langfuse_trace_id,
                profile=profile,
                source="boot",
            )
        except Exception as exc:
            LIFECYCLE_LOGGER.warning(
                "State profile boot index update failed session=%s error=%s",
                session.langfuse_trace_id,
                type(exc).__name__,
            )
        return session

    def prompt(self, session: SessionState) -> str:
        frame = active_shell_frame(session)
        template = (
            frame.interactive_program.prompt_template
            or session.interactive_program.prompt_template
            or self.profile.prompt_template
        )
        return template.format(
            username=frame.username,
            hostname=frame.host_identity.hostname,
            cwd=_display_cwd(session, frame),
            program=frame.interactive_program.name,
        )

    def release_session(self, session: SessionState) -> None:
        """Release a retired native session after its command owners finish."""
        close_session_connections(session)
        release = getattr(self.agent, "release_session", None)
        if release is not None:
            release(session.langfuse_trace_id)
        LIFECYCLE_LOGGER.debug(
            "SSH visit runtime released session=%s", session.langfuse_trace_id
        )

    def execute(
        self,
        session: SessionState,
        command: str,
    ) -> CommandResult | Awaitable[CommandResult]:
        if ignores_blank_input(
            active_shell_frame(session).interactive_program, command
        ):
            return CommandResult()
        COMMAND_LOGGER.info(
            "user=%s cwd=%s command=%r",
            session.username,
            active_shell_frame(session).cwd,
            command,
        )

        plan = plan_builtin_command_list(session, command)
        if plan is not None:
            stdout = ""
            stderr = ""
            last_result = CommandResult()
            for segment, result in plan:
                last_result = self._record_builtin_segment(session, segment, result)
                stdout += last_result.stdout
                stderr += last_result.stderr
            return _combined_command_list_result(stdout, stderr, last_result)
        return self._execute_single(session, command)

    def _execute_single(
        self,
        session: SessionState,
        command: str,
    ) -> CommandResult | Awaitable[CommandResult]:
        starting_cwd = active_shell_frame(session).cwd
        context = self._context_from_session(session, command)
        self._publish_command_accepted(context)
        builtin_result = self._builtin_result(context)
        result: CommandResult | Awaitable[CommandResult]
        if builtin_result is not None:
            result = builtin_result
        else:
            try:
                result = self.agent.respond(context)
            except Exception as exc:
                result = self._classified_provider_failure(command, exc)
        if inspect.isawaitable(result):
            return self._record_awaitable_result(
                session,
                command,
                starting_cwd,
                result,
                command_id=context.command_id,
            )
        return self._record_result(
            session,
            command,
            starting_cwd,
            result,
            command_id=context.command_id,
        )

    def _record_builtin_segment(
        self,
        session: SessionState,
        command: str,
        result: CommandResult,
    ) -> CommandResult:
        context = self._context_from_session(session, command)
        self._publish_command_accepted(context)
        result = self._record_result(
            session, command, context.cwd, result, command_id=context.command_id
        )
        # Local list evaluation bypasses the agent's checkpoint. The next graph
        # invocation must receive the accepted authoritative session state.
        session.force_session_state_next_command = True
        return result

    def execute_stream(
        self,
        session: SessionState,
        command: str,
        control: CommandExecutionControl | None = None,
    ) -> Iterator[CommandStreamEvent]:
        if ignores_blank_input(
            active_shell_frame(session).interactive_program, command
        ):
            result = CommandResult()
            yield CommandStreamResult(result)
            return
        COMMAND_LOGGER.info(
            "user=%s cwd=%s command=%r",
            session.username,
            active_shell_frame(session).cwd,
            command,
        )

        plan = plan_builtin_command_list(session, command)
        if plan is not None:
            stdout = ""
            stderr = ""
            last_result = CommandResult()
            for segment, result in plan:
                if control is not None and control.cancelled():
                    last_result = control.rendered_result()
                    break
                last_result = self._record_builtin_segment(session, segment, result)
                stdout += last_result.stdout
                stderr += last_result.stderr
                yield CommandOutputChunk(last_result.stdout, "stdout")
                yield CommandOutputChunk(last_result.stderr, "stderr")
            yield CommandStreamResult(
                _combined_command_list_result(stdout, stderr, last_result)
            )
            return

        starting_cwd = active_shell_frame(session).cwd
        context = self._context_from_session(session, command)
        self._publish_command_accepted(context)
        builtin_result = self._builtin_result(context)
        if builtin_result is not None:
            result = self._record_result(
                session,
                command,
                starting_cwd,
                builtin_result,
                command_id=context.command_id,
            )
            yield CommandOutputChunk(result.stdout, "stdout")
            yield CommandOutputChunk(result.stderr, "stderr")
            yield CommandStreamResult(result)
            return

        stream_respond = getattr(self.agent, "stream_respond", None)
        if stream_respond is None:
            try:
                execution_result = self.agent.respond(context)
                if inspect.isawaitable(execution_result):
                    execution_result = _run_awaitable(execution_result)
            except Exception as exc:
                execution_result = self._classified_provider_failure(command, exc)
            if control is not None and control.cancelled():
                execution_result = control.rendered_result()
                if control.claim_recording():
                    execution_result = self._record_result(
                        session,
                        command,
                        starting_cwd,
                        execution_result,
                        command_id=context.command_id,
                    )
                yield CommandStreamResult(execution_result)
                return
            execution_result = self._record_result(
                session,
                command,
                starting_cwd,
                execution_result,
                command_id=context.command_id,
            )
            yield CommandOutputChunk(execution_result.stdout, "stdout")
            yield CommandOutputChunk(execution_result.stderr, "stderr")
            yield CommandStreamResult(execution_result)
            return

        final_result: CommandResult | None = None
        rendered_stdout = ""
        rendered_stderr = ""
        try:
            for event in _stream_agent_response(stream_respond, context, control):
                if control is not None and control.cancelled():
                    result = control.rendered_result()
                    final_result = (
                        self._record_result(
                            session,
                            command,
                            starting_cwd,
                            result,
                            command_id=context.command_id,
                        )
                        if control.claim_recording()
                        else result
                    )
                    yield CommandStreamResult(final_result)
                    return
                if isinstance(event, CommandStreamResult):
                    result = (
                        control.rendered_result()
                        if control is not None and control.cancelled()
                        else event.result
                    )
                    final_result = (
                        self._record_result(
                            session,
                            command,
                            starting_cwd,
                            result,
                            command_id=context.command_id,
                        )
                        if control is None
                        or not control.cancelled()
                        or control.claim_recording()
                        else result
                    )
                    yield CommandStreamResult(final_result)
                else:
                    if event.stream == "stdout":
                        rendered_stdout += event.text
                    else:
                        rendered_stderr += event.text
                    yield event
        except Exception as exc:
            if control is not None and control.cancelled():
                result = control.rendered_result()
                final_result = (
                    self._record_result(
                        session,
                        command,
                        starting_cwd,
                        result,
                        command_id=context.command_id,
                    )
                    if control.claim_recording()
                    else result
                )
                yield CommandStreamResult(final_result)
                return
            result = self._classified_provider_failure(
                command,
                exc,
                stdout=rendered_stdout,
                stderr=f"{rendered_stderr}{_PROVIDER_FAILURE_MESSAGE}",
            )
            final_result = self._record_result(
                session,
                command,
                starting_cwd,
                result,
                command_id=context.command_id,
            )
            yield CommandOutputChunk(_PROVIDER_FAILURE_MESSAGE, "stderr")
            yield CommandStreamResult(final_result)
            return
        if final_result is None:
            result = (
                control.rendered_result()
                if control is not None and control.cancelled()
                else CommandResult()
            )
            final_result = (
                self._record_result(
                    session,
                    command,
                    starting_cwd,
                    result,
                    command_id=context.command_id,
                )
                if control is None
                or not control.cancelled()
                or control.claim_recording()
                else result
            )
            yield CommandStreamResult(final_result)

    def _context_from_session(
        self,
        session: SessionState,
        command: str,
    ) -> CommandContext:
        frame = active_shell_frame(session)
        session.world_state = sync_active_host(
            session.world_state,
            frame.host_identity.hostname,
        )
        observe_foreground_authentication_paths(session, command)
        if session.force_session_state_next_command:
            trace_shell_stack_event(
                session.langfuse_trace_id,
                event="command_context_force_refresh",
                marker=SHELL_STACK_EXIT_FIX_MARKER,
                command=command,
                previous_depth=len(session.shell_stack),
                next_depth=len(session.shell_stack),
                previous_host=session.host_identity.hostname,
                next_host=frame.host_identity.hostname,
                previous_program=frame.interactive_program.name,
                next_program=frame.interactive_program.name,
                force_session_state_next_command=session.force_session_state_next_command,
            )
        return CommandContext(
            username=frame.username,
            login_username=session.login_username or session.username,
            cwd=frame.cwd,
            hostname=frame.host_identity.hostname,
            host_identity=frame.host_identity,
            command=command,
            langfuse_trace_id=session.langfuse_trace_id,
            interactive_program=frame.interactive_program,
            shell_environment=frame.shell_environment,
            shell_stack=tuple(session.shell_stack),
            environment_metadata=projected_environment_metadata(session),
            world_state=deepcopy(session.world_state),
            active_host=active_host_ref(session.world_state),
            active_host_metadata=active_host_metadata_for_context(session.world_state),
            force_session_state=session.force_session_state_next_command,
            runtime_provenance=session.runtime_provenance,
            transport_connection=session.environment_metadata.connection,
        )

    def _builtin_result(self, context: CommandContext) -> CommandResult | None:
        if getattr(self.agent, "handles_builtins", False):
            return None
        return handle_builtin_command_context(context)

    def _record_result(
        self,
        session: SessionState,
        command: str,
        cwd: str,
        result: CommandResult,
        *,
        command_id: str,
    ) -> CommandResult:
        previous_host = active_host_ref(session.world_state)
        previous_host_refs = _host_registry_keys(session.world_state)
        result = _capture_structured_failure(
            session,
            command,
            cwd,
            result,
            command_id=command_id,
            structured_failure_store=self.structured_failure_store,
            structured_failure_capture_enabled=(
                self.structured_failure_capture_enabled
            ),
        )
        recorded = _record_result(
            session,
            command,
            cwd,
            result,
            command_id=command_id,
        )
        self._publish_command_completed(
            session,
            command,
            recorded,
            command_id=command_id,
            starting_cwd=cwd,
            previous_host=previous_host,
            previous_host_refs=previous_host_refs,
        )
        return recorded

    def _publish_command_accepted(self, context: CommandContext) -> None:
        if self.event_bus is None:
            return
        self.event_bus.publish(
            new_session_event(
                session_id=context.langfuse_trace_id,
                event_type=SessionEventType.COMMAND_ACCEPTED,
                source=EventSource.USER_COMMAND,
                correlation_id=context.command_id,
                command_id=context.command_id,
                target_host=context.active_host or context.hostname,
                surface=infer_command_surface(context.command),
                payload={
                    "command": context.command,
                    "interaction_focus": infer_command_focus(
                        context.command,
                        starting_cwd=context.cwd,
                        result_cwd=context.cwd,
                    ),
                },
            )
        )

    def _publish_command_completed(
        self,
        session: SessionState,
        command: str,
        result: CommandResult,
        *,
        command_id: str,
        starting_cwd: str,
        previous_host: str,
        previous_host_refs: frozenset[str],
    ) -> None:
        if self.event_bus is None:
            return
        target_host = active_host_ref(session.world_state)
        surface = infer_command_surface(command)
        current_host_refs = _host_registry_keys(session.world_state)
        discovered_hosts = tuple(sorted(current_host_refs - previous_host_refs))
        active_host_changed = previous_host != target_host
        interaction_focus = infer_command_focus(
            command,
            starting_cwd=starting_cwd,
            result_cwd=session.cwd,
        )
        world_patches = [
            update.world_state
            for update in (
                result.state_update,
                *(effect.state_update for effect in result.state_update.effects),
            )
            if update.world_state is not None
        ]
        payload: dict[str, Any] = {
            "exit_status": result.exit_status,
            "interrupted": result.interrupted,
            "state_update_present": bool(world_patches),
            "active_host_changed": active_host_changed,
            # Higher-level subscribers can make a deterministic priority
            # decision before the more specific events below arrive.
            "new_hosts": discovered_hosts,
            "interaction_focus": interaction_focus,
        }
        presence_probe = classify_presence_probe(command)
        if presence_probe is not None:
            payload["presence_probe"] = presence_probe.value
        provider_failure = _provider_failure_event_payload(result.metadata)
        if provider_failure is not None:
            payload["provider_failure"] = provider_failure
        completed = new_session_event(
            session_id=session.langfuse_trace_id,
            event_type=SessionEventType.COMMAND_COMPLETED,
            source=EventSource.COMMAND_EMULATOR,
            correlation_id=command_id,
            command_id=command_id,
            target_host=target_host,
            surface=surface,
            payload=payload,
        )
        self.event_bus.publish(completed)
        if active_host_changed:
            self.event_bus.publish(
                new_session_event(
                    session_id=session.langfuse_trace_id,
                    event_type=SessionEventType.ACTIVE_HOST_CHANGED,
                    source=EventSource.COMMAND_EMULATOR,
                    correlation_id=command_id,
                    caused_by_event_id=completed.event_id,
                    command_id=command_id,
                    target_host=target_host,
                    surface="host",
                    anchors=(previous_host, target_host),
                    payload={
                        "previous_host": previous_host,
                        "interaction_focus": interaction_focus,
                    },
                )
            )
        if world_patches or discovered_hosts:
            kind = "host_discovered" if discovered_hosts else "state_materialized"
            root_keys = tuple(
                sorted({str(key) for patch in world_patches for key in patch})
            )
            self.event_bus.publish(
                new_session_event(
                    session_id=session.langfuse_trace_id,
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    correlation_id=command_id,
                    caused_by_event_id=completed.event_id,
                    command_id=command_id,
                    target_host=target_host,
                    surface=surface,
                    anchors=discovered_hosts,
                    payload={
                        "kind": kind,
                        "new_hosts": discovered_hosts,
                        "active_host_changed": active_host_changed,
                        "state_update_root_keys": root_keys,
                        "interaction_focus": interaction_focus,
                    },
                )
            )

    async def _record_awaitable_result(
        self,
        session: SessionState,
        command: str,
        cwd: str,
        result: Awaitable[CommandResult],
        *,
        command_id: str,
    ) -> CommandResult:
        try:
            resolved = await result
        except Exception as exc:
            resolved = self._classified_provider_failure(command, exc)
        return self._record_result(
            session,
            command,
            cwd,
            resolved,
            command_id=command_id,
        )

    def _classified_provider_failure(
        self,
        command: str,
        error: Exception,
        *,
        stdout: str = "",
        stderr: str = _PROVIDER_FAILURE_MESSAGE,
    ) -> CommandResult:
        return _provider_failure_result(
            command,
            error,
            stdout=stdout,
            stderr=stderr,
            provider=_bounded_agent_attribute(
                self.agent,
                "provider",
                maximum=_MAX_PROVIDER_LABEL_CHARS,
            ),
            model=_bounded_agent_attribute(
                self.agent,
                "model_name",
                maximum=_MAX_MODEL_LABEL_CHARS,
            ),
        )


def _record_result(
    session: SessionState,
    command: str,
    cwd: str,
    result: CommandResult,
    *,
    command_id: str,
) -> CommandResult:
    if result.interrupted:
        result = replace(result, state_update=CommandStateUpdate())
    source_hostname = session.host_identity.hostname
    history_stdout, history_stderr = normalize_command_history_streams(
        result.stdout,
        result.stderr,
    )
    history_entry = CommandHistoryEntry(
        cwd=cwd,
        command=command,
        id=command_id,
        stdout=history_stdout,
        stderr=history_stderr,
        exit_status=result.exit_status,
        interrupted=result.interrupted,
        signal=result.signal,
    )
    session.world_state = append_host_command_history(
        session.world_state,
        source_hostname,
        history_entry,
    )
    session.force_session_state_next_command = result.interrupted
    if not result.interrupted:
        apply_command_state_update(session, result.state_update, command=command)
        observe_foreground_authentication_paths(session)
    session.history.append(history_entry)
    _log_reply(command, result)
    summary = result.metadata.get(REASONING_SUMMARY_METADATA_KEY)
    if isinstance(summary, str) and summary:
        trace_reasoning_summary(session.langfuse_trace_id, command, summary)
    return result


def _capture_structured_failure(
    session: SessionState,
    command: str,
    cwd: str,
    result: CommandResult,
    *,
    command_id: str,
    structured_failure_store: StructuredFailureStore,
    structured_failure_capture_enabled: bool,
) -> CommandResult:
    if not structured_failure_capture_enabled:
        return result
    if not should_capture_structured_failure(result):
        return result
    try:
        capsule = create_structured_failure_capsule(
            session,
            command=command,
            command_id=command_id,
            starting_cwd=cwd,
            result=result,
        )
        path = structured_failure_store.save(capsule)
    except Exception as exc:
        FAILURE_LOGGER.warning(
            "Structured failure capsule capture failed session=%s command_id=%s "
            "error=%s",
            session.langfuse_trace_id,
            command_id,
            type(exc).__name__,
        )
        return result
    FAILURE_LOGGER.warning(
        "Structured response failure captured capsule=%s session=%s command_id=%s "
        "failure_class=%s error=%r",
        capsule.id,
        session.langfuse_trace_id,
        command_id,
        capsule.failure_class,
        capsule.parser_error,
    )
    return replace(
        result,
        metadata={
            **result.metadata,
            STRUCTURED_FAILURE_CAPSULE_ID_METADATA: capsule.id,
            STRUCTURED_FAILURE_CAPSULE_PATH_METADATA: str(path),
        },
    )


def _combined_command_list_result(
    stdout: str,
    stderr: str,
    last_result: CommandResult,
) -> CommandResult:
    return CommandResult(
        stdout=stdout,
        stderr=stderr,
        exit_status=last_result.exit_status,
        interrupted=last_result.interrupted,
        signal=last_result.signal,
        metadata=last_result.metadata,
    )


def _log_reply(command: str, result: CommandResult) -> None:
    excerpt = reply_excerpt(result.stdout, result.stderr)
    REPLY_LOGGER.info(
        "command=%r exit_status=%s excerpt=%r",
        command,
        result.exit_status,
        excerpt,
    )


def _provider_failure_result(
    command: str,
    exc: Exception,
    *,
    stdout: str = "",
    stderr: str = _PROVIDER_FAILURE_MESSAGE,
    provider: str | None = None,
    model: str | None = None,
) -> CommandResult:
    classification = classify_provider_failure(exc)
    provider = _bounded_agent_label(provider, maximum=_MAX_PROVIDER_LABEL_CHARS)
    model = _bounded_agent_label(model, maximum=_MAX_MODEL_LABEL_CHARS)
    FAILURE_LOGGER.error(
        "command=%r provider_error=%s failure_kind=%s "
        "failure_disposition=%s provider=%s model=%s provider_code=%s "
        "provider_status=%s",
        command,
        type(exc).__name__,
        classification.kind.value,
        classification.disposition.value,
        provider,
        model,
        classification.provider_code,
        classification.status_code,
    )
    metadata: dict[str, object] = {
        "dreamland.failure.type": "provider",
        "dreamland.failure.exception": type(exc).__name__,
        "dreamland.failure.kind": classification.kind.value,
        "dreamland.failure.disposition": classification.disposition.value,
    }
    if provider is not None:
        metadata["dreamland.failure.provider"] = provider
    if model is not None:
        metadata["dreamland.failure.model"] = model
    if classification.provider_code is not None:
        metadata["dreamland.failure.code"] = classification.provider_code
    if classification.status_code is not None:
        metadata["dreamland.failure.status"] = classification.status_code
    return CommandResult(
        stdout=stdout,
        stderr=stderr,
        exit_status=_PROVIDER_FAILURE_EXIT_STATUS,
        metadata=metadata,
    )


def _bounded_agent_attribute(
    agent: object,
    name: str,
    *,
    maximum: int,
) -> str | None:
    try:
        value = getattr(agent, name, None)
    except Exception:
        return None
    return _bounded_agent_label(value, maximum=maximum)


def _bounded_agent_label(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    label = value.strip()
    if not label or len(label) > maximum or _SAFE_AGENT_LABEL.fullmatch(label) is None:
        return None
    return label


def _provider_failure_event_payload(
    metadata: dict[str, object],
) -> dict[str, object] | None:
    if metadata.get("dreamland.failure.type") != "provider":
        return None
    kind = metadata.get("dreamland.failure.kind")
    disposition = metadata.get("dreamland.failure.disposition")
    if (
        not isinstance(kind, str)
        or kind not in _PROVIDER_FAILURE_KINDS
        or not isinstance(disposition, str)
        or disposition not in _PROVIDER_FAILURE_DISPOSITIONS
    ):
        return None
    payload: dict[str, object] = {
        "kind": kind,
        "disposition": disposition,
    }
    provider = _bounded_agent_label(
        metadata.get("dreamland.failure.provider"),
        maximum=_MAX_PROVIDER_LABEL_CHARS,
    )
    model = _bounded_agent_label(
        metadata.get("dreamland.failure.model"),
        maximum=_MAX_MODEL_LABEL_CHARS,
    )
    code = metadata.get("dreamland.failure.code")
    status = metadata.get("dreamland.failure.status")
    if provider is not None:
        payload["provider"] = provider
    if model is not None:
        payload["model"] = model
    if (
        isinstance(code, str)
        and len(code) <= _MAX_PROVIDER_CODE_CHARS
        and _SAFE_AGENT_LABEL.fullmatch(code) is not None
    ):
        payload["code"] = code
    if (
        isinstance(status, int)
        and not isinstance(status, bool)
        and 100 <= status <= 599
    ):
        payload["status"] = status
    return payload


def _stream_agent_response(
    stream_respond,
    context: CommandContext,
    control: CommandExecutionControl | None,
) -> Iterator[CommandStreamEvent]:
    try:
        signature = inspect.signature(stream_respond)
    except (TypeError, ValueError):
        yield from stream_respond(context)
        return
    if "control" in signature.parameters:
        yield from stream_respond(context, control=control)
        return
    yield from stream_respond(context)


def _run_awaitable(result: Awaitable[CommandResult]) -> CommandResult:
    return asyncio.run(cast(Coroutine[Any, Any, CommandResult], result))


def _display_cwd(session: SessionState, frame: ShellFrame | None = None) -> str:
    active_frame = frame or active_shell_frame(session)
    home = active_frame.shell_environment.variables.get(
        "HOME",
        f"/home/{active_frame.username}",
    )
    if active_frame.cwd == home:
        return "~"
    if active_frame.cwd.startswith(f"{home}/"):
        return f"~/{active_frame.cwd[len(home) + 1 :]}"
    return active_frame.cwd


def _host_registry_keys(world_state: dict[str, object]) -> frozenset[str]:
    hosts = world_state.get("hosts_by_name")
    if not isinstance(hosts, dict):
        return frozenset()
    return frozenset(str(key) for key in hosts)
