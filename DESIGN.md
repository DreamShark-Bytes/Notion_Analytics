# Notion Analytics — Design

Settled architecture and decisions. Updated only when a decision is finalized and will not be revisited.

---

## Deployment Architecture

Production host: Windows machine (ThinkPad) running both the sync and the gateway.

```
Task Scheduler
    └─ runs sync.py on a schedule (e.g. hourly)
            └─ writes notion_analytics.db (local SQLite)

On-Premises Data Gateway (personal mode, background service)
    └─ bridges Power BI Service to local notion_analytics.db via ODBC
            └─ Power BI Service scheduled refresh reads through gateway
                    └─ cloud dataset updated automatically
                            └─ iPad / PC read from cloud — no manual step
```

Linux alternative: Grafana reads the SQLite file directly as a service — no gateway needed. Both paths are documented in the README and remain supported.

**Constraint:** the Windows host must be on and the gateway service running when a Power BI Service scheduled refresh fires.

---

## Architecture Overview

Four modules:

| Module              | Role                                                                                |
| ---------------------| -------------------------------------------------------------------------------------|
| `sync.py`           | Entry point. Loads config, drives per-database sync loop, handles CSV export.       |
| `extractor.py`      | Converts raw Notion API page dicts into flat `{col: value}` dicts ready for SQLite. |
| `storage.py`        | SQLite persistence. Auto-evolves schema as new Notion columns appear.               |
| `change_tracker.py` | Compares new row against stored snapshot; emits field-level change records.         |
| `notion_api.py`     | Shared library (Notion_API project, pinned via requirements.txt).                   |

---

## Storage: SQLite

**Decision:** SQLite as primary storage. CSV export is optional (off by default).

**Rationale:** SQLite is portable, zero-setup, and Power BI can connect to it via ODBC (Windows) or as a flat file. CSV is available as a fallback for simpler Power BI import modes, but SQLite is the source of truth.

WAL mode enabled (`PRAGMA journal_mode=WAL`) for concurrent read access while sync is writing.

---

## Table Structure

Per configured database (example prefix: `tasks`):

| Table             | Description                                                      |
| -------------------| ------------------------------------------------------------------|
| `{name}_pages`    | Current state — one row per Notion page, one column per property |
| `{name}_changes`  | Field-level change history — one row per detected change         |
| `{name}_comments` | Page comments (optional, per-database flag)                      |

### `{name}_pages` fixed columns

`page_id` (PK), `created_time`, `last_edited_time`, `url`, `content_text` (optional).
All Notion properties appended as sanitized columns.

### `{name}_changes` columns

`id` (PK autoincrement), `page_id`, `field`, `old_value`, `new_value`, `valid_from`, `detected_at`.
- `valid_from` = `page.created_time` for initial records; `detected_at` for subsequent changes.
- `old_value` = NULL for initial records (first time a page is seen).

### `{name}_comments` columns

`comment_id` (PK), `page_id`, `created_time`, `last_edited_time`, `text`.

---

## Schema Evolution

New Notion properties are added via `ALTER TABLE ADD COLUMN` automatically on the next sync. No manual migration needed for new columns.

Renamed properties: declare in `config.toml` under `column_renames`. On next sync, data is copied from old column to new column and change history is updated. Remove the entry after first successful sync.

Deleted properties: the column remains in SQLite with its historical data but stops being updated. No data is lost.

---

## Column Sanitization

Notion property names → SQLite column names via `sanitize_col()`:
- Non-word characters → `_`
- Multiple `_` → single `_`
- Strip leading/trailing `_`
- Lowercase
- Leading digit → prefix with `_`

**Convention:** database `name` in config should be `lowercase_with_underscores` (no spaces). SQLite handles quoted spaces, but snake_case is cleaner for Power BI.

---

## Change Tracking

**Always excluded from tracking** (regardless of config): `last_edited_time`, `content_text`, `url`.

**User-controlled:** `change_fields` (opt-in list) and `exclude_change_fields` (opt-out list) per database in config.toml.

**Comparison:** values compared as strings to handle SQLite/Python type mismatches.

---

## Sync Mode

Currently: full fetch every run — all pages queried from Notion on every sync, regardless of `--full` flag (flag is accepted but not yet wired to incremental logic). See PLANNED.md for incremental sync.

---

## Config Format

TOML (same as Notion_Automator). One `[[databases]]` block per Notion database. `[output]` section for SQLite path and CSV export settings.

---

## Notion_API Dependency

Pinned in `requirements.txt` to a specific git tag:
```
notion-api @ git+https://github.com/DreamShark-Bytes/Notion_API.git@v1.0.1
```

| Notion PowerBI | [Notion API](https://github.com/DreamShark-Bytes/Notion_API) |
| ----------------| --------------------------------------------------------------|
| v1.x           | v1.x                                                         |

---

## Decision Log

| Decision                                                | Rationale                                                                                                |
| ---------------------------------------------------------| ----------------------------------------------------------------------------------------------------------|
| SQLite over a dedicated Notion database for storage     | No API overhead on reads; portable; Power BI connects natively via ODBC                                  |
| Change tracking in the sync tool (not Notion_Automator) | PowerBI owns its own history; Automator's change tracking (if implemented) is separate and complementary |
| Per-field change tracking (not just last_edited_time)   | last_edited_time changes on any edit; field-level tracking is what makes trend analysis possible         |
| `include_content` off by default for non-task DBs       | Page content is large and noisy; not needed for most KPI calculations                                    |
| No write-back to Notion from this project               | This project is read-only. Bulk edits to Notion belong in Notion_Automator's tools/ folder.              |
