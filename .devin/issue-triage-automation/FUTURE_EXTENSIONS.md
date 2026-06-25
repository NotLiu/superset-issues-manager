# Future Extensions — deferred out of the MVP

These are intentionally **out of scope for the initial 2–3h MVP** and tracked here
as the project roadmap. The MVP delivers: bounded upstream corpus in SQLite FTS5,
a Devin-session classifier orchestrating tested scripts, the `duplicate` /
`needs-review` / `invalid` branches, one `auto-resolve` class (dependency-CVE) →
draft PR, and a minimal metrics dashboard.

## Datastore & search
- **Postgres** instead of SQLite (concurrency, hosted, production scale).
- **Vector similarity** (pgvector + HNSW, or Pinecone) for semantic dedup recall
  beyond keyword/FTS matches.
- **Embeddings pipeline** + **weekly re-index cron** to keep a vector store fresh
  (only needed once a local vector index exists; FTS/GitHub stay source-of-truth
  in the MVP so no cron is needed).
- **Full corpus ingest** of all upstream `apache/superset` issues (MVP ingests a
  bounded recent subset).

## Trigger & automation
- **Real event trigger**: GitHub webhook → relay → Devin API on issue creation
  (MVP uses a CLI trigger that simulates the event).
- **Two-tier split**: a cheap non-Devin LLM (or heuristic) pre-classifier so a
  Devin session is spun only for `auto-resolve` (MVP collapses classify+act into
  one Devin session — simpler but more ACU).
- **Parallel child sessions** — one per issue at scale, with concurrency/rate
  caps.

## Auto-resolve breadth
- Widen the allow-list beyond dependency-CVE to: lint / type / `noqa` cleanups,
  docs typos / broken links, and small bugs with a clear stack trace.
- Richer verification gate (full targeted test runs, CI integration) before the
  draft PR.

## Issue-helper UX
- **Helpfulness rating** on the bot's dedup comment (👍/👎 or a reply) →
  auto-close the issue when confirmed a duplicate.
- Posting **related (not just duplicate)** issues and richer comment formatting.

## Dashboard & observability
- **Dogfood Superset** as the analytics dashboard (vs the MVP minimal page).
- Reorderable / focusable stat list + additional filters (by label, class, time
  bucket, repo).
- Cost/latency observability per Devin session beyond the MVP counters.

## Hardening
- Idempotency store to never reprocess an issue.
- Daily ACU / issue budget caps with alerting.
- Confidence-threshold tuning + scope caps (max files/lines) auto-downgrade.
- Secret management for any external LLM/provider keys (MVP uses Devin sessions,
  no external key).
