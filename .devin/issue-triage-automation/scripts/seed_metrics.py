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
"""Seed the metrics store with a representative demo funnel.

Inserts a balanced set of triage classifications + runs by calling the frozen
`record.classification()` / `record.run()` helpers (never raw SQL), so the
dashboard renders a non-empty, realistic funnel offline.

Usage (from the scripts/ dir):
    python seed_metrics.py          # populate demo rows, print a summary

The seeder is also importable: `from seed_metrics import seed; seed()` (used by
`dashboard.py --demo-seed`). It is safe to run repeatedly for a demo; each run
appends a fresh batch of representative rows.
"""

# Standalone CLI tooling (not part of the superset package): stdlib json is
# intended here.
# ruff: noqa: TID251
from __future__ import annotations

import json
from typing import Any

import record

REPO = "apache/superset"

# Representative, balanced label mix (do NOT over-weight auto-resolve):
#   duplicate x4, needs-review x3, invalid x2, auto-resolve x2
_SEED_ROWS: list[dict[str, Any]] = [
    # duplicate -> comment top matches, deflected
    {
        "issue": 41001,
        "label": "duplicate",
        "confidence": 0.93,
        "matched": [40583, 39912],
        "evidence": "same stack trace as #40583",
        "action": "comment",
        "outcome": "deflected",
    },
    {
        "issue": 41002,
        "label": "duplicate",
        "confidence": 0.88,
        "matched": [38211],
        "evidence": "duplicate of dashboard filter bug #38211",
        "action": "comment",
        "outcome": "deflected",
    },
    {
        "issue": 41003,
        "label": "duplicate",
        "confidence": 0.91,
        "matched": [40771, 40012],
        "evidence": "reported previously in #40771",
        "action": "comment",
        "outcome": "deflected",
    },
    {
        "issue": 41004,
        "label": "duplicate",
        "confidence": 0.86,
        "matched": [37650],
        "evidence": "same SQL Lab autocomplete request as #37650",
        "action": "comment",
        "outcome": "deflected",
    },
    # invalid -> request info / comment, deflected
    {
        "issue": 41010,
        "label": "invalid",
        "confidence": 0.79,
        "matched": [],
        "evidence": "no reproduction steps; not a Superset bug",
        "action": "comment",
        "outcome": "deflected",
    },
    {
        "issue": 41011,
        "label": "invalid",
        "confidence": 0.74,
        "matched": [],
        "evidence": "support question, belongs on Slack/discussions",
        "action": "comment",
        "outcome": "deflected",
    },
    # needs-review -> label, left open (one escalates to a human)
    {
        "issue": 41020,
        "label": "needs-review",
        "confidence": 0.55,
        "matched": [],
        "evidence": "plausible bug, needs maintainer triage",
        "action": "label",
        "outcome": "left_open",
    },
    {
        "issue": 41021,
        "label": "needs-review",
        "confidence": 0.58,
        "matched": [],
        "evidence": "feature request, needs product input",
        "action": "label",
        "outcome": "left_open",
    },
    {
        "issue": 41022,
        "label": "needs-review",
        "confidence": 0.49,
        "matched": [],
        "evidence": "ambiguous; routed to a human reviewer",
        "action": "label",
        "outcome": "needs_human",
    },
    # auto-resolve -> dependency-CVE fix, draft PR opened
    {
        "issue": 41030,
        "label": "auto-resolve",
        "confidence": 0.95,
        "matched": [],
        "evidence": "flask pinned to CVE-2026-27205 version; safe bump",
        "action": "auto_resolve",
        "outcome": "pr_opened",
        "devin_session_id": "devin-seed-aaaa1111",
        "acu_cost": 1.8,
        "pr_url": "https://github.com/apache/superset/pull/99001",
    },
    {
        "issue": 41031,
        "label": "auto-resolve",
        "confidence": 0.92,
        "matched": [],
        "evidence": "vulnerable transitive pin surfaced by OSV; minimal recompile",
        "action": "auto_resolve",
        "outcome": "pr_opened",
        "devin_session_id": "devin-seed-bbbb2222",
        "acu_cost": 1.6,
        "pr_url": "https://github.com/apache/superset/pull/99002",
    },
]


def seed() -> dict[str, int]:
    """Insert the demo funnel via record.* and return a label-count summary."""
    by_label: dict[str, int] = {}
    for row in _SEED_ROWS:
        record.classification(
            repo=REPO,
            issue=int(row["issue"]),
            label=str(row["label"]),
            confidence=row.get("confidence"),
            matched=row.get("matched"),
            evidence=row.get("evidence"),
        )
        record.run(
            repo=REPO,
            issue=int(row["issue"]),
            label=str(row["label"]),
            action=str(row["action"]),
            outcome=str(row["outcome"]),
            devin_session_id=row.get("devin_session_id"),
            acu_cost=row.get("acu_cost"),
            pr_url=row.get("pr_url"),
        )
        label = str(row["label"])
        by_label[label] = by_label.get(label, 0) + 1
    return by_label


def main() -> None:
    by_label = seed()
    total = sum(by_label.values())
    print(f"Seeded {total} demo issues across labels:")
    print(json.dumps(by_label, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
