# Updating

Back up first if the database matters to you: see [Backups and Database Copies](backup-and-snapshots.md).

## 1. Stop the scheduler

**Windows — Task Scheduler:**
Right-click the `Notion Analytics Sync` task → **Disable**. If it is currently running, also click **End** before disabling.

**Linux — systemd timer:**
```bash
sudo systemctl stop notion-analytics.timer
```

**Linux — cron:**
```bash
crontab -e   # comment out the sync line with #
```

## 2. Pull and reinstall

```bash
git pull
```

Reinstall dependencies in case `requirements.txt` changed:

```bash
venv/bin/python -m pip install -r requirements.txt    # Linux
venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
```

Check that the Notion client in the virtual environment is the version `requirements.txt` pins (the tag after `@`):

```bash
venv/bin/python -c "import notion_api; print(notion_api.__version__)"        # Linux / macOS
venv\Scripts\python -c "import notion_api; print(notion_api.__version__)"   # Windows
```

If it prints a different version, run the `pip install -r requirements.txt` command above again and re-check. Trust this check rather than `pip show notion-api`: the package metadata can carry a different version number than the code (a 1.0.1 client reported `0.1.0`). Always call the virtual environment's own `python` and `pip` by path, as above; an old `activate` script can silently fall back to the system Python if the project folder was ever moved or renamed.

Check the [commit history](https://github.com/DreamShark-Bytes/Notion_Analytics/commits/main) for breaking changes or migration steps before continuing.

## 3. Run the sync once manually

```bash
venv/bin/python sync.py    # Linux
venv\Scripts\python sync.py   # Windows
```

Schema changes are applied automatically. Verify the log looks clean before re-enabling the scheduler.

## 4. Re-enable the scheduler

**Windows — Task Scheduler:**
Right-click the task → **Enable**.

**Linux — systemd timer:**
```bash
sudo systemctl start notion-analytics.timer
```

**Linux — cron:**
```bash
crontab -e   # remove the # from the sync line
```

---
