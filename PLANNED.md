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

If [[select-status-option-id-tracking]] is implemented, the rename tool becomes more reliable: it can find affected rows by option UUID rather than matching on potentially-ambiguous display names.

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

| KPI                         | Source table(s)                                   | Notes                                                                                    |
| -----------------------------| ---------------------------------------------------| ------------------------------------------------------------------------------------------|
| Open Volume over time       | `tasks_changes` (Status field)                    | Count of tasks in non-Done status per day/week                                           |
| Closed Volume over time     | `tasks_changes` (Status field)                    | Count of tasks moved to Done per period                                                  |
| Mean Time to Resolve (MTTR) | `tasks_pages` (created_time, closed_date)         | Avg days from creation to close                                                          |
| Recurring Task Adherence    | `tasks_pages`, `recurring_task_definitions_pages` | Habit completion rate per cadence, per series                                            |
| Due Date Drift              | `tasks_pages` (first_due_date, due_date)          | Avg drift; distribution of how many times due date moved                                 |
| Tasks Being Avoided         | `tasks_pages`                                     | Long-open tasks sorted by age; tasks with high due date update count                     |
| Work per Pursuit            | `tasks_pages` + Pursuits relation                 | Closed task count (and eventually time) attributed per pursuit                           |
| Bot Task Creation           | `tasks_pages`                                     | Tasks created by daemon (identifiable by Period Key / Recurring Series fields being set) |

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

## Date Field: Start/End Split

**Status:** Pre-design
**One-liner:** Split Notion date properties into two SQLite columns (`fieldname_start`, `fieldname_end`) so BI tools can use native date types instead of text parsing.

### Problem
Power BI, Grafana, and SQLite have no date-range type. Date fields (e.g. `due_date`) are currently stored as a single text column that may contain a plain date or a range. This blocks native date filtering, comparison, and timeline visualizations in any BI tool.

### Proposed approach
- In `extractor.py`, detect Notion date properties and always extract into `fieldname_start` and `fieldname_end`.
- `fieldname_start` = start date (always present). `fieldname_end` = end date, NULL if not set.
- The original combined text column is removed.
- Store as ISO date strings (`YYYY-MM-DD`), or include time component if Notion supplies it.
- Migration script needed for existing tables (e.g. `tasks`: `due_date` → `due_date_start` / `due_date_end`).

### Open questions
- Should time components be preserved (Notion dates can include time), or stripped to date-only?
- Migration: one-off script in `tools/`, or a general schema migration helper?

### Dependencies
- Migration must run before next sync on any table with existing date columns.

---

## Page Icon Capture

**Status:** Pre-design
**One-liner:** Capture the page icon (emoji or image URL) from each Notion page and store it in the `_pages` table.

### Proposed approach
- Add `icon` column to each `_pages` table (TEXT, nullable). Schema auto-add via `ALTER TABLE` applies as with any new column.
- In `extractor.py`, extract `page["icon"]` from the page object:
  - `{"type": "emoji", "emoji": "✅"}` → store the emoji character.
  - `{"type": "external", "external": {"url": "..."}}` → store the URL.
  - NULL if no icon set.

### Open questions
- Should the icon type (`emoji` vs `external`) also be stored as a separate column, or just the value?

---

## Select / Status Option ID Tracking

**Status:** Pre-design
**One-liner:** Store the stable Notion UUID for each select/status option alongside its display name, enabling rename detection without breaking history.

### Problem
When a Notion select option is renamed (e.g. "In Progress" → "Active"), `_pages` self-corrects on next sync, but `_changes` permanently retains the old name. The option's UUID is stable across renames — tracking it alongside the name allows retroactive history correction.

### Proposed approach
- For select, multi-select, and status properties: also store `fieldname_id` (Notion option UUID).
- For multi-select: comma-separated UUIDs in `fieldname_id`, paralleling existing comma-separated names in `fieldname`.
- `_changes` tracks the ID column for rename detection; the name column for display.
- The option-rename tool (see Data Maintenance Tools) uses `fieldname_id` to find affected `_changes` rows reliably.

### Open questions
- Opt-in per field, or default for all select/status/multi-select fields?
- Does `change_tracker.py` need to be aware of the paired ID column to avoid producing duplicate change events (one for the name change, one for the ID non-change)?

### Dependencies
- Pairs with Data Maintenance Tools option rename tool — the ID column makes that tool more reliable.

---

## Status Property Group Tracking

**Status:** Pre-design
**One-liner:** For Notion Status properties, also store the group (e.g. "Not started", "In progress", "Done") alongside the specific status value.

### Problem
Notion Status options belong to named groups. Dashboards often need the group (coarse-grained state) rather than the specific value. Hard-coding specific status names in reports is brittle when options are renamed or reorganized.

### Proposed approach
- Add `fieldname_group` column for each Status-type property in `_pages`.
- The value→group mapping lives in the Notion database schema (not the page). Fetch the database object once per sync session to get the mapping.
- In `extractor.py`, look up the group for the current status value using the cached schema mapping.

### Open questions
- Should `fieldname_group` changes also be tracked in `_changes`?
- How to handle a status option with no group assigned (NULL or a sentinel value)?
- Cache schema in-memory per sync session, or persist to `_meta` table in SQLite?

---

## Change Tracking Backup

**Status:** Pre-design
**One-liner:** Automated backup of `_changes` tables — irreplaceable historical data that cannot be rebuilt from Notion.

### Problem
`_changes` records every field change over time. Unlike `_pages` (rebuildable via `--full` sync), this history is permanently lost if the SQLite file is corrupted or the machine fails.

### Options considered
- **CSV export (append-only)** — after each sync, append new `_changes` rows to a CSV, then copy to cloud storage. Lightweight, human-readable, trivially re-importable.
- **Full `.db` file copy** — copy the entire SQLite file to a backup location after each sync. Simple and complete, but the file grows over time.
- **`tools/backup.py` script** — standalone script that exports `_changes` to CSV and copies to a configured destination (OneDrive, Google Drive, or a local path). Scheduled separately or called from `sync.py`.
- **Git LFS** — track `.db` in git. Versioned history, but LFS storage has limits and is overkill for this use case.

### Lean recommendation
Append-only CSV export of `_changes` tables, copied to OneDrive (already on ThinkPad) after each sync. Small, readable, easy to restore.

### Open questions
- Should backup run at the end of every sync automatically, or as a separate scheduled task?
- OneDrive, Google Drive, or a local path to a second machine / NAS?
- Should `_pages` also be backed up, or only `_changes`?

---

## Bug: Checkbox / Boolean Fields Stored as Text

**Status:** Fix needed — should be resolved before significant data accumulates.
**One-liner:** Notion checkbox properties return Python `bool` but are stored in SQLite as TEXT ("True"/"False") instead of INTEGER (0/1).

### Impact
- BI tools must match strings ("True") instead of using native boolean/integer comparisons, which is fragile.
- Change tracker may log spurious events if Python's bool repr varies between runs.

### Fix
- In `extractor.py` (or `storage.py`), coerce `bool` values to `int` (1 → 1, 0 → 0) before insert.
- SQLite has no native boolean type; INTEGER 0/1 is the standard convention.
- Migration script needed: existing TEXT columns containing "True"/"False" must be converted to INTEGER.

### Dependencies
- Migration must run before next sync on any table with checkbox columns. The column type change requires `ALTER TABLE` or a table rebuild (SQLite does not support `ALTER COLUMN`).

---

