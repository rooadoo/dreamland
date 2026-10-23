"""Trusted SSH visit identity and an operator-only durable reference index.

The SQLite index contains correlation/lifecycle information, never transcripts.
Only application-created runtime provenance is projected into observations.
"""

from __future__ import annotations

import logging
import math
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dreamland.models import RuntimeProvenance, SessionState

PUBLIC_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
PUBLIC_ID_METADATA_KEY = "dreamlandPublicId"
_PUBLIC_ID = re.compile(r"DL-(?:[2-9A-HJ-NP-Z]{4}-){3}[2-9A-HJ-NP-Z]{4}\Z")
_LOGGER = logging.getLogger("dreamland.lifecycle")


def is_public_reference(reference: str) -> bool:
    """Reserve the namespace before internal-ID prefix or fallback resolution."""
    return reference.strip().upper().startswith("DL")


def canonical_public_id(reference: str) -> str:
    canonical = reference.strip().upper()
    if not _PUBLIC_ID.fullmatch(canonical):
        raise ValueError("Use a complete public simulation ID: DL-XXXX-XXXX-XXXX-XXXX.")
    return canonical


def generate_public_id() -> str:
    return "DL-" + "-".join(
        "".join(secrets.choice(PUBLIC_ID_ALPHABET) for _ in range(4)) for _ in range(4)
    )


def public_trace_metadata(source: object) -> dict[str, str]:
    """Project a captured trusted identity; ignore every simulated-state key."""
    provenance = getattr(source, "runtime_provenance", None)
    if not isinstance(provenance, RuntimeProvenance):
        return {}
    return {PUBLIC_ID_METADATA_KEY: provenance.public_id}


@dataclass(frozen=True, slots=True)
class PublicSimulationRecord:
    public_id: str
    session_id: str | None
    profile_name: str
    parent_connection_id: str
    notice_version: str
    accepted_at: str
    allocated_at: str
    started_at: str | None
    ended_at: str | None
    status: str
    end_reason: str | None


class PublicSimulationRegistry:
    """One startup-owned index; transactions serialize allocation and binding.

    Construct once per server startup. Previously allocated/live records become
    interrupted on recovery. No recovered row can recreate a live session.
    Terminal rows expire after ``retention_days``; cleanup physically deletes
    them at startup, allocation, or an explicit operator call to ``cleanup``.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        retention_days: float = 30,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not math.isfinite(retention_days) or retention_days <= 0:
            raise ValueError("Public reference retention must be finite and positive.")
        self.path = Path(path)
        self.retention_days = retention_days
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or generate_public_id
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        info = self.path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
        ):
            raise ValueError(
                "Public reference index must be an owner-only regular file."
            )
        with self._transaction() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS visits (
                    public_id TEXT PRIMARY KEY,
                    session_id TEXT UNIQUE,
                    profile_name TEXT NOT NULL,
                    parent_connection_id TEXT NOT NULL,
                    notice_version TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    allocated_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    end_reason TEXT
                )"""
            )
            connection.execute(
                "UPDATE visits SET status='interrupted', ended_at=?, "
                "end_reason='process_restart' WHERE status IN ('allocated', 'live')",
                (self._now(),),
            )
        self.cleanup()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _now(self) -> str:
        return self._clock().astimezone(UTC).isoformat()

    def _cutoff(self) -> str:
        return (
            self._clock().astimezone(UTC) - timedelta(days=self.retention_days)
        ).isoformat()

    def allocate(
        self,
        profile_name: str,
        *,
        parent_connection_id: str,
        notice_version: str,
        accepted_at: str,
    ) -> PublicSimulationRecord:
        self.cleanup()
        with self._transaction() as connection:
            for _ in range(128):
                public_id = canonical_public_id(self._id_factory())
                record = PublicSimulationRecord(
                    public_id=public_id,
                    session_id=None,
                    profile_name=profile_name,
                    parent_connection_id=parent_connection_id,
                    notice_version=notice_version,
                    accepted_at=accepted_at,
                    allocated_at=self._now(),
                    started_at=None,
                    ended_at=None,
                    status="allocated",
                    end_reason=None,
                )
                try:
                    connection.execute(
                        "INSERT INTO visits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        tuple(
                            getattr(record, name)
                            for name in record.__dataclass_fields__
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                break
            else:
                raise RuntimeError("Unable to allocate a unique public simulation ID.")
        _LOGGER.info(
            "Lobby visit allocated public_id=%s profile=%s", public_id, profile_name
        )
        return record

    def bind(self, public_id: str, session: SessionState) -> PublicSimulationRecord:
        canonical = canonical_public_id(public_id)
        if session.runtime_provenance is not None:
            raise ValueError("Session already has runtime visit provenance.")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM visits WHERE public_id=?", (canonical,)
            ).fetchone()
            if row is None or row["status"] != "allocated":
                raise ValueError("Public simulation ID is unavailable for binding.")
            try:
                connection.execute(
                    "UPDATE visits SET session_id=?, started_at=?, status='live' "
                    "WHERE public_id=?",
                    (session.langfuse_trace_id, self._now(), canonical),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "Session already has a public simulation binding."
                ) from exc
            row = connection.execute(
                "SELECT * FROM visits WHERE public_id=?", (canonical,)
            ).fetchone()
            assert row is not None
            record = PublicSimulationRecord(**dict(row))
        session.runtime_provenance = RuntimeProvenance(
            public_id=record.public_id,
            session_id=session.langfuse_trace_id,
            profile_name=record.profile_name,
            parent_connection_id=record.parent_connection_id,
            notice_version=record.notice_version,
            accepted_at=record.accepted_at,
        )
        _LOGGER.info(
            "Lobby visit bound public_id=%s session=%s profile=%s",
            canonical,
            session.langfuse_trace_id,
            record.profile_name,
        )
        return record

    def get(self, reference: str) -> PublicSimulationRecord | None:
        canonical = canonical_public_id(reference)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM visits WHERE public_id=? "
                "AND (ended_at IS NULL OR ended_at>?)",
                (canonical, self._cutoff()),
            ).fetchone()
        return PublicSimulationRecord(**dict(row)) if row is not None else None

    def end(self, public_id: str, reason: str) -> PublicSimulationRecord | None:
        canonical = canonical_public_id(public_id)
        with self._transaction() as connection:
            connection.execute(
                "UPDATE visits SET status=CASE WHEN status='allocated' OR ?='failed_start' "
                "THEN 'failed' ELSE 'ended' END, ended_at=?, end_reason=? "
                "WHERE public_id=? AND status IN ('allocated', 'live')",
                (reason, self._now(), reason, canonical),
            )
            row = connection.execute(
                "SELECT * FROM visits WHERE public_id=?", (canonical,)
            ).fetchone()
        record = PublicSimulationRecord(**dict(row)) if row is not None else None
        if record is not None:
            _LOGGER.info(
                "Lobby visit ended public_id=%s session=%s reason=%s",
                canonical,
                record.session_id,
                record.end_reason,
            )
        return record

    def get_by_session_id(self, session_id: str) -> PublicSimulationRecord | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM visits WHERE session_id=? "
                "AND (ended_at IS NULL OR ended_at>?)",
                (session_id, self._cutoff()),
            ).fetchone()
        return PublicSimulationRecord(**dict(row)) if row is not None else None

    def cleanup(self) -> int:
        """Delete expired terminal references; live visits never expire in place."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM visits WHERE ended_at IS NOT NULL AND ended_at<=?",
                (self._cutoff(),),
            )
            return cursor.rowcount
