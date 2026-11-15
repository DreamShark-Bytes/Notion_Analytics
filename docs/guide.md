# Guide

How the sync behaves and how to tune it. For table layouts and property types see [Reference](reference.md).

## Incremental sync and full refresh

By default a run fetches only pages edited since the last successful run of that database (the timestamp is stored per database in the `_meta` table as `last_sync:<name>`, and the query starts 60 seconds earlier for overlap). A database whose sync fails keeps its old timestamp, so the next run retries the same window. The first run of a database has no timestamp and fetches everything.

`python sync.py --full` ignores the stored timestamp and re-fetches every page. Use it after changing `include_columns`, `exclude_columns`, `include_content` or other options that affect what is stored for pages that have not been edited in Notion.

## Change Tracking

When `track_changes = true` is set for a database, every property change is recorded in the `{name}_changes` table with a timestamp.

**Initial state:** On the first sync of any page, all tracked fields are written as initial records — `old_value = NULL`, `new_value` = current value, `valid_from` = the page's `created_time`. This gives you a baseline for every field from the moment Notion created the page, not just from when you started syncing.

**Subsequent syncs:** Only fields whose values have changed since the last sync are recorded. Unchanged fields produce no entry.

**Fields always excluded from tracking** (too noisy or redundant):

| Field | Reason |
|---|---|
| `last_edited_time` | Changes on every sync |
| `content_text` | Large and noisy |
| `url` | Never changes |

**Controlling which fields are tracked:**

```toml
[[databases]]
name = "tasks"
track_changes = true

# Track only these fields (allowlist):
change_fields = ["Status", "Due Date", "Assignee"]

# Or track everything except these (denylist):
exclude_change_fields = ["Notes", "Description"]
```

`change_fields` and `exclude_change_fields` use original Notion property names (not sanitized column names). If both are set, `change_fields` takes precedence.

## Content Sync

When `include_content = true` is set for a database, the full body of each Notion page is captured as plain text in the `content_text` column.

> **Performance note:** Content requires a separate API call per page. For large databases (hundreds of pages), this significantly increases sync time. Set `include_content = false` for databases where the body text is not needed.

Block types are converted to plain text as follows:

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
| Images, video, audio, PDF | `[image: caption]`, `[video]`, `[audio]`, `[pdf]` |
| File attachments | `[file: filename]` |
| Bookmark, embed, link preview | `[bookmark: url]`, `[embed: url]`, `[link_preview: url]` |
| Unsupported | `[unsupported]` |

## Usage Tips

**Use separate configs for testing**

Keep a `config_test.toml` with a different `db_path` so test runs never touch production data:

```toml
# config_test.toml
[output]
db_path = "notion_analytics_test.db"
```

```bash
venv/bin/python sync.py --config config_test.toml
```

**Limit which columns are synced**

Use `include_columns` to sync only the fields you need. Reduces table width and sync time for wide databases:

```toml
[[databases]]
name = "tasks"
include_columns = ["Name", "Status", "Due Date", "Assignee"]
```

**Declare a property rename**

Renames are followed automatically by Notion's property id: the column and its change history move to the new name on the next sync. Declaring the rename in `column_renames` is still useful when you want the history merged under the new name before the next scheduled sync runs. See [Handling Schema Changes](reference.md#handling-schema-changes).

**CSV export for simple consumers**

```toml
[output]
export_csv = true
csv_dir = "exports"   # default
```

CSV files are written alongside the SQLite file after each sync. Useful for sharing snapshots or loading into tools that don't support SQLite directly.

---

**Columns you exclude are ignored everywhere.** A property listed in `exclude_columns` (or missing from a non-empty `include_columns`) gets no column, is not tracked in `_schema`, and is not backed up if its type changes in Notion.
