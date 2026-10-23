"""Operator-owned, atomic catalog of saved profiles offered to SSH visitors."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from dreamland.lobby_content import (
    LobbyContentError,
    read_bounded_file,
    validate_public_text,
)
from dreamland.state_profiles import (
    StateProfile,
    StateProfileStore,
    state_profile_from_document,
    state_profile_to_session,
)

CATALOG_KIND = "dreamland.ssh_lobby_catalog"
CATALOG_VERSION = 2
MAX_WORLDS = 100
MAX_CATALOG_BYTES = 262_144
MAX_PROFILE_BYTES = 16_777_216
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_ENTRY_USERNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")


class LobbyCatalogError(ValueError):
    """An operator catalog update or attendee selection could not be admitted."""


class LobbySelectionChanged(LobbyCatalogError):
    """A displayed picker is stale and must be shown again before selection."""


@dataclass(frozen=True, slots=True)
class LobbyWorld:
    profile: str
    title: str
    description: str
    order: int
    entry_username: str

    @property
    def identifier(self) -> str:
        return self.profile


@dataclass(frozen=True, slots=True)
class LobbyCatalogSnapshot:
    revision: int
    entries: tuple[LobbyWorld, ...]


def _profile_entry_username(profile: StateProfile) -> str:
    """Resolve a declared starting account without guessing from prose or users."""

    def account(value: object) -> str:
        if not isinstance(value, str) or _ENTRY_USERNAME.fullmatch(value) is None:
            raise LobbyCatalogError(
                "profile starting account is not a valid literal entry_username; "
                "specify --entry-username explicitly"
            )
        return value

    for key in ("login_username", "username"):
        if profile.state.get(key) is not None:
            return account(profile.state[key])
    world = profile.state.get("world_state")
    host = None
    if isinstance(world, dict):
        hosts = world.get("hosts_by_name")
        entry_host = world.get("entry_host")
        if isinstance(hosts, dict) and isinstance(entry_host, str):
            host = hosts.get(entry_host)
    candidates: set[str] = set()
    if isinstance(host, dict):
        if "default_user" in host:
            return account(host["default_user"])
        facts = host.get("facts_by_id")
        if isinstance(facts, dict):
            for fact in facts.values():
                if not isinstance(fact, dict):
                    continue
                for key in ("username", "local_username", "maintenance_username"):
                    if key in fact:
                        candidates.add(account(fact[key]))
    if len(candidates) == 1:
        return candidates.pop()
    reason = "ambiguous" if candidates else "missing"
    raise LobbyCatalogError(
        f"profile starting entry_username is {reason}; "
        "specify --entry-username explicitly"
    )


class LobbyCatalog:
    def __init__(self, store: StateProfileStore, path: Path | str) -> None:
        self.store = store
        self.path = Path(path)
        self._lock = threading.RLock()
        self._snapshot = LobbyCatalogSnapshot(revision=0, entries=())
        if self.path.exists() or self.path.is_symlink():
            self.reload()
        else:
            self._persist(())

    def snapshot(self) -> LobbyCatalogSnapshot:
        with self._lock:
            return self._snapshot

    def add(
        self,
        profile: str,
        *,
        title: str,
        description: str,
        entry_username: str | None = None,
    ) -> LobbyCatalogSnapshot:
        with self._lock:
            if any(world.profile == profile for world in self._snapshot.entries):
                raise LobbyCatalogError("profile is already in the lobby catalog")
            if entry_username is None:
                entry_username = _profile_entry_username(self._read_profile(profile))
            world = LobbyWorld(
                profile,
                title,
                description,
                len(self._snapshot.entries) + 1,
                entry_username,
            )
            return self._publish((*self._snapshot.entries, world), persist=True)

    def update(
        self,
        profile: str,
        *,
        title: str | None = None,
        description: str | None = None,
        entry_username: str | None = None,
    ) -> LobbyCatalogSnapshot:
        with self._lock:
            self._required_world(profile)
            entries = tuple(
                replace(
                    world,
                    title=world.title if title is None else title,
                    description=world.description
                    if description is None
                    else description,
                    entry_username=world.entry_username
                    if entry_username is None
                    else entry_username,
                )
                if world.profile == profile
                else world
                for world in self._snapshot.entries
            )
            return self._publish(entries, persist=True)

    def order(self, profiles: Sequence[str]) -> LobbyCatalogSnapshot:
        with self._lock:
            worlds = {world.profile: world for world in self._snapshot.entries}
            if len(profiles) != len(worlds) or set(profiles) != set(worlds):
                raise LobbyCatalogError(
                    "order must contain every catalog profile exactly once"
                )
            entries = tuple(
                replace(worlds[name], order=number)
                for number, name in enumerate(profiles, 1)
            )
            return self._publish(entries, persist=True)

    def remove(self, profile: str) -> LobbyCatalogSnapshot:
        with self._lock:
            self._required_world(profile)
            remaining = (
                world for world in self._snapshot.entries if world.profile != profile
            )
            entries = tuple(
                replace(world, order=number)
                for number, world in enumerate(remaining, 1)
            )
            return self._publish(entries, persist=True)

    def reload(self) -> LobbyCatalogSnapshot:
        with self._lock:
            try:
                data = read_bounded_file(
                    self.path, max_bytes=MAX_CATALOG_BYTES, name="lobby catalog"
                )
                document = json.loads(data)
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                raise LobbyCatalogError(
                    "could not read lobby catalog; check its JSON and file access"
                ) from exc
            if (
                isinstance(document, dict)
                and document.get("kind") == CATALOG_KIND
                and type(document.get("schema_version")) is int
                and document["schema_version"] == 1
            ):
                raise LobbyCatalogError(
                    "lobby catalog schema_version 1 requires migration: add an explicit "
                    "entry_username to every world and set schema_version to 2"
                )
            if (
                not isinstance(document, dict)
                or document.get("kind") != CATALOG_KIND
                or type(document.get("schema_version")) is not int
                or document.get("schema_version") != CATALOG_VERSION
            ):
                raise LobbyCatalogError(
                    "lobby catalog kind or schema_version is invalid"
                )
            records = document.get("worlds")
            if not isinstance(records, list) or len(records) > MAX_WORLDS:
                raise LobbyCatalogError(
                    f"lobby catalog worlds must be a list of at most {MAX_WORLDS} entries"
                )
            entries = []
            for record in records:
                if not isinstance(record, dict) or set(record) != {
                    "profile",
                    "title",
                    "description",
                    "order",
                    "entry_username",
                }:
                    raise LobbyCatalogError(
                        "each world requires profile, title, description, order, and entry_username"
                    )
                if type(record["order"]) is not int:
                    raise LobbyCatalogError("world order must be an integer")
                for key in ("profile", "title", "description", "entry_username"):
                    if not isinstance(record[key], str):
                        raise LobbyCatalogError(f"world {key} must be text")
                entries.append(LobbyWorld(**record))
            return self._publish(
                tuple(sorted(entries, key=lambda world: world.order)), persist=False
            )

    def resolve_selection(
        self,
        snapshot: LobbyCatalogSnapshot,
        number: int,
    ) -> tuple[LobbyWorld, StateProfile]:
        with self._lock:
            if snapshot != self._snapshot:
                raise LobbySelectionChanged(
                    "The world menu changed. Refresh and choose again."
                )
            if type(number) is not int or not 1 <= number <= len(snapshot.entries):
                raise LobbyCatalogError(
                    "Choose a number from the displayed world menu."
                )
            world = snapshot.entries[number - 1]
            try:
                profile = self._load_profile(world.profile, world.entry_username)
            except LobbyCatalogError as exc:
                # Public errors contain neither saved names nor private filesystem paths.
                raise LobbySelectionChanged(
                    "That world is unavailable. Refresh and choose again."
                ) from exc
            return world, profile

    def _required_world(self, profile: str) -> LobbyWorld:
        for world in self._snapshot.entries:
            if world.profile == profile:
                return world
        raise LobbyCatalogError("profile is not in the lobby catalog")

    def _publish(
        self, entries: tuple[LobbyWorld, ...], *, persist: bool
    ) -> LobbyCatalogSnapshot:
        if len(entries) > MAX_WORLDS:
            raise LobbyCatalogError(
                f"lobby catalog supports at most {MAX_WORLDS} worlds"
            )
        if len({world.profile for world in entries}) != len(entries):
            raise LobbyCatalogError("duplicate lobby profiles are not allowed")
        validated = []
        for expected, world in enumerate(entries, 1):
            if world.order != expected:
                raise LobbyCatalogError("world order must be contiguous, starting at 1")
            if any(
                "[dreamland simulation" in value.lower()
                for value in (world.title, world.description)
            ):
                raise LobbyCatalogError(
                    "world copy cannot contain reserved simulation markers"
                )
            try:
                title = validate_public_text(
                    world.title, name="world title", max_length=80
                )
                description = validate_public_text(
                    world.description, name="world description", max_length=320
                )
            except LobbyContentError as exc:
                raise LobbyCatalogError(str(exc)) from exc
            self._load_profile(world.profile, world.entry_username)
            validated.append(replace(world, title=title, description=description))
        published_entries = tuple(validated)
        if persist:
            self._persist(published_entries)
        self._snapshot = LobbyCatalogSnapshot(
            self._snapshot.revision + 1, published_entries
        )
        return self._snapshot

    def _load_profile(self, name: str, entry_username: str) -> StateProfile:
        if (
            not isinstance(entry_username, str)
            or _ENTRY_USERNAME.fullmatch(entry_username) is None
        ):
            raise LobbyCatalogError(
                "entry_username must be a literal account name of 1-64 ASCII "
                "characters, starting with a letter or underscore and containing "
                "only letters, numbers, dots, dashes, and underscores"
            )
        profile = self._read_profile(name)
        try:
            probe = state_profile_to_session(
                profile,
                current_session_id="lobby-validation",
                authenticated_username=entry_username,
            )
            if (
                probe.username != entry_username
                or probe.login_username != entry_username
                or probe.history
                or len(probe.shell_stack) != 1
                or probe.shell_stack[0].username != entry_username
            ):
                raise LobbyCatalogError(
                    "profile does not seed a fresh authenticated login"
                )
            variables = probe.shell_environment.variables
            if any(variables.get(key) != entry_username for key in ("USER", "LOGNAME")):
                raise LobbyCatalogError(
                    "profile USER and LOGNAME must match entry_username; "
                    "use {username} templates or the configured account"
                )
            home = variables.get("HOME", "")
            if not home.startswith("/") or any(
                ord(character) < 32 or ord(character) == 127 for character in home
            ):
                raise LobbyCatalogError(
                    "profile HOME must be an absolute path without control characters"
                )
            return deepcopy(profile)
        except LobbyCatalogError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise LobbyCatalogError(
                "could not load a compatible saved profile; check its JSON, state, and file access"
            ) from exc

    def _read_profile(self, name: str) -> StateProfile:
        if not isinstance(name, str) or _PROFILE_NAME.fullmatch(name) is None:
            raise LobbyCatalogError("profile must be a saved profile name, not a path")
        try:
            path = self.store.path_for(name)
            if (
                not path.is_file()
                or path.is_symlink()
                or path.resolve().parent != self.store.directory.resolve()
            ):
                raise LobbyCatalogError(
                    "save the profile in the state profile directory before adding it"
                )
            data = read_bounded_file(
                path, max_bytes=MAX_PROFILE_BYTES, name="saved lobby profile"
            )
            profile = state_profile_from_document(json.loads(data))
            if profile.name != name:
                raise LobbyCatalogError(
                    "saved profile document name must match the catalog profile"
                )
            if profile.level not in {"world", "system"}:
                raise LobbyCatalogError(
                    "lobby worlds require a world/system profile; prepare a fresh-login profile instead of an exact snapshot"
                )
            if (
                profile.state.get("history")
                or profile.state.get("shell_stack")
                or profile.state.get("interactive_program")
            ):
                raise LobbyCatalogError(
                    "profile restores history or shell position; save a compatible world/system profile"
                )
            return profile
        except LobbyCatalogError:
            raise
        except (
            OSError,
            UnicodeError,
            ValueError,
            RecursionError,
            TypeError,
            KeyError,
            AttributeError,
        ) as exc:
            raise LobbyCatalogError(
                "could not load a compatible saved profile; check its JSON, state, and file access"
            ) from exc

    def _persist(self, entries: tuple[LobbyWorld, ...]) -> None:
        document = {
            "kind": CATALOG_KIND,
            "schema_version": CATALOG_VERSION,
            "worlds": [asdict(world) for world in entries],
        }
        text = json.dumps(document, indent=2, ensure_ascii=True) + "\n"
        if len(text.encode("utf-8")) > MAX_CATALOG_BYTES:
            raise LobbyCatalogError("encoded lobby catalog exceeds its file size limit")
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{self.path.name}.",
                dir=self.path.parent,
                delete=False,
            ) as stream:
                temporary = stream.name
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            raise LobbyCatalogError(
                "could not persist lobby catalog; check directory permissions and free space"
            ) from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
