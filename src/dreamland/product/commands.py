"""Pure command validation for the integration-only product runtime.

``product-single-v1`` is intentionally separate from DREAMLAND's SSH command
parsing.  It validates and classifies one product command without allocating a
command identity, claiming a session slot, reserving capacity, or touching any
runtime state.  Consumers must call it before performing those operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import NoReturn

PRODUCT_COMMAND_LANGUAGE = "product-single-v1"
MAX_CANONICAL_COMMAND_UTF8_BYTES = 8 * 1024
MAX_CANONICAL_COMMAND_UNICODE_SCALARS = 4 * 1024

_ASCII_TRIM = frozenset({" ", "\t"})
_UNQUOTED_CONTROL_OPERATORS = frozenset({";", "&", "|", "<", ">", "(", ")"})
_CONTROL_WORDS = frozenset(
    {
        "!",
        "[[",
        "]]",
        "case",
        "coproc",
        "do",
        "done",
        "elif",
        "else",
        "esac",
        "fi",
        "for",
        "function",
        "if",
        "in",
        "select",
        "then",
        "time",
        "until",
        "while",
        "{",
        "}",
    }
)


class ProductCommandErrorCode(StrEnum):
    """Stable safe-error categories exposed to the product core."""

    INVALID_COMMAND_ENCODING = "invalid_command_encoding"
    INVALID_COMMAND_TEXT = "invalid_command_text"
    INVALID_COMMAND_SYNTAX = "invalid_command_syntax"
    COMMAND_TOO_LARGE = "command_too_large"
    COMPOUND_SYNTAX_UNSUPPORTED = "compound_syntax_unsupported"


class ProductCommandErrorReason(StrEnum):
    """Stable internal details for deterministic transport mapping and tests."""

    UNSUPPORTED_INPUT_TYPE = "unsupported_input_type"
    MALFORMED_UTF8 = "malformed_utf8"
    UNPAIRED_SURROGATE = "unpaired_surrogate"
    NUL_CHARACTER = "nul_character"
    LINE_BREAK = "line_break"
    EMPTY = "empty"
    UNICODE_SCALAR_LIMIT = "unicode_scalar_limit"
    UTF8_BYTE_LIMIT = "utf8_byte_limit"
    TRAILING_ESCAPE = "trailing_escape"
    UNCLOSED_SINGLE_QUOTE = "unclosed_single_quote"
    UNCLOSED_DOUBLE_QUOTE = "unclosed_double_quote"
    CONTROL_OPERATOR = "control_operator"
    CONTROL_WORD = "control_word"
    COMMAND_SUBSTITUTION = "command_substitution"


@dataclass(frozen=True, slots=True)
class ProductCommandErrorDetails:
    """Bounded details that a product-core adapter can map without raw input."""

    code: ProductCommandErrorCode
    reason: ProductCommandErrorReason
    message: str
    status: int = 422
    retry: str = "none"
    retry_after_ms: None = None


class ProductCommandError(ValueError):
    """A deterministic product-command validation or classification failure."""

    def __init__(self, details: ProductCommandErrorDetails) -> None:
        super().__init__(details.message)
        self.details = details
        self.code = details.code
        self.reason = details.reason
        self.message = details.message
        self.status = details.status
        self.retry = details.retry
        self.retry_after_ms = details.retry_after_ms


@dataclass(frozen=True, slots=True)
class CanonicalProductCommand:
    """One accepted command and the exact bytes used by later core hashing."""

    canonical_text: str = field(repr=False)
    canonical_utf8: bytes = field(repr=False)
    unicode_scalar_count: int
    utf8_byte_count: int
    language: str = field(default=PRODUCT_COMMAND_LANGUAGE, init=False)


def decode_product_command_utf8(command: bytes) -> str:
    """Decode one byte string strictly, returning a safe typed error on failure."""
    if not isinstance(command, bytes):
        _reject(
            ProductCommandErrorCode.INVALID_COMMAND_ENCODING,
            ProductCommandErrorReason.UNSUPPORTED_INPUT_TYPE,
            "Command input must be text or an immutable UTF-8 byte string.",
        )
    try:
        return command.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _reject(
            ProductCommandErrorCode.INVALID_COMMAND_ENCODING,
            ProductCommandErrorReason.MALFORMED_UTF8,
            "Command input must be well-formed UTF-8.",
        )


def classify_product_command(command: str | bytes) -> CanonicalProductCommand:
    """Canonicalize and accept one ``product-single-v1`` command.

    Bytes are decoded exactly once with strict UTF-8.  Text is treated as the
    already-decoded JSON string.  The returned text differs only by removal of
    leading and trailing ASCII SP/HT; Unicode normalization is never applied.
    """
    if isinstance(command, bytes):
        text = decode_product_command_utf8(command)
    elif isinstance(command, str):
        text = command
    else:
        _reject(
            ProductCommandErrorCode.INVALID_COMMAND_ENCODING,
            ProductCommandErrorReason.UNSUPPORTED_INPUT_TYPE,
            "Command input must be text or an immutable UTF-8 byte string.",
        )

    _validate_decoded_text(text)
    canonical_text = _trim_ascii_space_and_tab(text)
    if not canonical_text:
        _reject(
            ProductCommandErrorCode.INVALID_COMMAND_TEXT,
            ProductCommandErrorReason.EMPTY,
            "Command must not be empty after ASCII space and tab trimming.",
        )

    scalar_count = len(canonical_text)
    if scalar_count > MAX_CANONICAL_COMMAND_UNICODE_SCALARS:
        _reject(
            ProductCommandErrorCode.COMMAND_TOO_LARGE,
            ProductCommandErrorReason.UNICODE_SCALAR_LIMIT,
            "Command exceeds the Unicode scalar limit.",
        )

    canonical_utf8 = canonical_text.encode("utf-8", errors="strict")
    utf8_byte_count = len(canonical_utf8)
    if utf8_byte_count > MAX_CANONICAL_COMMAND_UTF8_BYTES:
        _reject(
            ProductCommandErrorCode.COMMAND_TOO_LARGE,
            ProductCommandErrorReason.UTF8_BYTE_LIMIT,
            "Command exceeds the UTF-8 byte limit.",
        )

    _classify_single_command(canonical_text)
    return CanonicalProductCommand(
        canonical_text=canonical_text,
        canonical_utf8=canonical_utf8,
        unicode_scalar_count=scalar_count,
        utf8_byte_count=utf8_byte_count,
    )


def _validate_decoded_text(text: str) -> None:
    for character in text:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            _reject(
                ProductCommandErrorCode.INVALID_COMMAND_ENCODING,
                ProductCommandErrorReason.UNPAIRED_SURROGATE,
                "Command text must not contain surrogate code points.",
            )
        if character == "\x00":
            _reject(
                ProductCommandErrorCode.INVALID_COMMAND_TEXT,
                ProductCommandErrorReason.NUL_CHARACTER,
                "Command text must not contain NUL.",
            )
        if character in {"\r", "\n"}:
            _reject(
                ProductCommandErrorCode.INVALID_COMMAND_TEXT,
                ProductCommandErrorReason.LINE_BREAK,
                "Command text must not contain CR or LF.",
            )


def _trim_ascii_space_and_tab(text: str) -> str:
    start = 0
    end = len(text)
    while start < end and text[start] in _ASCII_TRIM:
        start += 1
    while end > start and text[end - 1] in _ASCII_TRIM:
        end -= 1
    if start == 0 and end == len(text):
        return text
    return text[start:end]


def _classify_single_command(text: str) -> None:
    quote: str | None = None
    compound_reason: ProductCommandErrorReason | None = None
    first_word: list[str] = []
    first_word_protected = False
    first_word_finished = False
    index = 0

    while index < len(text):
        character = text[index]

        if quote == "'":
            if character == "'":
                quote = None
            elif not first_word_finished:
                first_word.append(character)
            index += 1
            continue

        if quote == '"':
            if character == '"':
                quote = None
                index += 1
                continue
            if character == "\\":
                if index + 1 >= len(text):
                    _reject_invalid_syntax(ProductCommandErrorReason.TRAILING_ESCAPE)
                if not first_word_finished:
                    first_word.append(text[index + 1])
                    first_word_protected = True
                index += 2
                continue
            if character == "`" or (
                character == "$" and index + 1 < len(text) and text[index + 1] == "("
            ):
                compound_reason = compound_reason or (
                    ProductCommandErrorReason.COMMAND_SUBSTITUTION
                )
            if not first_word_finished:
                first_word.append(character)
            index += 1
            continue

        if character in _ASCII_TRIM:
            if not first_word_finished and first_word:
                if _is_control_word(first_word, first_word_protected):
                    compound_reason = (
                        compound_reason or ProductCommandErrorReason.CONTROL_WORD
                    )
                first_word_finished = True
            index += 1
            continue
        if character == "'" or character == '"':
            quote = character
            if not first_word_finished:
                first_word_protected = True
            index += 1
            continue
        if character == "\\":
            if index + 1 >= len(text):
                _reject_invalid_syntax(ProductCommandErrorReason.TRAILING_ESCAPE)
            if not first_word_finished:
                first_word.append(text[index + 1])
                first_word_protected = True
            index += 2
            continue
        if character == "`" or (
            character == "$" and index + 1 < len(text) and text[index + 1] == "("
        ):
            compound_reason = (
                compound_reason or ProductCommandErrorReason.COMMAND_SUBSTITUTION
            )
        if character in _UNQUOTED_CONTROL_OPERATORS:
            compound_reason = (
                compound_reason or ProductCommandErrorReason.CONTROL_OPERATOR
            )
        if not first_word_finished:
            first_word.append(character)
        index += 1

    if quote == "'":
        _reject_invalid_syntax(ProductCommandErrorReason.UNCLOSED_SINGLE_QUOTE)
    if quote == '"':
        _reject_invalid_syntax(ProductCommandErrorReason.UNCLOSED_DOUBLE_QUOTE)
    if not first_word_finished and _is_control_word(first_word, first_word_protected):
        compound_reason = compound_reason or ProductCommandErrorReason.CONTROL_WORD
    if compound_reason is not None:
        _reject_compound(compound_reason)


def _is_control_word(word: list[str], protected: bool) -> bool:
    return not protected and "".join(word) in _CONTROL_WORDS


def _reject_invalid_syntax(reason: ProductCommandErrorReason) -> NoReturn:
    _reject(
        ProductCommandErrorCode.INVALID_COMMAND_SYNTAX,
        reason,
        "Command quoting or escaping is incomplete.",
    )


def _reject_compound(reason: ProductCommandErrorReason) -> NoReturn:
    _reject(
        ProductCommandErrorCode.COMPOUND_SYNTAX_UNSUPPORTED,
        reason,
        "Browser sessions accept one command without shell-control syntax. "
        "Use SSH for pipelines, redirection, backgrounding, or substitution.",
    )


def _reject(
    code: ProductCommandErrorCode,
    reason: ProductCommandErrorReason,
    message: str,
) -> NoReturn:
    raise ProductCommandError(
        ProductCommandErrorDetails(code=code, reason=reason, message=message)
    )
