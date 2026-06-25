# Playbook — Devin issue-triage session

This is the step-by-step procedure a **Devin session** follows to act as the
`devin-session` classifier for the Superset issue-triage MVP: retrieve candidates,
classify the issue with the session's own judgment, then route and record the
decision through the frozen scripts.

Read `../AGENTS.md` and `../DESIGN.md` first — they hold the locked decisions.
This playbook only documents the *operational loop*; it does not relitigate scope.

---

## 0. Trigger (CLI MVP stand-in)

The production trigger is **on-issue-created**: a new issue is opened on the fork
(`NotLiu/superset-issues-manager`, standing in for `apache/superset`) and that
event starts one triage session for that issue.

For the MVP the trigger is a **CLI invocation** that simulates the event — you
(the session) are handed an issue number (or raw text) and run `triage.py`. The
real webhook → Devin API wiring is **deferred** (see `TRIAGE_USAGE.md` and
`../FUTURE_EXTENSIONS.md`); treat the CLI run as the on-issue-created event.

All work happens from the scripts directory:

```bash
cd .devin/issue-triage-automation/scripts
```

`triage.py` imports the frozen `search`, `act`, and `record` modules and resolves
the corpus DB via `db.py`, so it must be run from `scripts/` (same as the other
tools). Make sure the corpus DB exists first (see "Prerequisites" below).

---

## 1. End-to-end flow

```
on-issue-created (CLI stand-in)
        │
        ▼
  search.py  ── cheap FTS5 candidate retrieval (top ~K), no LLM
        │
        ▼
  classify   ── the Devin session's OWN judgment over the candidate set
        │        → {label, confidence, matched, evidence}
        ▼
  act.py     ── route: comment / label (honors --dry-run, no real writes)
        │
        ▼
  record.py  ── persist the classification + the run/outcome to SQLite
```

`triage.py` orchestrates all four steps. As the `devin-session` classifier you
supply **only** step 2 (the judgment); `triage.py` performs retrieval, routing,
and recording for you by calling the frozen modules. **Do not** call `act.py` or
`record.py` yourself for a normal triage — that would double-record.

---

## 2. The two-call `devin-session` flow

In `--classifier devin-session` mode the Devin session **is** the classifier
(no external LLM key). Because a session cannot block inside a single process
waiting for its own judgment, the flow is split into two CLI calls against the
same issue/text:

### Call 1 — get the `classification_request`

Run `triage.py` with `--classifier devin-session` and **no decision** flag. It
performs retrieval and prints a JSON request to stdout, then exits **without
routing or recording**:

```bash
python triage.py --issue 123 --classifier devin-session
```

Output shape:

```json
{
  "classification_request": {
    "repo": "NotLiu/superset-issues-manager",
    "issue": 123,
    "text": "<title>\n\n<body>",
    "candidates": [
      {"number": 41234, "title": "...", "html_url": "...", "relevance": 18.4,
       "state": "closed", "labels": "...", "source_repo": "apache/superset"}
    ]
  }
}
```

Read `text` and `candidates`. Using your own judgment, decide the
**label**, **confidence** (0.0–1.0), **matched** (a list of candidate issue
**numbers** drawn only from the `candidates` set), and a one-line **evidence**
string. See §3 for how to pick the label.

### Call 2 — route + record

Re-invoke `triage.py` with the **same** `--issue`/`--text` and `--k` (so the
candidate set is identical and `matched` numbers resolve back to the same
candidate dicts), passing your decision as JSON:

```bash
python triage.py --issue 123 --classifier devin-session --dry-run \
  --decision-json '{"label":"duplicate","confidence":0.88,"matched":[41234],"evidence":"same Postgres SSL error; closed by #41234"}'
```

`--decision-file PATH` is equivalent if the JSON is large or awkward to inline.
From here `triage.py` routes and records exactly as the `heuristic` classifier
would (the routing/recording path is identical regardless of classifier).

> Keep `--dry-run` on every example/test run: it is passed through to all
> `act.py` actions so no real GitHub comment or label is written.

---

## 3. Choosing the label

Pick exactly one of the four labels. When genuinely unsure, prefer
`needs-review` — a false `auto-resolve`/`duplicate` is the costly mistake;
routing to a human is the safe default.

### `duplicate`
The issue is already tracked by one or more existing issues in the candidate
set. Choose this when at least one candidate is a strong match (shares the same
root cause / error / reproduction), not merely keyword overlap. Set `matched`
to the most relevant candidate numbers (top ~3).

- **Routing:** `triage.py` renders a top-3 comment via
  `act.render_dup_comment(...)`, posts it with `act.comment(...)`, and adds the
  `duplicate` label with `act.label(...)`.
- **Recorded as:** action `comment`, outcome `deflected`.

### `invalid`
The report is not actionable — it lacks the basics a maintainer needs
(reproduction steps, Superset version, and logs/traceback). Choose this for
empty, vague, or "it's broken" reports with no diagnostic content.

- **Routing:** `triage.py` posts a fixed comment requesting **repro steps +
  Superset version + logs/traceback**, and adds the `invalid` label.
- **Recorded as:** action `comment`, outcome `deflected`.

### `needs-review`
Actionable but no clear duplicate and not safely auto-resolvable — a real bug or
question that needs a human maintainer. This is the fallback when no other label
clearly fits.

- **Routing:** `triage.py` adds the `needs-review` label only (no comment).
  `matched` may carry related (non-duplicate) candidate numbers for context.
- **Recorded as:** action `label`, outcome `needs_human`.

### `auto-resolve`
The issue is an **easy, well-scoped fix** you can implement and ship as a draft
PR: a small bug with a clear cause/location, a typo, a copy/UI tweak, etc. This
is a judgment only you (the session) can make — choose it when you are confident
you can reproduce the problem, find the cause, and write a *minimal* fix. When in
doubt prefer `needs-review`; a wrong `auto-resolve` is the expensive mistake.
This label is **not** about dependencies/CVEs. The offline `heuristic` classifier
never returns `auto-resolve` (it cannot make this judgment).

- **Routing (two phases):**
  1. `triage.py` adds the `auto-resolve` label, records the classification, and
     prints an `auto_resolve_request` handoff (it does **not** record a terminal
     run — that is deferred to phase 2).
  2. You then run the resolver loop **as this session**: reproduce → scan the
     codebase → plan → implement a minimal fix in the working tree → open a
     **draft** PR with `resolve.py open-pr` (see §3.1). `resolve.py` opens the
     PR, comments the link on the issue, and records the run.
- **Recorded as:** action `auto_resolve`, outcome `pr_opened` (or `error` if the
  PR could not be opened). Never auto-merge; never close the issue.

#### 3.1 Opening the draft PR (`resolve.py`)

After you have implemented the fix in the working tree, open the draft PR from
the `scripts/` directory, naming exactly the files you changed:

```bash
python resolve.py open-pr \
  --repo NotLiu/superset-issues-manager --issue 123 \
  --files "superset/path/one.py superset/path/two.py" \
  --title "fix: <concise summary> (#123)" \
  --body-file /tmp/pr_body.md \
  --session "$DEVIN_SESSION_ID" --acu 1.7
```

`resolve.py` creates a fresh branch, stages **only** the named files, commits,
pushes, and opens the PR with `gh pr create --draft`. Write `--body-file` as a
reviewer-facing description of the change (what was wrong, what you changed, how
you verified). It refuses to run if none of the named files actually changed.
Pass `--dry-run` to print every git/gh command without touching git, GitHub, or
the metrics store. On any failure it records the run as `outcome=error`.

---

## 4. Output & verification

By default `triage.py` prints a human-readable summary. Add `--json` for the full
machine-readable result object:

```json
{
  "repo": "...", "issue": 123, "classifier": "devin-session",
  "label": "duplicate", "confidence": 0.88,
  "matched": [{"number": 41234, "title": "...", "html_url": "...",
               "relevance": 18.4, "state": "closed"}],
  "evidence": "...", "action": "comment", "outcome": "deflected",
  "candidates_considered": 10
}
```

Confirm the `label`, `action`, and `outcome` match your intent before treating
the issue as triaged. Under `--dry-run`, `act.py` prints the `gh` command it
*would* run (prefixed `[dry-run] gh ...`) instead of writing to GitHub.

---

## 5. Frozen scripts referenced (real CLIs)

`triage.py` calls these as imported modules; their standalone CLIs are also the
way to inspect/debug each step. **Do not edit these files.**

| script | what it does | standalone CLI |
|---|---|---|
| `search.py` | FTS5 BM25 candidate retrieval over the corpus | `python search.py --text "..." --k 10 [--json]` |
| `act.py` | GitHub comment/label + dup-comment renderer | `python act.py comment\|label\|dup-comment --repo O/R --issue N [--dry-run]` |
| `record.py` | persist classification + run; dashboard metrics | `python record.py classification\|run\|metrics\|timeseries` |
| `resolve.py` | auto-resolve draft-PR flow (branch/commit/push/draft PR + record) | `python resolve.py open-pr --repo O/R --issue N --files "..." --title "..." --body-file F [--dry-run]` |
| `db.py` | SQLite schema + connection (corpus + metrics) | `python db.py` (init) |
| `ingest.py` | build/rebuild the corpus (read-only upstream) | `python ingest.py --repo apache/superset --limit 300` |

The recording enums come from `db.py`: action ∈ `comment|label|auto_resolve|none`;
outcome ∈ `deflected|pr_opened|left_open|needs_human|error`.

---

## 6. Prerequisites

- Run from `.devin/issue-triage-automation/scripts/`.
- The corpus DB (`data/triage.db`, gitignored) must exist. Rebuild it from the
  committed JSONL seed without any GitHub calls:

  ```bash
  python ingest.py --from-seed ../data/corpus__apache__superset.jsonl
  ```

  (or re-run `python ingest.py --repo apache/superset --limit 300` with `gh`
  authenticated). Override the DB path with the `TRIAGE_DB` env var if needed.
- For `--issue N`, `gh` must be authenticated (the issue is fetched via
  `gh issue view`). For offline testing use `--text "..."` with `--dry-run`.

See `TRIAGE_USAGE.md` for copy-paste example commands covering all four labels in
both `heuristic` and `devin-session` modes.
