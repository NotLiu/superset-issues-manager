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
  `duplicate` branch. **`auto-resolve` is a KEY FEATURE that must be implemented**
  (classify → gate → reproduce → fix → draft PR) — per the user it is NOT to be
  dropped or left label-only. CURRENT STATE: it is wired as a classifier label
  but the resolver itself is not yet built (this is the top remaining feature).
  NOTE the scope tension to confirm with the user before building: earlier in the
  project the user said to drop **dependency/vulnerability**-specific work, and
  dependency-CVE was the originally-planned first `auto-resolve` class. So before
  implementing, confirm WHICH class of issues `auto-resolve` should actually fix
  (a non-dependency class, or re-include dependency/CVE).
- **Classifier runs inside a Devin session** (uses existing Devin access — no
  external LLM API key). MVP collapses Tier-0 classify + Tier-1 act into one
  session per issue.
- **No external API key needed.** Classification = Devin session; dependency
  audit = public **OSV API**; GitHub = authenticated `gh` CLI.
- **Datastore = shared, persistent Postgres** when `TRIAGE_DATABASE_URL` is set
  (the live-automation path — corpus + metrics persist across sessions). Falls
  back to local **SQLite + FTS5** when the env var is unset (offline CLI/tests).
  Keyword retrieval uses Postgres `tsvector`+GIN or SQLite FTS5 accordingly.
  pgvector remains the post-MVP north star (see DESIGN §7).
- **Bounded corpus**: ~300 recent upstream issues ingested read-only — NOT the
  full history, and NOT recreated as real fork issues.
- **Dependency audit via OSV API directly** (not pip-audit's pip dry-run, which is
  flaky under a non-interactive subprocess).
- **Draft PRs only** — never auto-merge, never auto-close.
- **Directory** = `.devin/issue-triage-automation/`.
- **Trigger = live GitHub `issues` webhook → Devin automation** (headline). The
  CLI (`triage`) remains as an offline test harness.

## 3. Architecture (MVP)

```
new/test issue (fork) ─▶ Devin triage session
    ├─ search.py        cheap FTS5 candidate retrieval (top ~10), no LLM
    ├─ classify         the session's LLM judgment → {duplicate, auto-resolve,
    │                    needs-review, invalid} + confidence + matched ids
    ├─ route via act.py  duplicate → comment top-3 + label; invalid → request
    │                    info; needs-review → label; auto-resolve → resolve branch
    ├─ resolve_dependency.py (auto-resolve, dependency-CVE only) → draft PR
    └─ record.py        log classification + run/outcome to shared Postgres
                              │
                              ▼
                        dashboard reads metrics/timeseries
```

Guardrails: confidence threshold; default to `needs-review` when unsure; scope cap
(files/lines) → downgrade; idempotency; draft-PR-only. See DESIGN §3.

## 4. Component map (`scripts/`)

| file | role | stable CLI |
|---|---|---|
| `db.py` | Dual-backend store: Postgres (`TRIAGE_DATABASE_URL`) or SQLite. Corpus (`issues`) + metrics (`classifications`, `runs`). | `python db.py` (init) |
| `ingest.py` | Fetch bounded upstream issues via `gh`, filter PRs, store + FTS index, write JSONL seed. | `--repo apache/superset --limit 300` |
| `search.py` | Candidate retrieval over the corpus (Postgres `ts_rank` or SQLite FTS5 BM25). | `--text "..." --k 5 [--json]` |
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

## 8. Status & next steps (current)

- **WORKING end-to-end (live)**: GitHub `issues/opened` webhook → Devin automation
  fires a session automatically → classify → comment/label on the issue → record
  metrics to shared Postgres → dashboard funnel increments. Proven on issue #12
  (classified `duplicate`, posted top-3 comment incl. upstream apache/superset#38976,
  applied `duplicate` label). See §9 for the exact operational details.
- **Merged to master**: PR #2 (foundation + docs), PR #9 (shared Postgres datastore
  + OR-tsquery search fix).
- **Open, ready to merge to master** (retargeted, mergeable, disjoint files):
  PR #7 (metrics dashboard), PR #8 (classification spine `triage.py` + playbook).
- **Closed**: PR #10 (auto-created integration PR; duplicated #7+#8 and revived
  descoped dependency `resolve_dependency.py` changes).
- **Classifier labels live**: `duplicate`, `invalid`, `needs-review` fully routed.
  `auto-resolve` label is applied but the **resolver is not built yet** — this is
  the **top remaining feature** (see §2 for the scope question to confirm first).
- **Known caveat**: the offline `triage.py --classifier heuristic` dup-floor was
  tuned for SQLite BM25 score magnitudes; Postgres `ts_rank` scores are smaller, so
  the offline heuristic under-detects duplicates. The LIVE automation uses LLM
  judgment (not numeric thresholds) so it is unaffected; recalibrate the heuristic
  if the offline CLI matters.
- **Next**: (1) confirm + build the `auto-resolve` resolver (key feature);
  (2) merge #7 and #8; (3) optionally add a payload filter so non-`opened`/ping
  deliveries don't even spawn a guard-only session.

## 9. Live automation — operational & testing context (READ before testing)

**Automation** (manage via the `devin_automation_manage` MCP tool):
- id `auto-af1bbdf33a714426a7022e34c9c1797f`, name "Superset issue auto-triage
  (live webhook)", **enabled**, `bypass_approval=true` (sessions run the full flow
  with no manual approval), `max_acu_limit=10`, `invocation_limit=10 / 3600s`.
- Trigger: `webhook:incoming`, conditions match-all (`[[]]`). Action: `start_session`
  with the triage prompt (STEP 0–7 below), `repos=[NotLiu/superset-issues-manager]`.
- The triage prompt lives ONLY in the automation action (not in the repo). STEP 0
  GUARD: stop unless `action == "opened"` AND author is not a bot. STEP 1 setup +
  assert `backend: postgres`. STEP 2 `search.py`. STEP 3 classify (LLM judgment).
  STEP 4 `act.py` comment/label (NOT dry-run). STEP 5 `record.py`. STEP 6
  `ingest.py --add-issue` (grow corpus). STEP 7 stop (never close/PR/push code).

**GitHub webhook** (repo Settings → Webhooks, hook id `646234668`): `issues`
events, `application/json`, active.

**⚠️ Webhook auth gotcha (cost us the most time):** Devin's incoming webhook
authenticates via a **`?secret=<value>` QUERY PARAMETER appended to the Payload
URL** — it does NOT use GitHub's HMAC `Secret` field. Symptoms when wrong:
`401 {"detail":"Missing webhook secret"}` = no `?secret=` in the URL;
`403 {"detail":"Invalid webhook secret"}` = wrong value. The secret is
set/rotated in the automation editor ("Regenerate webhook secret") and is NOT
exposed via the API. Correct Payload URL form:
`https://app.devin.ai/api/webhooks/automations/org-1bd4e4736c50422eaf783c33d876e949/auto-af1bbdf33a714426a7022e34c9c1797f?secret=<SECRET>`

**Shared datastore:** hosted Neon **Postgres** via env var `TRIAGE_DATABASE_URL`
(org secret; auto-injected into automation sessions). 300-issue corpus + metrics
persist across sessions. `psycopg[binary]>=3.1` is required — install with
`python3 -m pip install -r ../requirements.txt`. Verify with `python3 db.py`
(must print `backend: postgres`; if it prints sqlite the env var is missing —
do NOT proceed with the ephemeral store). Do NOT re-ingest the corpus per run.

**How to test end-to-end (the golden path):**
1. Open a **human-authored** issue on the fork (the GitHub UI as yourself).
   ⚠️ Issues opened via the `gh` CLI are authored by `devin-ai-integration[bot]`
   and are SKIPPED by the STEP 0 bot-author guard — they will NOT classify.
2. Confirm the delivery: `gh api repos/NotLiu/superset-issues-manager/hooks/646234668/deliveries`
   → newest `issues/opened` should be `200`.
3. Find the session: `devin_session_search` (origin api / most recent) or watch
   the automation. With `bypass_approval=true` it runs automatically.
4. Verify on GitHub: `gh issue view <n> --json labels,comments` → expect the label
   and (for `duplicate`) a top-3 similar-issues comment.
5. Verify metrics: from `scripts/`, `python3 -c "import record,json;print(json.dumps(record.metrics()))"`.

**Dashboard:** `cd scripts && python3 dashboard.py --port 8765` → http://127.0.0.1:8765/
(reads the shared DB via `record.py`). Needs `dashboard.py` + `dashboard_template.html`
present — they live in PR #7; until #7 is merged restore them with
`git show origin/pull/7/head:.devin/issue-triage-automation/scripts/dashboard.py`
(and `dashboard_template.html`). The server reads the template from disk per request.

**Demo evidence from the build run:** issue #12 → duplicate (comment + label);
funnel reached 11 tracked / 5 deflected. Ping events and bot-authored issue #11
each fired a session that correctly STOPPED at the STEP 0 guard.
