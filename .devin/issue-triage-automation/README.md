# Issue Triage & Auto-Resolve Automation

Containerized issue-triage system for Apache Superset. Classifies new GitHub
issues (duplicate, invalid, needs-review, auto-resolve) and routes them — for
safe cases it opens a draft PR with the fix.

## Prerequisites

- Docker & Docker Compose v2+
- `GH_TOKEN` env var (only needed for live GitHub operations)

## Quick Start

```bash
cd .devin/issue-triage-automation

# 1. Start Postgres and initialize the schema
docker compose up -d postgres
docker compose --profile cli run --rm triage db.py

# 2. Load the issue corpus
docker compose --profile cli run --rm triage ingest.py \
  --from-seed ../data/corpus__apache__superset.jsonl

# 3. Start the metrics dashboard (http://localhost:8765)
docker compose up -d dashboard
```

## Triage an Issue

```bash
# Dry-run: classify by text (no GitHub writes)
docker compose --profile cli run --rm triage triage.py \
  --text "ChunkLoadError loading lazy frontend bundle" --dry-run

# Dry-run: classify a real issue number (requires GH_TOKEN)
docker compose --profile cli run --rm triage triage.py \
  --issue 12 --repo NotLiu/superset-issues-manager --dry-run --json
```

Remove `--dry-run` to execute the full pipeline (label, comment, and
optionally open a draft PR for auto-resolve issues).

## Dashboard API

```
GET http://localhost:8765/            → HTML dashboard
GET http://localhost:8765/api/metrics     → funnel counters (JSON)
GET http://localhost:8765/api/timeseries  → bucketed time series (JSON)
```

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `TRIAGE_DATABASE_URL` | Postgres connection string | Local compose Postgres |
| `GH_TOKEN` | GitHub token for `gh` CLI operations | (none) |
| `TRIAGE_DB` | Override SQLite path (non-Postgres mode) | `data/triage.db` |

## Teardown

```bash
docker compose --profile cli down -v   # stops all services, removes volumes
```
