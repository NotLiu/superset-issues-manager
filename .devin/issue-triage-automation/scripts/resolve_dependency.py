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
"""Auto-resolve branch: the dependency-CVE fix loop.

Encodes the procedure proven manually on flask CVE-2026-27205:
  scan  -> pip-audit requirements/base.txt for fixable, direct-shipped vulns
  fix   -> add/raise a `# Security:` pin in base.in, then recompile base.txt to
           a *minimal* diff (only the targeted package + forced transitive bumps)
  verify-> re-run pip-audit and confirm the advisory id is gone

The edit/recompile preserve the existing annotation style by applying only the
changed version lines to the original base.txt (a blind recompile churns ~160
lines and is unreviewable).

Usage:
    python resolve_dependency.py scan
    python resolve_dependency.py fix --package flask --dry-run
    python resolve_dependency.py verify --cve CVE-2026-27205
"""

# Standalone CLI tooling (not part of the superset package): stdlib json, the
# urllib call to the public OSV API, and subprocess calls to `uv` are intended.
# ruff: noqa: TID251, S310, S603, S607
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_IN = REPO_ROOT / "requirements" / "base.in"
BASE_TXT = REPO_ROOT / "requirements" / "base.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"

PIN_BLOCK_START = "# >>> issue-triage-automation security pins >>>"
PIN_BLOCK_END = "# <<< issue-triage-automation security pins <<<"

UV = os.environ.get("UV_BIN", "uv")
OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/"

_PIN_LINE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)")


def _http_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _parse_pins(requirements: Path) -> list[tuple[str, str]]:
    pins: list[tuple[str, str]] = []
    for line in requirements.read_text().splitlines():
        m = _PIN_LINE.match(line)
        if m:
            pins.append((m.group(1), m.group(2)))
    return pins


def _audit(requirements: Path) -> list[dict[str, Any]]:
    """Audit the pinned lockfile against OSV directly (no pip resolve).

    pip-audit's default flow shells out to a pip dry-run resolve that is flaky
    under a non-interactive subprocess; querying OSV with the exact pins is
    deterministic and dependency-free.
    """
    pins = _parse_pins(requirements)
    queries = [
        {"package": {"ecosystem": "PyPI", "name": n}, "version": v} for n, v in pins
    ]
    results = _http_json(OSV_BATCH_URL, {"queries": queries}).get("results", [])

    findings: list[dict[str, Any]] = []
    detail_cache: dict[str, dict[str, Any]] = {}
    for (name, version), res in zip(pins, results, strict=False):
        for vuln in res.get("vulns", []) or []:
            vid = vuln["id"]
            detail = detail_cache.get(vid) or _http_json(OSV_VULN_URL + vid)
            detail_cache[vid] = detail
            aliases = detail.get("aliases", [])
            fixed = [
                e["fixed"]
                for a in detail.get("affected", [])
                if a.get("package", {}).get("name", "").lower() == name.lower()
                for r in a.get("ranges", [])
                for e in r.get("events", [])
                if "fixed" in e
            ]
            primary = next((x for x in aliases if x.startswith("CVE")), vid)
            findings.append(
                {
                    "package": name,
                    "version": version,
                    "id": primary,
                    "osv_id": vid,
                    "aliases": aliases,
                    "fix_versions": sorted(set(fixed)),
                }
            )
    return findings


def scan() -> list[dict[str, Any]]:
    findings = _audit(BASE_TXT)
    for f in findings:
        f["fixable"] = bool(f["fix_versions"])
    return findings


def _ceiling(fix_version: str) -> str:
    major = int(fix_version.split(".")[0])
    return f"<{major + 1}.0.0"


def _upsert_pin(package: str, fix_version: str, advisory: str) -> str:
    text = BASE_IN.read_text()
    pin = f"{package}>={fix_version},{_ceiling(fix_version)}"
    comment = f"# Security: {advisory} - resolved by issue-triage-automation"
    entry = f"{comment}\n{pin}"

    if PIN_BLOCK_START in text:
        start = text.index(PIN_BLOCK_START)
        end = text.index(PIN_BLOCK_END) + len(PIN_BLOCK_END)
        block = text[start:end]
        # Drop any prior pin for this package, then append the fresh one.
        kept = [
            ln
            for ln in block.splitlines()
            if not _PIN_LINE.match(ln.strip()) or not ln.strip().startswith(package)
        ]
        new_block = "\n".join(kept[:-1] + [entry, PIN_BLOCK_END])
        return text[:start] + new_block + text[end:]
    new_block = f"\n{PIN_BLOCK_START}\n{entry}\n{PIN_BLOCK_END}\n"
    return text.rstrip("\n") + "\n" + new_block


def _resolved_versions(text: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    for line in text.splitlines():
        m = _PIN_LINE.match(line)
        if m:
            versions[m.group(1).lower()] = m.group(2)
    return versions


def _recompile_minimal(
    targets: list[str],
) -> tuple[dict[str, tuple[str, str]], str]:
    """Recompile, allowing only `targets` (and forced deps) to move.

    Returns {package: (old, new)} for every version line that changed. Applies
    those changes to the original base.txt to keep the diff minimal and the
    annotations intact.
    """
    orig = BASE_TXT.read_text()
    constraint_lines = [
        ln
        for ln in orig.splitlines()
        if "superset-core" not in ln
        and not any(_PIN_LINE.match(ln) and ln.lower().startswith(t) for t in targets)
    ]
    with tempfile.TemporaryDirectory() as tmp:
        constraint = Path(tmp) / "constraint.txt"
        constraint.write_text("\n".join(constraint_lines) + "\n")
        out_file = Path(tmp) / "out.txt"
        subprocess.run(
            [
                UV,
                "pip",
                "compile",
                str(PYPROJECT),
                str(BASE_IN),
                "-c",
                str(constraint),
                "-o",
                str(out_file),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        new_versions = _resolved_versions(out_file.read_text())

    old_versions = _resolved_versions(orig)
    changed: dict[str, tuple[str, str]] = {}
    updated = orig
    for name, new_ver in new_versions.items():
        old_ver = old_versions.get(name)
        if old_ver and old_ver != new_ver:
            changed[name] = (old_ver, new_ver)
            updated = re.sub(
                rf"(?m)^{re.escape(name)}=={re.escape(old_ver)}$",
                f"{name}=={new_ver}",
                updated,
            )
    return changed, updated


def fix(package: str, dry_run: bool) -> dict[str, Any]:
    findings = {f["package"].lower(): f for f in scan() if f["fixable"]}
    f = findings.get(package.lower())
    if not f:
        return {"error": f"no fixable finding for {package}", "fixable": list(findings)}
    fix_version = f["fix_versions"][0]
    advisory = f["id"]

    new_base_in = _upsert_pin(package, fix_version, advisory)
    orig_base_in = BASE_IN.read_text()
    BASE_IN.write_text(new_base_in)
    try:
        changed, new_base_txt = _recompile_minimal([package.lower()])
    finally:
        if dry_run:
            BASE_IN.write_text(orig_base_in)

    result: dict[str, Any] = {
        "package": package,
        "advisory": advisory,
        "fix_version": fix_version,
        "changed": {k: {"from": v[0], "to": v[1]} for k, v in changed.items()},
        "dry_run": dry_run,
    }
    if dry_run:
        out = Path(tempfile.gettempdir()) / "base.txt.proposed"
        out.write_text(new_base_txt)
        result["proposed_base_txt"] = str(out)
    else:
        BASE_TXT.write_text(new_base_txt)
    return result


def verify(cve: str) -> dict[str, Any]:
    findings = _audit(BASE_TXT)
    ids = (
        {f["id"] for f in findings}
        | {f["osv_id"] for f in findings}
        | {a for f in findings for a in f.get("aliases", [])}
    )
    return {"cve": cve, "resolved": cve not in ids, "remaining": sorted(ids)}


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    pf = sub.add_parser("fix")
    pf.add_argument("--package", required=True)
    pf.add_argument("--dry-run", action="store_true")
    pv = sub.add_parser("verify")
    pv.add_argument("--cve", required=True)

    args = ap.parse_args()
    if args.cmd == "scan":
        print(json.dumps(scan(), indent=2))
    elif args.cmd == "fix":
        print(json.dumps(fix(args.package, args.dry_run), indent=2))
    elif args.cmd == "verify":
        print(json.dumps(verify(args.cve), indent=2))


if __name__ == "__main__":
    main()
