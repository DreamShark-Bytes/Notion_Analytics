# Stale-Data Alert (Windows)

If the database is copied to another Windows machine (for example to open it in Power BI), a copy that silently stops updating looks the same as a working one. `tools/check_freshness.py` reads the `last_sync:<table>` timestamps in the `_meta` table (read-only) and shows a Windows toast notification when any are older than `--max-age-hours` (default 30), or when the file is missing or unreadable. It uses only the Python standard library, so you can copy that single file to the machine that holds the database copy.

```
pythonw.exe check_freshness.py --db "C:\path\to\notion_analytics.db"
```

It also runs SQLite's `PRAGMA quick_check` on the copy, so a damaged file is reported even when its timestamps look fresh.

Create it as its own Task Scheduler task, separate from whatever copies the file, so it still fires when the copy task never runs:

- **Run only when user is logged on** (a toast cannot be shown from a SYSTEM or logged-off task).
- **Program:** the full path to `pythonw.exe` (not `python.exe`, which opens a console window). **Arguments:** the script path and `--db ...`.
- **Triggers:** At log on, plus repeat every few hours.
- Exit code 1 (stale) appears as the task's "Last Run Result".

Repeated alerts are limited by `--realert-hours` (default 24), and a "fresh again" toast is shown once on recovery. `--snooze-hours N` silences alerts for a planned shutdown. `--notify none` prints the result without a toast, which is useful for a first manual run.

To make the copy itself reliable, use the snapshot tool rather than a plain file copy: see [Backups and Database Copies](backup-and-snapshots.md).
