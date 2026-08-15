"""
notion_analytics_dag.py
Hourly pipeline: Notion sync → dbt run → dbt test → Parquet export → rsync to ThinkPad.

Environment variables (set on the pipeline Pi, or via Airflow Admin → Variables):
    NOTION_ANALYTICS_DIR    — repo root on the Pi
                              e.g. /home/pi/projects/Notion_Analytics
    DUCKDB_PATH             — path to the DuckDB analytical database
                              e.g. /home/pi/data/analytics.duckdb
    NOTION_DB_PATH          — path to the SQLite file written by sync.py
                              e.g. /home/pi/data/notion_analytics.db
    PARQUET_DIR             — local directory for exported Parquet files
                              e.g. /home/pi/data/parquet
    THINKPAD_HOST           — Tailscale hostname (or IP) of the ThinkPad
                              e.g. thinkpad  (resolves via Tailscale MagicDNS)
    THINKPAD_PARQUET_DIR    — destination path on the ThinkPad for rsync.
                              Windows OpenSSH uses Windows-style paths:
                              e.g. /C:/Users/Vince/Projects/Notion/Parquet
                              (note leading /C: required by OpenSSH on Windows)

Pipeline order:
    notion_sync → dbt_run → dbt_test → export_parquet → push_to_thinkpad
"""

import os
from datetime import datetime
from airflow import DAG
from airflow.operators.bash import BashOperator

REPO_DIR          = os.environ.get("NOTION_ANALYTICS_DIR",  "/home/pi/projects/Notion_Analytics")
DUCKDB_PATH       = os.environ.get("DUCKDB_PATH",           "/home/pi/data/analytics.duckdb")
NOTION_DB_PATH    = os.environ.get("NOTION_DB_PATH",        "/home/pi/data/notion_analytics.db")
PARQUET_DIR       = os.environ.get("PARQUET_DIR",           "/home/pi/data/parquet")
THINKPAD_HOST     = os.environ.get("THINKPAD_HOST",         "thinkpad")
THINKPAD_DIR      = os.environ.get("THINKPAD_PARQUET_DIR",  "/C:/Users/Vince/Projects/Notion/Parquet")

DBT_DIR = f"{REPO_DIR}/dbt"

with DAG(
    dag_id="notion_analytics",
    description="Sync Notion → SQLite → dbt/DuckDB → Parquet → ThinkPad",
    schedule_interval="@hourly",
    start_date=datetime(2026, 8, 14),
    catchup=False,
    max_active_runs=1,   # prevent overlap if a run takes longer than an hour
    tags=["notion", "analytics"],
) as dag:

    # 1. Pull latest data from Notion into SQLite
    notion_sync = BashOperator(
        task_id="notion_sync",
        bash_command=f"cd {REPO_DIR} && python sync.py",
        env={
            "NOTION_DB_PATH": NOTION_DB_PATH,
            **os.environ,
        },
    )

    # 2. Run dbt transformations (staging → intermediate → marts)
    dbt_run = BashOperator(
        task_id="dbt_run",
        bash_command=f"cd {DBT_DIR} && dbt run --profiles-dir .",
        env={
            "DUCKDB_PATH":    DUCKDB_PATH,
            "NOTION_DB_PATH": NOTION_DB_PATH,
            **os.environ,
        },
    )

    # 3. Run dbt schema and data quality tests
    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=f"cd {DBT_DIR} && dbt test --profiles-dir .",
        env={
            "DUCKDB_PATH":    DUCKDB_PATH,
            "NOTION_DB_PATH": NOTION_DB_PATH,
            **os.environ,
        },
    )

    # 4. Export mart tables to Parquet files
    export_parquet = BashOperator(
        task_id="export_parquet",
        bash_command=f"python {DBT_DIR}/scripts/export_parquet.py",
        env={
            "DUCKDB_PATH": DUCKDB_PATH,
            "PARQUET_DIR": PARQUET_DIR,
            **os.environ,
        },
    )

    # 5. Push Parquet files to ThinkPad via rsync over Tailscale SSH
    # --delete removes files on the ThinkPad that no longer exist locally
    # (e.g. if a mart model is renamed or dropped)
    push_to_thinkpad = BashOperator(
        task_id="push_to_thinkpad",
        bash_command=(
            f'rsync -avz --delete '
            f'{PARQUET_DIR}/ '
            f'{THINKPAD_HOST}:"{THINKPAD_DIR}/"'
        ),
    )

    notion_sync >> dbt_run >> dbt_test >> export_parquet >> push_to_thinkpad
