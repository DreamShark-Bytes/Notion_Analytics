"""
tools/cleanup_orphaned_columns.py
Interactive tool to remove orphaned columns from _pages and _changes tables.

A column is orphaned when it exists in a _pages table but has no matching entry
in _schema (i.e. the Notion property was deleted or renamed without the program
tracking it). This includes type-change backup columns (_bak_YYYYMMDD).

Usage:
  # List all orphaned columns across all databases
  python tools/cleanup_orphaned_columns.py

  # Limit to one database
  python tools/cleanup_orphaned_columns.py --table tasks

  # Preview without making changes
  python tools/cleanup_orphaned_columns.py --dry-run
"""

import argparse
import sqlite3
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("Python < 3.11: run 'pip install tomli' first.")

_SYSTEM_COLS = {
    "page_id", "created_time", "last_edited_time", "url", "content_text",
}


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def find_orphans(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return column names in {table}_pages that have no _schema entry."""
    pages_table = f"{table}_pages"
    cur = conn.execute(f'PRAGMA table_info("{pages_table}")')
    all_cols = {row[1] for row in cur.fetchall()}
    if not all_cols:
        return []

    cur = conn.execute(
        'SELECT column_name FROM "_schema" WHERE table_name = ?', (table,)
    )
    schema_cols = {row[0] for row in cur.fetchall()}

    backups_exist = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_backups'"
    ).fetchone()
    tracked_backups: set[str] = set()
    if backups_exist:
        cur = conn.execute('SELECT backup_column FROM "_backups" WHERE table_name = ?', (table,))
        tracked_backups = {row[0] for row in cur.fetchall()}

    # Also exclude _id shadow columns (they're derived, not in _schema directly)
    orphans = []
    for col in sorted(all_cols):
        if col in _SYSTEM_COLS:
            continue
        if col in schema_cols:
            continue
        if col in tracked_backups:
            continue
        # Skip _id shadow columns — they're valid companions to select/status cols
        base = col[:-3] if col.endswith("_id") else None
        if base and base in schema_cols:
            continue
        # Skip _start/_end split columns
        for suffix in ("_start", "_end"):
            if col.endswith(suffix) and col[: -len(suffix)] in schema_cols:
                break
        else:
            orphans.append(col)
    return orphans


def non_null_count(conn: sqlite3.Connection, table: str, col: str) -> int:
    row = conn.execute(
        f'SELECT COUNT(*) FROM "{table}_pages" WHERE "{col}" IS NOT NULL'
    ).fetchone()
    return row[0] if row else 0


def changes_count(conn: sqlite3.Connection, table: str, col: str) -> int:
    try:
        row = conn.execute(
            f'SELECT COUNT(*) FROM "{table}_changes" WHERE field = ?', (col,)
        ).fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def drop_orphan(conn: sqlite3.Connection, table: str, col: str, dry_run: bool):
    pages_table = f"{table}_pages"
    changes_table = f"{table}_changes"

    if dry_run:
        print(f"    [dry-run] Would DROP COLUMN \"{col}\" from {pages_table}")
        print(f"    [dry-run] Would DELETE FROM {changes_table} WHERE field = '{col}'")
        print(f"    [dry-run] Would DELETE FROM _schema WHERE table_name='{table}' AND column_name='{col}'")
        return

    conn.execute("BEGIN")
    try:
        conn.execute(f'ALTER TABLE "{pages_table}" DROP COLUMN "{col}"')
        try:
            conn.execute(
                f'DELETE FROM "{changes_table}" WHERE field = ?', (col,)
            )
        except sqlite3.OperationalError:
            pass  # no changes table
        conn.execute(
            'DELETE FROM "_schema" WHERE table_name = ? AND column_name = ?',
            (table, col),
        )
        conn.execute("COMMIT")
        print(f"    Dropped '{col}' from {pages_table} and cleared related records.")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"    ERROR — rolled back: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Remove orphaned columns from Notion Analytics SQLite tables."
    )
    parser.add_argument("--table", help="Limit to one database name from config")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    db_path = Path(cfg.get("output", {}).get("db_path", "notion_analytics.db"))
    databases = cfg.get("databases", [])

    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")

    target_dbs = databases
    if args.table:
        target_dbs = [db for db in databases if db["name"] == args.table]
        if not target_dbs:
            sys.exit(f"Database '{args.table}' not found in config.")

    conn = sqlite3.connect(db_path)
    conn.isolation_level = None

    # Check _schema table exists (sync must have run at least once with schema tracking)
    schema_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_schema'"
    ).fetchone()
    if not schema_exists:
        sys.exit(
            "The _schema table does not exist yet. Run sync.py at least once first "
            "to populate schema tracking data."
        )

    found_any = False
    for db_cfg in target_dbs:
        table = db_cfg["name"]
        orphans = find_orphans(conn, table)
        if not orphans:
            continue

        found_any = True
        print(f"\n[{table}] — {len(orphans)} orphaned column(s) found:")

        for col in orphans:
            nn = non_null_count(conn, table, col)
            ch = changes_count(conn, table, col)
            print(f"\n  Column: {col}")
            print(f"    Non-NULL rows in {table}_pages: {nn}")
            print(f"    Rows in {table}_changes referencing this field: {ch}")

            if args.dry_run:
                drop_orphan(conn, table, col, dry_run=True)
                continue

            answer = input(f"    Drop '{col}' and remove all related records? [y/N] ").strip().lower()
            if answer == "y":
                drop_orphan(conn, table, col, dry_run=False)
            else:
                print(f"    Skipped '{col}'.")

    if not found_any:
        print("No orphaned columns found.")

    conn.close()

    if args.dry_run:
        print("\n[dry-run complete — no changes made]")


if __name__ == "__main__":
    main()
