"""
tools/db_snapshot.py
Publish and fetch consistent snapshots of the Notion Analytics SQLite database.

Why: the live database runs in WAL mode, so a plain file copy (robocopy, Explorer) can
catch it mid-write and produce a corrupt copy. SQLite's backup API reads one consistent
state even while sync.py is writing, and SQLite is only ever opened on the machine that
owns the file (never across a network share).

  make   (laptop, scheduled on its own) Snapshot the live database into --out-dir as
         notion_analytics.<UTC timestamp>.db. It is written under a .tmp name, verified
         (integrity_check + a last_sync timestamp in _meta), then renamed, so a reader
         never sees a half-written file. Only the newest --keep snapshots are kept.
  fetch  (PC, scheduled) Copy the newest snapshot from --src-dir, verify the copy, and swap
         it in as --dest. The old --dest is replaced only by a verified, newer file, and
         stale -wal/-shm/-journal files beside --dest are removed after the swap.

Usage examples:
  python tools/db_snapshot.py make --db C:\\Notion_Data\\notion_analytics.db --out-dir C:\\Notion_Data\\snapshots
  python tools/db_snapshot.py fetch --src-dir \\\\sync-host\\Notion_Snapshots --dest D:\\Notion_Data\\notion_analytics.db

Exit code: 0 = ok (including "already up to date"), 1 = failed (nothing published or
installed), 2 = usage error, 3 = destination in use (old copy kept, try again later).
Needs check_freshness.py in the same folder. Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from check_freshness import parse_timestamp, read_last_syncs

PREFIX = "notion_analytics."
SNAPSHOT_RE = re.compile(r"^notion_analytics\.\d{8}T\d{6}Z\.db$")
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
DEFAULT_LOG_DAYS = 60


class SnapshotError(Exception):
    """A snapshot could not be made, verified or installed."""


class DestinationInUse(SnapshotError):
    """The destination file is open elsewhere; the old copy was kept."""


# ── Shared ────────────────────────────────────────────────────────────────────

def verify_database(path: Path) -> str:
    """Return the newest `last_sync:*` timestamp after a clean integrity_check. Raises SnapshotError."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
        last_syncs = read_last_syncs(path)
    except (OSError, sqlite3.Error) as exc:
        raise SnapshotError(f"{path.name}: {exc}") from exc
    if result != [("ok",)]:
        raise SnapshotError(f"{path.name}: integrity_check failed: {result[:3]}")
    if not last_syncs:
        raise SnapshotError(f"{path.name}: no last_sync timestamps in _meta")
    return max(last_syncs.values(), key=parse_timestamp)


def list_snapshots(directory: Path) -> list[Path]:
    """Complete snapshots in `directory`, newest first (the name embeds a sortable UTC stamp)."""
    return sorted((p for p in directory.iterdir() if SNAPSHOT_RE.match(p.name)), reverse=True)


# ── make (laptop) ─────────────────────────────────────────────────────────────

def make_snapshot(db: Path, out_dir: Path, keep: int, now: datetime | None = None) -> Path:
    """Write a verified snapshot of `db` into `out_dir` and prune old ones. Returns the new path."""
    if not db.is_file():
        raise SnapshotError(f"database not found: {db}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for leftover in out_dir.glob(f"{PREFIX}*.db.tmp"):
        leftover.unlink(missing_ok=True)

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    final = out_dir / f"{PREFIX}{stamp}.db"
    tmp = final.with_name(final.name + ".tmp")
    try:
        src = sqlite3.connect(str(db), timeout=30)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm
            finally:
                dst.close()
        finally:
            src.close()
        verify_database(tmp)
        os.replace(tmp, final)
    except (OSError, sqlite3.Error) as exc:
        raise SnapshotError(f"could not snapshot {db}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)

    for old in list_snapshots(out_dir)[keep:]:
        try:
            old.unlink()
        except OSError:
            pass  # a reader still has it open; the next run prunes it
    return final


# ── fetch (PC) ────────────────────────────────────────────────────────────────

def fetch_snapshot(src_dir: Path, dest: Path) -> str:
    """Install the newest snapshot as `dest`. Returns 'installed' or 'current'. Raises SnapshotError."""
    try:
        snapshots = list_snapshots(src_dir)
    except OSError as exc:
        raise SnapshotError(f"cannot read {src_dir}: {exc}") from exc
    if not snapshots:
        raise SnapshotError(f"no snapshot found in {src_dir}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".fetch.tmp")
    try:
        try:
            shutil.copyfile(snapshots[0], tmp)
        except OSError as exc:
            raise SnapshotError(f"could not copy {snapshots[0].name}: {exc}") from exc
        new_sync = verify_database(tmp)

        if dest.is_file():
            try:
                old_sync = max(read_last_syncs(dest).values(), key=parse_timestamp, default=None)
            except (OSError, sqlite3.Error):
                old_sync = None  # unreadable old copy: replace it
            if old_sync and parse_timestamp(old_sync) >= parse_timestamp(new_sync):
                return "current"

        try:
            os.replace(tmp, dest)
        except PermissionError as exc:
            raise DestinationInUse(f"{dest} is open in another program; old copy kept") from exc
        except OSError as exc:
            raise SnapshotError(f"could not install {dest}: {exc}") from exc
        for suffix in SIDECAR_SUFFIXES:
            dest.with_name(dest.name + suffix).unlink(missing_ok=True)
        return "installed"
    finally:
        tmp.unlink(missing_ok=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

def write_log(path: Path, command: str, code: int, message: str, keep_days: int, now: datetime | None = None) -> None:
    """Append one line for this run and drop lines older than keep_days. Never raises: a log problem must not change the exit code."""
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(days=keep_days)

    def is_old(line: str) -> bool:
        try:
            stamp = datetime.fromisoformat(line.lstrip("\ufeff").split(" ", 1)[0])
        except ValueError:
            return False  # not one of ours: keep it
        return stamp.astimezone() < cutoff  # a timestamp without an offset counts as local time

    entry = f"{now.isoformat(timespec='seconds')} {command} exit={code} {' | '.join(message.splitlines())}"
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if path.is_file() else []
        tmp.write_text("\n".join([ln for ln in old if not is_old(ln)] + [entry]) + "\n", encoding="utf-8")
        os.replace(tmp, path)  # a crash mid-write cannot truncate the existing log
    except OSError as exc:
        print(f"WARNING - could not write log {path}: {exc}")
    finally:
        tmp.unlink(missing_ok=True)


def _log_args(argv: list[str]) -> tuple[Path | None, int]:
    """Find --log / --log-days in raw arguments, for runs where argparse itself failed."""
    path, days = None, DEFAULT_LOG_DAYS
    for i, arg in enumerate(argv):
        name, _, value = arg.partition("=")
        value = value or (argv[i + 1] if i + 1 < len(argv) else "")
        if name == "--log" and value:
            path = Path(value)
        elif name == "--log-days" and value.isdigit():
            days = int(value)
    return path, days


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log", type=Path, help="Append one line per run to this file (kept for --log-days)")
    common.add_argument("--log-days", type=int, default=DEFAULT_LOG_DAYS, help=f"Days of log history to keep (default {DEFAULT_LOG_DAYS})")

    ap = argparse.ArgumentParser(description="Publish or fetch consistent Notion Analytics database snapshots.")
    sub = ap.add_subparsers(dest="command", required=True)

    mk = sub.add_parser("make", parents=[common], help="Laptop: snapshot the live database into a folder")
    mk.add_argument("--db", required=True, type=Path, help="Path to the live notion_analytics.db")
    mk.add_argument("--out-dir", required=True, type=Path, help="Folder the PC will fetch from")
    mk.add_argument("--keep", type=int, default=3, help="Snapshots to keep (default 3)")

    fe = sub.add_parser("fetch", parents=[common], help="PC: install the newest snapshot as the local database")
    fe.add_argument("--src-dir", required=True, type=Path, help="Folder holding the snapshots (the share)")
    fe.add_argument("--dest", required=True, type=Path, help="Local notion_analytics.db to replace")

    usage_text = io.StringIO()
    try:
        with contextlib.redirect_stderr(usage_text):
            args = ap.parse_args(argv)
            if args.command == "make" and args.keep < 1:
                ap.error("--keep must be at least 1")
    except SystemExit as exc:  # argparse exits 2 on bad arguments; keep that code but record why
        sys.stderr.write(usage_text.getvalue())
        code = exc.code if isinstance(exc.code, int) else 2
        log_path, log_days = _log_args(argv)
        if code != 0 and log_path:
            error_line = usage_text.getvalue().strip().splitlines()[-1:] or ["usage error"]
            write_log(log_path, "usage", code, f"{error_line[0]} (arguments: {' '.join(argv)})", log_days)
        return code

    try:
        if args.command == "make":
            path = make_snapshot(args.db, args.out_dir, args.keep)
            code, message = 0, f"OK - snapshot written: {path}"
        else:
            outcome = fetch_snapshot(args.src_dir, args.dest)
            code, message = 0, "OK - already up to date" if outcome == "current" else f"OK - installed {args.dest}"
    except DestinationInUse as exc:
        code, message = 3, f"IN USE - {exc}"
    except SnapshotError as exc:
        code, message = 1, f"FAILED - {exc}"
    print(message)
    if args.log:
        write_log(args.log, args.command, code, message, args.log_days)
    return code


if __name__ == "__main__":
    sys.exit(main())
