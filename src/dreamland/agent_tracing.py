from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from dreamland.emulator_context import (
    detailed_host_registry_refs_for_command_emulator,
    project_world_state_for_command_emulator,
)
from dreamland.lobby_identity import public_trace_metadata
from dreamland.models import CommandContext, ConnectionMetadata, SessionState
from dreamland.session_metadata import (
    SESSION_LABEL_METADATA_KEY,
    SESSION_METADATA_METADATA_KEY,
    SESSION_ORIGIN_METADATA_KEY,
    SESSION_TAGS_METADATA_KEY,
    langfuse_tags_for_session,
    session_label_from_metadata,
    session_origin_from_metadata,
    session_tags_from_metadata,
    state_profile_name_from_metadata,
)
from dreamland.world_state import active_host_ref, format_world_state_for_prompt

TOKEN_LOGGER = logging.getLogger("dreamland.tokens")
LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")
FAILURE_LOGGER = logging.getLogger("dreamland.failures")
SESSION_START_OBSERVATION_NAME = "dreamland-session-start"


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_cache_creation: int | None = None
    input_cache_read: int | None = None


class TokenUsageLoggingCallback(BaseCallbackHandler):
    def __init__(
        self,
        model_name: str,
        *,
        command: str,
        max_output_tokens: int | None,
    ) -> None:
        self.model_name = model_name
        self.command = command
        self.max_output_tokens = max_output_tokens
        self._interrupted = threading.Event()
        self._usage_logged = threading.Event()
        self._unavailable_logged = threading.Event()

    def mark_dreamland_interrupted(self) -> None:
        self._interrupted.set()
        if not self._usage_logged.is_set() and not self._unavailable_logged.is_set():
            self._unavailable_logged.set()
            TOKEN_LOGGER.warning(
                "model=%s command=%r interrupted=true token_usage=unavailable "
                "reason=provider_usage_not_received_before_interrupt",
                self.model_name,
                self.command,
            )

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        finish_reason = _finish_reason(response)
        usage = _token_usage(response)
        if usage is None:
            if self._unavailable_logged.is_set():
                return
            self._unavailable_logged.set()
            TOKEN_LOGGER.warning(
                "model=%s command=%r interrupted=%s token_usage=unavailable",
                self.model_name,
                self.command,
                _bool_text(self._interrupted.is_set()),
            )
            if _output_cap_hit(
                finish_reason=finish_reason,
                output_tokens=None,
                max_output_tokens=self.max_output_tokens,
            ):
                self._log_output_cap_hit(
                    output_tokens=None,
                    finish_reason=finish_reason,
                )
            return
        self._usage_logged.set()
        TOKEN_LOGGER.info(
            "model=%s command=%r interrupted=%s input_tokens=%s "
            "output_tokens=%s total_tokens=%s input_cache_creation=%s "
            "input_cache_read=%s service_tier=%s",
            self.model_name,
            self.command,
            _bool_text(self._interrupted.is_set()),
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
            usage.input_cache_creation if usage.input_cache_creation is not None else 0,
            usage.input_cache_read if usage.input_cache_read is not None else 0,
            _reported_service_tier(response) or "unknown",
        )
        if _output_cap_hit(
            finish_reason=finish_reason,
            output_tokens=usage.output_tokens,
            max_output_tokens=self.max_output_tokens,
        ):
            self._log_output_cap_hit(
                output_tokens=usage.output_tokens,
                finish_reason=finish_reason,
            )

    def _log_output_cap_hit(
        self,
        *,
        output_tokens: int | None,
        finish_reason: str | None,
    ) -> None:
        TOKEN_LOGGER.warning(
            "model=%s command=%r output_token_cap_hit=true "
            "max_output_tokens=%s output_tokens=%s finish_reason=%s",
            self.model_name,
            self.command,
            self.max_output_tokens,
            output_tokens if output_tokens is not None else "unknown",
            finish_reason or "unknown",
        )


def build_langfuse_callbacks(
    *,
    enabled: bool,
    public_key: str | None,
    secret_key: str | None,
    host: str | None,
    trace_id: str | None = None,
) -> tuple[Any, ...]:
    if not enabled:
        return ()
    initialize_langfuse_client(
        enabled=enabled,
        public_key=public_key,
        secret_key=secret_key,
        host=host,
    )
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError as exc:  # pragma: no cover - optional runtime dependency.
        raise RuntimeError(
            "Langfuse tracing requires langfuse and langchain. "
            "Install the project dependencies first."
        ) from exc
    trace_context = {"trace_id": trace_id} if trace_id else None
    return (
        create_langfuse_callback_handler(
            base_handler=CallbackHandler,
            public_key=public_key,
            trace_context=trace_context,
        ),
    )


def _record_langfuse_session_lifecycle(
    *,
    enabled: bool,
    public_key: str | None,
    secret_key: str | None,
    host: str | None,
    session: SessionState,
    event: str,
    reason: str | None = None,
) -> None:
    """Create a lightweight Langfuse event so new sessions are queryable early."""
    if not enabled:
        return
    try:
        client = initialize_langfuse_client(
            enabled=enabled,
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        if client is None:
            return
        from langfuse import propagate_attributes

        metadata = session_start_trace_metadata(session)
        if event == "end":
            metadata["dreamland.session.lifecycle"] = event
        if reason is not None:
            metadata["dreamland.session.end_reason"] = reason
        with propagate_attributes(
            user_id=session.username,
            session_id=session.langfuse_trace_id,
            trace_name=(
                f"dreamland:{session.username}@{session.host_identity.hostname}"
            ),
            tags=langfuse_tags_for_session(
                metadata=session.environment_metadata.metadata,
            ),
            metadata=metadata,
        ):
            client.create_event(
                trace_context={"trace_id": session.langfuse_trace_id},
                name=(
                    SESSION_START_OBSERVATION_NAME
                    if event == "start"
                    else "dreamland-session-end"
                ),
                metadata=metadata,
            )
        client.flush()
        LIFECYCLE_LOGGER.info(
            "Langfuse session %s recorded session=%s user=%s profile=%s public_id=%s",
            event,
            session.langfuse_trace_id,
            session.username,
            state_profile_name_from_metadata(session.environment_metadata.metadata),
            public_trace_metadata(session).get("dreamlandPublicId", ""),
        )
    except Exception as exc:
        FAILURE_LOGGER.warning(
            "Langfuse session %s failed session=%s error=%s",
            event,
            session.langfuse_trace_id,
            type(exc).__name__,
        )


def record_langfuse_session_start(
    *,
    enabled: bool,
    public_key: str | None,
    secret_key: str | None,
    host: str | None,
    session: SessionState,
) -> None:
    _record_langfuse_session_lifecycle(
        enabled=enabled,
        public_key=public_key,
        secret_key=secret_key,
        host=host,
        session=session,
        event="start",
    )


def record_langfuse_session_end(
    *,
    enabled: bool,
    public_key: str | None,
    secret_key: str | None,
    host: str | None,
    session: SessionState,
    reason: str,
) -> None:
    _record_langfuse_session_lifecycle(
        enabled=enabled,
        public_key=public_key,
        secret_key=secret_key,
        host=host,
        session=session,
        event="end",
        reason=reason,
    )


def session_start_trace_metadata(
    session: SessionState,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "dreamland.username": session.username,
        "dreamland.login_username": session.login_username or session.username,
        "dreamland.hostname": session.host_identity.hostname,
        "dreamland.cwd": session.cwd,
        "dreamland.interactive_program": session.interactive_program.name,
        "dreamland.shell_stack_depth": str(max(len(session.shell_stack), 1)),
        "dreamland.history_length": str(len(session.history)),
        "dreamland.connection.scope": "transport_ingress",
        "langfuse_session_id": session.langfuse_trace_id,
        "langfuse_user_id": session.username,
        "langfuse_trace_name": (
            f"dreamland:{session.username}@{session.host_identity.hostname}"
        ),
    }
    if session.environment_metadata.connection is not None:
        connection = session.environment_metadata.connection
        metadata.update(
            {
                "dreamland.connection.protocol": connection.protocol or "",
                "dreamland.connection.source_ip": connection.source.ip or "",
                "dreamland.connection.source_port": _format_optional_value(
                    connection.source.port
                ),
                "dreamland.connection.destination_ip": (
                    connection.destination.ip or ""
                ),
                "dreamland.connection.destination_port": _format_optional_value(
                    connection.destination.port
                ),
            }
        )
    metadata.update(
        _transport_connection_trace_metadata(session.environment_metadata.connection)
    )
    state_profile_name = session.environment_metadata.metadata.get("state_profile.name")
    state_profile_level = session.environment_metadata.metadata.get(
        "state_profile.level"
    )
    if state_profile_name is not None:
        metadata["dreamland.state_profile.name"] = str(state_profile_name)
    if state_profile_level is not None:
        metadata["dreamland.state_profile.level"] = str(state_profile_level)
    _add_session_metadata(metadata, session.environment_metadata.metadata)
    metadata.update(public_trace_metadata(session))
    return metadata


def create_langfuse_callback_handler(
    *,
    base_handler: Any,
    public_key: str | None,
    trace_context: dict[str, str] | None,
) -> Any:
    if not isinstance(base_handler, type):
        return base_handler(public_key=public_key, trace_context=trace_context)

    class DreamlandLangfuseCallbackHandler(base_handler):
        def __init__(self) -> None:
            super().__init__(public_key=public_key, trace_context=trace_context)
            self._dreamland_interrupted = threading.Event()

        def mark_dreamland_interrupted(self) -> None:
            self._dreamland_interrupted.set()

        def _get_error_level_and_status_message(
            self,
            error: BaseException,
        ) -> tuple[str, str]:
            if self._dreamland_interrupted.is_set() and is_interrupt_error(error):
                return "DEFAULT", "User interrupted command with Ctrl-C"
            return super()._get_error_level_and_status_message(error)

        def on_chain_end(
            self,
            outputs: dict[str, Any],
            *,
            run_id: Any,
            parent_run_id: Any = None,
            **kwargs: Any,
        ) -> Any:
            transition_metadata = transition_trace_metadata(outputs)
            span = getattr(self, "_runs", {}).get(run_id)
            if span is not None and transition_metadata:
                span.update(metadata=transition_metadata)
            return super().on_chain_end(
                outputs,
                run_id=run_id,
                parent_run_id=parent_run_id,
                **kwargs,
            )

    return DreamlandLangfuseCallbackHandler()


def is_interrupt_error(error: BaseException) -> bool:
    return isinstance(error, (GeneratorExit, asyncio.CancelledError))


def initialize_langfuse_client(
    *,
    enabled: bool,
    public_key: str | None,
    secret_key: str | None,
    host: str | None,
) -> Any | None:
    if not enabled:
        return None
    if not public_key or not secret_key:
        raise RuntimeError(
            "Langfuse tracing is enabled, but LANGFUSE_PUBLIC_KEY and "
            "LANGFUSE_SECRET_KEY are not configured."
        )

    try:
        from langfuse import Langfuse
    except ImportError as exc:  # pragma: no cover - optional runtime dependency.
        raise RuntimeError(
            "Langfuse tracing requires langfuse and langchain. "
            "Install the project dependencies first."
        ) from exc
    return Langfuse(public_key=public_key, secret_key=secret_key, host=host)


def graph_invoke_config(
    callbacks: tuple[Any, ...],
    context: CommandContext,
) -> dict[str, Any]:
    projection = _command_emulator_projection_metrics(context)
    config: dict[str, Any] = {
        "configurable": {"thread_id": context.langfuse_trace_id},
        "run_name": "dreamland-command-agent",
        "metadata": {
            "dreamland.username": context.username,
            "dreamland.hostname": context.hostname,
            "dreamland.cwd": context.cwd,
            "dreamland.command": context.command,
            "dreamland.command_id": context.command_id,
            "dreamland.interactive_program": context.interactive_program.name,
            "dreamland.shell_stack_depth": str(max(len(context.shell_stack), 1)),
            "dreamland.history_length": str(_host_command_history_length(context)),
            "dreamland.connection.scope": "simulated_host",
            "dreamland.context_projection.mode": "active-host-v1",
            "dreamland.context_projection.active_host": projection["active_host"],
            "dreamland.context_projection.detailed_hosts": projection["detailed_hosts"],
            "dreamland.context_projection.host_count": str(projection["host_count"]),
            "dreamland.context_projection.detailed_host_count": str(
                projection["detailed_host_count"]
            ),
            "dreamland.context_projection.compact_host_count": str(
                projection["compact_host_count"]
            ),
            "dreamland.context_projection.full_world_chars": str(
                projection["full_world_chars"]
            ),
            "dreamland.context_projection.projected_world_chars": str(
                projection["projected_world_chars"]
            ),
            "dreamland.context_projection.saved_world_chars": str(
                projection["saved_world_chars"]
            ),
            "langfuse_session_id": context.langfuse_trace_id,
            "langfuse_user_id": context.username,
            "langfuse_trace_name": (f"dreamland:{context.username}@{context.hostname}"),
        },
        "tags": langfuse_tags_for_session(
            metadata=context.environment_metadata.metadata,
        ),
    }
    if context.environment_metadata.connection is not None:
        connection = context.environment_metadata.connection
        config["metadata"].update(
            {
                "dreamland.connection.protocol": connection.protocol or "",
                "dreamland.connection.source_ip": connection.source.ip or "",
                "dreamland.connection.source_port": _format_optional_value(
                    connection.source.port
                ),
                "dreamland.connection.destination_ip": (
                    connection.destination.ip or ""
                ),
                "dreamland.connection.destination_port": _format_optional_value(
                    connection.destination.port
                ),
            }
        )
    config["metadata"].update(
        _transport_connection_trace_metadata(context.transport_connection)
    )
    state_profile_name = context.environment_metadata.metadata.get("state_profile.name")
    state_profile_level = context.environment_metadata.metadata.get(
        "state_profile.level"
    )
    if state_profile_name is not None:
        config["metadata"]["dreamland.state_profile.name"] = str(state_profile_name)
    if state_profile_level is not None:
        config["metadata"]["dreamland.state_profile.level"] = str(state_profile_level)
    _add_session_metadata(config["metadata"], context.environment_metadata.metadata)
    config["metadata"].update(public_trace_metadata(context))
    TOKEN_LOGGER.info(
        "command_id=%s context_projection_prepared=active-host-v1 host_count=%s "
        "detailed_host_count=%s compact_host_count=%s full_world_chars=%s "
        "projected_world_chars=%s saved_world_chars=%s",
        context.command_id,
        projection["host_count"],
        projection["detailed_host_count"],
        projection["compact_host_count"],
        projection["full_world_chars"],
        projection["projected_world_chars"],
        projection["saved_world_chars"],
    )
    if callbacks:
        config["callbacks"] = list(callbacks)
    return config


def _command_emulator_projection_metrics(
    context: CommandContext,
) -> dict[str, int | str]:
    world_state = context.world_state
    requested_ref = context.active_host or context.hostname
    projected = project_world_state_for_command_emulator(
        world_state,
        detailed_host_refs=(requested_ref,),
    )
    hosts = world_state.get("hosts_by_name")
    host_count = len(hosts) if isinstance(hosts, dict) else 0
    active_host = context.active_host or active_host_ref(world_state)
    detailed_hosts = detailed_host_registry_refs_for_command_emulator(
        world_state,
        detailed_host_refs=(requested_ref,),
    )
    detailed_host_count = len(detailed_hosts)
    full_world_chars = len(format_world_state_for_prompt(world_state))
    projected_world_chars = len(format_world_state_for_prompt(projected))
    return {
        "active_host": active_host,
        "detailed_hosts": ",".join(detailed_hosts),
        "host_count": host_count,
        "detailed_host_count": detailed_host_count,
        "compact_host_count": max(host_count - detailed_host_count, 0),
        "full_world_chars": full_world_chars,
        "projected_world_chars": projected_world_chars,
        "saved_world_chars": max(full_world_chars - projected_world_chars, 0),
    }


def _host_command_history_length(context: CommandContext) -> int:
    world_state = context.world_state
    active_host = context.active_host or active_host_ref(world_state)
    hosts = world_state.get("hosts_by_name")
    if not isinstance(hosts, dict):
        return 0
    host = hosts.get(active_host)
    if not isinstance(host, dict):
        return 0
    history = host.get("command_history")
    return len(history) if isinstance(history, list) else 0


def _transport_connection_trace_metadata(
    connection: ConnectionMetadata | None,
) -> dict[str, object]:
    if connection is None:
        return {}
    raw = connection.metadata.get("transport")
    raw = raw if isinstance(raw, dict) else {}
    metadata: dict[str, object] = {
        "dreamland.transport.connection.protocol": connection.protocol or "",
    }
    for name, endpoint in (
        ("source", connection.source),
        ("destination", connection.destination),
    ):
        captured = raw.get(name)
        captured = captured if isinstance(captured, dict) else {}
        ip = captured.get("ip", endpoint.ip)
        port = captured.get("port", endpoint.port)
        metadata[f"dreamland.transport.connection.{name}_ip"] = (
            ip if isinstance(ip, str) else endpoint.ip or ""
        )
        metadata[f"dreamland.transport.connection.{name}_port"] = (
            _format_optional_value(
                port if isinstance(port, (str, int)) else endpoint.port
            )
        )
    return metadata


def _add_session_metadata(
    target: dict[str, object],
    source: dict[str, object],
) -> None:
    evaluation = source.get("dreamland.session.evaluation_config")
    if isinstance(evaluation, dict):
        target["dreamland.session.evaluation_config"] = evaluation
    origin = session_origin_from_metadata(source)
    label = session_label_from_metadata(source)
    tags = session_tags_from_metadata(source)
    if origin is not None:
        target[SESSION_ORIGIN_METADATA_KEY] = origin
    if label is not None:
        target[SESSION_LABEL_METADATA_KEY] = label
    if tags:
        target[SESSION_TAGS_METADATA_KEY] = ",".join(tags)
    extra = source.get(SESSION_METADATA_METADATA_KEY)
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(key, str):
                target[f"{SESSION_METADATA_METADATA_KEY}.{key}"] = value


def transition_trace_metadata(outputs: object) -> dict[str, object]:
    if not isinstance(outputs, dict):
        return {}
    result = outputs.get("result")
    if not isinstance(result, dict):
        return {}
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    return {
        key: value
        for key, value in metadata.items()
        if isinstance(key, str)
        and (
            key.startswith("dreamland.transition.")
            or key == "dreamland.reasoning_summary"
        )
    }


def _token_usage(response: Any) -> TokenUsage | None:
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        parsed = _parse_usage(usage)
        if parsed is not None:
            return parsed

    for generation_group in getattr(response, "generations", ()):
        for generation in generation_group:
            message = getattr(generation, "message", None)
            parsed = _parse_usage(getattr(message, "usage_metadata", None))
            if parsed is not None:
                return parsed
            response_metadata = getattr(message, "response_metadata", None)
            if isinstance(response_metadata, dict):
                parsed = _parse_usage(
                    response_metadata.get("token_usage")
                    or response_metadata.get("usage")
                )
                if parsed is not None:
                    return parsed
    return None


def _parse_usage(value: object) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    input_tokens = _usage_int(value, "input_tokens", "prompt_tokens")
    output_tokens = _usage_int(value, "output_tokens", "completion_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    total_tokens = _usage_int(value, "total_tokens")
    input_cache_creation = _usage_int(
        value,
        "input_cache_creation",
        "cache_creation_input_tokens",
        "cache_write_tokens",
        "priority_cache_creation",
        "flex_cache_creation",
    )
    input_cache_read = _usage_int(
        value,
        "input_cache_read",
        "cache_read_input_tokens",
        "cached_tokens",
        "priority_cache_read",
        "flex_cache_read",
    )
    for input_details in (
        value.get("input_token_details"),
        value.get("input_tokens_details"),
        value.get("prompt_tokens_details"),
    ):
        if not isinstance(input_details, dict):
            continue
        if input_cache_creation is None:
            input_cache_creation = _usage_int(
                input_details,
                "cache_creation",
                "cache_creation_input_tokens",
                "cache_write_tokens",
                "priority_cache_creation",
                "flex_cache_creation",
            )
        if input_cache_read is None:
            input_cache_read = _usage_int(
                input_details,
                "cache_read",
                "cache_read_input_tokens",
                "cached_tokens",
                "priority_cache_read",
                "flex_cache_read",
            )
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=(
            total_tokens if total_tokens is not None else input_tokens + output_tokens
        ),
        input_cache_creation=input_cache_creation,
        input_cache_read=input_cache_read,
    )


def _usage_int(value: dict[str, object], *keys: str) -> int | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, int):
            return candidate
    return None


def _reported_service_tier(response: Any) -> str | None:
    """Use returned metadata, never infer actual serving tier from a request."""
    candidates = [getattr(response, "llm_output", None)]
    for group in getattr(response, "generations", ()):
        for generation in group:
            candidates.append(getattr(generation, "generation_info", None))
            candidates.append(
                getattr(getattr(generation, "message", None), "response_metadata", None)
            )
    for metadata in candidates:
        if isinstance(metadata, dict):
            value = metadata.get("service_tier")
            if isinstance(value, str) and value in {
                "auto",
                "default",
                "flex",
                "fast",
                "priority",
                "ultrafast",
                "scale",
            }:
                return value
    return None


def _finish_reason(response: Any) -> str | None:
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        value = llm_output.get("finish_reason")
        if isinstance(value, str):
            return value
    for generation_group in getattr(response, "generations", ()):
        for generation in generation_group:
            generation_info = getattr(generation, "generation_info", None)
            if isinstance(generation_info, dict):
                value = generation_info.get("finish_reason")
                if isinstance(value, str):
                    return value
            message = getattr(generation, "message", None)
            response_metadata = getattr(message, "response_metadata", None)
            if isinstance(response_metadata, dict):
                value = response_metadata.get("finish_reason")
                if isinstance(value, str):
                    return value
    return None


def _output_cap_hit(
    *,
    finish_reason: str | None,
    output_tokens: int | None,
    max_output_tokens: int | None,
) -> bool:
    if max_output_tokens is None:
        return False
    return finish_reason in {"length", "max_output_tokens"} or (
        output_tokens is not None and output_tokens >= max_output_tokens
    )


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _format_optional_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)
