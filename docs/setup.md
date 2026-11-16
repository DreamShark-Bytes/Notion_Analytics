# Setup

Everything needed to go from a fresh clone to a scheduled, verified sync. Allow about 20 minutes. Sections 1–5 get you a working database; 6–8 are for running it unattended and using it.

## What you need

- **Python.** Developed and tested on 3.13; older versions are untested.
- **git**, to clone and update the project.
- **A Notion account** where you can create an integration and connect it to your databases.
- Optional: **Power BI Desktop** (Windows only) or **Grafana** to build dashboards.

This project only reads from Notion. It never writes to your workspace.

## 1. Get the code and install dependencies

```bash
git clone https://github.com/DreamShark-Bytes/Notion_Analytics.git
cd Notion_Analytics
python -m venv venv

# Linux / macOS
venv/bin/python -m pip install -r requirements.txt

# Windows
venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` pins the Notion_API client to a specific release tag. Do not call the Notion REST API directly from your own code in this project; everything goes through that client.

## 2. Create your Notion integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click **New integration**, give it a name, select your workspace
3. Copy the **Internal Integration Secret**
4. For each database you want to sync: open the database in Notion → `...` menu → **Connect to** → select your integration

## 3. Configure

```bash
cp config_example.toml config.toml
```

`config.toml` holds your token and is git-ignored. Never commit it or paste it anywhere.

Find each database ID in its Notion URL (the 32-character part before `?v=`). The smallest working config:

```toml
token = "ntn_your_token_here"

[[databases]]
database_id = "your-database-id-here"
name = "tasks"                # table prefix in SQLite: no spaces, lowercase
include_content = true        # include page body as plain text
include_comments = true       # include page comments
track_changes = true          # record field-level change history

[output]
db_path = "notion_analytics.db"
```

Options per `[[databases]]` block:

| Option | Default | Meaning |
|---|---|---|
| `database_id` | required | Notion database ID. (An older key named `id` still works but logs a warning.) |
| `name` | required | Table prefix in SQLite. Pages go to `<name>_pages`, history to `<name>_changes`, comments to `<name>_comments` |
| `include_content` | `true` | Store each page body as plain text. One extra API call per page, so it slows large databases |
| `include_comments` | `true` | Store page comments |
| `include_columns` / `exclude_columns` | `[]` | Allowlist or denylist of Notion property names (case-sensitive). Ignored properties get no column |
| `track_changes` | `true` | Record field-level change history |
| `change_fields` / `exclude_change_fields` | `[]` | Narrow which fields are tracked. See the [Guide](guide.md#change-tracking) |
| `column_renames` | `{}` | Declare a renamed property so history follows it. See [Reference](reference.md#handling-schema-changes) |
| `delete_guard_ratio` | `0.5` | Safety guard for `--detect-deleted`. See [Deleted and Moved Pages](deleted-pages.md) |

Options under `[output]`:

| Option | Default | Meaning |
|---|---|---|
| `db_path` | `notion_powerbi.db` | The SQLite file. Relative paths are relative to where you run `sync.py` |
| `export_csv` / `csv_dir` | `false` / `exports` | Also write CSV files after each sync |
| `content_fetch_delay_ms` | `0` | Pause between per-page content requests. Raise it (for example `200`) if Notion resets connections on large databases |

`config_example.toml` lists every option with comments.

## 4. Run the first sync

```bash
venv/bin/python sync.py       # Linux / macOS
venv\Scripts\python sync.py  # Windows
```

The first run fetches every page and can take several minutes for large databases. Output: the SQLite file at `db_path`, optionally `exports/`, and the log `notion_analytics.log` in the folder you ran it from. Later runs are incremental. Other options:

```bash
python sync.py --config other.toml   # use a different config file
python sync.py --full                # re-fetch every page
python sync.py --detect-deleted      # also mark pages that were deleted or moved
```

## 5. Verify the database

```bash
# Linux / macOS
sqlite3 notion_analytics.db ".tables"
sqlite3 notion_analytics.db "SELECT COUNT(*) FROM tasks_pages;"
sqlite3 notion_analytics.db "SELECT COUNT(*) FROM tasks_changes;"
```

On Windows use [DB Browser for SQLite](https://sqlitebrowser.org/) (free) to browse tables and run queries. Rows in `_changes` with `old_value = NULL` are initial-state records from the first sync, which is expected. The log should end with `Sync complete`, with no `Unexpected error` lines.

## 6. Schedule it

Run `sync.py` hourly with Task Scheduler, cron or a systemd timer: see [Scheduling](scheduling.md).

## 7. Connect your tools

See [Connecting Power BI and Grafana](connecting-bi-tools.md). Point them at a local copy of the database, not a file on a network share.

## 8. Optional extras

| If you want... | Read |
|---|---|
| Pages you delete or move in Notion marked in SQLite | [Deleted and Moved Pages](deleted-pages.md) |
| A safe copy of the database on another machine, or a backup before risky work | [Backups and Database Copies](backup-and-snapshots.md) |
| A warning when a copy on another machine stops updating | [Stale-Data Alert](stale-data-alert.md) |
| Typed, dashboard-ready tables (dbt, DuckDB, Parquet; experimental) | [dbt/README.md](../dbt/README.md) |

## Developer setup: running the tests

```bash
venv/bin/python -m pip install -r requirements.txt     # includes pytest
venv/bin/python -m pytest tests/ -q
```

The tests use temporary SQLite databases and a stub of the Notion client, so they need no token and make no network calls.

## Next

- [Guide](guide.md): change tracking, content sync, tuning
- [Reference](reference.md): tables, property types, schema changes
- [Updating](updating.md) and [Troubleshooting](troubleshooting.md)
