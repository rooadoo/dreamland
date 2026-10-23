import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from queue import Empty

import paramiko

from dreamland.ssh_host_keys import (
    SSHHostKeyError,
    load_or_create_ssh_host_key,
    openssh_sha256_fingerprint,
)


def _load_host_key_in_process(path, start_event, results):
    start_event.wait(10)
    try:
        identity = load_or_create_ssh_host_key(path)
    except Exception as exc:  # pragma: no cover - reported to the parent process.
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        results.put(("ok", identity.fingerprint, identity.generated))


@unittest.skipUnless(os.name == "posix", "secure key lifecycle requires POSIX")
class SSHHostKeyTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.path = self.root / "identity" / "ssh_host_rsa_key"

    def tearDown(self):
        self._temporary_directory.cleanup()

    def test_generates_once_then_reuses_identical_key(self):
        first = load_or_create_ssh_host_key(self.path)
        original_bytes = self.path.read_bytes()
        original_stat = self.path.stat()

        second = load_or_create_ssh_host_key(self.path)

        self.assertTrue(first.generated)
        self.assertFalse(second.generated)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.key.asbytes(), second.key.asbytes())
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(self.path.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

    def test_fingerprint_uses_openssh_sha256_form(self):
        identity = load_or_create_ssh_host_key(self.path)

        self.assertEqual(identity.algorithm, "ssh-rsa")
        self.assertEqual(identity.fingerprint, identity.key.fingerprint)
        self.assertEqual(
            identity.fingerprint,
            openssh_sha256_fingerprint(identity.key),
        )
        self.assertTrue(identity.fingerprint.startswith("SHA256:"))

    def test_generated_permissions_ignore_process_umask(self):
        for index, process_umask in enumerate((0, 0o777)):
            with self.subTest(process_umask=oct(process_umask)):
                path = self.root / f"identity-{index}" / "ssh_host_rsa_key"
                previous_umask = os.umask(process_umask)
                try:
                    load_or_create_ssh_host_key(path)
                finally:
                    os.umask(previous_umask)

                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_corrupt_existing_key_fails_without_replacement(self):
        self.path.parent.mkdir(mode=0o700)
        original = b"not a private key\n"
        self.path.write_bytes(original)
        self.path.chmod(0o600)

        with self.assertRaisesRegex(SSHHostKeyError, "valid unencrypted RSA"):
            load_or_create_ssh_host_key(self.path)

        self.assertEqual(self.path.read_bytes(), original)

    def test_encrypted_existing_key_fails_without_replacement(self):
        self.path.parent.mkdir(mode=0o700)
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(str(self.path), password="key-password")
        self.path.chmod(0o600)
        original = self.path.read_bytes()

        with self.assertRaisesRegex(SSHHostKeyError, "valid unencrypted RSA"):
            load_or_create_ssh_host_key(self.path)

        self.assertEqual(self.path.read_bytes(), original)

    def test_insecure_existing_key_permissions_fail_without_repair(self):
        self.path.parent.mkdir(mode=0o700)
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(str(self.path))
        self.path.chmod(0o644)

        with self.assertRaisesRegex(SSHHostKeyError, "permissions"):
            load_or_create_ssh_host_key(self.path)

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_special_permission_bits_are_rejected(self):
        self.path.parent.mkdir(mode=0o700)
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(str(self.path))
        self.path.chmod(0o4600)

        with self.assertRaisesRegex(SSHHostKeyError, "permissions"):
            load_or_create_ssh_host_key(self.path)

    def test_undersized_existing_rsa_key_is_rejected(self):
        self.path.parent.mkdir(mode=0o700)
        key = paramiko.RSAKey.generate(1024)
        key.write_private_key_file(str(self.path))
        self.path.chmod(0o600)

        with self.assertRaisesRegex(SSHHostKeyError, "at least 2048 RSA bits"):
            load_or_create_ssh_host_key(self.path)

    def test_group_writable_parent_directory_is_rejected(self):
        self.path.parent.mkdir(mode=0o700)
        self.path.parent.chmod(0o770)

        with self.assertRaisesRegex(SSHHostKeyError, "group/world writable"):
            load_or_create_ssh_host_key(self.path)

        self.assertFalse(self.path.exists())

    def test_symlink_key_is_rejected_without_touching_target(self):
        self.path.parent.mkdir(mode=0o700)
        target = self.root / "unrelated"
        target.write_text("leave me alone\n", encoding="utf-8")
        self.path.symlink_to(target)

        with self.assertRaisesRegex(SSHHostKeyError, "cannot open"):
            load_or_create_ssh_host_key(self.path)

        self.assertEqual(target.read_text(encoding="utf-8"), "leave me alone\n")
        self.assertTrue(self.path.is_symlink())

    def test_symlink_parent_is_rejected_without_creating_key(self):
        target = self.root / "real-identity"
        target.mkdir(mode=0o700)
        self.path.parent.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(SSHHostKeyError, "ancestry.*symlink"):
            load_or_create_ssh_host_key(self.path)

        self.assertFalse((target / self.path.name).exists())

    def test_fifo_key_is_rejected_without_blocking(self):
        self.path.parent.mkdir(mode=0o700)
        os.mkfifo(self.path, mode=0o600)

        with self.assertRaisesRegex(SSHHostKeyError, "regular file"):
            load_or_create_ssh_host_key(self.path)

    def test_concurrent_processes_publish_one_identity(self):
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_load_host_key_in_process,
                args=(self.path, start_event, results),
            )
            for _ in range(4)
        ]
        for process in processes:
            process.start()
        start_event.set()

        outcomes = []
        try:
            for _ in processes:
                outcomes.append(results.get(timeout=20))
        except Empty:
            self.fail("host-key worker did not report a result")
        finally:
            for process in processes:
                process.join(timeout=20)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)

        self.assertEqual([process.exitcode for process in processes], [0] * 4)
        self.assertTrue(all(outcome[0] == "ok" for outcome in outcomes), outcomes)
        fingerprints = {outcome[1] for outcome in outcomes}
        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(sum(bool(outcome[2]) for outcome in outcomes), 1)
        self.assertEqual(
            load_or_create_ssh_host_key(self.path).fingerprint,
            fingerprints.pop(),
        )
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
