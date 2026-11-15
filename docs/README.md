# Documentation

Support documentation for running and maintaining Notion Analytics. The [project README](../README.md) says what the project is and what it does.

| Page | What it covers |
|---|---|
| [Setup](setup.md) | From a fresh clone to a verified sync: install, Notion integration, `config.toml` options, first run, verification |
| [Scheduling](scheduling.md) | Hourly sync with Task Scheduler, cron or systemd |
| [Guide](guide.md) | Incremental vs full sync, change tracking, content sync, usage tips |
| [Reference](reference.md) | Tables and columns, property types, handling renamed, deleted and retyped properties, resetting the database |
| [Deleted and Moved Pages](deleted-pages.md) | `--detect-deleted`, soft-delete columns and states |
| [Backups and Database Copies](backup-and-snapshots.md) | Why plain copies corrupt the database; `tools/db_snapshot.py`; backups; restoring |
| [Stale-Data Alert](stale-data-alert.md) | Windows toast when a copy of the database stops updating |
| [Connecting Power BI and Grafana](connecting-bi-tools.md) | ODBC, CSV, Grafana plugin, remote access |
| [Updating](updating.md) | Safe update procedure |
| [Troubleshooting](troubleshooting.md) | Common errors and what they mean |
