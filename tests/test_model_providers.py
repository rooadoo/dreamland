import sys
import types
import unittest
from unittest.mock import patch

from dreamland.model_providers import (
    ChatModelProviderConfig,
    build_chat_model,
    parse_model_reference,
)


class ModelProviderTests(unittest.TestCase):
    def test_parse_model_reference_splits_provider_and_model(self):
        model_reference = parse_model_reference("xai:grok-4.3", name="agent model")

        self.assertEqual(model_reference.provider, "xai")
        self.assertEqual(model_reference.model, "grok-4.3")

    def test_parse_model_reference_requires_provider_prefix(self):
        with self.assertRaisesRegex(ValueError, "provider:model"):
            parse_model_reference("grok-4.3")

    def test_openai_provider_passes_openai_model_controls(self):
        with patch("langchain_openai.ChatOpenAI") as chat_openai:
            build_chat_model(
                ChatModelProviderConfig(
                    provider="openai",
                    model="gpt-test",
                    api_key="sk-test",
                    reasoning_effort="low",
                    verbosity=None,
                    speed="fast",
                    prompt_cache="automatic",
                    max_output_tokens=640,
                    max_retries=0,
                    streaming=True,
                )
            )

        chat_openai.assert_called_once()
        kwargs = chat_openai.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-test")
        self.assertEqual(kwargs["api_key"], "sk-test")
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertIsNone(kwargs["verbosity"])
        self.assertNotIn("speed", kwargs)
        self.assertNotIn("prompt_cache", kwargs)
        self.assertEqual(kwargs["max_completion_tokens"], 640)
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertTrue(kwargs["streaming"])
        self.assertTrue(kwargs["stream_usage"])

    def test_openai_reasoning_summary_uses_responses_payload(self):
        with patch("langchain_openai.ChatOpenAI") as chat_openai:
            build_chat_model(
                ChatModelProviderConfig(
                    provider="openai",
                    model="gpt-test",
                    api_key="sk-test",
                    reasoning_effort="low",
                    reasoning_summary="auto",
                )
            )

        kwargs = chat_openai.call_args.kwargs
        self.assertEqual(kwargs["reasoning"], {"effort": "low", "summary": "auto"})
        self.assertEqual(kwargs["output_version"], "responses/v1")
        self.assertNotIn("reasoning_effort", kwargs)
        self.assertNotIn("streaming", kwargs)

    def test_langchain_openai_serializes_command_json_and_cache_for_both_apis(self):
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI

        from dreamland.agent_core import _command_system_prompt_and_invoke_kwargs

        system_content, invoke_kwargs = _command_system_prompt_and_invoke_kwargs(
            "Return one JSON object.",
            provider="openai",
            model="gpt-5.6",
            prompt_cache=None,
        )
        messages = [
            SystemMessage(content=system_content),
            HumanMessage(content="dynamic command context"),
        ]

        for use_responses_api, collection_key, system_text_type in (
            (True, "input", "input_text"),
            (False, "messages", "text"),
        ):
            with self.subTest(use_responses_api=use_responses_api):
                model = ChatOpenAI(
                    model="gpt-5.6",
                    api_key="test-key",
                    use_responses_api=use_responses_api,
                    streaming=True,
                )
                payload = model._get_request_payload(messages, **invoke_kwargs)

                self.assertTrue(payload["stream"])
                if use_responses_api:
                    self.assertEqual(payload["text"]["format"], {"type": "json_object"})
                    self.assertNotIn("response_format", payload)
                else:
                    self.assertEqual(
                        payload["response_format"], {"type": "json_object"}
                    )

                self.assertEqual(
                    payload["prompt_cache_key"],
                    invoke_kwargs["prompt_cache_key"],
                )
                self.assertEqual(
                    payload["prompt_cache_options"],
                    {"mode": "explicit"},
                )
                self.assertNotIn("ttl", payload["prompt_cache_options"])
                self.assertNotIn("prompt_cache_retention", payload)
                self.assertNotIn("extra_body", payload)

                outbound_messages = payload[collection_key]
                system_block = outbound_messages[0]["content"][0]
                self.assertEqual(system_block["type"], system_text_type)
                self.assertEqual(system_block["text"], "Return one JSON object.")
                self.assertEqual(
                    system_block["prompt_cache_breakpoint"],
                    {"mode": "explicit"},
                )
                self.assertEqual(outbound_messages[1]["role"], "user")
                self.assertEqual(
                    outbound_messages[1]["content"],
                    "dynamic command context",
                )

    def test_foreground_json_mode_is_not_sent_to_other_providers(self):
        from dreamland.agent_core import _command_system_prompt_and_invoke_kwargs

        for provider, model_name in (
            ("anthropic", "claude-test"),
            ("xai", "grok-test"),
        ):
            with self.subTest(provider=provider):
                _, options = _command_system_prompt_and_invoke_kwargs(
                    "Return one JSON object.",
                    provider=provider,
                    model=model_name,
                    prompt_cache=None,
                )
                self.assertNotIn("response_format", options)

    def test_openai_evaluation_controls_survive_real_sdk_serialization(self):
        from langchain_core.messages import HumanMessage, SystemMessage

        from dreamland.agent_core import _command_system_prompt_and_invoke_kwargs

        for model_name in (
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6.1-sol",
            "gpt-6-luna",
        ):
            for tier in ("auto", "default", "flex", "fast", "priority", "ultrafast"):
                with self.subTest(model=model_name, tier=tier):
                    model = build_chat_model(
                        ChatModelProviderConfig(
                            provider="openai",
                            model=model_name,
                            api_key="test-key",
                            reasoning_effort="max",
                            openai_api="responses",
                            openai_reasoning_mode="pro",
                            openai_service_tier=tier,
                            max_output_tokens=4096,
                        )
                    )
                    system, options = _command_system_prompt_and_invoke_kwargs(
                        "stable prompt",
                        provider="openai",
                        model=model_name,
                        prompt_cache=None,
                    )
                    payload = model._get_request_payload(
                        [
                            SystemMessage(content=system),
                            HumanMessage(content="command"),
                        ],
                        **options,
                    )
                    self.assertEqual(payload["model"], model_name)
                    self.assertEqual(
                        payload["reasoning"], {"effort": "max", "mode": "pro"}
                    )
                    self.assertEqual(payload["service_tier"], tier)
                    self.assertEqual(payload["max_output_tokens"], 4096)
                    self.assertEqual(
                        payload["prompt_cache_options"], {"mode": "explicit"}
                    )
                    self.assertEqual(
                        payload["input"][0]["content"][0]["prompt_cache_breakpoint"],
                        {"mode": "explicit"},
                    )
                    for excluded in (
                        "temperature",
                        "top_p",
                        "logprobs",
                        "top_logprobs",
                        "extra_body",
                        "messages",
                    ):
                        self.assertNotIn(excluded, payload)

    def test_endpoint_can_be_selected_without_requesting_a_summary(self):
        from langchain_core.messages import HumanMessage

        for endpoint, collection in (
            ("responses", "input"),
            ("chat_completions", "messages"),
        ):
            with self.subTest(endpoint=endpoint):
                model = build_chat_model(
                    ChatModelProviderConfig(
                        provider="openai",
                        model="gpt-5.6-terra",
                        api_key="test-key",
                        reasoning_effort="none",
                        openai_api=endpoint,
                        openai_service_tier="default",
                    )
                )
                payload = model._get_request_payload([HumanMessage(content="hello")])
                self.assertIn(collection, payload)
                self.assertEqual(payload["service_tier"], "default")
                if endpoint == "responses":
                    self.assertEqual(payload["reasoning"], {"effort": "none"})
                else:
                    self.assertEqual(payload["reasoning_effort"], "none")

    def test_astra_defaults_to_responses_without_a_summary(self):
        from langchain_core.messages import HumanMessage

        model = build_chat_model(
            ChatModelProviderConfig(
                provider="openai",
                model="gpt-6-astra",
                api_key="test-key",
                reasoning_effort="low",
            )
        )
        payload = model._get_request_payload([HumanMessage(content="hello")])
        self.assertIn("input", payload)
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertNotIn("service_tier", payload)

    def test_invalid_openai_combinations_fail_before_provider_construction(self):
        cases = (
            (
                {"model": "gpt-6-astra", "reasoning_effort": "none"},
                "reasoning_effort=none",
            ),
            (
                {"model": "gpt-6.1-sol", "reasoning_effort": "none"},
                "reasoning_effort=none",
            ),
            (
                {"model": "gpt-6.1-sol-2026-10-01", "reasoning_effort": "minimal"},
                "reasoning_effort=minimal",
            ),
            ({"model": "gpt-5.5", "reasoning_effort": "max"}, "reasoning_effort=max"),
            (
                {"model": "gpt-5.5", "openai_reasoning_mode": "pro"},
                "openai_reasoning_mode",
            ),
            (
                {"openai_api": "chat_completions", "openai_reasoning_mode": "pro"},
                "requires blank",
            ),
            (
                {"openai_api": "chat_completions", "reasoning_summary": "detailed"},
                "requires blank",
            ),
            ({"openai_api": "invalid"}, "openai_api"),
            ({"openai_service_tier": "invalid"}, "openai_service_tier"),
            ({"openai_reasoning_mode": "invalid"}, "openai_reasoning_mode"),
        )
        for overrides, message in cases:
            with (
                self.subTest(overrides=overrides),
                patch("langchain_openai.ChatOpenAI") as factory,
            ):
                with self.assertRaisesRegex(ValueError, message):
                    build_chat_model(
                        ChatModelProviderConfig(
                            **{
                                "provider": "openai",
                                "model": "gpt-5.6-sol",
                                **overrides,
                            }
                        )
                    )
                factory.assert_not_called()

    def test_other_providers_do_not_receive_openai_evaluation_fields(self):
        for provider, model_name, module_name, class_name in (
            ("anthropic", "claude-test", "langchain_anthropic", "ChatAnthropic"),
            ("xai", "grok-test", "langchain_xai", "ChatXAI"),
        ):
            with (
                self.subTest(provider=provider),
                patch(f"{module_name}.{class_name}") as factory,
                self.assertLogs("dreamland.model_providers", level="INFO") as logs,
            ):
                build_chat_model(
                    ChatModelProviderConfig(
                        provider=provider,
                        model=model_name,
                        reasoning_effort="max",
                        openai_api="chat_completions",
                        openai_reasoning_mode="pro",
                        openai_service_tier="fast",
                        max_output_tokens=512,
                    )
                )
                kwargs = factory.call_args.kwargs
                for control in (
                    "openai_api",
                    "openai_reasoning_mode",
                    "openai_service_tier",
                ):
                    self.assertIn(
                        f"ignored_unsupported_model_control={control}",
                        "\n".join(logs.output),
                    )
                for field in (
                    "service_tier",
                    "use_responses_api",
                    "reasoning",
                    "openai_api",
                    "openai_service_tier",
                    "openai_reasoning_mode",
                ):
                    self.assertNotIn(field, kwargs)
                if provider == "anthropic":
                    self.assertEqual(kwargs["effort"], "max")
                else:
                    self.assertNotIn("reasoning_effort", kwargs)
                self.assertEqual(kwargs["max_tokens"], 512)

    def test_xai_provider_passes_xai_model_controls(self):
        fake_module = types.ModuleType("langchain_xai")
        fake_module.ChatXAI = FakeChatXAI
        with patch.dict(sys.modules, {"langchain_xai": fake_module}):
            model = build_chat_model(
                ChatModelProviderConfig(
                    provider="xai",
                    model="grok-4.3",
                    api_key="xai-test",
                    reasoning_effort="low",
                    max_output_tokens=512,
                    max_retries=0,
                    streaming=True,
                )
            )

        self.assertIsInstance(model, FakeChatXAI)
        self.assertEqual(
            model.kwargs,
            {
                "model": "grok-4.3",
                "api_key": "xai-test",
                "max_tokens": 512,
                "reasoning_effort": "low",
                "max_retries": 0,
                "streaming": True,
            },
        )

    def test_xai_provider_ignores_reasoning_summary_with_log(self):
        fake_module = types.ModuleType("langchain_xai")
        fake_module.ChatXAI = FakeChatXAI
        with (
            patch.dict(sys.modules, {"langchain_xai": fake_module}),
            self.assertLogs("dreamland.model_providers", level="INFO") as captured,
        ):
            model = build_chat_model(
                ChatModelProviderConfig(
                    provider="xai",
                    model="grok-4.3",
                    reasoning_summary="auto",
                )
            )

        self.assertIsInstance(model, FakeChatXAI)
        self.assertNotIn("reasoning_summary", model.kwargs)
        self.assertIn(
            "ignored_unsupported_model_control=reasoning_summary", captured.output[0]
        )

    def test_xai_provider_ignores_verbosity_with_log(self):
        fake_module = types.ModuleType("langchain_xai")
        fake_module.ChatXAI = FakeChatXAI
        with (
            patch.dict(sys.modules, {"langchain_xai": fake_module}),
            self.assertLogs("dreamland.model_providers", level="INFO") as captured,
        ):
            model = build_chat_model(
                ChatModelProviderConfig(
                    provider="xai",
                    model="grok-4.3",
                    verbosity="high",
                )
            )

        self.assertIsInstance(model, FakeChatXAI)
        self.assertNotIn("verbosity", model.kwargs)
        self.assertIn("ignored_unsupported_model_control=verbosity", captured.output[0])

    def test_xai_provider_ignores_speed_with_log(self):
        fake_module = types.ModuleType("langchain_xai")
        fake_module.ChatXAI = FakeChatXAI
        with (
            patch.dict(sys.modules, {"langchain_xai": fake_module}),
            self.assertLogs("dreamland.model_providers", level="INFO") as captured,
        ):
            model = build_chat_model(
                ChatModelProviderConfig(
                    provider="xai",
                    model="grok-4.3",
                    speed="fast",
                )
            )

        self.assertIsInstance(model, FakeChatXAI)
        self.assertNotIn("speed", model.kwargs)
        self.assertIn("ignored_unsupported_model_control=speed", captured.output[0])

    def test_xai_provider_ignores_prompt_cache_with_log(self):
        fake_module = types.ModuleType("langchain_xai")
        fake_module.ChatXAI = FakeChatXAI
        with (
            patch.dict(sys.modules, {"langchain_xai": fake_module}),
            self.assertLogs("dreamland.model_providers", level="INFO") as captured,
        ):
            model = build_chat_model(
                ChatModelProviderConfig(
                    provider="xai",
                    model="grok-4.3",
                    prompt_cache="automatic",
                )
            )

        self.assertIsInstance(model, FakeChatXAI)
        self.assertNotIn("prompt_cache", model.kwargs)
        self.assertIn(
            "ignored_unsupported_model_control=prompt_cache", captured.output[0]
        )

    def test_anthropic_provider_passes_anthropic_model_controls(self):
        fake_module = types.ModuleType("langchain_anthropic")
        fake_module.ChatAnthropic = FakeChatAnthropic
        with patch.dict(sys.modules, {"langchain_anthropic": fake_module}):
            model = build_chat_model(
                ChatModelProviderConfig(
                    provider="anthropic",
                    model="claude-sonnet-test",
                    api_key="anthropic-test",
                    reasoning_effort="medium",
                    speed="fast",
                    prompt_cache="automatic",
                    max_output_tokens=1024,
                    max_retries=0,
                    streaming=True,
                )
            )

        self.assertIsInstance(model, FakeChatAnthropic)
        self.assertEqual(
            model.kwargs,
            {
                "model": "claude-sonnet-test",
                "api_key": "anthropic-test",
                "max_tokens": 1024,
                "streaming": True,
                "stream_usage": True,
                "max_retries": 0,
                "model_kwargs": {"speed": "fast"},
                "betas": ["fast-mode-2026-02-01"],
                "effort": "medium",
                "thinking": {
                    "type": "adaptive",
                    "display": "summarized",
                },
            },
        )

    def test_anthropic_provider_maps_xhigh_reasoning_effort_to_max(self):
        fake_module = types.ModuleType("langchain_anthropic")
        fake_module.ChatAnthropic = FakeChatAnthropic
        with patch.dict(sys.modules, {"langchain_anthropic": fake_module}):
            model = build_chat_model(
                ChatModelProviderConfig(
                    provider="anthropic",
                    model="claude-opus-test",
                    reasoning_effort="xhigh",
                )
            )

        self.assertEqual(model.kwargs["effort"], "max")
        self.assertNotIn("model_kwargs", model.kwargs)
        self.assertEqual(
            model.kwargs["thinking"],
            {
                "type": "adaptive",
                "display": "summarized",
            },
        )

    def test_anthropic_provider_ignores_none_reasoning_effort_with_log(self):
        fake_module = types.ModuleType("langchain_anthropic")
        fake_module.ChatAnthropic = FakeChatAnthropic
        with (
            patch.dict(sys.modules, {"langchain_anthropic": fake_module}),
            self.assertLogs("dreamland.model_providers", level="INFO") as captured,
        ):
            model = build_chat_model(
                ChatModelProviderConfig(
                    provider="anthropic",
                    model="claude-sonnet-test",
                    reasoning_effort="none",
                )
            )

        self.assertIsInstance(model, FakeChatAnthropic)
        self.assertNotIn("effort", model.kwargs)
        self.assertNotIn("thinking", model.kwargs)
        self.assertNotIn("model_kwargs", model.kwargs)
        self.assertIn(
            "ignored_unsupported_model_control=reasoning_effort", captured.output[0]
        )

    def test_anthropic_provider_can_disable_prompt_cache(self):
        fake_module = types.ModuleType("langchain_anthropic")
        fake_module.ChatAnthropic = FakeChatAnthropic
        with patch.dict(sys.modules, {"langchain_anthropic": fake_module}):
            model = build_chat_model(
                ChatModelProviderConfig(
                    provider="anthropic",
                    model="claude-sonnet-test",
                    prompt_cache="off",
                )
            )

        self.assertIsInstance(model, FakeChatAnthropic)
        self.assertNotIn("model_kwargs", model.kwargs)

    def test_anthropic_provider_ignores_reasoning_summary_and_verbosity(self):
        fake_module = types.ModuleType("langchain_anthropic")
        fake_module.ChatAnthropic = FakeChatAnthropic
        with (
            patch.dict(sys.modules, {"langchain_anthropic": fake_module}),
            self.assertLogs("dreamland.model_providers", level="INFO") as captured,
        ):
            model = build_chat_model(
                ChatModelProviderConfig(
                    provider="anthropic",
                    model="claude-sonnet-test",
                    reasoning_summary="auto",
                    verbosity="high",
                )
            )

        self.assertIsInstance(model, FakeChatAnthropic)
        self.assertNotIn("reasoning_summary", model.kwargs)
        self.assertNotIn("verbosity", model.kwargs)
        logs = "\n".join(captured.output)
        self.assertIn("ignored_unsupported_model_control=reasoning_summary", logs)
        self.assertIn("ignored_unsupported_model_control=verbosity", logs)

    def test_unknown_provider_raises(self):
        with self.assertRaisesRegex(ValueError, "provider"):
            build_chat_model(
                ChatModelProviderConfig(
                    provider="unknown",
                    model="model",
                )
            )

    def test_retry_limit_is_opt_in_and_validated(self):
        with patch("langchain_openai.ChatOpenAI") as chat_openai:
            build_chat_model(
                ChatModelProviderConfig(provider="openai", model="gpt-test")
            )

        self.assertNotIn("max_retries", chat_openai.call_args.kwargs)
        for invalid in (True, -1, 1_000_001):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "max_retries"),
            ):
                build_chat_model(
                    ChatModelProviderConfig(
                        provider="openai",
                        model="gpt-test",
                        max_retries=invalid,
                    )
                )


class FakeChatXAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeChatAnthropic:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


if __name__ == "__main__":
    unittest.main()
