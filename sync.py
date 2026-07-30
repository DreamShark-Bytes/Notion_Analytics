"""
sync.py
Main entry point for the Notion → Power BI sync.

Usage:
    python sync.py                   # one-shot sync using config.toml
    python sync.py --config my.toml  # use a different config file
    python sync.py --full            # ignore last-sync timestamp, fetch all pages
"""

import argparse
import logging
import time
import sys
from datetime import datetime, timedelta, timezone

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("Python < 3.11 detected: install tomli with 'pip install tomli'")

from notion_api import NotionClient
from extractor import extract_page_row, extract_comments, extract_content, sanitize_col, _SKIP_TYPES
from storage import Storage
from change_tracker import detect_changes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("notion_analytics.log"),
    ],
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Config loading
# ------------------------------------------------------------------ #

_OPTION_TYPES = {"select", "status", "multi_select"}


def _upsert_options(
    table: str,
    property_id: str,
    prop_type: str,
    schema: dict,
    storage: Storage,
    now: str,
):
    if prop_type in ("select", "multi_select"):
        for opt in schema.get(prop_type, {}).get("options", []):
            storage.upsert_option(table, property_id, opt["id"], opt["name"], None, now)
    elif prop_type == "status":
        status_def = schema.get("status", {})
        opt_group = {}
        for g in status_def.get("groups", []):
            for oid in g.get("option_ids", []):
                opt_group[oid] = g["name"]
        for opt in status_def.get("options", []):
            storage.upsert_option(
                table, property_id, opt["id"], opt["name"], opt_group.get(opt["id"]), now
            )


def _do_backup(
    table: str,
    col: str,
    prop_name: str,
    stored_type: str,
    prop_type: str,
    storage: Storage,
    now: str,
):
    date_str = now[:10].replace("-", "")
    backup_name = storage.backup_column(table, col, date_str)
    storage.record_backup(table, col, backup_name, stored_type, now)
    logger.warning(
        f"[{table}] Property '{prop_name}' changed type "
        f"'{stored_type}' → '{prop_type}'. "
        f"Old data preserved in '{backup_name}'. "
        f"Run tools/cleanup_orphaned_columns.py to remove it when satisfied."
    )


def _compare_schema(
    table: str,
    notion_schema: dict,
    storage: Storage,
    now: str,
) -> set[str]:
    """
    Compare the current Notion schema against the stored _schema table.

    Automatically applies renames when a property_id is seen under a new name.
    Renames the old column to col_bak_YYYYMMDD when a property type changes.
    Logs WARNING for properties that have disappeared (may have been deleted).
    Returns the set of _id shadow column names for select/status/multi_select fields.
    """
    stored = storage.get_stored_schema(table)
    notion_props = notion_schema.get("properties", {})
    seen_ids: set[str] = set()
    id_shadow_cols: set[str] = set()

    for prop_name, schema in notion_props.items():
        prop_type = schema.get("type")
        if prop_type in _SKIP_TYPES:
            continue
        property_id = schema.get("id")
        if not property_id:
            continue

        col = sanitize_col(prop_name)
        seen_ids.add(property_id)

        if property_id in stored:
            entry = stored[property_id]
            stored_col = entry["column_name"]
            stored_name = entry["property_name"]
            stored_type = entry["property_type"]

            if prop_name != stored_name:
                logger.info(
                    f"[{table}] Property renamed in Notion: '{stored_name}' → '{prop_name}' "
                    f"(column: '{stored_col}' → '{col}'). Migrating data automatically."
                )
                storage.rename_column(table, stored_col, col)

            if prop_type != stored_type:
                existing_backup = storage.get_backup_for_type(table, col, prop_type)
                if existing_backup:
                    restored = storage.restore_backup(table, col, existing_backup)
                    if restored:
                        logger.info(
                            f"[{table}] Property '{prop_name}' type reverted "
                            f"'{stored_type}' → '{prop_type}'. Restored from '{existing_backup}'."
                        )
                    else:
                        _do_backup(table, col, prop_name, stored_type, prop_type, storage, now)
                else:
                    _do_backup(table, col, prop_name, stored_type, prop_type, storage, now)

        storage.upsert_schema(table, property_id, prop_name, col, prop_type, now)

        if prop_type in _OPTION_TYPES:
            id_shadow_cols.add(col + "_id")
            _upsert_options(table, property_id, prop_type, schema, storage, now)

    for property_id, entry in stored.items():
        if property_id not in seen_ids:
            logger.warning(
                f"[{table}] Column '{entry['column_name']}' (Notion property '{entry['property_name']}') "
                f"no longer exists in the Notion schema — it may have been deleted or renamed. "
                f"Run tools/cleanup_orphaned_columns.py to remove it."
            )

    return id_shadow_cols


def _expand_change_fields(fields: list[str], sample_row: dict) -> list[str]:
    """
    Expand sanitized date field names to their _start/_end split variants.

    When a Notion date column (e.g. "due_date") has been migrated to
    "due_date_start" / "due_date_end", the base name no longer appears in
    page rows. This maps the base name to whichever split variants exist so
    change tracking continues working without any config change.
    """
    if not fields:
        return fields
    expanded = []
    for f in fields:
        if f in sample_row:
            expanded.append(f)
        elif f"{f}_start" in sample_row or f"{f}_end" in sample_row:
            if f"{f}_start" in sample_row:
                expanded.append(f"{f}_start")
            if f"{f}_end" in sample_row:
                expanded.append(f"{f}_end")
        else:
            expanded.append(f)
    return expanded


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _db_cfg(db: dict, key: str, default=None):
    return db.get(key, default)


# ------------------------------------------------------------------ #
#  Per-database sync
# ------------------------------------------------------------------ #

def sync_database(client: NotionClient, db_cfg: dict, storage: Storage, full: bool, content_fetch_delay_ms: int = 0):
    db_id = db_cfg["id"]
    table = db_cfg["name"]
    include_cols: list[str] = _db_cfg(db_cfg, "include_columns", [])
    exclude_cols: list[str] = _db_cfg(db_cfg, "exclude_columns", [])
    include_content: bool = _db_cfg(db_cfg, "include_content", True)
    include_comments: bool = _db_cfg(db_cfg, "include_comments", True)
    track_changes: bool = _db_cfg(db_cfg, "track_changes", True)
    change_fields: list[str] = _db_cfg(db_cfg, "change_fields", [])
    exclude_change_fields: list[str] = _db_cfg(db_cfg, "exclude_change_fields", [])

    # Apply any declared column renames before syncing new data
    renames: dict[str, str] = _db_cfg(db_cfg, "column_renames", {})
    for old_name, new_name in renames.items():
        old_col = sanitize_col(old_name)
        new_col = sanitize_col(new_name)
        storage.apply_column_rename(table, old_col, new_col)

    logger.info(f"[{table}] Fetching schema for database {db_id}")
    try:
        db_schema = client.get_database(db_id)
    except Exception as e:
        logger.error(f"[{table}] Failed to fetch database schema: {e}")
        return

    now = datetime.now(timezone.utc).isoformat()

    storage.ensure_schema_table()
    storage.ensure_options_table()
    storage.ensure_backups_table()
    storage.ensure_meta_table()
    id_shadow_cols = _compare_schema(table, db_schema, storage, now)

    meta_key = f"last_sync:{table}"
    last_sync = None if full else storage.get_meta(meta_key)

    if last_sync:
        cutoff = (datetime.fromisoformat(last_sync) - timedelta(seconds=60)).isoformat()
        filter_payload = {
            "timestamp": "last_edited_time",
            "last_edited_time": {"on_or_after": cutoff},
        }
        logger.info(f"[{table}] Incremental sync from {cutoff}")
    else:
        filter_payload = None
        logger.info(f"[{table}] Full sync (no prior timestamp)")

    logger.info(f"[{table}] Querying pages ...")
    try:
        pages = client.query_database(db_id, filter_payload=filter_payload)
    except Exception as e:
        logger.error(f"[{table}] Failed to query database: {e}")
        return

    logger.info(f"[{table}] {len(pages)} page(s) found")

    if track_changes:
        storage.ensure_changes_table(table)
        storage.backfill_option_names(table)
    if include_comments:
        storage.ensure_comments_table(table)

    # Sanitize change_fields / exclude_change_fields to match stored col names
    change_fields_san = [sanitize_col(f) for f in change_fields]
    exclude_change_fields_san = list(
        set(sanitize_col(f) for f in exclude_change_fields) | id_shadow_cols
    )

    pages_synced = 0
    changes_recorded = 0
    comments_synced = 0
    _fields_expanded = False

    for page in pages:
        page_id = page["id"]

        # --- Build the page row ---
        row = extract_page_row(page, db_schema, include_cols, exclude_cols)

        if include_content:
            row["content_text"] = extract_content(client, page_id)
            if content_fetch_delay_ms > 0:
                time.sleep(content_fetch_delay_ms / 1000)

        # --- Ensure table schema covers all columns in this row ---
        storage.ensure_pages_table(table, row)

        # --- Expand change_fields for split date columns (once per database) ---
        if track_changes and not _fields_expanded:
            expanded_cf = _expand_change_fields(change_fields_san, row)
            expanded_ex = _expand_change_fields(exclude_change_fields_san, row)
            if expanded_cf != change_fields_san or expanded_ex != exclude_change_fields_san:
                logger.debug(f"[{table}] change_fields expanded: {change_fields_san} → {expanded_cf}")
            change_fields_san = expanded_cf
            exclude_change_fields_san = expanded_ex
            _fields_expanded = True

        # --- Change tracking ---
        if track_changes:
            prev_row = storage.get_page(table, page_id)
            changes = detect_changes(
                row,
                prev_row,
                change_fields_san,
                exclude_change_fields_san,
            )
            for ch in changes:
                field = ch["field"]
                storage.record_change(
                    table,
                    ch["page_id"],
                    field,
                    ch["old_value"],
                    ch["new_value"],
                    ch["valid_from"],
                    ch["detected_at"],
                    old_value_id=prev_row.get(f"{field}_id") if prev_row else None,
                    new_value_id=row.get(f"{field}_id"),
                )
            changes_recorded += len(changes)

        # --- Upsert page ---
        storage.upsert_page(table, row)
        pages_synced += 1

        # --- Comments ---
        if include_comments:
            comments = extract_comments(client, page_id)
            for comment in comments:
                storage.upsert_comment(table, comment)
            comments_synced += len(comments)

    storage.set_meta(meta_key, now)
    logger.info(
        f"[{table}] Done. "
        f"{pages_synced} pages synced, "
        f"{changes_recorded} changes recorded, "
        f"{comments_synced} comments synced."
    )


# ------------------------------------------------------------------ #
#  Main
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(description="Notion → Power BI sync")
    parser.add_argument("--config", default="config.toml", help="Path to config file")
    parser.add_argument("--full", action="store_true", help="Full refresh (fetch all pages)")
    args = parser.parse_args()

    cfg = load_config(args.config)

    token = cfg.get("token")
    if not token:
        logger.error("token is not set in config.toml")
        sys.exit(1)
    output_cfg = cfg.get("output", {})
    db_path = output_cfg.get("db_path", "notion_powerbi.db")
    export_csv = output_cfg.get("export_csv", False)
    csv_dir = output_cfg.get("csv_dir", "exports")
    content_fetch_delay_ms: int = output_cfg.get("content_fetch_delay_ms", 0)

    databases = cfg.get("databases", [])
    if not databases:
        logger.error("No databases configured in config.toml")
        sys.exit(1)

    client = NotionClient(token)
    storage = Storage(db_path)

    start = datetime.now(timezone.utc)
    logger.info(f"Sync started at {start.isoformat()}")
    logger.info(f"Output: {db_path}")

    for db_cfg in databases:
        try:
            sync_database(client, db_cfg, storage, full=args.full, content_fetch_delay_ms=content_fetch_delay_ms)
        except Exception as e:
            logger.error(f"Unexpected error syncing '{db_cfg.get('name')}': {e}", exc_info=True)

        if export_csv:
            storage.export_csv(db_cfg["name"], csv_dir)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    logger.info(f"Sync complete in {elapsed:.1f}s")

    storage.close()


if __name__ == "__main__":
    main()
