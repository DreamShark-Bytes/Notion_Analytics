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
import sys
from datetime import datetime, timezone

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("Python < 3.11 detected: install tomli with 'pip install tomli'")

from notion_api import NotionClient
from extractor import extract_page_row, extract_comments, extract_content, sanitize_col
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

def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _db_cfg(db: dict, key: str, default=None):
    return db.get(key, default)


# ------------------------------------------------------------------ #
#  Per-database sync
# ------------------------------------------------------------------ #

def sync_database(client: NotionClient, db_cfg: dict, storage: Storage, full: bool):
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

    logger.info(f"[{table}] Querying pages ...")
    try:
        pages = client.query_database(db_id)
    except Exception as e:
        logger.error(f"[{table}] Failed to query database: {e}")
        return

    logger.info(f"[{table}] {len(pages)} page(s) found")

    if track_changes:
        storage.ensure_changes_table(table)
    if include_comments:
        storage.ensure_comments_table(table)

    # Sanitize change_fields / exclude_change_fields to match stored col names
    change_fields_san = [sanitize_col(f) for f in change_fields]
    exclude_change_fields_san = [sanitize_col(f) for f in exclude_change_fields]

    pages_synced = 0
    changes_recorded = 0
    comments_synced = 0

    for page in pages:
        page_id = page["id"]

        # --- Build the page row ---
        row = extract_page_row(page, db_schema, include_cols, exclude_cols)

        if include_content:
            row["content_text"] = extract_content(client, page_id)

        # --- Ensure table schema covers all columns in this row ---
        storage.ensure_pages_table(table, row)

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
                storage.record_change(
                    table,
                    ch["page_id"],
                    ch["field"],
                    ch["old_value"],
                    ch["new_value"],
                    ch["valid_from"],
                    ch["detected_at"],
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
            sync_database(client, db_cfg, storage, full=args.full)
        except Exception as e:
            logger.error(f"Unexpected error syncing '{db_cfg.get('name')}': {e}", exc_info=True)

        if export_csv:
            storage.export_csv(db_cfg["name"], csv_dir)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    logger.info(f"Sync complete in {elapsed:.1f}s")

    storage.close()


if __name__ == "__main__":
    main()
