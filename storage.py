"""
storage.py
SQLite persistence layer.

Tables created per configured database:
  {name}_pages    — current state, one row per Notion page
  {name}_changes  — field-level change history
  {name}_comments — page comments (optional)

Schema evolves automatically: new Notion columns trigger ALTER TABLE ADD COLUMN.
"""

import sqlite3
import csv
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# SQLite type for each Python type
_TYPE_MAP = {
    bool: "INTEGER",
    int: "INTEGER",
    float: "REAL",
    str: "TEXT",
    type(None): "TEXT",
}


def _val_to_str(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return "1" if val else "0"
    return str(val)

_SYSTEM_COLS = {
    "page_id": "TEXT PRIMARY KEY",
    "created_time": "TEXT",
    "last_edited_time": "TEXT",
    "url": "TEXT",
    "content_text": "TEXT",
}

_CHANGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS "{table}_changes" (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         TEXT NOT NULL,
    field           TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    old_value_id    TEXT,
    new_value_id    TEXT,
    valid_from      TEXT,
    detected_at     TEXT NOT NULL
)
"""

_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS "_schema" (
    table_name    TEXT NOT NULL,
    property_id   TEXT NOT NULL,
    property_name TEXT NOT NULL,
    column_name   TEXT NOT NULL,
    property_type TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    PRIMARY KEY (table_name, property_id)
)
"""

_OPTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS "_options" (
    table_name   TEXT NOT NULL,
    property_id  TEXT NOT NULL,
    option_id    TEXT NOT NULL,
    option_name  TEXT NOT NULL,
    option_group TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (table_name, property_id, option_id)
)
"""

_BACKUPS_TABLE = """
CREATE TABLE IF NOT EXISTS "_backups" (
    table_name    TEXT NOT NULL,
    column_name   TEXT NOT NULL,
    backup_column TEXT NOT NULL,
    type          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (table_name, column_name, type)
)
"""

_COMMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS "{table}_comments" (
    comment_id      TEXT PRIMARY KEY,
    page_id         TEXT NOT NULL,
    created_time    TEXT,
    last_edited_time TEXT,
    text            TEXT
)
"""


def _sqlite_type(value) -> str:
    return _TYPE_MAP.get(type(value), "TEXT")


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._known_cols: dict[str, set[str]] = {}  # table_name → set of col names

    # ------------------------------------------------------------------ #
    #  Pages table
    # ------------------------------------------------------------------ #

    def ensure_pages_table(self, table: str, sample_row: dict):
        """
        Create the pages table if it doesn't exist, then add any missing columns.
        `sample_row` is a representative row dict to infer column types.
        """
        if table not in self._known_cols:
            # Build initial CREATE TABLE with system columns
            col_defs = [f'"{col}" {defn}' for col, defn in _SYSTEM_COLS.items()]
            ddl = f'CREATE TABLE IF NOT EXISTS "{table}_pages" ({", ".join(col_defs)})'
            self.conn.execute(ddl)
            self.conn.commit()
            self._known_cols[table] = self._fetch_col_names(f"{table}_pages")

        existing = self._known_cols[table]
        for col, val in sample_row.items():
            if col in existing:
                continue
            sqlite_type = _sqlite_type(val)
            try:
                self.conn.execute(f'ALTER TABLE "{table}_pages" ADD COLUMN "{col}" {sqlite_type}')
                self.conn.commit()
                existing.add(col)
                logger.debug(f"Added column '{col}' ({sqlite_type}) to {table}_pages")
            except sqlite3.OperationalError as e:
                logger.warning(f"Could not add column '{col}': {e}")

    def _fetch_col_names(self, table_name: str) -> set[str]:
        cur = self.conn.execute(f'PRAGMA table_info("{table_name}")')
        return {row["name"] for row in cur.fetchall()}

    def upsert_page(self, table: str, row: dict):
        cols = [c for c in row if c in self._known_cols.get(table, set())]
        if not cols:
            return
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(f'"{c}"' for c in cols)
        values = [row[c] for c in cols]
        self.conn.execute(
            f'INSERT OR REPLACE INTO "{table}_pages" ({col_list}) VALUES ({placeholders})',
            values,
        )
        self.conn.commit()

    def get_page(self, table: str, page_id: str) -> dict | None:
        cur = self.conn.execute(
            f'SELECT * FROM "{table}_pages" WHERE page_id = ?', (page_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_all_page_ids(self, table: str) -> set[str]:
        try:
            cur = self.conn.execute(f'SELECT page_id FROM "{table}_pages"')
            return {row["page_id"] for row in cur.fetchall()}
        except sqlite3.OperationalError:
            return set()

    # ------------------------------------------------------------------ #
    #  Changes table
    # ------------------------------------------------------------------ #

    def ensure_changes_table(self, table: str):
        self.conn.execute(_CHANGES_SCHEMA.format(table=table))
        self.conn.commit()
        for col in ("old_value_id", "new_value_id"):
            try:
                self.conn.execute(f'ALTER TABLE "{table}_changes" ADD COLUMN "{col}" TEXT')
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    def ensure_schema_table(self):
        self.conn.execute(_SCHEMA_TABLE)
        self.conn.commit()

    def ensure_options_table(self):
        self.conn.execute(_OPTIONS_TABLE)
        self.conn.commit()

    def ensure_backups_table(self):
        self.conn.execute(_BACKUPS_TABLE)
        self.conn.commit()

    def record_change(
        self,
        table: str,
        page_id: str,
        field: str,
        old_value,
        new_value,
        valid_from: str,
        detected_at: str,
        old_value_id: str | None = None,
        new_value_id: str | None = None,
    ):
        self.conn.execute(
            f'INSERT INTO "{table}_changes" '
            f"(page_id, field, old_value, new_value, old_value_id, new_value_id, valid_from, detected_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                page_id,
                field,
                _val_to_str(old_value),
                _val_to_str(new_value),
                old_value_id,
                new_value_id,
                valid_from,
                detected_at,
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    #  Comments table
    # ------------------------------------------------------------------ #

    def ensure_comments_table(self, table: str):
        self.conn.execute(_COMMENTS_SCHEMA.format(table=table))
        self.conn.commit()

    def upsert_comment(self, table: str, comment: dict):
        self.conn.execute(
            f'INSERT OR REPLACE INTO "{table}_comments" '
            f"(comment_id, page_id, created_time, last_edited_time, text) "
            f"VALUES (?, ?, ?, ?, ?)",
            (
                comment["comment_id"],
                comment["page_id"],
                comment.get("created_time"),
                comment.get("last_edited_time"),
                comment.get("text"),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    #  CSV export
    # ------------------------------------------------------------------ #

    def export_csv(self, table: str, csv_dir: str):
        os.makedirs(csv_dir, exist_ok=True)
        for suffix in ("pages", "changes", "comments"):
            tname = f"{table}_{suffix}"
            path = Path(csv_dir) / f"{tname}.csv"
            try:
                cur = self.conn.execute(f'SELECT * FROM "{tname}"')
                rows = cur.fetchall()
                if not rows:
                    continue
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(rows[0].keys())
                    writer.writerows(rows)
                logger.info(f"Exported {len(rows)} rows → {path}")
            except sqlite3.OperationalError:
                pass  # table doesn't exist yet (e.g. comments disabled)

    # ------------------------------------------------------------------ #
    #  Column rename
    # ------------------------------------------------------------------ #

    def apply_column_rename(self, table: str, old_col: str, new_col: str) -> bool:
        """
        Handle a Notion property rename by copying data from old_col to new_col
        in the pages table, then updating the field name in the changes table.

        - If old_col doesn't exist, this is a no-op (rename already applied or
          the old column was never synced).
        - If new_col doesn't exist yet, it is created as TEXT before copying.
        - Returns True if data was migrated, False if nothing needed doing.
        """
        existing = self._fetch_col_names(f"{table}_pages")

        if old_col not in existing:
            return False  # nothing to migrate

        if new_col not in existing:
            try:
                self.conn.execute(
                    f'ALTER TABLE "{table}_pages" ADD COLUMN "{new_col}" TEXT'
                )
                self.conn.commit()
                existing.add(new_col)
                if table in self._known_cols:
                    self._known_cols[table].add(new_col)
            except sqlite3.OperationalError as e:
                logger.warning(f"Could not add column '{new_col}' for rename: {e}")
                return False

        # Copy old → new where new is still NULL
        self.conn.execute(
            f'UPDATE "{table}_pages" '
            f'SET "{new_col}" = "{old_col}" '
            f'WHERE "{new_col}" IS NULL AND "{old_col}" IS NOT NULL'
        )

        # Mirror the rename in the changes table so history stays coherent
        try:
            self.conn.execute(
                f'UPDATE "{table}_changes" SET field = ? WHERE field = ?',
                (new_col, old_col),
            )
        except sqlite3.OperationalError:
            pass  # changes table may not exist yet

        self.conn.commit()
        logger.info(f"[{table}] Column rename applied: '{old_col}' → '{new_col}'")
        return True

    # ------------------------------------------------------------------ #
    #  Schema tracking
    # ------------------------------------------------------------------ #

    def get_stored_schema(self, table: str) -> dict:
        cur = self.conn.execute(
            'SELECT property_id, property_name, column_name, property_type '
            'FROM "_schema" WHERE table_name = ?',
            (table,),
        )
        return {
            row[0]: {"property_name": row[1], "column_name": row[2], "property_type": row[3]}
            for row in cur.fetchall()
        }

    def upsert_schema(
        self,
        table: str,
        property_id: str,
        property_name: str,
        column_name: str,
        property_type: str,
        now: str,
    ):
        self.conn.execute(
            'INSERT INTO "_schema" '
            "(table_name, property_id, property_name, column_name, property_type, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(table_name, property_id) DO UPDATE SET "
            "property_name=excluded.property_name, column_name=excluded.column_name, "
            "property_type=excluded.property_type, last_seen=excluded.last_seen",
            (table, property_id, property_name, column_name, property_type, now, now),
        )
        self.conn.commit()

    def get_stored_options(self, table: str, property_id: str) -> dict:
        cur = self.conn.execute(
            'SELECT option_id, option_name, option_group FROM "_options" '
            "WHERE table_name = ? AND property_id = ?",
            (table, property_id),
        )
        return {
            row[0]: {"option_name": row[1], "option_group": row[2]}
            for row in cur.fetchall()
        }

    def upsert_option(
        self,
        table: str,
        property_id: str,
        option_id: str,
        option_name: str,
        option_group: str | None,
        now: str,
    ):
        self.conn.execute(
            'INSERT INTO "_options" '
            "(table_name, property_id, option_id, option_name, option_group, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(table_name, property_id, option_id) DO UPDATE SET "
            "option_name=excluded.option_name, option_group=excluded.option_group, last_seen=excluded.last_seen",
            (table, property_id, option_id, option_name, option_group, now, now),
        )
        self.conn.commit()

    def record_backup(self, table: str, column_name: str, backup_column: str, type: str, now: str):
        self.conn.execute(
            'INSERT OR REPLACE INTO "_backups" '
            "(table_name, column_name, backup_column, type, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (table, column_name, backup_column, type, now),
        )
        self.conn.commit()

    def get_backup_for_type(self, table: str, column_name: str, type: str) -> str | None:
        cur = self.conn.execute(
            'SELECT backup_column FROM "_backups" WHERE table_name = ? AND column_name = ? AND type = ?',
            (table, column_name, type),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def restore_backup(self, table: str, column_name: str, backup_column: str) -> bool:
        existing = self._fetch_col_names(f"{table}_pages")
        if backup_column not in existing:
            self.conn.execute(
                'DELETE FROM "_backups" WHERE table_name = ? AND backup_column = ?',
                (table, backup_column),
            )
            self.conn.commit()
            return False
        if column_name in existing:
            self.conn.execute(f'ALTER TABLE "{table}_pages" DROP COLUMN "{column_name}"')
            self.conn.commit()
        self.conn.execute(
            f'ALTER TABLE "{table}_pages" RENAME COLUMN "{backup_column}" TO "{column_name}"'
        )
        self.conn.commit()
        self.conn.execute(
            'DELETE FROM "_backups" WHERE table_name = ? AND backup_column = ?',
            (table, backup_column),
        )
        self.conn.commit()
        if table in self._known_cols:
            self._known_cols[table].discard(backup_column)
            self._known_cols[table].add(column_name)
        logger.info(f"[{table}] Restored '{backup_column}' → '{column_name}'")
        return True

    def backfill_option_names(self, table: str) -> int:
        try:
            cur = self.conn.execute(
                f'UPDATE "{table}_changes" '
                f'SET old_value = ('
                f'  SELECT option_name FROM "_options"'
                f'  WHERE option_id = old_value_id AND table_name = ? LIMIT 1'
                f') '
                f'WHERE old_value_id IS NOT NULL'
                f'  AND EXISTS ('
                f'    SELECT 1 FROM "_options"'
                f'    WHERE option_id = old_value_id AND table_name = ?'
                f'      AND option_name != old_value'
                f'  )',
                (table, table),
            )
            old_updates = cur.rowcount
            cur = self.conn.execute(
                f'UPDATE "{table}_changes" '
                f'SET new_value = ('
                f'  SELECT option_name FROM "_options"'
                f'  WHERE option_id = new_value_id AND table_name = ? LIMIT 1'
                f') '
                f'WHERE new_value_id IS NOT NULL'
                f'  AND EXISTS ('
                f'    SELECT 1 FROM "_options"'
                f'    WHERE option_id = new_value_id AND table_name = ?'
                f'      AND option_name != new_value'
                f'  )',
                (table, table),
            )
            new_updates = cur.rowcount
            self.conn.commit()
            total = old_updates + new_updates
            if total > 0:
                logger.info(f"[{table}] Backfilled {total} option name(s) in _changes.")
            return total
        except sqlite3.OperationalError:
            return 0

    def rename_column(self, table: str, old_col: str, new_col: str):
        """Atomically rename a column in _pages and update field names in _changes."""
        existing = self._fetch_col_names(f"{table}_pages")
        if old_col not in existing:
            return
        self.conn.execute(
            f'ALTER TABLE "{table}_pages" RENAME COLUMN "{old_col}" TO "{new_col}"'
        )
        self.conn.commit()
        try:
            self.conn.execute(
                f'UPDATE "{table}_changes" SET field = ? WHERE field = ?',
                (new_col, old_col),
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass
        if table in self._known_cols:
            self._known_cols[table].discard(old_col)
            self._known_cols[table].add(new_col)
        try:
            self.conn.execute(
                'UPDATE "_backups" SET column_name = ? WHERE table_name = ? AND column_name = ?',
                (new_col, table, old_col),
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass
        logger.info(f"[{table}] Column renamed: '{old_col}' → '{new_col}'")

    def backup_column(self, table: str, col: str, date_str: str) -> str:
        """Rename col to col_bak_YYYYMMDD in the pages table. Returns the backup name."""
        backup_name = f"{col}_bak_{date_str}"
        existing = self._fetch_col_names(f"{table}_pages")
        if backup_name in existing:
            i = 2
            while f"{backup_name}_{i}" in existing:
                i += 1
            backup_name = f"{backup_name}_{i}"
        self.conn.execute(
            f'ALTER TABLE "{table}_pages" RENAME COLUMN "{col}" TO "{backup_name}"'
        )
        self.conn.commit()
        if table in self._known_cols:
            self._known_cols[table].discard(col)
            self._known_cols[table].add(backup_name)
        return backup_name

    def close(self):
        self.conn.close()
