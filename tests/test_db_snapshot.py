import os
import sqlite3
from datetime import datetime, timezone

import pytest
from db_snapshot import (
    DestinationInUse,
    SnapshotError,
    fetch_snapshot,
    list_snapshots,
    main,
    make_snapshot,
    verify_database,
)

T0 = datetime(2026, 11, 12, 18, 0, 0, tzinfo=timezone.utc)


def _make_live(path, last_sync="2026-11-12T18:00:00+00:00", rows=50):
    """A WAL-mode database; the returned connection stays open so the -wal keeps unmerged data."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute('CREATE TABLE "_meta" (key TEXT PRIMARY KEY, value TEXT)')
    conn.execute('INSERT INTO "_meta" VALUES (?, ?)', ("last_sync:tasks", last_sync))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row {i}",) for i in range(rows)])
    conn.commit()
    return conn


def _count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def _journal_mode(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "live" / "notion_analytics.db"
    path.parent.mkdir()
    conn = _make_live(path)
    yield path, conn
    conn.close()


# ── make ──────────────────────────────────────────────────────────────────────

def test_make_includes_data_still_in_the_wal(live, tmp_path):
    path, conn = live
    assert os.path.getsize(str(path) + "-wal") > 0  # recent writes are only in the -wal file
    snap = make_snapshot(path, tmp_path / "out", keep=3, now=T0)
    assert _count(snap) == 50


def test_make_snapshot_is_a_single_clean_file(live, tmp_path):
    path, conn = live
    out = tmp_path / "out"
    snap = make_snapshot(path, out, keep=3, now=T0)
    assert snap.name == "notion_analytics.20261112T180000Z.db"
    assert _journal_mode(snap) == "delete"
    assert sorted(p.name for p in out.iterdir()) == [snap.name]
    assert verify_database(snap) == "2026-11-12T18:00:00+00:00"


def test_make_while_a_writer_is_active(live, tmp_path):
    path, conn = live
    conn.execute("BEGIN")
    conn.execute("INSERT INTO t (v) VALUES ('uncommitted')")
    snap = make_snapshot(path, tmp_path / "out", keep=3, now=T0)
    conn.rollback()
    assert _count(snap) == 50  # the open transaction is not in the snapshot


def test_make_prunes_to_keep(live, tmp_path):
    path, conn = live
    out = tmp_path / "out"
    for minute in range(5):
        make_snapshot(path, out, keep=2, now=T0.replace(minute=minute))
    names = [p.name for p in list_snapshots(out)]
    assert names == ["notion_analytics.20261112T180400Z.db", "notion_analytics.20261112T180300Z.db"]


def test_make_missing_database_fails_cleanly(tmp_path):
    with pytest.raises(SnapshotError, match="not found"):
        make_snapshot(tmp_path / "nope.db", tmp_path / "out", keep=3, now=T0)


def test_make_corrupt_live_database_publishes_nothing(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite database" * 200)
    out = tmp_path / "out"
    with pytest.raises(SnapshotError):
        make_snapshot(bad, out, keep=3, now=T0)
    assert list(out.iterdir()) == []


def test_make_database_without_sync_timestamps_is_rejected(tmp_path):
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE "_meta" (key TEXT PRIMARY KEY, value TEXT)')
    conn.commit()
    conn.close()
    out = tmp_path / "out"
    with pytest.raises(SnapshotError, match="last_sync"):
        make_snapshot(path, out, keep=3, now=T0)
    assert list(out.iterdir()) == []


def test_make_removes_leftover_tmp_files(live, tmp_path):
    path, conn = live
    out = tmp_path / "out"
    out.mkdir()
    (out / "notion_analytics.20260101T000000Z.db.tmp").write_bytes(b"half written")
    make_snapshot(path, out, keep=3, now=T0)
    assert [p.name for p in out.iterdir()] == ["notion_analytics.20261112T180000Z.db"]


# ── fetch ─────────────────────────────────────────────────────────────────────

def _publish(live_path, out, minute, last_sync):
    conn = sqlite3.connect(live_path)
    conn.execute('UPDATE "_meta" SET value = ?', (last_sync,))
    conn.commit()
    conn.close()
    return make_snapshot(live_path, out, keep=5, now=T0.replace(minute=minute))


def test_fetch_installs_the_newest_snapshot(live, tmp_path):
    path, conn = live
    out = tmp_path / "share"
    _publish(path, out, 1, "2026-11-12T18:01:00+00:00")
    _publish(path, out, 2, "2026-11-12T18:02:00+00:00")
    dest = tmp_path / "pc" / "notion_analytics.db"
    assert fetch_snapshot(out, dest) == "installed"
    assert verify_database(dest) == "2026-11-12T18:02:00+00:00"
    assert [p.name for p in dest.parent.iterdir()] == ["notion_analytics.db"]


def test_fetch_removes_stale_sidecar_files(live, tmp_path):
    path, conn = live
    out = tmp_path / "share"
    _publish(path, out, 1, "2026-11-12T18:01:00+00:00")
    dest = tmp_path / "pc" / "notion_analytics.db"
    dest.parent.mkdir()
    dest.write_bytes(b"old broken copy")
    for suffix in ("-wal", "-shm", "-journal"):
        (dest.parent / ("notion_analytics.db" + suffix)).write_bytes(b"stale")
    assert fetch_snapshot(out, dest) == "installed"
    assert [p.name for p in dest.parent.iterdir()] == ["notion_analytics.db"]


def test_fetch_skips_when_local_copy_is_current(live, tmp_path):
    path, conn = live
    out = tmp_path / "share"
    _publish(path, out, 1, "2026-11-12T18:01:00+00:00")
    dest = tmp_path / "pc" / "notion_analytics.db"
    assert fetch_snapshot(out, dest) == "installed"
    before = dest.stat().st_mtime_ns
    assert fetch_snapshot(out, dest) == "current"
    assert dest.stat().st_mtime_ns == before


def test_fetch_never_replaces_with_an_older_snapshot(live, tmp_path):
    path, conn = live
    out = tmp_path / "share"
    _publish(path, out, 1, "2026-11-12T18:01:00+00:00")
    dest = tmp_path / "pc" / "notion_analytics.db"
    dest.parent.mkdir()
    newer = _make_live(dest, last_sync="2026-11-12T19:00:00+00:00", rows=7)
    newer.close()
    assert fetch_snapshot(out, dest) == "current"
    assert _count(dest) == 7


def test_fetch_rejects_a_corrupt_snapshot_and_keeps_the_old_copy(live, tmp_path):
    path, conn = live
    out = tmp_path / "share"
    snap = _publish(path, out, 1, "2026-11-12T18:01:00+00:00")
    snap.write_bytes(snap.read_bytes()[:3000])  # truncated in transit
    dest = tmp_path / "pc" / "notion_analytics.db"
    dest.parent.mkdir()
    dest.write_bytes(b"previous good copy")
    with pytest.raises(SnapshotError):
        fetch_snapshot(out, dest)
    assert dest.read_bytes() == b"previous good copy"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["notion_analytics.db"]


def test_fetch_ignores_incomplete_tmp_files(live, tmp_path):
    path, conn = live
    out = tmp_path / "share"
    _publish(path, out, 1, "2026-11-12T18:01:00+00:00")
    (out / "notion_analytics.20261112T185900Z.db.tmp").write_bytes(b"half written")
    dest = tmp_path / "pc" / "notion_analytics.db"
    assert fetch_snapshot(out, dest) == "installed"
    assert verify_database(dest) == "2026-11-12T18:01:00+00:00"


def test_fetch_empty_or_missing_share_fails(tmp_path):
    (tmp_path / "share").mkdir()
    with pytest.raises(SnapshotError, match="no snapshot"):
        fetch_snapshot(tmp_path / "share", tmp_path / "pc" / "notion_analytics.db")
    with pytest.raises(SnapshotError, match="cannot read"):
        fetch_snapshot(tmp_path / "missing", tmp_path / "pc" / "notion_analytics.db")


def test_fetch_destination_in_use_keeps_old_copy(live, tmp_path, monkeypatch):
    path, conn = live
    out = tmp_path / "share"
    _publish(path, out, 1, "2026-11-12T18:01:00+00:00")
    dest = tmp_path / "pc" / "notion_analytics.db"
    dest.parent.mkdir()
    dest.write_bytes(b"previous copy, open in Power BI")

    def locked(src, dst):
        raise PermissionError("[WinError 32] The process cannot access the file")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(DestinationInUse):
        fetch_snapshot(out, dest)
    assert dest.read_bytes() == b"previous copy, open in Power BI"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["notion_analytics.db"]


# ── CLI exit codes ────────────────────────────────────────────────────────────

def test_cli_make_then_fetch(live, tmp_path, capsys):
    path, conn = live
    out = tmp_path / "share"
    dest = tmp_path / "pc" / "notion_analytics.db"
    assert main(["make", "--db", str(path), "--out-dir", str(out)]) == 0
    assert main(["fetch", "--src-dir", str(out), "--dest", str(dest)]) == 0
    assert main(["fetch", "--src-dir", str(out), "--dest", str(dest)]) == 0
    assert "already up to date" in capsys.readouterr().out


def test_cli_failures_exit_nonzero(tmp_path):
    assert main(["make", "--db", str(tmp_path / "nope.db"), "--out-dir", str(tmp_path / "o")]) == 1
    assert main(["fetch", "--src-dir", str(tmp_path / "none"), "--dest", str(tmp_path / "d.db")]) == 1


def test_cli_destination_in_use_exits_3(live, tmp_path, monkeypatch):
    path, conn = live
    out = tmp_path / "share"
    _publish(path, out, 1, "2026-11-12T18:01:00+00:00")
    dest = tmp_path / "pc" / "notion_analytics.db"
    dest.parent.mkdir()
    dest.write_bytes(b"x")
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("locked")))
    assert main(["fetch", "--src-dir", str(out), "--dest", str(dest)]) == 3
