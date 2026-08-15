#!/usr/bin/env python3
"""
export_parquet.py
Exports all mart-layer dbt models from DuckDB to Parquet files.

Environment variables:
    DUCKDB_PATH   — path to the DuckDB file (default: ~/data/analytics.duckdb)
    PARQUET_DIR   — output directory for Parquet files (default: ~/data/parquet)
    DUCKDB_SCHEMA — schema containing mart tables (default: main_marts)

Called by the Airflow DAG after dbt run + dbt test succeed.
Power BI Desktop reads these Parquet files directly (no ODBC driver needed).

Usage:
    python dbt/scripts/export_parquet.py
    PARQUET_DIR=/mnt/share/parquet python dbt/scripts/export_parquet.py
"""

import os
import sys
import logging
import duckdb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

DUCKDB_PATH   = os.environ.get("DUCKDB_PATH",   os.path.expanduser("~/data/analytics.duckdb"))
PARQUET_DIR   = os.environ.get("PARQUET_DIR",   os.path.expanduser("~/data/parquet"))
DUCKDB_SCHEMA = os.environ.get("DUCKDB_SCHEMA", "main_marts")


def list_mart_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Return all table names in the marts schema."""
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
        [DUCKDB_SCHEMA],
    ).fetchall()
    return [r[0] for r in rows]


def export_table(con: duckdb.DuckDBPyConnection, schema: str, table: str, out_dir: str) -> bool:
    out_path = os.path.join(out_dir, f"{table}.parquet")
    try:
        con.execute(
            f"COPY (SELECT * FROM {schema}.{table}) TO ? (FORMAT PARQUET)",
            [out_path],
        )
        log.info(f"  {table} → {out_path}")
        return True
    except Exception as e:
        log.warning(f"  {table}: skipped — {e}")
        return False


def main() -> int:
    if not os.path.exists(DUCKDB_PATH):
        log.error(f"DuckDB file not found: {DUCKDB_PATH}")
        log.error("Run 'dbt run' first to build the mart tables.")
        return 1

    os.makedirs(PARQUET_DIR, exist_ok=True)

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    tables = list_mart_tables(con)

    if not tables:
        log.warning(f"No tables found in schema '{DUCKDB_SCHEMA}'. Have mart models been built?")
        con.close()
        return 1

    log.info(f"Exporting {len(tables)} mart table(s) from '{DUCKDB_SCHEMA}' → {PARQUET_DIR}")
    ok = sum(export_table(con, DUCKDB_SCHEMA, t, PARQUET_DIR) for t in tables)
    con.close()

    log.info(f"Done — {ok}/{len(tables)} tables exported.")
    return 0 if ok == len(tables) else 1


if __name__ == "__main__":
    sys.exit(main())
