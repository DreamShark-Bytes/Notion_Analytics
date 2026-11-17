# Troubleshooting

The first place to look is always `notion_analytics.log` in the project folder. The terminal only shows a summary.

## Sync errors

**`Failed to fetch database schema`**
Integration token is wrong, or the integration hasn't been connected to that database in Notion (`...` menu → Connect to).

**`No databases configured`**
`config.toml` is missing `[[databases]]` entries, or the wrong config file is being used.

**`Unexpected error syncing '<name>': ...`**
That one database failed; the other databases still ran. The failed database keeps its previous `last_sync` timestamp, so the next run retries the same window. The text after the colon is the cause; the log has the full traceback.

**`no such column: ""<name>""` when syncing a database**
Older versions crashed this way when a property listed in `exclude_columns` changed type in Notion (the backup step tried to rename a column that was never created). Update to the current version; excluded properties are now skipped.

**`Column '<name>' (Notion property '<Name>') no longer exists in the Notion schema`**
The property was deleted or renamed in Notion. The SQLite column and its history stay, and stop updating. If it was a rename, see [Handling Schema Changes](reference.md#handling-schema-changes).

**`Property '<Name>' changed type '<old>' → '<new>'`**
Expected after you change a property's type in Notion. The old values are kept in a `<column>_bak_YYYYMMDD` column. See [Changed property type](reference.md#changed-property-type).

**Windows says "part of this app has been blocked ... pip.exe"**
Windows Security blocks the small `pip.exe` launcher because it is unsigned. Run pip through Python instead, which is the same installer: `venv\Scripts\python.exe -m pip install -r requirements.txt`. Do not turn off Smart App Control to get around it; it cannot be switched back on without resetting Windows.

## Data looks wrong

**Columns missing in Power BI**
New Notion properties appear once a page carrying them is synced. If `include_columns` is set, or the property is listed in `exclude_columns`, it is deliberately not stored.

**Change history looks wrong after a rename**
Renames are followed automatically by property id and the history field names are updated with them. If a rename happened while the sync could not run, or the old and new columns both exist, add the old and new names to `column_renames` and run the sync once. See [Handling Schema Changes](reference.md#handling-schema-changes).

**A value did not update after a config change**
Incremental syncs skip pages that were not edited in Notion. Run `python sync.py --full` once. See [Guide](guide.md#incremental-sync-and-full-refresh).

**Rows for pages I deleted are still there**
Deleted pages are never removed from SQLite. Run `sync.py --detect-deleted` to mark them, and filter reports with `WHERE deleted_at IS NULL`. See [Deleted and Moved Pages](deleted-pages.md).

## Copies on another machine

**Power BI can't find the SQLite file**
Verify the file path in your ODBC DSN matches the actual `.db` file location. If the file is on a network share, confirm the drive is mapped before opening Power BI. Better, open a local copy made with the snapshot tool.

**The copy is stale, corrupt, or fails `PRAGMA integrity_check`**
A plain file copy of a live WAL database is unsafe. See [Backups and Database Copies](backup-and-snapshots.md), and set up the [Stale-Data Alert](stale-data-alert.md) so a copy that stops updating is noticed.

**The fetch says `IN USE` (exit code 3), or the copy never updates while a program is open**
Another program has the destination `.db` open, so Windows refuses to replace it. The old copy is kept and the next scheduled run retries, so the copy goes stale for as long as the program holds the file. Known culprits:
- **DBeaver** (or another DB browser) with a SQLite connection that is still connected. An idle connection still holds the file. Right-click the connection and choose **Disconnect**; you do not need to close the program.
- **Power BI Desktop** while it refreshes or has the file loaded through ODBC. Close it, or refresh and then close.
- A Python or other script that opened the file and was never closed.

To find the holder: open **Resource Monitor** (Windows search "resmon"), go to **CPU**, expand **Associated Handles**, and search for `notion_analytics.db`. To avoid the problem, point DBeaver and similar tools at a separate copy of the file, not at the fetch destination, and keep connections read-only. After releasing the file, run `python C:\Scripts\db_snapshot.py fetch ...` by hand; it prints `OK` when it installs the new copy. See [Backups and Database Copies](backup-and-snapshots.md) for the exit codes.
