#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Datastore for the issue-triage system.

Two interchangeable backends, selected at runtime:

* **Postgres** (shared, persistent) when ``TRIAGE_DATABASE_URL`` is set. This is
  the production store used by the live automation and the dashboard so the
  corpus and metrics persist across automation-triggered sessions.
* **SQLite** (zero-config, ephemeral) otherwise. Handy for offline CLI runs and
  local testing.

The store holds the upstream issue corpus (with a keyword index for cheap
candidate retrieval) and the automation metrics. All callers go through
``connect()`` and a uniform cursor shim, so the rest of the scripts are
backend-agnostic; the two places where SQL genuinely differs (the keyword index
and time bucketing) branch on ``is_postgres()``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DB_PATH = DATA_DIR / "triage.db"


def database_url() -> str | None:
    """Postgres DSN for the shared store, or None to use local SQLite."""
    url = os.environ.get("TRIAGE_DATABASE_URL")
    return url or None


def is_postgres() -> bool:
    return database_url() is not None


def db_path() -> Path:
    """Resolve the SQLite path, allowing override via TRIAGE_DB env var."""
    override = os.environ.get("TRIAGE_DB")
    return Path(override) if override else DEFAULT_DB_PATH


class Cursor:
    """Thin cursor wrapper exposing a uniform fetch API across backends."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def fetchone(self) -> Any:
        return self._raw.fetchone()

    def fetchall(self) -> list[Any]:
        return self._raw.fetchall()

    @property
    def lastrowid(self) -> int | None:
        return getattr(self._raw, "lastrowid", None)

    def __iter__(self) -> Any:
        return iter(self._raw.fetchall())


class Connection:
    """Backend-agnostic connection.

    ``execute`` accepts ``?`` placeholders for both backends (translated to
    ``%s`` for Postgres) and rows always support string-key access.
    """

    def __init__(self, raw: Any, backend: str) -> None:
        self._raw = raw
        self.backend = backend

    def execute(self, sql: str, params: Any = ()) -> Cursor:
        if self.backend == "postgres":
            cur = self._raw.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return Cursor(cur)
        return Cursor(self._raw.execute(sql, params))

    def executescript(self, sql: str) -> None:
        if self.backend == "postgres":
            with self._raw.cursor() as cur:
                cur.execute(sql)
            return
        self._raw.executescript(sql)

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()


def connect(path: Path | None = None) -> Connection:
    """Open a connection to the configured backend."""
    if is_postgres():
        import psycopg
        from psycopg.rows import dict_row

        url = database_url()
        assert url is not None
        raw = psycopg.connect(url, row_factory=dict_row)
        return Connection(raw, "postgres")

    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(target)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return Connection(raw, "sqlite")


# Corpus + metrics schema. The two backends diverge on the keyword index
# (SQLite FTS5 virtual table vs. a Postgres tsvector + GIN index) and on
# identity/timestamp defaults, so each gets its own DDL.

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS issues (
    source_repo TEXT NOT NULL,
    number      INTEGER NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    labels      TEXT NOT NULL DEFAULT '',
    html_url    TEXT NOT NULL,
    created_at  TEXT,
    closed_at   TEXT,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_repo, number)
);

CREATE VIRTUAL TABLE IF NOT EXISTS issues_fts USING fts5(
    title,
    body,
    source_repo UNINDEXED,
    number UNINDEXED,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS classifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    label       TEXT NOT NULL,
    confidence  REAL,
    evidence    TEXT,
    matched     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_repo   TEXT NOT NULL,
    issue_number  INTEGER NOT NULL,
    label         TEXT NOT NULL,
    action        TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    devin_session_id TEXT,
    acu_cost      REAL,
    pr_url        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS issues (
    source_repo TEXT NOT NULL,
    number      INTEGER NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    labels      TEXT NOT NULL DEFAULT '',
    html_url    TEXT NOT NULL,
    created_at  TEXT,
    closed_at   TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    search_tsv  tsvector,
    PRIMARY KEY (source_repo, number)
);

CREATE INDEX IF NOT EXISTS issues_search_tsv_idx
    ON issues USING GIN (search_tsv);

CREATE TABLE IF NOT EXISTS classifications (
    id          SERIAL PRIMARY KEY,
    target_repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    label       TEXT NOT NULL,
    confidence  DOUBLE PRECISION,
    evidence    TEXT,
    matched     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    id            SERIAL PRIMARY KEY,
    target_repo   TEXT NOT NULL,
    issue_number  INTEGER NOT NULL,
    label         TEXT NOT NULL,
    action        TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    devin_session_id TEXT,
    acu_cost      DOUBLE PRECISION,
    pr_url        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def init_db(path: Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA_PG if conn.backend == "postgres" else _SCHEMA_SQLITE)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    backend = "postgres" if is_postgres() else f"sqlite ({db_path()})"
    print(f"Initialized triage DB backend: {backend}")
