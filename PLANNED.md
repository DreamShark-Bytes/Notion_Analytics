# Notion PowerBI — Planned Features

Living design document. Sections are deleted when a feature is implemented and its decisions have moved to `DESIGN.md`. See `STATUS.md` for one-line summaries and current priority.

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

### Open questions
- What tooling? Python (pandas, scikit-learn, statsmodels) seems right. Jupyter notebooks for exploration.
- Where does this live — in this project, or a separate `Data_Science` project? Separate is cleaner if it grows.
- Kaggle datasets: keep as a separate pursuit unrelated to Notion data.

### Dependencies
- Enough historical task data to be meaningful (weeks to months of sync history).

---

## README Cleanup

**Status:** Ready to implement (low priority)
**One-liner:** README references `.env` / `config.yaml` that don't exist — vestigial from an earlier draft. Update to match actual TOML config.

### Changes needed
- Remove all `.env` / `config.yaml` references
- Update Setup section to match actual `config.toml` workflow
- Remove YAML config block examples; replace with TOML
- Update Power BI connection section to reflect current SQLite approach

### Dependencies
- None.
