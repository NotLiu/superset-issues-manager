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
"""Ingest a bounded set of upstream issues into the SQLite corpus.

Pulls recent issues from a GitHub repo via the authenticated `gh` CLI, filters
out pull requests, and stores them with an FTS5 index for keyword retrieval. A
JSONL seed is also written so the corpus is reproducible without committing a
binary database.

Usage:
    python ingest.py --repo apache/superset --limit 300
"""

# Standalone CLI tooling (not part of the superset package): stdlib json and
# subprocess calls to the authenticated `gh` CLI are intended here.
# ruff: noqa: TID251, S603, S607
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from db import connect, DATA_DIR, init_db, is_postgres


def fetch_issues(repo: str, limit: int) -> list[dict[str, Any]]:
    """Fetch up to `limit` issues (newest first), excluding PRs, via gh API."""
    issues: list[dict[str, Any]] = []
    page = 1
    per_page = 100
    while len(issues) < limit:
        out = subprocess.run(
            [
                "gh",
                "api",
                "-X",
                "GET",
                f"/repos/{repo}/issues",
                "-f",
                "state=all",
                "-f",
                "sort=created",
                "-f",
                "direction=desc",
                "-f",
                f"per_page={per_page}",
                "-f",
                f"page={page}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        batch = json.loads(out.stdout)
        if not batch:
            break
        for it in batch:
            if "pull_request" in it:  # the issues endpoint also returns PRs
                continue
            issues.append(it)
            if len(issues) >= limit:
                break
        page += 1
    return issues


def fetch_issue(repo: str, number: int) -> dict[str, Any]:
    """Fetch a single issue by number via the gh API."""
    out = subprocess.run(
        ["gh", "api", f"/repos/{repo}/issues/{number}"],
        capture_output=True,
        text=True,
        check=True,
    )
    result: dict[str, Any] = json.loads(out.stdout)
    return result


def add_issue(repo: str, number: int) -> dict[str, Any]:
    """Append a single (new) issue to the shared corpus for future dedup."""
    row = normalize(repo, fetch_issue(repo, number))
    store([row])
    return row


def normalize(repo: str, it: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_repo": repo,
        "number": it["number"],
        "title": it.get("title") or "",
        "body": (it.get("body") or "")[:20000],
        "state": it.get("state") or "",
        "labels": ",".join(lbl["name"] for lbl in it.get("labels", [])),
        "html_url": it.get("html_url") or "",
        "created_at": it.get("created_at"),
        "closed_at": it.get("closed_at"),
    }


def _store_postgres(conn: Any, rows: list[dict[str, Any]]) -> None:
    for r in rows:
        conn.execute(
            """INSERT INTO issues
               (source_repo, number, title, body, state, labels, html_url,
                created_at, closed_at, search_tsv)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                       to_tsvector('english', ? || ' ' || ?))
               ON CONFLICT (source_repo, number) DO UPDATE SET
                   title = EXCLUDED.title,
                   body = EXCLUDED.body,
                   state = EXCLUDED.state,
                   labels = EXCLUDED.labels,
                   html_url = EXCLUDED.html_url,
                   created_at = EXCLUDED.created_at,
                   closed_at = EXCLUDED.closed_at,
                   search_tsv = EXCLUDED.search_tsv""",
            (
                r["source_repo"],
                r["number"],
                r["title"],
                r["body"],
                r["state"],
                r["labels"],
                r["html_url"],
                r["created_at"],
                r["closed_at"],
                r["title"],
                r["body"],
            ),
        )


def _store_sqlite(conn: Any, rows: list[dict[str, Any]]) -> None:
    for r in rows:
        conn.execute(
            """INSERT OR REPLACE INTO issues
               (source_repo, number, title, body, state, labels, html_url,
                created_at, closed_at)
               VALUES (:source_repo, :number, :title, :body, :state, :labels,
                       :html_url, :created_at, :closed_at)""",
            r,
        )
        conn.execute(
            "DELETE FROM issues_fts WHERE source_repo = ? AND number = ?",
            (r["source_repo"], r["number"]),
        )
        conn.execute(
            """INSERT INTO issues_fts (title, body, source_repo, number)
               VALUES (?, ?, ?, ?)""",
            (r["title"], r["body"], r["source_repo"], r["number"]),
        )


def store(rows: list[dict[str, Any]]) -> None:
    init_db()
    conn = connect()
    try:
        if is_postgres():
            _store_postgres(conn, rows)
        else:
            _store_sqlite(conn, rows)
        conn.commit()
    finally:
        conn.close()


def write_seed(repo: str, rows: list[dict[str, Any]]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    slug = repo.replace("/", "__")
    seed = DATA_DIR / f"corpus__{slug}.jsonl"
    with seed.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return seed


def load_seed(seed: Path) -> int:
    """Rebuild the corpus DB from a committed JSONL seed (no GitHub calls)."""
    rows = [json.loads(line) for line in seed.read_text().splitlines() if line.strip()]
    store(rows)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="apache/superset")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument(
        "--from-seed",
        metavar="PATH",
        help="rebuild the corpus from a JSONL seed instead of calling GitHub",
    )
    ap.add_argument(
        "--add-issue",
        type=int,
        metavar="N",
        help="append a single issue (by number) from --repo to the corpus",
    )
    args = ap.parse_args()

    if args.from_seed:
        n = load_seed(Path(args.from_seed))
        print(f"Loaded {n} issues into the corpus from {args.from_seed}")
        return

    if args.add_issue:
        row = add_issue(args.repo, args.add_issue)
        print(f"Appended issue #{row['number']} to the corpus: {row['title']}")
        return

    print(f"Fetching up to {args.limit} issues from {args.repo} ...")
    raw = fetch_issues(args.repo, args.limit)
    rows = [normalize(args.repo, it) for it in raw]
    store(rows)
    seed = write_seed(args.repo, rows)
    print(f"Ingested {len(rows)} issues into the corpus; seed written to {seed}")


if __name__ == "__main__":
    main()
