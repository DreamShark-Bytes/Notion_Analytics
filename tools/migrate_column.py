"""
tools/migrate_column.py
General-purpose column type migration for Notion Analytics SQLite databases.

Usage examples:
  # Dry run — see exact SQL before committing
  python tools/migrate_column.py --column done --new-type INTEGER --dry-run

  # Apply to all databases in config
  python tools/migrate_column.py --column done is_template --new-type INTEGER

  # Apply to one database only
  python tools/migrate_column.py --database tasks --column done --new-type INTEGER

  # Custom value map for _changes normalization
  python tools/migrate_column.py --column priority --new-type INTEGER --map "High=1" "Medium=2" "Low=3"

  # List available backups
  python tools/migrate_column.py --list-backups

  # Restore most recent backup
  python tools/migrate_column.py --restore

  # Restore specific backup
  python tools/migrate_column.py --restore notion_analytics.20260608_143000.bak
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("Python < 3.11: run 'pip install tomli' first.")

_AUTO_MAPS_TEXT_TO_INTEGER = [
    ("True", "1"),
    ("False", "0"),
    ("true", "1"),
    ("false", "0"),
]


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def backup_db(db_path: Path, dry_run: bool) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bak_path = db_path.with_name(f"{db_path.stem}.{ts}.bak")
    if dry_run:
        print(f"[dry-run] Would create backup: {bak_path}")
    else:
        shutil.copy2(db_path, bak_path)
        print(f"Backup created: {bak_path}")
    return bak_path


def list_backups(db_path: Path):
    baks = sorted(db_path.parent.glob(f"{db_path.stem}.*.bak"))
    if not baks:
        print("No backups found.")
    else:
        print("Available backups:")
        for b in baks:
            print(f"  {b}")


def restore_backup(db_path: Path, bak_path: Path | None):
    if bak_path is None:
        baks = sorted(db_path.parent.glob(f"{db_path.stem}.*.bak"))
        if not baks:
            sys.exit("No backups found to restore.")
        bak_path = baks[-1]
    if not bak_path.exists():
        sys.exit(f"Backup not found: {bak_path}")
    shutil.copy2(bak_path, db_path)
    for ext in (".db-wal", ".db-shm"):
        sidecar = db_path.with_suffix(ext)
        if sidecar.exists():
            sidecar.unlink()
            print(f"Removed sidecar: {sidecar.name}")
    print(f"Restored {db_path} from {bak_path}")


def build_col_expr(col: str, value_map: list[tuple[str, str]], new_type: str) -> str:
    if not value_map:
        return f'CAST("{col}" AS {new_type})'
    when_clauses = []
    for old, new in value_map:
        old_sql = repr(old)
        new_sql = repr(new) if new_type == "TEXT" else new
        when_clauses.append(f"WHEN {old_sql} THEN {new_sql}")
    return f'CASE "{col}" {" ".join(when_clauses)} ELSE CAST("{col}" AS {new_type}) END'


def migrate_pages_table(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    new_type: str,
    value_map: list[tuple[str, str]],
    dry_run: bool,
):
    pages_table = f"{table}_pages"
    cur = conn.execute(f'PRAGMA table_info("{pages_table}")')
    cols_info = cur.fetchall()
    if not cols_info:
        print(f"  [{table}] {pages_table} not found — skipping.")
        return

    col_names = [row[1] for row in cols_info]
    col_types = {row[1]: row[2] for row in cols_info}
    col_notnull = {row[1]: row[3] for row in cols_info}
    col_pk = {row[1]: row[5] for row in cols_info}

    migrating = [c for c in columns if c in col_names]
    missing = [c for c in columns if c not in col_names]
    if missing:
        print(f"  [{table}] Not found in {pages_table}: {', '.join(missing)}")
    if not migrating:
        print(f"  [{table}] No matching columns — skipping _pages rebuild.")
        return

    tmp = f"{pages_table}_migration_tmp"

    col_defs = []
    for col in col_names:
        t = new_type if col in migrating else col_types[col]
        nn = " NOT NULL" if col_notnull[col] else ""
        pk = " PRIMARY KEY" if col_pk[col] else ""
        col_defs.append(f'  "{col}" {t}{pk}{nn}')
    create_sql = f'CREATE TABLE "{tmp}" (\n' + ",\n".join(col_defs) + "\n)"

    select_parts = []
    for col in col_names:
        if col in migrating:
            select_parts.append(build_col_expr(col, value_map, new_type))
        else:
            select_parts.append(f'"{col}"')
    insert_sql = (
        f'INSERT INTO "{tmp}" SELECT\n  '
        + ",\n  ".join(select_parts)
        + f'\nFROM "{pages_table}"'
    )
    drop_sql = f'DROP TABLE "{pages_table}"'
    rename_sql = f'ALTER TABLE "{tmp}" RENAME TO "{pages_table}"'

    sqls = [create_sql, insert_sql, drop_sql, rename_sql]

    if dry_run:
        print(f"\n-- [{table}] _pages rebuild")
        for sql in sqls:
            print(sql + ";\n")
        return

    print(f"  [{table}] Rebuilding {pages_table} ...")
    conn.execute("BEGIN")
    try:
        for sql in sqls:
            conn.execute(sql)
        conn.execute("COMMIT")
        print(f"  [{table}] Done.")
    except Exception as e:
        conn.execute("ROLLBACK")
        sys.exit(f"  [{table}] ERROR — rolled back: {e}")


def migrate_changes_table(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    value_map: list[tuple[str, str]],
    dry_run: bool,
):
    if not value_map:
        return

    changes_table = f"{table}_changes"
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (changes_table,),
    ).fetchone()
    if not exists:
        print(f"  [{table}] No changes table — skipping _changes normalization.")
        return

    for col in columns:
        for old_str, new_str in value_map:
            for value_col in ("old_value", "new_value"):
                sql = (
                    f'UPDATE "{changes_table}" SET {value_col} = ? '
                    f"WHERE field = ? AND {value_col} = ?"
                )
                if dry_run:
                    print(
                        f'\n-- [{table}] _changes normalization\n'
                        f"UPDATE \"{changes_table}\" SET {value_col} = '{new_str}' "
                        f"WHERE field = '{col}' AND {value_col} = '{old_str}';"
                    )
                else:
                    cur = conn.execute(sql, (new_str, col, old_str))
                    if cur.rowcount:
                        print(
                            f"  [{table}] {changes_table}.{value_col}: "
                            f"'{old_str}'→'{new_str}' for field='{col}' ({cur.rowcount} rows)"
                        )
    if not dry_run:
        conn.commit()


def main():
    parser = argparse.ArgumentParser(
        description="Migrate column types in Notion Analytics SQLite databases."
    )
    parser.add_argument("--database", help="Database name from config (omit for all)")
    parser.add_argument("--column", nargs="+", help="Column name(s) to migrate")
    parser.add_argument("--new-type", choices=["INTEGER", "REAL", "TEXT"], help="Target SQLite type")
    parser.add_argument("--map", nargs="+", metavar="OLD=NEW", help="Value maps for _changes (e.g. 'True=1' 'False=0')")
    parser.add_argument("--skip-pages", action="store_true", help="Skip _pages table rebuild")
    parser.add_argument("--skip-changes", action="store_true", help="Skip _changes normalization")
    parser.add_argument("--dry-run", action="store_true", help="Print SQL without executing")
    parser.add_argument("--restore", nargs="?", const="__latest__", metavar="PATH", help="Restore from backup")
    parser.add_argument("--list-backups", action="store_true", help="List available backups")
    parser.add_argument("--config", default="config.toml", help="Path to config file")
    args = parser.parse_args()

    cfg = load_config(args.config)
    db_path = Path(cfg.get("output", {}).get("db_path", "notion_analytics.db"))
    databases = cfg.get("databases", [])

    if args.list_backups:
        list_backups(db_path)
        return

    if args.restore is not None:
        bak = None if args.restore == "__latest__" else Path(args.restore)
        restore_backup(db_path, bak)
        return

    if not args.column or not args.new_type:
        parser.error("--column and --new-type are required.")

    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")

    value_map: list[tuple[str, str]] = []
    if args.map:
        for entry in args.map:
            if "=" not in entry:
                sys.exit(f"Invalid --map entry (expected OLD=NEW): {entry}")
            old, new = entry.split("=", 1)
            value_map.append((old, new))
    elif args.new_type == "INTEGER":
        value_map = _AUTO_MAPS_TEXT_TO_INTEGER

    target_dbs = databases
    if args.database:
        target_dbs = [db for db in databases if db["name"] == args.database]
        if not target_dbs:
            sys.exit(f"Database '{args.database}' not found in config.")

    backup_db(db_path, dry_run=args.dry_run)

    conn = sqlite3.connect(db_path)
    conn.isolation_level = None

    for db_cfg in target_dbs:
        table = db_cfg["name"]
        print(f"\n[{table}]")
        if not args.skip_pages:
            migrate_pages_table(conn, table, args.column, args.new_type, value_map, args.dry_run)
        if not args.skip_changes:
            migrate_changes_table(conn, table, args.column, value_map, args.dry_run)

    conn.close()

    if args.dry_run:
        print("\n[dry-run complete — no changes made]")
    else:
        print("\nMigration complete.")


if __name__ == "__main__":
    main()
