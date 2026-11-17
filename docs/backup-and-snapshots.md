# Backups and Database Copies

The database is a SQLite file in WAL mode. That one fact decides how it must be copied, backed up and restored. This page covers the safe ways, the tool that does it (`tools/db_snapshot.py`), and how to restore.

The database holds the text of your Notion pages (titles, properties and, if `include_content` is on, page bodies). Treat every snapshot and backup as private data: keep them out of the repository, public shares and cloud folders you do not control.

## Why a plain file copy is unsafe

While `sync.py` runs, SQLite writes recent changes to a `notion_analytics.db-wal` file beside the database and keeps a `-shm` index file. The `.db` file alone is not a complete, consistent state until SQLite folds the WAL back in.

A plain copy (Explorer, `cp`, `robocopy`, a cloud-sync client) can therefore:

- catch the database mid-write, producing a copy that fails `PRAGMA integrity_check`;
- copy the `.db` without its `-wal`, silently dropping the newest changes;
- leave a stale `-wal` beside a new `.db`, which SQLite may then apply to the wrong file.

Also never open the database over a network share (SQLite file locking is unreliable there). Make a copy with the tools below, then open the copy locally.

## Which protection do I need?

| I want to... | Use | Protects against |
|---|---|---|
| Give another machine (for example a Power BI PC) a current copy | `tools/db_snapshot.py make` on the machine that owns the database, `fetch` on the other | corrupt or half-written copies |
| Take a safe backup before something risky (an update, a migration, a first run of new code) | `tools/db_snapshot.py make` with a dated `--out-dir`, or the one-line fallback below | any mistake that damages the database file |
| Run a maintenance tool | Stop the sync task first. `tools/migrate_column.py` and `tools/split_date_columns.py` write a `.bak` copy of the whole database before they start, but they use a plain file copy (not the backup API), so it is only safe while no sync is running. For extra safety take a `make` snapshot first | a failed migration |
| Recover old values after a property type change in Notion | the `<column>_bak_YYYYMMDD` columns the sync creates | Notion converting or losing values on a type change |

The last row is **not** protection against corruption: those columns live inside the same database file. See [Changed property type](reference.md#changed-property-type). The `<table>_changes` tables (your change history) exist nowhere else, so a file-level backup before risky work is the only way to protect them.

## `tools/db_snapshot.py`

Standard library only. It has two commands. Run it with the same Python as the sync (`venv\Scripts\python.exe` on Windows, `venv/bin/python` on Linux). It needs `check_freshness.py` in the same folder.

### `make`: snapshot the live database

Run on the machine that owns the live database.

```
python tools/db_snapshot.py make --db C:\Notion_Data\notion_analytics.db --out-dir C:\Notion_Data\snapshots
```

| Option | Meaning |
|---|---|
| `--db` | The live `notion_analytics.db` (required) |
| `--out-dir` | Folder to write snapshots into; created if missing (required) |
| `--keep N` | Keep only the newest N snapshots (default 3; minimum 1) |

What it does:

1. Uses SQLite's own backup API, which reads one consistent state even while `sync.py` is writing.
2. Writes to `notion_analytics.<UTC timestamp>.db.tmp`, switches the copy to a single self-contained file (no `-wal`/`-shm`), and checks it: `PRAGMA integrity_check` must return `ok` and `_meta` must contain a `last_sync` timestamp.
3. Only then renames it to `notion_analytics.<YYYYMMDDTHHMMSSZ>.db`. A reader never sees a half-written snapshot, and a failed run publishes nothing.
4. Deletes snapshots beyond `--keep`, newest kept first. A file another program has open is skipped and removed on the next run.

A snapshot taken during a sync is consistent but reflects the database as of that moment.

### `fetch`: install the newest snapshot elsewhere

Run on the machine that reads the copy.

```
python tools/db_snapshot.py fetch --src-dir \\sync-host\Notion_Snapshots --dest D:\Notion_Data\notion_analytics.db
```

| Option | Meaning |
|---|---|
| `--src-dir` | Folder holding the snapshots, for example the share the other machine publishes to (required) |
| `--dest` | The local `notion_analytics.db` to replace (required) |
| `--log` | Optional file that gets one line per run (time, command, exit code, message). Also records bad-arguments runs (`usage`, exit 2) with the error. Scheduled tasks discard their output, so use this on the reader machine |
| `--log-days N` | Days of log history kept (default 60); older lines are dropped on each run, so the file stays small. `make` accepts both options too |

What it does: copies the newest snapshot to a temporary file beside `--dest`, runs the same checks, and replaces `--dest` only if the snapshot is newer than the current copy (by `last_sync`). After the swap it removes stale `-wal`, `-shm` and `-journal` files beside `--dest`. If anything fails, the existing copy is left untouched.

### Exit codes

| Code | Output starts with | Meaning |
|---|---|---|
| 0 | `OK` | Done, or "already up to date" |
| 1 | `FAILED` | Nothing was published or installed. The message says why |
| 2 | (usage text) | Bad or missing arguments |
| 3 | `IN USE` | `--dest` is open in another program (for example a connected DB browser or Power BI). The old copy is kept; try again later |

Task Scheduler stores the exit code as the task's last result (`Get-ScheduledTaskInfo -TaskName "<name>" | Select-Object LastRunTime, LastTaskResult`; the History tab only says "completed" for any code). It keeps only the latest run, so add `--log` to keep the message for every run.

## Scheduling

- **`make`, simplest:** let the sync do it. Add `snapshot_dir` (and optionally `snapshot_keep`, default 3) to the `[output]` section of `config.toml`; `sync.py` then writes a verified snapshot after every run, even when a database failed, and exits 1 if the snapshot itself fails. One scheduled task on the owning machine does both.
- **`make`, separate task:** only if you want snapshots on a different schedule from the sync. Schedule it a few minutes after the hourly sync; it is safe if the two overlap.
- **`fetch`:** schedule it on the reader machine, a few minutes after `make`. On Windows, set the task to **Run whether user is logged on or not** so it still runs when nobody is signed in. A task limited to "only when user is logged on" silently skips runs. In the Task Scheduler window, set the trigger's "Repeat task every 1 hour" to "for a duration of: **Indefinitely**". The default is 1 day, and the repeat silently stops 24 hours after the trigger's start time (the history shows no runs after that).
- Pair it with the [Stale-Data Alert](stale-data-alert.md) (`tools/check_freshness.py`) so a copy that stops updating is noticed.

## A one-off backup without the tool

If the tool is not on the machine yet, this makes the same kind of consistent copy using only Python. Replace the path:

```
python -c "import sqlite3,sys,datetime; src=sys.argv[1]; dst=src+'.'+datetime.date.today().isoformat()+'.bak'; s=sqlite3.connect(src); d=sqlite3.connect(dst); s.backup(d); print(dst); print(d.execute('PRAGMA integrity_check').fetchone()[0])" C:\Notion_Data\notion_analytics.db
```

It must print the new file name and then `ok`. If the second line is anything else, do not rely on that backup.

## Restoring from a snapshot or backup

1. Stop whatever runs the sync (disable the scheduled task) and close any program that has the database open.
2. Copy the snapshot or `.bak` over `notion_analytics.db`.
3. Delete `notion_analytics.db-wal` and `notion_analytics.db-shm` beside it if they exist. They belong to the old file.
4. Check it (see below).
5. Run `sync.py` once by hand and read the end of `notion_analytics.log`. The incremental sync catches up from the `last_sync` stored in the restored file.
6. Re-enable the scheduled task.

## Checking a database

```
python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute('PRAGMA integrity_check').fetchone()[0]); [print(r) for r in c.execute('SELECT key,value FROM _meta') if r[0].startswith('last_sync:')]" C:\Notion_Data\notion_analytics.db
```

Healthy output is `ok` followed by one `last_sync:<table>` line per synced database with a recent timestamp.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `FAILED - database not found` | `--db` points at the wrong path. Check `db_path` under `[output]` in `config.toml` (relative paths are relative to the project folder) |
| `FAILED - ...: no last_sync timestamps in _meta` | The database has never completed a sync, so there is nothing to verify. Run `sync.py` once first |
| `FAILED - ...: integrity_check failed` | The source database is damaged. Do not publish it; restore from an earlier snapshot |
| `FAILED - no snapshot found in ...` | `make` has not run yet, or `--src-dir` is wrong |
| `IN USE` (exit 3) | Disconnect or close the program holding the destination (a connected DBeaver or other DB browser, Power BI Desktop) and let the next scheduled run retry; see [Troubleshooting](troubleshooting.md) |
| Copy is stale but the task shows success | Compare the `last_sync` lines from "Checking a database" on both machines. If `make` is succeeding but the reader is old, `fetch` is not running (check that the task is set to run when logged off) |
