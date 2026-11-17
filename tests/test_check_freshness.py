import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from check_freshness import evaluate, main, parse_timestamp, read_last_syncs, should_notify, toast_lines

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
MAX_AGE = timedelta(hours=30)
REALERT = timedelta(hours=24)


def _iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


# ── parse_timestamp ───────────────────────────────────────────────────────────

def test_parse_timestamp_aware():
    assert parse_timestamp("2026-10-21T12:00:00+00:00") == NOW


def test_parse_timestamp_naive_is_utc():
    assert parse_timestamp("2026-10-21T12:00:00") == NOW


# ── evaluate ──────────────────────────────────────────────────────────────────

def test_evaluate_all_fresh():
    assert evaluate({"tasks": _iso(2), "areas": _iso(29)}, NOW, MAX_AGE) == {}


def test_evaluate_flags_only_stale_tables():
    problems = evaluate({"tasks": _iso(2), "areas": _iso(31)}, NOW, MAX_AGE)
    assert list(problems) == ["areas"]
    assert "31 hours" in problems["areas"]


def test_evaluate_long_age_reported_in_days():
    problems = evaluate({"tasks": _iso(72)}, NOW, MAX_AGE)
    assert "3.0 days" in problems["tasks"]


def test_evaluate_empty_is_a_problem():
    assert "(database)" in evaluate({}, NOW, MAX_AGE)


def test_evaluate_unparseable_timestamp():
    problems = evaluate({"tasks": "not-a-date"}, NOW, MAX_AGE)
    assert "unreadable" in problems["tasks"]


# ── should_notify ─────────────────────────────────────────────────────────────

def test_should_notify_no_problems():
    assert not should_notify({}, {}, NOW, REALERT)


def test_should_notify_first_alert():
    assert should_notify({}, {"tasks": "x"}, NOW, REALERT)


def test_should_notify_suppressed_within_realert_window():
    state = {"alerting": ["tasks"], "last_alert": _iso(2)}
    assert not should_notify(state, {"tasks": "x"}, NOW, REALERT)


def test_should_notify_after_realert_window():
    state = {"alerting": ["tasks"], "last_alert": _iso(25)}
    assert should_notify(state, {"tasks": "x"}, NOW, REALERT)


def test_should_notify_when_problem_set_changes():
    state = {"alerting": ["tasks"], "last_alert": _iso(1)}
    assert should_notify(state, {"tasks": "x", "areas": "y"}, NOW, REALERT)


def test_should_notify_respects_snooze():
    state = {"snooze_until": (NOW + timedelta(hours=1)).isoformat()}
    assert not should_notify(state, {"tasks": "x"}, NOW, REALERT)


def test_should_notify_after_snooze_expires():
    state = {"snooze_until": (NOW - timedelta(hours=1)).isoformat()}
    assert should_notify(state, {"tasks": "x"}, NOW, REALERT)


# ── read_last_syncs / main ────────────────────────────────────────────────────

def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE "_meta" (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.executemany('INSERT INTO "_meta" VALUES (?, ?)', rows)
    conn.commit()
    conn.close()


def test_read_last_syncs_returns_only_last_sync_keys(tmp_path):
    db = tmp_path / "n.db"
    _make_db(db, [("last_sync:tasks", "T1"), ("other", "x"), ("last_sync:areas", "T2")])
    assert read_last_syncs(db) == {"tasks": "T1", "areas": "T2"}


def test_read_last_syncs_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_last_syncs(tmp_path / "missing.db")


def test_main_fresh_exits_zero_and_writes_state(tmp_path):
    db = tmp_path / "n.db"
    _make_db(db, [("last_sync:tasks", datetime.now(timezone.utc).isoformat())])
    assert main(["--db", str(db), "--notify", "none"]) == 0
    state = json.loads((tmp_path / "n.db.freshness.json").read_text())
    assert state["last_result"] == "fresh"


def test_main_stale_exits_one(tmp_path):
    db = tmp_path / "n.db"
    old = (datetime.now(timezone.utc) - timedelta(hours=60)).isoformat()
    _make_db(db, [("last_sync:tasks", old)])
    assert main(["--db", str(db), "--notify", "none"]) == 1


def test_main_missing_db_exits_one(tmp_path):
    assert main(["--db", str(tmp_path / "missing.db"), "--notify", "none"]) == 1


def test_main_stale_toasts_once_then_suppresses(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("check_freshness.show_toast", lambda t, b: calls.append((t, b)))
    db = tmp_path / "n.db"
    old = (datetime.now(timezone.utc) - timedelta(hours=60)).isoformat()
    _make_db(db, [("last_sync:tasks", old)])
    main(["--db", str(db)])
    main(["--db", str(db)])
    assert len(calls) == 1


def test_main_recovery_toast_then_clears_state(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("check_freshness.show_toast", lambda t, b: calls.append(t))
    db = tmp_path / "n.db"
    old = (datetime.now(timezone.utc) - timedelta(hours=60)).isoformat()
    _make_db(db, [("last_sync:tasks", old)])
    main(["--db", str(db)])
    conn = sqlite3.connect(db)
    conn.execute('UPDATE "_meta" SET value = ?', (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    conn.close()
    assert main(["--db", str(db), "--recovery-toast"]) == 0
    assert len(calls) == 2 and "fresh again" in calls[1]
    state = json.loads((tmp_path / "n.db.freshness.json").read_text())
    assert "alerting" not in state


def test_main_recovery_is_silent_by_default_but_still_clears_state(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("check_freshness.show_toast", lambda t, b: calls.append(t))
    db = tmp_path / "n.db"
    old = (datetime.now(timezone.utc) - timedelta(hours=60)).isoformat()
    _make_db(db, [("last_sync:tasks", old)])
    main(["--db", str(db)])
    conn = sqlite3.connect(db)
    conn.execute('UPDATE "_meta" SET value = ?', (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    conn.close()
    assert main(["--db", str(db)]) == 0
    assert len(calls) == 1
    state = json.loads((tmp_path / "n.db.freshness.json").read_text())
    assert "alerting" not in state


def test_main_snooze_suppresses_toast(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("check_freshness.show_toast", lambda t, b: calls.append(t))
    db = tmp_path / "n.db"
    old = (datetime.now(timezone.utc) - timedelta(hours=60)).isoformat()
    _make_db(db, [("last_sync:tasks", old)])
    assert main(["--db", str(db), "--snooze-hours", "48"]) == 0
    assert main(["--db", str(db)]) == 1
    assert calls == []


def test_damaged_copy_with_fresh_meta_is_reported(tmp_path):
    """`_meta` can be intact while a data page is destroyed; only a full-page check notices."""
    db = tmp_path / "n.db"
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE "_meta" (key TEXT PRIMARY KEY, value TEXT)')
    conn.execute('INSERT INTO "_meta" VALUES (?, ?)', ("last_sync:tasks", datetime.now(timezone.utc).isoformat()))
    conn.execute("CREATE TABLE big (x TEXT)")
    conn.executemany("INSERT INTO big VALUES (?)", [("y" * 500,)] * 400)
    conn.commit()
    conn.close()
    with open(db, "r+b") as f:  # overwrite one 4 KiB page that belongs to `big`
        f.seek(4096 * 20)
        f.write(b"\xff" * 4096)
    assert main(["--db", str(db), "--notify", "none"]) == 1


def test_toast_lines_groups_identical_messages():
    problems = {f"t{i}": "last synced 14 hours ago" for i in range(7)}
    assert toast_lines(problems) == ["7 tables: last synced 14 hours ago"]


def test_toast_lines_one_line_per_distinct_problem_and_caps():
    problems = {"a": "last synced 14 hours ago", "b": "last synced 20 hours ago", "c": "unreadable sync timestamp 'x'"}
    lines = toast_lines(problems)
    assert lines == ["a: last synced 14 hours ago", "+2 more problems"]


def test_main_suppress_success_hides_ok_line_but_not_stale_line(tmp_path, capsys):
    db = tmp_path / "n.db"
    _make_db(db, [("last_sync:tasks", datetime.now(timezone.utc).isoformat())])
    assert main(["--db", str(db), "--suppress-success", "--notify", "none"]) == 0
    assert capsys.readouterr().out == ""
    old = (datetime.now(timezone.utc) - timedelta(hours=60)).isoformat()
    _make_db(tmp_path / "o.db", [("last_sync:tasks", old)])
    assert main(["--db", str(tmp_path / "o.db"), "--suppress-success", "--notify", "none"]) == 1
    assert "STALE" in capsys.readouterr().out
