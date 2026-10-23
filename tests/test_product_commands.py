import unicodedata
import unittest

from dreamland.product.commands import (
    MAX_CANONICAL_COMMAND_UNICODE_SCALARS,
    MAX_CANONICAL_COMMAND_UTF8_BYTES,
    PRODUCT_COMMAND_LANGUAGE,
    ProductCommandError,
    ProductCommandErrorCode,
    ProductCommandErrorReason,
    classify_product_command,
    decode_product_command_utf8,
)


class ProductCommandCanonicalizationTests(unittest.TestCase):
    def test_accepts_text_and_immutable_utf8_bytes(self) -> None:
        expected = classify_product_command("printf 'café'")
        decoded = classify_product_command("printf 'café'".encode())

        self.assertEqual(decoded, expected)
        self.assertEqual(decoded.language, PRODUCT_COMMAND_LANGUAGE)
        self.assertEqual(decoded.canonical_text, "printf 'café'")
        self.assertEqual(decoded.canonical_utf8, "printf 'café'".encode())
        self.assertEqual(decoded.unicode_scalar_count, len("printf 'café'"))
        self.assertEqual(decoded.utf8_byte_count, len("printf 'café'".encode()))

    def test_decode_helper_is_strict_and_does_not_transform_text(self) -> None:
        encoded = "\ufeffe\u0301".encode()
        self.assertEqual(decode_product_command_utf8(encoded), "\ufeffe\u0301")

    def test_trims_only_leading_and_trailing_ascii_space_and_tab(self) -> None:
        command = classify_product_command(" \t  echo  keep \t ")
        self.assertEqual(command.canonical_text, "echo  keep")

        non_ascii = classify_product_command("\u00a0\u2003echo\u2003\u00a0")
        self.assertEqual(non_ascii.canonical_text, "\u00a0\u2003echo\u2003\u00a0")

    def test_canonical_bounds_ignore_trimmed_ascii_space_and_tab(self) -> None:
        command = classify_product_command(" " * 10_000 + "echo" + "\t" * 10_000)
        self.assertEqual(command.canonical_text, "echo")

    def test_performs_no_unicode_normalization(self) -> None:
        composed_text = "printf é"
        decomposed_text = "printf e\u0301"
        self.assertEqual(unicodedata.normalize("NFC", decomposed_text), composed_text)

        composed = classify_product_command(composed_text)
        decomposed = classify_product_command(decomposed_text)
        self.assertEqual(composed.canonical_text, composed_text)
        self.assertEqual(decomposed.canonical_text, decomposed_text)
        self.assertNotEqual(composed.canonical_utf8, decomposed.canonical_utf8)

    def test_rejects_malformed_utf8_deterministically(self) -> None:
        malformed_values = (
            b"\xff",
            b"echo \xc3",
            b"echo \xc0\xaf",
            b"echo \xed\xa0\x80",
            b"echo \xf4\x90\x80\x80",
        )
        for value in malformed_values:
            with self.subTest(value=value):
                self.assert_error(
                    value,
                    ProductCommandErrorCode.INVALID_COMMAND_ENCODING,
                    ProductCommandErrorReason.MALFORMED_UTF8,
                )

    def test_rejects_unpaired_surrogates_in_decoded_text(self) -> None:
        for value in ("\ud800", "echo \udfff", " \udbff\t"):
            with self.subTest(value=ascii(value)):
                self.assert_error(
                    value,
                    ProductCommandErrorCode.INVALID_COMMAND_ENCODING,
                    ProductCommandErrorReason.UNPAIRED_SURROGATE,
                )

    def test_rejects_nul_cr_and_lf_even_when_quoted_or_escaped(self) -> None:
        cases = (
            ("echo\x00value", ProductCommandErrorReason.NUL_CHARACTER),
            ("echo '\x00'", ProductCommandErrorReason.NUL_CHARACTER),
            ("echo\rvalue", ProductCommandErrorReason.LINE_BREAK),
            ('echo "line\nvalue"', ProductCommandErrorReason.LINE_BREAK),
            ("echo \\\nvalue", ProductCommandErrorReason.LINE_BREAK),
        )
        for value, reason in cases:
            with self.subTest(value=ascii(value)):
                self.assert_error(
                    value,
                    ProductCommandErrorCode.INVALID_COMMAND_TEXT,
                    reason,
                )

    def test_rejects_empty_canonical_command(self) -> None:
        for value in ("", " ", "\t", " \t \t"):
            with self.subTest(value=ascii(value)):
                self.assert_error(
                    value,
                    ProductCommandErrorCode.INVALID_COMMAND_TEXT,
                    ProductCommandErrorReason.EMPTY,
                )

    def test_non_ascii_whitespace_is_not_an_empty_command(self) -> None:
        for value in ("\u00a0", "\u2003", "\v"):
            with self.subTest(value=ascii(value)):
                self.assertEqual(classify_product_command(value).canonical_text, value)

    def test_enforces_unicode_scalar_boundary(self) -> None:
        at_limit = "a" * MAX_CANONICAL_COMMAND_UNICODE_SCALARS
        accepted = classify_product_command(at_limit)
        self.assertEqual(
            accepted.unicode_scalar_count,
            MAX_CANONICAL_COMMAND_UNICODE_SCALARS,
        )

        self.assert_error(
            at_limit + "a",
            ProductCommandErrorCode.COMMAND_TOO_LARGE,
            ProductCommandErrorReason.UNICODE_SCALAR_LIMIT,
        )

    def test_enforces_utf8_byte_boundary_independently(self) -> None:
        at_limit = "é" * (MAX_CANONICAL_COMMAND_UTF8_BYTES // 2)
        accepted = classify_product_command(at_limit)
        self.assertEqual(accepted.utf8_byte_count, MAX_CANONICAL_COMMAND_UTF8_BYTES)
        self.assertEqual(
            accepted.unicode_scalar_count,
            MAX_CANONICAL_COMMAND_UNICODE_SCALARS,
        )

        over_byte_limit = at_limit[:-1] + "€"
        self.assertEqual(
            len(over_byte_limit),
            MAX_CANONICAL_COMMAND_UNICODE_SCALARS,
        )
        self.assert_error(
            over_byte_limit,
            ProductCommandErrorCode.COMMAND_TOO_LARGE,
            ProductCommandErrorReason.UTF8_BYTE_LIMIT,
        )

    def test_rejects_non_text_non_bytes_without_coercion(self) -> None:
        for value in (bytearray(b"echo"), memoryview(b"echo"), 1, None):
            with self.subTest(type=type(value).__name__):
                self.assert_error(
                    value,  # type: ignore[arg-type]
                    ProductCommandErrorCode.INVALID_COMMAND_ENCODING,
                    ProductCommandErrorReason.UNSUPPORTED_INPUT_TYPE,
                )

        with self.assertRaises(ProductCommandError) as raised:
            decode_product_command_utf8(bytearray(b"echo"))  # type: ignore[arg-type]
        self.assertEqual(
            raised.exception.reason,
            ProductCommandErrorReason.UNSUPPORTED_INPUT_TYPE,
        )

    def test_safe_error_and_result_repr_do_not_echo_command_text(self) -> None:
        secret = "secret-command-fragment"
        accepted = classify_product_command(secret)
        self.assertNotIn(secret, repr(accepted))

        with self.assertRaises(ProductCommandError) as raised:
            classify_product_command(secret + "; other")
        error = raised.exception
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, repr(error.details))
        self.assertEqual(error.status, 422)
        self.assertEqual(error.retry, "none")
        self.assertIsNone(error.retry_after_ms)
        self.assertEqual(error.code, error.details.code)
        self.assertEqual(error.reason, error.details.reason)

    def assert_error(
        self,
        value: str | bytes,
        code: ProductCommandErrorCode,
        reason: ProductCommandErrorReason,
    ) -> None:
        with self.assertRaises(ProductCommandError) as raised:
            classify_product_command(value)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.reason, reason)


class ProductCommandSyntaxTests(unittest.TestCase):
    def test_accepts_ordinary_single_commands(self) -> None:
        commands = (
            "echo hello",
            "env NAME=value program --flag",
            "printf %s value",
            "echo ${HOME}",
            "echo [a-z]* ?.txt",
            "echo # ordinary-product-text",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    classify_product_command(command).canonical_text,
                    command,
                )

    def test_quoted_metacharacters_remain_ordinary(self) -> None:
        commands = (
            "printf '%s|%s' a b",
            'echo "a; b && c > d < e & f | g ( h )"',
            "echo '$(date) `whoami` <(x) >(y)'",
            'echo "{ one; two; }"',
            "echo ';' '&' '|' '<' '>' '(' ')' '`'",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    classify_product_command(command).canonical_text,
                    command,
                )

    def test_escaped_metacharacters_remain_ordinary(self) -> None:
        commands = (
            r"echo \; \& \| \< \> \( \) \`",
            r"echo \$\(date\)",
            r'echo "\$(date)"',
            r"echo one\;two",
            r"echo \\x\;still-one",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    classify_product_command(command).canonical_text,
                    command,
                )

    def test_rejects_unquoted_lists_conditionals_and_pipelines(self) -> None:
        commands = (
            "one; two",
            "one && two",
            "one || two",
            "one | two",
            "one |& two",
            "one & two",
            "one&",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assert_compound(
                    command, ProductCommandErrorReason.CONTROL_OPERATOR
                )

    def test_rejects_every_unquoted_redirection_form(self) -> None:
        commands = (
            "read < input",
            "write > output",
            "write >> output",
            "write 2> error",
            "write &> all",
            "read << EOF",
            "read <<< value",
            "open <> file",
            "copy 2>&1",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assert_compound(
                    command, ProductCommandErrorReason.CONTROL_OPERATOR
                )

    def test_rejects_subshells_and_command_or_process_substitution(self) -> None:
        operator_cases = (
            "(one)",
            "echo (one)",
            "echo <(one)",
            "echo >(one)",
        )
        for command in operator_cases:
            with self.subTest(command=command):
                self.assert_compound(
                    command, ProductCommandErrorReason.CONTROL_OPERATOR
                )

        substitution_cases = (
            "echo $(date)",
            "echo $((1 + 2))",
            "echo `date`",
            'echo "$(date)"',
            'echo "`date`"',
        )
        for command in substitution_cases:
            with self.subTest(command=command):
                self.assert_compound(
                    command,
                    ProductCommandErrorReason.COMMAND_SUBSTITUTION,
                )

    def test_double_quote_escaping_does_not_hide_later_substitution(self) -> None:
        self.assert_compound(
            r'echo "\\$(date)"',
            ProductCommandErrorReason.COMMAND_SUBSTITUTION,
        )

    def test_rejects_malformed_quoting_and_escaping(self) -> None:
        cases = (
            ("echo 'unterminated", ProductCommandErrorReason.UNCLOSED_SINGLE_QUOTE),
            ('echo "unterminated', ProductCommandErrorReason.UNCLOSED_DOUBLE_QUOTE),
            ("echo trailing\\", ProductCommandErrorReason.TRAILING_ESCAPE),
            ('echo "trailing\\', ProductCommandErrorReason.TRAILING_ESCAPE),
            ("'it\\'s'", ProductCommandErrorReason.UNCLOSED_SINGLE_QUOTE),
        )
        for command, reason in cases:
            with self.subTest(command=command):
                with self.assertRaises(ProductCommandError) as raised:
                    classify_product_command(command)
                self.assertEqual(
                    raised.exception.code,
                    ProductCommandErrorCode.INVALID_COMMAND_SYNTAX,
                )
                self.assertEqual(raised.exception.reason, reason)

    def test_malformed_quote_or_escape_wins_over_compound_classification(self) -> None:
        cases = (
            ("one; 'unterminated", ProductCommandErrorReason.UNCLOSED_SINGLE_QUOTE),
            ('one | "unterminated', ProductCommandErrorReason.UNCLOSED_DOUBLE_QUOTE),
            ("one && trailing\\", ProductCommandErrorReason.TRAILING_ESCAPE),
        )
        for command, reason in cases:
            with self.subTest(command=command):
                with self.assertRaises(ProductCommandError) as raised:
                    classify_product_command(command)
                self.assertEqual(
                    raised.exception.code,
                    ProductCommandErrorCode.INVALID_COMMAND_SYNTAX,
                )
                self.assertEqual(raised.exception.reason, reason)

    def test_rejects_unquoted_process_control_words_in_command_position(self) -> None:
        commands = (
            "if condition",
            "then command",
            "for item",
            "while condition",
            "until condition",
            "case value",
            "function name",
            "time command",
            "coproc command",
            "! command",
            "[[ condition ]]",
            "{ command",
            "} command",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assert_compound(command, ProductCommandErrorReason.CONTROL_WORD)

    def test_quoted_or_escaped_control_words_are_ordinary(self) -> None:
        commands = (
            "'if' condition",
            'i"f" condition',
            r"\if condition",
            "echo if then fi",
            "echo [[ condition ]]",
            "echo { command }",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    classify_product_command(command).canonical_text,
                    command,
                )

    def test_operator_after_a_quoted_word_is_still_rejected(self) -> None:
        self.assert_compound(
            "'ordinary'; second",
            ProductCommandErrorReason.CONTROL_OPERATOR,
        )

    def test_classification_is_repeatable_and_has_no_mutable_result(self) -> None:
        first = classify_product_command("  printf 'a|b'  ")
        second = classify_product_command("  printf 'a|b'  ")
        self.assertEqual(first, second)
        with self.assertRaises((AttributeError, TypeError)):
            first.canonical_text = "changed"  # type: ignore[misc]

    def assert_compound(
        self,
        command: str,
        reason: ProductCommandErrorReason,
    ) -> None:
        with self.assertRaises(ProductCommandError) as raised:
            classify_product_command(command)
        self.assertEqual(
            raised.exception.code,
            ProductCommandErrorCode.COMPOUND_SYNTAX_UNSUPPORTED,
        )
        self.assertEqual(raised.exception.reason, reason)
        self.assertIn("shell-control syntax", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
