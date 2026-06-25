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
"""Triage orchestrator: classify a new issue and route + record the outcome.

This is the CLI trigger that simulates "on issue created". It retrieves cheap
FTS5 candidates (`search`), classifies the issue into one of four labels, routes
the decision through the side-effecting GitHub wrappers (`act`, honoring
`--dry-run`), and persists the decision + action to the metrics store
(`record`). The classifier is pluggable:

- ``heuristic`` (default): offline, deterministic keyword rules.
- ``devin-session``: the Devin session itself is the classifier. With no
  decision supplied, this prints a ``classification_request`` for the session to
  judge; the session then re-invokes with ``--decision-json`` to route + record.

Usage:
    python triage.py --text "ChunkLoadError loading lazy bundle" --dry-run
    python triage.py --issue 7 --repo NotLiu/superset-issues-manager --json
    python triage.py --text "..." --classifier devin-session   # prints request
"""

# Standalone CLI tooling (not part of the superset package): stdlib json and a
# subprocess call to the authenticated `gh` CLI (to fetch a real issue) are
# intended here.
# ruff: noqa: TID251, S603, S607
from __future__ import annotations

import argparse
import json
import re
import subprocess
from typing import Any

import act
import record
from search import search

LABELS = ("duplicate", "auto-resolve", "needs-review", "invalid")

# auto-resolve precedence signals (case-insensitive).
_AUTO_RESOLVE_PATTERNS = (
    r"cve-\d",
    r"vulnerabilit",
    r"dependenc",
    r"\bbump\b",
)

# Signals that an issue is an actionable bug report (absence ⇒ invalid).
_ACTIONABLE_PATTERNS = (
    r"reproduc",
    r"\bsteps\b",
    r"version",
    r"\b\d+\.\d+(\.\d+)?\b",
    r"\blogs?\b",
    r"traceback",
    r"stack trace",
    r"\berror\b",
    r"exception",
)

_SHORT_TEXT_CHARS = 200

_INVALID_BODY = (
    "Thanks for the report! To help us triage this, please provide:\n"
    "\n"
    "- **Steps to reproduce** the problem\n"
    "- The **Superset version** you are running\n"
    "- Any relevant **logs / traceback**\n"
    "\n"
    "Once that detail is added a maintainer can take a closer look.\n"
    "\n"
    "_Posted by the Superset issue-triage automation._"
)

_AUTO_RESOLVE_NOTICE = (
    "auto-resolve: applied the `auto-resolve` label only. Automated resolution "
    "is reserved for a future resolver and is not yet implemented."
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def fetch_issue(repo: str, number: int) -> tuple[int, str]:
    """Fetch a real issue from the fork and build its classification text."""
    out = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,labels",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data: dict[str, Any] = json.loads(out.stdout)
    title = data.get("title") or ""
    body = data.get("body") or ""
    resolved = int(data.get("number") or number)
    return resolved, f"{title}\n\n{body}"


def classify_heuristic(
    text: str,
    candidates: list[dict[str, Any]],
    dup_floor: float,
    dup_dominance: float,
) -> dict[str, Any]:
    """Deterministic, offline classifier (see module docstring / contract)."""
    low = text.lower()

    # 1. auto-resolve: dependency / CVE / vulnerability / bump signals.
    if any(re.search(p, low) for p in _AUTO_RESOLVE_PATTERNS):
        return {
            "label": "auto-resolve",
            "confidence": 0.9,
            "matched": [],
            "evidence": "matched dependency/CVE/vulnerability/bump signal",
        }

    # 2. duplicate: a dominant top candidate above the relevance floor.
    if candidates:
        top = candidates[0]
        top_rel = float(top["relevance"])
        if top_rel >= dup_floor:
            if len(candidates) == 1:
                dominant = True
                confidence = 0.95
            else:
                second_rel = float(candidates[1]["relevance"])
                ratio = top_rel / second_rel if second_rel else 1e9
                dominant = top_rel >= dup_dominance * second_rel
                confidence = _clamp(0.6 + 0.08 * (ratio - dup_dominance), 0.6, 0.95)
            if dominant:
                matched = [int(c["number"]) for c in candidates[:3]]
                return {
                    "label": "duplicate",
                    "confidence": round(confidence, 3),
                    "matched": matched,
                    "evidence": (f"closest match #{top['number']}: {top['title']}"),
                }

    # 3. invalid: short text with no actionable bug-report signals.
    has_signal = any(re.search(p, low) for p in _ACTIONABLE_PATTERNS)
    if not has_signal and len(text) < _SHORT_TEXT_CHARS:
        return {
            "label": "invalid",
            "confidence": 0.7,
            "matched": [],
            "evidence": "no repro/version/logs",
        }

    # 4. fallback: route to a human.
    matched = [int(c["number"]) for c in candidates[:3]]
    return {
        "label": "needs-review",
        "confidence": 0.4,
        "matched": matched,
        "evidence": "no dominant duplicate; routed to human review",
    }


def _parse_decision(raw: str) -> dict[str, Any]:
    decision: dict[str, Any] = json.loads(raw)
    if (label := decision.get("label")) not in LABELS:
        raise SystemExit(f"decision label must be one of {LABELS}, got {label!r}")
    decision.setdefault("confidence", None)
    decision.setdefault("matched", [])
    decision.setdefault("evidence", "")
    decision["matched"] = [int(n) for n in decision.get("matched") or []]
    return decision


def _candidates_by_number(
    candidates: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    return {int(c["number"]): c for c in candidates}


def route(
    repo: str,
    issue: int,
    decision: dict[str, Any],
    candidates: list[dict[str, Any]],
    dry_run: bool,
) -> tuple[str, str]:
    """Execute the side-effects for a decision. Returns (action, outcome)."""
    label = decision["label"]
    matched = decision["matched"]
    by_number = _candidates_by_number(candidates)

    if label == "duplicate":
        top3 = [by_number[n] for n in matched[:3] if n in by_number]
        body = act.render_dup_comment(top3, 3)
        act.comment(repo, issue, body, dry_run)
        act.label(repo, issue, ["duplicate"], dry_run)
        return "comment", "deflected"

    if label == "invalid":
        act.comment(repo, issue, _INVALID_BODY, dry_run)
        act.label(repo, issue, ["invalid"], dry_run)
        return "comment", "deflected"

    if label == "needs-review":
        act.label(repo, issue, ["needs-review"], dry_run)
        return "label", "needs_human"

    if label == "auto-resolve":
        act.label(repo, issue, ["auto-resolve"], dry_run)
        print(_AUTO_RESOLVE_NOTICE)
        return "label", "left_open"

    raise SystemExit(f"unroutable label: {label!r}")


def _matched_dicts(
    matched: list[int], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_number = _candidates_by_number(candidates)
    out: list[dict[str, Any]] = []
    for n in matched:
        c = by_number.get(n)
        if c is None:
            continue
        out.append(
            {
                "number": c["number"],
                "title": c["title"],
                "html_url": c["html_url"],
                "relevance": c["relevance"],
                "state": c["state"],
            }
        )
    return out


def _print_summary(result: dict[str, Any]) -> None:
    print(f"repo:       {result['repo']}")
    print(f"issue:      #{result['issue']}")
    print(f"classifier: {result['classifier']}")
    print(f"label:      {result['label']} (confidence {result['confidence']})")
    print(f"evidence:   {result['evidence']}")
    print(f"action:     {result['action']} / outcome: {result['outcome']}")
    print(f"candidates considered: {result['candidates_considered']}")
    if matched := result["matched"]:
        print("matched:")
        for m in matched:
            print(f"  - #{m['number']} (rel={m['relevance']}) {m['title']}")
            print(f"    {m['html_url']}")


def triage(
    repo: str,
    issue: int,
    text: str,
    classifier: str,
    k: int,
    dup_floor: float,
    dup_dominance: float,
    decision: dict[str, Any] | None,
    dry_run: bool,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = [dict(c) for c in search(text, k)]

    if classifier == "devin-session":
        if decision is None:
            request = {
                "classification_request": {
                    "repo": repo,
                    "issue": issue,
                    "text": text,
                    "candidates": candidates,
                }
            }
            print(json.dumps(request, indent=2))
            return request
        chosen = decision
    else:
        chosen = classify_heuristic(text, candidates, dup_floor, dup_dominance)

    action, outcome = route(repo, issue, chosen, candidates, dry_run)

    record.classification(
        repo,
        issue,
        chosen["label"],
        chosen["confidence"],
        chosen["matched"],
        chosen["evidence"],
    )
    record.run(repo, issue, chosen["label"], action, outcome)

    return {
        "repo": repo,
        "issue": issue,
        "classifier": classifier,
        "label": chosen["label"],
        "confidence": chosen["confidence"],
        "matched": _matched_dicts(chosen["matched"], candidates),
        "evidence": chosen["evidence"],
        "action": action,
        "outcome": outcome,
        "candidates_considered": len(candidates),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Triage a new issue.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--issue", type=int, help="issue number to fetch via gh")
    src.add_argument("--text", help="raw issue text (offline testing)")
    ap.add_argument("--repo", default="NotLiu/superset-issues-manager")
    ap.add_argument(
        "--classifier", choices=["heuristic", "devin-session"], default="heuristic"
    )
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--dup-floor", type=float, default=12.0)
    ap.add_argument("--dup-dominance", type=float, default=2.0)
    ap.add_argument("--decision-json", help="decision object as a JSON string")
    ap.add_argument("--decision-file", help="path to a JSON decision file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="emit the full result object")
    args = ap.parse_args()

    if args.issue is not None:
        issue, text = fetch_issue(args.repo, args.issue)
    else:
        issue, text = 0, args.text

    decision: dict[str, Any] | None = None
    raw_decision: str | None = args.decision_json
    if args.decision_file:
        with open(args.decision_file) as fh:
            raw_decision = fh.read()
    if raw_decision:
        decision = _parse_decision(raw_decision)

    result = triage(
        args.repo,
        issue,
        text,
        args.classifier,
        args.k,
        args.dup_floor,
        args.dup_dominance,
        decision,
        args.dry_run,
    )

    # The devin-session request path already printed its payload and did not
    # route/record, so there is no result object to summarize.
    if "classification_request" in result:
        return

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_summary(result)


if __name__ == "__main__":
    main()
