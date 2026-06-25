# `triage.py` usage

`triage.py` is the CLI entrypoint for the issue-triage MVP. It retrieves
candidate issues (`search.py`), classifies the issue into one of four labels,
then routes (`act.py`) and records (`record.py`) the decision. Run it from this
directory so the frozen-module imports and the corpus DB resolve correctly:

```bash
cd .devin/issue-triage-automation/scripts
```

The CLI is the **MVP stand-in for the on-issue-created event**. The real trigger
path — **GitHub webhook → relay → Devin API** on issue creation — is **DEFERRED**
(see `../FUTURE_EXTENSIONS.md`). Until then, invoking `triage.py` simulates that
event for a single issue.

## Prerequisites: build the corpus DB

The runtime DB (`../data/triage.db`) is gitignored; rebuild it from the committed
JSONL seed (no GitHub calls needed):

```bash
python ingest.py --from-seed ../data/corpus__apache__superset.jsonl
```

`--text "..."` is for **offline testing** (synthetic issue number `0`); always
pair it with `--dry-run` so no real GitHub writes happen. Use `--issue N` (which
fetches the real issue via `gh`) for live triage; keep `--dry-run` while testing.

## Invocation

```text
python triage.py (--issue N | --text "...") [--repo OWNER/REPO]
    [--classifier {heuristic,devin-session}] [--k N]
    [--dup-floor FLOAT] [--dup-dominance FLOAT]
    [--decision-json '<json>'] [--decision-file PATH]
    [--dry-run] [--json]
```

Defaults: `--repo NotLiu/superset-issues-manager`, `--classifier heuristic`,
`--k 10`, `--dup-floor 12.0`, `--dup-dominance 2.0`. Exactly one of
`--issue` / `--text` is required.

## Classifiers

- **`heuristic`** (default, offline, deterministic) — keyword/score rules, no
  LLM. Good for tests and CI; runs fully offline against the local corpus.
- **`devin-session`** (production) — the Devin session itself is the classifier
  (no API key) via a **two-call flow**: call 1 (no decision) prints a
  `classification_request` JSON for the session to read; call 2 passes the
  session's `--decision-json` to route + record. See
  `../playbooks/triage.md` for the full procedure.

---

## Examples — all four outcomes (`heuristic`, offline)

Each uses `--text` + `--dry-run` so it is safe and reproducible offline. Add
`--json` to any of them for the full result object.

```bash
# auto-resolve — text mentions a dependency bump / CVE (label only; no resolver)
python triage.py --dry-run \
  --text "Bump flask from 2.3.3 to 3.1.3 (CVE-2026-27205)"

# duplicate — strongly matches an existing corpus issue (clears --dup-floor)
python triage.py --dry-run \
  --text "Dashboard fails to load charts, request times out with a 500 error"

# invalid — short, no repro steps / version / logs
python triage.py --dry-run \
  --text "superset is broken please fix"

# needs-review — actionable but no dominant duplicate (fallback)
python triage.py --dry-run \
  --text "Steps to reproduce: on version 3.1.0, exporting a CSV from SQL Lab \
returns an empty file. No error in the logs. Need a maintainer to look."
```

> `auto-resolve` and `invalid` are decided purely from the issue text, so they
> are deterministic. `duplicate` vs `needs-review` depends on the corpus
> retrieval scores (tune with `--dup-floor` / `--dup-dominance`); rebuild the
> corpus first.

`auto-resolve` applies **only** the `auto-resolve` label and prints that
automated resolution is reserved for a future resolver and is not yet
implemented — it opens no PR.

---

## Examples — `devin-session` two-call flow

```bash
# Call 1: get the classification_request (NO routing/recording happens)
python triage.py --classifier devin-session \
  --text "Dashboard fails to load charts, request times out with a 500 error"

# (the Devin session reads text + candidates, then decides label/confidence/
#  matched/evidence)

# Call 2: route + record with the session's decision (same --text/--k)
python triage.py --classifier devin-session --dry-run \
  --text "Dashboard fails to load charts, request times out with a 500 error" \
  --decision-json '{"label":"duplicate","confidence":0.88,"matched":[41234],"evidence":"same chart-load timeout; tracked upstream"}'
```

Use `--issue N` instead of `--text` for live issues. `--decision-file PATH` is
an alternative to inline `--decision-json`. The `matched` numbers must come from
the candidate set returned in call 1, and call 2 must use the same
`--issue`/`--text` and `--k` so those candidates resolve identically.

---

## Output

Default output is a human-readable summary. `--json` prints the full result:

```json
{
  "repo": "NotLiu/superset-issues-manager", "issue": 0, "classifier": "heuristic",
  "label": "duplicate", "confidence": 0.9,
  "matched": [{"number": 41234, "title": "...", "html_url": "...",
               "relevance": 18.4, "state": "closed"}],
  "evidence": "...", "action": "comment", "outcome": "deflected",
  "candidates_considered": 10
}
```

Under `--dry-run`, `act.py` prints the `gh` command it *would* run instead of
writing to GitHub.

## Related

- `../playbooks/triage.md` — the full Devin-session triage playbook.
- `../AGENTS.md`, `../DESIGN.md` — project context and locked decisions.
- `record.py metrics --json` — dashboard counters after triaging issues.
