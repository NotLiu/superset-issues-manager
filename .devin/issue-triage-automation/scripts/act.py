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
"""Side-effecting GitHub actions for the triage branches (comment + label).

Thin wrappers over the authenticated `gh` CLI so the Devin session can act
deterministically. Pass --dry-run to preview without touching GitHub.

Usage:
    python act.py comment --repo o/r --issue 5 --body-file /tmp/c.md
    python act.py label   --repo o/r --issue 5 --labels duplicate,needs-triage
    python act.py dup-comment --repo o/r --issue 5 --matched-json /tmp/m.json
"""

# Standalone CLI tooling (not part of the superset package): stdlib json and
# subprocess calls to the authenticated `gh` CLI are intended here.
# ruff: noqa: TID251, S603, S607
from __future__ import annotations

import argparse
import json
import subprocess
import sys


def _gh(args: list[str], dry_run: bool) -> None:
    if dry_run:
        print("[dry-run] gh " + " ".join(args))
        return
    subprocess.run(["gh", *args], check=True)


def comment(repo: str, issue: int, body: str, dry_run: bool) -> None:
    _gh(
        ["issue", "comment", str(issue), "--repo", repo, "--body", body],
        dry_run,
    )


def label(repo: str, issue: int, labels: list[str], dry_run: bool) -> None:
    _gh(
        ["issue", "edit", str(issue), "--repo", repo, "--add-label", ",".join(labels)],
        dry_run,
    )


def render_dup_comment(matched: list[dict[str, object]], n: int = 3) -> str:
    top = matched[:n]
    lines = [
        "This issue looks like it may already be tracked. "
        f"The {len(top)} most similar existing issues:",
        "",
    ]
    for m in top:
        state = m.get("state", "")
        lines.append(f"- [#{m['number']}]({m['html_url']}) ({state}) — {m['title']}")
    lines += [
        "",
        "If one of these matches, please add any extra detail there and this "
        "issue can be closed as a duplicate. If none match, let us know and a "
        "maintainer will take a closer look.",
        "",
        "_Posted by the Superset issue-triage automation._",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("comment", "label", "dup-comment"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--issue", type=int, required=True)
        p.add_argument("--dry-run", action="store_true")
        if name == "comment":
            p.add_argument("--body")
            p.add_argument("--body-file")
        elif name == "label":
            p.add_argument("--labels", required=True)
        elif name == "dup-comment":
            p.add_argument("--matched-json", required=True)
            p.add_argument("--n", type=int, default=3)

    args = ap.parse_args()
    if args.cmd == "comment":
        body = args.body
        if args.body_file:
            with open(args.body_file) as fh:
                body = fh.read()
        if not body:
            sys.exit("comment requires --body or --body-file")
        comment(args.repo, args.issue, body, args.dry_run)
    elif args.cmd == "label":
        label(args.repo, args.issue, args.labels.split(","), args.dry_run)
    elif args.cmd == "dup-comment":
        with open(args.matched_json) as fh:
            matched = json.load(fh)
        body = render_dup_comment(matched, args.n)
        comment(args.repo, args.issue, body, args.dry_run)


if __name__ == "__main__":
    main()
