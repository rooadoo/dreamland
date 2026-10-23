"""Owner-only persistent setup and credentials for the Linux deployment."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

PROFILE_NAMES = ("lobby-supermarket", "lobby-colonyship", "lobby-offshore")
SECRET_NAMES = frozenset({"ssh_password", "openai_api_key"})
_MARKER = ".initialized.json"
_MARKER_DOCUMENT = {"kind": "dreamland.deployment.initialized", "schema_version": 1}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_MANAGED_LIMITS = {
    "deployment.json": 64 * 1024,
    "dreamland.ini": 64 * 1024,
    "lobby/catalog.json": 1024 * 1024,
    "lobby/provenance.json": 1024 * 1024,
    **{f"state_profiles/{name}.json": 4 * 1024 * 1024 for name in PROFILE_NAMES},
}


class StorageError(ValueError):
    """Deployment storage could not be used safely."""


def initialize(root: Path, deployment_document: dict, runtime_ini: str) -> dict:
    """Seed once, preserving existing bytes on recovery and removals thereafter.

    The completion marker is published last. An interrupted first setup can be
    rerun; already published files are preserved and missing files are seeded.
    After completion, missing files are intentional and never restored here.
    """
    try:
        with _root_directory(root, create=True) as root_fd, _lock(root_fd):
            _recover_publications(root_fd, _root_publication_names(root_fd))
            marker = _read_file(root_fd, _MARKER, maximum=1024)
            if marker is not None:
                _validate_marker(marker)
            completed = marker is not None
            resources = files("dreamland.deployment").joinpath("assets")
            baselines = files("dreamland.default_state_profiles")
            seeds = {
                "deployment.json": (
                    json.dumps(deployment_document, indent=2, sort_keys=True) + "\n"
                ).encode("utf-8"),
                "dreamland.ini": runtime_ini.encode("utf-8"),
                "lobby/catalog.json": resources.joinpath("catalog.json").read_bytes(),
                "lobby/provenance.json": resources.joinpath(
                    "provenance.json"
                ).read_bytes(),
                **{
                    f"state_profiles/{name}.json": baselines.joinpath(
                        f"{name}.json"
                    ).read_bytes()
                    for name in PROFILE_NAMES
                },
            }
            report: dict = {
                "initialized": not completed,
                "created": [],
                "preserved": [],
                "absent": [],
            }
            with (
                _child_directory(root_fd, "lobby", create=not completed) as lobby_fd,
                _child_directory(
                    root_fd, "state_profiles", create=not completed
                ) as profiles_fd,
                _child_directory(
                    root_fd, "secrets", create=not completed
                ) as secrets_fd,
            ):
                directories = {
                    "": root_fd,
                    "lobby": lobby_fd,
                    "state_profiles": profiles_fd,
                }
                for parent, directory_fd in directories.items():
                    if parent and directory_fd is not None:
                        _recover_publications(
                            directory_fd,
                            tuple(
                                relative.rpartition("/")[2]
                                for relative in seeds
                                if relative.rpartition("/")[0] == parent
                            ),
                        )
                # Reject unsafe existing targets before publishing any seed.
                present = {}
                for relative in seeds:
                    parent, _, name = relative.rpartition("/")
                    directory_fd = directories[parent]
                    present[relative] = directory_fd is not None and _file_present(
                        directory_fd, name
                    )
                if secrets_fd is not None:
                    _recover_publications(secrets_fd, tuple(SECRET_NAMES))
                    for name in SECRET_NAMES:
                        _file_present(secrets_fd, name)
                for relative, content in seeds.items():
                    if present[relative]:
                        report["preserved"].append(relative)
                    elif completed:
                        report["absent"].append(relative)
                    else:
                        parent, _, name = relative.rpartition("/")
                        directory_fd = directories[parent]
                        assert directory_fd is not None
                        created = _publish(directory_fd, name, content)
                        report["created" if created else "preserved"].append(relative)
                if not completed:
                    _publish(
                        root_fd,
                        _MARKER,
                        (json.dumps(_MARKER_DOCUMENT) + "\n").encode("utf-8"),
                    )
            return report
    except StorageError:
        raise
    except (OSError, TypeError, UnicodeError):
        raise StorageError("could not initialize deployment storage") from None


def read_configuration(root: Path) -> tuple[dict, str]:
    """Read completed deployment settings without disclosing malformed content."""
    document_bytes = read_managed(root, "deployment.json")
    ini_bytes = read_managed(root, "dreamland.ini")
    try:
        document = json.loads(document_bytes)
        runtime_ini = ini_bytes.decode("utf-8")
    except (ValueError, UnicodeError):
        raise StorageError("deployment configuration is invalid") from None
    if not isinstance(document, dict):
        raise StorageError("deployment configuration must be a JSON object")
    return document, runtime_ini


def read_managed(root: Path, relative: str) -> bytes:
    """Read one bounded allowlisted setup file from a completed deployment."""
    if not isinstance(relative, str) or relative not in _MANAGED_LIMITS:
        raise StorageError("unsupported deployment file")
    return _read_deployment_file(root, relative, maximum=_MANAGED_LIMITS[relative])


def replace_runtime_ini(root: Path, *, expected: bytes, replacement: bytes) -> str:
    """Back up and compare-and-replace an explicitly reviewed INI update."""
    limit = _MANAGED_LIMITS["dreamland.ini"]
    if not replacement or len(replacement) > limit:
        raise StorageError("replacement runtime configuration has an invalid size")
    backup = (
        "dreamland.ini.before-model-defaults-"
        + hashlib.sha256(expected).hexdigest()[:16]
    )
    try:
        with _root_directory(root, create=False) as root_fd, _lock(root_fd):
            marker = _read_file(root_fd, _MARKER, maximum=1024)
            if marker is None:
                raise StorageError("deployment initialization is incomplete")
            _validate_marker(marker)
            _recover_publications(root_fd, _root_publication_names(root_fd))
            original = _read_file(root_fd, "dreamland.ini", maximum=limit)
            if original is None or original != expected:
                raise StorageError("runtime configuration changed; review it again")
            saved = _read_file(root_fd, backup, maximum=limit)
            if saved is not None and saved != original:
                raise StorageError(
                    "runtime configuration backup conflicts with existing data"
                )
            if saved is None:
                _publish(root_fd, backup, original)
            _publish(root_fd, "dreamland.ini", replacement, replace=True)
            return backup
    except OSError:
        raise StorageError("could not replace runtime configuration safely") from None


def read_profile(root: Path, name: str) -> bytes:
    """Read one selected saved profile safely, including operator-authored worlds."""
    if (
        not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) is None
    ):
        raise StorageError("unsupported deployment profile name")
    return _read_deployment_file(
        root, f"state_profiles/{name}.json", maximum=16 * 1024 * 1024
    )


def _read_deployment_file(root: Path, relative: str, *, maximum: int) -> bytes:
    try:
        with _root_directory(root, create=False) as root_fd:
            marker = _read_file(root_fd, _MARKER, maximum=1024)
            if marker is None:
                raise StorageError("deployment initialization is incomplete")
            _validate_marker(marker)
            parent, _, name = relative.rpartition("/")
            if parent:
                with _child_directory(root_fd, parent, create=False) as directory_fd:
                    content = (
                        None
                        if directory_fd is None
                        else _read_file(directory_fd, name, maximum=maximum)
                    )
            else:
                content = _read_file(root_fd, name, maximum=maximum)
            if content is None:
                raise StorageError("deployment file is missing")
            return content
    except OSError:
        raise StorageError("could not read deployment file safely") from None


def _validate_marker(marker: bytes) -> None:
    try:
        document = json.loads(marker)
    except (ValueError, UnicodeError):
        raise StorageError("deployment initialization marker is invalid") from None
    if (
        document != _MARKER_DOCUMENT
        or not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
    ):
        raise StorageError("deployment initialization marker is invalid")


def write_secret(root: Path, name: str, value: str) -> None:
    """Atomically replace an allowlisted credential without changing its mode."""
    _validate_secret_name(name)
    content = _encode_secret(value)
    try:
        with _root_directory(root, create=False) as root_fd, _lock(root_fd):
            with _child_directory(root_fd, "secrets", create=False) as directory_fd:
                if directory_fd is None:
                    raise StorageError("deployment secrets directory is missing")
                _recover_publications(directory_fd, tuple(SECRET_NAMES))
                _file_present(directory_fd, name)
                _publish(directory_fd, name, content, replace=True)
    except OSError:
        raise StorageError("could not store deployment secret") from None


def validate_secret(value: str) -> None:
    """Check a prompted credential before committing any credential changes."""
    _encode_secret(value)


def read_secret(root: Path, name: str) -> str:
    """Read a bounded owner-only regular credential; errors never include it."""
    _validate_secret_name(name)
    try:
        with _root_directory(root, create=False) as root_fd:
            with _child_directory(root_fd, "secrets", create=False) as directory_fd:
                if directory_fd is None:
                    raise StorageError("deployment secrets directory is missing")
                content = _read_file(directory_fd, name, maximum=4097)
                if content is None:
                    raise StorageError("deployment secret is missing")
                value = content.decode("utf-8").removesuffix("\n")
                _encode_secret(value)
                return value
    except (OSError, UnicodeError):
        raise StorageError("could not read deployment secret") from None


def secret_present(root: Path, name: str) -> bool:
    """Report whether an allowlisted credential exists, without reading its value.

    Presence is not acceptance: a stored credential has never been offered to a
    provider by this command.
    """
    _validate_secret_name(name)
    try:
        with _root_directory(root, create=False) as root_fd:
            with _child_directory(root_fd, "secrets", create=False) as directory_fd:
                if directory_fd is None:
                    return False
                return _file_present(directory_fd, name)
    except OSError:
        raise StorageError("could not inspect deployment secrets") from None


def _validate_secret_name(name: str) -> None:
    if not isinstance(name, str) or name not in SECRET_NAMES:
        raise StorageError("unsupported deployment secret")


def _encode_secret(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise StorageError("deployment secret must be a nonempty single line")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise StorageError("deployment secret must use valid UTF-8") from None
    if len(encoded) > 4096:
        raise StorageError("deployment secret exceeds the size limit")
    return encoded + b"\n"


def _validate_directory(directory_fd: int) -> None:
    details = os.fstat(directory_fd)
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise StorageError(
            "deployment directories must be owner-only and owned by the current user"
        )


def _validate_file(file_fd: int) -> None:
    details = os.fstat(file_fd)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise StorageError(
            "deployment files must be owner-only regular files without hardlinks"
        )


@contextmanager
def _root_directory(root: Path, *, create: bool) -> Iterator[int]:
    path = Path(root)
    if ".." in path.parts:
        raise StorageError("deployment root must not contain parent traversal")
    path = path.absolute()
    if path == Path("/"):
        raise StorageError("deployment root must be a dedicated directory")
    current_fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        # Directory-relative opens reject symlinks in every path component.
        for part in path.parts[1:]:
            try:
                child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise StorageError("deployment root is missing") from None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = child_fd
        _validate_directory(current_fd)
        yield current_fd
    except OSError:
        raise StorageError("cannot open deployment directory safely") from None
    finally:
        os.close(current_fd)


@contextmanager
def _child_directory(
    parent_fd: int, name: str, *, create: bool
) -> Iterator[int | None]:
    try:
        directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            yield None
            return
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        os.fsync(parent_fd)
    try:
        _validate_directory(directory_fd)
        yield directory_fd
    finally:
        os.close(directory_fd)


@contextmanager
def _lock(root_fd: int) -> Iterator[None]:
    lock_fd = os.open(
        ".initialize.lock", os.O_RDWR | os.O_CREAT | _FILE_FLAGS, 0o600, dir_fd=root_fd
    )
    try:
        _validate_file(lock_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StorageError(
                "another deployment setup or secret update is running"
            ) from None
        yield
    finally:
        os.close(lock_fd)


def _file_present(directory_fd: int, name: str) -> bool:
    try:
        file_fd = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    try:
        _validate_file(file_fd)
    finally:
        os.close(file_fd)
    return True


def _read_file(directory_fd: int, name: str, *, maximum: int) -> bytes | None:
    try:
        file_fd = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file_fd, "rb") as handle:
        _validate_file(handle.fileno())
        content = handle.read(maximum + 1)
        if len(content) > maximum:
            raise StorageError("deployment file exceeds the size limit")
        return content


def _root_publication_names(directory_fd: int) -> tuple[str, ...]:
    backups = tuple(
        name
        for name in os.listdir(directory_fd)
        if re.fullmatch(r"dreamland\.ini\.before-model-defaults-[0-9a-f]{16}", name)
    )
    return ("deployment.json", "dreamland.ini", _MARKER, *backups)


def _recover_publications(directory_fd: int, names: tuple[str, ...]) -> None:
    """Remove only our reserved stages, including a crash after linking a seed."""
    for name in os.listdir(directory_fd):
        if not (
            name.startswith(".setup-")
            and len(name) == 39
            and all(character in "0123456789abcdef" for character in name[7:])
        ):
            continue
        file_fd = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=directory_fd)
        try:
            details = os.fstat(file_fd)
            if details.st_nlink == 2:
                matched = False
                for target in names:
                    try:
                        published = os.stat(
                            target, dir_fd=directory_fd, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        continue
                    if (details.st_dev, details.st_ino) == (
                        published.st_dev,
                        published.st_ino,
                    ):
                        matched = True
                        break
                if not matched:
                    raise StorageError("unsafe interrupted deployment publication")
            elif details.st_nlink != 1:
                raise StorageError("unsafe interrupted deployment publication")
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) & 0o077
            ):
                raise StorageError("unsafe interrupted deployment publication")
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(file_fd)


def _publish(
    directory_fd: int, name: str, content: bytes, *, replace: bool = False
) -> bool:
    temporary = f".setup-{secrets.token_hex(16)}"
    file_fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(file_fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            _file_present(directory_fd, name)
            os.replace(
                temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
            )
        else:
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                _file_present(directory_fd, name)
                return False
        return True
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.fsync(directory_fd)
