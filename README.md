# Notion → Power BI Sync

Pulls Notion databases into a local SQLite file for Power BI Import mode.
Run it on a schedule; Power BI refreshes from the SQLite file (or optional CSV exports).

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
# project is already at Notion_PowerBI/
cd Notion_PowerBI
```

### 2. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Create your Notion integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click **New integration**, give it a name, select your workspace
3. Copy the **Internal Integration Secret**
4. For each database you want to sync: open the database in Notion → `...` menu → **Connect to** → select your integration

### 4. Configure credentials

```bash
cp .env.example .env
```

Edit `.env`:

```
NOTION_TOKEN=secret_your_token_here
```

### 5. Configure databases

Edit `config.yaml`. Find each database ID in its Notion URL:
`https://notion.so/workspace/**{database-id}**?v=...`

```yaml
databases:
  - id: "abc123def456..."   # database ID from the URL
    name: "tasks"           # becomes the SQLite table prefix — no spaces
    include_content: true   # include page body as plain text
    include_comments: true  # include page comments
    exclude_columns: []     # Notion property names to skip
    track_changes: true     # record field-level change history
```

See `config.yaml` for all available options.

---

## Running the sync

```bash
# Activate the venv first if not already active
source .venv/bin/activate

# One-shot sync
python sync.py

# Use a different config file
python sync.py --config my_config.yaml

# Full refresh (re-fetches all pages, useful after config changes)
python sync.py --full
```

Output is written to `notion_powerbi.db` (SQLite) and optionally to `exports/` as CSV files.
A log is written to `notion_powerbi.log`.

---

## Scheduling the sync

### Linux — cron

```bash
crontab -e
```

Add a line (runs every hour; adjust as needed):

```
0 * * * * cd /home/vince/Documents/Notion_PowerBI && /home/vince/Documents/Notion_PowerBI/.venv/bin/python sync.py >> notion_powerbi.log 2>&1
```

### Linux — systemd timer (recommended for a server)

Create `/etc/systemd/system/notion-powerbi.service`:

```ini
[Unit]
Description=Notion to Power BI sync

[Service]
Type=oneshot
WorkingDirectory=/home/vince/Documents/Notion_PowerBI
ExecStart=/home/vince/Documents/Notion_PowerBI/.venv/bin/python sync.py
User=vince
EnvironmentFile=/home/vince/Documents/Notion_PowerBI/.env
```

Create `/etc/systemd/system/notion-powerbi.timer`:

```ini
[Unit]
Description=Run Notion Power BI sync hourly

[Timer]
OnBootSec=1min
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now notion-powerbi.timer
sudo systemctl status notion-powerbi.timer
```

---

## Connecting to Power BI

### Option A — SQLite ODBC (recommended for desktop use)

1. On your Windows machine, install the [SQLite ODBC driver](http://www.ch-werner.de/sqliteodbc/)
2. Open Power BI Desktop → **Get Data** → **ODBC**
3. Set the DSN to point at `notion_powerbi.db`
   - If the file is on your Linux server, map a network drive first (Samba), or copy it over

### Option B — CSV files (simplest)

In `config.yaml`, set:

```yaml
output:
  export_csv: true
  csv_dir: "exports"
```

Then in Power BI Desktop → **Get Data** → **Text/CSV** → point at the CSV files.
If the server is on your network, you can point Power BI at a Samba share directly.

### Option C — Power BI Service with On-Premises Gateway

Install the [Power BI On-Premises Data Gateway](https://powerbi.microsoft.com/en-us/gateway/)
on the machine that hosts `notion_powerbi.db`, then configure the gateway in Power BI Service
to reach the SQLite or CSV files. This enables cloud-based scheduled refresh.

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
| `url` | TEXT | Notion page URL |
| `content_text` | TEXT | Page body as plain text (if enabled) |
| *(property columns)* | varies | One column per Notion property, sanitized to lowercase_with_underscores |

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

1. In `config.yaml`, add the rename under the affected database:

```yaml
column_renames:
  "Old Property Name": "New Property Name"
```

2. Run `python sync.py` — data and change history are migrated automatically.
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
