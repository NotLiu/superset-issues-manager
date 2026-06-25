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
"""SQLite store for the issue-triage MVP.

A single SQLite file holds both the upstream issue corpus (with an FTS5 index
for cheap keyword candidate retrieval) and the automation metrics. No external
infrastructure is required.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DB_PATH = DATA_DIR / "triage.db"


def db_path() -> Path:
    """Resolve the SQLite path, allowing override via TRIAGE_DB env var."""
    override = os.environ.get("TRIAGE_DB")
    return Path(override) if override else DEFAULT_DB_PATH


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
-- Read-only corpus of upstream issues used for duplicate detection.
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

-- FTS5 keyword index over title+body for cheap candidate retrieval.
CREATE VIRTUAL TABLE IF NOT EXISTS issues_fts USING fts5(
    title,
    body,
    source_repo UNINDEXED,
    number UNINDEXED,
    tokenize = 'porter unicode61'
);

-- One row per triaged issue: the classifier's decision.
CREATE TABLE IF NOT EXISTS classifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    label       TEXT NOT NULL,          -- duplicate|auto-resolve|needs-review|invalid
    confidence  REAL,
    evidence    TEXT,
    matched     TEXT,                   -- JSON list of matched upstream issue numbers
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per action taken (incl. Devin auto-resolve runs).
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_repo   TEXT NOT NULL,
    issue_number  INTEGER NOT NULL,
    label         TEXT NOT NULL,
    action        TEXT NOT NULL,  -- comment|label|auto_resolve|none
    -- outcome: deflected|pr_opened|left_open|needs_human|error
    outcome       TEXT NOT NULL,
    devin_session_id TEXT,
    acu_cost      REAL,
    pr_url        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db(path: Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized triage DB at {db_path()}")
