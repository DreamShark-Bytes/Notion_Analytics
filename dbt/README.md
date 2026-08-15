# Notion Analytics — dbt + DuckDB Pipeline

Transforms the raw SQLite data written by `sync.py` into typed, Power BI-ready
Parquet files via dbt and DuckDB.

## Architecture

```
sync.py → notion_analytics.db (SQLite, raw)
                ↓
          DuckDB attaches SQLite as read-only source
                ↓
          dbt staging models (stg_*)    — type-cast and normalize
                ↓
          dbt intermediate models (int_*) — joins and aggregations
                ↓
          dbt mart models (fct_*, dim_*)  — Power BI-ready fact/dim tables
                ↓
          export_parquet.py → *.parquet
                ↓
          rsync over Tailscale → ThinkPad → Power BI Desktop
```

The SQLite database is **never modified** by this pipeline. dbt only reads from it.

## Prerequisites

```
pip install -r pipeline_requirements.txt
```

Airflow is installed separately with its constraints file — see
https://airflow.apache.org/docs/apache-airflow/stable/start.html

## Environment variables

Set these on the pipeline Pi (or export in your shell before running dbt):

| Variable | Description | Example |
|---|---|---|
| `NOTION_DB_PATH` | Path to the SQLite file from sync.py | `/home/pi/data/notion_analytics.db` |
| `DUCKDB_PATH` | Path for the DuckDB analytical database | `/home/pi/data/analytics.duckdb` |
| `PARQUET_DIR` | Output directory for Parquet export | `/home/pi/data/parquet` |
| `THINKPAD_HOST` | ThinkPad Tailscale hostname | `thinkpad` |
| `THINKPAD_PARQUET_DIR` | Parquet destination on ThinkPad | `/C:/Users/Vince/Projects/Notion/Parquet` |

## Running dbt manually

```bash
# From the dbt/ directory
cd dbt

# Check connection (reads SQLite, creates/opens DuckDB)
dbt debug --profiles-dir .

# Run all models
dbt run --profiles-dir .

# Run only staging models
dbt run --profiles-dir . --select staging

# Run tests
dbt test --profiles-dir .

# Generate and serve docs
dbt docs generate --profiles-dir . && dbt docs serve
```

## Model layers

| Layer | Folder | Materialization | Purpose |
|---|---|---|---|
| Staging | `models/staging/` | View | Type-cast and normalize raw columns |
| Intermediate | `models/intermediate/` | Table | Joins, pivots, aggregations |
| Marts | `models/marts/` | Table | Final fact/dim tables; exported to Parquet |

### Staging models (built)

| Model | Source table | Key transformations |
|---|---|---|
| `stg_tasks` | `tasks_pages` | Cast `created_time`, `last_edited_time`, `due_date_*`, `closed_date_*` to proper types |
| `stg_task_changes` | `tasks_changes` | Cast `valid_from`, `detected_at` to timestamptz |
| `stg_job_applications` | `job_applications_pages` | Cast date fields; cast `resume`/`cover_letter` to BOOLEAN |
| `stg_job_application_changes` | `job_applications_changes` | Cast timestamps |
| `stg_pursuits` | `pursuits_pages` | Cast system timestamps |

### Intermediate and mart models (planned)

See `models/intermediate/_schema.yml` and `models/marts/_schema.yml` for
the full list with descriptions.

## Exporting Parquet manually

```bash
DUCKDB_PATH=/home/pi/data/analytics.duckdb \
PARQUET_DIR=/home/pi/data/parquet \
python dbt/scripts/export_parquet.py
```

## Airflow

The DAG lives at `airflow/dags/notion_analytics_dag.py`. Copy or symlink it
into Airflow's `dags/` directory on the pipeline Pi.

Pipeline order: `notion_sync → dbt_run → dbt_test → export_parquet → push_to_thinkpad`

## Date casting — why try_cast(x as timestamptz)::date

Notion returns dates in two formats depending on whether the user set a time:
- Plain date: `"2026-08-04"`
- Datetime with offset: `"2026-08-04T14:00:00.000-05:00"`

Power Query's `type date` fails on the datetime-with-offset form. The staging
models cast via `timestamptz` first (which handles both), then truncate to `date`.
This means all `_start` and `_end` date columns arrive in Power BI as native
date types with no per-column Power Query workarounds needed.
