import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from db_snapshot import (
    DestinationInUse,
    SnapshotError,
    fetch_snapshot,
    list_snapshots,
    main,
    make_snapshot,
    verify_database,
    write_log,
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


# ── Run log (--log) ───────────────────────────────────────────────────────────

def test_log_records_success_failure_and_in_use(live, tmp_path, monkeypatch):
    path, conn = live
    out = tmp_path / "share"
    _publish(path, out, 1, "2026-11-12T18:01:00+00:00")
    dest = tmp_path / "pc" / "notion_analytics.db"
    log = tmp_path / "logs" / "fetch.log"
    assert main(["fetch", "--src-dir", str(out), "--dest", str(dest), "--log", str(log)]) == 0
    assert main(["fetch", "--src-dir", str(tmp_path / "none"), "--dest", str(dest), "--log", str(log)]) == 1
    _publish(path, out, 2, "2026-11-12T18:02:00+00:00")
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("locked")) if str(d).endswith(".db") else real_replace(s, d))
    assert main(["fetch", "--src-dir", str(out), "--dest", str(dest), "--log", str(log)]) == 3
    lines = log.read_text(encoding="utf-8").splitlines()
    assert [" ".join(ln.split(" ", 3)[1:3]) for ln in lines] == ["fetch exit=0", "fetch exit=1", "fetch exit=3"]
    assert "OK - installed" in lines[0] and "FAILED" in lines[1] and "IN USE" in lines[2]


def test_log_drops_lines_older_than_the_retention_window(tmp_path):
    log = tmp_path / "fetch.log"
    now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
    old = (now - timedelta(days=61)).isoformat(timespec="seconds")
    recent = (now - timedelta(days=59)).isoformat(timespec="seconds")
    log.write_text(f"{old} fetch exit=0 old\n{recent} fetch exit=0 recent\nnot a log line\n", encoding="utf-8")
    write_log(log, "fetch", 0, "OK", keep_days=60, now=now)
    text = log.read_text(encoding="utf-8")
    assert " old" not in text and "recent" in text and "not a log line" in text
    assert text.count("\n") == 3 and text.rstrip().endswith("fetch exit=0 OK")


def test_log_records_usage_errors_with_the_message(tmp_path, capsys):
    log = tmp_path / "fetch.log"
    assert main(["fetch", "--log", str(log), "--dest", str(tmp_path / "d.db")]) == 2
    line = log.read_text(encoding="utf-8")
    assert " usage exit=2 " in line and "--src-dir" in line
    assert "required" in capsys.readouterr().err


def test_log_failure_never_changes_the_exit_code(live, tmp_path, capsys):
    path, conn = live
    out = tmp_path / "share"
    log_dir = tmp_path / "is_a_folder"
    log_dir.mkdir()
    assert main(["make", "--db", str(path), "--out-dir", str(out), "--log", str(log_dir)]) == 0
    assert "WARNING - could not write log" in capsys.readouterr().out


def test_no_log_option_writes_no_file(live, tmp_path):
    path, conn = live
    main(["make", "--db", str(path), "--out-dir", str(tmp_path / "share")])
    assert not list(tmp_path.glob("**/*.log"))


def test_log_survives_naive_timestamps_bom_and_binary_junk(tmp_path):
    log = tmp_path / "fetch.log"
    now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
    log.write_bytes("\ufeff2020-01-01T00:00:00 fetch exit=0 naive and ancient\n".encode("utf-8") + b"\xff\xfe junk\n")
    write_log(log, "fetch", 0, "OK", keep_days=60, now=now)
    text = log.read_text(encoding="utf-8")
    assert "ancient" not in text and "junk" in text and text.rstrip().endswith("fetch exit=0 OK")
    assert not (tmp_path / "fetch.log.tmp").exists()
