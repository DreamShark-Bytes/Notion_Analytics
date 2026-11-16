"""
tools/check_freshness.py
Alert when the Notion Analytics database copy on this machine has gone stale.

Reads the per-database `last_sync:<table>` timestamps that sync.py stores in the
`_meta` table (read-only) and shows a Windows toast notification if any is older
than --max-age-hours, or if the database is missing or unreadable. Run it as its
own scheduled task, separate from the copy task, so it still fires when the copy
task never runs.

Usage examples:
  # Check now, print the result, never notify
  python tools/check_freshness.py --db C:\\data\\notion_analytics.db --notify none

  # Normal scheduled use (no console window: use pythonw.exe)
  pythonw.exe tools\\check_freshness.py --db C:\\data\\notion_analytics.db

  # Silence alerts for 48 hours (e.g. planned shutdown)
  python tools/check_freshness.py --db C:\\data\\notion_analytics.db --snooze-hours 48

Exit code: 0 = fresh, 1 = stale/missing/unreadable (visible as "Last Run Result" in
Task Scheduler), 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

META_PREFIX = "last_sync:"

_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$esc = { param($s) [System.Security.SecurityElement]::Escape($s) }
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast scenario='reminder'><visual><binding template='ToastGeneric'><text>$(& $esc $env:NA_TOAST_TITLE)</text><text>$(& $esc $env:NA_TOAST_BODY)</text></binding></visual><actions><action content='Dismiss' arguments='dismiss' activationType='system'/></actions></toast>")
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


# ── Pure logic ────────────────────────────────────────────────────────────────

def parse_timestamp(value: str) -> datetime:
    """Parse an ISO timestamp; a naive value is treated as UTC."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def evaluate(last_syncs: dict[str, str], now: datetime, max_age: timedelta) -> dict[str, str]:
    """Return {table: problem description} for every table that is stale or unparseable."""
    problems: dict[str, str] = {}
    if not last_syncs:
        problems["(database)"] = "no sync timestamps recorded"
        return problems
    for table, raw in sorted(last_syncs.items()):
        try:
            age = now - parse_timestamp(raw)
        except ValueError:
            problems[table] = f"unreadable sync timestamp {raw!r}"
            continue
        if age > max_age:
            problems[table] = f"last synced {_fmt_age(age)} ago"
    return problems


def _fmt_age(age: timedelta) -> str:
    hours = age.total_seconds() / 3600
    return f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.0f} hours"


def should_notify(state: dict, problems: dict[str, str], now: datetime, realert: timedelta) -> bool:
    """Notify on a new/changed problem set, or when the re-alert interval has passed."""
    if not problems:
        return False
    snooze = state.get("snooze_until")
    if snooze and now < parse_timestamp(snooze):
        return False
    if sorted(problems) != state.get("alerting", []):
        return True
    last_alert = state.get("last_alert")
    return last_alert is None or now - parse_timestamp(last_alert) >= realert


# ── I/O ───────────────────────────────────────────────────────────────────────

def read_last_syncs(db_path: Path) -> dict[str, str]:
    """Read `last_sync:*` rows from `_meta` read-only. Raises on a missing, unreadable or damaged file."""
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        # A copy can hold a fresh `_meta` and still be damaged elsewhere; quick_check reads every page.
        verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
        if verdict != "ok":
            raise sqlite3.DatabaseError(f"quick_check failed: {verdict}")
        rows = conn.execute(
            'SELECT key, value FROM "_meta" WHERE key LIKE ?', (META_PREFIX + "%",)
        ).fetchall()
    finally:
        conn.close()
    return {key[len(META_PREFIX):]: value for key, value in rows}


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def show_toast(title: str, body: str) -> None:
    """Show a Windows toast via the built-in WinRT API (no extra packages)."""
    if sys.platform != "win32":
        print(f"[toast unavailable on {sys.platform}] {title}: {body}")
        return
    env = {**os.environ, "NA_TOAST_TITLE": title, "NA_TOAST_BODY": body}
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _TOAST_PS],
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Alert when the Notion Analytics database copy is stale.")
    ap.add_argument("--db", required=True, type=Path, help="Path to the notion_analytics.db copy to check")
    ap.add_argument("--max-age-hours", type=float, default=30,
                    help="Alert when a database's last sync is older than this (default 30: daily copy + margin)")
    ap.add_argument("--realert-hours", type=float, default=24,
                    help="Repeat an unchanged alert at most this often (default 24)")
    ap.add_argument("--snooze-hours", type=float, default=None,
                    help="Silence alerts for this many hours, then exit")
    ap.add_argument("--state-file", type=Path, default=None,
                    help="Alert state file (default: <db>.freshness.json next to the database)")
    ap.add_argument("--notify", choices=["toast", "none"], default="toast",
                    help="How to alert (default toast); 'none' only prints")
    args = ap.parse_args(argv)

    state_path = args.state_file or args.db.with_name(args.db.name + ".freshness.json")
    state = load_state(state_path)
    now = datetime.now(timezone.utc)

    if args.snooze_hours is not None:
        state["snooze_until"] = (now + timedelta(hours=args.snooze_hours)).isoformat()
        save_state(state_path, state)
        print(f"Alerts snoozed until {state['snooze_until']}")
        return 0

    try:
        problems = evaluate(read_last_syncs(args.db), now, timedelta(hours=args.max_age_hours))
    except (OSError, sqlite3.Error) as exc:
        problems = {"(database)": f"cannot read {args.db}: {exc}"}

    state["last_check"] = now.isoformat()
    state["last_result"] = "stale" if problems else "fresh"

    if problems:
        body = "; ".join(f"{t}: {msg}" for t, msg in problems.items())
        print(f"STALE - {body}")
        if args.notify == "toast" and should_notify(state, problems, now, timedelta(hours=args.realert_hours)):
            show_toast("Notion Analytics data is stale", body)
            state["last_alert"] = now.isoformat()
            state["alerting"] = sorted(problems)
    else:
        print("OK - all databases are fresh")
        if state.get("alerting") and args.notify == "toast":
            show_toast("Notion Analytics data is fresh again", "The database copy is up to date.")
        state.pop("alerting", None)
        state.pop("last_alert", None)

    save_state(state_path, state)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
