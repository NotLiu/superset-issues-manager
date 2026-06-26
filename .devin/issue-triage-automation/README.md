# Issue Triage & Auto-Resolve Automation

Event-driven issue-triage system for [Apache Superset](https://github.com/apache/superset).
When a new issue is opened on the fork, the system **classifies** it (duplicate,
invalid, needs-review, or auto-resolve) and **routes** the decision — for a narrow,
safe subset it autonomously implements a fix and opens a **draft PR**.

## Context

This repository is a fork of `apache/superset` used as a staging ground for the
automation. A GitHub webhook fires on every new issue, triggering a Devin
automation session that runs the triage pipeline against a 300-issue upstream
corpus. Every classification and action is recorded to a shared Postgres
datastore and surfaced on a lightweight metrics dashboard.

## Components

```
.devin/issue-triage-automation/
├── scripts/
│   ├── db.py           # Dual-backend datastore (Postgres / SQLite)
│   ├── ingest.py       # Corpus builder (upstream issues via gh CLI)
│   ├── search.py       # FTS candidate retrieval (tsvector / FTS5 BM25)
│   ├── triage.py       # Orchestrator: classify → route → record
│   ├── act.py          # Side-effects: GitHub comment + label
│   ├── record.py       # Metrics persistence + dashboard queries
│   ├── resolve.py      # Auto-resolve: branch/commit/push → draft PR
│   ├── resolve_dependency.py  # Standalone OSV dependency-audit tool
│   ├── dashboard.py    # HTTP server for the metrics dashboard
│   ├── dashboard_template.html
│   └── seed_metrics.py # Demo data seeder
├── data/
│   └── corpus__apache__superset.jsonl  # Reproducible corpus seed
├── playbooks/
│   └── triage.md       # Step-by-step session playbook
├── Dockerfile          # Container image for the triage system
├── docker-compose.yaml # Full local stack (Postgres + triage + dashboard)
├── requirements.txt    # Python runtime deps (psycopg)
├── DESIGN.md           # Architecture north star
├── AGENTS.md           # Project context for Devin sessions
└── FUTURE_EXTENSIONS.md
```

### Key scripts

| Script | Purpose |
|--------|---------|
| `triage.py` | Entry point — classifies an issue and routes the decision |
| `search.py` | Cheap keyword retrieval (top-K candidates from corpus) |
| `act.py` | Executes GitHub side-effects (comments, labels) |
| `record.py` | Persists classifications/runs; serves dashboard metrics |
| `resolve.py` | Opens a draft PR for auto-resolve issues |
| `dashboard.py` | Serves the metrics dashboard UI |
| `ingest.py` | Builds/rebuilds the issue corpus from upstream |

## Usage Loop

### 1. Start the stack

```bash
cd .devin/issue-triage-automation
docker compose up -d postgres
docker compose --profile cli run --rm triage db.py
docker compose --profile cli run --rm triage ingest.py --from-seed ../data/corpus__apache__superset.jsonl
docker compose up dashboard
```

The dashboard is available at **http://localhost:8765**.

### 2. Triage an issue (offline / dry-run)

```bash
# Classify by raw text (no GitHub writes)
docker compose --profile cli run --rm triage triage.py \
  --text "ChunkLoadError loading lazy frontend bundle" --dry-run

# Classify a real issue number (requires GH_TOKEN)
docker compose --profile cli run --rm triage triage.py \
  --issue 12 --repo NotLiu/superset-issues-manager --dry-run --json
```

### 3. Live automation (production path)

In production, a GitHub `issues/opened` webhook triggers a Devin automation
session that:

1. **Guards** — skips bot-authored and non-`opened` events
2. **Retrieves** — `search.py` finds top-K corpus candidates
3. **Classifies** — the Devin session judges the label (LLM-as-classifier)
4. **Routes** — `act.py` posts comments/labels on GitHub
5. **Records** — `record.py` persists the decision to Postgres
6. **Grows corpus** — `ingest.py --add-issue` adds the new issue for future dedup
7. **(If auto-resolve)** — `resolve.py` opens a draft PR with the fix

### 4. View metrics

```bash
docker compose up dashboard
# Then open http://localhost:8765
#   GET /api/metrics      → funnel counters (JSON)
#   GET /api/timeseries   → bucketed time series (JSON)
```

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `TRIAGE_DATABASE_URL` | Postgres connection string | Local compose Postgres |
| `GH_TOKEN` | GitHub token for `gh` CLI operations | (none) |
| `TRIAGE_DB` | Override SQLite path (non-Postgres mode) | `data/triage.db` |

## Design Principles

- **Two-tier cost gate**: cheap classification on every issue; expensive fix loop
  only when safe and likely to succeed.
- **Draft PRs only**: never auto-merge, never auto-close.
- **Idempotent**: skip already-processed issues; safe to re-run.
- **Dual backend**: Postgres for production persistence; SQLite for zero-config
  offline testing.
- **Observable**: every step writes to the metrics store; the dashboard shows the
  full triage funnel.
