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
"""Cheap candidate retrieval over the issue corpus using SQLite FTS5.

Given free text (a new issue's title/body), return the top-K most similar
upstream issues by BM25 keyword relevance. This is the non-LLM retrieval step;
the Devin session then judges the small candidate set.

Usage:
    python search.py --text "dashboard fails to load charts" --k 5
    python search.py --json --text "..."        # machine-readable output
"""

# Standalone CLI tooling (not part of the superset package): stdlib json is
# intended here.
# ruff: noqa: TID251
from __future__ import annotations

import argparse
import json
import re

from db import connect, is_postgres

_TOKEN = re.compile(r"[A-Za-z0-9]+")


def _fts_query(text: str) -> str:
    """Build a safe OR-query of significant tokens for FTS5 MATCH."""
    tokens = [t.lower() for t in _TOKEN.findall(text) if len(t) > 2]
    seen: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.append(t)
    # Cap to keep the query cheap; quote each token to neutralize FTS syntax.
    return " OR ".join(f'"{t}"' for t in seen[:40])


def _search_postgres(text: str, k: int) -> list[dict[str, object]]:
    if not _TOKEN.search(text):
        return []
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT i.source_repo, i.number, i.title,
                   ts_rank(i.search_tsv, q) AS score,
                   i.state, i.labels, i.html_url
            FROM issues i, websearch_to_tsquery('english', ?) q
            WHERE i.search_tsv @@ q
            ORDER BY score DESC
            LIMIT ?
            """,
            (text, k),
        ).fetchall()
    finally:
        conn.close()
    # ts_rank returns higher=better.
    return [
        {
            "source_repo": r["source_repo"],
            "number": r["number"],
            "title": r["title"],
            "relevance": round(float(r["score"]), 4),
            "state": r["state"],
            "labels": r["labels"],
            "html_url": r["html_url"],
        }
        for r in rows
    ]


def _search_sqlite(text: str, k: int) -> list[dict[str, object]]:
    query = _fts_query(text)
    if not query:
        return []
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT f.source_repo, f.number, f.title,
                   bm25(issues_fts) AS score,
                   i.state, i.labels, i.html_url
            FROM issues_fts f
            JOIN issues i
              ON i.source_repo = f.source_repo AND i.number = f.number
            WHERE issues_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (query, k),
        ).fetchall()
    finally:
        conn.close()
    # bm25 returns lower=better; expose a positive relevance for readability.
    return [
        {
            "source_repo": r["source_repo"],
            "number": r["number"],
            "title": r["title"],
            "relevance": round(-r["score"], 3),
            "state": r["state"],
            "labels": r["labels"],
            "html_url": r["html_url"],
        }
        for r in rows
    ]


def search(text: str, k: int = 5) -> list[dict[str, object]]:
    """Top-K corpus issues by keyword relevance (backend-appropriate)."""
    return _search_postgres(text, k) if is_postgres() else _search_sqlite(text, k)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    results = search(args.text, args.k)
    if args.json:
        print(json.dumps(results, indent=2))
        return
    if not results:
        print("(no candidates)")
        return
    for r in results:
        print(f"#{r['number']:<6} rel={r['relevance']:<7} [{r['state']}] {r['title']}")
        print(f"        {r['html_url']}")


if __name__ == "__main__":
    main()
