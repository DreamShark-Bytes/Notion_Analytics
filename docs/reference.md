# Reference

## SQLite Table Structure

For each configured database (example: `tasks`):

| Table | Description |
|---|---|
| `tasks_pages` | Current state — one row per Notion page |
| `tasks_changes` | Field-level change history |
| `tasks_comments` | Page comments (if enabled) |

### `tasks_pages` columns

| Column | Type | Notes |
|---|---|---|
| `page_id` | TEXT (PK) | Notion page ID |
| `created_time` | TEXT | ISO 8601 |
| `last_edited_time` | TEXT | ISO 8601 |
| `url` | TEXT | Notion page URL |
| `content_text` | TEXT | Page body as plain text (if enabled) |
| `deleted_at` | TEXT | When `--detect-deleted` first noticed the page missing; empty for live pages (see [Deleted and Moved Pages](deleted-pages.md)) |
| `delete_state` | TEXT | `trashed`, `gone` or `moved`; empty for live pages |
| `moved_to` | TEXT | Config name of the database a moved page now lives in, if you sync it |
| *(property columns)* | varies | One column per Notion property, sanitized to `lowercase_with_underscores` |

### `tasks_changes` columns

| Column | Notes |
|---|---|
| `page_id` | References `tasks_pages.page_id` |
| `field` | Sanitized column name |
| `old_value` | Previous value (NULL for initial record) |
| `new_value` | New value |
| `valid_from` | When the value took effect (page `created_time` for initial records, detection time for changes) |
| `detected_at` | When this sync run detected the change |

### `tasks_comments` columns

| Column | Notes |
|---|---|
| `comment_id` | Notion comment ID (PK) |
| `page_id` | References `tasks_pages.page_id` |
| `created_time` | ISO 8601 |
| `last_edited_time` | ISO 8601 |
| `text` | Comment body as plain text |

### System tables

| Table | Purpose |
|---|---|
| `_meta` | `key`/`value` pairs. `last_sync:<name>` holds the start time of the last successful sync of each database |
| `_schema` | One row per tracked Notion property: `table_name`, `property_id`, `property_name`, `column_name`, `property_type`, `first_seen`, `last_seen`. Used to detect renames and type changes |
| `_options` | One row per select, multi-select or status option: its id and name. Used to detect option renames |
| `_backups` | One row per column kept after a type change: `table_name`, `column_name`, `backup_column`, `type`, `created_at` |

## Property Types

A column's SQLite type is chosen from the first value the sync sees for it: whole numbers and checkboxes become `INTEGER`, decimals `REAL`, everything else `TEXT`. **A property whose first synced value was empty becomes `TEXT`**, so a number column can hold text such as `'3'`. In Power BI or SQL, cast it (`CAST(col AS REAL)`) rather than assuming a numeric type.

| Notion type | Stored as |
|---|---|
| Title, Rich text | Plain text string |
| Number | The number (see the note above about the column type) |
| Select, Status | Option name, plus the option id in `<name>_id` |
| Multi-select | Comma-separated names, plus ids in `<name>_id` |
| Date | Two columns, `<name>_start` and `<name>_end` (ISO text; `_end` is empty for a single date) |
| Checkbox | `1` / `0` |
| Formula | The computed result (the API returns the value, not the formula); a boolean result is `1` / `0` |
| Relation | Comma-separated related page IDs |
| Rollup | The rolled-up value: a number, a date start, or the values of an array as text |
| People | Comma-separated names |
| Files / Images | `1` if any file is attached, otherwise `0` |
| Created by / Last edited by | **Excluded** |

## Handling Schema Changes

Properties that `exclude_columns` / `include_columns` ignore are skipped by all of this: they have no column, are not recorded in `_schema`, and are never backed up.

### Renamed property

1. Add the rename to `config.toml` under the affected database before the next sync runs:

```toml
column_renames = {"Old Property Name" = "New Property Name"}
```

2. Run the sync. Data and change history are migrated automatically.
3. Remove the entry once done (safe to leave, but cleaner without).

Renames are also detected by Notion's property id, so a rename you forgot to declare is still followed, but declaring it keeps the change history under one name.

### New property

No action needed. The column is added automatically the next time a page that carries it is synced.

### Deleted property

The column remains in SQLite with its historical data but stops being updated. The log warns `Column '<name>' ... no longer exists in the Notion schema`.

### Changed property type

When a property's type changes in Notion, Notion converts or discards the old values, so the SQLite copy may be the only place they survive. The sync renames the old column to `<column>_bak_YYYYMMDD`, records it in `_backups`, and creates a fresh column for the new type on the next synced row. If the property later returns to the old type, the backup is renamed back.

- The log shows a WARNING: `Property '<name>' changed type '<old>' → '<new>'. Old data preserved in '<column>_bak_YYYYMMDD'.`
- Date properties are stored as `<name>_start` and `<name>_end`, which are not renamed; they keep their old data and stop updating.
- If the column was never created (for example, no page has carried the property yet), nothing is backed up, a WARNING says so, and the new type is recorded.
- Backups stay in the database indefinitely, and no tool removes them yet.
- This is protection against Notion changing your data, not against file corruption: the backup lives in the same database file. For that, see [Backups and Database Copies](backup-and-snapshots.md).

## Resetting the Database

```bash
rm notion_analytics.db    # Linux
del notion_analytics.db   # Windows

venv/bin/python sync.py   # Linux
venv\Scripts\python sync.py  # Windows
```

The next run recreates all tables. Change history starts fresh from that point.

---
