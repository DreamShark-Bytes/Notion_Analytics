# Scheduling

The sync is meant to run unattended. An hourly run is an *incremental* sync (only pages edited since the last successful run are fetched; see [Guide](guide.md#incremental-sync-and-full-refresh)), so it is short once the first full pull is done.

## Windows — Task Scheduler

1. Open **Task Scheduler** → **Create Task** (not "Create Basic Task")
2. **General tab:** Name: `Notion Analytics Sync` · Description: `Syncs Notion databases to a local SQLite file for use with Power BI and Grafana.`
3. **Triggers tab:** New → On a schedule → **One time** → start at the next top of the hour → check **Repeat task every: 1 hour** for duration **Indefinitely** (a Daily trigger plus an indefinite repeat gave duplicate launches and a "launch request ignored" warning every hour on the author's machine, so use One time; a duration of 1 day silently stops the repeat after 24 hours)
4. **Actions tab:** New → Start a program
   - **Program/script:** `C:\Users\YourName\Documents\Notion_Analytics\venv\Scripts\python.exe`
   - **Add arguments:** `sync.py`
   - **Start in:** `C:\Users\YourName\Documents\Notion_Analytics`
5. **Conditions tab:** uncheck "Start the task only if the computer is on AC power" if on battery
6. **Settings tab:** check "If the task is already running, do not start a new instance" and "Stop the task if it runs longer than" **1 hour**
7. Click OK → right-click the task → **Run** once → check `notion_analytics.log`

## Linux — cron

```bash
crontab -e
```

Add (runs every hour):

```
0 * * * * cd /path/to/Notion_Analytics && /path/to/Notion_Analytics/venv/bin/python sync.py >> notion_analytics.log 2>&1
```

## Linux — systemd timer

Create `/etc/systemd/system/notion-analytics.service`:

```ini
[Unit]
Description=Notion Analytics sync

[Service]
Type=oneshot
WorkingDirectory=/path/to/Notion_Analytics
ExecStart=/path/to/Notion_Analytics/venv/bin/python sync.py
User=your-username
```

Create `/etc/systemd/system/notion-analytics.timer`:

```ini
[Unit]
Description=Run Notion Analytics sync hourly

[Timer]
OnBootSec=1min
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now notion-analytics.timer
```

## Check that it worked

After a scheduled run, `notion_analytics.log` (in the project folder) ends with `Sync complete in <seconds>s`. A line containing `Unexpected error syncing '<name>'` means that one database failed and the others still ran; see [Troubleshooting](troubleshooting.md).

## Optional: nightly deleted-page check

`sync.py --detect-deleted` is slower than the hourly run and is meant to run once a night as its own scheduled task, using the same steps as above with `sync.py --detect-deleted` as the argument. See [Deleted and Moved Pages](deleted-pages.md).
