from __future__ import annotations

import hashlib
import json
import logging
import warnings
from collections.abc import Awaitable, Iterator
from copy import deepcopy
from dataclasses import replace
from typing import Any, Literal, Protocol, cast

from dreamland.agent_builtins import (
    handle_builtin_command_if_deterministic as _handle_builtin_command_if_deterministic,
)
from dreamland.agent_state import (
    coerce_int as _coerce_int,
)
from dreamland.agent_state import (
    environment_metadata_from_state as _environment_metadata_from_state,
)
from dreamland.agent_state import (
    environment_metadata_to_state as _environment_metadata_to_state,
)
from dreamland.agent_state import (
    host_identity_from_state as _host_identity_from_state,
)
from dreamland.agent_state import (
    host_identity_to_state as _host_identity_to_state,
)
from dreamland.agent_state import (
    interactive_program_from_state as _interactive_program_from_state,
)
from dreamland.agent_state import (
    interactive_program_to_state as _interactive_program_to_state,
)
from dreamland.agent_state import (
    optional_host_identity_from_state as _optional_host_identity_from_state,
)
from dreamland.agent_state import (
    optional_int as _optional_int,
)
from dreamland.agent_state import (
    optional_string as _optional_string,
)
from dreamland.agent_state import (
    shell_environment_from_state as _shell_environment_from_state,
)
from dreamland.agent_state import (
    shell_environment_to_state as _shell_environment_to_state,
)
from dreamland.agent_state import (
    shell_frame_from_state as _shell_frame_from_state,
)
from dreamland.agent_state import (
    shell_frame_to_state as _shell_frame_to_state,
)
from dreamland.agent_state import (
    shell_stack_from_state as _shell_stack_from_state,
)
from dreamland.agent_structured import (
    STRUCTURED_FAILURE_CLASS_METADATA,
    STRUCTURED_RAW_RESPONSE_METADATA,
    classify_structured_failure,
)
from dreamland.agent_structured import (
    command_result_from_model_response as _command_result_from_model_response,
)
from dreamland.agent_structured import (
    structured_stdout_prefix as _structured_stdout_prefix,
)
from dreamland.agent_tracing import (
    TokenUsageLoggingCallback,
    create_langfuse_callback_handler,
)
from dreamland.agent_tracing import (
    build_langfuse_callbacks as _build_langfuse_callbacks,
)
from dreamland.agent_tracing import (
    graph_invoke_config as _graph_invoke_config,
)
from dreamland.agent_tracing import (
    initialize_langfuse_client as _initialize_langfuse_client,
)
from dreamland.command_routing import aggregate_builtin_plan, plan_builtin_command_list
from dreamland.emulator_context import project_world_state_for_command_emulator
from dreamland.model_providers import (
    ChatModelProviderConfig,
    build_chat_model,
    supports_openai_explicit_cache,
)
from dreamland.models import (
    CommandContext,
    CommandExecutionControl,
    CommandHistoryEntry,
    CommandOutputChunk,
    CommandResult,
    CommandStateEffect,
    CommandStateUpdate,
    CommandStreamEvent,
    CommandStreamResult,
    ConnectionMetadata,
    DreamlandCommandState,
    EnvironmentMetadata,
    InteractiveProgram,
    SessionState,
    ShellEnvironment,
    ShellFrame,
    ShellStackTransition,
    default_shell_environment,
    default_shell_frame,
)
from dreamland.program_input import (
    has_pending_shell_input,
    ignores_blank_input,
    is_command_shell,
)
from dreamland.prompt_loader import load_prompt
from dreamland.session_state import (
    MAX_COMMAND_STATE_EFFECTS,
    append_host_command_history,
    apply_command_state_update,
    merge_world_state,
)
from dreamland.world_state import (
    active_host_metadata_for_context,
    active_host_ref,
    format_world_state_for_prompt,
    sync_active_host,
)

LOGGER = logging.getLogger(__name__)
REASONING_SUMMARY_METADATA_KEY = "dreamland.reasoning_summary"
_ANTHROPIC_EPHEMERAL_CACHE_CONTROL = {"type": "ephemeral"}
_OPENAI_COMMAND_CACHE_KEY_NAMESPACE = "dreamland-command-v1"
_OPENAI_EXPLICIT_CACHE_MODE = "explicit"


def _DreamlandLangfuseCallbackHandler(
    *,
    base_handler: Any,
    public_key: str | None,
    trace_context: dict[str, str] | None,
) -> Any:
    return create_langfuse_callback_handler(
        base_handler=base_handler,
        public_key=public_key,
        trace_context=trace_context,
    )


class CommandAgent(Protocol):
    """Boundary for command generation backends.

    Implementations may be synchronous or asynchronous. This keeps the SSH
    transport independent from the LangGraph integration.
    """

    def respond(
        self,
        context: CommandContext,
    ) -> CommandResult | Awaitable[CommandResult]:
        pass

    def stream_respond(
        self,
        context: CommandContext,
        control: CommandExecutionControl | None = None,
    ) -> Iterator[CommandStreamEvent]:
        pass


class LangGraphCommandAgent:
    """LangGraph-backed command agent using a configured chat model provider."""

    handles_builtins = True

    def __init__(
        self,
        model_name: str,
        system_prompt_name: str,
        provider: str = "openai",
        model: Any | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        langfuse_enabled: bool = False,
        langfuse_public_key: str | None = None,
        langfuse_secret_key: str | None = None,
        langfuse_host: str | None = None,
        callbacks: tuple[Any, ...] | None = None,
        checkpointer: Any | None = None,
        reasoning_effort: str | None = None,
        reasoning_summary: str | None = None,
        verbosity: str | None = None,
        speed: str | None = None,
        prompt_cache: str | None = None,
        max_output_tokens: int | None = None,
        max_retries: int | None = None,
        redact_token_usage_command: bool = False,
        openai_api: str | None = None,
        openai_service_tier: str | None = None,
        openai_reasoning_mode: str | None = None,
    ) -> None:
        if not isinstance(redact_token_usage_command, bool):
            raise TypeError("redact_token_usage_command must be a boolean")
        if max_retries is not None and (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or not 0 <= max_retries <= 1_000_000
        ):
            raise ValueError("max_retries must be a bounded non-negative integer")
        self.provider = provider
        self.model_name = model_name
        self.reasoning_effort = reasoning_effort
        self.reasoning_summary = reasoning_summary
        self.verbosity = verbosity
        self.speed = speed
        self.prompt_cache = prompt_cache
        self.openai_api = openai_api
        self.openai_service_tier = openai_service_tier
        self.openai_reasoning_mode = openai_reasoning_mode
        self.max_output_tokens = max_output_tokens
        self.max_retries = max_retries
        self._redact_token_usage_command = redact_token_usage_command
        self.api_key = api_key
        self.system_prompt = system_prompt or load_prompt(system_prompt_name)
        self.model = model or self._default_model()
        self._callbacks = callbacks
        self._langfuse_enabled = langfuse_enabled
        self._langfuse_public_key = langfuse_public_key
        self._langfuse_secret_key = langfuse_secret_key
        self._langfuse_host = langfuse_host
        self._checkpointer = checkpointer
        if callbacks is None and langfuse_enabled:
            _initialize_langfuse_client(
                enabled=langfuse_enabled,
                public_key=langfuse_public_key,
                secret_key=langfuse_secret_key,
                host=langfuse_host,
            )
        self.graph = self._build_graph()

    def respond(self, context: CommandContext) -> CommandResult:
        callbacks = self._callbacks_for_context(context)
        config = _graph_invoke_config(callbacks, context)
        include_session_state = (
            context.force_session_state
            or not _graph_has_checkpoint_state(self.graph, config)
        )
        result = self.graph.invoke(
            _state_input_from_context(
                context, include_session_state=include_session_state
            ),
            config=config,
        )
        command_result = _result_from_state(result.get("result"))
        if command_result is not None:
            return command_result
        return CommandResult(
            stdout=_ensure_trailing_newline(result.get("response", ""))
        )

    def stream_respond(
        self,
        context: CommandContext,
        control: CommandExecutionControl | None = None,
    ) -> Iterator[CommandStreamEvent]:
        callbacks = self._callbacks_for_context(context)
        config = _graph_invoke_config(callbacks, context)
        include_session_state = (
            context.force_session_state
            or not _graph_has_checkpoint_state(self.graph, config)
        )
        result: CommandResult | None = None
        streamed_text = ""
        pending_streamed_text = ""
        stream_decision: str | None = None
        structured_stdout = ""
        stream = self.graph.stream(
            _state_input_from_context(
                context, include_session_state=include_session_state
            ),
            config=config,
            stream_mode=["messages", "updates"],
        )
        if control is not None:
            # Cancellation callbacks may run on an observer thread.  Keep the
            # graph stream and checkpoint state owned by this iterator thread;
            # its ``finally`` block performs cancellation cleanup below.
            for callback in callbacks:
                mark_interrupted = getattr(callback, "mark_dreamland_interrupted", None)
                if mark_interrupted is not None:
                    control.add_cancel_callback(mark_interrupted)

        try:
            for mode, payload in stream:
                if control is not None and control.cancelled():
                    result = control.rendered_result()
                    _record_interrupted_graph_result_once(
                        self.graph,
                        config,
                        context,
                        control,
                    )
                    yield CommandStreamResult(result)
                    return
                if mode == "messages":
                    text = _message_content_to_text(payload[0])
                    if not text:
                        continue
                    streamed_text += text
                    if stream_decision == "structured":
                        pending_streamed_text += text
                        stdout = _structured_stdout_prefix(streamed_text)
                        if stdout is not None and len(stdout) > len(structured_stdout):
                            delta = stdout[len(structured_stdout) :]
                            structured_stdout = stdout
                            yield CommandOutputChunk(delta)
                        continue
                    if stream_decision == "plain":
                        yield CommandOutputChunk(text)
                        continue
                    pending_streamed_text += text
                    stripped = pending_streamed_text.lstrip()
                    if not stripped:
                        continue
                    if _streamed_response_looks_structured(stripped):
                        stream_decision = "structured"
                        stdout = _structured_stdout_prefix(streamed_text)
                        if stdout is not None:
                            structured_stdout = stdout
                            if stdout:
                                yield CommandOutputChunk(stdout)
                        continue
                    stream_decision = "plain"
                    yield CommandOutputChunk(pending_streamed_text)
                    pending_streamed_text = ""
                elif mode == "updates":
                    result = _result_from_graph_update(payload) or result
        finally:
            if control is not None and control.cancelled():
                close = getattr(stream, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        LOGGER.debug(
                            "Failed to close cancelled graph stream",
                            exc_info=True,
                        )
                _record_interrupted_graph_result_once(
                    self.graph,
                    config,
                    context,
                    control,
                )

        if control is not None and control.cancelled():
            result = control.rendered_result()
            _record_interrupted_graph_result_once(self.graph, config, context, control)
            yield CommandStreamResult(result)
            return

        if result is None:
            result = _command_result_from_model_response(streamed_text)
        yield CommandStreamResult(result)

    def _callbacks_for_context(self, context: CommandContext) -> tuple[Any, ...]:
        token_callback = TokenUsageLoggingCallback(
            self.model_name,
            command=(
                "[redacted]" if self._redact_token_usage_command else context.command
            ),
            max_output_tokens=self.max_output_tokens,
        )
        callbacks = (
            self._callbacks
            if self._callbacks is not None
            else _build_langfuse_callbacks(
                enabled=self._langfuse_enabled,
                public_key=self._langfuse_public_key,
                secret_key=self._langfuse_secret_key,
                host=self._langfuse_host,
            )
        )
        return (token_callback, *callbacks)

    def _default_model(self) -> Any:
        return build_chat_model(
            ChatModelProviderConfig(
                provider=self.provider,
                model=self.model_name,
                api_key=self.api_key,
                reasoning_effort=self.reasoning_effort,
                reasoning_summary=self.reasoning_summary,
                verbosity=self.verbosity,
                speed=self.speed,
                prompt_cache=self.prompt_cache,
                max_output_tokens=self.max_output_tokens,
                openai_api=self.openai_api,
                openai_service_tier=self.openai_service_tier,
                openai_reasoning_mode=self.openai_reasoning_mode,
                max_retries=self.max_retries,
                streaming=True,
            )
        )

    def release_session(self, session_id: str) -> None:
        """Reclaim one completed session's checkpoint namespace.

        The caller owns quiescence: cancellation alone is insufficient because
        a still-running stream may write its final interrupted checkpoint.
        """
        checkpointer = getattr(self.graph, "checkpointer", None)
        delete_thread = getattr(checkpointer, "delete_thread", None)
        if delete_thread is not None:
            delete_thread(session_id)

    def _build_graph(self) -> Any:
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langgraph.checkpoint.memory import InMemorySaver
            from langgraph.graph import END, START, StateGraph
        except (
            ImportError
        ) as exc:  # pragma: no cover - depends on optional runtime deps.
            raise RuntimeError(
                "LangGraphCommandAgent requires langgraph and langchain-core. "
                "Install the project dependencies first."
            ) from exc

        def prepare_command(state: DreamlandCommandState) -> dict[str, Any]:
            command = state.get("current_command", "")
            updates: dict[str, Any] = {"current_command": command}
            if ignores_blank_input(
                _interactive_program_from_state(state.get("interactive_program")),
                command,
            ):
                updates["result"] = _result_to_state(CommandResult())
            return updates

        def assign_interactive_program(state: DreamlandCommandState) -> dict[str, Any]:
            interactive_program = _interactive_program_from_state(
                state.get("interactive_program")
            )
            shell_environment = _shell_environment_from_state(
                state.get("shell_environment")
            )
            return {
                "interactive_program": _interactive_program_to_state(
                    interactive_program
                ),
                "shell_environment": _shell_environment_to_state(shell_environment),
                "shell_stack": [
                    _shell_frame_to_state(frame)
                    for frame in _shell_stack_from_state(
                        state.get("shell_stack"),
                        username=str(state.get("username", "guest")),
                        hostname=_hostname_from_state(state),
                        cwd=str(state.get("cwd", "/home/guest")),
                        interactive_program=interactive_program,
                        shell_environment=shell_environment,
                    )
                ],
                "world_state": _world_state_from_state(state.get("world_state")),
                "active_host": active_host_ref(
                    _world_state_from_state(state.get("world_state"))
                ),
                "active_host_metadata": active_host_metadata_for_context(
                    _world_state_from_state(state.get("world_state"))
                ),
            }

        def route_command(state: DreamlandCommandState) -> str:
            command = state.get("current_command", "")
            if ignores_blank_input(
                _interactive_program_from_state(state.get("interactive_program")),
                command,
            ):
                return "record_history"
            if _builtin_result_for_command_state(state) is not None:
                return "handle_builtin"
            return "call_model"

        def handle_builtin(
            state: DreamlandCommandState,
        ) -> dict[str, dict[str, object]]:
            result = _builtin_result_for_command_state(state)
            return {"result": _result_to_state(result or CommandResult())}

        def call_model(
            state: DreamlandCommandState,
        ) -> dict[str, dict[str, object] | str]:
            rendered_system_prompt = _system_prompt_for_active_program(
                self.system_prompt,
                state,
            )
            system_content, model_invoke_kwargs = (
                _command_system_prompt_and_invoke_kwargs(
                    rendered_system_prompt,
                    provider=self.provider,
                    model=self.model_name,
                    prompt_cache=self.prompt_cache,
                )
            )
            response = self.model.invoke(
                [
                    SystemMessage(content=system_content),
                    HumanMessage(content=_format_command_state(state)),
                ],
                **model_invoke_kwargs,
            )
            reasoning_summary = _reasoning_summary_from_message(response)
            result = _command_result_from_model_response(
                _message_content_to_text(response),
                state,
            )
            if reasoning_summary is not None:
                result = replace(
                    result,
                    metadata={
                        **result.metadata,
                        REASONING_SUMMARY_METADATA_KEY: reasoning_summary,
                    },
                )
            return {
                "response": result.stdout,
                "result": _result_to_state(result),
            }

        def record_history(state: DreamlandCommandState) -> dict[str, Any]:
            command = state.get("current_command", "")
            result = _result_from_state(state.get("result")) or CommandResult()
            starting_stack = tuple(
                _shell_frame_from_state(frame) for frame in state.get("shell_stack", [])
            )
            updates: dict[str, Any] = {
                "response": result.stdout,
            }
            session = _session_from_command_state(state)
            apply_command_state_update(
                session,
                result.state_update,
                command=command,
                reconcile_network=False,
            )
            updates.update(_command_state_updates_from_session(session))
            resulting_stack = tuple(
                _shell_frame_from_state(frame)
                for frame in updates.get("shell_stack", state.get("shell_stack", []))
            )
            current_world_state = _world_state_from_state(
                updates.get("world_state", state.get("world_state"))
            )
            if not ignores_blank_input(
                _interactive_program_from_state(state.get("interactive_program")),
                command,
            ):
                current_world_state = append_host_command_history(
                    current_world_state,
                    str(state.get("hostname", "ubuntu")),
                    CommandHistoryEntry(
                        cwd=str(state.get("cwd", "/")),
                        command=command,
                        stdout=result.stdout,
                        stderr=result.stderr,
                        exit_status=result.exit_status,
                        interrupted=result.interrupted,
                        signal=result.signal,
                    ),
                )
            active_hostname = str(
                updates.get("hostname", state.get("hostname", "ubuntu"))
            )
            synced_world_state = sync_active_host(current_world_state, active_hostname)
            updates["world_state"] = synced_world_state
            updates["active_host"] = active_host_ref(synced_world_state)
            updates["active_host_metadata"] = active_host_metadata_for_context(
                synced_world_state
            )
            result = _result_with_transition_audit(
                result,
                starting_stack=starting_stack,
                resulting_stack=resulting_stack,
            )
            updates["result"] = _result_to_state(result)
            return updates

        graph = StateGraph(DreamlandCommandState)
        graph.add_node("prepare_command", prepare_command)
        graph.add_node("assign_interactive_program", assign_interactive_program)
        graph.add_node("handle_builtin", handle_builtin)
        graph.add_node("call_model", call_model)
        graph.add_node("record_history", record_history)
        graph.add_edge(START, "prepare_command")
        graph.add_conditional_edges(
            "assign_interactive_program",
            route_command,
            {
                "handle_builtin": "handle_builtin",
                "call_model": "call_model",
                "record_history": "record_history",
            },
        )
        graph.add_edge("prepare_command", "assign_interactive_program")
        graph.add_edge("handle_builtin", "record_history")
        graph.add_edge("call_model", "record_history")
        graph.add_edge("record_history", END)
        return graph.compile(checkpointer=self._checkpointer or InMemorySaver())


class DeterministicCommandAgent:
    """Deterministic command agent used when no model-backed agent is configured."""

    handles_builtins = True

    def respond(self, context: CommandContext) -> CommandResult:
        command = context.command
        builtin_result = _builtin_result_for_command_state(
            _state_from_context_for_builtin(context)
        )
        if builtin_result is not None:
            return builtin_result
        return CommandResult(
            stdout=(f"[deterministic-agent] received command: {command}\n")
        )


class MockCommandAgent(DeterministicCommandAgent):
    """Deprecated compatibility name for :class:`DeterministicCommandAgent`."""

    def __init__(self) -> None:
        warnings.warn(
            "MockCommandAgent is deprecated; use DeterministicCommandAgent instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__()


def handle_builtin_command_context(context: CommandContext) -> CommandResult | None:
    return _handle_builtin_command_if_deterministic(
        _state_from_context_for_builtin(context)
    )


def _builtin_result_for_command_state(
    state: DreamlandCommandState,
) -> CommandResult | None:
    result = _handle_builtin_command_if_deterministic(state)
    if result is not None:
        return result
    plan = plan_builtin_command_list(
        _session_from_command_state(state), state.get("current_command", "")
    )
    return aggregate_builtin_plan(plan) if plan is not None else None


def _format_command_context(context: CommandContext) -> str:
    active_frame = _active_shell_frame_for_context(context)
    login_username = context.login_username or context.username
    projected_world_state = project_world_state_for_command_emulator(
        context.world_state,
        detailed_host_refs=(context.active_host or context.hostname,),
    )
    lines = [
        f"username: {active_frame.username}",
        f"login username: {login_username}",
        f"host identity hostname: {context.host_identity.hostname}",
        f"hostname: {context.hostname}",
        f"cwd: {active_frame.cwd}",
        f"shell stack depth: {max(len(context.shell_stack), 1)}",
        "interactive program:",
        f"program name: {active_frame.interactive_program.name}",
        f"program command: {active_frame.interactive_program.command}",
        f"program argv: {_format_program_argv(active_frame.interactive_program.argv)}",
        "program prompt: "
        + json.dumps(active_frame.interactive_program.prompt_template),
        "program metadata: "
        + json.dumps(active_frame.interactive_program.metadata, sort_keys=True),
        "",
        "shell environment:",
        _indent_block(_format_shell_environment(active_frame.shell_environment)),
        "",
        "environment metadata:",
        _indent_block(_format_environment_metadata(context.environment_metadata)),
        "",
        "world state:",
        _indent_block(format_world_state_for_prompt(projected_world_state)),
    ]
    lines.extend(["", f"current command: {context.command}"])
    return "\n".join(lines)


def _host_metadata_without_command_history(
    metadata: dict[str, object],
) -> dict[str, object]:
    clean = deepcopy(metadata)
    clean.pop("command_history", None)
    return clean


def _system_prompt_for_active_program(
    system_prompt: str,
    state: DreamlandCommandState,
) -> str:
    program = _interactive_program_from_state(state.get("interactive_program"))
    active_program_instruction = "\n".join(
        [
            "Active Program Instructions:",
            (
                "The authoritative active program is "
                f"{json.dumps(program.name)} ({json.dumps(program.command)})."
            ),
            "Interpret the current input exclusively as input to this program.",
            (
                "Ordinary input and completed evaluations do not end the program. "
                "Keep it active with no stack transition."
            ),
            (
                "Return a pop transition only when processing the current input "
                "causes the active program itself to exit."
            ),
            "Otherwise, do not pop the stack.",
        ]
    )
    return f"{system_prompt}\n\n{active_program_instruction}"


def _state_input_from_context(
    context: CommandContext,
    *,
    include_session_state: bool,
) -> DreamlandCommandState:
    # Network connections belong to runtime session state, not the model's
    # checkpoint. Refresh their host-local projection even on incremental calls.
    state: DreamlandCommandState = {
        "current_command": context.command,
        "environment_metadata": _environment_metadata_to_state(
            context.environment_metadata
        ),
    }
    if include_session_state:
        active_frame = _active_shell_frame_for_context(context)
        state.update(
            {
                "username": active_frame.username,
                "login_username": context.login_username or context.username,
                "cwd": active_frame.cwd,
                "hostname": context.hostname,
                "host_identity": _host_identity_to_state(context.host_identity),
                "langfuse_trace_id": context.langfuse_trace_id,
                "interactive_program": _interactive_program_to_state(
                    active_frame.interactive_program
                ),
                "shell_environment": _shell_environment_to_state(
                    active_frame.shell_environment
                ),
                "shell_stack": [
                    _shell_frame_to_state(frame)
                    for frame in _shell_stack_for_context(context)
                ],
                "world_state": _world_state_from_state(context.world_state),
                "active_host": context.active_host
                or active_host_ref(_world_state_from_state(context.world_state)),
                "active_host_metadata": (
                    _host_metadata_without_command_history(context.active_host_metadata)
                    if context.active_host_metadata
                    else active_host_metadata_for_context(
                        _world_state_from_state(context.world_state)
                    )
                ),
            }
        )
    return state


def _state_from_context_for_builtin(context: CommandContext) -> DreamlandCommandState:
    active_frame = _active_shell_frame_for_context(context)
    return {
        "username": active_frame.username,
        "login_username": context.login_username or context.username,
        "cwd": active_frame.cwd,
        "hostname": context.hostname,
        "host_identity": _host_identity_to_state(context.host_identity),
        "current_command": context.command,
        "langfuse_trace_id": context.langfuse_trace_id,
        "interactive_program": _interactive_program_to_state(
            active_frame.interactive_program
        ),
        "shell_environment": _shell_environment_to_state(
            active_frame.shell_environment
        ),
        "shell_stack": [
            _shell_frame_to_state(frame) for frame in _shell_stack_for_context(context)
        ],
        "world_state": _world_state_from_state(context.world_state),
        "active_host": context.active_host
        or active_host_ref(_world_state_from_state(context.world_state)),
        "active_host_metadata": (
            _host_metadata_without_command_history(context.active_host_metadata)
            if context.active_host_metadata
            else active_host_metadata_for_context(
                _world_state_from_state(context.world_state)
            )
        ),
    }


def _shell_environment_for_context(context: CommandContext) -> ShellEnvironment:
    if context.shell_stack:
        return context.shell_stack[-1].shell_environment
    default_environment = default_shell_environment()
    if context.shell_environment == default_environment:
        return default_shell_environment(
            username=context.username,
            hostname=context.hostname,
            cwd=context.cwd,
            shell=context.interactive_program.command,
        )
    return context.shell_environment


def _shell_stack_for_context(context: CommandContext) -> tuple[ShellFrame, ...]:
    if context.shell_stack:
        return tuple(context.shell_stack)
    return (
        default_shell_frame(
            username=context.username,
            hostname=context.hostname,
            cwd=context.cwd,
            interactive_program=context.interactive_program,
            shell_environment=_shell_environment_for_context(context),
        ),
    )


def _active_shell_frame_for_context(context: CommandContext) -> ShellFrame:
    return _shell_stack_for_context(context)[-1]


def _format_command_state(state: DreamlandCommandState) -> str:
    environment_metadata = state.get("environment_metadata")
    if environment_metadata is None and "connection_metadata" in state:
        environment_metadata = {"connection": state.get("connection_metadata")}
    return _format_command_context(
        CommandContext(
            username=state.get("username", "guest"),
            login_username=state.get("login_username"),
            cwd=state.get("cwd", "/home/guest"),
            hostname=state.get("hostname", "ubuntu"),
            host_identity=_host_identity_from_state(
                state.get("host_identity"),
                default_hostname=str(state.get("hostname", "ubuntu")),
            ),
            command=state.get("current_command", ""),
            langfuse_trace_id=state.get("langfuse_trace_id", ""),
            interactive_program=_interactive_program_from_state(
                state.get("interactive_program")
            ),
            shell_environment=_shell_environment_from_state(
                state.get("shell_environment")
            ),
            shell_stack=tuple(
                _shell_frame_from_state(entry) for entry in state.get("shell_stack", [])
            ),
            environment_metadata=_environment_metadata_from_state(environment_metadata),
            world_state=_world_state_from_state(state.get("world_state")),
            active_host=str(
                state.get("active_host")
                or active_host_ref(_world_state_from_state(state.get("world_state")))
            ),
            active_host_metadata=(
                _host_metadata_without_command_history(state["active_host_metadata"])
                if isinstance(state.get("active_host_metadata"), dict)
                else active_host_metadata_for_context(
                    _world_state_from_state(state.get("world_state"))
                )
            ),
        )
    )


def _history_entry_to_state(entry: CommandHistoryEntry) -> dict[str, object]:
    return {
        "id": entry.id,
        "cwd": entry.cwd,
        "command": entry.command,
        "stdout": entry.stdout,
        "stderr": entry.stderr,
        "exit_status": entry.exit_status,
        "interrupted": entry.interrupted,
        "signal": entry.signal,
    }


def _history_entry_from_state(entry: dict[str, object]) -> CommandHistoryEntry:
    return CommandHistoryEntry(
        id=str(entry["id"]) if entry.get("id") is not None else "",
        cwd=str(entry.get("cwd", "")),
        command=str(entry.get("command", "")),
        stdout=str(entry.get("stdout", "")),
        stderr=str(entry.get("stderr", "")),
        exit_status=_coerce_int(entry.get("exit_status", 0)),
        interrupted=bool(entry.get("interrupted", False)),
        signal=str(entry["signal"]) if entry.get("signal") is not None else None,
    )


def _format_shell_environment(environment: ShellEnvironment) -> str:
    if not environment.variables:
        return "variables: none"
    lines = ["variables:"]
    for key, value in sorted(environment.variables.items()):
        marker = " exported" if key in environment.exported else ""
        lines.append(f"  {key}{marker}: {value}")
    return "\n".join(lines)


def _format_environment_metadata(environment_metadata: EnvironmentMetadata) -> str:
    lines = [
        "connection:",
        _indent_block(_format_connection_metadata(environment_metadata.connection)),
    ]
    visible_metadata = {
        key: value
        for key, value in environment_metadata.metadata.items()
        if key != "dreamland.session.evaluation_config"
    }
    if visible_metadata:
        lines.append("metadata:")
        for key, value in sorted(visible_metadata.items()):
            if key == "active_network":
                lines.append(f"  {key}: {json.dumps(value, sort_keys=True)}")
            else:
                lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def _format_connection_metadata(connection_metadata: ConnectionMetadata | None) -> str:
    if connection_metadata is None:
        return "none"
    lines = [
        f"protocol: {_format_optional_value(connection_metadata.protocol)}",
        "source:",
        f"  ip: {_format_optional_value(connection_metadata.source.ip)}",
        f"  port: {_format_optional_value(connection_metadata.source.port)}",
        "destination:",
        f"  ip: {_format_optional_value(connection_metadata.destination.ip)}",
        f"  port: {_format_optional_value(connection_metadata.destination.port)}",
    ]
    if connection_metadata.metadata:
        lines.append("metadata:")
        for key, value in sorted(connection_metadata.metadata.items()):
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def _format_optional_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _world_state_from_state(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        if value:
            return deepcopy(value)
        return {}
    return {}


def _merged_world_state(
    current: dict[str, object],
    patch: dict[str, object],
) -> dict[str, object]:
    return merge_world_state(current, patch)


def _session_from_command_state(
    state: DreamlandCommandState | None,
) -> SessionState:
    stack = _base_shell_stack_for_structured_model_state(state)
    frame = stack[-1]
    return SessionState(
        username=frame.username,
        login_username=state.get("login_username") if state else None,
        cwd=frame.cwd,
        host_identity=frame.host_identity,
        interactive_program=frame.interactive_program,
        shell_environment=frame.shell_environment,
        shell_stack=list(stack),
        world_state=_world_state_from_state(
            state.get("world_state") if state else None
        ),
    )


def _command_state_updates_from_session(session: SessionState) -> DreamlandCommandState:
    return {
        "username": session.username,
        "cwd": session.cwd,
        "hostname": session.host_identity.hostname,
        "host_identity": _host_identity_to_state(session.host_identity),
        "interactive_program": _interactive_program_to_state(
            session.interactive_program
        ),
        "shell_environment": _shell_environment_to_state(session.shell_environment),
        "shell_stack": [_shell_frame_to_state(frame) for frame in session.shell_stack],
        "world_state": deepcopy(session.world_state),
    }


def _result_to_state(result: CommandResult) -> dict[str, object]:
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_status": result.exit_status,
        "interrupted": result.interrupted,
        "signal": result.signal,
        "metadata": dict(result.metadata),
        **_command_state_update_to_state(result.state_update),
    }


def _command_state_update_to_state(update: CommandStateUpdate) -> dict[str, object]:
    return {
        "host_identity": (
            _host_identity_to_state(update.host_identity)
            if update.host_identity is not None
            else None
        ),
        "cwd": update.cwd,
        "interactive_program": (
            _interactive_program_to_state(update.interactive_program)
            if update.interactive_program is not None
            else None
        ),
        "shell_environment": (
            _shell_environment_to_state(update.shell_environment)
            if update.shell_environment is not None
            else None
        ),
        "shell_stack": (
            [_shell_frame_to_state(frame) for frame in update.shell_stack]
            if update.shell_stack is not None
            else None
        ),
        "shell_stack_transition": (
            _shell_stack_transition_to_state(update.shell_stack_transition)
            if update.shell_stack_transition is not None
            else None
        ),
        "world_state": deepcopy(update.world_state),
        "effects": [
            {
                "command": effect.command,
                "state_update": _command_state_update_to_state(effect.state_update),
            }
            for effect in update.effects
        ],
    }


def _result_from_state(value: object) -> CommandResult | None:
    if not isinstance(value, dict):
        return None
    return CommandResult(
        stdout=str(value.get("stdout", "")),
        stderr=str(value.get("stderr", "")),
        exit_status=_coerce_int(value.get("exit_status", 0)),
        interrupted=bool(value.get("interrupted", False)),
        signal=str(value["signal"]) if value.get("signal") is not None else None,
        metadata=(
            dict(value["metadata"]) if isinstance(value.get("metadata"), dict) else {}
        ),
        state_update=_command_state_update_from_state(value),
    )


def _command_state_update_from_state(value: dict[str, object]) -> CommandStateUpdate:
    cwd = value.get("cwd")
    host_identity = value.get("host_identity")
    interactive_program = value.get("interactive_program")
    shell_environment = value.get("shell_environment")
    shell_stack = value.get("shell_stack")
    shell_stack_transition = value.get("shell_stack_transition")
    world_state = value.get("world_state")
    effects = value.get("effects")
    return CommandStateUpdate(
        host_identity=_optional_host_identity_from_state(host_identity),
        cwd=str(cwd) if cwd is not None else None,
        interactive_program=(
            _interactive_program_from_state(interactive_program)
            if interactive_program is not None
            else None
        ),
        shell_environment=(
            _shell_environment_from_state(shell_environment)
            if shell_environment is not None
            else None
        ),
        shell_stack=(
            tuple(_shell_frame_from_state(frame) for frame in shell_stack)
            if isinstance(shell_stack, (list, tuple))
            else None
        ),
        shell_stack_transition=_shell_stack_transition_from_state(
            shell_stack_transition
        ),
        world_state=deepcopy(world_state) if isinstance(world_state, dict) else None,
        effects=tuple(
            CommandStateEffect(
                command=str(effect["command"]),
                state_update=_command_state_update_from_state(effect["state_update"]),
            )
            for effect in (effects if isinstance(effects, (list, tuple)) else ())
        ),
    )


def _result_from_graph_update(update: object) -> CommandResult | None:
    if not isinstance(update, dict):
        return None
    for value in update.values():
        if not isinstance(value, dict) or "result" not in value:
            continue
        result = _result_from_state(value.get("result"))
        if result is not None:
            return result
    return None


def _streamed_response_looks_structured(text: str) -> bool:
    if text.startswith("{"):
        return True
    fence = "```"
    return fence.startswith(text) or text.startswith(fence)


def _result_from_structured_model_payload(
    payload: dict[str, object],
    state: DreamlandCommandState | None = None,
) -> CommandResult | None:
    required_keys = {"stdout", "stderr", "exit_status", "state_update"}
    if not required_keys.issubset(payload):
        return None
    metadata: dict[str, object] = {}
    if not isinstance(payload.get("state_update"), dict):
        metadata["structured_parse_error"] = "state_update must be an object"
    state_update = _state_update_from_structured_model_payload(
        payload.get("state_update"),
        state,
        metadata,
    )
    return CommandResult(
        stdout=_coerce_output(payload.get("stdout")),
        stderr=_coerce_output(payload.get("stderr")),
        exit_status=_optional_int(payload.get("exit_status")) or 0,
        metadata=metadata,
        state_update=state_update,
    )


def _unparsed_structured_model_response(text: str, error: str) -> CommandResult:
    return CommandResult(
        stdout=_ensure_trailing_newline(text),
        metadata={
            "structured_parse_error": error,
            "structured_response_accepted": False,
            STRUCTURED_FAILURE_CLASS_METADATA: classify_structured_failure(error),
            STRUCTURED_RAW_RESPONSE_METADATA: text,
        },
    )


def _state_update_from_structured_model_payload(
    value: object,
    state: DreamlandCommandState | None = None,
    metadata: dict[str, object] | None = None,
) -> CommandStateUpdate:
    if not isinstance(value, dict):
        return CommandStateUpdate()
    if "effects" in value:
        return _state_update_from_model_effects(value, state, metadata)
    normalized = dict(value)
    transition_value = normalized.get("shell_stack_transition")
    transition = _normalized_shell_stack_transition(
        transition_value,
        normalized,
        metadata,
    )
    active_program = _interactive_program_from_state(
        state.get("interactive_program") if state is not None else None
    )
    next_program = (
        _interactive_program_from_state(normalized["interactive_program"])
        if normalized.get("interactive_program") is not None
        else None
    )
    if (
        transition is not None
        and transition.operation == "push"
        and active_program.name in {"ssh", "sudo", "ssh-keygen", "passwd"}
        and normalized.get("shell_stack") is None
    ):
        # Input to these dialogues cannot launch a nested copy. Authentication
        # may replace the client with a shell, but still owns the same frame.
        transition = replace(transition, operation="replace_active")
        if metadata is not None:
            metadata["dreamland.transition.normalized"] = (
                "active_dialogue_replace_active"
            )
    if (
        transition is None
        and transition_value is None
        and normalized.get("interactive_program") is not None
        and normalized.get("shell_stack") is None
    ):
        continues = next_program is not None and _continues_active_program(
            active_program, next_program
        )
        transition = ShellStackTransition(
            operation="replace_active" if continues else "push"
        )
        if metadata is not None:
            metadata["dreamland.transition.normalized"] = (
                "active_ssh_program_replace_active"
                if continues and active_program.name == "ssh"
                else "active_program_replace_active"
                if continues
                else "implicit_program_push"
            )
    return CommandStateUpdate(
        host_identity=_optional_host_identity_from_state(
            normalized.get("host_identity")
        ),
        cwd=_optional_string(normalized.get("cwd")),
        interactive_program=(
            _interactive_program_from_state(normalized.get("interactive_program"))
            if normalized.get("interactive_program") is not None
            else None
        ),
        shell_environment=(
            _shell_environment_from_state(normalized.get("shell_environment"))
            if normalized.get("shell_environment") is not None
            else None
        ),
        shell_stack=(
            _shell_stack_from_structured_model_state(normalized["shell_stack"], state)
            if isinstance(normalized.get("shell_stack"), list)
            else None
        ),
        shell_stack_transition=transition,
        world_state=(
            deepcopy(normalized["world_state"])
            if isinstance(normalized.get("world_state"), dict)
            else None
        ),
    )


def _state_update_from_model_effects(
    value: dict[str, object],
    state: DreamlandCommandState | None,
    metadata: dict[str, object] | None,
) -> CommandStateUpdate:
    """Validate the complete sequence before resolving its ordered frame changes."""
    try:
        if set(value) != {"effects"}:
            raise ValueError("effects cannot accompany ordinary state fields")
        raw_effects = value["effects"]
        if (
            not isinstance(raw_effects, list)
            or not 1 <= len(raw_effects) <= MAX_COMMAND_STATE_EFFECTS
        ):
            raise ValueError("effects must contain between 1 and 64 entries")
        for effect in raw_effects:
            _validate_model_state_effect(effect)
    except ValueError as exc:
        if metadata is not None:
            metadata["structured_parse_error"] = f"invalid state_update.effects: {exc}"
            metadata["dreamland.state_effects.accepted"] = False
            metadata["dreamland.transition.accepted"] = False
            metadata["dreamland.transition.rejection_reason"] = str(exc)
        return CommandStateUpdate()

    # This detached state resolves later implicit transitions against earlier
    # effects. It never allocates runtime sockets or publishes lifecycle events.
    session = _session_from_command_state(state)
    next_state = dict(state or {})
    effects: list[CommandStateEffect] = []
    operations: list[str] = []
    normalizations: list[str] = []
    for index, effect in enumerate(raw_effects):
        effect_metadata: dict[str, object] = {}
        update = _state_update_from_structured_model_payload(
            effect["state_update"],
            cast(DreamlandCommandState, next_state),
            effect_metadata,
        )
        effects.append(
            CommandStateEffect(command=effect["command"], state_update=update)
        )
        if (
            update.shell_stack_transition is not None
            and update.shell_stack_transition.operation == "pop"
            and len(session.shell_stack) <= 1
            and index != len(raw_effects) - 1
        ):
            if metadata is not None:
                metadata["structured_parse_error"] = (
                    "invalid state_update.effects: effects cannot follow an outermost pop"
                )
                metadata["dreamland.state_effects.accepted"] = False
                metadata["dreamland.transition.accepted"] = False
                metadata["dreamland.transition.rejection_reason"] = (
                    "effects cannot follow an outermost pop"
                )
            return CommandStateUpdate()
        apply_command_state_update(session, update, reconcile_network=False)
        next_state.update(_command_state_updates_from_session(session))
        if update.shell_stack_transition is not None:
            operations.append(update.shell_stack_transition.operation)
        normalization = effect_metadata.get("dreamland.transition.normalized")
        if normalization:
            normalizations.append(f"effects[{index}]:{normalization}")
    if metadata is not None:
        metadata["dreamland.state_effects.accepted"] = True
        metadata["dreamland.state_effects.count"] = len(effects)
        metadata["dreamland.transition.requested_operation"] = (
            ",".join(operations) or "none"
        )
        if normalizations:
            metadata["dreamland.transition.normalized"] = ";".join(normalizations)
    return CommandStateUpdate(effects=tuple(effects))


def _validate_model_state_effect(effect: object) -> None:
    if not isinstance(effect, dict) or set(effect) != {"command", "state_update"}:
        raise ValueError("each effect must contain command and state_update only")
    command = effect["command"]
    if not isinstance(command, str) or not command.strip():
        raise ValueError("effect command must be a nonempty string")
    update = effect["state_update"]
    allowed = {
        "cwd",
        "host_identity",
        "interactive_program",
        "shell_environment",
        "shell_stack_transition",
        "world_state",
    }
    if not isinstance(update, dict) or set(update) - allowed:
        raise ValueError(
            "effect state_update must use ordinary fields and transitions; no nested effects or shell_stack"
        )
    if "cwd" in update and (not isinstance(update["cwd"], str) or not update["cwd"]):
        raise ValueError("effect cwd must be a nonempty string")
    if "host_identity" in update:
        identity = update["host_identity"]
        if (
            not isinstance(identity, dict)
            or set(identity) != {"hostname"}
            or not isinstance(identity["hostname"], str)
            or not identity["hostname"]
        ):
            raise ValueError("effect host_identity must contain a hostname string")
    if "interactive_program" in update:
        program = update["interactive_program"]
        if (
            not isinstance(program, dict)
            or set(program) - {"name", "command", "argv", "prompt_template", "metadata"}
            or any(
                not isinstance(program.get(key), str) or not program[key]
                for key in ("name", "command")
            )
            or (
                "argv" in program
                and (
                    not isinstance(program["argv"], list)
                    or any(not isinstance(arg, str) for arg in program["argv"])
                )
            )
            or (
                program.get("prompt_template") is not None
                and not isinstance(program["prompt_template"], str)
            )
            or ("metadata" in program and not isinstance(program["metadata"], dict))
        ):
            raise ValueError("invalid effect interactive_program")
    if "shell_environment" in update:
        environment = update["shell_environment"]
        if (
            not isinstance(environment, dict)
            or set(environment) != {"variables", "exported"}
            or not isinstance(environment["variables"], dict)
            or any(
                not isinstance(key, str) or not isinstance(val, str)
                for key, val in environment["variables"].items()
            )
            or not isinstance(environment["exported"], list)
            or any(not isinstance(name, str) for name in environment["exported"])
        ):
            raise ValueError("invalid effect shell_environment")
    if "shell_stack_transition" in update:
        transition = update["shell_stack_transition"]
        if (
            not isinstance(transition, dict)
            or set(transition) - {"operation", "username", "metadata"}
            or transition.get("operation") not in ("push", "pop", "replace_active")
            or (
                "username" in transition
                and (
                    not isinstance(transition["username"], str)
                    or not transition["username"]
                )
            )
            or (
                "metadata" in transition
                and not isinstance(transition["metadata"], dict)
            )
            or (
                isinstance(transition.get("metadata"), dict)
                and set(transition["metadata"])
                & {
                    "name",
                    "command",
                    "argv",
                    "prompt_template",
                    "cwd",
                    "shell_environment",
                    "interactive_program",
                }
            )
        ):
            raise ValueError("invalid effect shell_stack_transition")
    if "world_state" in update and not isinstance(update["world_state"], dict):
        raise ValueError("effect world_state must be an object")


def _normalized_shell_stack_transition(
    value: object,
    state_update: dict[str, object],
    result_metadata: dict[str, object] | None,
) -> ShellStackTransition | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        _reject_transition(result_metadata, "shell_stack_transition must be an object")
        return None

    operation = value.get("operation")
    if operation not in {"push", "pop", "replace_active"}:
        _reject_transition(result_metadata, "invalid shell stack transition operation")
        return None
    if result_metadata is not None:
        result_metadata["dreamland.transition.requested_operation"] = operation

    transition_state = dict(value)
    transition_metadata = (
        dict(transition_state["metadata"])
        if isinstance(transition_state.get("metadata"), dict)
        else {}
    )
    normalized_fields: list[str] = []
    for field_name in ("cwd", "interactive_program", "shell_environment"):
        field_value = transition_state.get(field_name)
        if field_value is None:
            field_value = transition_metadata.pop(field_name, None)
        if state_update.get(field_name) is None and field_value is not None:
            state_update[field_name] = field_value
            normalized_fields.append(field_name)

    if normalized_fields and result_metadata is not None:
        result_metadata["dreamland.transition.normalized"] = ",".join(normalized_fields)
    return ShellStackTransition(
        operation=cast(Literal["push", "pop", "replace_active"], operation),
        username=_optional_string(transition_state.get("username")),
        metadata=transition_metadata,
    )


def _active_program_name(state: DreamlandCommandState | None) -> str | None:
    if state is None:
        return None
    return _interactive_program_from_state(state.get("interactive_program")).name


def _continues_active_program(
    active: InteractiveProgram, updated: InteractiveProgram
) -> bool:
    # Authentication owns the client frame even when it becomes a login shell.
    if active.name in {"ssh", "sudo"}:
        return True
    if active.name in {"ssh-keygen", "passwd"} and active.name == updated.name:
        return True
    # Shells can deliberately nest another instance of themselves. Other
    # programs updating phase/prompt metadata continue in their existing frame;
    # genuine nested invocation remains representable by an explicit push.
    return (
        (
            not is_command_shell(active)
            or has_pending_shell_input(active)
            or has_pending_shell_input(updated)
        )
        and active.name == updated.name
        and active.command == updated.command
        and active.argv == updated.argv
    )


def _reject_transition(
    metadata: dict[str, object] | None,
    reason: str,
) -> None:
    if metadata is None:
        return
    metadata["dreamland.transition.accepted"] = False
    metadata["dreamland.transition.rejection_reason"] = reason


def _shell_stack_transition_from_state(value: object) -> ShellStackTransition | None:
    if not isinstance(value, dict):
        return None
    operation = value.get("operation")
    if operation not in {"push", "pop", "replace_active"}:
        return None
    return ShellStackTransition(
        operation=cast(Literal["push", "pop", "replace_active"], operation),
        username=_optional_string(value.get("username")),
        metadata=(
            dict(value["metadata"]) if isinstance(value.get("metadata"), dict) else {}
        ),
    )


def _shell_stack_transition_to_state(
    transition: ShellStackTransition,
) -> dict[str, object]:
    state: dict[str, object] = {"operation": transition.operation}
    if transition.username is not None:
        state["username"] = transition.username
    if transition.metadata:
        state["metadata"] = dict(transition.metadata)
    return state


def _result_with_transition_audit(
    result: CommandResult,
    *,
    starting_stack: tuple[ShellFrame, ...],
    resulting_stack: tuple[ShellFrame, ...],
) -> CommandResult:
    metadata = dict(result.metadata)
    transition = result.state_update.shell_stack_transition
    metadata.setdefault(
        "dreamland.transition.requested_operation",
        transition.operation if transition is not None else "none",
    )
    metadata.setdefault("dreamland.transition.accepted", True)
    metadata["dreamland.transition.applied_operation"] = _applied_stack_operation(
        starting_stack,
        resulting_stack,
    )
    metadata["dreamland.transition.resulting_stack_depth"] = len(resulting_stack)
    metadata["dreamland.transition.resulting_active_program"] = (
        resulting_stack[-1].interactive_program.name if resulting_stack else ""
    )
    return replace(result, metadata=metadata)


def _applied_stack_operation(
    starting_stack: tuple[ShellFrame, ...],
    resulting_stack: tuple[ShellFrame, ...],
) -> str:
    if len(resulting_stack) > len(starting_stack):
        return "push"
    if len(resulting_stack) < len(starting_stack):
        return "pop"
    if resulting_stack != starting_stack:
        return "replace_active"
    return "none"


def _shell_stack_from_structured_model_state(
    value: object,
    state: DreamlandCommandState | None,
) -> tuple[ShellFrame, ...]:
    base_stack = _base_shell_stack_for_structured_model_state(state)
    if not isinstance(value, list) or not value:
        return base_stack
    hostname = _hostname_from_state(state)
    login_username = (
        str(state.get("login_username") or state.get("username") or "guest")
        if state is not None
        else "guest"
    )
    frames: list[ShellFrame] = []
    for index, item in enumerate(value):
        default_frame = base_stack[index] if index < len(base_stack) else base_stack[-1]
        frames.append(
            _shell_frame_from_structured_model_state(
                item,
                default_frame=default_frame,
                hostname=hostname,
                login_username=login_username,
            )
        )
    if len(frames) == len(base_stack) - 1 and all(
        _same_context_frame(frame, base_frame)
        for frame, base_frame in zip(frames, base_stack, strict=False)
    ):
        return base_stack[:-1]
    if len(frames) == len(base_stack) + 1 and all(
        _same_context_frame(frame, base_frame)
        for frame, base_frame in zip(frames[:-1], base_stack, strict=True)
    ):
        return (*base_stack, frames[-1])
    if (
        len(base_stack) == 1
        and len(frames) == 1
        and _shell_frame_replaces_base_frame(frames[0], base_stack[0])
        and not _continues_active_program(
            base_stack[0].interactive_program, frames[0].interactive_program
        )
    ):
        return (*base_stack, frames[0])
    if (
        len(frames) == len(base_stack)
        and len(frames) > 1
        and _matching_context_prefix(frames[:-1], base_stack[:-1])
        and _program_transition_replaces_active_frame(frames[-1], base_stack[-1])
    ):
        return (*base_stack, frames[-1])
    if len(frames) == len(base_stack) and len(frames) > 1:
        return (*base_stack[:-1], frames[-1])
    return tuple(frames)


def _hostname_from_state(state: DreamlandCommandState | None) -> str:
    if state is None:
        return "ubuntu"
    host_identity = _host_identity_from_state(
        state.get("host_identity"),
        default_hostname=str(state.get("hostname", "ubuntu")),
    )
    return host_identity.hostname


def _shell_frame_replaces_base_frame(frame: ShellFrame, base_frame: ShellFrame) -> bool:
    return (
        frame.username != base_frame.username
        or frame.host_identity != base_frame.host_identity
        or frame.cwd != base_frame.cwd
        or frame.interactive_program != base_frame.interactive_program
    )


def _program_transition_replaces_active_frame(
    frame: ShellFrame,
    base_frame: ShellFrame,
) -> bool:
    return (
        frame.username == base_frame.username
        and frame.host_identity == base_frame.host_identity
        and frame.cwd == base_frame.cwd
        and frame.interactive_program != base_frame.interactive_program
        and not _continues_active_program(
            base_frame.interactive_program, frame.interactive_program
        )
    )


def _same_context_frame(frame: ShellFrame, base_frame: ShellFrame) -> bool:
    return (
        frame.username == base_frame.username
        and frame.host_identity == base_frame.host_identity
        and frame.cwd == base_frame.cwd
        and frame.interactive_program.command == base_frame.interactive_program.command
    )


def _matching_context_prefix(
    frames: list[ShellFrame] | tuple[ShellFrame, ...],
    base_frames: tuple[ShellFrame, ...],
) -> bool:
    return all(
        _same_context_frame(frame, base_frame)
        for frame, base_frame in zip(frames, base_frames, strict=True)
    )


def _base_shell_stack_for_structured_model_state(
    state: DreamlandCommandState | None,
) -> tuple[ShellFrame, ...]:
    if state is None:
        return (default_shell_frame(),)
    interactive_program = _interactive_program_from_state(
        state.get("interactive_program")
    )
    shell_environment = _shell_environment_from_state(state.get("shell_environment"))
    return _shell_stack_from_state(
        state.get("shell_stack"),
        username=str(state.get("username", "guest")),
        hostname=_hostname_from_state(state),
        cwd=str(state.get("cwd", "/home/guest")),
        interactive_program=interactive_program,
        shell_environment=shell_environment,
    )


def _shell_frame_from_structured_model_state(
    value: object,
    *,
    default_frame: ShellFrame,
    hostname: str,
    login_username: str,
) -> ShellFrame:
    if not isinstance(value, dict):
        return default_frame
    cwd = str(value.get("cwd") or default_frame.cwd)
    program = (
        _interactive_program_from_state(value.get("interactive_program"))
        if value.get("interactive_program") is not None
        else default_frame.interactive_program
    )
    username = _structured_frame_username(
        value.get("username"),
        cwd=cwd,
        default_username=default_frame.username,
        login_username=login_username,
    )
    environment = _structured_frame_environment(
        value.get("shell_environment"),
        username=username,
        hostname=hostname,
        cwd=cwd,
        program=program,
        default_environment=default_frame.shell_environment,
    )
    metadata = value.get("metadata", {})
    host_identity = _host_identity_from_state(
        value.get("host_identity"),
        default_hostname=environment.variables.get("HOSTNAME", hostname),
    )
    return ShellFrame(
        username=username,
        cwd=cwd,
        interactive_program=program,
        shell_environment=environment,
        host_identity=host_identity,
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


def _structured_frame_username(
    value: object,
    *,
    cwd: str,
    default_username: str,
    login_username: str,
) -> str:
    username = str(value) if value not in {None, ""} else default_username
    if username != "guest" or login_username == "guest":
        return username
    if cwd == "/root" or cwd.startswith("/root/"):
        return "root"
    if cwd == f"/home/{login_username}" or cwd.startswith(f"/home/{login_username}/"):
        return login_username
    return username


def _structured_frame_environment(
    value: object,
    *,
    username: str,
    hostname: str,
    cwd: str,
    program: InteractiveProgram,
    default_environment: ShellEnvironment,
) -> ShellEnvironment:
    environment = (
        _shell_environment_from_state(value)
        if isinstance(value, dict)
        else default_environment
    )
    variables = dict(environment.variables)
    if not variables:
        environment = default_shell_environment(
            username=username,
            hostname=hostname,
            cwd=cwd,
            shell=program.command,
        )
        variables = dict(environment.variables)
    variables["USER"] = username
    variables["LOGNAME"] = username
    if username == "root":
        variables["HOME"] = "/root"
    else:
        variables.setdefault("HOME", f"/home/{username}")
    variables.setdefault("SHELL", program.command)
    variables["PWD"] = cwd
    exported = set(environment.exported)
    exported.update({"HOME", "LOGNAME", "PWD", "SHELL", "USER"})
    return ShellEnvironment(variables=variables, exported=frozenset(exported))


def _coerce_output(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _record_interrupted_graph_result(
    graph: Any,
    config: dict[str, Any] | None,
    context: CommandContext,
    result: CommandResult,
) -> None:
    update_state = getattr(graph, "update_state", None)
    if update_state is None:
        return
    state = _graph_state_values(graph, config)
    if state is None:
        state = _state_input_from_context(context, include_session_state=True)
    world_state = append_host_command_history(
        _world_state_from_state(state.get("world_state")),
        str(state.get("hostname", context.hostname)),
        CommandHistoryEntry(
            cwd=str(state.get("cwd", context.cwd)),
            command=str(state.get("current_command", context.command)),
            stdout=result.stdout,
            stderr=result.stderr,
            exit_status=result.exit_status,
            interrupted=result.interrupted,
            signal=result.signal,
        ),
    )
    try:
        update_state(
            config,
            {
                "world_state": world_state,
                "active_host": active_host_ref(world_state),
                "active_host_metadata": active_host_metadata_for_context(world_state),
                "response": result.stdout,
                "result": _result_to_state(result),
            },
        )
    except Exception:
        LOGGER.debug("Failed to record interrupted graph result", exc_info=True)
        return


def _record_interrupted_graph_result_once(
    graph: Any,
    config: dict[str, Any] | None,
    context: CommandContext,
    control: CommandExecutionControl,
) -> None:
    if not control.claim_graph_recording():
        return
    _record_interrupted_graph_result(
        graph,
        config,
        context,
        control.rendered_result(),
    )


def _graph_state_values(
    graph: Any,
    config: dict[str, Any] | None,
) -> DreamlandCommandState | None:
    get_state = getattr(graph, "get_state", None)
    if get_state is None or config is None:
        return None
    try:
        snapshot = get_state(config)
    except Exception:
        LOGGER.debug("Failed to read graph checkpoint state", exc_info=True)
        return None
    values = getattr(snapshot, "values", None)
    if not isinstance(values, dict):
        return None
    return cast(DreamlandCommandState, values)


def _graph_has_checkpoint_state(graph: Any, config: dict[str, Any]) -> bool:
    get_state = getattr(graph, "get_state", None)
    if get_state is None:
        return False
    try:
        snapshot = get_state(config)
    except Exception:
        LOGGER.debug("Failed to inspect graph checkpoint state", exc_info=True)
        return False
    return bool(getattr(snapshot, "values", None))


def _format_program_argv(argv: tuple[str, ...]) -> str:
    if not argv:
        return ""
    return " ".join(argv)


def _indent_block(text: str) -> str:
    return "\n".join(f"  {line}" if line else "" for line in text.splitlines())


def _message_content_to_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content)


def _system_prompt_content_for_model(
    content: str,
    *,
    provider: str,
    prompt_cache: str | None,
) -> str | list[dict[str, object]]:
    if provider != "anthropic" or prompt_cache == "off":
        return content
    return [
        {
            "type": "text",
            "text": content,
            "cache_control": dict(_ANTHROPIC_EPHEMERAL_CACHE_CONTROL),
        }
    ]


def _command_system_prompt_and_invoke_kwargs(
    content: str,
    *,
    provider: str,
    model: str,
    prompt_cache: str | None,
) -> tuple[str | list[dict[str, object]], dict[str, object]]:
    invoke_kwargs: dict[str, object] = {}
    if provider.lower() == "openai":
        invoke_kwargs["response_format"] = {"type": "json_object"}

    if not _uses_openai_explicit_prompt_cache(provider=provider, model=model):
        return (
            _system_prompt_content_for_model(
                content,
                provider=provider,
                prompt_cache=prompt_cache,
            ),
            invoke_kwargs,
        )

    cache_mode = {"mode": _OPENAI_EXPLICIT_CACHE_MODE}
    return (
        [
            {
                "type": "text",
                "text": content,
                "prompt_cache_breakpoint": dict(cache_mode),
            }
        ],
        {
            **invoke_kwargs,
            "prompt_cache_key": _openai_command_prompt_cache_key(
                provider=provider,
                model=model,
                system_prompt=content,
            ),
            "prompt_cache_options": dict(cache_mode),
        },
    )


def _uses_openai_explicit_prompt_cache(*, provider: str, model: str) -> bool:
    return provider.lower() == "openai" and supports_openai_explicit_cache(model)


def _openai_command_prompt_cache_key(
    *,
    provider: str,
    model: str,
    system_prompt: str,
) -> str:
    key_material = json.dumps(
        [
            _OPENAI_COMMAND_CACHE_KEY_NAMESPACE,
            provider,
            model,
            system_prompt,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(key_material).hexdigest()[:32]
    return f"{_OPENAI_COMMAND_CACHE_KEY_NAMESPACE}:{digest}"


def _reasoning_summary_from_message(message: Any) -> str | None:
    summaries = _reasoning_summaries_from_value(getattr(message, "content", message))
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict):
        summaries.extend(_reasoning_summaries_from_value(additional_kwargs))
    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        summaries.extend(_reasoning_summaries_from_value(response_metadata))
    cleaned = [summary.strip() for summary in summaries if summary.strip()]
    if not cleaned:
        return None
    return "\n\n".join(cleaned)


def _reasoning_summaries_from_value(value: Any) -> list[str]:
    summaries: list[str] = []
    _collect_reasoning_summaries(value, summaries)
    return summaries


def _collect_reasoning_summaries(value: Any, summaries: list[str]) -> None:
    if isinstance(value, dict):
        if value.get("type") == "reasoning":
            summaries.extend(_summary_texts(value.get("summary")))
        if value.get("type") == "thinking":
            thinking = value.get("thinking")
            if isinstance(thinking, str):
                summaries.append(thinking)
        reasoning_content = value.get("reasoning_content")
        if isinstance(reasoning_content, str):
            summaries.append(reasoning_content)
        for key in ("content", "output", "message"):
            if key in value:
                _collect_reasoning_summaries(value[key], summaries)
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


def _ensure_trailing_newline(text: str) -> str:
    if not text:
        return ""
    if text.endswith("\n"):
        return text
    return f"{text}\n"
