import unittest

from dreamland.ssh_auth import SharedPasswordAuthenticator


class SharedPasswordAuthenticatorTests(unittest.TestCase):
    def test_fast_aliases_accept_only_their_own_password(self):
        authenticator = SharedPasswordAuthenticator(
            "normal-secret",
            required_username="dreamland",
            allow_simple_login=True,
            fast_password="demo-secret",
        )
        for username in ("dreamland", "dreamland_simple"):
            self.assertTrue(
                authenticator.authenticate_password(username, "normal-secret")
            )
            self.assertFalse(
                authenticator.authenticate_password(username, "demo-secret")
            )
        for username in ("dreamland_fast", "dreamland_fast_simple"):
            self.assertTrue(
                authenticator.authenticate_password(username, b"demo-secret")
            )
            for password in ("normal-secret", "wrong", "", b"\xff\xfe"):
                self.assertFalse(
                    authenticator.authenticate_password(username, password)
                )
        for username in (
            "_fast",
            "other_fast",
            "dreamland_simple_fast",
            "dreamland_fast_fast",
            "dreamland_fast_simple_simple",
            "dreamland_FAST",
            "dreamland_fast ",
            "dreamland_fast\n",
            "dreamland_fast\x00",
            "DREAMLAND_fast",
        ):
            for password in ("normal-secret", "demo-secret"):
                self.assertFalse(
                    authenticator.authenticate_password(username, password)
                )
        self.assertNotIn("demo-secret", repr(authenticator))
        self.assertNotIn("digest", repr(authenticator))

    def test_unconfigured_fast_login_never_uses_normal_password(self):
        authenticator = SharedPasswordAuthenticator(
            "normal-secret", required_username="dreamland", allow_simple_login=True
        )
        for username in ("dreamland_fast", "dreamland_fast_simple"):
            self.assertFalse(
                authenticator.authenticate_password(username, "normal-secret")
            )

    def test_fast_password_must_be_distinct_and_nonblank(self):
        for password in ("", " \t", "normal-secret"):
            with (
                self.subTest(password=password),
                self.assertRaisesRegex(ValueError, "DREAMLAND_SSH_FAST_PASSWORD"),
            ):
                SharedPasswordAuthenticator(
                    "normal-secret",
                    required_username="dreamland",
                    fast_password=password,
                )

    def test_direct_usernames_keep_fast_suffix_literal(self):
        authenticator = SharedPasswordAuthenticator("normal-secret")
        for username in ("dreamland_fast", "dreamland_fast_simple", "_fast"):
            self.assertTrue(
                authenticator.authenticate_password(username, "normal-secret")
            )
        with self.assertRaisesRegex(ValueError, "fixed lobby username"):
            SharedPasswordAuthenticator("normal-secret", fast_password="demo-secret")

    def test_accepts_arbitrary_nonempty_username_with_exact_password(self):
        authenticator = SharedPasswordAuthenticator("conference-secret")

        for username in ("alice", "root", "name@example.test", "Δ-user"):
            with self.subTest(username=username):
                self.assertTrue(
                    authenticator.authenticate_password(
                        username,
                        "conference-secret",
                    )
                )

    def test_rejects_empty_username_or_wrong_password(self):
        authenticator = SharedPasswordAuthenticator("conference-secret")

        self.assertFalse(authenticator.authenticate_password("", "conference-secret"))
        self.assertFalse(authenticator.authenticate_password("alice", ""))
        self.assertFalse(authenticator.authenticate_password("alice", "wrong"))

    def test_supports_unicode_password_and_rejects_invalid_utf8_bytes(self):
        authenticator = SharedPasswordAuthenticator("correct horse ☃")

        self.assertTrue(authenticator.authenticate_password("alice", "correct horse ☃"))
        self.assertFalse(authenticator.authenticate_password("alice", b"\xff\xfe"))

    def test_requires_nonblank_password(self):
        for password in ("", " ", "\t\n"):
            with self.subTest(password=password):
                with self.assertRaisesRegex(
                    ValueError,
                    "DREAMLAND_SSH_PASSWORD",
                ):
                    SharedPasswordAuthenticator(password)

    def test_required_username_remains_exact_without_simple_login_opt_in(self):
        authenticator = SharedPasswordAuthenticator(
            "conference-secret", required_username="dreamland"
        )
        self.assertTrue(
            authenticator.authenticate_password("dreamland", "conference-secret")
        )
        for username in ("", "other", "dreamland_simple", "DREAMLAND", "dreamland "):
            with self.subTest(username=username):
                self.assertFalse(
                    authenticator.authenticate_password(username, "conference-secret")
                )

    def test_simple_alias_requires_exact_base_and_same_password(self):
        authenticator = SharedPasswordAuthenticator(
            "conference-secret",
            required_username="dreamland",
            allow_simple_login=True,
        )
        for username in ("dreamland", "dreamland_simple"):
            with self.subTest(username=username):
                self.assertTrue(
                    authenticator.authenticate_password(username, "conference-secret")
                )
                for password in ("wrong", "", b"\xff\xfe"):
                    self.assertFalse(
                        authenticator.authenticate_password(username, password)
                    )
        for username in (
            "",
            "_simple",
            "other_simple",
            "dreamland_simple_simple",
            "dreamland_static",
            "dreamland_SIMPLE",
            "DREAMLAND_simple",
            "dreamland_simple ",
            "dreamland_simple\n",
            "dreamland_simple\x00",
        ):
            with self.subTest(username=username):
                self.assertFalse(
                    authenticator.authenticate_password(username, "conference-secret")
                )

    def test_simple_alias_preserves_configured_name_already_ending_in_suffix(self):
        authenticator = SharedPasswordAuthenticator(
            "conference-secret",
            required_username="operator_simple",
            allow_simple_login=True,
        )
        for username in ("operator_simple", "operator_simple_simple"):
            self.assertTrue(
                authenticator.authenticate_password(username, "conference-secret")
            )
        self.assertFalse(
            authenticator.authenticate_password("operator", "conference-secret")
        )

    def test_unrestricted_simple_alias_requires_nonempty_base(self):
        authenticator = SharedPasswordAuthenticator(
            "conference-secret", allow_simple_login=True
        )
        for username in (
            "alice",
            "alice_simple",
            "name@example.test_simple",
            "Δ_simple",
        ):
            self.assertTrue(
                authenticator.authenticate_password(username, "conference-secret")
            )
        for username in ("", "_simple"):
            self.assertFalse(
                authenticator.authenticate_password(username, "conference-secret")
            )

    def test_direct_unrestricted_usernames_keep_literal_suffix(self):
        authenticator = SharedPasswordAuthenticator("conference-secret")
        for username in ("alice_simple", "_simple"):
            self.assertTrue(
                authenticator.authenticate_password(username, "conference-secret")
            )

    def test_representation_does_not_expose_password_or_digest(self):
        password = "unique-sentinel-password"
        authenticator = SharedPasswordAuthenticator(password)

        rendered = repr(authenticator)

        self.assertNotIn(password, rendered)
        self.assertNotIn("password_digest", rendered)


if __name__ == "__main__":
    unittest.main()
