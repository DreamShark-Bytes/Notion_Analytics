# Notion Analytics — Planned Features

Living design document. Sections are deleted when a feature is implemented and its decisions have moved to `DESIGN.md`. See `STATUS.md` for one-line summaries and current priority.

---

## Configurable Sync Interval / Daemon Mode

**Status:** Pre-design
**One-liner:** Let users control how often the sync runs, either via config or by running sync.py as a daemon.

### Options considered

**Option A — Document the scheduler interval (no code change)**
The sync interval lives in the cron expression, systemd timer, or Windows Task Scheduler task. Update the README to make this obvious and easy to change. Simplest — but the "source of truth" for the interval is outside the project.

**Option B — `sync_interval_minutes` in config.toml (documentation only)**
Add `sync_interval_minutes = 60` to `config.toml`. sync.py doesn't use it at runtime, but it serves as a single place to declare intent, and the README points users there when setting up their scheduler. Still requires manually matching the scheduler to the config value.

**Option C — Daemon mode**
Add a `--daemon` flag to sync.py. When set, the process runs in a loop, sleeping `sync_interval_minutes` between runs. No external scheduler needed — just run it as a service (systemd or NSSM). Consistent with how Notion_Automator works. More self-contained for users.

### Open questions
- Is daemon mode worth the added complexity, or is the scheduler-based approach sufficient?
- If daemon mode: should `sync_interval_minutes` live in `config.toml` or be a CLI arg (`--interval 60`)?

### Dependencies
- Understand Windows Task Scheduler behavior first (in progress).

---

## Power BI Service Integration (REST API Push)

**Status:** Deprioritized — Pro license required; ODBC on ThinkPad is the current approach.
**One-liner:** Push transformed SQLite data directly to Power BI Service via REST API — no Windows machine, no gateway required.

### Why deprioritized

Free Power BI tier does not support REST API automation. Pro or Premium/Fabric license required (~$10-14/month). Service Principal auth (headless/scheduled) additionally requires Premium capacity. For a personal project where the ThinkPad is already in the production loop, ODBC from Power BI Desktop is free and sufficient.

Revisit if: (a) a Pro license becomes available, or (b) the sync needs to run without a Windows machine being involved.

### Architecture (for when this is eventually implemented)

```
Notion → sync.py → SQLite (transform layer) → push_to_powerbi.py → Power BI Service
```

SQLite remains the transformation layer. The push script sends already-clean data to Power BI Service.

### Confirmed API limits (from Microsoft docs)

- **Licensing:** Pro or Premium/Fabric required. Free tier: no API automation.
- **Batch size:** max 10,000 rows per POST request — chunk large tables.
- **POST rate:** max 120 requests/minute per dataset.
- **Hourly throughput:** max 1,000,000 rows/hour per semantic model.
- **Table size:** 200,000 rows (FIFO retention) or 5,000,000 rows (no retention policy).
- **Schema:** max 75 columns, 75 tables. Strings capped at 4,000 characters per value.
- **Auth:** Azure AD app registration + OAuth token. Service Principals require Premium.

### Push strategy per table type (when implemented)

- **`_pages` (current state):** full refresh — DELETE all rows, re-POST everything.
- **`_changes` (history):** append-only — POST only new rows since last push.

### Dependencies
- Incremental sync should be in place first so the push script knows what's new since last push.

---

## Data Maintenance Tools

**Status:** Pre-design
**One-liner:** CLI tools for correcting stale or inconsistent data in the SQLite database — select option renames and deleted page cleanup.

### Select / Status Option Rename

When a Notion select or status option is renamed (e.g. "In Progress" → "Active"), the `_pages` table self-corrects on the next sync (upsert overwrites with the new value). The problem is `_changes` historical records — they permanently retain the old option name in `old_value` and `new_value`, making trend charts inconsistent.

**Proposed tool:** a script that takes a table name, field name, old option value, and new option value, then updates all matching rows in `{table}_changes`. Preview before committing (show count of affected rows). Backup first.

### Deleted Page Cleanup

Pages deleted in Notion stop appearing in sync results but remain in SQLite indefinitely. Over time this causes stale rows in `_pages` and orphaned history in `_changes`.

**Decided: soft delete with configurable lifespan** — mirrors Notion's own archive behavior.

- Add `deleted_at` column to `_pages` — stamped when a page disappears from Notion results on a `--full` run.
- Config option per database: `archive_lifespan_days` (e.g. `90`; `0` = keep forever). After the lifespan expires, rows are hard-deleted from both `_pages` and `_changes`.
- Detection: on `--full` runs, compare page IDs returned by Notion against stored IDs. Anything in SQLite but not in Notion gets `deleted_at` stamped.
- Power BI current-state views should filter `WHERE deleted_at IS NULL`.

### Open questions
- Should deleted-page detection run automatically on every `--full`, or only with an explicit flag (e.g. `--detect-deleted`)?
- Should the option-rename tool live in a `tools/` subfolder (like Notion_Automator) or as a flag on `sync.py`?

### Dependencies
- Deleted page detection depends on incremental sync being in place first, so `--full` has a clear meaning.

---

## Incremental Sync

**Status:** Ready to implement
**One-liner:** Filter Notion queries by `last_edited_time` so only changed pages are fetched — the `--full` flag already exists as the escape hatch.

### Decisions made
- `--full` flag bypasses the filter and fetches all pages (already wired in argparse; just not used yet).
- Last-sync timestamp stored in SQLite (a `_meta` table or a sidecar `.json` file).
- On first run or `--full`, fetch everything and record the timestamp.
- On subsequent runs, query with `filter: { last_edited_time: { on_or_after: last_sync } }` plus a small overlap buffer (same boundary-guard pattern as Notion_Automator).
- Pages deleted in Notion won't appear in filtered results — need a separate strategy (periodic full sweep, or detect via `get_all_page_ids` diff). Defer until needed.

### Open questions
- Where to store the last-sync timestamp? Sidecar JSON is simpler; `_meta` table keeps everything in one file.
- How often will sync run? Hourly is probably fine for the KPIs targeted.

### Dependencies
- None. Isolated to `sync.py`.

---

## Power BI KPI Dashboards

**Status:** Pre-design (data collection running; dashboard design not started)
**One-liner:** Build Power BI reports from the SQLite data covering task volume, resolution time, recurring task adherence, due date drift, and pursuit tracking.

### KPIs identified

| KPI | Source table(s) | Notes |
|---|---|---|
| Open Volume over time | `tasks_changes` (Status field) | Count of tasks in non-Done status per day/week |
| Closed Volume over time | `tasks_changes` (Status field) | Count of tasks moved to Done per period |
| Mean Time to Resolve (MTTR) | `tasks_pages` (created_time, closed_date) | Avg days from creation to close |
| Recurring Task Adherence | `tasks_pages`, `recurring_task_definitions_pages` | Habit completion rate per cadence, per series |
| Due Date Drift | `tasks_pages` (first_due_date, due_date) | Avg drift; distribution of how many times due date moved |
| Tasks Being Avoided | `tasks_pages` | Long-open tasks sorted by age; tasks with high due date update count |
| Work per Pursuit | `tasks_pages` + Pursuits relation | Closed task count (and eventually time) attributed per pursuit |
| Bot Task Creation | `tasks_pages` | Tasks created by daemon (identifiable by Period Key / Recurring Series fields being set) |

### Open questions
- Power BI connection method: ODBC (SQLite driver) vs CSV? ODBC preferred but requires driver install on Windows.
- How to handle the Pursuits relation field (stored as comma-separated page IDs in SQLite)? Needs a lookup join to `pursuits_pages`.
- Bot task creation: no dedicated "created_by_bot" flag in current data — infer from presence of `recurring_series` relation field.
- Free tier Power BI Service limitation (1 report published). Use Power BI Desktop for local viewing; defer cloud publishing.

### Dependencies
- First sync must complete successfully before dashboard work begins.
- Pursuits and Areas tables needed for dimensional joins.

---

## Data Science / ML Exploration

**Status:** Idea (not yet designed)
**One-liner:** Use task history data and/or external datasets (Kaggle etc.) as a playground for learning data science and ML techniques in Python.

### Context
This is an open-ended personal learning pursuit, not a specific deliverable. The task database provides a real dataset with time-series properties (status changes, due date drift, completion patterns). External datasets from Kaggle or similar can supplement when the task data is too small or doesn't fit a technique.

### Possible directions
- Time-series forecasting: predict weekly closed volume from historical patterns
- Classification: predict whether a task will be completed on time based on age, type, recurring/non-recurring
- Clustering: group tasks by behavior patterns (quickly closed, chronically delayed, etc.)
- Anomaly detection: flag unusually long-open tasks or unusual due date drift
- **Pre-trained model analysis** — feed task titles, content, and history to an existing LLM or embedding model to surface insights about strengths, weaknesses, patterns, and direction. Doesn't require large amounts of training data — a few hundred tasks is enough for meaningful analysis.

### PII and data sensitivity
Task data is personal — it describes what you're doing, struggling with, and pursuing. Before any ML work:
- **Local inference preferred** — run models on-device (sentence-transformers, llama.cpp, Ollama) so data never leaves the machine. Zero PII risk.
- **API-based inference** — data is transmitted to a third-party server (OpenAI, Anthropic, etc.). API data is generally not used for training, but it is transmitted. Acceptable for task titles; think carefully before sending `content_text` from sensitive databases.
- **Mitigation already available** — `include_content = false` per database, `exclude_columns` for sensitive fields. Exclude Pursuits/Projects body content by default; include titles only.

### Open questions
- What tooling? Python (pandas, scikit-learn, statsmodels) seems right. Jupyter notebooks for exploration.
- Where does this live — in this project, or a separate `Data_Science` project? Separate is cleaner if it grows.
- Kaggle datasets: keep as a separate pursuit unrelated to Notion data.
- Local vs. API inference — decide per analysis based on data sensitivity.

### Dependencies
- Enough historical task data to be meaningful (weeks to months of sync history).
- PII decision made before any data is sent outside the machine.

---

