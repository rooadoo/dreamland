"""Secure lifecycle management for DREAMLAND's SSH transport identity."""

from __future__ import annotations

import base64
import errno
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

import paramiko

DEFAULT_SSH_HOST_KEY_NAME = "ssh_host_rsa_key"
SSH_HOST_KEY_BITS = 2048
_PRIVATE_KEY_MODE = 0o600
_PRIVATE_DIRECTORY_MODE = 0o700


class SSHHostKeyError(RuntimeError):
    """Raised when a durable SSH host identity cannot be loaded safely."""


@dataclass(frozen=True, slots=True)
class SSHHostKeyIdentity:
    """A validated durable SSH host key and its public presentation data."""

    key: paramiko.RSAKey
    path: Path
    generated: bool
    algorithm: str
    fingerprint: str


def default_ssh_host_key_path() -> Path:
    """Return the per-service-user default path for DREAMLAND's host key."""

    configured_state_home = os.environ.get("XDG_STATE_HOME")
    if configured_state_home:
        state_home = Path(configured_state_home).expanduser()
        if not state_home.is_absolute():
            raise ValueError("XDG_STATE_HOME must be an absolute path")
    else:
        state_home = Path.home() / ".local" / "state"
    return state_home / "dreamland" / DEFAULT_SSH_HOST_KEY_NAME


def load_or_create_ssh_host_key(path: Path | str) -> SSHHostKeyIdentity:
    """Load one secure RSA host key, generating and publishing it once."""

    key_path = Path(path).expanduser()
    if not key_path.name:
        raise SSHHostKeyError("SSH host key path must name a file")
    directory_path = key_path.parent
    directory_created = _create_parent_directory(directory_path)
    directory_fd = _open_host_key_directory(directory_path)
    try:
        if directory_created and os.name == "posix":
            os.fchmod(directory_fd, _PRIVATE_DIRECTORY_MODE)
        _validate_host_key_directory(directory_fd, directory_path)
        existing = _load_key_if_present(directory_fd, key_path)
        if existing is not None:
            return _identity(existing, key_path, generated=False)

        generated = _publish_generated_key(directory_fd, key_path)
        published = _load_key_if_present(directory_fd, key_path)
        if published is None:  # pragma: no cover - guarded by atomic publication.
            raise SSHHostKeyError(
                f"SSH host key disappeared after publication: {key_path}"
            )
        return _identity(published, key_path, generated=generated)
    finally:
        os.close(directory_fd)


def openssh_sha256_fingerprint(key: paramiko.PKey) -> str:
    """Return the modern OpenSSH SHA-256 fingerprint for a public key."""

    digest = hashlib.sha256(key.asbytes()).digest()
    encoded = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{encoded}"


def _create_parent_directory(path: Path) -> bool:
    existing_ancestor = path
    while not existing_ancestor.exists():
        if existing_ancestor.parent == existing_ancestor:
            break
        existing_ancestor = existing_ancestor.parent
    _validate_existing_directory_path(existing_ancestor)
    try:
        path.mkdir(parents=True, mode=_PRIVATE_DIRECTORY_MODE, exist_ok=False)
    except FileExistsError:
        return False
    except OSError as exc:
        raise SSHHostKeyError(
            f"cannot create SSH host key directory {path}: {exc.strerror or exc}"
        ) from exc
    if os.name == "posix":
        try:
            os.chmod(path, _PRIVATE_DIRECTORY_MODE)
        except OSError as exc:
            raise SSHHostKeyError(
                f"cannot secure SSH host key directory {path}: {exc.strerror or exc}"
            ) from exc
    return True


def _validate_existing_directory_path(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise SSHHostKeyError(
            f"cannot inspect SSH host key directory ancestor {path}: "
            f"{exc.strerror or exc}"
        ) from exc
    if stat.S_ISLNK(details.st_mode):
        raise SSHHostKeyError(
            f"SSH host key directory ancestry must not contain symlinks: {path}"
        )
    if not stat.S_ISDIR(details.st_mode):
        raise SSHHostKeyError(
            f"SSH host key directory ancestor is not a directory: {path}"
        )


def _open_host_key_directory(path: Path) -> int:
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    missing_flags = [name for name in required_flags if not hasattr(os, name)]
    if missing_flags:
        missing = ", ".join(missing_flags)
        raise SSHHostKeyError(
            "secure SSH host-key storage is unavailable on this platform "
            f"(missing {missing})"
        )
    if (
        os.open not in os.supports_dir_fd
        or os.link not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
        or os.link not in os.supports_follow_symlinks
    ):
        raise SSHHostKeyError(
            "secure SSH host-key storage requires directory-relative open/link/unlink"
        )

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise SSHHostKeyError(
            f"cannot open SSH host key directory {path}: {exc.strerror or exc}"
        ) from exc


def _validate_host_key_directory(directory_fd: int, path: Path) -> None:
    details = os.fstat(directory_fd)
    if not stat.S_ISDIR(details.st_mode):  # pragma: no cover - O_DIRECTORY enforces.
        raise SSHHostKeyError(f"SSH host key parent is not a directory: {path}")
    if os.name != "posix":
        return
    if details.st_uid != os.geteuid():
        raise SSHHostKeyError(
            f"SSH host key directory must be owned by the current user: {path}"
        )
    if stat.S_IMODE(details.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
        raise SSHHostKeyError(
            f"SSH host key directory must not be group/world writable: {path}"
        )


def _load_key_if_present(
    directory_fd: int,
    path: Path,
) -> paramiko.RSAKey | None:
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_NOCTTY", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, name, 0)
    try:
        key_fd = os.open(path.name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SSHHostKeyError(
            f"cannot open SSH host key {path}: {exc.strerror or exc}"
        ) from exc

    try:
        details = os.fstat(key_fd)
        _validate_private_key_file(details, path)
        with os.fdopen(key_fd, "r", encoding="utf-8") as key_file:
            key_fd = -1
            try:
                key = paramiko.RSAKey.from_private_key(key_file)
            except (OSError, UnicodeError, ValueError, paramiko.SSHException) as exc:
                raise SSHHostKeyError(
                    f"SSH host key is not a valid unencrypted RSA private key: {path}"
                ) from exc
    finally:
        if key_fd >= 0:
            os.close(key_fd)

    if key.get_bits() < SSH_HOST_KEY_BITS:
        raise SSHHostKeyError(
            f"SSH host key must contain at least {SSH_HOST_KEY_BITS} RSA bits: {path}"
        )
    return key


def _validate_private_key_file(details: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(details.st_mode):
        raise SSHHostKeyError(f"SSH host key must be a regular file: {path}")
    if os.name != "posix":
        return
    if details.st_uid != os.geteuid():
        raise SSHHostKeyError(f"SSH host key must be owned by the current user: {path}")
    mode = stat.S_IMODE(details.st_mode)
    if mode not in {_PRIVATE_KEY_MODE, stat.S_IRUSR}:
        raise SSHHostKeyError(f"SSH host key permissions must be 0600 or 0400: {path}")


def _publish_generated_key(directory_fd: int, path: Path) -> bool:
    key = paramiko.RSAKey.generate(SSH_HOST_KEY_BITS)
    temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for name in ("O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)

    temporary_fd = -1
    published = False
    try:
        temporary_fd = os.open(
            temporary_name,
            flags,
            _PRIVATE_KEY_MODE,
            dir_fd=directory_fd,
        )
        if os.name == "posix":
            os.fchmod(temporary_fd, _PRIVATE_KEY_MODE)
        with os.fdopen(temporary_fd, "w", encoding="utf-8", newline="\n") as stream:
            temporary_fd = -1
            key.write_private_key(stream)
            stream.flush()
            os.fsync(stream.fileno())

        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            pass
        except OSError as exc:
            raise SSHHostKeyError(
                f"cannot atomically publish SSH host key {path}: {exc.strerror or exc}"
            ) from exc
        else:
            published = True
        _sync_directory(directory_fd, path.parent)
    except SSHHostKeyError:
        raise
    except OSError as exc:
        raise SSHHostKeyError(
            f"cannot create SSH host key {path}: {exc.strerror or exc}"
        ) from exc
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SSHHostKeyError(
                f"cannot remove temporary SSH host key for {path}: "
                f"{exc.strerror or exc}"
            ) from exc
        _sync_directory(directory_fd, path.parent)
    return published


def _sync_directory(directory_fd: int, path: Path) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP}:
            raise SSHHostKeyError(
                f"SSH host key directory does not support durable sync: {path}"
            ) from exc
        raise


def _identity(
    key: paramiko.RSAKey,
    path: Path,
    *,
    generated: bool,
) -> SSHHostKeyIdentity:
    return SSHHostKeyIdentity(
        key=key,
        path=path,
        generated=generated,
        algorithm=key.get_name(),
        fingerprint=openssh_sha256_fingerprint(key),
    )
