from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from inspect import signature
from typing import Any, Protocol

from dreamland.agent_core import (
    _state_update_from_structured_model_payload,
    _system_prompt_content_for_model,
)
from dreamland.agent_state import (
    environment_metadata_to_state,
    host_identity_to_state,
    interactive_program_to_state,
    shell_environment_to_state,
    shell_frame_to_state,
)
from dreamland.agent_tracing import build_langfuse_callbacks
from dreamland.config import SpiritguideSettings
from dreamland.lobby_identity import public_trace_metadata
from dreamland.model_providers import ChatModelProviderConfig, build_chat_model
from dreamland.models import CommandStateUpdate, RuntimeProvenance, SessionState
from dreamland.prompt_loader import load_prompt
from dreamland.world_state import format_world_state_for_prompt

LOGGER = logging.getLogger(__name__)
ANTHROPIC_SPIRITGUIDE_WARNING = (
    "Anthropic/Claude is currently not recommended for DREAMLAND Spiritguide. "
    "Provider dry runs repeatedly produced oversized or malformed broad "
    "world-state patches, including with high reasoning effort. Prefer xAI "
    "Grok for Spiritguide, with OpenAI as the next-best tested option."
)


@dataclass(frozen=True, slots=True)
class SpiritguideContext:
    instruction: str
    session_id: str
    session: SessionState
    dry_run: bool = False
    runtime_provenance: RuntimeProvenance | None = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_provenance", self.session.runtime_provenance)


@dataclass(frozen=True, slots=True)
class SpiritguideResult:
    message: str = ""
    state_update: CommandStateUpdate = field(default_factory=CommandStateUpdate)
    metadata: dict[str, object] = field(default_factory=dict)


class Spiritguide(Protocol):
    def respond(self, context: SpiritguideContext) -> SpiritguideResult:
        pass


class DeterministicSpiritguide:
    def respond(self, context: SpiritguideContext) -> SpiritguideResult:
        return SpiritguideResult(
            message=(
                "Spiritguide received the instruction, but no model-backed "
                "state guidance is configured yet."
            )
        )


class ModelSpiritguide:
    def __init__(
        self,
        settings: SpiritguideSettings,
        *,
        api_key: str | None = None,
        model: Any | None = None,
        system_prompt: str | None = None,
        langfuse_enabled: bool = False,
        langfuse_public_key: str | None = None,
        langfuse_secret_key: str | None = None,
        langfuse_host: str | None = None,
        callbacks: tuple[Any, ...] | None = None,
    ) -> None:
        self.settings = settings
        if settings.model_provider == "anthropic":
            LOGGER.warning(ANTHROPIC_SPIRITGUIDE_WARNING)
        self.api_key = api_key
        self.system_prompt = system_prompt or load_prompt(settings.system_prompt_name)
        self.model = model or self._default_model()
        self.langfuse_enabled = langfuse_enabled
        self.langfuse_public_key = langfuse_public_key
        self.langfuse_secret_key = langfuse_secret_key
        self.langfuse_host = langfuse_host
        self.callbacks = callbacks

    def respond(self, context: SpiritguideContext) -> SpiritguideResult:
        user_context = _format_spiritguide_context(context)
        messages = [
            _message(
                "system",
                _system_prompt_content_for_model(
                    self.system_prompt,
                    provider=self.settings.model_provider,
                    prompt_cache=self.settings.prompt_cache,
                ),
            ),
            _message("user", user_context),
        ]
        callbacks = self._callbacks_for_context(context)
        if not callbacks:
            response = self.model.invoke(messages)
            return spiritguide_result_from_model_response(
                _message_content_to_text(response)
            )

        metadata = _spiritguide_trace_metadata(context)
        tags = ["dreamland", "spiritguide"]
        try:
            from langchain_core.callbacks.manager import CallbackManager
        except ImportError as exc:  # pragma: no cover - optional runtime deps.
            raise RuntimeError(
                "Spiritguide Langfuse tracing requires langchain."
            ) from exc

        callback_manager = CallbackManager.configure(
            inheritable_callbacks=list(callbacks),
            inheritable_tags=tags,
            inheritable_metadata=metadata,
        )
        run_manager = callback_manager.on_chain_start(
            {"name": "dreamland-spiritguide-agent"},
            {
                "instruction": context.instruction,
                "spiritguide_context": user_context,
            },
        )
        try:
            response = _invoke_model(
                self.model,
                messages,
                config={
                    "callbacks": run_manager.get_child(),
                    "run_name": "spiritguide-call-model",
                    "tags": tags,
                    "metadata": metadata,
                },
            )
            raw_output = _message_content_to_text(response)
            result = spiritguide_result_from_model_response(raw_output)
            outputs = {
                "message": result.message,
                "raw_output": raw_output,
                "state_update": _state_update_to_trace_state(result.state_update),
                "metadata": {
                    **result.metadata,
                    "dreamland.spiritguide.patch_keys": _patch_keys(
                        result.state_update
                    ),
                },
            }
            run_manager.on_chain_end(outputs)
            return result
        except Exception as exc:
            run_manager.on_chain_error(exc)
            raise

    def _callbacks_for_context(self, context: SpiritguideContext) -> tuple[Any, ...]:
        if self.callbacks is not None:
            return self.callbacks
        return build_langfuse_callbacks(
            enabled=self.langfuse_enabled,
            public_key=self.langfuse_public_key,
            secret_key=self.langfuse_secret_key,
            host=self.langfuse_host,
            trace_id=context.session_id,
        )

    def _default_model(self) -> Any:
        model_reference = self.settings.model_reference
        return build_chat_model(
            ChatModelProviderConfig(
                provider=model_reference.provider,
                model=model_reference.model,
                api_key=self.api_key,
                reasoning_effort=self.settings.reasoning_effort,
                reasoning_summary=self.settings.reasoning_summary,
                verbosity=self.settings.verbosity,
                speed=self.settings.speed,
                prompt_cache=self.settings.prompt_cache,
                max_output_tokens=self.settings.max_output_tokens,
                openai_api=self.settings.openai_api,
                openai_service_tier=self.settings.openai_service_tier,
                openai_reasoning_mode=self.settings.openai_reasoning_mode,
            )
        )


def spiritguide_result_from_model_response(text: str) -> SpiritguideResult:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        LOGGER.warning("Spiritguide response was not valid JSON: %s", exc)
        return SpiritguideResult(
            message=text,
            metadata={
                "structured_parse_error": str(exc),
                "structured_response_accepted": False,
            },
        )
    if not isinstance(payload, dict):
        return SpiritguideResult(
            message=text,
            metadata={
                "structured_parse_error": "response must be a JSON object",
                "structured_response_accepted": False,
            },
        )
    state_update = payload.get("state_update", {})
    metadata: dict[str, object] = {"structured_response_accepted": True}
    if not isinstance(state_update, dict):
        metadata["structured_parse_error"] = "state_update must be an object"
        state_update = {}
    return SpiritguideResult(
        message=str(payload.get("message", "")),
        state_update=_state_update_from_structured_model_payload(
            state_update,
            metadata=metadata,
        ),
        metadata=metadata,
    )


def _format_spiritguide_context(context: SpiritguideContext) -> str:
    session = context.session
    active_frame = session.shell_stack[-1] if session.shell_stack else None
    lines = [
        "DREAMLAND spiritguide request",
        "",
        "Operator instruction:",
        context.instruction,
        "",
        "Current Dreamland context:",
        f"username: {session.username}",
        f"login_username: {session.login_username or session.username}",
        f"host_identity_hostname: {session.host_identity.hostname}",
        f"cwd: {session.cwd}",
    ]
    if active_frame is not None:
        lines.extend(
            [
                f"active_frame_username: {active_frame.username}",
                f"active_frame_cwd: {active_frame.cwd}",
                "active_interactive_program:",
                _indent_json(
                    interactive_program_to_state(active_frame.interactive_program)
                ),
                "active_shell_environment:",
                _indent_json(
                    shell_environment_to_state(active_frame.shell_environment)
                ),
            ]
        )
    lines.extend(
        [
            "environment_metadata:",
            _indent_json(
                environment_metadata_to_state(
                    replace(
                        session.environment_metadata,
                        metadata={
                            key: value
                            for key, value in session.environment_metadata.metadata.items()
                            if key != "dreamland.session.evaluation_config"
                        },
                    )
                )
            ),
            "shell_stack:",
            _indent_json(
                [shell_frame_to_state(frame) for frame in session.shell_stack]
            ),
            "world_state:",
            _indent_block(format_world_state_for_prompt(session.world_state)),
        ]
    )
    return "\n".join(lines)


def _message(role: str, content: str | list[dict[str, object]]) -> Any:
    try:
        if role == "system":
            from langchain_core.messages import SystemMessage

            return SystemMessage(content=content)
        from langchain_core.messages import HumanMessage

        return HumanMessage(content=content)
    except ImportError:  # pragma: no cover - test and fallback path.
        return {"role": role, "content": content}


def _message_content_to_text(message: object) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text is not None:
                    parts.append(str(text))
                continue
            parts.append(str(item))
        return "".join(parts)
    return str(content)


def _invoke_model(model: Any, messages: list[Any], *, config: dict[str, Any]) -> Any:
    try:
        parameters = signature(model.invoke).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "config" in parameters:
        return model.invoke(messages, config=config)
    return model.invoke(messages)


def _spiritguide_trace_metadata(context: SpiritguideContext) -> dict[str, object]:
    session = context.session
    active_frame = session.shell_stack[-1] if session.shell_stack else None
    instruction_hash = hashlib.sha256(context.instruction.encode("utf-8")).hexdigest()
    metadata: dict[str, object] = {
        "dreamland.operation": "spiritguide",
        "dreamland.username": session.username,
        "dreamland.login_username": session.login_username or session.username,
        "dreamland.cwd": session.cwd,
        "dreamland.spiritguide.instruction_preview": _metadata_preview(
            context.instruction
        ),
        "dreamland.spiritguide.instruction_length": str(len(context.instruction)),
        "dreamland.spiritguide.instruction_sha256": instruction_hash[:16],
        "dreamland.spiritguide.dry_run": _bool_text(context.dry_run),
        "dreamland.shell_stack_depth": str(max(len(session.shell_stack), 1)),
        "dreamland.history_length": str(len(session.history)),
        "langfuse_session_id": context.session_id,
        "langfuse_user_id": session.username,
        "langfuse_trace_name": f"dreamland:{session.username}:spiritguide",
    }
    evaluation = session.environment_metadata.metadata.get(
        "dreamland.session.evaluation_config"
    )
    if isinstance(evaluation, dict):
        metadata["dreamland.session.evaluation_config"] = evaluation
    if active_frame is not None:
        metadata["dreamland.interactive_program"] = (
            active_frame.interactive_program.name
        )
        metadata["dreamland.cwd"] = active_frame.cwd
    metadata.update(public_trace_metadata(context))
    return metadata


def _metadata_preview(value: str, *, limit: int = 160) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def _state_update_to_trace_state(update: CommandStateUpdate) -> dict[str, object]:
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
            "metadata": update.shell_stack_transition.metadata,
        }
    if update.world_state is not None:
        state["world_state"] = update.world_state
    return state


def _patch_keys(update: CommandStateUpdate) -> list[str]:
    keys = []
    for name in (
        "host_identity",
        "cwd",
        "interactive_program",
        "shell_environment",
        "shell_stack",
        "shell_stack_transition",
        "world_state",
    ):
        if getattr(update, name) is not None:
            keys.append(name)
    return keys


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _indent_block(value: str) -> str:
    if not value:
        return "  "
    return "\n".join(f"  {line}" for line in value.splitlines())


def _indent_json(value: object) -> str:
    return _indent_block(json.dumps(value, indent=2, sort_keys=True))
