from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

LOGGER = logging.getLogger(__name__)
LLM_PROVIDER_VALUES = frozenset({"anthropic", "openai", "xai"})
ANTHROPIC_REASONING_EFFORTS = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}
OPENAI_API_VALUES = frozenset({"responses", "chat_completions"})
OPENAI_SERVICE_TIER_VALUES = frozenset(
    {"auto", "default", "flex", "fast", "priority", "ultrafast"}
)
OPENAI_REASONING_MODE_VALUES = frozenset({"standard", "pro"})


@dataclass(frozen=True, slots=True)
class ModelReference:
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class ChatModelProviderConfig:
    provider: str
    model: str
    api_key: str | None = None
    reasoning_effort: str | None = None
    reasoning_summary: str | None = None
    verbosity: str | None = None
    speed: str | None = None
    prompt_cache: str | None = None
    max_output_tokens: int | None = None
    max_retries: int | None = None
    streaming: bool = False
    openai_api: str | None = None
    openai_service_tier: str | None = None
    openai_reasoning_mode: str | None = None


def _is_model_family(model: str, family: str) -> bool:
    return model == family or model.startswith(f"{family}-")


def supports_openai_explicit_cache(model: str) -> bool:
    model = model.strip().lower()
    return any(
        _is_model_family(model, family)
        for family in (
            "gpt-5.6",
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6.1-sol",
            "gpt-6-luna",
        )
    )


def validate_model_controls(config: ChatModelProviderConfig) -> None:
    """Reject known OpenAI incompatibilities without constructing a provider."""
    if config.provider != "openai":
        return
    for control, allowed in (
        ("openai_api", OPENAI_API_VALUES),
        ("openai_service_tier", OPENAI_SERVICE_TIER_VALUES),
        ("openai_reasoning_mode", OPENAI_REASONING_MODE_VALUES),
    ):
        value = getattr(config, control)
        if value is not None and value not in allowed:
            raise ValueError(
                f"Invalid {control}: {value!r}; expected {sorted(allowed)}"
            )
    model = config.model.strip().lower()
    if _is_model_family(model, "gpt-6-astra") and config.reasoning_effort == "none":
        raise ValueError(
            "GPT-6 Astra does not support reasoning_effort=none; use low or higher"
        )
    if _is_model_family(model, "gpt-6.1-sol") and config.reasoning_effort in (
        "none",
        "minimal",
    ):
        raise ValueError(
            f"GPT-6.1 Sol does not support reasoning_effort={config.reasoning_effort}; "
            "use low or higher"
        )
    older_gpt5 = any(
        _is_model_family(model, family)
        for family in ("gpt-5", "gpt-5.1", "gpt-5.2", "gpt-5.3", "gpt-5.4", "gpt-5.5")
    )
    if older_gpt5 and config.reasoning_effort == "max":
        raise ValueError(f"{config.model} does not support reasoning_effort=max")
    if older_gpt5 and config.openai_reasoning_mode is not None:
        raise ValueError(
            f"{config.model} does not support openai_reasoning_mode; leave it blank"
        )
    if config.openai_api == "chat_completions" and (
        config.reasoning_summary is not None or config.openai_reasoning_mode is not None
    ):
        raise ValueError(
            "openai_api=chat_completions requires blank reasoning_summary and "
            "openai_reasoning_mode; use openai_api=responses for these controls"
        )


def parse_model_reference(value: str, *, name: str = "model") -> ModelReference:
    provider, separator, model = value.partition(":")
    if not separator:
        choices = ", ".join(sorted(LLM_PROVIDER_VALUES))
        raise ValueError(
            f"Expected {name} to use provider:model format with provider one "
            f"of {choices}, got {value!r}."
        )
    provider = provider.strip().lower()
    model = model.strip()
    if provider not in LLM_PROVIDER_VALUES:
        choices = ", ".join(sorted(LLM_PROVIDER_VALUES))
        raise ValueError(f"Expected {name} provider to be one of {choices}.")
    if not model:
        raise ValueError(f"Expected {name} to include a model after ':'.")
    return ModelReference(provider=provider, model=model)


def build_chat_model(config: ChatModelProviderConfig) -> Any:
    validate_model_controls(config)
    if config.max_retries is not None and (
        isinstance(config.max_retries, bool)
        or not isinstance(config.max_retries, int)
        or not 0 <= config.max_retries <= 1_000_000
    ):
        raise ValueError("max_retries must be a bounded non-negative integer")
    if config.provider == "anthropic":
        return _build_anthropic_model(config)
    if config.provider == "openai":
        return _build_openai_model(config)
    if config.provider == "xai":
        return _build_xai_model(config)
    choices = ", ".join(sorted(LLM_PROVIDER_VALUES))
    raise ValueError(
        f"Expected LLM provider to be one of {choices}, got {config.provider!r}."
    )


def _build_openai_model(config: ChatModelProviderConfig) -> Any:
    if config.speed is not None:
        _log_ignored_control(
            provider=config.provider,
            model=config.model,
            control="speed",
            value=config.speed,
        )
    if config.prompt_cache is not None:
        _log_ignored_control(
            provider=config.provider,
            model=config.model,
            control="prompt_cache",
            value=config.prompt_cache,
        )

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - depends on optional runtime deps.
        raise RuntimeError(
            "OpenAI provider requires langchain-openai. "
            "Install the project dependencies first."
        ) from exc

    model_kwargs: dict[str, Any] = {}
    use_responses = (
        config.openai_api == "responses"
        or config.reasoning_summary is not None
        or config.openai_reasoning_mode is not None
        or (
            config.openai_api is None
            and _is_model_family(config.model.lower(), "gpt-6-astra")
        )
    )
    if use_responses:
        reasoning: dict[str, Any] = {}
        if config.reasoning_summary is not None:
            reasoning["summary"] = config.reasoning_summary
        if config.reasoning_effort is not None:
            reasoning["effort"] = config.reasoning_effort
        if config.openai_reasoning_mode is not None:
            reasoning["mode"] = config.openai_reasoning_mode
        if reasoning:
            model_kwargs["reasoning"] = reasoning
        model_kwargs["output_version"] = "responses/v1"
        model_kwargs["use_responses_api"] = True
    else:
        model_kwargs["reasoning_effort"] = config.reasoning_effort
        if config.openai_api == "chat_completions":
            model_kwargs["use_responses_api"] = False
    if config.openai_service_tier is not None:
        model_kwargs["service_tier"] = config.openai_service_tier
    if config.streaming:
        model_kwargs["streaming"] = True
        model_kwargs["stream_usage"] = True
    if config.max_retries is not None:
        model_kwargs["max_retries"] = config.max_retries

    model = ChatOpenAI(
        model=config.model,
        api_key=cast(Any, config.api_key),
        verbosity=config.verbosity,
        max_completion_tokens=config.max_output_tokens,
        **model_kwargs,
    )
    logging.getLogger("dreamland.lifecycle").info(
        "model.configured provider=openai model=%s openai_api=%s "
        "reasoning_effort=%s reasoning_summary=%s openai_reasoning_mode=%s "
        "openai_service_tier=%s verbosity=%s max_output_tokens=%s",
        config.model,
        "responses" if use_responses else config.openai_api or "sdk_default",
        config.reasoning_effort or "default",
        config.reasoning_summary or "off",
        config.openai_reasoning_mode or "default",
        config.openai_service_tier or "project_default",
        config.verbosity or "default",
        config.max_output_tokens,
    )
    return model


def _build_xai_model(config: ChatModelProviderConfig) -> Any:
    _log_ignored_openai_controls(config)
    if config.speed is not None:
        _log_ignored_control(
            provider=config.provider,
            model=config.model,
            control="speed",
            value=config.speed,
        )
    if config.reasoning_summary is not None:
        _log_ignored_control(
            provider=config.provider,
            model=config.model,
            control="reasoning_summary",
            value=config.reasoning_summary,
        )
    if config.verbosity is not None:
        _log_ignored_control(
            provider=config.provider,
            model=config.model,
            control="verbosity",
            value=config.verbosity,
        )
    if config.prompt_cache is not None:
        _log_ignored_control(
            provider=config.provider,
            model=config.model,
            control="prompt_cache",
            value=config.prompt_cache,
        )

    try:
        from langchain_xai import ChatXAI
    except ImportError as exc:  # pragma: no cover - depends on optional runtime deps.
        raise RuntimeError(
            "xAI provider requires langchain-xai. Install the xAI dependencies first."
        ) from exc

    model_kwargs: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_output_tokens,
        "reasoning_effort": config.reasoning_effort,
    }
    if config.reasoning_effort == "max":
        _log_ignored_control(
            provider=config.provider,
            model=config.model,
            control="reasoning_effort",
            value=config.reasoning_effort,
        )
        model_kwargs.pop("reasoning_effort")
    if config.api_key is not None:
        model_kwargs["api_key"] = cast(Any, config.api_key)
    if config.streaming:
        model_kwargs["streaming"] = True
    if config.max_retries is not None:
        model_kwargs["max_retries"] = config.max_retries
    return ChatXAI(**model_kwargs)


def _build_anthropic_model(config: ChatModelProviderConfig) -> Any:
    _log_ignored_openai_controls(config)
    if config.reasoning_summary is not None:
        _log_ignored_control(
            provider=config.provider,
            model=config.model,
            control="reasoning_summary",
            value=config.reasoning_summary,
        )
    if config.verbosity is not None:
        _log_ignored_control(
            provider=config.provider,
            model=config.model,
            control="verbosity",
            value=config.verbosity,
        )

    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:  # pragma: no cover - depends on optional runtime deps.
        raise RuntimeError(
            "Anthropic provider requires langchain-anthropic. "
            "Install the Anthropic dependencies first."
        ) from exc

    model_kwargs: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_output_tokens,
    }
    if config.api_key is not None:
        model_kwargs["api_key"] = cast(Any, config.api_key)
    if config.streaming:
        model_kwargs["streaming"] = True
        model_kwargs["stream_usage"] = True
    if config.max_retries is not None:
        model_kwargs["max_retries"] = config.max_retries
    anthropic_model_kwargs: dict[str, Any] = {}
    if config.speed == "fast":
        anthropic_model_kwargs["speed"] = "fast"
        model_kwargs["betas"] = ["fast-mode-2026-02-01"]
    if anthropic_model_kwargs:
        model_kwargs["model_kwargs"] = anthropic_model_kwargs
    if config.reasoning_effort is not None:
        effort = ANTHROPIC_REASONING_EFFORTS.get(config.reasoning_effort)
        if effort is None:
            _log_ignored_control(
                provider=config.provider,
                model=config.model,
                control="reasoning_effort",
                value=config.reasoning_effort,
            )
        else:
            model_kwargs["effort"] = effort
            model_kwargs["thinking"] = {
                "type": "adaptive",
                "display": "summarized",
            }
    return ChatAnthropic(**model_kwargs)


def _log_ignored_openai_controls(config: ChatModelProviderConfig) -> None:
    for control in ("openai_api", "openai_service_tier", "openai_reasoning_mode"):
        value = getattr(config, control)
        if value is not None:
            _log_ignored_control(
                provider=config.provider,
                model=config.model,
                control=control,
                value=value,
            )


def _log_ignored_control(
    *,
    provider: str,
    model: str,
    control: str,
    value: object,
) -> None:
    LOGGER.info(
        "provider=%s model=%s ignored_unsupported_model_control=%s value=%r",
        provider,
        model,
        control,
        value,
    )
