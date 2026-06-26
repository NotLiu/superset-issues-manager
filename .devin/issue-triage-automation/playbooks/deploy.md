# Playbook — deploying the issue-triage automation to a new repo

How to wire the self-contained issue-triage system onto an arbitrary GitHub
repository. The scripts, corpus build, and classification loop are internal and
repo-parameterized; you only connect **two external seams**:

| seam | what you provide |
|------|------------------|
| **A — GitHub webhook → Devin automation** | A `webhook:incoming`-triggered Devin automation + an `issues`-events webhook on the target repo pointing at it. |
| **B — Datastore / observability platform** | A Postgres-compatible DSN (`TRIAGE_DATABASE_URL`) where the corpus and metrics persist across sessions. |

Everything else — `search.py`, `triage.py`, `act.py`, `record.py`,
`resolve.py`, `ingest.py`, `db.py`, the corpus build, the classification
loop — ships in the repo under `.devin/issue-triage-automation/` and is
parameterized by the `--repo` flag or `TRIAGE_DATABASE_URL`.

> **This playbook covers deployment only.** The per-issue operational loop
> (classify → route → record) is documented in [`triage.md`](triage.md). For
> architecture and locked decisions see [`../AGENTS.md`](../AGENTS.md) and
> [`../DESIGN.md`](../DESIGN.md).

---

## Architecture sketch

```
                          ┌──────────────────────────────────┐
                          │   GitHub repo (OWNER/REPO)       │
                          │                                  │
                          │  new issue ──▶ issues webhook ───┼──┐
                          └──────────────────────────────────┘  │
                                                                │  (A)
          ┌─────────────────────────────────────────────────────┘
          ▼
   Devin automation (webhook:incoming)
          │  start_session → triage prompt (STEP 0–7)
          ▼
   ┌──────────────────────────────────────────────────┐
   │  Devin session (self-contained scripts)          │
   │                                                  │
   │  search.py → classify (LLM) → act.py → record.py│
   │       ↕              ↕              ↕            │
   │  ┌────────────────────────────────────────┐      │
   │  │  TRIAGE_DATABASE_URL (Postgres)   (B)  │      │
   │  │  corpus (issues) + metrics             │      │
   │  │  (classifications, runs)               │      │
   │  └────────────────────────────────────────┘      │
   │                                                  │
   │  on auto-resolve: resolve.py → draft PR          │
   │  on every run:    ingest.py --add-issue          │
   └──────────────────────────────────────────────────┘
          │
          ▼
   dashboard.py / record.py metrics  ← reads same store
```

---

## Placeholders used in this guide

| placeholder | meaning | Superset worked example |
|---|---|---|
| `OWNER/REPO` | the target repo where issues are filed and the automation runs | `NotLiu/superset-issues-manager` |
| `UPSTREAM_OWNER/UPSTREAM_REPO` | the upstream repo whose issues seed the corpus | `apache/superset` |
| `<ORG_ID>` | Devin org id (from automation settings) | `org-1bd4e4736c50422eaf783c33d876e949` |
| `<AUTO_ID>` | Devin automation id | `auto-af1bbdf33a714426a7022e34c9c1797f` |

---

## 1. Prerequisites

- **`gh` CLI** authenticated against `OWNER/REPO` (used by `ingest.py`,
  `act.py`, and `resolve.py` to call the GitHub API).
- **Python 3.10+** with the triage dependencies installed:

  ```bash
  cd .devin/issue-triage-automation
  python3 -m pip install -r requirements.txt
  ```

  The requirements include `psycopg[binary]>=3.1`, which is needed for the
  Postgres datastore path. The SQLite fallback needs only the standard library.

- **A Devin org** with automations enabled and the target repo connected
  (`repos=[OWNER/REPO]`).

---

## 2. Connection point B — bring your own datastore / observability platform

The system persists the upstream issue corpus and all triage metrics to whatever
`TRIAGE_DATABASE_URL` points at. For live automation this **must** be a
persistent, Postgres-compatible endpoint — Neon, Supabase, RDS, Cloud SQL, or
any Postgres-wire-compatible service.

### 2.1 Set the env var locally (for testing)

```bash
export TRIAGE_DATABASE_URL="postgresql://user:pass@host:5432/triage"
```

### 2.2 Register as a Devin org secret

So the var auto-injects into every automation-triggered session, add it as an
org-scoped secret named `TRIAGE_DATABASE_URL` in the Devin org settings (or via
the `request_secret` / org-secret API). Sessions spawned by the automation will
then have it in their environment without any manual export.

### 2.3 Verify the backend

```bash
cd .devin/issue-triage-automation/scripts
python3 db.py
```

Expected output:

```
Initialized triage DB backend: postgres
```

If it prints `sqlite (…/data/triage.db)` the env var is missing or malformed —
**do not proceed with live automation** on the ephemeral SQLite store. Fix the
DSN first.

### 2.4 Observability story

Metrics live in the **same Postgres store** and are read by:

- `record.py metrics --json` — aggregate counters (total tracked, deflected,
  PRs opened, ACU cost, estimated minutes saved).
- `record.py timeseries --bucket day` — classifications bucketed by day/week.
- `dashboard.py` — a minimal web UI that calls `record.metrics()` /
  `record.timeseries()` and renders the funnel.

To connect an external observability platform, either:

1. **Point it at the same Postgres tables** (`classifications`, `runs`) with a
   read-only connection, or
2. **Consume the CLI output** (`record.py metrics --json`) in a cron/pipeline
   and forward to your platform.

> **Limitation:** `db.py` only speaks Postgres or SQLite. "Bring your own
> datastore" means a **Postgres-compatible DSN**; forwarding to an arbitrary
> external store or observability SaaS (Datadog, Grafana Cloud, etc.) is a
> not-yet-built extension — see
> [`../FUTURE_EXTENSIONS.md`](../FUTURE_EXTENSIONS.md) §Dashboard &
> observability.

---

## 3. Build the corpus for the target repo

The classifier needs a corpus of existing upstream issues for duplicate
detection. The corpus is bounded (~300 recent issues), read-only, and persists
in the connected datastore — you do **not** re-ingest per automation run.

### 3.1 Ingest from GitHub (needs `gh` auth)

```bash
cd .devin/issue-triage-automation/scripts
python3 ingest.py --repo UPSTREAM_OWNER/UPSTREAM_REPO --limit 300
```

Superset example:

```bash
python3 ingest.py --repo apache/superset --limit 300
```

This fetches the 300 most recent issues (excluding PRs), stores them in the
connected datastore with a keyword index (Postgres `tsvector`+GIN or SQLite
FTS5), and writes a JSONL seed file to `data/corpus__<slug>.jsonl`.

### 3.2 Rebuild from a committed JSONL seed (no GitHub calls)

```bash
python3 ingest.py --from-seed ../data/corpus__apache__superset.jsonl
```

### 3.3 Growing the corpus at runtime

Each automation run appends the newly triaged issue to the corpus via
`ingest.py --add-issue N` (STEP 6 in the triage prompt), so future runs can
deduplicate against it.

---

## 4. Connection point A — GitHub webhook → Devin automation

Two sub-steps: create the Devin automation, then wire the GitHub webhook to it.

### 4.1 Create the Devin automation

Create a `webhook:incoming`-triggered automation in your Devin org with:

| field | value |
|---|---|
| **Trigger** | `webhook:incoming` |
| **Action** | `start_session` |
| **Repos** | `[OWNER/REPO]` |
| **`bypass_approval`** | `true` (sessions run the full flow automatically) |
| **`max_acu_limit`** | `10` (or your budget) |
| **`invocation_limit`** | `10 / 3600s` (rate cap) |

The action's **prompt** encodes the per-issue triage loop as STEP 0–7:

| step | what it does |
|---|---|
| **STEP 0** | **Guard**: stop unless `action == "opened"` AND the issue author is not a bot (prevents re-fires on edits, closes, and bot-created issues). |
| **STEP 1** | Setup: `cd` into scripts, `pip install -r ../requirements.txt`, run `python3 db.py` and assert `backend: postgres`. |
| **STEP 2** | `search.py --text "<title>\n<body>" --k 10 --json` — cheap keyword retrieval over the corpus. |
| **STEP 3** | Classify: the session reads the issue + candidates and decides `{label, confidence, matched, evidence}` (LLM judgment). |
| **STEP 4** | `act.py` — comment/label on the issue (NOT `--dry-run`). |
| **STEP 5** | `record.py` — persist the classification + run to the shared store. |
| **STEP 6** | `ingest.py --add-issue N` — grow the corpus with the new issue. |
| **STEP 7** | Stop. On an `auto-resolve` decision, the session instead runs the `resolve.py` draft-PR flow (reproduce → scan → plan → implement → `resolve.py open-pr`) before stopping. |

The prompt lives **in the automation action**, not in the repo. See
[`triage.md`](triage.md) for the full per-issue operational loop and
[`../scripts/TRIAGE_USAGE.md`](../scripts/TRIAGE_USAGE.md) for CLI flag
details.

Superset example: automation id `auto-af1bbdf33a714426a7022e34c9c1797f`, org id
`org-1bd4e4736c50422eaf783c33d876e949`.

### 4.2 Wire the GitHub webhook

In `OWNER/REPO` → **Settings → Webhooks → Add webhook**:

| field | value |
|---|---|
| **Payload URL** | `https://app.devin.ai/api/webhooks/automations/<ORG_ID>/<AUTO_ID>?secret=<SECRET>` |
| **Content type** | `application/json` |
| **Events** | Select **"Let me select individual events"** → check **Issues** only |
| **Active** | checked |

Superset example Payload URL:

```
https://app.devin.ai/api/webhooks/automations/org-1bd4e4736c50422eaf783c33d876e949/auto-af1bbdf33a714426a7022e34c9c1797f?secret=<SECRET>
```

> ### ⚠️ Webhook auth gotcha
>
> Devin authenticates incoming webhooks via a **`?secret=<value>` query
> parameter appended to the Payload URL**. It does **NOT** use GitHub's HMAC
> `Secret` field (leave that blank or set it to anything — it is ignored by
> Devin).
>
> **Symptoms when wrong:**
>
> | HTTP response | cause |
> |---|---|
> | `401 {"detail":"Missing webhook secret"}` | No `?secret=` in the Payload URL. |
> | `403 {"detail":"Invalid webhook secret"}` | Wrong `?secret=` value. |
>
> The secret is set / rotated in the Devin automation editor
> (**"Regenerate webhook secret"**) and is **not** exposed via the API. Copy it
> from the editor and append it to the Payload URL as a query parameter.
>
> **Correct Payload URL form:**
>
> ```
> https://app.devin.ai/api/webhooks/automations/<ORG_ID>/<AUTO_ID>?secret=<SECRET>
> ```

---

## 5. End-to-end verification (golden path)

1. **Open a human-authored issue** on `OWNER/REPO` via the GitHub web UI
   (logged in as yourself).

   > ⚠️ Issues opened via the `gh` CLI are authored by
   > `devin-ai-integration[bot]` and are **skipped by the STEP 0 bot-author
   > guard** — they will not trigger classification. Use the GitHub UI.

2. **Confirm the webhook delivery** returned `200`:

   ```bash
   gh api repos/OWNER/REPO/hooks/<HOOK_ID>/deliveries
   ```

   The newest `issues/opened` delivery should show status `200`.

3. **Find / watch the spawned Devin session.** With `bypass_approval=true` the
   session runs automatically. Search via the Devin session list or
   `devin_session_search` (origin `api`, most recent).

4. **Verify on GitHub:**

   ```bash
   gh issue view <N> --repo OWNER/REPO --json labels,comments
   ```

   Expect the classification label (e.g. `duplicate`, `needs-review`) and, for
   `duplicate`, a top-3 similar-issues comment linking upstream issues.

5. **Verify metrics landed in the connected datastore:**

   ```bash
   cd .devin/issue-triage-automation/scripts
   python3 -c "import record, json; print(json.dumps(record.metrics(), indent=2))"
   ```

   `total_issues_tracked` should have incremented.

---

## 6. Known limitations / per-repo deltas

- **Postgres-only datastore.** `db.py` speaks Postgres or SQLite; "bring your
  own store" means a Postgres-compatible DSN. Forwarding to an arbitrary
  external datastore/observability SaaS is deferred (see
  [`../FUTURE_EXTENSIONS.md`](../FUTURE_EXTENSIONS.md)).
- **Repo-root contributor rules are Superset-specific.** The repo-root
  `AGENTS.md` mandates `pre-commit run --all-files` and ASF license headers on
  new `.py` files. These rules bind `resolve.py`'s draft-PR flow; for a
  non-Superset repo the auto-resolve branch may need to skip or replace them.
- **Auto-resolve allow-list breadth.** The LLM judges "easy, well-scoped fix"
  generically, but the proven path is narrow (small bugs, typos, copy fixes).
  Widen with caution; the offline `heuristic` classifier never emits
  `auto-resolve`.
- **Heuristic score calibration.** The offline `triage.py --classifier
  heuristic` dup-floor was tuned for SQLite BM25 magnitudes; Postgres `ts_rank`
  scores are smaller, so the offline heuristic under-detects duplicates. The
  live automation uses LLM judgment (unaffected).
- **Scaling.** Each issue triggers one Devin session. Parallel child sessions,
  an idempotency store, and ACU budget caps are deferred — see
  [`../FUTURE_EXTENSIONS.md`](../FUTURE_EXTENSIONS.md) §Trigger & automation
  and §Hardening.

---

## Related docs

- [`triage.md`](triage.md) — per-issue operational loop (the session playbook).
- [`../AGENTS.md`](../AGENTS.md) — project context, locked decisions, live
  automation operational details (§9).
- [`../DESIGN.md`](../DESIGN.md) — full architecture and north-star design.
- [`../FUTURE_EXTENSIONS.md`](../FUTURE_EXTENSIONS.md) — deferred scope
  (scaling, vector search, external observability forwarding).
- [`../scripts/TRIAGE_USAGE.md`](../scripts/TRIAGE_USAGE.md) — `triage.py` CLI
  flags, defaults, and worked examples for all four labels.
