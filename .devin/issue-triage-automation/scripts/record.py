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
"""Record triage decisions/actions and compute dashboard metrics.

The Devin triage session calls `classification` and `run` to persist what it
decided and did; the dashboard reads `metrics()` / `timeseries()`.

Usage (from the session):
    python record.py classification --repo o/r --issue 5 --label duplicate \\
        --confidence 0.9 --matched 41001,40583 --evidence "same stack trace"
    python record.py run --repo o/r --issue 5 --label duplicate \\
        --action comment --outcome deflected
"""

# Standalone CLI tooling (not part of the superset package): stdlib json is
# intended; the timeseries query interpolates only a fixed internal date
# format string (no user input), so S608 is a false positive here.
# ruff: noqa: TID251, S608
from __future__ import annotations

import argparse
import json

from db import connect, init_db, is_postgres

MINUTES_SAVED_PER_ISSUE = 5  # estimate, surfaced as such in the dashboard


def classification(
    repo: str,
    issue: int,
    label: str,
    confidence: float | None,
    matched: list[int] | None,
    evidence: str | None,
) -> int:
    init_db()
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO classifications
               (target_repo, issue_number, label, confidence, evidence, matched)
               VALUES (?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (repo, issue, label, confidence, evidence, json.dumps(matched or [])),
        )
        new_id = int(cur.fetchone()["id"])
        conn.commit()
        return new_id
    finally:
        conn.close()


def run(
    repo: str,
    issue: int,
    label: str,
    action: str,
    outcome: str,
    devin_session_id: str | None = None,
    acu_cost: float | None = None,
    pr_url: str | None = None,
) -> int:
    init_db()
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO runs
               (target_repo, issue_number, label, action, outcome,
                devin_session_id, acu_cost, pr_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (repo, issue, label, action, outcome, devin_session_id, acu_cost, pr_url),
        )
        new_id = int(cur.fetchone()["id"])
        conn.commit()
        return new_id
    finally:
        conn.close()


def metrics() -> dict[str, object]:
    """Aggregate counters for the dashboard."""
    conn = connect()
    try:
        total_tracked = conn.execute(
            "SELECT COUNT(DISTINCT issue_number) AS n FROM classifications"
        ).fetchone()["n"]
        by_label = {
            r["label"]: r["n"]
            for r in conn.execute(
                "SELECT label, COUNT(*) n FROM classifications GROUP BY label"
            )
        }
        by_outcome = {
            r["outcome"]: r["n"]
            for r in conn.execute(
                "SELECT outcome, COUNT(*) n FROM runs GROUP BY outcome"
            )
        }
        acu_total = conn.execute(
            "SELECT COALESCE(SUM(acu_cost), 0) AS s FROM runs"
        ).fetchone()["s"]
        runs_with_acu = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE acu_cost IS NOT NULL"
        ).fetchone()["n"]
    finally:
        conn.close()

    deflected = by_outcome.get("deflected", 0)
    prs = by_outcome.get("pr_opened", 0)
    return {
        "total_issues_tracked": total_tracked,
        "issues_deflected": deflected,
        "prs_opened": prs,
        "left_open": by_outcome.get("left_open", 0),
        "resolved_after_human": by_outcome.get("needs_human", 0),
        "by_label": by_label,
        "by_outcome": by_outcome,
        "estimated_minutes_saved": (deflected + prs) * MINUTES_SAVED_PER_ISSUE,
        "estimated_acu_cost": round(acu_total, 3),
        "acu_per_issue": round(acu_total / runs_with_acu, 3) if runs_with_acu else 0,
    }


def timeseries(bucket: str = "day") -> list[dict[str, object]]:
    """Counts of classifications per time bucket (day or week)."""
    if is_postgres():
        fmt = "IYYY-IW" if bucket == "week" else "YYYY-MM-DD"
        sql = f"""SELECT to_char(created_at, '{fmt}') AS bucket,
                         label, COUNT(*) AS n
                  FROM classifications
                  GROUP BY bucket, label
                  ORDER BY bucket"""
    else:
        fmt = "%Y-%W" if bucket == "week" else "%Y-%m-%d"
        sql = f"""SELECT strftime('{fmt}', created_at) AS bucket,
                         label, COUNT(*) AS n
                  FROM classifications
                  GROUP BY bucket, label
                  ORDER BY bucket"""
    conn = connect()
    try:
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classification")
    c.add_argument("--repo", required=True)
    c.add_argument("--issue", type=int, required=True)
    c.add_argument("--label", required=True)
    c.add_argument("--confidence", type=float)
    c.add_argument("--matched", default="", help="comma-separated issue numbers")
    c.add_argument("--evidence", default="")

    r = sub.add_parser("run")
    r.add_argument("--repo", required=True)
    r.add_argument("--issue", type=int, required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--action", required=True)
    r.add_argument("--outcome", required=True)
    r.add_argument("--session", dest="devin_session_id")
    r.add_argument("--acu", dest="acu_cost", type=float)
    r.add_argument("--pr-url", dest="pr_url")

    m = sub.add_parser("metrics")
    m.add_argument("--json", action="store_true")

    sub.add_parser("timeseries").add_argument(
        "--bucket", default="day", choices=["day", "week"]
    )

    args = ap.parse_args()
    if args.cmd == "classification":
        matched = [int(x) for x in args.matched.split(",") if x.strip()]
        rid = classification(
            args.repo, args.issue, args.label, args.confidence, matched, args.evidence
        )
        print(f"recorded classification id={rid}")
    elif args.cmd == "run":
        rid = run(
            args.repo,
            args.issue,
            args.label,
            args.action,
            args.outcome,
            args.devin_session_id,
            args.acu_cost,
            args.pr_url,
        )
        print(f"recorded run id={rid}")
    elif args.cmd == "metrics":
        print(json.dumps(metrics(), indent=2))
    elif args.cmd == "timeseries":
        print(json.dumps(timeseries(args.bucket), indent=2))


if __name__ == "__main__":
    main()
