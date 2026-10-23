from __future__ import annotations

import dataclasses
import hashlib
import json
import multiprocessing
import os
import pickle
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dreamland.models import CommandResult, CommandStateUpdate, CommandStreamResult
from dreamland.product.attendee_live import (
    MAX_PROVIDER_CREDENTIAL_BYTES,
    AttendeeLiveProviderSettings,
    AttendeeLiveWorkerFactory,
    validate_attendee_live_credential_file,
    validate_attendee_live_environment,
)
from dreamland.product.attendee_provider import (
    AttendeeProviderLifecycle,
    AttendeeProviderPool,
    AttendeeProviderPoolLimits,
)
from dreamland.product.authored_artifacts import (
    AUTHORED_REVISION_KIND,
    ImmutableAuthoredRevision,
    ImmutableAuthoredStore,
    authored_revision_digest,
)
from dreamland.product.execution import (
    ProductExecutionOutcome,
    ProductExecutionRequest,
    ProductOutputStream,
)
from dreamland.product.launch_selection_runtime import AUTHORED_RUNTIME_STATE_KIND
from dreamland.product.live_agent import (
    DEFAULT_PUBLICATION_CHUNK_BYTES,
    ExactThreadCheckpointReaper,
    ProductLiveAgentAdapter,
    ReservedMaximumUsageMeter,
)
from dreamland.product.runtime_executor import ProductRuntimeCommandExecutor
from dreamland.session_state import apply_command_state_update
from dreamland.state_profiles import StateProfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@dataclasses.dataclass(frozen=True, slots=True)
class _NoNetworkLiveFactoryProbe:
    """Invoke the exact production factory with only its model constructor faked."""

    factory: AttendeeLiveWorkerFactory

    def __call__(self) -> ProductRuntimeCommandExecutor:
        with patch(
            "dreamland.agent_core.LangGraphCommandAgent",
            return_value=MagicMock(),
        ):
            return self.factory()


class ProductAttendeeLiveBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.credential = self.root / "attendee-provider.key"
        self._write_credential("dedicated-provider-secret\n")
        profile = _authored_profile()
        self.revision = ImmutableAuthoredStore(self.root / "authored-revisions").save(
            profile,
            expected_digest=authored_revision_digest(profile),
        )
        self.settings = _settings()

    def test_import_and_descriptor_construction_are_provider_free(self) -> None:
        script = r"""
import json, pathlib, sys, threading
from unittest.mock import patch
import dreamland.model_providers as providers
before = [(thread.ident, thread.name) for thread in threading.enumerate()]
with patch.object(providers, "build_chat_model") as build:
    import dreamland.product.attendee_live
after = [(thread.ident, thread.name) for thread in threading.enumerate()]
print(json.dumps({
    "provider_calls": build.call_count,
    "provider_clients_loaded": any(
        name in sys.modules
        for name in ("langchain_openai", "langchain_xai", "langchain_anthropic")
    ),
    "threads_unchanged": before == after,
}))
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            cwd=REPOSITORY_ROOT,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
        )
        self.assertEqual(
            json.loads(result.stdout),
            {
                "provider_calls": 0,
                "provider_clients_loaded": False,
                "threads_unchanged": True,
            },
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("dreamland.model_providers.build_chat_model") as build,
        ):
            factory = self._factory()
        self.assertEqual(build.call_count, 0)
        self.assertIsInstance(factory, AttendeeLiveWorkerFactory)

    def test_settings_are_frozen_picklable_bounded_and_non_secret(self) -> None:
        settings = _settings(
            reasoning_effort="high",
            reasoning_summary="detailed",
            verbosity="low",
            speed="fast",
            prompt_cache="automatic",
        )
        self.assertEqual(pickle.loads(pickle.dumps(settings)), settings)
        self.assertEqual(settings.model_reference.provider, "openai")
        self.assertEqual(settings.model_reference.model, "gpt-attendee-test")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.model = "xai:changed"  # type: ignore[misc]

        invalid = (
            {"model": "gpt-without-provider"},
            {"model": "openai:"},
            {"model": " openai:gpt-attendee-test"},
            {"model": "openai:model with spaces"},
            {"system_prompt_name": "../system.md"},
            {"system_prompt_name": "not-bundled.md"},
            {"system_prompt_name": "system.txt"},
            {"max_output_tokens": True},
            {"max_output_tokens": 0},
            {"max_output_tokens": -1},
            {"max_output_tokens": 1 << 63},
            {"reasoning_effort": "extreme"},
            {"reasoning_summary": "full"},
            {"verbosity": "verbose"},
            {"speed": "turbo"},
            {"prompt_cache": "forever"},
        )
        for override in invalid:
            with (
                self.subTest(override=override),
                self.assertRaises((TypeError, ValueError)),
            ):
                _settings(**override)

    def test_parent_validates_metadata_without_reading_credential(self) -> None:
        with patch("dreamland.product.attendee_live.os.fdopen") as fdopen:
            normalized = validate_attendee_live_credential_file(self.credential)
        self.assertEqual(normalized, self.credential.absolute())
        fdopen.assert_not_called()

        unsafe_mode = self.root / "group-readable.key"
        unsafe_mode.write_text("secret", encoding="utf-8")
        unsafe_mode.chmod(0o640)
        empty = self.root / "empty.key"
        empty.touch(mode=0o600)
        oversized = self.root / "oversized.key"
        oversized.write_bytes(b"x" * (MAX_PROVIDER_CREDENTIAL_BYTES + 1))
        oversized.chmod(0o600)
        directory = self.root / "directory.key"
        directory.mkdir(mode=0o600)
        symlink = self.root / "symlink.key"
        symlink.symlink_to(self.credential)

        for path in (unsafe_mode, empty, oversized, directory, symlink):
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                validate_attendee_live_credential_file(path)

        with (
            patch(
                "dreamland.product.attendee_live.os.geteuid",
                return_value=os.geteuid() + 1,
            ),
            self.assertRaises(ValueError),
        ):
            validate_attendee_live_credential_file(self.credential)

    def test_parent_refuses_generic_provider_and_every_langfuse_environment(
        self,
    ) -> None:
        forbidden = (
            "OPENAI_API_KEY",
            "openai_base_url",
            "XAI_API_KEY",
            "xai_future_setting",
            "ANTHROPIC_API_KEY",
            "anthropic_custom_endpoint",
            "LANGCHAIN_TRACING_V2",
            "langchain_api_key",
            "LANGSMITH_API_KEY",
            "langsmith_endpoint",
            "LANGFUSE",
            "LANGFUSE_ENABLED",
            "LANGFUSE_ARBITRARY_FUTURE_SETTING",
            "langfuse_secret_key",
        )
        for name in forbidden:
            with (
                self.subTest(name=name),
                patch.dict(os.environ, {name: "must-not-be-used"}, clear=True),
                self.assertRaises(ValueError),
            ):
                self._factory()
        validate_attendee_live_environment({"UNRELATED_SETTING": "safe"})
        validate_attendee_live_environment({"OPENAI": "not-a-prefixed-setting"})

    def test_child_rechecks_scrubs_and_rejects_ambient_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            factory = self._factory()
            os.environ.update(
                {
                    "OPENAI_API_KEY": "ambient-provider-secret",
                    "anthropic_base_url": "ambient-provider-route",
                    "XAI_FUTURE_SETTING": "ambient-provider-setting",
                    "LANGCHAIN_TRACING_V2": "true",
                    "langsmith_api_key": "ambient-tracing-secret",
                    "LANGFUSE_NEW_SECRET": "ambient-observability-secret",
                }
            )
            with self.assertRaises(ValueError):
                factory()
            self.assertEqual(dict(os.environ), {})

    def test_factory_repr_pickle_and_errors_do_not_disclose_secret_material(
        self,
    ) -> None:
        secret_path = self.root / "path-containing-top-secret-material.key"
        secret_path.write_text("credential-top-secret-material\n", encoding="utf-8")
        secret_path.chmod(0o600)
        with patch.dict(os.environ, {}, clear=True):
            factory = AttendeeLiveWorkerFactory(
                settings=self.settings,
                credential_file=secret_path,
                authored_revision=self.revision,
            )
            copied = pickle.loads(pickle.dumps(factory))
        rendered = repr(factory)
        self.assertNotIn(str(secret_path), rendered)
        self.assertNotIn("top-secret-material", rendered)
        self.assertNotIn(self.revision.revision_id, rendered)
        self.assertNotIn(self.revision.digest, rendered)
        self.assertEqual(copied, factory)
        self.assertTrue(factory.credential_file.is_absolute())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            factory.credential_file = self.root  # type: ignore[misc]

        missing = self.root / "cli-secret-credential-path.key"
        try:
            validate_attendee_live_credential_file(missing)
        except ValueError as error:
            rendered_error = "".join(traceback.format_exception(error))
        else:  # pragma: no cover - the path is deliberately absent.
            self.fail("missing credential path was accepted")
        self.assertNotIn(str(missing), rendered_error)
        self.assertNotIn("cli-secret-credential-path", rendered_error)

    def test_exact_authored_revision_is_validated_before_worker_start(self) -> None:
        mismatched = ImmutableAuthoredRevision(
            "world-revision:sha256:" + "0" * 64,
            "sha256:" + "0" * 64,
            self.revision.canonical_bytes(),
        )
        malformed_document = {"kind": AUTHORED_REVISION_KIND, "schema_revision": "99"}
        malformed_data = _canonical_bytes(malformed_document)
        malformed_digest = hashlib.sha256(malformed_data).hexdigest()
        malformed = ImmutableAuthoredRevision(
            f"world-revision:sha256:{malformed_digest}",
            f"sha256:{malformed_digest}",
            malformed_data,
        )
        with patch.dict(os.environ, {}, clear=True):
            for revision in (mismatched, malformed, object()):
                with (
                    self.subTest(revision=type(revision)),
                    self.assertRaises((TypeError, ValueError)),
                ):
                    AttendeeLiveWorkerFactory(
                        settings=self.settings,
                        credential_file=self.credential,
                        authored_revision=revision,  # type: ignore[arg-type]
                    )

    def test_worker_lazy_construction_is_attendee_only_and_redacted(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            factory = self._factory()
            checkpointer = SimpleNamespace(delete_thread=MagicMock())
            agent = MagicMock()
            with (
                patch(
                    "langgraph.checkpoint.memory.InMemorySaver",
                    return_value=checkpointer,
                ) as saver,
                patch(
                    "dreamland.agent_core.LangGraphCommandAgent",
                    return_value=agent,
                ) as command_agent,
            ):
                executor = factory()

        saver.assert_called_once_with()
        command_agent.assert_called_once_with(
            provider="openai",
            model_name="gpt-attendee-test",
            system_prompt_name="system.md",
            reasoning_effort="low",
            reasoning_summary="detailed",
            verbosity="low",
            speed=None,
            prompt_cache="off",
            max_output_tokens=2048,
            max_retries=0,
            api_key="dedicated-provider-secret",
            langfuse_enabled=False,
            langfuse_public_key=None,
            langfuse_secret_key=None,
            langfuse_host=None,
            callbacks=(),
            checkpointer=checkpointer,
            redact_token_usage_command=True,
        )
        self.assertIsInstance(executor, ProductRuntimeCommandExecutor)
        self.assertIsInstance(executor._agent, ProductLiveAgentAdapter)
        self.assertEqual(
            executor._agent._publication_chunk_bytes,
            DEFAULT_PUBLICATION_CHUNK_BYTES,
        )
        self.assertEqual(
            executor._publication_chunk_bytes,
            DEFAULT_PUBLICATION_CHUNK_BYTES,
        )
        self.assertIsInstance(
            executor._checkpoint_reaper,
            ExactThreadCheckpointReaper,
        )
        self.assertIsInstance(executor._usage_meter, ReservedMaximumUsageMeter)
        before_ms = time.time_ns() // 1_000_000
        clock_ms = executor._clock_ms()
        after_ms = time.time_ns() // 1_000_000
        self.assertLessEqual(before_ms, clock_ms)
        self.assertLessEqual(clock_ms, after_ms)

    def test_worker_bounds_terminal_only_utf8_stdout_and_stderr(self) -> None:
        complete_stdout = "builtin-output-" + ("\N{ROCKET}" * 1_500)
        complete_stderr = "structured-error-" + ("\N{GLOBE WITH MERIDIANS}" * 1_500)
        agent = MagicMock()
        agent.stream_respond.return_value = iter(
            (
                CommandStreamResult(
                    CommandResult(stdout=complete_stdout, stderr=complete_stderr)
                ),
            )
        )
        checkpointer = SimpleNamespace(delete_thread=MagicMock())
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "langgraph.checkpoint.memory.InMemorySaver",
                return_value=checkpointer,
            ),
            patch(
                "dreamland.agent_core.LangGraphCommandAgent",
                return_value=agent,
            ),
        ):
            executor = self._factory()()

        chunks = []
        result = executor.execute(
            ProductExecutionRequest(
                service_epoch="epoch:attendee_live_test_0001",
                session_id="session:attendee_live_test_0001",
                command_id="command:attendee_live_test_0001",
                slot_generation=1,
                canonical_command="show terminal output",
                provisional_document=self.revision.allocate_document(),
                checkpoint_namespace="checkpoint:attendee_live_test_0001",
                deadline_ms=(time.time_ns() // 1_000_000) + 60_000,
                max_tokens=2_048,
                max_spend_units=1_000,
                max_attempts=1,
                cancelled=lambda: False,
            ),
            chunks.append,
        )

        self.assertEqual(result.outcome, ProductExecutionOutcome.COMPLETED)
        self.assertTrue(chunks)
        self.assertTrue(
            all(
                len(chunk.text.encode("utf-8")) <= DEFAULT_PUBLICATION_CHUNK_BYTES
                for chunk in chunks
            )
        )
        self.assertEqual(
            "".join(
                chunk.text
                for chunk in chunks
                if chunk.stream is ProductOutputStream.STDOUT
            ),
            complete_stdout,
        )
        self.assertEqual(
            "".join(
                chunk.text
                for chunk in chunks
                if chunk.stream is ProductOutputStream.STDERR
            ),
            complete_stderr,
        )
        checkpointer.delete_thread.assert_called_once_with(
            "checkpoint:attendee_live_test_0001"
        )

    def test_exact_factory_starts_and_shuts_down_in_real_spawn_worker(self) -> None:
        context = multiprocessing.get_context("spawn")
        limits = AttendeeProviderPoolLimits(
            worker_capacity=1,
            startup_timeout_ms=5_000,
            shutdown_timeout_ms=2_000,
            terminate_grace_ms=100,
        )
        with patch.dict(os.environ, {}, clear=True):
            factory = self._factory()
            pool = AttendeeProviderPool(
                _NoNetworkLiveFactoryProbe(factory),
                limits=limits,
                process_context=context,
            )
            started = pool.start()
            closed = pool.shutdown()

        self.assertEqual(started.lifecycle, AttendeeProviderLifecycle.READY)
        self.assertEqual(started.ready_workers, 1)
        self.assertEqual(closed.lifecycle, AttendeeProviderLifecycle.CLOSED)
        self.assertEqual(closed.ready_workers, 0)

    def test_each_worker_is_preseeded_for_authored_runtime_reassignment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            factory = self._factory()
            checkpointers = [
                SimpleNamespace(delete_thread=MagicMock()),
                SimpleNamespace(delete_thread=MagicMock()),
            ]
            agents = [MagicMock(), MagicMock()]
            with (
                patch(
                    "langgraph.checkpoint.memory.InMemorySaver",
                    side_effect=checkpointers,
                ),
                patch(
                    "dreamland.agent_core.LangGraphCommandAgent",
                    side_effect=agents,
                ),
            ):
                first = factory()
                second = factory()

        self.assertIsNot(first._state_codec, second._state_codec)
        first_state = first._state_codec.decode(
            self.revision.allocate_document(),
            checkpoint_namespace="attendee-live:first-assignment",
        )
        apply_command_state_update(
            first_state.session,
            CommandStateUpdate(cwd="/srv/reassigned-attendee"),
        )
        runtime_document = first._state_codec.encode(first_state)
        self.assertEqual(runtime_document["kind"], AUTHORED_RUNTIME_STATE_KIND)

        second_state = second._state_codec.decode(
            runtime_document,
            checkpoint_namespace="attendee-live:second-assignment",
        )
        self.assertEqual(second_state.session.cwd, "/srv/reassigned-attendee")

        other_profile = _authored_profile(hostname="different-authored-host")
        other_revision = ImmutableAuthoredStore(
            self.root / "other-authored-revisions"
        ).save(
            other_profile,
            expected_digest=authored_revision_digest(other_profile),
        )
        unauthorized_documents = (
            other_revision.allocate_document(),
            {
                "kind": "dreamland.world_forge_revision",
                "schema_revision": "1",
                "profile": {},
            },
        )
        for document in unauthorized_documents:
            with self.subTest(kind=document["kind"]), self.assertRaises(ValueError):
                second._state_codec.decode(
                    document,
                    checkpoint_namespace="attendee-live:unauthorized-assignment",
                )

    def test_credential_contents_are_checked_only_inside_worker_call(self) -> None:
        invalid_values = ("   \n", "two\nkeys\n", None)
        for index, value in enumerate(invalid_values):
            path = self.root / f"invalid-{index}.key"
            if value is None:
                path.write_bytes(b"\xff\xfe")
            else:
                path.write_text(value, encoding="utf-8")
            path.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True):
                factory = AttendeeLiveWorkerFactory(
                    settings=self.settings,
                    credential_file=path,
                    authored_revision=self.revision,
                )
                with self.assertRaises(ValueError):
                    factory()

    def test_module_has_no_cross_product_composition_imports(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "dreamland" / "product" / "attendee_live.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "load_settings",
            "StateProfileStore",
            "dreamland.admin_api",
            "dreamland.mcp",
            "dreamland.product.stage",
            "dreamland.product.world_access",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def _factory(self) -> AttendeeLiveWorkerFactory:
        return AttendeeLiveWorkerFactory(
            settings=self.settings,
            credential_file=self.credential,
            authored_revision=self.revision,
        )

    def _write_credential(self, value: str) -> None:
        self.credential.write_text(value, encoding="utf-8")
        self.credential.chmod(0o600)


def _settings(**overrides: object) -> AttendeeLiveProviderSettings:
    values: dict[str, object] = {
        "model": "openai:gpt-attendee-test",
        "system_prompt_name": "system.md",
        "max_output_tokens": 2048,
        "reasoning_effort": "low",
        "reasoning_summary": "detailed",
        "verbosity": "low",
        "speed": None,
        "prompt_cache": "off",
    }
    values.update(overrides)
    return AttendeeLiveProviderSettings(**values)  # type: ignore[arg-type]


def _authored_profile(hostname: str = "attendee-live-host") -> StateProfile:
    return StateProfile(
        name="configured-attendee-live",
        level="world",
        created_at="2026-09-01T00:00:00+00:00",
        state={
            "world_state": {
                "name": "Attendee Live World",
                "entry_host": hostname,
                "active_host": hostname,
                "purpose": {"theme": "Dedicated attendee live worker test"},
                "hosts_by_name": {
                    hostname: {
                        "login_banner": "Test console\n",
                        "hostname": hostname,
                        "purpose": {"theme": "Attendee-only command execution"},
                        "shell": {
                            "default": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["bash"],
                            }
                        },
                    }
                },
            }
        },
    )


def _canonical_bytes(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
