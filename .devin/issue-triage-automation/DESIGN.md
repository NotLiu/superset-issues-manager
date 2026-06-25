# Superset Issue Triage & Auto-Resolve Automation — Design

Event-driven system that triages every new GitHub issue and, for a narrow safe
subset, autonomously fixes it and opens a draft PR — with full observability into
the triage funnel.

Headline = the **auto-resolve → plan → fix → PR** loop (the full Devin system),
gated by a **cheap classifier** so the expensive loop only runs when it's safe and
likely to succeed. Duplicate detection is one branch of the classifier, not the
whole project.

---

## 0. MVP scope (2–3 hour build window)

Hard constraint: get to **working + testable** in 2–3 hours. The full design below
is the north star; the MVP cuts aggressively to a demoable spine.

**MVP keeps:**
- **SQLite (not Postgres)** with **FTS5** full-text — zero infra, built-in keyword
  search. Single file, perfect for the "index + cheap retrieval" plan.
- **Bounded corpus** — ingest a few hundred recent upstream `apache/superset`
  issues (by recency/label), not all thousands. Enough to demo dedup.
- **CLI trigger** — `triage <issue#-or-text>` simulates the "on issue created"
  event. (Real webhook→Devin wiring is documented but not built in the window.)
- **Tier-0 classify** (one LLM call) → route to: `duplicate` (comment top-3),
  `auto-resolve` (dependency-CVE class only → reuse the proven PR #1 loop → draft
  PR), else `needs-review`/`invalid` (label/comment).
- **Metrics to SQLite** + a **minimal dashboard** (one small page reading the
  metrics; charts via Chart.js or a generated HTML report).

**MVP defers:** Postgres/pgvector + embeddings, full corpus ingest, webhook relay,
weekly re-index cron, helpfulness-rating→auto-close, Superset-dogfood dashboard,
the broader auto-resolve allow-list (lint/docs/bugs).

**Demo path:** create a fork issue that dupes a known upstream issue → `triage` →
see top-3 comment; point `triage` at a dependency issue → auto-resolve → draft PR;
open dashboard → see funnel counts update.

---

## 1. Flow

```
new issue on fork ──▶ Tier-0 classify (1 cheap LLM call, NO Devin session)
                         │
        ┌────────────────┼───────────────────────┬─────────────────────┐
        ▼                ▼                         ▼                     ▼
   duplicate        invalid/                  needs-review         auto-resolve
   comment top-3    insufficient-info         label + (opt.)       (allow-list +
   + label          comment for info          related issues       confidence ≥ τ)
        │                │                         │                     │
        └──── all cheap, comment/label only ───────┘                     ▼
                                                              Tier-1: spin Devin
                                                              session → reproduce →
                                                              plan → fix → DRAFT PR
                                                              → verify (tests/scan)

   every step writes a row to the metrics store ──▶ analytics dashboard
```

## 2. Two-tier design (the safety/cost gate)

- **Tier 0 — classify (always, cheap):** a single LLM call in the trigger handler
  reads the issue body + a small set of candidate similar issues and returns
  `{label, confidence, evidence, matched_issue_ids}`. No Devin session. Runs on
  every issue; cost ~negligible.
- **Tier 1 — resolve (gated, expensive):** only when `label == auto-resolve`,
  `confidence ≥ τ`, AND the issue matches the allow-list → spin a Devin session
  that does the full reproduce → plan → fix → **draft PR** → verify loop.

This is what makes the system cheap and safe: ACU is spent only on issues we can
actually fix. Duplicate/triage branches cost zero Devin sessions.

## 3. Classification

| label | decided by (Tier 0) | action |
|---|---|---|
| `duplicate` | candidate retrieval → LLM confirms ≥1 strong match | comment top-3 links + label `duplicate` |
| `invalid/insufficient-info` | LLM checks for repro / version / logs | comment requesting info + label |
| `auto-resolve` | LLM + **allow-list** match + confidence ≥ τ | trigger Tier-1 → draft PR |
| `needs-review` | fallback / anything uncertain | label + optionally attach related issues |

**Auto-resolve allow-list (start tiny):**
- dependency CVE / version bumps (already proven — see PR #1)
- lint / type / `noqa` cleanups
- docs typos / broken links
- bugs with a clear stack trace pointing at a specific file

**Hard exclusions:** feature requests, large refactors, auth/security-sensitive
paths, ambiguous bugs.

**Guardrails:**
- confidence threshold τ; default to `needs-review` whenever unsure (false
  `auto-resolve` is the expensive failure mode).
- scope cap (max N files / M lines changed) → else downgrade to `needs-review`.
- **always draft PR; never auto-merge; never auto-close.**
- idempotency: skip already-processed issues (track by issue id).
- daily ACU / issue cap.

## 4. Duplicate detection without a vector DB (v1)

Per the open question — **no embeddings/vector DB in v1.** The corpus is large but
we never score it wholesale:

1. **Ingest time:** store each issue in Postgres with a `tsvector` full-text
   column + **GIN** index.
2. **Query time (new issue):** cheap **candidate retrieval** — Postgres full-text
   search (or the GitHub Search API) returns the top ~10 candidates that share
   terms. *No LLM, no embeddings here.*
3. **One LLM call** judges the new issue against just those ~10 candidates →
   classify + pick the top-3 duplicates/related.

So "index the issues + one cheap LLM call per new issue" is exactly right: the
LLM only ever sees a small candidate set, not the whole history.

**Vector / pgvector is a stretch** that only improves *recall* of step 2
(semantic matches sharing no keywords). Add it later (HNSW index in the same
Postgres) if keyword recall proves weak. Dropping it from v1 also removes the
need for the weekly re-index cron — GitHub stays the source of truth.

## 5. Data sources (the fork problem)

The working repo is a **fork** with no inherited issues. Decision:

- **Do NOT recreate upstream issues as real issues in the fork** (thousands of
  them → massive API writes, rate limits, pollutes the fork, pointless).
- **Ingest upstream `apache/superset` issues read-only into our Postgres
  corpus.** The corpus lives in our datastore, not as GitHub issues.
- The **fork only hosts the new/test issues** that trigger the automation.
  Duplicate-comment links point to the **real upstream issue URLs** (better — they
  have full context and resolutions).
- As new issues are created on the fork, add them to the corpus too so later
  issues can dedupe against earlier ones.

**Ingestion notes:** the GitHub issues endpoint returns PRs too — filter them out
via the `pull_request` field. Paginate at 100/page; authenticated REST = 5000
req/hr (use GraphQL for fewer round-trips if needed). Capture title, body,
labels, state, created/closed timestamps, and resolution.

## 6. Trigger mechanism (load-bearing — decide early)

GitHub Actions are disabled on the fork, so the "on issue creation" event needs:
- **Option A:** GitHub webhook → small relay service → Devin API (`/v3`) to start
  a session/automation. Most robust.
- **Option B:** Devin native automation trigger on the issue event, if available
  for this integration.
- **Option C (dev/demo fallback):** a short poller that lists new issues.

Tier-0 classification should run in the relay/handler (a plain LLM call), and only
escalate to a Devin **session** for Tier-1.

## 7. Datastore & metrics

**Postgres** (single store; pgvector optional later). Model the **funnel**, not
just counters:

- `issues(id, source_repo, number, title, body, labels, state, created_at, closed_at, resolution, tsv)`
  — GIN index on `tsv`, btree on `(source_repo, number)`.
- `classifications(id, issue_id, label, confidence, evidence, matched_ids, ts)`
- `runs(id, issue_id, devin_session_id, acu_cost, outcome, pr_url, started_at, finished_at)`

Counters become queries over these:
`total tracked`, `deflected (duplicate/invalid)`, `left open`, `resolved after
human intervention`, `estimated time saved (5 min/issue)`, `estimated ACU cost`,
`ACU/issue`. Pull `acu_cost` + status per session from the **Devin API v3**. Mark
time-saved / ACU as *estimates* in the UI.

## 8. Dashboard

Two options:
- **Dogfood Superset** (recommended): pipe metrics into a DB and build a Superset
  dashboard. On-brand ("Superset visualizing the bot that maintains Superset"),
  no separate frontend to build.
- **React + Vite SPA** (fallback): left = reorderable stat list with a focused
  stat; right = trend chart bucketed by day→week. Defer drag-reorder/focus polish
  to after v1.

## 9. Phases

- **Phase 1 (safe, data-rich):** upstream ingestion → Postgres; Tier-0 classifier;
  trigger wiring; no-Devin branches (`duplicate` comment, `needs-review`/`invalid`
  labels). Funnel + metrics flowing, zero PR risk.
- **Phase 2 (headline):** `auto-resolve` branch, **dependency-CVE class first**
  (proven loop) → draft PR → verify; then widen allow-list to lint/docs.
- **Phase 3:** metrics dashboard.
- **Stretch:** pgvector similarity; helpfulness rating → auto-close.

## 10. Decisions (locked) & still-open

**Locked:**
- Directory: `.devin/issue-triage-automation/`.
- Classifier runs **inside a Devin session** (uses existing Devin access; no
  external LLM key). For the MVP, Tier-0 classify and Tier-1 act are collapsed
  into a single session per issue.
- Datastore is a **shared, persistent Postgres** (`TRIAGE_DATABASE_URL`) so the
  corpus and metrics survive across automation-triggered sessions; keyword
  retrieval uses `tsvector`+GIN. SQLite+FTS5 remains a zero-config fallback for
  offline CLI/tests. (pgvector stays the post-MVP north star.)
- Dependency audit uses the **public OSV API** directly (no key, no flaky pip
  resolve) — see `scripts/resolve_dependency.py`.
- Trigger is a **live GitHub `issues` webhook → Devin automation** (headline);
  the CLI remains as an offline test harness.

**Still open (post-MVP):** dashboard hosting (Superset dogfood vs standalone
SPA); pgvector for semantic recall. See `FUTURE_EXTENSIONS.md`.
