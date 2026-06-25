# Issue Triage & Auto-Resolve Automation — project context

Context for future Devin sessions working on this project. Read this first, then
`DESIGN.md` (full architecture / north star) and `FUTURE_EXTENSIONS.md` (deferred
scope). This file is the source of truth for **direction, locked decisions, and
the planning/development log** so sessions stay aligned and can be sized.

> This is a project-scoped AGENTS.md under `.devin/`. The repo-root `AGENTS.md`
> (Apache Superset contributor rules) still applies — notably: run
> `pre-commit run --all-files` before pushing, and add ASF license headers to new
> code files.

## 1. What this project is

An **event-driven issue-triage system** for the Apache Superset repo (this repo is
a fork standing in for upstream). On a new issue it **classifies** the issue and
routes it; for a narrow, safe allow-list it autonomously **fixes** the issue and
opens a **draft PR**. Every step is recorded to a metrics store and surfaced on a
dashboard (the observability story).

Headline capability = the full Devin loop **classify → (gate) → reproduce → plan →
fix → draft PR**, made cheap and safe by a classifier that decides whether the
expensive fix loop runs at all. Duplicate detection is **one branch** of the
classifier, not the whole project.

## 2. Locked decisions (do not relitigate without the user)

- **Scope = Idea 2** (classify → auto-resolve → PR) as the spine; dedup is the
  `duplicate` branch; dependency-CVE fixing is the first `auto-resolve` class.
- **Classifier runs inside a Devin session** (uses existing Devin access — no
  external LLM API key). MVP collapses Tier-0 classify + Tier-1 act into one
  session per issue.
- **No external API key needed.** Classification = Devin session; dependency
  audit = public **OSV API**; GitHub = authenticated `gh` CLI.
- **Datastore = SQLite + FTS5** for the MVP (single file, zero infra). Postgres
  /pgvector is the post-MVP north star (see DESIGN §7).
- **Bounded corpus**: ~300 recent upstream issues ingested read-only — NOT the
  full history, and NOT recreated as real fork issues.
- **Dependency audit via OSV API directly** (not pip-audit's pip dry-run, which is
  flaky under a non-interactive subprocess).
- **Draft PRs only** — never auto-merge, never auto-close.
- **Directory** = `.devin/issue-triage-automation/`.
- **MVP trigger = CLI** simulating on-issue-created (real webhook→Devin deferred).

## 3. Architecture (MVP)

```
new/test issue (fork) ─▶ Devin triage session
    ├─ search.py        cheap FTS5 candidate retrieval (top ~10), no LLM
    ├─ classify         the session's LLM judgment → {duplicate, auto-resolve,
    │                    needs-review, invalid} + confidence + matched ids
    ├─ route via act.py  duplicate → comment top-3 + label; invalid → request
    │                    info; needs-review → label; auto-resolve → resolve branch
    ├─ resolve_dependency.py (auto-resolve, dependency-CVE only) → draft PR
    └─ record.py        log classification + run/outcome to SQLite
                              │
                              ▼
                        dashboard reads metrics/timeseries
```

Guardrails: confidence threshold; default to `needs-review` when unsure; scope cap
(files/lines) → downgrade; idempotency; draft-PR-only. See DESIGN §3.

## 4. Component map (`scripts/`)

| file | role | stable CLI |
|---|---|---|
| `db.py` | SQLite schema + connection; corpus (`issues` + `issues_fts`) and metrics (`classifications`, `runs`). Override path with `TRIAGE_DB`. | `python db.py` (init) |
| `ingest.py` | Fetch bounded upstream issues via `gh`, filter PRs, store + FTS index, write JSONL seed. | `--repo apache/superset --limit 300` |
| `search.py` | FTS5 BM25 candidate retrieval over the corpus. | `--text "..." --k 5 [--json]` |
| `record.py` | Persist classifications/runs; compute dashboard `metrics()` / `timeseries()`. | `classification|run|metrics|timeseries` |
| `act.py` | Side-effecting GitHub actions (comment/label) + dup-comment renderer. `--dry-run` to preview. | `comment|label|dup-comment` |
| `resolve_dependency.py` | Auto-resolve branch: OSV `scan` → `fix` (pin in base.in + minimal recompile) → `verify`. | `scan|fix|verify` |
| `data/corpus__*.jsonl` | Reproducible corpus seed (the `.db` is gitignored, rebuildable). | — |

## 5. How to run (MVP)

```bash
cd .devin/issue-triage-automation/scripts
python ingest.py --repo apache/superset --limit 300   # build corpus (needs gh auth)
python search.py --text "dashboard fails to load" --k 5
python record.py metrics --json
# auto-resolve branch (needs uv on PATH):
python resolve_dependency.py scan
```

The runtime DB lives at `data/triage.db` (gitignored); rebuild it from the JSONL
seed or by re-running `ingest.py`.

## 6. Repo constraints that bind this project

- **`pre-commit run --all-files` before pushing** (repo-root AGENTS.md, non-negotiable).
- **ASF license header on every new code file** (`.py`). LLM/instruction docs
  (`*AGENTS.md`, etc.) are excluded via `.rat-excludes`.
- **SECURITY.md scope** governs which dependency findings are in scope: only a
  **direct, shipped** dependency pinned to a known-vulnerable version is in scope;
  transitive/operator-selected deps are out of scope. Automated findings must name
  the violated capability-matrix row + assumed principal (AGENTS.md "Finding
  Contract").
- **Python deps are compiled, not hand-edited**: edit `requirements/base.in`, then
  recompile `base.txt` (a blind recompile churns ~160 packages — do targeted,
  minimal-diff recompiles). Follow the existing `# Security: CVE-…` pin convention.
- Actions/CI are disabled on the fork, so CI won't auto-run on PRs here.

## 7. Planning & development log (how we got here)

1. **Started broad** (remediation augmentation areas) → narrowed to dependency
   vulnerability + dependency-fix automation.
2. **Phase 0 viability** (PR #1: https://github.com/NotLiu/superset-issues-manager/pull/1):
   - Proved the scan→fix→verify→PR loop on flask CVE-2026-27205 (2.3.3→3.1.3).
   - Found Dependabot is configured but **not live** on the fork (0 open PRs); the
     real existing remediation is the manual `# Security:` pins in `base.in`.
   - **bandit (SAST) is low-signal** here (all 5 HIGH were pre-triaged MD5 false
     positives) → dropped from v1; dependency track uses pip-audit/OSV + npm audit.
   - In-scope actionable surface is small (mostly Python, ~1–2 issues) → **scope
     classification is the difference between signal and spam.**
3. **Direction evaluation**: scored 3 ideas (dependency-fix loop, dedup bot,
   classify→auto-resolve) against (a) real workflow problem, (b) event-driven Devin
   fit, (c) observability, (d) Devin code→PR differentiation. Concluded the
   dependency-only loop is real but narrow (common case = a no-op version bump =
   Dependabot's job; the interesting breaking-change case is rare/un-demoable).
   **Chose to unify all three under the classify→auto-resolve spine.**
4. **Reframed** the user's "Duplicate Issues Handler" spec by inverting it: the
   project is **Issue Triage & Auto-Resolve**; dedup is the `duplicate` branch.
   Introduced the **two-tier** (cheap classify / gated expensive resolve) design as
   the safety+cost mechanism.
5. **Simplifications locked**: no vector DB in v1 (FTS + cheap candidate retrieval
   + one LLM judgment); ingest upstream read-only (don't recreate fork issues);
   draft-PR-only; helpfulness-rating→auto-close deferred.
6. **MVP scoped to a 2–3h window**: SQLite+FTS5, bounded corpus, CLI trigger,
   dependency-CVE as the only auto-resolve class, minimal dashboard.
7. **Build so far**: corpus ingested (300 issues, FTS works — retrieval returns the
   exact matching upstream issue as top hit), `search.py`/`record.py`/`act.py`
   built + tested, `resolve_dependency.py` switched to the OSV API (pip-audit's pip
   resolve was flaky under subprocess).
8. **Parallelized** the remaining phases (classification spine + trigger;
   dashboard; auto-resolve branch) into independent child sessions.

## 8. Status & next steps

- **Done**: ingest, search, metrics recorder, GitHub action wrappers, OSV audit.
- **In progress (child sessions)**: (1) classification spine + CLI trigger,
  (2) metrics dashboard, (3) auto-resolve dependency branch → draft PR.
- **Known issue**: `resolve_dependency.py fix` uses a prefix match that also
  unpins `flask-*` packages — must be an exact package-name match (child 3 fixes).
- **Then**: integrate child PRs + run the end-to-end demo on the fork.
