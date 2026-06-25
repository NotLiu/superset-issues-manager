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
"""Auto-resolve branch: open a draft PR for a fix the session implemented.

This is the deterministic plumbing for the ``auto-resolve`` label — the
headline ``classify -> reproduce -> plan -> fix -> draft PR`` loop. The *fix*
itself is performed by the Devin triage session (it scans the codebase, plans,
and edits files), exactly as the classification is the session's own judgment.
This module only turns those working-tree edits into a reviewable **draft PR**
and records the run, so the side effects are deterministic and idempotent.

``open-pr`` (run from ``scripts/`` after the fix is in the working tree):
  1. assert the named ``--files`` actually changed (else nothing to PR),
  2. create a fresh branch, ``git add`` exactly those files, commit, push,
  3. open a **draft** PR via ``gh pr create --draft`` (never auto-merge),
  4. comment the PR link on the issue + apply the ``auto-resolve`` label,
  5. record the run as ``action=auto_resolve, outcome=pr_opened, pr_url=...``
     (or ``outcome=error`` if any step fails).

Draft-PR-only and the issue is never closed (honoring the locked guardrails).
Pass ``--dry-run`` to print every git/gh command without touching git, GitHub,
or the metrics store.

Usage:
    python resolve.py open-pr --repo NotLiu/superset-issues-manager --issue 7 \\
        --files "superset/foo.py superset/bar.py" \\
        --title "fix: guard null dataset in chart export (#7)" \\
        --body-file /tmp/pr_body.md --session devin-abc --acu 1.7
"""

# Standalone CLI tooling (not part of the superset package): stdlib json and
# subprocess calls to the authenticated `git` and `gh` CLIs are intended here.
# ruff: noqa: TID251, S603, S607
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import act
import record

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REPO = "NotLiu/superset-issues-manager"
DEFAULT_BASE = "master"
LABEL = "auto-resolve"


def _split_files(raw: str) -> list[str]:
    """Accept files as comma- or whitespace-separated; drop blanks."""
    parts = raw.replace(",", " ").split()
    return [p for p in parts if p.strip()]


def _git(args: list[str], dry_run: bool, capture: bool = False) -> str:
    cmd = ["git", "-C", str(REPO_ROOT), *args]
    if dry_run:
        print("[dry-run] " + " ".join(cmd))
        return ""
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _changed_files(files: list[str], dry_run: bool) -> list[str]:
    """Return the subset of ``files`` with staged/unstaged changes."""
    if dry_run:
        return files
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", *files],
        capture_output=True,
        text=True,
        check=True,
    )
    # Each porcelain line is "XY <path>"; collect the trailing paths.
    changed = [ln[3:].strip() for ln in out.stdout.splitlines() if ln.strip()]
    return changed


def _commit(title: str, issue: int, dry_run: bool) -> None:
    message = f"{title}\n\nAuto-resolve draft PR for issue #{issue}."
    try:
        _git(["commit", "-m", message], dry_run)
    except subprocess.CalledProcessError:
        # A pre-commit hook may have reformatted the staged files and aborted
        # the commit; re-stage the same paths and retry once.
        _git(["add", "-u"], dry_run)
        _git(["commit", "-m", message], dry_run)


def _create_pr(
    repo: str, base: str, branch: str, title: str, body: str, dry_run: bool
) -> str:
    cmd = [
        "gh",
        "pr",
        "create",
        "--repo",
        repo,
        "--base",
        base,
        "--head",
        branch,
        "--draft",
        "--title",
        title,
        "--body",
        body,
    ]
    if dry_run:
        print("[dry-run] " + " ".join(cmd))
        return f"https://github.com/{repo}/pull/<draft>"
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    # gh prints the new PR URL on its own line.
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("https://"):
            return line
    return out.stdout.strip()


def _pr_comment(pr_url: str) -> str:
    return (
        "The Superset issue-triage automation classified this issue as "
        f"`{LABEL}` and opened a **draft** PR with a proposed fix: {pr_url}\n"
        "\n"
        "This PR is a draft — a maintainer should review (and run CI) before "
        "merging. The automation never auto-merges and never closes the issue.\n"
        "\n"
        "_Posted by the Superset issue-triage automation._"
    )


def open_pr(
    repo: str,
    issue: int,
    files: list[str],
    title: str,
    body: str,
    base: str,
    branch: str | None,
    session: str | None,
    acu: float | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Open a draft PR for the working-tree fix and record the run."""
    if not files:
        raise SystemExit("open-pr requires at least one --files path")

    changed = _changed_files(files, dry_run)
    if not changed:
        raise SystemExit(
            "no changes detected in the named files — implement the fix before "
            "running open-pr (nothing to put in a PR)"
        )

    branch = branch or f"devin/{int(time.time())}-triage-issue-{issue}"

    try:
        _git(["checkout", "-b", branch], dry_run)
        _git(["add", "--", *files], dry_run)
        _commit(title, issue, dry_run)
        _git(["push", "-u", "origin", branch], dry_run)
        pr_url = _create_pr(repo, base, branch, title, body, dry_run)

        act.comment(repo, issue, _pr_comment(pr_url), dry_run)
        act.label(repo, issue, [LABEL], dry_run)
    except subprocess.CalledProcessError as exc:
        if not dry_run:
            record.run(repo, issue, LABEL, "auto_resolve", "error", session, acu, None)
        detail = exc.stderr or str(exc)
        raise SystemExit(
            f"auto-resolve failed; recorded outcome=error: {detail}"
        ) from exc

    if not dry_run:
        record.run(
            repo, issue, LABEL, "auto_resolve", "pr_opened", session, acu, pr_url
        )

    return {
        "repo": repo,
        "issue": issue,
        "branch": branch,
        "base": base,
        "files": changed,
        "pr_url": pr_url,
        "outcome": "pr_opened",
        "dry_run": dry_run,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Auto-resolve draft-PR flow.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("open-pr", help="open a draft PR for the implemented fix")
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument(
        "--files",
        required=True,
        help="changed file paths (comma- or space-separated), repo-relative",
    )
    p.add_argument("--title", required=True, help="PR title")
    p.add_argument("--body", help="PR description body")
    p.add_argument("--body-file", help="path to a file holding the PR body")
    p.add_argument("--base", default=DEFAULT_BASE, help="base branch (default master)")
    p.add_argument("--branch", help="head branch name (default: auto-generated)")
    p.add_argument("--session", help="Devin session id, recorded with the run")
    p.add_argument("--acu", type=float, help="ACU cost, recorded with the run")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true", help="emit the full result object")

    args = ap.parse_args()
    if args.cmd != "open-pr":  # pragma: no cover - argparse enforces this
        ap.error(f"unknown command {args.cmd!r}")

    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text()
    if not body:
        sys.exit("open-pr requires --body or --body-file")

    result = open_pr(
        args.repo,
        args.issue,
        _split_files(args.files),
        args.title,
        body,
        args.base,
        args.branch,
        args.session,
        args.acu,
        args.dry_run,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"draft PR: {result['pr_url']}")
        print(f"branch:   {result['branch']} -> {result['base']}")
        print(f"files:    {', '.join(result['files'])}")
        print(f"outcome:  {result['outcome']}")


if __name__ == "__main__":
    main()
