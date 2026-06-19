"""
tools/split_date_columns.py
Split Notion date columns into separate _start and _end columns.

Usage examples:
  # Dry run — see exact SQL before committing
  python tools/split_date_columns.py --column task_due_date --dry-run

  # Auto-detect and split all date columns across all databases
  python tools/split_date_columns.py --all-dates

  # Auto-detect, excluding formula fields that return date strings (not native date properties),
  # and explicitly add any NULL-value columns the detector would miss
  python tools/split_date_columns.py --all-dates --exclude period_key_recurring_task due_date_sort --column closed_date

  # Apply to all databases in config (explicit columns)
  python tools/split_date_columns.py --column task_due_date due_date

  # Apply to one database only
  python tools/split_date_columns.py --database tasks --column task_due_date

  # List available backups
  python tools/split_date_columns.py --list-backups

  # Restore most recent backup
  python tools/split_date_columns.py --restore

  # Restore specific backup
  python tools/split_date_columns.py --restore notion_analytics.20260609_120000.bak
"""

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}')
_SKIP_AUTO_DETECT = {'page_id', 'created_time', 'last_edited_time', 'url', 'content_text'}

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("Python < 3.11: run 'pip install tomli' first.")


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


def auto_detect_date_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return TEXT columns in {table}_pages whose values look like Notion ISO dates."""
    pages_table = f"{table}_pages"
    cur = conn.execute(f'PRAGMA table_info("{pages_table}")')
    schema_rows = cur.fetchall()
    if not schema_rows:
        return []
    date_cols = []
    for row in schema_rows:
        col_name, col_type = row[1], row[2]
        if col_type != 'TEXT':
            continue
        if col_name in _SKIP_AUTO_DETECT:
            continue
        if col_name.endswith('_start') or col_name.endswith('_end'):
            continue
        sample = conn.execute(
            f'SELECT "{col_name}" FROM "{pages_table}" '
            f'WHERE "{col_name}" IS NOT NULL AND "{col_name}" != \'\' LIMIT 1'
        ).fetchone()
        if sample and sample[0] and _DATE_PATTERN.match(str(sample[0])):
            date_cols.append(col_name)
    return date_cols


def _split_start(val: str | None) -> str | None:
    if val is None:
        return None
    return val.split("/", 1)[0]


def _split_end(val: str | None) -> str | None:
    if val is None:
        return None
    parts = val.split("/", 1)
    return parts[1] if len(parts) > 1 else None


def split_pages_table(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    dry_run: bool,
):
    pages_table = f"{table}_pages"
    cur = conn.execute(f'PRAGMA table_info("{pages_table}")')
    existing = {row[1] for row in cur.fetchall()}

    if not existing:
        print(f"  [{table}] {pages_table} not found — skipping.")
        return

    for col in columns:
        if col not in existing:
            print(f"  [{table}] '{col}' not found in {pages_table} — skipping.")
            continue

        start_col = f"{col}_start"
        end_col = f"{col}_end"

        sqls = []
        if start_col not in existing:
            sqls.append(f'ALTER TABLE "{pages_table}" ADD COLUMN "{start_col}" TEXT')
        if end_col not in existing:
            sqls.append(f'ALTER TABLE "{pages_table}" ADD COLUMN "{end_col}" TEXT')

        sqls.append(
            f'UPDATE "{pages_table}" SET '
            f'"{start_col}" = CASE WHEN "{col}" LIKE \'%/%\' '
            f'THEN substr("{col}", 1, instr("{col}", \'/\') - 1) '
            f'ELSE "{col}" END, '
            f'"{end_col}" = CASE WHEN "{col}" LIKE \'%/%\' '
            f'THEN substr("{col}", instr("{col}", \'/\') + 1) '
            f'ELSE NULL END'
        )
        sqls.append(f'ALTER TABLE "{pages_table}" DROP COLUMN "{col}"')

        if dry_run:
            print(f"\n-- [{table}] split '{col}' → '{start_col}', '{end_col}'")
            for sql in sqls:
                print(sql + ";")
        else:
            print(f"  [{table}] Splitting '{col}' in {pages_table} ...")
            conn.execute("BEGIN")
            try:
                for sql in sqls:
                    conn.execute(sql)
                conn.execute("COMMIT")
                print(f"  [{table}] Done.")
            except Exception as e:
                conn.execute("ROLLBACK")
                sys.exit(f"  [{table}] ERROR — rolled back: {e}")


def split_changes_table(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    dry_run: bool,
):
    changes_table = f"{table}_changes"
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (changes_table,),
    ).fetchone()
    if not exists:
        print(f"  [{table}] No changes table — skipping.")
        return

    for col in columns:
        rows = conn.execute(
            f'SELECT id, page_id, old_value, new_value, valid_from, detected_at '
            f'FROM "{changes_table}" WHERE field = ?',
            (col,),
        ).fetchall()

        if not rows:
            print(f"  [{table}] No _changes rows for '{col}' — skipping.")
            continue

        if dry_run:
            print(
                f"\n-- [{table}] split '{col}' in {changes_table} ({len(rows)} rows)\n"
                f"-- Would insert {len(rows) * 2} rows (_{col}_start, _{col}_end)\n"
                f"-- Would delete {len(rows)} original rows"
            )
            continue

        print(f"  [{table}] Migrating {len(rows)} _changes rows for '{col}' ...")
        conn.execute("BEGIN")
        try:
            for row in rows:
                row_id, page_id, old_val, new_val, valid_from, detected_at = row
                for suffix, o, n in [
                    ("_start", _split_start(old_val), _split_start(new_val)),
                    ("_end", _split_end(old_val), _split_end(new_val)),
                ]:
                    conn.execute(
                        f'INSERT INTO "{changes_table}" '
                        f"(page_id, field, old_value, new_value, valid_from, detected_at) "
                        f"VALUES (?, ?, ?, ?, ?, ?)",
                        (page_id, col + suffix, o, n, valid_from, detected_at),
                    )
                conn.execute(f'DELETE FROM "{changes_table}" WHERE id = ?', (row_id,))
            conn.execute("COMMIT")
            print(f"  [{table}] Done.")
        except Exception as e:
            conn.execute("ROLLBACK")
            sys.exit(f"  [{table}] ERROR — rolled back: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Split Notion date columns into _start and _end columns."
    )
    parser.add_argument("--database", help="Database name from config (omit for all)")
    parser.add_argument("--column", nargs="+", metavar="COL",
                        help="Column name(s) to split. Combined with --all-dates, these are added to the auto-detected set.")
    parser.add_argument("--all-dates", action="store_true",
                        help="Auto-detect and split all Notion date columns")
    parser.add_argument("--exclude", nargs="+", metavar="COL", default=[],
                        help="Column name(s) to skip when using --all-dates (e.g. formula fields that return dates)")
    parser.add_argument("--dry-run", action="store_true", help="Print SQL without executing")
    parser.add_argument("--restore", nargs="?", const="__latest__", metavar="PATH")
    parser.add_argument("--list-backups", action="store_true")
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

    if not args.column and not args.all_dates:
        parser.error("--column or --all-dates is required (they can be combined).")

    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")

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
        if args.all_dates:
            detected = auto_detect_date_columns(conn, table)
            if args.exclude:
                detected = [c for c in detected if c not in args.exclude]
            explicit = args.column or []
            columns = list(dict.fromkeys(detected + [c for c in explicit if c not in detected]))
            if not columns:
                print(f"  [{table}] No date columns detected — skipping.")
                continue
            print(f"  [{table}] Detected date columns: {', '.join(columns)}")
        else:
            columns = args.column
        split_pages_table(conn, table, columns, dry_run=args.dry_run)
        split_changes_table(conn, table, columns, dry_run=args.dry_run)

    conn.close()

    if args.dry_run:
        print("\n[dry-run complete — no changes made]")
    else:
        print("\nMigration complete.")


if __name__ == "__main__":
    main()
