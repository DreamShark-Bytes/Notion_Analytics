# Notion → Power BI Sync

Pulls Notion databases into a local SQLite file for Power BI Import mode.
Run it on a schedule; Power BI refreshes from the SQLite file (or optional CSV exports).

---

## Related Projects

- **[Notion_API](https://github.com/DreamShark-Bytes/Notion_API)** — shared Notion API client used by this project (pinned via `requirements.txt`)
- **[Notion_Automator](https://github.com/DreamShark-Bytes/Notion_Automator)** — companion daemon that writes automation logic back to Notion (recurring tasks, closed date stamping, etc.); this project reads what that one writes

---

## Prerequisites

- Python 3.11+
- A Notion integration with read access to your databases
- Power BI Desktop (Windows) — or Power BI Service with a gateway

---

## Setup

### 1. Clone / copy the project

```bash
cd ~/Documents
# project is already at Notion_Analytics/
cd Notion_Analytics
```

### 2. Install dependencies

```bash
python -m venv venv

# Linux / macOS
venv/bin/pip install -r requirements.txt

# Windows
venv\Scripts\pip install -r requirements.txt
```

### 3. Create your Notion integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click **New integration**, give it a name, select your workspace
3. Copy the **Internal Integration Secret**
4. For each database you want to sync: open the database in Notion → `...` menu → **Connect to** → select your integration

### 4. Configure

Copy the example config:

```bash
cp config_example.toml config.toml
```

Edit `config.toml` — add your Notion integration token and configure each database you want to sync. Find each database ID in its Notion URL: `https://notion.so/workspace/**{database-id}**?v=...`

```toml
token = "ntn_your_token_here"

[[databases]]
id = "your-database-id-here"  # database ID from the URL
name = "tasks"                # table prefix in SQLite — no spaces, lowercase
include_content = true        # include page body as plain text
include_comments = true       # include page comments
track_changes = true          # record field-level change history
```

See `config_example.toml` for all available options.

---

## Running the sync

```bash
# One-shot sync — uses config.toml in the current directory
# Linux / macOS
venv/bin/python sync.py
# Windows
venv\Scripts\python sync.py

# Use a different config file (e.g. for test vs production)
venv/bin/python sync.py --config config_test.toml   # Linux
venv\Scripts\python sync.py --config config_test.toml  # Windows

# Full refresh — re-fetches all pages, useful after config changes
venv/bin/python sync.py --full   # Linux
venv\Scripts\python sync.py --full  # Windows
```

Output is written to `notion_analytics.db` (SQLite) and optionally to `exports/` as CSV files.
A log is written to `notion_analytics.log`.

---

## Verifying the database

After the first sync, confirm the tables were created and populated.

**Install sqlite3 if needed:**
```bash
# Linux
sudo apt install sqlite3

# macOS
brew install sqlite
```
Windows: sqlite3 is included with [DB Browser for SQLite](https://sqlitebrowser.org/) — see below.

**Quick checks:**
```bash
# List all tables
sqlite3 notion_analytics.db ".tables"

# Row counts
sqlite3 notion_analytics.db "SELECT COUNT(*) FROM tasks_pages;"
sqlite3 notion_analytics.db "SELECT COUNT(*) FROM tasks_changes;"

# Peek at a few rows
sqlite3 notion_analytics.db "SELECT page_id, last_edited_time FROM tasks_pages LIMIT 5;"
```

You should see one `_pages`, `_changes`, and (if enabled) `_comments` table per configured database. Rows in `_changes` with `old_value = NULL` are the initial state records from the first sync — that's expected.

**GUI alternative:** [DB Browser for SQLite](https://sqlitebrowser.org/) (free, Windows/Mac/Linux) lets you browse tables and run queries visually. Useful for exploring the data before connecting Power BI.

---

## Scheduling the sync

### Linux — cron

```bash
crontab -e
```

Add a line (runs every hour; adjust as needed):

```
0 * * * * cd /home/vince/Documents/Notion_Analytics && /home/vince/Documents/Notion_Analytics/venv/bin/python sync.py >> notion_analytics.log 2>&1
```

### Linux — systemd timer (recommended for a server)

Create `/etc/systemd/system/notion-analytics.service`:

```ini
[Unit]
Description=Notion Analytics sync

[Service]
Type=oneshot
WorkingDirectory=/home/vince/Documents/Notion_Analytics
ExecStart=/home/vince/Documents/Notion_Analytics/venv/bin/python sync.py
User=vince
```

Create `/etc/systemd/system/notion-analytics.timer`:

```ini
[Unit]
Description=Run Notion Analytics sync hourly

[Timer]
OnBootSec=1min
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now notion-analytics.timer
sudo systemctl status notion-analytics.timer
```

### Windows — Task Scheduler

1. Open **Task Scheduler** (search "Task Scheduler" in Start)
2. Click **Create Basic Task** → give it a name (e.g. `Notion Analytics Sync`)
3. **Trigger:** Daily → set a start time → check **Repeat task every: 1 hour** → **for a duration of: Indefinitely**
4. **Action:** Start a program
   - **Program/script:** full path to the venv Python, e.g. `C:\Users\YourName\Documents\Notion_Analytics\venv\Scripts\python.exe`
   - **Add arguments:** `sync.py`
   - **Start in:** full path to the project folder, e.g. `C:\Users\YourName\Documents\Notion_Analytics`
5. Finish → right-click the task → **Run** once to verify
6. Check `notion_analytics.log` to confirm it ran successfully

> **Note:** Task Scheduler runs even when no user is logged in, but the machine must be on. In task Properties → Conditions, uncheck "Start the task only if the computer is on AC power" if you want it to run on battery.

---

## Connecting on Linux — Grafana

[Grafana](https://grafana.com/) is a free, open-source dashboarding tool that runs natively on Linux and connects directly to SQLite. It is the recommended option if you are running the sync on a Linux machine without a Windows host available.

**Setup:**

1. Install Grafana: https://grafana.com/docs/grafana/latest/setup-grafana/installation/debian/
2. Install the SQLite plugin: **Administration → Plugins** → search `SQLite` → install
3. Add a data source: **Connections → Data sources → Add → SQLite** → set the database path to your `notion_analytics.db` file
4. Build dashboards using SQL queries against the `_pages`, `_changes`, and `_comments` tables

Grafana runs as a service and is accessible from any browser on your network — no desktop app required.

---

## Connecting to Power BI

### Step 1 — Get the database file onto your Windows machine

The sync produces `notion_analytics.db` wherever you run it. Power BI Desktop needs to reach that file from Windows.

**Option A — Run the sync on your Windows PC directly (simplest)**
Python runs on Windows. Clone the repo, install dependencies, configure `config.toml`, and run `python sync.py` from a Windows terminal. The `.db` file is local and Power BI can read it directly.

**Option B — Samba network share (if sync runs on a Linux server/Pi)**
Set up a Samba share on the Linux machine, map it as a network drive on Windows, and point Power BI at the network path. The sync keeps running on the server; Power BI reads the file over the network.

### Step 2 — Connect Power BI Desktop

**Option A — SQLite ODBC driver (recommended)**

1. Download and install the [SQLite ODBC driver](http://www.ch-werner.de/sqliteodbc/) on Windows
2. Open **ODBC Data Sources (64-bit)** → **System DSN** → **Add** → select `SQLite3 ODBC Driver`
3. Give the DSN a name (e.g. `NotionPowerBI`) and set the database path to your `notion_analytics.db` file
4. In Power BI Desktop → **Get Data** → **ODBC** → select your DSN → load the tables you want

**Option B — CSV export (no driver install needed)**

In `config.toml`, set `export_csv = true` under `[output]`. After each sync, CSV files appear in `exports/`. In Power BI Desktop → **Get Data** → **Text/CSV** → point at the files. Repeat when data changes.

### Step 3 — View reports on iPad

Power BI Mobile (iOS) lets you view any report you've published to Power BI Service.

1. Create a free Microsoft / Power BI account at [app.powerbi.com](https://app.powerbi.com)
2. In Power BI Desktop: sign in with that account → **File → Publish → Publish to Power BI** → choose **My workspace**
3. On iPad: install **Microsoft Power BI** (free on the App Store) → sign in with the same account → your reports appear automatically

**Refresh on iPad:** the published report is a snapshot from when you last published. To update it, run the sync, open the report in Power BI Desktop, and publish again. (Automatic cloud refresh requires an On-Premises Data Gateway — see below if you want that later.)

### Optional — On-Premises Data Gateway (automatic cloud refresh)

Without a gateway, refreshing the cloud dataset requires opening Power BI Desktop and manually republishing. With the gateway, Power BI Service can pull fresh data from your local `.db` file on a schedule — no human step needed.

**How the full automated pipeline works:**

```
Task Scheduler → sync.py → notion_analytics.db (local)
                                    ↑
         On-Premises Data Gateway ──┘ (running on same machine)
                    ↓
         Power BI Service scheduled refresh → cloud dataset updated
                    ↓
         iPad / PC read from cloud — always current
```

**Setup:**

1. Download and install the [Power BI On-Premises Data Gateway](https://powerbi.microsoft.com/en-us/gateway/) — choose **Personal mode** (free; no Pro license required for My Workspace)
2. During setup, sign in with the **same Microsoft account** you use for Power BI Service
3. The gateway runs as a Windows background service — it starts automatically on boot
4. In [Power BI Service](https://app.powerbi.com): open your dataset → **Settings** → **Gateway and cloud connections** → map the SQLite ODBC data source to your gateway
5. Under **Scheduled refresh**: enable it and set your preferred frequency (e.g. hourly)
6. Power BI Service will now refresh the dataset automatically whenever the gateway is reachable

> **Constraint:** The ThinkPad must be on and the gateway service running when a scheduled refresh fires. If the machine is off, that refresh cycle is skipped — the next scheduled refresh will catch up.

---

## SQLite table structure

For each configured database (example name: `tasks`):

| Table | Description |
|---|---|
| `tasks_pages` | Current state — one row per Notion page, one column per property |
| `tasks_changes` | Field-level change history |
| `tasks_comments` | Page comments |

### `tasks_pages` columns

| Column | Type | Notes |
|---|---|---|
| `page_id` | TEXT (PK) | Notion page ID |
| `created_time` | TEXT | ISO 8601 |
| `last_edited_time` | TEXT | ISO 8601 |
| `url` | TEXT | Notion page URL — set column Data Category to **Web URL** in Power BI to make it a clickable link |
| `content_text` | TEXT | Page body as plain text (if enabled) — see below for block type handling |
| *(property columns)* | varies | One column per Notion property, sanitized to lowercase_with_underscores |

**`content_text` block handling:**

| Block type | Stored as |
|---|---|
| Paragraph, headings, toggles | Plain text |
| Bullet / numbered list | Plain text (prefix stripped) |
| To-do | `[x]` or `[ ]` prefix |
| Quote | `> text` |
| Callout | `\| text` |
| Code | `[code:language] text` |
| Table | Pipe-separated rows: `cell1 \| cell2 \| cell3` |
| Child page / inline database | `[child_page: Title]` or `[child_database: Title]` — not recursed into |
| Images, video, audio, PDF | `[image: caption]`, `[video]`, `[audio]`, `[pdf]` — caption included if present |
| File attachments | `[file: filename]` |
| Bookmark, embed, link preview | `[bookmark: url]`, `[embed: url]`, `[link_preview: url]` |
| Unsupported blocks | `[unsupported]` |

### `tasks_changes` columns

| Column | Notes |
|---|---|
| `page_id` | References `tasks_pages.page_id` |
| `field` | Sanitized column name |
| `old_value` | Previous value (NULL for initial record) |
| `new_value` | New value |
| `valid_from` | When the value took effect (page `created_time` for initial records, detection time for subsequent changes) |
| `detected_at` | When this sync run detected the change |

### `tasks_comments` columns

| Column | Notes |
|---|---|
| `comment_id` | Notion comment ID (PK) |
| `page_id` | References `tasks_pages.page_id` |
| `created_time` | ISO 8601 |
| `last_edited_time` | ISO 8601 |
| `text` | Comment body as plain text |

---

## Handling Notion schema changes

### Renamed property

1. In `config.toml`, add the rename under the affected database:

```toml
column_renames = {"Old Property Name" = "New Property Name"}
```

2. Run `venv/bin/python sync.py` — data and change history are migrated automatically.
3. Remove the `column_renames` entry (it's safe to leave, but cleaner without).

### New property

No action needed — new columns are added to SQLite automatically on the next sync.

### Deleted property

The column remains in SQLite with its historical data but stops being updated.
No data is lost.

---

## Notes on property types

| Notion type | Stored as |
|---|---|
| Title, Rich text | Plain text string |
| Number | REAL |
| Select, Status | Option name string |
| Multi-select | Comma-separated names |
| Date | ISO string; date ranges as `start/end` |
| Checkbox | 1 / 0 |
| Formula | Computed result (not the formula expression — API limitation) |
| Relation | Comma-separated related page IDs |
| Rollup | Number (count for arrays), date start, or number |
| People | Comma-separated names |
| Files / Images | `True`/`False` (default) — or raw file objects, or excluded; controlled by `files_handling` parameter on `extract_page_row` |
| Created by / Last edited by | **Excluded** |

---

## Resetting the database

To start fresh (e.g. switching from test to production data, or after a config overhaul):

```bash
rm notion_analytics.db
python sync.py
```

The next run recreates all tables and re-syncs everything from Notion. Change history starts fresh from that point.

For testing without touching production data, use a separate config file with a different `db_path`:

```toml
# config_test.toml
[output]
db_path = "notion_powerbi_test.db"
```

```bash
python sync.py --config config_test.toml
```

---

## Credits

Developed in collaboration with Claude Code by Anthropic. All architectural decisions, data model design, requirements definition, and production deployment are owned by the human author. Claude assisted with implementation, documentation, and code review under directed oversight — a design-led workflow where nothing ships without human review and approval.

---

## Troubleshooting

### Sync errors

- **`Failed to fetch database schema`** — check that your integration token is correct and the integration has been connected to the database in Notion (`...` menu → Connect to).
- **`No databases configured`** — `config.toml` is missing `[[databases]]` entries, or the wrong config file is being used.
- Check `notion_analytics.log` for full error details — the terminal only shows a summary.

### Power BI can't find the SQLite file

- Verify the file path in your ODBC DSN points to the actual `.db` file location.
- If the file is on a network share, make sure the drive is mapped and accessible before opening Power BI.

### Columns missing in Power BI

- A new Notion property won't appear until the next sync after it was added.
- If `include_columns` is set in `config.toml`, only listed columns are synced — check that list.

### Change history looks wrong after a Notion property rename

- Add the old and new names to `column_renames` in `config.toml` and run the sync once. See [Handling Notion schema changes](#handling-notion-schema-changes).
