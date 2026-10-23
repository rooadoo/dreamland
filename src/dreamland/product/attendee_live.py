"""Credential-isolated construction for attendee-only live provider workers.

This module is inert at import.  Its frozen descriptors contain provider
configuration and one immutable authored revision, but never a provider API
key.  An explicit worker invocation revalidates and reads one owner-private
credential file, constructs one attendee-only executor, and does not compose
any listener or DREAMLAND authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from dreamland.model_providers import ModelReference, parse_model_reference
from dreamland.product.authored_artifacts import (
    AUTHORED_REVISION_KIND,
    MAX_AUTHORED_REVISION_BYTES,
    ImmutableAuthoredRevision,
)
from dreamland.product.launch_selection_runtime import (
    AUTHORED_RUNTIME_STATE_KIND,
    LaunchSelectionStateProfileCodec,
)
from dreamland.product.live_agent import (
    DEFAULT_PUBLICATION_CHUNK_BYTES,
    ExactThreadCheckpointReaper,
    ProductLiveAgentAdapter,
    ReservedMaximumUsageMeter,
)
from dreamland.product.runtime_executor import (
    ProductRuntimeCommandExecutor,
    ProductRuntimeState,
)

MAX_PROVIDER_CREDENTIAL_BYTES = 4 * 1024
MAX_MODEL_REFERENCE_BYTES = 512
MAX_PROMPT_FILENAME_BYTES = 128

_MAX_SAFE_INTEGER = (1 << 63) - 1
_AUTHORED_ORIGIN_SEED_CHECKPOINT = "attendee-live:authored-origin-seed"
_PROMPT_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.md$")
_FORBIDDEN_ENVIRONMENT_PREFIXES = (
    "ANTHROPIC_",
    "LANGCHAIN_",
    "LANGSMITH_",
    "OPENAI_",
    "XAI_",
)
_REASONING_EFFORT_VALUES = frozenset({"none", "low", "medium", "high", "xhigh"})
_REASONING_SUMMARY_VALUES = frozenset({"auto", "concise", "detailed"})
_VERBOSITY_VALUES = frozenset({"low", "medium", "high"})
_SPEED_VALUES = frozenset({"fast"})
_PROMPT_CACHE_VALUES = frozenset({"automatic", "off"})


@dataclass(frozen=True, slots=True)
class AttendeeLiveProviderSettings:
    """Finite, non-secret model configuration safe to send to a worker."""

    model: str
    system_prompt_name: str
    max_output_tokens: int
    reasoning_effort: str | None = None
    reasoning_summary: str | None = None
    verbosity: str | None = None
    speed: str | None = None
    prompt_cache: str | None = None

    def __post_init__(self) -> None:
        reference = _validate_model_reference(self.model)
        if self.model != f"{reference.provider}:{reference.model}":
            raise ValueError("attendee live model reference must be canonical")
        _validate_prompt_filename(self.system_prompt_name)
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 1 <= self.max_output_tokens <= _MAX_SAFE_INTEGER
        ):
            raise ValueError(
                "attendee live max_output_tokens must be a finite positive integer"
            )
        _validate_optional_choice(
            self.reasoning_effort,
            name="reasoning_effort",
            allowed=_REASONING_EFFORT_VALUES,
        )
        _validate_optional_choice(
            self.reasoning_summary,
            name="reasoning_summary",
            allowed=_REASONING_SUMMARY_VALUES,
        )
        _validate_optional_choice(
            self.verbosity,
            name="verbosity",
            allowed=_VERBOSITY_VALUES,
        )
        _validate_optional_choice(
            self.speed,
            name="speed",
            allowed=_SPEED_VALUES,
        )
        _validate_optional_choice(
            self.prompt_cache,
            name="prompt_cache",
            allowed=_PROMPT_CACHE_VALUES,
        )

    @property
    def model_reference(self) -> ModelReference:
        """Return the already-validated provider and model identity."""
        return parse_model_reference(self.model, name="attendee live model")


@dataclass(frozen=True, slots=True)
class AttendeeLiveWorkerFactory:
    """Picklable descriptor that lazily creates one child-local executor."""

    settings: AttendeeLiveProviderSettings
    credential_file: Path = field(repr=False)
    authored_revision: ImmutableAuthoredRevision = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.settings, AttendeeLiveProviderSettings):
            raise TypeError("attendee live provider settings are invalid")
        credential_file = validate_attendee_live_credential_file(self.credential_file)
        object.__setattr__(self, "credential_file", credential_file)
        validate_attendee_live_environment()
        # Fail before spawning if the purported immutable revision is not exact.
        _seeded_authored_codec(self.authored_revision)

    def __call__(self) -> ProductRuntimeCommandExecutor:
        """Construct one executor only after entry into the dedicated worker."""
        _scrub_and_reject_ambient_credentials(os.environ)
        api_key = _read_provider_credential(self.credential_file)
        state_codec = _seeded_authored_codec(self.authored_revision)

        # Optional provider and checkpoint dependencies are intentionally lazy.
        from langgraph.checkpoint.memory import InMemorySaver

        from dreamland.agent_core import LangGraphCommandAgent

        checkpointer = InMemorySaver()
        reference = self.settings.model_reference
        agent = LangGraphCommandAgent(
            provider=reference.provider,
            model_name=reference.model,
            system_prompt_name=self.settings.system_prompt_name,
            reasoning_effort=self.settings.reasoning_effort,
            reasoning_summary=self.settings.reasoning_summary,
            verbosity=self.settings.verbosity,
            speed=self.settings.speed,
            prompt_cache=self.settings.prompt_cache,
            max_output_tokens=self.settings.max_output_tokens,
            max_retries=0,
            api_key=api_key,
            langfuse_enabled=False,
            langfuse_public_key=None,
            langfuse_secret_key=None,
            langfuse_host=None,
            callbacks=(),
            checkpointer=checkpointer,
            redact_token_usage_command=True,
        )
        return ProductRuntimeCommandExecutor(
            agent=ProductLiveAgentAdapter(
                agent,
                publication_chunk_bytes=DEFAULT_PUBLICATION_CHUNK_BYTES,
            ),
            state_codec=state_codec,
            checkpoint_reaper=ExactThreadCheckpointReaper(checkpointer.delete_thread),
            usage_meter=ReservedMaximumUsageMeter(),
            clock_ms=_utc_ms,
            cancellation_poll_interval_ms=25,
            publication_chunk_bytes=DEFAULT_PUBLICATION_CHUNK_BYTES,
        )


class _ExactAuthoredStateProfileCodec(LaunchSelectionStateProfileCodec):
    """Keep one launch-selection codec confined to its pinned authored world."""

    def __init__(
        self,
        revision: ImmutableAuthoredRevision,
        document: dict[str, object],
    ) -> None:
        self._exact_revision_id = revision.revision_id
        self._exact_digest = revision.digest
        self._exact_canonical = revision.canonical_bytes()
        super().__init__()
        super().decode(
            document,
            checkpoint_namespace=_AUTHORED_ORIGIN_SEED_CHECKPOINT,
        )

    def decode(
        self,
        document: Mapping[str, object],
        *,
        checkpoint_namespace: str,
    ) -> ProductRuntimeState:
        kind = document.get("kind")
        if kind == AUTHORED_REVISION_KIND:
            try:
                canonical = _canonical_document_bytes(document)
            except (RecursionError, TypeError, UnicodeError, ValueError):
                raise ValueError("attendee live authored revision is invalid") from None
            if canonical != self._exact_canonical:
                raise ValueError("attendee live authored revision is not authorized")
        elif kind == AUTHORED_RUNTIME_STATE_KIND:
            self._require_exact_runtime_origin(document)
        else:
            raise ValueError("attendee live runtime source is not authorized")
        return super().decode(
            document,
            checkpoint_namespace=checkpoint_namespace,
        )

    def encode(self, state: ProductRuntimeState) -> Mapping[str, object]:
        document = super().encode(state)
        self._require_exact_runtime_origin(document)
        return document

    def _require_exact_runtime_origin(self, document: Mapping[str, object]) -> None:
        origin = document.get("origin")
        if not isinstance(origin, Mapping) or (
            origin.get("revision_id") != self._exact_revision_id
            or origin.get("digest") != self._exact_digest
        ):
            raise ValueError("attendee live authored runtime origin is not authorized")


def validate_attendee_live_environment(
    environment: Mapping[str, str] | None = None,
) -> None:
    """Reject ambient provider and Langfuse credentials in the parent."""
    candidate = os.environ if environment is None else environment
    if any(_forbidden_environment_name(name) for name in candidate):
        raise ValueError(
            "attendee live workers refuse ambient provider or tracing environment"
        )


def validate_attendee_live_credential_file(path: Path | str) -> Path:
    """Validate credential metadata without reading credential contents."""
    normalized = _absolute_path(path)
    descriptor = _open_validated_credential(normalized)
    os.close(descriptor)
    return normalized


def _validate_model_reference(value: object) -> ModelReference:
    if not isinstance(value, str):
        raise TypeError("attendee live model must be a provider:model string")
    if (
        not value
        or len(value.encode("utf-8")) > MAX_MODEL_REFERENCE_BYTES
        or not value.isprintable()
        or any(character.isspace() for character in value)
    ):
        raise ValueError("attendee live model reference is invalid")
    return parse_model_reference(value, name="attendee live model")


def _validate_prompt_filename(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("attendee live system prompt name must be a string")
    if (
        not value
        or len(value.encode("utf-8")) > MAX_PROMPT_FILENAME_BYTES
        or _PROMPT_FILENAME_PATTERN.fullmatch(value) is None
        or value in {".", ".."}
    ):
        raise ValueError("attendee live system prompt name is unsafe")
    resource = files("dreamland.prompts").joinpath(value)
    if not resource.is_file():
        raise ValueError("attendee live system prompt is not bundled")


def _validate_optional_choice(
    value: object,
    *,
    name: str,
    allowed: frozenset[str],
) -> None:
    if value is not None and (not isinstance(value, str) or value not in allowed):
        raise ValueError(f"attendee live {name} is invalid")


def _absolute_path(value: Path | str) -> Path:
    if not isinstance(value, (str, Path)):
        raise TypeError("attendee provider credential path is invalid")
    try:
        return Path(os.path.abspath(os.fspath(value)))
    except (OSError, TypeError, ValueError):
        raise ValueError("attendee provider credential path is invalid") from None


def _open_validated_credential(path: Path) -> int:
    try:
        path_metadata = os.lstat(path)
    except OSError:
        raise ValueError("attendee provider credential file is unavailable") from None
    if stat.S_ISLNK(path_metadata.st_mode):
        raise ValueError("attendee provider credential file is unsafe")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError("attendee provider credential file is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            metadata.st_dev != path_metadata.st_dev
            or metadata.st_ino != path_metadata.st_ino
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not 1 <= metadata.st_size <= MAX_PROVIDER_CREDENTIAL_BYTES
        ):
            raise ValueError("attendee provider credential file is unsafe")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_provider_credential(path: Path) -> str:
    descriptor = _open_validated_credential(path)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_PROVIDER_CREDENTIAL_BYTES + 1)
    except OSError:
        raise ValueError("attendee provider credential file is unreadable") from None
    finally:
        os.close(descriptor)
    if not 1 <= len(data) <= MAX_PROVIDER_CREDENTIAL_BYTES:
        raise ValueError("attendee provider credential file is unsafe")
    try:
        credential = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ValueError("attendee provider credential is not valid UTF-8") from None
    if not credential or any(character.isspace() for character in credential):
        raise ValueError("attendee provider credential must contain one API key")
    return credential


def _seeded_authored_codec(
    revision: ImmutableAuthoredRevision,
) -> LaunchSelectionStateProfileCodec:
    document = _validated_authored_revision_document(revision)
    return _ExactAuthoredStateProfileCodec(revision, document)


def _validated_authored_revision_document(
    revision: ImmutableAuthoredRevision,
) -> dict[str, object]:
    if type(revision) is not ImmutableAuthoredRevision:
        raise TypeError("attendee live authored revision is invalid")
    data = revision.canonical_bytes()
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_AUTHORED_REVISION_BYTES:
        raise ValueError("attendee live authored revision is invalid")
    digest_hex = hashlib.sha256(data).hexdigest()
    if (
        revision.revision_id != f"world-revision:sha256:{digest_hex}"
        or revision.digest != f"sha256:{digest_hex}"
    ):
        raise ValueError("attendee live authored revision identity is invalid")
    try:
        document = revision.allocate_document()
        canonical = _canonical_document_bytes(document)
    except (RecursionError, TypeError, UnicodeError, ValueError):
        raise ValueError("attendee live authored revision is invalid") from None
    if canonical != data:
        raise ValueError("attendee live authored revision is not canonical")
    return document


def _canonical_document_bytes(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(document),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _forbidden_environment_name(name: object) -> bool:
    if not isinstance(name, str):
        return True
    normalized = name.upper()
    return normalized.startswith(_FORBIDDEN_ENVIRONMENT_PREFIXES) or (
        normalized == "LANGFUSE" or normalized.startswith("LANGFUSE_")
    )


def _scrub_and_reject_ambient_credentials(
    environment: MutableMapping[str, str],
) -> None:
    forbidden = [name for name in environment if _forbidden_environment_name(name)]
    for name in forbidden:
        environment.pop(name, None)
    if forbidden:
        raise ValueError(
            "attendee live workers refuse ambient provider or tracing environment"
        )


def _utc_ms() -> int:
    return time.time_ns() // 1_000_000


__all__ = [
    "AttendeeLiveProviderSettings",
    "AttendeeLiveWorkerFactory",
    "MAX_MODEL_REFERENCE_BYTES",
    "MAX_PROMPT_FILENAME_BYTES",
    "MAX_PROVIDER_CREDENTIAL_BYTES",
    "validate_attendee_live_credential_file",
    "validate_attendee_live_environment",
]
