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
    int: "INTEGER",
    float: "REAL",
    str: "TEXT",
    type(None): "TEXT",
}

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
    valid_from      TEXT,
    detected_at     TEXT NOT NULL
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

    def record_change(
        self,
        table: str,
        page_id: str,
        field: str,
        old_value,
        new_value,
        valid_from: str,
        detected_at: str,
    ):
        self.conn.execute(
            f'INSERT INTO "{table}_changes" (page_id, field, old_value, new_value, valid_from, detected_at) '
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (
                page_id,
                field,
                str(old_value) if old_value is not None else None,
                str(new_value) if new_value is not None else None,
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

    def close(self):
        self.conn.close()
