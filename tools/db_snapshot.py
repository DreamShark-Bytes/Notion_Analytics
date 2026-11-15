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
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from check_freshness import parse_timestamp, read_last_syncs

PREFIX = "notion_analytics."
SNAPSHOT_RE = re.compile(r"^notion_analytics\.\d{8}T\d{6}Z\.db$")
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


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

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Publish or fetch consistent Notion Analytics database snapshots.")
    sub = ap.add_subparsers(dest="command", required=True)

    mk = sub.add_parser("make", help="Laptop: snapshot the live database into a folder")
    mk.add_argument("--db", required=True, type=Path, help="Path to the live notion_analytics.db")
    mk.add_argument("--out-dir", required=True, type=Path, help="Folder the PC will fetch from")
    mk.add_argument("--keep", type=int, default=3, help="Snapshots to keep (default 3)")

    fe = sub.add_parser("fetch", help="PC: install the newest snapshot as the local database")
    fe.add_argument("--src-dir", required=True, type=Path, help="Folder holding the snapshots (the share)")
    fe.add_argument("--dest", required=True, type=Path, help="Local notion_analytics.db to replace")

    args = ap.parse_args(argv)

    try:
        if args.command == "make":
            if args.keep < 1:
                ap.error("--keep must be at least 1")
            path = make_snapshot(args.db, args.out_dir, args.keep)
            print(f"OK - snapshot written: {path}")
        else:
            outcome = fetch_snapshot(args.src_dir, args.dest)
            print("OK - already up to date" if outcome == "current" else f"OK - installed {args.dest}")
        return 0
    except DestinationInUse as exc:
        print(f"IN USE - {exc}")
        return 3
    except SnapshotError as exc:
        print(f"FAILED - {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
