import sqlite3
import pytest
from storage import Storage
from sync import _compare_schema, _upsert_options


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_storage() -> Storage:
    s = Storage(":memory:")
    s.ensure_schema_table()
    s.ensure_options_table()
    s.ensure_backups_table()
    return s


def _notion_schema(*props) -> dict:
    """Build a minimal Notion schema dict from (property_id, name, type) tuples."""
    return {
        "properties": {
            name: {"id": pid, "type": ptype, ptype: {}}
            for pid, name, ptype in props
        }
    }


# ── Storage roundtrip ─────────────────────────────────────────────────────────

def test_upsert_and_get_schema():
    s = _make_storage()
    s.upsert_schema("tasks", "prop1", "Status", "status", "select", "2026-01-01")
    stored = s.get_stored_schema("tasks")
    assert "prop1" in stored
    assert stored["prop1"]["property_name"] == "Status"
    assert stored["prop1"]["column_name"] == "status"
    assert stored["prop1"]["property_type"] == "select"


def test_upsert_schema_updates_on_conflict():
    s = _make_storage()
    s.upsert_schema("tasks", "prop1", "Old Name", "old_name", "select", "2026-01-01")
    s.upsert_schema("tasks", "prop1", "New Name", "new_name", "select", "2026-06-18")
    stored = s.get_stored_schema("tasks")
    assert stored["prop1"]["property_name"] == "New Name"
    assert stored["prop1"]["column_name"] == "new_name"


def test_upsert_and_get_options():
    s = _make_storage()
    s.upsert_option("tasks", "prop1", "opt1", "Open", None, "2026-01-01")
    s.upsert_option("tasks", "prop1", "opt2", "Done", None, "2026-01-01")
    opts = s.get_stored_options("tasks", "prop1")
    assert "opt1" in opts
    assert opts["opt1"]["option_name"] == "Open"
    assert "opt2" in opts


def test_upsert_option_updates_name():
    s = _make_storage()
    s.upsert_option("tasks", "prop1", "opt1", "In Progress", "In progress", "2026-01-01")
    s.upsert_option("tasks", "prop1", "opt1", "Active", "In progress", "2026-06-18")
    opts = s.get_stored_options("tasks", "prop1")
    assert opts["opt1"]["option_name"] == "Active"


def test_backup_column():
    s = _make_storage()
    s.conn.execute("CREATE TABLE tasks_pages (page_id TEXT, effort TEXT)")
    s.conn.execute("INSERT INTO tasks_pages VALUES ('p1', 'high')")
    backup = s.backup_column("tasks", "effort", "20260618")
    assert backup == "effort_bak_20260618"
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info('tasks_pages')")}
    assert "effort" not in cols
    assert "effort_bak_20260618" in cols
    val = s.conn.execute("SELECT effort_bak_20260618 FROM tasks_pages").fetchone()[0]
    assert val == "high"


# ── _compare_schema: first run populates _schema ──────────────────────────────

def test_first_run_populates_schema():
    s = _make_storage()
    schema = _notion_schema(
        ("pid1", "Status", "select"),
        ("pid2", "Due Date", "date"),
    )
    _compare_schema("tasks", schema, s, "2026-06-18T00:00:00Z")
    stored = s.get_stored_schema("tasks")
    assert "pid1" in stored
    assert "pid2" in stored
    assert stored["pid1"]["property_name"] == "Status"


# ── _compare_schema: rename detection ────────────────────────────────────────

def test_rename_detected_and_applied(caplog):
    s = _make_storage()
    s.conn.execute(
        "CREATE TABLE tasks_pages (page_id TEXT PRIMARY KEY, effort_level TEXT)"
    )
    s.conn.execute("INSERT INTO tasks_pages VALUES ('p1', 'high')")
    s.upsert_schema("tasks", "pid_effort", "Effort Level", "effort_level", "select", "2026-01-01")

    schema = _notion_schema(("pid_effort", "Effort", "select"))
    with caplog.at_level("INFO"):
        _compare_schema("tasks", schema, s, "2026-06-18T00:00:00Z")

    cols = {r[1] for r in s.conn.execute("PRAGMA table_info('tasks_pages')")}
    assert "effort" in cols
    assert "effort_level" not in cols
    assert any("renamed" in msg.lower() for msg in caplog.messages)


def test_rename_updates_schema_entry():
    s = _make_storage()
    s.conn.execute("CREATE TABLE tasks_pages (page_id TEXT PRIMARY KEY, effort_level TEXT)")
    s.upsert_schema("tasks", "pid_effort", "Effort Level", "effort_level", "select", "2026-01-01")

    schema = _notion_schema(("pid_effort", "Effort", "select"))
    _compare_schema("tasks", schema, s, "2026-06-18T00:00:00Z")

    stored = s.get_stored_schema("tasks")
    assert stored["pid_effort"]["property_name"] == "Effort"
    assert stored["pid_effort"]["column_name"] == "effort"


# ── _compare_schema: orphan detection ────────────────────────────────────────

def test_orphan_logs_warning(caplog):
    s = _make_storage()
    s.upsert_schema("tasks", "pid_deleted", "Old Field", "old_field", "select", "2026-01-01")
    schema = _notion_schema(("pid_other", "Status", "select"))
    with caplog.at_level("WARNING"):
        _compare_schema("tasks", schema, s, "2026-06-18T00:00:00Z")
    assert any("old_field" in msg for msg in caplog.messages)


# ── _compare_schema: type change ──────────────────────────────────────────────

def test_type_change_creates_backup(caplog):
    s = _make_storage()
    s.conn.execute(
        "CREATE TABLE tasks_pages (page_id TEXT PRIMARY KEY, effort TEXT)"
    )
    s.upsert_schema("tasks", "pid_effort", "Effort", "effort", "select", "2026-01-01")

    schema = _notion_schema(("pid_effort", "Effort", "rich_text"))
    with caplog.at_level("WARNING"):
        _compare_schema("tasks", schema, s, "2026-06-18T00:00:00Z")

    cols = {r[1] for r in s.conn.execute("PRAGMA table_info('tasks_pages')")}
    assert any(c.startswith("effort_bak_") for c in cols)
    assert any("type" in msg.lower() for msg in caplog.messages)


# ── _compare_schema: returns id shadow cols ───────────────────────────────────

def test_returns_id_shadow_columns():
    s = _make_storage()
    schema = _notion_schema(
        ("pid1", "Status", "select"),
        ("pid2", "Priority", "select"),
        ("pid3", "Due Date", "date"),
    )
    id_cols = _compare_schema("tasks", schema, s, "2026-06-18T00:00:00Z")
    assert "status_id" in id_cols
    assert "priority_id" in id_cols
    assert "due_date_id" not in id_cols  # date, not select


# ── _upsert_options ───────────────────────────────────────────────────────────

def test_upsert_options_select():
    s = _make_storage()
    schema = {
        "select": {
            "options": [
                {"id": "o1", "name": "Open"},
                {"id": "o2", "name": "Done"},
            ]
        }
    }
    _upsert_options("tasks", "pid1", "select", schema, s, "2026-06-18T00:00:00Z")
    opts = s.get_stored_options("tasks", "pid1")
    assert opts["o1"]["option_name"] == "Open"
    assert opts["o2"]["option_name"] == "Done"


def test_upsert_options_status_with_groups():
    s = _make_storage()
    schema = {
        "status": {
            "options": [
                {"id": "o1", "name": "Backlog"},
                {"id": "o2", "name": "In Progress"},
                {"id": "o3", "name": "Done"},
            ],
            "groups": [
                {"name": "Not started", "option_ids": ["o1"]},
                {"name": "In progress", "option_ids": ["o2"]},
                {"name": "Done", "option_ids": ["o3"]},
            ],
        }
    }
    _upsert_options("tasks", "pid1", "status", schema, s, "2026-06-18T00:00:00Z")
    opts = s.get_stored_options("tasks", "pid1")
    assert opts["o1"]["option_group"] == "Not started"
    assert opts["o2"]["option_group"] == "In progress"
    assert opts["o3"]["option_group"] == "Done"


# ── _backups: record / get ────────────────────────────────────────────────────

def test_record_and_get_backup():
    s = _make_storage()
    s.ensure_backups_table()
    s.record_backup("tasks", "effort", "effort_bak_20260715", "select", "2026-07-15T00:00:00Z")
    result = s.get_backup_for_type("tasks", "effort", "select")
    assert result == "effort_bak_20260715"
    assert s.get_backup_for_type("tasks", "effort", "rich_text") is None
    assert s.get_backup_for_type("tasks", "other_col", "select") is None


# ── restore_backup: swaps columns ────────────────────────────────────────────

def test_restore_backup_swaps_columns():
    s = _make_storage()
    s.ensure_backups_table()
    s.conn.execute(
        "CREATE TABLE tasks_pages (page_id TEXT PRIMARY KEY, effort TEXT, effort_bak_20260715 TEXT)"
    )
    s.conn.execute("INSERT INTO tasks_pages VALUES ('p1', 'wrong_type_data', 'original_data')")
    s.record_backup("tasks", "effort", "effort_bak_20260715", "select", "2026-07-15T00:00:00Z")

    result = s.restore_backup("tasks", "effort", "effort_bak_20260715")
    assert result is True

    cols = {r[1] for r in s.conn.execute("PRAGMA table_info('tasks_pages')")}
    assert "effort" in cols
    assert "effort_bak_20260715" not in cols

    val = s.conn.execute("SELECT effort FROM tasks_pages WHERE page_id = 'p1'").fetchone()[0]
    assert val == "original_data"

    assert s.get_backup_for_type("tasks", "effort", "select") is None


# ── restore_backup: stale entry ───────────────────────────────────────────────

def test_restore_backup_stale_entry():
    s = _make_storage()
    s.ensure_backups_table()
    s.conn.execute("CREATE TABLE tasks_pages (page_id TEXT PRIMARY KEY, effort TEXT)")
    s.record_backup("tasks", "effort", "effort_bak_20260101", "select", "2026-01-01T00:00:00Z")

    result = s.restore_backup("tasks", "effort", "effort_bak_20260101")
    assert result is False
    assert s.get_backup_for_type("tasks", "effort", "select") is None


# ── full flow: _compare_schema detects revert and restores ────────────────────

def test_type_revert_auto_restores_via_compare_schema():
    s = _make_storage()
    s.ensure_backups_table()
    s.conn.execute("CREATE TABLE tasks_pages (page_id TEXT PRIMARY KEY, effort TEXT)")
    s.conn.execute("INSERT INTO tasks_pages VALUES ('p1', 'high')")
    s.upsert_schema("tasks", "pid_effort", "Effort", "effort", "select", "2026-01-01")

    # Sync 1: type changes select → rich_text → backup created
    schema_b = _notion_schema(("pid_effort", "Effort", "rich_text"))
    _compare_schema("tasks", schema_b, s, "2026-07-15T00:00:00Z")

    cols = {r[1] for r in s.conn.execute("PRAGMA table_info('tasks_pages')")}
    assert any(c.startswith("effort_bak_") for c in cols)
    assert s.get_backup_for_type("tasks", "effort", "select") is not None

    # Sync 2: type reverts rich_text → select → backup restored
    schema_a = _notion_schema(("pid_effort", "Effort", "select"))
    _compare_schema("tasks", schema_a, s, "2026-07-16T00:00:00Z")

    cols = {r[1] for r in s.conn.execute("PRAGMA table_info('tasks_pages')")}
    assert "effort" in cols
    assert not any(c.startswith("effort_bak_") for c in cols)
    assert s.get_backup_for_type("tasks", "effort", "select") is None
    assert s.get_backup_for_type("tasks", "effort", "rich_text") is None


# ── backfill_option_names ─────────────────────────────────────────────────────

def test_backfill_option_names_updates_stale_display():
    s = _make_storage()
    s.ensure_backups_table()
    s.ensure_changes_table("tasks")
    s.upsert_option("tasks", "pid1", "opt1", "Active", None, "2026-07-15T00:00:00Z")
    s.conn.execute(
        'INSERT INTO "tasks_changes" (page_id, field, old_value, new_value, old_value_id, new_value_id, detected_at) '
        "VALUES ('p1', 'status', 'In Progress', 'Done', 'opt1', 'opt2', '2026-06-01T00:00:00Z')"
    )
    s.conn.commit()

    count = s.backfill_option_names("tasks")
    assert count >= 1

    row = s.conn.execute('SELECT old_value FROM "tasks_changes"').fetchone()
    assert row[0] == "Active"


def test_backfill_skips_already_matching_names():
    s = _make_storage()
    s.ensure_backups_table()
    s.ensure_changes_table("tasks")
    s.upsert_option("tasks", "pid1", "opt1", "Active", None, "2026-07-15T00:00:00Z")
    s.conn.execute(
        'INSERT INTO "tasks_changes" (page_id, field, old_value, new_value, old_value_id, new_value_id, detected_at) '
        "VALUES ('p1', 'status', 'Active', 'Done', 'opt1', 'opt2', '2026-06-01T00:00:00Z')"
    )
    s.conn.commit()

    count = s.backfill_option_names("tasks")
    assert count == 0
