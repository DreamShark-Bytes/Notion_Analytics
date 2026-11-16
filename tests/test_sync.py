import sys
from sync import _expand_change_fields


def _row_with_dates():
    return {
        "page_id": "p1",
        "status": "Open",
        "due_date_start": "2026-06-01T09:00:00.000-05:00",
        "due_date_end": None,
        "closed_date_start": "2026-06-18T12:00:00.000-05:00",
        "closed_date_end": None,
    }


def test_base_field_in_row_unchanged():
    row = {"status": "Open", "due_date_start": "2026-06-01"}
    assert _expand_change_fields(["status"], row) == ["status"]


def test_split_field_expands_to_start_and_end():
    row = _row_with_dates()
    result = _expand_change_fields(["due_date"], row)
    assert result == ["due_date_start", "due_date_end"]


def test_split_field_only_start_exists():
    row = {"my_date_start": "2026-06-01"}
    result = _expand_change_fields(["my_date"], row)
    assert result == ["my_date_start"]


def test_unknown_field_preserved():
    # Field is in change_fields but not in this row — keep it (may appear on other pages)
    row = {"status": "Open"}
    result = _expand_change_fields(["nonexistent_field"], row)
    assert result == ["nonexistent_field"]


def test_empty_fields_returns_empty():
    assert _expand_change_fields([], _row_with_dates()) == []


def test_mixed_fields_expands_correctly():
    row = _row_with_dates()
    result = _expand_change_fields(["status", "due_date", "closed_date"], row)
    assert result == [
        "status",
        "due_date_start",
        "due_date_end",
        "closed_date_start",
        "closed_date_end",
    ]


def test_already_split_field_in_list_unchanged():
    # If someone puts "due_date_start" directly in change_fields, leave it alone
    row = _row_with_dates()
    result = _expand_change_fields(["due_date_start"], row)
    assert result == ["due_date_start"]


def test_expansion_is_idempotent():
    row = _row_with_dates()
    once = _expand_change_fields(["due_date"], row)
    twice = _expand_change_fields(once, row)
    assert once == twice


# ── Option ID wiring in record_change ─────────────────────────────────────────

def test_record_change_includes_option_ids():
    import sqlite3
    from storage import Storage

    s = Storage(":memory:")
    s.ensure_changes_table("tasks")

    s.record_change(
        "tasks", "p1", "status",
        "Open", "Done",
        "2026-06-18", "2026-06-18",
        old_value_id="opt_open",
        new_value_id="opt_done",
    )

    row = s.conn.execute(
        "SELECT old_value_id, new_value_id FROM tasks_changes WHERE field='status'"
    ).fetchone()
    assert row[0] == "opt_open"
    assert row[1] == "opt_done"


def test_record_change_null_ids_for_non_select():
    import sqlite3
    from storage import Storage

    s = Storage(":memory:")
    s.ensure_changes_table("tasks")

    s.record_change(
        "tasks", "p1", "name",
        "Old Name", "New Name",
        "2026-06-18", "2026-06-18",
    )

    row = s.conn.execute(
        "SELECT old_value_id, new_value_id FROM tasks_changes WHERE field='name'"
    ).fetchone()
    assert row[0] is None
    assert row[1] is None


def test_normalize_database_cfgs_accepts_database_id():
    from sync import _normalize_database_cfgs
    cfgs = [{"database_id": "abc", "name": "tasks"}]
    _normalize_database_cfgs(cfgs)
    assert cfgs[0]["database_id"] == "abc"


def test_normalize_database_cfgs_falls_back_to_legacy_id(caplog):
    from sync import _normalize_database_cfgs
    cfgs = [{"id": "abc", "name": "tasks"}]
    _normalize_database_cfgs(cfgs)
    assert cfgs[0]["database_id"] == "abc"
    assert "legacy key 'id'" in caplog.text


def test_normalize_database_cfgs_exits_when_no_id():
    import pytest
    from sync import _normalize_database_cfgs
    with pytest.raises(SystemExit):
        _normalize_database_cfgs([{"name": "tasks"}])


# --- exit code: a failed database must make the run fail (Task Scheduler "Last Run Result") ---

def _run_main(monkeypatch, tmp_path, results):
    """Run sync.main() with sync_database stubbed; `results` maps table name -> True/False/Exception."""
    import sync

    cfg = {
        "token": "x",
        "output": {"db_path": str(tmp_path / "t.db")},
        "databases": [{"name": n, "database_id": f"id-{n}"} for n in results],
    }

    def fake_sync_database(client, db_cfg, storage, **kwargs):
        outcome = results[db_cfg["name"]]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sync, "load_config", lambda path: cfg)
    monkeypatch.setattr(sync, "NotionClient", lambda token: object())
    monkeypatch.setattr(sync, "sync_database", fake_sync_database)
    monkeypatch.setattr(sys, "argv", ["sync.py"])
    sync.main()


def test_main_exits_zero_when_all_databases_succeed(monkeypatch, tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO)
    _run_main(monkeypatch, tmp_path, {"a": True, "b": True})
    assert "Sync complete" in caplog.text


def test_main_exits_one_when_a_database_returns_failure(monkeypatch, tmp_path, caplog):
    import pytest
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, tmp_path, {"a": True, "b": False})
    assert exc.value.code == 1
    assert "1 failed database(s)" in caplog.text and "b" in caplog.text
    assert "Sync complete" not in caplog.text


def test_main_exits_one_when_a_database_raises_and_still_syncs_the_rest(monkeypatch, tmp_path, caplog):
    import pytest
    seen = []
    import sync

    def fake_sync_database(client, db_cfg, storage, **kwargs):
        seen.append(db_cfg["name"])
        if db_cfg["name"] == "a":
            raise RuntimeError("boom")
        return True

    cfg = {"token": "x", "output": {"db_path": str(tmp_path / "t.db")},
           "databases": [{"name": "a", "database_id": "1"}, {"name": "b", "database_id": "2"}]}
    monkeypatch.setattr(sync, "load_config", lambda path: cfg)
    monkeypatch.setattr(sync, "NotionClient", lambda token: object())
    monkeypatch.setattr(sync, "sync_database", fake_sync_database)
    monkeypatch.setattr(sys, "argv", ["sync.py"])
    with pytest.raises(SystemExit) as exc:
        sync.main()
    assert exc.value.code == 1
    assert seen == ["a", "b"]


# --- optional snapshot step: one scheduled task does sync and snapshot ---

def _run_main_with_snapshot(monkeypatch, tmp_path, snap_dir):
    import sync
    cfg = {"token": "x",
           "output": {"db_path": str(tmp_path / "t.db"), "snapshot_dir": str(snap_dir), "snapshot_keep": 2},
           "databases": [{"name": "a", "database_id": "1"}]}
    monkeypatch.setattr(sync, "load_config", lambda path: cfg)
    monkeypatch.setattr(sync, "NotionClient", lambda token: object())
    monkeypatch.setattr(sync, "sync_database", lambda *a, **k: True)
    monkeypatch.setattr(sys, "argv", ["sync.py"])
    sync.main()


def test_main_writes_snapshot_when_configured(monkeypatch, tmp_path, caplog):
    import logging
    import sqlite3
    caplog.set_level(logging.INFO)
    # a real DB file so make_snapshot has something to copy and verify
    from storage import Storage
    s = Storage(str(tmp_path / "t.db")); s.ensure_meta_table(); s.set_meta("last_sync:a", "2026-11-16T00:00:00+00:00"); s.close()
    snap = tmp_path / "snaps"
    _run_main_with_snapshot(monkeypatch, tmp_path, snap)
    files = list(snap.glob("*.db"))
    assert len(files) == 1
    assert sqlite3.connect(files[0]).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert "Snapshot written" in caplog.text and "Sync complete" in caplog.text


def test_main_exits_one_when_snapshot_fails(monkeypatch, tmp_path, caplog):
    import pytest
    # no database file is ever created (sync_database is stubbed), so the snapshot cannot be made
    with pytest.raises(SystemExit) as exc:
        _run_main_with_snapshot(monkeypatch, tmp_path, tmp_path / "snaps")
    assert exc.value.code == 1
    assert "Snapshot failed" in caplog.text and "Sync complete" not in caplog.text
