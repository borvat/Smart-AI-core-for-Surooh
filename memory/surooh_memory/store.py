"""SQLite persistence for Surooh Memory.

All SQL lives in this module so a Postgres adapter can later implement the same
``Store`` surface without touching business rules in ``service.py``.

Erasure: the DB runs with ``secure_delete`` and the FTS index with its own
``secure-delete`` option, and the WAL is checkpointed after a forget, so
forgotten text is overwritten on disk rather than left in free pages.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT NOT NULL UNIQUE,
    company_id       TEXT NOT NULL,
    project_id       TEXT NOT NULL,
    subject_key      TEXT,
    kind             TEXT NOT NULL,
    text             TEXT NOT NULL,
    norm_text        TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    speaker          TEXT NOT NULL CHECK (speaker IN ('user', 'assistant_suggestion')),
    status           TEXT NOT NULL CHECK (status IN ('active', 'proposed', 'superseded')),
    supersedes       TEXT,
    superseded_by    TEXT,
    source_assistant TEXT NOT NULL,
    source_ref       TEXT NOT NULL,
    principal        TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS facts_scope ON facts (company_id, project_id, status);
CREATE INDEX IF NOT EXISTS facts_hash ON facts (company_id, project_id, content_hash);
CREATE INDEX IF NOT EXISTS facts_subject ON facts (company_id, project_id, subject_key);
-- At most one active fact per subject in a scope.
CREATE UNIQUE INDEX IF NOT EXISTS facts_one_active_subject
    ON facts (company_id, project_id, subject_key)
    WHERE status = 'active' AND subject_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS tombstones (
    company_id   TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    forgotten_at TEXT NOT NULL,
    PRIMARY KEY (company_id, project_id, content_hash)
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    norm_text,
    fact_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

_FACT_COLUMNS = (
    "seq, id, company_id, project_id, subject_key, kind, text, speaker, status, "
    "supersedes, superseded_by, source_assistant, source_ref, principal, created_at, updated_at, content_hash"
)


@dataclass(frozen=True)
class Fact:
    seq: int
    id: str
    company_id: str
    project_id: str
    subject_key: str | None
    kind: str
    text: str
    speaker: str
    status: str
    supersedes: str | None
    superseded_by: str | None
    source_assistant: str
    source_ref: str
    principal: str
    created_at: str
    updated_at: str
    content_hash: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "company_id": self.company_id,
            "project_id": self.project_id,
            "subject_key": self.subject_key,
            "kind": self.kind,
            "text": self.text,
            "speaker": self.speaker,
            "status": self.status,
            "approved": self.speaker == "user" and self.status == "active",
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "source_assistant": self.source_assistant,
            "source_ref": self.source_ref,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row_to_fact(row: sqlite3.Row) -> Fact:
    return Fact(**{k: row[k] for k in row.keys()})


class Store:
    """Thread-safe repository over one SQLite file. One writer at a time."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA secure_delete = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute("INSERT INTO facts_fts(facts_fts, rank) VALUES ('secure-delete', 1)")
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- transactions -----------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """Serialized write transaction (BEGIN IMMEDIATE)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @contextmanager
    def reading(self) -> Iterator["Store"]:
        with self._lock:
            yield self

    def ping(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    # -- reads --------------------------------------------------------------

    def get(self, fact_id: str) -> Fact | None:
        row = self._conn.execute(f"SELECT {_FACT_COLUMNS} FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return _row_to_fact(row) if row else None

    def find_live_by_hash(self, company_id: str, project_id: str, content_hash: str) -> Fact | None:
        row = self._conn.execute(
            f"SELECT {_FACT_COLUMNS} FROM facts WHERE company_id = ? AND project_id = ? "
            "AND content_hash = ? AND status IN ('active', 'proposed') ORDER BY seq DESC LIMIT 1",
            (company_id, project_id, content_hash),
        ).fetchone()
        return _row_to_fact(row) if row else None

    def active_for_subject(self, company_id: str, project_id: str, subject_key: str) -> Fact | None:
        row = self._conn.execute(
            f"SELECT {_FACT_COLUMNS} FROM facts WHERE company_id = ? AND project_id = ? "
            "AND subject_key = ? AND status = 'active'",
            (company_id, project_id, subject_key),
        ).fetchone()
        return _row_to_fact(row) if row else None

    def proposed_for_subject(self, company_id: str, project_id: str, subject_key: str) -> list[Fact]:
        rows = self._conn.execute(
            f"SELECT {_FACT_COLUMNS} FROM facts WHERE company_id = ? AND project_id = ? "
            "AND subject_key = ? AND status = 'proposed'",
            (company_id, project_id, subject_key),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]

    def is_tombstoned(self, company_id: str, project_id: str, content_hash: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM tombstones WHERE company_id = ? AND project_id = ? AND content_hash = ?",
                (company_id, project_id, content_hash),
            ).fetchone()
            is not None
        )

    def live_facts(
        self,
        company_id: str,
        project_ids: list[str] | None,
        fts_query: str | None,
        limit: int,
    ) -> list[Fact]:
        """Active + proposed facts in scope, best match (or newest) first.

        ``project_ids=None`` means every project of the company; callers must
        only pass that when the principal may read the whole company.
        """
        params: list[Any] = []
        where = ["f.company_id = ?", "f.status IN ('active', 'proposed')"]
        params.append(company_id)
        if project_ids is not None:
            if not project_ids:
                return []
            where.append(f"f.project_id IN ({','.join('?' * len(project_ids))})")
            params.extend(project_ids)
        if fts_query:
            sql = (
                f"SELECT {', '.join('f.' + c.strip() for c in _FACT_COLUMNS.split(','))} "
                "FROM facts_fts JOIN facts f ON f.id = facts_fts.fact_id "
                f"WHERE facts_fts MATCH ? AND {' AND '.join(where)} "
                "ORDER BY bm25(facts_fts), f.seq DESC LIMIT ?"
            )
            params = [fts_query, *params, limit]
        else:
            sql = (
                f"SELECT {', '.join('f.' + c.strip() for c in _FACT_COLUMNS.split(','))} FROM facts f "
                f"WHERE {' AND '.join(where)} ORDER BY f.seq DESC LIMIT ?"
            )
            params.append(limit)
        return [_row_to_fact(r) for r in self._conn.execute(sql, params).fetchall()]

    def all_facts(self, company_id: str, project_ids: list[str] | None) -> list[Fact]:
        params: list[Any] = [company_id]
        sql = f"SELECT {_FACT_COLUMNS} FROM facts WHERE company_id = ?"
        if project_ids is not None:
            if not project_ids:
                return []
            sql += f" AND project_id IN ({','.join('?' * len(project_ids))})"
            params.extend(project_ids)
        sql += " ORDER BY seq"
        return [_row_to_fact(r) for r in self._conn.execute(sql, params).fetchall()]

    def tombstone_count(self, company_id: str, project_ids: list[str] | None) -> int:
        params: list[Any] = [company_id]
        sql = "SELECT COUNT(*) FROM tombstones WHERE company_id = ?"
        if project_ids is not None:
            if not project_ids:
                return 0
            sql += f" AND project_id IN ({','.join('?' * len(project_ids))})"
            params.extend(project_ids)
        return int(self._conn.execute(sql, params).fetchone()[0])

    # -- writes (call inside transaction()) ---------------------------------

    def insert(self, *, norm_text: str, **fields: Any) -> Fact:
        cols = list(fields.keys()) + ["norm_text"]
        vals = list(fields.values()) + [norm_text]
        self._conn.execute(
            f"INSERT INTO facts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
        )
        self._conn.execute(
            "INSERT INTO facts_fts (norm_text, fact_id) VALUES (?, ?)", (norm_text, fields["id"])
        )
        fact = self.get(fields["id"])
        assert fact is not None
        return fact

    def set_status(self, fact_id: str, status: str, updated_at: str, *, superseded_by: str | None = None,
                   supersedes: str | None = None, speaker: str | None = None,
                   subject_key: str | None = None,
                   source_assistant: str | None = None, source_ref: str | None = None) -> None:
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, updated_at]
        for col, val in (("superseded_by", superseded_by), ("supersedes", supersedes), ("speaker", speaker),
                         ("subject_key", subject_key),
                         ("source_assistant", source_assistant), ("source_ref", source_ref)):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        params.append(fact_id)
        self._conn.execute(f"UPDATE facts SET {', '.join(sets)} WHERE id = ?", params)

    def erase(self, fact: Fact, forgotten_at: str) -> None:
        """Delete the fact's content everywhere and leave only a hash tombstone."""
        self._conn.execute("DELETE FROM facts_fts WHERE fact_id = ?", (fact.id,))
        self._conn.execute("UPDATE facts SET supersedes = NULL WHERE supersedes = ?", (fact.id,))
        self._conn.execute("UPDATE facts SET superseded_by = NULL WHERE superseded_by = ?", (fact.id,))
        self._conn.execute("DELETE FROM facts WHERE id = ?", (fact.id,))
        self._conn.execute(
            "INSERT OR IGNORE INTO tombstones (company_id, project_id, content_hash, forgotten_at) "
            "VALUES (?, ?, ?, ?)",
            (fact.company_id, fact.project_id, fact.content_hash, forgotten_at),
        )

    def checkpoint(self) -> None:
        """Flush the WAL into the main file so erased pages are overwritten there too."""
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
